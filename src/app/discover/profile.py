"""Taste profile computation for Discover."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.db.models import Q
from django.utils import timezone

from app.discover import cache_repo
from app.discover.registry import ALL_MEDIA_KEY, DISCOVER_MEDIA_TYPES
from app.discover.scoring import normalize_numeric_map
from app.models import (
    DiscoverFeedback,
    DiscoverFeedbackType,
    ItemPersonCredit,
    ItemTag,
    MediaTypes,
)

PROFILE_TTL_SECONDS = 60 * 60 * 24

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
    person_affinity: dict[str, float]
    negative_genre_affinity: dict[str, float]
    negative_tag_affinity: dict[str, float]
    negative_person_affinity: dict[str, float]
    activity_snapshot_at: timezone.datetime | None

    def to_dict(self) -> dict:
        return {
            "genre_affinity": self.genre_affinity,
            "recent_genre_affinity": self.recent_genre_affinity,
            "phase_genre_affinity": self.phase_genre_affinity,
            "tag_affinity": self.tag_affinity,
            "recent_tag_affinity": self.recent_tag_affinity,
            "phase_tag_affinity": self.phase_tag_affinity,
            "person_affinity": self.person_affinity,
            "negative_genre_affinity": self.negative_genre_affinity,
            "negative_tag_affinity": self.negative_tag_affinity,
            "negative_person_affinity": self.negative_person_affinity,
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


def _model_has_field(model, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)
    except FieldDoesNotExist:
        return False
    return True


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


def has_new_activity(user, media_type: str, snapshot_at) -> bool:
    """Return whether user has new activity after profile snapshot."""
    if snapshot_at is None:
        return True

    for media_type_key in _resolve_media_types(media_type):
        model_name = MODEL_BY_MEDIA_TYPE.get(media_type_key)
        if not model_name:
            continue
        model = apps.get_model("app", model_name)
        activity_filter = Q(created_at__gt=snapshot_at)
        if _model_has_field(model, "progressed_at"):
            activity_filter |= Q(progressed_at__gt=snapshot_at)
        if _model_has_field(model, "end_date"):
            activity_filter |= Q(end_date__gt=snapshot_at)

        query = model.objects.filter(user=user).filter(activity_filter)
        if query.exists():
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
    person_weights: dict[str, float] = defaultdict(float)
    negative_genre_weights: dict[str, float] = defaultdict(float)
    negative_tag_weights: dict[str, float] = defaultdict(float)
    negative_person_weights: dict[str, float] = defaultdict(float)

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
            "created_at",
            "item__genres",
            "item__media_type",
        ]
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
        if media_type_key in {MediaTypes.MOVIE.value, MediaTypes.TV.value}:
            for credit in ItemPersonCredit.objects.filter(item_id__in=item_ids).select_related("person"):
                person_name = (credit.person.name or "").strip() if credit.person_id else ""
                if person_name:
                    person_map[credit.item_id].append(person_name)

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
            if activity_dt and (activity_snapshot is None or activity_dt > activity_snapshot):
                activity_snapshot = activity_dt

            weight = _entry_weight(entry, now, activity_dt=activity_dt)
            genres = [str(genre).strip() for genre in (entry.item.genres or []) if str(genre).strip()]

            for genre in genres:
                genre_key = genre.lower()
                genre_weights[genre_key] += weight
                if activity_dt and activity_dt >= now - timedelta(days=30):
                    recent_genre_weights[genre_key] += weight
                if activity_dt and activity_dt >= now - timedelta(days=90):
                    phase_genre_weights[genre_key] += weight

            for tag in tag_map.get(entry.item_id, []):
                tag_key = tag.lower()
                tag_weights[tag_key] += weight
                if activity_dt and activity_dt >= now - timedelta(days=30):
                    recent_tag_weights[tag_key] += weight
                if activity_dt and activity_dt >= now - timedelta(days=90):
                    phase_tag_weights[tag_key] += weight

            for person in person_map.get(entry.item_id, []):
                person_weights[person.lower()] += weight

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
        if media_type_key in {MediaTypes.MOVIE.value, MediaTypes.TV.value}:
            for credit in ItemPersonCredit.objects.filter(item_id__in=feedback_item_ids).select_related("person"):
                person_name = (credit.person.name or "").strip() if credit.person_id else ""
                if person_name:
                    feedback_person_map[credit.item_id].append(person_name)

        for feedback in feedback_rows:
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
                negative_person_weights[person.lower()] += weight

    return ProfilePayload(
        genre_affinity=normalize_numeric_map(dict(genre_weights)),
        recent_genre_affinity=normalize_numeric_map(dict(recent_genre_weights)),
        phase_genre_affinity=normalize_numeric_map(dict(phase_genre_weights)),
        tag_affinity=normalize_numeric_map(dict(tag_weights)),
        recent_tag_affinity=normalize_numeric_map(dict(recent_tag_weights)),
        phase_tag_affinity=normalize_numeric_map(dict(phase_tag_weights)),
        person_affinity=normalize_numeric_map(dict(person_weights)),
        negative_genre_affinity=normalize_numeric_map(dict(negative_genre_weights)),
        negative_tag_affinity=normalize_numeric_map(dict(negative_tag_weights)),
        negative_person_affinity=normalize_numeric_map(dict(negative_person_weights)),
        activity_snapshot_at=activity_snapshot,
    )


def get_or_compute_taste_profile(user, media_type: str, *, force: bool = False) -> dict:
    """Return profile payload from DB cache or recompute when stale."""
    cached_entry, is_stale = cache_repo.get_taste_profile(user.id, media_type)

    if (
        cached_entry
        and not force
        and not is_stale
        and not has_new_activity(user, media_type, cached_entry.activity_snapshot_at)
    ):
        return {
            "genre_affinity": getattr(cached_entry, "genre_affinity", None) or {},
            "recent_genre_affinity": getattr(cached_entry, "recent_genre_affinity", None) or {},
            "phase_genre_affinity": getattr(cached_entry, "phase_genre_affinity", None) or {},
            "tag_affinity": getattr(cached_entry, "tag_affinity", None) or {},
            "recent_tag_affinity": getattr(cached_entry, "recent_tag_affinity", None) or {},
            "phase_tag_affinity": getattr(cached_entry, "phase_tag_affinity", None) or {},
            "person_affinity": getattr(cached_entry, "person_affinity", None) or {},
            "negative_genre_affinity": getattr(cached_entry, "negative_genre_affinity", None) or {},
            "negative_tag_affinity": getattr(cached_entry, "negative_tag_affinity", None) or {},
            "negative_person_affinity": getattr(cached_entry, "negative_person_affinity", None) or {},
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
        person_affinity=profile.person_affinity,
        negative_genre_affinity=profile.negative_genre_affinity,
        negative_tag_affinity=profile.negative_tag_affinity,
        negative_person_affinity=profile.negative_person_affinity,
        activity_snapshot_at=profile.activity_snapshot_at,
        ttl_seconds=PROFILE_TTL_SECONDS,
    )
    return profile.to_dict()
