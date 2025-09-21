"""Celery tasks for the app."""

import logging
from celery import shared_task
from django.db import transaction

from app.models import Item, MediaTypes
from app.providers import services

logger = logging.getLogger(__name__)


@shared_task
def populate_runtime_data_batch(batch_size=10, delay_seconds=1.0):
    """Populate runtime data for a batch of items that don't have it."""
    from app.statistics import parse_runtime_to_minutes
    import time
    
    # Get items that need runtime data (exclude manual items, episodes, and previously failed items)
    items_to_update = Item.objects.filter(
        runtime_minutes__isnull=True,
        media_type__in=[MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value],  # Episodes get runtime from season metadata, not individually
        source__in=['tmdb', 'mal', 'simkl']  # Only process items from providers that have runtime data
    ).exclude(
        runtime_minutes=999999  # Exclude items marked as failed
    ).order_by('id')[:batch_size]
    
    if not items_to_update.exists():
        logger.info("No items need runtime data")
        return {"updated": 0, "errors": 0}
    
    updated_count = 0
    error_count = 0
    
    for item in items_to_update:
        try:
            # Get metadata from provider
            metadata = services.get_media_metadata(
                item.media_type.lower(),
                item.media_id,
                item.source,
            )
            
            # Check if metadata is None or doesn't have the expected structure
            if not metadata:
                logger.warning(f"No metadata returned for {item.title} ({item.media_type}, {item.source})")
                error_count += 1
                continue
                
            if not isinstance(metadata, dict):
                logger.warning(f"Invalid metadata format for {item.title}: {type(metadata)}")
                error_count += 1
                continue
                
            if not metadata.get("details"):
                logger.warning(f"No details in metadata for {item.title}")
                error_count += 1
                continue
                
            details = metadata["details"]
            runtime_str = details.get("runtime")
            
            if not runtime_str:
                logger.warning(f"No runtime data available for {item.title}")
                error_count += 1
                
                # Mark item as failed to avoid endless retries
                try:
                    with transaction.atomic():
                        item.runtime_minutes = 999999  # Use 999999 as a "failed" marker
                        item.save()
                    logger.warning(f"Marked {item.title} as failed (runtime_minutes=999999) - no runtime data available")
                except Exception as save_error:
                    logger.error(f"Failed to mark {item.title} as failed: {save_error}")
                
                continue
            
            runtime_minutes = parse_runtime_to_minutes(runtime_str)
            
            if runtime_minutes is None:
                logger.warning(f"Failed to parse runtime '{runtime_str}' for {item.title}")
                error_count += 1
                
                # Mark item as failed to avoid endless retries
                try:
                    with transaction.atomic():
                        item.runtime_minutes = 999999  # Use 999999 as a "failed" marker
                        item.save()
                    logger.warning(f"Marked {item.title} as failed (runtime_minutes=999999) - failed to parse runtime")
                except Exception as save_error:
                    logger.error(f"Failed to mark {item.title} as failed: {save_error}")
                
                continue
            
            # Update the item
            with transaction.atomic():
                item.runtime_minutes = runtime_minutes
                item.save()
            
            updated_count += 1
            logger.info(f"Updated runtime for {item.title}: {runtime_minutes} minutes")
            
            # Add delay to avoid rate limiting
            if delay_seconds > 0:
                time.sleep(delay_seconds)
                
        except Exception as e:
            error_count += 1
            logger.error(f"Error updating runtime for {item.title}: {e}")
            
            # Mark item as failed by setting runtime_minutes to 999999 to avoid endless retries
            # This is a simple way to skip items that consistently fail
            try:
                with transaction.atomic():
                    item.runtime_minutes = 999999  # Use 999999 as a "failed" marker
                    item.save()
                logger.warning(f"Marked {item.title} as failed (runtime_minutes=999999) to avoid endless retries")
            except Exception as save_error:
                logger.error(f"Failed to mark {item.title} as failed: {save_error}")
    
    logger.info(f"Runtime population batch completed: {updated_count} updated, {error_count} errors")
    
    # Check if there are more items to process (exclude previously failed items)
    remaining_items = Item.objects.filter(
        runtime_minutes__isnull=True,
        media_type__in=[MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value],
        source__in=['tmdb', 'mal', 'simkl']
    ).exclude(
        runtime_minutes=999999  # Exclude items marked as failed
    ).count()
    
    if remaining_items > 0:
        logger.info(f"Found {remaining_items} remaining items. Scheduling next batch...")
        # Schedule the next batch with a small delay
        populate_runtime_data_batch.apply_async(
            kwargs={'batch_size': batch_size, 'delay_seconds': delay_seconds},
            countdown=5  # 5 second delay between batches
        )
        return {
            "updated": updated_count, 
            "errors": error_count,
            "remaining_items": remaining_items,
            "next_batch_scheduled": True
        }
    else:
        logger.info("🎉 All runtime data population completed! No more items need processing.")
        
        # Mark as completed in cache to prevent repeated runs
        from django.core.cache import cache
        cache.set("runtime_population_completed", True, timeout=3600)  # 1 hour
        
        return {
            "updated": updated_count, 
            "errors": error_count,
            "remaining_items": 0,
            "next_batch_scheduled": False,
            "completion_message": "All runtime data populated successfully!"
        }


@shared_task
def populate_runtime_data_continuous():
    """Populate runtime data for ALL items that don't have it (startup task)."""
    from app.models import Item, MediaTypes
    from django.core.cache import cache
    
    # Check if runtime population has already been completed recently (within last hour)
    cache_key = "runtime_population_completed"
    if cache.get(cache_key):
        # Check if episodes also need runtime data
        episodes_needing_runtime = Item.objects.filter(
            runtime_minutes__isnull=True,
            media_type=MediaTypes.EPISODE.value,
            source__in=['tmdb', 'mal', 'simkl']
        ).exclude(
            runtime_minutes=999999
        ).count()
        
        if episodes_needing_runtime > 0:
            logger.info(f"Runtime population completed for movies/TV/anime, but {episodes_needing_runtime} episodes still need runtime data. Starting episode population...")
            # Clear the cache and continue with episode population
            cache.delete(cache_key)
        else:
            logger.info("Runtime population already completed recently - skipping")
            return {"total_items": 0, "batches_scheduled": 0, "message": "Already completed recently"}
    
    # Count total items that need runtime data (exclude previously failed items)
    total_items = Item.objects.filter(
        runtime_minutes__isnull=True,
        media_type__in=[MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value],
        source__in=['tmdb', 'mal', 'simkl']
    ).exclude(
        runtime_minutes=999999  # Exclude items marked as failed
    ).count()
    
    if total_items == 0:
        # Check if episodes also need runtime data
        episodes_needing_runtime = Item.objects.filter(
            runtime_minutes__isnull=True,
            media_type=MediaTypes.EPISODE.value,
            source__in=['tmdb', 'mal', 'simkl']
        ).exclude(
            runtime_minutes=999999
        ).count()
        
        if episodes_needing_runtime > 0:
            logger.info(f"No movies/TV/anime need runtime data, but {episodes_needing_runtime} episodes still need runtime data. Starting episode population...")
            # Start episode population
            episode_result = populate_episode_runtime_data.delay()
            return {
                "total_items": 0,
                "episode_task_id": episode_result.id,
                "message": f"Movies/TV/anime up to date, starting episode population for {episodes_needing_runtime} episodes"
            }
        else:
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
        "message": "Started comprehensive runtime population for movies/TV/anime and episodes. Check logs for progress."
    }


@shared_task
def populate_episode_runtime_data():
    """Populate runtime data for episodes by syncing season metadata."""
    from app.models import Item, MediaTypes, Season
    from app.providers import services
    from app.statistics import parse_runtime_to_minutes
    import time
    
    # Find episodes that need runtime data
    episodes_needing_runtime = Item.objects.filter(
        runtime_minutes__isnull=True,
        media_type=MediaTypes.EPISODE.value,
        source__in=['tmdb', 'mal', 'simkl']
    ).exclude(
        runtime_minutes=999999
    )
    
    if not episodes_needing_runtime.exists():
        logger.info("No episodes need runtime data")
        return {"updated": 0, "errors": 0, "message": "No episodes need runtime data"}
    
    updated_count = 0
    error_count = 0
    processed_seasons = set()
    
    for episode in episodes_needing_runtime:
        try:
            # Create a season key to avoid processing the same season multiple times
            season_key = (episode.media_id, episode.source, episode.season_number)
            
            if season_key in processed_seasons:
                continue
                
            processed_seasons.add(season_key)
            
            # Get season metadata to populate episode runtime data
            season_metadata = services.get_media_metadata(
                "tv_with_seasons",
                episode.media_id,
                episode.source,
                [episode.season_number]
            )
            
            if not season_metadata or f"season/{episode.season_number}" not in season_metadata:
                logger.warning(f"No season metadata for {episode.title} S{episode.season_number}")
                error_count += 1
                continue
            
            season_data = season_metadata[f"season/{episode.season_number}"]
            
            # Process episodes to get runtime data
            from app.providers import tmdb
            episodes_metadata = tmdb.process_episodes(season_data, [])
            
            # Update episodes with runtime data
            for ep_data in episodes_metadata:
                if ep_data.get("runtime"):
                    episode_item, created = Item.objects.update_or_create(
                        media_id=episode.media_id,
                        source=episode.source,
                        media_type=MediaTypes.EPISODE.value,
                        season_number=episode.season_number,
                        episode_number=ep_data["episode_number"],
                        defaults={
                            "title": episode.title,  # Keep existing title
                            "image": ep_data.get("image", episode.image),
                            "runtime_minutes": parse_runtime_to_minutes(ep_data["runtime"])
                        }
                    )
                    
                    if not created and episode_item.runtime_minutes:
                        updated_count += 1
                        logger.info(f"Updated runtime for {episode_item.title} S{episode.season_number}E{ep_data['episode_number']}: {episode_item.runtime_minutes} minutes")
            
            # Small delay to avoid rate limiting
            time.sleep(0.1)
            
        except Exception as e:
            logger.error(f"Error processing episode {episode.title}: {e}")
            error_count += 1
            continue
    
    logger.info(f"Episode runtime population completed: {updated_count} episodes updated, {error_count} errors")
    
    # Mark runtime population as completed since both movies/TV/anime and episodes are done
    from django.core.cache import cache
    cache.set("runtime_population_completed", True, timeout=3600)  # 1 hour
    logger.info("🎉 All runtime data population completed! Movies, TV shows, anime, and episodes all processed.")
    
    return {
        "updated": updated_count,
        "errors": error_count,
        "message": f"Processed {len(processed_seasons)} seasons, updated {updated_count} episodes. All runtime data population completed!"
    }
