"""Tests for Audiobookshelf importer."""

from datetime import UTC, datetime
from unittest.mock import call, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import Book, Item, MediaTypes, Sources, Status
from integrations.imports import helpers
from integrations.imports.audiobookshelf import (
    AudiobookshelfAuthError,
    AudiobookshelfClientError,
    AudiobookshelfImporter,
)
from integrations.imports.helpers import MediaImportError
from integrations.models import AudiobookshelfAccount


class AudiobookshelfImporterTests(TestCase):
    """Validate ABS import mapping and filtering."""

    def setUp(self):
        """Create test user and connected ABS account."""
        self.user = get_user_model().objects.create_user(
            username="abs-user",
            password="pass",  # noqa: S106
        )
        AudiobookshelfAccount.objects.create(
            user=self.user,
            base_url="https://abs.example.com",
            api_token=helpers.encrypt("token"),
        )

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_imports_audiobook_progress_as_book(self, mock_me, mock_item):
        """Import ABS audiobook progress into Book rows."""
        mock_me.return_value = {
            "mediaProgress": [
                {
                    "libraryItemId": "item-1",
                    "currentTime": 3600,
                    "duration": 7200,
                    "progress": 0.5,
                    "isFinished": False,
                    "lastUpdate": 1000,
                },
            ],
        }
        mock_item.return_value = {
            "media": {
                "duration": 7200,
                "metadata": {
                    "title": "The Hobbit",
                    "authors": [{"name": "J.R.R. Tolkien"}],
                },
            },
            "coverPath": "https://img.example/hobbit.jpg",
        }

        importer = AudiobookshelfImporter(self.user)
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")

        media = Book.objects.get(user=self.user)
        self.assertEqual(media.status, Status.IN_PROGRESS.value)
        self.assertEqual(media.progress, 60)
        self.assertEqual(media.item.source, Sources.AUDIOBOOKSHELF.value)
        self.assertEqual(media.item.media_type, MediaTypes.BOOK.value)
        self.assertEqual(media.item.runtime_minutes, 120)

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_skips_podcast_episode_progress(self, mock_me, mock_item):
        """Skip ABS podcast episode progress in v1."""
        mock_me.return_value = {
            "mediaProgress": [
                {
                    "libraryItemId": "podcast-item",
                    "episodeId": "ep-1",
                    "currentTime": 100,
                    "lastUpdate": 2000,
                },
            ],
        }

        importer = AudiobookshelfImporter(self.user)
        counts, _ = importer.import_data()

        self.assertEqual(counts, {})
        self.assertFalse(Item.objects.filter(source=Sources.AUDIOBOOKSHELF.value).exists())
        mock_item.assert_not_called()

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_parses_millisecond_timestamps_for_started_and_finished(
        self,
        mock_me,
        mock_item,
    ):
        """Use UTC-aware datetimes when ABS returns millisecond timestamps."""
        started_at_ms = 1_700_000_000_000
        finished_at_ms = 1_700_003_600_000
        mock_me.return_value = {
            "mediaProgress": [
                {
                    "libraryItemId": "item-2",
                    "currentTime": 7200,
                    "duration": 7200,
                    "progress": 1,
                    "isFinished": True,
                    "startedAt": started_at_ms,
                    "finishedAt": finished_at_ms,
                    "lastUpdate": finished_at_ms,
                },
            ],
        }
        mock_item.return_value = {
            "media": {
                "duration": 7200,
                "metadata": {
                    "title": "Dune",
                    "authors": [{"name": "Frank Herbert"}],
                },
            },
            "coverPath": "https://img.example/dune.jpg",
        }

        importer = AudiobookshelfImporter(self.user)
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")

        media = Book.objects.get(user=self.user)
        self.assertEqual(media.status, Status.COMPLETED.value)
        self.assertEqual(
            media.start_date,
            datetime.fromtimestamp(started_at_ms / 1000, tz=UTC),
        )
        self.assertEqual(
            media.end_date,
            datetime.fromtimestamp(finished_at_ms / 1000, tz=UTC),
        )

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_respects_last_sync_cursor_and_updates_last_sync_ms(
        self,
        mock_me,
        mock_item,
    ):
        """Changed rows import first, and unchanged missing rows are repaired."""
        account = self.user.audiobookshelf_account
        account.last_sync_ms = 1_500
        account.save(update_fields=["last_sync_ms", "updated_at"])

        mock_me.return_value = {
            "mediaProgress": [
                {
                    "libraryItemId": "old-item",
                    "currentTime": 120,
                    "lastUpdate": 1_000,
                },
                {
                    "libraryItemId": "new-item",
                    "currentTime": 1_800,
                    "duration": 3_600,
                    "lastUpdate": 2_000,
                },
            ],
        }
        mock_item.side_effect = lambda library_item_id: {
            "media": {
                "duration": 3_600,
                "metadata": {
                    "title": "New Book"
                    if library_item_id == "new-item"
                    else "Old Book",
                    "authors": [{"name": "Brandon Sanderson"}],
                },
            },
            "coverPath": f"https://img.example/{library_item_id}.jpg",
        }

        importer = AudiobookshelfImporter(self.user)
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 2)
        self.assertEqual(warnings, "")
        self.assertEqual(Book.objects.filter(user=self.user).count(), 2)
        self.assertCountEqual(
            Book.objects.filter(user=self.user).values_list("item__title", flat=True),
            ["New Book", "Old Book"],
        )
        self.user.audiobookshelf_account.refresh_from_db()
        self.assertEqual(self.user.audiobookshelf_account.last_sync_ms, 2_000)
        self.assertEqual(
            mock_item.call_args_list,
            [call("new-item"), call("old-item")],
        )

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_repairs_unchanged_completed_item_missing_metadata_after_cursor_advance(
        self,
        mock_me,
        mock_item,
    ):
        """Repair unchanged completed ABS books when local metadata is sparse."""
        account = self.user.audiobookshelf_account
        account.last_sync_ms = 2_000
        account.save(update_fields=["last_sync_ms", "updated_at"])

        importer = AudiobookshelfImporter(self.user)
        media_id = importer._stable_media_id(account.base_url, "completed-item")
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.AUDIOBOOKSHELF.value,
            media_type=MediaTypes.BOOK.value,
            title="The Emperor's Soul",
            image=settings.IMG_NONE,
            authors=["Brandon Sanderson"],
            format="audiobook",
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=211,
        )

        mock_me.return_value = {
            "mediaProgress": [
                {
                    "libraryItemId": "completed-item",
                    "currentTime": 12_660,
                    "duration": 12_660,
                    "progress": 1,
                    "isFinished": True,
                    "startedAt": 1_739_145_600_000,
                    "finishedAt": 1_739_923_200_000,
                    "lastUpdate": 1_500,
                },
            ],
        }
        mock_item.return_value = {
            "media": {
                "duration": 12_660,
                "metadata": {
                    "title": "The Emperor's Soul",
                    "authors": [{"name": "Brandon Sanderson"}],
                    "isbn": "978-1-61696-058-2",
                },
            },
            "coverPath": "",
        }
        with (
            patch(
                "integrations.imports.audiobookshelf.services.search",
                return_value={
                    "results": [
                        {
                            "media_id": "314",
                            "source": Sources.HARDCOVER.value,
                            "title": "The Emperor's Soul",
                        },
                    ],
                },
            ) as mock_search,
            patch(
                "integrations.imports.audiobookshelf.services.get_media_metadata",
                return_value={
                    "media_id": "314",
                    "source": Sources.HARDCOVER.value,
                    "media_type": MediaTypes.BOOK.value,
                    "title": "The Emperor's Soul",
                    "image": "https://covers.example/emperor.jpg",
                    "genres": ["Fantasy"],
                    "details": {
                        "author": "Brandon Sanderson",
                        "publisher": "Subterranean Press",
                        "isbn": ["9781616960582"],
                        "publish_date": "2012-10-11",
                    },
                },
            ) as mock_get_media_metadata,
        ):
            importer = AudiobookshelfImporter(self.user)
            importer.enable_provider_enrichment = True
            counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")

        item.refresh_from_db()
        self.assertEqual(item.image, "https://covers.example/emperor.jpg")
        self.assertEqual(item.isbn, ["9781616960582"])
        self.assertEqual(item.publishers, "Subterranean Press")
        self.assertEqual(item.genres, ["Fantasy"])
        self.assertEqual(
            item.release_datetime,
            datetime(2012, 10, 11, tzinfo=UTC),
        )
        self.assertEqual(item.original_title, "The Emperor's Soul")
        self.assertEqual(item.localized_title, "The Emperor's Soul")
        self.assertIsNotNone(item.metadata_fetched_at)
        mock_item.assert_called_once_with("completed-item")
        mock_search.assert_called_once()
        mock_get_media_metadata.assert_called_once()

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_collects_warning_when_library_item_lookup_fails(self, mock_me, mock_item):
        """A failed item metadata fetch should not abort the whole import."""
        mock_me.return_value = {
            "mediaProgress": [
                {"libraryItemId": "broken-item", "lastUpdate": 3_000},
            ],
        }
        mock_item.side_effect = AudiobookshelfClientError("boom")

        importer = AudiobookshelfImporter(self.user)
        counts, warnings = importer.import_data()

        self.assertEqual(counts, {})
        self.assertIn("broken-item", warnings)
        self.assertFalse(Book.objects.filter(user=self.user).exists())

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_retries_unchanged_item_after_prior_lookup_failure(
        self,
        mock_me,
        mock_item,
    ):
        """A prior lookup failure should be retried on the next unchanged import."""
        mock_me.return_value = {
            "mediaProgress": [
                {
                    "libraryItemId": "broken-item",
                    "currentTime": 1_200,
                    "duration": 3_600,
                    "lastUpdate": 3_000,
                },
            ],
        }
        mock_item.side_effect = [
            AudiobookshelfClientError("boom"),
            {
                "media": {
                    "duration": 3_600,
                    "metadata": {
                        "title": "Warbreaker",
                        "authors": [{"name": "Brandon Sanderson"}],
                        "isbn": "978-0-7653-2030-8",
                        "publisher": "Tor",
                        "genres": ["Fantasy"],
                    },
                },
                "coverPath": "https://img.example/warbreaker.jpg",
            },
        ]

        importer = AudiobookshelfImporter(self.user)
        first_counts, first_warnings = importer.import_data()

        self.assertEqual(first_counts, {})
        self.assertIn("broken-item", first_warnings)
        self.user.audiobookshelf_account.refresh_from_db()
        self.assertEqual(self.user.audiobookshelf_account.last_sync_ms, 3_000)
        self.assertFalse(
            Item.objects.filter(
                source=Sources.AUDIOBOOKSHELF.value,
                media_type=MediaTypes.BOOK.value,
            ).exists(),
        )

        importer = AudiobookshelfImporter(self.user)
        second_counts, second_warnings = importer.import_data()

        self.assertEqual(second_counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(second_warnings, "")
        self.assertEqual(mock_item.call_count, 2)

        item = Book.objects.get(user=self.user).item
        self.assertEqual(item.title, "Warbreaker")
        self.assertEqual(item.image, "https://img.example/warbreaker.jpg")
        self.assertEqual(item.isbn, ["9780765320308"])
        self.assertEqual(item.publishers, "Tor")
        self.assertEqual(item.genres, ["Fantasy"])
        self.assertIsNotNone(item.metadata_fetched_at)

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_falls_back_to_item_title_and_plain_string_authors(
        self,
        mock_me,
        mock_item,
    ):
        """Importer should support string authors and top-level title fallback."""
        mock_me.return_value = {
            "mediaProgress": [
                {
                    "libraryItemId": "item-3",
                    "currentTime": 300,
                    "lastUpdate": 5_000,
                },
            ],
        }
        mock_item.return_value = {
            "title": "Fallback Title",
            "media": {
                "metadata": {
                    "authors": [
                        "Author One",
                        {"name": "Author Two"},
                        {"name": ""},
                    ],
                },
            },
        }

        importer = AudiobookshelfImporter(self.user)
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")

        item = Book.objects.get(user=self.user).item
        self.assertEqual(item.title, "Fallback Title")
        self.assertEqual(item.authors, ["Author One", "Author Two"])

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_normalizes_relative_abs_cover_paths(self, mock_me, mock_item):
        """Relative Audiobookshelf cover paths should be converted to absolute URLs."""
        mock_me.return_value = {
            "mediaProgress": [
                {
                    "libraryItemId": "item-4",
                    "currentTime": 1_200,
                    "lastUpdate": 6_000,
                },
            ],
        }
        mock_item.return_value = {
            "media": {
                "duration": 3_600,
                "metadata": {
                    "title": "Words of Radiance",
                    "authors": [{"name": "Brandon Sanderson"}],
                },
            },
            "coverPath": "/api/items/item-4/cover",
        }

        importer = AudiobookshelfImporter(self.user)
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")
        item = Book.objects.get(user=self.user).item
        self.assertEqual(
            item.image,
            "https://abs.example.com/api/items/item-4/cover",
        )

    @patch("integrations.imports.audiobookshelf.services.get_media_metadata")
    @patch("integrations.imports.audiobookshelf.services.search")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_enriches_missing_cover_from_book_provider(
        self,
        mock_me,
        mock_item,
        mock_search,
        mock_get_media_metadata,
    ):
        """Importer should enrich ABS books when Audiobookshelf has no cover."""
        mock_me.return_value = {
            "mediaProgress": [
                {
                    "libraryItemId": "item-5",
                    "currentTime": 2_400,
                    "lastUpdate": 7_000,
                },
            ],
        }
        mock_item.return_value = {
            "media": {
                "duration": 4_800,
                "metadata": {
                    "title": "Mistborn",
                    "isbn": "978-0-7653-1178-8",
                },
            },
            "coverPath": "",
        }

        mock_search.return_value = {
            "results": [
                {
                    "media_id": "314",
                    "source": Sources.HARDCOVER.value,
                    "title": "Mistborn: The Final Empire",
                },
            ],
        }
        mock_get_media_metadata.return_value = {
            "media_id": "314",
            "source": Sources.HARDCOVER.value,
            "media_type": MediaTypes.BOOK.value,
            "title": "Mistborn: The Final Empire",
            "image": "https://covers.example/mistborn.jpg",
            "max_progress": 541,
            "genres": ["Fantasy"],
            "series_name": "Mistborn",
            "series_position": 1,
            "details": {
                "author": "Brandon Sanderson",
                "publisher": "Tor",
                "isbn": ["9780765311788"],
                "publish_date": "2006-07-17",
            },
        }

        importer = AudiobookshelfImporter(self.user)
        importer.enable_provider_enrichment = True
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")
        item = Book.objects.get(user=self.user).item
        self.assertEqual(item.image, "https://covers.example/mistborn.jpg")
        self.assertEqual(item.authors, ["Brandon Sanderson"])
        self.assertEqual(item.isbn, ["9780765311788"])
        self.assertEqual(item.publishers, "Tor")
        self.assertEqual(item.genres, ["Fantasy"])
        self.assertEqual(item.series_name, "Mistborn")
        self.assertEqual(item.series_position, 1)
        self.assertEqual(
            item.release_datetime,
            datetime(2006, 7, 17, tzinfo=UTC),
        )

        mock_search.assert_called_once_with(
            MediaTypes.BOOK.value,
            "9780765311788",
            1,
            Sources.HARDCOVER.value,
        )
        mock_get_media_metadata.assert_any_call(
            MediaTypes.BOOK.value,
            "314",
            Sources.HARDCOVER.value,
        )

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_marks_connection_broken_on_auth_error(self, mock_me):
        """Auth failures should mark the account as broken and raise import error."""
        mock_me.side_effect = AudiobookshelfAuthError(
            "Audiobookshelf token is invalid or expired",
        )

        importer = AudiobookshelfImporter(self.user)

        with self.assertRaises(MediaImportError):
            importer.import_data()

        self.user.audiobookshelf_account.refresh_from_db()
        self.assertTrue(self.user.audiobookshelf_account.connection_broken)
        self.assertIn(
            "invalid or expired",
            self.user.audiobookshelf_account.last_error_message,
        )

    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_library_item")
    @patch("integrations.imports.audiobookshelf.AudiobookshelfClient.get_me")
    def test_does_not_refetch_unchanged_books_that_are_already_hydrated(
        self,
        mock_me,
        mock_item,
    ):
        """Healthy unchanged ABS books should not trigger repair lookups."""
        account = self.user.audiobookshelf_account
        account.last_sync_ms = 2_000
        account.save(update_fields=["last_sync_ms", "updated_at"])

        importer = AudiobookshelfImporter(self.user)
        media_id = importer._stable_media_id(account.base_url, "healthy-item")
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.AUDIOBOOKSHELF.value,
            media_type=MediaTypes.BOOK.value,
            title="The Blade Itself",
            original_title="The Blade Itself",
            localized_title="The Blade Itself",
            image="https://covers.example/blade.jpg",
            authors=["Joe Abercrombie"],
            isbn=["9780316387310"],
            publishers="Orbit",
            genres=["Fantasy"],
            release_datetime=datetime(2006, 5, 4, tzinfo=UTC),
            format="audiobook",
            metadata_fetched_at=timezone.now(),
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.IN_PROGRESS.value,
            progress=60,
        )

        mock_me.return_value = {
            "mediaProgress": [
                {
                    "libraryItemId": "healthy-item",
                    "currentTime": 3_600,
                    "duration": 12_000,
                    "lastUpdate": 1_500,
                },
            ],
        }

        importer = AudiobookshelfImporter(self.user)
        counts, warnings = importer.import_data()

        self.assertEqual(counts, {})
        self.assertEqual(warnings, "")
        mock_item.assert_not_called()
