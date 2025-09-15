"""Celery tasks for the app."""

import logging
from celery import shared_task
from django.db import transaction

from app.models import Item, MediaTypes
from app import providers

logger = logging.getLogger(__name__)


@shared_task
def populate_runtime_data_batch(batch_size=10, delay_seconds=1.0):
    """Populate runtime data for a batch of items that don't have it."""
    from app.statistics import parse_runtime_to_minutes
    import time
    
    # Get items that need runtime data (exclude manual items as they don't have provider data)
    items_to_update = Item.objects.filter(
        runtime_minutes__isnull=True,
        media_type__in=[MediaTypes.MOVIE.value, MediaTypes.ANIME.value, MediaTypes.EPISODE.value],
        source__in=['tmdb', 'mal', 'simkl']  # Only process items from providers that have runtime data
    ).order_by('id')[:batch_size]
    
    if not items_to_update.exists():
        logger.info("No items need runtime data")
        return {"updated": 0, "errors": 0}
    
    updated_count = 0
    error_count = 0
    
    for item in items_to_update:
        try:
            # Get metadata from provider
            metadata = providers.services.get_media_metadata(
                item.media_type.lower(),
                item.media_id,
                item.source,
            )
            
            if not metadata or not metadata.get("details", {}).get("runtime"):
                logger.warning(f"No runtime data available for {item.title}")
                continue
            
            runtime_str = metadata["details"]["runtime"]
            runtime_minutes = parse_runtime_to_minutes(runtime_str)
            
            if runtime_minutes is None:
                logger.warning(f"Failed to parse runtime '{runtime_str}' for {item.title}")
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
    
    logger.info(f"Runtime population batch completed: {updated_count} updated, {error_count} errors")
    return {"updated": updated_count, "errors": error_count}


@shared_task
def populate_runtime_data_continuous():
    """Continuously populate runtime data for items that don't have it."""
    # Run in smaller batches to avoid overwhelming the system
    result = populate_runtime_data_batch.delay(batch_size=5, delay_seconds=2.0)
    return result
