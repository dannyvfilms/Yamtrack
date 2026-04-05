from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from app import signals, tasks
from app.models import (
    Item,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Sources,
)


class ItemSignalTests(TestCase):
    def test_item_save_does_not_delete_current_genre_state_on_unrelated_update(self):
        item = Item.objects.create(
            media_id="3001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Signal Show",
            image="https://example.com/signal-show.jpg",
            genres=["Drama", "Anime"],
        )
        MetadataBackfillState.objects.create(
            item=item,
            field=MetadataBackfillField.GENRES,
            strategy_version=tasks.GENRE_BACKFILL_VERSION,
            last_success_at=timezone.now(),
        )

        signals.schedule_runtime_backfill_on_item_save(
            sender=Item,
            instance=item,
            created=False,
            update_fields={"title"},
        )

        self.assertTrue(
            MetadataBackfillState.objects.filter(
                item=item,
                field=MetadataBackfillField.GENRES,
                strategy_version=tasks.GENRE_BACKFILL_VERSION,
            ).exists(),
        )

    @override_settings(TESTING=False)
    @patch("app.tasks.enqueue_runtime_backfill_items", return_value=0)
    @patch("app.tasks.enqueue_credits_backfill_items", return_value=0)
    @patch("app.tasks.enqueue_genre_backfill_items", return_value=1)
    def test_item_save_requeues_tmdb_tv_genre_backfill_on_identity_change(
        self,
        mock_enqueue_genre_backfill_items,
        _mock_enqueue_credits_backfill_items,
        _mock_enqueue_runtime_backfill_items,
    ):
        item = Item.objects.create(
            media_id="3002",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Signal Identity Change",
            image="https://example.com/signal-identity-change.jpg",
            genres=["Drama"],
        )
        MetadataBackfillState.objects.create(
            item=item,
            field=MetadataBackfillField.GENRES,
            strategy_version=tasks.GENRE_BACKFILL_VERSION,
            last_success_at=timezone.now(),
        )
        mock_enqueue_genre_backfill_items.reset_mock()

        signals.schedule_runtime_backfill_on_item_save(
            sender=Item,
            instance=item,
            created=False,
            update_fields={"media_id"},
        )

        self.assertFalse(
            MetadataBackfillState.objects.filter(
                item=item,
                field=MetadataBackfillField.GENRES,
            ).exists(),
        )
        mock_enqueue_genre_backfill_items.assert_called_once_with([item.id])
