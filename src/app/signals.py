import logging

from celery import states
from celery.signals import before_task_publish
from django.db.models.signals import post_delete, post_save
from django.db.backends.signals import connection_created
from django.dispatch import receiver
from django_celery_results.models import TaskResult

from app.history_cache import invalidate_history_cache, schedule_history_refresh
from app import statistics_cache
from app.models import (
    Anime,
    BoardGame,
    Book,
    Comic,
    Episode,
    Game,
    Manga,
    Movie,
    Music,
    Season,
    TV,
)

logger = logging.getLogger(__name__)


@receiver(connection_created)
def setup_sqlite_pragmas(sender, connection, **kwargs):  # noqa: ARG001
    """Set up SQLite pragmas for WAL mode and busy timeout on connection creation."""
    if connection.vendor == "sqlite":
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=wal;")
        cursor.execute("PRAGMA busy_timeout=5000;")
        cursor.close()


@before_task_publish.connect
def create_task_result_on_publish(
    sender=None, headers=None, body=None, **kwargs
):  # noqa: ARG001
    """Create a TaskResult object with PENDING status on task publish.

    https://github.com/celery/django-celery-results/issues/286#issuecomment-1279161047
    """
    if "task" not in headers:
        return

    TaskResult.objects.store_result(
        content_type="application/json",
        content_encoding="utf-8",
        task_id=headers["id"],
        result=None,
        status=states.PENDING,
        task_name=headers["task"],
        task_args=headers.get("argsrepr", ""),
        task_kwargs=headers.get("kwargsrepr", ""),
    )


@receiver([post_save, post_delete], sender=Episode)
def refresh_history_cache_on_episode_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule history cache refresh when episode activity changes."""
    user_id = getattr(getattr(instance, "related_season", None), "user_id", None)
    if user_id:
        schedule_history_refresh(user_id)
        # Also invalidate statistics cache for all ranges
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Movie)
def refresh_history_cache_on_movie_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule history cache refresh when movie activity changes."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        schedule_history_refresh(user_id)
        # Also invalidate statistics cache for all ranges
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Music)
def refresh_history_cache_on_music_change(sender, instance, **kwargs):  # noqa: ARG001
    """Invalidate and schedule history cache refresh when music activity changes.
    
    We invalidate immediately so the next page load rebuilds fresh,
    rather than relying on potentially delayed background task.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        invalidate_history_cache(user_id)
        schedule_history_refresh(user_id)
        # Also invalidate statistics cache for all ranges
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=TV)
def refresh_statistics_cache_on_tv_change(sender, instance, **kwargs):  # noqa: ARG001
    """Invalidate and schedule statistics cache refresh when TV activity changes."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Season)
def refresh_statistics_cache_on_season_change(sender, instance, **kwargs):  # noqa: ARG001
    """Invalidate and schedule statistics cache refresh when season activity changes."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Anime)
def refresh_statistics_cache_on_anime_change(sender, instance, **kwargs):  # noqa: ARG001
    """Invalidate and schedule statistics cache refresh when anime activity changes."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Manga)
def refresh_statistics_cache_on_manga_change(sender, instance, **kwargs):  # noqa: ARG001
    """Invalidate and schedule statistics cache refresh when manga activity changes."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Book)
def refresh_statistics_cache_on_book_change(sender, instance, **kwargs):  # noqa: ARG001
    """Invalidate and schedule statistics cache refresh when book activity changes."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Comic)
def refresh_statistics_cache_on_comic_change(sender, instance, **kwargs):  # noqa: ARG001
    """Invalidate and schedule statistics cache refresh when comic activity changes."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Game)
def refresh_statistics_cache_on_game_change(sender, instance, **kwargs):  # noqa: ARG001
    """Invalidate and schedule statistics cache refresh when game activity changes."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=BoardGame)
def refresh_statistics_cache_on_boardgame_change(sender, instance, **kwargs):  # noqa: ARG001
    """Invalidate and schedule statistics cache refresh when board game activity changes."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        statistics_cache.invalidate_statistics_cache(user_id)
        statistics_cache.schedule_all_ranges_refresh(user_id)
