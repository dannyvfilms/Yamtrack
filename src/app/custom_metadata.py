"""Helpers for editing and projecting manual/custom item metadata."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime

from django import forms
from django.conf import settings

from app import helpers
from app.models import MediaTypes
from app.statistics import parse_runtime_to_minutes


@dataclass(frozen=True)
class CustomMetadataFieldSpec:
    """Describe a single editable custom metadata field."""

    name: str
    label: str
    widget: str = "text"
    placeholder: str = ""
    help_text: str = ""
    detail_key: str | None = None
    top_level_key: str | None = None
    required: bool = False
    item_attr: str | None = None
    list_value: bool = False
    numeric_value: bool = False
    media_types: tuple[str, ...] = ()


TITLE_MEDIA_TYPES = (
    MediaTypes.TV.value,
    MediaTypes.MOVIE.value,
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.GAME.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
    MediaTypes.BOARDGAME.value,
)

CUSTOM_PROVIDER_MEDIA_TYPES = TITLE_MEDIA_TYPES

SUPPORTED_MEDIA_TYPES = (
    *TITLE_MEDIA_TYPES,
    MediaTypes.SEASON.value,
    MediaTypes.EPISODE.value,
)

FIELD_SPECS: dict[str, CustomMetadataFieldSpec] = {
    "title": CustomMetadataFieldSpec(
        name="title",
        label="Title",
        required=True,
        top_level_key="title",
        item_attr="title",
        placeholder="Enter a title",
        media_types=TITLE_MEDIA_TYPES,
    ),
    "season_title": CustomMetadataFieldSpec(
        name="season_title",
        label="Season Title",
        top_level_key="season_title",
        placeholder="Season 1",
        media_types=(MediaTypes.SEASON.value,),
    ),
    "episode_title": CustomMetadataFieldSpec(
        name="episode_title",
        label="Episode Title",
        top_level_key="episode_title",
        placeholder="Episode 1",
        media_types=(MediaTypes.EPISODE.value,),
    ),
    "original_title": CustomMetadataFieldSpec(
        name="original_title",
        label="Original Title",
        top_level_key="original_title",
        item_attr="original_title",
        placeholder="Enter the original title",
        media_types=TITLE_MEDIA_TYPES,
    ),
    "localized_title": CustomMetadataFieldSpec(
        name="localized_title",
        label="Localized Title",
        top_level_key="localized_title",
        item_attr="localized_title",
        placeholder="Enter the localized title",
        media_types=TITLE_MEDIA_TYPES,
    ),
    "image_url": CustomMetadataFieldSpec(
        name="image_url",
        label="Image URL",
        widget="url",
        top_level_key="image",
        placeholder="https://example.com/poster.jpg",
        media_types=SUPPORTED_MEDIA_TYPES,
    ),
    "synopsis": CustomMetadataFieldSpec(
        name="synopsis",
        label="Synopsis",
        widget="textarea",
        top_level_key="synopsis",
        placeholder="Add a synopsis or description...",
        media_types=SUPPORTED_MEDIA_TYPES,
    ),
    "genres": CustomMetadataFieldSpec(
        name="genres",
        label="Genres",
        widget="textarea",
        top_level_key="genres",
        placeholder="Drama, Thriller",
        help_text="Enter one per line or separate values with commas.",
        item_attr="genres",
        list_value=True,
        media_types=SUPPORTED_MEDIA_TYPES,
    ),
    "release_date": CustomMetadataFieldSpec(
        name="release_date",
        label="Release Date",
        widget="date",
        detail_key="release_date",
        media_types=(
            MediaTypes.MOVIE.value,
            MediaTypes.GAME.value,
            MediaTypes.COMIC.value,
            MediaTypes.BOARDGAME.value,
        ),
    ),
    "first_air_date": CustomMetadataFieldSpec(
        name="first_air_date",
        label="First Air Date",
        widget="date",
        detail_key="first_air_date",
        media_types=(
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
        ),
    ),
    "last_air_date": CustomMetadataFieldSpec(
        name="last_air_date",
        label="Last Air Date",
        widget="date",
        detail_key="last_air_date",
        media_types=(
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
        ),
    ),
    "start_date": CustomMetadataFieldSpec(
        name="start_date",
        label="Start Date",
        widget="date",
        detail_key="start_date",
        media_types=(
            MediaTypes.ANIME.value,
            MediaTypes.MANGA.value,
        ),
    ),
    "end_date": CustomMetadataFieldSpec(
        name="end_date",
        label="End Date",
        widget="date",
        detail_key="end_date",
        media_types=(
            MediaTypes.ANIME.value,
            MediaTypes.MANGA.value,
        ),
    ),
    "publish_date": CustomMetadataFieldSpec(
        name="publish_date",
        label="Publish Date",
        widget="date",
        detail_key="publish_date",
        media_types=(MediaTypes.BOOK.value,),
    ),
    "air_date": CustomMetadataFieldSpec(
        name="air_date",
        label="Air Date",
        widget="date",
        detail_key="air_date",
        media_types=(MediaTypes.EPISODE.value,),
    ),
    "status": CustomMetadataFieldSpec(
        name="status",
        label="Metadata Status",
        detail_key="status",
        item_attr="status",
        placeholder="Ended, Released, Finished...",
        media_types=(
            MediaTypes.TV.value,
            MediaTypes.MOVIE.value,
            MediaTypes.ANIME.value,
            MediaTypes.MANGA.value,
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
        ),
    ),
    "season_count": CustomMetadataFieldSpec(
        name="season_count",
        label="Seasons",
        widget="number",
        detail_key="seasons",
        numeric_value=True,
        media_types=(MediaTypes.TV.value,),
    ),
    "episode_count": CustomMetadataFieldSpec(
        name="episode_count",
        label="Episodes",
        widget="number",
        detail_key="episodes",
        numeric_value=True,
        media_types=(
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
            MediaTypes.ANIME.value,
        ),
    ),
    "chapter_count": CustomMetadataFieldSpec(
        name="chapter_count",
        label="Chapters",
        widget="number",
        detail_key="number_of_chapters",
        numeric_value=True,
        media_types=(MediaTypes.MANGA.value,),
    ),
    "issue_count": CustomMetadataFieldSpec(
        name="issue_count",
        label="Issues",
        widget="number",
        detail_key="number_of_issues",
        numeric_value=True,
        media_types=(MediaTypes.COMIC.value,),
    ),
    "page_count": CustomMetadataFieldSpec(
        name="page_count",
        label="Pages",
        widget="number",
        detail_key="number_of_pages",
        item_attr="number_of_pages",
        numeric_value=True,
        media_types=(MediaTypes.BOOK.value,),
    ),
    "runtime": CustomMetadataFieldSpec(
        name="runtime",
        label="Runtime",
        detail_key="runtime",
        item_attr="runtime",
        placeholder="2h 10min or 24m",
        media_types=(
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
            MediaTypes.EPISODE.value,
            MediaTypes.MOVIE.value,
            MediaTypes.ANIME.value,
            MediaTypes.BOARDGAME.value,
        ),
    ),
    "format": CustomMetadataFieldSpec(
        name="format",
        label="Format",
        detail_key="format",
        item_attr="format",
        placeholder="Hardcover, Main game, One-shot...",
        media_types=(
            MediaTypes.GAME.value,
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
            MediaTypes.BOARDGAME.value,
        ),
    ),
    "studios": CustomMetadataFieldSpec(
        name="studios",
        label="Studios",
        widget="textarea",
        detail_key="studios",
        item_attr="studios",
        list_value=True,
        placeholder="Studio Bones, Madhouse",
        help_text="Enter one per line or separate values with commas.",
        media_types=(
            MediaTypes.TV.value,
            MediaTypes.MOVIE.value,
            MediaTypes.ANIME.value,
            MediaTypes.GAME.value,
        ),
    ),
    "country": CustomMetadataFieldSpec(
        name="country",
        label="Country",
        detail_key="country",
        item_attr="country",
        placeholder="Japan",
        media_types=(
            MediaTypes.TV.value,
            MediaTypes.MOVIE.value,
            MediaTypes.ANIME.value,
            MediaTypes.MANGA.value,
            MediaTypes.GAME.value,
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.BOARDGAME.value,
        ),
    ),
    "languages": CustomMetadataFieldSpec(
        name="languages",
        label="Languages",
        widget="textarea",
        detail_key="languages",
        item_attr="languages",
        list_value=True,
        placeholder="English, Japanese",
        help_text="Enter one per line or separate values with commas.",
        media_types=(
            MediaTypes.TV.value,
            MediaTypes.MOVIE.value,
            MediaTypes.ANIME.value,
            MediaTypes.MANGA.value,
            MediaTypes.GAME.value,
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.BOARDGAME.value,
        ),
    ),
    "authors": CustomMetadataFieldSpec(
        name="authors",
        label="Authors",
        widget="textarea",
        detail_key="author",
        item_attr="authors",
        list_value=True,
        placeholder="Author Name",
        help_text="Enter one per line or separate values with commas.",
        media_types=(
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
        ),
    ),
    "publishers": CustomMetadataFieldSpec(
        name="publishers",
        label="Publisher",
        detail_key="publisher",
        item_attr="publishers",
        placeholder="Publisher name",
        media_types=(
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.MANGA.value,
            MediaTypes.BOARDGAME.value,
        ),
    ),
    "isbn": CustomMetadataFieldSpec(
        name="isbn",
        label="ISBN",
        widget="textarea",
        detail_key="isbn",
        item_attr="isbn",
        list_value=True,
        placeholder="9780141187761",
        help_text="Enter one per line or separate values with commas.",
        media_types=(MediaTypes.BOOK.value,),
    ),
    "platforms": CustomMetadataFieldSpec(
        name="platforms",
        label="Platforms",
        widget="textarea",
        detail_key="platforms",
        item_attr="platforms",
        list_value=True,
        placeholder="PC, PlayStation 5",
        help_text="Enter one per line or separate values with commas.",
        media_types=(MediaTypes.GAME.value,),
    ),
    "themes": CustomMetadataFieldSpec(
        name="themes",
        label="Themes",
        widget="textarea",
        detail_key="themes",
        item_attr="themes",
        list_value=True,
        placeholder="Fantasy, Sci-Fi",
        help_text="Enter one per line or separate values with commas.",
        media_types=(
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
        ),
    ),
    "source_material": CustomMetadataFieldSpec(
        name="source_material",
        label="Source Material",
        detail_key="source",
        item_attr="source_material",
        placeholder="Manga",
        media_types=(MediaTypes.ANIME.value,),
    ),
    "creators": CustomMetadataFieldSpec(
        name="creators",
        label="Creators",
        widget="textarea",
        detail_key="people",
        item_attr="creators",
        list_value=True,
        placeholder="Creator Name",
        help_text="Enter one per line or separate values with commas.",
        media_types=(MediaTypes.COMIC.value,),
    ),
}

MEDIA_TYPE_FIELD_ORDER: dict[str, list[str]] = {
    MediaTypes.TV.value: [
        "title",
        "original_title",
        "localized_title",
        "image_url",
        "synopsis",
        "genres",
        "first_air_date",
        "last_air_date",
        "status",
        "season_count",
        "episode_count",
        "runtime",
        "studios",
        "country",
        "languages",
    ],
    MediaTypes.SEASON.value: [
        "season_title",
        "image_url",
        "synopsis",
        "first_air_date",
        "last_air_date",
        "episode_count",
        "runtime",
    ],
    MediaTypes.EPISODE.value: [
        "episode_title",
        "image_url",
        "synopsis",
        "air_date",
        "runtime",
    ],
    MediaTypes.MOVIE.value: [
        "title",
        "original_title",
        "localized_title",
        "image_url",
        "synopsis",
        "genres",
        "release_date",
        "status",
        "runtime",
        "studios",
        "country",
        "languages",
    ],
    MediaTypes.ANIME.value: [
        "title",
        "original_title",
        "localized_title",
        "image_url",
        "synopsis",
        "genres",
        "start_date",
        "end_date",
        "status",
        "episode_count",
        "runtime",
        "studios",
        "country",
        "languages",
        "source_material",
    ],
    MediaTypes.MANGA.value: [
        "title",
        "original_title",
        "localized_title",
        "image_url",
        "synopsis",
        "genres",
        "start_date",
        "end_date",
        "status",
        "chapter_count",
        "authors",
        "publishers",
        "format",
        "country",
        "languages",
    ],
    MediaTypes.GAME.value: [
        "title",
        "original_title",
        "localized_title",
        "image_url",
        "synopsis",
        "genres",
        "release_date",
        "status",
        "format",
        "platforms",
        "themes",
        "studios",
        "country",
        "languages",
    ],
    MediaTypes.BOOK.value: [
        "title",
        "original_title",
        "localized_title",
        "image_url",
        "synopsis",
        "genres",
        "publish_date",
        "authors",
        "publishers",
        "isbn",
        "page_count",
        "format",
        "country",
        "languages",
    ],
    MediaTypes.COMIC.value: [
        "title",
        "original_title",
        "localized_title",
        "image_url",
        "synopsis",
        "genres",
        "release_date",
        "authors",
        "creators",
        "publishers",
        "issue_count",
        "format",
        "country",
        "languages",
    ],
    MediaTypes.BOARDGAME.value: [
        "title",
        "original_title",
        "localized_title",
        "image_url",
        "synopsis",
        "genres",
        "release_date",
        "publishers",
        "runtime",
        "format",
        "themes",
        "country",
        "languages",
    ],
}

DETAIL_VALUE_ORDER: dict[str, list[str]] = {
    media_type: [
        field_name
        for field_name in field_names
        if FIELD_SPECS[field_name].detail_key
    ]
    for media_type, field_names in MEDIA_TYPE_FIELD_ORDER.items()
}

MAX_PROGRESS_DETAIL_KEY = {
    MediaTypes.MOVIE.value: None,
    MediaTypes.TV.value: "episodes",
    MediaTypes.SEASON.value: "episodes",
    MediaTypes.ANIME.value: "episodes",
    MediaTypes.BOOK.value: "number_of_pages",
    MediaTypes.MANGA.value: "number_of_chapters",
    MediaTypes.COMIC.value: "number_of_issues",
}

RELEASE_DATE_DETAIL_KEYS = (
    "release_date",
    "first_air_date",
    "start_date",
    "publish_date",
    "air_date",
)

IMAGE_FIELD_NAME = "image_url"


def supports_custom_metadata(item_or_source, media_type: str | None = None) -> bool:
    """Return whether a media type supports editable custom metadata fields."""
    if hasattr(item_or_source, "media_type"):
        media_type = item_or_source.media_type
    elif media_type is None:
        media_type = item_or_source
    return media_type in MEDIA_TYPE_FIELD_ORDER


def supports_custom_provider(item_or_media_type) -> bool:
    """Return whether a route media type supports the Custom display provider."""
    media_type = (
        item_or_media_type.media_type
        if hasattr(item_or_media_type, "media_type")
        else item_or_media_type
    )
    return media_type in CUSTOM_PROVIDER_MEDIA_TYPES


def field_specs_for_media_type(media_type: str) -> list[CustomMetadataFieldSpec]:
    """Return ordered field specs for the given media type."""
    return [FIELD_SPECS[name] for name in MEDIA_TYPE_FIELD_ORDER.get(media_type, [])]


def detail_field_names_for_media_type(media_type: str) -> list[str]:
    """Return ordered detail-backed field names for the given media type."""
    return DETAIL_VALUE_ORDER.get(media_type, [])


def _coerce_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(entry).strip() for entry in value if str(entry).strip()]
    if not value:
        return []
    return [str(value).strip()]


def _serialize_list_for_initial(value) -> str:
    return "\n".join(_coerce_list(value))


def _split_list_value(value) -> list[str]:
    if not value:
        return []
    parts = []
    for chunk in str(value).replace("\r", "\n").split("\n"):
        for entry in chunk.split(","):
            normalized = entry.strip()
            if normalized:
                parts.append(normalized)
    return list(dict.fromkeys(parts))


def _is_empty_value(value) -> bool:
    return value in (None, "", [], {})


def _date_to_iso(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _normalize_stored_value(value, spec: CustomMetadataFieldSpec):
    if spec.list_value:
        return _coerce_list(value)
    if spec.numeric_value:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    if spec.widget == "date":
        return _date_to_iso(value)
    return str(value).strip() if value not in (None, "") else ""


def _build_field(spec: CustomMetadataFieldSpec) -> forms.Field:
    common_kwargs = {
        "label": spec.label,
        "required": spec.required,
        "help_text": spec.help_text,
    }
    if spec.widget == "textarea":
        return forms.CharField(
            widget=forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": spec.placeholder,
                },
            ),
            **common_kwargs,
        )
    if spec.widget == "url":
        return forms.URLField(
            widget=forms.URLInput(attrs={"placeholder": spec.placeholder}),
            **common_kwargs,
        )
    if spec.widget == "date":
        return forms.DateField(
            widget=forms.DateInput(attrs={"type": "date"}),
            input_formats=["%Y-%m-%d"],
            **common_kwargs,
        )
    if spec.widget == "number":
        return forms.IntegerField(
            min_value=0,
            widget=forms.NumberInput(attrs={"min": 0}),
            **common_kwargs,
        )
    return forms.CharField(
        widget=forms.TextInput(attrs={"placeholder": spec.placeholder}),
        **common_kwargs,
    )


def _manual_metadata_map(item) -> dict:
    payload = getattr(item, "manual_metadata", None)
    return payload if isinstance(payload, dict) else {}


def _manual_detail_map(item) -> dict:
    details = _manual_metadata_map(item).get("details")
    return details if isinstance(details, dict) else {}


def _release_date_fallback_value(item, spec: CustomMetadataFieldSpec):
    if spec.detail_key in RELEASE_DATE_DETAIL_KEYS:
        return getattr(item, "release_datetime", None)
    return ""


def _get_item_field_value(item, spec: CustomMetadataFieldSpec):
    value = ""
    if spec.name == "image_url":
        stored_image = _manual_metadata_map(item).get(spec.top_level_key or "")
        if stored_image not in (None, ""):
            value = stored_image
        else:
            image = getattr(item, "image", "") or ""
            value = "" if image == settings.IMG_NONE else image
    elif spec.name == "synopsis":
        value = _manual_metadata_map(item).get("synopsis") or ""
    elif spec.top_level_key:
        value = _manual_metadata_map(item).get(spec.top_level_key)
        if _is_empty_value(value) and spec.item_attr:
            value = getattr(item, spec.item_attr, None)
        if _is_empty_value(value):
            value = ""
    elif spec.detail_key:
        details = _manual_detail_map(item)
        detail_value = details.get(spec.detail_key)
        if not _is_empty_value(detail_value):
            value = detail_value
        elif spec.item_attr:
            value = getattr(item, spec.item_attr, None)
        else:
            value = _release_date_fallback_value(item, spec)
    elif spec.item_attr:
        value = getattr(item, spec.item_attr, None)
    else:
        value = _release_date_fallback_value(item, spec)

    if spec.widget == "date":
        return _date_to_iso(value)
    return value


def initial_value_for_field(item, field_name: str):
    """Return the initial form value for a field on an item."""
    spec = FIELD_SPECS[field_name]
    value = _get_item_field_value(item, spec)
    if spec.list_value:
        return _serialize_list_for_initial(value)
    if spec.widget == "date":
        return _date_to_iso(value)
    if spec.numeric_value:
        return value if value not in ("", None) else None
    return value


def provider_value_for_item(item, field_name: str, fallback=None):
    """Return a provider-style top-level value for a custom-enabled item field."""
    spec = FIELD_SPECS[field_name]
    value = _get_item_field_value(item, spec)
    if _is_empty_value(value):
        value = fallback

    if spec.name == IMAGE_FIELD_NAME:
        normalized = str(value).strip() if value not in (None, "") else ""
        return normalized or settings.IMG_NONE
    if spec.list_value:
        return _coerce_list(value)
    if spec.numeric_value:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    if spec.widget == "date":
        return _date_to_iso(value)
    return str(value).strip() if value not in (None, "") else ""


def detail_value_for_item(item, field_name: str, fallback_details: dict | None = None):
    """Return the provider-style detail value for a field."""
    spec = FIELD_SPECS[field_name]
    if not spec.detail_key:
        return None

    details = _manual_detail_map(item)
    fallback_details = fallback_details or {}
    value = details.get(spec.detail_key)
    if _is_empty_value(value):
        if spec.item_attr:
            value = getattr(item, spec.item_attr, None)
        elif spec.detail_key in RELEASE_DATE_DETAIL_KEYS:
            value = getattr(item, "release_datetime", None)
    if _is_empty_value(value):
        value = fallback_details.get(spec.detail_key)

    if spec.widget == "date":
        return _date_to_iso(value)
    if spec.list_value:
        return _coerce_list(value)
    if spec.numeric_value:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return str(value).strip() if value not in (None, "") else ""


def build_manual_detail_payload(
    item,
    *,
    fallback_details: dict | None = None,
) -> OrderedDict:
    """Return ordered detail metadata for a manual/custom item."""
    details = OrderedDict()
    for field_name in detail_field_names_for_media_type(item.media_type):
        spec = FIELD_SPECS[field_name]
        value = detail_value_for_item(
            item,
            field_name,
            fallback_details=fallback_details,
        )
        if _is_empty_value(value):
            continue
        details[spec.detail_key] = value
    return details


def get_manual_synopsis(item) -> str:
    """Return a stored manual synopsis or the default fallback."""
    synopsis = str(_manual_metadata_map(item).get("synopsis") or "").strip()
    return synopsis or "No synopsis available."


def get_manual_top_level_value(item, key: str):
    """Return a stored manual top-level metadata value."""
    value = _manual_metadata_map(item).get(key)
    if isinstance(value, str):
        return value.strip()
    return value


def build_custom_overlay_metadata(base_metadata: dict, item) -> dict:
    """Overlay stored custom metadata onto provider metadata for any tracked item."""
    merged = dict(base_metadata or {})
    base_details = merged.get("details")
    base_details = dict(base_details) if isinstance(base_details, dict) else {}

    merged["title"] = provider_value_for_item(
        item,
        "title",
        fallback=merged.get("title"),
    )
    merged["original_title"] = provider_value_for_item(
        item,
        "original_title",
        fallback=merged.get("original_title"),
    )
    merged["localized_title"] = provider_value_for_item(
        item,
        "localized_title",
        fallback=merged.get("localized_title") or merged.get("title"),
    )
    merged["image"] = provider_value_for_item(
        item,
        IMAGE_FIELD_NAME,
        fallback=merged.get("image"),
    )
    merged["synopsis"] = provider_value_for_item(
        item,
        "synopsis",
        fallback=merged.get("synopsis"),
    )
    merged["genres"] = provider_value_for_item(
        item,
        "genres",
        fallback=merged.get("genres"),
    )

    custom_details = build_manual_detail_payload(
        item,
        fallback_details=base_details,
    )
    merged["details"] = base_details | dict(custom_details)
    merged["max_progress"] = manual_max_progress(
        item,
        merged["details"],
        fallback_max_progress=merged.get("max_progress"),
    )
    return merged


def _metadata_details_map(metadata) -> dict:
    details = (metadata or {}).get("details")
    return details if isinstance(details, dict) else {}


def metadata_value_for_field(metadata: dict, field_name: str):
    """Return the provider payload value for a custom metadata field."""
    payload = metadata if isinstance(metadata, dict) else {}
    spec = FIELD_SPECS[field_name]

    if spec.name == IMAGE_FIELD_NAME:
        image_value = payload.get("image") or ""
        return "" if image_value == settings.IMG_NONE else image_value
    if spec.name == "synopsis":
        return payload.get("synopsis") or ""
    if spec.top_level_key:
        return payload.get(spec.top_level_key)
    if spec.detail_key:
        return _metadata_details_map(payload).get(spec.detail_key)
    return ""


def snapshot_custom_metadata(item, metadata: dict) -> list[str]:
    """Persist the current display metadata as the item's Custom provider state."""
    if not item or not supports_custom_metadata(item) or not isinstance(metadata, dict):
        return []

    update_fields: list[str] = []
    manual_metadata = {}
    details = {}

    def assign(attr_name: str, new_value) -> None:
        if getattr(item, attr_name) != new_value:
            setattr(item, attr_name, new_value)
            update_fields.append(attr_name)

    for spec in field_specs_for_media_type(item.media_type):
        value = _normalize_stored_value(
            metadata_value_for_field(metadata, spec.name),
            spec,
        )

        if spec.name == IMAGE_FIELD_NAME:
            assign("image", value or settings.IMG_NONE)
        elif spec.item_attr:
            assign(spec.item_attr, value)

        if spec.top_level_key and not _is_empty_value(value):
            manual_metadata[spec.top_level_key] = value
        if spec.detail_key and not _is_empty_value(value):
            details[spec.detail_key] = value

    payload = {"details": details} if details else {}
    payload.update(manual_metadata)
    assign("manual_metadata", payload)

    release_datetime = helpers.extract_release_datetime({"details": details})
    assign("release_datetime", release_datetime)

    runtime_value = str(item.runtime or "").strip()
    runtime_minutes = (
        parse_runtime_to_minutes(runtime_value)
        if runtime_value
        else None
    )
    assign("runtime_minutes", runtime_minutes)

    if update_fields:
        item.save(update_fields=list(dict.fromkeys(update_fields)))
    return update_fields


def manual_max_progress(item, details: dict | None = None, fallback_max_progress=None):
    """Return the best max_progress value for a manual/custom item."""
    if item.media_type == MediaTypes.MOVIE.value:
        return 1

    details = details or {}
    detail_key = MAX_PROGRESS_DETAIL_KEY.get(item.media_type)
    if detail_key:
        try:
            value = details.get(detail_key)
            if value not in (None, ""):
                return int(value)
        except (TypeError, ValueError):
            pass
    return fallback_max_progress


class ManualMetadataForm(forms.Form):
    """Edit metadata for a manual/custom library item."""

    def __init__(self, *args, item=None, **kwargs):
        """Build the media-type-specific metadata form for a manual item."""
        self.item = item
        super().__init__(*args, **kwargs)

        if not item or not supports_custom_metadata(item):
            return

        for spec in field_specs_for_media_type(item.media_type):
            self.fields[spec.name] = _build_field(spec)

        if not self.is_bound:
            for spec in field_specs_for_media_type(item.media_type):
                self.initial[spec.name] = initial_value_for_field(item, spec.name)

    def _clean_field_value(self, spec: CustomMetadataFieldSpec):
        value = self.cleaned_data.get(spec.name)
        if spec.list_value:
            return _split_list_value(value)
        if spec.numeric_value:
            return int(value) if value not in (None, "") else None
        if spec.widget == "date":
            return _date_to_iso(value)
        return str(value).strip() if value not in (None, "") else ""

    def _assign_item_value(
        self,
        attr_name: str,
        new_value,
        update_fields: list[str],
    ) -> None:
        item = self.item
        if getattr(item, attr_name) != new_value:
            setattr(item, attr_name, new_value)
            update_fields.append(attr_name)

    def _build_payload(self) -> tuple[dict, dict]:
        manual_metadata = {}
        details = {}
        for spec in field_specs_for_media_type(self.item.media_type):
            value = self._clean_field_value(spec)

            if spec.top_level_key and not _is_empty_value(value):
                manual_metadata[spec.top_level_key] = value
            if spec.detail_key and not _is_empty_value(value):
                details[spec.detail_key] = value
        return manual_metadata, details

    def _sync_item_fields(self, update_fields: list[str]) -> None:
        for spec in field_specs_for_media_type(self.item.media_type):
            value = self._clean_field_value(spec)

            if spec.name == "image_url":
                self._assign_item_value(
                    "image",
                    value or settings.IMG_NONE,
                    update_fields,
                )
                continue

            if spec.item_attr:
                self._assign_item_value(spec.item_attr, value, update_fields)

    def _sync_release_datetime(self, details: dict, update_fields: list[str]) -> None:
        release_datetime = helpers.extract_release_datetime({"details": details})
        self._assign_item_value("release_datetime", release_datetime, update_fields)

    def _sync_runtime_minutes(self, update_fields: list[str]) -> None:
        runtime_value = str(self.item.runtime or "").strip()
        runtime_minutes = (
            parse_runtime_to_minutes(runtime_value)
            if runtime_value
            else None
        )
        self._assign_item_value("runtime_minutes", runtime_minutes, update_fields)

    def save(self):
        """Persist manual metadata and synced item columns."""
        if not self.item or not supports_custom_metadata(self.item):
            return []

        item = self.item
        update_fields = []
        manual_metadata, details = self._build_payload()

        self._sync_item_fields(update_fields)

        payload = {"details": details} if details else {}
        payload.update(manual_metadata)
        self._assign_item_value("manual_metadata", payload, update_fields)
        self._sync_release_datetime(details, update_fields)
        self._sync_runtime_minutes(update_fields)

        if update_fields:
            item.save(update_fields=list(dict.fromkeys(update_fields)))
        return update_fields
