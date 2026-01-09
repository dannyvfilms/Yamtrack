"""BoardGameGeek (BGG) API provider for board game metadata."""
import logging
import time

import defusedxml.ElementTree as ET
import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

BASE_URL = "https://boardgamegeek.com/xmlapi2"
MIN_REQUEST_INTERVAL = 0  # seconds; adjust if rate limiting hits
_last_request_time = 0


def _rate_limit():
    """Ensure minimum time between BGG API requests."""
    global _last_request_time
    current_time = time.time()
    elapsed = current_time - _last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()


def _bgg_request(endpoint, params=None):
    """Make a rate-limited request to the BGG API and return the parsed XML root."""
    _rate_limit()
    url = f"{BASE_URL}/{endpoint}"
    headers = {
        "User-Agent": "Yamtrack/1.0 (https://github.com/FuzzyGrim/Yamtrack)",
    }

    bgg_token = getattr(settings, "BGG_API_TOKEN", None)
    if bgg_token:
        headers["Authorization"] = f"Bearer {bgg_token}"

    try:
        response = requests.get(url, params=params, headers=headers, timeout=10)

        if response.status_code == 401:
            logger.error(
                "BGG API requires authorization. Register at "
                "https://boardgamegeek.com/using_the_xml_api and set BGG_API_TOKEN.",
            )

        response.raise_for_status()

        # BGG sometimes queues requests with 202 responses
        if response.status_code == 202:
            logger.info("BGG queued request, retrying...")
            time.sleep(2)
            return _bgg_request(endpoint, params)

        return ET.fromstring(response.text)
    except requests.exceptions.HTTPError as error:
        raise services.ProviderAPIError(Sources.BGG.value, error) from error
    except ET.ParseError as error:
        logger.exception("Failed to parse BGG XML response")
        raise services.ProviderAPIError(
            Sources.BGG.value,
            error,
            "Invalid XML response from BGG",
        ) from error


def search(query, page=1):
    """Search for board games on BoardGameGeek."""
    ids_cache_key = f"bgg_search_ids_{query.lower()}"
    game_data = cache.get(ids_cache_key)

    if not game_data:
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

    per_page = 20
    total_results = len(game_ids)
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_ids = game_ids[start_idx:end_idx]

    results = []
    if page_ids:
        try:
            thing_params = {
                "id": ",".join(page_ids),
            }
            thing_root = _bgg_request("thing", thing_params)

            thumbnails = {}
            years = {}
            for item in thing_root.findall(".//item"):
                game_id = item.get("id")
                thumbnail_elem = item.find("thumbnail")
                if thumbnail_elem is not None and thumbnail_elem.text:
                    thumbnails[game_id] = thumbnail_elem.text
                else:
                    image_elem = item.find("image")
                    if image_elem is not None and image_elem.text:
                        thumbnails[game_id] = image_elem.text
                year_elem = item.find("yearpublished")
                if year_elem is not None and year_elem.get("value"):
                    years[game_id] = year_elem.get("value")

            for game_id in page_ids:
                results.append(
                    {
                        "media_id": game_id,
                        "source": Sources.BGG.value,
                        "media_type": MediaTypes.BOARDGAME.value,
                        "title": game_names.get(game_id, "Unknown"),
                        "image": thumbnails.get(game_id, settings.IMG_NONE),
                        "year": years.get(game_id),
                    },
                )
        except Exception as exc:
            logger.warning("Failed to fetch thumbnails: %s", exc)
            for game_id in page_ids:
                results.append(
                    {
                        "media_id": game_id,
                        "source": Sources.BGG.value,
                        "media_type": MediaTypes.BOARDGAME.value,
                        "title": game_names.get(game_id, "Unknown"),
                        "image": settings.IMG_NONE,
                    },
                )

    data = helpers.format_search_response(
        page=page,
        per_page=per_page,
        total_results=total_results,
        results=results,
    )
    cache.set(page_cache_key, data, 60 * 60 * 24)
    return data


def metadata(media_id):
    """Get detailed metadata for a board game."""
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

    name_elem = item.find(".//name[@type='primary']")
    title = name_elem.get("value", "Unknown") if name_elem is not None else "Unknown"

    image_elem = item.find("image")
    image = image_elem.text if image_elem is not None else settings.IMG_NONE

    desc_elem = item.find("description")
    description = desc_elem.text if desc_elem is not None else ""

    year_elem = item.find("yearpublished")
    year = year_elem.get("value", "") if year_elem is not None else ""

    minplayers_elem = item.find("minplayers")
    maxplayers_elem = item.find("maxplayers")
    minplayers = minplayers_elem.get("value", "") if minplayers_elem is not None else ""
    maxplayers = maxplayers_elem.get("value", "") if maxplayers_elem is not None else ""

    playtime_elem = item.find("playingtime")
    playtime = playtime_elem.get("value", "") if playtime_elem is not None else ""

    minage_elem = item.find("minage")
    minage = minage_elem.get("value", "") if minage_elem is not None else ""

    avg_rating_elem = item.find(".//statistics/ratings/average")
    avg_rating = avg_rating_elem.get("value", "") if avg_rating_elem is not None else ""

    publish_date = None
    if year:
        publish_date = f"{year}-01-01"

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
        "max_progress": None,
        "related": {},
        "details": {
            "publish_date": publish_date,
        },
    }

    cache.set(cache_key, result, 60 * 60 * 24 * 7)
    return result
