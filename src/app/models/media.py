import logging

from django.apps import apps
from django.conf import settings
from django.core.validators import (
    DecimalValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.utils import timezone
from model_utils import FieldTracker
from model_utils.fields import MonitorField
from requests import RequestException
from simple_history.models import HistoricalRecords

import app
from app import providers
from app.models.choices import MediaTypes, Status
from app.models.item import Item
from app.models.manager import MediaManager

logger = logging.getLogger(__name__)


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
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self):
        """Return the title of the media."""
        return self.item.__str__()

    def save(self, *args, **kwargs):
        """Save the media instance."""
        if not getattr(self, "_history_user", None) and getattr(self, "user_id", None):
            self._history_user = self.user

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
            # Music and board games are play-count based; podcasts use local runtime data.
            if self.item.media_type in (
                MediaTypes.PODCAST.value,
                MediaTypes.MUSIC.value,
                MediaTypes.BOARDGAME.value,
            ):
                max_progress = self._get_local_max_progress()
            else:
                try:
                    max_progress = providers.services.get_media_metadata(
                        self.item.media_type,
                        self.item.media_id,
                        self.item.source,
                    )["max_progress"]
                except (providers.services.ProviderAPIError, RequestException, ValueError):
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
            # Music and board game progress are play-count based; don't overwrite on status changes.
            if self.item.media_type in (MediaTypes.MUSIC.value, MediaTypes.BOARDGAME.value):
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
                except (providers.services.ProviderAPIError, RequestException, ValueError):
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

    def _get_known_item_runtime_minutes(self):
        """Return a persisted runtime value without falling back to estimates."""
        runtime_minutes = getattr(self.item, "runtime_minutes", None)
        if runtime_minutes and runtime_minutes < 999998:
            return runtime_minutes

        runtime_display = getattr(self.item, "runtime", "")
        if runtime_display:
            from app.statistics import parse_runtime_to_minutes

            parsed_runtime = parse_runtime_to_minutes(runtime_display)
            if parsed_runtime and parsed_runtime < 999998:
                return parsed_runtime

        return None

    def _plays_sort_value(self):
        """Return the aggregated play/progress count used by plays-based UI."""
        aggregated_progress = getattr(self, "aggregated_progress", None)
        if aggregated_progress is not None:
            return aggregated_progress
        return self.progress or 0

    def _episode_runtime_entries(self):
        """Return {season_number: [(episode_number, runtime), ...]} for this show.

        Uses the index prefilled by prefill_episode_runtime_index when present
        (bulk pages); otherwise fetches the whole show in one query and
        memoizes it, so detail pages issue one query instead of one per season.
        """
        index = getattr(self, "_episode_runtime_index", None)
        if index is None:
            from app.models.episode_runtimes import build_episode_runtime_index

            key = (self.item.media_id, self.item.source)
            index = build_episode_runtime_index({key}).get(key, {})
            self._episode_runtime_index = index
        return index

    def _calc_total_runtime_from_items(self, total_episodes):
        """Estimate full released runtime from stored episode runtimes when possible."""
        if not total_episodes or total_episodes <= 0:
            return None

        if self.item.media_type == MediaTypes.TV.value:
            breakdown = getattr(self, "released_episode_breakdown", None) or {}
            if not breakdown:
                return None

            season_episodes = self._episode_runtime_entries()
            total_runtime = 0
            episodes_with_data = 0
            for season_num in sorted(breakdown.keys()):
                released_episode_count = breakdown[season_num]
                season_runtimes = [
                    runtime
                    for episode_number, runtime in season_episodes.get(season_num, ())
                    if episode_number is not None
                    and episode_number <= released_episode_count
                ]
                if season_runtimes:
                    total_runtime += sum(season_runtimes)
                    episodes_with_data += len(season_runtimes)

            if episodes_with_data > 0:
                if episodes_with_data == total_episodes:
                    return total_runtime
                missing_eps = total_episodes - episodes_with_data
                avg_runtime = total_runtime / episodes_with_data
                return total_runtime + int(missing_eps * avg_runtime)
            return None

        if self.item.media_type != MediaTypes.ANIME.value:
            return None

        episode_runtimes = [
            runtime
            for season_entries in self._episode_runtime_entries().values()
            for episode_number, runtime in season_entries
            if episode_number is not None and episode_number <= total_episodes
        ]
        if not episode_runtimes:
            return None
        if len(episode_runtimes) == total_episodes:
            return sum(episode_runtimes)
        avg_runtime = sum(episode_runtimes) / len(episode_runtimes)
        return sum(episode_runtimes) + int((total_episodes - len(episode_runtimes)) * avg_runtime)

    @property
    def total_runtime_minutes(self):
        """Return total title runtime in minutes for supported media types."""
        cached_total = getattr(self, "_total_runtime_minutes_cache", None)
        if cached_total is not None:
            return cached_total

        total_runtime = None
        media_type = getattr(self.item, "media_type", None)

        if media_type == MediaTypes.MOVIE.value:
            total_runtime = self._get_known_item_runtime_minutes()
        elif media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            total_episodes = getattr(self, "max_progress", None)
            if total_episodes and total_episodes > 0:
                total_runtime = self._calc_total_runtime_from_items(total_episodes)
                if total_runtime is None:
                    average_runtime = self._get_known_item_runtime_minutes()
                    if average_runtime is None:
                        average_runtime = self._get_fallback_runtime_minutes()
                    if average_runtime and average_runtime < 999999:
                        total_runtime = total_episodes * average_runtime

        self._total_runtime_minutes_cache = total_runtime or 0
        return total_runtime

    @property
    def formatted_total_runtime(self):
        """Return the total runtime in a readable display format."""
        total_runtime = self.total_runtime_minutes
        return app.helpers.minutes_to_hhmm(total_runtime) if total_runtime else "--"

    @property
    def average_runtime_minutes(self):
        """Return the best runtime estimate for a single play/watch."""
        runtime_minutes = self._get_known_item_runtime_minutes()
        if runtime_minutes:
            return runtime_minutes

        total_runtime = self.total_runtime_minutes
        if not total_runtime:
            return None

        max_progress = getattr(self, "max_progress", None)
        if max_progress and max_progress > 0:
            return max(1, round(total_runtime / max_progress))

        if getattr(self.item, "media_type", None) == MediaTypes.MOVIE.value:
            return total_runtime

        return None

    @property
    def time_watched_minutes(self):
        """Return the estimated total watched time in minutes."""
        plays = self._plays_sort_value()
        if plays <= 0:
            return None

        average_runtime = self.average_runtime_minutes
        if not average_runtime:
            return None

        return plays * average_runtime

    @property
    def formatted_time_watched(self):
        """Return total watched time in a readable display format."""
        total_minutes = self.time_watched_minutes
        return app.helpers.minutes_to_hhmm(total_minutes) if total_minutes else "--"

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
                # Use prefilled index (set by prefill_episode_runtime_index) when
                # available to avoid one DB query per partially-watched season.
                episode_runtime_index = getattr(self, "_episode_runtime_index", None)

                for season_num in sorted(breakdown.keys()):
                    season_episode_count = breakdown[season_num]

                    if remaining_progress >= season_episode_count:
                        remaining_progress -= season_episode_count
                    else:
                        watched_in_season = remaining_progress
                        remaining_progress = 0

                        if episode_runtime_index is not None:
                            runtimes = [
                                rt
                                for ep_num, rt in episode_runtime_index.get(season_num, [])
                                if ep_num > watched_in_season
                            ]
                        else:
                            runtimes = list(
                                Item.objects.filter(
                                    media_id=self.item.media_id,
                                    source=self.item.source,
                                    media_type=MediaTypes.EPISODE.value,
                                    season_number=season_num,
                                    episode_number__gt=watched_in_season,
                                    runtime_minutes__isnull=False,
                                )
                                .exclude(runtime_minutes=999999)
                                .exclude(runtime_minutes=999998)
                                .values_list("runtime_minutes", flat=True)
                            )

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


class Manga(Media):
    """Model for manga."""

    tracker = FieldTracker()


class ActiveAnimeQuerySet(models.QuerySet):
    """Anime rows that have not been migrated into grouped series."""

    def active(self):
        """Return only rows still surfaced in the flat anime library."""
        return self.filter(migrated_to_item__isnull=True)


class ActiveAnimeManager(models.Manager):
    """Default anime manager that hides migrated legacy rows."""

    def get_queryset(self):
        """Return only active flat anime rows."""
        return ActiveAnimeQuerySet(self.model, using=self._db).active()


class Anime(Media):
    """Model for anime."""

    migrated_to_item = models.ForeignKey(
        Item,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="migrated_anime_entries",
    )
    migrated_at = models.DateTimeField(null=True, blank=True)

    tracker = FieldTracker()
    objects = ActiveAnimeManager()
    all_objects = models.Manager()


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

    @property
    def formatted_progress(self):
        """Return progress formatted by book format: time for audiobooks, pages otherwise."""
        if getattr(self, "item", None) and self.item.format == "audiobook":
            return app.helpers.minutes_to_hhmm(self.progress)
        return str(self.progress)


class Comic(Media):
    """Model for comics."""

    tracker = FieldTracker()


class ComicIssue(Media):
    """Model for individual comic issues."""

    tracker = FieldTracker()


