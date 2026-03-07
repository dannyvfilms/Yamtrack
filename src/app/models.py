import logging
from datetime import timedelta
from collections import defaultdict

from django.apps import apps
from django.conf import settings
from django.core.validators import (
    DecimalValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.db.models import (
    CheckConstraint,
    Count,
    F,
    IntegerField,
    Max,
    Prefetch,
    Q,
    UniqueConstraint,
    Window,
)
from django.db.models.functions import Cast, RowNumber
from django.utils import timezone
from model_utils import FieldTracker
from model_utils.fields import MonitorField
from simple_history.models import HistoricalRecords
from simple_history.utils import bulk_create_with_history, bulk_update_with_history

import app
import events
import users
from app import cache_utils, providers
from app.mixins import CalendarTriggerMixin

logger = logging.getLogger(__name__)


class Sources(models.TextChoices):
    """Choices for the source of the item."""

    TMDB = "tmdb", "The Movie Database"
    MAL = "mal", "MyAnimeList"
    MANGAUPDATES = "mangaupdates", "MangaUpdates"
    IGDB = "igdb", "Internet Game Database"
    OPENLIBRARY = "openlibrary", "Open Library"
    HARDCOVER = "hardcover", "Hardcover"
    COMICVINE = "comicvine", "Comic Vine"
    BGG = "bgg", "BoardGameGeek"
    MUSICBRAINZ = "musicbrainz", "MusicBrainz"
    POCKETCASTS = "pocketcasts", "Pocket Casts"
    AUDIOBOOKSHELF = "audiobookshelf", "Audiobookshelf"
    MANUAL = "manual", "Manual"


class MediaTypes(models.TextChoices):
    """Choices for the media type of the item."""

    TV = "tv", "TV Show"
    SEASON = "season", "TV Season"
    EPISODE = "episode", "Episode"
    MOVIE = "movie", "Movie"
    ANIME = "anime", "Anime"
    MANGA = "manga", "Manga"
    GAME = "game", "Game"
    BOOK = "book", "Book"
    COMIC = "comic", "Comic"
    BOARDGAME = "boardgame", "Board Game"
    MUSIC = "music", "Music"
    PODCAST = "podcast", "Podcast"


class Item(CalendarTriggerMixin, models.Model):
    """Model to store basic information about media items."""

    media_id = models.CharField(max_length=20)
    source = models.CharField(
        max_length=20,
        choices=Sources,
    )
    media_type = models.CharField(
        max_length=10,
        choices=MediaTypes,
        default=MediaTypes.MOVIE.value,
    )
    title = models.TextField()
    original_title = models.TextField(null=True, blank=True)
    localized_title = models.TextField(null=True, blank=True)
    image = models.URLField()  # if add default, custom media entry will show the value
    season_number = models.PositiveIntegerField(null=True, blank=True)
    episode_number = models.PositiveIntegerField(null=True, blank=True)
    runtime_minutes = models.PositiveIntegerField(null=True, blank=True, help_text="Runtime in minutes")
    number_of_pages = models.PositiveIntegerField(null=True, blank=True, help_text="Number of pages for books")
    release_datetime = models.DateTimeField(null=True, blank=True)
    genres = models.JSONField(default=list, blank=True)
    # Metadata fields for filtering, sorting, and statistics
    country = models.CharField(max_length=255, blank=True, default="", help_text="Origin country")
    languages = models.JSONField(default=list, blank=True, help_text="Array of languages")
    platforms = models.JSONField(default=list, blank=True, help_text="Array of platforms (Games)")
    format = models.CharField(max_length=100, blank=True, default="", help_text="Media format type")
    status = models.CharField(max_length=100, blank=True, default="", help_text="Production status")
    studios = models.JSONField(default=list, blank=True, help_text="Array of production studios")
    themes = models.JSONField(default=list, blank=True, help_text="Array of themes (Games)")
    authors = models.JSONField(default=list, blank=True, help_text="Array of authors")
    publishers = models.CharField(max_length=255, blank=True, default="", help_text="Publisher name")
    isbn = models.JSONField(default=list, blank=True, help_text="Array of ISBN numbers")
    source_material = models.CharField(max_length=100, blank=True, default="", help_text="Source material (Anime)")
    creators = models.JSONField(default=list, blank=True, help_text="Array of creators (Comics)")
    runtime = models.CharField(max_length=50, blank=True, default="", help_text="Formatted runtime string")
    provider_popularity = models.FloatField(
        null=True,
        blank=True,
        help_text="Normalized popularity value from provider metadata",
    )
    provider_rating = models.FloatField(
        null=True,
        blank=True,
        help_text="Average rating value from provider metadata",
    )
    provider_rating_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Rating count from provider metadata",
    )
    provider_keywords = models.JSONField(default=list, blank=True, help_text="Provider keywords")
    provider_certification = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Primary provider certification/content rating",
    )
    provider_collection_id = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Provider collection/franchise id",
    )
    provider_collection_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Provider collection/franchise name",
    )
    metadata_fetched_at = models.DateTimeField(null=True, blank=True, help_text="When metadata was last fetched")
    series_name = models.TextField(null=True, blank=True)
    series_position = models.FloatField(null=True, blank=True)

    class Meta:
        """Meta options for the model."""

        constraints = [
            # Ensures items without season/episode numbers are unique
            UniqueConstraint(
                fields=["media_id", "source", "media_type"],
                condition=Q(season_number__isnull=True, episode_number__isnull=True),
                name="unique_item_without_season_episode",
            ),
            # Ensures seasons are unique within a show
            UniqueConstraint(
                fields=["media_id", "source", "media_type", "season_number"],
                condition=Q(season_number__isnull=False, episode_number__isnull=True),
                name="unique_item_with_season",
            ),
            # Ensures episodes are unique within a season
            UniqueConstraint(
                fields=[
                    "media_id",
                    "source",
                    "media_type",
                    "season_number",
                    "episode_number",
                ],
                condition=Q(season_number__isnull=False, episode_number__isnull=False),
                name="unique_item_with_season_episode",
            ),
            # Enforces that season items must have a season number but no episode number
            CheckConstraint(
                condition=Q(
                    media_type=MediaTypes.SEASON.value,
                    season_number__isnull=False,
                    episode_number__isnull=True,
                )
                | ~Q(media_type=MediaTypes.SEASON.value),
                name="season_number_required_for_season",
            ),
            # Enforces that episode items must have both season and episode numbers
            CheckConstraint(
                condition=Q(
                    media_type=MediaTypes.EPISODE.value,
                    season_number__isnull=False,
                    episode_number__isnull=False,
                )
                | ~Q(media_type=MediaTypes.EPISODE.value),
                name="season_and_episode_required_for_episode",
            ),
            # Prevents season/episode numbers from being set on non-TV media types
            CheckConstraint(
                condition=Q(
                    ~Q(
                        media_type__in=[
                            MediaTypes.SEASON.value,
                            MediaTypes.EPISODE.value,
                        ],
                    ),
                    season_number__isnull=True,
                    episode_number__isnull=True,
                )
                | Q(media_type__in=[MediaTypes.SEASON.value, MediaTypes.EPISODE.value]),
                name="no_season_episode_for_other_types",
            ),
            # Validate source choices
            CheckConstraint(
                condition=Q(source__in=Sources.values),
                name="%(app_label)s_%(class)s_source_valid",
            ),
            # Validate media_type choices
            CheckConstraint(
                condition=Q(media_type__in=MediaTypes.values),
                name="%(app_label)s_%(class)s_media_type_valid",
            ),
        ]
        ordering = ["media_id"]

    def __str__(self):
        """Return the name of the item."""
        name = self.title
        if self.season_number is not None:
            name += f" S{self.season_number}"
            if self.episode_number is not None:
                name += f"E{self.episode_number}"
        return name

    @staticmethod
    def _normalize_title_value(value):
        """Normalize title values to non-empty strings or None."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def title_fields_from_metadata(cls, metadata, fallback_title=""):
        """Build item title fields from provider metadata."""
        metadata = metadata or {}
        title = cls._normalize_title_value(metadata.get("title"))
        original_title = cls._normalize_title_value(metadata.get("original_title"))
        localized_title = cls._normalize_title_value(metadata.get("localized_title"))

        if not localized_title and title:
            localized_title = title

        if not title:
            title = (
                localized_title
                or original_title
                or cls._normalize_title_value(fallback_title)
                or ""
            )

        return {
            "title": title,
            "original_title": original_title,
            "localized_title": localized_title,
        }

    def get_display_and_alternative_title(self, user=None):
        """Return display and alternate titles based on user preference."""
        preference = getattr(user, "title_display_preference", "localized")
        return self.resolve_title_preference(preference)

    def resolve_title_preference(self, preference):
        """Resolve display and alternative titles for a preference value."""
        preference = (preference or "localized").lower()
        original_title = self._normalize_title_value(self.original_title)
        localized_title = (
            self._normalize_title_value(self.localized_title)
            or self._normalize_title_value(self.title)
        )
        fallback_title = (
            self._normalize_title_value(self.title)
            or localized_title
            or original_title
            or ""
        )

        if preference == "original":
            display_title = original_title or localized_title or fallback_title
            alternative_title = (
                localized_title if localized_title and localized_title != display_title else None
            )
            return display_title, alternative_title

        # Auto currently prefers localized titles when available.
        display_title = localized_title or original_title or fallback_title
        alternative_title = (
            original_title if original_title and original_title != display_title else None
        )
        return display_title, alternative_title

    def get_display_title(self, user=None):
        """Return the preferred title to render for this item."""
        display_title, _ = self.get_display_and_alternative_title(user=user)
        return display_title

    def get_alternative_title(self, user=None):
        """Return the opposite title variant for tooltip display."""
        _, alternative_title = self.get_display_and_alternative_title(user=user)
        return alternative_title

    @classmethod
    def generate_manual_id(cls, media_type):
        """Generate a new ID for manual items."""
        latest_item = (
            cls.objects.filter(source=Sources.MANUAL.value, media_type=media_type)
            .annotate(
                media_id_int=Cast("media_id", IntegerField()),
            )
            .order_by("-media_id_int")
            .first()
        )

        if latest_item is None:
            return "1"

        return str(int(latest_item.media_id) + 1)

    def save(self, *args, **kwargs):
        """Save the item, ensuring JSONField arrays are never None."""
        # Ensure all JSONField arrays are lists, never None
        json_array_fields = [
            "genres",
            "languages",
            "platforms",
            "studios",
            "themes",
            "authors",
            "isbn",
            "creators",
        ]
        for field_name in json_array_fields:
            value = getattr(self, field_name, None)
            if value is None:
                setattr(self, field_name, [])

        super().save(*args, **kwargs)

    def fetch_releases(self, delay):
        """Fetch releases for the item."""
        if self._disable_calendar_triggers:
            return
        if settings.TESTING:
            return

        if self.media_type == MediaTypes.SEASON.value:
            # Get or create the TV item for this season
            try:
                tv_item = Item.objects.get(
                    media_id=self.media_id,
                    source=self.source,
                    media_type=MediaTypes.TV.value,
                )
            except Item.DoesNotExist:
                # Get metadata for the TV show
                tv_metadata = providers.services.get_media_metadata(
                    MediaTypes.TV.value,
                    self.media_id,
                    self.source,
                )
                # Extract runtime from metadata
                runtime_minutes = None
                if tv_metadata.get("details", {}).get("runtime"):
                    from app.statistics import parse_runtime_to_minutes

                    runtime_minutes = parse_runtime_to_minutes(tv_metadata["details"]["runtime"])

                tv_item = Item.objects.create(
                    media_id=self.media_id,
                    source=self.source,
                    media_type=MediaTypes.TV.value,
                    **Item.title_fields_from_metadata(tv_metadata),
                    image=tv_metadata["image"],
                    runtime_minutes=runtime_minutes,
                )
                logger.info("Created TV item %s for season %s", tv_item, self)

            # Process the TV item instead of the season
            items_to_process = [tv_item]
        else:
            items_to_process = [self]

        if delay:
            events.tasks.reload_calendar.apply_async(kwargs={"items_to_process": items_to_process}, countdown=3)
        else:
            events.tasks.reload_calendar(items_to_process=items_to_process)


class MetadataBackfillField(models.TextChoices):
    """Fields that can be backfilled from external metadata."""

    RUNTIME = "runtime", "Runtime"
    GENRES = "genres", "Genres"
    CREDITS = "credits", "Credits"
    RELEASE = "release", "Release Date"
    DISCOVER = "discover", "Discover Metadata"


CREDITS_BACKFILL_VERSION = 2
DISCOVER_MOVIE_METADATA_BACKFILL_VERSION = 1


class MetadataBackfillState(models.Model):
    """Track metadata backfill attempts to avoid endless retries."""

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="metadata_backfill_states",
    )
    field = models.CharField(
        max_length=20,
        choices=MetadataBackfillField.choices,
    )
    fail_count = models.PositiveIntegerField(default=0)
    strategy_version = models.PositiveIntegerField(default=1)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    give_up = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["item", "field"],
                name="unique_metadata_backfill_state",
            ),
        ]
        indexes = [
            models.Index(fields=["field", "next_retry_at"]),
            models.Index(fields=["field", "give_up"]),
        ]



class PersonGender(models.TextChoices):
    """Normalized person genders used across providers."""

    UNKNOWN = "unknown", "Unknown"
    FEMALE = "female", "Female"
    MALE = "male", "Male"
    NON_BINARY = "non_binary", "Non-binary"


class CreditRoleType(models.TextChoices):
    """Credit role category."""

    CAST = "cast", "Cast"
    CREW = "crew", "Crew"
    AUTHOR = "author", "Author"


class Person(models.Model):
    """Known cast/crew person."""

    source = models.CharField(
        max_length=20,
        choices=Sources.choices,
        default=Sources.TMDB.value,
    )
    source_person_id = models.CharField(max_length=32)
    name = models.CharField(max_length=255)
    image = models.URLField(blank=True, default="")
    known_for_department = models.CharField(max_length=120, blank=True, default="")
    biography = models.TextField(blank=True, default="")
    gender = models.CharField(
        max_length=20,
        choices=PersonGender.choices,
        default=PersonGender.UNKNOWN.value,
    )
    birth_date = models.DateField(null=True, blank=True)
    death_date = models.DateField(null=True, blank=True)
    place_of_birth = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        """Meta options for the model."""

        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "source_person_id"],
                name="%(app_label)s_%(class)s_unique_source_person",
            ),
        ]
        indexes = [
            models.Index(fields=["source", "source_person_id"]),
        ]

    def __str__(self):
        """Return the person name."""
        return self.name


class Studio(models.Model):
    """Studio/company associated with a media item."""

    source = models.CharField(
        max_length=20,
        choices=Sources.choices,
        default=Sources.TMDB.value,
    )
    source_studio_id = models.CharField(max_length=32)
    name = models.CharField(max_length=255)
    logo = models.URLField(blank=True, default="")

    class Meta:
        """Meta options for the model."""

        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "source_studio_id"],
                name="%(app_label)s_%(class)s_unique_source_studio",
            ),
        ]
        indexes = [
            models.Index(fields=["source", "source_studio_id"]),
        ]

    def __str__(self):
        """Return the studio name."""
        return self.name


class ItemPersonCredit(models.Model):
    """Cast/crew credits connecting media items and people."""

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="person_credits",
    )
    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="item_credits",
    )
    role_type = models.CharField(max_length=10, choices=CreditRoleType.choices)
    role = models.CharField(max_length=255, blank=True, default="")
    department = models.CharField(max_length=120, blank=True, default="")
    sort_order = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["sort_order", "person__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "person", "role_type", "role", "department"],
                name="%(app_label)s_%(class)s_unique_credit",
            ),
        ]
        indexes = [
            models.Index(fields=["item", "role_type"]),
            models.Index(fields=["person", "role_type"]),
            models.Index(fields=["department"]),
        ]

    def __str__(self):
        """Return the credit label."""
        return f"{self.person} - {self.role_type}"


class ItemStudioCredit(models.Model):
    """Studio/company links for media items."""

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="studio_credits",
    )
    studio = models.ForeignKey(
        Studio,
        on_delete=models.CASCADE,
        related_name="item_credits",
    )
    sort_order = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["sort_order", "studio__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "studio"],
                name="%(app_label)s_%(class)s_unique_item_studio",
            ),
        ]
        indexes = [
            models.Index(fields=["item"]),
            models.Index(fields=["studio"]),
        ]

    def __str__(self):
        """Return the studio credit label."""
        return f"{self.studio} - {self.item}"


class MediaManager(models.Manager):
    """Custom manager for media models."""

    def get_historical_models(self):
        """Return list of historical model names."""
        return [f"historical{media_type}" for media_type in MediaTypes.values]

    def resolve_direction(self, sort_filter, direction=None):
        """Normalize sort direction with per-field defaults."""
        normalized = (direction or "").lower()
        if normalized not in ("asc", "desc"):
            return self._default_direction(sort_filter)
        return normalized

    def _default_direction(self, sort_filter):
        """Return default direction for a sort key."""
        if sort_filter in ("start_date", "title", "time_left"):
            return "asc"
        return "desc"

    def get_media_list(self, user, media_type, status_filter, sort_filter, search=None, direction=None):
        """Get a media list by type with filtering and sorting."""
        model = apps.get_model(app_label="app", model_name=media_type)
        direction = self.resolve_direction(sort_filter, direction)

        # Build base queryset
        queryset = model.objects.filter(user=user.id)

        # Apply status filter
        if status_filter != users.models.MediaStatusChoices.ALL:
            queryset = queryset.filter(status=status_filter)

        # Apply search filter
        if search:
            queryset = queryset.filter(
                models.Q(item__title__icontains=search)
                | models.Q(item__media_id__icontains=search),
            )

        # Handle duplicate entries by selecting the most recent record for each item
        has_progress_field = any(
            getattr(field, "attname", "") == "progress"
            for field in model._meta.get_fields()
            if getattr(field, "concrete", False)
        )
        if sort_filter == "progress" and has_progress_field:
            # For progress sorting, select the record with highest individual progress
            queryset = queryset.annotate(
                repeats=Window(
                    expression=Count("id"),
                    partition_by=[F("item")],
                ),
                row_number=Window(
                    expression=RowNumber(),
                    partition_by=[F("item")],
                    order_by=F("progress").desc(),
                ),
            ).filter(row_number=1)
        else:
            # For non-progress sorting, select the most recent record
            queryset = queryset.annotate(
                repeats=Window(
                    expression=Count("id"),
                    partition_by=[F("item")],
                ),
                row_number=Window(
                    expression=RowNumber(),
                    partition_by=[F("item")],
                    order_by=F("created_at").desc(),
                ),
            ).filter(row_number=1)

        queryset = queryset.select_related("item")
        queryset = self._apply_prefetch_related(queryset, media_type)

        requires_presort_aggregation = (
            sort_filter in ("progress", "plays")
            and media_type not in (MediaTypes.TV.value, MediaTypes.SEASON.value)
        )

        # Generic progress sorting uses Python and reads aggregated_progress, so
        # duplicates must be aggregated before sorting in that specific path.
        if requires_presort_aggregation:
            queryset = self._aggregate_duplicate_data(queryset, user, media_type)

        # Apply sorting AFTER aggregation
        if sort_filter:
            queryset = self._sort_media_list(queryset, sort_filter, media_type, direction)

        # Re-apply duplicate aggregation because SQL queryset operations in sorting
        # can materialize fresh model instances and drop dynamic aggregated attrs.
        return self._aggregate_duplicate_data(queryset, user, media_type)

    def _aggregate_duplicate_data(self, queryset, user, media_type):
        """Aggregate data from duplicate entries for each item."""
        # Get all media entries for the user to aggregate data
        model = apps.get_model(app_label="app", model_name=media_type)
        all_media = model.objects.filter(user=user.id).select_related("item")

        # Group media by item_id
        media_by_item = {}
        for media in all_media:
            item_id = media.item.id
            if item_id not in media_by_item:
                media_by_item[item_id] = []
            media_by_item[item_id].append(media)

        # Aggregate data for each item in the queryset
        for media in queryset:
            item_id = media.item.id
            if item_id in media_by_item and len(media_by_item[item_id]) > 1:
                # Aggregate data from all duplicates
                self._aggregate_item_data(media, media_by_item[item_id])

        return queryset

    def _aggregate_item_data(self, display_media, all_media_entries):
        """Aggregate data from multiple media entries for the same item."""
        # Sort by created_at to get chronological order
        sorted_entries = sorted(all_media_entries, key=lambda x: x.created_at)

        # Aggregate progress:
        # - Movies: count completed entries as plays (legacy rows may have progress=0)
        # - Other media: sum raw progress values
        if getattr(display_media.item, "media_type", None) == MediaTypes.MOVIE.value:
            completed_entries = [
                entry
                for entry in all_media_entries
                if entry.end_date or entry.status == Status.COMPLETED.value
            ]
            total_progress = len(completed_entries)
        else:
            total_progress = sum(entry.progress for entry in all_media_entries)
        display_media.aggregated_progress = total_progress

        # Aggregate start date (earliest start date)
        start_dates = [entry.start_date for entry in all_media_entries if entry.start_date]
        if start_dates:
            display_media.aggregated_start_date = min(start_dates)
        else:
            display_media.aggregated_start_date = None

        # Aggregate end date (latest end date)
        end_dates = [entry.end_date for entry in all_media_entries if entry.end_date]
        if end_dates:
            display_media.aggregated_end_date = max(end_dates)
        else:
            display_media.aggregated_end_date = None

        # Aggregate status (most recent status by activity)
        latest_status = None
        latest_status_activity = None

        # Aggregate rating (find the most recent rating among all entries)
        # Since created_at only represents when the entry was first created,
        # we need to use a different approach to find the most recent rating
        # We'll prioritize entries with more recent activity (end_date, progressed_at)
        latest_rating = None
        latest_rating_activity = None

        for entry in all_media_entries:
            if entry.score is not None:
                # Determine the most recent activity for this entry
                entry_activity = None
                if entry.end_date:
                    entry_activity = entry.end_date
                elif entry.progressed_at:
                    entry_activity = entry.progressed_at
                else:
                    entry_activity = entry.created_at

                # If this entry has more recent activity, use its rating
                if latest_rating_activity is None or entry_activity > latest_rating_activity:
                    latest_rating_activity = entry_activity
                    latest_rating = entry.score
            else:
                entry_activity = entry.end_date or entry.progressed_at or entry.created_at

            if entry_activity and (
                latest_status_activity is None or entry_activity > latest_status_activity
            ):
                latest_status_activity = entry_activity
                latest_status = entry.status

        display_media.aggregated_status = latest_status or display_media.status

        if latest_rating is not None:
            display_media.aggregated_score = latest_rating
        else:
            display_media.aggregated_score = None

        # Store the number of repeats for display
        display_media.repeats = len(all_media_entries)

    def _apply_prefetch_related(self, queryset, media_type):
        """Apply appropriate prefetch_related based on media type."""
        # Apply media-specific prefetches
        if media_type == MediaTypes.TV.value:
            return queryset.prefetch_related(
                Prefetch(
                    "seasons",
                    queryset=Season.objects.select_related("item"),
                ),
                Prefetch(
                    "seasons__episodes",
                    queryset=Episode.objects.select_related("item"),
                ),
            )

        base_queryset = queryset.prefetch_related(
            Prefetch(
                "item__event_set",
                queryset=events.models.Event.objects.all(),
                to_attr="prefetched_events",
            ),
        )

        if media_type == MediaTypes.SEASON.value:
            return base_queryset.prefetch_related(
                Prefetch(
                    "episodes",
                    queryset=Episode.objects.select_related("item"),
                ),
            )

        return base_queryset

    def _sort_media_list(self, queryset, sort_filter, media_type=None, direction=None):
        """Sort media list using SQL sorting with annotations for calculated fields."""
        direction = self.resolve_direction(sort_filter, direction)
        if media_type == MediaTypes.TV.value:
            return self._sort_tv_media_list(queryset, sort_filter, direction)
        if media_type == MediaTypes.SEASON.value:
            return self._sort_season_media_list(queryset, sort_filter, direction)

        return self._sort_generic_media_list(queryset, sort_filter, direction)

    def _sort_tv_media_list(self, queryset, sort_filter, direction):
        """Sort TV media list based on the sort criteria."""
        if sort_filter == "start_date":
            # Annotate with the minimum start_date from related seasons/episodes
            queryset = queryset.annotate(
                calculated_start_date=models.Min(
                    "seasons__episodes__end_date",
                    filter=models.Q(seasons__item__season_number__gt=0),
                ),
            )
            order = (
                models.F("calculated_start_date").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_start_date").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "end_date":
            # Annotate with the maximum end_date from related seasons/episodes
            queryset = queryset.annotate(
                calculated_end_date=models.Max(
                    "seasons__episodes__end_date",
                    filter=models.Q(seasons__item__season_number__gt=0),
                ),
            )
            order = (
                models.F("calculated_end_date").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_end_date").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "progress":
            # Annotate with the sum of episodes watched (excluding season 0)
            queryset = queryset.annotate(
                # Count episodes in regular seasons (season_number > 0)
                calculated_progress=models.Count(
                    "seasons__episodes",
                    filter=models.Q(seasons__item__season_number__gt=0),
                ),
            )
            order = (
                models.F("calculated_progress").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_progress").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "time_left":
            # For time_left sorting, we need custom Python sorting
            # Return queryset as-is for custom sorting in views
            return queryset

        # Default to generic sorting
        return self._sort_generic_media_list(queryset, sort_filter, direction)

    def _sort_season_media_list(self, queryset, sort_filter, direction):
        """Sort Season media list based on the sort criteria."""
        if sort_filter == "start_date":
            # Annotate with the minimum end_date from related episodes
            queryset = queryset.annotate(
                calculated_start_date=models.Min("episodes__end_date"),
            )
            order = (
                models.F("calculated_start_date").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_start_date").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "end_date":
            # Annotate with the maximum end_date from related episodes
            queryset = queryset.annotate(
                calculated_end_date=models.Max("episodes__end_date"),
            )
            order = (
                models.F("calculated_end_date").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_end_date").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "progress":
            # Annotate with the maximum episode number
            queryset = queryset.annotate(
                calculated_progress=models.Max("episodes__item__episode_number"),
            )
            order = (
                models.F("calculated_progress").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_progress").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        # Default to generic sorting
        return self._sort_generic_media_list(queryset, sort_filter, direction)

    def _sort_generic_media_list(self, queryset, sort_filter, direction):
        """Apply generic sorting logic for all media types."""
        # Handle progress sorting specially to use aggregated progress
        if sort_filter in ("progress", "plays"):
            # Since we're now sorting after aggregation, we can use the aggregated_progress attribute
            # Convert to list for Python-based sorting since aggregated_progress is a Python attribute
            media_list = list(queryset)
            return sorted(
                media_list,
                key=lambda x: (getattr(x, "aggregated_progress", x.progress), x.item.title.lower()),
                reverse=(direction == "desc"),
            )

        # Handle sorting by date fields with special null handling
        if sort_filter in ("start_date", "end_date", "date_added"):
            sort_field = "created_at" if sort_filter == "date_added" else sort_filter
            order = (
                models.F(sort_field).asc(nulls_last=True)
                if direction == "asc"
                else models.F(sort_field).desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "release_date":
            order = (
                models.F("item__release_datetime").asc(nulls_last=True)
                if direction == "asc"
                else models.F("item__release_datetime").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        # Handle sorting by Item fields
        item_fields = [f.name for f in Item._meta.fields]
        if sort_filter in item_fields:
            if sort_filter == "title":
                # Case-insensitive title sorting
                expr = models.functions.Lower("item__title")
                order = expr.asc() if direction == "asc" else expr.desc()
                return queryset.order_by(order)
            # Default sorting for other Item fields
            order = (
                models.F(f"item__{sort_filter}").asc(nulls_last=True)
                if direction == "asc"
                else models.F(f"item__{sort_filter}").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        # Default sorting by media field
        order = (
            models.F(sort_filter).asc(nulls_last=True)
            if direction == "asc"
            else models.F(sort_filter).desc(nulls_last=True)
        )
        return queryset.order_by(order, models.functions.Lower("item__title"))

    def get_in_progress(self, user, sort_by, items_limit, specific_media_type=None):
        """Get a media list of in progress media by type."""
        list_by_type = {}
        media_types = self._get_media_types_to_process(user, specific_media_type)

        # Get user preference for planned items display mode
        planned_mode = getattr(user, "show_planned_on_home", users.models.PlannedHomeDisplayChoices.DISABLED)

        def filter_by_latest_status(media_list, desired_status):
            """Filter media entries by their most recent status across duplicates."""
            if not media_list:
                return media_list
            filtered = []
            for media in media_list:
                latest_status = getattr(media, "aggregated_status", None) or getattr(media, "status", None)
                if latest_status == desired_status:
                    filtered.append(media)
            return filtered

        for media_type in media_types:
            base_media_type = media_type

            # Get base media list for in-progress media
            in_progress_list = self.get_media_list(
                user=user,
                media_type=base_media_type,
                status_filter=Status.IN_PROGRESS.value,
                sort_filter=None,
            )
            in_progress_list = list(in_progress_list)
            in_progress_list = filter_by_latest_status(in_progress_list, Status.IN_PROGRESS.value)

            # Get planned items if needed
            planned_list = []
            if planned_mode != users.models.PlannedHomeDisplayChoices.DISABLED:
                planned_queryset = self.get_media_list(
                    user=user,
                    media_type=base_media_type,
                    status_filter=Status.PLANNING.value,
                    sort_filter=None,
                )
                planned_list = filter_by_latest_status(list(planned_queryset), Status.PLANNING.value)

            # Handle different modes
            if planned_mode == users.models.PlannedHomeDisplayChoices.DISABLED:
                # Only in-progress items
                media_list = in_progress_list
                if not media_list:
                    continue

                # Process in-progress items
                self.annotate_max_progress(media_list, base_media_type)
                self._annotate_next_event(media_list)

                if base_media_type == MediaTypes.SEASON.value:
                    self._fix_missing_season_images(media_list)

                sorted_list = self._sort_in_progress_media(media_list, sort_by)
                total_count = len(sorted_list)
                if specific_media_type:
                    paginated_list = sorted_list[items_limit:]
                else:
                    paginated_list = sorted_list[:items_limit]

                list_by_type[base_media_type] = {
                    "items": paginated_list,
                    "total": total_count,
                }

            elif planned_mode == users.models.PlannedHomeDisplayChoices.COMBINED:
                # Combine in-progress and planned items
                media_list = list(in_progress_list)
                existing_item_ids = {media.item.id for media in media_list}
                for planned_media in planned_list:
                    if planned_media.item.id not in existing_item_ids:
                        media_list.append(planned_media)
                        existing_item_ids.add(planned_media.item.id)

                if not media_list:
                    continue

                # Process combined items
                self.annotate_max_progress(media_list, base_media_type)
                self._annotate_next_event(media_list)

                if base_media_type == MediaTypes.SEASON.value:
                    self._fix_missing_season_images(media_list)

                sorted_list = self._sort_in_progress_media(media_list, sort_by)
                total_count = len(sorted_list)
                if specific_media_type:
                    paginated_list = sorted_list[items_limit:]
                else:
                    paginated_list = sorted_list[:items_limit]

                list_by_type[base_media_type] = {
                    "items": paginated_list,
                    "total": total_count,
                }

            elif planned_mode == users.models.PlannedHomeDisplayChoices.SEPARATED:
                # Separated mode: two distinct sections
                # Determine which sections to process based on specific_media_type request
                process_in_progress = True
                process_planned = True

                if specific_media_type:
                    if specific_media_type.endswith("_in_progress"):
                        process_planned = False
                    elif specific_media_type.endswith("_planned"):
                        process_in_progress = False

                # Handle in-progress section
                if process_in_progress and in_progress_list:
                    in_progress_processed = list(in_progress_list)
                    self.annotate_max_progress(in_progress_processed, base_media_type)
                    self._annotate_next_event(in_progress_processed)

                    if base_media_type == MediaTypes.SEASON.value:
                        self._fix_missing_season_images(in_progress_processed)

                    sorted_in_progress = self._sort_in_progress_media(in_progress_processed, sort_by)
                    total_in_progress = len(sorted_in_progress)

                    if specific_media_type and specific_media_type.endswith("_in_progress"):
                        paginated_in_progress = sorted_in_progress[items_limit:]
                    else:
                        paginated_in_progress = sorted_in_progress[:items_limit]

                    list_by_type[f"{base_media_type}_in_progress"] = {
                        "items": paginated_in_progress,
                        "total": total_in_progress,
                        "section_label": "In Progress",
                        "media_type": base_media_type,
                    }

                # Handle planned section
                if process_planned and planned_list:
                    planned_processed = list(planned_list)
                    self.annotate_max_progress(planned_processed, base_media_type)
                    self._annotate_next_event(planned_processed)

                    if base_media_type == MediaTypes.SEASON.value:
                        self._fix_missing_season_images(planned_processed)

                    sorted_planned = self._sort_in_progress_media(planned_processed, sort_by)
                    total_planned = len(sorted_planned)

                    if specific_media_type and specific_media_type.endswith("_planned"):
                        paginated_planned = sorted_planned[items_limit:]
                    else:
                        paginated_planned = sorted_planned[:items_limit]

                    list_by_type[f"{base_media_type}_planned"] = {
                        "items": paginated_planned,
                        "total": total_planned,
                        "section_label": "Planned",
                        "media_type": base_media_type,
                    }

        return list_by_type

    def get_recently_unrated(self, user, days=7):
        """Return recently played media items without a user score."""
        cutoff = timezone.now() - timedelta(days=days)
        recent_items = []
        media_types = self._get_media_types_to_process(user, None)

        def resolve_last_played(media):
            if media.item.media_type == MediaTypes.SEASON.value:
                return (
                    getattr(media, "last_watched", None)
                    or media.progressed_at
                    or media.created_at
                )
            return media.end_date or media.progressed_at or media.created_at

        for media_type in media_types:
            model = apps.get_model(app_label="app", model_name=media_type)

            rated_item_ids = model.objects.filter(
                user=user.id,
                score__isnull=False,
            ).values("item_id")

            queryset = (
                model.objects.filter(
                    user=user.id,
                    score__isnull=True,
                    status=Status.COMPLETED.value,
                )
                .exclude(item_id__in=rated_item_ids)
            )

            if media_type == MediaTypes.SEASON.value:
                queryset = queryset.filter(
                    episodes__end_date__gte=cutoff,
                ).annotate(
                    last_watched=Max("episodes__end_date"),
                )
                order_by_fields = [
                    F("last_watched").desc(nulls_last=True),
                    F("created_at").desc(),
                ]
            else:
                queryset = queryset.filter(
                    end_date__gte=cutoff,
                )
                order_by_fields = [
                    F("progressed_at").desc(nulls_last=True),
                    F("end_date").desc(nulls_last=True),
                    F("created_at").desc(),
                ]

            select_related_fields = ["item"]
            if media_type == MediaTypes.PODCAST.value:
                select_related_fields.append("show")
            elif media_type == MediaTypes.MUSIC.value:
                select_related_fields.append("album")

            queryset = queryset.annotate(
                repeats=Window(
                    expression=Count("id"),
                    partition_by=[F("item")],
                ),
                row_number=Window(
                    expression=RowNumber(),
                    partition_by=[F("item")],
                    order_by=order_by_fields,
                ),
            ).filter(row_number=1).select_related(*select_related_fields)

            queryset = self._apply_prefetch_related(queryset, media_type)
            items = list(queryset)
            for media in items:
                media.last_played_at = resolve_last_played(media)
                media.use_podcast_show = (
                    media.item.media_type == MediaTypes.PODCAST.value
                    and getattr(media, "show", None)
                )
            recent_items.extend(items)

        return sorted(
            recent_items,
            key=lambda media: media.last_played_at or media.created_at,
            reverse=True,
        )

    def _get_media_types_to_process(self, user, specific_media_type):
        """Determine which media types to process based on user settings."""
        if specific_media_type:
            # Extract base media_type if it has a suffix (e.g., "movie_in_progress" -> "movie")
            if "_" in specific_media_type:
                base_type = specific_media_type.rsplit("_", 1)[0]
                return [base_type]
            return [specific_media_type]

        media_types = [
            media_type
            for media_type in user.get_active_media_types()
            if media_type != MediaTypes.TV.value
        ]

        # Home should continue to include TV seasons when TV shows are enabled.
        if getattr(user, "tv_enabled", False) and MediaTypes.SEASON.value not in media_types:
            media_types.insert(0, MediaTypes.SEASON.value)

        return media_types

    def _annotate_next_event(self, media_list):
        """Annotate next_event for media items."""
        current_time = timezone.now()

        for media in media_list:
            # Get future events sorted by datetime
            future_events = sorted(
                [
                    event
                    for event in getattr(media.item, "prefetched_events", [])
                    if event.datetime > current_time
                ],
                key=lambda e: e.datetime,
            )

            media.next_event = future_events[0] if future_events else None

    def _fix_missing_season_images(self, season_list):
        """Backfill missing season poster images from metadata."""
        from django.conf import settings

        items_to_update = []
        for season in season_list:
            if season.item.image == settings.IMG_NONE:
                try:
                    season_metadata = providers.services.get_media_metadata(
                        MediaTypes.SEASON.value,
                        season.item.media_id,
                        season.item.source,
                        [season.item.season_number],
                    )
                    # Use season poster if available, otherwise fallback to TV show poster
                    season_image = season_metadata.get("image")
                    if not season_image:
                        # Get TV show metadata for fallback
                        tv_metadata = providers.services.get_media_metadata(
                            MediaTypes.TV.value,
                            season.item.media_id,
                            season.item.source,
                        )
                        season_image = tv_metadata.get("image")

                    if season_image:
                        season.item.image = season_image
                        items_to_update.append(season.item)
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch image for {season}: {e}",
                    )

        if items_to_update:
            Item.objects.bulk_update(items_to_update, ["image"])
            logger.info(f"Updated {len(items_to_update)} season poster(s)")

    def _sort_in_progress_media(self, media_list, sort_by):
        """Sort in-progress media based on the sort criteria."""
        # Define primary sort functions based on sort_by
        primary_sort_functions = {
            users.models.HomeSortChoices.UPCOMING: lambda x: (
                x.next_event is None,
                x.next_event.datetime if x.next_event else None,
            ),
            users.models.HomeSortChoices.RECENT: lambda x: (
                -timezone.datetime.timestamp(
                    x.progressed_at if x.progressed_at is not None else x.created_at,
                )
            ),
            users.models.HomeSortChoices.COMPLETION: lambda x: (
                x.max_progress is None,
                -(
                    x.progress / x.max_progress * 100
                    if x.max_progress and x.max_progress > 0
                    else 0
                ),
            ),
            users.models.HomeSortChoices.EPISODES_LEFT: lambda x: (
                x.max_progress is None,
                (x.max_progress - x.progress if x.max_progress else 0),
            ),
            users.models.HomeSortChoices.TITLE: lambda x: x.item.title.lower(),
        }

        primary_sort_function = primary_sort_functions[sort_by]

        return sorted(
            media_list,
            key=lambda x: (
                primary_sort_function(x),
                -timezone.datetime.timestamp(
                    x.progressed_at if x.progressed_at is not None else x.created_at,
                ),
                x.item.title.lower(),
            ),
        )

    def annotate_max_progress(self, media_list, media_type):
        """Annotate max_progress for all media items."""
        current_datetime = timezone.now()

        if media_type == MediaTypes.MOVIE.value:
            for media in media_list:
                media.max_progress = 1
            return

        if media_type == MediaTypes.TV.value:
            self._annotate_tv_released_episodes(media_list, current_datetime)
            return

        if media_type == MediaTypes.SEASON.value:
            # For seasons, use metadata max_progress instead of database annotation
            # The metadata value is more accurate as it reflects the actual total episodes
            # from the provider, not just episodes with release_datetime set
            from app.providers import services
            for season in media_list:
                try:
                    season_metadata = services.get_media_metadata(
                        MediaTypes.SEASON.value,
                        season.item.media_id,
                        season.item.source,
                        [season.item.season_number],
                    )
                    # Use metadata max_progress if available, otherwise fall back to annotation
                    metadata_max_progress = season_metadata.get("max_progress")
                    if metadata_max_progress is not None:
                        season.max_progress = metadata_max_progress
                    else:
                        # Fall back to database annotation if metadata doesn't have max_progress
                        self._annotate_season_released_episodes([season], current_datetime)
                except Exception:
                    # If metadata fetch fails, fall back to database annotation
                    self._annotate_season_released_episodes([season], current_datetime)
            return

        if media_type == MediaTypes.BOOK.value:
            # For books, use number_of_pages from Item model
            # If not available, try to fetch from metadata
            for media in media_list:
                if media.item.number_of_pages:
                    media.max_progress = media.item.number_of_pages
                else:
                    # Try to fetch from metadata if not stored
                    try:
                        from app.providers import services
                        metadata = services.get_media_metadata(
                            media.item.media_type,
                            media.item.media_id,
                            media.item.source,
                        )
                        number_of_pages = metadata.get("max_progress") or metadata.get("details", {}).get("number_of_pages")
                        if number_of_pages:
                            # Save it to the Item for future use
                            media.item.number_of_pages = number_of_pages
                            media.item.save(update_fields=["number_of_pages"])
                            media.max_progress = number_of_pages
                        else:
                            media.max_progress = None
                    except Exception:
                        media.max_progress = None
            return

        # For other media types, calculate max_progress from events
        # Create a dictionary mapping item_id to max content_number
        max_progress_dict = {}

        item_ids = [media.item.id for media in media_list]

        # Fetch all relevant events in a single query
        events_data = events.models.Event.objects.filter(
            item_id__in=item_ids,
            datetime__lte=current_datetime,
        ).values("item_id", "content_number")

        # Process events to find max content number per item
        for event in events_data:
            item_id = event["item_id"]
            content_number = event["content_number"]
            if content_number is not None:
                current_max = max_progress_dict.get(item_id, 0)
                max_progress_dict[item_id] = max(current_max, content_number)

        for media in media_list:
            media.max_progress = max_progress_dict.get(media.item.id)

    def _annotate_tv_released_episodes(self, tv_list, current_datetime):
        """Annotate TV shows with the number of released episodes."""
        if not tv_list:
            return

        media_keys = {(tv.item.media_id, tv.item.source) for tv in tv_list}
        media_ids = {media_id for media_id, _ in media_keys}
        media_sources = {source for _, source in media_keys}

        released_by_show: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)

        episode_rows = (
            Item.objects.filter(
                media_type=MediaTypes.EPISODE.value,
                media_id__in=media_ids,
                source__in=media_sources,
                release_datetime__isnull=False,
                release_datetime__lte=current_datetime,
                season_number__gt=0,
            )
            .values("media_id", "source", "season_number")
            .annotate(max_episode=models.Max("episode_number"))
        )

        for row in episode_rows:
            key = (row["media_id"], row["source"])
            season_number = row["season_number"]
            max_episode = row["max_episode"] or 0
            released_by_show[key][season_number] = max(
                released_by_show[key].get(season_number, 0),
                max_episode,
            )

        released_events = (
            events.models.Event.objects.filter(
                item__media_id__in=media_ids,
                item__source__in=media_sources,
                item__media_type=MediaTypes.SEASON.value,
                item__season_number__gt=0,
                datetime__lte=current_datetime,
                content_number__isnull=False,
            )
            .exclude(datetime__year__lt=1900)
            .values(
                "item__media_id",
                "item__source",
                "item__season_number",
            )
            .annotate(max_episode=models.Max("content_number"))
        )

        for row in released_events:
            key = (row["item__media_id"], row["item__source"])
            season_number = row["item__season_number"]
            max_episode = row["max_episode"] or 0
            released_by_show[key][season_number] = max(
                released_by_show[key].get(season_number, 0),
                max_episode,
            )

        for tv in tv_list:
            key = (tv.item.media_id, tv.item.source)
            breakdown = released_by_show.get(key, {})
            tv.released_episode_breakdown = breakdown
            if breakdown:
                tv.max_progress = sum(breakdown.values())
            else:
                tv.max_progress = None

    def _annotate_season_released_episodes(self, season_list, current_datetime):
        """Annotate seasons with the number of released episodes."""
        if not season_list:
            return

        season_keys = {
            (season.item.media_id, season.item.source, season.item.season_number)
            for season in season_list
        }
        media_ids = {media_id for media_id, _, _ in season_keys}
        media_sources = {source for _, source, _ in season_keys}
        season_numbers = {season_number for _, _, season_number in season_keys if season_number is not None}

        released_by_season: dict[tuple[str, str, int], int] = {}

        episode_rows = (
            Item.objects.filter(
                media_type=MediaTypes.EPISODE.value,
                media_id__in=media_ids,
                source__in=media_sources,
                season_number__in=season_numbers,
                release_datetime__isnull=False,
                release_datetime__lte=current_datetime,
            )
            .values("media_id", "source", "season_number")
            .annotate(max_episode=models.Max("episode_number"))
        )

        for row in episode_rows:
            key = (row["media_id"], row["source"], row["season_number"])
            max_episode = row["max_episode"] or 0
            released_by_season[key] = max(released_by_season.get(key, 0), max_episode)

        released_events = (
            events.models.Event.objects.filter(
                item__media_id__in=media_ids,
                item__source__in=media_sources,
                item__media_type=MediaTypes.SEASON.value,
                item__season_number__in=season_numbers,
                datetime__lte=current_datetime,
                content_number__isnull=False,
            )
            .exclude(datetime__year__lt=1900)
            .values(
                "item__media_id",
                "item__source",
                "item__season_number",
            )
            .annotate(max_episode=models.Max("content_number"))
        )

        for row in released_events:
            key = (row["item__media_id"], row["item__source"], row["item__season_number"])
            max_episode = row["max_episode"] or 0
            released_by_season[key] = max(released_by_season.get(key, 0), max_episode)

        for season in season_list:
            key = (season.item.media_id, season.item.source, season.item.season_number)
            season.max_progress = released_by_season.get(key)

    def fetch_media_for_items(self, media_types, item_ids, user, status_filter=None):
        """Fetch media objects for given items, optionally filtering by status.

        Args:
            media_types: Iterable of media type strings to query
            item_ids: QuerySet or list of item IDs to fetch media for
            user: User to filter media by
            status_filter: Optional status value to filter by

        Returns:
            dict mapping item_id to media object
        """
        media_by_item_id = {}

        for media_type in media_types:
            model = apps.get_model("app", media_type)

            if media_type == MediaTypes.EPISODE.value:
                filter_kwargs = {
                    "item__in": item_ids,
                    "related_season__user": user,
                }
                if status_filter:
                    filter_kwargs["related_season__status"] = status_filter
            else:
                filter_kwargs = {
                    "item__in": item_ids,
                    "user": user,
                }
                if status_filter:
                    filter_kwargs["status"] = status_filter

            queryset = model.objects.filter(**filter_kwargs).select_related("item")
            queryset = self._apply_prefetch_related(queryset, media_type)
            self.annotate_max_progress(queryset, media_type)

            for entry in queryset:
                media_by_item_id.setdefault(entry.item_id, entry)

        return media_by_item_id

    def get_media(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get user media object given the media type and item."""
        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._get_media_params(
            user,
            media_type,
            instance_id,
        )

        return model.objects.get(**params)

    def get_media_prefetch(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get user media object with prefetch_related applied."""
        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._get_media_params(
            user,
            media_type,
            instance_id,
        )

        queryset = model.objects.filter(**params)

        queryset = self._apply_prefetch_related(queryset, media_type)
        self.annotate_max_progress(queryset, media_type)

        return queryset[0]

    def _get_media_params(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get the common filter parameters for media queries."""
        params = {"id": instance_id}

        if media_type == MediaTypes.EPISODE.value:
            params["related_season__user"] = user
        else:
            params["user"] = user

        return params

    def filter_media(
        self,
        user,
        media_id,
        media_type,
        source,
        season_number=None,
        episode_number=None,
    ):
        """Filter media objects based on parameters."""
        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._filter_media_params(
            media_type,
            media_id,
            source,
            user,
            season_number,
            episode_number,
        )

        return model.objects.filter(**params)

    def filter_media_prefetch(
        self,
        user,
        media_id,
        media_type,
        source,
        season_number=None,
        episode_number=None,
    ):
        """Filter user media object with prefetch_related applied."""
        queryset = self.filter_media(
            user,
            media_id,
            media_type,
            source,
            season_number,
            episode_number,
        )
        queryset = self._apply_prefetch_related(queryset, media_type)
        self.annotate_max_progress(queryset, media_type)

        return queryset

    def _filter_media_params(
        self,
        media_type,
        media_id,
        source,
        user,
        season_number=None,
        episode_number=None,
    ):
        """Get the common filter parameters for media queries."""
        params = {
            "item__media_type": media_type,
            "item__source": source,
            "item__media_id": media_id,
        }

        if media_type == MediaTypes.SEASON.value:
            params["item__season_number"] = season_number
            params["user"] = user
        elif media_type == MediaTypes.EPISODE.value:
            params["item__season_number"] = season_number
            params["item__episode_number"] = episode_number
            params["related_season__user"] = user
        else:
            params["user"] = user

        return params


class Status(models.TextChoices):
    """Choices for item status."""

    COMPLETED = "Completed", "Completed"
    IN_PROGRESS = "In progress", "In Progress"
    PLANNING = "Planning", "Planning"
    PAUSED = "Paused", "Paused"
    DROPPED = "Dropped", "Dropped"


class Media(models.Model):
    """Abstract model for all media types."""

    history = HistoricalRecords(
        cascade_delete_history=True,
        inherit=True,
        excluded_fields=[
            "item",
            "progressed_at",
            "user",
            "related_tv",
            "created_at",
        ],
    )

    created_at = models.DateTimeField(auto_now_add=True)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    score = models.DecimalField(
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=1,
        validators=[
            DecimalValidator(3, 1),
            MinValueValidator(0),
            MaxValueValidator(10),
        ],
    )
    progress = models.PositiveIntegerField(default=0)
    progressed_at = MonitorField(monitor="progress")
    status = models.CharField(
        max_length=20,
        choices=Status,
        default=Status.COMPLETED.value,
    )
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        """Meta options for the model."""

        abstract = True
        ordering = ["user", "item", "-created_at"]

    def __str__(self):
        """Return the title of the media."""
        return self.item.__str__()

    def save(self, *args, **kwargs):
        """Save the media instance."""
        if self.tracker.has_changed("progress"):
            self.process_progress()

        if self.tracker.has_changed("status"):
            self.process_status()

        super().save(*args, **kwargs)

    def _get_local_max_progress(self):
        """Return locally-derived runtime minutes for music/podcast without provider calls."""
        if self.item.media_type == MediaTypes.PODCAST.value:
            return self.item.runtime_minutes

        if self.item.media_type != MediaTypes.MUSIC.value:
            return None

        track = getattr(self, "track", None)
        if track and track.duration_ms:
            return track.duration_ms // 60000

        if self.item.runtime_minutes:
            return self.item.runtime_minutes

        album_id = getattr(self, "album_id", None)
        if album_id and self.item.media_id:
            Track = apps.get_model("app", "Track")
            match = Track.objects.filter(
                album_id=album_id,
                musicbrainz_recording_id=self.item.media_id,
                duration_ms__isnull=False,
            ).first()
            if match and match.duration_ms:
                return match.duration_ms // 60000

        return None

    def process_progress(self):
        """Update fields depending on the progress of the media."""
        if self.progress < 0:
            self.progress = 0
        elif self.status == Status.IN_PROGRESS.value:
            # For podcasts/music use local runtime data; other types use provider metadata.
            if self.item.media_type in (MediaTypes.PODCAST.value, MediaTypes.MUSIC.value):
                max_progress = self._get_local_max_progress()
            else:
                try:
                    max_progress = providers.services.get_media_metadata(
                        self.item.media_type,
                        self.item.media_id,
                        self.item.source,
                    )["max_progress"]
                except providers.services.ProviderAPIError:
                    logger.warning(
                        "Unable to fetch max progress for %s (%s/%s)",
                        self.item.media_type,
                        self.item.source,
                        self.item.media_id,
                    )
                    max_progress = None

            if max_progress:
                self.progress = min(self.progress, max_progress)

                if self.progress == max_progress:
                    self.status = Status.COMPLETED.value

                    # For podcasts, don't set end_date here - it's calculated from published date + duration in import
                    # For other media types, set end_date if not already set
                    if self.item.media_type != MediaTypes.PODCAST.value and not self.end_date:
                        now = timezone.now().replace(second=0, microsecond=0)
                        self.end_date = now

    def process_status(self):
        """Update fields depending on the status of the media."""
        if self.status == Status.COMPLETED.value:
            # Music progress is play-count based; don't clamp/overwrite on status transitions.
            if self.item.media_type == MediaTypes.MUSIC.value:
                max_progress = None
            # For podcasts, use runtime_minutes from Item instead of external metadata.
            elif self.item.media_type == MediaTypes.PODCAST.value:
                max_progress = self._get_local_max_progress()
            else:
                try:
                    max_progress = providers.services.get_media_metadata(
                        self.item.media_type,
                        self.item.media_id,
                        self.item.source,
                    )["max_progress"]
                except providers.services.ProviderAPIError:
                    logger.warning(
                        "Unable to fetch max progress for %s (%s/%s)",
                        self.item.media_type,
                        self.item.source,
                        self.item.media_id,
                    )
                    max_progress = None

            if max_progress:
                self.progress = max_progress

        if self.item.media_type not in (MediaTypes.MUSIC.value, MediaTypes.PODCAST.value):
            self.item.fetch_releases(delay=True)

    @property
    def formatted_score(self):
        """Return as int if score is 10.0 or 0.0, otherwise show decimal."""
        if self.score is not None:
            max_score = 10
            min_score = 0
            if self.score in (max_score, min_score):
                return int(self.score)
            return self.score
        return None

    @property
    def formatted_progress(self):
        """Return the progress of the media in a formatted string."""
        return str(self.progress)

    @property
    def formatted_aggregated_progress(self):
        """Return formatted aggregated progress string."""
        if hasattr(self, "aggregated_progress") and self.aggregated_progress is not None:
            # Format based on media type
            if hasattr(self, "item") and self.item.media_type == MediaTypes.GAME.value:
                return app.helpers.minutes_to_hhmm(self.aggregated_progress)
            return str(self.aggregated_progress)
        return str(self.progress)

    @property
    def episodes_left(self):
        """Return the number of episodes left to watch."""
        if not hasattr(self, "max_progress") or self.max_progress is None:
            return 0
        return max(0, self.max_progress - self.progress)

    @property
    def time_left(self):
        """Return the estimated time left to complete the show in minutes.

        For accuracy, this sums actual episode runtimes for unwatched episodes
        from the Item table, falling back to averages only when data is unavailable.
        """
        if not hasattr(self, "max_progress") or self.max_progress is None:
            return 0

        episodes_left = self.episodes_left
        if episodes_left <= 0:
            return 0

        # First, try to sum actual unwatched episode runtimes from Item table
        total_from_items = self._calc_unwatched_runtime_from_items(episodes_left)
        if total_from_items is not None:
            return total_from_items

        # Fallback: use average runtime × episodes_left
        runtime_minutes = self._get_fallback_runtime_minutes()

        # Skip shows with unrealistic runtime (999999 fallback)
        if runtime_minutes >= 999999:
            return 0

        return episodes_left * runtime_minutes

    def _calc_unwatched_runtime_from_items(self, episodes_left):
        """Sum actual runtimes for unwatched episodes from Item table.

        Returns total runtime in minutes, or None if data is unavailable.
        """
        season_number = getattr(self.item, "season_number", None)

        if self.item.media_type == MediaTypes.SEASON.value and season_number:
            # For a Season: query episodes in this season where episode_number > progress
            # Only count episodes that have actually been released (have aired)
            current_datetime = timezone.now()
            unwatched_episodes = Item.objects.filter(
                media_id=self.item.media_id,
                source=self.item.source,
                media_type=MediaTypes.EPISODE.value,
                season_number=season_number,
                episode_number__gt=self.progress,
                runtime_minutes__isnull=False,
                release_datetime__isnull=False,  # Only count episodes with air dates
                release_datetime__lte=current_datetime,  # Only count episodes that have aired
            ).exclude(
                runtime_minutes=999999,  # Exclude placeholder for unknown runtime
            ).exclude(
                runtime_minutes=999998,  # Exclude 999998 marker for "aired but runtime unknown"
            ).values_list("runtime_minutes", flat=True)

            runtimes = list(unwatched_episodes)
            if runtimes:
                total = sum(runtimes)
                # If we have data for all unwatched episodes, return exact sum
                if len(runtimes) == episodes_left:
                    return total
                # Partial data: estimate missing episodes using average of known
                missing_eps = episodes_left - len(runtimes)
                avg_runtime = total / len(runtimes)
                return total + int(missing_eps * avg_runtime)

        elif self.item.media_type == MediaTypes.TV.value:
            # For TV show: need to aggregate across seasons
            # Use released_episode_breakdown if available
            breakdown = getattr(self, "released_episode_breakdown", None)
            if breakdown:
                total_runtime = 0
                episodes_with_data = 0
                remaining_progress = self.progress

                for season_num in sorted(breakdown.keys()):
                    season_episode_count = breakdown[season_num]

                    if remaining_progress >= season_episode_count:
                        remaining_progress -= season_episode_count
                    else:
                        watched_in_season = remaining_progress
                        remaining_progress = 0

                        unwatched_episodes = Item.objects.filter(
                            media_id=self.item.media_id,
                            source=self.item.source,
                            media_type=MediaTypes.EPISODE.value,
                            season_number=season_num,
                            episode_number__gt=watched_in_season,
                            runtime_minutes__isnull=False,
                        ).exclude(
                            runtime_minutes=999999,  # Exclude placeholder for unknown runtime
                        ).exclude(
                            runtime_minutes=999998,  # Exclude 999998 marker for "aired but runtime unknown"
                        ).values_list("runtime_minutes", flat=True)

                        runtimes = list(unwatched_episodes)
                        if runtimes:
                            total_runtime += sum(runtimes)
                            episodes_with_data += len(runtimes)

                if episodes_with_data > 0:
                    if episodes_with_data == episodes_left:
                        return total_runtime
                    # Partial data: estimate missing
                    missing_eps = episodes_left - episodes_with_data
                    avg_runtime = total_runtime / episodes_with_data
                    return total_runtime + int(missing_eps * avg_runtime)

        return None  # Signal to use fallback

    def _get_fallback_runtime_minutes(self):
        """Get average runtime for fallback calculation."""
        from django.core.cache import cache

        from app.statistics import parse_runtime_to_minutes

        runtime_minutes = None

        # First, try to get from TV show runtime
        if hasattr(self, "item") and self.item.runtime_minutes:
            if self.item.runtime_minutes < 999999:
                runtime_minutes = self.item.runtime_minutes

        if not runtime_minutes:
            # Try to get from season cache
            season_cache_key = f"tmdb_season_{self.item.media_id}_1"
            cached_season_data = cache.get(season_cache_key)

            if cached_season_data and cached_season_data.get("details", {}).get("runtime"):
                runtime_str = cached_season_data["details"]["runtime"]
                runtime_minutes = parse_runtime_to_minutes(runtime_str)
            else:
                # Try other seasons
                for season_num in [2, 3, 4, 5]:
                    season_cache_key = f"tmdb_season_{self.item.media_id}_{season_num}"
                    cached_season_data = cache.get(season_cache_key)
                    if cached_season_data and cached_season_data.get("details", {}).get("runtime"):
                        runtime_str = cached_season_data["details"]["runtime"]
                        runtime_minutes = parse_runtime_to_minutes(runtime_str)
                        break

        # Use fallback values if nothing found
        if runtime_minutes is None:
            if self.item.source == "tmdb":
                runtime_minutes = 30
            elif self.item.source == "mal":
                runtime_minutes = 23
            else:
                runtime_minutes = 30

        return runtime_minutes

    @property
    def formatted_time_left(self):
        """Return the time left in a human-readable format."""
        time_left_minutes = self.time_left
        if time_left_minutes <= 0:
            return "0m"

        hours = time_left_minutes // 60
        minutes = time_left_minutes % 60

        if hours > 0:
            if minutes > 0:
                return f"{hours}h {minutes}m"
            return f"{hours}h"
        return f"{minutes}m"

    def increase_progress(self):
        """Increase the progress of the media by one."""
        self.progress += 1
        self.save()
        logger.info("Incresed progress of %s to %s", self, self.progress)

    def decrease_progress(self):
        """Decrease the progress of the media by one."""
        self.progress -= 1
        self.save()
        logger.info("Decreased progress of %s to %s", self, self.progress)


class BasicMedia(Media):
    """Model for basic media types."""

    objects = MediaManager()


class TV(Media):
    """Model for TV shows."""

    tracker = FieldTracker()

    class Meta:
        """Meta options for the model."""

        ordering = ["user", "item"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "item"],
                name="%(app_label)s_%(class)s_unique_item_user",
            ),
        ]

    @tracker  # postpone field reset until after the save
    def save(self, *args, **kwargs):
        """Save the media instance."""
        is_create = self._state.adding
        super(Media, self).save(*args, **kwargs)

        if not is_create and self.tracker.has_changed("status"):
            if self.status == Status.COMPLETED.value:
                self._completed()

            elif self.status == Status.DROPPED.value:
                self._mark_in_progress_seasons_as_dropped()

            elif (
                self.status == Status.IN_PROGRESS.value
                and not self.seasons.filter(status=Status.IN_PROGRESS.value).exists()
            ):
                self._start_next_available_season()

            self.item.fetch_releases(delay=True)
        elif (
            not is_create
            and self.status == Status.IN_PROGRESS.value
            and not self.seasons.filter(status=Status.IN_PROGRESS.value).exists()
        ):
            # Keep TV+Season state aligned even when status was already IN_PROGRESS.
            self._start_next_available_season()

        cache_utils.clear_time_left_cache_for_user(self.user_id)

    @property
    def progress(self):
        """Return the total episodes watched for the TV show."""
        return sum(
            season.progress
            for season in self.seasons.all()
            if season.item.season_number != 0
        )

    @property
    def last_watched(self):
        """Return the latest watched episode in SxxExx format."""
        watched_episodes = [
            {
                "season": season.item.season_number,
                "episode": episode.item.episode_number,
                "end_date": episode.end_date,
            }
            for season in self.seasons.all()
            if hasattr(season, "episodes") and season.item.season_number != 0
            for episode in season.episodes.all()
            if episode.end_date is not None
        ]

        if not watched_episodes:
            return ""

        latest_episode = max(
            watched_episodes,
            key=lambda x: (x["end_date"], x["season"], x["episode"]),
        )

        return f"S{latest_episode['season']:02d}E{latest_episode['episode']:02d}"

    @property
    def progressed_at(self):
        """Return the date when the last episode was watched."""
        dates = [
            season.progressed_at
            for season in self.seasons.all()
            if season.progressed_at and season.item.season_number != 0
        ]
        return max(dates) if dates else None

    @property
    def start_date(self):
        """Return the date of the first episode watched."""
        dates = [
            season.start_date
            for season in self.seasons.all()
            if season.start_date and season.item.season_number != 0
        ]
        if dates:
            return min(dates)
        if self.status == Status.IN_PROGRESS.value:
            return self.created_at
        return None

    @property
    def end_date(self):
        """Return the date of the last episode watched."""
        dates = [
            season.end_date
            for season in self.seasons.all()
            if season.end_date and season.item.season_number != 0
        ]
        return max(dates) if dates else None

    def _completed(self):
        """Create remaining seasons and episodes for a TV show."""
        tv_metadata = providers.services.get_media_metadata(
            self.item.media_type,
            self.item.media_id,
            self.item.source,
        )
        max_progress = tv_metadata["max_progress"]

        if not max_progress or self.progress > max_progress:
            return

        seasons_to_create = []
        seasons_to_update = []
        episodes_to_create = []

        season_numbers = [
            season["season_number"]
            for season in tv_metadata["related"]["seasons"]
            if season["season_number"] != 0
        ]
        tv_with_seasons_metadata = providers.services.get_media_metadata(
            "tv_with_seasons",
            self.item.media_id,
            self.item.source,
            season_numbers,
        )
        for season_number in season_numbers:
            season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

            # Use season poster if available, otherwise fallback to TV show poster
            season_image = season_metadata.get("image") or self.item.image

            item, _ = Item.objects.get_or_create(
                media_id=self.item.media_id,
                source=self.item.source,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
                defaults={
                    **Item.title_fields_from_metadata(
                        season_metadata,
                        fallback_title=self.item.title,
                    ),
                    "image": season_image,
                },
            )
            try:
                season_instance = Season.objects.get(
                    item=item,
                    user=self.user,
                )

                if season_instance.status != Status.COMPLETED.value:
                    season_instance.status = Status.COMPLETED.value
                    seasons_to_update.append(season_instance)

            except Season.DoesNotExist:
                seasons_to_create.append(
                    Season(
                        item=item,
                        score=None,
                        status=Status.COMPLETED.value,
                        notes="",
                        related_tv=self,
                        user=self.user,
                    ),
                )

        bulk_create_with_history(seasons_to_create, Season)
        bulk_update_with_history(seasons_to_update, Season, ["status"])

        for season_instance in seasons_to_create + seasons_to_update:
            season_metadata = tv_with_seasons_metadata[
                f"season/{season_instance.item.season_number}"
            ]
            episodes_to_create.extend(
                season_instance.get_remaining_eps(season_metadata),
            )
        bulk_create_with_history(episodes_to_create, Episode)

    def _mark_in_progress_seasons_as_dropped(self):
        """Mark all in-progress seasons as dropped."""
        in_progress_seasons = list(
            self.seasons.filter(status=Status.IN_PROGRESS.value),
        )

        for season in in_progress_seasons:
            season.status = Status.DROPPED.value

        if in_progress_seasons:
            bulk_update_with_history(
                in_progress_seasons,
                Season,
                fields=["status"],
            )

    def _start_next_available_season(self):
        """Find the next available season to watch and set it to in-progress."""
        all_seasons = self.seasons.filter(
            item__season_number__gt=0,
        ).order_by("item__season_number")

        next_unwatched_season = all_seasons.exclude(
            status__in=[Status.COMPLETED.value],
        ).first()

        if not next_unwatched_season:
            # If all existing seasons are watched, get the next available season
            tv_metadata = providers.services.get_media_metadata(
                self.item.media_type,
                self.item.media_id,
                self.item.source,
            )

            existing_season_numbers = set(
                all_seasons.values_list("item__season_number", flat=True),
            )

            for season_data in tv_metadata["related"]["seasons"]:
                season_number = season_data["season_number"]
                if season_number > 0 and season_number not in existing_season_numbers:
                    # Use season poster if available, otherwise fallback to TV show poster
                    season_image = season_data.get("image") or self.item.image

                    item, _ = Item.objects.get_or_create(
                        media_id=self.item.media_id,
                        source=self.item.source,
                        media_type=MediaTypes.SEASON.value,
                        season_number=season_data["season_number"],
                        defaults={
                            **Item.title_fields_from_metadata(
                                season_data,
                                fallback_title=self.item.title,
                            ),
                            "image": season_image,
                        },
                    )

                    next_unwatched_season = Season(
                        item=item,
                        user=self.user,
                        related_tv=self,
                        status=Status.IN_PROGRESS.value,
                    )
                    bulk_create_with_history([next_unwatched_season], Season)
                    break

        elif next_unwatched_season.status != Status.IN_PROGRESS.value:
            next_unwatched_season.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [next_unwatched_season],
                Season,
                fields=["status"],
            )


class Season(Media):
    """Model for seasons of TV shows."""

    related_tv = models.ForeignKey(
        TV,
        on_delete=models.CASCADE,
        related_name="seasons",
    )

    tracker = FieldTracker()

    class Meta:
        """Limit the uniqueness of seasons.

        Only one season per media can have the same season number.
        """

        constraints = [
            models.UniqueConstraint(
                fields=["related_tv", "item"],
                name="%(app_label)s_season_unique_tv_item",
            ),
        ]

    def __str__(self):
        """Return the title of the media and season number."""
        return f"{self.item.title} S{self.item.season_number}"

    @tracker  # postpone field reset until after the save
    def save(self, *args, **kwargs):
        """Save the media instance."""
        # if related_tv is not set
        if self.related_tv_id is None:
            self.related_tv = self.get_tv()

        is_create = self._state.adding
        super(Media, self).save(*args, **kwargs)

        if not is_create and self.tracker.has_changed("status"):
            if self.status == Status.COMPLETED.value:
                season_metadata = providers.services.get_media_metadata(
                    MediaTypes.SEASON.value,
                    self.item.media_id,
                    self.item.source,
                    [self.item.season_number],
                )
                episodes_to_create = self.get_remaining_eps(season_metadata)
                if episodes_to_create:
                    bulk_create_with_history(
                        episodes_to_create,
                        Episode,
                    )

            elif (
                self.status == Status.DROPPED.value
                and self.related_tv.status != Status.DROPPED.value
            ):
                self.related_tv.status = Status.DROPPED.value
                bulk_update_with_history(
                    [self.related_tv],
                    TV,
                    fields=["status"],
                )

            elif (
                self.status == Status.IN_PROGRESS.value
                and self.related_tv.status != Status.IN_PROGRESS.value
            ):
                self.related_tv.status = Status.IN_PROGRESS.value
                bulk_update_with_history(
                    [self.related_tv],
                    TV,
                    fields=["status"],
                )

            self.item.fetch_releases(delay=True)

        cache_utils.clear_time_left_cache_for_user(self.user_id)

    @property
    def progress(self):
        """Return the current episode number of the season.
        
        For rewatching: only considers it a rewatch if ALL episodes up to that point
        have been watched at least that many times. Otherwise uses max episode number
        to ignore errant repeats.
        """
        stats = self._get_episode_stats()
        episode_counts = stats["episode_counts"]
        if not episode_counts:
            return 0

        if self.status == Status.IN_PROGRESS.value:
            # Check for systematic rewatching: only consider it a rewatch if ALL episodes
            # up to that point have been watched at least that many times.
            # This prevents errant repeats (single episode watched twice) from skewing progress.
            sorted_episode_nums = sorted(episode_counts.keys())
            max_rewatch_level = 0
            max_rewatch_progress = 0

            # Check each possible rewatch level (2, 3, ...)
            # Level 1 is just normal watching, so we start at 2
            for rewatch_level in range(2, max(episode_counts.values()) + 1):
                # Find the highest episode number where all episodes up to it have at least this many watches
                consistent_up_to = 0
                for ep_num in sorted_episode_nums:
                    if episode_counts[ep_num] >= rewatch_level:
                        consistent_up_to = ep_num
                    else:
                        # Can't be a consistent rewatch beyond this point
                        break

                if consistent_up_to > max_rewatch_progress:
                    max_rewatch_level = rewatch_level
                    max_rewatch_progress = consistent_up_to

            # If we found a consistent rewatch pattern, use it
            if max_rewatch_level > 1 and max_rewatch_progress > 0:
                return max_rewatch_progress

        # Otherwise, use the maximum episode number watched (at least once)
        # This handles normal watching and errant repeats
        return stats["max_episode_number"]

    @property
    def completed_episode_count(self):
        """Return the number of unique episodes with a completed play."""
        stats = self._get_episode_stats()
        return len(stats["completed_episode_numbers"])

    def _get_episode_stats(self):
        """Return cached episode stats for this season."""
        cached = getattr(self, "_episode_stats_cache", None)
        if cached is not None:
            return cached

        episodes = list(self.episodes.all())
        episode_counts = {}
        completed_episode_numbers = set()
        max_episode_number = 0

        for ep in episodes:
            ep_num = ep.item.episode_number
            episode_counts[ep_num] = episode_counts.get(ep_num, 0) + 1
            if ep_num and ep_num > max_episode_number:
                max_episode_number = ep_num
            if ep.end_date is not None:
                completed_episode_numbers.add(ep_num)

        cached = {
            "episode_counts": episode_counts,
            "completed_episode_numbers": completed_episode_numbers,
            "max_episode_number": max_episode_number,
        }
        self._episode_stats_cache = cached
        return cached

    @property
    def progressed_at(self):
        """Return the date when the last episode was watched."""
        dates = [
            episode.end_date
            for episode in self.episodes.all()
            if episode.end_date is not None
        ]
        return max(dates) if dates else None

    @property
    def start_date(self):
        """Return the date of the first episode watched."""
        dates = [
            episode.end_date
            for episode in self.episodes.all()
            if episode.end_date is not None
        ]
        return min(dates) if dates else None

    @property
    def end_date(self):
        """Return the date of the last episode watched."""
        dates = [
            episode.end_date
            for episode in self.episodes.all()
            if episode.end_date is not None
        ]
        return max(dates) if dates else None

    def increase_progress(self):
        """Watch the next episode of the season."""
        season_metadata = providers.services.get_media_metadata(
            MediaTypes.SEASON.value,
            self.item.media_id,
            self.item.source,
            [self.item.season_number],
        )
        episodes = season_metadata["episodes"]

        if self.progress == 0:
            # start watching from the first episode
            next_episode_number = episodes[0]["episode_number"]
        else:
            next_episode_number = providers.tmdb.find_next_episode(
                self.progress,
                episodes,
            )

        now = timezone.now().replace(second=0, microsecond=0)

        if next_episode_number:
            self.watch(next_episode_number, now)
        else:
            logger.info("No more episodes to watch.")

    def watch(self, episode_number, end_date):
        """Create or add a repeat to an episode of the season."""
        item = self.get_episode_item(episode_number)

        episode = Episode.objects.create(
            related_season=self,
            item=item,
            end_date=end_date,
        )
        logger.info(
            "%s created successfully.",
            episode,
        )
        cache_utils.clear_time_left_cache_for_user(self.user_id)

    def decrease_progress(self):
        """Unwatch the current episode of the season."""
        self.unwatch(self.progress)

    def unwatch(self, episode_number):
        """Unwatch the episode instance."""
        item = self.get_episode_item(episode_number)

        episodes = Episode.objects.filter(
            related_season=self,
            item=item,
        ).order_by("-end_date")

        episode = episodes.first()

        if episode is None:
            logger.warning(
                "Episode %s does not exist.",
                self.item,
            )
            return

        # Get count before deletion for logging
        remaining_count = episodes.count() - 1

        episode.delete()
        logger.info(
            "Deleted %s S%02dE%02d (%d remaining instances)",
            self.item.title,
            self.item.season_number,
            episode_number,
            remaining_count,
        )

        # Re-evaluate season/TV status after deletion so completed shows don't stay "In progress"
        self._sync_status_after_episode_change()
        cache_utils.clear_time_left_cache_for_user(self.user_id)

    def _sync_status_after_episode_change(self):
        """Recalculate season (and TV) status using local data (no provider calls)."""
        if self.status == Status.DROPPED.value:
            return
        if self.status == Status.PAUSED.value:
            return

        # What episodes do we have logged?
        episode_numbers = set(
            self.episodes.values_list("item__episode_number", flat=True),
        )
        episode_numbers.discard(None)
        max_watched = max(episode_numbers) if episode_numbers else 0

        # Best local hint for total episodes: release events in the DB
        total_eps = (
            events.models.Event.objects.filter(
                item=self.item,
                content_number__isnull=False,
                datetime__lte=timezone.now(),
            ).aggregate(max_ep=Max("content_number"))["max_ep"]
            or 0
        )

        desired_status = None

        if total_eps > 0 and max_watched >= total_eps:
            # We know how many have released and we've logged them all
            desired_status = Status.COMPLETED.value
        elif max_watched > 0 and total_eps == 0:
            # No release data, but we have watches — stay in progress
            desired_status = Status.IN_PROGRESS.value
        elif max_watched > 0:
            desired_status = Status.IN_PROGRESS.value
        else:
            desired_status = Status.PLANNING.value

        season_updates = []
        if desired_status and self.status != desired_status:
            self.status = desired_status
            season_updates.append(self)

        # Align the parent TV unless it was dropped explicitly
        tv_updates = []
        tv = getattr(self, "related_tv", None)
        if tv and tv.status != Status.DROPPED.value and desired_status:
            if desired_status == Status.COMPLETED.value:
                # Only mark TV complete if all real seasons are complete
                has_incomplete = tv.seasons.filter(
                    item__season_number__gt=0,
                ).exclude(status=Status.COMPLETED.value).exists()
                tv_target = (
                    Status.COMPLETED.value
                    if not has_incomplete
                    else Status.IN_PROGRESS.value
                )
            else:
                tv_target = Status.IN_PROGRESS.value

            if tv.status != tv_target:
                tv.status = tv_target
                tv_updates.append(tv)

        if season_updates:
            bulk_update_with_history(season_updates, Season, fields=["status"])
        if tv_updates:
            bulk_update_with_history(tv_updates, TV, fields=["status"])

    def get_tv(self):
        """Get related TV instance for a season and create it if it doesn't exist."""
        try:
            tv = TV.objects.get(
                item__media_id=self.item.media_id,
                item__media_type=MediaTypes.TV.value,
                item__season_number=None,
                item__source=self.item.source,
                user=self.user,
            )
        except TV.DoesNotExist:
            fallback_title = self.item.series_name or self.item.title
            try:
                tv_metadata = providers.services.get_media_metadata(
                    MediaTypes.TV.value,
                    self.item.media_id,
                    self.item.source,
                )
                season_count = tv_metadata.get("details", {}).get("seasons")
                if season_count is None:
                    season_count = len(tv_metadata.get("related", {}).get("seasons", []))
            except Exception as exc:  # pragma: no cover - defensive for test/no-network paths
                logger.warning(
                    "Could not fetch TV metadata for media_id=%s while creating season parent: %s",
                    self.item.media_id,
                    exc,
                )
                tv_metadata = {
                    "title": fallback_title,
                    "localized_title": fallback_title,
                    "original_title": None,
                    "image": self.item.image,
                    "details": {},
                    "related": {"seasons": []},
                }
                season_count = None

            # creating tv with multiple seasons from a completed season
            if self.status == Status.COMPLETED.value and season_count > 1:
                status = Status.IN_PROGRESS.value
            else:
                status = self.status

            item, _ = Item.objects.get_or_create(
                media_id=self.item.media_id,
                source=self.item.source,
                media_type=MediaTypes.TV.value,
                defaults={
                    **Item.title_fields_from_metadata(
                        tv_metadata,
                        fallback_title=fallback_title,
                    ),
                    "image": tv_metadata.get("image") or self.item.image,
                },
            )

            tv = TV(
                item=item,
                score=None,
                status=status,
                notes="",
                user=self.user,
            )

            # save_base to avoid custom save method
            TV.save_base(tv)

            logger.info("%s did not exist, it was created successfully.", tv)

        return tv

    def get_remaining_eps(self, season_metadata):
        """Return episodes needed to complete a season."""
        latest_watched_ep_num = Episode.objects.filter(related_season=self).aggregate(
            latest_watched_ep_num=Max("item__episode_number"),
        )["latest_watched_ep_num"]

        if latest_watched_ep_num is None:
            latest_watched_ep_num = 0

        episodes_to_create = []

        # Calculate current time once before the loop
        now = timezone.now().replace(second=0, microsecond=0)

        # Create Episode objects for the remaining episodes
        for episode in reversed(season_metadata["episodes"]):
            if episode["episode_number"] <= latest_watched_ep_num:
                break

            item = self.get_episode_item(episode["episode_number"], season_metadata)

            # Resolve end_date based on user preference
            end_date = self.user.resolve_watch_date(now, episode.get("air_date"))

            episode_db = Episode(
                related_season=self,
                item=item,
                end_date=end_date,
            )
            episodes_to_create.append(episode_db)

        return episodes_to_create

    def get_episode_item(self, episode_number, season_metadata=None):
        """Get the episode item instance, create it if it doesn't exist."""
        if not season_metadata:
            season_metadata = providers.services.get_media_metadata(
                MediaTypes.SEASON.value,
                self.item.media_id,
                self.item.source,
                [self.item.season_number],
            )

        image = settings.IMG_NONE
        runtime_minutes = None
        release_datetime = None
        
        for episode in season_metadata["episodes"]:
            if episode["episode_number"] == int(episode_number):
                if episode.get("still_path"):
                    image = (
                        f"https://image.tmdb.org/t/p/original{episode['still_path']}"
                    )
                elif "image" in episode:
                    # for manual seasons
                    image = episode["image"]
                else:
                    image = settings.IMG_NONE

                # Extract runtime from episode metadata (raw TMDB data has integer runtime in minutes)
                if episode.get("runtime") is not None:
                    # Runtime is an integer (minutes) from TMDB
                    runtime_minutes = int(episode["runtime"]) if episode["runtime"] > 0 else None
                
                # Extract release_datetime from episode air_date
                air_date = episode.get("air_date")
                if air_date:
                    from datetime import datetime
                    from django.utils import timezone
                    
                    try:
                        # TMDB returns dates in YYYY-MM-DD format (string)
                        if isinstance(air_date, str):
                            date_obj = datetime.strptime(air_date, "%Y-%m-%d")
                            release_datetime = timezone.make_aware(date_obj, timezone.get_current_timezone())
                        elif hasattr(air_date, "year"):
                            # Already a datetime object
                            release_datetime = air_date if timezone.is_aware(air_date) else timezone.make_aware(air_date)
                    except (ValueError, TypeError):
                        # If parsing fails, keep release_datetime as None
                        pass
                
                break

        item, created = Item.objects.get_or_create(
            media_id=self.item.media_id,
            source=self.item.source,
            media_type=MediaTypes.EPISODE.value,
            season_number=self.item.season_number,
            episode_number=episode_number,
            defaults={
                **Item.title_fields_from_metadata({"title": self.item.title}),
                "image": image,
                "runtime_minutes": runtime_minutes,
                "release_datetime": release_datetime,
            },
        )

        # Update fields if not set and we have them now
        updated = False
        if not created:
            if not item.runtime_minutes and runtime_minutes:
                item.runtime_minutes = runtime_minutes
                updated = True
            if not item.release_datetime and release_datetime:
                item.release_datetime = release_datetime
                updated = True
            if updated:
                item.save(update_fields=["runtime_minutes", "release_datetime"])
        elif created:
            # Ensure runtime and release_datetime are set for newly created items
            needs_save = False
            if runtime_minutes and not item.runtime_minutes:
                item.runtime_minutes = runtime_minutes
                needs_save = True
            if release_datetime and not item.release_datetime:
                item.release_datetime = release_datetime
                needs_save = True
            if needs_save:
                item.save(update_fields=["runtime_minutes", "release_datetime"])

        return item


class Episode(models.Model):
    """Model for episodes of a season."""

    history = HistoricalRecords(
        cascade_delete_history=True,
        excluded_fields=["item", "related_season", "created_at"],
    )

    created_at = models.DateTimeField(auto_now_add=True)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, null=True)
    related_season = models.ForeignKey(
        Season,
        on_delete=models.CASCADE,
        related_name="episodes",
    )
    end_date = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Meta options for the model."""

        ordering = [
            "related_season",
            "item__episode_number",
            "-end_date",
            "-created_at",
        ]

    def __str__(self):
        """Return the season and episode number."""
        return self.item.__str__()

    @property
    def status(self):
        """Expose season status for UI components that expect media.status."""
        if hasattr(self, "_status_override"):
            return self._status_override
        related_season = getattr(self, "related_season", None)
        return related_season.status if related_season else None

    @status.setter
    def status(self, value):
        self._status_override = value

    @property
    def score(self):
        """Episodes do not carry standalone ratings in Yamtrack."""
        return getattr(self, "_score_override", None)

    @score.setter
    def score(self, value):
        self._score_override = value

    @property
    def progress(self):
        """Expose episode number as progress for list rendering/sorting fallbacks."""
        if hasattr(self, "_progress_override"):
            return self._progress_override
        item = getattr(self, "item", None)
        return item.episode_number if item else None

    @progress.setter
    def progress(self, value):
        self._progress_override = value

    @property
    def max_progress(self):
        """Expose related season max progress when available."""
        if hasattr(self, "_max_progress_override"):
            return self._max_progress_override
        related_season = getattr(self, "related_season", None)
        return getattr(related_season, "max_progress", None)

    @max_progress.setter
    def max_progress(self, value):
        self._max_progress_override = value

    def save(self, *args, **kwargs):
        """Save the episode instance."""
        super().save(*args, **kwargs)

        season_number = self.item.season_number
        if season_number is None:
            return
        try:
            tv_with_seasons_metadata = providers.services.get_media_metadata(
                "tv_with_seasons",
                self.item.media_id,
                self.item.source,
                [season_number],
            )
            season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]
            max_progress = len(season_metadata["episodes"])
        except (
            providers.services.ProviderAPIError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            logger.warning(
                "Skipping Episode status sync due to missing metadata for %s S%sE%s: %s",
                self.item.media_id,
                season_number,
                self.item.episode_number,
                error,
            )
            return

        # clear prefetch cache to get the updated episodes
        self.related_season.refresh_from_db()

        season_just_completed = False
        if self.item.episode_number == max_progress:
            self.related_season.status = Status.COMPLETED.value
            bulk_update_with_history(
                [self.related_season],
                Season,
                fields=["status"],
            )
            season_just_completed = True

        elif self.related_season.status != Status.IN_PROGRESS.value:
            self.related_season.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self.related_season],
                Season,
                fields=["status"],
            )

        if season_just_completed:
            last_season = tv_with_seasons_metadata["related"]["seasons"][-1][
                "season_number"
            ]
            # mark the TV show as completed if it's the last season
            if season_number == last_season:
                self.related_season.related_tv.status = Status.COMPLETED.value
                bulk_update_with_history(
                    [self.related_season.related_tv],
                    TV,
                    fields=["status"],
                )
        elif self.related_season.related_tv.status != Status.IN_PROGRESS.value:
            self.related_season.related_tv.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self.related_season.related_tv],
                TV,
                fields=["status"],
            )


class Manga(Media):
    """Model for manga."""

    tracker = FieldTracker()


class Anime(Media):
    """Model for anime."""

    tracker = FieldTracker()


class Movie(Media):
    """Model for movies."""

    tracker = FieldTracker()


class Game(Media):
    """Model for games."""

    tracker = FieldTracker()

    @property
    def formatted_progress(self):
        """Return progress in hours:minutes format."""
        return app.helpers.minutes_to_hhmm(self.progress)

    def increase_progress(self):
        """Increase the progress of the media by 30 minutes."""
        self.progress += 30
        self.save()
        logger.info("Changed playtime of %s to %s", self, self.formatted_progress)

    def decrease_progress(self):
        """Decrease the progress of the media by 30 minutes."""
        self.progress -= 30
        self.save()
        logger.info("Changed playtime of %s to %s", self, self.formatted_progress)


class BoardGame(Media):
    """Model for board games."""

    tracker = FieldTracker()

    @property
    def formatted_progress(self):
        """Return progress as play count."""
        plays = self.progress or 0
        return f"{plays} play{'s' if plays != 1 else ''}"

    @property
    def formatted_aggregated_progress(self):
        """Return aggregated progress as play count."""
        plays = getattr(self, "aggregated_progress", None)
        value = plays if plays is not None else self.progress
        return f"{value} play{'s' if value != 1 else ''}"


class Book(Media):
    """Model for books."""

    tracker = FieldTracker()


class Comic(Media):
    """Model for comics."""

    tracker = FieldTracker()


class Artist(models.Model):
    """Model for music artists."""

    name = models.CharField(max_length=255)
    sort_name = models.CharField(max_length=255, blank=True, default="")
    musicbrainz_id = models.CharField(
        max_length=36,
        unique=True,
        null=True,
        blank=True,
        help_text="MusicBrainz Artist ID (UUID)",
    )
    image = models.URLField(blank=True, default="")
    country = models.CharField(
        max_length=5,
        blank=True,
        default="",
        help_text="ISO country code from MusicBrainz",
    )
    genres = models.JSONField(
        default=list,
        blank=True,
        help_text="Top genres/tags from MusicBrainz",
    )
    discography_synced_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the discography was last synced from MusicBrainz",
    )

    class Meta:
        """Meta options for the model."""

        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["musicbrainz_id"],
                condition=models.Q(musicbrainz_id__isnull=False),
                name="unique_artist_musicbrainz_id",
            ),
        ]

    def __str__(self):
        """Return the name of the artist."""
        return self.name


class Album(models.Model):
    """Model for music albums."""

    title = models.CharField(max_length=255)
    musicbrainz_release_id = models.CharField(
        max_length=36,
        null=True,
        blank=True,
        help_text="MusicBrainz Release ID (UUID) - one specific release",
    )
    musicbrainz_release_group_id = models.CharField(
        max_length=36,
        null=True,
        blank=True,
        help_text="MusicBrainz Release Group ID (UUID) - groups multiple releases",
    )
    artist = models.ForeignKey(
        Artist,
        on_delete=models.CASCADE,
        related_name="albums",
        null=True,
        blank=True,
    )
    release_date = models.DateField(null=True, blank=True)
    image = models.URLField(blank=True, default="")
    release_type = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Album type: Album, EP, Single, Compilation, etc.",
    )
    genres = models.JSONField(
        default=list,
        blank=True,
        help_text="Genres/tags from MusicBrainz release",
    )
    tracks_populated = models.BooleanField(
        default=False,
        help_text="Whether tracks have been fetched from MusicBrainz",
    )

    class Meta:
        """Meta options for the model."""

        ordering = ["-release_date", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["artist", "musicbrainz_release_group_id"],
                condition=models.Q(musicbrainz_release_group_id__isnull=False),
                name="unique_album_per_artist_release_group",
            ),
        ]

    def __str__(self):
        """Return the album title with artist."""
        if self.artist:
            return f"{self.title} - {self.artist.name}"
        return self.title


class Track(models.Model):
    """Model for music tracks (like Episode for TV).
    
    This represents a track from MusicBrainz metadata, independent of user tracking.
    Populated from MusicBrainz when an album is viewed.
    """

    album = models.ForeignKey(
        Album,
        on_delete=models.CASCADE,
        related_name="tracklist",
    )
    title = models.CharField(max_length=500)
    musicbrainz_recording_id = models.CharField(
        max_length=36,
        null=True,
        blank=True,
        help_text="MusicBrainz Recording ID (UUID)",
    )
    track_number = models.PositiveIntegerField(null=True, blank=True)
    disc_number = models.PositiveIntegerField(default=1)
    duration_ms = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Duration in milliseconds",
    )
    genres = models.JSONField(
        default=list,
        blank=True,
        help_text="Genres/tags for this recording",
    )

    class Meta:
        """Meta options for the model."""

        ordering = ["disc_number", "track_number", "title"]
        constraints = [
            models.UniqueConstraint(
                fields=["album", "disc_number", "track_number"],
                condition=models.Q(track_number__isnull=False),
                name="unique_track_per_album_disc",
            ),
        ]

    def __str__(self):
        """Return the track title."""
        if self.track_number:
            return f"{self.track_number}. {self.title}"
        return self.title

    @property
    def duration_formatted(self):
        """Return duration as mm:ss string."""
        if not self.duration_ms:
            return None
        total_seconds = self.duration_ms // 1000
        minutes = total_seconds // 60
        seconds = total_seconds % 60
        return f"{minutes}:{seconds:02d}"


class Music(Media):
    """Model for music tracks.

    This is the trackable unit for music (per-user tracking),
    backed by an Item with media_type='music'.
    Optionally links to a Track (MusicBrainz catalog entry) for metadata.
    """

    tracker = FieldTracker()

    album = models.ForeignKey(
        Album,
        on_delete=models.SET_NULL,
        related_name="music_entries",
        null=True,
        blank=True,
    )
    artist = models.ForeignKey(
        Artist,
        on_delete=models.SET_NULL,
        related_name="music_entries",
        null=True,
        blank=True,
        help_text="Convenience FK to artist (can be derived via album)",
    )
    track = models.ForeignKey(
        Track,
        on_delete=models.SET_NULL,
        related_name="music_entries",
        null=True,
        blank=True,
        help_text="Link to Track catalog entry from MusicBrainz",
    )

    @property
    def formatted_progress(self):
        """Return progress as play count."""
        plays = self.progress or 0
        return f"{plays} play{'s' if plays != 1 else ''}"

    @property
    def formatted_aggregated_progress(self):
        """Return aggregated progress as play count."""
        plays = getattr(self, "aggregated_progress", None)
        value = plays if plays is not None else self.progress
        return f"{value} play{'s' if value != 1 else ''}"


class ArtistTracker(models.Model):
    """Model for tracking artists in user's library.
    
    This mirrors the Media model fields so artist tracking feels identical
    to TV/Movie tracking in terms of status, score, dates, and notes.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="artist_trackers",
    )
    artist = models.ForeignKey(
        Artist,
        on_delete=models.CASCADE,
        related_name="trackers",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.IN_PROGRESS.value,
    )
    score = models.DecimalField(
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=1,
        validators=[
            DecimalValidator(3, 1),
            MinValueValidator(0),
            MaxValueValidator(10),
        ],
    )
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "artist"],
                name="unique_artist_tracker_per_user",
            ),
        ]

    def __str__(self):
        """Return the tracker string."""
        return f"{self.user.username} - {self.artist.name}"

    @property
    def status_readable(self):
        """Return the human-readable status."""
        return dict(Status.choices).get(self.status, self.status)


class AlbumTracker(models.Model):
    """Model for tracking albums in user's library.
    
    This mirrors the Media model fields so album tracking feels identical
    to TV/Movie tracking in terms of status, score, dates, and notes.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="album_trackers",
    )
    album = models.ForeignKey(
        Album,
        on_delete=models.CASCADE,
        related_name="trackers",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.IN_PROGRESS.value,
    )
    score = models.DecimalField(
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=1,
        validators=[
            DecimalValidator(3, 1),
            MinValueValidator(0),
            MaxValueValidator(10),
        ],
    )
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "album"],
                name="unique_album_tracker_per_user",
            ),
        ]

    def __str__(self):
        """Return the tracker string."""
        return f"{self.user.username} - {self.album.title}"

    @property
    def status_readable(self):
        """Return the human-readable status."""
        return dict(Status.choices).get(self.status, self.status)


class PodcastShow(models.Model):
    """Model for podcast shows (container, not Media subclass).
    
    Similar to Artist in the music hierarchy.
    """

    podcast_uuid = models.CharField(
        max_length=36,
        unique=True,
        help_text="Pocket Casts podcast UUID",
    )
    title = models.CharField(max_length=255)
    slug = models.CharField(max_length=255, blank=True, default="")
    author = models.CharField(max_length=255, blank=True, default="")
    image = models.URLField(blank=True, default="")
    description = models.TextField(blank=True, default="", help_text="Show description from Pocket Casts")
    language = models.CharField(max_length=10, blank=True, default="")
    genres = models.JSONField(default=list, blank=True)
    rss_feed_url = models.URLField(blank=True, default="", help_text="RSS feed URL for fetching full episode list")

    class Meta:
        """Meta options for the model."""

        ordering = ["title"]
        verbose_name = "Podcast Show"
        verbose_name_plural = "Podcast Shows"

    def __str__(self):
        """Return the show title."""
        return self.title


class PodcastEpisode(models.Model):
    """Model for podcast episodes (container, not Media subclass).
    
    Similar to Track in the music hierarchy.
    """

    show = models.ForeignKey(
        PodcastShow,
        on_delete=models.CASCADE,
        related_name="episodes",
    )
    episode_uuid = models.CharField(
        max_length=36,
        unique=True,
        help_text="Pocket Casts episode UUID",
    )
    title = models.CharField(max_length=500)
    slug = models.CharField(max_length=255, blank=True, default="")
    published = models.DateTimeField(null=True, blank=True)
    duration = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Duration in seconds",
    )
    audio_url = models.URLField(blank=True, default="")
    episode_number = models.PositiveIntegerField(null=True, blank=True)
    season_number = models.PositiveIntegerField(null=True, blank=True)
    file_type = models.CharField(max_length=50, blank=True, default="")
    episode_type = models.CharField(max_length=50, blank=True, default="")
    is_deleted = models.BooleanField(default=False)

    class Meta:
        """Meta options for the model."""

        ordering = ["-published", "episode_number"]
        verbose_name = "Podcast Episode"
        verbose_name_plural = "Podcast Episodes"

    def __str__(self):
        """Return the episode title."""
        return self.title

    @property
    def duration_formatted(self):
        """Return duration as mm:ss or hh:mm:ss string."""
        if not self.duration:
            return None
        hours = self.duration // 3600
        minutes = (self.duration % 3600) // 60
        seconds = self.duration % 60
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"


class Podcast(Media):
    """Model for podcast episodes (per-user tracking).
    
    This is the trackable unit for podcasts (per-user tracking),
    backed by an Item with media_type='podcast'.
    Links to PodcastEpisode and PodcastShow for metadata.
    """

    tracker = FieldTracker()

    show = models.ForeignKey(
        PodcastShow,
        on_delete=models.SET_NULL,
        related_name="podcast_entries",
        null=True,
        blank=True,
    )
    episode = models.ForeignKey(
        PodcastEpisode,
        on_delete=models.SET_NULL,
        related_name="podcast_entries",
        null=True,
        blank=True,
    )
    played_up_to_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Last seen playedUpTo in seconds from API",
    )
    last_seen_status = models.IntegerField(
        null=True,
        blank=True,
        help_text="Last seen playingStatus from API (2=in-progress, 3=completed)",
    )

    @property
    def completed_play_count(self):
        """Return count of completed plays (excludes in-progress records).
        
        For podcasts, we only count history records with end_date (completed plays),
        not in-progress records where end_date is None.
        """
        from django.apps import apps
        HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")

        # Count only history records with end_date (completed plays)
        return HistoricalPodcast.objects.filter(
            id=self.id,
            end_date__isnull=False,
        ).count()

    @property
    def formatted_progress(self):
        """Return progress as minutes listened.
        
        For in-progress episodes, shows actual progress from played_up_to_seconds.
        Otherwise shows progress from the progress field.
        """
        # Check if episode is in-progress and has actual progress stored
        is_in_progress = (
            self.status == Status.IN_PROGRESS.value or
            self.last_seen_status == 2  # 2 = in-progress from API
        )

        if is_in_progress and self.played_up_to_seconds and self.played_up_to_seconds > 0:
            # Use actual progress from played_up_to_seconds for in-progress episodes
            minutes = self.played_up_to_seconds // 60
            return f"{minutes}m"

        # Fall back to progress field (in minutes)
        minutes = (self.progress or 0) // 60
        return f"{minutes}m"


class PodcastShowTracker(models.Model):
    """Model for tracking podcast shows in user's library.
    
    This mirrors the Media model fields so show tracking feels identical
    to TV/Movie tracking in terms of status, score, dates, and notes.
    Similar to ArtistTracker for music.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="podcast_show_trackers",
    )
    show = models.ForeignKey(
        PodcastShow,
        on_delete=models.CASCADE,
        related_name="trackers",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.IN_PROGRESS.value,
    )
    score = models.DecimalField(
        null=True,
        blank=True,
        max_digits=3,
        decimal_places=1,
        validators=[
            DecimalValidator(3, 1),
            MinValueValidator(0),
            MaxValueValidator(10),
        ],
    )
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Meta options for the model."""

        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "show"],
                name="unique_podcast_show_tracker_per_user",
            ),
        ]

    def __str__(self):
        """Return the tracker string."""
        return f"{self.user.username} - {self.show.title}"

    @property
    def status_readable(self):
        """Return the human-readable status."""
        return dict(Status.choices).get(self.status, self.status)


class CollectionEntry(models.Model):
    """Model to store user-owned copies of media items with optional A/V metadata."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    item = models.ForeignKey(Item, on_delete=models.CASCADE)

    # Timestamps
    collected_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the item was added to collection",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="When the collection entry was last updated",
    )

    # Media source/format metadata
    media_type = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Physical/digital source: bluray, dvd, digital, etc.",
    )

    # Video metadata
    resolution = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Resolution: 720p, 1080p, 4k, etc.",
    )
    hdr = models.CharField(
        max_length=30,
        blank=True,
        default="",
        help_text="HDR format: HDR10, Dolby Vision, etc.",
    )
    is_3d = models.BooleanField(
        default=False,
        help_text="Whether the media is 3D",
    )

    # Audio metadata
    audio_codec = models.CharField(
        max_length=30,
        blank=True,
        default="",
        help_text="Audio codec: AAC, DTS, TrueHD, Atmos, etc.",
    )
    audio_channels = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Audio channels: 2.0, 5.1, 7.1.2, etc.",
    )
    bitrate = models.IntegerField(
        null=True,
        blank=True,
        help_text="Audio bitrate in kbps (e.g., 128, 320, 1411)",
    )

    # Plex rating key cache (for faster bulk imports)
    plex_rating_key = models.CharField(
        max_length=50,
        null=True,
        blank=True,
        db_index=True,
        help_text="Cached Plex rating key for this item (populated from webhook events)",
    )
    plex_uri = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Cached Plex server URI for this item",
    )
    plex_rating_key_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the Plex rating key was last updated",
    )

    class Meta:
        ordering = ["-collected_at"]
        indexes = [
            models.Index(fields=["user", "-collected_at"]),
            models.Index(fields=["user", "item"]),
            models.Index(fields=["user", "plex_rating_key"]),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.item.title}"


class Tag(models.Model):
    """User-defined tag for organizing media items."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tags",
    )
    name = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            UniqueConstraint(
                models.functions.Lower("name"),
                "user",
                name="app_tag_unique_user_name_ci",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "name"]),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name = " ".join(self.name.split())
        super().save(*args, **kwargs)


class ItemTag(models.Model):
    """Join table linking tags to items for a user."""

    tag = models.ForeignKey(
        Tag,
        on_delete=models.CASCADE,
        related_name="item_tags",
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="item_tags",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            UniqueConstraint(
                fields=["tag", "item"],
                name="app_itemtag_unique_tag_item",
            ),
        ]
        indexes = [
            models.Index(fields=["item", "tag"]),
        ]

    def __str__(self):
        return f"{self.tag.name} -> {self.item.title}"


class DiscoverFeedbackType(models.TextChoices):
    """Choices for hidden Discover feedback on an item."""

    NOT_INTERESTED = "not_interested", "Not interested"


class DiscoverFeedback(models.Model):
    """Hidden per-item Discover feedback used for recommendation suppression."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="discover_feedback",
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="discover_feedback",
    )
    feedback_type = models.CharField(
        max_length=32,
        choices=DiscoverFeedbackType,
        default=DiscoverFeedbackType.NOT_INTERESTED,
    )
    source_context = models.CharField(max_length=32, default="discover")
    row_key = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user_id", "feedback_type", "-updated_at"]
        constraints = [
            UniqueConstraint(
                fields=["user", "item", "feedback_type"],
                name="discover_feedback_unique_user_item_feedback_type",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "feedback_type", "updated_at"]),
            models.Index(fields=["item"]),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.item_id}:{self.feedback_type}"


class DiscoverApiCache(models.Model):
    """DB-backed cache for external Discover endpoint payloads."""

    provider = models.CharField(max_length=32)
    endpoint = models.CharField(max_length=255)
    params_hash = models.CharField(max_length=64)
    payload = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-fetched_at"]
        constraints = [
            UniqueConstraint(
                fields=["provider", "endpoint", "params_hash"],
                name="discover_api_cache_unique_endpoint_params",
            ),
        ]
        indexes = [
            models.Index(fields=["provider", "endpoint"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"{self.provider}:{self.endpoint}"


class DiscoverTasteProfile(models.Model):
    """Persisted Discover taste profile vectors per user and media type."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="discover_taste_profiles",
    )
    media_type = models.CharField(max_length=20, default="all")
    genre_affinity = models.JSONField(default=dict, blank=True)
    recent_genre_affinity = models.JSONField(default=dict, blank=True)
    phase_genre_affinity = models.JSONField(default=dict, blank=True)
    tag_affinity = models.JSONField(default=dict, blank=True)
    recent_tag_affinity = models.JSONField(default=dict, blank=True)
    phase_tag_affinity = models.JSONField(default=dict, blank=True)
    keyword_affinity = models.JSONField(default=dict, blank=True)
    recent_keyword_affinity = models.JSONField(default=dict, blank=True)
    phase_keyword_affinity = models.JSONField(default=dict, blank=True)
    studio_affinity = models.JSONField(default=dict, blank=True)
    recent_studio_affinity = models.JSONField(default=dict, blank=True)
    phase_studio_affinity = models.JSONField(default=dict, blank=True)
    collection_affinity = models.JSONField(default=dict, blank=True)
    recent_collection_affinity = models.JSONField(default=dict, blank=True)
    phase_collection_affinity = models.JSONField(default=dict, blank=True)
    director_affinity = models.JSONField(default=dict, blank=True)
    recent_director_affinity = models.JSONField(default=dict, blank=True)
    phase_director_affinity = models.JSONField(default=dict, blank=True)
    lead_cast_affinity = models.JSONField(default=dict, blank=True)
    recent_lead_cast_affinity = models.JSONField(default=dict, blank=True)
    phase_lead_cast_affinity = models.JSONField(default=dict, blank=True)
    certification_affinity = models.JSONField(default=dict, blank=True)
    recent_certification_affinity = models.JSONField(default=dict, blank=True)
    phase_certification_affinity = models.JSONField(default=dict, blank=True)
    runtime_bucket_affinity = models.JSONField(default=dict, blank=True)
    recent_runtime_bucket_affinity = models.JSONField(default=dict, blank=True)
    phase_runtime_bucket_affinity = models.JSONField(default=dict, blank=True)
    decade_affinity = models.JSONField(default=dict, blank=True)
    recent_decade_affinity = models.JSONField(default=dict, blank=True)
    phase_decade_affinity = models.JSONField(default=dict, blank=True)
    person_affinity = models.JSONField(default=dict, blank=True)
    negative_genre_affinity = models.JSONField(default=dict, blank=True)
    negative_tag_affinity = models.JSONField(default=dict, blank=True)
    negative_person_affinity = models.JSONField(default=dict, blank=True)
    activity_snapshot_at = models.DateTimeField(null=True, blank=True)
    computed_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["user_id", "media_type"]
        constraints = [
            UniqueConstraint(
                fields=["user", "media_type"],
                name="discover_taste_profile_unique_user_media_type",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "media_type"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.media_type}"


class DiscoverRowCache(models.Model):
    """DB-backed row cache for Discover page rendering."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="discover_row_caches",
    )
    media_type = models.CharField(max_length=20, default="all")
    row_key = models.CharField(max_length=100)
    payload = models.JSONField(default=dict, blank=True)
    built_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["user_id", "media_type", "row_key"]
        constraints = [
            UniqueConstraint(
                fields=["user", "media_type", "row_key"],
                name="discover_row_cache_unique_user_media_row",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "media_type"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.media_type}:{self.row_key}"
