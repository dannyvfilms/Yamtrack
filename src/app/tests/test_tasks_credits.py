from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from app import tasks
from app.models import (
    CreditRoleType,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Movie,
    Person,
    PersonGender,
    Sources,
    Status,
    Studio,
)


class CreditsBackfillTaskTests(TestCase):
    def setUp(self):
        cache.delete(tasks.CREDITS_BACKFILL_ITEMS_QUEUE_KEY)
        cache.delete(tasks.CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY)

    @patch("app.tasks.populate_credits_backfill_queue.apply_async")
    def test_enqueue_credits_backfill_filters_unsupported_or_complete_items(self, mock_apply_async):
        missing_item = Item.objects.create(
            media_id="1001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Needs Credits",
            runtime_minutes=90,
            genres=["Drama"],
        )
        complete_item = Item.objects.create(
            media_id="1002",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Has Credits",
            runtime_minutes=95,
            genres=["Sci-Fi"],
        )
        manual_item = Item.objects.create(
            media_id="1003",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Manual Movie",
        )
        person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="5001",
            name="Credit Person",
            gender=PersonGender.UNKNOWN.value,
        )
        studio = Studio.objects.create(
            source=Sources.TMDB.value,
            source_studio_id="6001",
            name="Credit Studio",
        )
        ItemPersonCredit.objects.create(
            item=complete_item,
            person=person,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        ItemStudioCredit.objects.create(
            item=complete_item,
            studio=studio,
        )
        cache.delete(tasks.CREDITS_BACKFILL_ITEMS_QUEUE_KEY)
        cache.delete(tasks.CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY)
        mock_apply_async.reset_mock()

        queued = tasks.enqueue_credits_backfill_items([missing_item.id, complete_item.id, manual_item.id], countdown=1)

        self.assertEqual(queued, 1)
        self.assertEqual(cache.get(tasks.CREDITS_BACKFILL_ITEMS_QUEUE_KEY), [missing_item.id])
        mock_apply_async.assert_called_once_with(countdown=1)

    @patch("app.tasks.enqueue_credits_backfill_items")
    @patch("app.credits.sync_item_credits_from_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_populate_credits_data_for_items_records_success(self, mock_get_metadata, mock_sync, _mock_enqueue):
        item = Item.objects.create(
            media_id="2001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Backfill Movie",
            runtime_minutes=100,
            genres=["Action"],
        )
        mock_get_metadata.return_value = {
            "media_id": "2001",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.MOVIE.value,
            "title": "Backfill Movie",
            "cast": [],
            "crew": [],
            "studios_full": [],
        }
        MetadataBackfillState.objects.filter(item=item, field=MetadataBackfillField.CREDITS).delete()

        result = tasks.populate_credits_data_for_items([item.id])

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["errors"], 0)
        mock_sync.assert_called_once()
        state = MetadataBackfillState.objects.get(item=item, field=MetadataBackfillField.CREDITS)
        self.assertEqual(state.fail_count, 0)
        self.assertFalse(state.give_up)
        self.assertIsNotNone(state.last_success_at)


class CreditsBackfillSignalTests(TestCase):
    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_movie_play_save_queues_credits_backfill(self, mock_enqueue):
        user = get_user_model().objects.create_user(username="signal-user", password="test12345")
        item = Item.objects.create(
            media_id="3001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Signal Movie",
            runtime_minutes=110,
            genres=["Mystery"],
        )
        mock_enqueue.reset_mock()

        Movie.objects.create(
            item=item,
            user=user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )

        mock_enqueue.assert_called_once_with([item.id], countdown=3)
