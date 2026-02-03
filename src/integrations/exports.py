import csv
import json
import logging

from django.apps import apps
from django.db.models import Field, Prefetch

from app import helpers
from app.models import Episode, Item, MediaTypes, Season
from lists.models import CustomList

logger = logging.getLogger(__name__)


class Echo:
    """An object that implements just the write method of the file-like interface."""

    def write(self, value):
        """Write the value by returning it, instead of storing in a buffer."""
        return value


def generate_rows(user):
    """Generate CSV rows."""
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

    # Yield data rows
    for media_type in MediaTypes.values:
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

    # Export custom lists owned by the user
    custom_lists = (
        CustomList.objects.filter(owner=user)
        .prefetch_related("customlistitem_set__item")
        .order_by("name")
    )

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

        for list_item in custom_list.customlistitem_set.all():
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
