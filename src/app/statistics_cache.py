"""Utilities for caching the Statistics page."""

import calendar
import heapq
import itertools
import logging
import re
import random
import time
from types import SimpleNamespace
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Max, Min, Q
from django.db.models.functions import ExtractDay, ExtractMonth
from django.db.models.functions import TruncDate
from django.utils import timezone

from app import config, credits as credit_helpers, helpers
from app import statistics as stats
from app import history_cache
from app.statistics_talent import (
    STATISTICS_TOP_N,
    STATISTICS_TOP_RATED_OVERALL,
    _aggregate_top_talent,
    _is_director_credit,
    _is_writer_credit,
    _resolve_missing_credit_item_ids,
    _safe_runtime_minutes,
    _tv_episode_play_rows,
    get_person_talent_totals,
)
from app.statistics_highlights import (
    _cached_horizontal_backdrop,
    _get_history_day_payload,
    _get_history_index_days,
    _get_horizontal_history_image,
    _get_range_history_boundary_days,
    _get_today_history_entries,
    _get_today_release_entry,
    _history_entry_card_payload,
    _normalize_history_highlight_images,
    _select_history_entry_for_day,
)
from app.models import (
    CREDITS_BACKFILL_VERSION,
    CreditRoleType,
    Episode,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    Movie,
    Person,
    PersonGender,
    Sources,
    Status,
)
from app.templatetags import app_tags

logger = logging.getLogger(__name__)

STATISTICS_CACHE_VERSION = 14
STATISTICS_CACHE_PREFIX = f"statistics_page_v{STATISTICS_CACHE_VERSION}"
STATISTICS_CACHE_TIMEOUT = 60 * 60 * 6  # 6 hours
STATISTICS_STALE_AFTER = timedelta(minutes=15)
STATISTICS_REFRESH_LOCK_PREFIX = f"{STATISTICS_CACHE_PREFIX}_refresh_lock"
STATISTICS_DAY_CACHE_VERSION = 6
STATISTICS_DAY_PREFIX = f"stats:day:v{STATISTICS_DAY_CACHE_VERSION}"
STATISTICS_DAY_DIRTY_PREFIX = "stats:dirty"
STATISTICS_HISTORY_VERSION_PREFIX = "stats:history_version"
STATISTICS_SCHEDULE_DEDUPE_PREFIX = "stats:refresh:scheduled"
STATISTICS_METADATA_REFRESH_PREFIX = "stats:metadata_refresh"
STATISTICS_METADATA_REFRESH_BUILT_PREFIX = "stats:metadata_refresh_built"
STATISTICS_DAY_CACHE_TIMEOUT = getattr(settings, "STATISTICS_DAY_CACHE_TIMEOUT", 60 * 60 * 24 * 30)
STATISTICS_WARM_DAYS = getattr(settings, "STATISTICS_CACHE_WARM_DAYS", 2)
STATISTICS_SCHEDULE_DEDUPE_TTL = getattr(settings, "STATISTICS_SCHEDULE_DEDUPE_TTL", 60 * 10)
STATISTICS_REFRESH_LOCK_MAX_AGE = getattr(settings, "STATISTICS_REFRESH_LOCK_MAX_AGE", timedelta(minutes=5))
STATISTICS_METADATA_REFRESH_TTL = getattr(settings, "STATISTICS_METADATA_REFRESH_TTL", 60 * 10)
STATISTICS_METADATA_REFRESH_RECENT_SECONDS = getattr(settings, "STATISTICS_METADATA_REFRESH_RECENT_SECONDS", 60)
STATISTICS_TASK_PRIORITY_INTERACTIVE = getattr(settings, "CELERY_TASK_PRIORITY_INTERACTIVE", 9)
STATISTICS_TASK_PRIORITY_FOLLOWUP = getattr(settings, "CELERY_TASK_PRIORITY_FOLLOWUP", 7)
STATISTICS_TASK_PRIORITY_BACKGROUND = getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1)
STATISTICS_ALL_TIME_REFRESH_DELAY = getattr(settings, "STATISTICS_ALL_TIME_REFRESH_DELAY", 45)

# Predefined ranges that can be cached
PREDEFINED_RANGES = [
    "Today",
    "Yesterday",
    "This Week",
    "Last 7 Days",
    "This Month",
    "Last 30 Days",
    "Last 90 Days",
    "This Year",
    "Last 6 Months",
    "Last 12 Months",
    "All Time",
]

_HOURS_DISPLAY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)h\s+(\d+(?:\.\d+)?)min\s*$")


def _normalize_range_name(range_name: str) -> str:
    """Normalize range name for cache key (e.g., 'All Time' -> 'all_time')."""
    if range_name == "All Time":
        return "all_time"
    # Replace spaces with underscores and convert to lowercase
    return range_name.lower().replace(" ", "_")


def _cache_key(user_id: int, range_name: str) -> str:
    """Generate cache key for statistics data."""
    normalized = _normalize_range_name(range_name)
    return f"{STATISTICS_CACHE_PREFIX}_{user_id}_{normalized}"


def _refresh_lock_key(user_id: int, range_name: str) -> str:
    """Generate lock key for debouncing refresh operations."""
    normalized = _normalize_range_name(range_name)
    return f"{STATISTICS_REFRESH_LOCK_PREFIX}_{user_id}_{normalized}"


def _lock_is_stale(value) -> bool:
    if not value:
        return False
    if isinstance(value, dict):
        started_at = value.get("started_at")
        if not started_at:
            return True
        if isinstance(started_at, str):
            try:
                started_at = datetime.fromisoformat(started_at)
            except ValueError:
                return True
        if not isinstance(started_at, datetime):
            return True
        if timezone.is_naive(started_at):
            started_at = timezone.make_aware(started_at, timezone.get_current_timezone())
        return timezone.now() - started_at > STATISTICS_REFRESH_LOCK_MAX_AGE
    return True


def _schedule_dedupe_key(user_id: int, range_name: str, history_version: str) -> str:
    normalized = _normalize_range_name(range_name)
    return f"{STATISTICS_SCHEDULE_DEDUPE_PREFIX}:{user_id}:{history_version}:{normalized}"


def _preferred_range_for_user(user_id: int) -> str:
    user_model = get_user_model()
    preferred_range = (
        user_model.objects.filter(id=user_id)
        .values_list("statistics_default_range", flat=True)
        .first()
    )
    if preferred_range not in PREDEFINED_RANGES:
        return "Last 12 Months"
    return preferred_range


def _day_cache_key(user_id: int, day_value: date | datetime | str) -> str:
    day = _normalize_day_value(day_value)
    if not day:
        return ""
    return f"{STATISTICS_DAY_PREFIX}:{user_id}:{day.isoformat()}"


def _dirty_days_key(user_id: int) -> str:
    return f"{STATISTICS_DAY_DIRTY_PREFIX}:{user_id}"


def _history_version_key(user_id: int) -> str:
    return f"{STATISTICS_HISTORY_VERSION_PREFIX}:{user_id}"


def _metadata_refresh_lock_key(user_id: int) -> str:
    return f"{STATISTICS_METADATA_REFRESH_PREFIX}:{user_id}"


def _metadata_refresh_built_key(user_id: int) -> str:
    return f"{STATISTICS_METADATA_REFRESH_BUILT_PREFIX}:{user_id}"


def _any_range_refreshing(user_id: int) -> bool:
    for check_range in PREDEFINED_RANGES:
        check_lock_key = _refresh_lock_key(user_id, check_range)
        check_lock = cache.get(check_lock_key)
        if check_lock and _lock_is_stale(check_lock):
            cache.delete(check_lock_key)
            check_lock = None
        if check_lock is not None:
            return True
    return False


def _parse_cached_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def mark_metadata_refreshing(user_id: int, reason: str | None = None) -> None:
    payload = {"started_at": timezone.now().isoformat(), "reason": reason or ""}
    cache.set(_metadata_refresh_lock_key(user_id), payload, timeout=STATISTICS_METADATA_REFRESH_TTL)


def clear_metadata_refreshing(user_id: int) -> None:
    cache.delete(_metadata_refresh_lock_key(user_id))
    cache.set(
        _metadata_refresh_built_key(user_id),
        timezone.now().isoformat(),
        timeout=STATISTICS_DAY_CACHE_TIMEOUT,
    )


def _metadata_refresh_status(user_id: int):
    lock = cache.get(_metadata_refresh_lock_key(user_id))
    if lock and _lock_is_stale(lock):
        cache.delete(_metadata_refresh_lock_key(user_id))
        lock = None
    built_at = _parse_cached_datetime(cache.get(_metadata_refresh_built_key(user_id)))
    if built_at and timezone.is_naive(built_at):
        built_at = timezone.make_aware(built_at, timezone.get_current_timezone())
    recently_built = False
    if built_at:
        recently_built = timezone.now() - built_at < timedelta(seconds=STATISTICS_METADATA_REFRESH_RECENT_SECONDS)
    return lock, built_at, recently_built


def _maybe_clear_metadata_refresh(user_id: int) -> None:
    lock = cache.get(_metadata_refresh_lock_key(user_id))
    if not lock:
        return
    if _lock_is_stale(lock):
        cache.delete(_metadata_refresh_lock_key(user_id))
        return
    if not _any_range_refreshing(user_id):
        clear_metadata_refreshing(user_id)


def _normalize_day_value(value):
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        localized = timezone.localtime(value) if timezone.is_aware(value) else value
        return localized.date()
    if isinstance(value, str):
        try:
            if value.isdigit() and len(value) == 8:
                return datetime.strptime(value, "%Y%m%d").date()
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _normalize_hours_display(value):
    if not isinstance(value, str):
        return value
    if "play" in value:
        return value
    match = _HOURS_DISPLAY_RE.match(value)
    if not match:
        return value
    try:
        hours = float(match.group(1))
        minutes = float(match.group(2))
    except ValueError:
        return value
    total_minutes = (hours * 60) + minutes
    return stats._format_hours_minutes(total_minutes)


def _normalize_hours_per_media_type(hours_per_media_type):
    if not isinstance(hours_per_media_type, dict):
        return hours_per_media_type
    for media_type, value in hours_per_media_type.items():
        hours_per_media_type[media_type] = _normalize_hours_display(value)
    return hours_per_media_type


def _get_history_version(user_id: int) -> str:
    version = cache.get(_history_version_key(user_id))
    if version:
        return version
    version = timezone.now().isoformat()
    cache.set(_history_version_key(user_id), version, timeout=STATISTICS_DAY_CACHE_TIMEOUT)
    return version


def _set_history_version(user_id: int, value: str | None = None) -> str:
    version = value or timezone.now().isoformat()
    cache.set(_history_version_key(user_id), version, timeout=STATISTICS_DAY_CACHE_TIMEOUT)
    return version


def _load_dirty_days(user_id: int) -> set[str]:
    raw = cache.get(_dirty_days_key(user_id)) or []
    if isinstance(raw, set):
        return set(raw)
    if isinstance(raw, (list, tuple)):
        return {str(item) for item in raw if item}
    return set()


def _store_dirty_days(user_id: int, days: set[str]) -> None:
    cache.set(_dirty_days_key(user_id), sorted(days), timeout=STATISTICS_DAY_CACHE_TIMEOUT)


def invalidate_statistics_days(user_id: int, day_values, reason: str | None = None) -> None:
    day_keys = []
    normalized_days = set()
    for value in day_values or []:
        day = _normalize_day_value(value)
        if not day:
            continue
        day_str = day.isoformat()
        normalized_days.add(day_str)
        day_keys.append(_day_cache_key(user_id, day))

    if day_keys:
        cache.delete_many(day_keys)

    if normalized_days:
        dirty_days = _load_dirty_days(user_id)
        dirty_days.update(normalized_days)
        _store_dirty_days(user_id, dirty_days)
        _set_history_version(user_id)

    if normalized_days:
        logger.info(
            "stats_day_invalidate user_id=%s days=%s reason=%s",
            user_id,
            len(normalized_days),
            reason or "unspecified",
        )


def _collect_stale_reading_score_days(user, day_whitelist: set[date] | None = None) -> set[date]:
    """Return reading activity days where cached score metadata is stale."""
    active_media_types = set(getattr(user, "get_active_media_types", lambda: [])())
    if not active_media_types:
        active_media_types = set(MediaTypes.values)

    expected_by_day: dict[date, list[tuple[str, int, float]]] = defaultdict(list)
    for media_type in (MediaTypes.ANIME.value, MediaTypes.BOOK.value, MediaTypes.COMIC.value, MediaTypes.MANGA.value):
        if media_type not in active_media_types:
            continue
        model = apps.get_model("app", media_type)
        rows = model.objects.filter(
            user=user,
            score__isnull=False,
        ).values(
            "item_id",
            "score",
            "start_date",
            "end_date",
            "created_at",
        ).iterator(chunk_size=500)
        for row in rows:
            item_id = row.get("item_id")
            if not item_id:
                continue
            activity_key = history_cache.history_day_key(
                row.get("end_date") or row.get("start_date") or row.get("created_at"),
            )
            day = _normalize_day_value(activity_key)
            if not day:
                continue
            if day_whitelist is not None and day not in day_whitelist:
                continue
            score = row.get("score")
            try:
                expected_score = float(score)
            except (TypeError, ValueError):
                continue
            expected_by_day[day].append((media_type, int(item_id), expected_score))

    if not expected_by_day:
        return set()

    key_map = {day: _day_cache_key(user.id, day) for day in expected_by_day}
    cached_payloads = cache.get_many(key_map.values())

    stale_days = set()
    for day, expected_entries in expected_by_day.items():
        day_payload = cached_payloads.get(key_map[day])
        if not isinstance(day_payload, dict):
            stale_days.add(day)
            continue
        items_payload = day_payload.get("items") or {}
        for media_type, item_id, expected_score in expected_entries:
            media_items = items_payload.get(media_type) or {}
            item_meta = media_items.get(str(item_id))
            if item_meta is None:
                item_meta = media_items.get(item_id)
            if not isinstance(item_meta, dict):
                stale_days.add(day)
                break
            cached_score = item_meta.get("score")
            if cached_score is None:
                stale_days.add(day)
                break
            try:
                cached_score_value = float(cached_score)
            except (TypeError, ValueError):
                stale_days.add(day)
                break
            if abs(cached_score_value - expected_score) > 1e-6:
                stale_days.add(day)
                break

    return stale_days


def build_statistics_data(user, start_date, end_date):
    """Build statistics data for a user and date range.
    
    This extracts the computation logic from the statistics() view.
    Returns a dictionary with all statistics data needed for the view.
    """
    # Get all user media data in a single operation
    user_media, media_count = stats.get_user_media(
        user,
        start_date,
        end_date,
    )

    # Handle season_enabled preference
    if not user.season_enabled:
        season_key = MediaTypes.SEASON.value
        season_count = media_count.pop(season_key, 0)
        if season_count:
            media_count["total"] = max(media_count.get("total", 0) - season_count, 0)
        user_media.pop(season_key, None)

    # Calculate minutes per media type (used by multiple stats)
    minutes_per_media_type = stats.calculate_minutes_per_media_type(
        user_media,
        start_date,
        end_date,
        user=user,
    )
    # Calculate all statistics from the retrieved data
    media_type_distribution = stats.get_media_type_distribution(
        media_count,
        minutes_per_media_type,
    )
    score_distribution, top_rated, top_rated_by_type = stats.get_score_distribution(user_media)
    status_distribution = stats.get_status_distribution(user_media)
    status_pie_chart_data = stats.get_status_pie_chart_data(
        status_distribution,
    )
    top_played = stats.get_top_played_media(user_media, start_date, end_date)
    top_talent = _aggregate_top_talent(user, start_date, end_date)

    # Calculate hours and detailed consumption summaries
    hours_per_media_type = stats.get_hours_per_media_type(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
        duration_format=user.duration_format,
    )
    tv_consumption = stats.get_tv_consumption_stats(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
    )
    movie_consumption = stats.get_movie_consumption_stats(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
    )
    anime_consumption = stats.get_anime_consumption_stats(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
    )
    music_consumption = stats.get_music_consumption_stats(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
    )
    podcast_consumption = stats.get_podcast_consumption_stats(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
        user=user,
    )
    game_consumption = stats.get_game_consumption_stats(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
        user=user,
    )
    book_consumption = stats.get_reading_consumption_stats(
        user_media,
        start_date,
        end_date,
        MediaTypes.BOOK.value,
    )
    comic_consumption = stats.get_reading_consumption_stats(
        user_media,
        start_date,
        end_date,
        MediaTypes.COMIC.value,
    )
    manga_consumption = stats.get_reading_consumption_stats(
        user_media,
        start_date,
        end_date,
        MediaTypes.MANGA.value,
    )

    # Daily hours per media type (used by the Activity History-attached chart)
    daily_hours_by_media_type = stats.get_daily_hours_by_media_type(
        user_media,
        start_date,
        end_date,
    )

    activity_data = stats.get_activity_data(
        user, start_date, end_date, daily_hours_data=daily_hours_by_media_type
    )

    return {
        "media_count": media_count,
        "activity_data": activity_data,
        "media_type_distribution": media_type_distribution,
        "score_distribution": score_distribution,
        "top_rated": top_rated,
        "top_rated_by_type": top_rated_by_type,
        "top_played": top_played,
        "top_talent": top_talent,
        "status_distribution": status_distribution,
        "status_pie_chart_data": status_pie_chart_data,
        "minutes_per_media_type": minutes_per_media_type,
        "hours_per_media_type": hours_per_media_type,
        "tv_consumption": tv_consumption,
        "movie_consumption": movie_consumption,
        "anime_consumption": anime_consumption,
        "music_consumption": music_consumption,
        "podcast_consumption": podcast_consumption,
        "game_consumption": game_consumption,
        "book_consumption": book_consumption,
        "comic_consumption": comic_consumption,
        "manga_consumption": manga_consumption,
        "daily_hours_by_media_type": daily_hours_by_media_type,
    }


def _get_empty_statistics_data():
    """Return an empty statistics data structure with all required keys.
    
    Used when cache is missing and refresh is in progress to avoid
    expensive inline rebuilds that cause page load delays.
    """
    return {
        "media_count": {"total": 0},
        "activity_data": [],
        "media_type_distribution": {},
        "score_distribution": {},
        "top_rated": [],
        "top_rated_by_type": {},
        "top_played": [],
        "top_talent": {
            "sort_by": "plays",
            "by_sort": {
                "plays": {
                    "top_actors": [],
                    "top_actresses": [],
                    "top_directors": [],
                    "top_writers": [],
                    "top_studios": [],
                },
                "time": {
                    "top_actors": [],
                    "top_actresses": [],
                    "top_directors": [],
                    "top_writers": [],
                    "top_studios": [],
                },
                "titles": {
                    "top_actors": [],
                    "top_actresses": [],
                    "top_directors": [],
                    "top_writers": [],
                    "top_studios": [],
                },
            },
            "top_actors": [],
            "top_actresses": [],
            "top_directors": [],
            "top_writers": [],
            "top_studios": [],
        },
        "status_distribution": {},
        "status_pie_chart_data": {},
        "minutes_per_media_type": {},
        "hours_per_media_type": {},
        "tv_consumption": {},
        "movie_consumption": {},
        "anime_consumption": {},
        "music_consumption": {},
        "podcast_consumption": {},
        "game_consumption": {
            "hours": {
                "total": 0,
                "per_year": 0,
                "per_month": 0,
                "per_day": 0,
            },
            "charts": {
                "by_year": {"labels": [], "datasets": []},
                "by_month": {"labels": [], "datasets": []},
                "by_daily_average": {"labels": [], "datasets": [], "top_games_per_band": {}},
            },
            "has_data": False,
            "top_genres": [],
            "top_daily_average_games": [],
            "platform_breakdown": [],
        },
        "book_consumption": {},
        "comic_consumption": {},
        "manga_consumption": {},
        "daily_hours_by_media_type": {},
        "history_highlights": {
            "first_play": None,
            "last_play": None,
            "today_in_history": None,
            "today_in_user_history": None,
            "today_in_history_year": None,
            "today_in_user_history_year": None,
            "today_month": None,
            "today_day": None,
        },
    }



def cache_statistics_data(user_id: int, range_name: str, data: dict, history_version: str | None = None):
    """Persist the statistics data in cache."""
    cache_key = _cache_key(user_id, range_name)
    _normalize_hours_per_media_type(data.get("hours_per_media_type"))
    cache_entry = {
        "data": data,
        "built_at": timezone.now(),
        "history_version": history_version or _get_history_version(user_id),
    }
    cache.set(cache_key, cache_entry, timeout=STATISTICS_CACHE_TIMEOUT)
    logger.debug("Cached statistics data for user %s, range %s", user_id, range_name)


def range_needs_top_talent_upgrade(user_id: int, range_name: str) -> bool:
    """Return True when cached top_talent payload is missing precomputed by_sort data."""
    if range_name not in PREDEFINED_RANGES:
        return False

    cache_entry = cache.get(_cache_key(user_id, range_name))
    if not isinstance(cache_entry, dict):
        return False

    data = cache_entry.get("data")
    if not isinstance(data, dict):
        return True

    top_talent = data.get("top_talent")
    if not isinstance(top_talent, dict):
        return True

    by_sort = top_talent.get("by_sort")
    if not isinstance(by_sort, dict):
        return True

    for mode in ("plays", "time", "titles"):
        if not isinstance(by_sort.get(mode), dict):
            return True

    return False


def get_top_talent_data(user, start_date, end_date, range_name=None):
    """Return top_talent payload without rebuilding the full statistics page payload."""
    if range_name in PREDEFINED_RANGES:
        cache_entry = cache.get(_cache_key(user.id, range_name))
        if isinstance(cache_entry, dict):
            data = cache_entry.get("data") or {}
            top_talent = data.get("top_talent")
            if isinstance(top_talent, dict) and isinstance(top_talent.get("by_sort"), dict):
                return top_talent

    return _aggregate_top_talent(user, start_date, end_date)


def get_statistics_minutes_by_type(user, start_date, end_date, range_name=None):
    """Return only minute totals for a statistics range.

    This avoids rebuilding the full statistics payload for lightweight comparison cards.
    """
    if range_name in PREDEFINED_RANGES:
        cache_entry = cache.get(_cache_key(user.id, range_name))
        if isinstance(cache_entry, dict):
            data = cache_entry.get("data") or {}
            minutes_per_type = data.get("minutes_per_media_type")
            if isinstance(minutes_per_type, dict):
                return minutes_per_type

    day_list = _resolve_day_list(user, start_date, end_date)
    if not day_list:
        return {}
    return _aggregate_minutes_per_media_type_from_days(
        user,
        day_list,
        build_missing=True,
    )



def get_statistics_data(user, start_date, end_date, range_name=None):
    """Return cached statistics, rebuilding if needed.
    
    Always returns cached data if available (even if stale) to avoid timeouts.
    Schedules background refresh if cache is stale.
    
    Args:
        user: User instance
        start_date: Start date for statistics (datetime or None)
        end_date: End date for statistics (datetime or None)
        range_name: Predefined range name (e.g., "Last 12 Months") or None
    
    Returns:
        Dictionary with statistics data
    """
    # Only cache predefined ranges
    if range_name is None or range_name not in PREDEFINED_RANGES:
        # For custom ranges, aggregate per-day caches to avoid range scans
        day_list = _resolve_day_list(user, start_date, end_date)
        if not day_list:
            return _get_empty_statistics_data()
        data = _aggregate_statistics_from_days(
            user,
            day_list,
            start_date,
            end_date,
            build_missing=True,
        )
        _normalize_hours_per_media_type(data.get("hours_per_media_type"))
        _normalize_history_highlight_images(data.get("history_highlights"))
        return data

    eager_mode = bool(
        getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)
        or getattr(settings, "TESTING", False),
    )

    cache_entry = cache.get(_cache_key(user.id, range_name))
    if cache_entry:
        # Always return cached data if it exists (even if stale)
        # This prevents timeouts while background refresh is in progress
        built_at = cache_entry.get("built_at")
        history_version = cache_entry.get("history_version")
        current_version = _get_history_version(user.id)
        if history_version and history_version != current_version:
            if eager_mode:
                data = refresh_statistics_cache(user.id, range_name)
                if data:
                    _normalize_hours_per_media_type(data.get("hours_per_media_type"))
                    _normalize_history_highlight_images(data.get("history_highlights"))
                    return data
            schedule_statistics_refresh(user.id, range_name, allow_inline=False)
        elif not history_version:
            if not built_at or timezone.now() - built_at > STATISTICS_STALE_AFTER:
                if eager_mode:
                    data = refresh_statistics_cache(user.id, range_name)
                    if data:
                        _normalize_hours_per_media_type(data.get("hours_per_media_type"))
                        _normalize_history_highlight_images(data.get("history_highlights"))
                        return data
                schedule_statistics_refresh(user.id, range_name, allow_inline=False)
        data = cache_entry.get("data", {})
        _normalize_hours_per_media_type(data.get("hours_per_media_type"))
        _normalize_history_highlight_images(data.get("history_highlights"))
        return data

    # Cache miss - check if refresh is in progress
    refresh_lock_key = _refresh_lock_key(user.id, range_name)
    refresh_lock = cache.get(refresh_lock_key)
    if refresh_lock and _lock_is_stale(refresh_lock):
        cache.delete(refresh_lock_key)
        refresh_lock = None
    if refresh_lock is not None:
        if eager_mode:
            data = refresh_statistics_cache(user.id, range_name)
            if data:
                _normalize_hours_per_media_type(data.get("hours_per_media_type"))
                _normalize_history_highlight_images(data.get("history_highlights"))
                return data
        # Refresh is in progress, return minimal empty data structure
        # Frontend will poll and update when refresh completes
        # Don't build full statistics here - that's expensive and causes delays
        logger.debug("Statistics cache miss but refresh in progress for user %s, range %s, returning empty structure", user.id, range_name)
        return _get_empty_statistics_data()

    current_version = _get_history_version(user.id)
    if _has_covering_range_cache(
        user.id,
        range_name,
        start_date,
        end_date,
        current_version,
    ):
        return _build_predefined_range_from_day_caches(
            user,
            start_date,
            end_date,
            range_name,
            current_version,
        )

    # No cache and no refresh in progress.
    # In eager mode (tests), build inline. Otherwise schedule and return empty.
    if eager_mode:
        data = refresh_statistics_cache(user.id, range_name)
        if data:
            _normalize_hours_per_media_type(data.get("hours_per_media_type"))
            _normalize_history_highlight_images(data.get("history_highlights"))
            return data
        return _get_empty_statistics_data()

    schedule_statistics_refresh(user.id, range_name, allow_inline=False)
    return _get_empty_statistics_data()



# Re-exports — keep all public symbols importable from this module.
from app.statistics_day_builder import (  # noqa: E402
    _day_bounds,
    _day_boundary_datetime,
    _iter_day_range,
    _overlap_day_filter,
    build_stats_for_day,
)
from app.statistics_aggregator import (  # noqa: E402
    _aggregate_minutes_per_media_type_from_days,
    _aggregate_statistics_from_days,
    _build_activity_data,
    _build_daily_hours_chart,
    _build_media_charts_from_counts,
    _compute_metric_breakdown_for_range,
    _empty_reading_consumption,
    _empty_top_talent_payload,
    _fetch_media_objects,
    _parse_activity_dt,
)
from app.statistics_refresh import (  # noqa: E402
    _build_predefined_range_from_day_caches,
    _get_activity_bounds,
    _get_predefined_range_dates,
    _get_sparse_activity_days,
    _has_covering_range_cache,
    invalidate_all_statistics_days,
    _range_cache_covers_days,
    _range_day_bounds,
    _resolve_day_list,
    invalidate_statistics_cache,
    refresh_statistics_cache,
    schedule_all_ranges_refresh,
    schedule_statistics_refresh,
)
