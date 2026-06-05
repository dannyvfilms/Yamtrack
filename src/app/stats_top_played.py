"""stats_top_played.py — Top played media ranking by total time spent."""
import logging

from app.helpers import minutes_to_hhmm
from app.stats_time import (
    _calculate_anime_time,
    _calculate_game_time_in_range,
    _calculate_movie_time,
    _calculate_music_time,
    _calculate_tv_time,
)
from app.stats_utils import _iter_media_list

logger = logging.getLogger(__name__)


def get_top_played_media(user_media, start_date, end_date):
    """Get top played media by total time spent within date range.

    Returns a dictionary with media types as keys and lists of top media items.
    Each media item includes total_time_minutes, formatted_duration, and episode_count.
    """
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
