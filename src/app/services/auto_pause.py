import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from simple_history.utils import bulk_update_with_history

from app.models import (
    Anime,
    BoardGame,
    Book,
    Comic,
    Game,
    Manga,
    MediaTypes,
    Movie,
    Season,
    Status,
)

logger = logging.getLogger(__name__)

TARGET_MODEL_MAP = {
    MediaTypes.GAME.value: Game,
    MediaTypes.MOVIE.value: Movie,
    MediaTypes.SEASON.value: Season,
    MediaTypes.ANIME.value: Anime,
    MediaTypes.MANGA.value: Manga,
    MediaTypes.BOOK.value: Book,
    MediaTypes.COMIC.value: Comic,
    MediaTypes.BOARDGAME.value: BoardGame,
}


def auto_pause_stale_items(now=None) -> dict[str, int]:
    """Pause stale in-progress media for all users with the feature enabled."""
    now = now or timezone.now()
    User = get_user_model()
    users = User.objects.filter(
        auto_pause_in_progress_enabled=True,
    ).iterator()

    stats = {
        "users_with_rules": 0,
        "items_paused": 0,
    }

    for user in users:
        if not user.auto_pause_rules:
            continue

        paused_count = _process_user_rules(user, now)
        if paused_count is None:
            continue

        stats["users_with_rules"] += 1
        stats["items_paused"] += paused_count

    logger.info(
        "Auto-pause stale items complete: %s users updated, %s items paused",
        stats["users_with_rules"],
        stats["items_paused"],
    )

    return stats


def _process_user_rules(user, now) -> int:
    """Apply auto-pause rules for a single user."""
    paused_total = 0
    rule_cache: dict[str, dict] = {}

    for media_type in TARGET_MODEL_MAP:
        rule = rule_cache.get(media_type)
        if rule is None:
            rule = user.get_auto_pause_rule(media_type)
            rule_cache[media_type] = rule

        if not rule:
            continue

        paused_total += _pause_stale_media_for_type(
            user=user,
            media_type=media_type,
            weeks=rule.get("weeks"),
            now=now,
        )

    return paused_total


def _pause_stale_media_for_type(user, media_type: str, weeks, now) -> int:
    """Pause stale media instances for a specific user and media type."""
    try:
        weeks_value = int(weeks)
    except (TypeError, ValueError):
        weeks_value = 16

    weeks_value = max(1, weeks_value)
    cutoff = now - timedelta(weeks=weeks_value)

    model = TARGET_MODEL_MAP[media_type]
    queryset = model.objects.filter(
        user=user,
        status=Status.IN_PROGRESS.value,
    ).select_related("item")

    if media_type == MediaTypes.SEASON.value:
        queryset = queryset.prefetch_related("episodes__item")

    to_pause: list = []

    for media in queryset:
        last_activity = _get_last_activity(media)
        if last_activity is None or last_activity <= cutoff:
            media.status = Status.PAUSED.value
            to_pause.append(media)

    if not to_pause:
        return 0

    bulk_update_with_history(
        to_pause,
        model,
        fields=["status"],
    )

    logger.debug(
        "Paused %s stale %s item(s) for %s",
        len(to_pause),
        media_type,
        user.username,
    )

    return len(to_pause)


def _get_last_activity(media):
    """Return the best last-activity timestamp for a media record."""
    candidates = [
        getattr(media, "end_date", None),
        getattr(media, "start_date", None),
        getattr(media, "progressed_at", None),
        getattr(media, "created_at", None),
    ]

    for value in candidates:
        if value:
            return value

    return None
