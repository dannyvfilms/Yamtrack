import calendar
import datetime
from collections import defaultdict

from django.utils import timezone

from app import config
from app.models import MediaTypes
from app.statistics_cache import STATISTICS_TOP_N


def _game_entry_in_range(game, start_date, end_date):
    """Return True if a game entry overlaps the requested date range."""
    from app.statistics import _get_activity_datetime, _localize_datetime

    if not (start_date and end_date):
        return True

    filter_start = start_date.date() if hasattr(start_date, "date") else start_date
    filter_end = end_date.date() if hasattr(end_date, "date") else end_date

    game_start = game.start_date.date() if game.start_date else None
    game_end = game.end_date.date() if game.end_date else None

    if game_start and game_end:
        return not (game_end < filter_start or game_start > filter_end)
    if game_end:
        return filter_start <= game_end <= filter_end
    if game_start:
        return filter_start <= game_start <= filter_end

    activity_datetime = _get_activity_datetime(game)
    if activity_datetime is None:
        return False
    activity_date = _localize_datetime(activity_datetime).date()
    return filter_start <= activity_date <= filter_end


def _collect_game_data(game_queryset, start_date, end_date):
    """Collect game data with hours, dates, and daily averages.

    Returns:
        list of dicts with keys: game, hours, start_date, end_date, daily_average, activity_datetime
    """
    from app.statistics import _get_activity_datetime, _get_entry_play_dates

    game_data = []

    if game_queryset is None:
        return game_data

    games_by_item = defaultdict(list)
    for game in list(game_queryset):
        if not getattr(game, "item", None):
            continue
        if not _game_entry_in_range(game, start_date, end_date):
            continue
        games_by_item[game.item.id].append(game)

    for entries in games_by_item.values():
        total_minutes = sum((entry.progress or 0) for entry in entries)
        total_hours = total_minutes / 60 if total_minutes else 0
        if total_hours <= 0:
            continue

        activity_datetime = None
        for entry in entries:
            entry_activity = _get_activity_datetime(entry)
            if entry_activity and (activity_datetime is None or entry_activity > activity_datetime):
                activity_datetime = entry_activity
        if activity_datetime is None:
            continue

        start_dates = []
        end_dates = []
        segments = []
        days_played = set()
        total_minutes_for_avg = 0

        for entry in entries:
            entry_minutes = entry.progress or 0
            entry_start = entry.start_date
            entry_end = entry.end_date

            if entry_start:
                start_dates.append(timezone.localtime(entry_start).date())
            if entry_end:
                end_dates.append(timezone.localtime(entry_end).date())

            if entry_minutes > 0:
                total_minutes_for_avg += entry_minutes
                days_played.update(_get_entry_play_dates(entry))

            if entry_start and entry_end:
                start_local = timezone.localtime(entry_start).date()
                end_local = timezone.localtime(entry_end).date()

                if entry_minutes > 0:
                    segments.append(
                        {
                            "start_date": start_local,
                            "end_date": end_local,
                            "hours": entry_minutes / 60,
                            "activity_datetime": _get_activity_datetime(entry),
                        }
                    )
            elif entry_minutes > 0:
                segments.append(
                    {
                        "start_date": None,
                        "end_date": None,
                        "hours": entry_minutes / 60,
                        "activity_datetime": _get_activity_datetime(entry),
                    }
                )

        total_days = len(days_played)
        if total_days:
            daily_average_hours = (total_minutes_for_avg / total_days) / 60
        else:
            daily_average_hours = 0

        game_data.append(
            {
                "game": max(
                    entries,
                    key=lambda entry: _get_activity_datetime(entry) or entry.created_at,
                ),
                "hours": total_hours,
                "start_date": min(start_dates) if start_dates else None,
                "end_date": max(end_dates) if end_dates else None,
                "daily_average": daily_average_hours,
                "activity_datetime": activity_datetime,
                "segments": segments,
            }
        )

    return game_data


def _collect_game_play_data(game_queryset, start_date, end_date):
    """Collect game play data for genre calculation.

    Returns:
        tuple: (list of datetimes, list of (game_entry, datetime, runtime_minutes) tuples)
    """
    from app.statistics import _get_activity_datetime, _localize_datetime

    datetimes = []
    play_details = []  # (game_entry, datetime, runtime_minutes)

    if game_queryset is None:
        return datetimes, play_details

    for game in game_queryset:
        activity_date = _get_activity_datetime(game)
        if activity_date is None:
            continue

        # Check if game is within date range (similar logic to _collect_game_data)
        if not _game_entry_in_range(game, start_date, end_date):
            continue

        # Get runtime in minutes (from progress field)
        runtime_minutes = game.progress or 0
        if runtime_minutes <= 0:
            continue

        localized_date = _localize_datetime(activity_date)
        datetimes.append(localized_date)
        play_details.append((game, localized_date, runtime_minutes))

    return datetimes, play_details


def _build_game_hours_charts(game_data, start_date, end_date, color, dataset_label):
    """Build hours-by-year and hours-by-month charts for games.

    Hours are evenly distributed across the date range of each game.
    """
    from app.statistics import _build_single_series_chart, _localize_datetime

    empty_chart = {"labels": [], "datasets": []}

    if not game_data:
        return {
            "by_year": empty_chart,
            "by_month": empty_chart,
        }

    # Initialize counters
    year_hours = defaultdict(float)
    month_hours = defaultdict(float)

    # Determine filter range dates
    if start_date and hasattr(start_date, "date"):
        filter_start_date = start_date.date()
    elif start_date:
        filter_start_date = start_date
    else:
        filter_start_date = None

    if end_date and hasattr(end_date, "date"):
        filter_end_date = end_date.date()
    elif end_date:
        filter_end_date = end_date
    else:
        filter_end_date = None

    for data in game_data:
        total_hours = data["hours"]
        game_start = data["start_date"]
        game_end = data["end_date"]
        segments = data.get("segments")

        if segments:
            for segment in segments:
                segment_hours = segment.get("hours", 0) or 0
                if segment_hours <= 0:
                    continue

                segment_start = segment.get("start_date")
                segment_end = segment.get("end_date")

                if not segment_start or not segment_end:
                    activity_dt = segment.get("activity_datetime") or data.get("activity_datetime")
                    if not activity_dt:
                        continue

                    activity_date = _localize_datetime(activity_dt).date()
                    if filter_start_date and filter_end_date:
                        if not (filter_start_date <= activity_date <= filter_end_date):
                            continue
                    year_hours[activity_date.year] += segment_hours
                    month_hours[activity_date.month] += segment_hours
                    continue

                segment_total_days = (segment_end - segment_start).days + 1
                if segment_total_days <= 0:
                    segment_total_days = 1

                hours_per_day = segment_hours / segment_total_days

                range_start = segment_start
                range_end = segment_end
                if filter_start_date and filter_end_date:
                    range_start = max(range_start, filter_start_date)
                    range_end = min(range_end, filter_end_date)
                    if range_start > range_end:
                        continue

                current_date = range_start
                while current_date <= range_end:
                    if not filter_start_date or filter_start_date <= current_date <= filter_end_date:
                        year_hours[current_date.year] += hours_per_day
                        month_hours[current_date.month] += hours_per_day
                    current_date += datetime.timedelta(days=1)
            continue

        if not game_start or not game_end:
            # If no date range, assign all hours to activity date
            activity_date = _localize_datetime(data["activity_datetime"]).date()
            if filter_start_date and filter_end_date:
                if filter_start_date <= activity_date <= filter_end_date:
                    year_hours[activity_date.year] += total_hours
                    month_hours[activity_date.month] += total_hours
            elif not filter_start_date and not filter_end_date:
                year_hours[activity_date.year] += total_hours
                month_hours[activity_date.month] += total_hours
            continue

        # Calculate daily average based on full game duration
        game_total_days = (game_end - game_start).days + 1
        if game_total_days <= 0:
            game_total_days = 1

        # Calculate hours per day based on full game duration
        hours_per_day = total_hours / game_total_days

        # Calculate date range (intersection with filter range if applicable)
        range_start = game_start
        range_end = game_end

        if filter_start_date and filter_end_date:
            # Intersect with filter range
            range_start = max(range_start, filter_start_date)
            range_end = min(range_end, filter_end_date)
            if range_start > range_end:
                continue

        # Aggregate hours by year and month
        current_date = range_start
        while current_date <= range_end:
            if not filter_start_date or filter_start_date <= current_date <= filter_end_date:
                year_hours[current_date.year] += hours_per_day
                month_hours[current_date.month] += hours_per_day

            current_date += datetime.timedelta(days=1)

    # Build year chart
    if year_hours:
        sorted_years = sorted(year_hours.keys())
        year_labels = [str(year) for year in sorted_years]
        year_values = [year_hours[year] for year in sorted_years]
        year_chart = _build_single_series_chart(year_labels, year_values, color, dataset_label)
    else:
        year_chart = empty_chart

    # Build month chart
    month_labels = [calendar.month_abbr[i] for i in range(1, 13)]
    month_values = [month_hours.get(i, 0) for i in range(1, 13)]
    month_chart = _build_single_series_chart(month_labels, month_values, color, dataset_label)

    return {
        "by_year": year_chart,
        "by_month": month_chart,
    }


DAILY_AVERAGE_BANDS = [
    (0, 5 / 60, "5 min"),  # 0 to 5 minutes
    (5 / 60, 15 / 60, "15 min"),  # 5 to 15 minutes
    (15 / 60, 30 / 60, "30 min"),  # 15 to 30 minutes
    (30 / 60, 1, "60 min"),  # 30 minutes to 1 hour
    (1, 2, "2 hr"),  # 1 to 2 hours
    (2, 4, "4 hr"),  # 2 to 4 hours
    (4, float("inf"), "4+ hr"),  # 4+ hours
]


def _get_daily_average_band_index(daily_avg_hours):
    """Return the band index for a given daily average hours value."""
    for i, (min_hours, max_hours, _) in enumerate(DAILY_AVERAGE_BANDS):
        if min_hours <= daily_avg_hours < max_hours:
            return i
    if daily_avg_hours >= DAILY_AVERAGE_BANDS[-1][0]:
        return len(DAILY_AVERAGE_BANDS) - 1
    return -1


def _build_daily_average_band_top_games(game_data, limit=5):
    """Build a dict mapping each band label to the top N games by daily average.

    Each entry in the returned dict is a list of serialisable dicts:
        {"title": str, "image": str, "formatted_daily_average": str}

    Only game_data dicts that contain a "game" key (Game model instance with an
    .item FK) will contribute title/image; dicts with only bare fields (cache path)
    are expected to already have "title" and "image" injected by the caller.
    """
    from app.helpers import minutes_to_hhmm

    band_games = {label: [] for _, _, label in DAILY_AVERAGE_BANDS}

    for data in game_data:
        daily_avg_hours = data.get("daily_average", 0)
        if daily_avg_hours <= 0:
            continue
        idx = _get_daily_average_band_index(daily_avg_hours)
        if idx < 0:
            continue
        band_label = DAILY_AVERAGE_BANDS[idx][2]

        game_obj = data.get("game")
        if game_obj is not None:
            item = getattr(game_obj, "item", None)
            title = item.title if item else ""
            image = item.image if item else ""
        else:
            title = data.get("title", "")
            image = data.get("image", "")

        daily_avg_minutes = daily_avg_hours * 60
        band_games[band_label].append(
            {
                "title": title,
                "image": image,
                "formatted_daily_average": minutes_to_hhmm(daily_avg_minutes) + "/day",
                "_sort_key": daily_avg_hours,
            }
        )

    result = {}
    for label, games in band_games.items():
        sorted_games = sorted(games, key=lambda g: g["_sort_key"], reverse=True)[:limit]
        for game in sorted_games:
            game.pop("_sort_key", None)
        if sorted_games:
            result[label] = sorted_games

    return result


def _build_daily_average_distribution_chart(game_data, color, dataset_label):
    """Build chart showing distribution of games by daily average time bands.

    Returns chart data with labels (time bands) and values (number of games),
    plus a ``top_games_per_band`` key with the top 5 games per band (serialisable).
    """
    from app.statistics import _build_single_series_chart

    empty_chart = {"labels": [], "datasets": [], "top_games_per_band": {}}

    if not game_data:
        return empty_chart

    bands = DAILY_AVERAGE_BANDS

    # Count games in each band
    band_counts = [0] * len(bands)

    for data in game_data:
        daily_avg_hours = data["daily_average"]
        idx = _get_daily_average_band_index(daily_avg_hours)
        if 0 <= idx < len(bands):
            band_counts[idx] += 1

    # Extract labels
    labels = [label for _, _, label in bands]

    # Build chart and attach top-games-per-band tooltip data
    chart = _build_single_series_chart(labels, band_counts, color, dataset_label)
    chart["top_games_per_band"] = _build_daily_average_band_top_games(game_data)
    return chart


def _compute_game_top_genres(play_details, limit=STATISTICS_TOP_N):
    """Compute top genres from game play details using stored genres and cache.

    Args:
        play_details: List of (game_entry, datetime, runtime_minutes) tuples
        limit: Number of genres to return

    Returns:
        list of genre dicts with name, minutes, games, formatted_duration
    """
    from django.core.cache import cache

    from app.helpers import minutes_to_hhmm
    from app.models import Sources
    from app.statistics import _coerce_genre_list

    genre_stats = defaultdict(
        lambda: {"minutes": 0, "game_ids": set(), "name": ""}
    )

    for game, dt, runtime in play_details:
        minutes = runtime or 0

        # Get genres from stored item or cached metadata only (don't trigger API calls)
        genres = []
        if hasattr(game, "item") and game.item:
            genres = _coerce_genre_list(getattr(game.item, "genres", None))

            if not genres:
                # Try to get genres from cache directly
                cache_key = f"{Sources.IGDB.value}_{MediaTypes.GAME.value}_{game.item.media_id}"
                cached_metadata = cache.get(cache_key)

                if cached_metadata:
                    # Extract genres from cached metadata
                    genres_raw = cached_metadata.get("genres", [])
                    if genres_raw:
                        genres = _coerce_genre_list(genres_raw)
                    # Also check details.genres if top-level is empty
                    if not genres:
                        details = cached_metadata.get("details", {})
                        if isinstance(details, dict):
                            genres_raw = details.get("genres", [])
                            if genres_raw:
                                genres = _coerce_genre_list(genres_raw)

                if genres and genres != game.item.genres:
                    game.item.genres = genres
                    game.item.save(update_fields=["genres"])

        game_id = None
        if hasattr(game, "item") and game.item:
            game_id = game.item_id
        elif hasattr(game, "id"):
            game_id = game.id

        for genre in genres:
            key = str(genre).title()
            genre_stats[key]["minutes"] += minutes
            genre_stats[key]["name"] = key
            if game_id is not None:
                genre_stats[key]["game_ids"].add(game_id)

    # Sort by minutes (descending), then by games (descending)
    items = sorted(
        genre_stats.values(),
        key=lambda x: (x["minutes"], len(x["game_ids"])),
        reverse=True,
    )[:limit]

    # Format durations
    for item in items:
        item["formatted_duration"] = minutes_to_hhmm(item["minutes"])
        item["games"] = len(item["game_ids"])
        item["plays"] = item["games"]
        item.pop("game_ids", None)

    return items


def _compute_game_top_daily_average(game_data, limit=STATISTICS_TOP_N):
    """Compute top games by daily average time spent.

    Args:
        game_data: List of game data dicts from _collect_game_data
        limit: Number of games to return

    Returns:
        list of dicts with game info and daily_average
    """
    from app.helpers import minutes_to_hhmm

    # Filter games with valid daily averages and sort
    games_with_average = [
        data for data in game_data
        if data["daily_average"] > 0
    ]

    # Sort by daily average (descending)
    sorted_games = sorted(
        games_with_average,
        key=lambda x: x["daily_average"],
        reverse=True,
    )[:limit]

    # Format results
    results = []
    for data in sorted_games:
        game = data["game"]
        daily_avg_hours = data["daily_average"]
        daily_avg_minutes = daily_avg_hours * 60

        results.append(
            {
                "game": game,
                "daily_average_hours": daily_avg_hours,
                "daily_average_minutes": daily_avg_minutes,
                "formatted_daily_average": minutes_to_hhmm(daily_avg_minutes) + "/day",
                "total_hours": data["hours"],
                "formatted_total": minutes_to_hhmm(data["hours"] * 60),
            }
        )

    return results


def _compute_game_platform_breakdown(game_data, user):
    """Compute a breakdown of hours and unique game counts per platform.

    Platform detection priority:
      1. CollectionEntry.resolution  (user's explicitly chosen platform)
      2. item.platforms if exactly one platform listed on the Item
      3. Skip (multi-platform game with no collection data)

    Args:
        game_data: List of game data dicts from _collect_game_data (each has
                   a "game" key pointing to a Game model instance and "hours").
        user: The Django user instance used to query CollectionEntry.

    Returns:
        List of dicts sorted by hours desc:
            {"name": str, "games": int, "hours": float, "formatted_hours": str}
    """
    from app.helpers import minutes_to_hhmm
    from app.models import CollectionEntry

    if not game_data or not user:
        return []

    # Collect all unique item_ids from this period's game_data
    item_to_hours = {}  # item_id -> total hours across all entries for that item
    item_obj_map = {}  # item_id -> item object (for platforms list)
    for data in game_data:
        game = data.get("game")
        if not game:
            continue
        item = getattr(game, "item", None)
        if not item:
            continue
        item_id = item.id
        item_to_hours[item_id] = item_to_hours.get(item_id, 0) + data["hours"]
        item_obj_map[item_id] = item

    if not item_to_hours:
        return []

    # Fetch collection entries for these items so we can read the resolution field
    collection_platform_map = {}  # item_id -> platform string
    ce_qs = CollectionEntry.objects.filter(
        user=user,
        item_id__in=list(item_to_hours.keys()),
    ).values("item_id", "resolution")
    for ce in ce_qs:
        resolution = (ce.get("resolution") or "").strip()
        if resolution:
            collection_platform_map[ce["item_id"]] = resolution

    # Aggregate per platform
    platform_hours = defaultdict(float)
    platform_game_ids = defaultdict(set)

    for item_id, hours in item_to_hours.items():
        item = item_obj_map.get(item_id)
        if not item:
            continue

        # Priority 1: collection entry resolution
        platform = collection_platform_map.get(item_id)

        # Priority 2: single IGDB platform
        if not platform:
            item_platforms = item.platforms if isinstance(item.platforms, list) else []
            item_platforms = [str(p).strip() for p in item_platforms if str(p).strip()]
            if len(item_platforms) == 1:
                platform = item_platforms[0]

        # Skip if we can't determine the platform
        if not platform:
            continue

        platform_hours[platform] += hours
        platform_game_ids[platform].add(item_id)

    if not platform_hours:
        return []

    results = []
    for platform_name, hours in platform_hours.items():
        results.append(
            {
                "name": platform_name,
                "games": len(platform_game_ids[platform_name]),
                "hours": hours,
                "formatted_hours": minutes_to_hhmm(hours * 60),
            }
        )

    return sorted(results, key=lambda x: x["hours"], reverse=True)


def get_game_consumption_stats(user_media, start_date, end_date, minutes_per_type=None, user=None):
    """Return aggregate metrics and chart data for game activity."""
    from app.statistics import (
        _compute_metric_breakdown,
        _infer_user_from_user_media,
        _localize_datetime,
        calculate_minutes_per_media_type,
    )

    if user is None:
        user = _infer_user_from_user_media(user_media)

    game_queryset = (user_media or {}).get(MediaTypes.GAME.value)
    game_data = _collect_game_data(game_queryset, start_date, end_date)

    # Collect play details for genre calculation
    _, play_details = _collect_game_play_data(game_queryset, start_date, end_date)

    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(user_media or {}, start_date, end_date)

    total_minutes = minutes_per_type.get(MediaTypes.GAME.value, 0)
    total_hours = total_minutes / 60 if total_minutes else 0

    # Get activity datetimes for breakdown calculation
    game_datetimes = [
        _localize_datetime(data["activity_datetime"])
        for data in game_data
        if data["activity_datetime"]
    ]

    hours_breakdown = _compute_metric_breakdown(
        total_hours,
        game_datetimes,
        start_date,
        end_date,
    )

    color = config.get_stats_color(MediaTypes.GAME.value)
    chart_label = "Game Hours"

    # Build hours charts
    hours_charts = _build_game_hours_charts(game_data, start_date, end_date, color, chart_label)

    # Build daily average distribution chart (includes top_games_per_band for tooltip)
    daily_avg_chart = _build_daily_average_distribution_chart(game_data, color, "Games")

    # Combine charts
    charts = {
        "by_year": hours_charts["by_year"],
        "by_month": hours_charts["by_month"],
        "by_daily_average": daily_avg_chart,
    }

    # Compute top genres using stored genres, fall back to cached metadata only
    top_genres = _compute_game_top_genres(play_details, limit=STATISTICS_TOP_N)

    # Compute top daily average games
    top_daily_average_games = _compute_game_top_daily_average(game_data, limit=STATISTICS_TOP_N)

    # Compute platform breakdown
    platform_breakdown = _compute_game_platform_breakdown(game_data, user)

    # has_data should be True if we have any game data, not just hours
    has_data = len(game_data) > 0 or total_hours > 0

    return {
        "hours": hours_breakdown,
        "charts": charts,
        "has_data": has_data,
        "top_genres": top_genres,
        "top_daily_average_games": top_daily_average_games,
        "platform_breakdown": platform_breakdown,
    }
