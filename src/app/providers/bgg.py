"""BoardGameGeek (BGG) API provider for board game metadata.

IMPORTANT: As of November 2024, BGG requires API registration and authorization tokens.

To use this provider:
1. Register for API access at: https://boardgamegeek.com/using_the_xml_api
2. Obtain your Bearer token from BGG
3. Set BGG_API_TOKEN environment variable with your token

API Documentation: https://boardgamegeek.com/wiki/page/BGG_XML_API2
Registration & Authorization: https://boardgamegeek.com/using_the_xml_api
API Terms: https://boardgamegeek.com/wiki/page/XML_API_Terms_of_Use

Rate Limiting: BGG recommends waiting 5 seconds between requests
to avoid 500/503 errors.
"""

import logging
import time

import requests
from defusedxml import ElementTree

from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://boardgamegeek.com/xmlapi2"
RESULTS_PER_PAGE = 20

# Rate limiting: Disabled - BGG handles rate limiting on their end
# If you encounter 429/500/503 errors, enable by setting MIN_REQUEST_INTERVAL > 0
MIN_REQUEST_INTERVAL = 0  # seconds
_rate_limit_state = {"last_request_time": 0}

# HTTP status codes
HTTP_UNAUTHORIZED = 401
HTTP_ACCEPTED = 202


def _rate_limit():
    """Ensure minimum time between BGG API requests."""
    current_time = time.time()
    time_since_last = current_time - _rate_limit_state["last_request_time"]

    if time_since_last < MIN_REQUEST_INTERVAL:
        sleep_time = MIN_REQUEST_INTERVAL - time_since_last
        time.sleep(sleep_time)

    _rate_limit_state["last_request_time"] = time.time()


def _bgg_request(endpoint, params=None):
    """Make a rate-limited request to BGG API.

    Args:
        endpoint: API endpoint (e.g., 'search', 'thing')
        params: Query parameters dict

    Returns:
        ElementTree root element

    Raises:
        ProviderAPIError: On API errors
    """
    _rate_limit()

    url = f"{base_url}/{endpoint}"
    headers = {
        "User-Agent": "Yamtrack/1.0 (https://github.com/FuzzyGrim/Yamtrack)",
    }

    bgg_token = getattr(settings, "BGG_API_TOKEN", None)
    if bgg_token:
        headers["Authorization"] = f"Bearer {bgg_token}"

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)

        if response.status_code == HTTP_UNAUTHORIZED:
            logger.error(
                "BGG API requires authorization. Register at "
                "https://boardgamegeek.com/using_the_xml_api "
                "and set BGG_API_TOKEN in your environment.",
            )

        response.raise_for_status()

        if response.status_code == HTTP_ACCEPTED:
            logger.info("BGG queued request, retrying...")
            time.sleep(2)
            return _bgg_request(endpoint, params)
    except requests.exceptions.HTTPError as error:
        raise services.ProviderAPIError(Sources.BGG.value, error) from error
    except ElementTree.ParseError as error:
        logger.exception("Failed to parse BGG XML response")
        raise services.ProviderAPIError(
            Sources.BGG.value,
            error,
            "Invalid XML response from BGG",
        ) from error
    else:
        return ElementTree.fromstring(response.text)


def _fetch_thumbnails(page_ids):
    """Fetch thumbnail images for a list of game IDs.

    Args:
        page_ids: List of BGG game IDs

    Returns:
        Dict mapping game_id to image URL
    """
    if not page_ids:
        return {}

    try:
        thing_params = {
            "id": ",".join(page_ids),
            # Don't filter by type - allows BGG to return expansions/accessories too
        }
        thing_root = _bgg_request("thing", thing_params)

        thumbnails = {}
        for item in thing_root.findall(".//item"):
            game_id = item.get("id")
            # Try thumbnail first, fall back to full image if no thumbnail
            thumbnail_elem = item.find("thumbnail")
            if thumbnail_elem is not None and thumbnail_elem.text:
                thumbnails[game_id] = thumbnail_elem.text
            else:
                image_elem = item.find("image")
                if image_elem is not None and image_elem.text:
                    thumbnails[game_id] = image_elem.text
    except services.ProviderAPIError:
        logger.warning("Failed to fetch thumbnails from BGG")
        return {}
    else:
        return thumbnails


def _build_search_results(page_ids, game_names, thumbnails):
    """Build search result list from game data.

    Args:
        page_ids: List of game IDs for current page
        game_names: Dict mapping game_id to name
        thumbnails: Dict mapping game_id to image URL

    Returns:
        List of search result dicts
    """
    return [
        {
            "media_id": game_id,
            "source": Sources.BGG.value,
            "media_type": MediaTypes.BOARDGAME.value,
            "title": game_names[game_id],
            "image": thumbnails.get(game_id, settings.IMG_NONE),
        }
        for game_id in page_ids
    ]


def search(query, page=1):
    """Search for board games on BoardGameGeek.

    Args:
        query: Search term
        page: Page number for client-side pagination

    Returns:
        Formatted search response with results
    """
    # Cache all game IDs separately from page-specific results
    ids_cache_key = f"bgg_search_ids_{query.lower()}"
    game_data = cache.get(ids_cache_key)

    if not game_data:
        # First search or cache expired - fetch from BGG
        params = {
            "query": query,
            "type": "boardgame",
        }
        root = _bgg_request("search", params)

        game_ids = []
        game_names = {}
        for item in root.findall(".//item"):
            game_id = item.get("id")
            name_elem = item.find("name")
            if name_elem is not None and game_id:
                game_ids.append(game_id)
                game_names[game_id] = name_elem.get("value", "Unknown")

        game_data = {"ids": game_ids, "names": game_names}
        cache.set(ids_cache_key, game_data, 60 * 60 * 24)

    game_ids = game_data["ids"]
    game_names = game_data["names"]

    page_cache_key = f"bgg_search_page_{query.lower()}_p{page}"
    cached_page = cache.get(page_cache_key)
    if cached_page:
        return cached_page

    total_results = len(game_ids)
    start_idx = (page - 1) * RESULTS_PER_PAGE
    end_idx = start_idx + RESULTS_PER_PAGE
    page_ids = game_ids[start_idx:end_idx]

    # Fetch thumbnails only for current page
    thumbnails = _fetch_thumbnails(page_ids)
    results = _build_search_results(page_ids, game_names, thumbnails)

    data = helpers.format_search_response(
        page=page,
        per_page=RESULTS_PER_PAGE,
        total_results=total_results,
        results=results,
    )

    cache.set(page_cache_key, data, 60 * 60 * 24)

    return data


def metadata(media_id):
    """Get detailed metadata for a board game.

    Args:
        media_id: BGG thing ID

    Returns:
        Dict with game details
    """
    cache_key = f"bgg_metadata_{media_id}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    params = {
        "id": media_id,
        "stats": "1",
    }

    root = _bgg_request("thing", params)

    item = root.find(".//item")
    if item is None:
        raise services.ProviderAPIError(
            Sources.BGG.value,
            None,
            f"Game not found: {media_id}",
        )

    # Extract primary name
    name_elem = item.find(".//name[@type='primary']")
    title = name_elem.get("value", "Unknown") if name_elem is not None else "Unknown"

    # Extract image
    image_elem = item.find("image")
    image = image_elem.text if image_elem is not None else settings.IMG_NONE

    # Extract description
    desc_elem = item.find("description")
    description = desc_elem.text if desc_elem is not None else ""

    # Extract year
    year_elem = item.find("yearpublished")
    year = year_elem.get("value", "") if year_elem is not None else ""

    # Extract player counts
    minplayers_elem = item.find("minplayers")
    maxplayers_elem = item.find("maxplayers")
    minplayers = minplayers_elem.get("value", "") if minplayers_elem is not None else ""
    maxplayers = maxplayers_elem.get("value", "") if maxplayers_elem is not None else ""

    # Extract playtime
    playtime_elem = item.find("playingtime")
    playtime = playtime_elem.get("value", "") if playtime_elem is not None else ""

    # Extract minimum age
    minage_elem = item.find("minage")
    minage = minage_elem.get("value", "") if minage_elem is not None else ""

    # Extract BGG rating
    avg_rating_elem = item.find(".//statistics/ratings/average")
    avg_rating = avg_rating_elem.get("value", "") if avg_rating_elem is not None else ""

    result = {
        "media_id": media_id,
        "source": Sources.BGG.value,
        "media_type": MediaTypes.BOARDGAME.value,
        "title": title,
        "image": image,
        "description": description,
        "year": year,
        "players": f"{minplayers}-{maxplayers}" if minplayers and maxplayers else "",
        "playtime": f"{playtime} min" if playtime else "",
        "age": f"{minage}+" if minage else "",
        "bgg_rating": avg_rating,
        # Board games don't have max progress - tracks plays instead
        "max_progress": None,
        "related": {},
    }

    cache.set(cache_key, result, 60 * 60 * 24 * 7)
    return result
