import json
import logging
from collections import defaultdict
from csv import DictReader

from django.apps import apps
from django.conf import settings
from django.utils.dateparse import parse_datetime

import app
from app.log_safety import mapping_keys
from app import config
from app import forms as app_forms
from app.models import MediaTypes, Sources, Status
from app.providers import services
from app.templatetags import app_tags
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from lists.models import CustomList, CustomListItem

logger = logging.getLogger(__name__)


def _parse_bool(value):
    """Parse truthy values from CSV strings."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "t"}


def _parse_tags(value):
    """Parse list tags from JSON or comma-delimited string."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass
    return [tag.strip() for tag in str(value).split(",") if tag.strip()]


def _normalize_status(value):
    """Normalize status strings to match Status choices."""
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "":
        return ""
    lowered = raw.lower()

    for status in Status:
        if lowered in (status.value.lower(), status.label.lower()):
            return status.value

    aliases = {
        "inprogress": Status.IN_PROGRESS.value,
        "in-progress": Status.IN_PROGRESS.value,
        "on hold": Status.PAUSED.value,
        "hold": Status.PAUSED.value,
        "paused": Status.PAUSED.value,
        "plan": Status.PLANNING.value,
        "planned": Status.PLANNING.value,
        "plan to watch": Status.PLANNING.value,
        "plan to read": Status.PLANNING.value,
        "want to watch": Status.PLANNING.value,
        "watchlist": Status.PLANNING.value,
        "complete": Status.COMPLETED.value,
        "finished": Status.COMPLETED.value,
        "done": Status.COMPLETED.value,
        "abandoned": Status.DROPPED.value,
    }
    return aliases.get(lowered, raw)


def importer(file, user, mode):
    """Import media from CSV file using the class-based importer."""
    csv_importer = YamtrackImporter(file, user, mode)
    return csv_importer.import_data()


class YamtrackImporter:
    """Class to handle importing user data from CSV files."""

    def __init__(self, file, user, mode):
        """Initialize the importer with file, user, and mode.

        Args:
            file: Uploaded CSV file object
            user: Django user object to import data for
            mode (str): Import mode ("new" or "overwrite")
        """
        self.file = file
        self.user = user
        self.mode = mode
        self.warnings = []

        # Track existing media for "new" mode
        self.existing_media = helpers.get_existing_media(user)

        # Track media IDs to delete in overwrite mode
        self.to_delete = defaultdict(lambda: defaultdict(set))

        # Track bulk creation lists for each media type
        self.bulk_media = defaultdict(list)
        self.list_map = {}
        self.status_overrides = {
            MediaTypes.TV.value: {},
            MediaTypes.SEASON.value: {},
        }

        logger.info(
            "Initialized Yamtrack CSV importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Import all user data from the CSV file."""
        try:
            decoded_file = self.file.read().decode("utf-8").splitlines()
        except UnicodeDecodeError as e:
            msg = "Invalid file format. Please upload a CSV file."
            raise MediaImportError(msg) from e

        reader = DictReader(decoded_file)

        for row in reader:
            try:
                self._process_row(row)
            except services.ProviderAPIError as error:
                error_msg = (
                    f"Error processing entry with ID {row['media_id']} "
                    f"({app_tags.media_type_readable(row['media_type'])}): {error}"
                )
                self.warnings.append(error_msg)
                continue
            except Exception as error:
                error_msg = f"Error processing entry: {row}"
                raise MediaImportUnexpectedError(error_msg) from error

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        self._apply_status_overrides()

        imported_counts = {
            media_type: len(media_list)
            for media_type, media_list in self.bulk_media.items()
        }

        deduplicated_messages = "\n".join(dict.fromkeys(self.warnings))
        return imported_counts, deduplicated_messages

    def _apply_status_overrides(self):
        """Apply explicit TV/Season status values from the CSV after import."""
        tv_overrides = self.status_overrides.get(MediaTypes.TV.value, {})
        for (source, media_id), status in tv_overrides.items():
            if not status:
                continue
            app.models.TV.objects.filter(
                user=self.user,
                item__source=source,
                item__media_id=media_id,
            ).exclude(status=status).update(status=status)

        season_overrides = self.status_overrides.get(MediaTypes.SEASON.value, {})
        for (source, media_id, season_number), status in season_overrides.items():
            if not status:
                continue
            app.models.Season.objects.filter(
                user=self.user,
                item__source=source,
                item__media_id=media_id,
                item__season_number=season_number,
            ).exclude(status=status).update(status=status)

    def _process_row(self, row):
        """Process a single row from the CSV file."""
        row_type = (row.get("row_type") or "").strip().lower()
        if row_type in ("", "media"):
            self._process_media_row(row)
            return
        if row_type == "list":
            self._process_list_row(row)
            return
        if row_type == "list_item":
            self._process_list_item_row(row)
            return

        self.warnings.append(f"Skipping unknown row type: {row_type}")

    def _process_media_row(self, row):
        """Process a single media row from the CSV file."""
        media_type = (row.get("media_type") or "").strip().lower()
        row["media_type"] = media_type
        row["source"] = (row.get("source") or "").strip().lower()
        normalized_status = _normalize_status(row.get("status"))
        if normalized_status is not None:
            row["status"] = normalized_status

        season_number = (
            int(row["season_number"]) if row["season_number"] != "" else None
        )
        episode_number = (
            int(row["episode_number"]) if row["episode_number"] != "" else None
        )

        if row["progress"] == "":
            row["progress"] = 0

        parent_type = (
            MediaTypes.TV.value
            if media_type in (MediaTypes.SEASON.value, MediaTypes.EPISODE.value)
            else media_type
        )

        # Check if we should process this movie based on mode
        if not helpers.should_process_media(
            self.existing_media,
            self.to_delete,
            parent_type,
            row["source"],
            row["media_id"],
            self.mode,
        ):
            return

        if row["title"] == "" or row["image"] == "":
            self._handle_missing_metadata(
                row,
                media_type,
                season_number,
                episode_number,
            )

        item, _ = helpers.retry_on_lock(
            lambda: app.models.Item.objects.update_or_create(
                media_id=row["media_id"],
                source=row["source"],
                media_type=media_type,
                season_number=season_number,
                episode_number=episode_number,
                defaults={
                    "title": row["title"],
                    "image": row["image"],
                },
            ),
        )

        model = apps.get_model(app_label="app", model_name=media_type)
        instance = model(item=item)
        if media_type != MediaTypes.EPISODE.value:  # episode has no user field
            instance.user = self.user

        row["item"] = item
        form = app_forms.get_form_class(media_type)(
            row,
            instance=instance,
        )

        if form.is_valid():
            progressed_at = row.get("progressed_at")
            if progressed_at:
                form.instance._history_date = parse_datetime(progressed_at)
            if media_type in (MediaTypes.TV.value, MediaTypes.SEASON.value):
                status_value = row.get("status")
                if status_value:
                    if media_type == MediaTypes.TV.value:
                        self.status_overrides[media_type][
                            (row["source"], row["media_id"])
                        ] = status_value
                    else:
                        self.status_overrides[media_type][
                            (row["source"], row["media_id"], season_number)
                        ] = status_value
            self.bulk_media[media_type].append(form.instance)
        else:
            error_msg = f"{row['title']} ({media_type}): {form.errors.as_json()}"
            self.warnings.append(error_msg)
            logger.error(
                "Yamtrack import validation failed media_type=%s error_fields=%s",
                media_type,
                mapping_keys(form.errors),
            )

    def _process_list_row(self, row):
        """Process a list definition row."""
        list_uid = (row.get("list_uid") or "").strip()
        list_name = (row.get("list_name") or "").strip()
        if not list_name:
            self.warnings.append("Skipping list row without a name.")
            return

        list_source = (row.get("list_source") or "local").strip() or "local"
        list_source_id = (row.get("list_source_id") or "").strip()
        list_visibility = (row.get("list_visibility") or "private").strip() or "private"
        list_description = row.get("list_description") or ""
        list_allow_recommendations = _parse_bool(row.get("list_allow_recommendations"))
        list_tags = _parse_tags(row.get("list_tags"))

        existing = None
        if list_source_id:
            existing = CustomList.objects.filter(
                owner=self.user,
                source=list_source,
                source_id=list_source_id,
            ).first()
        if not existing:
            existing = CustomList.objects.filter(owner=self.user, name=list_name).first()

        seen_key = list_uid or list_name
        already_seen = bool(seen_key and seen_key in self.list_map)

        if existing:
            if self.mode == "overwrite":
                existing.description = list_description
                existing.tags = list_tags
                existing.visibility = list_visibility
                existing.allow_recommendations = list_allow_recommendations
                existing.source = list_source
                existing.source_id = list_source_id
                existing.save(
                    update_fields=[
                        "description",
                        "tags",
                        "visibility",
                        "allow_recommendations",
                        "source",
                        "source_id",
                    ],
                )
                if not already_seen:
                    CustomListItem.objects.filter(custom_list=existing).delete()
            custom_list = existing
        else:
            custom_list = CustomList.objects.create(
                name=list_name,
                description=list_description,
                tags=list_tags,
                visibility=list_visibility,
                allow_recommendations=list_allow_recommendations,
                source=list_source,
                source_id=list_source_id,
                owner=self.user,
            )

        if list_uid:
            self.list_map[list_uid] = custom_list
        else:
            self.list_map[list_name] = custom_list

    def _process_list_item_row(self, row):
        """Process a list item row without creating tracked media."""
        list_uid = (row.get("list_uid") or "").strip()
        list_name = (row.get("list_name") or "").strip()

        custom_list = None
        if list_uid:
            custom_list = self.list_map.get(list_uid)
        if not custom_list and list_name:
            custom_list = self.list_map.get(list_name) or CustomList.objects.filter(
                owner=self.user,
                name=list_name,
            ).first()
        if not custom_list and list_name:
            custom_list = CustomList.objects.create(
                name=list_name,
                owner=self.user,
            )
            if list_uid:
                self.list_map[list_uid] = custom_list
            else:
                self.list_map[list_name] = custom_list
        if not custom_list:
            self.warnings.append("Skipping list item row without a list reference.")
            return

        media_type = row.get("media_type") or ""
        if not media_type:
            self.warnings.append(
                f"Skipping list item without media_type for list {custom_list.name}."
            )
            return

        season_number = (
            int(row["season_number"]) if row.get("season_number") else None
        )
        episode_number = (
            int(row["episode_number"]) if row.get("episode_number") else None
        )

        if (
            row.get("media_id") == ""
            or row.get("title") == ""
            or row.get("image") == ""
        ):
            self._handle_missing_metadata(
                row,
                media_type,
                season_number,
                episode_number,
            )

        item, _ = helpers.retry_on_lock(
            lambda: app.models.Item.objects.update_or_create(
                media_id=row["media_id"],
                source=row["source"],
                media_type=media_type,
                season_number=season_number,
                episode_number=episode_number,
                defaults={
                    "title": row["title"],
                    "image": row["image"],
                },
            ),
        )

        list_item, created = CustomListItem.objects.get_or_create(
            custom_list=custom_list,
            item=item,
            defaults={"added_by": self.user},
        )
        list_item_date = row.get("list_item_date_added")
        if created and list_item_date:
            parsed_date = parse_datetime(list_item_date)
            if parsed_date:
                CustomListItem.objects.filter(pk=list_item.pk).update(
                    date_added=parsed_date,
                )

    def _handle_missing_metadata(self, row, media_type, season_number, episode_number):
        """Handle missing metadata by fetching from provider."""
        if row["source"] == Sources.MANUAL.value and row["image"] == "":
            row["image"] = settings.IMG_NONE
            return

        if row.get("media_id", "") != "":
            metadata = services.get_media_metadata(
                media_type,
                row["media_id"],
                row["source"],
                [season_number],
                episode_number,
            )
            row["title"] = metadata["title"]
            row["image"] = metadata["image"]
            return

        if row.get("title", "") != "":
            source = row.get("source", "")
            if source == "":
                source = config.get_default_source_name(media_type).value

            metadata = services.search(
                media_type,
                row["title"],
                1,
                source,
            )

            first_result = metadata["results"][0]
            row["title"] = first_result["title"]
            row["source"] = first_result["source"]
            row["media_id"] = first_result["media_id"]
            row["media_type"] = media_type
            row["image"] = first_result["image"]

            logger.info(
                "Resolved missing metadata for Yamtrack import row from %s",
                source,
            )
            return

        msg = f"Missing metadata for: {row}"
        raise MediaImportError(msg)
