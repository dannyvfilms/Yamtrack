"""IMDB ratings refresh Celery task using IMDB public datasets."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="app.tasks.refresh_imdb_ratings_from_datasets")
def refresh_imdb_ratings_from_datasets():
    """Download IMDB public datasets and bulk-update imdb_rating on tracked items."""
    from app.providers import imdb_datasets  # noqa: PLC0415
    from app.services import imdb_ratings as imdb_svc  # noqa: PLC0415

    if not imdb_datasets.is_enabled():
        return {"message": "IMDB ratings disabled"}

    try:
        ratings = imdb_datasets.download_ratings()
    except Exception as exc:
        logger.warning("imdb_datasets: failed to download ratings: %s", exc)
        return {"error": str(exc)}

    updated_items = imdb_svc.bulk_refresh_imdb_ratings(ratings)

    show_imdb_ids = imdb_svc.get_tracked_show_imdb_ids()
    updated_episodes = 0
    if show_imdb_ids:
        try:
            episode_map = imdb_datasets.download_episode_map(set(show_imdb_ids))
            updated_episodes = imdb_svc.bulk_refresh_imdb_episode_ratings(ratings, episode_map)
        except Exception as exc:
            logger.warning("imdb_datasets: failed to download episode map: %s", exc)

    return {
        "updated_items": updated_items,
        "updated_episodes": updated_episodes,
    }
