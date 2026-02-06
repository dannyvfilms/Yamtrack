from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from app import tasks
from app.models import (
    CREDITS_BACKFILL_VERSION,
    CreditRoleType,
    Episode,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Movie,
    Person,
    PersonGender,
    Season,
    Sources,
    Status,
    Studio,
    TV,
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
        missing_episode_item = Item.objects.create(
            media_id="1004",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Needs Episode Credits",
        )
        complete_episode_item = Item.objects.create(
            media_id="1005",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            title="Has Episode Credits",
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
        ItemPersonCredit.objects.create(
            item=complete_episode_item,
            person=person,
            role_type=CreditRoleType.CAST.value,
            role="Guest",
        )
        MetadataBackfillState.objects.create(
            item=complete_episode_item,
            field=MetadataBackfillField.CREDITS,
            last_success_at=timezone.now(),
            strategy_version=CREDITS_BACKFILL_VERSION,
        )
        cache.delete(tasks.CREDITS_BACKFILL_ITEMS_QUEUE_KEY)
        cache.delete(tasks.CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY)
        mock_apply_async.reset_mock()

        queued = tasks.enqueue_credits_backfill_items(
            [
                missing_item.id,
                complete_item.id,
                manual_item.id,
                missing_episode_item.id,
                complete_episode_item.id,
            ],
            countdown=1,
        )

        self.assertEqual(queued, 2)
        self.assertCountEqual(
            cache.get(tasks.CREDITS_BACKFILL_ITEMS_QUEUE_KEY),
            [missing_item.id, missing_episode_item.id],
        )
        mock_apply_async.assert_called_once_with(countdown=1)

    @patch("app.tasks.populate_credits_backfill_queue.apply_async")
    def test_enqueue_credits_backfill_requeues_episode_with_old_strategy_version(self, mock_apply_async):
        episode_item = Item.objects.create(
            media_id="1006",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=3,
            title="Stale Episode Credits",
        )
        person = Person.objects.create(
            source=Sources.TMDB.value,
            source_person_id="5002",
            name="Legacy Credit Person",
            gender=PersonGender.UNKNOWN.value,
        )
        ItemPersonCredit.objects.create(
            item=episode_item,
            person=person,
            role_type=CreditRoleType.CAST.value,
            role="Lead",
        )
        MetadataBackfillState.objects.create(
            item=episode_item,
            field=MetadataBackfillField.CREDITS,
            last_success_at=timezone.now(),
            strategy_version=max(CREDITS_BACKFILL_VERSION - 1, 1),
        )

        cache.delete(tasks.CREDITS_BACKFILL_ITEMS_QUEUE_KEY)
        cache.delete(tasks.CREDITS_BACKFILL_ITEMS_SCHEDULED_KEY)
        mock_apply_async.reset_mock()

        queued = tasks.enqueue_credits_backfill_items([episode_item.id], countdown=1)

        self.assertEqual(queued, 1)
        self.assertEqual(cache.get(tasks.CREDITS_BACKFILL_ITEMS_QUEUE_KEY), [episode_item.id])
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
        self.assertEqual(state.strategy_version, CREDITS_BACKFILL_VERSION)
        self.assertFalse(state.give_up)
        self.assertIsNotNone(state.last_success_at)

    @patch("app.tasks.enqueue_credits_backfill_items")
    @patch("app.credits.sync_item_credits_from_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_populate_credits_data_for_episode_items_uses_episode_lookup(
        self,
        mock_get_metadata,
        mock_sync,
        _mock_enqueue,
    ):
        item = Item.objects.create(
            media_id="2002",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=3,
            title="Backfill Episode",
            runtime_minutes=45,
        )
        mock_get_metadata.return_value = {
            "title": "Backfill Show",
            "season_title": "Season 1",
            "episode_title": "Episode 3",
            "cast": [],
            "crew": [],
        }
        MetadataBackfillState.objects.filter(item=item, field=MetadataBackfillField.CREDITS).delete()

        result = tasks.populate_credits_data_for_items([item.id])

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["errors"], 0)
        mock_get_metadata.assert_called_once_with(
            MediaTypes.EPISODE.value,
            item.media_id,
            item.source,
            [item.season_number],
            item.episode_number,
        )
        mock_sync.assert_called_once()
        state = MetadataBackfillState.objects.get(item=item, field=MetadataBackfillField.CREDITS)
        self.assertEqual(state.strategy_version, CREDITS_BACKFILL_VERSION)


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

    @patch("app.providers.services.get_media_metadata")
    @patch("app.tasks.enqueue_credits_backfill_items")
    def test_episode_play_save_queues_episode_and_show_credit_backfill(self, mock_enqueue, mock_get_metadata):
        user = get_user_model().objects.create_user(username="episode-signal-user", password="test12345")
        tv_item = Item.objects.create(
            media_id="4001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Signal Show",
        )
        tv = TV.objects.create(
            item=tv_item,
            user=user,
            status=Status.PLANNING.value,
        )
        season_item, _ = Item.objects.get_or_create(
            media_id="4001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={"title": "Signal Show"},
        )
        season = Season.objects.create(
            item=season_item,
            user=user,
            related_tv=tv,
            status=Status.PLANNING.value,
        )
        episode_item, _ = Item.objects.get_or_create(
            media_id="4001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            defaults={
                "title": "Signal Show",
                "runtime_minutes": 45,
            },
        )
        mock_get_metadata.return_value = {
            "season/1": {
                "episodes": [{"episode_number": 1}, {"episode_number": 2}],
            },
            "related": {
                "seasons": [{"season_number": 1}],
            },
        }
        mock_enqueue.reset_mock()

        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=timezone.now(),
        )

        self.assertEqual(mock_enqueue.call_count, 2)
        mock_enqueue.assert_any_call([episode_item.id], countdown=3)
        mock_enqueue.assert_any_call([tv_item.id], countdown=3)
