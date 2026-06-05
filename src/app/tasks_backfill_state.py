"""Backfill state management, retry tracking, and shared ID utilities.

Extracted from tasks.py. Re-exported from app.tasks for backward compatibility.
"""

import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from app import history_cache
from app.models import (
    CREDITS_BACKFILL_VERSION,
    Item,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
)

logger = logging.getLogger(__name__)

BACKGROUND_TASK_PRIORITY = getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1)

METADATA_BACKFILL_BASE_DELAY_SECONDS = 60 * 60  # 1 hour
METADATA_BACKFILL_MAX_DELAY_SECONDS = 60 * 60 * 24  # 1 day
METADATA_BACKFILL_MAX_ATTEMPTS = 6
GENRE_BACKFILL_VERSION = 3


def _apply_backfill_state_filters(queryset, field: str):
    now = timezone.now()
    blocked = MetadataBackfillState.objects.filter(field=field).filter(
        Q(give_up=True) | Q(next_retry_at__gt=now),
    ).values("item_id")
    return queryset.exclude(id__in=blocked)


def _backfill_delay_seconds(fail_count: int) -> int:
    if fail_count <= 0:
        return METADATA_BACKFILL_BASE_DELAY_SECONDS
    delay = METADATA_BACKFILL_BASE_DELAY_SECONDS * (2 ** (fail_count - 1))
    return min(delay, METADATA_BACKFILL_MAX_DELAY_SECONDS)


def _record_backfill_failure(item: Item, field: str, error_message: str | None = None) -> bool:
    now = timezone.now()
    state, _ = MetadataBackfillState.objects.get_or_create(item=item, field=field)
    state.fail_count = min(state.fail_count + 1, 9999)
    state.last_attempt_at = now
    if error_message:
        state.last_error = str(error_message)[:500]
    if state.fail_count >= METADATA_BACKFILL_MAX_ATTEMPTS:
        state.give_up = True
        state.next_retry_at = None
    else:
        state.give_up = False
        state.next_retry_at = now + timedelta(seconds=_backfill_delay_seconds(state.fail_count))
    state.save(update_fields=[
        "fail_count",
        "last_attempt_at",
        "next_retry_at",
        "last_error",
        "give_up",
    ])
    if state.give_up:
        logger.warning(
            "metadata_backfill_give_up item_id=%s media_type=%s field=%s fail_count=%s has_reason=%s",
            item.id,
            item.media_type,
            field,
            state.fail_count,
            bool(error_message or state.last_error),
        )
    else:
        logger.info(
            "metadata_backfill_retry_later item_id=%s media_type=%s field=%s fail_count=%s next_retry_at=%s has_reason=%s",
            item.id,
            item.media_type,
            field,
            state.fail_count,
            state.next_retry_at.isoformat() if state.next_retry_at else None,
            bool(error_message or state.last_error),
        )
    return state.give_up


def _record_backfill_success(
    item: Item,
    field: str,
    strategy_version: int | None = None,
) -> None:
    now = timezone.now()
    state, _ = MetadataBackfillState.objects.get_or_create(item=item, field=field)
    state.fail_count = 0
    state.last_attempt_at = now
    state.next_retry_at = None
    state.last_success_at = now
    state.last_error = ""
    state.give_up = False
    update_fields = [
        "fail_count",
        "last_attempt_at",
        "next_retry_at",
        "last_success_at",
        "last_error",
        "give_up",
    ]
    if strategy_version is not None:
        state.strategy_version = int(strategy_version)
        update_fields.append("strategy_version")
    state.save(update_fields=update_fields)


def _filter_backfill_item_ids(item_ids, field: str):
    if not item_ids:
        return []
    now = timezone.now()
    blocked_ids = set(
        MetadataBackfillState.objects.filter(field=field, item_id__in=item_ids)
        .filter(Q(give_up=True) | Q(next_retry_at__gt=now))
        .values_list("item_id", flat=True)
    )
    if field == MetadataBackfillField.CREDITS:
        blocked_ids.update(
            MetadataBackfillState.objects.filter(
                field=field,
                item_id__in=item_ids,
                give_up=False,
                fail_count=0,
                last_success_at__isnull=False,
                strategy_version__gte=CREDITS_BACKFILL_VERSION,
            ).values_list("item_id", flat=True),
        )
    if field == MetadataBackfillField.GENRES:
        blocked_ids.update(
            MetadataBackfillState.objects.filter(
                field=field,
                item_id__in=item_ids,
                give_up=False,
                fail_count=0,
                last_success_at__isnull=False,
                strategy_version__gte=GENRE_BACKFILL_VERSION,
            ).values_list("item_id", flat=True),
        )
    return [item_id for item_id in item_ids if item_id not in blocked_ids]


def _add_user_day_key(user_day_keys, user_id, day_key):
    if not user_id or not day_key:
        return
    user_day_keys[user_id].add(day_key)


def _collect_backfill_day_keys(items, field: str):
    from app.models import (  # noqa: PLC0415
        Anime,
        Book,
        Comic,
        Episode,
        Game,
        Manga,
        Movie,
    )

    user_day_keys = defaultdict(set)
    if not items:
        return user_day_keys

    for item in items:
        if item.media_type == MediaTypes.MOVIE.value:
            rows = Movie.objects.filter(item_id=item.id).values(
                "user_id",
                "start_date",
                "end_date",
                "created_at",
            )
            for row in rows:
                activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
                _add_user_day_key(user_day_keys, row.get("user_id"), history_cache.history_day_key(activity_dt))
            continue

        if item.media_type == MediaTypes.ANIME.value:
            if field == MetadataBackfillField.GENRES:
                continue
            rows = Anime.objects.filter(item_id=item.id).values(
                "user_id",
                "start_date",
                "end_date",
                "created_at",
            )
            for row in rows:
                if field == MetadataBackfillField.RUNTIME and row.get("start_date") and row.get("end_date"):
                    day_keys = history_cache.history_day_keys_for_range(
                        row.get("start_date"),
                        row.get("end_date"),
                    )
                    if day_keys:
                        user_day_keys[row.get("user_id")].update(day_keys)
                    continue
                activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
                _add_user_day_key(user_day_keys, row.get("user_id"), history_cache.history_day_key(activity_dt))
            continue

        if item.media_type == MediaTypes.GAME.value:
            rows = Game.objects.filter(item_id=item.id).values(
                "user_id",
                "start_date",
                "end_date",
                "created_at",
            )
            for row in rows:
                activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
                _add_user_day_key(user_day_keys, row.get("user_id"), history_cache.history_day_key(activity_dt))
            continue

        if item.media_type in (
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        ):
            reading_models = {
                MediaTypes.BOOK.value: Book,
                MediaTypes.COMIC.value: Comic,
                MediaTypes.MANGA.value: Manga,
            }
            model = reading_models[item.media_type]
            rows = model.objects.filter(item_id=item.id).values(
                "user_id",
                "start_date",
                "end_date",
                "created_at",
            )
            for row in rows:
                activity_dt = row.get("end_date") or row.get("start_date") or row.get("created_at")
                _add_user_day_key(user_day_keys, row.get("user_id"), history_cache.history_day_key(activity_dt))
            continue

        if item.media_type == MediaTypes.TV.value and field in (
            MetadataBackfillField.GENRES,
            MetadataBackfillField.CREDITS,
        ):
            rows = Episode.objects.filter(
                related_season__related_tv__item_id=item.id,
            ).values("related_season__user_id", "end_date")
            for row in rows:
                _add_user_day_key(
                    user_day_keys,
                    row.get("related_season__user_id"),
                    history_cache.history_day_key(row.get("end_date")),
                )
            continue

        if item.media_type == MediaTypes.SEASON.value and field == MetadataBackfillField.CREDITS:
            rows = Episode.objects.filter(
                related_season__item_id=item.id,
            ).values("related_season__user_id", "end_date")
            for row in rows:
                _add_user_day_key(
                    user_day_keys,
                    row.get("related_season__user_id"),
                    history_cache.history_day_key(row.get("end_date")),
                )
            continue

        if item.media_type == MediaTypes.EPISODE.value and field == MetadataBackfillField.RUNTIME:
            rows = Episode.objects.filter(item_id=item.id).values(
                "related_season__user_id",
                "end_date",
            )
            for row in rows:
                _add_user_day_key(
                    user_day_keys,
                    row.get("related_season__user_id"),
                    history_cache.history_day_key(row.get("end_date")),
                )

    return user_day_keys


def _schedule_metadata_statistics_refresh(items, field: str, reason: str):
    if not items:
        return
    from app import statistics_cache  # noqa: PLC0415

    user_day_keys = _collect_backfill_day_keys(items, field)
    for user_id, day_keys in user_day_keys.items():
        if not day_keys:
            continue
        statistics_cache.mark_metadata_refreshing(user_id, reason=reason)
        statistics_cache.invalidate_statistics_days(user_id, day_keys, reason=reason)
        statistics_cache.schedule_all_ranges_refresh(
            user_id,
            debounce_seconds=10,
            countdown=3,
            preferred_priority=BACKGROUND_TASK_PRIORITY,
            all_time_priority=BACKGROUND_TASK_PRIORITY,
        )
        logger.info(
            "metadata_refresh_scheduled user_id=%s field=%s days=%s reason=%s",
            user_id,
            field,
            len(day_keys),
            reason,
        )


def _normalize_item_ids(item_ids):
    normalized = []
    for item_id in item_ids or []:
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            continue
        if item_id > 0:
            normalized.append(item_id)
    return sorted(set(normalized))
