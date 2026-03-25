"""Raw Trakt API helpers used for persisted popularity enrichment."""

from __future__ import annotations

from typing import Any

from django.conf import settings

from app.models import MediaTypes
from app.providers import services

TRAKT_BASE_URL = "https://api.trakt.tv"
TRAKT_API_PROVIDER = "TRAKT"
TRAKT_API_VERSION = "2"

TRAKT_SEARCH_TYPES = {
    MediaTypes.MOVIE.value: "movie",
    MediaTypes.TV.value: "show",
    MediaTypes.ANIME.value: "show",
}


def is_configured() -> bool:
    """Return whether Trakt API access is configured."""
    return bool(getattr(settings, "TRAKT_API", ""))


def _headers() -> dict[str, str]:
    """Return Trakt request headers."""
    return {
        "Content-Type": "application/json",
        "trakt-api-version": TRAKT_API_VERSION,
        "trakt-api-key": settings.TRAKT_API,
    }


def _normalize_ids(ids: dict[str, Any] | None) -> dict[str, str]:
    """Return a compact normalized Trakt ID payload."""
    normalized: dict[str, str] = {}
    for key, value in (ids or {}).items():
        if value in (None, ""):
            continue
        normalized[str(key)] = str(value)
    return normalized


def lookup_by_external_id(
    external_id_type: str,
    external_id: str | int,
    *,
    media_type: str,
) -> dict[str, Any] | None:
    """Return Trakt summary metadata for a single external-ID lookup."""
    trakt_media_type = TRAKT_SEARCH_TYPES.get(media_type)
    normalized_id = str(external_id or "").strip()
    if not trakt_media_type or not normalized_id or not is_configured():
        return None

    response = services.api_request(
        TRAKT_API_PROVIDER,
        "GET",
        f"{TRAKT_BASE_URL}/search/{external_id_type}/{normalized_id}",
        params={
            "type": trakt_media_type,
            "extended": "full",
        },
        headers=_headers(),
    )

    entries = response if isinstance(response, list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        payload = entry.get(trakt_media_type)
        if not isinstance(payload, dict):
            continue
        rating = payload.get("rating")
        votes = payload.get("votes")
        return {
            "rating": float(rating) if rating is not None else None,
            "votes": int(votes) if votes is not None else None,
            "trakt_ids": _normalize_ids(payload.get("ids")),
            "external_ids": _normalize_ids(payload.get("ids")),
            "matched_id_type": external_id_type,
            "title": payload.get("title"),
            "year": payload.get("year"),
        }

    return None
