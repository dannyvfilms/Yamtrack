"""Radarr importer for movie collection ownership sync."""

import logging
from collections import defaultdict

import requests
from django.conf import settings
from django.utils import timezone

from app.models import Item, MediaTypes, Sources
from app.providers import services
from integrations.imports.helpers import MediaImportError, decrypt
from integrations.models import RadarrAccount
from integrations.source_sync import upsert_collection_source_state

logger = logging.getLogger(__name__)


class RadarrClient:
    """Thin API client for Radarr v3."""

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
            msg = "Radarr API key is invalid or unauthorized"
            raise MediaImportError(msg)
        if response.status_code >= 400:
            msg = f"Radarr request failed ({response.status_code}) for {path}"
            raise MediaImportError(msg)
        return response.json()

    def healthcheck(self):
        """Verify connection."""
        return self._request("/api/v3/system/status")

    def movies(self):
        """Fetch movie collection rows."""
        return self._request("/api/v3/movie")


def importer(identifier, user, mode):  # noqa: ARG001
    """Import Radarr collection ownership."""
    return RadarrImporter(user).import_data()


class RadarrImporter:
    """Import collection data from Radarr."""

    def __init__(self, user):
        self.user = user
        try:
            self.account = user.radarr_account
        except RadarrAccount.DoesNotExist as error:
            msg = "Connect Radarr before importing"
            raise MediaImportError(msg) from error

        self.client = RadarrClient(
            self.account.base_url,
            decrypt(self.account.api_key),
        )
        self.warnings = []

    def import_data(self):
        imported_counts = defaultdict(int)

        try:
            movies = self.client.movies()
        except MediaImportError as error:
            self.account.connection_broken = True
            self.account.last_error_message = str(error)
            self.account.save(update_fields=["connection_broken", "last_error_message", "updated_at"])
            raise

        for row in movies:
            if not row.get("hasFile"):
                continue

            item = self._resolve_movie_item(row)
            if not item:
                imported_counts["skipped_missing_ids"] += 1
                continue

            quality_label = (
                (row.get("movieFile") or {}).get("quality", {}).get("quality", {}).get("name")
                or ""
            )
            updated_at = self._parse_source_timestamp(row)
            upsert_collection_source_state(
                user=self.user,
                item=item,
                source="radarr",
                quality_label=quality_label,
                source_updated_at=updated_at,
            )
            imported_counts[item.media_type] += 1
            imported_counts["updated"] += 1

        self.account.last_sync_at = timezone.now()
        self.account.connection_broken = False
        self.account.last_error_message = ""
        self.account.save(update_fields=["last_sync_at", "connection_broken", "last_error_message", "updated_at"])

        return dict(imported_counts), "\n".join(dict.fromkeys(self.warnings))

    def _resolve_movie_item(self, row):
        tmdb_id = row.get("tmdbId")
        imdb_id = row.get("imdbId")

        if tmdb_id:
            existing = Item.objects.filter(
                media_id=str(tmdb_id),
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
            ).first()
            if existing:
                return existing

            try:
                metadata = services.get_media_metadata(
                    MediaTypes.MOVIE.value,
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
                media_type=MediaTypes.MOVIE.value,
                defaults=defaults,
            )
            return item

        if imdb_id:
            existing = Item.objects.filter(
                media_id=str(imdb_id),
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
            ).first()
            return existing

        return None

    def _parse_source_timestamp(self, row):
        for key in ("movieFile", "added", "lastInfoSync", "updated"):
            value = row.get(key)
            if isinstance(value, dict):
                value = value.get("dateAdded") or value.get("date")
            if not value:
                continue
            try:
                return timezone.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
        return timezone.now()
