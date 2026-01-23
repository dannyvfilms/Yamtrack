from urllib.parse import parse_qsl, urlencode, urlparse

from django.apps import apps
from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.utils.encoding import iri_to_uri
from django.utils.http import url_has_allowed_host_and_scheme

from app.models import BasicMedia, CollectionEntry, MediaTypes


def minutes_to_hhmm(total_minutes):
    """Convert total minutes to HH:MM format."""
    hours = int(total_minutes / 60)
    minutes = int(total_minutes % 60)
    if hours == 0:
        return f"{minutes}min"
    return f"{hours}h {minutes:02d}min"


def redirect_back(request):
    """Redirect to the previous page, removing the 'page' parameter if present."""
    if url_has_allowed_host_and_scheme(request.GET.get("next"), None):
        next_url = request.GET["next"]

        # Parse the URL
        parsed_url = urlparse(next_url)

        # Get the query parameters and remove params we don't want
        query_params = dict(parse_qsl(parsed_url.query))
        query_params.pop("page", None)
        query_params.pop("load_media_type", None)

        # Reconstruct the URL
        new_query = urlencode(query_params)
        new_parts = list(parsed_url)
        new_parts[4] = new_query  # index 4 is the query part

        # Convert back to a URL string
        clean_url = iri_to_uri(parsed_url._replace(query=new_query).geturl())

        return HttpResponseRedirect(clean_url)

    return redirect("home")


def form_error_messages(form, request):
    """Display form errors as messages."""
    for field, errors in form.errors.items():
        for error in errors:
            messages.error(
                request,
                f"{field.replace('_', ' ').title()}: {error}",
            )


def format_search_response(page, per_page, total_results, results):
    """Format the search response for pagination."""
    return {
        "page": page,
        "total_results": total_results,
        "total_pages": total_results // per_page + 1,
        "results": results,
    }


def enrich_items_with_user_data(request, items, user=None):
    """Enrich a list of items with user tracking data."""
    if not items:
        return []

    # Use provided user or fall back to request.user
    # If user is provided, use it (should be authenticated list owner)
    # If user is None and request.user is AnonymousUser, skip enrichment
    if user is not None:
        target_user = user
    elif request.user.is_authenticated:
        target_user = request.user
    else:
        # Anonymous user with no provided user - return items without enrichment
        return [{"item": item, "media": None} for item in items]

    # All items are the same media type
    media_type = items[0]["media_type"]
    source = items[0]["source"]

    # Build Q objects for all items
    q_objects = Q()
    for item in items:
        filter_params = {
            "item__media_id": item["media_id"],
            "item__media_type": media_type,
            "item__source": source,
        }

        if media_type == MediaTypes.SEASON.value:
            filter_params["item__season_number"] = item.get("season_number")

        q_objects |= Q(**filter_params)

    q_objects &= Q(user=target_user)

    # Bulk fetch all media with prefetch
    model = apps.get_model(app_label="app", model_name=media_type)
    media_queryset = model.objects.filter(q_objects).select_related("item")
    media_queryset = BasicMedia.objects._apply_prefetch_related(
        media_queryset,
        media_type,
    )
    BasicMedia.objects.annotate_max_progress(media_queryset, media_type)

    # For podcasts, order by created_at descending to get most recent entry when multiple exist
    # This allows multiple plays of the same episode to be tracked separately in the DB
    # but we show the most recent one in the UI
    if media_type == MediaTypes.PODCAST.value:
        media_queryset = media_queryset.order_by("item__media_id", "item__source", "-created_at")

    # Create a lookup dictionary for fast matching
    # For podcasts with multiple entries, keep only the most recent one (first after ordering)
    media_lookup = {}
    for media in media_queryset:
        if media_type == MediaTypes.SEASON.value:
            key = (media.item.media_id, media.item.source, media.item.season_number)
        else:
            key = (media.item.media_id, media.item.source)

        # Only store the first (most recent for podcasts) entry for each key
        if key not in media_lookup:
            media_lookup[key] = media

    # Enrich items with matched media
    enriched_items = []
    for item in items:
        if media_type == MediaTypes.SEASON.value:
            key = (str(item["media_id"]), item["source"], item.get("season_number"))
        else:
            key = (str(item["media_id"]), item["source"])

        enriched_item = {
            "item": item,
            "media": media_lookup.get(key),
        }
        enriched_items.append(enriched_item)

    return enriched_items


def extract_release_datetime(metadata):
    """Extract release datetime from metadata dict."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    date_str = None
    for field in ["release_date", "first_air_date", "start_date", "publish_date"]:
        if metadata.get("details", {}).get(field):
            date_str = metadata["details"][field]
            break
        if metadata.get(field):
            date_str = metadata[field]
            break

    if not date_str:
        year = metadata.get("details", {}).get("year") or metadata.get("year")
        if year:
            try:
                return datetime(int(year), 1, 1, tzinfo=ZoneInfo("UTC"))
            except (ValueError, TypeError):
                return None
        return None

    if isinstance(date_str, datetime):
        if date_str.tzinfo is None:
            return date_str.replace(tzinfo=ZoneInfo("UTC"))
        return date_str

    date_str = str(date_str)
    format_lengths = {
        "%Y-%m-%d": 10,
        "%Y-%m": 7,
        "%Y": 4,
    }
    for fmt, length in format_lengths.items():
        try:
            dt = datetime.strptime(date_str[:length], fmt)
            return dt.replace(tzinfo=ZoneInfo("UTC"))
        except (ValueError, TypeError):
            continue

    return None


def get_user_collection(user, media_type=None):
    """Get user's collection entries with optional media type filtering.

    Args:
        user: Django user object
        media_type: Optional media type to filter by

    Returns:
        QuerySet of CollectionEntry objects
    """
    queryset = CollectionEntry.objects.filter(user=user).select_related("item")
    if media_type:
        queryset = queryset.filter(item__media_type=media_type)
    return queryset


def is_item_collected(user, item):
    """Check if a specific item is in user's collection.

    Args:
        user: Django user object
        item: Item object to check

    Returns:
        CollectionEntry object if found, None otherwise
    """
    try:
        return CollectionEntry.objects.get(user=user, item=item)
    except CollectionEntry.DoesNotExist:
        return None


def get_collection_stats(user):
    """Get collection statistics for a user.

    Args:
        user: Django user object

    Returns:
        Dictionary with collection statistics:
        - total: Total number of collection entries
        - by_media_type: Count by media type
        - by_format: Count by media_type (format) field
    """
    collection = CollectionEntry.objects.filter(user=user)
    stats = {
        "total": collection.count(),
        "by_media_type": {},
        "by_format": {},
    }

    # Count by media type (Item.media_type)
    for entry in collection.select_related("item"):
        item_media_type = entry.item.media_type
        stats["by_media_type"][item_media_type] = (
            stats["by_media_type"].get(item_media_type, 0) + 1
        )

        # Count by format (CollectionEntry.media_type)
        if entry.media_type:
            stats["by_format"][entry.media_type] = (
                stats["by_format"].get(entry.media_type, 0) + 1
            )

    return stats
