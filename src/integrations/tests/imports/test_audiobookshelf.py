"""Tests for Audiobookshelf importer."""

from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import Book, Item, MediaTypes, Sources, Status
from integrations.imports import helpers
from integrations.imports.audiobookshelf import AudiobookshelfImporter
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
