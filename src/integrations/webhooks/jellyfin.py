import logging

from django.db.models import Q
from django.utils import timezone

import app
from app import live_playback
from app.log_safety import mapping_keys, presence_map
from app.models import (
    TV,
    Item,
    ItemProviderLink,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)

from .base import BaseWebhookProcessor

logger = logging.getLogger(__name__)

JELLYFIN_EVENT_MAP = {
    "Play": "media.play",
    "Pause": "media.pause",
    "Stop": "media.stop",
}


def _ticks_to_seconds(ticks) -> int | None:
    """Convert Jellyfin 100-nanosecond ticks to whole seconds."""
    if ticks is None:
        return None
    try:
        return max(0, int(ticks) // 10_000_000)
    except (TypeError, ValueError):
        return None


class JellyfinWebhookProcessor(BaseWebhookProcessor):
    """Processor for Jellyfin webhook events.

    Supports two optional features controlled by user settings:

    Feature #1 - Provider Priority for Tracking Source
        When ``jellyfin_provider_priority_enabled`` is True, webhooks will
        attempt to track episodes under the user's preferred metadata provider
        (MAL / TVDB) rather than always using TMDB.

    Feature #2 - Match Existing Tracked Items
        When ``jellyfin_match_existing_enabled`` is True, incoming webhook
        IDs are matched against *all* known provider IDs for items already
        tracked by the user.  If a match is found the progress update is
        applied to the existing item regardless of which provider supplied
        the ID in the webhook payload.

    Processing priority when both are enabled::

        Feature #2 (match existing) > Feature #1 (preferred source) > Fallback (TMDB)
    """

    def _resolve_tvdb_episode_to_show(self, media_type, ids):
        """Resolve TVDB episode ID to show-level TMDB/TVDB IDs if needed.
        
        Jellyfin may send a TVDB episode ID instead of a show ID for TV series.
        This function converts episode-level IDs to show-level IDs by:
        1. Using the base class _find_tv_media_id() to resolve to TMDB show ID
        2. Then calling TMDB's external_ids() to get the show-level TVDB ID
        """
        if media_type != MediaTypes.TV.value:
            return ids
        
        tvdb_episode_id = ids.get("tvdb_id")
        imdb_episode_id = ids.get("imdb_id")
        
        if not tvdb_episode_id and not imdb_episode_id:
            return ids
        
        # Build a copy to avoid modifying the original dict
        resolved_ids = dict(ids)
        
        # Try to resolve episode-level TVDB ID to TMDB show ID
        if tvdb_episode_id:
            logger.info(
                "_resolve_tvdb_episode_to_show: Resolving TVDB episode ID %s to show ID",
                tvdb_episode_id,
            )
            tmdb_show_id, _, _ = self._find_tv_media_id(resolved_ids, MediaTypes.EPISODE.value)
            
            if tmdb_show_id:
                # Step 1: Update resolved IDs to use TMDB show ID instead
                resolved_ids["tmdb_id"] = str(tmdb_show_id)
                
                # Step 2: Get show-level TVDB ID from TMDB external_ids endpoint
                try:
                    ext_data = app.providers.tmdb.external_ids(
                        MediaTypes.TV.value,
                        tmdb_show_id,
                    )
                    if ext_data and isinstance(ext_data, dict):
                        show_tvdb_id = ext_data.get("tvdb_id")
                        if show_tvdb_id:
                            resolved_ids["tvdb_id"] = str(show_tvdb_id)
                            logger.info(
                                "_resolve_tvdb_episode_to_show: Resolved TVDB episode %s -> show %s -> TVDB show %s",
                                tvdb_episode_id,
                                tmdb_show_id,
                                show_tvdb_id,
                            )
                except Exception as exc:
                    logger.warning(
                        "_resolve_tvdb_episode_to_show: Failed to resolve TVDB episode ID %s: %s",
                        tvdb_episode_id,
                        exc,
                    )
        # If we only have an IMDB episode ID, the existing _find_existing_item
        # logic will handle it when looking up the item
        return resolved_ids

    def process_payload(self, payload, user):
        """Process the incoming Jellyfin webhook payload."""
        logger.info(
            "Processing Jellyfin webhook payload: %s",
            payload,
        )

        logger.debug(
            "Processing Jellyfin webhook payload keys=%s item_keys=%s",
            mapping_keys(payload),
            mapping_keys(payload.get("Item")),
        )

        event_type = payload.get("Event")
        logger.info("Jellyfin webhook event type: %s", event_type)
        if not self._is_supported_event(event_type):
            logger.debug("Ignoring Jellyfin webhook event type: %s", event_type)
            return

        ids = self._extract_external_ids(payload)
        media_type = self._get_media_type(payload)

        logger.info(
            "Extracted Jellyfin IDs from payload: %s",
            ids,
        )

        # Resolve TVDB episode IDs to show-level IDs before logging
        ids = self._resolve_tvdb_episode_to_show(media_type, ids)

        # Update payload ProviderIds with resolved IDs
        payload["Item"]["ProviderIds"] = {
            "Tmdb": ids.get("tmdb_id", ""),
            "Imdb": ids.get("imdb_id", ""),
            "Tvdb": ids.get("tvdb_id", ""),
        }

        # Update live playback state (before media tracking)
        playback_media_type = self._get_live_playback_media_type(payload)
        self._update_live_playback_state(
            payload, user, ids, playback_media_type,
        )

        # Pause events only update the card — no media tracking
        if event_type == "Pause":
            return

        if not any(ids.values()):
            logger.warning(
                "Ignoring Jellyfin webhook call because no ID was found.",
            )
            return

        self._process_media(payload, user, ids)

    # -- Event helpers --------------------------------------------------

    def _is_supported_event(self, event_type):
        logger.info("_is_supported_event: event_type=%s, supported=%s", event_type, event_type in ("Play", "Pause", "Stop"))
        return event_type in ("Play", "Pause", "Stop")

    def _is_played(self, payload):
        played = payload["Item"]["UserData"]["Played"]
        logger.info("_is_played: played=%s", played)
        return played

    def _get_media_type(self, payload):
        media_type = self.MEDIA_TYPE_MAPPING.get(payload["Item"].get("Type"))
        logger.info("_get_media_type: payload_item_type=%s, mapped=%s", payload["Item"].get("Type"), media_type)
        return media_type

    def _get_media_title(self, payload):
        """Get media title from payload."""
        title = None

        if self._get_media_type(payload) == MediaTypes.TV.value:
            series_name = payload["Item"].get("SeriesName")
            season_number = payload["Item"].get("ParentIndexNumber")
            episode_number = payload["Item"].get("IndexNumber")
            title = f"{series_name} S{season_number:02d}E{episode_number:02d}"
            logger.info("_get_media_title: TV title=%s", title)

        elif self._get_media_type(payload) == MediaTypes.MOVIE.value:
            movie_name = payload["Item"].get("Name")
            year = payload["Item"].get("ProductionYear")
            title = f"{movie_name} ({year})" if movie_name and year else movie_name
            logger.info("_get_media_title: Movie title=%s", title)

        return title

    def _extract_external_ids(self, payload):
        provider_ids = payload["Item"].get("ProviderIds", {})
        result = {
            "tmdb_id": provider_ids.get("Tmdb"),
            "imdb_id": provider_ids.get("Imdb"),
            "tvdb_id": provider_ids.get("Tvdb"),
        }
        logger.info("_extract_external_ids: result=%s", result)
        return result

    def _extract_season_episode_from_payload(self, payload):
        """Extract season and episode numbers from Jellyfin payload."""
        item = payload.get("Item", {})
        season_number = item.get("ParentIndexNumber")
        episode_number = item.get("IndexNumber")
        logger.info("_extract_season_episode_from_payload: raw season=%s, episode=%s", season_number, episode_number)

        try:
            season_number = int(season_number) if season_number is not None else None
            episode_number = int(episode_number) if episode_number is not None else None
        except (ValueError, TypeError):
            logger.info("_extract_season_episode_from_payload: conversion failed, returning None, None")
            return None, None

        logger.info("_extract_season_episode_from_payload: parsed season=%s, episode=%s", season_number, episode_number)
        return season_number, episode_number

    def _extract_series_title(self, payload):
        """Extract TV series title from Jellyfin payload."""
        if self._get_media_type(payload) == MediaTypes.TV.value:
            title = payload.get("Item", {}).get("SeriesName")
            logger.info("_extract_series_title: title=%s", title)
            return title
        logger.info("_extract_series_title: not TV type, returning None")
        return None

    # -- Feature #2: Match existing tracked items -----------------------

    def _find_existing_item(self, user, media_type, ids):
        """Find existing tracked item by *any* known provider ID.

        Searches for items matching tmdb_id, tvdb_id, imdb_id, mal_id, etc.
        Only returns items where the user has a tracking instance with a
        status (Completed, In progress, Planning, Paused, or Dropped).

        All resolved show IDs (TMDB, TVDB, MAL) are tried against every
        tracking source so that a webhook carrying e.g. a TVDB ID can still
        match an item originally tracked under MAL or TMDB.

        Feature #2 Priority: check existing items **first** before applying
        provider priority.

        Returns ``(item, created)`` where ``created`` is ``False`` when a
        pre-existing item was found.
        """
        logger.info(
            "_find_existing_item: user=%s, media_type=%s, ids=%s, match_existing_enabled=%s",
            user,
            media_type,
            ids,
            getattr(user, "jellyfin_match_existing_enabled", False),
        )
        if not getattr(user, "jellyfin_match_existing_enabled", False):
            logger.info("_find_existing_item: match_existing disabled, returning None, True")
            return None, True

        # Helper to check if user has a tracking instance for an item
        def has_tracking_instance(item_):
            if media_type == MediaTypes.MOVIE.value:
                return Movie.objects.filter(item=item_, user=user).exists()
            if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
                return TV.objects.filter(item=item_, user=user).exists()
            return False

        resolved_ids = dict(ids)

        # --- Direct lookups by known fields on Item ----------------------
        # Try ALL show IDs (TMDB, TVDB, MAL) against ALL tracking sources.
        # This ensures we find items even if the webhook sends a different
        # provider's ID than what the item was originally tracked under.
        all_source_id_pairs = [
            (Sources.TMDB.value, "tmdb_id"),
            (Sources.TVDB.value, "tvdb_id"),
        ]
        if media_type == MediaTypes.ANIME.value:
            all_source_id_pairs.append((Sources.MAL.value, "mal_id"))

        for source, id_key in all_source_id_pairs:
            raw_id = resolved_ids.get(id_key)
            logger.info("_find_existing_item: checking direct lookup source=%s, id_key=%s, raw_id=%s", source, id_key, raw_id)
            if not raw_id:
                continue
            try:
                item = Item.objects.get(
                    media_type=media_type,
                    source=source,
                    media_id=str(raw_id),
                )
                if has_tracking_instance(item):
                    logger.info("_find_existing_item: found existing item via direct lookup: %s (source=%s)", item, source)
                    return item, False
            except Item.DoesNotExist:
                pass

        # --- Cross-provider lookup via ItemProviderLink ------------------
        if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            link_qs = ItemProviderLink.objects.filter(
                provider__in=(Sources.TMDB.value, Sources.TVDB.value, Sources.MAL.value),
                provider_media_type=media_type,
            )

            # Build query using ALL resolved IDs (TMDB, TVDB, MAL)
            q = Q()
            for source, id_key in all_source_id_pairs:
                item_id = resolved_ids.get(id_key)
                if item_id:
                    q |= Q(provider=source, provider_media_id=str(item_id))

            if q.children:
                logger.info("_find_existing_item: narrowing provider link query by resolved IDs: %s", {k: v for k, v in resolved_ids.items() if v})
                link_qs = link_qs.filter(q)

            for link in link_qs[:50]:
                try:
                    item = link.item
                    logger.info(
                        "_find_existing_item: checking provider link item=%s, media_type=%s, has_tracking=%s",
                        item,
                        item.media_type,
                        has_tracking_instance(item),
                    )
                    if (
                        item.media_type == media_type
                        and has_tracking_instance(item)
                    ):
                        logger.info("_find_existing_item: found existing item via provider link: %s", item)
                        return item, False
                except Exception:
                    continue

        logger.info("_find_existing_item: no existing item found, returning None, True")
        return None, True

    def _update_existing_item(self, item, payload, user):
        """Update progress on an existing item without changing
        its identity provider.
        """
        logger.info("_update_existing_item: item=%s, media_type=%s, user=%s", item, item.media_type, user)
        played = self._is_played(payload)
        now = self._get_played_at(payload) or timezone.now().replace(
            second=0, microsecond=0,
        )
        logger.info("_update_existing_item: played=%s, now=%s", played, now)

        if item.media_type == MediaTypes.MOVIE.value:
            logger.info("_update_existing_item: routing to _update_movie_instance")
            self._update_movie_instance(item, user, played, now)
        elif item.media_type == MediaTypes.TV.value:
            logger.info("_update_existing_item: routing to _update_tv_season_episode")
            self._update_tv_season_episode(item, payload, user, played, now)

    def _update_movie_instance(self, item, user, played, now):
        """Create or update a Movie tracking instance."""
        logger.info("_update_movie_instance: item=%s, user=%s, played=%s, now=%s", item, user, played, now)
        instance, created = Movie.objects.get_or_create(
            item=item,
            user=user,
            defaults={
                "status": (
                    Status.COMPLETED.value
                    if played
                    else Status.IN_PROGRESS.value
                ),
                "progress": 1 if played else 0,
                "start_date": None if played else now,
                "end_date": now if played else None,
            },
        )
        if not created and instance.status != Status.COMPLETED.value:
            instance.progress = 1 if played else instance.progress
            if played:
                instance.end_date = now
                instance.status = Status.COMPLETED.value
            elif instance.status != Status.IN_PROGRESS.value:
                instance.start_date = now
                instance.status = Status.IN_PROGRESS.value
            if instance.tracker.changed():
                instance.save()

    def _update_tv_season_episode(  # noqa: C901
        self, item, payload, user, played, now,
    ):
        """Create or update Season/Episode tracking instances.

        Creates or updates Season and Episode tracking instances
        for an existing TV show item.
        """
        logger.info("_update_tv_season_episode: item=%s, user=%s, played=%s, now=%s", item, user, played, now)
        # We need season/episode numbers to create Episode records.
        season_number, episode_number = (
            self._extract_season_episode_from_payload(payload)
        )
        if season_number is None or episode_number is None:
            logger.warning(
                "Cannot update TV season/episode without season/episode numbers: %s",
                item,
            )
            return

        # Ensure the show-level TV instance exists
        tv_instance, _ = TV.objects.get_or_create(
            item=item,
            user=user,
            defaults={"status": Status.IN_PROGRESS.value},
        )
        if tv_instance.status != Status.IN_PROGRESS.value and not played:
            tv_instance.status = Status.IN_PROGRESS.value
            tv_instance.save(update_fields=["status"])

        # Get or create the Season instance
        season_item, _ = Item.objects.get_or_create(
            media_id=item.media_id,
            source=item.source,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                "title": item.title,
                "image": item.image,
            },
        )

        season_instance, season_created = Season.objects.get_or_create(
            item=season_item,
            user=user,
            related_tv=tv_instance,
            defaults={"status": Status.IN_PROGRESS.value},
        )
        if season_created:
            logger.info(
                "Created new season instance: %s S%02d",
                item.title,
                season_number,
            )
        elif season_instance.status != Status.IN_PROGRESS.value and not played:
            season_instance.status = Status.IN_PROGRESS.value
            season_instance.save(update_fields=["status"])

        # Create Episode record if marked as played
        if played:
            # Use a simpler episode lookup
            try:
                episode_item = Season.objects.filter(
                    item=season_item, user=user, related_tv=tv_instance
                ).first()
                if episode_item:
                    latest = (
                        app.models.Episode.objects.filter(
                            related_season=season_instance,
                        )
                        .order_by("-end_date")
                        .first()
                    )
                    should_create = True
                    if latest and latest.end_date:
                        time_diff = abs(
                            (now - latest.end_date).total_seconds(),
                        )
                        if time_diff < 5:
                            should_create = False

                    if should_create:
                        app.models.Episode.objects.create(
                            item=season_instance.item,
                            related_season=season_instance,
                            end_date=now,
                        )
            except Exception:
                # Best-effort: don't fail the webhook if episode creation fails
                logger.debug(
                    "Episode creation best-effort failed (non-critical)",
                    exc_info=True,
                )

    # -- Feature #1: Provider priority for tracking source --------------

    def _get_jellyfin_preferred_source(self, user, media_type):
        """Return the user's preferred tracking source for the given media type.

        Returns the preferred source string (e.g. ``'mal'``, ``'tvdb'``) or
        ``None`` when the setting is disabled / unknown — letting the caller
        fall through to normal TMDB-first processing.
        """
        provider_priority_enabled = getattr(user, "jellyfin_provider_priority_enabled", False)
        logger.info(
            "_get_jellyfin_preferred_source: user=%s, media_type=%s, provider_priority_enabled=%s",
            user,
            media_type,
            provider_priority_enabled,
        )
        if not provider_priority_enabled:
            logger.info("_get_jellyfin_preferred_source: provider priority disabled, returning None")
            return None

        if media_type == MediaTypes.TV.value:
            preferred = getattr(user, "tv_metadata_source_default", None)
            logger.info("_get_jellyfin_preferred_source: TV preferred source=%s", preferred)
            return preferred

        if media_type == MediaTypes.ANIME.value:
            preferred = getattr(user, "anime_metadata_source_default", None)
            logger.info("_get_jellyfin_preferred_source: Anime preferred source=%s", preferred)
            return preferred

        # Movies always use TMDB — no override needed
        logger.info("_get_jellyfin_preferred_source: Movies use TMDB, returning None")
        return None

    def _resolve_media_id_to_preferred_source(
        self, user, media_type, ids, season_number, episode_number,
    ):
        """Resolve media ID to user's preferred provider.

        Returns ``(media_id, source, season, episode)``.

        When ``jellyfin_provider_priority_enabled`` is True this method checks
        whether the user's preferred provider has a matching ID in the webhook
        payload.  If found, the item is tracked under that provider.

        Note: Episode-level TVDB/IMDB IDs are resolved to show-level IDs
        in ``_resolve_tvdb_episode_to_show()`` before this method is called,
        so this function only handles show-level IDs.

        Returns ``(None, None, None, None)`` when the setting is disabled or when
        no matching ID was found — signalling that fallback (normal TMDB-first)
        processing should apply.
        """
        logger.info(
            "_resolve_media_id_to_preferred_source: user=%s, media_type=%s, ids=%s, season=%s, episode=%s",
            user,
            media_type,
            ids,
            season_number,
            episode_number,
        )
        preferred = self._get_jellyfin_preferred_source(user, media_type)
        if not preferred:
            logger.info("_resolve_media_id_to_preferred_source: no preferred source, returning None")
            return None, None, None, None

        media_id = ids.get(f"{preferred}_id")
        if not media_id:
            logger.info("_resolve_media_id_to_preferred_source: no ID for preferred source (%s), returning None", preferred)
            return None, None, None, None
        logger.info("_resolve_media_id_to_preferred_source: preferred=%s, media_id=%s", preferred, media_id)

        try:
            result = str(media_id), preferred, season_number, episode_number
            logger.info("_resolve_media_id_to_preferred_source: resolved to %s", result)
            return result
        except Exception as exc:
            logger.debug(
                "Failed resolving preferred provider %s: %s",
                preferred, exc,
            )
            return None, None, None, None

    # -- TV/movie routing -----------------------------------------------

    def _process_tv(
        self, payload, user, ids,
        season_number=None, episode_number=None,
    ):
        """Process TV episode webhook with matching/priority.

        Priority order:

        1. **Feature #2**: Check for an existing tracked item by *any* provider ID.
        2. **Feature #1**: Resolve media ID using user's preferred provider if enabled.
        3. **Fallback**: Normal TMDB-first processing via the base class.

        If ``season_number`` or ``episode_number`` are not provided as parameters,
        they are extracted from the payload before any downstream calls.
        """
        # Extract season/episode from payload if not provided as parameters
        if season_number is None or episode_number is None:
            season_number, episode_number = self._extract_season_episode_from_payload(payload)
            logger.info("_process_tv: extracted season=%s, episode=%s from payload", season_number, episode_number)

        # Feature #2: Check for existing item FIRST (highest priority)
        existing_item, created = self._find_existing_item(
            user, MediaTypes.TV.value, ids,
        )

        if existing_item and not created:
            logger.info(
                "Found existing item for TV episode (%s), "
                "updating progress instead of creating a new entry",
                existing_item.source,
            )
            self._update_existing_item(existing_item, payload, user)
            return

        # Feature #1: Try to resolve media ID using user's preferred provider
        resolved_media_id, resolved_source, resolved_season, resolved_episode = (
            self._resolve_media_id_to_preferred_source(
                user, MediaTypes.TV.value, ids, season_number, episode_number,
            )
        )

        # None of the options are enabled, fall back to using TMDB, if possible
        if (
            resolved_media_id is None and
            ids.get("tmdb_id") is not None and
            season_number is not None and
            episode_number is not None
        ):
            resolved_media_id = int(ids.get("tmdb_id"))
            resolved_source = Sources.TMDB.value
            resolved_season = season_number
            resolved_episode = episode_number


        if resolved_media_id is not None and resolved_season is not None and resolved_episode is not None:
            logger.info(
                "Tracking TV episode under source=%s (ID=%s, S%s E%s)",
                resolved_source,
                resolved_media_id,
                resolved_season,
                resolved_episode,
            )
            # Use _handle_tv_episode with the resolved source;
            # _get_tv_metadata override ensures metadata comes from the right provider.
            self._handle_tv_episode(
                resolved_media_id,
                resolved_season,
                resolved_episode,
                payload,
                user,
                source=resolved_source,
            )
            return

        # Fallback: Normal TMDB-first processing via parent class
        super()._process_tv(payload, user, ids, season_number, episode_number)

    def _process_movie(self, payload, user, ids):
        """Process movie webhook with optional existing-item matching.

        Feature #2 takes priority; Feature #1 does not apply to movies
        (movies are always tracked under TMDB).
        """
        # Feature #2: Check for existing item first
        existing_item, created = self._find_existing_item(
            user, MediaTypes.MOVIE.value, ids,
        )

        if existing_item and not created:
            logger.info(
                "Found existing item for movie (%s), "
                "updating progress instead of creating",
                existing_item.source,
            )
            self._update_existing_item(existing_item, payload, user)
            return

        # Feature #1 does not apply to movies; fall through to base class
        super()._process_movie(payload, user, ids)

    # -- Provider-aware metadata fetching -------------------------------

    def _get_tv_metadata(self, media_id, season_numbers, source):
        """Return TV metadata from the given provider source.

        Overrides the base-class implementation to support TVDB and MAL in
        addition to the default TMDB backend.
        """
        logger.info("_get_tv_metadata: media_id=%s, season_numbers=%s, source=%s", media_id, season_numbers, source)
        if source == Sources.TMDB.value:
            logger.info("_get_tv_metadata: using TMDB provider")
            result = app.providers.tmdb.tv_with_seasons(media_id, season_numbers)
            logger.info("_get_tv_metadata: TMDB result keys=%s", list(result.keys()) if result else None)
            return result

        if source == Sources.TVDB.value:
            logger.info("_get_tv_metadata: using TVDB provider")
            result = app.providers.tvdb.tv_with_seasons(
                media_id, season_numbers, routed_media_type=MediaTypes.TV.value,
            )
            logger.info("_get_tv_metadata: TVDB result keys=%s", list(result.keys()) if result else None)
            return result

        # MAL / fallback: use TMDB for season structure
        logger.info("_get_tv_metadata: falling back to TMDB for source=%s, media_id=%s", source, media_id)
        result = app.providers.tmdb.tv_with_seasons(media_id, season_numbers)
        logger.info("_get_tv_metadata: TMDB fallback result keys=%s", list(result.keys()) if result else None)
        return result

    def _queue_collection_metadata_update_for_tv(self, payload, user, tv_item):
        """Queue collection metadata update for TV show (not episode-specific)."""
        self._queue_collection_metadata_update(payload, user, tv_item)

    # -- Live playback --------------------------------------------------

    def _get_live_playback_media_type(self, payload):
        """Map Jellyfin item type to a playback card media type."""
        item_type = (
            payload.get("Item", {}).get("Type") or ""
        ).strip()
        logger.info("_get_live_playback_media_type: item_type=%s", item_type)
        if item_type == "Episode":
            logger.info("_get_live_playback_media_type: returning EPISODE")
            return MediaTypes.EPISODE.value
        if item_type == "Movie":
            logger.info("_get_live_playback_media_type: returning MOVIE")
            return MediaTypes.MOVIE.value
        logger.info("_get_live_playback_media_type: returning None")
        return None

    def _update_live_playback_state(
        self, payload, user, ids, playback_media_type,
    ):
        """Update cache-backed live playback state for home-page UI.

        Applies ``jellyfin_match_existing_enabled`` and
        ``jellyfin_provider_priority_enabled`` so the card reflects the
        user's tracking identity.
        """
        logger.info("_update_live_playback_state: payload=%s, user=%s, ids=%s, playback_media_type=%s", payload, user, ids, playback_media_type)
        event_type = JELLYFIN_EVENT_MAP.get(payload.get("Event"))
        logger.info("_update_live_playback_state: event_type=%s", event_type)
        if not event_type:
            logger.info("_update_live_playback_state: no event_type, returning early")
            return

        if playback_media_type not in (
            MediaTypes.MOVIE.value,
            MediaTypes.EPISODE.value,
        ):
            return

        item = payload.get("Item", {})
        media_id = None
        source = Sources.TMDB.value
        season_number = None
        episode_number = None

        if playback_media_type == MediaTypes.MOVIE.value:
            # Match existing tracked movie (jellyfin_match_existing_enabled)
            existing_item, _ = self._find_existing_item(
                user, MediaTypes.MOVIE.value, ids,
            )
            if existing_item:
                media_id = existing_item.media_id
                source = existing_item.source
            else:
                media_id = ids.get("tmdb_id")
        else:
            season_number, episode_number = (
                self._extract_season_episode_from_payload(payload)
            )

            # Match existing tracked TV show (jellyfin_match_existing_enabled)
            existing_item, _ = self._find_existing_item(
                user, MediaTypes.TV.value, ids,
            )
            if existing_item:
                media_id = existing_item.media_id
                source = existing_item.source
                logger.debug(
                    "Live playback: matched existing item %s (source=%s)",
                    media_id, source,
                )

            # Use user's preferred provider (jellyfin_provider_priority_enabled)
            if media_id is None:
                resolved_media_id, resolved_source, _, _ = (
                    self._resolve_media_id_to_preferred_source(
                        user, MediaTypes.TV.value, ids,
                        season_number, episode_number,
                    )
                )
                if resolved_media_id is not None:
                    media_id = resolved_media_id
                    source = resolved_source
                    logger.debug(
                        "Live playback: using preferred source=%s (id=%s)",
                        source, media_id,
                    )

            # Fallback: resolve via TMDB find
            if media_id is None:
                if ids.get("tmdb_id") is not None:
                    media_id = ids.get("tmdb_id")
                elif ids.get("tvdb_id") or ids.get("imdb_id"):
                    alt_ids = dict(ids)
                    alt_ids["tmdb_id"] = None
                    # Specifically avoid resolving episode IDs to avoid errors, 
                    # as we already have a TV ID.
                    resolved_id, _, _ = self._find_tv_media_id(alt_ids, MediaTypes.TV.value)
                    if resolved_id:
                        media_id = str(resolved_id)
                
                if media_id is None:
                    return

        # Duration / offset from Jellyfin ticks (100 ns units)
        duration_seconds = _ticks_to_seconds(item.get("RunTimeTicks"))
        offset_seconds = _ticks_to_seconds(
            payload.get("PlaybackPositionTicks")
            or item.get("PlaybackPositionTicks"),
        )

        live_playback.apply_playback_event(
            user_id=user.id,
            event_type=event_type,
            playback_media_type=playback_media_type,
            media_id=media_id,
            source=source,
            rating_key=str(item.get("Id") or "").strip() or None,
            title=item.get("Name"),
            series_title=item.get("SeriesName"),
            episode_title=(
                item.get("Name")
                if playback_media_type == MediaTypes.EPISODE.value
                else None
            ),
            season_number=season_number,
            episode_number=episode_number,
            view_offset_seconds=offset_seconds,
            duration_seconds=duration_seconds,
        )
