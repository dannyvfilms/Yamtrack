import logging
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import EmptyPage, Paginator
from django.db import IntegrityError, models
from django.db.models import prefetch_related_objects
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.timezone import datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from app import cache_utils, config, helpers, history_cache, history_processor
from app import statistics as stats
from app.forms import EpisodeForm, ManualItemForm, get_form_class
from app.models import (
    TV,
    Album,
    Artist,
    BasicMedia,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Music,
    Season,
    Sources,
    Status,
    Track,
)
from app.providers import manual, services, tmdb
from app.templatetags import app_tags
from users.models import HomeSortChoices, MediaSortChoices, MediaStatusChoices
from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)


@require_GET
def home(request):
    """Home page with media items in progress."""
    sort_by = request.user.update_preference("home_sort", request.GET.get("sort"))
    media_type_to_load = request.GET.get("load_media_type")
    items_limit = 14

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

    context = {
        "user": request.user,
        "list_by_type": list_by_type,
        "current_sort": sort_by,
        "sort_choices": HomeSortChoices.choices,
        "items_limit": items_limit,
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
    search_query = request.GET.get("search", "")
    page = request.GET.get("page", 1)

    # Prepare status filter for database query
    if not status_filter:
        status_filter = MediaStatusChoices.ALL

    # Get media list with filters applied
    media_queryset = BasicMedia.objects.get_media_list(
        user=request.user,
        media_type=media_type,
        status_filter=status_filter,
        sort_filter=sort_filter,
        search=search_query,
        direction=direction,
    )

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
            logger.debug(f"DEBUG: Annotated max_progress for all media")
            
            # Apply time_left sorting
            media_list = _sort_tv_media_by_time_left(media_list, direction)
            logger.debug(f"DEBUG: Applied time_left sorting")
            
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
            episodes_left = media.max_progress - media.progress if hasattr(media, 'max_progress') else 0
            logger.debug(f"  {i+1}. {media.item.title} - Episodes left: {episodes_left}, Status: {getattr(media, 'status', 'Unknown')}")
        
        # Additional debug info for pagination issues
        logger.debug(f"DEBUG: Page {page} pagination info - has_next: {media_page.has_next()}, next_page: {media_page.next_page_number() if media_page.has_next() else 'None'}")
        if hasattr(media_page, 'has_previous') and media_page.has_previous():
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
        "sort_choices": MediaSortChoices.choices,
        "status_choices": MediaStatusChoices.choices,
    }

    # Handle HTMX requests for partial updates
    if request.headers.get("HX-Request"):
        # Changing from empty list to a status with items
        if request.headers.get("HX-Target") == "empty_list":
            response = HttpResponse()
            response["HX-Redirect"] = reverse("medialist", args=[media_type])
            return response
        
        # Check if this is a pagination request (has page parameter and is not the first page)
        is_pagination = request.GET.get("page") and int(request.GET.get("page", 1)) > 1
        
        if layout == "grid":
            template_name = "app/components/media_grid_items.html"
        else:
            if is_pagination:
                # For pagination, we need to return only the table rows, not the full template
                # Return only the table rows without headers
                context["is_pagination"] = True
                template_name = "app/components/media_table_items.html"
            else:
                context["is_pagination"] = False
                template_name = "app/components/media_table_items.html"
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
                request, track_data["results"]
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


@require_GET
def media_details(
    request, source, media_type, media_id, title
):  # noqa: ARG001 title for URL
    """Return the details page for a media item."""
    media_metadata = services.get_media_metadata(media_type, media_id, source)
    user_medias = BasicMedia.objects.filter_media_prefetch(
        request.user,
        media_id,
        media_type,
        source,
    )
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

    # Enrich related items with user tracking data
    if media_metadata.get("related"):
        for section_name, related_items in media_metadata["related"].items():
            if related_items:
                media_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                    )
                )

    # For music tracks, get linked artist and album for navigation
    music_artist = None
    music_album = None
    if media_type == MediaTypes.MUSIC.value and current_instance:
        music_artist = getattr(current_instance, "artist", None)
        music_album = getattr(current_instance, "album", None)

    context = {
        "user": request.user,
        "media": media_metadata,
        "media_type": media_type,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "music_artist": music_artist,
        "music_album": music_album,
    }
    return render(request, "app/media_details.html", context)


@require_GET
def season_details(
    request, source, media_id, title, season_number
):  # noqa: ARG001 For URL
    """Return the details page for a season."""
    tv_with_seasons_metadata = services.get_media_metadata(
        "tv_with_seasons",
        media_id,
        source,
        [season_number],
    )
    season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

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
    if season_metadata.get("related"):
        for section_name, related_items in season_metadata["related"].items():
            if related_items:
                season_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                    )
                )

    context = {
        "user": request.user,
        "media": season_metadata,
        "tv": tv_with_seasons_metadata,
        "media_type": MediaTypes.SEASON.value,
        "user_medias": user_medias,
        "current_instance": current_instance,
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


@require_GET
def track_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
):
    """Return the tracking form for a media item."""
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
        
        item, created = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
                "runtime_minutes": runtime_minutes,
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
        if needs_save:
            item.save()
        model = apps.get_model(app_label="app", model_name=media_type)
        instance = model(item=item, user=request.user)

        # For music tracks, create/link Artist and Album
        if media_type == MediaTypes.MUSIC.value:
            artist_instance = None
            album_instance = None

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
            
            if album_id and album_title:
                album_instance, created = Album.objects.get_or_create(
                    musicbrainz_release_id=album_id,
                    defaults={
                        "title": album_title,
                        "artist": artist_instance,
                        "image": image_url,
                    },
                )
                # Update album image if it's missing
                if not created and image_url and image_url != settings.IMG_NONE:
                    if not album_instance.image or album_instance.image == settings.IMG_NONE:
                        album_instance.image = image_url
                        album_instance.save(update_fields=["image"])
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

        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                "title": tv_with_seasons_metadata["title"],
                "image": season_metadata["image"],
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
    user_medias = BasicMedia.objects.filter_media(
        request.user,
        media_id,
        media_type,
        source,
        season_number=season_number,
        episode_number=episode_number,
    )

    total_medias = user_medias.count()
    timeline_entries = []
    for index, media in enumerate(user_medias, start=1):
        if history := media.history.all():
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

        historical_model.objects.get(
            history_id=history_id,
            history_user=request.user,
        ).delete()

        logger.info(
            "Deleted history record %s",
            str(history_id),
        )

        # Return empty 200 response - the element will be removed by HTMX
        return HttpResponse()

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
    history_days_all = history_cache.get_history_days(request.user)

    paginator = Paginator(history_days_all, history_cache.HISTORY_DAYS_PER_PAGE)

    if paginator.count == 0:
        page_obj = None
        history_days = []
        current_page = 1
    else:
        try:
            page_number = int(request.GET.get("page", 1))
        except (TypeError, ValueError):
            page_number = 1

        try:
            page_obj = paginator.page(page_number)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)

        history_days = page_obj.object_list
        current_page = page_obj.number

    context = {
        "user": request.user,
        "history_days": history_days,
        "page_obj": page_obj,
        "current_page": current_page,
        "total_pages": paginator.num_pages,
        "total_days": paginator.count,
        "days_per_page": paginator.per_page,
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
        )
        logger.info("Created album %s from MusicBrainz", album.title)
    else:
        # Update album image if it's missing or placeholder
        new_image = release_data.get("image", "")
        if new_image and new_image != settings.IMG_NONE and (not album.image or album.image == settings.IMG_NONE):
            album.image = new_image
            album.save(update_fields=["image"])
            logger.info("Updated album %s image", album.title)

    return redirect("album_detail", album_id=album.id)


@require_GET
def artist_detail(request, artist_id):
    """Return the detail page for a music artist."""
    from django.db.models import Count, Min, Max
    from django.shortcuts import get_object_or_404
    from app.services.music import (
        needs_discography_sync,
        sync_artist_discography,
        get_artist_hero_image,
        prefetch_album_covers,
    )
    from app.providers import musicbrainz
    from app.models import ArtistTracker

    artist = get_object_or_404(Artist, id=artist_id)

    # Sync discography from MusicBrainz if needed (like TV populates seasons from TMDB)
    if needs_discography_sync(artist):
        sync_artist_discography(artist)

    # Get ALL albums for this artist (metadata-driven, like Seasons)
    all_albums = Album.objects.filter(artist=artist).annotate(
        library_track_count=Count(
            "music_entries",
            filter=models.Q(music_entries__user=request.user),
        )
    ).order_by("-release_date", "title")

    # Prefetch album covers for albums missing art (up to 20 to respect rate limits)
    prefetch_album_covers(artist, limit=20)

    # Refresh the queryset after prefetch to get updated images
    all_albums = Album.objects.filter(artist=artist).annotate(
        library_track_count=Count(
            "music_entries",
            filter=models.Q(music_entries__user=request.user),
        )
    ).order_by("-release_date", "title")

    # Get total track count for this artist in user's library
    total_tracks = Music.objects.filter(
        user=request.user,
        album__artist=artist,
    ).count()

    # Get hero image from albums (since MusicBrainz doesn't have artist photos)
    hero_image = get_artist_hero_image(artist)

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
    annotation = ""
    
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
            annotation = mb_data.get("annotation", "")
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
        "total_tracks": total_tracks,
        "total_albums": all_albums.count(),
        "hero_image": hero_image,
        "artist_tracker": artist_tracker,
        "history_stats": history_stats,
        "artist_metadata": artist_metadata,
        "genre_chips": genre_chips,
        "annotation": annotation,
        "mb_rating": mb_rating,
        "mb_rating_count": mb_rating_count,
    }
    return render(request, "app/music_artist_detail.html", context)


@require_GET
def album_detail(request, album_id):
    """Return the detail page for a music album."""
    from django.db.models import Min, Max
    from django.shortcuts import get_object_or_404
    from app.models import Track
    from app.providers import musicbrainz
    from app.services.music import ensure_album_has_release_id, album_has_musicbrainz_id

    album = get_object_or_404(Album.objects.select_related("artist"), id=album_id)

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
                
                for track_data in tracks_data:
                    Track.objects.update_or_create(
                        album=album,
                        disc_number=track_data.get("disc_number", 1),
                        track_number=track_data.get("track_number"),
                        defaults={
                            "title": track_data.get("title", "Unknown Track"),
                            "musicbrainz_recording_id": track_data.get("recording_id"),
                            "duration_ms": track_data.get("duration_ms"),
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
    ).select_related("item", "track").prefetch_related("history")
    
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
    }
    return render(request, "app/music_album_detail.html", context)


@require_POST
def sync_artist_discography_view(request, artist_id):
    """Manually trigger discography sync for an artist."""
    from django.shortcuts import get_object_or_404
    from app.services.music import sync_artist_discography

    artist = get_object_or_404(Artist, id=artist_id)
    
    # Force sync
    count = sync_artist_discography(artist, force=True)
    
    messages.success(request, f"Synced {count} albums for {artist.name}")
    
    # Return HX-Refresh header to reload the page
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@require_POST
def artist_tracker_toggle(request, artist_id):
    """Add or remove an artist from the user's library."""
    from django.shortcuts import get_object_or_404
    from app.models import ArtistTracker

    artist = get_object_or_404(Artist, id=artist_id)
    
    tracker, created = ArtistTracker.objects.get_or_create(
        user=request.user,
        artist=artist,
        defaults={"status": Status.IN_PROGRESS.value},
    )
    
    if not created:
        # Already tracked - remove it
        tracker.delete()
        messages.success(request, f"Removed {artist.name} from your library")
    else:
        messages.success(request, f"Added {artist.name} to your library")
    
    # Return HX-Refresh header to reload the page
    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@require_POST
def artist_score_update(request, artist_id):
    """Update the user's score for an artist."""
    from django.shortcuts import get_object_or_404
    from app.models import ArtistTracker

    artist = get_object_or_404(Artist, id=artist_id)
    score = request.POST.get("score")
    
    if score:
        try:
            score = int(score)
            if score < 1 or score > 10:
                score = None
        except ValueError:
            score = None
    else:
        score = None
    
    # Get or create tracker
    tracker, _ = ArtistTracker.objects.get_or_create(
        user=request.user,
        artist=artist,
        defaults={"status": Status.IN_PROGRESS.value},
    )
    
    tracker.score = score
    tracker.save(update_fields=["score"])
    
    return HttpResponse(status=204)


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
                    },
                )
            
            album.tracks_populated = True
            album.save(update_fields=["tracks_populated", "image"])
            
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
    # Set default date range to last year
    timeformat = "%Y-%m-%d"
    today = timezone.localdate()
    one_year_ago = today.replace(year=today.year - 1)

    # Get date parameters with defaults
    start_date_str = request.GET.get("start-date") or one_year_ago.strftime(timeformat)
    end_date_str = request.GET.get("end-date") or today.strftime(timeformat)

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
            )

            # End date should be end of day
            end_date = timezone.make_aware(
                datetime.combine(end_date, datetime.max.time()),
            )

    # Get all user media data in a single operation
    user_media, media_count = stats.get_user_media(
        request.user,
        start_date,
        end_date,
    )

    if not request.user.season_enabled:
        season_key = MediaTypes.SEASON.value
        season_count = media_count.pop(season_key, 0)
        if season_count:
            media_count["total"] = max(media_count.get("total", 0) - season_count, 0)
        user_media.pop(season_key, None)

    # Calculate all statistics from the retrieved data
    media_type_distribution = stats.get_media_type_distribution(
        media_count,
    )
    score_distribution, top_rated = stats.get_score_distribution(user_media)
    status_distribution = stats.get_status_distribution(user_media)
    status_pie_chart_data = stats.get_status_pie_chart_data(
        status_distribution,
    )
    top_played = stats.get_top_played_media(user_media, start_date, end_date)
    
    # Calculate hours and detailed consumption summaries
    minutes_per_media_type = stats.calculate_minutes_per_media_type(
        user_media,
        start_date,
        end_date,
    )
    hours_per_media_type = stats.get_hours_per_media_type(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
    )
    tv_consumption = stats.get_tv_consumption_stats(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
    )
    movie_consumption = stats.get_movie_consumption_stats(
        user_media,
        start_date,
        end_date,
        minutes_per_media_type,
    )

    # Daily hours per media type (used by the Activity History-attached chart)
    daily_hours_by_media_type = stats.get_daily_hours_by_media_type(
        user_media,
        start_date,
        end_date,
    )

    activity_data = stats.get_activity_data(request.user, start_date, end_date)

    selected_range_name = _identify_predefined_range(start_date, end_date)
    show_year_charts = selected_range_name in (None, "All Time")

    context = {
        "user": request.user,
        "start_date": start_date,
        "end_date": end_date,
        "media_count": media_count,
        "activity_data": activity_data,
        "media_type_distribution": media_type_distribution,
        "score_distribution": score_distribution,
        "top_rated": top_rated,
        "top_played": top_played,
        "status_distribution": status_distribution,
        "status_pie_chart_data": status_pie_chart_data,
        "hours_per_media_type": hours_per_media_type,
        "tv_consumption": tv_consumption,
        "movie_consumption": movie_consumption,
        "daily_hours_by_media_type": daily_hours_by_media_type,
        "show_year_charts": show_year_charts,
    }

    return render(request, "app/statistics.html", context)


@require_GET
def service_worker(request):
    """Serve the service worker file from static files."""
    sw_path = settings.STATICFILES_DIRS[0] / "js" / "serviceworker.js"
    with open(sw_path, encoding="utf-8") as sw_file:
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
    from django.core.cache import cache
    from app.statistics import parse_runtime_to_minutes
    import logging
    
    logger = logging.getLogger(__name__)
    
    def _calc_runtime_minutes(media):
        """Best-effort runtime in minutes for a TV show or fallback."""
        runtime_minutes = None
        # FIRST: Check locally stored runtime (but exclude 999999 marker for unknown)
        if hasattr(media, 'item') and media.item.runtime_minutes:
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
                runtime_minutes__isnull=False
            ).exclude(
                runtime_minutes=999999
            ).values_list('runtime_minutes', flat=True)
            
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
        return getattr(media, 'aggregated_end_date', None) or getattr(media, 'end_date', None) or getattr(media, 'progressed_at', None) or getattr(media, 'created_at', None)

    def _effective_max_progress(media):
        """Prefer annotated max_progress; fallback to DB episodes to avoid negatives."""
        annotated = getattr(media, 'max_progress', 0) or 0
        if annotated <= 0 or annotated < media.progress:
            total_from_db = 0
            # Use prefetched seasons/episodes when available
            if hasattr(media, 'seasons'):
                for season in media.seasons.all():
                    if getattr(season.item, 'season_number', 0) and hasattr(season, 'episodes'):
                        max_ep_num = 0
                        for ep in season.episodes.all():
                            ep_num = getattr(ep.item, 'episode_number', 0) or 0
                            if ep_num > max_ep_num:
                                max_ep_num = ep_num
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
        except Exception:  # noqa: BLE001 - log and continue
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
        if not hasattr(media, 'max_progress'):
            group_tail.append(media)
            continue

        annotated_max = getattr(media, 'max_progress', None)
        status = getattr(media, 'status', Status.IN_PROGRESS.value)

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
            annotated_max = getattr(media, 'max_progress', None)

        fallback_max = _effective_max_progress(media) or 0

        if annotated_max is None:
            effective_max = max(fallback_max, media.progress)
        else:
            effective_max = max(annotated_max, fallback_max)

        media.max_progress = effective_max
        episodes_left = effective_max - media.progress
        if episodes_left < 0:
            episodes_left = 0
        
        # Debug shows that should have episodes left but show 0
        if media.progress > 0 and episodes_left == 0 and media.item.title in ["Taskmaster", "Rent-a-Girlfriend", "The Last of Us"]:
            logger.debug(f"DEBUG 0 episodes: {media.item.title} - progress={media.progress}, max_progress={effective_max}, episodes_left={episodes_left}")
        
        status = getattr(media, 'status', Status.IN_PROGRESS.value)

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
        key=lambda m: (-( _end_date_for_sort(m).timestamp() if _end_date_for_sort(m) else float('-inf') ), m.item.title.lower()),
    )

    # 3) Completed by newest end_date
    for m in group_completed:
        m.episodes_left_display = 0
        m.time_left_display = "0m"
    group_completed_sorted = sorted(
        group_completed,
        key=lambda m: (-( _end_date_for_sort(m).timestamp() if _end_date_for_sort(m) else float('-inf') ), m.item.title.lower()),
    )

    # 4) Dropped - show remaining content (sorted by least time left)
    for m in group_dropped:
        # Debug logging for first few dropped shows
        if not hasattr(m, '_debug_logged'):
            m._debug_logged = True
            logger.debug(f"Dropped show: {m.item.title} - progress={m.progress}, max_progress={getattr(m, 'max_progress', 'MISSING')}, hasattr={hasattr(m, 'max_progress')}")
        
        # Calculate episodes remaining (not watched)
        if hasattr(m, 'max_progress') and hasattr(m, 'progress') and m.max_progress > 0:
            episodes_left = m.max_progress - m.progress
            if episodes_left < 0:
                episodes_left = 0
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
        len(group_active_sorted), len(group_inprog_zero_sorted), len(group_completed_sorted), len(group_dropped_sorted), len(group_tail)
    )
    
    # Log first 10 items for debugging
    logger.debug("DEBUG: First 10 sorted shows:")
    for i, media in enumerate(sorted_list[:10]):
        episodes_left = media.max_progress - media.progress if hasattr(media, 'max_progress') else 0
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


def _adjust_month_delta(reference_date, months):
    candidate = reference_date - relativedelta(months=months)
    if candidate.day != reference_date.day:
        candidate = (candidate.replace(day=1) + relativedelta(months=1)) - timedelta(days=1)
    return candidate


def _dates_close(date_one, date_two, tolerance_days=1):
    return abs((date_one - date_two).days) <= tolerance_days
