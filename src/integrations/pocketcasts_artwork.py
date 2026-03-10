"""Fetch podcast artwork from alternative public sources.

Since Pocket Casts image URLs require authentication, we fetch artwork
from public APIs that don't require auth: RSS feeds, Podcast Index, and iTunes.
"""

import logging
import xml.etree.ElementTree as ET

import requests
from django.core.cache import cache

from app.log_safety import exception_summary, safe_url

logger = logging.getLogger(__name__)

PODCAST_INDEX_API_BASE = "https://api.podcastindex.org/api/1.0"
ITUNES_API_BASE = "https://itunes.apple.com/search"
USER_AGENT = "Yamtrack/1.0 (https://github.com/FuzzyGrim/Yamtrack)"


def fetch_podcast_artwork(
    podcast_uuid: str,
    show_title: str,
    author: str | None = None,
    rss_feed_url: str | None = None,
) -> str | None:
    """Fetch podcast artwork from alternative public sources.
    
    Tries multiple sources in order:
    1. RSS feed (if provided) - most reliable
    2. Podcast Index API - free, no auth, good coverage
    3. iTunes API - legacy but still works for many podcasts
    
    Args:
        podcast_uuid: Pocket Casts podcast UUID (for caching)
        show_title: Podcast show title
        author: Podcast author/network (optional, helps with search)
        rss_feed_url: RSS feed URL if available (optional)
        
    Returns:
        Image URL string or None if not found
    """
    # Check cache first
    cache_key = f"podcast_artwork_{podcast_uuid}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached else None

    image_url = None

    # Try RSS feed first (most reliable if available)
    if rss_feed_url:
        image_url = _fetch_from_rss_feed(rss_feed_url)
        if image_url:
            cache.set(cache_key, image_url, 60 * 60 * 24 * 7)  # Cache for 7 days
            return image_url

    # Try Podcast Index API
    if not image_url:
        image_url = _fetch_from_podcast_index(show_title, author)
        if image_url:
            cache.set(cache_key, image_url, 60 * 60 * 24 * 7)  # Cache for 7 days
            return image_url

    # Try iTunes API as fallback
    if not image_url:
        image_url = _fetch_from_itunes(show_title, author)
        if image_url:
            cache.set(cache_key, image_url, 60 * 60 * 24 * 7)  # Cache for 7 days
            return image_url

    # Cache the miss to avoid repeated failed lookups
    cache.set(cache_key, "", 60 * 60 * 24)  # Cache miss for 1 day
    return None


def fetch_podcast_artwork_and_rss(
    show_title: str,
    author: str | None = None,
) -> tuple[str | None, str | None]:
    """Fetch podcast artwork and RSS feed URL from iTunes API in a single call.
    
    Args:
        show_title: Podcast show title
        author: Podcast author (optional, helps narrow search)
        
    Returns:
        Tuple of (artwork_url, rss_feed_url) or (None, None) if not found
    """
    try:
        # Build search query
        if author:
            query = f"{show_title} {author}"
        else:
            query = show_title

        # iTunes API expects URL-encoded query
        params = {
            "term": query,
            "media": "podcast",
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
            return None, None

        # Try to find best match by title
        show_title_lower = show_title.lower()
        for result in results:
            result_title = result.get("collectionName", "").lower()
            # Check if titles are similar (exact match or one contains the other)
            if (
                result_title == show_title_lower
                or show_title_lower in result_title
                or result_title in show_title_lower
            ):
                artwork_url = result.get("artworkUrl600") or result.get("artworkUrl100")
                feed_url = result.get("feedUrl")
                if artwork_url or feed_url:
                    logger.debug("Found iTunes match for %s: artwork=%s, feed=%s", show_title, bool(artwork_url), bool(feed_url))
                    return artwork_url, feed_url

        # If no exact match, use first result
        if results:
            artwork_url = results[0].get("artworkUrl600") or results[0].get("artworkUrl100")
            feed_url = results[0].get("feedUrl")
            if artwork_url or feed_url:
                logger.debug("Using first iTunes result for %s: artwork=%s, feed=%s", show_title, bool(artwork_url), bool(feed_url))
                return artwork_url, feed_url

    except Exception as e:
        logger.debug("Failed to fetch from iTunes for %s: %s", show_title, e)

    return None, None


def _fetch_from_rss_feed(rss_feed_url: str) -> str | None:
    """Fetch artwork from RSS feed.
    
    Args:
        rss_feed_url: URL to the podcast RSS feed
        
    Returns:
        Image URL or None
    """
    try:
        response = requests.get(rss_feed_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        response.raise_for_status()

        # Parse XML - handle both RSS and Atom feeds
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as e:
            logger.debug("Failed to parse RSS feed XML: %s", e)
            return None

        # RSS 2.0 format: <channel><image><url>
        # Also check <itunes:image> and <image><url>
        namespaces = {
            "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
            "atom": "http://www.w3.org/2005/Atom",
        }

        # Try iTunes image first (usually highest quality)
        # Can be in channel or item level
        for prefix in ["", ".//channel/"]:
            itunes_image = root.find(f"{prefix}itunes:image", namespaces)
            if itunes_image is not None:
                href = itunes_image.get("href")
                if href:
                    return href

        # Try RSS image element (channel/image/url)
        image_elem = root.find(".//channel/image/url")
        if image_elem is not None and image_elem.text:
            return image_elem.text.strip()

        # Try generic image/url (some feeds use this)
        image_elem = root.find(".//image/url")
        if image_elem is not None and image_elem.text:
            return image_elem.text.strip()

        # Try Atom feed logo (atom:logo)
        atom_logo = root.find(".//atom:logo", namespaces)
        if atom_logo is not None and atom_logo.text:
            return atom_logo.text.strip()

    except Exception as e:
        logger.debug(
            "Failed to fetch artwork from RSS feed %s: %s",
            safe_url(rss_feed_url),
            exception_summary(e),
        )

    return None


def _fetch_from_podcast_index(show_title: str, author: str | None = None) -> str | None:
    """Fetch artwork from Podcast Index API.
    
    Podcast Index API is free but requires API key. For now, we'll skip it
    and rely on iTunes. If we want to use it later, we'd need to add API keys.
    
    Args:
        show_title: Podcast show title
        author: Podcast author (optional)
        
    Returns:
        Image URL or None
    """
    # Podcast Index requires API key/secret, so we'll skip for now
    # If we want to add it later, we'd need:
    # - API key and secret in settings
    # - Generate auth header (X-Auth-Key, X-Auth-Date, Authorization)
    # - Search endpoint: /search/byterm?q={query}
    return None


def _fetch_from_itunes(show_title: str, author: str | None = None) -> str | None:
    """Fetch artwork from iTunes/Apple Podcasts API.
    
    Args:
        show_title: Podcast show title
        author: Podcast author (optional, helps narrow search)
        
    Returns:
        Image URL or None
    """
    try:
        # Build search query
        if author:
            query = f"{show_title} {author}"
        else:
            query = show_title

        # iTunes API expects URL-encoded query
        params = {
            "term": query,
            "media": "podcast",
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
            return None

        # Try to find best match by title
        # iTunes sometimes returns slightly different titles, so we do fuzzy matching
        show_title_lower = show_title.lower()
        for result in results:
            result_title = result.get("collectionName", "").lower()
            # Check if titles are similar (exact match or one contains the other)
            if (
                result_title == show_title_lower
                or show_title_lower in result_title
                or result_title in show_title_lower
            ):
                artwork_url = result.get("artworkUrl600") or result.get("artworkUrl100")
                if artwork_url:
                    # Replace with higher resolution if available
                    # artworkUrl600 is 600x600, but we can get 1400x1400 by replacing dimensions
                    if "artworkUrl600" in result:
                        # Try to get larger version
                        large_url = artwork_url.replace("600x600", "1400x1400")
                        # Verify it exists (or just use 600x600 which is usually fine)
                        return artwork_url
                    return artwork_url

        # If no exact match, use first result's artwork
        if results:
            artwork_url = results[0].get("artworkUrl600") or results[0].get("artworkUrl100")
            if artwork_url:
                return artwork_url

    except Exception as e:
        logger.debug("Failed to fetch artwork from iTunes for %s: %s", show_title, e)

    return None
