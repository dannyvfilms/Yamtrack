import json
import logging
import re

from django.utils import timezone

import app
from app.models import MediaTypes
from app.services import music_scrobble

from .base import BaseWebhookProcessor

logger = logging.getLogger(__name__)


class PlexWebhookProcessor(BaseWebhookProcessor):
    """Processor for Plex webhook events."""

    MEDIA_TYPE_MAPPING = {
        **BaseWebhookProcessor.MEDIA_TYPE_MAPPING,
        "Track": MediaTypes.MUSIC.value,
    }

    def process_payload(self, payload, user):
        """Process the incoming Plex webhook payload."""
        logger.debug("Received Plex webhook payload: %s", json.dumps(payload, indent=2))

        event_type = payload.get("event")
        if not self._is_supported_event(payload.get("event")):
            logger.debug("Ignoring Plex webhook event type: %s", event_type)
            return None

        payload_user = payload["Account"]["title"].strip().lower()
        if not self._is_valid_user(payload_user, user):
            logger.debug(
                "Ignoring Plex webhook event for user %s: not a valid user",
                payload_user,
            )
            return None

        media_type = self._get_media_type(payload)
        if media_type == MediaTypes.MUSIC.value:
            if not getattr(user, "music_enabled", False):
                logger.debug(
                    "Ignoring Plex music webhook for user %s: music disabled",
                    payload_user,
                )
                return None

            music_event = self._build_music_event(payload, user)
            music_entry = music_scrobble.record_music_playback(music_event)
            if music_entry is None:
                logger.info(
                    "Processed Plex music %s for %s: %s - %s (no tracking yet; waiting for scrobble)",
                    "scrobble" if music_event.completed else "play",
                    payload_user,
                    music_event.track_title,
                    music_event.artist_name or "Unknown Artist",
                )
                return None
            logger.info(
                "Processed Plex music %s for %s: %s - %s (status=%s, progress=%s)",
                "scrobble" if music_event.completed else "play",
                payload_user,
                music_event.track_title,
                music_event.artist_name or "Unknown Artist",
                music_entry.status,
                music_entry.progress,
            )
            return music_entry

        ids = self._extract_external_ids(payload)
        logger.info("Extracted IDs from payload: %s", ids)

        ids = self._resolve_ids_if_missing(payload, ids)
        if not any(ids.get(key) for key in ("tmdb_id", "imdb_id", "tvdb_id")):
            logger.warning("Ignoring Plex webhook call because no ID was found.")
            return None

        self._process_media(payload, user, ids)

    def _resolve_ids_if_missing(self, payload, ids):
        """Attempt to resolve TMDB ID when only plex:// GUID is present."""
        if any(ids.get(key) for key in ("tmdb_id", "imdb_id", "tvdb_id")):
            return ids

        plex_guid = ids.get("plex_guid")
        if not plex_guid:
            return ids

        # Only handle TV for now; movies with plex:// GUID are not tracked today
        if self._get_media_type(payload) != MediaTypes.TV.value:
            return ids

        metadata = payload.get("Metadata", {})
        series_title = metadata.get("grandparentTitle")
        original_date = metadata.get("originallyAvailableAt")

        if not series_title:
            logger.debug("Cannot resolve plex:// GUID without series title")
            return ids

        try:
            search_results = app.providers.tmdb.search(
                MediaTypes.TV.value,
                series_title,
                page=1,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed TMDB search while resolving plex:// GUID")
            return ids

        tmdb_id = None
        results = search_results.get("results") or []

        if original_date:
            year = str(original_date).split("-")[0]
            for result in results:
                first_air_date = result.get("details", {}).get("first_air_date") or ""
                if str(first_air_date).startswith(year):
                    tmdb_id = result.get("media_id")
                    break

        if not tmdb_id and results:
            tmdb_id = results[0].get("media_id")

        if tmdb_id:
            ids["tmdb_id"] = tmdb_id
            logger.info(
                "Resolved plex:// GUID to TMDB ID %s using title search",
                tmdb_id,
            )

        return ids

    def _process_media(self, payload, user, ids):
        """Route processing based on media type, extracting season/episode for TV."""
        media_type = self._get_media_type(payload)
        if not media_type:
            logger.debug("Ignoring unsupported media type")
            return

        title = self._get_media_title(payload)
        logger.info("Received webhook for %s: %s", media_type, title)

        if media_type == MediaTypes.TV.value:
            # Extract season/episode from Plex payload
            season_number, episode_number = self._extract_season_episode_from_payload(
                payload,
            )
            self._process_tv(payload, user, ids, season_number, episode_number)
        elif media_type == MediaTypes.MOVIE.value:
            self._process_movie(payload, user, ids)

    def _is_supported_event(self, event_type):
        return event_type in ("media.scrobble", "media.play")

    def _is_valid_user(self, payload_user, user):
        stored_usernames = [
            u.strip().lower()
            for u in (user.plex_usernames or "").split(",")
            if u.strip()
        ]
        logger.debug(
            "Checking if payload user '%s' is in stored usernames: %s",
            payload_user,
            stored_usernames,
        )
        return payload_user in stored_usernames

    def _is_played(self, payload):
        return payload["event"] == "media.scrobble"

    def _get_media_type(self, payload):
        media_type = payload["Metadata"].get("type")
        if not media_type:
            return None

        return self.MEDIA_TYPE_MAPPING.get(media_type.title())

    def _get_media_title(self, payload):
        """Get media title from payload."""
        title = None

        media_type = self._get_media_type(payload)

        if media_type == MediaTypes.TV.value:
            series_name = payload["Metadata"].get("grandparentTitle")
            season_number = payload["Metadata"].get("parentIndex")
            episode_number = payload["Metadata"].get("index")
            title = f"{series_name} S{season_number:02d}E{episode_number:02d}"

        elif media_type == MediaTypes.MOVIE.value:
            title = payload["Metadata"].get("title")

        elif media_type == MediaTypes.MUSIC.value:
            metadata = payload.get("Metadata", {})
            artist = metadata.get("grandparentTitle")
            track = metadata.get("title")
            if artist and track:
                title = f"{artist} - {track}"
            else:
                title = track or artist

        return title

    def _extract_series_title(self, payload):
        """Extract TV series title from Plex payload."""
        if self._get_media_type(payload) == MediaTypes.TV.value:
            return payload.get("Metadata", {}).get("grandparentTitle")
        return None

    def _extract_external_ids(self, payload):
        guids = payload["Metadata"].get("Guid", [])
        if not guids:
            single_guid = payload["Metadata"].get("guid")
            if single_guid:
                guids = [{"id": single_guid}]

        def get_id(prefix):
            return next(
                (
                    guid["id"].replace(f"{prefix}://", "")
                    for guid in guids
                    if guid["id"].startswith(f"{prefix}://")
                ),
                None,
            )

        return {
            "tmdb_id": get_id("tmdb"),
            "imdb_id": get_id("imdb"),
            "tvdb_id": get_id("tvdb"),
            "plex_guid": get_id("plex"),
        }

    def _extract_music_ids(self, metadata):
        """Extract MusicBrainz IDs from a Plex track payload."""
        guids = metadata.get("Guid", [])
        if not guids:
            single_guid = metadata.get("guid")
            if single_guid:
                guids = [{"id": single_guid}]

        ids = {}
        for guid in guids:
            guid_value = guid.get("id") or ""
            guid_lower = guid_value.lower()
            uuid = self._extract_uuid(guid_value)

            if "musicbrainz" in guid_lower or "mbid" in guid_lower:
                if "recording" in guid_lower or "track" in guid_lower:
                    ids.setdefault("musicbrainz_recording", uuid or guid_value)
                elif "release-group" in guid_lower or "release_group" in guid_lower:
                    ids.setdefault("musicbrainz_release_group", uuid or guid_value)
                elif "release" in guid_lower or "album" in guid_lower:
                    ids.setdefault("musicbrainz_release", uuid or guid_value)
                elif "artist" in guid_lower:
                    ids.setdefault("musicbrainz_artist", uuid or guid_value)
                else:
                    ids.setdefault("musicbrainz_recording", uuid or guid_value)

        return ids

    def _extract_uuid(self, value):
        """Extract UUID from a string."""
        match = re.search(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            value,
        )
        return match.group(0) if match else None

    def _build_music_event(self, payload, user):
        """Build a normalized music playback event from Plex payload."""
        metadata = payload.get("Metadata", {}) or {}
        played_at = self._get_played_at(payload) or timezone.now().replace(
            second=0,
            microsecond=0,
        )
        duration_ms = metadata.get("duration")
        try:
            duration_ms = int(duration_ms) if duration_ms is not None else None
        except (TypeError, ValueError):
            duration_ms = None
        track_number = metadata.get("index")
        try:
            track_number = int(track_number) if track_number is not None else None
        except (TypeError, ValueError):
            track_number = None

        return music_scrobble.MusicPlaybackEvent(
            user=user,
            artist_name=metadata.get("grandparentTitle"),
            album_title=metadata.get("parentTitle"),
            track_title=metadata.get("title") or "Unknown Track",
            track_number=track_number,
            duration_ms=duration_ms,
            plex_rating_key=metadata.get("ratingKey"),
            external_ids=self._extract_music_ids(metadata),
            completed=payload.get("event") == "media.scrobble",
            played_at=played_at,
            defer_cover_prefetch=bool(payload.get("_import_batch")),
        )

    def _extract_season_episode_from_payload(self, payload):
        """Extract season and episode numbers from Plex payload."""
        metadata = payload.get("Metadata", {})
        season_number = metadata.get("parentIndex")
        episode_number = metadata.get("index")

        # Convert to int if they exist
        try:
            season_number = int(season_number) if season_number is not None else None
            episode_number = int(episode_number) if episode_number is not None else None
        except (ValueError, TypeError):
            return None, None

        return season_number, episode_number
