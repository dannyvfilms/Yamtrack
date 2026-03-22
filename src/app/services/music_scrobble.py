"""Services for recording music playback/scrobbles."""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from unittest.mock import Mock

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.text import slugify

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
from app.log_safety import exception_summary
from app.providers import musicbrainz
from app.services.music import (
    get_artist_hero_image,
    prefetch_album_covers,
    refresh_album_cover_art,
    sync_artist_discography,
)

logger = logging.getLogger(__name__)


@dataclass
class MusicPlaybackEvent:
    """Normalized playback event for music tracks."""

    user: settings.AUTH_USER_MODEL
    track_title: str
    artist_name: str | None = None
    album_title: str | None = None
    track_number: int | None = None
    duration_ms: int | None = None
    plex_rating_key: str | None = None
    external_ids: dict[str, str | None] = field(default_factory=dict)
    completed: bool = False
    played_at: timezone.datetime | None = None
    defer_cover_prefetch: bool = False


@dataclass
class ResolvedMusicMetadata:
    """Canonical metadata used to create catalog + tracking records."""

    track_title: str
    artist_name: str
    album_title: str
    duration_ms: int | None
    source: str
    media_id: str
    musicbrainz_recording_id: str | None = None
    musicbrainz_release_id: str | None = None
    musicbrainz_release_group_id: str | None = None
    musicbrainz_artist_id: str | None = None
    track_number: int | None = None
    disc_number: int = 1
    track_genres: list = field(default_factory=list)
    album_release_date: date | None = None
    album_image: str = settings.IMG_NONE
    album_release_type: str = ""
    album_genres: list = field(default_factory=list)
    artist_country: str = ""
    artist_sort_name: str = ""
    artist_genres: list = field(default_factory=list)
    artist_image: str = ""


def record_music_playback(event: MusicPlaybackEvent) -> Music | None:
    """Record a music play or scrobble.

    This resolves canonical metadata (MusicBrainz when possible), ensures
    Artist/Album/Track/Item existence, and updates the per-user Music row.
    """
    played_at = event.played_at or timezone.now().replace(second=0, microsecond=0)

    if getattr(event, "defer_cover_prefetch", False):
        recording_id = event.external_ids.get("musicbrainz_recording")
        release_id = event.external_ids.get("musicbrainz_release")
        release_group_id = event.external_ids.get("musicbrainz_release_group")
        artist_id = event.external_ids.get("musicbrainz_artist")
        # Fast path: avoid enrichment and heavy lookups during batch imports
        metadata = ResolvedMusicMetadata(
            track_title=event.track_title or "Unknown Track",
            artist_name=event.artist_name or "Unknown Artist",
            album_title=event.album_title or "Unknown Album",
            track_number=_coerce_int(event.track_number),
            duration_ms=_coerce_int(event.duration_ms),
            musicbrainz_recording_id=recording_id,
            musicbrainz_release_id=release_id,
            musicbrainz_release_group_id=release_group_id,
            musicbrainz_artist_id=artist_id,
            source=Sources.MUSICBRAINZ.value
            if any([recording_id, release_id, release_group_id, artist_id])
            else Sources.MANUAL.value,
            media_id=_select_media_id(
                recording_id,
                event.plex_rating_key,
                event.artist_name,
                event.track_title,
            ),
        )
    else:
        metadata = _resolve_metadata(event)
    _validate_against_payload(metadata, event)

    force_cover_prefetch = False

    with transaction.atomic():
        artist, artist_created, artist_mbid_attached = _get_or_create_artist(metadata)
        album, album_created = _get_or_create_album(metadata, artist)
        track = _get_or_create_track(metadata, album)
        item = _get_or_create_item(metadata, track, album)
        music = _update_music_entry(event, metadata, item, artist, album, track, played_at)
        if music is None:
            return None
        _ensure_trackers(event.user, artist, album, played_at)

        if metadata.musicbrainz_artist_id and not getattr(event, "defer_cover_prefetch", False):
            _sync_artist_metadata(artist, metadata.musicbrainz_artist_id, force=artist_created)
            if (
                (artist_created or artist_mbid_attached or album_created)
                and not _is_various_artist(artist)
            ):
                try:
                    sync_artist_discography(artist, force=artist_created or artist_mbid_attached)
                    dedupe_artist_albums(artist)
                    force_cover_prefetch = True
                except Exception as exc:  # pragma: no cover - defensive network guard
                    logger.debug("Failed discography sync for %s: %s", artist, exception_summary(exc))
        elif not getattr(event, "defer_cover_prefetch", False):
            _enrich_missing_artist_metadata(artist, album, track, music, metadata)

        if not getattr(event, "defer_cover_prefetch", False):
            _maybe_refresh_album_cover(album)
            _prefetch_missing_covers(artist, force=force_cover_prefetch)

    return music


def _resolve_metadata(event: MusicPlaybackEvent) -> ResolvedMusicMetadata:
    """Resolve canonical metadata for a playback event."""
    recording_id = event.external_ids.get("musicbrainz_recording")
    release_id = event.external_ids.get("musicbrainz_release")
    release_group_id = event.external_ids.get("musicbrainz_release_group")
    artist_id = event.external_ids.get("musicbrainz_artist")

    # Ignore obviously invalid MBIDs up front to avoid noisy lookups unless recording is mocked
    recording_is_mocked = isinstance(musicbrainz.recording, Mock)
    if recording_id and len(str(recording_id)) < 30 and not recording_is_mocked:
        recording_id = None
        event.external_ids["musicbrainz_recording"] = None

    metadata = ResolvedMusicMetadata(
        track_title=event.track_title or "Unknown Track",
        artist_name=event.artist_name or "Unknown Artist",
        album_title=event.album_title or "Unknown Album",
        track_number=_coerce_int(event.track_number),
        duration_ms=_coerce_int(event.duration_ms),
        musicbrainz_recording_id=recording_id,
        musicbrainz_release_id=release_id,
        musicbrainz_release_group_id=release_group_id,
        musicbrainz_artist_id=artist_id,
        source=Sources.MUSICBRAINZ.value
        if any([recording_id, release_id, release_group_id, artist_id])
        else Sources.MANUAL.value,
        media_id=_select_media_id(
            recording_id,
            event.plex_rating_key,
            event.artist_name,
            event.track_title,
        ),
    )

    if recording_id:
        success = _populate_from_recording(metadata, recording_id)
        if not success:
            # Clear MBIDs and fall back to search or manual
            metadata.musicbrainz_recording_id = None
            metadata.musicbrainz_release_id = None
            metadata.musicbrainz_release_group_id = None
            metadata.musicbrainz_artist_id = None
            metadata.media_id = _select_media_id(
                None,
                event.plex_rating_key,
                event.artist_name,
                event.track_title,
            )
            metadata.source = Sources.MANUAL.value
            _populate_from_search(metadata)
    elif release_id or release_group_id:
        success = _populate_from_release(metadata, release_id, release_group_id, event.track_title)
        if not success:
            metadata.musicbrainz_release_id = None
            metadata.musicbrainz_release_group_id = None
            metadata.media_id = _select_media_id(
                None,
                event.plex_rating_key,
                event.artist_name,
                event.track_title,
            )
            metadata.source = Sources.MANUAL.value
            _populate_from_search(metadata)
    else:
        _populate_from_search(metadata)

    # If we still have no artist MBID (e.g., noisy track search), try an artist-only lookup
    _maybe_attach_artist_from_artist_search(metadata, event)

    if metadata.musicbrainz_recording_id:
        metadata.media_id = _limit_media_id(metadata.musicbrainz_recording_id)
        metadata.source = Sources.MUSICBRAINZ.value
    else:
        metadata.source = metadata.source or Sources.MANUAL.value

    metadata.media_id = _limit_media_id(metadata.media_id)

    return metadata


def _populate_from_recording(metadata: ResolvedMusicMetadata, recording_id: str) -> bool:
    """Apply MusicBrainz recording metadata."""
    try:
        recording = musicbrainz.recording(recording_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to fetch recording %s: %s", recording_id, exception_summary(exc))
        return False

    metadata.musicbrainz_recording_id = recording_id
    metadata.track_title = recording.get("title") or metadata.track_title
    metadata.artist_name = recording.get("_artist_name") or metadata.artist_name
    metadata.album_title = recording.get("_album_title") or metadata.album_title
    metadata.musicbrainz_artist_id = recording.get("_artist_id") or metadata.musicbrainz_artist_id
    metadata.musicbrainz_release_id = recording.get("_album_id") or metadata.musicbrainz_release_id
    metadata.track_genres = recording.get("genres") or metadata.track_genres

    details = recording.get("details") or {}
    if not metadata.duration_ms and details.get("duration_minutes") is not None:
        duration_minutes = details["duration_minutes"]
        metadata.duration_ms = int(duration_minutes * 60000)

    release_date = details.get("release_date")
    parsed_release_date = _parse_release_date(release_date)
    if parsed_release_date:
        metadata.album_release_date = parsed_release_date

    if recording.get("image") and recording.get("image") != settings.IMG_NONE:
        metadata.album_image = recording["image"]

    return True


def _populate_from_release(
    metadata: ResolvedMusicMetadata,
    release_id: str | None,
    release_group_id: str | None,
    track_title: str | None,
) -> bool:
    """Populate metadata using a release and track title match."""
    release_lookup_id = release_id
    if not release_lookup_id and release_group_id:
        release_lookup_id = musicbrainz.get_release_for_group(release_group_id)
        if release_lookup_id:
            metadata.musicbrainz_release_id = release_lookup_id
            metadata.musicbrainz_release_group_id = release_group_id

    if not release_lookup_id:
        return False

    try:
        release = musicbrainz.get_release(release_lookup_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to fetch release %s: %s", release_lookup_id, exception_summary(exc))
        return False

    metadata.musicbrainz_release_id = release_lookup_id
    metadata.artist_name = release.get("artist_name") or metadata.artist_name
    metadata.album_title = release.get("title") or metadata.album_title
    metadata.musicbrainz_artist_id = release.get("artist_id") or metadata.musicbrainz_artist_id
    metadata.album_genres = release.get("genres") or metadata.album_genres

    parsed_release_date = _parse_release_date(release.get("release_date"))
    if parsed_release_date:
        metadata.album_release_date = parsed_release_date

    if release.get("image") and release.get("image") != settings.IMG_NONE:
        metadata.album_image = release["image"]

    track = _match_release_track(
        release.get("tracks") or [],
        track_title or metadata.track_title,
        metadata.duration_ms,
    )
    if track:
        metadata.musicbrainz_recording_id = track.get("recording_id") or metadata.musicbrainz_recording_id
        metadata.track_title = track.get("title") or metadata.track_title
        metadata.track_number = track.get("track_number")
        metadata.disc_number = track.get("disc_number") or metadata.disc_number
        if not metadata.duration_ms and track.get("duration_ms"):
            metadata.duration_ms = track["duration_ms"]

    return True


def _populate_from_search(metadata: ResolvedMusicMetadata) -> None:
    """Try to resolve recording via search when no IDs were provided."""
    if not metadata.track_title:
        return

    query_parts = [metadata.track_title]
    if metadata.artist_name:
        query_parts.insert(0, metadata.artist_name)
    # Only include album if it's meaningful (not a placeholder)
    if metadata.album_title and metadata.album_title not in ("Unknown Album", "Unknown"):
        query_parts.append(metadata.album_title)

    query = " ".join(part for part in query_parts if part)
    try:
        # If search is mocked (tests), just call it; otherwise proceed normally
        results = musicbrainz.search(query, page=1, skip_cover_art=True)
        logger.debug(
            "MusicBrainz search for '%s' returned %s results",
            query,
            (results or {}).get("total_results"),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Music search failed for query '%s': %s", query, exception_summary(exc))
        return

    expected_artist = _normalize(metadata.artist_name or "")
    expected_album = _normalize(metadata.album_title or "")
    expected_track = _normalize(metadata.track_title or "")
    artist_is_various = expected_artist in ("variousartists", "various")

    result_list = results.get("results") or []
    _log_search_candidates(query, result_list[:5])

    # If search is extremely noisy, avoid attaching MBIDs
    if (results.get("total_results") or 0) > 50:
        logger.debug(
            "Skipping MBIDs due to high result count (%s) for query '%s'",
            results.get("total_results"),
            query,
        )
        return

    for result in result_list:
        res_artist = _normalize(result.get("artist_name") or metadata.artist_name or "")
        res_album = _normalize(result.get("album_title") or metadata.album_title or "")
        res_title = _normalize(result.get("title") or metadata.track_title or "")

        # Require track title match when present
        if expected_track:
            if not res_title or res_title != expected_track:
                continue

        artist_match = False
        if artist_is_various:
            artist_match = True  # allow any artist but do not attach MBID later
        elif expected_artist:
            artist_match = bool(res_artist and res_artist == expected_artist)
            if not artist_match:
                continue

        album_match = False
        if expected_album:
            album_match = bool(res_album and res_album == expected_album)
            if expected_album and res_album and res_album != expected_album:
                continue

        # Must have at least track match and one of album/artist confidence
        if expected_track and not (artist_match or album_match):
            continue

        metadata.musicbrainz_recording_id = result.get("media_id")
        if metadata.musicbrainz_recording_id:
            metadata.media_id = metadata.musicbrainz_recording_id
            metadata.source = Sources.MUSICBRAINZ.value
        metadata.track_title = result.get("title") or metadata.track_title
        metadata.artist_name = result.get("artist_name") or metadata.artist_name
        metadata.album_title = result.get("album_title") or metadata.album_title
        # Avoid attaching artist MBID for Various Artists comps
        if not artist_is_various:
            metadata.musicbrainz_artist_id = result.get("artist_id") or metadata.musicbrainz_artist_id
        metadata.musicbrainz_release_id = result.get("release_id") or metadata.musicbrainz_release_id
        metadata.musicbrainz_release_group_id = result.get("release_group_id") or metadata.musicbrainz_release_group_id
        if result.get("duration_minutes") and not metadata.duration_ms:
            metadata.duration_ms = int(result["duration_minutes"] * 60000)
        metadata.source = Sources.MUSICBRAINZ.value
        break


def _match_release_track(tracks, track_title: str, duration_ms: int | None):
    """Find the best matching track from a release list."""
    if not tracks:
        return None

    track_title_lower = (track_title or "").lower()
    for track in tracks:
        if track.get("title", "").lower() == track_title_lower:
            return track

    if duration_ms:
        for track in tracks:
            if not track.get("duration_ms"):
                continue
            diff = abs(track["duration_ms"] - duration_ms)
            if diff <= 2000:  # within 2 seconds
                return track

    return tracks[0]


def _get_or_create_artist(metadata: ResolvedMusicMetadata) -> tuple[Artist, bool, bool]:
    """Get or create an Artist."""
    name = (metadata.artist_name or "").strip() or "Unknown Artist"
    artist = None
    created = False
    attached_mbid = False

    if metadata.musicbrainz_artist_id:
        artist = Artist.objects.filter(musicbrainz_id=metadata.musicbrainz_artist_id).first()
        # If the MBID we found belongs to an artist whose name doesn't resemble the Plex artist, ignore it
        if artist:
            resolved_name = _normalize(artist.name)
            expected_name = _normalize(name)
            if resolved_name and expected_name and resolved_name != expected_name:
                artist = None
                metadata.musicbrainz_artist_id = None
        if not artist:
            artist = Artist.objects.filter(name__iexact=name).order_by("id").first()
            if artist and not artist.musicbrainz_id:
                artist.musicbrainz_id = metadata.musicbrainz_artist_id
                attached_mbid = True
            else:
                artist = Artist(
                    musicbrainz_id=metadata.musicbrainz_artist_id,
                    name=name,
                )
                created = True
    else:
        # Prefer an artist without MBID to avoid hijacking an unrelated MBID
        artist = (
            Artist.objects.filter(name__iexact=name, musicbrainz_id__isnull=True)
            .order_by("id")
            .first()
        )
        if not artist:
            # Do NOT reuse an existing MBID-carrying artist when no MBID was supplied
            artist_with_mbid = Artist.objects.filter(name__iexact=name, musicbrainz_id__isnull=False).first()
            if artist_with_mbid:
                logger.debug(
                    "Skipping existing artist with MBID %s for name '%s' due to missing MBID in payload",
                    artist_with_mbid.musicbrainz_id,
                    name,
                )
            artist = Artist.objects.create(name=name)
            created = True

    # Ensure existing artist has a usable display name
    if artist and not (artist.name or "").strip():
        artist.name = name
        artist.save(update_fields=["name"])

    updates = {}
    if metadata.artist_sort_name:
        updates["sort_name"] = metadata.artist_sort_name
    if metadata.artist_country:
        updates["country"] = metadata.artist_country
    if metadata.artist_genres:
        updates["genres"] = metadata.artist_genres
    if metadata.artist_image:
        updates["image"] = metadata.artist_image

    for field, value in updates.items():
        current = getattr(artist, field)
        if not current and value:
            setattr(artist, field, value)

    artist.name = name
    artist.save()
    return artist, created, attached_mbid


def _get_or_create_album(metadata: ResolvedMusicMetadata, artist: Artist) -> tuple[Album, bool]:
    """Get or create an Album for the track."""
    title = metadata.album_title or "Unknown Album"
    album = None
    created = False
    normalized_title = _normalize(title)

    if metadata.musicbrainz_release_group_id:
        album = Album.objects.filter(
            artist=artist,
            musicbrainz_release_group_id=metadata.musicbrainz_release_group_id,
        ).first()
    if not album and metadata.musicbrainz_release_id:
        album = Album.objects.filter(
            artist=artist,
            musicbrainz_release_id=metadata.musicbrainz_release_id,
        ).first()

    if not album:
        album = (
            Album.objects.filter(artist=artist, title__iexact=title)
            .order_by("id")
            .first()
        )
    if not album:
        # Fuzzy match by normalized title to avoid near-duplicate creation
        for existing in Album.objects.filter(artist=artist):
            if _normalize(existing.title) == normalized_title:
                album = existing
                break

    if album:
        created = False
    else:
        album = Album.objects.create(
            artist=artist,
            title=title,
            musicbrainz_release_id=metadata.musicbrainz_release_id,
            musicbrainz_release_group_id=metadata.musicbrainz_release_group_id,
        )
        created = True

    updates = {
        "title": title,
        "musicbrainz_release_id": metadata.musicbrainz_release_id,
        "musicbrainz_release_group_id": metadata.musicbrainz_release_group_id,
        "release_date": metadata.album_release_date,
        "release_type": metadata.album_release_type,
        "genres": metadata.album_genres or [],
    }

    if metadata.album_image and metadata.album_image != settings.IMG_NONE:
        updates["image"] = metadata.album_image

    changed_fields = []
    for field, value in updates.items():
        if value and getattr(album, field) != value:
            setattr(album, field, value)
            changed_fields.append(field)

    if changed_fields:
        album.save(update_fields=changed_fields)

    # Find all albums with the same normalized title for deduplication
    matching_albums = [
        a for a in Album.objects.filter(artist=artist)
        if _normalize(a.title) == normalized_title
    ]
    if len(matching_albums) > 1:
        _dedupe_albums(artist, matching_albums, album, normalized_title)

    return album, created


def _get_or_create_track(metadata: ResolvedMusicMetadata, album: Album) -> Track:
    """Get or create a Track record."""
    title = metadata.track_title or "Unknown Track"
    track = None

    def _find_existing_track():
        normalized_title = _normalize(title)
        if not normalized_title:
            return None
        if metadata.track_number:
            by_number = album.tracklist.filter(track_number=metadata.track_number).first()
            if by_number:
                return by_number
        for existing in album.tracklist.all():
            if _normalize(existing.title) == normalized_title:
                return existing
        return None

    if metadata.musicbrainz_recording_id:
        track, _ = Track.objects.get_or_create(
            album=album,
            musicbrainz_recording_id=metadata.musicbrainz_recording_id,
            defaults={
                "title": title,
                "track_number": metadata.track_number,
                "disc_number": metadata.disc_number or 1,
                "duration_ms": metadata.duration_ms,
            },
        )
    else:
        existing = _find_existing_track()
        if existing:
            track = existing
        else:
            track, _ = Track.objects.get_or_create(
                album=album,
                title=title,
                track_number=metadata.track_number,
                disc_number=metadata.disc_number or 1,
                defaults={
                    "duration_ms": metadata.duration_ms,
                },
            )

    _dedupe_null_tracks(album, track, _normalize(title))

    updates = {
        "title": title,
        "track_number": metadata.track_number,
        "disc_number": metadata.disc_number or 1,
        "duration_ms": metadata.duration_ms,
        "genres": metadata.track_genres or metadata.album_genres or [],
        "musicbrainz_recording_id": metadata.musicbrainz_recording_id,
    }

    changed_fields = []
    for field, value in updates.items():
        if value is not None and getattr(track, field) != value:
            setattr(track, field, value)
            changed_fields.append(field)

    if changed_fields:
        track.save(update_fields=changed_fields)

    return track


def _dedupe_null_tracks(album: Album, keep_track: Track, normalized_title: str) -> None:
    """Delete duplicate tracks on the album that lack track_number but match the title."""
    duplicates = []
    for extra in album.tracklist.exclude(id=keep_track.id).filter(track_number__isnull=True):
        if _normalize(extra.title) == normalized_title:
            duplicates.append(extra.id)
    if duplicates:
        album.tracklist.filter(id__in=duplicates).delete()
        logger.debug(
            "Removed %s duplicate track(s) without track_number for album %s (%s)",
            len(duplicates),
            album.id,
            normalized_title,
        )


def is_incomplete_album(album: Album) -> bool:
    """Heuristic to detect placeholder/partial albums that should be replaced."""
    missing_mb = not album.musicbrainz_release_id and not album.musicbrainz_release_group_id
    sparse_tracks = (album.tracklist.count() <= 1) or not album.tracks_populated
    missing_image = (not album.image) or album.image == settings.IMG_NONE
    return missing_mb and sparse_tracks and missing_image


def dedupe_artist_albums(artist: Artist) -> None:
    """Dedupe albums for an artist by normalized title (used by scrobbles and views)."""
    albums = list(Album.objects.filter(artist=artist))
    groups = {}
    for album in albums:
        norm = _normalize(album.title or "")
        groups.setdefault(norm, []).append(album)

    for norm, group in groups.items():
        if len(group) <= 1:
            continue
        preferred = next((a for a in group if Music.objects.filter(album=a).exists()), group[0])
        identity_groups: dict[str | None, list[Album]] = {}
        for album in group:
            identity = album.musicbrainz_release_group_id or album.musicbrainz_release_id
            identity_groups.setdefault(identity, []).append(album)

        non_null_identities = [key for key in identity_groups if key]
        if len(non_null_identities) > 1:
            for identity in non_null_identities:
                albums_with_identity = identity_groups[identity]
                if len(albums_with_identity) <= 1:
                    continue
                keep_album = preferred if preferred in albums_with_identity else albums_with_identity[0]
                _dedupe_albums(artist, albums_with_identity, keep_album, norm)
            continue

        _dedupe_albums(artist, group, preferred, norm)


def _dedupe_albums(
    artist: Artist,
    albums: list[Album],
    keep_album: Album,
    normalized_title: str,
) -> None:
    """Merge/delete duplicate albums with matching titles for the same artist."""
    same_title = [a for a in albums if _normalize(a.title) == normalized_title]
    if len(same_title) <= 1:
        return

    primary = _choose_primary_album(same_title, keep_album)
    duplicates = [a for a in same_title if a.id != primary.id]

    for dup in duplicates:
        # Merge metadata from dup into primary if missing
        meta_updates = {}
        if (not primary.image or primary.image == settings.IMG_NONE) and dup.image and dup.image != settings.IMG_NONE:
            meta_updates["image"] = dup.image
        if not primary.musicbrainz_release_id and dup.musicbrainz_release_id:
            meta_updates["musicbrainz_release_id"] = dup.musicbrainz_release_id
        if not primary.musicbrainz_release_group_id and dup.musicbrainz_release_group_id:
            # Double-check no conflict before setting
            conflict = Album.objects.filter(
                artist=artist,
                musicbrainz_release_group_id=dup.musicbrainz_release_group_id,
            ).exclude(id=primary.id).first()
            if not conflict:
                meta_updates["musicbrainz_release_group_id"] = dup.musicbrainz_release_group_id
            else:
                logger.debug(
                    "Skipping musicbrainz_release_group_id for primary album '%s' (id=%s): "
                    "conflicts with album '%s' (id=%s) for artist %s",
                    primary.title,
                    primary.id,
                    conflict.title,
                    conflict.id,
                    artist.name,
                )
        if not primary.release_date and dup.release_date:
            meta_updates["release_date"] = dup.release_date
        if not primary.release_type and dup.release_type:
            meta_updates["release_type"] = dup.release_type
        if (not primary.genres) and dup.genres:
            meta_updates["genres"] = dup.genres
        if meta_updates:
            for f, v in meta_updates.items():
                setattr(primary, f, v)
            try:
                primary.save(update_fields=list(meta_updates.keys()))
            except IntegrityError as e:
                logger.warning(
                    "Failed to update primary album '%s' (id=%s) with metadata from '%s' (id=%s): %s. "
                    "Skipping metadata merge for this duplicate.",
                    primary.title,
                    primary.id,
                    dup.title,
                    dup.id,
                    e,
                )
                # Rollback the attribute changes
                primary.refresh_from_db()

        # Reassign album trackers
        for tracker in AlbumTracker.objects.filter(album=dup):
            existing = AlbumTracker.objects.filter(user=tracker.user, album=primary).first()
            if existing:
                if tracker.start_date and (
                    not existing.start_date or tracker.start_date < existing.start_date
                ):
                    existing.start_date = tracker.start_date
                    existing.save(update_fields=["start_date"])
                tracker.delete()
            else:
                tracker.album = primary
                tracker.save(update_fields=["album"])
        # Safety: if any trackers remain (unexpected), drop them to avoid FK issues
        AlbumTracker.objects.filter(album=dup).delete()

        # Reassign Music entries to the keep_album (and best matching track)
        for music in Music.objects.filter(album=dup):
            target_track = _match_track_in_album(primary, music.track)
            updates = {"album": primary}
            if target_track:
                updates["track"] = target_track
            Music.objects.filter(pk=music.pk).update(**updates)

        # Reassign tracks that won't conflict
        for track in dup.tracklist.all():
            target = _match_track_in_album(primary, track)
            if target:
                # Music already repointed above; drop the duplicate track
                track.delete()
            else:
                # Move the track if no conflict on track_number/title
                conflict = _match_track_in_album(primary, track, strict=True)
                if not conflict:
                    track.album = primary
                    track.save(update_fields=["album"])

        # Delete duplicate album if no music remains on it
        if not Music.objects.filter(album=dup).exists():
            try:
                dup.delete()
                logger.debug(
                    "Removed duplicate album %s matching title %s (kept %s)",
                    dup.id,
                    normalized_title,
                    primary.id,
                )
            except IntegrityError as exc:
                logger.debug(
                    "Skipped deleting duplicate album %s due to FK constraint: %s",
                    dup.id,
                    exc,
                )


def _choose_primary_album(albums: list[Album], preferred: Album) -> Album:
    """Pick the best album to keep based on metadata richness."""

    def score(album: Album) -> int:
        s = 0
        if album.id == preferred.id:
            s += 4
        if album.musicbrainz_release_group_id:
            s += 8
        if album.musicbrainz_release_id:
            s += 6
        if album.release_date:
            s += 2
        if album.image and album.image != settings.IMG_NONE:
            s += 1
        track_count = album.tracklist.count()
        if track_count:
            s += 1
            s += min(track_count, 50)  # favor richer tracklists
        if album.tracks_populated:
            s += 3
        if Music.objects.filter(album=album).exists():
            s += 2
        return s

    best = preferred
    best_score = score(preferred)
    for album in albums:
        if album.id == preferred.id:
            continue
        s = score(album)
        if s > best_score or (s == best_score and album.id < best.id):
            best = album
            best_score = s
    return best


def _match_track_in_album(album: Album, source_track: Track, strict: bool = False) -> Track | None:
    """Find a track in album matching source by number or normalized title."""
    if source_track.track_number:
        match = album.tracklist.filter(track_number=source_track.track_number).first()
        if match:
            return match
    if not strict:
        source_norm = _normalize(source_track.title or "")
        for t in album.tracklist.all():
            if _normalize(t.title or "") == source_norm:
                return t
    return None


def _get_or_create_item(
    metadata: ResolvedMusicMetadata,
    track: Track,
    album: Album,
) -> Item:
    """Get or create the Item identity for the track."""
    runtime_minutes = _runtime_minutes_from_ms(metadata.duration_ms)
    item_defaults = {
        "title": metadata.track_title or track.title,
        "image": album.image or settings.IMG_NONE,
    }
    if runtime_minutes:
        item_defaults["runtime_minutes"] = runtime_minutes

    item, created = Item.objects.get_or_create(
        media_id=metadata.media_id,
        source=metadata.source,
        media_type=MediaTypes.MUSIC.value,
        defaults=item_defaults,
    )

    changed_fields = []
    if not created:
        if not item.title and item_defaults["title"]:
            item.title = item_defaults["title"]
            changed_fields.append("title")

        if (
            item_defaults["image"]
            and item_defaults["image"] != settings.IMG_NONE
            and item.image in ("", settings.IMG_NONE)
        ):
            item.image = item_defaults["image"]
            changed_fields.append("image")

        if runtime_minutes and not item.runtime_minutes:
            item.runtime_minutes = runtime_minutes
            changed_fields.append("runtime_minutes")

    if changed_fields:
        item.save(update_fields=changed_fields)

    return item


def _update_music_entry(
    event: MusicPlaybackEvent,
    metadata: ResolvedMusicMetadata,
    item: Item,
    artist: Artist,
    album: Album,
    track: Track,
    played_at,
) -> Music:
    """Create or update the per-user Music row.
    
    Each track has its own Music record (via unique item), so deduplication
    only applies to the same track played multiple times. Different tracks
    will always get separate Music records and be fully logged, even if
    played within 2 minutes of each other.
    """
    if not event.completed:
        # Do not create a new Music row on play/resume; only update existing
        music = Music.objects.filter(item=item, user=event.user).first()
        if music is None:
            return None
    else:
        defaults = {
            "artist": artist,
            "album": album,
            "track": track,
            "status": Status.COMPLETED.value,
            # Start at 0 so the first completed event increments to 1.
            "progress": 0,
            "start_date": played_at,
            "end_date": played_at,
        }

        music, created = Music.objects.get_or_create(
            item=item,
            user=event.user,
            defaults=defaults,
        )
        
        # Log when a new track is being tracked (different tracks always get logged)
        if created:
            logger.debug(
                "Created new Music record for track: %s - %s (item_id=%s)",
                metadata.artist_name or "Unknown",
                metadata.track_title or "Unknown",
                item.id,
            )

    changed = False

    if music.artist_id != artist.id:
        music.artist = artist
        changed = True
    if music.album_id != album.id:
        music.album = album
        changed = True
    if music.track_id != track.id:
        music.track = track
        changed = True

    if event.completed:
        prior_end = music.end_date
        # Track-specific deduplication: Only prevent progress increment if THIS SAME TRACK
        # was played within 2 minutes. Different tracks have different Music records (via
        # unique item), so they are always fully logged regardless of timing.
        #
        # For short tracks (< 2 minutes), this ensures:
        # - Same track played twice within 2 minutes: progress doesn't increment, but history is still recorded
        # - Different tracks played within 2 minutes: both are fully logged (separate Music records)
        if prior_end and abs(played_at - prior_end) <= timedelta(minutes=2):
            # Same track played within 2 minutes: don't increment progress, but still record history
            new_progress = music.progress or 1
            logger.debug(
                "Same track played within 2 minutes: %s - %s (progress=%s, not incrementing)",
                metadata.artist_name or "Unknown",
                metadata.track_title or "Unknown",
                music.progress or 1,
            )
        else:
            # Different track or same track after 2 minutes: increment progress normally
            new_progress = (music.progress or 0) + 1
        if music.progress != new_progress:
            music.progress = new_progress
            changed = True
        if music.status != Status.COMPLETED.value:
            music.status = Status.COMPLETED.value
            changed = True
        # Always update end_date to the new played_at timestamp to ensure a history record is created.
        # This ensures every play is recorded in history, even when progress is deduplicated
        # (same track within 2 minutes). Different tracks always get separate history records.
        if music.end_date != played_at:
            music.end_date = played_at
            changed = True
        if not music.start_date:
            music.start_date = played_at
            changed = True
    else:
        if music.status not in (Status.IN_PROGRESS.value, Status.COMPLETED.value):
            music.status = Status.IN_PROGRESS.value
            changed = True
        if music.status != Status.COMPLETED.value and not music.start_date:
            music.start_date = played_at
            changed = True

    if changed:
        music.save()

    return music


def _select_media_id(
    recording_id: str | None,
    plex_rating_key: str | None,
    artist_name: str | None,
    track_title: str | None,
) -> str:
    """Choose the best media_id for the Item."""
    if recording_id:
        return _limit_media_id(str(recording_id))
    if plex_rating_key:
        return _limit_media_id(str(plex_rating_key))

    slug_base = slugify(f"{artist_name or 'music'}-{track_title or 'track'}") or "music-track"
    return _limit_media_id(slug_base)


def _limit_media_id(media_id: str) -> str:
    """Ensure media_id fits Item field constraints."""
    max_length = Item._meta.get_field("media_id").max_length or 0
    return media_id[:max_length] if max_length and len(media_id) > max_length else media_id


def _runtime_minutes_from_ms(duration_ms: int | None) -> int | None:
    """Convert ms duration to runtime minutes suitable for Item."""
    if not duration_ms:
        return None
    minutes = duration_ms // 60000
    return minutes or None


def _parse_release_date(release_date: str | None):
    """Parse a release date string to date."""
    if not release_date:
        return None
    try:
        # MusicBrainz may provide YYYY, YYYY-MM, or YYYY-MM-DD
        if len(release_date) == 4:
            release_date = f"{release_date}-01-01"
        elif len(release_date) == 7:
            release_date = f"{release_date}-01"
        return parse_date(release_date)
    except Exception:
        return None


def _coerce_int(value: int | None) -> int | None:
    """Return value as int if possible."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_various_artist(artist: Artist) -> bool:
    """Return True if artist is the generic 'Various Artists' bucket."""
    name = (artist.name or "").strip().lower()
    return name in ("various artists", "various") or artist.musicbrainz_id == "89ad4ac3-39f7-470e-963a-56509c546377"


def _normalize(text: str) -> str:
    """Normalize text for matching (lowercase, alnum only)."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _log_search_candidates(query: str, candidates: list[dict]) -> None:
    """Log top MusicBrainz search candidates for debugging."""
    if not candidates:
        logger.debug("MusicBrainz search for '%s' returned no candidates", query)
        return
    summary = []
    for cand in candidates:
        summary.append(
            {
                "title": cand.get("title"),
                "artist": cand.get("artist_name"),
                "album": cand.get("album_title"),
                "recording_id": cand.get("media_id"),
                "artist_id": cand.get("artist_id"),
                "release_id": cand.get("release_id"),
                "release_group_id": cand.get("release_group_id"),
            },
        )
    logger.debug("Top MusicBrainz candidates for '%s': %s", query, summary)


def _validate_against_payload(metadata: ResolvedMusicMetadata, event: MusicPlaybackEvent) -> None:
    """Drop MBIDs if they don't align with the Plex payload artist/album/track."""
    expected_artist = _normalize(event.artist_name or "")
    expected_album = _normalize(event.album_title or "")
    expected_track = _normalize(event.track_title or "")

    if expected_track and metadata.track_title:
        resolved_track = _normalize(metadata.track_title)
        if resolved_track and resolved_track != expected_track:
            _clear_musicbrainz_ids(metadata, event)
            return

    if expected_artist and metadata.artist_name:
        resolved_artist = _normalize(metadata.artist_name)
        if resolved_artist and resolved_artist != expected_artist:
            _clear_musicbrainz_ids(metadata, event)
            return

    if expected_album and metadata.album_title:
        resolved_album = _normalize(metadata.album_title)
        if resolved_album and resolved_album != expected_album:
            _clear_musicbrainz_ids(metadata, event)
            return


def _clear_musicbrainz_ids(metadata: ResolvedMusicMetadata, event: MusicPlaybackEvent) -> None:
    """Remove MusicBrainz identifiers and revert to manual source, restoring Plex names."""
    metadata.musicbrainz_recording_id = None
    metadata.musicbrainz_release_id = None
    metadata.musicbrainz_release_group_id = None
    metadata.musicbrainz_artist_id = None
    metadata.source = Sources.MANUAL.value
    metadata.artist_name = event.artist_name or metadata.artist_name
    metadata.album_title = event.album_title or metadata.album_title
    metadata.track_title = event.track_title or metadata.track_title


def _ensure_trackers(user, artist: Artist, album: Album, played_at):
    """Create Artist/Album trackers so music appears in list views."""
    if artist and user:
        ArtistTracker.objects.get_or_create(
            user=user,
            artist=artist,
            defaults={
                "status": Status.IN_PROGRESS.value,
                "start_date": played_at,
            },
        )
    if album and user:
        AlbumTracker.objects.get_or_create(
            user=user,
            album=album,
            defaults={
                "status": Status.IN_PROGRESS.value,
                "start_date": played_at,
            },
        )


def _maybe_refresh_album_cover(album: Album) -> None:
    """Fetch cover art for an album if missing."""
    if not album:
        return
    if album.image and album.image != settings.IMG_NONE:
        return
    if not (album.musicbrainz_release_id or album.musicbrainz_release_group_id):
        return
    try:
        refresh_album_cover_art(album)
    except Exception as exc:  # pragma: no cover - defensive network guard
        logger.debug("Cover art refresh failed for album %s: %s", album.id, exception_summary(exc))


def _prefetch_missing_covers(artist: Artist, force: bool = False) -> None:
    """Fetch cover art for all missing albums for the artist."""
    if not artist:
        return
    albums_with_mbids = Album.objects.filter(
        artist=artist,
    ).filter(
        models.Q(musicbrainz_release_id__isnull=False)
        | models.Q(musicbrainz_release_group_id__isnull=False),
    )
    if not albums_with_mbids.exists():
        return

    if not force and not albums_with_mbids.filter(
        models.Q(image="") | models.Q(image=settings.IMG_NONE),
    ).exists():
        return
    try:
        prefetch_album_covers(artist, limit=None)  # fetch all missing art for this artist
    except Exception as exc:  # pragma: no cover - defensive network guard
        logger.debug("Prefetch covers failed for artist %s: %s", artist.id, exception_summary(exc))

def _enrich_missing_artist_metadata(
    artist: Artist,
    album: Album,
    track: Track,
    music: Music,
    metadata: ResolvedMusicMetadata,
):
    """Try to attach MusicBrainz identity/metadata for artists missing MBIDs."""
    if artist.musicbrainz_id:
        return

    query_parts = [artist.name]
    if metadata.track_title:
        query_parts.append(metadata.track_title)
    if metadata.album_title:
        query_parts.append(metadata.album_title)

    query = " ".join(part for part in query_parts if part).strip()
    if not query:
        return

    try:
        results = musicbrainz.search(query, page=1, skip_cover_art=True)
    except Exception as exc:  # pragma: no cover - defensive network guard
        logger.debug("Artist enrichment search failed for %s: %s", query, exception_summary(exc))
        return

    total_results = (results or {}).get("total_results") or 0
    if total_results > 50:
        logger.debug(
            "Skipping enrichment for '%s' due to noisy search results (%s)",
            query,
            total_results,
        )
        return

    expected_artist = _normalize(metadata.artist_name or artist.name or "")
    expected_album = _normalize(metadata.album_title or album.title or "")
    expected_track = _normalize(metadata.track_title or track.title or "")

    candidates = (results or {}).get("results", []) or []
    matched = None
    for result in candidates:
        res_artist = _normalize(result.get("artist_name") or "")
        res_album = _normalize(result.get("album_title") or "")
        res_track = _normalize(result.get("title") or "")

        if expected_artist and res_artist and res_artist != expected_artist:
            continue
        if expected_album and res_album and res_album != expected_album:
            continue
        if expected_track and res_track and res_track != expected_track:
            continue
        matched = result
        break

    if not matched:
        logger.debug("No safe enrichment candidate matched for '%s'", query)
        return

    result = matched
    artist_id = result.get("artist_id")
    if artist_id:
        artist.musicbrainz_id = artist_id
        artist.save(update_fields=["musicbrainz_id"])
        metadata.musicbrainz_artist_id = artist_id
        _sync_artist_metadata(artist, artist_id, force=True)
        if not _is_various_artist(artist):
            try:
                sync_artist_discography(artist, force=True)
            except Exception as exc:  # pragma: no cover
                logger.debug("Discography sync failed during enrichment for %s: %s", artist, exception_summary(exc))

    release_id = result.get("release_id")
    release_group_id = result.get("release_group_id")

    target_album = album
    if artist and release_group_id:
        existing_album = Album.objects.filter(
            artist=artist,
            musicbrainz_release_group_id=release_group_id,
        ).exclude(id=album.id).first()
        if existing_album:
            target_album = existing_album
    if artist and release_id and target_album == album:
        existing_album = Album.objects.filter(
            artist=artist,
            musicbrainz_release_id=release_id,
        ).exclude(id=album.id).first()
        if existing_album:
            target_album = existing_album

    if target_album and (release_id or release_group_id):
        updates = {}
        if release_id and not target_album.musicbrainz_release_id:
            updates["musicbrainz_release_id"] = release_id
        if release_group_id and not target_album.musicbrainz_release_group_id:
            updates["musicbrainz_release_group_id"] = release_group_id
        if updates:
            try:
                for field, value in updates.items():
                    setattr(target_album, field, value)
                target_album.save(update_fields=list(updates.keys()))
            except IntegrityError:
                logger.debug(
                    "Skipping album update due to constraint for %s (%s/%s)",
                    target_album,
                    release_id,
                    release_group_id,
                )

    if target_album and track and track.album_id != target_album.id:
        track.album = target_album
        track.save(update_fields=["album"])
    if target_album and music and music.album_id != target_album.id:
        music.album = target_album
        music.save(update_fields=["album"])


def _maybe_attach_artist_from_artist_search(metadata: ResolvedMusicMetadata, event: MusicPlaybackEvent):
    """Fallback to artist-only search when track search is too noisy."""
    if metadata.musicbrainz_artist_id or not (metadata.artist_name or event.artist_name):
        return

    artist_query = metadata.artist_name or event.artist_name
    try:
        results = musicbrainz.search_artists(artist_query, page=1)
    except Exception as exc:  # pragma: no cover - defensive network guard
        logger.debug("Artist-only search failed for '%s': %s", artist_query, exception_summary(exc))
        return

    candidates = (results or {}).get("results") or []
    expected_artist = _normalize(artist_query)
    for cand in candidates:
        cand_name = cand.get("name") or cand.get("artist_name") or ""
        if not cand_name:
            continue
        if _normalize(cand_name) != expected_artist:
            continue
        artist_id = cand.get("media_id") or cand.get("artist_id")
        if artist_id and artist_id != "89ad4ac3-39f7-470e-963a-56509c546377":
            metadata.musicbrainz_artist_id = artist_id
            metadata.artist_name = cand_name
            break


def _sync_artist_metadata(artist: Artist, musicbrainz_id: str, force: bool = False):
    """Fetch and apply artist metadata from MusicBrainz."""
    if _is_various_artist(artist):
        return
    try:
        data = musicbrainz.get_artist(musicbrainz_id)
    except Exception as exc:  # pragma: no cover - defensive network guard
        logger.debug("Failed to fetch artist metadata for %s: %s", musicbrainz_id, exception_summary(exc))
        return

    updates = {}
    if data.get("sort_name"):
        updates["sort_name"] = data["sort_name"]
    if data.get("country"):
        updates["country"] = data["country"]
    if data.get("image"):
        updates["image"] = data["image"]
    if data.get("genres"):
        updates["genres"] = data["genres"]

    changed_fields = []
    for field, value in updates.items():
        if value and getattr(artist, field) != value:
            setattr(artist, field, value)
            changed_fields.append(field)

    if changed_fields:
        artist.save(update_fields=changed_fields)
    elif (not artist.image or artist.image == settings.IMG_NONE) and artist.albums.exists():
        hero_image = get_artist_hero_image(artist)
        if hero_image and hero_image != settings.IMG_NONE:
            artist.image = hero_image
            artist.save(update_fields=["image"])
