"""Shared helpers and constants used by multiple Discover service modules."""

from __future__ import annotations

import math
from collections import defaultdict

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Count, Q
from django.utils import timezone

from app.discover.feature_metadata import (
    is_director_credit,
    normalize_person_name,
    normalize_studio,
)
from app.discover.profile import MODEL_BY_MEDIA_TYPE
from app.discover.schemas import CandidateItem
from app.models import (
    CreditRoleType,
    Episode,
    ItemPersonCredit,
    ItemStudioCredit,
    ItemTag,
    MediaTypes,
    Status,
)

MAX_ITEMS_PER_ROW = 12

BEHAVIOR_FIRST_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
}
WORLD_QUALITY_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
}

HOLIDAY_STRONG_TERMS = {"christmas", "xmas", "noel", "grinch", "krampus", "nutcracker"}
HOLIDAY_SOFT_TERMS = {"holiday", "holidays", "new year", "new years", "jack frost", "santa claus"}

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


def _item_tag_map(user, item_ids: list[int]) -> dict[int, list[str]]:
    mapping: dict[int, list[str]] = defaultdict(list)
    if not item_ids:
        return mapping

    for item_tag in ItemTag.objects.filter(item_id__in=item_ids, tag__user=user).select_related("tag"):
        tag_name = (item_tag.tag.name or "").strip()
        if tag_name:
            mapping[item_tag.item_id].append(tag_name)

    return mapping


def _item_credit_feature_maps(item_ids: list[int]) -> tuple[dict[int, list[str]], dict[int, list[str]], dict[int, list[str]]]:
    people_map: dict[int, list[str]] = defaultdict(list)
    directors_map: dict[int, list[str]] = defaultdict(list)
    lead_cast_map: dict[int, list[str]] = defaultdict(list)
    if not item_ids:
        return people_map, directors_map, lead_cast_map

    people_seen: dict[int, set[str]] = defaultdict(set)
    directors_seen: dict[int, set[str]] = defaultdict(set)
    lead_cast_seen: dict[int, set[str]] = defaultdict(set)
    lead_cast_counts: dict[int, int] = defaultdict(int)
    credits = (
        ItemPersonCredit.objects.filter(item_id__in=item_ids)
        .order_by("item_id", "role_type", "sort_order", "person__name")
        .values_list(
            "item_id",
            "role_type",
            "role",
            "department",
            "person__name",
        )
    )
    for item_id, role_type, role, department, person_name_raw in credits:
        person_name = normalize_person_name(person_name_raw or "")
        if not person_name:
            continue
        if person_name not in people_seen[item_id]:
            people_seen[item_id].add(person_name)
            people_map[item_id].append(person_name)
        if is_director_credit(role_type, role, department):
            if person_name not in directors_seen[item_id]:
                directors_seen[item_id].add(person_name)
                directors_map[item_id].append(person_name)
        if (
            role_type == CreditRoleType.CAST.value
            and lead_cast_counts[item_id] < 3
            and person_name not in lead_cast_seen[item_id]
        ):
            lead_cast_seen[item_id].add(person_name)
            lead_cast_map[item_id].append(person_name)
            lead_cast_counts[item_id] += 1
    return people_map, directors_map, lead_cast_map


def _item_studio_map(item_ids: list[int]) -> dict[int, list[str]]:
    mapping: dict[int, list[str]] = defaultdict(list)
    if not item_ids:
        return mapping

    seen: dict[int, set[str]] = defaultdict(set)
    credits = (
        ItemStudioCredit.objects.filter(item_id__in=item_ids)
        .order_by("item_id", "sort_order", "studio__name")
        .values_list("item_id", "studio__name")
    )
    for item_id, studio_name_raw in credits:
        studio_name = normalize_studio(studio_name_raw or "")
        if not studio_name or studio_name in seen[item_id]:
            continue
        seen[item_id].add(studio_name)
        mapping[item_id].append(studio_name)
    return mapping


def _feature_vector(values: list[str]) -> dict[str, float]:
    vector: dict[str, float] = {}
    for value in values:
        key = str(value).strip().lower()
        if not key:
            continue
        vector[key] = 1.0
    return vector


def _feature_vector_norm(vector: dict[str, float]) -> float:
    if not vector:
        return 0.0
    return math.sqrt(sum(float(value) ** 2 for value in vector.values()))


def _entry_activity_datetime(entry):
    return (
        getattr(entry, "end_date", None)
        or getattr(entry, "progressed_at", None)
        or getattr(entry, "created_at", None)
    )


def _tv_episode_rewatch_counts(user, item_ids: list[int]) -> dict[int, float]:
    if not item_ids:
        return {}

    episode_rollups = (
        Episode.objects.filter(
            related_season__related_tv__user=user,
            related_season__related_tv__item_id__in=item_ids,
            related_season__item__season_number__gt=0,
            end_date__isnull=False,
        )
        .values("related_season__related_tv__item_id")
        .annotate(
            total_episode_plays=Count("id"),
            unique_episodes_watched=Count("item_id", distinct=True),
        )
    )
    counts: dict[int, float] = {}
    for rollup in episode_rollups:
        item_id = rollup.get("related_season__related_tv__item_id")
        if not item_id:
            continue
        unique_episodes = int(rollup.get("unique_episodes_watched") or 0)
        if unique_episodes <= 0:
            continue
        total_plays = int(rollup.get("total_episode_plays") or 0)
        equivalent_watches = max(1.0, float(total_plays) / float(unique_episodes))
        if equivalent_watches > 1.0:
            counts[int(item_id)] = equivalent_watches
    return counts


def _rewatch_counts(
    user,
    model,
    item_ids: list[int],
    *,
    media_type: str | None = None,
) -> dict[int, float]:
    if not item_ids:
        return {}

    if media_type == MediaTypes.TV.value:
        return _tv_episode_rewatch_counts(user, item_ids)

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
        int(row["item_id"]): float(row["watch_count"])
        for row in grouped
        if row.get("item_id")
    }


def _model_for_media_type(media_type: str):
    model_name = MODEL_BY_MEDIA_TYPE.get(media_type)
    if not model_name:
        return None
    return apps.get_model("app", model_name)


def _model_has_field(model, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)
    except FieldDoesNotExist:
        return False
    return True


def _activity_filter_query(model, cutoff, *, newer_than: bool) -> Q:
    op = "gte" if newer_than else "lte"
    query = Q(**{f"created_at__{op}": cutoff})
    if _model_has_field(model, "progressed_at"):
        query |= Q(**{f"progressed_at__{op}": cutoff})
    if _model_has_field(model, "end_date"):
        query |= Q(**{f"end_date__{op}": cutoff})
    return query


def _activity_ordering(model) -> tuple[str, ...]:
    order_fields: list[str] = []
    if _model_has_field(model, "end_date"):
        order_fields.append("-end_date")
    if _model_has_field(model, "progressed_at"):
        order_fields.append("-progressed_at")
    order_fields.append("-created_at")
    return tuple(order_fields)


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _calibrate_display_score(raw_score: float, *, offset: float, weight: float) -> float:
    return _clamp_unit(offset + (raw_score * weight))


def _is_holiday_window(today=None) -> bool:
    if today is None:
        today = timezone.localdate()
    month_day = (today.month, today.day)
    return month_day >= (11, 15) or month_day <= (1, 10)


def _holiday_value_strength(value: str | None) -> float:
    if not value:
        return 0.0

    key = str(value).strip().lower()
    if not key:
        return 0.0

    if any(term in key for term in HOLIDAY_STRONG_TERMS):
        return 1.0
    if any(term in key for term in HOLIDAY_SOFT_TERMS):
        return 0.7
    return 0.0


def _holiday_seasonal_strength(candidate: CandidateItem) -> float:
    values = [
        *(candidate.tags or []),
        *(candidate.genres or []),
        *(candidate.keywords or []),
        candidate.collection_name,
        candidate.title,
        candidate.original_title,
        candidate.localized_title,
    ]
    strength = 0.0
    for value in values:
        strength = max(strength, _holiday_value_strength(value))
    return strength


def _holiday_seasonal_adjustment(candidate: CandidateItem, *, holiday_window_active: bool) -> tuple[float, float]:
    holiday_strength = _holiday_seasonal_strength(candidate)
    if holiday_strength <= 0.0:
        return 0.0, 0.0
    if holiday_window_active:
        return holiday_strength, 0.06 * holiday_strength
    return holiday_strength, -0.40 * holiday_strength
