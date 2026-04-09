"""Test that TV show status updates when a new season is detected."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import TV, Episode, Item, MediaTypes, Season, Sources, Status
from events.calendar.tv import process_tv_seasons


class NewSeasonStatusUpdateTests(TestCase):
    """Test that completed TV shows update to In Progress when a new season is added."""

    def setUp(self):
        """Set up the test with a completed TV show."""
        # Use unique media_id for each test run to avoid conflicts
        import uuid
        self.test_id = str(uuid.uuid4())[:8]

        self.credentials = {"username": f"testuser_{self.test_id}", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        # Create TV show item
        self.tv_item, _ = Item.objects.get_or_create(
            media_id=f"test_{self.test_id}",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            defaults={
                "title": "Test Show",
                "image": "http://example.com/show.jpg",
            },
        )

        # Create TV instance - mock provider calls to avoid API requests
        # First, create with Planning status to avoid any triggers
        with patch("app.models.providers.services.get_media_metadata") as mock_get_metadata:
            # Mock for _start_next_available_season if triggered
            mock_get_metadata.return_value = {
                "related": {"seasons": [{"season_number": 1, "image": "http://example.com/season1.jpg"}]},
                "max_progress": 8,
                "details": {"seasons": 1},
            }
            self.tv_instance = TV(
                item=self.tv_item,
                user=self.user,
                status=Status.PLANNING.value,
            )
            # Save base to avoid triggering custom save logic
            TV.save_base(self.tv_instance)

        # Now set to Completed with proper mocking
        with patch("app.models.providers.services.get_media_metadata") as mock_get_metadata:
            mock_get_metadata.side_effect = [
                # First call from _completed() for tv_with_seasons
                {
                    "related": {"seasons": [{"season_number": 1}]},
                    "max_progress": 8,
                    "details": {"seasons": 1},
                },
                # Second call from _completed() for tv_with_seasons with season numbers
                {
                    "season/1": {
                        "image": "http://example.com/season1.jpg",
                        "season_number": 1,
                        "episodes": [
                            {"episode_number": i, "still_path": f"/ep{i}.jpg"}
                            for i in range(1, 9)
                        ],
                    },
                },
            ]
            self.tv_instance.status = Status.COMPLETED.value
            self.tv_instance.save()

        # Create Season 1 item
        self.season1_item, _ = Item.objects.get_or_create(
            media_id=f"test_{self.test_id}",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={
                "title": "Test Show",
                "image": "http://example.com/season1.jpg",
            },
        )

        # Create Season 1 instance with Completed status
        self.season1, _ = Season.objects.get_or_create(
            item=self.season1_item,
            related_tv=self.tv_instance,
            user=self.user,
            defaults={"status": Status.COMPLETED.value},
        )
        # Ensure it's set to Completed for the test
        self.season1.status = Status.COMPLETED.value
        self.season1.save()

        # Create 8 episode items for Season 1
        self.episode_items = []
        for ep_num in range(1, 9):
            episode_item, _ = Item.objects.get_or_create(
                media_id=f"test_{self.test_id}",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=ep_num,
                defaults={
                    "title": "Test Show",
                    "image": "http://example.com/ep.jpg",
                },
            )
            self.episode_items.append(episode_item)

            # Create Episode instances (watched) if they don't exist
            Episode.objects.get_or_create(
                item=episode_item,
                related_season=self.season1,
                defaults={
                    "end_date": timezone.now() - timezone.timedelta(days=10 - ep_num),
                },
            )

        # Verify initial state
        self.tv_instance.refresh_from_db()
        self.assertEqual(self.tv_instance.status, Status.COMPLETED.value)
        self.season1.refresh_from_db()
        self.assertEqual(self.season1.status, Status.COMPLETED.value)
        self.assertEqual(self.season1.episodes.count(), 8)

    @patch("events.calendar.tv.cache_utils.clear_time_left_cache_for_user")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    def test_new_season_updates_completed_tv_status(
        self,
        mock_tv_with_seasons,
        mock_clear_time_left_cache,
    ):
        """New-season detection should reopen the TV show without auto-starting the season."""
        season_metadata = {
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": "2024-01-15",
                        "still_path": "/ep1.jpg",
                    },
                    {
                        "episode_number": 2,
                        "air_date": "2024-01-22",
                        "still_path": "/ep2.jpg",
                    },
                ],
                "tvdb_id": None,
            },
        }
        mock_tv_with_seasons.return_value = season_metadata

        # Create events_bulk list to collect events
        events_bulk = []

        # Process Season 2 (this should detect it as new and update TV status)
        # Season 2 Item doesn't exist yet, so it will be created
        process_tv_seasons(self.tv_item, [2], events_bulk)

        # Verify Season 2 Item was created
        season2_item = Item.objects.get(
            media_id=f"test_{self.test_id}",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=2,
        )
        self.assertIsNotNone(season2_item)
        self.assertEqual(season2_item.title, "Test Show")

        self.assertFalse(
            Season.objects.filter(
                item=season2_item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            ).exists(),
        )

        # Verify events were created for Season 2 episodes
        self.assertEqual(len(events_bulk), 2)  # 2 episodes

        # Most importantly: Verify TV status changed from Completed to In Progress
        self.tv_instance.refresh_from_db()
        self.assertEqual(
            self.tv_instance.status,
            Status.IN_PROGRESS.value,
            "TV show status should have changed from Completed to In Progress",
        )
        mock_clear_time_left_cache.assert_any_call(self.user.id)

    @patch("events.calendar.tv.cache_utils.clear_time_left_cache_for_user")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    def test_announced_future_season_reopens_show_without_starting_season(
        self,
        mock_tv_with_seasons,
        mock_clear_time_left_cache,
    ):
        """A newly announced season should reopen the show, but not auto-start the season."""
        future_date = (timezone.now() + timezone.timedelta(days=30)).strftime("%Y-%m-%d")
        season_metadata = {
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": future_date,
                        "still_path": "/ep1.jpg",
                    },
                ],
                "tvdb_id": None,
            },
        }
        mock_tv_with_seasons.return_value = season_metadata

        events_bulk = []
        process_tv_seasons(self.tv_item, [2], events_bulk)

        self.tv_instance.refresh_from_db()
        self.assertEqual(self.tv_instance.status, Status.IN_PROGRESS.value)
        self.assertFalse(
            self.tv_instance.seasons.filter(
                item__season_number=2,
                status=Status.IN_PROGRESS.value,
            ).exists(),
        )
        mock_clear_time_left_cache.assert_any_call(self.user.id)

    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    def test_new_season_does_not_update_dropped_status(self, mock_tv_with_seasons):
        """Test that Dropped status remains unchanged when a new season is detected."""
        # Change TV status to Dropped
        self.tv_instance.status = Status.DROPPED.value
        self.tv_instance.save()

        # Mock provider responses
        mock_tv_with_seasons.return_value = {
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": "2024-01-15",
                    },
                ],
                "tvdb_id": None,
            },
        }

        events_bulk = []
        process_tv_seasons(self.tv_item, [2], events_bulk)

        # Verify status remains Dropped
        self.tv_instance.refresh_from_db()
        self.assertEqual(
            self.tv_instance.status,
            Status.DROPPED.value,
            "Dropped status should remain unchanged",
        )

    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    def test_new_season_does_not_update_planning_status(self, mock_tv_with_seasons):
        """Test that Planning status remains unchanged when a new season is detected."""
        # Change TV status to Planning
        self.tv_instance.status = Status.PLANNING.value
        self.tv_instance.save()

        # Mock provider responses
        mock_tv_with_seasons.return_value = {
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": "2024-01-15",
                    },
                ],
                "tvdb_id": None,
            },
        }

        events_bulk = []
        process_tv_seasons(self.tv_item, [2], events_bulk)

        # Verify status remains Planning
        self.tv_instance.refresh_from_db()
        self.assertEqual(
            self.tv_instance.status,
            Status.PLANNING.value,
            "Planning status should remain unchanged",
        )

    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    def test_new_season_does_not_update_paused_status(self, mock_tv_with_seasons):
        """Test that Paused status remains unchanged when a new season is detected."""
        # Change TV status to Paused
        self.tv_instance.status = Status.PAUSED.value
        self.tv_instance.save()

        # Mock provider responses
        mock_tv_with_seasons.return_value = {
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": "2024-01-15",
                    },
                ],
                "tvdb_id": None,
            },
        }

        events_bulk = []
        process_tv_seasons(self.tv_item, [2], events_bulk)

        # Verify status remains Paused
        self.tv_instance.refresh_from_db()
        self.assertEqual(
            self.tv_instance.status,
            Status.PAUSED.value,
            "Paused status should remain unchanged",
        )
