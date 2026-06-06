import logging

from django.conf import settings
from django.core.validators import (
    DecimalValidator,
    MaxValueValidator,
    MinValueValidator,
)
from django.db import models
from django.db.models import Max
from django.utils import timezone
from model_utils import FieldTracker
from requests import RequestException
from simple_history.models import HistoricalRecords
from simple_history.utils import bulk_create_with_history, bulk_update_with_history

import events
from app import cache_utils, providers
from app.models.choices import MediaTypes, Sources, Status
from app.models.item import Item
from app.models.media import Media

logger = logging.getLogger(__name__)


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
        """Return the total episodes watched for the TV show, excluding dropped seasons."""
        return sum(
            season.progress
            for season in self.seasons.all()
            if season.item.season_number != 0
            and season.status != Status.DROPPED.value
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
        """Return the date when the last attached episode was watched."""
        dates = self._season_activity_dates("progressed_at", include_specials=True)
        return max(dates) if dates else None

    @property
    def start_date(self):
        """Return the first watched date, preferring main seasons over specials."""
        dates = self._season_activity_dates("start_date")
        if dates:
            return min(dates)
        special_dates = self._season_activity_dates("start_date", include_specials=True)
        if special_dates:
            return min(special_dates)
        if self.status == Status.IN_PROGRESS.value:
            return self.created_at
        return None

    @property
    def end_date(self):
        """Return the last watched date across main seasons and specials."""
        dates = self._season_activity_dates("end_date", include_specials=True)
        return max(dates) if dates else None

    def _get_quick_update_season(self, operation):
        """Return the season that should handle quick TV progress updates."""
        seasons = sorted(
            (
                season
                for season in self.seasons.all()
                if season.item.season_number != 0
            ),
            key=lambda season: season.item.season_number,
        )

        for season in seasons:
            if season.status == Status.IN_PROGRESS.value:
                return season

        if operation == "increase" and self._start_next_available_season():
            return (
                self.seasons.filter(
                    item__season_number__gt=0,
                    status=Status.IN_PROGRESS.value,
                )
                .order_by("item__season_number")
                .first()
            )

        if operation == "decrease":
            for season in reversed(seasons):
                if season.progress > 0:
                    return season

        return None

    def increase_progress(self):
        """Increase TV progress by advancing the active season."""
        season = self._get_quick_update_season("increase")
        if season is None:
            logger.info("No season available to increase progress for %s", self)
            return
        season.increase_progress()

    def decrease_progress(self):
        """Decrease TV progress by rewinding the relevant season."""
        season = self._get_quick_update_season("decrease")
        if season is None:
            logger.info("No season available to decrease progress for %s", self)
            return
        season.decrease_progress()

    def _season_activity_dates(self, attr_name, include_specials=False):
        """Collect season activity dates, optionally including specials."""
        dates = []
        for season in self.seasons.all():
            season_number = getattr(season.item, "season_number", None)
            if not include_specials and season_number == 0:
                continue

            date_value = getattr(season, attr_name, None)
            if date_value is not None:
                dates.append(date_value)

        return dates

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
                library_media_type=self.item.library_media_type,
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

    def _start_next_available_season(
        self,
        min_season_number=0,
    ):
        """Find the next available season to watch and set it to in-progress."""
        min_season_number = int(min_season_number or 0)

        all_seasons = self.seasons.filter(
            item__season_number__gt=min_season_number,
        ).order_by("item__season_number")

        next_unwatched_season = all_seasons.exclude(
            status__in=[Status.COMPLETED.value],
        ).first()

        season_started = False

        if not next_unwatched_season:
            # If all existing seasons are watched, get the next available season
            tv_metadata = providers.services.get_media_metadata(
                self.item.media_type,
                self.item.media_id,
                self.item.source,
            )
            related_seasons = tv_metadata.get("related", {}).get("seasons", [])

            existing_season_numbers = set(
                all_seasons.values_list("item__season_number", flat=True),
            )

            for season_data in related_seasons:
                season_number = season_data["season_number"]
                if (
                    season_number > min_season_number
                    and season_number not in existing_season_numbers
                ):
                    # Use season poster if available, otherwise fallback to TV show poster
                    season_image = season_data.get("image") or self.item.image

                    item, _ = Item.objects.get_or_create(
                        media_id=self.item.media_id,
                        source=self.item.source,
                        media_type=MediaTypes.SEASON.value,
                        library_media_type=self.item.library_media_type,
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
                    season_started = True
                    break

        elif next_unwatched_season.status != Status.IN_PROGRESS.value:
            next_unwatched_season.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [next_unwatched_season],
                Season,
                fields=["status"],
            )
            season_started = True
        else:
            season_started = True

        if season_started and self.status != Status.IN_PROGRESS.value:
            self.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self],
                TV,
                fields=["status"],
            )

        return season_started

    def _handle_completed_season(
        self,
        completed_season_number,
    ):
        """Start the next season, or complete the TV show if no seasons remain."""
        if self._start_next_available_season(
            completed_season_number,
        ):
            return

        incomplete_seasons_exist = (
            self.seasons.filter(
                item__season_number__gt=0,
            )
            .exclude(
                status=Status.COMPLETED.value,
            )
            .exists()
        )

        if not incomplete_seasons_exist and self.status != Status.COMPLETED.value:
            self.status = Status.COMPLETED.value
            bulk_update_with_history(
                [self],
                TV,
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

                self.related_tv._handle_completed_season(
                    self.item.season_number,
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

    def derived_status_from_episode_progress(self, max_progress=None):
        """Return the effective season status from local episode history."""
        if self.status in {Status.DROPPED.value, Status.PAUSED.value}:
            return self.status

        max_progress_value = max_progress
        if max_progress_value is None:
            max_progress_value = getattr(self, "max_progress", None)
        try:
            max_progress_value = int(max_progress_value)
        except (TypeError, ValueError):
            max_progress_value = None
        if max_progress_value is not None and max_progress_value <= 0:
            max_progress_value = None

        completed_episode_count = self.completed_episode_count
        progress_value = self.progress

        if (
            max_progress_value is not None
            and completed_episode_count >= max_progress_value
        ):
            return Status.COMPLETED.value
        if progress_value > 0 or completed_episode_count > 0:
            return Status.IN_PROGRESS.value
        if self.status == Status.PLANNING.value:
            return Status.PLANNING.value
        return self.status

    def promote_to_completed_if_fully_watched(self, max_progress=None):
        """Persist a completed season when local episode history proves it."""
        desired_status = self.derived_status_from_episode_progress(
            max_progress=max_progress,
        )
        if (
            desired_status != Status.COMPLETED.value
            or self.status == Status.COMPLETED.value
        ):
            return False

        self.status = Status.COMPLETED.value
        bulk_update_with_history([self], Season, fields=["status"])
        self.related_tv._handle_completed_season(self.item.season_number)
        return True

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
        if hasattr(self, "_episode_stats_cache"):
            delattr(self, "_episode_stats_cache")
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
                    "library_media_type": self.item.library_media_type,
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

        from app import helpers

        image = settings.IMG_NONE
        runtime_minutes = None
        release_datetime = None
        matched_episode = {}
        tvdb_episode_images = {}
        normalized_episode_number = int(episode_number)

        if self.item.source == Sources.TMDB.value:
            if isinstance(season_metadata, dict):
                tvdb_episode_images = season_metadata.get("_tvdb_episode_image_map")
                if tvdb_episode_images is None:
                    tvdb_episode_images = providers.tmdb.get_tvdb_episode_image_map(
                        season_metadata.get("tvdb_id"),
                        season_metadata.get("season_number") or self.item.season_number,
                        tmdb_media_id=self.item.media_id,
                    )
                    season_metadata["_tvdb_episode_image_map"] = tvdb_episode_images
            else:
                tvdb_episode_images = providers.tmdb.get_tvdb_episode_image_map(
                    season_metadata.get("tvdb_id"),
                    season_metadata.get("season_number") or self.item.season_number,
                    tmdb_media_id=self.item.media_id,
                )

        if isinstance(season_metadata, dict):
            episodes_by_number = season_metadata.get("_episodes_by_number")
            if episodes_by_number is None:
                episodes_by_number = {
                    episode.get("episode_number"): episode
                    for episode in season_metadata.get("episodes") or []
                    if isinstance(episode, dict)
                    and episode.get("episode_number") is not None
                }
                season_metadata["_episodes_by_number"] = episodes_by_number
            matched_episode = episodes_by_number.get(normalized_episode_number, {})
        else:
            for episode in season_metadata["episodes"]:
                if episode["episode_number"] == normalized_episode_number:
                    matched_episode = episode
                    break

        if matched_episode:
            image = helpers.first_real_image(
                (
                    f"https://image.tmdb.org/t/p/original{matched_episode['still_path']}"
                    if matched_episode.get("still_path")
                    else None
                ),
                tvdb_episode_images.get(str(episode_number)),
                matched_episode.get("image"),
            )

            # Extract runtime from episode metadata (raw TMDB data has integer runtime in minutes)
            if matched_episode.get("runtime") is not None:
                # Runtime is an integer (minutes) from TMDB
                runtime_minutes = int(matched_episode["runtime"]) if matched_episode["runtime"] > 0 else None

            # Extract release_datetime from episode air_date
            air_date = matched_episode.get("air_date")
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

        item, created = Item.objects.get_or_create(
            media_id=self.item.media_id,
            source=self.item.source,
            media_type=MediaTypes.EPISODE.value,
            library_media_type=self.item.library_media_type,
            season_number=self.item.season_number,
            episode_number=normalized_episode_number,
            defaults={
                **Item.title_fields_from_episode_metadata(
                    matched_episode,
                    fallback_title=self.item.title,
                ),
                "image": image,
                "runtime_minutes": runtime_minutes,
                "release_datetime": release_datetime,
            },
        )

        # Update fields if not set and we have them now
        updated = False
        if not created:
            update_fields = []
            title_fields = Item.title_fields_from_episode_metadata(
                matched_episode,
                fallback_title=self.item.title,
            )
            for field_name, value in title_fields.items():
                if getattr(item, field_name) != value:
                    setattr(item, field_name, value)
                    update_fields.append(field_name)
                    updated = True
            if item.library_media_type != self.item.library_media_type:
                item.library_media_type = self.item.library_media_type
                update_fields.append("library_media_type")
                updated = True
            if not item.runtime_minutes and runtime_minutes:
                item.runtime_minutes = runtime_minutes
                update_fields.append("runtime_minutes")
                updated = True
            if not item.release_datetime and release_datetime:
                item.release_datetime = release_datetime
                update_fields.append("release_datetime")
                updated = True
            if updated:
                item.save(update_fields=update_fields)
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
                item.save(
                    update_fields=[
                        "library_media_type",
                        "runtime_minutes",
                        "release_datetime",
                    ],
                )

        return item


class Episode(models.Model):
    """Model for episodes of a season."""

    history = HistoricalRecords(
        cascade_delete_history=True,
        excluded_fields=["item", "related_season", "created_at", "score"],
    )

    created_at = models.DateTimeField(auto_now_add=True)
    item = models.ForeignKey(Item, on_delete=models.CASCADE, null=True)
    related_season = models.ForeignKey(
        Season,
        on_delete=models.CASCADE,
        related_name="episodes",
    )
    end_date = models.DateTimeField(null=True, blank=True)
    dropped = models.BooleanField(default=False)
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

    @property
    def start_date(self):
        return None

    @property
    def progressed_at(self):
        return None

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
            self.related_season.max_progress = max_progress
        except (
            providers.services.ProviderAPIError,
            RequestException,
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
        if hasattr(self.related_season, "_episode_stats_cache"):
            delattr(self.related_season, "_episode_stats_cache")
        self.related_season.refresh_from_db()

        desired_status = self.related_season.derived_status_from_episode_progress(
            max_progress=max_progress,
        )

        if desired_status != self.related_season.status:
            self.related_season.status = desired_status
            bulk_update_with_history(
                [self.related_season],
                Season,
                fields=["status"],
            )

        if desired_status == Status.COMPLETED.value:
            self.related_season.related_tv._handle_completed_season(season_number)
        elif self.related_season.related_tv.status != Status.IN_PROGRESS.value:
            self.related_season.related_tv.status = Status.IN_PROGRESS.value
            bulk_update_with_history(
                [self.related_season.related_tv],
                TV,
                fields=["status"],
            )
