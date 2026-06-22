"""Discover orchestration service."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import OperationalError
from django.utils import timezone

from app.discover import cache_repo, tab_cache
from app.discover.adapters import TMDB_ADAPTER
from app.discover.artwork import (
    PROVIDER_ARTWORK_HYDRATION_ROW_KEYS,
    _hydrate_provider_ranked_artwork,
    _hydrate_trakt_ranked_artwork,
    _row_ttl_seconds,
    _supports_provider_artwork_hydration,
)
from app.discover.comfort_scoring import (
    COMFORT_DEBUG_TOP_N,
    _apply_comfort_confidence,
    _build_comfort_debug_payload,
)
from app.discover.entry_candidates import (
    _clear_out_next_entries,
    _coerce_media_type,
    _entries_to_candidates,
    _in_progress_candidates,
    _planning_candidates,
)
from app.discover.filters import (
    dedupe_candidates,
    exclude_tracked_items,
    get_feedback_keys_by_media_type,
    get_tracked_keys_by_media_type,
)
from app.discover.match_signals import (
    _row_match_signal,
    _row_match_signal_with_details,
    _wildcard_genres,
)
from app.discover.movie_comfort import (
    _candidate_release_status,
    _entry_phase_evidence,
    _phase_affinity_maps,
)
from app.discover.profile import get_or_compute_taste_profile
from app.discover.provider_candidates import _provider_row_candidates
from app.discover.registry import ALL_MEDIA_KEY, DISCOVER_MEDIA_TYPES, get_rows
from app.discover.row_cache_schema import (
    ROW_CACHE_ACTIVITY_VERSION_META_KEY,
    _apply_row_definition_metadata,
    _is_row_cache_compatible,
    _required_row_cache_schema_version,
    _row_cache_matches_activity_version,
    _row_requires_artwork_rebuild,
)
from app.discover.schemas import (
    CandidateItem,
    DiscoverPayload,
    RowDefinition,
    RowResult,
)
from app.discover.scoring import score_candidates
from app.discover.service_helpers import (
    BEHAVIOR_FIRST_MEDIA_TYPES,
    COMFORT_PHASE_EVIDENCE_THRESHOLD,
    COMFORT_PHASE_POOL_MIN_BACKFILL,
    COMFORT_RECENT_HISTORY_TAG_WINDOW_DAYS,
    MAX_ITEMS_PER_ROW,
    _activity_filter_query,
    _activity_ordering,
    _calibrate_display_score,
    _clamp_unit,
    _entry_activity_datetime,
    _item_tag_map,
    _model_for_media_type,
    _model_has_field,
    _rewatch_counts,
)
from app.discover.tabs import TAB_REGISTRY, TAB_ROW_DESCRIPTIONS
from app.discover.trakt_candidates import (
    ROW_CACHE_SCHEMA_META_KEY,
    _genre_discovery_candidates,
    _merge_unique_candidates,
    _related_candidates_for_anchors,
    _related_row_candidates,
    _select_recent_anchors,
    _top_profile_genres,
    _trakt_anticipated_candidates,
    _trakt_canon_candidates,
)
from app.models import (
    Item,
    MediaTypes,
    Status,
)

logger = logging.getLogger(__name__)

# Row keys backed by an external provider tab (derived from the tab registry so
# new tabs route through _provider_row_candidates without touching this module).
_PROVIDER_TAB_ROW_KEYS = frozenset(
    tab.row_key for tabs in TAB_REGISTRY.values() for tab in tabs
)

STALE_REFRESH_LOCK_SECONDS = 60
ROW_CANDIDATE_BUFFER_MULTIPLIER = 5
ARTWORK_HYDRATION_ITEMS_PER_ROW = MAX_ITEMS_PER_ROW * 2
FIVE_ROW_DISCOVER_KEYS = {
    "trending_right_now",
    "all_time_greats_unseen",
    "coming_soon",
    "top_picks_for_you",
    "clear_out_next",
    "comfort_rewatches",
}
FIVE_ROW_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
    MediaTypes.MUSIC.value,
    MediaTypes.PODCAST.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
    MediaTypes.MANGA.value,
    MediaTypes.GAME.value,
    MediaTypes.BOARDGAME.value,
}
ALWAYS_VISIBLE_EMPTY_ROWS = {
    "continue",
    "continue_up_next",
    "next_episode",
}


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
    recent_queryset = (
        model.objects.filter(
            user=user,
            status=Status.COMPLETED.value,
        )
    )
    if _model_has_field(model, "end_date") or _model_has_field(model, "progressed_at"):
        recent_queryset = recent_queryset.filter(
            _activity_filter_query(model, cutoff, newer_than=True),
        )
    recent_entries = [
        entry
        for entry in recent_queryset.order_by(*_activity_ordering(model))[:300]
        if (_entry_activity_datetime(entry) and _entry_activity_datetime(entry) >= cutoff)
    ]
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
    rated_queryset = (
        model.objects.filter(
            user=user,
            status=Status.COMPLETED.value,
            score__gte=min_score,
        )
        .select_related("item")
        .order_by("-score", *_activity_ordering(model))
    )
    if _model_has_field(model, "end_date") or _model_has_field(model, "progressed_at"):
        rated_queryset = rated_queryset.filter(
            _activity_filter_query(model, cutoff, newer_than=False),
        )

    unrated_cutoff = now - timedelta(days=max(older_than_days, 90))
    unrated_queryset = (
        model.objects.filter(
            user=user,
            status=Status.COMPLETED.value,
            score__isnull=True,
        )
        .select_related("item")
        .order_by(*_activity_ordering(model))
    )
    if _model_has_field(model, "end_date") or _model_has_field(model, "progressed_at"):
        unrated_queryset = unrated_queryset.filter(
            _activity_filter_query(model, unrated_cutoff, newer_than=False),
        )

    rated_entries = []
    for entry in rated_queryset:
        activity_dt = _entry_activity_datetime(entry)
        if activity_dt and activity_dt <= cutoff:
            rated_entries.append(entry)

    unrated_entries = []
    for entry in unrated_queryset:
        activity_dt = _entry_activity_datetime(entry)
        if activity_dt and activity_dt <= unrated_cutoff:
            unrated_entries.append(entry)

    entries = rated_entries + unrated_entries
    item_ids = [entry.item_id for entry in entries if entry.item_id]
    rewatch_count_map = _rewatch_counts(
        user,
        model,
        item_ids,
        media_type=media_type,
    )
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


def _top_picks_candidates(user, media_type: str, row_key: str, profile_payload: dict) -> list[CandidateItem]:
    candidates = _planning_candidates(
        user,
        media_type,
        row_key=row_key,
        source_reason="Ranked from your planning list",
    )
    if media_type == MediaTypes.MOVIE.value:
        today = timezone.localdate()
        candidates = [
            candidate
            for candidate in candidates
            if _candidate_release_status(candidate, today=today) != "upcoming"
        ]
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

    _apply_top_picks_confidence(candidates, profile_payload, media_type=media_type, user=user)
    return candidates


def _clear_out_next_candidates(
    user,
    media_type: str,
    row_key: str,
    profile_payload: dict,
) -> list[CandidateItem]:
    candidates = _entries_to_candidates(
        _clear_out_next_entries(user, media_type),
        user=user,
        row_key=row_key,
        source_reason="Ranked from your in-progress list",
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

    _apply_top_picks_confidence(
        candidates,
        profile_payload,
        media_type=media_type,
        user=user,
    )
    return candidates


def _apply_top_picks_confidence(
    candidates: list[CandidateItem],
    profile_payload: dict | None = None,
    *,
    media_type: str = "",
    user=None,
) -> list[CandidateItem]:
    return _apply_comfort_confidence(
        candidates,
        profile_payload,
        use_movie_rewatch_model=(media_type in BEHAVIOR_FIRST_MEDIA_TYPES),
        user=user if media_type == MediaTypes.MOVIE.value else None,
    )


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


def _build_row_candidates(
    user,
    media_type: str,
    row_definition: RowDefinition,
    profile_payload: dict,
) -> list[CandidateItem]:
    row_key = row_definition.key

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

    if row_key in _PROVIDER_TAB_ROW_KEYS or row_key in {
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

    if row_key == "clear_out_next":
        return _clear_out_next_candidates(user, media_type, row_key, profile_payload)

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
            _apply_comfort_confidence(
                candidates,
                profile_payload,
                use_movie_rewatch_model=(media_type in BEHAVIOR_FIRST_MEDIA_TYPES),
                user=user,
            )
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


def _blocked_statuses_for_row(row_definition: RowDefinition) -> set[str] | None:
    if (
        row_definition.key in {"trending_right_now", "all_time_greats_unseen", "coming_soon"}
        or row_definition.key in _PROVIDER_TAB_ROW_KEYS
    ):
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
        from app.tasks import refresh_discover_rows  # noqa: PLC0415

        refresh_discover_rows.delay(user_id, media_type, [row_key], show_more=show_more)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "discover_refresh_enqueue_failed user_id=%s media_type=%s row_key=%s error=%s",
            user_id,
            media_type,
            row_key,
            error,
        )


def _prepare_row_from_candidates(
    user,
    media_type: str,
    row_definition: RowDefinition,
    profile_payload: dict,
    candidates: list[CandidateItem],
    *,
    defer_artwork: bool = False,
    show_more: bool = False,
    source_state: str = "live",
) -> tuple[RowResult, bool]:
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

    feedback_keys = _discover_feedback_keys(user, media_type)
    if feedback_keys:
        candidates = exclude_tracked_items(candidates, feedback_keys)

    needs_async_artwork_refresh = False
    is_trakt_ranked_row = media_type in {
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    } and row_definition.key in {
        "trending_right_now",
        "all_time_greats_unseen",
        "coming_soon",
    }
    is_provider_ranked_row = (
        row_definition.source == "provider"
        and row_definition.key in PROVIDER_ARTWORK_HYDRATION_ROW_KEYS
    )
    artwork_hydration_limit = min(
        len(candidates),
        MAX_ITEMS_PER_ROW * ROW_CANDIDATE_BUFFER_MULTIPLIER,
        ARTWORK_HYDRATION_ITEMS_PER_ROW,
    )
    if is_trakt_ranked_row:
        if defer_artwork:
            needs_async_artwork_refresh = any(
                _is_missing_image(item)
                for item in candidates[:artwork_hydration_limit]
            )
        else:
            _hydrate_trakt_ranked_artwork(
                media_type,
                candidates,
                hydrate_limit=artwork_hydration_limit,
            )
    elif is_provider_ranked_row:
        provider_display_candidates = [
            candidate
            for candidate in candidates[:artwork_hydration_limit]
            if _supports_provider_artwork_hydration(candidate)
        ]
        if defer_artwork:
            needs_async_artwork_refresh = any(
                _is_missing_image(candidate)
                for candidate in provider_display_candidates
            )
        else:
            _hydrate_provider_ranked_artwork(
                candidates,
                hydrate_limit=artwork_hydration_limit,
            )

    match_signal = _row_match_signal(
        row_definition.key,
        candidates,
        profile_payload,
    )

    buffered_limit = MAX_ITEMS_PER_ROW * ROW_CANDIDATE_BUFFER_MULTIPLIER
    row = RowResult(
        key=row_definition.key,
        title=row_definition.title,
        mission=row_definition.mission,
        why=row_definition.why,
        source=row_definition.source,
        items=candidates[:buffered_limit],
        show_more=row_definition.show_more,
        source_state=source_state,
        match_signal=match_signal or None,
    )
    return row, needs_async_artwork_refresh


def _trakt_row_provider_fallback_candidates(media_type: str, row_key: str) -> list[CandidateItem]:
    if media_type not in {MediaTypes.MOVIE.value, MediaTypes.TV.value}:
        return []

    if row_key == "trending_right_now":
        return TMDB_ADAPTER.current_cycle(media_type, limit=100)
    if row_key == "all_time_greats_unseen":
        return TMDB_ADAPTER.top_rated(media_type, limit=100)
    if row_key == "coming_soon":
        return TMDB_ADAPTER.upcoming(media_type, limit=100)
    return []


def _build_row_error_fallback(
    user,
    media_type: str,
    row_definition: RowDefinition,
    profile_payload: dict,
    *,
    cached_payload: dict | None,
    defer_artwork: bool = False,
    show_more: bool = False,
) -> RowResult | None:
    if cached_payload:
        row = RowResult.from_dict(cached_payload)
        row = _apply_row_definition_metadata(row, row_definition)
        row.is_stale = True
        row.source_state = "stale"
        return row

    if row_definition.source != "trakt":
        return None

    fallback_candidates = _trakt_row_provider_fallback_candidates(
        media_type,
        row_definition.key,
    )
    if not fallback_candidates:
        return None

    fallback_row, needs_async_artwork_refresh = _prepare_row_from_candidates(
        user,
        media_type,
        row_definition,
        profile_payload,
        fallback_candidates,
        defer_artwork=defer_artwork,
        show_more=show_more,
        source_state="fallback",
    )
    if needs_async_artwork_refresh:
        _queue_stale_refresh(user.id, media_type, row_definition.key, show_more)
    return fallback_row


def _build_and_cache_row(
    user,
    media_type: str,
    row_definition: RowDefinition,
    profile_payload: dict,
    *,
    seen_identities: set[tuple[str, str, str]] | None = None,
    defer_artwork: bool = False,
    show_more: bool = False,
) -> RowResult:
    started = timezone.now()
    build_activity_version = tab_cache.get_activity_version(user.id, media_type)
    row_meta: dict | None = None
    if media_type in {MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value} and row_definition.key in {
        "all_time_greats_unseen",
        "coming_soon",
    }:
        if row_definition.key == "all_time_greats_unseen":
            candidates, row_meta = _trakt_canon_candidates(
                user,
                media_type,
                row_key=row_definition.key,
                seen_identities=seen_identities,
            )
        else:
            candidates, row_meta = _trakt_anticipated_candidates(
                user,
                media_type,
                row_key=row_definition.key,
                seen_identities=seen_identities,
            )
    else:
        candidates = _build_row_candidates(user, media_type, row_definition, profile_payload)

    row_meta = dict(row_meta or {})
    required_schema_version = _required_row_cache_schema_version(media_type, row_definition.key)
    if required_schema_version is not None:
        row_meta[ROW_CACHE_SCHEMA_META_KEY] = required_schema_version
    row_meta[ROW_CACHE_ACTIVITY_VERSION_META_KEY] = build_activity_version

    row, needs_async_artwork_refresh = _prepare_row_from_candidates(
        user,
        media_type,
        row_definition,
        profile_payload,
        candidates,
        defer_artwork=defer_artwork,
        show_more=show_more,
        source_state="live",
    )

    cache_payload = row.to_dict()
    cache_payload["meta"] = row_meta

    try:
        current_activity_version = tab_cache.get_activity_version(user.id, media_type)
        if current_activity_version == build_activity_version:
            cache_repo.set_row_cache(
                user.id,
                media_type,
                row_definition.key,
                cache_payload,
                ttl_seconds=_row_ttl_seconds(row_definition),
            )
        else:
            logger.info(
                "discover_row_cache_skip_stale_build user_id=%s media_type=%s row_key=%s "
                "started_version=%s current_version=%s",
                user.id,
                media_type,
                row_definition.key,
                build_activity_version,
                current_activity_version,
            )
    except OperationalError as error:
        logger.warning(
            "discover_row_cache_write_failed user_id=%s media_type=%s row_key=%s error=%s",
            user.id,
            media_type,
            row_definition.key,
            error,
        )

    if needs_async_artwork_refresh:
        _queue_stale_refresh(user.id, media_type, row_definition.key, show_more)

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


def _allow_empty_row(
    media_type: str,
    row_key: str,
) -> bool:
    if row_key in ALWAYS_VISIBLE_EMPTY_ROWS:
        return True
    if media_type in (FIVE_ROW_MEDIA_TYPES - {MediaTypes.MOVIE.value}) and row_key in FIVE_ROW_DISCOVER_KEYS:
        return True
    return False


def _media_type_readable_plural(media_type: str) -> str:
    """Return a human-readable plural label for a Discover media type."""
    label = MediaTypes(media_type).label
    if media_type in {
        MediaTypes.ANIME.value,
        MediaTypes.MANGA.value,
        MediaTypes.MUSIC.value,
    }:
        return label
    return f"{label}s"


def _get_all_media_component_rows(
    user,
    media_type: str,
    *,
    show_more: bool,
    include_debug: bool,
    defer_artwork: bool,
) -> list[RowResult]:
    """Return the rendered rows for a single media type tab."""
    return get_discover_rows(
        user,
        media_type,
        show_more=show_more,
        include_debug=include_debug,
        defer_artwork=defer_artwork,
        row_keys=None if show_more else ["trending_right_now"],
    )


def _compose_all_media_rows(
    user,
    *,
    show_more: bool,
    include_debug: bool,
    defer_artwork: bool,
) -> list[RowResult]:
    """Compose the all-media tab from the user's enabled Discover media types."""
    enabled_media_types = [
        media_type
        for media_type in user.get_enabled_media_types()
        if media_type in DISCOVER_MEDIA_TYPES
    ]
    target_media_types = enabled_media_types or DISCOVER_MEDIA_TYPES
    rows: list[RowResult] = []

    for component_media_type in target_media_types:
        component_rows = _get_all_media_component_rows(
            user,
            component_media_type,
            show_more=show_more,
            include_debug=include_debug,
            defer_artwork=defer_artwork,
        )
        if not show_more:
            component_rows = [
                row
                for row in component_rows
                if row.key == "trending_right_now"
            ]

        row_prefix = _media_type_readable_plural(component_media_type)
        for row in component_rows:
            row.title = f"{row_prefix}: {row.title}"
            row.component_media_type = component_media_type
            rows.append(row)

    return rows


def _discover_feedback_keys(user, media_type: str) -> set[tuple[str, str, str]]:
    if media_type != ALL_MEDIA_KEY:
        return get_feedback_keys_by_media_type(user, media_type)

    feedback_keys: set[tuple[str, str, str]] = set()
    for media_type_key in DISCOVER_MEDIA_TYPES:
        feedback_keys.update(get_feedback_keys_by_media_type(user, media_type_key))
    return feedback_keys


def get_discover_rows(
    user,
    media_type: str,
    *,
    show_more: bool = False,
    include_debug: bool = False,
    defer_artwork: bool = False,
    row_keys: list[str] | None = None,
) -> list[RowResult]:
    """Return discover rows for selected media type."""
    media_type = _coerce_media_type(media_type)
    media_type = tab_cache.resolve_media_type_for_user(user, media_type)
    if media_type == ALL_MEDIA_KEY:
        return _compose_all_media_rows(
            user,
            show_more=show_more,
            include_debug=include_debug,
            defer_artwork=defer_artwork,
        )

    row_definitions = get_rows(media_type, include_show_more=show_more)
    if row_keys is not None:
        requested_keys = set(row_keys)
        row_definitions = [
            row_definition
            for row_definition in row_definitions
            if row_definition.key in requested_keys
        ]
    profile_payload = get_or_compute_taste_profile(user, media_type)

    seen_identities: set[tuple[str, str, str]] = set()
    rows: list[RowResult] = []

    for row_definition in row_definitions:
        cached_payload: dict | None = None
        is_stale = False
        row: RowResult | None = None
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
                        defer_artwork=defer_artwork,
                        show_more=show_more,
                    )
                elif not _is_row_cache_compatible(media_type, row_definition, cached_payload):
                    row = _build_and_cache_row(
                        user,
                        media_type,
                        row_definition,
                        profile_payload,
                        seen_identities=seen_identities,
                        defer_artwork=defer_artwork,
                        show_more=show_more,
                    )
                elif not _row_cache_matches_activity_version(user.id, media_type, cached_payload):
                    row = _build_and_cache_row(
                        user,
                        media_type,
                        row_definition,
                        profile_payload,
                        seen_identities=seen_identities,
                        defer_artwork=defer_artwork,
                        show_more=show_more,
                    )
                elif _row_requires_artwork_rebuild(media_type, row_definition, row):
                    if defer_artwork:
                        row = _apply_row_definition_metadata(row, row_definition)
                        _queue_stale_refresh(user.id, media_type, row_definition.key, show_more)
                    else:
                        row = _build_and_cache_row(
                            user,
                            media_type,
                            row_definition,
                            profile_payload,
                            seen_identities=seen_identities,
                            defer_artwork=defer_artwork,
                            show_more=show_more,
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
                    defer_artwork=defer_artwork,
                    show_more=show_more,
                )

        except Exception as error:  # noqa: BLE001
            logger.exception(
                "discover_row_failed user_id=%s media_type=%s row_key=%s error=%s",
                user.id,
                media_type,
                row_definition.key,
                error,
            )
            row = _build_row_error_fallback(
                user,
                media_type,
                row_definition,
                profile_payload,
                cached_payload=cached_payload,
                defer_artwork=defer_artwork,
                show_more=show_more,
            )
            if row is not None:
                logger.warning(
                    "discover_row_error_fallback user_id=%s media_type=%s row_key=%s source_state=%s",
                    user.id,
                    media_type,
                    row_definition.key,
                    row.source_state,
                )
            elif _allow_empty_row(media_type, row_definition.key):
                fallback_signal, _fallback_details = _row_match_signal_with_details(
                    row_definition.key,
                    [],
                    profile_payload,
                )
                fallback_row = RowResult(
                    key=row_definition.key,
                    title=row_definition.title,
                    mission=row_definition.mission,
                    why=row_definition.why,
                    source=row_definition.source,
                    items=[],
                    show_more=row_definition.show_more,
                    source_state="error",
                    match_signal=fallback_signal,
                )
                logger.info(
                    "discover_row_render user_id=%s media_type=%s row_key=%s result_count=%s source=%s filtered_count=%s",
                    user.id,
                    media_type,
                    row_definition.key,
                    0,
                    "error",
                    0,
                )
                rows.append(fallback_row)
                continue
            else:
                continue

        if row is None:
            continue

        prior_seen_identities = set(seen_identities)
        before_count = len(row.items)
        all_time_row = row_definition.key == "all_time_greats_unseen"
        if all_time_row:
            deduped_items = dedupe_candidates(row.items, seen_identities=set())
            seen_identities.update(item.identity() for item in deduped_items[:MAX_ITEMS_PER_ROW])
        else:
            deduped_items = dedupe_candidates(row.items, seen_identities=seen_identities)
        dedupe_removed = before_count - len(deduped_items)

        if (
            media_type in {MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value}
            and row_definition.key == "coming_soon"
            and len(deduped_items) < MAX_ITEMS_PER_ROW
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
                defer_artwork=defer_artwork,
                show_more=show_more,
            )
            before_count = len(row.items)
            deduped_items = dedupe_candidates(row.items, seen_identities=seen_identities)
            dedupe_removed = before_count - len(deduped_items)

        row.items = deduped_items[:MAX_ITEMS_PER_ROW]
        row.reserve_items = deduped_items[MAX_ITEMS_PER_ROW:]
        filtered_count = before_count - len(row.items)
        match_signal, match_signal_details = _row_match_signal_with_details(
            row_definition.key,
            row.items,
            profile_payload,
        )
        row.match_signal = match_signal

        if not row.items and not _allow_empty_row(media_type, row_definition.key):
            continue

        if len(row.items) < row_definition.min_items and not _allow_empty_row(
            media_type,
            row_definition.key,
        ):
            continue

        if include_debug and row_definition.key in {
            "comfort_rewatches",
            "top_picks_for_you",
            "clear_out_next",
        }:
            row.debug_payload = _build_comfort_debug_payload(
                row.items,
                top_n=COMFORT_DEBUG_TOP_N,
                match_signal_details=match_signal_details,
            )

        if match_signal_details:
            logger.info(
                "discover_row_match_signal user_id=%s media_type=%s row_key=%s signal=%s explanation=%s",
                user.id,
                media_type,
                row_definition.key,
                row.match_signal or "",
                str(match_signal_details.get("explanation", "")),
            )

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

    return rows


def _tab_row_definition(media_type: str, tab) -> RowDefinition:
    """Return the RowDefinition backing a tab.

    Reuses the registry definition when the tab maps to an existing row (so the
    Trakt canon/anticipated build paths and copy are preserved); otherwise
    synthesizes a definition for the new tab-only row.
    """
    for row_definition in get_rows(media_type, include_show_more=True):
        if row_definition.key == tab.row_key:
            return row_definition
    return RowDefinition(
        key=tab.row_key,
        title=tab.label,
        mission="",
        why=TAB_ROW_DESCRIPTIONS.get(tab.row_key, ""),
        source=tab.provider,
    )


def get_discover_tab_row(user, media_type: str, tab) -> RowResult:
    """Build a single editorial tab row on demand.

    Unlike the stacked rows, a user-selected tab always renders (even when sparse),
    so the min-items / drop-if-empty filtering used by get_discover_rows is skipped.
    """
    media_type = _coerce_media_type(media_type)
    media_type = tab_cache.resolve_media_type_for_user(user, media_type)
    row_definition = _tab_row_definition(media_type, tab)
    profile_payload = get_or_compute_taste_profile(user, media_type)
    row = _build_and_cache_row(
        user,
        media_type,
        row_definition,
        profile_payload,
        defer_artwork=False,
        show_more=False,
    )
    deduped_items = dedupe_candidates(row.items, seen_identities=set())
    row.items = deduped_items[:MAX_ITEMS_PER_ROW]
    row.reserve_items = deduped_items[MAX_ITEMS_PER_ROW:]
    row.match_signal = _row_match_signal_with_details(
        row_definition.key,
        row.items,
        profile_payload,
    )[0]
    return row


def get_discover_payload(
    user,
    media_type: str,
    *,
    show_more: bool = False,
    include_debug: bool = False,
    defer_artwork: bool = False,
) -> DiscoverPayload:
    """Return top-level discover payload for selected media type."""
    media_type = _coerce_media_type(media_type)
    rows = get_discover_rows(
        user,
        media_type,
        show_more=show_more,
        include_debug=include_debug,
        defer_artwork=defer_artwork,
    )
    return DiscoverPayload(
        media_type=media_type,
        rows=rows,
        show_more=show_more,
    )


def refresh_rows_for_user(user, media_type: str, row_keys: list[str], *, show_more: bool = False) -> int:
    """Rebuild selected rows and refresh row cache entries."""
    media_type = _coerce_media_type(media_type)
    if (
        media_type != ALL_MEDIA_KEY
        and not tab_cache.media_type_is_enabled_for_user(user, media_type)
    ):
        logger.debug(
            "discover_row_refresh_skipped user_id=%s media_type=%s reason=disabled_media_type",
            user.id,
            media_type,
        )
        return 0
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
