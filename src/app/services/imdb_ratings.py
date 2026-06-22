"""IMDB rating sync helpers using IMDB public non-commercial datasets."""

from __future__ import annotations

import logging

from app.models import Item, MediaTypes, Sources

logger = logging.getLogger(__name__)

APPLICABLE_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
    MediaTypes.SEASON.value,
    MediaTypes.EPISODE.value,
}
APPLICABLE_SOURCES = {Sources.TMDB.value, Sources.TVDB.value}


def get_imdb_id(item: Item) -> str | None:
    """Return the IMDB tconst for an item, or None if unavailable."""
    return (item.provider_external_ids or {}).get("imdb_id") or None


def bulk_refresh_imdb_ratings(
    ratings: dict[str, tuple[float, int]],
) -> int:
    """Update imdb_rating/imdb_rating_count on all tracked non-episode items.

    ratings is the dict returned by imdb_datasets.download_ratings():
    {tconst: (averageRating, numVotes)}

    Returns the count of items updated.
    """
    items = list(
        Item.objects.filter(
            source__in=APPLICABLE_SOURCES,
            media_type__in=[
                MediaTypes.MOVIE.value,
                MediaTypes.TV.value,
                MediaTypes.ANIME.value,
                MediaTypes.SEASON.value,
            ],
            provider_external_ids__has_key="imdb_id",
        )
    )

    to_update = []
    for item in items:
        imdb_id = get_imdb_id(item)
        if not imdb_id:
            continue
        entry = ratings.get(imdb_id)
        if entry is None:
            continue
        avg_rating, num_votes = entry
        if item.imdb_rating != avg_rating or item.imdb_rating_count != num_votes:
            item.imdb_rating = avg_rating
            item.imdb_rating_count = num_votes
            to_update.append(item)

    if to_update:
        Item.objects.bulk_update(to_update, ["imdb_rating", "imdb_rating_count"])
        logger.info("imdb_ratings: updated %d items", len(to_update))

    return len(to_update)


def get_tracked_show_imdb_ids() -> list[str]:
    """Return IMDB IDs for tracked TV/Anime show items that have episode Items."""
    from django.db.models import Exists, OuterRef  # noqa: PLC0415

    episode_subquery = Item.objects.filter(
        media_id=OuterRef("media_id"),
        source=OuterRef("source"),
        media_type=MediaTypes.EPISODE.value,
    )
    shows = (
        Item.objects.filter(
            source__in=APPLICABLE_SOURCES,
            media_type__in=[MediaTypes.TV.value, MediaTypes.ANIME.value],
        )
        .annotate(has_episodes=Exists(episode_subquery))
        .filter(has_episodes=True)
        .exclude(provider_external_ids={})
    )
    return [
        imdb_id
        for item in shows
        if (imdb_id := get_imdb_id(item))
    ]


def bulk_refresh_imdb_episode_ratings(
    ratings: dict[str, tuple[float, int]],
    episode_map: dict[str, dict[tuple[int, int], str]],
) -> int:
    """Update imdb_rating/imdb_rating_count on Episode Items.

    episode_map is {show_imdb_id: {(season, ep): episode_tconst}} from
    imdb_datasets.download_episode_map().

    Episode Items don't carry the show's imdb_id directly, so we look up
    show Items first to build a (media_id, source) → show_imdb_id mapping,
    then fetch the corresponding episode Items.

    Returns the count of episode items updated.
    """
    if not episode_map:
        return 0

    # Build (media_id, source) → show_imdb_id mapping from show Items
    show_items = Item.objects.filter(
        source__in=APPLICABLE_SOURCES,
        media_type__in=[MediaTypes.TV.value, MediaTypes.ANIME.value],
    ).exclude(provider_external_ids={})

    show_key_to_imdb: dict[tuple[str, str], str] = {}
    for item in show_items:
        imdb_id = get_imdb_id(item)
        if imdb_id and imdb_id in episode_map:
            show_key_to_imdb[(str(item.media_id), item.source)] = imdb_id

    if not show_key_to_imdb:
        return 0

    # Fetch all episode Items for these shows
    from django.db.models import Q  # noqa: PLC0415

    episode_filter = Q()
    for media_id, source in show_key_to_imdb:
        episode_filter |= Q(media_id=media_id, source=source)

    episode_items = Item.objects.filter(
        episode_filter,
        media_type=MediaTypes.EPISODE.value,
    )

    to_update = []
    for item in episode_items:
        if item.season_number is None or item.episode_number is None:
            continue
        show_imdb_id = show_key_to_imdb.get((str(item.media_id), item.source))
        if not show_imdb_id:
            continue
        season_ep_map = episode_map.get(show_imdb_id, {})
        ep_tconst = season_ep_map.get((item.season_number, item.episode_number))
        if ep_tconst is None:
            continue
        entry = ratings.get(ep_tconst)
        if entry is None:
            continue
        avg_rating, num_votes = entry
        if item.imdb_rating != avg_rating or item.imdb_rating_count != num_votes:
            item.imdb_rating = avg_rating
            item.imdb_rating_count = num_votes
            to_update.append(item)

    if to_update:
        Item.objects.bulk_update(to_update, ["imdb_rating", "imdb_rating_count"])
        logger.info("imdb_ratings: updated %d episode items", len(to_update))

    return len(to_update)
