"""Discover orchestration service."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from app.discover import cache_repo
from app.discover.filters import dedupe_candidates, exclude_tracked_items, get_tracked_keys_by_media_type
from app.discover.profile import MODEL_BY_MEDIA_TYPE, get_or_compute_taste_profile
from app.discover.providers.trakt_adapter import TraktDiscoverAdapter
from app.discover.providers.tmdb_adapter import TMDbDiscoverAdapter
from app.discover.registry import ALL_MEDIA_KEY, DISCOVER_MEDIA_TYPES, get_rows
from app.discover.schemas import CandidateItem, DiscoverPayload, RowDefinition, RowResult
from app.discover.scoring import score_candidates
from app.models import Item, ItemPersonCredit, ItemTag, MediaTypes, Season, Status
from app.providers import services

logger = logging.getLogger(__name__)

MAX_ITEMS_PER_ROW = 12
ROW_CACHE_TTL_SECONDS = 60 * 60
ROW_CACHE_TTL_LOCAL_SECONDS = 60 * 30
STALE_REFRESH_LOCK_SECONDS = 60
TRAKT_POPULAR_PAGE_SIZE = 100
TRAKT_POPULAR_PULL_STEP = 100
TRAKT_POPULAR_DEFAULT_PULL_TARGET = 100
TRAKT_POPULAR_MAX_PULL_TARGET = 2000
ADAPTIVE_PULL_TARGET_META_KEY = "adaptive_pull_target"
ROW_CACHE_SCHEMA_META_KEY = "schema_version"
MOVIE_CANON_ROW_SCHEMA_VERSION = 2
MOVIE_COMING_SOON_ROW_SCHEMA_VERSION = 1
MOVIE_PERSONALIZED_ROW_SCHEMA_VERSION = 1
ROW_CANDIDATE_BUFFER_MULTIPLIER = 5
MOVIE_PERSONALIZED_ROW_KEYS = {
    "top_picks_for_you",
    "comfort_rewatches",
    "wildcard_for_you",
}

ALWAYS_VISIBLE_EMPTY_ROWS = {
    "continue",
    "continue_all",
    "continue_up_next",
    "next_episode",
}

TMDB_ADAPTER = TMDbDiscoverAdapter()
TRAKT_ADAPTER = TraktDiscoverAdapter()


def _coerce_media_type(media_type: str | None) -> str:
    media_type = (media_type or ALL_MEDIA_KEY).strip().lower()
    if media_type == ALL_MEDIA_KEY:
        return media_type
    if media_type in DISCOVER_MEDIA_TYPES:
        return media_type
    return ALL_MEDIA_KEY


def _item_tag_map(user, item_ids: list[int]) -> dict[int, list[str]]:
    mapping: dict[int, list[str]] = defaultdict(list)
    if not item_ids:
        return mapping

    for item_tag in ItemTag.objects.filter(item_id__in=item_ids, tag__user=user).select_related("tag"):
        tag_name = (item_tag.tag.name or "").strip()
        if tag_name:
            mapping[item_tag.item_id].append(tag_name)

    return mapping


def _item_people_map(item_ids: list[int]) -> dict[int, list[str]]:
    mapping: dict[int, list[str]] = defaultdict(list)
    if not item_ids:
        return mapping

    for credit in ItemPersonCredit.objects.filter(item_id__in=item_ids).select_related("person"):
        person_name = (credit.person.name or "").strip() if credit.person_id else ""
        if person_name:
            mapping[credit.item_id].append(person_name)

    return mapping


def _entry_activity_datetime(entry):
    return (
        getattr(entry, "end_date", None)
        or getattr(entry, "progressed_at", None)
        or getattr(entry, "created_at", None)
    )


def _entries_to_candidates(
    entries,
    *,
    user,
    row_key: str,
    source_reason: str,
    override_media_type: str | None = None,
) -> list[CandidateItem]:
    entries = list(entries)
    if not entries:
        return []

    item_ids = [entry.item_id for entry in entries if entry.item_id]
    tag_map = _item_tag_map(user, item_ids)

    include_people = bool(
        override_media_type in {MediaTypes.MOVIE.value, MediaTypes.TV.value}
        or (entries and entries[0].item.media_type in {MediaTypes.MOVIE.value, MediaTypes.TV.value})
    )
    people_map = _item_people_map(item_ids) if include_people else {}

    candidates: list[CandidateItem] = []
    for entry in entries:
        item = entry.item
        media_type = override_media_type or item.media_type
        release_date = item.release_datetime.date().isoformat() if item.release_datetime else None
        activity_dt = _entry_activity_datetime(entry)
        candidate = CandidateItem(
            media_type=media_type,
            source=item.source,
            media_id=str(item.media_id),
            title=item.title,
            original_title=item.original_title,
            localized_title=item.localized_title,
            image=item.image,
            release_date=release_date,
            activity_at=activity_dt.isoformat() if activity_dt else None,
            genres=[str(genre).strip() for genre in (item.genres or []) if str(genre).strip()],
            tags=tag_map.get(item.id, []),
            people=people_map.get(item.id, []),
            popularity=item.provider_popularity,
            rating=item.provider_rating,
            rating_count=item.provider_rating_count,
            row_key=row_key,
            source_reason=source_reason,
        )
        if getattr(entry, "score", None) is not None:
            entry_score = float(entry.score)
            candidate.score_breakdown["user_score"] = entry_score
            if candidate.rating is None:
                candidate.rating = entry_score
        if activity_dt:
            candidate.score_breakdown["days_since_activity"] = float(
                max(0, (timezone.now() - activity_dt).days),
            )
        candidates.append(candidate)

    return candidates


def _model_for_media_type(media_type: str):
    model_name = MODEL_BY_MEDIA_TYPE.get(media_type)
    if not model_name:
        return None
    return apps.get_model("app", model_name)


def _in_progress_candidates(user, media_type: str, *, row_key: str, source_reason: str) -> list[CandidateItem]:
    if media_type == MediaTypes.TV.value and row_key == "next_episode":
        entries = (
            Season.objects.filter(user=user, status=Status.IN_PROGRESS.value)
            .select_related("item")
            .order_by("-progressed_at", "-created_at")
        )
        return _entries_to_candidates(
            entries,
            user=user,
            row_key=row_key,
            source_reason=source_reason,
            override_media_type=MediaTypes.TV.value,
        )

    model = _model_for_media_type(media_type)
    if not model:
        return []
    entries = (
        model.objects.filter(user=user, status=Status.IN_PROGRESS.value)
        .select_related("item")
        .order_by("-progressed_at", "-created_at")
    )
    return _entries_to_candidates(
        entries,
        user=user,
        row_key=row_key,
        source_reason=source_reason,
    )


def _planning_candidates(user, media_type: str, *, row_key: str, source_reason: str) -> list[CandidateItem]:
    model = _model_for_media_type(media_type)
    if not model:
        return []

    entries = (
        model.objects.filter(user=user, status=Status.PLANNING.value)
        .select_related("item")
        .order_by("-created_at")
    )
    return _entries_to_candidates(
        entries,
        user=user,
        row_key=row_key,
        source_reason=source_reason,
    )


def _comfort_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    source_reason: str,
    older_than_days: int,
    min_score: float = 8.0,
) -> list[CandidateItem]:
    model = _model_for_media_type(media_type)
    if not model:
        return []

    cutoff = timezone.now() - timedelta(days=older_than_days)
    entries = (
        model.objects.filter(
            user=user,
            status=Status.COMPLETED.value,
            score__gte=min_score,
        )
        .filter(
            Q(end_date__lte=cutoff)
            | Q(progressed_at__lte=cutoff)
            | Q(created_at__lte=cutoff),
        )
        .select_related("item")
        .order_by("-score", "-end_date", "-progressed_at", "-created_at")
    )

    return _entries_to_candidates(
        entries,
        user=user,
        row_key=row_key,
        source_reason=source_reason,
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


def _provider_row_candidates(media_type: str, row_key: str) -> list[CandidateItem]:
    if media_type == MediaTypes.MOVIE.value and row_key == "trending_right_now":
        return TRAKT_ADAPTER.movie_watched_weekly(limit=100)

    if media_type == MediaTypes.MOVIE.value and row_key == "all_time_greats_unseen":
        return TRAKT_ADAPTER.movie_popular(page=1, limit=TRAKT_POPULAR_PAGE_SIZE)

    if media_type == MediaTypes.MOVIE.value and row_key == "coming_soon":
        return TRAKT_ADAPTER.movie_anticipated(page=1, limit=TRAKT_POPULAR_PAGE_SIZE)

    if media_type not in {MediaTypes.MOVIE.value, MediaTypes.TV.value}:
        return []

    if row_key == "trending_tv":
        return TMDB_ADAPTER.trending(media_type, limit=80)

    if row_key in {"new_noteworthy", "new_returning_seasons"}:
        return TMDB_ADAPTER.current_cycle(media_type, limit=80)

    if row_key == "coming_soon":
        return TMDB_ADAPTER.upcoming(media_type, limit=80)

    if row_key == "all_time_great_tv":
        return TMDB_ADAPTER.top_rated(media_type, limit=80)

    return []


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


def _movie_trakt_ranked_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    fetch_page,
    row_schema_version: int,
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
    if seen_identities:
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

    return filtered_candidates, {
        ADAPTIVE_PULL_TARGET_META_KEY: next_pull_target,
        "last_pulled_count": len(candidates),
        "last_filtered_count": len(filtered_candidates),
        ROW_CACHE_SCHEMA_META_KEY: row_schema_version,
    }


def _movie_canon_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    seen_identities: set[tuple[str, str, str]] | None = None,
) -> tuple[list[CandidateItem], dict[str, int]]:
    return _movie_trakt_ranked_candidates(
        user,
        media_type,
        row_key=row_key,
        fetch_page=TRAKT_ADAPTER.movie_popular,
        row_schema_version=MOVIE_CANON_ROW_SCHEMA_VERSION,
        seen_identities=seen_identities,
    )


def _movie_anticipated_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    seen_identities: set[tuple[str, str, str]] | None = None,
) -> tuple[list[CandidateItem], dict[str, int]]:
    return _movie_trakt_ranked_candidates(
        user,
        media_type,
        row_key=row_key,
        fetch_page=TRAKT_ADAPTER.movie_anticipated,
        row_schema_version=MOVIE_COMING_SOON_ROW_SCHEMA_VERSION,
        seen_identities=seen_identities,
    )


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


def _top_picks_candidates(user, media_type: str, row_key: str, profile_payload: dict) -> list[CandidateItem]:
    candidates = _planning_candidates(
        user,
        media_type,
        row_key=row_key,
        source_reason="Ranked from your planning list",
    )
    score_candidates(candidates, profile_payload)
    _apply_top_picks_display_score(candidates)
    return candidates


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _calibrate_display_score(raw_score: float, *, offset: float, weight: float) -> float:
    return _clamp_unit(offset + (raw_score * weight))


def _apply_top_picks_display_score(candidates: list[CandidateItem]) -> list[CandidateItem]:
    for candidate in candidates:
        raw_score = float(candidate.final_score or 0.0)
        display_score = _calibrate_display_score(raw_score, offset=0.42, weight=0.44)
        candidate.display_score = round(display_score, 6)
    return candidates


def _apply_comfort_confidence(candidates: list[CandidateItem]) -> list[CandidateItem]:
    if not candidates:
        return candidates

    for candidate in candidates:
        base_score = float(candidate.final_score or 0.0)
        user_score = float(candidate.score_breakdown.get("user_score", 0.0))
        user_score_norm = _clamp_unit(user_score / 10.0)
        inactivity_days = float(candidate.score_breakdown.get("days_since_activity", 0.0))
        inactivity_norm = _clamp_unit(inactivity_days / 365.0)

        comfort_score = _clamp_unit(
            (base_score * 0.35)
            + (user_score_norm * 0.5)
            + (inactivity_norm * 0.15),
        )
        candidate.score_breakdown["base_score"] = round(base_score, 6)
        candidate.score_breakdown["user_score_norm"] = round(user_score_norm, 6)
        candidate.score_breakdown["inactivity_norm"] = round(inactivity_norm, 6)
        candidate.score_breakdown["comfort_score"] = round(comfort_score, 6)
        candidate.final_score = round(comfort_score, 6)

        display_score = _calibrate_display_score(comfort_score, offset=0.58, weight=0.38)
        if user_score_norm >= 0.9 and inactivity_days >= 180:
            display_score = max(display_score, 0.80)
        elif user_score_norm >= 0.8 and inactivity_days >= 90:
            display_score = max(display_score, 0.70)
        candidate.display_score = round(display_score, 6)

    candidates.sort(
        key=lambda candidate: (
            candidate.final_score if candidate.final_score is not None else -1.0,
            float(candidate.score_breakdown.get("user_score", -1.0)),
            float(candidate.score_breakdown.get("days_since_activity", -1.0)),
        ),
        reverse=True,
    )
    return candidates


def _wildcard_genres(profile_payload: dict) -> list[str]:
    genre_affinity = profile_payload.get("genre_affinity") or {}
    if not genre_affinity:
        return []

    ranked = sorted(
        (
            (str(genre), float(value))
            for genre, value in genre_affinity.items()
        ),
        key=lambda item: item[1],
        reverse=True,
    )

    top_genres = [genre for genre, _ in ranked[:3]]
    less_used_genres = [
        genre
        for genre, _ in sorted(ranked[3:], key=lambda item: item[1])
    ][:2]
    return top_genres + less_used_genres


def _apply_wildcard_novelty(candidates: list[CandidateItem], profile_payload: dict) -> list[CandidateItem]:
    if not candidates:
        return candidates

    recent_affinity = {
        str(key).lower(): float(value)
        for key, value in (profile_payload.get("recent_genre_affinity") or {}).items()
    }
    if not recent_affinity:
        for candidate in candidates:
            raw_score = float(candidate.final_score or 0.0)
            candidate.display_score = round(
                _calibrate_display_score(raw_score, offset=0.45, weight=0.39),
                6,
            )
        return candidates

    for candidate in candidates:
        base_score = float(candidate.final_score or 0.0)
        genre_keys = [str(genre).strip().lower() for genre in (candidate.genres or []) if str(genre).strip()]
        if genre_keys:
            exposure_values = [recent_affinity.get(genre_key, 0.0) for genre_key in genre_keys]
            exposure = sum(exposure_values) / len(exposure_values)
        else:
            exposure = 0.5

        novelty_score = max(0.0, min(1.0, 1.0 - exposure))
        wildcard_score = _clamp_unit((base_score * 0.7) + (novelty_score * 0.3))
        candidate.score_breakdown["base_score"] = round(base_score, 6)
        candidate.score_breakdown["novelty_score"] = round(novelty_score, 6)
        candidate.score_breakdown["wildcard_score"] = round(wildcard_score, 6)
        candidate.final_score = round(wildcard_score, 6)
        candidate.display_score = round(
            _calibrate_display_score(wildcard_score, offset=0.45, weight=0.39),
            6,
        )

    candidates.sort(
        key=lambda candidate: (
            candidate.final_score if candidate.final_score is not None else -1.0,
            candidate.rating if candidate.rating is not None else -1.0,
            candidate.popularity if candidate.popularity is not None else -1.0,
        ),
        reverse=True,
    )
    return candidates


def _wildcard_candidates(user, media_type: str, row_key: str, profile_payload: dict) -> list[CandidateItem]:
    planning_candidates = _planning_candidates(
        user,
        media_type,
        row_key=row_key,
        source_reason="Wildcard from your backlog",
    )
    anchors = _select_recent_anchors(user, media_type, max_anchors=3)
    related_candidates = _related_candidates_for_anchors(
        anchors,
        media_type,
        row_key=row_key,
        source_reason="Wildcard from {anchor_title}",
    )
    wildcard_genres = _wildcard_genres(profile_payload)
    if not wildcard_genres:
        wildcard_genres = _top_profile_genres(profile_payload, limit=3)
    if not wildcard_genres and not anchors and not planning_candidates:
        return []

    genre_candidates = _genre_discovery_candidates(
        media_type,
        row_key,
        profile_payload,
        genres=wildcard_genres,
        apply_scoring=False,
    )
    top_rated_candidates = TMDB_ADAPTER.top_rated(media_type, limit=80)
    for candidate in top_rated_candidates:
        candidate.row_key = row_key
        candidate.source_reason = "Wildcard quality fallback"

    candidates = _merge_unique_candidates(
        planning_candidates,
        related_candidates,
        genre_candidates,
        top_rated_candidates,
    )
    score_candidates(candidates, profile_payload)
    return _apply_wildcard_novelty(candidates, profile_payload)


def _movie_night_candidates(user, media_type: str, *, row_key: str) -> list[CandidateItem]:
    candidates = _planning_candidates(
        user,
        media_type,
        row_key=row_key,
        source_reason="Short runtime planning pick",
    )
    filtered = [
        candidate
        for candidate in candidates
        if (
            Item.objects.filter(
                media_type=media_type,
                source=candidate.source,
                media_id=candidate.media_id,
                runtime_minutes__lt=120,
            ).exists()
        )
    ]
    return filtered


def _short_runs_candidates(user, media_type: str, *, row_key: str) -> list[CandidateItem]:
    candidates = _planning_candidates(
        user,
        media_type,
        row_key=row_key,
        source_reason="Lower commitment planning pick",
    )
    filtered = [
        candidate
        for candidate in candidates
        if (
            Item.objects.filter(
                media_type=media_type,
                source=candidate.source,
                media_id=candidate.media_id,
                runtime_minutes__lt=45,
            ).exists()
        )
    ]
    return filtered


def _build_all_media_row(user, row_definition: RowDefinition, profile_payload: dict) -> list[CandidateItem]:
    if row_definition.key == "continue_all":
        combined: list[CandidateItem] = []
        for media_type in DISCOVER_MEDIA_TYPES:
            combined.extend(
                _in_progress_candidates(
                    user,
                    media_type,
                    row_key=row_definition.key,
                    source_reason="In progress",
                ),
            )
        combined.sort(
            key=lambda candidate: candidate.activity_at or "",
            reverse=True,
        )
        return combined[:MAX_ITEMS_PER_ROW]

    if row_definition.key == "trending_all":
        combined: list[CandidateItem] = []
        combined.extend(TMDB_ADAPTER.trending(MediaTypes.MOVIE.value, limit=20))
        combined.extend(TMDB_ADAPTER.trending(MediaTypes.TV.value, limit=20))
        combined.sort(
            key=lambda candidate: candidate.popularity if candidate.popularity is not None else -1,
            reverse=True,
        )
        return combined[:MAX_ITEMS_PER_ROW]

    if row_definition.key == "top_picks_all":
        combined: list[CandidateItem] = []
        for media_type in DISCOVER_MEDIA_TYPES:
            combined.extend(
                _planning_candidates(
                    user,
                    media_type,
                    row_key=row_definition.key,
                    source_reason="Top local planning fit",
                ),
            )

        score_candidates(combined, profile_payload)
        return combined[:MAX_ITEMS_PER_ROW]

    return []


def _build_row_candidates(
    user,
    media_type: str,
    row_definition: RowDefinition,
    profile_payload: dict,
) -> list[CandidateItem]:
    row_key = row_definition.key

    if media_type == ALL_MEDIA_KEY:
        return _build_all_media_row(user, row_definition, profile_payload)

    if row_key in {"continue_up_next", "next_episode", "continue"}:
        return _in_progress_candidates(
            user,
            media_type,
            row_key=row_key,
            source_reason="Continue where you left off",
        )

    if row_key == "backlog":
        return _planning_candidates(
            user,
            media_type,
            row_key=row_key,
            source_reason="Planned and unplayed",
        )

    if row_key in {
        "trending_right_now",
        "trending_tv",
        "new_noteworthy",
        "new_returning_seasons",
        "coming_soon",
        "all_time_greats_unseen",
        "all_time_great_tv",
        "trending",
        "new_releases",
        "all_time_greats",
        "new_episodes",
        "top_untried",
        "hotness",
        "top_100",
    }:
        return _provider_row_candidates(media_type, row_key)

    if row_key == "top_picks_for_you":
        return _top_picks_candidates(user, media_type, row_key, profile_payload)

    if row_key in {"backlog_ranked", "backlog", "great_tonight"}:
        candidates = _planning_candidates(
            user,
            media_type,
            row_key=row_key,
            source_reason="Backlog candidate",
        )
        score_candidates(candidates, profile_payload)
        return candidates

    if row_key in {"because_you_watched", "because_you_liked", "because_you_played", "because_you_read", "because_you_listen"}:
        return _related_row_candidates(user, media_type, row_key, profile_payload)

    if row_key in {"hidden_gems_genres", "genre_spotlight"}:
        return _genre_discovery_candidates(media_type, row_key, profile_payload)

    if row_key in {"comfort_picks", "comfort_binge", "comfort", "comfort_replay", "comfort_rewatches"}:
        older = 180 if media_type == MediaTypes.TV.value else 90
        candidates: list[CandidateItem] = []
        seen: set[tuple[str, str, str]] = set()
        comfort_tiers = [
            (8.0, older),
            (8.0, max(30, older - 30)),
            (7.0, max(30, older - 30)),
            (7.0, 30),
            (6.0, 30),
        ]
        for min_score, min_days in comfort_tiers:
            tier_candidates = _comfort_candidates(
                user,
                media_type,
                row_key=row_key,
                source_reason="Past favorite",
                older_than_days=min_days,
                min_score=min_score,
            )
            for candidate in tier_candidates:
                identity = candidate.identity()
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(candidate)
            if len(candidates) >= MAX_ITEMS_PER_ROW * 3:
                break

        score_candidates(candidates, profile_payload)
        if row_key == "comfort_rewatches":
            _apply_comfort_confidence(candidates)
        return candidates

    if row_key == "wildcard_for_you":
        return _wildcard_candidates(user, media_type, row_key, profile_payload)

    if row_key == "movie_night":
        candidates = _movie_night_candidates(user, media_type, row_key=row_key)
        score_candidates(candidates, profile_payload)
        return candidates

    if row_key in {"short_runs", "quick_plays"}:
        candidates = _short_runs_candidates(user, media_type, row_key=row_key)
        score_candidates(candidates, profile_payload)
        return candidates

    return []


def _row_ttl_seconds(row_definition: RowDefinition) -> int:
    return ROW_CACHE_TTL_LOCAL_SECONDS if row_definition.source == "local" else ROW_CACHE_TTL_SECONDS


def _is_missing_image(candidate: CandidateItem) -> bool:
    return not candidate.image or candidate.image == settings.IMG_NONE


def _hydrate_trending_movie_artwork(candidates: list[CandidateItem]) -> None:
    """Hydrate missing artwork for displayed Trakt-ranked movie candidates."""
    display_candidates = [
        candidate
        for candidate in candidates[:MAX_ITEMS_PER_ROW]
        if candidate.media_type == MediaTypes.MOVIE.value
        and candidate.source == TMDB_ADAPTER.provider
    ]
    if not display_candidates:
        return

    missing = [candidate for candidate in display_candidates if _is_missing_image(candidate)]
    if not missing:
        return

    local_images = {
        str(item.media_id): item.image
        for item in Item.objects.filter(
            media_type=MediaTypes.MOVIE.value,
            source=TMDB_ADAPTER.provider,
            media_id__in=[candidate.media_id for candidate in missing],
        ).only("media_id", "image")
        if item.image and item.image != settings.IMG_NONE
    }

    for candidate in missing:
        local_image = local_images.get(str(candidate.media_id))
        if local_image:
            candidate.image = local_image

    for candidate in missing:
        if not _is_missing_image(candidate):
            continue
        try:
            metadata = services.get_media_metadata(
                MediaTypes.MOVIE.value,
                candidate.media_id,
                TMDB_ADAPTER.provider,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "discover_tmdb_artwork_lookup_failed media_id=%s error=%s",
                candidate.media_id,
                error,
            )
            continue

        image = (metadata or {}).get("image")
        if image:
            candidate.image = image


def _blocked_statuses_for_row(row_definition: RowDefinition) -> set[str] | None:
    if row_definition.key == "wildcard_for_you":
        return {
            Status.COMPLETED.value,
            Status.DROPPED.value,
            Status.IN_PROGRESS.value,
        }

    if row_definition.key in {"trending_right_now", "all_time_greats_unseen", "coming_soon"}:
        return {
            Status.COMPLETED.value,
            Status.DROPPED.value,
            Status.PLANNING.value,
        }
    return None


def _queue_stale_refresh(user_id: int, media_type: str, row_key: str, show_more: bool) -> None:
    lock_key = f"discover:refresh:{user_id}:{media_type}:{row_key}:{int(show_more)}"
    if not cache.add(lock_key, True, timeout=STALE_REFRESH_LOCK_SECONDS):
        return

    if getattr(settings, "TESTING", False):
        return

    try:
        from app.tasks import refresh_discover_rows

        refresh_discover_rows.delay(user_id, media_type, [row_key], show_more=show_more)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "discover_refresh_enqueue_failed user_id=%s media_type=%s row_key=%s error=%s",
            user_id,
            media_type,
            row_key,
            error,
        )


def _build_and_cache_row(
    user,
    media_type: str,
    row_definition: RowDefinition,
    profile_payload: dict,
    *,
    seen_identities: set[tuple[str, str, str]] | None = None,
) -> RowResult:
    started = timezone.now()
    row_meta: dict | None = None
    if media_type == MediaTypes.MOVIE.value and row_definition.key in {
        "all_time_greats_unseen",
        "coming_soon",
    }:
        if row_definition.key == "all_time_greats_unseen":
            candidates, row_meta = _movie_canon_candidates(
                user,
                media_type,
                row_key=row_definition.key,
                seen_identities=seen_identities,
            )
        else:
            candidates, row_meta = _movie_anticipated_candidates(
                user,
                media_type,
                row_key=row_definition.key,
                seen_identities=seen_identities,
            )
    else:
        candidates = _build_row_candidates(user, media_type, row_definition, profile_payload)

    required_schema_version = _required_row_cache_schema_version(media_type, row_definition.key)
    if required_schema_version is not None:
        row_meta = dict(row_meta or {})
        row_meta[ROW_CACHE_SCHEMA_META_KEY] = required_schema_version

    if not row_definition.allow_tracked:
        blocked_statuses = _blocked_statuses_for_row(row_definition)
        tracked_keys = (
            get_tracked_keys_by_media_type(
                user,
                media_type,
                statuses=blocked_statuses,
            )
            if media_type != ALL_MEDIA_KEY
            else set()
        )
        if media_type == ALL_MEDIA_KEY:
            for media_type_key in DISCOVER_MEDIA_TYPES:
                tracked_keys.update(
                    get_tracked_keys_by_media_type(
                        user,
                        media_type_key,
                        statuses=blocked_statuses,
                    ),
                )
        candidates = exclude_tracked_items(candidates, tracked_keys)

    if media_type == MediaTypes.MOVIE.value and row_definition.key in {
        "trending_right_now",
        "all_time_greats_unseen",
        "coming_soon",
    }:
        _hydrate_trending_movie_artwork(candidates)

    buffered_limit = MAX_ITEMS_PER_ROW * ROW_CANDIDATE_BUFFER_MULTIPLIER
    row = RowResult(
        key=row_definition.key,
        title=row_definition.title,
        mission=row_definition.mission,
        why=row_definition.why,
        source=row_definition.source,
        items=candidates[:buffered_limit],
        show_more=row_definition.show_more,
        source_state="live",
    )

    cache_payload = row.to_dict()
    if row_meta:
        cache_payload["meta"] = row_meta

    cache_repo.set_row_cache(
        user.id,
        media_type,
        row_definition.key,
        cache_payload,
        ttl_seconds=_row_ttl_seconds(row_definition),
    )

    duration_ms = int((timezone.now() - started).total_seconds() * 1000)
    logger.info(
        "discover_row_built user_id=%s media_type=%s row_key=%s result_count=%s source=%s duration_ms=%s",
        user.id,
        media_type,
        row_definition.key,
        len(row.items),
        row.source_state,
        duration_ms,
    )

    return row


def _apply_row_definition_metadata(
    row: RowResult,
    row_definition: RowDefinition,
) -> RowResult:
    """Keep display metadata aligned with the current registry definition."""
    row.title = row_definition.title
    row.mission = row_definition.mission
    row.why = row_definition.why
    row.source = row_definition.source
    row.show_more = row_definition.show_more
    return row


def _row_requires_artwork_rebuild(
    media_type: str,
    row_definition: RowDefinition,
    row: RowResult,
) -> bool:
    if media_type != MediaTypes.MOVIE.value or row_definition.key not in {
        "trending_right_now",
        "all_time_greats_unseen",
        "coming_soon",
    }:
        return False
    return any(_is_missing_image(item) for item in row.items[:MAX_ITEMS_PER_ROW])


def _is_row_cache_compatible(
    media_type: str,
    row_definition: RowDefinition,
    cached_payload: dict,
) -> bool:
    required_schema_version = _required_row_cache_schema_version(media_type, row_definition.key)
    if required_schema_version is None:
        return True

    meta = cached_payload.get("meta")
    if not isinstance(meta, dict):
        return False

    try:
        return int(meta.get(ROW_CACHE_SCHEMA_META_KEY, 0)) >= required_schema_version
    except (TypeError, ValueError):
        return False


def _required_row_cache_schema_version(media_type: str, row_key: str) -> int | None:
    if media_type != MediaTypes.MOVIE.value:
        return None
    if row_key == "all_time_greats_unseen":
        return MOVIE_CANON_ROW_SCHEMA_VERSION
    if row_key == "coming_soon":
        return MOVIE_COMING_SOON_ROW_SCHEMA_VERSION
    if row_key in MOVIE_PERSONALIZED_ROW_KEYS:
        return MOVIE_PERSONALIZED_ROW_SCHEMA_VERSION
    return None


def get_discover_rows(user, media_type: str, *, show_more: bool = False) -> list[RowResult]:
    """Return discover rows for selected media type."""
    media_type = _coerce_media_type(media_type)
    row_definitions = get_rows(media_type, include_show_more=show_more)
    profile_payload = get_or_compute_taste_profile(user, media_type)

    seen_identities: set[tuple[str, str, str]] = set()
    rows: list[RowResult] = []

    for row_definition in row_definitions:
        try:
            cached_payload, is_stale = cache_repo.get_row_cache(user.id, media_type, row_definition.key)

            if cached_payload:
                row = RowResult.from_dict(cached_payload)
                if row.source != row_definition.source:
                    row = _build_and_cache_row(
                        user,
                        media_type,
                        row_definition,
                        profile_payload,
                        seen_identities=seen_identities,
                    )
                elif not _is_row_cache_compatible(media_type, row_definition, cached_payload):
                    row = _build_and_cache_row(
                        user,
                        media_type,
                        row_definition,
                        profile_payload,
                        seen_identities=seen_identities,
                    )
                elif _row_requires_artwork_rebuild(media_type, row_definition, row):
                    row = _build_and_cache_row(
                        user,
                        media_type,
                        row_definition,
                        profile_payload,
                        seen_identities=seen_identities,
                    )
                else:
                    row = _apply_row_definition_metadata(row, row_definition)
                    if is_stale:
                        row.is_stale = True
                        row.source_state = "stale"
                        _queue_stale_refresh(user.id, media_type, row_definition.key, show_more)
                    else:
                        row.source_state = "cache"
            else:
                row = _build_and_cache_row(
                    user,
                    media_type,
                    row_definition,
                    profile_payload,
                    seen_identities=seen_identities,
                )

            prior_seen_identities = set(seen_identities)
            before_count = len(row.items)
            row.items = dedupe_candidates(row.items, seen_identities=seen_identities)
            dedupe_removed = before_count - len(row.items)

            if (
                media_type == MediaTypes.MOVIE.value
                and row_definition.key in {"all_time_greats_unseen", "coming_soon"}
                and len(row.items) < MAX_ITEMS_PER_ROW
                and dedupe_removed > 0
            ):
                seen_identities.clear()
                seen_identities.update(prior_seen_identities)
                row = _build_and_cache_row(
                    user,
                    media_type,
                    row_definition,
                    profile_payload,
                    seen_identities=prior_seen_identities,
                )
                before_count = len(row.items)
                row.items = dedupe_candidates(row.items, seen_identities=seen_identities)
                dedupe_removed = before_count - len(row.items)

            row.items = row.items[:MAX_ITEMS_PER_ROW]
            filtered_count = before_count - len(row.items)

            if not row.items and row_definition.key not in ALWAYS_VISIBLE_EMPTY_ROWS:
                continue

            if len(row.items) < row_definition.min_items and row_definition.key not in ALWAYS_VISIBLE_EMPTY_ROWS:
                continue

            logger.info(
                "discover_row_render user_id=%s media_type=%s row_key=%s result_count=%s source=%s filtered_count=%s",
                user.id,
                media_type,
                row_definition.key,
                len(row.items),
                row.source_state,
                filtered_count,
            )
            rows.append(row)
        except Exception as error:  # noqa: BLE001
            logger.exception(
                "discover_row_failed user_id=%s media_type=%s row_key=%s error=%s",
                user.id,
                media_type,
                row_definition.key,
                error,
            )
            continue

    return rows


def get_discover_payload(user, media_type: str, *, show_more: bool = False) -> DiscoverPayload:
    """Return top-level discover payload for selected media type."""
    media_type = _coerce_media_type(media_type)
    rows = get_discover_rows(user, media_type, show_more=show_more)
    return DiscoverPayload(
        media_type=media_type,
        rows=rows,
        show_more=show_more,
    )


def refresh_rows_for_user(user, media_type: str, row_keys: list[str], *, show_more: bool = False) -> int:
    """Rebuild selected rows and refresh row cache entries."""
    media_type = _coerce_media_type(media_type)
    row_definitions = [
        row
        for row in get_rows(media_type, include_show_more=True)
        if row.key in set(row_keys)
    ]
    profile_payload = get_or_compute_taste_profile(user, media_type, force=False)

    refreshed = 0
    for row_definition in row_definitions:
        _build_and_cache_row(user, media_type, row_definition, profile_payload)
        refreshed += 1

    return refreshed
