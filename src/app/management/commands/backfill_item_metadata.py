"""Management command to backfill metadata fields for existing Items."""
from django.core.management.base import BaseCommand
from django.utils import timezone
from app import helpers
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

                details = metadata.get("details", {})

                # Extract all metadata fields (same logic as media_save)
                country = details.get("country") or ""

                languages = details.get("languages") or []
                if not isinstance(languages, list):
                    languages = [languages] if languages else []

                platforms = details.get("platforms") or []
                if not isinstance(platforms, list):
                    platforms = []

                format_type = details.get("format") or ""
                status = details.get("status") or ""

                studios = details.get("studios") or []
                if not isinstance(studios, list):
                    studios = []

                themes = details.get("themes") or []
                if not isinstance(themes, list):
                    themes = []

                authors = details.get("authors") or details.get("author") or []
                if isinstance(authors, str):
                    authors = [authors] if authors else []
                elif not isinstance(authors, list):
                    authors = []

                publishers = details.get("publishers") or details.get("publisher") or ""
                if isinstance(publishers, list):
                    publishers = publishers[0] if publishers else ""

                isbn = details.get("isbn") or []
                if not isinstance(isbn, list):
                    isbn = []

                source_material = details.get("source") or ""

                creators = details.get("people") or []
                if not isinstance(creators, list):
                    creators = []

                runtime = details.get("runtime") or ""
                release_datetime = helpers.extract_release_datetime(metadata)

                # Update item
                item.country = country
                item.languages = languages
                item.platforms = platforms
                item.format = format_type
                item.status = status
                item.studios = studios
                item.themes = themes
                item.authors = authors
                item.publishers = publishers
                item.isbn = isbn
                item.source_material = source_material
                item.creators = creators
                item.runtime = runtime
                if release_datetime:
                    item.release_datetime = release_datetime
                item.metadata_fetched_at = timezone.now()

                item.save(update_fields=[
                    'country', 'languages', 'platforms', 'format', 'status',
                    'studios', 'themes', 'authors', 'publishers', 'isbn',
                    'source_material', 'creators', 'runtime', 'release_datetime', 'metadata_fetched_at'
                ])

                success_count += 1
                self.stdout.write(
                    f"[{i}/{total}] ✓ {item.title} ({item.media_type}): "
                    f"country={country}, format={format_type}"
                )

            except Exception as e:
                error_count += 1
                self.stderr.write(
                    f"[{i}/{total}] ✗ Error for {item.title}: {str(e)}"
                )

        self.stdout.write(self.style.SUCCESS(
            f"\nBackfill complete! Success: {success_count}, Errors: {error_count}"
        ))
