from django.conf import settings
from django.db import models
from django.db.models import UniqueConstraint

from app.models.item import Item


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
    comfort_library_affinity = models.JSONField(default=dict, blank=True)
    comfort_rewatch_affinity = models.JSONField(default=dict, blank=True)
    person_affinity = models.JSONField(default=dict, blank=True)
    negative_genre_affinity = models.JSONField(default=dict, blank=True)
    negative_tag_affinity = models.JSONField(default=dict, blank=True)
    negative_person_affinity = models.JSONField(default=dict, blank=True)
    world_rating_profile = models.JSONField(default=dict, blank=True)
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
