"""Movie comfort scoring pipeline for Discover."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from django.utils import timezone

from app.discover.feature_metadata import (
    SIGNAL_LABEL_STOPLIST,
    normalize_certification,
    normalize_collection,
    normalize_features,
    normalize_keyword,
    normalize_person_name,
    normalize_studio,
    release_decade_label,
    runtime_bucket_label,
)
from app.discover.provider_candidates import _iso_date
from app.discover.schemas import CandidateItem
from app.discover.scoring import (
    blended_world_quality,
    cosine_similarity,
    normalize_values,
)
from app.discover.service_helpers import (
    BEHAVIOR_FIRST_MEDIA_TYPES,
    COMFORT_MEDIUM_PHASE_SUPERIORITY_MARGIN,
    COMFORT_PHASE_LANE_QUOTA,
    COMFORT_PHASE_LANE_WINDOW,
    COMFORT_STRONG_PHASE_OPENING_WINDOW,
    MAX_ITEMS_PER_ROW,
    WORLD_QUALITY_MEDIA_TYPES,
    _activity_ordering,
    _clamp_unit,
    _entry_activity_datetime,
    _feature_vector,
    _feature_vector_norm,
    _holiday_seasonal_adjustment,
    _is_holiday_window,
    _item_credit_feature_maps,
    _item_studio_map,
    _model_for_media_type,
    _model_has_field,
)
from app.models import MediaTypes, Status

MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS = {
    "phase": 0.60,
    "recent": 0.40,
    "library": 0.70,
    "rewatch": 0.30,
}
MOVIE_COMFORT_FAMILY_WEIGHTS = {
    "keywords": 0.22,
    "collections": 0.18,
    "studios": 0.16,
    "genres": 0.14,
    "directors": 0.08,
    "lead_cast": 0.05,
    "certifications": 0.07,
    "runtime_buckets": 0.05,
    "decades": 0.05,
}
MOVIE_COMFORT_PHASE_PROFILE_KEYS = {
    "keywords": "phase_keyword_affinity",
    "collections": "phase_collection_affinity",
    "studios": "phase_studio_affinity",
    "genres": "phase_genre_affinity",
    "directors": "phase_director_affinity",
    "lead_cast": "phase_lead_cast_affinity",
    "certifications": "phase_certification_affinity",
    "runtime_buckets": "phase_runtime_bucket_affinity",
    "decades": "phase_decade_affinity",
}
MOVIE_COMFORT_RECENT_PROFILE_KEYS = {
    "keywords": "recent_keyword_affinity",
    "collections": "recent_collection_affinity",
    "studios": "recent_studio_affinity",
    "genres": "recent_genre_affinity",
    "directors": "recent_director_affinity",
    "lead_cast": "recent_lead_cast_affinity",
    "certifications": "recent_certification_affinity",
    "runtime_buckets": "recent_runtime_bucket_affinity",
    "decades": "recent_decade_affinity",
}
MOVIE_COMFORT_FIT_KEYS = {
    "keywords": "keyword_fit",
    "collections": "collection_fit",
    "studios": "studio_fit",
    "genres": "genre_fit",
    "directors": "director_fit",
    "lead_cast": "lead_cast_fit",
    "certifications": "certification_fit",
    "runtime_buckets": "runtime_fit",
    "decades": "decade_fit",
}
MOVIE_COMFORT_RICH_FAMILIES = (
    "keywords",
    "collections",
    "studios",
    "genres",
    "directors",
    "lead_cast",
)
MOVIE_COMFORT_BUCKET_SOURCE_PRIORITY = (
    "keywords",
    "collections",
    "studios",
    "genres",
    "directors",
    "lead_cast",
)
MOVIE_COMFORT_EXPLANATION_SOURCE_PRIORITY = (
    "keywords",
    "collections",
    "studios",
    "directors",
    "lead_cast",
    "genres",
    "certifications",
    "runtime_buckets",
    "decades",
)
MOVIE_COMFORT_GENERIC_SOURCES = {"certifications", "runtime_buckets", "decades"}
MOVIE_COMFORT_REASON_BUCKET_TARGET = MAX_ITEMS_PER_ROW
MOVIE_COMFORT_REASON_BUCKET_RELAX_INCREMENT = 1
MOVIE_COMFORT_COOLDOWN_DEFAULT_DAYS = 28.0
MOVIE_COMFORT_COOLDOWN_MIN_DAYS = 7.0
MOVIE_COMFORT_COOLDOWN_MAX_DAYS = 60.0
MOVIE_COMFORT_BURST_GAP_DAYS = 30.0
MOVIE_COMFORT_BURST_HISTORY_MIN_WATCHES = 3
MOVIE_COMFORT_RECENT_TITLE_MULTIPLIER_FLOOR = 0.72
MOVIE_COMFORT_READY_NOW_WEIGHT = 0.12
MOVIE_COMFORT_SATURATION_ELIGIBLE_DAYS = 30.0
MOVIE_COMFORT_SATURATION_WINDOW_90D = 90
MOVIE_COMFORT_SATURATION_WINDOW_180D = 180
MOVIE_COMFORT_SATURATION_GAP_TARGET_DAYS = 45.0
MOVIE_COMFORT_SATURATION_GAP_RANGE_DAYS = 30.0
MOVIE_COMFORT_SATURATION_WEIGHT = 0.45
MOVIE_TOP_PICKS_HISTORY_NEIGHBOR_TARGET = 6.0
MOVIE_TOP_PICKS_PLANNING_CONFIDENCE_WEIGHT = 0.05
WORLD_RATING_PROFILE_MIN_SAMPLE_SIZE = 5
WORLD_QUALITY_ALIGNMENT_BASELINE = 0.25
WORLD_QUALITY_ALIGNMENT_WEIGHT = 0.20
WORLD_QUALITY_ALIGNMENT_FLOOR = 0.10
WORLD_QUALITY_ALIGNMENT_CAP = 0.45
COMFORT_DEBUG_TOP_N = 12

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


def _profile_affinity_map(profile_payload: dict | None, *keys: str) -> dict[str, float]:
    profile = profile_payload or {}
    for key in keys:
        values = profile.get(key) or {}
        if not values:
            continue
        return {
            str(raw_key).strip().lower(): float(raw_value)
            for raw_key, raw_value in values.items()
            if str(raw_key).strip()
        }
    return {}


def _profile_exact_affinity_map(profile_payload: dict | None, key: str) -> dict[str, float]:
    values = (profile_payload or {}).get(key) or {}
    return {
        str(raw_key).strip().lower(): float(raw_value)
        for raw_key, raw_value in values.items()
        if str(raw_key).strip()
    }


def _movie_comfort_bundle_map(
    profile_payload: dict | None,
    bundle_key: str,
    family: str,
) -> dict[str, float]:
    bundle = (profile_payload or {}).get(bundle_key) or {}
    values = bundle.get(family) or {}
    return {
        str(raw_key).strip().lower(): float(raw_value)
        for raw_key, raw_value in values.items()
        if str(raw_key).strip()
    }


def _candidate_collection_labels(candidate: CandidateItem) -> list[str]:
    return normalize_features(
        [candidate.collection_name or candidate.collection_id],
        normalize_collection,
    )


def _movie_comfort_candidate_families(candidate: CandidateItem) -> dict[str, list[str]]:
    return {
        "keywords": normalize_features(candidate.keywords, normalize_keyword),
        "collections": _candidate_collection_labels(candidate),
        "studios": normalize_features(candidate.studios, normalize_studio),
        "genres": normalize_features(candidate.genres, normalize_person_name),
        "directors": normalize_features(candidate.directors, normalize_person_name),
        "lead_cast": normalize_features(candidate.lead_cast, normalize_person_name),
        "certifications": normalize_features([candidate.certification], normalize_certification),
        "runtime_buckets": normalize_features([candidate.runtime_bucket], normalize_person_name),
        "decades": normalize_features([candidate.release_decade], normalize_person_name),
    }


def _candidate_has_extended_movie_metadata(candidate: CandidateItem) -> bool:
    return any(
        [
            candidate.keywords,
            candidate.studios,
            candidate.directors,
            candidate.lead_cast,
            _candidate_collection_labels(candidate),
            normalize_features([candidate.certification], normalize_certification),
            normalize_features([candidate.runtime_bucket], normalize_person_name),
            normalize_features([candidate.release_decade], normalize_person_name),
        ],
    )


def _affinity_fit(values: list[str], affinity_map: dict[str, float]) -> float:
    if not values or not affinity_map:
        return 0.0
    return cosine_similarity(_feature_vector(values), affinity_map)


def _affinity_fit_from_vector(
    feature_vector: dict[str, float],
    feature_vector_norm: float,
    affinity_map: dict[str, float],
    affinity_norm: float,
) -> float:
    if not feature_vector or not affinity_map or feature_vector_norm <= 0.0 or affinity_norm <= 0.0:
        return 0.0
    dot = sum(
        float(value) * float(affinity_map.get(key, 0.0))
        for key, value in feature_vector.items()
    )
    return _clamp_unit(dot / (feature_vector_norm * affinity_norm))


def phase_fit_family(profile_payload: dict | None, family: str, values: list[str]) -> float:
    return _affinity_fit(
        values,
        _profile_exact_affinity_map(
            profile_payload,
            MOVIE_COMFORT_PHASE_PROFILE_KEYS[family],
        ),
    )


def recent_fit_family(profile_payload: dict | None, family: str, values: list[str]) -> float:
    return _affinity_fit(
        values,
        _profile_exact_affinity_map(
            profile_payload,
            MOVIE_COMFORT_RECENT_PROFILE_KEYS[family],
        ),
    )


def library_fit_family(profile_payload: dict | None, family: str, values: list[str]) -> float:
    return _affinity_fit(
        values,
        _movie_comfort_bundle_map(
            profile_payload,
            "comfort_library_affinity",
            family,
        ),
    )


def rewatch_fit_family(profile_payload: dict | None, family: str, values: list[str]) -> float:
    return _affinity_fit(
        values,
        _movie_comfort_bundle_map(
            profile_payload,
            "comfort_rewatch_affinity",
            family,
        ),
    )


def _movie_comfort_weighted_fit(family_fits: dict[str, float]) -> float:
    return _clamp_unit(
        sum(
            float(family_fits.get(family, 0.0)) * weight
            for family, weight in MOVIE_COMFORT_FAMILY_WEIGHTS.items()
        ),
    )


def _affinity_map_norm(affinity_map: dict[str, float]) -> float:
    if not affinity_map:
        return 0.0
    return math.sqrt(sum(float(value) ** 2 for value in affinity_map.values()))


def _singleton_affinity_fit(
    label: str,
    affinity_map: dict[str, float],
    affinity_norm: float,
) -> float:
    if not label or not affinity_map or affinity_norm <= 0.0:
        return 0.0
    return _clamp_unit(float(affinity_map.get(label, 0.0)) / affinity_norm)


def _movie_reason_label_strength(
    family_profile_maps: dict[str, dict[str, dict[str, float]]],
    family_profile_norms: dict[str, dict[str, float]],
    family: str,
    label: str,
    *,
    strength_cache: dict[tuple[str, str], float] | None = None,
) -> float:
    cache_key = (family, str(label).strip().lower())
    if strength_cache is not None and cache_key in strength_cache:
        return strength_cache[cache_key]

    family_layers = family_profile_maps.get(family) or {}
    family_norms = family_profile_norms.get(family) or {}
    phase_fit = _singleton_affinity_fit(
        cache_key[1],
        family_layers.get("phase") or {},
        float(family_norms.get("phase", 0.0)),
    )
    recent_fit = _singleton_affinity_fit(
        cache_key[1],
        family_layers.get("recent") or {},
        float(family_norms.get("recent", 0.0)),
    )
    library_fit = _singleton_affinity_fit(
        cache_key[1],
        family_layers.get("library") or {},
        float(family_norms.get("library", 0.0)),
    )
    rewatch_fit = _singleton_affinity_fit(
        cache_key[1],
        family_layers.get("rewatch") or {},
        float(family_norms.get("rewatch", 0.0)),
    )
    recency_phase_fit = (
        (phase_fit * MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS["phase"])
        + (recent_fit * MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS["recent"])
    )
    library_bundle_fit = (
        (library_fit * MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS["library"])
        + (rewatch_fit * MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS["rewatch"])
    )
    strength = _clamp_unit((library_bundle_fit * 0.55) + (recency_phase_fit * 0.45))
    if strength_cache is not None:
        strength_cache[cache_key] = strength
    return strength


def _movie_reason_bucket_label(
    family_profile_maps: dict[str, dict[str, dict[str, float]]],
    family_profile_norms: dict[str, dict[str, float]],
    candidate_families: dict[str, list[str]],
    *,
    rewatch_strength: float,
    strength_cache: dict[tuple[str, str], float] | None = None,
) -> tuple[str, str, str]:
    for family in MOVIE_COMFORT_BUCKET_SOURCE_PRIORITY:
        values = candidate_families.get(family) or []
        if not values:
            continue
        best_label = ""
        best_strength = 0.0
        for value in values:
            strength = _movie_reason_label_strength(
                family_profile_maps,
                family_profile_norms,
                family,
                value,
                strength_cache=strength_cache,
            )
            if strength > best_strength:
                best_label = value
                best_strength = strength
        if best_strength < 0.20 or not best_label:
            continue
        return f"{family}:{best_label}", family, best_label
    if rewatch_strength >= 0.50:
        return "rewatch:personal", "rewatch", "personal"
    return "broad:general", "broad", "general"


def _movie_item_feature_families(
    item: Item,
    *,
    studios: list[str],
    directors: list[str],
    lead_cast: list[str],
) -> dict[str, list[str]]:
    runtime_bucket = runtime_bucket_label(item.runtime_minutes)
    release_decade = release_decade_label(item.release_datetime)
    collection_labels = normalize_features(
        [item.provider_collection_name, item.provider_collection_id],
        normalize_collection,
    )
    return {
        "keywords": normalize_features(item.provider_keywords or [], normalize_keyword),
        "collections": collection_labels,
        "studios": studios or normalize_features(item.studios or [], normalize_studio),
        "genres": [
            str(genre).strip().lower()
            for genre in (item.genres or [])
            if str(genre).strip()
        ],
        "directors": directors,
        "lead_cast": lead_cast,
        "certifications": normalize_features([item.provider_certification], normalize_certification),
        "runtime_buckets": normalize_features([runtime_bucket], normalize_person_name),
        "decades": normalize_features([release_decade], normalize_person_name),
    }


def _movie_cadence_signal(
    activity_dts: list[datetime],
    *,
    now: datetime,
) -> dict[str, float]:
    ordered = sorted((dt for dt in activity_dts if dt), reverse=True)
    if not ordered:
        return {
            "watch_count": 0.0,
            "days_since_last_watch": 9999.0,
            "median_gap_days": MOVIE_COMFORT_COOLDOWN_DEFAULT_DAYS,
            "burstiness": 0.0,
        }

    days_since_last_watch = float(max(0, (now - ordered[0]).days))
    gaps = [
        float(max(0, (earlier - later).days))
        for earlier, later in zip(ordered, ordered[1:], strict=False)
    ]
    median_gap_days = (
        float(statistics.median(gaps))
        if gaps
        else MOVIE_COMFORT_COOLDOWN_DEFAULT_DAYS
    )
    recent_90_cutoff = now - timedelta(days=MOVIE_COMFORT_SATURATION_WINDOW_90D)
    recent_180_cutoff = now - timedelta(days=MOVIE_COMFORT_SATURATION_WINDOW_180D)
    recent_90_activity = [dt for dt in ordered if dt >= recent_90_cutoff]
    recent_180_activity = [dt for dt in ordered if dt >= recent_180_cutoff]
    recent_gaps = [
        float(max(0, (earlier - later).days))
        for earlier, later in zip(recent_180_activity, recent_180_activity[1:], strict=False)
    ]
    recent_gap_median_days = (
        float(statistics.median(recent_gaps))
        if recent_gaps
        else 0.0
    )
    burstiness = _clamp_unit(
        (
            sum(1 for gap in gaps if gap <= MOVIE_COMFORT_BURST_GAP_DAYS)
            / len(gaps)
        )
        if gaps
        else 0.0,
    )
    return {
        "watch_count": float(len(ordered)),
        "days_since_last_watch": days_since_last_watch,
        "median_gap_days": median_gap_days,
        "burstiness": burstiness,
        "recent_play_count_90d": float(len(recent_90_activity)),
        "recent_play_count_180d": float(len(recent_180_activity)),
        "recent_gap_median_days": recent_gap_median_days,
        "recent_gap_count": float(len(recent_gaps)),
    }


def _movie_title_saturation_signal(
    *,
    media_type: str,
    days_since_title_watch: float,
    title_signal: dict[str, float],
) -> dict[str, float]:
    recent_play_count_90d = float(title_signal.get("recent_play_count_90d", 0.0))
    recent_play_count_180d = float(title_signal.get("recent_play_count_180d", 0.0))
    recent_gap_median_days = float(title_signal.get("recent_gap_median_days", 0.0))
    recent_gap_count = int(title_signal.get("recent_gap_count", 0.0) or 0)

    if (
        media_type != MediaTypes.MOVIE.value
        or days_since_title_watch < MOVIE_COMFORT_SATURATION_ELIGIBLE_DAYS
    ):
        return {
            "recent_play_count_90d": round(recent_play_count_90d, 6),
            "recent_play_count_180d": round(recent_play_count_180d, 6),
            "recent_gap_median_days": round(recent_gap_median_days, 6),
            "title_saturation_penalty": 0.0,
            "saturation_multiplier": 1.0,
        }

    plays_90_pressure = _clamp_unit((recent_play_count_90d - 1.0) / 3.0)
    plays_180_pressure = _clamp_unit((recent_play_count_180d - 2.0) / 4.0)
    if recent_gap_count > 0:
        recent_gap_pressure = _clamp_unit(
            (
                MOVIE_COMFORT_SATURATION_GAP_TARGET_DAYS
                - recent_gap_median_days
            )
            / MOVIE_COMFORT_SATURATION_GAP_RANGE_DAYS,
        )
    else:
        recent_gap_pressure = 0.0
    title_saturation_penalty = _clamp_unit(
        (plays_90_pressure * 0.50)
        + (plays_180_pressure * 0.30)
        + (recent_gap_pressure * 0.20),
    )
    saturation_multiplier = _clamp_unit(
        1.0 - (MOVIE_COMFORT_SATURATION_WEIGHT * title_saturation_penalty),
    )
    return {
        "recent_play_count_90d": round(recent_play_count_90d, 6),
        "recent_play_count_180d": round(recent_play_count_180d, 6),
        "recent_gap_median_days": round(recent_gap_median_days, 6),
        "title_saturation_penalty": round(title_saturation_penalty, 6),
        "saturation_multiplier": round(saturation_multiplier, 6),
    }


def _movie_top_picks_planning_confidence(
    candidate: CandidateItem,
    *,
    candidate_families: dict[str, list[str]],
    family_layer_fits: dict[str, dict[str, float]],
    cooldown_context: dict[str, dict],
) -> dict[str, object]:
    if (
        candidate.media_type != MediaTypes.MOVIE.value
        or candidate.row_key != "top_picks_for_you"
        or float(candidate.score_breakdown.get("planning_entry", 0.0)) < 1.0
    ):
        return {
            "planning_confidence": 0.0,
            "planning_confidence_bonus": 0.0,
            "similar_watched_count": 0,
            "matched_history_families": [],
            "rich_history_family_count": 0,
            "generic_history_family_count": 0,
        }

    history_items_by_family_label = cooldown_context.get("history_items_by_family_label") or {}
    if not history_items_by_family_label:
        return {
            "planning_confidence": 0.0,
            "planning_confidence_bonus": 0.0,
            "similar_watched_count": 0,
            "matched_history_families": [],
            "rich_history_family_count": 0,
            "generic_history_family_count": 0,
        }

    matched_history_families: list[str] = []
    matched_history_items: set[int] = set()
    weighted_history_fit = 0.0
    rich_history_family_count = 0
    generic_history_family_count = 0
    total_family_weight = sum(MOVIE_COMFORT_FAMILY_WEIGHTS.values()) or 1.0

    for family in MOVIE_COMFORT_FAMILY_WEIGHTS:
        family_values = candidate_families.get(family) or []
        family_history_items = history_items_by_family_label.get(family) or {}
        family_match_items: set[int] = set()
        for value in family_values:
            family_match_items.update(family_history_items.get(value, set()))
        if not family_match_items:
            continue
        matched_history_families.append(family)
        matched_history_items.update(family_match_items)
        weighted_history_fit += (
            MOVIE_COMFORT_FAMILY_WEIGHTS[family]
            * float(family_layer_fits.get(family, {}).get("blended", 0.0))
        )
        if family in MOVIE_COMFORT_RICH_FAMILIES:
            rich_history_family_count += 1
        else:
            generic_history_family_count += 1

    if not matched_history_families:
        return {
            "planning_confidence": 0.0,
            "planning_confidence_bonus": 0.0,
            "similar_watched_count": 0,
            "matched_history_families": [],
            "rich_history_family_count": 0,
            "generic_history_family_count": 0,
        }

    weighted_history_fit = _clamp_unit(weighted_history_fit / total_family_weight)
    rich_history_coverage = _clamp_unit(
        rich_history_family_count / max(1, len(MOVIE_COMFORT_RICH_FAMILIES)),
    )
    generic_history_coverage = _clamp_unit(
        generic_history_family_count / max(1, len(MOVIE_COMFORT_GENERIC_SOURCES)),
    )
    similar_watched_count = len(matched_history_items)
    history_neighbor_norm = _clamp_unit(
        similar_watched_count / MOVIE_TOP_PICKS_HISTORY_NEIGHBOR_TARGET,
    )
    planning_confidence = _clamp_unit(
        (weighted_history_fit * 0.40)
        + (rich_history_coverage * 0.25)
        + (history_neighbor_norm * 0.25)
        + (generic_history_coverage * 0.10),
    )
    planning_confidence_bonus = planning_confidence * MOVIE_TOP_PICKS_PLANNING_CONFIDENCE_WEIGHT

    return {
        "planning_confidence": round(planning_confidence, 6),
        "planning_confidence_bonus": round(planning_confidence_bonus, 6),
        "similar_watched_count": int(similar_watched_count),
        "matched_history_families": matched_history_families,
        "rich_history_family_count": int(rich_history_family_count),
        "generic_history_family_count": int(generic_history_family_count),
    }


def _movie_comfort_cooldown_context(
    user,
    candidates: list[CandidateItem],
) -> dict[str, dict]:
    if not user or not candidates:
        return {"title": {}, "family": {}, "history_items_by_family_label": {}}

    candidate_media_types = {
        candidate.media_type
        for candidate in candidates
        if candidate.media_type
    }
    if len(candidate_media_types) != 1:
        return {"title": {}, "family": {}, "history_items_by_family_label": {}}
    media_type = next(iter(candidate_media_types))
    if media_type not in BEHAVIOR_FIRST_MEDIA_TYPES:
        return {"title": {}, "family": {}, "history_items_by_family_label": {}}

    model = _model_for_media_type(media_type)
    if not model:
        return {"title": {}, "family": {}, "history_items_by_family_label": {}}

    entry_only_fields = [
        "item_id",
        "created_at",
        "item__id",
        "item__source",
        "item__media_id",
        "item__genres",
        "item__provider_keywords",
        "item__provider_certification",
        "item__provider_collection_id",
        "item__provider_collection_name",
        "item__release_datetime",
        "item__runtime_minutes",
        "item__studios",
    ]
    if _model_has_field(model, "progressed_at"):
        entry_only_fields.append("progressed_at")
    if _model_has_field(model, "end_date"):
        entry_only_fields.append("end_date")

    entries = list(
        model.objects.filter(user=user, status=Status.COMPLETED.value)
        .select_related("item")
        .only(*entry_only_fields)
        .order_by(*_activity_ordering(model))
    )
    if not entries:
        return {"title": {}, "family": {}, "history_items_by_family_label": {}}

    item_ids = sorted({entry.item_id for entry in entries if entry.item_id})
    studio_map = _item_studio_map(item_ids)
    _people_map, directors_map, lead_cast_map = _item_credit_feature_maps(item_ids)
    now = timezone.now()

    title_activity: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    family_activity: dict[str, dict[str, list[datetime]]] = {
        family: defaultdict(list) for family in MOVIE_COMFORT_FAMILY_WEIGHTS
    }
    history_items_by_family_label: dict[str, dict[str, set[int]]] = {
        family: defaultdict(set) for family in MOVIE_COMFORT_FAMILY_WEIGHTS
    }

    for entry in entries:
        activity_dt = _entry_activity_datetime(entry)
        if not activity_dt or not getattr(entry, "item", None):
            continue
        item = entry.item
        title_key = (str(item.source or "").strip(), str(item.media_id or "").strip())
        title_activity[title_key].append(activity_dt)
        item_families = _movie_item_feature_families(
            item,
            studios=studio_map.get(item.id, []),
            directors=directors_map.get(item.id, []),
            lead_cast=lead_cast_map.get(item.id, []),
        )
        for family, values in item_families.items():
            for value in values:
                family_activity[family][value].append(activity_dt)
                history_items_by_family_label[family][value].add(item.id)

    title_signals = {
        key: _movie_cadence_signal(activity_dts, now=now)
        for key, activity_dts in title_activity.items()
    }
    family_signals = {
        family: {
            label: _movie_cadence_signal(activity_dts, now=now)
            for label, activity_dts in label_map.items()
        }
        for family, label_map in family_activity.items()
    }
    return {
        "title": title_signals,
        "family": family_signals,
        "history_items_by_family_label": history_items_by_family_label,
    }


def _movie_ready_now_signal(
    candidate: CandidateItem,
    candidate_families: dict[str, list[str]],
    cooldown_context: dict[str, dict],
) -> dict[str, float]:
    title_key = (str(candidate.source or "").strip(), str(candidate.media_id or "").strip())
    title_signal = (cooldown_context.get("title") or {}).get(title_key, {})
    title_history_present = 1.0 if title_signal else 0.0
    planning_entry = float(candidate.score_breakdown.get("planning_entry", 0.0)) >= 1.0
    release_status = _candidate_release_status(candidate)
    release_ready_score = 0.0 if release_status == "upcoming" else 1.0

    title_watch_count = float(title_signal.get("watch_count", 0.0))
    if title_signal:
        days_since_title_watch = float(
            title_signal.get(
                "days_since_last_watch",
                candidate.score_breakdown.get("days_since_activity", 9999.0),
            ),
        )
        title_burstiness = float(title_signal.get("burstiness", 0.0))
        median_gap_days = float(
            title_signal.get("median_gap_days", MOVIE_COMFORT_COOLDOWN_DEFAULT_DAYS),
        )
    else:
        days_since_title_watch = float(
            candidate.score_breakdown.get("days_since_planned", 0.0)
            if planning_entry
            else 0.0,
        )
        title_burstiness = 0.0
        median_gap_days = 0.0

    lane_burstiness = 0.0
    lane_days_since_watch = 9999.0
    lane_watch_count = 0.0
    family_context = cooldown_context.get("family") or {}
    for family in MOVIE_COMFORT_RICH_FAMILIES:
        family_values = candidate_families.get(family) or []
        family_signal_map = family_context.get(family) or {}
        for value in family_values:
            family_signal = family_signal_map.get(value)
            if not family_signal:
                continue
            lane_burstiness = max(lane_burstiness, float(family_signal.get("burstiness", 0.0)))
            lane_days_since_watch = min(
                lane_days_since_watch,
                float(family_signal.get("days_since_last_watch", 9999.0)),
            )
            lane_watch_count = max(
                lane_watch_count,
                float(family_signal.get("watch_count", 0.0)),
            )

    if title_signal:
        cooldown_window_days = (
            median_gap_days
            if title_watch_count >= 2
            else MOVIE_COMFORT_COOLDOWN_DEFAULT_DAYS
        )
        cooldown_window_days = max(
            MOVIE_COMFORT_COOLDOWN_MIN_DAYS,
            min(MOVIE_COMFORT_COOLDOWN_MAX_DAYS, cooldown_window_days),
        )

        title_cooldown_penalty = _clamp_unit(
            1.0 - (days_since_title_watch / max(cooldown_window_days, 1.0)),
        )
        burst_replay_allowance = 0.0
        cooldown_penalty = title_cooldown_penalty
        ready_now_score = _clamp_unit(1.0 - cooldown_penalty)
        saturation_signal = _movie_title_saturation_signal(
            media_type=candidate.media_type,
            days_since_title_watch=days_since_title_watch,
            title_signal=title_signal,
        )
    else:
        cooldown_window_days = 0.0
        title_cooldown_penalty = 0.0
        burst_replay_allowance = 0.0
        cooldown_penalty = _clamp_unit(1.0 - release_ready_score)
        ready_now_score = release_ready_score
        saturation_signal = {
            "recent_play_count_90d": 0.0,
            "recent_play_count_180d": 0.0,
            "recent_gap_median_days": 0.0,
            "title_saturation_penalty": 0.0,
            "saturation_multiplier": 1.0,
        }

    return {
        "days_since_title_watch": round(days_since_title_watch, 6),
        "title_watch_count": round(title_watch_count, 6),
        "title_burstiness": round(title_burstiness, 6),
        "title_repeat_gap_days": round(median_gap_days, 6),
        "lane_burstiness": round(lane_burstiness, 6),
        "lane_days_since_watch": round(lane_days_since_watch, 6),
        "lane_watch_count": round(lane_watch_count, 6),
        "cooldown_window_days": round(cooldown_window_days, 6),
        "title_cooldown_penalty": round(title_cooldown_penalty, 6),
        "burst_replay_allowance": round(burst_replay_allowance, 6),
        "cooldown_penalty": round(cooldown_penalty, 6),
        "ready_now_score": round(ready_now_score, 6),
        "recent_play_count_90d": float(saturation_signal["recent_play_count_90d"]),
        "recent_play_count_180d": float(saturation_signal["recent_play_count_180d"]),
        "recent_gap_median_days": float(saturation_signal["recent_gap_median_days"]),
        "title_saturation_penalty": float(saturation_signal["title_saturation_penalty"]),
        "saturation_multiplier": float(saturation_signal["saturation_multiplier"]),
        "title_history_present": round(title_history_present, 6),
        "release_ready_score": round(release_ready_score, 6),
        "release_status": release_status,
    }


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


def _candidate_release_date_value(candidate: CandidateItem):
    iso_date = _iso_date(candidate.release_date)
    if not iso_date:
        return None
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").date()
    except ValueError:
        return None


def _candidate_release_status(
    candidate: CandidateItem,
    *,
    today=None,
) -> str:
    release_date = _candidate_release_date_value(candidate)
    if release_date is None:
        return "unknown"
    if today is None:
        today = timezone.localdate()
    return "upcoming" if release_date > today else "released"


def _format_phase_label(value: str) -> str:
    key = str(value).strip().lower()
    if not key:
        return ""
    if key == "<90":
        return "Under 90 Minutes"
    if key == "130_plus":
        return "130+ Minutes"
    if "_" in key:
        lower, upper = key.split("_", 1)
        if lower.isdigit() and upper.isdigit():
            return f"{int(lower)}-{int(upper)} Minutes"
    return " ".join(part.capitalize() for part in key.replace("_", " ").replace("-", " ").split())


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
        if key in SIGNAL_LABEL_STOPLIST:
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


def _signal_phase_feature_maps(profile_payload: dict | None) -> list[tuple[str, dict[str, float]]]:
    return [
        ("keywords", _profile_affinity_map(profile_payload, "phase_keyword_affinity")),
        ("collections", _profile_affinity_map(profile_payload, "phase_collection_affinity")),
        ("studios", _profile_affinity_map(profile_payload, "phase_studio_affinity")),
        ("directors", _profile_affinity_map(profile_payload, "phase_director_affinity")),
        ("lead_cast", _profile_affinity_map(profile_payload, "phase_lead_cast_affinity")),
        ("certifications", _profile_affinity_map(profile_payload, "phase_certification_affinity")),
        ("runtime_buckets", _profile_affinity_map(profile_payload, "phase_runtime_bucket_affinity")),
        ("decades", _profile_affinity_map(profile_payload, "phase_decade_affinity")),
        ("tags", _profile_affinity_map(profile_payload, "phase_tag_affinity")),
        ("genres", _profile_affinity_map(profile_payload, "phase_genre_affinity")),
    ]


def _candidate_signal_labels(candidate: CandidateItem) -> dict[str, set[str]]:
    return {
        "keywords": set(candidate.keywords or []),
        "collections": set(_candidate_collection_labels(candidate)),
        "studios": set(candidate.studios or []),
        "directors": set(candidate.directors or []),
        "lead_cast": set(candidate.lead_cast or []),
        "certifications": set(normalize_features([candidate.certification], normalize_certification)),
        "runtime_buckets": set(normalize_features([candidate.runtime_bucket], normalize_person_name)),
        "decades": set(normalize_features([candidate.release_decade], normalize_person_name)),
        "tags": {
            str(tag).strip().lower()
            for tag in (candidate.tags or [])
            if str(tag).strip()
        },
        "genres": {
            str(genre).strip().lower()
            for genre in (candidate.genres or [])
            if str(genre).strip()
        },
    }


def _movie_comfort_bucket_sort_key(candidate: CandidateItem) -> tuple[float, float, float, float]:
    return (
        float(candidate.final_score or 0.0),
        float(candidate.score_breakdown.get("library_fit", 0.0)),
        float(candidate.score_breakdown.get("recency_phase_fit", 0.0)),
        float(candidate.score_breakdown.get("behavior_score", 0.0)),
    )


def _movie_comfort_legacy_sort_key(candidate: CandidateItem) -> tuple[float, float, float, float]:
    return (
        float(candidate.score_breakdown.get("legacy_final_score", 0.0)),
        float(candidate.score_breakdown.get("library_fit", 0.0)),
        float(candidate.score_breakdown.get("recency_phase_fit", 0.0)),
        float(candidate.score_breakdown.get("behavior_score", 0.0)),
    )


def _world_rating_profile(profile_payload: dict | None) -> dict[str, float]:
    raw_profile = (profile_payload or {}).get("world_rating_profile") or {}
    try:
        sample_size = max(int(raw_profile.get("sample_size", 0) or 0), 0)
    except (TypeError, ValueError):
        sample_size = 0
    try:
        alignment = max(-1.0, min(1.0, float(raw_profile.get("alignment", 0.0) or 0.0)))
    except (TypeError, ValueError):
        alignment = 0.0
    try:
        confidence = _clamp_unit(float(raw_profile.get("confidence", 0.0) or 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "sample_size": float(sample_size),
        "alignment": alignment,
        "confidence": confidence,
    }


def _candidate_world_quality_signal(candidate: CandidateItem) -> dict[str, float | str]:
    score = candidate.score_breakdown
    provider_rating = score.get("provider_rating")
    provider_rating_count = score.get("provider_rating_count")
    trakt_rating = score.get("trakt_rating")
    trakt_rating_count = score.get("trakt_rating_count")
    return blended_world_quality(
        provider_rating=(
            float(provider_rating)
            if provider_rating is not None
            else None
        ),
        provider_votes=(
            int(provider_rating_count)
            if provider_rating_count is not None
            else None
        ),
        trakt_rating=(
            float(trakt_rating)
            if trakt_rating is not None
            else None
        ),
        trakt_votes=(
            int(trakt_rating_count)
            if trakt_rating_count is not None
            else None
        ),
    )


def _aligned_world_quality(
    candidate: CandidateItem,
    profile_payload: dict | None,
) -> dict[str, float | str]:
    world_signal = _candidate_world_quality_signal(candidate)
    world_profile = _world_rating_profile(profile_payload)
    raw_world_quality = float(world_signal.get("world_quality", 0.5))
    sample_size = int(world_profile["sample_size"])
    alignment = float(world_profile["alignment"])
    confidence = float(world_profile["confidence"])

    if sample_size < WORLD_RATING_PROFILE_MIN_SAMPLE_SIZE:
        aligned_world_quality = 0.5
    else:
        alignment_scale = _clamp_unit(
            WORLD_QUALITY_ALIGNMENT_BASELINE
            + (alignment * WORLD_QUALITY_ALIGNMENT_WEIGHT * confidence),
        )
        alignment_scale = max(
            WORLD_QUALITY_ALIGNMENT_FLOOR,
            min(WORLD_QUALITY_ALIGNMENT_CAP, alignment_scale),
        )
        aligned_world_quality = _clamp_unit(
            0.5 + ((raw_world_quality - 0.5) * alignment_scale),
        )

    return {
        **world_signal,
        "aligned_world_quality": aligned_world_quality,
        "world_alignment": alignment,
        "world_alignment_confidence": confidence,
        "world_alignment_sample_size": float(sample_size),
    }


def _movie_comfort_reason_bucket_parts(candidate: CandidateItem) -> tuple[str, str]:
    bucket = str(candidate.score_breakdown.get("primary_reason_bucket", "broad:general"))
    if ":" in bucket:
        source, label = bucket.split(":", 1)
        return source, label
    return bucket, ""


def _apply_movie_reason_bucket_quotas(
    candidates: list[CandidateItem],
    *,
    target: int = MOVIE_COMFORT_REASON_BUCKET_TARGET,
) -> list[CandidateItem]:
    if not candidates:
        return candidates

    ordered = sorted(candidates, key=_movie_comfort_bucket_sort_key, reverse=True)
    selected: list[CandidateItem] = []
    deferred: list[CandidateItem] = []
    counts: dict[str, int] = defaultdict(int)
    target_count = min(target, len(ordered))

    for candidate in ordered:
        bucket = str(candidate.score_breakdown.get("primary_reason_bucket", "broad:general"))
        if len(selected) >= target_count:
            candidate.score_breakdown.setdefault("reason_bucket_quota_action", "reserve")
            continue
        base_limit = 2 if len(selected) < 8 else 3
        if counts[bucket] >= base_limit:
            candidate.score_breakdown["reason_bucket_quota_action"] = "deferred"
            deferred.append(candidate)
            continue
        counts[bucket] += 1
        candidate.score_breakdown["reason_bucket_quota_action"] = "selected"
        selected.append(candidate)

    remaining = deferred[:]
    if len(selected) < target_count:
        still_deferred: list[CandidateItem] = []
        for candidate in remaining:
            if len(selected) >= target_count:
                break
            bucket = str(candidate.score_breakdown.get("primary_reason_bucket", "broad:general"))
            base_limit = 2 if len(selected) < 8 else 3
            relaxed_limit = base_limit + MOVIE_COMFORT_REASON_BUCKET_RELAX_INCREMENT
            if counts[bucket] >= relaxed_limit:
                still_deferred.append(candidate)
                continue
            counts[bucket] += 1
            candidate.score_breakdown["reason_bucket_quota_action"] = "relaxed_fill"
            selected.append(candidate)
        remaining = still_deferred

    if len(selected) < target_count:
        for candidate in remaining:
            if len(selected) >= target_count:
                break
            bucket = str(candidate.score_breakdown.get("primary_reason_bucket", "broad:general"))
            counts[bucket] += 1
            candidate.score_breakdown["reason_bucket_quota_action"] = "forced_fill"
            selected.append(candidate)

    selected_ids = {id(candidate) for candidate in selected}
    tail = [candidate for candidate in ordered if id(candidate) not in selected_ids]
    candidates[:] = [*selected, *tail]
    return candidates


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


def _apply_movie_comfort_confidence(
    candidates: list[CandidateItem],
    profile_payload: dict | None,
    *,
    user=None,
    phase_genre_affinity: dict[str, float],
) -> list[CandidateItem]:
    initial_candidate_positions = {
        id(candidate): position
        for position, candidate in enumerate(candidates, start=1)
    }
    family_profile_maps = {
        family: {
            "phase": _profile_exact_affinity_map(
                profile_payload,
                MOVIE_COMFORT_PHASE_PROFILE_KEYS[family],
            ),
            "recent": _profile_exact_affinity_map(
                profile_payload,
                MOVIE_COMFORT_RECENT_PROFILE_KEYS[family],
            ),
            "library": _movie_comfort_bundle_map(
                profile_payload,
                "comfort_library_affinity",
                family,
            ),
            "rewatch": _movie_comfort_bundle_map(
                profile_payload,
                "comfort_rewatch_affinity",
                family,
            ),
        }
        for family in MOVIE_COMFORT_FAMILY_WEIGHTS
    }
    family_profile_norms = {
        family: {
            layer_name: _affinity_map_norm(layer_map)
            for layer_name, layer_map in family_layers.items()
        }
        for family, family_layers in family_profile_maps.items()
    }
    if not any(
        any(layer_map for layer_map in family_layers.values())
        for family_layers in family_profile_maps.values()
    ):
        return candidates

    cooldown_context = _movie_comfort_cooldown_context(user, candidates)
    popularity_norm = normalize_values([candidate.popularity for candidate in candidates])
    rating_count_norm = normalize_values([candidate.rating_count for candidate in candidates])
    holiday_window_active = _is_holiday_window()
    reason_label_strength_cache: dict[tuple[str, str], float] = {}

    for index, candidate in enumerate(candidates):
        candidate_families = _movie_comfort_candidate_families(candidate)
        family_layer_fits: dict[str, dict[str, float]] = {}
        evaluated_signal_families = list(MOVIE_COMFORT_FAMILY_WEIGHTS.keys())
        active_signal_families: list[str] = []
        suppressed_map: dict[str, str] = {}

        for family in MOVIE_COMFORT_FAMILY_WEIGHTS:
            values = candidate_families.get(family) or []
            feature_vector = _feature_vector(values)
            feature_vector_norm = _feature_vector_norm(feature_vector)
            phase_fit = _affinity_fit_from_vector(
                feature_vector,
                feature_vector_norm,
                family_profile_maps[family]["phase"],
                float(family_profile_norms[family]["phase"]),
            )
            recent_fit = _affinity_fit_from_vector(
                feature_vector,
                feature_vector_norm,
                family_profile_maps[family]["recent"],
                float(family_profile_norms[family]["recent"]),
            )
            library_family_fit = _affinity_fit_from_vector(
                feature_vector,
                feature_vector_norm,
                family_profile_maps[family]["library"],
                float(family_profile_norms[family]["library"]),
            )
            rewatch_family_fit = _affinity_fit_from_vector(
                feature_vector,
                feature_vector_norm,
                family_profile_maps[family]["rewatch"],
                float(family_profile_norms[family]["rewatch"]),
            )
            recency_phase_family_fit = _clamp_unit(
                (phase_fit * MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS["phase"])
                + (recent_fit * MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS["recent"]),
            )
            library_blend_family_fit = _clamp_unit(
                (library_family_fit * MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS["library"])
                + (rewatch_family_fit * MOVIE_COMFORT_PROFILE_LAYER_WEIGHTS["rewatch"]),
            )
            blended_fit = _clamp_unit(
                (library_blend_family_fit * 0.55) + (recency_phase_family_fit * 0.45),
            )
            family_layer_fits[family] = {
                "phase": round(phase_fit, 6),
                "recent": round(recent_fit, 6),
                "library": round(library_family_fit, 6),
                "rewatch": round(rewatch_family_fit, 6),
                "recency_phase": round(recency_phase_family_fit, 6),
                "library_blend": round(library_blend_family_fit, 6),
                "blended": round(blended_fit, 6),
            }
            if not values:
                suppressed_map[family] = "no_candidate_feature"
            elif not any(family_profile_maps[family].values()):
                suppressed_map[family] = "no_profile_signal"
            elif blended_fit >= 0.20:
                active_signal_families.append(family)

        recency_phase_fit = _movie_comfort_weighted_fit(
            {
                family: family_layer_fits[family]["recency_phase"]
                for family in MOVIE_COMFORT_FAMILY_WEIGHTS
            },
        )
        library_fit = _movie_comfort_weighted_fit(
            {
                family: family_layer_fits[family]["library_blend"]
                for family in MOVIE_COMFORT_FAMILY_WEIGHTS
            },
        )

        user_score = candidate.score_breakdown.get("user_score")
        if user_score is not None:
            rating_confidence = _clamp_unit(
                max(
                    0.35,
                    1.0 - (0.5 ** ((float(user_score) - 5.0) / 2.5)),
                ),
            )
        else:
            rating_confidence = 0.50

        rewatch_count = max(
            1.0,
            float(candidate.score_breakdown.get("rewatch_count", 1.0)),
        )
        rewatch_strength = _clamp_unit(
            math.log1p(rewatch_count - 1) / math.log(6)
            if rewatch_count > 1
            else 0.0,
        )
        inactivity_norm = _clamp_unit(
            float(candidate.score_breakdown.get("days_since_activity", 0.0)) / 730.0,
        )
        behavior_score = _clamp_unit(
            (rating_confidence * 0.45)
            + (rewatch_strength * 0.35)
            + (inactivity_norm * 0.20),
        )
        provider_support = _clamp_unit(
            (popularity_norm[index] + rating_count_norm[index]) / 2.0,
        )
        world_quality_signal = _aligned_world_quality(candidate, profile_payload)
        legacy_quality_score = _clamp_unit(
            (rating_confidence * 0.55) + (provider_support * 0.45),
        )
        world_quality_active = (
            candidate.media_type in WORLD_QUALITY_MEDIA_TYPES
            and int(world_quality_signal["world_alignment_sample_size"])
            >= WORLD_RATING_PROFILE_MIN_SAMPLE_SIZE
        )
        if world_quality_active:
            quality_score = _clamp_unit(
                (rating_confidence * 0.45)
                + (provider_support * 0.30)
                + (float(world_quality_signal["aligned_world_quality"]) * 0.25),
            )
        else:
            quality_score = legacy_quality_score
        certification_fit = family_layer_fits["certifications"]["blended"]
        runtime_fit = family_layer_fits["runtime_buckets"]["blended"]
        decade_fit = family_layer_fits["decades"]["blended"]
        comfort_safety = _clamp_unit(
            (certification_fit * 0.50)
            + (runtime_fit * 0.25)
            + (quality_score * 0.25),
        )
        legacy_comfort_safety = _clamp_unit(
            (certification_fit * 0.50)
            + (runtime_fit * 0.25)
            + (legacy_quality_score * 0.25),
        )

        rich_family_fits = {
            family: family_layer_fits[family]["blended"]
            for family in MOVIE_COMFORT_RICH_FAMILIES
        }
        shape_coverage = _clamp_unit(
            sum(
                1
                for family_fit in rich_family_fits.values()
                if family_fit >= 0.20
            )
            / 4.0,
        )
        generic_only_match = (
            1.0
            if max(rich_family_fits.values(), default=0.0) < 0.20
            and max(certification_fit, runtime_fit, decade_fit) > 0.0
            else 0.0
        )

        core_affinity_score = _clamp_unit(
            (library_fit * 0.30)
            + (recency_phase_fit * 0.25)
            + (behavior_score * 0.20)
            + (comfort_safety * 0.10)
            + (quality_score * 0.10)
            + (shape_coverage * 0.05),
        )
        legacy_core_affinity_score = _clamp_unit(
            (library_fit * 0.30)
            + (recency_phase_fit * 0.25)
            + (behavior_score * 0.20)
            + (legacy_comfort_safety * 0.10)
            + (legacy_quality_score * 0.10)
            + (shape_coverage * 0.05),
        )
        if (
            generic_only_match >= 1.0
            and library_fit < 0.40
            and rewatch_strength < 0.35
        ):
            core_affinity_score = _clamp_unit(core_affinity_score * 0.86)
            legacy_core_affinity_score = _clamp_unit(legacy_core_affinity_score * 0.86)
            for family in MOVIE_COMFORT_GENERIC_SOURCES:
                if family_layer_fits[family]["blended"] > 0.0:
                    suppressed_map[family] = "downweighted_generic"
            active_signal_families = [
                family
                for family in active_signal_families
                if family not in MOVIE_COMFORT_GENERIC_SOURCES
            ]

        cooldown_signal = _movie_ready_now_signal(
            candidate,
            candidate_families,
            cooldown_context,
        )
        ready_now_score = float(cooldown_signal["ready_now_score"])
        raw_final_score = _clamp_unit(
            (core_affinity_score * (1.0 - MOVIE_COMFORT_READY_NOW_WEIGHT))
            + (ready_now_score * MOVIE_COMFORT_READY_NOW_WEIGHT),
        )
        legacy_raw_final_score = _clamp_unit(
            (legacy_core_affinity_score * (1.0 - MOVIE_COMFORT_READY_NOW_WEIGHT))
            + (ready_now_score * MOVIE_COMFORT_READY_NOW_WEIGHT),
        )
        if float(cooldown_signal["cooldown_penalty"]) > 0.0:
            floor_multiplier = MOVIE_COMFORT_RECENT_TITLE_MULTIPLIER_FLOOR + (
                (1.0 - MOVIE_COMFORT_RECENT_TITLE_MULTIPLIER_FLOOR)
                * (1.0 - float(cooldown_signal["cooldown_penalty"]))
            )
            raw_final_score = min(
                raw_final_score,
                _clamp_unit(core_affinity_score * floor_multiplier),
            )
            legacy_raw_final_score = min(
                legacy_raw_final_score,
                _clamp_unit(legacy_core_affinity_score * floor_multiplier),
            )

        saturation_multiplier = float(cooldown_signal["saturation_multiplier"])
        saturation_penalty_contribution = 0.0
        if candidate.media_type == MediaTypes.MOVIE.value and saturation_multiplier < 0.999:
            raw_before_saturation = raw_final_score
            raw_final_score = _clamp_unit(raw_final_score * saturation_multiplier)
            legacy_raw_final_score = _clamp_unit(
                legacy_raw_final_score * saturation_multiplier,
            )
            saturation_penalty_contribution = max(
                0.0,
                raw_before_saturation - raw_final_score,
            )

        planning_confidence_signal = _movie_top_picks_planning_confidence(
            candidate,
            candidate_families=candidate_families,
            family_layer_fits=family_layer_fits,
            cooldown_context=cooldown_context,
        )
        planning_confidence_bonus = float(
            planning_confidence_signal["planning_confidence_bonus"],
        )
        if planning_confidence_bonus > 0.0:
            raw_final_score = _clamp_unit(raw_final_score + planning_confidence_bonus)
            legacy_raw_final_score = _clamp_unit(
                legacy_raw_final_score + planning_confidence_bonus,
            )

        holiday_strength, seasonal_adjustment = _holiday_seasonal_adjustment(
            candidate,
            holiday_window_active=holiday_window_active,
        )
        final_score = _clamp_unit(raw_final_score + seasonal_adjustment)
        legacy_final_score = _clamp_unit(legacy_raw_final_score + seasonal_adjustment)

        primary_reason_bucket, primary_reason_source, primary_reason_label = _movie_reason_bucket_label(
            family_profile_maps,
            family_profile_norms,
            candidate_families,
            rewatch_strength=rewatch_strength,
            strength_cache=reason_label_strength_cache,
        )
        for family in MOVIE_COMFORT_RICH_FAMILIES:
            if family == primary_reason_source:
                continue
            if family_layer_fits[family]["blended"] >= 0.20 and family not in suppressed_map:
                suppressed_map[family] = "not_selected_in_bucket"

        if primary_reason_source in MOVIE_COMFORT_RICH_FAMILIES and primary_reason_source not in active_signal_families:
            active_signal_families.append(primary_reason_source)
        active_signal_families = [
            family
            for family in MOVIE_COMFORT_FAMILY_WEIGHTS
            if family in set(active_signal_families)
        ]

        candidate.score_breakdown["phase_fit"] = round(recency_phase_fit, 6)
        candidate.score_breakdown["library_fit"] = round(library_fit, 6)
        candidate.score_breakdown["recency_phase_fit"] = round(recency_phase_fit, 6)
        candidate.score_breakdown["behavior_score"] = round(behavior_score, 6)
        candidate.score_breakdown["quality_score"] = round(quality_score, 6)
        candidate.score_breakdown["legacy_quality_score"] = round(legacy_quality_score, 6)
        candidate.score_breakdown["shape_coverage"] = round(shape_coverage, 6)
        candidate.score_breakdown["generic_only_match"] = float(generic_only_match)
        candidate.score_breakdown["core_affinity_score"] = round(core_affinity_score, 6)
        candidate.score_breakdown["legacy_core_affinity_score"] = round(
            legacy_core_affinity_score,
            6,
        )
        candidate.score_breakdown["ready_now_score"] = round(ready_now_score, 6)
        candidate.score_breakdown["cooldown_penalty"] = round(
            float(cooldown_signal["cooldown_penalty"]),
            6,
        )
        candidate.score_breakdown["title_cooldown_penalty"] = round(
            float(cooldown_signal["title_cooldown_penalty"]),
            6,
        )
        candidate.score_breakdown["burst_replay_allowance"] = round(
            float(cooldown_signal["burst_replay_allowance"]),
            6,
        )
        candidate.score_breakdown["title_burstiness"] = round(
            float(cooldown_signal["title_burstiness"]),
            6,
        )
        candidate.score_breakdown["lane_burstiness"] = round(
            float(cooldown_signal["lane_burstiness"]),
            6,
        )
        candidate.score_breakdown["days_since_title_watch"] = round(
            float(cooldown_signal["days_since_title_watch"]),
            6,
        )
        candidate.score_breakdown["title_repeat_gap_days"] = round(
            float(cooldown_signal["title_repeat_gap_days"]),
            6,
        )
        candidate.score_breakdown["cooldown_window_days"] = round(
            float(cooldown_signal["cooldown_window_days"]),
            6,
        )
        candidate.score_breakdown["recent_play_count_90d"] = round(
            float(cooldown_signal["recent_play_count_90d"]),
            6,
        )
        candidate.score_breakdown["recent_play_count_180d"] = round(
            float(cooldown_signal["recent_play_count_180d"]),
            6,
        )
        candidate.score_breakdown["recent_gap_median_days"] = round(
            float(cooldown_signal["recent_gap_median_days"]),
            6,
        )
        candidate.score_breakdown["title_saturation_penalty"] = round(
            float(cooldown_signal["title_saturation_penalty"]),
            6,
        )
        candidate.score_breakdown["saturation_multiplier"] = round(
            float(cooldown_signal["saturation_multiplier"]),
            6,
        )
        candidate.score_breakdown["title_history_present"] = round(
            float(cooldown_signal["title_history_present"]),
            6,
        )
        candidate.score_breakdown["release_ready_score"] = round(
            float(cooldown_signal["release_ready_score"]),
            6,
        )
        candidate.score_breakdown["release_status"] = str(
            cooldown_signal["release_status"],
        )
        candidate.score_breakdown["planning_confidence"] = round(
            float(planning_confidence_signal["planning_confidence"]),
            6,
        )
        candidate.score_breakdown["planning_confidence_bonus"] = round(
            planning_confidence_bonus,
            6,
        )
        candidate.score_breakdown["similar_watched_count"] = int(
            planning_confidence_signal["similar_watched_count"],
        )
        candidate.score_breakdown["matched_history_families"] = list(
            planning_confidence_signal["matched_history_families"],
        )
        candidate.score_breakdown["rich_history_family_count"] = int(
            planning_confidence_signal["rich_history_family_count"],
        )
        candidate.score_breakdown["generic_history_family_count"] = int(
            planning_confidence_signal["generic_history_family_count"],
        )
        candidate.score_breakdown["keyword_fit"] = round(family_layer_fits["keywords"]["blended"], 6)
        candidate.score_breakdown["collection_fit"] = round(family_layer_fits["collections"]["blended"], 6)
        candidate.score_breakdown["studio_fit"] = round(family_layer_fits["studios"]["blended"], 6)
        candidate.score_breakdown["genre_fit"] = round(family_layer_fits["genres"]["blended"], 6)
        candidate.score_breakdown["genre_backstop_fit"] = round(
            family_layer_fits["genres"]["blended"],
            6,
        )
        candidate.score_breakdown["director_fit"] = round(family_layer_fits["directors"]["blended"], 6)
        candidate.score_breakdown["lead_cast_fit"] = round(family_layer_fits["lead_cast"]["blended"], 6)
        candidate.score_breakdown["certification_fit"] = round(certification_fit, 6)
        candidate.score_breakdown["runtime_fit"] = round(runtime_fit, 6)
        candidate.score_breakdown["decade_fit"] = round(decade_fit, 6)
        candidate.score_breakdown["recent_shape_fit"] = round(recency_phase_fit, 6)
        candidate.score_breakdown["comfort_safety"] = round(comfort_safety, 6)
        candidate.score_breakdown["legacy_comfort_safety"] = round(
            legacy_comfort_safety,
            6,
        )
        candidate.score_breakdown["provider_support"] = round(provider_support, 6)
        candidate.score_breakdown["world_quality"] = round(
            float(world_quality_signal["aligned_world_quality"]),
            6,
        )
        candidate.score_breakdown["tmdb_world_quality"] = round(
            float(world_quality_signal["tmdb_world_quality"]),
            6,
        )
        candidate.score_breakdown["trakt_world_quality"] = round(
            float(world_quality_signal["trakt_world_quality"]),
            6,
        )
        candidate.score_breakdown["world_source_blend"] = str(
            world_quality_signal["world_source_blend"],
        )
        candidate.score_breakdown["world_alignment"] = round(
            float(world_quality_signal["world_alignment"]),
            6,
        )
        candidate.score_breakdown["world_alignment_confidence"] = round(
            float(world_quality_signal["world_alignment_confidence"]),
            6,
        )
        candidate.score_breakdown["world_alignment_sample_size"] = int(
            world_quality_signal["world_alignment_sample_size"],
        )
        candidate.score_breakdown["rating_confidence"] = round(rating_confidence, 6)
        candidate.score_breakdown["rewatch_strength"] = round(rewatch_strength, 6)
        candidate.score_breakdown["rewatch_bonus"] = round(rewatch_strength, 6)
        candidate.score_breakdown["inactivity_norm"] = round(inactivity_norm, 6)
        candidate.score_breakdown["phase_evidence"] = round(
            max(recency_phase_fit, library_fit),
            6,
        )
        candidate.score_breakdown["candidate_has_extended_metadata"] = (
            1.0 if _candidate_has_extended_movie_metadata(candidate) else 0.0
        )
        candidate.score_breakdown["candidate_is_unrated"] = (
            1.0 if user_score is None and candidate.rating is None else 0.0
        )
        candidate.score_breakdown["tag_signal_mode"] = "behavior_first"
        candidate.score_breakdown["hot_recency"] = round(recency_phase_fit, 6)
        candidate.score_breakdown["hot_recency_base"] = round(recency_phase_fit, 6)
        candidate.score_breakdown["hot_recency_mode_multiplier"] = 1.0
        candidate.score_breakdown["family_layer_fits"] = family_layer_fits
        candidate.score_breakdown["evaluated_signal_families"] = evaluated_signal_families
        candidate.score_breakdown["active_signal_families"] = active_signal_families
        candidate.score_breakdown["suppressed_signal_families"] = [
            {
                "family": family,
                "reason": reason,
            }
            for family, reason in suppressed_map.items()
        ]
        candidate.score_breakdown["primary_reason_bucket"] = primary_reason_bucket
        candidate.score_breakdown["primary_reason_source"] = primary_reason_source
        candidate.score_breakdown["primary_reason_label"] = primary_reason_label
        candidate.score_breakdown["reason_bucket_quota_action"] = "pending"
        candidate.score_breakdown["library_contribution"] = round(library_fit * 0.30, 6)
        candidate.score_breakdown["recency_phase_contribution"] = round(recency_phase_fit * 0.25, 6)
        candidate.score_breakdown["behavior_contribution"] = round(behavior_score * 0.20, 6)
        candidate.score_breakdown["comfort_safety_contribution"] = round(comfort_safety * 0.10, 6)
        candidate.score_breakdown["quality_contribution"] = round(quality_score * 0.10, 6)
        candidate.score_breakdown["shape_coverage_contribution"] = round(shape_coverage * 0.05, 6)
        candidate.score_breakdown["ready_now_contribution"] = round(
            ready_now_score * MOVIE_COMFORT_READY_NOW_WEIGHT,
            6,
        )
        candidate.score_breakdown["planning_confidence_contribution"] = round(
            planning_confidence_bonus,
            6,
        )
        candidate.score_breakdown["phase_family_contribution"] = round(recency_phase_fit * 0.25, 6)
        candidate.score_breakdown["hot_recency_contribution"] = 0.0
        candidate.score_breakdown["rating_contribution"] = round(quality_score * 0.10, 6)
        candidate.score_breakdown["rewatch_contribution"] = round(behavior_score * 0.20, 6)
        candidate.score_breakdown["background_contribution"] = round(
            (library_fit * 0.30) + (shape_coverage * 0.05),
            6,
        )
        candidate.score_breakdown["holiday_strength"] = round(holiday_strength, 6)
        candidate.score_breakdown["raw_final_score"] = round(raw_final_score, 6)
        candidate.score_breakdown["dampeners_contribution"] = round(
            seasonal_adjustment - saturation_penalty_contribution,
            6,
        )
        candidate.score_breakdown["saturation_dampener_contribution"] = round(
            -saturation_penalty_contribution,
            6,
        )
        candidate.score_breakdown["seasonality_dampener_contribution"] = round(
            seasonal_adjustment,
            6,
        )
        candidate.score_breakdown["diversity_dampener_contribution"] = 0.0
        candidate.score_breakdown["era_dampener_contribution"] = 0.0
        candidate.score_breakdown["opening_era_dampener_contribution"] = 0.0
        candidate.score_breakdown["seasonal_adjustment"] = round(seasonal_adjustment, 6)
        candidate.score_breakdown["diversity_multiplier"] = 1.0
        candidate.score_breakdown["era_multiplier"] = 1.0
        candidate.score_breakdown["comfort_score"] = round(final_score, 6)
        candidate.score_breakdown["legacy_raw_final_score"] = round(
            legacy_raw_final_score,
            6,
        )
        candidate.score_breakdown["legacy_final_score"] = round(legacy_final_score, 6)
        candidate.final_score = round(final_score, 6)

        if phase_genre_affinity:
            cand_genres = {
                genre.strip().lower()
                for genre in (candidate.genres or [])
                if genre
            }
            overlap = [
                genre
                for genre in sorted(
                    phase_genre_affinity,
                    key=phase_genre_affinity.get,
                    reverse=True,
                )[:5]
                if genre in cand_genres
            ]
            if overlap:
                candidate.score_breakdown["match_genres"] = ", ".join(
                    genre.title() for genre in overlap[:3]
                )

    candidates.sort(key=_movie_comfort_bucket_sort_key, reverse=True)

    filtered_candidates: list[CandidateItem] = []
    for candidate in candidates:
        is_unrated = float(candidate.score_breakdown.get("candidate_is_unrated", 0.0)) >= 1.0
        if (
            is_unrated
            and float(candidate.score_breakdown.get("rewatch_strength", 0.0)) < 0.35
            and float(candidate.score_breakdown.get("library_fit", 0.0)) < 0.40
            and max(
                float(candidate.score_breakdown.get("keyword_fit", 0.0)),
                float(candidate.score_breakdown.get("collection_fit", 0.0)),
                float(candidate.score_breakdown.get("studio_fit", 0.0)),
                float(candidate.score_breakdown.get("genre_fit", 0.0)),
                float(candidate.score_breakdown.get("director_fit", 0.0)),
                float(candidate.score_breakdown.get("lead_cast_fit", 0.0)),
            ) < 0.20
        ):
            candidate.score_breakdown["filtered_unrated_weak_shape"] = 1.0
            continue
        filtered_candidates.append(candidate)
    candidates[:] = filtered_candidates
    if not candidates:
        return candidates

    legacy_order = sorted(
        candidates,
        key=lambda candidate: (
            *_movie_comfort_legacy_sort_key(candidate),
            -initial_candidate_positions.get(id(candidate), 0),
        ),
        reverse=True,
    )
    legacy_positions = {
        id(candidate): position
        for position, candidate in enumerate(legacy_order, start=1)
    }
    for candidate in candidates:
        candidate.score_breakdown["legacy_rank"] = legacy_positions.get(id(candidate), 0)

    candidates.sort(key=_movie_comfort_bucket_sort_key, reverse=True)
    _apply_movie_reason_bucket_quotas(candidates)
    for current_rank, candidate in enumerate(candidates, start=1):
        legacy_rank = int(candidate.score_breakdown.get("legacy_rank", 0) or 0)
        candidate.score_breakdown["rank_delta"] = legacy_rank - current_rank
    return candidates


