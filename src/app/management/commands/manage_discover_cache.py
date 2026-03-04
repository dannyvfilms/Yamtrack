"""Support command to clear and rebuild Discover caches."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from app.discover.profile import get_or_compute_taste_profile
from app.discover.registry import ALL_MEDIA_KEY, DISCOVER_MEDIA_TYPES, get_rows
from app.discover.service import refresh_rows_for_user
from app.models import DiscoverApiCache, DiscoverRowCache, DiscoverTasteProfile


class Command(BaseCommand):
    """Clear and rebuild Discover caches per user/media type."""

    help = "Clear and optionally rebuild Discover row/profile caches"

    def add_arguments(self, parser):
        parser.add_argument(
            "--user-id",
            action="append",
            type=int,
            dest="user_ids",
            help="Target user id (repeatable). Defaults to all users.",
        )
        parser.add_argument(
            "--media-type",
            default=ALL_MEDIA_KEY,
            help="Target media type or 'all' (default: all)",
        )
        parser.add_argument(
            "--row-key",
            action="append",
            dest="row_keys",
            help="Optional row key filter (repeatable)",
        )
        parser.add_argument(
            "--clear-only",
            action="store_true",
            help="Clear cache entries without rebuilding",
        )
        parser.add_argument(
            "--include-api-cache",
            action="store_true",
            help="Also clear global Discover API cache entries",
        )

    def _resolve_media_types(self, media_type: str) -> list[str]:
        media_type = (media_type or ALL_MEDIA_KEY).strip().lower()
        if media_type == ALL_MEDIA_KEY:
            return list(DISCOVER_MEDIA_TYPES)
        if media_type not in DISCOVER_MEDIA_TYPES:
            raise CommandError(f"Unsupported media_type '{media_type}'")
        return [media_type]

    def _resolve_row_keys(self, media_type: str, raw_row_keys: list[str] | None) -> list[str]:
        if not raw_row_keys:
            if media_type == ALL_MEDIA_KEY:
                return []
            return [row.key for row in get_rows(media_type, include_show_more=True)]
        return sorted({key.strip() for key in raw_row_keys if key and key.strip()})

    def handle(self, *_args, **options):
        user_model = get_user_model()
        users = user_model.objects.all().order_by("id")

        user_ids = options.get("user_ids") or []
        if user_ids:
            users = users.filter(id__in=user_ids)

        if not users.exists():
            self.stdout.write(self.style.WARNING("No users matched the filter."))
            return

        media_type = (options.get("media_type") or ALL_MEDIA_KEY).strip().lower()
        target_media_types = self._resolve_media_types(media_type)
        row_keys = self._resolve_row_keys(media_type, options.get("row_keys"))
        clear_only = bool(options.get("clear_only"))
        include_api_cache = bool(options.get("include_api_cache"))

        row_cache_queryset = DiscoverRowCache.objects.filter(
            user__in=users,
            media_type__in=target_media_types,
        )
        if row_keys:
            row_cache_queryset = row_cache_queryset.filter(row_key__in=row_keys)

        profile_queryset = DiscoverTasteProfile.objects.filter(
            user__in=users,
            media_type__in=target_media_types,
        )

        deleted_rows = row_cache_queryset.count()
        deleted_profiles = profile_queryset.count()
        row_cache_queryset.delete()
        profile_queryset.delete()

        deleted_api = 0
        if include_api_cache:
            deleted_api = DiscoverApiCache.objects.count()
            DiscoverApiCache.objects.all().delete()

        self.stdout.write(
            "Cleared Discover cache entries "
            f"(rows={deleted_rows}, profiles={deleted_profiles}, api={deleted_api}).",
        )

        if clear_only:
            self.stdout.write(self.style.SUCCESS("Clear-only operation complete."))
            return

        refreshed_rows = 0
        refreshed_profiles = 0

        for user in users.iterator(chunk_size=200):
            for target_media_type in target_media_types:
                get_or_compute_taste_profile(user, target_media_type, force=True)
                refreshed_profiles += 1

                target_row_keys = row_keys or [
                    row.key
                    for row in get_rows(target_media_type, include_show_more=True)
                ]
                refreshed_rows += refresh_rows_for_user(
                    user,
                    target_media_type,
                    target_row_keys,
                    show_more=True,
                )

        self.stdout.write(
            self.style.SUCCESS(
                "Rebuild complete "
                f"(rows={refreshed_rows}, profiles={refreshed_profiles}).",
            ),
        )
