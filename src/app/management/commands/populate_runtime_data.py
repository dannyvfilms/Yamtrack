"""Management command to populate runtime data for existing items."""

import logging
import time

from django.core.management.base import BaseCommand
from django.db import transaction

from app import providers
from app.models import Item, MediaTypes

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Populate runtime data for existing items that don't have it."""

    help = "Populate runtime data for existing items that don't have it"

    def add_arguments(self, parser):
        """Add command arguments."""
        parser.add_argument(
            "--batch-size",
            type=int,
            default=10,
            help="Number of items to process in each batch",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=1.0,
            help="Delay in seconds between API calls to avoid rate limiting",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without making changes",
        )

    def handle(self, *args, **options):
        """Handle the command."""
        batch_size = options["batch_size"]
        delay = options["delay"]
        dry_run = options["dry_run"]

        self.stdout.write("Starting runtime data population...")

        # Get items that need runtime data (exclude manual items as they don't have provider data)
        items_to_update = Item.objects.filter(
            runtime_minutes__isnull=True,
            media_type__in=[MediaTypes.MOVIE.value, MediaTypes.ANIME.value, MediaTypes.EPISODE.value],
            source__in=["tmdb", "mal", "simkl"],  # Only process items from providers that have runtime data
        ).order_by("id")

        total_items = items_to_update.count()
        self.stdout.write(f"Found {total_items} items that need runtime data")

        if total_items == 0:
            self.stdout.write("No items need runtime data. Exiting.")
            return

        if dry_run:
            self.stdout.write("DRY RUN MODE - No changes will be made")
            for item in items_to_update[:10]:  # Show first 10 as example
                self.stdout.write(f"Would update: {item.title} ({item.media_type})")
            return

        updated_count = 0
        error_count = 0

        for i in range(0, total_items, batch_size):
            batch = items_to_update[i:i + batch_size]
            self.stdout.write(f"Processing batch {i//batch_size + 1} ({len(batch)} items)")

            for item in batch:
                try:
                    self._update_item_runtime(item)
                    updated_count += 1
                    self.stdout.write(f"  ✓ Updated {item.title}")

                    # Add delay to avoid rate limiting
                    if delay > 0:
                        time.sleep(delay)

                except Exception as e:
                    error_count += 1
                    self.stdout.write(f"  ✗ Error updating {item.title}: {e}")
                    logger.error(f"Error updating runtime for {item.title}: {e}")

        self.stdout.write(f"\nCompleted! Updated {updated_count} items, {error_count} errors")

    def _update_item_runtime(self, item):
        """Update runtime for a single item."""
        try:
            # Get metadata from provider
            metadata = providers.services.get_media_metadata(
                item.media_type.lower(),
                item.media_id,
                item.source,
            )

            if not metadata:
                raise ValueError("No metadata returned from provider")

            if not metadata.get("details"):
                raise ValueError("No details in metadata")

            if not metadata["details"].get("runtime"):
                raise ValueError("No runtime data in metadata")

            runtime_str = metadata["details"]["runtime"]

            # Parse runtime to minutes
            from app.statistics import parse_runtime_to_minutes
            runtime_minutes = parse_runtime_to_minutes(runtime_str)

            if runtime_minutes is None:
                raise ValueError(f"Failed to parse runtime '{runtime_str}'")

            # Update the item
            with transaction.atomic():
                item.runtime_minutes = runtime_minutes
                item.save()

        except Exception as e:
            logger.error(f"Failed to update runtime for {item.title}: {e}")
            raise
