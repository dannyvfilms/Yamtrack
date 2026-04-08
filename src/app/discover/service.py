"""Discover orchestration service."""

from __future__ import annotations

import math
import logging
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import FieldDoesNotExist
from django.db import OperationalError
from django.db.models import Count, Q
from django.utils import timezone

from app.discover import cache_repo, tab_cache
from app.discover.feature_metadata import (
    SIGNAL_LABEL_STOPLIST,
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
from app.discover.filters import (
    dedupe_candidates,
    exclude_tracked_items,
    get_feedback_keys_by_media_type,
    get_tracked_keys_by_media_type,
)
from app.discover.profile import MODEL_BY_MEDIA_TYPE, get_or_compute_taste_profile
from app.discover.providers.trakt_adapter import TraktDiscoverAdapter
from app.discover.providers.tmdb_adapter import TMDbDiscoverAdapter
from app.discover.registry import ALL_MEDIA_KEY, DISCOVER_MEDIA_TYPES, get_rows
from app.discover.schemas import CandidateItem, DiscoverPayload, RowDefinition, RowResult
from app.discover.scoring import (
    blended_world_quality,
    cosine_similarity,
    normalize_values,
    score_candidates,
)
from app.models import (
    BasicMedia,
    CreditRoleType,
    Episode,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    ItemTag,
    MediaTypes,
    Season,
    Sources,
    Status,
)
from app.providers import bgg, comicvine, igdb, mal, musicbrainz, openlibrary, services

logger = logging.getLogger(__name__)

MAX_ITEMS_PER_ROW = 12
ROW_CACHE_TTL_SECONDS = 60 * 60
ROW_CACHE_TTL_LOCAL_SECONDS = 60 * 30
STALE_REFRESH_LOCK_SECONDS = 60
TRAKT_POPULAR_PAGE_SIZE = 100
TRAKT_POPULAR_PULL_STEP = 100
TRAKT_POPULAR_DEFAULT_PULL_TARGET = 100
TRAKT_POPULAR_MAX_PULL_TARGET = 2000
ADAPTIVE_PULL_TARGET_META_KEY = "adaptive_pull_target"
ROW_CACHE_SCHEMA_META_KEY = "schema_version"
ROW_CACHE_ACTIVITY_VERSION_META_KEY = "activity_version"
MOVIE_CANON_ROW_SCHEMA_VERSION = 2
MOVIE_COMING_SOON_ROW_SCHEMA_VERSION = 1
MOVIE_PERSONALIZED_ROW_SCHEMA_VERSION = 4
TV_ANIME_TRAKT_ROW_SCHEMA_VERSION = 1
TV_ANIME_PERSONALIZED_ROW_SCHEMA_VERSION = 4
ROW_CANDIDATE_BUFFER_MULTIPLIER = 5
ARTWORK_HYDRATION_ITEMS_PER_ROW = MAX_ITEMS_PER_ROW * 2
MOVIE_PERSONALIZED_ROW_KEYS = {
    "top_picks_for_you",
    "comfort_rewatches",
}
TV_ANIME_PERSONALIZED_ROW_KEYS = {
    "top_picks_for_you",
    "clear_out_next",
    "comfort_rewatches",
}
TV_ANIME_ROW_KEYS = {
    "trending_right_now",
    "all_time_greats_unseen",
    "coming_soon",
    "top_picks_for_you",
    "clear_out_next",
    "comfort_rewatches",
}
BEHAVIOR_FIRST_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
}
WORLD_QUALITY_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
}
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
PROVIDER_DISCOVER_TTL_SECONDS = 60 * 60
PROVIDER_COMING_SOON_WINDOW_DAYS = 180

ALWAYS_VISIBLE_EMPTY_ROWS = {
    "continue",
    "continue_up_next",
    "next_episode",
}
PROVIDER_ARTWORK_HYDRATION_ROW_KEYS = {
    "trending_right_now",
    "all_time_greats_unseen",
    "coming_soon",
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
WORLD_RATING_PROFILE_MIN_SAMPLE_SIZE = 5
WORLD_QUALITY_ALIGNMENT_BASELINE = 0.25
WORLD_QUALITY_ALIGNMENT_WEIGHT = 0.20
WORLD_QUALITY_ALIGNMENT_FLOOR = 0.10
WORLD_QUALITY_ALIGNMENT_CAP = 0.45
COMFORT_DEBUG_TOP_N = 12
COMFORT_SPREAD_COMPRESSION_THRESHOLD = 0.08
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

TMDB_ADAPTER = TMDbDiscoverAdapter()
TRAKT_ADAPTER = TraktDiscoverAdapter()


def _coerce_media_type(media_type: str | None) -> str:
    media_type = (media_type or ALL_MEDIA_KEY).strip().lower()
    if media_type == ALL_MEDIA_KEY:
        return media_type
    if media_type in DISCOVER_MEDIA_TYPES:
        return media_type
    return ALL_MEDIA_KEY


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
        if activity_dt:
            candidate.score_breakdown["days_since_activity"] = float(
                max(0, (timezone.now() - activity_dt).days),
            )
        candidates.append(candidate)

    return candidates


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


def _select_recent_anchors(user, media_type: str, *, max_anchors: int = 3):
    model = _model_for_media_type(media_type)
    if not model:
        return []

    anchors = (
        model.objects.filter(user=user)
        .filter(
            Q(status=Status.COMPLETED.value)
            | Q(score__gte=8),
        )
        .select_related("item")
        .order_by("-end_date", "-progressed_at", "-created_at")[: max_anchors * 3]
    )

    selected = []
    seen_ids = set()
    for entry in anchors:
        if entry.item_id in seen_ids:
            continue
        selected.append(entry)
        seen_ids.add(entry.item_id)
        if len(selected) >= max_anchors:
            break

    return selected


def _api_cached_results(
    provider: str,
    endpoint: str,
    params: dict,
    *,
    ttl_seconds: int,
    fetcher,
) -> list[dict]:
    payload, is_stale = cache_repo.get_api_cache(provider, endpoint, params)
    if payload and not is_stale:
        return list(payload.get("results") or [])

    try:
        results = list(fetcher() or [])
        cache_repo.set_api_cache(
            provider,
            endpoint,
            params,
            {"results": results},
            ttl_seconds=ttl_seconds,
        )
        return results
    except Exception as error:  # noqa: BLE001
        if payload:
            logger.warning(
                "discover_provider_cache_fallback provider=%s endpoint=%s error=%s",
                provider,
                endpoint,
                error,
            )
            return list(payload.get("results") or [])
        logger.warning(
            "discover_provider_fetch_failed provider=%s endpoint=%s error=%s",
            provider,
            endpoint,
            error,
        )
        return []


def _safe_float(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_date(raw) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return None


def _iso_date_from_timestamp(value) -> str | None:
    try:
        if value is None or value == "":
            return None
        timestamp = int(float(value))
        return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _openlibrary_cover_url(entry: dict) -> str:
    cover_id = (
        entry.get("cover_i")
        or entry.get("cover_id")
        or (entry.get("covers") or [None])[0]
    )
    if cover_id:
        return f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
    return settings.IMG_NONE


def _openlibrary_first_edition_id(work_key: str) -> str | None:
    if not work_key or not str(work_key).startswith("/works/"):
        return None

    cache_key = f"discover:openlibrary:first_edition:{work_key}"
    cached_value = cache.get(cache_key)
    if cached_value is not None:
        return str(cached_value) or None

    try:
        payload = services.api_request(
            Sources.OPENLIBRARY.value,
            "GET",
            f"https://openlibrary.org{work_key}/editions.json",
            params={"limit": 1},
        )
    except Exception:  # noqa: BLE001
        cache.set(cache_key, "", timeout=60 * 60 * 6)
        return None

    entries = payload.get("entries") or []
    edition_id = None
    if entries and isinstance(entries[0], dict):
        edition_key = entries[0].get("key")
        if isinstance(edition_key, str) and "/books/" in edition_key:
            edition_id = edition_key.rstrip("/").split("/books/")[-1]

    cache.set(cache_key, edition_id or "", timeout=60 * 60 * 24)
    return edition_id


def _openlibrary_entry_edition_id(entry: dict) -> str | None:
    direct_edition = entry.get("cover_edition_key")
    if isinstance(direct_edition, str) and direct_edition.strip():
        return direct_edition.strip()

    edition_keys = entry.get("edition_key") or entry.get("edition_keys")
    if isinstance(edition_keys, list):
        for edition_key in edition_keys:
            if isinstance(edition_key, str) and edition_key.strip():
                return edition_key.strip()

    editions = entry.get("editions")
    if isinstance(editions, list):
        for edition in editions:
            if not isinstance(edition, dict):
                continue
            key = edition.get("key")
            if isinstance(key, str) and "/books/" in key:
                return key.rstrip("/").split("/books/")[-1]

    key = entry.get("key")
    if isinstance(key, str):
        if "/books/" in key:
            return key.rstrip("/").split("/books/")[-1]
        if key.startswith("/works/"):
            return _openlibrary_first_edition_id(key)

    work = entry.get("work")
    if isinstance(work, dict):
        work_key = work.get("key")
        if isinstance(work_key, str):
            return _openlibrary_first_edition_id(work_key)

    return None


def _openlibrary_trending_candidates(
    *,
    period: str,
    row_key: str,
    source_reason: str,
    limit: int = 100,
) -> list[CandidateItem]:
    endpoint = f"/trending/{period}.json"
    params = {"limit": min(max(limit, 1), 100), "page": 1}

    def fetcher() -> list[dict]:
        payload = services.api_request(
            Sources.OPENLIBRARY.value,
            "GET",
            f"https://openlibrary.org{endpoint}",
            params=params,
        )
        works = payload.get("works") or payload.get("docs") or payload.get("results") or []
        if isinstance(works, dict):
            works = list(works.values())
        return [entry for entry in works if isinstance(entry, dict)]

    entries = _api_cached_results(
        Sources.OPENLIBRARY.value,
        endpoint,
        params,
        ttl_seconds=PROVIDER_DISCOVER_TTL_SECONDS,
        fetcher=fetcher,
    )

    candidates: list[CandidateItem] = []
    for index, entry in enumerate(entries, start=1):
        edition_id = _openlibrary_entry_edition_id(entry)
        if not edition_id:
            continue

        title = (entry.get("title") or entry.get("name") or "").strip()
        if not title:
            continue

        publish_year = _safe_int(entry.get("first_publish_year"))
        release_date = f"{publish_year}-01-01" if publish_year else None
        popularity = _safe_float(entry.get("reading_log_count")) or _safe_float(
            entry.get("want_to_read_count"),
        )
        if popularity is None:
            popularity = float(max(len(entries) - index + 1, 1))

        subjects = entry.get("subject") or entry.get("subjects") or []
        genres = [
            str(subject).strip()
            for subject in subjects
            if str(subject).strip()
        ][:4]

        candidates.append(
            CandidateItem(
                media_type=MediaTypes.BOOK.value,
                source=Sources.OPENLIBRARY.value,
                media_id=str(edition_id),
                title=title,
                image=_openlibrary_cover_url(entry),
                release_date=release_date,
                genres=genres,
                popularity=popularity,
                row_key=row_key,
                source_reason=source_reason,
            ),
        )

    return candidates[:limit]


def _openlibrary_coming_soon_candidates(
    *,
    row_key: str,
    source_reason: str,
    limit: int = 100,
) -> list[CandidateItem]:
    endpoint = "/search.json"
    today = timezone.localdate()
    current_year = today.year
    next_year = today.year + 1
    params = {
        "q": f"publish_year:{current_year} OR publish_year:{next_year}",
        "sort": "new",
        "limit": min(max(limit, 1), 100),
        "page": 1,
        "fields": "title,key,edition_key,cover_i,first_publish_year,publish_year,subject",
    }

    def fetcher() -> list[dict]:
        payload = services.api_request(
            Sources.OPENLIBRARY.value,
            "GET",
            openlibrary.search_url,
            params=params,
        )
        docs = payload.get("docs") or []
        if isinstance(docs, dict):
            docs = [docs]
        return [entry for entry in docs if isinstance(entry, dict)]

    entries = _api_cached_results(
        Sources.OPENLIBRARY.value,
        f"{endpoint}:coming_soon",
        params,
        ttl_seconds=PROVIDER_DISCOVER_TTL_SECONDS,
        fetcher=fetcher,
    )

    candidates: list[CandidateItem] = []
    for index, entry in enumerate(entries, start=1):
        edition_id = _openlibrary_entry_edition_id(entry)
        if not edition_id:
            continue

        title = (entry.get("title") or entry.get("name") or "").strip()
        if not title:
            continue

        publish_year = _safe_int(entry.get("first_publish_year"))
        if publish_year is None:
            publish_years = entry.get("publish_year") or []
            if isinstance(publish_years, list):
                filtered_years = [
                    year
                    for year in (_safe_int(year) for year in publish_years)
                    if year is not None
                ]
                if filtered_years:
                    publish_year = min(filtered_years)

        if publish_year and publish_year < current_year:
            continue

        release_date = f"{publish_year}-01-01" if publish_year else None
        popularity = _safe_float(entry.get("edition_count"))
        if popularity is None:
            popularity = float(max(len(entries) - index + 1, 1))

        subjects = entry.get("subject") or entry.get("subjects") or []
        genres = [
            str(subject).strip()
            for subject in subjects
            if str(subject).strip()
        ][:4]

        candidates.append(
            CandidateItem(
                media_type=MediaTypes.BOOK.value,
                source=Sources.OPENLIBRARY.value,
                media_id=str(edition_id),
                title=title,
                image=_openlibrary_cover_url(entry),
                release_date=release_date,
                genres=genres,
                popularity=popularity,
                row_key=row_key,
                source_reason=source_reason,
            ),
        )

    return candidates[:limit]


def _comicvine_volume_candidates(
    *,
    sort: str,
    row_key: str,
    source_reason: str,
    limit: int = 100,
) -> list[CandidateItem]:
    endpoint = "/volumes/"
    params = {
        "api_key": settings.COMICVINE_API,
        "format": "json",
        "field_list": "id,name,image,start_year,count_of_issues,date_last_updated",
        "sort": sort,
        "limit": min(max(limit, 1), 100),
        "offset": 0,
    }

    def fetcher() -> list[dict]:
        payload = services.api_request(
            Sources.COMICVINE.value,
            "GET",
            f"{comicvine.base_url}{endpoint}",
            params=params,
            headers=comicvine.headers,
        )
        return [entry for entry in (payload.get("results") or []) if isinstance(entry, dict)]

    entries = _api_cached_results(
        Sources.COMICVINE.value,
        endpoint,
        params,
        ttl_seconds=PROVIDER_DISCOVER_TTL_SECONDS,
        fetcher=fetcher,
    )
    candidates: list[CandidateItem] = []
    for index, entry in enumerate(entries, start=1):
        media_id = _safe_int(entry.get("id"))
        title = (entry.get("name") or "").strip()
        if not media_id or not title:
            continue

        start_year = _safe_int(entry.get("start_year"))
        release_date = f"{start_year}-01-01" if start_year else None
        popularity = _safe_float(entry.get("count_of_issues"))
        if popularity is None:
            popularity = float(max(len(entries) - index + 1, 1))

        candidates.append(
            CandidateItem(
                media_type=MediaTypes.COMIC.value,
                source=Sources.COMICVINE.value,
                media_id=str(media_id),
                title=title,
                image=comicvine.get_image(entry),
                release_date=release_date,
                popularity=popularity,
                row_key=row_key,
                source_reason=source_reason,
            ),
        )

    return candidates[:limit]


def _comicvine_coming_soon_volume_candidates(
    *,
    row_key: str,
    source_reason: str,
    limit: int = 100,
) -> list[CandidateItem]:
    endpoint = "/issues/"
    start_date = timezone.localdate().isoformat()
    end_date = (timezone.localdate() + timedelta(days=PROVIDER_COMING_SOON_WINDOW_DAYS)).isoformat()
    params = {
        "api_key": settings.COMICVINE_API,
        "format": "json",
        "field_list": "id,name,issue_number,store_date,cover_date,image,volume",
        "filter": f"store_date:{start_date}|{end_date}",
        "sort": "store_date:asc",
        "limit": min(max(limit * 2, 20), 200),
        "offset": 0,
    }

    def fetcher() -> list[dict]:
        payload = services.api_request(
            Sources.COMICVINE.value,
            "GET",
            f"{comicvine.base_url}{endpoint}",
            params=params,
            headers=comicvine.headers,
        )
        return [entry for entry in (payload.get("results") or []) if isinstance(entry, dict)]

    entries = _api_cached_results(
        Sources.COMICVINE.value,
        f"{endpoint}:coming_soon",
        params,
        ttl_seconds=PROVIDER_DISCOVER_TTL_SECONDS,
        fetcher=fetcher,
    )

    earliest_issue_by_volume: dict[int, dict] = {}
    for entry in entries:
        volume = entry.get("volume") or {}
        volume_id = _safe_int(volume.get("id"))
        if not volume_id:
            continue
        release_date = _iso_date(entry.get("store_date")) or _iso_date(entry.get("cover_date"))
        existing = earliest_issue_by_volume.get(volume_id)
        if existing is None:
            earliest_issue_by_volume[volume_id] = {
                "volume_name": str(volume.get("name") or "").strip(),
                "release_date": release_date,
                "image": comicvine.get_image(entry),
            }
            continue

        existing_release = existing.get("release_date")
        if release_date and (not existing_release or release_date < existing_release):
            earliest_issue_by_volume[volume_id] = {
                "volume_name": str(volume.get("name") or "").strip(),
                "release_date": release_date,
                "image": comicvine.get_image(entry),
            }

    candidates: list[CandidateItem] = []
    sorted_volumes = sorted(
        earliest_issue_by_volume.items(),
        key=lambda item: (item[1].get("release_date") or "9999-12-31", item[0]),
    )
    for index, (volume_id, payload) in enumerate(sorted_volumes, start=1):
        title = payload.get("volume_name") or ""
        if not title:
            continue
        candidates.append(
            CandidateItem(
                media_type=MediaTypes.COMIC.value,
                source=Sources.COMICVINE.value,
                media_id=str(volume_id),
                title=title,
                image=payload.get("image") or settings.IMG_NONE,
                release_date=payload.get("release_date"),
                popularity=float(max(len(sorted_volumes) - index + 1, 1)),
                row_key=row_key,
                source_reason=source_reason,
            ),
        )
        if len(candidates) >= limit:
            break

    return candidates[:limit]


def _bgg_hot_candidates(
    *,
    row_key: str,
    source_reason: str,
    limit: int = 100,
) -> list[CandidateItem]:
    endpoint = "/xmlapi2/hot"
    params = {"type": "boardgame"}
    headers = {"Authorization": f"Bearer {settings.BGG_API_TOKEN}"}

    def fetcher() -> list[dict]:
        root = services.api_request(
            Sources.BGG.value,
            "GET",
            f"{bgg.base_url}/hot",
            params=params,
            headers=headers,
            response_format="xml",
        )
        entries: list[dict] = []
        for item in root.findall(".//item"):
            name_node = item.find("name")
            year_node = item.find("yearpublished")
            entries.append(
                {
                    "id": item.get("id"),
                    "rank": item.get("rank"),
                    "title": name_node.get("value") if name_node is not None else None,
                    "year": year_node.get("value") if year_node is not None else None,
                },
            )
        return entries

    entries = _api_cached_results(
        Sources.BGG.value,
        endpoint,
        params,
        ttl_seconds=PROVIDER_DISCOVER_TTL_SECONDS,
        fetcher=fetcher,
    )

    ids = [str(entry.get("id")) for entry in entries[:limit] if entry.get("id")]
    thumbnails = bgg._fetch_thumbnails(ids) if ids else {}  # noqa: SLF001

    candidates: list[CandidateItem] = []
    for index, entry in enumerate(entries, start=1):
        media_id = str(entry.get("id") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not media_id or not title:
            continue
        release_year = _safe_int(entry.get("year"))
        release_date = f"{release_year}-01-01" if release_year else None
        popularity = _safe_float(entry.get("rank"))
        if popularity is None:
            popularity = float(max(len(entries) - index + 1, 1))
        else:
            popularity = max(1.0, 1000.0 - popularity)

        candidates.append(
            CandidateItem(
                media_type=MediaTypes.BOARDGAME.value,
                source=Sources.BGG.value,
                media_id=media_id,
                title=title,
                image=thumbnails.get(media_id, settings.IMG_NONE),
                release_date=release_date,
                popularity=popularity,
                row_key=row_key,
                source_reason=source_reason,
            ),
        )

    return candidates[:limit]


def _musicbrainz_coming_soon_recording_candidates(
    *,
    row_key: str,
    source_reason: str,
    limit: int = 100,
) -> list[CandidateItem]:
    endpoint = "/recording/"
    start_date = timezone.localdate().isoformat()
    end_date = (timezone.localdate() + timedelta(days=PROVIDER_COMING_SOON_WINDOW_DAYS)).isoformat()
    params = {
        "query": f'firstreleasedate:[{start_date} TO {end_date}]',
        "limit": min(max(limit, 1), 100),
        "offset": 0,
        "fmt": "json",
        "inc": "artist-credits+releases+release-groups",
    }

    def fetcher() -> list[dict]:
        payload = services.api_request(
            Sources.MUSICBRAINZ.value,
            "GET",
            f"{musicbrainz.BASE_URL}/recording/",
            params=params,
            headers={
                "User-Agent": musicbrainz.USER_AGENT,
                "Accept": "application/json",
            },
        )
        recordings = payload.get("recordings") or []
        if isinstance(recordings, dict):
            recordings = [recordings]
        return [entry for entry in recordings if isinstance(entry, dict)]

    entries = _api_cached_results(
        Sources.MUSICBRAINZ.value,
        f"{endpoint}:coming_soon",
        params,
        ttl_seconds=PROVIDER_DISCOVER_TTL_SECONDS,
        fetcher=fetcher,
    )

    candidates: list[CandidateItem] = []
    for index, entry in enumerate(entries, start=1):
        media_id = str(entry.get("id") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not media_id or not title:
            continue

        artist_credits = entry.get("artist-credit") or []
        artist_name_parts: list[str] = []
        if isinstance(artist_credits, list):
            for credit in artist_credits:
                if not isinstance(credit, dict):
                    continue
                artist = credit.get("artist") or {}
                artist_name_parts.append(
                    str(credit.get("name") or artist.get("name") or "").strip(),
                )
                artist_name_parts.append(str(credit.get("joinphrase") or ""))
        artist_name = "".join(part for part in artist_name_parts if part).strip()

        release_date = _iso_date(entry.get("first-release-date"))
        image = settings.IMG_NONE
        releases = entry.get("releases") or []
        if isinstance(releases, list):
            selected_release = None
            for release in releases:
                if not isinstance(release, dict):
                    continue
                if release.get("date"):
                    selected_release = release
                    break
            if selected_release is None and releases:
                selected_release = releases[0]
            if isinstance(selected_release, dict):
                release_date = release_date or _iso_date(selected_release.get("date"))

        display_title = title if not artist_name else f"{title} - {artist_name}"
        popularity = _safe_float(entry.get("score")) or float(max(len(entries) - index + 1, 1))
        candidates.append(
            CandidateItem(
                media_type=MediaTypes.MUSIC.value,
                source=Sources.MUSICBRAINZ.value,
                media_id=media_id,
                title=display_title,
                image=image,
                release_date=release_date,
                popularity=popularity,
                row_key=row_key,
                source_reason=source_reason,
            ),
        )

    return candidates[:limit]


def _itunes_top_podcasts_candidates(
    *,
    row_key: str,
    source_reason: str,
    limit: int = 100,
) -> list[CandidateItem]:
    endpoint = "/itunes/top-podcasts"
    params = {"country": "us", "limit": min(max(limit, 1), 100)}

    def fetcher() -> list[dict]:
        payload = services.api_request(
            Sources.POCKETCASTS.value,
            "GET",
            f"https://itunes.apple.com/us/rss/toppodcasts/limit={params['limit']}/json",
        )
        entries = ((payload.get("feed") or {}).get("entry") or [])
        if isinstance(entries, dict):
            entries = [entries]
        return [entry for entry in entries if isinstance(entry, dict)]

    entries = _api_cached_results(
        Sources.POCKETCASTS.value,
        endpoint,
        params,
        ttl_seconds=PROVIDER_DISCOVER_TTL_SECONDS,
        fetcher=fetcher,
    )

    candidates: list[CandidateItem] = []
    for index, entry in enumerate(entries, start=1):
        media_id = (
            ((entry.get("id") or {}).get("attributes") or {}).get("im:id")
            or ((entry.get("id") or {}).get("label") or "").strip().rsplit("/", 1)[-1]
        )
        title = ((entry.get("im:name") or {}).get("label") or "").strip()
        if not media_id or not title:
            continue

        image = settings.IMG_NONE
        images = entry.get("im:image") or []
        if isinstance(images, list) and images:
            image = ((images[-1] or {}).get("label") or "").strip() or settings.IMG_NONE
        release_text = (
            ((entry.get("im:releaseDate") or {}).get("label"))
            or ((entry.get("im:releaseDate") or {}).get("attributes") or {}).get("label")
        )
        release_date = _iso_date(release_text)
        popularity = float(max(len(entries) - index + 1, 1))

        candidates.append(
            CandidateItem(
                media_type=MediaTypes.PODCAST.value,
                source=Sources.POCKETCASTS.value,
                media_id=str(media_id),
                title=title,
                image=image,
                release_date=release_date,
                popularity=popularity,
                row_key=row_key,
                source_reason=source_reason,
            ),
        )

    return candidates[:limit]


def _lastfm_top_tracks_candidates(
    *,
    row_key: str,
    source_reason: str,
    limit: int = 100,
) -> list[CandidateItem]:
    if not settings.LASTFM_API_KEY:
        return []

    endpoint = "/2.0/chart.gettoptracks"
    params = {
        "method": "chart.gettoptracks",
        "api_key": settings.LASTFM_API_KEY,
        "format": "json",
        "limit": min(max(limit, 1), 200),
    }

    def fetcher() -> list[dict]:
        payload = services.api_request(
            "LASTFM",
            "GET",
            "https://ws.audioscrobbler.com/2.0/",
            params=params,
        )
        tracks = ((payload.get("tracks") or {}).get("track") or [])
        if isinstance(tracks, dict):
            tracks = [tracks]
        return [track for track in tracks if isinstance(track, dict)]

    tracks = _api_cached_results(
        Sources.MUSICBRAINZ.value,
        endpoint,
        params,
        ttl_seconds=PROVIDER_DISCOVER_TTL_SECONDS,
        fetcher=fetcher,
    )

    candidates: list[CandidateItem] = []
    for track in tracks:
        mbid = str(track.get("mbid") or "").strip()
        title = str(track.get("name") or "").strip()
        if not mbid or not title:
            continue
        artist_info = track.get("artist") or {}
        artist_name = (
            artist_info.get("name")
            if isinstance(artist_info, dict)
            else str(artist_info)
        )
        images = track.get("image") or []
        image = settings.IMG_NONE
        if isinstance(images, list):
            for img in reversed(images):
                if not isinstance(img, dict):
                    continue
                image_value = str(img.get("#text") or "").strip()
                if image_value:
                    image = image_value
                    break

        listeners = _safe_float(track.get("listeners"))
        playcount = _safe_float(track.get("playcount"))
        popularity = playcount if playcount is not None else listeners

        display_title = title if not artist_name else f"{title} - {artist_name}"
        candidates.append(
            CandidateItem(
                media_type=MediaTypes.MUSIC.value,
                source=Sources.MUSICBRAINZ.value,
                media_id=mbid,
                title=display_title,
                image=image,
                popularity=popularity,
                row_key=row_key,
                source_reason=source_reason,
            ),
        )
        if len(candidates) >= limit:
            break

    return candidates[:limit]


def _mal_manga_ranking_candidates(
    *,
    ranking_type: str,
    row_key: str,
    source_reason: str,
    limit: int = 100,
) -> list[CandidateItem]:
    endpoint = "/manga/ranking"
    params = {
        "ranking_type": ranking_type,
        "limit": min(max(limit, 1), 100),
        "fields": "media_type,start_date,genres,mean,num_scoring_users,main_picture,alternative_titles",
    }
    if settings.MAL_NSFW:
        params["nsfw"] = "true"

    def fetcher() -> list[dict]:
        payload = services.api_request(
            Sources.MAL.value,
            "GET",
            f"{mal.base_url}{endpoint}",
            params=params,
            headers={"X-MAL-CLIENT-ID": settings.MAL_API},
        )
        return [entry for entry in (payload.get("data") or []) if isinstance(entry, dict)]

    entries = _api_cached_results(
        Sources.MAL.value,
        f"{endpoint}:{ranking_type}",
        params,
        ttl_seconds=PROVIDER_DISCOVER_TTL_SECONDS,
        fetcher=fetcher,
    )
    candidates: list[CandidateItem] = []
    for index, entry in enumerate(entries, start=1):
        node = entry.get("node") or {}
        media_id = _safe_int(node.get("id"))
        title = (mal.get_localized_title(node) or node.get("title") or "").strip()
        if not media_id or not title:
            continue
        image = mal.get_image_url(node)
        genres = [
            str(genre.get("name")).strip()
            for genre in (node.get("genres") or [])
            if isinstance(genre, dict) and str(genre.get("name") or "").strip()
        ]
        ranking = entry.get("ranking") or {}
        popularity = _safe_float(ranking.get("rank"))
        if popularity is None:
            popularity = float(max(len(entries) - index + 1, 1))
        else:
            popularity = max(1.0, 1000.0 - popularity)

        candidates.append(
            CandidateItem(
                media_type=MediaTypes.MANGA.value,
                source=Sources.MAL.value,
                media_id=str(media_id),
                title=title,
                original_title=node.get("title") or title,
                localized_title=title,
                image=image,
                release_date=_iso_date(node.get("start_date")),
                genres=genres,
                popularity=popularity,
                rating=_safe_float(node.get("mean")),
                rating_count=_safe_int(node.get("num_scoring_users")),
                row_key=row_key,
                source_reason=source_reason,
            ),
        )

    return candidates[:limit]


def _igdb_games_candidates(
    *,
    query: str,
    endpoint_key: str,
    row_key: str,
    source_reason: str,
    limit: int = 100,
) -> list[CandidateItem]:
    params = {"query": query, "limit": min(max(limit, 1), 100)}

    def fetcher() -> list[dict]:
        access_token = igdb.get_access_token()
        payload = services.api_request(
            Sources.IGDB.value,
            "POST",
            f"{igdb.base_url}/games",
            data=query,
            headers={
                "Client-ID": settings.IGDB_ID,
                "Authorization": f"Bearer {access_token}",
            },
        )
        return [entry for entry in payload if isinstance(entry, dict)]

    entries = _api_cached_results(
        Sources.IGDB.value,
        endpoint_key,
        params,
        ttl_seconds=PROVIDER_DISCOVER_TTL_SECONDS,
        fetcher=fetcher,
    )

    candidates: list[CandidateItem] = []
    for index, entry in enumerate(entries, start=1):
        media_id = _safe_int(entry.get("id"))
        title = (entry.get("name") or "").strip()
        if not media_id or not title:
            continue
        genres = [
            str(genre.get("name")).strip()
            for genre in (entry.get("genres") or [])
            if isinstance(genre, dict) and str(genre.get("name") or "").strip()
        ]
        popularity = _safe_float(entry.get("total_rating_count"))
        if popularity is None:
            popularity = float(max(len(entries) - index + 1, 1))
        candidates.append(
            CandidateItem(
                media_type=MediaTypes.GAME.value,
                source=Sources.IGDB.value,
                media_id=str(media_id),
                title=title,
                image=igdb.get_image_url(entry),
                release_date=_iso_date_from_timestamp(entry.get("first_release_date")),
                genres=genres,
                popularity=popularity,
                rating=_safe_float(entry.get("total_rating")),
                rating_count=_safe_int(entry.get("total_rating_count")),
                row_key=row_key,
                source_reason=source_reason,
            ),
        )

    return candidates[:limit]


def _provider_row_candidates(media_type: str, row_key: str) -> list[CandidateItem]:
    if row_key == "trending_right_now":
        if media_type == MediaTypes.MOVIE.value:
            return TRAKT_ADAPTER.movie_watched_weekly(limit=100)
        if media_type == MediaTypes.TV.value:
            return TRAKT_ADAPTER.show_watched_weekly(
                limit=100,
                media_type=MediaTypes.TV.value,
            )
        if media_type == MediaTypes.ANIME.value:
            return TRAKT_ADAPTER.show_watched_weekly(
                limit=100,
                media_type=MediaTypes.ANIME.value,
                trakt_genres=["anime"],
            )
        if media_type == MediaTypes.MANGA.value:
            return _mal_manga_ranking_candidates(
                ranking_type="manga",
                row_key=row_key,
                source_reason="MAL ranking",
                limit=100,
            )
        if media_type == MediaTypes.GAME.value:
            recent_cutoff = int((timezone.now() - timedelta(days=90)).timestamp())
            return _igdb_games_candidates(
                query=(
                    "fields name,cover.image_id,first_release_date,total_rating,total_rating_count,genres.name;"
                    f" where first_release_date != null & first_release_date > {recent_cutoff};"
                    " sort total_rating_count desc;"
                    " limit 100;"
                ),
                endpoint_key="/games/trending_right_now",
                row_key=row_key,
                source_reason="IGDB recent popular",
                limit=100,
            )
        if media_type == MediaTypes.BOOK.value:
            return _openlibrary_trending_candidates(
                period="daily",
                row_key=row_key,
                source_reason="Open Library trending",
                limit=100,
            )
        if media_type == MediaTypes.COMIC.value:
            return _comicvine_volume_candidates(
                sort="date_last_updated:desc",
                row_key=row_key,
                source_reason="Comic Vine recently active",
                limit=100,
            )
        if media_type == MediaTypes.BOARDGAME.value:
            return _bgg_hot_candidates(
                row_key=row_key,
                source_reason="BGG hotness",
                limit=100,
            )
        if media_type == MediaTypes.PODCAST.value:
            return _itunes_top_podcasts_candidates(
                row_key=row_key,
                source_reason="iTunes top podcasts",
                limit=100,
            )
        if media_type == MediaTypes.MUSIC.value:
            return _lastfm_top_tracks_candidates(
                row_key=row_key,
                source_reason="Last.fm chart top tracks",
                limit=100,
            )

    if row_key == "all_time_greats_unseen":
        if media_type == MediaTypes.MOVIE.value:
            return TRAKT_ADAPTER.movie_popular(page=1, limit=TRAKT_POPULAR_PAGE_SIZE)
        if media_type == MediaTypes.TV.value:
            return TRAKT_ADAPTER.show_popular(
                page=1,
                limit=TRAKT_POPULAR_PAGE_SIZE,
                media_type=MediaTypes.TV.value,
            )
        if media_type == MediaTypes.ANIME.value:
            return TRAKT_ADAPTER.show_popular(
                page=1,
                limit=TRAKT_POPULAR_PAGE_SIZE,
                media_type=MediaTypes.ANIME.value,
                trakt_genres=["anime"],
            )
        if media_type == MediaTypes.MANGA.value:
            return _mal_manga_ranking_candidates(
                ranking_type="bypopularity",
                row_key=row_key,
                source_reason="MAL popular ranking",
                limit=100,
            )
        if media_type == MediaTypes.GAME.value:
            return _igdb_games_candidates(
                query=(
                    "fields name,cover.image_id,first_release_date,total_rating,total_rating_count,genres.name;"
                    " where first_release_date != null;"
                    " sort total_rating_count desc;"
                    " limit 100;"
                ),
                endpoint_key="/games/all_time_greats_unseen",
                row_key=row_key,
                source_reason="IGDB all-time popular",
                limit=100,
            )
        if media_type == MediaTypes.BOOK.value:
            return _openlibrary_trending_candidates(
                period="monthly",
                row_key=row_key,
                source_reason="Open Library monthly popular",
                limit=100,
            )
        if media_type == MediaTypes.COMIC.value:
            return _comicvine_volume_candidates(
                sort="count_of_issues:desc",
                row_key=row_key,
                source_reason="Comic Vine long-running volumes",
                limit=100,
            )
        if media_type == MediaTypes.BOARDGAME.value:
            return _bgg_hot_candidates(
                row_key=row_key,
                source_reason="BGG top hotness",
                limit=100,
            )
        if media_type == MediaTypes.PODCAST.value:
            return _itunes_top_podcasts_candidates(
                row_key=row_key,
                source_reason="iTunes top podcasts",
                limit=100,
            )
        if media_type == MediaTypes.MUSIC.value:
            return _lastfm_top_tracks_candidates(
                row_key=row_key,
                source_reason="Last.fm chart top tracks",
                limit=100,
            )

    if row_key == "coming_soon":
        if media_type == MediaTypes.MOVIE.value:
            return TRAKT_ADAPTER.movie_anticipated(page=1, limit=TRAKT_POPULAR_PAGE_SIZE)
        if media_type == MediaTypes.TV.value:
            return TRAKT_ADAPTER.show_anticipated(
                page=1,
                limit=TRAKT_POPULAR_PAGE_SIZE,
                media_type=MediaTypes.TV.value,
            )
        if media_type == MediaTypes.ANIME.value:
            return TRAKT_ADAPTER.show_anticipated(
                page=1,
                limit=TRAKT_POPULAR_PAGE_SIZE,
                media_type=MediaTypes.ANIME.value,
                trakt_genres=["anime"],
            )
        if media_type == MediaTypes.MANGA.value:
            candidates = _mal_manga_ranking_candidates(
                ranking_type="upcoming",
                row_key=row_key,
                source_reason="MAL upcoming ranking",
                limit=100,
            )
            if candidates:
                return candidates
            return _mal_manga_ranking_candidates(
                ranking_type="manga",
                row_key=row_key,
                source_reason="MAL upcoming fallback",
                limit=100,
            )
        if media_type == MediaTypes.GAME.value:
            now_ts = int(timezone.now().timestamp())
            return _igdb_games_candidates(
                query=(
                    "fields name,cover.image_id,first_release_date,total_rating,total_rating_count,genres.name;"
                    f" where first_release_date != null & first_release_date > {now_ts};"
                    " sort first_release_date asc;"
                    " limit 100;"
                ),
                endpoint_key="/games/coming_soon",
                row_key=row_key,
                source_reason="IGDB upcoming releases",
                limit=100,
            )
        if media_type == MediaTypes.BOOK.value:
            candidates = _openlibrary_coming_soon_candidates(
                row_key=row_key,
                source_reason="Open Library upcoming releases",
                limit=100,
            )
            if candidates:
                return candidates
            return _openlibrary_trending_candidates(
                period="daily",
                row_key=row_key,
                source_reason="Open Library upcoming fallback",
                limit=100,
            )
        if media_type == MediaTypes.COMIC.value:
            candidates = _comicvine_coming_soon_volume_candidates(
                row_key=row_key,
                source_reason="Comic Vine upcoming issues",
                limit=100,
            )
            if candidates:
                return candidates
            return _comicvine_volume_candidates(
                sort="date_last_updated:desc",
                row_key=row_key,
                source_reason="Comic Vine upcoming fallback",
                limit=100,
            )
        if media_type == MediaTypes.BOARDGAME.value:
            return _bgg_hot_candidates(
                row_key=row_key,
                source_reason="BGG upcoming fallback",
                limit=100,
            )
        if media_type == MediaTypes.PODCAST.value:
            return _itunes_top_podcasts_candidates(
                row_key=row_key,
                source_reason="iTunes upcoming fallback",
                limit=100,
            )
        if media_type == MediaTypes.MUSIC.value:
            candidates = _musicbrainz_coming_soon_recording_candidates(
                row_key=row_key,
                source_reason="MusicBrainz upcoming releases",
                limit=100,
            )
            if candidates:
                return candidates
            return _lastfm_top_tracks_candidates(
                row_key=row_key,
                source_reason="Last.fm upcoming fallback",
                limit=100,
            )

    return []


def _clamp_adaptive_pull_target(value: int | None) -> int:
    if value is None:
        return TRAKT_POPULAR_DEFAULT_PULL_TARGET
    return max(TRAKT_POPULAR_PAGE_SIZE, min(int(value), TRAKT_POPULAR_MAX_PULL_TARGET))


def _get_cached_adaptive_pull_target(user_id: int, media_type: str, row_key: str) -> int:
    cached_payload, _is_stale = cache_repo.get_row_cache(user_id, media_type, row_key)
    if not cached_payload:
        return TRAKT_POPULAR_DEFAULT_PULL_TARGET

    meta = cached_payload.get("meta")
    if not isinstance(meta, dict):
        return TRAKT_POPULAR_DEFAULT_PULL_TARGET

    try:
        return _clamp_adaptive_pull_target(int(meta.get(ADAPTIVE_PULL_TARGET_META_KEY)))
    except (TypeError, ValueError):
        return TRAKT_POPULAR_DEFAULT_PULL_TARGET


def _trakt_ranked_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    fetch_page,
    row_schema_version: int | None,
    seen_identities: set[tuple[str, str, str]] | None = None,
) -> tuple[list[CandidateItem], dict[str, int]]:
    blocked_statuses = {
        Status.COMPLETED.value,
        Status.DROPPED.value,
        Status.PLANNING.value,
    }
    tracked_keys = get_tracked_keys_by_media_type(
        user,
        media_type,
        statuses=blocked_statuses,
    )
    blocked_identities = set(tracked_keys)
    blocked_identities.update(get_feedback_keys_by_media_type(user, media_type))
    if seen_identities and row_key != "all_time_greats_unseen":
        blocked_identities.update(seen_identities)

    required_pull = _get_cached_adaptive_pull_target(user.id, media_type, row_key)
    page = 1
    seen_identities: set[tuple[str, str, str]] = set()
    candidates: list[CandidateItem] = []
    filtered_candidates: list[CandidateItem] = []
    first_success_pull: int | None = None

    while len(candidates) < required_pull and len(candidates) < TRAKT_POPULAR_MAX_PULL_TARGET:
        page_candidates = fetch_page(
            page=page,
            limit=TRAKT_POPULAR_PAGE_SIZE,
        )
        if not page_candidates:
            break

        count_before = len(candidates)
        for candidate in page_candidates:
            identity = candidate.identity()
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
            candidates.append(candidate)

        filtered_candidates = exclude_tracked_items(candidates, blocked_identities)
        if len(filtered_candidates) >= MAX_ITEMS_PER_ROW and first_success_pull is None:
            first_success_pull = len(candidates)

        if len(page_candidates) < TRAKT_POPULAR_PAGE_SIZE:
            break

        if len(candidates) >= required_pull and len(filtered_candidates) < MAX_ITEMS_PER_ROW:
            required_pull = _clamp_adaptive_pull_target(required_pull + TRAKT_POPULAR_PULL_STEP)

        if len(candidates) == count_before:
            break

        page += 1

    if not filtered_candidates:
        filtered_candidates = exclude_tracked_items(candidates, blocked_identities)

    if len(filtered_candidates) >= MAX_ITEMS_PER_ROW:
        next_pull_target = first_success_pull or len(candidates)
    else:
        next_pull_target = max(required_pull, len(candidates))
    next_pull_target = _clamp_adaptive_pull_target(next_pull_target)

    meta: dict[str, int] = {
        ADAPTIVE_PULL_TARGET_META_KEY: next_pull_target,
        "last_pulled_count": len(candidates),
        "last_filtered_count": len(filtered_candidates),
    }
    if row_schema_version is not None:
        meta[ROW_CACHE_SCHEMA_META_KEY] = row_schema_version
    return filtered_candidates, meta


def _trakt_canon_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    seen_identities: set[tuple[str, str, str]] | None = None,
) -> tuple[list[CandidateItem], dict[str, int]]:
    if media_type == MediaTypes.MOVIE.value:
        fetch_page = TRAKT_ADAPTER.movie_popular
        row_schema_version: int | None = MOVIE_CANON_ROW_SCHEMA_VERSION
    elif media_type == MediaTypes.TV.value:
        fetch_page = lambda *, page, limit: TRAKT_ADAPTER.show_popular(  # noqa: E731
            page=page,
            limit=limit,
            media_type=MediaTypes.TV.value,
        )
        row_schema_version = None
    elif media_type == MediaTypes.ANIME.value:
        fetch_page = lambda *, page, limit: TRAKT_ADAPTER.show_popular(  # noqa: E731
            page=page,
            limit=limit,
            media_type=MediaTypes.ANIME.value,
            trakt_genres=["anime"],
        )
        row_schema_version = None
    else:
        return [], {
            ADAPTIVE_PULL_TARGET_META_KEY: TRAKT_POPULAR_DEFAULT_PULL_TARGET,
            "last_pulled_count": 0,
            "last_filtered_count": 0,
        }

    return _trakt_ranked_candidates(
        user,
        media_type,
        row_key=row_key,
        fetch_page=fetch_page,
        row_schema_version=row_schema_version,
        seen_identities=seen_identities,
    )


def _trakt_anticipated_candidates(
    user,
    media_type: str,
    *,
    row_key: str,
    seen_identities: set[tuple[str, str, str]] | None = None,
) -> tuple[list[CandidateItem], dict[str, int]]:
    if media_type == MediaTypes.MOVIE.value:
        fetch_page = TRAKT_ADAPTER.movie_anticipated
        row_schema_version: int | None = MOVIE_COMING_SOON_ROW_SCHEMA_VERSION
    elif media_type == MediaTypes.TV.value:
        fetch_page = lambda *, page, limit: TRAKT_ADAPTER.show_anticipated(  # noqa: E731
            page=page,
            limit=limit,
            media_type=MediaTypes.TV.value,
        )
        row_schema_version = None
    elif media_type == MediaTypes.ANIME.value:
        fetch_page = lambda *, page, limit: TRAKT_ADAPTER.show_anticipated(  # noqa: E731
            page=page,
            limit=limit,
            media_type=MediaTypes.ANIME.value,
            trakt_genres=["anime"],
        )
        row_schema_version = None
    else:
        return [], {
            ADAPTIVE_PULL_TARGET_META_KEY: TRAKT_POPULAR_DEFAULT_PULL_TARGET,
            "last_pulled_count": 0,
            "last_filtered_count": 0,
        }

    return _trakt_ranked_candidates(
        user,
        media_type,
        row_key=row_key,
        fetch_page=fetch_page,
        row_schema_version=row_schema_version,
        seen_identities=seen_identities,
    )


def _related_candidates_for_anchors(
    anchors,
    media_type: str,
    *,
    row_key: str,
    source_reason: str,
) -> list[CandidateItem]:
    candidates: list[CandidateItem] = []
    seen = set()

    for anchor in anchors:
        related = TMDB_ADAPTER.related(media_type, anchor.item.media_id, limit=120)
        for candidate in related:
            identity = candidate.identity()
            if identity in seen:
                continue
            seen.add(identity)
            candidate.row_key = row_key
            candidate.anchor_title = anchor.item.title
            candidate.source_reason = source_reason.format(anchor_title=anchor.item.title)
            candidates.append(candidate)

    return candidates


def _top_profile_genres(profile_payload: dict, *, limit: int = 3) -> list[str]:
    genre_affinity = profile_payload.get("genre_affinity") or {}
    if not genre_affinity:
        return []

    return [
        genre
        for genre, _ in sorted(
            genre_affinity.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:limit]
    ]


def _genre_discovery_candidates(
    media_type: str,
    row_key: str,
    profile_payload: dict,
    *,
    genres: list[str] | None = None,
    apply_scoring: bool = True,
) -> list[CandidateItem]:
    target_genres = genres or _top_profile_genres(profile_payload, limit=3)
    if not target_genres:
        return []

    candidates = TMDB_ADAPTER.genre_discovery(media_type, target_genres, limit=120)
    for candidate in candidates:
        candidate.row_key = row_key
        candidate.source_reason = "Genre affinity"

    if apply_scoring:
        score_candidates(candidates, profile_payload)

    return candidates


def _merge_unique_candidates(*candidate_sets: list[CandidateItem]) -> list[CandidateItem]:
    merged: list[CandidateItem] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate_set in candidate_sets:
        for candidate in candidate_set:
            identity = candidate.identity()
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(candidate)
    return merged


def _related_row_candidates(user, media_type: str, row_key: str, profile_payload: dict) -> list[CandidateItem]:
    anchors = _select_recent_anchors(user, media_type, max_anchors=3)
    candidates = _related_candidates_for_anchors(
        anchors,
        media_type,
        row_key=row_key,
        source_reason="Because you watched {anchor_title}",
    )

    score_candidates(candidates, profile_payload)
    return candidates


def _top_picks_candidates(user, media_type: str, row_key: str, profile_payload: dict) -> list[CandidateItem]:
    candidates = _planning_candidates(
        user,
        media_type,
        row_key=row_key,
        source_reason="Ranked from your planning list",
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
    # Keep Top Picks ranking formula aligned with Comfort Rewatches.
    # Pass user=None to skip _movie_comfort_cooldown_context: planning candidates
    # are unwatched so title cooldown is impossible and the three full-library DB
    # queries it runs are wasted work here.
    return _apply_comfort_confidence(
        candidates,
        profile_payload,
        use_movie_rewatch_model=(media_type in BEHAVIOR_FIRST_MEDIA_TYPES),
        user=None,
    )


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _calibrate_display_score(raw_score: float, *, offset: float, weight: float) -> float:
    return _clamp_unit(offset + (raw_score * weight))


def _apply_top_picks_display_score(candidates: list[CandidateItem]) -> list[CandidateItem]:
    return _calibrate_comfort_display_scores(candidates)


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
    }


def _movie_comfort_cooldown_context(
    user,
    candidates: list[CandidateItem],
) -> dict[str, dict]:
    if not user or not candidates:
        return {"title": {}, "family": {}}

    candidate_media_types = {
        candidate.media_type
        for candidate in candidates
        if candidate.media_type
    }
    if len(candidate_media_types) != 1:
        return {"title": {}, "family": {}}
    media_type = next(iter(candidate_media_types))
    if media_type not in BEHAVIOR_FIRST_MEDIA_TYPES:
        return {"title": {}, "family": {}}

    model = _model_for_media_type(media_type)
    if not model:
        return {"title": {}, "family": {}}

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
        return {"title": {}, "family": {}}

    item_ids = sorted({entry.item_id for entry in entries if entry.item_id})
    studio_map = _item_studio_map(item_ids)
    _people_map, directors_map, lead_cast_map = _item_credit_feature_maps(item_ids)
    now = timezone.now()

    title_activity: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    family_activity: dict[str, dict[str, list[datetime]]] = {
        family: defaultdict(list) for family in MOVIE_COMFORT_FAMILY_WEIGHTS
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
    }


def _movie_ready_now_signal(
    candidate: CandidateItem,
    candidate_families: dict[str, list[str]],
    cooldown_context: dict[str, dict],
) -> dict[str, float]:
    title_key = (str(candidate.source or "").strip(), str(candidate.media_id or "").strip())
    title_signal = (cooldown_context.get("title") or {}).get(title_key, {})

    title_watch_count = float(title_signal.get("watch_count", 0.0))
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

    cooldown_window_days = median_gap_days if title_watch_count >= 2 else MOVIE_COMFORT_COOLDOWN_DEFAULT_DAYS
    cooldown_window_days = max(
        MOVIE_COMFORT_COOLDOWN_MIN_DAYS,
        min(MOVIE_COMFORT_COOLDOWN_MAX_DAYS, cooldown_window_days),
    )
    if title_watch_count >= MOVIE_COMFORT_BURST_HISTORY_MIN_WATCHES:
        cooldown_window_days *= 1.0 - (0.45 * title_burstiness)
    cooldown_window_days *= 1.0 - (0.20 * lane_burstiness)
    cooldown_window_days = max(
        MOVIE_COMFORT_COOLDOWN_MIN_DAYS,
        min(MOVIE_COMFORT_COOLDOWN_MAX_DAYS, cooldown_window_days),
    )

    title_cooldown_penalty = _clamp_unit(
        1.0 - (days_since_title_watch / max(cooldown_window_days, 1.0)),
    )
    burst_replay_allowance = _clamp_unit((title_burstiness * 0.7) + (lane_burstiness * 0.3))
    cooldown_penalty = _clamp_unit(
        title_cooldown_penalty * (1.0 - (burst_replay_allowance * 0.75)),
    )
    ready_now_score = _clamp_unit(1.0 - cooldown_penalty)

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


def _format_phase_label(value: str) -> str:
    key = str(value).strip().lower()
    if not key:
        return ""
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
        candidate.score_breakdown["phase_family_contribution"] = round(recency_phase_fit * 0.25, 6)
        candidate.score_breakdown["hot_recency_contribution"] = 0.0
        candidate.score_breakdown["rating_contribution"] = round(quality_score * 0.10, 6)
        candidate.score_breakdown["rewatch_contribution"] = round(behavior_score * 0.20, 6)
        candidate.score_breakdown["background_contribution"] = round(
            (library_fit * 0.30) + (shape_coverage * 0.05),
            6,
        )
        candidate.score_breakdown["holiday_strength"] = round(holiday_strength, 6)
        candidate.score_breakdown["dampeners_contribution"] = round(seasonal_adjustment, 6)
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

    if row_key in {
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


def _row_ttl_seconds(row_definition: RowDefinition) -> int:
    return ROW_CACHE_TTL_LOCAL_SECONDS if row_definition.source == "local" else ROW_CACHE_TTL_SECONDS


def _is_missing_image(candidate: CandidateItem) -> bool:
    return not candidate.image or candidate.image == settings.IMG_NONE


def _provider_media_type_for_artwork(candidate_media_type: str) -> str | None:
    if candidate_media_type == MediaTypes.MOVIE.value:
        return MediaTypes.MOVIE.value
    if candidate_media_type in {MediaTypes.TV.value, MediaTypes.ANIME.value}:
        return MediaTypes.TV.value
    return None


def _supports_provider_artwork_hydration(candidate: CandidateItem) -> bool:
    return (
        (
            candidate.media_type == MediaTypes.BOARDGAME.value
            and candidate.source == Sources.BGG.value
        )
        or (
            candidate.media_type == MediaTypes.MUSIC.value
            and candidate.source == Sources.MUSICBRAINZ.value
        )
    )


def _hydrate_provider_ranked_artwork(
    candidates: list[CandidateItem],
    *,
    allow_remote: bool = True,
    hydrate_limit: int = MAX_ITEMS_PER_ROW,
) -> None:
    """Hydrate missing artwork for top provider-ranked boardgame/music candidates."""
    display_candidates = [
        candidate
        for candidate in candidates[:hydrate_limit]
        if _supports_provider_artwork_hydration(candidate)
    ]
    if not display_candidates:
        return

    missing = [candidate for candidate in display_candidates if _is_missing_image(candidate)]
    if not missing:
        return

    local_images = {
        (item.media_type, item.source, str(item.media_id)): item.image
        for item in Item.objects.filter(
            media_id__in=[candidate.media_id for candidate in missing],
            media_type__in=[MediaTypes.BOARDGAME.value, MediaTypes.MUSIC.value],
            source__in=[Sources.BGG.value, Sources.MUSICBRAINZ.value],
        ).only("media_type", "source", "media_id", "image")
        if item.image and item.image != settings.IMG_NONE
    }

    for candidate in missing:
        local_image = local_images.get(candidate.identity())
        if local_image:
            candidate.image = local_image

    if not allow_remote:
        return

    for candidate in missing:
        if not _is_missing_image(candidate):
            continue
        try:
            metadata = services.get_media_metadata(
                candidate.media_type,
                candidate.media_id,
                candidate.source,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "discover_provider_artwork_lookup_failed media_type=%s source=%s media_id=%s error=%s",
                candidate.media_type,
                candidate.source,
                candidate.media_id,
                error,
            )
            continue

        image = (metadata or {}).get("image")
        if image and image != settings.IMG_NONE:
            candidate.image = image


def _hydrate_trakt_ranked_artwork(
    media_type: str,
    candidates: list[CandidateItem],
    *,
    allow_remote: bool = True,
    hydrate_limit: int = MAX_ITEMS_PER_ROW,
) -> None:
    """Hydrate missing artwork for displayed Trakt-ranked TMDB candidates."""
    provider_media_type = _provider_media_type_for_artwork(media_type)
    if provider_media_type is None:
        return

    display_candidates = [
        candidate
        for candidate in candidates[:hydrate_limit]
        if candidate.media_type == media_type
        and candidate.source == TMDB_ADAPTER.provider
    ]
    if not display_candidates:
        return

    missing = [candidate for candidate in display_candidates if _is_missing_image(candidate)]
    if not missing:
        return

    local_images = {
        str(item.media_id): item.image
        for item in Item.objects.filter(
            media_type=media_type,
            source=TMDB_ADAPTER.provider,
            media_id__in=[candidate.media_id for candidate in missing],
        ).only("media_id", "image")
        if item.image and item.image != settings.IMG_NONE
    }

    for candidate in missing:
        local_image = local_images.get(str(candidate.media_id))
        if local_image:
            candidate.image = local_image

    if not allow_remote:
        return

    for candidate in missing:
        if not _is_missing_image(candidate):
            continue
        try:
            metadata = services.get_media_metadata(
                provider_media_type,
                candidate.media_id,
                TMDB_ADAPTER.provider,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "discover_tmdb_artwork_lookup_failed media_id=%s error=%s",
                candidate.media_id,
                error,
            )
            continue

        image = (metadata or {}).get("image")
        if image:
            candidate.image = image


def hydrate_visible_row_artwork(
    row: RowResult,
    *,
    allow_remote: bool = True,
) -> None:
    """Hydrate missing artwork for currently visible row items.

    This is used by the optimistic Discover tab-cache patching path so a
    reserve item promoted into the visible 12 can render with poster artwork
    immediately instead of waiting for a later full row rebuild.
    """
    if not row.items:
        return

    if not any(_is_missing_image(item) for item in row.items[:MAX_ITEMS_PER_ROW]):
        return

    effective_media_type = next(
        (
            candidate.media_type
            for candidate in [*row.items, *row.reserve_items]
            if candidate.media_type
        ),
        None,
    )
    if not effective_media_type:
        return

    if effective_media_type in {
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    } and row.key in {
        "trending_right_now",
        "all_time_greats_unseen",
        "coming_soon",
    }:
        _hydrate_trakt_ranked_artwork(
            effective_media_type,
            row.items,
            allow_remote=allow_remote,
        )
        return

    if row.source == "provider" and row.key in PROVIDER_ARTWORK_HYDRATION_ROW_KEYS:
        _hydrate_provider_ranked_artwork(
            row.items,
            allow_remote=allow_remote,
        )


def _blocked_statuses_for_row(row_definition: RowDefinition) -> set[str] | None:
    if row_definition.key in {"trending_right_now", "all_time_greats_unseen", "coming_soon"}:
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
        from app.tasks import refresh_discover_rows

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


def _apply_row_definition_metadata(
    row: RowResult,
    row_definition: RowDefinition,
) -> RowResult:
    """Keep display metadata aligned with the current registry definition."""
    row.title = row_definition.title
    row.mission = row_definition.mission
    row.why = row_definition.why
    row.source = row_definition.source
    row.show_more = row_definition.show_more
    return row


def _row_requires_artwork_rebuild(
    media_type: str,
    row_definition: RowDefinition,
    row: RowResult,
) -> bool:
    if media_type in {MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value} and row_definition.key in {
        "trending_right_now",
        "all_time_greats_unseen",
        "coming_soon",
    }:
        return any(_is_missing_image(item) for item in row.items[:MAX_ITEMS_PER_ROW])

    if row_definition.source == "provider" and row_definition.key in PROVIDER_ARTWORK_HYDRATION_ROW_KEYS:
        return any(
            _supports_provider_artwork_hydration(item) and _is_missing_image(item)
            for item in row.items[:MAX_ITEMS_PER_ROW]
        )
    return False


def _is_row_cache_compatible(
    media_type: str,
    row_definition: RowDefinition,
    cached_payload: dict,
) -> bool:
    required_schema_version = _required_row_cache_schema_version(media_type, row_definition.key)
    if required_schema_version is None:
        return True

    meta = cached_payload.get("meta")
    if not isinstance(meta, dict):
        return False

    try:
        return int(meta.get(ROW_CACHE_SCHEMA_META_KEY, 0)) >= required_schema_version
    except (TypeError, ValueError):
        return False


def _required_row_cache_schema_version(media_type: str, row_key: str) -> int | None:
    if media_type == MediaTypes.MOVIE.value:
        if row_key == "all_time_greats_unseen":
            return MOVIE_CANON_ROW_SCHEMA_VERSION
        if row_key == "coming_soon":
            return MOVIE_COMING_SOON_ROW_SCHEMA_VERSION
        if row_key in MOVIE_PERSONALIZED_ROW_KEYS:
            return MOVIE_PERSONALIZED_ROW_SCHEMA_VERSION
        return None
    if media_type in {MediaTypes.TV.value, MediaTypes.ANIME.value}:
        if row_key in {
            "trending_right_now",
            "all_time_greats_unseen",
            "coming_soon",
        }:
            return TV_ANIME_TRAKT_ROW_SCHEMA_VERSION
        if row_key in TV_ANIME_PERSONALIZED_ROW_KEYS:
            return TV_ANIME_PERSONALIZED_ROW_SCHEMA_VERSION
    return None


def _row_cache_matches_activity_version(
    user_id: int,
    media_type: str,
    cached_payload: dict,
) -> bool:
    meta = cached_payload.get("meta")
    if not isinstance(meta, dict):
        return True

    cached_activity_version = str(meta.get(ROW_CACHE_ACTIVITY_VERSION_META_KEY) or "")
    if not cached_activity_version:
        return True

    return cached_activity_version == tab_cache.get_activity_version(user_id, media_type)


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
