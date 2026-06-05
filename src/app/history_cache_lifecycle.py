"""Cache lifecycle management: invalidation and background-task scheduling."""

import logging
from typing import Iterable

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from app.history_cache_utils import (
    HISTORY_COVERAGE_REPAIR_LOCK_TTL,
    HISTORY_REFRESH_LOCK_MAX_AGE,
    _cache_key,
    _coverage_repair_key,
    _day_cache_key,
    _day_key_from_value,
    _normalize_logging_style,
    _refresh_lock_key,
)
from app.log_safety import stable_hmac

logger = logging.getLogger(__name__)


def _clean_refresh_lock(lock_key: str):
    refresh_lock = cache.get(lock_key)
    if refresh_lock:
        if not isinstance(refresh_lock, dict):
            cache.delete(lock_key)
            return None
        started_at = refresh_lock.get("started_at")
        if started_at and timezone.now() - started_at > HISTORY_REFRESH_LOCK_MAX_AGE:
            cache.delete(lock_key)
            return None
    return refresh_lock


def _delete_history_cache_entries(user_id: int, logging_style: str, day_keys=None):
    if day_keys is None:
        index_entry = cache.get(_cache_key(user_id, logging_style))
        day_keys = index_entry.get("days", []) if index_entry else []

    normalized_keys = []
    for value in day_keys:
        day_key = _day_key_from_value(value)
        if day_key:
            normalized_keys.append(day_key)

    if normalized_keys:
        cache.delete_many(
            [_day_cache_key(user_id, logging_style, day_key) for day_key in normalized_keys],
        )
    cache.delete(_cache_key(user_id, logging_style))


def invalidate_history_days(
    user_id: int,
    day_keys: Iterable | None,
    logging_styles: Iterable | None = None,
    reason: str | None = None,
    force: bool = False,
    refresh_index: bool = True,
):
    """Invalidate per-day history cache entries for a user.

    Day-scoped invalidations keep the existing payloads readable by default and
    schedule a targeted rebuild. Hard deletes are reserved for explicit force
    operations such as cache-version busts.
    """
    logging_styles = logging_styles or ("sessions", "repeats")
    normalized_keys = []
    for value in day_keys or []:
        day_key = _day_key_from_value(value)
        if day_key:
            normalized_keys.append(day_key)

    for style in logging_styles:
        logging_style = _normalize_logging_style(style)
        if force and normalized_keys:
            cache.delete_many(
                [_day_cache_key(user_id, logging_style, day_key) for day_key in normalized_keys],
            )
        logger.info(
            "history_day_invalidate user_id=%s logging_style=%s dates=%s reason=%s deleted=%s",
            user_id,
            logging_style,
            len(normalized_keys),
            reason or "unspecified",
            force and bool(normalized_keys),
        )

    if refresh_index:
        for style in logging_styles:
            logging_style = _normalize_logging_style(style)
            scheduled = schedule_history_refresh(
                user_id,
                logging_style,
                warm_days=0,
                day_keys=normalized_keys if normalized_keys else None,
            )
            logger.info(
                "history_index_refresh_scheduled user_id=%s logging_style=%s warm_days=0 day_keys=%s scheduled=%s reason=%s",
                user_id,
                logging_style,
                len(normalized_keys) if normalized_keys else 0,
                scheduled,
                reason or "unspecified",
            )


def invalidate_history_cache(
    user_id: int,
    force: bool = False,
    day_keys: Iterable | None = None,
    logging_styles: Iterable | None = None,
):
    """Remove cached history for a user, optionally scoped to specific days.

    If a refresh is in progress, keep the old cache so users can see it
    while the refresh completes. Otherwise, delete the cache/index.
    """
    if day_keys is not None:
        invalidate_history_days(
            user_id,
            day_keys=day_keys,
            logging_styles=logging_styles,
            force=force,
            refresh_index=True,
        )
        return

    logging_styles = logging_styles or ("sessions", "repeats")
    for style in logging_styles:
        logging_style = _normalize_logging_style(style)
        refresh_lock = _clean_refresh_lock(_refresh_lock_key(user_id, logging_style))
        if refresh_lock is None or force:
            _delete_history_cache_entries(user_id, logging_style, None)
            logger.info(
                "history_cache_invalidate_all user_id=%s logging_style=%s reason=%s",
                user_id,
                logging_style,
                "full_clear",
            )

    # Schedule refresh after invalidating all cache
    # This ensures cache is rebuilt and page doesn't get stuck
    if force:
        for style in logging_styles:
            logging_style = _normalize_logging_style(style)
            scheduled = schedule_history_refresh(
                user_id,
                logging_style,
                warm_days=0,  # Index-only refresh, don't warm days
            )
            logger.info(
                "history_index_refresh_scheduled user_id=%s logging_style=%s warm_days=0 scheduled=%s reason=%s",
                user_id,
                logging_style,
                scheduled,
                "album_score_change",
            )


def schedule_history_refresh(
    user_id: int,
    logging_style: str = "repeats",
    debounce_seconds: int = 30,
    countdown: int = 3,
    warm_days: int | None = None,
    day_keys: Iterable | None = None,
    allow_inline: bool = True,
    priority: int | None = None,
):
    """Queue a background refresh for a user's history cache.

    Args:
        user_id: User ID
        logging_style: Logging style for history
        debounce_seconds: Seconds to debounce refresh requests
        countdown: Seconds to delay task execution (default 3)
        warm_days: Optional warm window for day payloads
        day_keys: Optional list of day keys to warm
    """
    logging_style = _normalize_logging_style(logging_style)
    lock_key = _refresh_lock_key(user_id, logging_style)
    normalized_day_keys = []
    for value in day_keys or []:
        day_key = _day_key_from_value(value)
        if day_key:
            normalized_day_keys.append(day_key)
    if normalized_day_keys:
        dedupe_seed = ",".join(normalized_day_keys)
        dedupe_hash = stable_hmac(
            dedupe_seed,
            namespace="history_refresh_days",
            length=10,
        )
        dedupe_key = f"{lock_key}_days_{dedupe_hash}"
    else:
        dedupe_key = lock_key
    # Keep TTL close to the frontend polling timeout so locks don't appear "stuck"
    # while still covering normal task execution time.
    lock_ttl = 120  # Matches CacheUpdater timeout window
    lock_payload = {"started_at": timezone.now()}
    if normalized_day_keys:
        lock_payload["day_keys"] = normalized_day_keys
        # Store dedupe_key in payload so we can delete it when task completes
        lock_payload["dedupe_key"] = dedupe_key
    if debounce_seconds and not cache.add(dedupe_key, lock_payload, debounce_seconds):
        return False

    # Extend the lock TTL to cover the full task duration
    # This ensures the lock exists even if the task takes longer than debounce_seconds
    cache.set(dedupe_key, lock_payload, lock_ttl)
    if dedupe_key != lock_key:
        cache.set(lock_key, lock_payload, lock_ttl)

    try:
        from app.tasks import refresh_history_cache_task

        task_args = [user_id, logging_style]
        task_kwargs = {}
        if warm_days is not None:
            task_kwargs["warm_days"] = warm_days
        if normalized_day_keys:
            task_kwargs["day_keys"] = normalized_day_keys
        refresh_history_cache_task.apply_async(
            args=task_args,
            kwargs=task_kwargs,
            countdown=countdown,
            priority=(
                getattr(settings, "CELERY_TASK_PRIORITY_INTERACTIVE", 9)
                if priority is None
                else priority
            ),
        )
        return True
    except Exception as exc:  # pragma: no cover - Celery not available
        if not allow_inline:
            cache.delete(dedupe_key)
            if dedupe_key != lock_key:
                cache.delete(lock_key)
            logger.warning(
                "Failed to schedule history cache refresh for user %s: %s",
                user_id,
                exc,
            )
            return False
        logger.debug(
            "Falling back to inline history cache rebuild for user %s: %s",
            user_id,
            exc,
        )
        from app.history_cache import (
            refresh_history_cache,  # deferred to avoid circular import
        )
        refresh_history_cache(user_id, logging_style=logging_style, warm_days=warm_days)
        return False


def schedule_history_day_cache_coverage(
    user_id: int,
    logging_style: str = "repeats",
    *,
    debounce_seconds: int = 60 * 10,
    countdown: int = 30,
    batch_size: int | None = None,
    priority: int | None = None,
):
    """Queue low-priority repair work for missing persisted day payloads."""
    logging_style = _normalize_logging_style(logging_style)
    repair_key = _coverage_repair_key(user_id, logging_style)
    lock_payload = {
        "started_at": timezone.now().isoformat(),
        "batch_size": batch_size,
    }
    lock_ttl = max(int(debounce_seconds or 0), HISTORY_COVERAGE_REPAIR_LOCK_TTL)
    if debounce_seconds and not cache.add(repair_key, lock_payload, debounce_seconds):
        return False

    cache.set(repair_key, lock_payload, lock_ttl)

    try:
        from app.tasks import repair_history_day_cache_coverage_task

        task_kwargs = {
            "user_id": user_id,
            "logging_style": logging_style,
        }
        if batch_size is not None:
            task_kwargs["batch_size"] = batch_size
        repair_history_day_cache_coverage_task.apply_async(
            kwargs=task_kwargs,
            countdown=countdown,
            priority=(
                getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1)
                if priority is None
                else priority
            ),
        )
        return True
    except Exception as exc:  # pragma: no cover - Celery not available
        cache.delete(repair_key)
        logger.warning(
            "Failed to schedule history day coverage repair for user %s: %s",
            user_id,
            exc,
        )
        return False
