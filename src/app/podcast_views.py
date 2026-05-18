import logging
from datetime import UTC

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from app import helpers
from app.discover import tab_cache as discover_tab_cache
from app.log_safety import exception_summary
from app.models import Item, MediaTypes, Sources, Status
from app.track_modal_views import _DummyPodcastWrapper, _render_podcast_show_track_modal

logger = logging.getLogger(__name__)


@login_not_required
@require_GET
def podcast_show_detail(request, show_id):
    """Return the detail page for a podcast show."""
    from datetime import datetime

    from django.db.models import DateTimeField, Value
    from django.db.models.functions import Coalesce

    from app.models import Podcast, PodcastEpisode, PodcastShow, PodcastShowTracker

    show = get_object_or_404(PodcastShow, id=show_id)

    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()

    episodes = PodcastEpisode.objects.filter(show=show).annotate(
        published_or_old=Coalesce(
            "published",
            Value(datetime(1970, 1, 1, tzinfo=UTC),
                  output_field=DateTimeField()),
        ),
    ).order_by("-published_or_old", "-episode_number")

    user_podcasts = list(Podcast.objects.filter(
        user=request.user,
        show=show,
    ).select_related("episode", "item"))

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
    """Return the tracking form modal for a podcast show."""
    from app.models import PodcastShow

    show = get_object_or_404(PodcastShow, id=show_id)
    return _render_podcast_show_track_modal(request, show)


@require_GET
def podcast_episodes_api(request, show_id):
    """API endpoint for paginated podcast episodes.

    Returns HTML fragments for infinite scroll if format=html, otherwise JSON.
    """
    from datetime import datetime

    from django.db.models import DateTimeField, Value
    from django.db.models.functions import Coalesce

    from app.models import Podcast, PodcastEpisode, PodcastShow

    show = get_object_or_404(PodcastShow, id=show_id)
    format_type = request.GET.get("format", "json")

    try:
        page = int(request.GET.get("page", 1))
        page_size = int(request.GET.get("page_size", 20))
    except ValueError:
        page = 1
        page_size = 20

    episodes_qs = PodcastEpisode.objects.filter(show=show).annotate(
        published_or_old=Coalesce(
            "published",
            Value(datetime(1970, 1, 1, tzinfo=UTC),
                  output_field=DateTimeField()),
        ),
    ).order_by("-published_or_old", "-episode_number")
    total_count = episodes_qs.count()

    start = (page - 1) * page_size
    end = start + page_size
    episodes = episodes_qs[start:end]

    user_podcasts = list(Podcast.objects.filter(
        user=request.user,
        show=show,
    ).select_related("episode", "item").order_by("episode_id", "-created_at"))

    episode_podcast_map = {}
    for podcast in user_podcasts:
        if podcast.episode_id:
            if podcast.episode_id not in episode_podcast_map:
                episode_podcast_map[podcast.episode_id] = podcast

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

    enriched_episodes_raw = helpers.enrich_items_with_user_data(
        request,
        episode_items_data,
        user=request.user,
    )

    has_more = end < total_count
    next_page = page + 1 if has_more else None

    if format_type == "html":
        from django.template.loader import render_to_string

        episode_list = []
        for episode_obj in episodes:
            enriched = None
            for e in enriched_episodes_raw:
                if e["item"]["media_id"] == episode_obj.episode_uuid:
                    enriched = e
                    break

            duration_str = ""
            if episode_obj.duration:
                hours = episode_obj.duration // 3600
                minutes = (episode_obj.duration % 3600) // 60
                if hours > 0:
                    duration_str = f"{hours}h {minutes}m"
                else:
                    duration_str = f"{minutes}m"

            user_podcast = episode_podcast_map.get(episode_obj.id)

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

            all_history = []
            if user_podcast:
                all_history = list(user_podcast.history.filter(end_date__isnull=False).order_by("-end_date")[:10])

                class PodcastHistoryWrapper:
                    def __init__(self, podcast, item, history_list):
                        self.item = item
                        self.id = podcast.id
                        self._history_list = history_list
                        self.in_progress_instance_id = podcast.id if not podcast.end_date else None

                    @property
                    def completed_play_count(self):
                        return len(self._history_list)

                    @property
                    def has_in_progress_entry(self):
                        return bool(self.in_progress_instance_id)

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

                item = episode_items_map.get(episode_obj.episode_uuid)
                podcast_wrapper = PodcastHistoryWrapper(user_podcast, enriched["item"] if enriched else item, all_history)
            else:
                item = episode_items_map.get(episode_obj.episode_uuid)
                podcast_wrapper = _DummyPodcastWrapper(enriched["item"] if enriched else item)

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
        response["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response

    # Return JSON
    episode_list = []
    for episode_obj in episodes:
        enriched = None
        for e in enriched_episodes_raw:
            if e["item"]["media_id"] == episode_obj.episode_uuid:
                enriched = e
                break

        duration_str = ""
        if episode_obj.duration:
            hours = episode_obj.duration // 3600
            minutes = (episode_obj.duration % 3600) // 60
            if hours > 0:
                duration_str = f"{hours}h {minutes}m"
            else:
                duration_str = f"{minutes}m"

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
    from app.forms import PodcastShowTrackerForm
    from app.models import PodcastShow, PodcastShowTracker

    show_id = request.POST.get("show_id")
    show = get_object_or_404(PodcastShow, id=show_id)
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.PODCAST.value,
    )

    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()

    form = PodcastShowTrackerForm(request.POST, instance=tracker, user=request.user)
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
        redirect_response = redirect(next_url)
    else:
        redirect_response = redirect("podcast_show_detail", show_id=show.id)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": redirect_response.url})
    return redirect_response


@require_POST
def podcast_show_delete(request):
    """Delete a podcast show tracker - mirrors artist_delete."""
    from app.models import PodcastShow, PodcastShowTracker

    show_id = request.POST.get("show_id")
    show = get_object_or_404(PodcastShow, id=show_id)
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.PODCAST.value,
    )

    tracker = PodcastShowTracker.objects.filter(user=request.user, show=show).first()
    if tracker:
        tracker.delete()
        messages.success(request, f"Removed {show.title} from your library")

    next_url = request.GET.get("next", "")
    if next_url:
        redirect_response = redirect(next_url)
    else:
        redirect_response = redirect("podcast_show_detail", show_id=show.id)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=204, headers={"HX-Redirect": redirect_response.url})
    return redirect_response


@require_POST
def podcast_mark_all_played(request, show_id):
    """Mark all episodes of this podcast currently in the library as completed on their release date.

    Episodes not yet imported from Pocket Casts are not included — run a Pocket Casts
    import first to fetch the full episode list.
    """
    import hashlib

    from django.utils import timezone

    import events
    from app.mixins import disable_fetch_releases
    from app.models import Podcast, PodcastEpisode, PodcastShow, PodcastShowTracker
    from integrations import podcast_rss

    show = get_object_or_404(PodcastShow, id=show_id)

    tracker, _ = PodcastShowTracker.objects.get_or_create(
        user=request.user,
        show=show,
        defaults={"status": Status.IN_PROGRESS.value},
    )

    if show.rss_feed_url:
        try:
            episodes_data = podcast_rss.fetch_episodes_from_rss(show.rss_feed_url, limit=None)

            seen_uuids = set(
                PodcastEpisode.objects.filter(show=show).values_list("episode_uuid", flat=True)
            )
            for episode_data in episodes_data:
                episode_uuid = episode_data.get("guid")
                if not episode_uuid:
                    uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                    episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                if episode_uuid in seen_uuids:
                    continue

                exists = False
                if episode_data.get("title") and episode_data.get("published"):
                    exists = PodcastEpisode.objects.filter(
                        show=show,
                        title__iexact=episode_data["title"].strip(),
                        published__date=episode_data["published"].date(),
                    ).exists()

                if not exists:
                    try:
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
                        seen_uuids.add(episode_uuid)
                    except Exception:
                        logger.debug("Skipping duplicate episode UUID %s for show %s", episode_uuid, show.title)
        except Exception as e:
            logger.warning(
                "Failed to fetch full episode list from RSS feed for %s: %s",
                show.title,
                exception_summary(e),
            )

    all_episodes = PodcastEpisode.objects.filter(show=show)

    completed_episodes = set(
        Podcast.objects.filter(
            user=request.user,
            show=show,
            episode__isnull=False,
            end_date__isnull=False,
        ).values_list("episode_id", flat=True),
    )

    unplayed_episodes = all_episodes.exclude(id__in=completed_episodes)

    if not unplayed_episodes.exists():
        messages.info(request, f"All episodes of {show.title} are already marked as played")
        return redirect("media_details", source=Sources.POCKETCASTS.value, media_type=MediaTypes.PODCAST.value, media_id=show.podcast_uuid, title=show.slug or show.title)

    created_count = 0
    items_created = []

    with disable_fetch_releases():
        for episode in unplayed_episodes:
            runtime_minutes = episode.duration // 60 if episode.duration else None
            item_defaults = {
                "title": episode.title,
                "image": show.image or settings.IMG_NONE,
            }
            if runtime_minutes:
                item_defaults["runtime_minutes"] = runtime_minutes
            if episode.published:
                item_defaults["release_datetime"] = episode.published

            item, item_created = Item.objects.get_or_create(
                media_id=episode.episode_uuid,
                source=Sources.POCKETCASTS.value,
                media_type=MediaTypes.PODCAST.value,
                defaults=item_defaults,
            )

            if not item_created:
                update_fields = []
                if runtime_minutes and item.runtime_minutes != runtime_minutes:
                    item.runtime_minutes = runtime_minutes
                    update_fields.append("runtime_minutes")
                if episode.published and item.release_datetime != episode.published:
                    item.release_datetime = episode.published
                    update_fields.append("release_datetime")
                if update_fields:
                    item.save(update_fields=update_fields)

            if item_created:
                items_created.append(item)

            end_date = episode.published if episode.published else timezone.now()

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

    if items_created:
        events.tasks.reload_calendar.apply_async(
            kwargs={"item_ids": [item.id for item in items_created]},
            countdown=3,
        )

    episode_word = "episodes" if created_count != 1 else "episode"
    messages.success(
        request,
        f"Marked {created_count} {episode_word} of {show.title} as played",
    )

    return redirect("media_details", source=Sources.POCKETCASTS.value, media_type=MediaTypes.PODCAST.value, media_id=show.podcast_uuid, title=show.slug or show.title)


@require_POST
def podcast_save(request):
    """Handle adding a play for a podcast episode - mirrors song_save for music."""
    from django.utils import timezone
    from django.utils.dateparse import parse_date, parse_datetime

    from app.models import Podcast, PodcastEpisode, PodcastShow

    episode_uuid = request.POST.get("episode_uuid")
    show_id = request.POST.get("show_id")
    episode_id = request.POST.get("episode_id")
    end_date_str = request.POST.get("end_date")
    discover_tab_cache.mark_active_from_request(
        request,
        fallback_media_type=MediaTypes.PODCAST.value,
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

    show = get_object_or_404(PodcastShow, id=show_id)
    episode = get_object_or_404(PodcastEpisode, id=episode_id) if episode_id else None

    runtime_minutes = None
    if episode and episode.duration:
        runtime_minutes = episode.duration // 60

    item_defaults = {
        "title": episode.title if episode else "Unknown Episode",
        "image": show.image or settings.IMG_NONE,
    }
    if runtime_minutes:
        item_defaults["runtime_minutes"] = runtime_minutes
    if episode and episode.published:
        item_defaults["release_datetime"] = episode.published

    item, created = Item.objects.get_or_create(
        media_id=episode_uuid,
        source=Sources.POCKETCASTS.value,
        media_type=MediaTypes.PODCAST.value,
        defaults=item_defaults,
    )
    if not created:
        update_fields = []
        if runtime_minutes and item.runtime_minutes != runtime_minutes:
            item.runtime_minutes = runtime_minutes
            update_fields.append("runtime_minutes")
        if episode and episode.published and item.release_datetime != episode.published:
            item.release_datetime = episode.published
            update_fields.append("release_datetime")
        if update_fields:
            item.save(update_fields=update_fields)

    existing_podcast = Podcast.objects.filter(
        user=request.user,
        item=item,
    ).first()

    if existing_podcast:
        latest_history = existing_podcast.history.filter(end_date__isnull=False).order_by("-end_date").first()
        if latest_history and latest_history.end_date and end_date:
            time_diff = abs((end_date - latest_history.end_date).total_seconds())
            if time_diff < 300:
                logger.debug("Skipping duplicate podcast history entry (time difference: %d seconds)", time_diff)
                messages.info(request, f"Play already recorded for {episode.title if episode else 'episode'}")
            else:
                existing_podcast.end_date = end_date
                if runtime_minutes and existing_podcast.progress != runtime_minutes:
                    existing_podcast.progress = runtime_minutes
                existing_podcast.save()
                messages.success(request, f"Added play for {episode.title if episode else 'episode'}")
        else:
            existing_podcast.end_date = end_date
            if runtime_minutes and existing_podcast.progress != runtime_minutes:
                existing_podcast.progress = runtime_minutes
            existing_podcast.save()
            messages.success(request, f"Added play for {episode.title if episode else 'episode'}")
    else:
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

    if request.headers.get("HX-Request"):
        from django.template.loader import render_to_string

        episode_obj = episode
        if not episode_obj:
            return HttpResponse("Episode not found", status=404)

        user_podcast = Podcast.objects.filter(
            user=request.user,
            show=show,
            episode=episode_obj,
        ).order_by("-created_at").first()

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

        duration_str = ""
        if episode_obj.duration:
            hours = episode_obj.duration // 3600
            minutes = (episode_obj.duration % 3600) // 60
            if hours > 0:
                duration_str = f"{hours}h {minutes}m"
            else:
                duration_str = f"{minutes}m"

        all_history = []
        if user_podcast:
            all_history = list(user_podcast.history.filter(end_date__isnull=False).order_by("-end_date")[:10])

            class PodcastHistoryWrapper:
                def __init__(self, podcast, item, history_list):
                    self.item = item
                    self.id = podcast.id
                    self._history_list = history_list
                    self.in_progress_instance_id = podcast.id if not podcast.end_date else None

                @property
                def completed_play_count(self):
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

                @property
                def has_in_progress_entry(self):
                    return bool(self.in_progress_instance_id)

            podcast_wrapper = PodcastHistoryWrapper(user_podcast, item, all_history)
        else:
            podcast_wrapper = _DummyPodcastWrapper(item)

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
        response["HX-Trigger"] = "closeModal"
        return response

    from django.utils.text import slugify

    return redirect(
        "media_details",
        source=Sources.POCKETCASTS.value,
        media_type=MediaTypes.PODCAST.value,
        media_id=show.podcast_uuid,
        title=show.slug or slugify(show.title),
    )
