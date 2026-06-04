"""
Views for the list recommendation workflow.

Covers: public recommendation search page, search results, submitting a
recommendation, viewing/approving/denying pending recommendations, and
list activity history.
"""

import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
    ListRecommendation,
)
from lists.views_helpers import (
    _extract_list_search_results,
    _list_item_title_fields_from_metadata,
    _maybe_backfill_episode_title,
)

logger = logging.getLogger(__name__)


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
    results, total_pages = _extract_list_search_results(media_type, data)
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
        "total_pages": total_pages,
    }

    return render(request, "lists/components/recommend_search_results.html", context)


@login_not_required
@require_POST
def submit_recommendation(request, list_id):
    """Submit a recommendation for an item to be added to a list."""
    custom_list = get_object_or_404(CustomList, id=list_id)

    if not custom_list.can_recommend():
        messages.error(request, "Recommendations are not enabled for this list.")
        return redirect("list_detail", custom_list.public_reference)

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

    return _redirect_after_submit(redirect("list_detail", custom_list.public_reference))


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
