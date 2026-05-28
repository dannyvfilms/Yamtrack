"""Row match signal helpers for Discover."""

from __future__ import annotations

from collections import defaultdict

from app.discover.movie_comfort import (
    MOVIE_COMFORT_BUCKET_SOURCE_PRIORITY,
    MOVIE_COMFORT_FIT_KEYS,
    _candidate_signal_labels,
    _format_phase_label,
    _movie_comfort_candidate_families,
    _movie_comfort_reason_bucket_parts,
    _signal_phase_feature_maps,
    _top_phase_labels,
)
from app.discover.schemas import CandidateItem
from app.discover.service_helpers import (
    BEHAVIOR_FIRST_MEDIA_TYPES,
    _clamp_unit,
)

ROW_MATCH_SIGNAL_CANDIDATE_LIMIT = 12
ROW_MATCH_SIGNAL_ROWS = {
    "top_picks_for_you",
    "clear_out_next",
    "comfort_rewatches",
    "comfort_binge",
    "comfort",
    "comfort_replay",
    "comfort_picks",
}

def _comfort_match_signal(profile_payload: dict) -> str:
    """Build a row-level signal string from phase tag/genre activity."""
    feature_maps = _signal_phase_feature_maps(profile_payload)
    top_labels: list[str] = []

    # Prefer specific labels first, then broader generic tags/metadata, and use
    # plain genres only as the final fallback.
    for allow_generic_terms, allowed_sources in (
        (False, {source_name for source_name, _map in feature_maps if source_name != "genres"}),
        (True, {source_name for source_name, _map in feature_maps if source_name != "genres"}),
        (True, {"genres"}),
    ):
        for source_name, affinity_map in feature_maps:
            if source_name not in allowed_sources:
                continue
            for label in _top_phase_labels(
                affinity_map,
                limit=3,
                allow_generic_terms=allow_generic_terms,
            ):
                if label not in top_labels:
                    top_labels.append(label)
                if len(top_labels) >= 3:
                    break
            if len(top_labels) >= 3:
                break
        if len(top_labels) >= 3:
            break

    if not top_labels:
        return ""
    return "Driven by your current " + ", ".join(top_labels[:3]) + " phase"


def _movie_comfort_match_signal_with_details(
    candidates: list[CandidateItem],
) -> tuple[str | None, dict | None]:
    candidates_window = candidates[:ROW_MATCH_SIGNAL_CANDIDATE_LIMIT]
    if not candidates_window:
        return None, None

    label_scores: dict[tuple[str, str], float] = defaultdict(float)
    label_matches: dict[tuple[str, str], int] = defaultdict(int)
    window_size = max(1, len(candidates_window))

    for index, candidate in enumerate(candidates_window):
        score = candidate.score_breakdown
        rank_weight = 1.0 - ((index / window_size) * 0.35)
        evidence_weight = max(
            0.2,
            float(score.get("library_fit", 0.0)),
            float(score.get("recency_phase_fit", 0.0)),
            float(candidate.final_score or 0.0),
        ) * rank_weight

        bucket_source, bucket_label = _movie_comfort_reason_bucket_parts(candidate)
        if (
            bucket_source in MOVIE_COMFORT_BUCKET_SOURCE_PRIORITY
            and bucket_label
            and bucket_label not in {"personal", "general"}
        ):
            contribution = evidence_weight * max(
                0.2,
                float(score.get(MOVIE_COMFORT_FIT_KEYS[bucket_source], 0.0)),
            )
            label_scores[(bucket_source, bucket_label)] += contribution
            label_matches[(bucket_source, bucket_label)] += 1

        candidate_families = _movie_comfort_candidate_families(candidate)
        for family in ("certifications", "runtime_buckets", "decades"):
            fit_value = float(score.get(MOVIE_COMFORT_FIT_KEYS[family], 0.0))
            if fit_value <= 0.0:
                continue
            for label in candidate_families.get(family, []):
                contribution = evidence_weight * max(0.2, fit_value)
                label_scores[(family, label)] += contribution
                label_matches[(family, label)] += 1

    selected: list[tuple[str, str, float]] = []
    seen_labels: set[str] = set()

    rich_ranked = sorted(
        (
            (source, label, score_value)
            for (source, label), score_value in label_scores.items()
            if source in MOVIE_COMFORT_BUCKET_SOURCE_PRIORITY and score_value >= 0.15
        ),
        key=lambda item: item[2],
        reverse=True,
    )
    for source, label, score_value in rich_ranked:
        if label in seen_labels:
            continue
        seen_labels.add(label)
        selected.append((source, label, score_value))
        if len(selected) >= 3:
            break

    if len(selected) < 3:
        certification_ranked = sorted(
            (
                (source, label, score_value)
                for (source, label), score_value in label_scores.items()
                if source == "certifications" and score_value >= 0.15
            ),
            key=lambda item: item[2],
            reverse=True,
        )
        for source, label, score_value in certification_ranked:
            if label in seen_labels:
                continue
            seen_labels.add(label)
            selected.append((source, label, score_value))
            if len(selected) >= 3:
                break

    if len(selected) < 2:
        generic_ranked = sorted(
            (
                (source, label, score_value)
                for (source, label), score_value in label_scores.items()
                if source in {"runtime_buckets", "decades"} and score_value >= 0.15
            ),
            key=lambda item: item[2],
            reverse=True,
        )
        for source, label, score_value in generic_ranked:
            if label in seen_labels:
                continue
            seen_labels.add(label)
            selected.append((source, label, score_value))
            if len(selected) >= 3:
                break

    if not selected:
        return None, None

    signal_labels = [_format_phase_label(label) for _source, label, _score in selected]
    signal = "Driven by your current " + ", ".join(signal_labels[:3]) + " phase"
    debug_labels: list[dict[str, object]] = []
    explanation_parts: list[str] = []
    for source, label, score_value in selected:
        formatted_label = _format_phase_label(label)
        debug_labels.append(
            {
                "label": formatted_label,
                "score": round(float(score_value), 6),
                "matches": int(label_matches[(source, label)]),
                "top_sources": [source],
            },
        )
        explanation_parts.append(f"{formatted_label} (sources: {source})")

    return signal, {
        "mode": "movie_reason_buckets",
        "signal": signal,
        "candidate_window": len(candidates_window),
        "labels": debug_labels,
        "explanation": "Signal evidence: " + "; ".join(explanation_parts),
        "match_signal_label_sources": debug_labels,
    }


def _row_match_signal_with_details(
    row_key: str,
    candidates: list[CandidateItem],
    profile_payload: dict | None,
) -> tuple[str | None, dict | None]:
    if row_key not in ROW_MATCH_SIGNAL_ROWS:
        return None, None

    if (
        row_key == "comfort_rewatches"
        and candidates
        and all(candidate.media_type in BEHAVIOR_FIRST_MEDIA_TYPES for candidate in candidates)
        and any("primary_reason_bucket" in candidate.score_breakdown for candidate in candidates)
    ):
        movie_signal, movie_details = _movie_comfort_match_signal_with_details(candidates)
        if movie_signal:
            return movie_signal, movie_details

    label_scores: dict[str, float] = defaultdict(float)
    label_source_scores: dict[str, dict[str, dict[str, float] | int]] = defaultdict(
        lambda: {
            "sources": defaultdict(float),
            "matches": 0,
        },
    )
    candidates_window = candidates[:ROW_MATCH_SIGNAL_CANDIDATE_LIMIT]
    window_size = max(1, len(candidates_window))
    feature_maps = _signal_phase_feature_maps(profile_payload)

    for index, candidate in enumerate(candidates_window):
        rank_weight = 1.0 - ((index / window_size) * 0.35)
        phase_weight = _clamp_unit(
            float(
                candidate.score_breakdown.get(
                    "phase_fit",
                    candidate.final_score or 0.0,
                ),
            ),
        )
        evidence_weight = max(0.2, phase_weight) * rank_weight
        candidate_labels = _candidate_signal_labels(candidate)

        for source_name, affinity_map in feature_maps:
            source_values = candidate_labels.get(source_name) or set()
            if not source_values or not affinity_map:
                continue
            for raw_label, affinity in affinity_map.items():
                if raw_label not in source_values:
                    continue
                label = _format_phase_label(raw_label)
                if not label:
                    continue
                contribution = evidence_weight * max(0.2, float(affinity))
                label_scores[label] += contribution
                source_bucket = label_source_scores[label]
                source_bucket["sources"][source_name] += contribution
                source_bucket["matches"] += 1

    if not label_scores:
        fallback = _comfort_match_signal(profile_payload or {})
        if not fallback:
            return None, None
        return fallback, {
            "mode": "profile_fallback",
            "signal": fallback,
            "candidate_window": len(candidates_window),
            "labels": [],
            "explanation": "Signal fell back to phase profile affinity because row candidates had no direct label overlaps.",
        }

    ranked_labels = sorted(
        label_scores.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:3]
    labels = [label for label, _score in ranked_labels]
    if not labels:
        return None, None

    signal = "Driven by your current " + ", ".join(labels) + " phase"
    debug_labels: list[dict[str, object]] = []
    explanation_parts: list[str] = []
    for label, score_value in ranked_labels:
        source_bucket = label_source_scores[label]
        source_scores = source_bucket["sources"]
        top_sources = [
            str(key)
            for key, _value in sorted(
                source_scores.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:2]
        ]
        debug_labels.append(
            {
                "label": label,
                "score": round(float(score_value), 6),
                "matches": int(source_bucket["matches"]),
                "top_sources": top_sources,
            },
        )

        source_phrases: list[str] = []
        if top_sources:
            source_phrases.append("sources: " + ", ".join(top_sources))
        if source_phrases:
            explanation_parts.append(f"{label} ({'; '.join(source_phrases)})")
        else:
            explanation_parts.append(label)

    explanation = "Signal evidence: " + "; ".join(explanation_parts)
    return signal, {
        "mode": "row_candidates",
        "signal": signal,
        "candidate_window": len(candidates_window),
        "labels": debug_labels,
        "explanation": explanation,
        "match_signal_label_sources": debug_labels,
    }


def _row_match_signal(
    row_key: str,
    candidates: list[CandidateItem],
    profile_payload: dict | None,
) -> str | None:
    signal, _details = _row_match_signal_with_details(
        row_key,
        candidates,
        profile_payload,
    )
    return signal


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


