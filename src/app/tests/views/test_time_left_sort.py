from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Prefetch
from django.test import TestCase
from django.utils import timezone
from django.utils.crypto import get_random_string

from app.models import (
    TV,
    BasicMedia,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
)
from app.views import _sort_tv_media_by_time_left


class TVTimeLeftSortTests(TestCase):
    """Regression tests for TV time-left sorting."""

    def setUp(self):
        """Create a user for sort tests."""
        password = get_random_string(16)
        self.user = get_user_model().objects.create_user(
            username="test",
            password=password,
        )
        self.now = timezone.now()

    def _create_tv(self, title, media_id, season_configs):
        tv_item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=title,
            image="http://example.com/show.jpg",
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        for config in season_configs:
            season_number = config["season_number"]
            released_episodes = config["released_episodes"]
            watched_episodes = config.get("watched_episodes", 0)
            runtime_minutes = config.get("runtime_minutes", 30)
            watched_instances = []

            season_item = Item.objects.create(
                media_id=media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title=f"{title} Season {season_number}",
                image="http://example.com/season.jpg",
                season_number=season_number,
            )
            season = Season.objects.create(
                item=season_item,
                user=self.user,
                related_tv=tv,
                status=config["status"],
            )

            for episode_number in range(1, released_episodes + 1):
                episode_item = Item.objects.create(
                    media_id=media_id,
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.EPISODE.value,
                    title=f"{title} S{season_number:02d}E{episode_number:02d}",
                    image="http://example.com/episode.jpg",
                    season_number=season_number,
                    episode_number=episode_number,
                    runtime_minutes=runtime_minutes,
                    release_datetime=self.now - timedelta(days=episode_number),
                )
                if episode_number <= watched_episodes:
                    watched_instances.append(
                        Episode(
                            item=episode_item,
                            related_season=season,
                            end_date=self.now,
                        ),
                    )
            if watched_instances:
                Episode.objects.bulk_create(watched_instances)

        return tv

    def test_time_left_sort_excludes_dropped_season_remaining_episodes_only(self):
        """Ignore dropped-season backlog in time-left sorting for active shows."""
        excluded = self._create_tv(
            "Excluded Seasons Show",
            "tv-excluded",
            [
                {
                    "season_number": 1,
                    "status": Status.DROPPED.value,
                    "released_episodes": 10,
                    "watched_episodes": 5,
                },
                {
                    "season_number": 2,
                    "status": Status.IN_PROGRESS.value,
                    "released_episodes": 10,
                    "watched_episodes": 1,
                },
            ],
        )
        self._create_tv(
            "Comparison Show",
            "tv-comparison",
            [
                {
                    "season_number": 1,
                    "status": Status.IN_PROGRESS.value,
                    "released_episodes": 11,
                    "watched_episodes": 0,
                },
            ],
        )

        media_list = list(
            TV.objects.filter(user=self.user)
            .select_related("item")
            .prefetch_related(
                Prefetch("seasons", queryset=Season.objects.select_related("item")),
                Prefetch(
                    "seasons__episodes",
                    queryset=Episode.objects.select_related("item"),
                ),
            ),
        )
        BasicMedia.objects.annotate_max_progress(media_list, MediaTypes.TV.value)

        sorted_media = _sort_tv_media_by_time_left(media_list, direction="asc")

        self.assertEqual(
            [media.item.title for media in sorted_media[:2]],
            ["Excluded Seasons Show", "Comparison Show"],
        )

        sorted_excluded = next(
            media for media in sorted_media if media.item.title == excluded.item.title
        )
        self.assertEqual(sorted_excluded.episodes_left_display, 9)
        self.assertEqual(sorted_excluded.time_left_display, "4h 30m")

        # The sort view changes only the time-left ordering/display math.
        self.assertEqual(sorted_excluded.episodes_left, 14)
        self.assertEqual(sorted_excluded.time_left, 420)
