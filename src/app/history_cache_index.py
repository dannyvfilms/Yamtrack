"""History cache index builder and cache storage helpers."""

import logging
import time
from datetime import datetime, timedelta

from django.apps import apps
from django.core.cache import cache
from django.db import models
from django.db.models.functions import TruncDate
from django.utils import timezone

from app.history_cache_serialization import _serialize_history_day
from app.history_cache_utils import (
    HISTORY_CACHE_TIMEOUT,
    HISTORY_DAY_CACHE_TIMEOUT,
    _cache_key,
    _day_cache_key,
    _day_key_for_date,
    _day_key_from_value,
    _localize_datetime,
    _music_history_user_q,
    _normalize_logging_style,
)
from app.models import BoardGame, Episode, Game, Movie

logger = logging.getLogger(__name__)


def _add_days(days_set, days_iterable):
    added = 0
    for day in days_iterable:
        if day and day not in days_set:
            days_set.add(day)
            added += 1
    return added


def build_history_index(user, logging_style_override=None):
    """Build an ordered list of active history days for a user."""
    build_start = time.perf_counter()
    logging_style = _normalize_logging_style(logging_style_override, user)
    days = set()

    episode_days = Episode.objects.filter(
        related_season__user=user,
        end_date__isnull=False,
    ).annotate(
        day=TruncDate("end_date"),
    ).values_list("day", flat=True).distinct()
    episode_count = _add_days(days, episode_days)

    movie_qs = Movie.objects.filter(user=user).filter(
        models.Q(end_date__isnull=False) | models.Q(start_date__isnull=False),
    )
    movie_end_days = movie_qs.filter(
        end_date__isnull=False,
    ).annotate(
        day=TruncDate("end_date"),
    ).values_list("day", flat=True).distinct()
    movie_start_days = movie_qs.filter(
        end_date__isnull=True,
        start_date__isnull=False,
    ).annotate(
        day=TruncDate("start_date"),
    ).values_list("day", flat=True).distinct()
    movie_count = _add_days(days, movie_end_days)
    movie_count += _add_days(days, movie_start_days)

    HistoricalMusic = apps.get_model("app", "HistoricalMusic")
    music_days = HistoricalMusic.objects.filter(
        _music_history_user_q(user),
        end_date__isnull=False,
    ).annotate(
        day=TruncDate("end_date"),
    ).values_list("day", flat=True).distinct()
    music_count = _add_days(days, music_days)

    HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")
    podcast_days = HistoricalPodcast.objects.filter(
        models.Q(history_user=user) | models.Q(history_user__isnull=True),
        end_date__isnull=False,
    ).annotate(
        day=TruncDate("end_date"),
    ).values_list("day", flat=True).distinct()
    podcast_count = _add_days(days, podcast_days)

    game_count = 0
    boardgame_count = 0
    if logging_style == "sessions":
        games = Game.objects.filter(user=user)
        game_end_days = games.filter(
            end_date__isnull=False,
        ).annotate(
            day=TruncDate("end_date"),
        ).values_list("day", flat=True).distinct()
        game_start_days = games.filter(
            end_date__isnull=True,
            start_date__isnull=False,
        ).annotate(
            day=TruncDate("start_date"),
        ).values_list("day", flat=True).distinct()
        game_created_days = games.filter(
            end_date__isnull=True,
            start_date__isnull=True,
        ).annotate(
            day=TruncDate("created_at"),
        ).values_list("day", flat=True).distinct()
        game_count += _add_days(days, game_end_days)
        game_count += _add_days(days, game_start_days)
        game_count += _add_days(days, game_created_days)

        boardgames = BoardGame.objects.filter(user=user)
        boardgame_end_days = boardgames.filter(
            end_date__isnull=False,
        ).annotate(
            day=TruncDate("end_date"),
        ).values_list("day", flat=True).distinct()
        boardgame_start_days = boardgames.filter(
            end_date__isnull=True,
            start_date__isnull=False,
        ).annotate(
            day=TruncDate("start_date"),
        ).values_list("day", flat=True).distinct()
        boardgame_created_days = boardgames.filter(
            end_date__isnull=True,
            start_date__isnull=True,
        ).annotate(
            day=TruncDate("created_at"),
        ).values_list("day", flat=True).distinct()
        boardgame_count += _add_days(days, boardgame_end_days)
        boardgame_count += _add_days(days, boardgame_start_days)
        boardgame_count += _add_days(days, boardgame_created_days)
    else:
        games = Game.objects.filter(user=user).only(
            "start_date",
            "end_date",
            "created_at",
            "progress",
        )
        for game in games.iterator():
            total_minutes = game.progress or 0
            if total_minutes <= 0:
                continue
            start_dt = game.start_date or game.end_date or game.created_at
            end_dt = game.end_date or game.start_date or game.created_at
            if not start_dt or not end_dt:
                continue
            start_local = _localize_datetime(start_dt)
            end_local = _localize_datetime(end_dt)
            if not start_local or not end_local:
                continue
            start_date = start_local.date()
            end_date = end_local.date()
            if start_date > end_date:
                start_date, end_date = end_date, start_date
            day_count = (end_date - start_date).days + 1
            for offset in range(day_count):
                day_value = start_date + timedelta(days=offset)
                if day_value not in days:
                    days.add(day_value)
                    game_count += 1

        boardgames = BoardGame.objects.filter(user=user).only(
            "start_date",
            "end_date",
            "created_at",
            "progress",
        )
        for boardgame in boardgames.iterator():
            total_plays = boardgame.progress or 0
            if total_plays <= 0:
                continue
            start_dt = boardgame.start_date or boardgame.end_date or boardgame.created_at
            end_dt = boardgame.end_date or boardgame.start_date or boardgame.created_at
            if not start_dt or not end_dt:
                continue
            start_local = _localize_datetime(start_dt)
            end_local = _localize_datetime(end_dt)
            if not start_local or not end_local:
                continue
            start_date = start_local.date()
            end_date = end_local.date()
            if start_date > end_date:
                start_date, end_date = end_date, start_date
            day_count = (end_date - start_date).days + 1
            for offset in range(day_count):
                day_value = start_date + timedelta(days=offset)
                if day_value not in days:
                    days.add(day_value)
                    boardgame_count += 1

    day_list = sorted(days, reverse=True)
    day_keys = [_day_key_for_date(day) for day in day_list]
    logger.info(
        "history_index_build user_id=%s logging_style=%s days=%s episode_days=%s movie_days=%s music_days=%s podcast_days=%s game_days=%s boardgame_days=%s elapsed_ms=%.2f",
        user.id,
        logging_style,
        len(day_keys),
        episode_count,
        movie_count,
        music_count,
        podcast_count,
        game_count,
        boardgame_count,
        (time.perf_counter() - build_start) * 1000,
    )
    return day_keys


def cache_history_days(user_id: int, logging_style: str, history_days):
    """Persist the grouped history in cache."""
    cache_history_payloads(user_id, logging_style, history_days)


def cache_history_payloads(user_id: int, logging_style: str, history_days):
    """Persist index + per-day history payloads in cache."""
    logging_style = _normalize_logging_style(logging_style)
    index_days = []
    day_payloads = {}
    total_entries = 0
    for day in history_days:
        day_date = day.get("date")
        if not day_date:
            continue
        if isinstance(day_date, str):
            try:
                day_date = datetime.strptime(day_date, "%Y-%m-%d").date()
            except ValueError:
                continue
        day_key = _day_key_for_date(day_date)
        index_days.append(day_key)
        total_entries += len(day.get("entries", []))
        day_payloads[_day_cache_key(user_id, logging_style, day_key)] = _serialize_history_day(day)

    cache.set(
        _cache_key(user_id, logging_style),
        {
            "days": index_days,
            "built_at": timezone.now(),
        },
        timeout=HISTORY_CACHE_TIMEOUT,
    )
    if day_payloads:
        cache.set_many(day_payloads, timeout=HISTORY_DAY_CACHE_TIMEOUT)
    logger.info(
        "history_cache_store user_id=%s logging_style=%s days=%s entries=%s",
        user_id,
        logging_style,
        len(index_days),
        total_entries,
    )


def cache_history_index(user_id: int, logging_style: str, day_keys, built_at=None):
    logging_style = _normalize_logging_style(logging_style)
    if built_at is None:
        built_at = timezone.now()
    cache.set(
        _cache_key(user_id, logging_style),
        {
            "days": day_keys,
            "built_at": built_at,
        },
        timeout=HISTORY_CACHE_TIMEOUT,
    )
    return built_at


def _missing_history_day_keys(user_id: int, logging_style: str, day_keys):
    """Return normalized day keys that do not currently have cached payloads."""
    normalized_keys = []
    for value in day_keys or []:
        day_key = _day_key_from_value(value)
        if day_key:
            normalized_keys.append(day_key)

    if not normalized_keys:
        return []

    payloads = cache.get_many(
        [_day_cache_key(user_id, logging_style, day_key) for day_key in normalized_keys],
    )
    return [
        day_key
        for day_key in normalized_keys
        if _day_cache_key(user_id, logging_style, day_key) not in payloads
    ]
