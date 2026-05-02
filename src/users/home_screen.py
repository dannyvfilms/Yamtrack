"""Shared helpers for Home screen row persistence and rendering."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from app import helpers
from app.models import BasicMedia, Item, MediaTypes, Sources, Status
from app.templatetags import app_tags
from lists import smart_rules
from lists.models import CustomList
from users.models import (
    DirectionChoices,
    HomeScreenRow,
    HomeScreenRowTypeChoices,
    HomeSortChoices,
    MediaSortChoices,
)

RECENTLY_UNRATED_DAYS = 7
RECENTLY_UNRATED_LABEL = "Recently Played - Not Rated"
SQUARE_HOME_MEDIA_TYPES = {
    MediaTypes.MUSIC.value,
    MediaTypes.PODCAST.value,
}
HOME_SCREEN_FILTER_KEYS = tuple(
    key
    for key in smart_rules.SMART_FILTER_KEYS
    if key != "search"
)
AUTHOR_MEDIA_TYPES = {
    MediaTypes.BOOK.value,
    MediaTypes.MANGA.value,
    MediaTypes.COMIC.value,
}
CRITIC_RATING_MEDIA_TYPES = {
    MediaTypes.TV.value,
    MediaTypes.SEASON.value,
    MediaTypes.MOVIE.value,
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.GAME.value,
    MediaTypes.BOARDGAME.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
}
POPULARITY_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
}
PLAYS_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
}
RUNTIME_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
}
HOME_ONLY_SORTS = {
    HomeSortChoices.UPCOMING,
    HomeSortChoices.RECENT,
    HomeSortChoices.COMPLETION,
    HomeSortChoices.EPISODES_LEFT,
}
STATUS_FILTER_VALUES = {"all", *Status.values}
STATUS_FILTER_ALIASES = {"all": "all"}
for _status_choice in Status:
    STATUS_FILTER_ALIASES[str(_status_choice.value).strip().casefold()] = _status_choice.value
    STATUS_FILTER_ALIASES[str(_status_choice.label).strip().casefold()] = _status_choice.value

HOME_QUERY_DEFAULT_FILTERS = {
    "status": Status.IN_PROGRESS.value,
    "rating": "all",
    "collection": "all",
    "genre": "",
    "year": "",
    "release": "all",
    "source": "",
    "language": "",
    "country": "",
    "platform": "",
    "origin": "",
    "format": "",
    "author": "",
    "tag": "",
    "tag_exclude": "",
}
SUPPORTED_FILTERS_BY_MEDIA_TYPE = {
    MediaTypes.TV.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "language",
        "country",
        "tag",
        "tag_exclude",
    },
    MediaTypes.SEASON.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "tag",
        "tag_exclude",
    },
    MediaTypes.MOVIE.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "language",
        "country",
        "tag",
        "tag_exclude",
    },
    MediaTypes.ANIME.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "language",
        "country",
        "tag",
        "tag_exclude",
    },
    MediaTypes.MANGA.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "format",
        "author",
        "tag",
        "tag_exclude",
    },
    MediaTypes.GAME.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "platform",
        "tag",
        "tag_exclude",
    },
    MediaTypes.BOARDGAME.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "tag",
        "tag_exclude",
    },
    MediaTypes.BOOK.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "format",
        "author",
        "tag",
        "tag_exclude",
    },
    MediaTypes.COMIC.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "format",
        "author",
        "tag",
        "tag_exclude",
    },
    MediaTypes.MUSIC.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "origin",
        "tag",
        "tag_exclude",
    },
    MediaTypes.PODCAST.value: {
        "status",
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "language",
        "country",
        "tag",
        "tag_exclude",
    },
}


class HomeScreenValidationError(ValidationError):
    """Raised when submitted Home screen settings are invalid."""


@dataclass
class HomeRowEntry:
    """Template-facing Home row item wrapper."""

    item: Item
    media: object | None = None
    use_podcast_show: bool = False
    podcast_show: object | None = None
    show_progress_controls: bool = True


def resolve_home_row_direction(sort_by: str, direction: str | None = None) -> str:
    """Return a valid direction for the requested home-row sort key."""
    normalized = (direction or "").strip().lower()
    if normalized in DirectionChoices.values:
        return normalized

    if sort_by == HomeSortChoices.UPCOMING:
        return DirectionChoices.ASC
    if sort_by == HomeSortChoices.RECENT:
        return DirectionChoices.DESC
    if sort_by == HomeSortChoices.COMPLETION:
        return DirectionChoices.DESC
    if sort_by == HomeSortChoices.EPISODES_LEFT:
        return DirectionChoices.ASC
    return BasicMedia.objects.resolve_direction(sort_by, None)


def get_enabled_home_media_types(user) -> list[str]:
    """Return enabled sidebar media types in stable display order."""
    return list(user.get_enabled_media_types())


def get_allowed_sort_choices(media_type: str, row_type: str) -> list[dict]:
    """Return sort options for a home row."""
    sort_choices: list[tuple[str, str]] = [
        (MediaSortChoices.SCORE, "Rating"),
        (MediaSortChoices.TITLE, "Title"),
        (MediaSortChoices.PROGRESS, "Progress"),
        (MediaSortChoices.RELEASE_DATE, "Release Date"),
        (MediaSortChoices.DATE_ADDED, "Date Added"),
        (MediaSortChoices.START_DATE, "Start Date"),
        (MediaSortChoices.END_DATE, "Last Watched"),
    ]

    if media_type in CRITIC_RATING_MEDIA_TYPES:
        sort_choices.append((MediaSortChoices.CRITIC_RATING, "Critic Rating"))
    if media_type in AUTHOR_MEDIA_TYPES:
        sort_choices.append((MediaSortChoices.AUTHOR, "Author"))
    if media_type in POPULARITY_MEDIA_TYPES:
        sort_choices.append((MediaSortChoices.POPULARITY, "Popularity"))
    if media_type in RUNTIME_MEDIA_TYPES:
        sort_choices.append((MediaSortChoices.RUNTIME, "Runtime"))
        sort_choices.append((MediaSortChoices.TIME_WATCHED, "Time Watched"))
    if media_type in PLAYS_MEDIA_TYPES:
        sort_choices.append((MediaSortChoices.PLAYS, "Plays"))
    if media_type == MediaTypes.GAME.value:
        sort_choices.append((MediaSortChoices.TIME_TO_BEAT, "Time to Beat"))
    if media_type == MediaTypes.TV.value:
        sort_choices.append((MediaSortChoices.TIME_LEFT, "Time Left"))

    if row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY:
        sort_choices.extend(
            [
                (HomeSortChoices.UPCOMING, "Upcoming"),
                (HomeSortChoices.RECENT, "Recent"),
                (HomeSortChoices.COMPLETION, "Completion"),
                (HomeSortChoices.EPISODES_LEFT, "Episodes Left"),
            ],
        )

    deduped: list[dict] = []
    seen = set()
    for value, label in sort_choices:
        if value in seen:
            continue
        seen.add(value)
        deduped.append({"value": value, "label": label})
    return deduped


def _media_type_group_label(media_type: str) -> str:
    return app_tags.media_type_readable_plural(media_type)


def _default_library_sort(user, media_type: str) -> str:
    requested = getattr(user, "home_sort", HomeSortChoices.TITLE)
    allowed = {choice["value"] for choice in get_allowed_sort_choices(media_type, HomeScreenRowTypeChoices.LIBRARY_QUERY)}
    if requested in allowed:
        return requested
    return MediaSortChoices.TITLE


def _seeded_home_media_types(user) -> list[str]:
    """Return the enabled media types that should receive default Home rows."""
    return [
        media_type
        for media_type in get_enabled_home_media_types(user)
        if media_type != MediaTypes.TV.value
    ]


def _preferred_default_library_sort(user, media_type: str) -> str:
    """Return the Home-row default sort that best matches legacy Home behavior."""
    requested = _default_library_sort(user, media_type)
    if requested != HomeSortChoices.UPCOMING:
        return requested
    if media_type == MediaTypes.SEASON.value:
        return HomeSortChoices.UPCOMING
    return HomeSortChoices.RECENT


def _default_recent_row_direction() -> str:
    return DirectionChoices.DESC


def _build_default_rows_for_media_type(user, media_type: str) -> list[HomeScreenRow]:
    if media_type == MediaTypes.TV.value:
        return []

    sort_by = _preferred_default_library_sort(user, media_type)
    defaults = [
        HomeScreenRow(
            user=user,
            media_type=media_type,
            position=0,
            enabled=True,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            sort_by=sort_by,
            direction=resolve_home_row_direction(sort_by),
            filters=dict(HOME_QUERY_DEFAULT_FILTERS),
        ),
    ]
    if getattr(user, "show_planned_on_home", "disabled") != "disabled":
        planned_filters = dict(HOME_QUERY_DEFAULT_FILTERS)
        planned_filters["status"] = Status.PLANNING.value
        defaults.append(
            HomeScreenRow(
                user=user,
                media_type=media_type,
                position=len(defaults),
                enabled=True,
                row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
                sort_by=sort_by,
                direction=resolve_home_row_direction(sort_by),
                filters=planned_filters,
            ),
        )
    return defaults


def _row_signature(row: HomeScreenRow, media_type: str) -> dict:
    filters = {}
    custom_list_id = None
    if row.row_type == HomeScreenRowTypeChoices.LIBRARY_QUERY:
        filters = _normalized_filter_payload(row.filters or {}, media_type)
    elif row.row_type == HomeScreenRowTypeChoices.CUSTOM_LIST:
        custom_list_id = row.custom_list_id

    return {
        "enabled": row.enabled,
        "row_type": row.row_type,
        "sort_by": row.sort_by,
        "direction": row.direction,
        "filters": filters,
        "custom_list_id": custom_list_id,
    }


def _legacy_default_rows_for_media_type(user, media_type: str) -> list[HomeScreenRow]:
    sort_by = _default_library_sort(user, media_type)
    defaults = [
        HomeScreenRow(
            user=user,
            media_type=media_type,
            position=0,
            enabled=True,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            sort_by=sort_by,
            direction=resolve_home_row_direction(sort_by),
            filters=dict(HOME_QUERY_DEFAULT_FILTERS),
        ),
    ]
    if getattr(user, "show_planned_on_home", "disabled") != "disabled":
        planned_filters = dict(HOME_QUERY_DEFAULT_FILTERS)
        planned_filters["status"] = Status.PLANNING.value
        defaults.append(
            HomeScreenRow(
                user=user,
                media_type=media_type,
                position=len(defaults),
                enabled=True,
                row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
                sort_by=sort_by,
                direction=resolve_home_row_direction(sort_by),
                filters=planned_filters,
            ),
        )
    defaults.append(
        HomeScreenRow(
            user=user,
            media_type=media_type,
            position=len(defaults),
            enabled=True,
            row_type=HomeScreenRowTypeChoices.RECENTLY_UNRATED,
            sort_by=HomeSortChoices.RECENT,
            direction=_default_recent_row_direction(),
            filters={},
        ),
    )
    return defaults


def _rows_match_signature(existing_rows: list[HomeScreenRow], expected_rows: list[HomeScreenRow], media_type: str) -> bool:
    if len(existing_rows) != len(expected_rows):
        return False
    return all(
        _row_signature(existing, media_type) == _row_signature(expected, media_type)
        for existing, expected in zip(existing_rows, expected_rows, strict=False)
    )


def ensure_home_screen_rows(user) -> list[HomeScreenRow]:
    """Ensure each enabled media type has a default Home row set."""
    enabled_media_types = get_enabled_home_media_types(user)
    rows = list(
        user.home_screen_rows.select_related("custom_list").order_by("media_type", "position", "id"),
    )
    rows_by_media_type: dict[str, list[HomeScreenRow]] = defaultdict(list)
    for row in rows:
        rows_by_media_type[row.media_type].append(row)

    media_types_to_reset: list[str] = []
    replacement_rows: list[HomeScreenRow] = []
    for media_type in enabled_media_types:
        media_rows = rows_by_media_type.get(media_type, [])
        if not media_rows:
            continue
        legacy_defaults = _legacy_default_rows_for_media_type(user, media_type)
        if not _rows_match_signature(media_rows, legacy_defaults, media_type):
            continue
        desired_defaults = _build_default_rows_for_media_type(user, media_type)
        if _rows_match_signature(media_rows, desired_defaults, media_type):
            continue
        media_types_to_reset.append(media_type)
        replacement_rows.extend(desired_defaults)

    if media_types_to_reset:
        with transaction.atomic():
            HomeScreenRow.objects.filter(
                user=user,
                media_type__in=media_types_to_reset,
            ).delete()
            if replacement_rows:
                HomeScreenRow.objects.bulk_create(replacement_rows)
        rows = list(
            user.home_screen_rows.select_related("custom_list").order_by("media_type", "position", "id"),
        )

    existing_media_types = {row.media_type for row in rows}
    missing_media_types = [
        media_type
        for media_type in _seeded_home_media_types(user)
        if media_type not in existing_media_types
    ]
    if missing_media_types:
        HomeScreenRow.objects.bulk_create(
            [
                row
                for media_type in missing_media_types
                for row in _build_default_rows_for_media_type(user, media_type)
            ],
        )
        rows = list(
            user.home_screen_rows.select_related("custom_list").order_by("media_type", "position", "id"),
        )
    return rows


def _author_options_for_media_type(user, media_type: str) -> list[dict]:
    if media_type not in AUTHOR_MEDIA_TYPES:
        return []

    try:
        actual_model = apps.get_model("app", media_type)
    except LookupError:  # pragma: no cover - defensive
        return []

    item_ids = set(actual_model.objects.filter(user=user).values_list("item_id", flat=True))
    authors = set()
    for item in Item.objects.filter(id__in=item_ids).only("authors"):
        authors.update(smart_rules._extract_authors(item))
    return [
        {"value": value, "label": value}
        for value in sorted(authors, key=lambda entry: entry.lower())
    ]


def build_filter_field_data(user, media_type: str) -> list[dict]:
    """Return template-friendly filter field definitions for a media type."""
    filter_data = smart_rules.build_rule_filter_data(user, [media_type], "all", "")
    filter_data["authors"] = _author_options_for_media_type(user, media_type)
    filter_data["show_authors"] = media_type in AUTHOR_MEDIA_TYPES

    field_definitions = [
        {
            "key": "status",
            "label": "Status",
            "options": [
                {"value": "all", "label": "All"},
                {"value": Status.IN_PROGRESS.value, "label": Status.IN_PROGRESS.label},
                {"value": Status.COMPLETED.value, "label": Status.COMPLETED.label},
                {"value": Status.PLANNING.value, "label": Status.PLANNING.label},
                {"value": Status.PAUSED.value, "label": Status.PAUSED.label},
                {"value": Status.DROPPED.value, "label": Status.DROPPED.label},
            ],
        },
        {
            "key": "rating",
            "label": "Rating",
            "options": [
                {"value": "all", "label": "All"},
                {"value": "rated", "label": "Rated"},
                {"value": "not_rated", "label": "Not Rated"},
            ],
        },
        {
            "key": "collection",
            "label": "Collection",
            "options": [
                {"value": "all", "label": "All"},
                {"value": "collected", "label": "Collected"},
                {"value": "not_collected", "label": "Not Collected"},
            ],
        },
        {
            "key": "genre",
            "label": "Genre",
            "options": [{"value": "", "label": "Any"}] + [
                {"value": value, "label": value}
                for value in filter_data.get("genres", [])
            ],
        },
        {
            "key": "year",
            "label": "Year",
            "options": [{"value": "", "label": "Any"}] + filter_data.get("years", []),
        },
        {
            "key": "release",
            "label": "Release",
            "options": [
                {"value": "all", "label": "All"},
                {"value": "released", "label": "Released"},
                {"value": "not_released", "label": "Not Released"},
            ],
        },
        {
            "key": "source",
            "label": "Source",
            "options": [{"value": "", "label": "Any"}] + filter_data.get("sources", []),
        },
        {
            "key": "language",
            "label": "Language",
            "options": [{"value": "", "label": "Any"}] + filter_data.get("languages", []),
            "visible": filter_data.get("show_languages", False),
        },
        {
            "key": "country",
            "label": "Country",
            "options": [{"value": "", "label": "Any"}] + filter_data.get("countries", []),
            "visible": filter_data.get("show_countries", False),
        },
        {
            "key": "platform",
            "label": "Platform",
            "options": [{"value": "", "label": "Any"}] + filter_data.get("platforms", []),
            "visible": filter_data.get("show_platforms", False),
        },
        {
            "key": "origin",
            "label": "Origin",
            "options": [{"value": "", "label": "Any"}] + filter_data.get("origins", []),
            "visible": filter_data.get("show_origins", False),
        },
        {
            "key": "format",
            "label": "Format",
            "options": [{"value": "", "label": "Any"}] + filter_data.get("formats", []),
            "visible": filter_data.get("show_formats", False),
        },
        {
            "key": "author",
            "label": "Author",
            "options": [{"value": "", "label": "Any"}] + filter_data.get("authors", []),
            "visible": filter_data.get("show_authors", False),
        },
        {
            "key": "tag",
            "label": "Tag",
            "options": [{"value": "", "label": "Any"}] + [
                {"value": value, "label": value}
                for value in filter_data.get("tags", [])
            ],
        },
        {
            "key": "tag_exclude",
            "label": "Exclude Tag",
            "options": [{"value": "", "label": "Any"}] + [
                {"value": value, "label": value}
                for value in filter_data.get("tags", [])
            ],
        },
    ]

    supported_fields = SUPPORTED_FILTERS_BY_MEDIA_TYPE.get(media_type, set())
    visible_fields = []
    for field in field_definitions:
        if field["key"] not in supported_fields:
            continue
        if field.get("visible", True):
            visible_fields.append(field)
    return visible_fields


_SUMMARY_STATIC_FILTER_LABELS = {
    "rating": {
        "rated": "Rated",
        "not_rated": "Not Rated",
    },
    "collection": {
        "collected": "Collected",
        "not_collected": "Not Collected",
    },
    "release": {
        "released": "Released",
        "not_released": "Not Released",
    },
    "source": dict(Sources.choices),
    "format": {
        "hardcover": "Hardcover",
        "paperback": "Paperback",
        "ebook": "eBook",
        "audiobook": "Audiobook",
    },
}


def _summary_filter_label(key: str, value: str) -> str:
    label = _SUMMARY_STATIC_FILTER_LABELS.get(key, {}).get(value)
    if label:
        return label
    if key == "year" and value == "unknown":
        return "Unknown Year"
    return value


def _canonical_status_filter(value, default="all") -> str | None:
    """Normalize status aliases and labels to the stored choice value."""
    raw_value = str(value or "").strip()
    if not raw_value:
        return default
    return STATUS_FILTER_ALIASES.get(raw_value.casefold(), default)


def describe_library_query(filters: dict, user, media_type: str) -> str:
    """Return a compact query-row summary for settings and home."""
    normalized = _normalized_filter_payload(filters, media_type)

    status = normalized.get("status") or "all"
    if status == Status.IN_PROGRESS.value:
        parts = ["In Progress"]
    elif status == Status.PLANNING.value:
        parts = ["Planning"]
    elif status == Status.COMPLETED.value:
        parts = ["Completed"]
    elif status == Status.PAUSED.value:
        parts = ["Paused"]
    elif status == Status.DROPPED.value:
        parts = ["Dropped"]
    else:
        parts = ["Library"]

    for key in (
        "rating",
        "collection",
        "genre",
        "year",
        "release",
        "source",
        "language",
        "country",
        "platform",
        "origin",
        "format",
        "author",
        "tag",
        "tag_exclude",
    ):
        value = str(normalized.get(key, "") or "").strip()
        if not value or value in {"all", "Any"}:
            continue
        label = _summary_filter_label(key, value)
        if key == "tag_exclude":
            label = f"Not tagged {label}"
        parts.append(label)
        if len(parts) >= 4:
            break

    return " • ".join(parts)


def serialize_settings_sections(user) -> list[dict]:
    """Return Home Screen settings sections for the enabled sidebar media types."""
    rows = ensure_home_screen_rows(user)
    rows_by_media_type: dict[str, list[HomeScreenRow]] = defaultdict(list)
    for row in rows:
        rows_by_media_type[row.media_type].append(row)

    sections = []
    for media_type in get_enabled_home_media_types(user):
        media_rows = rows_by_media_type.get(media_type, [])
        sections.append(
            {
                "media_type": media_type,
                "label": _media_type_group_label(media_type),
                "icon_svg": str(app_tags.icon(media_type, False, "w-5 h-5 text-slate-300")),
                "sort_choices": {
                    HomeScreenRowTypeChoices.LIBRARY_QUERY: get_allowed_sort_choices(
                        media_type,
                        HomeScreenRowTypeChoices.LIBRARY_QUERY,
                    ),
                    HomeScreenRowTypeChoices.CUSTOM_LIST: get_allowed_sort_choices(
                        media_type,
                        HomeScreenRowTypeChoices.CUSTOM_LIST,
                    ),
                },
                "filter_fields": build_filter_field_data(user, media_type),
                "rows": [
                    {
                        "id": row.id,
                        "client_id": f"row-{row.id}",
                        "enabled": row.enabled,
                        "row_type": row.row_type,
                        "custom_list_id": row.custom_list_id,
                        "custom_list_name": row.custom_list.name if row.custom_list_id else "",
                        "sort_by": row.sort_by,
                        "direction": row.direction,
                        "filters": _normalized_filter_payload(row.filters, media_type),
                        "title": row_title(row, user),
                        "summary": row_summary(row, user),
                    }
                    for row in media_rows
                ],
            },
        )
    return sections


def row_title(row: HomeScreenRow, user) -> str:
    """Return the display title for a configured row."""
    if row.row_type == HomeScreenRowTypeChoices.CUSTOM_LIST:
        if row.custom_list_id and row.custom_list:
            return row.custom_list.name
        return "List / Smart List"
    if row.row_type == HomeScreenRowTypeChoices.RECENTLY_UNRATED:
        return RECENTLY_UNRATED_LABEL
    return describe_library_query(row.filters or {}, user, row.media_type)


def row_summary(row: HomeScreenRow, user) -> str:
    """Return a compact subtitle for a configured row."""
    if row.row_type == HomeScreenRowTypeChoices.CUSTOM_LIST:
        if row.custom_list_id and row.custom_list:
            return "List-backed row"
        return "Choose a list or smart list"
    if row.row_type == HomeScreenRowTypeChoices.RECENTLY_UNRATED:
        return "Recent unrated plays from this library"
    sort_choices = {
        choice["value"]: choice["label"]
        for choice in get_allowed_sort_choices(row.media_type, row.row_type)
    }
    sort_label = sort_choices.get(row.sort_by, row.sort_by.replace("_", " ").title())
    direction_label = "Ascending" if row.direction == DirectionChoices.ASC else "Descending"
    return f"Sorted by {sort_label} • {direction_label}"


def _normalized_filter_payload(filters: dict | None, media_type: str) -> dict:
    raw_filters = dict(filters or {})
    if "status" in raw_filters:
        raw_filters["status"] = _canonical_status_filter(
            raw_filters.get("status"),
            raw_filters.get("status"),
        )

    normalized = smart_rules.normalize_rule_payload(
        {
            "media_types": [media_type],
            **HOME_QUERY_DEFAULT_FILTERS,
            **raw_filters,
        },
        owner=None,
    )
    normalized.pop("media_types", None)
    normalized["status"] = _canonical_status_filter(
        raw_filters.get("status", normalized.get("status")),
        HOME_QUERY_DEFAULT_FILTERS["status"],
    )
    return {
        key: normalized.get(key, HOME_QUERY_DEFAULT_FILTERS.get(key, ""))
        for key in HOME_SCREEN_FILTER_KEYS
    }


def _row_payload_to_model(user, media_type: str, row_payload: dict, position: int) -> HomeScreenRow:
    row_type = str(row_payload.get("row_type") or "").strip()
    if row_type not in HomeScreenRowTypeChoices.values:
        raise HomeScreenValidationError(f"Unsupported row type for {media_type}.")

    enabled = bool(row_payload.get("enabled", True))
    custom_list = None
    filters = {}

    if row_type == HomeScreenRowTypeChoices.CUSTOM_LIST:
        try:
            custom_list_id = int(row_payload.get("custom_list_id") or 0)
        except (TypeError, ValueError):
            custom_list_id = 0
        custom_list = (
            CustomList.objects.get_user_lists(user)
            .filter(id=custom_list_id)
            .first()
        )
        if not custom_list:
            raise HomeScreenValidationError(f"Choose an accessible list for {media_type}.")
        sort_choices = get_allowed_sort_choices(media_type, row_type)
    elif row_type == HomeScreenRowTypeChoices.RECENTLY_UNRATED:
        sort_choices = []
    else:
        filters = validate_library_row_filters(row_payload.get("filters"), media_type)
        sort_choices = get_allowed_sort_choices(media_type, row_type)

    allowed_sort_values = {choice["value"] for choice in sort_choices}
    sort_by = str(row_payload.get("sort_by") or "").strip()
    if row_type == HomeScreenRowTypeChoices.RECENTLY_UNRATED:
        sort_by = HomeSortChoices.RECENT
        direction = _default_recent_row_direction()
    else:
        if sort_by not in allowed_sort_values:
            raise HomeScreenValidationError(f"Unsupported sort for {media_type}.")
        direction = resolve_home_row_direction(sort_by, row_payload.get("direction"))
        if direction not in DirectionChoices.values:
            raise HomeScreenValidationError(f"Unsupported direction for {media_type}.")

    return HomeScreenRow(
        user=user,
        media_type=media_type,
        position=position,
        enabled=enabled,
        row_type=row_type,
        custom_list=custom_list,
        sort_by=sort_by,
        direction=direction,
        filters=filters,
    )


def validate_library_row_filters(raw_filters: dict | None, media_type: str) -> dict:
    """Validate one library-query filter payload."""
    if raw_filters is None:
        raw_filters = {}
    if not isinstance(raw_filters, dict):
        raise HomeScreenValidationError("Library row filters must be an object.")

    supported = SUPPORTED_FILTERS_BY_MEDIA_TYPE.get(media_type, set())
    for key, value in raw_filters.items():
        if key not in HOME_SCREEN_FILTER_KEYS:
            raise HomeScreenValidationError(f"Unsupported filter '{key}' for {media_type}.")
        if key not in supported and str(value or "").strip():
            raise HomeScreenValidationError(f"Filter '{key}' is not available for {media_type}.")

    normalized = _normalized_filter_payload(raw_filters, media_type)
    raw_status = str(raw_filters.get("status", "") or "").strip()
    if raw_status:
        canonical_status = _canonical_status_filter(raw_status, None)
        if canonical_status not in STATUS_FILTER_VALUES:
            raise HomeScreenValidationError(f"Unsupported status filter for {media_type}.")
        normalized["status"] = canonical_status
    raw_rating = str(raw_filters.get("rating", normalized["rating"]) or "").strip().lower()
    if raw_rating and raw_rating not in {"all", "rated", "not_rated"}:
        raise HomeScreenValidationError(f"Unsupported rating filter for {media_type}.")
    raw_collection = str(raw_filters.get("collection", normalized["collection"]) or "").strip().lower()
    if raw_collection and raw_collection not in {"all", "collected", "not_collected"}:
        raise HomeScreenValidationError(f"Unsupported collection filter for {media_type}.")
    raw_release = str(raw_filters.get("release", normalized["release"]) or "").strip().lower()
    if raw_release and raw_release not in {"all", "released", "not_released"}:
        raise HomeScreenValidationError(f"Unsupported release filter for {media_type}.")
    raw_year = str(raw_filters.get("year", normalized["year"]) or "").strip().lower()
    if raw_year and raw_year != "unknown" and not raw_year.isdigit():
        raise HomeScreenValidationError(f"Unsupported year filter for {media_type}.")
    raw_source = str(raw_filters.get("source", normalized["source"]) or "").strip().lower()
    if raw_source and raw_source not in Sources.values:
        raise HomeScreenValidationError(f"Unsupported source filter for {media_type}.")
    return normalized


def save_home_screen_configuration(user, raw_payload: str) -> None:
    """Validate and persist Home screen settings from a JSON payload."""
    try:
        parsed_payload = json.loads(raw_payload or "[]")
    except (TypeError, ValueError) as exc:
        raise HomeScreenValidationError("Home Screen settings payload is invalid JSON.") from exc

    if not isinstance(parsed_payload, list):
        raise HomeScreenValidationError("Home Screen settings payload must be a list.")

    allowed_media_types = set(get_enabled_home_media_types(user))
    replacement_rows: list[HomeScreenRow] = []
    seen_recent_rows: set[str] = set()

    for section in parsed_payload:
        if not isinstance(section, dict):
            raise HomeScreenValidationError("Invalid Home Screen section payload.")
        media_type = str(section.get("media_type") or "").strip()
        if media_type not in allowed_media_types:
            raise HomeScreenValidationError(f"Unsupported media type '{media_type}'.")
        rows = section.get("rows")
        if not isinstance(rows, list):
            raise HomeScreenValidationError(f"Rows payload for {media_type} must be a list.")

        for index, row_payload in enumerate(rows):
            if not isinstance(row_payload, dict):
                raise HomeScreenValidationError(f"Row {index + 1} for {media_type} is invalid.")
            model_row = _row_payload_to_model(user, media_type, row_payload, index)
            if model_row.row_type == HomeScreenRowTypeChoices.RECENTLY_UNRATED:
                if media_type in seen_recent_rows:
                    raise HomeScreenValidationError(
                        f"Only one '{RECENTLY_UNRATED_LABEL}' row is allowed for {media_type}.",
                    )
                seen_recent_rows.add(media_type)
            replacement_rows.append(model_row)

    with transaction.atomic():
        HomeScreenRow.objects.filter(user=user, media_type__in=allowed_media_types).delete()
        HomeScreenRow.objects.bulk_create(replacement_rows)


def search_home_screen_lists(user, query: str, media_type: str) -> list[dict]:
    """Return accessible list suggestions for Home row selection."""
    queryset = CustomList.objects.get_user_lists(user).order_by("name")
    normalized_query = str(query or "").strip()
    if normalized_query:
        queryset = queryset.filter(name__icontains=normalized_query)
    return [
        {
            "id": custom_list.id,
            "name": custom_list.name,
            "is_smart": custom_list.is_smart,
            "label": f"{custom_list.name} ({'Smart list' if custom_list.is_smart else 'List'})",
        }
        for custom_list in queryset[:12]
    ]


def _item_matches_home_media_type(item: Item, media_type: str) -> bool:
    library_media_type = getattr(item, "library_media_type", "") or ""
    return library_media_type == media_type or item.media_type == media_type


def _annotate_home_card_images(media_items):
    """Annotate season cards with show-poster fallbacks when needed."""
    season_items = [
        media
        for media in media_items
        if getattr(getattr(media, "item", None), "media_type", None) == MediaTypes.SEASON.value
    ]
    if season_items:
        BasicMedia.objects._fix_missing_season_images(season_items)


def _build_recent_music_album_entries(media_items: list[object]) -> list[HomeRowEntry]:
    albums_by_id = {}
    album_play_counts = defaultdict(int)
    album_last_played = {}
    album_primary_track = {}

    for track in media_items:
        album = getattr(track, "album", None)
        if not album:
            continue
        album_id = album.id
        albums_by_id[album_id] = album
        play_count = getattr(track, "repeats", None) or 1
        album_play_counts[album_id] += play_count
        last_played = getattr(track, "last_played_at", None) or getattr(track, "created_at", None)
        if album_id not in album_last_played or last_played > album_last_played[album_id]:
            album_last_played[album_id] = last_played
            album_primary_track[album_id] = track

    class AlbumAdapter:
        def __init__(self, album, play_count, last_played_at, primary_track):
            self.album = album
            self.id = album.id
            self.play_count = play_count
            self.last_played_at = last_played_at
            self.created_at = last_played_at
            self.status = None
            self.end_date = last_played_at
            self.next_event = None
            self.score = None
            self.title = album.title
            album_media_id = f"album_{album.id}"
            self.item, _ = Item.objects.get_or_create(
                media_id=album_media_id,
                source=Sources.MANUAL.value,
                media_type=MediaTypes.MUSIC.value,
                defaults={
                    "title": album.title,
                    "image": album.image or settings.IMG_NONE,
                },
            )
            desired_image = album.image or settings.IMG_NONE
            if self.item.title != album.title or self.item.image != desired_image:
                self.item.title = album.title
                self.item.image = desired_image
                self.item.save(update_fields=["title", "image"])
            self.primary_track = primary_track

    entries = [
        HomeRowEntry(
            item=adapter.item,
            media=adapter,
            show_progress_controls=False,
        )
        for adapter in [
            AlbumAdapter(
                albums_by_id[album_id],
                album_play_counts[album_id],
                album_last_played[album_id],
                album_primary_track[album_id],
            )
            for album_id in albums_by_id
        ]
    ]
    entries.sort(
        key=lambda entry: getattr(entry.media, "last_played_at", None)
        or getattr(entry.media, "created_at", None),
        reverse=True,
    )
    return entries


def _media_lookup_for_items(user, items: list[Item]) -> dict[int, object]:
    items_by_media_type: dict[str, list[Item]] = defaultdict(list)
    for item in items:
        items_by_media_type[item.media_type].append(item)

    lookup: dict[int, object] = {}
    for actual_media_type, type_items in items_by_media_type.items():
        model = apps.get_model("app", actual_media_type)
        queryset = model.objects.filter(
            user=user,
            item_id__in=[item.id for item in type_items],
        ).select_related("item")
        if actual_media_type == MediaTypes.PODCAST.value:
            queryset = queryset.select_related("show")
        if actual_media_type == MediaTypes.MUSIC.value:
            queryset = queryset.select_related("album")
        queryset = BasicMedia.objects._apply_prefetch_related(queryset, actual_media_type)
        media_entries = list(queryset)

        grouped_entries: dict[int, list[object]] = defaultdict(list)
        for media_entry in media_entries:
            grouped_entries[media_entry.item_id].append(media_entry)

        deduped_entries = []
        for item_id, entries in grouped_entries.items():
            if actual_media_type == MediaTypes.PODCAST.value:
                entries = sorted(entries, key=lambda entry: entry.created_at, reverse=True)
            else:
                entries = sorted(entries, key=lambda entry: entry.created_at, reverse=True)
            primary_entry = entries[0]
            if actual_media_type != MediaTypes.PODCAST.value and len(entries) > 1:
                BasicMedia.objects._aggregate_item_data(primary_entry, entries)
            if actual_media_type == MediaTypes.PODCAST.value:
                primary_entry.use_podcast_show = bool(getattr(primary_entry, "show", None))
            lookup[item_id] = primary_entry
            deduped_entries.append(primary_entry)

        if deduped_entries:
            BasicMedia.objects.annotate_max_progress(deduped_entries, actual_media_type)
            _annotate_home_card_images(deduped_entries)

    return lookup


def _wrap_media_entries(media_entries: list[object]) -> list[HomeRowEntry]:
    _annotate_home_card_images(media_entries)
    return [
        HomeRowEntry(
            item=media.item,
            media=media,
            use_podcast_show=bool(getattr(media, "use_podcast_show", False)),
            podcast_show=getattr(media, "show", None),
            show_progress_controls=True,
        )
        for media in media_entries
    ]


def _coerce_numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            return timezone.localtime(value)
        return value.replace(tzinfo=UTC)
    return None


def _entry_title(entry: HomeRowEntry) -> str:
    return str(getattr(entry.item, "title", "") or "")


def _entry_media(entry: HomeRowEntry):
    return entry.media


def _entry_score(entry: HomeRowEntry):
    media = _entry_media(entry)
    if not media:
        return None
    aggregated = getattr(media, "aggregated_score", None)
    if aggregated is not None:
        return aggregated
    return getattr(media, "score", None)


def _entry_progress(entry: HomeRowEntry):
    media = _entry_media(entry)
    if not media:
        return None
    aggregated = getattr(media, "aggregated_progress", None)
    if aggregated is not None:
        return aggregated
    return getattr(media, "progress", None)


def _entry_authors(entry: HomeRowEntry):
    return smart_rules._extract_authors(entry.item)


def _entry_recent_timestamp(entry: HomeRowEntry):
    media = _entry_media(entry)
    if not media:
        return None
    candidate = (
        getattr(media, "last_played_at", None)
        or getattr(media, "progressed_at", None)
        or getattr(media, "created_at", None)
    )
    dt_value = _coerce_datetime(candidate)
    return dt_value.timestamp() if dt_value else None


def _entry_start_timestamp(entry: HomeRowEntry):
    media = _entry_media(entry)
    if not media:
        return None
    candidate = getattr(media, "aggregated_start_date", None) or getattr(media, "start_date", None)
    dt_value = _coerce_datetime(candidate)
    return dt_value.timestamp() if dt_value else None


def _entry_end_timestamp(entry: HomeRowEntry):
    media = _entry_media(entry)
    if not media:
        return None
    candidate = getattr(media, "aggregated_end_date", None) or getattr(media, "end_date", None)
    dt_value = _coerce_datetime(candidate)
    return dt_value.timestamp() if dt_value else None


def _entry_date_added_timestamp(entry: HomeRowEntry):
    media = _entry_media(entry)
    if not media:
        return None
    dt_value = _coerce_datetime(getattr(media, "created_at", None))
    return dt_value.timestamp() if dt_value else None


def _entry_release_timestamp(entry: HomeRowEntry):
    dt_value = _coerce_datetime(getattr(entry.item, "release_datetime", None))
    return dt_value.timestamp() if dt_value else None


def _entry_next_event_timestamp(entry: HomeRowEntry):
    media = _entry_media(entry)
    next_event = getattr(media, "next_event", None) if media else None
    dt_value = _coerce_datetime(getattr(next_event, "datetime", None))
    return dt_value.timestamp() if dt_value else None


def _sort_numeric(entries: list[HomeRowEntry], value_fn, direction: str) -> list[HomeRowEntry]:
    descending = direction == DirectionChoices.DESC
    return sorted(
        entries,
        key=lambda entry: (
            value_fn(entry) is None,
            0 if value_fn(entry) is None else (-value_fn(entry) if descending else value_fn(entry)),
            _entry_title(entry).lower(),
        ),
    )


def _sort_string(entries: list[HomeRowEntry], value_fn, direction: str) -> list[HomeRowEntry]:
    with_value = []
    without_value = []
    for entry in entries:
        value = str(value_fn(entry) or "").strip()
        if value:
            with_value.append(entry)
        else:
            without_value.append(entry)
    with_value.sort(
        key=lambda entry: (
            str(value_fn(entry) or "").lower(),
            _entry_title(entry).lower(),
        ),
        reverse=direction == DirectionChoices.DESC,
    )
    without_value.sort(key=lambda entry: _entry_title(entry).lower())
    return with_value + without_value


def sort_home_entries(entries: list[HomeRowEntry], sort_by: str, direction: str) -> list[HomeRowEntry]:
    """Sort Home row wrappers with graceful handling for list rows lacking media."""
    media_entries = [entry.media for entry in entries if entry.media]
    if sort_by == HomeSortChoices.UPCOMING and media_entries:
        BasicMedia.objects._annotate_next_event(media_entries)
        with_events = []
        without_events = []
        for entry in entries:
            if _entry_next_event_timestamp(entry) is None:
                without_events.append(entry)
            else:
                with_events.append(entry)

        descending = direction == DirectionChoices.DESC

        def _upcoming_key(entry: HomeRowEntry):
            next_event_timestamp = _entry_next_event_timestamp(entry) or 0
            recent_timestamp = _entry_recent_timestamp(entry)
            return (
                -next_event_timestamp if descending else next_event_timestamp,
                0 if recent_timestamp is None else -recent_timestamp,
                _entry_title(entry).lower(),
            )

        with_events.sort(key=_upcoming_key)
        without_events.sort(
            key=lambda entry: (
                _entry_recent_timestamp(entry) is None,
                0
                if _entry_recent_timestamp(entry) is None
                else -_entry_recent_timestamp(entry),
                _entry_title(entry).lower(),
            ),
        )
        return with_events + without_events
    if sort_by == HomeSortChoices.RECENT:
        return _sort_numeric(entries, _entry_recent_timestamp, direction)
    if sort_by == HomeSortChoices.COMPLETION:
        def completion_value(entry):
            media = _entry_media(entry)
            progress = _entry_progress(entry)
            max_progress = getattr(media, "max_progress", None) if media else None
            if progress is None or not max_progress:
                return None
            return (progress / max_progress) * 100

        return _sort_numeric(entries, completion_value, direction)
    if sort_by == HomeSortChoices.EPISODES_LEFT:
        def episodes_left(entry):
            media = _entry_media(entry)
            if not media:
                return None
            max_progress = getattr(media, "max_progress", None)
            progress = _entry_progress(entry)
            if max_progress is None or progress is None:
                return None
            return max_progress - progress

        return _sort_numeric(entries, episodes_left, direction)
    if sort_by == MediaSortChoices.SCORE:
        return _sort_numeric(entries, _entry_score, direction)
    if sort_by == MediaSortChoices.CRITIC_RATING:
        return _sort_numeric(entries, lambda entry: _coerce_numeric(getattr(entry.item, "provider_rating", None)), direction)
    if sort_by == MediaSortChoices.TITLE:
        return sorted(entries, key=lambda entry: _entry_title(entry).lower(), reverse=direction == DirectionChoices.DESC)
    if sort_by == MediaSortChoices.AUTHOR:
        return _sort_string(entries, lambda entry: _entry_authors(entry)[0] if _entry_authors(entry) else "", direction)
    if sort_by == MediaSortChoices.POPULARITY:
        return _sort_numeric(
            entries,
            lambda entry: _coerce_numeric(getattr(entry.item, "provider_popularity", None))
            if getattr(entry.item, "provider_popularity", None) is not None
            else (
                None
                if getattr(entry.item, "trakt_popularity_rank", None) is None
                else -float(getattr(entry.item, "trakt_popularity_rank"))
            ),
            direction,
        )
    if sort_by == MediaSortChoices.PROGRESS:
        return _sort_numeric(entries, _entry_progress, direction)
    if sort_by == MediaSortChoices.RUNTIME:
        return _sort_numeric(entries, lambda entry: _coerce_numeric(getattr(_entry_media(entry), "total_runtime_minutes", None)), direction)
    if sort_by == MediaSortChoices.TIME_TO_BEAT:
        return _sort_numeric(entries, lambda entry: _coerce_numeric(getattr(entry.item, "game_time_to_beat_minutes", None)), direction)
    if sort_by == MediaSortChoices.PLAYS:
        return _sort_numeric(entries, _entry_progress, direction)
    if sort_by == MediaSortChoices.TIME_WATCHED:
        return _sort_numeric(entries, lambda entry: _coerce_numeric(getattr(_entry_media(entry), "time_watched_minutes", None)), direction)
    if sort_by == MediaSortChoices.RELEASE_DATE:
        return _sort_numeric(entries, _entry_release_timestamp, direction)
    if sort_by == MediaSortChoices.DATE_ADDED:
        return _sort_numeric(entries, _entry_date_added_timestamp, direction)
    if sort_by == MediaSortChoices.START_DATE:
        return _sort_numeric(entries, _entry_start_timestamp, direction)
    if sort_by == MediaSortChoices.END_DATE:
        return _sort_numeric(entries, _entry_end_timestamp, direction)
    if sort_by == MediaSortChoices.TIME_LEFT:
        def time_left(entry):
            media = _entry_media(entry)
            if not media:
                return None
            max_progress = getattr(media, "max_progress", None)
            progress = _entry_progress(entry)
            if max_progress is None or progress is None:
                return None
            return max_progress - progress

        return _sort_numeric(entries, time_left, direction)
    return sorted(entries, key=lambda entry: _entry_title(entry).lower())


def _library_query_entries(user, row: HomeScreenRow) -> list[HomeRowEntry]:
    normalized_filters = _normalized_filter_payload(row.filters or {}, row.media_type)
    rule_payload = {
        "media_types": [row.media_type],
        **normalized_filters,
    }
    item_ids = smart_rules.collect_matching_item_ids(
        user,
        smart_rules.normalize_rule_payload(rule_payload, user),
    )
    if not item_ids:
        return []

    items = list(Item.objects.filter(id__in=item_ids))
    media_lookup = _media_lookup_for_items(user, items)
    entries = [
        HomeRowEntry(
            item=item,
            media=media_lookup.get(item.id),
            use_podcast_show=bool(getattr(media_lookup.get(item.id), "use_podcast_show", False)),
            podcast_show=getattr(media_lookup.get(item.id), "show", None),
            show_progress_controls=media_lookup.get(item.id) is not None,
        )
        for item in items
        if _item_matches_home_media_type(item, row.media_type)
    ]
    return sort_home_entries(entries, row.sort_by, row.direction)


def _custom_list_entries(user, row: HomeScreenRow) -> list[HomeRowEntry]:
    custom_list = row.custom_list
    if not custom_list:
        return []
    if custom_list.is_smart:
        custom_list.sync_smart_items()
        items = list(custom_list.get_smart_items_queryset())
    else:
        items = list(
            Item.objects.filter(customlistitem__custom_list=custom_list)
            .distinct()
            .order_by("customlistitem__date_added", "id"),
        )

    items = [item for item in items if _item_matches_home_media_type(item, row.media_type)]
    if not items:
        return []

    media_lookup = _media_lookup_for_items(user, items)
    entries = [
        HomeRowEntry(
            item=item,
            media=media_lookup.get(item.id),
            use_podcast_show=bool(getattr(media_lookup.get(item.id), "use_podcast_show", False)),
            podcast_show=getattr(media_lookup.get(item.id), "show", None),
            show_progress_controls=media_lookup.get(item.id) is not None,
        )
        for item in items
    ]
    return sort_home_entries(entries, row.sort_by, row.direction)


def _recently_unrated_entries(user, row: HomeScreenRow) -> list[HomeRowEntry]:
    media_items = [
        media
        for media in BasicMedia.objects.get_recently_unrated(user, days=RECENTLY_UNRATED_DAYS)
        if _item_matches_home_media_type(media.item, row.media_type)
    ]
    if row.media_type == MediaTypes.MUSIC.value:
        return _build_recent_music_album_entries(media_items)
    entries = _wrap_media_entries(media_items)
    return sort_home_entries(entries, row.sort_by, row.direction)


def build_home_page_groups(
    user,
    items_limit: int,
    load_row_id: int | None = None,
    load_row_offset: int = 0,
    *,
    append_only: bool = False,
) -> list[dict]:
    """Build grouped home sections from persisted Home rows."""
    rows = ensure_home_screen_rows(user)
    enabled_media_types = get_enabled_home_media_types(user)
    rows_by_media_type: dict[str, list[HomeScreenRow]] = defaultdict(list)
    for row in rows:
        if row.enabled:
            rows_by_media_type[row.media_type].append(row)

    groups = []
    for media_type in enabled_media_types:
        row_sections = []
        for row in rows_by_media_type.get(media_type, []):
            if row.row_type == HomeScreenRowTypeChoices.CUSTOM_LIST:
                entries = _custom_list_entries(user, row)
            elif row.row_type == HomeScreenRowTypeChoices.RECENTLY_UNRATED:
                entries = _recently_unrated_entries(user, row)
            else:
                entries = _library_query_entries(user, row)

            if not entries:
                continue

            batch_start = load_row_offset if load_row_id == row.id and append_only else 0
            batch_end = batch_start + items_limit
            section_entries = entries[batch_start:batch_end]
            loaded_count = min(len(entries), batch_start + len(section_entries))
            row_sections.append(
                {
                    "row_id": row.id,
                    "title": row_title(row, user),
                    "summary": row_summary(row, user),
                    "items": section_entries,
                    "total": len(entries),
                    "loaded_count": loaded_count,
                    "show_played_chip": row.row_type == HomeScreenRowTypeChoices.RECENTLY_UNRATED,
                    "card_width_class": "w-52" if media_type in SQUARE_HOME_MEDIA_TYPES else "w-44",
                    "grid_class": "media-grid media-grid-square"
                    if media_type in SQUARE_HOME_MEDIA_TYPES
                    else "media-grid",
                },
            )
        if row_sections:
            groups.append(
                {
                    "media_type": media_type,
                    "label": _media_type_group_label(media_type),
                    "rows": row_sections,
                },
            )
    return groups
