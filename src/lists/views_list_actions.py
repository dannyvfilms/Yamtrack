"""
List management action views: CRUD, column preferences, modal, item toggle,
and release-year fetch.

None of these views render the list detail page — they mutate state or return
small fragments. The read-heavy detail views live in views_list_detail.py and
views_smart_list.py; the browse views live in views_list_browse.py.
"""

import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required, login_required
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from app import helpers
from app.columns import sanitize_column_prefs
from app.discover import tab_cache as discover_tab_cache
from app.models import Item, MediaTypes
from app.providers import services
from app.services import metadata_resolution
from lists.forms import CustomListForm
from lists import smart_rules
from lists.models import (
    CustomList,
    CustomListItem,
    ListActivity,
    ListActivityType,
)
from users.models import ListDetailSortChoices
from lists.views_helpers import (
    _list_item_title_fields_from_metadata,
    _maybe_backfill_episode_title,
)

logger = logging.getLogger(__name__)


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
    except Item.MultipleObjectsReturned:
        item = Item.objects.filter(**lookup).first()
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
