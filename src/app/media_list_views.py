import calendar
import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_DOWN, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.contrib.auth.decorators import login_required, login_not_required
from django.core.paginator import Paginator
from django.db.models import F, Min
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import formats, timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from app import cache_utils, helpers
from app.columns import (
    resolve_column_config,
    resolve_columns,
    resolve_default_column_config,
    sanitize_column_prefs,
)
from app import history_cache
from app.log_safety import safe_url
from app.models import (
    TV,
    Artist,
    ArtistTracker,
    BasicMedia,
    Book,
    CollectionEntry,
    Episode,
    Item,
    ItemTag,
    MediaTypes,
    PodcastShow,
    PodcastShowTracker,
    Sources,
    Status,
    Tag,
    prefill_episode_runtime_index,
    Track,
)
from app.search_views import _mark_grouped_anime_route
from app.release_years import prefill_display_release_years
from app.templatetags import app_tags
from app.tv_sort import _sort_tv_media_by_time_left
from users.models import MediaSortChoices, MediaStatusChoices

logger = logging.getLogger(__name__)

MEDIA_RATING_CHOICES = (
    ("all", "All"),
    ("rated", "Rated"),
    # "not_rated" is handled in logic but not shown in dropdown (toggle behavior)
)
MEDIA_LIST_NO_STATUS = "no_status"
MEDIA_LIST_NO_STATUS_LABEL = "No Status"
RECENTLY_NOT_RATED_KEY = "recently_not_rated"
RECENTLY_NOT_RATED_LABEL = "Recently Played - Not Rated"
RECENTLY_NOT_RATED_DAYS = 7


@dataclass
class MediaListEntry:
    """Template-facing list entry that may or may not have a tracker row."""

    item: object
    media: object | None = None

    @classmethod
    def from_media(cls, media):
        return cls(item=getattr(media, "item", None), media=media)

    @property
    def is_untracked(self) -> bool:
        return self.media is None

    @property
    def item_id(self):
        if self.media is not None:
            return getattr(self.media, "item_id", None)
        return getattr(self.item, "id", None)

    def __bool__(self):
        return self.media is not None

    def __getattr__(self, attr):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)
        media = self.__dict__.get("media")
        if media is None:
            return None
        return getattr(media, attr, None)


def _tracked_media_entries(entries):
    """Return the tracker-backed objects from mixed media-list entries."""
    tracked_entries = []
    for entry in entries:
        tracked_media = getattr(entry, "media", entry)
        if tracked_media is not None:
            tracked_entries.append(tracked_media)
    return tracked_entries


def _collect_reading_activity_day_keys(entries):
    """Return history/statistics day keys touched by reading entries."""
    day_keys = set()
    for entry in entries or []:
        start_dt = getattr(entry, "start_date", None)
        end_dt = getattr(entry, "end_date", None)
        if start_dt and end_dt:
            range_keys = history_cache.history_day_keys_for_range(start_dt, end_dt)
            day_keys.update(range_keys or [])
        activity_dt = end_dt or start_dt or getattr(entry, "created_at", None)
        activity_key = history_cache.history_day_key(activity_dt)
        if activity_key:
            day_keys.add(activity_key)
    return sorted(day_keys)


FORMAT_LABELS = {
    "hardcover": "Hardcover",
    "paperback": "Paperback",
    "ebook": "eBook",
    "audiobook": "Audiobook",
}


def _normalize_filter_value(value):
    return str(value or "").strip().lower()


def _extract_item_languages(item):
    """Extract languages from database fields only."""
    if not item:
        return []
    languages = getattr(item, "languages", None)
    if not languages:
        return []
    if isinstance(languages, list):
        return [str(lang).strip() for lang in languages if str(lang).strip()]
    return [str(languages).strip()] if str(languages).strip() else []


def _extract_item_country(item):
    """Extract country from database fields only."""
    if not item:
        return ""
    country = getattr(item, "country", None)
    return str(country).strip() if country else ""


def _extract_item_platforms(item):
    """Extract platforms from database fields only."""
    if not item:
        return []
    platforms = getattr(item, "platforms", None)
    if not platforms:
        return []
    if isinstance(platforms, list):
        return [str(p).strip() for p in platforms if str(p).strip()]
    return [str(platforms).strip()] if str(platforms).strip() else []


def _extract_item_authors(item):
    """Extract authors from database fields only."""
    if not item:
        return []
    authors = getattr(item, "authors", None)
    if not authors:
        return []
    if not isinstance(authors, list):
        authors = [authors]
    normalized = []
    for raw_author in authors:
        if isinstance(raw_author, dict):
            author_name = (
                raw_author.get("name")
                or raw_author.get("person")
                or raw_author.get("author")
            )
        else:
            author_name = raw_author
        author_text = str(author_name).strip() if author_name else ""
        if author_text:
            normalized.append(author_text)
    return normalized


def _extract_item_formats(item, collection_formats_by_item_id=None):
    """Extract normalized format values from Item and collection metadata."""
    formats = set()
    if item and hasattr(item, "format") and item.format:
        normalized_item_format = _normalize_filter_value(item.format)
        if normalized_item_format:
            formats.add(normalized_item_format)
    if item and collection_formats_by_item_id:
        formats.update(collection_formats_by_item_id.get(item.id, set()))
    return formats


def _extract_item_platforms_with_collection(item, collection_platforms_by_item_id=None):
    """Extract platform values, preferring explicit collection platform entries."""
    if not item:
        return []
    if collection_platforms_by_item_id:
        explicit_platforms = collection_platforms_by_item_id.get(item.id, set())
        if explicit_platforms:
            return sorted(explicit_platforms, key=lambda value: value.lower())
    return _extract_item_platforms(item)


def build_filter_data_from_items(
    media_items,
    *,
    collection_formats_by_item_id=None,
    collection_platforms_by_item_id=None,
):
    """Build filter menu option lists from a sequence of media/Item objects.

    Accepts either ORM media objects (with a ``.item`` FK attribute) or raw
    Item instances.  Pass ``collection_formats_by_item_id`` /
    ``collection_platforms_by_item_id`` from the media-list view to include
    collection-enriched format/platform data; omit them (or pass None) for
    contexts that don't have collection data (e.g. person detail pages).
    """
    from app.models import Sources

    genres_set = set()
    implied_genres_set = set()
    years_set = set()
    sources_set = set()
    languages_set = set()
    countries_set = set()
    platforms_set = set()
    formats_set = set()
    authors_set = set()
    has_unknown_year = False
    for media in media_items:
        item = getattr(media, "item", media)
        if not item:
            continue
        for genre in getattr(item, "genres", None) or []:
            genre_value = str(genre).strip()
            if genre_value:
                genres_set.add(genre_value)
        for genre in getattr(item, "implied_genres", None) or []:
            genre_value = str(genre).strip()
            if genre_value:
                implied_genres_set.add(genre_value)
        release_dt = getattr(item, "release_datetime", None)
        if release_dt and getattr(release_dt, "year", None):
            years_set.add(release_dt.year)
        else:
            has_unknown_year = True
        if getattr(item, "source", None):
            sources_set.add(item.source)
        db_languages = _extract_item_languages(item)
        if db_languages:
            languages_set.update(db_languages)
        country_value = _extract_item_country(item)
        if country_value:
            countries_set.add(country_value)
        platforms = _extract_item_platforms_with_collection(
            item, collection_platforms_by_item_id
        )
        if platforms:
            platforms_set.update(platforms)
        authors = _extract_item_authors(item)
        if authors:
            authors_set.update(authors)
        item_formats = _extract_item_formats(item, collection_formats_by_item_id)
        if item_formats:
            formats_set.update(item_formats)

    genres = sorted(genres_set, key=lambda value: value.lower())
    implied_genres = sorted(implied_genres_set, key=lambda value: value.lower())
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
    formats = [
        {
            "value": value,
            "label": FORMAT_LABELS.get(_normalize_filter_value(value), value.title()),
        }
        for value in sorted(formats_set, key=lambda val: val.lower())
    ]
    authors = [
        {"value": value, "label": value}
        for value in sorted(authors_set, key=lambda val: val.lower())
    ]
    return {
        "genres": genres,
        "implied_genres": implied_genres,
        "years": years,
        "sources": sources,
        "languages": languages,
        "countries": countries,
        "platforms": platforms,
        "origins": [],
        "formats": formats,
        "authors": authors,
        "show_languages": False,
        "show_countries": False,
        "show_platforms": False,
        "show_origins": False,
        "show_formats": False,
        "show_authors": False,
    }


def media_list(request, media_type):
    """Return the media list page."""
    route_media_type = media_type
    comic_subview = None
    if route_media_type == MediaTypes.COMIC.value:
        comic_subview = request.GET.get("subview", "comics")
        if comic_subview not in {"comics", "issues"}:
            comic_subview = "comics"

    previous_sort = getattr(request.user, f"{route_media_type}_sort")
    sorted_media_sort_choices = sorted(
        MediaSortChoices.choices,
        key=lambda choice: str(choice[1]).lower(),
    )
    author_media_types = (
        MediaTypes.BOOK.value,
        MediaTypes.MANGA.value,
        MediaTypes.COMIC.value,
        MediaTypes.COMIC_ISSUE.value,
    )
    critic_rating_media_types = {
        MediaTypes.TV.value,
        MediaTypes.SEASON.value,
        MediaTypes.MOVIE.value,
        MediaTypes.ANIME.value,
        MediaTypes.MANGA.value,
        MediaTypes.GAME.value,
        MediaTypes.BOARDGAME.value,
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
    }
    popularity_media_types = {
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    }
    progress_media_types = {
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    }
    plays_media_types = {
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    }
    runtime_media_types = {
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    }
    next_episode_air_date_media_types = {
        MediaTypes.TV.value,
        MediaTypes.SEASON.value,
        MediaTypes.ANIME.value,
    }
    layout = request.user.update_preference(
        f"{route_media_type}_layout",
        request.GET.get("layout"),
    )
    sort_filter = request.user.update_preference(
        f"{route_media_type}_sort",
        request.GET.get("sort"),
    )
    direction_param = request.GET.get("direction")
    direction_field = f"{route_media_type}_direction"

    # Enforce media-type-specific sort options.
    effective_media_type = (
        MediaTypes.COMIC_ISSUE.value
        if route_media_type == MediaTypes.COMIC.value and comic_subview == "issues"
        else route_media_type
    )

    if sort_filter == "time_left" and effective_media_type != MediaTypes.TV.value:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{route_media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "runtime" and effective_media_type not in runtime_media_types:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{route_media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "time_to_beat" and effective_media_type != MediaTypes.GAME.value:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{route_media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "plays" and effective_media_type not in plays_media_types:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{route_media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "time_watched" and effective_media_type not in runtime_media_types:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{route_media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "next_episode_air_date" and effective_media_type not in next_episode_air_date_media_types:
        sort_filter = "title"  # Default fallback
        request.user.update_preference(f"{route_media_type}_sort", "title")
        direction_param = None
    elif sort_filter == "author" and effective_media_type not in author_media_types:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{route_media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "critic_rating" and effective_media_type not in critic_rating_media_types:
        sort_filter = "title"
        request.user.update_preference(f"{route_media_type}_sort", "title")
        direction_param = None
    elif sort_filter == "popularity" and effective_media_type not in popularity_media_types:
        sort_filter = "title"
        request.user.update_preference(f"{route_media_type}_sort", "title")
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
    media_type = effective_media_type

    # Pre-filter sort choices to only include those valid for the current media type.
    # critic_rating and author remain template-gated via supports_critic_rating_sort /
    # filter_data.show_authors; all other per-type exclusions are handled here so the
    # sort dropdown template only needs a simple {% for %} loop.
    _sort_type_guards = {
        "progress": lambda mt: mt != MediaTypes.MOVIE.value,
        "time_left": lambda mt: mt == MediaTypes.TV.value,
        "runtime": lambda mt: mt in runtime_media_types,
        "popularity": lambda mt: mt in popularity_media_types,
        "time_to_beat": lambda mt: mt == MediaTypes.GAME.value,
        "plays": lambda mt: mt in plays_media_types,
        "time_watched": lambda mt: mt in runtime_media_types,
        "next_episode_air_date": lambda mt: mt in next_episode_air_date_media_types,
    }
    sorted_media_sort_choices = [
        (value, label)
        for value, label in sorted_media_sort_choices
        if value not in _sort_type_guards or _sort_type_guards[value](media_type)
    ]

    supports_untracked_status_filter = media_type not in {
        MediaTypes.MUSIC.value,
        MediaTypes.PODCAST.value,
    }
    raw_status_filter = request.GET.get("status")
    valid_statuses = {choice[0] for choice in MediaStatusChoices.choices}
    persisted_status_filter = getattr(
        request.user,
        f"{route_media_type}_status",
        MediaStatusChoices.ALL,
    )

    if raw_status_filter in valid_statuses:
        status_filter = request.user.update_preference(
            f"{route_media_type}_status",
            raw_status_filter,
        )
    elif raw_status_filter is None:
        status_filter = persisted_status_filter
    elif (
        supports_untracked_status_filter
        and raw_status_filter == MEDIA_LIST_NO_STATUS
    ):
        status_filter = MEDIA_LIST_NO_STATUS
    else:
        status_filter = persisted_status_filter

    status_choices = list(MediaStatusChoices.choices)
    if supports_untracked_status_filter:
        status_choices.insert(1, (MEDIA_LIST_NO_STATUS, MEDIA_LIST_NO_STATUS_LABEL))

    rating_filter = request.GET.get("rating", "all")
    # Allow "not_rated" even though it's not in display choices (toggle behavior)
    valid_rating_filters = {"all", "rated", "not_rated"}
    if rating_filter not in valid_rating_filters:
        rating_filter = "all"
    
    collection_filter = request.GET.get("collection", "all")
    valid_collection_filters = {"all", "collected", "not_collected"}
    if collection_filter not in valid_collection_filters:
        collection_filter = "all"

    progress_filter = (request.GET.get("progress") or "all").strip().lower()
    valid_progress_filters = {"all", "not_caught_up", "caught_up"}
    if progress_filter not in valid_progress_filters or media_type not in progress_media_types:
        progress_filter = "all"

    genre_filter = (request.GET.get("genre") or "").strip()
    implied_genre_filter = (request.GET.get("implied_genre") or "").strip()
    if media_type != MediaTypes.MUSIC.value:
        implied_genre_filter = ""
    year_filter = (request.GET.get("year") or "").strip()
    release_filter = (request.GET.get("release") or "all").strip().lower()
    valid_release_filters = {"all", "released", "not_released"}
    if release_filter not in valid_release_filters:
        release_filter = "all"
    source_filter = (request.GET.get("source") or "").strip()
    language_filter = (request.GET.get("language") or "").strip()
    country_filter = (request.GET.get("country") or "").strip()
    platform_filter = (request.GET.get("platform") or "").strip()
    origin_filter = (request.GET.get("origin") or "").strip()
    format_filter = (request.GET.get("format") or "").strip()
    author_filter = (request.GET.get("author") or "").strip()
    tag_filter = (request.GET.get("tag") or "").strip()
    tag_exclude_filter = (request.GET.get("tag_exclude") or "").strip()

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

    def apply_latest_status_filter(media_items, filter_value):
        """Filter against each item's latest aggregated status."""
        if not filter_value or filter_value == MediaStatusChoices.ALL:
            return media_items
        if filter_value == MEDIA_LIST_NO_STATUS:
            return [
                media
                for media in media_items
                if getattr(media, "media", media) is None
            ]
        filtered_items = []
        for media in media_items:
            latest_status = (
                getattr(media, "aggregated_status", None)
                or getattr(media, "status", None)
            )
            if latest_status == filter_value:
                filtered_items.append(media)
        return filtered_items

    def apply_collection_filter(media_items, filter_value, user, media_type):
        """Filter media items based on collection status.

        For TV shows, checks both show-level and episode-level collection entries.
        Uses one CollectionEntry query and bulk episode lookup instead of per-item queries.
        """
        if filter_value == "all":
            return media_items

        from app.models import Item, CollectionEntry, MediaTypes

        collected_item_ids = frozenset(
            CollectionEntry.objects.filter(user=user).values_list("item_id", flat=True),
        )

        tv_anime_types = (MediaTypes.TV.value, MediaTypes.ANIME.value)
        episode_ids_by_show = {}
        if media_type in tv_anime_types and media_items:
            show_keys = {
                (m.item.media_id, m.item.source)
                for m in media_items
                if getattr(m, "item", None)
            }
            if show_keys:
                media_ids = {k[0] for k in show_keys}
                sources = {k[1] for k in show_keys}
                episode_rows = Item.objects.filter(
                    media_type=MediaTypes.EPISODE.value,
                    media_id__in=media_ids,
                    source__in=sources,
                ).values_list("id", "media_id", "source")
                for eid, mid, src in episode_rows:
                    key = (mid, src)
                    if key in show_keys:
                        episode_ids_by_show.setdefault(key, []).append(eid)

        def show_has_episode_collection(media):
            key = (media.item.media_id, media.item.source)
            return any(eid in collected_item_ids for eid in episode_ids_by_show.get(key, ()))

        filtered_items = []
        for media in media_items:
            has_collection = media.item_id in collected_item_ids
            if not has_collection and media_type in tv_anime_types:
                has_collection = show_has_episode_collection(media)

            if filter_value == "collected" and has_collection:
                filtered_items.append(media)
            elif filter_value == "not_collected" and not has_collection:
                filtered_items.append(media)

        return filtered_items

    def _is_caught_up_media(media):
        """Return True when the item's watched progress has reached released progress."""
        return helpers.is_caught_up_media(media)

    def apply_progress_filter(media_items, filter_value, media_type):
        if filter_value == "all" or media_type not in progress_media_types:
            return media_items

        tracked_media_items = _tracked_media_entries(media_items)
        if tracked_media_items and any(
            getattr(media, "max_progress", None) is None
            for media in tracked_media_items
        ):
            BasicMedia.objects.annotate_max_progress(tracked_media_items, media_type)

        if filter_value == "caught_up":
            return [
                media
                for media in media_items
                if getattr(media, "media", media) is not None
                and _is_caught_up_media(media)
            ]
        if filter_value == "not_caught_up":
            return [
                media
                for media in media_items
                if getattr(media, "media", media) is not None
                and not _is_caught_up_media(media)
            ]
        return media_items

    def _release_date_from_value(value):
        if value is None:
            return None
        if isinstance(value, date) and not hasattr(value, "hour"):
            return value
        if hasattr(value, "date"):
            try:
                if hasattr(value, "utcoffset") and timezone.is_aware(value):
                    return timezone.localtime(value).date()
            except Exception:
                pass
            try:
                return value.date()
            except Exception:
                return None
        return None

    def _matches_release_filter_value(release_value, filter_value, today):
        if filter_value == "all":
            return True
        release_date = _release_date_from_value(release_value)
        if not release_date:
            return filter_value == "not_released"
        if filter_value == "released":
            return release_date <= today
        if filter_value == "not_released":
            return release_date > today
        return True

    collection_formats_by_item_id = defaultdict(set)
    collection_platforms_by_item_id = defaultdict(set)

    def apply_format_filter(media_items, filter_value):
        if not filter_value:
            return media_items
        target = _normalize_filter_value(filter_value)
        filtered_items = []
        for media in media_items:
            item = getattr(media, "item", None)
            if not item:
                continue
            item_formats = _extract_item_formats(item, collection_formats_by_item_id)
            if target in item_formats:
                filtered_items.append(media)
        return filtered_items

    def apply_author_filter(media_items, filter_value):
        if not filter_value:
            return media_items
        target = _normalize_filter_value(filter_value)
        filtered_items = []
        for media in media_items:
            item = getattr(media, "item", None)
            if not item:
                continue
            authors = _extract_item_authors(item)
            if any(_normalize_filter_value(author) == target for author in authors):
                filtered_items.append(media)
        return filtered_items

    def _author_sort_value(media):
        item = getattr(media, "item", None)
        authors = _extract_item_authors(item)
        return authors[0].strip() if authors else ""

    def sort_media_items_by_author(media_items, sort_direction):
        with_author = []
        without_author = []

        for media in media_items:
            if _author_sort_value(media):
                with_author.append(media)
            else:
                without_author.append(media)

        with_author.sort(
            key=lambda media: (
                _author_sort_value(media).lower(),
                getattr(getattr(media, "item", None), "title", "").lower(),
            ),
            reverse=sort_direction == "desc",
        )
        without_author.sort(
            key=lambda media: getattr(getattr(media, "item", None), "title", "").lower(),
        )
        return with_author + without_author

    def _game_time_to_beat_sort_value(media):
        item = getattr(media, "item", None)
        if not item:
            return None
        return item.game_time_to_beat_minutes

    def sort_media_items_by_game_time_to_beat(media_items, sort_direction):
        with_time_to_beat = []
        without_time_to_beat = []

        for media in media_items:
            minutes = _game_time_to_beat_sort_value(media)
            if minutes:
                with_time_to_beat.append((media, minutes))
            else:
                without_time_to_beat.append(media)

        if sort_direction == "desc":
            with_time_to_beat.sort(
                key=lambda entry: (
                    -entry[1],
                    getattr(getattr(entry[0], "item", None), "title", "").lower(),
                ),
            )
        else:
            with_time_to_beat.sort(
                key=lambda entry: (
                    entry[1],
                    getattr(getattr(entry[0], "item", None), "title", "").lower(),
                ),
            )

        without_time_to_beat.sort(
            key=lambda media: getattr(getattr(media, "item", None), "title", "").lower(),
        )
        return [media for media, _minutes in with_time_to_beat] + without_time_to_beat

    def _runtime_sort_value(media):
        return getattr(media, "total_runtime_minutes", None)

    def _plays_sort_value(media):
        aggregated_progress = getattr(media, "aggregated_progress", None)
        if aggregated_progress is not None:
            return aggregated_progress
        return getattr(media, "progress", 0) or 0

    def sort_media_items_by_plays(media_items, sort_direction):
        with_plays = []
        without_plays = []

        for media in media_items:
            plays = _plays_sort_value(media)
            if plays:
                with_plays.append((media, plays))
            else:
                without_plays.append(media)

        if sort_direction == "desc":
            with_plays.sort(
                key=lambda entry: (
                    -entry[1],
                    getattr(getattr(entry[0], "item", None), "title", "").lower(),
                ),
            )
        else:
            with_plays.sort(
                key=lambda entry: (
                    entry[1],
                    getattr(getattr(entry[0], "item", None), "title", "").lower(),
                ),
            )

        without_plays.sort(
            key=lambda media: getattr(getattr(media, "item", None), "title", "").lower(),
        )
        return [media for media, _plays in with_plays] + without_plays

    def _time_watched_sort_value(media):
        return getattr(media, "time_watched_minutes", None)

    def sort_media_items_by_time_watched(media_items, sort_direction):
        with_time_watched = []
        without_time_watched = []

        for media in media_items:
            total_minutes = _time_watched_sort_value(media)
            if total_minutes:
                with_time_watched.append((media, total_minutes))
            else:
                without_time_watched.append(media)

        if sort_direction == "desc":
            with_time_watched.sort(
                key=lambda entry: (
                    -entry[1],
                    getattr(getattr(entry[0], "item", None), "title", "").lower(),
                ),
            )
        else:
            with_time_watched.sort(
                key=lambda entry: (
                    entry[1],
                    getattr(getattr(entry[0], "item", None), "title", "").lower(),
                ),
            )

        without_time_watched.sort(
            key=lambda media: getattr(getattr(media, "item", None), "title", "").lower(),
        )
        return [media for media, _total_minutes in with_time_watched] + without_time_watched

    def sort_media_items_by_runtime(media_items, sort_direction):
        with_runtime = []
        without_runtime = []

        for media in media_items:
            minutes = _runtime_sort_value(media)
            if minutes:
                with_runtime.append((media, minutes))
            else:
                without_runtime.append(media)

        if sort_direction == "desc":
            with_runtime.sort(
                key=lambda entry: (
                    -entry[1],
                    getattr(getattr(entry[0], "item", None), "title", "").lower(),
                ),
            )
        else:
            with_runtime.sort(
                key=lambda entry: (
                    entry[1],
                    getattr(getattr(entry[0], "item", None), "title", "").lower(),
                ),
            )

        without_runtime.sort(
            key=lambda media: getattr(getattr(media, "item", None), "title", "").lower(),
        )
        return [media for media, _minutes in with_runtime] + without_runtime

    def annotate_media_authors(media_items):
        for media in media_items:
            media.display_authors = _extract_item_authors(getattr(media, "item", None))

    # Pre-fetch tag item IDs for include/exclude filters
    tag_included_ids = None
    tag_excluded_ids = None
    if tag_filter:
        tag_included_ids = set(
            ItemTag.objects.filter(
                tag__user=request.user,
                tag__name__iexact=tag_filter,
            ).values_list("item_id", flat=True)
        )
    if tag_exclude_filter:
        tag_excluded_ids = set(
            ItemTag.objects.filter(
                tag__user=request.user,
                tag__name__iexact=tag_exclude_filter,
            ).values_list("item_id", flat=True)
        )

    # Get media list with filters applied
    query_sort_filter = (
        "title"
        if sort_filter in {"author", "runtime", "time_to_beat", "time_watched"}
        else sort_filter
    )

    list_sql_filters = {
        "genre": genre_filter,
        "implied_genre": implied_genre_filter,
        "year": year_filter,
        "release": release_filter,
        "source": source_filter,
        "language": language_filter,
        "country": country_filter,
        "platform": platform_filter,
        "tag_included_ids": tag_included_ids,
        "tag_excluded_ids": tag_excluded_ids,
    }

    anime_library_mode = getattr(
        request.user,
        "anime_library_mode",
        MediaTypes.ANIME.value,
    )
    include_grouped_anime_in_anime = anime_library_mode in {
        MediaTypes.ANIME.value,
        "both",
    }
    include_grouped_anime_in_tv = anime_library_mode in {
        MediaTypes.TV.value,
        "both",
    }
    cache_variant = (
        anime_library_mode
        if media_type in {MediaTypes.ANIME.value, MediaTypes.TV.value}
        else ""
    )

    tracked_status_filter = (
        MediaStatusChoices.ALL
        if status_filter == MEDIA_LIST_NO_STATUS
        else status_filter
    )

    def _item_matches_requested_media_type(item):
        if not item:
            return False
        if media_type == MediaTypes.ANIME.value:
            if item.media_type == MediaTypes.ANIME.value:
                return True
            return (
                include_grouped_anime_in_anime
                and item.media_type == MediaTypes.TV.value
                and getattr(item, "library_media_type", None) == MediaTypes.ANIME.value
            )
        if media_type == MediaTypes.TV.value:
            if item.media_type != MediaTypes.TV.value:
                return False
            return (
                include_grouped_anime_in_tv
                or getattr(item, "library_media_type", None) != MediaTypes.ANIME.value
            )
        return item.media_type == media_type

    def _build_untracked_media_entries(tracked_item_ids, *, ignore_platform_filter=False):
        if not supports_untracked_status_filter:
            return []

        collected_item_ids = set(
            CollectionEntry.objects.filter(user=request.user).values_list("item_id", flat=True),
        )
        if not collected_item_ids:
            return []

        candidate_item_ids = set(collected_item_ids)
        if media_type in {MediaTypes.TV.value, MediaTypes.ANIME.value}:
            episode_pairs = {
                (str(media_id), str(source))
                for media_id, source in Item.objects.filter(
                    id__in=collected_item_ids,
                    media_type=MediaTypes.EPISODE.value,
                ).values_list("media_id", "source")
            }
            if episode_pairs:
                show_media_ids = {media_id for media_id, _source in episode_pairs}
                show_sources = {source for _media_id, source in episode_pairs}
                for show_item in Item.objects.filter(
                    media_type__in=(MediaTypes.TV.value, MediaTypes.ANIME.value),
                    media_id__in=show_media_ids,
                    source__in=show_sources,
                ).only("id", "media_id", "source"):
                    if (str(show_item.media_id), str(show_item.source)) in episode_pairs:
                        candidate_item_ids.add(show_item.id)

        candidate_item_ids -= tracked_item_ids
        if not candidate_item_ids:
            return []

        if media_type == MediaTypes.GAME.value:
            for item_id, collection_platform in CollectionEntry.objects.filter(
                user=request.user,
                item_id__in=candidate_item_ids,
            ).values_list("item_id", "resolution"):
                platform_value = str(collection_platform or "").strip()
                if platform_value:
                    collection_platforms_by_item_id[item_id].add(platform_value)

        if media_type in author_media_types:
            for item_id, collection_format in CollectionEntry.objects.filter(
                user=request.user,
                item_id__in=candidate_item_ids,
            ).exclude(media_type="").values_list("item_id", "media_type"):
                normalized_collection_format = _normalize_filter_value(collection_format)
                if normalized_collection_format:
                    collection_formats_by_item_id[item_id].add(normalized_collection_format)

        effective_platform_filter = "" if ignore_platform_filter else platform_filter
        today = timezone.localdate()
        filtered_items = []
        candidate_items = list(
            Item.objects.filter(id__in=candidate_item_ids).order_by("title", "id"),
        )
        for item in candidate_items:
            if not _item_matches_requested_media_type(item):
                continue
            if search_query:
                normalized_search = _normalize_filter_value(search_query)
                if normalized_search not in _normalize_filter_value(item.title) and normalized_search not in _normalize_filter_value(item.media_id):
                    continue
            if genre_filter:
                normalized_genre = _normalize_filter_value(genre_filter)
                if not any(
                    _normalize_filter_value(genre) == normalized_genre
                    for genre in (getattr(item, "genres", None) or [])
                ):
                    continue
            if implied_genre_filter:
                normalized_implied_genre = _normalize_filter_value(implied_genre_filter)
                if not any(
                    _normalize_filter_value(genre) == normalized_implied_genre
                    for genre in (getattr(item, "implied_genres", None) or [])
                ):
                    continue
            normalized_year = _normalize_filter_value(year_filter)
            if normalized_year == "unknown" and getattr(item, "release_datetime", None):
                continue
            if normalized_year.isdigit():
                release_value = getattr(item, "release_datetime", None)
                release_year = getattr(release_value, "year", None) if release_value else None
                if release_year != int(normalized_year):
                    continue
            if source_filter and getattr(item, "source", None) != source_filter:
                continue
            if not _matches_release_filter_value(
                getattr(item, "release_datetime", None),
                release_filter,
                today,
            ):
                continue
            if language_filter and not any(
                _normalize_filter_value(language) == _normalize_filter_value(language_filter)
                for language in _extract_item_languages(item)
            ):
                continue
            if country_filter and _normalize_filter_value(_extract_item_country(item)) != _normalize_filter_value(country_filter):
                continue
            if effective_platform_filter and media_type == MediaTypes.GAME.value:
                normalized_platform = _normalize_filter_value(effective_platform_filter)
                if not any(
                    _normalize_filter_value(platform) == normalized_platform
                    for platform in _extract_item_platforms_with_collection(item)
                ):
                    continue
            if tag_included_ids is not None and item.id not in tag_included_ids:
                continue
            if tag_excluded_ids is not None and item.id in tag_excluded_ids:
                continue
            filtered_items.append(MediaListEntry(item=item, media=None))

        return filtered_items

    # Cache tracker-backed list results and filter summaries separately.
    _use_media_list_cache = not (
        sort_filter == "time_left" and media_type == MediaTypes.TV.value
    )
    _include_untracked_entries = status_filter == MEDIA_LIST_NO_STATUS
    _media_list_cache_key = (
        cache_utils.build_media_list_cache_key(
            request.user.id,
            media_type,
            sort_filter,
            direction,
            status_filter,
            search_query,
            rating_filter,
            progress_filter,
            collection_filter,
            author_filter,
            format_filter,
            genre_filter,
            implied_genre_filter,
            year_filter,
            release_filter,
            source_filter,
            language_filter,
            country_filter,
            platform_filter,
            origin_filter,
            tag_filter,
            tag_exclude_filter,
            cache_variant,
        )
        if _use_media_list_cache
        else None
    )
    _media_list_filter_cache_key = (
        cache_utils.build_media_list_filter_cache_key(
            request.user.id,
            media_type,
            status_filter,
            search_query,
            progress_filter,
            genre_filter,
            implied_genre_filter,
            year_filter,
            release_filter,
            source_filter,
            language_filter,
            country_filter,
            platform_filter,
            author_filter,
            format_filter,
            tag_filter,
            tag_exclude_filter,
            cache_variant,
        )
        if _use_media_list_cache
        else None
    )
    _media_list_cached = cache.get(_media_list_cache_key) if _media_list_cache_key else None
    filter_data = (
        cache.get(_media_list_filter_cache_key)
        if _media_list_filter_cache_key
        else None
    )

    if _media_list_cached is None:
        media_queryset = BasicMedia.objects.get_media_list(
            user=request.user,
            media_type=media_type,
            status_filter=tracked_status_filter,
            sort_filter=query_sort_filter,
            search=search_query,
            direction=direction,
            list_sql_filters=list_sql_filters,
        )

        # Convert to list for filtering (rating and collection filters work on lists)
        media_list = list(media_queryset)
        if media_type in {MediaTypes.TV.value, MediaTypes.SEASON.value} and not include_grouped_anime_in_tv:
            media_list = [
                media
                for media in media_list
                if getattr(getattr(media, "item", None), "library_media_type", None)
                != MediaTypes.ANIME.value
            ]
        elif media_type == MediaTypes.ANIME.value and include_grouped_anime_in_anime:
            grouped_anime_media = list(
                BasicMedia.objects.get_media_list(
                    user=request.user,
                    media_type=MediaTypes.TV.value,
                    status_filter=tracked_status_filter,
                    sort_filter=query_sort_filter,
                    search=search_query,
                    direction=direction,
                    list_sql_filters=list_sql_filters,
                ),
            )
            grouped_anime_media = [
                media
                for media in grouped_anime_media
                if getattr(getattr(media, "item", None), "library_media_type", None)
                == MediaTypes.ANIME.value
            ]
            _mark_grouped_anime_route(grouped_anime_media)
            media_list.extend(grouped_anime_media)

        tracked_item_ids = {
            media.item_id
            for media in media_list
            if getattr(media, "item_id", None)
        }
        untracked_media_entries = []
        if _include_untracked_entries:
            untracked_media_entries = _build_untracked_media_entries(tracked_item_ids)

        media_list = [MediaListEntry.from_media(media) for media in media_list]
        media_list.extend(untracked_media_entries)

        media_list = apply_latest_status_filter(media_list, status_filter)

        if (
            status_filter == MEDIA_LIST_NO_STATUS
            and media_type != MediaTypes.ANIME.value
            and sort_filter not in {"author", "runtime", "plays", "time_watched", "time_to_beat", "time_left"}
        ):
            _reverse = direction == "desc"
            _none_sentinel = -math.inf if _reverse else math.inf

            def _untracked_sort_key(entry):
                item = getattr(entry, "item", None)
                title = (getattr(item, "title", "") or "").lower()
                if sort_filter == "release_date":
                    val = getattr(item, "release_datetime", None)
                    return (val.timestamp() if val else _none_sentinel, title)
                if sort_filter == "popularity":
                    val = getattr(item, "trakt_popularity_rank", None)
                    return (val if val is not None else _none_sentinel, title)
                if sort_filter == "critic_rating":
                    val = getattr(item, "provider_rating", None)
                    return (val if val is not None else _none_sentinel, title)
                return title

            media_list = sorted(media_list, key=_untracked_sort_key, reverse=_reverse)

        filter_data_source_items = media_list
        if media_type == MediaTypes.GAME.value and platform_filter and filter_data is None:
            filter_sql_filters = {**list_sql_filters, "platform": ""}
            filter_data_source_items = [
                MediaListEntry.from_media(media)
                for media in list(
                    BasicMedia.objects.get_media_list(
                        user=request.user,
                        media_type=media_type,
                        status_filter=tracked_status_filter,
                        sort_filter=query_sort_filter,
                        search=search_query,
                        direction=direction,
                        list_sql_filters=filter_sql_filters,
                    ),
                )
            ]
            if _include_untracked_entries:
                filter_data_source_items.extend(
                    _build_untracked_media_entries(
                        tracked_item_ids,
                        ignore_platform_filter=True,
                    ),
                )
            filter_data_source_items = apply_latest_status_filter(
                filter_data_source_items,
                status_filter,
            )
        if filter_data is None and media_type == MediaTypes.GAME.value:
            item_ids = {
                media.item_id
                for media in filter_data_source_items
                if getattr(media, "item_id", None)
            }
            if item_ids:
                collection_platforms = CollectionEntry.objects.filter(
                    user=request.user,
                    item_id__in=item_ids,
                ).values_list("item_id", "resolution")
                for item_id, collection_platform in collection_platforms:
                    platform_value = str(collection_platform or "").strip()
                    if platform_value:
                        collection_platforms_by_item_id[item_id].add(platform_value)
        if filter_data is None and media_type in author_media_types:
            item_ids = {
                media.item_id
                for media in media_list
                if getattr(media, "item_id", None)
            }
            if item_ids:
                collection_formats = CollectionEntry.objects.filter(
                    user=request.user,
                    item_id__in=item_ids,
                ).exclude(media_type="").values_list("item_id", "media_type")
                for item_id, collection_format in collection_formats:
                    normalized_collection_format = _normalize_filter_value(collection_format)
                    if normalized_collection_format:
                        collection_formats_by_item_id[item_id].add(normalized_collection_format)
        if filter_data is None:
            filter_data = build_filter_data_from_items(
            filter_data_source_items,
            collection_formats_by_item_id=collection_formats_by_item_id,
            collection_platforms_by_item_id=collection_platforms_by_item_id,
        )
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
            filter_data["show_formats"] = media_type in author_media_types
            filter_data["show_authors"] = media_type in author_media_types
            filter_data["show_progress"] = media_type in progress_media_types
            user_tags = list(
                Tag.objects.filter(user=request.user)
                .values_list("name", flat=True)
                .order_by("name")
            )
            filter_data["tags"] = user_tags
            if _media_list_filter_cache_key:
                cache.set(
                    _media_list_filter_cache_key,
                    filter_data,
                    cache_utils.MEDIA_LIST_FILTER_CACHE_TTL,
                )
                cache_utils.register_media_list_cache_key(
                    request.user.id,
                    _media_list_filter_cache_key,
                )
        media_list = apply_rating_filter(media_list, rating_filter)
        media_list = apply_collection_filter(media_list, collection_filter, request.user, media_type)
        media_list = apply_progress_filter(media_list, progress_filter, media_type)
        if media_type in author_media_types:
            media_list = apply_author_filter(media_list, author_filter)
            media_list = apply_format_filter(media_list, format_filter)
        if sort_filter == "author" and media_type in author_media_types:
            media_list = sort_media_items_by_author(media_list, direction)
        if sort_filter == "runtime" and media_type in runtime_media_types:
            BasicMedia.objects.annotate_max_progress(
                _tracked_media_entries(media_list),
                media_type,
            )
            prefill_episode_runtime_index(media_list)
            media_list = sort_media_items_by_runtime(media_list, direction)
        if sort_filter == "plays" and media_type in plays_media_types:
            media_list = sort_media_items_by_plays(media_list, direction)
        if sort_filter == "time_watched" and media_type in runtime_media_types:
            BasicMedia.objects.annotate_max_progress(
                _tracked_media_entries(media_list),
                media_type,
            )
            prefill_episode_runtime_index(media_list)
            media_list = sort_media_items_by_time_watched(media_list, direction)
        if sort_filter == "time_to_beat" and media_type == MediaTypes.GAME.value:
            media_list = sort_media_items_by_game_time_to_beat(media_list, direction)
        if media_type == MediaTypes.ANIME.value and any(
            getattr(getattr(media, "item", None), "media_type", None) == MediaTypes.TV.value
            for media in media_list
        ):
            if sort_filter not in {"plays", "time_watched"}:
                def _sortable_dt(value):
                    if value is not None:
                        return value
                    return (
                        datetime.min.replace(tzinfo=UTC)
                        if direction == "desc"
                        else datetime.max.replace(tzinfo=UTC)
                    )

                def _mixed_sort_key(media):
                    item = getattr(media, "item", None)
                    title = getattr(item, "title", "") or ""
                    if sort_filter == "score":
                        score = getattr(media, "aggregated_score", None)
                        if score is None:
                            score = getattr(media, "score", None)
                        return (score is None, score or 0, title.lower())
                    if sort_filter == "progress":
                        progress = getattr(media, "aggregated_progress", None)
                        if progress is None:
                            progress = getattr(media, "progress", 0)
                        return (progress, title.lower())
                    if sort_filter == "release_date":
                        release_dt = getattr(item, "release_datetime", None)
                        return (_sortable_dt(release_dt), title.lower())
                    if sort_filter == "popularity":
                        rank = getattr(item, "trakt_popularity_rank", None)
                        if rank is None:
                            rank = math.inf if direction == "asc" else -math.inf
                        return (rank, title.lower())
                    if sort_filter == "critic_rating":
                        rating = getattr(item, "provider_rating", None)
                        if rating is None:
                            rating = math.inf if direction == "asc" else -math.inf
                        return (rating, title.lower())
                    if sort_filter == "date_added":
                        return (_sortable_dt(getattr(media, "created_at", None)), title.lower())
                    if sort_filter == "start_date":
                        start_dt = getattr(media, "aggregated_start_date", None) or getattr(media, "start_date", None)
                        return (_sortable_dt(start_dt), title.lower())
                    if sort_filter == "end_date":
                        end_dt = getattr(media, "aggregated_end_date", None) or getattr(media, "end_date", None)
                        return (_sortable_dt(end_dt), title.lower())
                    if sort_filter == "next_episode_air_date":
                        next_episode_air_date = getattr(media, "next_episode_air_date", None)
                        return (_sortable_dt(next_episode_air_date), title.lower())
                    return title.lower()

                _sort_reverse = direction == "desc"
                media_list = sorted(media_list, key=_mixed_sort_key, reverse=_sort_reverse)

        if _media_list_cache_key:
            cache.set(
                _media_list_cache_key,
                media_list,
                cache_utils.MEDIA_LIST_CACHE_TTL,
            )
            cache_utils.register_media_list_cache_key(request.user.id, _media_list_cache_key)
    else:
        media_list = _media_list_cached

    if filter_data is None:
        tracked_item_ids = {
            media.item_id
            for media in media_list
            if getattr(media, "item_id", None)
        }
        filter_data_source_items = media_list
        if media_type == MediaTypes.GAME.value and platform_filter:
            filter_sql_filters = {**list_sql_filters, "platform": ""}
            filter_data_source_items = [
                MediaListEntry.from_media(media)
                for media in list(
                    BasicMedia.objects.get_media_list(
                        user=request.user,
                        media_type=media_type,
                        status_filter=tracked_status_filter,
                        sort_filter=query_sort_filter,
                        search=search_query,
                        direction=direction,
                        list_sql_filters=filter_sql_filters,
                    ),
                )
            ]
            if _include_untracked_entries:
                filter_data_source_items.extend(
                    _build_untracked_media_entries(
                        tracked_item_ids,
                        ignore_platform_filter=True,
                    ),
                )
            filter_data_source_items = apply_latest_status_filter(
                filter_data_source_items,
                status_filter,
            )
        if media_type == MediaTypes.GAME.value:
            item_ids = {
                media.item_id
                for media in filter_data_source_items
                if getattr(media, "item_id", None)
            }
            if item_ids:
                collection_platforms = CollectionEntry.objects.filter(
                    user=request.user,
                    item_id__in=item_ids,
                ).values_list("item_id", "resolution")
                for item_id, collection_platform in collection_platforms:
                    platform_value = str(collection_platform or "").strip()
                    if platform_value:
                        collection_platforms_by_item_id[item_id].add(platform_value)
        if media_type in author_media_types:
            item_ids = {
                media.item_id
                for media in media_list
                if getattr(media, "item_id", None)
            }
            if item_ids:
                collection_formats = CollectionEntry.objects.filter(
                    user=request.user,
                    item_id__in=item_ids,
                ).exclude(media_type="").values_list("item_id", "media_type")
                for item_id, collection_format in collection_formats:
                    normalized_collection_format = _normalize_filter_value(collection_format)
                    if normalized_collection_format:
                        collection_formats_by_item_id[item_id].add(normalized_collection_format)
        filter_data = build_filter_data_from_items(
            filter_data_source_items,
            collection_formats_by_item_id=collection_formats_by_item_id,
            collection_platforms_by_item_id=collection_platforms_by_item_id,
        )
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
        filter_data["show_formats"] = media_type in author_media_types
        filter_data["show_authors"] = media_type in author_media_types
        filter_data["show_progress"] = media_type in progress_media_types
        filter_data["tags"] = list(
            Tag.objects.filter(user=request.user)
            .values_list("name", flat=True)
            .order_by("name")
        )
        if _media_list_filter_cache_key:
            cache.set(
                _media_list_filter_cache_key,
                filter_data,
                cache_utils.MEDIA_LIST_FILTER_CACHE_TTL,
            )
            cache_utils.register_media_list_cache_key(
                request.user.id,
                _media_list_filter_cache_key,
            )

    # Handle time_left sorting for TV shows
    if sort_filter == "time_left" and media_type == MediaTypes.TV.value:
        # Cache sorted results for 5 minutes to avoid expensive re-sorts
        cache_key = cache_utils.build_time_left_cache_key(
            request.user.id,
            media_type,
            status_filter,
            search_query,
            direction,
            rating_filter,
            progress_filter,
            collection_filter,
            genre_filter,
            year_filter,
            release_filter,
            source_filter,
            language_filter,
            country_filter,
            platform_filter,
            origin_filter,
            tag_filter,
            tag_exclude_filter,
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
            BasicMedia.objects.annotate_max_progress(
                _tracked_media_entries(media_list),
                media_type,
            )
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
            max_progress = getattr(media, "max_progress", None)
            progress_value = getattr(media, "progress", None)
            episodes_left = (
                max_progress - progress_value
                if max_progress is not None and progress_value is not None
                else 0
            )
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
            _tracked_media_entries(media_page.object_list),
            media_type,
        )
        # Table runtime/time-watched cells read total_runtime_minutes per row;
        # prefill so the page renders with one episode-runtime query.
        prefill_episode_runtime_index(media_page.object_list)
    prefill_display_release_years(media_page.object_list)

    if media_type in author_media_types:
        annotate_media_authors(media_page.object_list)

    if filter_data is not None:
        filter_data.setdefault("departments", [])

    _layout_class = ".media-grid" if layout == "grid" else ".media-table"
    context = {
        "user": request.user,
        "media_type": route_media_type,
        "media_type_plural": app_tags.media_type_readable_plural(media_type).lower(),
        "media_list": media_page,
        "current_layout": layout,
        "layout_class": _layout_class,
        "media_list_url": reverse("medialist", args=[route_media_type]),
        "filter_hx_target": _layout_class if media_page else "#empty_list",
        "current_sort": sort_filter,
        "current_direction": direction,
        "current_status": status_filter,
        "current_rating": rating_filter,
        "current_collection": collection_filter,
        "current_progress": progress_filter,
        "current_genre": genre_filter,
        "current_implied_genre": implied_genre_filter,
        "current_year": year_filter,
        "current_release": release_filter,
        "current_source": source_filter,
        "current_language": language_filter,
        "current_country": country_filter,
        "current_platform": platform_filter,
        "current_origin": origin_filter,
        "current_format": format_filter,
        "current_author": author_filter,
        "current_tag": tag_filter,
        "current_tag_exclude": tag_exclude_filter,
        "sort_choices": sorted_media_sort_choices,
        "status_choices": status_choices,
        "rating_choices": MEDIA_RATING_CHOICES,
        "filter_data": filter_data,
        "is_artist_list": False,
        "is_album_list": False,
        "supports_critic_rating_sort": media_type in critic_rating_media_types,
    }
    if comic_subview:
        context["current_subview"] = comic_subview

    # For music, show tracked artists instead of individual tracks
    # For podcasts, show tracked shows instead of individual episodes
    # This parallels TV which shows TV shows, not seasons/episodes
    if media_type == MediaTypes.PODCAST.value:
        from app.models import PodcastShowTracker

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

        should_annotate_first_published = (
            release_filter != "all"
            or sort_filter == "release_date"
            or layout == "table"
        )
        if should_annotate_first_published:
            show_trackers = show_trackers.annotate(first_published=Min("show__episodes__published"))

        # Apply sorting
        if sort_filter == "title":
            order = "show__title" if direction == "asc" else "-show__title"
            show_trackers = show_trackers.order_by(order)
        elif sort_filter == "score":
            order = "score" if direction == "asc" else "-score"
            show_trackers = show_trackers.order_by(order, "show__title")
        elif sort_filter == "release_date":
            order = (
                F("first_published").asc(nulls_last=True)
                if direction == "asc"
                else F("first_published").desc(nulls_last=True)
            )
            show_trackers = show_trackers.order_by(order, "show__title")
        elif sort_filter == "date_added":
            order = "created_at" if direction == "asc" else "-created_at"
            show_trackers = show_trackers.order_by(order, "show__title")
        elif sort_filter == "start_date":
            order = "start_date" if direction == "asc" else "-start_date"
            show_trackers = show_trackers.order_by(order)
        else:
            # Default: most recently updated
            show_trackers = show_trackers.order_by("-updated_at")

        show_trackers_list = list(show_trackers)

        if release_filter != "all":
            today = timezone.localdate()
            show_trackers_list = [
                tracker
                for tracker in show_trackers_list
                if _matches_release_filter_value(
                    getattr(tracker, "first_published", None),
                    release_filter,
                    today,
                )
            ]

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
                self.release_datetime = getattr(tracker, "first_published", None)

                # Create a mock Item for compatibility with media components
                # Use the show's podcast_uuid as media_id for routing
                self.item, _ = Item.objects.get_or_create(
                    media_id=tracker.show.podcast_uuid,
                    source=tracker.show.source,
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
            "current_implied_genre": implied_genre_filter,
            "current_year": year_filter,
            "current_release": release_filter,
            "current_source": source_filter,
            "current_language": language_filter,
            "current_country": country_filter,
            "current_platform": platform_filter,
            "current_origin": origin_filter,
            "sort_choices": sorted_media_sort_choices,
            "status_choices": status_choices,
            "rating_choices": MEDIA_RATING_CHOICES,
            "search_query": search_query,
            "filter_data": filter_data,
            "is_artist_list": False,
            "is_album_list": False,
            "supports_critic_rating_sort": False,
        }

    if media_type == MediaTypes.MUSIC.value:
        from app.models import Artist, ArtistTracker, AlbumTracker

        music_subview = request.GET.get("subview", "artists")
        if music_subview not in {"artists", "albums", "tracks"}:
            music_subview = "artists"
        context["current_subview"] = music_subview

        if music_subview == "albums":
            album_trackers = (
                AlbumTracker.objects.filter(user=request.user)
                .select_related("album", "album__artist")
                .prefetch_related("album__artist_credits__artist")
            )

            if status_filter and status_filter != MediaStatusChoices.ALL:
                album_trackers = album_trackers.filter(status=status_filter)

            if search_query:
                album_trackers = album_trackers.filter(album__title__icontains=search_query)

            if rating_filter == "rated":
                album_trackers = album_trackers.filter(score__isnull=False)
            elif rating_filter == "not_rated":
                album_trackers = album_trackers.filter(score__isnull=True)

            should_annotate_release_date = (
                release_filter != "all"
                or sort_filter == "release_date"
                or layout == "table"
            )
            if should_annotate_release_date:
                album_trackers = album_trackers.annotate(
                    first_release_date=F("album__release_date")
                )

            if sort_filter == "title":
                order = "album__title" if direction == "asc" else "-album__title"
                album_trackers = album_trackers.order_by(order)
            elif sort_filter == "score":
                order = "score" if direction == "asc" else "-score"
                album_trackers = album_trackers.order_by(order, "album__title")
            elif sort_filter == "release_date":
                order = (
                    F("album__release_date").asc(nulls_last=True)
                    if direction == "asc"
                    else F("album__release_date").desc(nulls_last=True)
                )
                album_trackers = album_trackers.order_by(order, "album__title")
            elif sort_filter == "date_added":
                order = "created_at" if direction == "asc" else "-created_at"
                album_trackers = album_trackers.order_by(order, "album__title")
            elif sort_filter == "start_date":
                order = "start_date" if direction == "asc" else "-start_date"
                album_trackers = album_trackers.order_by(order)
            else:
                album_trackers = album_trackers.order_by("-updated_at")

            album_trackers_list = list(album_trackers)

            if release_filter != "all":
                today = timezone.localdate()
                album_trackers_list = [
                    tracker
                    for tracker in album_trackers_list
                    if _matches_release_filter_value(
                        getattr(tracker, "first_release_date", None),
                        release_filter,
                        today,
                    )
                ]

            def _build_album_filter_data(trackers):
                genres_set = set()
                for tracker in trackers:
                    for genre in (tracker.album.genres or []):
                        genre_value = str(genre).strip()
                        if genre_value:
                            genres_set.add(genre_value)
                genres = sorted(genres_set, key=lambda value: value.lower())
                return {
                    "genres": genres,
                    "years": [],
                    "sources": [],
                    "languages": [],
                    "countries": [],
                    "platforms": [],
                    "origins": [],
                    "show_languages": False,
                    "show_countries": False,
                    "show_platforms": False,
                    "show_origins": False,
                    "subview": music_subview,
                }

            filter_data = _build_album_filter_data(album_trackers_list)

            if genre_filter:
                target_genre = _normalize_filter_value(genre_filter)
                album_trackers_list = [
                    tracker
                    for tracker in album_trackers_list
                    if any(
                        _normalize_filter_value(genre) == target_genre
                        for genre in (tracker.album.genres or [])
                    )
                ]

            album_paginator = Paginator(album_trackers_list, 32)
            album_page = album_paginator.get_page(page)

            context["media_list"] = album_page
            context["is_artist_list"] = False
            context["is_album_list"] = True
            context["filter_data"] = filter_data
            context["media_type_plural"] = "albums"

        elif music_subview == "tracks":
            context["is_artist_list"] = False
            context["is_album_list"] = False
            context["media_type_plural"] = "tracks"

        if music_subview == "artists":
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

            should_annotate_first_release_date = (
                release_filter != "all"
                or sort_filter == "release_date"
                or layout == "table"
            )
            if should_annotate_first_release_date:
                artist_trackers = artist_trackers.annotate(first_release_date=Min("artist__albums__release_date"))

            # Apply sorting (limited to what makes sense for artists)
            if sort_filter == "title":
                order = "artist__name" if direction == "asc" else "-artist__name"
                artist_trackers = artist_trackers.order_by(order)
            elif sort_filter == "score":
                order = "score" if direction == "asc" else "-score"
                artist_trackers = artist_trackers.order_by(order, "artist__name")
            elif sort_filter == "release_date":
                order = (
                    F("first_release_date").asc(nulls_last=True)
                    if direction == "asc"
                    else F("first_release_date").desc(nulls_last=True)
                )
                artist_trackers = artist_trackers.order_by(order, "artist__name")
            elif sort_filter == "date_added":
                order = "created_at" if direction == "asc" else "-created_at"
                artist_trackers = artist_trackers.order_by(order, "artist__name")
            elif sort_filter == "start_date":
                order = "start_date" if direction == "asc" else "-start_date"
                artist_trackers = artist_trackers.order_by(order)
            else:
                # Default: most recently updated
                artist_trackers = artist_trackers.order_by("-updated_at")

            artist_trackers_list = list(artist_trackers)

            if release_filter != "all":
                today = timezone.localdate()
                artist_trackers_list = [
                    tracker
                    for tracker in artist_trackers_list
                    if _matches_release_filter_value(
                        getattr(tracker, "first_release_date", None),
                        release_filter,
                        today,
                    )
                ]

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
            from app.models import Album

            artists_to_update = []
            seen_artist_ids = set()
            artist_id_to_updated_image = {}  # Track which artists got updated images
            artists_checked = 0
            artists_with_images = 0
            artists_missing_images = 0

            # Partition artists into those with / without images in a single pass.
            artists_missing_image_objects = []
            for tracker in artist_page.object_list:
                artist = tracker.artist
                if artist.id not in seen_artist_ids:
                    seen_artist_ids.add(artist.id)
                    artists_checked += 1
                    has_image = artist.image and artist.image != settings.IMG_NONE and artist.image != ""
                    if has_image:
                        artists_with_images += 1
                    else:
                        artists_missing_images += 1
                        artists_missing_image_objects.append(artist)

            # Batch fetch earliest album image for all artists missing one — 1 query instead of 2 per artist.
            hero_image_map: dict[int, str] = {}
            if artists_missing_image_objects:
                missing_ids = [a.id for a in artists_missing_image_objects]
                seen_for_hero: set[int] = set()
                for row in (
                    Album.objects.filter(artist_id__in=missing_ids)
                    .exclude(image="")
                    .exclude(image=settings.IMG_NONE)
                    .order_by("artist_id", "release_date")
                    .values("artist_id", "image")
                ):
                    aid = row["artist_id"]
                    if aid not in seen_for_hero:
                        seen_for_hero.add(aid)
                        hero_image_map[aid] = row["image"]

            for artist in artists_missing_image_objects:
                hero_image = hero_image_map.get(artist.id)
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

    if context.get("is_artist_list", False):
        table_type = "artist"
    elif context.get("is_album_list", False):
        table_type = "album"
    else:
        table_type = "media"
    context["table_type"] = table_type
    if layout == "table":
        context["resolved_columns"] = resolve_columns(
            media_type,
            sort_filter,
            request.user,
            table_type,
        )
        context["column_config"] = resolve_column_config(
            media_type,
            sort_filter,
            request.user,
            table_type,
        )
        context["default_column_config"] = resolve_default_column_config(
            media_type,
            sort_filter,
            table_type,
        )
        if settings.DEBUG:
            prefs = (request.user.table_column_prefs or {}).get(media_type, {})
            pref_order = prefs.get("order", []) if isinstance(prefs, dict) else []
            pref_hidden = prefs.get("hidden", []) if isinstance(prefs, dict) else []
            resolved_keys = [column.key for column in context["resolved_columns"]]
            logger.info(
                (
                    "[COLUMN_DEBUG] media_list_resolved user=%s media_type=%s "
                    "table_type=%s sort=%s page=%s hx=%s pref_order=%s "
                    "pref_hidden=%s resolved_keys=%s"
                ),
                request.user.id,
                media_type,
                table_type,
                sort_filter,
                page,
                bool(request.headers.get("HX-Request")),
                pref_order,
                pref_hidden,
                resolved_keys,
            )

    # Handle HTMX requests for partial updates (boosted navigation still
    # sends HX-Request but needs the full page)
    if helpers.is_htmx_fragment(request):
        is_artist_list = context.get("is_artist_list", False)
        is_album_list = context.get("is_album_list", False)
        # Changing from empty list to a status with items
        if request.headers.get("HX-Target") == "empty_list":
            media_page = context.get("media_list")
            if media_page is not None and not media_page.object_list:
                return HttpResponse(status=204)
            response = HttpResponse()
            response["HX-Redirect"] = reverse("medialist", args=[media_type])
            return response

        # Check if this is a pagination request (has page parameter and is not the first page)
        is_pagination = request.GET.get("page") and int(request.GET.get("page", 1)) > 1
        context["is_pagination"] = bool(is_pagination)

        if layout == "grid":
            if is_artist_list:
                template_name = "app/components/artist_grid_items.html"
            elif is_album_list:
                template_name = "app/components/album_list_grid_items.html"
            else:
                template_name = "app/components/media_grid_items.html"
        else:
            template_name = "app/components/table_items.html"

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


@require_POST
def update_table_columns(request, media_type):
    """Persist table column order/visibility and trigger table refresh."""
    if not request.user.is_authenticated:
        return HttpResponseBadRequest("Authentication required")

    table_type = request.POST.get("table_type", "media")
    if table_type not in {"media", "artist", "album"}:
        table_type = "media"
    if media_type != MediaTypes.MUSIC.value:
        table_type = "media"

    raw_order = request.POST.get("order", "[]")
    raw_hidden = request.POST.get("hidden", "[]")

    previous_prefs = (request.user.table_column_prefs or {}).get(media_type, {})
    previous_order = previous_prefs.get("order", []) if isinstance(previous_prefs, dict) else []
    previous_hidden = previous_prefs.get("hidden", []) if isinstance(previous_prefs, dict) else []

    try:
        parsed_order = json.loads(raw_order)
    except json.JSONDecodeError:
        parsed_order = []
    try:
        parsed_hidden = json.loads(raw_hidden)
    except json.JSONDecodeError:
        parsed_hidden = []

    order = [value for value in parsed_order if isinstance(value, str)] if isinstance(parsed_order, list) else []
    hidden = [value for value in parsed_hidden if isinstance(value, str)] if isinstance(parsed_hidden, list) else []

    current_sort = request.POST.get("sort") or getattr(request.user, f"{media_type}_sort", MediaSortChoices.SCORE)
    if current_sort == "time_left" and media_type != MediaTypes.TV.value:
        current_sort = "title"
    elif current_sort == "runtime" and media_type not in {
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    }:
        current_sort = "title"
    elif current_sort == "time_to_beat" and media_type != MediaTypes.GAME.value:
        current_sort = "title"
    elif current_sort == "plays" and media_type not in {
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    }:
        current_sort = "title"
    elif current_sort == "time_watched" and media_type not in {
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    }:
        current_sort = "title"
    elif current_sort == "next_episode_air_date" and media_type not in {
        MediaTypes.TV.value,
        MediaTypes.SEASON.value,
        MediaTypes.ANIME.value,
    }:
        current_sort = "title"
    elif current_sort == "critic_rating" and media_type in {
        MediaTypes.MUSIC.value,
        MediaTypes.PODCAST.value,
    }:
        current_sort = "title"
    elif current_sort == "popularity" and media_type not in {
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    }:
        current_sort = "title"

    if settings.DEBUG:
        logger.info(
            (
                "[COLUMN_DEBUG] update_request user=%s media_type=%s table_type=%s "
                "sort=%s previous_order=%s previous_hidden=%s requested_order=%s "
                "requested_hidden=%s raw_order=%s raw_hidden=%s"
            ),
            request.user.id,
            media_type,
            table_type,
            current_sort,
            previous_order,
            previous_hidden,
            order,
            hidden,
            raw_order,
            raw_hidden,
        )

    clean_order, clean_hidden = sanitize_column_prefs(
        media_type=media_type,
        current_sort=current_sort,
        user=request.user,
        table_type=table_type,
        order=order,
        hidden=hidden,
    )

    request.user.update_column_prefs(
        media_type=media_type,
        table_type=table_type,
        order=clean_order,
        hidden=clean_hidden,
    )

    if settings.DEBUG:
        logger.info(
            (
                "[COLUMN_DEBUG] update_sanitized user=%s media_type=%s table_type=%s "
                "sanitized_order=%s sanitized_hidden=%s"
            ),
            request.user.id,
            media_type,
            table_type,
            clean_order,
            clean_hidden,
        )

        poll_results = []
        for attempt in range(1, 4):
            request.user.refresh_from_db(fields=["table_column_prefs"])
            polled_prefs = (request.user.table_column_prefs or {}).get(media_type, {})
            polled_order = polled_prefs.get("order", []) if isinstance(polled_prefs, dict) else []
            polled_hidden = polled_prefs.get("hidden", []) if isinstance(polled_prefs, dict) else []
            resolved_keys = [
                column.key
                for column in resolve_columns(
                    media_type,
                    current_sort,
                    request.user,
                    table_type,
                )
            ]
            poll_results.append(
                {
                    "attempt": attempt,
                    "order": polled_order,
                    "hidden": polled_hidden,
                    "resolved": resolved_keys,
                },
            )
            if attempt < 3:
                time.sleep(0.05)

        logger.info(
            "[COLUMN_DEBUG] update_poll user=%s media_type=%s table_type=%s polls=%s",
            request.user.id,
            media_type,
            table_type,
            poll_results,
        )

    response = HttpResponse(status=204)
    response["HX-Trigger"] = json.dumps({"refreshTableColumns": True})
    return response
