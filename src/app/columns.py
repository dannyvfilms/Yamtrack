from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.models import MediaTypes

TableType = str
VisibilityRule = Callable[[str, str, Any], bool]


@dataclass(frozen=True)
class ColumnDef:
    """Definition for a table column in media/artist list views."""

    key: str
    label: str
    th_classes: str
    td_classes: str
    cell_template: str
    table_types: tuple[TableType, ...]
    is_visible: VisibilityRule | None = None
    default_order: int = 0
    user_hideable: bool = True


def _show_progress(media_type: str, current_sort: str, _user: Any) -> bool:
    return media_type != MediaTypes.MOVIE.value and current_sort != "time_left"


def _show_episodes_left(media_type: str, current_sort: str, _user: Any) -> bool:
    return media_type == MediaTypes.TV.value and current_sort == "time_left"


def _show_time_left(media_type: str, current_sort: str, _user: Any) -> bool:
    return media_type == MediaTypes.TV.value and current_sort == "time_left"


def _show_game_time_to_beat(media_type: str, _current_sort: str, _user: Any) -> bool:
    return media_type == MediaTypes.GAME.value


def _show_runtime(media_type: str, _current_sort: str, _user: Any) -> bool:
    return media_type in (
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    )


def _show_time_watched(media_type: str, _current_sort: str, _user: Any) -> bool:
    return media_type in (
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    )


def _show_popularity(media_type: str, _current_sort: str, _user: Any) -> bool:
    return media_type in (
        MediaTypes.MOVIE.value,
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    )


def _show_last_watched(media_type: str, current_sort: str, _user: Any) -> bool:
    return media_type == MediaTypes.TV.value and current_sort != "time_left"


def _show_author(media_type: str, _current_sort: str, _user: Any) -> bool:
    return media_type in (
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
        MediaTypes.MANGA.value,
    )


MEDIA_COLUMNS: list[ColumnDef] = [
    ColumnDef(
        key="image",
        label="",
        th_classes="p-2 w-15",
        td_classes="p-2 relative",
        cell_template="app/components/cells/media_image_cell.html",
        table_types=("media",),
        default_order=10,
        user_hideable=False,
    ),
    ColumnDef(
        key="title",
        label="Title",
        th_classes="p-2 pe-8 w-2/5",
        td_classes="p-2 pe-8 font-medium",
        cell_template="app/components/cells/media_title_cell.html",
        table_types=("media",),
        default_order=20,
        user_hideable=False,
    ),
    ColumnDef(
        key="author",
        label="Author",
        th_classes="p-2 pe-6",
        td_classes="p-2 pe-6 text-gray-200",
        cell_template="app/components/cells/media_author_cell.html",
        table_types=("media",),
        is_visible=_show_author,
        default_order=25,
    ),
    ColumnDef(
        key="media_type",
        label="Media Type",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/media_media_type_cell.html",
        table_types=("list",),
        default_order=27,
    ),
    ColumnDef(
        key="score",
        label="Score",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/media_score_cell.html",
        table_types=("media",),
        default_order=30,
    ),
    ColumnDef(
        key="progress",
        label="Progress",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/progress_cell.html",
        table_types=("media",),
        is_visible=_show_progress,
        default_order=40,
    ),
    ColumnDef(
        key="episodes_left",
        label="Episodes Left",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/episodes_left_cell.html",
        table_types=("media",),
        is_visible=_show_episodes_left,
        default_order=50,
    ),
    ColumnDef(
        key="time_left",
        label="Time Left",
        th_classes="p-2 text-center w-24",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/time_left_cell.html",
        table_types=("media",),
        is_visible=_show_time_left,
        default_order=60,
    ),
    ColumnDef(
        key="time_to_beat",
        label="Time to Beat",
        th_classes="p-2 text-center w-28",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/time_to_beat_cell.html",
        table_types=("media",),
        is_visible=_show_game_time_to_beat,
        default_order=65,
    ),
    ColumnDef(
        key="runtime",
        label="Runtime",
        th_classes="p-2 text-center w-28",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/runtime_cell.html",
        table_types=("media",),
        is_visible=_show_runtime,
        default_order=66,
    ),
    ColumnDef(
        key="time_watched",
        label="Time Watched",
        th_classes="p-2 text-center w-28",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/time_watched_cell.html",
        table_types=("media",),
        is_visible=_show_time_watched,
        default_order=67,
    ),
    ColumnDef(
        key="popularity",
        label="Popularity",
        th_classes="p-2 text-center w-24",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/popularity_cell.html",
        table_types=("media",),
        is_visible=_show_popularity,
        default_order=68,
    ),
    ColumnDef(
        key="last_watched",
        label="Last Watched",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/last_watched_cell.html",
        table_types=("media",),
        is_visible=_show_last_watched,
        default_order=70,
    ),
    ColumnDef(
        key="status",
        label="Status",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/media_status_cell.html",
        table_types=("media",),
        default_order=80,
    ),
    ColumnDef(
        key="release_date",
        label="Release Date",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/media_release_date_cell.html",
        table_types=("media",),
        default_order=85,
    ),
    ColumnDef(
        key="date_added",
        label="Date Added",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/media_date_added_cell.html",
        table_types=("media",),
        default_order=87,
    ),
    ColumnDef(
        key="start_date",
        label="Start Date",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/media_start_date_cell.html",
        table_types=("media",),
        default_order=90,
    ),
    ColumnDef(
        key="end_date",
        label="End Date",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/media_end_date_cell.html",
        table_types=("media",),
        default_order=100,
    ),
    ColumnDef(
        key="image",
        label="",
        th_classes="p-2 w-15",
        td_classes="p-2 relative",
        cell_template="app/components/cells/artist_image_cell.html",
        table_types=("artist",),
        default_order=10,
        user_hideable=False,
    ),
    ColumnDef(
        key="artist_name",
        label="Artist",
        th_classes="p-2 pe-8 w-2/5",
        td_classes="p-2 pe-8 font-medium",
        cell_template="app/components/cells/artist_name_cell.html",
        table_types=("artist",),
        default_order=20,
        user_hideable=False,
    ),
    ColumnDef(
        key="score",
        label="Score",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/artist_score_cell.html",
        table_types=("artist",),
        default_order=30,
    ),
    ColumnDef(
        key="status",
        label="Status",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/artist_status_cell.html",
        table_types=("artist",),
        default_order=40,
    ),
    ColumnDef(
        key="release_date",
        label="Release Date",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/artist_release_date_cell.html",
        table_types=("artist",),
        default_order=45,
    ),
    ColumnDef(
        key="date_added",
        label="Date Added",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/artist_date_added_cell.html",
        table_types=("artist",),
        default_order=47,
    ),
    ColumnDef(
        key="start_date",
        label="Start Date",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/artist_start_date_cell.html",
        table_types=("artist",),
        default_order=50,
    ),
    ColumnDef(
        key="end_date",
        label="End Date",
        th_classes="p-2 text-center",
        td_classes="p-2 text-center",
        cell_template="app/components/cells/artist_end_date_cell.html",
        table_types=("artist",),
        default_order=60,
    ),
]


def _matches_table_type(column: ColumnDef, table_type: TableType) -> bool:
    if table_type in column.table_types:
        return True
    return table_type == "list" and "media" in column.table_types


def _get_media_type_prefs(
    user: Any,
    media_type: str,
    table_type: TableType,
) -> tuple[list[str], list[str]]:
    prefs = getattr(user, "table_column_prefs", None) or {}
    raw = prefs.get(media_type, {})
    if not isinstance(raw, dict):
        return [], []

    # Support both flat and table-type-scoped shapes.
    media_prefs = raw if "order" in raw or "hidden" in raw else raw.get(table_type, {})

    raw_order = media_prefs.get("order", [])
    raw_hidden = media_prefs.get("hidden", [])

    order = [str(key) for key in raw_order if isinstance(key, str)]
    hidden = [str(key) for key in raw_hidden if isinstance(key, str)]
    return order, hidden


def _base_columns(
    media_type: str,
    current_sort: str,
    user: Any,
    table_type: TableType,
) -> list[ColumnDef]:
    columns = [
        column for column in MEDIA_COLUMNS if _matches_table_type(column, table_type)
    ]
    visible_columns = []
    for column in columns:
        if column.is_visible and not column.is_visible(media_type, current_sort, user):
            continue
        visible_columns.append(column)
    return sorted(visible_columns, key=lambda col: col.default_order)


def _split_fixed_and_flex(
    columns: list[ColumnDef],
) -> tuple[list[ColumnDef], list[ColumnDef]]:
    fixed_columns = [column for column in columns if not column.user_hideable]
    flex_columns = [column for column in columns if column.user_hideable]
    return fixed_columns, flex_columns


def _resolve_order_and_hidden(
    media_type: str,
    current_sort: str,
    user: Any,
    table_type: TableType,
) -> tuple[list[ColumnDef], set[str]]:
    base_columns = _base_columns(media_type, current_sort, user, table_type)
    fixed_columns, flex_columns = _split_fixed_and_flex(base_columns)
    flex_by_key = {column.key: column for column in flex_columns}

    saved_order, saved_hidden = _get_media_type_prefs(user, media_type, table_type)

    ordered_flex_keys: list[str] = []
    for key in saved_order:
        if key in flex_by_key and key not in ordered_flex_keys:
            ordered_flex_keys.append(key)
    for column in flex_columns:
        if column.key not in ordered_flex_keys:
            ordered_flex_keys.append(column.key)

    hidden_keys = {
        key
        for key in saved_hidden
        if key in flex_by_key
    }

    ordered_columns = fixed_columns + [flex_by_key[key] for key in ordered_flex_keys]
    return ordered_columns, hidden_keys


def resolve_columns(
    media_type: str,
    current_sort: str,
    user: Any,
    table_type: TableType,
) -> list[ColumnDef]:
    """Resolve visible columns in final left-to-right order."""
    ordered_columns, hidden_keys = _resolve_order_and_hidden(
        media_type,
        current_sort,
        user,
        table_type,
    )
    return [column for column in ordered_columns if column.key not in hidden_keys]


def resolve_column_config(
    media_type: str,
    current_sort: str,
    user: Any,
    table_type: TableType,
) -> list[dict[str, Any]]:
    """Return ordered, user-hideable columns for the table config dropdown."""
    ordered_columns, hidden_keys = _resolve_order_and_hidden(
        media_type,
        current_sort,
        user,
        table_type,
    )
    config = []
    for column in ordered_columns:
        if not column.user_hideable:
            continue
        config.append(
            {
                "key": column.key,
                "label": column.label,
                "visible": column.key not in hidden_keys,
            },
        )
    return config


def sanitize_column_prefs(
    media_type: str,
    current_sort: str,
    user: Any,
    table_type: TableType,
    order: list[str],
    hidden: list[str],
) -> tuple[list[str], list[str]]:
    """Sanitize user prefs against current flexible (hideable) columns only."""
    base_columns = _base_columns(media_type, current_sort, user, table_type)
    _, flex_columns = _split_fixed_and_flex(base_columns)
    flex_by_key = {column.key: column for column in flex_columns}

    clean_order: list[str] = []
    for key in order:
        if key in flex_by_key and key not in clean_order:
            clean_order.append(key)
    for column in flex_columns:
        if column.key not in clean_order:
            clean_order.append(column.key)

    clean_hidden: list[str] = []
    for key in hidden:
        if key in flex_by_key and key not in clean_hidden:
            clean_hidden.append(key)

    return clean_order, clean_hidden


def resolve_default_column_config(
    media_type: str,
    current_sort: str,
    table_type: TableType,
) -> list[dict[str, Any]]:
    """Return default user-hideable column order for reset actions."""
    base_columns = _base_columns(
        media_type,
        current_sort,
        user=None,
        table_type=table_type,
    )
    return [
        {
            "key": column.key,
            "label": column.label,
            "visible": True,
        }
        for column in base_columns
        if column.user_hideable
    ]
