"""Trakt Discover adapter."""

from __future__ import annotations

import logging

from django.conf import settings

from app.discover import cache_repo
from app.discover.schemas import CandidateItem
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

TRAKT_BASE_URL = "https://api.trakt.tv"
TRAKT_API_PROVIDER = "TRAKT"
WATCHED_WEEKLY_TTL = 60 * 60
POPULAR_TTL = 60 * 60 * 24
ANTICIPATED_TTL = 60 * 60


class TraktDiscoverAdapter:
    """Trakt adapter used by Discover service."""

    provider = "trakt"

    def _cache_request(self, endpoint: str, params: dict, *, ttl_seconds: int) -> dict:
        payload, is_stale = cache_repo.get_api_cache(self.provider, endpoint, params)
        if payload and not is_stale:
            return payload

        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": settings.TRAKT_API,
        }

        try:
            response = services.api_request(
                TRAKT_API_PROVIDER,
                "GET",
                f"{TRAKT_BASE_URL}{endpoint}",
                params=params,
                headers=headers,
            )
            normalized_payload = {"results": response if isinstance(response, list) else []}
            cache_repo.set_api_cache(
                self.provider,
                endpoint,
                params,
                normalized_payload,
                ttl_seconds=ttl_seconds,
            )
            return normalized_payload
        except Exception as error:  # noqa: BLE001
            if payload:
                logger.warning(
                    "discover_trakt_cache_fallback endpoint=%s error=%s",
                    endpoint,
                    error,
                )
                return payload
            raise

    def movie_watched_weekly(self, *, limit: int = 100) -> list[CandidateItem]:
        """Return Trakt watched-weekly movies normalized to Discover candidates."""
        if limit <= 0:
            return []

        params = {
            "extended": "full",
            "page": 1,
            "limit": min(max(limit, 25), 100),
        }
        payload = self._cache_request(
            "/movies/watched/weekly",
            params,
            ttl_seconds=WATCHED_WEEKLY_TTL,
        )

        candidates: list[CandidateItem] = []
        for entry in payload.get("results", []):
            movie = entry.get("movie") or {}
            if not movie:
                continue

            ids = movie.get("ids") or {}
            tmdb_id = ids.get("tmdb")
            if not tmdb_id:
                continue

            title = (movie.get("title") or "").strip()
            if not title:
                continue

            popularity = entry.get("watcher_count")
            if popularity is None:
                popularity = entry.get("play_count")
            if popularity is None:
                popularity = entry.get("collected_count")

            candidates.append(
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source=Sources.TMDB.value,
                    media_id=str(tmdb_id),
                    title=title,
                    original_title=title,
                    localized_title=title,
                    image=settings.IMG_NONE,
                    release_date=movie.get("released"),
                    genres=[str(genre).strip() for genre in (movie.get("genres") or []) if str(genre).strip()],
                    popularity=float(popularity) if popularity is not None else None,
                    rating=float(movie["rating"]) if movie.get("rating") is not None else None,
                    rating_count=int(movie["votes"]) if movie.get("votes") is not None else None,
                    row_key="trending_right_now",
                    source_reason="Trakt watched weekly",
                ),
            )

        return candidates[:limit]

    def movie_popular(self, *, page: int = 1, limit: int = 100) -> list[CandidateItem]:
        """Return Trakt popular movies normalized to Discover candidates."""
        if page <= 0 or limit <= 0:
            return []

        page_limit = min(max(limit, 1), 100)
        params = {
            "extended": "full",
            "page": page,
            "limit": page_limit,
        }
        payload = self._cache_request(
            "/movies/popular",
            params,
            ttl_seconds=POPULAR_TTL,
        )

        candidates: list[CandidateItem] = []
        for index, movie in enumerate(payload.get("results", []), start=1):
            if not movie:
                continue

            ids = movie.get("ids") or {}
            tmdb_id = ids.get("tmdb")
            if not tmdb_id:
                continue

            title = (movie.get("title") or "").strip()
            if not title:
                continue

            # Keep provider order meaningful when popularity score is missing.
            popularity = movie.get("votes")
            if popularity is None:
                popularity = max(page_limit - index + 1, 1)

            candidates.append(
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source=Sources.TMDB.value,
                    media_id=str(tmdb_id),
                    title=title,
                    original_title=title,
                    localized_title=title,
                    image=settings.IMG_NONE,
                    release_date=movie.get("released"),
                    genres=[str(genre).strip() for genre in (movie.get("genres") or []) if str(genre).strip()],
                    popularity=float(popularity) if popularity is not None else None,
                    rating=float(movie["rating"]) if movie.get("rating") is not None else None,
                    rating_count=int(movie["votes"]) if movie.get("votes") is not None else None,
                    row_key="all_time_greats_unseen",
                    source_reason="Trakt popular",
                ),
            )

        return candidates[:page_limit]

    def movie_anticipated(self, *, page: int = 1, limit: int = 100) -> list[CandidateItem]:
        """Return Trakt anticipated movies normalized to Discover candidates."""
        if page <= 0 or limit <= 0:
            return []

        page_limit = min(max(limit, 1), 100)
        params = {
            "extended": "full",
            "page": page,
            "limit": page_limit,
        }
        payload = self._cache_request(
            "/movies/anticipated",
            params,
            ttl_seconds=ANTICIPATED_TTL,
        )

        candidates: list[CandidateItem] = []
        for index, entry in enumerate(payload.get("results", []), start=1):
            movie = entry.get("movie") if isinstance(entry, dict) else None
            if not isinstance(movie, dict):
                movie = entry if isinstance(entry, dict) else {}
            if not movie:
                continue

            ids = movie.get("ids") or {}
            tmdb_id = ids.get("tmdb")
            if not tmdb_id:
                continue

            title = (movie.get("title") or "").strip()
            if not title:
                continue

            popularity = entry.get("list_count") if isinstance(entry, dict) else None
            if popularity is None:
                popularity = movie.get("votes")
            if popularity is None:
                popularity = max(page_limit - index + 1, 1)

            candidates.append(
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source=Sources.TMDB.value,
                    media_id=str(tmdb_id),
                    title=title,
                    original_title=title,
                    localized_title=title,
                    image=settings.IMG_NONE,
                    release_date=movie.get("released"),
                    genres=[str(genre).strip() for genre in (movie.get("genres") or []) if str(genre).strip()],
                    popularity=float(popularity) if popularity is not None else None,
                    rating=float(movie["rating"]) if movie.get("rating") is not None else None,
                    rating_count=int(movie["votes"]) if movie.get("votes") is not None else None,
                    row_key="coming_soon",
                    source_reason="Trakt anticipated",
                ),
            )

        return candidates[:page_limit]

    def check_capability(self) -> dict[str, bool]:
        """Return endpoint-level availability booleans."""
        checks = {
            "movie_watched_weekly": False,
            "movie_popular": False,
            "movie_anticipated": False,
        }

        try:
            checks["movie_watched_weekly"] = bool(self.movie_watched_weekly(limit=1))
        except Exception:  # noqa: BLE001
            checks["movie_watched_weekly"] = False

        try:
            checks["movie_popular"] = bool(self.movie_popular(limit=1))
        except Exception:  # noqa: BLE001
            checks["movie_popular"] = False

        try:
            checks["movie_anticipated"] = bool(self.movie_anticipated(limit=1))
        except Exception:  # noqa: BLE001
            checks["movie_anticipated"] = False

        return checks
