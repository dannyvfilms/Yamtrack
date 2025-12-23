"""Pocket Casts provider for podcast search using iTunes API."""

import logging

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

ITUNES_API_BASE = "https://itunes.apple.com/search"
ITUNES_LOOKUP_BASE = "https://itunes.apple.com/lookup"
USER_AGENT = "Yamtrack/1.0 (https://github.com/FuzzyGrim/Yamtrack)"


def handle_error(error):
    """Handle iTunes API errors."""
    raise services.ProviderAPIError(
        Sources.POCKETCASTS.value,
        error,
    )


def search(query, page):
    """Search for podcasts using iTunes API."""
    cache_key = (
        f"search_{Sources.POCKETCASTS.value}_{MediaTypes.PODCAST.value}_{query}_{page}"
    )
    data = cache.get(cache_key)

    if data is None:
        # Calculate offset for pagination
        per_page = settings.PER_PAGE
        offset = (page - 1) * per_page

        params = {
            "term": query,
            "media": "podcast",
            "limit": per_page,
            "offset": offset,
        }

        try:
            response = requests.get(
                ITUNES_API_BASE,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=settings.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            response_data = response.json()
        except requests.RequestException as e:
            handle_error(e)

        results = []
        for item in response_data.get("results", []):
            # Use collectionId as media_id (preferred) or trackId as fallback
            media_id = str(item.get("collectionId") or item.get("trackId", ""))
            if not media_id:
                continue

            # Get title from collectionName (preferred) or trackName
            title = item.get("collectionName") or item.get("trackName", "Unknown Podcast")

            # Get image URL - prefer artworkUrl600, fallback to artworkUrl100
            image = item.get("artworkUrl600") or item.get("artworkUrl100") or settings.IMG_NONE

            results.append(
                {
                    "media_id": media_id,
                    "source": Sources.POCKETCASTS.value,
                    "media_type": MediaTypes.PODCAST.value,
                    "title": title,
                    "image": image,
                },
            )

        total_results = response_data.get("resultCount", len(results))
        data = helpers.format_search_response(
            page,
            per_page,
            total_results,
            results,
        )

        cache.set(cache_key, data)
    return data


def lookup_by_itunes_id(itunes_collection_id):
    """Look up podcast metadata by iTunes collection ID.
    
    Args:
        itunes_collection_id: iTunes collection ID (string)
        
    Returns:
        Dict with podcast metadata:
        - feed_url: RSS feed URL
        - title: Podcast title
        - author: Podcast author
        - artwork_url: Artwork image URL
        - description: Podcast description
        - genres: List of genres
    """
    cache_key = f"itunes_lookup_{itunes_collection_id}"
    data = cache.get(cache_key)

    if data is None:
        params = {
            "id": itunes_collection_id,
        }

        try:
            response = requests.get(
                ITUNES_LOOKUP_BASE,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=settings.REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            response_data = response.json()
        except requests.RequestException as e:
            handle_error(e)

        results = response_data.get("results", [])
        if not results:
            raise services.ProviderAPIError(
                Sources.POCKETCASTS.value,
                type("obj", (object,), {"response": type("obj", (object,), {"status_code": 404, "text": "Podcast not found"})()}),
                "Podcast not found in iTunes",
            )

        # Get the first result (should be the collection/podcast)
        item = results[0]

        # Extract metadata
        # iTunes description might be HTML, we'll clean it if needed
        description = item.get("description", "") or item.get("longDescription", "")

        data = {
            "feed_url": item.get("feedUrl", ""),
            "title": item.get("collectionName") or item.get("trackName", "Unknown Podcast"),
            "author": item.get("artistName", ""),
            "artwork_url": item.get("artworkUrl600") or item.get("artworkUrl100") or "",
            "description": description,
            "genres": item.get("genres", []),
            "language": item.get("primaryLanguageName", ""),
        }

        # Cache for 7 days
        cache.set(cache_key, data, 60 * 60 * 24 * 7)

    return data
