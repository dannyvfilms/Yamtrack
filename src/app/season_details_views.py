import json
import logging
import time

from django.conf import settings
from django.db import IntegrityError, transaction
from django.contrib.auth.decorators import login_not_required
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from app import config, helpers
from app.activity_builders import (
    _normalize_detail_episode_actions,
    _paginate_detail_episodes,
)
from app.detail_builders import (
    _build_detail_link_sections,
    _build_trakt_popularity_context,
)
from app.log_safety import exception_summary
from app.metadata_sync_views import (
    _build_local_tv_with_seasons_metadata,
    _build_missing_season_metadata,
    _get_local_show_item,
    _save_provider_metadata_status,
)
from app.models import (
    BasicMedia,
    CollectionEntry,
    Item,
    MediaTypes,
    ProviderMetadataStatus,
    Season,
    Sources,
    Status,
)
from app.providers import manual, services, tmdb
from app.services import trakt_popularity as trakt_popularity_service
from app.tag_views import (
    _build_detail_tag_sections,
    _detail_request_url,
    _resolve_detail_tag_genres,
)
from app.view_constants import DETAIL_SECONDARY_FRAGMENT, LOCAL_ONLY_MISSING_SEASON_BANNER
from lists.models import CustomList

logger = logging.getLogger(__name__)


@login_not_required
@require_GET
def season_details(
    request, source, media_id, title, season_number, parent_media_type=None,
):
    """Return the details page for a season."""
    detail_view_started_at = time.perf_counter()
    render_secondary_only = request.GET.get("fragment") == DETAIL_SECONDARY_FRAGMENT
    defer_detail_secondary = not render_secondary_only
    detail_return_url = _detail_request_url(request)
    detail_secondary_fragment_url = _detail_request_url(
        request,
        fragment=DETAIL_SECONDARY_FRAGMENT,
    )

    # Treat all anonymous views as public (no user-specific data/actions)
    is_anonymous = not request.user.is_authenticated
    public_view = is_anonymous
    public_list_view = request.GET.get("public_view") == "1" and is_anonymous

    # Scope all Season Item / tracking lookups to the correct library type so that
    # anime seasons and TV seasons are fully independent.
    season_library_media_type = (
        MediaTypes.ANIME.value
        if parent_media_type == MediaTypes.ANIME.value
        else None
    )

    def _scoped_season_item_qs():
        """Return a queryset for the Season Item scoped by library_media_type."""
        qs = Item.objects.filter(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
        )
        if season_library_media_type:
            qs = qs.filter(library_media_type=season_library_media_type)
        else:
            qs = qs.exclude(library_media_type=MediaTypes.ANIME.value)
        return qs

    # For public views, find a public list containing this item to get the owner
    list_owner = None
    if public_list_view:
        try:
            item = _scoped_season_item_qs().first()
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

    season_item = _scoped_season_item_qs().first()
    show_item = _get_local_show_item(media_id, source)
    season_key = f"season/{season_number}"
    season_item_is_local_only = (
        season_item is not None
        and season_item.provider_metadata_status
        == ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
    )

    # For public views, we don't need user media data
    if public_view:
        user_medias = []
        current_instance = None
    else:
        if season_item_is_local_only:
            season_qs = Season.objects.filter(
                item__media_id=media_id,
                item__media_type=MediaTypes.SEASON.value,
                item__source=source,
                item__season_number=season_number,
                user=request.user,
            )
            if season_library_media_type:
                season_qs = season_qs.filter(
                    item__library_media_type=season_library_media_type,
                )
            else:
                season_qs = season_qs.exclude(
                    item__library_media_type=MediaTypes.ANIME.value,
                )
            user_medias = list(
                season_qs.select_related("item", "related_tv", "related_tv__item")
                .prefetch_related("episodes", "episodes__item")
            )
        else:
            user_medias = BasicMedia.objects.filter_media_prefetch(
                request.user,
                media_id,
                MediaTypes.SEASON.value,
                source,
                season_number=season_number,
            )
            if season_library_media_type:
                user_medias = user_medias.filter(
                    item__library_media_type=season_library_media_type,
                )
            else:
                user_medias = user_medias.exclude(
                    item__library_media_type=MediaTypes.ANIME.value,
                )
            user_medias = list(user_medias)
        current_instance = user_medias[0] if user_medias else None

    episodes_in_db = current_instance.episodes.all() if current_instance else []
    if season_item_is_local_only:
        tv_with_seasons_metadata = _build_local_tv_with_seasons_metadata(
            media_id,
            source,
            show_item=show_item,
            season_item=season_item,
        )
        season_metadata = _build_missing_season_metadata(
            tv_with_seasons_metadata,
            media_id,
            source,
            season_number,
            episodes_in_db,
            season_item=season_item,
            show_item=show_item,
        )
        season_metadata_missing = True
    else:
        tv_with_seasons_metadata = services.get_media_metadata(
            "tv_with_seasons",
            media_id,
            source,
            [season_number],
        )
        season_metadata = tv_with_seasons_metadata.get(season_key)
        season_metadata_missing = season_metadata is None
        if season_metadata_missing:
            tv_with_seasons_metadata = _build_local_tv_with_seasons_metadata(
                media_id,
                source,
                tv_metadata=tv_with_seasons_metadata,
                show_item=show_item,
                season_item=season_item,
            )
            season_metadata = _build_missing_season_metadata(
                tv_with_seasons_metadata,
                media_id,
                source,
                season_number,
                episodes_in_db,
                season_item=season_item,
                show_item=show_item,
            )

    if current_instance is not None and isinstance(season_metadata, dict):
        try:
            helpers.refresh_item_image_if_missing(
                current_instance.item,
                season_metadata.get("image"),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Skipping season image refresh for %s due to error",
                request.path,
                exc_info=True,
            )

    default_season_title = (
        "Specials" if season_number == 0 else f"Season {season_number}"
    )
    anime_show_item = Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.TV.value,
        library_media_type=MediaTypes.ANIME.value,
    ).first()
    if isinstance(season_metadata, dict):
        season_metadata.setdefault(
            "season_header_title",
            season_metadata.get("season_title") or default_season_title,
        )
        season_metadata.setdefault("season_alternative_title", None)
        if anime_show_item:
            provider_season_title = (season_metadata.get("season_title") or "").strip()
            if provider_season_title and provider_season_title != default_season_title:
                season_metadata["season_header_title"] = default_season_title
                season_metadata["season_alternative_title"] = provider_season_title

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

    if render_secondary_only and season_item is None:
        season_defaults = {
            **Item.title_fields_from_metadata(
                season_metadata if isinstance(season_metadata, dict) else {},
                fallback_title=((season_metadata or {}).get("title") or ""),
            ),
            "image": (
                (season_metadata or {}).get("image")
                if isinstance(season_metadata, dict)
                else settings.IMG_NONE
            )
            or settings.IMG_NONE,
            "provider_metadata_status": (
                ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
                if season_metadata_missing and season_number > 0
                else ""
            ),
        }
        season_item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.SEASON.value,
            library_media_type=parent_media_type if parent_media_type == MediaTypes.ANIME.value else "",
            season_number=season_number,
            defaults=season_defaults,
        )
    elif render_secondary_only and season_metadata_missing and season_number > 0:
        season_item = _save_provider_metadata_status(
            season_item,
            ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value,
        )

    # Save episode runtimes from raw metadata before processing for display
    # This ensures runtime data is persisted when viewing the season page
    if (
        render_secondary_only
        and not season_metadata_missing
        and source != Sources.MANUAL.value
        and season_metadata.get("episodes")
    ):
        from datetime import datetime

        raw_episodes = season_metadata["episodes"]
        current_datetime = timezone.now()
        episodes_to_update = []

        for episode in raw_episodes:
            episode_number = episode.get("episode_number")
            if episode_number is None:
                continue

            # Get or create episode item — retry on race condition
            lookup = dict(
                media_id=media_id,
                source=source,
                media_type=MediaTypes.EPISODE.value,
                library_media_type=parent_media_type if parent_media_type == MediaTypes.ANIME.value else "",
                season_number=season_number,
                episode_number=episode_number,
            )
            try:
                with transaction.atomic():
                    episode_item, _ = Item.objects.get_or_create(
                        **lookup,
                        defaults={
                            "title": season_metadata.get("title", ""),
                            "image": settings.IMG_NONE,
                        },
                    )
            except IntegrityError:
                episode_item = Item.objects.get(**lookup)

            # Extract runtime from raw episode data (TMDB returns integer minutes)
            runtime_minutes = None
            if episode.get("runtime") is not None:
                runtime_minutes = (
                    int(episode["runtime"])
                    if episode["runtime"] > 0
                    else None
                )
            elif episode.get("air_date"):
                # Check if episode has aired
                try:
                    if isinstance(episode["air_date"], str):
                        date_obj = datetime.strptime(episode["air_date"], "%Y-%m-%d")
                        air_date_dt = timezone.make_aware(
                            date_obj,
                            timezone.get_current_timezone(),
                        )
                    else:
                        air_date_dt = episode["air_date"]

                    if (
                        air_date_dt
                        and air_date_dt.year > 1900
                        and air_date_dt <= current_datetime
                    ):
                        # Episode has aired but no runtime - mark as unknown (use 999998)
                        runtime_minutes = 999998
                except (ValueError, TypeError):
                    pass

            # Only update if runtime is actually new (not just saving the same value)
            if episode_item.runtime_minutes != runtime_minutes:
                episode_item.runtime_minutes = runtime_minutes
                episodes_to_update.append(episode_item)

        if episodes_to_update:
            Item.objects.bulk_update(
                episodes_to_update,
                ["runtime_minutes"],
                batch_size=100,
            )
            # Invalidate time_left cache for all users (runtime affects time calculations)
            from app.cache_utils import clear_time_left_cache_for_user
            # Get all users who track this show
            tracking_users = BasicMedia.objects.filter(
                item__media_id=media_id,
                item__source=source,
                item__media_type__in=[MediaTypes.TV.value, MediaTypes.SEASON.value],
            ).values_list("user_id", flat=True).distinct()
            for user_id in tracking_users:
                clear_time_left_cache_for_user(user_id)

    if render_secondary_only and not season_metadata_missing:
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

    if (
        season_item
        and isinstance(season_metadata, dict)
        and season_item.image
        and season_item.image != settings.IMG_NONE
    ):
        season_metadata["image"] = season_item.image

    season_provider_metadata_status = (
        season_item.provider_metadata_status
        if season_item is not None
        else (
            ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
            if season_metadata_missing and season_number > 0
            else ""
        )
    )
    if isinstance(season_metadata, dict):
        season_metadata["provider_metadata_status"] = season_provider_metadata_status

    # Add collection_entry data to each episode (if not public view)
    if render_secondary_only and not public_view and season_metadata.get("episodes"):
        from app.models import Item as ItemModel

        # Get all episode items for this season
        episode_numbers = [
            ep.get("episode_number")
            for ep in season_metadata["episodes"]
        ]
        episode_items = ItemModel.objects.filter(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.EPISODE.value,
            season_number=season_number,
            episode_number__in=episode_numbers,
        )

        # Build episode_number → Item map for item references and collection lookups
        item_by_episode_number = {
            item.episode_number: item
            for item in episode_items
            if item.episode_number is not None
        }
        episode_item_ids = [
            item_by_episode_number[ep_num].id
            for ep_num in item_by_episode_number
        ]
        collection_entries = {}
        if episode_item_ids:
            collection_entries_qs = (
                CollectionEntry.objects.filter(
                    user=request.user,
                    item_id__in=episode_item_ids,
                )
                .select_related("item")
                .order_by("-collected_at", "-id")
            )
            # Map by episode_number for quick lookup
            for entry in collection_entries_qs:
                ep_num = entry.item.episode_number
                if ep_num is not None and ep_num not in collection_entries:
                    collection_entries[ep_num] = entry

        # Add collection_entry and item reference to each episode
        for episode in season_metadata["episodes"]:
            episode_number = episode.get("episode_number")
            episode["collection_entry"] = collection_entries.get(episode_number)
            episode["item"] = item_by_episode_number.get(episode_number)

    # Enrich related items with user tracking data
    # For public views, use list owner's data if available
    if render_secondary_only and season_metadata.get("related"):
        for section_name, related_items in season_metadata["related"].items():
            if related_items:
                season_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                        section_name=section_name,
                        user=list_owner,
                    )
                )

    if current_instance and hasattr(current_instance, "derived_status_from_episode_progress"):
        season_max_progress = (
            season_metadata.get("max_progress")
            if isinstance(season_metadata, dict)
            else None
        )
        if (
            current_instance.derived_status_from_episode_progress(
                max_progress=season_max_progress,
            )
            == Status.COMPLETED.value
            and current_instance.status != Status.COMPLETED.value
        ):
            current_instance.promote_to_completed_if_fully_watched(
                max_progress=season_max_progress,
            )
        current_instance.max_progress = season_max_progress
        current_instance.status = current_instance.derived_status_from_episode_progress(
            max_progress=season_max_progress,
        )
        for user_media in user_medias:
            if not hasattr(user_media, "derived_status_from_episode_progress"):
                continue
            user_media.max_progress = season_max_progress
            user_media.status = user_media.derived_status_from_episode_progress(
                max_progress=season_max_progress,
            )

    # Get collection entry, stats, and metadata for this season (if not public view)
    collection_entry = None
    collection_entries = []
    season_collection_stats = None
    fetching_collection_data = False
    item_id_for_polling = None
    if render_secondary_only and not public_view:
        from app.helpers import (
            get_item_collection_entries,
            get_season_collection_metadata,
            get_season_collection_stats,
        )
        from app.models import Item as ItemModel  # Use alias to avoid any potential shadowing

        # season_item is already scoped by library_media_type at the top of this view
        try:
            if season_item is None:
                raise ItemModel.DoesNotExist

            # Check if the show has collection data, and trigger background fetch if not
            # We check the show item (not season) because episode collection data is tied to the show
            try:
                # Use parent_media_type to pick the correct show Item (anime vs TV)
                # so we don't hit MultipleObjectsReturned when both exist.
                show_media_type = (
                    MediaTypes.ANIME.value
                    if parent_media_type == MediaTypes.ANIME.value
                    else MediaTypes.TV.value
                )
                show_item = ItemModel.objects.get(
                    media_id=media_id,
                    source=source,
                    media_type=show_media_type,
                )
                show_collection_entry = get_item_collection_entries(request.user, show_item).first()

                logger.info("Season page: Checking show %s (item_id=%s) - collection entry exists: %s",
                           show_item.title, show_item.id, show_collection_entry is not None)

                # If no collection entry exists for the show and auto-fetch is supported, trigger background fetch
                if not show_collection_entry and config.supports_collection_auto_fetch(show_item.media_type):
                    plex_account = getattr(request.user, "plex_account", None)
                    if plex_account and plex_account.plex_token:
                        try:
                            from integrations.tasks import fetch_collection_metadata_for_item
                            # Trigger background task to fetch collection data for the show
                            result = fetch_collection_metadata_for_item.delay(
                                user_id=request.user.id,
                                item_id=show_item.id,
                                lookup_policy="cached_only",
                            )
                            logger.info("Triggered background collection fetch for show %s - %s (item_id=%s) from season page (task_id=%s)",
                                       request.user.username, show_item.title, show_item.id, result.id if result else "None")
                            # TODO(issue-166): Re-enable a user-facing collection-fetching banner only
                            # after the background task reliably self-resolves for empty collections;
                            # remove this reminder once that task/UX overhaul is complete.
                            fetching_collection_data = True
                            item_id_for_polling = show_item.id
                        except Exception as task_exc:
                            logger.error("Failed to trigger background collection fetch for show %s - %s: %s",
                                        request.user.username, show_item.title, task_exc, exc_info=True)
                    else:
                        logger.info("Season page: User %s does not have Plex connected, skipping background fetch", request.user.username)
            except ItemModel.DoesNotExist:
                # Show item doesn't exist yet, skip background fetch
                logger.debug("Season page: Show item not found for media_id=%s, source=%s", media_id, source)
                pass
            except Exception as exc:
                logger.error("Error checking show collection entry in season_details: %s", exception_summary(exc), exc_info=True)

            # Get collection entry for the season item itself (if it exists)
            collection_entries = list(get_item_collection_entries(request.user, season_item))
            season_collection_entry = collection_entries[0] if collection_entries else None

            # Get aggregated collection metadata from episodes (or season/show-level entry)
            season_collection_metadata = get_season_collection_metadata(request.user, season_item)

            # Use season-level entry if it exists, otherwise use aggregated metadata
            if season_collection_entry:
                collection_entry = season_collection_entry
            elif season_collection_metadata:
                # Check if aggregated metadata has any actual values
                has_metadata = any([
                    season_collection_metadata.get("resolution"),
                    season_collection_metadata.get("hdr"),
                    season_collection_metadata.get("audio_codec"),
                    season_collection_metadata.get("audio_channels"),
                    season_collection_metadata.get("bitrate"),
                    season_collection_metadata.get("media_type"),
                    season_collection_metadata.get("is_3d"),
                ])

                if has_metadata:
                    # Create a mock collection entry object from aggregated metadata
                    # This allows the template to access fields like collection_entry.resolution
                    from types import SimpleNamespace
                    collection_entry = SimpleNamespace(
                        resolution=season_collection_metadata.get("resolution") or "",
                        hdr=season_collection_metadata.get("hdr") or "",
                        audio_codec=season_collection_metadata.get("audio_codec") or "",
                        audio_channels=season_collection_metadata.get("audio_channels") or "",
                        bitrate=season_collection_metadata.get("bitrate"),
                        media_type=season_collection_metadata.get("media_type") or "",
                        is_3d=season_collection_metadata.get("is_3d", False),
                        collected_at=season_collection_metadata.get("collected_at"),
                    )

            # Get collection stats for this season (episodes)
            season_collection_stats = get_season_collection_stats(request.user, season_item)
        except ItemModel.DoesNotExist:
            pass

    if (
        render_secondary_only
        and season_item
        and current_instance
        and season_number > 0
        and season_item.provider_metadata_status
        != ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
        and trakt_popularity_service.trakt_provider.is_configured()
        and trakt_popularity_service.needs_refresh(season_item)
    ):
        try:
            trakt_popularity_service.refresh_trakt_popularity(
                season_item,
                route_media_type=MediaTypes.SEASON.value,
                force=False,
            )
            season_item.refresh_from_db()
        except Exception as exc:
            logger.warning(
                "trakt_popularity_season_refresh_failed item_id=%s media_id=%s season=%s error=%s",
                season_item.id,
                season_item.media_id,
                season_number,
                exception_summary(exc),
            )

    has_collection_data = bool(collection_entries) or collection_entry is not None
    trakt_score = _build_trakt_popularity_context(
        season_item,
        MediaTypes.SEASON.value,
    )
    episode_load_more = None
    if render_secondary_only and season_metadata.get("episodes"):
        season_metadata["episodes"] = _normalize_detail_episode_actions(
            season_metadata["episodes"],
        )
        season_metadata["episodes"], episode_load_more = _paginate_detail_episodes(
            request,
            season_metadata["episodes"],
        )

    # Resolve parent media type: anime URL kwarg takes priority, else detect via DB
    if parent_media_type is None and anime_show_item and getattr(request.user, "anime_enabled", False):
        parent_media_type = MediaTypes.ANIME.value
    if parent_media_type is None:
        parent_media_type = MediaTypes.TV.value

    context = {
        "user": request.user,
        "media": season_metadata,
        "tv": tv_with_seasons_metadata,
        "media_type": MediaTypes.SEASON.value,
        "parent_media_type": parent_media_type,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "public_view": public_view,
        "collection_entry": collection_entry,
        "collection_entries": collection_entries,
        "collection_stats": season_collection_stats,
        "has_collection_data": has_collection_data,
        "fetching_collection_data": (
            fetching_collection_data if not public_view else False
        ),
        "item_id_for_polling": item_id_for_polling if not public_view else None,
        "trakt_score": trakt_score,
        "watch_providers": tmdb.filter_providers(
            season_metadata.get("providers"), request.user.watch_provider_region
        ),
        "watch_provider_region": request.user.watch_provider_region,
        "detail_link_sections": _build_detail_link_sections(
            season_metadata,
            MediaTypes.SEASON.value,
            source,
            source,
        ),
        "detail_tag_sections": _build_detail_tag_sections(
            season_metadata,
            season_item,
            request.user,
        ),
        "detail_tag_preview_genres_json": json.dumps(
            _resolve_detail_tag_genres(season_metadata, season_item)
        ),
        "display_provider": source,
        "identity_provider": source,
        "episode_load_more": episode_load_more,
        "season_provider_metadata_status": season_provider_metadata_status,
        "season_provider_metadata_banner": LOCAL_ONLY_MISSING_SEASON_BANNER,
        "season_provider_metadata_is_local_only": (
            season_provider_metadata_status
            == ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
        ),
        "detail_return_url": detail_return_url,
        "detail_secondary_fragment_url": detail_secondary_fragment_url,
        "defer_detail_secondary": defer_detail_secondary,
        "render_secondary_only": render_secondary_only,
    }
    logger.info(
        "detail_render_complete path=%s phase=%s media_type=%s source=%s duration_ms=%.2f",
        request.path,
        "secondary" if render_secondary_only else "shell",
        MediaTypes.SEASON.value,
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
