import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

import events
from app import history_cache
from app.log_safety import exception_summary
from app.mixins import disable_fetch_releases
from integrations.imports import (
    anilist,
    audiobookshelf,
    gpodder,
    goodreads,
    hardcover,
    helpers,
    hltb,
    imdb,
    kitsu,
    mal,
    plex,
    pocketcasts,
    radarr,
    simkl,
    sonarr,
    steam,
    storyteller,
    trakt,
    yamtrack,
)
from integrations.plex_watchlist import PlexWatchlistSyncService
from integrations.tasks._import_helpers import (
    GOODREADS_IMPORT_TASK_NAME,
    LEGACY_GOODREADS_IMPORT_TASK_NAMES,
    _coerce_uploaded_file,
    format_import_message,
    format_watchlist_sync_message,
)
from integrations.tasks._plex_collection import update_collection_metadata_from_plex

logger = logging.getLogger(__name__)


def import_media(importer_func, identifier, user_id, mode, oauth_username=None):
    """Handle the import process for different media services."""
    user = get_user_model().objects.get(id=user_id)

    with disable_fetch_releases():
        if oauth_username is None:
            imported_counts, warnings = importer_func(
                identifier,
                user,
                mode,
            )
        else:
            imported_counts, warnings = importer_func(
                identifier,
                user,
                mode,
                username=oauth_username,
            )

    events.tasks.reload_calendar.delay()

    # Importers rely heavily on bulk_create_with_history, which bypasses model signals.
    # Force-clear history cache so month view index pages don't keep stale "empty month"
    # payloads after imports (notably reproducible with SIMKL imports).
    history_cache.invalidate_history_cache(user.id, force=True)

    # bulk_create also bypasses the post_save signals that normally schedule a statistics
    # cache refresh. Trigger it explicitly so the hours card and activity overview reflect
    # the newly imported media without requiring a manual page reload or waiting for the
    # next scheduled Celery beat.
    from app import statistics_cache as _statistics_cache
    _statistics_cache.schedule_all_ranges_refresh(user.id)

    # Queue collection metadata update task for media server imports
    _queue_post_import_collection_update(user_id, importer_func)

    return format_import_message(imported_counts, warnings)


def _run_arr_import(service_name, importer_func, user_id, mode):
    """Run ARR imports without surfacing expected connection failures as task tracebacks."""
    try:
        return import_media(importer_func, None, user_id, mode)
    except helpers.MediaImportError as exc:
        logger.warning("%s import failed for user %s: %s", service_name, user_id, exc)
        return f"{service_name} import failed: {exc}"


def _queue_post_import_collection_update(user_id, importer_func):
    """Queue collection metadata update task after import if applicable.

    Args:
        user_id: User ID
        importer_func: The importer function that was called
    """
    # Check if this is a media server import that supports collection updates
    # Compare by function reference
    import integrations.imports.plex as plex_import_module
    if importer_func == plex_import_module.importer:
        # Queue Plex collection update (run after calendar reload with a delay)
        update_collection_metadata_from_plex.apply_async(
            args=("all", user_id),
            countdown=60,  # Run 60 seconds after import to allow calendar reload to complete
        )
        logger.info("Queued post-import collection metadata update for user %s", user_id)
    # TODO: Add Jellyfin and Emby when their importers are available


@shared_task(name="Import from Trakt")
def import_trakt(user_id, mode, token=None, username=None):
    """Celery task for importing media data from Trakt.

    Can import using either OAuth (token provided) or public username.
    """
    return import_media(trakt.importer, token, user_id, mode, username)


@shared_task(name="Import from SIMKL")
def import_simkl(token, user_id, mode, username=None):  # noqa: ARG001
    """Celery task for importing media data from SIMKL."""
    return import_media(simkl.importer, token, user_id, mode)


@shared_task(name="Import from MyAnimeList")
def import_mal(username, user_id, mode):
    """Celery task for importing anime and manga data from MyAnimeList."""
    return import_media(mal.importer, username, user_id, mode)


@shared_task(name="Import from AniList")
def import_anilist(user_id, mode, token=None, username=None):
    """Celery task for importing media data from AniList."""
    return import_media(anilist.importer, token, user_id, mode, username)


@shared_task(name="Import from Kitsu")
def import_kitsu(username, user_id, mode):
    """Celery task for importing anime and manga data from Kitsu."""
    return import_media(kitsu.importer, username, user_id, mode)


@shared_task(name="Import from Yamtrack")
def import_yamtrack(file, user_id, mode):
    """Celery task for importing media data from Yamtrack."""
    return import_media(yamtrack.importer, _coerce_uploaded_file(file), user_id, mode)


@shared_task(name="Import from HowLongToBeat")
def import_hltb(file, user_id, mode):
    """Celery task for importing media data from HowLongToBeat."""
    return import_media(hltb.importer, _coerce_uploaded_file(file), user_id, mode)


@shared_task(name="Import from Steam")
def import_steam(username, user_id, mode):
    """Celery task for importing game data from Steam."""
    return import_media(steam.importer, username, user_id, mode)


@shared_task(name="Import from IMDB")
def import_imdb(file, user_id, mode):
    """Celery task for importing media data from IMDB."""
    return import_media(imdb.importer, _coerce_uploaded_file(file), user_id, mode)


def _run_goodreads_import(file, user_id, mode):
    """Execute the Goodreads CSV import for any registered task alias."""
    return import_media(goodreads.importer, _coerce_uploaded_file(file), user_id, mode)


@shared_task(name=GOODREADS_IMPORT_TASK_NAME)
def import_goodreads(file, user_id, mode):
    """Celery task for importing media data from Goodreads."""
    return _run_goodreads_import(file, user_id, mode)


@shared_task(name=LEGACY_GOODREADS_IMPORT_TASK_NAMES[0])
def import_goodreads_legacy(file, user_id, mode):
    """Compatibility alias for the legacy Goodreads task name."""
    return _run_goodreads_import(file, user_id, mode)


@shared_task(name=LEGACY_GOODREADS_IMPORT_TASK_NAMES[1])
def import_goodreads_dotted(file, user_id, mode):
    """Compatibility alias for dotted Goodreads task references."""
    return _run_goodreads_import(file, user_id, mode)


@shared_task(name="Import from Hardcover")
def import_hardcover(file, user_id, mode):
    """Celery task for importing media data from Hardcover."""
    return import_media(hardcover.importer, _coerce_uploaded_file(file), user_id, mode)


@shared_task(name="Import from Plex")
def import_plex(library, user_id, mode, username=None):  # noqa: ARG001
    """Celery task for importing media data from Plex."""
    return import_media(plex.importer, library, user_id, mode)


@shared_task(name="Import from Radarr")
def import_radarr(user_id, mode="new", username=None):  # noqa: ARG001
    """Celery task for importing movie collection data from Radarr."""
    return _run_arr_import("Radarr", radarr.importer, user_id, mode)


@shared_task(name="Import from Radarr (Recurring)")
def import_radarr_recurring(user_id):
    """Recurring import task for Radarr."""
    return _run_arr_import("Radarr", radarr.importer, user_id, "new")


@shared_task(name="Import from Sonarr")
def import_sonarr(user_id, mode="new", username=None):  # noqa: ARG001
    """Celery task for importing TV collection data from Sonarr."""
    return _run_arr_import("Sonarr", sonarr.importer, user_id, mode)


@shared_task(name="Import from Sonarr (Recurring)")
def import_sonarr_recurring(user_id):
    """Recurring import task for Sonarr."""
    return _run_arr_import("Sonarr", sonarr.importer, user_id, "new")


@shared_task(name="Sync Plex Watchlist")
def sync_plex_watchlist(user_id, mode="watchlist"):  # noqa: ARG001
    """Celery task for syncing Plex Discover watchlist items."""
    from integrations.models import PlexAccount

    user = get_user_model().objects.get(id=user_id)
    account = getattr(user, "plex_account", None)
    if not account:
        raise helpers.MediaImportError("Connect Plex before syncing the watchlist.")

    try:
        sync_counts, warnings = PlexWatchlistSyncService(user, account).sync()
    except helpers.MediaImportError as exc:
        PlexAccount.objects.filter(user=user).update(
            watchlist_last_error=str(exc),
            watchlist_last_error_at=timezone.now(),
        )
        raise
    except Exception as exc:  # pragma: no cover - defensive
        PlexAccount.objects.filter(user=user).update(
            watchlist_last_error=str(exc),
            watchlist_last_error_at=timezone.now(),
        )
        raise

    PlexAccount.objects.filter(user=user).update(
        watchlist_last_synced_at=timezone.now(),
        watchlist_last_error="",
        watchlist_last_error_at=None,
    )

    if sync_counts.get("created") or sync_counts.get("removed"):
        events.tasks.reload_calendar.delay()

    return format_watchlist_sync_message(sync_counts, warnings)




@shared_task(name="Import from Audiobookshelf")
def import_audiobookshelf(user_id, mode="new"):
    """Celery task for importing audiobook progress from Audiobookshelf."""
    return import_media(audiobookshelf.importer, None, user_id, mode)


@shared_task(name="Import from Audiobookshelf (Recurring)")
def import_audiobookshelf_recurring(user_id):
    """Recurring import task for Audiobookshelf."""
    return import_media(audiobookshelf.importer, None, user_id, "new")


@shared_task(name="Import from Storyteller")
def import_storyteller(user_id, mode="new"):
    """Celery task for importing book reading progress from Storyteller."""
    return import_media(storyteller.importer, None, user_id, mode)


@shared_task(name="Import from Storyteller (Recurring)")
def import_storyteller_recurring(user_id):
    """Recurring import task for Storyteller."""
    return import_media(storyteller.importer, None, user_id, "new")


@shared_task(name="Import from Pocket Casts")
def import_pocketcasts(user_id, mode="new"):
    """Celery task for importing podcast history from Pocket Casts."""
    lock_key = f"pocketcasts_import_lock_{user_id}"
    if not cache.add(lock_key, "1", timeout=600):
        logger.info("Pocket Casts import already running for user %s, skipping", user_id)
        return "Skipped: import already in progress"
    try:
        return import_media(pocketcasts.importer, None, user_id, mode)
    finally:
        cache.delete(lock_key)


@shared_task(name="Import from Pocket Casts (Recurring)")
def import_pocketcasts_history(user_id):
    """Recurring import task for Pocket Casts (called every 2 hours via Celery beat)."""
    lock_key = f"pocketcasts_import_lock_{user_id}"
    if not cache.add(lock_key, "1", timeout=600):
        logger.info("Pocket Casts import already running for user %s, skipping", user_id)
        return "Skipped: import already in progress"
    try:
        return import_media(pocketcasts.importer, None, user_id, "new")
    finally:
        cache.delete(lock_key)


@shared_task(name="Import from GPodder")
def import_gpodder(user_id, mode="new"):
    """Celery task for importing podcast history from GPodder-compatible servers."""
    lock_key = f"gpodder_import_lock_{user_id}"
    if not cache.add(lock_key, "1", timeout=600):
        logger.info("GPodder import already running for user %s, skipping", user_id)
        return "Skipped: import already in progress"
    try:
        return import_media(gpodder.importer, None, user_id, mode)
    finally:
        cache.delete(lock_key)


@shared_task(name="Import from GPodder (Recurring)")
def import_gpodder_recurring(user_id):
    """Recurring import task for GPodder-compatible servers."""
    lock_key = f"gpodder_import_lock_{user_id}"
    if not cache.add(lock_key, "1", timeout=600):
        logger.info("GPodder import already running for user %s, skipping", user_id)
        return "Skipped: import already in progress"
    try:
        return import_media(gpodder.importer, None, user_id, "new")
    finally:
        cache.delete(lock_key)

