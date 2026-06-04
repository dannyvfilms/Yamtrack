"""Discover cache warming and refresh Celery tasks.

Extracted from tasks.py. Re-exported from app.tasks for backward compatibility.
Tasks keep their original explicit Celery names so queued tasks survive the deploy.
"""

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache  # noqa: F401 - imported for potential future use

from app import history_cache
from app.interactive_requests import interactive_request_active
from app.models import MediaTypes

logger = logging.getLogger(__name__)

# Mirrors BACKGROUND_TASK_PRIORITY in tasks.py — both read from the same setting.
BACKGROUND_TASK_PRIORITY = getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1)


@shared_task(name="Refresh Discover Rows")
def refresh_discover_rows(user_id: int, media_type: str, row_keys: list[str], show_more: bool = False):
    """Refresh selected Discover rows for a user."""
    from app.discover.service import refresh_rows_for_user
    from app.discover.tab_cache import refresh_tab_cache
    from app.discover import tab_cache as discover_tab_cache

    user_model = get_user_model()
    user = user_model.objects.filter(id=user_id).first()
    if not user:
        logger.warning("discover_refresh_rows_user_missing user_id=%s", user_id)
        return {"refreshed": 0, "reason": "missing_user"}

    requested_media_type = (media_type or discover_tab_cache.ALL_MEDIA_KEY).strip().lower()
    if (
        requested_media_type != discover_tab_cache.ALL_MEDIA_KEY
        and not discover_tab_cache.media_type_is_enabled_for_user(user, requested_media_type)
    ):
        logger.info(
            "discover_refresh_rows_skipped user_id=%s media_type=%s reason=disabled_media_type",
            user_id,
            requested_media_type,
        )
        return {"refreshed": 0, "reason": "disabled_media_type", "user_id": user_id}

    refreshed = refresh_rows_for_user(
        user,
        requested_media_type,
        row_keys or [],
        show_more=show_more,
    )
    # Keep the higher-level tab cache aligned with refreshed row caches.
    refresh_tab_cache(
        user,
        requested_media_type,
        show_more=show_more,
        force=False,
        clear_provider_cache=False,
    )
    return {
        "refreshed": refreshed,
        "user_id": user_id,
        "media_type": requested_media_type,
    }


@shared_task(name="Refresh Discover Tab Cache")
def refresh_discover_tab_cache(
    user_id: int,
    media_type: str,
    show_more: bool = False,
    force: bool = False,
    clear_provider_cache: bool = False,
):
    """Refresh the Redis-backed Discover tab cache for a user/media type."""
    from app.discover.tab_cache import refresh_tab_cache
    from app.discover import tab_cache as discover_tab_cache

    user_model = get_user_model()
    user = user_model.objects.filter(id=user_id).first()
    if not user:
        logger.warning("discover_tab_refresh_user_missing user_id=%s", user_id)
        return {"refreshed": False, "reason": "missing_user"}

    requested_media_type = (media_type or discover_tab_cache.ALL_MEDIA_KEY).strip().lower()
    if (
        requested_media_type != discover_tab_cache.ALL_MEDIA_KEY
        and not discover_tab_cache.media_type_is_enabled_for_user(user, requested_media_type)
    ):
        logger.info(
            "discover_tab_refresh_skipped user_id=%s media_type=%s reason=disabled_media_type",
            user_id,
            requested_media_type,
        )
        return {"refreshed": False, "reason": "disabled_media_type", "user_id": user_id}

    rows = refresh_tab_cache(
        user,
        requested_media_type,
        show_more=show_more,
        force=force,
        clear_provider_cache=clear_provider_cache,
    )
    return {
        "refreshed": True,
        "row_count": len(rows),
        "user_id": user_id,
        "media_type": requested_media_type,
        "show_more": bool(show_more),
        "force": bool(force),
        "clear_provider_cache": bool(clear_provider_cache),
    }


@shared_task(name="Warm Discover Startup Tabs")
def warm_discover_startup_tabs(user_ids: list[int] | None = None):
    """Warm the default Discover tab cache for users after app startup."""
    from app.discover.registry import ALL_MEDIA_KEY
    from app.discover.tab_cache import schedule_user_tab_warmup

    user_model = get_user_model()
    users = user_model.objects.filter(is_active=True)
    if user_ids:
        users = users.filter(id__in=user_ids)

    scheduled = 0
    users_count = 0
    for user in users.iterator(chunk_size=200):
        users_count += 1
        scheduled += schedule_user_tab_warmup(
            user,
            media_types=[ALL_MEDIA_KEY],
            prioritize_media_type=ALL_MEDIA_KEY,
            show_more=False,
        )

    return {
        "scheduled": scheduled,
        "users_count": users_count,
    }


@shared_task(name="Warm History Day Cache Coverage")
def warm_history_day_cache_coverage(
    user_ids: list[int] | None = None,
    logging_styles: list[str] | None = None,
):
    """Queue chunked history day coverage repair for active users."""
    user_model = get_user_model()
    users = user_model.objects.filter(is_active=True)
    if user_ids:
        users = users.filter(id__in=user_ids)

    styles = logging_styles or ["sessions", "repeats"]
    scheduled = 0
    users_count = 0
    for user in users.iterator(chunk_size=200):
        users_count += 1
        for logging_style in styles:
            scheduled += int(
                history_cache.schedule_history_day_cache_coverage(
                    user.id,
                    logging_style=logging_style,
                    countdown=0,
                    batch_size=history_cache.HISTORY_COVERAGE_REPAIR_BATCH_SIZE,
                    priority=BACKGROUND_TASK_PRIORITY,
                ),
            )

    return {
        "scheduled": scheduled,
        "users_count": users_count,
        "logging_styles": styles,
    }


@shared_task(name="Refresh Discover Profiles")
def refresh_discover_profiles(user_ids: list[int] | None = None, media_types: list[str] | None = None):
    """Refresh Discover taste profiles for users and media types."""
    from app.discover.profile import get_or_compute_taste_profile
    from app.discover.registry import ALL_MEDIA_KEY

    user_model = get_user_model()
    users = user_model.objects.all().order_by("id")
    if user_ids:
        users = users.filter(id__in=user_ids)

    target_media_types = media_types or [ALL_MEDIA_KEY]
    refreshed = 0
    for user in users.iterator(chunk_size=200):
        for media_type in target_media_types:
            get_or_compute_taste_profile(user, media_type, force=True)
            refreshed += 1

    return {
        "profiles_refreshed": refreshed,
        "users_count": len(user_ids) if user_ids else users.count(),
        "media_types": target_media_types,
    }


@shared_task(name="Warm Discover API Cache")
def warm_discover_api_cache():
    """Warm provider-backed Discover API cache for core TMDb and Trakt rows."""
    if interactive_request_active():
        logger.info("warm_discover_api_cache skipped reason=interactive_request_active")
        return {
            "skipped": True,
            "reason": "interactive_request_active",
            "warmed": 0,
            "failed": 0,
        }

    from app.discover.providers.trakt_adapter import TraktDiscoverAdapter
    from app.discover.providers.tmdb_adapter import TMDbDiscoverAdapter

    adapter = TMDbDiscoverAdapter()
    trakt_adapter = TraktDiscoverAdapter()
    warmed = 0
    failed = 0
    for media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
        for fetcher in (
            adapter.trending,
            adapter.current_cycle,
            adapter.upcoming,
            adapter.top_rated,
        ):
            try:
                fetcher(media_type, limit=20)
                warmed += 1
            except Exception as error:  # noqa: BLE001
                failed += 1
                logger.warning(
                    "discover_api_warm_failed media_type=%s fetcher=%s error=%s",
                    media_type,
                    getattr(fetcher, "__name__", "unknown"),
                    error,
                )

    try:
        trakt_adapter.movie_watched_weekly(limit=25)
        warmed += 1
    except Exception as error:  # noqa: BLE001
        failed += 1
        logger.warning(
            "discover_api_warm_failed media_type=%s fetcher=%s error=%s",
            MediaTypes.MOVIE.value,
            "movie_watched_weekly",
            error,
        )

    try:
        trakt_adapter.movie_popular(page=1, limit=25)
        warmed += 1
    except Exception as error:  # noqa: BLE001
        failed += 1
        logger.warning(
            "discover_api_warm_failed media_type=%s fetcher=%s error=%s",
            MediaTypes.MOVIE.value,
            "movie_popular",
            error,
        )

    try:
        trakt_adapter.movie_anticipated(page=1, limit=25)
        warmed += 1
    except Exception as error:  # noqa: BLE001
        failed += 1
        logger.warning(
            "discover_api_warm_failed media_type=%s fetcher=%s error=%s",
            MediaTypes.MOVIE.value,
            "movie_anticipated",
            error,
        )

    return {"warmed": warmed, "failed": failed}
