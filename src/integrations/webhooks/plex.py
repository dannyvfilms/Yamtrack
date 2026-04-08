import logging
import re

from django.utils import timezone

import app
from app import live_playback
from app.log_safety import exception_summary, mapping_keys, presence_map, safe_url
from app.models import MediaTypes, Sources
from app.providers import services
from app.services import music_scrobble
from integrations import plex as plex_api

from .base import BaseWebhookProcessor

logger = logging.getLogger(__name__)


class _TasksProxy:
    """Lazily import integrations.tasks to avoid circular imports."""

    def __getattr__(self, name):
        from integrations import tasks as tasks_module

        return getattr(tasks_module, name)


tasks = _TasksProxy()


class PlexWebhookProcessor(BaseWebhookProcessor):
    """Processor for Plex webhook events."""

    MEDIA_TYPE_MAPPING = {
        **BaseWebhookProcessor.MEDIA_TYPE_MAPPING,
        "Track": MediaTypes.MUSIC.value,
    }

    def process_payload(self, payload, user):
        """Process the incoming Plex webhook payload."""
        event_type = payload.get("event")
        logger.info("Received Plex webhook event: %s", event_type)
        logger.debug(
            "Received Plex webhook payload keys=%s metadata_keys=%s",
            mapping_keys(payload),
            mapping_keys(payload.get("Metadata")),
        )

        if not self._is_supported_event(payload.get("event")):
            logger.debug("Ignoring Plex webhook event type: %s", event_type)
            return None

        payload_user = payload["Account"]["title"].strip().lower()
        rejection_reason = self._get_user_rejection_reason(payload_user, payload, user)
        if rejection_reason is not None:
            metadata = payload.get("Metadata", {}) or {}
            media_label = self._get_media_title(payload) or metadata.get("title") or "<unknown>"
            logger.info(
                "Ignored Plex webhook event=%s title=%s for yamtrack_user=%s: %s",
                event_type,
                media_label,
                user.username,
                rejection_reason,
            )
            return None

        media_type = self._get_media_type(payload)
        if media_type == MediaTypes.MUSIC.value:
            if event_type not in ("media.play", "media.resume", "media.scrobble"):
                logger.debug(
                    "Ignoring Plex music webhook event type: %s",
                    event_type,
                )
                return None

            if not getattr(user, "music_enabled", False):
                logger.debug("Ignoring Plex music webhook because music tracking is disabled")
                return None

            music_event = self._build_music_event(payload, user)
            music_entry = music_scrobble.record_music_playback(music_event)
            if music_entry is None:
                logger.info(
                    "Processed Plex music %s event (tracking deferred)",
                    "scrobble" if music_event.completed else "play",
                )
                return None
            logger.info(
                "Processed Plex music %s event (status=%s progress=%s)",
                "scrobble" if music_event.completed else "play",
                music_entry.status,
                music_entry.progress,
            )
            
            # Queue collection metadata update for music
            if music_entry.item:
                logger.debug(
                    "Queueing collection metadata update for Plex music track",
                )
                self._queue_collection_metadata_update(payload, user, music_entry.item)
            else:
                logger.warning(
                    "Cannot queue collection metadata update: music_entry has no item"
                )
            
            return music_entry

        # Handle rating events separately
        if event_type == "media.rate":
            return self._process_rating(payload, user)

        ids = self.resolve_external_ids(
            payload,
            allow_title_search=event_type not in ("media.pause", "media.stop"),
        )
        logger.info(
            "Extracted Plex ID presence from payload: %s",
            presence_map(ids, ("tmdb_id", "imdb_id", "tvdb_id", "anidb_id")),
        )

        playback_media_type = self._get_live_playback_media_type(payload)
        self._update_live_playback_state(
            payload,
            user,
            ids,
            playback_media_type,
        )

        if event_type in ("media.pause", "media.stop"):
            return None

        if not any(
            ids.get(key)
            for key in ("tmdb_id", "imdb_id", "tvdb_id", "anidb_id")
        ):
            logger.warning("Ignoring Plex webhook call because no ID was found.")
            return None

        self._process_media(payload, user, ids)
        return None

    def _get_live_playback_media_type(self, payload):
        """Map raw Plex metadata type into a playback card media type."""
        metadata_type = ((payload.get("Metadata", {}) or {}).get("type") or "").strip().lower()
        if metadata_type == "episode":
            return MediaTypes.EPISODE.value
        if metadata_type == "movie":
            return MediaTypes.MOVIE.value
        return None

    def _update_live_playback_state(
        self,
        payload,
        user,
        ids,
        playback_media_type,
    ):
        """Update cache-backed live playback state for home-page UI."""
        if playback_media_type not in (MediaTypes.MOVIE.value, MediaTypes.EPISODE.value):
            return

        event_type = payload.get("event")
        media_id = None
        season_number = None
        episode_number = None

        if playback_media_type == MediaTypes.MOVIE.value:
            media_id = ids.get("tmdb_id")
        else:
            season_number, episode_number = self._extract_season_episode_from_payload(
                payload,
            )
            resolve_media_id = event_type in (
                "media.play", "media.resume", "media.scrobble",
            )
            if resolve_media_id:
                # Prefer TVDB/IMDB resolution — they reliably return the
                # show-level TMDB ID via the TMDB find API.  The raw
                # tmdb_id from Plex GUIDs is often an episode-level ID
                # that would 404 on /tv/{id}.
                if ids.get("tvdb_id") or ids.get("imdb_id"):
                    alt_ids = dict(ids)
                    alt_ids["tmdb_id"] = None
                    resolved_id, _, _ = super()._find_tv_media_id(alt_ids)
                    if resolved_id:
                        media_id = str(resolved_id)
                        logger.debug(
                            "Live playback resolved show ID via TVDB/IMDB",
                        )

                # Fallback: title search then raw tmdb_id
                if media_id is None:
                    series_title = self._extract_series_title(payload)
                    resolved_media_id, _, _ = self._find_tv_media_id(
                        ids,
                        series_title=series_title,
                        allow_title_fallback=True,
                    )
                    if resolved_media_id:
                        media_id = str(resolved_media_id)
            if media_id is None:
                media_id = ids.get("tmdb_id")

        live_playback.apply_plex_event(
            user_id=user.id,
            payload=payload,
            playback_media_type=playback_media_type,
            media_id=media_id,
            source=Sources.TMDB.value,
            season_number=season_number,
            episode_number=episode_number,
        )

    def resolve_external_ids(self, payload, allow_title_search=True):
        """Extract external IDs, optionally allowing title search fallback."""
        ids = self._extract_external_ids(payload)
        if allow_title_search:
            ids = self._resolve_ids_if_missing(payload, ids)
        return ids

    def _resolve_ids_if_missing(self, payload, ids):
        """Attempt to resolve TMDB ID when it is missing from extracted IDs."""
        if ids.get("tmdb_id"):
            return ids

        media_type = self._get_media_type(payload)
        # Attempt TMDB 'find' if we have an external ID (TVDB or IMDB)
        external_id = ids.get("tvdb_id") or ids.get("imdb_id")
        if external_id and media_type in (MediaTypes.TV.value, MediaTypes.MOVIE.value):
            source = "tvdb_id" if ids.get("tvdb_id") else "imdb_id"
            try:
                from app.providers import tmdb
                find_results = tmdb.find(external_id, source)

                tmdb_id = None
                if media_type == MediaTypes.TV.value:
                    episode_results = find_results.get("tv_episode_results") or []
                    if episode_results:
                        tmdb_id = episode_results[0].get("show_id")
                    else:
                        tv_results = find_results.get("tv_results") or []
                        if tv_results:
                            tmdb_id = tv_results[0].get("id")
                else:
                    results = find_results.get("movie_results") or []
                    if results:
                        tmdb_id = results[0].get("id")

                if tmdb_id:
                    ids["tmdb_id"] = str(tmdb_id)
                    logger.info("Resolved Plex external ID to TMDB using find API")
                    return ids

                logger.debug("TMDB find returned no results for source=%s", source)
            except Exception as exc:
                logger.warning(
                    "TMDB find fallback failed for source=%s: %s",
                    source,
                    exception_summary(exc),
                )

        # Fallback to title search for TV shows and Movies
        if media_type not in (MediaTypes.TV.value, MediaTypes.MOVIE.value):
            return ids

        metadata = payload.get("Metadata", {})
        # For episodes, use series title (grandparentTitle) falling back to episode title if needed
        # For movies, use the movie title
        search_title = (
            metadata.get("grandparentTitle")
            or metadata.get("title")
            if media_type == MediaTypes.TV.value
            else metadata.get("title")
        )
        original_date = metadata.get("originallyAvailableAt") or metadata.get("year")

        if not search_title:
            logger.debug("Cannot resolve plex:// GUID without title")
            return ids

        try:
            from app.providers import services
            search_results = services.search(
                media_type,
                search_title,
                page=1,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception("Failed TMDB search while resolving plex:// GUID")
            return ids

        tmdb_id = None
        results = search_results.get("results") or []

        if original_date:
            year = str(original_date).split("-")[0]
            for result in results:
                # Use first_air_date for TV, release_date for movies
                date_key = (
                    "first_air_date"
                    if media_type == MediaTypes.TV.value
                    else "release_date"
                )
                result_date = result.get("details", {}).get(date_key) or ""
                if str(result_date).startswith(year):
                    tmdb_id = result.get("media_id")
                    break

        if not tmdb_id and results:
            tmdb_id = results[0].get("media_id")

        if tmdb_id:
            ids["tmdb_id"] = str(tmdb_id)
            logger.info("Resolved plex:// GUID via title search")
        else:
            logger.debug("Title search returned no Plex GUID match")

        return ids

    def _find_tv_media_id(self, ids, series_title=None, allow_title_fallback=False):
        """
        Resolve TMDB ID for a TV show using external IDs or title search.

        Returns:
            tuple: (media_id, season_number, episode_number)
                   season/episode are None unless extracted from specific lookup context,
                   checking primarily for show ID here.
        """
        # 1. Try resolving using existing IDs (TMDB, TVDB, IMDB)
        tmdb_id = ids.get("tmdb_id")
        if tmdb_id:
            return str(tmdb_id), None, None

        # 2. If TMDB ID is unavailable, resolve from TVDB/IMDB via TMDB find API.
        # This is used by _process_tv fallback when Plex supplied an episode-level
        # TMDB ID that failed against /tv/{id}.
        media_id, season_number, episode_number = super()._find_tv_media_id(ids)
        if media_id:
            return media_id, season_number, episode_number

        if not allow_title_fallback or not series_title:
            return None, None, None

        # 3. Try title search
        logger.debug("TV ID missing; attempting title fallback search")
        try:
            search_results = services.search(
                MediaTypes.TV.value,
                series_title,
                page=1,
            )
            results = search_results.get("results") or []
            if results:
                found_id = results[0].get("media_id")
                logger.info("Resolved Plex TV entry via title search")
                return str(found_id), None, None
            
            logger.debug("Title search returned no results for Plex TV entry")

            # Try stripping year from title like "Show (YYYY)"
            clean_title = re.sub(r'\s*\(\d{4}\)$', '', series_title[:500])
            if clean_title != series_title:
                logger.debug("Retrying Plex TV search with normalized title")
                search_results = services.search(
                    MediaTypes.TV.value,
                    clean_title,
                    page=1,
                )
                results = search_results.get("results") or []
                if results:
                    found_id = results[0].get("media_id")
                    logger.info("Resolved Plex TV entry via normalized title search")
                    return str(found_id), None, None

        except Exception as exc:
            logger.warning(
                "Title search failed during Plex TV resolution: %s",
                exception_summary(exc),
            )

        return None, None, None


    def _process_rating(self, payload, user):
        """Process media.rate webhook events to update user ratings.
        
        Note: Plex may not send media.rate webhook events reliably.
        Ratings are primarily synced via the Plex import process which
        fetches ratings from library items.
        """
        logger.info("Processing media.rate webhook event")
        logger.debug(
            "Plex rating payload keys=%s metadata_keys=%s",
            mapping_keys(payload),
            mapping_keys(payload.get("Metadata")),
        )
        
        metadata = payload.get("Metadata", {})
        # Try different possible field names for user rating (preserve 0 values)
        user_rating = None
        rating_source = None
        rating_fields = [
            ("userRating", metadata.get("userRating")),
            ("user_rating", metadata.get("user_rating")),
            ("rating", metadata.get("rating")),
            ("payload_rating", payload.get("rating")),
            ("payload_userRating", payload.get("userRating")),
        ]
        for source, value in rating_fields:
            if value is not None:
                user_rating = value
                rating_source = source
                break
        
        logger.debug("Rating payload metadata keys: %s", list(metadata.keys()))
        logger.debug(
            "Plex rating payload contains user_rating=%s source=%s",
            user_rating is not None,
            rating_source,
        )
        
        if user_rating is None:
            logger.warning(
                "No userRating found in Plex rating payload. "
                "Available metadata keys: %s, Top-level payload keys: %s",
                list(metadata.keys()),
                list(payload.keys()),
            )
            # Try fetching rating from Plex API as fallback
            rating_key = metadata.get("ratingKey") or metadata.get("ratingkey")
            if rating_key:
                logger.info("Attempting to fetch rating from Plex API")
                user_rating = self._fetch_rating_from_plex_api(user, rating_key, payload)
                if user_rating is None:
                    logger.warning("Could not fetch rating from Plex API either")
                    return None
                rating_source = "userRating"
            else:
                logger.warning("No ratingKey found in payload, cannot fetch rating from API")
                return None

        title = self._get_media_title(payload)
        media_type = self._get_media_type(payload)
        
        logger.debug("Plex rating payload media_type=%s", media_type)
        
        if not media_type:
            logger.warning("Ignoring rating for unsupported media type. Payload type: %s", metadata.get("type"))
            return None

        # Check if this is a rating removal event (-1.0)
        try:
            rating_float = float(user_rating)
            if rating_float == -1.0:
                logger.info("Detected Plex webhook rating removal for media_type=%s", media_type)
                # Resolve external IDs for removal
                ids = self.resolve_external_ids(payload)
                if not any(ids.get(key) for key in ("tmdb_id", "imdb_id", "tvdb_id")):
                    logger.warning("Ignoring Plex rating removal webhook because no ID was found")
                    return None
                # Handle rating removal
                self._remove_rating(payload, user, ids, media_type)
                return None
        except (TypeError, ValueError):
            # Not a numeric value, continue with normal processing
            pass

        # Normalize rating
        normalized_rating = self._normalize_rating(
            user_rating,
            title,
            rating_source=rating_source,
        )
        if normalized_rating is None:
            logger.warning("Invalid Plex rating value received; skipped")
            return None

        logger.info("Processing Plex rating for media_type=%s", media_type)

        # Resolve external IDs
        ids = self.resolve_external_ids(payload)
        if not any(ids.get(key) for key in ("tmdb_id", "imdb_id", "tvdb_id")):
            logger.warning("Ignoring Plex rating webhook because no ID was found")
            return None

        # Apply rating based on media type
        if media_type == MediaTypes.MOVIE.value:
            self._apply_movie_rating(payload, user, ids, normalized_rating)
        elif media_type == MediaTypes.TV.value:
            # For TV, apply rating to the show (not episode-specific)
            self._apply_tv_rating(payload, user, ids, normalized_rating)
        else:
            logger.debug("Rating sync not supported for media type: %s", media_type)
            return None

        return normalized_rating

    def _apply_movie_rating(self, payload, user, ids, rating):
        """Apply rating to a movie instance."""
        from app.models import Sources, Status

        tmdb_id = ids.get("tmdb_id")
        if not tmdb_id:
            logger.warning("Cannot apply movie rating: no TMDB ID found")
            return

        try:
            movie_metadata = app.providers.tmdb.movie(tmdb_id)
        except Exception as exc:
            logger.warning(
                "Failed to fetch movie metadata during Plex rating sync: %s",
                exception_summary(exc),
            )
            return

        movie_item, _ = app.models.Item.objects.get_or_create(
            media_id=tmdb_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            defaults={
                "title": movie_metadata["title"],
                "image": movie_metadata["image"],
            },
        )

        # Get or create movie instance
        movie_instance, created = app.models.Movie.objects.get_or_create(
            item=movie_item,
            user=user,
            defaults={
                "status": Status.COMPLETED.value,
                "progress": 1,
            },
        )

        # Update rating (Plex is master, overwrites existing)
        movie_instance.score = rating
        movie_instance.save(update_fields=["score"])

        action = "Created" if created else "Updated"
        logger.info(
            "%s movie rating from Plex webhook",
            action,
        )

    def _apply_tv_rating(self, payload, user, ids, rating):
        """Apply rating to a TV show instance (show-level rating)."""
        from app.models import Sources, Status

        tmdb_id = ids.get("tmdb_id")
        if not tmdb_id:
            logger.warning("Cannot apply TV rating: no TMDB ID found")
            return

        try:
            tv_metadata = app.providers.tmdb.tv(tmdb_id)
        except Exception as exc:
            logger.warning(
                "Failed to fetch TV metadata during Plex rating sync: %s",
                exception_summary(exc),
            )
            return

        tv_item, _ = app.models.Item.objects.get_or_create(
            media_id=tmdb_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            defaults={
                "title": tv_metadata["title"],
                "image": tv_metadata["image"],
            },
        )

        # Get or create TV instance
        tv_instance, created = app.models.TV.objects.get_or_create(
            item=tv_item,
            user=user,
            defaults={
                "status": Status.IN_PROGRESS.value,
            },
        )

        # Update rating (Plex is master, overwrites existing)
        tv_instance.score = rating
        tv_instance.save(update_fields=["score"])

        action = "Created" if created else "Updated"
        logger.info(
            "%s TV rating from Plex webhook",
            action,
        )

    def _remove_rating(self, payload, user, ids, media_type):
        """Remove rating from a movie or TV instance.
        
        Only removes ratings from existing instances; does not create new instances.
        """
        from app.models import Sources

        tmdb_id = ids.get("tmdb_id")
        if not tmdb_id:
            logger.warning("Cannot remove rating: no TMDB ID found")
            return

        if media_type == MediaTypes.MOVIE.value:
            try:
                movie_metadata = app.providers.tmdb.movie(tmdb_id)
            except Exception as exc:
                logger.warning(
                    "Failed to fetch movie metadata for Plex rating removal: %s",
                    exception_summary(exc),
                )
                return

            movie_item, _ = app.models.Item.objects.get_or_create(
                media_id=tmdb_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                defaults={
                    "title": movie_metadata["title"],
                    "image": movie_metadata["image"],
                },
            )

            # Only remove rating from existing instances
            movie_instance = app.models.Movie.objects.filter(
                item=movie_item,
                user=user,
            ).first()

            if movie_instance:
                movie_instance.score = None
                movie_instance.save(update_fields=["score"])
                logger.info("Removed movie rating from Plex webhook")
            else:
                logger.debug("No movie instance found for Plex rating removal")

        elif media_type == MediaTypes.TV.value:
            try:
                tv_metadata = app.providers.tmdb.tv(tmdb_id)
            except Exception as exc:
                logger.warning(
                    "Failed to fetch TV metadata for Plex rating removal: %s",
                    exception_summary(exc),
                )
                return

            tv_item, _ = app.models.Item.objects.get_or_create(
                media_id=tmdb_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.TV.value,
                defaults={
                    "title": tv_metadata["title"],
                    "image": tv_metadata["image"],
                },
            )

            # Only remove rating from existing instances
            tv_instance = app.models.TV.objects.filter(
                item=tv_item,
                user=user,
            ).first()

            if tv_instance:
                tv_instance.score = None
                tv_instance.save(update_fields=["score"])
                logger.info("Removed TV rating from Plex webhook")
            else:
                logger.debug("No TV instance found for Plex rating removal")
        else:
            logger.debug("Rating removal not supported for media type: %s", media_type)

    def _process_media(self, payload, user, ids):
        """Route processing based on media type, extracting season/episode for TV."""
        media_type = self._get_media_type(payload)
        if not media_type:
            logger.debug("Ignoring unsupported media type")
            return

        logger.info("Received Plex webhook for media_type=%s", media_type)

        if media_type == MediaTypes.TV.value:
            # Extract season/episode from Plex payload
            season_number, episode_number = self._extract_season_episode_from_payload(
                payload,
            )
            self._process_tv(payload, user, ids, season_number, episode_number)
        elif media_type == MediaTypes.MOVIE.value:
            self._process_movie(payload, user, ids)

    def _is_supported_event(self, event_type):
        return event_type in (
            "media.scrobble",
            "media.play",
            "media.resume",
            "media.pause",
            "media.stop",
            "media.rate",
        )

    def _fetch_rating_from_plex_api(self, user, rating_key, payload):
        """Fetch user rating from Plex API as fallback if not in webhook payload."""

        plex_account = getattr(user, "plex_account", None)
        if not plex_account or not plex_account.plex_token:
            logger.debug("No Plex account found for rating fetch")
            return None

        # Get server URI from payload or account
        plex_uri = None
        server_info = payload.get("Server", {})
        if server_info:
            if isinstance(server_info, dict):
                plex_uri = server_info.get("uri") or server_info.get("Uri")
            elif isinstance(server_info, str):
                plex_uri = server_info

        if not plex_uri and plex_account.sections:
            for section in plex_account.sections:
                if isinstance(section, dict):
                    section_uri = section.get("uri")
                    if section_uri:
                        plex_uri = section_uri
                        break

        if not plex_uri:
            logger.debug("No Plex server URI found for rating fetch")
            return None

        try:
            metadata = plex_api.fetch_metadata(
                plex_account.plex_token,
                plex_uri,
                str(rating_key),
            )
            if metadata:
                user_rating = metadata.get("userRating")
                logger.debug("Fetched user rating from Plex API")
                return user_rating
        except Exception as exc:
            logger.warning(
                "Failed to fetch rating from Plex API: %s",
                exception_summary(exc),
            )

        return None

    def _normalize_rating(
        self,
        rating_value,
        title: str | None = None,  # noqa: ARG002 - kept for caller compatibility
        rating_source: str | None = None,
    ) -> float | None:
        """Normalize Plex rating values onto a 0-10 scale.
        
        Plex userRating values are typically on a 0-10 scale (even for 5-star UI).
        Some metadata sources may report 0-100, which we normalize down.
        """
        if rating_value in (None, ""):
            return None

        try:
            rating = float(rating_value)
        except (TypeError, ValueError):
            logger.warning("Invalid Plex rating received (non-numeric)")
            return None

        if rating < 0:
            logger.warning("Invalid Plex rating received (negative)")
            return None

        if rating_source in {"userRating", "user_rating", "payload_userRating"}:
            if rating <= 10:
                rating = rating
            elif rating <= 100:
                rating /= 10
            else:
                logger.warning("Invalid Plex rating received (out of range)")
                return None
        elif rating <= 5:
            rating *= 2
        elif rating <= 10:
            rating = rating
        elif rating <= 100:
            rating /= 10
        else:
            logger.warning("Invalid Plex rating received (out of range)")
            return None

        rating = round(rating, 1)
        if rating < 0 or rating > 10:
            logger.warning("Invalid Plex rating received (normalized out of range)")
            return None

        return rating

    def _is_valid_user(self, payload_user, payload, user):
        return self._get_user_rejection_reason(payload_user, payload, user) is None

    def _get_user_rejection_reason(self, payload_user, payload, user):
        stored_usernames = {
            u.strip().lower()
            for u in (user.plex_usernames or "").split(",")
            if u.strip()
        }
        plex_account = getattr(user, "plex_account", None)
        plex_username = str(getattr(plex_account, "plex_username", "") or "").strip()
        if plex_username:
            stored_usernames.add(plex_username.lower())

        payload_usernames = {
            candidate
            for candidate in [
                payload_user,
                self._extract_payload_username(payload),
            ]
            if candidate
        }
        logger.debug(
            "Checking Plex webhook payload user against configured usernames",
        )

        if stored_usernames and payload_usernames.intersection(stored_usernames):
            return self._get_library_rejection_reason(payload, user)

        payload_account_id = self._extract_payload_account_id(payload)
        connected_account_id = str(
            getattr(plex_account, "plex_account_id", "") or "",
        ).strip()
        if (
            payload_account_id
            and connected_account_id
            and payload_account_id == connected_account_id
        ):
            return self._get_library_rejection_reason(payload, user)

        configured_usernames = sorted(stored_usernames) if stored_usernames else ["<none>"]
        payload_username_values = sorted(payload_usernames) if payload_usernames else ["<none>"]
        return (
            "payload user/account did not match configured Plex identity "
            f"(payload_usernames={payload_username_values}, "
            f"payload_account_id={payload_account_id or '<none>'}, "
            f"configured_usernames={configured_usernames}, "
            f"connected_account_id={connected_account_id or '<none>'})"
        )

    def _extract_payload_username(self, payload):
        """Extract any alternate Plex username field from a webhook payload."""
        for value in (
            payload.get("user"),
            payload.get("owner"),
        ):
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        return None

    def _extract_payload_account_id(self, payload):
        """Extract Plex account id from webhook payload when available."""
        account = payload.get("Account", {}) or {}
        for key in ("id", "accountID", "accountId", "account_id"):
            value = account.get(key)
            if value is None:
                continue
            value = str(value).strip()
            if value:
                return value
        return None

    def _is_valid_library(self, payload, user):
        return self._get_library_rejection_reason(payload, user) is None

    def _get_library_rejection_reason(self, payload, user):
        selected_libraries = user.plex_webhook_libraries
        if not selected_libraries:
            return None

        machine_identifier = payload.get("Server", {}).get("uuid")
        section_id = payload.get("Metadata", {}).get("librarySectionID")
        if not machine_identifier or not section_id:
            logger.debug(
                "Rejecting Plex webhook event because library info is missing while library filtering is enabled.",
            )
            return (
                "library filtering is enabled but the webhook payload is missing "
                "Server.uuid or Metadata.librarySectionID"
            )

        payload_library = f"{machine_identifier}::{section_id}"
        logger.debug(
            "Checking Plex webhook payload library against configured libraries",
        )
        if payload_library in selected_libraries:
            return None

        return (
            f"payload library {payload_library} is not selected "
            f"(selected_libraries={sorted(selected_libraries)})"
        )

    def _is_played(self, payload):
        return payload["event"] == "media.scrobble"

    def _get_media_type(self, payload):
        media_type = payload["Metadata"].get("type")
        if not media_type:
            return None

        return self.MEDIA_TYPE_MAPPING.get(media_type.title())

    def _get_media_title(self, payload):
        """Get media title from payload."""
        title = None

        media_type = self._get_media_type(payload)

        if media_type == MediaTypes.TV.value:
            series_name = payload["Metadata"].get("grandparentTitle")
            season_number = payload["Metadata"].get("parentIndex")
            episode_number = payload["Metadata"].get("index")
            title = f"{series_name} S{season_number:02d}E{episode_number:02d}"

        elif media_type == MediaTypes.MOVIE.value:
            title = payload["Metadata"].get("title")

        elif media_type == MediaTypes.MUSIC.value:
            metadata = payload.get("Metadata", {})
            artist = metadata.get("grandparentTitle")
            track = metadata.get("title")
            if artist and track:
                title = f"{artist} - {track}"
            else:
                title = track or artist

        return title

    def _extract_series_title(self, payload):
        """Extract TV series title from Plex payload."""
        if self._get_media_type(payload) == MediaTypes.TV.value:
            return payload.get("Metadata", {}).get("grandparentTitle")
        return None

    def _extract_external_ids(self, payload):
        metadata = payload.get("Metadata", {})
        guids = metadata.get("Guid", [])
        if not guids:
            single_guid = metadata.get("guid")
            if single_guid:
                guids = [{"id": single_guid}]

        ids = {
            "tmdb_id": None,
            "imdb_id": None,
            "tvdb_id": None,
            "plex_guid": None,
            "anidb_id": None,
        }

        logger.debug("Extracting external IDs from %d GUIDs", len(guids))

        def extract_hama_anidb_id(guid_value):
            """Extract the AniDB ID from a Hama agent GUID string."""
            if not guid_value:
                return None

            guid_lower = guid_value.lower()
            if "hama://anidb-" not in guid_lower:
                return None

            match = re.search(r"anidb-(\d+)", guid_lower)
            if match:
                return match.group(1)
            return None

        for guid in guids:
            guid_value = guid.get("id") if isinstance(guid, dict) else guid
            if not guid_value:
                continue

            guid_lower = guid_value.lower()

            if ids["anidb_id"] is None:
                anidb_id = extract_hama_anidb_id(guid_value)
                if anidb_id:
                    ids["anidb_id"] = anidb_id
                    logger.debug("Found AniDB ID in Plex GUIDs")

            if ids["plex_guid"] is None and guid_lower.startswith("plex://"):
                ids["plex_guid"] = guid_value.split("plex://", 1)[1]
                logger.debug("Found Plex GUID in payload")

            # Priority 1: Explicitly labeled IMDB or 'tt' prefix anywhere
            if ids["imdb_id"] is None:
                imdb_id = self._extract_imdb_id(guid_value)
                if imdb_id:
                    ids["imdb_id"] = imdb_id
                    logger.debug("Found IMDB ID in Plex GUIDs")
                    if "imdb" in guid_lower:
                        continue

            # Priority 2: TMDB
            if ids["tmdb_id"] is None and (
                "tmdb" in guid_lower or "themoviedb" in guid_lower
            ):
                tmdb_id = self._extract_numeric_guid_id(guid_value)
                if tmdb_id:
                    # If it looks like an IMDB ID (7+ digits) and we don't have an IMDB ID yet,
                    # AND it's a TV show, be skeptical of treating it as TMDB.
                    if int(tmdb_id) > 3000000 and ids["imdb_id"] is None:
                        ids["imdb_id"] = f"tt{tmdb_id}"
                        logger.debug("Skeptically treated large TMDB-style ID as IMDB")
                    else:
                        ids["tmdb_id"] = tmdb_id
                        logger.debug("Found TMDB ID in Plex GUIDs")

            # Priority 3: TVDB
            if ids["tvdb_id"] is None and ("tvdb" in guid_lower or "thetvdb" in guid_lower):
                tvdb_id = self._extract_numeric_guid_id(guid_value)
                if tvdb_id:
                    ids["tvdb_id"] = tvdb_id
                    logger.debug("Found TVDB ID in Plex GUIDs")

            if all(
                ids.get(key)
                for key in ("tmdb_id", "imdb_id", "tvdb_id", "plex_guid", "anidb_id")
            ):
                break

        return ids

    def _extract_numeric_guid_id(self, guid_value):
        """Extract the first numeric identifier from a Plex GUID string."""
        cleaned = guid_value.split("?", 1)[0]
        if "://" in cleaned:
            cleaned = cleaned.split("://", 1)[1]
        cleaned = cleaned.lstrip("/")
        if "/" in cleaned:
            cleaned = cleaned.split("/", 1)[0]

        match = re.search(r"\d+", cleaned)
        return match.group(0) if match else None

    def _extract_imdb_id(self, guid_value):
        """Extract IMDB ID from a Plex GUID string."""
        match = re.search(r"tt\d+", guid_value)
        return match.group(0) if match else None

    def _extract_music_ids(self, metadata):
        """Extract MusicBrainz IDs from a Plex track payload."""
        guids = metadata.get("Guid", [])
        if not guids:
            single_guid = metadata.get("guid")
            if single_guid:
                guids = [{"id": single_guid}]

        ids = {}
        for guid in guids:
            guid_value = guid.get("id") or ""
            guid_lower = guid_value.lower()
            uuid = self._extract_uuid(guid_value)

            if "musicbrainz" in guid_lower or "mbid" in guid_lower:
                if "recording" in guid_lower or "track" in guid_lower:
                    ids.setdefault("musicbrainz_recording", uuid or guid_value)
                elif "release-group" in guid_lower or "release_group" in guid_lower:
                    ids.setdefault("musicbrainz_release_group", uuid or guid_value)
                elif "release" in guid_lower or "album" in guid_lower:
                    ids.setdefault("musicbrainz_release", uuid or guid_value)
                elif "artist" in guid_lower:
                    ids.setdefault("musicbrainz_artist", uuid or guid_value)
                else:
                    ids.setdefault("musicbrainz_recording", uuid or guid_value)

        return ids

    def _extract_uuid(self, value):
        """Extract UUID from a string."""
        match = re.search(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            value,
        )
        return match.group(0) if match else None

    def _build_music_event(self, payload, user):
        """Build a normalized music playback event from Plex payload."""
        metadata = payload.get("Metadata", {}) or {}
        played_at = self._get_played_at(payload) or timezone.now().replace(
            second=0,
            microsecond=0,
        )
        duration_ms = metadata.get("duration")
        try:
            duration_ms = int(duration_ms) if duration_ms is not None else None
        except (TypeError, ValueError):
            duration_ms = None
        track_number = metadata.get("index")
        try:
            track_number = int(track_number) if track_number is not None else None
        except (TypeError, ValueError):
            track_number = None

        return music_scrobble.MusicPlaybackEvent(
            user=user,
            artist_name=metadata.get("grandparentTitle"),
            album_title=metadata.get("parentTitle"),
            track_title=metadata.get("title") or "Unknown Track",
            track_number=track_number,
            duration_ms=duration_ms,
            plex_rating_key=metadata.get("ratingKey"),
            external_ids=self._extract_music_ids(metadata),
            completed=payload.get("event") == "media.scrobble",
            played_at=played_at,
            defer_cover_prefetch=bool(payload.get("_import_batch")),
        )

    def _extract_season_episode_from_payload(self, payload):
        """Extract season and episode numbers from Plex payload."""
        metadata = payload.get("Metadata", {})
        season_number = metadata.get("parentIndex")
        episode_number = metadata.get("index")

        # Convert to int if they exist
        try:
            season_number = int(season_number) if season_number is not None else None
            episode_number = int(episode_number) if episode_number is not None else None
        except (ValueError, TypeError):
            return None, None

        return season_number, episode_number

    def _queue_collection_metadata_update(self, payload, user, item):
        """Queue collection metadata update task for Plex webhook."""
        # Get Plex account
        plex_account = getattr(user, "plex_account", None)
        if not plex_account or not plex_account.plex_token:
            logger.debug("No Plex account found, skipping collection update")
            return

        # Extract rating key from payload
        metadata = payload.get("Metadata", {})
        rating_key = metadata.get("ratingKey") or metadata.get("ratingkey")
        if not rating_key:
            logger.debug("No rating key found in Plex payload, skipping collection update")
            return

        # Get server URI - try multiple methods, prioritizing known-good sources
        plex_uri = None
        
        # Method 1: Try to get from Server info in payload (most reliable from webhook)
        server_info = payload.get("Server", {})
        if server_info:
            if isinstance(server_info, dict):
                plex_uri = server_info.get("uri") or server_info.get("Uri")
            elif isinstance(server_info, str):
                plex_uri = server_info

        # Method 2: Use Plex account sections (known to work, already tested)
        if not plex_uri and plex_account.sections:
            # Get first section's URI
            for section in plex_account.sections:
                if isinstance(section, dict):
                    section_uri = section.get("uri")
                    if section_uri:
                        plex_uri = section_uri
                        break

        # Method 3: Try to get from Plex resources API
        if not plex_uri:
            try:
                resources = plex_api.list_resources(plex_account.plex_token)
                for resource in resources:
                    connections = resource.get("connections", [])
                    if connections:
                        # Use first connection
                        if isinstance(connections[0], dict):
                            plex_uri = connections[0].get("uri")
                        else:
                            plex_uri = connections[0]
                        if plex_uri:
                            break
            except Exception as exc:
                logger.debug(
                    "Failed to get Plex URI from resources API: %s",
                    exception_summary(exc),
                )

        # Method 4: Last resort - try Player addresses (may not be server URI)
        if not plex_uri:
            player_info = payload.get("Player", {})
            if player_info:
                if isinstance(player_info, dict):
                    # Prefer localAddress over publicAddress for local connections
                    plex_uri = player_info.get("localAddress") or player_info.get("publicAddress")

        if not plex_uri:
            logger.warning(
                "No Plex server URI found for collection update after checking payload, sections, and resources API.",
            )
            return

        # Normalize URI to ensure it has a scheme
        # If URI is just an IP address or hostname without scheme, add http://
        if plex_uri and not plex_uri.startswith(("http://", "https://")):
            # Prefer localAddress for local connections (usually http)
            # publicAddress might be remote, but default to http for compatibility
            plex_uri = f"http://{plex_uri}"
            logger.debug("Normalized Plex URI for collection update: %s", safe_url(plex_uri))

        # Queue the collection metadata update task
        try:
            tasks.update_collection_metadata_from_plex_webhook.delay(
                user.id,
                item.id,
                str(rating_key),
                plex_uri,
                plex_account.plex_token,
            )
            logger.info("Queued collection metadata update from Plex webhook")
        except Exception as exc:
            logger.warning(
                "Failed to queue collection metadata update from Plex webhook: %s",
                exception_summary(exc),
                exc_info=True,
            )
