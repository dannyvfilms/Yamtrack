"""DB-backed cache repository for Discover."""

from __future__ import annotations

import json
import random
import time
from datetime import timedelta
from typing import Any

from django.db import OperationalError
from django.utils import timezone

from app.log_safety import stable_hmac
from app.models import DiscoverApiCache, DiscoverRowCache, DiscoverTasteProfile


def _params_hash(params: dict[str, Any] | None) -> str:
    payload = json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)
    return stable_hmac(payload, namespace="discover_api_cache")


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
    keyword_affinity: dict[str, float],
    recent_keyword_affinity: dict[str, float],
    phase_keyword_affinity: dict[str, float],
    studio_affinity: dict[str, float],
    recent_studio_affinity: dict[str, float],
    phase_studio_affinity: dict[str, float],
    collection_affinity: dict[str, float],
    recent_collection_affinity: dict[str, float],
    phase_collection_affinity: dict[str, float],
    director_affinity: dict[str, float],
    recent_director_affinity: dict[str, float],
    phase_director_affinity: dict[str, float],
    lead_cast_affinity: dict[str, float],
    recent_lead_cast_affinity: dict[str, float],
    phase_lead_cast_affinity: dict[str, float],
    certification_affinity: dict[str, float],
    recent_certification_affinity: dict[str, float],
    phase_certification_affinity: dict[str, float],
    runtime_bucket_affinity: dict[str, float],
    recent_runtime_bucket_affinity: dict[str, float],
    phase_runtime_bucket_affinity: dict[str, float],
    decade_affinity: dict[str, float],
    recent_decade_affinity: dict[str, float],
    phase_decade_affinity: dict[str, float],
    comfort_library_affinity: dict[str, dict[str, float]],
    comfort_rewatch_affinity: dict[str, dict[str, float]],
    person_affinity: dict[str, float],
    negative_genre_affinity: dict[str, float],
    negative_tag_affinity: dict[str, float],
    negative_person_affinity: dict[str, float],
    world_rating_profile: dict[str, float | int],
    activity_snapshot_at,
    ttl_seconds: int,
) -> DiscoverTasteProfile:
    """Persist taste profile to DB-backed profile cache."""
    now = timezone.now()
    defaults = {
        "genre_affinity": genre_affinity,
        "recent_genre_affinity": recent_genre_affinity,
        "phase_genre_affinity": phase_genre_affinity,
        "tag_affinity": tag_affinity,
        "recent_tag_affinity": recent_tag_affinity,
        "phase_tag_affinity": phase_tag_affinity,
        "keyword_affinity": keyword_affinity,
        "recent_keyword_affinity": recent_keyword_affinity,
        "phase_keyword_affinity": phase_keyword_affinity,
        "studio_affinity": studio_affinity,
        "recent_studio_affinity": recent_studio_affinity,
        "phase_studio_affinity": phase_studio_affinity,
        "collection_affinity": collection_affinity,
        "recent_collection_affinity": recent_collection_affinity,
        "phase_collection_affinity": phase_collection_affinity,
        "director_affinity": director_affinity,
        "recent_director_affinity": recent_director_affinity,
        "phase_director_affinity": phase_director_affinity,
        "lead_cast_affinity": lead_cast_affinity,
        "recent_lead_cast_affinity": recent_lead_cast_affinity,
        "phase_lead_cast_affinity": phase_lead_cast_affinity,
        "certification_affinity": certification_affinity,
        "recent_certification_affinity": recent_certification_affinity,
        "phase_certification_affinity": phase_certification_affinity,
        "runtime_bucket_affinity": runtime_bucket_affinity,
        "recent_runtime_bucket_affinity": recent_runtime_bucket_affinity,
        "phase_runtime_bucket_affinity": phase_runtime_bucket_affinity,
        "decade_affinity": decade_affinity,
        "recent_decade_affinity": recent_decade_affinity,
        "phase_decade_affinity": phase_decade_affinity,
        "comfort_library_affinity": comfort_library_affinity,
        "comfort_rewatch_affinity": comfort_rewatch_affinity,
        "person_affinity": person_affinity,
        "negative_genre_affinity": negative_genre_affinity,
        "negative_tag_affinity": negative_tag_affinity,
        "negative_person_affinity": negative_person_affinity,
        "world_rating_profile": world_rating_profile,
        "activity_snapshot_at": activity_snapshot_at,
        "computed_at": now,
        "expires_at": now + timedelta(seconds=ttl_seconds),
    }
    # SQLite only allows one writer at a time. Concurrent Celery workers can
    # race on this upsert and receive OperationalError("database is locked")
    # even with busy_timeout set, because select_for_update() inside
    # update_or_create() can trigger SQLITE_LOCKED (same-process lock) rather
    # than SQLITE_BUSY, which the timeout doesn't retry.  Back off and retry.
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            entry, _ = DiscoverTasteProfile.objects.update_or_create(
                user_id=user_id,
                media_type=media_type,
                defaults=defaults,
            )
            return entry
        except OperationalError as exc:
            if "database is locked" not in str(exc) or attempt >= max_attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1) + random.random() * 0.3)
    raise RuntimeError("unreachable")


def delete_row_caches(user_ids: list[int], media_types: list[str]) -> int:
    """Delete cached row payloads for the selected user/media pairs."""
    if not user_ids or not media_types:
        return 0
    deleted, _details = DiscoverRowCache.objects.filter(
        user_id__in=user_ids,
        media_type__in=media_types,
    ).delete()
    return int(deleted)


def delete_taste_profiles(user_ids: list[int], media_types: list[str]) -> int:
    """Delete cached taste profiles for the selected user/media pairs."""
    if not user_ids or not media_types:
        return 0
    deleted, _details = DiscoverTasteProfile.objects.filter(
        user_id__in=user_ids,
        media_type__in=media_types,
    ).delete()
    return int(deleted)
