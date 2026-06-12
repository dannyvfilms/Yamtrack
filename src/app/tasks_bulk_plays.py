"""Bulk plays Celery tasks (episode and music).

Extracted from tasks.py. Re-exported from app.tasks for backward compatibility.
Tasks keep their original explicit Celery names so queued tasks survive the deploy.
"""

import logging

from celery import shared_task
from django.contrib.auth import get_user_model

from app.models import Item, MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="Bulk Episode Plays")
def bulk_episode_plays_task(
    self,
    user_id: int,
    media_type: str,
    source: str,
    media_id: str,
    first_season_number: int,
    first_episode_number: int,
    last_season_number: int,
    last_episode_number: int,
    write_mode: str,
    distribution_mode: str,
    start_date_str: str | None,
    end_date_str: str | None,
    identity_media_type: str | None = None,
    library_media_type: str | None = None,
):
    """Apply a bulk episode play range in the background after the modal has closed."""
    from datetime import date  # noqa: PLC0415

    from app.models import PodcastShow  # noqa: PLC0415
    from app.services import bulk_episode_tracking, metadata_resolution  # noqa: PLC0415

    logger.info(
        "bulk_episode_plays_task_start task_id=%s user_id=%d media_id=%s",
        self.request.id,
        user_id,
        media_id,
    )
    User = get_user_model()
    user = User.objects.get(id=user_id)

    metadata_item = None
    base_metadata = None
    metadata_resolution_result = None
    podcast_show = None

    if media_type == MediaTypes.PODCAST.value and source in {
        Sources.POCKETCASTS.value,
        Sources.GPODDER.value,
    }:
        podcast_show = PodcastShow.objects.filter(podcast_uuid=media_id).first()
    else:
        tracking_media_type = metadata_resolution.get_tracking_media_type(
            media_type,
            source=source,
            identity_media_type=identity_media_type or None,
        )
        item_lookup = {
            "media_id": media_id,
            "source": source,
            "media_type": tracking_media_type,
        }
        if media_type == MediaTypes.ANIME.value and source in {
            Sources.TMDB.value,
            Sources.TVDB.value,
        }:
            item_lookup["library_media_type"] = MediaTypes.ANIME.value
        metadata_item = Item.objects.filter(**item_lookup).first()
        base_metadata = services.get_media_metadata(media_type, media_id, source)
        if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            metadata_resolution_result = metadata_resolution.resolve_detail_metadata(
                user,
                item=metadata_item,
                route_media_type=media_type,
                media_id=media_id,
                source=source,
                base_metadata=base_metadata,
            )

    episode_domain = bulk_episode_tracking.build_episode_play_domain(
        user,
        media_type,
        source,
        media_id,
        metadata_item=metadata_item,
        base_metadata=base_metadata,
        metadata_resolution_result=metadata_resolution_result,
        podcast_show=podcast_show,
    )
    if not episode_domain:
        logger.warning(
            "bulk_episode_plays_task: domain unavailable media_type=%s source=%s media_id=%s",
            media_type,
            source,
            media_id,
        )
        return

    all_episodes = episode_domain.get("episodes", [])
    episode_lookup = {
        (ep["season_number"], ep["episode_number"]): ep
        for ep in all_episodes
    }
    first_ep = episode_lookup.get((first_season_number, first_episode_number))
    last_ep = episode_lookup.get((last_season_number, last_episode_number))
    if first_ep is None or last_ep is None:
        logger.warning(
            "bulk_episode_plays_task: episode range not found first=(%s,%s) last=(%s,%s)",
            first_season_number,
            first_episode_number,
            last_season_number,
            last_episode_number,
        )
        return

    selected_episodes = [
        ep for ep in all_episodes
        if first_ep["order"] <= ep["order"] <= last_ep["order"]
    ]

    start_date = date.fromisoformat(start_date_str.split("T")[0]) if start_date_str else None
    end_date = date.fromisoformat(end_date_str.split("T")[0]) if end_date_str else None

    result = bulk_episode_tracking.apply_bulk_episode_plays(
        user,
        episode_domain,
        selected_episodes=selected_episodes,
        write_mode=write_mode,
        distribution_mode=distribution_mode,
        start_date=start_date,
        end_date=end_date,
    )
    logger.info(
        "bulk_episode_plays_task_complete created=%d user_id=%d media_id=%s",
        result.created_count,
        user_id,
        media_id,
    )


@shared_task(bind=True, name="Bulk Music Plays")
def bulk_music_plays_task(
    self,
    user_id: int,
    context_kind: str,
    context_id: int,
    first_season_number: int,
    first_episode_number: int,
    last_season_number: int,
    last_episode_number: int,
    write_mode: str,
    distribution_mode: str,
    start_date_str: str | None,
    end_date_str: str | None,
):
    """Apply a bulk music play range in the background after the modal has closed."""
    from datetime import date  # noqa: PLC0415

    from app.models import Album, Artist  # noqa: PLC0415
    from app.services import bulk_music_tracking  # noqa: PLC0415

    logger.info(
        "bulk_music_plays_task_start task_id=%s user_id=%d context_kind=%s context_id=%d",
        self.request.id,
        user_id,
        context_kind,
        context_id,
    )
    User = get_user_model()
    user = User.objects.get(id=user_id)

    if context_kind == "artist":
        artist = Artist.objects.filter(id=context_id).first()
        if artist is None:
            logger.warning("bulk_music_plays_task: artist %d not found", context_id)
            return
        bulk_domain = bulk_music_tracking.build_artist_play_domain(user, artist)
    elif context_kind == "album":
        album = Album.objects.select_related("artist").filter(id=context_id).first()
        if album is None:
            logger.warning("bulk_music_plays_task: album %d not found", context_id)
            return
        bulk_domain = bulk_music_tracking.build_album_play_domain(user, album)
    else:
        logger.warning("bulk_music_plays_task: unknown context_kind=%s", context_kind)
        return

    if not bulk_domain:
        logger.warning(
            "bulk_music_plays_task: domain unavailable context_kind=%s context_id=%d",
            context_kind,
            context_id,
        )
        return

    all_episodes = bulk_domain.get("episodes", [])
    episode_lookup = {
        (ep["season_number"], ep["episode_number"]): ep
        for ep in all_episodes
    }
    first_ep = episode_lookup.get((first_season_number, first_episode_number))
    last_ep = episode_lookup.get((last_season_number, last_episode_number))
    if first_ep is None or last_ep is None:
        logger.warning(
            "bulk_music_plays_task: track range not found first=(%s,%s) last=(%s,%s)",
            first_season_number,
            first_episode_number,
            last_season_number,
            last_episode_number,
        )
        return

    selected_episodes = [
        ep for ep in all_episodes
        if first_ep["order"] <= ep["order"] <= last_ep["order"]
    ]

    start_date = date.fromisoformat(start_date_str.split("T")[0]) if start_date_str else None
    end_date = date.fromisoformat(end_date_str.split("T")[0]) if end_date_str else None

    result = bulk_music_tracking.apply_bulk_music_plays(
        user,
        bulk_domain,
        selected_episodes=selected_episodes,
        write_mode=write_mode,
        distribution_mode=distribution_mode,
        start_date=start_date,
        end_date=end_date,
    )
    logger.info(
        "bulk_music_plays_task_complete created=%d user_id=%d context_kind=%s context_id=%d",
        result.created_count,
        user_id,
        context_kind,
        context_id,
    )
