"""Utilities for caching the Statistics page."""

import logging
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from app import statistics as stats
from app.models import MediaTypes

logger = logging.getLogger(__name__)

STATISTICS_CACHE_PREFIX = "statistics_page_v1"
STATISTICS_CACHE_TIMEOUT = 60 * 60 * 6  # 6 hours
STATISTICS_STALE_AFTER = timedelta(minutes=15)
STATISTICS_REFRESH_LOCK_PREFIX = f"{STATISTICS_CACHE_PREFIX}_refresh_lock"

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

    # Calculate all statistics from the retrieved data
    media_type_distribution = stats.get_media_type_distribution(
        media_count,
    )
    score_distribution, top_rated = stats.get_score_distribution(user_media)
    status_distribution = stats.get_status_distribution(user_media)
    status_pie_chart_data = stats.get_status_pie_chart_data(
        status_distribution,
    )
    top_played = stats.get_top_played_media(user_media, start_date, end_date)
    
    # Calculate hours and detailed consumption summaries
    minutes_per_media_type = stats.calculate_minutes_per_media_type(
        user_media,
        start_date,
        end_date,
    )
    hours_per_media_type = stats.get_hours_per_media_type(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
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
    music_consumption = stats.get_music_consumption_stats(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
    )

    # Daily hours per media type (used by the Activity History-attached chart)
    daily_hours_by_media_type = stats.get_daily_hours_by_media_type(
        user_media,
        start_date,
        end_date,
    )

    activity_data = stats.get_activity_data(user, start_date, end_date)

    return {
        "media_count": media_count,
        "activity_data": activity_data,
        "media_type_distribution": media_type_distribution,
        "score_distribution": score_distribution,
        "top_rated": top_rated,
        "top_played": top_played,
        "status_distribution": status_distribution,
        "status_pie_chart_data": status_pie_chart_data,
        "hours_per_media_type": hours_per_media_type,
        "tv_consumption": tv_consumption,
        "movie_consumption": movie_consumption,
        "music_consumption": music_consumption,
        "daily_hours_by_media_type": daily_hours_by_media_type,
    }


def cache_statistics_data(user_id: int, range_name: str, data: dict):
    """Persist the statistics data in cache."""
    cache_key = _cache_key(user_id, range_name)
    cache_entry = {
        "data": data,
        "built_at": timezone.now(),
    }
    cache.set(
        cache_key,
        cache_entry,
        timeout=STATISTICS_CACHE_TIMEOUT,
    )
    logger.debug("Cached statistics data for user %s, range %s", user_id, range_name)


def get_statistics_data(user, start_date, end_date, range_name=None):
    """Return cached statistics, rebuilding if needed.
    
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
        # For custom ranges, compute inline without caching
        return build_statistics_data(user, start_date, end_date)
    
    cache_entry = cache.get(_cache_key(user.id, range_name))
    if cache_entry:
        built_at = cache_entry.get("built_at")
        if built_at and timezone.now() - built_at > STATISTICS_STALE_AFTER:
            # Cache is stale, schedule background refresh
            schedule_statistics_refresh(user.id, range_name)
        return cache_entry.get("data", {})

    # Cache miss, build inline
    data = build_statistics_data(user, start_date, end_date)
    cache_statistics_data(user.id, range_name, data)
    return data


def invalidate_statistics_cache(user_id: int, range_name: str = None):
    """Remove cached statistics for a user.
    
    Args:
        user_id: User ID
        range_name: Specific range to invalidate, or None to invalidate all ranges
    """
    if range_name:
        if range_name in PREDEFINED_RANGES:
            cache.delete(_cache_key(user_id, range_name))
            logger.debug("Invalidated statistics cache for user %s, range %s", user_id, range_name)
    else:
        # Invalidate all predefined ranges
        for range_name_item in PREDEFINED_RANGES:
            cache.delete(_cache_key(user_id, range_name_item))
        logger.debug("Invalidated all statistics caches for user %s", user_id)


def refresh_statistics_cache(user_id: int, range_name: str):
    """Rebuild and store statistics for a user and range."""
    user_model = get_user_model()
    try:
        user = user_model.objects.get(id=user_id)
    except user_model.DoesNotExist:
        return None

    if range_name not in PREDEFINED_RANGES:
        logger.warning("Attempted to refresh cache for non-predefined range: %s", range_name)
        return None

    # Calculate date range from range name
    today = timezone.localdate()
    start_date = None
    end_date = None
    
    if range_name == "All Time":
        start_date = None
        end_date = None
    elif range_name == "Today":
        start_date = timezone.make_aware(datetime.combine(today, datetime.min.time()))
        end_date = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    elif range_name == "Yesterday":
        yesterday = today - timedelta(days=1)
        start_date = timezone.make_aware(datetime.combine(yesterday, datetime.min.time()))
        end_date = timezone.make_aware(datetime.combine(yesterday, datetime.max.time()))
    elif range_name == "This Week":
        monday = today - timedelta(days=today.weekday())
        start_date = timezone.make_aware(datetime.combine(monday, datetime.min.time()))
        end_date = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    elif range_name == "Last 7 Days":
        start_date = timezone.make_aware(datetime.combine(today - timedelta(days=6), datetime.min.time()))
        end_date = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    elif range_name == "This Month":
        month_start = today.replace(day=1)
        start_date = timezone.make_aware(datetime.combine(month_start, datetime.min.time()))
        end_date = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    elif range_name == "Last 30 Days":
        start_date = timezone.make_aware(datetime.combine(today - timedelta(days=29), datetime.min.time()))
        end_date = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    elif range_name == "Last 90 Days":
        start_date = timezone.make_aware(datetime.combine(today - timedelta(days=89), datetime.min.time()))
        end_date = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    elif range_name == "This Year":
        year_start = today.replace(month=1, day=1)
        start_date = timezone.make_aware(datetime.combine(year_start, datetime.min.time()))
        end_date = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    elif range_name == "Last 6 Months":
        six_months_start = today - relativedelta(months=6)
        if six_months_start.day != today.day:
            six_months_start = (six_months_start.replace(day=1) + relativedelta(months=1)) - timedelta(days=1)
        start_date = timezone.make_aware(datetime.combine(six_months_start, datetime.min.time()))
        end_date = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    elif range_name == "Last 12 Months":
        twelve_months_start = today - relativedelta(months=12)
        if twelve_months_start.day != today.day:
            twelve_months_start = (twelve_months_start.replace(day=1) + relativedelta(months=1)) - timedelta(days=1)
        start_date = timezone.make_aware(datetime.combine(twelve_months_start, datetime.min.time()))
        end_date = timezone.make_aware(datetime.combine(today, datetime.max.time()))
    
    data = build_statistics_data(user, start_date, end_date)
    cache_statistics_data(user_id, range_name, data)
    cache.delete(_refresh_lock_key(user_id, range_name))
    return data


def schedule_statistics_refresh(user_id: int, range_name: str, debounce_seconds: int = 30):
    """Queue a background refresh for a user's statistics cache.
    
    Args:
        user_id: User ID
        range_name: Predefined range name
        debounce_seconds: Seconds to debounce refresh requests
    """
    if range_name not in PREDEFINED_RANGES:
        return False
    
    lock_key = _refresh_lock_key(user_id, range_name)
    if debounce_seconds and not cache.add(lock_key, True, debounce_seconds):
        return False

    try:
        from app.tasks import refresh_statistics_cache_task

        refresh_statistics_cache_task.delay(user_id, range_name)
        return True
    except Exception as exc:  # pragma: no cover - Celery not available
        logger.debug(
            "Falling back to inline statistics cache rebuild for user %s, range %s: %s",
            user_id,
            range_name,
            exc,
        )
        refresh_statistics_cache(user_id, range_name)
        return False


def schedule_all_ranges_refresh(user_id: int, debounce_seconds: int = 30):
    """Schedule background refresh for all predefined ranges for a user.
    
    This is useful when media changes and we want to refresh all ranges.
    Uses a single lock to debounce all ranges together, then schedules
    them individually so they can run in parallel.
    """
    # Use a single lock for all ranges to prevent thundering herd
    all_ranges_lock_key = f"{STATISTICS_REFRESH_LOCK_PREFIX}_all_{user_id}"
    if debounce_seconds and not cache.add(all_ranges_lock_key, True, debounce_seconds):
        # Already scheduled recently, skip
        return
    
    # Schedule all ranges - they'll run in parallel
    for range_name in PREDEFINED_RANGES:
        # Don't use debounce here since we already debounced at the all-ranges level
        # This allows all ranges to be scheduled immediately and run in parallel
        lock_key = _refresh_lock_key(user_id, range_name)
        # Only add lock if not already present (allows parallel execution)
        cache.add(lock_key, True, debounce_seconds)
        
        try:
            from app.tasks import refresh_statistics_cache_task
            refresh_statistics_cache_task.delay(user_id, range_name)
        except Exception as exc:  # pragma: no cover - Celery not available
            logger.debug(
                "Falling back to inline statistics cache rebuild for user %s, range %s: %s",
                user_id,
                range_name,
                exc,
            )
            refresh_statistics_cache(user_id, range_name)
