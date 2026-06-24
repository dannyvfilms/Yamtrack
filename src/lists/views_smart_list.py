"""
Smart list detail response helper.

Contains _smart_list_detail_response, which renders the smart-list detail
page and its HTMX partial responses. This function is called from
list_detail() in views_list_detail.py when custom_list.is_smart is True.

Smart lists use rule-based membership (smart_rules module) rather than
manually curated items, and have their own template (smart_list_detail.html).
"""

import logging

from django.core.paginator import Paginator
from django.db.models import F, OuterRef, Subquery
from django.shortcuts import render
from django.urls import reverse

from app import helpers
from app.columns import resolve_column_config, resolve_columns, resolve_default_column_config
from app.models import Item, MediaTypes
from app.release_years import prefill_display_release_years
from lists.forms import CustomListForm
from lists import smart_rules
from lists.models import CustomList, CustomListItem
from users.models import ListDetailSortChoices, MediaStatusChoices
from lists.views_helpers import (
    _adapt_list_items_for_table,
    _attach_media_with_aggregation,
    _build_list_url_template,
    _build_media_type_breakdown,
    _date_sort_value,
    _media_date_value,
    _order_expression,
    _progress_value,
    _rating_value,
    _resolve_list_sort_direction,
    _resolve_list_table_media_type,
)

logger = logging.getLogger(__name__)


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
        _attach_media_with_aggregation(all_items, media_user)
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
        _attach_media_with_aggregation(items_page, media_user)

    prefill_display_release_years(items_page)

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
