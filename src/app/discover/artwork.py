"""Artwork hydration for Discover row candidates."""

from __future__ import annotations

import logging

from django.conf import settings

from app.discover.adapters import TMDB_ADAPTER
from app.discover.schemas import CandidateItem, RowDefinition, RowResult
from app.discover.service_helpers import MAX_ITEMS_PER_ROW
from app.models import Item, MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

ROW_CACHE_TTL_SECONDS = 60 * 60
ROW_CACHE_TTL_LOCAL_SECONDS = 60 * 30

PROVIDER_ARTWORK_HYDRATION_ROW_KEYS = {
    "trending_right_now",
    "all_time_greats_unseen",
    "coming_soon",
}


def _row_ttl_seconds(row_definition: RowDefinition) -> int:
    return ROW_CACHE_TTL_LOCAL_SECONDS if row_definition.source == "local" else ROW_CACHE_TTL_SECONDS


def _is_missing_image(candidate: CandidateItem) -> bool:
    return not candidate.image or candidate.image == settings.IMG_NONE


def _provider_media_type_for_artwork(candidate_media_type: str) -> str | None:
    if candidate_media_type == MediaTypes.MOVIE.value:
        return MediaTypes.MOVIE.value
    if candidate_media_type in {MediaTypes.TV.value, MediaTypes.ANIME.value}:
        return MediaTypes.TV.value
    return None


def _supports_provider_artwork_hydration(candidate: CandidateItem) -> bool:
    return (
        (
            candidate.media_type == MediaTypes.BOARDGAME.value
            and candidate.source == Sources.BGG.value
        )
        or (
            candidate.media_type == MediaTypes.MUSIC.value
            and candidate.source == Sources.MUSICBRAINZ.value
        )
    )


def _hydrate_provider_ranked_artwork(
    candidates: list[CandidateItem],
    *,
    allow_remote: bool = True,
    hydrate_limit: int = MAX_ITEMS_PER_ROW,
) -> None:
    """Hydrate missing artwork for top provider-ranked boardgame/music candidates."""
    display_candidates = [
        candidate
        for candidate in candidates[:hydrate_limit]
        if _supports_provider_artwork_hydration(candidate)
    ]
    if not display_candidates:
        return

    missing = [candidate for candidate in display_candidates if _is_missing_image(candidate)]
    if not missing:
        return

    local_images = {
        (item.media_type, item.source, str(item.media_id)): item.image
        for item in Item.objects.filter(
            media_id__in=[candidate.media_id for candidate in missing],
            media_type__in=[MediaTypes.BOARDGAME.value, MediaTypes.MUSIC.value],
            source__in=[Sources.BGG.value, Sources.MUSICBRAINZ.value],
        ).only("media_type", "source", "media_id", "image")
        if item.image and item.image != settings.IMG_NONE
    }

    for candidate in missing:
        local_image = local_images.get(candidate.identity())
        if local_image:
            candidate.image = local_image

    if not allow_remote:
        return

    for candidate in missing:
        if not _is_missing_image(candidate):
            continue
        try:
            metadata = services.get_media_metadata(
                candidate.media_type,
                candidate.media_id,
                candidate.source,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "discover_provider_artwork_lookup_failed media_type=%s source=%s media_id=%s error=%s",
                candidate.media_type,
                candidate.source,
                candidate.media_id,
                error,
            )
            continue

        image = (metadata or {}).get("image")
        if image and image != settings.IMG_NONE:
            candidate.image = image


def _hydrate_trakt_ranked_artwork(
    media_type: str,
    candidates: list[CandidateItem],
    *,
    allow_remote: bool = True,
    hydrate_limit: int = MAX_ITEMS_PER_ROW,
) -> None:
    """Hydrate missing artwork for displayed Trakt-ranked TMDB candidates."""
    provider_media_type = _provider_media_type_for_artwork(media_type)
    if provider_media_type is None:
        return

    display_candidates = [
        candidate
        for candidate in candidates[:hydrate_limit]
        if candidate.media_type == media_type
        and candidate.source == TMDB_ADAPTER.provider
    ]
    if not display_candidates:
        return

    missing = [candidate for candidate in display_candidates if _is_missing_image(candidate)]
    if not missing:
        return

    local_images = {
        str(item.media_id): item.image
        for item in Item.objects.filter(
            media_type=media_type,
            source=TMDB_ADAPTER.provider,
            media_id__in=[candidate.media_id for candidate in missing],
        ).only("media_id", "image")
        if item.image and item.image != settings.IMG_NONE
    }

    for candidate in missing:
        local_image = local_images.get(str(candidate.media_id))
        if local_image:
            candidate.image = local_image

    if not allow_remote:
        return

    for candidate in missing:
        if not _is_missing_image(candidate):
            continue
        try:
            metadata = services.get_media_metadata(
                provider_media_type,
                candidate.media_id,
                TMDB_ADAPTER.provider,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "discover_tmdb_artwork_lookup_failed media_id=%s error=%s",
                candidate.media_id,
                error,
            )
            continue

        image = (metadata or {}).get("image")
        if image:
            candidate.image = image


def hydrate_visible_row_artwork(
    row: RowResult,
    *,
    allow_remote: bool = True,
) -> None:
    """Hydrate missing artwork for currently visible row items.

    This is used by the optimistic Discover tab-cache patching path so a
    reserve item promoted into the visible 12 can render with poster artwork
    immediately instead of waiting for a later full row rebuild.
    """
    if not row.items:
        return

    if not any(_is_missing_image(item) for item in row.items[:MAX_ITEMS_PER_ROW]):
        return

    effective_media_type = next(
        (
            candidate.media_type
            for candidate in [*row.items, *row.reserve_items]
            if candidate.media_type
        ),
        None,
    )
    if not effective_media_type:
        return

    if effective_media_type in {
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    } and row.key in {
        "trending_right_now",
        "all_time_greats_unseen",
        "coming_soon",
    }:
        _hydrate_trakt_ranked_artwork(
            effective_media_type,
            row.items,
            allow_remote=allow_remote,
        )
        return

    if row.source == "provider" and row.key in PROVIDER_ARTWORK_HYDRATION_ROW_KEYS:
        _hydrate_provider_ranked_artwork(
            row.items,
            allow_remote=allow_remote,
        )
