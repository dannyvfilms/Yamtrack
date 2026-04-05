import logging
from contextlib import contextmanager
from contextvars import ContextVar

from celery import states
from celery.signals import before_task_publish
from django.conf import settings
from django.db.backends.signals import connection_created
from django.db.models.signals import post_delete, post_save
from django.db.utils import OperationalError
from django.dispatch import receiver
from django_celery_results.models import TaskResult

from app import statistics_cache
from app import history_cache
from app.discover import tab_cache as discover_tab_cache
from app.models import (
    TV,
    Anime,
    BoardGame,
    Book,
    Comic,
    CollectionEntry,
    DiscoverFeedback,
    DiscoverFeedbackType,
    Episode,
    Game,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    ItemTag,
    Manga,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Movie,
    Music,
    Podcast,
    Season,
    Sources,
)
from lists.smart_rules import sync_smart_lists_for_item

logger = logging.getLogger(__name__)

RUNTIME_BACKFILL_SOURCES = ("tmdb", "mal", "simkl")
GENRE_BACKFILL_SOURCES = ("tmdb", "mal", "simkl", "igdb", "bgg")
DISCOVER_PRIORITY_HISTORY_DEBOUNCE_SECONDS = 15
DISCOVER_PRIORITY_HISTORY_COUNTDOWN = 15
DISCOVER_PRIORITY_STATISTICS_DEBOUNCE_SECONDS = 20
DISCOVER_PRIORITY_STATISTICS_COUNTDOWN = 20
_SUPPRESS_MEDIA_CACHE_CHANGE_SIGNALS: ContextVar[bool] = ContextVar(
    "suppress_media_cache_change_signals",
    default=False,
)


@contextmanager
def suppress_media_cache_change_signals():
    """Temporarily skip tracked-media cache invalidation signal work."""
    token = _SUPPRESS_MEDIA_CACHE_CHANGE_SIGNALS.set(True)
    try:
        yield
    finally:
        _SUPPRESS_MEDIA_CACHE_CHANGE_SIGNALS.reset(token)


def media_cache_change_signals_suppressed() -> bool:
    """Return whether tracked-media cache invalidation signals are suppressed."""
    return bool(_SUPPRESS_MEDIA_CACHE_CHANGE_SIGNALS.get())


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


def _sync_owner_smart_lists_for_items(owner, items):
    """Sync smart-list membership for a deduped set of owner items."""
    if not owner:
        return

    seen_item_ids = set()
    for item in items:
        if not item:
            continue
        if item.id in seen_item_ids:
            continue
        seen_item_ids.add(item.id)
        try:
            sync_smart_lists_for_item(owner=owner, item=item)
        except Exception:
            logger.exception(
                "Failed incremental smart-list sync for owner_id=%s item_id=%s",
                owner.id,
                item.id,
            )


@receiver([post_save, post_delete], sender=TV)
@receiver([post_save, post_delete], sender=Season)
@receiver([post_save, post_delete], sender=Anime)
@receiver([post_save, post_delete], sender=Movie)
@receiver([post_save, post_delete], sender=Manga)
@receiver([post_save, post_delete], sender=Book)
@receiver([post_save, post_delete], sender=Comic)
@receiver([post_save, post_delete], sender=Game)
@receiver([post_save, post_delete], sender=BoardGame)
@receiver([post_save, post_delete], sender=Music)
@receiver([post_save, post_delete], sender=Podcast)
def sync_smart_lists_on_media_change(sender, instance, **kwargs):  # noqa: ARG001
    """Incrementally update smart-list memberships when owner media rows change."""
    _sync_owner_smart_lists_for_items(
        getattr(instance, "user", None),
        [getattr(instance, "item", None)],
    )


@receiver([post_save, post_delete], sender=CollectionEntry)
def sync_smart_lists_on_collection_change(sender, instance, **kwargs):  # noqa: ARG001
    """Incrementally update smart lists when collection ownership changes."""
    owner = getattr(instance, "user", None)
    item = getattr(instance, "item", None)
    if not owner or not item:
        return

    items_to_sync = [item]
    if item.media_type == MediaTypes.EPISODE.value:
        related_show_items = Item.objects.filter(
            media_id=item.media_id,
            source=item.source,
            media_type__in=[
                MediaTypes.TV.value,
                MediaTypes.ANIME.value,
                MediaTypes.SEASON.value,
            ],
        ).only("id", "media_type", "media_id", "source")
        items_to_sync.extend(related_show_items)

    _sync_owner_smart_lists_for_items(owner, items_to_sync)


@receiver([post_save, post_delete], sender=ItemTag)
def sync_smart_lists_on_item_tag_change(sender, instance, **kwargs):  # noqa: ARG001
    """Incrementally update smart lists when a tag is applied to or removed from an item."""
    owner = getattr(getattr(instance, "tag", None), "user", None)
    item = getattr(instance, "item", None)
    if not owner or not item:
        return
    _sync_owner_smart_lists_for_items(owner, [item])


def _invalidate_history_for_media_change(
    user_id: int,
    *,
    day_keys,
    logging_styles,
    reason: str,
    prioritized: bool,
) -> None:
    normalized_day_keys = [day_key for day_key in (day_keys or []) if day_key]
    if not normalized_day_keys:
        return

    history_cache.invalidate_history_days(
        user_id,
        day_keys=normalized_day_keys,
        logging_styles=logging_styles,
        reason=reason,
        refresh_index=not prioritized,
    )
    if not prioritized:
        return

    for logging_style in logging_styles or ("sessions", "repeats"):
        history_cache.schedule_history_refresh(
            user_id,
            logging_style,
            debounce_seconds=DISCOVER_PRIORITY_HISTORY_DEBOUNCE_SECONDS,
            countdown=DISCOVER_PRIORITY_HISTORY_COUNTDOWN,
            warm_days=0,
            day_keys=normalized_day_keys,
            allow_inline=False,
        )


def _schedule_statistics_refresh_for_media_change(user_id: int, *, prioritized: bool) -> None:
    if prioritized:
        statistics_cache.schedule_all_ranges_refresh(
            user_id,
            debounce_seconds=DISCOVER_PRIORITY_STATISTICS_DEBOUNCE_SECONDS,
            countdown=DISCOVER_PRIORITY_STATISTICS_COUNTDOWN,
        )
        return

    statistics_cache.schedule_all_ranges_refresh(user_id)


def _handle_media_cache_change(
    user_id: int | None,
    changed_media_type: str,
    *,
    reason: str,
    history_specs=None,
    statistics_day_values=None,
    schedule_statistics: bool = True,
) -> None:
    if not user_id:
        return
    if media_cache_change_signals_suppressed():
        return

    active_context = discover_tab_cache.get_active_context(user_id)
    prioritized = discover_tab_cache.should_prioritize(active_context, changed_media_type)
    discover_tab_cache.invalidate_for_media_change(user_id, changed_media_type)

    for day_keys, logging_styles in history_specs or []:
        _invalidate_history_for_media_change(
            user_id,
            day_keys=day_keys,
            logging_styles=logging_styles,
            reason=reason,
            prioritized=prioritized,
        )

    normalized_stat_days = [day_value for day_value in (statistics_day_values or []) if day_value]
    if normalized_stat_days:
        statistics_cache.invalidate_statistics_days(
            user_id,
            day_values=normalized_stat_days,
            reason=reason,
        )

    if schedule_statistics:
        _schedule_statistics_refresh_for_media_change(user_id, prioritized=prioritized)


def _invalidate_discover_from_item_tag(instance) -> None:
    owner = getattr(getattr(instance, "tag", None), "user", None)
    item = getattr(instance, "item", None)
    if not owner or not item:
        return
    discover_tab_cache.invalidate_for_media_change(owner.id, item.media_type)


@receiver(post_save, sender=TV)
@receiver(post_save, sender=Season)
@receiver(post_save, sender=Anime)
@receiver(post_save, sender=Movie)
@receiver(post_save, sender=Manga)
@receiver(post_save, sender=Book)
@receiver(post_save, sender=Comic)
@receiver(post_save, sender=Game)
@receiver(post_save, sender=BoardGame)
@receiver(post_save, sender=Music)
@receiver(post_save, sender=Podcast)
def clear_discover_feedback_on_media_save(sender, instance, **kwargs):  # noqa: ARG001
    """Clear hidden Discover feedback when a user explicitly tracks an item."""
    user_id = getattr(instance, "user_id", None)
    item_id = getattr(instance, "item_id", None)
    if not user_id or not item_id:
        return
    DiscoverFeedback.objects.filter(
        user_id=user_id,
        item_id=item_id,
        feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
    ).delete()


def _discover_user_ids_for_credit_item(item) -> tuple[set[int], str | None]:
    if not item:
        return set(), None

    if item.media_type == MediaTypes.MOVIE.value:
        user_ids = Movie.objects.filter(item_id=item.id).values_list("user_id", flat=True).distinct()
        return set(user_ids), MediaTypes.MOVIE.value

    if item.media_type == MediaTypes.TV.value:
        user_ids = TV.objects.filter(item_id=item.id).values_list("user_id", flat=True).distinct()
        return set(user_ids), MediaTypes.TV.value

    if item.media_type == MediaTypes.EPISODE.value:
        user_ids = (
            Episode.objects.filter(item_id=item.id)
            .values_list("related_season__user_id", flat=True)
            .distinct()
        )
        return set(user_ids), MediaTypes.TV.value

    return set(), None


@receiver([post_save, post_delete], sender=ItemTag)
def refresh_discover_cache_on_item_tag_change(sender, instance, **kwargs):  # noqa: ARG001
    """Refresh Discover when item tags change, since they affect taste profiles."""
    _invalidate_discover_from_item_tag(instance)


@receiver([post_save, post_delete], sender=ItemPersonCredit)
def refresh_discover_cache_on_item_person_credit_change(sender, instance, **kwargs):  # noqa: ARG001
    """Refresh Discover when credited people change on tracked movie/TV items."""
    item = getattr(instance, "item", None)
    if item is None and getattr(instance, "item_id", None):
        item = Item.objects.filter(id=instance.item_id).only("id", "media_type").first()
    user_ids, media_type = _discover_user_ids_for_credit_item(item)
    if not media_type:
        return
    for user_id in user_ids:
        discover_tab_cache.invalidate_for_media_change(user_id, media_type)


@receiver([post_save, post_delete], sender=Episode)
def refresh_history_cache_on_episode_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule history cache refresh when episode activity changes."""
    user_id = getattr(getattr(instance, "related_season", None), "user_id", None)
    day_key = history_cache.history_day_key(getattr(instance, "end_date", None))
    _handle_media_cache_change(
        user_id,
        MediaTypes.EPISODE.value,
        reason="episode_change",
        history_specs=[([day_key] if day_key else [], ("sessions", "repeats"))],
        statistics_day_values=[day_key] if day_key else [],
    )


@receiver([post_save, post_delete], sender=Movie)
def refresh_history_cache_on_movie_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule history cache refresh when movie activity changes."""
    user_id = getattr(instance, "user_id", None)
    activity_dt = getattr(instance, "end_date", None) or getattr(instance, "start_date", None)
    day_key = history_cache.history_day_key(activity_dt)
    _handle_media_cache_change(
        user_id,
        MediaTypes.MOVIE.value,
        reason="movie_change",
        history_specs=[([day_key] if day_key else [], ("sessions", "repeats"))],
        statistics_day_values=[day_key] if day_key else [],
    )


def _schedule_credits_backfill_if_needed(item_id):
    if not item_id:
        return
    item_row = Item.objects.filter(
        id=item_id,
        source=Sources.TMDB.value,
        media_type__in=[
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.EPISODE.value,
        ],
    ).values("media_type").first()
    if not item_row:
        return
    media_type = item_row["media_type"]

    has_people = ItemPersonCredit.objects.filter(item_id=item_id).exists()
    has_studios = ItemStudioCredit.objects.filter(item_id=item_id).exists()
    needs_studios = media_type != MediaTypes.EPISODE.value
    if has_people and (has_studios or not needs_studios):
        MetadataBackfillState.objects.filter(
            item_id=item_id,
            field=MetadataBackfillField.CREDITS,
        ).delete()
        return
    from app.tasks import enqueue_credits_backfill_items

    enqueue_credits_backfill_items([item_id], countdown=3)


@receiver(post_save, sender=Episode)
def schedule_credits_backfill_on_episode_play(sender, instance, **kwargs):  # noqa: ARG001
    """Queue credits backfill for episode and related show when an episode play is saved."""
    if not getattr(instance, "end_date", None):
        return
    episode_item_id = getattr(instance, "item_id", None)
    _schedule_credits_backfill_if_needed(episode_item_id)
    related_season = getattr(instance, "related_season", None)
    related_tv = getattr(related_season, "related_tv", None)
    tv_item_id = getattr(related_tv, "item_id", None)
    _schedule_credits_backfill_if_needed(tv_item_id)


@receiver(post_save, sender=Movie)
def schedule_credits_backfill_on_movie_play(sender, instance, **kwargs):  # noqa: ARG001
    """Queue credits backfill for TMDB movies when a play is saved."""
    if not (getattr(instance, "end_date", None) or getattr(instance, "start_date", None)):
        return
    _schedule_credits_backfill_if_needed(getattr(instance, "item_id", None))


@receiver([post_save, post_delete], sender=Music)
def refresh_history_cache_on_music_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule history cache refresh when music activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    day_key = history_cache.history_day_key(getattr(instance, "end_date", None))
    _handle_media_cache_change(
        user_id,
        MediaTypes.MUSIC.value,
        reason="music_change",
        history_specs=[([day_key] if day_key else [], ("sessions", "repeats"))],
        statistics_day_values=[day_key] if day_key else [],
    )


@receiver([post_save, post_delete], sender=Podcast)
def refresh_history_cache_on_podcast_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule history cache refresh when podcast activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    day_key = history_cache.history_day_key(getattr(instance, "end_date", None))
    _handle_media_cache_change(
        user_id,
        MediaTypes.PODCAST.value,
        reason="podcast_change",
        history_specs=[([day_key] if day_key else [], ("sessions", "repeats"))],
        statistics_day_values=[day_key] if day_key else [],
    )


@receiver([post_save, post_delete], sender=TV)
def refresh_statistics_cache_on_tv_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when TV activity changes.

    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    _handle_media_cache_change(
        getattr(instance, "user_id", None),
        MediaTypes.TV.value,
        reason="tv_change",
    )


@receiver(post_delete, sender=TV)
def clear_time_left_cache_on_tv_delete(sender, instance, **kwargs):  # noqa: ARG001
    """Clear time_left cache when TV show is deleted."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        from app.cache_utils import clear_time_left_cache_for_user
        clear_time_left_cache_for_user(user_id)
        logger.debug(
            "Cleared time_left cache for user %s after deleting TV show: %s",
            user_id,
            instance,
        )


@receiver([post_save, post_delete], sender=Season)
def refresh_statistics_cache_on_season_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when season activity changes.

    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    _handle_media_cache_change(
        getattr(instance, "user_id", None),
        MediaTypes.SEASON.value,
        reason="season_change",
    )


@receiver(post_delete, sender=Season)
def clear_time_left_cache_on_season_delete(sender, instance, **kwargs):  # noqa: ARG001
    """Clear time_left cache when Season is deleted."""
    user_id = getattr(instance, "user_id", None)
    if user_id:
        from app.cache_utils import clear_time_left_cache_for_user
        clear_time_left_cache_for_user(user_id)
        logger.debug(
            "Cleared time_left cache for user %s after deleting Season: %s",
            user_id,
            instance,
        )


@receiver([post_save, post_delete], sender=Anime)
def refresh_statistics_cache_on_anime_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when anime activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    _handle_media_cache_change(
        getattr(instance, "user_id", None),
        MediaTypes.ANIME.value,
        reason="anime_change",
    )


def _collect_reading_statistics_day_keys(instance):
    """Return statistics day keys touched by a reading entry."""
    start_dt = getattr(instance, "start_date", None)
    end_dt = getattr(instance, "end_date", None)
    range_keys = history_cache.history_day_keys_for_range(start_dt, end_dt)
    activity_key = history_cache.history_day_key(
        end_dt or start_dt or getattr(instance, "created_at", None),
    )
    day_keys = set(range_keys or [])
    if activity_key:
        day_keys.add(activity_key)
    return day_keys


@receiver([post_save, post_delete], sender=Manga)
def refresh_statistics_cache_on_manga_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when manga activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    day_keys = _collect_reading_statistics_day_keys(instance)
    _handle_media_cache_change(
        user_id,
        MediaTypes.MANGA.value,
        reason="manga_change",
        statistics_day_values=day_keys,
    )


@receiver([post_save, post_delete], sender=Book)
def refresh_statistics_cache_on_book_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when book activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    day_keys = _collect_reading_statistics_day_keys(instance)
    _handle_media_cache_change(
        user_id,
        MediaTypes.BOOK.value,
        reason="book_change",
        statistics_day_values=day_keys,
    )


@receiver([post_save, post_delete], sender=Comic)
def refresh_statistics_cache_on_comic_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when comic activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    day_keys = _collect_reading_statistics_day_keys(instance)
    _handle_media_cache_change(
        user_id,
        MediaTypes.COMIC.value,
        reason="comic_change",
        statistics_day_values=day_keys,
    )


@receiver([post_save, post_delete], sender=Game)
def refresh_statistics_cache_on_game_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when game activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    start_dt = getattr(instance, "start_date", None) or getattr(instance, "end_date", None)
    end_dt = getattr(instance, "end_date", None) or getattr(instance, "start_date", None)
    range_keys = history_cache.history_day_keys_for_range(start_dt, end_dt)
    session_key = history_cache.history_day_key(end_dt or start_dt)
    stats_day_keys = set(range_keys or [])
    if session_key:
        stats_day_keys.add(session_key)
    _handle_media_cache_change(
        user_id,
        MediaTypes.GAME.value,
        reason="game_change",
        history_specs=[
            (range_keys, ("repeats",)),
            ([session_key] if session_key else [], ("sessions",)),
        ],
        statistics_day_values=stats_day_keys,
    )


@receiver([post_save, post_delete], sender=BoardGame)
def refresh_statistics_cache_on_boardgame_change(sender, instance, **kwargs):  # noqa: ARG001
    """Schedule statistics cache refresh when board game activity changes.
    
    We schedule a refresh but don't delete the cache immediately,
    so users can see the old data with a notification while refresh happens.
    """
    user_id = getattr(instance, "user_id", None)
    start_dt = getattr(instance, "start_date", None) or getattr(instance, "end_date", None)
    end_dt = getattr(instance, "end_date", None) or getattr(instance, "start_date", None)
    range_keys = history_cache.history_day_keys_for_range(start_dt, end_dt)
    session_key = history_cache.history_day_key(end_dt or start_dt)
    stats_day_keys = set(range_keys or [])
    if session_key:
        stats_day_keys.add(session_key)
    _handle_media_cache_change(
        user_id,
        MediaTypes.BOARDGAME.value,
        reason="boardgame_change",
        history_specs=[
            (range_keys, ("repeats",)),
            ([session_key] if session_key else [], ("sessions",)),
        ],
        statistics_day_values=stats_day_keys,
    )


@receiver(post_save, sender=Item)
def schedule_runtime_backfill_on_item_save(
    sender,
    instance,
    created,
    update_fields=None,
    **kwargs,
):  # noqa: ARG001
    """Queue runtime/genre/credits backfills for newly created or missing metadata items.
    
    Also invalidates time_left cache when episode runtime changes.
    """
    # Check if runtime_minutes was actually updated (not just saving the same value)
    runtime_updated = (
        update_fields is None or "runtime_minutes" in update_fields
    ) and instance.media_type == MediaTypes.EPISODE.value
    
    # Invalidate time_left cache for all users tracking this show/season when runtime changes
    if runtime_updated:
        from app.cache_utils import clear_time_left_cache_for_user
        from app.models import BasicMedia
        
        # Get all users who track this show or season
        tracking_users = BasicMedia.objects.filter(
            item__media_id=instance.media_id,
            item__source=instance.source,
            item__media_type__in=[MediaTypes.TV.value, MediaTypes.SEASON.value],
        ).values_list("user_id", flat=True).distinct()
        
        for user_id in tracking_users:
            clear_time_left_cache_for_user(user_id)
            logger.debug(
                "Cleared time_left cache for user %s due to runtime update on %s",
                user_id,
                instance,
            )
    
    if instance.runtime_minutes is not None and instance.runtime_minutes != 999999:
        MetadataBackfillState.objects.filter(
            item=instance,
            field=MetadataBackfillField.RUNTIME,
        ).delete()
    genre_identity_fields = {"media_id", "source", "media_type"}
    genre_identity_changed = created or bool(
        genre_identity_fields.intersection(update_fields or set()),
    )
    if genre_identity_changed:
        MetadataBackfillState.objects.filter(
            item=instance,
            field=MetadataBackfillField.GENRES,
        ).delete()
    has_people = False
    has_studios = False
    if instance.source == Sources.TMDB.value and instance.media_type in (
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
    ):
        has_people = ItemPersonCredit.objects.filter(item=instance).exists()
        has_studios = ItemStudioCredit.objects.filter(item=instance).exists()
        if has_people and has_studios:
            MetadataBackfillState.objects.filter(
                item=instance,
                field=MetadataBackfillField.CREDITS,
            ).delete()

    relevant_fields = {"runtime_minutes", "genres", *genre_identity_fields}
    if not created and update_fields is not None and not relevant_fields.intersection(update_fields):
        return

    # Avoid eager backfill task execution during tests; tests call backfill helpers directly.
    if settings.TESTING:
        return

    runtime_missing = instance.runtime_minutes in (None, 0) and instance.runtime_minutes != 999999
    genres_missing = not instance.genres

    if runtime_missing and instance.source in RUNTIME_BACKFILL_SOURCES:
        if instance.media_type in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.ANIME.value,
        ):
            from app.tasks import enqueue_runtime_backfill_items

            enqueue_runtime_backfill_items([instance.id])
        elif instance.media_type == MediaTypes.EPISODE.value and instance.season_number is not None:
            from app.tasks import enqueue_episode_runtime_backfill

            enqueue_episode_runtime_backfill(
                [(instance.media_id, instance.source, instance.season_number)],
            )

    genre_backfill_applicable = (
        instance.source in GENRE_BACKFILL_SOURCES
        and instance.media_type
        in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.ANIME.value,
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
        )
    )
    requires_tmdb_tv_genre_verification = (
        instance.source == Sources.TMDB.value
        and instance.media_type == MediaTypes.TV.value
    )
    tmdb_tv_genre_verification_triggered = requires_tmdb_tv_genre_verification and (
        created or genre_identity_changed or update_fields is None
    )

    if genre_backfill_applicable and (
        genres_missing
        or genre_identity_changed
        or tmdb_tv_genre_verification_triggered
    ):
        from app.tasks import enqueue_genre_backfill_items

        enqueue_genre_backfill_items([instance.id])

    if (
        instance.source == Sources.TMDB.value
        and instance.media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value)
        and (not has_people or not has_studios)
    ):
        from app.tasks import enqueue_credits_backfill_items

        enqueue_credits_backfill_items([instance.id])
