from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from app import tasks
from app.models import (
    Book,
    Item,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Movie,
    Sources,
    Status,
)


class TraktPopularityTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="trakt-task-user",
            password="pw12345",
        )

    def _create_tracked_movie(
        self,
        media_id,
        title,
        *,
        release_days_ago=None,
        fetched_days_ago=None,
        with_data=False,
    ):
        release_datetime = None
        if release_days_ago is not None:
            release_datetime = timezone.now() - timedelta(days=release_days_ago)

        trakt_kwargs = {}
        if with_data:
            trakt_kwargs = {
                "trakt_rating": 8.0,
                "trakt_rating_count": 1200,
                "trakt_popularity_score": 1234.5,
                "trakt_popularity_rank": 42,
            }
        if fetched_days_ago is not None:
            trakt_kwargs["trakt_popularity_fetched_at"] = timezone.now() - timedelta(days=fetched_days_ago)

        item = Item.objects.create(
            media_id=str(media_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=title,
            image="https://example.com/movie.jpg",
            release_datetime=release_datetime,
            **trakt_kwargs,
        )
        Movie.objects.bulk_create(
            [
                Movie(
                    item=item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=0,
                ),
            ],
        )
        return item

    @patch("app.management.commands.backfill_trakt_popularity.trakt_popularity.refresh_trakt_popularity")
    def test_backfill_command_targets_only_missing_supported_tracked_items(self, mock_refresh):
        tracked_movie = self._create_tracked_movie("101", "Tracked Movie")
        self._create_tracked_movie(
            "102",
            "Already Populated",
            with_data=True,
            fetched_days_ago=1,
        )

        book_item = Item.objects.create(
            media_id="book-1",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Tracked Book",
            image="https://example.com/book.jpg",
        )
        Book.objects.bulk_create(
            [
                Book(
                    item=book_item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=0,
                ),
            ],
        )

        Item.objects.create(
            media_id="103",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Untracked Movie",
            image="https://example.com/untracked.jpg",
        )

        mock_refresh.return_value = {
            "rank": 5,
            "votes": 12345,
            "matched_id_type": "tmdb",
        }

        stdout = StringIO()
        call_command("backfill_trakt_popularity", stdout=stdout)

        mock_refresh.assert_called_once()
        self.assertEqual(mock_refresh.call_args.args[0].id, tracked_movie.id)
        self.assertEqual(
            mock_refresh.call_args.kwargs["route_media_type"],
            MediaTypes.MOVIE.value,
        )

    @patch("app.tasks.enqueue_trakt_popularity_backfill_items")
    def test_nightly_task_queues_only_missing_and_stale_trakt_items(self, mock_enqueue):
        missing_item = self._create_tracked_movie("201", "Missing Trakt Data", release_days_ago=20)
        recent_stale = self._create_tracked_movie(
            "202",
            "Recent Stale",
            release_days_ago=20,
            fetched_days_ago=15,
            with_data=True,
        )
        self._create_tracked_movie(
            "203",
            "Recent Fresh",
            release_days_ago=20,
            fetched_days_ago=5,
            with_data=True,
        )
        first_year_stale = self._create_tracked_movie(
            "204",
            "First Year Stale",
            release_days_ago=200,
            fetched_days_ago=61,
            with_data=True,
        )
        older_stale = self._create_tracked_movie(
            "205",
            "Older Stale",
            release_days_ago=900,
            fetched_days_ago=181,
            with_data=True,
        )
        self._create_tracked_movie(
            "206",
            "Older Fresh",
            release_days_ago=2200,
            fetched_days_ago=100,
            with_data=True,
        )

        mock_enqueue.side_effect = lambda item_ids, countdown=0: len(item_ids)

        summary = tasks.nightly_metadata_quality_backfill_task(
            genre_batch_size=0,
            runtime_batch_size=0,
            episode_season_batch_size=0,
            credits_batch_size=0,
            trakt_popularity_batch_size=10,
        )

        mock_enqueue.assert_called_once()
        queued_ids = set(mock_enqueue.call_args.args[0])
        self.assertEqual(
            queued_ids,
            {missing_item.id, recent_stale.id, first_year_stale.id, older_stale.id},
        )
        self.assertEqual(summary["selected"]["trakt_popularity"], 4)
        self.assertEqual(summary["queued"]["trakt_popularity"], 4)

    @patch("app.tasks.trakt_popularity_service.refresh_trakt_popularity", side_effect=RuntimeError("boom"))
    def test_populate_trakt_popularity_data_for_items_preserves_existing_values_on_failure(
        self,
        _mock_refresh_trakt_popularity,
    ):
        item = self._create_tracked_movie(
            "301",
            "Failure Path Movie",
            release_days_ago=20,
            fetched_days_ago=20,
            with_data=True,
        )

        tasks.populate_trakt_popularity_data_for_items([item.id], force=True)

        item.refresh_from_db()
        self.assertEqual(item.trakt_rating, 8.0)
        self.assertEqual(item.trakt_rating_count, 1200)
        self.assertEqual(item.trakt_popularity_rank, 42)
        state = MetadataBackfillState.objects.get(
            item=item,
            field=MetadataBackfillField.TRAKT_POPULARITY,
        )
        self.assertEqual(state.fail_count, 1)
