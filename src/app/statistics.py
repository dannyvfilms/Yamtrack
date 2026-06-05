"""statistics.py — Core statistics orchestration for Cursor.

The heavy lifting has been extracted into focused submodules:
  - stats_utils.py    shared pure utilities (parse_runtime_to_minutes, etc.)
  - stats_charts.py   Chart.js dataset builders
  - stats_activity.py timeline, activity calendar, streaks
  - stats_time.py     time-calculation pipeline
  - stats_score.py    score distribution & top-rated

Re-exports at the bottom of this file keep all callers that use
`from app import statistics as stats` fully transparent.
"""
import datetime
import logging
from collections import defaultdict

from django.apps import apps
from django.db import models
from django.db.models import (
    Prefetch,
    Q,
)
from django.utils import timezone

from app import config
from app.models import (
    TV,
    Episode,
    MediaTypes,
    Season,
    Status,
)
from app.statistics_cache import STATISTICS_TOP_N
from app.templatetags import app_tags

logger = logging.getLogger(__name__)


def get_user_media(user, start_date, end_date):
    """Get all media items and their counts for a user within date range."""
    media_models = [
        apps.get_model(app_label="app", model_name=media_type)
        for media_type in user.get_active_media_types()
    ]
    user_media = {}
    media_count = {"total": 0}

    # Cache the base episodes query
    base_episodes = None
    if TV in media_models or Season in media_models:
        if start_date is None and end_date is None:
            # No date filtering for "All Time"
            base_episodes = Episode.objects.filter(
                related_season__user=user,
            )
        else:
            base_episodes = Episode.objects.filter(
                related_season__user=user,
                end_date__range=(start_date, end_date),
            )

    _tv_ids = None  # saved for grouped-anime pass after the main loop

    for model in media_models:
        media_type = model.__name__.lower()
        queryset = None

        if model == TV:
            _tv_ids = base_episodes.values_list(
                "related_season__related_tv",
                flat=True,
            ).distinct()
            # Exclude grouped anime (library_media_type="anime") — they belong in the anime bucket
            queryset = TV.objects.filter(
                id__in=_tv_ids,
                status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
            ).exclude(
                item__library_media_type=MediaTypes.ANIME.value,
            ).prefetch_related(
                Prefetch(
                    "seasons",
                    queryset=Season.objects.filter(
                        status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
                    ).select_related(
                        "item",
                    ).prefetch_related(
                        Prefetch(
                            "episodes",
                            queryset=base_episodes.filter(
                                related_season__related_tv__in=_tv_ids,
                            ),
                        ),
                    ),
                ),
            )
        elif model == Season:
            season_ids = base_episodes.values_list(
                "related_season",
                flat=True,
            ).distinct()
            queryset = Season.objects.filter(
                id__in=season_ids,
                status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
            ).prefetch_related(
                Prefetch("episodes", queryset=base_episodes),
            )
        # For other models, apply date filtering conditionally
        elif start_date is None and end_date is None:
            # No date filtering for "All Time"
            queryset = model.objects.filter(
                user=user,
                status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
            )
        else:
            queryset = model.objects.filter(
                user=user,
                status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
            ).filter(
                # Case 1: Media has both start_date and end_date
                # Include if ranges overlap
                # (exclude if media ends before filter start or starts after filter end)
                (
                    Q(start_date__isnull=False)
                    & Q(end_date__isnull=False)
                    & ~(Q(end_date__lt=start_date) | Q(start_date__gt=end_date))
                )
                |
                # Case 2: Media only has start_date (end_date is null)
                # Include if start_date is within filter range
                (
                    Q(start_date__isnull=False)
                    & Q(end_date__isnull=True)
                    & Q(start_date__gte=start_date)
                    & Q(start_date__lte=end_date)
                )
                |
                # Case 3: Media only has end_date (start_date is null)
                # Include if end_date is within filter range
                (
                    Q(start_date__isnull=True)
                    & Q(end_date__isnull=False)
                    & Q(end_date__gte=start_date)
                    & Q(end_date__lte=end_date)
                ),
            )

        queryset = queryset.select_related("item")
        user_media[media_type] = queryset
        count = queryset.count()
        media_count[media_type] = count
        media_count["total"] += count

    # Pull grouped anime (TV-structured, library_media_type="anime") into the anime bucket
    if _tv_ids is not None and base_episodes is not None:
        _grouped_anime_qs = (
            TV.objects.filter(
                id__in=_tv_ids,
                item__library_media_type=MediaTypes.ANIME.value,
                status__in=[
                    Status.IN_PROGRESS.value,
                    Status.COMPLETED.value,
                    Status.DROPPED.value,
                    Status.PAUSED.value,
                ],
            )
            .select_related("item")
            .prefetch_related(
                Prefetch(
                    "seasons",
                    queryset=Season.objects.filter(
                        status__in=[
                            Status.IN_PROGRESS.value,
                            Status.COMPLETED.value,
                            Status.DROPPED.value,
                            Status.PAUSED.value,
                        ],
                    )
                    .select_related("item")
                    .prefetch_related(
                        Prefetch(
                            "episodes",
                            queryset=base_episodes.filter(
                                related_season__related_tv__in=_tv_ids,
                            ),
                        ),
                    ),
                ),
            )
        )
        _grouped_anime_count = _grouped_anime_qs.count()
        if _grouped_anime_count > 0:
            anime_key = MediaTypes.ANIME.value
            if anime_key in user_media:
                # Combine with flat anime (MAL) queryset already in the bucket
                user_media[anime_key] = _CombinedMediaBucket(user_media[anime_key], _grouped_anime_qs)
                media_count[anime_key] = media_count.get(anime_key, 0) + _grouped_anime_count
            else:
                user_media[anime_key] = _grouped_anime_qs
                media_count[anime_key] = _grouped_anime_count
            media_count["total"] += _grouped_anime_count

    logger.info(
        "%s - Retrieved media %s",
        user,
        "for all time" if start_date is None else f"from {start_date} to {end_date}",
    )
    return user_media, media_count


def get_media_type_distribution(media_count, minutes_per_type=None):
    """Get data formatted for Chart.js pie chart."""
    # Define colors for each media type
    # Format for Chart.js
    chart_data = {
        "labels": [],
        "datasets": [
            {
                "data": [],
                "backgroundColor": [],
            },
        ],
    }

    dataset = chart_data["datasets"][0]

    if minutes_per_type:
        dataset["value_label"] = "Hours"
        dataset["value_suffix"] = "h"
        dataset["value_decimals"] = 1

        ordered_types = list(MEDIA_TYPE_HOURS_ORDER)
        ordered_types.extend(
            [media_type for media_type in minutes_per_type if media_type not in ordered_types],
        )

        for media_type in ordered_types:
            total_minutes = minutes_per_type.get(media_type, 0) or 0
            if total_minutes <= 0:
                continue
            hours = round(total_minutes / 60, 2)
            if hours <= 0:
                continue
            label = app_tags.media_type_readable(media_type)
            chart_data["labels"].append(label)
            dataset["data"].append(hours)
            dataset["backgroundColor"].append(
                config.get_stats_color(media_type),
            )
        return chart_data

    # Only include media types with counts > 0
    for media_type, count in media_count.items():
        if media_type != "total" and count > 0:
            # Format label with first letter capitalized
            label = app_tags.media_type_readable(media_type)
            chart_data["labels"].append(label)
            dataset["data"].append(count)
            dataset["backgroundColor"].append(
                config.get_stats_color(media_type),
            )
    return chart_data


def get_status_distribution(user_media):
    """Get status distribution for each media type within date range."""
    distribution = {}
    total_completed = 0
    # Define status order to ensure consistent stacking
    status_order = list(Status.values)
    for media_type, media_list in user_media.items():
        status_counts = dict.fromkeys(status_order, 0)
        counts = media_list.values("status").annotate(count=models.Count("id"))
        for count_data in counts:
            status_counts[count_data["status"]] = (
                status_counts.get(count_data["status"], 0) + count_data["count"]
            )
            if count_data["status"] == Status.COMPLETED.value:
                total_completed += count_data["count"]

        distribution[media_type] = status_counts

    # Format the response for charting
    return {
        "labels": [app_tags.media_type_readable(x) for x in distribution],
        "datasets": [
            {
                "label": status,
                "data": [
                    distribution[media_type][status] for media_type in distribution
                ],
                "background_color": get_status_color(status),
                "total": sum(
                    distribution[media_type][status] for media_type in distribution
                ),
            }
            for status in status_order
        ],
        "total_completed": total_completed,
    }


def get_status_pie_chart_data(status_distribution):
    """Get status distribution as a pie chart."""
    # Format for Chart.js pie chart
    chart_data = {
        "labels": [],
        "datasets": [
            {
                "data": [],
                "backgroundColor": [],
            },
        ],
    }

    # Process each status dataset
    for dataset in status_distribution["datasets"]:
        status_label = dataset["label"]
        status_count = dataset["total"]
        status_color = dataset["background_color"]

        # Only include statuses with counts > 0
        if status_count > 0:
            chart_data["labels"].append(status_label)
            chart_data["datasets"][0]["data"].append(status_count)
            chart_data["datasets"][0]["backgroundColor"].append(status_color)

    return chart_data


def get_status_color(status):
    """Get the color for the status of the media."""
    try:
        return config.get_status_stats_color(status)
    except KeyError:
        return "rgba(201, 203, 207)"


# ---------------------------------------------------------------------------
# Play-data collection helpers (used by consumption stats below)
# ---------------------------------------------------------------------------

def _collect_episode_datetimes(tv_queryset, start_date, end_date):
    """Return localized episode completion datetimes for the queryset."""
    datetimes = []

    if tv_queryset is None:
        return datetimes

    for tv in tv_queryset:
        seasons = getattr(tv, "seasons", None)
        if seasons is None:
            continue

        for season in seasons.all():
            episodes = getattr(season, "episodes", None)
            if episodes is None:
                continue

            for episode in episodes.all():
                if not episode.end_date:
                    continue
                if not _is_episode_in_range(episode, start_date, end_date):
                    continue
                localized_date = _localize_datetime(episode.end_date)
                datetimes.append(localized_date)

    return datetimes


def _collect_movie_datetimes(movie_queryset, start_date, end_date):
    """Return localized movie completion datetimes for the queryset."""
    datetimes = []

    if movie_queryset is None:
        return datetimes

    for movie in movie_queryset:
        activity_date = _get_activity_datetime(movie)
        if activity_date is None:
            continue

        if start_date and end_date:
            if not (start_date <= activity_date <= end_date):
                continue

        datetimes.append(_localize_datetime(activity_date))

    return datetimes


def _collect_movie_play_data(movie_queryset, start_date, end_date):
    """Collect movie play datetimes and per-play runtime.

    Returns:
        tuple: (list of datetimes, list of (movie_entry, datetime, runtime_minutes) tuples)
    """
    datetimes = []
    play_details = []  # (movie_entry, datetime, runtime_minutes)

    if movie_queryset is None:
        return datetimes, play_details

    import logging  # noqa: PLC0415
    _logger = logging.getLogger(__name__)

    for movie in movie_queryset:
        activity_date = _get_activity_datetime(movie)
        if activity_date is None:
            continue

        if start_date and end_date:
            if not (start_date <= activity_date <= end_date):
                continue

        # Get runtime for this movie
        runtime_minutes = _get_media_runtime_from_cache(movie, _logger, context="movie play data")
        if runtime_minutes <= 0:
            # Skip if no runtime available
            continue

        localized_date = _localize_datetime(activity_date)
        datetimes.append(localized_date)
        play_details.append((movie, localized_date, runtime_minutes))

    return datetimes, play_details


def _collect_tv_play_data(tv_queryset, start_date, end_date):
    """Collect TV episode play datetimes and per-play runtime.

    Returns:
        tuple: (list of datetimes, list of (episode_entry, datetime, runtime_minutes) tuples)
    """
    datetimes = []
    play_details = []  # (episode_entry, datetime, runtime_minutes)

    if tv_queryset is None:
        return datetimes, play_details

    import logging  # noqa: PLC0415
    _logger = logging.getLogger(__name__)

    for tv in tv_queryset:
        seasons = getattr(tv, "seasons", None)
        if seasons is None:
            continue

        for season in seasons.all():
            episodes = getattr(season, "episodes", None)
            if episodes is None:
                continue

            for episode in episodes.all():
                if not episode.end_date:
                    continue
                if not _is_episode_in_range(episode, start_date, end_date):
                    continue

                # Get runtime for this episode
                runtime_minutes = _get_media_runtime_from_cache(episode, _logger, context="TV episode play data")
                if runtime_minutes <= 0:
                    # Skip if no runtime available
                    continue

                localized_date = _localize_datetime(episode.end_date)
                datetimes.append(localized_date)
                play_details.append((episode, localized_date, runtime_minutes))

    return datetimes, play_details


# ---------------------------------------------------------------------------
# Per-media-type consumption stats
# ---------------------------------------------------------------------------

def get_tv_consumption_stats(user_media, start_date, end_date, minutes_per_type=None):
    """Return aggregate metrics and chart data for TV episode activity."""
    tv_queryset = (user_media or {}).get(MediaTypes.TV.value)
    episode_datetimes = _collect_episode_datetimes(tv_queryset, start_date, end_date)

    # Collect play details for genre calculation
    _, play_details = _collect_tv_play_data(tv_queryset, start_date, end_date)

    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(
            user_media or {},
            start_date,
            end_date,
        )

    total_minutes = minutes_per_type.get(MediaTypes.TV.value, 0)
    total_hours = total_minutes / 60 if total_minutes else 0
    total_plays = len(episode_datetimes)

    hours_breakdown = _compute_metric_breakdown(
        total_hours,
        episode_datetimes,
        start_date,
        end_date,
    )
    plays_breakdown = _compute_metric_breakdown(
        total_plays,
        episode_datetimes,
        start_date,
        end_date,
    )

    color = config.get_stats_color(MediaTypes.TV.value)
    chart_label = "Episode Plays"
    charts = _build_media_charts(episode_datetimes, color, chart_label)

    # Compute top genres
    top_genres = _compute_movie_tv_top_genres(play_details, limit=STATISTICS_TOP_N)

    return {
        "hours": hours_breakdown,
        "plays": plays_breakdown,
        "charts": charts,
        "has_data": total_plays > 0,
        "top_genres": top_genres,
    }


def get_movie_consumption_stats(user_media, start_date, end_date, minutes_per_type=None):
    """Return aggregate metrics and chart data for movie activity."""
    movie_queryset = (user_media or {}).get(MediaTypes.MOVIE.value)
    movie_datetimes = _collect_movie_datetimes(movie_queryset, start_date, end_date)

    # Collect play details for genre calculation
    _, play_details = _collect_movie_play_data(movie_queryset, start_date, end_date)

    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(user_media or {}, start_date, end_date)

    total_minutes = minutes_per_type.get(MediaTypes.MOVIE.value, 0)
    total_hours = total_minutes / 60 if total_minutes else 0
    total_plays = len(movie_datetimes)

    hours_breakdown = _compute_metric_breakdown(
        total_hours,
        movie_datetimes,
        start_date,
        end_date,
    )
    plays_breakdown = _compute_metric_breakdown(
        total_plays,
        movie_datetimes,
        start_date,
        end_date,
    )

    color = config.get_stats_color(MediaTypes.MOVIE.value)
    chart_label = "Movie Plays"
    charts = _build_media_charts(movie_datetimes, color, chart_label)

    # Compute top genres
    top_genres = _compute_movie_tv_top_genres(play_details, limit=STATISTICS_TOP_N)

    return {
        "hours": hours_breakdown,
        "plays": plays_breakdown,
        "charts": charts,
        "has_data": total_plays > 0,
        "top_genres": top_genres,
    }


def get_anime_consumption_stats(user_media, start_date, end_date, minutes_per_type=None):
    """Return aggregate metrics and chart data for anime (grouped episode-based + standalone)."""
    import logging as _logging  # noqa: PLC0415

    anime_queryset = (user_media or {}).get(MediaTypes.ANIME.value)

    # Episode-level datetimes from grouped anime (TV shows with library_media_type='anime')
    episode_datetimes = _collect_episode_datetimes(anime_queryset, start_date, end_date)
    _, episode_play_details = _collect_tv_play_data(anime_queryset, start_date, end_date)

    # Show-level datetimes from standalone Anime model instances
    standalone_datetimes = []
    standalone_play_details = []
    if anime_queryset is not None:
        _logger = _logging.getLogger(__name__)
        for media in _iter_media_list(anime_queryset):
            if getattr(media, "seasons", None) is not None:
                continue  # grouped anime — already counted via episodes above
            activity_date = _get_activity_datetime(media)
            if activity_date is None:
                continue
            if start_date and end_date:
                if not (start_date <= activity_date <= end_date):
                    continue
            runtime = _get_media_runtime_from_cache(media, _logger, context="anime play data")
            localized = _localize_datetime(activity_date)
            standalone_datetimes.append(localized)
            if runtime > 0:
                standalone_play_details.append((media, localized, runtime))

    all_datetimes = sorted(episode_datetimes + standalone_datetimes)
    all_play_details = episode_play_details + standalone_play_details

    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(user_media or {}, start_date, end_date)

    total_minutes = minutes_per_type.get(MediaTypes.ANIME.value, 0)
    total_hours = total_minutes / 60 if total_minutes else 0
    total_plays = len(all_datetimes)

    hours_breakdown = _compute_metric_breakdown(total_hours, all_datetimes, start_date, end_date)
    plays_breakdown = _compute_metric_breakdown(total_plays, all_datetimes, start_date, end_date)

    color = config.get_stats_color(MediaTypes.ANIME.value)
    charts = _build_media_charts(all_datetimes, color, "Anime Plays")
    top_genres = _compute_movie_tv_top_genres(all_play_details, limit=STATISTICS_TOP_N)

    return {
        "hours": hours_breakdown,
        "plays": plays_breakdown,
        "charts": charts,
        "has_data": total_plays > 0,
        "top_genres": top_genres,
    }


# ---------------------------------------------------------------------------
# Genre aggregation
# ---------------------------------------------------------------------------

# Country name mapping (ISO 3166-1 alpha-2 -> English name)
def _compute_movie_tv_top_genres(play_details, limit=STATISTICS_TOP_N):
    """Compute top genres from movie/TV play details.

    Args:
        play_details: List of (media_entry, datetime, runtime_minutes) tuples
        limit: Number of genres to return

    Returns:
        list of genre dicts with name, minutes, plays, formatted_duration
    """
    from app.helpers import minutes_to_hhmm  # noqa: PLC0415
    from app.models import Episode  # noqa: PLC0415

    genre_stats = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": ""})

    for media, dt, runtime in play_details:
        minutes = runtime or 0

        # Get genres from media.item.details or metadata
        genres = []

        # For TV episodes, get genres from the parent TV show
        # For movies, get genres directly from the movie
        media_to_use = media
        if isinstance(media, Episode):
            # Episode -> Season -> TV show
            if hasattr(media, "related_season") and media.related_season:
                if hasattr(media.related_season, "related_tv") and media.related_season.related_tv:
                    media_to_use = media.related_season.related_tv
                else:
                    # Skip if we can't get the TV show
                    continue
            else:
                # Skip if we can't get the season
                continue

        if hasattr(media_to_use, "item") and media_to_use.item:
            # Try to get genres from item details
            try:
                metadata = _get_media_metadata_for_statistics(media_to_use)
                if metadata:
                    details = metadata.get("details") if isinstance(metadata, dict) else None
                    if isinstance(details, dict):
                        genres_raw = details.get("genres", [])
                        if genres_raw:
                            genres = _coerce_genre_list(genres_raw)
                    # Also check top-level genres
                    if not genres:
                        genres_raw = metadata.get("genres", [])
                        if genres_raw:
                            genres = _coerce_genre_list(genres_raw)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                # Skip this media if metadata retrieval fails
                import logging  # noqa: PLC0415
                _g_logger = logging.getLogger(__name__)
                _g_logger.debug(f"Skipping genre calculation for {getattr(media_to_use.item, 'title', 'unknown')}: {e}")
                continue

        for genre in genres:
            key = str(genre).title()
            genre_stats[key]["minutes"] += minutes
            genre_stats[key]["plays"] += 1
            genre_stats[key]["name"] = key

    # Sort by minutes (descending), then by plays (descending)
    items = sorted(
        genre_stats.values(),
        key=lambda x: (x["minutes"], x["plays"]),
        reverse=True,
    )[:limit]

    # Format durations
    for item in items:
        item["formatted_duration"] = minutes_to_hhmm(item["minutes"])

    return items


# ---------------------------------------------------------------------------
# Daily hours by media type (stacked bar chart)
# ---------------------------------------------------------------------------

def get_daily_hours_by_media_type(user_media, start_date, end_date):
    """Build Chart.js-friendly stacked bar data where X axis is dates (inclusive)
    between start_date and end_date and Y axis is hours per media type per day.

    Currently implemented for movies; other media types included as zeros and can
    be expanded later.
    """
    # If no date range is provided (All Time), infer a sensible range from
    # available media activity dates so the chart can show a meaningful span.
    if not start_date or not end_date:
        # Gather all candidate activity datetimes from the provided media
        candidate_dates = []
        for media_list in user_media.values():
            for media in _iter_media_list(media_list):
                activity_dt = _get_activity_datetime(media)
                if activity_dt:
                    candidate_dates.append(_localize_datetime(activity_dt))

        if not candidate_dates:
            # No activity dates available -> nothing to chart
            return {"labels": [], "datasets": []}

        # Derive start/end from min/max activity datetimes
        min_dt = min(candidate_dates)
        max_dt = max(candidate_dates)
        # Convert to naive date boundaries for the rest of the function
        start_date = datetime.datetime.combine(min_dt.date(), datetime.time.min)
        end_date = datetime.datetime.combine(max_dt.date(), datetime.time.max)
        # Ensure they are timezone-aware in the current timezone
        try:
            start_date = timezone.make_aware(start_date)
            end_date = timezone.make_aware(end_date)
        except Exception:
            # If awareness fails, fall back to original naive datetimes
            pass

    # Normalize to dates (without time)
    start_date_dt = start_date.date()
    end_date_dt = end_date.date()
    if start_date_dt > end_date_dt:
        start_date_dt, end_date_dt = end_date_dt, start_date_dt

    # Build list of date labels in ISO format (YYYY-MM-DD)
    num_days = (end_date_dt - start_date_dt).days + 1
    labels = [(start_date_dt + datetime.timedelta(days=i)).isoformat() for i in range(num_days)]

    # Prepare per-media-type mapping of date -> minutes
    per_type_minutes = {mt: dict.fromkeys(labels, 0) for mt in user_media.keys()}

    # We'll need the runtime lookup function and logger
    for media_type, media_list in user_media.items():
        # Movies
        if media_type == MediaTypes.MOVIE.value:
            for media in _iter_media_list(media_list):
                activity_dt = _get_activity_datetime(media)
                if activity_dt is None:
                    continue
                activity_date = _localize_datetime(activity_dt).date()
                if activity_date < start_date_dt or activity_date > end_date_dt:
                    continue

                # Get runtime in minutes from cache (will attempt metadata fetch if missing)
                minutes = _get_media_runtime_from_cache(media, logger, "(daily aggregation)")
                if not minutes or minutes <= 0:
                    continue

                label = activity_date.isoformat()
                if label in per_type_minutes[media_type]:
                    per_type_minutes[media_type][label] += minutes

        # TV shows / Seasons: use per-episode end_date and runtime from episode cache
        elif media_type == MediaTypes.TV.value or media_type == MediaTypes.SEASON.value:
            for tv in _iter_media_list(media_list):
                seasons = getattr(tv, "seasons", None)
                if seasons is None:
                    continue
                for season in seasons.all():
                    episodes = getattr(season, "episodes", None)
                    if episodes is None:
                        continue
                    for episode in episodes.all():
                        if not episode.end_date:
                            continue
                        ep_date = _localize_datetime(episode.end_date).date()
                        if ep_date < start_date_dt or ep_date > end_date_dt:
                            continue
                        # runtime from cached episode data
                        try:
                            minutes = _calculate_episode_time_from_cache(episode, logger)
                        except Exception:
                            minutes = 0
                        if minutes and minutes > 0:
                            label = ep_date.isoformat()
                            if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                                per_type_minutes[media_type][label] += minutes

        # Anime: grouped anime uses episode-level data; flat anime uses progress * runtime
        elif media_type == MediaTypes.ANIME.value:
            for media in _iter_media_list(media_list):
                if hasattr(media, "seasons"):
                    # Grouped anime (TV model) — distribute by episode end_date
                    seasons = getattr(media, "seasons", None)
                    if seasons is None:
                        continue
                    for season in seasons.all():
                        episodes = getattr(season, "episodes", None)
                        if episodes is None:
                            continue
                        for episode in episodes.all():
                            if not episode.end_date:
                                continue
                            ep_date = _localize_datetime(episode.end_date).date()
                            if ep_date < start_date_dt or ep_date > end_date_dt:
                                continue
                            try:
                                ep_minutes = _calculate_episode_time_from_cache(episode, logger)
                            except Exception:
                                ep_minutes = 0
                            if ep_minutes and ep_minutes > 0:
                                label = ep_date.isoformat()
                                if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                                    per_type_minutes[media_type][label] += ep_minutes
                else:
                    # Flat anime (Anime model) — total minutes from cached runtime * progress
                    episode_count = getattr(media, "progress", 0) or 0
                    if episode_count <= 0:
                        continue
                    minutes = _get_anime_runtime_from_cache(media, episode_count, logger, "(daily aggregation)")
                    if not minutes or minutes <= 0:
                        continue

                    media_start = getattr(media, "start_date", None)
                    media_end = getattr(media, "end_date", None)
                    if media_start and media_end:
                        ds = max(media_start.date(), start_date_dt)
                        de = min(media_end.date(), end_date_dt)
                        if ds > de:
                            continue
                        days = (de - ds).days + 1
                        per_day = minutes / days
                        for i in range(days):
                            d = (ds + datetime.timedelta(days=i)).isoformat()
                            if media_type in per_type_minutes and d in per_type_minutes[media_type]:
                                per_type_minutes[media_type][d] += per_day
                    else:
                        activity_dt = _get_activity_datetime(media)
                        if not activity_dt:
                            continue
                        label = _localize_datetime(activity_dt).date().isoformat()
                        if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                            per_type_minutes[media_type][label] += minutes

        # Music: assign runtime to each play date from history records
        elif media_type == MediaTypes.MUSIC.value:
            for media in _iter_media_list(media_list):
                runtime_minutes = _get_music_runtime_minutes(media)
                if runtime_minutes <= 0:
                    continue

                # Each history record represents a play
                for history_record in media.history.all():
                    history_end_date = getattr(history_record, "end_date", None)
                    if not history_end_date:
                        continue

                    play_date = _localize_datetime(history_end_date).date()
                    if play_date < start_date_dt or play_date > end_date_dt:
                        continue

                    label = play_date.isoformat()
                    if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                        per_type_minutes[media_type][label] += runtime_minutes

        # Podcasts: use history records so deleted plays don't appear
        elif media_type == MediaTypes.PODCAST.value:
            podcast_user = _infer_user_from_user_media(user_media)
            podcast_history_records, podcasts_lookup = _get_podcast_history_data(
                podcast_user,
                start_date,
                end_date,
            )
            _, play_details = _collect_podcast_play_data(
                podcast_history_records,
                podcasts_lookup,
                start_date,
                end_date,
            )

            for _, play_dt, runtime_minutes in play_details:
                if not play_dt or runtime_minutes <= 0:
                    continue

                completion_date = play_dt.date()
                if completion_date < start_date_dt or completion_date > end_date_dt:
                    continue

                label = completion_date.isoformat()
                if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                    per_type_minutes[media_type][label] += runtime_minutes

        # Manga, Games, Books, Comics: use progress field and distribute evenly across item's date span
        elif media_type in (
            MediaTypes.MANGA.value,
            MediaTypes.GAME.value,
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.BOARDGAME.value,
        ):
            for media in _iter_media_list(media_list):
                total_progress = getattr(media, "progress", 0) or 0
                if not total_progress or total_progress <= 0:
                    continue

                # For games, progress is stored in minutes; for others we follow user instruction and treat 'progress' as an amount to distribute
                total_minutes = total_progress

                media_start = getattr(media, "start_date", None)
                media_end = getattr(media, "end_date", None)
                if media_start and media_end:
                    ds = max(media_start.date(), start_date_dt)
                    de = min(media_end.date(), end_date_dt)
                    if ds > de:
                        continue
                    days = (de - ds).days + 1
                    per_day = total_minutes / days
                    for i in range(days):
                        d = (ds + datetime.timedelta(days=i)).isoformat()
                        if media_type in per_type_minutes and d in per_type_minutes[media_type]:
                            per_type_minutes[media_type][d] += per_day
                else:
                    activity_dt = _get_activity_datetime(media)
                    if not activity_dt:
                        continue
                    label = _localize_datetime(activity_dt).date().isoformat()
                    if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                        per_type_minutes[media_type][label] += total_minutes

    # Build datasets for Chart.js: convert minutes -> hours (float)
    datasets = []
    ordered_types = list(MEDIA_TYPE_HOURS_ORDER)
    ordered_types.extend(
        [media_type for media_type in per_type_minutes.keys() if media_type not in ordered_types]
    )
    for media_type in ordered_types:
        date_map = per_type_minutes.get(media_type)
        if not date_map:
            continue
        # Skip media types that have zero total minutes
        total = sum(date_map.values())
        if total == 0:
            continue

        datasets.append({
            "label": app_tags.media_type_readable(media_type),
            "data": [round(date_map[d] / 60, 2) for d in labels],
            "background_color": config.get_stats_color(media_type),
        })

    return {"labels": labels, "datasets": datasets}


# ---------------------------------------------------------------------------
# Top played media
# ---------------------------------------------------------------------------

def get_top_played_media(user_media, start_date, end_date):
    """Get top played media by total time spent within date range.

    Returns a dictionary with media types as keys and lists of top media items.
    Each media item includes total_time_minutes, formatted_duration, and episode_count.
    """
    import logging  # noqa: PLC0415

    from app.helpers import minutes_to_hhmm  # noqa: PLC0415

    _logger = logging.getLogger(__name__)
    top_played = {}

    # Define the media types we want to show
    target_media_types = ["movie", "tv", "game", "boardgame", "anime", "music"]

    for media_type, media_list in user_media.items():
        # Normalize media type to match our target types
        normalized_type = media_type.lower()
        if normalized_type not in target_media_types:
            continue

        if not media_list.exists():
            continue

        # Get media items with their progress and metadata
        media_with_progress = []

        if normalized_type == "movie":
            aggregated_movies = {}

            for media in _iter_media_list(media_list):
                total_time_minutes = _calculate_movie_time(media, start_date, end_date, normalized_type, _logger)
                if total_time_minutes <= 0:
                    continue

                item = getattr(media, "item", None)
                if not item:
                    continue

                # Use item id when available, fallback to (media_id, source) tuple
                item_key = getattr(item, "id", None)
                if item_key is None:
                    item_key = (getattr(item, "media_id", None), getattr(item, "source", None))

                activity = media.end_date or media.start_date or media.created_at
                if item_key not in aggregated_movies:
                    aggregated_movies[item_key] = {
                        "media": media,
                        "total_time_minutes": total_time_minutes,
                        "formatted_duration": None,  # populated after aggregation
                        "episode_count": 0,
                        "last_activity": activity,
                        "play_count": 1,
                        "_media_activity": activity,
                    }
                else:
                    entry = aggregated_movies[item_key]
                    entry["total_time_minutes"] += total_time_minutes
                    entry["play_count"] += 1

                    if activity and (entry["last_activity"] is None or activity > entry["last_activity"]):
                        entry["last_activity"] = activity

                    current_media_activity = entry.get("_media_activity")
                    if activity and (current_media_activity is None or activity > current_media_activity):
                        entry["media"] = media
                        entry["_media_activity"] = activity

            for entry in aggregated_movies.values():
                entry["formatted_duration"] = minutes_to_hhmm(entry["total_time_minutes"])
                entry.pop("_media_activity", None)
                media_with_progress.append(entry)
        elif normalized_type == "game":
            aggregated_games = {}

            for media in _iter_media_list(media_list):
                total_time_minutes = _calculate_game_time_in_range(media, start_date, end_date)
                if total_time_minutes <= 0:
                    continue

                item = getattr(media, "item", None)
                if not item:
                    continue

                # Use item id when available, fallback to (media_id, source) tuple
                item_key = getattr(item, "id", None)
                if item_key is None:
                    item_key = (getattr(item, "media_id", None), getattr(item, "source", None))

                activity = media.end_date or media.start_date or media.created_at
                if item_key not in aggregated_games:
                    aggregated_games[item_key] = {
                        'media': media,
                        'total_time_minutes': total_time_minutes,
                        'formatted_duration': None,  # populated after aggregation
                        'episode_count': 0,
                        'last_activity': activity,
                        'play_count': 1,
                        '_media_activity': activity,
                    }
                else:
                    entry = aggregated_games[item_key]
                    entry['total_time_minutes'] += total_time_minutes
                    entry['play_count'] += 1

                    if activity and (entry['last_activity'] is None or activity > entry['last_activity']):
                        entry['last_activity'] = activity

                    current_media_activity = entry.get('_media_activity')
                    if activity and (current_media_activity is None or activity > current_media_activity):
                        entry['media'] = media
                        entry['_media_activity'] = activity

            for entry in aggregated_games.values():
                entry['formatted_duration'] = minutes_to_hhmm(entry['total_time_minutes'])
                entry.pop('_media_activity', None)
                media_with_progress.append(entry)
        else:
            for media in _iter_media_list(media_list):
                total_time_minutes = 0
                episode_count = 0

                if normalized_type == "tv":
                    total_time_minutes, episode_count = _calculate_tv_time(media, start_date, end_date, _logger)
                elif normalized_type == "anime":
                    # Grouped anime uses TV model (seasons + episodes)
                    if hasattr(media, "seasons"):
                        total_time_minutes, episode_count = _calculate_tv_time(media, start_date, end_date, _logger)
                    else:
                        total_time_minutes, episode_count = _calculate_anime_time(media, start_date, end_date, _logger)
                elif normalized_type == "boardgame":
                    if (
                        media.end_date
                        and start_date
                        and end_date
                        and start_date <= media.end_date <= end_date
                    ) or (
                        media.start_date
                        and start_date
                        and end_date
                        and start_date <= media.start_date <= end_date
                    ) or (not start_date and not end_date):
                        total_time_minutes += media.progress
                elif normalized_type == "music":
                    # Music: sum runtime for each play (history record) within date range
                    total_time_minutes = _calculate_music_time(media, start_date, end_date, _logger)
                    # Count plays for display - deduplicate by end_date (each unique end_date = one play)
                    play_count = 0
                    history_records = list(media.history.all().order_by("history_date"))

                    # Group by end_date to deduplicate
                    unique_end_dates = set()
                    for history_record in history_records:
                        history_end_date = getattr(history_record, "end_date", None)
                        if not history_end_date:
                            continue
                        unique_end_dates.add(history_end_date)

                    # Count unique plays within date range
                    for play_end_date in unique_end_dates:
                        if start_date and end_date:
                            if start_date <= play_end_date <= end_date:
                                play_count += 1
                        else:
                            play_count += 1

                    episode_count = play_count  # Reuse episode_count for plays
                else:
                    # For movies and other media types, get runtime from metadata
                    total_time_minutes = _calculate_movie_time(media, start_date, end_date, normalized_type, _logger)

                if total_time_minutes > 0:
                    formatted_duration = minutes_to_hhmm(total_time_minutes)
                    if normalized_type == "boardgame":
                        formatted_duration = f"{total_time_minutes} play{'s' if total_time_minutes != 1 else ''}"

                    media_with_progress.append({
                        "media": media,
                        "total_time_minutes": total_time_minutes,
                        "formatted_duration": formatted_duration,
                        "episode_count": episode_count,
                        "last_activity": media.end_date or media.start_date or media.created_at,
                        "play_count": 1,
                    })

        # Sort by total time, then by most recent activity
        media_with_progress.sort(
            key=lambda x: (x["total_time_minutes"], x["last_activity"]),
            reverse=True,
        )

        # Take top 50 for games, top 10 for other media types
        limit = 50 if normalized_type == "game" else 10
        top_played[normalized_type] = media_with_progress[:limit]

    return top_played


# ---------------------------------------------------------------------------
# Re-exports from extracted submodules — keeps all callers using
# `from app import statistics as stats` fully transparent.
# ---------------------------------------------------------------------------
from app.stats_utils import (  # noqa: E402,F401
    MEDIA_TYPE_HOURS_ORDER,
    _CombinedMediaBucket,
    _CombinedValuesResult,
    _coerce_genre_list,
    _format_hours_minutes,
    _format_long_units,
    _get_activity_datetime,
    _get_entry_play_dates,
    _infer_user_from_user_media,
    _is_media_in_date_range,
    _iter_media_list,
    _localize_datetime,
    parse_runtime_to_minutes,
)
from app.stats_charts import (  # noqa: E402,F401
    _build_completed_length_distribution_chart,
    _build_media_charts,
    _build_release_year_chart,
    _build_single_series_chart,
    _compute_metric_breakdown,
    _format_hour_label,
)
from app.stats_activity import (  # noqa: E402,F401
    _convert_chart_to_day_minutes,
    calculate_day_of_week_stats,
    calculate_most_active_weekday,
    calculate_streak_details,
    calculate_streaks,
    get_activity_data,
    get_aligned_week_start,
    get_filtered_historical_data,
    get_level,
    get_timeline,
    time_line_sort_key,
)
from app.stats_time import (  # noqa: E402,F401
    _calculate_anime_time,
    _calculate_episode_time_from_cache,
    _calculate_episode_time_from_data,
    _calculate_game_time_in_range,
    _calculate_movie_time,
    _calculate_music_time,
    _calculate_tv_time,
    _get_anime_runtime_from_cache,
    _get_media_metadata_for_statistics,
    _get_media_runtime_from_cache,
    _get_music_runtime_minutes,
    _get_season_metadata,
    _get_season_metadata_with_episodes,
    _is_episode_in_range,
    calculate_minutes_per_media_type,
    get_hours_per_media_type,
)
from app.stats_score import (  # noqa: E402,F401
    _annotate_top_rated_media,
    get_score_distribution,
)
from app.stats_podcast import (  # noqa: E402,F401
    _collect_podcast_play_data,
    _compute_podcast_top_lists,
    _get_podcast_history_data,
    _get_podcast_runtime_minutes,
    get_podcast_consumption_stats,
)
from app.stats_reading import (  # noqa: E402,F401
    _build_reading_top_authors,
    _build_weighted_media_charts,
    _extract_cached_item_authors,
    _extract_item_authors,
    _fetch_reading_items_with_authors,
    _format_reading_unit,
    _normalize_item_author_names,
    _reading_entry_in_range,
    get_reading_consumption_stats,
)
from app.stats_game import (  # noqa: E402,F401
    DAILY_AVERAGE_BANDS,
    _build_daily_average_band_top_games,
    _build_daily_average_distribution_chart,
    _build_game_hours_charts,
    _collect_game_data,
    _collect_game_play_data,
    _compute_game_platform_breakdown,
    _compute_game_top_daily_average,
    _compute_game_top_genres,
    _game_entry_in_range,
    _get_daily_average_band_index,
    get_game_consumption_stats,
)
from app.stats_music import (  # noqa: E402,F401
    COUNTRY_NAME_MAP,
    _collect_music_play_data,
    _compute_music_top_lists,
    _compute_music_top_rollups,
    _country_name_from_code,
    _hydrate_music_metadata_for_rollups,
    _parse_release_date_str,
    get_music_consumption_stats,
)
