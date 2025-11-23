import json
import logging

import app
from app.models import MediaTypes

from .base import BaseWebhookProcessor

logger = logging.getLogger(__name__)


class PlexWebhookProcessor(BaseWebhookProcessor):
    """Processor for Plex webhook events."""

    def process_payload(self, payload, user):
        """Process the incoming Plex webhook payload."""
        logger.debug("Received Plex webhook payload: %s", json.dumps(payload, indent=2))

        event_type = payload.get("event")
        if not self._is_supported_event(payload.get("event")):
            logger.debug("Ignoring Plex webhook event type: %s", event_type)
            return

        payload_user = payload["Account"]["title"].strip().lower()
        if not self._is_valid_user(payload_user, user):
            logger.debug(
                "Ignoring Plex webhook event for user %s: not a valid user",
                payload_user,
            )
            return

        ids = self._extract_external_ids(payload)
        logger.info("Extracted IDs from payload: %s", ids)

        ids = self._resolve_ids_if_missing(payload, ids)
        if not any(ids.get(key) for key in ("tmdb_id", "imdb_id", "tvdb_id")):
            logger.warning("Ignoring Plex webhook call because no ID was found.")
            return

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
                payload
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

        if self._get_media_type(payload) == MediaTypes.TV.value:
            series_name = payload["Metadata"].get("grandparentTitle")
            season_number = payload["Metadata"].get("parentIndex")
            episode_number = payload["Metadata"].get("index")
            title = f"{series_name} S{season_number:02d}E{episode_number:02d}"

        elif self._get_media_type(payload) == MediaTypes.MOVIE.value:
            title = payload["Metadata"].get("title")

        return title

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
