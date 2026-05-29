"""Library entry ORM → CandidateItem conversion for Discover rows."""

from __future__ import annotations

from collections import defaultdict

from django.utils import timezone

from app.discover.feature_metadata import (
    normalize_certification,
    normalize_features,
    normalize_keyword,
    normalize_studio,
    release_decade_label,
    runtime_bucket_label,
)
from app.discover.registry import ALL_MEDIA_KEY, DISCOVER_MEDIA_TYPES
from app.discover.schemas import CandidateItem
from app.discover.service_helpers import (
    BEHAVIOR_FIRST_MEDIA_TYPES,
    _activity_ordering,
    _clamp_unit,
    _entry_activity_datetime,
    _item_credit_feature_maps,
    _item_studio_map,
    _item_tag_map,
    _model_for_media_type,
)
from app.models import BasicMedia, MediaTypes, Season, Status


def _coerce_media_type(media_type: str | None) -> str:
    media_type = (media_type or ALL_MEDIA_KEY).strip().lower()
    if media_type == ALL_MEDIA_KEY:
        return media_type
    if media_type in DISCOVER_MEDIA_TYPES:
        return media_type
    return ALL_MEDIA_KEY


def _entries_to_candidates(
    entries,
    *,
    user,
    row_key: str,
    source_reason: str,
    override_media_type: str | None = None,
    rewatch_counts: dict[int, float] | None = None,
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
        override_media_type in BEHAVIOR_FIRST_MEDIA_TYPES
        or (entries and entries[0].item.media_type in BEHAVIOR_FIRST_MEDIA_TYPES)
    )
    people_map: dict[int, list[str]] = defaultdict(list)
    directors_map: dict[int, list[str]] = defaultdict(list)
    lead_cast_map: dict[int, list[str]] = defaultdict(list)
    studio_map: dict[int, list[str]] = defaultdict(list)
    if include_people:
        people_map, directors_map, lead_cast_map = _item_credit_feature_maps(item_ids)
        studio_map = _item_studio_map(item_ids)

    candidates: list[CandidateItem] = []
    for entry in entries:
        item = entry.item
        media_type = override_media_type or item.media_type
        release_date = item.release_datetime.date().isoformat() if item.release_datetime else None
        activity_dt = _entry_activity_datetime(entry)
        entry_status = str(getattr(entry, "status", "") or "")
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
            keywords=normalize_features(item.provider_keywords or [], normalize_keyword),
            studios=studio_map.get(item.id) or normalize_features(item.studios or [], normalize_studio),
            directors=directors_map.get(item.id, []),
            lead_cast=lead_cast_map.get(item.id, []),
            collection_id=str(item.provider_collection_id or "").strip() or None,
            collection_name=str(item.provider_collection_name or "").strip() or None,
            certification=normalize_certification(item.provider_certification) or None,
            runtime_bucket=runtime_bucket_label(item.runtime_minutes) or None,
            release_decade=release_decade_label(item.release_datetime) or None,
            popularity=item.provider_popularity,
            rating=item.provider_rating,
            rating_count=item.provider_rating_count,
            row_key=row_key,
            source_reason=source_reason,
        )
        candidate.score_breakdown["provider_rating"] = item.provider_rating
        candidate.score_breakdown["provider_rating_count"] = item.provider_rating_count
        candidate.score_breakdown["trakt_rating"] = item.trakt_rating
        candidate.score_breakdown["trakt_rating_count"] = item.trakt_rating_count
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
        is_movie_top_picks_planning = (
            row_key == "top_picks_for_you"
            and media_type == MediaTypes.MOVIE.value
            and entry_status == Status.PLANNING.value
        )
        if is_movie_top_picks_planning:
            candidate.score_breakdown["planning_entry"] = 1.0
            created_at = getattr(entry, "created_at", None)
            if created_at:
                candidate.score_breakdown["days_since_planned"] = float(
                    max(0, (timezone.now() - created_at).days),
                )
        elif activity_dt:
            candidate.score_breakdown["days_since_activity"] = float(
                max(0, (timezone.now() - activity_dt).days),
            )
        candidates.append(candidate)

    return candidates


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
        .order_by(*_activity_ordering(model))
    )
    return _entries_to_candidates(
        entries,
        user=user,
        row_key=row_key,
        source_reason=source_reason,
    )


def _is_caught_up_in_progress_entry(entry) -> bool:
    max_progress = getattr(entry, "max_progress", None)
    if max_progress is None:
        return False

    try:
        max_progress_value = int(max_progress)
    except (TypeError, ValueError):
        return False

    if max_progress_value <= 0:
        return False

    try:
        progress_value = int(getattr(entry, "progress", 0) or 0)
    except (TypeError, ValueError):
        return False

    return progress_value >= max_progress_value


def _clear_out_next_entries(user, media_type: str):
    model = _model_for_media_type(media_type)
    if not model:
        return []

    entries = list(
        model.objects.filter(user=user, status=Status.IN_PROGRESS.value)
        .select_related("item")
        .order_by(*_activity_ordering(model))
    )
    if not entries:
        return []

    BasicMedia.objects.annotate_max_progress(entries, media_type)
    return [
        entry
        for entry in entries
        if not _is_caught_up_in_progress_entry(entry)
    ]


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
