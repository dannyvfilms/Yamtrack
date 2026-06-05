"""Runtime backfill: queryset builders, enqueue helpers, and populate tasks.

Extracted from tasks.py. Re-exported from app.tasks for backward compatibility.
"""

import logging

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from app.log_safety import exception_summary
from app.models import Item, MediaTypes, MetadataBackfillField, MetadataBackfillState
from app.providers import services
from app.tasks_backfill_state import (
    _apply_backfill_state_filters,
    _filter_backfill_item_ids,
    _normalize_item_ids,
    _record_backfill_failure,
    _record_backfill_success,
    _schedule_metadata_statistics_refresh,
)

logger = logging.getLogger(__name__)

BACKGROUND_TASK_PRIORITY = getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1)

RUNTIME_BACKFILL_SOURCES = ("tmdb", "mal", "simkl")
RUNTIME_BACKFILL_QUEUE_TTL = 60 * 60  # 1 hour
RUNTIME_BACKFILL_ITEMS_QUEUE_KEY = "runtime_backfill_items_queue"
RUNTIME_BACKFILL_ITEMS_SCHEDULED_KEY = "runtime_backfill_items_scheduled"
RUNTIME_BACKFILL_EPISODES_QUEUE_KEY = "runtime_backfill_episode_queue"
RUNTIME_BACKFILL_EPISODES_SCHEDULED_KEY = "runtime_backfill_episode_scheduled"
RUNTIME_BACKFILL_EPISODES_LOCK_PREFIX = "runtime_backfill_episode_lock:"
RUNTIME_BACKFILL_EPISODES_LOCK_TTL = 60 * 5  # 5 minutes


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
    from datetime import timedelta  # noqa: PLC0415

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
            # Deferred to avoid circular import: tasks_episode.py re-exports from app.tasks.
            from app.tasks_episode import (
                populate_episode_runtime_queue,  # noqa: PLC0415
            )
            populate_episode_runtime_queue.apply_async(countdown=countdown)
    except Exception as exc:  # pragma: no cover - cache unavailable
        logger.debug("Episode runtime backfill queue unavailable: %s", exception_summary(exc))
        from app.tasks_episode import populate_episode_runtime_data  # noqa: PLC0415
        populate_episode_runtime_data.apply_async(kwargs={"season_keys": normalized}, countdown=countdown)
        return len(normalized)
    return len(tokens)


def _populate_runtime_for_items(items, delay_seconds):
    from app.statistics import parse_runtime_to_minutes  # noqa: PLC0415

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
                import time  # noqa: PLC0415
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


@shared_task(name="app.tasks.populate_runtime_data_batch")
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
        logger.info("Found %s remaining items. Scheduling next batch...", remaining_items)
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
    cache.set("runtime_population_completed", True, timeout=3600)  # 1 hour

    return {
        "updated": updated_count,
        "errors": error_count,
        "remaining_items": 0,
        "next_batch_scheduled": False,
        "completion_message": "All runtime data populated successfully!",
    }


@shared_task(name="app.tasks.populate_runtime_data_for_items")
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


@shared_task(name="app.tasks.populate_runtime_backfill_queue")
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


@shared_task(name="app.tasks.populate_runtime_data_continuous")
def populate_runtime_data_continuous():
    """Populate runtime data for ALL items that don't have it (startup task)."""
    # Deferred to avoid circular import: tasks_episode.py re-exports from app.tasks.
    from app.tasks_episode import populate_episode_runtime_data  # noqa: PLC0415

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
            logger.info("Runtime population completed for movies/TV/anime, but %s episodes still need runtime data. Starting episode population...", episodes_needing_runtime)
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
            logger.info("No movies/TV/anime need runtime data, but %s episodes still need runtime data. Starting episode population...", episodes_needing_runtime)
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

    logger.info("Found %s items that need runtime data. Starting comprehensive population...", total_items)

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
