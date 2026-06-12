import logging
import re
from datetime import UTC, datetime

from django.utils import timezone

import app
from app.log_safety import exception_summary
from app.models import MediaTypes, ProviderMetadataStatus, Sources, Status
from integrations.webhooks import anime_mappings

logger = logging.getLogger(__name__)


class BaseWebhookProcessor:
    """Base class for webhook processors."""

    MEDIA_TYPE_MAPPING = {
        "Episode": MediaTypes.TV.value,
        "Movie": MediaTypes.MOVIE.value,
    }

    def process_payload(self, payload, user):
        """Process webhook payload."""
        raise NotImplementedError

    def _get_played_at(self, payload):
        """Extract played-at timestamp if provided by the payload."""
        metadata = payload.get("Metadata", {}) or {}
        ts = (
            metadata.get("viewedAt")
            or metadata.get("lastViewedAt")
            or payload.get("viewedAt")
        )
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            return None

        played_at = datetime.fromtimestamp(ts_int, tz=UTC)
        return timezone.localtime(played_at)

    def _is_supported_event(self, event_type):
        """Check if event type is supported."""
        raise NotImplementedError

    def _is_played(self, payload):
        """Check if media is marked as played."""
        raise NotImplementedError

    def _extract_external_ids(self, payload):
        """Extract external IDs from payload."""
        raise NotImplementedError

    def _get_media_type(self, payload):
        """Get media type from payload."""
        raise NotImplementedError

    def _get_media_title(self, payload):
        """Get media title from payload."""
        raise NotImplementedError

    def _extract_season_episode_from_payload(self, payload):
        """Extract season and episode numbers from payload.
        
        Override in subclasses if payload structure differs.
        Returns (season_number, episode_number) or (None, None) if not found.
        """
        return None, None

    def _extract_series_title(self, payload):
        """Extract TV series title from payload for title-based TMDB search.
        
        Override in subclasses if payload structure differs.
        Returns series title string or None if not found.
        """
        return

    def _process_media(self, payload, user, ids):
        """Route processing based on media type."""
        media_type = self._get_media_type(payload)
        if not media_type:
            logger.debug("Ignoring unsupported media type")
            return

        logger.info("Received webhook for media_type=%s", media_type)

        if media_type == MediaTypes.TV.value:
            self._process_tv(payload, user, ids)
        elif media_type == MediaTypes.MOVIE.value:
            self._process_movie(payload, user, ids)

    def _process_tv(self, payload, user, ids, season_number=None, episode_number=None):
        """Process TV episode webhook.
        
        Args:
            payload: Webhook payload
            user: User instance
            ids: Extracted external IDs
            season_number: Season number from payload (optional, will be extracted if None)
            episode_number: Episode number from payload (optional, will be extracted if None)
        """
        anidb_id = ids.get("anidb_id")
        if user.anime_enabled and anidb_id:
            mapping_data = anime_mappings.fetch_mapping_data()
            resolved_episode = episode_number
            if resolved_episode is None:
                _, resolved_episode = self._extract_season_episode_from_payload(payload)
            mal_id = None
            mal_episode_number = None
            if not resolved_episode:
                logger.warning(
                    "No episode number found for AniDB ID: %s",
                    anidb_id,
                )
            else:
                mal_id, mal_episode_number = anime_mappings.get_mal_id_from_anidb(
                    mapping_data,
                    anidb_id,
                    resolved_episode,
                )
            if resolved_episode and not mal_id:
                logger.info(
                    "AniDB ID %s not found in mapping, falling through to TV processing",
                    anidb_id,
                )
            elif resolved_episode:
                logger.info(
                    "Detected anime via AniDB ID: %s. Matching MAL ID: %s, Episode: %d",
                    anidb_id,
                    mal_id,
                    mal_episode_number,
                )
                if self._handle_anime(mal_id, mal_episode_number, payload, user):
                    return

        media_id, found_season, found_episode = self._find_tv_media_id(ids)
        if not media_id:
            logger.warning("No matching TMDB ID found for TV show")
            return

        # Use season/episode from parameters if provided, otherwise from lookup
        season_number = season_number or found_season
        episode_number = episode_number or found_episode

        # If we still don't have season/episode, try to get from payload
        if season_number is None or episode_number is None:
            season_number, episode_number = self._extract_season_episode_from_payload(
                payload,
            )

        if season_number is None or episode_number is None:
            logger.warning(
                "Could not determine season/episode numbers for webhook payload",
            )
            return

        # Pull TMDB metadata; if the TMDB ID is actually episode-level, fall back to
        # TVDB/IMDB to resolve the show ID instead of erroring and losing the scrobble.
        tv_metadata = None
        try:
            tv_metadata = app.providers.tmdb.tv_with_seasons(media_id, [season_number])
        except Exception as exc:  # pragma: no cover - defensive network guard
            logger.warning(
                "Failed tmdb.tv_with_seasons for season %s: %s",
                season_number,
                exception_summary(exc),
            )

            # If TMDB lookup failed, try resolving the show via TVDB/IMDB and retry.
            fallback_media_id = None
            if ids.get("tmdb_id") and (ids.get("tvdb_id") or ids.get("imdb_id")):
                alt_ids = dict(ids)
                alt_ids["tmdb_id"] = None
                fallback_media_id, alt_season, alt_episode = self._find_tv_media_id(
                    alt_ids,
                )

                if fallback_media_id:
                    media_id = fallback_media_id
                    season_number = season_number or alt_season
                    episode_number = episode_number or alt_episode
                    self._remember_tvdb_override(media_id, ids)
                    try:
                        tv_metadata = app.providers.tmdb.tv_with_seasons(
                            media_id,
                            [season_number],
                        )
                        logger.info("Recovered TMDB lookup using TVDB/IMDB mapping")
                    except Exception as fallback_exc:  # pragma: no cover - defensive
                        logger.warning(
                            "Fallback tmdb.tv_with_seasons failed: %s",
                            exception_summary(fallback_exc),
                        )
                        fallback_media_id = None  # Mark as failed so title search runs

            # Last resort: search by title if all ID-based lookups failed
            if not fallback_media_id and not tv_metadata:
                series_title = self._extract_series_title(payload)
                if series_title:
                    logger.info("Attempting title-based TMDB search for webhook payload")
                    try:
                        search_results = app.providers.tmdb.search(
                            MediaTypes.TV.value, series_title, page=1,
                        )
                        if search_results and search_results.get("results"):
                            top_result = search_results["results"][0]
                            media_id = top_result.get("media_id")
                            if media_id:
                                tv_metadata = app.providers.tmdb.tv_with_seasons(
                                    media_id, [season_number],
                                )
                                logger.info("Recovered TMDB lookup using title search")
                    except Exception as search_exc:
                        logger.warning(
                            "Title-based search failed: %s",
                            exception_summary(search_exc),
                        )

        if not tv_metadata:
            logger.warning("All TMDB lookup attempts failed for webhook show payload")
            return

        if self._should_recover_tv_show_from_external_ids(
            payload,
            ids,
            media_id,
            tv_metadata,
        ):
            alt_ids = dict(ids)
            alt_ids["tmdb_id"] = None
            fallback_media_id, alt_season, alt_episode = self._find_tv_media_id(
                alt_ids,
            )
            if fallback_media_id:
                media_id = fallback_media_id
                season_number = season_number or alt_season
                episode_number = episode_number or alt_episode
                self._remember_tvdb_override(media_id, ids)
                try:
                    tv_metadata = app.providers.tmdb.tv_with_seasons(
                        media_id,
                        [season_number],
                    )
                    logger.info(
                        "Recovered TMDB lookup after suspicious raw TMDB match: TMDB show %s",
                        media_id,
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "Recovery tmdb.tv_with_seasons failed for show %s: %s",
                        media_id,
                        exc,
                    )
                    return
        elif (
            ids.get("tvdb_id")
            and not self._extract_payload_tmdb_id(payload)
            and str(tv_metadata.get("tvdb_id") or "") != str(ids["tvdb_id"])
        ):
            self._remember_tvdb_override(media_id, ids)
            try:
                tv_metadata = app.providers.tmdb.tv_with_seasons(
                    media_id,
                    [season_number],
                )
                logger.info(
                    "Rebuilt TMDB TV metadata using preferred TVDB ID for show %s",
                    media_id,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Preferred TVDB TMDB lookup refresh failed for show %s: %s",
                    media_id,
                    exc,
                )

        tvdb_id = tv_metadata.get("tvdb_id") if tv_metadata else None

        if user.anime_enabled:
            link_sources = [
                ("stored TMDB", *self._get_mal_id_from_provider_links(
                    Sources.TMDB.value,
                    media_id,
                    season_number,
                    episode_number,
                )),
                ("stored TVDB", *self._get_mal_id_from_provider_links(
                    Sources.TVDB.value,
                    tvdb_id,
                    season_number,
                    episode_number,
                )),
            ]
            for mapping_source, mal_id, mapped_episode in link_sources:
                if not mal_id:
                    continue
                logger.info(
                    "Detected anime episode via %s mapping: MAL ID %s, Episode: %d",
                    mapping_source,
                    mal_id,
                    mapped_episode,
                )
                if self._handle_anime(mal_id, mapped_episode, payload, user):
                    return

            mapping_data = anime_mappings.fetch_mapping_data()
            mapping_sources = [
                ("TVDB", *anime_mappings.get_mal_id_from_tvdb(
                    mapping_data,
                    tvdb_id,
                    season_number,
                    episode_number,
                )),
            ]
            for mapping_source, mal_id, mapped_episode in mapping_sources:
                if not mal_id:
                    continue
                logger.info(
                    "Detected anime episode via %s mapping: MAL ID %s, Episode: %d",
                    mapping_source,
                    mal_id,
                    mapped_episode,
                )
                if self._handle_anime(mal_id, mapped_episode, payload, user):
                    return

            if self._try_route_tvdb_anime(
                payload,
                user,
                media_id,
                episode_number,
                tv_metadata,
                tvdb_id,
            ):
                return

        logger.info(
            "Detected TV episode via TMDB ID: %s, Season: %d, Episode: %d",
            media_id,
            season_number,
            episode_number,
        )
        self._handle_tv_episode(media_id, season_number, episode_number, payload, user)

    def _has_existing_tv_tracking(self, media_id, tvdb_id=None):
        """Return whether the TMDB/TVDB show is already tracked locally."""
        media_id = str(media_id)

        if app.models.Item.objects.filter(
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            media_id=media_id,
        ).exists():
            return True

        if app.models.ItemProviderLink.objects.filter(
            provider=Sources.TMDB.value,
            provider_media_type=MediaTypes.TV.value,
            provider_media_id=media_id,
        ).exists():
            return True

        if tvdb_id not in (None, "") and app.models.ItemProviderLink.objects.filter(
            provider=Sources.TVDB.value,
            provider_media_type=MediaTypes.TV.value,
            provider_media_id=str(tvdb_id),
        ).exists():
            return True

        return False

    def _try_route_tvdb_anime(
        self,
        payload,
        user,
        media_id,
        episode_number,
        tv_metadata,
        tvdb_id,
    ):
        """Probe TVDB for Anime before falling back to a TV track."""
        if not user.anime_enabled or not app.providers.tvdb.enabled():
            return False

        if self._has_existing_tv_tracking(media_id, tvdb_id):
            return False

        resolved_tvdb_id = tvdb_id or app.providers.tmdb.resolve_tvdb_id_for_tmdb_show(
            media_id,
            tv_metadata,
        )
        if not resolved_tvdb_id:
            return False

        if self._has_existing_tv_tracking(media_id, resolved_tvdb_id):
            return False

        try:
            tvdb_metadata = app.providers.tvdb.tv(resolved_tvdb_id)
        except Exception as exc:  # pragma: no cover - defensive network guard
            logger.warning(
                "Failed TVDB anime probe for show %s via TVDB ID %s: %s",
                media_id,
                resolved_tvdb_id,
                exception_summary(exc),
            )
            return False

        if not app.providers.tvdb.series_has_anime_genre(
            resolved_tvdb_id,
            tv_data=tvdb_metadata,
        ):
            return False

        mal_id = (tvdb_metadata.get("provider_external_ids") or {}).get("mal_id")
        if not mal_id:
            logger.info(
                "TVDB anime probe matched show %s but no MAL ID was available",
                media_id,
            )
            return False

        logger.info(
            "Detected anime episode via TVDB genre probe: TVDB ID %s, MAL ID %s, Episode: %s",
            resolved_tvdb_id,
            mal_id,
            episode_number,
        )
        return self._handle_anime(mal_id, episode_number, payload, user)

    def _normalize_series_title(self, title):
        """Normalize series titles for loose webhook-vs-TMDB comparisons."""
        if not title:
            return None
        title_str = str(title)[:500]
        return re.sub(r"\s*\(\d{4}\)$", "", title_str).strip().casefold()

    def _remember_tvdb_override(self, media_id, ids):
        """Persist a preferred TVDB ID for a resolved TMDB show when available."""
        tvdb_id = ids.get("tvdb_id")
        if not media_id or not tvdb_id:
            return

        app.providers.tmdb.set_tvdb_id_override(media_id, tvdb_id)

    def _extract_payload_tmdb_id(self, payload):
        """Extract a raw TMDB ID directly from provider payload GUID fields."""
        metadata = payload.get("Metadata", {}) or {}
        guids = metadata.get("Guid", [])
        if not guids:
            single_guid = metadata.get("guid")
            if single_guid:
                guids = [{"id": single_guid}]

        for guid in guids:
            guid_value = guid.get("id") if isinstance(guid, dict) else guid
            if not guid_value:
                continue

            guid_lower = str(guid_value).lower()
            if "tmdb" not in guid_lower and "themoviedb" not in guid_lower:
                continue

            cleaned = str(guid_value).split("?", 1)[0]
            if "://" in cleaned:
                cleaned = cleaned.split("://", 1)[1]
            cleaned = cleaned.lstrip("/")
            if "/" in cleaned:
                cleaned = cleaned.split("/", 1)[0]

            match = re.search(r"\d+", cleaned)
            if match:
                return match.group(0)

        return None

    def _should_recover_tv_show_from_external_ids(
        self,
        payload,
        ids,
        media_id,
        tv_metadata,
    ):
        """Detect when a raw Plex TMDB GUID appears to map to the wrong show."""
        if not tv_metadata:
            return False

        raw_tmdb_id = self._extract_payload_tmdb_id(payload)
        if not raw_tmdb_id or str(media_id) != str(raw_tmdb_id):
            return False

        if not (ids.get("tvdb_id") or ids.get("imdb_id")):
            return False

        expected_tvdb_id = ids.get("tvdb_id")
        actual_tvdb_id = tv_metadata.get("tvdb_id")
        if (
            expected_tvdb_id
            and actual_tvdb_id
            and str(expected_tvdb_id) != str(actual_tvdb_id)
        ):
            logger.info(
                "TV metadata mismatch for raw TMDB ID %s: expected TVDB %s, got %s",
                media_id,
                expected_tvdb_id,
                actual_tvdb_id,
            )
            return True

        expected_title = self._normalize_series_title(
            self._extract_series_title(payload),
        )
        actual_title = self._normalize_series_title(tv_metadata.get("title"))
        if expected_title and actual_title and expected_title != actual_title:
            logger.info(
                "TV metadata mismatch for raw TMDB ID %s: expected title '%s', got '%s'",
                media_id,
                expected_title,
                actual_title,
            )
            return True

        return False

    def _process_movie(self, payload, user, ids):
        tmdb_id = ids["tmdb_id"]
        imdb_id = ids["imdb_id"]
        find_response = None

        # Try to detect anime first if user has anime enabled
        if user.anime_enabled:
            mapping_data = anime_mappings.fetch_mapping_data()
            mal_id = None
            source = None
            resolved_tmdb_id = tmdb_id

            if tmdb_id:
                mal_id = anime_mappings.get_mal_id_from_tmdb_movie(mapping_data, tmdb_id)
                source = "TMDB"

            if not mal_id and imdb_id:
                mal_id = anime_mappings.get_mal_id_from_imdb(mapping_data, imdb_id)
                source = "IMDB"

            if not mal_id and imdb_id and not resolved_tmdb_id:
                try:
                    find_response = app.providers.tmdb.find(imdb_id, "imdb_id")
                except Exception as exc:  # pragma: no cover - defensive network guard
                    logger.warning(
                        "Failed TMDB lookup for movie IMDB ID %s: %s",
                        imdb_id,
                        exception_summary(exc),
                    )
                else:
                    movie_results = find_response.get("movie_results") or []
                    if movie_results:
                        resolved_tmdb_id = str(movie_results[0].get("id") or "")
                        if resolved_tmdb_id:
                            mal_id = anime_mappings.get_mal_id_from_tmdb_movie(
                                mapping_data,
                                resolved_tmdb_id,
                            )
                            source = "IMDB->TMDB"
                            tmdb_id = resolved_tmdb_id

            if mal_id:
                logger.info(
                    "Detected anime movie with MAL ID: %s (via %s)",
                    mal_id,
                    source,
                )
                if self._handle_anime(mal_id, 1, payload, user):
                    return

        # Handle as regular movie
        if tmdb_id:
            logger.info("Detected movie via TMDB ID: %s", tmdb_id)
            self._handle_movie(tmdb_id, payload, user)
        elif imdb_id:
            logger.debug("No TMDB ID found, looking up via IMDB ID: %s", imdb_id)
            try:
                response = find_response or app.providers.tmdb.find(imdb_id, "imdb_id")
            except Exception as exc:  # pragma: no cover - defensive network guard
                logger.warning(
                    "Failed IMDB->TMDB lookup for movie %s: %s",
                    imdb_id,
                    exception_summary(exc),
                )
                return

            if response.get("movie_results"):
                media_id = response["movie_results"][0]["id"]
                logger.info("Found matching TMDB ID: %s", media_id)
                self._handle_movie(media_id, payload, user)
            else:
                logger.warning(
                    "No matching TMDB ID found for IMDB ID: %s",
                    imdb_id,
                )
        else:
            logger.warning("No TMDB or IMDB ID found for movie, skipping processing")

    def _find_tv_media_id(
        self,
        ids,
        series_title=None,
        allow_title_fallback=False,
        year=None,
    ):
        """Find TV media ID from external IDs, with optional title search fallback.

        Args:
            ids: Dict of external IDs (tmdb_id, tvdb_id, imdb_id, anidb_id).
            series_title: Show title used for title-search fallback.
            allow_title_fallback: Enable title-search when all ID lookups fail.
            year: First-air year used to disambiguate title-search results.

        Returns:
            tuple: (media_id, season_number, episode_number)
        """
        # Prioritize TVDB/IMDB — TMDB find API resolves episode-level IDs to show IDs
        for ext_id, ext_type in [
            (ids["tvdb_id"], "tvdb_id"),
            (ids["imdb_id"], "imdb_id"),
        ]:
            if ext_id:
                response = app.providers.tmdb.find(ext_id, ext_type)
                if response.get("tv_episode_results"):
                    result = response["tv_episode_results"][0]
                    return (
                        result.get("show_id"),
                        result.get("season_number"),
                        result.get("episode_number"),
                    )
                if response.get("tv_results"):
                    result = response["tv_results"][0]
                    return result.get("id"), None, None

        # Direct TMDB ID fallback (may be episode-level; _process_tv handles that case)
        if ids["tmdb_id"]:
            try:
                return str(ids["tmdb_id"]), None, None
            except (ValueError, TypeError):
                logger.debug("Invalid TMDB ID format: %s", ids["tmdb_id"])

        if not allow_title_fallback or not series_title:
            return None, None, None

        # Title search fallback when all ID-based resolution fails
        logger.debug("TV ID missing; attempting title fallback search for: %s", series_title)
        try:
            search_results = app.providers.tmdb.search(
                MediaTypes.TV.value, series_title, page=1,
            )
            results = (search_results or {}).get("results") or []
            found_id = self._pick_title_search_result(results, year)
            if found_id:
                logger.info("Resolved TV entry via title search")
                return str(found_id), None, None

            # Retry with year stripped from titles like "Show (YYYY)"
            clean_title = re.sub(r"\s*\(\d{4}\)$", "", series_title[:500])
            if clean_title != series_title:
                search_results = app.providers.tmdb.search(
                    MediaTypes.TV.value, clean_title, page=1,
                )
                results = (search_results or {}).get("results") or []
                found_id = self._pick_title_search_result(results, year)
                if found_id:
                    logger.info("Resolved TV entry via normalized title search")
                    return str(found_id), None, None
        except Exception as exc:
            logger.warning(
                "Title search failed during TV resolution: %s", exception_summary(exc),
            )

        return None, None, None

    def _pick_title_search_result(self, results, year):
        """Pick the search result matching the show year, if one was given."""
        if not results:
            return None
        if year is not None:
            for result in results:
                result_year = result.get("year")
                if result_year and str(result_year) == str(year):
                    return result.get("media_id")
            return None
        return results[0].get("media_id")

    def _get_mal_id_from_provider_links(
        self,
        provider,
        provider_media_id,
        season_number,
        episode_number,
    ):
        """Prefer explicit season-aware anime links before global mapping data."""
        if (
            provider_media_id in (None, "")
            or season_number is None
            or episode_number is None
        ):
            return None, None

        exact_link = (
            app.models.ItemProviderLink.objects.filter(
                provider=provider,
                provider_media_type=MediaTypes.TV.value,
                provider_media_id=str(provider_media_id),
                season_number=season_number,
                item__source=Sources.MAL.value,
                item__media_type=MediaTypes.ANIME.value,
            )
            .select_related("item")
            .first()
        )
        if exact_link is None:
            exact_link = (
                app.models.ItemProviderLink.objects.filter(
                    provider=provider,
                    provider_media_type=MediaTypes.TV.value,
                    provider_media_id=str(provider_media_id),
                    season_number__isnull=True,
                    item__source=Sources.MAL.value,
                    item__media_type=MediaTypes.ANIME.value,
                )
                .select_related("item")
                .first()
            )

        if exact_link is None:
            return None, None

        mapped_episode = episode_number - int(exact_link.episode_offset or 0)
        if mapped_episode <= 0:
            return None, None

        return str(exact_link.item.media_id), mapped_episode

    def _handle_movie(self, media_id, payload, user):
        """Handle movie playback event."""
        from app.services import metadata_resolution  # noqa: PLC0415

        movie_metadata = app.providers.tmdb.movie(media_id)
        movie_item, _ = app.models.Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            defaults={
                "title": movie_metadata["title"],
                "image": movie_metadata["image"],
            },
        )
        movie_external_ids = self._extract_external_ids(payload)
        metadata_resolution.upsert_provider_links(
            movie_item,
            movie_metadata
            | {
                "provider_external_ids": {
                    **(movie_metadata.get("provider_external_ids") or {}),
                    "tmdb_id": str(media_id),
                    "imdb_id": movie_external_ids.get("imdb_id"),
                    "tvdb_id": movie_external_ids.get("tvdb_id"),
                },
            },
            provider=Sources.TMDB.value,
            provider_media_type=MediaTypes.MOVIE.value,
        )

        movie_instances = app.models.Movie.objects.filter(item=movie_item, user=user)
        current_instance = movie_instances.first()
        movie_played = self._is_played(payload)

        progress = 1 if movie_played else 0
        now = self._get_played_at(payload) or timezone.now().replace(
            second=0, microsecond=0,
        )

        if current_instance and current_instance.status != Status.COMPLETED.value:
            current_instance.progress = progress

            if movie_played:
                current_instance.end_date = now
                current_instance.status = Status.COMPLETED.value

            elif current_instance.status != Status.IN_PROGRESS.value:
                current_instance.start_date = now
                current_instance.status = Status.IN_PROGRESS.value

            if current_instance.tracker.changed():
                current_instance.save()
                logger.info(
                    "Updated existing movie instance to status: %s",
                    current_instance.status,
                )
            else:
                logger.debug(
                    "No changes detected for existing movie instance: %s",
                    current_instance.item,
                )
        else:
            app.models.Movie.objects.create(
                item=movie_item,
                user=user,
                progress=progress,
                status=Status.COMPLETED.value
                if movie_played
                else Status.IN_PROGRESS.value,
                start_date=now if not movie_played else None,
                end_date=now if movie_played else None,
            )
            logger.info(
                "Created new movie instance with status: %s",
                Status.COMPLETED.value if movie_played else Status.IN_PROGRESS.value,
            )

        # Queue collection metadata update if supported
        self._queue_collection_metadata_update(payload, user, movie_item)

    def _queue_collection_metadata_update_for_tv(self, payload, user, tv_item):
        """Queue collection metadata update for TV show (not episode-specific)."""
        self._queue_collection_metadata_update(payload, user, tv_item)

    def _build_fallback_episode_metadata(self, payload, episode_number, tv_metadata):
        """Build minimal episode metadata from payload when TMDB season data is missing."""
        metadata = payload.get("Metadata", {}) or {}

        duration_ms = metadata.get("duration") or metadata.get("Duration")
        runtime = None
        try:
            runtime_minutes = int(duration_ms) // 60000 if duration_ms else None
            runtime = runtime_minutes if runtime_minutes and runtime_minutes > 0 else None
        except (TypeError, ValueError):
            runtime = None

        air_date = (
            metadata.get("originallyAvailableAt")
            or metadata.get("originally_available_at")
        )

        return {
            "episode_number": int(episode_number),
            "runtime": runtime,
            "air_date": air_date,
            "still_path": None,
            "image": tv_metadata.get("image"),
            "name": metadata.get("title") or f"Episode {episode_number}",
            "overview": metadata.get("summary") or "",
        }

    def _build_fallback_season_metadata(
        self,
        payload,
        season_number,
        episode_number,
        tv_metadata,
    ):
        """Build minimal season metadata for missing TMDB seasons."""
        metadata = payload.get("Metadata", {}) or {}
        try:
            fallback_episode = self._build_fallback_episode_metadata(
                payload,
                episode_number,
                tv_metadata,
            )
        except (TypeError, ValueError):
            return None

        return {
            "season_number": int(season_number),
            "season_title": (
                "Specials"
                if int(season_number) == 0
                else metadata.get("parentTitle") or f"Season {season_number}"
            ),
            "synopsis": tv_metadata.get("synopsis") or "No synopsis available.",
            "image": tv_metadata.get("image"),
            "max_progress": int(episode_number),
            "episodes": [fallback_episode],
            "details": {
                "episodes": int(episode_number),
            },
            "providers": {},
            "source_url": tv_metadata.get("external_links", {}).get("TVDB"),
        }

    def _load_tv_metadata_with_required_season(
        self,
        media_id,
        season_number,
        *,
        reason,
    ):
        """Return TMDB show metadata only when the requested season exists."""
        try:
            tv_metadata = app.providers.tmdb.tv_with_seasons(media_id, [season_number])
        except Exception as exc:  # pragma: no cover - defensive network guard
            logger.warning(
                "TV season recovery failed for candidate %s via %s: %s",
                media_id,
                reason,
                exception_summary(exc),
            )
            return None

        if f"season/{season_number}" not in tv_metadata:
            logger.info(
                "TV season recovery candidate %s via %s still missing season %s",
                media_id,
                reason,
                season_number,
            )
            return None

        return tv_metadata

    def _recover_tv_metadata_for_missing_season(
        self,
        media_id,
        season_number,
        payload,
        ids,
    ):
        """Try one bounded recovery pass when the resolved show lacks the season."""
        if season_number in (None, 0):
            return None, None

        seen_media_ids = {str(media_id)}
        alt_ids = dict(ids)
        alt_ids["tmdb_id"] = None
        recovered_media_id, _alt_season, _alt_episode = self._find_tv_media_id(alt_ids)
        if recovered_media_id and str(recovered_media_id) not in seen_media_ids:
            recovered_tv_metadata = self._load_tv_metadata_with_required_season(
                recovered_media_id,
                season_number,
                reason="external_ids",
            )
            if recovered_tv_metadata is not None:
                return str(recovered_media_id), recovered_tv_metadata
            seen_media_ids.add(str(recovered_media_id))

        series_title = self._extract_series_title(payload)
        if not series_title:
            return None, None

        try:
            search_results = app.providers.tmdb.search(
                MediaTypes.TV.value,
                series_title,
                page=1,
            )
        except Exception as exc:  # pragma: no cover - defensive network guard
            logger.warning(
                "TV title-search recovery failed for '%s': %s",
                series_title,
                exception_summary(exc),
            )
            return None, None

        for result in (search_results.get("results") or [])[:3]:
            candidate_media_id = result.get("media_id")
            if not candidate_media_id or str(candidate_media_id) in seen_media_ids:
                continue
            recovered_tv_metadata = self._load_tv_metadata_with_required_season(
                candidate_media_id,
                season_number,
                reason=f"title_search:{series_title}",
            )
            if recovered_tv_metadata is not None:
                return str(candidate_media_id), recovered_tv_metadata
            seen_media_ids.add(str(candidate_media_id))

        return None, None

    def _handle_tv_episode(
        self,
        media_id,
        season_number,
        episode_number,
        payload,
        user,
    ):
        """Handle TV episode playback event."""
        from app.services import metadata_resolution  # noqa: PLC0415

        tv_metadata = app.providers.tmdb.tv_with_seasons(media_id, [season_number])
        external_ids = self._extract_external_ids(payload)

        season_key = f"season/{season_number}"
        season_metadata = tv_metadata.get(season_key)
        used_local_only_fallback = False
        if not season_metadata and int(season_number) != 0:
            (
                recovered_media_id,
                recovered_tv_metadata,
            ) = self._recover_tv_metadata_for_missing_season(
                media_id,
                season_number,
                payload,
                external_ids,
            )
            if recovered_media_id and recovered_tv_metadata:
                media_id = recovered_media_id
                tv_metadata = recovered_tv_metadata
                self._remember_tvdb_override(media_id, external_ids)
                season_metadata = tv_metadata.get(season_key)
                logger.info(
                    "Recovered missing season %s using TMDB show %s",
                    season_number,
                    media_id,
                )

        if not season_metadata:
            logger.warning(
                "Season %s metadata missing for TMDB ID %s; using payload fallback",
                season_number,
                media_id,
            )
            season_metadata = self._build_fallback_season_metadata(
                payload,
                season_number,
                episode_number,
                tv_metadata,
            )
            if season_metadata and int(season_number) == 0:
                cached_fallback = app.providers.tmdb.cache_fallback_season_metadata(
                    media_id,
                    season_number,
                    tv_metadata,
                    season_metadata,
                )
                if cached_fallback:
                    season_metadata = cached_fallback
            elif season_metadata:
                used_local_only_fallback = True

        if not season_metadata:
            logger.warning(
                "Failed to build fallback season metadata for TMDB ID %s season %s",
                media_id,
                season_number,
            )
            return

        tv_item, _ = app.models.Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            defaults={
                "title": tv_metadata["title"],
                "image": tv_metadata["image"],
            },
        )
        metadata_resolution.upsert_provider_links(
            tv_item,
            tv_metadata
            | {
                "provider_external_ids": {
                    **(tv_metadata.get("provider_external_ids") or {}),
                    "tmdb_id": str(media_id),
                    "tvdb_id": (
                        external_ids.get("tvdb_id")
                        or tv_metadata.get("tvdb_id")
                    ),
                    "imdb_id": external_ids.get("imdb_id"),
                },
            },
            provider=Sources.TMDB.value,
            provider_media_type=MediaTypes.TV.value,
        )

        tv_instance, tv_created = app.models.TV.objects.get_or_create(
            item=tv_item,
            user=user,
            defaults={"status": Status.IN_PROGRESS.value},
        )

        if tv_created:
            logger.info("Created new TV instance: %s", tv_metadata["title"])
        elif tv_instance.status != Status.IN_PROGRESS.value:
            tv_instance.status = Status.IN_PROGRESS.value
            tv_instance.save()
            logger.info(
                "Updated TV instance status to %s: %s",
                Status.IN_PROGRESS.value,
                tv_metadata["title"],
            )

        # Use season poster if available, otherwise fallback to TV show poster
        season_image = season_metadata.get("image") or tv_metadata.get("image")

        # If the user is already tracking this show via the anime pathway (TMDB-based
        # anime, separate from MAL anime), keep scrobbles in that same bucket so that
        # anime-scoped Season Items stay separate from TV-scoped ones.  This also
        # prevents MultipleObjectsReturned when both anime and TV Season Items exist.
        uses_anime_tracking = app.models.Item.objects.filter(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            library_media_type=MediaTypes.ANIME.value,
        ).exists()
        # Use the post-save normalised value ('season') not '' so the lookup hits
        # existing Season Items that were created via Item.save() normalisation.
        season_library_media_type = (
            MediaTypes.ANIME.value
            if uses_anime_tracking
            else MediaTypes.SEASON.value
        )

        season_item, _ = app.models.Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            library_media_type=season_library_media_type,
            defaults={
                "title": tv_metadata["title"],
                "image": season_image,
                "provider_metadata_status": (
                    ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
                    if used_local_only_fallback
                    else ""
                ),
            },
        )
        desired_provider_metadata_status = (
            ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
            if used_local_only_fallback
            else ""
        )
        if season_item.provider_metadata_status != desired_provider_metadata_status:
            season_item.provider_metadata_status = desired_provider_metadata_status
            season_item.save(update_fields=["provider_metadata_status"])
        metadata_resolution.upsert_provider_links(
            season_item,
            season_metadata
            | {
                "provider_external_ids": {
                    **(season_metadata.get("provider_external_ids") or {}),
                    "tmdb_id": str(media_id),
                    "tvdb_id": (
                        external_ids.get("tvdb_id")
                        or tv_metadata.get("tvdb_id")
                    ),
                    "imdb_id": external_ids.get("imdb_id"),
                },
            },
            provider=Sources.TMDB.value,
            provider_media_type=MediaTypes.SEASON.value,
            season_number=season_number,
        )

        season_instance, season_created = app.models.Season.objects.get_or_create(
            item=season_item,
            user=user,
            related_tv=tv_instance,
            defaults={"status": Status.IN_PROGRESS.value},
        )

        if season_created:
            logger.info(
                "Created new season instance: %s S%02d",
                tv_metadata["title"],
                season_number,
            )
        elif season_instance.status != Status.IN_PROGRESS.value:
            season_instance.status = Status.IN_PROGRESS.value
            season_instance.save()
            logger.info(
                "Updated season instance status to %s: %s S%02d",
                Status.IN_PROGRESS.value,
                tv_metadata["title"],
                season_number,
            )

        episode_item = season_instance.get_episode_item(episode_number, season_metadata)

        if self._is_played(payload):
            now = self._get_played_at(payload) or timezone.now().replace(
                second=0, microsecond=0,
            )
            latest_episode = (
                app.models.Episode.objects.filter(
                    item=episode_item,
                    related_season=season_instance,
                )
                .order_by("-end_date")
                .first()
            )

            should_create = True
            # check for duplicate episode records,
            # sometimes webhooks are triggered multiple times #689
            if latest_episode and latest_episode.end_date:
                time_diff = abs((now - latest_episode.end_date).total_seconds())
                threshold = 5
                if time_diff < threshold:
                    should_create = False
                    logger.debug(
                        "Skipping duplicate episode record "
                        "(time difference: %d seconds): %s S%02dE%02d",
                        time_diff,
                        tv_metadata["title"],
                        season_number,
                        episode_number,
                    )

            if should_create:
                app.models.Episode.objects.create(
                    item=episode_item,
                    related_season=season_instance,
                    end_date=now,
                )
                logger.info(
                    "Marked episode as played: %s S%02dE%02d",
                    tv_metadata["title"],
                    season_number,
                    episode_number,
                )
        else:
            logger.debug(
                "Episode not marked as played: %s S%02dE%02d",
                tv_metadata["title"],
                season_number,
                episode_number,
            )

        # Queue collection metadata update for TV show (not episode-specific)
        self._queue_collection_metadata_update_for_tv(payload, user, tv_item)

    def _handle_anime(self, media_id, episode_number, payload, user):
        """Handle anime playback event."""
        from app.services import metadata_resolution  # noqa: PLC0415

        anime_metadata = app.providers.mal.anime(media_id)
        if not self._is_played(payload):
            episode_number = max(0, episode_number - 1)

        max_progress = anime_metadata.get("max_progress")
        if (
            isinstance(max_progress, int)
            and max_progress > 0
            and episode_number > max_progress
        ):
            logger.warning(
                "Skipping anime mapping for MAL ID %s: episode %s exceeds max_progress %s",
                media_id,
                episode_number,
                max_progress,
            )
            return False

        anime_item, _ = app.models.Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            defaults={
                "title": anime_metadata["title"],
                "image": anime_metadata["image"],
            },
        )
        metadata_resolution.upsert_provider_links(
            anime_item,
            anime_metadata | {"media_id": str(media_id)},
            provider=Sources.MAL.value,
            provider_media_type=MediaTypes.ANIME.value,
        )

        anibridge_data = anime_mappings.fetch_mapping_data()
        for mapping_entry in anime_mappings.find_entries_for_mal_id(anibridge_data, media_id):
            tmdb_id = mapping_entry.get("tmdb_id")
            tvdb_id = mapping_entry.get("tvdb_id")
            season_number = mapping_entry.get("season_number")
            episode_offset = mapping_entry.get("episode_offset") or 0

            if tmdb_id not in (None, ""):
                metadata_resolution.upsert_provider_links(
                    anime_item,
                    {
                        "media_id": str(tmdb_id),
                        "source": Sources.TMDB.value,
                        "media_type": MediaTypes.ANIME.value,
                        "identity_media_type": MediaTypes.TV.value,
                        "provider_external_ids": {"tmdb_id": str(tmdb_id)},
                    },
                    provider=Sources.TMDB.value,
                    provider_media_type=MediaTypes.TV.value,
                    season_number=season_number,
                    episode_offset=episode_offset,
                )

            if tvdb_id not in (None, ""):
                metadata_resolution.upsert_provider_links(
                    anime_item,
                    {
                        "media_id": str(tvdb_id),
                        "source": Sources.TVDB.value,
                        "media_type": MediaTypes.ANIME.value,
                        "identity_media_type": MediaTypes.TV.value,
                        "provider_external_ids": {"tvdb_id": str(tvdb_id)},
                    },
                    provider=Sources.TVDB.value,
                    provider_media_type=MediaTypes.TV.value,
                    season_number=season_number,
                    episode_offset=episode_offset,
                )

        anime_instances = app.models.Anime.objects.filter(item=anime_item, user=user)
        current_instance = anime_instances.first()

        now = timezone.now().replace(second=0, microsecond=0)
        is_completed = episode_number == anime_metadata["max_progress"]
        status = Status.COMPLETED.value if is_completed else Status.IN_PROGRESS.value

        if current_instance and current_instance.status != Status.COMPLETED.value:
            current_instance.progress = episode_number

            if is_completed:
                current_instance.end_date = now
                current_instance.status = status

            elif current_instance.status != Status.IN_PROGRESS.value:
                current_instance.start_date = now
                current_instance.status = status

            if current_instance.tracker.changed():
                current_instance.save()
                logger.info(
                    "Updated existing anime instance to status: %s with progress %d",
                    current_instance.status,
                    episode_number,
                )
            else:
                logger.debug(
                    "No changes detected for existing anime instance: %s",
                    current_instance.item,
                )
        else:
            app.models.Anime.objects.create(
                item=anime_item,
                user=user,
                progress=episode_number,
                status=status,
                start_date=now if not is_completed else None,
                end_date=now if is_completed else None,
            )
            logger.info(
                "Created new anime instance with status: %s and progress %d",
                status,
                episode_number,
            )
        return True

    def _queue_collection_metadata_update(self, payload, user, item):
        """Queue collection metadata update task if media server info is available.
        
        This is a no-op by default. Subclasses should override to implement
        collection metadata extraction for their specific media server.
        """
        pass
