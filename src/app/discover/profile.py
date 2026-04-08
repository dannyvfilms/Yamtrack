"""Taste profile computation for Discover."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Max, Q
from django.utils import timezone

from app.discover import cache_repo
from app.discover.feature_metadata import (
    is_director_credit,
    normalize_certification,
    normalize_collection,
    normalize_features,
    normalize_keyword,
    normalize_person_name,
    normalize_studio,
    release_decade_label,
    runtime_bucket_label,
)
from app.discover.registry import ALL_MEDIA_KEY, DISCOVER_MEDIA_TYPES
from app.discover.scoring import (
    blended_world_quality,
    normalize_numeric_map,
    weighted_pearson_correlation,
)
from app.models import (
    CreditRoleType,
    DiscoverFeedback,
    DiscoverFeedbackType,
    ItemPersonCredit,
    ItemStudioCredit,
    ItemTag,
    MediaTypes,
    Status,
)

PROFILE_TTL_SECONDS = 60 * 60 * 24
COMFORT_PROFILE_FAMILIES = (
    "genres",
    "keywords",
    "studios",
    "collections",
    "directors",
    "lead_cast",
    "certifications",
    "runtime_buckets",
    "decades",
)
VIDEO_COMFORT_PROFILE_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
}
WORLD_RATING_PROFILE_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
}
WORLD_RATING_PROFILE_ACTIVATION_MIN_SAMPLE = 5
WORLD_RATING_PROFILE_MAX_CONFIDENCE_SAMPLE = 12.0

MODEL_BY_MEDIA_TYPE = {
    MediaTypes.MOVIE.value: "movie",
    MediaTypes.TV.value: "tv",
    MediaTypes.ANIME.value: "anime",
    MediaTypes.MUSIC.value: "music",
    MediaTypes.PODCAST.value: "podcast",
    MediaTypes.BOOK.value: "book",
    MediaTypes.COMIC.value: "comic",
    MediaTypes.MANGA.value: "manga",
    MediaTypes.GAME.value: "game",
    MediaTypes.BOARDGAME.value: "boardgame",
}


@dataclass(slots=True)
class ProfilePayload:
    """In-memory profile payload consumed by scoring."""

    genre_affinity: dict[str, float]
    recent_genre_affinity: dict[str, float]
    phase_genre_affinity: dict[str, float]
    tag_affinity: dict[str, float]
    recent_tag_affinity: dict[str, float]
    phase_tag_affinity: dict[str, float]
    keyword_affinity: dict[str, float]
    recent_keyword_affinity: dict[str, float]
    phase_keyword_affinity: dict[str, float]
    studio_affinity: dict[str, float]
    recent_studio_affinity: dict[str, float]
    phase_studio_affinity: dict[str, float]
    collection_affinity: dict[str, float]
    recent_collection_affinity: dict[str, float]
    phase_collection_affinity: dict[str, float]
    director_affinity: dict[str, float]
    recent_director_affinity: dict[str, float]
    phase_director_affinity: dict[str, float]
    lead_cast_affinity: dict[str, float]
    recent_lead_cast_affinity: dict[str, float]
    phase_lead_cast_affinity: dict[str, float]
    certification_affinity: dict[str, float]
    recent_certification_affinity: dict[str, float]
    phase_certification_affinity: dict[str, float]
    runtime_bucket_affinity: dict[str, float]
    recent_runtime_bucket_affinity: dict[str, float]
    phase_runtime_bucket_affinity: dict[str, float]
    decade_affinity: dict[str, float]
    recent_decade_affinity: dict[str, float]
    phase_decade_affinity: dict[str, float]
    comfort_library_affinity: dict[str, dict[str, float]]
    comfort_rewatch_affinity: dict[str, dict[str, float]]
    person_affinity: dict[str, float]
    negative_genre_affinity: dict[str, float]
    negative_tag_affinity: dict[str, float]
    negative_person_affinity: dict[str, float]
    world_rating_profile: dict[str, float | int]
    activity_snapshot_at: timezone.datetime | None

    def to_dict(self) -> dict:
        return {
            "genre_affinity": self.genre_affinity,
            "recent_genre_affinity": self.recent_genre_affinity,
            "phase_genre_affinity": self.phase_genre_affinity,
            "tag_affinity": self.tag_affinity,
            "recent_tag_affinity": self.recent_tag_affinity,
            "phase_tag_affinity": self.phase_tag_affinity,
            "keyword_affinity": self.keyword_affinity,
            "recent_keyword_affinity": self.recent_keyword_affinity,
            "phase_keyword_affinity": self.phase_keyword_affinity,
            "studio_affinity": self.studio_affinity,
            "recent_studio_affinity": self.recent_studio_affinity,
            "phase_studio_affinity": self.phase_studio_affinity,
            "collection_affinity": self.collection_affinity,
            "recent_collection_affinity": self.recent_collection_affinity,
            "phase_collection_affinity": self.phase_collection_affinity,
            "director_affinity": self.director_affinity,
            "recent_director_affinity": self.recent_director_affinity,
            "phase_director_affinity": self.phase_director_affinity,
            "lead_cast_affinity": self.lead_cast_affinity,
            "recent_lead_cast_affinity": self.recent_lead_cast_affinity,
            "phase_lead_cast_affinity": self.phase_lead_cast_affinity,
            "certification_affinity": self.certification_affinity,
            "recent_certification_affinity": self.recent_certification_affinity,
            "phase_certification_affinity": self.phase_certification_affinity,
            "runtime_bucket_affinity": self.runtime_bucket_affinity,
            "recent_runtime_bucket_affinity": self.recent_runtime_bucket_affinity,
            "phase_runtime_bucket_affinity": self.phase_runtime_bucket_affinity,
            "decade_affinity": self.decade_affinity,
            "recent_decade_affinity": self.recent_decade_affinity,
            "phase_decade_affinity": self.phase_decade_affinity,
            "comfort_library_affinity": self.comfort_library_affinity,
            "comfort_rewatch_affinity": self.comfort_rewatch_affinity,
            "person_affinity": self.person_affinity,
            "negative_genre_affinity": self.negative_genre_affinity,
            "negative_tag_affinity": self.negative_tag_affinity,
            "negative_person_affinity": self.negative_person_affinity,
            "world_rating_profile": self.world_rating_profile,
            "activity_snapshot_at": self.activity_snapshot_at,
        }


def _resolve_media_types(media_type: str) -> list[str]:
    if media_type == ALL_MEDIA_KEY:
        return DISCOVER_MEDIA_TYPES
    if media_type in MODEL_BY_MEDIA_TYPE:
        return [media_type]
    return []


def _entry_activity_datetime(entry):
    return (
        getattr(entry, "end_date", None)
        or getattr(entry, "progressed_at", None)
        or getattr(entry, "created_at", None)
    )


def _latest_datetime(*values):
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    return max(filtered)


def _entry_activity_snapshot_datetime(
    entry,
    *,
    has_progressed_at: bool = True,
    has_end_date: bool = True,
):
    return _latest_datetime(
        getattr(entry, "created_at", None),
        getattr(entry, "progressed_at", None) if has_progressed_at else None,
        getattr(entry, "end_date", None) if has_end_date else None,
    )


def _model_has_field(model, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)
    except FieldDoesNotExist:
        return False
    return True


def _world_rating_sample_size(raw_profile: dict | None) -> int:
    try:
        return max(int((raw_profile or {}).get("sample_size", 0) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _world_rating_candidate_count(user, media_type: str) -> int:
    if media_type not in WORLD_RATING_PROFILE_MEDIA_TYPES:
        return 0

    model_name = MODEL_BY_MEDIA_TYPE.get(media_type)
    if not model_name:
        return 0

    model = apps.get_model("app", model_name)
    return model.objects.filter(
        user=user,
        status=Status.COMPLETED.value,
    ).exclude(
        score__isnull=True,
    ).filter(
        Q(
            item__provider_rating__isnull=False,
            item__provider_rating_count__isnull=False,
        )
        | Q(
            item__trakt_rating__isnull=False,
            item__trakt_rating_count__isnull=False,
        ),
    ).count()


def _entry_weight(entry, now, *, activity_dt=None):
    score = float(entry.score) if getattr(entry, "score", None) is not None else 6.0
    score_weight = max(0.2, min(1.0, score / 10.0))

    if activity_dt is None:
        activity_dt = _entry_activity_datetime(entry)
    if not activity_dt:
        recency_weight = 0.1
    else:
        days_old = max(0, (now - activity_dt).days)
        recency_weight = max(0.1, 1.0 - (min(days_old, 365) / 365.0))

    return score_weight * recency_weight


def _feedback_weight(entry, now):
    updated_at = getattr(entry, "updated_at", None) or getattr(entry, "created_at", None)
    if not updated_at:
        return 1.0
    days_old = max(0, (now - updated_at).days)
    return max(0.35, 1.0 - (min(days_old, 365) / 365.0))


def _item_credit_feature_maps(item_ids: list[int]) -> tuple[dict[int, list[str]], dict[int, list[str]], dict[int, list[str]]]:
    people_map: dict[int, list[str]] = defaultdict(list)
    directors_map: dict[int, list[str]] = defaultdict(list)
    lead_cast_map: dict[int, list[str]] = defaultdict(list)
    if not item_ids:
        return people_map, directors_map, lead_cast_map

    people_seen: dict[int, set[str]] = defaultdict(set)
    directors_seen: dict[int, set[str]] = defaultdict(set)
    lead_cast_seen: dict[int, set[str]] = defaultdict(set)
    lead_cast_count: dict[int, int] = defaultdict(int)

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
            and lead_cast_count[item_id] < 3
        ):
            if person_name not in lead_cast_seen[item_id]:
                lead_cast_seen[item_id].add(person_name)
                lead_cast_map[item_id].append(person_name)
                lead_cast_count[item_id] += 1

    return people_map, directors_map, lead_cast_map


def _item_studio_feature_map(item_ids: list[int]) -> dict[int, list[str]]:
    studio_map: dict[int, list[str]] = defaultdict(list)
    if not item_ids:
        return studio_map

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
        studio_map[item_id].append(studio_name)
    return studio_map


def _update_affinity_maps(
    all_weights: dict[str, float],
    recent_weights: dict[str, float],
    phase_weights: dict[str, float],
    values: list[str],
    *,
    weight: float,
    activity_dt,
    now,
) -> None:
    for value in values:
        all_weights[value] += weight
        if activity_dt and activity_dt >= now - timedelta(days=30):
            recent_weights[value] += weight
        if activity_dt and activity_dt >= now - timedelta(days=90):
            phase_weights[value] += weight


def _empty_comfort_affinity_bundle() -> dict[str, dict[str, float]]:
    return {family: {} for family in COMFORT_PROFILE_FAMILIES}


def _normalize_comfort_affinity_bundle(
    bundle_weights: dict[str, defaultdict[str, float]],
) -> dict[str, dict[str, float]]:
    return {
        family: normalize_numeric_map(dict(bundle_weights.get(family) or {}))
        for family in COMFORT_PROFILE_FAMILIES
    }


def _video_feature_families_for_entry(
    entry,
    *,
    studio_map: dict[int, list[str]],
    directors_map: dict[int, list[str]],
    lead_cast_map: dict[int, list[str]],
) -> dict[str, list[str]]:
    return {
        "genres": normalize_features(entry.item.genres or [], normalize_person_name),
        "keywords": normalize_features(entry.item.provider_keywords or [], normalize_keyword),
        "studios": studio_map.get(entry.item_id) or normalize_features(
            entry.item.studios or [],
            normalize_studio,
        ),
        "collections": normalize_features(
            [entry.item.provider_collection_name or entry.item.provider_collection_id],
            normalize_collection,
        ),
        "directors": directors_map.get(entry.item_id, []),
        "lead_cast": lead_cast_map.get(entry.item_id, []),
        "certifications": normalize_features(
            [entry.item.provider_certification],
            normalize_certification,
        ),
        "runtime_buckets": normalize_features(
            [runtime_bucket_label(entry.item.runtime_minutes)],
            normalize_person_name,
        ),
        "decades": normalize_features(
            [release_decade_label(entry.item.release_datetime)],
            normalize_person_name,
        ),
    }


def _build_video_comfort_affinity_bundles(
    entries: list,
    *,
    now,
    studio_map: dict[int, list[str]],
    directors_map: dict[int, list[str]],
    lead_cast_map: dict[int, list[str]],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    library_weights: dict[str, defaultdict[str, float]] = {
        family: defaultdict(float)
        for family in COMFORT_PROFILE_FAMILIES
    }
    rewatch_weights: dict[str, defaultdict[str, float]] = {
        family: defaultdict(float)
        for family in COMFORT_PROFILE_FAMILIES
    }
    aggregated: dict[int, dict[str, object]] = {}

    for entry in entries:
        if getattr(entry, "status", None) != Status.COMPLETED.value or not entry.item_id:
            continue
        activity_dt = _entry_activity_datetime(entry)
        aggregate = aggregated.setdefault(
            entry.item_id,
            {
                "watch_count": 0,
                "latest_activity_dt": activity_dt,
                "latest_score": getattr(entry, "score", None),
                "features": _video_feature_families_for_entry(
                    entry,
                    studio_map=studio_map,
                    directors_map=directors_map,
                    lead_cast_map=lead_cast_map,
                ),
            },
        )
        aggregate["watch_count"] = int(aggregate["watch_count"]) + 1
        latest_activity_dt = aggregate.get("latest_activity_dt")
        if latest_activity_dt is None or (
            activity_dt is not None and activity_dt >= latest_activity_dt
        ):
            aggregate["latest_activity_dt"] = activity_dt
            aggregate["latest_score"] = getattr(entry, "score", None)

    if not aggregated:
        return _empty_comfort_affinity_bundle(), _empty_comfort_affinity_bundle()

    for aggregate in aggregated.values():
        watch_count = int(aggregate.get("watch_count") or 0)
        if watch_count <= 0:
            continue
        latest_activity_dt = aggregate.get("latest_activity_dt")
        latest_score = aggregate.get("latest_score")
        days_since_latest_watch = (
            max(0, (now - latest_activity_dt).days)
            if latest_activity_dt
            else 1825
        )
        score_weight = max(
            0.45,
            min(
                1.0,
                (
                    float(latest_score)
                    if latest_score is not None
                    else 6.0
                )
                / 10.0,
            ),
        )
        repeat_weight = 1.0 + min(1.0, 0.30 * (watch_count - 1))
        library_age_weight = max(
            0.70,
            1.0 - (min(days_since_latest_watch, 1825) / 1825.0),
        )
        library_weight = score_weight * repeat_weight * library_age_weight
        rewatch_weight = library_weight * 1.35 if watch_count >= 2 else 0.0
        feature_map = aggregate.get("features") or {}
        for family, values in feature_map.items():
            for value in values:
                library_weights[family][value] += library_weight
                if rewatch_weight > 0.0:
                    rewatch_weights[family][value] += rewatch_weight

    return (
        _normalize_comfort_affinity_bundle(library_weights),
        _normalize_comfort_affinity_bundle(rewatch_weights),
    )


def has_new_activity(user, media_type: str, snapshot_at) -> bool:
    """Return whether user has new activity after profile snapshot."""
    if snapshot_at is None:
        return True

    for media_type_key in _resolve_media_types(media_type):
        model_name = MODEL_BY_MEDIA_TYPE.get(media_type_key)
        if not model_name:
            continue
        model = apps.get_model("app", model_name)
        aggregate_kwargs = {"max_created_at": Max("created_at")}
        if _model_has_field(model, "progressed_at"):
            aggregate_kwargs["max_progressed_at"] = Max("progressed_at")
        if _model_has_field(model, "end_date"):
            aggregate_kwargs["max_end_date"] = Max("end_date")
        latest_model_activity = _latest_datetime(
            *model.objects.filter(user=user).aggregate(**aggregate_kwargs).values(),
        )
        if latest_model_activity and latest_model_activity > snapshot_at:
            return True

        latest_feedback_activity = _latest_datetime(
            *DiscoverFeedback.objects.filter(
                user=user,
                feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
                item__media_type=media_type_key,
            )
            .aggregate(
                max_created_at=Max("created_at"),
                max_updated_at=Max("updated_at"),
            )
            .values(),
        )
        if latest_feedback_activity and latest_feedback_activity > snapshot_at:
            return True

    return False


def compute_taste_profile(user, media_type: str) -> ProfilePayload:
    """Compute weighted taste profile vectors for selected media type."""
    now = timezone.now()

    genre_weights: dict[str, float] = defaultdict(float)
    recent_genre_weights: dict[str, float] = defaultdict(float)
    phase_genre_weights: dict[str, float] = defaultdict(float)
    tag_weights: dict[str, float] = defaultdict(float)
    recent_tag_weights: dict[str, float] = defaultdict(float)
    phase_tag_weights: dict[str, float] = defaultdict(float)
    keyword_weights: dict[str, float] = defaultdict(float)
    recent_keyword_weights: dict[str, float] = defaultdict(float)
    phase_keyword_weights: dict[str, float] = defaultdict(float)
    studio_weights: dict[str, float] = defaultdict(float)
    recent_studio_weights: dict[str, float] = defaultdict(float)
    phase_studio_weights: dict[str, float] = defaultdict(float)
    collection_weights: dict[str, float] = defaultdict(float)
    recent_collection_weights: dict[str, float] = defaultdict(float)
    phase_collection_weights: dict[str, float] = defaultdict(float)
    director_weights: dict[str, float] = defaultdict(float)
    recent_director_weights: dict[str, float] = defaultdict(float)
    phase_director_weights: dict[str, float] = defaultdict(float)
    lead_cast_weights: dict[str, float] = defaultdict(float)
    recent_lead_cast_weights: dict[str, float] = defaultdict(float)
    phase_lead_cast_weights: dict[str, float] = defaultdict(float)
    certification_weights: dict[str, float] = defaultdict(float)
    recent_certification_weights: dict[str, float] = defaultdict(float)
    phase_certification_weights: dict[str, float] = defaultdict(float)
    runtime_bucket_weights: dict[str, float] = defaultdict(float)
    recent_runtime_bucket_weights: dict[str, float] = defaultdict(float)
    phase_runtime_bucket_weights: dict[str, float] = defaultdict(float)
    decade_weights: dict[str, float] = defaultdict(float)
    recent_decade_weights: dict[str, float] = defaultdict(float)
    phase_decade_weights: dict[str, float] = defaultdict(float)
    comfort_library_affinity = _empty_comfort_affinity_bundle()
    comfort_rewatch_affinity = _empty_comfort_affinity_bundle()
    person_weights: dict[str, float] = defaultdict(float)
    negative_genre_weights: dict[str, float] = defaultdict(float)
    negative_tag_weights: dict[str, float] = defaultdict(float)
    negative_person_weights: dict[str, float] = defaultdict(float)
    world_rating_user_scores: list[float] = []
    world_rating_scores: list[float] = []
    world_rating_weights: list[float] = []

    activity_snapshot = None

    for media_type_key in _resolve_media_types(media_type):
        model_name = MODEL_BY_MEDIA_TYPE.get(media_type_key)
        if not model_name:
            continue

        model = apps.get_model("app", model_name)
        has_progressed_at = _model_has_field(model, "progressed_at")
        has_end_date = _model_has_field(model, "end_date")
        only_fields = [
            "id",
            "item_id",
            "score",
            "status",
            "created_at",
            "item__genres",
            "item__media_type",
            "item__provider_keywords",
            "item__provider_certification",
            "item__provider_collection_id",
            "item__provider_collection_name",
            "item__runtime_minutes",
            "item__release_datetime",
            "item__studios",
        ]
        if media_type_key in WORLD_RATING_PROFILE_MEDIA_TYPES:
            only_fields.extend(
                [
                    "item__provider_rating",
                    "item__provider_rating_count",
                    "item__trakt_rating",
                    "item__trakt_rating_count",
                ],
            )
        if has_progressed_at:
            only_fields.append("progressed_at")
        if has_end_date:
            only_fields.append("end_date")

        entries = list(
            model.objects.filter(user=user)
            .select_related("item")
            .only(*only_fields),
        )

        item_ids = [entry.item_id for entry in entries if entry.item_id]

        tag_map: dict[int, list[str]] = defaultdict(list)
        for item_tag in ItemTag.objects.filter(
            item_id__in=item_ids,
            tag__user=user,
        ).select_related("tag"):
            tag_name = (item_tag.tag.name or "").strip()
            if tag_name:
                tag_map[item_tag.item_id].append(tag_name)

        person_map: dict[int, list[str]] = defaultdict(list)
        directors_map: dict[int, list[str]] = defaultdict(list)
        lead_cast_map: dict[int, list[str]] = defaultdict(list)
        studio_map: dict[int, list[str]] = defaultdict(list)
        if media_type_key in VIDEO_COMFORT_PROFILE_MEDIA_TYPES:
            person_map, directors_map, lead_cast_map = _item_credit_feature_maps(item_ids)
            studio_map = _item_studio_feature_map(item_ids)
        if media_type_key in VIDEO_COMFORT_PROFILE_MEDIA_TYPES:
            (
                comfort_library_affinity,
                comfort_rewatch_affinity,
            ) = _build_video_comfort_affinity_bundles(
                entries,
                now=now,
                studio_map=studio_map,
                directors_map=directors_map,
                lead_cast_map=lead_cast_map,
            )

        for entry in entries:
            activity_dt = (
                getattr(entry, "end_date", None)
                if has_end_date
                else None
            ) or (
                getattr(entry, "progressed_at", None)
                if has_progressed_at
                else None
            ) or getattr(entry, "created_at", None)
            activity_snapshot = _latest_datetime(
                activity_snapshot,
                _entry_activity_snapshot_datetime(
                    entry,
                    has_progressed_at=has_progressed_at,
                    has_end_date=has_end_date,
                ),
            )

            weight = _entry_weight(entry, now, activity_dt=activity_dt)
            genres = normalize_features(entry.item.genres or [], normalize_person_name)
            tags = normalize_features(tag_map.get(entry.item_id, []), normalize_person_name)
            keywords = normalize_features(entry.item.provider_keywords or [], normalize_keyword)
            collections = normalize_features(
                [entry.item.provider_collection_name or entry.item.provider_collection_id],
                normalize_collection,
            )
            studios = studio_map.get(entry.item_id) or normalize_features(
                entry.item.studios or [],
                normalize_studio,
            )
            directors = directors_map.get(entry.item_id, [])
            lead_cast = lead_cast_map.get(entry.item_id, [])
            people = person_map.get(entry.item_id, [])
            certification = normalize_features(
                [entry.item.provider_certification],
                normalize_certification,
            )
            runtime_buckets = normalize_features(
                [runtime_bucket_label(entry.item.runtime_minutes)],
                normalize_person_name,
            )
            decades = normalize_features(
                [release_decade_label(entry.item.release_datetime)],
                normalize_person_name,
            )

            _update_affinity_maps(
                genre_weights,
                recent_genre_weights,
                phase_genre_weights,
                genres,
                weight=weight,
                activity_dt=activity_dt,
                now=now,
            )
            _update_affinity_maps(
                tag_weights,
                recent_tag_weights,
                phase_tag_weights,
                tags,
                weight=weight,
                activity_dt=activity_dt,
                now=now,
            )
            _update_affinity_maps(
                keyword_weights,
                recent_keyword_weights,
                phase_keyword_weights,
                keywords,
                weight=weight,
                activity_dt=activity_dt,
                now=now,
            )
            _update_affinity_maps(
                studio_weights,
                recent_studio_weights,
                phase_studio_weights,
                studios,
                weight=weight,
                activity_dt=activity_dt,
                now=now,
            )
            _update_affinity_maps(
                collection_weights,
                recent_collection_weights,
                phase_collection_weights,
                collections,
                weight=weight,
                activity_dt=activity_dt,
                now=now,
            )
            _update_affinity_maps(
                director_weights,
                recent_director_weights,
                phase_director_weights,
                directors,
                weight=weight,
                activity_dt=activity_dt,
                now=now,
            )
            _update_affinity_maps(
                lead_cast_weights,
                recent_lead_cast_weights,
                phase_lead_cast_weights,
                lead_cast,
                weight=weight,
                activity_dt=activity_dt,
                now=now,
            )
            _update_affinity_maps(
                certification_weights,
                recent_certification_weights,
                phase_certification_weights,
                certification,
                weight=weight,
                activity_dt=activity_dt,
                now=now,
            )
            _update_affinity_maps(
                runtime_bucket_weights,
                recent_runtime_bucket_weights,
                phase_runtime_bucket_weights,
                runtime_buckets,
                weight=weight,
                activity_dt=activity_dt,
                now=now,
            )
            _update_affinity_maps(
                decade_weights,
                recent_decade_weights,
                phase_decade_weights,
                decades,
                weight=weight,
                activity_dt=activity_dt,
                now=now,
            )
            for person in people:
                person_weights[person] += weight

            if (
                media_type_key in WORLD_RATING_PROFILE_MEDIA_TYPES
                and getattr(entry, "status", None) == Status.COMPLETED.value
                and getattr(entry, "score", None) is not None
            ):
                world_payload = blended_world_quality(
                    provider_rating=getattr(entry.item, "provider_rating", None),
                    provider_votes=getattr(entry.item, "provider_rating_count", None),
                    trakt_rating=getattr(entry.item, "trakt_rating", None),
                    trakt_votes=getattr(entry.item, "trakt_rating_count", None),
                )
                world_quality = float(world_payload.get("world_quality", 0.5))
                if world_payload.get("world_source_blend") != "neutral":
                    world_rating_user_scores.append(
                        max(0.0, min(1.0, float(entry.score) / 10.0)),
                    )
                    world_rating_scores.append(world_quality)
                    world_rating_weights.append(max(weight, 0.1))

        feedback_rows = list(
            DiscoverFeedback.objects.filter(
                user=user,
                feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
                item__media_type=media_type_key,
            )
            .select_related("item")
            .only(
                "item_id",
                "created_at",
                "updated_at",
                "item__genres",
                "item__media_type",
            ),
        )
        if not feedback_rows:
            continue

        feedback_item_ids = [entry.item_id for entry in feedback_rows if entry.item_id]
        feedback_tag_map: dict[int, list[str]] = defaultdict(list)
        for item_tag in ItemTag.objects.filter(
            item_id__in=feedback_item_ids,
            tag__user=user,
        ).select_related("tag"):
            tag_name = (item_tag.tag.name or "").strip()
            if tag_name:
                feedback_tag_map[item_tag.item_id].append(tag_name)

        feedback_person_map: dict[int, list[str]] = defaultdict(list)
        if media_type_key in VIDEO_COMFORT_PROFILE_MEDIA_TYPES:
            for credit in ItemPersonCredit.objects.filter(item_id__in=feedback_item_ids).select_related("person"):
                person_name = normalize_person_name(credit.person.name if credit.person_id else "")
                if person_name:
                    feedback_person_map[credit.item_id].append(person_name)

        for feedback in feedback_rows:
            activity_snapshot = _latest_datetime(
                activity_snapshot,
                getattr(feedback, "updated_at", None),
                getattr(feedback, "created_at", None),
            )
            weight = _feedback_weight(feedback, now)
            genres = [
                str(genre).strip()
                for genre in (feedback.item.genres or [])
                if str(genre).strip()
            ]
            for genre in genres:
                negative_genre_weights[genre.lower()] += weight
            for tag in feedback_tag_map.get(feedback.item_id, []):
                negative_tag_weights[tag.lower()] += weight
            for person in feedback_person_map.get(feedback.item_id, []):
                negative_person_weights[person] += weight

    world_alignment = weighted_pearson_correlation(
        world_rating_user_scores,
        world_rating_scores,
        world_rating_weights,
    )
    world_sample_size = len(world_rating_scores)
    world_confidence = min(
        world_sample_size / WORLD_RATING_PROFILE_MAX_CONFIDENCE_SAMPLE,
        1.0,
    )

    return ProfilePayload(
        genre_affinity=normalize_numeric_map(dict(genre_weights)),
        recent_genre_affinity=normalize_numeric_map(dict(recent_genre_weights)),
        phase_genre_affinity=normalize_numeric_map(dict(phase_genre_weights)),
        tag_affinity=normalize_numeric_map(dict(tag_weights)),
        recent_tag_affinity=normalize_numeric_map(dict(recent_tag_weights)),
        phase_tag_affinity=normalize_numeric_map(dict(phase_tag_weights)),
        keyword_affinity=normalize_numeric_map(dict(keyword_weights)),
        recent_keyword_affinity=normalize_numeric_map(dict(recent_keyword_weights)),
        phase_keyword_affinity=normalize_numeric_map(dict(phase_keyword_weights)),
        studio_affinity=normalize_numeric_map(dict(studio_weights)),
        recent_studio_affinity=normalize_numeric_map(dict(recent_studio_weights)),
        phase_studio_affinity=normalize_numeric_map(dict(phase_studio_weights)),
        collection_affinity=normalize_numeric_map(dict(collection_weights)),
        recent_collection_affinity=normalize_numeric_map(dict(recent_collection_weights)),
        phase_collection_affinity=normalize_numeric_map(dict(phase_collection_weights)),
        director_affinity=normalize_numeric_map(dict(director_weights)),
        recent_director_affinity=normalize_numeric_map(dict(recent_director_weights)),
        phase_director_affinity=normalize_numeric_map(dict(phase_director_weights)),
        lead_cast_affinity=normalize_numeric_map(dict(lead_cast_weights)),
        recent_lead_cast_affinity=normalize_numeric_map(dict(recent_lead_cast_weights)),
        phase_lead_cast_affinity=normalize_numeric_map(dict(phase_lead_cast_weights)),
        certification_affinity=normalize_numeric_map(dict(certification_weights)),
        recent_certification_affinity=normalize_numeric_map(dict(recent_certification_weights)),
        phase_certification_affinity=normalize_numeric_map(dict(phase_certification_weights)),
        runtime_bucket_affinity=normalize_numeric_map(dict(runtime_bucket_weights)),
        recent_runtime_bucket_affinity=normalize_numeric_map(dict(recent_runtime_bucket_weights)),
        phase_runtime_bucket_affinity=normalize_numeric_map(dict(phase_runtime_bucket_weights)),
        decade_affinity=normalize_numeric_map(dict(decade_weights)),
        recent_decade_affinity=normalize_numeric_map(dict(recent_decade_weights)),
        phase_decade_affinity=normalize_numeric_map(dict(phase_decade_weights)),
        comfort_library_affinity=comfort_library_affinity,
        comfort_rewatch_affinity=comfort_rewatch_affinity,
        person_affinity=normalize_numeric_map(dict(person_weights)),
        negative_genre_affinity=normalize_numeric_map(dict(negative_genre_weights)),
        negative_tag_affinity=normalize_numeric_map(dict(negative_tag_weights)),
        negative_person_affinity=normalize_numeric_map(dict(negative_person_weights)),
        world_rating_profile={
            "alignment": round(float(world_alignment), 6),
            "confidence": round(float(world_confidence), 6),
            "sample_size": world_sample_size,
        },
        activity_snapshot_at=activity_snapshot,
    )


def get_or_compute_taste_profile(user, media_type: str, *, force: bool = False) -> dict:
    """Return profile payload from DB cache or recompute when stale."""
    cached_entry, is_stale = cache_repo.get_taste_profile(user.id, media_type)

    missing_video_comfort_backfill = False
    if cached_entry and media_type in VIDEO_COMFORT_PROFILE_MEDIA_TYPES - {MediaTypes.MOVIE.value}:
        comfort_library_affinity = getattr(cached_entry, "comfort_library_affinity", None) or {}
        comfort_rewatch_affinity = getattr(cached_entry, "comfort_rewatch_affinity", None) or {}
        has_cached_video_comfort = any(
            comfort_library_affinity.get(family) or comfort_rewatch_affinity.get(family)
            for family in COMFORT_PROFILE_FAMILIES
        )
        if not has_cached_video_comfort:
            model_name = MODEL_BY_MEDIA_TYPE.get(media_type)
            if model_name:
                model = apps.get_model("app", model_name)
                missing_video_comfort_backfill = model.objects.filter(
                    user=user,
                    status=Status.COMPLETED.value,
                ).exists()

    missing_world_rating_profile_backfill = False
    if cached_entry and media_type in WORLD_RATING_PROFILE_MEDIA_TYPES:
        cached_world_rating_profile = getattr(cached_entry, "world_rating_profile", None) or {}
        cached_world_rating_sample_size = _world_rating_sample_size(cached_world_rating_profile)
        available_world_rating_candidates = _world_rating_candidate_count(user, media_type)
        if cached_world_rating_sample_size <= 0:
            missing_world_rating_profile_backfill = available_world_rating_candidates > 0
        elif (
            cached_world_rating_sample_size < WORLD_RATING_PROFILE_ACTIVATION_MIN_SAMPLE
            and available_world_rating_candidates >= WORLD_RATING_PROFILE_ACTIVATION_MIN_SAMPLE
            and available_world_rating_candidates > cached_world_rating_sample_size
        ):
            missing_world_rating_profile_backfill = True

    if (
        cached_entry
        and not force
        and not is_stale
        and not missing_video_comfort_backfill
        and not missing_world_rating_profile_backfill
        and not has_new_activity(user, media_type, cached_entry.activity_snapshot_at)
    ):
        return {
            "genre_affinity": getattr(cached_entry, "genre_affinity", None) or {},
            "recent_genre_affinity": getattr(cached_entry, "recent_genre_affinity", None) or {},
            "phase_genre_affinity": getattr(cached_entry, "phase_genre_affinity", None) or {},
            "tag_affinity": getattr(cached_entry, "tag_affinity", None) or {},
            "recent_tag_affinity": getattr(cached_entry, "recent_tag_affinity", None) or {},
            "phase_tag_affinity": getattr(cached_entry, "phase_tag_affinity", None) or {},
            "keyword_affinity": getattr(cached_entry, "keyword_affinity", None) or {},
            "recent_keyword_affinity": getattr(cached_entry, "recent_keyword_affinity", None) or {},
            "phase_keyword_affinity": getattr(cached_entry, "phase_keyword_affinity", None) or {},
            "studio_affinity": getattr(cached_entry, "studio_affinity", None) or {},
            "recent_studio_affinity": getattr(cached_entry, "recent_studio_affinity", None) or {},
            "phase_studio_affinity": getattr(cached_entry, "phase_studio_affinity", None) or {},
            "collection_affinity": getattr(cached_entry, "collection_affinity", None) or {},
            "recent_collection_affinity": getattr(cached_entry, "recent_collection_affinity", None) or {},
            "phase_collection_affinity": getattr(cached_entry, "phase_collection_affinity", None) or {},
            "director_affinity": getattr(cached_entry, "director_affinity", None) or {},
            "recent_director_affinity": getattr(cached_entry, "recent_director_affinity", None) or {},
            "phase_director_affinity": getattr(cached_entry, "phase_director_affinity", None) or {},
            "lead_cast_affinity": getattr(cached_entry, "lead_cast_affinity", None) or {},
            "recent_lead_cast_affinity": getattr(cached_entry, "recent_lead_cast_affinity", None) or {},
            "phase_lead_cast_affinity": getattr(cached_entry, "phase_lead_cast_affinity", None) or {},
            "certification_affinity": getattr(cached_entry, "certification_affinity", None) or {},
            "recent_certification_affinity": getattr(cached_entry, "recent_certification_affinity", None) or {},
            "phase_certification_affinity": getattr(cached_entry, "phase_certification_affinity", None) or {},
            "runtime_bucket_affinity": getattr(cached_entry, "runtime_bucket_affinity", None) or {},
            "recent_runtime_bucket_affinity": getattr(cached_entry, "recent_runtime_bucket_affinity", None) or {},
            "phase_runtime_bucket_affinity": getattr(cached_entry, "phase_runtime_bucket_affinity", None) or {},
            "decade_affinity": getattr(cached_entry, "decade_affinity", None) or {},
            "recent_decade_affinity": getattr(cached_entry, "recent_decade_affinity", None) or {},
            "phase_decade_affinity": getattr(cached_entry, "phase_decade_affinity", None) or {},
            "comfort_library_affinity": getattr(cached_entry, "comfort_library_affinity", None) or {},
            "comfort_rewatch_affinity": getattr(cached_entry, "comfort_rewatch_affinity", None) or {},
            "person_affinity": getattr(cached_entry, "person_affinity", None) or {},
            "negative_genre_affinity": getattr(cached_entry, "negative_genre_affinity", None) or {},
            "negative_tag_affinity": getattr(cached_entry, "negative_tag_affinity", None) or {},
            "negative_person_affinity": getattr(cached_entry, "negative_person_affinity", None) or {},
            "world_rating_profile": getattr(cached_entry, "world_rating_profile", None) or {},
            "activity_snapshot_at": cached_entry.activity_snapshot_at,
        }

    profile = compute_taste_profile(user, media_type)
    cache_repo.set_taste_profile(
        user.id,
        media_type,
        genre_affinity=profile.genre_affinity,
        recent_genre_affinity=profile.recent_genre_affinity,
        phase_genre_affinity=profile.phase_genre_affinity,
        tag_affinity=profile.tag_affinity,
        recent_tag_affinity=profile.recent_tag_affinity,
        phase_tag_affinity=profile.phase_tag_affinity,
        keyword_affinity=profile.keyword_affinity,
        recent_keyword_affinity=profile.recent_keyword_affinity,
        phase_keyword_affinity=profile.phase_keyword_affinity,
        studio_affinity=profile.studio_affinity,
        recent_studio_affinity=profile.recent_studio_affinity,
        phase_studio_affinity=profile.phase_studio_affinity,
        collection_affinity=profile.collection_affinity,
        recent_collection_affinity=profile.recent_collection_affinity,
        phase_collection_affinity=profile.phase_collection_affinity,
        director_affinity=profile.director_affinity,
        recent_director_affinity=profile.recent_director_affinity,
        phase_director_affinity=profile.phase_director_affinity,
        lead_cast_affinity=profile.lead_cast_affinity,
        recent_lead_cast_affinity=profile.recent_lead_cast_affinity,
        phase_lead_cast_affinity=profile.phase_lead_cast_affinity,
        certification_affinity=profile.certification_affinity,
        recent_certification_affinity=profile.recent_certification_affinity,
        phase_certification_affinity=profile.phase_certification_affinity,
        runtime_bucket_affinity=profile.runtime_bucket_affinity,
        recent_runtime_bucket_affinity=profile.recent_runtime_bucket_affinity,
        phase_runtime_bucket_affinity=profile.phase_runtime_bucket_affinity,
        decade_affinity=profile.decade_affinity,
        recent_decade_affinity=profile.recent_decade_affinity,
        phase_decade_affinity=profile.phase_decade_affinity,
        comfort_library_affinity=profile.comfort_library_affinity,
        comfort_rewatch_affinity=profile.comfort_rewatch_affinity,
        person_affinity=profile.person_affinity,
        negative_genre_affinity=profile.negative_genre_affinity,
        negative_tag_affinity=profile.negative_tag_affinity,
        negative_person_affinity=profile.negative_person_affinity,
        world_rating_profile=profile.world_rating_profile,
        activity_snapshot_at=profile.activity_snapshot_at,
        ttl_seconds=PROFILE_TTL_SECONDS,
    )
    return profile.to_dict()
