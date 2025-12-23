"""Plex history importer."""

import logging
from collections import defaultdict

from django.conf import settings
from django.utils import timezone

from app.models import MediaTypes
from app.services.music import prefetch_album_covers
from integrations import plex as plex_api
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from integrations.webhooks.plex import PlexWebhookProcessor

logger = logging.getLogger(__name__)


def importer(library, user, mode):
    """Import Plex watch/listen history for the user."""
    account = getattr(user, "plex_account", None)
    if not account or not account.plex_token:
        raise MediaImportError("Plex is not connected for this user.")

    plex_importer = PlexHistoryImporter(
        user=user,
        account=account,
        mode=mode,
        library=library,
    )
    return plex_importer.import_data()


class PlexHistoryImporter:
    """Importer that replays Plex history through the webhook processor."""

    def __init__(self, user, account, mode, library, fast_mode=True):
        self.user = user
        self.account = account
        self.mode = mode
        self.library = library
        self.fast_mode = fast_mode
        self.processor = PlexWebhookProcessor()
        self.counts = defaultdict(int)
        self.warnings = []
        self.resources = []
        self._metadata_cache: dict[str, dict] = {}
        self.allowed_media_types: set[str] = set()
        self._artists_for_prefetch: set[int] = set()
        # Track unique music tracks (by item key) for counting purposes
        self._unique_music_tracks: set[tuple[str, str]] = set()

    def import_data(self):
        """Import history for the selected library."""
        self._ensure_username_matches()
        try:
            self.resources = plex_api.list_resources(self.account.plex_token)
        except plex_api.PlexAuthError as exc:
            raise MediaImportError("Plex token expired; reconnect and try again.") from exc

        sections = self._get_target_sections()
        if not sections:
            raise MediaImportError("No Plex libraries are available to import.")

        self.allowed_media_types = self._media_types_for_sections(sections)

        if self.mode == "overwrite":
            self._cleanup_existing_media(self.allowed_media_types)

        for section in sections:
            try:
                self._import_section(section)
            except MediaImportError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                msg = f"Unexpected error importing Plex section {section.get('title')}: {exc}"
                raise MediaImportUnexpectedError(msg) from exc

        self._prefetch_collected_album_covers()
        self._enqueue_fast_runtime_backfill()
        self._enqueue_music_enrichment()

        # Add unique track count for music if we have music imports
        result_counts = dict(self.counts)
        if MediaTypes.MUSIC.value in result_counts:
            result_counts["music_unique_tracks"] = len(self._unique_music_tracks)

        deduped_warnings = "\n".join(dict.fromkeys(self.warnings))
        return result_counts, deduped_warnings

    def _ensure_username_matches(self):
        """Persist the Plex username into the user's webhook allow list."""
        username = (self.account.plex_username or "").strip()
        if not username:
            return

        existing = [
            u.strip()
            for u in (self.user.plex_usernames or "").split(",")
            if u.strip()
        ]

        if username.lower() in [u.lower() for u in existing]:
            return

        updated = existing + [username]
        self.user.plex_usernames = ", ".join(updated)
        self.user.save(update_fields=["plex_usernames"])

    def _get_target_sections(self):
        """Return the sections the user selected or all if requested."""
        sections = self.account.sections or []
        if not sections:
            sections = plex_api.list_sections(self.account.plex_token)
            self.account.sections = sections
            self.account.sections_refreshed_at = timezone.now()
            self.account.save(update_fields=["sections", "sections_refreshed_at"])

        if self.library == "all":
            return sections

        try:
            machine_id, section_id = self.library.split("::", 1)
        except ValueError:
            raise MediaImportError("Invalid Plex library selection.")

        filtered = [
            section
            for section in sections
            if section.get("machine_identifier") == machine_id
            and str(section.get("id")) == str(section_id)
        ]

        if not filtered:
            raise MediaImportError("The selected Plex library is no longer available.")
        return filtered

    def _cleanup_existing_media(self, allowed_media_types: set[str]):
        """Delete existing media when running in overwrite mode."""
        existing_media = helpers.get_existing_media(self.user)
        to_delete = defaultdict(lambda: defaultdict(set))

        for media_type, sources in existing_media.items():
            if allowed_media_types and media_type not in allowed_media_types:
                continue

            for source, media_ids in sources.items():
                to_delete[media_type][source].update(media_ids.keys())

        helpers.cleanup_existing_media(to_delete, self.user)

    def _import_section(self, section: dict):
        """Fetch and ingest history for a single Plex section."""
        connections = self._connections_for_machine(section.get("machine_identifier"))
        if section.get("uri"):
            connections.insert(0, section.get("uri"))
        seen = []
        connections = [c for c in connections if c and not (c in seen or seen.append(c))]
        if not connections:
            raise MediaImportError(
                f"Could not find a Plex connection for {section.get('server_name') or 'server'}.",
            )

        entries, uri_used = self._fetch_history_entries(connections, section.get("id"))
        imported = 0

        for entry in entries:
            try:
                self._process_entry(entry, uri_used)
                imported += 1
            except MediaImportError as exc:
                self.warnings.append(str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to import Plex entry %s: %s", entry, exc)
                self.warnings.append(f"Failed to import a Plex entry: {exc}")

        logger.info(
            "Imported %s entries from Plex library %s",
            imported,
            section.get("title") or section.get("id"),
        )

    def _fetch_history_entries(self, connections: list[str], section_id: str | None) -> tuple[list[dict], str]:
        """Pull all history pages up front to minimize per-page overhead, trying fallbacks."""
        entries: list[dict] = []
        start = 0
        max_items = settings.PLEX_HISTORY_MAX_ITEMS
        if not max_items or max_items < 1:
            max_items = None  # No cap
        page_size = settings.PLEX_HISTORY_PAGE_SIZE
        failures = []
        uri_index = 0
        uri_used = ""

        while uri_index < len(connections):
            uri = connections[uri_index]
            try:
                while max_items is None or start < max_items:
                    page, total = plex_api.fetch_history(
                        self.account.plex_token,
                        uri,
                        section_id,
                        start,
                        size=page_size,
                    )

                    if not page:
                        break

                    entries.extend(page)
                    start += len(page)
                    if len(page) < page_size or start >= total:
                        break
                uri_used = uri
                break
            except plex_api.PlexAuthError as exc:
                raise MediaImportError("Plex token expired; reconnect and try again.") from exc
            except plex_api.PlexClientError as exc:
                failures.append((uri, str(exc)))
                uri_index += 1
                start = 0
                entries = []
                continue

        logger.info(
            "Fetched %s Plex history entries for section %s (requested up to %s)",
            len(entries),
            section_id or "all",
            max_items if max_items is not None else "no limit",
        )
        if not entries and failures:
            raise MediaImportUnexpectedError(
                f"Could not fetch Plex history after trying connections: {failures}",
            )
        if max_items is None:
            return entries, uri_used
        return entries[:max_items], uri_used

    def _connections_for_machine(self, machine_identifier):
        """Return the sorted connection URIs for a server."""
        uris: list[str] = []
        for resource in self.resources:
            if resource.get("machine_identifier") != machine_identifier:
                continue
            connections = resource.get("connections") or []
            sorted_conns = plex_api._sorted_connections(connections)
            uris.extend([c.get("uri") for c in sorted_conns if c.get("uri")])
        return uris

    def _media_types_for_sections(self, sections: list[dict]) -> set[str]:
        """Infer which media types should be affected based on selected sections."""
        mapped_types: set[str] = set()

        for section in sections:
            section_type = (section.get("type") or "").lower()
            if section_type in ("artist", "music"):
                mapped_types.add(MediaTypes.MUSIC.value)
            elif section_type == "movie":
                mapped_types.add(MediaTypes.MOVIE.value)
            elif section_type == "show":
                mapped_types.update(
                    {MediaTypes.TV.value, MediaTypes.SEASON.value, MediaTypes.EPISODE.value},
                )

        if not mapped_types:
            mapped_types.update(
                {
                    MediaTypes.MOVIE.value,
                    MediaTypes.TV.value,
                    MediaTypes.SEASON.value,
                    MediaTypes.EPISODE.value,
                    MediaTypes.MUSIC.value,
                },
            )

        return mapped_types

    def _process_entry(self, entry: dict, uri: str):
        """Process a single history entry."""
        metadata = self._build_metadata(entry)

        # Enrich metadata when GUIDs are missing (needed for ID resolution)
        if not metadata.get("Guid"):
            rating_key = metadata.get("ratingKey") or metadata.get("ratingkey")
            if not rating_key:
                raise MediaImportError(
                    f"Skipping Plex entry without a rating key: {metadata.get('title')}",
                )

            if rating_key in self._metadata_cache:
                details = self._metadata_cache[rating_key]
            elif self.fast_mode:
                details = None
            else:
                details = plex_api.fetch_metadata(self.account.plex_token, uri, rating_key)
                if details:
                    self._metadata_cache[rating_key] = details

            if details:
                metadata = {**metadata, **details}
                metadata["Guid"] = details.get("Guid") or metadata.get("Guid") or []

        metadata["Guid"] = self._normalize_guid_list(metadata.get("Guid"))

        if not metadata.get("Guid") and not metadata.get("guid"):
            if self.fast_mode:
                # Allow ingest without external IDs; will heal later during enrichment
                pass
            else:
                raise MediaImportError(
                    f"Skipping Plex entry without external IDs: {metadata.get('title')}",
                )

        payload = {
            "event": "media.scrobble",
            "Account": {"title": self.account.plex_username or self.user.username},
            "Metadata": metadata,
        }
        payload["_import_batch"] = True

        media_type = self.processor._get_media_type(payload)
        result = self.processor.process_payload(payload, self.user)

        # Track unique music tracks separately from play events
        if media_type == MediaTypes.MUSIC.value and result:
            # Music objects have an item attribute with media_id and source
            if hasattr(result, "item") and result.item:
                track_key = (result.item.media_id, result.item.source)
                if track_key not in self._unique_music_tracks:
                    self._unique_music_tracks.add(track_key)

        if (hasattr(result, "artist_id") and result.artist_id) or (result and hasattr(result, "artist_id") and result.artist_id):
            self._artists_for_prefetch.add(result.artist_id)

        if media_type:
            self.counts[media_type] += 1

    def _build_metadata(self, entry: dict) -> dict:
        """Normalize metadata shape expected by Plex webhook processor."""
        metadata = dict(entry)
        metadata.setdefault("Guid", entry.get("Guid") or [])

        # Standardize keys casing
        for key in list(metadata.keys()):
            lower_key = key[0].lower() + key[1:] if key and key[0].isupper() else key
            if lower_key not in metadata:
                metadata[lower_key] = metadata[key]

        # Fallback: some history rows come without ratingKey; use key if present
        if not metadata.get("ratingKey") and metadata.get("key"):
            metadata["ratingKey"] = metadata["key"]
            metadata["ratingkey"] = metadata["key"]

        # Ensure duration is set if available from nested Media block
        if not metadata.get("duration"):
            media_block = metadata.get("Media") or metadata.get("media")
            if isinstance(media_block, list) and media_block:
                dur = media_block[0].get("duration")
                if dur:
                    metadata["duration"] = dur
                elif media_block[0].get("Part"):
                    part = media_block[0]["Part"]
                    if isinstance(part, list) and part:
                        dur = part[0].get("duration")
                        if dur:
                            metadata["duration"] = dur

        # Cast numeric fields for consistency
        for key in ("parentIndex", "index", "duration", "viewedAt", "lastViewedAt"):
            if key in metadata and metadata[key] is not None:
                try:
                    metadata[key] = int(metadata[key])
                except (TypeError, ValueError):
                    pass

        return metadata

    def _normalize_guid_list(self, guid_list):
        """Ensure GUID payload is a list of dicts with id keys."""
        if not guid_list:
            return []

        normalized = []
        if isinstance(guid_list, dict):
            guid_list = [guid_list]

        for guid in guid_list:
            if isinstance(guid, dict):
                guid_id = guid.get("id") or guid.get("Id") or guid.get("guid")
                if guid_id:
                    normalized.append({"id": guid_id})
            elif isinstance(guid, str):
                normalized.append({"id": guid})

        return normalized

    def _enqueue_fast_runtime_backfill(self):
        """Kick off fast runtime backfill immediately after import for statistics."""
        from app.tasks import fast_runtime_backfill_task  # local import to avoid cycles

        if MediaTypes.MUSIC.value not in self.counts:
            return  # No music imported, skip

        try:
            fast_runtime_backfill_task.delay(self.user.id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not enqueue fast runtime backfill task: %s", exc)

    def _enqueue_music_enrichment(self):
        """Kick off a post-import enrichment/dedupe pass for this user's music."""
        from app.tasks import (  # local import to avoid cycles
            enrich_albums_task,
            enrich_music_library_task,
        )

        if MediaTypes.MUSIC.value not in self.counts:
            return  # No music imported, skip

        try:
            enrich_music_library_task.delay(self.user.id)
            # Schedule album enrichment to run after artist enrichment
            # This processes albums that don't have MBIDs (those that didn't match discography)
            enrich_albums_task.delay(self.user.id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not enqueue music enrichment task: %s", exc)

    def _prefetch_collected_album_covers(self):
        """Fetch missing album covers after the full import completes."""
        if not self._artists_for_prefetch:
            return
        from app.models import (
            Artist,  # local import to avoid circular import at module load
        )

        for artist_id in self._artists_for_prefetch:
            try:
                artist = Artist.objects.get(id=artist_id)
            except Artist.DoesNotExist:
                continue
            try:
                prefetch_album_covers(artist, limit=None)
            except Exception as exc:  # pragma: no cover - defensive network guard
                logger.debug("Cover prefetch failed for artist %s: %s", artist_id, exc)
