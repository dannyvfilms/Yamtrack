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


def _get_cover_art(release_id):
    """Try to fetch cover art for a release from the Cover Art Archive."""
    try:
        url = f"{COVER_ART_BASE}/release/{release_id}"
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
            for image in response["images"]:
                if image.get("front"):
                    return image.get("thumbnails", {}).get("500") or image.get("image")
            # If no front cover, use the first image
            if response["images"]:
                first_image = response["images"][0]
                return first_image.get("thumbnails", {}).get("500") or first_image.get("image")
    except Exception:
        # Cover art is optional, don't fail the whole request
        logger.debug("No cover art found for release %s", release_id)
    
    return settings.IMG_NONE


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
    image = settings.IMG_NONE
    
    if releases:
        # Use the first release with a date, or just the first release
        for release in releases:
            if release.get("date"):
                album_title = release.get("title", "")
                album_id = release.get("id")
                release_date = release.get("date")
                image = _get_cover_art(album_id)
                break
        
        if not album_title and releases:
            first_release = releases[0]
            album_title = first_release.get("title", "")
            album_id = first_release.get("id")
            release_date = first_release.get("date")
            image = _get_cover_art(album_id)
    
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

