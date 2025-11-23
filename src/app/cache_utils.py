import logging
from typing import Iterable

from django.conf import settings
from django.core.cache import cache


logger = logging.getLogger(__name__)


TIME_LEFT_CACHE_PREFIX = "time_left_sorted_v12"
_REGISTRY_TEMPLATE = f"{TIME_LEFT_CACHE_PREFIX}_registry_{{user_id}}"


def build_time_left_cache_key(
    user_id: int,
    media_type: str,
    status_filter: str,
    search_query: str,
    direction: str,
) -> str:
    """Create the cache key used for time-left sorted TV lists."""
    normalized_status = status_filter or ""
    normalized_query = search_query or ""
    normalized_direction = direction or ""
    return f"{TIME_LEFT_CACHE_PREFIX}_{user_id}_{media_type}_{normalized_status}_{normalized_query}_{normalized_direction}"


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
