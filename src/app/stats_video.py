"""stats_video.py — Consumption stats for TV, movie, and anime.

Follows the same pattern as stats_podcast.py, stats_game.py, etc.:
private collectors/helpers at the top, one public get_*_consumption_stats()
per media type at the bottom.
"""
import logging
from collections import defaultdict

from app import config
from app.models import MediaTypes
from app.statistics_cache import STATISTICS_TOP_N
from app.stats_charts import _build_media_charts, _compute_metric_breakdown
from app.stats_time import (
    _get_media_metadata_for_statistics,
    _get_media_runtime_from_cache,
    _is_episode_in_range,
    calculate_minutes_per_media_type,
)
from app.stats_utils import (
    _coerce_genre_list,
    _get_activity_datetime,
    _iter_media_list,
    _localize_datetime,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Play-data collection helpers
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
# Genre aggregation
# ---------------------------------------------------------------------------

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
                import logging  # noqa: PLC0415
                _g_logger = logging.getLogger(__name__)
                _g_logger.debug("Skipping genre calculation for %s: %s", getattr(media_to_use.item, "title", "unknown"), e)
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
