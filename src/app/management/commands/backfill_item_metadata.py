"""Management command to backfill metadata fields for existing Items."""
from django.core.management.base import BaseCommand
from django.utils import timezone
from app import metadata_utils
from app.models import Item
from app.providers import services


class Command(BaseCommand):
    """Backfill metadata fields for existing Items."""

    help = 'Backfill metadata fields for existing Items that have never been fetched'

    def add_arguments(self, parser):
        """Add command line arguments."""
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Limit number of items to process (for testing)'
        )
        parser.add_argument(
            '--media-type',
            type=str,
            default=None,
            help='Only process specific media type (e.g., "tv", "movie", "anime")'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force re-fetch metadata even if already fetched before'
        )

    def handle(self, *args, **options):
        """Execute the command."""
        # Filter items that have never had metadata fetched
        # (metadata_fetched_at is NULL means we've never tried)
        if options['force']:
            queryset = Item.objects.all()
            self.stdout.write("Force mode: Re-fetching metadata for ALL items")
        else:
            queryset = Item.objects.filter(metadata_fetched_at__isnull=True)
            self.stdout.write("Only fetching metadata for items never checked before")

        if options['media_type']:
            queryset = queryset.filter(media_type=options['media_type'])

        if options['limit']:
            queryset = queryset[:options['limit']]

        total = queryset.count()
        self.stdout.write(f"Backfilling metadata for {total} items...")

        success_count = 0
        error_count = 0

        for i, item in enumerate(queryset, 1):
            try:
                metadata = services.get_media_metadata(
                    item.media_type,
                    item.media_id,
                    item.source
                )

                update_fields = metadata_utils.apply_item_metadata(
                    item,
                    metadata,
                    include_core=True,
                    include_provider=True,
                    include_release=True,
                )
                item.metadata_fetched_at = timezone.now()
                update_fields.append("metadata_fetched_at")
                item.save(update_fields=update_fields)

                success_count += 1
                self.stdout.write(
                    f"[{i}/{total}] ✓ {item.title} ({item.media_type}): "
                    f"country={item.country}, format={item.format}"
                )

            except Exception as e:
                error_count += 1
                self.stderr.write(
                    f"[{i}/{total}] ✗ Error for {item.title}: {str(e)}"
                )

        self.stdout.write(self.style.SUCCESS(
            f"\nBackfill complete! Success: {success_count}, Errors: {error_count}"
        ))
