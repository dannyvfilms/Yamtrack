"""Sonarr importer for TV collection ownership sync."""

import logging
from collections import defaultdict

import requests
from django.conf import settings
from django.utils import timezone

from app.models import CollectionEntry, Item, MediaTypes, Sources
from app.providers import services
from integrations.imports.helpers import MediaImportError, decrypt, retry_on_lock
from integrations.models import SonarrAccount
from integrations.source_sync import (
    remove_collection_source_state,
    upsert_collection_source_state,
)

logger = logging.getLogger(__name__)
HTTP_STATUS_BAD_REQUEST = 400
HTTP_STATUS_NOT_FOUND = 404


class SonarrClient:
    """Thin API client for Sonarr v3."""

    def __init__(self, base_url: str, api_key: str):
        """Store Sonarr connection settings."""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _request(self, path: str, params=None):
        response = requests.get(
            f"{self.base_url}{path}",
            headers={"X-Api-Key": self.api_key},
            params=params,
            timeout=20,
        )
        if response.status_code in (401, 403):
            msg = "Sonarr API key is invalid or unauthorized"
            raise MediaImportError(msg)
        if response.status_code >= HTTP_STATUS_BAD_REQUEST:
            msg = f"Sonarr request failed ({response.status_code}) for {path}"
            raise MediaImportError(msg)
        return response.json()

    def healthcheck(self):
        """Verify connection."""
        return self._request("/api/v3/system/status")

    def series(self):
        """Fetch tracked series rows."""
        return self._request("/api/v3/series")

    def episodes(self, series_id):
        """Fetch all episodes for a Sonarr series."""
        return self._request("/api/v3/episode", params={"seriesId": series_id})


def importer(identifier, user, mode):  # noqa: ARG001
    """Import Sonarr collection ownership."""
    return SonarrImporter(user).import_data()


class SonarrImporter:
    """Import collection data from Sonarr."""

    def __init__(self, user):
        """Bind the importer to a user with a connected Sonarr account."""
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
        """Sync Sonarr ownership into Yamtrack collection rows."""
        imported_counts = defaultdict(int)

        try:
            series_rows = self.client.series()
        except MediaImportError as error:
            self.account.connection_broken = True
            self.account.last_error_message = str(error)
            self.account.save(
                update_fields=["connection_broken", "last_error_message", "updated_at"],
            )
            raise

        for row in series_rows:
            stats = row.get("statistics") or {}
            episode_file_count = int(stats.get("episodeFileCount") or 0)
            if episode_file_count <= 0:
                item = self._find_series_item(row)
                if item:
                    self._prune_series_collection_state(item)
                continue

            item = self._resolve_series_item(row)
            if not item:
                imported_counts["skipped_missing_ids"] += 1
                continue

            if not self._sync_series_episode_collection(item, row):
                continue
            imported_counts[item.media_type] += 1
            imported_counts["updated"] += 1

        self.account.last_sync_at = timezone.now()
        self.account.connection_broken = False
        self.account.last_error_message = ""
        self.account.save(
            update_fields=[
                "last_sync_at",
                "connection_broken",
                "last_error_message",
                "updated_at",
            ],
        )

        return dict(imported_counts), "\n".join(dict.fromkeys(self.warnings))

    def _find_series_item(self, row):
        """Find an existing Yamtrack show item without creating one."""
        tmdb_id = row.get("tmdbId")
        tvdb_id = row.get("tvdbId")

        if tmdb_id:
            return Item.objects.filter(
                media_id=str(tmdb_id),
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
            ).first()

        if tvdb_id:
            return Item.objects.filter(
                media_id=str(tvdb_id),
                source=Sources.TVDB.value,
                media_type=MediaTypes.TV.value,
            ).first()

        return None

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
                if getattr(error, "status_code", None) == HTTP_STATUS_NOT_FOUND:
                    title = row.get("title") or row.get("sortTitle") or tmdb_id
                    self.warnings.append(
                        (
                            f"{title}: not found in {Sources.TMDB.label} "
                            f"with ID {tmdb_id}."
                        ),
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
            item, _ = retry_on_lock(
                lambda: Item.objects.update_or_create(
                    media_id=str(tmdb_id),
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.TV.value,
                    defaults=defaults,
                ),
            )
            return item

        if tvdb_id:
            return Item.objects.filter(
                media_id=str(tvdb_id),
                source=Sources.TVDB.value,
                media_type=MediaTypes.TV.value,
            ).first()

        return None

    def _sync_series_episode_collection(self, show_item, row):
        """Create episode-level Sonarr collection ownership for a show."""
        series_id = row.get("id")
        if series_id in (None, ""):
            upsert_collection_source_state(
                user=self.user,
                item=show_item,
                source="sonarr",
                quality_label="",
                source_updated_at=self._parse_source_timestamp(row),
            )
            return True

        try:
            episode_rows = self.client.episodes(series_id)
        except MediaImportError as error:
            title = row.get("title") or row.get("sortTitle") or show_item.title
            self.warnings.append(f"{title}: failed to fetch Sonarr episodes ({error}).")
            return False

        self._ensure_season_items(show_item, row, episode_rows)

        collected_episode_item_ids = set()
        for episode_row in episode_rows:
            episode_item = self._ensure_episode_item(show_item, episode_row)
            if not episode_item:
                continue

            if not self._episode_has_file(episode_row):
                continue

            upsert_collection_source_state(
                user=self.user,
                item=episode_item,
                source="sonarr",
                quality_label=self._extract_episode_quality_label(episode_row),
                source_updated_at=self._parse_source_timestamp(episode_row),
            )
            collected_episode_item_ids.add(episode_item.id)

        self._prune_series_collection_state(
            show_item,
            keep_episode_item_ids=collected_episode_item_ids,
        )
        return True

    def _ensure_season_items(self, show_item, row, episode_rows):
        """Ensure season items exist so collection stats can map episodes correctly."""
        season_numbers = {
            season.get("seasonNumber")
            for season in row.get("seasons") or []
            if season.get("seasonNumber") is not None
        }
        season_numbers.update(
            episode.get("seasonNumber")
            for episode in episode_rows
            if episode.get("seasonNumber") is not None
        )

        for season_number in season_numbers:
            season_title = (
                "Specials"
                if season_number == 0
                else f"{show_item.title} Season {season_number}"
            )

            def _get_or_create_season_item(
                season_number=season_number,
                season_title=season_title,
            ):
                return Item.objects.get_or_create(
                    media_id=show_item.media_id,
                    source=show_item.source,
                    media_type=MediaTypes.SEASON.value,
                    season_number=season_number,
                    defaults={
                        "title": season_title,
                        "original_title": season_title,
                        "localized_title": season_title,
                        "image": show_item.image or settings.IMG_NONE,
                        "release_datetime": show_item.release_datetime,
                        "genres": show_item.genres or [],
                    },
                )

            retry_on_lock(
                _get_or_create_season_item,
            )

    def _ensure_episode_item(self, show_item, row):
        """Create the episode item if it does not exist already."""
        season_number = row.get("seasonNumber")
        episode_number = row.get("episodeNumber")
        if season_number is None or episode_number is None:
            return None

        episode_item, _created = retry_on_lock(
            lambda: Item.objects.get_or_create(
                media_id=show_item.media_id,
                source=show_item.source,
                media_type=MediaTypes.EPISODE.value,
                season_number=season_number,
                episode_number=episode_number,
                defaults={
                    "title": row.get("title") or f"Episode {episode_number}",
                    "original_title": row.get("title") or f"Episode {episode_number}",
                    "localized_title": row.get("title") or f"Episode {episode_number}",
                    "image": show_item.image or settings.IMG_NONE,
                    "release_datetime": self._parse_episode_release_datetime(row),
                    "genres": show_item.genres or [],
                },
            ),
        )
        return episode_item

    def _episode_has_file(self, row):
        """Return True when Sonarr reports a local file for the episode."""
        if row.get("hasFile") is True:
            return True
        return int(row.get("episodeFileId") or 0) > 0

    def _extract_episode_quality_label(self, row):
        """Extract the episode file quality label when Sonarr includes it."""
        episode_file = row.get("episodeFile") or {}
        quality = episode_file.get("quality") or {}
        return (
            (quality.get("quality") or {}).get("name")
            or quality.get("name")
            or ""
        )

    def _prune_series_collection_state(self, show_item, *, keep_episode_item_ids=None):
        """Drop stale Sonarr collection ownership for a series."""
        if keep_episode_item_ids is None:
            keep_episode_item_ids = set()

        stale_episode_item_ids = list(
            Item.objects.filter(
                media_id=show_item.media_id,
                source=show_item.source,
                media_type=MediaTypes.EPISODE.value,
            )
            .exclude(id__in=keep_episode_item_ids)
            .filter(source_states__user=self.user, source_states__source="sonarr")
            .values_list("id", flat=True)
            .distinct()
        )
        for episode_item_id in stale_episode_item_ids:
            episode_item = Item.objects.get(id=episode_item_id)
            remove_collection_source_state(
                user=self.user,
                item=episode_item,
                source="sonarr",
            )

        if keep_episode_item_ids:
            self._retire_legacy_series_collection_entry(show_item)

        remove_collection_source_state(
            user=self.user,
            item=show_item,
            source="sonarr",
        )

    def _retire_legacy_series_collection_entry(self, show_item):
        """Delete the legacy show-level Sonarr row once episode rows exist."""
        if not show_item.source_states.filter(
            user=self.user,
            source="sonarr",
        ).exists():
            return

        legacy_entry_ids = list(
            CollectionEntry.objects.filter(
                user=self.user,
                item=show_item,
            )
            .order_by("-updated_at", "-collected_at", "-id")
            .values_list("id", flat=True)
        )
        if len(legacy_entry_ids) != 1:
            return

        retry_on_lock(
            lambda: CollectionEntry.objects.filter(id=legacy_entry_ids[0]).delete(),
        )

    def _parse_episode_release_datetime(self, row):
        return self._parse_source_timestamp(
            row,
            keys=("airDateUtc", "airDate"),
            default=None,
        )

    def _parse_source_timestamp(
        self,
        row,
        keys=("lastInfoSync", "added", "updated"),
        default=None,
    ):
        for key in keys:
            value = row.get(key)
            if not value:
                continue
            try:
                parsed = timezone.datetime.fromisoformat(
                    str(value).replace("Z", "+00:00"),
                )
            except ValueError:
                continue

            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(
                    parsed,
                    timezone.get_default_timezone(),
                )
            return parsed
        return timezone.now() if default is None else default
