# Original implementation by sboddy — FuzzyGrim/Yamtrack PR #1506
import logging
from enum import StrEnum

from app.models import MediaTypes

from .base import BaseWebhookProcessor

logger = logging.getLogger(__name__)

PERCENT_COMPLETE_THRESHOLD = 80


class KodiEvent(StrEnum):
    PLAYBACK_START = "start"
    PLAYBACK_STOP = "stop"
    PLAYBACK_END = "end"


class KodiWebhookProcessor(BaseWebhookProcessor):
    """Processor for Kodi webhook events via the HTTP Scrobbler add-on."""

    def process_payload(self, payload, user):
        event_type = payload.get("event")
        if not self._is_supported_event(event_type):
            logger.debug("Ignoring Kodi webhook event type: %s", event_type)
            return

        ids = self._extract_external_ids(payload)
        logger.info("Extracted IDs from Kodi payload: %s", ids)

        if not any(ids.values()):
            logger.warning("Ignoring Kodi webhook: no external ID found in payload.")
            return

        self._process_media(payload, user, ids)

    def _is_supported_event(self, event_type):
        return event_type in {
            KodiEvent.PLAYBACK_START,
            KodiEvent.PLAYBACK_STOP,
            KodiEvent.PLAYBACK_END,
        }

    def _is_played(self, payload):
        if payload.get("event") == KodiEvent.PLAYBACK_END:
            return True
        if payload.get("event") == KodiEvent.PLAYBACK_STOP:
            percent = payload.get("progress", {}).get("percent", 0)
            if percent and percent > PERCENT_COMPLETE_THRESHOLD:
                return True
        return False

    def _get_media_type(self, payload):
        return self.MEDIA_TYPE_MAPPING.get(payload.get("mediaType", "").capitalize())

    def _get_media_title(self, payload):
        if self._get_media_type(payload) == MediaTypes.TV.value:
            series_name = payload.get("tvShowTitle")
            season_number = payload.get("season")
            episode_number = payload.get("episode")
            return f"{series_name} S{season_number:02d}E{episode_number:02d}"

        if self._get_media_type(payload) == MediaTypes.MOVIE.value:
            movie_name = payload.get("title")
            year = payload.get("year")
            return f"{movie_name} ({year})" if movie_name and year else movie_name

        return None

    def _extract_season_episode_from_payload(self, payload):
        return payload.get("season"), payload.get("episode")

    def _extract_series_title(self, payload):
        return payload.get("tvShowTitle")

    def _extract_external_ids(self, payload):
        provider_ids = payload.get("uniqueIds", {})
        return {
            "tmdb_id": provider_ids.get("tmdb"),
            "imdb_id": provider_ids.get("imdb"),
            "tvdb_id": provider_ids.get("tvdb"),
        }
