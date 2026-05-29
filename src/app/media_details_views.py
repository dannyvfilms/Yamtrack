import hashlib
import json
import logging
import time
from datetime import datetime

from django.apps import apps
from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.utils import timezone
from django.shortcuts import render
from django.views.decorators.http import require_GET

from app import (
    config,
    custom_metadata,
    credits,
    helpers,
    metadata_utils,
    statistics_cache,
)
from app import statistics as stats
from app.activity_builders import (
    DETAIL_EPISODES_PER_PAGE,
    _build_detail_activity_state,
    _build_detail_activity_subtitle,
    _detail_episode_page_label,
    _get_game_lengths_refresh_lock,
    _normalize_detail_episode_actions,
    _paginate_detail_episodes,
    _queue_game_lengths_refresh,
    _should_queue_game_lengths_refresh,
)
from app.db_retry import run_retryable_db_operation
from app.detail_builders import (
    _apply_cached_hltb_link,
    _build_detail_link_sections,
    _build_game_lengths_context,
    _build_trakt_popularity_context,
)
from app.log_safety import exception_summary
from app.metadata_sync_views import _build_flat_anime_episode_preview
from app.models import (
    TV,
    Album,
    Anime,
    BasicMedia,
    CreditRoleType,
    Episode,
    Item,
    MediaTypes,
    PodcastShow,
    Season,
    Sources,
    Status,
    Track,
)
from app.providers import igdb, services, tmdb
from app.services import metadata_resolution
from app.services import trakt_popularity as trakt_popularity_service
from app.tag_views import (
    _build_detail_tag_sections,
    _detail_request_url,
    _resolve_detail_tag_genres,
)
from app.track_modal_views import _DummyPodcastWrapper
from app.view_constants import DETAIL_SECONDARY_FRAGMENT
from lists.models import CustomList

logger = logging.getLogger(__name__)


def _get_tv_runtime_display_fallback(detail_item, media_metadata):
    """Return a best-effort runtime string for TV details when provider runtime is missing."""
    if not detail_item or detail_item.media_type != MediaTypes.TV.value:
        return None

    runtime_minutes = getattr(detail_item, "runtime_minutes", None)
    if runtime_minutes and runtime_minutes < 999998:
        return tmdb.get_readable_duration(runtime_minutes)

    if detail_item.runtime:
        parsed_runtime = stats.parse_runtime_to_minutes(detail_item.runtime)
        if parsed_runtime and parsed_runtime > 0:
            return tmdb.get_readable_duration(parsed_runtime)

    episode_runtimes = list(
        Item.objects.filter(
            media_id=detail_item.media_id,
            source=detail_item.source,
            media_type=MediaTypes.EPISODE.value,
            runtime_minutes__isnull=False,
        ).exclude(
            runtime_minutes__in=[999998, 999999],
        ).values_list("runtime_minutes", flat=True),
    )
    if episode_runtimes:
        return tmdb.get_readable_duration(round(sum(episode_runtimes) / len(episode_runtimes)))

    details = media_metadata.get("details") if isinstance(media_metadata, dict) else {}
    if not isinstance(details, dict):
        details = {}

    max_seasons = details.get("seasons")
    try:
        max_seasons = int(max_seasons)
    except (TypeError, ValueError):
        max_seasons = 5
    max_seasons = max(1, min(max_seasons, 20))

    for season_num in range(1, max_seasons + 1):
        cached_season_data = cache.get(f"tmdb_season_{detail_item.media_id}_{season_num}")
        runtime_str = ((cached_season_data or {}).get("details") or {}).get("runtime")
        runtime_minutes = stats.parse_runtime_to_minutes(runtime_str)
        if runtime_minutes and runtime_minutes > 0:
            return tmdb.get_readable_duration(runtime_minutes)

    return None


@login_not_required
@require_GET
def media_details(
    request, source, media_type, media_id, title,
):
    """Return the details page for a media item."""
    detail_view_started_at = time.perf_counter()
    render_secondary_only = (
        request.GET.get("fragment") == DETAIL_SECONDARY_FRAGMENT
        and media_type != MediaTypes.PODCAST.value
    )
    defer_detail_secondary = (
        not render_secondary_only and media_type != MediaTypes.PODCAST.value
    )
    detail_return_url = _detail_request_url(request)
    detail_secondary_fragment_url = _detail_request_url(
        request,
        fragment=DETAIL_SECONDARY_FRAGMENT,
    )

    # Treat all anonymous views as public (no user-specific data/actions)
    is_anonymous = not request.user.is_authenticated
    public_view = is_anonymous
    public_list_view = request.GET.get("public_view") == "1" and is_anonymous

    # For public views, find a public list containing this item to get the owner
    list_owner = None
    if public_list_view:
        try:
            item = Item.objects.filter(
                media_id=media_id,
                source=source,
                media_type=media_type,
            ).first()
            if item:
                public_list = CustomList.objects.filter(
                    visibility="public",
                    items=item,
                ).select_related("owner").first()
                if public_list:
                    list_owner = public_list.owner
        except Exception:
            # If we can't find a list owner, list_owner stays None
            pass

    detail_persistence_deferred = False
    detail_db_max_retries = 0

    def _mark_detail_persistence_deferred(_error=None):
        nonlocal detail_persistence_deferred
        detail_persistence_deferred = True

    def _best_effort_detail_db_work(operation, *, fallback=None, operation_name):
        return run_retryable_db_operation(
            operation,
            mode="best_effort",
            fallback=fallback,
            operation_name=operation_name,
            operation_logger=logger,
            max_retries=detail_db_max_retries,
            on_deferred=_mark_detail_persistence_deferred,
        )

    def _best_effort_detail_followup(
        operation,
        *,
        operation_name,
        fallback=False,
    ):
        try:
            return operation()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Skipping detail follow-up %s for %s due to error",
                operation_name,
                request.path,
                exc_info=True,
            )
            _mark_detail_persistence_deferred()
            return fallback

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
                                    logger.debug(
                                        "Failed to fetch show metadata from RSS: %s",
                                        exception_summary(e),
                                    )

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
                                import hashlib

                                episodes_data = podcast_rss.fetch_episodes_from_rss(rss_feed_url, limit=None)
                                seen_uuids = set()

                                for episode_data in episodes_data:
                                    episode_uuid = episode_data.get("guid")
                                    if not episode_uuid:
                                        uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                                        episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                                    if episode_uuid in seen_uuids:
                                        continue

                                    # Check for existing match within this show by title + date
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
                                    "Failed to fetch episodes from RSS feed for %s: %s",
                                    show.title,
                                    exception_summary(e),
                                )

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
                    logger.error(
                        "Failed to enrich podcast from iTunes metadata: %s",
                        exception_summary(e),
                        exc_info=True,
                    )
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
                        episode_uuid = episode_data.get("guid")
                        if not episode_uuid:
                            uuid_str = f"{episode_data.get('title', '')}{episode_data.get('published', '')}"
                            episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                        if episode_uuid in existing_uuids:
                            continue

                        # Check for a match within this show by title + date
                        episode = None
                        if episode_data.get("title") and episode_data.get("published"):
                            episode = PodcastEpisode.objects.filter(
                                show=show,
                                title__iexact=episode_data["title"].strip(),
                                published__date=episode_data["published"].date(),
                            ).first()

                        if not episode:
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
                                new_episodes_count += 1
                                existing_uuids.add(episode_uuid)
                            except Exception:
                                logger.debug("Skipping duplicate episode UUID %s for show %s", episode_uuid, show.title)

                    if new_episodes_count > 0:
                        logger.info("Fetched %d additional episodes for show %s (ID: %d)", new_episodes_count, show.title, show.id)
                except Exception as e:
                    logger.warning(
                        "Failed to refresh episode list from RSS feed for show %s: %s",
                        show.title,
                        exception_summary(e),
                    )

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
            else:
                user_podcasts = []
                total_listened = 0
                total_minutes = 0

            # Build episode items - create Item objects for enrichment
            # Initially load first 20 episodes, rest will be loaded via infinite scroll
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
                    podcast_wrapper = _DummyPodcastWrapper(enriched["item"])

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
            media_metadata.setdefault("source_url", None)
            media_metadata.setdefault("tracking_source_url", None)
            media_metadata.setdefault("display_source_url", None)

            # For pagination, calculate if there are more episodes
            total_episodes_count = episodes.count()
            has_more = total_episodes_count > initial_limit
            next_page = 2 if has_more else None
            media_metadata["max_progress"] = total_episodes_count

            podcast_play_stats = None
            activity_subtitle = None
            if not public_view and user_podcasts:
                range_start_candidates = []
                range_end_candidates = []
                completed_entries = 0
                total_progress_seconds = 0

                for entry in user_podcasts:
                    range_start = entry.start_date or entry.end_date or entry.created_at
                    range_end = entry.end_date or entry.start_date or entry.created_at
                    if range_start:
                        range_start_candidates.append(range_start)
                    if range_end:
                        range_end_candidates.append(range_end)
                    if entry.end_date or entry.status == Status.COMPLETED.value:
                        completed_entries += 1
                    total_progress_seconds += int(entry.progress or 0)

                total_listened_minutes = total_progress_seconds // 60
                podcast_play_stats = {
                    "first_played": min(range_start_candidates) if range_start_candidates else None,
                    "last_played": max(range_end_candidates) if range_end_candidates else None,
                    "total_minutes": total_listened_minutes,
                    "total_hours": total_listened_minutes // 60,
                    "total_minutes_remainder": total_listened_minutes % 60,
                    "total_plays": completed_entries or total_listened,
                }
                activity_subtitle = _build_detail_activity_subtitle(
                    MediaTypes.PODCAST.value,
                    media_metadata,
                    tracker,
                    podcast_play_stats,
                )

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
                "play_stats": podcast_play_stats,
                "activity_subtitle": activity_subtitle,
                "IMG_NONE": settings.IMG_NONE,
                "TRACK_TIME": True,
                "has_more_episodes": has_more,  # Keep for backward compatibility
                "has_more": has_more,  # For fragment compatibility
                "next_page": next_page,
                "show_id": show.id,  # For API endpoint
            }
            return render(request, "app/media_details.html", context)

    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
    )
    detail_item_lookup = {
        "media_id": media_id,
        "source": source,
        "media_type": tracking_media_type,
    }
    if metadata_resolution.is_grouped_anime_route(media_type, source=source):
        detail_item_lookup["library_media_type"] = MediaTypes.ANIME.value

    media_metadata = services.get_media_metadata(media_type, media_id, source)
    if isinstance(media_metadata, dict):
        media_metadata.update(Item.title_fields_from_metadata(media_metadata))

    detail_item = Item.objects.filter(**detail_item_lookup).first()

    if (
        render_secondary_only
        and detail_item is None
        and source == Sources.IGDB.value
        and media_type == MediaTypes.GAME.value
        and isinstance(media_metadata, dict)
    ):
        detail_item_outcome = _best_effort_detail_db_work(
            lambda: Item.objects.get_or_create(
                media_id=media_id,
                source=source,
                media_type=media_type,
                defaults={
                    **Item.title_fields_from_metadata(media_metadata),
                    "image": media_metadata.get("image") or settings.IMG_NONE,
                },
            ),
            fallback=lambda: (None, False),
            operation_name="IGDB detail item create",
        )
        detail_item, _ = detail_item_outcome.value

    # When the user prefers original titles, aggressively refresh stale TMDB cache
    # if we don't yet have an original title. This lets details-page opens backfill
    # better title variants that can then propagate across the UI.
    tmdb_detail_cache_key = f"{Sources.TMDB.value}_{tracking_media_type}_{media_id}"
    should_refresh_tmdb_titles = (
        request.user.is_authenticated
        and source == Sources.TMDB.value
        and tracking_media_type in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
        )
        and getattr(request.user, "title_display_preference", "localized") == "original"
        and isinstance(media_metadata, dict)
        and not media_metadata.get("original_title")
    )
    if render_secondary_only and should_refresh_tmdb_titles:
        cache.delete(tmdb_detail_cache_key)
        media_metadata = services.get_media_metadata(media_type, media_id, source)
        if isinstance(media_metadata, dict):
            media_metadata.update(Item.title_fields_from_metadata(media_metadata))

    should_refresh_tmdb_tv_credits = (
        source == Sources.TMDB.value
        and tracking_media_type in (MediaTypes.TV.value, MediaTypes.SEASON.value)
        and isinstance(media_metadata, dict)
        and not media_metadata.get("cast")
        and not media_metadata.get("crew")
    )
    if render_secondary_only and should_refresh_tmdb_tv_credits:
        cache.delete(tmdb_detail_cache_key)
        media_metadata = services.get_media_metadata(media_type, media_id, source)
        if isinstance(media_metadata, dict):
            media_metadata.update(Item.title_fields_from_metadata(media_metadata))

    identity_media_metadata = media_metadata

    if render_secondary_only and detail_item and isinstance(media_metadata, dict):
        title_fields = Item.title_fields_from_metadata(
            media_metadata,
            fallback_title=detail_item.title,
        )
        update_fields = []
        normalized_existing_titles = {
            "title": Item._normalize_title_value(detail_item.title),
            "original_title": Item._normalize_title_value(detail_item.original_title),
            "localized_title": Item._normalize_title_value(detail_item.localized_title),
        }
        for field_name, normalized_value in normalized_existing_titles.items():
            if normalized_value and getattr(detail_item, field_name) != normalized_value:
                setattr(detail_item, field_name, normalized_value)
                update_fields.append(field_name)

        for field_name in ("title", "original_title", "localized_title"):
            desired_value = title_fields.get(field_name)
            if desired_value and getattr(detail_item, field_name) != desired_value:
                setattr(detail_item, field_name, desired_value)
                update_fields.append(field_name)
        if update_fields:
            _best_effort_detail_db_work(
                lambda: detail_item.save(update_fields=update_fields),
                operation_name="detail item title sync",
            )

    # Persist series info for books if available
    if (
        render_secondary_only
        and media_type == MediaTypes.BOOK.value
        and isinstance(media_metadata, dict)
    ):
        try:
            item = Item.objects.get(
                media_id=media_id,
                source=source,
                media_type=media_type,
            )
            update_fields = []
            if media_metadata.get("series_name") and item.series_name != media_metadata["series_name"]:
                item.series_name = media_metadata["series_name"]
                update_fields.append("series_name")
            if media_metadata.get("series_position") is not None and item.series_position != media_metadata["series_position"]:
                item.series_position = media_metadata["series_position"]
                update_fields.append("series_position")
            
            if update_fields:
                _best_effort_detail_db_work(
                    lambda: item.save(update_fields=update_fields),
                    operation_name="detail book-series sync",
                )
        except Item.DoesNotExist:
            pass

    igdb_game_studios_missing = (
        source == Sources.IGDB.value
        and tracking_media_type == MediaTypes.GAME.value
        and isinstance(media_metadata, dict)
        and "studios_full" not in media_metadata
    )

    if isinstance(media_metadata, dict):
        media_metadata.setdefault("cast", [])
        media_metadata.setdefault("crew", [])
        media_metadata.setdefault("studios_full", [])

    metadata_resolution_result = None
    should_resolve_metadata = bool(
        detail_item
        and isinstance(media_metadata, dict)
        and (
            media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value)
            or custom_metadata.supports_custom_provider(media_type)
        )
    )
    if render_secondary_only and should_resolve_metadata:
        metadata_resolution_result = metadata_resolution.resolve_detail_metadata(
            request.user if request.user.is_authenticated else None,
            item=detail_item,
            route_media_type=media_type,
            media_id=media_id,
            source=source,
            base_metadata=media_metadata,
            persistence_mode="best_effort",
            retry_max_retries=detail_db_max_retries,
            on_persistence_deferred=_mark_detail_persistence_deferred,
        )
        media_metadata = metadata_resolution_result.header_metadata
        media_metadata.update(
            Item.title_fields_from_metadata(
                media_metadata,
                fallback_title=detail_item.title if detail_item else "",
            ),
        )

    # For podcasts, ensure source is in metadata dict (fixes KeyError in template)
    if media_type == MediaTypes.PODCAST.value and isinstance(media_metadata, dict):
        media_metadata["source"] = source
        media_metadata["media_type"] = media_type
        media_metadata["media_id"] = media_id

    if (
        render_secondary_only
        and source == Sources.TMDB.value
        and tracking_media_type in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
        )
        and isinstance(media_metadata, dict)
    ):
        if detail_item:
            metadata_update_fields = metadata_utils.apply_item_metadata(
                detail_item,
                identity_media_metadata,
            )
            if metadata_update_fields:
                detail_item.metadata_fetched_at = timezone.now()
                metadata_update_fields.append("metadata_fetched_at")
                _best_effort_detail_db_work(
                    lambda: detail_item.save(update_fields=metadata_update_fields),
                    operation_name="TMDB detail metadata sync",
                )
            missing_people = not detail_item.person_credits.exists()
            missing_studios = not detail_item.studio_credits.exists()
            if missing_people or missing_studios:
                _best_effort_detail_db_work(
                    lambda: credits.sync_item_credits_from_metadata(
                        detail_item,
                        media_metadata,
                    ),
                    operation_name="TMDB detail credits sync",
                )

    should_refresh_igdb_game_studios = (
        source == Sources.IGDB.value
        and tracking_media_type == MediaTypes.GAME.value
        and detail_item is not None
        and igdb_game_studios_missing
    )
    if render_secondary_only and should_refresh_igdb_game_studios:
        cache.delete(f"{source}_{tracking_media_type}_{media_id}")
        media_metadata = services.get_media_metadata(media_type, media_id, source)
        if isinstance(media_metadata, dict):
            media_metadata.update(Item.title_fields_from_metadata(media_metadata))

    if (
        render_secondary_only
        and detail_item
        and source == Sources.IGDB.value
        and tracking_media_type == MediaTypes.GAME.value
        and isinstance(media_metadata, dict)
        and media_metadata.get("studios_full")
    ):
        existing_studio_ids = {
            str(studio_credit.studio.source_studio_id)
            for studio_credit in detail_item.studio_credits.select_related("studio")
            if studio_credit.studio and studio_credit.studio.source_studio_id is not None
        }
        incoming_studio_ids = {
            str(studio.get("studio_id") or studio.get("id"))
            for studio in media_metadata.get("studios_full", [])
            if isinstance(studio, dict) and (studio.get("studio_id") or studio.get("id"))
        }
        if existing_studio_ids != incoming_studio_ids:
            _best_effort_detail_db_work(
                lambda: credits.sync_item_credits_from_metadata(
                    detail_item,
                    {
                        "studios_full": media_metadata.get("studios_full", []),
                    },
                ),
                operation_name="IGDB detail studio sync",
            )

    identity_media_metadata = media_metadata

    if (
        render_secondary_only
        and source == Sources.IGDB.value
        and tracking_media_type == MediaTypes.GAME.value
        and detail_item
        and isinstance(media_metadata, dict)
    ):
        metadata_update_fields = metadata_utils.apply_item_metadata(
            detail_item,
            identity_media_metadata,
        )
        if metadata_update_fields:
            detail_item.metadata_fetched_at = timezone.now()
            metadata_update_fields.append("metadata_fetched_at")
            _best_effort_detail_db_work(
                lambda: detail_item.save(update_fields=metadata_update_fields),
                operation_name="IGDB detail metadata sync",
            )

    _apply_cached_hltb_link(media_metadata, detail_item)

    game_lengths = (
        _build_game_lengths_context(detail_item)
        if source == Sources.IGDB.value and media_type == MediaTypes.GAME.value
        else None
    )
    if (
        game_lengths
        and game_lengths.get("source") == "igdb"
        and isinstance(media_metadata, dict)
        and media_metadata.get("source_url")
    ):
        game_lengths["source_url"] = media_metadata["source_url"]
    game_lengths_fetch_queued = False
    game_lengths_refresh_pending = False
    if render_secondary_only and _should_queue_game_lengths_refresh(detail_item):
        game_lengths_refresh_pending = (
            _get_game_lengths_refresh_lock(
                detail_item,
                force=False,
                fetch_hltb=True,
            )
            is not None
        )
        if not game_lengths_refresh_pending:
            game_lengths_fetch_queued = _best_effort_detail_followup(
                lambda: _queue_game_lengths_refresh(
                    detail_item,
                    force=False,
                    fetch_hltb=True,
                ),
                operation_name="game lengths refresh enqueue",
                fallback=False,
            )
            game_lengths_refresh_pending = game_lengths_fetch_queued or (
                _get_game_lengths_refresh_lock(
                    detail_item,
                    force=False,
                    fetch_hltb=True,
                )
                is not None
            )
    trakt_score = _build_trakt_popularity_context(detail_item, media_type)

    author_detail_keys = ("author", "authors", "people")
    authors_linked = []
    if (
        render_secondary_only
        and media_type in (
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        )
        and isinstance(media_metadata, dict)
    ):
        def _collect_authors_linked(metadata_payload):
            linked = []

            if detail_item:
                author_credits = (
                    detail_item.person_credits.filter(
                        role_type=CreditRoleType.AUTHOR.value,
                    )
                    .select_related("person")
                    .order_by("sort_order", "person__name")
                )
                for author_credit in author_credits:
                    person = author_credit.person
                    linked.append(
                        {
                            "source": person.source,
                            "person_id": person.source_person_id,
                            "name": person.name,
                        },
                    )

            authors_full_payload = metadata_payload.get("authors_full")
            if not linked and isinstance(authors_full_payload, list):
                for author in authors_full_payload:
                    person_id = author.get("person_id") or author.get("id")
                    name = (author.get("name") or "").strip()
                    if person_id is None or not name:
                        continue
                    linked.append(
                        {
                            "source": source,
                            "person_id": str(person_id),
                            "name": name,
                        },
                    )

            return linked

        authors_full = media_metadata.get("authors_full")
        if detail_item and isinstance(authors_full, list):
            _best_effort_detail_db_work(
                lambda: credits.sync_item_author_credits(detail_item, authors_full),
                operation_name="detail author-credit sync",
            )

        authors_linked = _collect_authors_linked(media_metadata)

        details_payload = media_metadata.get("details")
        if not isinstance(details_payload, dict):
            details_payload = {}

        # Old provider cache entries may include plain author names but no authors_full
        # IDs, which prevents author links from rendering.
        should_refresh_author_cache = (
            not authors_linked
            and detail_item is not None
            and any(details_payload.get(key) for key in author_detail_keys)
            and not isinstance(media_metadata.get("authors_full"), list)
        )
        if should_refresh_author_cache:
            cache_key = f"{source}_{media_type}_{media_id}"
            cache.delete(cache_key)
            media_metadata = services.get_media_metadata(media_type, media_id, source)
            if isinstance(media_metadata, dict):
                media_metadata.setdefault("cast", [])
                media_metadata.setdefault("crew", [])
                media_metadata.setdefault("studios_full", [])
                refreshed_authors_full = media_metadata.get("authors_full")
                if detail_item and isinstance(refreshed_authors_full, list):
                    _best_effort_detail_db_work(
                        lambda: credits.sync_item_author_credits(
                            detail_item,
                            refreshed_authors_full,
                        ),
                        operation_name="refreshed detail author-credit sync",
                    )
                authors_linked = _collect_authors_linked(media_metadata)

    studio_detail_keys = ("studios", "companies")
    studios_linked = []

    def _collect_studios_linked(metadata_payload):
        linked = []

        if detail_item:
            studio_credits = (
                detail_item.studio_credits.select_related("studio")
                .order_by("sort_order", "studio__name")
            )
            for studio_credit in studio_credits:
                studio = studio_credit.studio
                linked.append(
                    {
                        "source": studio.source,
                        "studio_id": studio.source_studio_id,
                        "name": studio.name,
                        "logo": studio.logo,
                    },
                )

        studios_full_payload = metadata_payload.get("studios_full")
        if not linked and isinstance(studios_full_payload, list):
            for studio in studios_full_payload:
                studio_id = studio.get("studio_id") or studio.get("id")
                name = (studio.get("name") or "").strip()
                if studio_id is None or not name:
                    continue
                linked.append(
                    {
                        "source": source,
                        "studio_id": str(studio_id),
                        "name": name,
                        "logo": (studio.get("logo") or "").strip(),
                    },
                )

        return linked

    if render_secondary_only and isinstance(media_metadata, dict):
        studios_linked = _collect_studios_linked(media_metadata)

    # Prefer a stored poster/cover override when the tracked item has one.
    if (
        detail_item
        and isinstance(media_metadata, dict)
        and detail_item.image
        and detail_item.image != settings.IMG_NONE
    ):
        media_metadata["image"] = detail_item.image

    # For TV shows and grouped anime, enrich season cards from season-detail metadata.
    if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and isinstance(
        media_metadata,
        dict,
    ):
        details = media_metadata.get("details")
        if not isinstance(details, dict):
            details = {}
            media_metadata["details"] = details

        related = media_metadata.setdefault("related", {})
        seasons = related.setdefault("seasons", [])
        has_specials = any(season.get("season_number") == 0 for season in seasons)
        show_title = Item._normalize_title_value(media_metadata.get("title"))

        if (
            render_secondary_only
            and source == Sources.TMDB.value
            and media_metadata.get("tvdb_id")
            and not has_specials
        ):
            try:
                specials_metadata = services.get_media_metadata(
                    "tv_with_seasons",
                    media_id,
                    source,
                    [0],
                )
                if isinstance(specials_metadata, dict) and specials_metadata.get("season/0"):
                    enriched_related = specials_metadata.get("related") or {}
                    enriched_seasons = enriched_related.get("seasons")
                    if isinstance(enriched_seasons, list):
                        related["seasons"] = enriched_seasons
                        seasons = enriched_seasons
            except services.ProviderAPIError:
                logger.warning(
                    "Skipping specials enrichment for media_id=%s due to provider API error",
                    media_id,
                )

        if render_secondary_only and seasons and source in {Sources.TMDB.value, Sources.TVDB.value}:
            season_numbers = sorted(
                {
                    season_number
                    for season in seasons
                    for season_number in [season.get("season_number")]
                    if season_number is not None
                },
            )
            if season_numbers:
                try:
                    grouped_season_metadata = services.get_media_metadata(
                        "tv_with_seasons",
                        media_id,
                        source,
                        season_numbers,
                    )
                except services.ProviderAPIError:
                    grouped_season_metadata = None
                    logger.warning(
                        "Skipping season card enrichment for media_id=%s due to provider API error",
                        media_id,
                    )
                if isinstance(grouped_season_metadata, dict):
                    for season in seasons:
                        season_number = season.get("season_number")
                        season_payload = grouped_season_metadata.get(
                            f"season/{season_number}",
                        )
                        if not isinstance(season_payload, dict):
                            continue

                        detailed_title = Item._normalize_title_value(
                            season_payload.get("season_title"),
                        )
                        if detailed_title and detailed_title != show_title:
                            season["season_title"] = detailed_title
                        elif season_number == 0:
                            season["season_title"] = "Specials"
                        elif season_number is not None:
                            season["season_title"] = f"Season {season_number}"

                        payload_details = season_payload.get("details") or {}
                        if season.get("episode_count") in (None, ""):
                            season["episode_count"] = (
                                payload_details.get("episodes")
                                or season_payload.get("max_progress")
                            )
                        if season.get("max_progress") in (None, ""):
                            season["max_progress"] = season_payload.get(
                                "max_progress",
                            )
                        merged_details = dict(season.get("details") or {})
                        if merged_details.get("episodes") in (None, ""):
                            merged_details["episodes"] = (
                                season.get("episode_count")
                                or payload_details.get("episodes")
                                or season_payload.get("max_progress")
                            )
                        if merged_details.get("first_air_date") in (None, ""):
                            merged_details["first_air_date"] = payload_details.get(
                                "first_air_date",
                            )
                        season["details"] = merged_details
                        if season.get("first_air_date") in (None, ""):
                            season["first_air_date"] = payload_details.get(
                                "first_air_date",
                            )
                        if season.get("image") in (None, "", settings.IMG_NONE):
                            season["image"] = season_payload.get("image") or season.get(
                                "image",
                            )

        if not details.get("runtime"):
            fallback_runtime = _get_tv_runtime_display_fallback(detail_item, media_metadata)
            if fallback_runtime:
                details["runtime"] = fallback_runtime

        if render_secondary_only:
            tv_poster = media_metadata.get("image")
            if tv_poster:
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
            ),
        )
        if user_medias:
            def _activity_key(entry):
                dates = [d for d in (entry.end_date, entry.start_date) if d]
                primary_date = max(dates) if dates else entry.created_at
                return (primary_date, entry.start_date or entry.created_at, entry.created_at)

            user_medias.sort(key=_activity_key, reverse=True)
        current_instance = user_medias[0] if user_medias else None

    if current_instance is not None:
        _best_effort_detail_followup(
            lambda: helpers.refresh_item_image_if_missing(
                current_instance.item,
                media_metadata.get("image") if isinstance(media_metadata, dict) else None,
            ),
            operation_name="image refresh",
        )

    if media_type in (
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    ):
        runtime_media = current_instance
        if runtime_media is None and detail_item is not None:
            runtime_model = apps.get_model(app_label="app", model_name=media_type)
            runtime_media = runtime_model(item=detail_item)

        if runtime_media is not None and isinstance(media_metadata, dict):
            BasicMedia.objects.annotate_max_progress([runtime_media], media_type)
            total_runtime_display = runtime_media.formatted_total_runtime
            if total_runtime_display and total_runtime_display != "--":
                details = media_metadata.get("details")
                if not isinstance(details, dict):
                    details = {}
                    media_metadata["details"] = details

                if details.get("runtime"):
                    ordered_details = {}
                    for key, value in details.items():
                        ordered_details[key] = value
                        if key == "runtime":
                            ordered_details["total_runtime"] = total_runtime_display
                    details.clear()
                    details.update(ordered_details)
                else:
                    details["total_runtime"] = total_runtime_display

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
        render_secondary_only
        and not public_view
        and current_instance
        and media_type in (
            MediaTypes.GAME.value,
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        )
        and isinstance(media_metadata, dict)
    ):
        details = media_metadata.get("details", {})
        if not isinstance(details, dict):
            details = {}
        metadata_genres = stats._coerce_genre_list(
            media_metadata.get("genres")
            or details.get("genres")
            or media_metadata.get("genre")
            or details.get("genre"),
        )
        item = current_instance.item
        genres_updated = False
        if item:
            if metadata_genres and metadata_genres != item.genres:
                item.genres = metadata_genres
                genre_save_outcome = _best_effort_detail_db_work(
                    lambda: item.save(update_fields=["genres"]),
                    operation_name="detail genre sync",
                )
                genres_updated = not genre_save_outcome.deferred
                media_metadata["genres"] = metadata_genres
            elif item.genres:
                media_metadata["genres"] = item.genres
        if genres_updated and media_type in (
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        ):
            day_keys = _collect_reading_activity_day_keys(user_medias)
            if day_keys:
                statistics_cache.invalidate_statistics_days(
                    request.user.id,
                    day_values=day_keys,
                    reason="details_genres_update",
                )

    play_stats, activity_subtitle = _build_detail_activity_state(
        media_type,
        media_metadata,
        current_instance=current_instance,
        user_medias=user_medias,
        public_view=public_view,
    )

    # Enrich related items with user tracking data
    # For public views, use list owner's data if available
    if render_secondary_only and media_metadata.get("related"):
        for section_name, related_items in media_metadata["related"].items():
            if related_items:
                enriched_related_items = helpers.enrich_items_with_user_data(
                    request,
                    related_items,
                    section_name=section_name,
                    user=list_owner,
                    library_media_type=(
                        MediaTypes.ANIME.value
                        if media_type == MediaTypes.ANIME.value and section_name == "seasons"
                        else None
                    ),
                )
                if section_name == "seasons":
                    for enriched_item, raw_item in zip(
                        enriched_related_items,
                        related_items,
                        strict=False,
                    ):
                        if not isinstance(raw_item, dict):
                            continue
                        season_title = Item._normalize_title_value(
                            raw_item.get("season_title"),
                        )
                        show_title = Item._normalize_title_value(raw_item.get("title"))
                        if season_title and season_title != show_title:
                            enriched_item["card_title"] = season_title
                            continue

                        season_number = raw_item.get("season_number")
                        try:
                            season_number = (
                                int(season_number)
                                if season_number is not None
                                else None
                            )
                        except (TypeError, ValueError):
                            season_number = None

                        if season_number == 0:
                            enriched_item["card_title"] = "Specials"
                        elif season_number is not None:
                            enriched_item["card_title"] = f"Season {season_number}"

                # For anime shows, tag season items so media_url routes to anime season URLs
                if section_name == "seasons" and media_type == MediaTypes.ANIME.value:
                    for enriched_item in enriched_related_items:
                        item_dict = enriched_item.get("item")
                        if isinstance(item_dict, dict):
                            item_dict["route_media_type"] = MediaTypes.ANIME.value

                media_metadata["related"][section_name] = enriched_related_items

    # For music tracks, get linked artist and album for navigation
    music_artist = None
    music_album = None
    if media_type == MediaTypes.MUSIC.value and current_instance:
        music_artist = getattr(current_instance, "artist", None)
        music_album = getattr(current_instance, "album", None)

    notes_entry = None
    if render_secondary_only and not public_view and user_medias:
        if current_instance and current_instance.notes and current_instance.notes.strip():
            notes_entry = current_instance
        else:
            for entry in user_medias:
                if entry.notes and entry.notes.strip():
                    notes_entry = entry
                    break

    if (
        render_secondary_only
        and media_type == MediaTypes.ANIME.value
        and not media_metadata.get("episodes")
    ):
        flat_anime_episode_preview = _build_flat_anime_episode_preview(
            request,
            detail_item=detail_item,
            media_id=media_id,
            base_metadata=media_metadata,
            metadata_resolution_result=metadata_resolution_result,
            retry_max_retries=detail_db_max_retries,
            on_persistence_deferred=_mark_detail_persistence_deferred,
        )
        if flat_anime_episode_preview:
            media_metadata["episodes"] = flat_anime_episode_preview

    # Get collection entries for this item (if not public view and not podcast)
    collection_entry = None
    collection_entries = []
    collection_stats = None
    fetching_collection_data = False
    item_id_for_polling = None

    if (
        render_secondary_only
        and not public_view
        and media_type != MediaTypes.PODCAST.value
    ):
        from app.helpers import get_item_collection_entries, get_tv_show_collection_stats

        try:
            item = detail_item or Item.objects.get(**detail_item_lookup)
            collection_entries = list(get_item_collection_entries(request.user, item))
            collection_entry = collection_entries[0] if collection_entries else None

            # For TV shows, also get collection statistics (episodes/seasons)
            if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
                # Use episode count from metadata if available to match Details pane
                metadata_episode_count = media_metadata.get("details", {}).get("episodes") or media_metadata.get("episodes")
                collection_stats = get_tv_show_collection_stats(request.user, item, metadata_episode_count=metadata_episode_count)

            # If no collection entry exists and auto-fetch is supported, trigger background fetch
            if not collection_entry and config.supports_collection_auto_fetch(media_type):
                plex_account = getattr(request.user, "plex_account", None)
                if plex_account and plex_account.plex_token:
                    from integrations.tasks import fetch_collection_metadata_for_item
                    # Trigger background task to fetch collection data
                    followup_started = _best_effort_detail_followup(
                        lambda: fetch_collection_metadata_for_item.delay(
                            user_id=request.user.id,
                            item_id=item.id,
                            lookup_policy="cached_only",
                        ),
                        operation_name="collection metadata auto-fetch",
                        fallback=None,
                    )
                    if followup_started is not None:
                        # Use module-level logger directly to avoid UnboundLocalError
                        logging.getLogger(__name__).info(
                            "Triggered background collection fetch for %s - %s (item_id=%s)",
                            request.user.username,
                            item.title,
                            item.id,
                        )
                        # TODO(issue-166): Re-enable a user-facing collection-fetching banner only after
                        # the background task reliably self-resolves for empty collections; remove this
                        # reminder once that task/UX overhaul is complete.
                        fetching_collection_data = True
                        item_id_for_polling = item.id
        except Item.DoesNotExist:
            pass

    has_collection_data = bool(collection_entries) or collection_entry is not None

    if media_type in [MediaTypes.TV.value, MediaTypes.MOVIE.value, MediaTypes.ANIME.value]:
        watch_provider_payload = media_metadata.get("providers")
        if (
            render_secondary_only
            and detail_item
            and media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value)
            and (not watch_provider_payload or source == Sources.TVDB.value)
        ):
            tmdb_media_id = metadata_resolution.resolve_provider_media_id(
                detail_item,
                Sources.TMDB.value,
                route_media_type=media_type,
                persistence_mode="best_effort",
                retry_max_retries=detail_db_max_retries,
                on_deferred=_mark_detail_persistence_deferred,
            )
            if tmdb_media_id:
                tmdb_metadata = services.get_media_metadata(
                    media_type,
                    tmdb_media_id,
                    Sources.TMDB.value,
                )
                watch_provider_payload = tmdb_metadata.get("providers")

        watch_providers = (
            tmdb.filter_providers(
                watch_provider_payload,
                request.user.watch_provider_region,
            )
            if watch_provider_payload is not None
            else None
        )
    else:
        watch_providers = None

    display_provider = (
        metadata_resolution_result.display_provider
        if metadata_resolution_result
        else source
    )
    identity_provider = (
        metadata_resolution_result.identity_provider
        if metadata_resolution_result
        else source
    )
    grouped_preview = (
        metadata_resolution_result.grouped_preview
        if metadata_resolution_result
        else None
    )
    grouped_preview_target = (
        metadata_resolution_result.grouped_preview_target
        if metadata_resolution_result
        else None
    )
    metadata_provider_options = metadata_resolution.available_metadata_provider_options(
        media_type,
        identity_provider=identity_provider,
    )
    can_update_metadata_provider = bool(
        not public_view
        and detail_item is not None
        and metadata_provider_options
    )
    can_migrate_grouped_anime = False
    migrated_grouped_item = None
    migrated_grouped_title = None
    if (
        render_secondary_only
        and not public_view
        and media_type == MediaTypes.ANIME.value
        and detail_item is not None
    ):
        migrated_entry = (
            Anime.all_objects.filter(
                user=request.user,
                item=detail_item,
                migrated_to_item__isnull=False,
            )
            .select_related("migrated_to_item")
            .order_by("-migrated_at")
            .first()
        )
        if migrated_entry and migrated_entry.migrated_to_item:
            migrated_grouped_item = migrated_entry.migrated_to_item
            migrated_grouped_title = migrated_grouped_item.get_display_title(
                request.user,
            )

        can_migrate_grouped_anime = bool(
            detail_item.source == Sources.MAL.value
            and detail_item.media_type == MediaTypes.ANIME.value
            and display_provider in {Sources.TMDB.value, Sources.TVDB.value}
            and grouped_preview
            and Anime.objects.filter(user=request.user, item=detail_item).exists()
        )

    episode_load_more = None
    if (
        render_secondary_only
        and media_type != MediaTypes.PODCAST.value
        and media_metadata.get("episodes")
    ):
        media_metadata["episodes"] = _normalize_detail_episode_actions(
            media_metadata["episodes"],
        )
        media_metadata["episodes"], episode_load_more = _paginate_detail_episodes(
            request,
            media_metadata["episodes"],
        )

    context = {
        "user": request.user,
        "media": media_metadata,
        "media_type": media_type,
        "authors_linked": authors_linked,
        "author_detail_keys": author_detail_keys,
        "studios_linked": studios_linked,
        "studio_detail_keys": studio_detail_keys,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "music_artist": music_artist,
        "music_album": music_album,
        "public_view": public_view,
        "play_stats": play_stats,
        "activity_subtitle": activity_subtitle,
        "trakt_score": trakt_score,
        "game_lengths": game_lengths,
        "game_lengths_pending": game_lengths_refresh_pending
        and not (game_lengths and game_lengths.get("available")),
        "notes_entry": notes_entry,
        "collection_entry": collection_entry,
        "collection_entries": collection_entries,
        "collection_stats": collection_stats,
        "has_collection_data": has_collection_data,
        "fetching_collection_data": fetching_collection_data if not public_view else False,
        "item_id_for_polling": item_id_for_polling if not public_view else None,
        "watch_providers": watch_providers,
        "watch_provider_region": request.user.watch_provider_region,
        "detail_link_sections": _build_detail_link_sections(
            media_metadata,
            media_type,
            identity_provider,
            display_provider,
        ),
        "detail_tag_sections": _build_detail_tag_sections(
            media_metadata,
            detail_item,
            request.user,
        ),
        "detail_tag_preview_genres_json": json.dumps(
            _resolve_detail_tag_genres(media_metadata, detail_item)
        ),
        "display_provider": display_provider,
        "identity_provider": identity_provider,
        "metadata_provider_options": metadata_provider_options,
        "metadata_provider_mapping_status": (
            metadata_resolution_result.mapping_status
            if metadata_resolution_result
            else "identity"
        ),
        "grouped_preview": grouped_preview,
        "grouped_preview_target": grouped_preview_target,
        "can_update_metadata_provider": can_update_metadata_provider,
        "can_migrate_grouped_anime": can_migrate_grouped_anime,
        "migrated_grouped_item": migrated_grouped_item,
        "migrated_grouped_title": migrated_grouped_title,
        "episode_load_more": episode_load_more,
        "detail_persistence_deferred": detail_persistence_deferred,
        "detail_return_url": detail_return_url,
        "detail_secondary_fragment_url": detail_secondary_fragment_url,
        "defer_detail_secondary": defer_detail_secondary,
        "render_secondary_only": render_secondary_only,
    }
    logger.info(
        "detail_render_complete path=%s phase=%s media_type=%s source=%s duration_ms=%.2f",
        request.path,
        "secondary" if render_secondary_only else "shell",
        media_type,
        source,
        (time.perf_counter() - detail_view_started_at) * 1000,
    )
    return render(
        request,
        (
            "app/components/detail_secondary_content.html"
            if render_secondary_only
            else "app/media_details.html"
        ),
        context,
    )
