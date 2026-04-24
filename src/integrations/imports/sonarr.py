"""Sonarr importer for TV collection ownership sync."""

import logging
from collections import defaultdict

import requests
from django.conf import settings
from django.utils import timezone

from app.models import Item, MediaTypes, Sources
from app.providers import services
from integrations.imports.helpers import MediaImportError, decrypt
from integrations.models import SonarrAccount
from integrations.source_sync import upsert_collection_source_state

logger = logging.getLogger(__name__)


class SonarrClient:
    """Thin API client for Sonarr v3."""

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, path: str):
        response = requests.get(
            f"{self.base_url}{path}",
            headers={"X-Api-Key": self.api_key},
            timeout=20,
        )
        if response.status_code in (401, 403):
            msg = "Sonarr API key is invalid or unauthorized"
            raise MediaImportError(msg)
        if response.status_code >= 400:
            msg = f"Sonarr request failed ({response.status_code}) for {path}"
            raise MediaImportError(msg)
        return response.json()

    def healthcheck(self):
        """Verify connection."""
        return self._request("/api/v3/system/status")

    def series(self):
        """Fetch tracked series rows."""
        return self._request("/api/v3/series")


def importer(identifier, user, mode):  # noqa: ARG001
    """Import Sonarr collection ownership."""
    return SonarrImporter(user).import_data()


class SonarrImporter:
    """Import collection data from Sonarr."""

    def __init__(self, user):
        self.user = user
        try:
            self.account = user.sonarr_account
        except SonarrAccount.DoesNotExist as error:
            msg = "Connect Sonarr before importing"
            raise MediaImportError(msg) from error

        self.client = SonarrClient(
            self.account.base_url,
            decrypt(self.account.api_key),
        )
        self.warnings = []

    def import_data(self):
        imported_counts = defaultdict(int)

        try:
            series_rows = self.client.series()
        except MediaImportError as error:
            self.account.connection_broken = True
            self.account.last_error_message = str(error)
            self.account.save(update_fields=["connection_broken", "last_error_message", "updated_at"])
            raise

        for row in series_rows:
            stats = row.get("statistics") or {}
            if int(stats.get("episodeFileCount") or 0) <= 0:
                continue

            item = self._resolve_series_item(row)
            if not item:
                imported_counts["skipped_missing_ids"] += 1
                continue

            upsert_collection_source_state(
                user=self.user,
                item=item,
                source="sonarr",
                quality_label="",
                source_updated_at=self._parse_source_timestamp(row),
            )
            imported_counts[item.media_type] += 1
            imported_counts["updated"] += 1

        self.account.last_sync_at = timezone.now()
        self.account.connection_broken = False
        self.account.last_error_message = ""
        self.account.save(update_fields=["last_sync_at", "connection_broken", "last_error_message", "updated_at"])

        return dict(imported_counts), "\n".join(dict.fromkeys(self.warnings))

    def _resolve_series_item(self, row):
        tvdb_id = row.get("tvdbId")
        tmdb_id = row.get("tmdbId")

        if tmdb_id:
            existing = Item.objects.filter(
                media_id=str(tmdb_id),
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
            ).first()
            if existing:
                return existing

            try:
                metadata = services.get_media_metadata(
                    MediaTypes.TV.value,
                    str(tmdb_id),
                    Sources.TMDB.value,
                )
            except services.ProviderAPIError as error:
                if getattr(error, "status_code", None) == 404:
                    title = row.get("title") or row.get("sortTitle") or tmdb_id
                    self.warnings.append(
                        f"{title}: not found in {Sources.TMDB.label} with ID {tmdb_id}.",
                    )
                    return None
                raise

            defaults = {
                "title": metadata["title"],
                "image": metadata.get("image") or settings.IMG_NONE,
                "release_datetime": metadata.get("release_datetime"),
                "genres": metadata.get("genres") or [],
                "original_title": metadata.get("original_title") or metadata["title"],
                "localized_title": metadata.get("localized_title") or metadata["title"],
            }
            item, _ = Item.objects.update_or_create(
                media_id=str(tmdb_id),
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                defaults=defaults,
            )
            return item

        if tvdb_id:
            return Item.objects.filter(
                media_id=str(tvdb_id),
                source=Sources.TVDB.value,
                media_type=MediaTypes.TV.value,
            ).first()

        return None

    def _parse_source_timestamp(self, row):
        for key in ("lastInfoSync", "added", "updated"):
            value = row.get(key)
            if not value:
                continue
            try:
                return timezone.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
        return timezone.now()
