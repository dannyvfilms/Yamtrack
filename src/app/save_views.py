import json
import logging
from urllib.parse import quote, urlparse
from uuid import uuid4

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_POST

from app import cache_utils, helpers
from app.activity_builders import _build_detail_activity_state
from app.discover import tab_cache as discover_tab_cache
from app.forms import EpisodeForm, get_form_class
from app.models import (
    BasicMedia,
    CollectionEntry,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
)
from app.providers import services
from app.services import metadata_resolution
from app.services.tracking_hydration import ensure_item_metadata
from app.track_modal_views import (
    _render_standard_track_modal,
)

logger = logging.getLogger(__name__)


@require_POST
def media_save(request):
    """Save or update media data to the database."""
    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    identity_media_type = request.POST.get("identity_media_type") or None
    library_media_type = request.POST.get("library_media_type") or None
    season_number = request.POST.get("season_number")
    instance_id = request.POST.get("instance_id")
    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
        identity_media_type=identity_media_type,
    )
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=library_media_type or media_type,
    )

    # Handle percentage conversion for books/comics/manga
    progress_value = request.POST.get("progress")
    if progress_value and media_type in (MediaTypes.BOOK.value, MediaTypes.COMIC.value, MediaTypes.MANGA.value):
        if request.user.book_comic_manga_progress_percentage:
            # Make POST mutable for modification
            mutable_post = request.POST.copy()
            max_progress = None
            item = None

            # Get item to determine max_progress
            if instance_id:
                instance = BasicMedia.objects.get_media(
                    request.user,
                    media_type,
                    instance_id,
                )
                if instance:
                    item = instance.item
            else:
                # For new entries, get metadata first to get/create item
                metadata = services.get_media_metadata(
                    media_type,
                    media_id,
                    source,
                    [season_number],
                )
                if media_type == MediaTypes.BOOK.value:
                    number_of_pages = metadata.get("max_progress") or metadata.get("details", {}).get("number_of_pages")
                else:
                    number_of_pages = None
                item, _ = Item.objects.get_or_create(
                    media_id=media_id,
                    source=source,
                    media_type=tracking_media_type,
                    season_number=season_number,
                    defaults={
                        **Item.title_fields_from_metadata(metadata),
                        "library_media_type": (
                            library_media_type
                            or metadata.get("library_media_type")
                            or media_type
                        ),
                        "image": metadata["image"],
                        "number_of_pages": number_of_pages,
                    },
                )

            if item:
                if media_type == MediaTypes.BOOK.value:
                    max_progress = item.number_of_pages
                    if not max_progress:
                        # Try to fetch from metadata
                        try:
                            metadata = services.get_media_metadata(
                                item.media_type,
                                item.media_id,
                                item.source,
                            )
                            number_of_pages = metadata.get("max_progress") or metadata.get("details", {}).get("number_of_pages")
                            if number_of_pages:
                                item.number_of_pages = number_of_pages
                                item.save(update_fields=["number_of_pages"])
                                max_progress = number_of_pages
                        except Exception:
                            pass
                else:
                    # For comics and manga, need to get max_progress from events
                    from app.models import Manga, Comic
                    model_class = Manga if media_type == MediaTypes.MANGA.value else Comic
                    media_list = list(model_class.objects.filter(user=request.user, item=item).select_related("item"))
                    if media_list:
                        BasicMedia.objects.annotate_max_progress(media_list, media_type)
                        if hasattr(media_list[0], "max_progress"):
                            max_progress = media_list[0].max_progress

                if max_progress:
                    try:
                        percentage = float(progress_value)
                        converted_progress = round((percentage / 100) * max_progress)
                        mutable_post["progress"] = str(converted_progress)
                        request.POST = mutable_post
                    except (ValueError, TypeError):
                        pass

    if instance_id:
        instance = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    else:
        hydrated = ensure_item_metadata(
            request.user,
            media_type,
            media_id,
            source,
            season_number,
            identity_media_type=identity_media_type,
            library_media_type=library_media_type,
        )
        model = apps.get_model(app_label="app", model_name=tracking_media_type)
        instance = model(item=hydrated.item, user=request.user)

        if tracking_media_type == MediaTypes.MUSIC.value:
            instance.artist = hydrated.artist
            instance.album = hydrated.album
            instance.track = hydrated.track
        if tracking_media_type == MediaTypes.PODCAST.value and hydrated.podcast_show is not None:
            instance.show = hydrated.podcast_show

    # Validate the form and save the instance if it's valid
    form_class = get_form_class(tracking_media_type)
    form = form_class(request.POST, instance=instance, user=request.user)
    media = instance
    is_htmx = bool(request.headers.get("HX-Request"))
    track_form_id = request.POST.get("track_form_id") or (
        f"track-form-{uuid4().hex}"
    )
    return_url = quote(
        request.GET.get("next") or request.POST.get("return_url") or "",
        safe="",
    )
    home_row_id = request.GET.get("home_row_id") or ""
    old_status = getattr(instance, "status", None) if instance_id else None
    action_verb = "Added" if not instance_id else "Updated"
    if form.is_valid():
        media = form.save()
        BasicMedia.objects.annotate_max_progress([media], media_type)
        image_url = form.cleaned_data.get("image_url")
        if image_url and media.item.image != image_url:
            media.item.image = image_url
            media.item.save(update_fields=["image"])
        logger.info("%s saved successfully.", media)
        display_title = (
            media.item.get_display_title(request.user)
            if hasattr(media.item, "get_display_title")
            else media.item.title
        ) or "item"
        if is_htmx:
            user_medias = list(
                media.__class__.objects.filter(user=request.user, item=media.item).select_related(
                    "item",
                ),
            )
            play_stats, activity_subtitle = _build_detail_activity_state(
                media_type,
                {"max_progress": getattr(media, "max_progress", None)},
                current_instance=media,
                user_medias=user_medias,
                public_view=False,
            )
            response = render(
                request,
                "app/components/detail_track_action.html",
                {
                    "media": media.item,
                    "current_instance": media,
                    "return_url": return_url,
                    "track_action_update": True,
                },
            )
            activity_subtitle_response = render(
                request,
                "app/components/detail_activity_subtitle_slot.html",
                {
                    "media": media.item,
                    "media_type": media_type,
                    "current_instance": media,
                    "activity_subtitle": activity_subtitle,
                    "play_stats": play_stats,
                    "user": request.user,
                    "activity_subtitle_slot_oob": True,
                },
            )
            score_chip_response = render(
                request,
                "app/components/detail_score_chip_slot.html",
                {
                    "media": media.item,
                    "current_instance": media,
                    "media_type": media_type,
                    "user": request.user,
                    "user_medias": [media],
                    "public_view": False,
                    "csrf_token": request.META.get("CSRF_COOKIE", ""),
                    "score_chip_slot_oob": True,
                },
            )
            card_rating_response = render(
                request,
                "app/components/media_card_rating_oob.html",
                {
                    "media_instance_id": media.id,
                    "rating_value": media.formatted_score,
                    "user": request.user,
                },
            )
            response.write(activity_subtitle_response.content.decode())
            response.write(score_chip_response.content.decode())
            response.write(card_rating_response.content.decode())
            htmx_trigger = {
                "closeModal": {"formId": track_form_id},
                "showToast": {
                    "message": f"{action_verb} {display_title}.",
                    "type": "success",
                },
            }
            if home_row_id and instance_id and old_status != media.status:
                htmx_trigger["refreshHomeRow"] = {"rowId": int(home_row_id)}
            response["HX-Trigger"] = json.dumps(
                htmx_trigger,
            )
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
            return response
        messages.success(request, f"{action_verb} {display_title}.")
    else:
        logger.error(form.errors.as_json())
        if is_htmx:
            modal_response = _render_standard_track_modal(
                request,
                source,
                media_type,
                media_id,
                season_number=season_number,
                form_override=form,
                track_form_id=track_form_id,
                return_url=return_url,
                track_action_update=True,
            )
            response = render(
                request,
                "app/components/detail_track_action.html",
                {
                    "media": media.item,
                    "current_instance": media,
                    "return_url": return_url,
                    "track_open": True,
                    "track_modal_content": modal_response.content.decode(),
                    "track_action_update": True,
                },
            )
            response["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"
            return response
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
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=media_type,
    )
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

    redirect_response = helpers.redirect_back(request)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": redirect_response.url})
    return redirect_response


def _write_episode_save_oob(
    response,
    request,
    *,
    episode,
    related_season,
    media_id,
    source,
    season_number,
    episode_number,
    next_path,
):
    """Write the OOB fragments for an episode watch/drop response.

    The season-details episode list and the standalone episode page render
    different markup (small round button + history line + season-progress spans
    vs. a hero pill button + an always-present rating-chip slot), so each needs
    its own OOB targets. We detect the standalone episode page from the `next`
    path and emit the matching variant; sending the wrong one just no-ops since
    HTMX silently drops OOB swaps with no matching element.
    """
    parsed_next = urlparse(next_path).path
    path_parts = [segment for segment in parsed_next.split("/") if segment]
    is_episode_page = (
        len(path_parts) >= 2
        and path_parts[0] == "details"
        and "episode" in path_parts
    )

    if is_episode_page:
        response.write(
            render_to_string(
                "app/components/detail_episode_hero_track_button.html",
                {
                    "episode": episode,
                    "source": source,
                    "media_id": media_id,
                    "season_number": season_number,
                    "episode_number": episode_number,
                    "track_button_oob": True,
                },
                request=request,
            ),
        )
        response.write(
            render_to_string(
                "app/components/detail_episode_rating_chip.html",
                {
                    "episode": episode,
                    "current_instance": related_season,
                    "user": request.user,
                    "source": source,
                    "media_id": media_id,
                    "season_number": season_number,
                    "episode_number": episode_number,
                    "public_view": False,
                    "rating_chip_oob": True,
                },
                request=request,
            ),
        )
        # Season-progress spans only exist on the season page — nothing to target here.
        return

    response.write(
        render_to_string(
            "app/components/detail_episode_track_button.html",
            {
                "episode": episode,
                "track_button_oob": True,
            },
            request=request,
        ),
    )
    response.write(
        render_to_string(
            "app/components/detail_episode_history_line.html",
            {
                "episode": episode,
                "user": request.user,
                "history_oob": True,
            },
            request=request,
        ),
    )
    response.write(
        f'<span id="season-progress-mobile-{related_season.id}" hx-swap-oob="true" class="text-sm font-medium text-gray-400">Progress: {related_season.completed_episode_count}{f"/{related_season.max_progress}" if related_season.max_progress else ""}</span>',
    )
    response.write(
        f'<span id="season-progress-desktop-{related_season.id}" hx-swap-oob="true" class="text-sm font-medium text-gray-400">Progress: {related_season.completed_episode_count}{f"/{related_season.max_progress}" if related_season.max_progress else ""}</span>',
    )


@require_POST
def episode_save(request):
    """Handle the creation, deletion, and updating of episodes for a season."""
    media_id = request.POST["media_id"]
    season_number = int(request.POST["season_number"])
    episode_number = int(request.POST["episode_number"])
    source = request.POST["source"]
    library_media_type = (request.POST.get("library_media_type") or "").strip()

    next_path = request.GET.get("next") or ""
    if source == Sources.TMDB.value and next_path:
        parsed_next_path = urlparse(next_path).path
        path_parts = [segment for segment in parsed_next_path.split("/") if segment]
        if len(path_parts) >= 2 and path_parts[0] == "details":
            route_source = path_parts[1]
            if route_source in {choice[0] for choice in Sources.choices}:
                source = route_source

    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.TV.value,
    )

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
            source=source,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                **Item.title_fields_from_metadata(tv_with_seasons_metadata),
                "library_media_type": library_media_type,
                "image": season_image,
            },
        )
        if library_media_type and item.library_media_type != library_media_type:
            item.library_media_type = library_media_type
            item.save(update_fields=["library_media_type"])
        related_season = Season.objects.create(
            item=item,
            user=request.user,
            score=None,
            status=Status.IN_PROGRESS.value,
            notes="",
        )

        logger.info("%s did not exist, it was created successfully.", related_season)

    if library_media_type and related_season.item.library_media_type != library_media_type:
        related_season.item.library_media_type = library_media_type
        related_season.item.save(update_fields=["library_media_type"])
    if (
        library_media_type
        and related_season.related_tv.item.library_media_type != library_media_type
    ):
        related_season.related_tv.item.library_media_type = library_media_type
        related_season.related_tv.item.save(update_fields=["library_media_type"])

    related_season.watch(episode_number, form.cleaned_data["end_date"])

    if request.headers.get("HX-Request"):
        episode_history = list(
            Episode.objects.filter(
                related_season=related_season,
                item__media_id=media_id,
                item__source=source,
                item__episode_number=episode_number,
            )
            .select_related("item", "related_season")
            .order_by("-end_date", "-created_at")
        )
        if not episode_history:
            return HttpResponse("Episode not found", status=404)

        episode = episode_history[0]
        episode.history = episode_history
        episode.collection_entry = CollectionEntry.objects.filter(
            item=episode.item,
            user=request.user,
        ).select_related("item").first()

        response = HttpResponse()
        _write_episode_save_oob(
            response,
            request,
            episode=episode,
            related_season=related_season,
            media_id=media_id,
            source=source,
            season_number=season_number,
            episode_number=episode_number,
            next_path=next_path,
        )
        response["HX-Trigger"] = json.dumps(
            {
                "closeModal": {},
                "showToast": {
                    "message": f"Added watch for episode {episode_number}.",
                    "type": "success",
                },
            },
        )
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    return helpers.redirect_back(request)


@require_POST
def episode_drop(request):
    """Mark an episode as dropped — advances progress without adding to watch history."""
    media_id = request.POST["media_id"]
    season_number = int(request.POST["season_number"])
    episode_number = int(request.POST["episode_number"])
    source = request.POST["source"]
    library_media_type = (request.POST.get("library_media_type") or "").strip()

    next_path = request.GET.get("next") or ""
    if source == Sources.TMDB.value and next_path:
        parsed_next_path = urlparse(next_path).path
        path_parts = [segment for segment in parsed_next_path.split("/") if segment]
        if len(path_parts) >= 2 and path_parts[0] == "details":
            route_source = path_parts[1]
            if route_source in {choice[0] for choice in Sources.choices}:
                source = route_source

    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.TV.value,
    )

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
        season_image = season_metadata.get("image") or tv_with_seasons_metadata.get("image")

        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                **Item.title_fields_from_metadata(tv_with_seasons_metadata),
                "library_media_type": library_media_type,
                "image": season_image,
            },
        )
        if library_media_type and item.library_media_type != library_media_type:
            item.library_media_type = library_media_type
            item.save(update_fields=["library_media_type"])
        related_season = Season.objects.create(
            item=item,
            user=request.user,
            score=None,
            status=Status.IN_PROGRESS.value,
            notes="",
        )
        logger.info("%s did not exist, it was created successfully.", related_season)

    if library_media_type and related_season.item.library_media_type != library_media_type:
        related_season.item.library_media_type = library_media_type
        related_season.item.save(update_fields=["library_media_type"])
    if (
        library_media_type
        and related_season.related_tv.item.library_media_type != library_media_type
    ):
        related_season.related_tv.item.library_media_type = library_media_type
        related_season.related_tv.item.save(update_fields=["library_media_type"])

    item = related_season.get_episode_item(episode_number)
    episode_record = Episode.objects.create(
        related_season=related_season,
        item=item,
        end_date=None,
        dropped=True,
    )
    logger.info("%s dropped successfully.", episode_record)
    cache_utils.clear_time_left_cache_for_user(request.user.id)
    cache_utils.clear_media_list_cache_for_user(request.user.id)

    if request.headers.get("HX-Request"):
        episode_history = list(
            Episode.objects.filter(
                related_season=related_season,
                item__media_id=media_id,
                item__source=source,
                item__episode_number=episode_number,
            )
            .select_related("item", "related_season")
            .order_by("-end_date", "-created_at")
        )
        if not episode_history:
            return HttpResponse("Episode not found", status=404)

        episode = episode_history[0]
        episode.history = episode_history
        episode.collection_entry = CollectionEntry.objects.filter(
            item=episode.item,
            user=request.user,
        ).select_related("item").first()

        response = HttpResponse()
        _write_episode_save_oob(
            response,
            request,
            episode=episode,
            related_season=related_season,
            media_id=media_id,
            source=source,
            season_number=season_number,
            episode_number=episode_number,
            next_path=next_path,
        )
        response["HX-Trigger"] = json.dumps(
            {
                "closeModal": {},
                "showToast": {
                    "message": f"Dropped episode {episode_number}.",
                    "type": "success",
                },
            },
        )
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    return helpers.redirect_back(request)


@require_POST
def episode_bulk_save(request):
    """Dispatch a bulk episode play range as a background task and return immediately."""
    from app.tasks import bulk_episode_plays_task  # noqa: PLC0415

    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    fallback_media_type = request.POST.get("library_media_type") or media_type
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=fallback_media_type,
    )

    start_date_str = (request.POST.get("start_date") or "").strip()
    end_date_str = (request.POST.get("end_date") or "").strip()

    if not start_date_str or not end_date_str:
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=422)
            response["HX-Trigger"] = json.dumps({
                "showToast": {
                    "message": "Start and end dates are required.",
                    "type": "error",
                },
            })
            return response
        messages.error(request, "Start and end dates are required.")
        return redirect(request.POST.get("return_url") or "/")

    try:
        first_season_number = int(request.POST["first_season_number"])
        first_episode_number = int(request.POST["first_episode_number"])
        last_season_number = int(request.POST["last_season_number"])
        last_episode_number = int(request.POST["last_episode_number"])
    except (KeyError, ValueError):
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=422)
            response["HX-Trigger"] = json.dumps({
                "showToast": {
                    "message": "Invalid episode range.",
                    "type": "error",
                },
            })
            return response
        messages.error(request, "Invalid episode range.")
        return redirect(request.POST.get("return_url") or "/")

    episode_count = max(int(request.POST.get("episode_count") or 0), 0)
    write_mode = request.POST.get("write_mode", "add")
    distribution_mode = request.POST.get("distribution_mode", "even")
    identity_media_type = request.POST.get("identity_media_type") or None
    library_media_type = request.POST.get("library_media_type") or None

    task = bulk_episode_plays_task.apply_async(
        kwargs={
            "user_id": request.user.id,
            "media_type": media_type,
            "source": source,
            "media_id": media_id,
            "first_season_number": first_season_number,
            "first_episode_number": first_episode_number,
            "last_season_number": last_season_number,
            "last_episode_number": last_episode_number,
            "write_mode": write_mode,
            "distribution_mode": distribution_mode,
            "start_date_str": start_date_str,
            "end_date_str": end_date_str,
            "identity_media_type": identity_media_type,
            "library_media_type": library_media_type,
        },
        priority=settings.CELERY_TASK_PRIORITY_INTERACTIVE,
    )
    logger.info(
        "bulk_episode_plays_task_dispatched task_id=%s user_id=%d media_id=%s",
        task.id,
        request.user.id,
        media_id,
    )

    if request.headers.get("HX-Request"):
        plural = "s" if episode_count != 1 else ""
        response = HttpResponse(status=204)
        response["HX-Trigger"] = json.dumps({
            "closeModal": {},
            "showToast": {
                "message": f"Adding plays to {episode_count} episode{plural}.",
                "type": "info",
            },
        })
        return response

    messages.info(request, f"Adding plays to {episode_count} episodes.")
    return redirect(request.POST.get("return_url") or "/")
