"""Music-related service functions for discography sync."""

import logging
from datetime import datetime

from django.utils import timezone
from django.utils.dateparse import parse_date

from app.models import Album, Artist

logger = logging.getLogger(__name__)


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
        discography = musicbrainz.get_artist_discography(artist.musicbrainz_id)
        
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

