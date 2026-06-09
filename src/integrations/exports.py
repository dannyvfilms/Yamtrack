import csv
import json
import logging

from django.apps import apps
from django.db.models import Field, Prefetch

from app import helpers
from app.models import AlbumTracker, ArtistTracker, Episode, Item, MediaTypes, Season
from lists.models import CustomList

logger = logging.getLogger(__name__)


class Echo:
    """An object that implements just the write method of the file-like interface."""

    def write(self, value):
        """Write the value by returning it, instead of storing in a buffer."""
        return value


def _get_media_types_to_export(media_types=None):
    """Return the list of media type values to export.

    If *media_types* is ``None`` every type is included (original behaviour).
    Otherwise only the explicitly requested types **plus** their implicit
    children (season → episode, tv → season+episode) are returned.
    """
    if media_types is None:
        return list(MediaTypes.values) + ["music_artist", "music_album"]

    out = list(media_types)
    # If TV is selected, ensure season & episode are included
    if MediaTypes.TV.value in out:
        for child in (MediaTypes.SEASON.value, MediaTypes.EPISODE.value):
            if child not in out:
                out.append(child)
    # If season is selected, ensure episode is included
    if MediaTypes.SEASON.value in out and MediaTypes.EPISODE.value not in out:
        out.append(MediaTypes.EPISODE.value)
    # If music is selected, include artist and album trackers too
    if MediaTypes.MUSIC.value in out:
        if "music_artist" not in out:
            out.append("music_artist")
        if "music_album" not in out:
            out.append("music_album")
    return out


def generate_rows(user, media_types=None, include_lists=True):
    """Generate CSV rows.

    Parameters
    ----------
    user : User
        The user whose data is being exported.
    media_types : list[str] | None
        Restrict export to these media types.  ``None`` means *all*.
    include_lists : bool
        Whether to include custom lists in the export.
    """
    pseudo_buffer = Echo()
    writer = csv.writer(pseudo_buffer, quoting=csv.QUOTE_ALL)

    # Get fields
    fields = {
        "item": get_model_fields(Item),
        "track": get_track_fields(),
        "list": get_list_fields(),
    }

    # Yield header row
    yield writer.writerow(["row_type"] + fields["item"] + fields["track"] + fields["list"])

    prefetch_config = {
        MediaTypes.TV.value: Prefetch(
            "seasons",
            queryset=Season.objects.select_related("item").prefetch_related(
                Prefetch(
                    "episodes",
                    queryset=Episode.objects.select_related("item"),
                ),
            ),
        ),
        MediaTypes.SEASON.value: Prefetch(
            "episodes",
            queryset=Episode.objects.select_related("item"),
        ),
    }

    types_to_export = _get_media_types_to_export(media_types)

    # Yield data rows
    for media_type in MediaTypes.values:
        if media_type not in types_to_export:
            continue

        model = apps.get_model("app", media_type)

        filter_kwargs = (
            {"related_season__user": user}
            if media_type == MediaTypes.EPISODE.value
            else {"user": user}
        )

        queryset = model.objects.filter(**filter_kwargs).select_related("item")

        if media_type in prefetch_config:
            queryset = queryset.prefetch_related(prefetch_config[media_type])

        logger.debug("Streaming %ss to CSV", media_type)

        for media in queryset.iterator(chunk_size=500):
            row = (
                ["media"]
                + [getattr(media.item, field, "") for field in fields["item"]]
                + [getattr(media, field, "") for field in fields["track"]]
                + [""] * len(fields["list"])
            )

            if media_type == MediaTypes.GAME.value:
                # calculate index of progress field
                progress_index = fields["track"].index("progress")
                row[progress_index + 1 + len(fields["item"])] = helpers.minutes_to_hhmm(
                    media.progress,
                )

            yield writer.writerow(row)

        logger.debug("Finished streaming %ss to CSV", media_type)

    if "music_artist" in types_to_export:
        logger.debug("Streaming music_artists to CSV")
        for tracker in ArtistTracker.objects.filter(user=user).select_related("artist"):
            artist = tracker.artist
            item_vals = {
                "media_id": artist.musicbrainz_id or "",
                "source": "musicbrainz",
                "media_type": "music_artist",
                "title": artist.name,
                "image": artist.image or "",
                "library_media_type": "",
                "season_number": "",
                "episode_number": "",
            }
            track_vals = {
                "status": tracker.status,
                "score": tracker.score if tracker.score is not None else "",
                "notes": tracker.notes,
                "start_date": tracker.start_date.isoformat() if tracker.start_date else "",
                "end_date": tracker.end_date.isoformat() if tracker.end_date else "",
                "created_at": tracker.created_at.isoformat() if tracker.created_at else "",
            }
            row = (
                ["media"]
                + [item_vals.get(f, "") for f in fields["item"]]
                + [track_vals.get(f, "") for f in fields["track"]]
                + [""] * len(fields["list"])
            )
            yield writer.writerow(row)
        logger.debug("Finished streaming music_artists to CSV")

    if "music_album" in types_to_export:
        logger.debug("Streaming music_albums to CSV")
        for tracker in AlbumTracker.objects.filter(user=user).select_related("album"):
            album = tracker.album
            item_vals = {
                "media_id": album.musicbrainz_release_group_id or "",
                "source": "musicbrainz",
                "media_type": "music_album",
                "title": album.title,
                "image": album.image or "",
                "library_media_type": "",
                "season_number": "",
                "episode_number": "",
            }
            track_vals = {
                "status": tracker.status,
                "score": tracker.score if tracker.score is not None else "",
                "notes": tracker.notes,
                "start_date": tracker.start_date.isoformat() if tracker.start_date else "",
                "end_date": tracker.end_date.isoformat() if tracker.end_date else "",
                "created_at": tracker.created_at.isoformat() if tracker.created_at else "",
            }
            row = (
                ["media"]
                + [item_vals.get(f, "") for f in fields["item"]]
                + [track_vals.get(f, "") for f in fields["track"]]
                + [""] * len(fields["list"])
            )
            yield writer.writerow(row)
        logger.debug("Finished streaming music_albums to CSV")

    if not include_lists:
        return

    # Export custom lists owned by the user
    custom_lists = CustomList.objects.filter(owner=user).order_by("name")

    for custom_list in custom_lists:
        list_row = (
            ["list"]
            + [""] * len(fields["item"])
            + [""] * len(fields["track"])
            + [
                custom_list.id,
                custom_list.name,
                custom_list.description,
                json.dumps(custom_list.tags or []),
                custom_list.visibility,
                custom_list.allow_recommendations,
                custom_list.source,
                custom_list.source_id,
                "",
            ]
        )
        yield writer.writerow(list_row)

        list_items = (
            custom_list.customlistitem_set.select_related("item")
            .order_by("date_added", "pk")
        )
        for list_item in list_items:
            item = list_item.item
            list_item_row = (
                ["list_item"]
                + [getattr(item, field, "") for field in fields["item"]]
                + [""] * len(fields["track"])
                + [
                    custom_list.id,
                    custom_list.name,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    list_item.date_added.isoformat() if list_item.date_added else "",
                ]
            )
            yield writer.writerow(list_item_row)


def write_backup(user, media_types=None, include_lists=True):
    """Write a CSV backup to the configured backup directory.

    Returns the path to the created file.
    """
    from pathlib import Path

    from django.conf import settings
    from django.utils import timezone

    backup_dir = Path(settings.BACKUP_DIR) / str(user.username)
    backup_dir.mkdir(parents=True, exist_ok=True)

    now = timezone.localtime()
    filename = f"yamtrack_{now.strftime('%Y-%m-%d_%H%M%S')}.csv"
    filepath = backup_dir / filename

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        for row_data in generate_rows(user, media_types=media_types, include_lists=include_lists):
            f.write(row_data)

    logger.info("Backup written to %s for user %s", filepath, user.username)
    return str(filepath)


def get_model_fields(model):
    """Get a list of fields names from a model."""
    return [
        field.name
        for field in model._meta.get_fields()
        if isinstance(field, Field) and not field.auto_created and not field.is_relation
    ]


def get_track_fields():
    """Get a list of all track fields from all media models."""
    all_fields = []

    for media_type in MediaTypes.values:
        model = apps.get_model("app", media_type)
        for field in get_model_fields(model):
            if field not in all_fields:
                all_fields.append(field)

    # Put start_date and end_date next to each other
    # happens because Episode has end_date but not start_date
    if "start_date" in all_fields and "end_date" in all_fields:
        end_idx = all_fields.index("end_date")

        # Remove both dates
        all_fields.remove("start_date")
        all_fields.remove("end_date")
        # Insert them in the correct order at the earlier index
        all_fields.insert(end_idx, "end_date")
        all_fields.insert(end_idx, "start_date")

    for timestamp_field in ("created_at", "progressed_at"):
        if timestamp_field in all_fields:
            all_fields.remove(timestamp_field)
            all_fields.append(timestamp_field)

    return list(all_fields)


def get_list_fields():
    """Get list-specific export fields."""
    return [
        "list_uid",
        "list_name",
        "list_description",
        "list_tags",
        "list_visibility",
        "list_allow_recommendations",
        "list_source",
        "list_source_id",
        "list_item_date_added",
    ]
