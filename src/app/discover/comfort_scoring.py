"""Comfort confidence scoring and debug payload builders for Discover."""

from __future__ import annotations

import math

from app.discover.movie_comfort import (
    MOVIE_COMFORT_FAMILY_WEIGHTS,
    MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS,
    _apply_movie_comfort_confidence,
    _candidate_release_year,
    _comfort_bucket_key,
    _phase_affinity_maps,
    _phase_pool_source,
    _prefer_strong_phase_opening_window,
    _promote_phase_lane_candidates,
)
from app.discover.schemas import CandidateItem
from app.discover.service_helpers import (
    COMFORT_DIVERSITY_DECAY,
    COMFORT_ERA_DECAY,
    COMFORT_ERA_OPENING_DECAY,
    COMFORT_ERA_OPENING_WINDOW,
    COMFORT_HOT_RECENCY_SELECTIVE_EXPONENT,
    COMFORT_HOT_RECENCY_TAG_SPARSE_MULTIPLIER,
    COMFORT_LEGACY_ERA_DECAY,
    COMFORT_RECENT_HISTORY_TAG_WINDOW_DAYS,
    COMFORT_TAG_RICH_CANDIDATE_COVERAGE_THRESHOLD,
    COMFORT_TAG_RICH_HISTORY_COVERAGE_THRESHOLD,
    MAX_ITEMS_PER_ROW,
    WORLD_QUALITY_MEDIA_TYPES,
    _clamp_unit,
    _holiday_seasonal_adjustment,
    _is_holiday_window,
)

COMFORT_DEBUG_TOP_N = 12
COMFORT_SPREAD_COMPRESSION_THRESHOLD = 0.08

def _apply_comfort_confidence(
    candidates: list[CandidateItem],
    profile_payload: dict | None = None,
    *,
    use_movie_rewatch_model: bool = False,
    user=None,
) -> list[CandidateItem]:
    if not candidates:
        return candidates

    phase_genre_affinity, phase_tag_affinity = _phase_affinity_maps(profile_payload)
    if (
        use_movie_rewatch_model
        and candidates
        and all(candidate.media_type in WORLD_QUALITY_MEDIA_TYPES for candidate in candidates)
    ):
        behavior_first_candidates = _apply_movie_comfort_confidence(
            candidates,
            profile_payload,
            user=user,
            phase_genre_affinity=phase_genre_affinity,
        )
        if behavior_first_candidates is not candidates or any(
            "recent_shape_fit" in candidate.score_breakdown for candidate in candidates
        ):
            candidates = behavior_first_candidates
            _calibrate_comfort_display_scores(candidates)
            return candidates

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
        rewatch_count = max(
            1.0,
            float(candidate.score_breakdown.get("rewatch_count", 1.0)),
        )
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
        holiday_strength, seasonal_adjustment = _holiday_seasonal_adjustment(
            candidate,
            holiday_window_active=holiday_window_active,
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


def _build_movie_comfort_debug_payload(
    candidates: list[CandidateItem],
    *,
    top_n: int,
    match_signal_details: dict | None = None,
) -> dict:
    if not candidates:
        payload = {
            "score_model": "movie_behavior_first",
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
                "library": 0.0,
                "recency_phase": 0.0,
                "behavior": 0.0,
                "comfort_safety": 0.0,
                "quality": 0.0,
                "shape_coverage": 0.0,
                "ready_now": 0.0,
                "planning_confidence": 0.0,
            },
            "profile_layer_weights": dict(MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS),
            "family_weights": dict(MOVIE_COMFORT_FAMILY_WEIGHTS),
            "comparison_summary": {
                "legacy_top_titles": [],
                "current_top_titles": [],
                "promoted_titles": [],
                "dropped_titles": [],
                "changed_rank_count": 0,
            },
        }
        if match_signal_details:
            payload["match_signal"] = dict(match_signal_details)
            payload["match_signal_label_sources"] = match_signal_details.get(
                "match_signal_label_sources",
                [],
            )
        return payload

    raw_scores = [
        _clamp_unit(
            float(candidate.score_breakdown.get("raw_final_score", candidate.final_score or 0.0)),
        )
        for candidate in candidates
    ]
    display_scores = [_clamp_unit(float(candidate.display_score or 0.0)) for candidate in candidates]
    raw_min = min(raw_scores)
    raw_max = max(raw_scores)
    display_min = min(display_scores)
    display_max = max(display_scores)
    effective_top_n = min(len(candidates), max(1, top_n))
    top_slice = candidates[:effective_top_n]
    legacy_ranked = sorted(
        candidates,
        key=lambda candidate: int(candidate.score_breakdown.get("legacy_rank", 0) or 0),
    )
    legacy_top_slice = legacy_ranked[:effective_top_n]
    legacy_top_ids = {id(candidate) for candidate in legacy_top_slice}
    current_top_ids = {id(candidate) for candidate in top_slice}

    contribution_totals = {
        "library": 0.0,
        "recency_phase": 0.0,
        "behavior": 0.0,
        "comfort_safety": 0.0,
        "quality": 0.0,
        "shape_coverage": 0.0,
        "ready_now": 0.0,
        "planning_confidence": 0.0,
    }
    top_candidates: list[dict] = []
    multi_penalty_ids: list[str] = []

    for index, candidate in enumerate(top_slice, start=1):
        score = candidate.score_breakdown
        penalty_count = 0
        seasonal_adjustment = float(score.get("seasonal_adjustment", 0.0))
        if float(score.get("generic_only_match", 0.0)) >= 1.0:
            penalty_count += 1
        if str(score.get("reason_bucket_quota_action", "")) in {"relaxed_fill", "forced_fill"}:
            penalty_count += 1
        if float(score.get("candidate_is_unrated", 0.0)) >= 1.0:
            penalty_count += 1
        if float(score.get("saturation_multiplier", 1.0)) < 0.999:
            penalty_count += 1
        if seasonal_adjustment < 0.0:
            penalty_count += 1
        if penalty_count >= 2:
            multi_penalty_ids.append(str(candidate.media_id))

        contribution_totals["library"] += float(score.get("library_contribution", 0.0))
        contribution_totals["recency_phase"] += float(
            score.get("recency_phase_contribution", 0.0),
        )
        contribution_totals["behavior"] += float(score.get("behavior_contribution", 0.0))
        contribution_totals["comfort_safety"] += float(
            score.get("comfort_safety_contribution", 0.0),
        )
        contribution_totals["quality"] += float(score.get("quality_contribution", 0.0))
        contribution_totals["shape_coverage"] += float(
            score.get("shape_coverage_contribution", 0.0),
        )
        contribution_totals["ready_now"] += float(score.get("ready_now_contribution", 0.0))
        contribution_totals["planning_confidence"] += float(
            score.get("planning_confidence_contribution", 0.0),
        )

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
                "library_fit": round(float(score.get("library_fit", 0.0)), 6),
                "recency_phase_fit": round(float(score.get("recency_phase_fit", 0.0)), 6),
                "behavior_score": round(float(score.get("behavior_score", 0.0)), 6),
                "comfort_safety": round(float(score.get("comfort_safety", 0.0)), 6),
                "quality_score": round(float(score.get("quality_score", 0.0)), 6),
                "shape_coverage": round(float(score.get("shape_coverage", 0.0)), 6),
                "core_affinity_score": round(float(score.get("core_affinity_score", 0.0)), 6),
                "ready_now_score": round(float(score.get("ready_now_score", 0.0)), 6),
                "cooldown_penalty": round(float(score.get("cooldown_penalty", 0.0)), 6),
                "title_cooldown_penalty": round(
                    float(score.get("title_cooldown_penalty", 0.0)),
                    6,
                ),
                "burst_replay_allowance": round(
                    float(score.get("burst_replay_allowance", 0.0)),
                    6,
                ),
                "recent_play_count_90d": round(
                    float(score.get("recent_play_count_90d", 0.0)),
                    6,
                ),
                "recent_play_count_180d": round(
                    float(score.get("recent_play_count_180d", 0.0)),
                    6,
                ),
                "recent_gap_median_days": round(
                    float(score.get("recent_gap_median_days", 0.0)),
                    6,
                ),
                "title_saturation_penalty": round(
                    float(score.get("title_saturation_penalty", 0.0)),
                    6,
                ),
                "saturation_multiplier": round(
                    float(score.get("saturation_multiplier", 1.0)),
                    6,
                ),
                "planning_entry": float(score.get("planning_entry", 0.0)) >= 1.0,
                "days_since_planned": round(
                    float(score.get("days_since_planned", 0.0)),
                    6,
                ),
                "title_history_present": float(
                    score.get("title_history_present", 1.0),
                ) >= 1.0,
                "release_ready_score": round(
                    float(score.get("release_ready_score", 0.0)),
                    6,
                ),
                "release_status": str(score.get("release_status", "unknown")),
                "planning_confidence": round(
                    float(score.get("planning_confidence", 0.0)),
                    6,
                ),
                "planning_confidence_bonus": round(
                    float(score.get("planning_confidence_bonus", 0.0)),
                    6,
                ),
                "similar_watched_count": int(score.get("similar_watched_count", 0) or 0),
                "matched_history_families": list(score.get("matched_history_families") or []),
                "rich_history_family_count": int(
                    score.get("rich_history_family_count", 0) or 0,
                ),
                "generic_history_family_count": int(
                    score.get("generic_history_family_count", 0) or 0,
                ),
                "days_since_title_watch": round(
                    float(score.get("days_since_title_watch", 0.0)),
                    6,
                ),
                "title_repeat_gap_days": round(
                    float(score.get("title_repeat_gap_days", 0.0)),
                    6,
                ),
                "title_burstiness": round(float(score.get("title_burstiness", 0.0)), 6),
                "lane_burstiness": round(float(score.get("lane_burstiness", 0.0)), 6),
                "cooldown_window_days": round(
                    float(score.get("cooldown_window_days", 0.0)),
                    6,
                ),
                "rating_confidence": round(float(score.get("rating_confidence", 0.0)), 6),
                "rewatch_strength": round(float(score.get("rewatch_strength", 0.0)), 6),
                "provider_support": round(float(score.get("provider_support", 0.0)), 6),
                "world_quality": round(float(score.get("world_quality", 0.5)), 6),
                "tmdb_world_quality": round(float(score.get("tmdb_world_quality", 0.0)), 6),
                "trakt_world_quality": round(float(score.get("trakt_world_quality", 0.0)), 6),
                "world_source_blend": str(score.get("world_source_blend", "neutral")),
                "world_alignment": round(float(score.get("world_alignment", 0.0)), 6),
                "world_alignment_confidence": round(
                    float(score.get("world_alignment_confidence", 0.0)),
                    6,
                ),
                "world_alignment_sample_size": int(
                    score.get("world_alignment_sample_size", 0) or 0,
                ),
                "legacy_rank": int(score.get("legacy_rank", 0) or 0),
                "rank_delta": int(score.get("rank_delta", 0) or 0),
                "legacy_raw_final_score": round(
                    float(score.get("legacy_raw_final_score", 0.0)),
                    6,
                ),
                "generic_only_match": float(score.get("generic_only_match", 0.0)) >= 1.0,
                "candidate_is_unrated": float(score.get("candidate_is_unrated", 0.0)) >= 1.0,
                "primary_reason_bucket": str(score.get("primary_reason_bucket", "")),
                "reason_bucket_quota_action": str(score.get("reason_bucket_quota_action", "")),
                "evaluated_signal_families": list(score.get("evaluated_signal_families") or []),
                "active_signal_families": list(score.get("active_signal_families") or []),
                "suppressed_signal_families": list(score.get("suppressed_signal_families") or []),
                "family_layer_fits": dict(score.get("family_layer_fits") or {}),
                "library_contribution": round(float(score.get("library_contribution", 0.0)), 6),
                "recency_phase_contribution": round(
                    float(score.get("recency_phase_contribution", 0.0)),
                    6,
                ),
                "behavior_contribution": round(
                    float(score.get("behavior_contribution", 0.0)),
                    6,
                ),
                "comfort_safety_contribution": round(
                    float(score.get("comfort_safety_contribution", 0.0)),
                    6,
                ),
                "quality_contribution": round(float(score.get("quality_contribution", 0.0)), 6),
                "shape_coverage_contribution": round(
                    float(score.get("shape_coverage_contribution", 0.0)),
                    6,
                ),
                "ready_now_contribution": round(
                    float(score.get("ready_now_contribution", 0.0)),
                    6,
                ),
                "planning_confidence_contribution": round(
                    float(score.get("planning_confidence_contribution", 0.0)),
                    6,
                ),
                "holiday_strength": round(float(score.get("holiday_strength", 0.0)), 6),
                "seasonal_adjustment": round(seasonal_adjustment, 6),
                "dampeners_contribution": round(
                    float(score.get("dampeners_contribution", 0.0)),
                    6,
                ),
                "seasonality_dampener_contribution": round(
                    float(score.get("seasonality_dampener_contribution", 0.0)),
                    6,
                ),
                "saturation_dampener_contribution": round(
                    float(score.get("saturation_dampener_contribution", 0.0)),
                    6,
                ),
                "penalty_count": penalty_count,
            },
        )

    payload = {
        "score_model": "movie_behavior_first",
        "top_n": effective_top_n,
        "top_candidates": top_candidates,
        "score_distribution": {
            "raw_min": round(raw_min, 6),
            "raw_max": round(raw_max, 6),
            "raw_spread": round(raw_max - raw_min, 6),
            "display_min": round(display_min, 6),
            "display_max": round(display_max, 6),
            "display_spread": round(display_max - display_min, 6),
            "compressed_raw": (raw_max - raw_min) < COMFORT_SPREAD_COMPRESSION_THRESHOLD,
        },
        "penalty_stack": {
            "multi_penalty_count": len(multi_penalty_ids),
            "multi_penalty_media_ids": multi_penalty_ids,
        },
        "contribution_totals": {
            key: round(value, 6)
            for key, value in contribution_totals.items()
        },
        "profile_layer_weights": dict(MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS),
        "family_weights": dict(MOVIE_COMFORT_FAMILY_WEIGHTS),
        "comparison_summary": {
            "legacy_top_titles": [candidate.title for candidate in legacy_top_slice],
            "current_top_titles": [candidate.title for candidate in top_slice],
            "promoted_titles": [
                candidate.title for candidate in top_slice if id(candidate) not in legacy_top_ids
            ],
            "dropped_titles": [
                candidate.title
                for candidate in legacy_top_slice
                if id(candidate) not in current_top_ids
            ],
            "changed_rank_count": sum(
                1
                for current_rank, candidate in enumerate(top_slice, start=1)
                if int(candidate.score_breakdown.get("legacy_rank", 0) or 0) != current_rank
            ),
        },
    }
    if match_signal_details:
        payload["match_signal"] = dict(match_signal_details)
        payload["match_signal_label_sources"] = match_signal_details.get(
            "match_signal_label_sources",
            [],
        )
    return payload


def _build_comfort_debug_payload(
    candidates: list[CandidateItem],
    *,
    top_n: int = COMFORT_DEBUG_TOP_N,
    match_signal_details: dict | None = None,
) -> dict:
    if candidates and any("library_fit" in candidate.score_breakdown for candidate in candidates):
        return _build_movie_comfort_debug_payload(
            candidates,
            top_n=top_n,
            match_signal_details=match_signal_details,
        )

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
                "keyword_fit": round(float(score.get("keyword_fit", 0.0)), 6),
                "studio_fit": round(float(score.get("studio_fit", 0.0)), 6),
                "collection_fit": round(float(score.get("collection_fit", 0.0)), 6),
                "director_fit": round(float(score.get("director_fit", 0.0)), 6),
                "lead_cast_fit": round(float(score.get("lead_cast_fit", 0.0)), 6),
                "certification_fit": round(float(score.get("certification_fit", 0.0)), 6),
                "runtime_fit": round(float(score.get("runtime_fit", 0.0)), 6),
                "decade_fit": round(float(score.get("decade_fit", 0.0)), 6),
                "recent_shape_fit": round(float(score.get("recent_shape_fit", 0.0)), 6),
                "comfort_safety": round(float(score.get("comfort_safety", 0.0)), 6),
                "weak_shape_outlier": float(score.get("weak_shape_outlier", 0.0)) >= 1.0,
                "candidate_is_unrated": float(score.get("candidate_is_unrated", 0.0)) >= 1.0,
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

    payload = {
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
    if match_signal_details:
        payload["match_signal"] = dict(match_signal_details)
        payload["match_signal_label_sources"] = match_signal_details.get("match_signal_label_sources", [])
    return payload

