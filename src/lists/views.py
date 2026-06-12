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
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
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
from lists.views_helpers import (
    ASCENDING_LIST_SORTS,
    LIST_REFERENCE_PLACEHOLDER,
    _MEDIA_TYPE_COLORS,
    _adapt_list_items_for_table,
    _attach_list_card_overrides,
    _build_list_url_template,
    _build_media_type_breakdown,
    _default_list_sort_direction,
    _episode_title_fields_from_season_metadata,
    _episode_title_needs_backfill,
    _extract_list_search_results,
    _get_completed_item_ids,
    _get_item_last_watched_dates,
    _get_list_last_watched_dates,
    _get_trakt_credentials,
    _list_item_title_fields_from_metadata,
    _ListTableRowAdapter,
    _maybe_backfill_episode_title,
    _order_expression,
    _resolve_list_card_image_override,
    _resolve_list_sort_direction,
    _resolve_list_table_media_type,
)

logger = logging.getLogger(__name__)


User = get_user_model()


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
    
    # Boosted navigation still sends HX-Request but needs the full page
    if helpers.is_htmx_fragment(request):
        response = render(
            request,
            "lists/components/list_grid.html",
            {
                "custom_lists": lists_page,
                "current_sort": sort_by,
                "current_direction": direction,
                "cache_buster": cache_buster,
                "list_url_template": _build_list_url_template(request),
            },
        )
        # Explicitly set cache control headers for HTMX responses
        response["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        response["Vary"] = "Cookie, HX-Request, HX-Boosted"
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
            "list_url_template": _build_list_url_template(request),
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


@login_not_required
@require_GET
def list_cover_image(request, list_id):
    """Return an <img> fragment for a list card's cover image (HTMX lazy load).

    Called per-card via hx-trigger="revealed" so the expensive IGDB/TMDB
    backdrop lookups happen after the page has already rendered.
    """
    custom_list = get_object_or_404(
        CustomList.objects.prefetch_related(
            Prefetch(
                "customlistitem_set",
                queryset=CustomListItem.objects.select_related("item").order_by(
                    "-date_added"
                ),
            )
        ),
        id=list_id,
    )
    if not custom_list.user_can_view(request.user):
        return HttpResponseForbidden()
    image_url = custom_list.image
    return render(
        request,
        "lists/components/list_cover_image.html",
        {"image_url": image_url, "name": custom_list.name},
    )


from lists.views_trakt import (
    trakt_lists_callback,
    trakt_lists_credentials,
    trakt_lists_oauth,
)


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
    saved_sort = (custom_list.smart_filters or {}).get("sort") or ListDetailSortChoices.DATE_ADDED
    if saved_sort not in valid_sorts:
        saved_sort = ListDetailSortChoices.DATE_ADDED
    sort_by = request.GET.get("sort", saved_sort)
    if sort_by not in valid_sorts:
        sort_by = ListDetailSortChoices.DATE_ADDED
    saved_direction = (custom_list.smart_filters or {}).get("sort_direction") or ""
    direction = _resolve_list_sort_direction(
        sort_by,
        request.GET.get("direction", saved_direction) or None,
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
                "rating_min": request.GET.get("rating_min", saved_rules["rating_min"]),
                "rating_max": request.GET.get("rating_max", saved_rules["rating_max"]),
                "collection": request.GET.get("collection", saved_rules["collection"]),
                "genre": request.GET.get("genre", saved_rules["genre"]),
                "implied_genre": request.GET.get(
                    "implied_genre",
                    saved_rules["implied_genre"],
                ),
                "year": request.GET.get("year", saved_rules["year"]),
                "release": request.GET.get("release", saved_rules["release"]),
                "release_date_from": request.GET.get(
                    "release_date_from",
                    saved_rules["release_date_from"],
                ),
                "release_date_to": request.GET.get(
                    "release_date_to",
                    saved_rules["release_date_to"],
                ),
                "date_added_from": request.GET.get(
                    "date_added_from",
                    saved_rules["date_added_from"],
                ),
                "date_added_to": request.GET.get(
                    "date_added_to",
                    saved_rules["date_added_to"],
                ),
                "source": request.GET.get("source", saved_rules["source"]),
                "language": request.GET.get("language", saved_rules["language"]),
                "country": request.GET.get("country", saved_rules["country"]),
                "platform": request.GET.get("platform", saved_rules["platform"]),
                "origin": request.GET.get("origin", saved_rules["origin"]),
                "format": request.GET.get("format", saved_rules["format"]),
                "author": request.GET.get("author", saved_rules["author"]),
                "tag": request.GET.get("tag", saved_rules["tag"]),
                "tag_exclude": request.GET.get("tag_exclude", saved_rules["tag_exclude"]),
                "search": request.GET.get("q", saved_rules["search"]),
                "sort": request.GET.get("sort", saved_rules["sort"]),
                "sort_direction": request.GET.get("direction", saved_rules["sort_direction"]),
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

    is_partial = helpers.is_htmx_fragment(request)
    is_pagination = is_partial and page > 1
    has_active_filters = bool(active_rules.get("media_types")) or any(
        [
            active_rules.get("status") not in {"", "all"},
            active_rules.get("rating") not in {"", "all"},
            active_rules.get("rating_min"),
            active_rules.get("rating_max"),
            active_rules.get("collection") not in {"", "all"},
            active_rules.get("genre"),
            active_rules.get("implied_genre"),
            active_rules.get("year"),
            active_rules.get("release") not in {"", "all"},
            active_rules.get("release_date_from"),
            active_rules.get("release_date_to"),
            active_rules.get("date_added_from"),
            active_rules.get("date_added_to"),
            active_rules.get("source"),
            active_rules.get("language"),
            active_rules.get("country"),
            active_rules.get("platform"),
            active_rules.get("origin"),
            active_rules.get("author"),
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
        "table_refresh_url": reverse("list_detail", args=[custom_list.public_reference]),
        "table_refresh_target": "#items-view",
        "table_refresh_include_selector": "#smart-filter-form",
        "list_reference": custom_list.public_reference,
        "list_url_template": _build_list_url_template(request),
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
                "table_pagination_url": reverse("list_detail", args=[custom_list.public_reference]),
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

    context["media_type_breakdown"] = _build_media_type_breakdown(custom_list)
    if can_edit:
        context["form"] = CustomListForm(instance=custom_list, user=request.user)
    else:
        context["form"] = None
    return render(request, "lists/smart_list_detail.html", context)


@login_not_required
@never_cache
@require_GET
def list_detail(request, list_reference):
    """Return the detail page of a custom list."""
    reference = str(list_reference or "").strip()
    custom_list = CustomList.objects.get_by_reference(reference)
    if custom_list is None:
        # List doesn't exist - investigate why it might have been shown on lists page
        if reference.isdigit():
            logger.warning(
                "List ID %s not found. User: %s, Authenticated: %s",
                reference,
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
                    reference,
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
                    f"List ID {reference} not found. This may indicate a data inconsistency. "
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
        # Render current membership now; refresh it in the background so the
        # write-heavy sync never runs inside a GET request.
        list_tasks.schedule_smart_list_sync(custom_list)

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

    media_type_breakdown = _build_media_type_breakdown(custom_list)

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
    is_partial = helpers.is_htmx_fragment(request)
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
        "table_refresh_url": reverse("list_detail", args=[custom_list.public_reference]),
        "table_refresh_target": "#items-view",
        "table_refresh_include_selector": "#filter-form",
        "list_reference": custom_list.public_reference,
        "list_url_template": _build_list_url_template(request),
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
                "table_pagination_url": reverse("list_detail", args=[custom_list.public_reference]),
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
                "media_type_breakdown": media_type_breakdown,
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
            return redirect(
                f"{reverse('list_detail', args=[custom_list.public_reference])}?edit_smart_rules=1",
            )
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


from lists.views_add_reorder import (
    add_list_item_page,
    add_list_item_search,
    add_list_item_submit,
    reorder_list_item,
    reorder_list_items_all,
)
from lists.views_recommendations import (
    approve_recommendation,
    deny_recommendation,
    list_activity,
    list_recommendations,
    recommend_item_page,
    recommend_search,
    submit_recommendation,
)


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
