"""Scoring helpers for Discover recommendations."""

from __future__ import annotations

import math

from app.discover.schemas import CandidateItem


def cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """Compute cosine similarity for sparse dict vectors."""
    if not vec_a or not vec_b:
        return 0.0

    dot = 0.0
    for key, value in vec_a.items():
        dot += float(value) * float(vec_b.get(key, 0.0))

    norm_a = math.sqrt(sum(float(value) ** 2 for value in vec_a.values()))
    norm_b = math.sqrt(sum(float(value) ** 2 for value in vec_b.values()))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


def normalize_numeric_map(values: dict[str, float]) -> dict[str, float]:
    """Normalize dict values into [0, 1] using max scaling."""
    if not values:
        return {}
    max_value = max(float(value) for value in values.values())
    if max_value <= 0:
        return {key: 0.0 for key in values}
    return {key: float(value) / max_value for key, value in values.items()}


def normalize_values(values: list[float | None]) -> list[float]:
    """Min-max normalize a batch of values, fallback to 0.5 for no variance."""
    cleaned = [float(value) for value in values if value is not None]
    if not cleaned:
        return [0.5 for _ in values]

    min_value = min(cleaned)
    max_value = max(cleaned)
    if math.isclose(min_value, max_value):
        return [0.5 if value is not None else 0.5 for value in values]

    scale = max_value - min_value
    normalized: list[float] = []
    for value in values:
        if value is None:
            normalized.append(0.5)
            continue
        normalized.append((float(value) - min_value) / scale)

    return normalized


def weighted_pearson_correlation(
    values_a: list[float],
    values_b: list[float],
    weights: list[float],
) -> float:
    """Return weighted Pearson correlation in [-1, 1]."""
    if not values_a or not values_b or not weights:
        return 0.0
    if not (len(values_a) == len(values_b) == len(weights)):
        return 0.0

    weighted_rows = [
        (float(value_a), float(value_b), max(float(weight), 0.0))
        for value_a, value_b, weight in zip(values_a, values_b, weights, strict=False)
        if weight is not None
    ]
    total_weight = sum(weight for _value_a, _value_b, weight in weighted_rows)
    if total_weight <= 0.0:
        return 0.0

    mean_a = sum(value_a * weight for value_a, _value_b, weight in weighted_rows) / total_weight
    mean_b = sum(value_b * weight for _value_a, value_b, weight in weighted_rows) / total_weight
    covariance = sum(
        weight * (value_a - mean_a) * (value_b - mean_b)
        for value_a, value_b, weight in weighted_rows
    ) / total_weight
    variance_a = sum(
        weight * ((value_a - mean_a) ** 2)
        for value_a, _value_b, weight in weighted_rows
    ) / total_weight
    variance_b = sum(
        weight * ((value_b - mean_b) ** 2)
        for _value_a, value_b, weight in weighted_rows
    ) / total_weight
    if variance_a <= 0.0 or variance_b <= 0.0:
        return 0.0

    return max(-1.0, min(1.0, covariance / math.sqrt(variance_a * variance_b)))


def bayesian_world_quality(
    rating: float | None,
    votes: int | None,
    *,
    prior_mean: float = 6.5,
    prior_votes: int = 1500,
) -> float | None:
    """Return a vote-aware world quality score normalized into [0, 1]."""
    if rating is None:
        return None

    rating_value = float(rating)
    votes_value = max(int(votes or 0), 0)
    numerator = (rating_value * votes_value) + (float(prior_mean) * float(prior_votes))
    denominator = votes_value + max(int(prior_votes), 0)
    if denominator <= 0:
        return None
    return max(0.0, min(1.0, (numerator / denominator) / 10.0))


def blended_world_quality(
    *,
    provider_rating: float | None,
    provider_votes: int | None,
    trakt_rating: float | None = None,
    trakt_votes: int | None = None,
) -> dict[str, float | str]:
    """Blend TMDb/provider and Trakt quality into a single world score."""
    tmdb_world_quality = bayesian_world_quality(provider_rating, provider_votes)
    trakt_world_quality = bayesian_world_quality(trakt_rating, trakt_votes)
    tmdb_weight = math.log1p(max(int(provider_votes or 0), 0)) if tmdb_world_quality is not None else 0.0
    trakt_weight = math.log1p(max(int(trakt_votes or 0), 0)) if trakt_world_quality is not None else 0.0

    if tmdb_world_quality is None and trakt_world_quality is None:
        return {
            "world_quality": 0.5,
            "tmdb_world_quality": 0.0,
            "trakt_world_quality": 0.0,
            "world_source_blend": "neutral",
        }
    if trakt_world_quality is None:
        return {
            "world_quality": float(tmdb_world_quality),
            "tmdb_world_quality": float(tmdb_world_quality),
            "trakt_world_quality": 0.0,
            "world_source_blend": "tmdb_only",
        }
    if tmdb_world_quality is None:
        return {
            "world_quality": float(trakt_world_quality),
            "tmdb_world_quality": 0.0,
            "trakt_world_quality": float(trakt_world_quality),
            "world_source_blend": "trakt_only",
        }

    total_weight = tmdb_weight + trakt_weight
    if total_weight <= 0.0:
        world_quality = (float(tmdb_world_quality) + float(trakt_world_quality)) / 2.0
    else:
        world_quality = (
            (float(tmdb_world_quality) * tmdb_weight)
            + (float(trakt_world_quality) * trakt_weight)
        ) / total_weight
    return {
        "world_quality": max(0.0, min(1.0, float(world_quality))),
        "tmdb_world_quality": float(tmdb_world_quality),
        "trakt_world_quality": float(trakt_world_quality),
        "world_source_blend": "tmdb_trakt_blend",
    }


def _profile_vector(items: list[str]) -> dict[str, float]:
    vector: dict[str, float] = {}
    if not items:
        return vector
    for value in items:
        if not value:
            continue
        key = str(value).strip().lower()
        if not key:
            continue
        vector[key] = vector.get(key, 0.0) + 1.0
    return normalize_numeric_map(vector)


def score_candidates(
    candidates: list[CandidateItem],
    profile: dict,
    *,
    include_tag_weight: bool = True,
    apply_negative_penalty: bool = True,
) -> list[CandidateItem]:
    """Apply weighted scoring formula to candidate list in place."""
    if not candidates:
        return candidates

    popularity_norm = normalize_values([candidate.popularity for candidate in candidates])
    rating_norm = normalize_values([candidate.rating for candidate in candidates])

    genre_profile = {
        str(key).lower(): float(value)
        for key, value in (profile.get("genre_affinity") or {}).items()
    }
    tag_profile = {
        str(key).lower(): float(value)
        for key, value in (profile.get("tag_affinity") or {}).items()
    }
    recent_genre_profile = {
        str(key).lower(): float(value)
        for key, value in (profile.get("recent_genre_affinity") or {}).items()
    }
    phase_genre_profile = {
        str(key).lower(): float(value)
        for key, value in (profile.get("phase_genre_affinity") or {}).items()
    }
    recent_tag_profile = {
        str(key).lower(): float(value)
        for key, value in (profile.get("recent_tag_affinity") or {}).items()
    }
    phase_tag_profile = {
        str(key).lower(): float(value)
        for key, value in (profile.get("phase_tag_affinity") or {}).items()
    }
    negative_genre_profile = {
        str(key).lower(): float(value)
        for key, value in (profile.get("negative_genre_affinity") or {}).items()
    }
    negative_tag_profile = {
        str(key).lower(): float(value)
        for key, value in (profile.get("negative_tag_affinity") or {}).items()
    }
    negative_person_profile = {
        str(key).lower(): float(value)
        for key, value in (profile.get("negative_person_affinity") or {}).items()
    }

    for index, candidate in enumerate(candidates):
        genre_vector = _profile_vector(candidate.genres)
        tag_vector = _profile_vector(candidate.tags)
        person_vector = _profile_vector(candidate.people)

        genre_match = cosine_similarity(genre_vector, genre_profile)
        tag_match = cosine_similarity(tag_vector, tag_profile) if include_tag_weight else 0.0
        recency_bonus = cosine_similarity(genre_vector, recent_genre_profile)
        phase_genre_bonus = cosine_similarity(genre_vector, phase_genre_profile)
        recency_tag_bonus = cosine_similarity(tag_vector, recent_tag_profile)
        phase_tag_bonus = cosine_similarity(tag_vector, phase_tag_profile)
        popularity = popularity_norm[index]
        rating = rating_norm[index]
        negative_genre_penalty = (
            cosine_similarity(genre_vector, negative_genre_profile) * 0.18
            if apply_negative_penalty
            else 0.0
        )
        negative_tag_penalty = (
            cosine_similarity(tag_vector, negative_tag_profile) * 0.04
            if apply_negative_penalty
            else 0.0
        )
        negative_person_penalty = (
            cosine_similarity(person_vector, negative_person_profile) * 0.06
            if apply_negative_penalty
            else 0.0
        )
        total_negative_penalty = min(
            0.22,
            negative_genre_penalty + negative_tag_penalty + negative_person_penalty,
        )

        final_score = (
            (genre_match * 0.4)
            + (tag_match * 0.2)
            + (popularity * 0.15)
            + (rating * 0.15)
            + (recency_bonus * 0.1)
            - total_negative_penalty
        )

        candidate.score_breakdown.update(
            {
                "genre_match": round(genre_match, 6),
                "tag_match": round(tag_match, 6),
                "popularity": round(popularity, 6),
                "rating": round(rating, 6),
                "recency_bonus": round(recency_bonus, 6),
                "phase_genre_bonus": round(phase_genre_bonus, 6),
                "recency_tag_bonus": round(recency_tag_bonus, 6),
                "phase_tag_bonus": round(phase_tag_bonus, 6),
                "negative_genre_penalty": round(negative_genre_penalty, 6),
                "negative_tag_penalty": round(negative_tag_penalty, 6),
                "negative_person_penalty": round(negative_person_penalty, 6),
                "negative_total_penalty": round(total_negative_penalty, 6),
            },
        )
        candidate.final_score = round(final_score, 6)

    candidates.sort(
        key=lambda candidate: (
            candidate.final_score if candidate.final_score is not None else -1.0,
            candidate.rating if candidate.rating is not None else -1.0,
            candidate.popularity if candidate.popularity is not None else -1.0,
        ),
        reverse=True,
    )

    return candidates
