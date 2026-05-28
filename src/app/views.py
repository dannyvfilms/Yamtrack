import calendar
import json
import logging
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from datetime import UTC, date, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from uuid import uuid4

import requests
from dateutil.relativedelta import relativedelta
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required, login_required
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import EmptyPage, Paginator
from django.db import IntegrityError
from django.db.models import F, Max, Min, Q, prefetch_related_objects
from django.db.models.functions import ExtractDay, ExtractMonth, TruncDate
from django.db.utils import OperationalError
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import formats, timezone
from django.utils.dateparse import parse_date
from django.utils.text import slugify
from django.utils.timezone import datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from app import (
    cache_utils,
    credits,
    config,
    custom_metadata,
    discover,
    helpers,
    history_cache,
    history_processor,
    live_playback,
    metadata_utils,
    statistics_cache,
)
from app.db_retry import run_retryable_db_operation
from app.discover import tab_cache as discover_tab_cache
from app.columns import (
    resolve_column_config,
    resolve_columns,
    resolve_default_column_config,
    sanitize_column_prefs,
)
from app.collection_views import (
    _build_collection_episode_audit_entries,
    _build_collection_season_audit_entries,
    _collection_quality_labels_by_item_id,
    _collection_redirect,
    _collection_source_labels_by_item_id,
    _format_collection_progress,
    _format_collection_progress_value,
    _item_has_collection_source_state,
    _most_common_quality_label,
    _sonarr_episode_collection_entries,
    collection_add,
    collection_list,
    collection_modal,
    collection_remove,
    collection_remove_season,
    collection_status_api,
    collection_update,
)
from app.discover_views import (
    DISCOVER_ALLOWED_MEDIA_TYPES,
    DISCOVER_FAST_LOCAL_PLANNING_MEDIA_TYPES,
    DISCOVER_HIDDEN_SECTION,
    _apply_discover_response_headers,
    _build_track_modal_discover_tab_context,
    _coerce_discover_debug,
    _coerce_discover_media_type,
    _discover_candidate_seed,
    _discover_hidden_entries,
    _discover_media_options,
    _discover_model_for_media_type,
    _discover_planning_instance,
    _discover_response_rows,
    _discover_rows_context,
    _get_or_create_discover_item,
    _invalidate_discover_after_action,
    _mark_discover_stale_without_refresh,
    _render_discover_row_fragment,
    _render_discover_rows_fragment,
    _resolve_discover_media_type_for_user,
    discover_action,
    discover_page,
    discover_rows,
    discover_toggle_hidden,
    refresh_discover,
)
from app.history_views import (
    _build_anniversary_history_days,
    _build_release_history_days,
    _cached_history_entry_matches_filters,
    _can_use_cached_month_history,
    _filter_cached_history_days,
    _filter_history_by_enabled_media_types,
    delete_history_record,
    history,
    history_modal,
)
from app.music_views import (
    _build_music_album_activity_subtitle,
    _build_music_artist_activity_subtitle,
    _build_music_detail_secondary_actions,
    _music_activity_date_range,
    _music_album_detail_url,
    _music_artist_detail_url,
    _music_bulk_redirect_url,
    _render_music_album_details,
    _render_music_artist_details,
    _render_music_tracker_modal,
    album_detail,
    artist_delete,
    artist_detail,
    artist_save,
    artist_track_modal,
    create_album_from_search,
    create_artist_from_search,
    music_album_details,
    music_artist_details,
    prefetch_artist_covers,
    sync_artist_discography_view,
)
from app.people_views import person_detail, studio_detail
from app.podcast_views import (
    podcast_episodes_api,
    podcast_mark_all_played,
    podcast_save,
    podcast_show_delete,
    podcast_show_detail,
    podcast_show_save,
    podcast_show_track_modal,
)
from app.track_modal_views import (
    _DummyPodcastWrapper,
    _bulk_episode_form_initial_data,
    _episode_domain_template_payload,
    _render_podcast_show_track_modal,
    _render_standard_track_modal,
    _track_modal_field_groups,
    _track_modal_release_date_shortcut,
    _track_modal_release_runtime_minutes,
    track_modal,
)
from app.tag_views import (
    _build_detail_tag_sections,
    _detail_request_url,
    _parse_detail_tag_preview_genres,
    _render_tag_modal_response,
    _resolve_detail_tag_genres,
    _user_tags_for_item,
    tag_create,
    tag_delete,
    tag_item_toggle,
    tags_modal,
)
from app.metadata_sync_views import (
    _build_flat_anime_episode_preview,
    _build_local_tv_with_seasons_metadata,
    _build_missing_season_metadata,
    _get_local_show_item,
    _resolve_current_display_metadata_payload,
    _save_provider_metadata_status,
    migrate_grouped_anime,
    sync_metadata,
    update_item_image,
    update_manual_item_metadata,
    update_metadata_provider_preference,
)
from app.statistics_views import (
    STATISTICS_CARD_LAST_YEAR_LABELS,
    STATISTICS_COMPARE_LABELS,
    STATISTICS_COMPARE_LAST_YEAR,
    STATISTICS_COMPARE_NONE,
    STATISTICS_COMPARE_PREVIOUS_PERIOD,
    _STATISTICS_HOURS_DISPLAY_RE,
    _adjust_month_delta,
    _build_hours_per_media_type_comparison,
    _dates_close,
    _format_statistics_percent_change,
    _format_statistics_range_label,
    _format_statistics_total_for_media_type,
    _get_predefined_range_date_strings,
    _get_statistics_card_comparison_suffix,
    _get_statistics_card_range_label,
    _get_statistics_card_tooltip_labels,
    _get_statistics_minutes_by_type,
    _identify_predefined_range,
    _normalize_statistics_compare_mode,
    _parse_statistics_total_display_to_minutes,
    _resolve_statistics_comparison_range,
    _resolve_statistics_range_inputs,
    _statistics_day_boundary,
    refresh_statistics,
    statistics,
    update_top_talent_sort,
)

# history_cache is imported above
from app import (
    statistics as stats,
)
from app.forms import (
    BulkEpisodeTrackForm,
    CollectionEntryForm,
    EpisodeForm,
    ManualItemForm,
    get_form_class,
)
from app.log_safety import exception_summary, safe_url
from app.models import (
    TV,
    Album,
    Anime,
    Artist,
    BasicMedia,
    Book,
    CollectionEntry,
    Comic,
    CreditRoleType,
    DiscoverFeedback,
    DiscoverFeedbackType,
    Episode,
    Game,
    Item,
    MetadataProviderPreference,
    ItemPersonCredit,
    ItemTag,
    Manga,
    MediaTypes,
    ProviderMetadataStatus,
    Movie,
    Music,
    Person,
    PodcastShow,
    Season,
    Sources,
    Status,
    Tag,
    Studio,
    Track,
)
from app.providers import (
    comicvine,
    hardcover,
    igdb,
    mangaupdates,
    manual,
    openlibrary,
    services,
    tmdb,
)
from app.services import game_lengths as game_length_services
from app.services import (
    anime_migration,
    bulk_episode_tracking,
    bulk_music_tracking,
    metadata_resolution,
)
from app.services import music as sync_services
from app.services import trakt_popularity as trakt_popularity_service
from app.services.tracking_hydration import (
    ensure_item_metadata,
    ensure_item_metadata_from_discover_seed,
)
from app.save_views import (
    episode_bulk_save,
    episode_save,
    media_delete,
    media_save,
)
from app.score_views import (
    _collect_music_history_day_keys_for_album_ids,
    _collect_music_history_day_keys_for_artist,
    update_album_score,
    update_artist_score,
    update_episode_score,
    update_media_score,
)
from app.activity_builders import (
    DETAIL_EPISODES_PER_PAGE,
    _annotate_home_card_images,
    _build_detail_activity_state,
    _build_detail_activity_subtitle,
    _detail_episode_number_for_pagination,
    _detail_episode_page_label,
    _format_detail_activity_duration,
    _get_game_lengths_refresh_lock,
    _normalize_detail_episode_actions,
    _paginate_detail_episodes,
    _queue_game_lengths_refresh,
    _should_queue_game_lengths_refresh,
)
from app.detail_builders import (
    _apply_cached_hltb_link,
    _build_detail_link_entry,
    _build_detail_link_sections,
    _build_game_length_card,
    _build_game_lengths_context,
    _build_trakt_popularity_context,
    _format_game_length_minutes,
    _format_game_length_seconds,
    _normalize_detail_link_brand_key,
)
from app.templatetags import app_tags
from app.signals import suppress_media_cache_change_signals
from app.tv_sort import _sort_tv_media_by_time_left
from integrations import anime_mapping
from integrations.models import CollectionSourceState
from lists.models import CustomList
from users.home_screen import build_home_page_groups
from users.models import HomeSortChoices, MediaSortChoices, MediaStatusChoices
from users.models import TopTalentSortChoices

logger = logging.getLogger(__name__)


LOCAL_ONLY_MISSING_SEASON_BANNER = (
    "Season metadata is missing from the provider. "
    "This page is built from local activity and the linked show may be mismatched."
)
DETAIL_SECONDARY_FRAGMENT = "secondary"

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



def _get_tv_runtime_display_fallback(detail_item, media_metadata):
    """Return a best-effort runtime string for TV details when provider runtime is missing."""
    if not detail_item or detail_item.media_type != MediaTypes.TV.value:
        return None

    runtime_minutes = getattr(detail_item, "runtime_minutes", None)
    if runtime_minutes and runtime_minutes < 999998:
        return tmdb.get_readable_duration(runtime_minutes)

    if detail_item.runtime:
        parsed_runtime = stats.parse_runtime_to_minutes(detail_item.runtime)
        if parsed_runtime and parsed_runtime > 0:
            return tmdb.get_readable_duration(parsed_runtime)

    episode_runtimes = list(
        Item.objects.filter(
            media_id=detail_item.media_id,
            source=detail_item.source,
            media_type=MediaTypes.EPISODE.value,
            runtime_minutes__isnull=False,
        ).exclude(
            runtime_minutes__in=[999998, 999999],
        ).values_list("runtime_minutes", flat=True),
    )
    if episode_runtimes:
        return tmdb.get_readable_duration(round(sum(episode_runtimes) / len(episode_runtimes)))

    details = media_metadata.get("details") if isinstance(media_metadata, dict) else {}
    if not isinstance(details, dict):
        details = {}

    max_seasons = details.get("seasons")
    try:
        max_seasons = int(max_seasons)
    except (TypeError, ValueError):
        max_seasons = 5
    max_seasons = max(1, min(max_seasons, 20))

    for season_num in range(1, max_seasons + 1):
        cached_season_data = cache.get(f"tmdb_season_{detail_item.media_id}_{season_num}")
        runtime_str = ((cached_season_data or {}).get("details") or {}).get("runtime")
        runtime_minutes = stats.parse_runtime_to_minutes(runtime_str)
        if runtime_minutes and runtime_minutes > 0:
            return tmdb.get_readable_duration(runtime_minutes)

    return None


def _mark_grouped_anime_route(media_items):
    """Annotate grouped-anime rows so templates route them through the Anime UI."""
    for media in media_items or []:
        setattr(media, "route_media_type", MediaTypes.ANIME.value)
        item = getattr(media, "item", None)
        if item is not None:
            setattr(item, "route_media_type", MediaTypes.ANIME.value)
    return media_items

def home(request):
    """Home page with media items in progress."""
    try:
        items_limit = 14
        try:
            load_row_id = int(request.GET.get("load_row", ""))
        except (TypeError, ValueError):
            load_row_id = None
        try:
            load_row_offset = max(int(request.GET.get("offset", "0")), 0)
        except (TypeError, ValueError):
            load_row_offset = 0

        home_groups = build_home_page_groups(
            request.user,
            items_limit,
            load_row_id=load_row_id,
            load_row_offset=load_row_offset,
            append_only=bool(request.headers.get("HX-Request") and load_row_id),
        )

        if request.headers.get("HX-Request") and load_row_id:
            target_row = next(
                (
                    row
                    for group in home_groups
                    for row in group["rows"]
                    if row["row_id"] == load_row_id
                ),
                None,
            )
            if target_row is None:
                return HttpResponse("")
            return render(
                request,
                "app/components/home_grid.html",
                {
                    "media_list": target_row,
                    "user": request.user,
                    "MediaTypes": MediaTypes,
                    "IMG_NONE": settings.IMG_NONE,
                },
            )

        context = {
            "user": request.user,
            "home_groups": home_groups,
            "items_limit": items_limit,
            "active_playback_card": live_playback.build_home_playback_card(request.user),
            "MediaTypes": MediaTypes,
            "IMG_NONE": settings.IMG_NONE,
        }
        return render(request, "app/home.html", context)
    except OperationalError as error:
        logger.error("Database error in home view: %s", error, exc_info=True)
        # Return empty state on database error
        context = {
            "user": request.user,
            "home_groups": [],
            "items_limit": 14,
            "database_error": True,
            "active_playback_card": None,
            "MediaTypes": MediaTypes,
            "IMG_NONE": settings.IMG_NONE,
        }
        return render(request, "app/home.html", context)


def active_playback_fragment(request):
    """HTMX fragment: return the active playback card or empty response."""
    card = live_playback.build_home_playback_card(request.user)
    if not card:
        return HttpResponse("")
    return render(request, "app/components/active_playback_card.html", {
        "active_playback_card": card,
    })


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
    sorted_media_sort_choices = sorted(
        MediaSortChoices.choices,
        key=lambda choice: str(choice[1]).lower(),
    )
    author_media_types = (
        MediaTypes.BOOK.value,
        MediaTypes.MANGA.value,
        MediaTypes.COMIC.value,
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
        f"{media_type}_layout",
        request.GET.get("layout"),
    )
    sort_filter = request.user.update_preference(
        f"{media_type}_sort",
        request.GET.get("sort"),
    )
    direction_param = request.GET.get("direction")
    direction_field = f"{media_type}_direction"

    # Enforce media-type-specific sort options.
    if sort_filter == "time_left" and media_type != MediaTypes.TV.value:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "runtime" and media_type not in runtime_media_types:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "time_to_beat" and media_type != MediaTypes.GAME.value:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "plays" and media_type not in plays_media_types:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "time_watched" and media_type not in runtime_media_types:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "next_episode_air_date" and media_type not in next_episode_air_date_media_types:
        sort_filter = "title"  # Default fallback
        request.user.update_preference(f"{media_type}_sort", "title")
        direction_param = None
    elif sort_filter == "author" and media_type not in author_media_types:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None
    elif sort_filter == "critic_rating" and media_type not in critic_rating_media_types:
        sort_filter = "title"
        request.user.update_preference(f"{media_type}_sort", "title")
        direction_param = None
    elif sort_filter == "popularity" and media_type not in popularity_media_types:
        sort_filter = "title"
        request.user.update_preference(f"{media_type}_sort", "title")
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
    supports_untracked_status_filter = media_type not in {
        MediaTypes.MUSIC.value,
        MediaTypes.PODCAST.value,
    }
    raw_status_filter = request.GET.get("status")
    valid_statuses = {choice[0] for choice in MediaStatusChoices.choices}
    persisted_status_filter = getattr(request.user, f"{media_type}_status", MediaStatusChoices.ALL)

    if raw_status_filter in valid_statuses:
        status_filter = request.user.update_preference(
            f"{media_type}_status",
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

    def _normalize_filter_value(value):
        return str(value or "").strip().lower()

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

    collection_formats_by_item_id = defaultdict(set)
    collection_platforms_by_item_id = defaultdict(set)

    def _extract_item_formats(item):
        """Extract normalized format values from Item and collection metadata."""
        formats = set()
        if item and hasattr(item, "format") and item.format:
            normalized_item_format = _normalize_filter_value(item.format)
            if normalized_item_format:
                formats.add(normalized_item_format)

        if item:
            formats.update(collection_formats_by_item_id.get(item.id, set()))

        return formats

    def _extract_item_platforms_with_collection(item):
        """Extract platform values, preferring explicit collection platform entries."""
        if not item:
            return []

        explicit_platforms = collection_platforms_by_item_id.get(item.id, set())
        if explicit_platforms:
            return sorted(explicit_platforms, key=lambda value: value.lower())

        return _extract_item_platforms(item)

    def apply_format_filter(media_items, filter_value):
        if not filter_value:
            return media_items
        target = _normalize_filter_value(filter_value)
        filtered_items = []
        for media in media_items:
            item = getattr(media, "item", None)
            if not item:
                continue
            item_formats = _extract_item_formats(item)
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

    FORMAT_LABELS = {
        "hardcover": "Hardcover",
        "paperback": "Paperback",
        "ebook": "eBook",
        "audiobook": "Audiobook",
    }

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

    def build_filter_data_from_items(media_items):
        from app.models import Sources

        genres_set = set()
        years_set = set()
        sources_set = set()
        languages_set = set()
        countries_set = set()
        platforms_set = set()
        formats_set = set()
        authors_set = set()
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
            db_languages = _extract_item_languages(item)
            if db_languages:
                languages_set.update(db_languages)
            country_value = _extract_item_country(item)
            if country_value:
                countries_set.add(country_value)
            platforms = _extract_item_platforms_with_collection(item)
            if platforms:
                platforms_set.update(platforms)
            authors = _extract_item_authors(item)
            if authors:
                authors_set.update(authors)
            item_formats = _extract_item_formats(item)
            if item_formats:
                formats_set.update(item_formats)

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

    # Get media list with filters applied
    query_sort_filter = (
        "title"
        if sort_filter in {"author", "runtime", "time_to_beat", "time_watched"}
        else sort_filter
    )

    list_sql_filters = {
        "genre": genre_filter,
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
    if media_type == MediaTypes.TV.value and not include_grouped_anime_in_tv:
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
    if status_filter in {MediaStatusChoices.ALL, MEDIA_LIST_NO_STATUS}:
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
        if status_filter in {MediaStatusChoices.ALL, MEDIA_LIST_NO_STATUS}:
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
    filter_data = build_filter_data_from_items(filter_data_source_items)
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
        media_list = sort_media_items_by_runtime(media_list, direction)
    if sort_filter == "plays" and media_type in plays_media_types:
        media_list = sort_media_items_by_plays(media_list, direction)
    if sort_filter == "time_watched" and media_type in runtime_media_types:
        BasicMedia.objects.annotate_max_progress(
            _tracked_media_entries(media_list),
            media_type,
        )
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

            reverse = direction == "desc"
            media_list = sorted(media_list, key=_mixed_sort_key, reverse=reverse)

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

    if media_type in author_media_types:
        annotate_media_authors(media_page.object_list)

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
        "current_progress": progress_filter,
        "current_genre": genre_filter,
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
        "supports_critic_rating_sort": media_type in critic_rating_media_types,
    }

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
            "supports_critic_rating_sort": False,
        }

    if media_type == MediaTypes.MUSIC.value:
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

    table_type = "artist" if context.get("is_artist_list", False) else "media"
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

    # Handle HTMX requests for partial updates
    if request.headers.get("HX-Request"):
        is_artist_list = context.get("is_artist_list", False)
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
            template_name = (
                "app/components/artist_grid_items.html"
                if is_artist_list
                else "app/components/media_grid_items.html"
            )
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
    if table_type not in {"media", "artist"}:
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

    def _norm(text):
        return str(text or "").strip().casefold()

    def _title_fields(item_obj):
        if isinstance(item_obj, dict):
            return (
                item_obj.get("title"),
                item_obj.get("original_title"),
                item_obj.get("localized_title"),
            )
        return (
            getattr(item_obj, "title", None),
            getattr(item_obj, "original_title", None),
            getattr(item_obj, "localized_title", None),
        )

    def _display_title_for_user(item_obj):
        if hasattr(item_obj, "get_display_title"):
            return item_obj.get_display_title(user=request.user)

        title, original_title, localized_title = _title_fields(item_obj)
        title = str(title or "").strip()
        original_title = str(original_title or "").strip() or None
        localized_title = str(localized_title or "").strip() or None

        if not localized_title and title:
            localized_title = title

        preference = getattr(request.user, "title_display_preference", "localized")
        if preference == "original":
            return original_title or localized_title or title
        return localized_title or original_title or title

    def _matched_title(item_obj, search_query):
        normalized_query = _norm(search_query)
        if not normalized_query:
            return None

        display_title = _display_title_for_user(item_obj)
        display_norm = _norm(display_title)

        title, original_title, localized_title = _title_fields(item_obj)
        candidates = []
        for candidate in (title, localized_title, original_title):
            text = str(candidate or "").strip()
            if text and text not in candidates:
                candidates.append(text)

        # Prefer exact, then prefix, then contains.
        for predicate in (
            lambda value: _norm(value) == normalized_query,
            lambda value: _norm(value).startswith(normalized_query),
            lambda value: normalized_query in _norm(value),
        ):
            for candidate in candidates:
                if _norm(candidate) == display_norm:
                    continue
                if predicate(candidate):
                    return candidate
        return None

    local_results = []
    local_results_total = 0
    local_results_limit = 24
    local_results_kind = "media"
    local_music_artists = []
    local_music_artists_total = 0
    local_music_albums = []
    local_music_albums_total = 0
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
                local_results = [
                    {
                        "item": media.item,
                        "media": media,
                        "matched_title": _matched_title(media.item, query),
                    }
                    for media in adapted_media
                ]
            elif media_type == MediaTypes.MUSIC.value:
                from django.db.models import Q

                from app.models import AlbumTracker, ArtistTracker

                artist_trackers = (
                    ArtistTracker.objects.filter(user=request.user)
                    .exclude(artist__name__isnull=True)
                    .exclude(artist__name__exact="")
                    .filter(artist__name__icontains=query)
                    .select_related("artist")
                )
                local_music_artists_total = artist_trackers.count()
                local_music_artists = list(artist_trackers.order_by("artist__name")[:local_results_limit])

                album_trackers = (
                    AlbumTracker.objects.filter(user=request.user)
                    .exclude(album__title__isnull=True)
                    .exclude(album__title__exact="")
                    .filter(
                        Q(album__title__icontains=query)
                        | Q(album__artist__name__icontains=query),
                    )
                    .select_related("album", "album__artist")
                )
                local_music_albums_total = album_trackers.count()
                local_music_albums = list(album_trackers.order_by("album__title")[:local_results_limit])

                local_results_total = local_music_artists_total + local_music_albums_total
                local_results_kind = "music"
            else:
                local_queryset = BasicMedia.objects.get_media_list(
                    request.user,
                    media_type,
                    MediaStatusChoices.ALL,
                    "title",
                    search=query,
                    direction="asc",
                )
                local_media = list(local_queryset)
                if media_type == MediaTypes.TV.value and getattr(
                    request.user,
                    "anime_library_mode",
                    MediaTypes.ANIME.value,
                ) == MediaTypes.ANIME.value:
                    local_media = [
                        media
                        for media in local_media
                        if getattr(getattr(media, "item", None), "library_media_type", None)
                        != MediaTypes.ANIME.value
                    ]
                elif media_type == MediaTypes.ANIME.value and getattr(
                    request.user,
                    "anime_library_mode",
                    MediaTypes.ANIME.value,
                ) in {MediaTypes.ANIME.value, "both"}:
                    grouped_local_media = list(
                        BasicMedia.objects.get_media_list(
                            request.user,
                            MediaTypes.TV.value,
                            MediaStatusChoices.ALL,
                            "title",
                            search=query,
                            direction="asc",
                        ),
                    )
                    grouped_local_media = [
                        media
                        for media in grouped_local_media
                        if getattr(getattr(media, "item", None), "library_media_type", None)
                        == MediaTypes.ANIME.value
                    ]
                    _mark_grouped_anime_route(grouped_local_media)
                    local_media.extend(grouped_local_media)
                    local_media.sort(
                        key=lambda media: getattr(
                            getattr(media, "item", None),
                            "title",
                            "",
                        ).lower(),
                    )

                local_results_total = len(local_media)
                local_media = local_media[:local_results_limit]
                BasicMedia.objects.annotate_max_progress(local_media, media_type)
                local_results = [
                    {
                        "item": media.item,
                        "media": media,
                        "matched_title": _matched_title(media.item, query),
                    }
                    for media in local_media
                ]
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Local search failed: %s", exception_summary(exc))

    source_options = metadata_resolution.available_metadata_sources(media_type)
    default_source = metadata_resolution.metadata_default_source(
        request.user,
        media_type,
    )
    # only receives source when searching with secondary source
    source = request.GET.get("source", default_source)
    if source not in {option.value for option in source_options} and source_options:
        source = source_options[0].value

    search_page = 1 if media_type == MediaTypes.MUSIC.value else page
    data = services.search(media_type, query, search_page, source)

    if media_type == MediaTypes.MUSIC.value:
        context = {
            "user": request.user,
            "data": data,
            "music_online_artists": data.get("artists", []),
            "music_online_releases": data.get("releases", []),
            "source": source,
            "source_options": source_options,
            "media_type": media_type,
            "layout": layout,
            "local_results": local_results,
            "local_results_total": local_results_total,
            "local_results_limit": local_results_limit,
            "local_results_kind": local_results_kind,
            "local_music_artists": local_music_artists,
            "local_music_artists_total": local_music_artists_total,
            "local_music_albums": local_music_albums,
            "local_music_albums_total": local_music_albums_total,
        }
        return render(request, "app/search.html", context)

    # Enrich search results with user tracking data
    if data.get("results"):
        data["results"] = helpers.enrich_items_with_user_data(
            request,
            data["results"],
            section_name="search",
        )
        for result in data["results"]:
            result["matched_title"] = _matched_title(result.get("item"), query)

    context = {
        "user": request.user,
        "data": data,
        "source": source,
        "source_options": source_options,
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
    detail_view_started_at = time.perf_counter()
    render_secondary_only = (
        request.GET.get("fragment") == DETAIL_SECONDARY_FRAGMENT
        and media_type != MediaTypes.PODCAST.value
    )
    defer_detail_secondary = (
        not render_secondary_only and media_type != MediaTypes.PODCAST.value
    )
    detail_return_url = _detail_request_url(request)
    detail_secondary_fragment_url = _detail_request_url(
        request,
        fragment=DETAIL_SECONDARY_FRAGMENT,
    )

    # Treat all anonymous views as public (no user-specific data/actions)
    is_anonymous = not request.user.is_authenticated
    public_view = is_anonymous
    public_list_view = request.GET.get("public_view") == "1" and is_anonymous

    # For public views, find a public list containing this item to get the owner
    list_owner = None
    if public_list_view:
        try:
            item = Item.objects.filter(
                media_id=media_id,
                source=source,
                media_type=media_type,
            ).first()
            if item:
                public_list = CustomList.objects.filter(
                    visibility="public",
                    items=item,
                ).select_related("owner").first()
                if public_list:
                    list_owner = public_list.owner
        except Exception:
            # If we can't find a list owner, list_owner stays None
            pass

    detail_persistence_deferred = False
    detail_db_max_retries = 0

    def _mark_detail_persistence_deferred(_error=None):
        nonlocal detail_persistence_deferred
        detail_persistence_deferred = True

    def _best_effort_detail_db_work(operation, *, fallback=None, operation_name):
        return run_retryable_db_operation(
            operation,
            mode="best_effort",
            fallback=fallback,
            operation_name=operation_name,
            operation_logger=logger,
            max_retries=detail_db_max_retries,
            on_deferred=_mark_detail_persistence_deferred,
        )

    def _best_effort_detail_followup(
        operation,
        *,
        operation_name,
        fallback=False,
    ):
        try:
            return operation()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Skipping detail follow-up %s for %s due to error",
                operation_name,
                request.path,
                exc_info=True,
            )
            _mark_detail_persistence_deferred()
            return fallback

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
                                    logger.debug(
                                        "Failed to fetch show metadata from RSS: %s",
                                        exception_summary(e),
                                    )

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
                                import hashlib

                                episodes_data = podcast_rss.fetch_episodes_from_rss(rss_feed_url, limit=None)
                                seen_uuids = set()

                                for episode_data in episodes_data:
                                    episode_uuid = episode_data.get("guid")
                                    if not episode_uuid:
                                        uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                                        episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                                    if episode_uuid in seen_uuids:
                                        continue

                                    # Check for existing match within this show by title + date
                                    exists = False
                                    if episode_data.get("title") and episode_data.get("published"):
                                        exists = PodcastEpisode.objects.filter(
                                            show=show,
                                            title__iexact=episode_data["title"].strip(),
                                            published__date=episode_data["published"].date(),
                                        ).exists()

                                    if not exists:
                                        try:
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
                                            seen_uuids.add(episode_uuid)
                                        except Exception:
                                            logger.debug("Skipping duplicate episode UUID %s for show %s", episode_uuid, show.title)
                            except Exception as e:
                                logger.warning(
                                    "Failed to fetch episodes from RSS feed for %s: %s",
                                    show.title,
                                    exception_summary(e),
                                )

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
                    logger.error(
                        "Failed to enrich podcast from iTunes metadata: %s",
                        exception_summary(e),
                        exc_info=True,
                    )
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
                        episode_uuid = episode_data.get("guid")
                        if not episode_uuid:
                            uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                            episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                        if episode_uuid in existing_uuids:
                            continue

                        # Check for a match within this show by title + date
                        episode = None
                        if episode_data.get("title") and episode_data.get("published"):
                            episode = PodcastEpisode.objects.filter(
                                show=show,
                                title__iexact=episode_data["title"].strip(),
                                published__date=episode_data["published"].date(),
                            ).first()

                        if not episode:
                            try:
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
                            except Exception:
                                logger.debug("Skipping duplicate episode UUID %s for show %s", episode_uuid, show.title)

                    if new_episodes_count > 0:
                        logger.info("Fetched %d additional episodes for show %s (ID: %d)", new_episodes_count, show.title, show.id)
                except Exception as e:
                    logger.warning(
                        "Failed to refresh episode list from RSS feed for show %s: %s",
                        show.title,
                        exception_summary(e),
                    )

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
            else:
                user_podcasts = []
                total_listened = 0
                total_minutes = 0

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
                            in_progress_entry = next(
                                (entry for entry in podcasts if not entry.end_date),
                                None,
                            )
                            self.in_progress_instance_id = (
                                in_progress_entry.id if in_progress_entry else None
                            )

                        @property
                        def completed_play_count(self):
                            """Return count of completed plays (history records with end_date)."""
                            # Since we already filtered all_history to only include records with end_date,
                            # we can just count the length of the filtered history_list
                            return len(self._history_list)

                        @property
                        def has_in_progress_entry(self):
                            return bool(self.in_progress_instance_id)

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
                    podcast_wrapper = _DummyPodcastWrapper(enriched["item"])

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
            media_metadata.setdefault("source_url", None)
            media_metadata.setdefault("tracking_source_url", None)
            media_metadata.setdefault("display_source_url", None)

            # For pagination, calculate if there are more episodes
            total_episodes_count = episodes.count()
            has_more = total_episodes_count > initial_limit
            next_page = 2 if has_more else None
            media_metadata["max_progress"] = total_episodes_count

            podcast_play_stats = None
            activity_subtitle = None
            if not public_view and user_podcasts:
                range_start_candidates = []
                range_end_candidates = []
                completed_entries = 0
                total_progress_seconds = 0

                for entry in user_podcasts:
                    range_start = entry.start_date or entry.end_date or entry.created_at
                    range_end = entry.end_date or entry.start_date or entry.created_at
                    if range_start:
                        range_start_candidates.append(range_start)
                    if range_end:
                        range_end_candidates.append(range_end)
                    if entry.end_date or entry.status == Status.COMPLETED.value:
                        completed_entries += 1
                    total_progress_seconds += int(entry.progress or 0)

                total_listened_minutes = total_progress_seconds // 60
                podcast_play_stats = {
                    "first_played": min(range_start_candidates) if range_start_candidates else None,
                    "last_played": max(range_end_candidates) if range_end_candidates else None,
                    "total_minutes": total_listened_minutes,
                    "total_hours": total_listened_minutes // 60,
                    "total_minutes_remainder": total_listened_minutes % 60,
                    "total_plays": completed_entries or total_listened,
                }
                activity_subtitle = _build_detail_activity_subtitle(
                    MediaTypes.PODCAST.value,
                    media_metadata,
                    tracker,
                    podcast_play_stats,
                )

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
                "play_stats": podcast_play_stats,
                "activity_subtitle": activity_subtitle,
                "IMG_NONE": settings.IMG_NONE,
                "TRACK_TIME": True,
                "has_more_episodes": has_more,  # Keep for backward compatibility
                "has_more": has_more,  # For fragment compatibility
                "next_page": next_page,
                "show_id": show.id,  # For API endpoint
            }
            return render(request, "app/media_details.html", context)

    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
    )
    detail_item_lookup = {
        "media_id": media_id,
        "source": source,
        "media_type": tracking_media_type,
    }
    if metadata_resolution.is_grouped_anime_route(media_type, source=source):
        detail_item_lookup["library_media_type"] = MediaTypes.ANIME.value

    media_metadata = services.get_media_metadata(media_type, media_id, source)
    if isinstance(media_metadata, dict):
        media_metadata.update(Item.title_fields_from_metadata(media_metadata))

    detail_item = Item.objects.filter(**detail_item_lookup).first()

    if (
        render_secondary_only
        and detail_item is None
        and source == Sources.IGDB.value
        and media_type == MediaTypes.GAME.value
        and isinstance(media_metadata, dict)
    ):
        detail_item_outcome = _best_effort_detail_db_work(
            lambda: Item.objects.get_or_create(
                media_id=media_id,
                source=source,
                media_type=media_type,
                defaults={
                    **Item.title_fields_from_metadata(media_metadata),
                    "image": media_metadata.get("image") or settings.IMG_NONE,
                },
            ),
            fallback=lambda: (None, False),
            operation_name="IGDB detail item create",
        )
        detail_item, _ = detail_item_outcome.value

    # When the user prefers original titles, aggressively refresh stale TMDB cache
    # if we don't yet have an original title. This lets details-page opens backfill
    # better title variants that can then propagate across the UI.
    tmdb_detail_cache_key = f"{Sources.TMDB.value}_{tracking_media_type}_{media_id}"
    should_refresh_tmdb_titles = (
        request.user.is_authenticated
        and source == Sources.TMDB.value
        and tracking_media_type in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
        )
        and getattr(request.user, "title_display_preference", "localized") == "original"
        and isinstance(media_metadata, dict)
        and not media_metadata.get("original_title")
    )
    if render_secondary_only and should_refresh_tmdb_titles:
        cache.delete(tmdb_detail_cache_key)
        media_metadata = services.get_media_metadata(media_type, media_id, source)
        if isinstance(media_metadata, dict):
            media_metadata.update(Item.title_fields_from_metadata(media_metadata))

    should_refresh_tmdb_tv_credits = (
        source == Sources.TMDB.value
        and tracking_media_type in (MediaTypes.TV.value, MediaTypes.SEASON.value)
        and isinstance(media_metadata, dict)
        and not media_metadata.get("cast")
        and not media_metadata.get("crew")
    )
    if render_secondary_only and should_refresh_tmdb_tv_credits:
        cache.delete(tmdb_detail_cache_key)
        media_metadata = services.get_media_metadata(media_type, media_id, source)
        if isinstance(media_metadata, dict):
            media_metadata.update(Item.title_fields_from_metadata(media_metadata))

    identity_media_metadata = media_metadata

    if render_secondary_only and detail_item and isinstance(media_metadata, dict):
        title_fields = Item.title_fields_from_metadata(
            media_metadata,
            fallback_title=detail_item.title,
        )
        update_fields = []
        normalized_existing_titles = {
            "title": Item._normalize_title_value(detail_item.title),
            "original_title": Item._normalize_title_value(detail_item.original_title),
            "localized_title": Item._normalize_title_value(detail_item.localized_title),
        }
        for field_name, normalized_value in normalized_existing_titles.items():
            if normalized_value and getattr(detail_item, field_name) != normalized_value:
                setattr(detail_item, field_name, normalized_value)
                update_fields.append(field_name)

        for field_name in ("title", "original_title", "localized_title"):
            desired_value = title_fields.get(field_name)
            if desired_value and getattr(detail_item, field_name) != desired_value:
                setattr(detail_item, field_name, desired_value)
                update_fields.append(field_name)
        if update_fields:
            _best_effort_detail_db_work(
                lambda: detail_item.save(update_fields=update_fields),
                operation_name="detail item title sync",
            )

    # Persist series info for books if available
    if (
        render_secondary_only
        and media_type == MediaTypes.BOOK.value
        and isinstance(media_metadata, dict)
    ):
        try:
            item = Item.objects.get(
                media_id=media_id,
                source=source,
                media_type=media_type,
            )
            update_fields = []
            if media_metadata.get("series_name") and item.series_name != media_metadata["series_name"]:
                item.series_name = media_metadata["series_name"]
                update_fields.append("series_name")
            if media_metadata.get("series_position") is not None and item.series_position != media_metadata["series_position"]:
                item.series_position = media_metadata["series_position"]
                update_fields.append("series_position")
            
            if update_fields:
                _best_effort_detail_db_work(
                    lambda: item.save(update_fields=update_fields),
                    operation_name="detail book-series sync",
                )
        except Item.DoesNotExist:
            pass

    igdb_game_studios_missing = (
        source == Sources.IGDB.value
        and tracking_media_type == MediaTypes.GAME.value
        and isinstance(media_metadata, dict)
        and "studios_full" not in media_metadata
    )

    if isinstance(media_metadata, dict):
        media_metadata.setdefault("cast", [])
        media_metadata.setdefault("crew", [])
        media_metadata.setdefault("studios_full", [])

    metadata_resolution_result = None
    should_resolve_metadata = bool(
        detail_item
        and isinstance(media_metadata, dict)
        and (
            media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value)
            or custom_metadata.supports_custom_provider(media_type)
        )
    )
    if render_secondary_only and should_resolve_metadata:
        metadata_resolution_result = metadata_resolution.resolve_detail_metadata(
            request.user if request.user.is_authenticated else None,
            item=detail_item,
            route_media_type=media_type,
            media_id=media_id,
            source=source,
            base_metadata=media_metadata,
            persistence_mode="best_effort",
            retry_max_retries=detail_db_max_retries,
            on_persistence_deferred=_mark_detail_persistence_deferred,
        )
        media_metadata = metadata_resolution_result.header_metadata
        media_metadata.update(
            Item.title_fields_from_metadata(
                media_metadata,
                fallback_title=detail_item.title if detail_item else "",
            ),
        )

    # For podcasts, ensure source is in metadata dict (fixes KeyError in template)
    if media_type == MediaTypes.PODCAST.value and isinstance(media_metadata, dict):
        media_metadata["source"] = source
        media_metadata["media_type"] = media_type
        media_metadata["media_id"] = media_id

    if (
        render_secondary_only
        and source == Sources.TMDB.value
        and tracking_media_type in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
        )
        and isinstance(media_metadata, dict)
    ):
        if detail_item:
            metadata_update_fields = metadata_utils.apply_item_metadata(
                detail_item,
                identity_media_metadata,
            )
            if metadata_update_fields:
                detail_item.metadata_fetched_at = timezone.now()
                metadata_update_fields.append("metadata_fetched_at")
                _best_effort_detail_db_work(
                    lambda: detail_item.save(update_fields=metadata_update_fields),
                    operation_name="TMDB detail metadata sync",
                )
            missing_people = not detail_item.person_credits.exists()
            missing_studios = not detail_item.studio_credits.exists()
            if missing_people or missing_studios:
                _best_effort_detail_db_work(
                    lambda: credits.sync_item_credits_from_metadata(
                        detail_item,
                        media_metadata,
                    ),
                    operation_name="TMDB detail credits sync",
                )

    should_refresh_igdb_game_studios = (
        source == Sources.IGDB.value
        and tracking_media_type == MediaTypes.GAME.value
        and detail_item is not None
        and igdb_game_studios_missing
    )
    if render_secondary_only and should_refresh_igdb_game_studios:
        cache.delete(f"{source}_{tracking_media_type}_{media_id}")
        media_metadata = services.get_media_metadata(media_type, media_id, source)
        if isinstance(media_metadata, dict):
            media_metadata.update(Item.title_fields_from_metadata(media_metadata))

    if (
        render_secondary_only
        and detail_item
        and source == Sources.IGDB.value
        and tracking_media_type == MediaTypes.GAME.value
        and isinstance(media_metadata, dict)
        and media_metadata.get("studios_full")
    ):
        existing_studio_ids = {
            str(studio_credit.studio.source_studio_id)
            for studio_credit in detail_item.studio_credits.select_related("studio")
            if studio_credit.studio and studio_credit.studio.source_studio_id is not None
        }
        incoming_studio_ids = {
            str(studio.get("studio_id") or studio.get("id"))
            for studio in media_metadata.get("studios_full", [])
            if isinstance(studio, dict) and (studio.get("studio_id") or studio.get("id"))
        }
        if existing_studio_ids != incoming_studio_ids:
            _best_effort_detail_db_work(
                lambda: credits.sync_item_credits_from_metadata(
                    detail_item,
                    {
                        "studios_full": media_metadata.get("studios_full", []),
                    },
                ),
                operation_name="IGDB detail studio sync",
            )

    identity_media_metadata = media_metadata

    if (
        render_secondary_only
        and source == Sources.IGDB.value
        and tracking_media_type == MediaTypes.GAME.value
        and detail_item
        and isinstance(media_metadata, dict)
    ):
        metadata_update_fields = metadata_utils.apply_item_metadata(
            detail_item,
            identity_media_metadata,
        )
        if metadata_update_fields:
            detail_item.metadata_fetched_at = timezone.now()
            metadata_update_fields.append("metadata_fetched_at")
            _best_effort_detail_db_work(
                lambda: detail_item.save(update_fields=metadata_update_fields),
                operation_name="IGDB detail metadata sync",
            )

    _apply_cached_hltb_link(media_metadata, detail_item)

    game_lengths = (
        _build_game_lengths_context(detail_item)
        if source == Sources.IGDB.value and media_type == MediaTypes.GAME.value
        else None
    )
    if (
        game_lengths
        and game_lengths.get("source") == "igdb"
        and isinstance(media_metadata, dict)
        and media_metadata.get("source_url")
    ):
        game_lengths["source_url"] = media_metadata["source_url"]
    game_lengths_fetch_queued = False
    game_lengths_refresh_pending = False
    if render_secondary_only and _should_queue_game_lengths_refresh(detail_item):
        game_lengths_refresh_pending = (
            _get_game_lengths_refresh_lock(
                detail_item,
                force=False,
                fetch_hltb=True,
            )
            is not None
        )
        if not game_lengths_refresh_pending:
            game_lengths_fetch_queued = _best_effort_detail_followup(
                lambda: _queue_game_lengths_refresh(
                    detail_item,
                    force=False,
                    fetch_hltb=True,
                ),
                operation_name="game lengths refresh enqueue",
                fallback=False,
            )
            game_lengths_refresh_pending = game_lengths_fetch_queued or (
                _get_game_lengths_refresh_lock(
                    detail_item,
                    force=False,
                    fetch_hltb=True,
                )
                is not None
            )
    trakt_score = _build_trakt_popularity_context(detail_item, media_type)

    author_detail_keys = ("author", "authors", "people")
    authors_linked = []
    if (
        render_secondary_only
        and media_type in (
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        )
        and isinstance(media_metadata, dict)
    ):
        def _collect_authors_linked(metadata_payload):
            linked = []

            if detail_item:
                author_credits = (
                    detail_item.person_credits.filter(
                        role_type=CreditRoleType.AUTHOR.value,
                    )
                    .select_related("person")
                    .order_by("sort_order", "person__name")
                )
                for author_credit in author_credits:
                    person = author_credit.person
                    linked.append(
                        {
                            "source": person.source,
                            "person_id": person.source_person_id,
                            "name": person.name,
                        },
                    )

            authors_full_payload = metadata_payload.get("authors_full")
            if not linked and isinstance(authors_full_payload, list):
                for author in authors_full_payload:
                    person_id = author.get("person_id") or author.get("id")
                    name = (author.get("name") or "").strip()
                    if person_id is None or not name:
                        continue
                    linked.append(
                        {
                            "source": source,
                            "person_id": str(person_id),
                            "name": name,
                        },
                    )

            return linked

        authors_full = media_metadata.get("authors_full")
        if detail_item and isinstance(authors_full, list):
            _best_effort_detail_db_work(
                lambda: credits.sync_item_author_credits(detail_item, authors_full),
                operation_name="detail author-credit sync",
            )

        authors_linked = _collect_authors_linked(media_metadata)

        details_payload = media_metadata.get("details")
        if not isinstance(details_payload, dict):
            details_payload = {}

        # Old provider cache entries may include plain author names but no authors_full
        # IDs, which prevents author links from rendering.
        should_refresh_author_cache = (
            not authors_linked
            and detail_item is not None
            and any(details_payload.get(key) for key in author_detail_keys)
            and not isinstance(media_metadata.get("authors_full"), list)
        )
        if should_refresh_author_cache:
            cache_key = f"{source}_{media_type}_{media_id}"
            cache.delete(cache_key)
            media_metadata = services.get_media_metadata(media_type, media_id, source)
            if isinstance(media_metadata, dict):
                media_metadata.setdefault("cast", [])
                media_metadata.setdefault("crew", [])
                media_metadata.setdefault("studios_full", [])
                refreshed_authors_full = media_metadata.get("authors_full")
                if detail_item and isinstance(refreshed_authors_full, list):
                    _best_effort_detail_db_work(
                        lambda: credits.sync_item_author_credits(
                            detail_item,
                            refreshed_authors_full,
                        ),
                        operation_name="refreshed detail author-credit sync",
                    )
                authors_linked = _collect_authors_linked(media_metadata)

    studio_detail_keys = ("studios", "companies")
    studios_linked = []

    def _collect_studios_linked(metadata_payload):
        linked = []

        if detail_item:
            studio_credits = (
                detail_item.studio_credits.select_related("studio")
                .order_by("sort_order", "studio__name")
            )
            for studio_credit in studio_credits:
                studio = studio_credit.studio
                linked.append(
                    {
                        "source": studio.source,
                        "studio_id": studio.source_studio_id,
                        "name": studio.name,
                        "logo": studio.logo,
                    },
                )

        studios_full_payload = metadata_payload.get("studios_full")
        if not linked and isinstance(studios_full_payload, list):
            for studio in studios_full_payload:
                studio_id = studio.get("studio_id") or studio.get("id")
                name = (studio.get("name") or "").strip()
                if studio_id is None or not name:
                    continue
                linked.append(
                    {
                        "source": source,
                        "studio_id": str(studio_id),
                        "name": name,
                        "logo": (studio.get("logo") or "").strip(),
                    },
                )

        return linked

    if render_secondary_only and isinstance(media_metadata, dict):
        studios_linked = _collect_studios_linked(media_metadata)

    # Prefer a stored poster/cover override when the tracked item has one.
    if (
        detail_item
        and isinstance(media_metadata, dict)
        and detail_item.image
        and detail_item.image != settings.IMG_NONE
    ):
        media_metadata["image"] = detail_item.image

    # For TV shows and grouped anime, enrich season cards from season-detail metadata.
    if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and isinstance(
        media_metadata,
        dict,
    ):
        details = media_metadata.get("details")
        if not isinstance(details, dict):
            details = {}
            media_metadata["details"] = details

        related = media_metadata.setdefault("related", {})
        seasons = related.setdefault("seasons", [])
        has_specials = any(season.get("season_number") == 0 for season in seasons)
        show_title = Item._normalize_title_value(media_metadata.get("title"))

        if (
            render_secondary_only
            and source == Sources.TMDB.value
            and media_metadata.get("tvdb_id")
            and not has_specials
        ):
            try:
                specials_metadata = services.get_media_metadata(
                    "tv_with_seasons",
                    media_id,
                    source,
                    [0],
                )
                if isinstance(specials_metadata, dict) and specials_metadata.get("season/0"):
                    enriched_related = specials_metadata.get("related") or {}
                    enriched_seasons = enriched_related.get("seasons")
                    if isinstance(enriched_seasons, list):
                        related["seasons"] = enriched_seasons
                        seasons = enriched_seasons
            except services.ProviderAPIError:
                logger.warning(
                    "Skipping specials enrichment for media_id=%s due to provider API error",
                    media_id,
                )

        if render_secondary_only and seasons and source in {Sources.TMDB.value, Sources.TVDB.value}:
            season_numbers = sorted(
                {
                    season_number
                    for season in seasons
                    for season_number in [season.get("season_number")]
                    if season_number is not None
                },
            )
            if season_numbers:
                try:
                    grouped_season_metadata = services.get_media_metadata(
                        "tv_with_seasons",
                        media_id,
                        source,
                        season_numbers,
                    )
                except services.ProviderAPIError:
                    grouped_season_metadata = None
                    logger.warning(
                        "Skipping season card enrichment for media_id=%s due to provider API error",
                        media_id,
                    )
                if isinstance(grouped_season_metadata, dict):
                    for season in seasons:
                        season_number = season.get("season_number")
                        season_payload = grouped_season_metadata.get(
                            f"season/{season_number}",
                        )
                        if not isinstance(season_payload, dict):
                            continue

                        detailed_title = Item._normalize_title_value(
                            season_payload.get("season_title"),
                        )
                        if detailed_title and detailed_title != show_title:
                            season["season_title"] = detailed_title
                        elif season_number == 0:
                            season["season_title"] = "Specials"
                        elif season_number is not None:
                            season["season_title"] = f"Season {season_number}"

                        payload_details = season_payload.get("details") or {}
                        if season.get("episode_count") in (None, ""):
                            season["episode_count"] = (
                                payload_details.get("episodes")
                                or season_payload.get("max_progress")
                            )
                        if season.get("max_progress") in (None, ""):
                            season["max_progress"] = season_payload.get(
                                "max_progress",
                            )
                        merged_details = dict(season.get("details") or {})
                        if merged_details.get("episodes") in (None, ""):
                            merged_details["episodes"] = (
                                season.get("episode_count")
                                or payload_details.get("episodes")
                                or season_payload.get("max_progress")
                            )
                        if merged_details.get("first_air_date") in (None, ""):
                            merged_details["first_air_date"] = payload_details.get(
                                "first_air_date",
                            )
                        season["details"] = merged_details
                        if season.get("first_air_date") in (None, ""):
                            season["first_air_date"] = payload_details.get(
                                "first_air_date",
                            )
                        if season.get("image") in (None, "", settings.IMG_NONE):
                            season["image"] = season_payload.get("image") or season.get(
                                "image",
                            )

        if not details.get("runtime"):
            fallback_runtime = _get_tv_runtime_display_fallback(detail_item, media_metadata)
            if fallback_runtime:
                details["runtime"] = fallback_runtime

        if render_secondary_only:
            tv_poster = media_metadata.get("image")
            if tv_poster:
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

    if current_instance is not None:
        _best_effort_detail_followup(
            lambda: helpers.refresh_item_image_if_missing(
                current_instance.item,
                media_metadata.get("image") if isinstance(media_metadata, dict) else None,
            ),
            operation_name="image refresh",
        )

    if media_type in (
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    ):
        runtime_media = current_instance
        if runtime_media is None and detail_item is not None:
            runtime_model = apps.get_model(app_label="app", model_name=media_type)
            runtime_media = runtime_model(item=detail_item)

        if runtime_media is not None and isinstance(media_metadata, dict):
            BasicMedia.objects.annotate_max_progress([runtime_media], media_type)
            total_runtime_display = runtime_media.formatted_total_runtime
            if total_runtime_display and total_runtime_display != "--":
                details = media_metadata.get("details")
                if not isinstance(details, dict):
                    details = {}
                    media_metadata["details"] = details

                if details.get("runtime"):
                    ordered_details = {}
                    for key, value in details.items():
                        ordered_details[key] = value
                        if key == "runtime":
                            ordered_details["total_runtime"] = total_runtime_display
                    details.clear()
                    details.update(ordered_details)
                else:
                    details["total_runtime"] = total_runtime_display

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
        render_secondary_only
        and not public_view
        and current_instance
        and media_type in (
            MediaTypes.GAME.value,
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        )
        and isinstance(media_metadata, dict)
    ):
        details = media_metadata.get("details", {})
        if not isinstance(details, dict):
            details = {}
        metadata_genres = stats._coerce_genre_list(
            media_metadata.get("genres")
            or details.get("genres")
            or media_metadata.get("genre")
            or details.get("genre"),
        )
        item = current_instance.item
        genres_updated = False
        if item:
            if metadata_genres and metadata_genres != item.genres:
                item.genres = metadata_genres
                genre_save_outcome = _best_effort_detail_db_work(
                    lambda: item.save(update_fields=["genres"]),
                    operation_name="detail genre sync",
                )
                genres_updated = not genre_save_outcome.deferred
                media_metadata["genres"] = metadata_genres
            elif item.genres:
                media_metadata["genres"] = item.genres
        if genres_updated and media_type in (
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        ):
            day_keys = _collect_reading_activity_day_keys(user_medias)
            if day_keys:
                statistics_cache.invalidate_statistics_days(
                    request.user.id,
                    day_values=day_keys,
                    reason="details_genres_update",
                )

    play_stats, activity_subtitle = _build_detail_activity_state(
        media_type,
        media_metadata,
        current_instance=current_instance,
        user_medias=user_medias,
        public_view=public_view,
    )

    # Enrich related items with user tracking data
    # For public views, use list owner's data if available
    if render_secondary_only and media_metadata.get("related"):
        for section_name, related_items in media_metadata["related"].items():
            if related_items:
                enriched_related_items = helpers.enrich_items_with_user_data(
                    request,
                    related_items,
                    section_name=section_name,
                    user=list_owner,
                )
                if section_name == "seasons":
                    for enriched_item, raw_item in zip(
                        enriched_related_items,
                        related_items,
                        strict=False,
                    ):
                        if not isinstance(raw_item, dict):
                            continue
                        season_title = Item._normalize_title_value(
                            raw_item.get("season_title"),
                        )
                        show_title = Item._normalize_title_value(raw_item.get("title"))
                        if season_title and season_title != show_title:
                            enriched_item["card_title"] = season_title
                            continue

                        season_number = raw_item.get("season_number")
                        try:
                            season_number = (
                                int(season_number)
                                if season_number is not None
                                else None
                            )
                        except (TypeError, ValueError):
                            season_number = None

                        if season_number == 0:
                            enriched_item["card_title"] = "Specials"
                        elif season_number is not None:
                            enriched_item["card_title"] = f"Season {season_number}"

                # For anime shows, tag season items so media_url routes to anime season URLs
                if section_name == "seasons" and media_type == MediaTypes.ANIME.value:
                    for enriched_item in enriched_related_items:
                        item_dict = enriched_item.get("item")
                        if isinstance(item_dict, dict):
                            item_dict["route_media_type"] = MediaTypes.ANIME.value

                media_metadata["related"][section_name] = enriched_related_items

    # For music tracks, get linked artist and album for navigation
    music_artist = None
    music_album = None
    if media_type == MediaTypes.MUSIC.value and current_instance:
        music_artist = getattr(current_instance, "artist", None)
        music_album = getattr(current_instance, "album", None)

    notes_entry = None
    if render_secondary_only and not public_view and user_medias:
        if current_instance and current_instance.notes and current_instance.notes.strip():
            notes_entry = current_instance
        else:
            for entry in user_medias:
                if entry.notes and entry.notes.strip():
                    notes_entry = entry
                    break

    if (
        render_secondary_only
        and media_type == MediaTypes.ANIME.value
        and not media_metadata.get("episodes")
    ):
        flat_anime_episode_preview = _build_flat_anime_episode_preview(
            request,
            detail_item=detail_item,
            media_id=media_id,
            base_metadata=media_metadata,
            metadata_resolution_result=metadata_resolution_result,
            retry_max_retries=detail_db_max_retries,
            on_persistence_deferred=_mark_detail_persistence_deferred,
        )
        if flat_anime_episode_preview:
            media_metadata["episodes"] = flat_anime_episode_preview

    # Get collection entries for this item (if not public view and not podcast)
    collection_entry = None
    collection_entries = []
    collection_stats = None
    fetching_collection_data = False
    item_id_for_polling = None

    if (
        render_secondary_only
        and not public_view
        and media_type != MediaTypes.PODCAST.value
    ):
        from app.helpers import get_item_collection_entries, get_tv_show_collection_stats

        try:
            item = detail_item or Item.objects.get(**detail_item_lookup)
            collection_entries = list(get_item_collection_entries(request.user, item))
            collection_entry = collection_entries[0] if collection_entries else None

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
                    followup_started = _best_effort_detail_followup(
                        lambda: fetch_collection_metadata_for_item.delay(
                            user_id=request.user.id,
                            item_id=item.id,
                            lookup_policy="cached_only",
                        ),
                        operation_name="collection metadata auto-fetch",
                        fallback=None,
                    )
                    if followup_started is not None:
                        # Use module-level logger directly to avoid UnboundLocalError
                        logging.getLogger(__name__).info(
                            "Triggered background collection fetch for %s - %s (item_id=%s)",
                            request.user.username,
                            item.title,
                            item.id,
                        )
                        # TODO(issue-166): Re-enable a user-facing collection-fetching banner only after
                        # the background task reliably self-resolves for empty collections; remove this
                        # reminder once that task/UX overhaul is complete.
                        fetching_collection_data = True
                        item_id_for_polling = item.id
        except Item.DoesNotExist:
            pass

    has_collection_data = bool(collection_entries) or collection_entry is not None

    if media_type in [MediaTypes.TV.value, MediaTypes.MOVIE.value, MediaTypes.ANIME.value]:
        watch_provider_payload = media_metadata.get("providers")
        if (
            render_secondary_only
            and detail_item
            and media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value)
            and (not watch_provider_payload or source == Sources.TVDB.value)
        ):
            tmdb_media_id = metadata_resolution.resolve_provider_media_id(
                detail_item,
                Sources.TMDB.value,
                route_media_type=media_type,
                persistence_mode="best_effort",
                retry_max_retries=detail_db_max_retries,
                on_deferred=_mark_detail_persistence_deferred,
            )
            if tmdb_media_id:
                tmdb_metadata = services.get_media_metadata(
                    media_type,
                    tmdb_media_id,
                    Sources.TMDB.value,
                )
                watch_provider_payload = tmdb_metadata.get("providers")

        watch_providers = (
            tmdb.filter_providers(
                watch_provider_payload,
                request.user.watch_provider_region,
            )
            if watch_provider_payload is not None
            else None
        )
    else:
        watch_providers = None

    display_provider = (
        metadata_resolution_result.display_provider
        if metadata_resolution_result
        else source
    )
    identity_provider = (
        metadata_resolution_result.identity_provider
        if metadata_resolution_result
        else source
    )
    grouped_preview = (
        metadata_resolution_result.grouped_preview
        if metadata_resolution_result
        else None
    )
    grouped_preview_target = (
        metadata_resolution_result.grouped_preview_target
        if metadata_resolution_result
        else None
    )
    metadata_provider_options = metadata_resolution.available_metadata_provider_options(
        media_type,
        identity_provider=identity_provider,
    )
    can_update_metadata_provider = bool(
        not public_view
        and detail_item is not None
        and metadata_provider_options
    )
    can_migrate_grouped_anime = False
    migrated_grouped_item = None
    migrated_grouped_title = None
    if (
        render_secondary_only
        and not public_view
        and media_type == MediaTypes.ANIME.value
        and detail_item is not None
    ):
        migrated_entry = (
            Anime.all_objects.filter(
                user=request.user,
                item=detail_item,
                migrated_to_item__isnull=False,
            )
            .select_related("migrated_to_item")
            .order_by("-migrated_at")
            .first()
        )
        if migrated_entry and migrated_entry.migrated_to_item:
            migrated_grouped_item = migrated_entry.migrated_to_item
            migrated_grouped_title = migrated_grouped_item.get_display_title(
                request.user,
            )

        can_migrate_grouped_anime = bool(
            detail_item.source == Sources.MAL.value
            and detail_item.media_type == MediaTypes.ANIME.value
            and display_provider in {Sources.TMDB.value, Sources.TVDB.value}
            and grouped_preview
            and Anime.objects.filter(user=request.user, item=detail_item).exists()
        )

    episode_load_more = None
    if (
        render_secondary_only
        and media_type != MediaTypes.PODCAST.value
        and media_metadata.get("episodes")
    ):
        media_metadata["episodes"] = _normalize_detail_episode_actions(
            media_metadata["episodes"],
        )
        media_metadata["episodes"], episode_load_more = _paginate_detail_episodes(
            request,
            media_metadata["episodes"],
        )

    context = {
        "user": request.user,
        "media": media_metadata,
        "media_type": media_type,
        "authors_linked": authors_linked,
        "author_detail_keys": author_detail_keys,
        "studios_linked": studios_linked,
        "studio_detail_keys": studio_detail_keys,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "music_artist": music_artist,
        "music_album": music_album,
        "public_view": public_view,
        "play_stats": play_stats,
        "activity_subtitle": activity_subtitle,
        "trakt_score": trakt_score,
        "game_lengths": game_lengths,
        "game_lengths_pending": game_lengths_refresh_pending
        and not (game_lengths and game_lengths.get("available")),
        "notes_entry": notes_entry,
        "collection_entry": collection_entry,
        "collection_entries": collection_entries,
        "collection_stats": collection_stats,
        "has_collection_data": has_collection_data,
        "fetching_collection_data": fetching_collection_data if not public_view else False,
        "item_id_for_polling": item_id_for_polling if not public_view else None,
        "watch_providers": watch_providers,
        "watch_provider_region": request.user.watch_provider_region,
        "detail_link_sections": _build_detail_link_sections(
            media_metadata,
            media_type,
            identity_provider,
            display_provider,
        ),
        "detail_tag_sections": _build_detail_tag_sections(
            media_metadata,
            detail_item,
            request.user,
        ),
        "detail_tag_preview_genres_json": json.dumps(
            _resolve_detail_tag_genres(media_metadata, detail_item)
        ),
        "display_provider": display_provider,
        "identity_provider": identity_provider,
        "metadata_provider_options": metadata_provider_options,
        "metadata_provider_mapping_status": (
            metadata_resolution_result.mapping_status
            if metadata_resolution_result
            else "identity"
        ),
        "grouped_preview": grouped_preview,
        "grouped_preview_target": grouped_preview_target,
        "can_update_metadata_provider": can_update_metadata_provider,
        "can_migrate_grouped_anime": can_migrate_grouped_anime,
        "migrated_grouped_item": migrated_grouped_item,
        "migrated_grouped_title": migrated_grouped_title,
        "episode_load_more": episode_load_more,
        "detail_persistence_deferred": detail_persistence_deferred,
        "detail_return_url": detail_return_url,
        "detail_secondary_fragment_url": detail_secondary_fragment_url,
        "defer_detail_secondary": defer_detail_secondary,
        "render_secondary_only": render_secondary_only,
    }
    logger.info(
        "detail_render_complete path=%s phase=%s media_type=%s source=%s duration_ms=%.2f",
        request.path,
        "secondary" if render_secondary_only else "shell",
        media_type,
        source,
        (time.perf_counter() - detail_view_started_at) * 1000,
    )
    return render(
        request,
        (
            "app/components/detail_secondary_content.html"
            if render_secondary_only
            else "app/media_details.html"
        ),
        context,
    )


@login_not_required
@require_GET
def season_details(
    request, source, media_id, title, season_number, parent_media_type=None,
):
    """Return the details page for a season."""
    detail_view_started_at = time.perf_counter()
    render_secondary_only = request.GET.get("fragment") == DETAIL_SECONDARY_FRAGMENT
    defer_detail_secondary = not render_secondary_only
    detail_return_url = _detail_request_url(request)
    detail_secondary_fragment_url = _detail_request_url(
        request,
        fragment=DETAIL_SECONDARY_FRAGMENT,
    )

    # Treat all anonymous views as public (no user-specific data/actions)
    is_anonymous = not request.user.is_authenticated
    public_view = is_anonymous
    public_list_view = request.GET.get("public_view") == "1" and is_anonymous

    # For public views, find a public list containing this item to get the owner
    list_owner = None
    if public_list_view:
        try:
            item = Item.objects.filter(
                media_id=media_id,
                source=source,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
            ).first()
            if item:
                public_list = CustomList.objects.filter(
                    visibility="public",
                    items=item,
                ).select_related("owner").first()
                if public_list:
                    list_owner = public_list.owner
        except Exception:
            # If we can't find a list owner, list_owner stays None
            pass

    season_item = Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.SEASON.value,
        season_number=season_number,
    ).first()
    show_item = _get_local_show_item(media_id, source)
    season_key = f"season/{season_number}"
    season_item_is_local_only = (
        season_item is not None
        and season_item.provider_metadata_status
        == ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
    )

    # For public views, we don't need user media data
    if public_view:
        user_medias = []
        current_instance = None
    else:
        if season_item_is_local_only:
            user_medias = list(
                Season.objects.filter(
                    item__media_id=media_id,
                    item__media_type=MediaTypes.SEASON.value,
                    item__source=source,
                    item__season_number=season_number,
                    user=request.user,
                )
                .select_related("item", "related_tv", "related_tv__item")
                .prefetch_related("episodes", "episodes__item")
            )
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
    if season_item_is_local_only:
        tv_with_seasons_metadata = _build_local_tv_with_seasons_metadata(
            media_id,
            source,
            show_item=show_item,
            season_item=season_item,
        )
        season_metadata = _build_missing_season_metadata(
            tv_with_seasons_metadata,
            media_id,
            source,
            season_number,
            episodes_in_db,
            season_item=season_item,
            show_item=show_item,
        )
        season_metadata_missing = True
    else:
        tv_with_seasons_metadata = services.get_media_metadata(
            "tv_with_seasons",
            media_id,
            source,
            [season_number],
        )
        season_metadata = tv_with_seasons_metadata.get(season_key)
        season_metadata_missing = season_metadata is None
        if season_metadata_missing:
            tv_with_seasons_metadata = _build_local_tv_with_seasons_metadata(
                media_id,
                source,
                tv_metadata=tv_with_seasons_metadata,
                show_item=show_item,
                season_item=season_item,
            )
            season_metadata = _build_missing_season_metadata(
                tv_with_seasons_metadata,
                media_id,
                source,
                season_number,
                episodes_in_db,
                season_item=season_item,
                show_item=show_item,
            )

    if current_instance is not None and isinstance(season_metadata, dict):
        try:
            helpers.refresh_item_image_if_missing(
                current_instance.item,
                season_metadata.get("image"),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Skipping season image refresh for %s due to error",
                request.path,
                exc_info=True,
            )

    default_season_title = (
        "Specials" if season_number == 0 else f"Season {season_number}"
    )
    anime_show_item = Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.TV.value,
        library_media_type=MediaTypes.ANIME.value,
    ).first()
    if isinstance(season_metadata, dict):
        season_metadata.setdefault(
            "season_header_title",
            season_metadata.get("season_title") or default_season_title,
        )
        season_metadata.setdefault("season_alternative_title", None)
        if anime_show_item:
            provider_season_title = (season_metadata.get("season_title") or "").strip()
            if provider_season_title and provider_season_title != default_season_title:
                season_metadata["season_header_title"] = default_season_title
                season_metadata["season_alternative_title"] = provider_season_title

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

    if render_secondary_only and season_item is None:
        season_defaults = {
            **Item.title_fields_from_metadata(
                season_metadata if isinstance(season_metadata, dict) else {},
                fallback_title=((season_metadata or {}).get("title") or ""),
            ),
            "image": (
                (season_metadata or {}).get("image")
                if isinstance(season_metadata, dict)
                else settings.IMG_NONE
            )
            or settings.IMG_NONE,
            "provider_metadata_status": (
                ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
                if season_metadata_missing and season_number > 0
                else ""
            ),
        }
        season_item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults=season_defaults,
        )
    elif render_secondary_only and season_metadata_missing and season_number > 0:
        season_item = _save_provider_metadata_status(
            season_item,
            ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value,
        )

    # Save episode runtimes from raw metadata before processing for display
    # This ensures runtime data is persisted when viewing the season page
    if (
        render_secondary_only
        and not season_metadata_missing
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
                defaults={
                    "title": season_metadata.get("title", ""),
                    "image": settings.IMG_NONE,
                },
            )
            
            # Extract runtime from raw episode data (TMDB returns integer minutes)
            runtime_minutes = None
            if episode.get("runtime") is not None:
                runtime_minutes = (
                    int(episode["runtime"])
                    if episode["runtime"] > 0
                    else None
                )
            elif episode.get("air_date"):
                # Check if episode has aired
                try:
                    if isinstance(episode["air_date"], str):
                        date_obj = datetime.strptime(episode["air_date"], "%Y-%m-%d")
                        air_date_dt = timezone.make_aware(
                            date_obj,
                            timezone.get_current_timezone(),
                        )
                    else:
                        air_date_dt = episode["air_date"]
                    
                    if (
                        air_date_dt
                        and air_date_dt.year > 1900
                        and air_date_dt <= current_datetime
                    ):
                        # Episode has aired but no runtime - mark as unknown (use 999998)
                        runtime_minutes = 999998
                except (ValueError, TypeError):
                    pass
            
            # Only update if runtime is actually new (not just saving the same value)
            if episode_item.runtime_minutes != runtime_minutes:
                episode_item.runtime_minutes = runtime_minutes
                episodes_to_update.append(episode_item)
        
        if episodes_to_update:
            Item.objects.bulk_update(
                episodes_to_update,
                ["runtime_minutes"],
                batch_size=100,
            )
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

    if render_secondary_only and not season_metadata_missing:
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

    if (
        season_item
        and isinstance(season_metadata, dict)
        and season_item.image
        and season_item.image != settings.IMG_NONE
    ):
        season_metadata["image"] = season_item.image

    season_provider_metadata_status = (
        season_item.provider_metadata_status
        if season_item is not None
        else (
            ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
            if season_metadata_missing and season_number > 0
            else ""
        )
    )
    if isinstance(season_metadata, dict):
        season_metadata["provider_metadata_status"] = season_provider_metadata_status

    # Add collection_entry data to each episode (if not public view)
    if render_secondary_only and not public_view and season_metadata.get("episodes"):
        from app.models import Item as ItemModel, CollectionEntry
        
        # Get all episode items for this season
        episode_numbers = [
            ep.get("episode_number")
            for ep in season_metadata["episodes"]
        ]
        episode_items = ItemModel.objects.filter(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.EPISODE.value,
            season_number=season_number,
            episode_number__in=episode_numbers,
        )
        
        # Build episode_number → Item map for item references and collection lookups
        item_by_episode_number = {
            item.episode_number: item
            for item in episode_items
            if item.episode_number is not None
        }
        episode_item_ids = [
            item_by_episode_number[ep_num].id
            for ep_num in item_by_episode_number
        ]
        collection_entries = {}
        if episode_item_ids:
            collection_entries_qs = (
                CollectionEntry.objects.filter(
                    user=request.user,
                    item_id__in=episode_item_ids,
                )
                .select_related("item")
                .order_by("-collected_at", "-id")
            )
            # Map by episode_number for quick lookup
            for entry in collection_entries_qs:
                ep_num = entry.item.episode_number
                if ep_num is not None and ep_num not in collection_entries:
                    collection_entries[ep_num] = entry

        # Add collection_entry and item reference to each episode
        for episode in season_metadata["episodes"]:
            episode_number = episode.get("episode_number")
            episode["collection_entry"] = collection_entries.get(episode_number)
            episode["item"] = item_by_episode_number.get(episode_number)

    # Enrich related items with user tracking data
    # For public views, use list owner's data if available
    if render_secondary_only and season_metadata.get("related"):
        for section_name, related_items in season_metadata["related"].items():
            if related_items:
                season_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                        section_name=section_name,
                        user=list_owner,
                    )
                )

    if current_instance and hasattr(current_instance, "derived_status_from_episode_progress"):
        season_max_progress = (
            season_metadata.get("max_progress")
            if isinstance(season_metadata, dict)
            else None
        )
        if (
            current_instance.derived_status_from_episode_progress(
                max_progress=season_max_progress,
            )
            == Status.COMPLETED.value
            and current_instance.status != Status.COMPLETED.value
        ):
            current_instance.promote_to_completed_if_fully_watched(
                max_progress=season_max_progress,
            )
        current_instance.max_progress = season_max_progress
        current_instance.status = current_instance.derived_status_from_episode_progress(
            max_progress=season_max_progress,
        )
        for user_media in user_medias:
            if not hasattr(user_media, "derived_status_from_episode_progress"):
                continue
            user_media.max_progress = season_max_progress
            user_media.status = user_media.derived_status_from_episode_progress(
                max_progress=season_max_progress,
            )

    # Get collection entry, stats, and metadata for this season (if not public view)
    collection_entry = None
    collection_entries = []
    season_collection_stats = None
    fetching_collection_data = False
    item_id_for_polling = None
    if render_secondary_only and not public_view:
        from app.helpers import (
            get_item_collection_entries,
            get_season_collection_metadata,
            get_season_collection_stats,
        )
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
                show_collection_entry = get_item_collection_entries(request.user, show_item).first()
                
                logger.info("Season page: Checking show %s (item_id=%s) - collection entry exists: %s", 
                           show_item.title, show_item.id, show_collection_entry is not None)
                
                # If no collection entry exists for the show and auto-fetch is supported, trigger background fetch
                if not show_collection_entry and config.supports_collection_auto_fetch(show_item.media_type):
                    plex_account = getattr(request.user, "plex_account", None)
                    if plex_account and plex_account.plex_token:
                        try:
                            from integrations.tasks import fetch_collection_metadata_for_item
                            # Trigger background task to fetch collection data for the show
                            result = fetch_collection_metadata_for_item.delay(
                                user_id=request.user.id,
                                item_id=show_item.id,
                                lookup_policy="cached_only",
                            )
                            logger.info("Triggered background collection fetch for show %s - %s (item_id=%s) from season page (task_id=%s)", 
                                       request.user.username, show_item.title, show_item.id, result.id if result else "None")
                            # TODO(issue-166): Re-enable a user-facing collection-fetching banner only
                            # after the background task reliably self-resolves for empty collections;
                            # remove this reminder once that task/UX overhaul is complete.
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
                logger.error("Error checking show collection entry in season_details: %s", exception_summary(exc), exc_info=True)
            
            # Get collection entry for the season item itself (if it exists)
            collection_entries = list(get_item_collection_entries(request.user, season_item))
            season_collection_entry = collection_entries[0] if collection_entries else None
            
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

    if (
        render_secondary_only
        and season_item
        and current_instance
        and season_number > 0
        and season_item.provider_metadata_status
        != ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
        and trakt_popularity_service.trakt_provider.is_configured()
        and trakt_popularity_service.needs_refresh(season_item)
    ):
        try:
            trakt_popularity_service.refresh_trakt_popularity(
                season_item,
                route_media_type=MediaTypes.SEASON.value,
                force=False,
            )
            season_item.refresh_from_db()
        except Exception as exc:
            logger.warning(
                "trakt_popularity_season_refresh_failed item_id=%s media_id=%s season=%s error=%s",
                season_item.id,
                season_item.media_id,
                season_number,
                exception_summary(exc),
            )

    has_collection_data = bool(collection_entries) or collection_entry is not None
    trakt_score = _build_trakt_popularity_context(
        season_item,
        MediaTypes.SEASON.value,
    )
    episode_load_more = None
    if render_secondary_only and season_metadata.get("episodes"):
        season_metadata["episodes"] = _normalize_detail_episode_actions(
            season_metadata["episodes"],
        )
        season_metadata["episodes"], episode_load_more = _paginate_detail_episodes(
            request,
            season_metadata["episodes"],
        )

    # Resolve parent media type: anime URL kwarg takes priority, else detect via DB
    if parent_media_type is None and anime_show_item and getattr(request.user, "anime_enabled", False):
        parent_media_type = MediaTypes.ANIME.value
    if parent_media_type is None:
        parent_media_type = MediaTypes.TV.value

    context = {
        "user": request.user,
        "media": season_metadata,
        "tv": tv_with_seasons_metadata,
        "media_type": MediaTypes.SEASON.value,
        "parent_media_type": parent_media_type,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "public_view": public_view,
        "collection_entry": collection_entry,
        "collection_entries": collection_entries,
        "collection_stats": season_collection_stats,
        "has_collection_data": has_collection_data,
        "fetching_collection_data": (
            fetching_collection_data if not public_view else False
        ),
        "item_id_for_polling": item_id_for_polling if not public_view else None,
        "trakt_score": trakt_score,
        "watch_providers": tmdb.filter_providers(
            season_metadata.get("providers"), request.user.watch_provider_region
        ),
        "watch_provider_region": request.user.watch_provider_region,
        "detail_link_sections": _build_detail_link_sections(
            season_metadata,
            MediaTypes.SEASON.value,
            source,
            source,
        ),
        "detail_tag_sections": _build_detail_tag_sections(
            season_metadata,
            season_item,
            request.user,
        ),
        "detail_tag_preview_genres_json": json.dumps(
            _resolve_detail_tag_genres(season_metadata, season_item)
        ),
        "display_provider": source,
        "identity_provider": source,
        "episode_load_more": episode_load_more,
        "season_provider_metadata_status": season_provider_metadata_status,
        "season_provider_metadata_banner": LOCAL_ONLY_MISSING_SEASON_BANNER,
        "season_provider_metadata_is_local_only": (
            season_provider_metadata_status
            == ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
        ),
        "detail_return_url": detail_return_url,
        "detail_secondary_fragment_url": detail_secondary_fragment_url,
        "defer_detail_secondary": defer_detail_secondary,
        "render_secondary_only": render_secondary_only,
    }
    logger.info(
        "detail_render_complete path=%s phase=%s media_type=%s source=%s duration_ms=%.2f",
        request.path,
        "secondary" if render_secondary_only else "shell",
        MediaTypes.SEASON.value,
        source,
        (time.perf_counter() - detail_view_started_at) * 1000,
    )
    return render(
        request,
        (
            "app/components/detail_secondary_content.html"
            if render_secondary_only
            else "app/media_details.html"
        ),
        context,
    )


def episode_details(request, source, media_id, title, season_number, episode_number, parent_media_type=None):
    """Return the details page for a single episode."""
    is_anonymous = not request.user.is_authenticated
    public_view = is_anonymous

    tv_with_seasons_metadata = services.get_media_metadata(
        "tv_with_seasons",
        media_id,
        source,
        [season_number],
    )
    season_key = f"season/{season_number}"
    season_metadata = tv_with_seasons_metadata.get(season_key) or {}

    if public_view:
        current_season_instance = None
        episodes_in_db = []
    else:
        user_seasons = BasicMedia.objects.filter_media_prefetch(
            request.user,
            media_id,
            MediaTypes.SEASON.value,
            source,
            season_number=season_number,
        )
        current_season_instance = user_seasons[0] if user_seasons else None
        episodes_in_db = (
            current_season_instance.episodes.all() if current_season_instance else []
        )

    processed_episodes = []
    if season_metadata.get("episodes"):
        if source == Sources.MANUAL.value:
            from app.providers import manual
            processed_episodes = manual.process_episodes(season_metadata, episodes_in_db)
        else:
            processed_episodes = tmdb.process_episodes(season_metadata, episodes_in_db)

    processed_episodes = _normalize_detail_episode_actions(processed_episodes)
    episode_data = next(
        (ep for ep in processed_episodes if ep["episode_number"] == episode_number),
        None,
    )

    episode_metadata = {}
    if source == Sources.TMDB.value:
        try:
            episode_metadata = tmdb.episode(media_id, season_number, episode_number)
        except Exception:
            pass

    episode_item = Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.EPISODE.value,
        season_number=season_number,
        episode_number=episode_number,
    ).first()

    if episode_data is not None and episode_item is not None:
        episode_data["item"] = episode_item

    season_url = reverse(
        "anime_season_details" if parent_media_type == MediaTypes.ANIME.value else "season_details",
        kwargs={
            "source": source,
            "media_id": media_id,
            "title": title,
            "season_number": season_number,
        },
    )

    context = {
        "user": request.user,
        "episode": episode_data,
        "episode_metadata": episode_metadata,
        "season_metadata": season_metadata,
        "current_instance": current_season_instance,
        "public_view": public_view,
        "parent_media_type": parent_media_type or MediaTypes.TV.value,
        "season_url": season_url,
        "media_id": media_id,
        "source": source,
        "season_number": season_number,
        "episode_number": episode_number,
        "show_title": tv_with_seasons_metadata.get("title") or title,
        "season_title": season_metadata.get("season_title") or f"Season {season_number}",
        "episode_title": episode_metadata.get("episode_title")
            or (episode_data or {}).get("title")
            or f"Episode {episode_number}",
        "detail_return_url": request.build_absolute_uri(),
    }
    return render(request, "app/episode_details.html", context)

@require_POST
def music_bulk_save(request):
    """Dispatch a bulk music play range as a background task and return immediately."""
    context_kind = (request.POST.get("context_kind") or "").strip()
    context_id = request.POST.get("context_id")
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.MUSIC.value,
    )

    if context_kind not in {"artist", "album"}:
        return HttpResponseBadRequest("Invalid music bulk tracking context.")

    start_date_str = (request.POST.get("start_date") or "").strip()
    end_date_str = (request.POST.get("end_date") or "").strip()

    if not start_date_str or not end_date_str:
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=422)
            response["HX-Trigger"] = json.dumps({
                "showToast": {
                    "message": "Start and end dates are required.",
                    "type": "error",
                },
            })
            return response
        messages.error(request, "Start and end dates are required.")
        return redirect(request.POST.get("return_url") or "/")

    try:
        first_season_number = int(request.POST["first_season_number"])
        first_episode_number = int(request.POST["first_episode_number"])
        last_season_number = int(request.POST["last_season_number"])
        last_episode_number = int(request.POST["last_episode_number"])
    except (KeyError, ValueError):
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=422)
            response["HX-Trigger"] = json.dumps({
                "showToast": {
                    "message": "Invalid track range.",
                    "type": "error",
                },
            })
            return response
        messages.error(request, "Invalid track range.")
        return redirect(request.POST.get("return_url") or "/")

    track_count = max(int(request.POST.get("episode_count") or 0), 0)
    write_mode = request.POST.get("write_mode", "add")
    distribution_mode = request.POST.get("distribution_mode", "even")

    from app.tasks import bulk_music_plays_task  # noqa: PLC0415

    task = bulk_music_plays_task.apply_async(
        kwargs={
            "user_id": request.user.id,
            "context_kind": context_kind,
            "context_id": int(context_id),
            "first_season_number": first_season_number,
            "first_episode_number": first_episode_number,
            "last_season_number": last_season_number,
            "last_episode_number": last_episode_number,
            "write_mode": write_mode,
            "distribution_mode": distribution_mode,
            "start_date_str": start_date_str,
            "end_date_str": end_date_str,
        },
        priority=settings.CELERY_TASK_PRIORITY_INTERACTIVE,
    )
    logger.info(
        "bulk_music_plays_task_dispatched task_id=%s user_id=%d context_kind=%s context_id=%s",
        task.id,
        request.user.id,
        context_kind,
        context_id,
    )

    if request.headers.get("HX-Request"):
        plural = "s" if track_count != 1 else ""
        response = HttpResponse(status=204)
        response["HX-Trigger"] = json.dumps({
            "closeModal": {},
            "showToast": {
                "message": f"Adding plays to {track_count} track{plural}.",
                "type": "info",
            },
        })
        return response

    messages.info(request, f"Adding plays to {track_count} tracks.")
    return redirect(request.POST.get("return_url") or "/")


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

        # Keep the previous day payload readable until the targeted refresh
        # overwrites it so later navigation does not fall into a cold-miss path.
        history_cache.invalidate_history_days(
            request.user.id,
            day_keys=history_day_keys,
            logging_styles=logging_styles,
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



def album_track_modal(request, album_id):
    """Return the shared tracking form modal for a music album."""
    from django.shortcuts import get_object_or_404

    from app.forms import AlbumTrackerForm
    from app.models import AlbumTracker

    album = get_object_or_404(Album, id=album_id)
    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()
    form = AlbumTrackerForm(
        instance=tracker,
        initial={"album_id": album.id},
        user=request.user,
    )
    return _render_music_tracker_modal(
        request,
        title=album.title,
        tracker=tracker,
        form=form,
        save_url=reverse("album_save"),
        delete_url=reverse("album_delete"),
        release_date_shortcut=_track_modal_release_date_shortcut(album.release_date),
        bulk_domain=bulk_music_tracking.build_album_play_domain(request.user, album),
    )


@require_POST
def album_save(request):
    """Save an album tracker - mirrors artist_save."""
    from django.shortcuts import get_object_or_404

    from app.forms import AlbumTrackerForm
    from app.models import AlbumTracker

    album_id = request.POST.get("album_id")
    album = get_object_or_404(Album, id=album_id)
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.MUSIC.value,
    )

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
    return redirect(_music_album_detail_url(album))


@require_POST
def album_delete(request):
    """Delete an album tracker - mirrors artist_delete."""
    from django.shortcuts import get_object_or_404

    from app.models import AlbumTracker

    album_id = request.POST.get("album_id")
    album = get_object_or_404(Album, id=album_id)
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.MUSIC.value,
    )

    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {album.title} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect(_music_album_detail_url(album))


@require_POST
def song_save(request):
    """Handle adding a listen for a song - mirrors episode_save for episodes."""
    from django.shortcuts import get_object_or_404
    from django.utils import timezone
    from django.utils.dateparse import parse_date, parse_datetime
    from django.template.loader import render_to_string

    from app.models import AlbumTracker, CollectionEntry, Track

    recording_id = request.POST.get("recording_id")
    album_id = request.POST.get("album_id")
    track_id = request.POST.get("track_id")
    end_date_str = request.POST.get("end_date")
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.MUSIC.value,
    )

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

    if request.headers.get("HX-Request"):
        music = (
            Music.objects.filter(
                user=request.user,
                album=album,
                track=track,
            )
            .select_related("item", "track", "album")
            .order_by("-created_at")
            .first()
        )
        if music is None:
            return HttpResponse("Music entry not found", status=404)

        track_data = {
            "track": track,
            "music": music,
            "history": list(music.history.all().order_by("-end_date")),
            "collection_entry": CollectionEntry.objects.filter(
                user=request.user,
                item=music.item,
            )
            .select_related("item")
            .first(),
        }
        user_music_entries = list(
            Music.objects.filter(
                user=request.user,
                album=album,
            ).select_related("item", "track"),
        )
        album_tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()
        first_listened, last_listened, collapse_same_day = _music_activity_date_range(
            user_music_entries,
        )
        music_album_activity_subtitle = _build_music_album_activity_subtitle(
            album,
            album_tracker,
            len(user_music_entries),
            Track.objects.filter(album=album).count(),
            first_listened,
            last_listened,
            collapse_same_day,
        )

        response = HttpResponse()
        response.write(
            render_to_string(
                "app/components/detail_music_track_button.html",
                {
                    "track_data": track_data,
                    "track_button_oob": True,
                },
                request=request,
            ),
        )
        response.write(
            render_to_string(
                "app/components/detail_music_track_history_line.html",
                {
                    "track_data": track_data,
                    "history_oob": True,
                    "user": request.user,
                },
                request=request,
            ),
        )
        if music_album_activity_subtitle:
            response.write(
                render_to_string(
                    "app/components/detail_music_album_activity_subtitle.html",
                    {
                        "album": album,
                        "music_album_activity_subtitle": music_album_activity_subtitle,
                        "subtitle_oob": True,
                        "user": request.user,
                    },
                    request=request,
                ),
            )
        response["HX-Trigger"] = json.dumps(
            {
                "closeModal": {},
                "showToast": {
                    "message": f"Added listen for {track.title if track else 'track'}.",
                    "type": "success",
                },
            },
        )
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect(_music_album_detail_url(album))
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
def cache_status(request):
    """Return cache status metadata for history, statistics, or discover cache.
    
    Query params:
        cache_type: 'history', 'statistics', or 'discover'
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
    if cache_type not in ("history", "statistics", "discover"):
        return JsonResponse(
            {"error": "Invalid cache_type. Must be 'history', 'statistics', or 'discover'"},
            status=400,
        )

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

    media_type = _resolve_discover_media_type_for_user(
        request.user,
        request.GET.get("media_type"),
    )
    show_more = request.GET.get("show_more") in {"1", "true", "True"}
    return JsonResponse(
        discover_tab_cache.get_tab_status(
            request.user.id,
            media_type,
            show_more=show_more,
        ),
    )


@require_GET
def service_worker(request):
    """Serve the service worker file from static files."""
    sw_path = Path(settings.STATICFILES_DIRS[0]) / "js" / "serviceworker.js"
    with sw_path.open(encoding="utf-8") as sw_file:
        response = HttpResponse(sw_file.read(), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response

