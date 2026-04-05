"""Shared item metadata hydration for tracked saves and Discover actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date

from app import credits, helpers, metadata_utils
from app import statistics as stats
from app.models import Album, Artist, Item, MediaTypes, PodcastShow, Sources, Track
from app.providers import pocketcasts, services
from app.services.metadata_resolution import (
    get_library_media_type,
    get_tracking_media_type,
    upsert_provider_links,
)


@dataclass(slots=True)
class HydratedItemResult:
    """Shared hydration result used by tracked save flows."""

    item: Item
    metadata: dict
    created: bool
    artist: Artist | None = None
    album: Album | None = None
    track: Track | None = None
    podcast_show: PodcastShow | None = None


def _coerce_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_list(value) -> list:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _parse_release_date_str(release_date_value):
    if not release_date_value:
        return None
    if isinstance(release_date_value, date):
        return release_date_value
    if hasattr(release_date_value, "date"):
        return release_date_value.date()
    if isinstance(release_date_value, str):
        return parse_date(release_date_value[:10])
    return None


def _fallback_metadata(
    metadata: dict | None,
    *,
    media_type: str,
    source: str,
    fallback_title: str = "",
    fallback_image: str | None = None,
    fallback_release_date: str | None = None,
) -> dict:
    normalized = dict(metadata or {})
    normalized.setdefault("details", {})
    normalized.setdefault("related", {})
    normalized.setdefault("source", source)
    normalized.setdefault("media_type", media_type)

    title_fields = Item.title_fields_from_metadata(normalized, fallback_title=fallback_title)
    normalized.update(title_fields)
    if not normalized.get("image"):
        normalized["image"] = fallback_image or settings.IMG_NONE
    if fallback_release_date and not normalized.get("release_date"):
        normalized["release_date"] = fallback_release_date
    return normalized


def _enrich_podcast_lookup(metadata: dict, media_id: str) -> tuple[dict, PodcastShow | None]:
    if not str(media_id).isdigit():
        return metadata, None

    try:
        lookup = pocketcasts.lookup_by_itunes_id(media_id)
    except Exception:
        return metadata, None

    details = dict(metadata.get("details") or {})
    if lookup.get("language") and not details.get("language"):
        details["language"] = lookup["language"]
    if lookup.get("author") and not details.get("author"):
        details["author"] = lookup["author"]

    enriched = dict(metadata)
    enriched["details"] = details
    if not enriched.get("title") and lookup.get("title"):
        enriched["title"] = lookup["title"]
    if not enriched.get("image") and lookup.get("artwork_url"):
        enriched["image"] = lookup["artwork_url"]
    if not enriched.get("genres") and lookup.get("genres"):
        enriched["genres"] = list(lookup["genres"])

    podcast_uuid = f"itunes:{media_id}"
    rss_feed_url = lookup.get("feed_url", "")
    show = None
    if rss_feed_url:
        show = PodcastShow.objects.filter(rss_feed_url=rss_feed_url).first()
    if show is None:
        show, _ = PodcastShow.objects.get_or_create(
            podcast_uuid=podcast_uuid,
            defaults={
                "title": lookup.get("title", "") or enriched.get("title", "") or f"Podcast {media_id}",
                "author": lookup.get("author", ""),
                "image": lookup.get("artwork_url", "") or enriched.get("image", ""),
                "description": lookup.get("description", ""),
                "genres": list(lookup.get("genres") or []),
                "language": lookup.get("language", ""),
                "rss_feed_url": rss_feed_url,
            },
        )

    update_fields: list[str] = []
    if lookup.get("title") and not show.title:
        show.title = lookup["title"]
        update_fields.append("title")
    if lookup.get("author") and not show.author:
        show.author = lookup["author"]
        update_fields.append("author")
    if lookup.get("artwork_url") and not show.image:
        show.image = lookup["artwork_url"]
        update_fields.append("image")
    if lookup.get("description") and not show.description:
        show.description = lookup["description"]
        update_fields.append("description")
    if lookup.get("genres") and not show.genres:
        show.genres = list(lookup["genres"])
        update_fields.append("genres")
    if lookup.get("language") and not show.language:
        show.language = lookup["language"]
        update_fields.append("language")
    if rss_feed_url and not show.rss_feed_url:
        show.rss_feed_url = rss_feed_url
        update_fields.append("rss_feed_url")
    if update_fields:
        show.save(update_fields=update_fields)

    return enriched, show


def _hydrate_music_relations(
    media_id: str,
    metadata: dict,
) -> tuple[Artist | None, Album | None, Track | None]:
    artist_instance = None
    album_instance = None
    track_instance = None
    track_genres = metadata.get("genres", [])

    details = metadata.get("details", {})
    if not isinstance(details, dict):
        details = {}

    artist_id = metadata.get("_artist_id") or details.get("artist_id")
    artist_name = metadata.get("_artist_name") or details.get("artist")
    if artist_id and artist_name:
        artist_instance, _ = Artist.objects.get_or_create(
            musicbrainz_id=artist_id,
            defaults={"name": artist_name},
        )
    elif artist_name:
        artist_instance = Artist.objects.filter(name=artist_name).first()
        if artist_instance is None:
            artist_instance = Artist.objects.create(name=artist_name)

    album_id = metadata.get("_album_id") or details.get("album_id")
    album_title = metadata.get("_album_title") or details.get("album")
    image_url = metadata.get("image", "")
    release_date = _parse_release_date_str(details.get("release_date"))

    if album_id and album_title:
        album_instance, created = Album.objects.get_or_create(
            musicbrainz_release_id=album_id,
            defaults={
                "title": album_title,
                "artist": artist_instance,
                "image": image_url,
                "release_date": release_date,
                "genres": track_genres,
            },
        )
        update_fields: list[str] = []
        if not created and image_url and image_url != settings.IMG_NONE and not album_instance.image:
            album_instance.image = image_url
            update_fields.append("image")
        if not album_instance.release_date and release_date:
            album_instance.release_date = release_date
            update_fields.append("release_date")
        if not album_instance.genres and track_genres:
            album_instance.genres = track_genres
            update_fields.append("genres")
        if artist_instance and album_instance.artist_id is None:
            album_instance.artist = artist_instance
            update_fields.append("artist")
        if update_fields:
            album_instance.save(update_fields=update_fields)
    elif album_title:
        album_instance = Album.objects.filter(
            title=album_title,
            artist=artist_instance,
        ).first()
        if album_instance is None:
            album_instance = Album.objects.create(
                title=album_title,
                artist=artist_instance,
                image=image_url,
                release_date=release_date,
                genres=track_genres,
            )

    if album_instance:
        track_instance = Track.objects.filter(
            album=album_instance,
            musicbrainz_recording_id=media_id,
        ).first()

    return artist_instance, album_instance, track_instance


def ensure_item_metadata(
    user,
    media_type: str,
    media_id: str,
    source: str,
    season_number=None,
    *,
    episode_number=None,
    identity_media_type: str | None = None,
    library_media_type: str | None = None,
    fallback_title: str = "",
    fallback_image: str | None = None,
    fallback_release_date: str | None = None,
) -> HydratedItemResult:
    """Get or create an Item with the same metadata quality used for tracked saves."""
    season_numbers = [season_number] if season_number is not None else None
    metadata = services.get_media_metadata(
        media_type,
        media_id,
        source,
        season_numbers,
        episode_number,
    )
    podcast_show = None
    if media_type == MediaTypes.PODCAST.value and source == Sources.POCKETCASTS.value:
        metadata, podcast_show = _enrich_podcast_lookup(metadata, str(media_id))
    metadata = _fallback_metadata(
        metadata,
        media_type=media_type,
        source=source,
        fallback_title=fallback_title,
        fallback_image=fallback_image,
        fallback_release_date=fallback_release_date,
    )

    details = metadata.get("details", {})
    if not isinstance(details, dict):
        details = {}

    runtime_minutes = None
    if details.get("runtime"):
        runtime_minutes = stats.parse_runtime_to_minutes(details["runtime"])
    release_datetime = helpers.extract_release_datetime(metadata)

    number_of_pages = None
    if media_type == MediaTypes.BOOK.value:
        number_of_pages = metadata.get("max_progress") or details.get("number_of_pages")

    metadata_genres = metadata_utils.extract_metadata_genres(metadata)
    country = _coerce_text(details.get("country"))
    languages = [value for value in _coerce_list(details.get("languages")) if value]
    platforms = [value for value in _coerce_list(details.get("platforms")) if value]
    format_type = _coerce_text(details.get("format"))
    status = _coerce_text(details.get("status"))
    studios = [value for value in _coerce_list(details.get("studios")) if value]
    themes = [value for value in _coerce_list(details.get("themes")) if value]

    raw_authors = details.get("authors") or details.get("author") or details.get("people")
    authors: list[str] = []
    for raw_author in _coerce_list(raw_authors):
        if isinstance(raw_author, dict):
            author_name = (
                raw_author.get("name")
                or raw_author.get("person")
                or raw_author.get("author")
            )
        else:
            author_name = raw_author
        author_text = _coerce_text(author_name)
        if author_text:
            authors.append(author_text)

    publishers = details.get("publishers", "") or details.get("publisher", "")
    if isinstance(publishers, list):
        publishers = publishers[0] if publishers else ""
    publishers = _coerce_text(publishers)

    isbn = details.get("isbn", [])
    if not isinstance(isbn, list):
        isbn = []

    source_material = _coerce_text(details.get("source"))
    creators = details.get("people", [])
    if not isinstance(creators, list):
        creators = []
    runtime = _coerce_text(details.get("runtime"))
    tracking_media_type = get_tracking_media_type(
        media_type,
        source=source,
        identity_media_type=identity_media_type or metadata.get("identity_media_type"),
    )
    resolved_library_media_type = get_library_media_type(
        media_type,
        library_media_type=library_media_type or metadata.get("library_media_type"),
    )

    item, created = Item.objects.get_or_create(
        media_id=media_id,
        source=source,
        media_type=tracking_media_type,
        season_number=season_number,
        episode_number=episode_number,
        defaults={
            **Item.title_fields_from_metadata(metadata, fallback_title=fallback_title),
            "library_media_type": resolved_library_media_type,
            "image": metadata.get("image") or settings.IMG_NONE,
            "runtime_minutes": runtime_minutes,
            "number_of_pages": number_of_pages,
            "release_datetime": release_datetime,
            "genres": metadata_utils.merge_persisted_genres(
                source=source,
                media_type=tracking_media_type,
                incoming_genres=metadata_genres,
            ),
            "country": country,
            "languages": languages,
            "platforms": platforms,
            "format": format_type,
            "status": status,
            "studios": studios,
            "themes": themes,
            "authors": authors,
            "publishers": publishers,
            "isbn": isbn,
            "source_material": source_material,
            "creators": creators,
            "runtime": runtime,
            "metadata_fetched_at": timezone.now(),
        },
    )

    update_fields: list[str] = []
    title_fields = Item.title_fields_from_metadata(metadata, fallback_title=fallback_title)
    if not created:
        item.metadata_fetched_at = timezone.now()
        update_fields.append("metadata_fetched_at")
    if title_fields["title"] and item.title != title_fields["title"]:
        item.title = title_fields["title"]
        update_fields.append("title")
    if item.library_media_type != resolved_library_media_type:
        item.library_media_type = resolved_library_media_type
        update_fields.append("library_media_type")
    if item.image == settings.IMG_NONE and metadata.get("image"):
        item.image = metadata["image"]
        update_fields.append("image")
    if not item.runtime_minutes and runtime_minutes:
        item.runtime_minutes = runtime_minutes
        update_fields.append("runtime_minutes")
    if not item.number_of_pages and number_of_pages:
        item.number_of_pages = number_of_pages
        update_fields.append("number_of_pages")
    if not item.release_datetime and release_datetime:
        item.release_datetime = release_datetime
        update_fields.append("release_datetime")
    update_fields.extend(metadata_utils.apply_item_genres(item, metadata_genres))
    if not item.country and country:
        item.country = country
        update_fields.append("country")
    if not item.languages and languages:
        item.languages = languages
        update_fields.append("languages")
    if not item.platforms and platforms:
        item.platforms = platforms
        update_fields.append("platforms")
    if not item.format and format_type:
        item.format = format_type
        update_fields.append("format")
    if not item.status and status:
        item.status = status
        update_fields.append("status")
    if not item.studios and studios:
        item.studios = studios
        update_fields.append("studios")
    if not item.themes and themes:
        item.themes = themes
        update_fields.append("themes")
    if not item.authors and authors:
        item.authors = authors
        update_fields.append("authors")
    if not item.publishers and publishers:
        item.publishers = publishers
        update_fields.append("publishers")
    if not item.isbn and isbn:
        item.isbn = isbn
        update_fields.append("isbn")
    if not item.source_material and source_material:
        item.source_material = source_material
        update_fields.append("source_material")
    if not item.creators and creators:
        item.creators = creators
        update_fields.append("creators")
    if not item.runtime and runtime:
        item.runtime = runtime
        update_fields.append("runtime")
    if title_fields["original_title"] and item.original_title != title_fields["original_title"]:
        item.original_title = title_fields["original_title"]
        update_fields.append("original_title")
    if title_fields["localized_title"] and item.localized_title != title_fields["localized_title"]:
        item.localized_title = title_fields["localized_title"]
        update_fields.append("localized_title")
    if update_fields:
        item.save(update_fields=list(dict.fromkeys(update_fields)))

    upsert_provider_links(
        item,
        metadata,
        provider=source,
        provider_media_type=tracking_media_type,
        season_number=season_number,
    )

    if source == Sources.TMDB.value and tracking_media_type in (
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
    ):
        credits.sync_item_credits_from_metadata(item, metadata)

    artist = None
    album = None
    track = None
    if media_type == MediaTypes.MUSIC.value:
        artist, album, track = _hydrate_music_relations(str(media_id), metadata)

    return HydratedItemResult(
        item=item,
        metadata=metadata,
        created=created,
        artist=artist,
        album=album,
        track=track,
        podcast_show=podcast_show,
    )


def ensure_item_metadata_from_discover_seed(
    media_type: str,
    media_id: str,
    source: str,
    season_number=None,
    *,
    episode_number=None,
    identity_media_type: str | None = None,
    library_media_type: str | None = None,
    fallback_title: str = "",
    fallback_image: str | None = None,
    fallback_release_date: str | None = None,
) -> HydratedItemResult:
    """Create or update a lightweight Item from Discover card seed data only."""
    metadata = _fallback_metadata(
        {},
        media_type=media_type,
        source=source,
        fallback_title=fallback_title,
        fallback_image=fallback_image,
        fallback_release_date=fallback_release_date,
    )
    title_fields = Item.title_fields_from_metadata(metadata, fallback_title=fallback_title)
    release_datetime = helpers.extract_release_datetime(metadata)
    tracking_media_type = get_tracking_media_type(
        media_type,
        source=source,
        identity_media_type=identity_media_type,
    )
    resolved_library_media_type = get_library_media_type(
        media_type,
        library_media_type=library_media_type,
    )

    item, created = Item.objects.get_or_create(
        media_id=media_id,
        source=source,
        media_type=tracking_media_type,
        season_number=season_number,
        episode_number=episode_number,
        defaults={
            **title_fields,
            "library_media_type": resolved_library_media_type,
            "image": metadata.get("image") or settings.IMG_NONE,
            "release_datetime": release_datetime,
        },
    )

    update_fields: list[str] = []
    if item.library_media_type != resolved_library_media_type:
        item.library_media_type = resolved_library_media_type
        update_fields.append("library_media_type")
    if not item.title and title_fields["title"]:
        item.title = title_fields["title"]
        update_fields.append("title")
    if not item.original_title and title_fields["original_title"]:
        item.original_title = title_fields["original_title"]
        update_fields.append("original_title")
    if not item.localized_title and title_fields["localized_title"]:
        item.localized_title = title_fields["localized_title"]
        update_fields.append("localized_title")
    if (
        (not item.image or item.image == settings.IMG_NONE)
        and metadata.get("image")
        and metadata.get("image") != settings.IMG_NONE
    ):
        item.image = metadata["image"]
        update_fields.append("image")
    if not item.release_datetime and release_datetime:
        item.release_datetime = release_datetime
        update_fields.append("release_datetime")
    if update_fields:
        item.save(update_fields=list(dict.fromkeys(update_fields)))

    return HydratedItemResult(
        item=item,
        metadata=metadata,
        created=created,
    )
