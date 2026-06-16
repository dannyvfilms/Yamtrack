"""Utilities for caching the History page."""

import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import models
from django.db.models.functions import TruncDate
from django.utils import formats, timezone

from app import credits as credit_helpers, helpers
from app.log_safety import stable_hmac
from app.models import (
    CREDITS_BACKFILL_VERSION,
    Album,
    BoardGame,
    Book,
    Comic,
    CreditRoleType,
    Episode,
    Game,
    Item,
    ItemPersonCredit,
    Manga,
    MediaTypes,
    Movie,
    Music,
    Podcast,
    Sources,
    MetadataBackfillField,
    MetadataBackfillState,
    Track,
)

# Re-export from extracted modules so existing callers using `history_cache.X` continue to work.
from app.history_cache_utils import (  # noqa: F401
    HISTORY_CACHE_PREFIX,
    HISTORY_CACHE_TIMEOUT,
    HISTORY_CACHE_VERSION,
    HISTORY_COLD_MISS_WARM_DAYS,
    HISTORY_COVERAGE_REPAIR_BATCH_SIZE,
    HISTORY_COVERAGE_REPAIR_LOCK_TTL,
    HISTORY_COVERAGE_REPAIR_PREFIX,
    HISTORY_DAY_CACHE_TIMEOUT,
    HISTORY_DAY_PREFIX,
    HISTORY_DAYS_PER_PAGE,
    HISTORY_INDEX_PREFIX,
    HISTORY_REFRESH_LOCK_MAX_AGE,
    HISTORY_REFRESH_LOCK_PREFIX,
    HISTORY_STALE_AFTER,
    HISTORY_WARM_DAYS,
    _cache_key,
    _coerce_genre_list,
    _coerce_timedelta,
    _coverage_repair_key,
    _date_from_day_key,
    _day_cache_key,
    _day_key_for_date,
    _day_key_from_value,
    _get_rss_kb,
    _localize_datetime,
    _music_history_user_q,
    _normalize_logging_style,
    _refresh_lock_key,
    _resolve_genres,
    _resolve_music_genres,
    history_day_key,
    history_day_keys_for_range,
)
from app.history_entry_builders import (  # noqa: F401
    _attach_entry_score,
    _build_episode_entry,
    _build_movie_entry,
    _build_music_album_entries,
    _format_boardgame_plays,
    _format_game_hours,
    _get_episode_display_title,
    _get_episode_poster,
    _get_music_runtime_minutes,
    _resolve_runtime_minutes,
    _serialize_album,
    _serialize_item,
    _serialize_show,
)
from app.history_cache_serialization import (  # noqa: F401
    _deserialize_history_day,
    _deserialize_history_entry,
    _serialize_history_day,
    _serialize_history_entry,
)
from app.history_cache_lifecycle import (  # noqa: F401
    _clean_refresh_lock,
    _delete_history_cache_entries,
    invalidate_history_cache,
    invalidate_history_days,
    schedule_history_day_cache_coverage,
    schedule_history_refresh,
)

logger = logging.getLogger(__name__)




def build_history_days(user, filters=None, date_filters=None, logging_style_override=None):
    """Build the list of grouped history entries for a user.
    
    Args:
        user: User instance
        filters: Optional dict of filter parameters:
            - album: Filter music entries by album_id
            - artist: Filter music entries by album__artist_id
            - tv: Filter episodes by related_season__related_tv_id
            - season: Filter episodes by related_season_id
            - person_source: Filter by credited person source (e.g. "tmdb", "openlibrary")
            - person_id: Filter by credited provider person ID
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
    build_start = time.perf_counter()
    rss_kb_start = _get_rss_kb()
    entry_counts = {
        "episodes": 0,
        "movies": 0,
        "music": 0,
        "podcasts": 0,
        "games": 0,
        "boardgames": 0,
        "books": 0,
        "comics": 0,
        "manga": 0,
    }
    music_history_records_scanned = 0

    # Parse date filters
    start_date = None
    end_date = None
    if date_filters.get("start_date"):
        from django.utils import timezone as tz
        from django.utils.dateparse import parse_date
        parsed = parse_date(date_filters["start_date"])
        if parsed:
            start_date = tz.make_aware(datetime.combine(parsed, datetime.min.time()))
    if date_filters.get("end_date"):
        from django.utils import timezone as tz
        from django.utils.dateparse import parse_date
        parsed = parse_date(date_filters["end_date"])
        if parsed:
            end_date = tz.make_aware(datetime.combine(parsed, datetime.max.time()))
    if logging_style_override not in ("sessions", "repeats"):
        logging_style_override = None
    game_logging_style = logging_style_override or getattr(user, "game_logging_style", "repeats")

    logger.info(
        "history_build_start user_id=%s filters=%s date_filters=%s logging_style=%s",
        user.id,
        filters,
        date_filters,
        game_logging_style,
    )

    media_type_filter = filters.get('media_type')
    target_media_id = filters.get('media_id')
    target_source = filters.get('source')
    season_number_filter = filters.get('season_number')
    podcast_show_filter = filters.get('podcast_show')
    person_source_filter = filters.get("person_source")
    person_id_filter = filters.get("person_id")
    reading_media_types = {
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
        MediaTypes.MANGA.value,
    }
    has_reading_media_type_filter = media_type_filter in reading_media_types
    if target_media_id is not None:
        target_media_id = str(target_media_id)
    if target_source is not None:
        target_source = str(target_source)
    if person_source_filter is not None:
        person_source_filter = str(person_source_filter)
    if person_id_filter is not None:
        person_id_filter = str(person_id_filter)

    episodes_start = time.perf_counter()
    if (
        not media_type_filter
        or media_type_filter == MediaTypes.TV.value
        or filters.get('tv')
        or filters.get('season')
        or season_number_filter is not None
        or (person_source_filter and person_id_filter)
    ):
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
        if person_source_filter and person_id_filter:
            regular_show_cast_filter = (
                models.Q(role_type=CreditRoleType.CAST.value)
                & (
                    ~models.Q(item__source=Sources.TMDB.value)
                    | models.Q(
                        sort_order__lt=credit_helpers.TMDB_SHOW_REGULAR_CAST_SORT_ORDER_CUTOFF,
                    )
                )
            )
            episode_person_credits = ItemPersonCredit.objects.filter(
                item_id=models.OuterRef("item_id"),
            )
            episode_person_matches = episode_person_credits.filter(
                person__source=person_source_filter,
                person__source_person_id=person_id_filter,
            )
            season_person_credits = ItemPersonCredit.objects.filter(
                item_id=models.OuterRef("related_season__item_id"),
            )
            season_person_matches = season_person_credits.filter(
                person__source=person_source_filter,
                person__source_person_id=person_id_filter,
            )
            season_has_cast_credits = models.Exists(
                ItemPersonCredit.objects.filter(
                    item_id=models.OuterRef("related_season__item_id"),
                    role_type=CreditRoleType.CAST.value,
                ),
            )
            season_has_crew_credits = models.Exists(
                ItemPersonCredit.objects.filter(
                    item_id=models.OuterRef("related_season__item_id"),
                    role_type=CreditRoleType.CREW.value,
                ),
            )
            season_has_usable_credits = models.Exists(
                MetadataBackfillState.objects.filter(
                    item_id=models.OuterRef("related_season__item_id"),
                    field=MetadataBackfillField.CREDITS,
                    last_success_at__isnull=False,
                    strategy_version__gte=CREDITS_BACKFILL_VERSION,
                ),
            )
            show_cast_person_matches = ItemPersonCredit.objects.filter(
                item_id=models.OuterRef("related_season__related_tv__item_id"),
                person__source=person_source_filter,
                person__source_person_id=person_id_filter,
            ).filter(
                regular_show_cast_filter,
            )
            show_noncast_person_matches = ItemPersonCredit.objects.filter(
                item_id=models.OuterRef("related_season__related_tv__item_id"),
                person__source=person_source_filter,
                person__source_person_id=person_id_filter,
            ).filter(
                role_type=CreditRoleType.CREW.value,
            )
            show_has_usable_credits = models.Exists(
                MetadataBackfillState.objects.filter(
                    item_id=models.OuterRef("related_season__related_tv__item_id"),
                    field=MetadataBackfillField.CREDITS,
                    last_success_at__isnull=False,
                    strategy_version__gte=CREDITS_BACKFILL_VERSION,
                ),
            )
            episodes = episodes.annotate(
                has_episode_person=models.Exists(episode_person_matches),
                has_season_person=models.Exists(season_person_matches),
                season_has_cast_credits=season_has_cast_credits,
                season_has_crew_credits=season_has_crew_credits,
                season_has_usable_credits=season_has_usable_credits,
                has_show_cast_person=models.Exists(show_cast_person_matches),
                has_show_noncast_person=models.Exists(show_noncast_person_matches),
                show_has_usable_credits=show_has_usable_credits,
            ).filter(
                models.Q(has_episode_person=True)
                | models.Q(has_season_person=True)
                | (
                    (
                        models.Q(season_has_cast_credits=False)
                        | models.Q(
                            related_season__item__source=Sources.TMDB.value,
                            season_has_usable_credits=False,
                        )
                    )
                    & models.Q(has_show_cast_person=True)
                    & (
                        ~models.Q(related_season__related_tv__item__source=Sources.TMDB.value)
                        | models.Q(show_has_usable_credits=True)
                    )
                )
                | (
                    (
                        models.Q(season_has_crew_credits=False)
                        | models.Q(
                            related_season__item__source=Sources.TMDB.value,
                            season_has_usable_credits=False,
                        )
                    )
                    & models.Q(has_show_noncast_person=True)
                    & (
                        ~models.Q(related_season__related_tv__item__source=Sources.TMDB.value)
                        | models.Q(show_has_usable_credits=True)
                    )
                ),
            )
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
    else:
        episodes = []
    logger.info(
        "history_build_episodes user_id=%s count=%s elapsed_ms=%.2f",
        user.id,
        len(episodes),
        (time.perf_counter() - episodes_start) * 1000,
    )

    movies_start = time.perf_counter()
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
    if person_source_filter and person_id_filter:
        movies_qs = movies_qs.filter(
            item__person_credits__person__source=person_source_filter,
            item__person_credits__person__source_person_id=person_id_filter,
        ).distinct()

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
    try:
        movies_count = movies_qs.count()
    except Exception:
        movies_count = None
    logger.info(
        "history_build_movies user_id=%s qs_count=%s play_map=%s elapsed_ms=%.2f",
        user.id,
        movies_count,
        len(movie_play_map),
        (time.perf_counter() - movies_start) * 1000,
    )
    games_start = time.perf_counter()
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

    # Apply date range filter to games
    if start_date:
        games = games.filter(
            models.Q(end_date__gte=start_date)
            | (models.Q(end_date__isnull=True) & models.Q(start_date__gte=start_date))
        )
    if end_date:
        games = games.filter(
            models.Q(end_date__lte=end_date)
            | (models.Q(end_date__isnull=True) & models.Q(start_date__lte=end_date))
        )

    # Apply date range filter to boardgames
    if start_date:
        boardgames = boardgames.filter(
            models.Q(end_date__gte=start_date)
            | (models.Q(end_date__isnull=True) & models.Q(start_date__gte=start_date))
        )
    if end_date:
        boardgames = boardgames.filter(
            models.Q(end_date__lte=end_date)
            | (models.Q(end_date__isnull=True) & models.Q(start_date__lte=end_date))
        )

    try:
        games_count = games.count()
    except Exception:
        games_count = None
    try:
        boardgames_count = boardgames.count()
    except Exception:
        boardgames_count = None
    
    # Music - query all music entries with end_date
    music_start = time.perf_counter()
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

    try:
        music_entries_count = music_entries.count()
    except Exception:
        music_entries_count = None
    
    # Podcasts - query history records directly to ensure deleted records don't show up
    podcast_start = time.perf_counter()
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

    try:
        podcast_history_count = podcast_history_records.count()
    except Exception:
        podcast_history_count = None
    
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
    has_person_filter = bool(person_source_filter and person_id_filter)
    process_all = not (
        has_music_filter
        or has_tv_filter
        or has_podcast_filter
        or has_person_filter
        or media_type_filter
    )
    
    # Helper function to check if entry matches genre filter by checking metadata.
    # Uses a cache to avoid repeated metadata lookups for the same media item.
    genre_filter = filters.get("genre")
    implied_genre_filter = filters.get("implied_genre")
    # Support comma-separated genres — match if item has ANY selected genre (OR logic)
    genre_filters = (
        [g.strip().lower() for g in genre_filter.split(",") if g.strip()]
        if genre_filter
        else []
    )
    implied_genre_filters = (
        [g.strip().lower() for g in implied_genre_filter.split(",") if g.strip()]
        if implied_genre_filter
        else []
    )
    genre_cache = {}  # Cache: (media_type, media_id) -> bool (matches genre or None if not checked)

    def matches_genre(media_entry, media_type):
        """Check if media entry matches genre filter by checking metadata."""
        if not genre_filters:
            return True

        # For TV episodes, use the parent TV show for caching
        cache_key = None
        if media_type == MediaTypes.EPISODE.value and hasattr(media_entry, "related_season"):
            if hasattr(media_entry.related_season, "related_tv") and media_entry.related_season.related_tv:
                tv_show = media_entry.related_season.related_tv
                if hasattr(tv_show, "item") and tv_show.item:
                    cache_key = (MediaTypes.TV.value, tv_show.item.media_id, tv_show.item.source)
        elif hasattr(media_entry, "item") and media_entry.item:
            cache_key = (media_type, media_entry.item.media_id, media_entry.item.source)

        # Check cache first
        if cache_key and cache_key in genre_cache:
            return genre_cache[cache_key] is True

        try:
            from app.statistics import (
                _coerce_genre_list,
                _get_media_metadata_for_statistics,
            )

            # For TV episodes, get genres from parent TV show
            if media_type == MediaTypes.EPISODE.value and hasattr(media_entry, "related_season"):
                if hasattr(media_entry.related_season, "related_tv") and media_entry.related_season.related_tv:
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

            # Check if any item genre matches any selected genre (case-insensitive OR)
            item_genre_set = {str(g).lower() for g in genres}
            matches = bool(item_genre_set & set(genre_filters))

            # Cache the result
            if cache_key:
                genre_cache[cache_key] = matches

            return matches
        except Exception as e:
            logger.debug(f"Error checking genre for {media_entry}: {e}")
            if cache_key:
                genre_cache[cache_key] = False
            return False  # Skip if we can't check genre

    def matches_item_genre(item):
        """Check if an item has a genre match using stored genres only."""
        if not genre_filters:
            return True
        genres = _resolve_genres(item)
        item_genre_set = {str(g).lower() for g in genres}
        return bool(item_genre_set & set(genre_filters))

    def entry_matches_implied_genre(entry):
        """Check if a built entry matches the implied-genre filter."""
        if not implied_genre_filters:
            return True
        item = entry.get("item") or {}
        if isinstance(item, dict):
            implied_genres = item.get("implied_genres") or []
        else:
            implied_genres = getattr(item, "implied_genres", None) or []
        entry_implied_genres = entry.get("implied_genres") or implied_genres
        return bool(
            {str(genre).lower() for genre in entry_implied_genres}
            & set(implied_genre_filters),
        )

    # Build a lookup of episode titles from stored items to avoid provider calls
    # Only if we're processing episodes
    if process_all or has_tv_filter or has_person_filter or media_type_filter == MediaTypes.TV.value:
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
            if genre_filters and not matches_genre(episode, MediaTypes.EPISODE.value):
                continue
            entry = _build_episode_entry(episode, episode_title_map)
            if entry:
                entries.append(entry)
                entry_counts["episodes"] += 1

    # Process movies only if not filtering by specific media type or if filtering by movie
    if process_all or has_person_filter or media_type_filter == MediaTypes.MOVIE.value:
        for movie in movies:
            # Apply genre filter if specified
            if genre_filters and not matches_genre(movie, MediaTypes.MOVIE.value):
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
            entry_counts["movies"] += 1

    # Author-filtered reading history support:
    # include book/comic/manga entries when explicitly filtering to them, and
    # keep author-filtered reading history support.
    if has_person_filter or has_reading_media_type_filter:
        credited_reading_item_ids = None
        if has_person_filter:
            credited_reading_item_ids = set(
                ItemPersonCredit.objects.filter(
                    role_type=CreditRoleType.AUTHOR.value,
                    person__source=person_source_filter,
                    person__source_person_id=person_id_filter,
                    item__media_type__in=tuple(reading_media_types),
                ).values_list("item_id", flat=True),
            )

        reading_model_map = {
            MediaTypes.BOOK.value: Book,
            MediaTypes.COMIC.value: Comic,
            MediaTypes.MANGA.value: Manga,
        }
        for reading_media_type, model in reading_model_map.items():
            if media_type_filter and media_type_filter != reading_media_type:
                continue

            queryset = model.objects.filter(
                user=user,
                item__media_type=reading_media_type,
            ).select_related("item")
            if credited_reading_item_ids is not None:
                if not credited_reading_item_ids:
                    continue
                queryset = queryset.filter(item_id__in=credited_reading_item_ids)
            if target_media_id and target_source and media_type_filter == reading_media_type:
                queryset = queryset.filter(
                    item__media_id=target_media_id,
                    item__source=target_source,
                )
            if start_date:
                queryset = queryset.filter(
                    models.Q(end_date__gte=start_date)
                    | (
                        models.Q(end_date__isnull=True)
                        & models.Q(start_date__gte=start_date)
                    ),
                )
            if end_date:
                queryset = queryset.filter(
                    models.Q(end_date__lte=end_date)
                    | (
                        models.Q(end_date__isnull=True)
                        & models.Q(start_date__lte=end_date)
                    ),
                )
            queryset = queryset.filter(
                models.Q(start_date__isnull=False) | models.Q(end_date__isnull=False),
            ).order_by("-end_date", "-start_date", "-created_at")

            for reading_entry in queryset:
                item = getattr(reading_entry, "item", None)
                if not item:
                    continue
                if genre_filters and not matches_item_genre(item):
                    continue
                played_at_local = _localize_datetime(
                    reading_entry.end_date
                    or reading_entry.start_date
                    or reading_entry.created_at,
                )
                if not played_at_local:
                    continue

                entry = {
                    "media_type": item.media_type,
                    "item": _serialize_item(item),
                    "poster": item.image or settings.IMG_NONE,
                    "title": item.title,
                    "display_title": item.title,
                    "episode_label": None,
                    "episode_code": None,
                    "played_at_local": played_at_local,
                    "runtime_minutes": 0,
                    "runtime_display": None,
                    "instance_id": reading_entry.id,
                    "entry_key": f"{item.media_type}-{reading_entry.id}",
                }
                _attach_entry_score(entry, reading_entry)
                genres = _resolve_genres(item)
                if genres:
                    entry["genres"] = genres
                entries.append(entry)
                if item.media_type == MediaTypes.BOOK.value:
                    entry_counts["books"] += 1
                elif item.media_type == MediaTypes.COMIC.value:
                    entry_counts["comics"] += 1
                elif item.media_type == MediaTypes.MANGA.value:
                    entry_counts["manga"] += 1

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
                music_history_records_scanned += 1
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

        # Fetch album trackers for albums in history to include scores
        album_scores = {}
        if album_ids_with_music:
            from app.models import AlbumTracker
            album_trackers = AlbumTracker.objects.filter(
                user=user,
                album_id__in=album_ids_with_music,
            ).values("album_id", "score")
            for tracker in album_trackers:
                if tracker["score"] is not None:
                    album_scores[tracker["album_id"]] = tracker["score"]

        # Now build one entry per album per day
        for (album_id, day_date), album_music_entries in music_by_album_day.items():
            album = album_lookup.get(album_id)

            # Apply genre filter if specified - check album or artist genres
            if genre_filters and album:
                from app.statistics import _coerce_genre_list
                # Check album genres first, then artist genres
                album_genres = _coerce_genre_list(album.genres) if album.genres else []
                artist_genres = []
                if album.artist and album.artist.genres:
                    artist_genres = _coerce_genre_list(album.artist.genres)

                all_genres = album_genres + artist_genres
                all_genres_lower = {str(g).lower() for g in all_genres}
                genre_match = bool(all_genres_lower & set(genre_filters))
                if not genre_match:
                    continue

            entry = _build_music_album_entries(album_music_entries, album, day_date, user, track_duration_cache, album_scores)
            if entry:
                entries.append(entry)
                entry_counts["music"] += 1

        logger.info(
            "history_build_music user_id=%s music_entries=%s history_records_scanned=%s album_day_groups=%s entries=%s elapsed_ms=%.2f",
            user.id,
            music_entries_count,
            music_history_records_scanned,
            len(music_by_album_day),
            entry_counts["music"],
            (time.perf_counter() - music_start) * 1000,
        )

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

                entry = {
                    "media_type": MediaTypes.PODCAST.value,
                    "item": _serialize_item(podcast.item),
                    "show": _serialize_show(show),
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
                }
                _attach_entry_score(entry, podcast)
                entries.append(entry)
                entry_counts["podcasts"] += 1
            except Exception as e:
                logger.error("Error processing podcast history record %s: %s", history_record.history_id, e, exc_info=True)
                continue

        logger.info(
            "history_build_podcasts user_id=%s history_records=%s podcast_ids=%s entries=%s elapsed_ms=%.2f",
            user.id,
            podcast_history_count,
            len(podcast_ids),
            entry_counts["podcasts"],
            (time.perf_counter() - podcast_start) * 1000,
        )

    # Games - process when showing all media or filtering to games/board games
    process_games = process_all or media_type_filter == MediaTypes.GAME.value
    process_boardgames = process_all or media_type_filter == MediaTypes.BOARDGAME.value
    if process_games or process_boardgames:
        if game_logging_style == "sessions":
            if process_games:
                for game in games:
                    if not (game.start_date or game.end_date):
                        continue
                    if genre_filters and not matches_item_genre(game.item):
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

                    genres = _resolve_genres(game.item)
                    entry = {
                        "media_type": MediaTypes.GAME.value,
                        "item": _serialize_item(game.item),
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
                    }
                    _attach_entry_score(entry, game)
                    if genres:
                        entry["genres"] = genres
                    entries.append(entry)
                    entry_counts["games"] += 1
            if process_boardgames:
                for boardgame in boardgames:
                    if not (boardgame.start_date or boardgame.end_date):
                        continue
                    if genre_filters and not matches_item_genre(boardgame.item):
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

                    genres = _resolve_genres(boardgame.item)
                    entry = {
                        "media_type": MediaTypes.BOARDGAME.value,
                        "item": _serialize_item(boardgame.item),
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
                    }
                    _attach_entry_score(entry, boardgame)
                    if genres:
                        entry["genres"] = genres
                    entries.append(entry)
                    entry_counts["boardgames"] += 1
        else:
            # repeats style: spread playtime evenly across date range
            if process_games:
                for game in games:
                    if not (game.start_date or game.end_date):
                        continue
                    if genre_filters and not matches_item_genre(game.item):
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
                    genres = _resolve_genres(game.item)

                    for offset in range(day_count):
                        day = start_local + timedelta(days=offset)
                        minutes_for_day = base + (1 if offset < remainder else 0)
                        day_dt = timezone.make_aware(
                            datetime.combine(day, datetime.min.time()),
                            timezone.get_current_timezone(),
                        )
                        entry = {
                            "media_type": MediaTypes.GAME.value,
                            "item": _serialize_item(game.item),
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
                        }
                        _attach_entry_score(entry, game)
                        if genres:
                            entry["genres"] = genres
                        entries.append(entry)
                        entry_counts["games"] += 1
            if process_boardgames:
                for boardgame in boardgames:
                    if not (boardgame.start_date or boardgame.end_date):
                        continue
                    if genre_filters and not matches_item_genre(boardgame.item):
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
                    genres = _resolve_genres(boardgame.item)

                    for offset in range(day_count):
                        day = start_local + timedelta(days=offset)
                        plays_for_day = base + (1 if offset < remainder else 0)
                        day_dt = timezone.make_aware(
                            datetime.combine(day, datetime.min.time()),
                            timezone.get_current_timezone(),
                        )
                        entry = {
                            "media_type": MediaTypes.BOARDGAME.value,
                            "item": _serialize_item(boardgame.item),
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
                        }
                        _attach_entry_score(entry, boardgame)
                        if genres:
                            entry["genres"] = genres
                        entries.append(entry)
                        entry_counts["boardgames"] += 1

        logger.info(
            "history_build_games user_id=%s games=%s boardgames=%s entries_games=%s entries_boardgames=%s logging_style=%s elapsed_ms=%.2f",
            user.id,
            games_count,
            boardgames_count,
            entry_counts["games"],
            entry_counts["boardgames"],
            game_logging_style,
            (time.perf_counter() - games_start) * 1000,
        )

    if implied_genre_filters:
        entries = [entry for entry in entries if entry_matches_implied_genre(entry)]

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

    rss_kb_end = _get_rss_kb()
    rss_kb_delta = None
    if rss_kb_start is not None and rss_kb_end is not None:
        rss_kb_delta = rss_kb_end - rss_kb_start

    logger.info(
        "history_build_end user_id=%s entries=%s history_days=%s entry_counts=%s elapsed_ms=%.2f rss_kb_start=%s rss_kb_end=%s rss_kb_delta=%s",
        user.id,
        len(entries),
        len(history_days),
        entry_counts,
        (time.perf_counter() - build_start) * 1000,
        rss_kb_start,
        rss_kb_end,
        rss_kb_delta,
    )

    return history_days


def cache_history_days(user_id: int, logging_style: str, history_days):
    """Persist the grouped history in cache."""
    cache_history_payloads(user_id, logging_style, history_days)


def cache_history_payloads(user_id: int, logging_style: str, history_days):
    """Persist index + per-day history payloads in cache."""
    logging_style = _normalize_logging_style(logging_style)
    index_days = []
    day_payloads = {}
    total_entries = 0
    for day in history_days:
        day_date = day.get("date")
        if not day_date:
            continue
        if isinstance(day_date, str):
            try:
                day_date = datetime.strptime(day_date, "%Y-%m-%d").date()
            except ValueError:
                continue
        day_key = _day_key_for_date(day_date)
        index_days.append(day_key)
        total_entries += len(day.get("entries", []))
        day_payloads[_day_cache_key(user_id, logging_style, day_key)] = _serialize_history_day(day)

    cache.set(
        _cache_key(user_id, logging_style),
        {
            "days": index_days,
            "built_at": timezone.now(),
        },
        timeout=HISTORY_CACHE_TIMEOUT,
    )
    if day_payloads:
        cache.set_many(day_payloads, timeout=HISTORY_DAY_CACHE_TIMEOUT)
    logger.info(
        "history_cache_store user_id=%s logging_style=%s days=%s entries=%s",
        user_id,
        logging_style,
        len(index_days),
        total_entries,
    )


def cache_history_index(user_id: int, logging_style: str, day_keys, built_at=None):
    logging_style = _normalize_logging_style(logging_style)
    if built_at is None:
        built_at = timezone.now()
    cache.set(
        _cache_key(user_id, logging_style),
        {
            "days": day_keys,
            "built_at": built_at,
        },
        timeout=HISTORY_CACHE_TIMEOUT,
    )
    return built_at


def _missing_history_day_keys(user_id: int, logging_style: str, day_keys):
    """Return normalized day keys that do not currently have cached payloads."""
    normalized_keys = []
    for value in day_keys or []:
        day_key = _day_key_from_value(value)
        if day_key:
            normalized_keys.append(day_key)

    if not normalized_keys:
        return []

    payloads = cache.get_many(
        [_day_cache_key(user_id, logging_style, day_key) for day_key in normalized_keys],
    )
    return [
        day_key
        for day_key in normalized_keys
        if _day_cache_key(user_id, logging_style, day_key) not in payloads
    ]


def _add_days(days_set, days_iterable):
    added = 0
    for day in days_iterable:
        if day and day not in days_set:
            days_set.add(day)
            added += 1
    return added


def build_history_index(user, logging_style_override=None):
    """Build an ordered list of active history days for a user."""
    build_start = time.perf_counter()
    logging_style = _normalize_logging_style(logging_style_override, user)
    days = set()

    episode_days = Episode.objects.filter(
        related_season__user=user,
        end_date__isnull=False,
    ).annotate(
        day=TruncDate("end_date"),
    ).values_list("day", flat=True).distinct()
    episode_count = _add_days(days, episode_days)

    movie_qs = Movie.objects.filter(user=user).filter(
        models.Q(end_date__isnull=False) | models.Q(start_date__isnull=False),
    )
    movie_end_days = movie_qs.filter(
        end_date__isnull=False,
    ).annotate(
        day=TruncDate("end_date"),
    ).values_list("day", flat=True).distinct()
    movie_start_days = movie_qs.filter(
        end_date__isnull=True,
        start_date__isnull=False,
    ).annotate(
        day=TruncDate("start_date"),
    ).values_list("day", flat=True).distinct()
    movie_count = _add_days(days, movie_end_days)
    movie_count += _add_days(days, movie_start_days)

    from django.apps import apps

    HistoricalMusic = apps.get_model("app", "HistoricalMusic")
    music_days = HistoricalMusic.objects.filter(
        _music_history_user_q(user),
        end_date__isnull=False,
    ).annotate(
        day=TruncDate("end_date"),
    ).values_list("day", flat=True).distinct()
    music_count = _add_days(days, music_days)

    HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")
    podcast_days = HistoricalPodcast.objects.filter(
        models.Q(history_user=user) | models.Q(history_user__isnull=True),
        end_date__isnull=False,
    ).annotate(
        day=TruncDate("end_date"),
    ).values_list("day", flat=True).distinct()
    podcast_count = _add_days(days, podcast_days)

    game_count = 0
    boardgame_count = 0
    if logging_style == "sessions":
        games = Game.objects.filter(user=user)
        game_end_days = games.filter(
            end_date__isnull=False,
        ).annotate(
            day=TruncDate("end_date"),
        ).values_list("day", flat=True).distinct()
        game_start_days = games.filter(
            end_date__isnull=True,
            start_date__isnull=False,
        ).annotate(
            day=TruncDate("start_date"),
        ).values_list("day", flat=True).distinct()
        game_created_days = games.filter(
            end_date__isnull=True,
            start_date__isnull=True,
        ).annotate(
            day=TruncDate("created_at"),
        ).values_list("day", flat=True).distinct()
        game_count += _add_days(days, game_end_days)
        game_count += _add_days(days, game_start_days)
        game_count += _add_days(days, game_created_days)

        boardgames = BoardGame.objects.filter(user=user)
        boardgame_end_days = boardgames.filter(
            end_date__isnull=False,
        ).annotate(
            day=TruncDate("end_date"),
        ).values_list("day", flat=True).distinct()
        boardgame_start_days = boardgames.filter(
            end_date__isnull=True,
            start_date__isnull=False,
        ).annotate(
            day=TruncDate("start_date"),
        ).values_list("day", flat=True).distinct()
        boardgame_created_days = boardgames.filter(
            end_date__isnull=True,
            start_date__isnull=True,
        ).annotate(
            day=TruncDate("created_at"),
        ).values_list("day", flat=True).distinct()
        boardgame_count += _add_days(days, boardgame_end_days)
        boardgame_count += _add_days(days, boardgame_start_days)
        boardgame_count += _add_days(days, boardgame_created_days)
    else:
        games = Game.objects.filter(user=user).only(
            "start_date",
            "end_date",
            "created_at",
            "progress",
        )
        for game in games.iterator():
            total_minutes = game.progress or 0
            if total_minutes <= 0:
                continue
            start_dt = game.start_date or game.end_date or game.created_at
            end_dt = game.end_date or game.start_date or game.created_at
            if not start_dt or not end_dt:
                continue
            start_local = _localize_datetime(start_dt)
            end_local = _localize_datetime(end_dt)
            if not start_local or not end_local:
                continue
            start_date = start_local.date()
            end_date = end_local.date()
            if start_date > end_date:
                start_date, end_date = end_date, start_date
            day_count = (end_date - start_date).days + 1
            for offset in range(day_count):
                day_value = start_date + timedelta(days=offset)
                if day_value not in days:
                    days.add(day_value)
                    game_count += 1

        boardgames = BoardGame.objects.filter(user=user).only(
            "start_date",
            "end_date",
            "created_at",
            "progress",
        )
        for boardgame in boardgames.iterator():
            total_plays = boardgame.progress or 0
            if total_plays <= 0:
                continue
            start_dt = boardgame.start_date or boardgame.end_date or boardgame.created_at
            end_dt = boardgame.end_date or boardgame.start_date or boardgame.created_at
            if not start_dt or not end_dt:
                continue
            start_local = _localize_datetime(start_dt)
            end_local = _localize_datetime(end_dt)
            if not start_local or not end_local:
                continue
            start_date = start_local.date()
            end_date = end_local.date()
            if start_date > end_date:
                start_date, end_date = end_date, start_date
            day_count = (end_date - start_date).days + 1
            for offset in range(day_count):
                day_value = start_date + timedelta(days=offset)
                if day_value not in days:
                    days.add(day_value)
                    boardgame_count += 1

    day_list = sorted(days, reverse=True)
    day_keys = [_day_key_for_date(day) for day in day_list]
    logger.info(
        "history_index_build user_id=%s logging_style=%s days=%s episode_days=%s movie_days=%s music_days=%s podcast_days=%s game_days=%s boardgame_days=%s elapsed_ms=%.2f",
        user.id,
        logging_style,
        len(day_keys),
        episode_count,
        movie_count,
        music_count,
        podcast_count,
        game_count,
        boardgame_count,
        (time.perf_counter() - build_start) * 1000,
    )
    return day_keys


def build_history_day(user, day_key, logging_style_override=None):
    """Build a single history day payload for a user."""
    if not day_key:
        return None
    logging_style = _normalize_logging_style(logging_style_override, user)
    if isinstance(day_key, str):
        day_date = _date_from_day_key(day_key)
    else:
        day_date = day_key
        day_key = _day_key_for_date(day_date)
    if not day_date:
        return None

    day_start = timezone.make_aware(
        datetime.combine(day_date, datetime.min.time()),
        timezone.get_current_timezone(),
    )
    day_end = day_start + timedelta(days=1)

    entries = []

    # Episodes
    episodes = (
        Episode.objects.filter(
            related_season__user=user,
            end_date__gte=day_start,
            end_date__lt=day_end,
        )
        .select_related(
            "item",
            "related_season__item",
            "related_season__related_tv__item",
        )
        .order_by("-end_date")
    )
    episodes = list(episodes)
    episode_title_map = {}
    if episodes:
        episode_keys = []
        for ep in episodes:
            ep_item = getattr(ep, "item", None)
            if not ep_item:
                continue
            if (
                ep_item.media_id
                and ep_item.source
                and ep_item.season_number is not None
                and ep_item.episode_number is not None
            ):
                episode_keys.append(
                    (
                        ep_item.media_id,
                        ep_item.source,
                        ep_item.season_number,
                        ep_item.episode_number,
                    ),
                )
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
        entry = _build_episode_entry(episode, episode_title_map)
        if entry:
            entries.append(entry)

    # Movies
    movies_qs = Movie.objects.filter(user=user).filter(
        models.Q(end_date__isnull=False) | models.Q(start_date__isnull=False),
    ).select_related("item")

    movie_play_counts = (
        movies_qs.values("item__media_id", "item__source")
        .annotate(play_count=models.Count("id"))
        .order_by()
    )
    movie_play_map = {
        (row["item__media_id"], row["item__source"]): row["play_count"]
        for row in movie_play_counts
    }

    movies = movies_qs.filter(
        models.Q(end_date__gte=day_start, end_date__lt=day_end)
        | (models.Q(end_date__isnull=True) & models.Q(start_date__gte=day_start, start_date__lt=day_end)),
    ).order_by("-end_date")

    for movie in movies:
        entry = _build_movie_entry(movie)
        if not entry:
            continue
        key = (movie.item.media_id, movie.item.source)
        annotated = movie_play_map.get(key)
        repeat_attr = getattr(movie, "repeats", None)
        entry["play_count"] = annotated or repeat_attr or 1
        entries.append(entry)

    # Music (HistoricalMusic for the day)
    from django.apps import apps

    HistoricalMusic = apps.get_model("app", "HistoricalMusic")
    music_history = list(
        HistoricalMusic.objects.filter(
            _music_history_user_q(user),
            end_date__gte=day_start,
            end_date__lt=day_end,
        ).values("id", "end_date", "album_id", "track_id")
    )
    if music_history:
        album_ids = {record["album_id"] for record in music_history if record["album_id"]}
        track_ids = {record["track_id"] for record in music_history if record["track_id"]}
        music_ids = {record["id"] for record in music_history if record["id"]}

        album_map = {
            album.id: album
            for album in Album.objects.filter(id__in=album_ids).select_related("artist")
        } if album_ids else {}
        track_map = {
            track.id: track
            for track in Track.objects.filter(id__in=track_ids)
        } if track_ids else {}
        music_map = {
            music.id: music
            for music in Music.objects.filter(id__in=music_ids, user=user).select_related("item", "album", "track")
        } if music_ids else {}

        track_duration_cache = {}
        if album_ids:
            tracks_qs = Track.objects.filter(
                album_id__in=album_ids,
                duration_ms__isnull=False,
            ).values("album_id", "title", "duration_ms", "musicbrainz_recording_id")
            for track_data in tracks_qs:
                title_key = (track_data["album_id"], track_data["title"])
                track_duration_cache[title_key] = track_data["duration_ms"]
                if track_data["musicbrainz_recording_id"]:
                    recording_key = ("recording", track_data["musicbrainz_recording_id"])
                    track_duration_cache[recording_key] = track_data["duration_ms"]

        # Fetch album trackers for albums in history to include scores
        album_scores = {}
        if album_ids:
            from app.models import AlbumTracker
            album_trackers = AlbumTracker.objects.filter(
                user=user,
                album_id__in=album_ids,
            ).values("album_id", "score")
            for tracker in album_trackers:
                if tracker["score"] is not None:
                    album_scores[tracker["album_id"]] = tracker["score"]

        album_groups = {}
        for record in music_history:
            played_at_local = _localize_datetime(record["end_date"])
            if not played_at_local:
                continue
            album_id = record["album_id"]
            track_id = record["track_id"]
            runtime_minutes = 0

            music_entry = music_map.get(record["id"])
            if music_entry:
                runtime_minutes = _get_music_runtime_minutes(music_entry, track_duration_cache)
            if not runtime_minutes:
                track = track_map.get(track_id)
                if track and track.duration_ms:
                    runtime_minutes = track.duration_ms // 60000

            group = album_groups.setdefault(
                album_id,
                {
                    "play_times": [],
                    "play_count": 0,
                    "total_runtime_minutes": 0,
                    "latest_play_time": None,
                    "primary_music_id": None,
                },
            )
            group["play_times"].append(played_at_local)
            group["play_count"] += 1
            group["total_runtime_minutes"] += runtime_minutes
            latest_play_time = group["latest_play_time"]
            if latest_play_time is None or played_at_local > latest_play_time:
                group["latest_play_time"] = played_at_local
                group["primary_music_id"] = record["id"]

        for album_id, group in album_groups.items():
            play_times = group["play_times"]
            if not play_times:
                continue
            play_times.sort()
            earliest_time = play_times[0]
            latest_time = play_times[-1]
            if len(play_times) == 1:
                time_range_display = formats.time_format(earliest_time, "g:i A")
            else:
                time_range_display = f"{formats.time_format(earliest_time, 'g:i A')} - {formats.time_format(latest_time, 'g:i A')}"

            album = album_map.get(album_id)
            album_name = album.title if album else "Unknown Album"
            artist_name = album.artist.name if album and album.artist else "Unknown Artist"
            poster = album.image if album and album.image else settings.IMG_NONE
            entry_music = music_map.get(group["primary_music_id"])
            entry_item = entry_music.item if entry_music else None
            track = entry_music.track if entry_music else None
            genres = _resolve_music_genres(album=album, artist=album.artist if album else None, track=track)
            entry_key = f"{album_id or 'album'}-{day_key}"

            # Get album score if available
            album_score = None
            if album_scores and album_id:
                album_score = album_scores.get(album_id)

            entry = {
                "media_type": MediaTypes.MUSIC.value,
                "item": _serialize_item(entry_item),
                "album": _serialize_album(album),
                "poster": poster,
                "title": album_name,
                "display_title": album_name,
                "artist_name": artist_name,
                "play_count": group["play_count"],
                "time_range_display": time_range_display,
                "episode_label": None,
                "episode_code": None,
                "played_at_local": latest_time,
                "runtime_minutes": group["total_runtime_minutes"],
                "runtime_display": helpers.minutes_to_hhmm(group["total_runtime_minutes"])
                if group["total_runtime_minutes"]
                else None,
                "instance_id": group["primary_music_id"],
                "entry_key": entry_key,
                "score": album_score,  # Album tracker score
            }
            if genres:
                entry["genres"] = genres
            entries.append(entry)

    # Podcasts (HistoricalPodcast for the day)
    HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")
    podcast_history_records = list(
        HistoricalPodcast.objects.filter(
            models.Q(history_user=user) | models.Q(history_user__isnull=True),
            end_date__gte=day_start,
            end_date__lt=day_end,
        )
    )
    if podcast_history_records:
        podcast_ids = list({record.id for record in podcast_history_records})
        podcasts_lookup = {
            p.id: p
            for p in Podcast.objects.filter(
                id__in=podcast_ids,
                user=user,
            ).select_related("item", "episode", "episode__show", "show")
        }

        podcast_play_counts = {}
        if podcast_ids:
            counts_by_id = {
                row["id"]: row["play_count"]
                for row in HistoricalPodcast.objects.filter(
                    id__in=podcast_ids,
                    end_date__isnull=False,
                )
                .filter(models.Q(history_user=user) | models.Q(history_user__isnull=True))
                .values("id")
                .annotate(play_count=models.Count("id"))
            }
            for podcast_id, play_count in counts_by_id.items():
                podcast = podcasts_lookup.get(podcast_id)
                if not podcast or not podcast.item:
                    continue
                key = (podcast.item.media_id, podcast.item.source)
                podcast_play_counts[key] = podcast_play_counts.get(key, 0) + play_count

        for history_record in podcast_history_records:
            podcast = podcasts_lookup.get(history_record.id)
            if not podcast or not podcast.item:
                continue

            played_at_local = _localize_datetime(getattr(history_record, "end_date", None))
            if not played_at_local:
                continue

            show = podcast.episode.show if podcast.episode and podcast.episode.show else podcast.show
            show_podcast_uuid = show.podcast_uuid if show else None
            show_slug = show.slug if show and show.slug else (show.title if show else "")
            poster = settings.IMG_NONE
            if show and show.image:
                poster = show.image
            elif podcast.item.image:
                poster = podcast.item.image

            minutes_listened = podcast.progress or 0
            runtime_minutes = podcast.item.runtime_minutes if podcast.item.runtime_minutes else minutes_listened
            key = (podcast.item.media_id, podcast.item.source)
            play_count = podcast_play_counts.get(key, 1)

            entries.append(
                {
                    "media_type": MediaTypes.PODCAST.value,
                    "item": _serialize_item(podcast.item),
                    "show": _serialize_show(show),
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

    # Games / Boardgames
    if logging_style == "sessions":
        games = Game.objects.filter(user=user).filter(
            models.Q(end_date__gte=day_start, end_date__lt=day_end)
            | (
                models.Q(end_date__isnull=True)
                & models.Q(start_date__gte=day_start, start_date__lt=day_end)
            )
            | (
                models.Q(end_date__isnull=True)
                & models.Q(start_date__isnull=True)
                & models.Q(created_at__gte=day_start, created_at__lt=day_end)
            )
        ).select_related("item")
        for game in games:
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
            genres = _resolve_genres(game.item)
            entry = {
                "media_type": MediaTypes.GAME.value,
                "item": _serialize_item(game.item),
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
            }
            _attach_entry_score(entry, game)
            if genres:
                entry["genres"] = genres
            entries.append(entry)

        boardgames = BoardGame.objects.filter(user=user).filter(
            models.Q(end_date__gte=day_start, end_date__lt=day_end)
            | (
                models.Q(end_date__isnull=True)
                & models.Q(start_date__gte=day_start, start_date__lt=day_end)
            )
            | (
                models.Q(end_date__isnull=True)
                & models.Q(start_date__isnull=True)
                & models.Q(created_at__gte=day_start, created_at__lt=day_end)
            )
        ).select_related("item")
        for boardgame in boardgames:
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
            genres = _resolve_genres(boardgame.item)
            entry = {
                "media_type": MediaTypes.BOARDGAME.value,
                "item": _serialize_item(boardgame.item),
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
            }
            _attach_entry_score(entry, boardgame)
            if genres:
                entry["genres"] = genres
            entries.append(entry)
    else:
        games = Game.objects.filter(user=user).select_related("item")
        for game in games:
            total_minutes = game.progress or 0
            if total_minutes <= 0:
                continue
            start_dt = game.start_date or game.end_date or game.created_at
            end_dt = game.end_date or game.start_date or game.created_at
            if not start_dt or not end_dt:
                continue
            start_local = _localize_datetime(start_dt)
            end_local = _localize_datetime(end_dt)
            if not start_local or not end_local:
                continue
            start_date = start_local.date()
            end_date = end_local.date()
            if start_date > end_date:
                start_date, end_date = end_date, start_date
            if day_date < start_date or day_date > end_date:
                continue
            day_count = (end_date - start_date).days + 1
            base = total_minutes // day_count
            remainder = total_minutes % day_count
            offset = (day_date - start_date).days
            minutes_for_day = base + (1 if offset < remainder else 0)
            date_range_display = f"{formats.date_format(start_date, 'M j')} - {formats.date_format(end_date, 'M j')}"
            total_progress_display = _format_game_hours(total_minutes)
            genres = _resolve_genres(game.item)
            day_dt = timezone.make_aware(
                datetime.combine(day_date, datetime.min.time()),
                timezone.get_current_timezone(),
            )
            entry = {
                "media_type": MediaTypes.GAME.value,
                "item": _serialize_item(game.item),
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
                "entry_key": f"{game.id}-{day_key}",
            }
            _attach_entry_score(entry, game)
            if genres:
                entry["genres"] = genres
            entries.append(entry)

        boardgames = BoardGame.objects.filter(user=user).select_related("item")
        for boardgame in boardgames:
            total_plays = boardgame.progress or 0
            if total_plays <= 0:
                continue
            start_dt = boardgame.start_date or boardgame.end_date or boardgame.created_at
            end_dt = boardgame.end_date or boardgame.start_date or boardgame.created_at
            if not start_dt or not end_dt:
                continue
            start_local = _localize_datetime(start_dt)
            end_local = _localize_datetime(end_dt)
            if not start_local or not end_local:
                continue
            start_date = start_local.date()
            end_date = end_local.date()
            if start_date > end_date:
                start_date, end_date = end_date, start_date
            if day_date < start_date or day_date > end_date:
                continue
            day_count = (end_date - start_date).days + 1
            base = total_plays // day_count
            remainder = total_plays % day_count
            offset = (day_date - start_date).days
            plays_for_day = base + (1 if offset < remainder else 0)
            date_range_display = f"{formats.date_format(start_date, 'M j')} - {formats.date_format(end_date, 'M j')}"
            total_progress_display = _format_boardgame_plays(total_plays)
            genres = _resolve_genres(boardgame.item)
            day_dt = timezone.make_aware(
                datetime.combine(day_date, datetime.min.time()),
                timezone.get_current_timezone(),
            )
            entry = {
                "media_type": MediaTypes.BOARDGAME.value,
                "item": _serialize_item(boardgame.item),
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
                "entry_key": f"{boardgame.id}-{day_key}",
            }
            _attach_entry_score(entry, boardgame)
            if genres:
                entry["genres"] = genres
            entries.append(entry)

    if not entries:
        return None

    entries.sort(key=lambda entry: entry["played_at_local"], reverse=True)
    total_minutes = sum(entry["runtime_minutes"] or 0 for entry in entries)
    first_entry_time = entries[0]["played_at_local"]

    return {
        "date": day_date,
        "weekday": formats.date_format(first_entry_time, "l"),
        "date_display": formats.date_format(first_entry_time, "F j, Y"),
        "entries": entries,
        "total_minutes": total_minutes,
        "total_runtime_display": helpers.minutes_to_hhmm(total_minutes)
        if total_minutes
        else "0min",
    }


def _empty_history_day(day_date):
    return {
        "date": day_date,
        "weekday": formats.date_format(day_date, "l"),
        "date_display": formats.date_format(day_date, "F j, Y"),
        "entries": [],
        "total_minutes": 0,
        "total_runtime_display": "0min",
    }


def _cache_history_day_payload(user_id: int, logging_style: str, day_key: str, day_payload):
    cache.set(
        _day_cache_key(user_id, logging_style, day_key),
        _serialize_history_day(day_payload),
        timeout=HISTORY_DAY_CACHE_TIMEOUT,
    )
    return day_payload


def _build_and_cache_history_day(user, day_key, logging_style_override=None):
    logging_style = _normalize_logging_style(logging_style_override, user)
    normalized_day_key = _day_key_from_value(day_key)
    if not normalized_day_key:
        return None
    day_payload = build_history_day(
        user,
        normalized_day_key,
        logging_style_override=logging_style,
    )
    if day_payload is None:
        day_payload = _empty_history_day(_date_from_day_key(normalized_day_key))
    return _cache_history_day_payload(
        user.id,
        logging_style,
        normalized_day_key,
        day_payload,
    )


def get_month_history(user, year: int, month: int, logging_style_override=None):
    """Get history days for a specific calendar month.

    Reads the month from the cached history index plus indexed per-day payloads
    that are kept warm by media events. Any missing indexed day payloads are
    repaired inline so the page can render final content in a single response.

    Args:
        user: User instance
        year: Calendar year (e.g., 2026)
        month: Calendar month (1-12)
        logging_style_override: Optional logging style override

    Returns:
        Tuple of (history_days, cache_meta) where cache_meta contains:
        - refreshing: bool - Whether a background refresh is in progress
        - refresh_reason: str or None - Why refresh was triggered
    """
    start = time.perf_counter()
    logging_style = _normalize_logging_style(logging_style_override, user)
    cache_meta = {"refreshing": False, "refresh_reason": None}

    # Check if a refresh is already in progress
    cache_key = _cache_key(user.id, logging_style)
    lock_key = _refresh_lock_key(user.id, logging_style)
    refresh_lock = _clean_refresh_lock(lock_key)

    cache_entry = cache.get(cache_key)
    cache_age_s = None
    if cache_entry:
        built_at = cache_entry.get("built_at")
        if built_at:
            cache_age_s = (timezone.now() - built_at).total_seconds()
        if built_at and timezone.now() - built_at > HISTORY_STALE_AFTER:
            if refresh_lock is None:
                scheduled = schedule_history_refresh(user.id, logging_style, warm_days=0)
                logger.info(
                    "history_index_stale_refresh user_id=%s logging_style=%s scheduled=%s cache_age_s=%s",
                    user.id,
                    logging_style,
                    scheduled,
                    cache_age_s,
                )
    else:
        logger.warning(
            "history_index_inline_repair user_id=%s logging_style=%s year=%s month=%s",
            user.id,
            logging_style,
            year,
            month,
        )
        index_day_keys = build_history_index(user, logging_style)
        built_at = cache_history_index(user.id, logging_style, index_day_keys)
        cache_entry = {"days": index_day_keys, "built_at": built_at}

    index_days = cache_entry.get("days", [])
    month_prefix = f"{year}{month:02d}"
    month_day_keys = [
        day_key
        for day_key in index_days
        if str(day_key).startswith(month_prefix)
    ]

    day_cache_keys = [
        _day_cache_key(user.id, logging_style, day_key)
        for day_key in month_day_keys
    ]
    day_payloads = cache.get_many(day_cache_keys)
    cache_hits = len(day_payloads)
    logger.info(
        "history_month_cache_lookup user_id=%s year=%s month=%s indexed_days=%s "
        "cache_hits=%s lock=%s cache_age_s=%s",
        user.id,
        year,
        month,
        len(month_day_keys),
        cache_hits,
        refresh_lock is not None,
        cache_age_s,
    )

    if not month_day_keys:
        logger.info(
            "history_month_result user_id=%s year=%s month=%s days=0 elapsed_ms=%.2f",
            user.id,
            year,
            month,
            (time.perf_counter() - start) * 1000,
        )
        return [], cache_meta

    history_days = []
    missing_days = []
    for day_key in month_day_keys:
        payload_key = _day_cache_key(user.id, logging_style, day_key)
        payload = day_payloads.get(payload_key)
        if payload is None:
            missing_days.append(day_key)
        else:
            history_days.append(_deserialize_history_day(payload))

    if missing_days:
        logger.warning(
            "history_month_cache_repair user_id=%s year=%s month=%s indexed_days=%s "
            "cached=%s missing=%s lock=%s",
            user.id,
            year,
            month,
            len(month_day_keys),
            len(history_days),
            len(missing_days),
            refresh_lock is not None,
        )
        history_days = []
        for day_key in month_day_keys:
            payload = day_payloads.get(_day_cache_key(user.id, logging_style, day_key))
            if payload is not None:
                history_days.append(_deserialize_history_day(payload))
                continue
            history_days.append(_build_and_cache_history_day(user, day_key, logging_style))
        schedule_history_day_cache_coverage(
            user.id,
            logging_style,
            countdown=15,
        )

    logger.info(
        "history_month_result user_id=%s year=%s month=%s days=%s "
        "source=cache elapsed_ms=%.2f",
        user.id,
        year,
        month,
        len(history_days),
        (time.perf_counter() - start) * 1000,
    )

    return history_days, cache_meta


def get_history_days(user, filters=None, date_filters=None, logging_style_override=None):
    """Build history days directly (used for filtered requests)."""
    start = time.perf_counter()
    logger.info(
        "history_cache_bypass user_id=%s filters=%s date_filters=%s logging_style_override=%s",
        user.id,
        filters or {},
        date_filters or {},
        logging_style_override,
    )
    history_days = build_history_days(
        user,
        filters=filters,
        date_filters=date_filters,
        logging_style_override=logging_style_override,
    )
    logger.info(
        "history_cache_bypass_done user_id=%s days=%s elapsed_ms=%.2f",
        user.id,
        len(history_days),
        (time.perf_counter() - start) * 1000,
    )
    return history_days


def get_cached_history_page(user, page_number: int = 1, logging_style_override=None):
    """Return a cached history page, total day count, and refresh metadata."""
    start = time.perf_counter()
    logging_style = _normalize_logging_style(logging_style_override, user)
    cache_key = _cache_key(user.id, logging_style)
    lock_key = _refresh_lock_key(user.id, logging_style)
    meta = {"refreshing": False, "refresh_reason": None}

    refresh_lock = _clean_refresh_lock(lock_key)
    lock_age_s = None
    if isinstance(refresh_lock, dict):
        started_at = refresh_lock.get("started_at")
        if started_at:
            lock_age_s = (timezone.now() - started_at).total_seconds()

    cache_entry = cache.get(cache_key)
    logger.info(
        "history_index_lookup user_id=%s cache_key=%s hit=%s lock=%s lock_age_s=%s",
        user.id,
        cache_key,
        cache_entry is not None,
        refresh_lock is not None,
        lock_age_s,
    )

    if not cache_entry:
        if refresh_lock is not None:
            logger.info(
                "history_index_miss_refreshing user_id=%s logging_style=%s returning_empty=true",
                user.id,
                logging_style,
            )
            meta.update({"refreshing": True, "refresh_reason": "index_refreshing"})
            return [], 0, meta
        scheduled = schedule_history_refresh(
            user.id,
            logging_style,
            warm_days=HISTORY_COLD_MISS_WARM_DAYS,
            allow_inline=False,
        )
        logger.info(
            "history_index_miss user_id=%s logging_style=%s scheduled=%s returning_empty=true",
            user.id,
            logging_style,
            scheduled,
        )
        meta.update({"refreshing": True, "refresh_reason": "index_miss"})
        return [], 0, meta

    index_days = cache_entry.get("days", [])
    built_at = cache_entry.get("built_at")
    cache_age_s = None
    if built_at:
        cache_age_s = (timezone.now() - built_at).total_seconds()
    if built_at and timezone.now() - built_at > HISTORY_STALE_AFTER:
        refresh_lock = _clean_refresh_lock(lock_key)
        if refresh_lock is None:
            scheduled = schedule_history_refresh(user.id, logging_style, warm_days=0)
            logger.info(
                "history_index_stale_refresh user_id=%s logging_style=%s scheduled=%s cache_age_s=%s",
                user.id,
                logging_style,
                scheduled,
                cache_age_s,
            )

    total_days = len(index_days)
    if total_days == 0:
        logger.info(
            "history_index_hit user_id=%s logging_style=%s days=0 cache_age_s=%s",
            user.id,
            logging_style,
            cache_age_s,
        )
        return [], 0, meta

    try:
        page_number = int(page_number)
    except (TypeError, ValueError):
        page_number = 1
    if page_number < 1:
        page_number = 1

    start_index = (page_number - 1) * HISTORY_DAYS_PER_PAGE
    end_index = start_index + HISTORY_DAYS_PER_PAGE
    page_day_keys = index_days[start_index:end_index]
    logger.info(
        "history_page_days user_id=%s logging_style=%s page=%s days_per_page=%s needed=%s",
        user.id,
        logging_style,
        page_number,
        HISTORY_DAYS_PER_PAGE,
        len(page_day_keys),
    )

    day_cache_keys = [
        _day_cache_key(user.id, logging_style, day_key)
        for day_key in page_day_keys
    ]
    day_payloads = cache.get_many(day_cache_keys)
    logger.info(
        "history_day_cache_get_many user_id=%s logging_style=%s requested=%s hit=%s miss=%s",
        user.id,
        logging_style,
        len(page_day_keys),
        len(day_payloads),
        max(len(page_day_keys) - len(day_payloads), 0),
    )
    history_days = []
    missing_days = []
    for day_key in page_day_keys:
        payload_key = _day_cache_key(user.id, logging_style, day_key)
        payload = day_payloads.get(payload_key)
        if payload is None:
            missing_days.append(day_key)
            continue
        history_days.append(_deserialize_history_day(payload))

    if missing_days and len(day_payloads) == 0:
        refresh_lock = _clean_refresh_lock(lock_key)
        scheduled = False
        if refresh_lock is None:
            scheduled = schedule_history_refresh(
                user.id,
                logging_style,
                day_keys=missing_days,
                allow_inline=False,
            )
            logger.info(
                "history_day_cache_cold_miss user_id=%s logging_style=%s missing=%s scheduled=%s returning_empty=true",
                user.id,
                logging_style,
                len(missing_days),
                scheduled,
            )
        else:
            logger.info(
                "history_day_cache_cold_miss_refreshing user_id=%s logging_style=%s missing=%s",
                user.id,
                logging_style,
                len(missing_days),
            )
        refreshing = refresh_lock is not None or scheduled
        meta.update({"refreshing": refreshing, "refresh_reason": "day_cache_cold_miss"})
        return [], total_days, meta

    built_days = {}
    if missing_days:
        build_start = time.perf_counter()
        for day_key in missing_days:
            day_payload = build_history_day(user, day_key, logging_style_override=logging_style)
            if day_payload:
                # Day has activity - cache it
                built_days[day_key] = day_payload
                cache.set(
                    _day_cache_key(user.id, logging_style, day_key),
                    _serialize_history_day(day_payload),
                    timeout=HISTORY_DAY_CACHE_TIMEOUT,
                )
            else:
                # Day has no activity - cache empty day to prevent refresh loops
                day_date = _date_from_day_key(day_key)
                if day_date:
                    empty_day = {
                        "date": day_date,
                        "weekday": formats.date_format(day_date, "l"),
                        "date_display": formats.date_format(day_date, "F j, Y"),
                        "entries": [],
                        "total_minutes": 0,
                        "total_runtime_display": "0min",
                    }
                    cache.set(
                        _day_cache_key(user.id, logging_style, day_key),
                        _serialize_history_day(empty_day),
                        timeout=HISTORY_DAY_CACHE_TIMEOUT,
                    )

        if built_days:
            history_days = []
            for day_key in page_day_keys:
                payload_key = _day_cache_key(user.id, logging_style, day_key)
                payload = day_payloads.get(payload_key)
                if payload:
                    history_days.append(_deserialize_history_day(payload))
                    continue
                day_payload = built_days.get(day_key)
                if day_payload:
                    history_days.append(day_payload)

        if len(built_days) != len(missing_days):
            refresh_lock = _clean_refresh_lock(lock_key)
            if refresh_lock is None:
                scheduled = schedule_history_refresh(user.id, logging_style, warm_days=0)
                logger.info(
                    "history_day_cache_miss user_id=%s logging_style=%s missing=%s built=%s scheduled=%s",
                    user.id,
                    logging_style,
                    len(missing_days),
                    len(built_days),
                    scheduled,
                )
            else:
                logger.info(
                    "history_day_cache_miss_refreshing user_id=%s logging_style=%s missing=%s built=%s",
                    user.id,
                    logging_style,
                    len(missing_days),
                    len(built_days),
                )
        else:
            logger.info(
                "history_day_cache_inline_build user_id=%s logging_style=%s built=%s elapsed_ms=%.2f",
                user.id,
                logging_style,
                len(built_days),
                (time.perf_counter() - build_start) * 1000,
            )

    logger.info(
        "history_index_hit user_id=%s logging_style=%s days=%s page_days=%s cache_age_s=%s elapsed_ms=%.2f",
        user.id,
        logging_style,
        total_days,
        len(history_days),
        cache_age_s,
        (time.perf_counter() - start) * 1000,
    )
    return history_days, total_days, meta


def refresh_history_cache(
    user_id: int,
    logging_style: str | None = None,
    warm_days: int | None = None,
    day_keys: Iterable | None = None,
):
    """Rebuild and store history index for a user."""
    user_model = get_user_model()
    try:
        user = user_model.objects.get(id=user_id)
        logging_style = _normalize_logging_style(logging_style, user)
    except user_model.DoesNotExist:
        # Clear lock if user doesn't exist
        cache.delete(_refresh_lock_key(user_id, logging_style or "repeats"))
        return None

    try:
        normalized_day_keys = []
        for value in day_keys or []:
            day_key = _day_key_from_value(value)
            if day_key:
                normalized_day_keys.append(day_key)
        use_specific_days = bool(normalized_day_keys)
        if use_specific_days:
            seen = set()
            requested_day_keys = []
            for key in normalized_day_keys:
                if key in seen:
                    continue
                seen.add(key)
                requested_day_keys.append(key)
        else:
            requested_day_keys = None

        if warm_days is None:
            warm_days = HISTORY_WARM_DAYS
        logger.info(
            "history_cache_refresh_start user_id=%s logging_style=%s day_keys=%s mode=%s",
            user_id,
            logging_style,
            len(requested_day_keys or []),
            "page_days" if use_specific_days else "index",
        )
        index_day_keys = build_history_index(user, logging_style_override=logging_style)
        cache_history_index(user_id, logging_style, index_day_keys)

        warm_targets = []
        if use_specific_days:
            warm_targets = requested_day_keys or []
        elif index_day_keys:
            missing_day_keys = _missing_history_day_keys(user_id, logging_style, index_day_keys)
            if missing_day_keys:
                warm_targets = missing_day_keys
            elif warm_days:
                warm_targets = index_day_keys[:warm_days]
        rebuilt = 0
        populated = 0
        for day_key in warm_targets:
            day_payload = _build_and_cache_history_day(
                user,
                day_key,
                logging_style,
            )
            rebuilt += 1
            if day_payload and day_payload.get("entries"):
                populated += 1
        logger.info(
            "history_cache_refresh_done user_id=%s logging_style=%s days=%s rebuilt=%s populated=%s",
            user_id,
            logging_style,
            len(index_day_keys),
            rebuilt,
            populated,
        )
        # Delete the refresh lock AFTER cache is saved to ensure frontend sees
        # the new cache when it detects refresh completion
        lock_key = _refresh_lock_key(user_id, logging_style)
        # Get the lock to check for dedupe_key before deleting
        refresh_lock = cache.get(lock_key)
        dedupe_key = None
        if refresh_lock and isinstance(refresh_lock, dict):
            dedupe_key = refresh_lock.get("dedupe_key")
        
        # Delete both the main lock and any dedupe_key
        cache.delete(lock_key)
        if dedupe_key and dedupe_key != lock_key:
            cache.delete(dedupe_key)
            logger.debug(
                "Deleted dedupe_key %s for user %s",
                dedupe_key,
                user_id,
            )
        
        # Verify the lock was actually deleted
        verify_lock = cache.get(lock_key)
        logger.debug(
            "History cache refresh completed for user %s, lock released. Lock key: %s, still exists: %s",
            user_id,
            lock_key,
            verify_lock is not None,
        )
        return index_day_keys
    except Exception as e:
        # Always clear the lock, even on error, to prevent it from being stuck
        logger.error("Error refreshing history cache for user %s: %s", user_id, e, exc_info=True)
        lock_key = _refresh_lock_key(user_id, logging_style)
        refresh_lock = cache.get(lock_key)
        dedupe_key = None
        if refresh_lock and isinstance(refresh_lock, dict):
            dedupe_key = refresh_lock.get("dedupe_key")
        cache.delete(lock_key)
        if dedupe_key and dedupe_key != lock_key:
            cache.delete(dedupe_key)
        raise


def repair_history_day_cache_coverage(
    user_id: int,
    logging_style: str = "repeats",
    batch_size: int | None = None,
):
    """Repair missing persisted day payloads for a user's history cache in batches."""
    user_model = get_user_model()
    try:
        user = user_model.objects.get(id=user_id)
    except user_model.DoesNotExist:
        cache.delete(_coverage_repair_key(user_id, logging_style))
        return {"rebuilt": 0, "remaining": 0, "days": 0}

    logging_style = _normalize_logging_style(logging_style, user)
    if batch_size is None:
        batch_size = HISTORY_COVERAGE_REPAIR_BATCH_SIZE

    cache_entry = cache.get(_cache_key(user_id, logging_style))
    if cache_entry:
        index_day_keys = cache_entry.get("days", [])
    else:
        index_day_keys = build_history_index(user, logging_style_override=logging_style)
        cache_history_index(user_id, logging_style, index_day_keys)

    if not index_day_keys:
        return {"rebuilt": 0, "remaining": 0, "days": 0}

    missing_day_keys = _missing_history_day_keys(user_id, logging_style, index_day_keys)
    if not missing_day_keys:
        return {"rebuilt": 0, "remaining": 0, "days": len(index_day_keys)}

    target_day_keys = (
        missing_day_keys[:batch_size]
        if batch_size and batch_size > 0
        else missing_day_keys
    )
    rebuilt = 0
    populated = 0
    for day_key in target_day_keys:
        day_payload = _build_and_cache_history_day(user, day_key, logging_style)
        rebuilt += 1
        if day_payload and day_payload.get("entries"):
            populated += 1

    remaining = max(len(missing_day_keys) - len(target_day_keys), 0)
    logger.info(
        "history_day_coverage_repair user_id=%s logging_style=%s rebuilt=%s populated=%s remaining=%s days=%s",
        user_id,
        logging_style,
        rebuilt,
        populated,
        remaining,
        len(index_day_keys),
    )
    return {
        "rebuilt": rebuilt,
        "remaining": remaining,
        "days": len(index_day_keys),
    }

