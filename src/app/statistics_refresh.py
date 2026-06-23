"""Statistics refresh and scheduling — extracted from statistics_cache.py."""

import logging
import time
from datetime import datetime, timedelta

from dateutil.relativedelta import relativedelta
from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Max, Min, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from app import statistics as stats
from app.models import MediaTypes
from app.statistics_aggregator import _aggregate_statistics_from_days
from app.statistics_day_builder import (
    _day_cache_key,
    _iter_day_range,
    _normalize_day_value,
    build_stats_for_day,
)
from app.statistics_highlights import _normalize_history_highlight_images

# Controlled circular: all of these are defined in statistics_cache before the
# re-export block, so they are available on the partial module object when this
# module is first imported (which happens at the bottom of statistics_cache.py).
from app.statistics_cache import (
    PREDEFINED_RANGES,
    STATISTICS_ALL_TIME_REFRESH_DELAY,
    STATISTICS_REFRESH_LOCK_PREFIX,
    STATISTICS_SCHEDULE_DEDUPE_TTL,
    STATISTICS_TASK_PRIORITY_BACKGROUND,
    STATISTICS_TASK_PRIORITY_FOLLOWUP,
    STATISTICS_TASK_PRIORITY_INTERACTIVE,
    STATISTICS_WARM_DAYS,
    _cache_key,
    _collect_stale_reading_score_days,
    _get_empty_statistics_data,
    _get_history_version,
    _load_dirty_days,
    _lock_is_stale,
    _maybe_clear_metadata_refresh,
    _normalize_hours_per_media_type,
    _preferred_range_for_user,
    _refresh_lock_key,
    _schedule_dedupe_key,
    _set_history_version,
    _store_dirty_days,
    cache_statistics_data,
)

logger = logging.getLogger(__name__)

def _range_day_bounds(start_date, end_date):
    start_day = _normalize_day_value(start_date)
    end_day = _normalize_day_value(end_date)
    if start_day and end_day and start_day > end_day:
        start_day, end_day = end_day, start_day
    return start_day, end_day


def _range_cache_covers_days(range_name: str, start_day, end_day) -> bool:
    if not start_day or not end_day:
        return False
    if range_name == "All Time":
        return True

    cached_start, cached_end = _get_predefined_range_dates(range_name)
    cached_start_day, cached_end_day = _range_day_bounds(cached_start, cached_end)
    if not cached_start_day or not cached_end_day:
        return False
    return cached_start_day <= start_day and cached_end_day >= end_day


def _has_covering_range_cache(user_id: int, range_name: str, start_date, end_date, history_version: str) -> bool:
    start_day, end_day = _range_day_bounds(start_date, end_date)
    if not start_day or not end_day:
        return False

    for candidate_range in PREDEFINED_RANGES:
        if candidate_range == range_name:
            continue
        cache_entry = cache.get(_cache_key(user_id, candidate_range))
        if not isinstance(cache_entry, dict):
            continue
        if cache_entry.get("history_version") != history_version:
            continue
        if _range_cache_covers_days(candidate_range, start_day, end_day):
            return True
    return False


def _build_predefined_range_from_day_caches(user, start_date, end_date, range_name: str, history_version: str):
    day_list = _resolve_day_list(user, start_date, end_date)
    if not day_list:
        data = _get_empty_statistics_data()
        cache_statistics_data(user.id, range_name, data, history_version=history_version)
        return data

    data = _aggregate_statistics_from_days(
        user,
        day_list,
        start_date,
        end_date,
        build_missing=False,
    )
    _normalize_hours_per_media_type(data.get("hours_per_media_type"))
    _normalize_history_highlight_images(data.get("history_highlights"))
    cache_statistics_data(user.id, range_name, data, history_version=history_version)
    logger.debug(
        "Derived statistics cache for user %s, range %s from warmed day caches",
        user.id,
        range_name,
    )
    return data


def invalidate_statistics_cache(user_id: int, range_name: str = None):
    """Remove cached statistics for a user.
    
    If a refresh is in progress, keep the old cache so users can see it
    while the refresh completes. Otherwise, delete the cache.
    
    Args:
        user_id: User ID
        range_name: Specific range to invalidate, or None to invalidate all ranges
    """
    if range_name:
        if range_name in PREDEFINED_RANGES:
            # Check if refresh is in progress
            refresh_lock = cache.get(_refresh_lock_key(user_id, range_name))
            if refresh_lock is None:
                # No refresh in progress, safe to delete cache
                cache.delete(_cache_key(user_id, range_name))
                logger.debug("Invalidated statistics cache for user %s, range %s", user_id, range_name)
            # If refresh is in progress, keep the old cache - it will be replaced when refresh completes
            _set_history_version(user_id)
    else:
        # Invalidate all predefined ranges
        for range_name_item in PREDEFINED_RANGES:
            # Check if refresh is in progress for this range
            refresh_lock = cache.get(_refresh_lock_key(user_id, range_name_item))
            if refresh_lock is None:
                # No refresh in progress, safe to delete cache
                cache.delete(_cache_key(user_id, range_name_item))
        logger.debug("Invalidated all statistics caches for user %s", user_id)
        _set_history_version(user_id)


def _get_predefined_range_dates(range_name: str):
    today = timezone.localdate()
    tz = timezone.get_current_timezone()
    if range_name == "All Time":
        return None, None
    if range_name == "Today":
        start = timezone.make_aware(datetime.combine(today, datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(today, datetime.max.time()), tz)
        return start, end
    if range_name == "Yesterday":
        yesterday = today - timedelta(days=1)
        start = timezone.make_aware(datetime.combine(yesterday, datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(yesterday, datetime.max.time()), tz)
        return start, end
    if range_name == "This Week":
        monday = today - timedelta(days=today.weekday())
        start = timezone.make_aware(datetime.combine(monday, datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(today, datetime.max.time()), tz)
        return start, end
    if range_name == "Last 7 Days":
        start = timezone.make_aware(datetime.combine(today - timedelta(days=6), datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(today, datetime.max.time()), tz)
        return start, end
    if range_name == "This Month":
        month_start = today.replace(day=1)
        start = timezone.make_aware(datetime.combine(month_start, datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(today, datetime.max.time()), tz)
        return start, end
    if range_name == "Last 30 Days":
        start = timezone.make_aware(datetime.combine(today - timedelta(days=29), datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(today, datetime.max.time()), tz)
        return start, end
    if range_name == "Last 90 Days":
        start = timezone.make_aware(datetime.combine(today - timedelta(days=89), datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(today, datetime.max.time()), tz)
        return start, end
    if range_name == "This Year":
        year_start = today.replace(month=1, day=1)
        start = timezone.make_aware(datetime.combine(year_start, datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(today, datetime.max.time()), tz)
        return start, end
    if range_name == "Last 6 Months":
        six_months_start = today - relativedelta(months=6)
        if six_months_start.day != today.day:
            six_months_start = (six_months_start.replace(day=1) + relativedelta(months=1)) - timedelta(days=1)
        start = timezone.make_aware(datetime.combine(six_months_start, datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(today, datetime.max.time()), tz)
        return start, end
    if range_name == "Last 12 Months":
        twelve_months_start = today - relativedelta(months=12)
        if twelve_months_start.day != today.day:
            twelve_months_start = (twelve_months_start.replace(day=1) + relativedelta(months=1)) - timedelta(days=1)
        start = timezone.make_aware(datetime.combine(twelve_months_start, datetime.min.time()), tz)
        end = timezone.make_aware(datetime.combine(today, datetime.max.time()), tz)
        return start, end
    return None, None


def _get_activity_bounds(user):
    bounds = []

    def _add_bounds(min_value, max_value):
        if min_value:
            bounds.append(stats._localize_datetime(min_value).date())
        if max_value:
            bounds.append(stats._localize_datetime(max_value).date())

    Episode = apps.get_model("app", "Episode")
    episode_bounds = Episode.objects.filter(
        related_season__user=user,
        end_date__isnull=False,
    ).aggregate(min_date=Min("end_date"), max_date=Max("end_date"))
    _add_bounds(episode_bounds.get("min_date"), episode_bounds.get("max_date"))

    Movie = apps.get_model("app", "Movie")
    movie_bounds = Movie.objects.filter(user=user).aggregate(
        min_end=Min("end_date"),
        max_end=Max("end_date"),
        min_start=Min("start_date"),
        max_start=Max("start_date"),
    )
    _add_bounds(movie_bounds.get("min_end"), movie_bounds.get("max_end"))
    _add_bounds(movie_bounds.get("min_start"), movie_bounds.get("max_start"))

    HistoricalMusic = apps.get_model("app", "HistoricalMusic")
    music_bounds = HistoricalMusic.objects.filter(
        Q(history_user=user) | Q(history_user__isnull=True),
        end_date__isnull=False,
    ).aggregate(min_date=Min("end_date"), max_date=Max("end_date"))
    _add_bounds(music_bounds.get("min_date"), music_bounds.get("max_date"))

    HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")
    podcast_bounds = HistoricalPodcast.objects.filter(
        Q(history_user=user) | Q(history_user__isnull=True),
        end_date__isnull=False,
    ).aggregate(min_date=Min("end_date"), max_date=Max("end_date"))
    _add_bounds(podcast_bounds.get("min_date"), podcast_bounds.get("max_date"))

    for media_type in (MediaTypes.ANIME.value, MediaTypes.GAME.value, MediaTypes.BOARDGAME.value, MediaTypes.MANGA.value, MediaTypes.BOOK.value, MediaTypes.COMIC.value):
        model = apps.get_model("app", media_type)
        media_bounds = model.objects.filter(user=user).aggregate(
            min_end=Min("end_date"),
            max_end=Max("end_date"),
            min_start=Min("start_date"),
            max_start=Max("start_date"),
        )
        _add_bounds(media_bounds.get("min_end"), media_bounds.get("max_end"))
        _add_bounds(media_bounds.get("min_start"), media_bounds.get("max_start"))

    if not bounds:
        return None, None
    return min(bounds), max(bounds)


def _get_sparse_activity_days(user):
    active_media_types = set(getattr(user, "get_active_media_types", lambda: [])())
    if not active_media_types:
        active_media_types = set(MediaTypes.values)

    days = set()
    tz = timezone.get_current_timezone()

    def _add_day(value):
        if not value:
            return
        localized = stats._localize_datetime(value)
        if localized:
            days.add(localized.date())

    def _add_range(start_dt, end_dt):
        if not start_dt or not end_dt:
            return
        start_local = stats._localize_datetime(start_dt)
        end_local = stats._localize_datetime(end_dt)
        if not start_local or not end_local:
            return
        start_day = start_local.date()
        end_day = end_local.date()
        if start_day > end_day:
            start_day, end_day = end_day, start_day
        for offset in range((end_day - start_day).days + 1):
            days.add(start_day + timedelta(days=offset))

    if MediaTypes.TV.value in active_media_types or MediaTypes.SEASON.value in active_media_types:
        Episode = apps.get_model("app", "Episode")
        episode_days = Episode.objects.filter(
            related_season__user=user,
            end_date__isnull=False,
        ).annotate(
            day=TruncDate("end_date", tzinfo=tz),
        ).values_list("day", flat=True).distinct()
        days.update(day for day in episode_days if day)

    if MediaTypes.MOVIE.value in active_media_types:
        Movie = apps.get_model("app", "Movie")
        movie_qs = Movie.objects.filter(user=user)
        movie_end_days = movie_qs.filter(
            end_date__isnull=False,
        ).annotate(
            day=TruncDate("end_date", tzinfo=tz),
        ).values_list("day", flat=True).distinct()
        movie_start_days = movie_qs.filter(
            end_date__isnull=True,
            start_date__isnull=False,
        ).annotate(
            day=TruncDate("start_date", tzinfo=tz),
        ).values_list("day", flat=True).distinct()
        movie_created_days = movie_qs.filter(
            end_date__isnull=True,
            start_date__isnull=True,
        ).annotate(
            day=TruncDate("created_at", tzinfo=tz),
        ).values_list("day", flat=True).distinct()
        days.update(day for day in movie_end_days if day)
        days.update(day for day in movie_start_days if day)
        days.update(day for day in movie_created_days if day)

    if MediaTypes.MUSIC.value in active_media_types:
        HistoricalMusic = apps.get_model("app", "HistoricalMusic")
        music_days = HistoricalMusic.objects.filter(
            Q(history_user=user) | Q(history_user__isnull=True),
            end_date__isnull=False,
        ).annotate(
            day=TruncDate("end_date", tzinfo=tz),
        ).values_list("day", flat=True).distinct()
        days.update(day for day in music_days if day)

    if MediaTypes.PODCAST.value in active_media_types:
        HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")
        podcast_days = HistoricalPodcast.objects.filter(
            Q(history_user=user) | Q(history_user__isnull=True),
            end_date__isnull=False,
        ).annotate(
            day=TruncDate("end_date", tzinfo=tz),
        ).values_list("day", flat=True).distinct()
        days.update(day for day in podcast_days if day)

    for media_type in (
        MediaTypes.ANIME.value,
        MediaTypes.GAME.value,
        MediaTypes.BOARDGAME.value,
        MediaTypes.MANGA.value,
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
    ):
        if media_type not in active_media_types:
            continue
        model = apps.get_model("app", media_type)
        rows = model.objects.filter(user=user).values(
            "start_date",
            "end_date",
            "created_at",
            "progress",
        ).iterator(chunk_size=500)
        for row in rows:
            start_dt = row.get("start_date")
            end_dt = row.get("end_date")
            progress = row.get("progress") or 0
            if start_dt and end_dt and progress > 0:
                _add_range(start_dt, end_dt)
                continue
            activity_dt = end_dt or start_dt or row.get("created_at")
            _add_day(activity_dt)

    return sorted(days)


def _resolve_day_list(user, start_date, end_date):
    if start_date and end_date:
        return _iter_day_range(start_date, end_date)
    return _get_sparse_activity_days(user)


def refresh_statistics_cache(user_id: int, range_name: str):
    """Rebuild and store statistics for a user and range."""
    lock_key = _refresh_lock_key(user_id, range_name)
    user_model = get_user_model()
    try:
        user = user_model.objects.get(id=user_id)
        if range_name not in PREDEFINED_RANGES:
            logger.warning("Attempted to refresh cache for non-predefined range: %s", range_name)
            return None

        start_date, end_date = _get_predefined_range_dates(range_name)
        day_list = _resolve_day_list(user, start_date, end_date)
        day_list_set = set(day_list)

        dirty_days = _load_dirty_days(user_id)
        dirty_set = {day for day in dirty_days if day}
        dirty_dates = { _normalize_day_value(day) for day in dirty_set }

        warm_days = []
        if STATISTICS_WARM_DAYS and day_list:
            today = timezone.localdate()
            for offset in range(STATISTICS_WARM_DAYS):
                warm_day = today - timedelta(days=offset)
                if warm_day in day_list:
                    warm_days.append(warm_day)

        missing_days = set()
        chunk_size = 50
        for offset in range(0, len(day_list), chunk_size):
            chunk = day_list[offset : offset + chunk_size]
            keys = [_day_cache_key(user_id, day) for day in chunk]
            cached = cache.get_many(keys)
            for day, key in zip(chunk, keys, strict=False):
                if key not in cached:
                    missing_days.add(day)

        days_to_refresh = set(warm_days)
        days_to_refresh.update(missing_days)
        days_to_refresh.update(day for day in dirty_dates if day in day_list)
        stale_score_days = _collect_stale_reading_score_days(user, day_whitelist=day_list_set)
        days_to_refresh.update(stale_score_days)

        refreshed_days = 0
        nonempty_days = 0
        credit_backfill_hints = 0
        build_started = time.perf_counter()
        for day in sorted(days_to_refresh):
            if not day:
                continue
            day_stats = build_stats_for_day(user_id, day)
            refreshed_days += 1
            if day_stats:
                credit_backfill_hints += int(
                    day_stats.get("backfill", {}).get("missing_credits") or 0,
                )
                plays_total = sum(day_stats.get("totals", {}).get("plays_by_type", {}).values())
                minutes_total = sum(day_stats.get("totals", {}).get("minutes_by_type", {}).values())
                daily_minutes_total = sum(day_stats.get("daily_minutes_by_type", {}).values())
                if plays_total or minutes_total or daily_minutes_total:
                    nonempty_days += 1

        stats_data = _aggregate_statistics_from_days(
            user,
            day_list,
            start_date,
            end_date,
            build_missing=True,
            credit_backfill_hints=credit_backfill_hints,
        )
        history_version = _get_history_version(user_id)
        cache_statistics_data(user_id, range_name, stats_data, history_version=history_version)

        processed = {day.isoformat() for day in days_to_refresh if day}
        if processed:
            dirty_set.difference_update(processed)
            _store_dirty_days(user_id, dirty_set)

        if stale_score_days:
            logger.info(
                "stats_score_repair user_id=%s range=%s repaired_days=%s",
                user_id,
                range_name,
                len(stale_score_days),
            )

        logger.info(
            "stats_range_summary user_id=%s range=%s days=%s refreshed=%s nonempty=%s elapsed_ms=%.2f totals=%s",
            user_id,
            range_name,
            len(day_list),
            refreshed_days,
            nonempty_days,
            (time.perf_counter() - build_started) * 1000,
            stats_data.get("hours_per_media_type", {}),
        )
        return stats_data
    except user_model.DoesNotExist:
        return None
    finally:
        cache.delete(lock_key)
        _maybe_clear_metadata_refresh(user_id)


def schedule_statistics_refresh(
    user_id: int,
    range_name: str,
    debounce_seconds: int = 30,
    countdown: int = 3,
    allow_inline: bool = True,
    priority: int | None = None,
):
    """Queue a background refresh for a user's statistics cache.
    
    Args:
        user_id: User ID
        range_name: Predefined range name
        debounce_seconds: Seconds to debounce refresh requests
        countdown: Seconds to delay task execution (default 3)
        allow_inline: Whether to run inline if Celery is unavailable
    """
    if range_name not in PREDEFINED_RANGES:
        return False

    history_version = _get_history_version(user_id)
    cache_entry = cache.get(_cache_key(user_id, range_name))
    if cache_entry and cache_entry.get("history_version") == history_version:
        return False

    lock_key = _refresh_lock_key(user_id, range_name)
    lock_value = {"started_at": timezone.now().isoformat()}
    # Use a longer TTL (5 minutes) to ensure lock exists for entire task duration
    # The lock is deleted when task completes, but we need it to last longer than
    # the longest possible task execution time
    lock_ttl = 300  # 5 minutes should be more than enough for any statistics task
    if debounce_seconds and not cache.add(lock_key, lock_value, debounce_seconds):
        existing_lock = cache.get(lock_key)
        if _lock_is_stale(existing_lock):
            cache.delete(lock_key)
            if not cache.add(lock_key, lock_value, debounce_seconds):
                return False
        else:
            return False

    # Extend the lock TTL to cover the full task duration
    # This ensures the lock exists even if the task takes longer than debounce_seconds
    cache.set(lock_key, lock_value, lock_ttl)

    dedupe_key = None
    if cache_entry and STATISTICS_SCHEDULE_DEDUPE_TTL:
        dedupe_key = _schedule_dedupe_key(user_id, range_name, history_version)
        if not cache.add(dedupe_key, True, STATISTICS_SCHEDULE_DEDUPE_TTL):
            cache.delete(lock_key)
            return False

    try:
        from app.tasks import refresh_statistics_cache_task

        refresh_statistics_cache_task.apply_async(
            args=[user_id, range_name],
            countdown=countdown,
            priority=(
                STATISTICS_TASK_PRIORITY_INTERACTIVE
                if priority is None
                else priority
            ),
        )
        return True
    except Exception as exc:  # pragma: no cover - Celery not available
        if allow_inline:
            logger.debug(
                "Falling back to inline statistics cache rebuild for user %s, range %s: %s",
                user_id,
                range_name,
                exc,
            )
            refresh_statistics_cache(user_id, range_name)
            return False
        logger.debug(
            "Failed to schedule statistics refresh for user %s, range %s: %s",
            user_id,
            range_name,
            exc,
        )
        cache.delete(lock_key)
        if dedupe_key:
            cache.delete(dedupe_key)
        return False


def schedule_all_ranges_refresh(
    user_id: int,
    debounce_seconds: int = 30,
    countdown: int = 3,
    preferred_priority: int | None = None,
    all_time_priority: int | None = None,
):
    """Schedule proactive statistics refresh for the ranges most likely to be reopened.

    Media-change flows no longer enqueue every predefined range. They refresh the
    user's preferred range first and only keep "All Time" warm if it already exists.
    Less-frequently used ranges stay lazily repairable on demand via history_version.
    """
    all_ranges_lock_key = f"{STATISTICS_REFRESH_LOCK_PREFIX}_all_{user_id}"
    if debounce_seconds and not cache.add(all_ranges_lock_key, True, debounce_seconds):
        return

    all_time_range = "All Time"
    preferred_range = _preferred_range_for_user(user_id)
    preferred_priority = (
        STATISTICS_TASK_PRIORITY_FOLLOWUP
        if preferred_priority is None
        else preferred_priority
    )
    all_time_priority = (
        STATISTICS_TASK_PRIORITY_BACKGROUND
        if all_time_priority is None
        else all_time_priority
    )
    should_refresh_all_time = (
        preferred_range != all_time_range
        and cache.get(_cache_key(user_id, all_time_range)) is not None
    )

    logger.debug(
        "Scheduling proactive statistics refreshes for user %s (preferred=%s refresh_all_time=%s)",
        user_id,
        preferred_range,
        should_refresh_all_time,
    )
    schedule_statistics_refresh(
        user_id,
        preferred_range,
        debounce_seconds=debounce_seconds,
        countdown=countdown,
        allow_inline=False,
        priority=preferred_priority,
    )
    if should_refresh_all_time:
        schedule_statistics_refresh(
            user_id,
            all_time_range,
            debounce_seconds=debounce_seconds,
            countdown=countdown + STATISTICS_ALL_TIME_REFRESH_DELAY,
            allow_inline=False,
            priority=all_time_priority,
        )

