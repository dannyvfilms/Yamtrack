"""stats_daily_hours.py — Stacked bar chart of hours by media type per day."""
import datetime
import logging

from django.utils import timezone

from app import config
from app.models import MediaTypes
from app.templatetags import app_tags
from app.stats_podcast import _collect_podcast_play_data, _get_podcast_history_data
from app.stats_time import (
    _calculate_episode_time_from_cache,
    _get_anime_runtime_from_cache,
    _get_media_runtime_from_cache,
    _get_music_runtime_minutes,
)
from app.stats_utils import (
    MEDIA_TYPE_HOURS_ORDER,
    _get_activity_datetime,
    _infer_user_from_user_media,
    _iter_media_list,
    _localize_datetime,
)

logger = logging.getLogger(__name__)


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
