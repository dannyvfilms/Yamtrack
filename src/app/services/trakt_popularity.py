"""Persisted Trakt popularity helpers for movie, TV, anime, and season items."""

from __future__ import annotations

import json
import math
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from django.db.models import Q
from django.utils import timezone

from app.models import Item, MediaTypes, Sources
from app.providers import trakt as trakt_provider
from app.services import metadata_resolution

SUPPORTED_ROUTE_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
    MediaTypes.SEASON.value,
}
TRAKT_POPULARITY_PRIOR_MEAN = 60.0
TRAKT_POPULARITY_PRIOR_VOTES = 50_000.0
TRAKT_POPULARITY_VOTE_OFFSET = 1.0
TRAKT_POPULARITY_VOTE_EXPONENT = 3.0
# Increment this when formula constants change to trigger an automatic local recompute on startup.
TRAKT_POPULARITY_SCORE_VERSION = 2
TRAKT_POPULARITY_CALIBRATION_FIXTURE = (
    Path(__file__).resolve().parents[1] / "data" / "trakt_popularity_calibration.json"
)
TRAKT_POPULARITY_INTERVAL_NEW_DAYS = 14
TRAKT_POPULARITY_INTERVAL_FIRST_YEAR_DAYS = 60
TRAKT_POPULARITY_INTERVAL_FIVE_YEARS_DAYS = 180
TRAKT_POPULARITY_INTERVAL_OLDER_DAYS = 365


def route_media_type_for_item(item: Item, route_media_type: str | None = None) -> str:
    """Return the routed/library media type for a tracked item."""
    return route_media_type or item.library_media_type or item.media_type


def supports_route_media_type(route_media_type: str) -> bool:
    """Return whether the route media type supports Trakt popularity."""
    return route_media_type in SUPPORTED_ROUTE_MEDIA_TYPES


def supports_item(item: Item | None, route_media_type: str | None = None) -> bool:
    """Return whether the item supports Trakt popularity enrichment."""
    if item is None:
        return False
    return supports_route_media_type(route_media_type_for_item(item, route_media_type))


def has_popularity_data(item: Item) -> bool:
    """Return whether the item already has a complete Trakt popularity snapshot."""
    return all(
        value is not None
        for value in (
            item.trakt_rating,
            item.trakt_rating_count,
            item.trakt_popularity_score,
            item.trakt_popularity_rank,
            item.trakt_popularity_fetched_at,
        )
    )


def _normalize_external_ids(item: Item) -> dict[str, str]:
    """Return a normalized external-ID map for Trakt reverse lookups."""
    external_ids = {
        str(key): str(value)
        for key, value in (item.provider_external_ids or {}).items()
        if value not in (None, "")
    }
    if item.source == Sources.TMDB.value:
        external_ids.setdefault("tmdb_id", str(item.media_id))
    if item.source == Sources.TVDB.value:
        external_ids.setdefault("tvdb_id", str(item.media_id))
    if item.source == Sources.MAL.value:
        external_ids.setdefault("mal_id", str(item.media_id))
    return external_ids


def resolve_lookup_candidates(
    item: Item,
    *,
    route_media_type: str | None = None,
) -> list[tuple[str, str]]:
    """Return ordered external-ID candidates for Trakt reverse lookup."""
    route_media_type = route_media_type_for_item(item, route_media_type)
    if not supports_route_media_type(route_media_type):
        return []

    external_ids = _normalize_external_ids(item)

    if route_media_type in {
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
        MediaTypes.SEASON.value,
    }:
        for provider, external_key in (
            (Sources.TMDB.value, "tmdb_id"),
            (Sources.TVDB.value, "tvdb_id"),
        ):
            if external_ids.get(external_key):
                continue
            provider_media_id = metadata_resolution.resolve_provider_media_id(
                item,
                provider,
                route_media_type=(
                    MediaTypes.TV.value
                    if route_media_type == MediaTypes.SEASON.value
                    else route_media_type
                ),
            )
            if provider_media_id:
                external_ids[external_key] = str(provider_media_id)

    lookup_order = (
        ("tmdb", external_ids.get("tmdb_id")),
        ("imdb", external_ids.get("imdb_id")),
    )
    if route_media_type in {
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
        MediaTypes.SEASON.value,
    }:
        lookup_order = (
            ("tmdb", external_ids.get("tmdb_id")),
            ("tvdb", external_ids.get("tvdb_id")),
            ("imdb", external_ids.get("imdb_id")),
        )

    seen: set[tuple[str, str]] = set()
    candidates: list[tuple[str, str]] = []
    for lookup_type, lookup_value in lookup_order:
        normalized_value = str(lookup_value or "").strip()
        if not normalized_value:
            continue
        candidate = (lookup_type, normalized_value)
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return candidates


def lookup_item_summary(
    item: Item,
    *,
    route_media_type: str | None = None,
) -> dict[str, Any] | None:
    """Return normalized Trakt summary metadata for an item."""
    route_media_type = route_media_type_for_item(item, route_media_type)
    for lookup_type, lookup_value in resolve_lookup_candidates(
        item,
        route_media_type=route_media_type,
    ):
        payload = trakt_provider.lookup_by_external_id(
            lookup_type,
            lookup_value,
            media_type=route_media_type,
            season_number=item.season_number,
        )
        if payload:
            payload["matched_lookup_value"] = lookup_value
            return payload
    return None


def compute_popularity_score(
    rating: float | int | None,
    votes: int | None,
    *,
    prior_mean: float = TRAKT_POPULARITY_PRIOR_MEAN,
    prior_votes: float = TRAKT_POPULARITY_PRIOR_VOTES,
    vote_offset: float = TRAKT_POPULARITY_VOTE_OFFSET,
    vote_exponent: float = TRAKT_POPULARITY_VOTE_EXPONENT,
) -> float | None:
    """Return the persisted local popularity score derived from Trakt inputs."""
    if rating is None or votes is None:
        return None
    normalized_votes = max(int(votes), 0)
    rating_pct = float(rating) * 10.0
    bayes_pct = (
        (rating_pct * normalized_votes) + (float(prior_mean) * float(prior_votes))
    ) / (normalized_votes + float(prior_votes))
    vote_weight = math.log10(normalized_votes + float(vote_offset))
    if vote_weight <= 0:
        vote_weight = 0.0
    return bayes_pct * (vote_weight ** float(vote_exponent))


@lru_cache(maxsize=1)
def load_calibration_fixture() -> dict[str, Any]:
    """Return the checked-in calibration fixture."""
    with TRAKT_POPULARITY_CALIBRATION_FIXTURE.open(encoding="utf-8") as fixture_file:
        return json.load(fixture_file)


def evaluate_calibration_fixture() -> dict[str, Any]:
    """Return ordering metrics for the checked-in fixture using current constants."""
    fixture = load_calibration_fixture()
    raw_items = fixture.get("items") or []
    scored_items = []
    for raw_item in raw_items:
        item = dict(raw_item)
        item["score"] = compute_popularity_score(
            item.get("rating"),
            item.get("votes"),
        )
        scored_items.append(item)

    predicted_order = sorted(
        scored_items,
        key=lambda item: (
            -(item.get("score") or 0.0),
            str(item.get("title") or "").lower(),
        ),
    )
    predicted_rank_map = {
        str(item.get("title")): index
        for index, item in enumerate(predicted_order, start=1)
    }

    enriched_items = []
    abs_errors = []
    for item in scored_items:
        predicted_rank = predicted_rank_map.get(str(item.get("title")))
        expected_rank = int(item.get("expected_rank") or 0)
        abs_error = abs(predicted_rank - expected_rank) if predicted_rank and expected_rank else 0
        abs_errors.append(abs_error)
        enriched_items.append(
            {
                **item,
                "predicted_rank": predicted_rank,
                "absolute_error": abs_error,
            },
        )

    top_ten_expected = {
        str(item.get("title"))
        for item in raw_items
        if int(item.get("expected_rank") or 0) <= 10
    }
    top_ten_predicted = {
        str(item.get("title"))
        for item in predicted_order[:10]
    }

    return {
        "count": len(enriched_items),
        "mae": (sum(abs_errors) / len(abs_errors)) if abs_errors else 0.0,
        "max_abs_error": max(abs_errors) if abs_errors else 0,
        "top_ten_overlap": len(top_ten_expected & top_ten_predicted),
        "items": enriched_items,
    }


@lru_cache(maxsize=1)
def _calibration_reference_scores() -> tuple[float, ...]:
    """Return descending reference scores used for rank estimation."""
    scores = [
        item.get("score")
        for item in evaluate_calibration_fixture().get("items", [])
        if item.get("score") is not None
    ]
    return tuple(sorted((float(score) for score in scores), reverse=True))


def estimate_rank_from_score(score: float | None) -> int | None:
    """Estimate a global-ish rank by interpolating against calibration scores."""
    if score is None:
        return None
    reference_scores = _calibration_reference_scores()
    if not reference_scores:
        return None

    normalized_score = float(score)
    if normalized_score >= reference_scores[0]:
        return 1

    for index in range(len(reference_scores) - 1):
        upper = reference_scores[index]
        lower = reference_scores[index + 1]
        if upper >= normalized_score >= lower:
            if math.isclose(upper, lower):
                return index + 1
            fraction = (upper - normalized_score) / (upper - lower)
            return max(1, int(round((index + 1) + fraction)))

    if normalized_score <= 0:
        return len(reference_scores) + 1

    lowest_reference = reference_scores[-1]
    if lowest_reference <= 0:
        return len(reference_scores) + 1

    tail_ratio = lowest_reference / normalized_score
    tail_increment = max(
        1,
        int(round(max(tail_ratio - 1.0, 0.0) * len(reference_scores))),
    )
    return len(reference_scores) + tail_increment


def refresh_interval_for_item(item: Item, *, now=None):
    """Return the desired Trakt refresh interval for an item."""
    now = now or timezone.now()
    release_dt = item.release_datetime
    if release_dt is None:
        return timedelta(days=TRAKT_POPULARITY_INTERVAL_NEW_DAYS)

    if timezone.is_aware(release_dt):
        release_date = timezone.localtime(release_dt).date()
    else:
        release_date = release_dt.date()

    age_days = (now.date() - release_date).days
    if age_days <= 90:
        return timedelta(days=TRAKT_POPULARITY_INTERVAL_NEW_DAYS)
    if age_days <= 365:
        return timedelta(days=TRAKT_POPULARITY_INTERVAL_FIRST_YEAR_DAYS)
    if age_days <= (365 * 5):
        return timedelta(days=TRAKT_POPULARITY_INTERVAL_FIVE_YEARS_DAYS)
    return timedelta(days=TRAKT_POPULARITY_INTERVAL_OLDER_DAYS)


def needs_refresh(
    item: Item,
    *,
    now=None,
    force: bool = False,
) -> bool:
    """Return whether the item should refresh its Trakt popularity snapshot."""
    if force or not has_popularity_data(item):
        return True
    now = now or timezone.now()
    return item.trakt_popularity_fetched_at <= (now - refresh_interval_for_item(item, now=now))


def tracked_items_queryset(*, media_types: list[str] | tuple[str, ...] | None = None):
    """Return tracked items eligible for Trakt popularity enrichment."""
    from app.models import Anime, Movie, TV

    supported_media_types = tuple(media_types or sorted(SUPPORTED_ROUTE_MEDIA_TYPES))
    tracked_filter = (
        Q(id__in=Movie.objects.values("item_id"))
        | Q(id__in=TV.objects.values("item_id"))
        | Q(id__in=Anime.objects.values("item_id"))
    )
    return (
        Item.objects.filter(library_media_type__in=supported_media_types)
        .filter(tracked_filter)
        .distinct()
    )


def select_items_for_refresh(
    *,
    limit: int | None = None,
    media_types: list[str] | tuple[str, ...] | None = None,
    missing_only: bool = False,
    now=None,
) -> list[Item]:
    """Return tracked items that need an initial/stale Trakt popularity refresh."""
    now = now or timezone.now()
    queryset = tracked_items_queryset(media_types=media_types).order_by(
        "trakt_popularity_fetched_at",
        "id",
    )
    items: list[Item] = []
    for item in queryset.iterator(chunk_size=250):
        if missing_only and has_popularity_data(item):
            continue
        if not missing_only and not needs_refresh(item, now=now):
            continue
        items.append(item)
        if limit is not None and len(items) >= int(limit):
            break
    return items


def _merge_external_ids(item: Item, lookup_payload: dict[str, Any]) -> dict[str, str]:
    """Return merged provider external IDs after a Trakt lookup."""
    merged = dict(item.provider_external_ids or {})
    trakt_ids = lookup_payload.get("trakt_ids") or {}
    for source_key, item_key in (
        ("tmdb", "tmdb_id"),
        ("tvdb", "tvdb_id"),
        ("imdb", "imdb_id"),
    ):
        value = trakt_ids.get(source_key)
        if value not in (None, ""):
            merged[item_key] = str(value)
    return merged


def refresh_trakt_popularity(
    item: Item,
    *,
    route_media_type: str | None = None,
    force: bool = False,
) -> dict[str, Any] | None:
    """Refresh persisted Trakt popularity metadata for one tracked item."""
    if not supports_item(item, route_media_type) or not trakt_provider.is_configured():
        return None
    if not needs_refresh(item, force=force):
        return {
            "rating": item.trakt_rating,
            "votes": item.trakt_rating_count,
            "score": item.trakt_popularity_score,
            "rank": item.trakt_popularity_rank,
        }

    route_media_type = route_media_type_for_item(item, route_media_type)
    lookup_payload = lookup_item_summary(item, route_media_type=route_media_type)
    if not lookup_payload:
        raise LookupError(f"No Trakt match found for item {item.id}")
    if lookup_payload.get("rating") is None or lookup_payload.get("votes") is None:
        raise ValueError(f"Trakt payload missing rating/votes for item {item.id}")

    popularity_score = compute_popularity_score(
        lookup_payload.get("rating"),
        lookup_payload.get("votes"),
    )
    if popularity_score is None:
        raise ValueError(f"Unable to compute Trakt popularity score for item {item.id}")

    merged_external_ids = _merge_external_ids(item, lookup_payload)

    item.trakt_rating = float(lookup_payload["rating"])
    item.trakt_rating_count = int(lookup_payload["votes"])
    item.trakt_popularity_score = popularity_score
    item.trakt_popularity_rank = estimate_rank_from_score(popularity_score)
    item.trakt_popularity_fetched_at = timezone.now()

    update_fields = [
        "trakt_rating",
        "trakt_rating_count",
        "trakt_popularity_score",
        "trakt_popularity_rank",
        "trakt_popularity_fetched_at",
    ]
    if merged_external_ids != (item.provider_external_ids or {}):
        item.provider_external_ids = merged_external_ids
        update_fields.append("provider_external_ids")
    item.save(update_fields=update_fields)

    return {
        "rating": item.trakt_rating,
        "votes": item.trakt_rating_count,
        "score": item.trakt_popularity_score,
        "rank": item.trakt_popularity_rank,
        "matched_id_type": lookup_payload.get("matched_id_type"),
        "matched_lookup_value": lookup_payload.get("matched_lookup_value"),
    }
