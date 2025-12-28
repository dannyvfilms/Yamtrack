"""Music library validation and diagnostic queries."""

import logging

from django.db.models import (
    Q,
)

from app.models import Album, Artist, Music

logger = logging.getLogger(__name__)


def validate_music_library(user):
    """Comprehensive validation of user's music library.
    
    Returns a dictionary with data quality metrics including:
    - Track counts (total, unique, linked, with runtime)
    - Artist MBID coverage
    - Album track population status
    - Missing metadata breakdown
    - Enrichment readiness metrics
    
    Args:
        user: Django User instance
        
    Returns:
        dict with validation metrics
    """
    music_entries = Music.objects.filter(user=user).select_related(
        "item", "artist", "album", "track",
    )

    total_music_entries = music_entries.count()

    # Count unique tracks (distinct by item.media_id + item.source)
    unique_tracks = (
        music_entries.values("item__media_id", "item__source")
        .distinct()
        .count()
    )

    # Count Music entries with Track model links
    with_track_link = music_entries.filter(track__isnull=False).count()

    # Count Music entries with runtime (from Item)
    with_runtime = music_entries.filter(item__runtime_minutes__isnull=False).count()

    # Count Music entries with MBIDs (via Track musicbrainz_recording_id)
    with_mbid = music_entries.filter(
        track__musicbrainz_recording_id__isnull=False,
    ).count()

    # Get artist statistics
    # NOTE: This uses the same logic as enrich_music_library_task:
    # - Gets artist_ids from Music entries for the user
    # - Only checks artists that have Music entries (not orphaned artists)
    # - If validation shows different numbers than enrichment task:
    #   * Check if validation ran before enrichment completed
    #   * Check if new Music entries were added between runs
    #   * Both functions should show the same counts for the same Music entry set
    artist_ids = music_entries.exclude(artist_id__isnull=True).values_list(
        "artist_id", flat=True,
    ).distinct()

    artists = Artist.objects.filter(id__in=artist_ids)
    artists_with_mbid = artists.exclude(musicbrainz_id__isnull=True).count()
    total_artists = artists.count()

    # Get album statistics
    album_ids = music_entries.exclude(album_id__isnull=True).values_list(
        "album_id", flat=True,
    ).distinct()

    albums = Album.objects.filter(id__in=album_ids)
    albums_with_tracks = albums.filter(tracks_populated=True).count()
    total_albums = albums.count()

    # Count Music entries with plays (progress > 0)
    with_plays = music_entries.filter(progress__gt=0).count()

    # Missing linkage counts
    missing_track_link = music_entries.filter(track__isnull=True).count()
    missing_artist_link = music_entries.filter(artist__isnull=True).count()
    missing_album_link = music_entries.filter(album__isnull=True).count()
    missing_runtime = music_entries.filter(item__runtime_minutes__isnull=True).count()

    return {
        "total_music_entries": total_music_entries,
        "unique_tracks": unique_tracks,
        "with_track_link": with_track_link,
        "with_runtime": with_runtime,
        "with_mbid": with_mbid,
        "with_plays": with_plays,
        "artists": {
            "total": total_artists,
            "with_mbid": artists_with_mbid,
            "missing_mbid": total_artists - artists_with_mbid,
        },
        "albums": {
            "total": total_albums,
            "with_tracks_populated": albums_with_tracks,
            "missing_tracks": total_albums - albums_with_tracks,
        },
        "missing_linkages": {
            "track_link": missing_track_link,
            "artist_link": missing_artist_link,
            "album_link": missing_album_link,
            "runtime": missing_runtime,
        },
        "percentages": {
            "track_link": (
                (with_track_link / total_music_entries * 100)
                if total_music_entries > 0
                else 0
            ),
            "runtime": (
                (with_runtime / total_music_entries * 100)
                if total_music_entries > 0
                else 0
            ),
            "artist_mbid": (
                (artists_with_mbid / total_artists * 100)
                if total_artists > 0
                else 0
            ),
            "album_tracks": (
                (albums_with_tracks / total_albums * 100)
                if total_albums > 0
                else 0
            ),
        },
    }


def count_user_tracks(user):
    """Count unique tracks for a user.
    
    Returns:
        dict with track count metrics
    """
    music_entries = Music.objects.filter(user=user).select_related("item", "track")

    total_music_entries = music_entries.count()

    # Count unique tracks (distinct by item.media_id + item.source)
    unique_tracks = (
        music_entries.values("item__media_id", "item__source")
        .distinct()
        .count()
    )

    # Count Music entries linked to Track model
    with_track_link = music_entries.filter(track__isnull=False).count()

    # Count Music entries with runtime
    with_runtime = music_entries.filter(item__runtime_minutes__isnull=False).count()

    # Count Music entries with MusicBrainz recording ID
    with_mbid = music_entries.filter(
        track__musicbrainz_recording_id__isnull=False,
    ).count()

    return {
        "total_music_entries": total_music_entries,
        "unique_tracks": unique_tracks,
        "with_track_link": with_track_link,
        "with_runtime": with_runtime,
        "with_mbid": with_mbid,
    }


def get_enrichment_status(user):
    """Get enrichment status for user's music library.
    
    Returns information about what has been enriched and what's missing.
    """
    music_entries = Music.objects.filter(user=user).select_related(
        "item", "artist", "album", "track",
    )

    # Artists with/without MBIDs
    artist_ids = music_entries.exclude(artist_id__isnull=True).values_list(
        "artist_id", flat=True,
    ).distinct()

    artists = Artist.objects.filter(id__in=artist_ids)
    artists_with_mbid = artists.exclude(musicbrainz_id__isnull=True)
    artists_without_mbid = artists.filter(musicbrainz_id__isnull=True)

    # Albums with/without populated tracks
    album_ids = music_entries.exclude(album_id__isnull=True).values_list(
        "album_id", flat=True,
    ).distinct()

    albums = Album.objects.filter(id__in=album_ids)
    albums_with_tracks = albums.filter(tracks_populated=True)
    albums_without_tracks = albums.filter(tracks_populated=False)

    # Music entries with/without Track links
    music_with_track = music_entries.filter(track__isnull=False)
    music_without_track = music_entries.filter(track__isnull=True)

    # Missing runtime data
    music_with_runtime = music_entries.filter(item__runtime_minutes__isnull=False)
    music_without_runtime = music_entries.filter(item__runtime_minutes__isnull=True)

    return {
        "artists": {
            "total": artists.count(),
            "with_mbid": artists_with_mbid.count(),
            "without_mbid": artists_without_mbid.count(),
            "without_mbid_list": [
                {"id": a.id, "name": a.name}
                for a in artists_without_mbid[:20]  # Limit to first 20
            ],
        },
        "albums": {
            "total": albums.count(),
            "with_tracks": albums_with_tracks.count(),
            "without_tracks": albums_without_tracks.count(),
            "without_tracks_list": [
                {"id": a.id, "title": a.title, "artist": a.artist.name if a.artist else "Unknown"}
                for a in albums_without_tracks[:20]  # Limit to first 20
            ],
        },
        "music_entries": {
            "total": music_entries.count(),
            "with_track_link": music_with_track.count(),
            "without_track_link": music_without_track.count(),
            "with_runtime": music_with_runtime.count(),
            "without_runtime": music_without_runtime.count(),
        },
    }


def get_missing_linkages(user):
    """Get counts of Music entries with missing linkages.
    
    Returns breakdown of what's missing.
    """
    music_entries = Music.objects.filter(user=user).select_related(
        "item", "artist", "album", "track",
    )

    missing_track = music_entries.filter(track__isnull=True)
    missing_artist = music_entries.filter(artist__isnull=True)
    missing_album = music_entries.filter(album__isnull=True)
    missing_runtime = music_entries.filter(item__runtime_minutes__isnull=True)

    # Count entries missing multiple things
    missing_multiple = music_entries.filter(
        Q(track__isnull=True)
        | Q(artist__isnull=True)
        | Q(album__isnull=True)
        | Q(item__runtime_minutes__isnull=True),
    )

    return {
        "missing_track_link": missing_track.count(),
        "missing_artist_link": missing_artist.count(),
        "missing_album_link": missing_album.count(),
        "missing_runtime": missing_runtime.count(),
        "missing_multiple": missing_multiple.count(),
        "total": music_entries.count(),
    }


def compare_plex_track_count(user, plex_track_count=None):
    """Compare Yamtrack track counts with Plex data.
    
    Args:
        user: Django User instance
        plex_track_count: Optional Plex track count for comparison
        
    Returns:
        dict with comparison metrics
    """
    validation = validate_music_library(user)

    result = {
        "yamtrack": {
            "total_music_entries": validation["total_music_entries"],
            "unique_tracks": validation["unique_tracks"],
            "tracks_with_plays": validation["with_plays"],
        },
    }

    if plex_track_count:
        result["plex"] = {
            "tracks_with_plays": plex_track_count,
        }
        result["comparison"] = {
            "difference": validation["unique_tracks"] - plex_track_count,
            "yamtrack_higher": validation["unique_tracks"] > plex_track_count,
        }

    return result

