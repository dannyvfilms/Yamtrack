"""
Utility functions and constants shared across list views.

Nothing in this module handles HTTP requests directly — all symbols are
pure helpers called by views in views.py (and its submodules).
"""

import datetime
import logging

from django.apps import apps
from django.conf import settings
from django.db.models import Count, F, Q
from django.urls import reverse

from app.models import Item, MediaManager, MediaTypes
from app.providers import services
from integrations.imports import helpers as import_helpers
from integrations.models import TraktAccount
from lists.models import CustomListItem
from users.models import ListDetailSortChoices, ListSortChoices

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

LIST_REFERENCE_PLACEHOLDER = "__LIST_REFERENCE__"

_MEDIA_TYPE_COLORS = {
    "movie": "#6366f1",
    "tv": "#8b5cf6",
    "season": "#a855f7",
    "episode": "#c084fc",
    "anime": "#ec4899",
    "manga": "#f43f5e",
    "game": "#f97316",
    "book": "#eab308",
    "comic": "#22c55e",
    "boardgame": "#14b8a6",
    "music": "#06b6d4",
    "podcast": "#3b82f6",
}

ASCENDING_LIST_SORTS = {
    ListSortChoices.NAME,
    ListDetailSortChoices.TITLE,
    ListDetailSortChoices.MEDIA_TYPE,
    ListDetailSortChoices.RELEASE_DATE,
    ListDetailSortChoices.START_DATE,
}


# ---------------------------------------------------------------------------
# Media type / list metadata helpers
# ---------------------------------------------------------------------------


def _build_media_type_breakdown(custom_list):
    total = custom_list.items.count()
    if not total:
        return []
    raw = (
        custom_list.items.values("media_type")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    return [
        {
            "value": row["media_type"],
            "label": MediaTypes(row["media_type"]).label,
            "count": row["count"],
            "percent": round(row["count"] / total * 100),
            "color": _MEDIA_TYPE_COLORS.get(row["media_type"], "#6b7280"),
        }
        for row in raw
    ]


def _build_list_url_template(request):
    """Return an absolute list URL template with a replaceable reference placeholder."""
    return request.build_absolute_uri(
        reverse("list_detail", args=[LIST_REFERENCE_PLACEHOLDER]),
    )


def _get_completed_item_ids(user, item_ids):
    """Return the subset of item_ids that the user has marked Completed in any media type."""
    if not item_ids:
        return set()
    completed = set()
    for media_type in MediaTypes.values:
        if media_type == MediaTypes.EPISODE.value:
            continue  # Episode has no status/user field
        try:
            model = apps.get_model("app", media_type)
        except LookupError:
            continue
        completed.update(
            model.objects.filter(
                item_id__in=item_ids,
                user=user,
                status="Completed",
            ).values_list("item_id", flat=True).distinct()
        )
    return completed


def _get_item_last_watched_dates(user, item_ids):
    """Return the latest watched timestamp for each item ID for the current user."""
    if not item_ids:
        return {}

    item_ids_by_media_type = {}
    for item_id, media_type in Item.objects.filter(id__in=item_ids).values_list(
        "id",
        "media_type",
    ):
        item_ids_by_media_type.setdefault(media_type, set()).add(item_id)

    item_last_watched = {}
    try:
        episode_model = apps.get_model("app", MediaTypes.EPISODE.value)
    except LookupError:
        episode_model = None

    if episode_model is not None:
        episode_item_ids = item_ids_by_media_type.get(MediaTypes.EPISODE.value, set())
        if episode_item_ids:
            watch_rows = episode_model.objects.filter(
                item_id__in=episode_item_ids,
                related_season__user=user,
                end_date__isnull=False,
            ).values_list("item_id", "end_date")
            for item_id, end_date in watch_rows:
                current_latest = item_last_watched.get(item_id)
                if current_latest is None or end_date > current_latest:
                    item_last_watched[item_id] = end_date

        season_item_ids = item_ids_by_media_type.get(MediaTypes.SEASON.value, set())
        if season_item_ids:
            watch_rows = episode_model.objects.filter(
                related_season__item_id__in=season_item_ids,
                related_season__user=user,
                end_date__isnull=False,
            ).values_list("related_season__item_id", "end_date")
            for item_id, end_date in watch_rows:
                current_latest = item_last_watched.get(item_id)
                if current_latest is None or end_date > current_latest:
                    item_last_watched[item_id] = end_date

        tv_item_ids = item_ids_by_media_type.get(MediaTypes.TV.value, set())
        if tv_item_ids:
            watch_rows = episode_model.objects.filter(
                related_season__related_tv__item_id__in=tv_item_ids,
                related_season__user=user,
                end_date__isnull=False,
            ).values_list("related_season__related_tv__item_id", "end_date")
            for item_id, end_date in watch_rows:
                current_latest = item_last_watched.get(item_id)
                if current_latest is None or end_date > current_latest:
                    item_last_watched[item_id] = end_date

    for media_type, media_item_ids in item_ids_by_media_type.items():
        if media_type in {
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
            MediaTypes.EPISODE.value,
        }:
            continue

        try:
            model = apps.get_model("app", media_type)
        except LookupError:
            continue

        field_names = {field.name for field in model._meta.fields}
        if not {"item", "user", "end_date"}.issubset(field_names):
            continue

        watch_rows = model.objects.filter(
            item_id__in=media_item_ids,
            user=user,
            end_date__isnull=False,
        ).values_list("item_id", "end_date")

        for item_id, end_date in watch_rows:
            current_latest = item_last_watched.get(item_id)
            if current_latest is None or end_date > current_latest:
                item_last_watched[item_id] = end_date

    return item_last_watched


def _get_list_last_watched_dates(user, list_ids):
    """Return the latest watched timestamp for each list ID."""
    if not list_ids:
        return {}

    item_ids_by_list = {}
    all_item_ids = set()
    for list_id, item_id in CustomListItem.objects.filter(
        custom_list_id__in=list_ids,
    ).values_list("custom_list_id", "item_id"):
        item_ids_by_list.setdefault(list_id, set()).add(item_id)
        all_item_ids.add(item_id)

    item_last_watched = _get_item_last_watched_dates(user, all_item_ids)

    list_last_watched = {}
    for list_id, item_ids in item_ids_by_list.items():
        latest_watch = None
        for item_id in item_ids:
            watched_at = item_last_watched.get(item_id)
            if watched_at is not None and (latest_watch is None or watched_at > latest_watch):
                latest_watch = watched_at
        list_last_watched[list_id] = latest_watch

    return list_last_watched


# ---------------------------------------------------------------------------
# Sort / direction helpers
# ---------------------------------------------------------------------------


def _default_list_sort_direction(sort_by):
    return "asc" if sort_by in ASCENDING_LIST_SORTS else "desc"


def _resolve_list_sort_direction(sort_by, direction):
    if direction in {"asc", "desc"}:
        return direction
    return _default_list_sort_direction(sort_by)


def _order_expression(field_name, direction, *, nulls_last=True):
    field = F(field_name)
    if direction == "asc":
        return field.asc(nulls_last=nulls_last)
    return field.desc(nulls_last=nulls_last)


# ---------------------------------------------------------------------------
# Card image / episode title helpers
# ---------------------------------------------------------------------------


def _resolve_list_card_image_override(item, *, season_item=None):
    """Return a season-first poster override for episode cards when available."""
    if getattr(item, "media_type", None) != MediaTypes.EPISODE.value:
        return None

    media = getattr(item, "media", None)
    related_season = getattr(media, "related_season", None) if media else None
    related_tv = getattr(related_season, "related_tv", None) if related_season else None

    for candidate in (
        getattr(getattr(related_season, "item", None), "image", None),
        getattr(season_item, "image", None),
        getattr(getattr(related_tv, "item", None), "image", None),
        getattr(item, "image", None),
    ):
        if candidate and candidate != settings.IMG_NONE:
            return candidate

    return None


def _list_item_title_fields_from_metadata(media_type, metadata):
    """Return item title fields, preferring episode titles for episode items."""
    metadata = metadata or {}
    if media_type == MediaTypes.EPISODE.value:
        return Item.title_fields_from_episode_metadata(
            metadata,
            fallback_title=metadata.get("title") or "",
        )
    return Item.title_fields_from_metadata(metadata)


def _episode_title_needs_backfill(item, *, season_item=None):
    """Return whether an episode item is still using a parent show title."""
    if getattr(item, "media_type", None) != MediaTypes.EPISODE.value:
        return False
    if getattr(item, "season_number", None) is None or getattr(item, "episode_number", None) is None:
        return False

    media = getattr(item, "media", None)
    related_season = getattr(media, "related_season", None) if media else None
    related_tv = getattr(related_season, "related_tv", None) if related_season else None

    current_title = Item._normalize_title_value(getattr(item, "title", None))
    parent_titles = {
        Item._normalize_title_value(getattr(season_item, "title", None)),
        Item._normalize_title_value(getattr(getattr(related_season, "item", None), "title", None)),
        Item._normalize_title_value(getattr(getattr(related_tv, "item", None), "title", None)),
    }
    parent_titles.discard(None)

    return not current_title or current_title in parent_titles


def _episode_title_fields_from_season_metadata(item, season_metadata):
    """Return episode title fields from a season payload when available."""
    episodes = (season_metadata or {}).get("episodes") or []
    target_episode = str(getattr(item, "episode_number", ""))
    for episode in episodes:
        if str(episode.get("episode_number")) != target_episode:
            continue
        return Item.title_fields_from_episode_metadata(
            episode,
            fallback_title=getattr(item, "title", ""),
        )
    return None


def _maybe_backfill_episode_title(item, *, season_item=None, season_metadata=None, force=False):
    """Resolve malformed episode item titles that still store the show title."""
    if not force and not _episode_title_needs_backfill(item, season_item=season_item):
        return

    title_fields = _episode_title_fields_from_season_metadata(item, season_metadata)

    if title_fields is None:
        try:
            season_metadata = services.get_media_metadata(
                MediaTypes.SEASON.value,
                item.media_id,
                item.source,
                [item.season_number],
            )
        except Exception as exc:
            logger.debug(
                "Could not fetch season metadata for episode title backfill on item %s: %s",
                item.id,
                exc,
            )
        else:
            title_fields = _episode_title_fields_from_season_metadata(item, season_metadata)

    if title_fields is None:
        try:
            metadata = services.get_media_metadata(
                item.media_type,
                item.media_id,
                item.source,
                [item.season_number],
                item.episode_number,
            )
        except Exception as exc:
            logger.debug(
                "Could not backfill episode title for item %s: %s",
                item.id,
                exc,
            )
            return
        title_fields = _list_item_title_fields_from_metadata(item.media_type, metadata)

    if not title_fields:
        return

    update_fields = []
    for field_name, value in title_fields.items():
        if getattr(item, field_name) != value:
            setattr(item, field_name, value)
            update_fields.append(field_name)

    if update_fields:
        item.save(update_fields=update_fields)


def _attach_list_card_overrides(item_list):
    """Attach shared card overrides used by list grid cards."""
    episode_keys = {
        (str(item.media_id), item.source, item.season_number)
        for item in item_list
        if (
            getattr(item, "media_type", None) == MediaTypes.EPISODE.value
            and getattr(item, "season_number", None) is not None
        )
    }

    season_item_by_key = {}
    if episode_keys:
        season_filters = Q()
        for media_id, source, season_number in episode_keys:
            season_filters |= Q(
                media_id=media_id,
                source=source,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
            )
        season_item_by_key = {
            (str(season_item.media_id), season_item.source, season_item.season_number): season_item
            for season_item in Item.objects.filter(season_filters)
        }

    season_metadata_by_key = {}
    for item in item_list:
        item_key = (str(item.media_id), item.source, item.season_number)
        season_item = season_item_by_key.get(item_key)
        item.card_image_override = _resolve_list_card_image_override(
            item,
            season_item=season_item,
        )
        if (
            item_key not in season_metadata_by_key
            and _episode_title_needs_backfill(item, season_item=season_item)
        ):
            try:
                season_metadata_by_key[item_key] = services.get_media_metadata(
                    MediaTypes.SEASON.value,
                    item.media_id,
                    item.source,
                    [item.season_number],
                )
            except Exception as exc:
                logger.debug(
                    "Could not prefetch season metadata for episode title backfill on item %s: %s",
                    item.id,
                    exc,
                )
                season_metadata_by_key[item_key] = None
        _maybe_backfill_episode_title(
            item,
            season_item=season_item,
            season_metadata=season_metadata_by_key.get(item_key),
        )


# ---------------------------------------------------------------------------
# List search result normalization
# ---------------------------------------------------------------------------


def _extract_list_search_results(media_type, data):
    """Normalize provider search payloads for list and recommendation UIs."""
    if media_type != MediaTypes.MUSIC.value:
        return data.get("results", []), data.get("total_pages", 1)

    # MusicBrainz combined search returns tracks under a nested payload.
    track_payload = data.get("tracks") if isinstance(data, dict) else None
    if isinstance(track_payload, dict):
        return track_payload.get("results", []), track_payload.get("total_pages", 1)

    return data.get("results", []), data.get("total_pages", 1)


# ---------------------------------------------------------------------------
# Table adapter helpers
# ---------------------------------------------------------------------------


class _ListTableRowAdapter:
    """Expose list items through the shared media-table row contract."""

    def __init__(self, list_item):
        self._list_item = list_item
        self._source_media = getattr(list_item, "media", None)
        self.item = list_item
        self.id = getattr(self._source_media, "id", None)
        self.track_media_id = self.id
        self.created_at = getattr(list_item, "list_date_added", None)
        self.repeats = getattr(self._source_media, "repeats", 1) or 1

    def __getattr__(self, attr):
        if self._source_media is not None and hasattr(self._source_media, attr):
            return getattr(self._source_media, attr)
        return getattr(self._list_item, attr)


def _adapt_list_items_for_table(items_page):
    """Replace page rows with adapters that satisfy shared media-table cells."""
    items_page.object_list = [
        _ListTableRowAdapter(item) for item in items_page.object_list
    ]
    return items_page


def _resolve_list_table_media_type(selected_media_types, filtered_media_types):
    if len(selected_media_types) == 1:
        return selected_media_types[0]

    unique_filtered_media_types = list(dict.fromkeys(filtered_media_types))
    if len(unique_filtered_media_types) == 1:
        return unique_filtered_media_types[0]

    return "all"


# ---------------------------------------------------------------------------
# Trakt credential helper
# ---------------------------------------------------------------------------


def _get_trakt_credentials(user):
    """Return decrypted Trakt client credentials for a user, if configured."""
    trakt_account = TraktAccount.objects.filter(user=user).first()
    if not trakt_account or not trakt_account.client_id or not trakt_account.client_secret:
        return None
    try:
        client_id = import_helpers.decrypt(trakt_account.client_id)
        client_secret = import_helpers.decrypt(trakt_account.client_secret)
    except Exception:
        logger.error(
            "Failed to decrypt Trakt credentials for user %s",
            user.username,
            exc_info=True,
        )
        return None
    return client_id, client_secret


# ---------------------------------------------------------------------------
# Media aggregation helpers (shared by smart-list and regular-list detail views)
# ---------------------------------------------------------------------------


def _attach_media_with_aggregation(item_list, media_user):
    """Attach `.media` to each item in item_list using the given user's library data.

    Uses the smart-list version of Episode normalization so that Episode entries
    expose `status`, `score`, `progress`, and `max_progress` compatible with list
    card templates.
    """
    media_by_item_id = {}
    media_types_in_items = {item.media_type for item in item_list}
    media_manager = MediaManager()

    for media_type in media_types_in_items:
        model = apps.get_model("app", media_type)
        item_ids = [item.id for item in item_list if item.media_type == media_type]
        if not item_ids:
            continue

        if media_type == MediaTypes.EPISODE.value:
            filter_kwargs = {
                "item_id__in": item_ids,
                "related_season__user": media_user,
            }
        else:
            filter_kwargs = {
                "item_id__in": item_ids,
                "user": media_user,
            }

        select_related_fields = ["item"]
        if media_type == MediaTypes.EPISODE.value:
            select_related_fields.extend(
                [
                    "related_season",
                    "related_season__item",
                    "related_season__related_tv",
                    "related_season__related_tv__item",
                ],
            )
        queryset = model.objects.filter(**filter_kwargs).select_related(*select_related_fields)
        queryset = media_manager._apply_prefetch_related(queryset, media_type)
        media_manager.annotate_max_progress(queryset, media_type)

        entries_by_item = {}
        for entry in queryset:
            if media_type == MediaTypes.EPISODE.value:
                # Episode does not inherit Media; expose compatible fields for list templates.
                if not hasattr(entry, "status"):
                    entry.status = getattr(entry.related_season, "status", None)
                if not hasattr(entry, "score"):
                    entry.score = None
                if not hasattr(entry, "progress"):
                    entry.progress = entry.item.episode_number
                if not hasattr(entry, "max_progress"):
                    entry.max_progress = getattr(entry.related_season, "max_progress", None)
            entries_by_item.setdefault(entry.item_id, []).append(entry)

        for item_id, entries in entries_by_item.items():
            entries.sort(key=lambda entry: entry.created_at, reverse=True)
            display_media = entries[0]
            if len(entries) > 1:
                media_manager._aggregate_item_data(display_media, entries)
            media_by_item_id[item_id] = display_media

    for item in item_list:
        item.media = media_by_item_id.get(item.id)
    _attach_list_card_overrides(item_list)


def _rating_value(media):
    if not media:
        return -1
    aggregated_score = getattr(media, "aggregated_score", None)
    if aggregated_score is not None:
        return aggregated_score
    score = getattr(media, "score", None)
    if score is not None:
        return score
    return -1


def _progress_value(media):
    if not media:
        return -1
    aggregated_progress = getattr(media, "aggregated_progress", None)
    if aggregated_progress is not None:
        return aggregated_progress
    progress = getattr(media, "progress", None)
    if progress is not None:
        return progress
    return -1


def _media_date_value(media, attr_name):
    if not media:
        return None
    aggregated_value = getattr(media, f"aggregated_{attr_name}", None)
    if aggregated_value is not None:
        return aggregated_value
    return getattr(media, attr_name, None)


def _date_sort_value(value, direction):
    if value is None:
        return float("inf") if direction == "asc" else float("-inf")
    if isinstance(value, datetime.datetime):
        return value.timestamp()
    if isinstance(value, datetime.date):
        return datetime.datetime.combine(value, datetime.time.min).timestamp()
    return float("inf") if direction == "asc" else float("-inf")
