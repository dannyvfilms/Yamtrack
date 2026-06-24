import logging
from contextlib import contextmanager, suppress
from types import SimpleNamespace

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils.module_loading import import_string
from simple_history.models import HistoricalRecords

logger = logging.getLogger(__name__)

WEBHOOK_PROCESSORS = {
    "plex": "integrations.webhooks.plex.PlexWebhookProcessor",
    "jellyfin": "integrations.webhooks.jellyfin.JellyfinWebhookProcessor",
    "emby": "integrations.webhooks.emby.EmbyWebhookProcessor",
    "jellyseerr": "integrations.webhooks.jellyseerr.JellyseerrWebhookProcessor",
    "kodi": "integrations.webhooks.kodi.KodiWebhookProcessor",
}


@contextmanager
def _webhook_history_user(user):
    """Attribute history rows to the webhook user.

    Mirrors simple_history's HistoryRequestMiddleware so Episode history rows
    keep history_user_id when created from a Celery task.
    """
    HistoricalRecords.context.request = SimpleNamespace(user=user)
    try:
        yield
    finally:
        with suppress(AttributeError):
            del HistoricalRecords.context.request


@shared_task(name="Process media server webhook")
def process_webhook(provider, payload, user_id):
    """Process a validated media server webhook payload in the background.

    Keeps webhook HTTP handlers fast: external metadata lookups and DB writes
    run on the worker instead of blocking a web worker.
    """
    user_model = get_user_model()
    try:
        user = user_model.objects.get(pk=user_id)
    except user_model.DoesNotExist:
        logger.warning("Skipping %s webhook for missing user id %s", provider, user_id)
        return

    processor = import_string(WEBHOOK_PROCESSORS[provider])()
    try:
        with _webhook_history_user(user):
            processor.process_payload(payload, user)
    except Exception:
        logger.exception("Error processing %s webhook payload", provider)
        if provider == "plex":
            user.mark_plex_webhook_error(
                "Plex webhook processing failed. Check server logs for details.",
            )
        raise
    if provider == "plex":
        user.mark_plex_webhook_received()
