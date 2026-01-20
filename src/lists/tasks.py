import logging

from celery import shared_task
from django.contrib.auth import get_user_model

from lists.imports import trakt as trakt_lists

logger = logging.getLogger(__name__)
User = get_user_model()


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
