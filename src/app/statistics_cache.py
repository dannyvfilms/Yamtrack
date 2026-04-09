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
from app.models import (
    CREDITS_BACKFILL_VERSION,
    CreditRoleType,
    Episode,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Movie,
    Person,
    PersonGender,
    Sources,
    Status,
)
from app.templatetags import app_tags

logger = logging.getLogger(__name__)

STATISTICS_CACHE_VERSION = 6
STATISTICS_CACHE_PREFIX = f"statistics_page_v{STATISTICS_CACHE_VERSION}"
STATISTICS_CACHE_TIMEOUT = 60 * 60 * 6  # 6 hours
STATISTICS_STALE_AFTER = timedelta(minutes=15)
STATISTICS_REFRESH_LOCK_PREFIX = f"{STATISTICS_CACHE_PREFIX}_refresh_lock"
STATISTICS_DAY_CACHE_VERSION = 3
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
    for media_type in (MediaTypes.BOOK.value, MediaTypes.COMIC.value, MediaTypes.MANGA.value):
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


def _history_entry_card_payload(entry):
    if not entry:
        return None
    item = entry.get("item")
    if not item:
        return None
    played_at = entry.get("played_at_local")
    fallback_image = entry.get("poster") or getattr(item, "image", "")
    return {
        "entry": entry,
        "item": item,
        "media_type": entry.get("media_type") or getattr(item, "media_type", None),
        "title": entry.get("display_title") or entry.get("title") or getattr(item, "title", ""),
        "image": _get_horizontal_history_image(item, fallback_image),
        "played_at": played_at,
    }


def _get_horizontal_history_image(item, fallback_image):
    """Prefer horizontal artwork when available, matching list hub behavior."""
    if not item:
        return fallback_image or settings.IMG_NONE

    # Handle both dict (serialized) and model instance
    if isinstance(item, dict):
        image = fallback_image or item.get("image", "")
        source = item.get("source")
        media_type = item.get("media_type")
        media_id = item.get("media_id")
    else:
        image = fallback_image or getattr(item, "image", "")
        source = getattr(item, "source", None)
        media_type = getattr(item, "media_type", None)
        media_id = getattr(item, "media_id", None)

    if not source or not media_type or not media_id:
        return image or settings.IMG_NONE

    try:
        from lists.models import CustomList
    except Exception:
        return image or settings.IMG_NONE

    # For episodes/seasons, use the TV show's media_id to get the backdrop
    # Episodes/seasons share the same media_id as their TV show
    if source == Sources.TMDB.value and media_type in (MediaTypes.EPISODE.value, MediaTypes.SEASON.value):
        try:
            backdrop_url = CustomList()._get_tmdb_backdrop(MediaTypes.TV.value, media_id)
            if backdrop_url and backdrop_url != settings.IMG_NONE:
                return backdrop_url
        except Exception:
            pass

    if source == Sources.TMDB.value and media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
        try:
            backdrop_url = CustomList()._get_tmdb_backdrop(media_type, media_id)
            if backdrop_url and backdrop_url != settings.IMG_NONE:
                return backdrop_url
        except Exception:
            pass

    if source == Sources.IGDB.value and media_type == MediaTypes.GAME.value:
        try:
            backdrop_url = CustomList()._get_igdb_backdrop(media_id)
            if backdrop_url and backdrop_url != settings.IMG_NONE:
                return backdrop_url
        except Exception:
            pass

    return image or settings.IMG_NONE


def _normalize_history_highlight_images(history_highlights):
    """Ensure highlight cards prefer horizontal artwork, even for cached payloads."""
    if not isinstance(history_highlights, dict):
        return

    for key in ("first_play", "last_play", "today_in_history", "today_in_user_history"):
        entry = history_highlights.get(key)
        if not isinstance(entry, dict):
            continue
        fallback = entry.get("image") or entry.get("poster")
        entry["image"] = _get_horizontal_history_image(entry.get("item"), fallback)


def _select_history_entry_for_day(day_payload, pick_earliest=False, pick_latest=False):
    if not day_payload:
        return None
    entries = day_payload.get("entries") or []
    if not entries:
        return None
    if pick_earliest:
        entry = entries[-1]
    elif pick_latest:
        entry = entries[0]
    else:
        entry = random.choice(entries)
    return _history_entry_card_payload(entry)


def _get_today_history_entries(user):
    today = timezone.localdate()
    day_keys = history_cache.build_history_index(user)
    matching_dates = []
    for day_key in day_keys:
        try:
            day_date = date.fromisoformat(day_key)
        except ValueError:
            continue
        if day_date.month == today.month and day_date.day == today.day:
            matching_dates.append(day_date)

    if not matching_dates:
        return None, None

    available_years = sorted({day_date.year for day_date in matching_dates})
    selected_year = random.choice(available_years)
    year_dates = [day_date for day_date in matching_dates if day_date.year == selected_year]
    selected_date = random.choice(year_dates) if year_dates else None
    if not selected_date:
        return None, None

    day_payload = history_cache.build_history_day(user, selected_date)
    return _select_history_entry_for_day(day_payload), selected_year


def _get_today_release_entry(user):
    today = timezone.localdate()
    active_types = list(getattr(user, "get_active_media_types", lambda: [])())
    if not active_types:
        active_types = list(MediaTypes.values)
    include_podcasts = MediaTypes.PODCAST.value in active_types
    active_types = [
        media_type
        for media_type in active_types
        if media_type not in (MediaTypes.EPISODE.value, MediaTypes.PODCAST.value)
    ]

    items_by_year = defaultdict(list)
    seen_item_ids = set()

    for media_type in active_types:
        model = apps.get_model("app", media_type)
        qs = (
            model.objects.filter(user=user, item__release_datetime__isnull=False)
            .select_related("item")
            .annotate(
                release_month=ExtractMonth("item__release_datetime"),
                release_day=ExtractDay("item__release_datetime"),
            )
            .filter(release_month=today.month, release_day=today.day)
        )
        for media in qs:
            item = getattr(media, "item", None)
            if not item or item.id in seen_item_ids:
                continue
            seen_item_ids.add(item.id)
            release_dt = getattr(item, "release_datetime", None)
            if not release_dt:
                continue
            localized = stats._localize_datetime(release_dt)
            if not localized:
                continue
            release_date = localized.date()
            items_by_year[release_date.year].append({
                "item": item,
                "media_type": item.media_type,
                "title": item.title,
                "image": _get_horizontal_history_image(item, item.image),
                "release_date": release_date,
            })

    Episode = apps.get_model("app", "Episode")
    episode_qs = (
        Episode.objects.filter(
            related_season__user=user,
            item__release_datetime__isnull=False,
        )
        .select_related(
            "item",
            "related_season__item",
            "related_season__related_tv__item",
        )
        .annotate(
            release_month=ExtractMonth("item__release_datetime"),
            release_day=ExtractDay("item__release_datetime"),
        )
        .filter(release_month=today.month, release_day=today.day)
    )
    for episode in episode_qs:
        episode_item = getattr(episode, "item", None)
        if not episode_item or episode_item.id in seen_item_ids:
            continue
        seen_item_ids.add(episode_item.id)
        release_dt = getattr(episode_item, "release_datetime", None)
        if not release_dt:
            continue
        localized = stats._localize_datetime(release_dt)
        if not localized:
            continue
        release_date = localized.date()
        display_title = history_cache._get_episode_display_title(episode)
        episode_poster = history_cache._get_episode_poster(episode)
        items_by_year[release_date.year].append({
            "item": episode_item,
            "media_type": MediaTypes.EPISODE.value,
            "title": display_title or episode_item.title,
            "image": _get_horizontal_history_image(episode_item, episode_poster),
            "release_date": release_date,
        })

    if include_podcasts:
        Podcast = apps.get_model("app", "Podcast")
        podcast_base = Podcast.objects.filter(user=user).select_related("item", "episode", "show")
        podcast_qs = (
            podcast_base.filter(episode__published__isnull=False)
            .annotate(
                release_month=ExtractMonth("episode__published"),
                release_day=ExtractDay("episode__published"),
            )
            .filter(release_month=today.month, release_day=today.day)
        )
        for podcast in podcast_qs:
            item = getattr(podcast, "item", None)
            if not item or item.id in seen_item_ids:
                continue
            release_dt = getattr(getattr(podcast, "episode", None), "published", None)
            localized = stats._localize_datetime(release_dt) if release_dt else None
            if not localized:
                continue
            release_date = localized.date()
            show = None
            if getattr(podcast, "episode", None) and podcast.episode.show:
                show = podcast.episode.show
            if not show:
                show = podcast.show
            image = (show.image if show and show.image else None) or item.image
            title = item.title or getattr(getattr(podcast, "episode", None), "title", "")
            seen_item_ids.add(item.id)
            items_by_year[release_date.year].append({
                "item": item,
                "media_type": MediaTypes.PODCAST.value,
                "title": title,
                "image": _get_horizontal_history_image(item, image),
                "release_date": release_date,
            })

        podcast_fallback_qs = (
            podcast_base.filter(
                episode__published__isnull=True,
                item__release_datetime__isnull=False,
            )
            .annotate(
                release_month=ExtractMonth("item__release_datetime"),
                release_day=ExtractDay("item__release_datetime"),
            )
            .filter(release_month=today.month, release_day=today.day)
        )
        for podcast in podcast_fallback_qs:
            item = getattr(podcast, "item", None)
            if not item or item.id in seen_item_ids:
                continue
            release_dt = getattr(item, "release_datetime", None)
            localized = stats._localize_datetime(release_dt) if release_dt else None
            if not localized:
                continue
            release_date = localized.date()
            show = None
            if getattr(podcast, "episode", None) and podcast.episode.show:
                show = podcast.episode.show
            if not show:
                show = podcast.show
            image = (show.image if show and show.image else None) or item.image
            title = item.title or getattr(getattr(podcast, "episode", None), "title", "")
            seen_item_ids.add(item.id)
            items_by_year[release_date.year].append({
                "item": item,
                "media_type": MediaTypes.PODCAST.value,
                "title": title,
                "image": _get_horizontal_history_image(item, image),
                "release_date": release_date,
            })

    if not items_by_year:
        return None, None

    available_years = sorted(items_by_year.keys())
    selected_year = random.choice(available_years)
    selected_item = random.choice(items_by_year[selected_year]) if items_by_year[selected_year] else None
    if not selected_item:
        return None, None
    return selected_item, selected_year


def _day_bounds(day_value):
    day = _normalize_day_value(day_value)
    if not day:
        return None, None
    tz = timezone.get_current_timezone()
    day_start = timezone.make_aware(datetime.combine(day, datetime.min.time()), tz)
    day_end = day_start + timedelta(days=1)
    return day_start, day_end


def _day_boundary_datetime(day_value, *, end_of_day=False):
    """Return an aware datetime at the day boundary in the current timezone."""
    day = _normalize_day_value(day_value)
    if not day:
        return None
    tz = timezone.get_current_timezone()
    boundary_time = datetime.max.time() if end_of_day else datetime.min.time()
    return timezone.make_aware(datetime.combine(day, boundary_time), tz)


def _iter_day_range(start_date, end_date):
    if not start_date or not end_date:
        return []
    start_day = start_date.date() if hasattr(start_date, "date") else start_date
    end_day = end_date.date() if hasattr(end_date, "date") else end_date
    if start_day > end_day:
        start_day, end_day = end_day, start_day
    day_count = (end_day - start_day).days + 1
    return [start_day + timedelta(days=offset) for offset in range(day_count)]


def _overlap_day_filter(day_start, day_end):
    return (
        Q(start_date__isnull=False, end_date__isnull=False)
        & ~(Q(end_date__lt=day_start) | Q(start_date__gt=day_end))
    ) | (
        Q(start_date__isnull=False, end_date__isnull=True)
        & Q(start_date__gte=day_start, start_date__lt=day_end)
    ) | (
        Q(start_date__isnull=True, end_date__isnull=False)
        & Q(end_date__gte=day_start, end_date__lt=day_end)
    )


def _safe_runtime_minutes(value):
    if not value:
        return 0
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return 0
    # Exclude fallback values: 999998 (aired but runtime unknown) and 999999 (unknown runtime)
    if minutes >= 999998:
        return 0
    return minutes


def _resolve_missing_credit_item_ids(item_ids):
    """Return TMDB movie/show/episode item IDs that still need credits backfill."""
    normalized_ids = []
    for item_id in item_ids or []:
        try:
            parsed = int(item_id)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            normalized_ids.append(parsed)
    normalized_ids = sorted(set(normalized_ids))
    if not normalized_ids:
        return []

    candidate_items = list(
        Item.objects.filter(
            id__in=normalized_ids,
            source=Sources.TMDB.value,
            media_type__in=[
                MediaTypes.MOVIE.value,
                MediaTypes.TV.value,
                MediaTypes.EPISODE.value,
            ],
        ).values("id", "media_type"),
    )
    if not candidate_items:
        return []

    candidate_ids = {row["id"] for row in candidate_items}
    media_type_by_id = {row["id"]: row["media_type"] for row in candidate_items}
    episode_item_ids = [
        item_id
        for item_id, media_type in media_type_by_id.items()
        if media_type == MediaTypes.EPISODE.value
    ]
    credits_version_by_item_id = {}
    if episode_item_ids:
        credits_version_by_item_id = {
            row["item_id"]: int(row.get("strategy_version") or 0)
            for row in MetadataBackfillState.objects.filter(
                field=MetadataBackfillField.CREDITS,
                item_id__in=episode_item_ids,
            ).values("item_id", "strategy_version")
        }
    person_credit_ids = set(
        ItemPersonCredit.objects.filter(item_id__in=candidate_ids).values_list("item_id", flat=True),
    )
    studio_credit_ids = set(
        ItemStudioCredit.objects.filter(item_id__in=candidate_ids).values_list("item_id", flat=True),
    )

    missing_ids = []
    for item_id in sorted(candidate_ids):
        media_type = media_type_by_id.get(item_id)
        has_people = item_id in person_credit_ids
        has_studios = item_id in studio_credit_ids
        if media_type == MediaTypes.EPISODE.value:
            has_current_episode_attempt = (
                credits_version_by_item_id.get(item_id, 0) >= CREDITS_BACKFILL_VERSION
            )
            if not has_people or not has_current_episode_attempt:
                missing_ids.append(item_id)
            continue
        if not has_people or not has_studios:
            missing_ids.append(item_id)
    return missing_ids


def build_stats_for_day(user_id: int, day_value):
    """Build a per-day statistics payload for a single user."""
    user_model = get_user_model()
    try:
        user = user_model.objects.get(id=user_id)
    except user_model.DoesNotExist:
        return None

    day = _normalize_day_value(day_value)
    if not day:
        return None

    day_start, day_end = _day_bounds(day)
    if not day_start or not day_end:
        return None

    active_media_types = set(getattr(user, "get_active_media_types", lambda: [])())
    if not active_media_types:
        active_media_types = set(MediaTypes.values)

    items_by_type: dict[str, dict[int, dict]] = defaultdict(dict)
    top_played_by_type: dict[str, dict[int, dict]] = defaultdict(dict)
    minutes_by_type: dict[str, float] = defaultdict(float)
    plays_by_type: dict[str, int] = defaultdict(int)
    hour_counts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    daily_minutes_by_type: dict[str, float] = defaultdict(float)
    movie_genres = defaultdict(lambda: {"minutes": 0, "plays": 0})
    tv_genres = defaultdict(lambda: {"minutes": 0, "plays": 0})
    game_genres = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": "", "game_ids": set()})
    reading_genres = {
        MediaTypes.BOOK.value: defaultdict(lambda: {"units": 0, "titles": set(), "name": ""}),
        MediaTypes.COMIC.value: defaultdict(lambda: {"units": 0, "titles": set(), "name": ""}),
        MediaTypes.MANGA.value: defaultdict(lambda: {"units": 0, "titles": set(), "name": ""}),
    }
    music_rollups = {
        "artists": defaultdict(lambda: {"minutes": 0, "plays": 0, "name": "", "image": "", "id": None}),
        "albums": defaultdict(
            lambda: {
                "minutes": 0,
                "plays": 0,
                "title": "",
                "artist": "",
                "artist_id": None,
                "artist_name": "",
                "image": "",
                "id": None,
            },
        ),
        "tracks": defaultdict(
            lambda: {
                "minutes": 0,
                "plays": 0,
                "title": "",
                "artist": "",
                "album": "",
                "album_image": "",
                "album_id": None,
                "album_artist_id": None,
                "album_artist_name": "",
                "id": None,
            },
        ),
        "genres": defaultdict(lambda: {"minutes": 0, "plays": 0, "name": ""}),
        "decades": defaultdict(lambda: {"minutes": 0, "plays": 0, "label": ""}),
        "countries": defaultdict(lambda: {"minutes": 0, "plays": 0, "code": "", "name": ""}),
    }
    podcast_rollups = {
        "shows": defaultdict(lambda: {"minutes": 0, "plays": 0, "title": "", "show": "", "show_id": None, "podcast_uuid": None, "slug": "", "image": ""}),
        "episodes": defaultdict(lambda: {"title": "", "show": "", "show_id": None, "episode_id": None, "podcast_uuid": None, "slug": "", "image": "", "duration_seconds": 0}),
    }
    game_rollups: dict[int, dict] = {}
    missing_runtime = 0
    missing_genres = 0
    play_count = 0
    missing_runtime_item_ids = set()
    missing_genre_item_ids = set()
    missing_episode_runtime_keys = set()
    missing_credit_candidate_item_ids = set()

    def _update_item_meta(media_type: str, item_id: int, media_id: int | None, status, score, activity_dt):
        if not item_id:
            return
        activity_dt = stats._localize_datetime(activity_dt) if activity_dt else None
        score_dt = activity_dt if score is not None else None
        existing = items_by_type[media_type].get(item_id)
        if not existing:
            items_by_type[media_type][item_id] = {
                "item_id": item_id,
                "media_id": media_id,
                "media_type": media_type,
                "status": status,
                "score": float(score) if score is not None else None,
                "score_dt": score_dt,
                "activity_dt": activity_dt,
            }
            return

        existing_activity = existing.get("activity_dt")
        if activity_dt and (not existing_activity or activity_dt > existing_activity):
            existing["media_id"] = media_id or existing.get("media_id")
            existing["status"] = status
            existing["activity_dt"] = activity_dt

        if score is not None:
            existing_score_dt = existing.get("score_dt")
            if existing_score_dt is None or (score_dt and score_dt > existing_score_dt):
                existing["score"] = float(score)
                existing["score_dt"] = score_dt

        items_by_type[media_type][item_id] = existing

    def _update_top_played(media_type: str, item_id: int, media_id: int | None, minutes=0, plays=0, episode_count=0, activity_dt=None):
        if not item_id:
            return
        entry = top_played_by_type[media_type].get(item_id)
        if not entry:
            entry = {
                "item_id": item_id,
                "media_id": media_id,
                "minutes": 0.0,
                "plays": 0,
                "episode_count": 0,
                "activity_dt": None,
            }
            top_played_by_type[media_type][item_id] = entry
        entry["minutes"] += minutes or 0
        entry["plays"] += plays or 0
        entry["episode_count"] += episode_count or 0
        activity_dt = stats._localize_datetime(activity_dt) if activity_dt else None
        if activity_dt and (entry["activity_dt"] is None or activity_dt > entry["activity_dt"]):
            entry["activity_dt"] = activity_dt
            if media_id:
                entry["media_id"] = media_id

    def _add_hour(media_type: str, activity_dt):
        if not activity_dt:
            return
        localized = stats._localize_datetime(activity_dt)
        if not localized:
            return
        hour_counts[media_type][localized.hour] += 1

    def _add_genres(genre_map, genres, minutes):
        if not genres or not minutes:
            return False
        added = False
        for genre in stats._coerce_genre_list(genres):
            key = str(genre).title()
            genre_map[key]["minutes"] += minutes
            genre_map[key]["plays"] += 1
            genre_map[key]["name"] = key
            added = True
        return added

    if MediaTypes.TV.value in active_media_types or MediaTypes.SEASON.value in active_media_types:
        Episode = apps.get_model("app", "Episode")
        episodes = (
            Episode.objects.filter(
                related_season__user=user,
                end_date__gte=day_start,
                end_date__lt=day_end,
            )
            .values(
                "item_id",
                "end_date",
                "item__runtime_minutes",
                "item__media_id",
                "item__source",
                "item__season_number",
                "related_season_id",
                "related_season__item_id",
                "related_season__status",
                "related_season__score",
                "related_season__created_at",
                "related_season__related_tv_id",
                "related_season__related_tv__item_id",
                "related_season__related_tv__status",
                "related_season__related_tv__score",
                "related_season__related_tv__created_at",
                "related_season__related_tv__item__genres",
            )
            .iterator(chunk_size=1000)
        )
        for row in episodes:
            play_dt = row.get("end_date")
            plays_by_type[MediaTypes.TV.value] += 1
            play_count += 1
            _add_hour(MediaTypes.TV.value, play_dt)

            runtime_minutes = _safe_runtime_minutes(row.get("item__runtime_minutes"))
            if runtime_minutes <= 0:
                missing_runtime += 1
                media_id = row.get("item__media_id")
                source = row.get("item__source")
                season_number = row.get("item__season_number")
                if media_id and source and season_number is not None:
                    missing_episode_runtime_keys.add((media_id, source, season_number))
            else:
                minutes_by_type[MediaTypes.TV.value] += runtime_minutes
                daily_minutes_by_type[MediaTypes.TV.value] += runtime_minutes

            tv_item_id = row.get("related_season__related_tv__item_id")
            tv_media_id = row.get("related_season__related_tv_id")
            tv_activity = play_dt or row.get("related_season__related_tv__created_at")
            if tv_item_id:
                _update_item_meta(
                    MediaTypes.TV.value,
                    tv_item_id,
                    tv_media_id,
                    row.get("related_season__related_tv__status"),
                    row.get("related_season__related_tv__score"),
                    tv_activity,
                )
                _update_top_played(
                    MediaTypes.TV.value,
                    tv_item_id,
                    tv_media_id,
                    minutes=runtime_minutes,
                    plays=1,
                    episode_count=1,
                    activity_dt=play_dt or tv_activity,
                )
                if runtime_minutes > 0 and not _add_genres(
                    tv_genres,
                    row.get("related_season__related_tv__item__genres"),
                    runtime_minutes,
                ):
                    missing_genres += 1
                    if tv_item_id:
                        missing_genre_item_ids.add(tv_item_id)
            if row.get("item__source") == Sources.TMDB.value:
                episode_item_id = row.get("item_id")
                if episode_item_id:
                    missing_credit_candidate_item_ids.add(episode_item_id)
                if tv_item_id:
                    missing_credit_candidate_item_ids.add(tv_item_id)

            season_item_id = row.get("related_season__item_id")
            if season_item_id:
                season_activity = play_dt or row.get("related_season__created_at")
                _update_item_meta(
                    MediaTypes.SEASON.value,
                    season_item_id,
                    row.get("related_season_id"),
                    row.get("related_season__status"),
                    row.get("related_season__score"),
                    season_activity,
                )

    if MediaTypes.MOVIE.value in active_media_types:
        Movie = apps.get_model("app", "Movie")
        movie_rows = (
            Movie.objects.filter(user=user)
            .filter(_overlap_day_filter(day_start, day_end))
            .values(
                "id",
                "end_date",
                "start_date",
                "created_at",
                "status",
                "score",
                "item_id",
                "item__source",
                "item__runtime_minutes",
                "item__genres",
            )
            .iterator(chunk_size=1000)
        )
        for row in movie_rows:
            activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
            _update_item_meta(
                MediaTypes.MOVIE.value,
                row.get("item_id"),
                row.get("id"),
                row.get("status"),
                row.get("score"),
                activity_dt,
            )

            activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
            if activity_dt and day_start <= activity_dt < day_end:
                plays_by_type[MediaTypes.MOVIE.value] += 1
                play_count += 1
                runtime_minutes = _safe_runtime_minutes(row.get("item__runtime_minutes"))
                _add_hour(MediaTypes.MOVIE.value, activity_dt)
                if runtime_minutes > 0:
                    daily_minutes_by_type[MediaTypes.MOVIE.value] += runtime_minutes

                if runtime_minutes <= 0:
                    missing_runtime += 1
                    item_id = row.get("item_id")
                    if item_id:
                        missing_runtime_item_ids.add(item_id)
                if row.get("item__source") == Sources.TMDB.value:
                    item_id = row.get("item_id")
                    if item_id:
                        missing_credit_candidate_item_ids.add(item_id)

            play_end = row.get("end_date")
            if play_end and day_start <= play_end < day_end:
                runtime_minutes = _safe_runtime_minutes(row.get("item__runtime_minutes"))
                if runtime_minutes > 0:
                    minutes_by_type[MediaTypes.MOVIE.value] += runtime_minutes
                    if not _add_genres(movie_genres, row.get("item__genres"), runtime_minutes):
                        missing_genres += 1
                        item_id = row.get("item_id")
                        if item_id:
                            missing_genre_item_ids.add(item_id)
                    _update_top_played(
                        MediaTypes.MOVIE.value,
                        row.get("item_id"),
                        row.get("id"),
                        minutes=runtime_minutes,
                        plays=1,
                        episode_count=0,
                        activity_dt=play_end,
                    )

    if MediaTypes.ANIME.value in active_media_types:
        Anime = apps.get_model("app", "Anime")
        anime_rows = (
            Anime.objects.filter(user=user)
            .filter(_overlap_day_filter(day_start, day_end))
            .values(
                "id",
                "item_id",
                "end_date",
                "start_date",
                "created_at",
                "status",
                "score",
                "progress",
                "item__runtime_minutes",
                "item__genres",
            )
            .iterator(chunk_size=500)
        )
        for row in anime_rows:
            activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
            _update_item_meta(
                MediaTypes.ANIME.value,
                row.get("item_id"),
                row.get("id"),
                row.get("status"),
                row.get("score"),
                activity_dt,
            )

            runtime_minutes = _safe_runtime_minutes(row.get("item__runtime_minutes"))
            progress = row.get("progress") or 0
            total_minutes = runtime_minutes * progress if runtime_minutes and progress else 0
            if runtime_minutes <= 0 and progress:
                missing_runtime += 1
                item_id = row.get("item_id")
                if item_id:
                    missing_runtime_item_ids.add(item_id)

            end_date = row.get("end_date")
            if end_date and day_start <= end_date < day_end:
                minutes_by_type[MediaTypes.ANIME.value] += total_minutes
                _update_top_played(
                    MediaTypes.ANIME.value,
                    row.get("item_id"),
                    row.get("id"),
                    minutes=total_minutes,
                    plays=1,
                    episode_count=progress,
                    activity_dt=end_date,
                )

            if total_minutes > 0:
                start_dt = row.get("start_date")
                end_dt = row.get("end_date")
                if start_dt and end_dt:
                    start_local = stats._localize_datetime(start_dt).date()
                    end_local = stats._localize_datetime(end_dt).date()
                    if start_local <= day <= end_local:
                        days = (end_local - start_local).days + 1
                        per_day = total_minutes / days if days else total_minutes
                        daily_minutes_by_type[MediaTypes.ANIME.value] += per_day
                else:
                    activity_local = stats._localize_datetime(activity_dt)
                    if activity_local and activity_local.date() == day:
                        daily_minutes_by_type[MediaTypes.ANIME.value] += total_minutes

            if total_minutes > 0 and not stats._coerce_genre_list(row.get("item__genres")):
                missing_genres += 1
                item_id = row.get("item_id")
                if item_id:
                    missing_genre_item_ids.add(item_id)

    if MediaTypes.GAME.value in active_media_types:
        Game = apps.get_model("app", "Game")
        game_rollup_days_counted = set()
        game_rows = (
            Game.objects.filter(user=user)
            .filter(_overlap_day_filter(day_start, day_end))
            .values(
                "id",
                "item_id",
                "end_date",
                "start_date",
                "created_at",
                "status",
                "score",
                "progress",
                "item__genres",
            )
            .iterator(chunk_size=500)
        )
        for row in game_rows:
            activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
            _update_item_meta(
                MediaTypes.GAME.value,
                row.get("item_id"),
                row.get("id"),
                row.get("status"),
                row.get("score"),
                activity_dt,
            )

            total_minutes = row.get("progress") or 0
            if total_minutes <= 0:
                continue

            end_dt = row.get("end_date")
            if end_dt and day_start <= end_dt < day_end:
                minutes_by_type[MediaTypes.GAME.value] += total_minutes

            start_dt = row.get("start_date")
            start_local = stats._localize_datetime(start_dt).date() if start_dt else None
            end_local = stats._localize_datetime(end_dt).date() if end_dt else None
            entry_days = stats._get_entry_play_dates(
                SimpleNamespace(
                    start_date=start_dt,
                    end_date=end_dt,
                    created_at=row.get("created_at"),
                )
            )
            if row.get("item_id") and day in entry_days:
                if row.get("item_id") not in game_rollup_days_counted:
                    rollup = game_rollups.setdefault(
                        row.get("item_id"),
                        {"minutes_total": 0, "days": 0, "activity_dt": None, "media_id": row.get("id")},
                    )
                    rollup["days"] += 1
                    game_rollup_days_counted.add(row.get("item_id"))
            if start_local and end_local:
                if start_local <= day <= end_local:
                    total_days = (end_local - start_local).days + 1
                    per_day = total_minutes / total_days if total_days else total_minutes
                    daily_minutes_by_type[MediaTypes.GAME.value] += per_day
                    _update_top_played(
                        MediaTypes.GAME.value,
                        row.get("item_id"),
                        row.get("id"),
                        minutes=per_day,
                        plays=1 if activity_dt and stats._localize_datetime(activity_dt).date() == day else 0,
                        episode_count=0,
                        activity_dt=activity_dt,
                    )
                    if activity_dt and stats._localize_datetime(activity_dt).date() == day:
                        rollup = game_rollups.setdefault(
                            row.get("item_id"),
                            {"minutes_total": 0, "days": 0, "activity_dt": None, "media_id": row.get("id")},
                        )
                        rollup["minutes_total"] += total_minutes
                        rollup["activity_dt"] = activity_dt
                        rollup["media_id"] = row.get("id")
                        if not _add_genres(game_genres, row.get("item__genres"), total_minutes):
                            missing_genres += 1
                            item_id = row.get("item_id")
                            if item_id:
                                missing_genre_item_ids.add(item_id)
                        game_id = row.get("item_id")
                        if game_id:
                            for genre in stats._coerce_genre_list(row.get("item__genres")):
                                key = str(genre).title()
                                game_genres[key]["game_ids"].add(game_id)
                continue

            activity_local = stats._localize_datetime(activity_dt)
            if activity_local and activity_local.date() == day:
                daily_minutes_by_type[MediaTypes.GAME.value] += total_minutes
                _update_top_played(
                    MediaTypes.GAME.value,
                    row.get("item_id"),
                    row.get("id"),
                    minutes=total_minutes,
                    plays=1,
                    episode_count=0,
                    activity_dt=activity_dt,
                )
                rollup = game_rollups.setdefault(
                    row.get("item_id"),
                    {"minutes_total": 0, "days": 0, "activity_dt": None, "media_id": row.get("id")},
                )
                rollup["minutes_total"] += total_minutes
                rollup["activity_dt"] = activity_dt
                rollup["media_id"] = row.get("id")
                if not _add_genres(game_genres, row.get("item__genres"), total_minutes):
                    missing_genres += 1
                    item_id = row.get("item_id")
                    if item_id:
                        missing_genre_item_ids.add(item_id)
                game_id = row.get("item_id")
                if game_id:
                    for genre in stats._coerce_genre_list(row.get("item__genres")):
                        key = str(genre).title()
                        game_genres[key]["game_ids"].add(game_id)

    if MediaTypes.BOARDGAME.value in active_media_types:
        BoardGame = apps.get_model("app", "BoardGame")
        boardgame_rows = (
            BoardGame.objects.filter(user=user)
            .filter(_overlap_day_filter(day_start, day_end))
            .values(
                "id",
                "item_id",
                "end_date",
                "start_date",
                "created_at",
                "status",
                "score",
                "progress",
            )
            .iterator(chunk_size=500)
        )
        for row in boardgame_rows:
            activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
            _update_item_meta(
                MediaTypes.BOARDGAME.value,
                row.get("item_id"),
                row.get("id"),
                row.get("status"),
                row.get("score"),
                activity_dt,
            )

            total_minutes = row.get("progress") or 0
            if total_minutes <= 0:
                continue

            play_dt = row.get("end_date") or row.get("start_date")
            if play_dt and day_start <= play_dt < day_end:
                minutes_by_type[MediaTypes.BOARDGAME.value] += total_minutes
                _update_top_played(
                    MediaTypes.BOARDGAME.value,
                    row.get("item_id"),
                    row.get("id"),
                    minutes=total_minutes,
                    plays=1,
                    episode_count=0,
                    activity_dt=play_dt,
                )

            start_dt = row.get("start_date")
            end_dt = row.get("end_date")
            start_local = stats._localize_datetime(start_dt).date() if start_dt else None
            end_local = stats._localize_datetime(end_dt).date() if end_dt else None
            if start_local and end_local and start_local <= day <= end_local:
                total_days = (end_local - start_local).days + 1
                per_day = total_minutes / total_days if total_days else total_minutes
                daily_minutes_by_type[MediaTypes.BOARDGAME.value] += per_day
            else:
                activity_local = stats._localize_datetime(activity_dt)
                if activity_local and activity_local.date() == day:
                    daily_minutes_by_type[MediaTypes.BOARDGAME.value] += total_minutes

    if MediaTypes.MUSIC.value in active_media_types:
        HistoricalMusic = apps.get_model("app", "HistoricalMusic")
        music_history = (
            HistoricalMusic.objects.filter(
                Q(history_user=user) | Q(history_user__isnull=True),
                end_date__gte=day_start,
                end_date__lt=day_end,
            )
            .values("id", "end_date", "history_date")
            .iterator(chunk_size=1000)
        )
        plays_by_key = {}
        for record in music_history:
            music_id = record.get("id")
            play_end = record.get("end_date")
            hist_date = record.get("history_date")
            if not music_id or not play_end or not hist_date:
                continue
            key = (music_id, play_end)
            existing = plays_by_key.get(key)
            if not existing:
                plays_by_key[key] = hist_date
                continue
            existing_diff = abs((existing - play_end).total_seconds())
            current_diff = abs((hist_date - play_end).total_seconds())
            if current_diff < existing_diff and current_diff < 86400:
                plays_by_key[key] = hist_date

        music_ids = {key[0] for key in plays_by_key}
        if music_ids:
            Music = apps.get_model("app", "Music")
            music_map = {
                entry.id: entry
                for entry in Music.objects.filter(id__in=music_ids)
                .select_related("item", "artist", "album", "track")
            }
        else:
            music_map = {}

        track_duration_cache = {}
        if music_map:
            album_ids = {music.album_id for music in music_map.values() if music and music.album_id}
            if album_ids:
                Track = apps.get_model("app", "Track")
                track_rows = Track.objects.filter(
                    album_id__in=album_ids,
                    duration_ms__isnull=False,
                ).values("album_id", "title", "duration_ms", "musicbrainz_recording_id")
                for track_data in track_rows:
                    title_key = (track_data["album_id"], track_data["title"])
                    track_duration_cache[title_key] = track_data["duration_ms"]
                    recording_id = track_data.get("musicbrainz_recording_id")
                    if recording_id:
                        recording_key = ("recording", recording_id)
                        track_duration_cache[recording_key] = track_data["duration_ms"]

        for (music_id, play_end), _ in plays_by_key.items():
            music = music_map.get(music_id)
            if not music:
                continue
            runtime_minutes = stats._get_music_runtime_minutes(
                music,
                track_duration_cache=track_duration_cache,
            )
            if runtime_minutes <= 0:
                missing_runtime += 1
                runtime_minutes = 0
            localized = stats._localize_datetime(play_end)
            plays_by_type[MediaTypes.MUSIC.value] += 1
            play_count += 1
            _add_hour(MediaTypes.MUSIC.value, localized)
            if runtime_minutes:
                minutes_by_type[MediaTypes.MUSIC.value] += runtime_minutes
                daily_minutes_by_type[MediaTypes.MUSIC.value] += runtime_minutes

            _update_item_meta(
                MediaTypes.MUSIC.value,
                music.item_id if getattr(music, "item_id", None) else music.id,
                music.id,
                getattr(music, "status", None),
                getattr(music, "score", None),
                play_end,
            )
            if runtime_minutes:
                _update_top_played(
                    MediaTypes.MUSIC.value,
                    music.item_id if getattr(music, "item_id", None) else music.id,
                    music.id,
                    minutes=runtime_minutes,
                    plays=1,
                    episode_count=0,
                    activity_dt=play_end,
                )

            track_key = music.id
            track_stats = music_rollups["tracks"][track_key]
            track_stats["minutes"] += runtime_minutes
            track_stats["plays"] += 1
            track_stats["title"] = music.item.title if music.item else "Unknown"
            track_stats["id"] = music.id

            album = getattr(music, "album", None)
            artist = getattr(music, "artist", None) or getattr(album, "artist", None)
            if artist:
                track_stats["artist"] = artist.name
                artist_stats = music_rollups["artists"][artist.id]
                artist_stats["minutes"] += runtime_minutes
                artist_stats["plays"] += 1
                artist_stats["name"] = artist.name
                artist_stats["image"] = artist.image or ""
                artist_stats["id"] = artist.id

            if album:
                track_stats["album"] = album.title
                track_stats["album_image"] = album.image or track_stats.get("album_image") or ""
                track_stats["album_id"] = album.id
                track_stats["album_artist_id"] = artist.id if artist else None
                track_stats["album_artist_name"] = artist.name if artist else ""
                album_stats = music_rollups["albums"][album.id]
                album_stats["minutes"] += runtime_minutes
                album_stats["plays"] += 1
                album_stats["title"] = album.title
                album_stats["artist"] = artist.name if artist else "Unknown"
                album_stats["artist_id"] = artist.id if artist else None
                album_stats["artist_name"] = artist.name if artist else ""
                album_stats["image"] = album.image or ""
                album_stats["id"] = album.id

            genres = []
            if album and album.genres:
                genres = stats._coerce_genre_list(album.genres)
            elif artist and artist.genres:
                genres = stats._coerce_genre_list(artist.genres)
            elif getattr(music, "track", None) and music.track.genres:
                genres = stats._coerce_genre_list(music.track.genres)

            if runtime_minutes > 0 and not genres:
                missing_genres += 1

            for genre in genres:
                key = str(genre).title()
                genre_stats = music_rollups["genres"][key]
                genre_stats["minutes"] += runtime_minutes
                genre_stats["plays"] += 1
                genre_stats["name"] = key

            release_date = getattr(album, "release_date", None) if album else None
            if release_date and release_date.year:
                decade_label = f"{(release_date.year // 10) * 10}s"
                decade_stats = music_rollups["decades"][decade_label]
                decade_stats["minutes"] += runtime_minutes
                decade_stats["plays"] += 1
                decade_stats["label"] = decade_label

            country_code = getattr(artist, "country", None) if artist else None
            if country_code:
                code_upper = str(country_code).upper()
                country_stats = music_rollups["countries"][code_upper]
                country_stats["minutes"] += runtime_minutes
                country_stats["plays"] += 1
                country_stats["code"] = code_upper
                country_stats["name"] = stats._country_name_from_code(code_upper)

    if MediaTypes.PODCAST.value in active_media_types:
        HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")
        podcast_history = (
            HistoricalPodcast.objects.filter(
                Q(history_user=user) | Q(history_user__isnull=True),
                end_date__gte=day_start,
                end_date__lt=day_end,
            )
            .values("id", "end_date", "history_date", "progress")
            .iterator(chunk_size=1000)
        )
        podcast_plays = defaultdict(dict)
        for record in podcast_history:
            podcast_id = record.get("id")
            play_end = record.get("end_date")
            hist_date = record.get("history_date")
            progress = record.get("progress")
            if not podcast_id or not play_end or not hist_date:
                continue
            plays_for_podcast = podcast_plays[podcast_id]
            existing = plays_for_podcast.get(play_end)
            if not existing:
                plays_for_podcast[play_end] = (hist_date, progress)
            else:
                existing_diff = abs((existing[0] - play_end).total_seconds())
                current_diff = abs((hist_date - play_end).total_seconds())
                if current_diff < existing_diff and current_diff < 86400:
                    plays_for_podcast[play_end] = (hist_date, progress)

        podcast_ids = set(podcast_plays.keys())
        if podcast_ids:
            Podcast = apps.get_model("app", "Podcast")
            podcast_map = {
                podcast.id: podcast
                for podcast in Podcast.objects.filter(id__in=podcast_ids, user=user)
                .select_related("item", "show", "episode", "episode__show")
            }
        else:
            podcast_map = {}

        for podcast_id, plays_for_podcast in podcast_plays.items():
            podcast = podcast_map.get(podcast_id)
            if not podcast:
                continue
            for play_end, (_, history_progress) in plays_for_podcast.items():
                runtime_minutes = stats._get_podcast_runtime_minutes(podcast)
                if runtime_minutes <= 0 and history_progress and history_progress > 0:
                    runtime_minutes = history_progress
                if runtime_minutes <= 0:
                    missing_runtime += 1
                    continue
                localized = stats._localize_datetime(play_end)
                plays_by_type[MediaTypes.PODCAST.value] += 1
                play_count += 1
                _add_hour(MediaTypes.PODCAST.value, localized)
                minutes_by_type[MediaTypes.PODCAST.value] += runtime_minutes
                daily_minutes_by_type[MediaTypes.PODCAST.value] += runtime_minutes

                _update_item_meta(
                    MediaTypes.PODCAST.value,
                    podcast.item_id if getattr(podcast, "item_id", None) else podcast.id,
                    podcast.id,
                    getattr(podcast, "status", None),
                    getattr(podcast, "score", None),
                    play_end,
                )

                show = getattr(podcast, "show", None)
                if show:
                    show_stats = podcast_rollups["shows"][show.id]
                    show_stats["minutes"] += runtime_minutes
                    show_stats["plays"] += 1
                    show_stats["title"] = show.title
                    show_stats["show"] = show.title
                    show_stats["show_id"] = show.id
                    show_stats["podcast_uuid"] = show.podcast_uuid or show_stats.get("podcast_uuid")
                    show_stats["slug"] = show.slug or ""
                    show_stats["image"] = show.image or ""
                else:
                    show_stats = podcast_rollups["shows"][podcast.id]
                    show_stats["minutes"] += runtime_minutes
                    show_stats["plays"] += 1
                    show_stats["title"] = podcast.item.title if podcast.item else "Unknown Show"
                    show_stats["show"] = show_stats["title"]
                    show_stats["image"] = podcast.item.image if podcast.item else ""

                episode = getattr(podcast, "episode", None)
                episode_key = episode.id if episode else podcast.id
                episode_stats = podcast_rollups["episodes"][episode_key]
                if episode:
                    episode_stats["title"] = episode.title
                    episode_stats["episode_id"] = episode.id
                    episode_stats["duration_seconds"] = episode.duration or episode_stats.get("duration_seconds") or 0
                    episode_stats["show"] = episode.show.title if getattr(episode, "show", None) else episode_stats.get("show")
                    episode_stats["show_id"] = episode.show.id if getattr(episode, "show", None) else episode_stats.get("show_id")
                else:
                    episode_stats["title"] = podcast.item.title if podcast.item else "Unknown Episode"
                    episode_stats["episode_id"] = episode_key
                    if podcast.item and podcast.item.runtime_minutes:
                        episode_stats["duration_seconds"] = podcast.item.runtime_minutes * 60
                if show:
                    episode_stats["podcast_uuid"] = show.podcast_uuid or episode_stats.get("podcast_uuid")
                    episode_stats["slug"] = show.slug or ""
                    episode_stats["image"] = show.image or ""
                elif podcast.item:
                    episode_stats["image"] = podcast.item.image or ""

    for media_type in (MediaTypes.MANGA.value, MediaTypes.BOOK.value, MediaTypes.COMIC.value):
        if media_type not in active_media_types:
            continue
        model = apps.get_model("app", media_type)
        rows = (
            model.objects.filter(user=user)
            .filter(_overlap_day_filter(day_start, day_end))
            .values(
                "id",
                "item_id",
                "end_date",
                "start_date",
                "created_at",
                "status",
                "score",
                "progress",
                "item__genres",
            )
            .iterator(chunk_size=500)
        )
        for row in rows:
            activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
            _update_item_meta(
                media_type,
                row.get("item_id"),
                row.get("id"),
                row.get("status"),
                row.get("score"),
                activity_dt,
            )

            play_dt = row.get("end_date") or row.get("start_date")
            if play_dt and day_start <= play_dt < day_end:
                minutes_by_type[media_type] += 60
                _add_hour(media_type, play_dt)

            total_minutes = row.get("progress") or 0
            if total_minutes <= 0:
                continue
            genres = stats._coerce_genre_list(row.get("item__genres"))
            if not genres:
                missing_genres += 1
                item_id = row.get("item_id")
                if item_id:
                    missing_genre_item_ids.add(item_id)
            start_dt = row.get("start_date")
            end_dt = row.get("end_date")
            start_local = stats._localize_datetime(start_dt).date() if start_dt else None
            end_local = stats._localize_datetime(end_dt).date() if end_dt else None
            if start_local and end_local and start_local <= day <= end_local:
                total_days = (end_local - start_local).days + 1
                per_day = total_minutes / total_days if total_days else total_minutes
                daily_minutes_by_type[media_type] += per_day
                _update_top_played(
                    media_type,
                    row.get("item_id"),
                    row.get("id"),
                    minutes=per_day,
                    plays=1 if play_dt and day_start <= play_dt < day_end else 0,
                    activity_dt=activity_dt,
                )
                for genre in genres:
                    key = str(genre).title()
                    reading_genres[media_type][key]["units"] += per_day
                    reading_genres[media_type][key]["name"] = key
                    if row.get("item_id"):
                        reading_genres[media_type][key]["titles"].add(row.get("item_id"))
            else:
                activity_local = stats._localize_datetime(activity_dt)
                if activity_local and activity_local.date() == day:
                    daily_minutes_by_type[media_type] += total_minutes
                    _update_top_played(
                        media_type,
                        row.get("item_id"),
                        row.get("id"),
                        minutes=total_minutes,
                        plays=1 if play_dt and day_start <= play_dt < day_end else 0,
                        activity_dt=activity_dt,
                    )
                    for genre in genres:
                        key = str(genre).title()
                        reading_genres[media_type][key]["units"] += total_minutes
                        reading_genres[media_type][key]["name"] = key
                        if row.get("item_id"):
                            reading_genres[media_type][key]["titles"].add(row.get("item_id"))

    activity_count = play_count
    non_play_types = {
        MediaTypes.ANIME.value,
        MediaTypes.GAME.value,
        MediaTypes.BOARDGAME.value,
        MediaTypes.MANGA.value,
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
    }
    for media_type, minutes in daily_minutes_by_type.items():
        if media_type in non_play_types and minutes and minutes > 0:
            activity_count += 1
    if activity_count == 0 and sum(daily_minutes_by_type.values()) > 0:
        activity_count = 1

    history_version = _get_history_version(user_id)
    day_stats = {
        "computed_at": timezone.now().isoformat(),
        "history_version": history_version,
        "day": day.isoformat(),
        "items": {},
        "top_played": {},
        "totals": {
            "minutes_by_type": dict(minutes_by_type),
            "plays_by_type": dict(plays_by_type),
        },
        "hour_counts": {},
        "genres": {
            "movie": dict(movie_genres),
            "tv": dict(tv_genres),
            "game": {},
            MediaTypes.BOOK.value: {},
            MediaTypes.COMIC.value: {},
            MediaTypes.MANGA.value: {},
        },
        "music": {},
        "podcast": {},
        "game": {},
        "daily_minutes_by_type": dict(daily_minutes_by_type),
        "activity": {"count": activity_count},
    }

    for media_type, items in items_by_type.items():
        day_stats["items"][media_type] = {}
        for item_id, meta in items.items():
            day_stats["items"][media_type][str(item_id)] = {
                **meta,
                "activity_dt": meta["activity_dt"].isoformat() if meta.get("activity_dt") else None,
                "score_dt": meta["score_dt"].isoformat() if meta.get("score_dt") else None,
            }

    for media_type, items in top_played_by_type.items():
        day_stats["top_played"][media_type] = {}
        for item_id, entry in items.items():
            day_stats["top_played"][media_type][str(item_id)] = {
                **entry,
                "activity_dt": entry["activity_dt"].isoformat() if entry.get("activity_dt") else None,
            }

    for media_type, hours in hour_counts.items():
        day_stats["hour_counts"][media_type] = {str(hour): count for hour, count in hours.items()}

    game_genre_payload = {}
    for genre, payload in game_genres.items():
        game_genre_payload[genre] = {
            "minutes": payload["minutes"],
            "plays": payload["plays"],
            "game_ids": sorted({str(game_id) for game_id in payload["game_ids"]}),
            "name": genre,
        }
    day_stats["genres"]["game"] = game_genre_payload

    for reading_type in (MediaTypes.BOOK.value, MediaTypes.COMIC.value, MediaTypes.MANGA.value):
        reading_payload = {}
        for genre, payload in reading_genres[reading_type].items():
            reading_payload[genre] = {
                "units": payload["units"],
                "titles": len(payload["titles"]),
                "name": payload["name"],
            }
        day_stats["genres"][reading_type] = reading_payload

    for key, value in music_rollups.items():
        day_stats["music"][key] = {str(item_id): payload for item_id, payload in value.items()}

    for key, value in podcast_rollups.items():
        day_stats["podcast"][key] = {str(item_id): payload for item_id, payload in value.items()}

    game_payload = {}
    for item_id, payload in game_rollups.items():
        game_payload[str(item_id)] = {
            **payload,
            "activity_dt": payload["activity_dt"].isoformat() if payload.get("activity_dt") else None,
        }
    day_stats["game"]["by_game"] = game_payload
    missing_credit_item_ids = _resolve_missing_credit_item_ids(missing_credit_candidate_item_ids)
    missing_credits = len(missing_credit_item_ids)
    scheduled_credit_backfills = 0

    if (
        missing_runtime_item_ids
        or missing_genre_item_ids
        or missing_episode_runtime_keys
        or missing_credit_item_ids
    ):
        try:
            from app.tasks import (
                enqueue_credits_backfill_items,
                enqueue_episode_runtime_backfill,
                enqueue_genre_backfill_items,
                enqueue_runtime_backfill_items,
            )

            if missing_runtime_item_ids:
                enqueue_runtime_backfill_items(sorted(missing_runtime_item_ids))
            if missing_genre_item_ids:
                enqueue_genre_backfill_items(sorted(missing_genre_item_ids))
            if missing_episode_runtime_keys:
                enqueue_episode_runtime_backfill(sorted(missing_episode_runtime_keys))
            if missing_credit_item_ids:
                queued_credits = enqueue_credits_backfill_items(
                    missing_credit_item_ids,
                    countdown=3,
                )
                if isinstance(queued_credits, int) and queued_credits > 0:
                    scheduled_credit_backfills = queued_credits
        except Exception as exc:  # pragma: no cover - best-effort scheduling
            logger.debug(
                "stats_backfill_schedule_failed user_id=%s day=%s error=%s",
                user_id,
                day.isoformat(),
                exc,
            )
    day_stats["backfill"] = {
        "missing_credits": missing_credits,
        "scheduled_credits": scheduled_credit_backfills,
    }

    cache.set(_day_cache_key(user_id, day), day_stats, timeout=STATISTICS_DAY_CACHE_TIMEOUT)
    if play_count or missing_runtime or missing_credits:
        logger.info(
            (
                "stats_day_summary user_id=%s day=%s plays=%s missing_runtime=%s "
                "missing_genres=%s missing_credits=%s scheduled_credits=%s"
            ),
            user_id,
            day.isoformat(),
            play_count,
            missing_runtime,
            missing_genres,
            missing_credits,
            scheduled_credit_backfills,
        )
    else:
        logger.debug(
            (
                "stats_day_summary user_id=%s day=%s plays=%s missing_runtime=%s "
                "missing_genres=%s missing_credits=%s scheduled_credits=%s"
            ),
            user_id,
            day.isoformat(),
            play_count,
            missing_runtime,
            missing_genres,
            missing_credits,
            scheduled_credit_backfills,
        )
    return day_stats


def _parse_activity_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return stats._localize_datetime(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return stats._localize_datetime(parsed)
    return None


def _compute_metric_breakdown_for_range(total_value, start_date, end_date):
    breakdown = {"total": total_value, "per_year": 0, "per_month": 0, "per_day": 0}
    if not total_value or not start_date or not end_date:
        return breakdown
    start_dt = stats._localize_datetime(start_date)
    end_dt = stats._localize_datetime(end_date)
    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt
    total_days = (end_dt.date() - start_dt.date()).days + 1
    if total_days <= 0:
        total_days = 1
    total_years = max(total_days / 365.25, 1)
    total_months = max(total_days / 30.4375, 1)
    breakdown["per_year"] = total_value / total_years if total_years else total_value
    breakdown["per_month"] = total_value / total_months if total_months else total_value
    breakdown["per_day"] = total_value / total_days if total_days else total_value
    return breakdown


def _build_media_charts_from_counts(day_counts, hour_counts, color, dataset_label):
    empty_chart = {"labels": [], "datasets": []}
    if not day_counts:
        return {
            "by_year": empty_chart,
            "by_month": empty_chart,
            "by_weekday": empty_chart,
            "by_time_of_day": empty_chart,
        }

    year_counts = Counter()
    month_counts = Counter()
    weekday_counts = Counter()
    for day_str, count in day_counts.items():
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        year_counts[day.year] += count
        month_counts[day.month] += count
        weekday_counts[day.weekday()] += count

    sorted_years = sorted(year_counts)
    year_labels = [str(year) for year in sorted_years]
    year_values = [year_counts[year] for year in sorted_years]

    month_labels = [calendar.month_abbr[i] for i in range(1, 13)]
    month_values = [month_counts.get(i, 0) for i in range(1, 13)]

    weekday_map = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
    weekday_order = [6, 0, 1, 2, 3, 4, 5]
    weekday_labels = [weekday_map[index] for index in weekday_order]
    weekday_values = [weekday_counts.get(index, 0) for index in weekday_order]

    hour_labels = [stats._format_hour_label(hour) for hour in range(24)]
    hour_values = [hour_counts.get(str(hour), 0) for hour in range(24)]

    return {
        "by_year": stats._build_single_series_chart(year_labels, year_values, color, dataset_label),
        "by_month": stats._build_single_series_chart(month_labels, month_values, color, dataset_label),
        "by_weekday": stats._build_single_series_chart(weekday_labels, weekday_values, color, dataset_label),
        "by_time_of_day": stats._build_single_series_chart(hour_labels, hour_values, color, dataset_label),
    }


def _build_daily_hours_chart(day_minutes_by_type, day_list):
    labels = [day.isoformat() for day in day_list]
    datasets = []
    ordered_types = list(stats.MEDIA_TYPE_HOURS_ORDER)
    ordered_types.extend(
        [media_type for media_type in day_minutes_by_type.keys() if media_type not in ordered_types]
    )
    for media_type in ordered_types:
        minutes_map = day_minutes_by_type.get(media_type)
        if not minutes_map:
            continue
        totals = [minutes_map.get(label, 0) for label in labels]
        if not totals or sum(totals) == 0:
            continue
        datasets.append({
            "label": app_tags.media_type_readable(media_type),
            "data": [round(minutes / 60, 2) for minutes in totals],
            "background_color": config.get_stats_color(media_type),
        })
    return {"labels": labels, "datasets": datasets}


def _build_activity_data(date_counts, day_minutes_by_type, day_list, start_date, end_date):
    """Build activity data for the calendar heatmap and stats cards.

    Args:
        date_counts: Dict mapping date -> activity count (for heatmap)
        day_minutes_by_type: Dict mapping media_type -> {date_iso_str -> minutes}
        day_list: List of date objects in the filtered range
        start_date: Start of the date range
        end_date: End of the date range
    """
    if end_date is None:
        end_date = timezone.localtime()

    if start_date is None and date_counts:
        min_date = min(date_counts)
        start_date = _day_boundary_datetime(min_date)

    start_date_aligned = stats.get_aligned_monday(start_date)
    if start_date_aligned is None:
        return {
            "calendar_weeks": [],
            "months": [],
            "stats": {
                "most_active_day": None,
                "most_active_day_percentage": 0,
                "current_streak": 0,
                "longest_streak": 0,
                "longest_streak_start": None,
                "longest_streak_end": None,
            },
        }

    date_range = [
        start_date_aligned.date() + timedelta(days=offset)
        for offset in range((end_date.date() - start_date_aligned.date()).days + 1)
    ]

    # Use day_minutes_by_type for most active day calculation (same data as chart)
    most_active_day, day_percentage = stats.calculate_most_active_weekday(
        day_minutes_by_type,
        day_list,
    )
    end_date_value = end_date.date() if hasattr(end_date, "date") else end_date
    streaks = stats.calculate_streak_details(
        date_counts,
        end_date_value,
    )

    activity_data = [
        {
            "date": current_date.strftime("%Y-%m-%d"),
            "count": date_counts.get(current_date, 0),
            "level": stats.get_level(date_counts.get(current_date, 0)),
        }
        for current_date in date_range
    ]

    calendar_weeks = [activity_data[i : i + 7] for i in range(0, len(activity_data), 7)]

    months = []
    mondays_per_month = []
    current_month = date_range[0].strftime("%b") if date_range else None
    monday_count = 0

    for current_date in date_range:
        if current_date.weekday() == 0:
            month = current_date.strftime("%b")
            if current_month != month:
                if current_month is not None:
                    months.append(current_month if monday_count > 1 else "")
                    mondays_per_month.append(monday_count)
                current_month = month
                monday_count = 0
            monday_count += 1

    if monday_count > 1:
        months.append(current_month)
        mondays_per_month.append(monday_count)

    return {
        "calendar_weeks": calendar_weeks,
        "months": list(zip(months, mondays_per_month, strict=False)),
        "stats": {
            "most_active_day": most_active_day,
            "most_active_day_percentage": day_percentage,
            "current_streak": streaks["current_streak"],
            "longest_streak": streaks["longest_streak"],
            "longest_streak_start": streaks["longest_streak_start"],
            "longest_streak_end": streaks["longest_streak_end"],
        },
    }


def _fetch_media_objects(media_refs):
    media_objects = {}
    by_type = defaultdict(list)
    for media_type, media_id in media_refs:
        if media_type and media_id:
            by_type[media_type].append(media_id)

    for media_type, media_ids in by_type.items():
        model = apps.get_model("app", media_type)
        for media in model.objects.filter(id__in=media_ids):
            media_objects[(media_type, media.id)] = media

    return media_objects


def _is_director_credit(credit) -> bool:
    department = (credit.department or "").strip().lower()
    role = (credit.role or "").strip().lower()
    if department == "directing":
        return True
    return "director" in role


def _is_writer_credit(credit) -> bool:
    department = (credit.department or "").strip().lower()
    role = (credit.role or "").strip().lower()
    if department == "writing":
        return True
    return any(keyword in role for keyword in ("writer", "screenplay", "story", "teleplay", "script"))


def get_person_talent_totals(user, person_source, person_id, start_date=None, end_date=None):
    """Return stats-style totals for a single person's primary talent bucket."""
    if not user or not person_source or person_id is None:
        return None

    person = Person.objects.filter(
        source=person_source,
        source_person_id=str(person_id),
    ).first()
    if not person:
        return None

    movie_play_counts = Counter()
    movie_watch_minutes = Counter()
    episode_play_rows = []

    episodes_qs = Episode.objects.filter(
        related_season__user=user,
        end_date__isnull=False,
    )
    if start_date:
        episodes_qs = episodes_qs.filter(end_date__gte=start_date)
    if end_date:
        episodes_qs = episodes_qs.filter(end_date__lte=end_date)
    for episode_item_id, tv_item_id, runtime_minutes in episodes_qs.values_list(
        "item_id",
        "related_season__related_tv__item_id",
        "item__runtime_minutes",
    ).iterator():
        if not tv_item_id:
            continue
        episode_play_rows.append(
            (
                episode_item_id,
                tv_item_id,
                _safe_runtime_minutes(runtime_minutes),
            ),
        )

    movies_qs = Movie.objects.filter(
        user=user,
    ).filter(
        Q(end_date__isnull=False) | Q(start_date__isnull=False),
    )
    if start_date:
        movies_qs = movies_qs.filter(
            Q(end_date__gte=start_date)
            | (Q(end_date__isnull=True) & Q(start_date__gte=start_date)),
        )
    if end_date:
        movies_qs = movies_qs.filter(
            Q(end_date__lte=end_date)
            | (Q(end_date__isnull=True) & Q(start_date__lte=end_date)),
        )
    for item_id, runtime_minutes in movies_qs.values_list("item_id", "item__runtime_minutes").iterator():
        if item_id:
            movie_play_counts[item_id] += 1
            movie_watch_minutes[item_id] += _safe_runtime_minutes(runtime_minutes)

    if not movie_play_counts and not episode_play_rows:
        return None

    movie_item_ids = set(movie_play_counts.keys())
    show_item_ids = {tv_item_id for _, tv_item_id, _ in episode_play_rows if tv_item_id}
    episode_item_ids = {episode_item_id for episode_item_id, _, _ in episode_play_rows if episode_item_id}
    played_item_ids = movie_item_ids | show_item_ids | episode_item_ids
    if not played_item_ids:
        return None
    movie_and_show_item_ids = movie_item_ids | show_item_ids
    item_rows = list(
        Item.objects.filter(
            id__in=movie_and_show_item_ids,
        ).values_list("id", "media_type", "media_id", "source"),
    )
    item_media_key_by_id = {
        item_id: (media_type, str(media_id))
        for item_id, media_type, media_id, _source in item_rows
    }
    item_source_by_id = {
        item_id: source
        for item_id, _media_type, _media_id, source in item_rows
    }

    actor_credit_item_ids = set()
    actress_credit_item_ids = set()
    director_credit_item_ids = set()
    writer_credit_item_ids = set()

    person_credits = ItemPersonCredit.objects.filter(
        item_id__in=played_item_ids,
        person_id=person.id,
    )
    for credit in person_credits:
        if credit.role_type == CreditRoleType.CAST.value:
            if credit.item_id in show_item_ids and not credit_helpers.is_regular_show_cast_credit(
                item_source_by_id.get(credit.item_id),
                credit.sort_order,
            ):
                continue
            if person.gender == PersonGender.MALE.value:
                actor_credit_item_ids.add(credit.item_id)
            elif person.gender == PersonGender.FEMALE.value:
                actress_credit_item_ids.add(credit.item_id)
            continue

        if credit.role_type == CreditRoleType.CREW.value:
            if _is_director_credit(credit):
                director_credit_item_ids.add(credit.item_id)
            if _is_writer_credit(credit):
                writer_credit_item_ids.add(credit.item_id)

    bucket_plays = Counter()
    bucket_minutes = Counter()
    bucket_movie_items = defaultdict(set)
    bucket_show_items = defaultdict(set)
    bucket_minutes_by_media_key = defaultdict(lambda: defaultdict(int))

    role_sources = (
        ("actor", actor_credit_item_ids),
        ("actress", actress_credit_item_ids),
        ("director", director_credit_item_ids),
        ("writer", writer_credit_item_ids),
    )

    for item_id, plays in movie_play_counts.items():
        if plays <= 0:
            continue
        watched_minutes = int(movie_watch_minutes.get(item_id, 0))
        media_key = item_media_key_by_id.get(item_id)
        for bucket, item_ids in role_sources:
            if item_id not in item_ids:
                continue
            bucket_plays[bucket] += plays
            bucket_minutes[bucket] += watched_minutes
            bucket_movie_items[bucket].add(item_id)
            if media_key:
                bucket_minutes_by_media_key[bucket][media_key] += watched_minutes

    for episode_item_id, tv_item_id, watched_minutes in episode_play_rows:
        if not tv_item_id:
            continue
        media_key = item_media_key_by_id.get(tv_item_id)
        for bucket, item_ids in role_sources:
            is_match = episode_item_id in item_ids or tv_item_id in item_ids
            if not is_match:
                continue
            bucket_plays[bucket] += 1
            bucket_minutes[bucket] += watched_minutes
            bucket_show_items[bucket].add(tv_item_id)
            if media_key:
                bucket_minutes_by_media_key[bucket][media_key] += watched_minutes

    bucket_payloads = {}
    for bucket, _ in role_sources:
        unique_movies = len(bucket_movie_items.get(bucket, set()))
        unique_shows = len(bucket_show_items.get(bucket, set()))
        watched_minutes = int(bucket_minutes.get(bucket, 0))
        bucket_payloads[bucket] = {
            "bucket": bucket,
            "plays": int(bucket_plays.get(bucket, 0)),
            "watched_minutes": watched_minutes,
            "watched_time": stats._format_hours_minutes(watched_minutes),
            "unique_movies": unique_movies,
            "unique_shows": unique_shows,
            "unique_titles": unique_movies + unique_shows,
            "minutes_by_media_key": dict(bucket_minutes_by_media_key.get(bucket, {})),
        }

    nonzero_buckets = [
        bucket
        for bucket, payload in bucket_payloads.items()
        if payload["plays"] > 0 or payload["watched_minutes"] > 0
    ]
    if not nonzero_buckets:
        return None

    known_for_department = (person.known_for_department or "").strip().lower()
    preferred_order = []
    if known_for_department == "acting":
        if person.gender == PersonGender.MALE.value:
            preferred_order = ["actor"]
        elif person.gender == PersonGender.FEMALE.value:
            preferred_order = ["actress"]
        else:
            preferred_order = ["actor", "actress"]
    elif known_for_department == "directing":
        preferred_order = ["director"]
    elif known_for_department == "writing":
        preferred_order = ["writer"]

    selected_bucket = None
    for bucket in preferred_order:
        if bucket in nonzero_buckets:
            selected_bucket = bucket
            break

    if selected_bucket is None:
        selected_bucket = max(
            nonzero_buckets,
            key=lambda bucket: (
                bucket_payloads[bucket]["plays"],
                bucket_payloads[bucket]["watched_minutes"],
                bucket_payloads[bucket]["unique_titles"],
            ),
        )

    return bucket_payloads.get(selected_bucket)


def _aggregate_top_talent(user, start_date, end_date, limit=20, schedule_missing_backfill=True):
    """Aggregate top cast/crew/studio rollups from watched movie and TV plays."""
    movie_play_counts = Counter()
    movie_watch_minutes = Counter()
    episode_play_rows = []
    valid_sort_modes = ("plays", "time", "titles")
    sort_by = getattr(user, "top_talent_sort_by", "plays")
    if sort_by not in valid_sort_modes:
        sort_by = "plays"

    def _empty_talent_bucket():
        return {
            "top_actors": [],
            "top_actresses": [],
            "top_directors": [],
            "top_writers": [],
            "top_studios": [],
        }

    # TV plays: track episode play rows so we can prefer episode-level credits.
    episodes_qs = Episode.objects.filter(
        related_season__user=user,
        end_date__isnull=False,
    )
    if start_date:
        episodes_qs = episodes_qs.filter(end_date__gte=start_date)
    if end_date:
        episodes_qs = episodes_qs.filter(end_date__lte=end_date)
    for episode_item_id, tv_item_id, runtime_minutes in episodes_qs.values_list(
        "item_id",
        "related_season__related_tv__item_id",
        "item__runtime_minutes",
    ).iterator():
        if not tv_item_id:
            continue
        episode_play_rows.append(
            (
                episode_item_id,
                tv_item_id,
                _safe_runtime_minutes(runtime_minutes),
            ),
        )

    # Movie plays: count completed/dated movie entries.
    movies_qs = Movie.objects.filter(
        user=user,
    ).filter(
        Q(end_date__isnull=False) | Q(start_date__isnull=False),
    )
    if start_date:
        movies_qs = movies_qs.filter(
            Q(end_date__gte=start_date)
            | (Q(end_date__isnull=True) & Q(start_date__gte=start_date)),
        )
    if end_date:
        movies_qs = movies_qs.filter(
            Q(end_date__lte=end_date)
            | (Q(end_date__isnull=True) & Q(start_date__lte=end_date)),
        )
    for item_id, runtime_minutes in movies_qs.values_list("item_id", "item__runtime_minutes").iterator():
        if item_id:
            movie_play_counts[item_id] += 1
            movie_watch_minutes[item_id] += _safe_runtime_minutes(runtime_minutes)

    if not movie_play_counts and not episode_play_rows:
        by_sort = {mode: _empty_talent_bucket() for mode in valid_sort_modes}
        selected_payload = by_sort.get(sort_by, _empty_talent_bucket())
        return {
            "sort_by": sort_by,
            "by_sort": by_sort,
            **selected_payload,
        }

    movie_item_ids = set(movie_play_counts.keys())
    show_item_ids = {tv_item_id for _, tv_item_id, _ in episode_play_rows if tv_item_id}
    episode_item_ids = {episode_item_id for episode_item_id, _, _ in episode_play_rows if episode_item_id}
    played_item_ids = movie_item_ids | show_item_ids | episode_item_ids
    item_source_by_id = {
        item_id: source
        for item_id, source in Item.objects.filter(id__in=show_item_ids).values_list("id", "source")
    }

    cast_actor_ids_by_item = defaultdict(set)
    cast_actress_ids_by_item = defaultdict(set)
    director_ids_by_item = defaultdict(set)
    writer_ids_by_item = defaultdict(set)
    studio_ids_by_item = defaultdict(set)
    people_by_id = {}
    studios_by_id = {}
    items_with_people = set()
    items_with_studios = set()

    person_credits = ItemPersonCredit.objects.filter(item_id__in=played_item_ids).select_related("person")
    for credit in person_credits:
        person = credit.person
        if not person:
            continue
        items_with_people.add(credit.item_id)
        people_by_id[person.id] = person

        if credit.role_type == CreditRoleType.CAST.value:
            if credit.item_id in show_item_ids and not credit_helpers.is_regular_show_cast_credit(
                item_source_by_id.get(credit.item_id),
                credit.sort_order,
            ):
                continue
            if person.gender == PersonGender.MALE.value:
                cast_actor_ids_by_item[credit.item_id].add(person.id)
            elif person.gender == PersonGender.FEMALE.value:
                cast_actress_ids_by_item[credit.item_id].add(person.id)
            continue

        if credit.role_type == CreditRoleType.CREW.value:
            if _is_director_credit(credit):
                director_ids_by_item[credit.item_id].add(person.id)
            if _is_writer_credit(credit):
                writer_ids_by_item[credit.item_id].add(person.id)

    studio_item_ids = movie_item_ids | show_item_ids
    studio_credits = ItemStudioCredit.objects.filter(item_id__in=studio_item_ids).select_related("studio")
    for credit in studio_credits:
        studio = credit.studio
        if not studio:
            continue
        items_with_studios.add(credit.item_id)
        studios_by_id[studio.id] = studio
        studio_ids_by_item[credit.item_id].add(studio.id)

    tmdb_items = list(
        Item.objects.filter(
            id__in=played_item_ids,
            source=Sources.TMDB.value,
            media_type__in=[
                MediaTypes.MOVIE.value,
                MediaTypes.TV.value,
                MediaTypes.EPISODE.value,
            ],
        ).values_list("id", "media_type"),
    )
    episode_ids = [item_id for item_id, media_type in tmdb_items if media_type == MediaTypes.EPISODE.value]
    credits_version_by_item_id = {}
    if episode_ids:
        credits_version_by_item_id = {
            row["item_id"]: int(row.get("strategy_version") or 0)
            for row in MetadataBackfillState.objects.filter(
                field=MetadataBackfillField.CREDITS,
                item_id__in=episode_ids,
            ).values("item_id", "strategy_version")
        }
    missing_credit_item_ids = []
    for item_id, media_type in tmdb_items:
        has_people = item_id in items_with_people
        has_studios = item_id in items_with_studios
        if media_type == MediaTypes.EPISODE.value:
            has_current_episode_attempt = (
                credits_version_by_item_id.get(item_id, 0) >= CREDITS_BACKFILL_VERSION
            )
            if not has_people or not has_current_episode_attempt:
                missing_credit_item_ids.append(item_id)
            continue
        if not has_people or not has_studios:
            missing_credit_item_ids.append(item_id)
    missing_credit_item_ids = sorted(set(missing_credit_item_ids))

    if missing_credit_item_ids and schedule_missing_backfill:
        try:
            from app.tasks import enqueue_credits_backfill_items

            enqueue_credits_backfill_items(missing_credit_item_ids, countdown=3)
        except Exception as exc:  # pragma: no cover - best effort scheduling
            logger.debug(
                "top_talent_credits_backfill_schedule_failed user_id=%s items=%s error=%s",
                user.id,
                len(missing_credit_item_ids),
                exc,
            )

    actor_counts = Counter()
    actor_minutes = Counter()
    actress_counts = Counter()
    actress_minutes = Counter()
    director_counts = Counter()
    director_minutes = Counter()
    writer_counts = Counter()
    writer_minutes = Counter()
    studio_counts = Counter()
    studio_minutes = Counter()
    actor_movie_items = defaultdict(set)
    actor_show_items = defaultdict(set)
    actress_movie_items = defaultdict(set)
    actress_show_items = defaultdict(set)
    director_movie_items = defaultdict(set)
    director_show_items = defaultdict(set)
    writer_movie_items = defaultdict(set)
    writer_show_items = defaultdict(set)
    studio_movie_items = defaultdict(set)
    studio_show_items = defaultdict(set)

    for item_id, plays in movie_play_counts.items():
        if plays <= 0:
            continue
        watched_minutes = int(movie_watch_minutes.get(item_id, 0))
        for person_id in cast_actor_ids_by_item.get(item_id, ()):
            actor_counts[person_id] += plays
            actor_minutes[person_id] += watched_minutes
            actor_movie_items[person_id].add(item_id)
        for person_id in cast_actress_ids_by_item.get(item_id, ()):
            actress_counts[person_id] += plays
            actress_minutes[person_id] += watched_minutes
            actress_movie_items[person_id].add(item_id)
        for person_id in director_ids_by_item.get(item_id, ()):
            director_counts[person_id] += plays
            director_minutes[person_id] += watched_minutes
            director_movie_items[person_id].add(item_id)
        for person_id in writer_ids_by_item.get(item_id, ()):
            writer_counts[person_id] += plays
            writer_minutes[person_id] += watched_minutes
            writer_movie_items[person_id].add(item_id)
        for studio_id in studio_ids_by_item.get(item_id, ()):
            studio_counts[studio_id] += plays
            studio_minutes[studio_id] += watched_minutes
            studio_movie_items[studio_id].add(item_id)

    for episode_item_id, tv_item_id, watched_minutes in episode_play_rows:
        if not tv_item_id:
            continue

        actor_ids = cast_actor_ids_by_item.get(episode_item_id, set()) | cast_actor_ids_by_item.get(
            tv_item_id,
            set(),
        )
        for person_id in actor_ids:
            actor_counts[person_id] += 1
            actor_minutes[person_id] += watched_minutes
            actor_show_items[person_id].add(tv_item_id)

        actress_ids = cast_actress_ids_by_item.get(episode_item_id, set()) | cast_actress_ids_by_item.get(
            tv_item_id,
            set(),
        )
        for person_id in actress_ids:
            actress_counts[person_id] += 1
            actress_minutes[person_id] += watched_minutes
            actress_show_items[person_id].add(tv_item_id)

        director_ids = director_ids_by_item.get(episode_item_id, set()) | director_ids_by_item.get(
            tv_item_id,
            set(),
        )
        for person_id in director_ids:
            director_counts[person_id] += 1
            director_minutes[person_id] += watched_minutes
            director_show_items[person_id].add(tv_item_id)

        writer_ids = writer_ids_by_item.get(episode_item_id, set()) | writer_ids_by_item.get(
            tv_item_id,
            set(),
        )
        for person_id in writer_ids:
            writer_counts[person_id] += 1
            writer_minutes[person_id] += watched_minutes
            writer_show_items[person_id].add(tv_item_id)

        for studio_id in studio_ids_by_item.get(tv_item_id, ()):
            studio_counts[studio_id] += 1
            studio_minutes[studio_id] += watched_minutes
            studio_show_items[studio_id].add(tv_item_id)

    def _person_sort_key(person_id, plays, minutes, movie_items_by_person, show_items_by_person, mode):
        unique_movies = len(movie_items_by_person.get(person_id, set()))
        unique_shows = len(show_items_by_person.get(person_id, set()))
        unique_titles = unique_movies + unique_shows
        person = people_by_id.get(person_id)
        name_key = person.name.lower() if person else ""
        if mode == "time":
            return (-minutes, -plays, -unique_titles, name_key)
        if mode == "titles":
            return (-unique_titles, -plays, -minutes, name_key)
        return (-plays, -minutes, -unique_titles, name_key)

    def _studio_sort_key(studio_id, plays, minutes, movie_items_by_studio, show_items_by_studio, mode):
        unique_movies = len(movie_items_by_studio.get(studio_id, set()))
        unique_shows = len(show_items_by_studio.get(studio_id, set()))
        unique_titles = unique_movies + unique_shows
        studio = studios_by_id.get(studio_id)
        name_key = studio.name.lower() if studio else ""
        if mode == "time":
            return (-minutes, -plays, -unique_titles, name_key)
        if mode == "titles":
            return (-unique_titles, -plays, -minutes, name_key)
        return (-plays, -minutes, -unique_titles, name_key)

    def _sorted_people(counter_obj, minute_counter, movie_items_by_person, show_items_by_person, mode):
        ranked = sorted(
            counter_obj.items(),
            key=lambda row: _person_sort_key(
                row[0],
                row[1],
                int(minute_counter.get(row[0], 0)),
                movie_items_by_person,
                show_items_by_person,
                mode,
            ),
        )[:limit]
        payload = []
        for person_id, plays in ranked:
            person = people_by_id.get(person_id)
            if not person:
                continue
            watched_minutes = int(minute_counter.get(person_id, 0))
            unique_movies = len(movie_items_by_person.get(person_id, set()))
            unique_shows = len(show_items_by_person.get(person_id, set()))
            payload.append(
                {
                    "name": person.name,
                    "image": person.image or settings.IMG_NONE,
                    "source": person.source,
                    "person_id": person.source_person_id,
                    "plays": int(plays),
                    "watched_minutes": watched_minutes,
                    "watched_time": stats._format_hours_minutes(watched_minutes),
                    "unique_movies": unique_movies,
                    "unique_shows": unique_shows,
                    "unique_titles": unique_movies + unique_shows,
                },
            )
        return payload

    def _sorted_studios(counter_obj, minute_counter, movie_items_by_studio, show_items_by_studio, mode):
        ranked = sorted(
            counter_obj.items(),
            key=lambda row: _studio_sort_key(
                row[0],
                row[1],
                int(minute_counter.get(row[0], 0)),
                movie_items_by_studio,
                show_items_by_studio,
                mode,
            ),
        )[:limit]
        payload = []
        for studio_id, plays in ranked:
            studio = studios_by_id.get(studio_id)
            if not studio:
                continue
            watched_minutes = int(minute_counter.get(studio_id, 0))
            unique_movies = len(movie_items_by_studio.get(studio_id, set()))
            unique_shows = len(show_items_by_studio.get(studio_id, set()))
            payload.append(
                {
                    "name": studio.name,
                    "logo": studio.logo or settings.IMG_NONE,
                    "source": studio.source,
                    "studio_id": studio.source_studio_id,
                    "plays": int(plays),
                    "watched_minutes": watched_minutes,
                    "watched_time": stats._format_hours_minutes(watched_minutes),
                    "unique_movies": unique_movies,
                    "unique_shows": unique_shows,
                    "unique_titles": unique_movies + unique_shows,
                },
            )
        return payload

    by_sort = {}
    for mode in valid_sort_modes:
        by_sort[mode] = {
            "top_actors": _sorted_people(
                actor_counts,
                actor_minutes,
                actor_movie_items,
                actor_show_items,
                mode,
            ),
            "top_actresses": _sorted_people(
                actress_counts,
                actress_minutes,
                actress_movie_items,
                actress_show_items,
                mode,
            ),
            "top_directors": _sorted_people(
                director_counts,
                director_minutes,
                director_movie_items,
                director_show_items,
                mode,
            ),
            "top_writers": _sorted_people(
                writer_counts,
                writer_minutes,
                writer_movie_items,
                writer_show_items,
                mode,
            ),
            "top_studios": _sorted_studios(
                studio_counts,
                studio_minutes,
                studio_movie_items,
                studio_show_items,
                mode,
            ),
        }

    selected_payload = by_sort.get(sort_by, _empty_talent_bucket())
    return {
        "sort_by": sort_by,
        "by_sort": by_sort,
        **selected_payload,
    }


def _aggregate_statistics_from_days(
    user,
    day_list,
    start_date,
    end_date,
    build_missing=False,
    credit_backfill_hints: int = 0,
):
    items_by_type = defaultdict(dict)
    top_played_by_type = defaultdict(dict)
    minutes_by_type = defaultdict(float)
    plays_by_type = defaultdict(int)
    hour_counts = defaultdict(lambda: defaultdict(int))
    day_play_counts = defaultdict(dict)
    day_minutes_by_type = defaultdict(dict)
    movie_genres = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": ""})
    tv_genres = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": ""})
    game_genres = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": "", "game_ids": set()})
    reading_genres = {
        MediaTypes.BOOK.value: defaultdict(lambda: {"units": 0, "titles": 0, "name": ""}),
        MediaTypes.COMIC.value: defaultdict(lambda: {"units": 0, "titles": 0, "name": ""}),
        MediaTypes.MANGA.value: defaultdict(lambda: {"units": 0, "titles": 0, "name": ""}),
    }
    music_rollups = {
        "artists": defaultdict(lambda: {"minutes": 0, "plays": 0, "name": "", "image": "", "id": None}),
        "albums": defaultdict(
            lambda: {
                "minutes": 0,
                "plays": 0,
                "title": "",
                "artist": "",
                "artist_id": None,
                "artist_name": "",
                "image": "",
                "id": None,
            },
        ),
        "tracks": defaultdict(
            lambda: {
                "minutes": 0,
                "plays": 0,
                "title": "",
                "artist": "",
                "album": "",
                "album_image": "",
                "album_id": None,
                "album_artist_id": None,
                "album_artist_name": "",
                "id": None,
            },
        ),
        "genres": defaultdict(lambda: {"minutes": 0, "plays": 0, "name": ""}),
        "decades": defaultdict(lambda: {"minutes": 0, "plays": 0, "label": ""}),
        "countries": defaultdict(lambda: {"minutes": 0, "plays": 0, "code": "", "name": ""}),
    }
    podcast_rollups = {
        "shows": defaultdict(lambda: {"minutes": 0, "plays": 0, "title": "", "show": "", "show_id": None, "podcast_uuid": None, "slug": "", "image": ""}),
        "episodes": defaultdict(lambda: {"title": "", "show": "", "show_id": None, "episode_id": None, "podcast_uuid": None, "slug": "", "image": "", "duration_seconds": 0}),
    }
    game_rollups = {}
    activity_counts = {}
    try:
        credit_backfill_hints = int(credit_backfill_hints or 0)
    except (TypeError, ValueError):
        credit_backfill_hints = 0
    non_play_activity_types = {
        MediaTypes.ANIME.value,
        MediaTypes.GAME.value,
        MediaTypes.BOARDGAME.value,
        MediaTypes.MANGA.value,
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
    }

    chunk_size = 50
    for offset in range(0, len(day_list), chunk_size):
        chunk = day_list[offset : offset + chunk_size]
        key_map = {day: _day_cache_key(user.id, day) for day in chunk}
        cached = cache.get_many(key_map.values())
        for day in chunk:
            cache_key = key_map[day]
            day_stats = cached.get(cache_key)
            if not day_stats and build_missing:
                day_stats = build_stats_for_day(user.id, day)
                if day_stats:
                    credit_backfill_hints += int(
                        day_stats.get("backfill", {}).get("missing_credits") or 0,
                    )
            if not day_stats:
                continue

            for media_type, items in day_stats.get("items", {}).items():
                for item_id_str, meta in items.items():
                    try:
                        item_id = int(item_id_str)
                    except (TypeError, ValueError):
                        continue
                    activity_dt = _parse_activity_dt(meta.get("activity_dt"))
                    score_dt = _parse_activity_dt(meta.get("score_dt"))
                    if score_dt is None and meta.get("score") is not None:
                        score_dt = activity_dt

                    existing = items_by_type[media_type].get(item_id)
                    if not existing:
                        items_by_type[media_type][item_id] = {
                            "item_id": item_id,
                            "media_id": meta.get("media_id"),
                            "media_type": meta.get("media_type"),
                            "status": meta.get("status"),
                            "score": meta.get("score"),
                            "score_dt": score_dt,
                            "activity_dt": activity_dt,
                        }
                        continue

                    existing_activity = existing.get("activity_dt")
                    if activity_dt and (not existing_activity or activity_dt > existing_activity):
                        existing["media_id"] = meta.get("media_id") or existing.get("media_id")
                        existing["status"] = meta.get("status")
                        existing["activity_dt"] = activity_dt

                    if meta.get("score") is not None:
                        existing_score_dt = existing.get("score_dt")
                        if existing_score_dt is None or (score_dt and score_dt > existing_score_dt):
                            existing["score"] = meta.get("score")
                            existing["score_dt"] = score_dt

                    items_by_type[media_type][item_id] = existing

            for media_type, items in day_stats.get("top_played", {}).items():
                for item_id_str, entry in items.items():
                    try:
                        item_id = int(item_id_str)
                    except (TypeError, ValueError):
                        continue
                    aggregate = top_played_by_type[media_type].get(item_id)
                    if not aggregate:
                        aggregate = {
                            "item_id": item_id,
                            "media_id": entry.get("media_id"),
                            "minutes": 0.0,
                            "plays": 0,
                            "episode_count": 0,
                            "activity_dt": None,
                        }
                        top_played_by_type[media_type][item_id] = aggregate
                    aggregate["minutes"] += entry.get("minutes") or 0
                    aggregate["plays"] += entry.get("plays") or 0
                    aggregate["episode_count"] += entry.get("episode_count") or 0
                    activity_dt = _parse_activity_dt(entry.get("activity_dt"))
                    if activity_dt and (aggregate["activity_dt"] is None or activity_dt > aggregate["activity_dt"]):
                        aggregate["activity_dt"] = activity_dt
                        if entry.get("media_id"):
                            aggregate["media_id"] = entry.get("media_id")

            for media_type, minutes in day_stats.get("totals", {}).get("minutes_by_type", {}).items():
                minutes_by_type[media_type] += minutes or 0
            for media_type, plays in day_stats.get("totals", {}).get("plays_by_type", {}).items():
                plays_by_type[media_type] += plays or 0
                day_play_counts[media_type][day.isoformat()] = plays

            for media_type, hours in day_stats.get("hour_counts", {}).items():
                for hour, count in hours.items():
                    hour_counts[media_type][hour] += count

            for media_type, minutes in day_stats.get("daily_minutes_by_type", {}).items():
                day_minutes_by_type[media_type][day.isoformat()] = minutes

            for genre, payload in day_stats.get("genres", {}).get("movie", {}).items():
                movie_genres[genre]["minutes"] += payload.get("minutes", 0)
                movie_genres[genre]["plays"] += payload.get("plays", 0)
                movie_genres[genre]["name"] = payload.get("name") or genre

            for genre, payload in day_stats.get("genres", {}).get("tv", {}).items():
                tv_genres[genre]["minutes"] += payload.get("minutes", 0)
                tv_genres[genre]["plays"] += payload.get("plays", 0)
                tv_genres[genre]["name"] = payload.get("name") or genre

            for genre, payload in day_stats.get("genres", {}).get("game", {}).items():
                game_genres[genre]["minutes"] += payload.get("minutes", 0)
                game_genres[genre]["plays"] += payload.get("plays", 0)
                game_genres[genre]["name"] = payload.get("name") or genre
                for game_id in payload.get("game_ids", []):
                    game_genres[genre]["game_ids"].add(str(game_id))

            for reading_type in (MediaTypes.BOOK.value, MediaTypes.COMIC.value, MediaTypes.MANGA.value):
                for genre, payload in day_stats.get("genres", {}).get(reading_type, {}).items():
                    reading_genres[reading_type][genre]["units"] += payload.get("units", 0)
                    reading_genres[reading_type][genre]["titles"] += payload.get("titles", 0)
                    reading_genres[reading_type][genre]["name"] = payload.get("name") or genre

            for key, value in day_stats.get("music", {}).items():
                for item_id_str, payload in value.items():
                    existing = music_rollups[key][item_id_str]
                    existing["minutes"] += payload.get("minutes", 0)
                    existing["plays"] += payload.get("plays", 0)
                    for field in (
                        "name",
                        "image",
                        "id",
                        "title",
                        "artist",
                        "artist_id",
                        "artist_name",
                        "album",
                        "album_image",
                        "album_id",
                        "album_artist_id",
                        "album_artist_name",
                        "label",
                        "code",
                    ):
                        if payload.get(field) and not existing.get(field):
                            existing[field] = payload.get(field)

            for key, value in day_stats.get("podcast", {}).items():
                for item_id_str, payload in value.items():
                    existing = podcast_rollups[key][item_id_str]
                    if key == "shows":
                        existing["minutes"] += payload.get("minutes", 0)
                        existing["plays"] += payload.get("plays", 0)
                        for field in ("title", "show", "show_id", "podcast_uuid", "slug", "image"):
                            if payload.get(field) and not existing.get(field):
                                existing[field] = payload.get(field)
                    else:
                        duration = payload.get("duration_seconds", 0) or 0
                        if duration > existing.get("duration_seconds", 0):
                            existing["duration_seconds"] = duration
                        for field in ("title", "show", "show_id", "episode_id", "podcast_uuid", "slug", "image"):
                            if payload.get(field) and not existing.get(field):
                                existing[field] = payload.get(field)

            for item_id_str, payload in day_stats.get("game", {}).get("by_game", {}).items():
                try:
                    item_id = int(item_id_str)
                except (TypeError, ValueError):
                    continue
                existing = game_rollups.get(item_id)
                if not existing:
                    existing = {"minutes_total": 0, "days": 0, "activity_dt": None, "media_id": payload.get("media_id")}
                    game_rollups[item_id] = existing
                existing["minutes_total"] += payload.get("minutes_total", 0)
                existing["days"] += payload.get("days", 0)
                activity_dt = _parse_activity_dt(payload.get("activity_dt"))
                if activity_dt and (existing["activity_dt"] is None or activity_dt > existing["activity_dt"]):
                    existing["activity_dt"] = activity_dt
                    if payload.get("media_id"):
                        existing["media_id"] = payload.get("media_id")

            daily_minutes = day_stats.get("daily_minutes_by_type", {}) or {}
            plays_total = sum(day_stats.get("totals", {}).get("plays_by_type", {}).values())
            activity_total = plays_total
            for media_type in non_play_activity_types:
                if daily_minutes.get(media_type, 0):
                    activity_total += 1
            if activity_total == 0 and sum(daily_minutes.values()) > 0:
                activity_total = 1
            activity_counts[day] = activity_total

    active_types = list(getattr(user, "get_active_media_types", lambda: [])())
    if not active_types:
        active_types = list(MediaTypes.values)

    if not user.season_enabled and MediaTypes.SEASON.value in active_types:
        active_types = [mt for mt in active_types if mt != MediaTypes.SEASON.value]
        items_by_type.pop(MediaTypes.SEASON.value, None)
        top_played_by_type.pop(MediaTypes.SEASON.value, None)

    if start_date is None and end_date is None:
        undated_models = [MediaTypes.MOVIE.value, MediaTypes.ANIME.value, MediaTypes.GAME.value, MediaTypes.BOARDGAME.value, MediaTypes.MUSIC.value, MediaTypes.PODCAST.value, MediaTypes.MANGA.value, MediaTypes.BOOK.value, MediaTypes.COMIC.value]
        for media_type in undated_models:
            if media_type not in active_types:
                continue
            model = apps.get_model("app", media_type)
            qs = model.objects.filter(
                user=user,
                start_date__isnull=True,
                end_date__isnull=True,
                status__in=[
                    Status.IN_PROGRESS.value,
                    Status.COMPLETED.value,
                    Status.DROPPED.value,
                    Status.PAUSED.value,
                ],
            ).values("id", "item_id", "status", "score", "created_at")
            for row in qs.iterator(chunk_size=500):
                activity_dt = _parse_activity_dt(row.get("created_at"))
                score = row.get("score")
                score_dt = activity_dt if score is not None else None
                existing = items_by_type[media_type].get(row["item_id"])
                if not existing:
                    items_by_type[media_type][row["item_id"]] = {
                        "item_id": row["item_id"],
                        "media_id": row["id"],
                        "media_type": media_type,
                        "status": row.get("status"),
                        "score": float(score) if score is not None else None,
                        "score_dt": score_dt,
                        "activity_dt": activity_dt,
                    }
                    continue

                existing_activity = existing.get("activity_dt")
                if activity_dt and (not existing_activity or activity_dt > existing_activity):
                    existing["media_id"] = row["id"] or existing.get("media_id")
                    existing["status"] = row.get("status")
                    existing["activity_dt"] = activity_dt

                if score is not None:
                    existing_score_dt = existing.get("score_dt")
                    if existing_score_dt is None or (score_dt and score_dt > existing_score_dt):
                        existing["score"] = float(score)
                        existing["score_dt"] = score_dt

                items_by_type[media_type][row["item_id"]] = existing

    media_count = {"total": 0}
    for media_type in active_types:
        count = len(items_by_type.get(media_type, {}))
        if count:
            media_count[media_type] = count
            media_count["total"] += count

    status_order = list(Status.values)
    status_distribution = {}
    total_completed = 0
    for media_type in active_types:
        items = items_by_type.get(media_type, {})
        if not items:
            continue
        status_counts = dict.fromkeys(status_order, 0)
        for meta in items.values():
            status_value = meta.get("status")
            if not status_value:
                continue
            status_counts[status_value] = status_counts.get(status_value, 0) + 1
            if status_value == Status.COMPLETED.value:
                total_completed += 1
        status_distribution[media_type] = status_counts

    status_distribution_payload = {
        "labels": [app_tags.media_type_readable(x) for x in status_distribution],
        "datasets": [
            {
                "label": status,
                "data": [status_distribution[media_type][status] for media_type in status_distribution],
                "background_color": stats.get_status_color(status),
                "total": sum(status_distribution[media_type][status] for media_type in status_distribution),
            }
            for status in status_order
        ],
        "total_completed": total_completed,
    }

    score_distribution = {}
    total_scored = 0
    total_score_sum = 0
    top_rated_heap = []
    top_rated_by_type = {}
    global_counter = itertools.count()
    score_scale_max = getattr(user, "rating_scale_max", 10)
    try:
        score_scale_max = int(score_scale_max)
    except (TypeError, ValueError):
        score_scale_max = 10
    if score_scale_max not in (5, 10):
        score_scale_max = 10
    score_range = range(score_scale_max + 1)

    for media_type in active_types:
        items = items_by_type.get(media_type, {})
        if not items:
            continue
        score_counts = dict.fromkeys(score_range, 0)
        type_heap = []
        type_counter = itertools.count()
        for meta in items.values():
            score = meta.get("score")
            if score is None:
                continue
            score_value = float(score)
            score_value_scaled = score_value / 2 if score_scale_max == 5 else score_value
            binned = int(score_value_scaled)
            if binned < 0:
                binned = 0
            if binned > score_scale_max:
                binned = score_scale_max
            score_counts[binned] += 1
            total_scored += 1
            total_score_sum += score_value_scaled
            media_id = meta.get("media_id")
            if media_id is None:
                continue
            if len(top_rated_heap) < 14:
                heapq.heappush(top_rated_heap, (score_value, next(global_counter), meta))
            else:
                heapq.heappushpop(top_rated_heap, (score_value, next(global_counter), meta))
            if len(type_heap) < 20:
                heapq.heappush(type_heap, (score_value, next(type_counter), meta))
            else:
                heapq.heappushpop(type_heap, (score_value, next(type_counter), meta))
        score_distribution[media_type] = score_counts
        top_rated_by_type[media_type] = [
            meta for _, _, meta in sorted(type_heap, key=lambda x: (-x[0], x[1]))
        ]

    average_score = round(total_score_sum / total_scored, 2) if total_scored > 0 else None
    top_rated_meta = [meta for _, _, meta in sorted(top_rated_heap, key=lambda x: (-x[0], x[1]))]

    media_refs = []
    for meta in top_rated_meta:
        media_refs.append((meta.get("media_type"), meta.get("media_id")))
    for metas in top_rated_by_type.values():
        for meta in metas:
            media_refs.append((meta.get("media_type"), meta.get("media_id")))

    media_map = _fetch_media_objects(set(media_refs))
    top_rated_media = [media_map.get((meta.get("media_type"), meta.get("media_id"))) for meta in top_rated_meta]
    top_rated_media = [media for media in top_rated_media if media]
    top_rated_media = stats._annotate_top_rated_media(top_rated_media)

    top_rated_by_type_media = {}
    for media_type, metas in top_rated_by_type.items():
        media_list = [
            media_map.get((meta.get("media_type"), meta.get("media_id")))
            for meta in metas
        ]
        media_list = [media for media in media_list if media]
        top_rated_by_type_media[media_type] = stats._annotate_top_rated_media(media_list)

    top_rated_score_map = {}
    for meta in top_rated_meta:
        media_type = meta.get("media_type")
        media_id = meta.get("media_id")
        score = meta.get("score")
        if media_type and media_id and score is not None:
            top_rated_score_map[(media_type, media_id)] = score
    for metas in top_rated_by_type.values():
        for meta in metas:
            media_type = meta.get("media_type")
            media_id = meta.get("media_id")
            score = meta.get("score")
            if media_type and media_id and score is not None:
                top_rated_score_map[(media_type, media_id)] = score

    def _apply_top_rated_scores(media_list):
        for media in media_list:
            score = top_rated_score_map.get((media._meta.model_name, media.id))
            if score is not None:
                media.aggregated_score = score

    _apply_top_rated_scores(top_rated_media)
    for media_list in top_rated_by_type_media.values():
        _apply_top_rated_scores(media_list)

    score_distribution_payload = {
        "labels": [str(score) for score in score_range],
        "datasets": [
            {
                "label": app_tags.media_type_readable(media_type),
                "data": [score_distribution[media_type][score] for score in score_range],
                "background_color": config.get_stats_color(media_type),
            }
            for media_type in score_distribution
        ],
        "average_score": average_score,
        "total_scored": total_scored,
        "scale_max": score_scale_max,
    }

    top_played = {}
    target_media_types = ["movie", "tv", "game", "boardgame", "anime", "music"]
    media_refs = []
    for media_type in target_media_types:
        entries = list(top_played_by_type.get(media_type, {}).values())
        entries = [entry for entry in entries if entry.get("minutes", 0) > 0]
        baseline_dt = datetime(1970, 1, 1, tzinfo=timezone.get_current_timezone())
        entries.sort(
            key=lambda entry: (entry.get("minutes", 0), entry.get("activity_dt") or baseline_dt),
            reverse=True,
        )
        limit = 20 if media_type == "game" else 10
        entries = entries[:limit]
        top_played[media_type] = entries
        for entry in entries:
            media_refs.append((media_type, entry.get("media_id")))

    media_map = _fetch_media_objects(set(media_refs))
    for media_type, entries in top_played.items():
        enriched = []
        for entry in entries:
            media = media_map.get((media_type, entry.get("media_id")))
            if not media:
                continue
            total_minutes = entry.get("minutes", 0)
            formatted_duration = helpers.minutes_to_hhmm(total_minutes)
            if media_type == "boardgame":
                formatted_duration = f"{int(total_minutes)} play{'s' if int(total_minutes) != 1 else ''}"
            enriched.append({
                "media": media,
                "total_time_minutes": total_minutes,
                "formatted_duration": formatted_duration,
                "episode_count": entry.get("episode_count", 0),
                "last_activity": entry.get("activity_dt"),
                "play_count": entry.get("plays", 0),
            })
        top_played[media_type] = enriched

    hours_per_media_type = {}
    for media_type, total_minutes in minutes_by_type.items():
        if media_type == MediaTypes.BOARDGAME.value:
            hours_per_media_type[media_type] = f"{int(total_minutes)} play{'s' if int(total_minutes) != 1 else ''}"
        else:
            hours_per_media_type[media_type] = stats._format_hours_minutes(total_minutes)

    if start_date is None and end_date is None and day_list:
        start_date = _day_boundary_datetime(day_list[0])
        end_date = _day_boundary_datetime(day_list[-1], end_of_day=True)

    activity_counts_by_date = {day: activity_counts.get(day, 0) for day in day_list}
    activity_data = _build_activity_data(
        activity_counts_by_date,
        day_minutes_by_type,
        day_list,
        start_date,
        end_date,
    )

    media_type_distribution = stats.get_media_type_distribution(
        media_count,
        minutes_by_type,
    )
    status_pie_chart_data = stats.get_status_pie_chart_data(status_distribution_payload)

    daily_hours_by_media_type = _build_daily_hours_chart(day_minutes_by_type, day_list)

    movie_chart = _build_media_charts_from_counts(
        day_play_counts.get(MediaTypes.MOVIE.value, {}),
        hour_counts.get(MediaTypes.MOVIE.value, {}),
        config.get_stats_color(MediaTypes.MOVIE.value),
        "Movie Plays",
    )
    tv_chart = _build_media_charts_from_counts(
        day_play_counts.get(MediaTypes.TV.value, {}),
        hour_counts.get(MediaTypes.TV.value, {}),
        config.get_stats_color(MediaTypes.TV.value),
        "Episode Plays",
    )
    music_chart = _build_media_charts_from_counts(
        day_play_counts.get(MediaTypes.MUSIC.value, {}),
        hour_counts.get(MediaTypes.MUSIC.value, {}),
        config.get_stats_color(MediaTypes.MUSIC.value),
        "Music Plays",
    )
    podcast_chart = _build_media_charts_from_counts(
        day_play_counts.get(MediaTypes.PODCAST.value, {}),
        hour_counts.get(MediaTypes.PODCAST.value, {}),
        config.get_stats_color(MediaTypes.PODCAST.value),
        "Podcast Plays",
    )

    tv_total_minutes = minutes_by_type.get(MediaTypes.TV.value, 0)
    movie_total_minutes = minutes_by_type.get(MediaTypes.MOVIE.value, 0)
    music_total_minutes = minutes_by_type.get(MediaTypes.MUSIC.value, 0)
    podcast_total_minutes = minutes_by_type.get(MediaTypes.PODCAST.value, 0)
    game_total_minutes = minutes_by_type.get(MediaTypes.GAME.value, 0)

    tv_total_hours = tv_total_minutes / 60 if tv_total_minutes else 0
    movie_total_hours = movie_total_minutes / 60 if movie_total_minutes else 0
    game_total_hours = game_total_minutes / 60 if game_total_minutes else 0

    tv_consumption = {
        "hours": _compute_metric_breakdown_for_range(tv_total_hours, start_date, end_date),
        "plays": _compute_metric_breakdown_for_range(plays_by_type.get(MediaTypes.TV.value, 0), start_date, end_date),
        "charts": tv_chart,
        "has_data": plays_by_type.get(MediaTypes.TV.value, 0) > 0,
        "top_genres": [
            {**item, "formatted_duration": helpers.minutes_to_hhmm(item["minutes"])}
            for item in sorted(tv_genres.values(), key=lambda x: (x["minutes"], x["plays"]), reverse=True)[:20]
        ],
    }

    movie_consumption = {
        "hours": _compute_metric_breakdown_for_range(movie_total_hours, start_date, end_date),
        "plays": _compute_metric_breakdown_for_range(plays_by_type.get(MediaTypes.MOVIE.value, 0), start_date, end_date),
        "charts": movie_chart,
        "has_data": plays_by_type.get(MediaTypes.MOVIE.value, 0) > 0,
        "top_genres": [
            {**item, "formatted_duration": helpers.minutes_to_hhmm(item["minutes"])}
            for item in sorted(movie_genres.values(), key=lambda x: (x["minutes"], x["plays"]), reverse=True)[:20]
        ],
    }

    def _top_items(values, key_fields=("minutes", "plays"), limit=20):
        items = sorted(values, key=lambda x: tuple(x.get(field, 0) for field in key_fields), reverse=True)[:limit]
        for item in items:
            if "minutes" in item:
                item["formatted_duration"] = helpers.minutes_to_hhmm(item["minutes"])
        return items

    music_consumption = {
        "minutes": _compute_metric_breakdown_for_range(music_total_minutes, start_date, end_date),
        "plays": _compute_metric_breakdown_for_range(plays_by_type.get(MediaTypes.MUSIC.value, 0), start_date, end_date),
        "charts": music_chart,
        "has_data": plays_by_type.get(MediaTypes.MUSIC.value, 0) > 0,
        "top_artists": _top_items(list(music_rollups["artists"].values()), ("minutes", "plays")),
        "top_albums": _top_items(list(music_rollups["albums"].values()), ("minutes", "plays")),
        "top_tracks": _top_items(list(music_rollups["tracks"].values()), ("minutes", "plays")),
        "top_genres": _top_items(list(music_rollups["genres"].values()), ("minutes", "plays")),
        "top_decades": _top_items(list(music_rollups["decades"].values()), ("minutes", "plays")),
        "top_countries": _top_items(list(music_rollups["countries"].values()), ("minutes", "plays")),
    }

    podcast_consumption = {
        "minutes": _compute_metric_breakdown_for_range(podcast_total_minutes, start_date, end_date),
        "plays": _compute_metric_breakdown_for_range(plays_by_type.get(MediaTypes.PODCAST.value, 0), start_date, end_date),
        "charts": podcast_chart,
        "has_data": plays_by_type.get(MediaTypes.PODCAST.value, 0) > 0,
    }

    most_played = sorted(
        podcast_rollups["shows"].values(),
        key=lambda x: (x["plays"], x["minutes"]),
        reverse=True,
    )[:20]
    most_listened = sorted(
        podcast_rollups["shows"].values(),
        key=lambda x: (x["minutes"], x["plays"]),
        reverse=True,
    )[:20]
    longest_episodes = sorted(
        [ep for ep in podcast_rollups["episodes"].values() if ep.get("duration_seconds", 0) > 0],
        key=lambda x: x["duration_seconds"],
        reverse=True,
    )[:20]
    for item in most_played + most_listened:
        item["formatted_duration"] = helpers.minutes_to_hhmm(item["minutes"])
    for item in longest_episodes:
        hours = item["duration_seconds"] // 3600
        minutes = (item["duration_seconds"] % 3600) // 60
        item["formatted_duration"] = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    podcast_consumption.update({
        "most_played": most_played,
        "most_listened": most_listened,
        "longest_episodes": longest_episodes,
    })

    game_hours_by_year = defaultdict(float)
    game_hours_by_month = defaultdict(float)
    game_day_minutes = day_minutes_by_type.get(MediaTypes.GAME.value, {})
    for day_str, minutes in game_day_minutes.items():
        try:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        game_hours_by_year[day.year] += minutes / 60
        game_hours_by_month[day.month] += minutes / 60

    year_labels = [str(year) for year in sorted(game_hours_by_year)]
    year_values = [game_hours_by_year[year] for year in sorted(game_hours_by_year)]
    month_labels = [calendar.month_abbr[i] for i in range(1, 13)]
    month_values = [game_hours_by_month.get(i, 0) for i in range(1, 13)]
    game_hours_charts = {
        "by_year": stats._build_single_series_chart(year_labels, year_values, config.get_stats_color(MediaTypes.GAME.value), "Game Hours"),
        "by_month": stats._build_single_series_chart(month_labels, month_values, config.get_stats_color(MediaTypes.GAME.value), "Game Hours"),
    }

    game_data = []
    for item_id_str, payload in game_rollups.items():
        days = payload.get("days") or 0
        minutes_total = payload.get("minutes_total") or 0
        if days <= 0 or minutes_total <= 0:
            continue
        daily_avg_hours = (minutes_total / days) / 60
        game_data.append({
            "item_id": item_id_str,
            "media_id": payload.get("media_id"),
            "daily_average": daily_avg_hours,
            "hours": minutes_total / 60,
            "activity_dt": payload.get("activity_dt"),
        })

    # --- Collect item_ids needed for chart tooltip and platform breakdown ---
    all_item_ids = [item["item_id"] for item in game_data if item.get("item_id") is not None]

    # Determine which item_ids are needed for the top-5-per-band tooltip
    bands = stats.DAILY_AVERAGE_BANDS
    band_game_groups = {label: [] for _, _, label in bands}
    for item in game_data:
        idx = stats._get_daily_average_band_index(item["daily_average"])
        if 0 <= idx < len(bands):
            band_game_groups[bands[idx][2]].append(item)
    tooltip_item_ids = set()
    for label, items in band_game_groups.items():
        for g in sorted(items, key=lambda x: x["daily_average"], reverse=True)[:5]:
            if g.get("item_id") is not None:
                tooltip_item_ids.add(g["item_id"])

    # Fetch Item metadata (platforms for breakdown + title/image for tooltip)
    item_info_map = {}
    if all_item_ids:
        item_info_map = {
            row["id"]: row
            for row in Item.objects.filter(id__in=all_item_ids).values("id", "title", "image", "platforms")
        }

    # Fetch CollectionEntry resolution for platform breakdown
    CollectionEntry = apps.get_model("app", "CollectionEntry")
    collection_platform_map = {}
    if all_item_ids:
        for ce in CollectionEntry.objects.filter(
            user=user, item_id__in=all_item_ids
        ).values("item_id", "resolution"):
            resolution = (ce.get("resolution") or "").strip()
            if resolution:
                collection_platform_map[ce["item_id"]] = resolution

    # Enrich game_data with title/image for the chart tooltip
    enriched_game_data = []
    for item in game_data:
        info = item_info_map.get(item.get("item_id"), {})
        enriched_game_data.append({
            **item,
            "title": info.get("title", ""),
            "image": info.get("image", ""),
        })

    daily_avg_chart = stats._build_daily_average_distribution_chart(
        enriched_game_data,
        config.get_stats_color(MediaTypes.GAME.value),
        "Games",
    )

    game_genre_items = []
    for genre, payload in game_genres.items():
        game_genre_items.append({
            "minutes": payload["minutes"],
            "games": len(payload["game_ids"]),
            "plays": len(payload["game_ids"]),
            "name": genre,
            "formatted_duration": helpers.minutes_to_hhmm(payload["minutes"]),
        })
    game_genre_items = sorted(game_genre_items, key=lambda x: (x["minutes"], x["games"]), reverse=True)[:20]

    top_daily_avg_games = sorted(game_data, key=lambda x: x["daily_average"], reverse=True)[:20]
    game_media_map = _fetch_media_objects({(MediaTypes.GAME.value, item["media_id"]) for item in top_daily_avg_games})
    top_daily_avg_payload = []
    for item in top_daily_avg_games:
        media = game_media_map.get((MediaTypes.GAME.value, item["media_id"]))
        if not media:
            continue
        daily_avg_minutes = item["daily_average"] * 60
        top_daily_avg_payload.append({
            "game": media,
            "daily_average_hours": item["daily_average"],
            "daily_average_minutes": daily_avg_minutes,
            "formatted_daily_average": helpers.minutes_to_hhmm(daily_avg_minutes) + "/day",
            "total_hours": item["hours"],
            "formatted_total": helpers.minutes_to_hhmm(item["hours"] * 60),
        })

    # --- Platform breakdown ---
    platform_hours = defaultdict(float)
    platform_game_ids = defaultdict(set)
    for item in game_data:
        item_id = item.get("item_id")
        if item_id is None:
            continue
        hours = item["hours"]
        # Priority 1: collection entry resolution
        platform = collection_platform_map.get(item_id)
        # Priority 2: single IGDB platform from Item.platforms
        if not platform:
            raw_platforms = item_info_map.get(item_id, {}).get("platforms") or []
            if isinstance(raw_platforms, list):
                cleaned = [str(p).strip() for p in raw_platforms if str(p).strip()]
            else:
                cleaned = []
            if len(cleaned) == 1:
                platform = cleaned[0]
        if not platform:
            continue
        platform_hours[platform] += hours
        platform_game_ids[platform].add(item_id)

    platform_breakdown = sorted(
        [
            {
                "name": name,
                "games": len(platform_game_ids[name]),
                "hours": hours,
                "formatted_hours": helpers.minutes_to_hhmm(hours * 60),
            }
            for name, hours in platform_hours.items()
        ],
        key=lambda x: x["hours"],
        reverse=True,
    )

    game_consumption = {
        "hours": _compute_metric_breakdown_for_range(game_total_hours, start_date, end_date),
        "charts": {
            "by_year": game_hours_charts["by_year"],
            "by_month": game_hours_charts["by_month"],
            "by_daily_average": daily_avg_chart,
        },
        "has_data": bool(game_data) or game_total_hours > 0,
        "top_genres": game_genre_items,
        "top_daily_average_games": top_daily_avg_payload,
        "platform_breakdown": platform_breakdown,
    }

    def _build_cached_reading_consumption(media_type):
        unit_name = config.get_unit(media_type, short=False) or "Unit"
        chart_label = f"{unit_name}s Read"
        completion_label_map = {
            MediaTypes.BOOK.value: "Books Finished",
            MediaTypes.COMIC.value: "Comics Finished",
            MediaTypes.MANGA.value: "Manga Finished",
        }
        completion_label = completion_label_map.get(media_type, "Items Finished")
        release_label_map = {
            MediaTypes.BOOK.value: "Books Released",
            MediaTypes.COMIC.value: "Comics Released",
            MediaTypes.MANGA.value: "Manga Released",
        }
        release_label = release_label_map.get(media_type, "Items Released")
        color = config.get_stats_color(media_type)
        units_by_day = day_minutes_by_type.get(media_type, {})
        unit_total = sum(units_by_day.values()) if units_by_day else 0

        completion_total = int(round((minutes_by_type.get(media_type, 0) or 0) / 60))
        completion_by_day = {}
        for day_str, day_minutes in units_by_day.items():
            if day_minutes and day_minutes > 0:
                completion_by_day[day_str] = 1

        charts = _build_media_charts_from_counts(
            units_by_day,
            hour_counts.get(media_type, {}),
            color,
            chart_label,
        )
        completion_charts = _build_media_charts_from_counts(
            completion_by_day,
            hour_counts.get(media_type, {}),
            color,
            completion_label,
        )

        release_datetimes = []
        item_ids = [meta.get("item_id") for meta in items_by_type.get(media_type, {}).values() if meta.get("item_id")]
        items_with_authors = stats._fetch_reading_items_with_authors(item_ids)
        if items_with_authors:
            release_datetimes = [
                item.release_datetime
                for item in items_with_authors.values()
                if item.release_datetime
            ]
        completed_lengths = []
        model = apps.get_model("app", media_type)
        completed_queryset = model.objects.filter(user=user, status=Status.COMPLETED.value).select_related("item")
        for entry in completed_queryset.iterator(chunk_size=500):
            if not stats._reading_entry_in_range(entry, start_date, end_date):
                continue
            completed_length = entry.progress or getattr(entry.item, "number_of_pages", 0) or 0
            if completed_length > 0:
                completed_lengths.append(completed_length)

        release_chart = stats._build_release_year_chart(release_datetimes, color, release_label)
        completed_length_chart = stats._build_completed_length_distribution_chart(completed_lengths, unit_name, color)

        top_entries = list(top_played_by_type.get(media_type, {}).values())
        baseline_dt = datetime(1970, 1, 1, tzinfo=timezone.get_current_timezone())
        top_entries.sort(
            key=lambda entry: (entry.get("minutes", 0), entry.get("activity_dt") or baseline_dt),
            reverse=True,
        )
        top_authors = stats._build_reading_top_authors(
            [
                (
                    items_with_authors.get(entry.get("item_id")),
                    entry.get("minutes", 0),
                )
                for entry in top_entries
                if entry.get("minutes", 0) > 0
            ],
            unit_name,
            limit=20,
        )
        top_entries = top_entries[:20]
        top_media_refs = {(media_type, entry.get("media_id")) for entry in top_entries if entry.get("media_id")}
        top_media_map = _fetch_media_objects(top_media_refs)
        top_items = []
        for entry in top_entries:
            media = top_media_map.get((media_type, entry.get("media_id")))
            if not media:
                continue
            units = entry.get("minutes", 0)
            top_items.append(
                {
                    "media": media,
                    "units": units,
                    "entry_count": entry.get("plays", 0),
                    "formatted_units": f"{int(round(units))} {unit_name.lower()}{'' if int(round(units)) == 1 else 's'}",
                }
            )

        genre_items = []
        for payload in reading_genres.get(media_type, {}).values():
            units = payload.get("units", 0)
            if units <= 0:
                continue
            titles = payload.get("titles", 0)
            genre_items.append(
                {
                    "name": payload.get("name") or "",
                    "units": units,
                    "titles": titles,
                    "formatted_units": f"{int(round(units))} {unit_name.lower()}{'' if int(round(units)) == 1 else 's'}",
                }
            )
        genre_items = sorted(genre_items, key=lambda item: (item["units"], item["titles"]), reverse=True)[:20]

        item_lengths = []
        scored_values = []
        longest_item = None
        shortest_item = None
        for entry in top_items:
            media = entry["media"]
            pages = getattr(getattr(media, "item", None), "number_of_pages", None)
            if pages and pages > 0:
                item_lengths.append(pages)
                if longest_item is None or pages > longest_item["value"]:
                    longest_item = {"media": media, "value": pages}
                if shortest_item is None or pages < shortest_item["value"]:
                    shortest_item = {"media": media, "value": pages}
            score_value = getattr(media, "aggregated_score", None)
            if score_value is None:
                score_value = getattr(media, "score", None)
            if score_value is not None:
                scored_values.append(float(score_value))

        average_length = round(sum(item_lengths) / len(item_lengths), 1) if item_lengths else 0
        average_rating = round(sum(scored_values) / len(scored_values), 2) if scored_values else None

        return {
            "units": _compute_metric_breakdown_for_range(unit_total, start_date, end_date),
            "completions": _compute_metric_breakdown_for_range(completion_total, start_date, end_date),
            "charts": charts,
            "completion_charts": {
                "by_year": completion_charts["by_year"],
                "by_month": completion_charts["by_month"],
            },
            "completed_length_chart": completed_length_chart,
            "release_chart": release_chart,
            "has_data": unit_total > 0 or completion_total > 0,
            "unit_name": unit_name,
            "unit_label": chart_label,
            "completion_label": completion_label,
            "top_items": top_items,
            "top_authors": top_authors,
            "top_genres": genre_items,
            "highlights": {
                "longest_item": longest_item,
                "shortest_item": shortest_item,
                "average_length": average_length,
                "average_rating": average_rating,
            },
        }

    book_consumption = _build_cached_reading_consumption(MediaTypes.BOOK.value)
    comic_consumption = _build_cached_reading_consumption(MediaTypes.COMIC.value)
    manga_consumption = _build_cached_reading_consumption(MediaTypes.MANGA.value)

    first_play = None
    last_play = None
    if day_list:
        first_day_payload = history_cache.build_history_day(user, day_list[0])
        last_day_payload = history_cache.build_history_day(user, day_list[-1])
        first_play = _select_history_entry_for_day(first_day_payload, pick_earliest=True)
        last_play = _select_history_entry_for_day(last_day_payload, pick_latest=True)

    today_in_user_history, today_in_user_history_year = _get_today_history_entries(user)
    today_in_history, today_in_history_year = _get_today_release_entry(user)
    today = timezone.localdate()
    history_highlights = {
        "first_play": first_play,
        "last_play": last_play,
        "today_in_history": today_in_history,
        "today_in_history_year": today_in_history_year,
        "today_in_user_history": today_in_user_history,
        "today_in_user_history_year": today_in_user_history_year,
        "today_month": today.month,
        "today_day": today.day,
    }
    top_talent = _aggregate_top_talent(
        user,
        start_date,
        end_date,
        schedule_missing_backfill=credit_backfill_hints <= 0,
    )

    return {
        "media_count": media_count,
        "activity_data": activity_data,
        "media_type_distribution": media_type_distribution,
        "score_distribution": score_distribution_payload,
        "top_rated": top_rated_media,
        "top_rated_by_type": top_rated_by_type_media,
        "top_played": top_played,
        "top_talent": top_talent,
        "status_distribution": status_distribution_payload,
        "status_pie_chart_data": status_pie_chart_data,
        "minutes_per_media_type": dict(minutes_by_type),
        "hours_per_media_type": hours_per_media_type,
        "tv_consumption": tv_consumption,
        "movie_consumption": movie_consumption,
        "music_consumption": music_consumption,
        "podcast_consumption": podcast_consumption,
        "game_consumption": game_consumption,
        "book_consumption": book_consumption,
        "comic_consumption": comic_consumption,
        "manga_consumption": manga_consumption,
        "daily_hours_by_media_type": daily_hours_by_media_type,
        "history_highlights": history_highlights,
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
    refresh_lock = cache.get(_refresh_lock_key(user.id, range_name))
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

        refresh_statistics_cache_task.apply_async(args=[user_id, range_name], countdown=countdown)
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


def schedule_all_ranges_refresh(user_id: int, debounce_seconds: int = 30, countdown: int = 3):
    """Schedule background refresh for all predefined ranges for a user.
    
    This is useful when media changes and we want to refresh all ranges.
    Optimizes by calculating "All Time" first to pre-populate runtime data,
    then prioritizes the user's preferred range before scheduling the rest in parallel.
    
    Args:
        user_id: User ID
        debounce_seconds: Seconds to debounce refresh requests
        countdown: Seconds to delay task execution (default 3)
    """
    # Use a single lock for all ranges to prevent thundering herd
    all_ranges_lock_key = f"{STATISTICS_REFRESH_LOCK_PREFIX}_all_{user_id}"
    if debounce_seconds and not cache.add(all_ranges_lock_key, True, debounce_seconds):
        # Already scheduled recently, skip
        return

    # Schedule "All Time" with same countdown as history refresh (countdown=3)
    # This ensures history and "All Time" run together, then other ranges follow
    all_time_range = "All Time"
    user_model = get_user_model()
    preferred_range = (
        user_model.objects.filter(id=user_id)
        .values_list("statistics_default_range", flat=True)
        .first()
    )
    if preferred_range not in PREDEFINED_RANGES:
        preferred_range = "Last 12 Months"
    if preferred_range == all_time_range:
        preferred_range = None

    logger.debug(
        "Scheduling statistics refreshes for user %s (all_time=%s preferred=%s)",
        user_id,
        all_time_range,
        preferred_range,
    )
    schedule_statistics_refresh(user_id, all_time_range, debounce_seconds=debounce_seconds, countdown=countdown)
    if preferred_range:
        schedule_statistics_refresh(user_id, preferred_range, debounce_seconds=debounce_seconds, countdown=countdown + 1)

    # Schedule remaining ranges in parallel with longer countdown
    for range_name in PREDEFINED_RANGES:
        if range_name in (all_time_range, preferred_range):
            continue
        schedule_statistics_refresh(user_id, range_name, debounce_seconds=debounce_seconds, countdown=countdown + 2)
