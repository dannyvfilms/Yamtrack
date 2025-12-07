"""Music-related service functions for discography sync."""

import logging
import re
from datetime import datetime

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_date

from app.models import Album, Artist, Track

logger = logging.getLogger(__name__)


def get_artist_hero_image(artist: Artist) -> str:
    """Get a hero image for an artist from their albums.
    
    Since MusicBrainz doesn't have artist photos, we derive a hero image
    from the artist's albums - preferring albums with cover art.
    
    Strategy:
    1. Find albums with images, prefer earliest release (often most iconic)
    2. If no albums have images, return the default placeholder
    
    Args:
        artist: The Artist object
        
    Returns:
        URL to the hero image, or settings.IMG_NONE
    """
    # Get all albums for this artist that have images
    albums_with_images = Album.objects.filter(
        artist=artist,
    ).exclude(
        image=""
    ).exclude(
        image=settings.IMG_NONE
    ).order_by("release_date")
    
    if albums_with_images.exists():
        # Return the earliest album's image (often the most iconic)
        return albums_with_images.first().image
    
    return settings.IMG_NONE


def _norm_name(val: str) -> str:
    """Normalize names for matching (strip punctuation/whitespace, lowercase)."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (val or "")).strip()).lower()


def resolve_artist_mbid(name: str, sort_name: str | None = None):
    """Resolve an artist MBID using the same heuristics as the app search.

    Returns (mbid, candidate_count, matched_variant) or (None, 0, None).
    """
    if not (name or sort_name):
        return None, 0, None

    from app.providers import musicbrainz

    variants = []
    base_names = {name} if name else set()
    if sort_name:
        base_names.add(sort_name)
    for base in base_names:
        variants.append(base)
        # Slash/hyphen/space swaps
        variants.append(base.replace("/", " "))
        variants.append(base.replace("/", "-"))
        variants.append(base.replace("-", " "))
        # Punctuation-stripped
        variants.append(re.sub(r"[^\w\s]", " ", base))
        # Quoted exact search
        variants.append(f"\"{base}\"")

    seen = set()
    for variant in variants:
        variant = variant.strip()
        if not variant or variant in seen:
            continue
        seen.add(variant)

        try:
            resp = musicbrainz.search_artists(variant, page=1)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("resolve_artist_mbid: search failed for '%s': %s", variant, exc)
            continue

        candidates = (resp or {}).get("artists") or (resp or {}).get("results") or []
        logger.debug(
            "resolve_artist_mbid: variant '%s' returned %d candidates",
            variant,
            len(candidates),
        )
        if not candidates:
            continue

        target_norm = _norm_name(variant)
        chosen = None
        for cand in candidates:
            cid = cand.get("id")
            cname = cand.get("name") or ""
            if cid and _norm_name(cname) == target_norm:
                chosen = cid
                break
        if not chosen:
            chosen = next((c.get("id") for c in candidates if c.get("id")), None)

        if chosen:
            return chosen, len(candidates), variant

    return None, 0, None


def refresh_album_cover_art(album: Album) -> bool:
    """Try to fetch/refresh cover art for an album.
    
    Args:
        album: The Album object to refresh
        
    Returns:
        True if cover art was updated, False otherwise
    """
    from app.providers import musicbrainz
    
    # Only try if we have IDs to look up
    if not album.musicbrainz_release_id and not album.musicbrainz_release_group_id:
        return False
    
    # Skip if album already has good cover art
    if album.image and album.image != settings.IMG_NONE:
        return False
    
    try:
        new_image = musicbrainz.get_cover_art(
            release_id=album.musicbrainz_release_id,
            release_group_id=album.musicbrainz_release_group_id,
        )
        
        if new_image and new_image != settings.IMG_NONE:
            album.image = new_image
            album.save(update_fields=["image"])
            logger.info("Updated cover art for album %s", album.title)
            return True
            
    except Exception as e:
        logger.debug("Failed to fetch cover art for album %s: %s", album.title, e)
    
    return False


def refresh_missing_album_covers(artist: Artist, limit: int = 10) -> int:
    """Refresh cover art for albums missing images.
    
    Args:
        artist: The Artist whose albums to check
        limit: Maximum number of albums to refresh (to avoid rate limiting)
        
    Returns:
        Number of albums that got new cover art
    """
    albums_without_images = Album.objects.filter(
        artist=artist,
    ).filter(
        models.Q(image="") | models.Q(image=settings.IMG_NONE)
    )[:limit]
    
    refreshed = 0
    for album in albums_without_images:
        if refresh_album_cover_art(album):
            refreshed += 1
    
    return refreshed


def sync_artist_discography(artist: Artist, force: bool = False) -> int:
    """Sync the discography for an artist from MusicBrainz.
    
    This creates/updates Album records for all albums in the artist's
    discography, similar to how TV seasons are populated from TMDB.
    
    Args:
        artist: The Artist object to sync
        force: If True, sync even if already synced recently
        
    Returns:
        Number of albums synced
    """
    from app.providers import musicbrainz
    
    # Ensure artist is saved before using it in queries
    if not artist.pk:
        artist.save()
    
    # Skip if no MusicBrainz ID
    if not artist.musicbrainz_id:
        logger.debug("Artist %s has no MusicBrainz ID, skipping discography sync", artist.name)
        return 0
    
    # Skip if already synced recently (within 7 days) unless forced
    if not force and artist.discography_synced_at:
        days_since_sync = (timezone.now() - artist.discography_synced_at).days
        if days_since_sync < 7:
            logger.debug(
                "Artist %s discography synced %d days ago, skipping",
                artist.name,
                days_since_sync,
            )
            return 0
    
    try:
        # Skip cover art fetching during sync - covers are loaded async via HTMX
        discography = musicbrainz.get_artist_discography(artist.musicbrainz_id, skip_cover_art=True)
        
        synced_count = 0
        for album_data in discography:
            release_group_id = album_data.get("release_group_id")
            if not release_group_id:
                continue
            
            # Parse release date
            release_date = None
            date_str = album_data.get("release_date", "")
            if date_str:
                try:
                    if len(date_str) >= 10:
                        release_date = parse_date(date_str[:10])
                    elif len(date_str) == 7:
                        release_date = parse_date(date_str + "-01")
                    elif len(date_str) == 4:
                        release_date = parse_date(date_str + "-01-01")
                except (ValueError, TypeError):
                    pass
            
            # Update or create the album
            album, created = Album.objects.update_or_create(
                artist=artist,
                musicbrainz_release_group_id=release_group_id,
                defaults={
                    "title": album_data.get("title", "Unknown Album"),
                    "musicbrainz_release_id": album_data.get("release_id"),
                    "release_date": release_date,
                    "image": album_data.get("image", ""),
                    "release_type": album_data.get("release_type", ""),
                },
            )
            
            if created:
                logger.debug("Created album: %s", album.title)
            else:
                logger.debug("Updated album: %s", album.title)
            
            synced_count += 1
        
        # Update sync timestamp
        artist.discography_synced_at = timezone.now()
        artist.save(update_fields=["discography_synced_at"])
        
        logger.info(
            "Synced %d albums for artist %s from MusicBrainz",
            synced_count,
            artist.name,
        )
        return synced_count
        
    except Exception as e:
        logger.exception("Failed to sync discography for artist %s: %s", artist.name, e)
        return 0


def needs_discography_sync(artist: Artist, max_age_days: int = 7) -> bool:
    """Check if an artist needs discography sync.
    
    Args:
        artist: The Artist object to check
        max_age_days: Maximum age of sync before it's considered stale
        
    Returns:
        True if sync is needed
    """
    if not artist.musicbrainz_id:
        return False
    
    if not artist.discography_synced_at:
        return True
    
    days_since_sync = (timezone.now() - artist.discography_synced_at).days
    return days_since_sync >= max_age_days


def ensure_album_has_release_id(album: Album) -> bool:
    """Ensure an album has a release_id, fetching it from release_group if needed.
    
    If the album only has a release_group_id, this will query MusicBrainz to find
    a representative release and update the album.
    
    Args:
        album: The Album object
        
    Returns:
        True if the album now has a release_id (or already had one)
    """
    from app.providers import musicbrainz
    
    # Already has a release_id
    if album.musicbrainz_release_id:
        return True
    
    # No release_group_id either - truly no MusicBrainz identity
    if not album.musicbrainz_release_group_id:
        return False
    
    # Try to get a release from the release group
    try:
        release_id = musicbrainz.get_release_for_group(album.musicbrainz_release_group_id)
        if release_id:
            album.musicbrainz_release_id = release_id
            album.save(update_fields=["musicbrainz_release_id"])
            logger.info("Found release_id %s for album %s", release_id, album.title)
            return True
    except Exception as e:
        logger.debug("Failed to get release_id for album %s: %s", album.title, e)
    
    return False


def album_has_musicbrainz_id(album: Album) -> bool:
    """Check if an album has any MusicBrainz identity.
    
    Returns True if the album has either a release_id or release_group_id.
    """
    return bool(album.musicbrainz_release_id or album.musicbrainz_release_group_id)


def populate_album_tracks(album: Album) -> int:
    """Populate Track rows for an album from MusicBrainz and mark tracks_populated."""
    from app.providers import musicbrainz

    if album.tracks_populated:
        return 0

    # Ensure we have a concrete release_id (release_group alone can't list tracks)
    if not album.musicbrainz_release_id:
        ensure_album_has_release_id(album)
    if not album.musicbrainz_release_id:
        return 0

    try:
        release_data = musicbrainz.get_release(album.musicbrainz_release_id)
        tracks_data = release_data.get("tracks", [])

        # Update genres from release if album lacks them
        if release_data.get("genres") and not album.genres:
            album.genres = release_data.get("genres")

        created_or_updated = 0
        for track_data in tracks_data:
            _, created = Track.objects.update_or_create(
                album=album,
                disc_number=track_data.get("disc_number", 1),
                track_number=track_data.get("track_number"),
                defaults={
                    "title": track_data.get("title", "Unknown Track"),
                    "musicbrainz_recording_id": track_data.get("recording_id"),
                    "duration_ms": track_data.get("duration_ms"),
                    "genres": track_data.get("genres", []) or release_data.get("genres", []),
                },
            )
            if created:
                created_or_updated += 1

        # Also update album image if missing
        if (not album.image or album.image == settings.IMG_NONE) and release_data.get("image"):
            album.image = release_data["image"]

        album.tracks_populated = True
        album.save(update_fields=["tracks_populated", "image", "genres"])
        logger.info("Populated %d tracks for album %s", len(tracks_data), album.title)
        return len(tracks_data)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to populate tracks for album %s: %s", album.title, exc)
        return 0


def prefetch_album_covers(artist: Artist, limit: int | None = 20) -> int:
    """Prefetch cover art for albums missing images.
    
    This runs on artist page load to populate album covers that
    were not fetched during discography sync.
    
    Args:
        artist: The Artist whose albums to check
        limit: Maximum number of albums to prefetch (to respect rate limits)
        
    Returns:
        Number of albums that got new cover art
    """
    from app.providers import musicbrainz
    
    # Find albums with missing images that have MusicBrainz IDs
    albums_qs = Album.objects.filter(
        artist=artist,
    ).filter(
        models.Q(image="") | models.Q(image=settings.IMG_NONE)
    ).filter(
        models.Q(musicbrainz_release_id__isnull=False)
        | models.Q(musicbrainz_release_group_id__isnull=False)
    )

    albums_needing_art = list(albums_qs[:limit]) if limit else list(albums_qs)
    
    updated = 0
    for album in albums_needing_art:
        try:
            image = musicbrainz.get_cover_art(
                release_id=album.musicbrainz_release_id,
                release_group_id=album.musicbrainz_release_group_id,
            )
            if image and image != settings.IMG_NONE:
                album.image = image
                album.save(update_fields=["image"])
                updated += 1
                logger.debug("Prefetched cover for album: %s", album.title)
        except Exception as e:
            logger.debug("Failed to prefetch cover for %s: %s", album.title, e)
    
    return updated


def _preferred_status(current: str | None, incoming: str | None) -> str | None:
    """Pick the higher-precedence status between two values."""
    from app.models import Status

    if not current:
        return incoming
    if not incoming:
        return current

    order = {
        Status.COMPLETED.value: 5,
        Status.DROPPED.value: 4,
        Status.IN_PROGRESS.value: 3,
        Status.PAUSED.value: 2,
        Status.PLANNING.value: 1,
    }
    return current if order.get(current, 0) >= order.get(incoming, 0) else incoming


def merge_artist_records(source_artist: Artist, target_artist: Artist) -> Artist:
    """Merge a duplicate artist into a canonical one without losing data."""
    if source_artist.id == target_artist.id:
        return target_artist

    from django.db import IntegrityError
    from app.models import (
        Album,
        AlbumTracker,
        ArtistTracker,
        Music,
        Status,
    )
    from app.services.music_scrobble import dedupe_artist_albums

    # Merge artist trackers (per-user status/score)
    for tracker in ArtistTracker.objects.filter(artist=source_artist):
        existing = ArtistTracker.objects.filter(
            user=tracker.user,
            artist=target_artist,
        ).first()

        if existing:
            updates = set()
            preferred_status = _preferred_status(existing.status, tracker.status)
            if preferred_status and preferred_status != existing.status:
                existing.status = preferred_status
                updates.add("status")

            # Preserve earliest start and latest end dates
            start_date = min(
                [d for d in [existing.start_date, tracker.start_date] if d],
                default=None,
            )
            end_date = max(
                [d for d in [existing.end_date, tracker.end_date] if d],
                default=None,
            )
            if start_date and start_date != existing.start_date:
                existing.start_date = start_date
                updates.add("start_date")
            if end_date and end_date != existing.end_date:
                existing.end_date = end_date
                updates.add("end_date")

            # Fill missing score/notes
            if existing.score is None and tracker.score is not None:
                existing.score = tracker.score
                updates.add("score")
            if tracker.notes and tracker.notes.strip():
                if not existing.notes:
                    existing.notes = tracker.notes
                    updates.add("notes")
                elif tracker.notes not in existing.notes:
                    existing.notes = f"{existing.notes}\n{tracker.notes}"
                    updates.add("notes")

            if updates:
                existing.save(update_fields=list(updates))
            tracker.delete()
        else:
            tracker.artist = target_artist
            tracker.save(update_fields=["artist"])

    def _merge_album_into_target(source_album: Album, target_album: Album):
        updates = set()
        if (
            (not target_album.image or target_album.image == settings.IMG_NONE)
            and source_album.image
            and source_album.image != settings.IMG_NONE
        ):
            target_album.image = source_album.image
            updates.add("image")
        if not target_album.musicbrainz_release_id and source_album.musicbrainz_release_id:
            target_album.musicbrainz_release_id = source_album.musicbrainz_release_id
            updates.add("musicbrainz_release_id")
        if not target_album.musicbrainz_release_group_id and source_album.musicbrainz_release_group_id:
            target_album.musicbrainz_release_group_id = source_album.musicbrainz_release_group_id
            updates.add("musicbrainz_release_group_id")
        if not target_album.release_date and source_album.release_date:
            target_album.release_date = source_album.release_date
            updates.add("release_date")
        if not target_album.release_type and source_album.release_type:
            target_album.release_type = source_album.release_type
            updates.add("release_type")
        if updates:
            target_album.save(update_fields=list(updates))

        # Merge album trackers
        for tracker in AlbumTracker.objects.filter(album=source_album):
            existing = AlbumTracker.objects.filter(
                user=tracker.user,
                album=target_album,
            ).first()
            if existing:
                tracker_updates = set()
                preferred_status = _preferred_status(existing.status, tracker.status)
                if preferred_status and preferred_status != existing.status:
                    existing.status = preferred_status
                    tracker_updates.add("status")

                start_date = min(
                    [d for d in [existing.start_date, tracker.start_date] if d],
                    default=None,
                )
                end_date = max(
                    [d for d in [existing.end_date, tracker.end_date] if d],
                    default=None,
                )
                if start_date and start_date != existing.start_date:
                    existing.start_date = start_date
                    tracker_updates.add("start_date")
                if end_date and end_date != existing.end_date:
                    existing.end_date = end_date
                    tracker_updates.add("end_date")

                if existing.score is None and tracker.score is not None:
                    existing.score = tracker.score
                    tracker_updates.add("score")

                if tracker_updates:
                    existing.save(update_fields=list(tracker_updates))
                tracker.delete()
            else:
                tracker.album = target_album
                tracker.save(update_fields=["album"])

        # Re-point music entries to the canonical album
        Music.objects.filter(album=source_album).update(album=target_album, track=None)

        # Dropping the source album will also drop its tracks; music.track is SET_NULL
        source_album.delete()

    # Move albums over; if a collision happens, merge then delete source
    for album in Album.objects.filter(artist=source_artist):
        album.artist = target_artist
        try:
            album.save(update_fields=["artist"])
            continue
        except IntegrityError:
            conflict = (
                Album.objects.filter(
                    artist=target_artist,
                    musicbrainz_release_group_id=album.musicbrainz_release_group_id,
                ).first()
                or Album.objects.filter(
                    artist=target_artist,
                    musicbrainz_release_id=album.musicbrainz_release_id,
                ).first()
                or Album.objects.filter(
                    artist=target_artist,
                    title=album.title,
                ).first()
            )
            if conflict:
                _merge_album_into_target(album, conflict)
            else:
                # As a last resort, drop the conflicting album to avoid blocking merge
                Music.objects.filter(album=album).update(album=None, track=None)
                album.delete()

    # Move orphaned music entries that reference the artist directly
    Music.objects.filter(artist=source_artist).update(artist=target_artist)

    # Clean up the old artist now that references are moved
    source_artist.delete()

    # Final dedupe pass to collapse any remaining duplicate albums for the target artist
    dedupe_artist_albums(target_artist)

    return target_artist
