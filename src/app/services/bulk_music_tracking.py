"""Bulk music play helpers for artist and album tracking modals."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from app import history_cache
from app.mixins import disable_fetch_releases
from app.models import (
    Album,
    AlbumTracker,
    Artist,
    ArtistTracker,
    Item,
    MediaTypes,
    Music,
    Sources,
    Status,
    Track,
)
from app.signals import (
    flush_media_change_side_effects,
    suppress_media_change_side_effects,
)
from app.services import music as music_services
from app.services.bulk_episode_tracking import (
    coerce_episode_datetime,
    distribute_target_timestamps,
    distribute_timestamps,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BulkMusicPlayResult:
    """Outcome payload for a bulk music logging request."""

    created_count: int
    replaced_play_count: int


def _track_selector_label(track: Track) -> str:
    """Return a readable dropdown label for a music track selector."""
    prefix_bits = []
    if (track.disc_number or 1) > 1:
        prefix_bits.append(f"D{track.disc_number}")
    if track.track_number is not None:
        prefix_bits.append(f"T{track.track_number:02d}")
    elif track.id:
        prefix_bits.append(f"#{track.id}")

    prefix = " ".join(prefix_bits) if prefix_bits else "Track"
    label = f"{prefix} - {track.title or 'Unknown Track'}"
    if track.duration_formatted:
        label += f" ({track.duration_formatted})"
    return label


def _album_sort_key(album: Album):
    """Return a stable artist discography order for album selectors."""
    return (
        album.release_date is None,
        album.release_date or timezone.localdate(),
        (album.title or "").lower(),
        album.id,
    )


def _album_title_for_selector(album: Album) -> str:
    """Return the album label shown in the range selectors."""
    if album.release_date:
        return f"{album.title} ({album.release_date.year})"
    return album.title


def _track_release_datetime(album: Album, track_order: int):
    """Return a comparable release datetime for a track within its album."""
    base_release = coerce_episode_datetime(album.release_date)
    if base_release is None:
        return None
    return base_release + timedelta(seconds=track_order)


def _history_play_counts_by_track(music_entries: list[Music]) -> dict[int, int]:
    """Return grouped listen counts for current Music rows keyed by track id."""
    if not music_entries:
        return {}

    historical_music = apps.get_model("app", "HistoricalMusic")
    counts_by_music_id = {
        row["id"]: row["play_count"]
        for row in (
            historical_music.objects.filter(
                id__in=[entry.id for entry in music_entries if entry.id],
                end_date__isnull=False,
            )
            .values("id")
            .annotate(play_count=Count("history_id"))
        )
    }

    track_counts: dict[int, int] = {}
    for entry in music_entries:
        if not entry.track_id:
            continue
        track_counts[entry.track_id] = track_counts.get(entry.track_id, 0) + counts_by_music_id.get(
            entry.id,
            0,
        )
    return track_counts


def _ensure_album_tracks(album: Album) -> None:
    """Populate local Track rows for an album when possible."""
    if album.tracklist.exists():
        return
    if not music_services.album_has_musicbrainz_id(album):
        return
    music_services.populate_album_tracks(album)


def _build_domain_from_albums(
    *, albums: list[Album], context_kind: str, context_id: int, skip_missing_fetch: bool = False
):
    """Return a selector domain that reuses the shared bulk range form."""
    available_albums = []
    excluded_albums = 0
    episodes = []
    season_episode_map = {}

    for album in sorted(albums, key=_album_sort_key):
        if not skip_missing_fetch:
            _ensure_album_tracks(album)
        tracks = list(
            Track.objects.filter(album=album)
            .order_by("disc_number", "track_number", "title", "id")
        )
        if not tracks:
            excluded_albums += 1
            continue

        available_albums.append(album)
        season_tracks = []
        for track_index, track in enumerate(tracks):
            payload = {
                "order": len(episodes),
                "season_number": album.id,
                "season_title": _album_title_for_selector(album),
                "episode_number": track.id,
                "episode_title": track.title or f"Track {track_index + 1}",
                "selector_label": _track_selector_label(track),
                "air_date": _track_release_datetime(album, track_index),
                "release_datetime": _track_release_datetime(album, track_index),
                "existing_play_count": 0,
                "track_id": track.id,
                "album_id": album.id,
                "artist_id": album.artist_id,
            }
            episodes.append(payload)
            season_tracks.append(payload)

        season_episode_map[album.id] = season_tracks

    if not episodes:
        return None

    mode_notice = ""
    if excluded_albums:
        album_word = "album" if excluded_albums == 1 else "albums"
        if skip_missing_fetch:
            mode_notice = (
                f"{excluded_albums} {album_word} are still loading their track list "
                f"in the background. Reopen this modal once they're ready."
            )
        else:
            mode_notice = (
                f"Only albums with available track lists are included here. "
                f"{excluded_albums} {album_word} could not be loaded."
            )

    default_first = episodes[0]
    default_last = episodes[-1]

    return {
        "route_media_type": MediaTypes.MUSIC.value,
        "tracking_source": Sources.MUSICBRAINZ.value,
        "tracking_media_id": str(context_id),
        "tracking_media_type": MediaTypes.MUSIC.value,
        "identity_media_type": None,
        "library_media_type": MediaTypes.MUSIC.value,
        "season_payloads": {},
        "episodes": episodes,
        "episode_lookup": {
            (episode["season_number"], episode["episode_number"]): episode
            for episode in episodes
        },
        "season_episode_map": season_episode_map,
        "seasons": [
            {
                "season_number": album.id,
                "season_title": _album_title_for_selector(album),
                "episode_count": len(season_episode_map[album.id]),
                "locked": len(available_albums) == 1,
            }
            for album in available_albums
        ],
        "default_first": {
            "season_number": default_first["season_number"],
            "episode_number": default_first["episode_number"],
        },
        "default_last": {
            "season_number": default_last["season_number"],
            "episode_number": default_last["episode_number"],
        },
        "locked_season_number": available_albums[0].id if len(available_albums) == 1 else None,
        "hide_season_selectors": len(available_albums) == 1,
        "mode_notice": mode_notice,
        "is_flat_anime_grouped_slice": False,
        "context_kind": context_kind,
        "context_id": context_id,
        "season_field_label": "Album",
        "episode_field_label": "Track",
        "selection_noun": "track",
        "selection_noun_plural": "tracks",
        "distribution_target_label": "release date",
        "date_shortcut_label": "Release date",
        "missing_target_date_fallback_distribution": "even",
        "tab_label": "Track Plays",
        "submit_label": "Save plays",
    }


def build_album_play_domain(user, album: Album):
    """Return the shared bulk-play selector domain for one album."""
    domain = _build_domain_from_albums(
        albums=[album],
        context_kind="album",
        context_id=album.id,
    )
    if domain is None:
        return None

    music_entries = list(
        Music.objects.filter(user=user, album=album).only("id", "track_id"),
    )
    play_counts = _history_play_counts_by_track(music_entries)
    for payload in domain["episodes"]:
        payload["existing_play_count"] = play_counts.get(payload["track_id"], 0)
    return domain


def build_artist_play_domain(user, artist: Artist, fetch_missing: bool = True):
    """Return the shared bulk-play selector domain for an artist discography."""
    albums = list(
        Album.objects.filter(artist=artist)
        .select_related("artist")
        .order_by("release_date", "title", "id")
    )
    domain = _build_domain_from_albums(
        albums=albums,
        context_kind="artist",
        context_id=artist.id,
        skip_missing_fetch=not fetch_missing,
    )
    if domain is None:
        return None

    music_entries = list(
        Music.objects.filter(user=user, album__artist=artist).only("id", "track_id"),
    )
    play_counts = _history_play_counts_by_track(music_entries)
    for payload in domain["episodes"]:
        payload["existing_play_count"] = play_counts.get(payload["track_id"], 0)
    return domain


def _get_or_create_music_item(album: Album, track: Track):
    """Return the trackable item for a music track."""
    runtime_minutes = track.duration_ms // 60000 if track.duration_ms else None
    defaults = {
        "title": track.title or "Unknown Track",
        "image": album.image or settings.IMG_NONE,
    }
    if runtime_minutes:
        defaults["runtime_minutes"] = runtime_minutes

    media_id = track.musicbrainz_recording_id or f"track_{track.id}"
    item, created = Item.objects.get_or_create(
        media_id=media_id,
        source=Sources.MUSICBRAINZ.value,
        media_type=MediaTypes.MUSIC.value,
        defaults=defaults,
    )

    update_fields = []
    if item.title != defaults["title"]:
        item.title = defaults["title"]
        update_fields.append("title")
    if defaults["image"] and item.image != defaults["image"]:
        item.image = defaults["image"]
        update_fields.append("image")
    if runtime_minutes and item.runtime_minutes != runtime_minutes:
        item.runtime_minutes = runtime_minutes
        update_fields.append("runtime_minutes")
    if update_fields:
        item.save(update_fields=update_fields)

    music_services.sync_music_item_genres_from_album(item, album)

    return item, created


def _ensure_trackers(user, artist: Artist | None, album: Album | None, played_at):
    """Create container trackers so music stays visible in library views."""
    if artist is not None:
        tracker, created = ArtistTracker.objects.get_or_create(
            user=user,
            artist=artist,
            defaults={
                "status": Status.IN_PROGRESS.value,
                "start_date": played_at,
            },
        )
        if not created and tracker.start_date is None and played_at is not None:
            tracker.start_date = played_at
            tracker.save(update_fields=["start_date"])

    if album is not None:
        tracker, created = AlbumTracker.objects.get_or_create(
            user=user,
            album=album,
            defaults={
                "status": Status.IN_PROGRESS.value,
                "start_date": played_at,
            },
        )
        if not created and tracker.start_date is None and played_at is not None:
            tracker.start_date = played_at
            tracker.save(update_fields=["start_date"])


def apply_bulk_music_plays(
    user,
    domain,
    *,
    selected_episodes,
    write_mode: str,
    distribution_mode: str,
    start_date=None,
    end_date=None,
):
    """Persist a bulk music play range against artist or album tracking."""
    if distribution_mode == "air_date":
        timestamps = distribute_target_timestamps(
            [episode["air_date"] for episode in selected_episodes],
            start_date,
            end_date,
            fallback_dt=timezone.now().replace(second=0, microsecond=0),
        )
    else:
        timestamps = distribute_timestamps(
            start_date,
            end_date,
            len(selected_episodes),
            fallback_dt=timezone.now().replace(second=0, microsecond=0),
        )

    selected_track_ids = [
        episode["track_id"]
        for episode in selected_episodes
        if episode.get("track_id")
    ]
    tracks = Track.objects.select_related("album__artist").in_bulk(selected_track_ids)
    existing_music_entries = list(
        Music.objects.filter(user=user, track_id__in=selected_track_ids).select_related(
            "item",
            "album__artist",
            "track",
        )
    )
    existing_by_track = {entry.track_id: entry for entry in existing_music_entries if entry.track_id}
    existing_play_counts = _history_play_counts_by_track(existing_music_entries)

    replaced_play_count = 0
    created_count = 0
    affected_day_keys = set()
    affected_items = []

    with transaction.atomic(), disable_fetch_releases(), suppress_media_change_side_effects():
        if write_mode == "replace" and existing_music_entries:
            replaced_play_count = sum(existing_play_counts.values())
            for entry in existing_music_entries:
                existing_day_key = history_cache.history_day_key(getattr(entry, "end_date", None))
                if existing_day_key:
                    affected_day_keys.add(existing_day_key)
            affected_items.extend(entry.item for entry in existing_music_entries if entry.item_id)
            Music.objects.filter(id__in=[entry.id for entry in existing_music_entries]).delete()
            existing_by_track.clear()

        for track_payload, played_at in zip(selected_episodes, timestamps, strict=False):
            track = tracks.get(track_payload.get("track_id"))
            if track is None:
                continue

            album = track.album
            artist = album.artist if album else None
            _ensure_trackers(user, artist, album, played_at)

            existing_music = existing_by_track.get(track.id)
            if existing_music is not None:
                affected_items.append(existing_music.item)
                previous_day_key = history_cache.history_day_key(existing_music.end_date)
                if previous_day_key:
                    affected_day_keys.add(previous_day_key)
                item, _ = _get_or_create_music_item(album, track)
                affected_items.append(item)
                update_fields = []
                if existing_music.item_id != item.id:
                    existing_music.item = item
                    update_fields.append("item")
                if existing_music.artist_id != getattr(artist, "id", None):
                    existing_music.artist = artist
                    update_fields.append("artist")
                if existing_music.album_id != getattr(album, "id", None):
                    existing_music.album = album
                    update_fields.append("album")
                if existing_music.track_id != track.id:
                    existing_music.track = track
                    update_fields.append("track")
                if existing_music.status != Status.COMPLETED.value:
                    existing_music.status = Status.COMPLETED.value
                    update_fields.append("status")
                existing_music.end_date = played_at
                update_fields.append("end_date")
                existing_music.save(update_fields=update_fields)
            else:
                item, _ = _get_or_create_music_item(album, track)
                affected_items.append(item)
                existing_by_track[track.id] = Music.objects.create(
                    item=item,
                    user=user,
                    artist=artist,
                    album=album,
                    track=track,
                    status=Status.COMPLETED.value,
                    end_date=played_at,
                )
            played_day_key = history_cache.history_day_key(played_at)
            if played_day_key:
                affected_day_keys.add(played_day_key)
            created_count += 1

    if created_count or replaced_play_count:
        flush_media_change_side_effects(
            owner=user,
            items=affected_items,
            changed_media_type=MediaTypes.MUSIC.value,
            reason="music_change",
            history_day_keys=sorted(affected_day_keys),
            statistics_day_values=sorted(affected_day_keys),
        )

    logger.info(
        "bulk_music_plays_saved user_id=%s context=%s context_id=%s created=%s replaced=%s",
        user.id,
        domain.get("context_kind"),
        domain.get("context_id"),
        created_count,
        replaced_play_count,
    )

    return BulkMusicPlayResult(
        created_count=created_count,
        replaced_play_count=replaced_play_count,
    )
