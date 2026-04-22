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

    def process_payload(self, payload, user):
        """Process the incoming Jellyfin webhook payload."""
        logger.debug(
            "Processing Jellyfin webhook payload keys=%s item_keys=%s",
            mapping_keys(payload),
            mapping_keys(payload.get("Item")),
        )

        event_type = payload.get("Event")
        if not self._is_supported_event(event_type):
            logger.debug("Ignoring Jellyfin webhook event type: %s", event_type)
            return

        ids = self._extract_external_ids(payload)
        logger.info(
            "Extracted Jellyfin ID presence from payload: %s",
            presence_map(ids, ("tmdb_id", "imdb_id", "tvdb_id")),
        )

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
        return event_type in ("Play", "Pause", "Stop")

    def _is_played(self, payload):
        return payload["Item"]["UserData"]["Played"]

    def _get_media_type(self, payload):
        return self.MEDIA_TYPE_MAPPING.get(payload["Item"].get("Type"))

    def _get_media_title(self, payload):
        """Get media title from payload."""
        title = None

        if self._get_media_type(payload) == MediaTypes.TV.value:
            series_name = payload["Item"].get("SeriesName")
            season_number = payload["Item"].get("ParentIndexNumber")
            episode_number = payload["Item"].get("IndexNumber")
            title = f"{series_name} S{season_number:02d}E{episode_number:02d}"

        elif self._get_media_type(payload) == MediaTypes.MOVIE.value:
            movie_name = payload["Item"].get("Name")
            year = payload["Item"].get("ProductionYear")
            title = f"{movie_name} ({year})" if movie_name and year else movie_name

        return title

    def _extract_external_ids(self, payload):
        provider_ids = payload["Item"].get("ProviderIds", {})
        return {
            "tmdb_id": provider_ids.get("Tmdb"),
            "imdb_id": provider_ids.get("Imdb"),
            "tvdb_id": provider_ids.get("Tvdb"),
        }

    def _extract_season_episode_from_payload(self, payload):
        """Extract season and episode numbers from Jellyfin payload."""
        item = payload.get("Item", {})
        season_number = item.get("ParentIndexNumber")
        episode_number = item.get("IndexNumber")

        try:
            season_number = int(season_number) if season_number is not None else None
            episode_number = int(episode_number) if episode_number is not None else None
        except (ValueError, TypeError):
            return None, None

        return season_number, episode_number

    def _extract_series_title(self, payload):
        """Extract TV series title from Jellyfin payload."""
        if self._get_media_type(payload) == MediaTypes.TV.value:
            return payload.get("Item", {}).get("SeriesName")
        return None

    # -- Feature #2: Match existing tracked items -----------------------

    def _find_existing_item(self, user, media_type, ids, season_number=None, episode_number=None):
        """Find existing tracked item by *any* known provider ID.

        Searches for items matching tmdb_id, tvdb_id, imdb_id, mal_id, etc.
        Only returns items where the user has a tracking instance with a
        status (Completed, In progress, Planning, Paused, or Dropped).

        Feature #2 Priority: check existing items **first** before applying
        provider priority.

        Returns ``(item, created)`` where ``created`` is ``False`` when a
        pre-existing item was found.
        """
        if not getattr(user, "jellyfin_match_existing_enabled", False):
            return None, True

        # Helper to check if user has a tracking instance for an item
        def has_tracking_instance(item_):
            if media_type == MediaTypes.MOVIE.value:
                return Movie.objects.filter(item=item_, user=user).exists()
            if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
                return TV.objects.filter(item=item_, user=user).exists()
            return False

        # --- Direct lookups by known fields on Item ----------------------
        # Only TMDB, TVDB, and MAL are tracking sources for TV/anime.
        # IMDB is an external identifier, not a tracking source.
        direct_lookups = [
            (Sources.TMDB.value, "tmdb_id"),
            (Sources.TVDB.value, "tvdb_id"),
        ]
        if media_type == MediaTypes.ANIME.value:
            direct_lookups.append((Sources.MAL.value, "mal_id"))

        for source, id_key in direct_lookups:
            raw_id = ids.get(id_key)
            if not raw_id:
                continue
            try:
                item = Item.objects.get(
                    media_type=media_type,
                    source=source,
                    media_id=str(raw_id),
                )
                if has_tracking_instance(item):
                    return item, False
            except Item.DoesNotExist:
                pass

        # --- Cross-provider lookup via ItemProviderLink ------------------
        if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            link_qs = ItemProviderLink.objects.filter(
                provider__in=(Sources.TMDB.value, Sources.TVDB.value, Sources.MAL.value),
                provider_media_type=media_type,
            )

            # Narrow by known IDs when available
            tmdb_id = ids.get("tmdb_id")
            tvdb_id = ids.get("tvdb_id")
            if tmdb_id or tvdb_id:
                q = Q()
                if tmdb_id:
                    q |= Q(provider=Sources.TMDB.value, provider_media_id=str(tmdb_id))
                if tvdb_id:
                    q |= Q(provider=Sources.TVDB.value, provider_media_id=str(tvdb_id))
                link_qs = link_qs.filter(q)

            for link in link_qs[:20]:
                try:
                    item = link.item
                    if (
                        item.media_type == media_type
                        and has_tracking_instance(item)
                    ):
                        return item, False
                except Exception:
                    continue

        return None, True

    def _update_existing_item(self, item, payload, user):
        """Update progress on an existing item without changing its identity provider."""
        played = self._is_played(payload)
        now = self._get_played_at(payload) or timezone.now().replace(second=0, microsecond=0)

        if item.media_type == MediaTypes.MOVIE.value:
            self._update_movie_instance(item, user, played, now)
        elif item.media_type == MediaTypes.TV.value:
            self._update_tv_season_episode(item, payload, user, played, now)

    def _update_movie_instance(self, item, user, played, now):
        """Create or update a Movie tracking instance."""
        instance, created = Movie.objects.get_or_create(
            item=item,
            user=user,
            defaults={
                "status": Status.COMPLETED.value if played else Status.IN_PROGRESS.value,
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

    def _update_tv_season_episode(self, item, payload, user, played, now):
        """Create or update a Season / Episode tracking instance for an existing TV show item."""
        # We need season/episode numbers to create Episode records.
        season_number, episode_number = self._extract_season_episode_from_payload(payload)
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
            latest_episode = (
                season_instance.episodes.filter(item=season_instance.item)
                .order_by("-end_date")
                .first()
            )
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
                        time_diff = abs((now - latest.end_date).total_seconds())
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
                logger.debug("Episode creation best-effort failed (non-critical)", exc_info=True)

    # -- Feature #1: Provider priority for tracking source --------------

    def _get_jellyfin_preferred_source(self, user, media_type):
        """Return the user's preferred tracking source for the given media type.

        Returns the preferred source string (e.g. ``'mal'``, ``'tvdb'``) or
        ``None`` when the setting is disabled / unknown — letting the caller
        fall through to normal TMDB-first processing.
        """
        if not getattr(user, "jellyfin_provider_priority_enabled", False):
            return None

        if media_type == MediaTypes.TV.value:
            return getattr(user, "tv_metadata_source_default", None)

        if media_type == MediaTypes.ANIME.value:
            return getattr(user, "anime_metadata_source_default", None)

        # Movies always use TMDB — no override needed
        return None

    def _resolve_media_id_to_preferred_source(self, user, media_type, ids, season_number, episode_number):
        """Resolve media ID to user's preferred provider and return ``(media_id, source, season, episode)``.

        When ``jellyfin_provider_priority_enabled`` is True this method checks
        whether the user's preferred provider has a matching ID in the webhook
        payload.  If found, the item is tracked under that provider.

        The returned ``source`` reflects **which provider supplied the final ID**,
        so items are tracked under the correct identity provider.

        Returns ``(None, None, None, None)`` when the setting is disabled or when
        no matching ID was found — signalling that fallback (normal TMDB-first)
        processing should apply.
        """
        preferred = self._get_jellyfin_preferred_source(user, media_type)
        if not preferred:
            return None, None, None, None

        ext_id = ids.get(f"{preferred}_id")
        if not ext_id:
            return None, None, None, None

        try:
            return str(ext_id), preferred, season_number, episode_number
        except Exception as exc:
            logger.debug("Failed resolving preferred provider %s: %s", preferred, exc)
            return None, None, None, None

    # -- TV/movie routing -----------------------------------------------

    def _process_tv(self, payload, user, ids, season_number=None, episode_number=None):
        """Process TV episode webhook with optional existing-item matching and provider priority.

        Priority order:

        1. **Feature #2**: Check for an existing tracked item by *any* provider ID.
        2. **Feature #1**: Resolve media ID using user's preferred provider if enabled.
        3. **Fallback**: Normal TMDB-first processing via the base class.
        """
        # Feature #2: Check for existing item FIRST (highest priority)
        existing_item, created = self._find_existing_item(
            user, MediaTypes.TV.value, ids, season_number, episode_number,
        )

        if existing_item and not created:
            logger.info(
                "Found existing item for TV episode (%s), updating progress instead of creating a new entry",
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

        if resolved_media_id is not None:
            logger.info(
                "Tracking TV episode under source=%s (ID=%d, S%d E%d)",
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
                "Found existing item for movie (%s), updating progress instead of creating",
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
        if source == Sources.TMDB.value:
            return app.providers.tmdb.tv_with_seasons(media_id, season_numbers)

        if source == Sources.TVDB.value:
            return app.providers.tvdb.tv_with_seasons(
                media_id, season_numbers, routed_media_type=MediaTypes.TV.value,
            )

        # MAL / fallback: use TMDB for season structure
        logger.debug(
            "No direct TV metadata for source=%s; falling back to TMDB for media_id=%s",
            source,
            media_id,
        )
        return app.providers.tmdb.tv_with_seasons(media_id, season_numbers)

    def _queue_collection_metadata_update_for_tv(self, payload, user, tv_item):
        """Queue collection metadata update for TV show (not episode-specific)."""
        self._queue_collection_metadata_update(payload, user, tv_item)

    # -- Live playback --------------------------------------------------

    def _get_live_playback_media_type(self, payload):
        """Map Jellyfin item type to a playback card media type."""
        item_type = (
            payload.get("Item", {}).get("Type") or ""
        ).strip()
        if item_type == "Episode":
            return MediaTypes.EPISODE.value
        if item_type == "Movie":
            return MediaTypes.MOVIE.value
        return None

    def _update_live_playback_state(
        self, payload, user, ids, playback_media_type,
    ):
        """Update cache-backed live playback state for home-page UI.

        Applies ``jellyfin_match_existing_enabled`` and
        ``jellyfin_provider_priority_enabled`` so the card reflects the
        user's tracking identity.
        """
        event_type = JELLYFIN_EVENT_MAP.get(payload.get("Event"))
        if not event_type:
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
                season_number, episode_number,
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
                if ids.get("tvdb_id") or ids.get("imdb_id"):
                    alt_ids = dict(ids)
                    alt_ids["tmdb_id"] = None
                    resolved_id, _, _ = super()._find_tv_media_id(alt_ids)
                    if resolved_id:
                        media_id = str(resolved_id)

                if media_id is None:
                    media_id = ids.get("tmdb_id")

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
