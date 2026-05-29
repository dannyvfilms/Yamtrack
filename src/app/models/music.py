from django.conf import settings
from django.core.validators import DecimalValidator, MaxValueValidator, MinValueValidator
from django.db import models
from model_utils import FieldTracker

# Media and Status are imported from the parent package. This works because
# __init__.py defines them before it executes `from app.models.music import ...`,
# so the partial module object already contains both names when this file loads.
from app.models import Media, Status


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
