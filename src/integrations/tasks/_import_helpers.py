from io import BytesIO

from app.log_safety import exception_summary
from app.models import MediaTypes
from app.templatetags import app_tags
from integrations import plex as plex_api
from integrations.imports import helpers

ERROR_TITLE = "\n\n\n Couldn't import the following media: \n\n"
GOODREADS_IMPORT_TASK_NAME = "Import from Goodreads"
LEGACY_GOODREADS_IMPORT_TASK_NAMES = (
    "Import from GoodReads",
    "integrations.tasks.import_goodreads",
)


def _is_expected_plex_lookup_error(exc):
    """Return True for expected Plex library lookup failures that don't need tracebacks."""
    if isinstance(exc, plex_api.PlexClientError):
        return True

    summary = exception_summary(exc).lower()
    if "timeout" in summary or "timed out" in summary:
        return True

    exc_type = type(exc).__name__.lower()
    return "timeout" in exc_type


def _coerce_uploaded_file(file):
    """Normalize uploaded file task args to a binary file-like object."""
    if hasattr(file, "read"):
        try:
            file.seek(0)
        except (AttributeError, OSError):
            pass
        return file
    if isinstance(file, str):
        return BytesIO(file.encode("utf-8"))
    if isinstance(file, bytes):
        return BytesIO(file)
    msg = f"Unsupported uploaded file payload type: {type(file)!r}"
    raise TypeError(msg)


def format_media_type_display(count, media_type):
    """Format media type display with proper pluralization."""
    if count == 0:
        return None
    if count == 1:
        return f"{count} {dict(MediaTypes.choices).get(media_type, media_type)}"
    return f"{count} {app_tags.media_type_readable_plural(media_type)}"


def format_import_message(imported_counts, warning_messages=None):
    """Format the import result message based on counts and warnings."""
    parts = []

    # Handle music specially - show both play events and unique tracks
    music_play_events = imported_counts.get(MediaTypes.MUSIC.value, 0)
    music_unique_tracks = imported_counts.get("music_unique_tracks", 0)

    if music_play_events > 0:
        if music_unique_tracks > 0:
            # Show both play events and unique tracks
            parts.append(
                f"{music_play_events} music play event{'s' if music_play_events != 1 else ''} "
                f"({music_unique_tracks} unique track{'s' if music_unique_tracks != 1 else ''})",
            )
        else:
            # Fallback to standard format if unique tracks not available
            parts.append(format_media_type_display(music_play_events, MediaTypes.MUSIC.value))

    # Add other media types (excluding music which we handled above)
    media_type_values = set(MediaTypes.values)
    for media_type, count in imported_counts.items():
        if (
            media_type == MediaTypes.MUSIC.value
            or media_type == "music_unique_tracks"
            or media_type not in media_type_values
        ):
            continue
        formatted = format_media_type_display(count, media_type)
        if formatted:
            parts.append(formatted)

    parts = [p for p in parts if p is not None]

    if not parts:
        info_message = "No media was imported."
    else:
        info_message = f"Imported {helpers.join_with_commas_and(parts)}."

    metric_parts = []
    metric_mappings = [
        ("created", "created"),
        ("updated", "updated"),
        ("skipped_missing_ids", "skipped (missing IDs)"),
        ("skipped_existing", "skipped (existing)"),
        ("skipped_unknown_type", "skipped (unknown type)"),
        ("skipped_other_user", "skipped (other users)"),
    ]
    for key, label in metric_mappings:
        value = imported_counts.get(key)
        if value:
            metric_parts.append(f"{value} {label}")

    if metric_parts:
        info_message = f"{info_message} {helpers.join_with_commas_and(metric_parts)}."

    if warning_messages:
        return f"{info_message} {ERROR_TITLE} {warning_messages}"
    return info_message


def format_watchlist_sync_message(sync_counts, warning_messages=None):
    """Format the Plex watchlist sync result message."""
    created_parts = []
    movie_count = sync_counts.get(MediaTypes.MOVIE.value, 0)
    tv_count = sync_counts.get(MediaTypes.TV.value, 0)

    if movie_count:
        created_parts.append(
            f"{movie_count} movie{'s' if movie_count != 1 else ''}",
        )
    if tv_count:
        created_parts.append(
            f"{tv_count} TV show{'s' if tv_count != 1 else ''}",
        )

    if created_parts:
        info_message = (
            "Synced Plex watchlist. "
            f"Imported {helpers.join_with_commas_and(created_parts)}."
        )
    else:
        info_message = "Synced Plex watchlist. No new watchlist media was imported."

    metric_parts = []
    metric_mappings = [
        ("created", "created"),
        ("linked_existing", "linked to existing media"),
        ("removed", "removed from Planning"),
        ("deactivated", "deactivated"),
        ("skipped_missing_ids", "skipped (missing IDs)"),
        ("skipped_unknown_type", "skipped (unknown type)"),
        ("skipped_metadata", "skipped (metadata errors)"),
    ]
    for key, label in metric_mappings:
        value = sync_counts.get(key)
        if value:
            metric_parts.append(f"{value} {label}")

    if metric_parts:
        info_message = f"{info_message} {helpers.join_with_commas_and(metric_parts)}."

    if warning_messages:
        return f"{info_message} {ERROR_TITLE} {warning_messages}"
    return info_message
