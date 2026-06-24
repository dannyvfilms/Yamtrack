"""
List browsing and discovery views: user profile, list browser, and cover image.

These are the read-only discovery entry points — no mutations happen here.
Write operations live in views_list_actions.py; detail views live in
views_list_detail.py and views_smart_list.py.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q, Subquery
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from app import helpers
from integrations.models import TraktAccount
from lists.forms import CustomListForm
from lists.models import CustomList, CustomListItem
from users.models import ListSortChoices
from lists.views_helpers import (
    _build_list_url_template,
    _get_completed_item_ids,
    _get_list_last_watched_dates,
    _order_expression,
    _resolve_list_sort_direction,
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
