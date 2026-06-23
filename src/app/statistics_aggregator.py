"""Range-level statistics aggregator — extracted from statistics_cache.py."""

import calendar
import heapq
import itertools
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from django.apps import apps
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from app import config, helpers
from app import statistics as stats
from app.models import Item, MediaTypes, Status
from app.templatetags import app_tags
from app.statistics_talent import (
    STATISTICS_TOP_N,
    STATISTICS_TOP_RATED_OVERALL,
    _aggregate_top_talent,
)
from app.statistics_highlights import (
    _get_history_day_payload,
    _get_range_history_boundary_days,
    _get_today_history_entries,
    _get_today_release_entry,
    _select_history_entry_for_day,
)
from app.statistics_day_builder import (
    _day_boundary_datetime,
    _day_cache_key,
    _normalize_day_value,
    build_stats_for_day,
)

logger = logging.getLogger(__name__)

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


def _build_activity_data(date_counts, day_minutes_by_type, day_list, start_date, end_date, *, week_start_sunday=False):
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

    start_date_aligned = stats.get_aligned_week_start(start_date, week_start_sunday=week_start_sunday)
    if start_date_aligned is None:
        return {
            "calendar_weeks": [],
            "months": [],
            "weekday_labels": [],
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

    base_weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekday_labels = [base_weekdays[6], *base_weekdays[:6]] if week_start_sunday else base_weekdays
    week_start_weekday = 6 if week_start_sunday else 0

    months = []
    mondays_per_month = []
    current_month = date_range[0].strftime("%b") if date_range else None
    monday_count = 0

    for current_date in date_range:
        if current_date.weekday() == week_start_weekday:
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
        "weekday_labels": weekday_labels,
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
    by_type = defaultdict(set)
    for media_type, media_id in media_refs:
        if media_type and media_id:
            by_type[media_type].add(media_id)

    for media_type, media_ids in by_type.items():
        model = apps.get_model("app", media_type)
        queryset = model.objects.filter(id__in=media_ids).select_related("item")
        if media_type == MediaTypes.SEASON.value:
            queryset = queryset.select_related("item", "related_tv__item")
        elif media_type == MediaTypes.EPISODE.value:
            queryset = queryset.select_related(
                "item",
                "related_season__item",
                "related_season__related_tv__item",
            )
        found_ids = set()
        for media in queryset:
            media_objects[(media_type, media.id)] = media
            found_ids.add(media.id)

        # Grouped anime (TV shows with library_media_type='anime') are stored in the TV
        # table, so any anime IDs not found in the Anime model belong to TV instances.
        if media_type == MediaTypes.ANIME.value:
            missing_ids = media_ids - found_ids
            if missing_ids:
                TV = apps.get_model("app", MediaTypes.TV.value)
                for tv in TV.objects.filter(id__in=missing_ids).select_related("item"):
                    media_objects[(media_type, tv.id)] = tv

    return media_objects


def _aggregate_minutes_per_media_type_from_days(user, day_list, *, build_missing=False):
    minutes_by_type = defaultdict(float)
    if not day_list:
        return {}

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
            if not day_stats:
                continue

            for media_type, minutes in day_stats.get("totals", {}).get("minutes_by_type", {}).items():
                minutes_by_type[media_type] += minutes or 0

    return dict(minutes_by_type)


def _empty_top_talent_payload(sort_by="plays"):
    empty_bucket = {
        "top_actors": [],
        "top_actresses": [],
        "top_directors": [],
        "top_writers": [],
        "top_studios": [],
    }
    by_sort = {
        "plays": dict(empty_bucket),
        "time": dict(empty_bucket),
        "titles": dict(empty_bucket),
    }
    return {
        "sort_by": sort_by,
        "by_sort": by_sort,
        **by_sort.get(sort_by, dict(empty_bucket)),
    }


def _empty_reading_consumption(unit_name="Unit", completion_label="Items Finished"):
    return {
        "units": {"total": 0, "per_year": 0, "per_month": 0, "per_day": 0},
        "completions": {"total": 0, "per_year": 0, "per_month": 0, "per_day": 0},
        "charts": {
            "by_year": {"labels": [], "datasets": []},
            "by_month": {"labels": [], "datasets": []},
            "by_weekday": {"labels": [], "datasets": []},
            "by_time_of_day": {"labels": [], "datasets": []},
        },
        "completion_charts": {
            "by_year": {"labels": [], "datasets": []},
            "by_month": {"labels": [], "datasets": []},
        },
        "completed_length_chart": {"labels": [], "datasets": []},
        "release_chart": {"labels": [], "datasets": []},
        "has_data": False,
        "unit_name": unit_name,
        "unit_label": f"{unit_name}s Read",
        "completion_label": completion_label,
        "top_items": [],
        "top_authors": [],
        "top_genres": [],
        "highlights": {
            "longest_item": None,
            "shortest_item": None,
            "average_length": 0,
            "average_rating": None,
        },
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
    anime_genres = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": ""})
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

            for genre, payload in day_stats.get("genres", {}).get("anime", {}).items():
                anime_genres[genre]["minutes"] += payload.get("minutes", 0)
                anime_genres[genre]["plays"] += payload.get("plays", 0)
                anime_genres[genre]["name"] = payload.get("name") or genre

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
            if len(top_rated_heap) < STATISTICS_TOP_RATED_OVERALL:
                heapq.heappush(top_rated_heap, (score_value, next(global_counter), meta))
            else:
                heapq.heappushpop(top_rated_heap, (score_value, next(global_counter), meta))
            if len(type_heap) < STATISTICS_TOP_N:
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
        limit = STATISTICS_TOP_N
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
            hours_per_media_type[media_type] = stats._format_hours_minutes(total_minutes, user.duration_format)

    if start_date is None and end_date is None and day_list:
        start_date = _day_boundary_datetime(day_list[0])
        end_date = _day_boundary_datetime(day_list[-1], end_of_day=True)

    activity_counts_by_date = {day: activity_counts.get(day, 0) for day in day_list}
    from users.models import WeekStartDayChoices  # noqa: PLC0415 - avoid circular import
    week_start_sunday = user.week_start_day == WeekStartDayChoices.SUNDAY
    activity_data = _build_activity_data(
        activity_counts_by_date,
        day_minutes_by_type,
        day_list,
        start_date,
        end_date,
        week_start_sunday=week_start_sunday,
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
    anime_chart = _build_media_charts_from_counts(
        day_play_counts.get(MediaTypes.ANIME.value, {}),
        hour_counts.get(MediaTypes.ANIME.value, {}),
        config.get_stats_color(MediaTypes.ANIME.value),
        "Anime Plays",
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
    anime_total_minutes = minutes_by_type.get(MediaTypes.ANIME.value, 0)
    movie_total_minutes = minutes_by_type.get(MediaTypes.MOVIE.value, 0)
    music_total_minutes = minutes_by_type.get(MediaTypes.MUSIC.value, 0)
    podcast_total_minutes = minutes_by_type.get(MediaTypes.PODCAST.value, 0)
    game_total_minutes = minutes_by_type.get(MediaTypes.GAME.value, 0)

    tv_total_hours = tv_total_minutes / 60 if tv_total_minutes else 0
    anime_total_hours = anime_total_minutes / 60 if anime_total_minutes else 0
    movie_total_hours = movie_total_minutes / 60 if movie_total_minutes else 0
    game_total_hours = game_total_minutes / 60 if game_total_minutes else 0

    tv_consumption = {
        "hours": _compute_metric_breakdown_for_range(tv_total_hours, start_date, end_date),
        "plays": _compute_metric_breakdown_for_range(plays_by_type.get(MediaTypes.TV.value, 0), start_date, end_date),
        "charts": tv_chart,
        "has_data": plays_by_type.get(MediaTypes.TV.value, 0) > 0,
        "top_genres": [
            {**item, "formatted_duration": helpers.minutes_to_hhmm(item["minutes"])}
            for item in sorted(tv_genres.values(), key=lambda x: (x["minutes"], x["plays"]), reverse=True)[:STATISTICS_TOP_N]
        ],
    }

    movie_consumption = {
        "hours": _compute_metric_breakdown_for_range(movie_total_hours, start_date, end_date),
        "plays": _compute_metric_breakdown_for_range(plays_by_type.get(MediaTypes.MOVIE.value, 0), start_date, end_date),
        "charts": movie_chart,
        "has_data": plays_by_type.get(MediaTypes.MOVIE.value, 0) > 0,
        "top_genres": [
            {**item, "formatted_duration": helpers.minutes_to_hhmm(item["minutes"])}
            for item in sorted(movie_genres.values(), key=lambda x: (x["minutes"], x["plays"]), reverse=True)[:STATISTICS_TOP_N]
        ],
    }

    anime_consumption = {
        "hours": _compute_metric_breakdown_for_range(anime_total_hours, start_date, end_date),
        "plays": _compute_metric_breakdown_for_range(plays_by_type.get(MediaTypes.ANIME.value, 0), start_date, end_date),
        "charts": anime_chart,
        "has_data": plays_by_type.get(MediaTypes.ANIME.value, 0) > 0,
        "top_genres": [
            {**item, "formatted_duration": helpers.minutes_to_hhmm(item["minutes"])}
            for item in sorted(anime_genres.values(), key=lambda x: (x["minutes"], x["plays"]), reverse=True)[:STATISTICS_TOP_N]
        ],
    }

    def _top_items(values, key_fields=("minutes", "plays"), limit=STATISTICS_TOP_N):
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
    )[:STATISTICS_TOP_N]
    most_listened = sorted(
        podcast_rollups["shows"].values(),
        key=lambda x: (x["minutes"], x["plays"]),
        reverse=True,
    )[:STATISTICS_TOP_N]
    longest_episodes = sorted(
        [ep for ep in podcast_rollups["episodes"].values() if ep.get("duration_seconds", 0) > 0],
        key=lambda x: x["duration_seconds"],
        reverse=True,
    )[:STATISTICS_TOP_N]
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
    game_genre_items = sorted(game_genre_items, key=lambda x: (x["minutes"], x["games"]), reverse=True)[:STATISTICS_TOP_N]

    top_daily_avg_games = sorted(game_data, key=lambda x: x["daily_average"], reverse=True)[:STATISTICS_TOP_N]
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
        item_ids = [
            meta.get("item_id")
            for meta in items_by_type.get(media_type, {}).values()
            if meta.get("item_id")
        ]
        top_entries = list(top_played_by_type.get(media_type, {}).values())
        has_any_reading_data = bool(unit_total or completion_total or item_ids or top_entries)
        if not has_any_reading_data:
            return _empty_reading_consumption(
                unit_name=unit_name,
                completion_label=completion_label,
            )

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
            limit=STATISTICS_TOP_N,
        )
        top_entries = top_entries[:STATISTICS_TOP_N]
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
        genre_items = sorted(genre_items, key=lambda item: (item["units"], item["titles"]), reverse=True)[:STATISTICS_TOP_N]

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

    reading_media_types = {
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
        MediaTypes.MANGA.value,
    }
    reading_types_with_data = {
        media_type
        for media_type in reading_media_types
        if media_type in active_types and (
            minutes_by_type.get(media_type)
            or items_by_type.get(media_type)
            or top_played_by_type.get(media_type)
        )
    }
    book_consumption = (
        _build_cached_reading_consumption(MediaTypes.BOOK.value)
        if MediaTypes.BOOK.value in reading_types_with_data
        else _empty_reading_consumption("Page", "Books Finished")
    )
    comic_consumption = (
        _build_cached_reading_consumption(MediaTypes.COMIC.value)
        if MediaTypes.COMIC.value in reading_types_with_data
        else _empty_reading_consumption("Page", "Comics Finished")
    )
    manga_consumption = (
        _build_cached_reading_consumption(MediaTypes.MANGA.value)
        if MediaTypes.MANGA.value in reading_types_with_data
        else _empty_reading_consumption("Page", "Manga Finished")
    )

    first_play = None
    last_play = None
    first_highlight_day, last_highlight_day = _get_range_history_boundary_days(
        user,
        start_date,
        end_date,
    )
    if first_highlight_day and last_highlight_day:
        first_day_payload = _get_history_day_payload(user, first_highlight_day)
        last_day_payload = _get_history_day_payload(user, last_highlight_day)
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
    has_movie_tv_activity = bool(
        plays_by_type.get(MediaTypes.MOVIE.value, 0)
        or plays_by_type.get(MediaTypes.TV.value, 0)
    )
    top_talent = (
        _aggregate_top_talent(
            user,
            start_date,
            end_date,
            schedule_missing_backfill=credit_backfill_hints <= 0,
        )
        if has_movie_tv_activity
        else _empty_top_talent_payload(
            sort_by=getattr(user, "top_talent_sort_by", "plays"),
        )
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
        "anime_consumption": anime_consumption,
        "music_consumption": music_consumption,
        "podcast_consumption": podcast_consumption,
        "game_consumption": game_consumption,
        "book_consumption": book_consumption,
        "comic_consumption": comic_consumption,
        "manga_consumption": manga_consumption,
        "daily_hours_by_media_type": daily_hours_by_media_type,
        "history_highlights": history_highlights,
    }

