import json
import logging
import re

from django.utils import timezone

import app
from app.models import MediaTypes
from app.providers import services
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

        ids = self.resolve_external_ids(payload)
        logger.info("Extracted IDs from payload: %s", ids)
        if not any(ids.get(key) for key in ("tmdb_id", "imdb_id", "tvdb_id")):
            logger.warning("Ignoring Plex webhook call because no ID was found.")
            return None

        self._process_media(payload, user, ids)

    def resolve_external_ids(self, payload, allow_title_search=True):
        """Extract external IDs, optionally allowing title search fallback."""
        ids = self._extract_external_ids(payload)
        if allow_title_search:
            ids = self._resolve_ids_if_missing(payload, ids)
        return ids

    def _resolve_ids_if_missing(self, payload, ids):
        """Attempt to resolve TMDB ID when it is missing from extracted IDs."""
        if ids.get("tmdb_id"):
            return ids

        media_type = self._get_media_type(payload)
        # Attempt TMDB 'find' if we have an external ID (TVDB or IMDB)
        external_id = ids.get("tvdb_id") or ids.get("imdb_id")
        if external_id and media_type in (MediaTypes.TV.value, MediaTypes.MOVIE.value):
            source = "tvdb_id" if ids.get("tvdb_id") else "imdb_id"
            try:
                from app.providers import tmdb
                find_results = tmdb.find(external_id, source=source)
                
                tmdb_id = None
                if media_type == MediaTypes.TV.value:
                    results = find_results.get("tv_results") or find_results.get("tv_episode_results") or []
                    if results:
                        tmdb_id = results[0].get("media_id")
                else:
                    results = find_results.get("movie_results") or []
                    if results:
                        tmdb_id = results[0].get("media_id")
                
                if tmdb_id:
                    ids["tmdb_id"] = str(tmdb_id)
                    logger.info("Resolved %s %s to TMDB ID %s using find API", source, external_id, tmdb_id)
                    return ids

                logger.debug("TMDB find returned no results for %s %s", source, external_id)
            except Exception as exc:
                logger.warning("TMDB find fallback failed for %s %s: %s", source, external_id, exc)

        # Fallback to title search for TV shows and Movies
        if media_type not in (MediaTypes.TV.value, MediaTypes.MOVIE.value):
            return ids

        metadata = payload.get("Metadata", {})
        # For episodes, use series title (grandparentTitle) falling back to episode title if needed
        # For movies, use the movie title
        search_title = (
            metadata.get("grandparentTitle")
            or metadata.get("title")
            if media_type == MediaTypes.TV.value
            else metadata.get("title")
        )
        original_date = metadata.get("originallyAvailableAt") or metadata.get("year")

        if not search_title:
            logger.debug("Cannot resolve plex:// GUID without title")
            return ids

        try:
            from app.providers import services
            search_results = services.search(
                media_type,
                search_title,
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
                # Use first_air_date for TV, release_date for movies
                date_key = (
                    "first_air_date"
                    if media_type == MediaTypes.TV.value
                    else "release_date"
                )
                result_date = result.get("details", {}).get(date_key) or ""
                if str(result_date).startswith(year):
                    tmdb_id = result.get("media_id")
                    break

        if not tmdb_id and results:
            tmdb_id = results[0].get("media_id")

        if tmdb_id:
            ids["tmdb_id"] = str(tmdb_id)
            logger.info(
                "Resolved plex:// GUID to TMDB ID %s using title search for '%s'",
                tmdb_id,
                search_title,
            )
        else:
            logger.debug("Title search returned no match for '%s'", search_title)

        return ids

    def _find_tv_media_id(self, ids, series_title=None, allow_title_fallback=False):
        """
        Resolve TMDB ID for a TV show using external IDs or title search.

        Returns:
            tuple: (media_id, season_number, episode_number)
                   season/episode are None unless extracted from specific lookup context,
                   checking primarily for show ID here.
        """
        # 1. Try resolving using existing IDs (TMDB, TVDB, IMDB)
        tmdb_id = ids.get("tmdb_id")
        if tmdb_id:
            return str(tmdb_id), None, None

        if not allow_title_fallback or not series_title:
            return None, None, None

        # 2. Try title search
        logger.debug("TV ID missing; attempting title search for '%s'", series_title)
        try:
            search_results = services.search(
                MediaTypes.TV.value,
                series_title,
                page=1,
            )
            results = search_results.get("results") or []
            if results:
                found_id = results[0].get("media_id")
                logger.info(
                    "Resolved '%s' to TMDB ID %s via title search",
                    series_title,
                    found_id,
                )
                return str(found_id), None, None
            
            logger.debug("Title search returned no results for '%s'", series_title)

            # Try stripping year from title like "Show (YYYY)"
            clean_title = re.sub(r'\s*\(\d{4}\)$', '', series_title)
            if clean_title != series_title:
                logger.debug("Retrying search with cleaned title '%s'", clean_title)
                search_results = services.search(
                    MediaTypes.TV.value,
                    clean_title,
                    page=1,
                )
                results = search_results.get("results") or []
                if results:
                    found_id = results[0].get("media_id")
                    logger.info(
                        "Resolved '%s' to TMDB ID %s via title search",
                        clean_title,
                        found_id,
                    )
                    return str(found_id), None, None

        except Exception as exc:
            logger.warning("Title search failed for '%s': %s", series_title, exc)

        return None, None, None


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
        metadata = payload.get("Metadata", {})
        guids = metadata.get("Guid", [])
        if not guids:
            single_guid = metadata.get("guid")
            if single_guid:
                guids = [{"id": single_guid}]

        ids = {
            "tmdb_id": None,
            "imdb_id": None,
            "tvdb_id": None,
            "plex_guid": None,
        }

        logger.debug("Extracting external IDs from %d GUIDs", len(guids))

        for guid in guids:
            guid_value = guid.get("id") if isinstance(guid, dict) else guid
            if not guid_value:
                continue

            guid_lower = guid_value.lower()

            if ids["plex_guid"] is None and guid_lower.startswith("plex://"):
                ids["plex_guid"] = guid_value.split("plex://", 1)[1]
                logger.debug("Found plex_guid: %s", ids["plex_guid"])

            # Priority 1: Explicitly labeled IMDB or 'tt' prefix anywhere
            if ids["imdb_id"] is None:
                imdb_id = self._extract_imdb_id(guid_value)
                if imdb_id:
                    ids["imdb_id"] = imdb_id
                    logger.debug("Found imdb_id: %s", imdb_id)
                    if "imdb" in guid_lower:
                        continue

            # Priority 2: TMDB
            if ids["tmdb_id"] is None and (
                "tmdb" in guid_lower or "themoviedb" in guid_lower
            ):
                tmdb_id = self._extract_numeric_guid_id(guid_value)
                if tmdb_id:
                    # If it looks like an IMDB ID (7+ digits) and we don't have an IMDB ID yet,
                    # AND it's a TV show, be skeptical of treating it as TMDB.
                    if int(tmdb_id) > 3000000 and ids["imdb_id"] is None:
                        ids["imdb_id"] = f"tt{tmdb_id}"
                        logger.debug("Skeptically treated large TMDB ID as IMDB: %s", ids["imdb_id"])
                    else:
                        ids["tmdb_id"] = tmdb_id
                        logger.debug("Found tmdb_id: %s", tmdb_id)

            # Priority 3: TVDB
            if ids["tvdb_id"] is None and ("tvdb" in guid_lower or "thetvdb" in guid_lower):
                tvdb_id = self._extract_numeric_guid_id(guid_value)
                if tvdb_id:
                    ids["tvdb_id"] = tvdb_id
                    logger.debug("Found tvdb_id: %s", tvdb_id)

            if all(ids.get(key) for key in ("tmdb_id", "imdb_id", "tvdb_id", "plex_guid")):
                break

        return ids

    def _extract_numeric_guid_id(self, guid_value):
        """Extract the first numeric identifier from a Plex GUID string."""
        cleaned = guid_value.split("?", 1)[0]
        if "://" in cleaned:
            cleaned = cleaned.split("://", 1)[1]
        cleaned = cleaned.lstrip("/")
        if "/" in cleaned:
            cleaned = cleaned.split("/", 1)[0]

        match = re.search(r"\d+", cleaned)
        return match.group(0) if match else None

    def _extract_imdb_id(self, guid_value):
        """Extract IMDB ID from a Plex GUID string."""
        match = re.search(r"tt\d+", guid_value)
        return match.group(0) if match else None

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
