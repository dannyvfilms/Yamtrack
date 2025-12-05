"""Music-related service functions for discography sync."""

import logging
from datetime import datetime

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_date

from app.models import Album, Artist

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


def prefetch_album_covers(artist: Artist, limit: int = 20) -> int:
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
    albums_needing_art = Album.objects.filter(
        artist=artist,
    ).filter(
        models.Q(image="") | models.Q(image=settings.IMG_NONE)
    ).filter(
        models.Q(musicbrainz_release_id__isnull=False) | 
        models.Q(musicbrainz_release_group_id__isnull=False)
    )[:limit]
    
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

