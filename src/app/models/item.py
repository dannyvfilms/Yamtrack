import ast
import logging
import unicodedata
import uuid
from collections import defaultdict

from django.conf import settings
from django.db import models
from django.db.models import CheckConstraint, Q, UniqueConstraint
from django.utils import translation
from unidecode import unidecode

import app
import events
from app import providers
from app.mixins import CalendarTriggerMixin
from app.models.choices import MediaTypes, Sources

logger = logging.getLogger(__name__)


class Item(CalendarTriggerMixin, models.Model):
    """Model to store basic information about media items."""

    media_id = models.CharField(max_length=500)
    source = models.CharField(
        max_length=20,
        choices=Sources,
    )
    media_type = models.CharField(
        max_length=10,
        choices=MediaTypes,
        default=MediaTypes.MOVIE.value,
    )
    library_media_type = models.CharField(
        max_length=10,
        choices=MediaTypes,
        blank=True,
        default="",
        help_text="Library bucket for this item (e.g. grouped anime stored on TV rows).",
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
    implied_genres = models.JSONField(default=list, blank=True)
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
    trakt_rating = models.FloatField(
        null=True,
        blank=True,
        help_text="Average rating value from Trakt metadata",
    )
    trakt_rating_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Rating count from Trakt metadata",
    )
    imdb_rating = models.FloatField(
        null=True,
        blank=True,
        help_text="Average rating value from IMDB",
    )
    imdb_rating_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Rating count from IMDB",
    )
    trakt_popularity_score = models.FloatField(
        null=True,
        blank=True,
        help_text="Derived Trakt popularity score computed from rating and votes",
    )
    trakt_popularity_rank = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Estimated Trakt popularity rank derived from the local score model",
    )
    trakt_popularity_fetched_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When Trakt popularity metadata was last fetched",
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
    provider_external_ids = models.JSONField(default=dict, blank=True, help_text="Resolved external ids")
    provider_game_lengths = models.JSONField(
        default=dict,
        blank=True,
        help_text="Persisted game length metadata from external providers",
    )
    provider_game_lengths_source = models.CharField(
        max_length=10,
        blank=True,
        default="",
        choices=(
            ("", ""),
            ("hltb", "HowLongToBeat"),
            ("igdb", "IGDB"),
        ),
        help_text="Active provider for persisted game length metadata",
    )
    provider_game_lengths_match = models.CharField(
        max_length=32,
        blank=True,
        default="",
        choices=(
            ("", ""),
            ("direct_url", "Direct URL"),
            ("exact_title_year", "Exact Title + Year"),
            ("steam_verified", "Steam Verified"),
            ("ambiguous", "Ambiguous"),
            ("igdb_fallback", "IGDB Fallback"),
        ),
        help_text="How the active game length metadata was matched",
    )
    provider_game_lengths_fetched_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When game length metadata was last fetched",
    )
    metadata_fetched_at = models.DateTimeField(null=True, blank=True, help_text="When metadata was last fetched")
    manual_metadata = models.JSONField(
        blank=True,
        default=dict,
        help_text="Structured metadata overrides for manual/custom entries",
    )
    provider_metadata_status = models.CharField(
        blank=True,
        default="",
        max_length=64,
        choices=[("local_only_missing_season", "Local only: missing season metadata")],
        help_text="Flags special provider metadata states for UI and recovery flows",
    )
    series_name = models.TextField(null=True, blank=True)
    series_position = models.FloatField(null=True, blank=True)

    class Meta:
        """Meta options for the model."""

        constraints = [
            # Ensures items without season/episode numbers are unique per library type
            UniqueConstraint(
                fields=["media_id", "source", "media_type", "library_media_type"],
                condition=Q(season_number__isnull=True, episode_number__isnull=True),
                name="unique_item_without_season_episode",
            ),
            # Ensures seasons are unique within a show per library type
            UniqueConstraint(
                fields=["media_id", "source", "media_type", "library_media_type", "season_number"],
                condition=Q(season_number__isnull=False, episode_number__isnull=True),
                name="unique_item_with_season",
            ),
            # Ensures episodes are unique within a season per library type
            UniqueConstraint(
                fields=[
                    "media_id",
                    "source",
                    "media_type",
                    "library_media_type",
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
            CheckConstraint(
                condition=Q(library_media_type="") | Q(library_media_type__in=MediaTypes.values),
                name="%(app_label)s_%(class)s_library_media_type_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["metadata_fetched_at"],
                name="app_item_metadata_fetched_idx",
            ),
            models.Index(
                fields=["release_datetime"],
                name="app_item_release_dt_idx",
            ),
            models.Index(
                fields=["trakt_popularity_rank"],
                name="app_item_trakt_pop_rank_idx",
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

    @classmethod
    def _normalize_title_value(cls, value):
        """Normalize title values to non-empty strings or None."""
        if value is None:
            return None
        if isinstance(value, dict):
            for key in (
                "localized_title",
                "original_title",
                "title",
                "name",
                "value",
                "text",
                "label",
            ):
                normalized = cls._normalize_title_value(value.get(key))
                if normalized:
                    return normalized
            return None
        if isinstance(value, (list, tuple)):
            for entry in value:
                normalized = cls._normalize_title_value(entry)
                if normalized:
                    return normalized
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text[0] in "[{" and text[-1] in "]}":
                try:
                    parsed = ast.literal_eval(text)
                except (SyntaxError, ValueError):
                    return text
                normalized = cls._normalize_title_value(parsed)
                if normalized:
                    return normalized
            return text
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

    @classmethod
    def title_fields_from_episode_metadata(cls, metadata, fallback_title=""):
        """Build item title fields for an episode payload."""
        metadata = metadata or {}
        episode_title = cls._normalize_title_value(
            metadata.get("episode_title") or metadata.get("name") or metadata.get("title"),
        )
        if episode_title:
            return cls.title_fields_from_metadata(
                {"title": episode_title},
                fallback_title=episode_title,
            )
        return cls.title_fields_from_metadata({}, fallback_title=fallback_title)

    def get_display_and_alternative_title(self, user=None):
        """Return display and alternate titles based on user preference."""
        preference = getattr(user, "title_display_preference", "localized")
        return self.resolve_title_preference(preference)

    @classmethod
    def _title_comparison_key(cls, value):
        """Return a normalized key for comparing title variants."""
        normalized = cls._normalize_title_value(value)
        if not normalized:
            return ""

        ascii_text = unidecode(normalized)
        return "".join(char for char in ascii_text.casefold() if char.isalnum())

    @classmethod
    def _title_script_bucket(cls, value):
        """Return a coarse script bucket for a title."""
        normalized = cls._normalize_title_value(value) or ""
        bucket_counts = defaultdict(int)

        for char in normalized:
            if not char.isalpha():
                continue

            name = unicodedata.name(char, "")
            if "LATIN" in name:
                bucket = "latin"
            elif any(
                token in name
                for token in ("HIRAGANA", "KATAKANA", "CJK", "IDEOGRAPH", "HAN")
            ):
                bucket = "cjk"
            elif "HANGUL" in name:
                bucket = "hangul"
            elif "CYRILLIC" in name:
                bucket = "cyrillic"
            elif "GREEK" in name:
                bucket = "greek"
            elif "ARABIC" in name:
                bucket = "arabic"
            elif "HEBREW" in name:
                bucket = "hebrew"
            else:
                bucket = "other"
            bucket_counts[bucket] += 1

        if not bucket_counts:
            return "unknown"

        return max(bucket_counts.items(), key=lambda row: row[1])[0]

    @staticmethod
    def _preferred_locale_scripts(active_language=None):
        """Return likely title scripts for the active UI language."""
        language = str(active_language or translation.get_language() or "")
        base_language = language.split("-", 1)[0].split("_", 1)[0].lower()

        script_map = {
            "ja": {"cjk"},
            "zh": {"cjk"},
            "ko": {"hangul", "cjk"},
            "ru": {"cyrillic"},
            "uk": {"cyrillic"},
            "bg": {"cyrillic"},
            "be": {"cyrillic"},
            "mk": {"cyrillic"},
            "el": {"greek"},
            "ar": {"arabic"},
            "fa": {"arabic"},
            "ur": {"arabic"},
            "he": {"hebrew"},
            "yi": {"hebrew"},
        }
        return script_map.get(base_language, {"latin"})

    @classmethod
    def _should_show_alternative_title(
        cls,
        display_title,
        alternative_title,
        *,
        preference="localized",
        active_language=None,
    ):
        """Return whether an alternate title is useful enough to display."""
        if not display_title or not alternative_title:
            return False

        if cls._title_comparison_key(display_title) == cls._title_comparison_key(
            alternative_title,
        ):
            return False

        preference = (preference or "localized").lower()
        if preference == "original":
            return True

        locale_scripts = cls._preferred_locale_scripts(active_language)
        display_script = cls._title_script_bucket(display_title)
        alternative_script = cls._title_script_bucket(alternative_title)

        if (
            display_script in locale_scripts
            and alternative_script not in locale_scripts
            and alternative_script != "unknown"
        ):
            return False

        return True

    @classmethod
    def resolve_title_variants(
        cls,
        *,
        title=None,
        original_title=None,
        localized_title=None,
        preference="localized",
        active_language=None,
    ):
        """Resolve display and alternate titles from raw title fields."""
        preference = (preference or "localized").lower()
        original_title = cls._normalize_title_value(original_title)
        localized_title = (
            cls._normalize_title_value(localized_title)
            or cls._normalize_title_value(title)
        )
        fallback_title = (
            cls._normalize_title_value(title)
            or localized_title
            or original_title
            or ""
        )

        if preference == "original":
            display_title = original_title or localized_title or fallback_title
            alternative_title = (
                localized_title
                if localized_title and localized_title != display_title
                else None
            )
        else:
            display_title = localized_title or original_title or fallback_title
            alternative_title = (
                original_title
                if original_title and original_title != display_title
                else None
            )

        if not cls._should_show_alternative_title(
            display_title,
            alternative_title,
            preference=preference,
            active_language=active_language,
        ):
            alternative_title = None

        return display_title, alternative_title

    def resolve_title_preference(self, preference):
        """Resolve display and alternative titles for a preference value."""
        return self.resolve_title_variants(
            title=self.title,
            original_title=self.original_title,
            localized_title=self.localized_title,
            preference=preference,
        )

    def get_display_title(self, user=None):
        """Return the preferred title to render for this item."""
        display_title, _ = self.get_display_and_alternative_title(user=user)
        return display_title

    @staticmethod
    def _coerce_positive_int(value):
        """Return a positive integer or None."""
        try:
            coerced = int(value or 0)
        except (TypeError, ValueError):
            return None
        return coerced if coerced > 0 else None

    def _game_time_to_beat_minutes_for_source(self, source):
        """Return the persisted time-to-beat value in minutes for a source."""
        payload = self.provider_game_lengths or {}
        if source == "hltb":
            summary = ((payload.get("hltb") or {}).get("summary") or {})
            return self._coerce_positive_int(summary.get("all_styles_minutes"))

        if source == "igdb":
            summary = ((payload.get("igdb") or {}).get("summary") or {})
            seconds = self._coerce_positive_int(summary.get("normally_seconds"))
            return round(seconds / 60) if seconds else None

        return None

    @property
    def game_time_to_beat_minutes(self):
        """Return the best available persisted time-to-beat value in minutes."""
        if self.media_type != MediaTypes.GAME.value:
            return None

        payload = self.provider_game_lengths or {}
        active_source = self.provider_game_lengths_source or payload.get("active_source") or ""
        sources = []
        if active_source in {"hltb", "igdb"}:
            sources.append(active_source)
        for fallback_source in ("hltb", "igdb"):
            if fallback_source not in sources:
                sources.append(fallback_source)

        for source in sources:
            minutes = self._game_time_to_beat_minutes_for_source(source)
            if minutes:
                return minutes
        return None

    @property
    def formatted_game_time_to_beat(self):
        """Return a display string for the persisted time-to-beat value."""
        minutes = self.game_time_to_beat_minutes
        return app.helpers.minutes_to_hhmm(minutes) if minutes else "--"

    def get_alternative_title(self, user=None):
        """Return the opposite title variant for tooltip display."""
        _, alternative_title = self.get_display_and_alternative_title(user=user)
        return alternative_title

    @classmethod
    def generate_manual_id(cls):
        """Generate a unique ID for manual items."""
        return str(uuid.uuid4())

    def save(self, *args, **kwargs):
        """Save the item, ensuring JSONField arrays are never None."""
        if not self.library_media_type:
            self.library_media_type = self.media_type

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

        json_object_fields = [
            "manual_metadata",
            "provider_external_ids",
            "provider_game_lengths",
        ]
        for field_name in json_object_fields:
            value = getattr(self, field_name, None)
            if value is None:
                setattr(self, field_name, {})

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

                    runtime_minutes = parse_runtime_to_minutes(
                        tv_metadata["details"]["runtime"],
                    )

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
        item_ids_to_process = [item.id for item in items_to_process]

        if delay:
            events.tasks.reload_calendar.apply_async(
                kwargs={"item_ids": item_ids_to_process},
                countdown=3,
            )
        else:
            events.tasks.reload_calendar(item_ids=item_ids_to_process)
