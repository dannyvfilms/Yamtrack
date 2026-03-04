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

    for index, candidate in enumerate(candidates):
        genre_vector = _profile_vector(candidate.genres)
        tag_vector = _profile_vector(candidate.tags)

        genre_match = cosine_similarity(genre_vector, genre_profile)
        tag_match = cosine_similarity(tag_vector, tag_profile) if include_tag_weight else 0.0
        recency_bonus = cosine_similarity(genre_vector, recent_genre_profile)
        popularity = popularity_norm[index]
        rating = rating_norm[index]

        final_score = (
            (genre_match * 0.4)
            + (tag_match * 0.2)
            + (popularity * 0.15)
            + (rating * 0.15)
            + (recency_bonus * 0.1)
        )

        candidate.score_breakdown.update(
            {
            "genre_match": round(genre_match, 6),
            "tag_match": round(tag_match, 6),
            "popularity": round(popularity, 6),
            "rating": round(rating, 6),
            "recency_bonus": round(recency_bonus, 6),
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
