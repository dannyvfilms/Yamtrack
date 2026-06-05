"""Functions that build per-entry history dicts for each media type."""

import logging

from django.conf import settings
from django.utils import formats

from app import helpers
from app.models import MediaTypes

from app.history_cache_utils import (
    _coerce_genre_list,
    _localize_datetime,
    _resolve_genres,
    _resolve_music_genres,
)

logger = logging.getLogger(__name__)


# ── Model serializers ─────────────────────────────────────────────────────────


def _serialize_item(item):
    if not item:
        return None
    if isinstance(item, dict):
        data = dict(item)
        if data.get("season_number") is None:
            data.pop("season_number", None)
        if data.get("episode_number") is None:
            data.pop("episode_number", None)
        return data
    if not hasattr(item, "media_type"):
        return None
    data = {
        "id": getattr(item, "id", None),
        "media_type": item.media_type,
        "media_id": str(getattr(item, "media_id", "")) if getattr(item, "media_id", None) is not None else None,
        "source": getattr(item, "source", None),
        "title": getattr(item, "title", "") or "",
        "original_title": getattr(item, "original_title", None),
        "localized_title": getattr(item, "localized_title", None),
    }
    season_number = getattr(item, "season_number", None)
    if season_number is not None:
        data["season_number"] = season_number
    episode_number = getattr(item, "episode_number", None)
    if episode_number is not None:
        data["episode_number"] = episode_number
    genres = _coerce_genre_list(getattr(item, "genres", None))
    if genres:
        data["genres"] = genres
    provider_external_ids = getattr(item, "provider_external_ids", None)
    if provider_external_ids:
        data["provider_external_ids"] = dict(provider_external_ids)
    return data


def _serialize_album(album):
    if not album:
        return None
    if isinstance(album, dict):
        return album
    return {
        "id": getattr(album, "id", None),
        "title": getattr(album, "title", "") or "",
        "image": getattr(album, "image", "") or "",
        "artist_name": getattr(getattr(album, "artist", None), "name", "") or "",
    }


def _attach_entry_score(entry, media):
    """Attach a media score to a history card entry when available."""
    if not entry or not media or entry.get("score") is not None:
        return entry
    score = getattr(media, "score", None)
    if score is not None:
        entry["score"] = score
    return entry


def _serialize_show(show):
    if not show:
        return None
    if isinstance(show, dict):
        return show
    return {
        "id": getattr(show, "id", None),
        "title": getattr(show, "title", "") or "",
        "slug": getattr(show, "slug", "") or "",
        "podcast_uuid": getattr(show, "podcast_uuid", None),
        "image": getattr(show, "image", "") or "",
    }


# ── Runtime / formatting helpers ──────────────────────────────────────────────


def _resolve_runtime_minutes(*items):
    """Pick the first usable runtime value from the provided items."""
    for item in items:
        if not item:
            continue
        runtime = getattr(item, "runtime_minutes", None)
        # Exclude fallback values: 999998 (aired but runtime unknown) and 999999 (unknown runtime)
        if runtime and runtime < 999998:
            return runtime
    return 0


def _format_game_hours(minutes: int) -> str:
    """Show hours only if at least 1h, otherwise keep minutes."""
    minutes = minutes or 0
    if minutes >= 60:
        return f"{minutes // 60}h"
    return f"{minutes}min"


def _format_boardgame_plays(plays: int) -> str:
    """Return a play-count label."""
    plays = plays or 0
    return f"{plays} play{'s' if plays != 1 else ''}"


# ── Episode / movie builders ──────────────────────────────────────────────────


def _get_episode_poster(episode):
    """Prefer show/season posters over episodic stills for consistent cards."""
    season_item = getattr(episode.related_season, "item", None)
    episode_item = getattr(episode, "item", None)
    tv_item = getattr(getattr(episode.related_season, "related_tv", None), "item", None)

    poster = (
        getattr(tv_item, "image", None)
        or getattr(season_item, "image", None)
        or getattr(episode.item, "image", None)
        or settings.IMG_NONE
    )

    return poster


def _get_episode_display_title(episode, episode_title_map=None):
    """Derive a best-effort episode title from local data only."""
    episode_item = getattr(episode, "item", None)
    season_item = getattr(episode.related_season, "item", None)

    key = None
    if episode_item:
        key = (
            getattr(episode_item, "media_id", None),
            getattr(episode_item, "source", None),
            getattr(episode_item, "season_number", None),
            getattr(episode_item, "episode_number", None),
        )

    if episode_title_map and key in episode_title_map:
        title_candidate = episode_title_map.get(key)
        if title_candidate:
            return title_candidate
    # Prefer the stored episode title if present.
    if episode_item and episode_item.title:
        return episode_item.title
    if season_item and season_item.title:
        return season_item.title
    return ""


def _build_episode_entry(episode, episode_title_map=None):
    played_at_local = _localize_datetime(episode.end_date or episode.created_at)
    if not played_at_local:
        return None

    episode_item = getattr(episode, "item", None)
    season_item = getattr(episode.related_season, "item", None)
    tv_item = getattr(getattr(episode.related_season, "related_tv", None), "item", None)
    entry_item = episode_item or season_item or tv_item
    if not entry_item:
        return None
    runtime_minutes = _resolve_runtime_minutes(
        episode.item,
        season_item,
        tv_item,
    )
    genres = _resolve_genres(episode_item, season_item, tv_item)

    display_title = _get_episode_display_title(episode, episode_title_map)
    title = ""
    if episode_item and episode_item.title:
        title = episode_item.title
    elif season_item and season_item.title:
        title = season_item.title
    elif tv_item and tv_item.title:
        title = tv_item.title

    episode_label = None
    episode_code = None
    if episode_item and episode_item.season_number is not None and episode_item.episode_number is not None:
        episode_label = f"{episode_item.season_number}x{episode_item.episode_number:02d}"
        episode_code = f"S{episode_item.season_number:02d}E{episode_item.episode_number:02d}"

    entry = {
        "media_type": MediaTypes.EPISODE.value,
        "item": _serialize_item(entry_item),
        "poster": _get_episode_poster(episode),
        "title": title,
        "display_title": display_title,
        "episode_label": episode_label,
        "episode_code": episode_code,
        "played_at_local": played_at_local,
        "runtime_minutes": runtime_minutes,
        "runtime_display": helpers.minutes_to_hhmm(runtime_minutes) if runtime_minutes else None,
        "instance_id": episode.id,
        "entry_key": episode.id,
    }
    _attach_entry_score(entry, episode)
    if genres:
        entry["genres"] = genres
    return entry


def _build_movie_entry(movie):
    played_at_local = _localize_datetime(movie.end_date or movie.start_date or movie.created_at)
    if not played_at_local:
        return None

    runtime_minutes = _resolve_runtime_minutes(movie.item)
    genres = _resolve_genres(movie.item)

    entry = {
        "media_type": MediaTypes.MOVIE.value,
        "item": _serialize_item(movie.item),
        "poster": movie.item.image or settings.IMG_NONE,
        "title": movie.item.title,
        "display_title": movie.item.title,
        "status": movie.status,
        "play_count": 1,
        "episode_label": None,
        "episode_code": None,
        "played_at_local": played_at_local,
        "runtime_minutes": runtime_minutes,
        "runtime_display": helpers.minutes_to_hhmm(runtime_minutes) if runtime_minutes else None,
        "instance_id": movie.id,
        "entry_key": movie.id,
    }
    _attach_entry_score(entry, movie)
    if genres:
        entry["genres"] = genres
    return entry


# ── Music builders ────────────────────────────────────────────────────────────


def _get_music_runtime_minutes(music_entry, track_duration_cache=None):
    """Get runtime in minutes from a Music entry, checking track and item.

    Args:
        music_entry: The Music model instance
        track_duration_cache: Optional dict with two types of keys:
            - (album_id, track_title) -> duration_ms
            - ("recording", recording_id) -> duration_ms
    """
    # First try the linked Track's duration_ms
    if music_entry.track and music_entry.track.duration_ms:
        return music_entry.track.duration_ms // 60000  # ms to minutes

    # Fall back to item runtime_minutes
    if music_entry.item and music_entry.item.runtime_minutes:
        return music_entry.item.runtime_minutes

    # Try to look up duration from cache (built from album tracklist)
    if track_duration_cache and music_entry.item:
        # Try matching by title (if we have album)
        if music_entry.album_id:
            title_key = (music_entry.album_id, music_entry.item.title)
            duration_ms = track_duration_cache.get(title_key)
            if duration_ms:
                return duration_ms // 60000

        # Try matching by recording ID (item.media_id is the MusicBrainz recording ID)
        if music_entry.item.media_id:
            recording_key = ("recording", music_entry.item.media_id)
            duration_ms = track_duration_cache.get(recording_key)
            if duration_ms:
                return duration_ms // 60000

    return 0


def _build_music_album_entries(music_entries_for_album, album, day_date, user, track_duration_cache=None, album_scores=None):
    """Build a single history entry for an album's plays on a given day.

    Groups all track plays for an album on a day into one card showing:
    - Album poster
    - Play count (sum of plays that day from history records)
    - Album name
    - Time range (earliest to latest play time)
    - Total runtime
    - Album rating (if available)
    """
    if not music_entries_for_album:
        return None

    # Collect all play times and runtimes for this album on this day
    # We need to count history records, not Music entries, since a track
    # played twice creates 2 history records on the same Music entry
    play_times = []
    total_runtime_minutes = 0
    play_count = 0
    latest_play_time = None
    primary_music = None

    for music in music_entries_for_album:
        runtime_for_track = _get_music_runtime_minutes(music, track_duration_cache)

        # Check history records for plays on this day
        # Each history record with an end_date on this day counts as a play
        for history_record in music.history.all():
            # Only include history records for this user (or null history_user for legacy records)
            history_user = getattr(history_record, "history_user", None)
            if history_user is not None and history_user != user:
                continue

            history_end_date = getattr(history_record, "end_date", None)
            if history_end_date:
                play_time = _localize_datetime(history_end_date)
                if play_time and play_time.date() == day_date:
                    play_times.append(play_time)
                    play_count += 1
                    total_runtime_minutes += runtime_for_track
                    if latest_play_time is None or play_time > latest_play_time:
                        latest_play_time = play_time
                        primary_music = music

    if not play_times:
        return None

    play_times.sort()
    earliest_time = play_times[0]
    latest_time = play_times[-1]

    # Format time range (just times, no date)
    if len(play_times) == 1:
        time_range_display = formats.time_format(earliest_time, "g:i A")
    else:
        time_range_display = f"{formats.time_format(earliest_time, 'g:i A')} - {formats.time_format(latest_time, 'g:i A')}"

    # Get album poster
    poster = settings.IMG_NONE
    if album and album.image:
        poster = album.image

    # Album name
    album_name = album.title if album else "Unknown Album"
    artist_name = album.artist.name if album and album.artist else "Unknown Artist"

    entry_item = primary_music.item if primary_music and primary_music.item else None
    instance_id = primary_music.id if primary_music else None
    track = getattr(primary_music, "track", None) if primary_music else None
    genres = _resolve_music_genres(album=album, artist=album.artist if album else None, track=track)
    entry_key = f"{album.id if album else 'album'}-{day_date.strftime('%Y%m%d')}"

    # Get album score if available
    album_score = None
    if album_scores and album and album.id:
        album_score = album_scores.get(album.id)

    entry = {
        "media_type": MediaTypes.MUSIC.value,
        "item": _serialize_item(entry_item),
        "album": _serialize_album(album),
        "poster": poster,
        "title": album_name,
        "display_title": album_name,
        "artist_name": artist_name,
        "play_count": play_count,
        "time_range_display": time_range_display,
        "episode_label": None,
        "episode_code": None,
        "played_at_local": latest_time,  # Use latest play for sorting
        "runtime_minutes": total_runtime_minutes,
        "runtime_display": helpers.minutes_to_hhmm(total_runtime_minutes) if total_runtime_minutes else None,
        "instance_id": instance_id,
        "entry_key": entry_key,
        "score": album_score,  # Album tracker score
    }
    if genres:
        entry["genres"] = genres
    return entry
