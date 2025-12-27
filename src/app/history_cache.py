"""Utilities for caching the History page."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import models
from django.utils import formats, timezone

from app import helpers
from app.models import Album, BoardGame, Episode, Game, Item, MediaTypes, Movie, Music, Podcast, Track

logger = logging.getLogger(__name__)

HISTORY_CACHE_PREFIX = "history_page_v13"
HISTORY_CACHE_TIMEOUT = 60 * 60 * 6  # 6 hours
HISTORY_STALE_AFTER = timedelta(minutes=15)
HISTORY_DAYS_PER_PAGE = 30
HISTORY_REFRESH_LOCK_PREFIX = f"{HISTORY_CACHE_PREFIX}_refresh_lock"
HISTORY_REFRESH_LOCK_MAX_AGE = timedelta(minutes=5)  # safety to clear stuck locks


def _cache_key(user_id: int, logging_style: str) -> str:
    return f"{HISTORY_CACHE_PREFIX}_{user_id}_{logging_style or 'repeats'}"


def _refresh_lock_key(user_id: int, logging_style: str) -> str:
    return f"{HISTORY_REFRESH_LOCK_PREFIX}_{user_id}_{logging_style or 'repeats'}"


def _localize_datetime(value):
    """Convert a datetime to the current timezone if possible."""
    if value is None:
        return None

    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())

    return timezone.localtime(value)


def _resolve_runtime_minutes(*items):
    """Pick the first usable runtime value from the provided items."""
    for item in items:
        if not item:
            continue

        runtime = getattr(item, "runtime_minutes", None)
        if runtime and runtime < 999999:
            return runtime

    return 0


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


def _build_episode_entry(episode, episode_title_map=None, episode_history_map=None):
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

    episode_history = []
    if episode_history_map and episode_item and episode_item.episode_number is not None:
        history_key = (episode.related_season_id, episode_item.episode_number)
        episode_history = episode_history_map.get(history_key, [])

    episode_image = episode_item.image if episode_item and episode_item.image else _get_episode_poster(episode)
    episode_modal = {
        "title": title,
        "image": episode_image or settings.IMG_NONE,
        "episode_number": episode_item.episode_number if episode_item else None,
        "air_date": None,
        "history": episode_history,
    }

    return {
        "media_type": MediaTypes.EPISODE.value,
        "item": entry_item,
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
        "episode_modal": episode_modal,
    }


def _build_movie_entry(movie):
    played_at_local = _localize_datetime(movie.end_date or movie.start_date or movie.created_at)
    if not played_at_local:
        return None

    runtime_minutes = _resolve_runtime_minutes(movie.item)

    return {
        "media_type": MediaTypes.MOVIE.value,
        "item": movie.item,
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


def _build_music_album_entries(music_entries_for_album, album, day_date, user, track_duration_cache=None):
    """Build a single history entry for an album's plays on a given day.
    
    Groups all track plays for an album on a day into one card showing:
    - Album poster
    - Play count (sum of plays that day from history records)
    - Album name
    - Time range (earliest to latest play time)
    - Total runtime
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
    
    entry_item = primary_music.item if primary_music and primary_music.item else album
    instance_id = primary_music.id if primary_music else None
    entry_key = f"{album.id if album else 'album'}-{day_date.strftime('%Y%m%d')}"

    return {
        "media_type": MediaTypes.MUSIC.value,
        "item": entry_item,
        "album": album,
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
    }


def build_history_days(user, filters=None, date_filters=None, logging_style_override=None):
    """Build the list of grouped history entries for a user.
    
    Args:
        user: User instance
        filters: Optional dict of filter parameters:
            - album: Filter music entries by album_id
            - artist: Filter music entries by album__artist_id
            - tv: Filter episodes by related_season__related_tv_id
            - season: Filter episodes by related_season_id
            - media_id: Filter entries by item media_id
            - source: Filter entries by item source
            - season_number: Filter episodes by season number (requires media_id/source)
            - podcast_show: Filter podcast plays by show id
            - genre: Filter by genre name (string)
            - media_type: Filter by media type (string: 'movie', 'tv', 'music', etc.)
        date_filters: Optional dict with 'start_date' and 'end_date' (date strings)
        logging_style_override: Optional override for game logging style ("sessions" or "repeats")
    """
    filters = filters or {}
    date_filters = date_filters or {}
    
    # Parse date filters
    start_date = None
    end_date = None
    if date_filters.get('start_date'):
        from django.utils.dateparse import parse_date
        from django.utils import timezone as tz
        parsed = parse_date(date_filters['start_date'])
        if parsed:
            start_date = tz.make_aware(datetime.combine(parsed, datetime.min.time()))
    if date_filters.get('end_date'):
        from django.utils.dateparse import parse_date
        from django.utils import timezone as tz
        parsed = parse_date(date_filters['end_date'])
        if parsed:
            end_date = tz.make_aware(datetime.combine(parsed, datetime.max.time()))
    if logging_style_override not in ("sessions", "repeats"):
        logging_style_override = None
    game_logging_style = logging_style_override or getattr(user, "game_logging_style", "repeats")

    media_type_filter = filters.get('media_type')
    target_media_id = filters.get('media_id')
    target_source = filters.get('source')
    season_number_filter = filters.get('season_number')
    podcast_show_filter = filters.get('podcast_show')
    if target_media_id is not None:
        target_media_id = str(target_media_id)
    if target_source is not None:
        target_source = str(target_source)

    episodes = (
        Episode.objects.filter(
            related_season__user=user,
            end_date__isnull=False,
        )
        .select_related(
            "item",
            "related_season__item",
            "related_season__related_tv__item",
        )
        .order_by("-end_date")
    )
    
    # Apply date range filter to episodes
    if start_date:
        episodes = episodes.filter(end_date__gte=start_date)
    if end_date:
        episodes = episodes.filter(end_date__lte=end_date)
    
    # Apply episode filters
    if filters.get('tv'):
        episodes = episodes.filter(related_season__related_tv_id=filters['tv'])
    if filters.get('season'):
        episodes = episodes.filter(related_season_id=filters['season'])
    if target_media_id and target_source and (
        media_type_filter == MediaTypes.TV.value
        or filters.get('tv')
        or filters.get('season')
        or season_number_filter is not None
    ):
        episodes = episodes.filter(
            related_season__related_tv__item__media_id=target_media_id,
            related_season__related_tv__item__source=target_source,
        )
        if season_number_filter is not None:
            episodes = episodes.filter(related_season__item__season_number=season_number_filter)

    episodes = list(episodes)
    episode_history_map = defaultdict(list)
    for ep in episodes:
        ep_item = getattr(ep, "item", None)
        if not ep_item or ep_item.episode_number is None:
            continue
        history_key = (ep.related_season_id, ep_item.episode_number)
        episode_history_map[history_key].append(ep)

    movies_qs = Movie.objects.filter(
        user=user,
    ).filter(
        models.Q(end_date__isnull=False) | models.Q(start_date__isnull=False),
    ).select_related("item")
    
    # Apply date range filter to movies
    if start_date:
        movies_qs = movies_qs.filter(
            models.Q(end_date__gte=start_date)
            | (models.Q(end_date__isnull=True) & models.Q(start_date__gte=start_date))
        )
    if end_date:
        movies_qs = movies_qs.filter(
            models.Q(end_date__lte=end_date)
            | (models.Q(end_date__isnull=True) & models.Q(start_date__lte=end_date))
        )
    if target_media_id and target_source and media_type_filter == MediaTypes.MOVIE.value:
        movies_qs = movies_qs.filter(
            item__media_id=target_media_id,
            item__source=target_source,
        )

    movies = movies_qs.order_by("-end_date")
    movie_play_counts = (
        movies_qs.values("item__media_id", "item__source")
        .annotate(play_count=models.Count("id"))
        .order_by()
    )
    movie_play_map = {
        (row["item__media_id"], row["item__source"]): row["play_count"]
        for row in movie_play_counts
    }
    games = (
        Game.objects.filter(user=user)
        .select_related("item")
        .order_by("-end_date", "-created_at")
    )
    boardgames = (
        BoardGame.objects.filter(user=user)
        .select_related("item")
        .order_by("-end_date", "-created_at")
    )
    if target_media_id and target_source:
        if media_type_filter == MediaTypes.GAME.value:
            games = games.filter(item__media_id=target_media_id, item__source=target_source)
        if media_type_filter == MediaTypes.BOARDGAME.value:
            boardgames = boardgames.filter(item__media_id=target_media_id, item__source=target_source)
    
    # Music - query all music entries with end_date
    music_entries = (
        Music.objects.filter(
            user=user,
            end_date__isnull=False,
        )
        .select_related("item", "album", "album__artist", "track")
        .order_by("-end_date")
    )
    
    # Apply date range filter to music (filter by end_date in history records)
    # Note: Music entries have end_date directly, but we need to check history records
    # For now, we'll filter after processing since music uses history records for grouping
    
    # Apply music filters
    if filters.get('album'):
        music_entries = music_entries.filter(album_id=filters['album'])
    if filters.get('artist'):
        music_entries = music_entries.filter(album__artist_id=filters['artist'])
    if target_media_id and target_source and media_type_filter == MediaTypes.MUSIC.value:
        music_entries = music_entries.filter(
            item__media_id=target_media_id,
            item__source=target_source,
        )
    
    # Podcasts - query history records directly to ensure deleted records don't show up
    # Query HistoricalPodcast directly, filtering by user and end_date at database level
    from django.apps import apps
    HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")
    
    # Get all podcast history records for this user with end_date
    # Filter by history_user at database level to match template behavior
    podcast_history_records = (
        HistoricalPodcast.objects.filter(
            models.Q(history_user=user) | models.Q(history_user__isnull=True),
            end_date__isnull=False,
        )
        .order_by("-end_date")
    )
    
    # Apply date range filter to podcasts
    if start_date:
        podcast_history_records = podcast_history_records.filter(end_date__gte=start_date)
    if end_date:
        podcast_history_records = podcast_history_records.filter(end_date__lte=end_date)
    if podcast_show_filter:
        podcast_history_records = podcast_history_records.filter(show_id=podcast_show_filter)
    
    # Get unique podcast IDs from history records to fetch podcast metadata
    podcast_ids = list(set(podcast_history_records.values_list("id", flat=True)))
    if podcast_ids:
        podcasts_lookup = {
            p.id: p
            for p in Podcast.objects.filter(
                id__in=podcast_ids,
                user=user,
            )
            .select_related("item", "episode", "episode__show", "show")
        }
    else:
        podcasts_lookup = {}
    
    if (
        target_media_id
        and target_source
        and media_type_filter == MediaTypes.PODCAST.value
        and not podcast_show_filter
    ):
        podcast_history_records = [
            record
            for record in podcast_history_records
            if (
                (podcast := podcasts_lookup.get(record.id))
                and podcast.item
                and str(podcast.item.media_id) == target_media_id
                and str(podcast.item.source) == target_source
            )
        ]

    entries = []

    # Determine which media types to process based on filters
    # If filtering by music (album/artist), only process music
    # If filtering by TV (tv/season), only process episodes
    # If filtering by media_type, only process that type
    # Otherwise, process all media types
    has_music_filter = bool(filters.get('album') or filters.get('artist'))
    has_tv_filter = bool(filters.get('tv') or filters.get('season') or season_number_filter is not None)
    has_podcast_filter = bool(podcast_show_filter)
    process_all = not (has_music_filter or has_tv_filter or has_podcast_filter or media_type_filter)
    
    # Helper function to check if entry matches genre filter by checking metadata.
    # Uses a cache to avoid repeated metadata lookups for the same media item.
    genre_filter = filters.get('genre')
    genre_cache = {}  # Cache: (media_type, media_id) -> bool (matches genre or None if not checked)
    
    def matches_genre(media_entry, media_type):
        """Check if media entry matches genre filter by checking metadata."""
        if not genre_filter:
            return True
        
        # For TV episodes, use the parent TV show for caching
        cache_key = None
        if media_type == MediaTypes.EPISODE.value and hasattr(media_entry, 'related_season'):
            if hasattr(media_entry.related_season, 'related_tv') and media_entry.related_season.related_tv:
                tv_show = media_entry.related_season.related_tv
                if hasattr(tv_show, 'item') and tv_show.item:
                    cache_key = (MediaTypes.TV.value, tv_show.item.media_id, tv_show.item.source)
        elif hasattr(media_entry, 'item') and media_entry.item:
            cache_key = (media_type, media_entry.item.media_id, media_entry.item.source)
        
        # Check cache first
        if cache_key and cache_key in genre_cache:
            return genre_cache[cache_key] is True
        
        try:
            from app.statistics import _get_media_metadata_for_statistics, _coerce_genre_list
            
            # For TV episodes, get genres from parent TV show
            if media_type == MediaTypes.EPISODE.value and hasattr(media_entry, 'related_season'):
                if hasattr(media_entry.related_season, 'related_tv') and media_entry.related_season.related_tv:
                    tv_show = media_entry.related_season.related_tv
                    metadata = _get_media_metadata_for_statistics(tv_show)
                else:
                    metadata = None
            else:
                metadata = _get_media_metadata_for_statistics(media_entry)
            
            if not metadata:
                if cache_key:
                    genre_cache[cache_key] = False
                return False
            
            # Extract genres from metadata
            genres = []
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
            
            # Check if any genre matches (case-insensitive)
            genre_filter_lower = genre_filter.lower()
            matches = any(str(genre).lower() == genre_filter_lower for genre in genres)
            
            # Cache the result
            if cache_key:
                genre_cache[cache_key] = matches
            
            return matches
        except Exception as e:
            logger.debug(f"Error checking genre for {media_entry}: {e}")
            if cache_key:
                genre_cache[cache_key] = False
            return False  # Skip if we can't check genre

    # Build a lookup of episode titles from stored items to avoid provider calls
    # Only if we're processing episodes
    if process_all or has_tv_filter or media_type_filter == MediaTypes.TV.value:
        episode_keys = []
        for ep in episodes:
            ep_item = getattr(ep, "item", None)
            if not ep_item:
                continue
            episode_keys.append(
                (
                    getattr(ep_item, "media_id", None),
                    getattr(ep_item, "source", None),
                    getattr(ep_item, "season_number", None),
                    getattr(ep_item, "episode_number", None),
                ),
            )

        episode_keys = [key for key in episode_keys if all(key)]
        episode_title_map = {}
        if episode_keys:
            media_ids = {k[0] for k in episode_keys}
            sources = {k[1] for k in episode_keys}
            season_numbers = {k[2] for k in episode_keys}
            episode_numbers = {k[3] for k in episode_keys}

            titles_qs = Item.objects.filter(
                media_type=MediaTypes.EPISODE.value,
                media_id__in=media_ids,
                source__in=sources,
                season_number__in=season_numbers,
                episode_number__in=episode_numbers,
            ).exclude(title__isnull=True).exclude(title="")

            for item in titles_qs:
                key = (
                    item.media_id,
                    item.source,
                    item.season_number,
                    item.episode_number,
                )
                if key not in episode_title_map:
                    episode_title_map[key] = item.title

        for episode in episodes:
            # Apply genre filter if specified
            if genre_filter and not matches_genre(episode, MediaTypes.EPISODE.value):
                continue
            entry = _build_episode_entry(episode, episode_title_map, episode_history_map)
            if entry:
                entries.append(entry)

    # Process movies only if not filtering by specific media type or if filtering by movie
    if process_all or media_type_filter == MediaTypes.MOVIE.value:
        for movie in movies:
            # Apply genre filter if specified
            if genre_filter and not matches_genre(movie, MediaTypes.MOVIE.value):
                continue
            entry = _build_movie_entry(movie)
            if not entry:
                continue
            key = (movie.item.media_id, movie.item.source)
            annotated = movie_play_map.get(key)
            repeat_attr = getattr(movie, "repeats", None)
            play_count = annotated or repeat_attr or 1
            entry["play_count"] = play_count
            entries.append(entry)

    # Process music entries (always process if filtering by music, or if processing all)
    if process_all or has_music_filter or media_type_filter == MediaTypes.MUSIC.value:
        # Music - group by album and day based on history records
        # Each history record represents a play, so we need to find all days
        # where any track from an album was played
        music_by_album_day = defaultdict(list)
        album_lookup = {}
        
        for music in music_entries:
            album_id = music.album_id if music.album else None
            if album_id and music.album:
                album_lookup[album_id] = music.album
            
            # Find all days this track was played by checking history records
            days_played = set()
            for history_record in music.history.all():
                # Only include history records for this user (or null history_user for legacy records)
                history_user = getattr(history_record, "history_user", None)
                if history_user is not None and history_user != user:
                    continue
                
                history_end_date = getattr(history_record, "end_date", None)
                if history_end_date:
                    # Apply date range filter
                    if start_date and history_end_date < start_date:
                        continue
                    if end_date and history_end_date > end_date:
                        continue
                    
                    play_time = _localize_datetime(history_end_date)
                    if play_time:
                        days_played.add(play_time.date())
            
            # Add this Music entry to each day it was played
            for day_date in days_played:
                key = (album_id, day_date)
                if music not in music_by_album_day[key]:
                    music_by_album_day[key].append(music)
        
        # Build a cache of track durations from album tracklists for fallback
        # This helps get runtime for Music entries that don't have Track linked
        # Cache has two types of keys:
        # - (album_id, track_title) -> duration_ms
        # - ("recording", musicbrainz_recording_id) -> duration_ms
        track_duration_cache = {}
        album_ids_with_music = list(album_lookup.keys())
        if album_ids_with_music:
            tracks_qs = Track.objects.filter(
                album_id__in=album_ids_with_music,
                duration_ms__isnull=False,
            ).values("album_id", "title", "duration_ms", "musicbrainz_recording_id")
            for track_data in tracks_qs:
                # Key by album + title
                title_key = (track_data["album_id"], track_data["title"])
                track_duration_cache[title_key] = track_data["duration_ms"]
                # Also key by recording ID for fallback
                if track_data["musicbrainz_recording_id"]:
                    recording_key = ("recording", track_data["musicbrainz_recording_id"])
                    track_duration_cache[recording_key] = track_data["duration_ms"]
        
        # Now build one entry per album per day
        for (album_id, day_date), album_music_entries in music_by_album_day.items():
            album = album_lookup.get(album_id)
            
            # Apply genre filter if specified - check album or artist genres
            if genre_filter and album:
                from app.statistics import _coerce_genre_list
                # Check album genres first, then artist genres
                album_genres = _coerce_genre_list(album.genres) if album.genres else []
                artist_genres = []
                if album.artist and album.artist.genres:
                    artist_genres = _coerce_genre_list(album.artist.genres)
                
                all_genres = album_genres + artist_genres
                genre_filter_lower = genre_filter.lower()
                genre_match = any(str(g).lower() == genre_filter_lower for g in all_genres)
                if not genre_match:
                    continue
            
            entry = _build_music_album_entries(album_music_entries, album, day_date, user, track_duration_cache)
            if entry:
                entries.append(entry)

    # Podcasts - process when showing all media or filtering to podcasts
    # Query history records directly to ensure deleted records don't show up
    if process_all or has_podcast_filter or media_type_filter == MediaTypes.PODCAST.value:
        # Count podcast plays by (media_id, source) similar to movies
        # Group history records by podcast item to count total plays per episode
        podcast_play_counts = {}
        for history_record in podcast_history_records:
            podcast = podcasts_lookup.get(history_record.id)
            if not podcast or not podcast.item:
                continue
            key = (podcast.item.media_id, podcast.item.source)
            podcast_play_counts[key] = podcast_play_counts.get(key, 0) + 1
        
        for history_record in podcast_history_records:
            # Get the podcast instance for metadata
            podcast = podcasts_lookup.get(history_record.id)
            if not podcast:
                # Podcast was deleted, skip this history record
                continue
            
            # Skip if podcast doesn't have required data
            if not podcast.item:
                logger.warning("Skipping podcast entry %s without item", podcast.id)
                continue
            
            try:
                history_end_date = getattr(history_record, "end_date", None)
                if not history_end_date:
                    continue
                
                played_at_local = _localize_datetime(history_end_date)
                if not played_at_local:
                    continue
                
                # Get show - prefer episode.show (authoritative source), fallback to podcast.show
                show = None
                if podcast.episode:
                    show = podcast.episode.show
                if not show:
                    show = podcast.show
                
                # Get show URL components for navigation
                show_podcast_uuid = show.podcast_uuid if show else None
                # Use show.slug if available, otherwise use show.title for URL slug
                show_slug = show.slug if show and show.slug else (show.title if show else "")
                
                # Get poster - prefer show image, fallback to item image, then IMG_NONE
                poster = settings.IMG_NONE
                if show and show.image:
                    poster = show.image
                elif podcast.item.image:
                    poster = podcast.item.image
                
                # Progress is stored in minutes
                minutes_listened = podcast.progress or 0
                runtime_minutes = podcast.item.runtime_minutes if podcast.item.runtime_minutes else minutes_listened
                
                # Get play count for this episode (only counting completed records with end_date)
                key = (podcast.item.media_id, podcast.item.source)
                play_count = podcast_play_counts.get(key, 1)
                
                entries.append(
                    {
                        "media_type": MediaTypes.PODCAST.value,
                        "item": podcast.item,
                        "show": show,
                        "show_podcast_uuid": show_podcast_uuid,
                        "show_slug": show_slug,
                        "poster": poster,
                        "title": podcast.item.title,
                        "display_title": podcast.item.title,
                        "progress_display": f"{minutes_listened}m",
                        "date_range_display": None,
                        "episode_label": None,
                        "episode_code": None,
                        "played_at_local": played_at_local,
                        "runtime_minutes": runtime_minutes,
                        "runtime_display": helpers.minutes_to_hhmm(runtime_minutes) if runtime_minutes else None,
                        "play_count": play_count,
                        "instance_id": podcast.id,
                        "entry_key": history_record.history_id,
                    },
                )
            except Exception as e:
                logger.error("Error processing podcast history record %s: %s", history_record.history_id, e, exc_info=True)
                continue

    # Games - process when showing all media or filtering to games/board games
    process_games = process_all or media_type_filter == MediaTypes.GAME.value
    process_boardgames = process_all or media_type_filter == MediaTypes.BOARDGAME.value
    if process_games or process_boardgames:
        if game_logging_style == "sessions":
            if process_games:
                for game in games:
                    if not (game.start_date or game.end_date):
                        continue

                    activity_dt = game.end_date or game.start_date or game.created_at
                    played_at_local = _localize_datetime(activity_dt)
                    if not played_at_local:
                        continue
                    runtime_minutes = game.progress or 0
                    start_local = _localize_datetime(game.start_date).date() if game.start_date else None
                    end_local = _localize_datetime(game.end_date).date() if game.end_date else played_at_local.date()
                    if not start_local:
                        start_local = end_local
                    date_range_display = f"{formats.date_format(start_local, 'M j')} - {formats.date_format(end_local, 'M j')}"

                    entries.append(
                        {
                            "media_type": MediaTypes.GAME.value,
                            "item": game.item,
                            "poster": game.item.image or settings.IMG_NONE,
                            "title": game.item.title,
                            "display_title": game.item.title,
                            "progress_display": _format_game_hours(runtime_minutes),
                            "date_range_display": date_range_display,
                            "episode_label": None,
                            "episode_code": None,
                            "played_at_local": played_at_local,
                            "runtime_minutes": runtime_minutes,
                            "runtime_display": helpers.minutes_to_hhmm(runtime_minutes) if runtime_minutes else None,
                            "instance_id": game.id,
                            "entry_key": game.id,
                        },
                    )
            if process_boardgames:
                for boardgame in boardgames:
                    if not (boardgame.start_date or boardgame.end_date):
                        continue

                    activity_dt = boardgame.end_date or boardgame.start_date or boardgame.created_at
                    played_at_local = _localize_datetime(activity_dt)
                    if not played_at_local:
                        continue
                    plays = boardgame.progress or 0
                    start_local = _localize_datetime(boardgame.start_date).date() if boardgame.start_date else None
                    end_local = (
                        _localize_datetime(boardgame.end_date).date()
                        if boardgame.end_date
                        else played_at_local.date()
                    )
                    if not start_local:
                        start_local = end_local
                    date_range_display = f"{formats.date_format(start_local, 'M j')} - {formats.date_format(end_local, 'M j')}"

                    progress_display = _format_boardgame_plays(plays)

                    entries.append(
                        {
                            "media_type": MediaTypes.BOARDGAME.value,
                            "item": boardgame.item,
                            "poster": boardgame.item.image or settings.IMG_NONE,
                            "title": boardgame.item.title,
                            "display_title": boardgame.item.title,
                            "progress_display": progress_display,
                            "date_range_display": date_range_display,
                            "episode_label": None,
                            "episode_code": None,
                            "played_at_local": played_at_local,
                            "runtime_minutes": 0,
                            "runtime_display": progress_display,
                            "instance_id": boardgame.id,
                            "entry_key": boardgame.id,
                        },
                    )
        else:
            # repeats style: spread playtime evenly across date range
            if process_games:
                for game in games:
                    if not (game.start_date or game.end_date):
                        continue

                    total_minutes = game.progress or 0
                    if total_minutes <= 0:
                        continue

                    start_dt = game.start_date or game.end_date or game.created_at
                    end_dt = game.end_date or game.start_date or game.created_at
                    if not start_dt or not end_dt:
                        continue

                    start_local = _localize_datetime(start_dt).date()
                    end_local = _localize_datetime(end_dt).date()
                    if start_local > end_local:
                        start_local, end_local = end_local, start_local

                    day_count = (end_local - start_local).days + 1
                    if day_count <= 0:
                        day_count = 1

                    base = total_minutes // day_count
                    remainder = total_minutes % day_count
                    date_range_display = f"{formats.date_format(start_local, 'M j')} - {formats.date_format(end_local, 'M j')}"
                    total_progress_display = _format_game_hours(total_minutes)

                    for offset in range(day_count):
                        day = start_local + timedelta(days=offset)
                        minutes_for_day = base + (1 if offset < remainder else 0)
                        day_dt = timezone.make_aware(
                            datetime.combine(day, datetime.min.time()),
                            timezone.get_current_timezone(),
                        )
                        entries.append(
                            {
                                "media_type": MediaTypes.GAME.value,
                                "item": game.item,
                                "poster": game.item.image or settings.IMG_NONE,
                                "title": game.item.title,
                                "display_title": game.item.title,
                                "progress_display": total_progress_display,
                                "date_range_display": date_range_display,
                                "episode_label": None,
                                "episode_code": None,
                                "played_at_local": day_dt,
                                "runtime_minutes": minutes_for_day,
                                "runtime_display": helpers.minutes_to_hhmm(minutes_for_day) if minutes_for_day else None,
                                "instance_id": game.id,
                                "entry_key": f"{game.id}-{day.strftime('%Y%m%d')}",
                            },
                        )
            if process_boardgames:
                for boardgame in boardgames:
                    if not (boardgame.start_date or boardgame.end_date):
                        continue

                    total_plays = boardgame.progress or 0
                    if total_plays <= 0:
                        continue

                    start_dt = boardgame.start_date or boardgame.end_date or boardgame.created_at
                    end_dt = boardgame.end_date or boardgame.start_date or boardgame.created_at
                    if not start_dt or not end_dt:
                        continue

                    start_local = _localize_datetime(start_dt).date()
                    end_local = _localize_datetime(end_dt).date()
                    if start_local > end_local:
                        start_local, end_local = end_local, start_local

                    day_count = (end_local - start_local).days + 1
                    if day_count <= 0:
                        day_count = 1

                    base = total_plays // day_count
                    remainder = total_plays % day_count
                    date_range_display = f"{formats.date_format(start_local, 'M j')} - {formats.date_format(end_local, 'M j')}"
                    total_progress_display = _format_boardgame_plays(total_plays)

                    for offset in range(day_count):
                        day = start_local + timedelta(days=offset)
                        plays_for_day = base + (1 if offset < remainder else 0)
                        day_dt = timezone.make_aware(
                            datetime.combine(day, datetime.min.time()),
                            timezone.get_current_timezone(),
                        )
                        entries.append(
                            {
                                "media_type": MediaTypes.BOARDGAME.value,
                                "item": boardgame.item,
                                "poster": boardgame.item.image or settings.IMG_NONE,
                                "title": boardgame.item.title,
                                "display_title": boardgame.item.title,
                                "progress_display": total_progress_display,
                                "date_range_display": date_range_display,
                                "episode_label": None,
                                "episode_code": None,
                                "played_at_local": day_dt,
                                "runtime_minutes": 0,
                                "runtime_display": _format_boardgame_plays(plays_for_day) if plays_for_day else None,
                                "instance_id": boardgame.id,
                                "entry_key": f"{boardgame.id}-{day.strftime('%Y%m%d')}",
                            },
                        )

    entries.sort(key=lambda entry: entry["played_at_local"], reverse=True)

    grouped_entries = defaultdict(list)
    for entry in entries:
        grouped_entries[entry["played_at_local"].date()].append(entry)

    history_days = []
    for _, day_entries in sorted(
        grouped_entries.items(),
        key=lambda item: item[0],
        reverse=True,
    ):
        day_entries.sort(key=lambda entry: entry["played_at_local"], reverse=True)
        first_entry_time = day_entries[0]["played_at_local"]
        total_minutes = sum(entry["runtime_minutes"] or 0 for entry in day_entries)

        history_days.append(
            {
                "date": first_entry_time.date(),
                "weekday": formats.date_format(first_entry_time, "l"),
                "date_display": formats.date_format(first_entry_time, "F j, Y"),
                "entries": day_entries,
                "total_minutes": total_minutes,
                "total_runtime_display": helpers.minutes_to_hhmm(total_minutes)
                if total_minutes
                else "0min",
            },
        )

    return history_days


def cache_history_days(user_id: int, logging_style: str, history_days):
    """Persist the grouped history in cache."""
    cache.set(
        _cache_key(user_id, logging_style),
        {
            "history_days": history_days,
            "built_at": timezone.now(),
        },
        timeout=HISTORY_CACHE_TIMEOUT,
    )


def get_history_days(user, filters=None, date_filters=None, logging_style_override=None):
    """Return cached history, rebuilding if needed.
    
    Always returns cached data if available (even if stale) to avoid timeouts.
    Schedules background refresh if cache is stale.
    
    Args:
        user: User instance
        filters: Optional dict of filter parameters (album, artist, tv, season, genre, media_type, etc.)
                 When filters are provided, cache is bypassed and results are filtered.
        date_filters: Optional dict with 'start_date' and 'end_date' (date strings)
        logging_style_override: Optional override for game logging style ("sessions" or "repeats")
    """
    # If filters, date_filters, or logging override are provided, bypass cache and build filtered results directly
    if filters or date_filters or logging_style_override:
        return build_history_days(
            user,
            filters=filters,
            date_filters=date_filters,
            logging_style_override=logging_style_override,
        )
    
    logging_style = getattr(user, "game_logging_style", "repeats")
    cache_entry = cache.get(_cache_key(user.id, logging_style))
    refresh_lock = cache.get(_refresh_lock_key(user.id, logging_style))
    # Clean up stale/legacy locks so refresh can proceed
    if refresh_lock:
        # Legacy True/False locks (no metadata) or expired metadata get cleared
        if not isinstance(refresh_lock, dict):
            cache.delete(_refresh_lock_key(user.id, logging_style))
            refresh_lock = None
        else:
            started_at = refresh_lock.get("started_at")
            if started_at and timezone.now() - started_at > HISTORY_REFRESH_LOCK_MAX_AGE:
                cache.delete(_refresh_lock_key(user.id, logging_style))
                refresh_lock = None
    
    if cache_entry:
        # Always return cached data if it exists (even if stale)
        # This prevents timeouts while background refresh is in progress
        built_at = cache_entry.get("built_at")
        if built_at and timezone.now() - built_at > HISTORY_STALE_AFTER:
            # Check if a refresh is already in progress before scheduling a new one
            # This prevents re-scheduling a refresh immediately after one completes
            refresh_lock = cache.get(_refresh_lock_key(user.id, logging_style))
            if refresh_lock is None:
                # No refresh in progress, safe to schedule a new one
                schedule_history_refresh(user.id, logging_style)
        # Note: Even if cache is not stale, a refresh might be in progress
        # (e.g., triggered by music addition). The frontend will check via cache-status endpoint.
        return cache_entry.get("history_days", [])

    # Cache miss - check if refresh is in progress
    refresh_lock = cache.get(_refresh_lock_key(user.id, logging_style))
    if refresh_lock is not None:
        # Refresh is in progress, return empty data
        # Frontend will poll and update when refresh completes
        logger.debug("History cache miss but refresh in progress for user %s, returning empty", user.id)
        return []

    # No cache and no refresh in progress - build inline
    # This handles the case where cache was never built or expired naturally
    history_days = build_history_days(user)
    cache_history_days(user.id, logging_style, history_days)
    return history_days


def invalidate_history_cache(user_id: int, force: bool = False):
    """Remove cached history for a user.
    
    If a refresh is in progress, keep the old cache so users can see it
    while the refresh completes. Otherwise, delete the cache.
    
    Args:
        user_id: User ID
        force: If True, always delete cache even if refresh is in progress.
               Use this when data has been deleted to ensure deleted records don't persist.
               Note: This will cause the page to show empty until refresh completes,
               but the refresh will rebuild with correct data (excluding deleted records).
    """
    for style in ("sessions", "repeats", None):
        logging_style = style or "repeats"
        # Check if refresh is in progress
        refresh_lock = cache.get(_refresh_lock_key(user_id, logging_style))
        if refresh_lock is None or force:
            # No refresh in progress, or force deletion requested
            # When force=True, we delete even during refresh to ensure deleted records don't persist
            # The refresh will rebuild with correct data
            cache.delete(_cache_key(user_id, logging_style))
        # If refresh is in progress and not forcing, keep the old cache - it will be replaced when refresh completes


def refresh_history_cache(user_id: int):
    """Rebuild and store history for a user."""
    user_model = get_user_model()
    logging_style = "repeats"
    try:
        user = user_model.objects.get(id=user_id)
        logging_style = getattr(user, "game_logging_style", "repeats")
    except user_model.DoesNotExist:
        # Clear lock if user doesn't exist
        cache.delete(_refresh_lock_key(user_id, logging_style))
        return None

    try:
        history_days = build_history_days(user)
        cache_history_days(user_id, logging_style, history_days)
        # Delete the refresh lock AFTER cache is saved to ensure frontend sees
        # the new cache when it detects refresh completion
        lock_key = _refresh_lock_key(user_id, logging_style)
        cache.delete(lock_key)
        # Verify the lock was actually deleted
        verify_lock = cache.get(lock_key)
        logger.debug(
            "History cache refresh completed for user %s, lock released. Lock key: %s, still exists: %s",
            user_id,
            lock_key,
            verify_lock is not None,
        )
        return history_days
    except Exception as e:
        # Always clear the lock, even on error, to prevent it from being stuck
        logger.error("Error refreshing history cache for user %s: %s", user_id, e, exc_info=True)
        cache.delete(_refresh_lock_key(user_id, logging_style))
        raise


def schedule_history_refresh(user_id: int, logging_style: str = "repeats", debounce_seconds: int = 30, countdown: int = 3):
    """Queue a background refresh for a user's history cache.
    
    Args:
        user_id: User ID
        logging_style: Logging style for history
        debounce_seconds: Seconds to debounce refresh requests
        countdown: Seconds to delay task execution (default 3)
    """
    lock_key = _refresh_lock_key(user_id, logging_style)
    # Keep TTL close to the frontend polling timeout so locks don't appear "stuck"
    # while still covering normal task execution time.
    lock_ttl = 120  # Matches CacheUpdater timeout window
    lock_payload = {"started_at": timezone.now()}
    if debounce_seconds and not cache.add(lock_key, lock_payload, debounce_seconds):
        return False
    
    # Extend the lock TTL to cover the full task duration
    # This ensures the lock exists even if the task takes longer than debounce_seconds
    cache.set(lock_key, lock_payload, lock_ttl)

    try:
        from app.tasks import refresh_history_cache_task

        refresh_history_cache_task.apply_async(args=[user_id], countdown=countdown)
        return True
    except Exception as exc:  # pragma: no cover - Celery not available
        logger.debug(
            "Falling back to inline history cache rebuild for user %s: %s",
            user_id,
            exc,
        )
        refresh_history_cache(user_id)
        return False
