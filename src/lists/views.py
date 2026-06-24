import datetime
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.db.models import F, OuterRef, Subquery
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from app import helpers
from app.columns import resolve_column_config, resolve_columns, resolve_default_column_config
from app.models import MediaManager, MediaTypes
from app.providers import services  # noqa: F401 — kept so legacy test patches on lists.views.services still work
from app.release_years import prefill_display_release_years
from lists.forms import CustomListForm
from lists import tasks as list_tasks
from lists.models import CustomList, CustomListItem
from users.models import ListDetailSortChoices, MediaStatusChoices
from lists.views_helpers import (
    _adapt_list_items_for_table,
    _attach_media_with_aggregation,
    _build_list_url_template,
    _build_media_type_breakdown,
    _date_sort_value,
    _get_completed_item_ids,
    _media_date_value,
    _order_expression,
    _progress_value,
    _rating_value,
    _resolve_list_sort_direction,
    _resolve_list_table_media_type,
)
from lists.views_smart_list import _smart_list_detail_response

logger = logging.getLogger(__name__)


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
        _attach_media_with_aggregation(all_items, media_user)

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

        _attach_media_with_aggregation(items_page, media_user)

    prefill_display_release_years(items_page)

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
