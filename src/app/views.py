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
from app.templatetags import app_tags
from app.signals import suppress_media_cache_change_signals
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
DETAIL_EPISODES_PER_PAGE = 25
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
        if self.media is None:
            return None
        return getattr(self.media, attr, None)


def _tracked_media_entries(entries):
    """Return the tracker-backed objects from mixed media-list entries."""
    tracked_entries = []
    for entry in entries:
        tracked_media = getattr(entry, "media", entry)
        if tracked_media is not None:
            tracked_entries.append(tracked_media)
    return tracked_entries

class _EmptyHistoryProxy:
    """Minimal queryset-like history object for empty podcast wrappers."""

    def all(self):
        return []

    def count(self):
        return 0

    def filter(self, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self


class _DummyPodcastWrapper:
    """Template-compatible podcast wrapper when no plays exist yet."""

    def __init__(self, item):
        self.item = item
        self.id = 0
        self.in_progress_instance_id = None
        self.history = _EmptyHistoryProxy()

    @property
    def completed_play_count(self):
        return 0

    @property
    def has_in_progress_entry(self):
        return False


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


def _collect_music_history_day_keys_for_album_ids(user, album_ids):
    """Return distinct history day keys for plays tied to the given album ids."""
    normalized_album_ids = sorted({album_id for album_id in album_ids or [] if album_id})
    if not normalized_album_ids:
        return []

    HistoricalMusic = apps.get_model("app", "HistoricalMusic")
    history_days = (
        HistoricalMusic.objects.filter(
            Q(history_user=user) | Q(history_user__isnull=True),
            album_id__in=normalized_album_ids,
            end_date__isnull=False,
        )
        .annotate(day=TruncDate("end_date"))
        .values_list("day", flat=True)
        .distinct()
    )
    return sorted(
        {
            history_cache.history_day_key(day_value)
            for day_value in history_days
            if day_value
        },
    )


def _collect_music_history_day_keys_for_artist(user, artist):
    """Return distinct history day keys for plays tied to an artist's albums."""
    album_ids = Album.objects.filter(artist=artist).values_list("id", flat=True)
    return _collect_music_history_day_keys_for_album_ids(user, album_ids)


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


def _format_game_length_minutes(minutes):
    """Return a display string for stored game-length minutes."""
    try:
        minutes = int(minutes or 0)
    except (TypeError, ValueError):
        minutes = 0
    return helpers.minutes_to_hhmm(minutes) if minutes > 0 else "--"


def _format_game_length_seconds(seconds):
    """Return a display string for stored game-length seconds."""
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    return _format_game_length_minutes(round(seconds / 60)) if seconds > 0 else "--"


def _mark_grouped_anime_route(media_items):
    """Annotate grouped-anime rows so templates route them through the Anime UI."""
    for media in media_items or []:
        setattr(media, "route_media_type", MediaTypes.ANIME.value)
        item = getattr(media, "item", None)
        if item is not None:
            setattr(item, "route_media_type", MediaTypes.ANIME.value)
    return media_items


def _build_game_length_card(label, value, count):
    """Return display metadata for a summary game-length card."""
    card_styles = {
        "Main Story": {
            "icon_template": "app/icons/book-open.svg",
            "icon_background": "rgba(96, 165, 250, 0.2)",
            "icon_color": "#60a5fa",
        },
        "Main + Extras": {
            "icon_template": "app/icons/list.svg",
            "icon_background": "rgba(52, 211, 153, 0.2)",
            "icon_color": "#34d399",
        },
        "Completionist": {
            "icon_template": "app/icons/ribbon.svg",
            "icon_background": "rgba(245, 158, 11, 0.2)",
            "icon_color": "#f59e0b",
        },
        "All PlayStyles": {
            "icon_template": "app/icons/four-square.svg",
            "icon_background": "rgba(167, 139, 250, 0.2)",
            "icon_color": "#a78bfa",
        },
        "Hastily": {
            "icon_template": "app/icons/clock-reversing.svg",
            "icon_background": "rgba(245, 158, 11, 0.2)",
            "icon_color": "#f59e0b",
        },
        "Normally": {
            "icon_template": "app/icons/clock.svg",
            "icon_background": "rgba(96, 165, 250, 0.2)",
            "icon_color": "#60a5fa",
        },
        "Completely": {
            "icon_template": "app/icons/circle-check.svg",
            "icon_background": "rgba(52, 211, 153, 0.2)",
            "icon_color": "#34d399",
        },
    }
    style = card_styles.get(
        label,
        {
            "icon_template": "app/icons/clock.svg",
            "icon_background": "rgba(129, 140, 248, 0.2)",
            "icon_color": "#818cf8",
        },
    )
    return {
        "label": label,
        "value": value,
        "count": count or 0,
        **style,
    }


def _build_game_lengths_context(detail_item):
    """Return template-ready game-length metadata for a stored item."""
    if not detail_item:
        return None

    payload = detail_item.provider_game_lengths or {}
    external_ids = detail_item.provider_external_ids or {}
    active_source = detail_item.provider_game_lengths_source or payload.get("active_source")
    if active_source == "hltb":
        hltb_payload = payload.get("hltb") or {}
        cards = []
        card_specs = [
            ("Main Story", hltb_payload.get("summary", {}).get("main_minutes"), hltb_payload.get("counts", {}).get("main")),
            (
                "Main + Extras",
                hltb_payload.get("summary", {}).get("main_plus_minutes"),
                hltb_payload.get("counts", {}).get("main_plus"),
            ),
            (
                "Completionist",
                hltb_payload.get("summary", {}).get("completionist_minutes"),
                hltb_payload.get("counts", {}).get("completionist"),
            ),
            (
                "All PlayStyles",
                hltb_payload.get("summary", {}).get("all_styles_minutes"),
                hltb_payload.get("counts", {}).get("all_styles"),
            ),
        ]
        for label, minutes, count in card_specs:
            if (minutes or 0) <= 0:
                continue
            cards.append(_build_game_length_card(label, _format_game_length_minutes(minutes), count))

        single_player_rows = []
        for row in hltb_payload.get("single_player_table") or []:
            single_player_rows.append(
                {
                    "label": row.get("label") or "",
                    "count": row.get("count") or 0,
                    "average": _format_game_length_minutes(row.get("average_minutes")),
                    "median": _format_game_length_minutes(row.get("median_minutes")),
                    "rushed": _format_game_length_minutes(row.get("rushed_minutes")),
                    "leisure": _format_game_length_minutes(row.get("leisure_minutes")),
                },
            )

        platform_rows = []
        for row in hltb_payload.get("platform_table") or []:
            platform_rows.append(
                {
                    "platform": row.get("platform") or "",
                    "count": row.get("count") or 0,
                    "main": _format_game_length_minutes(row.get("main_minutes")),
                    "main_plus": _format_game_length_minutes(row.get("main_plus_minutes")),
                    "completionist": _format_game_length_minutes(row.get("completionist_minutes")),
                    "fastest": _format_game_length_minutes(row.get("fastest_minutes")),
                    "slowest": _format_game_length_minutes(row.get("slowest_minutes")),
                },
            )

        return {
            "available": bool(cards),
            "source": "hltb",
            "source_label": "How Long to Beat",
            "source_url": hltb_payload.get("url")
            or (
                f"https://howlongtobeat.com/game/{external_ids['hltb_game_id']}"
                if external_ids.get("hltb_game_id")
                else None
            ),
            "match": detail_item.provider_game_lengths_match,
            "cards": cards,
            "submission_count": (hltb_payload.get("counts") or {}).get("all_styles") or 0,
            "single_player_rows": single_player_rows,
            "platform_rows": platform_rows,
        }

    if active_source == "igdb":
        igdb_payload = payload.get("igdb") or {}
        summary = igdb_payload.get("summary") or {}
        cards = []
        for label, key in (
            ("Hastily", "hastily_seconds"),
            ("Normally", "normally_seconds"),
            ("Completely", "completely_seconds"),
        ):
            value = summary.get(key) or 0
            if value <= 0:
                continue
            cards.append(
                _build_game_length_card(
                    label,
                    _format_game_length_seconds(value),
                    summary.get("count") or 0,
                ),
            )

        return {
            "available": bool(cards),
            "source": "igdb",
            "source_label": "Internet Games Database",
            "source_url": None,
            "match": detail_item.provider_game_lengths_match,
            "cards": cards,
            "submission_count": summary.get("count") or 0,
            "single_player_rows": [],
            "platform_rows": [],
        }

    return None


def _build_trakt_popularity_context(detail_item, route_media_type):
    """Return template-ready stored Trakt popularity metadata for a detail item."""
    if (
        not detail_item
        or route_media_type not in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.ANIME.value,
            MediaTypes.SEASON.value,
        )
        or not trakt_popularity_service.trakt_provider.is_configured()
        or detail_item.trakt_rating_count is None
    ):
        return None

    rating = detail_item.trakt_rating
    if rating is not None:
        try:
            rating = float(
                Decimal(str(rating)).quantize(
                    Decimal("0.1"),
                    rounding=ROUND_DOWN,
                ),
            )
        except (InvalidOperation, TypeError, ValueError):
            pass

    return {
        "rating": rating,
        "rating_count": detail_item.trakt_rating_count,
        "rank": detail_item.trakt_popularity_rank,
        "score": detail_item.trakt_popularity_score,
        "fetched_at": detail_item.trakt_popularity_fetched_at,
    }

def _apply_cached_hltb_link(media_metadata, detail_item):
    """Prefer a stored direct HLTB link when one has already been resolved."""
    if not detail_item or not isinstance(media_metadata, dict):
        return
    if detail_item.media_type != MediaTypes.GAME.value:
        return

    external_links = media_metadata.setdefault("external_links", {})
    if not isinstance(external_links, dict):
        external_links = {}
        media_metadata["external_links"] = external_links

    hltb_game_id = ((detail_item.provider_external_ids or {}).get("hltb_game_id"))
    if hltb_game_id:
        external_links["HowLongToBeat"] = f"https://howlongtobeat.com/game/{hltb_game_id}"
    elif "HowLongToBeat" not in external_links:
        search_url = game_length_services.get_hltb_search_url(media_metadata.get("title"))
        if search_url:
            external_links["HowLongToBeat"] = search_url


_DETAIL_LINK_BRANDS = {
    Sources.TMDB.value: {
        "logo_src": static("img/tmdb-logo.png"),
        "chip_classes": "border-cyan-400/18 bg-cyan-500/[0.07]",
        "badge_classes": "border-cyan-400/28 bg-cyan-500/14",
        "accent_classes": "text-cyan-100",
        "fallback_text": "TMDB",
    },
    Sources.TVDB.value: {
        "logo_src": static("img/tvdb-logo.png"),
        "chip_classes": "border-teal-400/18 bg-teal-500/[0.07]",
        "badge_classes": "border-teal-400/28 bg-teal-500/14",
        "accent_classes": "text-teal-100",
        "fallback_text": "TVDB",
    },
    Sources.MAL.value: {
        "logo_src": static("img/myanimelist-logo.svg"),
        "chip_classes": "border-indigo-400/18 bg-indigo-500/[0.07]",
        "badge_classes": "border-indigo-400/28 bg-indigo-500/14",
        "accent_classes": "text-indigo-100",
        "fallback_text": "MAL",
    },
    Sources.MANGAUPDATES.value: {
        "chip_classes": "border-fuchsia-400/18 bg-fuchsia-500/[0.07]",
        "badge_classes": "border-fuchsia-400/28 bg-fuchsia-500/14",
        "accent_classes": "text-fuchsia-100",
        "fallback_text": "MU",
    },
    Sources.IGDB.value: {
        "logo_src": static("img/igdb-logo.png"),
        "chip_classes": "border-orange-400/18 bg-orange-500/[0.07]",
        "badge_classes": "border-orange-400/28 bg-orange-500/14",
        "accent_classes": "text-orange-100",
        "fallback_text": "IGDB",
    },
    Sources.OPENLIBRARY.value: {
        "chip_classes": "border-sky-400/18 bg-sky-500/[0.07]",
        "badge_classes": "border-sky-400/28 bg-sky-500/14",
        "accent_classes": "text-sky-100",
        "fallback_text": "OL",
    },
    Sources.HARDCOVER.value: {
        "logo_src": static("img/hardcover-logo.png"),
        "chip_classes": "border-amber-400/18 bg-amber-500/[0.07]",
        "badge_classes": "border-amber-400/28 bg-amber-500/14",
        "accent_classes": "text-amber-100",
        "fallback_text": "HC",
    },
    Sources.COMICVINE.value: {
        "chip_classes": "border-lime-400/18 bg-lime-500/[0.07]",
        "badge_classes": "border-lime-400/28 bg-lime-500/14",
        "accent_classes": "text-lime-100",
        "fallback_text": "CV",
    },
    Sources.BGG.value: {
        "chip_classes": "border-stone-400/18 bg-stone-500/[0.07]",
        "badge_classes": "border-stone-400/28 bg-stone-500/14",
        "accent_classes": "text-stone-100",
        "fallback_text": "BGG",
    },
    Sources.MUSICBRAINZ.value: {
        "chip_classes": "border-rose-400/18 bg-rose-500/[0.07]",
        "badge_classes": "border-rose-400/28 bg-rose-500/14",
        "accent_classes": "text-rose-100",
        "fallback_text": "MB",
    },
    Sources.POCKETCASTS.value: {
        "chip_classes": "border-orange-400/18 bg-orange-500/[0.07]",
        "badge_classes": "border-orange-400/28 bg-orange-500/14",
        "accent_classes": "text-orange-100",
        "fallback_text": "PC",
    },
    Sources.AUDIOBOOKSHELF.value: {
        "chip_classes": "border-teal-400/18 bg-teal-500/[0.07]",
        "badge_classes": "border-teal-400/28 bg-teal-500/14",
        "accent_classes": "text-teal-100",
        "fallback_text": "ABS",
    },
    Sources.MANUAL.value: {
        "chip_classes": "border-slate-400/18 bg-slate-500/[0.07]",
        "badge_classes": "border-slate-400/28 bg-slate-500/14",
        "accent_classes": "text-slate-100",
        "fallback_text": "MAN",
    },
    "anilist": {
        "logo_src": static("img/anilist-logo.svg"),
        "chip_classes": "border-sky-400/18 bg-sky-500/[0.07]",
        "badge_classes": "border-sky-400/28 bg-sky-500/14",
        "accent_classes": "text-sky-100",
        "fallback_text": "AL",
    },
    "kitsu": {
        "logo_src": static("img/kitsu-logo.png"),
        "chip_classes": "border-orange-400/18 bg-orange-500/[0.07]",
        "badge_classes": "border-orange-400/28 bg-orange-500/14",
        "accent_classes": "text-orange-100",
        "fallback_text": "KT",
    },
    "simkl": {
        "logo_src": static("img/simkl-logo.png"),
        "chip_classes": "border-violet-400/18 bg-violet-500/[0.07]",
        "badge_classes": "border-violet-400/28 bg-violet-500/14",
        "accent_classes": "text-violet-100",
        "fallback_text": "SK",
    },
    "steam": {
        "logo_src": static("img/steam-logo.ico"),
        "chip_classes": "border-slate-400/18 bg-slate-500/[0.07]",
        "badge_classes": "border-slate-400/28 bg-slate-500/14",
        "accent_classes": "text-slate-100",
        "fallback_text": "STM",
    },
    "plex": {
        "logo_src": static("img/plex-logo.svg"),
        "chip_classes": "border-amber-400/18 bg-amber-500/[0.07]",
        "badge_classes": "border-amber-400/28 bg-amber-500/14",
        "accent_classes": "text-amber-100",
        "fallback_text": "PLX",
    },
    "lastfm": {
        "logo_src": static("img/lastfm-logo.png"),
        "chip_classes": "border-rose-400/18 bg-rose-500/[0.07]",
        "badge_classes": "border-rose-400/28 bg-rose-500/14",
        "accent_classes": "text-rose-100",
        "fallback_text": "LFM",
    },
    "imdb": {
        "logo_src": static("img/imdb-logo.png"),
        "chip_classes": "border-amber-400/18 bg-amber-500/[0.07]",
        "badge_classes": "border-amber-400/28 bg-amber-500/14",
        "accent_classes": "text-amber-100",
        "fallback_text": "IMDb",
    },
    "trakt": {
        "logo_src": static("img/trakt-logo.svg"),
        "chip_classes": "border-rose-400/18 bg-rose-500/[0.07]",
        "badge_classes": "border-rose-400/28 bg-rose-500/14",
        "accent_classes": "text-rose-100",
        "fallback_text": "Trakt",
    },
    "wikidata": {
        "logo_src": static("img/wikidata-logo.png"),
        "chip_classes": "border-sky-400/18 bg-sky-500/[0.07]",
        "badge_classes": "border-sky-400/28 bg-sky-500/14",
        "accent_classes": "text-sky-100",
        "fallback_text": "WD",
    },
    "letterboxd": {
        "chip_classes": "border-emerald-400/18 bg-emerald-500/[0.07]",
        "badge_classes": "border-emerald-400/28 bg-emerald-500/14",
        "accent_classes": "text-emerald-100",
        "fallback_text": "LB",
    },
    "howlongtobeat": {
        "logo_src": static("img/hltb-logo.png"),
        "chip_classes": "border-amber-400/18 bg-amber-500/[0.07]",
        "badge_classes": "border-amber-400/28 bg-amber-500/14",
        "accent_classes": "text-amber-100",
        "fallback_text": "HLTB",
    },
}

_DEFAULT_DETAIL_LINK_BRAND = {
    "chip_classes": "border-slate-400/18 bg-slate-500/[0.07]",
    "badge_classes": "border-slate-400/28 bg-slate-500/14",
    "accent_classes": "text-slate-100",
    "fallback_text": "LINK",
}


def _normalize_detail_link_brand_key(value):
    """Return a normalized lookup key for link-provider branding."""
    return slugify(str(value or "")).replace("-", "")


def _build_detail_link_entry(label, url, brand_key):
    """Return a template-ready chip payload for a media detail link."""
    if not url:
        return None

    brand = _DETAIL_LINK_BRANDS.get(
        _normalize_detail_link_brand_key(brand_key),
        _DEFAULT_DETAIL_LINK_BRAND,
    )
    fallback_text = brand.get("fallback_text") or slugify(label).replace("-", "")[:4].upper() or "LINK"
    return {
        "label": label,
        "url": url,
        "chip_classes": brand["chip_classes"],
        "badge_classes": brand["badge_classes"],
        "accent_classes": brand["accent_classes"],
        "logo_src": brand.get("logo_src"),
        "fallback_text": fallback_text,
    }


def _build_detail_link_sections(media_metadata, media_type, identity_provider, display_provider):
    """Return grouped source and external link chips for the media detail action row."""
    if not isinstance(media_metadata, dict):
        return []

    tracking_source_entries = []
    metadata_source_entries = []
    external_entries = []
    seen_urls = set()

    def append_entry(collection, label, url, brand_key):
        if not url or url in seen_urls:
            return
        entry = _build_detail_link_entry(label, url, brand_key)
        if entry is None:
            return
        seen_urls.add(url)
        collection.append(entry)

    primary_source_url = media_metadata.get("tracking_source_url") or media_metadata.get("source_url")
    if primary_source_url:
        append_entry(
            tracking_source_entries,
            app_tags.source_readable(identity_provider),
            primary_source_url,
            identity_provider,
        )

    display_source_url = media_metadata.get("display_source_url")
    if display_provider != identity_provider and display_source_url:
        append_entry(
            metadata_source_entries,
            app_tags.source_readable(display_provider),
            display_source_url,
            display_provider,
        )

    if media_type == MediaTypes.MOVIE.value and identity_provider == Sources.TMDB.value:
        media_id = media_metadata.get("media_id")
        if media_id:
            append_entry(
                external_entries,
                "Letterboxd",
                f"https://letterboxd.com/tmdb/{media_id}",
                "letterboxd",
            )

    external_links = media_metadata.get("external_links")
    if isinstance(external_links, dict):
        for name, url in external_links.items():
            append_entry(external_entries, name, url, name)

    sections = []
    if metadata_source_entries:
        if tracking_source_entries:
            sections.append(
                {
                    "title": "Tracking Source",
                    "entries": tracking_source_entries,
                }
            )
        sections.append(
            {
                "title": "Metadata Source",
                "entries": metadata_source_entries,
            }
        )
    elif tracking_source_entries:
        sections.append(
            {
                "title": "Source",
                "entries": tracking_source_entries,
            }
        )
    if external_entries:
        sections.append({"title": "External links", "entries": external_entries})
    return sections


def _format_detail_activity_duration(total_minutes, suffix):
    """Return a detail subtitle duration string for a total-minute value."""
    if not total_minutes:
        return None

    total_minutes = int(total_minutes)
    total_hours, remainder_minutes = divmod(total_minutes, 60)
    if total_hours > 0:
        return f"{total_hours}h {remainder_minutes}min {suffix}"
    return f"{total_minutes}min {suffix}"


def _build_detail_activity_subtitle(media_type, media_metadata, current_instance=None, play_stats=None):
    """Return a shared subtitle payload for tracked detail pages."""
    if not current_instance and not play_stats:
        return None

    media_metadata = media_metadata if isinstance(media_metadata, dict) else {}
    play_stats = play_stats if isinstance(play_stats, dict) else {}
    max_progress = media_metadata.get("max_progress")

    def build_progress_text(value, include_max=False):
        if value in (None, ""):
            return None
        progress_text = f"Progress: {value}"
        if include_max and max_progress:
            progress_text += f"/{max_progress}"
        return progress_text

    date_start = (
        play_stats.get("first_played")
        or getattr(current_instance, "subtitle_start_date", None)
        or getattr(current_instance, "aggregated_start_date", None)
        or getattr(current_instance, "start_date", None)
    )
    date_end = (
        play_stats.get("last_played")
        or getattr(current_instance, "subtitle_end_date", None)
        or getattr(current_instance, "aggregated_end_date", None)
        or getattr(current_instance, "end_date", None)
    )
    duration_text = None
    collapse_same_day = bool(play_stats.get("same_play_day"))

    if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        primary_text = build_progress_text(
            getattr(current_instance, "formatted_progress", None),
            include_max=True,
        )
        duration_text = _format_detail_activity_duration(
            play_stats.get("total_minutes"),
            "watched",
        )
    elif media_type == MediaTypes.MOVIE.value:
        total_plays = play_stats.get("total_plays")
        if not total_plays:
            return None
        primary_text = "Watched once" if total_plays == 1 else f"Watched {total_plays} times"
        duration_text = _format_detail_activity_duration(
            play_stats.get("total_minutes"),
            "watched",
        )
    elif media_type in (
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
        MediaTypes.MANGA.value,
    ):
        primary_text = build_progress_text(
            getattr(current_instance, "formatted_progress", None),
            include_max=True,
        )
    elif media_type == MediaTypes.GAME.value:
        progress_value = (
            getattr(current_instance, "formatted_aggregated_progress", None)
            if getattr(current_instance, "aggregated_progress", None) is not None
            else getattr(current_instance, "formatted_progress", None)
        )
        primary_text = build_progress_text(progress_value)
    elif media_type in (MediaTypes.BOARDGAME.value, MediaTypes.MUSIC.value):
        progress_value = (
            getattr(current_instance, "formatted_aggregated_progress", None)
            if getattr(current_instance, "aggregated_progress", None) is not None
            else getattr(current_instance, "formatted_progress", None)
        )
        primary_text = build_progress_text(progress_value)
        if media_type == MediaTypes.MUSIC.value:
            duration_text = _format_detail_activity_duration(
                play_stats.get("total_minutes"),
                "listened",
            )
    elif media_type == MediaTypes.PODCAST.value:
        total_plays = play_stats.get("total_plays")
        if max_progress and total_plays:
            primary_text = f"Progress: {total_plays}/{max_progress}"
        else:
            primary_text = build_progress_text(
                getattr(current_instance, "formatted_progress", None),
            )
        duration_text = _format_detail_activity_duration(
            play_stats.get("total_minutes"),
            "listened",
        )
    else:
        return None

    if not primary_text and not date_start and not date_end and not duration_text:
        return None

    return {
        "primary_text": primary_text,
        "date_start": date_start,
        "date_end": date_end,
        "duration_text": duration_text,
        "collapse_same_day": collapse_same_day,
    }


def _build_detail_activity_state(
    media_type,
    media_metadata,
    current_instance=None,
    user_medias=None,
    public_view=False,
):
    """Return the activity subtitle payload for tracked detail pages."""
    play_stats = None
    activity_subtitle = None
    user_medias = list(user_medias or [])

    if (
        not public_view
        and current_instance
        and user_medias
        and media_type
        in [
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
            MediaTypes.ANIME.value,
            MediaTypes.MANGA.value,
            MediaTypes.MOVIE.value,
            MediaTypes.MUSIC.value,
            MediaTypes.PODCAST.value,
            MediaTypes.TV.value,
        ]
    ):
        if media_type == MediaTypes.TV.value or (
            media_type == MediaTypes.ANIME.value
            and hasattr(current_instance, "seasons")
        ):
            # Calculate TV and grouped-anime play stats from watched episodes.
            total_minutes = 0
            episode_count = 0
            first_played = None
            last_played = None

            # Iterate through all seasons and episodes
            seasons = current_instance.seasons.all().select_related("item").prefetch_related(
                "episodes__item",
            )
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
        elif media_type == MediaTypes.ANIME.value:
            # Flat anime entries track episode progress directly on the media row.
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
            current_instance.subtitle_start_date = play_stats["first_played"]
            current_instance.subtitle_end_date = play_stats["last_played"]

            runtime_minutes = current_instance._get_known_item_runtime_minutes()
            total_progress = int(aggregated_progress or 0)
            if runtime_minutes and total_progress > 0:
                total_minutes = runtime_minutes * total_progress
                play_stats.update(
                    {
                        "total_minutes": total_minutes,
                        "total_hours": total_minutes // 60,
                        "total_minutes_remainder": total_minutes % 60,
                    },
                )
        else:
            # Generic non-TV calculation based on aggregated item activity.
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
            elif media_type == MediaTypes.MOVIE.value:
                total_plays = int(aggregated_progress or 0)
                play_stats["total_plays"] = total_plays

                range_start_candidates = []
                range_end_candidates = []
                for entry in user_medias:
                    range_start = entry.start_date or entry.end_date or entry.created_at
                    range_end = entry.end_date or entry.start_date or entry.created_at
                    if range_start:
                        range_start_candidates.append(range_start)
                    if range_end:
                        range_end_candidates.append(range_end)

                if range_start_candidates:
                    play_stats["first_played"] = min(range_start_candidates)
                if range_end_candidates:
                    play_stats["last_played"] = max(range_end_candidates)

                first_played = play_stats.get("first_played")
                last_played = play_stats.get("last_played")
                if first_played and last_played:
                    first_played_local = stats._localize_datetime(first_played)
                    last_played_local = stats._localize_datetime(last_played)
                    if first_played_local and last_played_local:
                        play_stats["same_play_day"] = (
                            first_played_local.date() == last_played_local.date()
                        )

                runtime_minutes = current_instance._get_known_item_runtime_minutes()
                if runtime_minutes and total_plays > 0:
                    total_minutes = runtime_minutes * total_plays
                    play_stats.update(
                        {
                            "total_minutes": total_minutes,
                            "total_hours": total_minutes // 60,
                            "total_minutes_remainder": total_minutes % 60,
                        },
                    )
            elif media_type == MediaTypes.MUSIC.value:
                total_plays = int(aggregated_progress or 0)
                play_stats["total_plays"] = total_plays

                runtime_minutes = current_instance._get_known_item_runtime_minutes()
                if runtime_minutes and total_plays > 0:
                    total_minutes = runtime_minutes * total_plays
                    play_stats.update(
                        {
                            "total_minutes": total_minutes,
                            "total_hours": total_minutes // 60,
                            "total_minutes_remainder": total_minutes % 60,
                        },
                    )
            elif media_type == MediaTypes.PODCAST.value:
                total_progress_seconds = int(aggregated_progress or 0)
                total_minutes = total_progress_seconds // 60
                completed_entries = sum(
                    1
                    for entry in user_medias
                    if entry.end_date or entry.status == Status.COMPLETED.value
                )
                play_stats.update(
                    {
                        "total_minutes": total_minutes,
                        "total_hours": total_minutes // 60,
                        "total_minutes_remainder": total_minutes % 60,
                        "total_plays": completed_entries or len(user_medias),
                    },
                )
            else:
                play_stats["total_plays"] = int(aggregated_progress or 0)

        activity_subtitle = _build_detail_activity_subtitle(
            media_type,
            media_metadata,
            current_instance,
            play_stats,
        )

    return play_stats, activity_subtitle


def _detail_episode_number_for_pagination(episode):
    """Return a display-friendly episode number from a detail episode payload."""
    if isinstance(episode, dict):
        episode_number = episode.get("episode_number")
    else:
        episode_number = getattr(episode, "episode_number", None)

    try:
        return int(episode_number) if episode_number is not None else None
    except (TypeError, ValueError):
        return episode_number


def _detail_episode_page_label(page_episodes, start_index, end_index):
    """Return a human-readable label for an episode page range."""
    if page_episodes:
        first_episode_number = _detail_episode_number_for_pagination(page_episodes[0])
        last_episode_number = _detail_episode_number_for_pagination(page_episodes[-1])
        if first_episode_number is not None and last_episode_number is not None:
            if first_episode_number == last_episode_number:
                return f"Episode {first_episode_number}"
            return f"Episodes {first_episode_number}-{last_episode_number}"

    display_start = start_index + 1
    display_end = end_index
    if display_start == display_end:
        return f"Episode {display_start}"
    return f"Episodes {display_start}-{display_end}"


def _paginate_detail_episodes(
    request,
    episodes,
    *,
    page_param="episode_page",
    per_page=DETAIL_EPISODES_PER_PAGE,
):
    """Slice long episode lists for detail pages and build the next batch link."""
    episode_list = list(episodes or [])
    if not episode_list:
        return episode_list, None

    paginator = Paginator(episode_list, per_page)

    try:
        requested_page = int(request.GET.get(page_param, 1))
    except (TypeError, ValueError):
        requested_page = 1
    if requested_page < 1:
        requested_page = 1

    try:
        page_obj = paginator.page(requested_page)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    load_more = None
    if page_obj.has_next():
        next_page_number = page_obj.next_page_number()
        next_start_index = (next_page_number - 1) * per_page
        next_end_index = min(next_start_index + per_page, paginator.count)
        next_query = request.GET.copy()
        next_query[page_param] = str(next_page_number)
        load_more = {
            "querystring": next_query.urlencode(),
            "label": _detail_episode_page_label(
                episode_list[next_start_index:next_end_index],
                next_start_index,
                next_end_index,
            ),
        }

    return list(page_obj.object_list), load_more


def _normalize_detail_episode_actions(episodes):
    """Ensure detail-page episode dicts default to enabled actions unless disabled."""
    normalized_episodes = []
    for episode in episodes or []:
        if isinstance(episode, dict):
            normalized_episode = dict(episode)
            normalized_episode.setdefault("actions_enabled", True)
            normalized_episodes.append(normalized_episode)
            continue
        normalized_episodes.append(episode)
    return normalized_episodes


def _detail_request_url(request, *, fragment: str | None = None) -> str:
    """Return the current detail URL with an optional fragment query override."""
    query = request.GET.copy()
    query.pop("fragment", None)
    if fragment:
        query["fragment"] = fragment
    querystring = query.urlencode()
    if not querystring:
        return request.path
    return f"{request.path}?{querystring}"


def _resolve_detail_tag_genres(media_metadata, item, fallback_genres=None):
    """Return detail-page genres sourced from metadata, request state, or stored item data."""
    genres = []
    if isinstance(media_metadata, dict):
        details = media_metadata.get("details")
        genres = stats._coerce_genre_list(
            media_metadata.get("genres")
            or (details.get("genres") if isinstance(details, dict) else None)
            or media_metadata.get("genre")
            or (details.get("genre") if isinstance(details, dict) else None),
        )
    if not genres and fallback_genres:
        genres = stats._coerce_genre_list(fallback_genres)
    if not genres and item is not None:
        genres = list(item.genres or [])
    return genres


def _build_detail_tag_sections(media_metadata, item, user, fallback_genres=None):
    """Return grouped genre and tag preview sections for the media detail action row."""
    sections = []

    genres = _resolve_detail_tag_genres(
        media_metadata,
        item,
        fallback_genres=fallback_genres,
    )

    if genres:
        sections.append(
            {
                "title": "Genres",
                "entries": [
                    {
                        "label": genre,
                        "chip_classes": "border-violet-400/18 bg-violet-500/[0.07] text-violet-100",
                    }
                    for genre in genres
                ],
            }
        )

    tag_names = []
    is_authenticated_user = item is not None and getattr(user, "is_authenticated", False)
    if is_authenticated_user:
        tag_names = list(
            ItemTag.objects.filter(item=item, tag__user=user)
            .select_related("tag")
            .order_by("tag__name")
            .values_list("tag__name", flat=True)
        )

    if is_authenticated_user:
        tag_section = {
            "title": "Tags",
            "entries": [
                {
                    "label": tag_name,
                    "chip_classes": "border-slate-400/18 bg-slate-500/[0.07] text-slate-100",
                }
                for tag_name in tag_names
            ],
        }
        if not tag_names:
            tag_section["empty_label"] = "Click to add tags"

        sections.append(
            tag_section,
        )

    return sections


def _parse_detail_tag_preview_genres(raw_value):
    """Return a normalized genre list from a serialized detail-tag preview payload."""
    if not raw_value:
        return []
    try:
        parsed_value = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return []
    return stats._coerce_genre_list(parsed_value)


def _user_tags_for_item(user, item):
    """Return the user's tags annotated with whether they apply to the item."""
    from django.db import models as db_models

    return (
        Tag.objects.filter(user=user)
        .annotate(
            has_tag=db_models.Exists(
                ItemTag.objects.filter(
                    tag_id=db_models.OuterRef("id"),
                    item=item,
                ),
            ),
        )
        .order_by("name")
    )


def _render_tag_modal_response(request, item, preview_genres):
    """Render the tag modal plus OOB preview refresh for the current item."""
    from django.template.loader import render_to_string

    modal_html = render_to_string(
        "app/components/fill_tags.html",
        {
            "item": item,
            "user_tags": _user_tags_for_item(request.user, item),
            "preview_genres_json": json.dumps(preview_genres),
        },
        request=request,
    )
    preview_html = render_to_string(
        "app/components/detail_tag_preview.html",
        {
            "preview_id": app_tags.component_id("tag-preview", item),
            "detail_tag_sections": _build_detail_tag_sections(
                {},
                item,
                request.user,
                fallback_genres=preview_genres,
            ),
            "swap_oob": True,
        },
        request=request,
    )
    return HttpResponse(modal_html + preview_html)


def _should_queue_game_lengths_refresh(detail_item):
    """Return whether a background game-length refresh should be queued."""
    if not detail_item:
        return False
    if detail_item.source != Sources.IGDB.value or detail_item.media_type != MediaTypes.GAME.value:
        return False
    if not detail_item.provider_game_lengths:
        return True
    return detail_item.provider_game_lengths_match == "igdb_fallback"


def _get_game_lengths_refresh_lock(detail_item, *, force=False, fetch_hltb=True):
    """Return an active game-length refresh lock, clearing stale or legacy values."""
    if not detail_item:
        return None

    lock_key = game_length_services.get_game_lengths_refresh_lock_key(
        detail_item.id,
        force=force,
        fetch_hltb=fetch_hltb,
    )
    refresh_lock = cache.get(lock_key)
    if refresh_lock is None:
        return None

    if refresh_lock is True or game_length_services.is_game_lengths_refresh_lock_stale(refresh_lock):
        cache.delete(lock_key)
        return None
    return refresh_lock


def _queue_game_lengths_refresh(detail_item, *, force=False, fetch_hltb=True):
    """Schedule a background game-length refresh once per debounce window."""
    if not detail_item:
        return False

    lock_key = game_length_services.get_game_lengths_refresh_lock_key(
        detail_item.id,
        force=force,
        fetch_hltb=fetch_hltb,
    )
    if _get_game_lengths_refresh_lock(detail_item, force=force, fetch_hltb=fetch_hltb) is not None:
        return False

    lock_payload = game_length_services.build_game_lengths_refresh_lock(
        force=force,
        fetch_hltb=fetch_hltb,
    )
    if not cache.add(
        lock_key,
        lock_payload,
        timeout=game_length_services.GAME_LENGTHS_REFRESH_TTL,
    ):
        if _get_game_lengths_refresh_lock(detail_item, force=force, fetch_hltb=fetch_hltb) is not None:
            return False
        if not cache.add(
            lock_key,
            lock_payload,
            timeout=game_length_services.GAME_LENGTHS_REFRESH_TTL,
        ):
            return False

    try:
        from app.tasks import refresh_item_game_lengths

        refresh_item_game_lengths.delay(
            detail_item.id,
            force=force,
            fetch_hltb=fetch_hltb,
        )
    except Exception:
        cache.delete(lock_key)
        logger.warning(
            "game_lengths_refresh_schedule_failed item_id=%s media_id=%s",
            detail_item.id,
            detail_item.media_id,
            exc_info=True,
        )
        return False
    return True


def _annotate_home_card_images(media_items):
    """Annotate home-card image overrides for media that need display fallbacks."""
    season_items = [
        media
        for media in media_items
        if getattr(getattr(media, "item", None), "media_type", None) == MediaTypes.SEASON.value
    ]
    if season_items:
        BasicMedia.objects._fix_missing_season_images(season_items)


@require_GET
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


@login_required
@require_POST
def update_metadata_provider_preference(request, source, media_type, media_id):
    """Persist a per-item metadata display-provider override."""
    provider = (request.POST.get("provider") or "").strip()
    return_url = helpers.normalize_navigation_url(request.POST.get("return_url"))

    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
    )
    lookup = {
        "media_id": media_id,
        "source": source,
        "media_type": tracking_media_type,
    }
    if metadata_resolution.is_grouped_anime_route(media_type, source=source):
        lookup["library_media_type"] = MediaTypes.ANIME.value

    item = get_object_or_404(Item, **lookup)
    allowed_providers = {
        choice.value
        for choice in metadata_resolution.available_metadata_provider_options(
            media_type,
            identity_provider=item.source,
        )
    }
    if provider not in allowed_providers:
        messages.error(request, "That metadata provider is not available for this title.")
    else:
        if (
            provider == Sources.MANUAL.value
            and custom_metadata.supports_custom_provider(media_type)
        ):
            current_display_metadata = _resolve_current_display_metadata_payload(
                user=request.user,
                item=item,
                media_type=media_type,
                media_id=media_id,
                source=source,
            )
            custom_metadata.snapshot_custom_metadata(item, current_display_metadata)

        MetadataProviderPreference.objects.update_or_create(
            user=request.user,
            item=item,
            defaults={"provider": provider},
        )
        messages.success(request, "Metadata provider updated.")

    if return_url and (
        return_url.startswith("/")
        or url_has_allowed_host_and_scheme(
            return_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return redirect(return_url)

    return redirect(
        "media_details",
        source=source,
        media_type=media_type,
        media_id=media_id,
        title=title if (title := item.get_display_title(request.user)) else "item",
    )


@login_required
@require_POST
def update_item_image(request, item_id):
    """Persist an image URL override for an item the user already tracks."""
    return_url = helpers.normalize_navigation_url(request.POST.get("return_url"))
    image_url = (request.POST.get("image_url") or "").strip()

    item = get_object_or_404(Item, id=item_id)
    media_model = apps.get_model("app", item.media_type)
    if not media_model.objects.filter(user=request.user, item=item).exists():
        messages.error(request, "You can only update images for items in your library.")
        return helpers.redirect_back(request)

    if not image_url:
        messages.error(request, "Enter an image URL to save.")
        return helpers.redirect_back(request)

    if item.image != image_url:
        item.image = image_url
        item.save(update_fields=["image"])
        messages.success(request, "Image URL updated.")
    else:
        messages.success(request, "Image URL already matches this item.")

    if return_url and (
        return_url.startswith("/")
        or url_has_allowed_host_and_scheme(
            return_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return redirect(return_url)

    return helpers.redirect_back(request)


@login_required
@require_POST
def update_manual_item_metadata(request, item_id):
    """Persist custom metadata overrides for a tracked manual item."""
    return_url = helpers.normalize_navigation_url(request.POST.get("return_url"))
    item = get_object_or_404(Item, id=item_id)
    media_model = apps.get_model("app", item.media_type)
    if not media_model.objects.filter(user=request.user, item=item).exists():
        messages.error(request, "You can only update metadata for items in your library.")
        return helpers.redirect_back(request)

    if not custom_metadata.supports_custom_metadata(item):
        messages.error(request, "Metadata overrides are not available for this item.")
        return helpers.redirect_back(request)

    form = custom_metadata.ManualMetadataForm(
        request.POST,
        item=item,
        prefix="metadata",
    )
    if form.is_valid():
        update_fields = form.save()
        if update_fields:
            messages.success(request, "Custom metadata updated.")
        else:
            messages.success(request, "Custom metadata already matches this item.")
    else:
        logger.error(form.errors.as_json())
        helpers.form_error_messages(form, request)

    if return_url and (
        return_url.startswith("/")
        or url_has_allowed_host_and_scheme(
            return_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return redirect(return_url)

    return helpers.redirect_back(request)


def _resolve_current_display_metadata_payload(
    *,
    user,
    item,
    media_type: str,
    media_id: str,
    source: str,
):
    """Return the metadata payload currently shown for a tracked item."""
    base_metadata = services.get_media_metadata(
        media_type,
        media_id,
        source,
    )
    current_provider = metadata_resolution.get_preferred_provider(
        user,
        item,
        media_type,
        requested_source=source,
    )
    if current_provider == Sources.MANUAL.value:
        return custom_metadata.build_custom_overlay_metadata(base_metadata, item)
    if current_provider == source:
        return base_metadata

    provider_media_id = metadata_resolution.resolve_provider_media_id(
        item,
        current_provider,
        route_media_type=media_type,
    )
    if not provider_media_id:
        return base_metadata

    return services.get_media_metadata(
        metadata_resolution.provider_route_media_type(
            media_type,
            current_provider,
        ),
        provider_media_id,
        current_provider,
    )


@login_required
@require_POST
def migrate_grouped_anime(request, source, media_type, media_id):
    """Explicitly migrate a flat MAL anime entry into grouped TV-style tracking."""
    return_url = helpers.normalize_navigation_url(request.POST.get("return_url"))
    provider = (request.POST.get("provider") or "").strip()

    item = get_object_or_404(
        Item,
        media_id=media_id,
        source=source,
        media_type=MediaTypes.ANIME.value,
    )
    allowed_providers = {Sources.TMDB.value, Sources.TVDB.value}
    if media_type != MediaTypes.ANIME.value or source != Sources.MAL.value:
        messages.error(request, "Only flat MAL anime can be migrated to grouped series.")
    elif provider not in allowed_providers:
        messages.error(request, "Choose TMDB or TVDB before migrating this anime.")
    else:
        try:
            result = anime_migration.migrate_flat_anime_to_grouped(
                request.user,
                item,
                provider,
            )
        except anime_migration.AnimeMigrationError as error:
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                "Migrated this anime into grouped series tracking.",
            )
            grouped_item = result.grouped_tv.item
            grouped_title = grouped_item.get_display_title(request.user) or "item"
            return redirect(
                "media_details",
                source=grouped_item.source,
                media_type=MediaTypes.ANIME.value,
                media_id=grouped_item.media_id,
                title=grouped_title,
            )

    if return_url and url_has_allowed_host_and_scheme(return_url, allowed_hosts=None):
        return redirect(return_url)

    return redirect(
        "media_details",
        source=source,
        media_type=media_type,
        media_id=media_id,
        title=item.get_display_title(request.user) or "item",
    )


def _build_missing_season_metadata(
    tv_metadata,
    media_id,
    source,
    season_number,
    episodes_in_db,
    *,
    season_item=None,
    show_item=None,
):
    """Build minimal season metadata from local items when provider data is missing."""
    tv_metadata = tv_metadata or {}
    episodes_by_number = defaultdict(list)
    episode_item_by_number = {}

    for episode in episodes_in_db:
        item = getattr(episode, "item", None)
        episode_number = getattr(item, "episode_number", None)
        if episode_number is None:
            continue
        episodes_by_number[episode_number].append(episode)
        if item is not None:
            episode_item_by_number.setdefault(episode_number, item)

    for episode_item in Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.EPISODE.value,
        season_number=season_number,
    ).order_by("episode_number", "id"):
        if episode_item.episode_number is None:
            continue
        episode_item_by_number.setdefault(episode_item.episode_number, episode_item)

    episode_numbers = sorted(set(episodes_by_number) | set(episode_item_by_number))
    fallback_episodes = []
    tv_image = helpers.first_real_image(
        getattr(show_item, "image", None),
        tv_metadata.get("image"),
        getattr(season_item, "image", None),
        default=settings.IMG_NONE,
    )
    show_title = (
        tv_metadata.get("title")
        or getattr(show_item, "title", "")
        or getattr(season_item, "title", "")
    )

    for episode_number in episode_numbers:
        history_entries = episodes_by_number.get(episode_number, [])
        episode_item = episode_item_by_number.get(episode_number)
        air_date = None
        runtime = None
        title = f"Episode {episode_number}"
        primary_image = getattr(episode_item, "image", None)

        if (
            helpers.has_real_image(primary_image)
            and helpers.has_real_image(tv_image)
            and primary_image == tv_image
        ):
            primary_image = None

        if episode_item:
            if episode_item.release_datetime:
                air_date = episode_item.release_datetime
            if (
                episode_item.runtime_minutes
                and episode_item.runtime_minutes < 999998
            ):
                runtime = tmdb.get_readable_duration(episode_item.runtime_minutes)
            if episode_item.title and episode_item.title != show_title:
                title = episode_item.title

        episode_image, image_source = helpers.resolve_image_with_fallback(
            primary_image,
            tv_image,
        )

        fallback_episodes.append(
            {
                "media_id": media_id,
                "media_type": MediaTypes.EPISODE.value,
                "source": source,
                "season_number": season_number,
                "episode_number": episode_number,
                "air_date": air_date,
                "image": episode_image,
                "image_source": image_source,
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

    synopsis = tv_metadata.get("synopsis") or ""
    if not synopsis and show_item is not None:
        synopsis = (show_item.manual_metadata or {}).get("synopsis") or ""

    return {
        "media_id": media_id,
        "source": source,
        "media_type": MediaTypes.SEASON.value,
        "title": show_title,
        "season_title": f"Season {season_number}",
        "image": helpers.first_real_image(
            getattr(season_item, "image", None),
            tv_image,
            default=settings.IMG_NONE,
        ),
        "season_number": season_number,
        "synopsis": synopsis or "No synopsis available.",
        "genres": tv_metadata.get("genres") or getattr(show_item, "genres", []) or [],
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


def _get_local_show_item(media_id, source):
    """Return the locally stored show item for a season route, if available."""
    show_item = Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.TV.value,
    ).first()
    if show_item is not None:
        return show_item
    return Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.ANIME.value,
    ).first()


def _build_local_related_seasons(media_id, source, show_title, show_image):
    """Return locally persisted season rows for the season dropdown."""
    episode_stats = {
        row["season_number"]: row
        for row in Item.objects.filter(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.EPISODE.value,
            season_number__isnull=False,
        )
        .values("season_number")
        .annotate(
            max_progress=Max("episode_number"),
            first_air_date=Min("release_datetime"),
        )
    }

    related_seasons = []
    for season_item in Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.SEASON.value,
        season_number__isnull=False,
    ).order_by("season_number", "id"):
        season_number = season_item.season_number
        if season_number is None:
            continue

        season_title = "Specials" if season_number == 0 else f"Season {season_number}"
        season_stats = episode_stats.get(season_number, {})
        related_seasons.append(
            {
                "source": source,
                "media_type": MediaTypes.SEASON.value,
                "media_id": media_id,
                "title": show_title,
                "season_title": season_title,
                "season_header_title": season_title,
                "season_number": season_number,
                "image": helpers.first_real_image(
                    season_item.image,
                    show_image,
                    default=settings.IMG_NONE,
                ),
                "max_progress": season_stats.get("max_progress") or 0,
                "first_air_date": season_stats.get("first_air_date"),
            },
        )

    return related_seasons


def _build_local_tv_with_seasons_metadata(
    media_id,
    source,
    *,
    tv_metadata=None,
    show_item=None,
    season_item=None,
):
    """Return TV metadata enriched with locally persisted season rows."""
    tv_metadata = dict(tv_metadata or {})
    show_item = show_item or _get_local_show_item(media_id, source)
    show_title = (
        tv_metadata.get("title")
        or getattr(show_item, "title", "")
        or getattr(season_item, "title", "")
    )
    show_image = helpers.first_real_image(
        getattr(show_item, "image", None),
        tv_metadata.get("image"),
        getattr(season_item, "image", None),
        default=settings.IMG_NONE,
    )

    related = dict(tv_metadata.get("related") or {})
    provider_related_seasons = []
    seen_season_numbers = set()
    for season in related.get("seasons") or []:
        if not isinstance(season, dict):
            continue
        season_copy = dict(season)
        raw_season_number = season_copy.get("season_number")
        try:
            normalized_season_number = (
                int(raw_season_number) if raw_season_number is not None else None
            )
        except (TypeError, ValueError):
            normalized_season_number = None
        season_copy["season_number"] = normalized_season_number
        if normalized_season_number is not None:
            season_title = (
                "Specials"
                if normalized_season_number == 0
                else f"Season {normalized_season_number}"
            )
            season_copy.setdefault("title", show_title)
            season_copy.setdefault("season_title", season_title)
            season_copy.setdefault(
                "season_header_title",
                season_copy.get("season_title") or season_title,
            )
            season_copy.setdefault("media_id", media_id)
            season_copy.setdefault("media_type", MediaTypes.SEASON.value)
            season_copy.setdefault("source", source)
            season_copy.setdefault("image", show_image)
        provider_related_seasons.append(season_copy)
        seen_season_numbers.add(normalized_season_number)

    local_related_seasons = _build_local_related_seasons(
        media_id,
        source,
        show_title,
        show_image,
    )
    for local_season in local_related_seasons:
        if local_season["season_number"] not in seen_season_numbers:
            provider_related_seasons.append(local_season)

    provider_related_seasons.sort(
        key=lambda season: (
            season.get("season_number") is None,
            season.get("season_number")
            if season.get("season_number") is not None
            else 999999,
        ),
    )
    related["seasons"] = provider_related_seasons

    provider_external_ids = getattr(show_item, "provider_external_ids", {}) or {}
    tv_metadata.update(
        {
            "media_id": media_id,
            "source": source,
            "media_type": MediaTypes.TV.value,
            "title": show_title,
            "image": show_image,
            "synopsis": tv_metadata.get("synopsis")
            or ((show_item.manual_metadata or {}).get("synopsis") if show_item else "")
            or "",
            "genres": tv_metadata.get("genres")
            or getattr(show_item, "genres", [])
            or [],
            "related": related,
            "tvdb_id": (
                tv_metadata.get("tvdb_id")
                or provider_external_ids.get("tvdb_id")
            ),
        },
    )
    tv_metadata.setdefault("source_url", "")
    tv_metadata.setdefault("external_links", {})
    return tv_metadata


def _save_provider_metadata_status(item, status):
    """Persist provider metadata status when it changes."""
    if item is None or item.provider_metadata_status == status:
        return item
    item.provider_metadata_status = status
    item.save(update_fields=["provider_metadata_status"])
    return item


def _flat_anime_episode_preview_candidates(user, metadata_resolution_result=None):
    """Return grouped providers to try for flat MAL anime episode previews."""
    candidates = []

    def add_candidate(provider):
        if (
            provider in metadata_resolution.GROUPED_ANIME_PROVIDERS
            and metadata_resolution.provider_is_enabled(provider)
            and provider not in candidates
        ):
            candidates.append(provider)

    if metadata_resolution_result is not None:
        add_candidate(metadata_resolution_result.display_provider)

    if user and getattr(user, "is_authenticated", False):
        add_candidate(
            metadata_resolution.metadata_default_source(
                user,
                MediaTypes.ANIME.value,
            ),
        )

    add_candidate(Sources.TVDB.value)
    add_candidate(Sources.TMDB.value)
    return candidates


def _flat_anime_preview_season_numbers(
    grouped_series_metadata,
    grouped_preview_target,
):
    """Return grouped season numbers needed for a flat anime episode slice."""
    if not isinstance(grouped_preview_target, dict):
        return []

    season_number = grouped_preview_target.get("season_number")
    try:
        season_number = int(season_number) if season_number is not None else None
    except (TypeError, ValueError):
        season_number = None

    if season_number is not None and season_number >= 0:
        return [season_number]

    related = grouped_series_metadata.get("related") if isinstance(grouped_series_metadata, dict) else {}
    seasons = related.get("seasons") if isinstance(related, dict) else []
    target_total = grouped_preview_target.get("episode_total")
    try:
        target_total = int(target_total) if target_total is not None else None
    except (TypeError, ValueError):
        target_total = None
    episode_offset = grouped_preview_target.get("episode_offset") or 0
    try:
        episode_offset = int(episode_offset)
    except (TypeError, ValueError):
        episode_offset = 0

    sortable_seasons = []
    for season in seasons:
        if not isinstance(season, dict):
            continue
        raw_number = season.get("season_number")
        try:
            normalized_number = int(raw_number)
        except (TypeError, ValueError):
            continue
        sortable_seasons.append((normalized_number, season))

    sortable_seasons.sort(key=lambda pair: pair[0])

    season_numbers = []
    covered_episodes = 0
    for normalized_number, season in sortable_seasons:
        if normalized_number < 0 or normalized_number == 0:
            continue
        season_numbers.append(normalized_number)
        episode_count = (
            season.get("episode_count")
            or (season.get("details") or {}).get("episodes")
            or season.get("max_progress")
        )
        try:
            episode_count = int(episode_count)
        except (TypeError, ValueError):
            episode_count = None
        if episode_count is not None:
            covered_episodes += episode_count
        if (
            target_total is not None
            and episode_count is not None
            and covered_episodes >= episode_offset + target_total
        ):
            break

    if season_numbers:
        return season_numbers

    if any(number == 0 for number, _season in sortable_seasons):
        return [0]
    return []


def _flat_anime_preview_episode_rows(grouped_preview, grouped_preview_target):
    """Return mapped episode rows for a flat anime preview."""
    if not isinstance(grouped_preview, dict) or not isinstance(grouped_preview_target, dict):
        return []

    target_total = grouped_preview_target.get("episode_total")
    try:
        target_total = int(target_total) if target_total is not None else None
    except (TypeError, ValueError):
        target_total = None
    episode_offset = grouped_preview_target.get("episode_offset") or 0
    try:
        episode_offset = int(episode_offset)
    except (TypeError, ValueError):
        episode_offset = 0
    target_season = grouped_preview_target.get("season_number")
    try:
        target_season = int(target_season) if target_season is not None else None
    except (TypeError, ValueError):
        target_season = None

    def season_rows(season_number):
        season_payload = grouped_preview.get(f"season/{season_number}")
        if not isinstance(season_payload, dict):
            return []
        season_title = season_payload.get("season_title") or (
            "Specials" if season_number == 0 else f"Season {season_number}"
        )
        rows = []
        for raw_episode in season_payload.get("episodes") or []:
            provider_episode_number = raw_episode.get("episode_number")
            if provider_episode_number is None:
                continue
            rows.append(
                {
                    "season_number": season_number,
                    "season_title": season_title,
                    "provider_episode_number": provider_episode_number,
                    "raw_episode": raw_episode,
                },
            )
        return rows

    ordered_rows = []
    if target_season is not None and target_season >= 0:
        ordered_rows.extend(season_rows(target_season))
    else:
        season_numbers = _flat_anime_preview_season_numbers(
            grouped_preview,
            grouped_preview_target,
        )
        if not season_numbers:
            season_numbers = sorted(
                {
                    int(key.split("/", 1)[1])
                    for key, value in grouped_preview.items()
                    if key.startswith("season/")
                    and isinstance(value, dict)
                    and key.split("/", 1)[1].lstrip("-").isdigit()
                    and int(key.split("/", 1)[1]) >= 0
                },
            )
        for season_number in season_numbers:
            ordered_rows.extend(season_rows(season_number))

    if episode_offset > 0:
        ordered_rows = ordered_rows[episode_offset:]
    if target_total is not None:
        ordered_rows = ordered_rows[:target_total]

    mapped_rows = []
    for mapped_episode_number, row in enumerate(ordered_rows, start=1):
        mapped_rows.append(
            {
                **row,
                "mapped_episode_number": mapped_episode_number,
            },
        )
    return mapped_rows


def _build_flat_anime_episode_preview(
    request,
    *,
    detail_item,
    media_id,
    base_metadata,
    metadata_resolution_result=None,
    retry_max_retries: int | None = None,
    on_persistence_deferred=None,
):
    """Return a read-only mapped episode slice for flat MAL anime details."""
    if not isinstance(base_metadata, dict):
        return None

    identity_source = detail_item.source if detail_item else base_metadata.get("source")
    identity_media_type = (
        detail_item.media_type if detail_item else base_metadata.get("media_type")
    )
    if identity_source != Sources.MAL.value or identity_media_type != MediaTypes.ANIME.value:
        return None

    if base_metadata.get("episodes"):
        return None

    provider = None
    provider_media_id = None
    grouped_preview = None
    grouped_preview_target = None

    if metadata_resolution_result is not None:
        provider = metadata_resolution_result.display_provider
        provider_media_id = metadata_resolution_result.provider_media_id
        grouped_preview = metadata_resolution_result.grouped_preview
        grouped_preview_target = metadata_resolution_result.grouped_preview_target

    if (
        provider not in metadata_resolution.GROUPED_ANIME_PROVIDERS
        or not provider_media_id
        or not isinstance(grouped_preview, dict)
        or not isinstance(grouped_preview_target, dict)
    ):
        provider = None
        provider_media_id = None
        grouped_preview = None
        grouped_preview_target = None

        for candidate in _flat_anime_episode_preview_candidates(
            request.user if request.user.is_authenticated else None,
            metadata_resolution_result,
        ):
            candidate_media_id = (
                metadata_resolution.resolve_provider_media_id(
                    detail_item,
                    candidate,
                    route_media_type=MediaTypes.ANIME.value,
                    persistence_mode="best_effort",
                    retry_max_retries=retry_max_retries,
                    on_deferred=on_persistence_deferred,
                )
                if detail_item is not None
                else anime_mapping.resolve_provider_series_id(media_id, candidate)
            )
            if not candidate_media_id:
                continue

            preview_target = metadata_resolution._grouped_preview_target(
                item=detail_item,
                media_id=media_id,
                provider=candidate,
                provider_media_id=candidate_media_id,
                base_metadata=base_metadata,
                grouped_preview=None,
            )
            if not isinstance(preview_target, dict):
                continue

            season_number = preview_target.get("season_number")
            if season_number is None:
                continue

            season_numbers = _flat_anime_preview_season_numbers(
                {},
                preview_target,
            )
            if season_numbers:
                preview_payload = services.get_media_metadata(
                    "tv_with_seasons",
                    candidate_media_id,
                    candidate,
                    season_numbers,
                )
            else:
                grouped_series_metadata = services.get_media_metadata(
                    MediaTypes.ANIME.value,
                    candidate_media_id,
                    candidate,
                )
                season_numbers = _flat_anime_preview_season_numbers(
                    grouped_series_metadata,
                    preview_target,
                )
                if not season_numbers:
                    continue
                preview_payload = services.get_media_metadata(
                    "tv_with_seasons",
                    candidate_media_id,
                    candidate,
                    season_numbers,
                )

            preview_payload = metadata_resolution._enrich_grouped_preview(
                preview_payload,
            )
            if not any(
                isinstance(preview_payload.get(f"season/{number}"), dict)
                for number in season_numbers
            ):
                continue
            preview_target = metadata_resolution._grouped_preview_target(
                item=detail_item,
                media_id=media_id,
                provider=candidate,
                provider_media_id=candidate_media_id,
                base_metadata=base_metadata,
                grouped_preview=preview_payload,
            )
            if not isinstance(preview_target, dict):
                continue

            provider = candidate
            provider_media_id = candidate_media_id
            grouped_preview = preview_payload
            grouped_preview_target = preview_target
            break

    if not isinstance(grouped_preview_target, dict) or not isinstance(grouped_preview, dict):
        return None

    preview_rows = _flat_anime_preview_episode_rows(
        grouped_preview,
        grouped_preview_target,
    )
    if not preview_rows:
        return None

    history_by_episode_key = defaultdict(list)
    item_by_episode_key = {}
    collection_entry_by_episode_key = {}
    rating_season_id_by_episode_key = {}
    preview_episode_keys = {
        (row["season_number"], row["provider_episode_number"])
        for row in preview_rows
    }
    preview_season_numbers = sorted({row["season_number"] for row in preview_rows})

    if request.user.is_authenticated:
        tracked_episodes = list(
            Episode.objects.filter(
                related_season__related_tv__user=request.user,
                item__media_id=provider_media_id,
                item__source=provider,
                item__media_type=MediaTypes.EPISODE.value,
                item__season_number__in=preview_season_numbers,
            )
            .select_related("item")
            .order_by("-end_date", "-id"),
        )

        for tracked_episode in tracked_episodes:
            episode_item = getattr(tracked_episode, "item", None)
            episode_key = (
                getattr(episode_item, "season_number", None),
                getattr(episode_item, "episode_number", None),
            )
            if None in episode_key or episode_key not in preview_episode_keys:
                continue
            history_by_episode_key[episode_key].append(tracked_episode)
            item_by_episode_key.setdefault(episode_key, episode_item)
            rating_season_id_by_episode_key.setdefault(
                episode_key,
                tracked_episode.related_season_id,
            )

        if item_by_episode_key:
            collection_entries = (
                CollectionEntry.objects.filter(
                    user=request.user,
                    item_id__in=[item.id for item in item_by_episode_key.values()],
                )
                .select_related("item")
                .order_by("-collected_at", "-id")
            )
            for entry in collection_entries:
                episode_key = (
                    entry.item.season_number,
                    entry.item.episode_number,
                )
                if (
                    None not in episode_key
                    and episode_key in preview_episode_keys
                    and episode_key not in collection_entry_by_episode_key
                ):
                    collection_entry_by_episode_key[episode_key] = entry

    episodes = []
    episode_backdrop = None
    tvdb_episode_images_by_season = {}
    if provider == Sources.TMDB.value:
        episode_backdrop = helpers.get_tmdb_backdrop_image(
            MediaTypes.TV.value,
            provider_media_id,
        )

    for row in preview_rows:
        raw_episode = row["raw_episode"]
        season_number = row["season_number"]
        provider_episode_number = row["provider_episode_number"]
        episode_key = (season_number, provider_episode_number)
        episode_number = row["mapped_episode_number"]
        tvdb_episode_image = None

        if provider == Sources.TMDB.value:
            if season_number not in tvdb_episode_images_by_season:
                season_payload = grouped_preview.get(f"season/{season_number}", {})
                season_tvdb_id = None
                if isinstance(season_payload, dict):
                    season_tvdb_id = season_payload.get("tvdb_id")
                if not season_tvdb_id and isinstance(grouped_preview, dict):
                    season_tvdb_id = grouped_preview.get("tvdb_id")
                tvdb_episode_images_by_season[season_number] = (
                    tmdb.get_tvdb_episode_image_map(
                        season_tvdb_id,
                        season_number,
                        tmdb_media_id=provider_media_id,
                    )
                )
            tvdb_episode_image = tvdb_episode_images_by_season.get(
                season_number,
                {},
            ).get(str(provider_episode_number))

        image, image_source = helpers.resolve_image_with_fallback(
            tmdb.get_image_url(raw_episode["still_path"])
            if raw_episode.get("still_path")
            else None,
            tvdb_episode_image,
            helpers.first_real_image(raw_episode.get("image"), default=None),
            episode_backdrop,
        )

        runtime_value = raw_episode.get("runtime")
        runtime = (
            tmdb.get_readable_duration(runtime_value)
            if isinstance(runtime_value, (int, float)) and runtime_value > 0
            else runtime_value
        )

        episodes.append(
            {
                "media_id": provider_media_id,
                "media_type": MediaTypes.EPISODE.value,
                "source": provider,
                "season_number": season_number,
                "episode_number": provider_episode_number,
                "display_episode_number": episode_number,
                "provider_episode_number": provider_episode_number,
                "season_title": row["season_title"],
                "air_date": bulk_episode_tracking.coerce_episode_datetime(
                    raw_episode.get("air_date"),
                ),
                "image": image,
                "image_source": image_source,
                "title": raw_episode.get("name")
                or raw_episode.get("title")
                or f"Episode {episode_number}",
                "overview": raw_episode.get("overview") or "",
                "runtime": runtime,
                "history": history_by_episode_key.get(episode_key, []),
                "item": item_by_episode_key.get(episode_key),
                "collection_entry": collection_entry_by_episode_key.get(
                    episode_key,
                ),
                "rating_season_id": rating_season_id_by_episode_key.get(
                    episode_key,
                ),
                "library_media_type": MediaTypes.ANIME.value,
            },
        )

    return episodes or None


@login_not_required
@require_GET
def season_details(
    request, source, media_id, title, season_number,
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

    context = {
        "user": request.user,
        "media": season_metadata,
        "tv": tv_with_seasons_metadata,
        "media_type": MediaTypes.SEASON.value,
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


@login_required
@require_POST
def update_episode_score(request, season_id, episode_number):
    """Update the user's score for a specific episode."""
    season = get_object_or_404(Season, id=season_id, user=request.user)

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

    episodes = Episode.objects.filter(
        related_season=season,
        item__episode_number=episode_number,
    )

    if toggle and score is not None:
        existing = episodes.values_list("score", flat=True).first()
        if existing == score:
            score = None

    episodes.update(score=score)
    logger.info(
        "Episode S%sE%s score updated to %s for user %s",
        season.item.season_number,
        episode_number,
        score,
        request.user,
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

    history_day_keys = _collect_music_history_day_keys_for_artist(request.user, artist)
    if history_day_keys:
        history_cache.invalidate_history_days(
            request.user.id,
            day_keys=history_day_keys,
            logging_styles=("sessions", "repeats"),
            reason="artist_score_change",
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

    history_day_keys = _collect_music_history_day_keys_for_album_ids(
        request.user,
        [album.id],
    )
    if history_day_keys:
        history_cache.invalidate_history_days(
            request.user.id,
            day_keys=history_day_keys,
            logging_styles=("sessions", "repeats"),
            reason="album_score_change",
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
    def _sync_redirect_response():
        if request.headers.get("HX-Request"):
            return HttpResponse(
                status=204,
                headers={
                    "HX-Redirect": request.POST["next"],
                },
            )
        return helpers.redirect_back(request)

    def _restore_cached_metadata(cache_key, cached_metadata, cached_ttl):
        if cached_metadata is None:
            return

        timeout = (
            cached_ttl
            if isinstance(cached_ttl, int | float) and cached_ttl > 0
            else settings.CACHE_TIMEOUT
        )
        cache.set(cache_key, cached_metadata, timeout=timeout)

    if source == Sources.MANUAL.value:
        msg = "Manual items cannot be synced."
        messages.error(request, msg)
        return HttpResponse(
            msg,
            status=400,
            headers={"HX-Redirect": request.POST.get("next", "/")},
        )

    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
    )
    cache_key = f"{source}_{tracking_media_type}_{media_id}"
    if media_type == MediaTypes.SEASON.value:
        cache_key += f"_{season_number}"

    cached_metadata = cache.get(cache_key)
    ttl = cache.ttl(cache_key)
    logger.debug("%s - Cache TTL for: %s", cache_key, ttl)

    if ttl is not None and ttl > (settings.CACHE_TIMEOUT - 3):
        msg = "The data was recently synced, please wait a few seconds."
        messages.error(request, msg)
        logger.error(msg)
    else:
        deleted = cache.delete(cache_key)
        logger.debug("%s - Old cache deleted: %s", cache_key, deleted)

        try:
            metadata = services.get_media_metadata(
                media_type,
                media_id,
                source,
                [season_number],
            )
        except (requests.exceptions.RequestException, services.ProviderAPIError) as exc:
            _restore_cached_metadata(cache_key, cached_metadata, ttl)
            provider_label = Sources(source).label
            logger.warning(
                "metadata_manual_refresh_failed cache_key=%s media_id=%s source=%s error=%s",
                cache_key,
                media_id,
                source,
                exception_summary(exc),
            )
            if isinstance(exc, services.ProviderAPIError):
                msg = str(exc)
            else:
                msg = (
                    f"Could not sync with {provider_label} right now because the provider "
                    "could not be reached."
                )
            if cached_metadata is not None:
                msg += " Cached data has been kept."
            messages.error(request, msg)
            return _sync_redirect_response()
        
        # Extract number_of_pages for books
        number_of_pages = None
        if media_type == MediaTypes.BOOK.value:
            number_of_pages = metadata.get("max_progress") or metadata.get("details", {}).get("number_of_pages")
        
        item, _ = Item.objects.update_or_create(
            media_id=media_id,
            source=source,
            media_type=tracking_media_type,
            season_number=season_number,
            defaults={
                **Item.title_fields_from_metadata(metadata),
                "library_media_type": (
                    metadata.get("library_media_type")
                    or media_type
                ),
                "image": metadata["image"],
                "number_of_pages": number_of_pages,
            },
        )
        
        # Update number_of_pages if it wasn't set but we have it now
        if media_type == MediaTypes.BOOK.value and not item.number_of_pages and number_of_pages:
            item.number_of_pages = number_of_pages
            item.save(update_fields=["number_of_pages"])

        metadata_update_fields = metadata_utils.apply_item_genres(
            item,
            metadata_utils.extract_metadata_genres(metadata),
        )
        metadata_update_fields.extend(metadata_utils.apply_item_metadata(item, metadata))
        if metadata_update_fields:
            metadata_update_fields = list(dict.fromkeys(metadata_update_fields))
            item.metadata_fetched_at = timezone.now()
            metadata_update_fields.append("metadata_fetched_at")
            item.save(update_fields=metadata_update_fields)

        if source == Sources.IGDB.value and media_type == MediaTypes.GAME.value:
            try:
                game_length_services.refresh_game_lengths(
                    item,
                    igdb_metadata=metadata,
                    force=True,
                    fetch_hltb=True,
                )
            except Exception as exc:
                logger.warning(
                    "game_lengths_manual_refresh_failed item_id=%s media_id=%s error=%s",
                    item.id,
                    item.media_id,
                    exception_summary(exc),
                )
                messages.warning(
                    request,
                    "Game length metadata could not be refreshed. Cached data will be used if available.",
                )

        metadata_resolution.upsert_provider_links(
            item,
            metadata,
            provider=source,
            provider_media_type=tracking_media_type,
            season_number=season_number,
        )

        if trakt_popularity_service.supports_route_media_type(media_type):
            try:
                trakt_popularity_service.refresh_trakt_popularity(
                    item,
                    route_media_type=media_type,
                    force=True,
                )
            except Exception as exc:
                logger.warning(
                    "trakt_popularity_manual_refresh_failed item_id=%s media_id=%s error=%s",
                    item.id,
                    item.media_id,
                    exception_summary(exc),
                )
                messages.warning(
                    request,
                    "Trakt popularity metadata could not be refreshed. Cached data will be used if available.",
                )

        if source == Sources.TMDB.value and tracking_media_type in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
        ):
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
                    title_fields = Item.title_fields_from_metadata(metadata)
                    episode_item.title = title_fields["title"]
                    episode_item.original_title = title_fields["original_title"]
                    episode_item.localized_title = title_fields["localized_title"]
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
                    [
                        "title",
                        "original_title",
                        "localized_title",
                        "image",
                        "release_datetime",
                        "runtime_minutes",
                    ],
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

    return _sync_redirect_response()


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
    
    logger.info("Attempting to sync Plex rating for media_type=%s", media_type)
    
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
        logger.debug("Using cached Plex rating key for rating sync")
    else:
        # Search for item in Plex library
        try:
            resources = plex_api.list_resources(plex_account.plex_token)
        except Exception as exc:
            logger.debug(
                "Failed to list Plex resources for rating sync: %s",
                exception_summary(exc),
            )
            return
        
        # Get sections
        sections = plex_account.sections or []
        if not sections:
            try:
                sections = plex_api.list_sections(plex_account.plex_token)
            except Exception as exc:
                logger.debug(
                    "Failed to list Plex sections for rating sync: %s",
                    exception_summary(exc),
                )
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
                        logger.info("Found matching Plex item for rating sync")
                        break
                
                if rating_key:
                    break
            except Exception as exc:
                logger.debug(
                    "Failed to search Plex section for rating sync: %s",
                    exception_summary(exc),
                )
                continue
    
    if not rating_key or not plex_uri:
        logger.debug("Could not find Plex rating key for rating sync")
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
        logger.warning(
            "Failed to fetch Plex metadata for rating sync: %s",
            exception_summary(exc),
        )
        # Try HTTPS if HTTP failed, or vice versa
        if plex_uri.startswith("http://"):
            https_uri = plex_uri.replace("http://", "https://")
            logger.debug("Retrying Plex rating sync with HTTPS: %s", safe_url(https_uri))
            try:
                plex_metadata = plex_api.fetch_metadata(
                    plex_account.plex_token,
                    https_uri,
                    str(rating_key),
                    timeout=30,
                )
            except Exception as https_exc:
                logger.debug(
                    "HTTPS retry also failed during Plex rating sync: %s",
                    exception_summary(https_exc),
                )
                return
        elif plex_uri.startswith("https://"):
            http_uri = plex_uri.replace("https://", "http://")
            logger.debug("Retrying Plex rating sync with HTTP: %s", safe_url(http_uri))
            try:
                plex_metadata = plex_api.fetch_metadata(
                    plex_account.plex_token,
                    http_uri,
                    str(rating_key),
                    timeout=30,
                )
            except Exception as http_exc:
                logger.debug(
                    "HTTP retry also failed during Plex rating sync: %s",
                    exception_summary(http_exc),
                )
                return
        else:
            return
    
    if not plex_metadata:
        logger.debug("No Plex metadata returned for rating sync")
        return
    
    user_rating = plex_metadata.get("userRating")
    if user_rating is None:
        logger.debug("No userRating found in Plex metadata for rating sync")
        return
    
    # Check if this is a rating removal event (-1.0)
    try:
        rating_float = float(user_rating)
        if rating_float == -1.0:
            logger.info("Detected Plex rating removal event for media_type=%s", media_type)
            # Remove rating from existing instances only
            if media_type == MediaTypes.MOVIE.value:
                from app.models import Movie
                movie_instance = Movie.objects.filter(item=item, user=request.user).first()
                if movie_instance:
                    movie_instance.score = None
                    movie_instance.save(update_fields=["score"])
                    logger.info("Removed movie rating from Plex sync")
                else:
                    logger.debug("No movie instance found to remove Plex rating")
            elif media_type == MediaTypes.TV.value:
                from app.models import TV
                tv_instance = TV.objects.filter(item=item, user=request.user).first()
                if tv_instance:
                    tv_instance.score = None
                    tv_instance.save(update_fields=["score"])
                    logger.info("Removed TV rating from Plex sync")
                else:
                    logger.debug("No TV instance found to remove Plex rating")
            return
    except (TypeError, ValueError):
        logger.debug("Invalid rating value returned during Plex sync")
        return
    
    # Normalize rating (Plex userRating is typically 0-10, Yamtrack uses 0-10)
    if rating_float <= 10:
        normalized_rating = rating_float
    elif rating_float <= 100:
        normalized_rating = rating_float / 10
    else:
        logger.debug("Rating from Plex sync was out of expected range")
        return
    
    normalized_rating = round(normalized_rating, 1)
    if normalized_rating < 0 or normalized_rating > 10:
        logger.debug("Normalized Plex rating was out of range")
        return
    
    if normalized_rating is None:
        logger.debug("Invalid normalized rating returned during Plex sync")
        return
    
    # Apply rating to media instance
    if media_type == MediaTypes.MOVIE.value:
        from app.models import Movie
        movie_instance = Movie.objects.filter(item=item, user=request.user).first()
        if movie_instance:
            movie_instance.score = normalized_rating
            movie_instance.save(update_fields=["score"])
            logger.info("Synced Plex movie rating")
        else:
            # Create movie instance if it doesn't exist
            Movie.objects.create(
                item=item,
                user=request.user,
                status=Status.COMPLETED.value,
                progress=1,
                score=normalized_rating,
            )
            logger.info("Created movie instance from Plex rating sync")
    elif media_type == MediaTypes.TV.value:
        from app.models import TV
        tv_instance = TV.objects.filter(item=item, user=request.user).first()
        if tv_instance:
            tv_instance.score = normalized_rating
            tv_instance.save(update_fields=["score"])
            logger.info("Synced Plex TV rating")
        else:
            # Create TV instance if it doesn't exist
            TV.objects.create(
                item=item,
                user=request.user,
                status=Status.IN_PROGRESS.value,
                score=normalized_rating,
            )
            logger.info("Created TV instance from Plex rating sync")


def _bulk_episode_form_initial_data(return_url, domain):
    """Return initial form values for the bulk episode-play tab."""
    now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
    date_initial = now if settings.TRACK_TIME else now.date()

    return {
        "media_id": domain["tracking_media_id"],
        "source": domain["tracking_source"],
        "media_type": domain["route_media_type"],
        "identity_media_type": domain.get("identity_media_type") or "",
        "library_media_type": domain.get("library_media_type") or "",
        "instance_id": "",
        "return_url": return_url,
        "context_kind": domain.get("context_kind") or "",
        "context_id": domain.get("context_id") or "",
        "first_season_number": domain["default_first"]["season_number"],
        "first_episode_number": domain["default_first"]["episode_number"],
        "last_season_number": domain["default_last"]["season_number"],
        "last_episode_number": domain["default_last"]["episode_number"],
        "write_mode": BulkEpisodeTrackForm.WRITE_MODE_ADD,
        "distribution_mode": BulkEpisodeTrackForm.DISTRIBUTION_MODE_AIR_DATE,
        "start_date": date_initial,
        "end_date": date_initial,
    }


def _episode_domain_template_payload(domain):
    """Return the JSON-friendly episode selector payload for Alpine."""
    if not domain:
        return None

    season_episode_map = {}
    for season_number, episodes in domain["season_episode_map"].items():
        season_episode_map[str(season_number)] = [
            {
                "order": episode["order"],
                "season_number": episode["season_number"],
                "episode_number": episode["episode_number"],
                "episode_title": episode["episode_title"],
                "selector_label": episode.get("selector_label", ""),
                "existing_play_count": episode["existing_play_count"],
                "air_date": episode["air_date"].isoformat() if episode["air_date"] else "",
            }
            for episode in episodes
        ]

    return {
        "seasons": domain["seasons"],
        "seasonEpisodeMap": season_episode_map,
        "defaultFirst": domain["default_first"],
        "defaultLast": domain["default_last"],
        "lockedSeasonNumber": domain["locked_season_number"],
        "hideSeasonSelectors": domain.get("hide_season_selectors", False),
        "firstSelectionTitle": domain.get("first_selection_title", ""),
        "lastSelectionTitle": domain.get("last_selection_title", ""),
        "seasonFieldLabel": domain.get("season_field_label", ""),
        "episodeFieldLabel": domain.get("episode_field_label", ""),
        "selectionNoun": domain.get("selection_noun", ""),
        "selectionNounPlural": domain.get("selection_noun_plural", ""),
        "distributionTargetLabel": domain.get("distribution_target_label", ""),
        "missingTargetDateFallbackDistribution": domain.get(
            "missing_target_date_fallback_distribution",
            "",
        ),
        "dateShortcutLabel": domain.get("date_shortcut_label", ""),
        "modeNotice": domain.get("mode_notice", ""),
    }


def _track_modal_field_groups(form, *, hidden_field_names, metadata_field_names=None):
    """Split a track form into hidden, general, and metadata field groups."""
    metadata_field_names = metadata_field_names or set()
    ordered_general_field_names = [
        field_name
        for field_name in ("score", "status", "progress", "start_date", "end_date")
        if field_name in form.fields
    ]
    remaining_general_field_names = [
        field_name
        for field_name in form.fields
        if field_name not in hidden_field_names
        and field_name not in metadata_field_names
        and field_name != "notes"
        and field_name not in ordered_general_field_names
    ]
    return {
        "general_fields": [
            form[field_name]
            for field_name in ordered_general_field_names + remaining_general_field_names
        ],
        "metadata_fields": [
            form[field_name]
            for field_name in form.fields
            if field_name in metadata_field_names
        ],
        "hidden_fields": [
            form[field_name]
            for field_name in form.fields
            if field_name in hidden_field_names
        ],
    }


def _track_modal_release_date_shortcut(*candidates):
    """Return an ISO release-date string for the shared track modal shortcut."""
    for candidate in candidates:
        if not candidate:
            continue
        if isinstance(candidate, dict):
            candidate = helpers.extract_release_datetime(candidate)
        elif isinstance(candidate, str):
            candidate = parse_date(candidate[:10])

        if not candidate:
            continue
        if isinstance(candidate, datetime):
            if timezone.is_aware(candidate):
                candidate = timezone.localtime(candidate)
            return candidate.date().isoformat()
        if isinstance(candidate, date):
            return candidate.isoformat()
    return ""


def _track_modal_release_runtime_minutes(media_type, *candidates):
    """Return a trusted runtime in minutes for release-date start-date backfill."""
    if media_type != MediaTypes.MOVIE.value:
        return ""

    for candidate in candidates:
        if not candidate:
            continue

        runtime_minutes = None
        if isinstance(candidate, dict):
            runtime_minutes = candidate.get("runtime_minutes")
            if runtime_minutes is None:
                runtime_minutes = (candidate.get("details") or {}).get("runtime")
        else:
            runtime_minutes = getattr(candidate, "runtime_minutes", None)
            if runtime_minutes is None:
                runtime_minutes = getattr(candidate, "runtime", None)

        if isinstance(runtime_minutes, str):
            stripped_runtime = runtime_minutes.strip()
            runtime_minutes = (
                int(stripped_runtime)
                if stripped_runtime.isdigit()
                else stats.parse_runtime_to_minutes(stripped_runtime)
            )
        elif isinstance(runtime_minutes, float):
            runtime_minutes = int(runtime_minutes)

        if isinstance(runtime_minutes, int) and 0 < runtime_minutes < 999998:
            return str(runtime_minutes)

    return ""


def _render_standard_track_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    *,
    form_override=None,
    bulk_form_override=None,
    initial_active_tab="general",
    track_form_id=None,
    return_url=None,
    track_action_update=False,
):
    """Build and render the standard media track modal context."""
    instance_id = request.GET.get("instance_id") or request.POST.get("instance_id")
    if instance_id:
        media = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    elif request.GET.get("is_create"):
        media = None
    else:
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
    route_identity_media_type = None
    route_library_media_type = None

    max_progress = None
    metadata_resolution_result = None
    metadata_item = None
    base_metadata = None
    if media:
        title = media.item
        metadata_item = media.item
        if (
            media_type == MediaTypes.ANIME.value
            and media.item.media_type == MediaTypes.TV.value
            and media.item.library_media_type == MediaTypes.ANIME.value
        ):
            route_identity_media_type = MediaTypes.TV.value
            route_library_media_type = MediaTypes.ANIME.value
        if media_type == MediaTypes.GAME.value:
            initial_data["progress"] = helpers.minutes_to_hhmm(media.progress)
        elif media_type in (
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        ):
            if media_type == MediaTypes.BOOK.value:
                if media.item.number_of_pages:
                    max_progress = media.item.number_of_pages
                else:
                    try:
                        metadata = services.get_media_metadata(
                            media.item.media_type,
                            media.item.media_id,
                            media.item.source,
                        )
                        number_of_pages = metadata.get("max_progress") or metadata.get(
                            "details",
                            {},
                        ).get("number_of_pages")
                        if number_of_pages:
                            media.item.number_of_pages = number_of_pages
                            media.item.save(update_fields=["number_of_pages"])
                            max_progress = number_of_pages
                    except Exception:
                        pass
            else:
                media_list = [media]
                BasicMedia.objects.annotate_max_progress(media_list, media_type)
                if hasattr(media, "max_progress"):
                    max_progress = media.max_progress

            if (
                request.user.book_comic_manga_progress_percentage
                and max_progress
                and media.progress
            ):
                percentage = round((media.progress / max_progress) * 100, 1)
                initial_data["progress"] = percentage
    else:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        base_metadata = metadata
        title = metadata["title"]
        route_identity_media_type = metadata.get("identity_media_type")
        route_library_media_type = metadata.get("library_media_type")
        if media_type == MediaTypes.SEASON.value:
            title += f" S{season_number}"
        item_lookup = {
            "media_id": media_id,
            "source": source,
            "media_type": metadata_resolution.get_tracking_media_type(
                media_type,
                source=source,
                identity_media_type=route_identity_media_type,
            ),
            "season_number": season_number,
        }
        if metadata_resolution.is_grouped_anime_route(
            media_type,
            source=source,
            identity_media_type=route_identity_media_type,
            library_media_type=route_library_media_type,
        ):
            item_lookup["library_media_type"] = MediaTypes.ANIME.value
        metadata_item = Item.objects.filter(**item_lookup).first()

    if route_identity_media_type:
        initial_data["identity_media_type"] = route_identity_media_type
    if route_library_media_type:
        initial_data["library_media_type"] = route_library_media_type
    if "image_url" not in initial_data:
        preferred_image = None
        if metadata_item and metadata_item.image and metadata_item.image != settings.IMG_NONE:
            preferred_image = metadata_item.image
        elif (
            base_metadata
            and base_metadata.get("image")
            and base_metadata["image"] != settings.IMG_NONE
        ):
            preferred_image = base_metadata["image"]
        if preferred_image:
            initial_data["image_url"] = preferred_image

    form_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
        identity_media_type=route_identity_media_type,
    )
    form_class = get_form_class(form_media_type)
    if form_override is not None:
        form = form_override
    elif media_type in (
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
        MediaTypes.MANGA.value,
    ):
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

    hidden_field_names = {
        "instance_id",
        "media_type",
        "identity_media_type",
        "library_media_type",
        "source",
        "media_id",
        "season_number",
    }
    metadata_field_names = {"image_url"}
    field_groups = _track_modal_field_groups(
        form,
        hidden_field_names=hidden_field_names,
        metadata_field_names=metadata_field_names,
    )
    general_fields = field_groups["general_fields"]
    metadata_fields = field_groups["metadata_fields"]
    hidden_fields = field_groups["hidden_fields"]
    image_field = form["image_url"] if "image_url" in form.fields else None

    display_provider = source
    identity_provider = source
    grouped_preview = None
    grouped_preview_target = None
    can_update_metadata_provider = False
    can_migrate_grouped_anime = False
    metadata_provider_mapping_status = "identity"
    metadata_provider_options = []

    if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        if base_metadata is None:
            base_metadata = services.get_media_metadata(
                media_type,
                media_id,
                source,
                [season_number],
            )
        metadata_resolution_result = metadata_resolution.resolve_detail_metadata(
            request.user,
            item=metadata_item,
            route_media_type=media_type,
            media_id=media_id,
            source=source,
            base_metadata=base_metadata,
        )
        display_provider = metadata_resolution_result.display_provider
        identity_provider = metadata_resolution_result.identity_provider
        grouped_preview = metadata_resolution_result.grouped_preview
        grouped_preview_target = metadata_resolution_result.grouped_preview_target
        metadata_provider_mapping_status = metadata_resolution_result.mapping_status
        metadata_provider_options = metadata_resolution.available_metadata_provider_options(
            media_type,
            identity_provider=identity_provider,
        )
        can_migrate_grouped_anime = bool(
            metadata_item is not None
            and metadata_item.source == Sources.MAL.value
            and metadata_item.media_type == MediaTypes.ANIME.value
            and display_provider in {Sources.TMDB.value, Sources.TVDB.value}
            and grouped_preview
            and Anime.objects.filter(user=request.user, item=metadata_item).exists()
        )
    elif metadata_item is not None and custom_metadata.supports_custom_provider(media_type):
        metadata_provider_options = metadata_resolution.available_metadata_provider_options(
            media_type,
            identity_provider=identity_provider,
        )
        preference = MetadataProviderPreference.objects.filter(
            user=request.user,
            item=metadata_item,
        ).first()
        allowed_providers = {choice.value for choice in metadata_provider_options}
        if preference and preference.provider in allowed_providers:
            display_provider = preference.provider
            if (
                display_provider == Sources.MANUAL.value
                and identity_provider != Sources.MANUAL.value
            ):
                metadata_provider_mapping_status = "custom"

    can_update_metadata_provider = bool(
        metadata_item is not None and metadata_provider_options
    )

    manual_metadata_form = None
    can_edit_custom_metadata = bool(
        metadata_item is not None
        and display_provider == Sources.MANUAL.value
        and custom_metadata.supports_custom_metadata(metadata_item)
    )
    if can_edit_custom_metadata:
        manual_metadata_form = custom_metadata.ManualMetadataForm(
            item=metadata_item,
            prefix="metadata",
        )

    metadata_tab_available = bool(
        metadata_fields
        or can_update_metadata_provider
        or can_migrate_grouped_anime
        or manual_metadata_form
    )

    episode_plays_domain = bulk_episode_tracking.build_episode_play_domain(
        request.user,
        media_type,
        source,
        media_id,
        metadata_item=metadata_item,
        base_metadata=base_metadata,
        metadata_resolution_result=metadata_resolution_result,
    )
    episode_plays_tab_available = bool(episode_plays_domain)
    if return_url is None:
        return_url = (
            request.GET.get("return_url")
            or request.GET.get("next")
            or request.POST.get("return_url", "")
            or request.POST.get("next", "")
        )
    if episode_plays_tab_available:
        if bulk_form_override is not None:
            episode_plays_form = bulk_form_override
        else:
            bulk_initial = _bulk_episode_form_initial_data(return_url, episode_plays_domain)
            bulk_initial["instance_id"] = instance_id or ""
            episode_plays_form = BulkEpisodeTrackForm(
                initial=bulk_initial,
                domain=episode_plays_domain,
            )
    else:
        episode_plays_form = None

    track_form_id = track_form_id or f"track-form-{uuid4().hex}"
    release_date_shortcut = _track_modal_release_date_shortcut(
        getattr(metadata_item, "release_datetime", None) if metadata_item else None,
        (
            metadata_resolution_result.header_metadata
            if metadata_resolution_result is not None
            else None
        ),
        base_metadata,
    )
    release_date_runtime_minutes = _track_modal_release_runtime_minutes(
        media_type,
        metadata_item,
        (
            metadata_resolution_result.header_metadata
            if metadata_resolution_result is not None
            else None
        ),
        base_metadata,
    )
    context = {
        "user": request.user,
        "title": title,
        "media_type": media_type,
        "form": form,
        "media": media,
        "return_url": return_url,
        "max_progress": max_progress,
        "display_provider": display_provider,
        "display_provider_label": metadata_resolution.metadata_provider_label(
            display_provider,
        ),
        "identity_provider": identity_provider,
        "identity_provider_label": metadata_resolution.metadata_provider_label(
            identity_provider,
        ),
        "grouped_preview": grouped_preview,
        "grouped_preview_target": grouped_preview_target,
        "metadata_provider_mapping_status": metadata_provider_mapping_status,
        "metadata_provider_options": metadata_provider_options,
        "can_update_metadata_provider": can_update_metadata_provider,
        "can_migrate_grouped_anime": can_migrate_grouped_anime,
        "metadata_tab_available": metadata_tab_available,
        "metadata_item": metadata_item,
        "general_hidden_fields": hidden_fields,
        "general_fields": general_fields,
        "general_submit_formaction": f"{reverse('media_save')}?next={return_url}",
        "general_delete_formaction": f"{reverse('media_delete')}?next={return_url}",
        "general_existing_instance": media,
        "metadata_fields": metadata_fields,
        "image_field": image_field,
        "image_save_item_id": (
            metadata_item.id
            if media and metadata_item and not can_edit_custom_metadata
            else None
        ),
        "release_date_shortcut": release_date_shortcut,
        "release_date_runtime_minutes": release_date_runtime_minutes,
        "manual_metadata_form": manual_metadata_form,
        "manual_metadata_formaction": (
            reverse("update_manual_item_metadata", args=[metadata_item.id])
            if can_edit_custom_metadata
            else ""
        ),
        "can_edit_custom_metadata": can_edit_custom_metadata,
        "track_form_id": track_form_id,
        "track_action_update": track_action_update,
        "initial_active_tab": initial_active_tab,
        "episode_plays_tab_available": episode_plays_tab_available,
        "episode_plays_form": episode_plays_form,
        "episode_plays_formaction": reverse("episode_bulk_save"),
        "episode_plays_tab_label": "Episode Plays",
        "episode_plays_submit_label": "Save plays",
        "episode_plays_domain": _episode_domain_template_payload(episode_plays_domain),
        "episode_plays_mode_notice": (
            episode_plays_domain.get("mode_notice", "")
            if episode_plays_domain
            else ""
        ),
        "episode_plays_domain_script_id": f"{track_form_id}-episode-domain",
    }
    context.update(_build_track_modal_discover_tab_context(request.user, metadata_item))
    response = render(
        request,
        "app/components/fill_track.html",
        context,
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def _render_podcast_show_track_modal(
    request,
    show,
    *,
    form_override=None,
    bulk_form_override=None,
    initial_active_tab="general",
    track_form_id=None,
    return_url=None,
    track_action_update=False,
):
    """Build and render the podcast show tracking modal with bulk episode plays."""
    from app.forms import PodcastShowTrackerForm
    from app.models import PodcastShowTracker

    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()
    if return_url is None:
        return_url = (
            request.GET.get("return_url")
            or request.GET.get("next")
            or request.POST.get("return_url", "")
            or request.POST.get("next", "")
        )

    if form_override is not None:
        form = form_override
    else:
        form = PodcastShowTrackerForm(
            instance=tracker,
            initial={"show_id": show.id},
            user=request.user,
        )

    field_groups = _track_modal_field_groups(
        form,
        hidden_field_names={"show_id"},
        metadata_field_names=set(),
    )
    episode_plays_domain = bulk_episode_tracking.build_episode_play_domain(
        request.user,
        MediaTypes.PODCAST.value,
        Sources.POCKETCASTS.value,
        show.podcast_uuid,
        podcast_show=show,
    )
    episode_plays_tab_available = bool(episode_plays_domain)
    if episode_plays_tab_available:
        if bulk_form_override is not None:
            episode_plays_form = bulk_form_override
        else:
            bulk_initial = _bulk_episode_form_initial_data(
                return_url,
                episode_plays_domain,
            )
            bulk_initial["instance_id"] = tracker.id if tracker else ""
            episode_plays_form = BulkEpisodeTrackForm(
                initial=bulk_initial,
                domain=episode_plays_domain,
            )
    else:
        episode_plays_form = None

    track_form_id = track_form_id or f"track-form-{uuid4().hex}"
    response = render(
        request,
        "app/components/fill_track.html",
        {
            "user": request.user,
            "title": show.title,
            "media_type": MediaTypes.PODCAST.value,
            "form": form,
            "media": tracker,
            "return_url": return_url,
            "metadata_tab_available": False,
            "metadata_fields": [],
            "general_hidden_fields": field_groups["hidden_fields"],
            "general_fields": field_groups["general_fields"],
            "general_submit_formaction": (
                f"{reverse('podcast_show_save')}?next={return_url}"
            ),
            "general_delete_formaction": (
                f"{reverse('podcast_show_delete')}?next={return_url}"
            ),
            "general_existing_instance": tracker,
            "image_field": None,
            "image_save_item_id": None,
            "release_date_shortcut": "",
            "release_date_runtime_minutes": "",
            "track_form_id": track_form_id,
            "track_action_update": track_action_update,
            "initial_active_tab": initial_active_tab,
            "episode_plays_tab_available": episode_plays_tab_available,
            "episode_plays_form": episode_plays_form,
            "episode_plays_formaction": reverse("episode_bulk_save"),
            "episode_plays_tab_label": "Episode Plays",
            "episode_plays_submit_label": "Save plays",
            "episode_plays_domain": _episode_domain_template_payload(
                episode_plays_domain,
            ),
            "episode_plays_mode_notice": (
                episode_plays_domain.get("mode_notice", "")
                if episode_plays_domain
                else ""
            ),
            "episode_plays_domain_script_id": f"{track_form_id}-episode-domain",
        },
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


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
    track_action_update = (
        request.GET.get("track_action_update") == "1"
        or request.POST.get("track_action_update") == "1"
    )
    standard_modal = (
        request.GET.get("standard_modal") == "1"
        or request.POST.get("standard_modal") == "1"
    )

    # Handle podcast shows (identified by podcast_uuid)
    if (
        not standard_modal
        and media_type == MediaTypes.PODCAST.value
        and source == Sources.POCKETCASTS.value
    ):
        from app.models import PodcastEpisode, PodcastShow

        # Check if this is a show (podcast_uuid) or an episode (episode_uuid)
        show = PodcastShow.objects.filter(podcast_uuid=media_id).first()
        if show:
            return _render_podcast_show_track_modal(request, show)

        # This is an episode (episode_uuid) - use music-style modal
        episode = PodcastEpisode.objects.filter(episode_uuid=media_id).first()
        if episode:
            from app.models import Podcast

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

                podcast = PodcastHistoryWrapper(all_podcasts, item, all_history)
            else:
                podcast = _DummyPodcastWrapper(item)

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

    return _render_standard_track_modal(
        request,
        source,
        media_type,
        media_id,
        season_number=season_number,
        track_action_update=track_action_update,
    )

@require_POST
def media_save(request):
    """Save or update media data to the database."""
    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    identity_media_type = request.POST.get("identity_media_type") or None
    library_media_type = request.POST.get("library_media_type") or None
    season_number = request.POST.get("season_number")
    instance_id = request.POST.get("instance_id")
    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
        identity_media_type=identity_media_type,
    )
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=library_media_type or media_type,
    )
    
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
                    media_type=tracking_media_type,
                    season_number=season_number,
                    defaults={
                        **Item.title_fields_from_metadata(metadata),
                        "library_media_type": (
                            library_media_type
                            or metadata.get("library_media_type")
                            or media_type
                        ),
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
        hydrated = ensure_item_metadata(
            request.user,
            media_type,
            media_id,
            source,
            season_number,
            identity_media_type=identity_media_type,
            library_media_type=library_media_type,
        )
        model = apps.get_model(app_label="app", model_name=tracking_media_type)
        instance = model(item=hydrated.item, user=request.user)

        if tracking_media_type == MediaTypes.MUSIC.value:
            instance.artist = hydrated.artist
            instance.album = hydrated.album
            instance.track = hydrated.track
        if tracking_media_type == MediaTypes.PODCAST.value and hydrated.podcast_show is not None:
            instance.show = hydrated.podcast_show

    # Validate the form and save the instance if it's valid
    form_class = get_form_class(tracking_media_type)
    form = form_class(request.POST, instance=instance, user=request.user)
    media = instance
    is_htmx = bool(request.headers.get("HX-Request"))
    track_form_id = request.POST.get("track_form_id") or (
        f"track-form-{uuid4().hex}"
    )
    return_url = quote(
        request.GET.get("next") or request.POST.get("return_url") or "",
        safe="",
    )
    action_verb = "Added" if not instance_id else "Updated"
    if form.is_valid():
        media = form.save()
        BasicMedia.objects.annotate_max_progress([media], media_type)
        image_url = form.cleaned_data.get("image_url")
        if image_url and media.item.image != image_url:
            media.item.image = image_url
            media.item.save(update_fields=["image"])
        logger.info("%s saved successfully.", media)
        display_title = (
            media.item.get_display_title(request.user)
            if hasattr(media.item, "get_display_title")
            else media.item.title
        ) or "item"
        if is_htmx:
            user_medias = list(
                media.__class__.objects.filter(user=request.user, item=media.item).select_related(
                    "item",
                ),
            )
            play_stats, activity_subtitle = _build_detail_activity_state(
                media_type,
                {"max_progress": getattr(media, "max_progress", None)},
                current_instance=media,
                user_medias=user_medias,
                public_view=False,
            )
            response = render(
                request,
                "app/components/detail_track_action.html",
                {
                    "media": media.item,
                    "current_instance": media,
                    "return_url": return_url,
                    "track_action_update": True,
                },
            )
            activity_subtitle_response = render(
                request,
                "app/components/detail_activity_subtitle_slot.html",
                {
                    "media": media.item,
                    "media_type": media_type,
                    "current_instance": media,
                    "activity_subtitle": activity_subtitle,
                    "play_stats": play_stats,
                    "user": request.user,
                    "activity_subtitle_slot_oob": True,
                },
            )
            score_chip_response = render(
                request,
                "app/components/detail_score_chip_slot.html",
                {
                    "media": media.item,
                    "current_instance": media,
                    "media_type": media_type,
                    "user": request.user,
                    "user_medias": [media],
                    "public_view": False,
                    "csrf_token": request.META.get("CSRF_COOKIE", ""),
                    "score_chip_slot_oob": True,
                },
            )
            response.write(activity_subtitle_response.content.decode())
            response.write(score_chip_response.content.decode())
            response["HX-Trigger"] = json.dumps(
                {
                    "closeModal": {"formId": track_form_id},
                    "showToast": {
                        "message": f"{action_verb} {display_title}.",
                        "type": "success",
                    },
                },
            )
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
            return response
        messages.success(request, f"{action_verb} {display_title}.")
    else:
        logger.error(form.errors.as_json())
        if is_htmx:
            modal_response = _render_standard_track_modal(
                request,
                source,
                media_type,
                media_id,
                season_number=season_number,
                form_override=form,
                track_form_id=track_form_id,
                return_url=return_url,
                track_action_update=True,
            )
            response = render(
                request,
                "app/components/detail_track_action.html",
                {
                    "media": media.item,
                    "current_instance": media,
                    "return_url": return_url,
                    "track_open": True,
                    "track_modal_content": modal_response.content.decode(),
                    "track_action_update": True,
                },
            )
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
            return response
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
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=media_type,
    )
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

    redirect_response = helpers.redirect_back(request)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": redirect_response.url})
    return redirect_response


@require_POST
def episode_save(request):
    """Handle the creation, deletion, and updating of episodes for a season."""
    from django.template.loader import render_to_string

    media_id = request.POST["media_id"]
    season_number = int(request.POST["season_number"])
    episode_number = int(request.POST["episode_number"])
    source = request.POST["source"]
    library_media_type = (request.POST.get("library_media_type") or "").strip()

    next_path = request.GET.get("next") or ""
    if source == Sources.TMDB.value and next_path:
        parsed_next_path = urlparse(next_path).path
        path_parts = [segment for segment in parsed_next_path.split("/") if segment]
        if len(path_parts) >= 2 and path_parts[0] == "details":
            route_source = path_parts[1]
            if route_source in {choice[0] for choice in Sources.choices}:
                source = route_source

    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.TV.value,
    )

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
            source=source,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                **Item.title_fields_from_metadata(tv_with_seasons_metadata),
                "library_media_type": library_media_type,
                "image": season_image,
            },
        )
        if library_media_type and item.library_media_type != library_media_type:
            item.library_media_type = library_media_type
            item.save(update_fields=["library_media_type"])
        related_season = Season.objects.create(
            item=item,
            user=request.user,
            score=None,
            status=Status.IN_PROGRESS.value,
            notes="",
        )

        logger.info("%s did not exist, it was created successfully.", related_season)

    if library_media_type and related_season.item.library_media_type != library_media_type:
        related_season.item.library_media_type = library_media_type
        related_season.item.save(update_fields=["library_media_type"])
    if (
        library_media_type
        and related_season.related_tv.item.library_media_type != library_media_type
    ):
        related_season.related_tv.item.library_media_type = library_media_type
        related_season.related_tv.item.save(update_fields=["library_media_type"])

    related_season.watch(episode_number, form.cleaned_data["end_date"])

    if request.headers.get("HX-Request"):
        episode_history = list(
            Episode.objects.filter(
                related_season=related_season,
                item__media_id=media_id,
                item__source=source,
                item__episode_number=episode_number,
            )
            .select_related("item", "related_season")
            .order_by("-end_date", "-created_at")
        )
        if not episode_history:
            return HttpResponse("Episode not found", status=404)

        episode = episode_history[0]
        episode.history = episode_history
        episode.collection_entry = CollectionEntry.objects.filter(
            item=episode.item,
            user=request.user,
        ).select_related("item").first()

        response = HttpResponse()
        response.write(
            render_to_string(
                "app/components/detail_episode_track_button.html",
                {
                    "episode": episode,
                    "track_button_oob": True,
                },
                request=request,
            ),
        )
        response.write(
            render_to_string(
                "app/components/detail_episode_history_line.html",
                {
                    "episode": episode,
                    "user": request.user,
                    "history_oob": True,
                },
                request=request,
            ),
        )
        response.write(
            f'<span id="season-progress-mobile-{related_season.id}" hx-swap-oob="true" class="text-sm font-medium text-gray-400">Progress: {related_season.completed_episode_count}{f"/{related_season.max_progress}" if related_season.max_progress else ""}</span>',
        )
        response.write(
            f'<span id="season-progress-desktop-{related_season.id}" hx-swap-oob="true" class="text-sm font-medium text-gray-400">Progress: {related_season.completed_episode_count}{f"/{related_season.max_progress}" if related_season.max_progress else ""}</span>',
        )
        response["HX-Trigger"] = json.dumps(
            {
                "closeModal": {},
                "showToast": {
                    "message": f"Added watch for episode {episode_number}.",
                    "type": "success",
                },
            },
        )
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    return helpers.redirect_back(request)


def _episode_bulk_redirect_url(request, result):
    """Return the full-page destination after a successful bulk episode save."""
    if result.grouped_item and result.grouped_redirect_media_type:
        title = result.grouped_item.get_display_title(request.user) or result.grouped_item.title or "item"
        return reverse(
            "media_details",
            kwargs={
                "source": result.grouped_item.source,
                "media_type": result.grouped_redirect_media_type,
                "media_id": result.grouped_item.media_id,
                "title": slugify(title),
            },
        )

    redirect_response = helpers.redirect_back(request)
    return redirect_response.url


@require_POST
def episode_bulk_save(request):
    """Persist a bulk range of episode plays from track modal tabs."""
    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    fallback_media_type = request.POST.get("library_media_type") or media_type
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=fallback_media_type,
    )

    metadata_item = None
    base_metadata = None
    metadata_resolution_result = None
    podcast_show = None

    if media_type == MediaTypes.PODCAST.value and source == Sources.POCKETCASTS.value:
        podcast_show = PodcastShow.objects.filter(podcast_uuid=media_id).first()
    else:
        item_lookup = {
            "media_id": media_id,
            "source": source,
            "media_type": metadata_resolution.get_tracking_media_type(
                media_type,
                source=source,
                identity_media_type=request.POST.get("identity_media_type") or None,
            ),
        }
        if media_type == MediaTypes.ANIME.value and source in {
            Sources.TMDB.value,
            Sources.TVDB.value,
        }:
            item_lookup["library_media_type"] = MediaTypes.ANIME.value
        metadata_item = Item.objects.filter(**item_lookup).first()

        base_metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
        )
        if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            metadata_resolution_result = metadata_resolution.resolve_detail_metadata(
                request.user,
                item=metadata_item,
                route_media_type=media_type,
                media_id=media_id,
                source=source,
                base_metadata=base_metadata,
            )

    episode_domain = bulk_episode_tracking.build_episode_play_domain(
        request.user,
        media_type,
        source,
        media_id,
        metadata_item=metadata_item,
        base_metadata=base_metadata,
        metadata_resolution_result=metadata_resolution_result,
        podcast_show=podcast_show,
    )
    if not episode_domain:
        messages.error(
            request,
            "Bulk episode tracking is not available for this title.",
        )
        redirect_url = _episode_bulk_redirect_url(
            request,
            bulk_episode_tracking.BulkEpisodePlayResult(
                created_count=0,
                replaced_episode_count=0,
            ),
        )
        if request.headers.get("HX-Request"):
            return HttpResponse(status=400, headers={"HX-Redirect": redirect_url})
        return redirect(redirect_url)

    bulk_form = BulkEpisodeTrackForm(
        request.POST,
        domain=episode_domain,
    )
    if not bulk_form.is_valid():
        if podcast_show is not None:
            return _render_podcast_show_track_modal(
                request,
                podcast_show,
                bulk_form_override=bulk_form,
                initial_active_tab="episode-plays",
            )
        return _render_standard_track_modal(
            request,
            source,
            media_type,
            media_id,
            form_override=None,
            bulk_form_override=bulk_form,
            initial_active_tab="episode-plays",
        )

    result = bulk_episode_tracking.apply_bulk_episode_plays(
        request.user,
        episode_domain,
        selected_episodes=bulk_form.cleaned_data["selected_domain_episodes"],
        write_mode=bulk_form.cleaned_data["write_mode"],
        distribution_mode=bulk_form.cleaned_data["distribution_mode"],
        start_date=bulk_form.cleaned_data.get("start_date"),
        end_date=bulk_form.cleaned_data.get("end_date"),
    )

    action_verb = (
        "Replaced"
        if bulk_form.cleaned_data["write_mode"] == BulkEpisodeTrackForm.WRITE_MODE_REPLACE
        else "Added"
    )
    detail_bits = []
    if result.migrated_flat_anime:
        detail_bits.append("after migrating grouped anime tracking")
    elif result.created_grouped_tracking and result.grouped_item:
        detail_bits.append("after creating grouped anime tracking")
    detail_suffix = f" {' '.join(detail_bits)}" if detail_bits else ""
    messages.success(
        request,
        f"{action_verb} {result.created_count} episode play{'s' if result.created_count != 1 else ''}{detail_suffix}.",
    )

    redirect_url = _episode_bulk_redirect_url(request, result)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": redirect_url})
    return redirect(redirect_url)


@require_POST
def music_bulk_save(request):
    """Persist a bulk range of music plays from artist and album track modals."""
    from app.forms import AlbumTrackerForm, ArtistTrackerForm
    from app.models import AlbumTracker, ArtistTracker

    context_kind = (request.POST.get("context_kind") or "").strip()
    context_id = request.POST.get("context_id")
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.MUSIC.value,
    )

    artist = None
    album = None
    tracker = None
    tracker_form = None
    title = ""
    save_url = ""
    delete_url = ""
    bulk_domain = None

    if context_kind == "artist":
        artist = get_object_or_404(Artist, id=context_id)
        tracker = ArtistTracker.objects.filter(user=request.user, artist=artist).first()
        tracker_form = ArtistTrackerForm(
            instance=tracker,
            initial={"artist_id": artist.id},
            user=request.user,
        )
        title = artist.name
        save_url = reverse("artist_save")
        delete_url = reverse("artist_delete")
        bulk_domain = bulk_music_tracking.build_artist_play_domain(request.user, artist)
    elif context_kind == "album":
        album = get_object_or_404(Album.objects.select_related("artist"), id=context_id)
        tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()
        tracker_form = AlbumTrackerForm(
            instance=tracker,
            initial={"album_id": album.id},
            user=request.user,
        )
        title = album.title
        save_url = reverse("album_save")
        delete_url = reverse("album_delete")
        bulk_domain = bulk_music_tracking.build_album_play_domain(request.user, album)
    else:
        return HttpResponseBadRequest("Invalid music bulk tracking context.")

    if not bulk_domain:
        messages.error(
            request,
            "Bulk track plays are not available for this music entry yet.",
        )
        redirect_url = _music_bulk_redirect_url(
            request,
            artist=artist,
            album=album,
        )
        if request.headers.get("HX-Request"):
            return HttpResponse(status=400, headers={"HX-Redirect": redirect_url})
        return redirect(redirect_url)

    bulk_form = BulkEpisodeTrackForm(
        request.POST,
        domain=bulk_domain,
    )
    if not bulk_form.is_valid():
        return _render_music_tracker_modal(
            request,
            title=title,
            tracker=tracker,
            form=tracker_form,
            save_url=save_url,
            delete_url=delete_url,
            release_date_shortcut=(
                _track_modal_release_date_shortcut(album.release_date)
                if album is not None
                else ""
            ),
            bulk_domain=bulk_domain,
            bulk_form_override=bulk_form,
            initial_active_tab="episode-plays",
        )

    result = bulk_music_tracking.apply_bulk_music_plays(
        request.user,
        bulk_domain,
        selected_episodes=bulk_form.cleaned_data["selected_domain_episodes"],
        write_mode=bulk_form.cleaned_data["write_mode"],
        distribution_mode=bulk_form.cleaned_data["distribution_mode"],
        start_date=bulk_form.cleaned_data.get("start_date"),
        end_date=bulk_form.cleaned_data.get("end_date"),
    )

    action_verb = (
        "Replaced"
        if bulk_form.cleaned_data["write_mode"] == BulkEpisodeTrackForm.WRITE_MODE_REPLACE
        else "Added"
    )
    messages.success(
        request,
        f"{action_verb} {result.created_count} track play{'s' if result.created_count != 1 else ''}.",
    )

    redirect_url = _music_bulk_redirect_url(
        request,
        artist=artist,
        album=album,
    )
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": redirect_url})
    return redirect(redirect_url)


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


@login_not_required
@require_GET
def person_detail(request, source, person_id, name):
    """Render a provider-backed person or author profile page."""
    del name  # URL slug is cosmetic; person_id is canonical.
    source_dispatch = {
        Sources.TMDB.value: {
            "fetcher": tmdb.person,
            "entries_key": "filmography",
            "tracked_media_types": (
                MediaTypes.MOVIE.value,
                MediaTypes.TV.value,
            ),
            "source_url": lambda person_id_value: f"https://www.themoviedb.org/person/{person_id_value}",
            "is_author": False,
        },
        Sources.HARDCOVER.value: {
            "fetcher": hardcover.author_profile,
            "entries_key": "bibliography",
            "tracked_media_types": (MediaTypes.BOOK.value,),
            "source_url": lambda person_id_value: f"https://hardcover.app/authors/{person_id_value}",
            "is_author": True,
        },
        Sources.OPENLIBRARY.value: {
            "fetcher": openlibrary.author_profile,
            "entries_key": "bibliography",
            "tracked_media_types": (MediaTypes.BOOK.value,),
            "source_url": lambda person_id_value: f"https://openlibrary.org/authors/{person_id_value}",
            "is_author": True,
        },
        Sources.COMICVINE.value: {
            "fetcher": comicvine.person_profile,
            "entries_key": "bibliography",
            "tracked_media_types": (MediaTypes.COMIC.value,),
            "source_url": lambda person_id_value: f"https://comicvine.gamespot.com/person/4040-{person_id_value}/",
            "is_author": True,
        },
        Sources.MANGAUPDATES.value: {
            "fetcher": mangaupdates.author_profile,
            "entries_key": "bibliography",
            "tracked_media_types": (MediaTypes.MANGA.value,),
            "source_url": lambda person_id_value: f"https://www.mangaupdates.com/authors.html?id={person_id_value}",
            "is_author": True,
        },
    }
    source_config = source_dispatch.get(source)
    if not source_config:
        return HttpResponseBadRequest("Person pages are not available for this source.")

    person_metadata = source_config["fetcher"](person_id) or {}
    person = credits.upsert_person_profile(source, person_id, person_metadata)

    person_id_str = str(person_id)
    is_author = source_config["is_author"]
    person_data = {
        "source": source,
        "person_id": person_id_str,
        "name": person_metadata.get("name")
        or (person.name if person else "Unknown Person"),
        "image": person_metadata.get("image")
        or (person.image if person else settings.IMG_NONE),
        "biography": person_metadata.get("biography")
        or (person.biography if person else ""),
        "known_for_department": person_metadata.get("known_for_department")
        or (person.known_for_department if person else ("Author" if is_author else "")),
        "birth_date": person_metadata.get("birth_date")
        or (person.birth_date.isoformat() if person and person.birth_date else None),
        "death_date": person_metadata.get("death_date")
        or (person.death_date.isoformat() if person and person.death_date else None),
        "place_of_birth": person_metadata.get("place_of_birth")
        or (person.place_of_birth if person else ""),
    }

    media_types_for_source = source_config["tracked_media_types"]
    raw_entries = person_metadata.get(source_config["entries_key"], [])
    filmography = []
    for index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, dict):
            continue
        media_id_value = raw_entry.get("media_id")
        if media_id_value is None:
            continue
        media_type = raw_entry.get("media_type")
        if media_type is None and len(media_types_for_source) == 1:
            media_type = media_types_for_source[0]
        if media_type not in media_types_for_source:
            continue
        filmography.append(
            {
                **raw_entry,
                "media_id": str(media_id_value),
                "media_type": media_type,
                "source": raw_entry.get("source") or source,
                "title": raw_entry.get("title") or "Unknown Title",
                "image": raw_entry.get("image") or settings.IMG_NONE,
                "year": raw_entry.get("year"),
                "role": raw_entry.get("role") or "",
                "department": raw_entry.get("department") or "",
                "credit_type": raw_entry.get("credit_type") or ("author" if is_author else ""),
                "sort_order": raw_entry.get("sort_order", index),
            },
        )

    if is_author and not filmography:
        fallback_items = Item.objects.filter(
            source=source,
            media_type__in=media_types_for_source,
            person_credits__role_type=CreditRoleType.AUTHOR.value,
            person_credits__person__source=source,
            person_credits__person__source_person_id=person_id_str,
        ).order_by("title").distinct()
        for index, item in enumerate(fallback_items):
            filmography.append(
                {
                    "media_id": str(item.media_id),
                    "source": source,
                    "media_type": item.media_type,
                    "title": item.title,
                    "image": item.image or settings.IMG_NONE,
                    "year": None,
                    "role": "Author",
                    "department": "",
                    "credit_type": "author",
                    "sort_order": index,
                },
            )

    seen_media = set()
    deduped_filmography = []
    for entry in filmography:
        media_key = (entry.get("media_type"), str(entry.get("media_id")))
        if media_key in seen_media:
            continue
        seen_media.add(media_key)
        deduped_filmography.append(entry)
    filmography = deduped_filmography

    tracked_item_map = {}
    if filmography:
        tracked_filters = Q()
        for media_type in media_types_for_source:
            media_ids_for_type = {
                entry["media_id"]
                for entry in filmography
                if entry.get("media_type") == media_type
            }
            if media_ids_for_type:
                tracked_filters |= Q(
                    media_type=media_type,
                    media_id__in=media_ids_for_type,
                )
        if tracked_filters:
            tracked_items = Item.objects.filter(source=source).filter(tracked_filters)
            tracked_item_map = {
                (item.media_type, str(item.media_id)): item
                for item in tracked_items
            }

    credited_tracked_items_by_key = {}
    if request.user.is_authenticated and is_author:
        for model, media_type in (
            (Book, MediaTypes.BOOK.value),
            (Comic, MediaTypes.COMIC.value),
            (Manga, MediaTypes.MANGA.value),
        ):
            tracked_reads = (
                model.objects.filter(
                    user=request.user,
                    item__media_type=media_type,
                    item__person_credits__role_type=CreditRoleType.AUTHOR.value,
                    item__person_credits__person__source=source,
                    item__person_credits__person__source_person_id=person_id_str,
                )
                .filter(Q(start_date__isnull=False) | Q(end_date__isnull=False))
                .select_related("item")
                .distinct()
            )
            for tracked_read in tracked_reads:
                item = tracked_read.item
                media_key = (item.media_type, str(item.media_id))
                if media_key in credited_tracked_items_by_key:
                    continue
                credited_tracked_items_by_key[media_key] = item

    if credited_tracked_items_by_key:
        tracked_item_map.update(credited_tracked_items_by_key)

    watched_media_keys = set()
    watched_person_minutes_by_media_key = {}
    person_talent_totals = None
    if request.user.is_authenticated and not is_author:
        person_talent_totals = statistics_cache.get_person_talent_totals(
            request.user,
            source,
            person_id_str,
        )
        watched_person_minutes_by_media_key = (
            person_talent_totals.get("minutes_by_media_key", {})
            if person_talent_totals
            else {}
        )

    if credited_tracked_items_by_key:
        watched_media_keys.update(credited_tracked_items_by_key.keys())

    if request.user.is_authenticated and filmography:
        watched_movie_media_ids = {
            entry["media_id"]
            for entry in filmography
            if entry.get("media_type") == MediaTypes.MOVIE.value
        }
        watched_tv_media_ids = {
            entry["media_id"]
            for entry in filmography
            if entry.get("media_type") == MediaTypes.TV.value
        }
        watched_book_media_ids = {
            entry["media_id"]
            for entry in filmography
            if entry.get("media_type") == MediaTypes.BOOK.value
        }
        watched_comic_media_ids = {
            entry["media_id"]
            for entry in filmography
            if entry.get("media_type") == MediaTypes.COMIC.value
        }
        watched_manga_media_ids = {
            entry["media_id"]
            for entry in filmography
            if entry.get("media_type") == MediaTypes.MANGA.value
        }

        if watched_movie_media_ids:
            watched_movies = Movie.objects.filter(
                user=request.user,
                item__source=source,
                item__media_type=MediaTypes.MOVIE.value,
                item__media_id__in=watched_movie_media_ids,
            ).exclude(start_date__isnull=True, end_date__isnull=True)
            watched_media_keys.update(
                (media_type, str(media_id))
                for media_type, media_id in watched_movies.values_list(
                    "item__media_type",
                    "item__media_id",
                ).distinct()
            )

        if watched_tv_media_ids:
            watched_tv = Episode.objects.filter(
                related_season__user=request.user,
                end_date__isnull=False,
                related_season__related_tv__item__source=source,
                related_season__related_tv__item__media_type=MediaTypes.TV.value,
                related_season__related_tv__item__media_id__in=watched_tv_media_ids,
            )
            watched_media_keys.update(
                (media_type, str(media_id))
                for media_type, media_id in watched_tv.values_list(
                    "related_season__related_tv__item__media_type",
                    "related_season__related_tv__item__media_id",
                ).distinct()
            )

        if watched_book_media_ids:
            watched_books = Book.objects.filter(
                user=request.user,
                item__source=source,
                item__media_type=MediaTypes.BOOK.value,
                item__media_id__in=watched_book_media_ids,
            ).filter(Q(start_date__isnull=False) | Q(end_date__isnull=False))
            watched_media_keys.update(
                (media_type, str(media_id))
                for media_type, media_id in watched_books.values_list(
                    "item__media_type",
                    "item__media_id",
                ).distinct()
            )

        if watched_comic_media_ids:
            watched_comics = Comic.objects.filter(
                user=request.user,
                item__source=source,
                item__media_type=MediaTypes.COMIC.value,
                item__media_id__in=watched_comic_media_ids,
            ).filter(Q(start_date__isnull=False) | Q(end_date__isnull=False))
            watched_media_keys.update(
                (media_type, str(media_id))
                for media_type, media_id in watched_comics.values_list(
                    "item__media_type",
                    "item__media_id",
                ).distinct()
            )

        if watched_manga_media_ids:
            watched_manga = Manga.objects.filter(
                user=request.user,
                item__source=source,
                item__media_type=MediaTypes.MANGA.value,
                item__media_id__in=watched_manga_media_ids,
            ).filter(Q(start_date__isnull=False) | Q(end_date__isnull=False))
            watched_media_keys.update(
                (media_type, str(media_id))
                for media_type, media_id in watched_manga.values_list(
                    "item__media_type",
                    "item__media_id",
                ).distinct()
            )

    for entry in filmography:
        media_key = (entry.get("media_type"), str(entry.get("media_id")))
        entry["tracked_item"] = tracked_item_map.get(media_key)
        entry["is_watched"] = media_key in watched_media_keys

    watched_filmography = []
    if watched_media_keys:
        seen_watched_media = set()
        for entry in filmography:
            media_key = (entry.get("media_type"), str(entry.get("media_id")))
            if media_key in watched_media_keys and media_key not in seen_watched_media:
                watched_entry = dict(entry)
                watched_minutes = watched_person_minutes_by_media_key.get(media_key, 0)
                if watched_minutes > 0:
                    watched_entry["watched_person_runtime_display"] = (
                        helpers.minutes_to_hhmm(watched_minutes)
                    )
                watched_filmography.append(watched_entry)
                seen_watched_media.add(media_key)

        if is_author and credited_tracked_items_by_key:
            for media_key, tracked_item in credited_tracked_items_by_key.items():
                if media_key in seen_watched_media:
                    continue
                watched_filmography.append(
                    {
                        "media_id": str(tracked_item.media_id),
                        "source": tracked_item.source,
                        "media_type": tracked_item.media_type,
                        "title": tracked_item.title,
                        "image": tracked_item.image or settings.IMG_NONE,
                        "year": (
                            tracked_item.release_datetime.year
                            if tracked_item.release_datetime
                            else None
                        ),
                        "role": "Author",
                        "department": "",
                        "credit_type": "author",
                        "sort_order": len(watched_filmography),
                        "tracked_item": tracked_item,
                        "is_watched": True,
                    },
                )
                seen_watched_media.add(media_key)

    watched_movie_count = sum(
        1 for media_type, _ in watched_media_keys if media_type == MediaTypes.MOVIE.value
    )
    watched_show_count = sum(
        1 for media_type, _ in watched_media_keys if media_type == MediaTypes.TV.value
    )
    watched_book_count = sum(
        1
        for media_type, _ in watched_media_keys
        if media_type in (
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        )
    )

    history_filter_url = (
        f"{reverse('history')}?person_source={source}&person_id={person_id}"
    )
    source_url = source_config["source_url"](person_id_str)

    tracked_plays_count = None
    tracked_hours_count = None
    if request.user.is_authenticated:
        if is_author:
            tracked_plays_count = len(credited_tracked_items_by_key)
        else:
            tracked_plays_count = 0
            if person_talent_totals:
                tracked_plays_count = person_talent_totals.get("plays", 0)
                tracked_hours_count = person_talent_totals.get("watched_time")

    context = {
        "user": request.user,
        "person": person_data,
        "is_author": is_author,
        "watched_filmography": watched_filmography,
        "watched_movie_count": watched_movie_count,
        "watched_show_count": watched_show_count,
        "watched_book_count": watched_book_count,
        "filmography": filmography,
        "history_filter_url": history_filter_url,
        "tracked_plays_count": tracked_plays_count,
        "tracked_hours_count": tracked_hours_count,
        "source": source,
        "source_url": source_url,
    }
    return render(request, "app/person_detail.html", context)


def studio_detail(request, source, studio_id, name):
    """Render a provider-backed studio/company profile page."""
    del name  # URL slug is cosmetic; studio_id is canonical.

    studio = get_object_or_404(
        Studio,
        source=source,
        source_studio_id=str(studio_id),
    )

    studio_profile = (
        igdb.company_profile(studio_id)
        if source == Sources.IGDB.value
        else None
    )

    local_titles = []
    studio_credits = studio.item_credits.select_related("item").order_by(
        "sort_order",
        "item__title",
    )
    for index, studio_credit in enumerate(studio_credits):
        item = studio_credit.item
        if not item:
            continue
        local_titles.append(
            {
                "media_id": str(item.media_id),
                "source": item.source,
                "media_type": item.media_type,
                "title": item.title,
                "image": item.image or settings.IMG_NONE,
                "year": item.release_datetime.year if item.release_datetime else None,
                "role": "",
                "department": "",
                "credit_type": item.media_type,
                "sort_order": (
                    studio_credit.sort_order
                    if studio_credit.sort_order is not None
                    else index
                ),
                "tracked_item": item,
            },
        )

    credited_titles = []
    if studio_profile:
        credited_titles = [
            dict(entry)
            for entry in studio_profile.get("games") or []
            if isinstance(entry, dict)
        ]

    if credited_titles:
        existing_keys = {
            (entry.get("media_type"), str(entry.get("media_id")))
            for entry in credited_titles
        }
        for entry in local_titles:
            media_key = (entry.get("media_type"), str(entry.get("media_id")))
            if media_key not in existing_keys:
                credited_titles.append(entry)
    else:
        credited_titles = local_titles

    if credited_titles:
        game_ids = {
            str(entry.get("media_id"))
            for entry in credited_titles
            if entry.get("media_id") is not None
        }
        tracked_items = Item.objects.filter(
            source=source,
            media_type=MediaTypes.GAME.value,
            media_id__in=game_ids,
        )
        tracked_item_map = {
            (item.media_type, str(item.media_id)): item for item in tracked_items
        }
        for entry in credited_titles:
            media_key = (entry.get("media_type"), str(entry.get("media_id")))
            entry["tracked_item"] = tracked_item_map.get(media_key)

        credited_titles.sort(
            key=lambda row: (
                row.get("year") is None,
                -(row.get("year") or 0),
                row.get("title", "").lower(),
            ),
        )
        for index, entry in enumerate(credited_titles):
            entry["sort_order"] = index

    studio_description = "Studio profile generated from local credits."
    studio_source_url = ""
    studio_founded = None
    studio_developed_count = None
    studio_published_count = None
    if studio_profile:
        studio_description = studio_profile.get("description") or studio_description
        studio_source_url = studio_profile.get("source_url") or ""
        studio_details = studio_profile.get("details") or {}
        studio_founded = studio_details.get("founded")
        studio_developed_count = studio_details.get("developed_count")
        studio_published_count = studio_details.get("published_count")

    context = {
        "user": request.user,
        "studio": studio,
        "source": source,
        "credited_titles": credited_titles,
        "studio_description": studio_description,
        "studio_source_url": studio_source_url,
        "studio_founded": studio_founded,
        "studio_developed_count": studio_developed_count,
        "studio_published_count": studio_published_count,
        "studio_games_count": len(credited_titles),
        "IMG_NONE": settings.IMG_NONE,
    }
    return render(request, "app/studio_detail.html", context)


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
    """Return the tracking form modal for a podcast show."""
    from django.shortcuts import get_object_or_404

    from app.models import PodcastShow

    show = get_object_or_404(PodcastShow, id=show_id)
    return _render_podcast_show_track_modal(request, show)


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
                        self.in_progress_instance_id = podcast.id if not podcast.end_date else None

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
                podcast_wrapper = _DummyPodcastWrapper(enriched["item"] if enriched else item)

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
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.PODCAST.value,
    )

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
        redirect_response = redirect(next_url)
    else:
        redirect_response = redirect("podcast_show_detail", show_id=show.id)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": redirect_response.url})
    return redirect_response


@require_POST
def podcast_show_delete(request):
    """Delete a podcast show tracker - mirrors artist_delete."""
    from django.shortcuts import get_object_or_404

    from app.models import PodcastShow, PodcastShowTracker

    show_id = request.POST.get("show_id")
    show = get_object_or_404(PodcastShow, id=show_id)
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.PODCAST.value,
    )

    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {show.title} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        redirect_response = redirect(next_url)
    else:
        redirect_response = redirect("podcast_show_detail", show_id=show.id)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": redirect_response.url})
    return redirect_response


@require_POST
def podcast_mark_all_played(request, show_id):
    """Mark all episodes of this podcast currently in the library as completed on their release date.

    Episodes not yet imported from Pocket Casts are not included — run a Pocket Casts
    import first to fetch the full episode list.
    """
    from django.conf import settings
    from django.shortcuts import get_object_or_404
    from django.utils import timezone

    import hashlib

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

            seen_uuids = set(
                PodcastEpisode.objects.filter(show=show).values_list("episode_uuid", flat=True)
            )
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
                "Failed to fetch full episode list from RSS feed for %s: %s",
                show.title,
                exception_summary(e),
            )

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
        events.tasks.reload_calendar.apply_async(
            kwargs={"item_ids": [item.id for item in items_created]},
            countdown=3,
        )

    episode_word = "episodes" if created_count != 1 else "episode"
    messages.success(
        request,
        f"Marked {created_count} {episode_word} of {show.title} as played",
    )

    return redirect("media_details", source=Sources.POCKETCASTS.value, media_type=MediaTypes.PODCAST.value, media_id=show.podcast_uuid, title=show.slug or show.title)


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
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.PODCAST.value,
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
                    self.in_progress_instance_id = podcast.id if not podcast.end_date else None

                @property
                def completed_play_count(self):
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

                @property
                def has_in_progress_entry(self):
                    return bool(self.in_progress_instance_id)

            podcast_wrapper = PodcastHistoryWrapper(user_podcast, item, all_history)
        else:
            podcast_wrapper = _DummyPodcastWrapper(item)

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

    def _calc_unwatched_runtime_total(
        media,
        episodes_left_count,
        *,
        breakdown_override=None,
        progress_override=None,
    ):
        """Sum actual runtimes for unwatched episodes instead of using averages.

        Returns (total_runtime, episodes_with_data) or (None, 0) if no data available.
        """
        from app.models import Item, MediaTypes

        breakdown = (
            breakdown_override
            if breakdown_override is not None
            else getattr(media, "released_episode_breakdown", {})
        )
        if not breakdown:
            return None, 0

        total_runtime = 0
        episodes_with_runtime_data = 0
        remaining_progress = (
            media.progress if progress_override is None else progress_override
        )

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

    def _get_total_time_left(
        media,
        episodes_left,
        *,
        breakdown_override=None,
        progress_override=None,
    ):
        """Get total time left by summing actual unwatched episode runtimes, with fallback."""
        # First, try to sum actual unwatched episode runtimes
        total_runtime, eps_with_data = _calc_unwatched_runtime_total(
            media,
            episodes_left,
            breakdown_override=breakdown_override,
            progress_override=progress_override,
        )

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

    def _build_time_left_sort_context(media, effective_max):
        """Build a sort-only remaining-episodes view for TV time-left ordering."""
        base_progress = media.progress
        breakdown = getattr(media, "released_episode_breakdown", {}) or {}
        context = {
            "episodes_left": max(effective_max - base_progress, 0),
            "progress": base_progress,
            "breakdown": breakdown,
        }

        if getattr(media, "status", Status.IN_PROGRESS.value) == Status.DROPPED.value:
            return context

        seasons = [
            season
            for season in media.seasons.all()
            if getattr(season.item, "season_number", 0)
        ]
        if not seasons or not breakdown:
            return context

        dropped_season_numbers = {
            season.item.season_number
            for season in seasons
            if season.status == Status.DROPPED.value
        }
        if not dropped_season_numbers:
            return context

        filtered_breakdown = {
            season_num: count
            for season_num, count in breakdown.items()
            if season_num not in dropped_season_numbers
        }
        if filtered_breakdown == breakdown:
            return context

        included_progress = sum(
            season.progress
            for season in seasons
            if season.status != Status.DROPPED.value
        )
        logger.debug(
            "%s: excluding dropped seasons from time_left sort: %s",
            media.item.title,
            sorted(dropped_season_numbers),
        )
        return {
            "episodes_left": max(sum(filtered_breakdown.values()) - included_progress, 0),
            "progress": included_progress,
            "breakdown": filtered_breakdown,
        }

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
        time_left_context = _build_time_left_sort_context(media, effective_max)
        episodes_left = time_left_context["episodes_left"]

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
            group_active.append((media, time_left_context))
            continue

        group_tail.append(media)

    # Sort each group
    # 1) Active by least total minutes left
    def _active_key(entry):
        media, time_left_context = entry
        episodes_left = time_left_context["episodes_left"]
        # Use sum of actual unwatched episode runtimes instead of average
        total = _get_total_time_left(
            media,
            episodes_left,
            breakdown_override=time_left_context["breakdown"],
            progress_override=time_left_context["progress"],
        )
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


@require_GET
def tags_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    episode_number=None,
):
    """Return the modal showing all user tags and allowing to toggle them on an item."""
    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
    )
    lookup = {
        "media_id": media_id,
        "source": source,
        "media_type": tracking_media_type,
        "season_number": season_number,
        "episode_number": episode_number,
    }
    if metadata_resolution.is_grouped_anime_route(media_type, source=source):
        lookup["library_media_type"] = MediaTypes.ANIME.value

    try:
        item = Item.objects.get(**lookup)
    except Item.DoesNotExist:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
            episode_number,
        )
        item = Item.objects.create(
            media_id=media_id,
            source=source,
            media_type=tracking_media_type,
            season_number=season_number,
            episode_number=episode_number,
            library_media_type=metadata.get("library_media_type") or media_type,
            title=metadata["title"],
            image=metadata["image"],
        )

    preview_genres = _parse_detail_tag_preview_genres(
        request.GET.get("preview_genres_json"),
    )
    if not preview_genres:
        preview_genres = _resolve_detail_tag_genres({}, item)

    return render(
        request,
        "app/components/fill_tags.html",
        {
            "item": item,
            "user_tags": _user_tags_for_item(request.user, item),
            "preview_genres_json": json.dumps(preview_genres),
        },
    )


@require_POST
def tag_item_toggle(request):
    """Add or remove a tag from an item."""
    from django.template.loader import render_to_string

    item_id = request.POST["item_id"]
    tag_id = request.POST["tag_id"]

    item = get_object_or_404(Item, id=item_id)
    tag = get_object_or_404(Tag, id=tag_id, user=request.user)

    existing = ItemTag.objects.filter(tag=tag, item=item)
    if existing.exists():
        existing.delete()
        has_tag = False
    else:
        ItemTag.objects.create(tag=tag, item=item)
        has_tag = True

    preview_genres = _parse_detail_tag_preview_genres(
        request.POST.get("preview_genres_json"),
    )
    preview_sections = _build_detail_tag_sections(
        {},
        item,
        request.user,
        fallback_genres=preview_genres,
    )
    button_html = render_to_string(
        "app/components/tag_item_button.html",
        {
            "tag": tag,
            "item": item,
            "has_tag": has_tag,
            "preview_genres_json": json.dumps(preview_genres),
        },
        request=request,
    )
    preview_html = render_to_string(
        "app/components/detail_tag_preview.html",
        {
            "preview_id": app_tags.component_id("tag-preview", item),
            "detail_tag_sections": preview_sections,
            "swap_oob": True,
        },
        request=request,
    )
    return HttpResponse(button_html + preview_html)


@require_POST
def tag_create(request):
    """Create a new tag for the user and optionally apply it to an item."""
    name = (request.POST.get("name") or "").strip()
    item_id = request.POST.get("item_id")

    if not name:
        return HttpResponseBadRequest("Tag name is required.")

    # Check case-insensitive uniqueness
    if Tag.objects.filter(user=request.user, name__iexact=name).exists():
        messages.error(request, f'Tag "{name}" already exists.')
    else:
        tag = Tag.objects.create(user=request.user, name=name)
        if item_id:
            try:
                item = Item.objects.get(id=item_id)
                ItemTag.objects.get_or_create(tag=tag, item=item)
            except Item.DoesNotExist:
                pass

    # Re-render the full tags modal content
    if item_id:
        try:
            item = Item.objects.get(id=item_id)
        except Item.DoesNotExist:
            return HttpResponseBadRequest("Item not found.")

        preview_genres = _parse_detail_tag_preview_genres(
            request.POST.get("preview_genres_json"),
        )
        return _render_tag_modal_response(request, item, preview_genres)

    return HttpResponse(status=204)


@require_POST
def tag_delete(request):
    """Delete a tag owned by the current user and refresh the tag modal."""
    tag_id = request.POST.get("tag_id")
    item_id = request.POST.get("item_id")

    if not tag_id:
        return HttpResponseBadRequest("Tag is required.")

    tag = get_object_or_404(Tag, id=tag_id, user=request.user)
    tag.delete()

    if item_id:
        try:
            item = Item.objects.get(id=item_id)
        except Item.DoesNotExist:
            return HttpResponseBadRequest("Item not found.")

        preview_genres = _parse_detail_tag_preview_genres(
            request.POST.get("preview_genres_json"),
        )
        return _render_tag_modal_response(request, item, preview_genres)

    return HttpResponse(status=204)
