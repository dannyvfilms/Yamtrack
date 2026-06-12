import logging

from celery import shared_task
from django.contrib.auth import get_user_model

from lists.imports import trakt as trakt_lists

logger = logging.getLogger(__name__)
User = get_user_model()


@shared_task(name="Sync Smart Lists For Items")
def sync_smart_lists_for_items_task(owner_id: int, item_ids: list) -> None:
    """Sync smart-list membership for a set of items — runs in background after bulk media mutations."""
    from app.models import Item
    from lists.smart_rules import sync_smart_lists_for_item

    try:
        owner = User.objects.get(pk=owner_id)
    except User.DoesNotExist:
        return
    items = list(Item.objects.filter(id__in=item_ids))
    for item in items:
        try:
            sync_smart_lists_for_item(owner=owner, item=item)
        except Exception:
            logger.exception(
                "Smart list sync failed for owner_id=%s item_id=%s", owner_id, item.id
            )


@shared_task(name="Sync Smart List Items")
def sync_smart_list_task(list_id: int) -> None:
    """Sync a smart list's membership in the background."""
    from lists.models import CustomList

    try:
        custom_list = CustomList.objects.get(pk=list_id)
    except CustomList.DoesNotExist:
        return
    if not custom_list.is_smart:
        return
    try:
        custom_list.sync_smart_items()
    except Exception:
        logger.exception("Smart list sync failed for list_id=%s", list_id)
        raise


def schedule_smart_list_sync(custom_list, debounce_seconds=60):
    """Queue a background membership sync for a smart list, debounced.

    Used on GET paths so list pages render the current membership without
    blocking on the (write-heavy) sync; the result lands moments later.
    """
    from django.core.cache import cache

    if not custom_list.is_smart:
        return False
    lock_key = f"smart_list_sync_scheduled:{custom_list.id}"
    if not cache.add(lock_key, True, timeout=debounce_seconds):
        return False
    sync_smart_list_task.delay(custom_list.id)
    return True


@shared_task(name="Import Trakt Lists")
def import_trakt_lists_task(user_id, access_token, client_id=None):
    """Celery task for importing Trakt lists asynchronously."""
    user = User.objects.get(pk=user_id)
    try:
        trakt_lists.import_trakt_lists(user, access_token, client_id=client_id)
        logger.info("Successfully imported Trakt lists for user %s", user.username)
    except Exception as error:
        logger.error(
            "Failed to import Trakt lists for user %s: %s",
            user.username,
            error,
            exc_info=True,
        )
        raise
