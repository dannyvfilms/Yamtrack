import logging
import random
import time

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from app.log_safety import exception_summary

logger = logging.getLogger(__name__)

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
