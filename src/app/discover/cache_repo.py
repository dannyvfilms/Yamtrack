"""DB-backed cache repository for Discover."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from django.utils import timezone

from app.models import DiscoverApiCache, DiscoverRowCache, DiscoverTasteProfile


def _params_hash(params: dict[str, Any] | None) -> str:
    payload = json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_api_cache(provider: str, endpoint: str, params: dict[str, Any] | None):
    """Return cached API payload and staleness state."""
    params_hash = _params_hash(params)
    entry = DiscoverApiCache.objects.filter(
        provider=provider,
        endpoint=endpoint,
        params_hash=params_hash,
    ).first()
    if not entry:
        return None, False

    is_stale = bool(entry.expires_at and entry.expires_at <= timezone.now())
    return entry.payload, is_stale


def set_api_cache(
    provider: str,
    endpoint: str,
    params: dict[str, Any] | None,
    payload: dict[str, Any],
    *,
    ttl_seconds: int,
) -> None:
    """Persist API payload to DB-backed cache."""
    now = timezone.now()
    params_hash = _params_hash(params)
    DiscoverApiCache.objects.update_or_create(
        provider=provider,
        endpoint=endpoint,
        params_hash=params_hash,
        defaults={
            "payload": payload,
            "fetched_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
        },
    )


def get_row_cache(user_id: int, media_type: str, row_key: str):
    """Return cached row payload and staleness state."""
    entry = DiscoverRowCache.objects.filter(
        user_id=user_id,
        media_type=media_type,
        row_key=row_key,
    ).first()
    if not entry:
        return None, False

    is_stale = bool(entry.expires_at and entry.expires_at <= timezone.now())
    return entry.payload, is_stale


def set_row_cache(
    user_id: int,
    media_type: str,
    row_key: str,
    payload: dict[str, Any],
    *,
    ttl_seconds: int,
) -> None:
    """Persist row payload to DB-backed row cache."""
    now = timezone.now()
    DiscoverRowCache.objects.update_or_create(
        user_id=user_id,
        media_type=media_type,
        row_key=row_key,
        defaults={
            "payload": payload,
            "built_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
        },
    )


def get_taste_profile(user_id: int, media_type: str):
    """Return cached profile payload and staleness state."""
    entry = DiscoverTasteProfile.objects.filter(
        user_id=user_id,
        media_type=media_type,
    ).first()
    if not entry:
        return None, False

    is_stale = bool(entry.expires_at and entry.expires_at <= timezone.now())
    return entry, is_stale


def set_taste_profile(
    user_id: int,
    media_type: str,
    *,
    genre_affinity: dict[str, float],
    recent_genre_affinity: dict[str, float],
    phase_genre_affinity: dict[str, float],
    tag_affinity: dict[str, float],
    recent_tag_affinity: dict[str, float],
    phase_tag_affinity: dict[str, float],
    person_affinity: dict[str, float],
    activity_snapshot_at,
    ttl_seconds: int,
) -> DiscoverTasteProfile:
    """Persist taste profile to DB-backed profile cache."""
    now = timezone.now()
    entry, _ = DiscoverTasteProfile.objects.update_or_create(
        user_id=user_id,
        media_type=media_type,
        defaults={
            "genre_affinity": genre_affinity,
            "recent_genre_affinity": recent_genre_affinity,
            "phase_genre_affinity": phase_genre_affinity,
            "tag_affinity": tag_affinity,
            "recent_tag_affinity": recent_tag_affinity,
            "phase_tag_affinity": phase_tag_affinity,
            "person_affinity": person_affinity,
            "activity_snapshot_at": activity_snapshot_at,
            "computed_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
        },
    )
    return entry
