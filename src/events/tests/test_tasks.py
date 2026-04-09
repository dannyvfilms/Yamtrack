from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import Item, MediaTypes, Sources
from events.tasks import reload_calendar


class ReloadCalendarTaskTests(TestCase):
    """Tests for the reload_calendar Celery task."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="taskuser",
            password="pass12345",
        )

    @patch("events.tasks.auto_pause.auto_pause_stale_items")
    @patch("events.tasks.fetch_releases")
    def test_auto_pause_runs_for_global_refresh(self, mock_fetch, mock_auto_pause):
        mock_fetch.return_value = "ok"

        result = reload_calendar()

        self.assertEqual(result, "ok")
        mock_fetch.assert_called_once_with(user=None, items_to_process=None)
        mock_auto_pause.assert_called_once_with()

    @patch("events.tasks.auto_pause.auto_pause_stale_items")
    @patch("events.tasks.fetch_releases")
    def test_auto_pause_skipped_for_single_user(self, mock_fetch, mock_auto_pause):
        mock_fetch.return_value = "ok"

        result = reload_calendar(user_id=self.user.id)

        self.assertEqual(result, "ok")
        mock_fetch.assert_called_once_with(user=self.user, items_to_process=None)
        mock_auto_pause.assert_not_called()

    @patch("events.tasks.auto_pause.auto_pause_stale_items")
    @patch("events.tasks.fetch_releases")
    def test_item_ids_are_resolved_before_fetch(self, mock_fetch, mock_auto_pause):
        movie = Item.objects.create(
            media_id="603",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Matrix",
            image="https://example.com/matrix.jpg",
        )
        mock_fetch.return_value = "ok"

        result = reload_calendar(item_ids=[movie.id])

        self.assertEqual(result, "ok")
        mock_fetch.assert_called_once_with(user=None, items_to_process=[movie])
        mock_auto_pause.assert_not_called()

    @patch("app.tasks.backfill_item_metadata_task")
    @patch("events.tasks.auto_pause.auto_pause_stale_items")
    @patch("events.tasks.fetch_releases")
    def test_release_backfill_runs_on_global_refresh_when_release_dates_missing(
        self,
        mock_fetch,
        mock_auto_pause,
        mock_backfill_task,
    ):
        mock_fetch.return_value = "ok"
        mock_backfill_task.return_value = {
            "success_count": 1,
            "release_updated_count": 1,
            "error_count": 0,
            "remaining_metadata": 0,
            "remaining_release": 0,
        }
        Item.objects.create(
            media_id="262712",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Ananta",
            image="https://example.com/ananta.jpg",
            metadata_fetched_at=timezone.now(),
            release_datetime=None,
        )

        result = reload_calendar()

        self.assertEqual(result, "ok")
        mock_fetch.assert_called_once_with(user=None, items_to_process=None)
        mock_auto_pause.assert_called_once_with()
        mock_backfill_task.assert_called_once_with(batch_size=1000)
