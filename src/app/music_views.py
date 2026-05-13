import logging
from uuid import uuid4

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.db import IntegrityError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from app.discover import tab_cache as discover_tab_cache
from app.forms import BulkEpisodeTrackForm
from app.log_safety import exception_summary
from app.models import (
    Album,
    Artist,
    CollectionEntry,
    Item,
    MediaTypes,
    Music,
    Season,
    Sources,
    Status,
    TV,
    Track,
)
from app.services import bulk_music_tracking
from app.services import music as sync_services
from app.signals import suppress_media_cache_change_signals
from app.templatetags import app_tags

logger = logging.getLogger(__name__)


def _music_artist_detail_url(artist):
    """Return the canonical shared media-details URL for a music artist."""
    return app_tags.music_artist_url(artist)


def _music_album_detail_url(album):
    """Return the canonical shared media-details URL for a music album."""
    return app_tags.music_album_url(album)


def _music_activity_date_range(entries):
    """Return the earliest and latest meaningful activity dates from music entries."""
    start_candidates = []
    end_candidates = []
    for entry in entries:
        if entry.start_date or entry.end_date:
            start_candidates.append(entry.start_date or entry.end_date)
            end_candidates.append(entry.end_date or entry.start_date)

    first_date = min(start_candidates) if start_candidates else None
    last_date = max(end_candidates) if end_candidates else None
    collapse_same_day = bool(
        first_date
        and last_date
        and first_date.date() == last_date.date()
    )
    return first_date, last_date, collapse_same_day


def _build_music_artist_activity_subtitle(
    artist_tracker,
    total_plays,
    first_date,
    last_date,
    collapse_same_day,
):
    """Return the shared subtitle payload for a music artist detail page."""
    if not artist_tracker and not total_plays and not first_date and not last_date:
        return None

    primary_text = None
    if total_plays:
        primary_text = "Played once" if total_plays == 1 else f"Played {total_plays} times"

    return {
        "primary_text": primary_text,
        "date_start": first_date or getattr(artist_tracker, "start_date", None),
        "date_end": last_date or getattr(artist_tracker, "end_date", None),
        "collapse_same_day": collapse_same_day,
    }


def _build_music_album_activity_subtitle(
    album,
    album_tracker,
    library_track_count,
    total_tracks,
    first_date,
    last_date,
    collapse_same_day,
):
    """Return the shared subtitle payload for a music album detail page."""
    if (
        not album
        and not album_tracker
        and not total_tracks
        and not first_date
        and not last_date
    ):
        return None

    progress_text = None
    if total_tracks:
        progress_text = f"Progress: {library_track_count}/{total_tracks}"

    return {
        "title": album.title if album else "",
        "progress_text": progress_text,
        "date_start": first_date or getattr(album_tracker, "start_date", None),
        "date_end": last_date or getattr(album_tracker, "end_date", None),
        "collapse_same_day": collapse_same_day,
    }


def _build_music_detail_secondary_actions(
    *,
    history_url,
    sync_url,
    sync_title,
    delete_url,
    delete_title,
    delete_confirm,
):
    """Return shared secondary action metadata for music detail pages."""
    return [
        {
            "kind": "link",
            "title": "View your activity history",
            "url": history_url,
            "icon": "history",
        },
        {
            "kind": "button",
            "title": sync_title,
            "hx_post": sync_url,
            "icon": "circle-spinning-clockwise",
        },
        {
            "kind": "button",
            "title": delete_title,
            "hx_post": delete_url,
            "icon": "trashcan",
            "confirm": delete_confirm,
            "button_classes": "border-red-500/20 bg-red-500/10 text-red-100 hover:bg-red-500/20",
        },
    ]


def _render_music_tracker_modal(
    request,
    *,
    title,
    tracker,
    form,
    save_url,
    delete_url,
    release_date_shortcut="",
    bulk_domain=None,
    bulk_form_override=None,
    initial_active_tab="general",
):
    """Render a non-item music tracker modal through the shared shell."""
    from app import views as view_barrel

    return_url = request.GET.get("return_url") or request.POST.get("return_url", "")
    track_form_id = f"track-form-{uuid4().hex}"
    field_groups = view_barrel._track_modal_field_groups(
        form,
        hidden_field_names=set(
            field_name for field_name in form.fields if field_name.endswith("_id")
        ),
        metadata_field_names=set(),
    )
    episode_plays_tab_available = bool(bulk_domain)
    if episode_plays_tab_available:
        if bulk_form_override is not None:
            episode_plays_form = bulk_form_override
        else:
            bulk_initial = view_barrel._bulk_episode_form_initial_data(
                return_url,
                bulk_domain,
            )
            bulk_initial["instance_id"] = tracker.id if tracker else ""
            episode_plays_form = BulkEpisodeTrackForm(
                initial=bulk_initial,
                domain=bulk_domain,
            )
        episode_plays_tab_label = bulk_domain.get("tab_label") or "Track Plays"
        episode_plays_submit_label = bulk_domain.get("submit_label") or "Save plays"
    else:
        episode_plays_form = None
        episode_plays_tab_label = "Track Plays"
        episode_plays_submit_label = "Save plays"

    response = render(
        request,
        "app/components/fill_track.html",
        {
            "user": request.user,
            "title": title,
            "media_type": MediaTypes.MUSIC.value,
            "form": form,
            "media": tracker,
            "return_url": return_url,
            "metadata_tab_available": False,
            "metadata_fields": [],
            "general_hidden_fields": field_groups["hidden_fields"],
            "general_fields": field_groups["general_fields"],
            "general_submit_formaction": f"{save_url}?next={return_url}",
            "general_delete_formaction": f"{delete_url}?next={return_url}",
            "general_existing_instance": tracker,
            "image_field": None,
            "image_save_item_id": None,
            "release_date_shortcut": release_date_shortcut,
            "release_date_runtime_minutes": "",
            "track_form_id": track_form_id,
            "initial_active_tab": initial_active_tab,
            "episode_plays_tab_available": episode_plays_tab_available,
            "episode_plays_form": episode_plays_form,
            "episode_plays_formaction": reverse("music_bulk_save"),
            "episode_plays_tab_label": episode_plays_tab_label,
            "episode_plays_submit_label": episode_plays_submit_label,
            "episode_plays_domain": view_barrel._episode_domain_template_payload(
                bulk_domain,
            ),
            "episode_plays_mode_notice": (
                bulk_domain.get("mode_notice", "")
                if bulk_domain
                else ""
            ),
            "episode_plays_domain_script_id": f"{track_form_id}-episode-domain",
        },
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def _music_bulk_redirect_url(request, *, artist=None, album=None):
    """Return the destination after a successful music bulk-play save."""
    next_url = request.GET.get("next") or request.POST.get("return_url", "")
    if next_url:
        return next_url
    if album is not None:
        return _music_album_detail_url(album)
    if artist is not None:
        return _music_artist_detail_url(artist)
    return reverse("medialist", args=[MediaTypes.MUSIC.value])


def _render_music_artist_details(request, artist):
    """Render a music artist through the shared media details template."""
    from app import views as view_barrel
    from app.helpers import get_artist_collection_stats
    from app.models import AlbumTracker, ArtistTracker
    from app.providers import musicbrainz
    from app.services.music import (
        build_discography_groups,
        needs_discography_sync,
        sync_artist_discography,
    )
    from app.services.music_scrobble import dedupe_artist_albums

    if not artist.musicbrainz_id:
        try:
            mbid, cand_count, variant = sync_services.resolve_artist_mbid(
                artist.name or "",
                artist.sort_name or "",
            )
            if mbid:
                try:
                    artist.musicbrainz_id = mbid
                    artist.discography_synced_at = None
                    artist.save(
                        update_fields=["musicbrainz_id", "discography_synced_at"],
                    )
                    logger.info(
                        "Attached MBID %s to artist %s on view via '%s' (candidates=%d)",
                        mbid,
                        artist.name,
                        variant,
                        cand_count,
                    )
                except IntegrityError:
                    from app.services.music import merge_artist_records

                    existing = Artist.objects.filter(musicbrainz_id=mbid).first()
                    if existing:
                        artist = merge_artist_records(artist, existing)
                        logger.info(
                            "Merged artist %s into existing MBID %s via '%s'",
                            artist.name,
                            mbid,
                            variant,
                        )
                        return redirect(_music_artist_detail_url(artist))
            else:
                logger.debug(
                    "No MBID attached on view for %s after searching variants (candidates=%d)",
                    artist.name,
                    cand_count,
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "Artist MBID attach failed on view for %s: %s",
                artist.name,
                exception_summary(exc),
            )

    dedupe_artist_albums(artist)

    albums_qs = Album.objects.filter(artist=artist)
    existing_album_count = albums_qs.count()
    missing_mbids = albums_qs.filter(
        musicbrainz_release_id__isnull=True,
        musicbrainz_release_group_id__isnull=True,
    ).exists()
    should_sync = (
        needs_discography_sync(artist, max_age_days=1)
        or existing_album_count == 0
        or missing_mbids
    )
    force_sync = existing_album_count == 0 or missing_mbids

    synced_count = 0
    if should_sync and artist.musicbrainz_id:
        synced_count = sync_artist_discography(artist, force=force_sync)
        if synced_count:
            dedupe_artist_albums(artist)
    elif should_sync and not artist.musicbrainz_id:
        logger.debug(
            "Skipping discography sync for %s due to missing MBID",
            artist.name,
        )

    all_albums = list(
        Album.objects.filter(artist=artist).order_by("-release_date", "title"),
    )

    user_music_entries = list(
        Music.objects.filter(
            user=request.user,
            album__artist=artist,
        ).select_related("album", "item"),
    )

    album_play_counts = {}
    total_plays = 0
    for music in user_music_entries:
        if music.album_id:
            play_count = music.history.count()
            album_play_counts[music.album_id] = (
                album_play_counts.get(music.album_id, 0) + play_count
            )
            total_plays += play_count

    album_trackers = AlbumTracker.objects.filter(
        user=request.user,
        album__in=all_albums,
    ).select_related("album")
    album_scores = {
        tracker.album_id: tracker.score
        for tracker in album_trackers
        if tracker.score is not None
    }

    for album in all_albums:
        album.play_count = album_play_counts.get(album.id, 0)
        album.score = album_scores.get(album.id)

    discography_groups = build_discography_groups(all_albums)
    missing_cover_count = sum(
        1
        for album in all_albums
        if not album.image or album.image == settings.IMG_NONE
    )

    artist_tracker = ArtistTracker.objects.filter(
        user=request.user,
        artist=artist,
    ).first()

    first_listened, last_listened, collapse_same_day = _music_activity_date_range(
        user_music_entries,
    )
    artist_activity_subtitle = _build_music_artist_activity_subtitle(
        artist_tracker,
        total_plays,
        first_listened,
        last_listened,
        collapse_same_day,
    )

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
            bio = mb_data.get("bio", "")

            updated_fields = []
            if mb_data.get("country") and mb_data.get("country") != artist.country:
                artist.country = mb_data.get("country", "")
                updated_fields.append("country")
            if mb_data.get("genres"):
                genre_names = [
                    g.get("name")
                    for g in mb_data.get("genres")
                    if g.get("name")
                ]
                if genre_names != artist.genres:
                    artist.genres = genre_names
                    updated_fields.append("genres")
            if updated_fields:
                artist.save(update_fields=updated_fields)

            wiki_image = mb_data.get("image")
            if wiki_image and (not artist.image or artist.image == settings.IMG_NONE):
                artist.image = wiki_image
                artist.save(update_fields=["image"])
        except Exception as exc:
            logger.debug("Failed to fetch artist metadata from MusicBrainz: %s", exc)

    genre_chips = []
    if genres:
        genre_chips = [g["name"].title() for g in genres[:6]]
    elif tags:
        genre_chips = [t["name"].title() for t in tags[:6]]

    collection_stats = get_artist_collection_stats(request.user, artist)
    notes_entry = artist_tracker if artist_tracker and artist_tracker.notes else None
    detail_link_sections = view_barrel._build_detail_link_sections(
        {
            "source_url": (
                f"https://musicbrainz.org/artist/{artist.musicbrainz_id}"
                if artist.musicbrainz_id
                else ""
            ),
        },
        MediaTypes.MUSIC.value,
        Sources.MUSICBRAINZ.value,
        Sources.MUSICBRAINZ.value,
    )
    detail_primary_action = {
        "label": artist_tracker.status_readable if artist_tracker else "Add to Library",
        "modal_url": reverse("artist_track_modal", args=[artist.id]),
        "target_id": f"artist-track-modal-{artist.id}",
        "active": bool(artist_tracker),
    }
    detail_history_url = f"{reverse('history')}?artist={artist.id}"

    context = {
        "user": request.user,
        "music_detail_kind": "artist",
        "media_type": MediaTypes.MUSIC.value,
        "artist": artist,
        "media": {
            "media_type": MediaTypes.MUSIC.value,
            "source": Sources.MUSICBRAINZ.value,
            "media_id": artist.musicbrainz_id or f"artist-{artist.id}",
            "title": artist.name,
            "image": artist.image or settings.IMG_NONE,
            "synopsis": bio or artist_metadata.get("disambiguation", ""),
            "details": {},
            "related": {},
        },
        "artist_tracker": artist_tracker,
        "notes_entry": notes_entry,
        "detail_notes_modal_url": reverse("artist_track_modal", args=[artist.id]),
        "detail_notes_target_id": f"artist-track-modal-{artist.id}",
        "detail_history_url": detail_history_url,
        "detail_primary_action": detail_primary_action,
        "detail_secondary_actions": _build_music_detail_secondary_actions(
            history_url=detail_history_url,
            sync_url=reverse("sync_artist_discography", args=[artist.id]),
            sync_title="Sync discography from MusicBrainz",
            delete_url=reverse("delete_all_artist_plays", args=[artist.id]),
            delete_title="Delete all plays for this artist",
            delete_confirm="Are you sure you want to delete all plays for this artist? This cannot be undone.",
        ),
        "detail_collection_mode": "music_artist",
        "detail_link_sections": detail_link_sections,
        "discography_groups": discography_groups,
        "collection_stats": collection_stats,
        "music_artist_metadata": artist_metadata,
        "music_artist_rating": {
            "rating": mb_rating,
            "rating_count": mb_rating_count,
        },
        "music_artist_activity_subtitle": artist_activity_subtitle,
        "genre_chips": genre_chips,
        "total_plays": total_plays,
        "total_releases": len(all_albums),
        "missing_cover_count": missing_cover_count,
        "poll_for_covers": missing_cover_count > 0,
        "bio": bio,
    }
    return render(request, "app/media_details.html", context)


def _render_music_album_details(request, artist, album):
    """Render a music album through the shared media details template."""
    from app import views as view_barrel
    from app.helpers import get_album_collection_metadata
    from app.models import AlbumTracker
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

    original_artist = album.artist
    original_title = album.title
    original_norm = _normalize(original_title)

    if album.artist_id and artist and album.artist_id != artist.id:
        return redirect(_music_album_detail_url(album))

    if original_artist:
        dedupe_artist_albums(original_artist)
        try:
            album.refresh_from_db()
        except Album.DoesNotExist:
            replacement = (
                Album.objects.filter(
                    artist=original_artist,
                    title__iexact=original_title,
                )
                .order_by("id")
                .first()
            )
            if not replacement:
                for cand in Album.objects.filter(artist=original_artist):
                    if _normalize(cand.title) == original_norm:
                        replacement = cand
                        break
            if replacement:
                return redirect(_music_album_detail_url(replacement))
            raise

        if is_incomplete_album(album) and original_artist.musicbrainz_id:
            try:
                sync_artist_discography(original_artist, force=True)
                dedupe_artist_albums(original_artist)
                candidates = [
                    candidate
                    for candidate in Album.objects.filter(artist=original_artist)
                    if _normalize(candidate.title) == original_norm
                ]
                if candidates:
                    best = _choose_primary_album(candidates, album)
                    if best.id != album.id:
                        return redirect(_music_album_detail_url(best))
                    album = best
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "Failed to heal album %s via discography: %s",
                    album.id,
                    exception_summary(exc),
                )

    if not album.musicbrainz_release_id and album.musicbrainz_release_group_id:
        ensure_album_has_release_id(album)

    has_mb_identity = album_has_musicbrainz_id(album)
    if not album.tracks_populated and has_mb_identity:
        try:
            if album.musicbrainz_release_id:
                release_data = musicbrainz.get_release(album.musicbrainz_release_id)
                tracks_data = release_data.get("tracks", [])

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
                            "genres": track_data.get("genres", [])
                            or release_data.get("genres", []),
                        },
                    )

                if not album.image or album.image == settings.IMG_NONE:
                    new_image = release_data.get("image", "")
                    if new_image and new_image != settings.IMG_NONE:
                        album.image = new_image

                album.tracks_populated = True
                album.save(update_fields=["tracks_populated", "image"])
            else:
                logger.warning(
                    "Album %s has release_group but no release found for tracks",
                    album.title,
                )
        except Exception as exc:
            logger.warning(
                "Failed to populate tracks for album %s: %s",
                album.title,
                exc,
            )

    all_tracks = Track.objects.filter(album=album).order_by(
        "disc_number",
        "track_number",
        "title",
    )
    user_music_entries = list(
        Music.objects.filter(
            user=request.user,
            album=album,
        ).select_related("item", "track"),
    )

    user_music_by_track = {}
    for music in user_music_entries:
        if music.track_id:
            user_music_by_track[music.track_id] = music
        if music.item and music.item.media_id:
            user_music_by_track[f"recording_{music.item.media_id}"] = music

    collection_entries_by_item_id = {}
    music_item_ids = [music.item_id for music in user_music_entries if music.item_id]
    if music_item_ids:
        collection_entries = CollectionEntry.objects.filter(
            user=request.user,
            item_id__in=music_item_ids,
        ).order_by("-collected_at", "-id")
        for collection_entry in collection_entries:
            collection_entries_by_item_id.setdefault(
                collection_entry.item_id,
                collection_entry,
            )

    tracks_with_data = []
    total_duration_ms = 0
    for track in all_tracks:
        music_entry = user_music_by_track.get(track.id)
        if not music_entry and track.musicbrainz_recording_id:
            music_entry = user_music_by_track.get(
                f"recording_{track.musicbrainz_recording_id}",
            )

        collection_entry = None
        if music_entry and music_entry.item_id:
            collection_entry = collection_entries_by_item_id.get(music_entry.item_id)

        tracks_with_data.append(
            {
                "track": track,
                "music": music_entry,
                "history": (
                    list(music_entry.history.all().order_by("-end_date"))
                    if music_entry
                    else []
                ),
                "collection_entry": collection_entry,
            },
        )
        if track.duration_ms:
            total_duration_ms += track.duration_ms

    library_track_count = sum(
        1 for track_data in tracks_with_data if track_data["music"]
    )
    first_listened, last_listened, collapse_same_day = _music_activity_date_range(
        user_music_entries,
    )

    album_tracker = AlbumTracker.objects.filter(
        user=request.user,
        album=album,
    ).first()

    total_runtime = None
    if total_duration_ms:
        total_minutes = total_duration_ms // 60000
        if total_minutes >= 60:
            hours = total_minutes // 60
            minutes = total_minutes % 60
            total_runtime = f"{hours}h {minutes}m"
        else:
            total_runtime = f"{total_minutes}m"

    album_activity_subtitle = _build_music_album_activity_subtitle(
        album,
        album_tracker,
        library_track_count,
        len(tracks_with_data),
        first_listened,
        last_listened,
        collapse_same_day,
    )

    album_details = {
        "format": album.release_type or "Album",
        "release_date": album.release_date,
        "tracks": len(tracks_with_data),
        "runtime": total_runtime,
    }
    if album.musicbrainz_release_id:
        album_details["musicbrainz_id"] = album.musicbrainz_release_id
        album_details["musicbrainz_url"] = (
            f"https://musicbrainz.org/release/{album.musicbrainz_release_id}"
        )
    elif album.musicbrainz_release_group_id:
        album_details["musicbrainz_id"] = album.musicbrainz_release_group_id
        album_details["musicbrainz_url"] = (
            f"https://musicbrainz.org/release-group/{album.musicbrainz_release_group_id}"
        )

    collection_metadata = get_album_collection_metadata(request.user, album)
    notes_entry = album_tracker if album_tracker and album_tracker.notes else None
    detail_link_sections = view_barrel._build_detail_link_sections(
        {
            "source_url": album_details.get("musicbrainz_url", ""),
        },
        MediaTypes.MUSIC.value,
        Sources.MUSICBRAINZ.value,
        Sources.MUSICBRAINZ.value,
    )
    detail_primary_action = {
        "label": album_tracker.status_readable if album_tracker else "Add to Library",
        "modal_url": reverse("album_track_modal", args=[album.id]),
        "target_id": f"album-track-modal-{album.id}",
        "active": bool(album_tracker),
    }
    detail_history_url = f"{reverse('history')}?album={album.id}"

    context = {
        "user": request.user,
        "music_detail_kind": "album",
        "media_type": MediaTypes.MUSIC.value,
        "artist": artist or album.artist,
        "album": album,
        "media": {
            "media_type": MediaTypes.MUSIC.value,
            "source": Sources.MUSICBRAINZ.value,
            "media_id": (
                album.musicbrainz_release_id
                or album.musicbrainz_release_group_id
                or f"album-{album.id}"
            ),
            "title": album.title,
            "image": album.image or settings.IMG_NONE,
            "synopsis": "",
            "details": {},
            "related": {},
        },
        "tracks": tracks_with_data,
        "has_mb_identity": has_mb_identity,
        "album_tracker": album_tracker,
        "total_tracks": len(tracks_with_data),
        "library_track_count": library_track_count,
        "total_runtime": total_runtime,
        "music_album_metadata": album_details,
        "album_collection_metadata": collection_metadata,
        "album_collection_stats": None,
        "music_album_activity_subtitle": album_activity_subtitle,
        "detail_notes_modal_url": reverse("album_track_modal", args=[album.id]),
        "detail_notes_target_id": f"album-track-modal-{album.id}",
        "detail_history_url": detail_history_url,
        "detail_primary_action": detail_primary_action,
        "detail_secondary_actions": _build_music_detail_secondary_actions(
            history_url=detail_history_url,
            sync_url=reverse("sync_album_metadata", args=[album.id]),
            sync_title="Sync album metadata and tracks from MusicBrainz",
            delete_url=reverse("delete_all_album_plays", args=[album.id]),
            delete_title="Delete all plays for this album",
            delete_confirm="Are you sure you want to delete all plays for this album? This cannot be undone.",
        ),
        "detail_collection_mode": "music_album",
        "detail_link_sections": detail_link_sections,
        "notes_entry": notes_entry,
    }
    return render(request, "app/media_details.html", context)


@require_GET
def music_artist_details(request, artist_id, artist_slug):
    """Return the canonical shared music artist detail page."""
    artist = get_object_or_404(Artist, id=artist_id)
    return _render_music_artist_details(request, artist)


@require_GET
def music_album_details(request, artist_id, artist_slug, album_id, album_slug):
    """Return the canonical shared music album detail page."""
    album = get_object_or_404(Album.objects.select_related("artist"), id=album_id)
    if album.artist_id and album.artist_id != artist_id:
        return redirect(_music_album_detail_url(album))
    artist = album.artist
    return _render_music_album_details(request, artist, album)


@require_GET
def create_artist_from_search(request, musicbrainz_artist_id):
    """Create an Artist from MusicBrainz search and redirect to artist page."""
    from app.providers import musicbrainz
    from app.services.music import sync_artist_discography

    artist = Artist.objects.filter(musicbrainz_id=musicbrainz_artist_id).first()

    if not artist:
        artist_data = musicbrainz.get_artist(musicbrainz_artist_id)

        artist = Artist.objects.create(
            name=artist_data.get("name", "Unknown Artist"),
            sort_name=artist_data.get("sort_name", ""),
            musicbrainz_id=musicbrainz_artist_id,
            country=artist_data.get("country", "") or "",
            genres=[
                g.get("name")
                for g in artist_data.get("genres", [])
                if g.get("name")
            ]
            if artist_data.get("genres")
            else [],
        )
        logger.info("Created artist %s from MusicBrainz", artist.name)

        sync_artist_discography(artist)

    return redirect(_music_artist_detail_url(artist))


@require_GET
def create_album_from_search(request, musicbrainz_release_id):
    """Create an Album from MusicBrainz search and redirect to album page."""
    from app.providers import musicbrainz

    album = Album.objects.filter(
        musicbrainz_release_id=musicbrainz_release_id,
    ).first()

    release_data = musicbrainz.get_release(musicbrainz_release_id)

    if not album:
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

        release_date = None
        date_str = release_data.get("release_date", "")
        if date_str:
            try:
                from datetime import datetime

                if len(date_str) == 4:
                    release_date = datetime.strptime(date_str, "%Y").date()
                elif len(date_str) == 7:
                    release_date = datetime.strptime(date_str, "%Y-%m").date()
                elif len(date_str) >= 10:
                    release_date = datetime.strptime(
                        date_str[:10],
                        "%Y-%m-%d",
                    ).date()
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
        new_image = release_data.get("image", "")
        if (
            new_image
            and new_image != settings.IMG_NONE
            and (not album.image or album.image == settings.IMG_NONE)
        ):
            album.image = new_image
            album.save(update_fields=["image"])
            logger.info("Updated album %s image", album.title)
        if not album.genres and release_data.get("genres"):
            album.genres = release_data.get("genres", [])
            album.save(update_fields=["genres"])

    return redirect(_music_album_detail_url(album))


@require_GET
def artist_detail(request, artist_id):
    """Redirect legacy music artist URLs to the canonical shared detail page."""
    artist = get_object_or_404(Artist, id=artist_id)
    return redirect(_music_artist_detail_url(artist))


@require_GET
def prefetch_artist_covers(request, artist_id):
    """HTMX endpoint to asynchronously fetch album covers for an artist.

    This runs after the artist page loads to avoid blocking the initial render.
    Returns the updated album grid HTML.
    """
    from app.services.music import build_discography_groups
    from app.tasks import prefetch_album_covers_batch

    artist = get_object_or_404(Artist, id=artist_id)

    all_albums = list(Album.objects.filter(artist=artist).order_by("-release_date", "title"))

    user_music_entries = Music.objects.filter(
        user=request.user,
        album__artist=artist,
    ).select_related("album")

    album_play_counts = {}
    for music in user_music_entries:
        if music.album_id:
            play_count = music.history.count()
            album_play_counts[music.album_id] = (
                album_play_counts.get(music.album_id, 0) + play_count
            )

    for album in all_albums:
        album.play_count = album_play_counts.get(album.id, 0)

    discography_groups = build_discography_groups(all_albums)
    missing_cover_count = sum(
        1
        for album in all_albums
        if not album.image or album.image == settings.IMG_NONE
    )

    poll_for_covers = missing_cover_count > 0
    if missing_cover_count:
        cache_key = f"music:cover-prefetch:{artist.id}"
        try:
            if cache.add(cache_key, True, 60 * 10):
                try:
                    prefetch_album_covers_batch.delay([artist.id], limit_per_artist=None)
                except Exception as queue_exc:  # pragma: no cover - defensive
                    cache.delete(cache_key)
                    raise queue_exc
            poll_for_covers = bool(cache.get(cache_key))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "Cover prefetch queue failed for artist %s: %s",
                artist.id,
                exception_summary(exc),
            )

    return render(
        request,
        "app/components/artist_discography_container.html",
        {
            "discography_groups": discography_groups,
            "artist": artist,
            "missing_cover_count": missing_cover_count,
            "poll_for_covers": poll_for_covers,
        },
    )


@require_GET
def album_detail(request, album_id):
    """Redirect legacy music album URLs to the canonical shared detail page."""
    album = get_object_or_404(Album.objects.select_related("artist"), id=album_id)
    return redirect(_music_album_detail_url(album))


@require_POST
def sync_artist_discography_view(request, artist_id):
    """Manually trigger discography sync for an artist."""
    from app.services.music import prefetch_album_covers, sync_artist_discography
    from app.services.music_scrobble import dedupe_artist_albums
    from app.tasks import prefetch_album_covers_batch

    artist = get_object_or_404(Artist, id=artist_id)

    count = sync_artist_discography(artist, force=True)
    if count:
        dedupe_artist_albums(artist)

    cover_task_id = None
    try:
        result = prefetch_album_covers_batch.delay([artist.id], limit_per_artist=None)
        cover_task_id = result.id
        cache.set(f"music:cover-prefetch:{artist.id}", True, 60 * 10)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Cover prefetch queue failed for artist %s: %s",
            artist.id,
            exception_summary(exc),
        )
        try:
            prefetch_album_covers(artist, limit=None)
            cache.set(f"music:cover-prefetch:{artist.id}", True, 60 * 10)
        except Exception as inner_exc:  # pragma: no cover - defensive
            logger.debug(
                "Cover prefetch failed for artist %s: %s",
                artist.id,
                inner_exc,
            )

    if cover_task_id:
        messages.success(
            request,
            f"Synced {count} albums for {artist.name}. Cover art refresh queued.",
        )
    else:
        messages.success(request, f"Synced {count} albums for {artist.name}")

    response = HttpResponse(status=204)
    response["HX-Refresh"] = "true"
    return response


def artist_track_modal(request, artist_id):
    """Return the shared tracking form modal for a music artist."""
    from app.forms import ArtistTrackerForm
    from app.models import ArtistTracker

    artist = get_object_or_404(Artist, id=artist_id)
    tracker = ArtistTracker.objects.filter(user=request.user, artist=artist).first()
    form = ArtistTrackerForm(
        instance=tracker,
        initial={"artist_id": artist.id},
        user=request.user,
    )
    return _render_music_tracker_modal(
        request,
        title=artist.name,
        tracker=tracker,
        form=form,
        save_url=reverse("artist_save"),
        delete_url=reverse("artist_delete"),
        bulk_domain=bulk_music_tracking.build_artist_play_domain(request.user, artist),
    )


@require_POST
def artist_save(request):
    """Save an artist tracker - mirrors media_save for TV."""
    from app.forms import ArtistTrackerForm
    from app.models import ArtistTracker

    artist_id = request.POST.get("artist_id")
    artist = get_object_or_404(Artist, id=artist_id)
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.MUSIC.value,
    )

    tracker = ArtistTracker.objects.filter(user=request.user, artist=artist).first()

    form = ArtistTrackerForm(request.POST, instance=tracker, user=request.user)
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
    return redirect(_music_artist_detail_url(artist))


@require_POST
def artist_delete(request):
    """Delete an artist tracker - mirrors media_delete for TV."""
    from app.models import ArtistTracker

    artist_id = request.POST.get("artist_id")
    artist = get_object_or_404(Artist, id=artist_id)
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.MUSIC.value,
    )

    tracker = ArtistTracker.objects.filter(user=request.user, artist=artist).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {artist.name} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        return redirect(next_url)
    return redirect(_music_artist_detail_url(artist))
