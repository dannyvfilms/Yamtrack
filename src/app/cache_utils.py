import logging
from collections.abc import Iterable

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


TIME_LEFT_CACHE_PREFIX = "time_left_sorted_v16"
_REGISTRY_TEMPLATE = f"{TIME_LEFT_CACHE_PREFIX}_registry_{{user_id}}"


def build_time_left_cache_key(
    user_id: int,
    media_type: str,
    status_filter: str,
    search_query: str,
    direction: str,
    rating_filter: str,
    collection_filter: str = "",
    genre_filter: str = "",
    year_filter: str = "",
    release_filter: str = "",
    source_filter: str = "",
    language_filter: str = "",
    country_filter: str = "",
    platform_filter: str = "",
    origin_filter: str = "",
    tag_filter: str = "",
    tag_exclude_filter: str = "",
) -> str:
    """Create the cache key used for time-left sorted TV lists."""
    normalized_status = status_filter or ""
    normalized_query = search_query or ""
    normalized_direction = direction or ""
    normalized_rating = rating_filter or ""
    normalized_collection = collection_filter or ""
    normalized_genre = genre_filter or ""
    normalized_year = year_filter or ""
    normalized_release = release_filter or ""
    normalized_source = source_filter or ""
    normalized_language = language_filter or ""
    normalized_country = country_filter or ""
    normalized_platform = platform_filter or ""
    normalized_origin = origin_filter or ""
    normalized_tag = tag_filter or ""
    normalized_tag_exclude = tag_exclude_filter or ""
    return (
        f"{TIME_LEFT_CACHE_PREFIX}_{user_id}_{media_type}_{normalized_status}_"
        f"{normalized_query}_{normalized_direction}_{normalized_rating}_{normalized_collection}_"
        f"{normalized_genre}_{normalized_year}_{normalized_release}_{normalized_source}_"
        f"{normalized_language}_{normalized_country}_{normalized_platform}_{normalized_origin}_"
        f"{normalized_tag}_{normalized_tag_exclude}"
    )


def _registry_key_for_user(user_id: int) -> str:
    return _REGISTRY_TEMPLATE.format(user_id=user_id)


def register_time_left_cache_key(user_id: int, cache_key: str) -> None:
    """Keep track of active cache keys for a user so we can invalidate them later."""
    registry_key = _registry_key_for_user(user_id)
    existing_keys = cache.get(registry_key)

    if existing_keys:
        keys = set(existing_keys)
    else:
        keys = set()

    if cache_key not in keys:
        keys.add(cache_key)
        cache.set(registry_key, list(keys), getattr(settings, "CACHE_TIMEOUT", None))


def clear_time_left_cache_for_user(user_id: int) -> None:
    """Invalidate all cached time-left lists for a user."""
    registry_key = _registry_key_for_user(user_id)
    keys: Iterable[str] | None = cache.get(registry_key)

    if not keys:
        return

    deleted = 0
    for key in keys:
        if cache.delete(key):
            deleted += 1

    cache.delete(registry_key)

    logger.debug(
        "Cleared %s time_left cache entries for user %s",
        deleted,
        user_id,
    )
