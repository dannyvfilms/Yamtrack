import logging

from celery import shared_task
from django.contrib.auth import get_user_model

from app.models import Item
from app.services import auto_pause
from events import notifications
from events.calendar.main import fetch_releases

logger = logging.getLogger(__name__)


def _normalize_user_id(user_or_id):
    """Coerce a User instance or scalar value into a user ID."""
    if user_or_id is None:
        return None
    candidate = getattr(user_or_id, "pk", user_or_id)
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


def _normalize_item_ids(item_ids):
    """Coerce a list of Item instances or scalar values into item IDs."""
    if item_ids is None:
        return None

    normalized = []
    for item in item_ids:
        candidate = getattr(item, "pk", item)
        try:
            normalized.append(int(candidate))
        except (TypeError, ValueError):
            continue
    return normalized


@shared_task(name="Reload calendar", ignore_result=True)
def reload_calendar(user_id=None, item_ids=None, user=None, items_to_process=None):
    """Refresh the calendar with latest dates for all users."""
    normalized_user_id = _normalize_user_id(user_id)
    if normalized_user_id is None:
        normalized_user_id = _normalize_user_id(user)

    normalized_item_ids = _normalize_item_ids(item_ids)
    if normalized_item_ids is None:
        normalized_item_ids = _normalize_item_ids(items_to_process)

    resolved_user = None
    if normalized_user_id is not None:
        User = get_user_model()
        resolved_user = User.objects.filter(id=normalized_user_id).first()
        if resolved_user is None:
            logger.warning("Skipping calendar reload for missing user_id=%s", normalized_user_id)
            return "User not found"
        logger.info("Reloading calendar for user: %s", resolved_user.username)
    else:
        logger.info("Reloading calendar for all users")

    resolved_items = None
    if normalized_item_ids is not None:
        item_lookup = Item.objects.in_bulk(normalized_item_ids)
        resolved_items = [
            item_lookup[item_id]
            for item_id in normalized_item_ids
            if item_id in item_lookup
        ]
        missing_item_ids = [
            item_id for item_id in normalized_item_ids if item_id not in item_lookup
        ]
        if missing_item_ids:
            logger.info(
                "Calendar reload skipped %d missing item IDs",
                len(missing_item_ids),
            )

    result = fetch_releases(
        user=resolved_user,
        items_to_process=resolved_items,
    )

    if resolved_user is None and normalized_item_ids is None:
        auto_pause.auto_pause_stale_items()

        # Backfill metadata for items that have never been fetched
        # Use aggressive batch size to complete initial backfill quickly
        try:
            from app.tasks import backfill_item_metadata_task, count_release_backfill_items

            remaining_metadata_count = Item.objects.filter(metadata_fetched_at__isnull=True).count()
            remaining_release_count = count_release_backfill_items()

            # Use larger batch for initial metadata imports, then keep release backfill
            # running nightly so stale cached metadata can be corrected over time.
            if remaining_metadata_count > 1000:
                batch_size = 5000  # Aggressive initial backfill
                logger.info(
                    "Initial metadata backfill: processing %s items (batch of 5000)",
                    remaining_metadata_count,
                )
            elif remaining_metadata_count > 0:
                batch_size = 1000  # Cleanup mode
                logger.info(
                    "Metadata backfill cleanup: processing remaining %s items",
                    remaining_metadata_count,
                )
            elif remaining_release_count > 0:
                batch_size = 1000  # Release-date maintenance mode
                logger.info(
                    "Release-date backfill maintenance: processing remaining %s items",
                    remaining_release_count,
                )
            else:
                batch_size = 0  # Skip if nothing to do

            if batch_size > 0:
                backfill_result = backfill_item_metadata_task(batch_size=batch_size)
                logger.info(
                    (
                        "Metadata backfill completed: %s successful, %s release dates updated, "
                        "%s errors, %s metadata remaining, %s release remaining"
                    ),
                    backfill_result.get("success_count", 0),
                    backfill_result.get("release_updated_count", 0),
                    backfill_result.get("error_count", 0),
                    backfill_result.get("remaining_metadata", 0),
                    backfill_result.get("remaining_release", 0),
                )
        except Exception as e:
            logger.error("Failed to backfill metadata during calendar reload: %s", e)

    return result


@shared_task(name="Send release notifications")
def send_release_notifications():
    """Send notifications for recently released media."""
    logger.info("Starting recent release notification task")

    return notifications.send_releases()


@shared_task(name="Send daily digest")
def send_daily_digest_notifications():
    """Send daily digest of today's releases."""
    logger.info("Starting daily digest task")

    return notifications.send_daily_digest()
