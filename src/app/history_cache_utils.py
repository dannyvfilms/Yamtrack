"""Cache-key constants, key-derivation utilities, and low-level helpers for the History cache."""

import logging
from datetime import datetime, timedelta
from typing import Iterable

from django.conf import settings
from django.db import models
from django.utils import timezone

from app.models import Music

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────


def _coerce_timedelta(value, default):
    if value is None:
        return default
    if isinstance(value, timedelta):
        return value
    try:
        return timedelta(seconds=int(value))
    except (TypeError, ValueError):
        return default


HISTORY_CACHE_VERSION = 18
HISTORY_INDEX_PREFIX = f"history_index_v{HISTORY_CACHE_VERSION}"
HISTORY_DAY_PREFIX = f"history_day_v{HISTORY_CACHE_VERSION}"
HISTORY_CACHE_PREFIX = HISTORY_INDEX_PREFIX
HISTORY_CACHE_TIMEOUT = 60 * 60 * 6  # 6 hours for the history index
HISTORY_DAY_CACHE_TIMEOUT = getattr(settings, "HISTORY_DAY_CACHE_TIMEOUT", None)
HISTORY_STALE_AFTER = _coerce_timedelta(
    getattr(settings, "HISTORY_CACHE_STALE_AFTER", None),
    timedelta(hours=1),
)
HISTORY_DAYS_PER_PAGE = 30
HISTORY_WARM_DAYS = getattr(settings, "HISTORY_CACHE_WARM_DAYS", 0)
HISTORY_COLD_MISS_WARM_DAYS = getattr(
    settings,
    "HISTORY_CACHE_COLD_MISS_WARM_DAYS",
    HISTORY_DAYS_PER_PAGE,
)
HISTORY_REFRESH_LOCK_PREFIX = f"history_refresh_lock_v{HISTORY_CACHE_VERSION}"
HISTORY_REFRESH_LOCK_MAX_AGE = timedelta(minutes=5)  # safety to clear stuck locks
HISTORY_COVERAGE_REPAIR_PREFIX = f"history_day_coverage_v{HISTORY_CACHE_VERSION}"
HISTORY_COVERAGE_REPAIR_BATCH_SIZE = getattr(
    settings,
    "HISTORY_COVERAGE_REPAIR_BATCH_SIZE",
    120,
)
HISTORY_COVERAGE_REPAIR_LOCK_TTL = getattr(
    settings,
    "HISTORY_COVERAGE_REPAIR_LOCK_TTL",
    60 * 30,
)


# ── Query helpers ─────────────────────────────────────────────────────────────


def _music_history_user_q(user):
    user_id = getattr(user, "id", user)
    owned_music_ids = Music.objects.filter(user_id=user_id).values("id")
    return models.Q(history_user_id=user_id) | (
        models.Q(history_user__isnull=True) & models.Q(id__in=owned_music_ids)
    )


# ── Cache key functions ───────────────────────────────────────────────────────


def _cache_key(user_id: int, logging_style: str) -> str:
    return f"{HISTORY_CACHE_PREFIX}_{user_id}_{logging_style or 'repeats'}"


def _refresh_lock_key(user_id: int, logging_style: str) -> str:
    return f"{HISTORY_REFRESH_LOCK_PREFIX}_{user_id}_{logging_style or 'repeats'}"


def _coverage_repair_key(user_id: int, logging_style: str) -> str:
    return f"{HISTORY_COVERAGE_REPAIR_PREFIX}_{user_id}_{logging_style or 'repeats'}"


def _day_cache_key(user_id: int, logging_style: str, day_key: str) -> str:
    return f"{HISTORY_DAY_PREFIX}_{user_id}_{logging_style or 'repeats'}_{day_key}"


# ── Day key functions ─────────────────────────────────────────────────────────


def _day_key_for_date(day_value):
    return day_value.strftime("%Y%m%d")


def _date_from_day_key(day_key: str):
    return datetime.strptime(day_key, "%Y%m%d").date()


def _day_key_from_value(value):
    if value is None:
        return None
    if isinstance(value, (int, bytes)):
        try:
            value = value.decode() if isinstance(value, bytes) else str(value)
        except Exception:
            return None
    if isinstance(value, str):
        value = value.strip().strip("'").strip('"')
        if value.isdigit() and len(value) == 8:
            return value
        try:
            return _day_key_for_date(datetime.strptime(value, "%Y-%m-%d").date())
        except ValueError:
            return None
    if isinstance(value, datetime):
        localized = _localize_datetime(value)
        if localized:
            return _day_key_for_date(localized.date())
        return None
    if hasattr(value, "strftime"):
        return _day_key_for_date(value)
    return None


def history_day_key(value):
    return _day_key_from_value(value)


def history_day_keys_for_range(start_dt, end_dt):
    if not start_dt or not end_dt:
        return []
    start_local = _localize_datetime(start_dt)
    end_local = _localize_datetime(end_dt)
    if not start_local or not end_local:
        return []
    start_date = start_local.date()
    end_date = end_local.date()
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    day_count = (end_date - start_date).days + 1
    return [
        _day_key_for_date(start_date + timedelta(days=offset))
        for offset in range(day_count)
    ]


# ── Logging style ─────────────────────────────────────────────────────────────


def _normalize_logging_style(logging_style, user=None):
    if logging_style in ("sessions", "repeats"):
        return logging_style
    if user is not None:
        return getattr(user, "game_logging_style", "repeats")
    return "repeats"


# ── Diagnostics ───────────────────────────────────────────────────────────────


def _get_rss_kb():
    try:
        import resource
    except Exception:
        return None
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:
        return None


# ── Timezone / datetime ───────────────────────────────────────────────────────


def _localize_datetime(value):
    """Convert a datetime to the current timezone if possible."""
    if value is None:
        return None
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return timezone.localtime(value)


# ── Genre helpers ─────────────────────────────────────────────────────────────


def _coerce_genre_list(value):
    """Normalize a genre field (string, dict, or list) into a list of strings."""
    def _coerce_one(v):
        if not v:
            return None
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return v.get("name") or v.get("tag") or v.get("label")
        return str(v)

    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        coerced = _coerce_one(value)
        return [coerced] if coerced else []
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            coerced = _coerce_one(v)
            if coerced:
                out.append(coerced)
        return out
    coerced = _coerce_one(value)
    return [coerced] if coerced else []


def _resolve_genres(*items):
    """Pick the first usable genres value from the provided items."""
    for item in items:
        if not item:
            continue
        genres = getattr(item, "genres", None)
        if genres:
            return _coerce_genre_list(genres)
    return []


def _resolve_music_genres(album=None, artist=None, track=None):
    if album and album.genres:
        return _coerce_genre_list(album.genres)
    if artist and artist.genres:
        return _coerce_genre_list(artist.genres)
    if track and track.genres:
        return _coerce_genre_list(track.genres)
    return []
