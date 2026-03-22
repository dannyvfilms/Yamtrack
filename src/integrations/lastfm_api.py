"""Last.fm API client for fetching user scrobbles."""

from dataclasses import dataclass
import hashlib
import logging
import random
import time
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "Yamtrack/1.0 (https://github.com/FuzzyGrim/Yamtrack)"


class LastFMAPIError(Exception):
    """Base exception for Last.fm API errors."""

    pass


class LastFMRateLimitError(LastFMAPIError):
    """Raised when rate limit is exceeded (error code 29)."""

    pass


class LastFMClientError(LastFMAPIError):
    """Raised for client errors (invalid user, etc.)."""

    pass


@dataclass
class LastFMRecentTracksResult:
    """Structured response for paginated recent-track fetches."""

    tracks: list[dict[str, Any]]
    pages_fetched: int
    total_pages: int
    complete: bool
    interrupted: bool = False
    max_seen_uts: int | None = None


def _make_api_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Make a request to Last.fm API with rate limit handling."""
    api_key = getattr(settings, "LASTFM_API_KEY", None)
    if not api_key:
        raise LastFMAPIError("LASTFM_API_KEY not configured in settings")

    # Add required parameters
    params["method"] = method
    params["api_key"] = api_key
    params["format"] = "json"

    url = LASTFM_API_BASE
    headers = {"User-Agent": USER_AGENT}

    max_retries = 3
    retry_delay = 1

    for attempt in range(max_retries):
        try:
            start_time = time.time()
            response = requests.get(url, params=params, headers=headers, timeout=10)
            duration = time.time() - start_time

            # Log request metadata (not full payload)
            logger.debug(
                "Last.fm API request: method=%s, status=%s, duration=%.2fs, params_keys=%s",
                method,
                response.status_code,
                duration,
                list(params.keys()),
            )

            response.raise_for_status()
            data = response.json()

            # Check for API errors in response
            if "error" in data:
                error_code = data.get("error")
                error_message = data.get("message", "Unknown error")

                logger.warning(
                    "Last.fm API error: code=%s, message=%s, method=%s",
                    error_code,
                    error_message,
                    method,
                )

                # Handle rate limit specifically
                if error_code == 29:
                    if attempt < max_retries - 1:
                        # Exponential backoff with jitter
                        delay = retry_delay * (2 ** attempt) + random.uniform(0, 1)
                        logger.info(
                            "Rate limit exceeded, retrying after %.2fs (attempt %d/%d)",
                            delay,
                            attempt + 1,
                            max_retries,
                        )
                        time.sleep(delay)
                        retry_delay = delay
                        continue
                    raise LastFMRateLimitError(f"Rate limit exceeded: {error_message}")

                # Handle invalid user (code 6)
                if error_code == 6:
                    raise LastFMClientError(f"User not found: {error_message}")

                # Other errors
                raise LastFMAPIError(f"API error {error_code}: {error_message}")

            return data

        except requests.exceptions.RequestException as e:
            logger.error("Last.fm API request failed: %s", e)
            if attempt < max_retries - 1:
                delay = retry_delay * (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
                retry_delay = delay
                continue
            raise LastFMAPIError(f"Request failed: {e}") from e

    raise LastFMAPIError("Max retries exceeded")


def get_recent_tracks(
    username: str,
    from_timestamp_uts: int | None = None,
    to_timestamp_uts: int | None = None,
    limit: int = 200,
    page: int = 1,
    extended: int = 1,
) -> dict[str, Any]:
    """Fetch recent tracks for a user.

    Args:
        username: Last.fm username
        from_timestamp_uts: Unix timestamp (seconds) to fetch tracks from (optional)
        to_timestamp_uts: Unix timestamp (seconds) upper bound for fetches (optional)
        limit: Maximum tracks per page (max 200)
        page: Page number (1-indexed)
        extended: Include extended metadata (1 = yes, 0 = no)

    Returns:
        Dict with 'recenttracks' key containing track list and '@attr' metadata

    Raises:
        LastFMAPIError: For API errors
        LastFMRateLimitError: For rate limit errors
        LastFMClientError: For client errors (invalid user)
    """
    params: dict[str, Any] = {
        "user": username,
        "limit": min(limit, 200),  # Enforce max limit
        "page": page,
        "extended": extended,
    }

    if from_timestamp_uts is not None:
        params["from"] = from_timestamp_uts
    if to_timestamp_uts is not None:
        params["to"] = to_timestamp_uts

    return _make_api_request("user.getRecentTracks", params)


def get_recent_tracks_window(
    username: str,
    from_timestamp_uts: int | None = None,
    to_timestamp_uts: int | None = None,
    extended: int = 1,
    page_start: int = 1,
    max_pages: int | None = None,
) -> LastFMRecentTracksResult:
    """Fetch a recent-track window for a user, handling pagination.

    Args:
        username: Last.fm username
        from_timestamp_uts: Unix timestamp (seconds) to fetch tracks from (optional)
        to_timestamp_uts: Unix timestamp (seconds) upper bound for fetches (optional)
        extended: Include extended metadata (1 = yes, 0 = no)
        page_start: Page number to begin fetching from
        max_pages: Maximum number of pages to fetch in this call

    Returns:
        Structured page result with completion metadata

    Raises:
        LastFMAPIError: For API errors
        LastFMRateLimitError: For rate limit errors
        LastFMClientError: For client errors (invalid user)
    """
    all_tracks = []
    page = max(page_start, 1)
    total_pages = None
    pages_fetched = 0
    complete = True
    interrupted = False
    max_seen_uts = None

    while True:
        try:
            data = get_recent_tracks(
                username=username,
                from_timestamp_uts=from_timestamp_uts,
                to_timestamp_uts=to_timestamp_uts,
                limit=200,
                page=page,
                extended=extended,
            )

            recenttracks = data.get("recenttracks", {})
            tracks = recenttracks.get("track", [])

            # Handle single track (API returns dict) vs multiple tracks (list)
            if isinstance(tracks, dict):
                tracks = [tracks]

            # Get pagination info
            attr = recenttracks.get("@attr", {})
            if total_pages is None:
                total_pages = int(attr.get("totalPages", 1))
                logger.debug(
                    "Last.fm pagination: total_pages=%d, username=%s",
                    total_pages,
                    username,
                )

            all_tracks.extend(tracks)
            pages_fetched += 1

            for track in tracks:
                date_attr = track.get("date", {})
                date_uts = date_attr.get("uts")
                if not date_uts:
                    continue
                try:
                    track_timestamp = int(date_uts)
                except (TypeError, ValueError):
                    continue
                if max_seen_uts is None or track_timestamp > max_seen_uts:
                    max_seen_uts = track_timestamp

            # Check if we've fetched all pages
            current_page = int(attr.get("page", page))
            if current_page >= total_pages or not tracks:
                break

            if max_pages is not None and pages_fetched >= max_pages:
                complete = False
                break

            page += 1

            # Small delay between pages to be respectful
            time.sleep(0.2)

        except LastFMRateLimitError:
            # Re-raise rate limit errors immediately
            raise
        except Exception as e:
            logger.error(
                "Error fetching page %d for user %s: %s",
                page,
                username,
                e,
            )
            # If we got some tracks, return what we have
            if all_tracks:
                logger.warning(
                    "Returning partial results (%d tracks) due to error",
                    len(all_tracks),
                )
                complete = False
                interrupted = True
                break
            raise

    logger.info(
        "Fetched %d tracks across %d pages for user %s (complete=%s)",
        len(all_tracks),
        pages_fetched,
        username,
        complete,
    )

    return LastFMRecentTracksResult(
        tracks=all_tracks,
        pages_fetched=pages_fetched,
        total_pages=total_pages or 1,
        complete=complete,
        interrupted=interrupted,
        max_seen_uts=max_seen_uts,
    )


def get_all_recent_tracks(
    username: str,
    from_timestamp_uts: int | None = None,
    to_timestamp_uts: int | None = None,
    extended: int = 1,
) -> list[dict[str, Any]]:
    """Fetch all recent tracks for a user (handles pagination)."""
    result = get_recent_tracks_window(
        username=username,
        from_timestamp_uts=from_timestamp_uts,
        to_timestamp_uts=to_timestamp_uts,
        extended=extended,
    )
    return result.tracks
