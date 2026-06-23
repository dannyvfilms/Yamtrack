"""History cache reader, paginator, and refresh/repair workers."""

import logging
import time
from typing import Iterable

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import formats, timezone

from app.history_cache_day_builder import (
    _build_and_cache_history_day,
    build_history_day,
)
from app.history_cache_index import (
    _missing_history_day_keys,
    build_history_index,
    cache_history_index,
)
from app.history_cache_lifecycle import (
    _clean_refresh_lock,
    schedule_history_day_cache_coverage,
    schedule_history_refresh,
)
from app.history_cache_serialization import (
    _deserialize_history_day,
    _serialize_history_day,
)
from app.history_cache_utils import (
    HISTORY_COLD_MISS_WARM_DAYS,
    HISTORY_COVERAGE_REPAIR_BATCH_SIZE,
    HISTORY_DAY_CACHE_TIMEOUT,
    HISTORY_DAYS_PER_PAGE,
    HISTORY_STALE_AFTER,
    HISTORY_WARM_DAYS,
    _cache_key,
    _coverage_repair_key,
    _date_from_day_key,
    _day_cache_key,
    _day_key_from_value,
    _normalize_logging_style,
    _refresh_lock_key,
)

logger = logging.getLogger(__name__)


def get_month_history(user, year: int, month: int, logging_style_override=None):
    """Get history days for a specific calendar month.

    Reads the month from the cached history index plus indexed per-day payloads
    that are kept warm by media events. Any missing indexed day payloads are
    repaired inline so the page can render final content in a single response.

    Args:
        user: User instance
        year: Calendar year (e.g., 2026)
        month: Calendar month (1-12)
        logging_style_override: Optional logging style override

    Returns:
        Tuple of (history_days, cache_meta) where cache_meta contains:
        - refreshing: bool - Whether a background refresh is in progress
        - refresh_reason: str or None - Why refresh was triggered
    """
    start = time.perf_counter()
    logging_style = _normalize_logging_style(logging_style_override, user)
    cache_meta = {"refreshing": False, "refresh_reason": None}

    cache_key = _cache_key(user.id, logging_style)
    lock_key = _refresh_lock_key(user.id, logging_style)
    refresh_lock = _clean_refresh_lock(lock_key)

    cache_entry = cache.get(cache_key)
    cache_age_s = None
    if cache_entry:
        built_at = cache_entry.get("built_at")
        if built_at:
            cache_age_s = (timezone.now() - built_at).total_seconds()
        if built_at and timezone.now() - built_at > HISTORY_STALE_AFTER:
            if refresh_lock is None:
                scheduled = schedule_history_refresh(user.id, logging_style, warm_days=0)
                logger.info(
                    "history_index_stale_refresh user_id=%s logging_style=%s scheduled=%s cache_age_s=%s",
                    user.id,
                    logging_style,
                    scheduled,
                    cache_age_s,
                )
    else:
        logger.warning(
            "history_index_inline_repair user_id=%s logging_style=%s year=%s month=%s",
            user.id,
            logging_style,
            year,
            month,
        )
        index_day_keys = build_history_index(user, logging_style)
        built_at = cache_history_index(user.id, logging_style, index_day_keys)
        cache_entry = {"days": index_day_keys, "built_at": built_at}

    index_days = cache_entry.get("days", [])
    month_prefix = f"{year}{month:02d}"
    month_day_keys = [
        day_key
        for day_key in index_days
        if str(day_key).startswith(month_prefix)
    ]

    day_cache_keys = [
        _day_cache_key(user.id, logging_style, day_key)
        for day_key in month_day_keys
    ]
    day_payloads = cache.get_many(day_cache_keys)
    cache_hits = len(day_payloads)
    logger.info(
        "history_month_cache_lookup user_id=%s year=%s month=%s indexed_days=%s "
        "cache_hits=%s lock=%s cache_age_s=%s",
        user.id,
        year,
        month,
        len(month_day_keys),
        cache_hits,
        refresh_lock is not None,
        cache_age_s,
    )

    if not month_day_keys:
        logger.info(
            "history_month_result user_id=%s year=%s month=%s days=0 elapsed_ms=%.2f",
            user.id,
            year,
            month,
            (time.perf_counter() - start) * 1000,
        )
        return [], cache_meta

    history_days = []
    missing_days = []
    for day_key in month_day_keys:
        payload_key = _day_cache_key(user.id, logging_style, day_key)
        payload = day_payloads.get(payload_key)
        if payload is None:
            missing_days.append(day_key)
        else:
            history_days.append(_deserialize_history_day(payload))

    if missing_days:
        logger.warning(
            "history_month_cache_repair user_id=%s year=%s month=%s indexed_days=%s "
            "cached=%s missing=%s lock=%s",
            user.id,
            year,
            month,
            len(month_day_keys),
            len(history_days),
            len(missing_days),
            refresh_lock is not None,
        )
        history_days = []
        for day_key in month_day_keys:
            payload = day_payloads.get(_day_cache_key(user.id, logging_style, day_key))
            if payload is not None:
                history_days.append(_deserialize_history_day(payload))
                continue
            history_days.append(_build_and_cache_history_day(user, day_key, logging_style))
        schedule_history_day_cache_coverage(
            user.id,
            logging_style,
            countdown=15,
        )

    logger.info(
        "history_month_result user_id=%s year=%s month=%s days=%s "
        "source=cache elapsed_ms=%.2f",
        user.id,
        year,
        month,
        len(history_days),
        (time.perf_counter() - start) * 1000,
    )

    return history_days, cache_meta


def get_history_days(user, filters=None, date_filters=None, logging_style_override=None):
    """Build history days directly (used for filtered requests)."""
    start = time.perf_counter()
    logger.info(
        "history_cache_bypass user_id=%s filters=%s date_filters=%s logging_style_override=%s",
        user.id,
        filters or {},
        date_filters or {},
        logging_style_override,
    )
    # Deferred import: build_history_days still lives in history_cache.py
    from app.history_cache import build_history_days
    history_days = build_history_days(
        user,
        filters=filters,
        date_filters=date_filters,
        logging_style_override=logging_style_override,
    )
    logger.info(
        "history_cache_bypass_done user_id=%s days=%s elapsed_ms=%.2f",
        user.id,
        len(history_days),
        (time.perf_counter() - start) * 1000,
    )
    return history_days


def get_cached_history_page(user, page_number: int = 1, logging_style_override=None):
    """Return a cached history page, total day count, and refresh metadata."""
    start = time.perf_counter()
    logging_style = _normalize_logging_style(logging_style_override, user)
    cache_key = _cache_key(user.id, logging_style)
    lock_key = _refresh_lock_key(user.id, logging_style)
    meta = {"refreshing": False, "refresh_reason": None}

    refresh_lock = _clean_refresh_lock(lock_key)
    lock_age_s = None
    if isinstance(refresh_lock, dict):
        started_at = refresh_lock.get("started_at")
        if started_at:
            lock_age_s = (timezone.now() - started_at).total_seconds()

    cache_entry = cache.get(cache_key)
    logger.info(
        "history_index_lookup user_id=%s cache_key=%s hit=%s lock=%s lock_age_s=%s",
        user.id,
        cache_key,
        cache_entry is not None,
        refresh_lock is not None,
        lock_age_s,
    )

    if not cache_entry:
        if refresh_lock is not None:
            logger.info(
                "history_index_miss_refreshing user_id=%s logging_style=%s returning_empty=true",
                user.id,
                logging_style,
            )
            meta.update({"refreshing": True, "refresh_reason": "index_refreshing"})
            return [], 0, meta
        scheduled = schedule_history_refresh(
            user.id,
            logging_style,
            warm_days=HISTORY_COLD_MISS_WARM_DAYS,
            allow_inline=False,
        )
        logger.info(
            "history_index_miss user_id=%s logging_style=%s scheduled=%s returning_empty=true",
            user.id,
            logging_style,
            scheduled,
        )
        meta.update({"refreshing": True, "refresh_reason": "index_miss"})
        return [], 0, meta

    index_days = cache_entry.get("days", [])
    built_at = cache_entry.get("built_at")
    cache_age_s = None
    if built_at:
        cache_age_s = (timezone.now() - built_at).total_seconds()
    if built_at and timezone.now() - built_at > HISTORY_STALE_AFTER:
        refresh_lock = _clean_refresh_lock(lock_key)
        if refresh_lock is None:
            scheduled = schedule_history_refresh(user.id, logging_style, warm_days=0)
            logger.info(
                "history_index_stale_refresh user_id=%s logging_style=%s scheduled=%s cache_age_s=%s",
                user.id,
                logging_style,
                scheduled,
                cache_age_s,
            )

    total_days = len(index_days)
    if total_days == 0:
        logger.info(
            "history_index_hit user_id=%s logging_style=%s days=0 cache_age_s=%s",
            user.id,
            logging_style,
            cache_age_s,
        )
        return [], 0, meta

    try:
        page_number = int(page_number)
    except (TypeError, ValueError):
        page_number = 1
    if page_number < 1:
        page_number = 1

    start_index = (page_number - 1) * HISTORY_DAYS_PER_PAGE
    end_index = start_index + HISTORY_DAYS_PER_PAGE
    page_day_keys = index_days[start_index:end_index]
    logger.info(
        "history_page_days user_id=%s logging_style=%s page=%s days_per_page=%s needed=%s",
        user.id,
        logging_style,
        page_number,
        HISTORY_DAYS_PER_PAGE,
        len(page_day_keys),
    )

    day_cache_keys = [
        _day_cache_key(user.id, logging_style, day_key)
        for day_key in page_day_keys
    ]
    day_payloads = cache.get_many(day_cache_keys)
    logger.info(
        "history_day_cache_get_many user_id=%s logging_style=%s requested=%s hit=%s miss=%s",
        user.id,
        logging_style,
        len(page_day_keys),
        len(day_payloads),
        max(len(page_day_keys) - len(day_payloads), 0),
    )
    history_days = []
    missing_days = []
    for day_key in page_day_keys:
        payload_key = _day_cache_key(user.id, logging_style, day_key)
        payload = day_payloads.get(payload_key)
        if payload is None:
            missing_days.append(day_key)
            continue
        history_days.append(_deserialize_history_day(payload))

    if missing_days and len(day_payloads) == 0:
        refresh_lock = _clean_refresh_lock(lock_key)
        scheduled = False
        if refresh_lock is None:
            scheduled = schedule_history_refresh(
                user.id,
                logging_style,
                day_keys=missing_days,
                allow_inline=False,
            )
            logger.info(
                "history_day_cache_cold_miss user_id=%s logging_style=%s missing=%s scheduled=%s returning_empty=true",
                user.id,
                logging_style,
                len(missing_days),
                scheduled,
            )
        else:
            logger.info(
                "history_day_cache_cold_miss_refreshing user_id=%s logging_style=%s missing=%s",
                user.id,
                logging_style,
                len(missing_days),
            )
        refreshing = refresh_lock is not None or scheduled
        meta.update({"refreshing": refreshing, "refresh_reason": "day_cache_cold_miss"})
        return [], total_days, meta

    built_days = {}
    if missing_days:
        build_start = time.perf_counter()
        for day_key in missing_days:
            day_payload = build_history_day(user, day_key, logging_style_override=logging_style)
            if day_payload:
                built_days[day_key] = day_payload
                cache.set(
                    _day_cache_key(user.id, logging_style, day_key),
                    _serialize_history_day(day_payload),
                    timeout=HISTORY_DAY_CACHE_TIMEOUT,
                )
            else:
                day_date = _date_from_day_key(day_key)
                if day_date:
                    empty_day = {
                        "date": day_date,
                        "weekday": formats.date_format(day_date, "l"),
                        "date_display": formats.date_format(day_date, "F j, Y"),
                        "entries": [],
                        "total_minutes": 0,
                        "total_runtime_display": "0min",
                    }
                    cache.set(
                        _day_cache_key(user.id, logging_style, day_key),
                        _serialize_history_day(empty_day),
                        timeout=HISTORY_DAY_CACHE_TIMEOUT,
                    )

        if built_days:
            history_days = []
            for day_key in page_day_keys:
                payload_key = _day_cache_key(user.id, logging_style, day_key)
                payload = day_payloads.get(payload_key)
                if payload:
                    history_days.append(_deserialize_history_day(payload))
                    continue
                day_payload = built_days.get(day_key)
                if day_payload:
                    history_days.append(day_payload)

        if len(built_days) != len(missing_days):
            refresh_lock = _clean_refresh_lock(lock_key)
            if refresh_lock is None:
                scheduled = schedule_history_refresh(user.id, logging_style, warm_days=0)
                logger.info(
                    "history_day_cache_miss user_id=%s logging_style=%s missing=%s built=%s scheduled=%s",
                    user.id,
                    logging_style,
                    len(missing_days),
                    len(built_days),
                    scheduled,
                )
            else:
                logger.info(
                    "history_day_cache_miss_refreshing user_id=%s logging_style=%s missing=%s built=%s",
                    user.id,
                    logging_style,
                    len(missing_days),
                    len(built_days),
                )
        else:
            logger.info(
                "history_day_cache_inline_build user_id=%s logging_style=%s built=%s elapsed_ms=%.2f",
                user.id,
                logging_style,
                len(built_days),
                (time.perf_counter() - build_start) * 1000,
            )

    logger.info(
        "history_index_hit user_id=%s logging_style=%s days=%s page_days=%s cache_age_s=%s elapsed_ms=%.2f",
        user.id,
        logging_style,
        total_days,
        len(history_days),
        cache_age_s,
        (time.perf_counter() - start) * 1000,
    )
    return history_days, total_days, meta


def refresh_history_cache(
    user_id: int,
    logging_style: str | None = None,
    warm_days: int | None = None,
    day_keys: Iterable | None = None,
):
    """Rebuild and store history index for a user."""
    user_model = get_user_model()
    try:
        user = user_model.objects.get(id=user_id)
        logging_style = _normalize_logging_style(logging_style, user)
    except user_model.DoesNotExist:
        cache.delete(_refresh_lock_key(user_id, logging_style or "repeats"))
        return None

    try:
        normalized_day_keys = []
        for value in day_keys or []:
            day_key = _day_key_from_value(value)
            if day_key:
                normalized_day_keys.append(day_key)
        use_specific_days = bool(normalized_day_keys)
        if use_specific_days:
            seen = set()
            requested_day_keys = []
            for key in normalized_day_keys:
                if key in seen:
                    continue
                seen.add(key)
                requested_day_keys.append(key)
        else:
            requested_day_keys = None

        if warm_days is None:
            warm_days = HISTORY_WARM_DAYS
        logger.info(
            "history_cache_refresh_start user_id=%s logging_style=%s day_keys=%s mode=%s",
            user_id,
            logging_style,
            len(requested_day_keys or []),
            "page_days" if use_specific_days else "index",
        )
        index_day_keys = build_history_index(user, logging_style_override=logging_style)
        cache_history_index(user_id, logging_style, index_day_keys)

        warm_targets = []
        if use_specific_days:
            warm_targets = requested_day_keys or []
        elif index_day_keys:
            missing_day_keys = _missing_history_day_keys(user_id, logging_style, index_day_keys)
            if missing_day_keys:
                warm_targets = missing_day_keys
            elif warm_days:
                warm_targets = index_day_keys[:warm_days]
        rebuilt = 0
        populated = 0
        for day_key in warm_targets:
            day_payload = _build_and_cache_history_day(
                user,
                day_key,
                logging_style,
            )
            rebuilt += 1
            if day_payload and day_payload.get("entries"):
                populated += 1
        logger.info(
            "history_cache_refresh_done user_id=%s logging_style=%s days=%s rebuilt=%s populated=%s",
            user_id,
            logging_style,
            len(index_day_keys),
            rebuilt,
            populated,
        )
        lock_key = _refresh_lock_key(user_id, logging_style)
        refresh_lock = cache.get(lock_key)
        dedupe_key = None
        if refresh_lock and isinstance(refresh_lock, dict):
            dedupe_key = refresh_lock.get("dedupe_key")

        cache.delete(lock_key)
        if dedupe_key and dedupe_key != lock_key:
            cache.delete(dedupe_key)
            logger.debug(
                "Deleted dedupe_key %s for user %s",
                dedupe_key,
                user_id,
            )

        verify_lock = cache.get(lock_key)
        logger.debug(
            "History cache refresh completed for user %s, lock released. Lock key: %s, still exists: %s",
            user_id,
            lock_key,
            verify_lock is not None,
        )
        return index_day_keys
    except Exception as e:
        logger.error("Error refreshing history cache for user %s: %s", user_id, e, exc_info=True)
        lock_key = _refresh_lock_key(user_id, logging_style)
        refresh_lock = cache.get(lock_key)
        dedupe_key = None
        if refresh_lock and isinstance(refresh_lock, dict):
            dedupe_key = refresh_lock.get("dedupe_key")
        cache.delete(lock_key)
        if dedupe_key and dedupe_key != lock_key:
            cache.delete(dedupe_key)
        raise


def repair_history_day_cache_coverage(
    user_id: int,
    logging_style: str = "repeats",
    batch_size: int | None = None,
):
    """Repair missing persisted day payloads for a user's history cache in batches."""
    user_model = get_user_model()
    try:
        user = user_model.objects.get(id=user_id)
    except user_model.DoesNotExist:
        cache.delete(_coverage_repair_key(user_id, logging_style))
        return {"rebuilt": 0, "remaining": 0, "days": 0}

    logging_style = _normalize_logging_style(logging_style, user)
    if batch_size is None:
        batch_size = HISTORY_COVERAGE_REPAIR_BATCH_SIZE

    cache_entry = cache.get(_cache_key(user_id, logging_style))
    if cache_entry:
        index_day_keys = cache_entry.get("days", [])
    else:
        index_day_keys = build_history_index(user, logging_style_override=logging_style)
        cache_history_index(user_id, logging_style, index_day_keys)

    if not index_day_keys:
        return {"rebuilt": 0, "remaining": 0, "days": 0}

    missing_day_keys = _missing_history_day_keys(user_id, logging_style, index_day_keys)
    if not missing_day_keys:
        return {"rebuilt": 0, "remaining": 0, "days": len(index_day_keys)}

    target_day_keys = (
        missing_day_keys[:batch_size]
        if batch_size and batch_size > 0
        else missing_day_keys
    )
    rebuilt = 0
    populated = 0
    for day_key in target_day_keys:
        day_payload = _build_and_cache_history_day(user, day_key, logging_style)
        rebuilt += 1
        if day_payload and day_payload.get("entries"):
            populated += 1

    remaining = max(len(missing_day_keys) - len(target_day_keys), 0)
    logger.info(
        "history_day_coverage_repair user_id=%s logging_style=%s rebuilt=%s populated=%s remaining=%s days=%s",
        user_id,
        logging_style,
        rebuilt,
        populated,
        remaining,
        len(index_day_keys),
    )
    return {
        "rebuilt": rebuilt,
        "remaining": remaining,
        "days": len(index_day_keys),
    }
