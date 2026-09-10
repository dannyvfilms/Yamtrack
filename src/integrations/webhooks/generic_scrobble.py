"""Durable-write processor for the generic /api/v1/scrobble/ endpoint.

Unlike vendor webhook processors, third-party API clients already send
normalized ids and media type — there is no vendor payload to parse, so
this processor only needs to bridge that normalized shape into the shared
TMDB-resolution and DB-write logic in ``BaseWebhookProcessor``.
"""

import logging

import app.providers.mal
from app.live_playback import (
    PLAYBACK_SCROBBLE_BUFFER_SECONDS,
    PLAYBACK_SCROBBLE_FALLBACK_SECONDS,
)
from app.models import MediaTypes, Sources

from . import anime_mappings
from .base import BaseWebhookProcessor

logger = logging.getLogger(__name__)

# The API speaks "movie"/"episode" (matching apply_playback_event's
# playback_media_type vocabulary); the base processor routes on TV/MOVIE.
_MEDIA_TYPE_MAPPING = {
    "movie": MediaTypes.MOVIE.value,
    "episode": MediaTypes.TV.value,
}


def is_played(payload):
    """Completed if the client says so, else via position/duration.

    Reuses the same buffer/fallback heuristic the live playback card
    already uses to guess completion, so "played" detection is consistent
    across the scrobble, live-card and playback-progress surfaces.
    """
    completed = payload.get("completed")
    if completed is not None:
        return bool(completed)

    position = payload.get("position_seconds")
    if position is None:
        return False

    duration = payload.get("duration_seconds")
    if duration:
        return position >= duration - PLAYBACK_SCROBBLE_BUFFER_SECONDS
    return position >= PLAYBACK_SCROBBLE_FALLBACK_SECONDS


class GenericScrobbleProcessor(BaseWebhookProcessor):
    """Processor for normalized scrobble-stop events from the API."""

    def process_payload(self, payload, user):
        """Resolve and persist a stop/completion event from the API."""
        ids = self._extract_external_ids(payload)
        if not any(ids.values()):
            return
        self._process_media(payload, user, ids)

    def _is_supported_event(self, event_type):
        return True

    def _is_played(self, payload):
        return is_played(payload)

    def _get_media_type(self, payload):
        return _MEDIA_TYPE_MAPPING.get(payload.get("media_type"))

    def _get_media_title(self, payload):
        return payload.get("title") or payload.get("series_title")

    def _extract_external_ids(self, payload):
        # `anidb` is optional and rides alongside the required franchise id.
        # It lets `BaseWebhookProcessor._process_tv` resolve the exact MAL cour
        # directly, the same way the Plex/HAMA path already does, instead of
        # inferring one from a TVDB season and episode number.
        ids = payload.get("ids") or {}
        return {
            "tmdb_id": ids.get("tmdb"),
            "imdb_id": ids.get("imdb"),
            "tvdb_id": ids.get("tvdb"),
            "anidb_id": ids.get("anidb"),
        }

    def _extract_season_episode_from_payload(self, payload):
        return payload.get("season_number"), payload.get("episode_number")

    def _extract_series_title(self, payload):
        return payload.get("series_title")

    def _get_played_at(self, payload):
        return payload.get("played_at")

    def resolve_anime_live_identity(self, user, ids, season_number, episode_number):
        """Pin the live Now Playing card to the anime an AniDB id names.

        The durable stop path already routes an ``anidb`` id to its exact
        MyAnimeList cour when the user's Anime library stores flat MAL rows
        (see ``BaseWebhookProcessor._process_tv``). The live card never did:
        it resolved the payload's tmdb/tvdb id straight through, so a
        start/pause event showed the umbrella series - the "unresolved"
        anime - while the same client's stop marked the right cour as
        played.

        This is a read-only mirror of that decision. It returns ``None`` -
        leaving the default resolution untouched - unless a flat MAL cour is
        the shape this show resolves to. The grouped-anime case already
        renders from the payload's franchise id and is deliberately left
        alone.
        """
        from app.services import metadata_resolution

        anidb_id = ids.get("anidb_id")
        if not (getattr(user, "anime_enabled", False) and anidb_id and episode_number):
            return None

        try:
            mapping_data = anime_mappings.fetch_mapping_data()
            mal_id, mal_episode_number = anime_mappings.get_mal_id_from_anidb(
                mapping_data,
                anidb_id,
                episode_number,
            )
        except Exception:
            logger.warning("Live card AniDB resolution failed", exc_info=True)
            return None
        if not mal_id:
            return None

        tmdb_id = ids.get("tmdb_id")
        tvdb_id = ids.get("tvdb_id")
        if not (tmdb_id or tvdb_id):
            entry = next(
                (
                    e
                    for e in anime_mappings.find_entries_for_mal_id(mapping_data, mal_id)
                    if e.get("tvdb_id") or e.get("tmdb_id")
                ),
                None,
            )
            if entry:
                tmdb_id = entry.get("tmdb_id")
                tvdb_id = entry.get("tvdb_id")

        home = metadata_resolution.find_existing_anime_home(
            user,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
        )
        home_kind = home[0] if home else None
        defer_to_grouped = home_kind == "grouped" or (
            home_kind is None and metadata_resolution.prefers_grouped_anime(user)
        )
        if defer_to_grouped:
            return None

        try:
            anime_metadata = app.providers.mal.anime(mal_id)
        except Exception:
            logger.warning("Live card MAL lookup failed for %s", mal_id, exc_info=True)
            return None

        max_progress = anime_metadata.get("max_progress")
        if (
            isinstance(max_progress, int)
            and max_progress > 0
            and mal_episode_number > max_progress
        ):
            # Past this cour's end - the stop path refuses it too, so do not
            # pin the card to an entry that will not accept the scrobble.
            return None

        return {
            "source": Sources.MAL.value,
            "media_id": str(mal_id),
            "season_number": None,
            "episode_number": mal_episode_number,
            "series_title": anime_metadata.get("title"),
            "image": anime_metadata.get("image"),
        }
