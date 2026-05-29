"""Row cache schema versioning and cache compatibility checks."""

from __future__ import annotations

from app.discover import tab_cache
from app.discover.artwork import (
    PROVIDER_ARTWORK_HYDRATION_ROW_KEYS,
    _is_missing_image,
    _supports_provider_artwork_hydration,
)
from app.discover.schemas import RowDefinition, RowResult
from app.discover.service_helpers import MAX_ITEMS_PER_ROW
from app.discover.trakt_candidates import (
    MOVIE_CANON_ROW_SCHEMA_VERSION,
    MOVIE_COMING_SOON_ROW_SCHEMA_VERSION,
    ROW_CACHE_SCHEMA_META_KEY,
)
from app.models import MediaTypes

ROW_CACHE_ACTIVITY_VERSION_META_KEY = "activity_version"

MOVIE_PERSONALIZED_ROW_SCHEMA_VERSION = 6
TV_ANIME_TRAKT_ROW_SCHEMA_VERSION = 1
TV_ANIME_PERSONALIZED_ROW_SCHEMA_VERSION = 4

MOVIE_PERSONALIZED_ROW_KEYS = {
    "top_picks_for_you",
    "comfort_rewatches",
}
TV_ANIME_PERSONALIZED_ROW_KEYS = {
    "top_picks_for_you",
    "clear_out_next",
    "comfort_rewatches",
}


def _apply_row_definition_metadata(
    row: RowResult,
    row_definition: RowDefinition,
) -> RowResult:
    """Keep display metadata aligned with the current registry definition."""
    row.title = row_definition.title
    row.mission = row_definition.mission
    row.why = row_definition.why
    row.source = row_definition.source
    row.show_more = row_definition.show_more
    return row


def _row_requires_artwork_rebuild(
    media_type: str,
    row_definition: RowDefinition,
    row: RowResult,
) -> bool:
    if media_type in {MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value} and row_definition.key in {
        "trending_right_now",
        "all_time_greats_unseen",
        "coming_soon",
    }:
        return any(_is_missing_image(item) for item in row.items[:MAX_ITEMS_PER_ROW])

    if row_definition.source == "provider" and row_definition.key in PROVIDER_ARTWORK_HYDRATION_ROW_KEYS:
        return any(
            _supports_provider_artwork_hydration(item) and _is_missing_image(item)
            for item in row.items[:MAX_ITEMS_PER_ROW]
        )
    return False


def _required_row_cache_schema_version(media_type: str, row_key: str) -> int | None:
    if media_type == MediaTypes.MOVIE.value:
        if row_key == "all_time_greats_unseen":
            return MOVIE_CANON_ROW_SCHEMA_VERSION
        if row_key == "coming_soon":
            return MOVIE_COMING_SOON_ROW_SCHEMA_VERSION
        if row_key in MOVIE_PERSONALIZED_ROW_KEYS:
            return MOVIE_PERSONALIZED_ROW_SCHEMA_VERSION
        return None
    if media_type in {MediaTypes.TV.value, MediaTypes.ANIME.value}:
        if row_key in {
            "trending_right_now",
            "all_time_greats_unseen",
            "coming_soon",
        }:
            return TV_ANIME_TRAKT_ROW_SCHEMA_VERSION
        if row_key in TV_ANIME_PERSONALIZED_ROW_KEYS:
            return TV_ANIME_PERSONALIZED_ROW_SCHEMA_VERSION
    return None


def _is_row_cache_compatible(
    media_type: str,
    row_definition: RowDefinition,
    cached_payload: dict,
) -> bool:
    required_schema_version = _required_row_cache_schema_version(media_type, row_definition.key)
    if required_schema_version is None:
        return True

    meta = cached_payload.get("meta")
    if not isinstance(meta, dict):
        return False

    try:
        return int(meta.get(ROW_CACHE_SCHEMA_META_KEY, 0)) >= required_schema_version
    except (TypeError, ValueError):
        return False


def _row_cache_matches_activity_version(
    user_id: int,
    media_type: str,
    cached_payload: dict,
) -> bool:
    meta = cached_payload.get("meta")
    if not isinstance(meta, dict):
        return True

    cached_activity_version = str(meta.get(ROW_CACHE_ACTIVITY_VERSION_META_KEY) or "")
    if not cached_activity_version:
        return True

    return cached_activity_version == tab_cache.get_activity_version(user_id, media_type)
