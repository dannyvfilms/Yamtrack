"""Tests for Audiobookshelf importer."""

from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import Book, Item, MediaTypes, Sources, Status
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError
from integrations.imports.audiobookshelf import (
    AudiobookshelfAuthError,
    AudiobookshelfClientError,
    AudiobookshelfImporter,
)
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
        """Only changed progress after the cursor should be imported."""
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
        mock_item.return_value = {
            "media": {"duration": 3_600, "metadata": {"title": "New Book"}},
            "coverPath": "https://img.example/new-book.jpg",
        }

        importer = AudiobookshelfImporter(self.user)
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")
        self.assertEqual(Book.objects.filter(user=self.user).count(), 1)
        self.assertEqual(Book.objects.get(user=self.user).item.title, "New Book")
        self.user.audiobookshelf_account.refresh_from_db()
        self.assertEqual(self.user.audiobookshelf_account.last_sync_ms, 2_000)
        mock_item.assert_called_once_with("new-item")

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
