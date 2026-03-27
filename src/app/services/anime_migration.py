"""Explicit migration helpers for flat MAL anime -> grouped TV-style tracking."""

from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from app.models import (
    Anime,
    Episode,
    Item,
    MediaTypes,
    MetadataProviderPreference,
    Season,
    Status,
    TV,
)
from app.providers import services
from app.services.bulk_episode_tracking import distribute_timestamps
from app.services.metadata_resolution import upsert_provider_links
from app.services.tracking_hydration import ensure_item_metadata
from integrations import anime_mapping


class AnimeMigrationError(Exception):
    """Raised when grouped-anime migration cannot be completed safely."""


@dataclass(slots=True)
class AnimeMigrationResult:
    """Result payload for a grouped-anime migration."""

    grouped_tv: TV
    migrated_entries: list[Anime]


def _provider_series_id(entry: dict, provider: str) -> str | None:
    if provider == "tvdb":
        value = entry.get("tvdb_id")
        return str(value) if value not in (None, "") else None
    for key in ("tmdb_show_id", "tmdb_id", "tmdb_tv_id"):
        value = entry.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _provider_season_number(entry: dict, provider: str) -> int | None:
    keys = ["tvdb_season"] if provider == "tvdb" else ["tmdb_season", "tvdb_season", "season"]
    for key in keys:
        value = entry.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _provider_episode_offset(entry: dict, provider: str) -> int:
    keys = ["tvdb_epoffset"] if provider == "tvdb" else ["tmdb_epoffset", "tvdb_epoffset"]
    for key in keys:
        value = entry.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _distribution_timestamps(anime: Anime, total_episodes: int) -> list:
    return distribute_timestamps(
        anime.start_date,
        anime.end_date,
        total_episodes,
        fallback_dt=anime.progressed_at or anime.created_at or timezone.now(),
    )


def _entries_for_provider_series(anime_entries: list[Anime], provider: str, series_id: str) -> list[tuple[Anime, dict]]:
    matches: list[tuple[Anime, dict]] = []
    for anime in anime_entries:
        mapping_entries = anime_mapping.find_entries_for_mal_id(anime.item.media_id)
        provider_entries = [
            entry
            for entry in mapping_entries
            if _provider_series_id(entry, provider) == str(series_id)
        ]
        if len(provider_entries) > 1:
            raise AnimeMigrationError(
                f'Ambiguous mapping for "{anime.item.title}" on {provider.upper()}.',
            )
        if provider_entries:
            matches.append((anime, provider_entries[0]))
    return matches


def _entry_activity_datetime(anime: Anime):
    """Return the most relevant activity datetime for a flat anime row."""
    return anime.end_date or anime.progressed_at or anime.created_at or timezone.now()


def migrate_flat_anime_to_grouped(user, anime_item: Item, provider: str) -> AnimeMigrationResult:
    """Migrate all matching flat anime rows for a user into a grouped series."""
    if anime_item.source != "mal" or anime_item.media_type != MediaTypes.ANIME.value:
        raise AnimeMigrationError("Only flat MAL anime can be migrated in this phase.")

    provider_series_id = anime_mapping.resolve_provider_series_id(
        anime_item.media_id,
        provider,
    )
    if not provider_series_id:
        raise AnimeMigrationError(
            f"No {provider.upper()} mapping was found for this anime.",
        )

    flat_anime_entries = list(
        Anime.all_objects.filter(
            user=user,
            migrated_to_item__isnull=True,
        ).select_related("item"),
    )
    mapped_entries = _entries_for_provider_series(
        flat_anime_entries,
        provider,
        provider_series_id,
    )
    if not mapped_entries:
        raise AnimeMigrationError("No unmigrated anime entries matched this grouped series.")

    season_numbers = []
    for _anime, mapping_entry in mapped_entries:
        season_number = _provider_season_number(mapping_entry, provider)
        if season_number is None:
            raise AnimeMigrationError("One or more mapped entries do not define a season.")
        season_numbers.append(season_number)

    hydration = ensure_item_metadata(
        user,
        MediaTypes.ANIME.value,
        provider_series_id,
        provider,
        identity_media_type=MediaTypes.TV.value,
        library_media_type=MediaTypes.ANIME.value,
    )
    series_metadata = hydration.metadata
    grouped_tv, _ = TV.objects.get_or_create(
        item=hydration.item,
        user=user,
        defaults={
            "status": Status.PLANNING.value,
            "score": None,
            "notes": "",
        },
    )
    upsert_provider_links(
        grouped_tv.item,
        series_metadata,
        provider=provider,
        provider_media_type=MediaTypes.TV.value,
    )
    tv_with_seasons = services.get_media_metadata(
        "tv_with_seasons",
        provider_series_id,
        provider,
        season_numbers,
    )

    for anime, mapping_entry in mapped_entries:
        season_number = _provider_season_number(mapping_entry, provider)
        episode_offset = _provider_episode_offset(mapping_entry, provider)
        season_key = f"season/{season_number}"
        season_metadata = tv_with_seasons.get(season_key)
        if not season_metadata:
            raise AnimeMigrationError(
                f"Season {season_number} could not be loaded from {provider.upper()}.",
            )

        available_episodes = len(season_metadata.get("episodes") or [])
        if anime.progress > max(available_episodes - episode_offset, 0):
            raise AnimeMigrationError(
                f'"{anime.item.title}" has more watched episodes than the mapped season can hold.',
            )

    latest_score_entry = None
    latest_notes_entry = None
    now = timezone.now()

    for anime, mapping_entry in mapped_entries:
        season_number = _provider_season_number(mapping_entry, provider)
        episode_offset = _provider_episode_offset(mapping_entry, provider)
        upsert_provider_links(
            anime.item,
            {
                "media_id": provider_series_id,
                "source": provider,
                "identity_media_type": MediaTypes.TV.value,
                "provider_external_ids": {
                    f"{provider}_id": provider_series_id,
                },
            },
            provider=provider,
            provider_media_type=MediaTypes.TV.value,
            season_number=season_number,
            episode_offset=episode_offset,
            extra_metadata={
                "season_number": season_number,
                "episode_offset": episode_offset,
            },
        )
        season_key = f"season/{season_number}"
        season_metadata = tv_with_seasons[season_key]
        season_image = season_metadata.get("image") or grouped_tv.item.image

        season_item, _ = Item.objects.get_or_create(
            media_id=provider_series_id,
            source=provider,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                **Item.title_fields_from_metadata(
                    season_metadata,
                    fallback_title=grouped_tv.item.title,
                ),
                "library_media_type": MediaTypes.ANIME.value,
                "image": season_image,
            },
        )
        season_item_updates = []
        if season_item.library_media_type != MediaTypes.ANIME.value:
            season_item.library_media_type = MediaTypes.ANIME.value
            season_item_updates.append("library_media_type")
        if season_item.image != season_image and season_image:
            season_item.image = season_image
            season_item_updates.append("image")
        if season_item_updates:
            season_item.save(update_fields=season_item_updates)

        season_tracker, _ = Season.objects.get_or_create(
            item=season_item,
            user=user,
            related_tv=grouped_tv,
            defaults={
                "status": Status.PLANNING.value,
                "score": None,
                "notes": "",
            },
        )

        watched_episode_numbers = [
            episode_offset + episode_index
            for episode_index in range(1, int(anime.progress or 0) + 1)
        ]
        watched_timestamps = _distribution_timestamps(anime, len(watched_episode_numbers))

        for episode_number, watched_at in zip(watched_episode_numbers, watched_timestamps, strict=False):
            episode_item = season_tracker.get_episode_item(
                episode_number,
                season_metadata,
            )
            if episode_item.library_media_type != MediaTypes.ANIME.value:
                episode_item.library_media_type = MediaTypes.ANIME.value
                episode_item.save(update_fields=["library_media_type"])

            Episode.objects.create(
                related_season=season_tracker,
                item=episode_item,
                end_date=watched_at,
            )

        if anime.score is not None and (
            latest_score_entry is None
            or _entry_activity_datetime(anime) > _entry_activity_datetime(latest_score_entry)
        ):
            latest_score_entry = anime
        if anime.notes and (
            latest_notes_entry is None
            or _entry_activity_datetime(anime) > _entry_activity_datetime(latest_notes_entry)
        ):
            latest_notes_entry = anime

    update_fields: list[str] = []
    if latest_score_entry is not None and grouped_tv.score != latest_score_entry.score:
        grouped_tv.score = latest_score_entry.score
        update_fields.append("score")
    if latest_notes_entry is not None and grouped_tv.notes != latest_notes_entry.notes:
        grouped_tv.notes = latest_notes_entry.notes
        update_fields.append("notes")
    if grouped_tv.status == Status.PLANNING.value:
        grouped_tv.status = Status.IN_PROGRESS.value
        update_fields.append("status")
    if update_fields:
        grouped_tv.save(update_fields=update_fields)

    migrated_entries = [anime for anime, _mapping in mapped_entries]
    for anime in migrated_entries:
        anime.migrated_to_item = grouped_tv.item
        anime.migrated_at = now
    Anime.all_objects.bulk_update(
        migrated_entries,
        ["migrated_to_item", "migrated_at"],
    )
    MetadataProviderPreference.objects.update_or_create(
        user=user,
        item=grouped_tv.item,
        defaults={"provider": provider},
    )

    return AnimeMigrationResult(
        grouped_tv=grouped_tv,
        migrated_entries=migrated_entries,
    )
