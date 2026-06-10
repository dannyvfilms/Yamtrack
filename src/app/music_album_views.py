import json
import logging

from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_POST

from app.discover import tab_cache as discover_tab_cache
from app.forms import AlbumTrackerForm
from app.models import (
    Album,
    AlbumTracker,
    Artist,
    CollectionEntry,
    Item,
    MediaTypes,
    Music,
    Sources,
    Status,
    Track,
)
from app.music_views import (
    _build_music_album_activity_subtitle,
    _music_activity_date_range,
    _music_album_detail_url,
    _render_music_tracker_modal,
)
from app.providers import musicbrainz
from app.services import bulk_music_tracking
from app.services.music import ensure_album_has_release_id
from app.track_modal_views import _track_modal_release_date_shortcut

logger = logging.getLogger(__name__)


def album_track_modal(request, album_id):
    """Return the shared tracking form modal for a music album."""
    album = get_object_or_404(Album, id=album_id)
    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()
    form = AlbumTrackerForm(
        instance=tracker,
        initial={"album_id": album.id},
        user=request.user,
    )
    return _render_music_tracker_modal(
        request,
        title=album.title,
        tracker=tracker,
        form=form,
        save_url=reverse("album_save"),
        delete_url=reverse("album_delete"),
        release_date_shortcut=_track_modal_release_date_shortcut(album.release_date),
        bulk_domain=bulk_music_tracking.build_album_play_domain(request.user, album),
    )


@require_POST
def album_save(request):
    """Save an album tracker - mirrors artist_save."""
    album_id = request.POST.get("album_id")
    album = get_object_or_404(Album, id=album_id)
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.MUSIC.value,
    )

    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()

    form = AlbumTrackerForm(request.POST, instance=tracker, user=request.user)
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
    return redirect(_music_album_detail_url(album))


@require_POST
def album_delete(request):
    """Delete an album tracker - mirrors artist_delete."""
    album_id = request.POST.get("album_id")
    album = get_object_or_404(Album, id=album_id)
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.MUSIC.value,
    )

    tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {album.title} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect(_music_album_detail_url(album))


@require_POST
def song_save(request):
    """Handle adding a listen for a song - mirrors episode_save for episodes."""
    recording_id = request.POST.get("recording_id")
    album_id = request.POST.get("album_id")
    track_id = request.POST.get("track_id")
    end_date_str = request.POST.get("end_date")
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.MUSIC.value,
    )

    end_date = None
    if end_date_str:
        end_date = parse_datetime(end_date_str)
        if end_date:
            if timezone.is_naive(end_date):
                end_date = timezone.make_aware(end_date)
        else:
            parsed_date = parse_date(end_date_str)
            if parsed_date:
                end_date = timezone.make_aware(
                    timezone.datetime.combine(parsed_date, timezone.datetime.min.time()),
                )

    album = get_object_or_404(Album, id=album_id)
    track = get_object_or_404(Track, id=track_id) if track_id else None

    existing_music = Music.objects.filter(
        user=request.user,
        album=album,
        track=track,
    ).first()

    runtime_minutes = None
    if track and track.duration_ms:
        runtime_minutes = track.duration_ms // 60000  # Convert ms to minutes

    if existing_music:
        existing_music.end_date = end_date
        existing_music.save()

        if runtime_minutes and existing_music.item and not existing_music.item.runtime_minutes:
            existing_music.item.runtime_minutes = runtime_minutes
            existing_music.item.save(update_fields=["runtime_minutes"])

        messages.success(request, f"Added listen for {track.title if track else 'track'}")
    else:
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
            if not created and not item.runtime_minutes and runtime_minutes:
                item.runtime_minutes = runtime_minutes
                item.save(update_fields=["runtime_minutes"])
        else:
            item, created = Item.objects.get_or_create(
                media_id=f"track_{track_id}",
                source=Sources.MUSICBRAINZ.value,
                media_type=MediaTypes.MUSIC.value,
                defaults=item_defaults,
            )
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

    if request.headers.get("HX-Request"):
        music = (
            Music.objects.filter(
                user=request.user,
                album=album,
                track=track,
            )
            .select_related("item", "track", "album")
            .order_by("-created_at")
            .first()
        )
        if music is None:
            return HttpResponse("Music entry not found", status=404)

        track_data = {
            "track": track,
            "music": music,
            "history": list(music.history.all().order_by("-end_date")),
            "collection_entry": CollectionEntry.objects.filter(
                user=request.user,
                item=music.item,
            )
            .select_related("item")
            .first(),
        }
        user_music_entries = list(
            Music.objects.filter(
                user=request.user,
                album=album,
            ).select_related("item", "track"),
        )
        album_tracker = AlbumTracker.objects.filter(user=request.user, album=album).first()
        first_listened, last_listened, collapse_same_day = _music_activity_date_range(
            user_music_entries,
        )
        music_album_activity_subtitle = _build_music_album_activity_subtitle(
            album,
            album_tracker,
            len(user_music_entries),
            Track.objects.filter(album=album).count(),
            first_listened,
            last_listened,
            collapse_same_day,
        )

        response = HttpResponse()
        response.write(
            render_to_string(
                "app/components/detail_music_track_button.html",
                {
                    "track_data": track_data,
                    "track_button_oob": True,
                },
                request=request,
            ),
        )
        response.write(
            render_to_string(
                "app/components/detail_music_track_history_line.html",
                {
                    "track_data": track_data,
                    "history_oob": True,
                    "user": request.user,
                },
                request=request,
            ),
        )
        if music_album_activity_subtitle:
            response.write(
                render_to_string(
                    "app/components/detail_music_album_activity_subtitle.html",
                    {
                        "album": album,
                        "music_album_activity_subtitle": music_album_activity_subtitle,
                        "subtitle_oob": True,
                        "user": request.user,
                    },
                    request=request,
                ),
            )
        response.write(
            render_to_string(
                "app/components/detail_music_track_actions.html",
                {
                    "track_data": track_data,
                    "track_actions_oob": True,
                    "user": request.user,
                },
                request=request,
            ),
        )
        response["HX-Trigger"] = json.dumps(
            {
                "closeModal": {},
                "showToast": {
                    "message": f"Added listen for {track.title if track else 'track'}.",
                    "type": "success",
                },
            },
        )
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect(_music_album_detail_url(album))


@require_POST
def delete_all_album_plays_view(request, album_id):
    """Delete all music plays (listens) for an album."""
    album = get_object_or_404(Album, id=album_id)

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

    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@require_POST
def delete_all_artist_plays_view(request, artist_id):
    """Delete all music plays (listens) for an artist."""
    artist = get_object_or_404(Artist, id=artist_id)

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

    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


@require_POST
def sync_album_metadata_view(request, album_id):
    """Manually trigger metadata sync for an album."""
    album = get_object_or_404(Album, id=album_id)

    ensure_album_has_release_id(album)

    if album.musicbrainz_release_id:
        try:
            release_data = musicbrainz.get_release(album.musicbrainz_release_id)

            new_image = release_data.get("image", "")
            if new_image and new_image != settings.IMG_NONE:
                album.image = new_image

            if release_data.get("genres"):
                album.genres = release_data.get("genres")

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

    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response
