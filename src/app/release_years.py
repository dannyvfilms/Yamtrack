"""Helpers for pre-filling card release years before template render."""

from __future__ import annotations

from collections.abc import Iterable

from django.utils import timezone

from app.models import Item, MediaTypes


def _resolve_item(entry):
    return getattr(entry, "item", entry)


def prefill_display_release_years(entries: Iterable[object]) -> None:
    """Attach ``display_release_year`` to Items when it is cheaply derivable."""
    season_keys: set[tuple[str, str, int]] = set()
    resolved_items = []

    for entry in entries:
        item = _resolve_item(entry)
        if not item:
            continue
        resolved_items.append(item)
        if getattr(item, "display_release_year", None):
            continue
        if getattr(item, "release_datetime", None):
            item.display_release_year = timezone.localtime(item.release_datetime).year
            continue
        if (
            item.media_type == MediaTypes.SEASON.value
            and item.season_number is not None
            and item.media_id
            and item.source
        ):
            season_keys.add(
                (str(item.media_id), str(item.source), int(item.season_number)),
            )

    if not season_keys:
        return

    media_ids = {media_id for media_id, _source, _season_number in season_keys}
    sources = {source for _media_id, source, _season_number in season_keys}
    season_numbers = {
        season_number
        for _media_id, _source, season_number in season_keys
    }
    season_years: dict[tuple[str, str, int], int] = {}
    for media_id, source, season_number, release_dt in (
        Item.objects.filter(
            media_type=MediaTypes.EPISODE.value,
            media_id__in=media_ids,
            source__in=sources,
            season_number__in=season_numbers,
            release_datetime__isnull=False,
        )
        .order_by("release_datetime")
        .values_list("media_id", "source", "season_number", "release_datetime")
    ):
        key = (str(media_id), str(source), int(season_number))
        if key in season_keys and key not in season_years:
            season_years[key] = timezone.localtime(release_dt).year

    for item in resolved_items:
        if getattr(item, "display_release_year", None):
            continue
        if (
            item.media_type == MediaTypes.SEASON.value
            and item.season_number is not None
            and item.media_id
            and item.source
        ):
            item.display_release_year = season_years.get(
                (str(item.media_id), str(item.source), int(item.season_number)),
            )
