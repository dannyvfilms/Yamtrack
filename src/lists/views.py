import datetime
import logging
import secrets
from urllib.parse import urlencode

from django.apps import apps
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_not_required, login_required
from django.core.paginator import Paginator
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q, Subquery
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from app import helpers
from app.models import Item, MediaManager, MediaTypes
from app.providers import services
from integrations.imports import helpers as import_helpers
from integrations.imports import trakt as trakt_imports
from integrations.models import TraktAccount
from lists.forms import CustomListForm
from lists.imports import trakt as trakt_lists
from lists import tasks as list_tasks
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
    sort_by = request.user.update_preference("lists_sort", request.GET.get("sort"))
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
            "items",
            queryset=Item.objects.order_by("-customlistitem__date_added"),
        ),
        Prefetch(
            "customlistitem_set",
            queryset=CustomListItem.objects.order_by("-date_added"),
        ),
    )
    
    if sort_by == "name":
        custom_lists = custom_lists.order_by("name")
    elif sort_by == "items_count":
        custom_lists = custom_lists.order_by("-items_count")
    elif sort_by == "newest_first":
        custom_lists = custom_lists.order_by("-id")
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
        ).order_by("-latest_update", "name")
    
    items_per_page = 20
    paginator = Paginator(custom_lists, items_per_page)
    lists_page = paginator.get_page(page)

    # Validate lists and filter out any broken ones
    valid_lists = []
    broken_list_ids = []
    for custom_list in lists_page:
        try:
            # Verify the list still exists and is accessible
            # This catches cases where lists were deleted between query and render
            verified_list = CustomList.objects.get(id=custom_list.id)
            # Verify owner still exists
            if not verified_list.owner:
                logger.warning(
                    "List ID %s has no owner, excluding from display",
                    custom_list.id,
                )
                broken_list_ids.append(custom_list.id)
                continue
            valid_lists.append(custom_list)
        except CustomList.DoesNotExist:
            logger.warning(
                "List ID %s (%s) no longer exists, excluding from display",
                custom_list.id,
                custom_list.name,
            )
            broken_list_ids.append(custom_list.id)
            continue
        except Exception as e:
            logger.error(
                "Error validating list ID %s: %s",
                custom_list.id,
                e,
                exc_info=True,
            )
            broken_list_ids.append(custom_list.id)
            continue

    if broken_list_ids:
        logger.warning(
            "Filtered out %s broken lists from display: %s",
            len(broken_list_ids),
            broken_list_ids,
        )
        messages.warning(
            request,
            f"Some lists were removed from display because they no longer exist "
            f"(likely deleted during a re-import). Please refresh the page.",
        )
        # Re-fetch the page without broken lists
        # This is a workaround - ideally we'd filter in the query, but pagination makes it complex
        valid_list_ids = [l.id for l in valid_lists]
        if valid_list_ids:
            custom_lists = custom_lists.filter(id__in=valid_list_ids)
            paginator = Paginator(custom_lists, items_per_page)
            lists_page = paginator.get_page(page)
        else:
            # All lists on this page were broken, show empty page
            lists_page = paginator.get_page(1)
            lists_page.object_list = []

    available_tags = CustomListForm._normalize_tags(
        tag
        for custom_list in CustomList.objects.filter(
            Q(owner=request.user) | Q(collaborators=request.user),
        ).only("tags")
        for tag in (custom_list.tags or [])
    )

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

    # Determine if this is a public view (anonymous user viewing public list)
    can_edit = custom_list.user_can_edit(request.user)
    is_public_view = custom_list.visibility == "public" and not can_edit
    public_view = not request.user.is_authenticated and custom_list.visibility == "public"

    # Determine which user's data to use for media queries
    # For public views, use owner's data; otherwise use request.user
    media_user = custom_list.owner if is_public_view else request.user

    # Get and process request parameters
    # Handle anonymous users by using default values
    if request.user.is_authenticated:
        sort_by = request.user.update_preference(
            "list_detail_sort",
            request.GET.get("sort"),
        )
    else:
        # Default sort for anonymous users
        sort_by = request.GET.get("sort", "date_added")
        # Validate sort choice
        valid_sorts = [choice[0] for choice in ListDetailSortChoices.choices]
        if sort_by not in valid_sorts:
            sort_by = "date_added"

    if request.user.is_authenticated:
        status_filter = request.user.update_preference(
            "list_detail_status",
            request.GET.get("status"),
        )
    else:
        status_filter = request.GET.get("status", MediaStatusChoices.ALL)
        valid_statuses = [choice[0] for choice in MediaStatusChoices.choices]
        if status_filter not in valid_statuses:
            status_filter = MediaStatusChoices.ALL

    params = {
        "sort_by": sort_by,
        "media_type": request.GET.get("type", "all"),
        "status_filter": status_filter,
        "page": int(request.GET.get("page", 1)),
        "search_query": request.GET.get("q", ""),
    }

    # Build and filter base queryset
    items = custom_list.items.all()
    total_items_count = items.count()
    if params["search_query"]:
        items = items.filter(title__icontains=params["search_query"])
    if params["media_type"] != "all":
        items = items.filter(media_type=params["media_type"])
    items = items.annotate(list_date_added=F("customlistitem__date_added"))

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

            queryset = model.objects.filter(**filter_kwargs).select_related("item")
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

    def _rating_value(media):
        if not media:
            return -1
        aggregated_score = getattr(media, "aggregated_score", None)
        if aggregated_score is not None:
            return aggregated_score
        if media.score is not None:
            return media.score
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

    # Apply sorting
    sort_mapping = {
        "date_added": ["-customlistitem__date_added"],
        "title": [
            F("title").asc(nulls_last=True),
            F("season_number").asc(nulls_first=True),
            F("episode_number").asc(nulls_first=True),
        ],
        "media_type": ["media_type"],
        "rating": ["-customlistitem__date_added"],  # Fallback before media-based sorting
    }

    media_sort_config = {
        "rating": {
            "key": lambda item: _rating_value(item.media),
            "reverse": True,
        },
        "progress": {
            "key": lambda item: _progress_value(item.media),
            "reverse": True,
        },
        "start_date": {
            "key": lambda item: _date_sort_value(
                _media_date_value(item.media, "start_date"),
                "asc",
            ),
            "reverse": False,
        },
        "end_date": {
            "key": lambda item: _date_sort_value(
                _media_date_value(item.media, "end_date"),
                "desc",
            ),
            "reverse": True,
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

    # Get recommendation count for owners/collaborators
    recommendation_count = 0
    if can_edit and custom_list.allow_recommendations:
        recommendation_count = custom_list.recommendations.count()

    # Base context for both full and partial responses
    chip_sort = "score" if params["sort_by"] == "rating" else params["sort_by"]
    is_partial = bool(request.headers.get("HX-Request"))
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
        "chip_sort": chip_sort,
        "current_status": params["status_filter"] or MediaStatusChoices.ALL,
        "sort_choices": ListDetailSortChoices.choices,
        "status_choices": MediaStatusChoices.choices,
        "public_view": public_view,
        "can_edit": can_edit,
        "is_public_view": is_public_view,
        "recommendation_count": recommendation_count,
        "base_template": "base_public.html" if public_view else "base.html",
        "is_partial": is_partial,
    }

    # Additional context for full page render
    if not is_partial:
        context.update(
            {
                "form": CustomListForm(instance=custom_list, user=request.user)
                if can_edit
                else None,
                "media_types": MediaTypes.values,
                "collaborators_count": custom_list.collaborators.count() + 1,
            },
        )
        return render(request, "lists/list_detail.html", context)

    # HTMX partial response
    return render(request, "lists/components/media_grid.html", context)


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
    else:
        logger.error(form.errors.as_json())
        helpers.form_error_messages(form, request)
    return helpers.redirect_back(request)


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
    try:
        item = Item.objects.get(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
        )
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
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
            title=metadata["title"],
            image=metadata["image"],
        )

    custom_lists = CustomList.objects.get_user_lists_with_item(request.user, item)

    return render(
        request,
        "lists/components/fill_lists.html",
        {"item": item, "custom_lists": custom_lists},
    )


@require_POST
def list_item_toggle(request):
    """Add or remove an item from a custom list."""
    item_id = request.POST["item_id"]
    custom_list_id = request.POST["custom_list_id"]

    item = get_object_or_404(Item, id=item_id)
    custom_list = get_object_or_404(
        CustomList.objects.filter(
            Q(owner=request.user) | Q(collaborators=request.user),
            id=custom_list_id,
        ).distinct(),  # To prevent duplicates, when user is owner and collaborator
    )

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
            title=metadata["title"],
            image=metadata["image"],
            release_datetime=release_datetime,
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
        elif item.media_type == MediaTypes.EPISODE.value and item.season_number and item.episode_number:
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
