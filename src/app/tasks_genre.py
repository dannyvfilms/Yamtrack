"""Genre backfill: queryset builders, TVDB anime detection, enqueue, and reconcile tasks.

Extracted from tasks.py. Re-exported from app.tasks for backward compatibility.
"""

import logging

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from app import metadata_utils
from app.interactive_requests import interactive_request_active
from app.log_safety import exception_summary
from app.models import Item, MediaTypes, MetadataBackfillField, Sources
from app.providers import services
from app.task_cooperation import CooperativeRun
from app.tasks_backfill_state import (
    GENRE_BACKFILL_VERSION,
    _apply_backfill_state_filters,
    _filter_backfill_item_ids,
    _normalize_item_ids,
    _record_backfill_failure,
    _record_backfill_success,
    _schedule_metadata_statistics_refresh,
)

logger = logging.getLogger(__name__)

BACKGROUND_TASK_PRIORITY = getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1)

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
GENRE_BACKFILL_RECONCILE_FALLBACK_INTERVAL_SECONDS = 60 * 5

# Default batch size for reconcile tasks — mirrors NIGHTLY_METADATA_QUALITY_GENRE_BATCH_SIZE
# in tasks.py without creating a circular import.
_GENRE_BATCH_SIZE_DEFAULT = 1500


def _genre_items_queryset():
    from app.models import MetadataBackfillState  # noqa: PLC0415
    from app.providers import tvdb  # noqa: PLC0415

    tvdb_enabled = tvdb.enabled()
    from django.db.models import Q  # noqa: PLC0415
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


def _resolve_tmdb_tv_item_tvdb_id(item: Item, tmdb_metadata: dict | None) -> str | None:
    """Return a TVDB series ID for a TMDB TV item, persisting discovered mapping."""
    from app.services import metadata_resolution  # noqa: PLC0415

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


def _resolve_tvdb_id_via_imdb(
    tmdb_metadata: dict | None,
    *,
    stale_tvdb_id: str | None = None,
) -> str | None:
    """Return a TVDB series ID found via IMDb remote-ID lookup, or None.

    Used as a fallback when the stored TVDB ID is stale/wrong and the direct
    fetch returns 404. The existing upsert_provider_links call in the caller
    will persist the corrected ID automatically.
    """
    import re  # noqa: PLC0415
    from app.providers import tvdb  # noqa: PLC0415

    imdb_link = ((tmdb_metadata or {}).get("external_links") or {}).get("IMDb", "")
    if not imdb_link:
        return None

    m = re.search(r"/(tt\d+)", imdb_link)
    if not m:
        return None
    imdb_id = m.group(1)

    results = tvdb.search_remote_id(imdb_id)
    for result in results or []:
        if not isinstance(result, dict):
            continue
        series_data = result.get("series")
        if not isinstance(series_data, dict):
            continue
        found_id = str(series_data.get("id") or "").strip()
        if found_id and found_id != stale_tvdb_id:
            logger.info(
                "tvdb_id_healed_via_imdb imdb_id=%s stale_tvdb_id=%s new_tvdb_id=%s",
                imdb_id,
                stale_tvdb_id,
                found_id,
            )
            return found_id

    return None


def _tmdb_tv_item_is_tvdb_anime(item: Item, tmdb_metadata: dict | None) -> bool:
    """Return whether TVDB classifies a TMDB TV item as Anime."""
    from app.providers import tvdb  # noqa: PLC0415
    from app.services import metadata_resolution  # noqa: PLC0415

    if not tvdb.enabled():
        return False

    tvdb_id = _resolve_tmdb_tv_item_tvdb_id(item, tmdb_metadata)
    if not tvdb_id:
        return False

    from app.providers.services import ProviderAPIError  # noqa: PLC0415

    tvdb_metadata_result = None
    try:
        tvdb_metadata_result = services.get_media_metadata(
            MediaTypes.TV.value,
            tvdb_id,
            Sources.TVDB.value,
        )
    except ProviderAPIError:
        pass

    if not isinstance(tvdb_metadata_result, dict):
        healed_id = _resolve_tvdb_id_via_imdb(tmdb_metadata, stale_tvdb_id=tvdb_id)
        if healed_id:
            tvdb_id = healed_id
            tvdb_metadata_result = services.get_media_metadata(
                MediaTypes.TV.value,
                tvdb_id,
                Sources.TVDB.value,
            )

    if not isinstance(tvdb_metadata_result, dict):
        msg = "no tvdb metadata"
        raise ValueError(msg)

    metadata_resolution.upsert_provider_links(
        item,
        tvdb_metadata_result,
        provider=Sources.TVDB.value,
        provider_media_type=MediaTypes.TV.value,
    )
    return tvdb.series_has_anime_genre(tvdb_id, tv_data=tvdb_metadata_result)


def _populate_genres_for_items(items, delay_seconds):
    from app.providers import tvdb  # noqa: PLC0415

    updated_count = 0
    error_count = 0
    updated_items = []
    run = CooperativeRun("genre_backfill")
    for item in run.iter(items):
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
                import time  # noqa: PLC0415
                time.sleep(delay_seconds)
        except Exception as exc:
            error_count += 1
            logger.error("Error updating genres for %s: %s", item.title, exception_summary(exc))
            _record_backfill_failure(item, MetadataBackfillField.GENRES, f"exception: {exception_summary(exc)}")

    run.reenqueue_if_deferred(enqueue_genre_backfill_items)
    logger.info("Genre population batch completed: %s updated, %s errors", updated_count, error_count)
    if updated_items:
        _schedule_metadata_statistics_refresh(
            updated_items,
            MetadataBackfillField.GENRES,
            "genres_backfill",
        )
    return updated_count, error_count


def populate_genres_for_item_sync(item: Item, metadata: dict) -> None:
    """Synchronously populate genres for a single TMDB TV item during a manual refresh.

    Runs the full pipeline inline (including TVDB anime check + ID healing) so
    the corrected genres are persisted before the page reloads. Errors are caught
    and logged rather than raised so they don't abort the surrounding sync.
    """
    from app.providers import tvdb  # noqa: PLC0415

    source_genres = metadata_utils.extract_metadata_genres(metadata)
    incoming_genres = source_genres or metadata_utils.normalize_genres(item.genres)
    if not incoming_genres:
        logger.warning("populate_genres_for_item_sync: no genres for %s", item.title)
        return

    add_anime = False
    strategy_version = GENRE_BACKFILL_VERSION
    if tvdb.enabled():
        try:
            add_anime = _tmdb_tv_item_is_tvdb_anime(item, metadata)
        except Exception as exc:
            logger.warning(
                "populate_genres_for_item_sync tvdb_check_failed item_id=%s error=%s",
                item.id,
                exception_summary(exc),
            )
            strategy_version = None
    else:
        strategy_version = None

    genre_update_fields = metadata_utils.apply_item_genres(
        item,
        incoming_genres,
        add_anime=add_anime,
    )
    if genre_update_fields:
        with transaction.atomic():
            item.save(update_fields=genre_update_fields)
        logger.info(
            "populate_genres_for_item_sync updated item_id=%s genres=%s",
            item.id,
            item.genres,
        )

    _record_backfill_success(item, MetadataBackfillField.GENRES, strategy_version=strategy_version)


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


@shared_task(name="app.tasks.populate_genre_data_for_items")
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


@shared_task(name="app.tasks.populate_genre_backfill_queue")
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


@shared_task(name="app.tasks.reconcile_genre_backfill")
def reconcile_genre_backfill(
    strategy_version: int | None = None,
    batch_size: int = _GENRE_BATCH_SIZE_DEFAULT,
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
    batch_size: int = _GENRE_BATCH_SIZE_DEFAULT,
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
