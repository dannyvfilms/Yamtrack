"""Shared helpers for Last.fm incremental sync and history backfills."""

import logging

from integrations import lastfm_api
from integrations.webhooks.lastfm import LastFMScrobbleProcessor

logger = logging.getLogger(__name__)

LASTFM_HISTORY_IMPORT_LOCK_PREFIX = "lastfm_history_import_lock:"


def get_lastfm_history_import_lock_key(user_id: int) -> str:
    """Return the cache lock key for a user's Last.fm history import."""
    return f"{LASTFM_HISTORY_IMPORT_LOCK_PREFIX}{user_id}"


def _track_timestamp(track_data: dict) -> int:
    """Return the scrobble timestamp for stable oldest-first processing."""
    try:
        return int((track_data.get("date") or {}).get("uts") or 0)
    except (TypeError, ValueError):
        return 0


def sync_lastfm_account(
    account,
    *,
    from_timestamp_uts: int | None = None,
    to_timestamp_uts: int | None = None,
    page_start: int = 1,
    max_pages: int | None = None,
    fast_mode: bool = False,
) -> dict:
    """Fetch and process Last.fm scrobbles for a single account."""
    fetch_result = lastfm_api.get_recent_tracks_window(
        username=account.lastfm_username,
        from_timestamp_uts=from_timestamp_uts,
        to_timestamp_uts=to_timestamp_uts,
        extended=1,
        page_start=page_start,
        max_pages=max_pages,
    )

    tracks = sorted(fetch_result.tracks, key=_track_timestamp)
    processor = LastFMScrobbleProcessor()
    stats = processor.process_tracks(tracks, account.user, fast_mode=fast_mode)

    result = {
        "tracks": tracks,
        "pages_fetched": fetch_result.pages_fetched,
        "total_pages": fetch_result.total_pages,
        "complete": fetch_result.complete,
        "interrupted": fetch_result.interrupted,
        "max_seen_uts": fetch_result.max_seen_uts,
        **stats,
    }
    logger.debug(
        (
            "Last.fm sync result for user %s: pages=%s/%s "
            "complete=%s processed=%s skipped=%s errors=%s"
        ),
        account.user.username,
        fetch_result.pages_fetched,
        fetch_result.total_pages,
        fetch_result.complete,
        stats["processed"],
        stats["skipped"],
        stats["errors"],
    )
    return result
