import logging
import time
from datetime import UTC, timedelta
from pathlib import Path

from dateutil.relativedelta import relativedelta
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import EmptyPage, Paginator
from django.db import IntegrityError
from django.db.models import prefetch_related_objects
from django.db.utils import OperationalError
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.timezone import datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from app import (
    cache_utils,
    config,
    helpers,
    history_cache,
    history_processor,
    statistics as stats,
    statistics_cache,
)
from app.forms import EpisodeForm, ManualItemForm, get_form_class
from app.models import (
    TV,
    Album,
    Artist,
    BasicMedia,
    Item,
    MediaTypes,
    Music,
    Season,
    Sources,
    Status,
    Track,
)
from app.providers import manual, services, tmdb
from app.services import music as sync_services
from app.templatetags import app_tags
from lists.models import CustomList
from users.models import HomeSortChoices, MediaSortChoices, MediaStatusChoices

logger = logging.getLogger(__name__)

MEDIA_RATING_CHOICES = (
    ("all", "All"),
    ("rated", "Rated"),
    ("not_rated", "Not Rated"),
)
RECENTLY_NOT_RATED_KEY = "recently_not_rated"
RECENTLY_NOT_RATED_LABEL = "Recently Played - Not Rated"
RECENTLY_NOT_RATED_DAYS = 7


@require_GET
def home(request):
    """Home page with media items in progress."""
    try:
        sort_by = request.user.update_preference("home_sort", request.GET.get("sort"))
        media_type_to_load = request.GET.get("load_media_type")
        items_limit = 14

        if request.headers.get("HX-Request") and media_type_to_load == RECENTLY_NOT_RATED_KEY:
            recent_items = BasicMedia.objects.get_recently_unrated(
                request.user,
                days=RECENTLY_NOT_RATED_DAYS,
            )
            context = {
                "media_list": {
                    "items": recent_items[items_limit:],
                    "total": len(recent_items),
                    "show_played_chip": True,
                },
            }
            return render(request, "app/components/home_grid.html", context)

        if media_type_to_load == RECENTLY_NOT_RATED_KEY:
            media_type_to_load = None

        list_by_type = BasicMedia.objects.get_in_progress(
            request.user,
            sort_by,
            items_limit,
            media_type_to_load,
        )

        # If this is an HTMX request to load more items for a specific media type
        if request.headers.get("HX-Request") and media_type_to_load:
            context = {
                "media_list": list_by_type.get(media_type_to_load, []),
            }
            return render(request, "app/components/home_grid.html", context)

        recent_items = BasicMedia.objects.get_recently_unrated(
            request.user,
            days=RECENTLY_NOT_RATED_DAYS,
        )
        if recent_items:
            list_by_type[RECENTLY_NOT_RATED_KEY] = {
                "items": recent_items[:items_limit],
                "total": len(recent_items),
                "section_title": RECENTLY_NOT_RATED_LABEL,
                "show_played_chip": True,
            }

        context = {
            "user": request.user,
            "list_by_type": list_by_type,
            "current_sort": sort_by,
            "sort_choices": HomeSortChoices.choices,
            "items_limit": items_limit,
        }
        return render(request, "app/home.html", context)
    except OperationalError as error:
        logger.error("Database error in home view: %s", error, exc_info=True)
        # Return empty state on database error
        context = {
            "user": request.user,
            "list_by_type": {},
            "current_sort": request.GET.get("sort", "progress"),
            "sort_choices": HomeSortChoices.choices,
            "items_limit": 14,
            "database_error": True,
        }
        return render(request, "app/home.html", context)


@require_POST
def progress_edit(request, media_type, instance_id):
    """Increase or decrease the progress of a media item from home page."""
    operation = request.POST["operation"]

    media = BasicMedia.objects.get_media_prefetch(
        request.user,
        media_type,
        instance_id,
    )

    if operation == "increase":
        media.increase_progress()
    elif operation == "decrease":
        media.decrease_progress()

    if media_type == MediaTypes.SEASON.value:
        # clear prefetch cache to get the updated episodes
        media.refresh_from_db()
        prefetch_related_objects([media], "episodes")

    context = {
        "media": media,
    }
    return render(
        request,
        "app/components/progress_changer.html",
        context,
    )


@require_GET
def media_list(request, media_type):
    """Return the media list page."""
    previous_sort = getattr(request.user, f"{media_type}_sort")
    layout = request.user.update_preference(
        f"{media_type}_layout",
        request.GET.get("layout"),
    )
    sort_filter = request.user.update_preference(
        f"{media_type}_sort",
        request.GET.get("sort"),
    )
    direction_param = request.GET.get("direction")
    direction_field = f"{media_type}_direction"

    # If time_left sort is selected for non-TV media types, fallback to default
    if sort_filter == "time_left" and media_type != MediaTypes.TV.value:
        sort_filter = "title"  # Default fallback
        # Update the user's preference to the fallback
        request.user.update_preference(f"{media_type}_sort", "title")
        # Reset direction to the default for the fallback sort
        direction_param = None

    # Resolve and persist sort direction with the same preference flow as sort
    direction_pref = getattr(request.user, direction_field, None)
    if direction_param is not None:
        direction = BasicMedia.objects.resolve_direction(sort_filter, direction_param)
        request.user.update_preference(direction_field, direction)
    else:
        if sort_filter != previous_sort or direction_pref is None:
            direction = BasicMedia.objects.resolve_direction(sort_filter, None)
        else:
            direction = BasicMedia.objects.resolve_direction(sort_filter, direction_pref)
        request.user.update_preference(direction_field, direction)
    status_filter = request.user.update_preference(
        f"{media_type}_status",
        request.GET.get("status"),
    )
    rating_filter = request.GET.get("rating", "all")
    if rating_filter not in {choice[0] for choice in MEDIA_RATING_CHOICES}:
        rating_filter = "all"
    search_query = request.GET.get("search", "")
    try:
        page = int(request.GET.get("page", 1))
    except (ValueError, TypeError):
        page = 1

    # Prepare status filter for database query
    if not status_filter:
        status_filter = MediaStatusChoices.ALL

    def is_rated(media):
        aggregated_score = getattr(media, "aggregated_score", None)
        if aggregated_score is not None:
            return True
        return media.score is not None

    def apply_rating_filter(media_items, filter_value):
        if filter_value == "all":
            return media_items
        should_be_rated = filter_value == "rated"
        return [media for media in media_items if is_rated(media) == should_be_rated]

    # Get media list with filters applied
    media_queryset = BasicMedia.objects.get_media_list(
        user=request.user,
        media_type=media_type,
        status_filter=status_filter,
        sort_filter=sort_filter,
        search=search_query,
        direction=direction,
    )
    media_queryset = apply_rating_filter(media_queryset, rating_filter)

    # Handle time_left sorting for TV shows
    if sort_filter == "time_left" and media_type == MediaTypes.TV.value:
        import logging

        from django.core.cache import cache

        logger = logging.getLogger(__name__)

        # Cache sorted results for 5 minutes to avoid expensive re-sorts
        cache_key = cache_utils.build_time_left_cache_key(
            request.user.id,
            media_type,
            status_filter,
            search_query,
            direction,
            rating_filter,
        )
        cached_results = cache.get(cache_key)

        if cached_results is not None:
            logger.debug(f"DEBUG: Using cached time_left sort (page {page})")
            media_list = cached_results
        else:
            logger.debug(f"DEBUG: Starting time_left sort for page {page} (no cache)")

            # Get all media objects for sorting
            media_list = list(media_queryset)
            logger.debug(f"DEBUG: Got {len(media_list)} media objects from queryset")

            # Annotate max_progress first
            BasicMedia.objects.annotate_max_progress(media_list, media_type)
            logger.debug("DEBUG: Annotated max_progress for all media")

            # Apply time_left sorting
            media_list = _sort_tv_media_by_time_left(media_list, direction)
            logger.debug("DEBUG: Applied time_left sorting")

            # Cache for 5 minutes (300 seconds)
            cache.set(cache_key, media_list, 300)
            cache_utils.register_time_left_cache_key(request.user.id, cache_key)

        # Paginate the sorted list
        items_per_page = 32
        paginator = Paginator(media_list, items_per_page)
        media_page = paginator.get_page(page)

        logger.debug(f"DEBUG: Paginated to page {page} of {paginator.num_pages} pages")
        logger.debug(f"DEBUG: This page has {len(media_page)} items")

        # Log the first few items on this page to see what's being displayed
        logger.debug(f"DEBUG: First 5 items on page {page}:")
        for i, media in enumerate(media_page[:5]):
            episodes_left = media.max_progress - media.progress if hasattr(media, "max_progress") else 0
            logger.debug(f"  {i+1}. {media.item.title} - Episodes left: {episodes_left}, Status: {getattr(media, 'status', 'Unknown')}")

        # Additional debug info for pagination issues
        logger.debug(f"DEBUG: Page {page} pagination info - has_next: {media_page.has_next()}, next_page: {media_page.next_page_number() if media_page.has_next() else 'None'}")
        if hasattr(media_page, "has_previous") and media_page.has_previous():
            logger.debug(f"DEBUG: Page {page} has previous page: {media_page.previous_page_number()}")
    else:
        # Paginate results normally
        items_per_page = 32
        paginator = Paginator(media_queryset, items_per_page)
        media_page = paginator.get_page(page)

        BasicMedia.objects.annotate_max_progress(
            media_page.object_list,
            media_type,
        )

    context = {
        "user": request.user,
        "media_type": media_type,
        "media_type_plural": app_tags.media_type_readable_plural(media_type).lower(),
        "media_list": media_page,
        "current_layout": layout,
        "layout_class": ".media-grid" if layout == "grid" else ".media-table",
        "current_sort": sort_filter,
        "current_direction": direction,
        "current_status": status_filter,
        "current_rating": rating_filter,
        "sort_choices": MediaSortChoices.choices,
        "status_choices": MediaStatusChoices.choices,
        "rating_choices": MEDIA_RATING_CHOICES,
    }

    # For music, show tracked artists instead of individual tracks
    # For podcasts, show tracked shows instead of individual episodes
    # This parallels TV which shows TV shows, not seasons/episodes
    if media_type == MediaTypes.PODCAST.value:
        from django.conf import settings

        from app.models import Item, PodcastShowTracker, Sources

        show_trackers = (
            PodcastShowTracker.objects.filter(user=request.user)
            .exclude(show__title__isnull=True)
            .exclude(show__title__exact="")
            .select_related("show")
        )

        # Apply status filter to shows
        if status_filter and status_filter != MediaStatusChoices.ALL:
            show_trackers = show_trackers.filter(status=status_filter)

        # Apply search filter to shows
        if search_query:
            show_trackers = show_trackers.filter(show__title__icontains=search_query)

        # Apply rating filter to shows
        if rating_filter == "rated":
            show_trackers = show_trackers.filter(score__isnull=False)
        elif rating_filter == "not_rated":
            show_trackers = show_trackers.filter(score__isnull=True)

        # Apply sorting
        if sort_filter == "title":
            order = "show__title" if direction == "asc" else "-show__title"
            show_trackers = show_trackers.order_by(order)
        elif sort_filter == "score":
            order = "score" if direction == "asc" else "-score"
            show_trackers = show_trackers.order_by(order, "show__title")
        elif sort_filter == "start_date":
            order = "start_date" if direction == "asc" else "-start_date"
            show_trackers = show_trackers.order_by(order)
        else:
            # Default: most recently updated
            show_trackers = show_trackers.order_by("-updated_at")

        # Convert show trackers to Media-like objects for standard templates
        # Create a simple adapter class to make trackers compatible with media components
        class PodcastShowAdapter:
            """Adapter to make PodcastShowTracker compatible with media components."""

            def __init__(self, tracker):
                self.tracker = tracker
                self.id = tracker.id
                self.status = tracker.status
                self.score = tracker.score
                self.start_date = tracker.start_date
                self.end_date = tracker.end_date
                self.notes = tracker.notes
                self.created_at = tracker.created_at
                self.updated_at = tracker.updated_at

                # Create a mock Item for compatibility with media components
                # Use the show's podcast_uuid as media_id for routing
                self.item, _ = Item.objects.get_or_create(
                    media_id=tracker.show.podcast_uuid,
                    source=Sources.POCKETCASTS.value,
                    media_type=MediaTypes.PODCAST.value,
                    defaults={
                        "title": tracker.show.title,
                        "image": tracker.show.image or settings.IMG_NONE,
                    },
                )
                # Update item if show data changed
                # Always sync image to ensure it matches the show (especially after artwork fetch)
                show_image = tracker.show.image or settings.IMG_NONE
                if self.item.title != tracker.show.title or self.item.image != show_image:
                    self.item.title = tracker.show.title
                    self.item.image = show_image
                    self.item.save(update_fields=["title", "image"])

        # Convert trackers to adapters
        adapted_media = [PodcastShowAdapter(tracker) for tracker in show_trackers]

        # Paginate adapted media
        media_paginator = Paginator(adapted_media, 32)
        media_page = media_paginator.get_page(page)

        context = {
            "user": request.user,
            "media_list": media_page,
            "media_type": media_type,
            "media_type_plural": app_tags.media_type_readable_plural(media_type).lower(),
            "current_layout": layout,
            "layout_class": ".media-grid" if layout == "grid" else ".media-table",
            "current_sort": sort_filter,
            "current_direction": direction,
            "current_status": status_filter,
            "current_rating": rating_filter,
            "sort_choices": MediaSortChoices.choices,
            "status_choices": MediaStatusChoices.choices,
            "rating_choices": MEDIA_RATING_CHOICES,
            "search_query": search_query,
        }

        # Handle HTMX requests for partial updates
        if request.headers.get("HX-Request"):
            if request.headers.get("HX-Target") == "empty_list":
                response = HttpResponse()
                response["HX-Redirect"] = reverse("medialist", args=[media_type])
                return response

            is_pagination = request.GET.get("page") and int(request.GET.get("page", 1)) > 1
            context["is_pagination"] = bool(is_pagination)

            if layout == "grid":
                template_name = "app/components/media_grid_items.html"
            else:
                template_name = "app/components/media_table_items.html"
        else:
            context["is_pagination"] = False
            template_name = "app/media_list.html"

        return render(request, template_name, context)

    if media_type == MediaTypes.MUSIC.value:
        from django.conf import settings

        from app.models import Artist, ArtistTracker
        from app.services.music import get_artist_hero_image

        artist_trackers = (
            ArtistTracker.objects.filter(user=request.user)
            .exclude(artist__name__isnull=True)
            .exclude(artist__name__exact="")
            .select_related("artist")
        )

        # Apply status filter to artists
        if status_filter and status_filter != MediaStatusChoices.ALL:
            artist_trackers = artist_trackers.filter(status=status_filter)

        # Apply search filter to artists
        if search_query:
            artist_trackers = artist_trackers.filter(artist__name__icontains=search_query)

        # Apply rating filter to artists
        if rating_filter == "rated":
            artist_trackers = artist_trackers.filter(score__isnull=False)
        elif rating_filter == "not_rated":
            artist_trackers = artist_trackers.filter(score__isnull=True)

        # Apply sorting (limited to what makes sense for artists)
        if sort_filter == "title":
            order = "artist__name" if direction == "asc" else "-artist__name"
            artist_trackers = artist_trackers.order_by(order)
        elif sort_filter == "score":
            order = "score" if direction == "asc" else "-score"
            artist_trackers = artist_trackers.order_by(order, "artist__name")
        elif sort_filter == "start_date":
            order = "start_date" if direction == "asc" else "-start_date"
            artist_trackers = artist_trackers.order_by(order)
        else:
            # Default: most recently updated
            artist_trackers = artist_trackers.order_by("-updated_at")

        # Paginate artist trackers first
        artist_paginator = Paginator(artist_trackers, 32)
        artist_page = artist_paginator.get_page(page)

        # Backfill missing artist images from album covers (no API calls - uses existing data)
        # Similar to _fix_missing_season_images for TV seasons
        # First, bulk fetch latest image data from DB for all artists on this page
        # (images might have been set by background tasks, detail page visits, etc.)
        # This is more efficient and reliable than individual refresh_from_db calls
        artist_ids = [tracker.artist.id for tracker in artist_page.object_list]
        artist_images_map = dict(
            Artist.objects.filter(id__in=artist_ids)
            .values_list("id", "image"),
        )

        refreshed_with_images = 0
        images_in_db_count = 0
        for tracker in artist_page.object_list:
            artist_id = tracker.artist.id
            old_image = tracker.artist.image
            # Get the latest image from DB (may be None if not in map or if DB value is None)
            # Use get() with a sentinel to distinguish "not in map" from "None in DB"
            new_image = artist_images_map.get(artist_id, object())  # object() as sentinel

            # Always update the in-memory object with the latest image from DB
            # This ensures we have the most up-to-date data, even if it's None
            if artist_id in artist_images_map:
                # Get the actual value (could be None if DB has None)
                actual_image = artist_images_map[artist_id]
                tracker.artist.image = actual_image
                # Count images that exist in DB (for logging)
                if actual_image and actual_image != settings.IMG_NONE and actual_image != "":
                    images_in_db_count += 1
                # Count if refresh found an image that wasn't there before
                if (actual_image and actual_image != settings.IMG_NONE and
                    actual_image != "" and
                    (not old_image or old_image == settings.IMG_NONE or old_image == "")):
                    refreshed_with_images += 1

        # Only backfill images for artists on the current page to avoid full queryset evaluation
        # Use object_list to avoid consuming the page iterator (important for HTMX pagination)
        artists_to_update = []
        seen_artist_ids = set()
        artist_id_to_updated_image = {}  # Track which artists got updated images
        artists_checked = 0
        artists_with_images = 0
        artists_missing_images = 0

        for tracker in artist_page.object_list:
            artist = tracker.artist
            if artist.id not in seen_artist_ids:
                seen_artist_ids.add(artist.id)
                artists_checked += 1

                # Check if artist already has an image (handle both None and empty string)
                # This check happens AFTER refresh, so we have the latest data
                has_image = artist.image and artist.image != settings.IMG_NONE and artist.image != ""
                if has_image:
                    artists_with_images += 1
                else:
                    artists_missing_images += 1
                    # Try to get hero image from albums
                    hero_image = get_artist_hero_image(artist)
                    if hero_image and hero_image != settings.IMG_NONE:
                        artist.image = hero_image
                        artists_to_update.append(artist)
                        artist_id_to_updated_image[artist.id] = hero_image

        # Log backfill attempt (always, not just when updates happen)
        is_pagination_req = bool(request.GET.get("page") and int(request.GET.get("page", 1)) > 1)
        # Use module-level logger via logging module to avoid conflict with local 'logger' variable
        # (there's a local 'logger' assignment on line 168 that makes Python treat it as local)
        import logging as _logging_module
        _log = _logging_module.getLogger(__name__)
        _log.debug(
            "Artist image backfill check (page %d, pagination=%s): checked %d artists, %d had images in DB, %d had images after refresh, %d missing, %d updated from albums",
            page,
            is_pagination_req,
            artists_checked,
            images_in_db_count,
            artists_with_images,
            artists_missing_images,
            len(artists_to_update),
        )

        if artists_to_update:
            Artist.objects.bulk_update(artists_to_update, ["image"])
            _log.info(
                "Backfilled %d artist images from album covers (page %d, pagination=%s)",
                len(artists_to_update),
                page,
                is_pagination_req,
            )

        # Ensure all tracker artist references have the correct image
        # Update in-memory objects with images we just set via bulk_update
        for tracker in artist_page.object_list:
            if tracker.artist.id in artist_id_to_updated_image:
                # Update the in-memory artist object with the new image we just set
                tracker.artist.image = artist_id_to_updated_image[tracker.artist.id]

        if refreshed_with_images > 0:
            _log.info(
                "Refreshed %d artists from DB that now have images (page %d, pagination=%s)",
                refreshed_with_images,
                page,
                is_pagination_req,
            )

        # Replace media_list with artist trackers for music
        # Use the page object directly - it's already iterable and has all pagination metadata
        # This ensures HTMX pagination works correctly and images are backfilled for new pages
        context["media_list"] = artist_page
        context["is_artist_list"] = True

    # Handle HTMX requests for partial updates
    if request.headers.get("HX-Request"):
        is_artist_list = context.get("is_artist_list", False)
        # Changing from empty list to a status with items
        if request.headers.get("HX-Target") == "empty_list":
            response = HttpResponse()
            response["HX-Redirect"] = reverse("medialist", args=[media_type])
            return response

        # Check if this is a pagination request (has page parameter and is not the first page)
        is_pagination = request.GET.get("page") and int(request.GET.get("page", 1)) > 1
        context["is_pagination"] = bool(is_pagination)

        if layout == "grid":
            template_name = (
                "app/components/artist_grid_items.html"
                if is_artist_list
                else "app/components/media_grid_items.html"
            )
        else:
            template_name = (
                "app/components/artist_table_items.html"
                if is_artist_list
                else "app/components/media_table_items.html"
            )
    else:
        context["is_pagination"] = False
        template_name = "app/media_list.html"

    return render(request, template_name, context)


@require_GET
def media_search(request):
    """Return the media search page."""
    media_type = request.user.update_preference(
        "last_search_type",
        request.GET["media_type"],
    )
    query = request.GET["q"]
    page = int(request.GET.get("page", 1))
    layout = request.GET.get("layout", "grid")

    # only receives source when searching with secondary source
    source = request.GET.get(
        "source",
        config.get_default_source_name(media_type).value,
    )

    data = services.search(media_type, query, page, source)

    # Handle music's combined search format
    if media_type == MediaTypes.MUSIC.value:
        # Music returns {artists: [], releases: [], tracks: {...}}
        track_data = data.get("tracks", {})
        if track_data.get("results"):
            track_data["results"] = helpers.enrich_items_with_user_data(
                request, track_data["results"],
            )

        context = {
            "user": request.user,
            "data": track_data,  # Track results for pagination
            "music_artists": data.get("artists", []),
            "music_releases": data.get("releases", []),
            "source": source,
            "media_type": media_type,
            "layout": layout,
        }
        return render(request, "app/search_music.html", context)

    # Enrich search results with user tracking data
    if data.get("results"):
        data["results"] = helpers.enrich_items_with_user_data(request, data["results"])

    context = {
        "user": request.user,
        "data": data,
        "source": source,
        "media_type": media_type,
        "layout": layout,
    }

    return render(request, "app/search.html", context)


@login_not_required
@require_GET
def media_details(
    request, source, media_type, media_id, title,
):
    """Return the details page for a media item."""
    # Check if this is a public view (from query parameter)
    public_view = request.GET.get("public_view") == "1" and not request.user.is_authenticated

    # For public views, find a public list containing this item to get the owner
    list_owner = None
    if public_view:
        try:
            # Get or create the Item for this media
            item, _ = Item.objects.get_or_create(
                media_id=media_id,
                source=source,
                media_type=media_type,
                defaults={"title": "", "image": settings.IMG_NONE},
            )
            # Find a public list containing this item
            public_list = CustomList.objects.filter(
                visibility="public",
                items=item,
            ).select_related("owner").first()
            if public_list:
                list_owner = public_list.owner
        except Exception:
            # If we can't find a list owner, list_owner stays None
            pass

    # For podcast shows (identified by podcast_uuid), show show detail page
    if media_type == MediaTypes.PODCAST.value and source == Sources.POCKETCASTS.value:
        from app.models import PodcastEpisode, PodcastShow, PodcastShowTracker

        # Check if this is a show (podcast_uuid) or an episode (episode_uuid)
        show = PodcastShow.objects.filter(podcast_uuid=media_id).first()

        # If show not found, check if media_id is an iTunes ID and enrich
        if not show:
            # Check if media_id looks like an iTunes collection ID (numeric string)
            try:
                int(media_id)  # Will raise ValueError if not numeric
                # This looks like an iTunes ID, try to enrich
                import hashlib

                from django.contrib import messages
                from django.shortcuts import redirect

                from app.providers import pocketcasts
                from integrations import podcast_rss

                try:
                    # Look up podcast by iTunes ID
                    itunes_data = pocketcasts.lookup_by_itunes_id(media_id)
                    rss_feed_url = itunes_data.get("feed_url", "")

                    if not rss_feed_url:
                        messages.error(request, "Could not find RSS feed for this podcast.")
                        # Fall through to empty metadata
                    else:
                        # Check if show already exists with this RSS feed
                        existing_show = PodcastShow.objects.filter(rss_feed_url=rss_feed_url).first()
                        if existing_show:
                            # Redirect to existing show
                            from django.utils.text import slugify
                            return redirect(
                                "media_details",
                                source=Sources.POCKETCASTS.value,
                                media_type=MediaTypes.PODCAST.value,
                                media_id=existing_show.podcast_uuid,
                                title=slugify(existing_show.title or "podcast"),
                            )

                        # Create new show with iTunes ID as UUID prefix
                        podcast_uuid = f"itunes:{media_id}"

                        # Check if UUID already exists (shouldn't, but be safe)
                        if PodcastShow.objects.filter(podcast_uuid=podcast_uuid).exists():
                            show = PodcastShow.objects.get(podcast_uuid=podcast_uuid)
                        else:
                            # Try to get description from RSS feed if iTunes doesn't have it or it's empty
                            description = itunes_data.get("description", "")
                            if not description and rss_feed_url:
                                try:
                                    rss_metadata = podcast_rss.fetch_show_metadata_from_rss(rss_feed_url)
                                    description = rss_metadata.get("description", description)
                                    # Update author and language from RSS if not in iTunes data
                                    if not itunes_data.get("author") and rss_metadata.get("author"):
                                        itunes_data["author"] = rss_metadata["author"]
                                    if not itunes_data.get("language") and rss_metadata.get("language"):
                                        itunes_data["language"] = rss_metadata["language"]
                                except Exception as e:
                                    logger.debug("Failed to fetch show metadata from RSS: %s", e)

                            # Create the show
                            show = PodcastShow.objects.create(
                                podcast_uuid=podcast_uuid,
                                title=itunes_data.get("title", "Unknown Podcast"),
                                author=itunes_data.get("author", ""),
                                image=itunes_data.get("artwork_url", ""),
                                description=description,
                                genres=itunes_data.get("genres", []),
                                language=itunes_data.get("language", ""),
                                rss_feed_url=rss_feed_url,
                            )

                            # Fetch episodes from RSS feed (fetch all, no limit)
                            try:
                                episodes_data = podcast_rss.fetch_episodes_from_rss(rss_feed_url, limit=None)

                                for episode_data in episodes_data:
                                    # Generate episode UUID from GUID or create one
                                    # Use GUID directly (consistent with _sync_episodes_from_rss logic)
                                    episode_uuid = episode_data.get("guid")
                                    if not episode_uuid:
                                        # Use a hash of title + published date as fallback UUID
                                        import hashlib
                                        uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                                        episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                                    # Check if episode already exists by UUID, or try to match by title + date
                                    episode = None
                                    try:
                                        episode = PodcastEpisode.objects.get(episode_uuid=episode_uuid)
                                    except PodcastEpisode.DoesNotExist:
                                        # Try to match by title + published date
                                        if episode_data.get("title") and episode_data.get("published"):
                                            matching = PodcastEpisode.objects.filter(
                                                show=show,
                                                title__iexact=episode_data["title"].strip(),
                                                published__date=episode_data["published"].date(),
                                            ).first()
                                            if matching:
                                                episode = matching
                                    except PodcastEpisode.MultipleObjectsReturned:
                                        # If multiple found, use first one
                                        episode = PodcastEpisode.objects.filter(episode_uuid=episode_uuid).first()

                                    if not episode:
                                        PodcastEpisode.objects.create(
                                            show=show,
                                            episode_uuid=episode_uuid,
                                            title=episode_data.get("title", "Unknown Episode"),
                                            published=episode_data.get("published"),
                                            duration=episode_data.get("duration"),
                                            audio_url=episode_data.get("audio_url", ""),
                                            episode_number=episode_data.get("episode_number"),
                                            season_number=episode_data.get("season_number"),
                                        )
                            except Exception as e:
                                logger.warning("Failed to fetch episodes from RSS feed %s: %s", rss_feed_url, e)
                                # Continue without episodes

                        # Redirect to the new/enriched show
                        from django.utils.text import slugify
                        return redirect(
                            "media_details",
                            source=Sources.POCKETCASTS.value,
                            media_type=MediaTypes.PODCAST.value,
                            media_id=show.podcast_uuid,
                            title=slugify(show.title or "podcast"),
                        )
                except Exception as e:
                    logger.error("Failed to enrich podcast from iTunes ID %s: %s", media_id, e, exc_info=True)
                    messages.error(request, f"Failed to load podcast details: {e}")
                    # Fall through to empty metadata
            except ValueError:
                # media_id is not numeric, not an iTunes ID - fall through to empty metadata
                pass

        if show:
            # This is a show, not an episode - show show detail page
            tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first() if not public_view else None

            # If show has RSS feed, check if we need to fetch more episodes
            # This ensures we get the full episode list even if initial enrichment only got partial list
            if show.rss_feed_url and not public_view:
                try:
                    import hashlib

                    from integrations import podcast_rss

                    # Fetch all episodes from RSS to see what's available
                    episodes_data = podcast_rss.fetch_episodes_from_rss(show.rss_feed_url, limit=None)

                    # Get existing episode UUIDs
                    existing_uuids = set(
                        PodcastEpisode.objects.filter(show=show).values_list("episode_uuid", flat=True),
                    )

                    # Create any missing episodes
                    new_episodes_count = 0
                    for episode_data in episodes_data:
                        # Generate episode UUID from GUID or create one
                        # Use GUID directly (consistent with _sync_episodes_from_rss logic)
                        episode_uuid = episode_data.get("guid")
                        if not episode_uuid:
                            # Use a hash of title + published date as fallback UUID
                            uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                            episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                        # Check if episode already exists by UUID, or try to match by title + date
                        episode = None
                        try:
                            episode = PodcastEpisode.objects.get(episode_uuid=episode_uuid)
                        except PodcastEpisode.DoesNotExist:
                            # Try to match by title + published date
                            if episode_data.get("title") and episode_data.get("published"):
                                matching = PodcastEpisode.objects.filter(
                                    show=show,
                                    title__iexact=episode_data["title"].strip(),
                                    published__date=episode_data["published"].date(),
                                ).first()
                                if matching:
                                    episode = matching
                        except PodcastEpisode.MultipleObjectsReturned:
                            # If multiple found, use first one
                            episode = PodcastEpisode.objects.filter(episode_uuid=episode_uuid).first()

                        # Create episode if it doesn't exist
                        if not episode and episode_uuid not in existing_uuids:
                            PodcastEpisode.objects.create(
                                show=show,
                                episode_uuid=episode_uuid,
                                title=episode_data.get("title", "Unknown Episode"),
                                published=episode_data.get("published"),
                                duration=episode_data.get("duration"),
                                audio_url=episode_data.get("audio_url", ""),
                                episode_number=episode_data.get("episode_number"),
                                season_number=episode_data.get("season_number"),
                            )
                            new_episodes_count += 1
                            existing_uuids.add(episode_uuid)

                    if new_episodes_count > 0:
                        logger.info("Fetched %d additional episodes for show %s (ID: %d)", new_episodes_count, show.title, show.id)
                except Exception as e:
                    logger.debug("Failed to refresh episode list from RSS feed %s: %s", show.rss_feed_url, e)
                    # Continue with existing episodes

            # Get all episodes for this show, ordered by published date (newest first)
            # Use Coalesce to handle None published dates (put them at the end)
            from datetime import datetime

            from django.db.models import DateTimeField, Value
            from django.db.models.functions import Coalesce

            episodes = PodcastEpisode.objects.filter(show=show).annotate(
                published_or_old=Coalesce(
                    "published",
                    Value(datetime(1970, 1, 1, tzinfo=UTC),
                          output_field=DateTimeField()),
                ),
            ).order_by("-published_or_old", "-episode_number")

            # Get user's podcast entries for this show
            if not public_view:
                from app.models import Podcast
                user_podcasts = list(Podcast.objects.filter(
                    user=request.user,
                    show=show,
                ).select_related("episode", "item"))
                total_listened = len(user_podcasts)
                total_minutes = sum(podcast.progress or 0 for podcast in user_podcasts)

                # Count unplayed episodes (episodes without a completed Podcast entry for this user)
                completed_episode_ids = set(podcast.episode.id for podcast in user_podcasts if podcast.episode and podcast.end_date)
                if completed_episode_ids:
                    unplayed_count = episodes.exclude(id__in=completed_episode_ids).count()
                else:
                    # If no episodes have been completed, all episodes are unplayed
                    unplayed_count = episodes.count()
            else:
                user_podcasts = []
                total_listened = 0
                total_minutes = 0
                # For public views, still count total episodes (but button won't show due to public_view check)
                unplayed_count = episodes.count()

            # Build episode items - create Item objects for enrichment
            # Initially load first 20 episodes, rest will be loaded via infinite scroll
            from app.models import Item
            episode_items_data = []
            episode_items_map = {}  # Map media_id to Item object
            initial_limit = 20
            for episode in episodes[:initial_limit]:
                item, _ = Item.objects.get_or_create(
                    media_id=episode.episode_uuid,
                    source=source,
                    media_type=media_type,
                    defaults={
                        "title": episode.title,
                        "image": show.image or settings.IMG_NONE,
                    },
                )
                # Update if needed
                if item.title != episode.title:
                    item.title = episode.title
                    item.save(update_fields=["title"])
                # enrich_items_with_user_data expects dicts with media_id, source, media_type
                episode_items_data.append({
                    "media_id": episode.episode_uuid,
                    "source": source,
                    "media_type": media_type,
                })
                episode_items_map[episode.episode_uuid] = item

            # Enrich episodes with user data
            enriched_episodes_raw = helpers.enrich_items_with_user_data(
                request,
                episode_items_data,
                user=None if public_view else request.user,
            )

            # Replace dict items with Item model instances
            enriched_episodes = []
            for enriched in enriched_episodes_raw:
                # Get the Item object from our map
                item_obj = episode_items_map.get(enriched["item"]["media_id"])
                if item_obj:
                    enriched_episodes.append({
                        "item": item_obj,
                        "media": enriched["media"],
                    })
                else:
                    # Fallback: fetch Item from database
                    enriched_episodes.append({
                        "item": Item.objects.get(
                            media_id=enriched["item"]["media_id"],
                            source=enriched["item"]["source"],
                            media_type=enriched["item"]["media_type"],
                        ),
                        "media": enriched["media"],
                    })

            # Build episode data in TV season format (inline episodes, not related items)
            episode_list = []
            for episode_obj, enriched in zip(episodes[:initial_limit], enriched_episodes):
                # Format duration
                duration_str = ""
                if episode_obj.duration:
                    hours = episode_obj.duration // 3600
                    minutes = (episode_obj.duration % 3600) // 60
                    if hours > 0:
                        duration_str = f"{hours}h {minutes}m"
                    else:
                        duration_str = f"{minutes}m"

                # Get user's podcast media for this episode
                episode_media = enriched["media"]
                episode_history = []
                if episode_media:
                    # Get history for this episode using simple_history
                    # Media instances have a .history relationship from HistoricalRecords
                    # Only include history records with end_date (completed plays)
                    episode_history = list(episode_media.history.filter(end_date__isnull=False).order_by("-end_date")[:10])

                # Create adapter objects for music-style modal (like track_modal does)
                class PodcastEpisodeAdapter:
                    """Adapter to make PodcastEpisode work like Track in template."""

                    def __init__(self, episode):
                        self.title = episode.title
                        self.track_number = episode.episode_number
                        self.duration_formatted = self._format_duration(episode.duration) if episode.duration else None
                        self.musicbrainz_recording_id = None  # Not used for podcasts
                        self.id = episode.id
                        self.published = episode.published  # For "Published date" button
                        self.episode_uuid = episode.episode_uuid  # For form submission when music is None

                    def _format_duration(self, seconds):
                        """Format duration in seconds to MM:SS or H:MM:SS."""
                        hours = seconds // 3600
                        minutes = (seconds % 3600) // 60
                        secs = seconds % 60
                        if hours > 0:
                            return f"{hours}:{minutes:02d}:{secs:02d}"
                        return f"{minutes}:{secs:02d}"

                class PodcastShowAdapter:
                    """Adapter to make PodcastShow work like Album in template."""

                    def __init__(self, show):
                        self.image = show.image or settings.IMG_NONE
                        self.release_date = None  # Podcasts don't have release dates
                        self.id = show.id

                # Get all Podcast entries for this episode to aggregate history
                all_podcasts = list(Podcast.objects.filter(
                    user=request.user if not public_view else None,
                    show=show,
                    episode=episode_obj,
                ).order_by("-end_date")) if not public_view else []

                # Create a wrapper object that aggregates history from all podcast entries
                if all_podcasts:
                    # Aggregate all history records from all podcast entries
                    # Only include history records with end_date (completed plays)
                    all_history = []
                    for podcast in all_podcasts:
                        # Only include history records with end_date (completed plays)
                        history = podcast.history.filter(end_date__isnull=False) if hasattr(podcast.history, "filter") else [h for h in podcast.history.all() if h.end_date]
                        # Convert queryset to list if needed to ensure proper evaluation
                        if hasattr(history, "__iter__") and not isinstance(history, (list, tuple)):
                            history = list(history)
                        all_history.extend(history)

                    # Sort by end_date descending (most recent first) for display
                    all_history.sort(
                        key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                        reverse=True,
                    )

                    class PodcastHistoryWrapper:
                        """Wrapper to aggregate history from multiple Podcast entries."""

                        def __init__(self, podcasts, item, history_list):
                            self.item = item
                            self.id = podcasts[0].id if podcasts else 0
                            self._podcasts = podcasts
                            self._history_list = history_list

                        @property
                        def completed_play_count(self):
                            """Return count of completed plays (history records with end_date)."""
                            # Since we already filtered all_history to only include records with end_date,
                            # we can just count the length of the filtered history_list
                            return len(self._history_list)

                        @property
                        def history(self):
                            """Return a queryset-like object that aggregates all history."""
                            class HistoryProxy:
                                def __init__(self, history_list):
                                    self._history = history_list

                                def all(self):
                                    return self._history

                                def count(self):
                                    return len(self._history)

                                def filter(self, **kwargs):
                                    # Simple filtering for history_user
                                    if "history_user" in kwargs:
                                        user = kwargs["history_user"]
                                        filtered = [h for h in self._history if getattr(h, "history_user", None) == user or getattr(h, "history_user", None) is None]
                                        return HistoryProxy(filtered)
                                    return self

                                def order_by(self, order):
                                    # Re-sort based on order string (e.g., 'end_date' or '-end_date')
                                    if order == "end_date":
                                        sorted_list = sorted(
                                            self._history,
                                            key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                                        )
                                    elif order == "-end_date":
                                        sorted_list = sorted(
                                            self._history,
                                            key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                                            reverse=True,
                                        )
                                    else:
                                        sorted_list = self._history
                                    return HistoryProxy(sorted_list)

                            return HistoryProxy(self._history_list)

                    podcast_wrapper = PodcastHistoryWrapper(all_podcasts, enriched["item"], all_history)
                else:
                    # Create a dummy Podcast object with item for template compatibility when podcast is None
                    class DummyPodcast:
                        def __init__(self, item):
                            self.item = item
                            self.id = 0
                            self.history = type("History", (), {"count": lambda: 0, "all": list})()

                    podcast_wrapper = DummyPodcast(enriched["item"])

                # Create episode dict compatible with TV episode format
                # Include media_id, source, media_type for tracking modals
                episode_item = enriched["item"]
                episode_list.append({
                    "title": episode_obj.title,
                    "episode_number": episode_obj.episode_number or 0,
                    "image": show.image or settings.IMG_NONE,  # Use show image
                    "air_date": episode_obj.published,
                    "runtime": duration_str,
                    "overview": "",  # Podcast episodes don't have descriptions from API
                    "history": episode_history,
                    "media": episode_media,
                    "item": episode_item,
                    # Add fields needed for episode tracking modals
                    "media_id": episode_item.media_id,
                    "source": episode_item.source,
                    "media_type": episode_item.media_type,
                    # Add adapter objects for music-style modal
                    "track_adapter": PodcastEpisodeAdapter(episode_obj),
                    "album_adapter": PodcastShowAdapter(show),
                    "music_wrapper": podcast_wrapper,
                })

            # Build metadata dict for show
            media_metadata = {
                "title": show.title,
                "image": show.image or settings.IMG_NONE,
                "synopsis": show.description or "",  # Use description as synopsis
                "source": source,
                "media_type": media_type,
                "media_id": media_id,
                "genres": show.genres or [],
                "details": {
                    "author": show.author,
                    "language": show.language,
                },
                "episodes": episode_list,  # Use episodes key like TV seasons
            }

            # For pagination, calculate if there are more episodes
            total_episodes_count = episodes.count()
            has_more = total_episodes_count > initial_limit
            next_page = 2 if has_more else None

            context = {
                "user": request.user,
                "media": media_metadata,
                "media_type": media_type,
                "current_instance": tracker,  # Use tracker as current_instance for compatibility
                "user_medias": user_podcasts,  # Episodes user has listened to
                "podcast_show": show,
                "podcast_tracker": tracker,
                "episodes": episode_list,  # Use episode_list with adapter objects
                "paginated_episodes": episode_list,  # For fragment compatibility
                "total_episodes": total_episodes_count,
                "total_listened": total_listened,
                "total_minutes": total_minutes,
                "public_view": public_view,
                "IMG_NONE": settings.IMG_NONE,
                "TRACK_TIME": True,
                "has_more_episodes": has_more,  # Keep for backward compatibility
                "has_more": has_more,  # For fragment compatibility
                "next_page": next_page,
                "show_id": show.id,  # For API endpoint
                "unplayed_episodes_count": unplayed_count,  # Count of unplayed episodes
            }
            return render(request, "app/media_details.html", context)

    media_metadata = services.get_media_metadata(media_type, media_id, source)

    # For podcasts, ensure source is in metadata dict (fixes KeyError in template)
    if media_type == MediaTypes.PODCAST.value and isinstance(media_metadata, dict):
        media_metadata["source"] = source
        media_metadata["media_type"] = media_type
        media_metadata["media_id"] = media_id

    # For TV shows, apply fallback for seasons without posters (handles cached metadata)
    if media_type == MediaTypes.TV.value and isinstance(media_metadata, dict):
        tv_poster = media_metadata.get("image")
        if tv_poster:
            seasons = media_metadata.get("related", {}).get("seasons", [])
            for season in seasons:
                season_image = season.get("image")
                if not season_image or season_image == settings.IMG_NONE:
                    season["image"] = tv_poster

    # For public views, we don't need user media data
    if public_view:
        user_medias = []
        current_instance = None
    else:
        user_medias = list(
            BasicMedia.objects.filter_media_prefetch(
                request.user,
                media_id,
                media_type,
                source,
            )
        )
        if user_medias:
            def _activity_key(entry):
                dates = [d for d in (entry.end_date, entry.start_date) if d]
                primary_date = max(dates) if dates else entry.created_at
                return (primary_date, entry.start_date or entry.created_at, entry.created_at)

            user_medias.sort(key=_activity_key, reverse=True)
        current_instance = user_medias[0] if user_medias else None

    # Apply the same rating aggregation logic as in the media list
    if user_medias and len(user_medias) > 1:
        latest_rating = None
        latest_activity = None

        for user_media in user_medias:
            if user_media.score is not None:
                if user_media.end_date:
                    entry_activity = user_media.end_date
                elif user_media.progressed_at:
                    entry_activity = user_media.progressed_at
                else:
                    entry_activity = user_media.created_at

                if latest_activity is None or entry_activity > latest_activity:
                    latest_activity = entry_activity
                    latest_rating = user_media.score

        if latest_rating is not None:
            current_instance.score = latest_rating

    if (
        not public_view
        and current_instance
        and media_type == MediaTypes.GAME.value
        and isinstance(media_metadata, dict)
    ):
        metadata_genres = stats._coerce_genre_list(media_metadata.get("genres"))
        item = current_instance.item
        if item:
            if metadata_genres and metadata_genres != item.genres:
                item.genres = metadata_genres
                item.save(update_fields=["genres"])
            elif item.genres:
                media_metadata["genres"] = item.genres

    play_stats = None
    if (
        not public_view
        and current_instance
        and user_medias
        and media_type in [MediaTypes.GAME.value, MediaTypes.BOARDGAME.value]
    ):
        BasicMedia.objects._aggregate_item_data(current_instance, user_medias)
        aggregated_progress = getattr(current_instance, "aggregated_progress", None)
        if aggregated_progress is None:
            aggregated_progress = current_instance.progress or 0

        play_stats = {
            "first_played": getattr(current_instance, "aggregated_start_date", None)
            or current_instance.start_date,
            "last_played": getattr(current_instance, "aggregated_end_date", None)
            or current_instance.end_date,
        }

        if media_type == MediaTypes.GAME.value:
            total_minutes = int(aggregated_progress or 0)
            play_stats.update(
                {
                    "total_minutes": total_minutes,
                    "total_hours": total_minutes // 60,
                    "total_minutes_remainder": total_minutes % 60,
                }
            )
            days_played = set()
            total_minutes_for_avg = 0
            for entry in user_medias:
                entry_minutes = entry.progress or 0
                if entry_minutes <= 0:
                    continue
                total_minutes_for_avg += entry_minutes
                days_played.update(stats._get_entry_play_dates(entry))
            total_days = len(days_played)
            if total_days:
                avg_minutes = int(round(total_minutes_for_avg / total_days))
            else:
                avg_minutes = 0
            play_stats["avg_time_per_day"] = helpers.minutes_to_hhmm(avg_minutes)
        else:
            play_stats["total_plays"] = int(aggregated_progress or 0)

    # Enrich related items with user tracking data
    # For public views, use list owner's data if available
    if media_metadata.get("related"):
        for section_name, related_items in media_metadata["related"].items():
            if related_items:
                media_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                        user=list_owner,
                    )
                )

    # For music tracks, get linked artist and album for navigation
    music_artist = None
    music_album = None
    if media_type == MediaTypes.MUSIC.value and current_instance:
        music_artist = getattr(current_instance, "artist", None)
        music_album = getattr(current_instance, "album", None)

    notes_entry = None
    if not public_view and user_medias:
        if current_instance and current_instance.notes and current_instance.notes.strip():
            notes_entry = current_instance
        else:
            for entry in user_medias:
                if entry.notes and entry.notes.strip():
                    notes_entry = entry
                    break

    context = {
        "user": request.user,
        "media": media_metadata,
        "media_type": media_type,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "music_artist": music_artist,
        "music_album": music_album,
        "public_view": public_view,
        "play_stats": play_stats,
        "notes_entry": notes_entry,
    }
    return render(request, "app/media_details.html", context)


@login_not_required
@require_GET
def season_details(
    request, source, media_id, title, season_number,
):
    """Return the details page for a season."""
    # Check if this is a public view (from query parameter)
    public_view = request.GET.get("public_view") == "1" and not request.user.is_authenticated

    # For public views, find a public list containing this item to get the owner
    list_owner = None
    if public_view:
        try:
            # Get or create the Item for this season
            item, _ = Item.objects.get_or_create(
                media_id=media_id,
                source=source,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
                defaults={"title": "", "image": settings.IMG_NONE},
            )
            # Find a public list containing this item
            public_list = CustomList.objects.filter(
                visibility="public",
                items=item,
            ).select_related("owner").first()
            if public_list:
                list_owner = public_list.owner
        except Exception:
            # If we can't find a list owner, list_owner stays None
            pass

    tv_with_seasons_metadata = services.get_media_metadata(
        "tv_with_seasons",
        media_id,
        source,
        [season_number],
    )
    season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

    # For public views, we don't need user media data
    if public_view:
        user_medias = []
        current_instance = None
    else:
        user_medias = BasicMedia.objects.filter_media_prefetch(
            request.user,
            media_id,
            MediaTypes.SEASON.value,
            source,
            season_number=season_number,
        )
        current_instance = user_medias[0] if user_medias else None

    # Apply the same rating aggregation logic as in the media list
    if user_medias and len(user_medias) > 1:
        # Find the most recent rating among all entries
        latest_rating = None
        latest_activity = None

        for user_media in user_medias:
            if user_media.score is not None:
                # Determine the most recent activity for this entry
                entry_activity = None
                if user_media.end_date:
                    entry_activity = user_media.end_date
                elif user_media.progressed_at:
                    entry_activity = user_media.progressed_at
                else:
                    entry_activity = user_media.created_at

                # If this entry has more recent activity, use its rating
                if latest_activity is None or entry_activity > latest_activity:
                    latest_activity = entry_activity
                    latest_rating = user_media.score

        # Update the current_instance score to use the most recent rating
        if latest_rating is not None:
            current_instance.score = latest_rating

    episodes_in_db = current_instance.episodes.all() if current_instance else []

    if source == Sources.MANUAL.value:
        season_metadata["episodes"] = manual.process_episodes(
            season_metadata,
            episodes_in_db,
        )
    else:
        season_metadata["episodes"] = tmdb.process_episodes(
            season_metadata,
            episodes_in_db,
        )

    # Enrich related items with user tracking data
    # For public views, use list owner's data if available
    if season_metadata.get("related"):
        for section_name, related_items in season_metadata["related"].items():
            if related_items:
                season_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                        user=list_owner,
                    )
                )

    context = {
        "user": request.user,
        "media": season_metadata,
        "tv": tv_with_seasons_metadata,
        "media_type": MediaTypes.SEASON.value,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "public_view": public_view,
    }
    return render(request, "app/media_details.html", context)


@require_POST
def update_media_score(request, media_type, instance_id):
    """Update the user's score for a media item."""
    media = BasicMedia.objects.get_media(
        request.user,
        media_type,
        instance_id,
    )

    score = float(request.POST.get("score"))
    media.score = score
    media.save()
    logger.info(
        "%s score updated to %s",
        media,
        score,
    )

    return JsonResponse(
        {
            "success": True,
            "score": score,
        },
    )


@require_POST
def sync_metadata(request, source, media_type, media_id, season_number=None):
    """Refresh the metadata for a media item."""
    if source == Sources.MANUAL.value:
        msg = "Manual items cannot be synced."
        messages.error(request, msg)
        return HttpResponse(
            msg,
            status=400,
            headers={"HX-Redirect": request.POST.get("next", "/")},
        )

    cache_key = f"{source}_{media_type}_{media_id}"
    if media_type == MediaTypes.SEASON.value:
        cache_key += f"_{season_number}"

    ttl = cache.ttl(cache_key)
    logger.debug("%s - Cache TTL for: %s", cache_key, ttl)

    if ttl is not None and ttl > (settings.CACHE_TIMEOUT - 3):
        msg = "The data was recently synced, please wait a few seconds."
        messages.error(request, msg)
        logger.error(msg)
    else:
        deleted = cache.delete(cache_key)
        logger.debug("%s - Old cache deleted: %s", cache_key, deleted)

        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        item, _ = Item.objects.update_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
            },
        )
        title = metadata["title"]
        if season_number:
            title += f" - Season {season_number}"

        if media_type == MediaTypes.SEASON.value:
            metadata["episodes"] = tmdb.process_episodes(
                metadata,
                [],
            )

            # Create a dictionary of existing episodes keyed by episode number
            existing_episodes = {
                ep.episode_number: ep
                for ep in Item.objects.filter(
                    source=source,
                    media_type=MediaTypes.EPISODE.value,
                    media_id=media_id,
                    season_number=season_number,
                )
            }

            episodes_to_update = []
            episode_count = 0

            for episode_data in metadata["episodes"]:
                episode_number = episode_data["episode_number"]
                if episode_number in existing_episodes:
                    episode_item = existing_episodes[episode_number]
                    episode_item.title = metadata["title"]
                    episode_item.image = episode_data["image"]
                    episodes_to_update.append(episode_item)
                    episode_count += 1

            logger.info(
                "Found %s existing episodes to update for %s",
                episode_count,
                title,
            )

            if episodes_to_update:
                updated_count = Item.objects.bulk_update(
                    episodes_to_update,
                    ["title", "image"],
                    batch_size=100,
                )
                logger.info(
                    "Successfully updated %s episodes for %s",
                    updated_count,
                    title,
                )

        item.fetch_releases(delay=False)

        msg = f"{title} was synced to {Sources(source).label} successfully."
        messages.success(request, msg)

    if request.headers.get("HX-Request"):
        return HttpResponse(
            status=204,
            headers={
                "HX-Redirect": request.POST["next"],
            },
        )
    return helpers.redirect_back(request)


@never_cache
@require_GET
def track_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
):
    """Return the tracking form for a media item."""
    # Handle podcast shows (identified by podcast_uuid)
    if media_type == MediaTypes.PODCAST.value and source == Sources.POCKETCASTS.value:
        from app.forms import PodcastShowTrackerForm
        from app.models import PodcastEpisode, PodcastShow, PodcastShowTracker

        # Check if this is a show (podcast_uuid) or an episode (episode_uuid)
        show = PodcastShow.objects.filter(podcast_uuid=media_id).first()
        if show:
            # This is a show - use PodcastShowTracker form
            tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()
            return_url = request.GET.get("return_url", "")

            initial_data = {"show_id": show.id}
            form = PodcastShowTrackerForm(instance=tracker, initial=initial_data)

            return render(
                request,
                "app/components/podcast_show_track_modal.html",
                {
                    "show": show,
                    "tracker": tracker,
                    "form": form,
                    "return_url": return_url,
                },
            )

        # This is an episode (episode_uuid) - use music-style modal
        episode = PodcastEpisode.objects.filter(episode_uuid=media_id).first()
        if episode:
            from django.conf import settings

            from app.models import Item, Podcast

            show = episode.show
            instance_id = request.GET.get("instance_id")

            # Get all Podcast entries for this episode to aggregate history
            # Each Podcast entry has its own history, so we need to combine them
            all_podcasts = list(Podcast.objects.filter(
                user=request.user,
                show=show,
                episode=episode,
            ).order_by("-end_date"))

            # Get or create Item for this episode
            item, _ = Item.objects.get_or_create(
                media_id=episode.episode_uuid,
                source=source,
                media_type=media_type,
                defaults={
                    "title": episode.title,
                    "image": show.image or settings.IMG_NONE,
                    "runtime_minutes": (episode.duration // 60) if episode.duration else None,
                },
            )

            # Create adapter objects to match template expectations
            class PodcastEpisodeAdapter:
                """Adapter to make PodcastEpisode work like Track in template."""

                def __init__(self, episode):
                    self.title = episode.title
                    self.track_number = episode.episode_number
                    self.duration_formatted = self._format_duration(episode.duration) if episode.duration else None
                    self.musicbrainz_recording_id = None  # Not used for podcasts
                    self.id = episode.id
                    self.published = episode.published  # For "Published date" button
                    self.episode_uuid = episode.episode_uuid  # For form submission when music is None

                def _format_duration(self, seconds):
                    """Format duration in seconds to MM:SS or H:MM:SS."""
                    hours = seconds // 3600
                    minutes = (seconds % 3600) // 60
                    secs = seconds % 60
                    if hours > 0:
                        return f"{hours}:{minutes:02d}:{secs:02d}"
                    return f"{minutes}:{secs:02d}"

            class PodcastShowAdapter:
                """Adapter to make PodcastShow work like Album in template."""

                def __init__(self, show):
                    self.image = show.image or settings.IMG_NONE
                    self.release_date = None  # Podcasts don't have release dates
                    self.id = show.id

            # Create a wrapper object that aggregates history from all podcast entries
            # This allows the template to show all history records like music does
            if all_podcasts:
                from django.utils import timezone

                # Aggregate all history records from all podcast entries
                # Only include history records with end_date (completed plays)
                all_history = []
                for podcast in all_podcasts:
                    # Only include history records with end_date (completed plays)
                    history = podcast.history.filter(end_date__isnull=False) if hasattr(podcast.history, "filter") else [h for h in podcast.history.all() if h.end_date]
                    # Convert queryset to list if needed to ensure proper evaluation
                    if hasattr(history, "__iter__") and not isinstance(history, (list, tuple)):
                        history = list(history)
                    all_history.extend(history)

                # Sort by end_date descending (most recent first) for display
                # The template filter will re-sort if needed
                all_history.sort(
                    key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                    reverse=True,
                )

                class PodcastHistoryWrapper:
                    """Wrapper to aggregate history from multiple Podcast entries."""

                    def __init__(self, podcasts, item, history_list):
                        self.item = item
                        self.id = podcasts[0].id if podcasts else 0
                        self._podcasts = podcasts
                        self._history_list = history_list

                    @property
                    def completed_play_count(self):
                        """Return count of completed plays (history records with end_date)."""
                        # Since we already filtered all_history to only include records with end_date,
                        # we can just count the length of the filtered history_list
                        return len(self._history_list)

                    @property
                    def history(self):
                        """Return a queryset-like object that aggregates all history."""
                        class HistoryProxy:
                            def __init__(self, history_list):
                                self._history = history_list

                            def all(self):
                                return self._history

                            def count(self):
                                return len(self._history)

                            def filter(self, **kwargs):
                                # Simple filtering for history_user
                                if "history_user" in kwargs:
                                    user = kwargs["history_user"]
                                    filtered = [h for h in self._history if getattr(h, "history_user", None) == user or getattr(h, "history_user", None) is None]
                                    return HistoryProxy(filtered)
                                return self

                            def order_by(self, order):
                                # Re-sort based on order string (e.g., 'end_date' or '-end_date')
                                if order == "end_date":
                                    sorted_list = sorted(
                                        self._history,
                                        key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                                    )
                                elif order == "-end_date":
                                    sorted_list = sorted(
                                        self._history,
                                        key=lambda x: x.end_date if x.end_date else timezone.datetime.min.replace(tzinfo=UTC),
                                        reverse=True,
                                    )
                                else:
                                    sorted_list = self._history
                                return HistoryProxy(sorted_list)

                        return HistoryProxy(self._history_list)

                podcast = PodcastHistoryWrapper(all_podcasts, item, all_history)
            else:
                # Create a dummy Podcast object with item for template compatibility when podcast is None
                class DummyPodcast:
                    def __init__(self, item):
                        self.item = item
                        self.id = 0
                        self.history = type("History", (), {"count": lambda: 0, "all": list})()

                    @property
                    def completed_play_count(self):
                        """Return 0 for dummy podcast (no plays)."""
                        return 0

                podcast = DummyPodcast(item)

            return render(
                request,
                "app/components/fill_track_song.html",
                {
                    "user": request.user,
                    "album": PodcastShowAdapter(show),  # Use show as "album" for template compatibility
                    "track": PodcastEpisodeAdapter(episode),  # Use episode as "track" for template compatibility
                    "music": podcast,  # Use podcast as "music" for template compatibility
                    "request": request,
                    "csrf_token": request.META.get("CSRF_COOKIE", ""),
                    "TRACK_TIME": True,
                    "IMG_NONE": settings.IMG_NONE,
                },
            )

    instance_id = request.GET.get("instance_id")
    if instance_id:
        media = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    elif request.GET.get("is_create"):
        media = None
    else:
        # no specific instance, try to find the first one
        user_medias = BasicMedia.objects.filter_media(
            request.user,
            media_id,
            media_type,
            source,
            season_number=season_number,
        )
        media = user_medias.first()
        if media:
            instance_id = media.id

    initial_data = {
        "media_id": media_id,
        "source": source,
        "media_type": media_type,
        "season_number": season_number,
        "instance_id": instance_id,
    }

    if media:
        title = media.item
        if media_type == MediaTypes.GAME.value:
            initial_data["progress"] = helpers.minutes_to_hhmm(media.progress)
    else:
        title = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )["title"]
        if media_type == MediaTypes.SEASON.value:
            title += f" S{season_number}"

    form = get_form_class(media_type)(instance=media, initial=initial_data)

    return render(
        request,
        "app/components/fill_track.html",
        {
            "user": request.user,
            "title": title,
            "form": form,
            "media": media,
            "return_url": request.GET["return_url"],
        },
    )


@require_POST
def media_save(request):
    """Save or update media data to the database."""
    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    season_number = request.POST.get("season_number")
    instance_id = request.POST.get("instance_id")

    if instance_id:
        instance = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    else:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        # Extract runtime from metadata
        runtime_minutes = None
        if metadata.get("details", {}).get("runtime"):
            from app.statistics import parse_runtime_to_minutes
            runtime_minutes = parse_runtime_to_minutes(metadata["details"]["runtime"])

        metadata_genres = []
        if media_type == MediaTypes.GAME.value:
            metadata_genres = stats._coerce_genre_list(metadata.get("genres"))

        item, created = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
                "runtime_minutes": runtime_minutes,
                "genres": metadata_genres,
            },
        )

        # Update image and runtime if they're not set and we have them now
        needs_save = False
        if item.image == settings.IMG_NONE and metadata.get("image"):
            item.image = metadata["image"]
            needs_save = True
        if not item.runtime_minutes and runtime_minutes:
            item.runtime_minutes = runtime_minutes
            needs_save = True
        if metadata_genres and metadata_genres != item.genres:
            item.genres = metadata_genres
            needs_save = True
        if needs_save:
            item.save()
        model = apps.get_model(app_label="app", model_name=media_type)
        instance = model(item=item, user=request.user)

        # For music tracks, create/link Artist and Album
        if media_type == MediaTypes.MUSIC.value:
            artist_instance = None
            album_instance = None
            track_genres = metadata.get("genres", [])

            # Create or get Artist
            artist_id = metadata.get("_artist_id") or metadata.get("details", {}).get("artist_id")
            artist_name = metadata.get("_artist_name") or metadata.get("details", {}).get("artist")
            if artist_id and artist_name:
                artist_instance, _ = Artist.objects.get_or_create(
                    musicbrainz_id=artist_id,
                    defaults={"name": artist_name},
                )
            elif artist_name:
                # Try to find by name if no ID
                artist_instance = Artist.objects.filter(name=artist_name).first()
                if not artist_instance:
                    artist_instance = Artist.objects.create(name=artist_name)

            # Create or get Album
            album_id = metadata.get("_album_id") or metadata.get("details", {}).get("album_id")
            album_title = metadata.get("_album_title") or metadata.get("details", {}).get("album")
            image_url = metadata.get("image", "")
            release_date = None
            release_date_str = metadata.get("details", {}).get("release_date")
            if release_date_str:
                release_date = _parse_release_date_str(release_date_str)

            if album_id and album_title:
                album_instance, created = Album.objects.get_or_create(
                    musicbrainz_release_id=album_id,
                    defaults={
                        "title": album_title,
                        "artist": artist_instance,
                        "image": image_url,
                        "release_date": release_date,
                        "genres": track_genres,
                    },
                )
                # Update album image if it's missing
                if not created and image_url and image_url != settings.IMG_NONE:
                    if not album_instance.image or album_instance.image == settings.IMG_NONE:
                        album_instance.image = image_url
                        album_instance.save(update_fields=["image"])
                # Fill release_date/genres if missing
                if not album_instance.release_date and release_date:
                    album_instance.release_date = release_date
                    album_instance.save(update_fields=["release_date"])
                if not album_instance.genres and track_genres:
                    album_instance.genres = track_genres
                    album_instance.save(update_fields=["genres"])
            elif album_title:
                # Try to find by title and artist if no ID
                album_instance = Album.objects.filter(
                    title=album_title,
                    artist=artist_instance,
                ).first()
                if not album_instance:
                    album_instance = Album.objects.create(
                        title=album_title,
                        artist=artist_instance,
                        image=image_url,
                        release_date=release_date,
                        genres=track_genres,
                    )

            instance.artist = artist_instance
            instance.album = album_instance

            # Link to Track if it exists (for album-based additions)
            if album_instance:
                track_instance = Track.objects.filter(
                    album=album_instance,
                    musicbrainz_recording_id=media_id,
                ).first()
                if track_instance:
                    instance.track = track_instance

    # Validate the form and save the instance if it's valid
    form_class = get_form_class(media_type)
    form = form_class(request.POST, instance=instance)
    if form.is_valid():
        form.save()
        logger.info("%s saved successfully.", form.instance)
    else:
        logger.error(form.errors.as_json())
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(
                    request,
                    f"{field.replace('_', ' ').title()}: {error}",
                )

    return helpers.redirect_back(request)


@require_POST
def media_delete(request):
    """Delete media data from the database."""
    instance_id = request.POST["instance_id"]
    media_type = request.POST["media_type"]
    model = apps.get_model(app_label="app", model_name=media_type)

    try:
        media = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
        media.delete()
        logger.info("%s deleted successfully.", media)

    except model.DoesNotExist:
        logger.warning("The %s was already deleted before.", media_type)

    return helpers.redirect_back(request)


@require_POST
def episode_save(request):
    """Handle the creation, deletion, and updating of episodes for a season."""
    media_id = request.POST["media_id"]
    season_number = int(request.POST["season_number"])
    episode_number = int(request.POST["episode_number"])
    source = request.POST["source"]

    form = EpisodeForm(request.POST)
    if not form.is_valid():
        logger.error("Form validation failed: %s", form.errors)
        return HttpResponseBadRequest("Invalid form data")

    try:
        related_season = Season.objects.get(
            item__media_id=media_id,
            item__source=source,
            item__season_number=season_number,
            item__episode_number=None,
            user=request.user,
        )
    except Season.DoesNotExist:
        tv_with_seasons_metadata = services.get_media_metadata(
            "tv_with_seasons",
            media_id,
            source,
            [season_number],
        )
        season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

        # Use season poster if available, otherwise fallback to TV show poster
        season_image = season_metadata.get("image") or tv_with_seasons_metadata.get("image")

        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                "title": tv_with_seasons_metadata["title"],
                "image": season_image,
            },
        )
        related_season = Season.objects.create(
            item=item,
            user=request.user,
            score=None,
            status=Status.IN_PROGRESS.value,
            notes="",
        )

        logger.info("%s did not exist, it was created successfully.", related_season)

    related_season.watch(episode_number, form.cleaned_data["end_date"])

    return helpers.redirect_back(request)


@require_http_methods(["GET", "POST"])
def create_entry(request):
    """Return the form for manually adding media items."""
    if request.method == "GET":
        media_types = MediaTypes.values
        return render(request, "app/create_entry.html", {"media_types": media_types})

    # Process the form submission
    form = ManualItemForm(request.POST, user=request.user)
    if not form.is_valid():
        # Handle form validation errors
        logger.error(form.errors.as_json())
        helpers.form_error_messages(form, request)
        return redirect("create_entry")

    # Try to save the item
    try:
        item = form.save()
    except IntegrityError:
        # Handle duplicate item
        media_name = form.cleaned_data["title"]
        if form.cleaned_data.get("season_number"):
            media_name += f" - Season {form.cleaned_data['season_number']}"
        if form.cleaned_data.get("episode_number"):
            media_name += f" - Episode {form.cleaned_data['episode_number']}"

        logger.exception("%s already exists in the database.", media_name)
        messages.error(request, f"{media_name} already exists in the database.")
        return redirect("create_entry")

    # Prepare and validate the media form
    updated_request = request.POST.copy()
    updated_request.update({"source": item.source, "media_id": item.media_id})
    media_form = get_form_class(item.media_type)(updated_request)

    if not media_form.is_valid():
        # Handle media form validation errors
        logger.error(media_form.errors.as_json())
        helpers.form_error_messages(media_form, request)

        # Delete the item since the media creation failed
        item.delete()
        logger.info("%s was deleted due to media form validation failure", item)
        return redirect("create_entry")

    # Save the media instance
    media_form.instance.user = request.user
    media_form.instance.item = item

    # Handle relationships based on media type
    if item.media_type == MediaTypes.SEASON.value:
        media_form.instance.related_tv = form.cleaned_data["parent_tv"]
    elif item.media_type == MediaTypes.EPISODE.value:
        media_form.instance.related_season = form.cleaned_data["parent_season"]

    media_form.save()

    # Success message
    msg = f"{item} added successfully."
    messages.success(request, msg)
    logger.info(msg)

    return redirect("create_entry")


@require_GET
def search_parent_tv(request):
    """Return the search results for parent TV shows."""
    query = request.GET.get("q", "").strip()

    if len(query) <= 1:
        return render(request, "app/components/search_parent_tv.html")

    logger.debug(
        "%s - Searching for TV shows with query: %s",
        request.user.username,
        query,
    )

    parent_tvs = TV.objects.filter(
        user=request.user,
        item__source=Sources.MANUAL.value,
        item__media_type=MediaTypes.TV.value,
        item__title__icontains=query,
    )[:5]

    return render(
        request,
        "app/components/search_parent_tv.html",
        {"results": parent_tvs, "query": query},
    )


@require_GET
def search_parent_season(request):
    """Return the search results for parent seasons."""
    query = request.GET.get("q", "").strip()

    if len(query) <= 1:
        return render(request, "app/components/search_parent_tv.html")

    logger.debug(
        "%s - Searching for seasons with query: %s",
        request.user.username,
        query,
    )

    parent_seasons = Season.objects.filter(
        user=request.user,
        item__source=Sources.MANUAL.value,
        item__media_type=MediaTypes.SEASON.value,
        item__title__icontains=query,
    )[:5]

    return render(
        request,
        "app/components/search_parent_season.html",
        {"results": parent_seasons, "query": query},
    )


@require_GET
def history_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    episode_number=None,
):
    """Return the history page for a media item."""
    instance_id = request.GET.get("instance_id")
    if instance_id:
        try:
            media = BasicMedia.objects.get_media(
                request.user,
                media_type,
                instance_id,
            )
            user_medias = [media]
        except (ObjectDoesNotExist, ValueError, TypeError):
            user_medias = BasicMedia.objects.filter_media(
                request.user,
                media_id,
                media_type,
                source,
                season_number=season_number,
                episode_number=episode_number,
            )
    else:
        user_medias = BasicMedia.objects.filter_media(
            request.user,
            media_id,
            media_type,
            source,
            season_number=season_number,
            episode_number=episode_number,
        )

    try:
        total_medias = user_medias.count()
    except TypeError:
        total_medias = len(user_medias)
    timeline_entries = []
    for index, media in enumerate(user_medias, start=1):
        # Filter history to only include records with end_date (completed plays)
        # This prevents showing invalid history records from in-progress episodes
        history = (
            media.history.filter(end_date__isnull=False)
            if hasattr(media.history, "filter")
            else [h for h in media.history.all() if h.end_date]
        )
        if history:
            media_entry_number = total_medias - index + 1
            timeline_entries.extend(
                history_processor.process_history_entries(
                    history,
                    media_type,
                    media_entry_number,
                ),
            )
    return render(
        request,
        "app/components/fill_history.html",
        {
            "user": request.user,
            "media_type": media_type,
            "timeline": timeline_entries,
            "total_medias": total_medias,
            "return_url": request.GET["return_url"],
        },
    )


@require_http_methods(["DELETE"])
def delete_history_record(request, media_type, history_id):
    """Delete a specific history record."""
    try:
        historical_model = apps.get_model(
            app_label="app",
            model_name=f"historical{media_type.lower()}",
        )

        # Try to get the history record, checking both with and without history_user
        # This handles cases where history_user might be null (e.g., from old imports)
        try:
            history_record = historical_model.objects.get(
                history_id=history_id,
                history_user=request.user,
            )
        except historical_model.DoesNotExist:
            # If not found with history_user, check if history_user is null
            # and verify the record belongs to the user via the actual model instance
            history_record = historical_model.objects.get(
                history_id=history_id,
                history_user__isnull=True,
            )
            try:
                BasicMedia.objects.get_media(
                    request.user,
                    media_type.lower(),
                    history_record.id,
                )
            except ObjectDoesNotExist:
                raise historical_model.DoesNotExist(
                    f"History record {history_id} not found for user {request.user}",
                )

        # Get music_id or podcast_id from query params if provided (for updating count)
        music_id = request.GET.get("music_id")
        podcast_id = request.GET.get("podcast_id")

        history_record.delete()

        logger.info(
            "Deleted history record %s",
            str(history_id),
        )

        # Invalidate caches since history changed
        # This is needed because deleting a historical record doesn't trigger
        # the model's post_delete signal (we're deleting Historical*, not the actual model)
        media_type_lower = media_type.lower()
        logging_styles = ("sessions", "repeats")
        if media_type_lower in ("game", "boardgame"):
            start_dt = getattr(history_record, "start_date", None) or getattr(history_record, "end_date", None)
            end_dt = getattr(history_record, "end_date", None) or getattr(history_record, "start_date", None)
            history_day_keys = history_cache.history_day_keys_for_range(start_dt, end_dt)
        else:
            activity_dt = (
                getattr(history_record, "end_date", None)
                or getattr(history_record, "start_date", None)
                or getattr(history_record, "created_at", None)
            )
            history_day_key = history_cache.history_day_key(activity_dt)
            history_day_keys = [history_day_key] if history_day_key else []

        # For deletes, invalidate immediately (force) so the stale entry disappears,
        # then schedule refresh to rebuild. This shows the banner and reloads.
        history_cache.invalidate_history_days(
            request.user.id,
            day_keys=history_day_keys,
            logging_styles=logging_styles,
            force=True,
            reason="history_delete",
        )
        statistics_cache.invalidate_statistics_days(
            request.user.id,
            day_values=history_day_keys,
            reason="history_delete",
        )
        statistics_cache.schedule_all_ranges_refresh(request.user.id)

        # If music_id or podcast_id is provided, return updated count for out-of-band swap
        if music_id and media_type.lower() == "music":
            from app.models import Music
            from users.templatetags.user_tags import user_date_format

            try:
                music = Music.objects.get(id=music_id, user=request.user)
                # Get remaining history records (filtered by user or null)
                remaining_history = list(music.history.filter(
                    history_user=request.user,
                ).order_by("-end_date")) or list(music.history.filter(
                    history_user__isnull=True,
                ).order_by("-end_date"))

                remaining_count = len(remaining_history)

                if remaining_count > 0:
                    # Get the last entry for date display
                    last_entry = remaining_history[0]

                    # Format the date using the same filter as the template
                    last_date_formatted = user_date_format(last_entry.end_date, request.user) if last_entry.end_date else "No date provided"

                    if remaining_count == 1:
                        history_text = f"Last listened: {last_date_formatted}"
                    else:
                        history_text = f"Last listened: {last_date_formatted} • Listened {remaining_count} times"

                    # Return response with out-of-band swaps for both album page and modal
                    response = HttpResponse()
                    # Update the count on the album detail page
                    response.write(f'<p id="track-history-{music_id}" hx-swap-oob="true" class="text-xs text-gray-400 mt-2 px-4">{history_text}</p>')
                    # Update the count in the modal
                    modal_text = "Listened once" if remaining_count == 1 else f"Listened {remaining_count} times"
                    response.write(f'<p id="modal-listen-count-{music_id}" hx-swap-oob="true" class="text-sm text-gray-400 mt-1">{modal_text}</p>')
                    return response
                # No history left, hide the album page element and update modal
                response = HttpResponse()
                response.write(f'<p id="track-history-{music_id}" hx-swap-oob="true" class="text-xs text-gray-400 mt-2 px-4" style="display: none;"></p>')
                response.write(f'<p id="modal-listen-count-{music_id}" hx-swap-oob="true" class="text-sm text-gray-400 mt-1">Not listened yet</p>')
                return response
            except Music.DoesNotExist:
                pass

        # If podcast_id is provided, return updated count for out-of-band swap
        if podcast_id and media_type.lower() == "podcast":
            from app.models import Podcast
            from users.templatetags.user_tags import user_date_format

            try:
                podcast = Podcast.objects.get(id=podcast_id, user=request.user)
                # Get remaining history records (filtered by user or null)
                remaining_history = list(podcast.history.filter(
                    history_user=request.user,
                ).order_by("-end_date")) or list(podcast.history.filter(
                    history_user__isnull=True,
                ).order_by("-end_date"))

                remaining_count = len(remaining_history)

                if remaining_count > 0:
                    # Get the last entry for date display
                    last_entry = remaining_history[0]

                    # Format the date using the same filter as the template
                    last_date_formatted = user_date_format(last_entry.end_date, request.user) if last_entry.end_date else "No date provided"

                    if remaining_count == 1:
                        history_text = f"Last played: {last_date_formatted}"
                    else:
                        history_text = f"Last played: {last_date_formatted} • Played {remaining_count} times"

                    # Return response with out-of-band swaps for both show page and modal
                    response = HttpResponse()
                    # Update the count in the modal
                    modal_text = "Played once" if remaining_count == 1 else f"Played {remaining_count} times"
                    response.write(f'<p id="modal-listen-count-{podcast_id}" hx-swap-oob="true" class="text-sm text-gray-400 mt-1">{modal_text}</p>')
                    response["HX-Trigger"] = "history-refresh-start"
                    return response
                # No history left, update modal
                response = HttpResponse()
                response.write(f'<p id="modal-listen-count-{podcast_id}" hx-swap-oob="true" class="text-sm text-gray-400 mt-1">Not played yet</p>')
                response["HX-Trigger"] = "history-refresh-start"
                return response
            except Podcast.DoesNotExist:
                pass

        # Return empty 200 response - the element will be removed by HTMX
        response = HttpResponse()
        response["HX-Trigger"] = "history-refresh-start"
        return response

    except historical_model.DoesNotExist:
        logger.exception(
            "History record %s not found for user %s",
            str(history_id),
            str(request.user),
        )
        return HttpResponse("Record not found", status=404)


@require_GET
def history(request):
    """Show a day-by-day history of episode and movie plays."""
    try:
        view_start = time.perf_counter()
        # Extract filter parameters from query string
        filters = {}
        int_params = ['album', 'artist', 'tv', 'season', 'season_number', 'podcast_show']
        str_params = ['genre', 'media_type', 'media_id', 'source']
        for param in int_params:
            value = request.GET.get(param)
            if value:
                try:
                    filters[param] = int(value)
                except (TypeError, ValueError):
                    pass  # Skip invalid filter values
        for param in str_params:
            value = request.GET.get(param)
            if value:
                filters[param] = value

        logging_style = request.GET.get("logging_style")
        if logging_style not in ("sessions", "repeats"):
            logging_style = None
        
        # Extract date range filters
        date_filters = {}
        start_date_str = request.GET.get("start-date")
        end_date_str = request.GET.get("end-date")
        if start_date_str:
            date_filters["start_date"] = start_date_str
        if end_date_str:
            date_filters["end_date"] = end_date_str

        logger.info(
            "history_view_start user_id=%s page=%s filters=%s date_filters=%s logging_style=%s",
            request.user.id,
            request.GET.get("page", 1),
            filters,
            date_filters,
            logging_style,
        )

        try:
            page_number = int(request.GET.get("page", 1))
        except (TypeError, ValueError):
            page_number = 1

        use_cache = not filters and not date_filters
        history_refreshing = False
        if use_cache:
            history_days, total_days, cache_meta = history_cache.get_cached_history_page(
                request.user,
                page_number=page_number,
                logging_style_override=logging_style,
            )
            history_refreshing = cache_meta.get("refreshing", False)
            if total_days == 0:
                paginator = Paginator([], history_cache.HISTORY_DAYS_PER_PAGE)
                page_obj = None
                history_days = []
                current_page = 1
            else:
                paginator = Paginator(range(total_days), history_cache.HISTORY_DAYS_PER_PAGE)
                try:
                    page_obj = paginator.page(page_number)
                except EmptyPage:
                    page_obj = paginator.page(paginator.num_pages)
                    current_page = page_obj.number
                    if current_page != page_number:
                        history_days, _, cache_meta = history_cache.get_cached_history_page(
                            request.user,
                            page_number=current_page,
                            logging_style_override=logging_style,
                        )
                        history_refreshing = cache_meta.get("refreshing", False)
                else:
                    current_page = page_obj.number
        else:
            history_days_all = history_cache.get_history_days(
                request.user,
                filters=filters,
                date_filters=date_filters,
                logging_style_override=logging_style,
            )

            paginator = Paginator(history_days_all, history_cache.HISTORY_DAYS_PER_PAGE)

            if paginator.count == 0:
                page_obj = None
                history_days = []
                current_page = 1
            else:
                try:
                    page_obj = paginator.page(page_number)
                except EmptyPage:
                    page_obj = paginator.page(paginator.num_pages)

                history_days = page_obj.object_list
                current_page = page_obj.number

        # Combine all filters for pagination (including date filters as query params)
        active_filters = filters.copy()
        if date_filters.get('start_date'):
            active_filters['start-date'] = date_filters['start_date']
        if date_filters.get('end_date'):
            active_filters['end-date'] = date_filters['end_date']
        if logging_style:
            active_filters['logging_style'] = logging_style
        
        context = {
            "user": request.user,
            "history_days": history_days,
            "page_obj": page_obj,
            "current_page": current_page,
            "total_pages": paginator.num_pages,
            "total_days": paginator.count,
            "days_per_page": paginator.per_page,
            "active_filters": active_filters,  # Pass filters to template for pagination
            "history_refreshing": history_refreshing,
        }
        day_entry_counts = []
        total_entries = 0
        for day in history_days:
            entries = day.get("entries", []) if isinstance(day, dict) else getattr(day, "entries", [])
            count = len(entries)
            total_entries += count
            day_entry_counts.append((day.get("date_display") or day.get("date"), count))
        top_days = sorted(day_entry_counts, key=lambda item: item[1], reverse=True)[:3]
        logger.info(
            "history_page_entry_counts user_id=%s page=%s total_entries=%s top_days=%s",
            request.user.id,
            current_page,
            total_entries,
            top_days,
        )
        render_start = time.perf_counter()
        logger.info(
            "history_render_start user_id=%s page=%s",
            request.user.id,
            current_page,
        )
        response = render(request, "app/history.html", context)
        render_ms = (time.perf_counter() - render_start) * 1000
        response_bytes = len(response.content)
        logger.info(
            "history_render_end user_id=%s page=%s render_ms=%.2f response_bytes=%s",
            request.user.id,
            current_page,
            render_ms,
            response_bytes,
        )
        logger.info(
            "history_view_end user_id=%s page=%s total_days=%s page_days=%s total_pages=%s elapsed_ms=%.2f response_bytes=%s",
            request.user.id,
            current_page,
            paginator.count,
            len(history_days),
            paginator.num_pages,
            (time.perf_counter() - view_start) * 1000,
            response_bytes,
        )
        return response
    except OperationalError as error:
        logger.error("Database error in history view: %s", error, exc_info=True)
        # Return empty state on database error
        context = {
            "user": request.user,
            "history_days": [],
            "page_obj": None,
            "current_page": 1,
            "total_pages": 0,
            "total_days": 0,
            "days_per_page": history_cache.HISTORY_DAYS_PER_PAGE,
            "active_filters": {},
            "database_error": True,
            "history_refreshing": False,
        }
        return render(request, "app/history.html", context)


@require_GET
def create_artist_from_search(request, musicbrainz_artist_id):
    """Create an Artist from MusicBrainz search and redirect to artist page."""
    from app.providers import musicbrainz
    from app.services.music import sync_artist_discography

    # Check if artist already exists
    artist = Artist.objects.filter(musicbrainz_id=musicbrainz_artist_id).first()

    if not artist:
        # Fetch artist data from MusicBrainz
        artist_data = musicbrainz.get_artist(musicbrainz_artist_id)

        artist = Artist.objects.create(
            name=artist_data.get("name", "Unknown Artist"),
            sort_name=artist_data.get("sort_name", ""),
            musicbrainz_id=musicbrainz_artist_id,
            country=artist_data.get("country", "") or "",
            genres=[g.get("name") for g in artist_data.get("genres", []) if g.get("name")] if artist_data.get("genres") else [],
        )
        logger.info("Created artist %s from MusicBrainz", artist.name)

        # Sync discography immediately after creating artist
        sync_artist_discography(artist)

    return redirect("artist_detail", artist_id=artist.id)


@require_GET
def create_album_from_search(request, musicbrainz_release_id):
    """Create an Album from MusicBrainz search and redirect to album page."""
    from app.providers import musicbrainz

    # Check if album already exists
    album = Album.objects.filter(musicbrainz_release_id=musicbrainz_release_id).first()

    # Fetch release data from MusicBrainz (for both new and existing albums)
    release_data = musicbrainz.get_release(musicbrainz_release_id)

    if not album:
        # Create or get the artist
        artist = None
        artist_id = release_data.get("artist_id")
        artist_name = release_data.get("artist_name")

        if artist_id:
            artist = Artist.objects.filter(musicbrainz_id=artist_id).first()
            if not artist and artist_name:
                artist = Artist.objects.create(
                    name=artist_name,
                    musicbrainz_id=artist_id,
                    country=release_data.get("country", "") or "",
                )
        elif artist_name:
            artist = Artist.objects.filter(name=artist_name).first()
            if not artist:
                artist = Artist.objects.create(name=artist_name)

        # Parse release date
        release_date = None
        date_str = release_data.get("release_date", "")
        if date_str:
            try:
                from datetime import datetime
                if len(date_str) == 4:  # Year only
                    release_date = datetime.strptime(date_str, "%Y").date()
                elif len(date_str) == 7:  # Year-month
                    release_date = datetime.strptime(date_str, "%Y-%m").date()
                elif len(date_str) >= 10:  # Full date
                    release_date = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            except ValueError:
                pass

        album = Album.objects.create(
            title=release_data.get("title", "Unknown Album"),
            musicbrainz_release_id=musicbrainz_release_id,
            artist=artist,
            release_date=release_date,
            image=release_data.get("image", ""),
            genres=release_data.get("genres", []),
        )
        logger.info("Created album %s from MusicBrainz", album.title)
    else:
        # Update album image if it's missing or placeholder
        new_image = release_data.get("image", "")
        if new_image and new_image != settings.IMG_NONE and (not album.image or album.image == settings.IMG_NONE):
            album.image = new_image
            album.save(update_fields=["image"])
            logger.info("Updated album %s image", album.title)
        # Update genres if we have fresh metadata and none stored yet
        if not album.genres and release_data.get("genres"):
            album.genres = release_data.get("genres", [])
            album.save(update_fields=["genres"])

    return redirect("album_detail", album_id=album.id)


@require_GET
def artist_detail(request, artist_id):
    """Return the detail page for a music artist."""
    from django.db.models import Max, Min
    from django.shortcuts import get_object_or_404

    from app.models import ArtistTracker
    from app.providers import musicbrainz
    from app.services.music import (
        needs_discography_sync,
        sync_artist_discography,
    )
    from app.services.music_scrobble import dedupe_artist_albums

    artist = get_object_or_404(Artist, id=artist_id)

    # If we don't have an MBID yet, attempt a quick lookup so discography can sync
    if not artist.musicbrainz_id:
        try:
            mbid, cand_count, variant = sync_services.resolve_artist_mbid(
                artist.name or "",
                artist.sort_name or "",
            )
            if mbid:
                try:
                    artist.musicbrainz_id = mbid
                    artist.discography_synced_at = None  # force a fresh sync
                    artist.save(update_fields=["musicbrainz_id", "discography_synced_at"])
                    logger.info(
                        "Attached MBID %s to artist %s on view via '%s' (candidates=%d)",
                        mbid,
                        artist.name,
                        variant,
                        cand_count,
                    )
                except IntegrityError:
                    # Merge this artist into the existing MBID owner to avoid dupes
                    from app.services.music import merge_artist_records

                    existing = Artist.objects.filter(musicbrainz_id=mbid).first()
                    if existing:
                        existing = merge_artist_records(artist, existing)
                        artist = existing
                        logger.info(
                            "Merged artist %s into existing MBID %s via '%s'",
                            artist.name,
                            mbid,
                            variant,
                        )
                    else:
                        logger.debug(
                            "Artist MBID attach conflicted for %s with %s via '%s' but no target found",
                            artist.name,
                            mbid,
                            variant,
                        )
            else:
                logger.debug(
                    "No MBID attached on view for %s after searching variants (candidates=%d)",
                    artist.name,
                    cand_count,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Artist MBID attach failed on view for %s: %s", artist.name, exc)

    # Heal duplicate albums for this artist (caused by noisy metadata)
    dedupe_artist_albums(artist)

    # Decide whether we should force a fresh discography sync (e.g., after fast import)
    albums_qs = Album.objects.filter(artist=artist)
    existing_album_count = albums_qs.count()
    missing_mbids = albums_qs.filter(
        musicbrainz_release_id__isnull=True,
        musicbrainz_release_group_id__isnull=True,
    ).exists()
    # Refresh more aggressively on view: daily, if no albums yet, or if placeholders lack IDs
    should_sync = (
        needs_discography_sync(artist, max_age_days=1)
        or existing_album_count == 0
        or missing_mbids
    )
    force_sync = existing_album_count == 0 or missing_mbids

    # Sync discography from MusicBrainz if needed (like TV populates seasons from TMDB)
    synced_count = 0
    if should_sync and artist.musicbrainz_id:
        synced_count = sync_artist_discography(artist, force=force_sync)
        if synced_count:
            dedupe_artist_albums(artist)
    elif should_sync and not artist.musicbrainz_id:
        logger.debug("Skipping discography sync for %s due to missing MBID", artist.name)

    # Get ALL albums for this artist (metadata-driven, like Seasons)
    # Note: Album covers are prefetched asynchronously via HTMX after page load
    all_albums = list(Album.objects.filter(artist=artist).order_by("-release_date", "title"))

    # Get user's music entries for this artist to calculate play counts
    user_music_entries = Music.objects.filter(
        user=request.user,
        album__artist=artist,
    ).select_related("album")

    # Calculate play counts per album (history entries = listens)
    album_play_counts = {}
    total_plays = 0
    for music in user_music_entries:
        if music.album_id:
            play_count = music.history.count()
            album_play_counts[music.album_id] = album_play_counts.get(music.album_id, 0) + play_count
            total_plays += play_count

    # Attach play_count to each album
    for album in all_albums:
        album.play_count = album_play_counts.get(album.id, 0)

    # Artist image is set from Wikipedia when fetching metadata below

    # Get user's tracker for this artist
    artist_tracker = ArtistTracker.objects.filter(
        user=request.user,
        artist=artist,
    ).first()

    # Get user's music entries for this artist
    user_music_for_artist = Music.objects.filter(
        user=request.user,
        album__artist=artist,
    )

    # History summary - just get date ranges, not historical record counts
    history_stats = user_music_for_artist.aggregate(
        first_listened=Min("start_date"),
        last_listened=Max("end_date"),
    )

    # Fetch detailed artist metadata from MusicBrainz
    artist_metadata = {}
    genres = []
    tags = []
    mb_rating = None
    mb_rating_count = 0
    bio = ""

    if artist.musicbrainz_id:
        try:
            mb_data = musicbrainz.get_artist(artist.musicbrainz_id)
            artist_metadata = {
                "type": mb_data.get("type", ""),
                "country": mb_data.get("country", ""),
                "area": mb_data.get("area", ""),
                "begin_date": mb_data.get("begin_date", ""),
                "end_date": mb_data.get("end_date", ""),
                "ended": mb_data.get("ended", False),
                "disambiguation": mb_data.get("disambiguation", ""),
            }
            genres = mb_data.get("genres", [])
            tags = mb_data.get("tags", [])
            mb_rating = mb_data.get("rating")
            mb_rating_count = mb_data.get("rating_count", 0)
            bio = mb_data.get("bio", "")  # Wikipedia extract

            # Persist country/genres locally for statistics rollups
            updated_fields = []
            if mb_data.get("country") and mb_data.get("country") != artist.country:
                artist.country = mb_data.get("country", "")
                updated_fields.append("country")
            if mb_data.get("genres"):
                genre_names = [g.get("name") for g in mb_data.get("genres") if g.get("name")]
                if genre_names != artist.genres:
                    artist.genres = genre_names
                    updated_fields.append("genres")
            if updated_fields:
                artist.save(update_fields=updated_fields)

            # Save Wikipedia image to Artist if not already set
            wiki_image = mb_data.get("image")
            if wiki_image and (not artist.image or artist.image == settings.IMG_NONE):
                artist.image = wiki_image
                artist.save(update_fields=["image"])
        except Exception as e:
            logger.debug("Failed to fetch artist metadata from MusicBrainz: %s", e)

    # Build genre chips (prefer genres, fall back to tags)
    genre_chips = []
    if genres:
        genre_chips = [g["name"].title() for g in genres[:6]]
    elif tags:
        # Use top tags as genre chips if no official genres
        genre_chips = [t["name"].title() for t in tags[:6]]

    context = {
        "user": request.user,
        "artist": artist,
        "albums": all_albums,  # All albums from discography, not just "in library"
        "total_plays": total_plays,
        "total_albums": len(all_albums),
        "artist_tracker": artist_tracker,
        "history_stats": history_stats,
        "artist_metadata": artist_metadata,
        "genre_chips": genre_chips,
        "bio": bio,  # Wikipedia extract
        "mb_rating": mb_rating,
        "mb_rating_count": mb_rating_count,
    }
    return render(request, "app/music_artist_detail.html", context)


@require_GET
def prefetch_artist_covers(request, artist_id):
    """HTMX endpoint to asynchronously fetch album covers for an artist.
    
    This runs after the artist page loads to avoid blocking the initial render.
    Returns the updated album grid HTML.
    """
    from django.shortcuts import get_object_or_404

    from app.models import Album, Artist, Music
    from app.services.music import prefetch_album_covers

    artist = get_object_or_404(Artist, id=artist_id)

    # Prefetch covers for albums missing art
    prefetch_album_covers(artist, limit=20)

    # Get updated albums
    all_albums = list(Album.objects.filter(artist=artist).order_by("-release_date", "title"))

    # Calculate play counts
    user_music_entries = Music.objects.filter(
        user=request.user,
        album__artist=artist,
    ).select_related("album")

    album_play_counts = {}
    for music in user_music_entries:
        if music.album_id:
            play_count = music.history.count()
            album_play_counts[music.album_id] = album_play_counts.get(music.album_id, 0) + play_count

    for album in all_albums:
        album.play_count = album_play_counts.get(album.id, 0)

    return render(request, "app/components/album_grid.html", {
        "all_albums": all_albums,
        "artist": artist,
    })


@require_GET
def album_detail(request, album_id):
    """Return the detail page for a music album."""
    from django.db.models import Max, Min
    from django.shortcuts import get_object_or_404

    from app.models import Track
    from app.providers import musicbrainz
    from app.services.music import (
        album_has_musicbrainz_id,
        ensure_album_has_release_id,
        sync_artist_discography,
    )
    from app.services.music_scrobble import (
        _choose_primary_album,
        _normalize,
        dedupe_artist_albums,
        is_incomplete_album,
    )

    album = get_object_or_404(Album.objects.select_related("artist"), id=album_id)
    original_artist = album.artist
    original_title = album.title
    original_norm = _normalize(original_title)

    # Heal duplicate albums for this artist so detail pages are consistent
    dedupe_artist_albums(original_artist)
    try:
        album.refresh_from_db()
    except Album.DoesNotExist:
        # Find the canonical album by normalized title
        replacement = (
            Album.objects.filter(artist=original_artist, title__iexact=original_title)
            .order_by("id")
            .first()
        )
        if not replacement:
            for cand in Album.objects.filter(artist=original_artist):
                if _normalize(cand.title) == original_norm:
                    replacement = cand
                    break
        if replacement:
            return redirect("album_detail", album_id=replacement.id)
        raise

    # If we only have a sparse placeholder, try to repopulate via discography and re-dedupe
    if is_incomplete_album(album) and original_artist.musicbrainz_id:
        try:
            sync_artist_discography(original_artist, force=True)
            dedupe_artist_albums(original_artist)
            candidates = [
                a
                for a in Album.objects.filter(artist=original_artist)
                if _normalize(a.title) == original_norm
            ]
            if candidates:
                best = _choose_primary_album(candidates, album)
                if best.id != album.id:
                    return redirect("album_detail", album_id=best.id)
                album = best
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to heal album %s via discography: %s", album_id, exc)

    # Ensure album has a release_id (fetch from release_group if needed)
    # This fixes albums that came from discography sync with only release_group_id
    if not album.musicbrainz_release_id and album.musicbrainz_release_group_id:
        ensure_album_has_release_id(album)

    # Populate tracks from MusicBrainz if not done yet (like Season populates episodes)
    # Now we accept albums that have EITHER release_id OR release_group_id
    has_mb_identity = album_has_musicbrainz_id(album)
    if not album.tracks_populated and has_mb_identity:
        try:
            # If we still don't have release_id after ensure_album_has_release_id,
            # we can't fetch tracks (need a specific release for track listing)
            if album.musicbrainz_release_id:
                release_data = musicbrainz.get_release(album.musicbrainz_release_id)
                tracks_data = release_data.get("tracks", [])

                # Update genres from release if available
                if release_data.get("genres") and not album.genres:
                    album.genres = release_data.get("genres")
                    album.save(update_fields=["genres"])

                for track_data in tracks_data:
                    Track.objects.update_or_create(
                        album=album,
                        disc_number=track_data.get("disc_number", 1),
                        track_number=track_data.get("track_number"),
                        defaults={
                            "title": track_data.get("title", "Unknown Track"),
                            "musicbrainz_recording_id": track_data.get("recording_id"),
                            "duration_ms": track_data.get("duration_ms"),
                            "genres": track_data.get("genres", []) or release_data.get("genres", []),
                        },
                    )

                # Also update album image if missing
                if not album.image or album.image == settings.IMG_NONE:
                    new_image = release_data.get("image", "")
                    if new_image and new_image != settings.IMG_NONE:
                        album.image = new_image

                album.tracks_populated = True
                album.save(update_fields=["tracks_populated", "image"])
                logger.info("Populated %d tracks for album %s", len(tracks_data), album.title)
            else:
                logger.warning("Album %s has release_group but no release found for tracks", album.title)
        except Exception as e:
            logger.warning("Failed to populate tracks for album %s: %s", album.title, e)

    # Get ALL tracks for this album (metadata-driven, like Episodes)
    all_tracks = Track.objects.filter(album=album).order_by("disc_number", "track_number", "title")

    # Get user's Music entries for these tracks
    user_music_by_track = {}
    user_music_entries = Music.objects.filter(
        user=request.user,
        album=album,
    ).select_related("item", "track")

    for music in user_music_entries:
        if music.track_id:
            user_music_by_track[music.track_id] = music
        # Also index by recording ID for legacy data
        if music.item and music.item.media_id:
            user_music_by_track[f"recording_{music.item.media_id}"] = music

    # Build track data with user's tracking info (like Episodes)
    tracks_with_data = []
    total_duration_ms = 0
    for track in all_tracks:
        # Look up user's Music entry for this track
        music_entry = user_music_by_track.get(track.id)
        if not music_entry and track.musicbrainz_recording_id:
            music_entry = user_music_by_track.get(f"recording_{track.musicbrainz_recording_id}")

        track_data = {
            "track": track,
            "music": music_entry,
            "history": list(music_entry.history.all().order_by("-end_date")) if music_entry else [],
        }
        tracks_with_data.append(track_data)
        if track.duration_ms:
            total_duration_ms += track.duration_ms

    # Count tracks in library
    library_track_count = sum(1 for t in tracks_with_data if t["music"])

    # History summary for this album (like Season history)
    history_stats = user_music_entries.aggregate(
        first_listened=Min("start_date"),
        last_listened=Max("end_date"),
    )

    # Get the current/primary Music instance for this album (most recently updated)
    current_instance = user_music_entries.order_by("-end_date", "-start_date").first()

    # Get user's tracker for this album
    from app.models import AlbumTracker
    album_tracker = AlbumTracker.objects.filter(
        user=request.user, album=album,
    ).first()

    # Calculate total runtime
    total_runtime = None
    if total_duration_ms:
        total_minutes = total_duration_ms // 60000
        if total_minutes >= 60:
            hours = total_minutes // 60
            minutes = total_minutes % 60
            total_runtime = f"{hours}h {minutes}m"
        else:
            total_runtime = f"{total_minutes}m"

    # Album details (like Season details)
    album_details = {
        "format": album.release_type or "Album",
        "release_date": album.release_date,
        "tracks": len(tracks_with_data),
        "runtime": total_runtime,
    }
    # Show either release_id or release_group_id for the source link
    if album.musicbrainz_release_id:
        album_details["musicbrainz_id"] = album.musicbrainz_release_id
        album_details["musicbrainz_url"] = f"https://musicbrainz.org/release/{album.musicbrainz_release_id}"
    elif album.musicbrainz_release_group_id:
        album_details["musicbrainz_id"] = album.musicbrainz_release_group_id
        album_details["musicbrainz_url"] = f"https://musicbrainz.org/release-group/{album.musicbrainz_release_group_id}"

    context = {
        "user": request.user,
        "album": album,
        "tracks": tracks_with_data,  # All tracks from metadata
        "library_track_count": library_track_count,
        "total_tracks": len(tracks_with_data),
        "history_stats": history_stats,
        "current_instance": current_instance,
        "album_details": album_details,
        "total_runtime": total_runtime,
        "has_mb_identity": has_mb_identity,  # For template to show correct message
        "album_tracker": album_tracker,  # User's tracking for this album
    }
    return render(request, "app/music_album_detail.html", context)


@require_POST
def sync_artist_discography_view(request, artist_id):
    """Manually trigger discography sync for an artist."""
    from django.shortcuts import get_object_or_404

    from app.services.music import sync_artist_discography
    from app.services.music_scrobble import dedupe_artist_albums

    artist = get_object_or_404(Artist, id=artist_id)

    # Force sync
    count = sync_artist_discography(artist, force=True)
    if count:
        dedupe_artist_albums(artist)

    messages.success(request, f"Synced {count} albums for {artist.name}")

    # Return HX-Refresh header to reload the page
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


def artist_track_modal(request, artist_id):
    """Return the tracking form modal for an artist - mirrors TV's track_modal."""
    from django.shortcuts import get_object_or_404

    from app.forms import ArtistTrackerForm
    from app.models import ArtistTracker

    artist = get_object_or_404(Artist, id=artist_id)
    return_url = request.GET.get("return_url", "")

    # Get existing tracker if any
    tracker = ArtistTracker.objects.filter(user=request.user, artist=artist).first()

    initial_data = {"artist_id": artist.id}
    form = ArtistTrackerForm(instance=tracker, initial=initial_data)

    return render(
        request,
        "app/components/artist_track_modal.html",
        {
            "artist": artist,
            "tracker": tracker,
            "form": form,
            "return_url": return_url,
        },
    )


@require_POST
def artist_save(request):
    """Save an artist tracker - mirrors media_save for TV."""
    from django.shortcuts import get_object_or_404

    from app.forms import ArtistTrackerForm
    from app.models import ArtistTracker

    artist_id = request.POST.get("artist_id")
    artist = get_object_or_404(Artist, id=artist_id)

    # Get existing tracker or None
    tracker = ArtistTracker.objects.filter(user=request.user, artist=artist).first()

    form = ArtistTrackerForm(request.POST, instance=tracker)
    if form.is_valid():
        tracker = form.save(commit=False)
        tracker.user = request.user
        tracker.artist = artist
        tracker.save()
        messages.success(request, f"Saved {artist.name}")
    else:
        messages.error(request, f"Error saving {artist.name}: {form.errors}")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("artist_detail", artist_id=artist.id)


@require_POST
def artist_delete(request):
    """Delete an artist tracker - mirrors media_delete for TV."""
    from django.shortcuts import get_object_or_404

    from app.models import ArtistTracker

    artist_id = request.POST.get("artist_id")
    artist = get_object_or_404(Artist, id=artist_id)

    tracker = ArtistTracker.objects.filter(user=request.user, artist=artist).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {artist.name} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("artist_detail", artist_id=artist.id)


@require_GET
def podcast_show_detail(request, show_id):
    """Return the detail page for a podcast show."""
    from django.shortcuts import get_object_or_404

    from app.models import Podcast, PodcastEpisode, PodcastShow, PodcastShowTracker

    show = get_object_or_404(PodcastShow, id=show_id)

    # Get user's tracker for this show
    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()

    # Get all episodes for this show
    # Get all episodes for this show, ordered by published date (newest first)
    # Use Coalesce to handle None published dates (put them at the end)
    from datetime import datetime

    from django.db.models import DateTimeField, Value
    from django.db.models.functions import Coalesce

    episodes = PodcastEpisode.objects.filter(show=show).annotate(
        published_or_old=Coalesce(
            "published",
            Value(datetime(1970, 1, 1, tzinfo=UTC),
                  output_field=DateTimeField()),
        ),
    ).order_by("-published_or_old", "-episode_number")

    # Get user's podcast entries for this show
    user_podcasts = list(Podcast.objects.filter(
        user=request.user,
        show=show,
    ).select_related("episode", "item"))

    # Calculate stats
    total_episodes = episodes.count()
    total_listened = len(user_podcasts)
    total_minutes = sum(podcast.progress or 0 for podcast in user_podcasts)

    context = {
        "user": request.user,
        "show": show,
        "episodes": episodes,
        "user_podcasts": user_podcasts,
        "tracker": tracker,
        "total_episodes": total_episodes,
        "total_listened": total_listened,
        "total_minutes": total_minutes,
    }
    return render(request, "app/podcast_show_detail.html", context)


@require_GET
def podcast_show_track_modal(request, show_id):
    """Return the tracking form modal for a podcast show - mirrors artist_track_modal."""
    from django.shortcuts import get_object_or_404

    from app.forms import PodcastShowTrackerForm
    from app.models import PodcastShow, PodcastShowTracker

    show = get_object_or_404(PodcastShow, id=show_id)
    return_url = request.GET.get("return_url", "")

    # Get existing tracker if any
    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()

    initial_data = {"show_id": show.id}
    form = PodcastShowTrackerForm(instance=tracker, initial=initial_data)

    return render(
        request,
        "app/components/podcast_show_track_modal.html",
        {
            "show": show,
            "tracker": tracker,
            "form": form,
            "return_url": return_url,
        },
    )


@require_GET
def podcast_episodes_api(request, show_id):
    """API endpoint for paginated podcast episodes.
    
    Returns HTML fragments for infinite scroll if format=html, otherwise JSON.
    """
    from django.conf import settings
    from django.shortcuts import get_object_or_404

    from app.models import (
        Item,
        MediaTypes,
        Podcast,
        PodcastEpisode,
        PodcastShow,
        Sources,
    )

    show = get_object_or_404(PodcastShow, id=show_id)
    format_type = request.GET.get("format", "json")  # 'json' or 'html'

    # Get pagination parameters
    try:
        page = int(request.GET.get("page", 1))
        page_size = int(request.GET.get("page_size", 20))
    except ValueError:
        page = 1
        page_size = 20

    # Get all episodes for this show, ordered by published date (newest first)
    # Use Coalesce to handle None published dates (put them at the end)
    from datetime import datetime

    from django.db.models import DateTimeField, Value
    from django.db.models.functions import Coalesce

    # Episodes with published dates first (newest), then episodes without dates
    episodes_qs = PodcastEpisode.objects.filter(show=show).annotate(
        published_or_old=Coalesce(
            "published",
            Value(datetime(1970, 1, 1, tzinfo=UTC),
                  output_field=DateTimeField()),
        ),
    ).order_by("-published_or_old", "-episode_number")
    total_count = episodes_qs.count()

    # Calculate pagination
    start = (page - 1) * page_size
    end = start + page_size
    episodes = episodes_qs[start:end]

    # Get user's podcast entries for this show
    # Order by created_at descending so we get the most recent entry when multiple exist
    # This allows multiple plays of the same episode to be tracked separately in the DB
    # but we show the most recent one in the UI
    user_podcasts = list(Podcast.objects.filter(
        user=request.user,
        show=show,
    ).select_related("episode", "item").order_by("episode_id", "-created_at"))

    # Create a map of episode_id to user podcast
    # When multiple entries exist for the same episode, keep only the most recent one
    episode_podcast_map = {}
    for podcast in user_podcasts:
        if podcast.episode_id:
            # Only store the first (most recent after ordering) entry for each episode
            if podcast.episode_id not in episode_podcast_map:
                episode_podcast_map[podcast.episode_id] = podcast

    # Build episode items for enrichment
    episode_items_data = []
    episode_items_map = {}
    for episode in episodes:
        item, _ = Item.objects.get_or_create(
            media_id=episode.episode_uuid,
            source=Sources.POCKETCASTS.value,
            media_type=MediaTypes.PODCAST.value,
            defaults={
                "title": episode.title,
                "image": show.image or settings.IMG_NONE,
            },
        )
        if item.title != episode.title:
            item.title = episode.title
            item.save(update_fields=["title"])
        episode_items_data.append({
            "media_id": episode.episode_uuid,
            "source": Sources.POCKETCASTS.value,
            "media_type": MediaTypes.PODCAST.value,
        })
        episode_items_map[episode.episode_uuid] = item

    # Enrich episodes with user data
    enriched_episodes_raw = helpers.enrich_items_with_user_data(
        request,
        episode_items_data,
        user=request.user,
    )

    # Calculate pagination info
    has_more = end < total_count
    next_page = page + 1 if has_more else None

    if format_type == "html":
        # Return HTML fragments for HTMX
        from django.template.loader import render_to_string

        # Build episode data similar to media_details view
        episode_list = []
        for episode_obj in episodes:
            # Find enriched data
            enriched = None
            for e in enriched_episodes_raw:
                if e["item"]["media_id"] == episode_obj.episode_uuid:
                    enriched = e
                    break

            # Format duration
            duration_str = ""
            if episode_obj.duration:
                hours = episode_obj.duration // 3600
                minutes = (episode_obj.duration % 3600) // 60
                if hours > 0:
                    duration_str = f"{hours}h {minutes}m"
                else:
                    duration_str = f"{minutes}m"

            # Get user's podcast for this episode
            user_podcast = episode_podcast_map.get(episode_obj.id)

            # Create adapter objects (same as media_details view)
            class PodcastEpisodeAdapter:
                def __init__(self, episode):
                    self.title = episode.title
                    self.track_number = episode.episode_number
                    self.duration_formatted = self._format_duration(episode.duration) if episode.duration else None
                    self.musicbrainz_recording_id = None
                    self.id = episode.id
                    self.published = episode.published
                    self.episode_uuid = episode.episode_uuid

                def _format_duration(self, seconds):
                    hours = seconds // 3600
                    minutes = (seconds % 3600) // 60
                    secs = seconds % 60
                    if hours > 0:
                        return f"{hours}:{minutes:02d}:{secs:02d}"
                    return f"{minutes}:{secs:02d}"

            class PodcastShowAdapter:
                def __init__(self, show):
                    self.image = show.image or settings.IMG_NONE
                    self.release_date = None
                    self.id = show.id

            # Create history wrapper
            all_history = []
            if user_podcast:
                all_history = list(user_podcast.history.filter(end_date__isnull=False).order_by("-end_date")[:10])
                class PodcastHistoryWrapper:
                    def __init__(self, podcast, item, history_list):
                        self.item = item
                        self.id = podcast.id
                        self._history_list = history_list

                    @property
                    def completed_play_count(self):
                        """Return count of completed plays (history records with end_date)."""
                        # Since we already filtered all_history to only include records with end_date,
                        # we can just count the length of the filtered history_list
                        return len(self._history_list)

                    @property
                    def history(self):
                        class HistoryProxy:
                            def __init__(self, history_list):
                                self._history = history_list
                            def all(self):
                                return self._history
                            def count(self):
                                return len(self._history)
                        return HistoryProxy(self._history_list)

                podcast_wrapper = PodcastHistoryWrapper(user_podcast, enriched["item"] if enriched else item, all_history)
            else:
                class DummyPodcast:
                    def __init__(self, item):
                        self.item = item
                        self.id = 0
                        self.history = type("History", (), {"count": lambda: 0, "all": list})()
                podcast_wrapper = DummyPodcast(enriched["item"] if enriched else item)

            episode_list.append({
                "title": episode_obj.title,
                "episode_number": episode_obj.episode_number or 0,
                "image": show.image or settings.IMG_NONE,
                "air_date": episode_obj.published,
                "runtime": duration_str,
                "overview": "",
                "history": all_history,
                "media": enriched["media"] if enriched else None,
                "item": enriched["item"] if enriched else item,
                "media_id": episode_obj.episode_uuid,
                "source": Sources.POCKETCASTS.value,
                "media_type": MediaTypes.PODCAST.value,
                "track_adapter": PodcastEpisodeAdapter(episode_obj),
                "album_adapter": PodcastShowAdapter(show),
                "music_wrapper": podcast_wrapper,
            })

        # Render HTML fragment
        html = render_to_string(
            "app/components/podcast_episode_list.html",
            {
                "episodes": episode_list,
                "user": request.user,
                "show": show,
                "IMG_NONE": settings.IMG_NONE,
                "TRACK_TIME": True,
                "has_more": has_more,
                "next_page": next_page,
                "show_id": show_id,
            },
            request=request,
        )
        response = HttpResponse(html)
        # Prevent caching of episode list fragments
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response
    # Return JSON
    episode_list = []
    for episode_obj in episodes:
        # Find enriched data
        enriched = None
        for e in enriched_episodes_raw:
            if e["item"]["media_id"] == episode_obj.episode_uuid:
                enriched = e
                break

        # Format duration
        duration_str = ""
        if episode_obj.duration:
            hours = episode_obj.duration // 3600
            minutes = (episode_obj.duration % 3600) // 60
            if hours > 0:
                duration_str = f"{hours}h {minutes}m"
            else:
                duration_str = f"{minutes}m"

        # Get status if user has listened
        user_podcast = episode_podcast_map.get(episode_obj.id)
        status = user_podcast.status if user_podcast else None

        episode_data = {
            "id": episode_obj.id,
            "title": episode_obj.title,
            "published": episode_obj.published.isoformat() if episode_obj.published else None,
            "duration": duration_str,
            "duration_seconds": episode_obj.duration,
            "episode_number": episode_obj.episode_number,
            "status": status,
            "has_history": enriched and enriched.get("media") is not None,
        }
        episode_list.append(episode_data)

    total_pages = (total_count + page_size - 1) // page_size

    return JsonResponse({
        "episodes": episode_list,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "total_pages": total_pages,
            "has_more": has_more,
        },
    })


@require_POST
def podcast_show_save(request):
    """Save a podcast show tracker - mirrors artist_save."""
    from django.shortcuts import get_object_or_404

    from app.forms import PodcastShowTrackerForm
    from app.models import PodcastShow, PodcastShowTracker

    show_id = request.POST.get("show_id")
    show = get_object_or_404(PodcastShow, id=show_id)

    # Get existing tracker or None
    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()

    form = PodcastShowTrackerForm(request.POST, instance=tracker)
    if form.is_valid():
        tracker = form.save(commit=False)
        tracker.user = request.user
        tracker.show = show
        tracker.save()
        messages.success(request, f"Saved {show.title}")
    else:
        messages.error(request, f"Error saving {show.title}: {form.errors}")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("podcast_show_detail", show_id=show.id)


@require_POST
def podcast_show_delete(request):
    """Delete a podcast show tracker - mirrors artist_delete."""
    from django.shortcuts import get_object_or_404

    from app.models import PodcastShow, PodcastShowTracker

    show_id = request.POST.get("show_id")
    show = get_object_or_404(PodcastShow, id=show_id)

    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {show.title} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("podcast_show_detail", show_id=show.id)


@require_POST
def podcast_mark_all_played(request, show_id):
    """Mark all unplayed episodes for a podcast show as completed on their release date."""
    import hashlib

    from django.conf import settings
    from django.shortcuts import get_object_or_404
    from django.utils import timezone

    import events
    from app.mixins import disable_fetch_releases
    from app.models import (
        Item,
        MediaTypes,
        Podcast,
        PodcastEpisode,
        PodcastShow,
        PodcastShowTracker,
        Sources,
        Status,
    )
    from integrations import podcast_rss

    show = get_object_or_404(PodcastShow, id=show_id)

    # Create tracker if it doesn't exist (user hasn't added show to library yet)
    tracker, _ = PodcastShowTracker.objects.get_or_create(
        user=request.user,
        show=show,
        defaults={"status": Status.IN_PROGRESS.value},
    )

    # If show has RSS feed, fetch full episode list and ensure all episodes are in database
    if show.rss_feed_url:
        try:
            # Fetch ALL episodes (no limit) from RSS feed
            episodes_data = podcast_rss.fetch_episodes_from_rss(show.rss_feed_url, limit=None)

            for episode_data in episodes_data:
                # Generate episode UUID from GUID or create one
                # Use GUID directly (consistent with _sync_episodes_from_rss logic)
                episode_uuid = episode_data.get("guid")
                if not episode_uuid:
                    # Use a hash of title + published date as fallback UUID
                    import hashlib
                    uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                    episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                # Check if episode already exists by UUID, or try to match by title + date
                episode = None
                try:
                    episode = PodcastEpisode.objects.get(episode_uuid=episode_uuid)
                except PodcastEpisode.DoesNotExist:
                    # Try to match by title + published date
                    if episode_data.get("title") and episode_data.get("published"):
                        matching = PodcastEpisode.objects.filter(
                            show=show,
                            title__iexact=episode_data["title"].strip(),
                            published__date=episode_data["published"].date(),
                        ).first()
                        if matching:
                            episode = matching
                except PodcastEpisode.MultipleObjectsReturned:
                    # If multiple found, use first one
                    episode = PodcastEpisode.objects.filter(episode_uuid=episode_uuid).first()

                if not episode:
                    PodcastEpisode.objects.create(
                        show=show,
                        episode_uuid=episode_uuid,
                        title=episode_data.get("title", "Unknown Episode"),
                        published=episode_data.get("published"),
                        duration=episode_data.get("duration"),
                        audio_url=episode_data.get("audio_url", ""),
                        episode_number=episode_data.get("episode_number"),
                        season_number=episode_data.get("season_number"),
                    )
        except Exception as e:
            logger.warning("Failed to fetch full episode list from RSS feed %s: %s", show.rss_feed_url, e)
            # Continue with existing episodes in database

    # Get all episodes for this show (now including any newly fetched ones)
    all_episodes = PodcastEpisode.objects.filter(show=show)

    # Get all episodes the user has already completed (has end_date)
    completed_episodes = set(
        Podcast.objects.filter(
            user=request.user,
            show=show,
            episode__isnull=False,
            end_date__isnull=False,  # Only count completed episodes
        ).values_list("episode_id", flat=True),
    )

    # Find unplayed episodes (episodes without a completed Podcast entry)
    unplayed_episodes = all_episodes.exclude(id__in=completed_episodes)

    if not unplayed_episodes.exists():
        messages.info(request, f"All episodes of {show.title} are already marked as played")
        return redirect("media_details", source=Sources.POCKETCASTS.value, media_type=MediaTypes.PODCAST.value, media_id=show.podcast_uuid, title=show.slug or show.title)

    created_count = 0
    items_created = []

    # Disable calendar triggers during bulk operations to avoid queuing hundreds of tasks
    with disable_fetch_releases():
        for episode in unplayed_episodes:
            # Get or create Item for this episode
            runtime_minutes = episode.duration // 60 if episode.duration else None
            item_defaults = {
                "title": episode.title,
                "image": show.image or settings.IMG_NONE,
            }
            if runtime_minutes:
                item_defaults["runtime_minutes"] = runtime_minutes

            item, item_created = Item.objects.get_or_create(
                media_id=episode.episode_uuid,
                source=Sources.POCKETCASTS.value,
                media_type=MediaTypes.PODCAST.value,
                defaults=item_defaults,
            )

            # Update runtime if item existed but didn't have it
            if not item_created and not item.runtime_minutes and runtime_minutes:
                item.runtime_minutes = runtime_minutes
                item.save(update_fields=["runtime_minutes"])

            # Track items for calendar reload
            if item_created:
                items_created.append(item)

            # Use episode's published date as end_date, or current time if no published date
            end_date = episode.published if episode.published else timezone.now()

            # Create Podcast entry marking as completed
            Podcast.objects.create(
                item=item,
                user=request.user,
                show=show,
                episode=episode,
                status=Status.COMPLETED.value,
                end_date=end_date,
                progress=runtime_minutes if runtime_minutes else 0,
            )
            created_count += 1

    # Trigger a single calendar reload for all created items (if any)
    if items_created:
        events.tasks.reload_calendar.apply_async(kwargs={"items_to_process": items_created}, countdown=3)

    episode_word = "episodes" if created_count != 1 else "episode"
    messages.success(
        request,
        f"Marked {created_count} {episode_word} of {show.title} as played",
    )

    return redirect("media_details", source=Sources.POCKETCASTS.value, media_type=MediaTypes.PODCAST.value, media_id=show.podcast_uuid, title=show.slug or show.title)


def album_track_modal(request, album_id):
    """Return the tracking form modal for an album - mirrors artist_track_modal."""
    from django.shortcuts import get_object_or_404

    from app.forms import AlbumTrackerForm
    from app.models import AlbumTracker

    album = get_object_or_404(Album, id=album_id)
    return_url = request.GET.get("return_url", "")

    # Get existing tracker if any
    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()

    initial_data = {"album_id": album.id}
    form = AlbumTrackerForm(instance=tracker, initial=initial_data)

    return render(
        request,
        "app/components/album_track_modal.html",
        {
            "album": album,
            "tracker": tracker,
            "form": form,
            "return_url": return_url,
        },
    )


@require_POST
def album_save(request):
    """Save an album tracker - mirrors artist_save."""
    from django.shortcuts import get_object_or_404

    from app.forms import AlbumTrackerForm
    from app.models import AlbumTracker

    album_id = request.POST.get("album_id")
    album = get_object_or_404(Album, id=album_id)

    # Get existing tracker or None
    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()

    form = AlbumTrackerForm(request.POST, instance=tracker)
    if form.is_valid():
        tracker = form.save(commit=False)
        tracker.user = request.user
        tracker.album = album
        tracker.save()
        messages.success(request, f"Saved {album.title}")
    else:
        messages.error(request, f"Error saving {album.title}: {form.errors}")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("album_detail", album_id=album.id)


@require_POST
def album_delete(request):
    """Delete an album tracker - mirrors artist_delete."""
    from django.shortcuts import get_object_or_404

    from app.models import AlbumTracker

    album_id = request.POST.get("album_id")
    album = get_object_or_404(Album, id=album_id)

    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {album.title} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("album_detail", album_id=album.id)


@require_POST
def song_save(request):
    """Handle adding a listen for a song - mirrors episode_save for episodes."""
    from django.shortcuts import get_object_or_404
    from django.utils.dateparse import parse_date, parse_datetime

    from app.models import Track

    recording_id = request.POST.get("recording_id")
    album_id = request.POST.get("album_id")
    track_id = request.POST.get("track_id")
    end_date_str = request.POST.get("end_date")

    # Parse the end date
    end_date = None
    if end_date_str:
        end_date = parse_datetime(end_date_str)
        if not end_date:
            parsed_date = parse_date(end_date_str)
            if parsed_date:
                from django.utils import timezone
                end_date = timezone.make_aware(
                    timezone.datetime.combine(parsed_date, timezone.datetime.min.time()),
                )

    # Get the album and track
    album = get_object_or_404(Album, id=album_id)
    track = get_object_or_404(Track, id=track_id) if track_id else None

    # Check if user already has a Music entry for this track
    existing_music = Music.objects.filter(
        user=request.user,
        album=album,
        track=track,
    ).first()

    # Calculate runtime from track duration if available
    runtime_minutes = None
    if track and track.duration_ms:
        runtime_minutes = track.duration_ms // 60000  # Convert ms to minutes

    if existing_music:
        # Add a new history entry (rewatch/relisten)
        existing_music.end_date = end_date
        existing_music.save()

        # Update Item runtime if not set and we have it
        if runtime_minutes and existing_music.item and not existing_music.item.runtime_minutes:
            existing_music.item.runtime_minutes = runtime_minutes
            existing_music.item.save(update_fields=["runtime_minutes"])

        messages.success(request, f"Added listen for {track.title if track else 'track'}")
    else:
        # Create new Music entry
        # First, get or create the Item for this recording
        item_defaults = {
            "title": track.title if track else "Unknown Track",
            "image": album.image or settings.IMG_NONE,
        }
        if runtime_minutes:
            item_defaults["runtime_minutes"] = runtime_minutes

        if recording_id:
            item, created = Item.objects.get_or_create(
                media_id=recording_id,
                source=Sources.MUSICBRAINZ.value,
                media_type=MediaTypes.MUSIC.value,
                defaults=item_defaults,
            )
            # Update runtime if item existed but didn't have it
            if not created and not item.runtime_minutes and runtime_minutes:
                item.runtime_minutes = runtime_minutes
                item.save(update_fields=["runtime_minutes"])
        else:
            # Create a placeholder item for tracks without recording ID
            item, created = Item.objects.get_or_create(
                media_id=f"track_{track_id}",
                source=Sources.MUSICBRAINZ.value,
                media_type=MediaTypes.MUSIC.value,
                defaults=item_defaults,
            )
            # Update runtime if item existed but didn't have it
            if not created and not item.runtime_minutes and runtime_minutes:
                item.runtime_minutes = runtime_minutes
                item.save(update_fields=["runtime_minutes"])

        Music.objects.create(
            item=item,
            user=request.user,
            artist=album.artist,
            album=album,
            track=track,
            status=Status.COMPLETED.value,
            end_date=end_date,
        )
        messages.success(request, f"Added {track.title if track else 'track'} to your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect("album_detail", album_id=album.id)


@require_POST
def podcast_save(request):
    """Handle adding a play for a podcast episode - mirrors song_save for music."""
    from django.shortcuts import get_object_or_404
    from django.utils.dateparse import parse_date, parse_datetime

    from app.models import Podcast, PodcastEpisode, PodcastShow

    episode_uuid = request.POST.get("episode_uuid")
    show_id = request.POST.get("show_id")
    episode_id = request.POST.get("episode_id")
    end_date_str = request.POST.get("end_date")

    # Parse the end date
    end_date = None
    if end_date_str:
        end_date = parse_datetime(end_date_str)
        if not end_date:
            parsed_date = parse_date(end_date_str)
            if parsed_date:
                from django.utils import timezone
                end_date = timezone.make_aware(
                    timezone.datetime.combine(parsed_date, timezone.datetime.min.time()),
                )

    # Get the show and episode
    show = get_object_or_404(PodcastShow, id=show_id)
    episode = get_object_or_404(PodcastEpisode, id=episode_id) if episode_id else None

    # Calculate runtime from episode duration if available
    runtime_minutes = None
    if episode and episode.duration:
        runtime_minutes = episode.duration // 60  # Convert seconds to minutes

    # First, get or create the Item for this episode
    item_defaults = {
        "title": episode.title if episode else "Unknown Episode",
        "image": show.image or settings.IMG_NONE,
    }
    if runtime_minutes:
        item_defaults["runtime_minutes"] = runtime_minutes

    item, created = Item.objects.get_or_create(
        media_id=episode_uuid,
        source=Sources.POCKETCASTS.value,
        media_type=MediaTypes.PODCAST.value,
        defaults=item_defaults,
    )
    # Update runtime if item existed but didn't have it
    if not created and not item.runtime_minutes and runtime_minutes:
        item.runtime_minutes = runtime_minutes
        item.save(update_fields=["runtime_minutes"])

    # Check if user already has a Podcast entry for this episode
    existing_podcast = Podcast.objects.filter(
        user=request.user,
        item=item,
    ).first()

    if existing_podcast:
        # Check for duplicate before creating new history entry
        latest_history = existing_podcast.history.filter(end_date__isnull=False).order_by("-end_date").first()
        if latest_history and latest_history.end_date and end_date:
            time_diff = abs((end_date - latest_history.end_date).total_seconds())
            if time_diff < 300:  # 5 minutes threshold
                logger.debug("Skipping duplicate podcast history entry (time difference: %d seconds)", time_diff)
                messages.info(request, f"Play already recorded for {episode.title if episode else 'episode'}")
                # Continue to HTMX/redirect handling below - don't create duplicate but still return proper response
            else:
                # Add a new history entry (replay) by updating end_date
                # This creates a new history record via the historical records system
                existing_podcast.end_date = end_date

                # Update progress if needed
                if runtime_minutes and existing_podcast.progress != runtime_minutes:
                    existing_podcast.progress = runtime_minutes

                existing_podcast.save()
                messages.success(request, f"Added play for {episode.title if episode else 'episode'}")
        else:
            # No existing history or missing dates, proceed with creating history entry
            existing_podcast.end_date = end_date

            # Update progress if needed
            if runtime_minutes and existing_podcast.progress != runtime_minutes:
                existing_podcast.progress = runtime_minutes

            existing_podcast.save()
            messages.success(request, f"Added play for {episode.title if episode else 'episode'}")
    else:
        # Create new Podcast entry
        Podcast.objects.create(
            item=item,
            user=request.user,
            show=show,
            episode=episode,
            status=Status.COMPLETED.value,
            end_date=end_date,
            progress=runtime_minutes if runtime_minutes else 0,
        )
        messages.success(request, f"Added play for {episode.title if episode else 'episode'}")

    # If this is an HTMX request, return the updated episode card HTML
    if request.headers.get("HX-Request"):
        # Reuse the podcast_episodes_api logic to get the updated episode card
        from django.template.loader import render_to_string

        from app import helpers

        # Get the single episode with fresh data
        episode_obj = episode
        if not episode_obj:
            return HttpResponse("Episode not found", status=404)

        # Get user's podcast entry for this episode (should exist now)
        user_podcast = Podcast.objects.filter(
            user=request.user,
            show=show,
            episode=episode_obj,
        ).order_by("-created_at").first()

        # Build enriched episode data (similar to podcast_episodes_api)
        episode_items_data = [{
            "media_id": episode_obj.episode_uuid,
            "source": Sources.POCKETCASTS.value,
            "media_type": MediaTypes.PODCAST.value,
        }]
        enriched_episodes_raw = helpers.enrich_items_with_user_data(
            request,
            episode_items_data,
            user=request.user,
        )
        enriched = enriched_episodes_raw[0] if enriched_episodes_raw else {"item": {"media_id": episode_obj.episode_uuid}, "media": None}

        # Format duration
        duration_str = ""
        if episode_obj.duration:
            hours = episode_obj.duration // 3600
            minutes = (episode_obj.duration % 3600) // 60
            if hours > 0:
                duration_str = f"{hours}h {minutes}m"
            else:
                duration_str = f"{minutes}m"

        # Get history
        all_history = []
        if user_podcast:
            all_history = list(user_podcast.history.filter(end_date__isnull=False).order_by("-end_date")[:10])

            class PodcastHistoryWrapper:
                def __init__(self, podcast, item, history_list):
                    self.item = item
                    self.id = podcast.id
                    self._history_list = history_list

                @property
                def history(self):
                    class HistoryProxy:
                        def __init__(self, history_list):
                            self._history = history_list
                        def all(self):
                            return self._history
                        def count(self):
                            return len(self._history)
                    return HistoryProxy(self._history_list)

            podcast_wrapper = PodcastHistoryWrapper(user_podcast, item, all_history)
        else:
            class DummyPodcast:
                def __init__(self, item):
                    self.item = item
                    self.id = 0
                    self.history = type("History", (), {"count": lambda: 0, "all": list})()
            podcast_wrapper = DummyPodcast(item)

        # Create adapter classes
        class PodcastEpisodeAdapter:
            def __init__(self, episode):
                self.title = episode.title
                self.track_number = episode.episode_number
                self.duration_formatted = self._format_duration(episode.duration) if episode.duration else None
                self.musicbrainz_recording_id = None
                self.id = episode.id
                self.published = episode.published
                self.episode_uuid = episode.episode_uuid

            def _format_duration(self, seconds):
                hours = seconds // 3600
                minutes = (seconds % 3600) // 60
                secs = seconds % 60
                if hours > 0:
                    return f"{hours}:{minutes:02d}:{secs:02d}"
                return f"{minutes}:{secs:02d}"

        class PodcastShowAdapter:
            def __init__(self, show):
                self.image = show.image or settings.IMG_NONE
                self.id = show.id

        # Build episode data
        episode_data = {
            "title": episode_obj.title,
            "episode_number": episode_obj.episode_number or 0,
            "image": show.image or settings.IMG_NONE,
            "air_date": episode_obj.published,
            "runtime": duration_str,
            "overview": "",
            "history": all_history,
            "media": enriched["media"] if enriched else None,
            "item": item,
            "media_id": episode_obj.episode_uuid,
            "source": Sources.POCKETCASTS.value,
            "media_type": MediaTypes.PODCAST.value,
            "track_adapter": PodcastEpisodeAdapter(episode_obj),
            "album_adapter": PodcastShowAdapter(show),
            "music_wrapper": podcast_wrapper,
        }

        # Render just the single episode card
        html = render_to_string(
            "app/components/podcast_episode_list.html",
            {
                "episodes": [episode_data],
                "user": request.user,
                "show": show,
                "IMG_NONE": settings.IMG_NONE,
                "TRACK_TIME": True,
                "has_more": False,
                "show_id": show.id,
            },
            request=request,
        )
        response = HttpResponse(html)
        # Close the modal after successful save
        response["HX-Trigger"] = "closeModal"
        return response

    # Always redirect to media_details page for the podcast show
    # Don't trust the 'next' parameter as it might point to the API endpoint
    from django.utils.text import slugify
    return redirect(
        "media_details",
        source=Sources.POCKETCASTS.value,
        media_type=MediaTypes.PODCAST.value,
        media_id=show.podcast_uuid,
        title=show.slug or slugify(show.title),
    )


@require_POST
def delete_all_album_plays_view(request, album_id):
    """Delete all music plays (listens) for an album."""
    from django.shortcuts import get_object_or_404

    album = get_object_or_404(Album, id=album_id)

    # Get all Music entries for this user and album
    music_entries = Music.objects.filter(
        user=request.user,
        album=album,
    )

    count = music_entries.count()
    if count > 0:
        music_entries.delete()
        messages.success(request, f"Deleted {count} play{'s' if count != 1 else ''} for {album.title}")
    else:
        messages.info(request, f"No plays found for {album.title}")

    # Return HX-Refresh header to reload the page
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@require_POST
def delete_all_artist_plays_view(request, artist_id):
    """Delete all music plays (listens) for an artist."""
    from django.shortcuts import get_object_or_404

    artist = get_object_or_404(Artist, id=artist_id)

    # Get all Music entries for this user and artist (via album)
    music_entries = Music.objects.filter(
        user=request.user,
        album__artist=artist,
    )

    count = music_entries.count()
    if count > 0:
        music_entries.delete()
        messages.success(request, f"Deleted {count} play{'s' if count != 1 else ''} for {artist.name}")
    else:
        messages.info(request, f"No plays found for {artist.name}")

    # Return HX-Refresh header to reload the page
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@require_POST
def sync_album_metadata_view(request, album_id):
    """Manually trigger metadata sync for an album."""
    from django.shortcuts import get_object_or_404

    from app.models import Track
    from app.providers import musicbrainz
    from app.services.music import ensure_album_has_release_id

    album = get_object_or_404(Album, id=album_id)

    # Ensure we have a release_id
    ensure_album_has_release_id(album)

    if album.musicbrainz_release_id:
        try:
            # Fetch fresh data from MusicBrainz
            release_data = musicbrainz.get_release(album.musicbrainz_release_id)

            # Update album image
            new_image = release_data.get("image", "")
            if new_image and new_image != settings.IMG_NONE:
                album.image = new_image

            if release_data.get("genres"):
                album.genres = release_data.get("genres")

            # Update tracks
            tracks_data = release_data.get("tracks", [])
            for track_data in tracks_data:
                Track.objects.update_or_create(
                    album=album,
                    disc_number=track_data.get("disc_number", 1),
                    track_number=track_data.get("track_number"),
                    defaults={
                        "title": track_data.get("title", "Unknown Track"),
                        "musicbrainz_recording_id": track_data.get("recording_id"),
                        "duration_ms": track_data.get("duration_ms"),
                        "genres": track_data.get("genres", []) or release_data.get("genres", []),
                    },
                )

            album.tracks_populated = True
            album.save(update_fields=["tracks_populated", "image", "genres"])

            messages.success(request, f"Synced {len(tracks_data)} tracks for {album.title}")
        except Exception as e:
            logger.warning("Failed to sync album %s: %s", album.title, e)
            messages.error(request, f"Failed to sync album: {e}")
    else:
        messages.warning(request, "Could not find a MusicBrainz release for this album")

    # Return HX-Refresh header to reload the page
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@require_GET
def statistics(request):
    """Return the statistics page."""
    try:
        # Set default date range to last year
        timeformat = "%Y-%m-%d"
        today = timezone.localdate()
        one_year_ago = today.replace(year=today.year - 1)

        # Get date parameters with defaults
        start_date_param = request.GET.get("start-date")
        end_date_param = request.GET.get("end-date")

        if not start_date_param and not end_date_param:
            preferred_range = getattr(request.user, "statistics_default_range", None)
            if preferred_range not in statistics_cache.PREDEFINED_RANGES:
                preferred_range = "Last 12 Months"
            preferred_start, preferred_end = _get_predefined_range_date_strings(
                preferred_range,
                today,
                timeformat,
            )
            if preferred_start and preferred_end:
                start_date_str = preferred_start
                end_date_str = preferred_end
            else:
                start_date_str = one_year_ago.strftime(timeformat)
                end_date_str = today.strftime(timeformat)
        else:
            start_date_str = start_date_param or one_year_ago.strftime(timeformat)
            end_date_str = end_date_param or today.strftime(timeformat)

        if start_date_str == "all" and end_date_str == "all":
            start_date = None
            end_date = None
        else:
            start_date = parse_date(start_date_str)
            end_date = parse_date(end_date_str)

            if start_date and end_date:
                # Convert to datetime with timezone awareness
                start_date = timezone.make_aware(
                    datetime.combine(start_date, datetime.min.time()),
                    timezone.get_current_timezone(),
                )

                # End date should be end of day
                end_date = timezone.make_aware(
                    datetime.combine(end_date, datetime.max.time()),
                    timezone.get_current_timezone(),
                )

        # Identify predefined range for caching
        selected_range_name = _identify_predefined_range(start_date, end_date)

        if selected_range_name in statistics_cache.PREDEFINED_RANGES:
            request.user.update_preference("statistics_default_range", selected_range_name)
        
        # Get statistics data (cached for predefined ranges, computed inline for custom ranges)
        statistics_data = statistics_cache.get_statistics_data(
            request.user,
            start_date,
            end_date,
            range_name=selected_range_name,
        )

        show_year_charts = selected_range_name in (None, "All Time")

        # Get top rated by media type for compact cards
        top_rated = statistics_data["top_rated"]  # Keep for backward compatibility with "ALL MEDIA" section
        top_rated_by_type = statistics_data.get("top_rated_by_type", {})
        top_rated_movie = top_rated_by_type.get("movie", [])
        top_rated_tv = top_rated_by_type.get("tv", [])

        # Format dates as strings for URL parameters
        start_date_str_for_url = start_date_str if start_date_str else ""
        end_date_str_for_url = end_date_str if end_date_str else ""

        context = {
            "user": request.user,
            "start_date": start_date,
            "end_date": end_date,
            "start_date_str": start_date_str_for_url,
            "end_date_str": end_date_str_for_url,
            "selected_range_name": selected_range_name,
            "media_count": statistics_data["media_count"],
            "activity_data": statistics_data["activity_data"],
            "media_type_distribution": statistics_data["media_type_distribution"],
            "score_distribution": statistics_data["score_distribution"],
            "top_rated": statistics_data["top_rated"],
            "top_rated_movie": top_rated_movie,
            "top_rated_tv": top_rated_tv,
            "top_played": statistics_data["top_played"],
            "status_distribution": statistics_data["status_distribution"],
            "status_pie_chart_data": statistics_data["status_pie_chart_data"],
            "hours_per_media_type": statistics_data["hours_per_media_type"],
            "tv_consumption": statistics_data["tv_consumption"],
            "movie_consumption": statistics_data["movie_consumption"],
            "music_consumption": statistics_data["music_consumption"],
            "podcast_consumption": statistics_data["podcast_consumption"],
            "game_consumption": statistics_data["game_consumption"],
            "daily_hours_by_media_type": statistics_data["daily_hours_by_media_type"],
            "show_year_charts": show_year_charts,
            "media_type_colors": {
                "tv": config.get_stats_color(MediaTypes.TV.value),
                "movie": config.get_stats_color(MediaTypes.MOVIE.value),
                "game": config.get_stats_color(MediaTypes.GAME.value),
                "music": config.get_stats_color(MediaTypes.MUSIC.value),
                "podcast": config.get_stats_color(MediaTypes.PODCAST.value),
            },
        }

        return render(request, "app/statistics.html", context)
    except OperationalError as error:
        logger.error("Database error in statistics view: %s", error, exc_info=True)
        # Return empty state on database error
        timeformat = "%Y-%m-%d"
        today = timezone.localdate()
        one_year_ago = today.replace(year=today.year - 1)
        start_date_str = request.GET.get("start-date") or one_year_ago.strftime(timeformat)
        end_date_str = request.GET.get("end-date") or today.strftime(timeformat)

        # Create empty statistics data structure
        empty_statistics_data = {
            "media_count": {},
            "activity_data": [],
            "media_type_distribution": {},
            "hours_per_media_type": {},
            "media_type_colors": {
                "tv": config.get_stats_color(MediaTypes.TV.value),
                "movie": config.get_stats_color(MediaTypes.MOVIE.value),
                "game": config.get_stats_color(MediaTypes.GAME.value),
                "music": config.get_stats_color(MediaTypes.MUSIC.value),
                "podcast": config.get_stats_color(MediaTypes.PODCAST.value),
            },
            "score_distribution": {},
            "top_rated": [],
            "top_played": [],
            "status_distribution": {},
            "status_pie_chart_data": {},
            "hours_per_media_type": {},
            "tv_consumption": {},
            "movie_consumption": {},
            "music_consumption": {},
            "podcast_consumption": {},
            "game_consumption": {},
            "daily_hours_by_media_type": {},
        }

        context = {
            "user": request.user,
            "start_date": parse_date(start_date_str) if start_date_str != "all" else None,
            "end_date": parse_date(end_date_str) if end_date_str != "all" else None,
            "media_count": empty_statistics_data["media_count"],
            "activity_data": empty_statistics_data["activity_data"],
            "media_type_distribution": empty_statistics_data["media_type_distribution"],
            "score_distribution": empty_statistics_data["score_distribution"],
            "top_rated": empty_statistics_data["top_rated"],
            "top_played": empty_statistics_data["top_played"],
            "status_distribution": empty_statistics_data["status_distribution"],
            "status_pie_chart_data": empty_statistics_data["status_pie_chart_data"],
            "hours_per_media_type": empty_statistics_data["hours_per_media_type"],
            "tv_consumption": empty_statistics_data["tv_consumption"],
            "movie_consumption": empty_statistics_data["movie_consumption"],
            "music_consumption": empty_statistics_data["music_consumption"],
            "podcast_consumption": empty_statistics_data["podcast_consumption"],
            "game_consumption": empty_statistics_data["game_consumption"],
            "daily_hours_by_media_type": empty_statistics_data["daily_hours_by_media_type"],
            "media_type_colors": empty_statistics_data["media_type_colors"],
            "show_year_charts": False,
            "database_error": True,
        }
        return render(request, "app/statistics.html", context)


@require_GET
def cache_status(request):
    """Return cache status metadata for history or statistics cache.
    
    Query params:
        cache_type: 'history' or 'statistics'
        range_name: Required for statistics, ignored for history
        logging_style: Optional for history, defaults to 'repeats'
    
    Returns JSON with:
        exists: bool - Whether cache exists
        built_at: str - ISO format timestamp when cache was built (or None)
        is_stale: bool - Whether cache is considered stale
        is_refreshing: bool - Whether a refresh is currently in progress
        recently_built: bool - Whether cache was built in the last 30 seconds
    """
    cache_type = request.GET.get("cache_type")
    if cache_type not in ("history", "statistics"):
        return JsonResponse({"error": "Invalid cache_type. Must be 'history' or 'statistics'"}, status=400)

    if cache_type == "history":
        logging_style = request.GET.get("logging_style")
        if logging_style not in ("sessions", "repeats"):
            logging_style = "repeats"
        cache_entry = cache.get(history_cache._cache_key(request.user.id, logging_style))
        refresh_lock_key = history_cache._refresh_lock_key(request.user.id, logging_style)
        refresh_lock = cache.get(refresh_lock_key)
        if refresh_lock and statistics_cache._lock_is_stale(refresh_lock):
            cache.delete(refresh_lock_key)
            refresh_lock = None
        lock_has_day_keys = isinstance(refresh_lock, dict) and bool(refresh_lock.get("day_keys"))

        # If lock is too old, clear it to avoid a stuck "refreshing" state
        if refresh_lock:
            if isinstance(refresh_lock, dict):
                started_at = refresh_lock.get("started_at")
                if started_at and timezone.now() - started_at > history_cache.HISTORY_REFRESH_LOCK_MAX_AGE:
                    cache.delete(refresh_lock_key)
                    refresh_lock = None
            else:
                # Legacy lock with no metadata - clear it
                cache.delete(refresh_lock_key)
                refresh_lock = None

        # Debug logging to help diagnose lock issues
        logger.debug(
            "Cache status check for user %s, logging_style %s: lock_key=%s, lock_exists=%s",
            request.user.id,
            logging_style,
            refresh_lock_key,
            refresh_lock is not None,
        )

        if cache_entry:
            built_at = cache_entry.get("built_at")
            is_stale = False
            recently_built = False
            if built_at:
                age = timezone.now() - built_at
                is_stale = age > history_cache.HISTORY_STALE_AFTER
                # Consider cache "recently built" if it was built in the last 60 seconds
                # This helps catch refreshes that completed just before or during page load
                recently_built = age < timedelta(seconds=60)
                # If the cache was just rebuilt but the lock is still set, clear it
                # to avoid a stuck "refreshing" state on the frontend.
                if refresh_lock and recently_built and not lock_has_day_keys:
                    cache.delete(refresh_lock_key)
                    refresh_lock = None
                # If cache is fresh (not stale), ignore lingering locks for index rebuilds.
                # Page-day refresh locks should remain until the task completes.
                if not is_stale and refresh_lock and not lock_has_day_keys:
                    cache.delete(refresh_lock_key)
                    refresh_lock = None

            return JsonResponse({
                "exists": True,
                "built_at": built_at.isoformat() if built_at else None,
                "is_stale": is_stale,
                "is_refreshing": refresh_lock is not None,
                "recently_built": recently_built,
            })
        return JsonResponse({
            "exists": False,
            "built_at": None,
            "is_stale": False,
            "is_refreshing": refresh_lock is not None,
            "recently_built": False,
        })

    if cache_type == "statistics":
        range_name = request.GET.get("range_name")
        if not range_name:
            return JsonResponse({"error": "range_name is required for statistics cache"}, status=400)

        if range_name not in statistics_cache.PREDEFINED_RANGES:
            return JsonResponse({
                "exists": False,
                "built_at": None,
                "is_stale": False,
                "is_refreshing": False,
                "recently_built": False,
                "any_range_refreshing": False,
            })

        cache_key = statistics_cache._cache_key(request.user.id, range_name)
        refresh_lock_key = statistics_cache._refresh_lock_key(request.user.id, range_name)
        cache_entry = cache.get(cache_key)
        refresh_lock = cache.get(refresh_lock_key)
        if refresh_lock and statistics_cache._lock_is_stale(refresh_lock):
            cache.delete(refresh_lock_key)
            refresh_lock = None

        any_range_refreshing = statistics_cache._any_range_refreshing(request.user.id)
        metadata_lock, metadata_built_at, metadata_recently_built = (
            statistics_cache._metadata_refresh_status(request.user.id)
        )
        metadata_refreshing = metadata_lock is not None

        refresh_scheduled = False
        if cache_entry:
            built_at = cache_entry.get("built_at")
            history_version = cache_entry.get("history_version")
            current_version = statistics_cache._get_history_version(request.user.id)
            is_stale = False
            recently_built = False
            age = None
            if built_at:
                age = timezone.now() - built_at
                # Consider cache "recently built" if it was built in the last 60 seconds
                # This helps catch refreshes that completed just before or during page load
                recently_built = age < timedelta(seconds=60)
            if history_version:
                is_stale = history_version != current_version
            elif age:
                is_stale = age > statistics_cache.STATISTICS_STALE_AFTER

            if not is_stale and refresh_lock:
                cache.delete(refresh_lock_key)
                refresh_lock = None
            elif is_stale and refresh_lock is None:
                refresh_scheduled = statistics_cache.schedule_statistics_refresh(
                    request.user.id,
                    range_name,
                    allow_inline=False,
                )
                refresh_lock = cache.get(refresh_lock_key) if refresh_scheduled else refresh_lock

            is_refreshing = refresh_lock is not None or refresh_scheduled or metadata_refreshing
            return JsonResponse({
                "exists": True,
                "built_at": built_at.isoformat() if built_at else None,
                "is_stale": is_stale,
                "is_refreshing": is_refreshing,
                "recently_built": recently_built,
                "any_range_refreshing": any_range_refreshing,
                "refresh_scheduled": refresh_scheduled,
                "metadata_refreshing": metadata_refreshing,
                "metadata_built_at": metadata_built_at.isoformat() if metadata_built_at else None,
                "metadata_recently_built": metadata_recently_built,
            })
        is_refreshing = refresh_lock is not None or metadata_refreshing
        return JsonResponse({
            "exists": False,
            "built_at": None,
            "is_stale": False,
            "is_refreshing": is_refreshing,
            "recently_built": False,
            "any_range_refreshing": any_range_refreshing,
            "refresh_scheduled": False,
            "metadata_refreshing": metadata_refreshing,
            "metadata_built_at": metadata_built_at.isoformat() if metadata_built_at else None,
            "metadata_recently_built": metadata_recently_built,
        })


@require_GET
def service_worker(request):
    """Serve the service worker file from static files."""
    sw_path = Path(settings.STATICFILES_DIRS[0]) / "js" / "serviceworker.js"
    with sw_path.open(encoding="utf-8") as sw_file:
        response = HttpResponse(sw_file.read(), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    return response


def _sort_tv_media_by_time_left(media_list, direction="asc"):
    """Sort TV media by time left with explicit grouping order.

    Group order:
      1) Active (episodes_left > 0 for non-dropped statuses) by least total time left first
      2) In-Progress caught-up (episodes_left == 0) newest end_date first
      3) Completed (episodes_left == 0) newest end_date first
      4) Dropped (episodes_left may be 0 or > 0) newest end_date first
      5) Unreleased/unknown runtime at the very end
    """
    import logging

    from django.core.cache import cache

    from app.statistics import parse_runtime_to_minutes

    logger = logging.getLogger(__name__)

    def _calc_runtime_minutes(media):
        """Best-effort runtime in minutes for a TV show or fallback."""
        runtime_minutes = None
        # FIRST: Check locally stored runtime (but exclude 999999 marker for unknown)
        if hasattr(media, "item") and media.item.runtime_minutes:
            # 999999 is a placeholder value meaning "unknown runtime" - skip it
            if media.item.runtime_minutes < 999999:
                runtime_minutes = media.item.runtime_minutes
                logger.debug(f"Using stored runtime for {media.item.title}: {runtime_minutes}min")
            else:
                logger.debug(f"Skipping invalid runtime marker ({media.item.runtime_minutes}min) for {media.item.title}")

        if not runtime_minutes:
            # SECOND: Check for episode-level runtime data from database
            # This is the most accurate - uses actual episode runtimes that were saved when viewing season pages
            from app.models import Item, MediaTypes
            episodes_with_runtime = Item.objects.filter(
                media_id=media.item.media_id,
                source=media.item.source,
                media_type=MediaTypes.EPISODE.value,
                runtime_minutes__isnull=False,
            ).exclude(
                runtime_minutes=999999,
            ).values_list("runtime_minutes", flat=True)

            if episodes_with_runtime.exists():
                # Calculate average runtime from actual episodes
                episode_runtimes = list(episodes_with_runtime)
                runtime_minutes = round(sum(episode_runtimes) / len(episode_runtimes))
                logger.debug(f"Using average episode runtime for {media.item.title}: {runtime_minutes}min (from {len(episode_runtimes)} episodes)")

        if not runtime_minutes:
            # THIRD: Check cached season data (avg_runtime field from season metadata)
            season_cache_key = f"tmdb_season_{media.item.media_id}_1"
            cached_season_data = cache.get(season_cache_key)
            if cached_season_data and cached_season_data.get("details", {}).get("runtime"):
                runtime_str = cached_season_data["details"]["runtime"]
                runtime_minutes = parse_runtime_to_minutes(runtime_str)
                if runtime_minutes and runtime_minutes > 0:
                    logger.debug(f"Using cached season avg runtime for {media.item.title}: {runtime_minutes}min")
            # Try other seasons if season 1 didn't work
            if not runtime_minutes:
                for season_num in [2, 3, 4, 5]:
                    season_cache_key = f"tmdb_season_{media.item.media_id}_{season_num}"
                    cached_season_data = cache.get(season_cache_key)
                    if cached_season_data and cached_season_data.get("details", {}).get("runtime"):
                        runtime_str = cached_season_data["details"]["runtime"]
                        runtime_minutes = parse_runtime_to_minutes(runtime_str)
                        if runtime_minutes and runtime_minutes > 0:
                            logger.debug(f"Using cached season {season_num} avg runtime for {media.item.title}: {runtime_minutes}min")
                            break

        # FOURTH: Use industry standard fallback
        if not runtime_minutes or runtime_minutes <= 0:
            if media.item.source == "tmdb":
                runtime_minutes = 30
            elif media.item.source == "mal":
                runtime_minutes = 23
            else:
                runtime_minutes = 30
            logger.debug(f"Using fallback runtime for {media.item.title}: {runtime_minutes}min")
        return runtime_minutes

    def _end_date_for_sort(media):
        # Prefer aggregated_end_date when present, else media.end_date
        return getattr(media, "aggregated_end_date", None) or getattr(media, "end_date", None) or getattr(media, "progressed_at", None) or getattr(media, "created_at", None)

    def _effective_max_progress(media):
        """Prefer annotated max_progress; fallback to DB episodes to avoid negatives."""
        annotated = getattr(media, "max_progress", 0) or 0
        if annotated <= 0 or annotated < media.progress:
            total_from_db = 0
            # Use prefetched seasons/episodes when available
            if hasattr(media, "seasons"):
                for season in media.seasons.all():
                    if getattr(season.item, "season_number", 0) and hasattr(season, "episodes"):
                        max_ep_num = 0
                        for ep in season.episodes.all():
                            ep_num = getattr(ep.item, "episode_number", 0) or 0
                            max_ep_num = max(max_ep_num, ep_num)
                        total_from_db += max_ep_num
            return max(annotated, total_from_db)
        return annotated

    # Cache provider metadata lookups per (source, type, id)
    RELEASE_SYNC_TTL_SECONDS = 3600

    def _release_sync_cache_key(media):
        return f"timeleft:release-sync:{media.item.source}:{media.item.media_id}"

    def _refresh_release_metadata(media):
        if media.item.source == Sources.MANUAL.value:
            return

        cache_key = _release_sync_cache_key(media)
        if not cache.add(cache_key, True, RELEASE_SYNC_TTL_SECONDS):
            return

        try:
            media.item.fetch_releases(delay=False)
        except Exception:
            logger.exception("Failed to refresh release metadata for %s", media.item)
            return

        BasicMedia.objects.annotate_max_progress([media], MediaTypes.TV.value)

    # Explicit bucketing for deterministic grouping
    active_statuses = {Status.IN_PROGRESS.value, Status.PLANNING.value, Status.PAUSED.value}
    group_active = []           # episodes_left > 0 and status in active_statuses
    group_inprog_zero = []      # status == IN_PROGRESS and episodes_left == 0
    group_completed = []        # status == COMPLETED and episodes_left == 0
    group_dropped = []          # status == DROPPED
    group_tail = []             # everything else (unreleased/unknown)

    for media in media_list:
        # Compute effective episodes_left
        if not hasattr(media, "max_progress"):
            group_tail.append(media)
            continue

        annotated_max = getattr(media, "max_progress", None)
        status = getattr(media, "status", Status.IN_PROGRESS.value)

        should_refresh_release_data = (
            (annotated_max is None and status in active_statuses)
            or (annotated_max is not None and annotated_max < media.progress)
            or (
                status in active_statuses
                and annotated_max is not None
                and annotated_max == media.progress
            )
        )

        if should_refresh_release_data:
            _refresh_release_metadata(media)
            annotated_max = getattr(media, "max_progress", None)

        fallback_max = _effective_max_progress(media) or 0

        if annotated_max is None:
            effective_max = max(fallback_max, media.progress)
        else:
            effective_max = max(annotated_max, fallback_max)

        media.max_progress = effective_max
        episodes_left = effective_max - media.progress
        episodes_left = max(episodes_left, 0)

        # Debug shows that should have episodes left but show 0
        if media.progress > 0 and episodes_left == 0 and media.item.title in ["Taskmaster", "Rent-a-Girlfriend", "The Last of Us"]:
            logger.debug(f"DEBUG 0 episodes: {media.item.title} - progress={media.progress}, max_progress={effective_max}, episodes_left={episodes_left}")

        status = getattr(media, "status", Status.IN_PROGRESS.value)

        if status == Status.DROPPED.value:
            group_dropped.append(media)
            continue

        if episodes_left == 0 and status == Status.IN_PROGRESS.value:
            group_inprog_zero.append(media)
            continue

        if episodes_left == 0 and status == Status.COMPLETED.value:
            group_completed.append(media)
            continue

        if episodes_left > 0 and status in active_statuses:
            group_active.append((media, episodes_left))
            continue

        group_tail.append(media)

    # Sort each group
    # 1) Active by least total minutes left
    def _active_key(entry):
        media, episodes_left = entry
        runtime = _calc_runtime_minutes(media)
        if not runtime or runtime <= 0:
            runtime = 30  # Ensure fallback is used
        total = episodes_left * runtime
        # Store the display values using non-property attributes
        media.episodes_left_display = episodes_left
        if total > 0:
            hours = int(total // 60)
            minutes = int(total % 60)
            if hours > 0:
                media.time_left_display = f"{hours}h {minutes}m"
            else:
                media.time_left_display = f"{minutes}m"
        else:
            media.time_left_display = f"{episodes_left} ep" if episodes_left > 0 else "-"
        logger.debug(f"Active: {media.item.title} - {episodes_left} eps × {runtime}min = {total}min ({media.time_left_display})")
        return (total, media.item.title.lower())
    group_active_sorted = [m for (m, _) in sorted(group_active, key=_active_key)]

    # 2) In-Progress caught-up by newest end_date
    for m in group_inprog_zero:
        m.episodes_left_display = 0
        m.time_left_display = "0m"
    group_inprog_zero_sorted = sorted(
        group_inprog_zero,
        key=lambda m: (-( _end_date_for_sort(m).timestamp() if _end_date_for_sort(m) else float("-inf") ), m.item.title.lower()),
    )

    # 3) Completed by newest end_date
    for m in group_completed:
        m.episodes_left_display = 0
        m.time_left_display = "0m"
    group_completed_sorted = sorted(
        group_completed,
        key=lambda m: (-( _end_date_for_sort(m).timestamp() if _end_date_for_sort(m) else float("-inf") ), m.item.title.lower()),
    )

    # 4) Dropped - show remaining content (sorted by least time left)
    for m in group_dropped:
        # Debug logging for first few dropped shows
        if not hasattr(m, "_debug_logged"):
            m._debug_logged = True
            logger.debug(f"Dropped show: {m.item.title} - progress={m.progress}, max_progress={getattr(m, 'max_progress', 'MISSING')}, hasattr={hasattr(m, 'max_progress')}")

        # Calculate episodes remaining (not watched)
        if hasattr(m, "max_progress") and hasattr(m, "progress") and m.max_progress > 0:
            episodes_left = m.max_progress - m.progress
            episodes_left = max(episodes_left, 0)
            m.episodes_left_display = episodes_left

            if episodes_left > 0:
                runtime = _calc_runtime_minutes(m)
                total = episodes_left * runtime
                hours = int(total // 60)
                minutes = int(total % 60)
                if hours > 0:
                    m.time_left_display = f"{hours}h {minutes}m"
                else:
                    m.time_left_display = f"{minutes}m"
                logger.debug(f"Dropped: {m.item.title} - {episodes_left} eps left × {runtime}min = {total}min ({m.time_left_display})")
            else:
                m.time_left_display = "0m"
        else:
            # No max_progress data - show as unknown
            logger.debug(f"Dropped show NO DATA: {m.item.title} - Setting '-' display")
            m.episodes_left_display = 0
            m.time_left_display = "-"

    # Sort dropped by least time left (ascending), then by title
    group_dropped_sorted = sorted(
        group_dropped,
        key=lambda m: (m.episodes_left_display * _calc_runtime_minutes(m), m.item.title.lower()),
    )

    # 5) Tail (unreleased/unknown) - set display values
    for m in group_tail:
        m.episodes_left_display = 0
        m.time_left_display = "-"

    sorted_list = (
        group_active_sorted
        + group_inprog_zero_sorted
        + group_completed_sorted
        + group_dropped_sorted
        + group_tail
    )
    logger.debug(
        "DEBUG: Group counts -> active: %d, inprog_zero: %d, completed: %d, dropped: %d, tail: %d",
        len(group_active_sorted), len(group_inprog_zero_sorted), len(group_completed_sorted), len(group_dropped_sorted), len(group_tail),
    )

    # Log first 10 items for debugging
    logger.debug("DEBUG: First 10 sorted shows:")
    for i, media in enumerate(sorted_list[:10]):
        episodes_left = media.max_progress - media.progress if hasattr(media, "max_progress") else 0
        logger.debug(f"  {i+1}. {media.item.title} - Episodes left: {episodes_left}, Status: {getattr(media, 'status', 'Unknown')}")

    if direction == "desc":
        return list(reversed(sorted_list))

    return sorted_list


def _identify_predefined_range(start_date, end_date):
    if start_date is None and end_date is None:
        return "All Time"

    if not start_date or not end_date:
        return None

    # Use timezone.localdate to avoid off-by-one when converting aware datetimes
    # (localtime(...).date() can shift the date if the aware datetime is at UTC midnight)
    local_start = timezone.localdate(start_date)
    local_end = timezone.localdate(end_date)
    today = timezone.localdate()

    if local_start == today and local_end == today:
        return "Today"

    yesterday = today - timedelta(days=1)
    if local_start == yesterday and local_end == yesterday:
        return "Yesterday"

    monday = today - timedelta(days=today.weekday())
    if local_start == monday and local_end == today:
        return "This Week"

    if local_start == today - timedelta(days=6) and local_end == today:
        return "Last 7 Days"

    month_start = today.replace(day=1)
    if local_start == month_start and local_end == today:
        return "This Month"

    if local_start == today - timedelta(days=29) and local_end == today:
        return "Last 30 Days"

    if local_start == today - timedelta(days=89) and local_end == today:
        return "Last 90 Days"

    year_start = today.replace(month=1, day=1)
    if local_start == year_start and local_end == today:
        return "This Year"

    six_months_start = _adjust_month_delta(today, months=6)
    if _dates_close(local_start, six_months_start) and local_end == today:
        return "Last 6 Months"

    twelve_months_start = _adjust_month_delta(today, months=12)
    if _dates_close(local_start, twelve_months_start) and local_end == today:
        return "Last 12 Months"

    return None


def _get_predefined_range_date_strings(range_name, today, timeformat):
    if range_name == "All Time":
        return "all", "all"

    start_date = None
    end_date = today

    if range_name == "Today":
        start_date = today
    elif range_name == "Yesterday":
        start_date = today - timedelta(days=1)
        end_date = start_date
    elif range_name == "This Week":
        start_date = today - timedelta(days=today.weekday())
    elif range_name == "Last 7 Days":
        start_date = today - timedelta(days=6)
    elif range_name == "This Month":
        start_date = today.replace(day=1)
    elif range_name == "Last 30 Days":
        start_date = today - timedelta(days=29)
    elif range_name == "Last 90 Days":
        start_date = today - timedelta(days=89)
    elif range_name == "This Year":
        start_date = today.replace(month=1, day=1)
    elif range_name == "Last 6 Months":
        start_date = _adjust_month_delta(today, months=6)
    elif range_name == "Last 12 Months":
        start_date = _adjust_month_delta(today, months=12)

    if start_date is None:
        return None, None

    return start_date.strftime(timeformat), end_date.strftime(timeformat)


def _adjust_month_delta(reference_date, months):
    candidate = reference_date - relativedelta(months=months)
    if candidate.day != reference_date.day:
        candidate = (candidate.replace(day=1) + relativedelta(months=1)) - timedelta(days=1)
    return candidate


def _dates_close(date_one, date_two, tolerance_days=1):
    return abs((date_one - date_two).days) <= tolerance_days
