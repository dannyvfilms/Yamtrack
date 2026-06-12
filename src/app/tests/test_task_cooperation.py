"""Tests for CooperativeRun and its adoption in backfill tasks."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from app.models import Item, MediaTypes, Sources
from app.task_cooperation import CooperativeRun


def _fake_items(count):
    return [SimpleNamespace(id=index + 1) for index in range(count)]


class CooperativeRunTests(TestCase):
    """Unit tests for the CooperativeRun iterator."""

    @patch("app.task_cooperation.interactive_request_active", return_value=False)
    def test_processes_all_items_when_idle(self, _mock_active):
        """Without interactive activity, every item is yielded."""
        run = CooperativeRun("test_idle")
        items = _fake_items(3)
        self.assertEqual(list(run.iter(items)), items)
        self.assertFalse(run.deferred)
        self.assertEqual(run.remaining, [])

    @patch("app.task_cooperation.interactive_request_active", return_value=True)
    def test_defers_after_min_progress(self, _mock_active):
        """With the flag set, the first item still processes; the rest defer."""
        run = CooperativeRun("test_defer")
        items = _fake_items(4)
        with self.assertLogs("app.task_cooperation", level="INFO") as logs:
            processed = list(run.iter(items))
        self.assertEqual(processed, items[:1])
        self.assertTrue(run.deferred)
        self.assertEqual(run.remaining, items[1:])
        self.assertEqual(run.remaining_ids, [2, 3, 4])
        self.assertIn("test_defer_deferred", logs.output[0])
        self.assertIn("processed=1 remaining=3", logs.output[0])

    @patch("app.task_cooperation.interactive_request_active")
    def test_defers_when_flag_flips_mid_run(self, mock_active):
        """A flag raised mid-iteration stops the run at that point."""
        mock_active.side_effect = [False, False, True]
        run = CooperativeRun("test_mid")
        items = _fake_items(5)
        processed = list(run.iter(items))
        self.assertEqual(processed, items[:3])
        self.assertTrue(run.deferred)
        self.assertEqual(run.remaining_ids, [4, 5])

    @patch("app.task_cooperation.interactive_request_active", return_value=True)
    def test_check_every_skips_checks(self, mock_active):
        """check_every batches the flag checks."""
        run = CooperativeRun("test_batch", check_every=3, min_progress=3)
        items = _fake_items(7)
        processed = list(run.iter(items))
        # Checks happen at indexes 3 and 6; deferral hits at index 3.
        self.assertEqual(processed, items[:3])
        self.assertEqual(mock_active.call_count, 1)

    @patch("app.task_cooperation.interactive_request_active", return_value=False)
    def test_empty_iterable(self, _mock_active):
        """An empty input yields nothing and does not defer."""
        run = CooperativeRun("test_empty")
        self.assertEqual(list(run.iter([])), [])
        self.assertFalse(run.deferred)


class GenreBackfillDeferralTests(TestCase):
    """The genre backfill loop re-enqueues the remainder when deferred."""

    def _create_items(self, count):
        return [
            Item.objects.create(
                media_id=f"coop_{index}",
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Coop Movie {index}",
                image="https://example.com/movie.jpg",
            )
            for index in range(count)
        ]

    @patch("app.tasks_genre.enqueue_genre_backfill_items")
    @patch("app.tasks_genre.services.get_media_metadata", return_value=None)
    @patch("app.task_cooperation.interactive_request_active", return_value=True)
    def test_deferred_items_are_reenqueued(
        self,
        _mock_active,
        mock_metadata,
        mock_enqueue,
    ):
        """Only the first item is processed; the rest go back on the queue."""
        from app.tasks_genre import _populate_genres_for_items

        items = self._create_items(3)
        _populate_genres_for_items(items, delay_seconds=0)

        self.assertEqual(mock_metadata.call_count, 1)
        mock_enqueue.assert_called_once_with([items[1].id, items[2].id])

    @patch("app.tasks_genre.enqueue_genre_backfill_items")
    @patch("app.tasks_genre.services.get_media_metadata", return_value=None)
    @patch("app.task_cooperation.interactive_request_active", return_value=False)
    def test_idle_run_processes_all_without_reenqueue(
        self,
        _mock_active,
        mock_metadata,
        mock_enqueue,
    ):
        """Without interactive activity the whole batch is processed."""
        from app.tasks_genre import _populate_genres_for_items

        items = self._create_items(3)
        _populate_genres_for_items(items, delay_seconds=0)

        self.assertEqual(mock_metadata.call_count, 3)
        mock_enqueue.assert_not_called()
