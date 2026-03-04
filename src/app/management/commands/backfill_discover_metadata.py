"""Backfill provider popularity/rating fields used by Discover."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from app.models import Item, MediaTypes, Sources
from app.providers import services

TMDB_BASE_URL = "https://api.themoviedb.org/3"


class Command(BaseCommand):
    """Populate Item.provider_popularity/provider_rating/provider_rating_count."""

    help = "Backfill provider popularity/rating fields for Discover"

    def add_arguments(self, parser):
        parser.add_argument(
            "--media-types",
            default=f"{MediaTypes.MOVIE.value},{MediaTypes.TV.value}",
            help="Comma-separated media types to backfill (default: movie,tv)",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=200,
            help="Number of items to process per batch",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Optional max item count",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be updated without writing",
        )

    def _parse_media_types(self, raw_value: str) -> list[str]:
        parts = [part.strip() for part in raw_value.split(",") if part.strip()]
        return [part for part in parts if part in {MediaTypes.MOVIE.value, MediaTypes.TV.value}]

    def _tmdb_fetch(self, media_type: str, media_id: str):
        endpoint = f"/{media_type}/{media_id}"
        response = services.api_request(
            Sources.TMDB.value,
            "GET",
            f"{TMDB_BASE_URL}{endpoint}",
            params={
                "api_key": settings.TMDB_API,
                "language": settings.TMDB_LANG,
            },
        )
        return {
            "provider_popularity": response.get("popularity"),
            "provider_rating": response.get("vote_average"),
            "provider_rating_count": response.get("vote_count"),
        }

    def handle(self, *_args, **options):
        media_types = self._parse_media_types(options["media_types"])
        if not media_types:
            self.stdout.write(self.style.ERROR("No supported media types specified."))
            return

        batch_size = max(1, int(options["batch_size"]))
        limit = options.get("limit")
        dry_run = bool(options.get("dry_run"))

        queryset = Item.objects.filter(
            source=Sources.TMDB.value,
            media_type__in=media_types,
        ).filter(
            provider_popularity__isnull=True,
            provider_rating__isnull=True,
            provider_rating_count__isnull=True,
        ).order_by("id")

        if limit:
            queryset = queryset[:limit]

        total = queryset.count()
        updated = 0
        failed = 0
        buffer: list[Item] = []

        self.stdout.write(f"Backfilling Discover metadata for {total} item(s)...")

        for item in queryset.iterator(chunk_size=batch_size):
            try:
                payload = self._tmdb_fetch(item.media_type, item.media_id)
            except Exception as error:  # noqa: BLE001
                failed += 1
                self.stderr.write(
                    f"Failed item_id={item.id} media_type={item.media_type} media_id={item.media_id}: {error}",
                )
                continue

            item.provider_popularity = payload.get("provider_popularity")
            item.provider_rating = payload.get("provider_rating")
            item.provider_rating_count = payload.get("provider_rating_count")
            item.metadata_fetched_at = timezone.now()
            buffer.append(item)

            if len(buffer) >= batch_size:
                updated += len(buffer)
                if not dry_run:
                    Item.objects.bulk_update(
                        buffer,
                        [
                            "provider_popularity",
                            "provider_rating",
                            "provider_rating_count",
                            "metadata_fetched_at",
                        ],
                        batch_size=batch_size,
                    )
                buffer = []

        if buffer:
            updated += len(buffer)
            if not dry_run:
                Item.objects.bulk_update(
                    buffer,
                    [
                        "provider_popularity",
                        "provider_rating",
                        "provider_rating_count",
                        "metadata_fetched_at",
                    ],
                    batch_size=batch_size,
                )

        if dry_run:
            self.stdout.write(self.style.WARNING(f"Dry run complete: would update {updated} items, failed {failed}."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Backfill complete: updated {updated} items, failed {failed}."))
