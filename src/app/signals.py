import logging

from celery import states
from celery.signals import before_task_publish
from django.db.backends.signals import connection_created
from django.db.models.signals import post_delete, post_save
from django.db.utils import OperationalError
from django.dispatch import receiver
from django_celery_results.models import TaskResult

from app import statistics_cache
from app import history_cache
from app.models import (
    TV,
    Anime,
    BoardGame,
    Book,
    Comic,
    Episode,
    Game,
    Manga,
    Movie,
    Music,
    Podcast,
    Season,
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
    sender=None, headers=None, body=None, **kwargs,
):
    """Create a TaskResult object with PENDING status on task publish.

    https://github.com/celery/django-celery-results/issues/286#issuecomment-1279161047
    """
    if "task" not in headers:
        return

    try:
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
    except OperationalError as e:
        # Handle disk I/O errors gracefully - log and continue
        # This can happen if the database file is locked or there's a disk issue
        logger.warning("Failed to store task result due to database error: %s", e)
    except Exception as e:  # pragma: no cover
        # Catch any other unexpected errors
        logger.warning("Unexpected error storing task result: %s", e)


@receiver([post_save, post_delete], sender=Episode)
def refresh_history_cache_on_episode_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule history cache refresh when episode activity changes."""
    user_id = getattr(getattr(instance, "related_season", None), "user_id", None)
    if user_id:
        day_key = history_cache.history_day_key(getattr(instance, "end_date", None))
        if day_key:
            history_cache.invalidate_history_days(
                user_id,
                day_keys=[day_key],
                logging_styles=("sessions", "repeats"),
                reason="episode_change",
            )
        # Schedule statistics cache refresh but don't delete cache immediately
        # This allows users to see old data with notification while refresh happens
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Movie)
def refresh_history_cache_on_movie_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule history cache refresh when movie activity changes."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        activity_dt = getattr(instance, "end_date", None) or getattr(instance, "start_date", None)
        day_key = history_cache.history_day_key(activity_dt)
        if day_key:
            history_cache.invalidate_history_days(
                user_id,
                day_keys=[day_key],
                logging_styles=("sessions", "repeats"),
                reason="movie_change",
            )
        # Schedule statistics cache refresh but don't delete cache immediately
        # This allows users to see old data with notification while refresh happens
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Music)
def refresh_history_cache_on_music_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule history cache refresh when music activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        day_key = history_cache.history_day_key(getattr(instance, "end_date", None))
        if day_key:
            history_cache.invalidate_history_days(
                user_id,
                day_keys=[day_key],
                logging_styles=("sessions", "repeats"),
                reason="music_change",
            )
        # Schedule statistics cache refresh but don't delete cache immediately
        # This allows users to see old data with notification while refresh happens
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Podcast)
def refresh_history_cache_on_podcast_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule history cache refresh when podcast activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        day_key = history_cache.history_day_key(getattr(instance, "end_date", None))
        if day_key:
            history_cache.invalidate_history_days(
                user_id,
                day_keys=[day_key],
                logging_styles=("sessions", "repeats"),
                reason="podcast_change",
            )
        # Schedule statistics cache refresh but don't delete cache immediately
        # This allows users to see old data with notification while refresh happens
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=TV)
def refresh_statistics_cache_on_tv_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when TV activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        # Schedule refresh but don't delete cache - old data will show with notification
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Season)
def refresh_statistics_cache_on_season_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when season activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        # Schedule refresh but don't delete cache - old data will show with notification
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Anime)
def refresh_statistics_cache_on_anime_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when anime activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        # Schedule refresh but don't delete cache - old data will show with notification
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Manga)
def refresh_statistics_cache_on_manga_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when manga activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        # Schedule refresh but don't delete cache - old data will show with notification
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Book)
def refresh_statistics_cache_on_book_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when book activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        # Schedule refresh but don't delete cache - old data will show with notification
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Comic)
def refresh_statistics_cache_on_comic_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when comic activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        # Schedule refresh but don't delete cache - old data will show with notification
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=Game)
def refresh_statistics_cache_on_game_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when game activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        start_dt = getattr(instance, "start_date", None) or getattr(instance, "end_date", None)
        end_dt = getattr(instance, "end_date", None) or getattr(instance, "start_date", None)
        range_keys = history_cache.history_day_keys_for_range(start_dt, end_dt)
        session_key = history_cache.history_day_key(end_dt or start_dt)
        if range_keys:
            history_cache.invalidate_history_days(
                user_id,
                day_keys=range_keys,
                logging_styles=("repeats",),
                reason="game_change_repeats",
            )
        if session_key:
            history_cache.invalidate_history_days(
                user_id,
                day_keys=[session_key],
                logging_styles=("sessions",),
                reason="game_change_sessions",
            )
        # Schedule refresh but don't delete cache - old data will show with notification
        statistics_cache.schedule_all_ranges_refresh(user_id)


@receiver([post_save, post_delete], sender=BoardGame)
def refresh_statistics_cache_on_boardgame_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when board game activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    if user_id:
        start_dt = getattr(instance, "start_date", None) or getattr(instance, "end_date", None)
        end_dt = getattr(instance, "end_date", None) or getattr(instance, "start_date", None)
        range_keys = history_cache.history_day_keys_for_range(start_dt, end_dt)
        session_key = history_cache.history_day_key(end_dt or start_dt)
        if range_keys:
            history_cache.invalidate_history_days(
                user_id,
                day_keys=range_keys,
                logging_styles=("repeats",),
                reason="boardgame_change_repeats",
            )
        if session_key:
            history_cache.invalidate_history_days(
                user_id,
                day_keys=[session_key],
                logging_styles=("sessions",),
                reason="boardgame_change_sessions",
            )
        # Schedule refresh but don't delete cache - old data will show with notification
        statistics_cache.schedule_all_ranges_refresh(user_id)
