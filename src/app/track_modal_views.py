from datetime import UTC, date
from uuid import uuid4

from django.conf import settings
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.timezone import datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from app import custom_metadata, helpers
from app import statistics as stats
from app.discover_views import _build_track_modal_discover_tab_context
from app.forms import BulkEpisodeTrackForm, get_form_class
from app.models import (
    Anime,
    BasicMedia,
    Item,
    MediaTypes,
    MetadataProviderPreference,
    Sources,
    Status,
)
from app.providers import services
from app.services import bulk_episode_tracking, metadata_resolution


class _EmptyHistoryProxy:
    """Minimal queryset-like history object for empty podcast wrappers."""

    def all(self):
        return []

    def count(self):
        return 0

    def filter(self, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self


class _DummyPodcastWrapper:
    """Template-compatible podcast wrapper when no plays exist yet."""

    def __init__(self, item):
        self.item = item
        self.id = 0
        self.in_progress_instance_id = None
        self.history = _EmptyHistoryProxy()

    @property
    def completed_play_count(self):
        return 0

    @property
    def has_in_progress_entry(self):
        return False


def _bulk_episode_form_initial_data(return_url, domain):
    """Return initial form values for the bulk episode-play tab."""
    now = timezone.localtime(timezone.now()).replace(second=0, microsecond=0)
    date_initial = now if settings.TRACK_TIME else now.date()

    return {
        "media_id": domain["tracking_media_id"],
        "source": domain["tracking_source"],
        "media_type": domain["route_media_type"],
        "identity_media_type": domain.get("identity_media_type") or "",
        "library_media_type": domain.get("library_media_type") or "",
        "instance_id": "",
        "return_url": return_url,
        "context_kind": domain.get("context_kind") or "",
        "context_id": domain.get("context_id") or "",
        "first_season_number": domain["default_first"]["season_number"],
        "first_episode_number": domain["default_first"]["episode_number"],
        "last_season_number": domain["default_last"]["season_number"],
        "last_episode_number": domain["default_last"]["episode_number"],
        "write_mode": BulkEpisodeTrackForm.WRITE_MODE_ADD,
        "distribution_mode": BulkEpisodeTrackForm.DISTRIBUTION_MODE_AIR_DATE,
        "start_date": date_initial,
        "end_date": date_initial,
    }


def _episode_domain_template_payload(domain):
    """Return the JSON-friendly episode selector payload for Alpine."""
    if not domain:
        return None

    season_episode_map = {}
    for season_number, episodes in domain["season_episode_map"].items():
        season_episode_map[str(season_number)] = [
            {
                "order": episode["order"],
                "season_number": episode["season_number"],
                "episode_number": episode["episode_number"],
                "episode_title": episode["episode_title"],
                "selector_label": episode.get("selector_label", ""),
                "existing_play_count": episode["existing_play_count"],
                "air_date": episode["air_date"].isoformat() if episode["air_date"] else "",
            }
            for episode in episodes
        ]

    return {
        "seasons": domain["seasons"],
        "seasonEpisodeMap": season_episode_map,
        "defaultFirst": domain["default_first"],
        "defaultLast": domain["default_last"],
        "lockedSeasonNumber": domain["locked_season_number"],
        "hideSeasonSelectors": domain.get("hide_season_selectors", False),
        "firstSelectionTitle": domain.get("first_selection_title", ""),
        "lastSelectionTitle": domain.get("last_selection_title", ""),
        "seasonFieldLabel": domain.get("season_field_label", ""),
        "episodeFieldLabel": domain.get("episode_field_label", ""),
        "selectionNoun": domain.get("selection_noun", ""),
        "selectionNounPlural": domain.get("selection_noun_plural", ""),
        "distributionTargetLabel": domain.get("distribution_target_label", ""),
        "missingTargetDateFallbackDistribution": domain.get(
            "missing_target_date_fallback_distribution",
            "",
        ),
        "dateShortcutLabel": domain.get("date_shortcut_label", ""),
        "modeNotice": domain.get("mode_notice", ""),
    }


def _track_modal_field_groups(form, *, hidden_field_names, metadata_field_names=None):
    """Split a track form into hidden, general, and metadata field groups."""
    metadata_field_names = metadata_field_names or set()
    ordered_general_field_names = [
        field_name
        for field_name in ("score", "status", "progress", "start_date", "end_date")
        if field_name in form.fields
    ]
    remaining_general_field_names = [
        field_name
        for field_name in form.fields
        if field_name not in hidden_field_names
        and field_name not in metadata_field_names
        and field_name != "notes"
        and field_name not in ordered_general_field_names
    ]
    return {
        "general_fields": [
            form[field_name]
            for field_name in ordered_general_field_names + remaining_general_field_names
        ],
        "metadata_fields": [
            form[field_name]
            for field_name in form.fields
            if field_name in metadata_field_names
        ],
        "hidden_fields": [
            form[field_name]
            for field_name in form.fields
            if field_name in hidden_field_names
        ],
    }


def _track_modal_release_date_shortcut(*candidates):
    """Return an ISO release-date string for the shared track modal shortcut."""
    for candidate in candidates:
        if not candidate:
            continue
        if isinstance(candidate, dict):
            candidate = helpers.extract_release_datetime(candidate)
        elif isinstance(candidate, str):
            candidate = parse_date(candidate[:10])

        if not candidate:
            continue
        if isinstance(candidate, datetime):
            if timezone.is_aware(candidate):
                candidate = timezone.localtime(candidate)
            return candidate.date().isoformat()
        if isinstance(candidate, date):
            return candidate.isoformat()
    return ""


def _track_modal_release_runtime_minutes(media_type, *candidates):
    """Return a trusted runtime in minutes for release-date start-date backfill."""
    if media_type != MediaTypes.MOVIE.value:
        return ""

    for candidate in candidates:
        if not candidate:
            continue

        runtime_minutes = None
        if isinstance(candidate, dict):
            runtime_minutes = candidate.get("runtime_minutes")
            if runtime_minutes is None:
                runtime_minutes = (candidate.get("details") or {}).get("runtime")
        else:
            runtime_minutes = getattr(candidate, "runtime_minutes", None)
            if runtime_minutes is None:
                runtime_minutes = getattr(candidate, "runtime", None)

        if isinstance(runtime_minutes, str):
            stripped_runtime = runtime_minutes.strip()
            runtime_minutes = (
                int(stripped_runtime)
                if stripped_runtime.isdigit()
                else stats.parse_runtime_to_minutes(stripped_runtime)
            )
        elif isinstance(runtime_minutes, float):
            runtime_minutes = int(runtime_minutes)

        if isinstance(runtime_minutes, int) and 0 < runtime_minutes < 999998:
            return str(runtime_minutes)

    return ""


def _render_standard_track_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    *,
    form_override=None,
    bulk_form_override=None,
    initial_active_tab="general",
    track_form_id=None,
    return_url=None,
    track_action_update=False,
):
    """Build and render the standard media track modal context."""
    instance_id = request.GET.get("instance_id") or request.POST.get("instance_id")
    if instance_id:
        media = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    elif request.GET.get("is_create"):
        media = None
    else:
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
    route_identity_media_type = None
    route_library_media_type = None

    max_progress = None
    metadata_resolution_result = None
    metadata_item = None
    base_metadata = None
    if media:
        title = media.item
        metadata_item = media.item
        if (
            media_type == MediaTypes.ANIME.value
            and media.item.media_type == MediaTypes.TV.value
            and media.item.library_media_type == MediaTypes.ANIME.value
        ):
            route_identity_media_type = MediaTypes.TV.value
            route_library_media_type = MediaTypes.ANIME.value
        if media_type == MediaTypes.GAME.value:
            initial_data["progress"] = helpers.minutes_to_hhmm(media.progress)
        elif media_type in (
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        ):
            if media_type == MediaTypes.BOOK.value:
                if media.item.number_of_pages:
                    max_progress = media.item.number_of_pages
                else:
                    try:
                        metadata = services.get_media_metadata(
                            media.item.media_type,
                            media.item.media_id,
                            media.item.source,
                        )
                        number_of_pages = metadata.get("max_progress") or metadata.get(
                            "details",
                            {},
                        ).get("number_of_pages")
                        if number_of_pages:
                            media.item.number_of_pages = number_of_pages
                            media.item.save(update_fields=["number_of_pages"])
                            max_progress = number_of_pages
                    except Exception:
                        pass
            else:
                media_list = [media]
                BasicMedia.objects.annotate_max_progress(media_list, media_type)
                if hasattr(media, "max_progress"):
                    max_progress = media.max_progress

            if (
                request.user.book_comic_manga_progress_percentage
                and max_progress
                and media.progress
            ):
                percentage = round((media.progress / max_progress) * 100, 1)
                initial_data["progress"] = percentage
    else:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        base_metadata = metadata
        title = metadata["title"]
        route_identity_media_type = metadata.get("identity_media_type")
        route_library_media_type = metadata.get("library_media_type")
        if media_type == MediaTypes.SEASON.value:
            title += f" S{season_number}"
        item_lookup = {
            "media_id": media_id,
            "source": source,
            "media_type": metadata_resolution.get_tracking_media_type(
                media_type,
                source=source,
                identity_media_type=route_identity_media_type,
            ),
            "season_number": season_number,
        }
        if metadata_resolution.is_grouped_anime_route(
            media_type,
            source=source,
            identity_media_type=route_identity_media_type,
            library_media_type=route_library_media_type,
        ):
            item_lookup["library_media_type"] = MediaTypes.ANIME.value
        metadata_item = Item.objects.filter(**item_lookup).first()

        # Suggest "In progress" if the user already has an in-progress entry for this media
        if request.user.is_authenticated:
            existing_in_progress = BasicMedia.objects.filter_media(
                request.user,
                media_id,
                media_type,
                source,
                season_number=season_number,
            ).filter(status=Status.IN_PROGRESS.value).exists()
            if existing_in_progress:
                initial_data["status"] = Status.IN_PROGRESS.value

    if route_identity_media_type:
        initial_data["identity_media_type"] = route_identity_media_type
    if route_library_media_type:
        initial_data["library_media_type"] = route_library_media_type
    if "image_url" not in initial_data:
        preferred_image = None
        if metadata_item and metadata_item.image and metadata_item.image != settings.IMG_NONE:
            preferred_image = metadata_item.image
        elif (
            base_metadata
            and base_metadata.get("image")
            and base_metadata["image"] != settings.IMG_NONE
        ):
            preferred_image = base_metadata["image"]
        if preferred_image:
            initial_data["image_url"] = preferred_image

    form_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
        identity_media_type=route_identity_media_type,
    )
    form_class = get_form_class(form_media_type)
    if form_override is not None:
        form = form_override
    elif media_type in (
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
        MediaTypes.MANGA.value,
    ):
        form = form_class(
            instance=media,
            initial=initial_data,
            user=request.user,
            max_progress=max_progress,
        )
    else:
        form = form_class(
            instance=media,
            initial=initial_data,
            user=request.user,
        )

    hidden_field_names = {
        "instance_id",
        "media_type",
        "identity_media_type",
        "library_media_type",
        "source",
        "media_id",
        "season_number",
        "start_date_cleared",
    }
    metadata_field_names = {"image_url"}
    field_groups = _track_modal_field_groups(
        form,
        hidden_field_names=hidden_field_names,
        metadata_field_names=metadata_field_names,
    )
    general_fields = field_groups["general_fields"]
    metadata_fields = field_groups["metadata_fields"]
    hidden_fields = field_groups["hidden_fields"]
    image_field = form["image_url"] if "image_url" in form.fields else None

    display_provider = source
    identity_provider = source
    grouped_preview = None
    grouped_preview_target = None
    can_update_metadata_provider = False
    can_migrate_grouped_anime = False
    metadata_provider_mapping_status = "identity"
    metadata_provider_options = []

    if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        if base_metadata is None:
            base_metadata = services.get_media_metadata(
                media_type,
                media_id,
                source,
                [season_number],
            )
        metadata_resolution_result = metadata_resolution.resolve_detail_metadata(
            request.user,
            item=metadata_item,
            route_media_type=media_type,
            media_id=media_id,
            source=source,
            base_metadata=base_metadata,
        )
        display_provider = metadata_resolution_result.display_provider
        identity_provider = metadata_resolution_result.identity_provider
        grouped_preview = metadata_resolution_result.grouped_preview
        grouped_preview_target = metadata_resolution_result.grouped_preview_target
        metadata_provider_mapping_status = metadata_resolution_result.mapping_status
        metadata_provider_options = metadata_resolution.available_metadata_provider_options(
            media_type,
            identity_provider=identity_provider,
        )
        can_migrate_grouped_anime = bool(
            metadata_item is not None
            and metadata_item.source == Sources.MAL.value
            and metadata_item.media_type == MediaTypes.ANIME.value
            and display_provider in {Sources.TMDB.value, Sources.TVDB.value}
            and grouped_preview
            and Anime.objects.filter(user=request.user, item=metadata_item).exists()
        )
    elif metadata_item is not None and custom_metadata.supports_custom_provider(media_type):
        metadata_provider_options = metadata_resolution.available_metadata_provider_options(
            media_type,
            identity_provider=identity_provider,
        )
        preference = MetadataProviderPreference.objects.filter(
            user=request.user,
            item=metadata_item,
        ).first()
        allowed_providers = {choice.value for choice in metadata_provider_options}
        if preference and preference.provider in allowed_providers:
            display_provider = preference.provider
            if (
                display_provider == Sources.MANUAL.value
                and identity_provider != Sources.MANUAL.value
            ):
                metadata_provider_mapping_status = "custom"

    can_update_metadata_provider = bool(
        metadata_item is not None and metadata_provider_options
    )

    manual_metadata_form = None
    can_edit_custom_metadata = bool(
        metadata_item is not None
        and display_provider == Sources.MANUAL.value
        and custom_metadata.supports_custom_metadata(metadata_item)
    )
    if can_edit_custom_metadata:
        manual_metadata_form = custom_metadata.ManualMetadataForm(
            item=metadata_item,
            prefix="metadata",
        )

    metadata_tab_available = bool(
        metadata_fields
        or can_update_metadata_provider
        or can_migrate_grouped_anime
        or manual_metadata_form
    )

    episode_plays_domain = bulk_episode_tracking.build_episode_play_domain(
        request.user,
        media_type,
        source,
        media_id,
        metadata_item=metadata_item,
        base_metadata=base_metadata,
        metadata_resolution_result=metadata_resolution_result,
    )
    episode_plays_tab_available = bool(episode_plays_domain)
    if return_url is None:
        return_url = (
            request.GET.get("return_url")
            or request.GET.get("next")
            or request.POST.get("return_url", "")
            or request.POST.get("next", "")
        )
    home_row_id = request.GET.get("home_row_id") or request.POST.get("home_row_id") or ""
    if episode_plays_tab_available:
        if bulk_form_override is not None:
            episode_plays_form = bulk_form_override
        else:
            bulk_initial = _bulk_episode_form_initial_data(return_url, episode_plays_domain)
            bulk_initial["instance_id"] = instance_id or ""
            episode_plays_form = BulkEpisodeTrackForm(
                initial=bulk_initial,
                domain=episode_plays_domain,
            )
    else:
        episode_plays_form = None

    track_form_id = track_form_id or f"track-form-{uuid4().hex}"
    release_date_shortcut = _track_modal_release_date_shortcut(
        getattr(metadata_item, "release_datetime", None) if metadata_item else None,
        (
            metadata_resolution_result.header_metadata
            if metadata_resolution_result is not None
            else None
        ),
        base_metadata,
    )
    release_date_runtime_minutes = _track_modal_release_runtime_minutes(
        media_type,
        metadata_item,
        (
            metadata_resolution_result.header_metadata
            if metadata_resolution_result is not None
            else None
        ),
        base_metadata,
    )
    context = {
        "user": request.user,
        "title": title,
        "media_type": media_type,
        "form": form,
        "media": media,
        "return_url": return_url,
        "max_progress": max_progress,
        "display_provider": display_provider,
        "display_provider_label": metadata_resolution.metadata_provider_label(
            display_provider,
        ),
        "identity_provider": identity_provider,
        "identity_provider_label": metadata_resolution.metadata_provider_label(
            identity_provider,
        ),
        "grouped_preview": grouped_preview,
        "grouped_preview_target": grouped_preview_target,
        "metadata_provider_mapping_status": metadata_provider_mapping_status,
        "metadata_provider_options": metadata_provider_options,
        "can_update_metadata_provider": can_update_metadata_provider,
        "can_migrate_grouped_anime": can_migrate_grouped_anime,
        "metadata_tab_available": metadata_tab_available,
        "metadata_item": metadata_item,
        "general_hidden_fields": hidden_fields,
        "general_fields": general_fields,
        "general_submit_formaction": (
            f"{reverse('media_save')}?next={return_url}"
            + (f"&home_row_id={home_row_id}" if home_row_id else "")
        ),
        "general_delete_formaction": f"{reverse('media_delete')}?next={return_url}",
        "general_existing_instance": media,
        "metadata_fields": metadata_fields,
        "image_field": image_field,
        "image_save_item_id": (
            metadata_item.id
            if media and metadata_item and not can_edit_custom_metadata
            else None
        ),
        "release_date_shortcut": release_date_shortcut,
        "release_date_runtime_minutes": release_date_runtime_minutes,
        "manual_metadata_form": manual_metadata_form,
        "manual_metadata_formaction": (
            reverse("update_manual_item_metadata", args=[metadata_item.id])
            if can_edit_custom_metadata
            else ""
        ),
        "can_edit_custom_metadata": can_edit_custom_metadata,
        "track_form_id": track_form_id,
        "track_action_update": track_action_update,
        "initial_active_tab": initial_active_tab,
        "episode_plays_tab_available": episode_plays_tab_available,
        "episode_plays_form": episode_plays_form,
        "episode_plays_formaction": reverse("episode_bulk_save"),
        "episode_plays_tab_label": "Episode Plays",
        "episode_plays_submit_label": "Save plays",
        "episode_plays_domain": _episode_domain_template_payload(episode_plays_domain),
        "episode_plays_mode_notice": (
            episode_plays_domain.get("mode_notice", "")
            if episode_plays_domain
            else ""
        ),
        "episode_plays_domain_script_id": f"{track_form_id}-episode-domain",
    }
    context.update(_build_track_modal_discover_tab_context(request.user, metadata_item))
    response = render(
        request,
        "app/components/fill_track.html",
        context,
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def _render_podcast_show_track_modal(
    request,
    show,
    *,
    form_override=None,
    bulk_form_override=None,
    initial_active_tab="general",
    track_form_id=None,
    return_url=None,
    track_action_update=False,
):
    """Build and render the podcast show tracking modal with bulk episode plays."""
    from app.forms import PodcastShowTrackerForm
    from app.models import PodcastShowTracker

    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()
    if return_url is None:
        return_url = (
            request.GET.get("return_url")
            or request.GET.get("next")
            or request.POST.get("return_url", "")
            or request.POST.get("next", "")
        )

    if form_override is not None:
        form = form_override
    else:
        form = PodcastShowTrackerForm(
            instance=tracker,
            initial={"show_id": show.id},
            user=request.user,
        )

    field_groups = _track_modal_field_groups(
        form,
        hidden_field_names={"show_id"},
        metadata_field_names=set(),
    )
    episode_plays_domain = bulk_episode_tracking.build_episode_play_domain(
        request.user,
        MediaTypes.PODCAST.value,
        Sources.POCKETCASTS.value,
        show.podcast_uuid,
        podcast_show=show,
    )
    episode_plays_tab_available = bool(episode_plays_domain)
    if episode_plays_tab_available:
        if bulk_form_override is not None:
            episode_plays_form = bulk_form_override
        else:
            bulk_initial = _bulk_episode_form_initial_data(
                return_url,
                episode_plays_domain,
            )
            bulk_initial["instance_id"] = tracker.id if tracker else ""
            episode_plays_form = BulkEpisodeTrackForm(
                initial=bulk_initial,
                domain=episode_plays_domain,
            )
    else:
        episode_plays_form = None

    track_form_id = track_form_id or f"track-form-{uuid4().hex}"
    response = render(
        request,
        "app/components/fill_track.html",
        {
            "user": request.user,
            "title": show.title,
            "media_type": MediaTypes.PODCAST.value,
            "form": form,
            "media": tracker,
            "return_url": return_url,
            "metadata_tab_available": False,
            "metadata_fields": [],
            "general_hidden_fields": field_groups["hidden_fields"],
            "general_fields": field_groups["general_fields"],
            "general_submit_formaction": (
                f"{reverse('podcast_show_save')}?next={return_url}"
            ),
            "general_delete_formaction": (
                f"{reverse('podcast_show_delete')}?next={return_url}"
            ),
            "general_existing_instance": tracker,
            "image_field": None,
            "image_save_item_id": None,
            "release_date_shortcut": "",
            "release_date_runtime_minutes": "",
            "track_form_id": track_form_id,
            "track_action_update": track_action_update,
            "initial_active_tab": initial_active_tab,
            "episode_plays_tab_available": episode_plays_tab_available,
            "episode_plays_form": episode_plays_form,
            "episode_plays_formaction": reverse("episode_bulk_save"),
            "episode_plays_tab_label": "Episode Plays",
            "episode_plays_submit_label": "Save plays",
            "episode_plays_domain": _episode_domain_template_payload(
                episode_plays_domain,
            ),
            "episode_plays_mode_notice": (
                episode_plays_domain.get("mode_notice", "")
                if episode_plays_domain
                else ""
            ),
            "episode_plays_domain_script_id": f"{track_form_id}-episode-domain",
        },
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


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
    track_action_update = (
        request.GET.get("track_action_update") == "1"
        or request.POST.get("track_action_update") == "1"
    )
    standard_modal = (
        request.GET.get("standard_modal") == "1"
        or request.POST.get("standard_modal") == "1"
    )

    # Handle podcast shows (identified by podcast_uuid)
    if (
        not standard_modal
        and media_type == MediaTypes.PODCAST.value
        and source == Sources.POCKETCASTS.value
    ):
        from app.models import PodcastEpisode, PodcastShow

        # Check if this is a show (podcast_uuid) or an episode (episode_uuid)
        show = PodcastShow.objects.filter(podcast_uuid=media_id).first()
        if show:
            return _render_podcast_show_track_modal(request, show)

        # This is an episode (episode_uuid) - use music-style modal
        episode = PodcastEpisode.objects.filter(episode_uuid=media_id).first()
        if episode:
            from app.models import Podcast

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
                    key=lambda x: x.end_date if x.end_date else datetime.min.replace(tzinfo=UTC),
                    reverse=True,
                )

                class PodcastHistoryWrapper:
                    """Wrapper to aggregate history from multiple Podcast entries."""

                    def __init__(self, podcasts, item, history_list):
                        self.item = item
                        self.id = podcasts[0].id if podcasts else 0
                        self._podcasts = podcasts
                        self._history_list = history_list
                        in_progress_entry = next(
                            (entry for entry in podcasts if not entry.end_date),
                            None,
                        )
                        self.in_progress_instance_id = (
                            in_progress_entry.id if in_progress_entry else None
                        )

                    @property
                    def completed_play_count(self):
                        """Return count of completed plays (history records with end_date)."""
                        # Since we already filtered all_history to only include records with end_date,
                        # we can just count the length of the filtered history_list
                        return len(self._history_list)

                    @property
                    def has_in_progress_entry(self):
                        return bool(self.in_progress_instance_id)

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
                                        key=lambda x: x.end_date if x.end_date else datetime.min.replace(tzinfo=UTC),
                                    )
                                elif order == "-end_date":
                                    sorted_list = sorted(
                                        self._history,
                                        key=lambda x: x.end_date if x.end_date else datetime.min.replace(tzinfo=UTC),
                                        reverse=True,
                                    )
                                else:
                                    sorted_list = self._history
                                return HistoryProxy(sorted_list)

                        return HistoryProxy(self._history_list)

                podcast = PodcastHistoryWrapper(all_podcasts, item, all_history)
            else:
                podcast = _DummyPodcastWrapper(item)

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

    return _render_standard_track_modal(
        request,
        source,
        media_type,
        media_id,
        season_number=season_number,
        track_action_update=track_action_update,
    )
