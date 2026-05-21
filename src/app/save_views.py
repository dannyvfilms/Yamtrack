import json
import logging
from urllib.parse import quote, urlparse
from uuid import uuid4

from django.apps import apps
from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from app import helpers
from app.activity_builders import _build_detail_activity_state
from app.discover import tab_cache as discover_tab_cache
from app.forms import BulkEpisodeTrackForm, EpisodeForm, get_form_class
from app.models import (
    BasicMedia,
    CollectionEntry,
    Episode,
    Item,
    MediaTypes,
    PodcastShow,
    Season,
    Sources,
    Status,
)
from app.providers import services
from app.services import bulk_episode_tracking, metadata_resolution
from app.services.tracking_hydration import ensure_item_metadata
from app.track_modal_views import (
    _render_podcast_show_track_modal,
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
            response.write(activity_subtitle_response.content.decode())
            response.write(score_chip_response.content.decode())
            response["HX-Trigger"] = json.dumps(
                {
                    "closeModal": {"formId": track_form_id},
                    "showToast": {
                        "message": f"{action_verb} {display_title}.",
                        "type": "success",
                    },
                },
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


@require_POST
def episode_save(request):
    """Handle the creation, deletion, and updating of episodes for a season."""
    from django.template.loader import render_to_string

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


def _episode_bulk_redirect_url(request, result):
    """Return the full-page destination after a successful bulk episode save."""
    if result.grouped_item and result.grouped_redirect_media_type:
        title = result.grouped_item.get_display_title(request.user) or result.grouped_item.title or "item"
        return reverse(
            "media_details",
            kwargs={
                "source": result.grouped_item.source,
                "media_type": result.grouped_redirect_media_type,
                "media_id": result.grouped_item.media_id,
                "title": slugify(title),
            },
        )

    redirect_response = helpers.redirect_back(request)
    return redirect_response.url


@require_POST
def episode_bulk_save(request):
    """Persist a bulk range of episode plays from track modal tabs."""
    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    fallback_media_type = request.POST.get("library_media_type") or media_type
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=fallback_media_type,
    )

    metadata_item = None
    base_metadata = None
    metadata_resolution_result = None
    podcast_show = None

    if media_type == MediaTypes.PODCAST.value and source == Sources.POCKETCASTS.value:
        podcast_show = PodcastShow.objects.filter(podcast_uuid=media_id).first()
    else:
        item_lookup = {
            "media_id": media_id,
            "source": source,
            "media_type": metadata_resolution.get_tracking_media_type(
                media_type,
                source=source,
                identity_media_type=request.POST.get("identity_media_type") or None,
            ),
        }
        if media_type == MediaTypes.ANIME.value and source in {
            Sources.TMDB.value,
            Sources.TVDB.value,
        }:
            item_lookup["library_media_type"] = MediaTypes.ANIME.value
        metadata_item = Item.objects.filter(**item_lookup).first()

        base_metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
        )
        if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            metadata_resolution_result = metadata_resolution.resolve_detail_metadata(
                request.user,
                item=metadata_item,
                route_media_type=media_type,
                media_id=media_id,
                source=source,
                base_metadata=base_metadata,
            )

    episode_domain = bulk_episode_tracking.build_episode_play_domain(
        request.user,
        media_type,
        source,
        media_id,
        metadata_item=metadata_item,
        base_metadata=base_metadata,
        metadata_resolution_result=metadata_resolution_result,
        podcast_show=podcast_show,
    )
    if not episode_domain:
        messages.error(
            request,
            "Bulk episode tracking is not available for this title.",
        )
        redirect_url = _episode_bulk_redirect_url(
            request,
            bulk_episode_tracking.BulkEpisodePlayResult(
                created_count=0,
                replaced_episode_count=0,
            ),
        )
        if request.headers.get("HX-Request"):
            return HttpResponse(status=400, headers={"HX-Redirect": redirect_url})
        return redirect(redirect_url)

    bulk_form = BulkEpisodeTrackForm(
        request.POST,
        domain=episode_domain,
    )
    if not bulk_form.is_valid():
        if podcast_show is not None:
            return _render_podcast_show_track_modal(
                request,
                podcast_show,
                bulk_form_override=bulk_form,
                initial_active_tab="episode-plays",
            )
        return _render_standard_track_modal(
            request,
            source,
            media_type,
            media_id,
            form_override=None,
            bulk_form_override=bulk_form,
            initial_active_tab="episode-plays",
        )

    result = bulk_episode_tracking.apply_bulk_episode_plays(
        request.user,
        episode_domain,
        selected_episodes=bulk_form.cleaned_data["selected_domain_episodes"],
        write_mode=bulk_form.cleaned_data["write_mode"],
        distribution_mode=bulk_form.cleaned_data["distribution_mode"],
        start_date=bulk_form.cleaned_data.get("start_date"),
        end_date=bulk_form.cleaned_data.get("end_date"),
    )

    action_verb = (
        "Replaced"
        if bulk_form.cleaned_data["write_mode"] == BulkEpisodeTrackForm.WRITE_MODE_REPLACE
        else "Added"
    )
    detail_bits = []
    if result.migrated_flat_anime:
        detail_bits.append("after migrating grouped anime tracking")
    elif result.created_grouped_tracking and result.grouped_item:
        detail_bits.append("after creating grouped anime tracking")
    detail_suffix = f" {' '.join(detail_bits)}" if detail_bits else ""
    messages.success(
        request,
        f"{action_verb} {result.created_count} episode play{'s' if result.created_count != 1 else ''}{detail_suffix}.",
    )

    redirect_url = _episode_bulk_redirect_url(request, result)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": redirect_url})
    return redirect(redirect_url)
