"""MusicBrainz API provider for music metadata."""

import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

BASE_URL = "https://musicbrainz.org/ws/2"
COVER_ART_BASE = "https://coverartarchive.org"
WIKIPEDIA_API_BASE = "https://en.wikipedia.org/api/rest_v1/page/summary"
MIN_REQUEST_INTERVAL = 1.0  # MusicBrainz requires 1 req/sec for unauth requests
_last_request_time = 0

# User-Agent required by MusicBrainz API
USER_AGENT = "Yamtrack/1.0 (https://github.com/FuzzyGrim/Yamtrack)"


def get_wikipedia_data(title):
    """Fetch Wikipedia data for a given title (bio extract and image).
    
    Uses Wikipedia's REST API to get a summary/extract and image.
    The title can be an artist name or a specific Wikipedia article title
    (e.g., "Queen_(band)" which is more accurate than just "Queen").
    
    Returns a dict with 'extract' and 'image' keys, or None values if not found.
    """
    if not title:
        return {"extract": None, "image": None}
    
    # Normalize title for cache key and URL
    normalized_title = title.replace(' ', '_')
    cache_key = f"wikipedia_data_{normalized_title.lower()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    
    result = {"extract": None, "image": None}
    
    try:
        # Wikipedia API uses underscores for spaces in titles
        url = f"{WIKIPEDIA_API_BASE}/{normalized_title}"
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        
        if response.ok:
            data = response.json()
            
            # Get the extract (bio)
            extract = data.get("extract", "")
            result["extract"] = extract if extract else None
            
            # Get the image - prefer originalimage, fall back to thumbnail
            original = data.get("originalimage", {})
            thumbnail = data.get("thumbnail", {})
            
            # Use original if available and reasonable size, otherwise thumbnail
            if original.get("source"):
                result["image"] = original["source"]
            elif thumbnail.get("source"):
                result["image"] = thumbnail["source"]
            
            # Cache for 7 days
            cache.set(cache_key, result, 60 * 60 * 24 * 7)
        else:
            # Cache the miss to avoid repeated failed lookups
            cache.set(cache_key, result, 60 * 60 * 24)  # Cache miss for 1 day
            
    except Exception as e:
        logger.debug("Failed to fetch Wikipedia data for %s: %s", artist_name, e)
    
    return result


def get_wikipedia_extract(artist_name):
    """Fetch the Wikipedia extract for an artist (legacy wrapper).
    
    Returns just the extract string for backwards compatibility.
    """
    data = get_wikipedia_data(artist_name)
    return data.get("extract")


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


def _try_fetch_cover_from_url(url):
    """Helper to fetch cover from a specific Cover Art Archive URL.
    
    Returns the best quality image URL or None.
    """
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
                    # Try different thumbnail sizes (prefer medium quality for performance)
                    thumbnails = image.get("thumbnails", {})
                    return (
                        thumbnails.get("500") or 
                        thumbnails.get("large") or
                        thumbnails.get("250") or 
                        image.get("image")
                    )
            # No front cover, use first available image
            if response["images"]:
                first_image = response["images"][0]
                thumbnails = first_image.get("thumbnails", {})
                return (
                    thumbnails.get("500") or 
                    thumbnails.get("large") or
                    thumbnails.get("250") or 
                    first_image.get("image")
                )
    except Exception as e:
        logger.debug("Cover art fetch failed for %s: %s", url, e)
    return None


def get_cover_art(release_id=None, release_group_id=None):
    """Fetch cover art from Cover Art Archive.
    
    This is the centralized function for all cover art fetching.
    Tries release first, then release-group as fallback.
    
    Args:
        release_id: MusicBrainz release ID (specific album edition)
        release_group_id: MusicBrainz release group ID (canonical album)
    
    Returns:
        Image URL or IMG_NONE placeholder
    """
    if not release_id and not release_group_id:
        return settings.IMG_NONE
    
    # Build cache key from both IDs
    cache_key = f"musicbrainz_cover_{release_id or 'none'}_{release_group_id or 'none'}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    
    image_url = None
    
    # Try release-specific cover art first (more specific)
    if release_id:
        image_url = _try_fetch_cover_from_url(f"{COVER_ART_BASE}/release/{release_id}")
    
    # If no cover for release, try release group as fallback
    if not image_url and release_group_id:
        image_url = _try_fetch_cover_from_url(f"{COVER_ART_BASE}/release-group/{release_group_id}")
    
    result = image_url or settings.IMG_NONE
    
    # Cache the result (even if it's IMG_NONE) to avoid repeated lookups
    cache.set(cache_key, result, 60 * 60 * 24 * 7)  # Cache for 7 days
    
    return result


def _get_cover_art(release_id, release_group_id=None):
    """Legacy wrapper for get_cover_art. Use get_cover_art instead."""
    return get_cover_art(release_id=release_id, release_group_id=release_group_id)


def search(query, page=1, skip_cover_art=False):
    """Search for music recordings on MusicBrainz.
    
    Args:
        query: Search query string
        page: Page number for pagination
        skip_cover_art: If True, skip fetching cover art (faster)
    """
    cache_key = f"musicbrainz_search_{query.lower()}_p{page}"
    if skip_cover_art:
        cache_key += "_no_art"
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
            # Try to get cover art from the first release (skip if requested for faster search)
            if not skip_cover_art:
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


def search_releases(query, page=1, skip_cover_art=False):
    """Search for releases (albums) on MusicBrainz.
    
    Args:
        query: Search query string
        page: Page number for pagination
        skip_cover_art: If True, skip fetching cover art (faster)
    """
    cache_key = f"musicbrainz_release_search_{query.lower()}_p{page}"
    if skip_cover_art:
        cache_key += "_no_art"
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

        # Try to get cover art (skip if requested for faster search)
        image = settings.IMG_NONE
        if not skip_cover_art:
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
        "inc": "releases+release-groups+tags+ratings+annotation+genres+url-rels",
    }

    response = _mb_request(f"artist/{artist_id}", params)

    name = response.get("name", "Unknown")
    sort_name = response.get("sort-name", name)
    disambiguation = response.get("disambiguation", "")
    artist_type = response.get("type", "")
    country = response.get("country", "")
    
    # Life span
    life_span = response.get("life-span", {})
    begin_date = life_span.get("begin", "")
    end_date = life_span.get("end", "")
    ended = life_span.get("ended", False)
    
    # Area (country/location)
    area = response.get("area", {})
    area_name = area.get("name", "")
    
    # Annotation from MusicBrainz (often just editor notes, not a bio)
    mb_annotation = response.get("annotation", "")
    
    # Try to get the Wikipedia article title from MusicBrainz relations
    # This is more reliable than guessing (e.g., "Queen" -> "Queen_(band)")
    wikipedia_title = None
    relations = response.get("relations", [])
    for rel in relations:
        if rel.get("type") == "wikipedia":
            url = rel.get("url", {}).get("resource", "")
            # Extract article title from Wikipedia URL
            # e.g., "https://en.wikipedia.org/wiki/Queen_(band)" -> "Queen_(band)"
            if "wikipedia.org/wiki/" in url:
                wikipedia_title = url.split("/wiki/")[-1]
                # Prefer English Wikipedia
                if "en.wikipedia.org" in url:
                    break
    
    # Get Wikipedia data - try multiple strategies
    wikipedia_bio = None
    wikipedia_image = None
    
    if wikipedia_title:
        # Best case: MusicBrainz has the exact Wikipedia URL
        wiki_data = get_wikipedia_data(wikipedia_title)
        wikipedia_bio = wiki_data.get("extract")
        wikipedia_image = wiki_data.get("image")
    
    if not wikipedia_bio:
        # Try artist name directly (works for "Kenny G", etc.)
        wiki_data = get_wikipedia_data(name)
        wikipedia_bio = wiki_data.get("extract")
        wikipedia_image = wiki_data.get("image")
    
    if not wikipedia_bio and disambiguation:
        # Last resort: try name with disambiguation (e.g., "Queen_(band)")
        wiki_title_with_disambig = f"{name}_({disambiguation.replace(' ', '_')})"
        wiki_data = get_wikipedia_data(wiki_title_with_disambig)
        wikipedia_bio = wiki_data.get("extract")
        wikipedia_image = wiki_data.get("image")
    
    # Genres from MusicBrainz (new genre system)
    genres = []
    genre_data = response.get("genres", [])
    for g in genre_data:
        genre_name = g.get("name", "")
        genre_count = g.get("count", 0)
        if genre_name:
            genres.append({
                "name": genre_name,
                "count": genre_count,
            })
    # Sort by count (most relevant first)
    genres.sort(key=lambda x: x.get("count", 0), reverse=True)
    
    # Tags (user-submitted tags, often more available than genres)
    tags = []
    tag_data = response.get("tags", [])
    for t in tag_data:
        tag_name = t.get("name", "")
        tag_count = t.get("count", 0)
        if tag_name and tag_count > 0:
            tags.append({
                "name": tag_name,
                "count": tag_count,
            })
    # Sort by count (most relevant first)
    tags.sort(key=lambda x: x.get("count", 0), reverse=True)
    
    # Rating
    rating_data = response.get("rating", {})
    rating = rating_data.get("value")  # 0-5 scale
    rating_count = rating_data.get("votes-count", 0)

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
        "type": artist_type,
        "country": country,
        "area": area_name,
        "begin_date": begin_date,
        "end_date": end_date,
        "ended": ended,
        "bio": wikipedia_bio,  # Wikipedia extract as bio
        "image": wikipedia_image,  # Wikipedia image URL
        "annotation": mb_annotation,  # MusicBrainz annotation (usually editor notes)
        "genres": genres[:10],  # Top 10 genres
        "tags": tags[:15],  # Top 15 tags
        "rating": rating,
        "rating_count": rating_count,
        "albums": albums,
    }

    cache.set(cache_key, result, 60 * 60 * 24 * 7)
    return result


def get_artist_discography(artist_id, skip_cover_art=False):
    """Get the full discography for an artist from MusicBrainz.
    
    This fetches release-groups (which represent unique album releases)
    and finds a representative release for each to get cover art.
    
    Args:
        artist_id: MusicBrainz artist ID
        skip_cover_art: If True, skip fetching cover art (faster initial load)
    
    Returns a normalized list of albums with:
    - title, release_group_id, release_id, release_date, image, release_type
    """
    cache_key = f"musicbrainz_artist_discography_{artist_id}"
    if skip_cover_art:
        cache_key += "_no_art"
    cached = cache.get(cache_key)
    if cached:
        return cached

    # Fetch ALL release-groups for the artist
    # Don't filter by type to get everything, we'll sort later
    params = {
        "artist": artist_id,
        "limit": 100,
    }

    response = _mb_request("release-group", params)
    release_groups = response.get("release-groups", [])
    
    # Filter to sensible types (Album, EP, Compilation)
    # Skip Singles unless you want them
    allowed_types = {"Album", "EP", "Compilation"}
    release_groups = [
        rg for rg in release_groups 
        if rg.get("primary-type") in allowed_types
    ]

    albums = []
    for rg in release_groups:
        rg_id = rg.get("id")
        title = rg.get("title", "")
        primary_type = rg.get("primary-type", "")
        secondary_types = rg.get("secondary-types", [])
        first_release_date = rg.get("first-release-date", "")

        # Build a type string (e.g., "Album", "EP", "Album + Live")
        release_type = primary_type
        if secondary_types:
            release_type = f"{primary_type} + {', '.join(secondary_types)}"

        albums.append({
            "release_group_id": rg_id,
            "title": title,
            "release_date": first_release_date,
            "release_type": release_type,
            "primary_type": primary_type,
            "secondary_types": secondary_types,
            "release_id": None,  # Will be filled if we fetch releases
            "image": settings.IMG_NONE,  # Will be filled later
        })

    # Now fetch actual releases to get release IDs for cover art
    # We'll batch this to avoid too many API calls
    # Get releases for each release-group to find cover art
    release_params = {
        "artist": artist_id,
        "status": "official",
        "limit": 100,
    }
    
    release_response = _mb_request("release", release_params)
    releases = release_response.get("releases", [])
    
    # Map release-group-id to best release (prefer ones with cover art)
    rg_to_release = {}
    for release in releases:
        rg_id = release.get("release-group", {}).get("id")
        if rg_id:
            # Keep first release per release-group (API returns most relevant first)
            if rg_id not in rg_to_release:
                rg_to_release[rg_id] = release

    # Update albums with release IDs and optionally fetch cover art
    for album in albums:
        rg_id = album["release_group_id"]
        release_id = None
        
        if rg_id in rg_to_release:
            release = rg_to_release[rg_id]
            release_id = release.get("id")
            album["release_id"] = release_id
        
        if not skip_cover_art:
            # Try to get cover art - use both release_id and release_group_id
            # This ensures we try the release-group fallback even if we have a release_id
            album["image"] = get_cover_art(release_id=release_id, release_group_id=rg_id)

    # Sort by date (newest first), with albums without dates at the end
    albums.sort(key=lambda x: x.get("release_date", "") or "0000", reverse=True)

    cache.set(cache_key, albums, 60 * 60 * 24 * 7)  # Cache for 7 days
    return albums


def get_release_for_group(release_group_id):
    """Get a representative release for a release group.
    
    This is useful when we only have a release_group_id and need to find
    a specific release to fetch tracks from.
    
    Args:
        release_group_id: The MusicBrainz release group ID
        
    Returns:
        A release ID string, or None if not found
    """
    cache_key = f"musicbrainz_release_for_group_{release_group_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    
    try:
        # Query releases for this release group
        params = {
            "release-group": release_group_id,
            "status": "official",
            "limit": 5,
        }
        
        response = _mb_request("release", params)
        releases = response.get("releases", [])
        
        if releases:
            # Prefer releases with media/tracks info
            # Just pick the first official release
            release_id = releases[0].get("id")
            cache.set(cache_key, release_id, 60 * 60 * 24 * 7)
            return release_id
            
    except Exception as e:
        logger.debug("Failed to get release for group %s: %s", release_group_id, e)
    
    return None


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
    """Combined search returning artists, albums, and tracks.
    
    Cover art is skipped during search for speed - results use placeholders.
    """
    cache_key = f"musicbrainz_combined_search_{query.lower()}_p{page}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    # For first page, fetch artists, releases, and recordings
    # For subsequent pages, only fetch recordings (tracks)
    # Skip cover art for faster search results
    if page == 1:
        artist_results = search_artists(query, page=1)
        release_results = search_releases(query, page=1, skip_cover_art=True)
        track_results = search(query, page=1, skip_cover_art=True)

        data = {
            "artists": artist_results.get("results", [])[:5],  # Top 5 artists
            "releases": release_results.get("results", [])[:5],  # Top 5 albums
            "tracks": track_results,  # Full track results with pagination
        }
    else:
        # For page > 1, only return tracks (skip cover art for speed)
        track_results = search(query, page=page, skip_cover_art=True)
        data = {
            "artists": [],
            "releases": [],
            "tracks": track_results,
        }

    cache.set(cache_key, data, 60 * 60 * 24)
    return data

