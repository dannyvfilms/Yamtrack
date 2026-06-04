"""
Views for the add-item workflow and list item reordering.

Covers: the quick-add search page, search results / preview modal, item
submission to a list, and single-item / bulk drag-and-drop reordering.
"""

import datetime
import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from app import helpers
from app.discover import tab_cache as discover_tab_cache
from app.models import Item, MediaTypes
from app.providers import services
from lists.models import (
    CustomList,
    CustomListItem,
    ListActivity,
    ListActivityType,
)
from lists.views_helpers import (
    _extract_list_search_results,
    _list_item_title_fields_from_metadata,
    _maybe_backfill_episode_title,
)

logger = logging.getLogger(__name__)


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
        return redirect("list_detail", custom_list.public_reference)

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

    results, total_pages = _extract_list_search_results(media_type, data)
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
        "total_pages": total_pages,
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
        return redirect("list_detail", custom_list.public_reference)

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

    return _redirect_after_submit(redirect("list_detail", custom_list.public_reference))


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
