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

from app import history_cache
from app.models import (
    Item,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
)
from app.providers import services

logger = logging.getLogger(__name__)

RUNTIME_BACKFILL_SOURCES = ("tmdb", "mal", "simkl")
RUNTIME_BACKFILL_QUEUE_TTL = 60 * 60  # 1 hour
RUNTIME_BACKFILL_ITEMS_QUEUE_KEY = "runtime_backfill_items_queue"
RUNTIME_BACKFILL_ITEMS_SCHEDULED_KEY = "runtime_backfill_items_scheduled"
RUNTIME_BACKFILL_EPISODES_QUEUE_KEY = "runtime_backfill_episode_queue"
RUNTIME_BACKFILL_EPISODES_SCHEDULED_KEY = "runtime_backfill_episode_scheduled"
RUNTIME_BACKFILL_EPISODES_LOCK_PREFIX = "runtime_backfill_episode_lock:"
RUNTIME_BACKFILL_EPISODES_LOCK_TTL = 60 * 5  # 5 minutes
GENRE_BACKFILL_SOURCES = ("tmdb", "mal", "simkl", "igdb", "bgg")
GENRE_BACKFILL_QUEUE_TTL = 60 * 60  # 1 hour
GENRE_BACKFILL_ITEMS_QUEUE_KEY = "genre_backfill_items_queue"
GENRE_BACKFILL_ITEMS_SCHEDULED_KEY = "genre_backfill_items_scheduled"
METADATA_BACKFILL_BASE_DELAY_SECONDS = 60 * 60  # 1 hour
METADATA_BACKFILL_MAX_DELAY_SECONDS = 60 * 60 * 24  # 1 day
METADATA_BACKFILL_MAX_ATTEMPTS = 6


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
    reason = error_message or state.last_error or "unknown"
    if state.give_up:
        logger.warning(
            "metadata_backfill_give_up item_id=%s media_type=%s field=%s fail_count=%s reason=%s",
            item.id,
            item.media_type,
            field,
            state.fail_count,
            reason,
        )
    else:
        logger.info(
            "metadata_backfill_retry_later item_id=%s media_type=%s field=%s fail_count=%s next_retry_at=%s reason=%s",
            item.id,
            item.media_type,
            field,
            state.fail_count,
            state.next_retry_at.isoformat() if state.next_retry_at else None,
            reason,
        )
    return state.give_up


def _record_backfill_success(item: Item, field: str) -> None:
    now = timezone.now()
    MetadataBackfillState.objects.filter(item=item, field=field).update(
        fail_count=0,
        last_attempt_at=now,
        next_retry_at=None,
        last_success_at=now,
        last_error="",
        give_up=False,
    )


def _filter_backfill_item_ids(item_ids, field: str):
    if not item_ids:
        return []
    now = timezone.now()
    blocked = set(
        MetadataBackfillState.objects.filter(field=field, item_id__in=item_ids)
        .filter(Q(give_up=True) | Q(next_retry_at__gt=now))
        .values_list("item_id", flat=True)
    )
    return [item_id for item_id in item_ids if item_id not in blocked]


def _add_user_day_key(user_day_keys, user_id, day_key):
    if not user_id or not day_key:
        return
    user_day_keys[user_id].add(day_key)


def _collect_backfill_day_keys(items, field: str):
    from app.models import Anime, Episode, Game, Movie

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

        if item.media_type == MediaTypes.TV.value and field == MetadataBackfillField.GENRES:
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
        statistics_cache.schedule_all_ranges_refresh(user_id, debounce_seconds=10, countdown=3)
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


def _genre_items_queryset():
    queryset = Item.objects.filter(
        Q(genres__isnull=True) | Q(genres=[]),
        media_type__in=[
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.ANIME.value,
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
        ],
        source__in=GENRE_BACKFILL_SOURCES,
    )
    return _apply_backfill_state_filters(queryset, MetadataBackfillField.GENRES)


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
        logger.debug("Runtime backfill queue unavailable: %s", exc)
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
        logger.debug("Episode runtime backfill queue unavailable: %s", exc)
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
        logger.debug("Genre backfill queue unavailable: %s", exc)
        populate_genre_data_for_items.apply_async(args=[normalized], countdown=countdown)
    return len(normalized)


def _extract_genres_from_metadata(metadata):
    from app.statistics import _coerce_genre_list

    if not isinstance(metadata, dict):
        return []
    details = metadata.get("details")
    genres_raw = []
    if isinstance(details, dict):
        genres_raw = details.get("genres") or details.get("genre") or []
    if not genres_raw:
        genres_raw = metadata.get("genres") or metadata.get("genre") or []
    genres = _coerce_genre_list(genres_raw)
    return list(dict.fromkeys([genre for genre in genres if genre]))


def _populate_genres_for_items(items, delay_seconds):
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

            if not metadata:
                logger.warning("No metadata returned for %s (%s, %s)", item.title, item.media_type, item.source)
                error_count += 1
                _record_backfill_failure(item, MetadataBackfillField.GENRES, "no metadata")
                continue

            genres = _extract_genres_from_metadata(metadata)
            if not genres:
                logger.warning("No genre data available for %s", item.title)
                error_count += 1
                _record_backfill_failure(item, MetadataBackfillField.GENRES, "no genres")
                continue

            with transaction.atomic():
                item.genres = genres
                item.save(update_fields=["genres"])

            _record_backfill_success(item, MetadataBackfillField.GENRES)
            updated_count += 1
            updated_items.append(item)
            logger.info("Updated genres for %s: %s", item.title, genres)

            if delay_seconds > 0:
                import time

                time.sleep(delay_seconds)
        except Exception as exc:
            error_count += 1
            logger.error("Error updating genres for %s: %s", item.title, exc)
            _record_backfill_failure(item, MetadataBackfillField.GENRES, f"exception: {exc}")

    logger.info("Genre population batch completed: %s updated, %s errors", updated_count, error_count)
    if updated_items:
        _schedule_metadata_statistics_refresh(
            updated_items,
            MetadataBackfillField.GENRES,
            "genres_backfill",
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
            logger.error("Error updating runtime for %s: %s", item.title, exc)
            _mark_runtime_failure(item, f"exception: {exc}")

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
def populate_episode_runtime_queue(batch_size: int = 20):
    """Drain the episode runtime queue and process seasons in small batches."""
    queue = cache.get(RUNTIME_BACKFILL_EPISODES_QUEUE_KEY) or []
    if not queue:
        cache.delete(RUNTIME_BACKFILL_EPISODES_SCHEDULED_KEY)
        return {"processed": 0, "message": "No queued episode runtime seasons"}

    cache.delete(RUNTIME_BACKFILL_EPISODES_SCHEDULED_KEY)
    batch = queue[:batch_size]
    remaining = queue[batch_size:]
    if remaining:
        cache.set(RUNTIME_BACKFILL_EPISODES_QUEUE_KEY, remaining, timeout=RUNTIME_BACKFILL_QUEUE_TTL)
        if cache.add(RUNTIME_BACKFILL_EPISODES_SCHEDULED_KEY, True, timeout=30):
            populate_episode_runtime_queue.apply_async(countdown=10)
    else:
        cache.delete(RUNTIME_BACKFILL_EPISODES_QUEUE_KEY)

    return populate_episode_runtime_data(season_keys=batch)


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


@shared_task
def enrich_music_library_task(user_id: int):
    """Post-import enrichment/dedupe for a user's music library."""
    from app.models import Album, Artist, Music
    from app.services.music import (
        merge_artist_records,
        prefetch_album_covers,
        resolve_artist_mbid,
        sync_artist_discography,
    )
    from app.services.music_scrobble import dedupe_artist_albums
    from app.services.music_validation import validate_music_library

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning("enrich_music_library_task: user %s no longer exists", user_id)
        return {"artists": 0, "synced": 0, "deduped": 0}

    # Skip expensive validation before enrichment - run after to see improvements
    # Fast runtime backfill already runs immediately after import for statistics
    logger.info(
        "enrich_music_library_task: Starting enrichment for user %s",
        user_id,
    )

    artist_ids = (
        Music.objects.filter(user=user)
        .exclude(artist_id__isnull=True)
        .values_list("artist_id", flat=True)
        .distinct()
    )

    artists = list(Artist.objects.filter(id__in=artist_ids))
    artists_without_mbid = [a for a in artists if not a.musicbrainz_id]
    artists_with_mbid = [a for a in artists if a.musicbrainz_id]

    # Log sample names to verify we're seeing the full set (not just "A" names)
    sample_without_mbid = [a.name for a in artists_without_mbid[:10]] if artists_without_mbid else []
    sample_with_mbid = [a.name for a in artists_with_mbid[:10]] if artists_with_mbid else []

    logger.info(
        "enrich_music_library_task: Found %d total artists (%d without MBID, %d with MBID). "
        "Sample without MBID: %s. Sample with MBID: %s",
        len(artists),
        len(artists_without_mbid),
        len(artists_with_mbid),
        sample_without_mbid,
        sample_with_mbid,
    )

    synced = 0
    deduped = 0
    attached = 0
    merged = 0
    no_match = 0
    skipped_already_has_mbid = 0
    skipped_artist_names_sample = []  # Sample of skipped artist names
    total_candidates = 0
    albums_tracks_populated = 0
    albums_to_populate: list[int] = []  # Collect albums for background track population
    artists_for_covers: list[int] = []
    defer_covers = getattr(settings, "MUSIC_DEFER_COVER_PREFETCH", True)

    # Phase 1: Fast runtime backfill from existing tracks (DB-only, immediate)
    from app.models import Item
    from app.services.music_scrobble import _runtime_minutes_from_ms

    music_with_runtime = (
        Music.objects.filter(user=user, item__runtime_minutes__isnull=True)
        .exclude(track__duration_ms__isnull=True)
        .select_related("item", "track")
    )

    items_to_update_runtime = []
    for music in music_with_runtime:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                items_to_update_runtime.append(music.item)

    if items_to_update_runtime:
        Item.objects.bulk_update(items_to_update_runtime, ["runtime_minutes"], batch_size=500)
        logger.info(
            "enrich_music_library_task: Backfilled %d runtimes from existing tracks",
            len(items_to_update_runtime),
        )

    # Phase 2: API operations (MBID resolution, discography sync, track population)
    artists_processed_count = 0
    for idx, artist in enumerate(artists):
        artists_processed_count += 1
        # Log progress every 50 artists to track if we're processing the full list
        if artists_processed_count % 50 == 0 or artists_processed_count == len(artists):
            logger.info(
                "enrich_music_library_task: Progress - processed %d/%d artists (current: '%s', id=%s)",
                artists_processed_count,
                len(artists),
                artist.name if artist.name else "Unknown",
                artist.id,
            )
        # Heal blank names that slipped in during fast import
        if not (artist.name or "").strip():
            artist.name = "Unknown Artist"
            artist.save(update_fields=["name"])

        # If missing MBID, try to attach a safe one
        if artist.musicbrainz_id:
            # Artist already has MBID, skip MBID resolution
            skipped_already_has_mbid += 1
            # Collect sample names (first 20) for logging
            if len(skipped_artist_names_sample) < 20:
                skipped_artist_names_sample.append(artist.name)
        else:
            logger.info(
                "enrich_music_library_task: Processing artist '%s' (id=%s, no MBID, sort_name='%s')",
                artist.name,
                artist.id,
                artist.sort_name or "",
            )
            try:
                mbid, cand_count, variant = resolve_artist_mbid(
                    artist.name or "",
                    artist.sort_name or "",
                )
                total_candidates += cand_count
                logger.info(
                    "enrich_music_library_task: resolve_artist_mbid('%s', '%s') returned: mbid=%s, candidates=%d, variant='%s'",
                    artist.name or "",
                    artist.sort_name or "",
                    mbid or "None",
                    cand_count,
                    variant or "None",
                )
                if mbid:
                    logger.info(
                        "enrich_music_library_task: attempting to attach MBID %s to artist '%s' (id=%s) via variant '%s'",
                        mbid,
                        artist.name,
                        artist.id,
                        variant or "None",
                    )
                    try:
                        artist.musicbrainz_id = mbid
                        artist.save(update_fields=["musicbrainz_id"])
                        attached += 1
                        logger.info(
                            "enrich_music_library_task: SUCCESS - attached MBID %s to '%s' (id=%s) via '%s' (candidates=%d)",
                            mbid,
                            artist.name,
                            artist.id,
                            variant or "None",
                            cand_count,
                        )
                    except IntegrityError as integrity_err:
                        logger.info(
                            "enrich_music_library_task: IntegrityError attaching MBID %s to '%s' (id=%s) - MBID already exists, attempting merge",
                            mbid,
                            artist.name,
                            artist.id,
                        )
                        # Merge into the existing artist that already owns this MBID
                        existing = Artist.objects.filter(musicbrainz_id=mbid).first()
                        if existing:
                            logger.info(
                                "enrich_music_library_task: found existing artist '%s' (id=%s, MBID=%s) to merge into",
                                existing.name,
                                existing.id,
                                existing.musicbrainz_id,
                            )
                            try:
                                artist = merge_artist_records(artist, existing)
                                # Refresh from DB to ensure we have a valid saved instance
                                if artist.pk:
                                    artist.refresh_from_db()
                                merged += 1
                                logger.info(
                                    "enrich_music_library_task: SUCCESS - merged artist '%s' (id=%s) into '%s' (id=%s, MBID=%s) via variant '%s'",
                                    artist.name if hasattr(artist, "name") else "Unknown",
                                    artist.id if hasattr(artist, "id") else "Unknown",
                                    existing.name,
                                    existing.id,
                                    mbid,
                                    variant or "None",
                                )
                            except Exception as merge_exc:
                                logger.warning(
                                    "enrich_music_library_task: merge FAILED for '%s' (id=%s) into '%s' (id=%s, MBID=%s): %s",
                                    artist.name if hasattr(artist, "name") else "Unknown",
                                    artist.id if hasattr(artist, "id") else "Unknown",
                                    existing.name,
                                    existing.id,
                                    mbid,
                                    merge_exc,
                                    exc_info=True,
                                )
                                # After failed merge, artist might be invalid - skip remaining processing for this artist
                                if not artist.pk:
                                    logger.warning(
                                        "enrich_music_library_task: artist '%s' invalid after failed merge, skipping remaining processing for this artist, continuing with next",
                                        artist.name if hasattr(artist, "name") else "Unknown",
                                    )
                                    continue
                        else:
                            logger.warning(
                                "enrich_music_library_task: MBID attach failed for '%s' (id=%s) - MBID %s conflicts but no target artist found (variant '%s', error: %s)",
                                artist.name,
                                artist.id,
                                mbid,
                                variant or "None",
                                integrity_err,
                            )
                else:
                    no_match += 1
                    logger.info(
                        "enrich_music_library_task: NO MATCH - resolve_artist_mbid returned None for '%s' (id=%s, candidates=%d, variant='%s')",
                        artist.name,
                        artist.id,
                        cand_count,
                        variant or "None",
                    )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "enrich_music_library_task: EXCEPTION - MBID resolution failed for '%s' (id=%s): %s",
                    artist.name,
                    artist.id,
                    exc,
                    exc_info=True,
                )

        # Skip remaining processing if artist became invalid (e.g., deleted during merge)
        if not artist.pk:
            logger.debug(
                "enrich_music_library_task: skipping remaining processing for artist '%s' (no pk after MBID resolution)",
                artist.name if hasattr(artist, "name") else "Unknown",
            )
            continue

        if artist.musicbrainz_id:
            try:
                sync_artist_discography(artist, force=False)
                synced += 1
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Discography sync failed for %s: %s", artist.name, exc)

        try:
            dedupe_artist_albums(artist)
            deduped += 1
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Album dedupe failed for %s: %s", artist.name, exc)

        # Collect albums that need track population (defer to background for speed)
        # Only collect albums with MBIDs - can't populate tracks without them
        # Ensure artist is saved (has PK) and still exists before filtering
        # After failed merges, the artist might be deleted or invalid
        if artist.pk:
            # Verify artist still exists in DB (might have been deleted during failed merge)
            try:
                Artist.objects.get(pk=artist.pk)
            except Artist.DoesNotExist:
                logger.debug("Artist %s (pk=%s) no longer exists, skipping album collection", artist.name, artist.pk)
            else:
                for album in Album.objects.filter(
                    artist_id=artist.pk,
                    tracks_populated=False,
                ).exclude(
                    musicbrainz_release_id__isnull=True,
                    musicbrainz_release_group_id__isnull=True,
                ):
                    albums_to_populate.append(album.id)

        # Link Music entries to populated tracks by recording_id to unlock runtimes
        try:
            from app.models import Track as TrackModel

            # Ensure artist is saved before filtering
            if artist.pk:
                music_without_track = Music.objects.filter(
                    artist_id=artist.pk,
                    track__isnull=True,
                    item__media_id__isnull=False,
                    album__isnull=False,
                )
            else:
                music_without_track = Music.objects.none()

            if music_without_track.exists() and artist.pk:
                track_map = {
                    t.musicbrainz_recording_id: t.id
                    for t in TrackModel.objects.filter(
                        album__artist_id=artist.pk,
                        musicbrainz_recording_id__isnull=False,
                    )
                }
                to_update = []
                for music in music_without_track:
                    track_id = track_map.get(music.item.media_id)
                    if track_id:
                        music.track_id = track_id
                        to_update.append(music)
                if to_update:
                    Music.objects.bulk_update(to_update, ["track"])
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Music->Track relink failed for artist %s: %s", artist.id, exc)

        # Either queue cover prefetch for later or do it inline (configurable)
        if defer_covers and artist.musicbrainz_id:
            artists_for_covers.append(artist.id)
        elif artist.musicbrainz_id:
            try:
                prefetch_album_covers(artist, limit=None)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Cover prefetch failed for artist %s: %s", artist.id, exc)

    # Phase 3: Final runtime backfill from newly populated/linked tracks (if any)
    # This catches tracks that got duration_ms during enrichment
    music_with_new_runtime = (
        Music.objects.filter(user=user, item__runtime_minutes__isnull=True)
        .exclude(track__duration_ms__isnull=True)
        .select_related("item", "track")
    )

    items_final_runtime = []
    for music in music_with_new_runtime:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                items_final_runtime.append(music.item)

    if items_final_runtime:
        Item.objects.bulk_update(items_final_runtime, ["runtime_minutes"], batch_size=500)
        logger.info(
            "enrich_music_library_task: Backfilled %d additional runtimes from newly linked tracks",
            len(items_final_runtime),
        )

    cover_task_id = None
    if defer_covers and artists_for_covers:
        result = prefetch_album_covers_batch.delay(artists_for_covers, limit_per_artist=5)
        cover_task_id = result.id

    # Queue track population as background task (only for albums with MBIDs)
    # Pass user_id so we can link tracks and backfill runtime after population
    track_population_task_id = None
    if albums_to_populate:
        result = populate_album_tracks_batch.delay(albums_to_populate, user_id=user.id)
        track_population_task_id = result.id
        logger.info(
            "enrich_music_library_task: Queued track population for %d albums in background",
            len(albums_to_populate),
        )

    # Run validation after enrichment (optional - can be disabled for speed)
    run_validation = getattr(settings, "MUSIC_ENRICHMENT_VALIDATION", False)
    validation_result = None

    if run_validation:
        validation_after = validate_music_library(user)
        validation_result = {
            "after": validation_after,
        }
        logger.info(
            "enrich_music_library_task: Completed enrichment for user %s. "
            "Summary: %d total artists (%d skipped - already had MBID, %d processed without MBID). "
            "Results: attached %d MBIDs, merged %d, no match %d, synced %d discographies. "
            "Validation: %d tracks, %d artists (%d with MBID), %d albums (%d with tracks). "
            "Sample skipped artists: %s",
            user_id,
            len(artists),
            skipped_already_has_mbid,
            len(artists_without_mbid),
            attached,
            merged,
            no_match,
            synced,
            validation_after["unique_tracks"],
            validation_after["artists"]["total"],
            validation_after["artists"]["with_mbid"],
            validation_after["albums"]["total"],
            validation_after["albums"]["with_tracks_populated"],
            skipped_artist_names_sample[:10] if skipped_artist_names_sample else [],
        )
    else:
        logger.info(
            "enrich_music_library_task: Completed enrichment for user %s. "
            "Summary: %d total artists (%d skipped - already had MBID, %d processed without MBID). "
            "Results: attached %d MBIDs, merged %d, no match %d, synced %d discographies. "
            "Sample skipped artists: %s",
            user_id,
            len(artists),
            skipped_already_has_mbid,
            len(artists_without_mbid),
            attached,
            merged,
            no_match,
            synced,
            skipped_artist_names_sample[:10] if skipped_artist_names_sample else [],
        )

    return {
        "artists": len(artists),
        "synced": synced,
        "deduped": deduped,
        "attached_mbid": attached,
        "merged_artists": merged,
        "no_mbid_match": no_match,
        "skipped_already_has_mbid": skipped_already_has_mbid,
        "candidate_sum": total_candidates,
        "albums_tracks_populated": albums_tracks_populated,
        "albums_queued_for_tracks": len(albums_to_populate),
        "cover_task_id": cover_task_id,
        "track_population_task_id": track_population_task_id,
        "validation": validation_result,
    }


@shared_task
def fast_runtime_backfill_task(user_id: int):
    """Fast runtime backfill from existing Track durations - runs immediately after import.
    
    This is the critical path for statistics to work. Backfills runtime from:
    1. Track.duration_ms (if tracks already have duration from Plex)
    2. Direct lookup from album tracklists (if tracks are populated but not linked)
    
    This runs BEFORE enrichment to get statistics working immediately.
    """
    from app.models import Item, Music, Track
    from app.services.music_scrobble import _runtime_minutes_from_ms

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning("fast_runtime_backfill_task: user %s no longer exists", user_id)
        return {"backfilled": 0}

    # Strategy 1: Backfill from linked Track.duration_ms (fastest path)
    music_with_track_duration = (
        Music.objects.filter(
            user=user,
            item__runtime_minutes__isnull=True,
            track__duration_ms__isnull=False,
        )
        .select_related("item", "track")
    )

    items_to_update = []
    for music in music_with_track_duration:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                items_to_update.append(music.item)

    # Bulk update items
    if items_to_update:
        Item.objects.bulk_update(items_to_update, ["runtime_minutes"], batch_size=500)
        logger.info(
            "fast_runtime_backfill_task: Backfilled %d runtimes from linked Track records",
            len(items_to_update),
        )

    # Strategy 2: Backfill from album tracklists (for tracks not yet linked)
    # Find Music entries without runtime that have albums with populated tracks
    music_with_album_tracks = (
        Music.objects.filter(
            user=user,
            item__runtime_minutes__isnull=True,
            album__tracks_populated=True,
            item__media_id__isnull=False,
        )
        .exclude(track__duration_ms__isnull=False)  # Skip if already linked
        .select_related("item", "album")
    )

    additional_items = []
    for music in music_with_album_tracks:
        if not music.item or not music.item.media_id or not music.album:
            continue

        # Try to find track in album's tracklist by recording ID
        track = Track.objects.filter(
            album=music.album,
            musicbrainz_recording_id=music.item.media_id,
            duration_ms__isnull=False,
        ).first()

        if track and track.duration_ms:
            runtime = _runtime_minutes_from_ms(track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                additional_items.append(music.item)

    # Bulk update additional items
    if additional_items:
        Item.objects.bulk_update(additional_items, ["runtime_minutes"], batch_size=500)
        logger.info(
            "fast_runtime_backfill_task: Backfilled %d runtimes from album tracklists",
            len(additional_items),
        )

    total_backfilled = len(items_to_update) + len(additional_items)
    return {"backfilled": total_backfilled}


@shared_task
def populate_album_tracks_batch(album_ids: list[int], user_id: int | None = None):
    """Populate tracks for a batch of albums in the background.
    
    This defers the slow API operations (1 req/sec per album) to background
    so enrichment task completes faster.
    
    After populating tracks, automatically links Music entries to tracks and
    backfills runtime data.
    
    Args:
        album_ids: List of album IDs to populate tracks for
        user_id: Optional user ID - if provided, links tracks and backfills runtime after population
    """
    from app.models import Album
    from app.services.music import (
        backfill_music_runtimes,
        link_music_to_tracks,
        populate_album_tracks,
    )

    populated = 0
    skipped_no_release_id = 0
    skipped_already_populated = 0

    for album_id in album_ids:
        try:
            album = Album.objects.filter(id=album_id).first()
            if not album:
                continue

            if album.tracks_populated:
                skipped_already_populated += 1
                continue

            # Skip albums without MBIDs - can't populate tracks without them
            if not album.musicbrainz_release_id and not album.musicbrainz_release_group_id:
                continue

            count = populate_album_tracks(album)
            if count > 0:
                populated += 1
            elif album.musicbrainz_release_group_id and not album.musicbrainz_release_id:
                skipped_no_release_id += 1
        except Exception as exc:
            logger.warning("Track populate failed for album %s: %s", album_id, exc)

    if skipped_no_release_id > 0:
        logger.info(
            "populate_album_tracks_batch: Skipped %d albums that couldn't get release_id from release_group",
            skipped_no_release_id,
        )

    logger.info(
        "populate_album_tracks_batch: Populated tracks for %d albums (skipped: %d no release_id, %d already populated)",
        populated,
        skipped_no_release_id,
        skipped_already_populated,
    )

    # After populating tracks, link Music entries to tracks and backfill runtime
    if populated > 0 and user_id:
        try:
            User = get_user_model()
            user = User.objects.get(id=user_id)

            # Link Music entries to newly populated tracks
            link_result = link_music_to_tracks(user)

            # Backfill runtime from all available sources
            backfill_result = backfill_music_runtimes(user)

            logger.info(
                "populate_album_tracks_batch: After populating %d albums, linked %d Music->Track, backfilled %d runtimes",
                populated,
                link_result.get("linked", 0),
                backfill_result.get("backfilled", 0),
            )
        except User.DoesNotExist:
            logger.warning("populate_album_tracks_batch: User %s not found, skipping track linking", user_id)
        except Exception as exc:
            logger.warning("Failed to link tracks/backfill runtime after track population: %s", exc)

    return {
        "albums": len(album_ids),
        "populated": populated,
        "skipped_no_release_id": skipped_no_release_id,
        "skipped_already_populated": skipped_already_populated,
    }


@shared_task
def enrich_albums_task(user_id: int):
    """Post-import enrichment for albums - resolve MBIDs and populate tracks.
    
    This task processes albums that don't have MusicBrainz IDs, similar to how
    enrich_music_library_task processes artists. Uses the same proven search/matching
    logic from resolve_artist_mbid adapted for albums.
    """
    from app.models import Album, AlbumTracker, Item, Music
    from app.services.music import (
        backfill_music_runtimes,
        link_music_to_tracks,
        populate_album_tracks,
        resolve_album_mbid,
    )
    from app.services.music_scrobble import _runtime_minutes_from_ms

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning("enrich_albums_task: user %s no longer exists", user_id)
        return {"albums": 0, "attached_mbid": 0, "merged": 0}

    logger.info(
        "enrich_albums_task: Starting album enrichment for user %s",
        user_id,
    )

    # Get all albums for this user that need MBIDs
    # Albums are linked to users through Music entries
    album_ids = (
        Music.objects.filter(user=user)
        .exclude(album_id__isnull=True)
        .values_list("album_id", flat=True)
        .distinct()
    )

    albums = list(Album.objects.filter(id__in=album_ids))
    albums_without_mbid = [
        a
        for a in albums
        if not a.musicbrainz_release_id and not a.musicbrainz_release_group_id
    ]
    albums_with_mbid = [
        a
        for a in albums
        if a.musicbrainz_release_id or a.musicbrainz_release_group_id
    ]

    # Log sample names to verify we're seeing the full set
    sample_without_mbid = (
        [f"{a.title} - {a.artist.name if a.artist else 'Unknown'}" for a in albums_without_mbid[:10]]
        if albums_without_mbid
        else []
    )
    sample_with_mbid = (
        [f"{a.title} - {a.artist.name if a.artist else 'Unknown'}" for a in albums_with_mbid[:10]]
        if albums_with_mbid
        else []
    )

    logger.info(
        "enrich_albums_task: Found %d total albums (%d without MBID, %d with MBID). "
        "Sample without MBID: %s. Sample with MBID: %s",
        len(albums),
        len(albums_without_mbid),
        len(albums_with_mbid),
        sample_without_mbid,
        sample_with_mbid,
    )

    attached = 0
    merged = 0
    no_match = 0
    skipped_already_has_mbid = 0
    skipped_album_names_sample = []
    total_candidates = 0
    albums_to_populate: list[int] = []

    # Phase 1: Fast runtime backfill from existing tracks (DB-only, immediate)
    music_with_runtime = (
        Music.objects.filter(user=user, item__runtime_minutes__isnull=True)
        .exclude(track__duration_ms__isnull=True)
        .select_related("item", "track")
    )

    items_to_update_runtime = []
    for music in music_with_runtime:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                items_to_update_runtime.append(music.item)

    if items_to_update_runtime:
        Item.objects.bulk_update(items_to_update_runtime, ["runtime_minutes"], batch_size=500)
        logger.info(
            "enrich_albums_task: Backfilled %d runtimes from existing tracks",
            len(items_to_update_runtime),
        )

    # Phase 2: MBID resolution for albums
    albums_processed_count = 0
    for album in albums:
        albums_processed_count += 1
        # Log progress every 50 albums
        if albums_processed_count % 50 == 0 or albums_processed_count == len(albums):
            logger.info(
                "enrich_albums_task: Progress - processed %d/%d albums (current: '%s', id=%s)",
                albums_processed_count,
                len(albums),
                album.title if album.title else "Unknown",
                album.id,
            )

        # If missing MBID, try to attach one
        if album.musicbrainz_release_id or album.musicbrainz_release_group_id:
            skipped_already_has_mbid += 1
            if len(skipped_album_names_sample) < 20:
                skipped_album_names_sample.append(
                    f"{album.title} - {album.artist.name if album.artist else 'Unknown'}",
                )
        else:
            artist_name = album.artist.name if album.artist else None
            logger.info(
                "enrich_albums_task: Processing album '%s' (id=%s, artist='%s', no MBID)",
                album.title,
                album.id,
                artist_name or "Unknown",
            )
            try:
                release_group_id, release_id, cand_count, variant = resolve_album_mbid(
                    album.title or "",
                    artist_name,
                )
                total_candidates += cand_count
                logger.info(
                    "enrich_albums_task: resolve_album_mbid('%s', '%s') returned: release_group_id=%s, release_id=%s, candidates=%d, variant='%s'",
                    album.title or "",
                    artist_name or "None",
                    release_group_id or "None",
                    release_id or "None",
                    cand_count,
                    variant or "None",
                )
                if release_group_id or release_id:
                    logger.info(
                        "enrich_albums_task: attempting to attach MBIDs to album '%s' (id=%s) via variant '%s'",
                        album.title,
                        album.id,
                        variant or "None",
                    )
                    try:
                        # Update album with MBIDs
                        update_fields = []
                        if release_group_id and not album.musicbrainz_release_group_id:
                            album.musicbrainz_release_group_id = release_group_id
                            update_fields.append("musicbrainz_release_group_id")
                        if release_id and not album.musicbrainz_release_id:
                            album.musicbrainz_release_id = release_id
                            update_fields.append("musicbrainz_release_id")

                        if update_fields:
                            album.save(update_fields=update_fields)
                            attached += 1
                            logger.info(
                                "enrich_albums_task: SUCCESS - attached MBIDs to '%s' (id=%s) via '%s' (candidates=%d)",
                                album.title,
                                album.id,
                                variant or "None",
                                cand_count,
                            )
                    except IntegrityError as integrity_err:
                        logger.info(
                            "enrich_albums_task: IntegrityError attaching MBIDs to '%s' (id=%s) - MBID already exists, attempting merge",
                            album.title,
                            album.id,
                        )
                        # Find existing album with this release_group_id
                        existing = None
                        if release_group_id:
                            existing = Album.objects.filter(
                                musicbrainz_release_group_id=release_group_id,
                            ).exclude(id=album.id).first()
                        if not existing and release_id:
                            existing = Album.objects.filter(
                                musicbrainz_release_id=release_id,
                            ).exclude(id=album.id).first()

                        if existing:
                            logger.info(
                                "enrich_albums_task: found existing album '%s' (id=%s, release_group_id=%s) to merge into",
                                existing.title,
                                existing.id,
                                existing.musicbrainz_release_group_id or "None",
                            )
                            try:
                                # Merge album into existing (similar to _merge_album_into_target logic)
                                updates = set()
                                if (
                                    (not existing.image or existing.image == settings.IMG_NONE)
                                    and album.image
                                    and album.image != settings.IMG_NONE
                                ):
                                    existing.image = album.image
                                    updates.add("image")
                                if not existing.musicbrainz_release_id and album.musicbrainz_release_id:
                                    existing.musicbrainz_release_id = album.musicbrainz_release_id
                                    updates.add("musicbrainz_release_id")
                                if not existing.musicbrainz_release_group_id and album.musicbrainz_release_group_id:
                                    existing.musicbrainz_release_group_id = album.musicbrainz_release_group_id
                                    updates.add("musicbrainz_release_group_id")
                                if not existing.release_date and album.release_date:
                                    existing.release_date = album.release_date
                                    updates.add("release_date")
                                if not existing.release_type and album.release_type:
                                    existing.release_type = album.release_type
                                    updates.add("release_type")
                                if updates:
                                    existing.save(update_fields=list(updates))

                                # Merge album trackers
                                for tracker in AlbumTracker.objects.filter(album=album):
                                    existing_tracker = AlbumTracker.objects.filter(
                                        user=tracker.user,
                                        album=existing,
                                    ).first()
                                    if existing_tracker:
                                        if (
                                            tracker.start_date
                                            and (
                                                not existing_tracker.start_date
                                                or tracker.start_date < existing_tracker.start_date
                                            )
                                        ):
                                            existing_tracker.start_date = tracker.start_date
                                            existing_tracker.save(update_fields=["start_date"])
                                        tracker.delete()
                                    else:
                                        tracker.album = existing
                                        tracker.save(update_fields=["album"])

                                # Re-point music entries to the canonical album
                                Music.objects.filter(album=album).update(album=existing, track=None)

                                # Delete the source album
                                album.delete()
                                album = existing  # Use existing for further processing
                                merged += 1
                                logger.info(
                                    "enrich_albums_task: SUCCESS - merged album '%s' (id=%s) into '%s' (id=%s, release_group_id=%s) via variant '%s'",
                                    album.title if hasattr(album, "title") else "Unknown",
                                    album.id if hasattr(album, "id") else "Unknown",
                                    existing.title,
                                    existing.id,
                                    existing.musicbrainz_release_group_id or "None",
                                    variant or "None",
                                )
                            except Exception as merge_exc:
                                logger.warning(
                                    "enrich_albums_task: merge FAILED for '%s' (id=%s) into '%s' (id=%s): %s",
                                    album.title if hasattr(album, "title") else "Unknown",
                                    album.id if hasattr(album, "id") else "Unknown",
                                    existing.title,
                                    existing.id,
                                    merge_exc,
                                    exc_info=True,
                                )
                                # After failed merge, album might be invalid - skip remaining processing
                                if not album.pk:
                                    logger.warning(
                                        "enrich_albums_task: album '%s' invalid after failed merge, skipping remaining processing",
                                        album.title if hasattr(album, "title") else "Unknown",
                                    )
                                    continue
                        else:
                            logger.warning(
                                "enrich_albums_task: MBID attach failed for '%s' (id=%s) - MBID conflicts but no target album found (variant '%s', error: %s)",
                                album.title,
                                album.id,
                                variant or "None",
                                integrity_err,
                            )
                else:
                    no_match += 1
                    logger.info(
                        "enrich_albums_task: NO MATCH - resolve_album_mbid returned None for '%s' (id=%s, candidates=%d, variant='%s')",
                        album.title,
                        album.id,
                        cand_count,
                        variant or "None",
                    )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "enrich_albums_task: EXCEPTION - MBID resolution failed for '%s' (id=%s): %s",
                    album.title,
                    album.id,
                    exc,
                    exc_info=True,
                )

        # Skip remaining processing if album became invalid (e.g., deleted during merge)
        if not album.pk:
            logger.debug(
                "enrich_albums_task: skipping remaining processing for album '%s' (no pk after MBID resolution)",
                album.title if hasattr(album, "title") else "Unknown",
            )
            continue

        # Collect albums that need track population (only albums with MBIDs)
        if album.pk and (album.musicbrainz_release_id or album.musicbrainz_release_group_id):
            if not album.tracks_populated:
                albums_to_populate.append(album.id)

    # Phase 3: Populate tracks for albums that now have MBIDs
    populated_count = 0
    for album_id in albums_to_populate:
        try:
            album = Album.objects.filter(id=album_id).first()
            if not album:
                continue
            if album.tracks_populated:
                continue
            # Skip albums without MBIDs - can't populate tracks without them
            if not album.musicbrainz_release_id and not album.musicbrainz_release_group_id:
                continue

            count = populate_album_tracks(album)
            if count > 0:
                populated_count += 1
        except Exception as exc:
            logger.warning("Track populate failed for album %s: %s", album_id, exc)

    logger.info(
        "enrich_albums_task: Populated tracks for %d albums",
        populated_count,
    )

    # Phase 4: Link Music entries to tracks and backfill runtime
    if populated_count > 0:
        try:
            # Link Music entries to newly populated tracks
            link_result = link_music_to_tracks(user)

            # Backfill runtime from all available sources
            backfill_result = backfill_music_runtimes(user)

            logger.info(
                "enrich_albums_task: After populating tracks, linked %d Music->Track, backfilled %d runtimes",
                link_result.get("linked", 0),
                backfill_result.get("backfilled", 0),
            )
        except Exception as exc:
            logger.warning("Failed to link tracks/backfill runtime after track population: %s", exc)

    # Phase 5: Final runtime backfill from newly populated/linked tracks
    music_with_new_runtime = (
        Music.objects.filter(user=user, item__runtime_minutes__isnull=True)
        .exclude(track__duration_ms__isnull=True)
        .select_related("item", "track")
    )

    items_final_runtime = []
    for music in music_with_new_runtime:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                items_final_runtime.append(music.item)

    if items_final_runtime:
        Item.objects.bulk_update(items_final_runtime, ["runtime_minutes"], batch_size=500)
        logger.info(
            "enrich_albums_task: Backfilled %d additional runtimes from newly linked tracks",
            len(items_final_runtime),
        )

    logger.info(
        "enrich_albums_task: Completed enrichment for user %s. "
        "Summary: %d total albums (%d skipped - already had MBID, %d processed without MBID). "
        "Results: attached %d MBIDs, merged %d, no match %d, populated tracks for %d albums. "
        "Sample skipped albums: %s",
        user_id,
        len(albums),
        skipped_already_has_mbid,
        len(albums_without_mbid),
        attached,
        merged,
        no_match,
        populated_count,
        skipped_album_names_sample[:10] if skipped_album_names_sample else [],
    )

    return {
        "albums": len(albums),
        "attached_mbid": attached,
        "merged_albums": merged,
        "no_mbid_match": no_match,
        "skipped_already_has_mbid": skipped_already_has_mbid,
        "candidate_sum": total_candidates,
        "albums_tracks_populated": populated_count,
    }


@shared_task
def prefetch_album_covers_batch(artist_ids: list[int], limit_per_artist: int | None = 10):
    """Prefetch album covers for a batch of artists (run after enrichment)."""
    from app.models import Artist
    from app.services.music import prefetch_album_covers

    updated = 0
    for artist_id in artist_ids:
        artist = Artist.objects.filter(id=artist_id, musicbrainz_id__isnull=False).first()
        if not artist:
            continue
        try:
            updated += prefetch_album_covers(artist, limit=limit_per_artist)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Cover batch prefetch failed for artist %s: %s", artist_id, exc)
    return {"artists": len(artist_ids), "covers_updated": updated}


@shared_task
def populate_episode_runtime_data(season_keys: list[str] | None = None):
    """Populate runtime data for episodes by syncing season metadata."""
    import time

    from app.models import Item, MediaTypes
    from app.providers import services
    from app.statistics import parse_runtime_to_minutes

    normalized_seasons = _normalize_season_keys(season_keys)

    episodes_needing_runtime = _episode_runtime_items_queryset()

    if normalized_seasons:
        season_filters = Q()
        for media_id, source, season_number in normalized_seasons:
            season_filters |= Q(
                media_id=media_id,
                source=source,
                season_number=season_number,
            )
        episodes_needing_runtime = episodes_needing_runtime.filter(season_filters)

    if not episodes_needing_runtime.exists():
        logger.info("No episodes need runtime data")
        return {"updated": 0, "errors": 0, "message": "No episodes need runtime data"}

    updated_count = 0
    error_count = 0
    processed_seasons = set()
    updated_items = []

    seasons_to_process = set(normalized_seasons)
    if not seasons_to_process:
        seasons_to_process = set(
            episodes_needing_runtime.values_list(
                "media_id",
                "source",
                "season_number",
            ),
        )

    for media_id, source, season_number in seasons_to_process:
        try:
            if not media_id or season_number is None:
                continue
            season_key = (media_id, source, season_number)
            if season_key in processed_seasons:
                continue
            processed_seasons.add(season_key)

            eligible_missing = list(
                episodes_needing_runtime.filter(
                    media_id=media_id,
                    source=source,
                    season_number=season_number,
                ),
            )
            missing_by_number = {
                ep.episode_number: ep
                for ep in eligible_missing
                if ep.episode_number is not None
            }

            existing_episodes = list(
                Item.objects.filter(
                    media_id=media_id,
                    source=source,
                    media_type=MediaTypes.EPISODE.value,
                    season_number=season_number,
                )
            )
            existing_by_number = {
                ep.episode_number: ep
                for ep in existing_episodes
                if ep.episode_number is not None
            }
            episode_title_map = {
                ep.episode_number: (ep.title, ep.image)
                for ep in existing_episodes
                if ep.episode_number is not None
            }

            season_metadata = services.get_media_metadata(
                "tv_with_seasons",
                media_id,
                source,
                [season_number],
            )

            if not season_metadata or f"season/{season_number}" not in season_metadata:
                logger.warning("No season metadata for %s S%s", media_id, season_number)
                error_count += 1
                for episode_item in eligible_missing:
                    _record_backfill_failure(
                        episode_item,
                        MetadataBackfillField.RUNTIME,
                        "no season metadata",
                    )
                continue

            season_data = season_metadata[f"season/{season_number}"]

            from app.providers import tmdb

            episodes_metadata = tmdb.process_episodes(season_data, [])
            if not episodes_metadata:
                error_count += 1
                for episode_item in eligible_missing:
                    _record_backfill_failure(
                        episode_item,
                        MetadataBackfillField.RUNTIME,
                        "no episode metadata",
                    )
                continue

            for ep_data in episodes_metadata:
                episode_number = ep_data.get("episode_number")
                if episode_number is None:
                    continue
                runtime_value = ep_data.get("runtime")
                if not runtime_value:
                    missing_item = missing_by_number.pop(episode_number, None)
                    if missing_item:
                        _record_backfill_failure(
                            missing_item,
                            MetadataBackfillField.RUNTIME,
                            "no runtime",
                        )
                    continue

                runtime_minutes = parse_runtime_to_minutes(runtime_value)
                if runtime_minutes is None:
                    missing_item = missing_by_number.pop(episode_number, None)
                    if missing_item:
                        _record_backfill_failure(
                            missing_item,
                            MetadataBackfillField.RUNTIME,
                            "parse failure",
                        )
                    continue

                existing_item = existing_by_number.get(episode_number)
                existing_title, existing_image = episode_title_map.get(episode_number, ("", ""))
                title = existing_title or ep_data.get("title") or f"Episode {episode_number}"
                image = ep_data.get("image") or existing_image or settings.IMG_NONE

                if existing_item:
                    update_fields = {}
                    runtime_changed = False
                    if existing_item.runtime_minutes != runtime_minutes:
                        update_fields["runtime_minutes"] = runtime_minutes
                        runtime_changed = True
                    if not existing_item.title and title:
                        update_fields["title"] = title
                    if not existing_item.image and image:
                        update_fields["image"] = image
                    if update_fields:
                        for field_name, value in update_fields.items():
                            setattr(existing_item, field_name, value)
                        existing_item.save(update_fields=list(update_fields.keys()))
                        if runtime_changed:
                            updated_count += 1
                            updated_items.append(existing_item)
                            _record_backfill_success(existing_item, MetadataBackfillField.RUNTIME)
                            logger.info(
                                "Updated runtime for %s S%sE%s: %s minutes",
                                existing_item.title,
                                season_number,
                                episode_number,
                                runtime_minutes,
                            )
                else:
                    try:
                        episode_item = Item.objects.create(
                            media_id=media_id,
                            source=source,
                            media_type=MediaTypes.EPISODE.value,
                            season_number=season_number,
                            episode_number=episode_number,
                            title=title,
                            image=image,
                            runtime_minutes=runtime_minutes,
                        )
                        updated_count += 1
                        updated_items.append(episode_item)
                        _record_backfill_success(episode_item, MetadataBackfillField.RUNTIME)
                        logger.info(
                            "Updated runtime for %s S%sE%s: %s minutes",
                            episode_item.title,
                            season_number,
                            episode_number,
                            runtime_minutes,
                        )
                    except IntegrityError:
                        existing_item = Item.objects.filter(
                            media_id=media_id,
                            source=source,
                            media_type=MediaTypes.EPISODE.value,
                            season_number=season_number,
                            episode_number=episode_number,
                        ).first()
                        if existing_item and existing_item.runtime_minutes != runtime_minutes:
                            existing_item.runtime_minutes = runtime_minutes
                            existing_item.save(update_fields=["runtime_minutes"])
                            updated_count += 1
                            updated_items.append(existing_item)
                            _record_backfill_success(existing_item, MetadataBackfillField.RUNTIME)
                            logger.info(
                                "Updated runtime for %s S%sE%s: %s minutes",
                                existing_item.title,
                                season_number,
                                episode_number,
                                runtime_minutes,
                            )

                missing_by_number.pop(episode_number, None)

            if missing_by_number:
                for episode_item in missing_by_number.values():
                    _record_backfill_failure(
                        episode_item,
                        MetadataBackfillField.RUNTIME,
                        "missing episode metadata",
                    )

            time.sleep(0.1)

        except Exception as e:
            logger.error("Error processing episode season %s %s S%s: %s", media_id, source, season_number, e)
            error_count += 1
            continue

    logger.info(f"Episode runtime population completed: {updated_count} episodes updated, {error_count} errors")

    if updated_items:
        _schedule_metadata_statistics_refresh(
            updated_items,
            MetadataBackfillField.RUNTIME,
            "episode_runtime_backfill",
        )

    if not normalized_seasons:
        cache.set("runtime_population_completed", True, timeout=3600)
        logger.info("🎉 All runtime data population completed! Movies, TV shows, anime, and episodes all processed.")

    return {
        "updated": updated_count,
        "errors": error_count,
        "message": f"Processed {len(processed_seasons)} seasons, updated {updated_count} episodes.",
    }
