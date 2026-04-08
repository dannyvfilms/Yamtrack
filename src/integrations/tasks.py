import logging
import time
from io import BytesIO

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

import events
from app.collection_helpers import (
    extract_collection_metadata_from_plex,
)
from app.helpers import is_item_collected
from app.log_safety import exception_summary, safe_url
from app.mixins import disable_fetch_releases
from app.models import CollectionEntry, Item, MediaTypes
from app.templatetags import app_tags
from integrations import plex as plex_api
from integrations.imports import (
    anilist,
    audiobookshelf,
    goodreads,
    hardcover,
    helpers,
    hltb,
    imdb,
    kitsu,
    mal,
    plex,
    pocketcasts,
    simkl,
    steam,
    trakt,
    yamtrack,
)
from integrations.plex_watchlist import PlexWatchlistSyncService

logger = logging.getLogger(__name__)
ERROR_TITLE = "\n\n\n Couldn't import the following media: \n\n"


def _is_expected_plex_lookup_error(exc):
    """Return True for expected Plex library lookup failures that don't need tracebacks."""
    if isinstance(exc, plex_api.PlexClientError):
        return True

    summary = exception_summary(exc).lower()
    if "timeout" in summary or "timed out" in summary:
        return True

    exc_type = type(exc).__name__.lower()
    return "timeout" in exc_type


def _coerce_uploaded_file(file):
    """Normalize uploaded file task args to a binary file-like object."""
    if hasattr(file, "read"):
        try:
            file.seek(0)
        except (AttributeError, OSError):
            pass
        return file
    if isinstance(file, str):
        return BytesIO(file.encode("utf-8"))
    if isinstance(file, bytes):
        return BytesIO(file)
    msg = f"Unsupported uploaded file payload type: {type(file)!r}"
    raise TypeError(msg)


def format_media_type_display(count, media_type):
    """Format media type display with proper pluralization."""
    if count == 0:
        return None
    if count == 1:
        return f"{count} {dict(MediaTypes.choices).get(media_type, media_type)}"
    return f"{count} {app_tags.media_type_readable_plural(media_type)}"


def format_import_message(imported_counts, warning_messages=None):
    """Format the import result message based on counts and warnings."""
    parts = []

    # Handle music specially - show both play events and unique tracks
    music_play_events = imported_counts.get(MediaTypes.MUSIC.value, 0)
    music_unique_tracks = imported_counts.get("music_unique_tracks", 0)

    if music_play_events > 0:
        if music_unique_tracks > 0:
            # Show both play events and unique tracks
            parts.append(
                f"{music_play_events} music play event{'s' if music_play_events != 1 else ''} "
                f"({music_unique_tracks} unique track{'s' if music_unique_tracks != 1 else ''})",
            )
        else:
            # Fallback to standard format if unique tracks not available
            parts.append(format_media_type_display(music_play_events, MediaTypes.MUSIC.value))

    # Add other media types (excluding music which we handled above)
    media_type_values = set(MediaTypes.values)
    for media_type, count in imported_counts.items():
        if (
            media_type == MediaTypes.MUSIC.value
            or media_type == "music_unique_tracks"
            or media_type not in media_type_values
        ):
            continue
        formatted = format_media_type_display(count, media_type)
        if formatted:
            parts.append(formatted)

    parts = [p for p in parts if p is not None]

    if not parts:
        info_message = "No media was imported."
    else:
        info_message = f"Imported {helpers.join_with_commas_and(parts)}."

    metric_parts = []
    metric_mappings = [
        ("created", "created"),
        ("updated", "updated"),
        ("skipped_missing_ids", "skipped (missing IDs)"),
        ("skipped_existing", "skipped (existing)"),
        ("skipped_unknown_type", "skipped (unknown type)"),
        ("skipped_other_user", "skipped (other users)"),
    ]
    for key, label in metric_mappings:
        value = imported_counts.get(key)
        if value:
            metric_parts.append(f"{value} {label}")

    if metric_parts:
        info_message = f"{info_message} {helpers.join_with_commas_and(metric_parts)}."

    if warning_messages:
        return f"{info_message} {ERROR_TITLE} {warning_messages}"
    return info_message


def format_watchlist_sync_message(sync_counts, warning_messages=None):
    """Format the Plex watchlist sync result message."""
    created_parts = []
    movie_count = sync_counts.get(MediaTypes.MOVIE.value, 0)
    tv_count = sync_counts.get(MediaTypes.TV.value, 0)

    if movie_count:
        created_parts.append(
            f"{movie_count} movie{'s' if movie_count != 1 else ''}",
        )
    if tv_count:
        created_parts.append(
            f"{tv_count} TV show{'s' if tv_count != 1 else ''}",
        )

    if created_parts:
        info_message = (
            "Synced Plex watchlist. "
            f"Imported {helpers.join_with_commas_and(created_parts)}."
        )
    else:
        info_message = "Synced Plex watchlist. No new watchlist media was imported."

    metric_parts = []
    metric_mappings = [
        ("created", "created"),
        ("linked_existing", "linked to existing media"),
        ("removed", "removed from Planning"),
        ("deactivated", "deactivated"),
        ("skipped_missing_ids", "skipped (missing IDs)"),
        ("skipped_unknown_type", "skipped (unknown type)"),
        ("skipped_metadata", "skipped (metadata errors)"),
    ]
    for key, label in metric_mappings:
        value = sync_counts.get(key)
        if value:
            metric_parts.append(f"{value} {label}")

    if metric_parts:
        info_message = f"{info_message} {helpers.join_with_commas_and(metric_parts)}."

    if warning_messages:
        return f"{info_message} {ERROR_TITLE} {warning_messages}"
    return info_message


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

    # Queue collection metadata update task for media server imports
    _queue_post_import_collection_update(user_id, importer_func)

    return format_import_message(imported_counts, warnings)


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


@shared_task(name="Import from GoodReads")
def import_goodreads(file, user_id, mode):
    """Celery task for importing media data from GoodReads."""
    return import_media(goodreads.importer, _coerce_uploaded_file(file), user_id, mode)


@shared_task(name="Import from Hardcover")
def import_hardcover(file, user_id, mode):
    """Celery task for importing media data from Hardcover."""
    return import_media(hardcover.importer, _coerce_uploaded_file(file), user_id, mode)


@shared_task(name="Import from Plex")
def import_plex(library, user_id, mode, username=None):  # noqa: ARG001
    """Celery task for importing media data from Plex."""
    return import_media(plex.importer, library, user_id, mode)


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
    return import_audiobookshelf.delay(user_id=user_id, mode="new")

@shared_task(name="Import from Pocket Casts")
def import_pocketcasts(user_id, mode="new"):
    """Celery task for importing podcast history from Pocket Casts."""
    return import_media(pocketcasts.importer, None, user_id, mode)


@shared_task(name="Import from Pocket Casts (Recurring)")
def import_pocketcasts_history(user_id):
    """Recurring import task for Pocket Casts (called every 2 hours via Celery beat)."""
    return import_pocketcasts.delay(user_id, mode="new")


LASTFM_PARTIAL_SYNC_ERROR = (
    "Last.fm sync ended early during pagination. Imported partial results and will retry "
    "without advancing the cursor."
)


def _refresh_lastfm_statistics(user_id: int, affected_day_keys) -> None:
    """Refresh statistics caches for a Last.fm import."""
    if not affected_day_keys:
        return

    from app import statistics_cache

    statistics_cache.invalidate_statistics_days(
        user_id,
        day_values=list(affected_day_keys),
        reason="lastfm_batch_import",
    )
    statistics_cache.schedule_all_ranges_refresh(user_id)


def _enqueue_lastfm_music_enrichment(user_id: int) -> None:
    """Kick off deferred music enrichment after a history backfill."""
    from app.tasks import enrich_albums_task, enrich_music_library_task

    try:
        enrich_music_library_task.delay(user_id)
        enrich_albums_task.delay(user_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Could not enqueue Last.fm music enrichment for user %s: %s",
            user_id,
            exception_summary(exc),
        )


def _run_incremental_lastfm_sync(account) -> dict:
    """Poll new Last.fm scrobbles for a single account."""
    from integrations import lastfm_api, lastfm_sync

    if not getattr(account.user, "music_enabled", False):
        logger.debug(
            "Skipping Last.fm poll for user %s: music disabled",
            account.user.username,
        )
        return {
            "status": "skipped",
            "processed": 0,
            "skipped": 0,
            "errors": 0,
            "message": "Music tracking is disabled.",
        }

    from_timestamp_uts = None
    if account.last_fetch_timestamp_uts:
        from_timestamp_uts = account.last_fetch_timestamp_uts - 60

    try:
        sync_result = lastfm_sync.sync_lastfm_account(
            account,
            from_timestamp_uts=from_timestamp_uts,
            fast_mode=False,
        )
    except lastfm_api.LastFMRateLimitError as exc:
        logger.warning(
            "Rate limit exceeded for user %s, will retry next cycle: %s",
            account.user.username,
            exc,
        )
        now = timezone.now()
        account.connection_broken = False
        account.failure_count += 1
        account.last_error_code = "29"
        account.last_error_message = str(exc)[:500]
        account.last_failed_at = now
        account.save(
            update_fields=[
                "connection_broken",
                "failure_count",
                "last_error_code",
                "last_error_message",
                "last_failed_at",
            ],
        )
        return {
            "status": "error",
            "processed": 0,
            "skipped": 0,
            "errors": 1,
            "message": "Last.fm rate limit exceeded.",
        }
    except lastfm_api.LastFMClientError as exc:
        logger.error("Last.fm client error for user %s: %s", account.user.username, exc)
        now = timezone.now()
        account.connection_broken = True
        account.failure_count += 1
        account.last_error_code = "6"
        account.last_error_message = str(exc)[:500]
        account.last_failed_at = now
        account.save(
            update_fields=[
                "connection_broken",
                "failure_count",
                "last_error_code",
                "last_error_message",
                "last_failed_at",
            ],
        )
        return {
            "status": "error",
            "processed": 0,
            "skipped": 0,
            "errors": 1,
            "message": "Last.fm account is no longer valid.",
        }
    except lastfm_api.LastFMAPIError as exc:
        logger.error("Last.fm API error for user %s: %s", account.user.username, exc)
        now = timezone.now()
        account.connection_broken = False
        account.failure_count += 1
        account.last_error_code = "unknown"
        account.last_error_message = str(exc)[:500]
        account.last_failed_at = now
        account.save(
            update_fields=[
                "connection_broken",
                "failure_count",
                "last_error_code",
                "last_error_message",
                "last_failed_at",
            ],
        )
        return {
            "status": "error",
            "processed": 0,
            "skipped": 0,
            "errors": 1,
            "message": "Last.fm API request failed.",
        }
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Unexpected error polling Last.fm for user %s: %s",
            account.user.username,
            exc,
            exc_info=True,
        )
        now = timezone.now()
        account.connection_broken = False
        account.failure_count += 1
        account.last_error_code = "exception"
        account.last_error_message = str(exc)[:500]
        account.last_failed_at = now
        account.save(
            update_fields=[
                "connection_broken",
                "failure_count",
                "last_error_code",
                "last_error_message",
                "last_failed_at",
            ],
        )
        return {
            "status": "error",
            "processed": 0,
            "skipped": 0,
            "errors": 1,
            "message": "Unexpected Last.fm sync error.",
        }

    _refresh_lastfm_statistics(account.user.id, sync_result["affected_day_keys"])

    now = timezone.now()
    update_fields = [
        "last_sync_at",
        "connection_broken",
        "failure_count",
        "last_error_code",
        "last_error_message",
        "last_failed_at",
    ]
    account.last_sync_at = now
    account.connection_broken = False

    if sync_result["complete"]:
        latest_timestamp_uts = account.last_fetch_timestamp_uts or 0
        if sync_result["max_seen_uts"] is not None:
            latest_timestamp_uts = max(latest_timestamp_uts, sync_result["max_seen_uts"])
        account.last_fetch_timestamp_uts = latest_timestamp_uts
        account.failure_count = 0
        account.last_error_code = ""
        account.last_error_message = ""
        account.last_failed_at = None
        update_fields.append("last_fetch_timestamp_uts")
        status = "success"
        message = (
            f"Processed {sync_result['processed']} new Last.fm scrobble(s)."
            if sync_result["processed"]
            else "No new Last.fm scrobbles found."
        )
    else:
        account.failure_count += 1
        account.last_error_code = "partial"
        account.last_error_message = LASTFM_PARTIAL_SYNC_ERROR
        account.last_failed_at = now
        status = "partial"
        message = LASTFM_PARTIAL_SYNC_ERROR

    account.save(update_fields=update_fields)
    return {
        "status": status,
        "processed": sync_result["processed"],
        "skipped": sync_result["skipped"],
        "errors": sync_result["errors"],
        "message": message,
    }

@shared_task(name="Poll Last.fm for user")
def poll_lastfm_for_user(user_id):
    """Poll new Last.fm scrobbles for a single user."""
    from integrations.models import LastFMAccount

    account = LastFMAccount.objects.filter(user_id=user_id).select_related("user").first()
    if not account or not account.is_connected:
        logger.debug("Skipping per-user Last.fm poll for user %s: no connected account", user_id)
        return {"processed": 0, "errors": 0, "message": "No connected Last.fm account."}

    result = _run_incremental_lastfm_sync(account)
    return {
        "processed": 1 if result["status"] in {"success", "partial"} else 0,
        "errors": 1 if result["status"] in {"partial", "error"} else 0,
        "message": result["message"],
    }


@shared_task(name="Import from Last.fm History")
def import_lastfm_history(user_id, reset=False):
    """Import a user's historical Last.fm scrobbles in bounded chunks."""
    from integrations import lastfm_api, lastfm_sync
    from integrations.models import LastFMAccount, LastFMHistoryImportStatus

    account = LastFMAccount.objects.filter(user_id=user_id).select_related("user").first()
    if not account or not account.is_connected:
        logger.debug(
            "Skipping Last.fm history import for user %s: no connected account",
            user_id,
        )
        return {"message": "No connected Last.fm account."}

    if reset:
        cutoff_uts = (account.last_fetch_timestamp_uts or int(time.time())) - 1
        account.reset_history_import(cutoff_uts)
        account.save(
            update_fields=[
                "history_import_status",
                "history_import_cutoff_uts",
                "history_import_next_page",
                "history_import_total_pages",
                "history_import_started_at",
                "history_import_completed_at",
                "history_import_last_error_message",
            ],
        )

    if not getattr(account.user, "music_enabled", False):
        account.history_import_status = LastFMHistoryImportStatus.FAILED
        account.history_import_last_error_message = "Enable music tracking before importing Last.fm history."
        account.save(update_fields=["history_import_status", "history_import_last_error_message"])
        raise ValueError("Enable music tracking before importing Last.fm history.")

    if account.history_import_cutoff_uts is None:
        cutoff_uts = (account.last_fetch_timestamp_uts or int(time.time())) - 1
        account.reset_history_import(cutoff_uts)
        account.save(
            update_fields=[
                "history_import_status",
                "history_import_cutoff_uts",
                "history_import_next_page",
                "history_import_total_pages",
                "history_import_started_at",
                "history_import_completed_at",
                "history_import_last_error_message",
            ],
        )

    lock_key = lastfm_sync.get_lastfm_history_import_lock_key(user_id)
    if not cache.add(lock_key, {"started_at": timezone.now().isoformat()}, timeout=600):
        logger.debug("Last.fm history import already running for user %s", user_id)
        return {"message": "Full Last.fm history import already running."}

    try:
        now = timezone.now()
        account.refresh_from_db()
        if not account.is_connected:
            return {"message": "No connected Last.fm account."}

        account.history_import_status = LastFMHistoryImportStatus.RUNNING
        if not account.history_import_started_at:
            account.history_import_started_at = now
        account.history_import_last_error_message = ""
        account.save(
            update_fields=[
                "history_import_status",
                "history_import_started_at",
                "history_import_last_error_message",
            ],
        )

        try:
            sync_result = lastfm_sync.sync_lastfm_account(
                account,
                to_timestamp_uts=account.history_import_cutoff_uts,
                page_start=account.history_import_next_page,
                max_pages=getattr(settings, "LASTFM_HISTORY_PAGES_PER_TASK", 5),
                fast_mode=True,
            )
        except lastfm_api.LastFMClientError as exc:
            account.refresh_from_db()
            account.connection_broken = True
            account.failure_count += 1
            account.last_error_code = "6"
            account.last_error_message = str(exc)[:500]
            account.last_failed_at = timezone.now()
            account.history_import_status = LastFMHistoryImportStatus.FAILED
            account.history_import_last_error_message = str(exc)[:500]
            account.save(
                update_fields=[
                    "connection_broken",
                    "failure_count",
                    "last_error_code",
                    "last_error_message",
                    "last_failed_at",
                    "history_import_status",
                    "history_import_last_error_message",
                ],
            )
            raise
        except lastfm_api.LastFMAPIError as exc:
            account.refresh_from_db()
            account.history_import_status = LastFMHistoryImportStatus.FAILED
            account.history_import_last_error_message = str(exc)[:500]
            account.save(update_fields=["history_import_status", "history_import_last_error_message"])
            raise

        _refresh_lastfm_statistics(user_id, sync_result["affected_day_keys"])

        account.refresh_from_db()
        account.history_import_total_pages = sync_result["total_pages"]
        account.history_import_next_page = account.history_import_next_page + sync_result["pages_fetched"]

        if sync_result["interrupted"]:
            account.history_import_status = LastFMHistoryImportStatus.FAILED
            account.history_import_last_error_message = LASTFM_PARTIAL_SYNC_ERROR
            account.save(
                update_fields=[
                    "history_import_status",
                    "history_import_total_pages",
                    "history_import_next_page",
                    "history_import_last_error_message",
                ],
            )
            raise ValueError(LASTFM_PARTIAL_SYNC_ERROR)

        if sync_result["complete"]:
            account.history_import_status = LastFMHistoryImportStatus.COMPLETED
            account.history_import_completed_at = timezone.now()
            account.history_import_last_error_message = ""
            account.save(
                update_fields=[
                    "history_import_status",
                    "history_import_total_pages",
                    "history_import_next_page",
                    "history_import_completed_at",
                    "history_import_last_error_message",
                ],
            )
            _enqueue_lastfm_music_enrichment(user_id)
            return {
                "message": "Completed Last.fm history import. Music enrichment queued.",
            }

        account.history_import_status = LastFMHistoryImportStatus.QUEUED
        account.save(
            update_fields=[
                "history_import_status",
                "history_import_total_pages",
                "history_import_next_page",
            ],
        )
        import_lastfm_history.delay(user_id=user_id, reset=False)
        current_page = min(account.history_import_next_page, sync_result["total_pages"])
        return {
            "message": (
                f"Imported {sync_result['processed']} history scrobble(s). "
                f"Continuing with page {current_page} of {sync_result['total_pages']}."
            ),
        }
    finally:
        cache.delete(lock_key)


@shared_task(name="Poll Last.fm for all users")
def poll_all_lastfm_scrobbles():
    """Global task to poll Last.fm for all connected users."""
    from integrations.models import LastFMAccount

    accounts = LastFMAccount.objects.filter(connection_broken=False).select_related("user")
    if not accounts.exists():
        logger.debug("No Last.fm accounts to poll")
        return {"processed": 0, "errors": 0, "message": "No accounts to poll"}

    logger.info("Polling Last.fm for %d users", accounts.count())

    batch_size = 10
    import random

    processed_count = 0
    error_count = 0
    partial_count = 0

    accounts_list = list(accounts)
    random.shuffle(accounts_list)

    for index, account in enumerate(accounts_list):
        if index > 0 and index % batch_size == 0:
            time.sleep(random.uniform(0.5, 2.0))

        result = _run_incremental_lastfm_sync(account)
        if result["status"] == "success":
            processed_count += 1
        elif result["status"] == "partial":
            processed_count += 1
            partial_count += 1
            error_count += 1
        elif result["status"] == "error":
            error_count += 1

    logger.info(
        "Last.fm polling completed: %d users processed, %d partial, %d errors",
        processed_count,
        partial_count,
        error_count,
    )
    return {
        "processed": processed_count,
        "errors": error_count,
        "total_accounts": accounts.count(),
        "message": f"Processed {processed_count} Last.fm account(s).",
    }


@shared_task(name="Update collection metadata from Plex webhook")
def update_collection_metadata_from_plex_webhook(
    user_id,
    item_id,
    rating_key,
    plex_uri,
    plex_token,
):
    """Update collection metadata from Plex webhook event.

    Args:
        user_id: User ID
        item_id: Item ID in Yamtrack
        rating_key: Plex rating key
        plex_uri: Plex server URI
        plex_token: Plex authentication token
    """
    from integrations import plex as plex_api

    logger.info(
        "Starting collection metadata update task (user_id=%s item_id=%s uri=%s)",
        user_id,
        item_id,
        safe_url(plex_uri),
    )

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        item = Item.objects.get(id=item_id)
    except (User.DoesNotExist, Item.DoesNotExist) as exc:
        logger.warning(
            "Cannot update collection metadata: %s (user_id=%s, item_id=%s)",
            exc,
            user_id,
            item_id,
        )
        return None

    logger.debug("Found user=%s, item=%s (media_type=%s)", user.username, item.title, item.media_type)

    # Fetch detailed metadata from Plex
    try:
        logger.debug(
            "Fetching Plex metadata for collection update via %s",
            safe_url(plex_uri),
        )
        plex_metadata = plex_api.fetch_metadata(plex_token, plex_uri, rating_key)
    except Exception as exc:
        # Check if this is a timeout (expected network issue)
        is_timeout = (
            "timeout" in str(exc).lower() or
            "ReadTimeout" in str(type(exc).__name__) or
            "TimeoutError" in str(type(exc).__name__)
        )
        
        if is_timeout:
            # Timeouts are expected - log at debug level
            logger.debug(
                "Timeout fetching Plex metadata via %s: %s",
                safe_url(plex_uri),
                exception_summary(exc),
            )
        else:
            # Other errors are more serious - log as warning
            logger.warning(
                "Failed to fetch Plex metadata for collection update via %s: %s. "
                "This may indicate the URI is incorrect or the server is unreachable.",
                safe_url(plex_uri),
                exception_summary(exc),
                exc_info=True,
            )
        
        # If HTTP failed, try HTTPS (some servers require HTTPS)
        if plex_uri.startswith("http://") and "500" in str(exc):
            https_uri = plex_uri.replace("http://", "https://")
            logger.debug("Retrying collection update with HTTPS: %s", safe_url(https_uri))
            try:
                plex_metadata = plex_api.fetch_metadata(plex_token, https_uri, rating_key)
                logger.info("Successfully fetched metadata using HTTPS URI")
            except Exception as https_exc:
                logger.debug(
                    "HTTPS retry for collection update also failed: %s",
                    exception_summary(https_exc),
                )
                return None
        else:
            return None

    if not plex_metadata:
        logger.debug("No Plex metadata returned for collection update")
        return None

    logger.debug("Received Plex metadata with keys: %s", list(plex_metadata.keys()))

    # Extract collection metadata
    collection_metadata = extract_collection_metadata_from_plex(plex_metadata)
    logger.debug(
        "Extracted collection metadata: %s",
        {k: v for k, v in collection_metadata.items() if v},
    )

    # Update the most recent entry for this item, or create a new one if none exists.
    entry = (
        CollectionEntry.objects.filter(
            user=user,
            item=item,
        )
        .order_by("-updated_at", "-collected_at", "-id")
        .first()
    )
    created = entry is None
    if created:
        entry = CollectionEntry(
            user=user,
            item=item,
            **collection_metadata,
        )

    # Store rating key and URI for future bulk imports (cache for faster lookups)
    rating_key_updated = False
    if entry.plex_rating_key != rating_key or entry.plex_uri != plex_uri:
        entry.plex_rating_key = rating_key
        entry.plex_uri = plex_uri
        entry.plex_rating_key_updated_at = timezone.now()
        rating_key_updated = True

    if not created:
        # Update existing entry
        updated_fields = []
        for key, value in collection_metadata.items():
            if value:  # Only update non-empty values
                old_value = getattr(entry, key, None)
                if old_value != value:
                    setattr(entry, key, value)
                    updated_fields.append(f"{key}={old_value}->{value}")
        
        # Save if we have updates (collection metadata or rating key)
        if updated_fields or rating_key_updated:
            entry.save()
            if updated_fields:
                logger.debug("Updated collection entry fields: %s", ", ".join(updated_fields))
            if rating_key_updated:
                logger.debug("Updated cached Plex collection lookup details")
        else:
            logger.debug("No changes to collection entry")
    else:
        # New entry - save initial metadata and rating key cache.
        entry.save()
        if rating_key_updated:
            logger.debug("Stored cached Plex collection lookup details for new entry")

    logger.info(
        "Collection metadata update completed for %s - %s (created=%s, entry_id=%s)",
        user.username,
        item.title,
        created,
        entry.id,
    )

    # For TV shows, also create episode-level collection entries
    if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        logger.info("TV show detected, creating episode-level collection entries for %s", item.title)
        try:
            # Use the aggregated metadata function to get episode-level data
            aggregated_metadata, episode_list = _aggregate_tv_show_collection_metadata(
                plex_token,
                plex_uri,
                rating_key,
                show_metadata=plex_metadata,
                fetch_episode_details=True,  # Always fetch episode details for webhooks
            )
            
            logger.info("Found %d episodes with collection metadata for %s", len(episode_list), item.title)
            
            episode_entries_created = 0
            episode_entries_updated = 0
            episode_entries_skipped = 0
            
            for episode_data in episode_list:
                season_number = episode_data["season_number"]
                episode_number = episode_data["episode_number"]
                episode_collection_metadata = episode_data["collection_metadata"]
                
                # Skip Season 0 (Specials) to match Details pane behavior
                if season_number == 0:
                    episode_entries_skipped += 1
                    continue
                
                # Find or create the episode Item
                try:
                    episode_item, episode_item_created = Item.objects.get_or_create(
                        media_id=item.media_id,
                        source=item.source,
                        media_type=MediaTypes.EPISODE.value,
                        season_number=season_number,
                        episode_number=episode_number,
                        defaults={
                            "title": f"Episode {episode_number}",
                            "image": item.image,
                        },
                    )
                    
                    # Update the most recent episode entry, or create one if needed.
                    episode_entry = (
                        CollectionEntry.objects.filter(
                            user=user,
                            item=episode_item,
                        )
                        .order_by("-updated_at", "-collected_at", "-id")
                        .first()
                    )
                    episode_entry_created = episode_entry is None
                    if episode_entry_created:
                        episode_entry = CollectionEntry(
                            user=user,
                            item=episode_item,
                            **episode_collection_metadata,
                        )
                        episode_entry.save()
                    
                    # Store rating key and URI for episode (if we can get it from episode metadata)
                    # Note: We'd need to fetch individual episode metadata to get episode rating keys
                    # For now, we'll just store the collection metadata
                    
                    if episode_entry_created:
                        episode_entries_created += 1
                        logger.debug("Created collection entry for episode S%02dE%02d of %s", season_number, episode_number, item.title)
                    else:
                        # Update existing entry
                        updated = False
                        for key, value in episode_collection_metadata.items():
                            if value:  # Only update non-empty values
                                old_value = getattr(episode_entry, key, None)
                                if old_value != value:
                                    setattr(episode_entry, key, value)
                                    updated = True
                        if updated:
                            episode_entry.save()
                            episode_entries_updated += 1
                            logger.debug("Updated collection entry for episode S%02dE%02d of %s", season_number, episode_number, item.title)
                            
                except Exception as exc:
                    logger.warning(
                        "Failed to create collection entry for episode S%02dE%02d of %s: %s",
                        season_number,
                        episode_number,
                        item.title,
                        exc,
                        exc_info=True,
                    )
                    continue
            
            logger.info(
                "Episode collection entries for %s: %d created, %d updated, %d skipped (Season 0)",
                item.title,
                episode_entries_created,
                episode_entries_updated,
                episode_entries_skipped,
            )
        except Exception as exc:
            logger.warning(
                "Failed to create episode-level collection entries for TV show %s: %s",
                item.title,
                exc,
                exc_info=True,
            )

    return entry.id


@shared_task(name="Fetch collection metadata for item")
def fetch_collection_metadata_for_item(user_id, item_id):
    """Fetch collection metadata for a single item in the background.
    
    This is triggered when viewing a media details page for an item that doesn't
    have collection data yet. It attempts to find the item in Plex and create
    collection entries.
    
    Args:
        user_id: User ID
        item_id: Item ID in Yamtrack
    """
    from django.contrib.auth import get_user_model
    from app.models import Item, MediaTypes, CollectionEntry
    from integrations import plex as plex_api
    from app.collection_helpers import extract_collection_metadata_from_plex
    from django.utils import timezone
    
    logger.info("Starting collection metadata fetch for user_id=%s, item_id=%s", user_id, item_id)
    
    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        item = Item.objects.get(id=item_id)
    except (User.DoesNotExist, Item.DoesNotExist) as exc:
        logger.warning("Cannot fetch collection metadata: %s (user_id=%s, item_id=%s)", exc, user_id, item_id)
        return None
    
    # Check if user has Plex connected
    plex_account = getattr(user, "plex_account", None)
    if not plex_account or not plex_account.plex_token:
        logger.info("User %s does not have Plex connected, skipping collection fetch", user.username)
        return None
    
    # Check if collection entry already exists
    existing_entry = CollectionEntry.objects.filter(user=user, item=item).first()
    if existing_entry:
        logger.info("Collection entry already exists for %s - %s (entry_id=%s)", user.username, item.title, existing_entry.id)
        return existing_entry.id
    
    # Step 1: Check for cached rating keys (fast path)
    rating_key = None
    plex_uri = None
    
    # Check show-level cached rating key first
    show_cached_entry = CollectionEntry.objects.filter(
        user=user,
        item=item,
        plex_rating_key__isnull=False,
    ).first()
    
    if show_cached_entry and show_cached_entry.plex_rating_key and show_cached_entry.plex_uri:
        rating_key = show_cached_entry.plex_rating_key
        plex_uri = show_cached_entry.plex_uri
        logger.info("Using cached show-level Plex lookup for %s - %s", user.username, item.title)
    elif item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        # Check for cached rating key in any episode's collection entry
        from app.models import Item as ItemModel
        episode_items = ItemModel.objects.filter(
            media_id=item.media_id,
            source=item.source,
            media_type=MediaTypes.EPISODE.value,
        )
        episode_item_ids = list(episode_items.values_list('id', flat=True))
        if episode_item_ids:
            cached_episode_entry = CollectionEntry.objects.filter(
                user=user,
                item_id__in=episode_item_ids,
                plex_rating_key__isnull=False,
                plex_uri__isnull=False,
            ).first()
            
            if cached_episode_entry:
                # We have a cached rating key for an episode
                # Fetch episode metadata to get the show's rating key
                episode_rating_key = cached_episode_entry.plex_rating_key
                episode_plex_uri = cached_episode_entry.plex_uri
                
                logger.info("Found cached episode-level Plex lookup, deriving show-level key")
                try:
                    episode_metadata = plex_api.fetch_metadata(
                        plex_account.plex_token,
                        episode_plex_uri,
                        str(episode_rating_key),
                    )
                    
                    if episode_metadata:
                        # Get show rating key from episode's parentKey or librarySectionKey
                        # For episodes, parentKey points to the season, and we need to go up to the show
                        parent_key = episode_metadata.get("parentKey") or episode_metadata.get("grandparentKey")
                        if parent_key:
                            # parentKey might be a season, grandparentKey is the show
                            show_key = episode_metadata.get("grandparentKey")
                            if show_key:
                                # Extract rating key from the key path
                                # grandparentKey is usually like "/library/metadata/12345"
                                if "/" in show_key:
                                    show_rating_key_str = show_key.split("/")[-1]
                                    try:
                                        rating_key = str(int(show_rating_key_str))
                                        plex_uri = episode_plex_uri
                                        logger.info("Derived show-level Plex lookup from cached episode metadata")
                                    except (ValueError, TypeError):
                                        pass
                except Exception as exc:
                    logger.debug(
                        "Failed to derive show-level Plex lookup from episode metadata: %s",
                        exception_summary(exc),
                    )
    
    # If we found a cached rating key, use it directly
    if rating_key and plex_uri:
        logger.info("Using cached Plex lookup for %s - %s", user.username, item.title)
        return update_collection_metadata_from_plex_webhook(
            user_id=user_id,
            item_id=item_id,
            rating_key=str(rating_key),
            plex_uri=plex_uri,
            plex_token=plex_account.plex_token,
        )
    
    # Step 2: If no cached rating key, search Plex library
    logger.info("No cached rating key found for %s - %s, searching Plex library", user.username, item.title)
    
    try:
        resources = plex_api.list_resources(plex_account.plex_token)
        sections = plex_account.sections or []
        if not sections:
            sections = plex_api.list_sections(plex_account.plex_token)
        
        # Get all available Plex URIs to try as fallbacks
        available_uris = []
        if sections:
            # Add section URIs
            for section in sections:
                if section.get("uri") and section.get("uri") not in available_uris:
                    available_uris.append(section.get("uri"))
            
            # Add connection URIs from resources
            for resource in resources:
                machine_id = resource.get("machine_identifier")
                if machine_id:
                    for section in sections:
                        if section.get("machine_identifier") == machine_id:
                            for conn in resource.get("connections", []):
                                uri = conn.get("uri") if isinstance(conn, dict) else conn
                                if uri and uri not in available_uris:
                                    available_uris.append(uri)
                            break
        
        if not available_uris:
            logger.warning("No Plex URIs available for user %s", user.username)
            return None
        
        # Use first URI as primary, others as fallbacks
        primary_uri = available_uris[0]
        logger.info(
            "Using primary Plex URI %s (have %d total URIs available)",
            safe_url(primary_uri),
            len(available_uris),
        )
        
        # Search for the item in Plex sections
        for section in sections:
            section_type = (section.get("type") or "").lower()
            if section_type not in ("movie", "show"):
                continue
            
            if item.media_type == MediaTypes.MOVIE.value and section_type != "movie":
                continue
            if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and section_type != "show":
                continue
            
            # Get section key
            section_key = section.get("key") or section.get("id")
            if isinstance(section_key, str) and section_key.startswith("/library/sections/"):
                section_key = section_key.split("/")[-1]
            
            logger.info("Searching section '%s' for %s - %s", section.get("title"), user.username, item.title)
            
            from integrations.plex import extract_external_ids_from_guids
            
            # Try each available URI until one works
            section_uri = None
            library_items = None
            total = 0
            
            for uri_to_try in available_uris:
                try:
                    # Get total items in section
                    library_items, total = plex_api.fetch_section_all_items(
                        plex_account.plex_token,
                        uri_to_try,
                        str(section_key),
                        start=0,
                        size=1,  # Just get the total count
                    )
                    # Success - use this URI for the rest of this section
                    section_uri = uri_to_try
                    break
                except Exception as uri_exc:
                    logger.debug(
                        "Failed to connect to Plex URI %s: %s",
                        safe_url(uri_to_try),
                        exception_summary(uri_exc),
                    )
                    if uri_to_try == available_uris[-1]:
                        # Last URI failed, log and continue to next section
                        logger.warning(
                            "All Plex URIs failed for section '%s': %s",
                            section.get("title"),
                            exception_summary(uri_exc),
                        )
                        raise
            
            if not section_uri:
                continue
            
            try:
                logger.info(
                    "Section '%s' has %d total items (using URI: %s)",
                    section.get("title"),
                    total,
                    safe_url(section_uri),
                )
                
                found_match = False
                rating_key = None
                item_title_lower = item.title.lower().strip()
                max_pages_to_check = 0  # Initialize for logging
                
                # Strategy: Use title-based matching with smart pagination
                # Since Plex libraries are typically sorted alphabetically, we can use a smarter approach
                # 1. Search first 100 items (fast path for common shows)
                # 2. If not found, use title-based matching to find likely positions
                # 3. Search around those positions
                
                # Fast path: Check first 100 items
                if total > 0:
                    library_items, _ = plex_api.fetch_section_all_items(
                        plex_account.plex_token,
                        section_uri,
                        str(section_key),
                        start=0,
                        size=min(100, total),
                    )
                    
                    logger.info("Checking first %d items in section '%s'", len(library_items), section.get("title"))
                    
                    for entry in library_items:
                        # Extract external IDs
                        guids = entry.get("Guid", [])
                        if not guids:
                            single_guid = entry.get("guid")
                            if single_guid:
                                guids = [{"id": single_guid}]
                        
                        external_ids = extract_external_ids_from_guids(guids)
                        
                        # Check if this matches our item by ID
                        matches = False
                        if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                            matches = True
                        elif item.source == "imdb" and external_ids.get("imdb_id") == item.media_id:
                            matches = True
                        elif item.source == "tvdb" and external_ids.get("tvdb_id") == str(item.media_id):
                            matches = True
                        
                        if matches:
                            rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                            found_match = True
                            logger.info("Found match in first 100 items by external ID")
                            break
                
                # If not found and library is large, use title-based search with smart pagination
                if not found_match and total > 100:
                    logger.info("Item not found in first 100 items, using title-based search (total: %d items)", total)
                    
                    # Strategy: Search in chunks, prioritizing title matches
                    # Since libraries are often sorted alphabetically, we can search more efficiently
                    # by checking items that might match by title first
                    
                    page_size = 100
                    max_pages_to_check = min(50, (total + page_size - 1) // page_size)  # Check up to 50 pages (5000 items)
                    
                    # Search through pages, prioritizing title matches
                    for page in range(1, max_pages_to_check + 1):
                        start = (page - 1) * page_size
                        
                        try:
                            page_items, _ = plex_api.fetch_section_all_items(
                                plex_account.plex_token,
                                section_uri,
                                str(section_key),
                                start=start,
                                size=page_size,
                            )
                            
                            if not page_items:
                                break
                            
                            # First pass: Check external IDs (most reliable)
                            for entry in page_items:
                                guids = entry.get("Guid", [])
                                if not guids:
                                    single_guid = entry.get("guid")
                                    if single_guid:
                                        guids = [{"id": single_guid}]
                                
                                external_ids = extract_external_ids_from_guids(guids)
                                
                                # Check if this matches our item by ID
                                matches = False
                                if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                                    matches = True
                                elif item.source == "imdb" and external_ids.get("imdb_id") == item.media_id:
                                    matches = True
                                elif item.source == "tvdb" and external_ids.get("tvdb_id") == str(item.media_id):
                                    matches = True
                                
                                if matches:
                                    rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                                    found_match = True
                                    logger.info("Found match at position %d-%d by external ID", start, start + len(page_items))
                                    break
                            
                            if found_match:
                                break
                            
                            # Second pass: Check title matches (if no external ID match)
                            # Only do this if we haven't found a match yet
                            for entry in page_items:
                                entry_title = entry.get("title", "").lower().strip()
                                
                                # Check for title match (exact or close)
                                title_matches = (
                                    entry_title == item_title_lower or
                                    item_title_lower in entry_title or
                                    entry_title in item_title_lower
                                )
                                
                                if title_matches:
                                    # Title matches - verify with external IDs if available
                                    guids = entry.get("Guid", [])
                                    if not guids:
                                        single_guid = entry.get("guid")
                                        if single_guid:
                                            guids = [{"id": single_guid}]
                                    
                                    external_ids = extract_external_ids_from_guids(guids)
                                    
                                    # If we have external IDs, verify they match
                                    if external_ids:
                                        if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                                            rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                                            found_match = True
                                            logger.info("Found match at position %d-%d by title + external ID", start, start + len(page_items))
                                            break
                                        # If external IDs don't match, skip (might be a different show with similar title)
                                    else:
                                        # No external IDs but title matches - use as fallback
                                        # This is less reliable but better than nothing
                                        rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                                        found_match = True
                                        logger.info("Found match at position %d-%d by title only (no external IDs)", start, start + len(page_items))
                                        break
                            
                            if found_match:
                                break
                            
                            # Log progress every 10 pages
                            if page % 10 == 0:
                                logger.info("Searched %d/%d pages (%d items) in section '%s'", page, max_pages_to_check, start + len(page_items), section.get("title"))
                                
                        except Exception as page_exc:
                            logger.debug("Error searching page %d (start=%d): %s", page, start, page_exc)
                            continue
                
                if found_match and rating_key and section_uri:
                    logger.info(
                        "Found matching Plex item for %s - %s in section '%s'",
                        user.username,
                        item.title,
                        section.get("title"),
                    )
                    # Trigger webhook-style update
                    result = update_collection_metadata_from_plex_webhook(
                        user_id=user_id,
                        item_id=item_id,
                        rating_key=str(rating_key),
                        plex_uri=section_uri,
                        plex_token=plex_account.plex_token,
                    )
                    logger.info("Webhook task completed for %s - %s, returning entry_id=%s", 
                               user.username, item.title, result)
                    return result
                else:
                    searched_count = min(max_pages_to_check * 100, total) if max_pages_to_check > 0 and total > 100 else min(100, total)
                    logger.info("Could not find matching Plex item for %s - %s in section '%s' (searched %d/%d items)", 
                               user.username, item.title, section.get("title"), searched_count, total)
            except Exception as exc:
                if _is_expected_plex_lookup_error(exc):
                    logger.warning(
                        "Error searching section '%s' for item %s: %s",
                        section.get("title"),
                        item.title,
                        exception_summary(exc),
                    )
                else:
                    logger.warning(
                        "Error searching section '%s' for item %s: %s",
                        section.get("title"),
                        item.title,
                        exc,
                        exc_info=True,
                    )
                logger.info("Continuing to search other sections...")
                continue
    except Exception as exc:
        logger.warning("Failed to fetch collection metadata for %s - %s: %s", user.username, item.title, exception_summary(exc), exc_info=True)
        return None
    
    logger.info("Could not find matching Plex item for %s - %s in any section", user.username, item.title)
    return None


def _find_plex_rating_key_for_item(
    user,
    item,
    plex_account,
    sections,
    resources,
    available_uris=None,
):
    """Find Plex rating key for a Yamtrack item.
    
    Checks cached rating keys first, then searches Plex library if needed.
    
    Args:
        user: User object
        item: Item object to find rating key for
        plex_account: PlexAccount object
        sections: List of Plex sections
        resources: List of Plex resources
        available_uris: Optional list of Plex URIs to try (if None, will be determined)
        
    Returns:
        Tuple of (rating_key, plex_uri, match_type) or None if not found.
        match_type can be: "cached", "tmdb", "imdb", "tvdb", or None
    """
    from integrations import plex as plex_api
    from integrations.plex import extract_external_ids_from_guids
    
    # Step 1: Check for cached rating keys (fast path)
    cached_entry = CollectionEntry.objects.filter(
        user=user,
        item=item,
        plex_rating_key__isnull=False,
        plex_uri__isnull=False,
    ).first()
    
    if cached_entry and cached_entry.plex_rating_key and cached_entry.plex_uri:
        logger.debug("Using cached Plex lookup for %s - %s", user.username, item.title)
        return (cached_entry.plex_rating_key, cached_entry.plex_uri, "cached")
    
    # For TV shows, also check episode-level cached entries
    if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        from app.models import Item as ItemModel
        episode_items = ItemModel.objects.filter(
            media_id=item.media_id,
            source=item.source,
            media_type=MediaTypes.EPISODE.value,
        )
        episode_item_ids = list(episode_items.values_list('id', flat=True))
        if episode_item_ids:
            cached_episode_entry = CollectionEntry.objects.filter(
                user=user,
                item_id__in=episode_item_ids,
                plex_rating_key__isnull=False,
                plex_uri__isnull=False,
            ).first()
            
            if cached_episode_entry:
                # Derive show rating key from episode
                episode_rating_key = cached_episode_entry.plex_rating_key
                episode_plex_uri = cached_episode_entry.plex_uri
                
                try:
                    episode_metadata = plex_api.fetch_metadata(
                        plex_account.plex_token,
                        episode_plex_uri,
                        str(episode_rating_key),
                    )
                    
                    if episode_metadata:
                        show_key = episode_metadata.get("grandparentKey")
                        if show_key and "/" in show_key:
                            show_rating_key_str = show_key.split("/")[-1]
                            try:
                                rating_key = str(int(show_rating_key_str))
                                logger.debug("Derived show-level Plex lookup from episode metadata")
                                return (rating_key, episode_plex_uri, "cached")
                            except (ValueError, TypeError):
                                pass
                except Exception as exc:
                    logger.debug(
                        "Failed to derive show-level Plex lookup from episode metadata: %s",
                        exception_summary(exc),
                    )
    
    # Step 2: If no cached rating key, search Plex library
    if available_uris is None:
        available_uris = []
        if sections:
            for section in sections:
                if section.get("uri") and section.get("uri") not in available_uris:
                    available_uris.append(section.get("uri"))
            
            for resource in resources:
                machine_id = resource.get("machine_identifier")
                if machine_id:
                    for section in sections:
                        if section.get("machine_identifier") == machine_id:
                            for conn in resource.get("connections", []):
                                uri = conn.get("uri") if isinstance(conn, dict) else conn
                                if uri and uri not in available_uris:
                                    available_uris.append(uri)
                            break
        
        if not available_uris:
            logger.debug("No Plex URIs available for user %s", user.username)
            return None
    
    # Search for the item in Plex sections
    for section in sections:
        section_type = (section.get("type") or "").lower()
        if section_type not in ("movie", "show"):
            continue
        
        if item.media_type == MediaTypes.MOVIE.value and section_type != "movie":
            continue
        if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and section_type != "show":
            continue
        
        # Get section key
        section_key = section.get("key") or section.get("id")
        if isinstance(section_key, str) and section_key.startswith("/library/sections/"):
            section_key = section_key.split("/")[-1]
        
        # Try each available URI until one works
        section_uri = None
        total = 0
        
        for uri_to_try in available_uris:
            try:
                library_items, total = plex_api.fetch_section_all_items(
                    plex_account.plex_token,
                    uri_to_try,
                    str(section_key),
                    start=0,
                    size=1,  # Just get the total count
                )
                section_uri = uri_to_try
                break
            except Exception as uri_exc:
                logger.debug(
                    "Failed to connect to Plex URI %s: %s",
                    safe_url(uri_to_try),
                    exception_summary(uri_exc),
                )
                if uri_to_try == available_uris[-1]:
                    continue
        
        if not section_uri:
            continue
        
        try:
            # Fast path: Check first 100 items
            if total > 0:
                library_items, _ = plex_api.fetch_section_all_items(
                    plex_account.plex_token,
                    section_uri,
                    str(section_key),
                    start=0,
                    size=min(100, total),
                )
                
                for entry in library_items:
                    guids = entry.get("Guid", [])
                    if not guids:
                        single_guid = entry.get("guid")
                        if single_guid:
                            guids = [{"id": single_guid}]
                    
                    external_ids = extract_external_ids_from_guids(guids)
                    
                    matches = False
                    match_type = None
                    if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                        matches = True
                        match_type = "tmdb"
                    elif item.source == "imdb" and external_ids.get("imdb_id") == item.media_id:
                        matches = True
                        match_type = "imdb"
                    elif item.source == "tvdb" and external_ids.get("tvdb_id") == str(item.media_id):
                        matches = True
                        match_type = "tvdb"
                    
                    if matches:
                        rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                        if rating_key:
                            logger.debug("Found match in first 100 items by %s", match_type)
                            return (rating_key, section_uri, match_type)
            
            # If not found and library is large, search more pages
            if total > 100:
                page_size = 100
                max_pages_to_check = min(50, (total + page_size - 1) // page_size)
                
                for page in range(1, max_pages_to_check + 1):
                    start = (page - 1) * page_size
                    
                    try:
                        page_items, _ = plex_api.fetch_section_all_items(
                            plex_account.plex_token,
                            section_uri,
                            str(section_key),
                            start=start,
                            size=page_size,
                        )
                        
                        if not page_items:
                            break
                        
                        # Check external IDs
                        for entry in page_items:
                            guids = entry.get("Guid", [])
                            if not guids:
                                single_guid = entry.get("guid")
                                if single_guid:
                                    guids = [{"id": single_guid}]
                            
                            external_ids = extract_external_ids_from_guids(guids)
                            
                            matches = False
                            match_type = None
                            if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                                matches = True
                                match_type = "tmdb"
                            elif item.source == "imdb" and external_ids.get("imdb_id") == item.media_id:
                                matches = True
                                match_type = "imdb"
                            elif item.source == "tvdb" and external_ids.get("tvdb_id") == str(item.media_id):
                                matches = True
                                match_type = "tvdb"
                            
                            if matches:
                                rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                                if rating_key:
                                    logger.debug("Found match at position %d-%d by %s", start, start + len(page_items), match_type)
                                    return (rating_key, section_uri, match_type)
                    except Exception as page_exc:
                        logger.debug("Error searching page %d: %s", page, page_exc)
                        continue
        except Exception as exc:
            logger.debug("Error searching section '%s' for item %s: %s", section.get("title"), item.title, exc)
            continue
    
    return None


def _aggregate_tv_show_collection_metadata(
    token: str, 
    uri: str, 
    show_rating_key: str, 
    show_metadata: dict | None = None,
    fetch_episode_details: bool = True
) -> tuple[dict, list]:
    """Aggregate collection metadata from all episodes of a TV show.
    
    Similar to how we aggregate music track metadata at the album level,
    this function fetches episodes and aggregates their collection metadata
    at the show level.
    
    Args:
        token: Plex authentication token
        uri: Plex server URI
        show_rating_key: Plex rating key for the TV show
        show_metadata: Optional already-fetched show metadata to avoid duplicate API call
        fetch_episode_details: If False, skip fetching detailed metadata for each episode
                              (only fetch episode lists for episode entry creation)
        
    Returns:
        Tuple of (aggregated_metadata_dict, episode_list) where:
        - aggregated_metadata_dict: Dictionary with aggregated collection metadata (most common values)
        - episode_list: List of dicts with keys: season_number, episode_number, collection_metadata
    """
    from integrations import plex as plex_api
    from app.collection_helpers import extract_collection_metadata_from_plex
    import requests
    from django.conf import settings
    
    result = {
        "resolution": "",
        "hdr": "",
        "audio_codec": "",
        "audio_channels": "",
        "bitrate": None,
        "media_type": "",
    }
    
    # Use provided show_metadata or fetch it
    if show_metadata is None:
        show_metadata = plex_api.fetch_metadata(token, uri, show_rating_key)
    
    if not show_metadata:
        return result, []
    
    show_key = show_metadata.get("key")
    if not show_key:
        logger.debug("No key found in Plex show metadata")
        return result, []
    
    # The show key may already include /children, so check before appending
    if not show_key.endswith("/children"):
        seasons_key = f"{show_key}/children"
    else:
        seasons_key = show_key
    
    # Fetch seasons using the seasons key
    try:
        response = requests.get(
            f"{uri}{seasons_key}",
            headers=plex_api._headers(token),
            params={"X-Plex-Token": token},
            timeout=20,
            verify=settings.PLEX_SSL_VERIFY,
        )
        if not response.ok:
            logger.debug(
                "Failed to fetch Plex show seasons (status=%s)",
                response.status_code,
            )
            return result, []
        
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            payload = response.json()
            container = payload.get("MediaContainer") or {}
            # Seasons are typically in Metadata array (type="season")
            # Directory may contain aggregate entries like "All episodes"
            # Prefer Metadata, but fall back to Directory if needed
            metadata_seasons = [s for s in (container.get("Metadata") or []) if s.get("type") == "season"]
            if metadata_seasons:
                seasons = metadata_seasons
            else:
                # Fall back to Directory, but filter out aggregate entries
                seasons = [s for s in (container.get("Directory") or []) if "allLeaves" not in s.get("key", "")]
        else:
            # XML parsing would go here, but for now just return empty
            logger.debug("XML response not yet supported for season children")
            return result, []
    except Exception as exc:
        logger.debug(
            "Error fetching Plex show seasons: %s",
            exception_summary(exc),
        )
        return result, []
    
    if not seasons:
        logger.debug("No seasons found in Plex show metadata")
        return result, []
    
    # Collect metadata from all episodes across all seasons
    # Store both aggregated data and individual episode data
    all_episode_metadata = []  # For aggregation
    episode_list = []  # For individual episode entries
    
    for season in seasons:
        season_key = season.get("key")
        if not season_key:
            continue
        
        # Skip "All episodes" or similar aggregate entries
        if "allLeaves" in season_key:
            continue
        
        # Get season number from season metadata
        # For seasons, the index is the season number
        season_number = season.get("index")
        if season_number is None:
            logger.debug("No season number found for season key %s", season_key)
            continue
        
        # Season key may already include /children, so check before appending
        if not season_key.endswith("/children"):
            episodes_key = f"{season_key}/children"
        else:
            episodes_key = season_key
        
        # Fetch episodes using the episodes key
        try:
            season_response = requests.get(
                f"{uri}{episodes_key}",
                headers=plex_api._headers(token),
                params={"X-Plex-Token": token},
                timeout=20,
                verify=settings.PLEX_SSL_VERIFY,
            )
            if not season_response.ok:
                continue
            
            season_content_type = season_response.headers.get("Content-Type", "")
            if "json" in season_content_type:
                season_payload = season_response.json()
                season_container = season_payload.get("MediaContainer") or {}
                episodes = season_container.get("Metadata") or []
            else:
                continue
            
            # Extract collection metadata from each episode
            for episode in episodes:
                episode_rating_key = episode.get("ratingKey")
                if not episode_rating_key:
                    continue
                
                # Get episode number from episode metadata
                # For episodes, the index is the episode number
                episode_number = episode.get("index")
                if episode_number is None:
                    logger.debug("No episode number found in Plex episode metadata")
                    continue
                
                episode_collection = {}
                
                # Check if episode list response includes Media array with collection metadata
                episode_media = episode.get("Media")
                if episode_media and isinstance(episode_media, list) and len(episode_media) > 0:
                    # Try to extract collection metadata from episode list response
                    # Create a temporary metadata dict with Media array for extraction
                    temp_episode_metadata = {"Media": episode_media}
                    episode_collection = extract_collection_metadata_from_plex(temp_episode_metadata)
                
                # Only fetch detailed episode metadata if:
                # 1. fetch_episode_details is True AND
                # 2. We don't have collection metadata from the list response
                if fetch_episode_details and not any(episode_collection.values()):
                    try:
                        episode_metadata = plex_api.fetch_metadata(token, uri, str(episode_rating_key))
                        if episode_metadata:
                            episode_collection = extract_collection_metadata_from_plex(episode_metadata)
                    except Exception as exc:
                        logger.debug(
                            "Failed to fetch Plex episode metadata: %s",
                            exception_summary(exc),
                        )
                        continue
                
                # Add to lists if we have collection metadata
                if any(episode_collection.values()):
                    # Add to aggregation list
                    all_episode_metadata.append(episode_collection)
                    # Add to episode list with season/episode numbers
                    episode_list.append({
                        "season_number": int(season_number),
                        "episode_number": int(episode_number),
                        "collection_metadata": episode_collection,
                    })
        except Exception as exc:
            logger.debug("Error fetching episodes for season %s: %s", season_key, exception_summary(exc))
            continue
    
    if not all_episode_metadata:
        logger.debug("No Plex episode metadata found for aggregation")
        return result, []
    
    # Aggregate metadata - find most common values (like music album aggregation)
    resolutions = {}
    hdrs = {}
    audio_codecs = {}
    audio_channels_list = {}
    bitrates = {}
    media_types = {}
    
    for ep_meta in all_episode_metadata:
        if ep_meta.get("resolution"):
            resolutions[ep_meta["resolution"]] = resolutions.get(ep_meta["resolution"], 0) + 1
        if ep_meta.get("hdr"):
            hdrs[ep_meta["hdr"]] = hdrs.get(ep_meta["hdr"], 0) + 1
        if ep_meta.get("audio_codec"):
            audio_codecs[ep_meta["audio_codec"]] = audio_codecs.get(ep_meta["audio_codec"], 0) + 1
        if ep_meta.get("audio_channels"):
            audio_channels_list[ep_meta["audio_channels"]] = audio_channels_list.get(ep_meta["audio_channels"], 0) + 1
        if ep_meta.get("bitrate"):
            bitrates[ep_meta["bitrate"]] = bitrates.get(ep_meta["bitrate"], 0) + 1
        if ep_meta.get("media_type"):
            media_types[ep_meta["media_type"]] = media_types.get(ep_meta["media_type"], 0) + 1
    
    # Get most common value (or first if tie)
    result["resolution"] = max(resolutions.items(), key=lambda x: x[1])[0] if resolutions else ""
    result["hdr"] = max(hdrs.items(), key=lambda x: x[1])[0] if hdrs else ""
    result["audio_codec"] = max(audio_codecs.items(), key=lambda x: x[1])[0] if audio_codecs else ""
    result["audio_channels"] = max(audio_channels_list.items(), key=lambda x: x[1])[0] if audio_channels_list else ""
    result["bitrate"] = max(bitrates.items(), key=lambda x: x[1])[0] if bitrates else None
    result["media_type"] = max(media_types.items(), key=lambda x: x[1])[0] if media_types else ""
    
    logger.debug(
        "Aggregated collection metadata from %d episodes for Plex show: %s",
        len(all_episode_metadata),
        {k: v for k, v in result.items() if v},
    )
    
    return result, episode_list


@shared_task(name="Update collection metadata from Plex")
def update_collection_metadata_from_plex(library, user_id):
    """Update collection metadata for existing Yamtrack items from Plex server.

    This task queries the Plex server for items that match existing Yamtrack items
    and updates their collection metadata without performing a full import.

    Args:
        library: Plex library identifier (e.g., "all" or "machine_id::section_id")
        user_id: User ID
    """
    from integrations import plex as plex_api
    from app.collection_helpers import extract_collection_metadata_from_plex
    from app.helpers import is_item_collected
    from app.models import Item

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning("Cannot update collection metadata: user %s not found", user_id)
        return {"error": "User not found"}

    plex_account = getattr(user, "plex_account", None)
    if not plex_account or not plex_account.plex_token:
        logger.warning("Cannot update collection metadata: Plex not connected for user %s", user.username)
        return {"error": "Plex not connected"}

    try:
        resources = plex_api.list_resources(plex_account.plex_token)
    except plex_api.PlexAuthError as exc:
        logger.warning("Plex token expired for user %s: %s", user.username, exception_summary(exc))
        return {"error": "Plex token expired"}

    # Get target sections
    sections = plex_account.sections or []
    if not sections:
        sections = plex_api.list_sections(plex_account.plex_token)
        plex_account.sections = sections
        plex_account.sections_refreshed_at = timezone.now()
        plex_account.save(update_fields=["sections", "sections_refreshed_at"])

    if library != "all":
        try:
            machine_id, section_id = library.split("::", 1)
            sections = [
                s for s in sections
                if s.get("machine_identifier") == machine_id
                and str(s.get("id")) == str(section_id)
            ]
        except ValueError:
            logger.warning("Invalid Plex library selection: %s", library)
            return {"error": "Invalid library selection"}

    if not sections:
        logger.warning("No Plex sections found for user %s", user.username)
        return {"error": "No sections found"}

    updated_count = 0
    error_count = 0
    match_stats = {"tmdb": 0, "imdb": 0, "tvdb": 0, "unmatched": 0, "cached": 0}
    
    # Get counts before filtering
    from app.models import Movie, TV, Music, Anime
    user_movies_count = Movie.objects.filter(user=user).count()
    user_tv_count = TV.objects.filter(user=user).count()
    user_anime_count = Anime.objects.filter(user=user).count()
    user_music_count = Music.objects.filter(user=user).count()
    
    logger.info(
        "Starting collection metadata update for user %s: tracked items (Movies: %d, TV: %d, Anime: %d, Music: %d)",
        user.username,
        user_movies_count,
        user_tv_count,
        user_anime_count,
        user_music_count,
    )

    # Get user's tracked media items (Movies, TV, Anime, Music) that could have collection entries
    from app.models import Movie, TV, Music, Anime
    user_movies = Movie.objects.filter(user=user).select_related("item")
    user_tv = TV.objects.filter(user=user).select_related("item")
    user_anime = Anime.objects.filter(user=user).select_related("item")
    user_music = Music.objects.filter(user=user).select_related("item")

    all_user_items = list(user_movies.values_list("item_id", flat=True))
    all_user_items.extend(user_tv.values_list("item_id", flat=True))
    all_user_items.extend(user_anime.values_list("item_id", flat=True))
    all_user_items.extend(user_music.values_list("item_id", flat=True))

    if not all_user_items:
        logger.info("No tracked media found for user %s, nothing to update", user.username)
        return {"updated": 0, "errors": 0, "message": "No tracked media found"}

    user_items = Item.objects.filter(id__in=all_user_items).select_related()
    
    # Get available URIs once for reuse
    available_uris = []
    if sections:
        for section in sections:
            if section.get("uri") and section.get("uri") not in available_uris:
                available_uris.append(section.get("uri"))
        
        for resource in resources:
            machine_id = resource.get("machine_identifier")
            if machine_id:
                for section in sections:
                    if section.get("machine_identifier") == machine_id:
                        for conn in resource.get("connections", []):
                            uri = conn.get("uri") if isinstance(conn, dict) else conn
                            if uri and uri not in available_uris:
                                available_uris.append(uri)
                        break

    # Process each section incrementally: process cached items first, then scan in batches
    import time
    start_time = time.time()
    
    for section in sections:
        section_type = (section.get("type") or "").lower()
        # Only process movie and show sections (Anime maps to show sections in Plex)
        if section_type not in ("movie", "show"):
            continue

        # Get server URI
        connections = []
        if section.get("uri"):
            connections.append(section.get("uri"))
        # Add connections from resources
        for resource in resources:
            if resource.get("machine_identifier") == section.get("machine_identifier"):
                for conn in resource.get("connections", []):
                    uri = conn.get("uri") if isinstance(conn, dict) else conn
                    if uri and uri not in connections:
                        connections.append(uri)

        if not connections:
            logger.warning("No connections found for section %s", section.get("title"))
            continue

        plex_uri = connections[0]  # Use first available connection

        # Get section key for querying all items
        # Plex sections can have "key" (path like "/library/sections/1") or "id" (numeric)
        section_key = section.get("key") or section.get("id")
        if not section_key:
            logger.warning("Section %s has no key or id", section.get("title"))
            continue
        
        # If key is a path, extract just the numeric ID for the API call
        if isinstance(section_key, str) and section_key.startswith("/library/sections/"):
            section_key = section_key.split("/")[-1]

        try:
            # Get items for this section type
            section_items = [
                item for item in user_items
                if (item.media_type == MediaTypes.MOVIE.value and section_type == "movie") or
                   (item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and section_type == "show")
            ]
            
            if not section_items:
                continue
            
            logger.info(
                "Processing section '%s': %d items to check",
                section.get("title"),
                len(section_items),
            )
            
            # Step 1: Process cached items immediately (fast path)
            cached_processed = 0
            cached_updated = 0
            cached_errors = 0
            
            # Get cached entries for section items
            item_ids = [item.id for item in section_items]
            cached_entries = CollectionEntry.objects.filter(
                user=user,
                item_id__in=item_ids,
                plex_rating_key__isnull=False,
                plex_uri__isnull=False,
            ).select_related("item")
            
            cached_items_map = {entry.item_id: entry for entry in cached_entries}
            
            for item in section_items:
                cached_entry = cached_items_map.get(item.id)
                if cached_entry:
                    try:
                        # Use webhook function for consistency
                        entry_id = update_collection_metadata_from_plex_webhook(
                            user_id=user.id,
                            item_id=item.id,
                            rating_key=str(cached_entry.plex_rating_key),
                            plex_uri=cached_entry.plex_uri,
                            plex_token=plex_account.plex_token,
                        )
                        if entry_id:
                            cached_updated += 1
                            updated_count += 1
                            match_stats["cached"] = match_stats.get("cached", 0) + 1
                    except Exception as exc:
                        logger.warning(
                            "Failed to update cached item %s: %s",
                            item.title,
                            exc,
                        )
                        cached_errors += 1
                        error_count += 1
                    cached_processed += 1
            
            if cached_processed > 0:
                logger.info(
                    "Processed %d cached items in section '%s': %d updated, %d errors",
                    cached_processed,
                    section.get("title"),
                    cached_updated,
                    cached_errors,
                )
            
            # Step 2: Get items that need library scanning
            items_needing_scan = [
                item for item in section_items
                if not CollectionEntry.objects.filter(
                    user=user,
                    item=item,
                    plex_rating_key__isnull=False,
                ).exists()
            ]
            
            if not items_needing_scan:
                logger.info(
                    "All items in section '%s' processed (cached or already have entries)",
                    section.get("title"),
                )
                continue
            
            # Step 3: Scan library in batches and process matches incrementally
            logger.info(
                "Scanning library for %d uncached items in section '%s'",
                len(items_needing_scan),
                section.get("title"),
            )
            
            # Build set of items we're looking for (for early stopping)
            items_to_find = set((item.source, item.media_id) for item in items_needing_scan)
            items_found_set = set()
            
            # Build mapping of items by external ID for quick lookup
            items_by_external_id = {}
            for item in items_needing_scan:
                key = (item.source, item.media_id)
                items_by_external_id[key] = item
                # Also index by TMDB if source is tmdb
                if item.source == "tmdb":
                    items_by_external_id[("tmdb", item.media_id)] = item
            
            batch_size = 500
            start = 0
            total_items = None
            batch_processed = 0
            batch_matched = 0
            section_start_time = time.time()
            
            from integrations.plex import extract_external_ids_from_guids
            
            while True:
                # Early stopping: if we've found all items, stop scanning
                if len(items_found_set) >= len(items_to_find):
                    logger.info(
                        "Found all %d uncached items in section '%s', stopping scan",
                        len(items_to_find),
                        section.get("title"),
                    )
                    break
                
                # Fetch batch of library items
                try:
                    library_items, total = plex_api.fetch_section_all_items(
                        plex_account.plex_token,
                        plex_uri,
                        str(section_key),
                        start=start,
                        size=batch_size,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to fetch batch from section '%s' (start=%d): %s",
                        section.get("title"),
                        start,
                        exc,
                    )
                    break
                
                if total_items is None:
                    total_items = total
                    logger.info(
                        "Section '%s' has %d total items (scanning for %d uncached items)",
                        section.get("title"),
                        total_items,
                        len(items_to_find),
                    )
                
                if not library_items:
                    break
                
                # Process this batch: match items and update immediately
                batch_matches = 0
                for entry in library_items:
                    rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                    if not rating_key:
                        continue
                    
                    batch_processed += 1
                    
                    # Extract external IDs
                    guids = entry.get("Guid", [])
                    if not guids:
                        single_guid = entry.get("guid")
                        if single_guid:
                            guids = [{"id": single_guid}]
                    
                    external_ids = extract_external_ids_from_guids(guids)
                    
                    # If no external IDs, try fetching detailed metadata
                    if not external_ids and guids:
                        guid_value = guids[0].get("id") if isinstance(guids[0], dict) else guids[0]
                        if guid_value and guid_value.startswith("plex://"):
                            try:
                                detailed_metadata = plex_api.fetch_metadata(
                                    plex_account.plex_token,
                                    plex_uri,
                                    str(rating_key),
                                )
                                if detailed_metadata:
                                    detailed_guids = detailed_metadata.get("Guid", [])
                                    if not detailed_guids:
                                        single_guid = detailed_metadata.get("guid")
                                        if single_guid:
                                            detailed_guids = [{"id": single_guid}]
                                    external_ids = extract_external_ids_from_guids(detailed_guids)
                            except Exception as exc:
                                logger.debug(
                                    "Failed to fetch detailed Plex metadata during collection scan: %s",
                                    exception_summary(exc),
                                )
                    
                    # Try to match this Plex item with our items
                    matched_item = None
                    match_type = None
                    
                    if "tmdb_id" in external_ids:
                        tmdb_id = external_ids["tmdb_id"]
                        key = ("tmdb", tmdb_id)
                        if key in items_by_external_id:
                            matched_item = items_by_external_id[key]
                            match_type = "tmdb"
                    
                    if not matched_item and "imdb_id" in external_ids:
                        imdb_id = external_ids["imdb_id"]
                        key = ("imdb", imdb_id)
                        if key in items_by_external_id:
                            matched_item = items_by_external_id[key]
                            match_type = "imdb"
                    
                    if not matched_item and "tvdb_id" in external_ids:
                        tvdb_id = external_ids["tvdb_id"]
                        key = ("tvdb", tvdb_id)
                        if key in items_by_external_id:
                            matched_item = items_by_external_id[key]
                            match_type = "tvdb"
                    
                    # If we found a match, process it immediately
                    if matched_item:
                        item_key = (matched_item.source, matched_item.media_id)
                        if item_key not in items_found_set:
                            try:
                                # Use webhook function for consistency
                                entry_id = update_collection_metadata_from_plex_webhook(
                                    user_id=user.id,
                                    item_id=matched_item.id,
                                    rating_key=str(rating_key),
                                    plex_uri=plex_uri,
                                    plex_token=plex_account.plex_token,
                                )
                                if entry_id:
                                    items_found_set.add(item_key)
                                    batch_matches += 1
                                    batch_matched += 1
                                    updated_count += 1
                                    match_stats[match_type] = match_stats.get(match_type, 0) + 1
                            except Exception as exc:
                                logger.warning(
                                    "Failed to update item %s: %s",
                                    matched_item.title,
                                    exc,
                                )
                                error_count += 1
                
                # Log progress after each batch
                elapsed = time.time() - section_start_time
                items_remaining = len(items_to_find) - len(items_found_set)
                match_rate = (batch_matched / batch_processed * 100) if batch_processed > 0 else 0
                
                # Estimate time remaining
                if batch_processed > 0 and total_items:
                    items_per_second = batch_processed / elapsed if elapsed > 0 else 0
                    remaining_items = total_items - start - len(library_items)
                    estimated_seconds = remaining_items / items_per_second if items_per_second > 0 else 0
                    estimated_minutes = int(estimated_seconds / 60)
                else:
                    estimated_minutes = None
                
                logger.info(
                    "Processed %d/%d items in section '%s': %d matched this batch, %d total matched (%.1f%% overall), "
                    "%d/%d target items found. Updated: %d so far%s",
                    start + len(library_items),
                    total_items or "?",
                    section.get("title"),
                    batch_matches,
                    batch_matched,
                    match_rate,
                    len(items_found_set),
                    len(items_to_find),
                    updated_count,
                    f", ~{estimated_minutes} min remaining" if estimated_minutes is not None else "",
                )
                
                # Check if we need to paginate
                start += len(library_items)
                if start >= total or len(library_items) == 0:
                    break
            # Count unmatched items
            unmatched_count = len(items_needing_scan) - len(items_found_set)
            if unmatched_count > 0:
                match_stats["unmatched"] = match_stats.get("unmatched", 0) + unmatched_count

        except Exception as exc:
            logger.warning(
                "Failed to process section %s: %s",
                section.get("title"),
                exc,
                exc_info=True,
            )
            error_count += 1
            continue

        # Log final statistics for this section (before resetting)
        section_cached = match_stats.get("cached", 0)
        section_tmdb = match_stats.get("tmdb", 0)
        section_imdb = match_stats.get("imdb", 0)
        section_tvdb = match_stats.get("tvdb", 0)
        section_unmatched = match_stats.get("unmatched", 0)
        section_total = section_cached + section_tmdb + section_imdb + section_tvdb + section_unmatched
        
        if section_total > 0:
            logger.info(
                "Section '%s' matching statistics: Cached: %d, TMDB: %d, IMDB: %d, TVDB: %d, Unmatched: %d (Total: %d)",
                section.get("title"),
                section_cached,
                section_tmdb,
                section_imdb,
                section_tvdb,
                section_unmatched,
                section_total,
            )
        
        # Reset match_stats for next section (totals are accumulated in updated_count)
        match_stats["tmdb"] = 0
        match_stats["imdb"] = 0
        match_stats["tvdb"] = 0
        match_stats["unmatched"] = 0
        match_stats["cached"] = 0
    
    # Log final summary across all sections
    total_elapsed = time.time() - start_time
    
    logger.info(
        "Collection update task completed in %.1f minutes: %d items updated, %d errors",
        total_elapsed / 60,
        updated_count,
        error_count,
    )
    
    return {
        "updated": updated_count,
        "errors": error_count,
        "message": f"Updated collection metadata for {updated_count} items",
    }


@shared_task(name="Scheduled backup export")
def scheduled_backup_export(user_id, media_types=None, include_lists=True):
    """Celery task for exporting a CSV backup to the backup directory."""
    from integrations import exports

    User = get_user_model()
    user = User.objects.get(id=user_id)
    filepath = exports.write_backup(user, media_types=media_types, include_lists=include_lists)
    return f"Backup saved to {filepath}"
