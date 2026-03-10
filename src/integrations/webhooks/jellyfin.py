import logging

from app import live_playback
from app.log_safety import mapping_keys, presence_map
from app.models import MediaTypes, Sources

from .base import BaseWebhookProcessor

logger = logging.getLogger(__name__)

JELLYFIN_EVENT_MAP = {
    "Play": "media.play",
    "Pause": "media.pause",
    "Stop": "media.stop",
}


def _ticks_to_seconds(ticks) -> int | None:
    """Convert Jellyfin 100-nanosecond ticks to whole seconds."""
    if ticks is None:
        return None
    try:
        return max(0, int(ticks) // 10_000_000)
    except (TypeError, ValueError):
        return None


class JellyfinWebhookProcessor(BaseWebhookProcessor):
    """Processor for Jellyfin webhook events."""

    def process_payload(self, payload, user):
        """Process the incoming Jellyfin webhook payload."""
        logger.debug(
            "Processing Jellyfin webhook payload keys=%s item_keys=%s",
            mapping_keys(payload),
            mapping_keys(payload.get("Item")),
        )

        event_type = payload.get("Event")
        if not self._is_supported_event(event_type):
            logger.debug("Ignoring Jellyfin webhook event type: %s", event_type)
            return

        ids = self._extract_external_ids(payload)
        logger.info(
            "Extracted Jellyfin ID presence from payload: %s",
            presence_map(ids, ("tmdb_id", "imdb_id", "tvdb_id")),
        )

        # Update live playback state (before media tracking)
        playback_media_type = self._get_live_playback_media_type(payload)
        self._update_live_playback_state(
            payload, user, ids, playback_media_type,
        )

        # Pause events only update the card — no media tracking
        if event_type == "Pause":
            return

        if not any(ids.values()):
            logger.warning(
                "Ignoring Jellyfin webhook call because no ID was found.",
            )
            return

        self._process_media(payload, user, ids)

    def _is_supported_event(self, event_type):
        return event_type in ("Play", "Pause", "Stop")

    def _is_played(self, payload):
        return payload["Item"]["UserData"]["Played"]

    def _get_media_type(self, payload):
        return self.MEDIA_TYPE_MAPPING.get(payload["Item"].get("Type"))

    def _get_media_title(self, payload):
        """Get media title from payload."""
        title = None

        if self._get_media_type(payload) == MediaTypes.TV.value:
            series_name = payload["Item"].get("SeriesName")
            season_number = payload["Item"].get("ParentIndexNumber")
            episode_number = payload["Item"].get("IndexNumber")
            title = f"{series_name} S{season_number:02d}E{episode_number:02d}"

        elif self._get_media_type(payload) == MediaTypes.MOVIE.value:
            movie_name = payload["Item"].get("Name")
            year = payload["Item"].get("ProductionYear")

            title = f"{movie_name} ({year})" if movie_name and year else movie_name

        return title

    def _extract_external_ids(self, payload):
        provider_ids = payload["Item"].get("ProviderIds", {})
        return {
            "tmdb_id": provider_ids.get("Tmdb"),
            "imdb_id": provider_ids.get("Imdb"),
            "tvdb_id": provider_ids.get("Tvdb"),
        }

    def _extract_season_episode_from_payload(self, payload):
        """Extract season and episode numbers from Jellyfin payload."""
        item = payload.get("Item", {})
        season_number = item.get("ParentIndexNumber")
        episode_number = item.get("IndexNumber")

        # Convert to int if they exist
        try:
            season_number = int(season_number) if season_number is not None else None
            episode_number = int(episode_number) if episode_number is not None else None
        except (ValueError, TypeError):
            return None, None

        return season_number, episode_number

    def _extract_series_title(self, payload):
        """Extract TV series title from Jellyfin payload."""
        if self._get_media_type(payload) == MediaTypes.TV.value:
            return payload.get("Item", {}).get("SeriesName")
        return None

    # -- Live playback --------------------------------------------------

    def _get_live_playback_media_type(self, payload):
        """Map Jellyfin item type to a playback card media type."""
        item_type = (
            payload.get("Item", {}).get("Type") or ""
        ).strip()
        if item_type == "Episode":
            return MediaTypes.EPISODE.value
        if item_type == "Movie":
            return MediaTypes.MOVIE.value
        return None

    def _update_live_playback_state(  # noqa: C901
        self, payload, user, ids, playback_media_type,
    ):
        """Update cache-backed live playback state for home-page UI."""
        event_type = JELLYFIN_EVENT_MAP.get(payload.get("Event"))
        if not event_type:
            return

        if playback_media_type not in (
            MediaTypes.MOVIE.value,
            MediaTypes.EPISODE.value,
        ):
            return

        item = payload.get("Item", {})
        media_id = None
        season_number = None
        episode_number = None

        if playback_media_type == MediaTypes.MOVIE.value:
            media_id = ids.get("tmdb_id")
        else:
            season_number, episode_number = (
                self._extract_season_episode_from_payload(payload)
            )
            # Prefer TVDB/IMDB resolution — they reliably return the
            # show-level TMDB ID via the TMDB find API.
            if ids.get("tvdb_id") or ids.get("imdb_id"):
                alt_ids = dict(ids)
                alt_ids["tmdb_id"] = None
                resolved_id, _, _ = super()._find_tv_media_id(alt_ids)
                if resolved_id:
                    media_id = str(resolved_id)

            # Fallback: title search then raw tmdb_id
            if media_id is None:
                series_title = self._extract_series_title(payload)
                resolved_id, _, _ = self._find_tv_media_id(
                    ids,
                    series_title=series_title,
                    allow_title_fallback=True,
                )
                if resolved_id:
                    media_id = str(resolved_id)

            if media_id is None:
                media_id = ids.get("tmdb_id")

        # Duration / offset from Jellyfin ticks (100 ns units)
        duration_seconds = _ticks_to_seconds(item.get("RunTimeTicks"))
        offset_seconds = _ticks_to_seconds(
            payload.get("PlaybackPositionTicks")
            or item.get("PlaybackPositionTicks"),
        )

        live_playback.apply_playback_event(
            user_id=user.id,
            event_type=event_type,
            playback_media_type=playback_media_type,
            media_id=media_id,
            source=Sources.TMDB.value,
            rating_key=str(item.get("Id") or "").strip() or None,
            title=item.get("Name"),
            series_title=item.get("SeriesName"),
            episode_title=(
                item.get("Name")
                if playback_media_type == MediaTypes.EPISODE.value
                else None
            ),
            season_number=season_number,
            episode_number=episode_number,
            view_offset_seconds=offset_seconds,
            duration_seconds=duration_seconds,
        )
