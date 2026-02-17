from datetime import date, timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from app import tasks
from app.models import Item, MediaTypes, Sources


class MetadataBackfillTaskTests(TestCase):
    @patch("app.tasks.services.get_media_metadata")
    def test_backfill_updates_release_datetime_for_existing_items(self, mock_get_media_metadata):
        old_fetched_at = timezone.now() - timedelta(days=30)
        item = Item.objects.create(
            media_id="262712",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Ananta",
            image="https://example.com/image.jpg",
            genres=["Action"],
            metadata_fetched_at=old_fetched_at,
        )
        cache_key = f"{Sources.IGDB.value}_{MediaTypes.GAME.value}_{item.media_id}"
        cache.set(cache_key, {"details": {"release_date": None}}, timeout=300)

        mock_get_media_metadata.return_value = {
            "details": {
                "release_date": "2002-11-15",
            },
        }

        result = tasks.backfill_item_metadata_task(batch_size=10)

        item.refresh_from_db()

        self.assertEqual(item.release_datetime.date(), date(2002, 11, 15))
        self.assertGreater(item.metadata_fetched_at, old_fetched_at)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["release_updated_count"], 1)
        self.assertEqual(result["error_count"], 0)
        self.assertIsNone(cache.get(cache_key))
        mock_get_media_metadata.assert_any_call(
            MediaTypes.GAME.value,
            item.media_id,
            item.source,
        )

    @patch("app.tasks.services.get_media_metadata")
    def test_backfill_initial_metadata_includes_release_datetime(self, mock_get_media_metadata):
        item = Item.objects.create(
            media_id="9999",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Book",
            image="https://example.com/book.jpg",
        )
        self.assertIsNone(item.metadata_fetched_at)

        mock_get_media_metadata.return_value = {
            "details": {
                "publish_date": "1999-03-31",
                "country": "US",
            },
        }

        result = tasks.backfill_item_metadata_task(batch_size=10)

        item.refresh_from_db()

        self.assertEqual(item.release_datetime.date(), date(1999, 3, 31))
        self.assertEqual(item.country, "US")
        self.assertIsNotNone(item.metadata_fetched_at)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["release_updated_count"], 1)
        self.assertEqual(result["error_count"], 0)

    @patch("app.tasks.services.get_media_metadata")
    def test_backfill_prioritizes_never_fetched_items(self, mock_get_media_metadata):
        never_fetched = Item.objects.create(
            media_id="100",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Never Fetched",
            image="https://example.com/a.jpg",
        )
        old_fetched_at = timezone.now() - timedelta(days=10)
        release_only = Item.objects.create(
            media_id="200",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Release Only",
            image="https://example.com/b.jpg",
            genres=["Action"],
            metadata_fetched_at=old_fetched_at,
        )

        mock_get_media_metadata.return_value = {
            "details": {"release_date": "2020-01-01"},
        }

        tasks.backfill_item_metadata_task(batch_size=1)

        never_fetched.refresh_from_db()
        release_only.refresh_from_db()

        self.assertIsNotNone(never_fetched.metadata_fetched_at)
        self.assertIsNone(release_only.release_datetime)
        self.assertEqual(release_only.metadata_fetched_at, old_fetched_at)
        mock_get_media_metadata.assert_any_call(
            MediaTypes.BOOK.value,
            never_fetched.media_id,
            never_fetched.source,
        )

    def test_genre_backfill_queryset_includes_reading_media_types(self):
        book = Item.objects.create(
            media_id="book-no-genre",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Book",
            image="https://example.com/book.jpg",
            genres=[],
        )
        comic = Item.objects.create(
            media_id="comic-no-genre",
            source=Sources.COMICVINE.value,
            media_type=MediaTypes.COMIC.value,
            title="Comic",
            image="https://example.com/comic.jpg",
            genres=[],
        )
        manga = Item.objects.create(
            media_id="manga-no-genre",
            source=Sources.MANGAUPDATES.value,
            media_type=MediaTypes.MANGA.value,
            title="Manga",
            image="https://example.com/manga.jpg",
            genres=[],
        )

        queued_ids = set(tasks._genre_items_queryset().values_list("id", flat=True))

        self.assertIn(book.id, queued_ids)
        self.assertIn(comic.id, queued_ids)
        self.assertIn(manga.id, queued_ids)
