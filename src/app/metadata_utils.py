"""Helpers for persisting normalized provider metadata on Items."""

from __future__ import annotations

from app import helpers
from app.discover.feature_metadata import normalize_certification
from app.models import MediaTypes, Sources

ANIME_SUPPLEMENT_GENRE = "Anime"

CORE_METADATA_FIELDS = [
    "country",
    "languages",
    "platforms",
    "format",
    "status",
    "studios",
    "themes",
    "authors",
    "number_of_pages",
    "publishers",
    "isbn",
    "source_material",
    "creators",
    "runtime",
]

PROVIDER_METADATA_FIELDS = [
    "provider_popularity",
    "provider_rating",
    "provider_rating_count",
    "provider_keywords",
    "provider_certification",
    "provider_collection_id",
    "provider_collection_name",
]


def _coerce_list(value, *, allow_scalar: bool = True) -> list:
    if isinstance(value, list):
        return value
    if allow_scalar and value:
        return [value]
    return []


def normalize_genres(value) -> list[str]:
    """Normalize a provider or model genre payload into unique strings."""
    from app.statistics import _coerce_genre_list

    genres = _coerce_genre_list(value)
    return list(dict.fromkeys([str(genre) for genre in genres if genre]))


def genre_list_has_name(genres, name: str) -> bool:
    """Return whether a genre list contains the named genre, case-insensitively."""
    target = str(name or "").strip().lower()
    if not target:
        return False
    return any(str(genre).strip().lower() == target for genre in normalize_genres(genres))


def extract_metadata_genres(metadata: dict | None) -> list[str]:
    """Return normalized genres from provider metadata."""
    if not isinstance(metadata, dict):
        return []
    details = metadata.get("details")
    genres_raw = []
    if isinstance(details, dict):
        genres_raw = details.get("genres") or details.get("genre") or []
    if not genres_raw:
        genres_raw = metadata.get("genres") or metadata.get("genre") or []
    return normalize_genres(genres_raw)


def merge_persisted_genres(
    *,
    source: str,
    media_type: str,
    incoming_genres,
    existing_genres=None,
    add_anime: bool = False,
) -> list[str]:
    """Return the stored genre list for an item after source-driven updates."""
    merged = normalize_genres(incoming_genres)
    existing = normalize_genres(existing_genres)

    if source == Sources.TMDB.value and media_type == MediaTypes.TV.value:
        if add_anime or genre_list_has_name(existing, ANIME_SUPPLEMENT_GENRE):
            if not genre_list_has_name(merged, ANIME_SUPPLEMENT_GENRE):
                merged.append(ANIME_SUPPLEMENT_GENRE)

    return merged


def apply_item_genres(
    item,
    incoming_genres,
    *,
    add_anime: bool = False,
) -> list[str]:
    """Apply merged genres to an item and return changed fields."""
    merged = merge_persisted_genres(
        source=item.source,
        media_type=item.media_type,
        incoming_genres=incoming_genres,
        existing_genres=item.genres,
        add_anime=add_anime,
    )
    current = normalize_genres(item.genres)
    if current != merged:
        item.genres = merged
        return ["genres"]
    return []


def extract_item_metadata_values(metadata: dict | None) -> dict[str, object]:
    """Return normalized metadata values used on the Item model."""
    payload = metadata if isinstance(metadata, dict) else {}
    details = payload.get("details") or {}
    if not isinstance(details, dict):
        details = {}

    authors = details.get("authors") or details.get("author") or []
    if isinstance(authors, str):
        authors = [authors] if authors else []
    elif not isinstance(authors, list):
        authors = []

    publishers = details.get("publishers") or details.get("publisher") or ""
    if isinstance(publishers, list):
        publishers = publishers[0] if publishers else ""

    raw_number_of_pages = (
        payload.get("max_progress") or details.get("number_of_pages")
    )
    try:
        number_of_pages = (
            int(raw_number_of_pages) if raw_number_of_pages is not None else None
        )
    except (TypeError, ValueError):
        number_of_pages = None

    return {
        "country": details.get("country") or "",
        "languages": _coerce_list(details.get("languages")),
        "platforms": _coerce_list(details.get("platforms"), allow_scalar=False),
        "format": details.get("format") or "",
        "status": details.get("status") or "",
        "studios": _coerce_list(details.get("studios"), allow_scalar=False),
        "themes": _coerce_list(details.get("themes"), allow_scalar=False),
        "authors": authors,
        "number_of_pages": number_of_pages,
        "publishers": publishers,
        "isbn": _coerce_list(details.get("isbn"), allow_scalar=False),
        "source_material": details.get("source") or "",
        "creators": _coerce_list(details.get("people"), allow_scalar=False),
        "runtime": details.get("runtime") or "",
        "provider_popularity": payload.get("provider_popularity"),
        "provider_rating": payload.get("provider_rating", payload.get("score")),
        "provider_rating_count": payload.get(
            "provider_rating_count",
            payload.get("score_count"),
        ),
        "provider_keywords": _coerce_list(
            payload.get("provider_keywords"),
            allow_scalar=False,
        ),
        "provider_certification": normalize_certification(
            payload.get("provider_certification") or details.get("certification") or "",
        ),
        "provider_collection_id": str(
            payload.get("provider_collection_id") or ""
        ).strip(),
        "provider_collection_name": str(
            payload.get("provider_collection_name") or ""
        ).strip(),
        "release_datetime": helpers.extract_release_datetime(payload),
    }


def apply_item_metadata(
    item,
    metadata: dict | None,
    *,
    include_core: bool = True,
    include_provider: bool = True,
    include_release: bool = True,
) -> list[str]:
    """Apply selected metadata fields to an item and return changed fields."""
    values = extract_item_metadata_values(metadata)
    update_fields: list[str] = []
    for field_name in CORE_METADATA_FIELDS:
        if not include_core:
            break
        if getattr(item, field_name) != values[field_name]:
            setattr(item, field_name, values[field_name])
            update_fields.append(field_name)

    if include_provider:
        for field_name in PROVIDER_METADATA_FIELDS:
            if getattr(item, field_name) != values[field_name]:
                setattr(item, field_name, values[field_name])
                update_fields.append(field_name)

    if (
        include_release
        and values["release_datetime"]
        and item.release_datetime != values["release_datetime"]
    ):
        item.release_datetime = values["release_datetime"]
        update_fields.append("release_datetime")

    return update_fields
