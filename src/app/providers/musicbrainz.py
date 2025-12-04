"""MusicBrainz API provider for music metadata."""

import logging
import time

from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

BASE_URL = "https://musicbrainz.org/ws/2"
COVER_ART_BASE = "https://coverartarchive.org"
MIN_REQUEST_INTERVAL = 1.0  # MusicBrainz requires 1 req/sec for unauth requests
_last_request_time = 0

# User-Agent required by MusicBrainz API
USER_AGENT = "Yamtrack/1.0 (https://github.com/FuzzyGrim/Yamtrack)"


def _rate_limit():
    """Ensure minimum time between MusicBrainz API requests."""
    global _last_request_time
    current_time = time.time()
    elapsed = current_time - _last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()


def _mb_request(endpoint, params=None):
    """Make a rate-limited request to the MusicBrainz API."""
    _rate_limit()
    url = f"{BASE_URL}/{endpoint}"
    
    if params is None:
        params = {}
    params["fmt"] = "json"
    
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    
    try:
        response = services.api_request(
            Sources.MUSICBRAINZ.value,
            "GET",
            url,
            params=params,
            headers=headers,
        )
        return response
    except Exception as error:
        logger.exception("MusicBrainz API request failed: %s", error)
        raise


def _get_cover_art(release_id, release_group_id=None):
    """Try to fetch cover art for a release from the Cover Art Archive.
    
    Args:
        release_id: MusicBrainz release ID
        release_group_id: Optional release group ID to try as fallback
    
    Returns:
        Image URL or IMG_NONE placeholder
    """
    # Check cache first
    cache_key = f"musicbrainz_cover_{release_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    
    def _try_fetch_cover(url):
        """Helper to fetch cover from a specific URL."""
        try:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            }
            response = services.api_request(
                Sources.MUSICBRAINZ.value,
                "GET",
                url,
                headers=headers,
            )
            
            if response and "images" in response:
                # Prioritize front cover
                for image in response["images"]:
                    if image.get("front"):
                        # Try different thumbnail sizes
                        thumbnails = image.get("thumbnails", {})
                        return (
                            thumbnails.get("500") or 
                            thumbnails.get("250") or 
                            thumbnails.get("large") or 
                            image.get("image")
                        )
                # No front cover, use first available image
                if response["images"]:
                    first_image = response["images"][0]
                    thumbnails = first_image.get("thumbnails", {})
                    return (
                        thumbnails.get("500") or 
                        thumbnails.get("250") or 
                        thumbnails.get("large") or 
                        first_image.get("image")
                    )
        except Exception as e:
            logger.debug("Cover art fetch failed for %s: %s", url, e)
        return None
    
    # Try release-specific cover art first
    image_url = _try_fetch_cover(f"{COVER_ART_BASE}/release/{release_id}")
    
    # If no cover for release, try release group as fallback
    if not image_url and release_group_id:
        image_url = _try_fetch_cover(f"{COVER_ART_BASE}/release-group/{release_group_id}")
    
    result = image_url or settings.IMG_NONE
    
    # Cache the result (even if it's IMG_NONE) to avoid repeated lookups
    cache.set(cache_key, result, 60 * 60 * 24 * 7)  # Cache for 7 days
    
    return result


def search(query, page=1):
    """Search for music recordings on MusicBrainz."""
    cache_key = f"musicbrainz_search_{query.lower()}_p{page}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    
    per_page = 20
    offset = (page - 1) * per_page
    
    # Search for recordings (tracks)
    params = {
        "query": query,
        "limit": per_page,
        "offset": offset,
    }
    
    response = _mb_request("recording", params)
    
    recordings = response.get("recordings", [])
    total_results = response.get("count", 0)
    
    results = []
    for recording in recordings:
        recording_id = recording.get("id")
        title = recording.get("title", "Unknown")
        
        # Get artist info
        artist_credits = recording.get("artist-credit", [])
        artist_name = ""
        if artist_credits:
            # Combine all artist credits into a single string
            artist_parts = []
            for credit in artist_credits:
                if isinstance(credit, dict):
                    artist_parts.append(credit.get("name", credit.get("artist", {}).get("name", "")))
                    artist_parts.append(credit.get("joinphrase", ""))
            artist_name = "".join(artist_parts).strip()
        
        # Get release info (album) and image
        releases = recording.get("releases", [])
        image = settings.IMG_NONE
        release_date = None
        album_title = ""
        
        if releases:
            first_release = releases[0]
            album_title = first_release.get("title", "")
            release_date = first_release.get("date", "")
            # Try to get cover art from the first release
            release_id = first_release.get("id")
            if release_id:
                image = _get_cover_art(release_id)
        
        # Get duration in milliseconds, convert to minutes
        duration_ms = recording.get("length")
        duration_minutes = None
        if duration_ms:
            duration_minutes = round(duration_ms / 60000, 1)
        
        # Build display title with artist
        display_title = title
        if artist_name:
            display_title = f"{title} - {artist_name}"
        
        results.append({
            "media_id": recording_id,
            "source": Sources.MUSICBRAINZ.value,
            "media_type": MediaTypes.MUSIC.value,
            "title": display_title,
            "image": image,
            # Store additional data for later use
            "artist_name": artist_name,
            "album_title": album_title,
            "duration_minutes": duration_minutes,
            "release_date": release_date,
        })
    
    data = helpers.format_search_response(
        page=page,
        per_page=per_page,
        total_results=total_results,
        results=results,
    )
    
    cache.set(cache_key, data, 60 * 60 * 24)  # Cache for 24 hours
    return data


def recording(media_id):
    """Get detailed metadata for a music recording (track)."""
    cache_key = f"musicbrainz_recording_{media_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    
    # Fetch recording with releases and artist-credits included
    params = {
        "inc": "artists+releases+release-groups",
    }
    
    response = _mb_request(f"recording/{media_id}", params)
    
    title = response.get("title", "Unknown")
    recording_id = response.get("id", media_id)
    
    # Get artist info
    artist_credits = response.get("artist-credit", [])
    artist_name = ""
    artist_id = None
    if artist_credits:
        artist_parts = []
        for credit in artist_credits:
            if isinstance(credit, dict):
                artist_data = credit.get("artist", {})
                if not artist_id:
                    artist_id = artist_data.get("id")
                artist_parts.append(credit.get("name", artist_data.get("name", "")))
                artist_parts.append(credit.get("joinphrase", ""))
        artist_name = "".join(artist_parts).strip()
    
    # Get release info
    releases = response.get("releases", [])
    release_date = None
    album_title = ""
    album_id = None
    release_group_id = None
    image = settings.IMG_NONE
    
    if releases:
        # Use the first release with a date, or just the first release
        selected_release = None
        for release in releases:
            if release.get("date"):
                selected_release = release
                break
        
        if not selected_release and releases:
            selected_release = releases[0]
        
        if selected_release:
            album_title = selected_release.get("title", "")
            album_id = selected_release.get("id")
            release_date = selected_release.get("date")
            release_group_id = selected_release.get("release-group", {}).get("id")
            image = _get_cover_art(album_id, release_group_id)
    
    # Get duration
    duration_ms = response.get("length")
    duration_minutes = None
    runtime_str = None
    if duration_ms:
        duration_minutes = round(duration_ms / 60000, 1)
        minutes = int(duration_ms // 60000)
        seconds = int((duration_ms % 60000) // 1000)
        runtime_str = f"{minutes}:{seconds:02d}"
    
    # Build display title with artist
    display_title = title
    if artist_name:
        display_title = f"{title} - {artist_name}"
    
    result = {
        "media_id": recording_id,
        "source": Sources.MUSICBRAINZ.value,
        "media_type": MediaTypes.MUSIC.value,
        "title": display_title,
        "image": image,
        "max_progress": None,  # Music tracks don't have progress in the traditional sense
        "synopsis": "",  # MusicBrainz doesn't have track descriptions
        "related": {},
        "details": {
            "artist": artist_name,
            "artist_id": artist_id,
            "album": album_title,
            "album_id": album_id,
            "release_date": release_date,
            "runtime": runtime_str,
            "duration_minutes": duration_minutes,
        },
        # Additional fields for creating Artist/Album models
        "_artist_name": artist_name,
        "_artist_id": artist_id,
        "_album_title": album_title,
        "_album_id": album_id,
    }
    
    cache.set(cache_key, result, 60 * 60 * 24 * 7)  # Cache for 7 days
    return result


def search_artists(query, page=1):
    """Search for artists on MusicBrainz."""
    cache_key = f"musicbrainz_artist_search_{query.lower()}_p{page}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    per_page = 10
    offset = (page - 1) * per_page

    params = {
        "query": query,
        "limit": per_page,
        "offset": offset,
    }

    response = _mb_request("artist", params)

    artists = response.get("artists", [])
    total_results = response.get("count", 0)

    results = []
    for artist in artists:
        artist_id = artist.get("id")
        name = artist.get("name", "Unknown")
        sort_name = artist.get("sort-name", name)
        disambiguation = artist.get("disambiguation", "")
        
        # Get life-span for display
        life_span = artist.get("life-span", {})
        begin = life_span.get("begin", "")
        
        results.append({
            "artist_id": artist_id,
            "name": name,
            "sort_name": sort_name,
            "disambiguation": disambiguation,
            "begin_year": begin[:4] if begin else None,
            "type": artist.get("type", ""),
        })

    data = {
        "page": page,
        "per_page": per_page,
        "total_results": total_results,
        "total_pages": (total_results + per_page - 1) // per_page if total_results > 0 else 0,
        "results": results,
    }

    cache.set(cache_key, data, 60 * 60 * 24)
    return data


def search_releases(query, page=1):
    """Search for releases (albums) on MusicBrainz."""
    cache_key = f"musicbrainz_release_search_{query.lower()}_p{page}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    per_page = 10
    offset = (page - 1) * per_page

    params = {
        "query": query,
        "limit": per_page,
        "offset": offset,
    }

    response = _mb_request("release", params)

    releases = response.get("releases", [])
    total_results = response.get("count", 0)

    results = []
    for release in releases:
        release_id = release.get("id")
        title = release.get("title", "Unknown")
        date = release.get("date", "")

        # Get artist info
        artist_credits = release.get("artist-credit", [])
        artist_name = ""
        artist_id = None
        if artist_credits:
            artist_parts = []
            for credit in artist_credits:
                if isinstance(credit, dict):
                    artist_data = credit.get("artist", {})
                    if not artist_id:
                        artist_id = artist_data.get("id")
                    artist_parts.append(credit.get("name", artist_data.get("name", "")))
                    artist_parts.append(credit.get("joinphrase", ""))
            artist_name = "".join(artist_parts).strip()

        # Try to get cover art
        image = _get_cover_art(release_id)

        results.append({
            "release_id": release_id,
            "title": title,
            "artist_name": artist_name,
            "artist_id": artist_id,
            "release_date": date,
            "image": image,
        })

    data = {
        "page": page,
        "per_page": per_page,
        "total_results": total_results,
        "total_pages": (total_results + per_page - 1) // per_page if total_results > 0 else 0,
        "results": results,
    }

    cache.set(cache_key, data, 60 * 60 * 24)
    return data


def get_artist(artist_id):
    """Get detailed metadata for an artist."""
    cache_key = f"musicbrainz_artist_{artist_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    params = {
        "inc": "releases+release-groups",
    }

    response = _mb_request(f"artist/{artist_id}", params)

    name = response.get("name", "Unknown")
    sort_name = response.get("sort-name", name)
    disambiguation = response.get("disambiguation", "")

    # Get releases (albums) for this artist
    release_groups = response.get("release-groups", [])
    albums = []
    for rg in release_groups[:20]:  # Limit to 20 albums
        albums.append({
            "release_group_id": rg.get("id"),
            "title": rg.get("title", ""),
            "type": rg.get("primary-type", ""),
            "first_release_date": rg.get("first-release-date", ""),
        })

    result = {
        "artist_id": artist_id,
        "name": name,
        "sort_name": sort_name,
        "disambiguation": disambiguation,
        "albums": albums,
    }

    cache.set(cache_key, result, 60 * 60 * 24 * 7)
    return result


def get_artist_releases(artist_id, limit=100):
    """Get all releases (albums) for an artist with cover art.
    
    This fetches releases directly associated with the artist,
    which gives us actual release IDs for cover art lookup.
    """
    cache_key = f"musicbrainz_artist_releases_{artist_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    params = {
        "artist": artist_id,
        "type": "album|ep",  # Focus on albums and EPs
        "status": "official",
        "limit": limit,
    }

    response = _mb_request("release", params)
    
    releases = response.get("releases", [])
    
    # De-duplicate by release-group (keep first/best release per group)
    seen_groups = {}
    for release in releases:
        rg_id = release.get("release-group", {}).get("id")
        if rg_id and rg_id not in seen_groups:
            seen_groups[rg_id] = release
    
    albums = []
    for release in seen_groups.values():
        release_id = release.get("id")
        title = release.get("title", "")
        date = release.get("date", "")
        
        # Get cover art
        image = _get_cover_art(release_id)
        
        albums.append({
            "release_id": release_id,
            "release_group_id": release.get("release-group", {}).get("id"),
            "title": title,
            "release_date": date,
            "image": image,
            "type": release.get("release-group", {}).get("primary-type", ""),
        })
    
    # Sort by date (newest first)
    albums.sort(key=lambda x: x.get("release_date", "") or "0000", reverse=True)
    
    cache.set(cache_key, albums, 60 * 60 * 24 * 7)  # Cache for 7 days
    return albums


def get_release(release_id):
    """Get detailed metadata for a release (album)."""
    cache_key = f"musicbrainz_release_{release_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    params = {
        "inc": "artists+recordings+release-groups",
    }

    response = _mb_request(f"release/{release_id}", params)

    title = response.get("title", "Unknown")
    date = response.get("date", "")

    # Get release group ID for cover art fallback
    release_group_id = response.get("release-group", {}).get("id")

    # Get artist info
    artist_credits = response.get("artist-credit", [])
    artist_name = ""
    artist_id = None
    if artist_credits:
        artist_parts = []
        for credit in artist_credits:
            if isinstance(credit, dict):
                artist_data = credit.get("artist", {})
                if not artist_id:
                    artist_id = artist_data.get("id")
                artist_parts.append(credit.get("name", artist_data.get("name", "")))
                artist_parts.append(credit.get("joinphrase", ""))
        artist_name = "".join(artist_parts).strip()

    # Get cover art with release group fallback
    image = _get_cover_art(release_id, release_group_id)

    # Get tracks
    media_list = response.get("media", [])
    tracks = []
    for medium in media_list:
        disc_number = medium.get("position", 1)
        for track in medium.get("tracks", []):
            recording = track.get("recording", {})
            track_length = recording.get("length")
            duration_str = None
            if track_length:
                minutes = int(track_length // 60000)
                seconds = int((track_length % 60000) // 1000)
                duration_str = f"{minutes}:{seconds:02d}"

            tracks.append({
                "recording_id": recording.get("id"),
                "title": recording.get("title", track.get("title", "")),
                "track_number": track.get("position"),
                "disc_number": disc_number,
                "duration": duration_str,
                "duration_ms": track_length,
            })

    result = {
        "release_id": release_id,
        "title": title,
        "artist_name": artist_name,
        "artist_id": artist_id,
        "release_date": date,
        "image": image,
        "tracks": tracks,
    }

    cache.set(cache_key, result, 60 * 60 * 24 * 7)
    return result


def search_combined(query, page=1):
    """Combined search returning artists, albums, and tracks."""
    cache_key = f"musicbrainz_combined_search_{query.lower()}_p{page}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    # For first page, fetch artists, releases, and recordings
    # For subsequent pages, only fetch recordings (tracks)
    if page == 1:
        artist_results = search_artists(query, page=1)
        release_results = search_releases(query, page=1)
        track_results = search(query, page=1)

        data = {
            "artists": artist_results.get("results", [])[:5],  # Top 5 artists
            "releases": release_results.get("results", [])[:5],  # Top 5 albums
            "tracks": track_results,  # Full track results with pagination
        }
    else:
        # For page > 1, only return tracks
        track_results = search(query, page=page)
        data = {
            "artists": [],
            "releases": [],
            "tracks": track_results,
        }

    cache.set(cache_key, data, 60 * 60 * 24)
    return data

