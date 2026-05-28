import logging

from django.conf import settings
from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.http import require_GET

from app import helpers
from app.services import metadata_resolution
from app.log_safety import exception_summary
from app.models import (
    Album,
    AlbumTracker,
    Artist,
    ArtistTracker,
    BasicMedia,
    Item,
    MediaTypes,
    Music,
    PodcastShow,
    PodcastShowTracker,
    Sources,
    Track,
)
from app.providers import services
from users.models import MediaStatusChoices

logger = logging.getLogger(__name__)


def _mark_grouped_anime_route(media_items):
    """Annotate grouped-anime rows so templates route them through the Anime UI."""
    for media in media_items or []:
        setattr(media, "route_media_type", MediaTypes.ANIME.value)
        item = getattr(media, "item", None)
        if item is not None:
            setattr(item, "route_media_type", MediaTypes.ANIME.value)
    return media_items


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

    def _norm(text):
        return str(text or "").strip().casefold()

    def _title_fields(item_obj):
        if isinstance(item_obj, dict):
            return (
                item_obj.get("title"),
                item_obj.get("original_title"),
                item_obj.get("localized_title"),
            )
        return (
            getattr(item_obj, "title", None),
            getattr(item_obj, "original_title", None),
            getattr(item_obj, "localized_title", None),
        )

    def _display_title_for_user(item_obj):
        if hasattr(item_obj, "get_display_title"):
            return item_obj.get_display_title(user=request.user)

        title, original_title, localized_title = _title_fields(item_obj)
        title = str(title or "").strip()
        original_title = str(original_title or "").strip() or None
        localized_title = str(localized_title or "").strip() or None

        if not localized_title and title:
            localized_title = title

        preference = getattr(request.user, "title_display_preference", "localized")
        if preference == "original":
            return original_title or localized_title or title
        return localized_title or original_title or title

    def _matched_title(item_obj, search_query):
        normalized_query = _norm(search_query)
        if not normalized_query:
            return None

        display_title = _display_title_for_user(item_obj)
        display_norm = _norm(display_title)

        title, original_title, localized_title = _title_fields(item_obj)
        candidates = []
        for candidate in (title, localized_title, original_title):
            text = str(candidate or "").strip()
            if text and text not in candidates:
                candidates.append(text)

        # Prefer exact, then prefix, then contains.
        for predicate in (
            lambda value: _norm(value) == normalized_query,
            lambda value: _norm(value).startswith(normalized_query),
            lambda value: normalized_query in _norm(value),
        ):
            for candidate in candidates:
                if _norm(candidate) == display_norm:
                    continue
                if predicate(candidate):
                    return candidate
        return None

    local_results = []
    local_results_total = 0
    local_results_limit = 24
    local_results_kind = "media"
    local_music_artists = []
    local_music_artists_total = 0
    local_music_albums = []
    local_music_albums_total = 0
    if request.user.is_authenticated and query and page == 1:
        try:
            if media_type == MediaTypes.PODCAST.value:
                show_trackers = (
                    PodcastShowTracker.objects.filter(user=request.user)
                    .exclude(show__title__isnull=True)
                    .exclude(show__title__exact="")
                    .filter(show__title__icontains=query)
                )
                local_results_total = show_trackers.count()
                show_trackers = show_trackers.order_by("show__title")[:local_results_limit]

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

                        self.item, _ = Item.objects.get_or_create(
                            media_id=tracker.show.podcast_uuid,
                            source=Sources.POCKETCASTS.value,
                            media_type=MediaTypes.PODCAST.value,
                            defaults={
                                "title": tracker.show.title,
                                "image": tracker.show.image or settings.IMG_NONE,
                            },
                        )
                        show_image = tracker.show.image or settings.IMG_NONE
                        if self.item.title != tracker.show.title or self.item.image != show_image:
                            self.item.title = tracker.show.title
                            self.item.image = show_image
                            self.item.save(update_fields=["title", "image"])

                adapted_media = [PodcastShowAdapter(tracker) for tracker in show_trackers]
                local_results = [
                    {
                        "item": media.item,
                        "media": media,
                        "matched_title": _matched_title(media.item, query),
                    }
                    for media in adapted_media
                ]
            elif media_type == MediaTypes.MUSIC.value:
                artist_trackers = (
                    ArtistTracker.objects.filter(user=request.user)
                    .exclude(artist__name__isnull=True)
                    .exclude(artist__name__exact="")
                    .filter(artist__name__icontains=query)
                    .select_related("artist")
                )
                local_music_artists_total = artist_trackers.count()
                local_music_artists = list(artist_trackers.order_by("artist__name")[:local_results_limit])

                album_trackers = (
                    AlbumTracker.objects.filter(user=request.user)
                    .exclude(album__title__isnull=True)
                    .exclude(album__title__exact="")
                    .filter(
                        Q(album__title__icontains=query)
                        | Q(album__artist__name__icontains=query),
                    )
                    .select_related("album", "album__artist")
                )
                local_music_albums_total = album_trackers.count()
                local_music_albums = list(album_trackers.order_by("album__title")[:local_results_limit])

                local_results_total = local_music_artists_total + local_music_albums_total
                local_results_kind = "music"
            else:
                local_queryset = BasicMedia.objects.get_media_list(
                    request.user,
                    media_type,
                    MediaStatusChoices.ALL,
                    "title",
                    search=query,
                    direction="asc",
                )
                local_media = list(local_queryset)
                if media_type == MediaTypes.TV.value and getattr(
                    request.user,
                    "anime_library_mode",
                    MediaTypes.ANIME.value,
                ) == MediaTypes.ANIME.value:
                    local_media = [
                        media
                        for media in local_media
                        if getattr(getattr(media, "item", None), "library_media_type", None)
                        != MediaTypes.ANIME.value
                    ]
                elif media_type == MediaTypes.ANIME.value and getattr(
                    request.user,
                    "anime_library_mode",
                    MediaTypes.ANIME.value,
                ) in {MediaTypes.ANIME.value, "both"}:
                    grouped_local_media = list(
                        BasicMedia.objects.get_media_list(
                            request.user,
                            MediaTypes.TV.value,
                            MediaStatusChoices.ALL,
                            "title",
                            search=query,
                            direction="asc",
                        ),
                    )
                    grouped_local_media = [
                        media
                        for media in grouped_local_media
                        if getattr(getattr(media, "item", None), "library_media_type", None)
                        == MediaTypes.ANIME.value
                    ]
                    _mark_grouped_anime_route(grouped_local_media)
                    local_media.extend(grouped_local_media)
                    local_media.sort(
                        key=lambda media: getattr(
                            getattr(media, "item", None),
                            "title",
                            "",
                        ).lower(),
                    )

                local_results_total = len(local_media)
                local_media = local_media[:local_results_limit]
                BasicMedia.objects.annotate_max_progress(local_media, media_type)
                local_results = [
                    {
                        "item": media.item,
                        "media": media,
                        "matched_title": _matched_title(media.item, query),
                    }
                    for media in local_media
                ]
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Local search failed: %s", exception_summary(exc))

    source_options = metadata_resolution.available_metadata_sources(media_type)
    default_source = metadata_resolution.metadata_default_source(
        request.user,
        media_type,
    )
    # only receives source when searching with secondary source
    source = request.GET.get("source", default_source)
    if source not in {option.value for option in source_options} and source_options:
        source = source_options[0].value

    search_page = 1 if media_type == MediaTypes.MUSIC.value else page
    data = services.search(media_type, query, search_page, source)

    if media_type == MediaTypes.MUSIC.value:
        context = {
            "user": request.user,
            "data": data,
            "music_online_artists": data.get("artists", []),
            "music_online_releases": data.get("releases", []),
            "source": source,
            "source_options": source_options,
            "media_type": media_type,
            "layout": layout,
            "local_results": local_results,
            "local_results_total": local_results_total,
            "local_results_limit": local_results_limit,
            "local_results_kind": local_results_kind,
            "local_music_artists": local_music_artists,
            "local_music_artists_total": local_music_artists_total,
            "local_music_albums": local_music_albums,
            "local_music_albums_total": local_music_albums_total,
        }
        return render(request, "app/search.html", context)

    # Enrich search results with user tracking data
    if data.get("results"):
        data["results"] = helpers.enrich_items_with_user_data(
            request,
            data["results"],
            section_name="search",
        )
        for result in data["results"]:
            result["matched_title"] = _matched_title(result.get("item"), query)

    context = {
        "user": request.user,
        "data": data,
        "source": source,
        "source_options": source_options,
        "media_type": media_type,
        "layout": layout,
        "local_results": local_results,
        "local_results_total": local_results_total,
        "local_results_limit": local_results_limit,
        "local_results_kind": local_results_kind,
    }

    return render(request, "app/search.html", context)
