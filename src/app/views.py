import calendar
import json
import logging
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from datetime import UTC, date, timedelta
from pathlib import Path

from dateutil.relativedelta import relativedelta
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required, login_required
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import EmptyPage, Paginator
from django.db import IntegrityError
from django.db.models import prefetch_related_objects
from django.db.models.functions import ExtractDay, ExtractMonth
from django.db.utils import OperationalError
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import formats, timezone
from django.utils.dateparse import parse_date
from django.utils.timezone import datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from app import (
    cache_utils,
    credits,
    config,
    helpers,
    history_cache,
    history_processor,
    statistics_cache,
)

# history_cache is imported above
from app import (
    statistics as stats,
)
from app.forms import (
    CollectionEntryForm,
    EpisodeForm,
    ManualItemForm,
    get_form_class,
)
from app.models import (
    TV,
    Album,
    Artist,
    BasicMedia,
    CollectionEntry,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Music,
    Person,
    Season,
    Sources,
    Status,
    Track,
)
from app.providers import manual, services, tmdb
from app.services import music as sync_services
from app.templatetags import app_tags
from lists.models import CustomList
from users.models import HomeSortChoices, MediaSortChoices, MediaStatusChoices

logger = logging.getLogger(__name__)

MEDIA_RATING_CHOICES = (
    ("all", "All"),
    ("rated", "Rated"),
    # "not_rated" is handled in logic but not shown in dropdown (toggle behavior)
)
RECENTLY_NOT_RATED_KEY = "recently_not_rated"
RECENTLY_NOT_RATED_LABEL = "Recently Played - Not Rated"
RECENTLY_NOT_RATED_DAYS = 7


@require_GET
def home(request):
    """Home page with media items in progress."""
    try:
        sort_by = request.user.update_preference("home_sort", request.GET.get("sort"))
        media_type_to_load = request.GET.get("load_media_type")
        items_limit = 14

        if request.headers.get("HX-Request") and media_type_to_load == RECENTLY_NOT_RATED_KEY:
            from django.template.loader import render_to_string
            from collections import defaultdict
            from django.conf import settings
            from app.models import Album, Item, Sources
            
            recent_items = BasicMedia.objects.get_recently_unrated(
                request.user,
                days=RECENTLY_NOT_RATED_DAYS,
            )
            
            # Aggregate music tracks to albums (same logic as main view)
            music_tracks = []
            other_items = []
            albums_by_id = {}
            
            for item in recent_items:
                if item.item.media_type == MediaTypes.MUSIC.value:
                    music_tracks.append(item)
                else:
                    other_items.append(item)
            
            # Aggregate music tracks to albums
            album_play_counts = defaultdict(int)
            album_last_played = {}
            album_primary_track = {}
            
            for track in music_tracks:
                album = getattr(track, "album", None)
                if album:
                    album_id = album.id
                    albums_by_id[album_id] = album
                    play_count = getattr(track, "repeats", None) or 1
                    album_play_counts[album_id] += play_count
                    last_played = track.last_played_at or track.created_at
                    if album_id not in album_last_played or last_played > album_last_played[album_id]:
                        album_last_played[album_id] = last_played
                        album_primary_track[album_id] = track
            
            # Create AlbumAdapter for each unique album
            class AlbumAdapter:
                """Adapter to make Album compatible with media components."""
                
                def __init__(self, album, play_count, last_played_at, primary_track):
                    self.album = album
                    self.id = album.id
                    self.play_count = play_count
                    self.last_played_at = last_played_at
                    self.created_at = last_played_at
                    
                    # Media-like attributes for template compatibility
                    self.status = None  # Albums don't have status in Recently Played - Not Rated
                    self.end_date = last_played_at  # Use last_played_at as end_date
                    self.next_event = None  # Albums don't have next events
                    self.score = None  # Albums in Recently Played - Not Rated don't have scores
                    self.title = album.title  # For template title display
                    
                    album_media_id = f"album_{album.id}"
                    self.item, _ = Item.objects.get_or_create(
                        media_id=album_media_id,
                        source=Sources.MANUAL.value,
                        media_type=MediaTypes.MUSIC.value,
                        defaults={
                            "title": album.title,
                            "image": album.image or settings.IMG_NONE,
                        },
                    )
                    album_image = album.image or settings.IMG_NONE
                    if self.item.title != album.title or self.item.image != album_image:
                        self.item.title = album.title
                        self.item.image = album_image
                        self.item.save(update_fields=["title", "image"])
                    
                    self.primary_track = primary_track
                
                def __str__(self):
                    """Return album title for string representation."""
                    return self.album.title
            
            album_adapters = [
                AlbumAdapter(
                    albums_by_id[album_id],
                    album_play_counts[album_id],
                    album_last_played[album_id],
                    album_primary_track[album_id],
                )
                for album_id in albums_by_id.keys()
            ]
            
            album_adapters.sort(key=lambda a: a.last_played_at or a.created_at, reverse=True)
            all_items = album_adapters + other_items
            
            items_to_load = all_items[items_limit:]
            
            # Split items into 2:3 (standard) and 1:1 (square) types
            square_types = {"music", "podcast"}
            standard_items = []
            square_items = []
            for item in items_to_load:
                if isinstance(item, AlbumAdapter):
                    square_items.append(item)
                else:
                    media_type = getattr(getattr(item, "item", None), "media_type", "").lower() if getattr(item, "item", None) else None
                    if media_type in square_types:
                        square_items.append(item)
                    elif media_type:
                        standard_items.append(item)
            
            # Render each group with proper grid wrappers
            result_parts = []
            
            if standard_items:
                standard_context = {
                    "media_list": {
                        "items": standard_items,
                        "show_played_chip": True,
                    },
                    "user": request.user,
                    "MediaTypes": MediaTypes,
                    "csrf_token": request.META.get("CSRF_COOKIE"),
                }
                standard_html = render_to_string("app/components/home_grid.html", standard_context, request)
                result_parts.append(f'<div class="media-grid">{standard_html}</div>')
            
            if square_items:
                square_context = {
                    "media_list": {
                        "items": square_items,
                        "show_played_chip": True,
                    },
                    "user": request.user,
                    "MediaTypes": MediaTypes,
                    "csrf_token": request.META.get("CSRF_COOKIE"),
                }
                square_html = render_to_string("app/components/home_grid.html", square_context, request)
                result_parts.append(f'<div class="media-grid media-grid-square mt-4">{square_html}</div>')
            
            return HttpResponse("".join(result_parts))

        if media_type_to_load == RECENTLY_NOT_RATED_KEY:
            media_type_to_load = None

        list_by_type = BasicMedia.objects.get_in_progress(
            request.user,
            sort_by,
            items_limit,
            media_type_to_load,
        )

        # If this is an HTMX request to load more items for a specific media type
        if request.headers.get("HX-Request") and media_type_to_load:
            context = {
                "media_list": list_by_type.get(media_type_to_load, []),
            }
            return render(request, "app/components/home_grid.html", context)

        recent_items = BasicMedia.objects.get_recently_unrated(
            request.user,
            days=RECENTLY_NOT_RATED_DAYS,
        )
        if recent_items:
            # Aggregate music tracks to albums
            from collections import defaultdict
            from django.conf import settings
            from app.models import Album, Item, Sources
            
            music_tracks = []
            other_items = []
            albums_by_id = {}  # Track albums we've seen
            
            for item in recent_items:
                if item.item.media_type == MediaTypes.MUSIC.value:
                    music_tracks.append(item)
                else:
                    other_items.append(item)
            
            # Aggregate music tracks to albums
            album_play_counts = defaultdict(int)
            album_last_played = {}
            album_primary_track = {}
            
            for track in music_tracks:
                album = getattr(track, "album", None)
                if album:
                    album_id = album.id
                    albums_by_id[album_id] = album
                    # Count plays (repeats or 1)
                    play_count = getattr(track, "repeats", None) or 1
                    album_play_counts[album_id] += play_count
                    # Track most recent play
                    last_played = track.last_played_at or track.created_at
                    if album_id not in album_last_played or last_played > album_last_played[album_id]:
                        album_last_played[album_id] = last_played
                        album_primary_track[album_id] = track
            
            # Create AlbumAdapter for each unique album
            class AlbumAdapter:
                """Adapter to make Album compatible with media components."""
                
                def __init__(self, album, play_count, last_played_at, primary_track):
                    self.album = album
                    self.id = album.id
                    self.play_count = play_count
                    self.last_played_at = last_played_at
                    self.created_at = last_played_at  # For sorting
                    
                    # Media-like attributes for template compatibility
                    self.status = None  # Albums don't have status in Recently Played - Not Rated
                    self.end_date = last_played_at  # Use last_played_at as end_date
                    self.next_event = None  # Albums don't have next events
                    self.score = None  # Albums in Recently Played - Not Rated don't have scores
                    self.title = album.title  # For template title display
                    
                    # Create a mock Item for compatibility with media components
                    # Use a unique identifier for the album
                    album_media_id = f"album_{album.id}"
                    self.item, _ = Item.objects.get_or_create(
                        media_id=album_media_id,
                        source=Sources.MANUAL.value,
                        media_type=MediaTypes.MUSIC.value,
                        defaults={
                            "title": album.title,
                            "image": album.image or settings.IMG_NONE,
                        },
                    )
                    # Update item if album data changed
                    album_image = album.image or settings.IMG_NONE
                    if self.item.title != album.title or self.item.image != album_image:
                        self.item.title = album.title
                        self.item.image = album_image
                        self.item.save(update_fields=["title", "image"])
                    
                    # Store primary track for reference
                    self.primary_track = primary_track
                
                def __str__(self):
                    """Return album title for string representation."""
                    return self.album.title
            
            album_adapters = [
                AlbumAdapter(
                    albums_by_id[album_id],
                    album_play_counts[album_id],
                    album_last_played[album_id],
                    album_primary_track[album_id],
                )
                for album_id in albums_by_id.keys()
            ]
            
            # Sort albums by last played (most recent first)
            album_adapters.sort(key=lambda a: a.last_played_at or a.created_at, reverse=True)
            
            # Combine albums with other items
            all_items = album_adapters + other_items
            
            # Split items into 2:3 (standard) and 1:1 (square) types
            # This ensures both grids show if both types exist in the dataset
            square_types = {"music", "podcast"}
            
            standard_items = []
            square_items = []
            for item in all_items:
                # For AlbumAdapter, it's always music (square)
                # For other items, check media_type
                if isinstance(item, AlbumAdapter):
                    square_items.append(item)
                else:
                    media_type = item.item.media_type.lower() if item.item else None
                    if media_type in square_types:
                        square_items.append(item)
                    elif media_type:
                        standard_items.append(item)
            
            # If both types exist, show items from both types up to the limit
            # Prioritize showing both grids if both types are available
            limited_items = []
            if standard_items and square_items:
                # Show a mix: take up to half from each type (rounded up for standard)
                # This ensures both grids render
                standard_count = min(len(standard_items), (items_limit + 1) // 2)
                square_count = min(len(square_items), items_limit - standard_count)
                limited_items = standard_items[:standard_count] + square_items[:square_count]
            elif standard_items:
                limited_items = standard_items[:items_limit]
            elif square_items:
                limited_items = square_items[:items_limit]
            
            list_by_type[RECENTLY_NOT_RATED_KEY] = {
                "items": limited_items,
                "total": len(all_items),
                "section_title": RECENTLY_NOT_RATED_LABEL,
                "show_played_chip": True,
            }

        context = {
            "user": request.user,
            "list_by_type": list_by_type,
            "current_sort": sort_by,
            "sort_choices": HomeSortChoices.choices,
            "items_limit": items_limit,
        }
        return render(request, "app/home.html", context)
    except OperationalError as error:
        logger.error("Database error in home view: %s", error, exc_info=True)
        # Return empty state on database error
        context = {
            "user": request.user,
            "list_by_type": {},
            "current_sort": request.GET.get("sort", "progress"),
            "sort_choices": HomeSortChoices.choices,
            "items_limit": 14,
            "database_error": True,
        }
        return render(request, "app/home.html", context)


@require_POST
def progress_edit(request, media_type, instance_id):
    """Increase or decrease the progress of a media item from home page."""
    operation = request.POST["operation"]

    media = BasicMedia.objects.get_media_prefetch(
        request.user,
        media_type,
        instance_id,
    )

    if operation == "increase":
        media.increase_progress()
    elif operation == "decrease":
        media.decrease_progress()

    if media_type == MediaTypes.SEASON.value:
        # clear prefetch cache to get the updated episodes
        media.refresh_from_db()
        prefetch_related_objects([media], "episodes")

    context = {
        "media": media,
    }
    return render(
        request,
        "app/components/progress_changer.html",
        context,
    )


@never_cache
@require_GET
def media_list(request, media_type):
    """Return the media list page."""
    previous_sort = getattr(request.user, f"{media_type}_sort")
    layout = request.user.update_preference(
        f"{media_type}_layout",
        request.GET.get("layout"),
    )
    sort_filter = request.user.update_preference(
        f"{media_type}_sort",
        request.GET.get("sort"),
    )
    direction_param = request.GET.get("direction")
    direction_field = f"{media_type}_direction"

    # If time_left sort is selected for non-TV media types, fallback to default
    if sort_filter == "time_left" and media_type != MediaTypes.TV.value:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None

    # Resolve and persist sort direction with the same preference flow as sort
    direction_pref = getattr(request.user, direction_field, None)
    if direction_param is not None:
        direction = BasicMedia.objects.resolve_direction(sort_filter, direction_param)
        request.user.update_preference(direction_field, direction)
    else:
        if sort_filter != previous_sort or direction_pref is None:
            direction = BasicMedia.objects.resolve_direction(sort_filter, None)
        else:
            direction = BasicMedia.objects.resolve_direction(sort_filter, direction_pref)
        request.user.update_preference(direction_field, direction)
    status_filter = request.user.update_preference(
        f"{media_type}_status",
        request.GET.get("status"),
    )
    rating_filter = request.GET.get("rating", "all")
    # Allow "not_rated" even though it's not in display choices (toggle behavior)
    valid_rating_filters = {"all", "rated", "not_rated"}
    if rating_filter not in valid_rating_filters:
        rating_filter = "all"
    
    collection_filter = request.GET.get("collection", "all")
    valid_collection_filters = {"all", "collected", "not_collected"}
    if collection_filter not in valid_collection_filters:
        collection_filter = "all"

    genre_filter = (request.GET.get("genre") or "").strip()
    year_filter = (request.GET.get("year") or "").strip()
    source_filter = (request.GET.get("source") or "").strip()
    language_filter = (request.GET.get("language") or "").strip()
    country_filter = (request.GET.get("country") or "").strip()
    platform_filter = (request.GET.get("platform") or "").strip()
    origin_filter = (request.GET.get("origin") or "").strip()
    
    search_query = request.GET.get("search", "")
    try:
        page = int(request.GET.get("page", 1))
    except (ValueError, TypeError):
        page = 1

    # Prepare status filter for database query
    if not status_filter:
        status_filter = MediaStatusChoices.ALL

    def is_rated(media):
        aggregated_score = getattr(media, "aggregated_score", None)
        if aggregated_score is not None:
            return True
        return media.score is not None

    def apply_rating_filter(media_items, filter_value):
        if filter_value == "all":
            return media_items
        should_be_rated = filter_value == "rated"
        return [media for media in media_items if is_rated(media) == should_be_rated]
    
    def apply_collection_filter(media_items, filter_value, user, media_type):
        """Filter media items based on collection status.
        
        For TV shows, checks both show-level and episode-level collection entries.
        """
        if filter_value == "all":
            return media_items
        
        from app.models import Item, CollectionEntry, MediaTypes
        
        filtered_items = []
        for media in media_items:
            # Check show/item-level collection entry
            has_collection = helpers.is_item_collected(user, media.item) is not None
            
            # For TV shows, also check episode-level collection entries
            if not has_collection and media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
                episode_items = Item.objects.filter(
                    media_id=media.item.media_id,
                    source=media.item.source,
                    media_type=MediaTypes.EPISODE.value
                )
                if episode_items.exists():
                    has_episode_collection = CollectionEntry.objects.filter(
                        user=user,
                        item__in=episode_items
                    ).exists()
                    has_collection = has_episode_collection
            
            # Apply filter
            if filter_value == "collected" and has_collection:
                filtered_items.append(media)
            elif filter_value == "not_collected" and not has_collection:
                filtered_items.append(media)
        
        return filtered_items

    def _normalize_filter_value(value):
        return str(value or "").strip().lower()

    _metadata_cache = {}

    def _cached_metadata_for_item(item):
        if not item:
            return None
        cache_key = f"{item.source}_{item.media_type}_{item.media_id}"
        if cache_key in _metadata_cache:
            return _metadata_cache[cache_key]
        cached = cache.get(cache_key)
        _metadata_cache[cache_key] = cached
        return cached

    def _extract_cached_languages(item):
        cached = _cached_metadata_for_item(item)
        if not isinstance(cached, dict):
            return []
        details = cached.get("details") if isinstance(cached.get("details"), dict) else {}
        languages = details.get("languages") or cached.get("languages") or details.get("language")
        if not languages:
            return []
        if isinstance(languages, list):
            return [str(lang).strip() for lang in languages if str(lang).strip()]
        return [str(languages).strip()] if str(languages).strip() else []

    def _extract_cached_country(item):
        cached = _cached_metadata_for_item(item)
        if not isinstance(cached, dict):
            return ""
        details = cached.get("details") if isinstance(cached.get("details"), dict) else {}
        country = details.get("country") or cached.get("country")
        return str(country).strip() if country else ""

    def _extract_cached_platforms(item):
        cached = _cached_metadata_for_item(item)
        if not isinstance(cached, dict):
            return []
        details = cached.get("details") if isinstance(cached.get("details"), dict) else {}
        platforms = details.get("platforms") or cached.get("platforms")
        if not platforms:
            return []
        if isinstance(platforms, list):
            return [str(platform).strip() for platform in platforms if str(platform).strip()]
        return [str(platforms).strip()] if str(platforms).strip() else []

    def apply_genre_filter(media_items, filter_value):
        if not filter_value:
            return media_items
        target = _normalize_filter_value(filter_value)
        filtered_items = []
        for media in media_items:
            item = getattr(media, "item", None)
            genres = getattr(item, "genres", None) or []
            if any(_normalize_filter_value(genre) == target for genre in genres):
                filtered_items.append(media)
        return filtered_items

    def apply_year_filter(media_items, filter_value):
        if not filter_value:
            return media_items
        target = _normalize_filter_value(filter_value)
        if target == "unknown":
            return [
                media
                for media in media_items
                if not getattr(getattr(media, "item", None), "release_datetime", None)
            ]
        try:
            target_year = int(target)
        except (TypeError, ValueError):
            return media_items
        return [
            media
            for media in media_items
            if getattr(getattr(media, "item", None), "release_datetime", None)
            and getattr(media.item.release_datetime, "year", None) == target_year
        ]

    def apply_source_filter(media_items, filter_value):
        if not filter_value:
            return media_items
        target = str(filter_value).strip()
        return [
            media
            for media in media_items
            if getattr(getattr(media, "item", None), "source", None) == target
        ]

    def apply_language_filter(media_items, filter_value):
        if not filter_value:
            return media_items
        target = _normalize_filter_value(filter_value)
        filtered_items = []
        for media in media_items:
            item = getattr(media, "item", None)
            if not item:
                continue
            languages = _extract_cached_languages(item)
            if any(_normalize_filter_value(language) == target for language in languages):
                filtered_items.append(media)
        return filtered_items

    def apply_country_filter(media_items, filter_value):
        if not filter_value:
            return media_items
        target = _normalize_filter_value(filter_value)
        filtered_items = []
        for media in media_items:
            item = getattr(media, "item", None)
            if not item:
                continue
            country = _extract_cached_country(item)
            if country and _normalize_filter_value(country) == target:
                filtered_items.append(media)
        return filtered_items

    def apply_platform_filter(media_items, filter_value):
        if not filter_value:
            return media_items
        target = _normalize_filter_value(filter_value)
        filtered_items = []
        for media in media_items:
            item = getattr(media, "item", None)
            if not item:
                continue
            platforms = _extract_cached_platforms(item)
            if any(_normalize_filter_value(platform) == target for platform in platforms):
                filtered_items.append(media)
        return filtered_items

    def build_filter_data_from_items(media_items):
        from app.models import Sources

        genres_set = set()
        years_set = set()
        sources_set = set()
        languages_set = set()
        countries_set = set()
        platforms_set = set()
        has_unknown_year = False
        for media in media_items:
            item = getattr(media, "item", None)
            if not item:
                continue
            for genre in getattr(item, "genres", None) or []:
                genre_value = str(genre).strip()
                if genre_value:
                    genres_set.add(genre_value)
            release_dt = getattr(item, "release_datetime", None)
            if release_dt and getattr(release_dt, "year", None):
                years_set.add(release_dt.year)
            else:
                has_unknown_year = True
            if getattr(item, "source", None):
                sources_set.add(item.source)
            cached_languages = _extract_cached_languages(item)
            if cached_languages:
                languages_set.update(cached_languages)
            country_value = _extract_cached_country(item)
            if country_value:
                countries_set.add(country_value)
            platforms = _extract_cached_platforms(item)
            if platforms:
                platforms_set.update(platforms)

        genres = sorted(genres_set, key=lambda value: value.lower())
        years = [
            {"value": str(year), "label": str(year)}
            for year in sorted(years_set, reverse=True)
        ]
        if has_unknown_year:
            years.append({"value": "unknown", "label": "Unknown"})

        source_labels = dict(Sources.choices)
        sources = [
            {"value": source, "label": source_labels.get(source, source)}
            for source in sorted(sources_set)
        ]
        languages = [
            {
                "value": value,
                "label": value.upper() if len(value) <= 3 else value,
            }
            for value in sorted(languages_set)
        ]
        countries = [
            {
                "value": value,
                "label": value.upper() if len(value) <= 3 else value,
            }
            for value in sorted(countries_set)
        ]
        platforms = [
            {"value": value, "label": value}
            for value in sorted(platforms_set, key=lambda val: val.lower())
        ]
        return {
            "genres": genres,
            "years": years,
            "sources": sources,
            "languages": languages,
            "countries": countries,
            "platforms": platforms,
            "origins": [],
            "show_languages": False,
            "show_countries": False,
            "show_platforms": False,
            "show_origins": False,
        }

    # Get media list with filters applied
    media_queryset = BasicMedia.objects.get_media_list(
        user=request.user,
        media_type=media_type,
        status_filter=status_filter,
        sort_filter=sort_filter,
        search=search_query,
        direction=direction,
    )
    
    # Convert to list for filtering (rating and collection filters work on lists)
    media_list = list(media_queryset)
    filter_data = build_filter_data_from_items(media_list)
    filter_data["show_languages"] = media_type in (
        MediaTypes.TV.value,
        MediaTypes.MOVIE.value,
        MediaTypes.ANIME.value,
        MediaTypes.PODCAST.value,
    )
    filter_data["show_countries"] = media_type in (
        MediaTypes.TV.value,
        MediaTypes.MOVIE.value,
        MediaTypes.ANIME.value,
        MediaTypes.PODCAST.value,
    )
    filter_data["show_platforms"] = media_type == MediaTypes.GAME.value
    filter_data["show_origins"] = media_type == MediaTypes.MUSIC.value
    media_list = apply_rating_filter(media_list, rating_filter)
    media_list = apply_collection_filter(media_list, collection_filter, request.user, media_type)
    media_list = apply_genre_filter(media_list, genre_filter)
    media_list = apply_year_filter(media_list, year_filter)
    media_list = apply_source_filter(media_list, source_filter)
    if media_type in (MediaTypes.TV.value, MediaTypes.MOVIE.value, MediaTypes.ANIME.value):
        media_list = apply_language_filter(media_list, language_filter)
        media_list = apply_country_filter(media_list, country_filter)
    if media_type == MediaTypes.GAME.value:
        media_list = apply_platform_filter(media_list, platform_filter)

    # Handle time_left sorting for TV shows
    if sort_filter == "time_left" and media_type == MediaTypes.TV.value:
        import logging

        logger = logging.getLogger(__name__)

        # Cache sorted results for 5 minutes to avoid expensive re-sorts
        cache_key = cache_utils.build_time_left_cache_key(
            request.user.id,
            media_type,
            status_filter,
            search_query,
            direction,
            rating_filter,
            collection_filter,
            genre_filter,
            year_filter,
            source_filter,
            language_filter,
            country_filter,
            platform_filter,
            origin_filter,
        )
        cached_results = cache.get(cache_key)

        if cached_results is not None:
            logger.debug(f"DEBUG: Using cached time_left sort (page {page})")
            media_list = cached_results
        else:
            logger.debug(f"DEBUG: Starting time_left sort for page {page} (no cache)")

            # media_list already has filters applied from above
            logger.debug(f"DEBUG: Got {len(media_list)} media objects after filtering")

            # Annotate max_progress first
            BasicMedia.objects.annotate_max_progress(media_list, media_type)
            logger.debug("DEBUG: Annotated max_progress for all media")

            # Apply time_left sorting
            media_list = _sort_tv_media_by_time_left(media_list, direction)
            logger.debug("DEBUG: Applied time_left sorting")

            # Cache for 5 minutes (300 seconds)
            cache.set(cache_key, media_list, 300)
            cache_utils.register_time_left_cache_key(request.user.id, cache_key)

        # Paginate the sorted list
        items_per_page = 32
        paginator = Paginator(media_list, items_per_page)
        media_page = paginator.get_page(page)

        logger.debug(f"DEBUG: Paginated to page {page} of {paginator.num_pages} pages")
        logger.debug(f"DEBUG: This page has {len(media_page)} items")

        # Log the first few items on this page to see what's being displayed
        logger.debug(f"DEBUG: First 5 items on page {page}:")
        for i, media in enumerate(media_page[:5]):
            episodes_left = media.max_progress - media.progress if hasattr(media, "max_progress") else 0
            logger.debug(f"  {i+1}. {media.item.title} - Episodes left: {episodes_left}, Status: {getattr(media, 'status', 'Unknown')}")

        # Additional debug info for pagination issues
        logger.debug(f"DEBUG: Page {page} pagination info - has_next: {media_page.has_next()}, next_page: {media_page.next_page_number() if media_page.has_next() else 'None'}")
        if hasattr(media_page, "has_previous") and media_page.has_previous():
            logger.debug(f"DEBUG: Page {page} has previous page: {media_page.previous_page_number()}")
    else:
        # Paginate results normally
        items_per_page = 32
        paginator = Paginator(media_list, items_per_page)
        media_page = paginator.get_page(page)

        BasicMedia.objects.annotate_max_progress(
            media_page.object_list,
            media_type,
        )

    context = {
        "user": request.user,
        "media_type": media_type,
        "media_type_plural": app_tags.media_type_readable_plural(media_type).lower(),
        "media_list": media_page,
        "current_layout": layout,
        "layout_class": ".media-grid" if layout == "grid" else ".media-table",
        "current_sort": sort_filter,
        "current_direction": direction,
        "current_status": status_filter,
        "current_rating": rating_filter,
        "current_collection": collection_filter,
        "current_genre": genre_filter,
        "current_year": year_filter,
        "current_source": source_filter,
        "current_language": language_filter,
        "current_country": country_filter,
        "current_platform": platform_filter,
        "current_origin": origin_filter,
        "sort_choices": MediaSortChoices.choices,
        "status_choices": MediaStatusChoices.choices,
        "rating_choices": MEDIA_RATING_CHOICES,
        "filter_data": filter_data,
    }

    # For music, show tracked artists instead of individual tracks
    # For podcasts, show tracked shows instead of individual episodes
    # This parallels TV which shows TV shows, not seasons/episodes
    if media_type == MediaTypes.PODCAST.value:
        from django.conf import settings

        from app.models import Item, PodcastShowTracker

        show_trackers = (
            PodcastShowTracker.objects.filter(user=request.user)
            .exclude(show__title__isnull=True)
            .exclude(show__title__exact="")
            .select_related("show")
        )

        # Apply status filter to shows
        if status_filter and status_filter != MediaStatusChoices.ALL:
            show_trackers = show_trackers.filter(status=status_filter)

        # Apply search filter to shows
        if search_query:
            show_trackers = show_trackers.filter(show__title__icontains=search_query)

        # Apply rating filter to shows
        if rating_filter == "rated":
            show_trackers = show_trackers.filter(score__isnull=False)
        elif rating_filter == "not_rated":
            show_trackers = show_trackers.filter(score__isnull=True)

        # Apply sorting
        if sort_filter == "title":
            order = "show__title" if direction == "asc" else "-show__title"
            show_trackers = show_trackers.order_by(order)
        elif sort_filter == "score":
            order = "score" if direction == "asc" else "-score"
            show_trackers = show_trackers.order_by(order, "show__title")
        elif sort_filter == "start_date":
            order = "start_date" if direction == "asc" else "-start_date"
            show_trackers = show_trackers.order_by(order)
        else:
            # Default: most recently updated
            show_trackers = show_trackers.order_by("-updated_at")

        show_trackers_list = list(show_trackers)

        def _build_podcast_filter_data(trackers):
            genres_set = set()
            languages_set = set()
            for tracker in trackers:
                show = tracker.show
                for genre in (show.genres or []):
                    genre_value = str(genre).strip()
                    if genre_value:
                        genres_set.add(genre_value)
                language_value = (show.language or "").strip()
                if language_value:
                    languages_set.add(language_value)

            genres = sorted(genres_set, key=lambda value: value.lower())
            languages = [
                {"value": value, "label": value.upper() if len(value) <= 3 else value}
                for value in sorted(languages_set)
            ]
            return {
                "genres": genres,
                "years": [],
                "sources": [],
                "languages": languages,
                "countries": [],
                "platforms": [],
                "origins": [],
                "show_languages": True,
                "show_countries": True,
                "show_platforms": False,
                "show_origins": False,
            }

        filter_data = _build_podcast_filter_data(show_trackers_list)

        if genre_filter:
            target_genre = _normalize_filter_value(genre_filter)
            show_trackers_list = [
                tracker
                for tracker in show_trackers_list
                if any(
                    _normalize_filter_value(genre) == target_genre
                    for genre in (tracker.show.genres or [])
                )
            ]

        if language_filter:
            target_language = _normalize_filter_value(language_filter)
            show_trackers_list = [
                tracker
                for tracker in show_trackers_list
                if _normalize_filter_value(tracker.show.language) == target_language
            ]

        # Convert show trackers to Media-like objects for standard templates
        # Create a simple adapter class to make trackers compatible with media components
        class PodcastShowAdapter:
            """Adapter to make PodcastShowTracker compatible with media components."""

            def __init__(self, tracker):
                self.tracker = tracker
                self.id = tracker.id
                self.status = tracker.status
                self.score = tracker.score
                self.start_date = tracker.start_date
                self.end_date = tracker.end_date
                self.notes = tracker.notes
                self.created_at = tracker.created_at
                self.updated_at = tracker.updated_at

                # Create a mock Item for compatibility with media components
                # Use the show's podcast_uuid as media_id for routing
                self.item, _ = Item.objects.get_or_create(
                    media_id=tracker.show.podcast_uuid,
                    source=Sources.POCKETCASTS.value,
                    media_type=MediaTypes.PODCAST.value,
                    defaults={
                        "title": tracker.show.title,
                        "image": tracker.show.image or settings.IMG_NONE,
                    },
                )
                # Update item if show data changed
                # Always sync image to ensure it matches the show (especially after artwork fetch)
                show_image = tracker.show.image or settings.IMG_NONE
                if self.item.title != tracker.show.title or self.item.image != show_image:
                    self.item.title = tracker.show.title
                    self.item.image = show_image
                    self.item.save(update_fields=["title", "image"])

        # Convert trackers to adapters
        adapted_media = [PodcastShowAdapter(tracker) for tracker in show_trackers_list]

        # Paginate adapted media
        media_paginator = Paginator(adapted_media, 32)
        media_page = media_paginator.get_page(page)

        context = {
            "user": request.user,
            "media_list": media_page,
            "media_type": media_type,
            "media_type_plural": app_tags.media_type_readable_plural(media_type).lower(),
            "current_layout": layout,
            "layout_class": ".media-grid" if layout == "grid" else ".media-table",
            "current_sort": sort_filter,
            "current_direction": direction,
            "current_status": status_filter,
            "current_rating": rating_filter,
            "current_collection": collection_filter,
            "current_genre": genre_filter,
            "current_year": year_filter,
            "current_source": source_filter,
            "current_language": language_filter,
            "current_country": country_filter,
            "current_platform": platform_filter,
            "current_origin": origin_filter,
            "sort_choices": MediaSortChoices.choices,
            "status_choices": MediaStatusChoices.choices,
            "rating_choices": MEDIA_RATING_CHOICES,
            "search_query": search_query,
            "filter_data": filter_data,
        }

        # Handle HTMX requests for partial updates
        if request.headers.get("HX-Request"):
            is_artist_list = context.get("is_artist_list", False)

            # Changing from empty list to a status with items
            if request.headers.get("HX-Target") == "empty_list":
                response = HttpResponse()
                response["HX-Redirect"] = reverse("medialist", args=[media_type])
                return response

            # Check if this is a pagination request (has page parameter and is not the first page)
            is_pagination = request.GET.get("page") and int(request.GET.get("page", 1)) > 1
            context["is_pagination"] = bool(is_pagination)

            if layout == "grid":
                template_name = (
                    "app/components/artist_grid_items.html"
                    if is_artist_list
                    else "app/components/media_grid_items.html"
                )
            else:
                template_name = (
                    "app/components/artist_table_items.html"
                    if is_artist_list
                    else "app/components/media_table_items.html"
                )

            # --- Result-count update via HX-Trigger (keeps toolbar count in sync) ---
            from django.template.loader import render_to_string

            html = render_to_string(template_name, context, request=request)

            media_page = context.get("media_list")
            if media_page is not None and getattr(media_page, "paginator", None) is not None:
                total_count = media_page.paginator.count
            else:
                try:
                    total_count = len(media_page) if media_page is not None else 0
                except TypeError:
                    total_count = 0

            response = HttpResponse(html)
            response["HX-Trigger"] = json.dumps({"resultCountUpdated": {"count": total_count}})
            return response

        # Non-HTMX full render
        context["is_pagination"] = False
        return render(request, "app/media_list.html", context)

    if media_type == MediaTypes.MUSIC.value:
        from django.conf import settings

        from app.models import Artist, ArtistTracker
        from app.services.music import get_artist_hero_image

        artist_trackers = (
            ArtistTracker.objects.filter(user=request.user)
            .exclude(artist__name__isnull=True)
            .exclude(artist__name__exact="")
            .select_related("artist")
        )

        # Apply status filter to artists
        if status_filter and status_filter != MediaStatusChoices.ALL:
            artist_trackers = artist_trackers.filter(status=status_filter)

        # Apply search filter to artists
        if search_query:
            artist_trackers = artist_trackers.filter(artist__name__icontains=search_query)

        # Apply rating filter to artists
        if rating_filter == "rated":
            artist_trackers = artist_trackers.filter(score__isnull=False)
        elif rating_filter == "not_rated":
            artist_trackers = artist_trackers.filter(score__isnull=True)

        # Apply sorting (limited to what makes sense for artists)
        if sort_filter == "title":
            order = "artist__name" if direction == "asc" else "-artist__name"
            artist_trackers = artist_trackers.order_by(order)
        elif sort_filter == "score":
            order = "score" if direction == "asc" else "-score"
            artist_trackers = artist_trackers.order_by(order, "artist__name")
        elif sort_filter == "start_date":
            order = "start_date" if direction == "asc" else "-start_date"
            artist_trackers = artist_trackers.order_by(order)
        else:
            # Default: most recently updated
            artist_trackers = artist_trackers.order_by("-updated_at")

        artist_trackers_list = list(artist_trackers)

        def _build_music_filter_data(trackers):
            genres_set = set()
            origins_set = set()
            for tracker in trackers:
                artist = tracker.artist
                for genre in (artist.genres or []):
                    genre_value = str(genre).strip()
                    if genre_value:
                        genres_set.add(genre_value)
                origin_value = (artist.country or "").strip()
                if origin_value:
                    origins_set.add(origin_value)

            genres = sorted(genres_set, key=lambda value: value.lower())
            origins = []
            for value in sorted(origins_set):
                label = value.upper() if len(value) <= 3 else value
                try:
                    if len(value) <= 3:
                        country_name = stats._country_name_from_code(value.upper())
                        if country_name:
                            label = country_name
                except Exception:  # pragma: no cover - defensive
                    pass
                origins.append({"value": value, "label": label})
            return {
                "genres": genres,
                "years": [],
                "sources": [],
                "languages": [],
                "countries": [],
                "platforms": [],
                "origins": origins,
                "show_languages": False,
                "show_countries": False,
                "show_platforms": False,
                "show_origins": True,
            }

        filter_data = _build_music_filter_data(artist_trackers_list)

        if genre_filter:
            target_genre = _normalize_filter_value(genre_filter)
            artist_trackers_list = [
                tracker
                for tracker in artist_trackers_list
                if any(
                    _normalize_filter_value(genre) == target_genre
                    for genre in (tracker.artist.genres or [])
                )
            ]

        if origin_filter:
            target_country = _normalize_filter_value(origin_filter)
            artist_trackers_list = [
                tracker
                for tracker in artist_trackers_list
                if _normalize_filter_value(tracker.artist.country) == target_country
            ]

        # Paginate artist trackers first
        artist_paginator = Paginator(artist_trackers_list, 32)
        artist_page = artist_paginator.get_page(page)

        # Backfill missing artist images from album covers (no API calls - uses existing data)
        # Similar to _fix_missing_season_images for TV seasons
        # First, bulk fetch latest image data from DB for all artists on this page
        # (images might have been set by background tasks, detail page visits, etc.)
        # This is more efficient and reliable than individual refresh_from_db calls
        artist_ids = [tracker.artist.id for tracker in artist_page.object_list]
        artist_images_map = dict(
            Artist.objects.filter(id__in=artist_ids)
            .values_list("id", "image"),
        )

        refreshed_with_images = 0
        images_in_db_count = 0
        for tracker in artist_page.object_list:
            artist_id = tracker.artist.id
            old_image = tracker.artist.image
            # Get the latest image from DB (may be None if not in map or if DB value is None)
            # Use get() with a sentinel to distinguish "not in map" from "None in DB"
            new_image = artist_images_map.get(artist_id, object())  # object() as sentinel

            # Always update the in-memory object with the latest image from DB
            # This ensures we have the most up-to-date data, even if it's None
            if artist_id in artist_images_map:
                # Get the actual value (could be None if DB has None)
                actual_image = artist_images_map[artist_id]
                tracker.artist.image = actual_image
                # Count images that exist in DB (for logging)
                if actual_image and actual_image != settings.IMG_NONE and actual_image != "":
                    images_in_db_count += 1
                # Count if refresh found an image that wasn't there before
                if (actual_image and actual_image != settings.IMG_NONE and
                    actual_image != "" and
                    (not old_image or old_image == settings.IMG_NONE or old_image == "")):
                    refreshed_with_images += 1

        # Only backfill images for artists on the current page to avoid full queryset evaluation
        # Use object_list to avoid consuming the page iterator (important for HTMX pagination)
        artists_to_update = []
        seen_artist_ids = set()
        artist_id_to_updated_image = {}  # Track which artists got updated images
        artists_checked = 0
        artists_with_images = 0
        artists_missing_images = 0

        for tracker in artist_page.object_list:
            artist = tracker.artist
            if artist.id not in seen_artist_ids:
                seen_artist_ids.add(artist.id)
                artists_checked += 1

                # Check if artist already has an image (handle both None and empty string)
                # This check happens AFTER refresh, so we have the latest data
                has_image = artist.image and artist.image != settings.IMG_NONE and artist.image != ""
                if has_image:
                    artists_with_images += 1
                else:
                    artists_missing_images += 1
                    # Try to get hero image from albums
                    hero_image = get_artist_hero_image(artist)
                    if hero_image and hero_image != settings.IMG_NONE:
                        artist.image = hero_image
                        artists_to_update.append(artist)
                        artist_id_to_updated_image[artist.id] = hero_image

        # Log backfill attempt (always, not just when updates happen)
        is_pagination_req = bool(request.GET.get("page") and int(request.GET.get("page", 1)) > 1)
        # Use module-level logger via logging module to avoid conflict with local 'logger' variable
        # (there's a local 'logger' assignment on line 168 that makes Python treat it as local)
        import logging as _logging_module
        _log = _logging_module.getLogger(__name__)
        _log.debug(
            "Artist image backfill check (page %d, pagination=%s): checked %d artists, %d had images in DB, %d had images after refresh, %d missing, %d updated from albums",
            page,
            is_pagination_req,
            artists_checked,
            images_in_db_count,
            artists_with_images,
            artists_missing_images,
            len(artists_to_update),
        )

        if artists_to_update:
            Artist.objects.bulk_update(artists_to_update, ["image"])
            _log.info(
                "Backfilled %d artist images from album covers (page %d, pagination=%s)",
                len(artists_to_update),
                page,
                is_pagination_req,
            )

        # Ensure all tracker artist references have the correct image
        # Update in-memory objects with images we just set via bulk_update
        for tracker in artist_page.object_list:
            if tracker.artist.id in artist_id_to_updated_image:
                # Update the in-memory artist object with the new image we just set
                tracker.artist.image = artist_id_to_updated_image[tracker.artist.id]

        if refreshed_with_images > 0:
            _log.info(
                "Refreshed %d artists from DB that now have images (page %d, pagination=%s)",
                refreshed_with_images,
                page,
                is_pagination_req,
            )

        # Replace media_list with artist trackers for music
        # Use the page object directly - it's already iterable and has all pagination metadata
        # This ensures HTMX pagination works correctly and images are backfilled for new pages
        context["media_list"] = artist_page
        context["is_artist_list"] = True
        context["filter_data"] = filter_data

    # Handle HTMX requests for partial updates
    if request.headers.get("HX-Request"):
        is_artist_list = context.get("is_artist_list", False)
        # Changing from empty list to a status with items
        if request.headers.get("HX-Target") == "empty_list":
            response = HttpResponse()
            response["HX-Redirect"] = reverse("medialist", args=[media_type])
            return response

        # Check if this is a pagination request (has page parameter and is not the first page)
        is_pagination = request.GET.get("page") and int(request.GET.get("page", 1)) > 1
        context["is_pagination"] = bool(is_pagination)

        if layout == "grid":
            template_name = (
                "app/components/artist_grid_items.html"
                if is_artist_list
                else "app/components/media_grid_items.html"
            )
        else:
            template_name = (
                "app/components/artist_table_items.html"
                if is_artist_list
                else "app/components/media_table_items.html"
            )

        from django.template.loader import render_to_string

        html = render_to_string(template_name, context, request=request)

        media_page = context.get("media_list")
        if media_page is not None and getattr(media_page, "paginator", None) is not None:
            total_count = media_page.paginator.count
        else:
            try:
                total_count = len(media_page) if media_page is not None else 0
            except TypeError:
                total_count = 0

        response = HttpResponse(html)
        response["HX-Trigger"] = json.dumps({"resultCountUpdated": {"count": total_count}})
        return response

    context["is_pagination"] = False
    template_name = "app/media_list.html"

    return render(request, template_name, context)


@require_GET
def media_search(request):
    """Return the media search page."""
    media_type = request.user.update_preference(
        "last_search_type",
        request.GET["media_type"],
    )
    query = request.GET["q"]
    page = int(request.GET.get("page", 1))
    layout = request.GET.get("layout", "grid")

    local_results = []
    local_results_total = 0
    local_results_limit = 24
    local_results_kind = "media"
    if request.user.is_authenticated and query and page == 1:
        try:
            if media_type == MediaTypes.PODCAST.value:
                from django.conf import settings

                from app.models import Item, PodcastShowTracker, Sources

                show_trackers = (
                    PodcastShowTracker.objects.filter(user=request.user)
                    .exclude(show__title__isnull=True)
                    .exclude(show__title__exact="")
                    .filter(show__title__icontains=query)
                )
                local_results_total = show_trackers.count()
                show_trackers = show_trackers.order_by("show__title")[:local_results_limit]

                class PodcastShowAdapter:
                    """Adapter to make PodcastShowTracker compatible with media components."""

                    def __init__(self, tracker):
                        self.tracker = tracker
                        self.id = tracker.id
                        self.status = tracker.status
                        self.score = tracker.score
                        self.start_date = tracker.start_date
                        self.end_date = tracker.end_date
                        self.notes = tracker.notes
                        self.created_at = tracker.created_at
                        self.updated_at = tracker.updated_at

                        self.item, _ = Item.objects.get_or_create(
                            media_id=tracker.show.podcast_uuid,
                            source=Sources.POCKETCASTS.value,
                            media_type=MediaTypes.PODCAST.value,
                            defaults={
                                "title": tracker.show.title,
                                "image": tracker.show.image or settings.IMG_NONE,
                            },
                        )
                        show_image = tracker.show.image or settings.IMG_NONE
                        if self.item.title != tracker.show.title or self.item.image != show_image:
                            self.item.title = tracker.show.title
                            self.item.image = show_image
                            self.item.save(update_fields=["title", "image"])

                adapted_media = [PodcastShowAdapter(tracker) for tracker in show_trackers]
                local_results = [{"item": media.item, "media": media} for media in adapted_media]
            elif media_type == MediaTypes.MUSIC.value:
                from app.models import ArtistTracker

                artist_trackers = (
                    ArtistTracker.objects.filter(user=request.user)
                    .exclude(artist__name__isnull=True)
                    .exclude(artist__name__exact="")
                    .filter(artist__name__icontains=query)
                    .select_related("artist")
                )
                local_results_total = artist_trackers.count()
                local_results = list(artist_trackers.order_by("artist__name")[:local_results_limit])
                local_results_kind = "artists"
            else:
                local_queryset = BasicMedia.objects.get_media_list(
                    request.user,
                    media_type,
                    MediaStatusChoices.ALL,
                    "title",
                    search=query,
                    direction="asc",
                )
                local_results_total = local_queryset.count()
                local_media = list(local_queryset[:local_results_limit])
                BasicMedia.objects.annotate_max_progress(local_media, media_type)
                local_results = [{"item": media.item, "media": media} for media in local_media]
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Local search failed for %s: %s", query, exc)

    # only receives source when searching with secondary source
    source = request.GET.get(
        "source",
        config.get_default_source_name(media_type).value,
    )

    data = services.search(media_type, query, page, source)

    # Handle music's combined search format
    if media_type == MediaTypes.MUSIC.value:
        # Music returns {artists: [], releases: [], tracks: {...}}
        track_data = data.get("tracks", {})
        if track_data.get("results"):
            track_data["results"] = helpers.enrich_items_with_user_data(
                request, track_data["results"],
            )

        context = {
            "user": request.user,
            "data": track_data,  # Track results for pagination
            "music_artists": data.get("artists", []),
            "music_releases": data.get("releases", []),
            "source": source,
            "media_type": media_type,
            "layout": layout,
            "local_results": local_results,
            "local_results_total": local_results_total,
            "local_results_limit": local_results_limit,
            "local_results_kind": local_results_kind,
        }
        return render(request, "app/search_music.html", context)

    # Enrich search results with user tracking data
    if data.get("results"):
        data["results"] = helpers.enrich_items_with_user_data(request, data["results"])

    context = {
        "user": request.user,
        "data": data,
        "source": source,
        "media_type": media_type,
        "layout": layout,
        "local_results": local_results,
        "local_results_total": local_results_total,
        "local_results_limit": local_results_limit,
        "local_results_kind": local_results_kind,
    }

    return render(request, "app/search.html", context)


@login_not_required
@require_GET
def media_details(
    request, source, media_type, media_id, title,
):
    """Return the details page for a media item."""
    # Treat all anonymous views as public (no user-specific data/actions)
    is_anonymous = not request.user.is_authenticated
    public_view = is_anonymous
    public_list_view = request.GET.get("public_view") == "1" and is_anonymous

    # For public views, find a public list containing this item to get the owner
    list_owner = None
    if public_list_view:
        try:
            # Get or create the Item for this media
            item, _ = Item.objects.get_or_create(
                media_id=media_id,
                source=source,
                media_type=media_type,
                defaults={"title": "", "image": settings.IMG_NONE},
            )
            # Find a public list containing this item
            public_list = CustomList.objects.filter(
                visibility="public",
                items=item,
            ).select_related("owner").first()
            if public_list:
                list_owner = public_list.owner
        except Exception:
            # If we can't find a list owner, list_owner stays None
            pass

    # For podcast shows (identified by podcast_uuid), show show detail page
    if media_type == MediaTypes.PODCAST.value and source == Sources.POCKETCASTS.value:
        from app.models import PodcastEpisode, PodcastShow, PodcastShowTracker

        # Check if this is a show (podcast_uuid) or an episode (episode_uuid)
        show = PodcastShow.objects.filter(podcast_uuid=media_id).first()

        # If show not found, check if media_id is an iTunes ID and enrich
        if not show:
            # Check if media_id looks like an iTunes collection ID (numeric string)
            try:
                int(media_id)  # Will raise ValueError if not numeric
                # This looks like an iTunes ID, try to enrich
                import hashlib

                from django.contrib import messages
                from django.shortcuts import redirect

                from app.providers import pocketcasts
                from integrations import podcast_rss

                try:
                    # Look up podcast by iTunes ID
                    itunes_data = pocketcasts.lookup_by_itunes_id(media_id)
                    rss_feed_url = itunes_data.get("feed_url", "")

                    if not rss_feed_url:
                        messages.error(request, "Could not find RSS feed for this podcast.")
                        # Fall through to empty metadata
                    else:
                        # Check if show already exists with this RSS feed
                        existing_show = PodcastShow.objects.filter(rss_feed_url=rss_feed_url).first()
                        if existing_show:
                            # Redirect to existing show
                            from django.utils.text import slugify
                            return redirect(
                                "media_details",
                                source=Sources.POCKETCASTS.value,
                                media_type=MediaTypes.PODCAST.value,
                                media_id=existing_show.podcast_uuid,
                                title=slugify(existing_show.title or "podcast"),
                            )

                        # Create new show with iTunes ID as UUID prefix
                        podcast_uuid = f"itunes:{media_id}"

                        # Check if UUID already exists (shouldn't, but be safe)
                        if PodcastShow.objects.filter(podcast_uuid=podcast_uuid).exists():
                            show = PodcastShow.objects.get(podcast_uuid=podcast_uuid)
                        else:
                            # Try to get description from RSS feed if iTunes doesn't have it or it's empty
                            description = itunes_data.get("description", "")
                            if not description and rss_feed_url:
                                try:
                                    rss_metadata = podcast_rss.fetch_show_metadata_from_rss(rss_feed_url)
                                    description = rss_metadata.get("description", description)
                                    # Update author and language from RSS if not in iTunes data
                                    if not itunes_data.get("author") and rss_metadata.get("author"):
                                        itunes_data["author"] = rss_metadata["author"]
                                    if not itunes_data.get("language") and rss_metadata.get("language"):
                                        itunes_data["language"] = rss_metadata["language"]
                                except Exception as e:
                                    logger.debug("Failed to fetch show metadata from RSS: %s", e)

                            # Create the show
                            show = PodcastShow.objects.create(
                                podcast_uuid=podcast_uuid,
                                title=itunes_data.get("title", "Unknown Podcast"),
                                author=itunes_data.get("author", ""),
                                image=itunes_data.get("artwork_url", ""),
                                description=description,
                                genres=itunes_data.get("genres", []),
                                language=itunes_data.get("language", ""),
                                rss_feed_url=rss_feed_url,
                            )

                            # Fetch episodes from RSS feed (fetch all, no limit)
                            try:
                                episodes_data = podcast_rss.fetch_episodes_from_rss(rss_feed_url, limit=None)

                                for episode_data in episodes_data:
                                    # Generate episode UUID from GUID or create one
                                    # Use GUID directly (consistent with _sync_episodes_from_rss logic)
                                    episode_uuid = episode_data.get("guid")
                                    if not episode_uuid:
                                        # Use a hash of title + published date as fallback UUID
                                        import hashlib
                                        uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                                        episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                                    # Check if episode already exists by UUID, or try to match by title + date
                                    episode = None
                                    try:
                                        episode = PodcastEpisode.objects.get(episode_uuid=episode_uuid)
                                    except PodcastEpisode.DoesNotExist:
                                        # Try to match by title + published date
                                        if episode_data.get("title") and episode_data.get("published"):
                                            matching = PodcastEpisode.objects.filter(
                                                show=show,
                                                title__iexact=episode_data["title"].strip(),
                                                published__date=episode_data["published"].date(),
                                            ).first()
                                            if matching:
                                                episode = matching
                                    except PodcastEpisode.MultipleObjectsReturned:
                                        # If multiple found, use first one
                                        episode = PodcastEpisode.objects.filter(episode_uuid=episode_uuid).first()

                                    if not episode:
                                        PodcastEpisode.objects.create(
                                            show=show,
                                            episode_uuid=episode_uuid,
                                            title=episode_data.get("title", "Unknown Episode"),
                                            published=episode_data.get("published"),
                                            duration=episode_data.get("duration"),
                                            audio_url=episode_data.get("audio_url", ""),
                                            episode_number=episode_data.get("episode_number"),
                                            season_number=episode_data.get("season_number"),
                                        )
                            except Exception as e:
                                logger.warning("Failed to fetch episodes from RSS feed %s: %s", rss_feed_url, e)
                                # Continue without episodes

                        # Redirect to the new/enriched show
                        from django.utils.text import slugify
                        return redirect(
                            "media_details",
                            source=Sources.POCKETCASTS.value,
                            media_type=MediaTypes.PODCAST.value,
                            media_id=show.podcast_uuid,
                            title=slugify(show.title or "podcast"),
                        )
                except Exception as e:
                    logger.error("Failed to enrich podcast from iTunes ID %s: %s", media_id, e, exc_info=True)
                    messages.error(request, f"Failed to load podcast details: {e}")
                    # Fall through to empty metadata
            except ValueError:
                # media_id is not numeric, not an iTunes ID - fall through to empty metadata
                pass

        if show:
            # This is a show, not an episode - show show detail page
            tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first() if not public_view else None

            # If show has RSS feed, check if we need to fetch more episodes
            # This ensures we get the full episode list even if initial enrichment only got partial list
            if show.rss_feed_url and not public_view:
                try:
                    import hashlib

                    from integrations import podcast_rss

                    # Fetch all episodes from RSS to see what's available
                    episodes_data = podcast_rss.fetch_episodes_from_rss(show.rss_feed_url, limit=None)

                    # Get existing episode UUIDs
                    existing_uuids = set(
                        PodcastEpisode.objects.filter(show=show).values_list("episode_uuid", flat=True),
                    )

                    # Create any missing episodes
                    new_episodes_count = 0
                    for episode_data in episodes_data:
                        # Generate episode UUID from GUID or create one
                        # Use GUID directly (consistent with _sync_episodes_from_rss logic)
                        episode_uuid = episode_data.get("guid")
                        if not episode_uuid:
                            # Use a hash of title + published date as fallback UUID
                            uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                            episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                        # Check if episode already exists by UUID, or try to match by title + date
                        episode = None
                        try:
                            episode = PodcastEpisode.objects.get(episode_uuid=episode_uuid)
                        except PodcastEpisode.DoesNotExist:
                            # Try to match by title + published date
                            if episode_data.get("title") and episode_data.get("published"):
                                matching = PodcastEpisode.objects.filter(
                                    show=show,
                                    title__iexact=episode_data["title"].strip(),
                                    published__date=episode_data["published"].date(),
                                ).first()
                                if matching:
                                    episode = matching
                        except PodcastEpisode.MultipleObjectsReturned:
                            # If multiple found, use first one
                            episode = PodcastEpisode.objects.filter(episode_uuid=episode_uuid).first()

                        # Create episode if it doesn't exist
                        if not episode and episode_uuid not in existing_uuids:
                            PodcastEpisode.objects.create(
                                show=show,
                                episode_uuid=episode_uuid,
                                title=episode_data.get("title", "Unknown Episode"),
                                published=episode_data.get("published"),
                                duration=episode_data.get("duration"),
                                audio_url=episode_data.get("audio_url", ""),
                                episode_number=episode_data.get("episode_number"),
                                season_number=episode_data.get("season_number"),
                            )
                            new_episodes_count += 1
                            existing_uuids.add(episode_uuid)

                    if new_episodes_count > 0:
                        logger.info("Fetched %d additional episodes for show %s (ID: %d)", new_episodes_count, show.title, show.id)
                except Exception as e:
                    logger.debug("Failed to refresh episode list from RSS feed %s: %s", show.rss_feed_url, e)
                    # Continue with existing episodes

            # Get all episodes for this show, ordered by published date (newest first)
            # Use Coalesce to handle None published dates (put them at the end)
            from datetime import datetime

            from django.db.models import DateTimeField, Value
            from django.db.models.functions import Coalesce

            episodes = PodcastEpisode.objects.filter(show=show).annotate(
                published_or_old=Coalesce(
                    "published",
                    Value(datetime(1970, 1, 1, tzinfo=UTC),
                          output_field=DateTimeField()),
                ),
            ).order_by("-published_or_old", "-episode_number")

            # Get user's podcast entries for this show
            if not public_view:
                from app.models import Podcast
                user_podcasts = list(Podcast.objects.filter(
                    user=request.user,
                    show=show,
                ).select_related("episode", "item"))
                total_listened = len(user_podcasts)
                total_minutes = sum(podcast.progress or 0 for podcast in user_podcasts)

                # Count unplayed episodes (episodes without a completed Podcast entry for this user)
                completed_episode_ids = set(podcast.episode.id for podcast in user_podcasts if podcast.episode and podcast.end_date)
                if completed_episode_ids:
                    unplayed_count = episodes.exclude(id__in=completed_episode_ids).count()
                else:
                    # If no episodes have been completed, all episodes are unplayed
                    unplayed_count = episodes.count()
            else:
                user_podcasts = []
                total_listened = 0
                total_minutes = 0
                # For public views, still count total episodes (but button won't show due to public_view check)
                unplayed_count = episodes.count()

            # Build episode items - create Item objects for enrichment
            # Initially load first 20 episodes, rest will be loaded via infinite scroll
            episode_items_data = []
            episode_items_map = {}  # Map media_id to Item object
            initial_limit = 20
            for episode in episodes[:initial_limit]:
                item, _ = Item.objects.get_or_create(
                    media_id=episode.episode_uuid,
                    source=source,
                    media_type=media_type,
                    defaults={
                        "title": episode.title,
                        "image": show.image or settings.IMG_NONE,
                    },
                )
                # Update if needed
                if item.title != episode.title:
                    item.title = episode.title
                    item.save(update_fields=["title"])
                # enrich_items_with_user_data expects dicts with media_id, source, media_type
                episode_items_data.append({
                    "media_id": episode.episode_uuid,
                    "source": source,
                    "media_type": media_type,
                })
                episode_items_map[episode.episode_uuid] = item

            # Enrich episodes with user data
            enriched_episodes_raw = helpers.enrich_items_with_user_data(
                request,
                episode_items_data,
                user=None if public_view else request.user,
            )

            # Replace dict items with Item model instances
            enriched_episodes = []
            for enriched in enriched_episodes_raw:
                # Get the Item object from our map
                item_obj = episode_items_map.get(enriched["item"]["media_id"])
                if item_obj:
                    enriched_episodes.append({
                        "item": item_obj,
                        "media": enriched["media"],
                    })
                else:
                    # Fallback: fetch Item from database
                    enriched_episodes.append({
                        "item": Item.objects.get(
                            media_id=enriched["item"]["media_id"],
                            source=enriched["item"]["source"],
                            media_type=enriched["item"]["media_type"],
                        ),
                        "media": enriched["media"],
                    })

            # Build episode data in TV season format (inline episodes, not related items)
            episode_list = []
            for episode_obj, enriched in zip(episodes[:initial_limit], enriched_episodes):
                # Format duration
                duration_str = ""
                if episode_obj.duration:
                    hours = episode_obj.duration // 3600
                    minutes = (episode_obj.duration % 3600) // 60
                    if hours > 0:
                        duration_str = f"{hours}h {minutes}m"
                    else:
                        duration_str = f"{minutes}m"

                # Get user's podcast media for this episode
                episode_media = enriched["media"]
                episode_history = []
                if episode_media:
                    # Get history for this episode using simple_history
                    # Media instances have a .history relationship from HistoricalRecords
                    # Only include history records with end_date (completed plays)
                    episode_history = list(episode_media.history.filter(end_date__isnull=False).order_by("-end_date")[:10])

                # Create adapter objects for music-style modal (like track_modal does)
                class PodcastEpisodeAdapter:
                    """Adapter to make PodcastEpisode work like Track in template."""

                    def __init__(self, episode):
                        self.title = episode.title
                        self.track_number = episode.episode_number
                        self.duration_formatted = self._format_duration(episode.duration) if episode.duration else None
                        self.musicbrainz_recording_id = None  # Not used for podcasts
                        self.id = episode.id
                        self.published = episode.published  # For "Published date" button
                        self.episode_uuid = episode.episode_uuid  # For form submission when music is None

                    def _format_duration(self, seconds):
                        """Format duration in seconds to MM:SS or H:MM:SS."""
                        hours = seconds // 3600
                        minutes = (seconds % 3600) // 60
                        secs = seconds % 60
                        if hours > 0:
                            return f"{hours}:{minutes:02d}:{secs:02d}"
                        return f"{minutes}:{secs:02d}"

                class PodcastShowAdapter:
                    """Adapter to make PodcastShow work like Album in template."""

                    def __init__(self, show):
                        self.image = show.image or settings.IMG_NONE
                        self.release_date = None  # Podcasts don't have release dates
                        self.id = show.id

                # Get all Podcast entries for this episode to aggregate history
                all_podcasts = list(Podcast.objects.filter(
                    user=request.user if not public_view else None,
                    show=show,
                    episode=episode_obj,
                ).order_by("-end_date")) if not public_view else []

                # Create a wrapper object that aggregates history from all podcast entries
                if all_podcasts:
                    # Aggregate all history records from all podcast entries
                    # Only include history records with end_date (completed plays)
                    all_history = []
                    for podcast in all_podcasts:
                        # Only include history records with end_date (completed plays)
                        history = podcast.history.filter(end_date__isnull=False) if hasattr(podcast.history, "filter") else [h for h in podcast.history.all() if h.end_date]
                        # Convert queryset to list if needed to ensure proper evaluation
                        if hasattr(history, "__iter__") and not isinstance(history, (list, tuple)):
                            history = list(history)
                        all_history.extend(history)

                    # Sort by end_date descending (most recent first) for display
                    all_history.sort(
                        key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                        reverse=True,
                    )

                    class PodcastHistoryWrapper:
                        """Wrapper to aggregate history from multiple Podcast entries."""

                        def __init__(self, podcasts, item, history_list):
                            self.item = item
                            self.id = podcasts[0].id if podcasts else 0
                            self._podcasts = podcasts
                            self._history_list = history_list

                        @property
                        def completed_play_count(self):
                            """Return count of completed plays (history records with end_date)."""
                            # Since we already filtered all_history to only include records with end_date,
                            # we can just count the length of the filtered history_list
                            return len(self._history_list)

                        @property
                        def history(self):
                            """Return a queryset-like object that aggregates all history."""
                            class HistoryProxy:
                                def __init__(self, history_list):
                                    self._history = history_list

                                def all(self):
                                    return self._history

                                def count(self):
                                    return len(self._history)

                                def filter(self, **kwargs):
                                    # Simple filtering for history_user
                                    if "history_user" in kwargs:
                                        user = kwargs["history_user"]
                                        filtered = [h for h in self._history if getattr(h, "history_user", None) == user or getattr(h, "history_user", None) is None]
                                        return HistoryProxy(filtered)
                                    return self

                                def order_by(self, order):
                                    # Re-sort based on order string (e.g., 'end_date' or '-end_date')
                                    if order == "end_date":
                                        sorted_list = sorted(
                                            self._history,
                                            key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                                        )
                                    elif order == "-end_date":
                                        sorted_list = sorted(
                                            self._history,
                                            key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                                            reverse=True,
                                        )
                                    else:
                                        sorted_list = self._history
                                    return HistoryProxy(sorted_list)

                            return HistoryProxy(self._history_list)

                    podcast_wrapper = PodcastHistoryWrapper(all_podcasts, enriched["item"], all_history)
                else:
                    # Create a dummy Podcast object with item for template compatibility when podcast is None
                    class DummyPodcast:
                        def __init__(self, item):
                            self.item = item
                            self.id = 0
                            self.history = type("History", (), {"count": lambda: 0, "all": list})()

                    podcast_wrapper = DummyPodcast(enriched["item"])

                # Create episode dict compatible with TV episode format
                # Include media_id, source, media_type for tracking modals
                episode_item = enriched["item"]
                episode_list.append({
                    "title": episode_obj.title,
                    "episode_number": episode_obj.episode_number or 0,
                    "image": show.image or settings.IMG_NONE,  # Use show image
                    "air_date": episode_obj.published,
                    "runtime": duration_str,
                    "overview": "",  # Podcast episodes don't have descriptions from API
                    "history": episode_history,
                    "media": episode_media,
                    "item": episode_item,
                    # Add fields needed for episode tracking modals
                    "media_id": episode_item.media_id,
                    "source": episode_item.source,
                    "media_type": episode_item.media_type,
                    # Add adapter objects for music-style modal
                    "track_adapter": PodcastEpisodeAdapter(episode_obj),
                    "album_adapter": PodcastShowAdapter(show),
                    "music_wrapper": podcast_wrapper,
                })

            # Build metadata dict for show
            media_metadata = {
                "title": show.title,
                "image": show.image or settings.IMG_NONE,
                "synopsis": show.description or "",  # Use description as synopsis
                "source": source,
                "media_type": media_type,
                "media_id": media_id,
                "genres": show.genres or [],
                "details": {
                    "author": show.author,
                    "language": show.language,
                },
                "episodes": episode_list,  # Use episodes key like TV seasons
            }

            # For pagination, calculate if there are more episodes
            total_episodes_count = episodes.count()
            has_more = total_episodes_count > initial_limit
            next_page = 2 if has_more else None

            context = {
                "user": request.user,
                "media": media_metadata,
                "media_type": media_type,
                "current_instance": tracker,  # Use tracker as current_instance for compatibility
                "user_medias": user_podcasts,  # Episodes user has listened to
                "podcast_show": show,
                "podcast_tracker": tracker,
                "episodes": episode_list,  # Use episode_list with adapter objects
                "paginated_episodes": episode_list,  # For fragment compatibility
                "total_episodes": total_episodes_count,
                "total_listened": total_listened,
                "total_minutes": total_minutes,
                "public_view": public_view,
                "IMG_NONE": settings.IMG_NONE,
                "TRACK_TIME": True,
                "has_more_episodes": has_more,  # Keep for backward compatibility
                "has_more": has_more,  # For fragment compatibility
                "next_page": next_page,
                "show_id": show.id,  # For API endpoint
                "unplayed_episodes_count": unplayed_count,  # Count of unplayed episodes
            }
            return render(request, "app/media_details.html", context)

    media_metadata = services.get_media_metadata(media_type, media_id, source)

    if isinstance(media_metadata, dict):
        media_metadata.setdefault("cast", [])
        media_metadata.setdefault("crew", [])
        media_metadata.setdefault("studios_full", [])

    # For podcasts, ensure source is in metadata dict (fixes KeyError in template)
    if media_type == MediaTypes.PODCAST.value and isinstance(media_metadata, dict):
        media_metadata["source"] = source
        media_metadata["media_type"] = media_type
        media_metadata["media_id"] = media_id

    if (
        source == Sources.TMDB.value
        and media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value)
        and isinstance(media_metadata, dict)
    ):
        detail_item = Item.objects.filter(
            media_id=media_id,
            source=source,
            media_type=media_type,
        ).first()
        if detail_item:
            missing_people = not detail_item.person_credits.exists()
            missing_studios = not detail_item.studio_credits.exists()
            if missing_people or missing_studios:
                credits.sync_item_credits_from_metadata(detail_item, media_metadata)

    # For TV shows, apply fallback for seasons without posters (handles cached metadata)
    if media_type == MediaTypes.TV.value and isinstance(media_metadata, dict):
        tv_poster = media_metadata.get("image")
        if tv_poster:
            seasons = media_metadata.get("related", {}).get("seasons", [])
            for season in seasons:
                season_image = season.get("image")
                if not season_image or season_image == settings.IMG_NONE:
                    season["image"] = tv_poster

    # For public views, we don't need user media data
    if public_view:
        user_medias = []
        current_instance = None
    else:
        user_medias = list(
            BasicMedia.objects.filter_media_prefetch(
                request.user,
                media_id,
                media_type,
                source,
            ),
        )
        if user_medias:
            def _activity_key(entry):
                dates = [d for d in (entry.end_date, entry.start_date) if d]
                primary_date = max(dates) if dates else entry.created_at
                return (primary_date, entry.start_date or entry.created_at, entry.created_at)

            user_medias.sort(key=_activity_key, reverse=True)
        current_instance = user_medias[0] if user_medias else None

    # Apply the same rating aggregation logic as in the media list
    if user_medias and len(user_medias) > 1:
        latest_rating = None
        latest_activity = None

        for user_media in user_medias:
            if user_media.score is not None:
                if user_media.end_date:
                    entry_activity = user_media.end_date
                elif user_media.progressed_at:
                    entry_activity = user_media.progressed_at
                else:
                    entry_activity = user_media.created_at

                if latest_activity is None or entry_activity > latest_activity:
                    latest_activity = entry_activity
                    latest_rating = user_media.score

        if latest_rating is not None:
            current_instance.score = latest_rating

    if (
        not public_view
        and current_instance
        and media_type == MediaTypes.GAME.value
        and isinstance(media_metadata, dict)
    ):
        metadata_genres = stats._coerce_genre_list(media_metadata.get("genres"))
        item = current_instance.item
        if item:
            if metadata_genres and metadata_genres != item.genres:
                item.genres = metadata_genres
                item.save(update_fields=["genres"])
            elif item.genres:
                media_metadata["genres"] = item.genres

    play_stats = None
    if (
        not public_view
        and current_instance
        and user_medias
        and media_type in [MediaTypes.GAME.value, MediaTypes.BOARDGAME.value, MediaTypes.TV.value]
    ):
        if media_type == MediaTypes.TV.value:
            # Calculate TV show play stats from watched episodes
            total_minutes = 0
            episode_count = 0
            first_played = None
            last_played = None
            
            # Iterate through all seasons and episodes
            seasons = current_instance.seasons.all().select_related("item").prefetch_related("episodes__item")
            for season in seasons:
                episodes = season.episodes.all().select_related("item")
                for episode in episodes:
                    # Only count episodes that have been watched (have end_date)
                    if not episode.end_date:
                        continue
                    
                    # Get runtime for this episode
                    try:
                        runtime_minutes = stats._calculate_episode_time_from_cache(episode, logger)
                        if runtime_minutes > 0:
                            total_minutes += runtime_minutes
                            episode_count += 1
                            
                            # Track first and last played dates
                            if first_played is None or episode.end_date < first_played:
                                first_played = episode.end_date
                            if last_played is None or episode.end_date > last_played:
                                last_played = episode.end_date
                    except (ValueError, AttributeError):
                        # Skip episodes without runtime data
                        continue
            
            # Only create play_stats if we have watched episodes
            if episode_count > 0:
                play_stats = {
                    "first_played": first_played,
                    "last_played": last_played,
                    "total_minutes": total_minutes,
                    "total_hours": total_minutes // 60,
                    "total_minutes_remainder": total_minutes % 60,
                    "episode_count": episode_count,
                }
        else:
            # Games and boardgames calculation (existing logic)
            BasicMedia.objects._aggregate_item_data(current_instance, user_medias)
            aggregated_progress = getattr(current_instance, "aggregated_progress", None)
            if aggregated_progress is None:
                aggregated_progress = current_instance.progress or 0

            play_stats = {
                "first_played": getattr(current_instance, "aggregated_start_date", None)
                or current_instance.start_date,
                "last_played": getattr(current_instance, "aggregated_end_date", None)
                or current_instance.end_date,
            }

            if media_type == MediaTypes.GAME.value:
                total_minutes = int(aggregated_progress or 0)
                play_stats.update(
                    {
                        "total_minutes": total_minutes,
                        "total_hours": total_minutes // 60,
                        "total_minutes_remainder": total_minutes % 60,
                    },
                )
                days_played = set()
                total_minutes_for_avg = 0
                for entry in user_medias:
                    entry_minutes = entry.progress or 0
                    if entry_minutes <= 0:
                        continue
                    total_minutes_for_avg += entry_minutes
                    days_played.update(stats._get_entry_play_dates(entry))
                total_days = len(days_played)
                if total_days:
                    avg_minutes = int(round(total_minutes_for_avg / total_days))
                else:
                    avg_minutes = 0
                play_stats["avg_time_per_day"] = helpers.minutes_to_hhmm(avg_minutes)
            else:
                play_stats["total_plays"] = int(aggregated_progress or 0)

    # Enrich related items with user tracking data
    # For public views, use list owner's data if available
    if media_metadata.get("related"):
        for section_name, related_items in media_metadata["related"].items():
            if related_items:
                media_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                        user=list_owner,
                    )
                )

    # For music tracks, get linked artist and album for navigation
    music_artist = None
    music_album = None
    if media_type == MediaTypes.MUSIC.value and current_instance:
        music_artist = getattr(current_instance, "artist", None)
        music_album = getattr(current_instance, "album", None)

    notes_entry = None
    if not public_view and user_medias:
        if current_instance and current_instance.notes and current_instance.notes.strip():
            notes_entry = current_instance
        else:
            for entry in user_medias:
                if entry.notes and entry.notes.strip():
                    notes_entry = entry
                    break

    # Get collection entry for this item (if not public view and not podcast)
    collection_entry = None
    collection_stats = None
    fetching_collection_data = False
    item_id_for_polling = None
    
    if not public_view and media_type != MediaTypes.PODCAST.value:
        from app.helpers import is_item_collected, get_tv_show_collection_stats
        
        try:
            item = Item.objects.get(
                media_id=media_id,
                source=source,
                media_type=media_type,
            )
            collection_entry = is_item_collected(request.user, item)
            
            # For TV shows, also get collection statistics (episodes/seasons)
            if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
                # Use episode count from metadata if available to match Details pane
                metadata_episode_count = media_metadata.get("details", {}).get("episodes") or media_metadata.get("episodes")
                collection_stats = get_tv_show_collection_stats(request.user, item, metadata_episode_count=metadata_episode_count)
            
            # If no collection entry exists and auto-fetch is supported, trigger background fetch
            if not collection_entry and config.supports_collection_auto_fetch(media_type):
                plex_account = getattr(request.user, "plex_account", None)
                if plex_account and plex_account.plex_token:
                    from integrations.tasks import fetch_collection_metadata_for_item
                    # Trigger background task to fetch collection data
                    fetch_collection_metadata_for_item.delay(user_id=request.user.id, item_id=item.id)
                    # Use module-level logger directly to avoid UnboundLocalError
                    logging.getLogger(__name__).info("Triggered background collection fetch for %s - %s (item_id=%s)", request.user.username, item.title, item.id)
                    fetching_collection_data = True
                    item_id_for_polling = item.id
        except Item.DoesNotExist:
            pass

    context = {
        "user": request.user,
        "media": media_metadata,
        "media_type": media_type,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "music_artist": music_artist,
        "music_album": music_album,
        "public_view": public_view,
        "play_stats": play_stats,
        "notes_entry": notes_entry,
        "collection_entry": collection_entry,
        "collection_stats": collection_stats,
        "fetching_collection_data": fetching_collection_data if not public_view else False,
        "item_id_for_polling": item_id_for_polling if not public_view else None,
    }
    return render(request, "app/media_details.html", context)


def _build_missing_season_metadata(
    tv_metadata,
    media_id,
    source,
    season_number,
    episodes_in_db,
):
    """Build minimal season metadata from local items when provider data is missing."""
    tv_metadata = tv_metadata or {}
    episodes_by_number = defaultdict(list)

    for episode in episodes_in_db:
        item = getattr(episode, "item", None)
        episode_number = getattr(item, "episode_number", None)
        if episode_number is None:
            continue
        episodes_by_number[episode_number].append(episode)

    episode_numbers = sorted(episodes_by_number)
    fallback_episodes = []
    tv_image = tv_metadata.get("image") or settings.IMG_NONE
    show_title = tv_metadata.get("title", "")

    for episode_number in episode_numbers:
        history_entries = episodes_by_number[episode_number]
        episode_item = next(
            (entry.item for entry in history_entries if entry.item),
            None,
        )
        episode_image = tv_image
        air_date = None
        runtime = None
        title = f"Episode {episode_number}"

        if episode_item:
            if episode_item.image and episode_item.image != settings.IMG_NONE:
                episode_image = episode_item.image
            if episode_item.release_datetime:
                air_date = episode_item.release_datetime
            if (
                episode_item.runtime_minutes
                and episode_item.runtime_minutes < 999998
            ):
                runtime = tmdb.get_readable_duration(episode_item.runtime_minutes)
            if episode_item.title and episode_item.title != show_title:
                title = episode_item.title

        fallback_episodes.append(
            {
                "media_id": media_id,
                "media_type": MediaTypes.EPISODE.value,
                "source": source,
                "season_number": season_number,
                "episode_number": episode_number,
                "air_date": air_date,
                "image": episode_image,
                "title": title,
                "overview": "",
                "history": history_entries,
                "runtime": runtime,
                "item": episode_item,
            },
        )

    max_episode_number = max(episode_numbers) if episode_numbers else None
    details = {}
    if max_episode_number:
        details["episodes"] = max_episode_number

    air_dates = [ep["air_date"] for ep in fallback_episodes if ep.get("air_date")]
    if air_dates:
        details["first_air_date"] = min(air_dates)
        details["last_air_date"] = max(air_dates)

    source_url = tv_metadata.get("source_url") or ""
    if source == Sources.TMDB.value:
        source_url = f"https://www.themoviedb.org/tv/{media_id}/season/{season_number}"

    return {
        "media_id": media_id,
        "source": source,
        "media_type": MediaTypes.SEASON.value,
        "title": tv_metadata.get("title", ""),
        "season_title": f"Season {season_number}",
        "image": tv_image,
        "season_number": season_number,
        "synopsis": tv_metadata.get("synopsis") or "No synopsis available.",
        "genres": tv_metadata.get("genres") or [],
        "max_progress": max_episode_number,
        "score": None,
        "score_count": None,
        "details": details,
        "episodes": fallback_episodes,
        "related": {},
        "source_url": source_url,
        "tvdb_id": tv_metadata.get("tvdb_id"),
        "external_links": tv_metadata.get("external_links"),
    }


@login_not_required
@require_GET
def season_details(
    request, source, media_id, title, season_number,
):
    """Return the details page for a season."""
    # Treat all anonymous views as public (no user-specific data/actions)
    is_anonymous = not request.user.is_authenticated
    public_view = is_anonymous
    public_list_view = request.GET.get("public_view") == "1" and is_anonymous

    # For public views, find a public list containing this item to get the owner
    list_owner = None
    if public_list_view:
        try:
            # Get or create the Item for this season
            item, _ = Item.objects.get_or_create(
                media_id=media_id,
                source=source,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
                defaults={"title": "", "image": settings.IMG_NONE},
            )
            # Find a public list containing this item
            public_list = CustomList.objects.filter(
                visibility="public",
                items=item,
            ).select_related("owner").first()
            if public_list:
                list_owner = public_list.owner
        except Exception:
            # If we can't find a list owner, list_owner stays None
            pass

    tv_with_seasons_metadata = services.get_media_metadata(
        "tv_with_seasons",
        media_id,
        source,
        [season_number],
    )
    season_key = f"season/{season_number}"
    season_metadata = tv_with_seasons_metadata.get(season_key)

    # For public views, we don't need user media data
    if public_view:
        user_medias = []
        current_instance = None
    else:
        user_medias = BasicMedia.objects.filter_media_prefetch(
            request.user,
            media_id,
            MediaTypes.SEASON.value,
            source,
            season_number=season_number,
        )
        current_instance = user_medias[0] if user_medias else None

    episodes_in_db = current_instance.episodes.all() if current_instance else []

    season_metadata_missing = season_metadata is None
    if season_metadata_missing:
        season_metadata = _build_missing_season_metadata(
            tv_with_seasons_metadata,
            media_id,
            source,
            season_number,
            episodes_in_db,
        )
        if not public_view:
            messages.warning(
                request,
                "Season metadata was not found for this show. Showing local activity only.",
            )

    # Apply the same rating aggregation logic as in the media list
    if user_medias and len(user_medias) > 1:
        # Find the most recent rating among all entries
        latest_rating = None
        latest_activity = None

        for user_media in user_medias:
            if user_media.score is not None:
                # Determine the most recent activity for this entry
                entry_activity = None
                if user_media.end_date:
                    entry_activity = user_media.end_date
                elif user_media.progressed_at:
                    entry_activity = user_media.progressed_at
                else:
                    entry_activity = user_media.created_at

                # If this entry has more recent activity, use its rating
                if latest_activity is None or entry_activity > latest_activity:
                    latest_activity = entry_activity
                    latest_rating = user_media.score

        # Update the current_instance score to use the most recent rating
        if latest_rating is not None:
            current_instance.score = latest_rating

    # Save episode runtimes from raw metadata before processing for display
    # This ensures runtime data is persisted when viewing the season page
    if (
        not season_metadata_missing
        and source != Sources.MANUAL.value
        and season_metadata.get("episodes")
    ):
        from datetime import datetime
        
        raw_episodes = season_metadata["episodes"]
        current_datetime = timezone.now()
        episodes_to_update = []
        
        for episode in raw_episodes:
            episode_number = episode.get("episode_number")
            if episode_number is None:
                continue
            
            # Get or create episode item
            episode_item, _ = Item.objects.get_or_create(
                media_id=media_id,
                source=source,
                media_type=MediaTypes.EPISODE.value,
                season_number=season_number,
                episode_number=episode_number,
                defaults={"title": season_metadata.get("title", ""), "image": settings.IMG_NONE},
            )
            
            # Extract runtime from raw episode data (TMDB returns integer minutes)
            runtime_minutes = None
            if episode.get("runtime") is not None:
                runtime_minutes = int(episode["runtime"]) if episode["runtime"] > 0 else None
            elif episode.get("air_date"):
                # Check if episode has aired
                try:
                    if isinstance(episode["air_date"], str):
                        date_obj = datetime.strptime(episode["air_date"], "%Y-%m-%d")
                        air_date_dt = timezone.make_aware(date_obj, timezone.get_current_timezone())
                    else:
                        air_date_dt = episode["air_date"]
                    
                    if air_date_dt and air_date_dt.year > 1900 and air_date_dt <= current_datetime:
                        # Episode has aired but no runtime - mark as unknown (use 999998)
                        runtime_minutes = 999998
                except (ValueError, TypeError):
                    pass
            
            # Only update if runtime is actually new (not just saving the same value)
            if episode_item.runtime_minutes != runtime_minutes:
                episode_item.runtime_minutes = runtime_minutes
                episodes_to_update.append(episode_item)
        
        if episodes_to_update:
            Item.objects.bulk_update(episodes_to_update, ["runtime_minutes"], batch_size=100)
            # Invalidate time_left cache for all users (runtime affects time calculations)
            from app.cache_utils import clear_time_left_cache_for_user
            # Get all users who track this show
            tracking_users = BasicMedia.objects.filter(
                item__media_id=media_id,
                item__source=source,
                item__media_type__in=[MediaTypes.TV.value, MediaTypes.SEASON.value],
            ).values_list("user_id", flat=True).distinct()
            for user_id in tracking_users:
                clear_time_left_cache_for_user(user_id)

    if not season_metadata_missing:
        if source == Sources.MANUAL.value:
            season_metadata["episodes"] = manual.process_episodes(
                season_metadata,
                episodes_in_db,
            )
        else:
            season_metadata["episodes"] = tmdb.process_episodes(
                season_metadata,
                episodes_in_db,
            )

    # Add collection_entry data to each episode (if not public view)
    if not public_view and season_metadata.get("episodes"):
        from app.models import Item as ItemModel, CollectionEntry
        
        # Get all episode items for this season
        episode_numbers = [ep.get("episode_number") for ep in season_metadata["episodes"]]
        episode_items = ItemModel.objects.filter(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.EPISODE.value,
            season_number=season_number,
            episode_number__in=episode_numbers,
        )
        
        # Get all collection entries for these episodes in one query
        episode_item_ids = list(episode_items.values_list('id', flat=True))
        collection_entries = {}
        if episode_item_ids:
            collection_entries_qs = CollectionEntry.objects.filter(
                user=request.user,
                item_id__in=episode_item_ids,
            )
            # Map by (season_number, episode_number) for quick lookup
            for entry in collection_entries_qs:
                if entry.item.episode_number is not None:
                    collection_entries[entry.item.episode_number] = entry
        
        # Add collection_entry to each episode
        for episode in season_metadata["episodes"]:
            episode_number = episode.get("episode_number")
            episode["collection_entry"] = collection_entries.get(episode_number)

    # Enrich related items with user tracking data
    # For public views, use list owner's data if available
    if season_metadata.get("related"):
        for section_name, related_items in season_metadata["related"].items():
            if related_items:
                season_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                        user=list_owner,
                    )
                )

    # Get collection entry, stats, and metadata for this season (if not public view)
    collection_entry = None
    season_collection_stats = None
    fetching_collection_data = False
    item_id_for_polling = None
    if not public_view:
        from app.helpers import is_item_collected, get_season_collection_stats, get_season_collection_metadata
        from app.models import Item as ItemModel  # Use alias to avoid any potential shadowing
        
        # Get the season item
        try:
            season_item = ItemModel.objects.get(
                media_id=media_id,
                source=source,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
            )
            
            # Check if the show has collection data, and trigger background fetch if not
            # We check the show item (not season) because episode collection data is tied to the show
            try:
                show_item = ItemModel.objects.get(
                    media_id=media_id,
                    source=source,
                    media_type__in=(MediaTypes.TV.value, MediaTypes.ANIME.value),
                )
                show_collection_entry = is_item_collected(request.user, show_item)
                
                logger.info("Season page: Checking show %s (item_id=%s) - collection entry exists: %s", 
                           show_item.title, show_item.id, show_collection_entry is not None)
                
                # If no collection entry exists for the show and auto-fetch is supported, trigger background fetch
                if not show_collection_entry and config.supports_collection_auto_fetch(show_item.media_type):
                    plex_account = getattr(request.user, "plex_account", None)
                    if plex_account and plex_account.plex_token:
                        try:
                            from integrations.tasks import fetch_collection_metadata_for_item
                            # Trigger background task to fetch collection data for the show
                            result = fetch_collection_metadata_for_item.delay(user_id=request.user.id, item_id=show_item.id)
                            logger.info("Triggered background collection fetch for show %s - %s (item_id=%s) from season page (task_id=%s)", 
                                       request.user.username, show_item.title, show_item.id, result.id if result else "None")
                            fetching_collection_data = True
                            item_id_for_polling = show_item.id
                        except Exception as task_exc:
                            logger.error("Failed to trigger background collection fetch for show %s - %s: %s", 
                                        request.user.username, show_item.title, task_exc, exc_info=True)
                    else:
                        logger.info("Season page: User %s does not have Plex connected, skipping background fetch", request.user.username)
            except ItemModel.DoesNotExist:
                # Show item doesn't exist yet, skip background fetch
                logger.debug("Season page: Show item not found for media_id=%s, source=%s", media_id, source)
                pass
            except Exception as exc:
                logger.error("Error checking show collection entry in season_details: %s", exc, exc_info=True)
            
            # Get collection entry for the season item itself (if it exists)
            season_collection_entry = is_item_collected(request.user, season_item)
            
            # Get aggregated collection metadata from episodes (or season/show-level entry)
            season_collection_metadata = get_season_collection_metadata(request.user, season_item)
            
            # Use season-level entry if it exists, otherwise use aggregated metadata
            if season_collection_entry:
                collection_entry = season_collection_entry
            elif season_collection_metadata:
                # Check if aggregated metadata has any actual values
                has_metadata = any([
                    season_collection_metadata.get("resolution"),
                    season_collection_metadata.get("hdr"),
                    season_collection_metadata.get("audio_codec"),
                    season_collection_metadata.get("audio_channels"),
                    season_collection_metadata.get("bitrate"),
                    season_collection_metadata.get("media_type"),
                    season_collection_metadata.get("is_3d"),
                ])
                
                if has_metadata:
                    # Create a mock collection entry object from aggregated metadata
                    # This allows the template to access fields like collection_entry.resolution
                    from types import SimpleNamespace
                    collection_entry = SimpleNamespace(
                        resolution=season_collection_metadata.get("resolution") or "",
                        hdr=season_collection_metadata.get("hdr") or "",
                        audio_codec=season_collection_metadata.get("audio_codec") or "",
                        audio_channels=season_collection_metadata.get("audio_channels") or "",
                        bitrate=season_collection_metadata.get("bitrate"),
                        media_type=season_collection_metadata.get("media_type") or "",
                        is_3d=season_collection_metadata.get("is_3d", False),
                        collected_at=season_collection_metadata.get("collected_at"),
                    )
            
            # Get collection stats for this season (episodes)
            season_collection_stats = get_season_collection_stats(request.user, season_item)
        except ItemModel.DoesNotExist:
            pass

    context = {
        "user": request.user,
        "media": season_metadata,
        "tv": tv_with_seasons_metadata,
        "media_type": MediaTypes.SEASON.value,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "public_view": public_view,
        "collection_entry": collection_entry,
        "collection_stats": season_collection_stats,  # For season, this is episode stats
        "fetching_collection_data": fetching_collection_data if not public_view else False,
        "item_id_for_polling": item_id_for_polling if not public_view else None,
    }
    return render(request, "app/media_details.html", context)


@require_POST
def update_media_score(request, media_type, instance_id):
    """Update the user's score for a media item."""
    media = BasicMedia.objects.get_media(
        request.user,
        media_type,
        instance_id,
    )

    score_raw = request.POST.get("score")
    toggle = request.POST.get("toggle")
    score = None
    if score_raw is not None:
        score_raw = score_raw.strip()
        if score_raw and score_raw.lower() != "null":
            try:
                score = Decimal(score_raw)
            except (InvalidOperation, TypeError):
                return HttpResponseBadRequest("Invalid score.")
            score = request.user.scale_score_for_storage(score)
            if score is None:
                return HttpResponseBadRequest("Invalid score.")

    if toggle and score is not None and media.score == score:
        score = None

    media.score = score
    media.save()
    logger.info(
        "%s score updated to %s",
        media,
        score,
    )

    return JsonResponse(
        {
            "success": True,
            "score": request.user.format_score_for_display(score) if score is not None else None,
        },
    )


@require_POST
def update_artist_score(request, artist_id):
    """Update the user's score for an artist."""
    from django.shortcuts import get_object_or_404

    from app.models import Artist, ArtistTracker

    artist = get_object_or_404(Artist, id=artist_id)

    # Get or create the tracker for this user
    tracker, _ = ArtistTracker.objects.get_or_create(
        user=request.user,
        artist=artist,
    )

    score_raw = request.POST.get("score")
    if score_raw is None:
        return HttpResponseBadRequest("Invalid score.")
    try:
        score = Decimal(score_raw)
    except (InvalidOperation, TypeError):
        return HttpResponseBadRequest("Invalid score.")
    score = request.user.scale_score_for_storage(score)
    if score is None:
        return HttpResponseBadRequest("Invalid score.")
    tracker.score = score
    tracker.save()
    logger.info(
        "%s score updated to %s",
        artist,
        score,
    )

    # Invalidate history cache since artist ratings might appear in history entries
    # We invalidate all history days since ratings are metadata
    history_cache.invalidate_history_cache(
        request.user.id,
        force=True,
        logging_styles=("sessions", "repeats"),
    )

    return JsonResponse(
        {
            "success": True,
            "score": request.user.format_score_for_display(score),
        },
    )


@require_POST
def update_album_score(request, album_id):
    """Update the user's score for an album."""
    from django.shortcuts import get_object_or_404

    from app.models import Album, AlbumTracker

    album = get_object_or_404(Album, id=album_id)

    # Get or create the tracker for this user
    tracker, _ = AlbumTracker.objects.get_or_create(
        user=request.user,
        album=album,
    )

    score_raw = request.POST.get("score")
    if score_raw is None:
        return HttpResponseBadRequest("Invalid score.")
    try:
        score = Decimal(score_raw)
    except (InvalidOperation, TypeError):
        return HttpResponseBadRequest("Invalid score.")
    score = request.user.scale_score_for_storage(score)
    if score is None:
        return HttpResponseBadRequest("Invalid score.")
    tracker.score = score
    tracker.save()
    logger.info(
        "%s score updated to %s",
        album,
        score,
    )

    # Invalidate history cache since album ratings appear in history entries
    # We invalidate all history days since ratings are metadata displayed on all days
    # where the album appears in history
    history_cache.invalidate_history_cache(
        request.user.id,
        force=True,
        logging_styles=("sessions", "repeats"),
    )

    return JsonResponse(
        {
            "success": True,
            "score": request.user.format_score_for_display(score),
        },
    )


@require_POST
def sync_metadata(request, source, media_type, media_id, season_number=None):
    """Refresh the metadata for a media item."""
    if source == Sources.MANUAL.value:
        msg = "Manual items cannot be synced."
        messages.error(request, msg)
        return HttpResponse(
            msg,
            status=400,
            headers={"HX-Redirect": request.POST.get("next", "/")},
        )

    cache_key = f"{source}_{media_type}_{media_id}"
    if media_type == MediaTypes.SEASON.value:
        cache_key += f"_{season_number}"

    ttl = cache.ttl(cache_key)
    logger.debug("%s - Cache TTL for: %s", cache_key, ttl)

    if ttl is not None and ttl > (settings.CACHE_TIMEOUT - 3):
        msg = "The data was recently synced, please wait a few seconds."
        messages.error(request, msg)
        logger.error(msg)
    else:
        deleted = cache.delete(cache_key)
        logger.debug("%s - Old cache deleted: %s", cache_key, deleted)

        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        
        # Extract number_of_pages for books
        number_of_pages = None
        if media_type == MediaTypes.BOOK.value:
            number_of_pages = metadata.get("max_progress") or metadata.get("details", {}).get("number_of_pages")
        
        item, _ = Item.objects.update_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
                "number_of_pages": number_of_pages,
            },
        )
        
        # Update number_of_pages if it wasn't set but we have it now
        if media_type == MediaTypes.BOOK.value and not item.number_of_pages and number_of_pages:
            item.number_of_pages = number_of_pages
            item.save(update_fields=["number_of_pages"])

        if source == Sources.TMDB.value and media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
            credits.sync_item_credits_from_metadata(item, metadata)

        title = metadata["title"]
        if season_number:
            title += f" - Season {season_number}"

        if media_type == MediaTypes.SEASON.value:
            # Store raw episodes before processing (for runtime extraction)
            raw_episodes = metadata.get("episodes", [])
            
            metadata["episodes"] = tmdb.process_episodes(
                metadata,
                [],
            )

            # Create a dictionary of existing episodes keyed by episode number
            existing_episodes = {
                ep.episode_number: ep
                for ep in Item.objects.filter(
                    source=source,
                    media_type=MediaTypes.EPISODE.value,
                    media_id=media_id,
                    season_number=season_number,
                )
            }

            episodes_to_update = []
            episode_count = 0
            
            # Create a lookup for raw episode data by episode_number
            raw_episode_map = {
                ep["episode_number"]: ep
                for ep in raw_episodes
            }

            for episode_data in metadata["episodes"]:
                episode_number = episode_data["episode_number"]
                if episode_number in existing_episodes:
                    episode_item = existing_episodes[episode_number]
                    episode_item.title = metadata["title"]
                    episode_item.image = episode_data["image"]
                    
                    # Extract and update release_datetime from TMDB air_date
                    air_date = episode_data.get("air_date")
                    if air_date is not None:
                        # air_date is already converted to datetime by process_episodes
                        # or it's None if TMDB returned null
                        # Use same logic as process_season_episodes: only store meaningful dates
                        if hasattr(air_date, "year") and air_date.year > 1900:
                            episode_item.release_datetime = air_date
                        else:
                            episode_item.release_datetime = None
                    # If air_date is None, don't update release_datetime (keep existing or None)
                    
                    # Extract and update runtime_minutes from raw episode data
                    raw_episode = raw_episode_map.get(episode_number)
                    if raw_episode and raw_episode.get("runtime") is not None:
                        from app.statistics import parse_runtime_to_minutes
                        # Raw episode runtime is an integer (minutes) from TMDB
                        runtime_minutes = int(raw_episode["runtime"])
                        if runtime_minutes > 0:
                            episode_item.runtime_minutes = runtime_minutes
                    
                    episodes_to_update.append(episode_item)
                    episode_count += 1

            logger.info(
                "Found %s existing episodes to update for %s",
                episode_count,
                title,
            )

            if episodes_to_update:
                updated_count = Item.objects.bulk_update(
                    episodes_to_update,
                    ["title", "image", "release_datetime", "runtime_minutes"],
                    batch_size=100,
                )
                logger.info(
                    "Successfully updated %s episodes for %s (including release_datetime and runtime_minutes)",
                    updated_count,
                    title,
                )

        item.fetch_releases(delay=False)

        # Sync rating from Plex if user has Plex connected and webhooks configured
        _sync_plex_rating(request, item, media_type)

        msg = f"{title} was synced to {Sources(source).label} successfully."
        messages.success(request, msg)

    if request.headers.get("HX-Request"):
        return HttpResponse(
            status=204,
            headers={
                "HX-Redirect": request.POST["next"],
            },
        )
    return helpers.redirect_back(request)


def _sync_plex_rating(request, item, media_type):
    """Sync user rating from Plex for a specific item.
    
    This is called when syncing metadata if the user has Plex connected
    and webhooks configured (indicating they want Plex integration).
    """
    from app.models import CollectionEntry, MediaTypes, Status
    from integrations import plex as plex_api
    
    # Check if user has Plex connected and webhooks configured
    plex_account = getattr(request.user, "plex_account", None)
    if not plex_account or not plex_account.plex_token:
        return
    
    # Check if user has webhooks configured (has plex_usernames set)
    if not getattr(request.user, "plex_usernames", None):
        return
    
    # Only sync ratings for Movies and TV shows
    if media_type not in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
        return
    
    logger.info("Attempting to sync Plex rating for %s - %s", request.user.username, item.title)
    
    # Try to get rating key from cached CollectionEntry
    rating_key = None
    plex_uri = None
    
    collection_entry = CollectionEntry.objects.filter(
        user=request.user,
        item=item,
        plex_rating_key__isnull=False,
        plex_uri__isnull=False,
    ).first()
    
    if collection_entry:
        rating_key = collection_entry.plex_rating_key
        plex_uri = collection_entry.plex_uri
        logger.debug("Using cached rating key %s for %s", rating_key, item.title)
    else:
        # Search for item in Plex library
        try:
            resources = plex_api.list_resources(plex_account.plex_token)
        except Exception as exc:
            logger.debug("Failed to list Plex resources for rating sync: %s", exc)
            return
        
        # Get sections
        sections = plex_account.sections or []
        if not sections:
            try:
                sections = plex_api.list_sections(plex_account.plex_token)
            except Exception as exc:
                logger.debug("Failed to list Plex sections for rating sync: %s", exc)
                return
        
        # Find matching item in Plex
        for section in sections:
            section_type = (section.get("type") or "").lower()
            if media_type == MediaTypes.MOVIE.value and section_type != "movie":
                continue
            if media_type == MediaTypes.TV.value and section_type != "show":
                continue
            
            section_uri = section.get("uri")
            if not section_uri:
                continue
            
            try:
                # Search library items (first 100 should be enough for most cases)
                library_items, total = plex_api.fetch_section_all_items(
                    plex_account.plex_token,
                    section_uri,
                    str(section.get("key") or section.get("id")),
                    start=0,
                    size=100,
                )
                
                for plex_item in library_items:
                    # Extract external IDs
                    guids = plex_item.get("Guid", [])
                    if not guids:
                        single_guid = plex_item.get("guid")
                        if single_guid:
                            guids = [{"id": single_guid}]
                    
                    external_ids = plex_api.extract_external_ids_from_guids(guids)
                    
                    # Check if this matches our item
                    matches = False
                    if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                        matches = True
                    elif item.source == "imdb" and external_ids.get("imdb_id") == item.media_id:
                        matches = True
                    elif item.source == "tvdb" and external_ids.get("tvdb_id") == str(item.media_id):
                        matches = True
                    
                    if matches:
                        rating_key = plex_item.get("ratingKey") or plex_item.get("ratingkey")
                        plex_uri = section_uri
                        logger.info("Found matching Plex item for %s (rating_key=%s)", item.title, rating_key)
                        break
                
                if rating_key:
                    break
            except Exception as exc:
                logger.debug("Failed to search Plex section %s for rating: %s", section.get("title"), exc)
                continue
    
    if not rating_key or not plex_uri:
        logger.debug("Could not find Plex rating key for %s", item.title)
        return
    
    # Fetch metadata from Plex to get user rating
    # Use longer timeout for rating sync (30 seconds)
    try:
        plex_metadata = plex_api.fetch_metadata(
            plex_account.plex_token,
            plex_uri,
            str(rating_key),
            timeout=30,
        )
    except Exception as exc:
        logger.warning("Failed to fetch Plex metadata for rating sync: %s", exc)
        # Try HTTPS if HTTP failed, or vice versa
        if plex_uri.startswith("http://"):
            https_uri = plex_uri.replace("http://", "https://")
            logger.debug("Retrying with HTTPS: %s", https_uri)
            try:
                plex_metadata = plex_api.fetch_metadata(
                    plex_account.plex_token,
                    https_uri,
                    str(rating_key),
                    timeout=30,
                )
            except Exception as https_exc:
                logger.debug("HTTPS retry also failed: %s", https_exc)
                return
        elif plex_uri.startswith("https://"):
            http_uri = plex_uri.replace("https://", "http://")
            logger.debug("Retrying with HTTP: %s", http_uri)
            try:
                plex_metadata = plex_api.fetch_metadata(
                    plex_account.plex_token,
                    http_uri,
                    str(rating_key),
                    timeout=30,
                )
            except Exception as http_exc:
                logger.debug("HTTP retry also failed: %s", http_exc)
                return
        else:
            return
    
    if not plex_metadata:
        logger.debug("No Plex metadata returned for rating_key %s", rating_key)
        return
    
    user_rating = plex_metadata.get("userRating")
    if user_rating is None:
        logger.debug("No userRating found in Plex metadata for %s", item.title)
        return
    
    # Check if this is a rating removal event (-1.0)
    try:
        rating_float = float(user_rating)
        if rating_float == -1.0:
            logger.info("Detected rating removal event for %s: %s", media_type, item.title)
            # Remove rating from existing instances only
            if media_type == MediaTypes.MOVIE.value:
                from app.models import Movie
                movie_instance = Movie.objects.filter(item=item, user=request.user).first()
                if movie_instance:
                    movie_instance.score = None
                    movie_instance.save(update_fields=["score"])
                    logger.info("Removed rating for movie: %s", item.title)
                else:
                    logger.debug("No movie instance found to remove rating for %s", item.title)
            elif media_type == MediaTypes.TV.value:
                from app.models import TV
                tv_instance = TV.objects.filter(item=item, user=request.user).first()
                if tv_instance:
                    tv_instance.score = None
                    tv_instance.save(update_fields=["score"])
                    logger.info("Removed rating for TV show: %s", item.title)
                else:
                    logger.debug("No TV instance found to remove rating for %s", item.title)
            return
    except (TypeError, ValueError):
        logger.debug("Invalid rating value '%s' for %s", user_rating, item.title)
        return
    
    # Normalize rating (Plex userRating is typically 0-10, Yamtrack uses 0-10)
    if rating_float <= 10:
        normalized_rating = rating_float
    elif rating_float <= 100:
        normalized_rating = rating_float / 10
    else:
        logger.debug("Rating out of expected range: %s", user_rating)
        return
    
    normalized_rating = round(normalized_rating, 1)
    if normalized_rating < 0 or normalized_rating > 10:
        logger.debug("Normalized rating out of range: %s", normalized_rating)
        return
    
    if normalized_rating is None:
        logger.debug("Invalid rating value '%s' for %s", user_rating, item.title)
        return
    
    # Apply rating to media instance
    if media_type == MediaTypes.MOVIE.value:
        from app.models import Movie
        movie_instance = Movie.objects.filter(item=item, user=request.user).first()
        if movie_instance:
            movie_instance.score = normalized_rating
            movie_instance.save(update_fields=["score"])
            logger.info("Synced Plex rating %.1f for movie %s", normalized_rating, item.title)
        else:
            # Create movie instance if it doesn't exist
            Movie.objects.create(
                item=item,
                user=request.user,
                status=Status.COMPLETED.value,
                progress=1,
                score=normalized_rating,
            )
            logger.info("Created movie instance with Plex rating %.1f for %s", normalized_rating, item.title)
    elif media_type == MediaTypes.TV.value:
        from app.models import TV
        tv_instance = TV.objects.filter(item=item, user=request.user).first()
        if tv_instance:
            tv_instance.score = normalized_rating
            tv_instance.save(update_fields=["score"])
            logger.info("Synced Plex rating %.1f for TV show %s", normalized_rating, item.title)
        else:
            # Create TV instance if it doesn't exist
            TV.objects.create(
                item=item,
                user=request.user,
                status=Status.IN_PROGRESS.value,
                score=normalized_rating,
            )
            logger.info("Created TV instance with Plex rating %.1f for %s", normalized_rating, item.title)


@never_cache
@require_GET
def track_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
):
    """Return the tracking form for a media item."""
    # Handle podcast shows (identified by podcast_uuid)
    if media_type == MediaTypes.PODCAST.value and source == Sources.POCKETCASTS.value:
        from app.forms import PodcastShowTrackerForm
        from app.models import PodcastEpisode, PodcastShow, PodcastShowTracker

        # Check if this is a show (podcast_uuid) or an episode (episode_uuid)
        show = PodcastShow.objects.filter(podcast_uuid=media_id).first()
        if show:
            # This is a show - use PodcastShowTracker form
            tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()
            return_url = request.GET.get("return_url", "")

            initial_data = {"show_id": show.id}
            form = PodcastShowTrackerForm(
                instance=tracker,
                initial=initial_data,
                user=request.user,
            )

            return render(
                request,
                "app/components/podcast_show_track_modal.html",
                {
                    "show": show,
                    "tracker": tracker,
                    "form": form,
                    "return_url": return_url,
                },
            )

        # This is an episode (episode_uuid) - use music-style modal
        episode = PodcastEpisode.objects.filter(episode_uuid=media_id).first()
        if episode:
            from django.conf import settings

            from app.models import Item, Podcast

            show = episode.show
            instance_id = request.GET.get("instance_id")

            # Get all Podcast entries for this episode to aggregate history
            # Each Podcast entry has its own history, so we need to combine them
            all_podcasts = list(Podcast.objects.filter(
                user=request.user,
                show=show,
                episode=episode,
            ).order_by("-end_date"))

            # Get or create Item for this episode
            item, _ = Item.objects.get_or_create(
                media_id=episode.episode_uuid,
                source=source,
                media_type=media_type,
                defaults={
                    "title": episode.title,
                    "image": show.image or settings.IMG_NONE,
                    "runtime_minutes": (episode.duration // 60) if episode.duration else None,
                },
            )

            # Create adapter objects to match template expectations
            class PodcastEpisodeAdapter:
                """Adapter to make PodcastEpisode work like Track in template."""

                def __init__(self, episode):
                    self.title = episode.title
                    self.track_number = episode.episode_number
                    self.duration_formatted = self._format_duration(episode.duration) if episode.duration else None
                    self.musicbrainz_recording_id = None  # Not used for podcasts
                    self.id = episode.id
                    self.published = episode.published  # For "Published date" button
                    self.episode_uuid = episode.episode_uuid  # For form submission when music is None

                def _format_duration(self, seconds):
                    """Format duration in seconds to MM:SS or H:MM:SS."""
                    hours = seconds // 3600
                    minutes = (seconds % 3600) // 60
                    secs = seconds % 60
                    if hours > 0:
                        return f"{hours}:{minutes:02d}:{secs:02d}"
                    return f"{minutes}:{secs:02d}"

            class PodcastShowAdapter:
                """Adapter to make PodcastShow work like Album in template."""

                def __init__(self, show):
                    self.image = show.image or settings.IMG_NONE
                    self.release_date = None  # Podcasts don't have release dates
                    self.id = show.id

            # Create a wrapper object that aggregates history from all podcast entries
            # This allows the template to show all history records like music does
            if all_podcasts:
                from django.utils import timezone

                # Aggregate all history records from all podcast entries
                # Only include history records with end_date (completed plays)
                all_history = []
                for podcast in all_podcasts:
                    # Only include history records with end_date (completed plays)
                    history = podcast.history.filter(end_date__isnull=False) if hasattr(podcast.history, "filter") else [h for h in podcast.history.all() if h.end_date]
                    # Convert queryset to list if needed to ensure proper evaluation
                    if hasattr(history, "__iter__") and not isinstance(history, (list, tuple)):
                        history = list(history)
                    all_history.extend(history)

                # Sort by end_date descending (most recent first) for display
                # The template filter will re-sort if needed
                all_history.sort(
                    key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                    reverse=True,
                )

                class PodcastHistoryWrapper:
                    """Wrapper to aggregate history from multiple Podcast entries."""

                    def __init__(self, podcasts, item, history_list):
                        self.item = item
                        self.id = podcasts[0].id if podcasts else 0
                        self._podcasts = podcasts
                        self._history_list = history_list

                    @property
                    def completed_play_count(self):
                        """Return count of completed plays (history records with end_date)."""
                        # Since we already filtered all_history to only include records with end_date,
                        # we can just count the length of the filtered history_list
                        return len(self._history_list)

                    @property
                    def history(self):
                        """Return a queryset-like object that aggregates all history."""
                        class HistoryProxy:
                            def __init__(self, history_list):
                                self._history = history_list

                            def all(self):
                                return self._history

                            def count(self):
                                return len(self._history)

                            def filter(self, **kwargs):
                                # Simple filtering for history_user
                                if "history_user" in kwargs:
                                    user = kwargs["history_user"]
                                    filtered = [h for h in self._history if getattr(h, "history_user", None) == user or getattr(h, "history_user", None) is None]
                                    return HistoryProxy(filtered)
                                return self

                            def order_by(self, order):
                                # Re-sort based on order string (e.g., 'end_date' or '-end_date')
                                if order == "end_date":
                                    sorted_list = sorted(
                                        self._history,
                                        key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                                    )
                                elif order == "-end_date":
                                    sorted_list = sorted(
                                        self._history,
                                        key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                                        reverse=True,
                                    )
                                else:
                                    sorted_list = self._history
                                return HistoryProxy(sorted_list)

                        return HistoryProxy(self._history_list)

                podcast = PodcastHistoryWrapper(all_podcasts, item, all_history)
            else:
                # Create a dummy Podcast object with item for template compatibility when podcast is None
                class DummyPodcast:
                    def __init__(self, item):
                        self.item = item
                        self.id = 0
                        self.history = type("History", (), {"count": lambda: 0, "all": list})()

                    @property
                    def completed_play_count(self):
                        """Return 0 for dummy podcast (no plays)."""
                        return 0

                podcast = DummyPodcast(item)

            return render(
                request,
                "app/components/fill_track_song.html",
                {
                    "user": request.user,
                    "album": PodcastShowAdapter(show),  # Use show as "album" for template compatibility
                    "track": PodcastEpisodeAdapter(episode),  # Use episode as "track" for template compatibility
                    "music": podcast,  # Use podcast as "music" for template compatibility
                    "request": request,
                    "csrf_token": request.META.get("CSRF_COOKIE", ""),
                    "TRACK_TIME": True,
                    "IMG_NONE": settings.IMG_NONE,
                },
            )

    instance_id = request.GET.get("instance_id")
    if instance_id:
        media = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    elif request.GET.get("is_create"):
        media = None
    else:
        # no specific instance, try to find the first one
        user_medias = BasicMedia.objects.filter_media(
            request.user,
            media_id,
            media_type,
            source,
            season_number=season_number,
        )
        media = user_medias.first()
        if media:
            instance_id = media.id

    initial_data = {
        "media_id": media_id,
        "source": source,
        "media_type": media_type,
        "season_number": season_number,
        "instance_id": instance_id,
    }

    max_progress = None
    if media:
        title = media.item
        if media_type == MediaTypes.GAME.value:
            initial_data["progress"] = helpers.minutes_to_hhmm(media.progress)
        elif media_type in (MediaTypes.BOOK.value, MediaTypes.COMIC.value, MediaTypes.MANGA.value):
            # Get max_progress for percentage conversion
            if media_type == MediaTypes.BOOK.value:
                if media.item.number_of_pages:
                    max_progress = media.item.number_of_pages
                else:
                    # Try to fetch from metadata
                    try:
                        metadata = services.get_media_metadata(
                            media.item.media_type,
                            media.item.media_id,
                            media.item.source,
                        )
                        number_of_pages = metadata.get("max_progress") or metadata.get("details", {}).get("number_of_pages")
                        if number_of_pages:
                            media.item.number_of_pages = number_of_pages
                            media.item.save(update_fields=["number_of_pages"])
                            max_progress = number_of_pages
                    except Exception:
                        pass
            else:
                # For comics and manga, annotate max_progress from events
                media_list = [media]
                BasicMedia.objects.annotate_max_progress(media_list, media_type)
                if hasattr(media, "max_progress"):
                    max_progress = media.max_progress
            
            # Convert progress to percentage if preference is enabled
            if request.user.book_comic_manga_progress_percentage and max_progress and media.progress:
                percentage = round((media.progress / max_progress) * 100, 1)
                initial_data["progress"] = percentage
    else:
        title = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )["title"]
        if media_type == MediaTypes.SEASON.value:
            title += f" S{season_number}"

    form_class = get_form_class(media_type)
    # Only pass user and max_progress for book/comic/manga forms that handle them
    if media_type in (MediaTypes.BOOK.value, MediaTypes.COMIC.value, MediaTypes.MANGA.value):
        form = form_class(
            instance=media,
            initial=initial_data,
            user=request.user,
            max_progress=max_progress,
        )
    else:
        form = form_class(
            instance=media,
            initial=initial_data,
            user=request.user,
        )

    response = render(
        request,
        "app/components/fill_track.html",
        {
            "user": request.user,
            "title": title,
            "form": form,
            "media": media,
            "return_url": request.GET.get("return_url", ""),
            "max_progress": max_progress,
        },
    )
    # Explicitly set cache control headers for Safari compatibility
    # @never_cache should handle this, but Safari can be aggressive with caching
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


@require_POST
def media_save(request):
    """Save or update media data to the database."""
    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    season_number = request.POST.get("season_number")
    instance_id = request.POST.get("instance_id")
    
    # Handle percentage conversion for books/comics/manga
    progress_value = request.POST.get("progress")
    if progress_value and media_type in (MediaTypes.BOOK.value, MediaTypes.COMIC.value, MediaTypes.MANGA.value):
        if request.user.book_comic_manga_progress_percentage:
            # Make POST mutable for modification
            mutable_post = request.POST.copy()
            max_progress = None
            item = None
            
            # Get item to determine max_progress
            if instance_id:
                instance = BasicMedia.objects.get_media(
                    request.user,
                    media_type,
                    instance_id,
                )
                if instance:
                    item = instance.item
            else:
                # For new entries, get metadata first to get/create item
                metadata = services.get_media_metadata(
                    media_type,
                    media_id,
                    source,
                    [season_number],
                )
                if media_type == MediaTypes.BOOK.value:
                    number_of_pages = metadata.get("max_progress") or metadata.get("details", {}).get("number_of_pages")
                else:
                    number_of_pages = None
                item, _ = Item.objects.get_or_create(
                    media_id=media_id,
                    source=source,
                    media_type=media_type,
                    season_number=season_number,
                    defaults={
                        "title": metadata["title"],
                        "image": metadata["image"],
                        "number_of_pages": number_of_pages,
                    },
                )
            
            if item:
                if media_type == MediaTypes.BOOK.value:
                    max_progress = item.number_of_pages
                    if not max_progress:
                        # Try to fetch from metadata
                        try:
                            metadata = services.get_media_metadata(
                                item.media_type,
                                item.media_id,
                                item.source,
                            )
                            number_of_pages = metadata.get("max_progress") or metadata.get("details", {}).get("number_of_pages")
                            if number_of_pages:
                                item.number_of_pages = number_of_pages
                                item.save(update_fields=["number_of_pages"])
                                max_progress = number_of_pages
                        except Exception:
                            pass
                else:
                    # For comics and manga, need to get max_progress from events
                    from app.models import Manga, Comic
                    model_class = Manga if media_type == MediaTypes.MANGA.value else Comic
                    media_list = list(model_class.objects.filter(user=request.user, item=item).select_related("item"))
                    if media_list:
                        BasicMedia.objects.annotate_max_progress(media_list, media_type)
                        if hasattr(media_list[0], "max_progress"):
                            max_progress = media_list[0].max_progress
                
                if max_progress:
                    try:
                        percentage = float(progress_value)
                        converted_progress = round((percentage / 100) * max_progress)
                        mutable_post["progress"] = str(converted_progress)
                        request.POST = mutable_post
                    except (ValueError, TypeError):
                        pass

    if instance_id:
        instance = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    else:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        # Extract runtime from metadata
        runtime_minutes = None
        if metadata.get("details", {}).get("runtime"):
            from app.statistics import parse_runtime_to_minutes
            runtime_minutes = parse_runtime_to_minutes(metadata["details"]["runtime"])

        # Extract number_of_pages for books
        number_of_pages = None
        if media_type == MediaTypes.BOOK.value:
            # Try max_progress first (from metadata dict), then details.number_of_pages
            number_of_pages = metadata.get("max_progress") or metadata.get("details", {}).get("number_of_pages")

        metadata_genres = []
        if media_type == MediaTypes.GAME.value:
            metadata_genres = stats._coerce_genre_list(metadata.get("genres"))

        item, created = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
                "runtime_minutes": runtime_minutes,
                "number_of_pages": number_of_pages,
                "genres": metadata_genres,
            },
        )

        # Update image, runtime, and number_of_pages if they're not set and we have them now
        needs_save = False
        if item.image == settings.IMG_NONE and metadata.get("image"):
            item.image = metadata["image"]
            needs_save = True
        if not item.runtime_minutes and runtime_minutes:
            item.runtime_minutes = runtime_minutes
            needs_save = True
        if not item.number_of_pages and number_of_pages:
            item.number_of_pages = number_of_pages
            needs_save = True
        if metadata_genres and metadata_genres != item.genres:
            item.genres = metadata_genres
            needs_save = True
        if needs_save:
            item.save()

        if source == Sources.TMDB.value and media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
            credits.sync_item_credits_from_metadata(item, metadata)

        model = apps.get_model(app_label="app", model_name=media_type)
        instance = model(item=item, user=request.user)

        # For music tracks, create/link Artist and Album
        if media_type == MediaTypes.MUSIC.value:
            artist_instance = None
            album_instance = None
            track_genres = metadata.get("genres", [])

            # Create or get Artist
            artist_id = metadata.get("_artist_id") or metadata.get("details", {}).get("artist_id")
            artist_name = metadata.get("_artist_name") or metadata.get("details", {}).get("artist")
            if artist_id and artist_name:
                artist_instance, _ = Artist.objects.get_or_create(
                    musicbrainz_id=artist_id,
                    defaults={"name": artist_name},
                )
            elif artist_name:
                # Try to find by name if no ID
                artist_instance = Artist.objects.filter(name=artist_name).first()
                if not artist_instance:
                    artist_instance = Artist.objects.create(name=artist_name)

            # Create or get Album
            album_id = metadata.get("_album_id") or metadata.get("details", {}).get("album_id")
            album_title = metadata.get("_album_title") or metadata.get("details", {}).get("album")
            image_url = metadata.get("image", "")
            release_date = None
            release_date_str = metadata.get("details", {}).get("release_date")
            if release_date_str:
                release_date = _parse_release_date_str(release_date_str)

            if album_id and album_title:
                album_instance, created = Album.objects.get_or_create(
                    musicbrainz_release_id=album_id,
                    defaults={
                        "title": album_title,
                        "artist": artist_instance,
                        "image": image_url,
                        "release_date": release_date,
                        "genres": track_genres,
                    },
                )
                # Update album image if it's missing
                if not created and image_url and image_url != settings.IMG_NONE:
                    if not album_instance.image or album_instance.image == settings.IMG_NONE:
                        album_instance.image = image_url
                        album_instance.save(update_fields=["image"])
                # Fill release_date/genres if missing
                if not album_instance.release_date and release_date:
                    album_instance.release_date = release_date
                    album_instance.save(update_fields=["release_date"])
                if not album_instance.genres and track_genres:
                    album_instance.genres = track_genres
                    album_instance.save(update_fields=["genres"])
            elif album_title:
                # Try to find by title and artist if no ID
                album_instance = Album.objects.filter(
                    title=album_title,
                    artist=artist_instance,
                ).first()
                if not album_instance:
                    album_instance = Album.objects.create(
                        title=album_title,
                        artist=artist_instance,
                        image=image_url,
                        release_date=release_date,
                        genres=track_genres,
                    )

            instance.artist = artist_instance
            instance.album = album_instance

            # Link to Track if it exists (for album-based additions)
            if album_instance:
                track_instance = Track.objects.filter(
                    album=album_instance,
                    musicbrainz_recording_id=media_id,
                ).first()
                if track_instance:
                    instance.track = track_instance

    # Validate the form and save the instance if it's valid
    form_class = get_form_class(media_type)
    form = form_class(request.POST, instance=instance, user=request.user)
    if form.is_valid():
        form.save()
        logger.info("%s saved successfully.", form.instance)
    else:
        logger.error(form.errors.as_json())
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(
                    request,
                    f"{field.replace('_', ' ').title()}: {error}",
                )

    return helpers.redirect_back(request)


@require_POST
def media_delete(request):
    """Delete media data from the database."""
    instance_id = request.POST["instance_id"]
    media_type = request.POST["media_type"]
    model = apps.get_model(app_label="app", model_name=media_type)

    try:
        media = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
        media.delete()
        logger.info("%s deleted successfully.", media)

    except model.DoesNotExist:
        logger.warning("The %s was already deleted before.", media_type)

    return helpers.redirect_back(request)


@require_POST
def episode_save(request):
    """Handle the creation, deletion, and updating of episodes for a season."""
    media_id = request.POST["media_id"]
    season_number = int(request.POST["season_number"])
    episode_number = int(request.POST["episode_number"])
    source = request.POST["source"]

    form = EpisodeForm(request.POST)
    if not form.is_valid():
        logger.error("Form validation failed: %s", form.errors)
        return HttpResponseBadRequest("Invalid form data")

    try:
        related_season = Season.objects.get(
            item__media_id=media_id,
            item__source=source,
            item__season_number=season_number,
            item__episode_number=None,
            user=request.user,
        )
    except Season.DoesNotExist:
        tv_with_seasons_metadata = services.get_media_metadata(
            "tv_with_seasons",
            media_id,
            source,
            [season_number],
        )
        season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

        # Use season poster if available, otherwise fallback to TV show poster
        season_image = season_metadata.get("image") or tv_with_seasons_metadata.get("image")

        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                "title": tv_with_seasons_metadata["title"],
                "image": season_image,
            },
        )
        related_season = Season.objects.create(
            item=item,
            user=request.user,
            score=None,
            status=Status.IN_PROGRESS.value,
            notes="",
        )

        logger.info("%s did not exist, it was created successfully.", related_season)

    related_season.watch(episode_number, form.cleaned_data["end_date"])

    return helpers.redirect_back(request)


@require_http_methods(["GET", "POST"])
def create_entry(request):
    """Return the form for manually adding media items."""
    if request.method == "GET":
        media_types = MediaTypes.values
        return render(request, "app/create_entry.html", {"media_types": media_types})

    # Process the form submission
    form = ManualItemForm(request.POST, user=request.user)
    if not form.is_valid():
        # Handle form validation errors
        logger.error(form.errors.as_json())
        helpers.form_error_messages(form, request)
        return redirect("create_entry")

    # Try to save the item
    try:
        item = form.save()
    except IntegrityError:
        # Handle duplicate item
        media_name = form.cleaned_data["title"]
        if form.cleaned_data.get("season_number"):
            media_name += f" - Season {form.cleaned_data['season_number']}"
        if form.cleaned_data.get("episode_number"):
            media_name += f" - Episode {form.cleaned_data['episode_number']}"

        logger.exception("%s already exists in the database.", media_name)
        messages.error(request, f"{media_name} already exists in the database.")
        return redirect("create_entry")

    # Prepare and validate the media form
    updated_request = request.POST.copy()
    updated_request.update({"source": item.source, "media_id": item.media_id})
    media_form = get_form_class(item.media_type)(updated_request, user=request.user)

    if not media_form.is_valid():
        # Handle media form validation errors
        logger.error(media_form.errors.as_json())
        helpers.form_error_messages(media_form, request)

        # Delete the item since the media creation failed
        item.delete()
        logger.info("%s was deleted due to media form validation failure", item)
        return redirect("create_entry")

    # Save the media instance
    media_form.instance.user = request.user
    media_form.instance.item = item

    # Handle relationships based on media type
    if item.media_type == MediaTypes.SEASON.value:
        media_form.instance.related_tv = form.cleaned_data["parent_tv"]
    elif item.media_type == MediaTypes.EPISODE.value:
        media_form.instance.related_season = form.cleaned_data["parent_season"]

    media_form.save()

    # Success message
    msg = f"{item} added successfully."
    messages.success(request, msg)
    logger.info(msg)

    return redirect("create_entry")


@require_GET
def search_parent_tv(request):
    """Return the search results for parent TV shows."""
    query = request.GET.get("q", "").strip()

    if len(query) <= 1:
        return render(request, "app/components/search_parent_tv.html")

    logger.debug(
        "%s - Searching for TV shows with query: %s",
        request.user.username,
        query,
    )

    parent_tvs = TV.objects.filter(
        user=request.user,
        item__source=Sources.MANUAL.value,
        item__media_type=MediaTypes.TV.value,
        item__title__icontains=query,
    )[:5]

    return render(
        request,
        "app/components/search_parent_tv.html",
        {"results": parent_tvs, "query": query},
    )


@require_GET
def search_parent_season(request):
    """Return the search results for parent seasons."""
    query = request.GET.get("q", "").strip()

    if len(query) <= 1:
        return render(request, "app/components/search_parent_tv.html")

    logger.debug(
        "%s - Searching for seasons with query: %s",
        request.user.username,
        query,
    )

    parent_seasons = Season.objects.filter(
        user=request.user,
        item__source=Sources.MANUAL.value,
        item__media_type=MediaTypes.SEASON.value,
        item__title__icontains=query,
    )[:5]

    return render(
        request,
        "app/components/search_parent_season.html",
        {"results": parent_seasons, "query": query},
    )


@require_GET
def history_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    episode_number=None,
):
    """Return the history page for a media item."""
    instance_id = request.GET.get("instance_id")
    if instance_id:
        try:
            media = BasicMedia.objects.get_media(
                request.user,
                media_type,
                instance_id,
            )
            user_medias = [media]
        except (ObjectDoesNotExist, ValueError, TypeError):
            user_medias = BasicMedia.objects.filter_media(
                request.user,
                media_id,
                media_type,
                source,
                season_number=season_number,
                episode_number=episode_number,
            )
    else:
        user_medias = BasicMedia.objects.filter_media(
            request.user,
            media_id,
            media_type,
            source,
            season_number=season_number,
            episode_number=episode_number,
        )

    try:
        total_medias = user_medias.count()
    except TypeError:
        total_medias = len(user_medias)
    timeline_entries = []
    for index, media in enumerate(user_medias, start=1):
        # Filter history to only include records with end_date (completed plays)
        # This prevents showing invalid history records from in-progress episodes
        history = (
            media.history.filter(end_date__isnull=False)
            if hasattr(media.history, "filter")
            else [h for h in media.history.all() if h.end_date]
        )
        if history:
            media_entry_number = total_medias - index + 1
            timeline_entries.extend(
                history_processor.process_history_entries(
                    history,
                    media_type,
                    media_entry_number,
                    request.user,
                ),
            )
    return render(
        request,
        "app/components/fill_history.html",
        {
            "user": request.user,
            "media_type": media_type,
            "timeline": timeline_entries,
            "total_medias": total_medias,
            "return_url": request.GET.get("return_url", ""),
        },
    )


@require_http_methods(["DELETE"])
def delete_history_record(request, media_type, history_id):
    """Delete a specific history record."""
    try:
        historical_model = apps.get_model(
            app_label="app",
            model_name=f"historical{media_type.lower()}",
        )

        # Try to get the history record, checking both with and without history_user
        # This handles cases where history_user might be null (e.g., from old imports)
        try:
            history_record = historical_model.objects.get(
                history_id=history_id,
                history_user=request.user,
            )
        except historical_model.DoesNotExist:
            # If not found with history_user, check if history_user is null
            # and verify the record belongs to the user via the actual model instance
            history_record = historical_model.objects.get(
                history_id=history_id,
                history_user__isnull=True,
            )
            try:
                BasicMedia.objects.get_media(
                    request.user,
                    media_type.lower(),
                    history_record.id,
                )
            except ObjectDoesNotExist:
                raise historical_model.DoesNotExist(
                    f"History record {history_id} not found for user {request.user}",
                )

        # Capture all needed data BEFORE deletion to ensure we have it for cache invalidation
        # and verification, even if the object becomes invalid after deletion
        media_instance_id = history_record.id
        start_date = getattr(history_record, "start_date", None)
        end_date = getattr(history_record, "end_date", None)
        created_at = getattr(history_record, "created_at", None)
        media_type_lower = media_type.lower()

        # These media types store each play as a separate model instance.
        # Deleting only the historical record leaves the live row behind.
        instance_delete_types = {
            MediaTypes.MOVIE.value,
            MediaTypes.EPISODE.value,
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
        }
        delete_instance = media_type_lower in instance_delete_types

        logger.info(
            "Attempting to delete history record %s (media_type=%s, media_instance_id=%s, user=%s)",
            str(history_id),
            media_type_lower,
            media_instance_id,
            str(request.user),
        )

        # Get music_id or podcast_id from query params if provided (for updating count)
        music_id = request.GET.get("music_id")
        podcast_id = request.GET.get("podcast_id")

        # Perform the deletion
        if delete_instance:
            try:
                media_instance = BasicMedia.objects.get_media(
                    request.user,
                    media_type_lower,
                    media_instance_id,
                )
            except (ObjectDoesNotExist, ValueError, TypeError):
                logger.exception(
                    "Media instance %s not found for history record %s (media_type=%s, user=%s)",
                    str(media_instance_id),
                    str(history_id),
                    media_type_lower,
                    str(request.user),
                )
                return HttpResponse("Record not found", status=404)

            related_season = (
                getattr(media_instance, "related_season", None)
                if media_type_lower == MediaTypes.EPISODE.value
                else None
            )

            try:
                media_instance.delete()
            except Exception as e:
                logger.error(
                    "Failed to delete media instance %s for history record %s: %s",
                    str(media_instance_id),
                    str(history_id),
                    str(e),
                    exc_info=True,
                )
                return HttpResponse("Failed to delete record", status=500)

            # Keep season/TV status in sync when deleting episode plays
            if related_season:
                related_season._sync_status_after_episode_change()
                cache_utils.clear_time_left_cache_for_user(related_season.user_id)

            # Verify deletion succeeded by checking if the instance still exists
            try:
                model = apps.get_model(app_label="app", model_name=media_type_lower)
                verification_query = model.objects.filter(id=media_instance_id)
                if media_type_lower == MediaTypes.EPISODE.value:
                    verification_query = verification_query.filter(
                        related_season__user=request.user,
                    )
                else:
                    verification_query = verification_query.filter(user=request.user)

                if verification_query.exists():
                    logger.error(
                        "Deletion verification failed: media instance %s still exists after delete() call",
                        str(media_instance_id),
                    )
                    return HttpResponse("Deletion failed", status=500)
            except Exception as e:
                logger.warning(
                    "Could not verify deletion of media instance %s: %s",
                    str(media_instance_id),
                    str(e),
                )
                # Continue anyway as the delete() call may have succeeded
        else:
            try:
                history_record.delete()
            except Exception as e:
                logger.error(
                    "Failed to delete history record %s: %s",
                    str(history_id),
                    str(e),
                    exc_info=True,
                )
                return HttpResponse("Failed to delete record", status=500)

            # Verify deletion succeeded by checking if the record still exists
            try:
                verification_query = historical_model.objects.filter(history_id=history_id)
                if verification_query.exists():
                    logger.error(
                        "Deletion verification failed: history record %s still exists after delete() call",
                        str(history_id),
                    )
                    return HttpResponse("Deletion failed", status=500)
            except Exception as e:
                logger.warning(
                    "Could not verify deletion of history record %s: %s",
                    str(history_id),
                    str(e),
                )
                # Continue anyway as the delete() call may have succeeded

        logger.info(
            "Successfully deleted %s %s (media_type=%s, media_instance_id=%s)",
            "media instance" if delete_instance else "history record",
            str(history_id),
            media_type_lower,
            media_instance_id,
        )

        # Invalidate caches since history changed.
        # Use the captured data instead of accessing the deleted object.
        logging_styles = ("sessions", "repeats")
        if media_type_lower in ("game", "boardgame"):
            start_dt = start_date or end_date
            end_dt = end_date or start_date
            history_day_keys = history_cache.history_day_keys_for_range(start_dt, end_dt)
        else:
            activity_dt = end_date or start_date or created_at
            history_day_key = history_cache.history_day_key(activity_dt)
            history_day_keys = [history_day_key] if history_day_key else []

        # For deletes, invalidate immediately (force) so the stale entry disappears,
        # then schedule refresh to rebuild. This shows the banner and reloads.
        history_cache.invalidate_history_days(
            request.user.id,
            day_keys=history_day_keys,
            logging_styles=logging_styles,
            force=True,
            reason="history_delete",
        )
        statistics_cache.invalidate_statistics_days(
            request.user.id,
            day_values=history_day_keys,
            reason="history_delete",
        )
        statistics_cache.schedule_all_ranges_refresh(request.user.id)

        # If music_id or podcast_id is provided, return updated count for out-of-band swap
        if music_id and media_type.lower() == "music":
            from app.models import Music
            from users.templatetags.user_tags import user_date_format

            try:
                music = Music.objects.get(id=music_id, user=request.user)
                # Get remaining history records (filtered by user or null)
                remaining_history = list(music.history.filter(
                    history_user=request.user,
                ).order_by("-end_date")) or list(music.history.filter(
                    history_user__isnull=True,
                ).order_by("-end_date"))

                remaining_count = len(remaining_history)

                if remaining_count > 0:
                    # Get the last entry for date display
                    last_entry = remaining_history[0]

                    # Format the date using the same filter as the template
                    last_date_formatted = user_date_format(last_entry.end_date, request.user) if last_entry.end_date else "No date provided"

                    if remaining_count == 1:
                        history_text = f"Last listened: {last_date_formatted}"
                    else:
                        history_text = f"Last listened: {last_date_formatted} • Listened {remaining_count} times"

                    # Return response with out-of-band swaps for both album page and modal
                    response = HttpResponse()
                    # Update the count on the album detail page
                    response.write(f'<p id="track-history-{music_id}" hx-swap-oob="true" class="text-xs text-gray-400 mt-2 px-4">{history_text}</p>')
                    # Update the count in the modal
                    modal_text = "Listened once" if remaining_count == 1 else f"Listened {remaining_count} times"
                    response.write(f'<p id="modal-listen-count-{music_id}" hx-swap-oob="true" class="text-sm text-gray-400 mt-1">{modal_text}</p>')
                    return response
                # No history left, hide the album page element and update modal
                response = HttpResponse()
                response.write(f'<p id="track-history-{music_id}" hx-swap-oob="true" class="text-xs text-gray-400 mt-2 px-4" style="display: none;"></p>')
                response.write(f'<p id="modal-listen-count-{music_id}" hx-swap-oob="true" class="text-sm text-gray-400 mt-1">Not listened yet</p>')
                return response
            except Music.DoesNotExist:
                pass

        # If podcast_id is provided, return updated count for out-of-band swap
        if podcast_id and media_type.lower() == "podcast":
            from app.models import Podcast
            from users.templatetags.user_tags import user_date_format

            try:
                podcast = Podcast.objects.get(id=podcast_id, user=request.user)
                # Get remaining history records (filtered by user or null)
                remaining_history = list(podcast.history.filter(
                    history_user=request.user,
                ).order_by("-end_date")) or list(podcast.history.filter(
                    history_user__isnull=True,
                ).order_by("-end_date"))

                remaining_count = len(remaining_history)

                if remaining_count > 0:
                    # Get the last entry for date display
                    last_entry = remaining_history[0]

                    # Format the date using the same filter as the template
                    last_date_formatted = user_date_format(last_entry.end_date, request.user) if last_entry.end_date else "No date provided"

                    if remaining_count == 1:
                        history_text = f"Last played: {last_date_formatted}"
                    else:
                        history_text = f"Last played: {last_date_formatted} • Played {remaining_count} times"

                    # Return response with out-of-band swaps for both show page and modal
                    response = HttpResponse()
                    # Update the count in the modal
                    modal_text = "Played once" if remaining_count == 1 else f"Played {remaining_count} times"
                    response.write(f'<p id="modal-listen-count-{podcast_id}" hx-swap-oob="true" class="text-sm text-gray-400 mt-1">{modal_text}</p>')
                    response["HX-Trigger"] = "history-refresh-start"
                    return response
                # No history left, update modal
                response = HttpResponse()
                response.write(f'<p id="modal-listen-count-{podcast_id}" hx-swap-oob="true" class="text-sm text-gray-400 mt-1">Not played yet</p>')
                response["HX-Trigger"] = "history-refresh-start"
                return response
            except Podcast.DoesNotExist:
                pass

        # Return empty 200 response - the element will be removed by HTMX
        response = HttpResponse()
        response["HX-Trigger"] = "history-refresh-start"
        return response

    except historical_model.DoesNotExist:
        logger.exception(
            "History record %s not found for user %s",
            str(history_id),
            str(request.user),
        )
        return HttpResponse("Record not found", status=404)


def _build_anniversary_history_days(user, month, day, logging_style=None):
    day_keys = history_cache.build_history_index(user, logging_style_override=logging_style)
    history_days = []
    for day_key in day_keys:
        try:
            day_date = date.fromisoformat(day_key)
        except ValueError:
            continue
        if day_date.month != month or day_date.day != day:
            continue
        day_payload = history_cache.build_history_day(
            user,
            day_date,
            logging_style_override=logging_style,
        )
        if day_payload and day_payload.get("entries"):
            history_days.append(day_payload)
    return history_days


def _build_release_history_days(user, month=None, day=None, date_filters=None):
    active_types = list(getattr(user, "get_active_media_types", list)())
    if not active_types:
        active_types = list(MediaTypes.values)
    include_podcasts = MediaTypes.PODCAST.value in active_types
    active_types = [
        media_type
        for media_type in active_types
        if media_type not in (MediaTypes.EPISODE.value, MediaTypes.PODCAST.value)
    ]

    start_date = None
    end_date = None
    if date_filters:
        start_date = parse_date(date_filters.get("start_date") or "")
        end_date = parse_date(date_filters.get("end_date") or "")

    release_days = defaultdict(list)
    seen_item_ids = set()
    for media_type in active_types:
        model = apps.get_model("app", media_type)
        queryset = (
            model.objects.filter(user=user, item__release_datetime__isnull=False)
            .select_related("item")
        )
        if month and day:
            queryset = queryset.annotate(
                release_month=ExtractMonth("item__release_datetime"),
                release_day=ExtractDay("item__release_datetime"),
            ).filter(release_month=month, release_day=day)
        elif start_date or end_date:
            if start_date:
                queryset = queryset.filter(item__release_datetime__date__gte=start_date)
            if end_date:
                queryset = queryset.filter(item__release_datetime__date__lte=end_date)

        for media in queryset:
            item = getattr(media, "item", None)
            if not item or item.id in seen_item_ids:
                continue
            seen_item_ids.add(item.id)
            release_dt = getattr(item, "release_datetime", None)
            localized = stats._localize_datetime(release_dt) if release_dt else None
            if not localized:
                continue
            release_date = localized.date()
            entry = {
                "item": item,
                "media_type": item.media_type,
                "title": item.title,
                "display_title": item.title,
                "poster": item.image,
                "played_at_local": localized,
                "entry_key": f"release-{item.id}-{release_date.isoformat()}",
            }
            release_days[release_date].append(entry)

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
    )
    if month and day:
        episode_qs = episode_qs.annotate(
            release_month=ExtractMonth("item__release_datetime"),
            release_day=ExtractDay("item__release_datetime"),
        ).filter(release_month=month, release_day=day)
    elif start_date or end_date:
        if start_date:
            episode_qs = episode_qs.filter(item__release_datetime__date__gte=start_date)
        if end_date:
            episode_qs = episode_qs.filter(item__release_datetime__date__lte=end_date)

    for episode in episode_qs:
        episode_item = getattr(episode, "item", None)
        if not episode_item or episode_item.id in seen_item_ids:
            continue
        seen_item_ids.add(episode_item.id)
        release_dt = getattr(episode_item, "release_datetime", None)
        localized = stats._localize_datetime(release_dt) if release_dt else None
        if not localized:
            continue
        release_date = localized.date()
        season_item = getattr(episode.related_season, "item", None)
        tv_item = getattr(getattr(episode.related_season, "related_tv", None), "item", None)
        title = episode_item.title or (season_item.title if season_item else None) or (tv_item.title if tv_item else "")
        display_title = history_cache._get_episode_display_title(episode)
        entry = {
            "item": episode_item,
            "media_type": MediaTypes.EPISODE.value,
            "title": title,
            "display_title": display_title or title,
            "poster": history_cache._get_episode_poster(episode),
            "played_at_local": localized,
            "entry_key": f"release-episode-{episode.id}-{release_date.isoformat()}",
        }
        release_days[release_date].append(entry)

    if include_podcasts:
        Podcast = apps.get_model("app", "Podcast")
        podcast_base = Podcast.objects.filter(user=user).select_related("item", "episode", "show")
        podcast_qs = podcast_base.filter(episode__published__isnull=False)
        if month and day:
            podcast_qs = podcast_qs.annotate(
                release_month=ExtractMonth("episode__published"),
                release_day=ExtractDay("episode__published"),
            ).filter(release_month=month, release_day=day)
        elif start_date or end_date:
            if start_date:
                podcast_qs = podcast_qs.filter(episode__published__date__gte=start_date)
            if end_date:
                podcast_qs = podcast_qs.filter(episode__published__date__lte=end_date)

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
            poster = settings.IMG_NONE
            if show and show.image:
                poster = show.image
            elif item.image:
                poster = item.image
            title = item.title or getattr(getattr(podcast, "episode", None), "title", "")
            entry = {
                "item": item,
                "media_type": MediaTypes.PODCAST.value,
                "title": title,
                "display_title": title,
                "show": show,
                "poster": poster,
                "played_at_local": localized,
                "entry_key": f"release-podcast-{podcast.id}-{release_date.isoformat()}",
            }
            seen_item_ids.add(item.id)
            release_days[release_date].append(entry)

        podcast_fallback_qs = podcast_base.filter(
            episode__published__isnull=True,
            item__release_datetime__isnull=False,
        )
        if month and day:
            podcast_fallback_qs = podcast_fallback_qs.annotate(
                release_month=ExtractMonth("item__release_datetime"),
                release_day=ExtractDay("item__release_datetime"),
            ).filter(release_month=month, release_day=day)
        elif start_date or end_date:
            if start_date:
                podcast_fallback_qs = podcast_fallback_qs.filter(item__release_datetime__date__gte=start_date)
            if end_date:
                podcast_fallback_qs = podcast_fallback_qs.filter(item__release_datetime__date__lte=end_date)

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
            poster = settings.IMG_NONE
            if show and show.image:
                poster = show.image
            elif item.image:
                poster = item.image
            title = item.title or getattr(getattr(podcast, "episode", None), "title", "")
            entry = {
                "item": item,
                "media_type": MediaTypes.PODCAST.value,
                "title": title,
                "display_title": title,
                "show": show,
                "poster": poster,
                "played_at_local": localized,
                "entry_key": f"release-podcast-{podcast.id}-{release_date.isoformat()}",
            }
            seen_item_ids.add(item.id)
            release_days[release_date].append(entry)

    history_days = []
    for release_date, entries in sorted(release_days.items(), key=lambda item: item[0], reverse=True):
        entries.sort(key=lambda entry: entry.get("played_at_local"), reverse=True)
        release_display_dt = entries[0]["played_at_local"]
        history_days.append(
            {
                "date": release_date,
                "weekday": formats.date_format(release_display_dt, "l"),
                "date_display": formats.date_format(release_display_dt, "F j, Y"),
                "entries": entries,
                "total_minutes": 0,
                "total_runtime_display": f"{len(entries)} release{'s' if len(entries) != 1 else ''}",
                "release_count": len(entries),
            },
        )
    return history_days


def _filter_history_by_enabled_media_types(history_days, user):
    """Filter history entries to only include enabled media types.

    Episodes and seasons are mapped to the 'tv' media type for filtering.

    Args:
        history_days: List of day dicts with 'entries' lists
        user: User object with get_enabled_media_types method

    Returns:
        Filtered history_days with entries for disabled media types removed
    """
    enabled_types = user.get_enabled_media_types()
    if not enabled_types:
        return history_days

    # Build a set of allowed media types for fast lookup
    # Episodes and seasons map to 'tv' for filtering purposes
    allowed_types = set(enabled_types)

    # If 'tv' is enabled, also allow 'episode' and 'season' entries
    if MediaTypes.TV.value in allowed_types:
        allowed_types.add(MediaTypes.EPISODE.value)
        allowed_types.add(MediaTypes.SEASON.value)

    filtered_days = []
    for day in history_days:
        if isinstance(day, dict):
            entries = day.get("entries", [])
            filtered_entries = [
                entry for entry in entries
                if entry.get("media_type") in allowed_types
            ]
            if filtered_entries:
                filtered_day = day.copy()
                filtered_day["entries"] = filtered_entries
                filtered_days.append(filtered_day)
        else:
            # Handle non-dict day objects (shouldn't happen, but be safe)
            filtered_days.append(day)

    return filtered_days


@require_GET
def history(request):
    """Show a day-by-day history of episode and movie plays."""
    try:
        view_start = time.perf_counter()
        history_mode = request.GET.get("history_mode")
        if history_mode != "release":
            history_mode = "activity"

        # Extract filter parameters from query string
        filters = {}
        int_params = ["album", "artist", "tv", "season", "season_number", "podcast_show"]
        str_params = [
            "genre",
            "media_type",
            "media_id",
            "source",
            "person_source",
            "person_id",
        ]
        for param in int_params:
            value = request.GET.get(param)
            if value:
                try:
                    filters[param] = int(value)
                except (TypeError, ValueError):
                    pass  # Skip invalid filter values
        for param in str_params:
            value = request.GET.get(param)
            if value:
                filters[param] = value

        logging_style = request.GET.get("logging_style")
        if logging_style not in ("sessions", "repeats"):
            logging_style = None

        # Extract date range filters
        date_filters = {}
        start_date_str = request.GET.get("start-date")
        end_date_str = request.GET.get("end-date")
        if start_date_str:
            date_filters["start_date"] = start_date_str
        if end_date_str:
            date_filters["end_date"] = end_date_str

        # Anniversary mode: specific month/day across years
        anniversary_month = request.GET.get("month")
        anniversary_day = request.GET.get("day")
        try:
            anniversary_month = int(anniversary_month) if anniversary_month else None
            anniversary_day = int(anniversary_day) if anniversary_day else None
        except (TypeError, ValueError):
            anniversary_month = None
            anniversary_day = None

        # Month-based pagination: year and month for calendar month view
        now = timezone.localtime()
        try:
            view_year = int(request.GET.get("year", now.year))
            view_month = int(request.GET.get("m", now.month))
            # Validate month range
            if view_month < 1 or view_month > 12:
                view_month = now.month
        except (TypeError, ValueError):
            view_year = now.year
            view_month = now.month

        logger.info(
            "history_view_start user_id=%s year=%s month=%s filters=%s date_filters=%s logging_style=%s",
            request.user.id,
            view_year,
            view_month,
            filters,
            date_filters,
            logging_style,
        )

        # Determine if we can use month-based caching (no filters, date range)
        use_month_cache = (
            history_mode == "activity"
            and not filters
            and not date_filters
            and not anniversary_month
            and not anniversary_day
        )
        history_refreshing = False

        if use_month_cache:
            # Month-based pagination: load from per-day caches
            history_days, cache_meta = history_cache.get_month_history(
                request.user,
                view_year,
                view_month,
                logging_style_override=logging_style,
            )
            history_refreshing = cache_meta.get("refreshing", False)

            # Filter by enabled media types
            history_days = _filter_history_by_enabled_media_types(history_days, request.user)

            # No paginator needed - we show one month at a time
            page_obj = None
            current_page = 1
            total_pages = 1
            total_days = len(history_days)

            # Calculate prev/next month for navigation
            # "prev" = older month (going back in time)
            # "next" = newer month (going forward toward present)
            if view_month == 1:
                prev_year, prev_month = view_year - 1, 12
            else:
                prev_year, prev_month = view_year, view_month - 1
            if view_month == 12:
                next_year, next_month = view_year + 1, 1
            else:
                next_year, next_month = view_year, view_month + 1

            # Month names for navigation labels
            prev_month_name = calendar.month_abbr[prev_month]
            next_month_name = calendar.month_abbr[next_month]

            # Check if we're on the current month (can't go newer)
            is_current_month = (view_year == now.year and view_month == now.month)

            # Don't show next month link if it's in the future
            show_next_month = (
                next_year < now.year
                or (next_year == now.year and next_month <= now.month)
            )
        else:
            # Filtered/special modes - use traditional pagination
            try:
                page_number = int(request.GET.get("page", 1))
            except (TypeError, ValueError):
                page_number = 1

            if history_mode == "release":
                history_days_all = _build_release_history_days(
                    request.user,
                    month=anniversary_month,
                    day=anniversary_day,
                    date_filters=date_filters,
                )
                history_refreshing = False
            elif anniversary_month and anniversary_day:
                history_days_all = _build_anniversary_history_days(
                    request.user,
                    month=anniversary_month,
                    day=anniversary_day,
                    logging_style=logging_style,
                )
                history_refreshing = False
            else:
                history_days_all = history_cache.get_history_days(
                    request.user,
                    filters=filters,
                    date_filters=date_filters,
                    logging_style_override=logging_style,
                )

            # Filter by enabled media types
            history_days_all = _filter_history_by_enabled_media_types(history_days_all, request.user)

            paginator = Paginator(history_days_all, history_cache.HISTORY_DAYS_PER_PAGE)

            if paginator.count == 0:
                page_obj = None
                history_days = []
                current_page = 1
                total_pages = 1
                total_days = 0
            else:
                try:
                    page_obj = paginator.page(page_number)
                except EmptyPage:
                    page_obj = paginator.page(paginator.num_pages)

                history_days = page_obj.object_list
                current_page = page_obj.number
                total_pages = paginator.num_pages
                total_days = paginator.count

            # Set defaults for non-month-cache path
            prev_year = prev_month = next_year = next_month = None
            prev_month_name = next_month_name = None
            show_next_month = False
            is_current_month = False

        # Combine all filters for pagination (including date filters as query params)
        active_filters = filters.copy()
        if date_filters.get("start_date"):
            active_filters["start-date"] = date_filters["start_date"]
        if date_filters.get("end_date"):
            active_filters["end-date"] = date_filters["end_date"]
        if logging_style:
            active_filters["logging_style"] = logging_style
        if anniversary_month and anniversary_day:
            active_filters["month"] = anniversary_month
            active_filters["day"] = anniversary_day
        if history_mode == "release":
            active_filters["history_mode"] = "release"

        # Build month display name for header
        month_name = calendar.month_name[view_month] if use_month_cache else None

        context = {
            "user": request.user,
            "history_days": history_days,
            "page_obj": page_obj,
            "current_page": current_page,
            "total_pages": total_pages,
            "total_days": total_days,
            "active_filters": active_filters,
            "history_refreshing": history_refreshing,
            "history_mode": history_mode,
            # Month-based navigation
            "use_month_view": use_month_cache,
            "view_year": view_year,
            "view_month": view_month,
            "month_name": month_name,
            "prev_year": prev_year,
            "prev_month": prev_month,
            "prev_month_name": prev_month_name,
            "next_year": next_year,
            "next_month": next_month,
            "next_month_name": next_month_name,
            "show_next_month": show_next_month,
            "is_current_month": is_current_month,
            "current_year": now.year,
            "current_month_num": now.month,
        }
        day_entry_counts = []
        total_entries = 0
        for day in history_days:
            entries = day.get("entries", []) if isinstance(day, dict) else getattr(day, "entries", [])
            count = len(entries)
            total_entries += count
            day_entry_counts.append((day.get("date_display") or day.get("date"), count))
        top_days = sorted(day_entry_counts, key=lambda item: item[1], reverse=True)[:3]
        logger.info(
            "history_page_entry_counts user_id=%s page=%s total_entries=%s top_days=%s",
            request.user.id,
            current_page,
            total_entries,
            top_days,
        )
        render_start = time.perf_counter()
        logger.info(
            "history_render_start user_id=%s page=%s",
            request.user.id,
            current_page,
        )
        response = render(request, "app/history.html", context)
        render_ms = (time.perf_counter() - render_start) * 1000
        response_bytes = len(response.content)
        logger.info(
            "history_render_end user_id=%s page=%s render_ms=%.2f response_bytes=%s",
            request.user.id,
            current_page,
            render_ms,
            response_bytes,
        )
        logger.info(
            "history_view_end user_id=%s page=%s total_days=%s page_days=%s total_pages=%s elapsed_ms=%.2f response_bytes=%s",
            request.user.id,
            current_page,
            total_days,
            len(history_days),
            total_pages,
            (time.perf_counter() - view_start) * 1000,
            response_bytes,
        )
        return response
    except OperationalError as error:
        logger.error("Database error in history view: %s", error, exc_info=True)
        # Return empty state on database error
        context = {
            "user": request.user,
            "history_days": [],
            "page_obj": None,
            "current_page": 1,
            "total_pages": 0,
            "total_days": 0,
            "days_per_page": history_cache.HISTORY_DAYS_PER_PAGE,
            "active_filters": {},
            "database_error": True,
            "history_refreshing": False,
        }
        return render(request, "app/history.html", context)


@login_not_required
@require_GET
def person_detail(request, source, person_id, name):
    """Render a cast/crew person bio page."""
    del name  # URL slug is cosmetic; person_id is canonical.

    if source != Sources.TMDB.value:
        return HttpResponseBadRequest("Person pages are only available for TMDB metadata.")

    person_metadata = tmdb.person(person_id)
    person = credits.upsert_person_profile(source, person_id, person_metadata)

    person_data = {
        "source": source,
        "person_id": str(person_id),
        "name": person_metadata.get("name")
        or (person.name if person else "Unknown Person"),
        "image": person_metadata.get("image")
        or (person.image if person else settings.IMG_NONE),
        "biography": person_metadata.get("biography")
        or (person.biography if person else ""),
        "known_for_department": person_metadata.get("known_for_department")
        or (person.known_for_department if person else ""),
        "birth_date": person_metadata.get("birth_date")
        or (person.birth_date.isoformat() if person and person.birth_date else None),
        "death_date": person_metadata.get("death_date")
        or (person.death_date.isoformat() if person and person.death_date else None),
        "place_of_birth": person_metadata.get("place_of_birth")
        or (person.place_of_birth if person else ""),
    }

    filmography = [dict(entry) for entry in person_metadata.get("filmography", [])]
    filmography_media_ids = {
        str(entry.get("media_id"))
        for entry in filmography
        if entry.get("media_id") is not None
    }

    tracked_items = Item.objects.none()
    if filmography_media_ids:
        tracked_items = Item.objects.filter(
            source=source,
            media_type__in=(MediaTypes.MOVIE.value, MediaTypes.TV.value),
            media_id__in=filmography_media_ids,
        )
    tracked_item_map = {
        (item.media_type, str(item.media_id)): item
        for item in tracked_items
    }

    for entry in filmography:
        entry["tracked_item"] = tracked_item_map.get(
            (entry.get("media_type"), str(entry.get("media_id"))),
        )

    history_filter_url = (
        f"{reverse('history')}?person_source={source}&person_id={person_id}"
    )
    tracked_plays_count = None
    if request.user.is_authenticated:
        episode_plays = (
            Episode.objects.filter(
                related_season__user=request.user,
                end_date__isnull=False,
                related_season__related_tv__item__person_credits__person__source=source,
                related_season__related_tv__item__person_credits__person__source_person_id=str(person_id),
            )
            .distinct()
            .count()
        )
        movie_plays = (
            Movie.objects.filter(
                user=request.user,
                item__person_credits__person__source=source,
                item__person_credits__person__source_person_id=str(person_id),
            )
            .exclude(start_date__isnull=True, end_date__isnull=True)
            .distinct()
            .count()
        )
        tracked_plays_count = episode_plays + movie_plays

    context = {
        "user": request.user,
        "person": person_data,
        "filmography": filmography,
        "history_filter_url": history_filter_url,
        "tracked_plays_count": tracked_plays_count,
        "source": source,
    }
    return render(request, "app/person_detail.html", context)


@require_GET
def create_artist_from_search(request, musicbrainz_artist_id):
    """Create an Artist from MusicBrainz search and redirect to artist page."""
    from app.providers import musicbrainz
    from app.services.music import sync_artist_discography

    # Check if artist already exists
    artist = Artist.objects.filter(musicbrainz_id=musicbrainz_artist_id).first()

    if not artist:
        # Fetch artist data from MusicBrainz
        artist_data = musicbrainz.get_artist(musicbrainz_artist_id)

        artist = Artist.objects.create(
            name=artist_data.get("name", "Unknown Artist"),
            sort_name=artist_data.get("sort_name", ""),
            musicbrainz_id=musicbrainz_artist_id,
            country=artist_data.get("country", "") or "",
            genres=[g.get("name") for g in artist_data.get("genres", []) if g.get("name")] if artist_data.get("genres") else [],
        )
        logger.info("Created artist %s from MusicBrainz", artist.name)

        # Sync discography immediately after creating artist
        sync_artist_discography(artist)

    return redirect("artist_detail", artist_id=artist.id)


@require_GET
def create_album_from_search(request, musicbrainz_release_id):
    """Create an Album from MusicBrainz search and redirect to album page."""
    from app.providers import musicbrainz

    # Check if album already exists
    album = Album.objects.filter(musicbrainz_release_id=musicbrainz_release_id).first()

    # Fetch release data from MusicBrainz (for both new and existing albums)
    release_data = musicbrainz.get_release(musicbrainz_release_id)

    if not album:
        # Create or get the artist
        artist = None
        artist_id = release_data.get("artist_id")
        artist_name = release_data.get("artist_name")

        if artist_id:
            artist = Artist.objects.filter(musicbrainz_id=artist_id).first()
            if not artist and artist_name:
                artist = Artist.objects.create(
                    name=artist_name,
                    musicbrainz_id=artist_id,
                    country=release_data.get("country", "") or "",
                )
        elif artist_name:
            artist = Artist.objects.filter(name=artist_name).first()
            if not artist:
                artist = Artist.objects.create(name=artist_name)

        # Parse release date
        release_date = None
        date_str = release_data.get("release_date", "")
        if date_str:
            try:
                from datetime import datetime
                if len(date_str) == 4:  # Year only
                    release_date = datetime.strptime(date_str, "%Y").date()
                elif len(date_str) == 7:  # Year-month
                    release_date = datetime.strptime(date_str, "%Y-%m").date()
                elif len(date_str) >= 10:  # Full date
                    release_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            except ValueError:
                pass

        album = Album.objects.create(
            title=release_data.get("title", "Unknown Album"),
            musicbrainz_release_id=musicbrainz_release_id,
            artist=artist,
            release_date=release_date,
            image=release_data.get("image", ""),
            genres=release_data.get("genres", []),
        )
        logger.info("Created album %s from MusicBrainz", album.title)
    else:
        # Update album image if it's missing or placeholder
        new_image = release_data.get("image", "")
        if new_image and new_image != settings.IMG_NONE and (not album.image or album.image == settings.IMG_NONE):
            album.image = new_image
            album.save(update_fields=["image"])
            logger.info("Updated album %s image", album.title)
        # Update genres if we have fresh metadata and none stored yet
        if not album.genres and release_data.get("genres"):
            album.genres = release_data.get("genres", [])
            album.save(update_fields=["genres"])

    return redirect("album_detail", album_id=album.id)


@require_GET
def artist_detail(request, artist_id):
    """Return the detail page for a music artist."""
    from django.db.models import Max, Min
    from django.shortcuts import get_object_or_404

    from app.models import ArtistTracker
    from app.providers import musicbrainz
    from app.services.music import (
        build_discography_groups,
        needs_discography_sync,
        sync_artist_discography,
    )
    from app.services.music_scrobble import dedupe_artist_albums

    artist = get_object_or_404(Artist, id=artist_id)

    # If we don't have an MBID yet, attempt a quick lookup so discography can sync
    if not artist.musicbrainz_id:
        try:
            mbid, cand_count, variant = sync_services.resolve_artist_mbid(
                artist.name or "",
                artist.sort_name or "",
            )
            if mbid:
                try:
                    artist.musicbrainz_id = mbid
                    artist.discography_synced_at = None  # force a fresh sync
                    artist.save(update_fields=["musicbrainz_id", "discography_synced_at"])
                    logger.info(
                        "Attached MBID %s to artist %s on view via '%s' (candidates=%d)",
                        mbid,
                        artist.name,
                        variant,
                        cand_count,
                    )
                except IntegrityError:
                    # Merge this artist into the existing MBID owner to avoid dupes
                    from app.services.music import merge_artist_records

                    existing = Artist.objects.filter(musicbrainz_id=mbid).first()
                    if existing:
                        existing = merge_artist_records(artist, existing)
                        artist = existing
                        logger.info(
                            "Merged artist %s into existing MBID %s via '%s'",
                            artist.name,
                            mbid,
                            variant,
                        )
                    else:
                        logger.debug(
                            "Artist MBID attach conflicted for %s with %s via '%s' but no target found",
                            artist.name,
                            mbid,
                            variant,
                        )
            else:
                logger.debug(
                    "No MBID attached on view for %s after searching variants (candidates=%d)",
                    artist.name,
                    cand_count,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Artist MBID attach failed on view for %s: %s", artist.name, exc)

    # Heal duplicate albums for this artist (caused by noisy metadata)
    dedupe_artist_albums(artist)

    # Decide whether we should force a fresh discography sync (e.g., after fast import)
    albums_qs = Album.objects.filter(artist=artist)
    existing_album_count = albums_qs.count()
    missing_mbids = albums_qs.filter(
        musicbrainz_release_id__isnull=True,
        musicbrainz_release_group_id__isnull=True,
    ).exists()
    # Refresh more aggressively on view: daily, if no albums yet, or if placeholders lack IDs
    should_sync = (
        needs_discography_sync(artist, max_age_days=1)
        or existing_album_count == 0
        or missing_mbids
    )
    force_sync = existing_album_count == 0 or missing_mbids

    # Sync discography from MusicBrainz if needed (like TV populates seasons from TMDB)
    synced_count = 0
    if should_sync and artist.musicbrainz_id:
        synced_count = sync_artist_discography(artist, force=force_sync)
        if synced_count:
            dedupe_artist_albums(artist)
    elif should_sync and not artist.musicbrainz_id:
        logger.debug("Skipping discography sync for %s due to missing MBID", artist.name)

    # Get ALL albums for this artist (metadata-driven, like Seasons)
    # Note: Album covers are prefetched asynchronously via HTMX after page load
    all_albums = list(Album.objects.filter(artist=artist).order_by("-release_date", "title"))

    # Get user's music entries for this artist to calculate play counts
    user_music_entries = Music.objects.filter(
        user=request.user,
        album__artist=artist,
    ).select_related("album")

    # Calculate play counts per album (history entries = listens)
    album_play_counts = {}
    total_plays = 0
    for music in user_music_entries:
        if music.album_id:
            play_count = music.history.count()
            album_play_counts[music.album_id] = album_play_counts.get(music.album_id, 0) + play_count
            total_plays += play_count

    # Get user's album trackers for these albums
    from app.models import AlbumTracker
    album_trackers = AlbumTracker.objects.filter(
        user=request.user,
        album__in=all_albums,
    ).select_related('album')

    # Build a dict mapping album_id -> tracker score
    album_scores = {}
    for tracker in album_trackers:
        if tracker.score is not None:
            album_scores[tracker.album_id] = tracker.score

    # Attach play_count and score to each album
    for album in all_albums:
        album.play_count = album_play_counts.get(album.id, 0)
        album.score = album_scores.get(album.id)  # None if no tracker/score

    discography_groups = build_discography_groups(all_albums)
    missing_cover_count = sum(
        1
        for album in all_albums
        if not album.image or album.image == settings.IMG_NONE
    )

    # Artist image is set from Wikipedia when fetching metadata below

    # Get user's tracker for this artist
    artist_tracker = ArtistTracker.objects.filter(
        user=request.user,
        artist=artist,
    ).first()

    # Get user's music entries for this artist
    user_music_for_artist = Music.objects.filter(
        user=request.user,
        album__artist=artist,
    )

    # History summary - just get date ranges, not historical record counts
    history_stats = user_music_for_artist.aggregate(
        first_listened=Min("start_date"),
        last_listened=Max("end_date"),
    )

    # Fetch detailed artist metadata from MusicBrainz
    artist_metadata = {}
    genres = []
    tags = []
    mb_rating = None
    mb_rating_count = 0
    bio = ""

    if artist.musicbrainz_id:
        try:
            mb_data = musicbrainz.get_artist(artist.musicbrainz_id)
            artist_metadata = {
                "type": mb_data.get("type", ""),
                "country": mb_data.get("country", ""),
                "area": mb_data.get("area", ""),
                "begin_date": mb_data.get("begin_date", ""),
                "end_date": mb_data.get("end_date", ""),
                "ended": mb_data.get("ended", False),
                "disambiguation": mb_data.get("disambiguation", ""),
            }
            genres = mb_data.get("genres", [])
            tags = mb_data.get("tags", [])
            mb_rating = mb_data.get("rating")
            mb_rating_count = mb_data.get("rating_count", 0)
            bio = mb_data.get("bio", "")  # Wikipedia extract

            # Persist country/genres locally for statistics rollups
            updated_fields = []
            if mb_data.get("country") and mb_data.get("country") != artist.country:
                artist.country = mb_data.get("country", "")
                updated_fields.append("country")
            if mb_data.get("genres"):
                genre_names = [g.get("name") for g in mb_data.get("genres") if g.get("name")]
                if genre_names != artist.genres:
                    artist.genres = genre_names
                    updated_fields.append("genres")
            if updated_fields:
                artist.save(update_fields=updated_fields)

            # Save Wikipedia image to Artist if not already set
            wiki_image = mb_data.get("image")
            if wiki_image and (not artist.image or artist.image == settings.IMG_NONE):
                artist.image = wiki_image
                artist.save(update_fields=["image"])
        except Exception as e:
            logger.debug("Failed to fetch artist metadata from MusicBrainz: %s", e)

    # Build genre chips (prefer genres, fall back to tags)
    genre_chips = []
    if genres:
        genre_chips = [g["name"].title() for g in genres[:6]]
    elif tags:
        # Use top tags as genre chips if no official genres
        genre_chips = [t["name"].title() for t in tags[:6]]

    # Get collection statistics for this artist
    from app.helpers import get_artist_collection_stats
    collection_stats = get_artist_collection_stats(request.user, artist)

    context = {
        "user": request.user,
        "artist": artist,
        "discography_groups": discography_groups,  # All releases from discography
        "total_plays": total_plays,
        "total_releases": len(all_albums),
        "missing_cover_count": missing_cover_count,
        "poll_for_covers": missing_cover_count > 0,
        "artist_tracker": artist_tracker,
        "history_stats": history_stats,
        "artist_metadata": artist_metadata,
        "genre_chips": genre_chips,
        "bio": bio,  # Wikipedia extract
        "mb_rating": mb_rating,
        "mb_rating_count": mb_rating_count,
        "collection_stats": collection_stats,
    }
    return render(request, "app/music_artist_detail.html", context)


@require_GET
def prefetch_artist_covers(request, artist_id):
    """HTMX endpoint to asynchronously fetch album covers for an artist.
    
    This runs after the artist page loads to avoid blocking the initial render.
    Returns the updated album grid HTML.
    """
    from django.shortcuts import get_object_or_404

    from app.models import Album, Artist, Music
    from app.services.music import build_discography_groups
    from app.tasks import prefetch_album_covers_batch

    artist = get_object_or_404(Artist, id=artist_id)

    # Get updated albums
    all_albums = list(Album.objects.filter(artist=artist).order_by("-release_date", "title"))

    # Calculate play counts
    user_music_entries = Music.objects.filter(
        user=request.user,
        album__artist=artist,
    ).select_related("album")

    album_play_counts = {}
    for music in user_music_entries:
        if music.album_id:
            play_count = music.history.count()
            album_play_counts[music.album_id] = album_play_counts.get(music.album_id, 0) + play_count

    for album in all_albums:
        album.play_count = album_play_counts.get(album.id, 0)

    discography_groups = build_discography_groups(all_albums)
    missing_cover_count = sum(
        1
        for album in all_albums
        if not album.image or album.image == settings.IMG_NONE
    )

    poll_for_covers = missing_cover_count > 0
    if missing_cover_count:
        cache_key = f"music:cover-prefetch:{artist.id}"
        try:
            if cache.add(cache_key, True, 60 * 10):
                try:
                    prefetch_album_covers_batch.delay([artist.id], limit_per_artist=None)
                except Exception as queue_exc:  # pragma: no cover - defensive
                    cache.delete(cache_key)
                    raise queue_exc
            poll_for_covers = bool(cache.get(cache_key))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Cover prefetch queue failed for artist %s: %s", artist.id, exc)

    return render(request, "app/components/artist_discography_container.html", {
        "discography_groups": discography_groups,
        "artist": artist,
        "missing_cover_count": missing_cover_count,
        "poll_for_covers": poll_for_covers,
    })


@require_GET
def album_detail(request, album_id):
    """Return the detail page for a music album."""
    from django.db.models import Max, Min
    from django.shortcuts import get_object_or_404

    from app.models import Track
    from app.providers import musicbrainz
    from app.services.music import (
        album_has_musicbrainz_id,
        ensure_album_has_release_id,
        sync_artist_discography,
    )
    from app.services.music_scrobble import (
        _choose_primary_album,
        _normalize,
        dedupe_artist_albums,
        is_incomplete_album,
    )

    album = get_object_or_404(Album.objects.select_related("artist"), id=album_id)
    original_artist = album.artist
    original_title = album.title
    original_norm = _normalize(original_title)

    # Heal duplicate albums for this artist so detail pages are consistent
    dedupe_artist_albums(original_artist)
    try:
        album.refresh_from_db()
    except Album.DoesNotExist:
        # Find the canonical album by normalized title
        replacement = (
            Album.objects.filter(artist=original_artist, title__iexact=original_title)
            .order_by("id")
            .first()
        )
        if not replacement:
            for cand in Album.objects.filter(artist=original_artist):
                if _normalize(cand.title) == original_norm:
                    replacement = cand
                    break
        if replacement:
            return redirect("album_detail", album_id=replacement.id)
        raise

    # If we only have a sparse placeholder, try to repopulate via discography and re-dedupe
    if is_incomplete_album(album) and original_artist.musicbrainz_id:
        try:
            sync_artist_discography(original_artist, force=True)
            dedupe_artist_albums(original_artist)
            candidates = [
                a
                for a in Album.objects.filter(artist=original_artist)
                if _normalize(a.title) == original_norm
            ]
            if candidates:
                best = _choose_primary_album(candidates, album)
                if best.id != album.id:
                    return redirect("album_detail", album_id=best.id)
                album = best
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to heal album %s via discography: %s", album_id, exc)

    # Ensure album has a release_id (fetch from release_group if needed)
    # This fixes albums that came from discography sync with only release_group_id
    if not album.musicbrainz_release_id and album.musicbrainz_release_group_id:
        ensure_album_has_release_id(album)

    # Populate tracks from MusicBrainz if not done yet (like Season populates episodes)
    # Now we accept albums that have EITHER release_id OR release_group_id
    has_mb_identity = album_has_musicbrainz_id(album)
    if not album.tracks_populated and has_mb_identity:
        try:
            # If we still don't have release_id after ensure_album_has_release_id,
            # we can't fetch tracks (need a specific release for track listing)
            if album.musicbrainz_release_id:
                release_data = musicbrainz.get_release(album.musicbrainz_release_id)
                tracks_data = release_data.get("tracks", [])

                # Update genres from release if available
                if release_data.get("genres") and not album.genres:
                    album.genres = release_data.get("genres")
                    album.save(update_fields=["genres"])

                for track_data in tracks_data:
                    Track.objects.update_or_create(
                        album=album,
                        disc_number=track_data.get("disc_number", 1),
                        track_number=track_data.get("track_number"),
                        defaults={
                            "title": track_data.get("title", "Unknown Track"),
                            "musicbrainz_recording_id": track_data.get("recording_id"),
                            "duration_ms": track_data.get("duration_ms"),
                            "genres": track_data.get("genres", []) or release_data.get("genres", []),
                        },
                    )

                # Also update album image if missing
                if not album.image or album.image == settings.IMG_NONE:
                    new_image = release_data.get("image", "")
                    if new_image and new_image != settings.IMG_NONE:
                        album.image = new_image

                album.tracks_populated = True
                album.save(update_fields=["tracks_populated", "image"])
                logger.info("Populated %d tracks for album %s", len(tracks_data), album.title)
            else:
                logger.warning("Album %s has release_group but no release found for tracks", album.title)
        except Exception as e:
            logger.warning("Failed to populate tracks for album %s: %s", album.title, e)

    # Get ALL tracks for this album (metadata-driven, like Episodes)
    all_tracks = Track.objects.filter(album=album).order_by("disc_number", "track_number", "title")

    # Get user's Music entries for these tracks
    user_music_by_track = {}
    user_music_entries = Music.objects.filter(
        user=request.user,
        album=album,
    ).select_related("item", "track")

    for music in user_music_entries:
        if music.track_id:
            user_music_by_track[music.track_id] = music
        # Also index by recording ID for legacy data
        if music.item and music.item.media_id:
            user_music_by_track[f"recording_{music.item.media_id}"] = music

    # Build track data with user's tracking info (like Episodes)
    tracks_with_data = []
    total_duration_ms = 0
    
    # Get collection entries for all tracks in one query (if user is authenticated)
    from app.helpers import is_item_collected
    from app.models import CollectionEntry
    
    collection_entries_by_item_id = {}
    if request.user.is_authenticated:
        # Get all item IDs from music entries
        music_item_ids = [m.item_id for m in user_music_entries if m.item_id]
        if music_item_ids:
            # Fetch all collection entries for these items in one query
            collection_entries = CollectionEntry.objects.filter(
                user=request.user,
                item_id__in=music_item_ids,
            )
            collection_entries_by_item_id = {ce.item_id: ce for ce in collection_entries}
    
    for track in all_tracks:
        # Look up user's Music entry for this track
        music_entry = user_music_by_track.get(track.id)
        if not music_entry and track.musicbrainz_recording_id:
            music_entry = user_music_by_track.get(f"recording_{track.musicbrainz_recording_id}")

        # Get collection entry for this track
        collection_entry = None
        if music_entry and music_entry.item_id:
            collection_entry = collection_entries_by_item_id.get(music_entry.item_id)

        track_data = {
            "track": track,
            "music": music_entry,
            "history": list(music_entry.history.all().order_by("-end_date")) if music_entry else [],
            "collection_entry": collection_entry,
        }
        tracks_with_data.append(track_data)
        if track.duration_ms:
            total_duration_ms += track.duration_ms

    # Count tracks in library
    library_track_count = sum(1 for t in tracks_with_data if t["music"])

    # History summary for this album (like Season history)
    history_stats = user_music_entries.aggregate(
        first_listened=Min("start_date"),
        last_listened=Max("end_date"),
    )

    # Get the current/primary Music instance for this album (most recently updated)
    current_instance = user_music_entries.order_by("-end_date", "-start_date").first()

    # Get user's tracker for this album
    from app.models import AlbumTracker
    album_tracker = AlbumTracker.objects.filter(
        user=request.user, album=album,
    ).first()

    # Calculate total runtime
    total_runtime = None
    if total_duration_ms:
        total_minutes = total_duration_ms // 60000
        if total_minutes >= 60:
            hours = total_minutes // 60
            minutes = total_minutes % 60
            total_runtime = f"{hours}h {minutes}m"
        else:
            total_runtime = f"{total_minutes}m"

    # Album details (like Season details)
    album_details = {
        "format": album.release_type or "Album",
        "release_date": album.release_date,
        "tracks": len(tracks_with_data),
        "runtime": total_runtime,
    }
    # Show either release_id or release_group_id for the source link
    if album.musicbrainz_release_id:
        album_details["musicbrainz_id"] = album.musicbrainz_release_id
        album_details["musicbrainz_url"] = f"https://musicbrainz.org/release/{album.musicbrainz_release_id}"
    elif album.musicbrainz_release_group_id:
        album_details["musicbrainz_id"] = album.musicbrainz_release_group_id
        album_details["musicbrainz_url"] = f"https://musicbrainz.org/release-group/{album.musicbrainz_release_group_id}"

    # Get collection metadata for this album (aggregated from tracks)
    from app.helpers import get_album_collection_metadata
    collection_metadata = get_album_collection_metadata(request.user, album)

    context = {
        "user": request.user,
        "album": album,
        "tracks": tracks_with_data,  # All tracks from metadata
        "library_track_count": library_track_count,
        "total_tracks": len(tracks_with_data),
        "history_stats": history_stats,
        "current_instance": current_instance,
        "album_details": album_details,
        "total_runtime": total_runtime,
        "has_mb_identity": has_mb_identity,  # For template to show correct message
        "album_tracker": album_tracker,  # User's tracking for this album
        "collection_metadata": collection_metadata,  # Collection metadata aggregated from tracks
    }
    return render(request, "app/music_album_detail.html", context)


@require_POST
def sync_artist_discography_view(request, artist_id):
    """Manually trigger discography sync for an artist."""
    from django.shortcuts import get_object_or_404

    from app.services.music import prefetch_album_covers, sync_artist_discography
    from app.services.music_scrobble import dedupe_artist_albums
    from app.tasks import prefetch_album_covers_batch

    artist = get_object_or_404(Artist, id=artist_id)

    # Force sync
    count = sync_artist_discography(artist, force=True)
    if count:
        dedupe_artist_albums(artist)

    cover_task_id = None
    try:
        result = prefetch_album_covers_batch.delay([artist.id], limit_per_artist=None)
        cover_task_id = result.id
        cache.set(f"music:cover-prefetch:{artist.id}", True, 60 * 10)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Cover prefetch queue failed for artist %s: %s", artist.id, exc)
        try:
            prefetch_album_covers(artist, limit=None)
            cache.set(f"music:cover-prefetch:{artist.id}", True, 60 * 10)
        except Exception as inner_exc:  # pragma: no cover - defensive
            logger.debug("Cover prefetch failed for artist %s: %s", artist.id, inner_exc)

    if cover_task_id:
        messages.success(
            request,
            f"Synced {count} albums for {artist.name}. Cover art refresh queued.",
        )
    else:
        messages.success(request, f"Synced {count} albums for {artist.name}")

    # Return HX-Refresh header to reload the page
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


def artist_track_modal(request, artist_id):
    """Return the tracking form modal for an artist - mirrors TV's track_modal."""
    from django.shortcuts import get_object_or_404

    from app.forms import ArtistTrackerForm
    from app.models import ArtistTracker

    artist = get_object_or_404(Artist, id=artist_id)
    return_url = request.GET.get("return_url", "")

    # Get existing tracker if any
    tracker = ArtistTracker.objects.filter(user=request.user, artist=artist).first()

    initial_data = {"artist_id": artist.id}
    form = ArtistTrackerForm(
        instance=tracker,
        initial=initial_data,
        user=request.user,
    )

    return render(
        request,
        "app/components/artist_track_modal.html",
        {
            "artist": artist,
            "tracker": tracker,
            "form": form,
            "return_url": return_url,
        },
    )


@require_POST
def artist_save(request):
    """Save an artist tracker - mirrors media_save for TV."""
    from django.shortcuts import get_object_or_404

    from app.forms import ArtistTrackerForm
    from app.models import ArtistTracker

    artist_id = request.POST.get("artist_id")
    artist = get_object_or_404(Artist, id=artist_id)

    # Get existing tracker or None
    tracker = ArtistTracker.objects.filter(user=request.user, artist=artist).first()

    form = ArtistTrackerForm(request.POST, instance=tracker, user=request.user)
    if form.is_valid():
        tracker = form.save(commit=False)
        tracker.user = request.user
        tracker.artist = artist
        tracker.save()
        messages.success(request, f"Saved {artist.name}")
    else:
        messages.error(request, f"Error saving {artist.name}: {form.errors}")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("artist_detail", artist_id=artist.id)


@require_POST
def artist_delete(request):
    """Delete an artist tracker - mirrors media_delete for TV."""
    from django.shortcuts import get_object_or_404

    from app.models import ArtistTracker

    artist_id = request.POST.get("artist_id")
    artist = get_object_or_404(Artist, id=artist_id)

    tracker = ArtistTracker.objects.filter(user=request.user, artist=artist).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {artist.name} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("artist_detail", artist_id=artist.id)


@require_GET
def podcast_show_detail(request, show_id):
    """Return the detail page for a podcast show."""
    from django.shortcuts import get_object_or_404

    from app.models import Podcast, PodcastEpisode, PodcastShow, PodcastShowTracker

    show = get_object_or_404(PodcastShow, id=show_id)

    # Get user's tracker for this show
    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()

    # Get all episodes for this show
    # Get all episodes for this show, ordered by published date (newest first)
    # Use Coalesce to handle None published dates (put them at the end)
    from datetime import datetime

    from django.db.models import DateTimeField, Value
    from django.db.models.functions import Coalesce

    episodes = PodcastEpisode.objects.filter(show=show).annotate(
        published_or_old=Coalesce(
            "published",
            Value(datetime(1970, 1, 1, tzinfo=UTC),
                  output_field=DateTimeField()),
        ),
    ).order_by("-published_or_old", "-episode_number")

    # Get user's podcast entries for this show
    user_podcasts = list(Podcast.objects.filter(
        user=request.user,
        show=show,
    ).select_related("episode", "item"))

    # Calculate stats
    total_episodes = episodes.count()
    total_listened = len(user_podcasts)
    total_minutes = sum(podcast.progress or 0 for podcast in user_podcasts)

    context = {
        "user": request.user,
        "show": show,
        "episodes": episodes,
        "user_podcasts": user_podcasts,
        "tracker": tracker,
        "total_episodes": total_episodes,
        "total_listened": total_listened,
        "total_minutes": total_minutes,
    }
    return render(request, "app/podcast_show_detail.html", context)


@require_GET
def podcast_show_track_modal(request, show_id):
    """Return the tracking form modal for a podcast show - mirrors artist_track_modal."""
    from django.shortcuts import get_object_or_404

    from app.forms import PodcastShowTrackerForm
    from app.models import PodcastShow, PodcastShowTracker

    show = get_object_or_404(PodcastShow, id=show_id)
    return_url = request.GET.get("return_url", "")

    # Get existing tracker if any
    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()

    initial_data = {"show_id": show.id}
    form = PodcastShowTrackerForm(
        instance=tracker,
        initial=initial_data,
        user=request.user,
    )

    return render(
        request,
        "app/components/podcast_show_track_modal.html",
        {
            "show": show,
            "tracker": tracker,
            "form": form,
            "return_url": return_url,
        },
    )


@require_GET
def podcast_episodes_api(request, show_id):
    """API endpoint for paginated podcast episodes.
    
    Returns HTML fragments for infinite scroll if format=html, otherwise JSON.
    """
    from django.conf import settings
    from django.shortcuts import get_object_or_404

    from app.models import (
        Item,
        MediaTypes,
        Podcast,
        PodcastEpisode,
        PodcastShow,
        Sources,
    )

    show = get_object_or_404(PodcastShow, id=show_id)
    format_type = request.GET.get("format", "json")  # 'json' or 'html'

    # Get pagination parameters
    try:
        page = int(request.GET.get("page", 1))
        page_size = int(request.GET.get("page_size", 20))
    except ValueError:
        page = 1
        page_size = 20

    # Get all episodes for this show, ordered by published date (newest first)
    # Use Coalesce to handle None published dates (put them at the end)
    from datetime import datetime

    from django.db.models import DateTimeField, Value
    from django.db.models.functions import Coalesce

    # Episodes with published dates first (newest), then episodes without dates
    episodes_qs = PodcastEpisode.objects.filter(show=show).annotate(
        published_or_old=Coalesce(
            "published",
            Value(datetime(1970, 1, 1, tzinfo=UTC),
                  output_field=DateTimeField()),
        ),
    ).order_by("-published_or_old", "-episode_number")
    total_count = episodes_qs.count()

    # Calculate pagination
    start = (page - 1) * page_size
    end = start + page_size
    episodes = episodes_qs[start:end]

    # Get user's podcast entries for this show
    # Order by created_at descending so we get the most recent entry when multiple exist
    # This allows multiple plays of the same episode to be tracked separately in the DB
    # but we show the most recent one in the UI
    user_podcasts = list(Podcast.objects.filter(
        user=request.user,
        show=show,
    ).select_related("episode", "item").order_by("episode_id", "-created_at"))

    # Create a map of episode_id to user podcast
    # When multiple entries exist for the same episode, keep only the most recent one
    episode_podcast_map = {}
    for podcast in user_podcasts:
        if podcast.episode_id:
            # Only store the first (most recent after ordering) entry for each episode
            if podcast.episode_id not in episode_podcast_map:
                episode_podcast_map[podcast.episode_id] = podcast

    # Build episode items for enrichment
    episode_items_data = []
    episode_items_map = {}
    for episode in episodes:
        item, _ = Item.objects.get_or_create(
            media_id=episode.episode_uuid,
            source=Sources.POCKETCASTS.value,
            media_type=MediaTypes.PODCAST.value,
            defaults={
                "title": episode.title,
                "image": show.image or settings.IMG_NONE,
            },
        )
        if item.title != episode.title:
            item.title = episode.title
            item.save(update_fields=["title"])
        episode_items_data.append({
            "media_id": episode.episode_uuid,
            "source": Sources.POCKETCASTS.value,
            "media_type": MediaTypes.PODCAST.value,
        })
        episode_items_map[episode.episode_uuid] = item

    # Enrich episodes with user data
    enriched_episodes_raw = helpers.enrich_items_with_user_data(
        request,
        episode_items_data,
        user=request.user,
    )

    # Calculate pagination info
    has_more = end < total_count
    next_page = page + 1 if has_more else None

    if format_type == "html":
        # Return HTML fragments for HTMX
        from django.template.loader import render_to_string

        # Build episode data similar to media_details view
        episode_list = []
        for episode_obj in episodes:
            # Find enriched data
            enriched = None
            for e in enriched_episodes_raw:
                if e["item"]["media_id"] == episode_obj.episode_uuid:
                    enriched = e
                    break

            # Format duration
            duration_str = ""
            if episode_obj.duration:
                hours = episode_obj.duration // 3600
                minutes = (episode_obj.duration % 3600) // 60
                if hours > 0:
                    duration_str = f"{hours}h {minutes}m"
                else:
                    duration_str = f"{minutes}m"

            # Get user's podcast for this episode
            user_podcast = episode_podcast_map.get(episode_obj.id)

            # Create adapter objects (same as media_details view)
            class PodcastEpisodeAdapter:
                def __init__(self, episode):
                    self.title = episode.title
                    self.track_number = episode.episode_number
                    self.duration_formatted = self._format_duration(episode.duration) if episode.duration else None
                    self.musicbrainz_recording_id = None
                    self.id = episode.id
                    self.published = episode.published
                    self.episode_uuid = episode.episode_uuid

                def _format_duration(self, seconds):
                    hours = seconds // 3600
                    minutes = (seconds % 3600) // 60
                    secs = seconds % 60
                    if hours > 0:
                        return f"{hours}:{minutes:02d}:{secs:02d}"
                    return f"{minutes}:{secs:02d}"

            class PodcastShowAdapter:
                def __init__(self, show):
                    self.image = show.image or settings.IMG_NONE
                    self.release_date = None
                    self.id = show.id

            # Create history wrapper
            all_history = []
            if user_podcast:
                all_history = list(user_podcast.history.filter(end_date__isnull=False).order_by("-end_date")[:10])
                class PodcastHistoryWrapper:
                    def __init__(self, podcast, item, history_list):
                        self.item = item
                        self.id = podcast.id
                        self._history_list = history_list

                    @property
                    def completed_play_count(self):
                        """Return count of completed plays (history records with end_date)."""
                        # Since we already filtered all_history to only include records with end_date,
                        # we can just count the length of the filtered history_list
                        return len(self._history_list)

                    @property
                    def history(self):
                        class HistoryProxy:
                            def __init__(self, history_list):
                                self._history = history_list
                            def all(self):
                                return self._history
                            def count(self):
                                return len(self._history)
                        return HistoryProxy(self._history_list)

                podcast_wrapper = PodcastHistoryWrapper(user_podcast, enriched["item"] if enriched else item, all_history)
            else:
                class DummyPodcast:
                    def __init__(self, item):
                        self.item = item
                        self.id = 0
                        self.history = type("History", (), {"count": lambda: 0, "all": list})()
                podcast_wrapper = DummyPodcast(enriched["item"] if enriched else item)

            episode_list.append({
                "title": episode_obj.title,
                "episode_number": episode_obj.episode_number or 0,
                "image": show.image or settings.IMG_NONE,
                "air_date": episode_obj.published,
                "runtime": duration_str,
                "overview": "",
                "history": all_history,
                "media": enriched["media"] if enriched else None,
                "item": enriched["item"] if enriched else item,
                "media_id": episode_obj.episode_uuid,
                "source": Sources.POCKETCASTS.value,
                "media_type": MediaTypes.PODCAST.value,
                "track_adapter": PodcastEpisodeAdapter(episode_obj),
                "album_adapter": PodcastShowAdapter(show),
                "music_wrapper": podcast_wrapper,
            })

        # Render HTML fragment
        html = render_to_string(
            "app/components/podcast_episode_list.html",
            {
                "episodes": episode_list,
                "user": request.user,
                "show": show,
                "IMG_NONE": settings.IMG_NONE,
                "TRACK_TIME": True,
                "has_more": has_more,
                "next_page": next_page,
                "show_id": show_id,
            },
            request=request,
        )
        response = HttpResponse(html)
        # Prevent caching of episode list fragments
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response
    # Return JSON
    episode_list = []
    for episode_obj in episodes:
        # Find enriched data
        enriched = None
        for e in enriched_episodes_raw:
            if e["item"]["media_id"] == episode_obj.episode_uuid:
                enriched = e
                break

        # Format duration
        duration_str = ""
        if episode_obj.duration:
            hours = episode_obj.duration // 3600
            minutes = (episode_obj.duration % 3600) // 60
            if hours > 0:
                duration_str = f"{hours}h {minutes}m"
            else:
                duration_str = f"{minutes}m"

        # Get status if user has listened
        user_podcast = episode_podcast_map.get(episode_obj.id)
        status = user_podcast.status if user_podcast else None

        episode_data = {
            "id": episode_obj.id,
            "title": episode_obj.title,
            "published": episode_obj.published.isoformat() if episode_obj.published else None,
            "duration": duration_str,
            "duration_seconds": episode_obj.duration,
            "episode_number": episode_obj.episode_number,
            "status": status,
            "has_history": enriched and enriched.get("media") is not None,
        }
        episode_list.append(episode_data)

    total_pages = (total_count + page_size - 1) // page_size

    return JsonResponse({
        "episodes": episode_list,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_more": has_more,
        },
    })


@require_POST
def podcast_show_save(request):
    """Save a podcast show tracker - mirrors artist_save."""
    from django.shortcuts import get_object_or_404

    from app.forms import PodcastShowTrackerForm
    from app.models import PodcastShow, PodcastShowTracker

    show_id = request.POST.get("show_id")
    show = get_object_or_404(PodcastShow, id=show_id)

    # Get existing tracker or None
    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()

    form = PodcastShowTrackerForm(request.POST, instance=tracker, user=request.user)
    if form.is_valid():
        tracker = form.save(commit=False)
        tracker.user = request.user
        tracker.show = show
        tracker.save()
        messages.success(request, f"Saved {show.title}")
    else:
        messages.error(request, f"Error saving {show.title}: {form.errors}")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("podcast_show_detail", show_id=show.id)


@require_POST
def podcast_show_delete(request):
    """Delete a podcast show tracker - mirrors artist_delete."""
    from django.shortcuts import get_object_or_404

    from app.models import PodcastShow, PodcastShowTracker

    show_id = request.POST.get("show_id")
    show = get_object_or_404(PodcastShow, id=show_id)

    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {show.title} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("podcast_show_detail", show_id=show.id)


@require_POST
def podcast_mark_all_played(request, show_id):
    """Mark all unplayed episodes for a podcast show as completed on their release date."""
    import hashlib

    from django.conf import settings
    from django.shortcuts import get_object_or_404
    from django.utils import timezone

    import events
    from app.mixins import disable_fetch_releases
    from app.models import (
        Item,
        MediaTypes,
        Podcast,
        PodcastEpisode,
        PodcastShow,
        PodcastShowTracker,
        Sources,
        Status,
    )
    from integrations import podcast_rss

    show = get_object_or_404(PodcastShow, id=show_id)

    # Create tracker if it doesn't exist (user hasn't added show to library yet)
    tracker, _ = PodcastShowTracker.objects.get_or_create(
        user=request.user,
        show=show,
        defaults={"status": Status.IN_PROGRESS.value},
    )

    # If show has RSS feed, fetch full episode list and ensure all episodes are in database
    if show.rss_feed_url:
        try:
            # Fetch ALL episodes (no limit) from RSS feed
            episodes_data = podcast_rss.fetch_episodes_from_rss(show.rss_feed_url, limit=None)

            for episode_data in episodes_data:
                # Generate episode UUID from GUID or create one
                # Use GUID directly (consistent with _sync_episodes_from_rss logic)
                episode_uuid = episode_data.get("guid")
                if not episode_uuid:
                    # Use a hash of title + published date as fallback UUID
                    import hashlib
                    uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                    episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                # Check if episode already exists by UUID, or try to match by title + date
                episode = None
                try:
                    episode = PodcastEpisode.objects.get(episode_uuid=episode_uuid)
                except PodcastEpisode.DoesNotExist:
                    # Try to match by title + published date
                    if episode_data.get("title") and episode_data.get("published"):
                        matching = PodcastEpisode.objects.filter(
                            show=show,
                            title__iexact=episode_data["title"].strip(),
                            published__date=episode_data["published"].date(),
                        ).first()
                        if matching:
                            episode = matching
                except PodcastEpisode.MultipleObjectsReturned:
                    # If multiple found, use first one
                    episode = PodcastEpisode.objects.filter(episode_uuid=episode_uuid).first()

                if not episode:
                    PodcastEpisode.objects.create(
                        show=show,
                        episode_uuid=episode_uuid,
                        title=episode_data.get("title", "Unknown Episode"),
                        published=episode_data.get("published"),
                        duration=episode_data.get("duration"),
                        audio_url=episode_data.get("audio_url", ""),
                        episode_number=episode_data.get("episode_number"),
                        season_number=episode_data.get("season_number"),
                    )
        except Exception as e:
            logger.warning("Failed to fetch full episode list from RSS feed %s: %s", show.rss_feed_url, e)
            # Continue with existing episodes in database

    # Get all episodes for this show (now including any newly fetched ones)
    all_episodes = PodcastEpisode.objects.filter(show=show)

    # Get all episodes the user has already completed (has end_date)
    completed_episodes = set(
        Podcast.objects.filter(
            user=request.user,
            show=show,
            episode__isnull=False,
            end_date__isnull=False,  # Only count completed episodes
        ).values_list("episode_id", flat=True),
    )

    # Find unplayed episodes (episodes without a completed Podcast entry)
    unplayed_episodes = all_episodes.exclude(id__in=completed_episodes)

    if not unplayed_episodes.exists():
        messages.info(request, f"All episodes of {show.title} are already marked as played")
        return redirect("media_details", source=Sources.POCKETCASTS.value, media_type=MediaTypes.PODCAST.value, media_id=show.podcast_uuid, title=show.slug or show.title)

    created_count = 0
    items_created = []

    # Disable calendar triggers during bulk operations to avoid queuing hundreds of tasks
    with disable_fetch_releases():
        for episode in unplayed_episodes:
            # Get or create Item for this episode
            runtime_minutes = episode.duration // 60 if episode.duration else None
            item_defaults = {
                "title": episode.title,
                "image": show.image or settings.IMG_NONE,
            }
            if runtime_minutes:
                item_defaults["runtime_minutes"] = runtime_minutes
            if episode.published:
                item_defaults["release_datetime"] = episode.published

            item, item_created = Item.objects.get_or_create(
                media_id=episode.episode_uuid,
                source=Sources.POCKETCASTS.value,
                media_type=MediaTypes.PODCAST.value,
                defaults=item_defaults,
            )

            if not item_created:
                update_fields = []
                if runtime_minutes and item.runtime_minutes != runtime_minutes:
                    item.runtime_minutes = runtime_minutes
                    update_fields.append("runtime_minutes")
                if episode.published and item.release_datetime != episode.published:
                    item.release_datetime = episode.published
                    update_fields.append("release_datetime")
                if update_fields:
                    item.save(update_fields=update_fields)

            # Track items for calendar reload
            if item_created:
                items_created.append(item)

            # Use episode's published date as end_date, or current time if no published date
            end_date = episode.published if episode.published else timezone.now()

            # Create Podcast entry marking as completed
            Podcast.objects.create(
                item=item,
                user=request.user,
                show=show,
                episode=episode,
                status=Status.COMPLETED.value,
                end_date=end_date,
                progress=runtime_minutes if runtime_minutes else 0,
            )
            created_count += 1

    # Trigger a single calendar reload for all created items (if any)
    if items_created:
        events.tasks.reload_calendar.apply_async(kwargs={"items_to_process": items_created}, countdown=3)

    episode_word = "episodes" if created_count != 1 else "episode"
    messages.success(
        request,
        f"Marked {created_count} {episode_word} of {show.title} as played",
    )

    return redirect("media_details", source=Sources.POCKETCASTS.value, media_type=MediaTypes.PODCAST.value, media_id=show.podcast_uuid, title=show.slug or show.title)


def album_track_modal(request, album_id):
    """Return the tracking form modal for an album - mirrors artist_track_modal."""
    from django.shortcuts import get_object_or_404

    from app.forms import AlbumTrackerForm
    from app.models import AlbumTracker

    album = get_object_or_404(Album, id=album_id)
    return_url = request.GET.get("return_url", "")

    # Get existing tracker if any
    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()

    initial_data = {"album_id": album.id}
    form = AlbumTrackerForm(
        instance=tracker,
        initial=initial_data,
        user=request.user,
    )

    return render(
        request,
        "app/components/album_track_modal.html",
        {
            "album": album,
            "tracker": tracker,
            "form": form,
            "return_url": return_url,
        },
    )


@require_POST
def album_save(request):
    """Save an album tracker - mirrors artist_save."""
    from django.shortcuts import get_object_or_404

    from app.forms import AlbumTrackerForm
    from app.models import AlbumTracker

    album_id = request.POST.get("album_id")
    album = get_object_or_404(Album, id=album_id)

    # Get existing tracker or None
    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()

    form = AlbumTrackerForm(request.POST, instance=tracker, user=request.user)
    if form.is_valid():
        tracker = form.save(commit=False)
        tracker.user = request.user
        tracker.album = album
        tracker.save()
        messages.success(request, f"Saved {album.title}")
    else:
        messages.error(request, f"Error saving {album.title}: {form.errors}")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("album_detail", album_id=album.id)


@require_POST
def album_delete(request):
    """Delete an album tracker - mirrors artist_delete."""
    from django.shortcuts import get_object_or_404

    from app.models import AlbumTracker

    album_id = request.POST.get("album_id")
    album = get_object_or_404(Album, id=album_id)

    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {album.title} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("album_detail", album_id=album.id)


@require_POST
def song_save(request):
    """Handle adding a listen for a song - mirrors episode_save for episodes."""
    from django.shortcuts import get_object_or_404
    from django.utils import timezone
    from django.utils.dateparse import parse_date, parse_datetime

    from app.models import Track

    recording_id = request.POST.get("recording_id")
    album_id = request.POST.get("album_id")
    track_id = request.POST.get("track_id")
    end_date_str = request.POST.get("end_date")

    # Parse the end date
    end_date = None
    if end_date_str:
        end_date = parse_datetime(end_date_str)
        if end_date:
            if timezone.is_naive(end_date):
                end_date = timezone.make_aware(end_date)
        else:
            parsed_date = parse_date(end_date_str)
            if parsed_date:
                end_date = timezone.make_aware(
                    timezone.datetime.combine(parsed_date, timezone.datetime.min.time()),
                )

    # Get the album and track
    album = get_object_or_404(Album, id=album_id)
    track = get_object_or_404(Track, id=track_id) if track_id else None

    # Check if user already has a Music entry for this track
    existing_music = Music.objects.filter(
        user=request.user,
        album=album,
        track=track,
    ).first()

    # Calculate runtime from track duration if available
    runtime_minutes = None
    if track and track.duration_ms:
        runtime_minutes = track.duration_ms // 60000  # Convert ms to minutes

    if existing_music:
        # Add a new history entry (rewatch/relisten)
        existing_music.end_date = end_date
        existing_music.save()

        # Update Item runtime if not set and we have it
        if runtime_minutes and existing_music.item and not existing_music.item.runtime_minutes:
            existing_music.item.runtime_minutes = runtime_minutes
            existing_music.item.save(update_fields=["runtime_minutes"])

        messages.success(request, f"Added listen for {track.title if track else 'track'}")
    else:
        # Create new Music entry
        # First, get or create the Item for this recording
        item_defaults = {
            "title": track.title if track else "Unknown Track",
            "image": album.image or settings.IMG_NONE,
        }
        if runtime_minutes:
            item_defaults["runtime_minutes"] = runtime_minutes

        if recording_id:
            item, created = Item.objects.get_or_create(
                media_id=recording_id,
                source=Sources.MUSICBRAINZ.value,
                media_type=MediaTypes.MUSIC.value,
                defaults=item_defaults,
            )
            # Update runtime if item existed but didn't have it
            if not created and not item.runtime_minutes and runtime_minutes:
                item.runtime_minutes = runtime_minutes
                item.save(update_fields=["runtime_minutes"])
        else:
            # Create a placeholder item for tracks without recording ID
            item, created = Item.objects.get_or_create(
                media_id=f"track_{track_id}",
                source=Sources.MUSICBRAINZ.value,
                media_type=MediaTypes.MUSIC.value,
                defaults=item_defaults,
            )
            # Update runtime if item existed but didn't have it
            if not created and not item.runtime_minutes and runtime_minutes:
                item.runtime_minutes = runtime_minutes
                item.save(update_fields=["runtime_minutes"])

        Music.objects.create(
            item=item,
            user=request.user,
            artist=album.artist,
            album=album,
            track=track,
            status=Status.COMPLETED.value,
            end_date=end_date,
        )
        messages.success(request, f"Added {track.title if track else 'track'} to your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("album_detail", album_id=album.id)


@require_POST
def podcast_save(request):
    """Handle adding a play for a podcast episode - mirrors song_save for music."""
    from django.shortcuts import get_object_or_404
    from django.utils import timezone
    from django.utils.dateparse import parse_date, parse_datetime

    from app.models import Podcast, PodcastEpisode, PodcastShow

    episode_uuid = request.POST.get("episode_uuid")
    show_id = request.POST.get("show_id")
    episode_id = request.POST.get("episode_id")
    end_date_str = request.POST.get("end_date")

    # Parse the end date
    end_date = None
    if end_date_str:
        end_date = parse_datetime(end_date_str)
        if end_date:
            if timezone.is_naive(end_date):
                end_date = timezone.make_aware(end_date)
        else:
            parsed_date = parse_date(end_date_str)
            if parsed_date:
                end_date = timezone.make_aware(
                    timezone.datetime.combine(parsed_date, timezone.datetime.min.time()),
                )

    # Get the show and episode
    show = get_object_or_404(PodcastShow, id=show_id)
    episode = get_object_or_404(PodcastEpisode, id=episode_id) if episode_id else None

    # Calculate runtime from episode duration if available
    runtime_minutes = None
    if episode and episode.duration:
        runtime_minutes = episode.duration // 60  # Convert seconds to minutes

    # First, get or create the Item for this episode
    item_defaults = {
        "title": episode.title if episode else "Unknown Episode",
        "image": show.image or settings.IMG_NONE,
    }
    if runtime_minutes:
        item_defaults["runtime_minutes"] = runtime_minutes
    if episode and episode.published:
        item_defaults["release_datetime"] = episode.published

    item, created = Item.objects.get_or_create(
        media_id=episode_uuid,
        source=Sources.POCKETCASTS.value,
        media_type=MediaTypes.PODCAST.value,
        defaults=item_defaults,
    )
    if not created:
        update_fields = []
        if runtime_minutes and item.runtime_minutes != runtime_minutes:
            item.runtime_minutes = runtime_minutes
            update_fields.append("runtime_minutes")
        if episode and episode.published and item.release_datetime != episode.published:
            item.release_datetime = episode.published
            update_fields.append("release_datetime")
        if update_fields:
            item.save(update_fields=update_fields)

    # Check if user already has a Podcast entry for this episode
    existing_podcast = Podcast.objects.filter(
        user=request.user,
        item=item,
    ).first()

    if existing_podcast:
        # Check for duplicate before creating new history entry
        latest_history = existing_podcast.history.filter(end_date__isnull=False).order_by("-end_date").first()
        if latest_history and latest_history.end_date and end_date:
            time_diff = abs((end_date - latest_history.end_date).total_seconds())
            if time_diff < 300:  # 5 minutes threshold
                logger.debug("Skipping duplicate podcast history entry (time difference: %d seconds)", time_diff)
                messages.info(request, f"Play already recorded for {episode.title if episode else 'episode'}")
                # Continue to HTMX/redirect handling below - don't create duplicate but still return proper response
            else:
                # Add a new history entry (replay) by updating end_date
                # This creates a new history record via the historical records system
                existing_podcast.end_date = end_date

                # Update progress if needed
                if runtime_minutes and existing_podcast.progress != runtime_minutes:
                    existing_podcast.progress = runtime_minutes

                existing_podcast.save()
                messages.success(request, f"Added play for {episode.title if episode else 'episode'}")
        else:
            # No existing history or missing dates, proceed with creating history entry
            existing_podcast.end_date = end_date

            # Update progress if needed
            if runtime_minutes and existing_podcast.progress != runtime_minutes:
                existing_podcast.progress = runtime_minutes

            existing_podcast.save()
            messages.success(request, f"Added play for {episode.title if episode else 'episode'}")
    else:
        # Create new Podcast entry
        Podcast.objects.create(
            item=item,
            user=request.user,
            show=show,
            episode=episode,
            status=Status.COMPLETED.value,
            end_date=end_date,
            progress=runtime_minutes if runtime_minutes else 0,
        )
        messages.success(request, f"Added play for {episode.title if episode else 'episode'}")

    # If this is an HTMX request, return the updated episode card HTML
    if request.headers.get("HX-Request"):
        # Reuse the podcast_episodes_api logic to get the updated episode card
        from django.template.loader import render_to_string

        from app import helpers

        # Get the single episode with fresh data
        episode_obj = episode
        if not episode_obj:
            return HttpResponse("Episode not found", status=404)

        # Get user's podcast entry for this episode (should exist now)
        user_podcast = Podcast.objects.filter(
            user=request.user,
            show=show,
            episode=episode_obj,
        ).order_by("-created_at").first()

        # Build enriched episode data (similar to podcast_episodes_api)
        episode_items_data = [{
            "media_id": episode_obj.episode_uuid,
            "source": Sources.POCKETCASTS.value,
            "media_type": MediaTypes.PODCAST.value,
        }]
        enriched_episodes_raw = helpers.enrich_items_with_user_data(
            request,
            episode_items_data,
            user=request.user,
        )
        enriched = enriched_episodes_raw[0] if enriched_episodes_raw else {"item": {"media_id": episode_obj.episode_uuid}, "media": None}

        # Format duration
        duration_str = ""
        if episode_obj.duration:
            hours = episode_obj.duration // 3600
            minutes = (episode_obj.duration % 3600) // 60
            if hours > 0:
                duration_str = f"{hours}h {minutes}m"
            else:
                duration_str = f"{minutes}m"

        # Get history
        all_history = []
        if user_podcast:
            all_history = list(user_podcast.history.filter(end_date__isnull=False).order_by("-end_date")[:10])

            class PodcastHistoryWrapper:
                def __init__(self, podcast, item, history_list):
                    self.item = item
                    self.id = podcast.id
                    self._history_list = history_list

                @property
                def history(self):
                    class HistoryProxy:
                        def __init__(self, history_list):
                            self._history = history_list
                        def all(self):
                            return self._history
                        def count(self):
                            return len(self._history)
                    return HistoryProxy(self._history_list)

            podcast_wrapper = PodcastHistoryWrapper(user_podcast, item, all_history)
        else:
            class DummyPodcast:
                def __init__(self, item):
                    self.item = item
                    self.id = 0
                    self.history = type("History", (), {"count": lambda: 0, "all": list})()
            podcast_wrapper = DummyPodcast(item)

        # Create adapter classes
        class PodcastEpisodeAdapter:
            def __init__(self, episode):
                self.title = episode.title
                self.track_number = episode.episode_number
                self.duration_formatted = self._format_duration(episode.duration) if episode.duration else None
                self.musicbrainz_recording_id = None
                self.id = episode.id
                self.published = episode.published
                self.episode_uuid = episode.episode_uuid

            def _format_duration(self, seconds):
                hours = seconds // 3600
                minutes = (seconds % 3600) // 60
                secs = seconds % 60
                if hours > 0:
                    return f"{hours}:{minutes:02d}:{secs:02d}"
                return f"{minutes}:{secs:02d}"

        class PodcastShowAdapter:
            def __init__(self, show):
                self.image = show.image or settings.IMG_NONE
                self.id = show.id

        # Build episode data
        episode_data = {
            "title": episode_obj.title,
            "episode_number": episode_obj.episode_number or 0,
            "image": show.image or settings.IMG_NONE,
            "air_date": episode_obj.published,
            "runtime": duration_str,
            "overview": "",
            "history": all_history,
            "media": enriched["media"] if enriched else None,
            "item": item,
            "media_id": episode_obj.episode_uuid,
            "source": Sources.POCKETCASTS.value,
            "media_type": MediaTypes.PODCAST.value,
            "track_adapter": PodcastEpisodeAdapter(episode_obj),
            "album_adapter": PodcastShowAdapter(show),
            "music_wrapper": podcast_wrapper,
        }

        # Render just the single episode card
        html = render_to_string(
            "app/components/podcast_episode_list.html",
            {
                "episodes": [episode_data],
                "user": request.user,
                "show": show,
                "IMG_NONE": settings.IMG_NONE,
                "TRACK_TIME": True,
                "has_more": False,
                "show_id": show.id,
            },
            request=request,
        )
        response = HttpResponse(html)
        # Close the modal after successful save
        response["HX-Trigger"] = "closeModal"
        return response

    # Always redirect to media_details page for the podcast show
    # Don't trust the 'next' parameter as it might point to the API endpoint
    from django.utils.text import slugify
    return redirect(
        "media_details",
        source=Sources.POCKETCASTS.value,
        media_type=MediaTypes.PODCAST.value,
        media_id=show.podcast_uuid,
        title=show.slug or slugify(show.title),
    )


@require_POST
def delete_all_album_plays_view(request, album_id):
    """Delete all music plays (listens) for an album."""
    from django.shortcuts import get_object_or_404

    album = get_object_or_404(Album, id=album_id)

    # Get all Music entries for this user and album
    music_entries = Music.objects.filter(
        user=request.user,
        album=album,
    )

    count = music_entries.count()
    if count > 0:
        music_entries.delete()
        messages.success(request, f"Deleted {count} play{'s' if count != 1 else ''} for {album.title}")
    else:
        messages.info(request, f"No plays found for {album.title}")

    # Return HX-Refresh header to reload the page
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@require_POST
def delete_all_artist_plays_view(request, artist_id):
    """Delete all music plays (listens) for an artist."""
    from django.shortcuts import get_object_or_404

    artist = get_object_or_404(Artist, id=artist_id)

    # Get all Music entries for this user and artist (via album)
    music_entries = Music.objects.filter(
        user=request.user,
        album__artist=artist,
    )

    count = music_entries.count()
    if count > 0:
        music_entries.delete()
        messages.success(request, f"Deleted {count} play{'s' if count != 1 else ''} for {artist.name}")
    else:
        messages.info(request, f"No plays found for {artist.name}")

    # Return HX-Refresh header to reload the page
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@require_POST
def sync_album_metadata_view(request, album_id):
    """Manually trigger metadata sync for an album."""
    from django.shortcuts import get_object_or_404

    from app.models import Track
    from app.providers import musicbrainz
    from app.services.music import ensure_album_has_release_id

    album = get_object_or_404(Album, id=album_id)

    # Ensure we have a release_id
    ensure_album_has_release_id(album)

    if album.musicbrainz_release_id:
        try:
            # Fetch fresh data from MusicBrainz
            release_data = musicbrainz.get_release(album.musicbrainz_release_id)

            # Update album image
            new_image = release_data.get("image", "")
            if new_image and new_image != settings.IMG_NONE:
                album.image = new_image

            if release_data.get("genres"):
                album.genres = release_data.get("genres")

            # Update tracks
            tracks_data = release_data.get("tracks", [])
            for track_data in tracks_data:
                Track.objects.update_or_create(
                    album=album,
                    disc_number=track_data.get("disc_number", 1),
                    track_number=track_data.get("track_number"),
                    defaults={
                        "title": track_data.get("title", "Unknown Track"),
                        "musicbrainz_recording_id": track_data.get("recording_id"),
                        "duration_ms": track_data.get("duration_ms"),
                        "genres": track_data.get("genres", []) or release_data.get("genres", []),
                    },
                )

            album.tracks_populated = True
            album.save(update_fields=["tracks_populated", "image", "genres"])

            messages.success(request, f"Synced {len(tracks_data)} tracks for {album.title}")
        except Exception as e:
            logger.warning("Failed to sync album %s: %s", album.title, e)
            messages.error(request, f"Failed to sync album: {e}")
    else:
        messages.warning(request, "Could not find a MusicBrainz release for this album")

    # Return HX-Refresh header to reload the page
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@require_GET
def statistics(request):
    """Return the statistics page."""
    try:
        # Set default date range to last year
        timeformat = "%Y-%m-%d"
        today = timezone.localdate()
        one_year_ago = today.replace(year=today.year - 1)

        # Get date parameters with defaults
        start_date_param = request.GET.get("start-date")
        end_date_param = request.GET.get("end-date")

        if not start_date_param and not end_date_param:
            preferred_range = getattr(request.user, "statistics_default_range", None)
            if preferred_range not in statistics_cache.PREDEFINED_RANGES:
                preferred_range = "Last 12 Months"
            preferred_start, preferred_end = _get_predefined_range_date_strings(
                preferred_range,
                today,
                timeformat,
            )
            if preferred_start and preferred_end:
                start_date_str = preferred_start
                end_date_str = preferred_end
            else:
                start_date_str = one_year_ago.strftime(timeformat)
                end_date_str = today.strftime(timeformat)
        else:
            start_date_str = start_date_param or one_year_ago.strftime(timeformat)
            end_date_str = end_date_param or today.strftime(timeformat)

        if start_date_str == "all" and end_date_str == "all":
            start_date = None
            end_date = None
        else:
            start_date = parse_date(start_date_str)
            end_date = parse_date(end_date_str)

            if start_date and end_date:
                # Convert to datetime with timezone awareness
                start_date = timezone.make_aware(
                    datetime.combine(start_date, datetime.min.time()),
                    timezone.get_current_timezone(),
                )

                # End date should be end of day
                end_date = timezone.make_aware(
                    datetime.combine(end_date, datetime.max.time()),
                    timezone.get_current_timezone(),
                )

        # Identify predefined range for caching
        selected_range_name = _identify_predefined_range(start_date, end_date)

        if selected_range_name in statistics_cache.PREDEFINED_RANGES:
            request.user.update_preference("statistics_default_range", selected_range_name)

        # Get statistics data (cached for predefined ranges, computed inline for custom ranges)
        statistics_data = statistics_cache.get_statistics_data(
            request.user,
            start_date,
            end_date,
            range_name=selected_range_name,
        )

        show_year_charts = selected_range_name in (None, "All Time")

        # Get top rated by media type for compact cards
        top_rated = statistics_data["top_rated"]  # Keep for backward compatibility with "ALL MEDIA" section
        top_rated_by_type = statistics_data.get("top_rated_by_type", {})
        top_rated_movie = top_rated_by_type.get("movie", [])
        top_rated_tv = top_rated_by_type.get("tv", [])

        # Format dates as strings for URL parameters
        start_date_str_for_url = start_date_str if start_date_str else ""
        end_date_str_for_url = end_date_str if end_date_str else ""

        context = {
            "user": request.user,
            "start_date": start_date,
            "end_date": end_date,
            "start_date_str": start_date_str_for_url,
            "end_date_str": end_date_str_for_url,
            "selected_range_name": selected_range_name,
            "media_count": statistics_data["media_count"],
            "activity_data": statistics_data["activity_data"],
            "media_type_distribution": statistics_data["media_type_distribution"],
            "score_distribution": statistics_data["score_distribution"],
            "top_rated": statistics_data["top_rated"],
            "top_rated_movie": top_rated_movie,
            "top_rated_tv": top_rated_tv,
            "top_played": statistics_data["top_played"],
            "top_talent": statistics_data.get("top_talent", {}),
            "status_distribution": statistics_data["status_distribution"],
            "status_pie_chart_data": statistics_data["status_pie_chart_data"],
            "hours_per_media_type": statistics_data["hours_per_media_type"],
            "tv_consumption": statistics_data["tv_consumption"],
            "movie_consumption": statistics_data["movie_consumption"],
            "music_consumption": statistics_data["music_consumption"],
            "podcast_consumption": statistics_data["podcast_consumption"],
            "game_consumption": statistics_data["game_consumption"],
            "daily_hours_by_media_type": statistics_data["daily_hours_by_media_type"],
            "history_highlights": statistics_data.get("history_highlights", {}),
            "show_year_charts": show_year_charts,
            "media_type_colors": {
                "tv": config.get_stats_color(MediaTypes.TV.value),
                "movie": config.get_stats_color(MediaTypes.MOVIE.value),
                "game": config.get_stats_color(MediaTypes.GAME.value),
                "music": config.get_stats_color(MediaTypes.MUSIC.value),
                "podcast": config.get_stats_color(MediaTypes.PODCAST.value),
            },
        }

        return render(request, "app/statistics.html", context)
    except OperationalError as error:
        logger.error("Database error in statistics view: %s", error, exc_info=True)
        # Return empty state on database error
        timeformat = "%Y-%m-%d"
        today = timezone.localdate()
        one_year_ago = today.replace(year=today.year - 1)
        start_date_str = request.GET.get("start-date") or one_year_ago.strftime(timeformat)
        end_date_str = request.GET.get("end-date") or today.strftime(timeformat)

        # Create empty statistics data structure
        empty_statistics_data = {
            "media_count": {},
            "activity_data": [],
            "media_type_distribution": {},
            "hours_per_media_type": {},
            "media_type_colors": {
                "tv": config.get_stats_color(MediaTypes.TV.value),
                "movie": config.get_stats_color(MediaTypes.MOVIE.value),
                "game": config.get_stats_color(MediaTypes.GAME.value),
                "music": config.get_stats_color(MediaTypes.MUSIC.value),
                "podcast": config.get_stats_color(MediaTypes.PODCAST.value),
            },
            "score_distribution": {},
            "top_rated": [],
            "top_played": [],
            "top_talent": {},
            "status_distribution": {},
            "status_pie_chart_data": {},
            "hours_per_media_type": {},
            "tv_consumption": {},
            "movie_consumption": {},
            "music_consumption": {},
            "podcast_consumption": {},
            "game_consumption": {},
            "daily_hours_by_media_type": {},
            "history_highlights": {},
        }

        context = {
            "user": request.user,
            "start_date": parse_date(start_date_str) if start_date_str != "all" else None,
            "end_date": parse_date(end_date_str) if end_date_str != "all" else None,
            "media_count": empty_statistics_data["media_count"],
            "activity_data": empty_statistics_data["activity_data"],
            "media_type_distribution": empty_statistics_data["media_type_distribution"],
            "score_distribution": empty_statistics_data["score_distribution"],
            "top_rated": empty_statistics_data["top_rated"],
            "top_played": empty_statistics_data["top_played"],
            "top_talent": empty_statistics_data["top_talent"],
            "status_distribution": empty_statistics_data["status_distribution"],
            "status_pie_chart_data": empty_statistics_data["status_pie_chart_data"],
            "hours_per_media_type": empty_statistics_data["hours_per_media_type"],
            "tv_consumption": empty_statistics_data["tv_consumption"],
            "movie_consumption": empty_statistics_data["movie_consumption"],
            "music_consumption": empty_statistics_data["music_consumption"],
            "podcast_consumption": empty_statistics_data["podcast_consumption"],
            "game_consumption": empty_statistics_data["game_consumption"],
            "daily_hours_by_media_type": empty_statistics_data["daily_hours_by_media_type"],
            "history_highlights": empty_statistics_data["history_highlights"],
            "media_type_colors": empty_statistics_data["media_type_colors"],
            "show_year_charts": False,
            "database_error": True,
        }
        return render(request, "app/statistics.html", context)


@require_POST
def refresh_statistics(request):
    """Force refresh statistics cache for the current range."""
    from django.http import JsonResponse
    
    range_name = request.POST.get("range_name")
    if not range_name:
        return JsonResponse({"error": "range_name is required"}, status=400)
    
    if range_name not in statistics_cache.PREDEFINED_RANGES:
        return JsonResponse({"error": "Invalid range_name"}, status=400)
    
    # Invalidate the cache and schedule a refresh
    statistics_cache.invalidate_statistics_cache(request.user.id, range_name)
    statistics_cache.schedule_statistics_refresh(
        request.user.id,
        range_name,
        debounce_seconds=0,  # No debounce for manual refresh
        countdown=0,  # Start immediately
        allow_inline=True,
    )
    
    return JsonResponse({"success": True, "message": "Statistics refresh scheduled"})


@require_GET
def cache_status(request):
    """Return cache status metadata for history or statistics cache.
    
    Query params:
        cache_type: 'history' or 'statistics'
        range_name: Required for statistics, ignored for history
        logging_style: Optional for history, defaults to 'repeats'
    
    Returns JSON with:
        exists: bool - Whether cache exists
        built_at: str - ISO format timestamp when cache was built (or None)
        is_stale: bool - Whether cache is considered stale
        is_refreshing: bool - Whether a refresh is currently in progress
        recently_built: bool - Whether cache was built in the last 30 seconds
    """
    cache_type = request.GET.get("cache_type")
    if cache_type not in ("history", "statistics"):
        return JsonResponse({"error": "Invalid cache_type. Must be 'history' or 'statistics'"}, status=400)

    if cache_type == "history":
        logging_style = request.GET.get("logging_style")
        if logging_style not in ("sessions", "repeats"):
            logging_style = "repeats"
        cache_entry = cache.get(history_cache._cache_key(request.user.id, logging_style))
        refresh_lock_key = history_cache._refresh_lock_key(request.user.id, logging_style)
        refresh_lock = history_cache._clean_refresh_lock(refresh_lock_key)
        lock_has_day_keys = isinstance(refresh_lock, dict) and bool(refresh_lock.get("day_keys"))
        
        # Also check dedupe_key if lock has day_keys (for page_days refreshes)
        dedupe_key = None
        if lock_has_day_keys and isinstance(refresh_lock, dict):
            dedupe_key = refresh_lock.get("dedupe_key")
            if dedupe_key and dedupe_key != refresh_lock_key:
                # Check if dedupe lock is stale
                dedupe_lock = history_cache._clean_refresh_lock(dedupe_key)
                if dedupe_lock is None:
                    # Dedupe lock is stale/missing, clear main lock too
                    cache.delete(refresh_lock_key)
                    refresh_lock = None
                    lock_has_day_keys = False

        # Debug logging to help diagnose lock issues
        logger.debug(
            "Cache status check for user %s, logging_style %s: lock_key=%s, lock_exists=%s",
            request.user.id,
            logging_style,
            refresh_lock_key,
            refresh_lock is not None,
        )

        if cache_entry:
            built_at = cache_entry.get("built_at")
            is_stale = False
            recently_built = False
            if built_at:
                age = timezone.now() - built_at
                is_stale = age > history_cache.HISTORY_STALE_AFTER
                # Consider cache "recently built" if it was built in the last 60 seconds
                # This helps catch refreshes that completed just before or during page load
                recently_built = age < timedelta(seconds=60)
                # If the cache was just rebuilt but the lock is still set, clear it
                # to avoid a stuck "refreshing" state on the frontend.
                if refresh_lock and recently_built and not lock_has_day_keys:
                    cache.delete(refresh_lock_key)
                    refresh_lock = None
                # If cache is fresh (not stale), ignore lingering locks for index rebuilds.
                # Page-day refresh locks should remain until the task completes.
                if not is_stale and refresh_lock and not lock_has_day_keys:
                    cache.delete(refresh_lock_key)
                    refresh_lock = None

            return JsonResponse({
                "exists": True,
                "built_at": built_at.isoformat() if built_at else None,
                "is_stale": is_stale,
                "is_refreshing": refresh_lock is not None,
                "recently_built": recently_built,
            })
        return JsonResponse({
            "exists": False,
            "built_at": None,
            "is_stale": False,
            "is_refreshing": refresh_lock is not None,
            "recently_built": False,
        })

    if cache_type == "statistics":
        range_name = request.GET.get("range_name")
        if not range_name:
            return JsonResponse({"error": "range_name is required for statistics cache"}, status=400)

        if range_name not in statistics_cache.PREDEFINED_RANGES:
            return JsonResponse({
                "exists": False,
                "built_at": None,
                "is_stale": False,
                "is_refreshing": False,
                "recently_built": False,
                "any_range_refreshing": False,
            })

        cache_key = statistics_cache._cache_key(request.user.id, range_name)
        refresh_lock_key = statistics_cache._refresh_lock_key(request.user.id, range_name)
        cache_entry = cache.get(cache_key)
        refresh_lock = cache.get(refresh_lock_key)
        if refresh_lock and statistics_cache._lock_is_stale(refresh_lock):
            cache.delete(refresh_lock_key)
            refresh_lock = None

        any_range_refreshing = statistics_cache._any_range_refreshing(request.user.id)
        metadata_lock, metadata_built_at, metadata_recently_built = (
            statistics_cache._metadata_refresh_status(request.user.id)
        )
        metadata_refreshing = metadata_lock is not None

        refresh_scheduled = False
        if cache_entry:
            built_at = cache_entry.get("built_at")
            history_version = cache_entry.get("history_version")
            current_version = statistics_cache._get_history_version(request.user.id)
            is_stale = False
            recently_built = False
            age = None
            if built_at:
                age = timezone.now() - built_at
                # Consider cache "recently built" if it was built in the last 60 seconds
                # This helps catch refreshes that completed just before or during page load
                recently_built = age < timedelta(seconds=60)
            if history_version:
                is_stale = history_version != current_version
            elif age:
                is_stale = age > statistics_cache.STATISTICS_STALE_AFTER

            if not is_stale and refresh_lock:
                cache.delete(refresh_lock_key)
                refresh_lock = None
            elif is_stale and refresh_lock is None:
                refresh_scheduled = statistics_cache.schedule_statistics_refresh(
                    request.user.id,
                    range_name,
                    allow_inline=False,
                )
                refresh_lock = cache.get(refresh_lock_key) if refresh_scheduled else refresh_lock

            is_refreshing = refresh_lock is not None or refresh_scheduled or metadata_refreshing
            return JsonResponse({
                "exists": True,
                "built_at": built_at.isoformat() if built_at else None,
                "is_stale": is_stale,
                "is_refreshing": is_refreshing,
                "recently_built": recently_built,
                "any_range_refreshing": any_range_refreshing,
                "refresh_scheduled": refresh_scheduled,
                "metadata_refreshing": metadata_refreshing,
                "metadata_built_at": metadata_built_at.isoformat() if metadata_built_at else None,
                "metadata_recently_built": metadata_recently_built,
            })
        is_refreshing = refresh_lock is not None or metadata_refreshing
        return JsonResponse({
            "exists": False,
            "built_at": None,
            "is_stale": False,
            "is_refreshing": is_refreshing,
            "recently_built": False,
            "any_range_refreshing": any_range_refreshing,
            "refresh_scheduled": False,
            "metadata_refreshing": metadata_refreshing,
            "metadata_built_at": metadata_built_at.isoformat() if metadata_built_at else None,
            "metadata_recently_built": metadata_recently_built,
        })


@require_GET
def service_worker(request):
    """Serve the service worker file from static files."""
    sw_path = Path(settings.STATICFILES_DIRS[0]) / "js" / "serviceworker.js"
    with sw_path.open(encoding="utf-8") as sw_file:
        response = HttpResponse(sw_file.read(), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    return response


def _sort_tv_media_by_time_left(media_list, direction="asc"):
    """Sort TV media by time left with explicit grouping order.

    Group order:
      1) Active (episodes_left > 0 for non-dropped statuses) by least total time left first
      2) In-Progress caught-up (episodes_left == 0) newest end_date first
      3) Completed (episodes_left == 0) newest end_date first
      4) Dropped (episodes_left may be 0 or > 0) newest end_date first
      5) Unreleased/unknown runtime at the very end
    """
    import logging

    from django.core.cache import cache

    from app.statistics import parse_runtime_to_minutes

    logger = logging.getLogger(__name__)

    def _calc_unwatched_runtime_total(media, episodes_left_count):
        """Sum actual runtimes for unwatched episodes instead of using averages.

        Returns (total_runtime, episodes_with_data) or (None, 0) if no data available.
        """
        from app.models import Item, MediaTypes

        breakdown = getattr(media, "released_episode_breakdown", {})
        if not breakdown:
            return None, 0

        total_runtime = 0
        episodes_with_runtime_data = 0
        remaining_progress = media.progress

        # Process seasons in order to determine which episodes are unwatched
        for season_num in sorted(breakdown.keys()):
            season_episode_count = breakdown[season_num]

            if remaining_progress >= season_episode_count:
                # User has watched all episodes in this season
                remaining_progress -= season_episode_count
            else:
                # User is partway through this season or hasn't started it
                watched_in_season = remaining_progress
                remaining_progress = 0

                # Query unwatched episodes in this season (episode_number > watched count)
                unwatched_episodes = Item.objects.filter(
                    media_id=media.item.media_id,
                    source=media.item.source,
                    media_type=MediaTypes.EPISODE.value,
                    season_number=season_num,
                    episode_number__gt=watched_in_season,
                    runtime_minutes__isnull=False,
                ).exclude(
                    runtime_minutes=999999,  # Exclude placeholder for unknown runtime
                ).exclude(
                    runtime_minutes=999998,  # Exclude 999998 marker for "aired but runtime unknown"
                ).values_list("runtime_minutes", flat=True)

                runtimes = list(unwatched_episodes)
                if runtimes:
                    total_runtime += sum(runtimes)
                    episodes_with_runtime_data += len(runtimes)
                    logger.debug(
                        f"{media.item.title} S{season_num}: {len(runtimes)} unwatched eps "
                        f"(after ep {watched_in_season}), runtime sum={sum(runtimes)}min",
                    )

        if episodes_with_runtime_data > 0:
            return total_runtime, episodes_with_runtime_data
        return None, 0

    def _calc_runtime_minutes(media):
        """Best-effort average runtime in minutes for a TV show (fallback only)."""
        runtime_minutes = None
        # FIRST: Check locally stored runtime (but exclude fallback markers)
        if hasattr(media, "item") and media.item.runtime_minutes:
            # Exclude fallback values: 999998 (aired but runtime unknown) and 999999 (unknown runtime)
            if media.item.runtime_minutes < 999998:
                runtime_minutes = media.item.runtime_minutes
                logger.debug(f"Using stored runtime for {media.item.title}: {runtime_minutes}min")
            else:
                logger.debug(f"Skipping invalid runtime marker ({media.item.runtime_minutes}min) for {media.item.title}")

        if not runtime_minutes:
            # SECOND: Check for episode-level runtime data from database
            # This is the most accurate - uses actual episode runtimes that were saved when viewing season pages
            from app.models import Item, MediaTypes
            episodes_with_runtime = Item.objects.filter(
                media_id=media.item.media_id,
                source=media.item.source,
                media_type=MediaTypes.EPISODE.value,
                runtime_minutes__isnull=False,
            ).exclude(
                runtime_minutes=999999,  # Exclude placeholder for unknown runtime
            ).exclude(
                runtime_minutes=999998,  # Exclude 999998 marker for "aired but runtime unknown"
            ).values_list("runtime_minutes", flat=True)

            if episodes_with_runtime.exists():
                # Calculate average runtime from actual episodes
                episode_runtimes = list(episodes_with_runtime)
                runtime_minutes = round(sum(episode_runtimes) / len(episode_runtimes))
                logger.debug(f"Using average episode runtime for {media.item.title}: {runtime_minutes}min (from {len(episode_runtimes)} episodes)")

        if not runtime_minutes:
            # THIRD: Check cached season data (avg_runtime field from season metadata)
            season_cache_key = f"tmdb_season_{media.item.media_id}_1"
            cached_season_data = cache.get(season_cache_key)
            if cached_season_data and cached_season_data.get("details", {}).get("runtime"):
                runtime_str = cached_season_data["details"]["runtime"]
                runtime_minutes = parse_runtime_to_minutes(runtime_str)
                if runtime_minutes and runtime_minutes > 0:
                    logger.debug(f"Using cached season avg runtime for {media.item.title}: {runtime_minutes}min")
            # Try other seasons if season 1 didn't work
            if not runtime_minutes:
                for season_num in [2, 3, 4, 5]:
                    season_cache_key = f"tmdb_season_{media.item.media_id}_{season_num}"
                    cached_season_data = cache.get(season_cache_key)
                    if cached_season_data and cached_season_data.get("details", {}).get("runtime"):
                        runtime_str = cached_season_data["details"]["runtime"]
                        runtime_minutes = parse_runtime_to_minutes(runtime_str)
                        if runtime_minutes and runtime_minutes > 0:
                            logger.debug(f"Using cached season {season_num} avg runtime for {media.item.title}: {runtime_minutes}min")
                            break

        # FOURTH: Use industry standard fallback
        if not runtime_minutes or runtime_minutes <= 0:
            if media.item.source == "tmdb":
                runtime_minutes = 30
            elif media.item.source == "mal":
                runtime_minutes = 23
            else:
                runtime_minutes = 30
            logger.debug(f"Using fallback runtime for {media.item.title}: {runtime_minutes}min")
        return runtime_minutes

    def _get_total_time_left(media, episodes_left):
        """Get total time left by summing actual unwatched episode runtimes, with fallback."""
        # First, try to sum actual unwatched episode runtimes
        total_runtime, eps_with_data = _calc_unwatched_runtime_total(media, episodes_left)

        if total_runtime is not None and eps_with_data == episodes_left:
            # We have runtime data for all unwatched episodes - use exact sum
            logger.debug(
                f"{media.item.title}: Using exact sum of {eps_with_data} unwatched episodes = {total_runtime}min",
            )
            return total_runtime
        if total_runtime is not None and eps_with_data > 0:
            # Partial data: use what we have + estimate for missing episodes
            missing_eps = episodes_left - eps_with_data
            avg_runtime = total_runtime / eps_with_data
            estimated_missing = int(missing_eps * avg_runtime)
            final_total = total_runtime + estimated_missing
            logger.debug(
                f"{media.item.title}: Partial data - {eps_with_data} eps={total_runtime}min + "
                f"{missing_eps} eps estimated={estimated_missing}min (avg {avg_runtime:.0f}min/ep)",
            )
            return final_total
        # No runtime data for unwatched episodes - fall back to average method
        runtime = _calc_runtime_minutes(media)
        if not runtime or runtime <= 0:
            runtime = 30
        total = episodes_left * runtime
        logger.debug(
            f"{media.item.title}: Fallback to average - {episodes_left} eps × {runtime}min = {total}min",
        )
        return total

    def _end_date_for_sort(media):
        # Prefer aggregated_end_date when present, else media.end_date
        return getattr(media, "aggregated_end_date", None) or getattr(media, "end_date", None) or getattr(media, "progressed_at", None) or getattr(media, "created_at", None)

    def _effective_max_progress(media):
        """Prefer annotated max_progress; fallback to DB episodes to avoid negatives."""
        annotated = getattr(media, "max_progress", 0) or 0
        if annotated <= 0 or annotated < media.progress:
            total_from_db = 0
            # Use prefetched seasons/episodes when available
            if hasattr(media, "seasons"):
                for season in media.seasons.all():
                    if getattr(season.item, "season_number", 0) and hasattr(season, "episodes"):
                        max_ep_num = 0
                        for ep in season.episodes.all():
                            ep_num = getattr(ep.item, "episode_number", 0) or 0
                            max_ep_num = max(max_ep_num, ep_num)
                        total_from_db += max_ep_num
            return max(annotated, total_from_db)
        return annotated

    # Explicit bucketing for deterministic grouping
    active_statuses = {Status.IN_PROGRESS.value, Status.PLANNING.value, Status.PAUSED.value}
    group_active = []           # episodes_left > 0 and status in active_statuses
    group_inprog_zero = []      # status == IN_PROGRESS and episodes_left == 0
    group_completed = []        # status == COMPLETED and episodes_left == 0
    group_dropped = []          # status == DROPPED
    group_tail = []             # everything else (unreleased/unknown)

    for media in media_list:
        # Compute effective episodes_left
        if not hasattr(media, "max_progress"):
            group_tail.append(media)
            continue

        annotated_max = getattr(media, "max_progress", None)
        status = getattr(media, "status", Status.IN_PROGRESS.value)

        # Keep sorting fast by relying on scheduled calendar refreshes.
        fallback_max = _effective_max_progress(media) or 0
        effective_max = max(annotated_max or 0, fallback_max, media.progress)

        media.max_progress = effective_max
        episodes_left = effective_max - media.progress
        episodes_left = max(episodes_left, 0)

        # Debug shows that should have episodes left but show 0
        if media.progress > 0 and episodes_left == 0 and media.item.title in ["Taskmaster", "Rent-a-Girlfriend", "The Last of Us"]:
            logger.debug(f"DEBUG 0 episodes: {media.item.title} - progress={media.progress}, max_progress={effective_max}, episodes_left={episodes_left}")

        status = getattr(media, "status", Status.IN_PROGRESS.value)

        if status == Status.DROPPED.value:
            group_dropped.append(media)
            continue

        if episodes_left == 0 and status == Status.IN_PROGRESS.value:
            group_inprog_zero.append(media)
            continue

        if episodes_left == 0 and status == Status.COMPLETED.value:
            group_completed.append(media)
            continue

        if episodes_left > 0 and status in active_statuses:
            group_active.append((media, episodes_left))
            continue

        group_tail.append(media)

    # Sort each group
    # 1) Active by least total minutes left
    def _active_key(entry):
        media, episodes_left = entry
        # Use sum of actual unwatched episode runtimes instead of average
        total = _get_total_time_left(media, episodes_left)
        # Store the display values using non-property attributes
        media.episodes_left_display = episodes_left
        if total > 0:
            hours = int(total // 60)
            minutes = int(total % 60)
            if hours > 0:
                media.time_left_display = f"{hours}h {minutes}m"
            else:
                media.time_left_display = f"{minutes}m"
        else:
            media.time_left_display = f"{episodes_left} ep" if episodes_left > 0 else "-"
        return (total, media.item.title.lower())
    group_active_sorted = [m for (m, _) in sorted(group_active, key=_active_key)]

    # 2) In-Progress caught-up by newest end_date
    for m in group_inprog_zero:
        m.episodes_left_display = 0
        m.time_left_display = "0m"
    group_inprog_zero_sorted = sorted(
        group_inprog_zero,
        key=lambda m: (-( _end_date_for_sort(m).timestamp() if _end_date_for_sort(m) else float("-inf") ), m.item.title.lower()),
    )

    # 3) Completed by newest end_date
    for m in group_completed:
        m.episodes_left_display = 0
        m.time_left_display = "0m"
    group_completed_sorted = sorted(
        group_completed,
        key=lambda m: (-( _end_date_for_sort(m).timestamp() if _end_date_for_sort(m) else float("-inf") ), m.item.title.lower()),
    )

    # 4) Dropped - show remaining content (sorted by least time left)
    for m in group_dropped:
        # Debug logging for first few dropped shows
        if not hasattr(m, "_debug_logged"):
            m._debug_logged = True
            logger.debug(f"Dropped show: {m.item.title} - progress={m.progress}, max_progress={getattr(m, 'max_progress', 'MISSING')}, hasattr={hasattr(m, 'max_progress')}")

        # Calculate episodes remaining (not watched)
        if hasattr(m, "max_progress") and hasattr(m, "progress") and m.max_progress > 0:
            episodes_left = m.max_progress - m.progress
            episodes_left = max(episodes_left, 0)
            m.episodes_left_display = episodes_left

            if episodes_left > 0:
                # Use sum of actual unwatched episode runtimes
                total = _get_total_time_left(m, episodes_left)
                hours = int(total // 60)
                minutes = int(total % 60)
                if hours > 0:
                    m.time_left_display = f"{hours}h {minutes}m"
                else:
                    m.time_left_display = f"{minutes}m"
                # Store total for sorting
                m._time_left_total = total
            else:
                m.time_left_display = "0m"
                m._time_left_total = 0
        else:
            # No max_progress data - show as unknown
            logger.debug(f"Dropped show NO DATA: {m.item.title} - Setting '-' display")
            m.episodes_left_display = 0
            m.time_left_display = "-"
            m._time_left_total = 0

    # Sort dropped by least time left (ascending), then by title
    group_dropped_sorted = sorted(
        group_dropped,
        key=lambda m: (getattr(m, "_time_left_total", 0), m.item.title.lower()),
    )

    # 5) Tail (unreleased/unknown) - set display values
    for m in group_tail:
        m.episodes_left_display = 0
        m.time_left_display = "-"

    sorted_list = (
        group_active_sorted
        + group_inprog_zero_sorted
        + group_completed_sorted
        + group_dropped_sorted
        + group_tail
    )
    logger.debug(
        "DEBUG: Group counts -> active: %d, inprog_zero: %d, completed: %d, dropped: %d, tail: %d",
        len(group_active_sorted), len(group_inprog_zero_sorted), len(group_completed_sorted), len(group_dropped_sorted), len(group_tail),
    )

    # Log first 10 items for debugging
    logger.debug("DEBUG: First 10 sorted shows:")
    for i, media in enumerate(sorted_list[:10]):
        episodes_left = media.max_progress - media.progress if hasattr(media, "max_progress") else 0
        logger.debug(f"  {i+1}. {media.item.title} - Episodes left: {episodes_left}, Status: {getattr(media, 'status', 'Unknown')}")

    if direction == "desc":
        return list(reversed(sorted_list))

    return sorted_list


def _identify_predefined_range(start_date, end_date):
    if start_date is None and end_date is None:
        return "All Time"

    if not start_date or not end_date:
        return None

    # Use timezone.localdate to avoid off-by-one when converting aware datetimes
    # (localtime(...).date() can shift the date if the aware datetime is at UTC midnight)
    local_start = timezone.localdate(start_date)
    local_end = timezone.localdate(end_date)
    today = timezone.localdate()

    if local_start == today and local_end == today:
        return "Today"

    yesterday = today - timedelta(days=1)
    if local_start == yesterday and local_end == yesterday:
        return "Yesterday"

    monday = today - timedelta(days=today.weekday())
    if local_start == monday and local_end == today:
        return "This Week"

    if local_start == today - timedelta(days=6) and local_end == today:
        return "Last 7 Days"

    month_start = today.replace(day=1)
    if local_start == month_start and local_end == today:
        return "This Month"

    if local_start == today - timedelta(days=29) and local_end == today:
        return "Last 30 Days"

    if local_start == today - timedelta(days=89) and local_end == today:
        return "Last 90 Days"

    year_start = today.replace(month=1, day=1)
    if local_start == year_start and local_end == today:
        return "This Year"

    six_months_start = _adjust_month_delta(today, months=6)
    if _dates_close(local_start, six_months_start) and local_end == today:
        return "Last 6 Months"

    twelve_months_start = _adjust_month_delta(today, months=12)
    if _dates_close(local_start, twelve_months_start) and local_end == today:
        return "Last 12 Months"

    return None


def _get_predefined_range_date_strings(range_name, today, timeformat):
    if range_name == "All Time":
        return "all", "all"

    start_date = None
    end_date = today

    if range_name == "Today":
        start_date = today
    elif range_name == "Yesterday":
        start_date = today - timedelta(days=1)
        end_date = start_date
    elif range_name == "This Week":
        start_date = today - timedelta(days=today.weekday())
    elif range_name == "Last 7 Days":
        start_date = today - timedelta(days=6)
    elif range_name == "This Month":
        start_date = today.replace(day=1)
    elif range_name == "Last 30 Days":
        start_date = today - timedelta(days=29)
    elif range_name == "Last 90 Days":
        start_date = today - timedelta(days=89)
    elif range_name == "This Year":
        start_date = today.replace(month=1, day=1)
    elif range_name == "Last 6 Months":
        start_date = _adjust_month_delta(today, months=6)
    elif range_name == "Last 12 Months":
        start_date = _adjust_month_delta(today, months=12)

    if start_date is None:
        return None, None

    return start_date.strftime(timeformat), end_date.strftime(timeformat)


def _adjust_month_delta(reference_date, months):
    candidate = reference_date - relativedelta(months=months)
    if candidate.day != reference_date.day:
        candidate = (candidate.replace(day=1) + relativedelta(months=1)) - timedelta(days=1)
    return candidate


def _dates_close(date_one, date_two, tolerance_days=1):
    return abs((date_one - date_two).days) <= tolerance_days


@require_GET
def collection_list(request, media_type=None):
    """Display user's collection, optionally filtered by media_type."""
    collection = helpers.get_user_collection(request.user, media_type)
    paginator = Paginator(collection, 20)
    page_number = request.GET.get("page", 1)

    try:
        page_obj = paginator.page(page_number)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return render(
        request,
        "app/collection_list.html",
        {
            "collection_entries": page_obj,
            "media_type": media_type,
        },
    )


@require_POST
def collection_add(request):
    """Add item to collection (with optional metadata)."""
    item_id = request.POST.get("item_id")
    if not item_id:
        if request.headers.get("HX-Request"):
            return HttpResponseBadRequest("Item ID is required")
        messages.error(request, "Item ID is required")
        return redirect("collection_list")

    try:
        item = Item.objects.get(id=item_id)
    except Item.DoesNotExist:
        if request.headers.get("HX-Request"):
            return HttpResponseBadRequest("Item not found")
        messages.error(request, "Item not found")
        return redirect("collection_list")

    # Check if entry already exists
    existing_entry = helpers.is_item_collected(request.user, item)
    
    # Create mutable POST data and add item
    post_data = request.POST.copy()
    post_data["item"] = item.id
    
    if existing_entry:
        # Update instead of creating duplicate
        form = CollectionEntryForm(
            post_data,
            instance=existing_entry,
            user=request.user,
            collection_media_type=item.media_type,
        )
    else:
        form = CollectionEntryForm(
            post_data,
            user=request.user,
            collection_media_type=item.media_type,
        )

    if form.is_valid():
        entry = form.save(commit=False)
        entry.user = request.user
        entry.item = item
        entry.save()
        collected_at = form.cleaned_data.get("collected_at")
        if collected_at:
            CollectionEntry.objects.filter(id=entry.id).update(collected_at=collected_at)
            entry.collected_at = collected_at
        messages.success(request, f"Added {item.title} to collection")
    else:
        helpers.form_error_messages(form, request)

    if request.headers.get("HX-Request"):
        return JsonResponse({"success": True, "message": f"Added {item.title} to collection"})
    return redirect("collection_list")


@require_POST
def collection_update(request, entry_id):
    """Update collection entry metadata."""
    try:
        entry = CollectionEntry.objects.get(id=entry_id, user=request.user)
    except CollectionEntry.DoesNotExist:
        from django.http import Http404
        raise Http404("Collection entry not found")

    form = CollectionEntryForm(
        request.POST,
        instance=entry,
        user=request.user,
        collection_media_type=entry.item.media_type,
    )
    if form.is_valid():
        entry = form.save()
        collected_at = form.cleaned_data.get("collected_at")
        if collected_at:
            CollectionEntry.objects.filter(id=entry.id).update(collected_at=collected_at)
            entry.collected_at = collected_at
        messages.success(request, f"Updated collection entry for {entry.item.title}")
    else:
        helpers.form_error_messages(form, request)

    if request.headers.get("HX-Request"):
        return JsonResponse({"success": True, "message": f"Updated collection entry"})
    return redirect("collection_list")


@require_POST
def collection_remove(request, entry_id):
    """Remove item from collection."""
    try:
        entry = CollectionEntry.objects.get(id=entry_id, user=request.user)
    except CollectionEntry.DoesNotExist:
        from django.http import Http404
        raise Http404("Collection entry not found")

    item_title = entry.item.title
    entry.delete()
    messages.success(request, f"Removed {item_title} from collection")

    if request.headers.get("HX-Request"):
        return JsonResponse({"success": True, "message": f"Removed {item_title} from collection"})
    return redirect("collection_list")


@never_cache
@require_GET
def collection_modal(request, source, media_type, media_id):
    """Return modal HTML for adding/editing collection entry."""
    def _parse_optional_int(value):
        if value in (None, "", "null"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    season_number = _parse_optional_int(request.GET.get("season_number"))
    episode_number = _parse_optional_int(request.GET.get("episode_number"))

    lookup = {
        "media_id": media_id,
        "source": source,
        "media_type": media_type,
    }

    if media_type == MediaTypes.SEASON.value:
        if season_number is None:
            if request.headers.get("HX-Request"):
                return HttpResponseBadRequest("Season number is required")
            messages.error(request, "Season number is required")
            return redirect("home")
        lookup["season_number"] = season_number
    elif media_type == MediaTypes.EPISODE.value:
        if season_number is None or episode_number is None:
            if request.headers.get("HX-Request"):
                return HttpResponseBadRequest("Season and episode numbers are required")
            messages.error(request, "Season and episode numbers are required")
            return redirect("home")
        lookup["season_number"] = season_number
        lookup["episode_number"] = episode_number

    item = Item.objects.filter(**lookup).first()
    metadata = None
    needs_metadata = item is None or media_type == MediaTypes.GAME.value

    if needs_metadata:
        try:
            metadata = services.get_media_metadata(
                media_type,
                media_id,
                source,
                [season_number] if season_number is not None else None,
                episode_number=episode_number,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Collection modal metadata lookup failed for %s: %s", media_id, exc)

    if not item:
        item_defaults = {
            "title": "",
            "image": settings.IMG_NONE,
        }
        try:
            item_defaults["title"] = (
                (metadata or {}).get("title")
                or (metadata or {}).get("season_title")
                or (metadata or {}).get("name")
                or ""
            )
            item_defaults["image"] = (metadata or {}).get("image") or settings.IMG_NONE

            if media_type == MediaTypes.BOOK.value:
                item_defaults["number_of_pages"] = (
                    (metadata or {}).get("max_progress")
                    or (metadata or {}).get("details", {}).get("number_of_pages")
                )

            if (metadata or {}).get("details", {}).get("runtime"):
                from app.statistics import parse_runtime_to_minutes
                runtime_minutes = parse_runtime_to_minutes((metadata or {})["details"]["runtime"])
                if runtime_minutes:
                    item_defaults["runtime_minutes"] = runtime_minutes
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Collection modal metadata lookup failed for %s: %s", media_id, exc)

        item, _ = Item.objects.get_or_create(
            **lookup,
            defaults=item_defaults,
        )

    # Check if collection entry already exists
    platform_choices = None
    if media_type == MediaTypes.GAME.value:
        platforms = (metadata or {}).get("details", {}).get("platforms") or []
        if platforms:
            platform_choices = platforms

    existing_entry = helpers.is_item_collected(request.user, item)
    form = CollectionEntryForm(
        instance=existing_entry,
        user=request.user,
        collection_media_type=item.media_type,
        collection_choices_override={"resolution": platform_choices} if platform_choices else None,
    )
    form.fields["item"].initial = item.id

    return_url = request.GET.get("return_url", "")
    collection_fields = getattr(form, "collection_fields", [])

    response = render(
        request,
        "app/components/collection_modal.html",
        {
            "item": item,
            "entry": existing_entry,
            "form": form,
            "return_url": return_url,
            "collection_fields": collection_fields,
        },
    )
    # Explicitly set cache control headers for Safari compatibility
    # @never_cache should handle this, but Safari can be aggressive with caching
    response["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    response["Vary"] = "Cookie, HX-Request"
    return response


@login_required
@require_GET
@never_cache
def collection_status_api(request, item_id):
    """API endpoint to check if collection entry exists for an item."""
    from django.http import JsonResponse
    from app.helpers import is_item_collected
    
    try:
        item = Item.objects.get(id=item_id)
        collection_entry = is_item_collected(request.user, item)
        
        return JsonResponse({
            "has_collection_data": collection_entry is not None,
            "item_id": item_id,
        })
    except Item.DoesNotExist:
        return JsonResponse({"error": "Item not found"}, status=404)
