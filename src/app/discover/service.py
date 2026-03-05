"""Discover orchestration service."""

from __future__ import annotations

import math
import logging
from collections import defaultdict
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Q
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
MOVIE_PERSONALIZED_ROW_SCHEMA_VERSION = 3
ROW_CANDIDATE_BUFFER_MULTIPLIER = 5
MOVIE_PERSONALIZED_ROW_KEYS = {
    "top_picks_for_you",
    "comfort_rewatches",
}

ALWAYS_VISIBLE_EMPTY_ROWS = {
    "continue",
    "continue_all",
    "continue_up_next",
    "next_episode",
}
HOLIDAY_STRONG_TERMS = {"christmas", "xmas", "noel"}
HOLIDAY_SOFT_TERMS = {"holiday", "holidays", "new year", "new years"}
COMFORT_DIVERSITY_DECAY = 0.92
COMFORT_PHASE_LANE_QUOTA = 4
COMFORT_PHASE_LANE_WINDOW = 6
COMFORT_PHASE_EVIDENCE_THRESHOLD = 0.25
COMFORT_PHASE_POOL_MIN_BACKFILL = 6
COMFORT_ERA_DECAY = 0.95
COMFORT_LEGACY_ERA_DECAY = 0.97
COMFORT_ERA_OPENING_WINDOW = 5
COMFORT_ERA_OPENING_DECAY = 0.86
COMFORT_LEGACY_OPENING_DECAY = 0.9
COMFORT_STRONG_PHASE_OPENING_WINDOW = MAX_ITEMS_PER_ROW
COMFORT_MEDIUM_PHASE_SUPERIORITY_MARGIN = 0.04
COMFORT_HOT_RECENCY_SELECTIVE_EXPONENT = 0.75
COMFORT_HOT_RECENCY_TAG_SPARSE_MULTIPLIER = 0.2
COMFORT_TAG_RICH_CANDIDATE_COVERAGE_THRESHOLD = 0.35
COMFORT_TAG_RICH_HISTORY_COVERAGE_THRESHOLD = 0.25
COMFORT_RECENT_HISTORY_TAG_WINDOW_DAYS = 90
COMFORT_DEBUG_TOP_N = 12
COMFORT_SPREAD_COMPRESSION_THRESHOLD = 0.08
TOP_PICKS_PHASE_WEIGHT = 0.34
TOP_PICKS_HOT_RECENCY_WEIGHT = 0.14
TOP_PICKS_GENRE_WEIGHT = 0.2
TOP_PICKS_TAG_WEIGHT = 0.16
TOP_PICKS_POPULARITY_WEIGHT = 0.09
TOP_PICKS_RATING_WEIGHT = 0.07
GENERIC_PHASE_TERMS = {
    "action",
    "adventure",
    "animation",
    "comedy",
    "drama",
    "family",
    "fantasy",
    "horror",
    "mystery",
    "romance",
    "science fiction",
    "sci-fi",
    "thriller",
}
PHASE_LABEL_OVERRIDES = {
    "action": "Popcorn Action",
    "adventure": "Big Adventure",
    "animation": "Animated Comfort",
    "comedy": "Feel-Good Comedy",
    "drama": "Character Drama",
    "family": "Family Comfort",
    "fantasy": "Escapist Fantasy",
    "mystery": "Puzzle Mystery",
    "romance": "Warm Romance",
    "science fiction": "Sci-Fi Adventure",
    "sci-fi": "Sci-Fi Adventure",
    "thriller": "Clever Suspense",
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


def _rewatch_counts(user, model, item_ids: list[int]) -> dict[int, int]:
    if not item_ids:
        return {}

    grouped = (
        model.objects.filter(
            user=user,
            status=Status.COMPLETED.value,
            item_id__in=item_ids,
        )
        .values("item_id")
        .annotate(watch_count=Count("id"))
        .filter(watch_count__gt=1)
    )
    return {
        int(row["item_id"]): int(row["watch_count"])
        for row in grouped
        if row.get("item_id")
    }


def _entries_to_candidates(
    entries,
    *,
    user,
    row_key: str,
    source_reason: str,
    override_media_type: str | None = None,
    rewatch_counts: dict[int, int] | None = None,
    phase_evidence_by_item: dict[int, float] | None = None,
    phase_pool_bucket_by_item: dict[int, str] | None = None,
    recent_history_tag_coverage: float | None = None,
) -> list[CandidateItem]:
    entries = list(entries)
    if not entries:
        return []
    rewatch_counts = rewatch_counts or {}
    phase_evidence_by_item = phase_evidence_by_item or {}
    phase_pool_bucket_by_item = phase_pool_bucket_by_item or {}

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
        candidate.score_breakdown["rewatch_count"] = float(
            rewatch_counts.get(item.id, 1),
        )
        candidate.score_breakdown["phase_evidence"] = float(
            phase_evidence_by_item.get(item.id, 0.0),
        )
        if recent_history_tag_coverage is not None:
            candidate.score_breakdown["recent_history_tag_coverage"] = float(
                _clamp_unit(recent_history_tag_coverage),
            )
        phase_bucket = phase_pool_bucket_by_item.get(item.id, "")
        candidate.score_breakdown["phase_pool_strong"] = 1.0 if phase_bucket == "strong_phase" else 0.0
        candidate.score_breakdown["phase_pool_medium"] = 1.0 if phase_bucket == "medium_phase" else 0.0
        candidate.score_breakdown["phase_pool_backfill"] = 1.0 if phase_bucket == "weak_backfill" else 0.0
        candidate.score_breakdown["phase_pool_weak_only"] = 1.0 if phase_bucket == "weak_only" else 0.0
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


def _recent_completed_tag_coverage(
    user,
    media_type: str,
    *,
    window_days: int = COMFORT_RECENT_HISTORY_TAG_WINDOW_DAYS,
) -> float:
    model = _model_for_media_type(media_type)
    if not model:
        return 0.0

    cutoff = timezone.now() - timedelta(days=window_days)
    recent_entries = list(
        model.objects.filter(
            user=user,
            status=Status.COMPLETED.value,
        )
        .filter(
            Q(end_date__gte=cutoff)
            | Q(progressed_at__gte=cutoff)
            | Q(created_at__gte=cutoff),
        )
        .only("item_id")[:300],
    )
    if not recent_entries:
        return 0.0

    item_ids = [entry.item_id for entry in recent_entries if entry.item_id]
    tag_map = _item_tag_map(user, item_ids)
    tagged_count = sum(1 for entry in recent_entries if tag_map.get(entry.item_id))
    return _clamp_unit(tagged_count / max(1, len(recent_entries)))


def _comfort_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    source_reason: str,
    older_than_days: int,
    min_score: float = 8.0,
    profile_payload: dict | None = None,
) -> list[CandidateItem]:
    model = _model_for_media_type(media_type)
    if not model:
        return []

    now = timezone.now()
    cutoff = now - timedelta(days=older_than_days)
    rated_entries = (
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

    unrated_cutoff = now - timedelta(days=max(older_than_days, 90))
    unrated_entries = (
        model.objects.filter(
            user=user,
            status=Status.COMPLETED.value,
            score__isnull=True,
        )
        .filter(
            Q(end_date__lte=unrated_cutoff)
            | Q(progressed_at__lte=unrated_cutoff)
            | Q(created_at__lte=unrated_cutoff),
        )
        .select_related("item")
        .order_by("-end_date", "-progressed_at", "-created_at")
    )

    entries = list(rated_entries) + list(unrated_entries)
    item_ids = [entry.item_id for entry in entries if entry.item_id]
    rewatch_count_map = _rewatch_counts(user, model, item_ids)
    tag_map = _item_tag_map(user, item_ids)
    phase_evidence_by_item: dict[int, float] = {}
    phase_pool_bucket_by_item: dict[int, str] = {}
    phase_genre_affinity, phase_tag_affinity = _phase_affinity_maps(profile_payload)
    if entries and (phase_genre_affinity or phase_tag_affinity):
        scored_entries = [
            (
                entry,
                _entry_phase_evidence(
                    entry,
                    tag_map=tag_map,
                    phase_genre_affinity=phase_genre_affinity,
                    phase_tag_affinity=phase_tag_affinity,
                ),
            )
            for entry in entries
        ]
        strong_phase_entries = [entry for entry, evidence in scored_entries if evidence >= COMFORT_PHASE_EVIDENCE_THRESHOLD]
        medium_phase_entries = [entry for entry, evidence in scored_entries if 0.0 < evidence < COMFORT_PHASE_EVIDENCE_THRESHOLD]
        weak_phase_entries = [entry for entry, evidence in scored_entries if evidence <= 0.0]
        phase_entries = strong_phase_entries + medium_phase_entries
        if phase_entries:
            weak_backfill_limit = min(
                len(weak_phase_entries),
                max(COMFORT_PHASE_POOL_MIN_BACKFILL, len(phase_entries)),
            )
            selected_weak_entries = weak_phase_entries[:weak_backfill_limit]
            entries = phase_entries + selected_weak_entries
            for entry in selected_weak_entries:
                phase_pool_bucket_by_item[entry.item_id] = "weak_backfill"
        else:
            entries = weak_phase_entries
            for entry in weak_phase_entries:
                phase_pool_bucket_by_item[entry.item_id] = "weak_only"

        for entry in strong_phase_entries:
            phase_pool_bucket_by_item[entry.item_id] = "strong_phase"
        for entry in medium_phase_entries:
            if phase_pool_bucket_by_item.get(entry.item_id) != "strong_phase":
                phase_pool_bucket_by_item[entry.item_id] = "medium_phase"
        for entry, evidence in scored_entries:
            if evidence > phase_evidence_by_item.get(entry.item_id, 0.0):
                phase_evidence_by_item[entry.item_id] = evidence

    recent_cutoff = now - timedelta(days=COMFORT_RECENT_HISTORY_TAG_WINDOW_DAYS)
    recent_total = 0
    recent_with_tags = 0
    for entry in entries:
        activity_dt = _entry_activity_datetime(entry)
        if not activity_dt or activity_dt < recent_cutoff:
            continue
        recent_total += 1
        if tag_map.get(entry.item_id):
            recent_with_tags += 1
    if recent_total > 0:
        recent_history_tag_coverage = _clamp_unit(recent_with_tags / recent_total)
    else:
        overall_total = len(entries)
        overall_with_tags = sum(1 for entry in entries if tag_map.get(entry.item_id))
        recent_history_tag_coverage = _clamp_unit(
            (overall_with_tags / overall_total) if overall_total else 0.0,
        )

    return _entries_to_candidates(
        entries,
        user=user,
        row_key=row_key,
        source_reason=source_reason,
        rewatch_counts=rewatch_count_map,
        phase_evidence_by_item=phase_evidence_by_item,
        phase_pool_bucket_by_item=phase_pool_bucket_by_item,
        recent_history_tag_coverage=recent_history_tag_coverage,
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
    recent_history_tag_coverage = _recent_completed_tag_coverage(
        user,
        media_type,
        window_days=COMFORT_RECENT_HISTORY_TAG_WINDOW_DAYS,
    )
    for candidate in candidates:
        candidate.score_breakdown["recent_history_tag_coverage"] = round(
            recent_history_tag_coverage,
            6,
        )

    _apply_top_picks_confidence(candidates, profile_payload)
    _apply_top_picks_display_score(candidates)
    return candidates


def _apply_top_picks_confidence(
    candidates: list[CandidateItem],
    profile_payload: dict | None = None,
) -> list[CandidateItem]:
    if not candidates:
        return candidates

    phase_genre_affinity, phase_tag_affinity = _phase_affinity_maps(profile_payload)
    phase_affinity = phase_genre_affinity
    phase_top = sorted(phase_affinity, key=phase_affinity.get, reverse=True)[:5]

    candidate_tag_coverage_pool = _clamp_unit(
        sum(1 for candidate in candidates if candidate.tags) / max(1, len(candidates)),
    )
    history_tag_coverages = [
        float(candidate.score_breakdown.get("recent_history_tag_coverage", 0.0))
        for candidate in candidates
        if "recent_history_tag_coverage" in candidate.score_breakdown
    ]
    recent_history_tag_coverage = _clamp_unit(
        (sum(history_tag_coverages) / len(history_tag_coverages))
        if history_tag_coverages
        else 0.0,
    )
    tag_signal_mode = (
        "tag_rich"
        if (
            candidate_tag_coverage_pool >= COMFORT_TAG_RICH_CANDIDATE_COVERAGE_THRESHOLD
            and recent_history_tag_coverage >= COMFORT_TAG_RICH_HISTORY_COVERAGE_THRESHOLD
        )
        else "tag_sparse"
    )
    hot_recency_mode_multiplier = (
        1.0 if tag_signal_mode == "tag_rich" else COMFORT_HOT_RECENCY_TAG_SPARSE_MULTIPLIER
    )

    for candidate in candidates:
        phase_genre_bonus = float(candidate.score_breakdown.get("phase_genre_bonus", 0.0))
        phase_tag_bonus = float(candidate.score_breakdown.get("phase_tag_bonus", 0.0))
        recency_bonus = float(candidate.score_breakdown.get("recency_bonus", 0.0))
        recency_tag_bonus = float(candidate.score_breakdown.get("recency_tag_bonus", 0.0))
        genre_match = float(candidate.score_breakdown.get("genre_match", 0.0))
        tag_match = float(candidate.score_breakdown.get("tag_match", 0.0))
        popularity = float(candidate.score_breakdown.get("popularity", 0.0))
        rating = float(candidate.score_breakdown.get("rating", 0.0))

        phase_fit = _clamp_unit((phase_genre_bonus * 0.45) + (phase_tag_bonus * 0.55))
        hot_recency_base = _clamp_unit((recency_bonus * 0.35) + (recency_tag_bonus * 0.65))
        genre_overlap_ratio = (
            min(phase_genre_bonus, recency_bonus)
            / max(phase_genre_bonus, recency_bonus, 1e-6)
            if (phase_genre_bonus > 0.0 or recency_bonus > 0.0)
            else 0.0
        )
        tag_overlap_ratio = (
            min(phase_tag_bonus, recency_tag_bonus)
            / max(phase_tag_bonus, recency_tag_bonus, 1e-6)
            if (phase_tag_bonus > 0.0 or recency_tag_bonus > 0.0)
            else 0.0
        )
        hot_recency_incremental = _clamp_unit(
            (max(0.0, recency_bonus - phase_genre_bonus) * 0.35)
            + (max(0.0, recency_tag_bonus - phase_tag_bonus) * 0.65),
        )
        phase_hot_overlap_ratio = _clamp_unit(
            (genre_overlap_ratio * 0.4) + (tag_overlap_ratio * 0.6),
        )
        hot_recency_specificity = _clamp_unit(
            hot_recency_incremental * (1.0 - phase_hot_overlap_ratio),
        )
        hot_recency = _clamp_unit(
            (hot_recency_specificity ** COMFORT_HOT_RECENCY_SELECTIVE_EXPONENT)
            * hot_recency_mode_multiplier,
        )

        phase_family_contribution = phase_fit * TOP_PICKS_PHASE_WEIGHT
        hot_recency_contribution = hot_recency * TOP_PICKS_HOT_RECENCY_WEIGHT
        rating_contribution = rating * TOP_PICKS_RATING_WEIGHT
        rewatch_contribution = 0.0
        background_contribution = (
            (genre_match * TOP_PICKS_GENRE_WEIGHT)
            + (tag_match * TOP_PICKS_TAG_WEIGHT)
            + (popularity * TOP_PICKS_POPULARITY_WEIGHT)
        )
        top_picks_score = _clamp_unit(
            phase_family_contribution
            + hot_recency_contribution
            + rating_contribution
            + background_contribution,
        )

        candidate.score_breakdown["phase_fit"] = round(phase_fit, 6)
        candidate.score_breakdown["hot_recency_base"] = round(hot_recency_base, 6)
        candidate.score_breakdown["genre_overlap_ratio"] = round(genre_overlap_ratio, 6)
        candidate.score_breakdown["tag_overlap_ratio"] = round(tag_overlap_ratio, 6)
        candidate.score_breakdown["hot_recency_incremental"] = round(hot_recency_incremental, 6)
        candidate.score_breakdown["hot_recency_specificity"] = round(hot_recency_specificity, 6)
        candidate.score_breakdown["phase_hot_overlap_ratio"] = round(phase_hot_overlap_ratio, 6)
        candidate.score_breakdown["hot_recency"] = round(hot_recency, 6)
        candidate.score_breakdown["tag_signal_mode"] = tag_signal_mode
        candidate.score_breakdown["hot_recency_mode_multiplier"] = round(
            hot_recency_mode_multiplier,
            6,
        )
        candidate.score_breakdown["candidate_tag_coverage_pool"] = round(
            candidate_tag_coverage_pool,
            6,
        )
        candidate.score_breakdown["recent_history_tag_coverage"] = round(
            recent_history_tag_coverage,
            6,
        )
        candidate.score_breakdown["candidate_has_tags"] = 1.0 if candidate.tags else 0.0
        candidate.score_breakdown["phase_family_contribution"] = round(phase_family_contribution, 6)
        candidate.score_breakdown["hot_recency_contribution"] = round(hot_recency_contribution, 6)
        candidate.score_breakdown["rating_contribution"] = round(rating_contribution, 6)
        candidate.score_breakdown["rewatch_contribution"] = 0.0
        candidate.score_breakdown["rewatch_bonus"] = 0.0
        candidate.score_breakdown["rewatch_gate"] = 0.0
        candidate.score_breakdown["rating_confidence"] = round(rating, 6)
        candidate.score_breakdown["inactivity_norm"] = 0.0
        candidate.score_breakdown["phase_evidence"] = round(
            float(candidate.score_breakdown.get("phase_evidence", 0.0)),
            6,
        )
        candidate.score_breakdown["seasonal_adjustment"] = 0.0
        candidate.score_breakdown["seasonality_dampener_contribution"] = 0.0
        candidate.score_breakdown["diversity_dampener_contribution"] = 0.0
        candidate.score_breakdown["era_dampener_contribution"] = 0.0
        candidate.score_breakdown["opening_era_dampener_contribution"] = 0.0
        candidate.score_breakdown["dampeners_contribution"] = 0.0
        candidate.score_breakdown["background_contribution"] = round(background_contribution, 6)
        candidate.score_breakdown["comfort_score"] = round(top_picks_score, 6)
        candidate.final_score = round(top_picks_score, 6)

        if phase_top:
            cand_genres = {g.strip().lower() for g in (candidate.genres or []) if g}
            overlap = [g for g in phase_top if g in cand_genres]
            if overlap:
                candidate.score_breakdown["match_genres"] = ", ".join(
                    g.title() for g in overlap[:3]
                )

    candidates.sort(
        key=lambda candidate: (
            candidate.final_score if candidate.final_score is not None else -1.0,
            float(candidate.score_breakdown.get("phase_fit", -1.0)),
            float(candidate.score_breakdown.get("hot_recency", -1.0)),
            float(candidate.score_breakdown.get("genre_match", -1.0)),
        ),
        reverse=True,
    )
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


def _is_holiday_window(today=None) -> bool:
    if today is None:
        today = timezone.localdate()
    month_day = (today.month, today.day)
    return month_day >= (11, 15) or month_day <= (1, 10)


def _holiday_seasonal_strength(candidate: CandidateItem) -> float:
    values = [
        *(candidate.tags or []),
        *(candidate.genres or []),
        candidate.title,
        candidate.original_title,
        candidate.localized_title,
    ]
    strength = 0.0
    for value in values:
        if not value:
            continue
        key = str(value).strip().lower()
        if not key:
            continue
        if any(term in key for term in HOLIDAY_STRONG_TERMS):
            strength = max(strength, 1.0)
            continue
        if any(term in key for term in HOLIDAY_SOFT_TERMS):
            strength = max(strength, 0.7)
    return strength


def _comfort_bucket_key(candidate: CandidateItem) -> str:
    tags = sorted(
        str(tag).strip().lower()
        for tag in (candidate.tags or [])
        if str(tag).strip()
    )
    if tags:
        return f"tag:{tags[0]}"

    genres = sorted(
        str(genre).strip().lower()
        for genre in (candidate.genres or [])
        if str(genre).strip()
    )
    if genres:
        return f"genre:{genres[0]}"
    return "other"


def _top_affinity_keys(values: dict[str, float], *, limit: int = 5) -> set[str]:
    if not values:
        return set()
    ranked = sorted(
        (
            (str(key).strip().lower(), float(value))
            for key, value in values.items()
            if str(key).strip()
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    return {key for key, _ in ranked[:limit]}


def _phase_affinity_maps(profile_payload: dict | None) -> tuple[dict[str, float], dict[str, float]]:
    profile = profile_payload or {}
    phase_genre_affinity = {
        str(key).strip().lower(): float(value)
        for key, value in (
            profile.get("phase_genre_affinity")
            or profile.get("recent_genre_affinity")
            or {}
        ).items()
        if str(key).strip()
    }
    phase_tag_affinity = {
        str(key).strip().lower(): float(value)
        for key, value in (
            profile.get("phase_tag_affinity")
            or profile.get("recent_tag_affinity")
            or {}
        ).items()
        if str(key).strip()
    }
    return phase_genre_affinity, phase_tag_affinity


def _entry_phase_evidence(
    entry,
    *,
    tag_map: dict[int, list[str]],
    phase_genre_affinity: dict[str, float],
    phase_tag_affinity: dict[str, float],
) -> float:
    if not phase_genre_affinity and not phase_tag_affinity:
        return 0.0

    genres = [
        str(genre).strip().lower()
        for genre in (entry.item.genres or [])
        if str(genre).strip()
    ]
    tags = [
        str(tag).strip().lower()
        for tag in (tag_map.get(entry.item_id, []) or [])
        if str(tag).strip()
    ]
    best_genre = max((phase_genre_affinity.get(genre, 0.0) for genre in genres), default=0.0)
    best_tag = max((phase_tag_affinity.get(tag, 0.0) for tag in tags), default=0.0)
    return _clamp_unit((best_genre * 0.45) + (best_tag * 0.55))


def _candidate_release_year(candidate: CandidateItem) -> int | None:
    if not candidate.release_date:
        return None
    value = str(candidate.release_date).strip()
    if len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    for token in value.split():
        cleaned = token.strip(",.")
        if len(cleaned) == 4 and cleaned.isdigit():
            return int(cleaned)
    return None


def _format_phase_label(value: str) -> str:
    key = str(value).strip().lower()
    if not key:
        return ""
    if key in PHASE_LABEL_OVERRIDES:
        return PHASE_LABEL_OVERRIDES[key]
    return " ".join(part.capitalize() for part in key.replace("_", " ").split())


def _top_phase_labels(
    affinity: dict[str, float],
    *,
    limit: int,
    allow_generic_terms: bool,
) -> list[str]:
    if not affinity:
        return []

    labels: list[str] = []
    seen: set[str] = set()
    ranked = sorted(
        (
            (str(raw_key).strip().lower(), float(raw_score))
            for raw_key, raw_score in affinity.items()
            if str(raw_key).strip()
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    for key, _score in ranked:
        if not allow_generic_terms and key in GENERIC_PHASE_TERMS:
            continue
        label = _format_phase_label(key)
        if not label:
            continue
        normalized_label = label.strip().lower()
        if normalized_label in seen:
            continue
        seen.add(normalized_label)
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def _phase_pool_source(candidate: CandidateItem) -> str:
    if float(candidate.score_breakdown.get("phase_pool_strong", 0.0)) >= 1.0:
        return "strong_phase"
    if float(candidate.score_breakdown.get("phase_pool_medium", 0.0)) >= 1.0:
        return "medium_phase"
    if float(candidate.score_breakdown.get("phase_pool_backfill", 0.0)) >= 1.0:
        return "weak_backfill"
    if float(candidate.score_breakdown.get("phase_pool_weak_only", 0.0)) >= 1.0:
        return "weak_only"
    return "unknown"


def _is_phase_lane_candidate(
    candidate: CandidateItem,
    *,
    phase_genres: set[str],
    phase_tags: set[str],
) -> bool:
    candidate_tags = {
        str(tag).strip().lower()
        for tag in (candidate.tags or [])
        if str(tag).strip()
    }
    if phase_tags and candidate_tags.intersection(phase_tags):
        return True

    candidate_genres = {
        str(genre).strip().lower()
        for genre in (candidate.genres or [])
        if str(genre).strip()
    }
    return bool(phase_genres and candidate_genres.intersection(phase_genres))


def _promote_phase_lane_candidates(
    candidates: list[CandidateItem],
    *,
    phase_genre_affinity: dict[str, float],
    phase_tag_affinity: dict[str, float],
    quota: int = COMFORT_PHASE_LANE_QUOTA,
    window: int = COMFORT_PHASE_LANE_WINDOW,
) -> list[CandidateItem]:
    if not candidates or quota <= 0:
        return candidates

    window = min(window, len(candidates))
    phase_genres = _top_affinity_keys(phase_genre_affinity, limit=5)
    phase_tags = _top_affinity_keys(phase_tag_affinity, limit=5)
    if not phase_genres and not phase_tags:
        return candidates

    def is_phase_lane(item: CandidateItem) -> bool:
        return _is_phase_lane_candidate(
            item,
            phase_genres=phase_genres,
            phase_tags=phase_tags,
        )

    top_slice = candidates[:window]
    phase_in_top = [item for item in top_slice if is_phase_lane(item)]
    if len(phase_in_top) >= quota:
        return candidates

    needed = quota - len(phase_in_top)
    replacement_indices = [idx for idx, item in enumerate(top_slice) if not is_phase_lane(item)]
    if not replacement_indices:
        return candidates
    replacement_indices = replacement_indices[-needed:]

    promotable: list[CandidateItem] = []
    for item in candidates[window:]:
        if is_phase_lane(item):
            promotable.append(item)
            if len(promotable) >= len(replacement_indices):
                break
    if not promotable:
        return candidates

    for promoted, target_idx in zip(promotable, replacement_indices):
        current_idx = next((idx for idx, item in enumerate(candidates) if item is promoted), None)
        if current_idx is None or current_idx <= target_idx:
            continue
        displaced = candidates[target_idx]
        candidates.pop(current_idx)
        candidates.insert(target_idx, promoted)
        promoted.score_breakdown["phase_lane_promoted"] = 1.0
        displaced.score_breakdown["phase_lane_demoted"] = 1.0

    return candidates


def _prefer_strong_phase_opening_window(
    candidates: list[CandidateItem],
    *,
    opening_window: int = COMFORT_STRONG_PHASE_OPENING_WINDOW,
    superiority_margin: float = COMFORT_MEDIUM_PHASE_SUPERIORITY_MARGIN,
) -> list[CandidateItem]:
    """Prefer strong-phase pool entries in opening slots unless medium is clearly better."""
    if not candidates or opening_window <= 0:
        return candidates

    opening_window = min(opening_window, len(candidates))
    used_strong_indices: set[int] = set()
    for index in range(opening_window):
        candidate = candidates[index]
        is_medium = float(candidate.score_breakdown.get("phase_pool_medium", 0.0)) >= 1.0
        if not is_medium:
            continue

        replacement_idx = None
        for down_idx in range(opening_window, len(candidates)):
            if down_idx in used_strong_indices:
                continue
            if float(candidates[down_idx].score_breakdown.get("phase_pool_strong", 0.0)) >= 1.0:
                replacement_idx = down_idx
                break
        if replacement_idx is None:
            continue

        medium_score = float(candidate.final_score or 0.0)
        strong_score = float(candidates[replacement_idx].final_score or 0.0)
        if medium_score >= strong_score + superiority_margin:
            candidate.score_breakdown["medium_phase_held_opening"] = 1.0
            used_strong_indices.add(replacement_idx)
            continue

        candidates[index], candidates[replacement_idx] = (
            candidates[replacement_idx],
            candidates[index],
        )
        candidates[index].score_breakdown["strong_phase_promoted_opening"] = 1.0
        candidates[replacement_idx].score_breakdown["medium_phase_demoted_opening"] = 1.0
        used_strong_indices.add(replacement_idx)

    return candidates


def _apply_comfort_confidence(
    candidates: list[CandidateItem],
    profile_payload: dict | None = None,
) -> list[CandidateItem]:
    if not candidates:
        return candidates

    phase_genre_affinity, phase_tag_affinity = _phase_affinity_maps(profile_payload)
    phase_affinity = phase_genre_affinity
    phase_top = sorted(phase_affinity, key=phase_affinity.get, reverse=True)[:5]
    holiday_window_active = _is_holiday_window()
    candidate_tag_coverage_pool = _clamp_unit(
        sum(1 for candidate in candidates if candidate.tags) / max(1, len(candidates)),
    )
    history_tag_coverages = [
        float(candidate.score_breakdown.get("recent_history_tag_coverage", 0.0))
        for candidate in candidates
        if "recent_history_tag_coverage" in candidate.score_breakdown
    ]
    recent_history_tag_coverage = _clamp_unit(
        (sum(history_tag_coverages) / len(history_tag_coverages))
        if history_tag_coverages
        else 0.0,
    )
    tag_signal_mode = (
        "tag_rich"
        if (
            candidate_tag_coverage_pool >= COMFORT_TAG_RICH_CANDIDATE_COVERAGE_THRESHOLD
            and recent_history_tag_coverage >= COMFORT_TAG_RICH_HISTORY_COVERAGE_THRESHOLD
        )
        else "tag_sparse"
    )
    hot_recency_mode_multiplier = (
        1.0 if tag_signal_mode == "tag_rich" else COMFORT_HOT_RECENCY_TAG_SPARSE_MULTIPLIER
    )

    for candidate in candidates:
        phase_genre_bonus = float(
            candidate.score_breakdown.get("phase_genre_bonus", 0.0),
        )
        phase_tag_bonus = float(
            candidate.score_breakdown.get("phase_tag_bonus", 0.0),
        )
        recency_bonus = float(
            candidate.score_breakdown.get("recency_bonus", 0.0),
        )
        recency_tag_bonus = float(
            candidate.score_breakdown.get("recency_tag_bonus", 0.0),
        )
        genre_match = float(
            candidate.score_breakdown.get("genre_match", 0.0),
        )
        tag_match = float(
            candidate.score_breakdown.get("tag_match", 0.0),
        )
        phase_fit = _clamp_unit((phase_genre_bonus * 0.45) + (phase_tag_bonus * 0.55))
        hot_recency_base = _clamp_unit((recency_bonus * 0.35) + (recency_tag_bonus * 0.65))
        genre_overlap_ratio = (
            min(phase_genre_bonus, recency_bonus)
            / max(phase_genre_bonus, recency_bonus, 1e-6)
            if (phase_genre_bonus > 0.0 or recency_bonus > 0.0)
            else 0.0
        )
        tag_overlap_ratio = (
            min(phase_tag_bonus, recency_tag_bonus)
            / max(phase_tag_bonus, recency_tag_bonus, 1e-6)
            if (phase_tag_bonus > 0.0 or recency_tag_bonus > 0.0)
            else 0.0
        )
        hot_recency_incremental = _clamp_unit(
            (max(0.0, recency_bonus - phase_genre_bonus) * 0.35)
            + (max(0.0, recency_tag_bonus - phase_tag_bonus) * 0.65),
        )
        phase_hot_overlap_ratio = _clamp_unit(
            (genre_overlap_ratio * 0.4) + (tag_overlap_ratio * 0.6),
        )
        hot_recency_specificity = _clamp_unit(
            hot_recency_incremental * (1.0 - phase_hot_overlap_ratio),
        )
        hot_recency = _clamp_unit(
            (hot_recency_specificity ** COMFORT_HOT_RECENCY_SELECTIVE_EXPONENT)
            * hot_recency_mode_multiplier,
        )

        user_score = candidate.score_breakdown.get("user_score")
        if user_score is not None:
            rating_confidence = _clamp_unit(
                1.0 - (0.5 ** ((float(user_score) - 5.0) / 2.5)),
            )
        else:
            rating_confidence = 0.5
        phase_evidence = float(
            candidate.score_breakdown.get("phase_evidence", 0.0),
        )
        rewatch_count = int(float(candidate.score_breakdown.get("rewatch_count", 1.0)))
        raw_rewatch_bonus = _clamp_unit(
            math.log1p(rewatch_count - 1) / math.log(8)
            if rewatch_count > 1
            else 0.0
        )
        rewatch_gate = _clamp_unit(0.25 + (phase_fit * 0.75))
        rewatch_bonus = (
            min(0.85, raw_rewatch_bonus) * rewatch_gate
        )
        inactivity_days = float(
            candidate.score_breakdown.get("days_since_activity", 0.0),
        )
        # 730-day window: items under ~1 year score low, 2+ years score high.
        # A strong genre match to recent watches can still overcome a
        # shorter gap, but all else being equal longer-dormant favourites
        # surface first.
        inactivity_norm = _clamp_unit(inactivity_days / 730.0)
        holiday_strength = _holiday_seasonal_strength(candidate)
        seasonal_adjustment = 0.0
        if holiday_strength > 0.0:
            seasonal_adjustment = (
                0.06 * holiday_strength
                if holiday_window_active
                else -0.14 * holiday_strength
            )

        phase_family_contribution = (
            (phase_fit * 0.32)
            + (phase_evidence * 0.10)
        )
        hot_recency_contribution = hot_recency * 0.17
        rewatch_contribution = rewatch_bonus * 0.08
        rating_contribution = rating_confidence * 0.09
        background_contribution = (
            (inactivity_norm * 0.11)
            + (genre_match * 0.03)
            + (tag_match * 0.10)
        )
        comfort_score = _clamp_unit(
            phase_family_contribution
            + hot_recency_contribution
            + rewatch_contribution
            + rating_contribution
            + background_contribution
            + seasonal_adjustment,
        )

        candidate.score_breakdown["phase_fit"] = round(phase_fit, 6)
        candidate.score_breakdown["hot_recency_base"] = round(hot_recency_base, 6)
        candidate.score_breakdown["genre_overlap_ratio"] = round(genre_overlap_ratio, 6)
        candidate.score_breakdown["tag_overlap_ratio"] = round(tag_overlap_ratio, 6)
        candidate.score_breakdown["hot_recency_incremental"] = round(hot_recency_incremental, 6)
        candidate.score_breakdown["hot_recency_specificity"] = round(hot_recency_specificity, 6)
        candidate.score_breakdown["phase_hot_overlap_ratio"] = round(phase_hot_overlap_ratio, 6)
        candidate.score_breakdown["hot_recency"] = round(hot_recency, 6)
        candidate.score_breakdown["tag_signal_mode"] = tag_signal_mode
        candidate.score_breakdown["hot_recency_mode_multiplier"] = round(
            hot_recency_mode_multiplier,
            6,
        )
        candidate.score_breakdown["candidate_tag_coverage_pool"] = round(
            candidate_tag_coverage_pool,
            6,
        )
        candidate.score_breakdown["recent_history_tag_coverage"] = round(
            recent_history_tag_coverage,
            6,
        )
        candidate.score_breakdown["candidate_has_tags"] = 1.0 if candidate.tags else 0.0
        candidate.score_breakdown["rewatch_gate"] = round(rewatch_gate, 6)
        candidate.score_breakdown["rewatch_bonus"] = round(rewatch_bonus, 6)
        candidate.score_breakdown["rating_confidence"] = round(rating_confidence, 6)
        candidate.score_breakdown["inactivity_norm"] = round(inactivity_norm, 6)
        candidate.score_breakdown["phase_evidence"] = round(phase_evidence, 6)
        candidate.score_breakdown["holiday_strength"] = round(holiday_strength, 6)
        candidate.score_breakdown["seasonal_adjustment"] = round(seasonal_adjustment, 6)
        candidate.score_breakdown["phase_family_contribution"] = round(phase_family_contribution, 6)
        candidate.score_breakdown["hot_recency_contribution"] = round(hot_recency_contribution, 6)
        candidate.score_breakdown["rating_contribution"] = round(rating_contribution, 6)
        candidate.score_breakdown["rewatch_contribution"] = round(rewatch_contribution, 6)
        candidate.score_breakdown["background_contribution"] = round(background_contribution, 6)
        candidate.score_breakdown["dampeners_contribution"] = round(seasonal_adjustment, 6)
        candidate.score_breakdown["diversity_penalty_contribution"] = 0.0
        candidate.score_breakdown["diversity_dampener_contribution"] = 0.0
        candidate.score_breakdown["era_penalty_contribution"] = 0.0
        candidate.score_breakdown["era_base_penalty_contribution"] = 0.0
        candidate.score_breakdown["era_opening_penalty_contribution"] = 0.0
        candidate.score_breakdown["seasonality_dampener_contribution"] = round(
            seasonal_adjustment,
            6,
        )
        candidate.score_breakdown["era_dampener_contribution"] = 0.0
        candidate.score_breakdown["opening_era_dampener_contribution"] = 0.0
        candidate.score_breakdown["comfort_score"] = round(comfort_score, 6)
        candidate.final_score = round(comfort_score, 6)

        # Per-card match genres: overlap of candidate genres with phase activity
        if phase_top:
            cand_genres = {
                g.strip().lower() for g in (candidate.genres or []) if g
            }
            overlap = [g for g in phase_top if g in cand_genres]
            if overlap:
                candidate.score_breakdown["match_genres"] = ", ".join(
                    g.title() for g in overlap[:3]
                )

    candidates.sort(
        key=lambda candidate: (
            candidate.final_score if candidate.final_score is not None else -1.0,
            float(candidate.score_breakdown.get("phase_fit", -1.0)),
            float(candidate.score_breakdown.get("hot_recency", -1.0)),
            float(candidate.score_breakdown.get("rewatch_bonus", -1.0)),
        ),
        reverse=True,
    )

    bucket_counts: dict[str, int] = {}
    for candidate in candidates:
        bucket_key = _comfort_bucket_key(candidate)
        bucket_seen_count = bucket_counts.get(bucket_key, 0)
        diversity_multiplier = COMFORT_DIVERSITY_DECAY ** bucket_seen_count
        before_diversity = float(candidate.final_score or 0.0)
        diversified_score = _clamp_unit(
            before_diversity * diversity_multiplier,
        )
        diversity_penalty_contribution = max(0.0, before_diversity - diversified_score)
        candidate.score_breakdown["diversity_multiplier"] = round(diversity_multiplier, 6)
        candidate.score_breakdown["diversity_penalty"] = round(1.0 - diversity_multiplier, 6)
        candidate.score_breakdown["diversity_penalty_contribution"] = round(
            diversity_penalty_contribution,
            6,
        )
        candidate.score_breakdown["diversity_dampener_contribution"] = round(
            -diversity_penalty_contribution,
            6,
        )
        candidate.final_score = round(diversified_score, 6)
        bucket_counts[bucket_key] = bucket_seen_count + 1

    candidates.sort(
        key=lambda candidate: (
            candidate.final_score if candidate.final_score is not None else -1.0,
            float(candidate.score_breakdown.get("phase_fit", -1.0)),
            float(candidate.score_breakdown.get("hot_recency", -1.0)),
            float(candidate.score_breakdown.get("rewatch_bonus", -1.0)),
        ),
        reverse=True,
    )
    era_counts: dict[int, int] = {}
    legacy_count = 0
    for index, candidate in enumerate(candidates[: MAX_ITEMS_PER_ROW * 2]):
        candidate.score_breakdown["era_opening_slot"] = 1.0 if index < COMFORT_ERA_OPENING_WINDOW else 0.0
        candidate.score_breakdown["era_opening_multiplier"] = 1.0
        candidate.score_breakdown["era_base_multiplier"] = 1.0
        candidate.score_breakdown.setdefault("era_multiplier", 1.0)
        candidate.score_breakdown.setdefault("era_penalty_contribution", 0.0)
        candidate.score_breakdown["era_base_penalty_contribution"] = 0.0
        candidate.score_breakdown["era_opening_penalty_contribution"] = 0.0
        candidate.score_breakdown["era_dampener_contribution"] = 0.0
        candidate.score_breakdown["opening_era_dampener_contribution"] = 0.0
        release_year = _candidate_release_year(candidate)
        if release_year is None:
            continue
        era_bucket = (release_year // 10) * 10
        era_seen_count = era_counts.get(era_bucket, 0)
        base_multiplier = COMFORT_ERA_DECAY ** era_seen_count
        if index < COMFORT_ERA_OPENING_WINDOW:
            opening_multiplier = COMFORT_ERA_OPENING_DECAY ** era_seen_count
        else:
            opening_multiplier = 1.0
        if release_year < 2000:
            base_multiplier *= COMFORT_LEGACY_ERA_DECAY ** legacy_count
            if index < COMFORT_ERA_OPENING_WINDOW:
                opening_multiplier *= COMFORT_LEGACY_OPENING_DECAY ** legacy_count
            legacy_count += 1
        era_multiplier = base_multiplier * opening_multiplier
        before_era = float(candidate.final_score or 0.0)
        after_base = _clamp_unit(before_era * base_multiplier)
        era_base_penalty = max(0.0, before_era - after_base)
        era_adjusted_score = _clamp_unit(after_base * opening_multiplier)
        opening_era_penalty = max(0.0, after_base - era_adjusted_score)
        era_penalty_contribution = era_base_penalty + opening_era_penalty
        candidate.score_breakdown["era_multiplier"] = round(era_multiplier, 6)
        candidate.score_breakdown["era_base_multiplier"] = round(base_multiplier, 6)
        candidate.score_breakdown["era_opening_multiplier"] = round(opening_multiplier, 6)
        candidate.score_breakdown["era_penalty_contribution"] = round(era_penalty_contribution, 6)
        candidate.score_breakdown["era_base_penalty_contribution"] = round(
            era_base_penalty,
            6,
        )
        candidate.score_breakdown["era_opening_penalty_contribution"] = round(
            opening_era_penalty,
            6,
        )
        candidate.score_breakdown["era_dampener_contribution"] = round(
            -era_base_penalty,
            6,
        )
        candidate.score_breakdown["opening_era_dampener_contribution"] = round(
            -opening_era_penalty,
            6,
        )
        candidate.score_breakdown["era_bucket"] = float(era_bucket)
        candidate.final_score = round(era_adjusted_score, 6)
        era_counts[era_bucket] = era_seen_count + 1

    for candidate in candidates:
        seasonality_dampener_contribution = float(
            candidate.score_breakdown.get("seasonality_dampener_contribution", 0.0),
        )
        diversity_dampener_contribution = float(
            candidate.score_breakdown.get("diversity_dampener_contribution", 0.0),
        )
        era_dampener_contribution = float(
            candidate.score_breakdown.get("era_dampener_contribution", 0.0),
        )
        opening_era_dampener_contribution = float(
            candidate.score_breakdown.get("opening_era_dampener_contribution", 0.0),
        )
        dampeners_contribution = (
            seasonality_dampener_contribution
            + diversity_dampener_contribution
            + era_dampener_contribution
            + opening_era_dampener_contribution
        )
        candidate.score_breakdown["dampeners_contribution"] = round(
            dampeners_contribution,
            6,
        )

    candidates.sort(
        key=lambda candidate: (
            candidate.final_score if candidate.final_score is not None else -1.0,
            float(candidate.score_breakdown.get("phase_fit", -1.0)),
            float(candidate.score_breakdown.get("hot_recency", -1.0)),
            float(candidate.score_breakdown.get("rewatch_bonus", -1.0)),
        ),
        reverse=True,
    )
    _promote_phase_lane_candidates(
        candidates,
        phase_genre_affinity=phase_genre_affinity,
        phase_tag_affinity=phase_tag_affinity,
    )
    _prefer_strong_phase_opening_window(candidates)
    _calibrate_comfort_display_scores(candidates)
    return candidates


def _calibrate_comfort_display_scores(candidates: list[CandidateItem]) -> list[CandidateItem]:
    if not candidates:
        return candidates

    raw_scores = [_clamp_unit(float(candidate.final_score or 0.0)) for candidate in candidates]
    min_raw = min(raw_scores)
    max_raw = max(raw_scores)
    spread = max_raw - min_raw
    rank_denom = max(1, len(candidates) - 1)

    calibrated: list[float] = []
    for index, candidate in enumerate(candidates):
        raw_score = _clamp_unit(float(candidate.final_score or 0.0))
        spread_norm = (
            (raw_score - min_raw) / spread
            if spread > 0.0
            else 0.5
        )
        rank_norm = 1.0 - (index / rank_denom)
        display_score = _clamp_unit(
            0.58
            + (raw_score * 0.22)
            + (spread_norm * 0.12)
            + (rank_norm * 0.08),
        )
        candidate.score_breakdown["raw_final_score"] = round(raw_score, 6)
        candidate.score_breakdown["display_spread_norm"] = round(spread_norm, 6)
        candidate.score_breakdown["display_rank_norm"] = round(rank_norm, 6)
        candidate.score_breakdown["display_pre_monotonic"] = round(display_score, 6)
        calibrated.append(display_score)

    # Ensure top-to-bottom confidence never increases, while preserving ranking order.
    ceiling = 1.0
    for index, candidate in enumerate(candidates):
        monotonic_score = min(ceiling, calibrated[index])
        candidate.display_score = round(monotonic_score, 6)
        candidate.score_breakdown["display_score"] = round(monotonic_score, 6)
        ceiling = monotonic_score

    return candidates


def _build_comfort_debug_payload(
    candidates: list[CandidateItem],
    *,
    top_n: int = COMFORT_DEBUG_TOP_N,
) -> dict:
    if not candidates:
        return {
            "top_n": top_n,
            "top_candidates": [],
            "score_distribution": {
                "raw_min": 0.0,
                "raw_max": 0.0,
                "raw_spread": 0.0,
                "display_min": 0.0,
                "display_max": 0.0,
                "display_spread": 0.0,
                "compressed_raw": True,
            },
            "penalty_stack": {
                "multi_penalty_count": 0,
                "multi_penalty_media_ids": [],
            },
            "contribution_totals": {
                "phase_family": 0.0,
                "hot_recency": 0.0,
                "rating": 0.0,
                "rewatch": 0.0,
                "dampeners": 0.0,
            },
            "dampener_totals": {
                "seasonality": 0.0,
                "diversity": 0.0,
                "era": 0.0,
                "opening_era": 0.0,
            },
            "tag_signal": {
                "mode": "tag_sparse",
                "candidate_tag_coverage_top_n": 0.0,
                "candidate_tag_coverage_pool": 0.0,
                "recent_history_tag_coverage": 0.0,
                "recent_history_window_days": COMFORT_RECENT_HISTORY_TAG_WINDOW_DAYS,
                "hot_recency_mode_multiplier": COMFORT_HOT_RECENCY_TAG_SPARSE_MULTIPLIER,
            },
        }

    raw_scores = [
        _clamp_unit(
            float(
                candidate.score_breakdown.get("raw_final_score", candidate.final_score or 0.0),
            ),
        )
        for candidate in candidates
    ]
    display_scores = [
        _clamp_unit(float(candidate.display_score or 0.0))
        for candidate in candidates
    ]

    raw_min = min(raw_scores)
    raw_max = max(raw_scores)
    raw_spread = raw_max - raw_min
    display_min = min(display_scores)
    display_max = max(display_scores)
    display_spread = display_max - display_min

    effective_top_n = min(len(candidates), max(1, top_n))
    top_slice = candidates[:effective_top_n]
    top_tagged_count = sum(1 for candidate in top_slice if candidate.tags)
    pool_tagged_count = sum(1 for candidate in candidates if candidate.tags)
    recent_history_coverages = [
        float(candidate.score_breakdown.get("recent_history_tag_coverage", 0.0))
        for candidate in candidates
        if "recent_history_tag_coverage" in candidate.score_breakdown
    ]
    recent_history_tag_coverage = _clamp_unit(
        (sum(recent_history_coverages) / len(recent_history_coverages))
        if recent_history_coverages
        else 0.0,
    )
    tag_signal_mode = str(candidates[0].score_breakdown.get("tag_signal_mode", "tag_sparse"))
    hot_recency_mode_multiplier = _clamp_unit(
        float(
            candidates[0].score_breakdown.get(
                "hot_recency_mode_multiplier",
                COMFORT_HOT_RECENCY_TAG_SPARSE_MULTIPLIER,
            ),
        ),
    )

    multi_penalty_ids: list[str] = []
    top_candidates: list[dict] = []
    contribution_totals = {
        "phase_family": 0.0,
        "hot_recency": 0.0,
        "rating": 0.0,
        "rewatch": 0.0,
        "dampeners": 0.0,
    }
    dampener_totals = {
        "seasonality": 0.0,
        "diversity": 0.0,
        "era": 0.0,
        "opening_era": 0.0,
    }
    for index, candidate in enumerate(top_slice, start=1):
        score = candidate.score_breakdown
        seasonal_adjustment = float(score.get("seasonal_adjustment", 0.0))
        diversity_multiplier = float(score.get("diversity_multiplier", 1.0))
        era_multiplier = float(score.get("era_multiplier", 1.0))
        phase_family_contribution = float(score.get("phase_family_contribution", 0.0))
        hot_recency_contribution = float(score.get("hot_recency_contribution", 0.0))
        rating_contribution = float(score.get("rating_contribution", 0.0))
        rewatch_contribution = float(score.get("rewatch_contribution", 0.0))
        dampeners_contribution = float(score.get("dampeners_contribution", 0.0))
        seasonality_dampener_contribution = float(
            score.get("seasonality_dampener_contribution", 0.0),
        )
        diversity_dampener_contribution = float(
            score.get("diversity_dampener_contribution", 0.0),
        )
        era_dampener_contribution = float(
            score.get("era_dampener_contribution", 0.0),
        )
        opening_era_dampener_contribution = float(
            score.get("opening_era_dampener_contribution", 0.0),
        )
        phase_pool_source = _phase_pool_source(candidate)
        phase_lane_promoted = float(score.get("phase_lane_promoted", 0.0)) >= 1.0

        contribution_totals["phase_family"] += phase_family_contribution
        contribution_totals["hot_recency"] += hot_recency_contribution
        contribution_totals["rating"] += rating_contribution
        contribution_totals["rewatch"] += rewatch_contribution
        contribution_totals["dampeners"] += dampeners_contribution
        dampener_totals["seasonality"] += seasonality_dampener_contribution
        dampener_totals["diversity"] += diversity_dampener_contribution
        dampener_totals["era"] += era_dampener_contribution
        dampener_totals["opening_era"] += opening_era_dampener_contribution

        penalty_count = 0
        if seasonal_adjustment < 0.0:
            penalty_count += 1
        if diversity_multiplier < 0.999:
            penalty_count += 1
        if era_multiplier < 0.999:
            penalty_count += 1
        if phase_pool_source in {"weak_backfill", "weak_only"}:
            penalty_count += 1

        if penalty_count >= 2:
            multi_penalty_ids.append(str(candidate.media_id))

        top_candidates.append(
            {
                "rank": index,
                "media_id": str(candidate.media_id),
                "title": candidate.title,
                "raw_final_score": round(
                    _clamp_unit(float(score.get("raw_final_score", candidate.final_score or 0.0))),
                    6,
                ),
                "display_score": round(_clamp_unit(float(candidate.display_score or 0.0)), 6),
                "phase_fit": round(float(score.get("phase_fit", 0.0)), 6),
                "hot_recency_base": round(float(score.get("hot_recency_base", 0.0)), 6),
                "genre_overlap_ratio": round(float(score.get("genre_overlap_ratio", 0.0)), 6),
                "tag_overlap_ratio": round(float(score.get("tag_overlap_ratio", 0.0)), 6),
                "hot_recency_incremental": round(float(score.get("hot_recency_incremental", 0.0)), 6),
                "hot_recency_specificity": round(float(score.get("hot_recency_specificity", 0.0)), 6),
                "phase_hot_overlap_ratio": round(float(score.get("phase_hot_overlap_ratio", 0.0)), 6),
                "hot_recency": round(float(score.get("hot_recency", 0.0)), 6),
                "phase_evidence": round(float(score.get("phase_evidence", 0.0)), 6),
                "tag_signal_mode": str(score.get("tag_signal_mode", "tag_sparse")),
                "hot_recency_mode_multiplier": round(
                    float(score.get("hot_recency_mode_multiplier", 0.0)),
                    6,
                ),
                "candidate_tag_coverage_pool": round(
                    float(score.get("candidate_tag_coverage_pool", 0.0)),
                    6,
                ),
                "recent_history_tag_coverage": round(
                    float(score.get("recent_history_tag_coverage", 0.0)),
                    6,
                ),
                "rating_confidence": round(float(score.get("rating_confidence", 0.0)), 6),
                "rewatch_bonus": round(float(score.get("rewatch_bonus", 0.0)), 6),
                "phase_family_contribution": round(phase_family_contribution, 6),
                "hot_recency_contribution": round(hot_recency_contribution, 6),
                "rating_contribution": round(rating_contribution, 6),
                "rewatch_contribution": round(rewatch_contribution, 6),
                "dampeners_contribution": round(dampeners_contribution, 6),
                "seasonality_dampener_contribution": round(
                    seasonality_dampener_contribution,
                    6,
                ),
                "diversity_dampener_contribution": round(
                    diversity_dampener_contribution,
                    6,
                ),
                "era_dampener_contribution": round(era_dampener_contribution, 6),
                "opening_era_dampener_contribution": round(
                    opening_era_dampener_contribution,
                    6,
                ),
                "seasonal_adjustment": round(seasonal_adjustment, 6),
                "diversity_multiplier": round(diversity_multiplier, 6),
                "era_multiplier": round(era_multiplier, 6),
                "medium_phase_held_opening": float(
                    score.get("medium_phase_held_opening", 0.0),
                ) >= 1.0,
                "strong_phase_promoted_opening": float(
                    score.get("strong_phase_promoted_opening", 0.0),
                ) >= 1.0,
                "medium_phase_demoted_opening": float(
                    score.get("medium_phase_demoted_opening", 0.0),
                ) >= 1.0,
                "phase_lane_promoted": phase_lane_promoted,
                "phase_pool_source": phase_pool_source,
                "penalty_count": penalty_count,
            },
        )

    return {
        "top_n": effective_top_n,
        "top_candidates": top_candidates,
        "score_distribution": {
            "raw_min": round(raw_min, 6),
            "raw_max": round(raw_max, 6),
            "raw_spread": round(raw_spread, 6),
            "display_min": round(display_min, 6),
            "display_max": round(display_max, 6),
            "display_spread": round(display_spread, 6),
            "compressed_raw": raw_spread < COMFORT_SPREAD_COMPRESSION_THRESHOLD,
        },
        "penalty_stack": {
            "multi_penalty_count": len(multi_penalty_ids),
            "multi_penalty_media_ids": multi_penalty_ids,
        },
        "contribution_totals": {
            "phase_family": round(contribution_totals["phase_family"], 6),
            "hot_recency": round(contribution_totals["hot_recency"], 6),
            "rating": round(contribution_totals["rating"], 6),
            "rewatch": round(contribution_totals["rewatch"], 6),
            "dampeners": round(contribution_totals["dampeners"], 6),
        },
        "dampener_totals": {
            "seasonality": round(dampener_totals["seasonality"], 6),
            "diversity": round(dampener_totals["diversity"], 6),
            "era": round(dampener_totals["era"], 6),
            "opening_era": round(dampener_totals["opening_era"], 6),
        },
        "tag_signal": {
            "mode": tag_signal_mode,
            "candidate_tag_coverage_top_n": round(
                _clamp_unit(top_tagged_count / max(1, effective_top_n)),
                6,
            ),
            "candidate_tag_coverage_pool": round(
                _clamp_unit(pool_tagged_count / max(1, len(candidates))),
                6,
            ),
            "recent_history_tag_coverage": round(recent_history_tag_coverage, 6),
            "recent_history_window_days": COMFORT_RECENT_HISTORY_TAG_WINDOW_DAYS,
            "hot_recency_mode_multiplier": round(hot_recency_mode_multiplier, 6),
        },
    }


def _comfort_match_signal(profile_payload: dict) -> str:
    """Build a row-level signal string from phase tag/genre activity."""
    payload = profile_payload or {}
    phase_tags = payload.get("phase_tag_affinity") or payload.get("recent_tag_affinity") or {}
    phase_genres = payload.get("phase_genre_affinity") or payload.get("recent_genre_affinity") or {}

    top_labels = _top_phase_labels(
        phase_tags,
        limit=3,
        allow_generic_terms=False,
    )
    if len(top_labels) < 3:
        for label in _top_phase_labels(
            phase_tags,
            limit=3,
            allow_generic_terms=True,
        ):
            if label not in top_labels:
                top_labels.append(label)
            if len(top_labels) >= 3:
                break

    if len(top_labels) < 3:
        for label in _top_phase_labels(
            phase_genres,
            limit=3,
            allow_generic_terms=True,
        ):
            if label not in top_labels:
                top_labels.append(label)
            if len(top_labels) >= 3:
                break

    if not top_labels:
        return ""
    return "Driven by your current " + ", ".join(top_labels[:3]) + " phase"


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
                profile_payload=profile_payload,
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
            _apply_comfort_confidence(candidates, profile_payload)
        return candidates

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

    match_signal = None
    if row_definition.key in {
        "top_picks_for_you",
        "comfort_rewatches",
        "comfort_binge",
        "comfort",
        "comfort_replay",
        "comfort_picks",
    }:
        match_signal = _comfort_match_signal(profile_payload)

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
        match_signal=match_signal or None,
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


def get_discover_rows(
    user,
    media_type: str,
    *,
    show_more: bool = False,
    include_debug: bool = False,
) -> list[RowResult]:
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
            all_time_row = (
                media_type == MediaTypes.MOVIE.value
                and row_definition.key == "all_time_greats_unseen"
            )
            if all_time_row:
                row.items = dedupe_candidates(row.items, seen_identities=set())
                seen_identities.update(item.identity() for item in row.items)
            else:
                row.items = dedupe_candidates(row.items, seen_identities=seen_identities)
            dedupe_removed = before_count - len(row.items)

            if (
                media_type == MediaTypes.MOVIE.value
                and row_definition.key == "coming_soon"
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

            if include_debug and row_definition.key in {"comfort_rewatches", "top_picks_for_you"}:
                row.debug_payload = _build_comfort_debug_payload(row.items, top_n=COMFORT_DEBUG_TOP_N)

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


def get_discover_payload(
    user,
    media_type: str,
    *,
    show_more: bool = False,
    include_debug: bool = False,
) -> DiscoverPayload:
    """Return top-level discover payload for selected media type."""
    media_type = _coerce_media_type(media_type)
    rows = get_discover_rows(
        user,
        media_type,
        show_more=show_more,
        include_debug=include_debug,
    )
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
