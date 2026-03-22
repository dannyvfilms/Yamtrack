"""Processor for Last.fm scrobbles."""

import logging
from datetime import UTC, datetime, timedelta

from django.utils import timezone

from app.models import MediaTypes, Music
from app.services import music_scrobble

from integrations import lastfm_api

logger = logging.getLogger(__name__)


class LastFMScrobbleProcessor:
    """Processor for Last.fm scrobble data."""

    def process_track(self, track_data: dict, user, *, fast_mode: bool = False) -> music_scrobble.Music | None:
        """Process a single Last.fm track and record it as a scrobble.

        Args:
            track_data: Track data from Last.fm API
            user: User instance

        Returns:
            Music instance if created/updated, None otherwise
        """
        # Filter out "now playing" items
        attr = track_data.get("@attr", {})
        if attr.get("nowplaying") == "true":
            logger.debug("Skipping now playing track: %s", track_data.get("name"))
            return None

        # Must have date.uts to be a valid scrobble
        date_attr = track_data.get("date", {})
        date_uts = date_attr.get("uts")
        if not date_uts:
            logger.debug("Skipping track without date.uts: %s", track_data.get("name"))
            return None

        # Extract basic track info
        track_title = track_data.get("name", "Unknown Track")
        artist_data = track_data.get("artist", {})
        artist_name = artist_data.get("#text") or artist_data.get("name") or "Unknown Artist"
        album_data = track_data.get("album", {})
        album_title = album_data.get("#text") or album_data.get("name") or "Unknown Album"

        # Extract MBIDs if present
        external_ids = {}
        artist_mbid = artist_data.get("mbid")
        if artist_mbid:
            external_ids["musicbrainz_artist"] = artist_mbid

        track_mbid = track_data.get("mbid")
        if track_mbid:
            external_ids["musicbrainz_recording"] = track_mbid

        album_mbid = album_data.get("mbid")
        if album_mbid:
            # Last.fm doesn't distinguish between release and release-group
            # Try release first, fallback handled by music_scrobble service
            external_ids["musicbrainz_release"] = album_mbid

        # Convert Unix timestamp to timezone-aware datetime
        try:
            played_at_uts = int(date_uts)
            played_at = datetime.fromtimestamp(played_at_uts, tz=UTC)
            played_at = timezone.localtime(played_at)
        except (ValueError, TypeError, OSError) as e:
            logger.warning("Invalid date.uts for track %s: %s", track_title, e)
            return None

        # Check for exact duplicate before processing
        if self._is_duplicate(user, played_at_uts, artist_name, track_title, album_title):
            logger.debug(
                "Skipping duplicate scrobble: %s - %s (%s)",
                artist_name,
                track_title,
                played_at_uts,
            )
            return None

        # Build MusicPlaybackEvent
        event = music_scrobble.MusicPlaybackEvent(
            user=user,
            track_title=track_title,
            artist_name=artist_name,
            album_title=album_title if album_title != "Unknown Album" else None,
            track_number=None,  # Last.fm doesn't provide track numbers
            duration_ms=None,  # Last.fm doesn't provide duration in scrobbles
            plex_rating_key=None,
            external_ids=external_ids,
            completed=True,  # All Last.fm scrobbles are completed
            played_at=played_at,
            defer_cover_prefetch=fast_mode,
        )

        # Record the scrobble
        try:
            music_entry = music_scrobble.record_music_playback(event)
            if music_entry:
                logger.info(
                    "Processed Last.fm scrobble for %s: %s - %s (status=%s, progress=%s)",
                    user.username,
                    artist_name,
                    track_title,
                    music_entry.status,
                    music_entry.progress,
                )
            return music_entry
        except Exception as e:
            logger.error(
                "Error processing Last.fm scrobble for %s: %s - %s: %s",
                user.username,
                artist_name,
                track_title,
                e,
                exc_info=True,
            )
            return None

    def _is_duplicate(
        self,
        user,
        played_at_uts: int,
        artist_name: str,
        track_title: str,
        album_title: str,
    ) -> bool:
        """Check if this scrobble is an exact duplicate.

        Uses exact match on (user, played_at_uts, artist, track, album) to prevent
        duplicates from pagination overlap or API inconsistencies.

        Args:
            user: User instance
            played_at_uts: Unix timestamp in seconds
            artist_name: Artist name
            track_title: Track title
            album_title: Album title

        Returns:
            True if duplicate found, False otherwise
        """
        # Convert Unix timestamp to datetime for comparison
        try:
            played_at = datetime.fromtimestamp(played_at_uts, tz=UTC)
            played_at = timezone.localtime(played_at)
        except (ValueError, TypeError, OSError):
            return False

        # Find existing Music entries with same end_date (within 1 second tolerance for timezone conversion)
        # and matching artist/track/album
        existing = Music.objects.filter(
            user=user,
            end_date__gte=played_at - timedelta(seconds=1),
            end_date__lte=played_at + timedelta(seconds=1),
        ).select_related("artist", "album", "track")

        for music in existing:
            # Check exact match on all fields
            if (
                music.artist
                and music.artist.name == artist_name
                and music.track
                and music.track.title == track_title
            ):
                # Album match (can be None)
                music_album = music.album.title if music.album else None
                if music_album == album_title or (not music_album and not album_title):
                    return True

        return False

    def process_tracks(self, tracks: list[dict], user, *, fast_mode: bool = False) -> dict[str, int | set]:
        """Process multiple Last.fm tracks.

        Args:
            tracks: List of track dictionaries from Last.fm API
            user: User instance

        Returns:
            Dict with counts: processed, skipped, errors, and affected_day_keys (set)
        """
        from app import history_cache

        stats = {"processed": 0, "skipped": 0, "errors": 0, "affected_day_keys": set()}

        for track_data in tracks:
            try:
                result = self.process_track(track_data, user, fast_mode=fast_mode)
                if result is None:
                    stats["skipped"] += 1
                else:
                    stats["processed"] += 1
                    # Collect day_key for cache refresh
                    if result.end_date:
                        day_key = history_cache.history_day_key(result.end_date)
                        if day_key:
                            stats["affected_day_keys"].add(day_key)
            except Exception as e:
                logger.error("Error processing Last.fm track: %s", e, exc_info=True)
                stats["errors"] += 1

        return stats
