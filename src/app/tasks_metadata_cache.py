"""Provider metadata cache helpers and item metadata fetch utilities.

Extracted from tasks.py. Re-exported from app.tasks for backward compatibility.
"""

import logging

from django.core.cache import cache

from app.log_safety import exception_summary
from app.models import Item, MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)


def _exception_with_details(exc: Exception) -> str:
    """Return a compact exception summary that preserves the message when present."""
    summary = exception_summary(exc)
    details = str(exc).strip()
    if details and details != summary:
        return f"{summary}: {details}"
    return summary


def _metadata_cache_keys_for_item(item: Item):
    keys = {
        f"{item.source}_{item.media_type}_{item.media_id}",
    }
    if item.source == Sources.TMDB.value and item.media_type == MediaTypes.SEASON.value and item.season_number:
        keys.add(f"{item.source}_{item.media_type}_{item.media_id}_{item.season_number}")
    if (
        item.source == Sources.TMDB.value
        and item.media_type == MediaTypes.EPISODE.value
        and item.season_number
        and item.episode_number
    ):
        keys.add(
            f"{item.source}_{item.media_type}_{item.media_id}_{item.season_number}_{item.episode_number}",
        )
    if item.source == Sources.BGG.value and item.media_type == MediaTypes.BOARDGAME.value:
        keys.add(f"bgg_metadata_{item.media_id}")
    if item.source == Sources.MUSICBRAINZ.value and item.media_type == MediaTypes.MUSIC.value:
        keys.add(f"musicbrainz_recording_{item.media_id}")
    return [key for key in keys if key]


def _clear_item_metadata_cache(item: Item):
    keys = _metadata_cache_keys_for_item(item)
    if not keys:
        return
    try:
        cache.delete_many(keys)
    except Exception:  # pragma: no cover - cache backends may not support delete_many
        for key in keys:
            try:
                cache.delete(key)
            except Exception:
                continue


def _fetch_item_metadata(item: Item):
    if item.media_type == MediaTypes.SEASON.value:
        if item.season_number is None:
            raise ValueError("season item missing season_number")
        return services.get_media_metadata(
            item.media_type,
            item.media_id,
            item.source,
            [item.season_number],
        )
    if item.media_type == MediaTypes.EPISODE.value:
        if item.season_number is None or item.episode_number is None:
            raise ValueError("episode item missing season_number or episode_number")
        return services.get_media_metadata(
            item.media_type,
            item.media_id,
            item.source,
            [item.season_number],
            item.episode_number,
        )
    return services.get_media_metadata(
        item.media_type,
        item.media_id,
        item.source,
    )
