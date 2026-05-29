from django.apps import apps
from django.conf import settings
from django.core.validators import DecimalValidator, MaxValueValidator, MinValueValidator
from django.db import models
from model_utils import FieldTracker

from app.models import Media, Status


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
        max_length=500,
        help_text="Pocket Casts episode UUID or RSS GUID",
    )
    title = models.CharField(max_length=500)
    slug = models.CharField(max_length=255, blank=True, default="")
    published = models.DateTimeField(null=True, blank=True)
    duration = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Duration in seconds",
    )
    audio_url = models.URLField(max_length=500, blank=True, default="")
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
        unique_together = [("show", "episode_uuid")]

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
        HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")

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
        is_in_progress = (
            self.status == Status.IN_PROGRESS.value or
            self.last_seen_status == 2  # 2 = in-progress from API
        )

        if is_in_progress and self.played_up_to_seconds and self.played_up_to_seconds > 0:
            minutes = self.played_up_to_seconds // 60
            return f"{minutes}m"

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
