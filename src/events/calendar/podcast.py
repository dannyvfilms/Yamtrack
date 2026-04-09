import logging

from django.utils import timezone

from app.models import PodcastEpisode
from events.models import Event

logger = logging.getLogger(__name__)


def process_podcast(item, events_bulk):
    """Process podcast episodes using stored publish dates."""
    logger.info("Processing podcast episode: %s", item)

    release_datetime = item.release_datetime
    episode_number = None

    try:
        episode = PodcastEpisode.objects.get(episode_uuid=item.media_id)
        episode_number = episode.episode_number or None
        if not release_datetime and episode.published:
            release_datetime = episode.published
    except PodcastEpisode.DoesNotExist:
        logger.debug("Podcast episode metadata not found for item %s", item.media_id)

    if not release_datetime:
        logger.debug("Skipping podcast %s - no release date available", item)
        return

    if timezone.is_naive(release_datetime):
        release_datetime = timezone.make_aware(release_datetime)

    events_bulk.append(
        Event(
            item=item,
            content_number=episode_number,
            datetime=release_datetime,
        ),
    )
