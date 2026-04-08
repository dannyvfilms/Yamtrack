import datetime
import json
import logging
import secrets
from urllib.parse import urlencode

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_not_required, login_required
from django.core.paginator import Paginator
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q, Subquery
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from app import helpers
from app.columns import (
    resolve_column_config,
    resolve_columns,
    resolve_default_column_config,
    sanitize_column_prefs,
)
from app.discover import tab_cache as discover_tab_cache
from app.models import Item, MediaManager, MediaTypes
from app.providers import services
from app.services import metadata_resolution
from integrations.imports import helpers as import_helpers
from integrations.imports import trakt as trakt_imports
from integrations.models import TraktAccount
from lists.forms import CustomListForm
from lists.imports import trakt as trakt_lists
from lists import smart_rules, tasks as list_tasks
from lists.models import (
    CustomList,
    CustomListItem,
    ListActivity,
    ListActivityType,
    ListRecommendation,
)
from users.models import ListDetailSortChoices, ListSortChoices, MediaStatusChoices

logger = logging.getLogger(__name__)


User = get_user_model()


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


ASCENDING_LIST_SORTS = {
    ListSortChoices.NAME,
    ListDetailSortChoices.TITLE,
    ListDetailSortChoices.MEDIA_TYPE,
    ListDetailSortChoices.RELEASE_DATE,
    ListDetailSortChoices.START_DATE,
}


def _default_list_sort_direction(sort_by):
    return "asc" if sort_by in ASCENDING_LIST_SORTS else "desc"


def _resolve_list_sort_direction(sort_by, direction):
    if direction in {"asc", "desc"}:
        return direction
    return _default_list_sort_direction(sort_by)


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


def _order_expression(field_name, direction, *, nulls_last=True):
    field = F(field_name)
    if direction == "asc":
        return field.asc(nulls_last=nulls_last)
    return field.desc(nulls_last=nulls_last)


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


@login_not_required
@never_cache
@require_GET
def user_profile(request, username):
    """Return the public profile page showing all public lists for a user."""
    profile_user = get_object_or_404(User, username=username)

    # Get all public lists owned by this user
    # Use a fresh query each time to avoid any caching issues
    public_lists = list(
        CustomList.objects.filter(
            owner=profile_user,
            visibility="public",
        )
        .select_related("owner")
        .annotate(
            items_count=Count("items", distinct=True),
        )
        .prefetch_related("collaborators", "items")
        .order_by("-id")
    )

    tag_map = {}
    for custom_list in public_lists:
        tags = [
            tag.strip()
            for tag in (custom_list.tags or [])
            if isinstance(tag, str) and tag.strip()
        ]
        if not tags:
            tag_map.setdefault("Untagged", []).append(custom_list)
            continue
        for tag in tags:
            tag_map.setdefault(tag, []).append(custom_list)

    def _tag_sort_key(tag_name):
        return (tag_name == "Untagged", tag_name.lower())

    tag_sections = [
        {"tag": tag_name, "lists": tag_map[tag_name]}
        for tag_name in sorted(tag_map, key=_tag_sort_key)
    ]

    # Determine if this is the current user's own profile
    is_own_profile = request.user.is_authenticated and request.user == profile_user

    # Determine base template: use public template for anonymous users, regular for authenticated
    public_view = not request.user.is_authenticated
    base_template = "base_public.html" if public_view else "base.html"

    return render(
        request,
        "lists/user_profile.html",
        {
            "profile_user": profile_user,
            "custom_lists": public_lists,
            "tag_sections": tag_sections,
            "is_own_profile": is_own_profile,
            "public_view": public_view,
            "base_template": base_template,
            "profile_username": username,
        },
    )


@never_cache
@require_GET
def lists(request):
    """Return the custom list page."""
    # Get parameters from request
    search_query = request.GET.get("q", "")
    page = request.GET.get("page", 1)
    previous_sort = getattr(request.user, "lists_sort", ListSortChoices.LAST_ITEM_ADDED)
    sort_by = request.user.update_preference("lists_sort", request.GET.get("sort"))
    if sort_by not in ListSortChoices.values:
        sort_by = ListSortChoices.LAST_ITEM_ADDED
    direction_param = request.GET.get("direction")
    direction_pref = getattr(request.user, "lists_direction", None)
    if direction_param is not None:
        direction = _resolve_list_sort_direction(sort_by, direction_param)
    elif sort_by != previous_sort or direction_pref is None:
        direction = _resolve_list_sort_direction(sort_by, None)
    else:
        direction = _resolve_list_sort_direction(sort_by, direction_pref)
    request.user.update_preference("lists_direction", direction)
    enabled_media_types = request.user.get_enabled_media_types()
    selected_media_type = request.GET.get("media_type", "all")

    if selected_media_type != "all" and selected_media_type not in enabled_media_types:
        selected_media_type = "all"

    # Start with base queryset and annotate items_count first (before prefetch)
    # This ensures the count is accurate and not affected by prefetch cache
    custom_lists = (
        CustomList.objects.filter(Q(owner=request.user) | Q(collaborators=request.user))
        .select_related("owner")
        .annotate(
            items_count=Count("items", distinct=True),
        )
        .distinct()
    )

    if search_query:
        custom_lists = custom_lists.filter(
            Q(name__icontains=search_query) | Q(description__icontains=search_query),
        )

    if selected_media_type != "all":
        custom_lists = custom_lists.annotate(
            has_media_type=Exists(
                CustomListItem.objects.filter(
                    custom_list_id=OuterRef("pk"),
                    item__media_type=selected_media_type,
                ),
            ),
        ).filter(has_media_type=True).distinct()

    # Add prefetch after annotations to avoid interfering with counts
    # This is for the list image property which uses items.first()
    custom_lists = custom_lists.prefetch_related(
        "collaborators",
        Prefetch(
            "customlistitem_set",
            queryset=CustomListItem.objects.select_related("item").order_by("-date_added"),
        ),
    )
    
    if sort_by == ListSortChoices.NAME:
        custom_lists = custom_lists.order_by(_order_expression("name", direction))
    elif sort_by == ListSortChoices.ITEMS_COUNT:
        custom_lists = custom_lists.order_by(
            _order_expression("items_count", direction),
            F("name").asc(),
        )
    elif sort_by == ListSortChoices.NEWEST_FIRST:
        custom_lists = custom_lists.order_by(_order_expression("id", direction))
    elif sort_by == ListSortChoices.LAST_WATCHED:
        list_last_watched = _get_list_last_watched_dates(
            request.user,
            list(custom_lists.values_list("id", flat=True)),
        )
        custom_lists = list(custom_lists)
        for custom_list in custom_lists:
            custom_list.last_watched_at = list_last_watched.get(custom_list.id)
        custom_lists.sort(
            key=lambda custom_list: (
                custom_list.last_watched_at is None,
                (
                    custom_list.last_watched_at.timestamp()
                    if direction == "asc"
                    else -custom_list.last_watched_at.timestamp()
                )
                if custom_list.last_watched_at is not None
                else 0,
                custom_list.name.casefold(),
            ),
        )
    else:  # last_item_added is the default
        # Get the latest update date for each list
        custom_lists = custom_lists.annotate(
            latest_update=Subquery(
                CustomListItem.objects.filter(
                    custom_list=OuterRef("pk"),
                )
                .order_by("-date_added")
                .values("date_added")[:1],
            ),
        ).order_by(_order_expression("latest_update", direction), F("name").asc())
    
    items_per_page = 20
    paginator = Paginator(custom_lists, items_per_page)
    lists_page = paginator.get_page(page)

    available_tags = CustomListForm._normalize_tags(
        tag
        for custom_list in CustomList.objects.filter(
            Q(owner=request.user) | Q(collaborators=request.user),
        ).only("tags")
        for tag in (custom_list.tags or [])
    )

    # Compute completion percentages for each list (titles completed / total titles)
    page_list_ids = [custom_list.id for custom_list in lists_page]
    list_item_pairs = CustomListItem.objects.filter(
        custom_list_id__in=page_list_ids,
    ).values_list("custom_list_id", "item_id")

    item_ids_by_list = {}
    all_item_ids = set()
    for list_id, item_id in list_item_pairs:
        item_ids_by_list.setdefault(list_id, set()).add(item_id)
        all_item_ids.add(item_id)

    completed_item_ids = _get_completed_item_ids(request.user, all_item_ids)
    for cl in lists_page:
        list_item_ids = item_ids_by_list.get(cl.id, set())
        if list_item_ids:
            n_done = len(list_item_ids & completed_item_ids)
            cl.completed_count = n_done
            cl.completion_percent = round(n_done / len(list_item_ids) * 100)
        else:
            cl.completed_count = 0
            cl.completion_percent = None

    # Create a form for each list
    # needs unique id for django-select2
    for custom_list in lists_page:
        try:
            custom_list.form = CustomListForm(
                instance=custom_list,
                auto_id=f"id_{custom_list.id}_%s",
                user=request.user,
                available_tags=available_tags,
            )
        except Exception as e:
            logger.error(
                "Error creating form for list ID %s: %s",
                custom_list.id,
                e,
                exc_info=True,
            )
            # Skip form creation for this list
            custom_list.form = None

    # Add timestamp to context for cache busting
    import time
    cache_buster = int(time.time())
    
    if request.headers.get("HX-Request"):
        response = render(
            request,
            "lists/components/list_grid.html",
            {
                "custom_lists": lists_page,
                "current_sort": sort_by,
                "current_direction": direction,
                "cache_buster": cache_buster,
            },
        )
        # Explicitly set cache control headers for HTMX responses
        response["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        response["Vary"] = "Cookie, HX-Request"
        response["X-Cache-Buster"] = str(cache_buster)
        return response

    create_list_form = CustomListForm(
        user=request.user,
        available_tags=available_tags,
    )
    trakt_redirect_uri = request.build_absolute_uri(reverse("trakt_lists_callback"))
    trakt_account = TraktAccount.objects.filter(user=request.user).first()

    response = render(
        request,
        "lists/custom_lists.html",
        {
            "custom_lists": lists_page,
            "form": create_list_form,
            "current_sort": sort_by,
            "current_direction": direction,
            "sort_choices": ListSortChoices.choices,
            "media_types": enabled_media_types,
            "current_media_type": selected_media_type,
            "trakt_redirect_uri": trakt_redirect_uri,
            "trakt_account": trakt_account,
            "trakt_has_credentials": bool(trakt_account and trakt_account.is_configured),
            "cache_buster": cache_buster,
        },
    )
    # Explicitly set cache control headers for Safari compatibility
    # @never_cache should handle this, but Safari can be aggressive with caching
    response["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    response["Vary"] = "Cookie"
    response["X-Cache-Buster"] = str(cache_buster)
    return response


@login_required
@require_POST
def trakt_lists_credentials(request):
    """Store Trakt client credentials for list imports."""
    client_id = request.POST.get("client_id", "").strip()
    client_secret = request.POST.get("client_secret", "").strip()

    if not client_id or not client_secret:
        messages.error(request, "Trakt client ID and secret are required.")
        return redirect("lists")

    try:
        TraktAccount.objects.update_or_create(
            user=request.user,
            defaults={
                "client_id": import_helpers.encrypt(client_id),
                "client_secret": import_helpers.encrypt(client_secret),
            },
        )
    except Exception as error:
        logger.error("Failed to store Trakt credentials for user %s: %s", request.user.username, error)
        messages.error(request, "Failed to save Trakt credentials. Please try again.")
        return redirect("lists")

    messages.success(request, "Trakt credentials saved. You can now authorize Trakt.")
    return redirect("lists")


@login_required
@require_POST
def trakt_lists_oauth(request):
    """Start the Trakt OAuth flow for list imports."""
    redirect_uri = request.build_absolute_uri(reverse("trakt_lists_callback"))
    credentials = _get_trakt_credentials(request.user)
    if not credentials:
        messages.error(request, "Add your Trakt client ID and secret before authorizing.")
        return redirect("lists")

    client_id, _client_secret = credentials
    state_token = secrets.token_urlsafe(32)
    request.session[state_token] = {"source": "trakt_lists"}
    request.session.modified = True
    
    # Build query string manually to match the working trakt_oauth pattern
    # This ensures the redirect_uri is sent exactly as registered
    url = "https://trakt.tv/oauth/authorize"
    logger.debug(f"Trakt OAuth redirect URI: {redirect_uri}")
    
    return redirect(
        f"{url}?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&state={state_token}",
    )


@login_required
@require_GET
def trakt_lists_callback(request):
    """Handle Trakt OAuth callback and import lists."""
    state_token = request.GET.get("state")
    
    if not state_token:
        logger.error("Trakt OAuth callback missing state parameter")
        messages.error(request, "Invalid Trakt authorization request. Missing state parameter.")
        return redirect("lists")
    
    state_data = request.session.pop(state_token, None)

    if not state_data:
        logger.error(f"Trakt OAuth callback: state token '{state_token}' not found in session")
        messages.error(
            request,
            "Invalid or expired Trakt authorization request. Please try again - make sure to complete the authorization process without closing your browser.",
        )
        return redirect("lists")

    credentials = _get_trakt_credentials(request.user)
    if not credentials:
        messages.error(request, "Trakt credentials are missing. Please add them and try again.")
        return redirect("lists")

    client_id, client_secret = credentials

    try:
        oauth_callback = trakt_imports.handle_oauth_callback(
            request,
            redirect_uri=request.build_absolute_uri(reverse("trakt_lists_callback")),
            client_id=client_id,
            client_secret=client_secret,
        )
        # Queue the import task asynchronously so we can redirect immediately
        list_tasks.import_trakt_lists_task.delay(
            request.user.id,
            oauth_callback["access_token"],
            client_id=client_id,
        )
        messages.info(request, "Trakt authorization successful. Your lists are being imported in the background.")
    except import_helpers.MediaImportError as error:
        messages.error(request, f"Trakt list import failed: {error}")
        return redirect("lists")

    return redirect("lists")


def _smart_list_detail_response(
    request,
    custom_list,
    can_edit,
    is_public_view,
    public_view,
    media_user,
):
    """Render smart-list detail page and HTMX partial responses."""
    valid_sorts = [choice[0] for choice in ListDetailSortChoices.choices]
    sort_by = request.GET.get("sort", ListDetailSortChoices.DATE_ADDED)
    if sort_by not in valid_sorts:
        sort_by = ListDetailSortChoices.DATE_ADDED
    direction = _resolve_list_sort_direction(
        sort_by,
        request.GET.get("direction"),
    )

    layout = request.GET.get("layout", "grid")
    if layout not in {"grid", "table"}:
        layout = "grid"

    page = request.GET.get("page", 1)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 1

    recommendation_count = 0
    if can_edit and custom_list.allow_recommendations:
        recommendation_count = custom_list.recommendations.count()

    smart_edit_mode = can_edit and str(request.GET.get("edit_smart_rules", "")).lower() in {
        "1",
        "true",
        "yes",
    }

    saved_rules = smart_rules.normalize_rule_payload(
        {
            "media_types": custom_list.smart_media_types or [],
            **(custom_list.smart_filters or {}),
        },
        custom_list.owner,
    )

    active_rules = dict(saved_rules)
    allow_request_filters = smart_edit_mode or is_public_view
    if allow_request_filters:
        request_media_types = saved_rules["media_types"]
        if request.GET.get("type_mode") == "all":
            request_media_types = []
        elif "type" in request.GET:
            request_media_types = request.GET.getlist("type")

        active_rules = smart_rules.normalize_rule_payload(
            {
                "media_types": request_media_types,
                "status": request.GET.get("status", saved_rules["status"]),
                "rating": request.GET.get("rating", saved_rules["rating"]),
                "collection": request.GET.get("collection", saved_rules["collection"]),
                "genre": request.GET.get("genre", saved_rules["genre"]),
                "year": request.GET.get("year", saved_rules["year"]),
                "release": request.GET.get("release", saved_rules["release"]),
                "source": request.GET.get("source", saved_rules["source"]),
                "language": request.GET.get("language", saved_rules["language"]),
                "country": request.GET.get("country", saved_rules["country"]),
                "platform": request.GET.get("platform", saved_rules["platform"]),
                "origin": request.GET.get("origin", saved_rules["origin"]),
                "format": request.GET.get("format", saved_rules["format"]),
                "tag": request.GET.get("tag", saved_rules["tag"]),
                "tag_exclude": request.GET.get("tag_exclude", saved_rules["tag_exclude"]),
                "search": request.GET.get("q", saved_rules["search"]),
            },
            custom_list.owner,
        )

    matched_item_ids = smart_rules.collect_matching_item_ids(custom_list.owner, active_rules)
    items = Item.objects.filter(id__in=matched_item_ids).annotate(
        list_date_added=Subquery(
            CustomListItem.objects.filter(
                custom_list=custom_list,
                item_id=OuterRef("pk"),
            )
            .order_by("-date_added")
            .values("date_added")[:1],
        ),
    )
    total_items_count = items.count()
    filtered_media_types = list(items.values_list("media_type", flat=True).distinct())

    def _attach_media_with_aggregation(item_list):
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

    sort_mapping = {
        ListDetailSortChoices.DATE_ADDED: [
            _order_expression("list_date_added", direction),
            _order_expression("title", direction),
        ],
        ListDetailSortChoices.TITLE: [
            _order_expression("title", direction),
            F("season_number").asc(nulls_first=True)
            if direction == "asc"
            else F("season_number").desc(nulls_last=True),
            F("episode_number").asc(nulls_first=True)
            if direction == "asc"
            else F("episode_number").desc(nulls_last=True),
        ],
        ListDetailSortChoices.MEDIA_TYPE: [
            _order_expression("media_type", direction),
        ],
        ListDetailSortChoices.RATING: [
            _order_expression("list_date_added", direction),
        ],
        ListDetailSortChoices.PROGRESS: [
            _order_expression("list_date_added", direction),
        ],
        ListDetailSortChoices.RELEASE_DATE: [
            _order_expression("release_datetime", direction),
            _order_expression("title", direction),
        ],
        ListDetailSortChoices.START_DATE: [
            _order_expression("list_date_added", direction),
        ],
        ListDetailSortChoices.END_DATE: [
            _order_expression("list_date_added", direction),
        ],
    }
    media_sort_config = {
        ListDetailSortChoices.RATING: {
            "key": lambda item: _rating_value(item.media),
            "reverse": direction == "desc",
        },
        ListDetailSortChoices.PROGRESS: {
            "key": lambda item: _progress_value(item.media),
            "reverse": direction == "desc",
        },
        ListDetailSortChoices.START_DATE: {
            "key": lambda item: _date_sort_value(
                _media_date_value(item.media, "start_date"),
                direction,
            ),
            "reverse": direction == "desc",
        },
        ListDetailSortChoices.END_DATE: {
            "key": lambda item: _date_sort_value(
                _media_date_value(item.media, "end_date"),
                direction,
            ),
            "reverse": direction == "desc",
        },
    }

    sort_config = media_sort_config.get(sort_by)
    if sort_config:
        all_items = list(items.order_by(*sort_mapping.get(sort_by, sort_mapping[ListDetailSortChoices.DATE_ADDED])))
        _attach_media_with_aggregation(all_items)
        all_items = sorted(
            all_items,
            key=sort_config["key"],
            reverse=sort_config["reverse"],
        )
        paginator = Paginator(all_items, 16)
        items_page = paginator.get_page(page)
        filtered_items_count = paginator.count
    else:
        items = items.order_by(*sort_mapping.get(sort_by, sort_mapping[ListDetailSortChoices.DATE_ADDED]))
        paginator = Paginator(items, 16)
        items_page = paginator.get_page(page)
        filtered_items_count = paginator.count
        _attach_media_with_aggregation(items_page)

    if layout == "table":
        _adapt_list_items_for_table(items_page)

    status_choices = [("all", "All"), *[
        (value, label)
        for value, label in MediaStatusChoices.choices
        if value != MediaStatusChoices.ALL
    ]]
    sort_choices = sorted(ListDetailSortChoices.choices, key=lambda x: x[1])

    filter_data = smart_rules.build_rule_filter_data(
        owner=custom_list.owner,
        media_types=active_rules["media_types"],
        status=active_rules["status"],
        search=active_rules["search"],
    )
    available_media_types = sorted(
        smart_rules.get_available_media_types(custom_list.owner),
        key=lambda v: MediaTypes(v).label,
    )
    available_media_type_labels = {
        media_type: MediaTypes(media_type).label
        for media_type in available_media_types
    }

    is_partial = bool(request.headers.get("HX-Request"))
    is_pagination = is_partial and page > 1
    has_active_filters = bool(active_rules.get("media_types")) or any(
        [
            active_rules.get("status") not in {"", "all"},
            active_rules.get("rating") not in {"", "all"},
            active_rules.get("collection") not in {"", "all"},
            active_rules.get("genre"),
            active_rules.get("year"),
            active_rules.get("release") not in {"", "all"},
            active_rules.get("source"),
            active_rules.get("language"),
            active_rules.get("country"),
            active_rules.get("platform"),
            active_rules.get("origin"),
            active_rules.get("search"),
        ],
    )
    current_media_type = _resolve_list_table_media_type(
        active_rules["media_types"],
        filtered_media_types,
    )
    context = {
        "user": request.user,
        "custom_list": custom_list,
        "items": items_page,
        "has_next": items_page.has_next(),
        "next_page_number": items_page.next_page_number()
        if items_page.has_next()
        else None,
        "items_count": total_items_count,
        "filtered_items_count": filtered_items_count,
        "current_sort": sort_by,
        "current_direction": direction,
        "chip_sort": "score" if sort_by == ListDetailSortChoices.RATING else sort_by,
        "current_status": active_rules["status"],
        "current_layout": layout,
        "sort_choices": sort_choices,
        "status_choices": status_choices,
        "public_view": public_view,
        "can_edit": can_edit,
        "list_ordering_enabled": can_edit and sort_by == ListDetailSortChoices.CUSTOM,
        "is_public_view": is_public_view,
        "recommendation_count": recommendation_count,
        "base_template": "base_public.html" if public_view else "base.html",
        "is_partial": is_partial,
        "is_pagination": is_pagination,
        "is_smart_list": True,
        "smart_edit_mode": smart_edit_mode,
        "saved_smart_rules": saved_rules,
        "active_smart_rules": active_rules,
        "smart_filter_data": filter_data,
        "available_media_types": available_media_types,
        "available_media_type_labels": available_media_type_labels,
        "current_media_types": active_rules["media_types"],
        "has_media_type_filter": bool(active_rules["media_types"]),
        "has_active_filters": has_active_filters,
        "collaborators_count": custom_list.collaborators.count() + 1,
        "column_config": resolve_column_config(
            current_media_type,
            sort_by,
            request.user,
            "list",
        ),
        "default_column_config": resolve_default_column_config(
            current_media_type,
            sort_by,
            "list",
        ),
        "table_type": "list",
        "table_column_update_url": reverse(
            "list_detail_columns",
            args=[custom_list.id],
        ),
        "table_column_media_type": current_media_type,
        "table_refresh_url": reverse("list_detail", args=[custom_list.id]),
        "table_refresh_target": "#items-view",
        "table_refresh_include_selector": "#smart-filter-form",
    }

    if layout == "table":
        context.update(
            {
                "media_list": items_page,
                "resolved_columns": resolve_columns(
                    current_media_type,
                    sort_by,
                    request.user,
                    "list",
                ),
                "table_body_id": "list-table-body",
                "table_pagination_url": reverse("list_detail", args=[custom_list.id]),
                "table_target_selector": "#list-table-body",
                "table_include_selector": "#smart-filter-form",
            },
        )

    if is_partial:
        if layout == "table":
            if is_pagination:
                return render(request, "app/components/table_items.html", context)
            return render(request, "lists/components/list_table.html", context)
        return render(request, "lists/components/media_grid.html", context)

    if can_edit:
        context["form"] = CustomListForm(instance=custom_list, user=request.user)
    else:
        context["form"] = None
    return render(request, "lists/smart_list_detail.html", context)


@login_not_required
@never_cache
@require_GET
def list_detail(request, list_id):
    """Return the detail page of a custom list."""
    try:
        custom_list = CustomList.objects.select_related("owner").prefetch_related(
            "collaborators"
        ).get(id=list_id)
    except CustomList.DoesNotExist:
        # List doesn't exist - investigate why it might have been shown on lists page
        logger.warning(
            "List ID %s not found. User: %s, Authenticated: %s",
            list_id,
            request.user.username if request.user.is_authenticated else "anonymous",
            request.user.is_authenticated,
        )
        
        # Check if user has any lists that might match (for debugging)
        if request.user.is_authenticated:
            user_lists = CustomList.objects.get_user_lists(request.user)
            logger.info(
                "User %s has %s accessible lists. Checking if list %s should be in that set...",
                request.user.username,
                user_lists.count(),
                list_id,
            )
            
            # Check if there's a list with similar characteristics that was re-imported
            # This helps identify if it's a re-import issue
            trakt_lists = CustomList.objects.filter(
                owner=request.user,
                source="trakt",
            )
            logger.info(
                "User has %s Trakt lists. Recent list IDs: %s",
                trakt_lists.count(),
                list(trakt_lists.order_by("-id")[:5].values_list("id", flat=True)),
            )
            
            messages.error(
                request,
                f"List ID {list_id} not found. This may indicate a data inconsistency. "
                "The list may have been deleted or re-imported with a new ID. "
                "Please refresh the lists page to see current lists.",
            )
            return redirect("lists")
        # For anonymous users, just show 404
        raise Http404("List not found")

    # Check access: public lists are viewable by anyone, private lists require auth
    if not custom_list.user_can_view(request.user):
        if custom_list.visibility == "private":
            # Private list - show 404 with message
            msg = "This list is private."
            raise Http404(msg)
        # Should not reach here, but handle gracefully
        msg = "List not found"
        raise Http404(msg)

    if custom_list.is_smart:
        custom_list.sync_smart_items()

    # Determine if this is a public view (anonymous user viewing public list)
    can_edit = custom_list.user_can_edit(request.user)
    is_public_view = custom_list.visibility == "public" and not can_edit
    public_view = not request.user.is_authenticated and custom_list.visibility == "public"

    # Determine which user's data to use for media queries
    # For public views, use owner's data; otherwise use request.user
    media_user = custom_list.owner if is_public_view else request.user

    if custom_list.is_smart:
        return _smart_list_detail_response(
            request=request,
            custom_list=custom_list,
            can_edit=can_edit,
            is_public_view=is_public_view,
            public_view=public_view,
            media_user=media_user,
        )

    # Get and process request parameters
    # Handle anonymous users by using default values
    valid_sorts = [choice[0] for choice in ListDetailSortChoices.choices]
    valid_statuses = [choice[0] for choice in MediaStatusChoices.choices]

    if request.user.is_authenticated:
        sort_by = request.user.update_preference(
            "list_detail_sort",
            request.GET.get("sort"),
        )
        if sort_by not in valid_sorts:
            sort_by = "date_added"
    else:
        # Default sort for anonymous users
        sort_by = request.GET.get("sort", "date_added")
        # Validate sort choice
        if sort_by not in valid_sorts:
            sort_by = "date_added"
    direction = _resolve_list_sort_direction(
        sort_by,
        request.GET.get("direction"),
    )

    if request.user.is_authenticated:
        status_filter = request.user.update_preference(
            "list_detail_status",
            request.GET.get("status"),
        )
        if status_filter not in valid_statuses:
            status_filter = MediaStatusChoices.ALL
    else:
        status_filter = request.GET.get("status", MediaStatusChoices.ALL)
        if status_filter not in valid_statuses:
            status_filter = MediaStatusChoices.ALL

    selected_media_types = request.GET.getlist("type")
    if not selected_media_types:
        legacy_media_type = request.GET.get("type", "all")
        if legacy_media_type and legacy_media_type != "all":
            selected_media_types = [legacy_media_type]
    layout = request.GET.get("layout", "grid")
    if layout not in {"grid", "table"}:
        layout = "grid"
    valid_media_types = set(MediaTypes.values)
    selected_media_types = [
        media_type for media_type in selected_media_types if media_type in valid_media_types
    ]

    params = {
        "sort_by": sort_by,
        "direction": direction,
        "media_types": selected_media_types,
        "status_filter": status_filter,
        "page": int(request.GET.get("page", 1)),
        "search_query": request.GET.get("q", ""),
    }

    # Build and filter base queryset
    items = custom_list.items.all()
    total_items_count = items.count()

    # Compute completion percentage (titles completed / total titles)
    completion_percent = None
    completed_count = 0
    if total_items_count > 0 and not is_public_view:
        all_item_ids = set(custom_list.items.values_list("id", flat=True))
        completed_ids = _get_completed_item_ids(request.user, all_item_ids)
        completed_count = len(completed_ids)
        completion_percent = round(completed_count / total_items_count * 100)

    if params["search_query"]:
        items = items.filter(title__icontains=params["search_query"])
    if params["media_types"]:
        items = items.filter(media_type__in=params["media_types"])
    items = items.annotate(
        list_date_added=Subquery(
            CustomListItem.objects.filter(
                custom_list=custom_list,
                item_id=OuterRef("pk"),
            )
            .order_by("-date_added")
            .values("date_added")[:1],
        ),
    )

    def _attach_media_with_aggregation(item_list):
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

            queryset = model.objects.filter(**filter_kwargs).select_related(
                *select_related_fields,
            )
            queryset = media_manager._apply_prefetch_related(queryset, media_type)
            media_manager.annotate_max_progress(queryset, media_type)

            entries_by_item = {}
            for entry in queryset:
                entries_by_item.setdefault(entry.item_id, []).append(entry)

            for item_id, entries in entries_by_item.items():
                entries.sort(key=lambda e: e.created_at, reverse=True)
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

    # Get distinct media types for filtering
    media_types = items.values_list("media_type", flat=True).distinct()
    media_manager = MediaManager()
    media_by_item_id = {}

    # Filter by status if specified
    if params["status_filter"] != MediaStatusChoices.ALL:
        item_ids = items.values_list("id", flat=True)
        media_by_item_id = media_manager.fetch_media_for_items(
            media_types,
            item_ids,
            media_user,
            status_filter=params["status_filter"],
        )
        # Filter items to only those with the specified status
        items = items.filter(id__in=media_by_item_id.keys())
    filtered_media_types = list(items.values_list("media_type", flat=True).distinct())

    # Apply sorting
    sort_mapping = {
        "date_added": [
            _order_expression("customlistitem__date_added", params["direction"]),
            _order_expression("title", params["direction"]),
        ],
        "custom": ["customlistitem__date_added", "customlistitem__id"],
        "title": [
            _order_expression("title", params["direction"]),
            F("season_number").asc(nulls_first=True)
            if params["direction"] == "asc"
            else F("season_number").desc(nulls_last=True),
            F("episode_number").asc(nulls_first=True)
            if params["direction"] == "asc"
            else F("episode_number").desc(nulls_last=True),
        ],
        "media_type": [_order_expression("media_type", params["direction"])],
        "rating": [
            _order_expression("customlistitem__date_added", params["direction"]),
        ],  # Fallback before media-based sorting
        "release_date": [
            _order_expression("release_datetime", params["direction"]),
            _order_expression("title", params["direction"]),
        ],
    }

    media_sort_config = {
        "rating": {
            "key": lambda item: _rating_value(item.media),
            "reverse": params["direction"] == "desc",
        },
        "progress": {
            "key": lambda item: _progress_value(item.media),
            "reverse": params["direction"] == "desc",
        },
        "start_date": {
            "key": lambda item: _date_sort_value(
                _media_date_value(item.media, "start_date"),
                params["direction"],
            ),
            "reverse": params["direction"] == "desc",
        },
        "end_date": {
            "key": lambda item: _date_sort_value(
                _media_date_value(item.media, "end_date"),
                params["direction"],
            ),
            "reverse": params["direction"] == "desc",
        },
    }

    sort_config = media_sort_config.get(params["sort_by"])
    if sort_config:
        all_items = list(
            items.order_by(
                *sort_mapping.get(
                    params["sort_by"],
                    ["-customlistitem__date_added"],
                ),
            ),
        )
        _attach_media_with_aggregation(all_items)

        all_items = sorted(
            all_items,
            key=sort_config["key"],
            reverse=sort_config["reverse"],
        )

        paginator = Paginator(all_items, 16)
        items_page = paginator.get_page(params["page"])
        filtered_items_count = paginator.count
    else:
        # For database-backed sorts, apply ordering and paginate normally
        items = items.order_by(
            *sort_mapping.get(params["sort_by"], ["-customlistitem__date_added"]),
        )

        # Paginate and prepare media objects
        paginator = Paginator(items, 16)
        items_page = paginator.get_page(params["page"])
        filtered_items_count = paginator.count

        _attach_media_with_aggregation(items_page)

    if layout == "table":
        _adapt_list_items_for_table(items_page)

    # Get recommendation count for owners/collaborators
    recommendation_count = 0
    if can_edit and custom_list.allow_recommendations:
        recommendation_count = custom_list.recommendations.count()

    # Base context for both full and partial responses
    chip_sort = "score" if params["sort_by"] == "rating" else params["sort_by"]
    is_partial = bool(request.headers.get("HX-Request"))
    is_pagination = is_partial and params["page"] > 1
    current_media_type = _resolve_list_table_media_type(
        params["media_types"],
        filtered_media_types,
    )
    context = {
        "user": request.user,
        "custom_list": custom_list,
        "items": items_page,
        "has_next": items_page.has_next(),
        "next_page_number": items_page.next_page_number()
        if items_page.has_next()
        else None,
        "items_count": total_items_count,
        "filtered_items_count": filtered_items_count,
        "current_sort": params["sort_by"],
        "current_direction": params["direction"],
        "chip_sort": chip_sort,
        "current_status": params["status_filter"] or MediaStatusChoices.ALL,
        "current_layout": layout,
        "sort_choices": sorted(ListDetailSortChoices.choices, key=lambda x: x[1]),
        "status_choices": MediaStatusChoices.choices,
        "public_view": public_view,
        "can_edit": can_edit,
        "list_ordering_enabled": can_edit and params["sort_by"] == ListDetailSortChoices.CUSTOM,
        "is_public_view": is_public_view,
        "recommendation_count": recommendation_count,
        "base_template": "base_public.html" if public_view else "base.html",
        "is_partial": is_partial,
        "is_pagination": is_pagination,
        "current_media_types": params["media_types"],
        "has_media_type_filter": bool(params["media_types"]),
        "column_config": resolve_column_config(
            current_media_type,
            params["sort_by"],
            request.user,
            "list",
        ),
        "default_column_config": resolve_default_column_config(
            current_media_type,
            params["sort_by"],
            "list",
        ),
        "table_type": "list",
        "table_column_update_url": reverse(
            "list_detail_columns",
            args=[custom_list.id],
        ),
        "table_column_media_type": current_media_type,
        "table_refresh_url": reverse("list_detail", args=[custom_list.id]),
        "table_refresh_target": "#items-view",
        "table_refresh_include_selector": "#filter-form",
    }

    if layout == "table":
        context.update(
            {
                "media_list": items_page,
                "resolved_columns": resolve_columns(
                    current_media_type,
                    params["sort_by"],
                    request.user,
                    "list",
                ),
                "table_body_id": "list-table-body",
                "table_pagination_url": reverse("list_detail", args=[custom_list.id]),
                "table_target_selector": "#list-table-body",
                "table_include_selector": "#filter-form",
            },
        )

    # Additional context for full page render
    if not is_partial:
        context.update(
            {
                "form": CustomListForm(instance=custom_list, user=request.user)
                if can_edit
                else None,
                "media_types": sorted(MediaTypes.values, key=lambda v: MediaTypes(v).label),
                "collaborators_count": custom_list.collaborators.count() + 1,
                "completion_percent": completion_percent,
                "completed_count": completed_count,
            },
        )
        return render(request, "lists/list_detail.html", context)

    # HTMX partial response
    if layout == "table":
        if is_pagination:
            return render(request, "app/components/table_items.html", context)
        return render(request, "lists/components/list_table.html", context)
    return render(request, "lists/components/media_grid.html", context)


@require_POST
def update_list_table_columns(request, list_id):
    """Persist list-table column prefs without overwriting regular media-list prefs."""
    if not request.user.is_authenticated:
        return HttpResponseBadRequest("Authentication required")

    custom_list = get_object_or_404(
        CustomList.objects.select_related("owner").prefetch_related("collaborators"),
        id=list_id,
    )
    if not custom_list.user_can_view(request.user):
        raise Http404("List not found")

    media_type = request.POST.get("media_type_key", "all")
    if media_type != "all" and media_type not in MediaTypes.values:
        media_type = "all"

    raw_order = request.POST.get("order", "[]")
    raw_hidden = request.POST.get("hidden", "[]")

    try:
        parsed_order = json.loads(raw_order)
    except json.JSONDecodeError:
        parsed_order = []
    try:
        parsed_hidden = json.loads(raw_hidden)
    except json.JSONDecodeError:
        parsed_hidden = []

    order = (
        [value for value in parsed_order if isinstance(value, str)]
        if isinstance(parsed_order, list)
        else []
    )
    hidden = (
        [value for value in parsed_hidden if isinstance(value, str)]
        if isinstance(parsed_hidden, list)
        else []
    )

    valid_sorts = {choice[0] for choice in ListDetailSortChoices.choices}
    current_sort = request.POST.get("sort", ListDetailSortChoices.DATE_ADDED)
    if current_sort not in valid_sorts:
        current_sort = ListDetailSortChoices.DATE_ADDED

    clean_order, clean_hidden = sanitize_column_prefs(
        media_type=media_type,
        current_sort=current_sort,
        user=request.user,
        table_type="list",
        order=order,
        hidden=hidden,
    )

    request.user.update_column_prefs(
        media_type=media_type,
        table_type="list",
        order=clean_order,
        hidden=clean_hidden,
    )

    response = HttpResponse(status=204)
    response["HX-Trigger"] = json.dumps({"refreshTableColumns": True})
    return response


@require_POST
def create(request):
    """Create a new custom list."""
    form = CustomListForm(request.POST, user=request.user)
    if form.is_valid():
        custom_list = form.save(commit=False)
        custom_list.owner = request.user
        custom_list.save()
        form.save_m2m()
        logger.info("%s list created successfully.", custom_list)
        ListActivity.objects.create(
            custom_list=custom_list,
            user=request.user,
            activity_type=ListActivityType.LIST_CREATED,
        )
        if custom_list.is_smart and request.POST.get("smart_create_flow"):
            return redirect(f"{reverse('list_detail', args=[custom_list.id])}?edit_smart_rules=1")
    else:
        logger.error(form.errors.as_json())
        helpers.form_error_messages(form, request)
    return helpers.redirect_back(request)


@login_required
@require_POST
def smart_rules_update(request, list_id):
    """Persist smart list rules and sync list membership."""
    custom_list = get_object_or_404(CustomList, id=list_id)
    if not custom_list.user_can_edit(request.user):
        return HttpResponse(status=403)
    if not custom_list.is_smart:
        return JsonResponse({"error": "This list is not a smart list."}, status=400)

    payload = request.POST
    content_type = request.headers.get("Content-Type", "")
    if "application/json" in content_type:
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    normalized = smart_rules.normalize_rule_payload(payload, custom_list.owner)
    custom_list.smart_media_types = normalized["media_types"]
    custom_list.smart_excluded_media_types = []
    custom_list.smart_filters = {
        key: normalized.get(key, smart_rules.SMART_FILTER_DEFAULTS[key])
        for key in smart_rules.SMART_FILTER_KEYS
    }
    custom_list.save(
        update_fields=[
            "smart_media_types",
            "smart_excluded_media_types",
            "smart_filters",
        ],
    )
    custom_list.sync_smart_items()

    return JsonResponse(
        {
            "items_count": custom_list.items.count(),
            "rules": normalized,
        },
    )


@require_POST
def edit(request):
    """Edit an existing custom list."""
    list_id = request.POST.get("list_id")
    custom_list = get_object_or_404(CustomList, id=list_id)
    if custom_list.user_can_edit(request.user):
        form = CustomListForm(request.POST, instance=custom_list, user=request.user)
        if form.is_valid():
            form.save()
            logger.info("%s list edited successfully.", custom_list)
            ListActivity.objects.create(
                custom_list=custom_list,
                user=request.user,
                activity_type=ListActivityType.LIST_EDITED,
            )
    else:
        messages.error(request, "You do not have permission to edit this list.")
    return helpers.redirect_back(request)


@require_POST
def delete(request):
    """Delete a custom list."""
    list_id = request.POST.get("list_id")
    custom_list = get_object_or_404(CustomList, id=list_id)
    if custom_list.user_can_delete(request.user):
        custom_list.delete()
        logger.info("%s list deleted successfully.", custom_list)
        return redirect("lists")

    messages.error(request, "You do not have permission to delete this list.")
    return helpers.redirect_back(request)


@require_GET
def lists_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    episode_number=None,
):
    """Return the modal showing all custom lists and allowing to add to them."""
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
        _maybe_backfill_episode_title(item, force=True)
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
            image=metadata["image"],
            **_list_item_title_fields_from_metadata(tracking_media_type, metadata),
        )

    custom_lists = CustomList.objects.get_user_lists_with_item(request.user, item)
    if hasattr(custom_lists, "filter"):
        custom_lists = custom_lists.filter(is_smart=False)
    else:
        custom_lists = [
            custom_list
            for custom_list in custom_lists
            if not getattr(custom_list, "is_smart", False)
        ]
    custom_lists = list(custom_lists)

    selected_tag = (request.GET.get("tag") or "").strip()

    unique_tags = sorted(
        {
            tag.strip()
            for custom_list in custom_lists
            for tag in (custom_list.tags or [])
            if isinstance(tag, str) and tag.strip()
        },
        key=str.lower,
    )

    if selected_tag:
        selected_tag_folded = selected_tag.casefold()
        custom_lists = [
            custom_list
            for custom_list in custom_lists
            if any(
                isinstance(tag, str) and tag.strip().casefold() == selected_tag_folded
                for tag in (custom_list.tags or [])
            )
        ]

    return render(
        request,
        "lists/components/fill_lists.html",
        {
            "item": item,
            "custom_lists": custom_lists,
            "list_tags": unique_tags,
            "selected_list_tag": selected_tag,
        },
    )


@require_POST
def list_item_toggle(request):
    """Add or remove an item from a custom list."""
    item_id = request.POST["item_id"]
    custom_list_id = request.POST["custom_list_id"]

    item = get_object_or_404(Item, id=item_id)
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=item.media_type,
    )
    custom_list = get_object_or_404(
        CustomList.objects.filter(
            Q(owner=request.user) | Q(collaborators=request.user),
            id=custom_list_id,
        ).distinct(),  # To prevent duplicates, when user is owner and collaborator
    )

    if custom_list.is_smart:
        return HttpResponse(status=403)

    if custom_list.items.filter(id=item.id).exists():
        CustomListItem.objects.filter(custom_list=custom_list, item=item).delete()
        logger.info("%s removed from %s.", item, custom_list)
        has_item = False
        ListActivity.objects.create(
            custom_list=custom_list,
            user=request.user,
            activity_type=ListActivityType.ITEM_REMOVED,
            item=item,
        )
    else:
        CustomListItem.objects.create(
            custom_list=custom_list,
            item=item,
            added_by=request.user,
        )
        logger.info("%s added to %s.", item, custom_list)
        has_item = True
        ListActivity.objects.create(
            custom_list=custom_list,
            user=request.user,
            activity_type=ListActivityType.ITEM_ADDED,
            item=item,
        )

    return render(
        request,
        "lists/components/list_item_button.html",
        {"custom_list": custom_list, "item": item, "has_item": has_item},
    )


@require_GET
def add_list_item_page(request, list_id):
    """Show the owner/collaborator quick-add page for a manual list."""
    custom_list = get_object_or_404(
        CustomList.objects.select_related("owner").prefetch_related("collaborators"),
        id=list_id,
    )

    if not custom_list.user_can_edit(request.user):
        msg = "You do not have permission to add items to this list"
        raise Http404(msg)

    if custom_list.is_smart:
        messages.info(
            request,
            "Smart lists update from their rules. Edit the rules to change items.",
        )
        return redirect("list_detail", list_id=list_id)

    enabled_media_types = request.user.get_enabled_media_types()

    initial_query = request.GET.get("q", "").strip()
    initial_media_type = request.GET.get("media_type") or enabled_media_types[0]
    if initial_media_type not in enabled_media_types:
        initial_media_type = enabled_media_types[0]

    try:
        initial_page = int(request.GET.get("page", 1))
    except (TypeError, ValueError):
        initial_page = 1
    if initial_page < 1:
        initial_page = 1

    context = {
        "custom_list": custom_list,
        "media_types": enabled_media_types,
        "initial_query": initial_query,
        "initial_media_type": initial_media_type,
        "initial_page": initial_page,
    }

    return render(request, "lists/add_item.html", context)


@require_GET
def add_list_item_search(request, list_id):
    """Search for items to add directly to an editable manual list."""
    custom_list = get_object_or_404(
        CustomList.objects.select_related("owner").prefetch_related("collaborators"),
        id=list_id,
    )

    if not custom_list.user_can_edit(request.user):
        msg = "You do not have permission to add items to this list"
        raise Http404(msg)

    if custom_list.is_smart:
        return render(
            request,
            "lists/components/add_item_search_results.html",
            {
                "results": [],
                "custom_list": custom_list,
                "error": "Smart lists update from their rules and do not support manual additions.",
            },
            status=200,
        )

    show_preview = request.GET.get("show_preview")
    if show_preview:
        media_id = request.GET.get("media_id")
        media_type = request.GET.get("media_type")
        source = request.GET.get("source")
        season_number = request.GET.get("season_number")
        episode_number = request.GET.get("episode_number")

        try:
            media_metadata = services.get_media_metadata(media_type, media_id, source)
        except Exception as exc:
            logger.exception(
                "Quick add preview failed: list_id=%s media_type=%s media_id=%s",
                custom_list.id,
                media_type,
                media_id,
                exc_info=exc,
            )
            return JsonResponse(
                {"error": "Unable to load details right now. Please try again."},
                status=502,
            )

        item = Item.objects.filter(
            media_id=media_id,
            media_type=media_type,
            source=source,
        ).first()

        already_in_list = False
        if item:
            already_in_list = custom_list.items.filter(id=item.id).exists()

        query = request.GET.get("q", "").strip()
        search_media_type = request.GET.get("search_media_type")
        page = request.GET.get("page", "1")

        next_params = {}
        if query:
            next_params["q"] = query
        if search_media_type:
            next_params["media_type"] = search_media_type
        if page:
            next_params["page"] = page

        next_url = reverse("list_add_item", kwargs={"list_id": custom_list.id})
        if next_params:
            next_url = f"{next_url}?{urlencode(next_params)}"

        context = {
            "custom_list": custom_list,
            "media": media_metadata,
            "media_id": media_id,
            "media_type": media_type,
            "source": source,
            "season_number": season_number,
            "episode_number": episode_number,
            "already_in_list": already_in_list,
            "next_url": next_url,
        }
        return render(request, "lists/components/add_item_preview_modal.html", context)

    query = request.GET.get("q", "").strip()
    media_type = request.GET.get("media_type") or MediaTypes.TV.value
    if media_type not in MediaTypes.values and media_type != "tv_with_seasons":
        media_type = MediaTypes.TV.value

    try:
        page = int(request.GET.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    if not query or len(query) < 2:
        return render(
            request,
            "lists/components/add_item_search_results.html",
            {"results": [], "custom_list": custom_list},
        )

    from app import config

    source = config.get_default_source_name(media_type).value

    try:
        data = services.search(media_type, query, page, source)
    except Exception as exc:
        logger.exception(
            "Quick add search failed: list_id=%s media_type=%s query=%s",
            custom_list.id,
            media_type,
            query,
            exc_info=exc,
        )
        context = {
            "results": [],
            "custom_list": custom_list,
            "query": query,
            "media_type": media_type,
            "page": page,
            "total_pages": 1,
            "error": "Search is temporarily unavailable. Please try again.",
        }
        return render(
            request,
            "lists/components/add_item_search_results.html",
            context,
            status=200,
        )

    existing_items = set(
        custom_list.items.values_list("media_id", "source"),
    )

    results = data.get("results", [])
    for result in results:
        key = (str(result["media_id"]), result["source"])
        result["already_in_list"] = key in existing_items

    enriched_results = helpers.enrich_items_with_user_data(request, results)

    context = {
        "results": enriched_results,
        "custom_list": custom_list,
        "query": query,
        "media_type": media_type,
        "page": page,
        "total_pages": data.get("total_pages", 1),
    }

    return render(request, "lists/components/add_item_search_results.html", context)


@require_POST
def add_list_item_submit(request, list_id):
    """Add a searched item directly to an editable manual list."""
    custom_list = get_object_or_404(
        CustomList.objects.select_related("owner").prefetch_related("collaborators"),
        id=list_id,
    )

    if not custom_list.user_can_edit(request.user):
        messages.error(request, "You do not have permission to edit this list.")
        return helpers.redirect_back(request)

    if custom_list.is_smart:
        messages.error(
            request,
            "Smart lists update from their rules and do not support manual additions.",
        )
        return redirect("list_detail", list_id=list_id)

    next_url = request.POST.get("next")

    def _redirect_after_submit(fallback):
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts=None):
            return redirect(next_url)
        return fallback

    media_id = request.POST.get("media_id")
    media_type = request.POST.get("media_type")
    source = request.POST.get("source")
    season_number = request.POST.get("season_number")
    episode_number = request.POST.get("episode_number")

    season_number = int(season_number) if season_number else None
    episode_number = int(episode_number) if episode_number else None

    try:
        item = Item.objects.get(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
        )
        _maybe_backfill_episode_title(item, force=True)
    except Item.DoesNotExist:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number] if season_number else None,
            episode_number,
        )
        release_datetime = helpers.extract_release_datetime(metadata)
        item = Item.objects.create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
            image=metadata["image"],
            release_datetime=release_datetime,
            **_list_item_title_fields_from_metadata(media_type, metadata),
        )

    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=item.media_type,
    )

    if custom_list.items.filter(id=item.id).exists():
        messages.info(request, f'"{item.title}" is already in this list.')
        return _redirect_after_submit(redirect("list_add_item", list_id=list_id))

    CustomListItem.objects.create(
        custom_list=custom_list,
        item=item,
        added_by=request.user,
    )
    logger.info("%s added to %s from quick add search.", item, custom_list)
    ListActivity.objects.create(
        custom_list=custom_list,
        user=request.user,
        activity_type=ListActivityType.ITEM_ADDED,
        item=item,
    )
    messages.success(request, f'"{item.title}" has been added to the list.')

    return _redirect_after_submit(redirect("list_detail", list_id=list_id))


@login_required
@require_POST
def reorder_list_item(request, list_id):
    """Reorder a list item in custom sort mode."""
    custom_list = get_object_or_404(CustomList, id=list_id)
    if not custom_list.user_can_edit(request.user):
        return HttpResponse(status=403)

    item_id = request.POST.get("item_id")
    action = (request.POST.get("action") or "").strip().lower()
    if not item_id or action not in {"first", "back", "next", "last"}:
        return HttpResponse(status=400)

    list_items = list(
        CustomListItem.objects.filter(custom_list=custom_list)
        .select_related("item")
        .order_by("date_added", "id"),
    )
    if len(list_items) < 2:
        return HttpResponse(status=204)

    current_index = next(
        (
            index
            for index, custom_list_item in enumerate(list_items)
            if str(custom_list_item.item_id) == str(item_id)
        ),
        None,
    )
    if current_index is None:
        return HttpResponse(status=404)

    if action == "first":
        new_index = 0
    elif action == "back":
        new_index = max(0, current_index - 1)
    elif action == "next":
        new_index = min(len(list_items) - 1, current_index + 1)
    else:
        new_index = len(list_items) - 1

    if new_index == current_index:
        return HttpResponse(status=204)

    moved_item = list_items.pop(current_index)
    list_items.insert(new_index, moved_item)

    base_time = timezone.now().replace(microsecond=0)
    for index, custom_list_item in enumerate(list_items):
        custom_list_item.date_added = base_time + datetime.timedelta(seconds=index)
    CustomListItem.objects.bulk_update(list_items, ["date_added"])

    return HttpResponse(status=204)


@login_required
@require_POST
def reorder_list_items_all(request, list_id):
    """Reorder list items by full ordered ID list (drag-and-drop)."""
    custom_list = get_object_or_404(CustomList, id=list_id)
    if not custom_list.user_can_edit(request.user):
        return HttpResponse(status=403)

    item_ids = request.POST.getlist("item_ids[]")
    if not item_ids:
        return HttpResponse(status=400)

    all_items = list(
        CustomListItem.objects.filter(custom_list=custom_list).order_by("date_added", "id"),
    )
    submitted_set = {str(i) for i in item_ids}
    item_map = {str(li.item_id): li for li in all_items}

    # Positions in the full list currently occupied by the submitted subset
    original_positions = sorted(
        i for i, li in enumerate(all_items) if str(li.item_id) in submitted_set
    )
    if not original_positions:
        return HttpResponse(status=400)

    # Place submitted items in their new DnD order at those same positions
    for pos, item_id in zip(original_positions, item_ids):
        if str(item_id) in item_map:
            all_items[pos] = item_map[str(item_id)]

    base_time = timezone.now().replace(microsecond=0)
    for index, li in enumerate(all_items):
        li.date_added = base_time + datetime.timedelta(seconds=index)
    CustomListItem.objects.bulk_update(all_items, ["date_added"])

    return HttpResponse(status=204)


# =============================================================================
# Recommendation Views
# =============================================================================


@login_not_required
@require_GET
def recommend_item_page(request, list_id):
    """Show the recommendation search page for a public list."""
    custom_list = get_object_or_404(
        CustomList.objects.select_related("owner"),
        id=list_id,
    )

    if not custom_list.can_recommend():
        msg = "Recommendations are not enabled for this list"
        raise Http404(msg)

    # Get enabled media types - use defaults for anonymous users
    if request.user.is_authenticated:
        enabled_media_types = request.user.get_enabled_media_types()
    else:
        enabled_media_types = MediaTypes.values

    initial_query = request.GET.get("q", "").strip()
    initial_media_type = request.GET.get("media_type") or enabled_media_types[0]
    if initial_media_type not in enabled_media_types:
        initial_media_type = enabled_media_types[0]

    try:
        initial_page = int(request.GET.get("page", 1))
    except (TypeError, ValueError):
        initial_page = 1
    if initial_page < 1:
        initial_page = 1

    context = {
        "custom_list": custom_list,
        "media_types": enabled_media_types,
        "is_authenticated": request.user.is_authenticated,
        "public_view": not request.user.is_authenticated,
        "base_template": "base_public.html"
        if not request.user.is_authenticated
        else "base.html",
        "initial_query": initial_query,
        "initial_media_type": initial_media_type,
        "initial_page": initial_page,
    }

    return render(request, "lists/recommend_item.html", context)


@login_not_required
@require_GET
def recommend_search(request, list_id):
    """Search for items to recommend - returns search results or preview modal."""
    custom_list = get_object_or_404(CustomList, id=list_id)

    if not custom_list.can_recommend():
        return JsonResponse({"error": "Recommendations not enabled"}, status=403)

    # Check if this is a request to show the preview modal
    show_preview = request.GET.get("show_preview")
    if show_preview:
        media_id = request.GET.get("media_id")
        media_type = request.GET.get("media_type")
        source = request.GET.get("source")
        season_number = request.GET.get("season_number")
        episode_number = request.GET.get("episode_number")

        try:
            media_metadata = services.get_media_metadata(media_type, media_id, source)
        except Exception as exc:
            logger.exception(
                "Recommendation preview failed: list_id=%s media_type=%s media_id=%s",
                custom_list.id,
                media_type,
                media_id,
                exc_info=exc,
            )
            return JsonResponse(
                {"error": "Unable to load details right now. Please try again."},
                status=502,
            )

        # Check if already in list or recommended
        item = Item.objects.filter(
            media_id=media_id,
            media_type=media_type,
            source=source,
        ).first()

        already_in_list = False
        already_recommended = False
        if item:
            already_in_list = custom_list.items.filter(id=item.id).exists()
            already_recommended = ListRecommendation.objects.filter(
                custom_list=custom_list,
                item=item,
            ).exists()

        query = request.GET.get("q", "").strip()
        search_media_type = request.GET.get("search_media_type")
        page = request.GET.get("page", "1")

        next_params = {}
        if query:
            next_params["q"] = query
        if search_media_type:
            next_params["media_type"] = search_media_type
        if page:
            next_params["page"] = page

        next_url = reverse("recommend_item", kwargs={"list_id": custom_list.id})
        if next_params:
            next_url = f"{next_url}?{urlencode(next_params)}"

        context = {
            "custom_list": custom_list,
            "media": media_metadata,
            "media_id": media_id,
            "media_type": media_type,
            "source": source,
            "season_number": season_number,
            "episode_number": episode_number,
            "is_authenticated": request.user.is_authenticated,
            "already_in_list": already_in_list,
            "already_recommended": already_recommended,
            "next_url": next_url,
        }
        return render(request, "lists/components/recommend_preview_modal.html", context)

    query = request.GET.get("q", "").strip()
    media_type = request.GET.get("media_type") or MediaTypes.TV.value
    if media_type not in MediaTypes.values and media_type != "tv_with_seasons":
        media_type = MediaTypes.TV.value

    try:
        page = int(request.GET.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    if not query or len(query) < 2:
        return render(
            request,
            "lists/components/recommend_search_results.html",
            {"results": [], "custom_list": custom_list},
        )

    # Use the existing search service
    from app import config

    source = config.get_default_source_name(media_type).value

    try:
        data = services.search(media_type, query, page, source)
    except Exception as exc:
        logger.exception(
            "Recommendation search failed: list_id=%s media_type=%s query=%s",
            custom_list.id,
            media_type,
            query,
            exc_info=exc,
        )
        context = {
            "results": [],
            "custom_list": custom_list,
            "query": query,
            "media_type": media_type,
            "page": page,
            "total_pages": 1,
            "error": "Search is temporarily unavailable. Please try again.",
        }
        return render(
            request,
            "lists/components/recommend_search_results.html",
            context,
            status=200,
        )

    # Get items already in the list (by media_id and source)
    existing_items = set(
        custom_list.items.values_list("media_id", "source"),
    )

    # Get items already recommended (by media_id and source)
    recommended_items = set(
        ListRecommendation.objects.filter(
            custom_list=custom_list,
        ).values_list("item__media_id", "item__source"),
    )

    # Mark results that are already in the list or recommended
    results = data.get("results", [])
    for result in results:
        key = (str(result["media_id"]), result["source"])
        result["already_in_list"] = key in existing_items
        result["already_recommended"] = key in recommended_items

    enriched_results = helpers.enrich_items_with_user_data(request, results)

    context = {
        "results": enriched_results,
        "custom_list": custom_list,
        "query": query,
        "media_type": media_type,
        "page": page,
        "total_pages": data.get("total_pages", 1),
    }

    return render(request, "lists/components/recommend_search_results.html", context)


@login_not_required
@require_POST
def submit_recommendation(request, list_id):
    """Submit a recommendation for an item to be added to a list."""
    custom_list = get_object_or_404(CustomList, id=list_id)

    if not custom_list.can_recommend():
        messages.error(request, "Recommendations are not enabled for this list.")
        return redirect("list_detail", list_id=list_id)

    next_url = request.POST.get("next")

    def _redirect_after_submit(fallback):
        if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts=None):
            return redirect(next_url)
        return fallback

    # Get item details from the form
    media_id = request.POST.get("media_id")
    media_type = request.POST.get("media_type")
    source = request.POST.get("source")
    season_number = request.POST.get("season_number")
    episode_number = request.POST.get("episode_number")

    # Convert to int if present
    season_number = int(season_number) if season_number else None
    episode_number = int(episode_number) if episode_number else None

    # Get or create the item
    try:
        item = Item.objects.get(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
        )
        _maybe_backfill_episode_title(item, force=True)
    except Item.DoesNotExist:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number] if season_number else None,
            episode_number,
        )
        release_datetime = helpers.extract_release_datetime(metadata)
        item = Item.objects.create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
            image=metadata["image"],
            release_datetime=release_datetime,
            **_list_item_title_fields_from_metadata(media_type, metadata),
        )

    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=item.media_type,
    )

    # Check if item is already in the list
    if custom_list.items.filter(id=item.id).exists():
        messages.info(request, f'"{item.title}" is already in this list.')
        return _redirect_after_submit(redirect("recommend_item", list_id=list_id))

    # Check if already recommended
    if ListRecommendation.objects.filter(custom_list=custom_list, item=item).exists():
        messages.info(request, f'"{item.title}" has already been recommended.')
        return _redirect_after_submit(redirect("recommend_item", list_id=list_id))

    # Create the recommendation
    recommended_by = request.user if request.user.is_authenticated else None
    anonymous_name = ""
    if not request.user.is_authenticated:
        anonymous_name = request.POST.get("recommender_name", "").strip()[:100]

    note = request.POST.get("note", "").strip()[:1000]

    ListRecommendation.objects.create(
        custom_list=custom_list,
        item=item,
        recommended_by=recommended_by,
        anonymous_name=anonymous_name,
        note=note,
    )

    logger.info("Recommendation created: %s for %s", item.title, custom_list.name)
    messages.success(
        request,
        f'Your recommendation for "{item.title}" has been submitted!',
    )

    return _redirect_after_submit(redirect("list_detail", list_id=list_id))


@require_GET
def list_recommendations(request, list_id):
    """View all recommendations for a list (owner/collaborators only)."""
    custom_list = get_object_or_404(
        CustomList.objects.select_related("owner").prefetch_related("collaborators"),
        id=list_id,
    )

    if not custom_list.user_can_edit(request.user):
        msg = "You do not have permission to view recommendations for this list"
        raise Http404(msg)

    recommendations = custom_list.recommendations.select_related(
        "item",
        "recommended_by",
    ).order_by("-date_recommended")

    context = {
        "custom_list": custom_list,
        "recommendations": recommendations,
    }

    return render(request, "lists/list_recommendations.html", context)


@require_GET
def list_activity(request, list_id):
    """View activity history for a list (owner/collaborators only)."""
    custom_list = get_object_or_404(
        CustomList.objects.select_related("owner").prefetch_related("collaborators"),
        id=list_id,
    )

    if not custom_list.user_can_edit(request.user):
        msg = "You do not have permission to view activity for this list"
        raise Http404(msg)

    activities = custom_list.activities.select_related(
        "user",
        "item",
    ).order_by("-timestamp")[:100]

    context = {
        "custom_list": custom_list,
        "activities": activities,
    }

    return render(request, "lists/list_activity.html", context)


@require_POST
def approve_recommendation(request, list_id, recommendation_id):
    """Approve a recommendation and add the item to the list."""
    custom_list = get_object_or_404(CustomList, id=list_id)

    if not custom_list.user_can_edit(request.user):
        messages.error(request, "You do not have permission to manage recommendations.")
        return helpers.redirect_back(request)

    recommendation = get_object_or_404(
        ListRecommendation,
        id=recommendation_id,
        custom_list=custom_list,
    )
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=recommendation.item.media_type,
    )

    # Add item to the list if not already there
    if not custom_list.items.filter(id=recommendation.item.id).exists():
        CustomListItem.objects.create(
            custom_list=custom_list,
            item=recommendation.item,
            added_by=request.user,
        )
        logger.info(
            "Recommendation approved: %s added to %s",
            recommendation.item.title,
            custom_list.name,
        )
        messages.success(
            request,
            f'"{recommendation.item.title}" has been added to the list.',
        )
        ListActivity.objects.create(
            custom_list=custom_list,
            user=request.user,
            activity_type=ListActivityType.RECOMMENDATION_APPROVED,
            item=recommendation.item,
            details=f"Recommended by {recommendation.recommender_display_name}",
        )
    else:
        messages.info(
            request,
            f'"{recommendation.item.title}" is already in the list.',
        )

    recommendation.delete()

    return helpers.redirect_back(request)


@require_POST
def deny_recommendation(request, list_id, recommendation_id):
    """Deny/delete a recommendation."""
    custom_list = get_object_or_404(CustomList, id=list_id)

    if not custom_list.user_can_edit(request.user):
        messages.error(request, "You do not have permission to manage recommendations.")
        return helpers.redirect_back(request)

    recommendation = get_object_or_404(
        ListRecommendation,
        id=recommendation_id,
        custom_list=custom_list,
    )
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=recommendation.item.media_type,
    )

    item_title = recommendation.item.title
    item = recommendation.item
    recommender_name = recommendation.recommender_display_name
    recommendation.delete()

    ListActivity.objects.create(
        custom_list=custom_list,
        user=request.user,
        activity_type=ListActivityType.RECOMMENDATION_DENIED,
        item=item,
        details=f"Recommended by {recommender_name}",
    )

    logger.info("Recommendation denied: %s for %s", item_title, custom_list.name)
    messages.success(request, f'Recommendation for "{item_title}" has been removed.')

    return helpers.redirect_back(request)


@require_GET
@login_not_required
def fetch_release_year(request):
    """Fetch release year for a single item asynchronously."""
    item_id = request.GET.get("item_id")
    if not item_id:
        return JsonResponse({"error": "item_id required"}, status=400)

    try:
        item = Item.objects.get(id=item_id)
    except Item.DoesNotExist:
        return JsonResponse({"error": "Item not found"}, status=404)

    if item.release_datetime:
        return JsonResponse({"year": item.release_datetime.year})

    if item.media_type == MediaTypes.SEASON.value and item.season_number:
        episode_release = (
            Item.objects.filter(
                media_id=item.media_id,
                source=item.source,
                media_type=MediaTypes.EPISODE.value,
                season_number=item.season_number,
                release_datetime__isnull=False,
            )
            .order_by("release_datetime")
            .values_list("release_datetime", flat=True)
            .first()
        )
        if episode_release:
            item.release_datetime = episode_release
            item.save(update_fields=["release_datetime"])
            return JsonResponse({"year": episode_release.year})

    try:
        season_numbers = None
        episode_number = None
        if item.media_type == MediaTypes.SEASON.value and item.season_number:
            season_numbers = [item.season_number]
        elif (
            item.media_type == MediaTypes.EPISODE.value
            and item.season_number is not None
            and item.episode_number is not None
        ):
            season_numbers = [item.season_number]
            episode_number = item.episode_number

        metadata = services.get_media_metadata(
            item.media_type,
            item.media_id,
            item.source,
            season_numbers=season_numbers,
            episode_number=episode_number,
        )
        if metadata:
            release_datetime = helpers.extract_release_datetime(metadata)
            if release_datetime:
                item.release_datetime = release_datetime
                item.save(update_fields=["release_datetime"])
                return JsonResponse({"year": release_datetime.year})
    except Exception as exc:
        logger.warning(
            "Failed to fetch release year for item %s: %s",
            item_id,
            exc,
        )

    return JsonResponse({"year": None})
