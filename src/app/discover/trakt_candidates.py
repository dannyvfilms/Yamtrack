"""Trakt/TMDB external candidate fetching for Discover rows."""

from __future__ import annotations

from django.db.models import Q

from app.discover import cache_repo
from app.discover.adapters import TMDB_ADAPTER, TRAKT_ADAPTER
from app.discover.filters import (
    exclude_tracked_items,
    get_feedback_keys_by_media_type,
    get_tracked_keys_by_media_type,
)
from app.discover.schemas import CandidateItem
from app.discover.scoring import score_candidates
from app.discover.service_helpers import MAX_ITEMS_PER_ROW, _model_for_media_type
from app.models import MediaTypes, Status

TRAKT_POPULAR_PAGE_SIZE = 100
TRAKT_POPULAR_PULL_STEP = 100
TRAKT_POPULAR_DEFAULT_PULL_TARGET = 100
TRAKT_POPULAR_MAX_PULL_TARGET = 2000
ADAPTIVE_PULL_TARGET_META_KEY = "adaptive_pull_target"
ROW_CACHE_SCHEMA_META_KEY = "schema_version"

MOVIE_CANON_ROW_SCHEMA_VERSION = 2
MOVIE_COMING_SOON_ROW_SCHEMA_VERSION = 1


def _clamp_adaptive_pull_target(value: int | None) -> int:
    if value is None:
        return TRAKT_POPULAR_DEFAULT_PULL_TARGET
    return max(TRAKT_POPULAR_PAGE_SIZE, min(int(value), TRAKT_POPULAR_MAX_PULL_TARGET))


def _get_cached_adaptive_pull_target(user_id: int, media_type: str, row_key: str) -> int:
    cached_payload, _is_stale = cache_repo.get_row_cache(user_id, media_type, row_key)
    if not cached_payload:
        return TRAKT_POPULAR_DEFAULT_PULL_TARGET

    meta = cached_payload.get("meta")
    if not isinstance(meta, dict):
        return TRAKT_POPULAR_DEFAULT_PULL_TARGET

    try:
        return _clamp_adaptive_pull_target(int(meta.get(ADAPTIVE_PULL_TARGET_META_KEY)))
    except (TypeError, ValueError):
        return TRAKT_POPULAR_DEFAULT_PULL_TARGET


def _trakt_ranked_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    fetch_page,
    row_schema_version: int | None,
    seen_identities: set[tuple[str, str, str]] | None = None,
) -> tuple[list[CandidateItem], dict[str, int]]:
    blocked_statuses = {
        Status.COMPLETED.value,
        Status.DROPPED.value,
        Status.PLANNING.value,
    }
    tracked_keys = get_tracked_keys_by_media_type(
        user,
        media_type,
        statuses=blocked_statuses,
    )
    blocked_identities = set(tracked_keys)
    blocked_identities.update(get_feedback_keys_by_media_type(user, media_type))
    if seen_identities and row_key != "all_time_greats_unseen":
        blocked_identities.update(seen_identities)

    required_pull = _get_cached_adaptive_pull_target(user.id, media_type, row_key)
    page = 1
    seen_identities: set[tuple[str, str, str]] = set()
    candidates: list[CandidateItem] = []
    filtered_candidates: list[CandidateItem] = []
    first_success_pull: int | None = None

    while len(candidates) < required_pull and len(candidates) < TRAKT_POPULAR_MAX_PULL_TARGET:
        page_candidates = fetch_page(
            page=page,
            limit=TRAKT_POPULAR_PAGE_SIZE,
        )
        if not page_candidates:
            break

        count_before = len(candidates)
        for candidate in page_candidates:
            identity = candidate.identity()
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
            candidates.append(candidate)

        filtered_candidates = exclude_tracked_items(candidates, blocked_identities)
        if len(filtered_candidates) >= MAX_ITEMS_PER_ROW and first_success_pull is None:
            first_success_pull = len(candidates)

        if len(page_candidates) < TRAKT_POPULAR_PAGE_SIZE:
            break

        if len(candidates) >= required_pull and len(filtered_candidates) < MAX_ITEMS_PER_ROW:
            required_pull = _clamp_adaptive_pull_target(required_pull + TRAKT_POPULAR_PULL_STEP)

        if len(candidates) == count_before:
            break

        page += 1

    if not filtered_candidates:
        filtered_candidates = exclude_tracked_items(candidates, blocked_identities)

    if len(filtered_candidates) >= MAX_ITEMS_PER_ROW:
        next_pull_target = first_success_pull or len(candidates)
    else:
        next_pull_target = max(required_pull, len(candidates))
    next_pull_target = _clamp_adaptive_pull_target(next_pull_target)

    meta: dict[str, int] = {
        ADAPTIVE_PULL_TARGET_META_KEY: next_pull_target,
        "last_pulled_count": len(candidates),
        "last_filtered_count": len(filtered_candidates),
    }
    if row_schema_version is not None:
        meta[ROW_CACHE_SCHEMA_META_KEY] = row_schema_version
    return filtered_candidates, meta


def _trakt_canon_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    seen_identities: set[tuple[str, str, str]] | None = None,
) -> tuple[list[CandidateItem], dict[str, int]]:
    if media_type == MediaTypes.MOVIE.value:
        fetch_page = TRAKT_ADAPTER.movie_popular
        row_schema_version: int | None = MOVIE_CANON_ROW_SCHEMA_VERSION
    elif media_type == MediaTypes.TV.value:
        fetch_page = lambda *, page, limit: TRAKT_ADAPTER.show_popular(  # noqa: E731
            page=page,
            limit=limit,
            media_type=MediaTypes.TV.value,
        )
        row_schema_version = None
    elif media_type == MediaTypes.ANIME.value:
        fetch_page = lambda *, page, limit: TRAKT_ADAPTER.show_popular(  # noqa: E731
            page=page,
            limit=limit,
            media_type=MediaTypes.ANIME.value,
            trakt_genres=["anime"],
        )
        row_schema_version = None
    else:
        return [], {
            ADAPTIVE_PULL_TARGET_META_KEY: TRAKT_POPULAR_DEFAULT_PULL_TARGET,
            "last_pulled_count": 0,
            "last_filtered_count": 0,
        }

    return _trakt_ranked_candidates(
        user,
        media_type,
        row_key=row_key,
        fetch_page=fetch_page,
        row_schema_version=row_schema_version,
        seen_identities=seen_identities,
    )


def _trakt_anticipated_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    seen_identities: set[tuple[str, str, str]] | None = None,
) -> tuple[list[CandidateItem], dict[str, int]]:
    if media_type == MediaTypes.MOVIE.value:
        fetch_page = TRAKT_ADAPTER.movie_anticipated
        row_schema_version: int | None = MOVIE_COMING_SOON_ROW_SCHEMA_VERSION
    elif media_type == MediaTypes.TV.value:
        fetch_page = lambda *, page, limit: TRAKT_ADAPTER.show_anticipated(  # noqa: E731
            page=page,
            limit=limit,
            media_type=MediaTypes.TV.value,
        )
        row_schema_version = None
    elif media_type == MediaTypes.ANIME.value:
        fetch_page = lambda *, page, limit: TRAKT_ADAPTER.show_anticipated(  # noqa: E731
            page=page,
            limit=limit,
            media_type=MediaTypes.ANIME.value,
            trakt_genres=["anime"],
        )
        row_schema_version = None
    else:
        return [], {
            ADAPTIVE_PULL_TARGET_META_KEY: TRAKT_POPULAR_DEFAULT_PULL_TARGET,
            "last_pulled_count": 0,
            "last_filtered_count": 0,
        }

    return _trakt_ranked_candidates(
        user,
        media_type,
        row_key=row_key,
        fetch_page=fetch_page,
        row_schema_version=row_schema_version,
        seen_identities=seen_identities,
    )


def _select_recent_anchors(user, media_type: str, *, max_anchors: int = 3):
    model = _model_for_media_type(media_type)
    if not model:
        return []

    anchors = (
        model.objects.filter(user=user)
        .filter(
            Q(status=Status.COMPLETED.value)
            | Q(score__gte=8),
        )
        .select_related("item")
        .order_by("-end_date", "-progressed_at", "-created_at")[: max_anchors * 3]
    )

    selected = []
    seen_ids = set()
    for entry in anchors:
        if entry.item_id in seen_ids:
            continue
        selected.append(entry)
        seen_ids.add(entry.item_id)
        if len(selected) >= max_anchors:
            break

    return selected


def _related_candidates_for_anchors(
    anchors,
    media_type: str,
    *,
    row_key: str,
    source_reason: str,
) -> list[CandidateItem]:
    candidates: list[CandidateItem] = []
    seen = set()

    for anchor in anchors:
        related = TMDB_ADAPTER.related(media_type, anchor.item.media_id, limit=120)
        for candidate in related:
            identity = candidate.identity()
            if identity in seen:
                continue
            seen.add(identity)
            candidate.row_key = row_key
            candidate.anchor_title = anchor.item.title
            candidate.source_reason = source_reason.format(anchor_title=anchor.item.title)
            candidates.append(candidate)

    return candidates


def _top_profile_genres(profile_payload: dict, *, limit: int = 3) -> list[str]:
    genre_affinity = profile_payload.get("genre_affinity") or {}
    if not genre_affinity:
        return []

    return [
        genre
        for genre, _ in sorted(
            genre_affinity.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:limit]
    ]


def _genre_discovery_candidates(
    media_type: str,
    row_key: str,
    profile_payload: dict,
    *,
    genres: list[str] | None = None,
    apply_scoring: bool = True,
) -> list[CandidateItem]:
    target_genres = genres or _top_profile_genres(profile_payload, limit=3)
    if not target_genres:
        return []

    candidates = TMDB_ADAPTER.genre_discovery(media_type, target_genres, limit=120)
    for candidate in candidates:
        candidate.row_key = row_key
        candidate.source_reason = "Genre affinity"

    if apply_scoring:
        score_candidates(candidates, profile_payload)

    return candidates


def _merge_unique_candidates(*candidate_sets: list[CandidateItem]) -> list[CandidateItem]:
    merged: list[CandidateItem] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate_set in candidate_sets:
        for candidate in candidate_set:
            identity = candidate.identity()
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(candidate)
    return merged


def _related_row_candidates(user, media_type: str, row_key: str, profile_payload: dict) -> list[CandidateItem]:
    anchors = _select_recent_anchors(user, media_type, max_anchors=3)
    candidates = _related_candidates_for_anchors(
        anchors,
        media_type,
        row_key=row_key,
        source_reason="Because you watched {anchor_title}",
    )

    score_candidates(candidates, profile_payload)
    return candidates
