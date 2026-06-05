"""Credits backfill: queryset helpers, enqueue, and populate tasks.

Extracted from tasks.py. Re-exported from app.tasks for backward compatibility.
"""

import logging

from celery import shared_task
from django.core.cache import cache

from app import credits as credit_helpers
from app.log_safety import exception_summary
from app.models import CREDITS_BACKFILL_VERSION, Item, MediaTypes, MetadataBackfillField
from app.tasks_backfill_state import (
    _filter_backfill_item_ids,
    _normalize_item_ids,
    _record_backfill_failure,
    _record_backfill_success,
    _schedule_metadata_statistics_refresh,
)
from app.tasks_metadata_cache import _fetch_item_metadata

logger = logging.getLogger(__name__)

CREDITS_BACKFILL_SOURCES = ("tmdb",)
CREDITS_BACKFILL_QUEUE_TTL = 60 * 60  # 1 hour
CREDITS_BACKFILL_ITEMS_QUEUE_KEY = "credits_backfill_items_queue"
CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY = "credits_backfill_items_scheduled"


def _missing_credits_item_ids(item_ids):
    return credit_helpers.missing_credits_backfill_item_ids(item_ids)


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


def _populate_credits_for_items(items, delay_seconds):
    from app import credits  # noqa: PLC0415

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
                import time  # noqa: PLC0415
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


@shared_task(name="app.tasks.populate_credits_data_for_items")
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


@shared_task(name="app.tasks.populate_credits_backfill_queue")
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
