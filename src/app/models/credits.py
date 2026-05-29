from django.conf import settings
from django.db import models
from django.db.models import UniqueConstraint

from app.models import Item, MediaTypes, Sources

CREDITS_BACKFILL_VERSION = 4
DISCOVER_MOVIE_METADATA_BACKFILL_VERSION = 1
TRAKT_POPULARITY_BACKFILL_VERSION = 1


class ItemProviderLink(models.Model):
    """Cross-provider ID mapping for a tracked item."""

    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="provider_links",
    )
    provider = models.CharField(max_length=20, choices=Sources.choices)
    provider_media_id = models.CharField(max_length=32)
    provider_media_type = models.CharField(max_length=10, choices=MediaTypes.choices)
    season_number = models.PositiveIntegerField(null=True, blank=True)
    episode_offset = models.IntegerField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["provider", "provider_media_type", "provider_media_id", "season_number"]
        constraints = [
            UniqueConstraint(
                fields=["item", "provider", "provider_media_type", "season_number"],
                name="%(app_label)s_%(class)s_unique_item_provider_type",
            ),
            UniqueConstraint(
                fields=["provider", "provider_media_type", "provider_media_id", "season_number"],
                name="%(app_label)s_%(class)s_unique_provider_lookup",
            ),
        ]
        indexes = [
            models.Index(fields=["provider", "provider_media_type", "provider_media_id"]),
            models.Index(fields=["item", "provider"]),
        ]

    def __str__(self):
        """Return a readable mapping label."""
        season_suffix = f" S{self.season_number}" if self.season_number is not None else ""
        return (
            f"{self.item_id}:{self.provider}/{self.provider_media_type}/"
            f"{self.provider_media_id}{season_suffix}"
        )


class MetadataProviderPreference(models.Model):
    """Per-user display-provider override for a tracked item."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="metadata_provider_preferences",
    )
    item = models.ForeignKey(
        Item,
        on_delete=models.CASCADE,
        related_name="metadata_provider_preferences",
    )
    provider = models.CharField(max_length=20, choices=Sources.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["user", "item"],
                name="%(app_label)s_%(class)s_unique_user_item",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "provider"]),
            models.Index(fields=["item", "provider"]),
        ]

    def __str__(self):
        """Return the display-provider preference label."""
        return f"{self.user_id}:{self.item_id}->{self.provider}"


class MetadataBackfillField(models.TextChoices):
    """Fields that can be backfilled from external metadata."""

    RUNTIME = "runtime", "Runtime"
    GENRES = "genres", "Genres"
    CREDITS = "credits", "Credits"
    RELEASE = "release", "Release Date"
    DISCOVER = "discover", "Discover Metadata"
    GAME_LENGTHS = "game_lengths", "Game Lengths"
    TRAKT_POPULARITY = "trakt_popularity", "Trakt Popularity"


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
