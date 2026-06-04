"""Celery tasks for the app."""

import logging
from collections import defaultdict
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from app import credits as credit_helpers, helpers, history_cache, metadata_utils
from app.interactive_requests import interactive_request_active
from app.log_safety import exception_summary
from app.models import (
    CREDITS_BACKFILL_VERSION,
    DISCOVER_MOVIE_METADATA_BACKFILL_VERSION,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Sources,
    TRAKT_POPULARITY_BACKFILL_VERSION,
)
from app.providers import services
from app.services import game_lengths as game_length_services
from app.services import trakt_popularity as trakt_popularity_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Modular task re-exports
# These tasks are defined in focused sub-modules but re-exported here so all
# existing callers (import paths, test patches on "app.tasks.*", apps.py
# dynamic imports) continue to work without any call-site changes.
# ---------------------------------------------------------------------------
from app.tasks_bulk_plays import bulk_episode_plays_task, bulk_music_plays_task  # noqa: E402
from app.tasks_discover import (  # noqa: E402
    refresh_discover_profiles,
    refresh_discover_rows,
    refresh_discover_tab_cache,
    warm_discover_api_cache,
    warm_discover_startup_tabs,
    warm_history_day_cache_coverage,
)
from app.tasks_episode import populate_episode_runtime_data, populate_episode_runtime_queue  # noqa: E402
from app.tasks_music import (  # noqa: E402
    enrich_albums_task,
    enrich_music_library_task,
    fast_runtime_backfill_task,
    populate_album_tracks_batch,
    prefetch_album_covers_batch,
)
from app.tasks_trakt import (  # noqa: E402
    TRAKT_POPULARITY_BACKFILL_ITEMS_QUEUE_KEY,
    TRAKT_POPULARITY_BACKFILL_ITEMS_SCHEDULED_KEY,
    TRAKT_POPULARITY_BACKFILL_QUEUE_TTL,
    enqueue_trakt_popularity_backfill_items,
    populate_trakt_popularity_backfill_queue,
    populate_trakt_popularity_data_for_items,
    reconcile_trakt_popularity,
)

RUNTIME_BACKFILL_SOURCES = ("tmdb", "mal", "simkl")
RUNTIME_BACKFILL_QUEUE_TTL = 60 * 60  # 1 hour
RUNTIME_BACKFILL_ITEMS_QUEUE_KEY = "runtime_backfill_items_queue"
RUNTIME_BACKFILL_ITEMS_SCHEDULED_KEY = "runtime_backfill_items_scheduled"
RUNTIME_BACKFILL_EPISODES_QUEUE_KEY = "runtime_backfill_episode_queue"
RUNTIME_BACKFILL_EPISODES_SCHEDULED_KEY = "runtime_backfill_episode_scheduled"
RUNTIME_BACKFILL_EPISODES_LOCK_PREFIX = "runtime_backfill_episode_lock:"
RUNTIME_BACKFILL_EPISODES_LOCK_TTL = 60 * 5  # 5 minutes
GENRE_BACKFILL_SOURCES = (
    Sources.TMDB.value,
    Sources.MAL.value,
    "simkl",
    Sources.IGDB.value,
    Sources.BGG.value,
    Sources.OPENLIBRARY.value,
    Sources.HARDCOVER.value,
    Sources.COMICVINE.value,
    Sources.MANGAUPDATES.value,
)
GENRE_BACKFILL_QUEUE_TTL = 60 * 60  # 1 hour
GENRE_BACKFILL_ITEMS_QUEUE_KEY = "genre_backfill_items_queue"
GENRE_BACKFILL_ITEMS_SCHEDULED_KEY = "genre_backfill_items_scheduled"
CREDITS_BACKFILL_SOURCES = (Sources.TMDB.value,)
CREDITS_BACKFILL_QUEUE_TTL = 60 * 60  # 1 hour
CREDITS_BACKFILL_ITEMS_QUEUE_KEY = "credits_backfill_items_queue"
CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY = "credits_backfill_items_scheduled"
RELEASE_BACKFILL_SOURCES = (
    Sources.TMDB.value,
    Sources.MAL.value,
    Sources.MANGAUPDATES.value,
    Sources.IGDB.value,
    Sources.OPENLIBRARY.value,
    Sources.HARDCOVER.value,
    Sources.COMICVINE.value,
    Sources.BGG.value,
    Sources.MUSICBRAINZ.value,
)
RELEASE_BACKFILL_MEDIA_TYPES = (
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.SEASON.value,
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.GAME.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
    MediaTypes.BOARDGAME.value,
    MediaTypes.MUSIC.value,
)
TRACKED_TMDB_TV_REFRESH_STALE_AFTER = timedelta(days=1)
METADATA_BACKFILL_BASE_DELAY_SECONDS = 60 * 60  # 1 hour
METADATA_BACKFILL_MAX_DELAY_SECONDS = 60 * 60 * 24  # 1 day
METADATA_BACKFILL_MAX_ATTEMPTS = 6
GAME_LENGTHS_BACKFILL_VERSION = 2
GENRE_BACKFILL_VERSION = 3
NIGHTLY_METADATA_QUALITY_GENRE_BATCH_SIZE = 1500
NIGHTLY_METADATA_QUALITY_RUNTIME_BATCH_SIZE = 500
NIGHTLY_METADATA_QUALITY_EPISODE_SEASONS_BATCH_SIZE = 300
NIGHTLY_METADATA_QUALITY_CREDITS_BATCH_SIZE = 2500
NIGHTLY_METADATA_QUALITY_CREDITS_SCAN_MULTIPLIER = 20
NIGHTLY_METADATA_QUALITY_TRAKT_POPULARITY_BATCH_SIZE = 300
NIGHTLY_METADATA_QUALITY_GENRE_COUNTDOWN = 5
GENRE_BACKFILL_RECONCILE_FALLBACK_INTERVAL_SECONDS = 60 * 5
NIGHTLY_METADATA_QUALITY_RUNTIME_COUNTDOWN = 15
NIGHTLY_METADATA_QUALITY_EPISODE_COUNTDOWN = 30
NIGHTLY_METADATA_QUALITY_CREDITS_COUNTDOWN = 45
NIGHTLY_METADATA_QUALITY_TRAKT_POPULARITY_COUNTDOWN = 60
DISCOVER_METADATA_REFRESH_DEBOUNCE_SECONDS = 60 * 10
DISCOVER_METADATA_REFRESH_COUNTDOWN_SECONDS = 60
BACKGROUND_TASK_PRIORITY = getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1)


def _apply_backfill_state_filters(queryset, field: str):
    now = timezone.now()
    blocked = MetadataBackfillState.objects.filter(field=field).filter(
        Q(give_up=True) | Q(next_retry_at__gt=now),
    ).values("item_id")
    return queryset.exclude(id__in=blocked)


def _backfill_delay_seconds(fail_count: int) -> int:
    if fail_count <= 0:
        return METADATA_BACKFILL_BASE_DELAY_SECONDS
    delay = METADATA_BACKFILL_BASE_DELAY_SECONDS * (2 ** (fail_count - 1))
    return min(delay, METADATA_BACKFILL_MAX_DELAY_SECONDS)


def _record_backfill_failure(item: Item, field: str, error_message: str | None = None) -> bool:
    now = timezone.now()
    state, _ = MetadataBackfillState.objects.get_or_create(item=item, field=field)
    state.fail_count = min(state.fail_count + 1, 9999)
    state.last_attempt_at = now
    if error_message:
        state.last_error = str(error_message)[:500]
    if state.fail_count >= METADATA_BACKFILL_MAX_ATTEMPTS:
        state.give_up = True
        state.next_retry_at = None
    else:
        state.give_up = False
        state.next_retry_at = now + timedelta(seconds=_backfill_delay_seconds(state.fail_count))
    state.save(update_fields=[
        "fail_count",
        "last_attempt_at",
        "next_retry_at",
        "last_error",
        "give_up",
    ])
    if state.give_up:
        logger.warning(
            "metadata_backfill_give_up item_id=%s media_type=%s field=%s fail_count=%s has_reason=%s",
            item.id,
            item.media_type,
            field,
            state.fail_count,
            bool(error_message or state.last_error),
        )
    else:
        logger.info(
            "metadata_backfill_retry_later item_id=%s media_type=%s field=%s fail_count=%s next_retry_at=%s has_reason=%s",
            item.id,
            item.media_type,
            field,
            state.fail_count,
            state.next_retry_at.isoformat() if state.next_retry_at else None,
            bool(error_message or state.last_error),
        )
    return state.give_up


def _record_backfill_success(
    item: Item,
    field: str,
    strategy_version: int | None = None,
) -> None:
    now = timezone.now()
    state, _ = MetadataBackfillState.objects.get_or_create(item=item, field=field)
    state.fail_count = 0
    state.last_attempt_at = now
    state.next_retry_at = None
    state.last_success_at = now
    state.last_error = ""
    state.give_up = False
    update_fields = [
        "fail_count",
        "last_attempt_at",
        "next_retry_at",
        "last_success_at",
        "last_error",
        "give_up",
    ]
    if strategy_version is not None:
        state.strategy_version = int(strategy_version)
        update_fields.append("strategy_version")
    state.save(update_fields=update_fields)


def _filter_backfill_item_ids(item_ids, field: str):
    if not item_ids:
        return []
    now = timezone.now()
    blocked_ids = set(
        MetadataBackfillState.objects.filter(field=field, item_id__in=item_ids)
        .filter(Q(give_up=True) | Q(next_retry_at__gt=now))
        .values_list("item_id", flat=True)
    )
    if field == MetadataBackfillField.CREDITS:
        blocked_ids.update(
            MetadataBackfillState.objects.filter(
                field=field,
                item_id__in=item_ids,
                give_up=False,
                fail_count=0,
                last_success_at__isnull=False,
                strategy_version__gte=CREDITS_BACKFILL_VERSION,
            ).values_list("item_id", flat=True),
        )
    if field == MetadataBackfillField.GENRES:
        blocked_ids.update(
            MetadataBackfillState.objects.filter(
                field=field,
                item_id__in=item_ids,
                give_up=False,
                fail_count=0,
                last_success_at__isnull=False,
                strategy_version__gte=GENRE_BACKFILL_VERSION,
            ).values_list("item_id", flat=True),
        )
    return [item_id for item_id in item_ids if item_id not in blocked_ids]


def _add_user_day_key(user_day_keys, user_id, day_key):
    if not user_id or not day_key:
        return
    user_day_keys[user_id].add(day_key)


def _collect_backfill_day_keys(items, field: str):
    from app.models import Anime, Book, Comic, Episode, Game, Manga, Movie

    user_day_keys = defaultdict(set)
    if not items:
        return user_day_keys

    for item in items:
        if item.media_type == MediaTypes.MOVIE.value:
            rows = Movie.objects.filter(item_id=item.id).values(
                "user_id",
                "start_date",
                "end_date",
                "created_at",
            )
            for row in rows:
                activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
                _add_user_day_key(user_day_keys, row.get("user_id"), history_cache.history_day_key(activity_dt))
            continue

        if item.media_type == MediaTypes.ANIME.value:
            if field == MetadataBackfillField.GENRES:
                continue
            rows = Anime.objects.filter(item_id=item.id).values(
                "user_id",
                "start_date",
                "end_date",
                "created_at",
            )
            for row in rows:
                if field == MetadataBackfillField.RUNTIME and row.get("start_date") and row.get("end_date"):
                    day_keys = history_cache.history_day_keys_for_range(
                        row.get("start_date"),
                        row.get("end_date"),
                    )
                    if day_keys:
                        user_day_keys[row.get("user_id")].update(day_keys)
                    continue
                activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
                _add_user_day_key(user_day_keys, row.get("user_id"), history_cache.history_day_key(activity_dt))
            continue

        if item.media_type == MediaTypes.GAME.value:
            rows = Game.objects.filter(item_id=item.id).values(
                "user_id",
                "start_date",
                "end_date",
                "created_at",
            )
            for row in rows:
                activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
                _add_user_day_key(user_day_keys, row.get("user_id"), history_cache.history_day_key(activity_dt))
            continue

        if item.media_type in (
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        ):
            reading_models = {
                MediaTypes.BOOK.value: Book,
                MediaTypes.COMIC.value: Comic,
                MediaTypes.MANGA.value: Manga,
            }
            model = reading_models[item.media_type]
            rows = model.objects.filter(item_id=item.id).values(
                "user_id",
                "start_date",
                "end_date",
                "created_at",
            )
            for row in rows:
                activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
                _add_user_day_key(user_day_keys, row.get("user_id"), history_cache.history_day_key(activity_dt))
            continue

        if item.media_type == MediaTypes.TV.value and field in (
            MetadataBackfillField.GENRES,
            MetadataBackfillField.CREDITS,
        ):
            rows = Episode.objects.filter(
                related_season__related_tv__item_id=item.id,
            ).values("related_season__user_id", "end_date")
            for row in rows:
                _add_user_day_key(
                    user_day_keys,
                    row.get("related_season__user_id"),
                    history_cache.history_day_key(row.get("end_date")),
                )
            continue

        if item.media_type == MediaTypes.SEASON.value and field == MetadataBackfillField.CREDITS:
            rows = Episode.objects.filter(
                related_season__item_id=item.id,
            ).values("related_season__user_id", "end_date")
            for row in rows:
                _add_user_day_key(
                    user_day_keys,
                    row.get("related_season__user_id"),
                    history_cache.history_day_key(row.get("end_date")),
                )
            continue

        if item.media_type == MediaTypes.EPISODE.value and field == MetadataBackfillField.RUNTIME:
            rows = Episode.objects.filter(item_id=item.id).values(
                "related_season__user_id",
                "end_date",
            )
            for row in rows:
                _add_user_day_key(
                    user_day_keys,
                    row.get("related_season__user_id"),
                    history_cache.history_day_key(row.get("end_date")),
                )

    return user_day_keys


def _schedule_metadata_statistics_refresh(items, field: str, reason: str):
    if not items:
        return
    from app import statistics_cache

    user_day_keys = _collect_backfill_day_keys(items, field)
    for user_id, day_keys in user_day_keys.items():
        if not day_keys:
            continue
        statistics_cache.mark_metadata_refreshing(user_id, reason=reason)
        statistics_cache.invalidate_statistics_days(user_id, day_keys, reason=reason)
        statistics_cache.schedule_all_ranges_refresh(
            user_id,
            debounce_seconds=10,
            countdown=3,
            preferred_priority=BACKGROUND_TASK_PRIORITY,
            all_time_priority=BACKGROUND_TASK_PRIORITY,
        )
        logger.info(
            "metadata_refresh_scheduled user_id=%s field=%s days=%s reason=%s",
            user_id,
            field,
            len(day_keys),
            reason,
        )


def _runtime_items_queryset():
    queryset = Item.objects.filter(
        runtime_minutes__isnull=True,
        media_type__in=[
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.ANIME.value,
        ],
        source__in=RUNTIME_BACKFILL_SOURCES,
    ).exclude(
        runtime_minutes=999999,
    )
    return _apply_backfill_state_filters(queryset, MetadataBackfillField.RUNTIME)


def _episode_runtime_items_queryset():
    queryset = Item.objects.filter(
        Q(runtime_minutes__isnull=True) | Q(runtime_minutes__lte=0),
        media_type=MediaTypes.EPISODE.value,
        source__in=RUNTIME_BACKFILL_SOURCES,
    ).exclude(
        runtime_minutes=999999,
    )
    return _apply_backfill_state_filters(queryset, MetadataBackfillField.RUNTIME)


def _reset_stale_give_up_episode_runtimes():
    """Re-enable backfill for recently-aired episodes that gave up but may now have provider data.

    Episodes aired within the last 30 days (or with no known air date) are eligible.
    Items must have been last attempted more than 7 days ago to avoid immediate re-triggering.
    """
    now = timezone.now()
    attempt_cutoff = now - timedelta(days=7)
    recent_cutoff = now - timedelta(days=30)
    count = MetadataBackfillState.objects.filter(
        field=MetadataBackfillField.RUNTIME,
        give_up=True,
        last_attempt_at__lt=attempt_cutoff,
        item__media_type=MediaTypes.EPISODE.value,
        item__runtime_minutes__isnull=True,
        item__source__in=RUNTIME_BACKFILL_SOURCES,
    ).filter(
        Q(item__release_datetime__isnull=True)
        | Q(item__release_datetime__gte=recent_cutoff)
    ).update(
        give_up=False,
        fail_count=0,
        next_retry_at=None,
    )
    if count:
        logger.info("reset_stale_episode_runtime_give_up count=%s", count)
    return count


def _genre_items_queryset():
    from app.providers import tvdb

    tvdb_enabled = tvdb.enabled()
    genre_filters = Q(genres__isnull=True) | Q(genres=[])
    if tvdb_enabled:
        genre_filters |= Q(
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
        )

    queryset = Item.objects.filter(
        media_type__in=[
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.ANIME.value,
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        ],
        source__in=GENRE_BACKFILL_SOURCES,
    ).filter(genre_filters)
    queryset = _apply_backfill_state_filters(queryset, MetadataBackfillField.GENRES)
    completed_ids = MetadataBackfillState.objects.filter(
        field=MetadataBackfillField.GENRES,
        give_up=False,
        fail_count=0,
        last_success_at__isnull=False,
        strategy_version__gte=GENRE_BACKFILL_VERSION,
    ).values("item_id")
    return queryset.exclude(id__in=completed_ids)


def is_genre_backfill_reconcile_complete() -> bool:
    """Return whether the current genre strategy has no remaining candidates."""
    return not _genre_items_queryset().exists()


def _release_items_queryset():
    stale_tv_cutoff = timezone.now() - TRACKED_TMDB_TV_REFRESH_STALE_AFTER
    return Item.objects.filter(
        Q(
            release_datetime__isnull=True,
            media_type__in=RELEASE_BACKFILL_MEDIA_TYPES,
            source__in=RELEASE_BACKFILL_SOURCES,
        )
        | Q(
            # Revisit tracked TMDB shows even after first-air date is stored so
            # newly announced or started seasons can refresh time-left data.
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            metadata_fetched_at__isnull=False,
            metadata_fetched_at__lte=stale_tv_cutoff,
            tv__isnull=False,
        ),
    ).distinct()


def count_release_backfill_items() -> int:
    return _release_items_queryset().count()


def _discover_movie_metadata_items_queryset():
    queryset = Item.objects.filter(
        source=Sources.TMDB.value,
        media_type=MediaTypes.MOVIE.value,
        metadata_fetched_at__isnull=False,
    )
    queryset = _apply_backfill_state_filters(queryset, MetadataBackfillField.DISCOVER)
    completed_ids = MetadataBackfillState.objects.filter(
        field=MetadataBackfillField.DISCOVER,
        give_up=False,
        fail_count=0,
        last_success_at__isnull=False,
        strategy_version__gte=DISCOVER_MOVIE_METADATA_BACKFILL_VERSION,
    ).values("item_id")
    return queryset.exclude(id__in=completed_ids)


def count_discover_movie_metadata_backfill_items() -> int:
    return _discover_movie_metadata_items_queryset().count()


def _game_length_items_queryset():
    queryset = Item.objects.filter(
        source=Sources.IGDB.value,
        media_type=MediaTypes.GAME.value,
        metadata_fetched_at__isnull=False,
    ).exclude(
        provider_game_lengths_source=game_length_services.GAME_LENGTH_SOURCE_HLTB,
    ).exclude(
        provider_game_lengths_match=game_length_services.HLTB_MATCH_AMBIGUOUS,
    )
    queryset = _apply_backfill_state_filters(queryset, MetadataBackfillField.GAME_LENGTHS)
    completed_ids = MetadataBackfillState.objects.filter(
        field=MetadataBackfillField.GAME_LENGTHS,
        give_up=False,
        fail_count=0,
        last_success_at__isnull=False,
        strategy_version__gte=GAME_LENGTHS_BACKFILL_VERSION,
    ).values("item_id")
    return queryset.exclude(id__in=completed_ids)


def count_game_length_backfill_items() -> int:
    return _game_length_items_queryset().count()


def _initial_metadata_items_queryset():
    """Return initial metadata candidates, skipping Sonarr-seeded TV library rows.

    Sonarr imports can create large batches of season/episode rows using local
    library data. Treating those rows as generic "never fetched" metadata work
    causes avoidable provider storms and can monopolize SQLite during imports.
    """
    from django.db.models import Exists, OuterRef  # noqa: PLC0415

    from integrations.models import CollectionSourceState  # noqa: PLC0415

    sonarr_episode_collection_state = CollectionSourceState.objects.filter(
        source="sonarr",
        item__media_type=MediaTypes.EPISODE.value,
        item__media_id=OuterRef("media_id"),
        item__source=OuterRef("source"),
    )
    return (
        Item.objects.filter(metadata_fetched_at__isnull=True)
        .annotate(has_sonarr_episode_collection=Exists(sonarr_episode_collection_state))
        .exclude(
            media_type__in=[MediaTypes.SEASON.value, MediaTypes.EPISODE.value],
            has_sonarr_episode_collection=True,
        )
    )


def _schedule_discover_refresh_for_movie_items(items: list[Item]) -> None:
    movie_item_ids = [
        item.id
        for item in items
        if item.source == Sources.TMDB.value and item.media_type == MediaTypes.MOVIE.value
    ]
    if not movie_item_ids:
        return

    from app.discover import cache_repo
    from app.discover.registry import ALL_MEDIA_KEY
    from app.models import Movie

    user_ids = sorted(
        set(
            Movie.objects.filter(item_id__in=movie_item_ids).values_list("user_id", flat=True),
        ),
    )
    if not user_ids:
        return

    target_media_types = [MediaTypes.MOVIE.value, ALL_MEDIA_KEY]
    cache_repo.delete_taste_profiles(user_ids, target_media_types)
    cache_repo.delete_row_caches(user_ids, target_media_types)

    refresh_discover_profiles.apply_async(
        kwargs={
            "user_ids": user_ids,
            "media_types": target_media_types,
        },
        countdown=DISCOVER_METADATA_REFRESH_COUNTDOWN_SECONDS,
        priority=BACKGROUND_TASK_PRIORITY,
    )

    for user_id in user_ids:
        refresh_key = f"discover_movie_metadata_refresh:{user_id}"
        if not cache.add(
            refresh_key,
            True,
            timeout=DISCOVER_METADATA_REFRESH_DEBOUNCE_SECONDS,
        ):
            continue
        for media_type in target_media_types:
            refresh_discover_tab_cache.apply_async(
                kwargs={
                    "user_id": user_id,
                    "media_type": media_type,
                    "show_more": False,
                    "force": True,
                    "clear_provider_cache": False,
                },
                countdown=DISCOVER_METADATA_REFRESH_COUNTDOWN_SECONDS,
                priority=BACKGROUND_TASK_PRIORITY,
            )


def _metadata_cache_keys_for_item(item: Item):
    keys = {
        f"{item.source}_{item.media_type}_{item.media_id}",
    }
    if item.source == Sources.TMDB.value and item.media_type == MediaTypes.SEASON.value and item.season_number:
        keys.add(f"{item.source}_{item.media_type}_{item.media_id}_{item.season_number}")
    if (
        item.source == Sources.TMDB.value
        and item.media_type == MediaTypes.EPISODE.value
        and item.season_number
        and item.episode_number
    ):
        keys.add(
            f"{item.source}_{item.media_type}_{item.media_id}_{item.season_number}_{item.episode_number}",
        )
    if item.source == Sources.BGG.value and item.media_type == MediaTypes.BOARDGAME.value:
        keys.add(f"bgg_metadata_{item.media_id}")
    if item.source == Sources.MUSICBRAINZ.value and item.media_type == MediaTypes.MUSIC.value:
        keys.add(f"musicbrainz_recording_{item.media_id}")
    return [key for key in keys if key]


def _clear_item_metadata_cache(item: Item):
    keys = _metadata_cache_keys_for_item(item)
    if not keys:
        return
    try:
        cache.delete_many(keys)
    except Exception:  # pragma: no cover - cache backends may not support delete_many
        for key in keys:
            try:
                cache.delete(key)
            except Exception:
                continue


def _fetch_item_metadata(item: Item):
    if item.media_type == MediaTypes.SEASON.value:
        if item.season_number is None:
            raise ValueError("season item missing season_number")
        return services.get_media_metadata(
            item.media_type,
            item.media_id,
            item.source,
            [item.season_number],
        )
    if item.media_type == MediaTypes.EPISODE.value:
        if item.season_number is None or item.episode_number is None:
            raise ValueError("episode item missing season_number or episode_number")
        return services.get_media_metadata(
            item.media_type,
            item.media_id,
            item.source,
            [item.season_number],
            item.episode_number,
        )
    return services.get_media_metadata(
        item.media_type,
        item.media_id,
        item.source,
    )


@shared_task(name="Refresh item game lengths")
def refresh_item_game_lengths(item_id: int, force: bool = False, fetch_hltb: bool = True):
    """Refresh persisted game-length metadata for a game item."""
    lock_key = game_length_services.get_game_lengths_refresh_lock_key(
        item_id,
        force=force,
        fetch_hltb=fetch_hltb,
    )

    try:
        item = Item.objects.filter(id=item_id).first()
        if not item:
            return {"updated": False, "reason": "missing_item"}
        if item.source != Sources.IGDB.value or item.media_type != MediaTypes.GAME.value:
            return {"updated": False, "reason": "unsupported_item"}

        try:
            payload = game_length_services.refresh_game_lengths(
                item,
                force=force,
                fetch_hltb=fetch_hltb,
            )
        except Exception as exc:
            error_message = _exception_with_details(exc)
            _record_backfill_failure(
                item,
                MetadataBackfillField.GAME_LENGTHS,
                f"exception: {error_message}",
            )
            logger.error(
                "game_lengths_refresh_error item_id=%s media_id=%s error=%s",
                item.id,
                item.media_id,
                error_message,
            )
            return {
                "updated": False,
                "error": error_message,
                "item_id": item.id,
            }

        _record_backfill_success(
            item,
            MetadataBackfillField.GAME_LENGTHS,
            strategy_version=GAME_LENGTHS_BACKFILL_VERSION,
        )
        return {
            "updated": True,
            "item_id": item.id,
            "active_source": payload.get("active_source"),
            "match": item.provider_game_lengths_match,
        }
    finally:
        cache.delete(lock_key)


def _exception_with_details(exc: Exception) -> str:
    """Return a compact exception summary that preserves the message when present."""
    summary = exception_summary(exc)
    details = str(exc).strip()
    if details and details != summary:
        return f"{summary}: {details}"
    return summary


def _normalize_item_ids(item_ids):
    normalized = []
    for item_id in item_ids or []:
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            continue
        if item_id > 0:
            normalized.append(item_id)
    return sorted(set(normalized))


def _next_credits_backfill_item_ids(batch_size: int, scan_multiplier: int):
    if batch_size <= 0:
        return []
    candidate_limit = max(batch_size * max(scan_multiplier, 1), batch_size)
    candidates = (
            Item.objects.filter(
                source__in=CREDITS_BACKFILL_SOURCES,
                media_type__in=[
                    MediaTypes.MOVIE.value,
                    MediaTypes.TV.value,
                    MediaTypes.SEASON.value,
                    MediaTypes.EPISODE.value,
                ],
            )
            .order_by("id")
            .values_list("id", flat=True)[:candidate_limit]
    )
    candidate_ids = _filter_backfill_item_ids(list(candidates), MetadataBackfillField.CREDITS)
    if not candidate_ids:
        return []
    missing_ids = _missing_credits_item_ids(candidate_ids)
    return missing_ids[:batch_size]


def _missing_credits_item_ids(item_ids):
    return credit_helpers.missing_credits_backfill_item_ids(item_ids)


def _encode_season_key(media_id, source, season_number):
    if not media_id or not source or season_number is None:
        return None
    return f"{source}:{media_id}:{season_number}"


def _decode_season_key(token):
    if not token or not isinstance(token, str):
        return None
    try:
        source, media_id, season_str = token.split(":", 2)
        return media_id, source, int(season_str)
    except (ValueError, TypeError):
        return None


def _normalize_season_keys(season_keys):
    normalized = []
    for key in season_keys or []:
        if isinstance(key, (list, tuple)) and len(key) == 3:
            media_id, source, season_number = key
            token = _encode_season_key(media_id, source, season_number)
        else:
            token = key
        parsed = _decode_season_key(token)
        if parsed:
            normalized.append(parsed)
    return sorted(set(normalized))


def _filter_episode_runtime_season_keys(season_keys):
    normalized = _normalize_season_keys(season_keys)
    if not normalized:
        return []
    season_filters = Q()
    for media_id, source, season_number in normalized:
        season_filters |= Q(
            media_id=media_id,
            source=source,
            season_number=season_number,
        )
    if not season_filters:
        return []
    eligible = _episode_runtime_items_queryset().filter(season_filters).values_list(
        "media_id",
        "source",
        "season_number",
    )
    return sorted(set(eligible))


def enqueue_runtime_backfill_items(item_ids, countdown=10):
    normalized = _normalize_item_ids(item_ids)
    normalized = _filter_backfill_item_ids(normalized, MetadataBackfillField.RUNTIME)
    if not normalized:
        return 0
    try:
        queue = cache.get(RUNTIME_BACKFILL_ITEMS_QUEUE_KEY) or []
        queue = list(set(queue).union(normalized))
        cache.set(RUNTIME_BACKFILL_ITEMS_QUEUE_KEY, queue, timeout=RUNTIME_BACKFILL_QUEUE_TTL)
        if cache.add(RUNTIME_BACKFILL_ITEMS_SCHEDULED_KEY, True, timeout=30):
            populate_runtime_backfill_queue.apply_async(countdown=countdown)
    except Exception as exc:  # pragma: no cover - cache unavailable
        logger.debug("Runtime backfill queue unavailable: %s", exception_summary(exc))
        populate_runtime_data_for_items.apply_async(args=[normalized], countdown=countdown)
    return len(normalized)


def enqueue_episode_runtime_backfill(season_keys, countdown=10):
    normalized = _filter_episode_runtime_season_keys(season_keys)
    if not normalized:
        return 0
    tokens = []
    try:
        for media_id, source, season_number in normalized:
            token = _encode_season_key(media_id, source, season_number)
            if not token:
                continue
            lock_key = f"{RUNTIME_BACKFILL_EPISODES_LOCK_PREFIX}{token}"
            if cache.add(lock_key, True, timeout=RUNTIME_BACKFILL_EPISODES_LOCK_TTL):
                tokens.append(token)
        if not tokens:
            return 0
        queue = cache.get(RUNTIME_BACKFILL_EPISODES_QUEUE_KEY) or []
        queue = list(set(queue).union(tokens))
        cache.set(RUNTIME_BACKFILL_EPISODES_QUEUE_KEY, queue, timeout=RUNTIME_BACKFILL_QUEUE_TTL)
        if cache.add(RUNTIME_BACKFILL_EPISODES_SCHEDULED_KEY, True, timeout=30):
            populate_episode_runtime_queue.apply_async(countdown=countdown)
    except Exception as exc:  # pragma: no cover - cache unavailable
        logger.debug("Episode runtime backfill queue unavailable: %s", exception_summary(exc))
        populate_episode_runtime_data.apply_async(kwargs={"season_keys": normalized}, countdown=countdown)
        return len(normalized)
    return len(tokens)


def enqueue_genre_backfill_items(item_ids, countdown=10):
    normalized = _normalize_item_ids(item_ids)
    normalized = _filter_backfill_item_ids(normalized, MetadataBackfillField.GENRES)
    if not normalized:
        return 0
    try:
        queue = cache.get(GENRE_BACKFILL_ITEMS_QUEUE_KEY) or []
        queue = list(set(queue).union(normalized))
        cache.set(GENRE_BACKFILL_ITEMS_QUEUE_KEY, queue, timeout=GENRE_BACKFILL_QUEUE_TTL)
        if cache.add(GENRE_BACKFILL_ITEMS_SCHEDULED_KEY, True, timeout=30):
            populate_genre_backfill_queue.apply_async(countdown=countdown)
    except Exception as exc:  # pragma: no cover - cache unavailable
        logger.debug("Genre backfill queue unavailable: %s", exception_summary(exc))
        populate_genre_data_for_items.apply_async(args=[normalized], countdown=countdown)
    return len(normalized)


def enqueue_credits_backfill_items(item_ids, countdown=10):
    normalized = _normalize_item_ids(item_ids)
    normalized = _filter_backfill_item_ids(normalized, MetadataBackfillField.CREDITS)
    normalized = _missing_credits_item_ids(normalized)
    if not normalized:
        return 0
    try:
        queue = cache.get(CREDITS_BACKFILL_ITEMS_QUEUE_KEY) or []
        queue = list(set(queue).union(normalized))
        cache.set(CREDITS_BACKFILL_ITEMS_QUEUE_KEY, queue, timeout=CREDITS_BACKFILL_QUEUE_TTL)
        if cache.add(CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY, True, timeout=30):
            populate_credits_backfill_queue.apply_async(countdown=countdown)
    except Exception as exc:  # pragma: no cover - cache unavailable
        logger.debug("Credits backfill queue unavailable: %s", exception_summary(exc))
        populate_credits_data_for_items.apply_async(args=[normalized], countdown=countdown)
    return len(normalized)


def _resolve_tmdb_tv_item_tvdb_id(item: Item, tmdb_metadata: dict | None) -> str | None:
    """Return a TVDB series ID for a TMDB TV item, persisting discovered mapping."""
    from app.services import metadata_resolution

    if not (
        item.source == Sources.TMDB.value
        and item.media_type == MediaTypes.TV.value
    ):
        return None

    if isinstance(tmdb_metadata, dict):
        metadata_resolution.upsert_provider_links(
            item,
            tmdb_metadata,
            provider=item.source,
            provider_media_type=item.media_type,
        )

    tvdb_id = metadata_resolution.resolve_provider_media_id(
        item,
        Sources.TVDB.value,
        route_media_type=MediaTypes.TV.value,
    )
    return str(tvdb_id) if tvdb_id else None


def _tmdb_tv_item_is_tvdb_anime(item: Item, tmdb_metadata: dict | None) -> bool:
    """Return whether TVDB classifies a TMDB TV item as Anime."""
    from app.providers import tvdb
    from app.services import metadata_resolution

    if not tvdb.enabled():
        return False

    tvdb_id = _resolve_tmdb_tv_item_tvdb_id(item, tmdb_metadata)
    if not tvdb_id:
        return False

    tvdb_metadata = services.get_media_metadata(
        MediaTypes.TV.value,
        tvdb_id,
        Sources.TVDB.value,
    )
    if not isinstance(tvdb_metadata, dict):
        msg = "no tvdb metadata"
        raise ValueError(msg)

    metadata_resolution.upsert_provider_links(
        item,
        tvdb_metadata,
        provider=Sources.TVDB.value,
        provider_media_type=MediaTypes.TV.value,
    )
    return tvdb.series_has_anime_genre(tvdb_id, tv_data=tvdb_metadata)


def _populate_genres_for_items(items, delay_seconds):
    from app.providers import tvdb

    updated_count = 0
    error_count = 0
    updated_items = []
    for item in items:
        try:
            metadata = services.get_media_metadata(
                item.media_type.lower(),
                item.media_id,
                item.source,
            )

            if not isinstance(metadata, dict):
                logger.warning(
                    "No metadata returned for %s (%s, %s)",
                    item.title,
                    item.media_type,
                    item.source,
                )
                error_count += 1
                _record_backfill_failure(item, MetadataBackfillField.GENRES, "no metadata")
                continue

            source_genres = metadata_utils.extract_metadata_genres(metadata)
            incoming_genres = source_genres or metadata_utils.normalize_genres(item.genres)
            if not incoming_genres:
                logger.warning("No genre data available for %s", item.title)
                error_count += 1
                _record_backfill_failure(item, MetadataBackfillField.GENRES, "no genres")
                continue

            add_anime = False
            strategy_version = GENRE_BACKFILL_VERSION
            if item.source == Sources.TMDB.value and item.media_type == MediaTypes.TV.value:
                if tvdb.enabled():
                    add_anime = _tmdb_tv_item_is_tvdb_anime(item, metadata)
                else:
                    # Keep TMDB TV rows eligible for a future re-run after TVDB
                    # gets configured, while still persisting the TMDB genres now.
                    strategy_version = None

            genre_update_fields = metadata_utils.apply_item_genres(
                item,
                incoming_genres,
                add_anime=add_anime,
            )
            if genre_update_fields:
                with transaction.atomic():
                    item.save(update_fields=genre_update_fields)
                updated_items.append(item)

            _record_backfill_success(
                item,
                MetadataBackfillField.GENRES,
                strategy_version=strategy_version,
            )
            updated_count += 1
            logger.info("Updated genres for %s: %s", item.title, item.genres)

            if delay_seconds > 0:
                import time

                time.sleep(delay_seconds)
        except Exception as exc:
            error_count += 1
            logger.error("Error updating genres for %s: %s", item.title, exception_summary(exc))
            _record_backfill_failure(item, MetadataBackfillField.GENRES, f"exception: {exception_summary(exc)}")

    logger.info("Genre population batch completed: %s updated, %s errors", updated_count, error_count)
    if updated_items:
        _schedule_metadata_statistics_refresh(
            updated_items,
            MetadataBackfillField.GENRES,
            "genres_backfill",
        )
    return updated_count, error_count


def _populate_credits_for_items(items, delay_seconds):
    from app import credits

    updated_count = 0
    error_count = 0
    updated_items = []

    for item in items:
        try:
            if item.media_type == MediaTypes.EPISODE.value and (
                item.season_number is None or item.episode_number is None
            ):
                logger.warning(
                    "Episode item %s is missing season/episode numbers; skipping credits backfill",
                    item.id,
                )
                error_count += 1
                _record_backfill_failure(
                    item,
                    MetadataBackfillField.CREDITS,
                    "missing season/episode numbers",
                )
                continue

            if item.media_type == MediaTypes.SEASON.value and item.season_number is None:
                logger.warning(
                    "Season item %s is missing season_number; skipping credits backfill",
                    item.id,
                )
                error_count += 1
                _record_backfill_failure(
                    item,
                    MetadataBackfillField.CREDITS,
                    "missing season number",
                )
                continue

            metadata = _fetch_item_metadata(item)

            if not isinstance(metadata, dict):
                logger.warning(
                    "No metadata returned for %s (%s, %s)",
                    item.title,
                    item.media_type,
                    item.source,
                )
                error_count += 1
                _record_backfill_failure(item, MetadataBackfillField.CREDITS, "no metadata")
                continue

            has_payload = any(key in metadata for key in ("cast", "crew", "studios_full"))
            if not has_payload:
                logger.warning("No credits payload available for %s", item.title)
                error_count += 1
                _record_backfill_failure(item, MetadataBackfillField.CREDITS, "no credits payload")
                continue

            credits.sync_item_credits_from_metadata(item, metadata)
            _record_backfill_success(
                item,
                MetadataBackfillField.CREDITS,
                strategy_version=CREDITS_BACKFILL_VERSION,
            )
            updated_count += 1
            updated_items.append(item)

            if delay_seconds > 0:
                import time

                time.sleep(delay_seconds)
        except Exception as exc:
            error_count += 1
            logger.error("Error syncing credits for %s: %s", item.title, exception_summary(exc))
            _record_backfill_failure(item, MetadataBackfillField.CREDITS, f"exception: {exception_summary(exc)}")

    logger.info("Credits population batch completed: %s updated, %s errors", updated_count, error_count)
    if updated_items:
        _schedule_metadata_statistics_refresh(
            updated_items,
            MetadataBackfillField.CREDITS,
            "credits_backfill",
        )
    return updated_count, error_count


def _populate_runtime_for_items(items, delay_seconds):
    from app.statistics import parse_runtime_to_minutes

    updated_count = 0
    error_count = 0
    updated_items = []
    def _mark_runtime_failure(item, reason):
        give_up = _record_backfill_failure(item, MetadataBackfillField.RUNTIME, reason)
        if give_up:
            try:
                with transaction.atomic():
                    item.runtime_minutes = 999999
                    item.save(update_fields=["runtime_minutes"])
                logger.warning(
                    "Marked %s as failed (runtime_minutes=999999) after %s",
                    item.title,
                    reason,
                )
            except Exception as save_error:
                logger.error("Failed to mark %s as failed: %s", item.title, save_error)
        return give_up

    for item in items:
        try:
            metadata = services.get_media_metadata(
                item.media_type.lower(),
                item.media_id,
                item.source,
            )

            if not metadata:
                logger.warning("No metadata returned for %s (%s, %s)", item.title, item.media_type, item.source)
                error_count += 1
                _mark_runtime_failure(item, "no metadata")
                continue

            if not isinstance(metadata, dict):
                logger.warning("Invalid metadata format for %s: %s", item.title, type(metadata))
                error_count += 1
                _mark_runtime_failure(item, "invalid metadata")
                continue

            if not metadata.get("details"):
                logger.warning("No details in metadata for %s", item.title)
                error_count += 1
                _mark_runtime_failure(item, "missing details")
                continue

            details = metadata["details"]
            runtime_str = details.get("runtime")

            if not runtime_str:
                logger.warning("No runtime data available for %s", item.title)
                error_count += 1
                _mark_runtime_failure(item, "no runtime")
                continue

            runtime_minutes = parse_runtime_to_minutes(runtime_str)

            if runtime_minutes is None:
                logger.warning("Failed to parse runtime '%s' for %s", runtime_str, item.title)
                error_count += 1
                _mark_runtime_failure(item, "parse failure")
                continue

            with transaction.atomic():
                item.runtime_minutes = runtime_minutes
                item.save(update_fields=["runtime_minutes"])

            _record_backfill_success(item, MetadataBackfillField.RUNTIME)
            updated_count += 1
            updated_items.append(item)
            logger.info("Updated runtime for %s: %s minutes", item.title, runtime_minutes)

            if delay_seconds > 0:
                import time

                time.sleep(delay_seconds)
        except Exception as exc:
            error_count += 1
            logger.error("Error updating runtime for %s: %s", item.title, exception_summary(exc))
            _mark_runtime_failure(item, f"exception: {exception_summary(exc)}")

    logger.info("Runtime population batch completed: %s updated, %s errors", updated_count, error_count)
    if updated_items:
        _schedule_metadata_statistics_refresh(
            updated_items,
            MetadataBackfillField.RUNTIME,
            "runtime_backfill",
        )
    return updated_count, error_count


@shared_task
def populate_runtime_data_batch(batch_size=10, delay_seconds=1.0):
    """Populate runtime data for a batch of items that don't have it."""
    items_to_update = list(_runtime_items_queryset().order_by("id")[:batch_size])

    if not items_to_update:
        logger.info("No items need runtime data")
        return {"updated": 0, "errors": 0}

    updated_count, error_count = _populate_runtime_for_items(items_to_update, delay_seconds)

    # Check if there are more items to process (exclude previously failed items)
    remaining_items = _runtime_items_queryset().count()

    if remaining_items > 0:
        logger.info(f"Found {remaining_items} remaining items. Scheduling next batch...")
        # Schedule the next batch with a small delay
        populate_runtime_data_batch.apply_async(
            kwargs={"batch_size": batch_size, "delay_seconds": delay_seconds},
            countdown=5,  # 5 second delay between batches
        )
        return {
            "updated": updated_count,
            "errors": error_count,
            "remaining_items": remaining_items,
            "next_batch_scheduled": True,
        }
    logger.info("🎉 All runtime data population completed! No more items need processing.")

    # Mark as completed in cache to prevent repeated runs
    from django.core.cache import cache
    cache.set("runtime_population_completed", True, timeout=3600)  # 1 hour

    return {
        "updated": updated_count,
        "errors": error_count,
        "remaining_items": 0,
        "next_batch_scheduled": False,
        "completion_message": "All runtime data populated successfully!",
    }


@shared_task
def populate_runtime_data_for_items(item_ids: list[int], delay_seconds: float = 0.0):
    """Populate runtime data for a targeted list of item IDs."""
    normalized = _normalize_item_ids(item_ids)
    if not normalized:
        return {"updated": 0, "errors": 0, "message": "No item IDs provided"}

    items_to_update = list(_runtime_items_queryset().filter(id__in=normalized))
    if not items_to_update:
        logger.info("No targeted items need runtime data")
        return {"updated": 0, "errors": 0, "message": "No targeted items need runtime data"}

    updated_count, error_count = _populate_runtime_for_items(items_to_update, delay_seconds)
    return {
        "updated": updated_count,
        "errors": error_count,
        "message": f"Processed {len(items_to_update)} targeted items",
    }


@shared_task
def populate_genre_data_for_items(item_ids: list[int], delay_seconds: float = 0.0):
    """Populate genre data for a targeted list of item IDs."""
    normalized = _normalize_item_ids(item_ids)
    if not normalized:
        return {"updated": 0, "errors": 0, "message": "No item IDs provided"}

    items_to_update = list(_genre_items_queryset().filter(id__in=normalized))
    if not items_to_update:
        logger.info("No targeted items need genre data")
        return {"updated": 0, "errors": 0, "message": "No targeted items need genre data"}

    updated_count, error_count = _populate_genres_for_items(items_to_update, delay_seconds)
    return {
        "updated": updated_count,
        "errors": error_count,
        "message": f"Processed {len(items_to_update)} targeted items",
    }


@shared_task
def populate_credits_data_for_items(item_ids: list[int], delay_seconds: float = 0.0):
    """Populate cast/crew/studio credits for a targeted list of item IDs."""
    normalized = _normalize_item_ids(item_ids)
    normalized = _filter_backfill_item_ids(normalized, MetadataBackfillField.CREDITS)
    normalized = _missing_credits_item_ids(normalized)
    if not normalized:
        return {"updated": 0, "errors": 0, "message": "No targeted items need credits data"}

    items_to_update = list(
        Item.objects.filter(
            id__in=normalized,
            source__in=CREDITS_BACKFILL_SOURCES,
            media_type__in=[
                MediaTypes.MOVIE.value,
                MediaTypes.TV.value,
                MediaTypes.SEASON.value,
                MediaTypes.EPISODE.value,
            ],
        ),
    )
    if not items_to_update:
        logger.info("No targeted items need credits data")
        return {"updated": 0, "errors": 0, "message": "No targeted items need credits data"}

    updated_count, error_count = _populate_credits_for_items(items_to_update, delay_seconds)
    return {
        "updated": updated_count,
        "errors": error_count,
        "message": f"Processed {len(items_to_update)} targeted items",
    }


@shared_task
def populate_runtime_backfill_queue(batch_size: int = 50, delay_seconds: float = 0.0):
    """Drain the runtime backfill queue and process items in small batches."""
    queue = cache.get(RUNTIME_BACKFILL_ITEMS_QUEUE_KEY) or []
    if not queue:
        cache.delete(RUNTIME_BACKFILL_ITEMS_SCHEDULED_KEY)
        return {"processed": 0, "message": "No queued runtime items"}

    cache.delete(RUNTIME_BACKFILL_ITEMS_SCHEDULED_KEY)
    batch = queue[:batch_size]
    remaining = queue[batch_size:]
    if remaining:
        cache.set(RUNTIME_BACKFILL_ITEMS_QUEUE_KEY, remaining, timeout=RUNTIME_BACKFILL_QUEUE_TTL)
        if cache.add(RUNTIME_BACKFILL_ITEMS_SCHEDULED_KEY, True, timeout=30):
            populate_runtime_backfill_queue.apply_async(countdown=10)
    else:
        cache.delete(RUNTIME_BACKFILL_ITEMS_QUEUE_KEY)

    return populate_runtime_data_for_items(batch, delay_seconds=delay_seconds)


@shared_task
def populate_genre_backfill_queue(batch_size: int = 50, delay_seconds: float = 0.0):
    """Drain the genre backfill queue and process items in small batches."""
    queue = cache.get(GENRE_BACKFILL_ITEMS_QUEUE_KEY) or []
    if not queue:
        cache.delete(GENRE_BACKFILL_ITEMS_SCHEDULED_KEY)
        return {"processed": 0, "message": "No queued genre items"}

    cache.delete(GENRE_BACKFILL_ITEMS_SCHEDULED_KEY)
    batch = queue[:batch_size]
    remaining = queue[batch_size:]
    if remaining:
        cache.set(GENRE_BACKFILL_ITEMS_QUEUE_KEY, remaining, timeout=GENRE_BACKFILL_QUEUE_TTL)
        if cache.add(GENRE_BACKFILL_ITEMS_SCHEDULED_KEY, True, timeout=30):
            populate_genre_backfill_queue.apply_async(countdown=10)
    else:
        cache.delete(GENRE_BACKFILL_ITEMS_QUEUE_KEY)

    return populate_genre_data_for_items(batch, delay_seconds=delay_seconds)


@shared_task
def populate_credits_backfill_queue(batch_size: int = 50, delay_seconds: float = 0.0):
    """Drain the credits backfill queue and process items in small batches."""
    queue = cache.get(CREDITS_BACKFILL_ITEMS_QUEUE_KEY) or []
    if not queue:
        cache.delete(CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY)
        return {"processed": 0, "message": "No queued credits items"}

    cache.delete(CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY)
    batch = queue[:batch_size]
    remaining = queue[batch_size:]
    if remaining:
        cache.set(CREDITS_BACKFILL_ITEMS_QUEUE_KEY, remaining, timeout=CREDITS_BACKFILL_QUEUE_TTL)
        if cache.add(CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY, True, timeout=30):
            populate_credits_backfill_queue.apply_async(countdown=10)
    else:
        cache.delete(CREDITS_BACKFILL_ITEMS_QUEUE_KEY)

    return populate_credits_data_for_items(batch, delay_seconds=delay_seconds)


@shared_task
def reconcile_genre_backfill(
    strategy_version: int | None = None,
    batch_size: int = NIGHTLY_METADATA_QUALITY_GENRE_BATCH_SIZE,
):
    """Queue all current genre-backfill candidates without waiting for the nightly sweep."""
    batch_size = max(int(batch_size), 1)
    last_item_id = 0
    selected = 0
    enqueued = 0

    while True:
        batch_ids = list(
            _genre_items_queryset()
            .filter(id__gt=last_item_id)
            .order_by("id")
            .values_list("id", flat=True)[:batch_size],
        )
        if not batch_ids:
            break

        last_item_id = batch_ids[-1]
        selected += len(batch_ids)
        enqueued += enqueue_genre_backfill_items(batch_ids, countdown=10)

    if strategy_version is not None:
        cache.set(
            f"genre_backfill_reconciled_v{strategy_version}",
            "done",
            timeout=None,
        )

    logger.info(
        "reconcile_genre_backfill selected=%d enqueued=%d version=%s",
        selected,
        enqueued,
        strategy_version,
    )
    return {"selected": selected, "enqueued": enqueued}


@shared_task(name="Ensure genre backfill reconcile")
def ensure_genre_backfill_reconcile(
    strategy_version: int | None = None,
    batch_size: int = NIGHTLY_METADATA_QUALITY_GENRE_BATCH_SIZE,
):
    """Retry the current genre strategy reconcile until it has completed."""
    if interactive_request_active():
        logger.info("ensure_genre_backfill_reconcile skipped reason=interactive_request_active")
        return {"skipped": True, "reason": "interactive_request_active"}

    resolved_strategy_version = int(strategy_version or GENRE_BACKFILL_VERSION)
    version_key = f"genre_backfill_reconciled_v{resolved_strategy_version}"
    status = cache.get(version_key)
    reconcile_complete = is_genre_backfill_reconcile_complete()

    if reconcile_complete:
        cache.set(version_key, "done", timeout=None)
        logger.debug(
            "ensure_genre_backfill_reconcile skipped version=%s status=done",
            resolved_strategy_version,
        )
        return {"skipped": True, "reason": "done"}

    if status == "pending":
        logger.debug(
            "ensure_genre_backfill_reconcile skipped version=%s status=pending",
            resolved_strategy_version,
        )
        return {"skipped": True, "reason": "pending"}

    if status == "done":
        logger.info(
            "ensure_genre_backfill_reconcile rerunning version=%s stale_cache_done=1",
            resolved_strategy_version,
        )

    return reconcile_genre_backfill(
        strategy_version=resolved_strategy_version,
        batch_size=batch_size,
    )


@shared_task(name="Nightly metadata quality backfill")
def nightly_metadata_quality_backfill_task(
    genre_batch_size: int = NIGHTLY_METADATA_QUALITY_GENRE_BATCH_SIZE,
    runtime_batch_size: int = NIGHTLY_METADATA_QUALITY_RUNTIME_BATCH_SIZE,
    episode_season_batch_size: int = NIGHTLY_METADATA_QUALITY_EPISODE_SEASONS_BATCH_SIZE,
    credits_batch_size: int = NIGHTLY_METADATA_QUALITY_CREDITS_BATCH_SIZE,
    credits_scan_multiplier: int = NIGHTLY_METADATA_QUALITY_CREDITS_SCAN_MULTIPLIER,
    trakt_popularity_batch_size: int = NIGHTLY_METADATA_QUALITY_TRAKT_POPULARITY_BATCH_SIZE,
):
    """Queue targeted metadata backfill batches for genres/runtime/credits.

    This runs on a nightly schedule and uses queue-based workers so metadata quality
    converges over time without requiring user-triggered maintenance commands.
    """
    genre_batch_size = max(int(genre_batch_size), 0)
    runtime_batch_size = max(int(runtime_batch_size), 0)
    episode_season_batch_size = max(int(episode_season_batch_size), 0)
    credits_batch_size = max(int(credits_batch_size), 0)
    credits_scan_multiplier = max(int(credits_scan_multiplier), 1)
    trakt_popularity_batch_size = max(int(trakt_popularity_batch_size), 0)

    _reset_stale_give_up_episode_runtimes()

    genre_item_ids = []
    if genre_batch_size:
        genre_item_ids = list(
            _genre_items_queryset().order_by("id").values_list("id", flat=True)[:genre_batch_size],
        )

    runtime_item_ids = []
    if runtime_batch_size:
        runtime_item_ids = list(
            _runtime_items_queryset().order_by("id").values_list("id", flat=True)[:runtime_batch_size],
        )

    episode_season_keys = []
    if episode_season_batch_size:
        episode_season_keys = list(
            _episode_runtime_items_queryset()
            .exclude(season_number__isnull=True)
            .values_list("media_id", "source", "season_number")
            .distinct()
            .order_by("media_id", "source", "season_number")[:episode_season_batch_size],
        )

    credits_item_ids = _next_credits_backfill_item_ids(
        credits_batch_size,
        scan_multiplier=credits_scan_multiplier,
    )
    trakt_popularity_item_ids = []
    if trakt_popularity_batch_size and trakt_popularity_service.trakt_provider.is_configured():
        trakt_popularity_item_ids = [
            item.id
            for item in trakt_popularity_service.select_items_for_refresh(
                limit=trakt_popularity_batch_size,
            )
        ]

    queued_genres = 0
    if genre_item_ids:
        queued_genres = enqueue_genre_backfill_items(
            genre_item_ids,
            countdown=NIGHTLY_METADATA_QUALITY_GENRE_COUNTDOWN,
        )

    queued_runtime = 0
    if runtime_item_ids:
        queued_runtime = enqueue_runtime_backfill_items(
            runtime_item_ids,
            countdown=NIGHTLY_METADATA_QUALITY_RUNTIME_COUNTDOWN,
        )

    queued_episode_seasons = 0
    if episode_season_keys:
        queued_episode_seasons = enqueue_episode_runtime_backfill(
            episode_season_keys,
            countdown=NIGHTLY_METADATA_QUALITY_EPISODE_COUNTDOWN,
        )

    queued_credits = 0
    if credits_item_ids:
        queued_credits = enqueue_credits_backfill_items(
            credits_item_ids,
            countdown=NIGHTLY_METADATA_QUALITY_CREDITS_COUNTDOWN,
        )

    queued_trakt_popularity = 0
    if trakt_popularity_item_ids:
        queued_trakt_popularity = enqueue_trakt_popularity_backfill_items(
            trakt_popularity_item_ids,
            countdown=NIGHTLY_METADATA_QUALITY_TRAKT_POPULARITY_COUNTDOWN,
        )

    summary = {
        "selected": {
            "genres": len(genre_item_ids),
            "runtime": len(runtime_item_ids),
            "episode_seasons": len(episode_season_keys),
            "credits": len(credits_item_ids),
            "trakt_popularity": len(trakt_popularity_item_ids),
        },
        "queued": {
            "genres": queued_genres,
            "runtime": queued_runtime,
            "episode_seasons": queued_episode_seasons,
            "credits": queued_credits,
            "trakt_popularity": queued_trakt_popularity,
        },
        "remaining": {
            "genres": _genre_items_queryset().count(),
            "runtime": _runtime_items_queryset().count(),
            "episode_runtime": _episode_runtime_items_queryset().count(),
            "trakt_popularity": len(
                trakt_popularity_service.select_items_for_refresh(),
            )
            if trakt_popularity_service.trakt_provider.is_configured()
            else 0,
        },
    }
    logger.info("nightly_metadata_quality_backfill summary=%s", summary)
    return summary


@shared_task
def refresh_history_cache_task(
    user_id: int,
    logging_style: str = "repeats",
    warm_days: int | None = None,
    day_keys=None,
    *args,
    **kwargs,
):
    """Rebuild the cached History page for a user."""
    if logging_style not in ("sessions", "repeats"):
        for candidate in (logging_style, *args, kwargs.get("logging_style")):
            if candidate in ("sessions", "repeats"):
                logging_style = candidate
                break
        else:
            logging_style = "repeats"
    if warm_days is None:
        for candidate in (*args, kwargs.get("warm_days")):
            if candidate is None:
                continue
            try:
                warm_days = int(candidate)
                break
            except (TypeError, ValueError):
                continue
    if warm_days is not None and warm_days < 0:
        warm_days = None
    if day_keys is None:
        candidate = kwargs.get("day_keys")
        if candidate:
            day_keys = candidate
    if day_keys is None:
        for candidate in args:
            if isinstance(candidate, (list, tuple)):
                day_keys = candidate
                break
    history_cache.refresh_history_cache(
        user_id,
        logging_style=logging_style,
        warm_days=warm_days,
        day_keys=day_keys,
    )


@shared_task(name="Repair History Day Cache Coverage")
def repair_history_day_cache_coverage_task(
    user_id: int,
    logging_style: str = "repeats",
    batch_size: int | None = None,
):
    """Repair missing persisted history day payloads without blocking navigation."""
    repair_key = history_cache._coverage_repair_key(user_id, logging_style)
    result = history_cache.repair_history_day_cache_coverage(
        user_id,
        logging_style=logging_style,
        batch_size=batch_size,
    )
    if result.get("remaining"):
        cache.set(
            repair_key,
            {
                "started_at": timezone.now().isoformat(),
                "batch_size": batch_size,
            },
            history_cache.HISTORY_COVERAGE_REPAIR_LOCK_TTL,
        )
        repair_history_day_cache_coverage_task.apply_async(
            kwargs={
                "user_id": user_id,
                "logging_style": logging_style,
                "batch_size": batch_size,
            },
            countdown=5,
            priority=BACKGROUND_TASK_PRIORITY,
        )
    else:
        cache.delete(repair_key)
    return result


@shared_task
def refresh_statistics_cache_task(user_id: int, range_name: str):
    """Rebuild the cached Statistics page for a user and range."""
    from app import statistics_cache
    statistics_cache.refresh_statistics_cache(user_id, range_name)


@shared_task
def populate_runtime_data_continuous():
    """Populate runtime data for ALL items that don't have it (startup task)."""
    from django.core.cache import cache

    from app.models import Item, MediaTypes

    # Check if runtime population has already been completed recently (within last hour)
    cache_key = "runtime_population_completed"
    if cache.get(cache_key):
        # Check if episodes also need runtime data
        episodes_needing_runtime = Item.objects.filter(
            runtime_minutes__isnull=True,
            media_type=MediaTypes.EPISODE.value,
            source__in=RUNTIME_BACKFILL_SOURCES,
        ).exclude(runtime_minutes=999999).count()

        if episodes_needing_runtime > 0:
            logger.info(f"Runtime population completed for movies/TV/anime, but {episodes_needing_runtime} episodes still need runtime data. Starting episode population...")
            # Clear the cache and continue with episode population
            cache.delete(cache_key)
        else:
            logger.info("Runtime population already completed recently - skipping")
            return {"total_items": 0, "batches_scheduled": 0, "message": "Already completed recently"}

    # Count total items that need runtime data (exclude previously failed items)
    total_items = _runtime_items_queryset().count()

    if total_items == 0:
        # Check if episodes also need runtime data
        episodes_needing_runtime = Item.objects.filter(
            runtime_minutes__isnull=True,
            media_type=MediaTypes.EPISODE.value,
            source__in=RUNTIME_BACKFILL_SOURCES,
        ).exclude(runtime_minutes=999999).count()

        if episodes_needing_runtime > 0:
            logger.info(f"No movies/TV/anime need runtime data, but {episodes_needing_runtime} episodes still need runtime data. Starting episode population...")
            # Start episode population
            episode_result = populate_episode_runtime_data.delay()
            return {
                "total_items": 0,
                "episode_task_id": episode_result.id,
                "message": f"Movies/TV/anime up to date, starting episode population for {episodes_needing_runtime} episodes",
            }
        logger.info("No items need runtime data - all up to date!")
        # Mark as completed for 1 hour to prevent repeated runs
        cache.set(cache_key, True, timeout=3600)
        return {"total_items": 0, "batches_scheduled": 0, "message": "All up to date - marked as completed"}

    logger.info(f"Found {total_items} items that need runtime data. Starting comprehensive population...")

    # Start the first batch - it will chain itself if more items remain
    first_batch = populate_runtime_data_batch.delay(batch_size=20, delay_seconds=1.0)

    # Also start episode runtime population
    episode_result = populate_episode_runtime_data.delay()

    return {
        "total_items": total_items,
        "first_task_id": first_batch.id,
        "episode_task_id": episode_result.id,
        "message": "Started comprehensive runtime population for movies/TV/anime and episodes. Check logs for progress.",
    }


@shared_task(name="Backfill item metadata")
def backfill_item_metadata_task(batch_size: int = 10, game_length_batch_size: int | None = None):
    """Backfill metadata fields, missing release dates, and game-length metadata.

    Args:
        batch_size: Number of items to process in this batch (default: 10)
        game_length_batch_size: Max already-fetched IGDB games to enrich with game lengths.

    Returns:
        dict: Results including success_count, error_count, and message
    """
    if game_length_batch_size is None:
        game_length_batch_size = min(max(int(batch_size), 0), 25)
    else:
        game_length_batch_size = max(int(game_length_batch_size), 0)

    if interactive_request_active():
        logger.info("metadata_backfill_skipped reason=interactive_request_active")
        return {
            "skipped": True,
            "reason": "interactive_request_active",
            "success_count": 0,
            "error_count": 0,
            "remaining_metadata": _initial_metadata_items_queryset().count(),
            "remaining_release": count_release_backfill_items(),
            "remaining_discover_movie_metadata": count_discover_movie_metadata_backfill_items(),
            "remaining_game_lengths": count_game_length_backfill_items(),
            "message": "Skipped metadata backfill while an interactive request was active",
        }

    initial_items = list(_initial_metadata_items_queryset().order_by("id")[:batch_size])
    initial_item_ids = [item.id for item in initial_items]
    remaining_slots = max(batch_size - len(initial_items), 0)
    game_length_backfill_items = []
    release_backfill_items = []
    discover_backfill_items = []

    if remaining_slots > 0 and game_length_batch_size > 0:
        game_length_limit = min(remaining_slots, game_length_batch_size)
        game_length_backfill_items = list(
            _game_length_items_queryset()
            .exclude(id__in=initial_item_ids)
            .order_by("provider_game_lengths_fetched_at", "metadata_fetched_at", "id")[:game_length_limit],
        )
        remaining_slots = max(remaining_slots - len(game_length_backfill_items), 0)

    if remaining_slots > 0:
        selected_ids = initial_item_ids + [item.id for item in game_length_backfill_items]
        release_backfill_items = list(
            _release_items_queryset()
            .filter(metadata_fetched_at__isnull=False)
            .exclude(id__in=selected_ids)
            .order_by("metadata_fetched_at", "id")[:remaining_slots],
        )
        remaining_slots = max(remaining_slots - len(release_backfill_items), 0)

    if remaining_slots > 0:
        release_item_ids = [item.id for item in release_backfill_items]
        selected_ids = initial_item_ids + [item.id for item in game_length_backfill_items] + release_item_ids
        discover_backfill_items = list(
            _discover_movie_metadata_items_queryset()
            .exclude(id__in=selected_ids)
            .order_by("metadata_fetched_at", "id")[:remaining_slots],
        )

    items = initial_items + release_backfill_items + discover_backfill_items + game_length_backfill_items
    if not items:
        return {
            "success_count": 0,
            "error_count": 0,
            "remaining_metadata": 0,
            "remaining_release": 0,
            "remaining_discover_movie_metadata": 0,
            "remaining_game_lengths": 0,
            "message": "No items need metadata, release-date, Discover metadata, or game-length backfill",
        }

    success_count = 0
    error_count = 0
    release_updated_count = 0
    processed_movie_discover_items: list[Item] = []
    discover_item_ids = {item.id for item in discover_backfill_items}
    game_length_item_ids = {item.id for item in game_length_backfill_items}
    deferred_for_interactive_request = False

    for index, item in enumerate(items):
        if index > 0 and interactive_request_active():
            deferred_for_interactive_request = True
            logger.info(
                "metadata_backfill_deferred reason=interactive_request_active processed=%s remaining=%s",
                success_count + error_count,
                len(items) - index,
            )
            break
        initial_metadata_backfill = item.metadata_fetched_at is None
        discover_metadata_backfill = item.id in discover_item_ids
        game_lengths_backfill = item.id in game_length_item_ids
        try:
            if item.release_datetime is None:
                _clear_item_metadata_cache(item)

            metadata = _fetch_item_metadata(item)

            update_fields = []

            if initial_metadata_backfill:
                update_fields.extend(
                    metadata_utils.apply_item_metadata(
                        item,
                        metadata,
                        include_core=True,
                        include_provider=True,
                        include_release=True,
                    ),
                )
            else:
                update_fields.extend(
                    metadata_utils.apply_item_metadata(
                        item,
                        metadata,
                        include_core=False,
                        include_provider=True,
                        include_release=True,
                    ),
                )

            if "release_datetime" in update_fields:
                release_updated_count += 1

            item.metadata_fetched_at = timezone.now()
            update_fields.append("metadata_fetched_at")

            item.save(update_fields=update_fields)

            if item.source == Sources.IGDB.value and item.media_type == MediaTypes.GAME.value:
                try:
                    game_length_services.refresh_game_lengths(
                        item,
                        igdb_metadata=metadata,
                        force=False,
                        fetch_hltb=True,
                    )
                    _record_backfill_success(
                        item,
                        MetadataBackfillField.GAME_LENGTHS,
                        strategy_version=GAME_LENGTHS_BACKFILL_VERSION,
                    )
                except Exception as exc:
                    error_message = _exception_with_details(exc)
                    _record_backfill_failure(
                        item,
                        MetadataBackfillField.GAME_LENGTHS,
                        f"exception: {error_message}",
                    )
                    logger.warning(
                        "game_lengths_backfill_error item_id=%s media_id=%s error=%s",
                        item.id,
                        item.media_id,
                        error_message,
                    )

            if item.source == Sources.TMDB.value and item.media_type == MediaTypes.TV.value:
                from events.calendar.main import cleanup_invalid_events, save_events
                from events.calendar.tv import process_tv

                tv_events_bulk = []
                process_tv(
                    item,
                    tv_events_bulk,
                    tv_metadata=metadata,
                )
                if tv_events_bulk:
                    save_events(tv_events_bulk)
                    cleanup_invalid_events(tv_events_bulk)

            if item.source == Sources.TMDB.value and item.media_type == MediaTypes.MOVIE.value:
                _record_backfill_success(
                    item,
                    MetadataBackfillField.DISCOVER,
                    strategy_version=DISCOVER_MOVIE_METADATA_BACKFILL_VERSION,
                )
                processed_movie_discover_items.append(item)

            success_count += 1
            logger.info(
                (
                    "metadata_backfill_success item_id=%s media_type=%s "
                    "country=%s format=%s release_datetime=%s initial=%s discover=%s game_lengths=%s"
                ),
                item.id,
                item.media_type,
                item.country,
                item.format,
                item.release_datetime.isoformat() if item.release_datetime else None,
                initial_metadata_backfill,
                discover_metadata_backfill,
                game_lengths_backfill,
            )

        except Exception as e:
            error_count += 1
            if item.source == Sources.TMDB.value and item.media_type == MediaTypes.MOVIE.value:
                _record_backfill_failure(
                    item,
                    MetadataBackfillField.DISCOVER,
                    f"exception: {exception_summary(e)}",
                )
            # Still mark as fetched even if there was an error, to avoid retrying infinitely
            item.metadata_fetched_at = timezone.now()
            item.save(update_fields=["metadata_fetched_at"])

            logger.error(
                "metadata_backfill_error item_id=%s media_type=%s error=%s",
                item.id,
                item.media_type,
                exception_summary(e),
            )

    remaining_metadata = _initial_metadata_items_queryset().count()
    remaining_release = count_release_backfill_items()
    remaining_discover_movie_metadata = count_discover_movie_metadata_backfill_items()
    remaining_game_lengths = count_game_length_backfill_items()

    if processed_movie_discover_items:
        _schedule_discover_refresh_for_movie_items(processed_movie_discover_items)

    result = {
        "success_count": success_count,
        "release_updated_count": release_updated_count,
        "error_count": error_count,
        "remaining_metadata": remaining_metadata,
        "remaining_release": remaining_release,
        "remaining_discover_movie_metadata": remaining_discover_movie_metadata,
        "remaining_game_lengths": remaining_game_lengths,
        "remaining": remaining_metadata,
        "message": (
            f"Processed {success_count + error_count} items, "
            f"{remaining_metadata} metadata items remaining, "
            f"{remaining_release} release items remaining, "
            f"{remaining_discover_movie_metadata} Discover movie items remaining, "
            f"{remaining_game_lengths} game-length items remaining"
        ),
    }
    if deferred_for_interactive_request:
        result["deferred"] = True
        result["reason"] = "interactive_request_active"
    return result
