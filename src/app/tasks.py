"""Celery tasks for the app."""

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from app import history_cache, metadata_utils
from app.interactive_requests import interactive_request_active
from app.log_safety import exception_summary
from app.models import (
    DISCOVER_MOVIE_METADATA_BACKFILL_VERSION,
    Item,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Sources,
)
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
    populate_trakt_episode_ratings_for_season,
    populate_trakt_popularity_backfill_queue,
    populate_trakt_popularity_data_for_items,
    reconcile_trakt_popularity,
)
from app.tasks_imdb import refresh_imdb_ratings_from_datasets  # noqa: E402
from app.tasks_genre import (  # noqa: E402
    GENRE_BACKFILL_ITEMS_QUEUE_KEY,
    GENRE_BACKFILL_ITEMS_SCHEDULED_KEY,
    GENRE_BACKFILL_QUEUE_TTL,
    GENRE_BACKFILL_RECONCILE_FALLBACK_INTERVAL_SECONDS,
    GENRE_BACKFILL_SOURCES,
    _genre_items_queryset,
    _populate_genres_for_items,
    enqueue_genre_backfill_items,
    ensure_genre_backfill_reconcile,
    is_genre_backfill_reconcile_complete,
    populate_genre_backfill_queue,
    populate_genre_data_for_items,
    reconcile_genre_backfill,
)
from app.tasks_credits import (  # noqa: E402
    CREDITS_BACKFILL_ITEMS_QUEUE_KEY,
    CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY,
    CREDITS_BACKFILL_QUEUE_TTL,
    CREDITS_BACKFILL_SOURCES,
    _missing_credits_item_ids,
    _next_credits_backfill_item_ids,
    _populate_credits_for_items,
    enqueue_credits_backfill_items,
    populate_credits_backfill_queue,
    populate_credits_data_for_items,
)
from app.tasks_runtime import (  # noqa: E402
    RUNTIME_BACKFILL_EPISODES_LOCK_PREFIX,
    RUNTIME_BACKFILL_EPISODES_LOCK_TTL,
    RUNTIME_BACKFILL_EPISODES_QUEUE_KEY,
    RUNTIME_BACKFILL_EPISODES_SCHEDULED_KEY,
    RUNTIME_BACKFILL_ITEMS_QUEUE_KEY,
    RUNTIME_BACKFILL_ITEMS_SCHEDULED_KEY,
    RUNTIME_BACKFILL_QUEUE_TTL,
    RUNTIME_BACKFILL_SOURCES,
    _episode_runtime_items_queryset,
    _filter_episode_runtime_season_keys,
    _normalize_season_keys,
    _populate_runtime_for_items,
    _reset_stale_give_up_episode_runtimes,
    _runtime_items_queryset,
    enqueue_episode_runtime_backfill,
    enqueue_runtime_backfill_items,
    populate_runtime_backfill_queue,
    populate_runtime_data_batch,
    populate_runtime_data_continuous,
    populate_runtime_data_for_items,
)
from app.tasks_metadata_cache import (  # noqa: E402
    _clear_item_metadata_cache,
    _exception_with_details,
    _fetch_item_metadata,
    _metadata_cache_keys_for_item,
)
from app.tasks_backfill_state import (  # noqa: E402
    GENRE_BACKFILL_VERSION,
    METADATA_BACKFILL_BASE_DELAY_SECONDS,
    METADATA_BACKFILL_MAX_DELAY_SECONDS,
    METADATA_BACKFILL_MAX_ATTEMPTS,
    _add_user_day_key,
    _apply_backfill_state_filters,
    _backfill_delay_seconds,
    _collect_backfill_day_keys,
    _filter_backfill_item_ids,
    _normalize_item_ids,
    _record_backfill_failure,
    _record_backfill_success,
    _schedule_metadata_statistics_refresh,
)

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
GAME_LENGTHS_BACKFILL_VERSION = 2
NIGHTLY_METADATA_QUALITY_GENRE_BATCH_SIZE = 1500
NIGHTLY_METADATA_QUALITY_RUNTIME_BATCH_SIZE = 500
NIGHTLY_METADATA_QUALITY_EPISODE_SEASONS_BATCH_SIZE = 300
NIGHTLY_METADATA_QUALITY_CREDITS_BATCH_SIZE = 2500
NIGHTLY_METADATA_QUALITY_CREDITS_SCAN_MULTIPLIER = 20
NIGHTLY_METADATA_QUALITY_TRAKT_POPULARITY_BATCH_SIZE = 300
NIGHTLY_METADATA_QUALITY_GENRE_COUNTDOWN = 5
NIGHTLY_METADATA_QUALITY_RUNTIME_COUNTDOWN = 15
NIGHTLY_METADATA_QUALITY_EPISODE_COUNTDOWN = 30
NIGHTLY_METADATA_QUALITY_CREDITS_COUNTDOWN = 45
NIGHTLY_METADATA_QUALITY_TRAKT_POPULARITY_COUNTDOWN = 60
DISCOVER_METADATA_REFRESH_DEBOUNCE_SECONDS = 60 * 10
DISCOVER_METADATA_REFRESH_COUNTDOWN_SECONDS = 60
BACKGROUND_TASK_PRIORITY = getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1)
HISTORY_COVERAGE_REPAIR_REQUEUE_SECONDS = 15
HISTORY_COVERAGE_REPAIR_INTERACTIVE_RETRY_SECONDS = 60


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

    refresh_imdb_ratings_from_datasets.apply_async(countdown=60)

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
    if interactive_request_active():
        cache.set(
            repair_key,
            {
                "started_at": timezone.now().isoformat(),
                "batch_size": batch_size,
                "deferred_for_interactive_request": True,
            },
            history_cache.HISTORY_COVERAGE_REPAIR_LOCK_TTL,
        )
        repair_history_day_cache_coverage_task.apply_async(
            kwargs={
                "user_id": user_id,
                "logging_style": logging_style,
                "batch_size": batch_size,
            },
            countdown=HISTORY_COVERAGE_REPAIR_INTERACTIVE_RETRY_SECONDS,
            priority=BACKGROUND_TASK_PRIORITY,
        )
        return {
            "skipped": True,
            "reason": "interactive_request_active",
        }
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
            countdown=HISTORY_COVERAGE_REPAIR_REQUEUE_SECONDS,
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
