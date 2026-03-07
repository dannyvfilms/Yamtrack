"""Helpers for persisting normalized provider metadata on Items."""

from __future__ import annotations

from app import helpers
from app.discover.feature_metadata import normalize_certification

CORE_METADATA_FIELDS = [
    "country",
    "languages",
    "platforms",
    "format",
    "status",
    "studios",
    "themes",
    "authors",
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

    return {
        "country": details.get("country") or "",
        "languages": _coerce_list(details.get("languages")),
        "platforms": _coerce_list(details.get("platforms"), allow_scalar=False),
        "format": details.get("format") or "",
        "status": details.get("status") or "",
        "studios": _coerce_list(details.get("studios"), allow_scalar=False),
        "themes": _coerce_list(details.get("themes"), allow_scalar=False),
        "authors": authors,
        "publishers": publishers,
        "isbn": _coerce_list(details.get("isbn"), allow_scalar=False),
        "source_material": details.get("source") or "",
        "creators": _coerce_list(details.get("people"), allow_scalar=False),
        "runtime": details.get("runtime") or "",
        "provider_popularity": payload.get("provider_popularity"),
        "provider_rating": payload.get("provider_rating", payload.get("score")),
        "provider_rating_count": payload.get("provider_rating_count", payload.get("score_count")),
        "provider_keywords": _coerce_list(payload.get("provider_keywords"), allow_scalar=False),
        "provider_certification": normalize_certification(
            payload.get("provider_certification") or details.get("certification") or "",
        ),
        "provider_collection_id": str(payload.get("provider_collection_id") or "").strip(),
        "provider_collection_name": str(payload.get("provider_collection_name") or "").strip(),
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

    if include_release and values["release_datetime"] and item.release_datetime != values["release_datetime"]:
        item.release_datetime = values["release_datetime"]
        update_fields.append("release_datetime")

    return update_fields
