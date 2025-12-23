"""Fetch music artwork (album covers and artist images) from iTunes API.

This provides a fallback option when MusicBrainz Cover Art Archive
doesn't have artwork for albums or artists.
"""

import logging

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

ITUNES_API_BASE = "https://itunes.apple.com/search"
USER_AGENT = "Yamtrack/1.0 (https://github.com/FuzzyGrim/Yamtrack)"


def fetch_album_artwork(album_title: str, artist_name: str) -> str | None:
    """Fetch album artwork from iTunes API.
    
    Args:
        album_title: Album title
        artist_name: Artist name
        
    Returns:
        Image URL string or None if not found
    """
    # Check cache first
    cache_key = f"itunes_album_artwork_{artist_name}_{album_title}".lower()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached else None

    try:
        # Build search query: "artist_name album_title"
        query = f"{artist_name} {album_title}"

        # iTunes API expects URL-encoded query
        params = {
            "term": query,
            "media": "music",
            "entity": "album",
            "limit": 5,  # Get top 5 results
        }

        response = requests.get(
            ITUNES_API_BASE,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()

        data = response.json()
        results = data.get("results", [])

        if not results:
            # Cache None to avoid repeated lookups
            cache.set(cache_key, None, 60 * 60 * 24 * 7)  # Cache for 7 days
            return None

        # Try to find best match by album title and artist name
        album_title_lower = album_title.lower()
        artist_name_lower = artist_name.lower()

        for result in results:
            result_album = result.get("collectionName", "").lower()
            result_artist = result.get("artistName", "").lower()

            # Check if album title and artist name match (fuzzy matching)
            album_matches = (
                result_album == album_title_lower
                or album_title_lower in result_album
                or result_album in album_title_lower
            )
            artist_matches = (
                result_artist == artist_name_lower
                or artist_name_lower in result_artist
                or result_artist in artist_name_lower
            )

            if album_matches and artist_matches:
                artwork_url = result.get("artworkUrl600") or result.get("artworkUrl100")
                if artwork_url:
                    logger.debug("Found iTunes artwork for album %s by %s", album_title, artist_name)
                    # Cache the result
                    cache.set(cache_key, artwork_url, 60 * 60 * 24 * 7)  # Cache for 7 days
                    return artwork_url

        # If no exact match, try first result if artist matches
        for result in results:
            result_artist = result.get("artistName", "").lower()
            if artist_name_lower in result_artist or result_artist in artist_name_lower:
                artwork_url = result.get("artworkUrl600") or result.get("artworkUrl100")
                if artwork_url:
                    logger.debug("Using iTunes artwork for album %s by %s (best match)", album_title, artist_name)
                    cache.set(cache_key, artwork_url, 60 * 60 * 24 * 7)
                    return artwork_url

        # Last resort: use first result if available
        if results:
            artwork_url = results[0].get("artworkUrl600") or results[0].get("artworkUrl100")
            if artwork_url:
                logger.debug("Using first iTunes result for album %s by %s", album_title, artist_name)
                cache.set(cache_key, artwork_url, 60 * 60 * 24 * 7)
                return artwork_url

    except Exception as e:
        logger.debug("Failed to fetch album artwork from iTunes for %s by %s: %s", album_title, artist_name, e)

    # Cache None to avoid repeated lookups
    cache.set(cache_key, None, 60 * 60 * 24 * 7)
    return None


def fetch_artist_artwork(artist_name: str) -> str | None:
    """Fetch artist artwork from iTunes API.
    
    Note: iTunes doesn't typically have direct artist photos, so this
    function searches for albums by the artist and returns artwork from
    a representative album (preferring early releases which are often
    more iconic).
    
    Args:
        artist_name: Artist name
        
    Returns:
        Image URL string or None if not found
    """
    # Check cache first
    cache_key = f"itunes_artist_artwork_{artist_name}".lower()
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached else None

    try:
        # Build search query: just artist name
        query = artist_name

        # iTunes API expects URL-encoded query
        params = {
            "term": query,
            "media": "music",
            "entity": "album",
            "limit": 10,  # Get more results to find early releases
        }

        response = requests.get(
            ITUNES_API_BASE,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()

        data = response.json()
        results = data.get("results", [])

        if not results:
            # Cache None to avoid repeated lookups
            cache.set(cache_key, None, 60 * 60 * 24 * 7)  # Cache for 7 days
            return None

        # Filter results to only those by this artist
        artist_name_lower = artist_name.lower()
        artist_albums = []

        for result in results:
            result_artist = result.get("artistName", "").lower()
            # Check if artist name matches (fuzzy matching)
            if (
                result_artist == artist_name_lower
                or artist_name_lower in result_artist
                or result_artist in artist_name_lower
            ):
                artwork_url = result.get("artworkUrl600") or result.get("artworkUrl100")
                if artwork_url:
                    # Try to prefer earlier releases (lower release date)
                    release_date = result.get("releaseDate", "")
                    artist_albums.append((release_date, artwork_url))

        if not artist_albums:
            # Cache None to avoid repeated lookups
            cache.set(cache_key, None, 60 * 60 * 24 * 7)
            return None

        # Sort by release date (earliest first) and use first one
        artist_albums.sort(key=lambda x: x[0] if x[0] else "9999-12-31")
        artwork_url = artist_albums[0][1]

        logger.debug("Found iTunes artwork for artist %s", artist_name)
        # Cache the result
        cache.set(cache_key, artwork_url, 60 * 60 * 24 * 7)  # Cache for 7 days
        return artwork_url

    except Exception as e:
        logger.debug("Failed to fetch artist artwork from iTunes for %s: %s", artist_name, e)

    # Cache None to avoid repeated lookups
    cache.set(cache_key, None, 60 * 60 * 24 * 7)
    return None
