"""Episode runtime population Celery tasks.

Extracted from tasks.py. Re-exported from app.tasks for backward compatibility.
Tasks use deferred imports for private helpers that remain in tasks.py to avoid
circular imports (tasks.py re-exports these tasks).
"""

import logging

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q

from app.log_safety import exception_summary
from app.models import MetadataBackfillField

logger = logging.getLogger(__name__)


@shared_task(name="app.tasks.populate_episode_runtime_queue")
def populate_episode_runtime_queue(batch_size: int = 20):
    """Drain the episode runtime queue and process seasons in small batches."""
    # Deferred to avoid circular import: tasks.py re-exports this module.
    from app.tasks import (  # noqa: PLC0415
        RUNTIME_BACKFILL_EPISODES_QUEUE_KEY,
        RUNTIME_BACKFILL_EPISODES_SCHEDULED_KEY,
        RUNTIME_BACKFILL_QUEUE_TTL,
    )

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


@shared_task(name="app.tasks.populate_episode_runtime_data")
def populate_episode_runtime_data(season_keys: list[str] | None = None):
    """Populate runtime data for episodes by syncing season metadata."""
    import time  # noqa: PLC0415

    from app.models import Item, MediaTypes  # noqa: PLC0415
    from app.providers import services as _services  # noqa: PLC0415
    from app.statistics import parse_runtime_to_minutes  # noqa: PLC0415

    # Deferred to avoid circular import: tasks.py re-exports this module.
    from app.tasks import (  # noqa: PLC0415
        _episode_runtime_items_queryset,
        _normalize_season_keys,
        _record_backfill_failure,
        _record_backfill_success,
        _schedule_metadata_statistics_refresh,
    )

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

            season_metadata = _services.get_media_metadata(
                "tv_with_seasons",
                media_id,
                source,
                [season_number],
            )

            if not season_metadata or f"season/{season_number}" not in season_metadata:
                logger.warning(
                    "No season metadata during runtime backfill season=%s missing_episodes=%s",
                    season_number,
                    len(eligible_missing),
                )
                error_count += len(eligible_missing)
                for episode_item in eligible_missing:
                    _record_backfill_failure(
                        episode_item,
                        MetadataBackfillField.RUNTIME,
                        "no season metadata",
                    )
                continue

            season_data = season_metadata[f"season/{season_number}"]

            from app.providers import tmdb  # noqa: PLC0415

            episodes_metadata = tmdb.process_episodes(season_data, [])
            if not episodes_metadata:
                logger.warning(
                    "No episode metadata during runtime backfill season=%s missing_episodes=%s",
                    season_number,
                    len(eligible_missing),
                )
                error_count += len(eligible_missing)
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
                    logger.debug(
                        "Skipping episode metadata row without episode_number during runtime backfill season=%s",
                        season_number,
                    )
                    continue
                runtime_value = ep_data.get("runtime")
                if not runtime_value:
                    missing_item = missing_by_number.pop(episode_number, None)
                    if missing_item:
                        logger.debug(
                            "Episode metadata has no runtime during backfill season=%s episode=%s",
                            season_number,
                            episode_number,
                        )
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
                        logger.warning(
                            "Failed to parse runtime during backfill season=%s episode=%s",
                            season_number,
                            episode_number,
                        )
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
                                "Updated episode runtime during backfill season=%s episode=%s minutes=%s",
                                season_number,
                                episode_number,
                                runtime_minutes,
                            )
                else:
                    logger.debug(
                        "Skipping runtime backfill item creation for season=%s episode=%s; only existing episodes are updated",
                        season_number,
                        episode_number,
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
            logger.error(
                "Episode runtime backfill failed source=%s season=%s error=%s",
                source,
                season_number,
                exception_summary(e),
            )
            error_count += 1
            continue

    logger.info("Episode runtime population completed: %s episodes updated, %s errors", updated_count, error_count)

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
