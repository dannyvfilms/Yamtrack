"""Tests for the Storyteller importer."""

from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import Book, Item, MediaTypes, Sources, Status
from app.providers import services
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError
from integrations.imports.storyteller import (
    StorytellerAuthError,
    StorytellerImporter,
)
from integrations.models import StorytellerAccount


def _position(progression, timestamp=None):
    """Build a Storyteller position payload with a given total progression."""
    payload = {"locator": {"locations": {"totalProgression": progression}}}
    if timestamp is not None:
        payload["timestamp"] = timestamp
    return payload


class StorytellerImporterTests(TestCase):
    """Validate Storyteller import mapping and filtering."""

    def setUp(self):
        """Create a test user and connected Storyteller account."""
        self.user = get_user_model().objects.create_user(
            username="storyteller-user",
            password="pass",  # noqa: S106
        )
        StorytellerAccount.objects.create(
            user=self.user,
            server_url="https://storyteller.example.com",
            auth_token=helpers.encrypt("token"),
        )

    @patch("integrations.imports.storyteller.StorytellerClient.get_position")
    @patch("integrations.imports.storyteller.StorytellerClient.get_books")
    def test_imports_in_progress_book_as_percent_without_page_count(
        self,
        mock_books,
        mock_position,
    ):
        """Without a page count, in-progress progress is stored as whole percent."""
        mock_books.return_value = [{"uuid": "abc", "title": "The Hobbit"}]
        mock_position.return_value = _position(0.5)

        counts, warnings = StorytellerImporter(self.user).import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")

        media = Book.objects.get(user=self.user)
        self.assertEqual(media.status, Status.IN_PROGRESS.value)
        self.assertEqual(media.progress, 50)
        self.assertIsNone(media.end_date)
        self.assertEqual(media.item.source, Sources.STORYTELLER.value)
        self.assertEqual(media.item.media_type, MediaTypes.BOOK.value)
        self.assertEqual(media.item.title, "The Hobbit")

    @patch("integrations.imports.storyteller.StorytellerClient.get_position")
    @patch("integrations.imports.storyteller.StorytellerClient.get_books")
    def test_uses_embedded_position_pagecount_status_and_filters_narrators(
        self,
        mock_books,
        mock_position,
    ):
        """The books list payload (position, pages, status, series) is used directly."""
        mock_books.return_value = [
            {
                "uuid": "425033e0",
                "title": "Storm Front",
                "authors": [
                    {"name": "Jim Butcher"},
                    {"name": "James Marsters"},
                ],
                "narrators": [{"name": "James Marsters"}],
                "series": [
                    {"name": "The Dresden Files", "featured": True, "position": 1},
                ],
                "tags": [{"name": "Fantasy"}, {"name": "Mystery"}],
                "status": {"name": "Read"},
                "ebook": {"pageCount": 234},
                "position": {
                    "locator": {"locations": {"totalProgression": 0.9904}},
                    "timestamp": 1767473562139,
                },
            },
        ]

        counts, warnings = StorytellerImporter(self.user).import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")
        # The embedded position means the per-book endpoint is never hit.
        mock_position.assert_not_called()

        media = Book.objects.get(user=self.user)
        self.assertEqual(media.status, Status.COMPLETED.value)
        self.assertEqual(media.item.number_of_pages, 234)
        self.assertEqual(media.progress, 234)
        # Narrator (James Marsters) is filtered out of the author list.
        self.assertEqual(media.item.authors, ["Jim Butcher"])
        self.assertEqual(media.item.series_name, "The Dresden Files")
        self.assertEqual(media.item.series_position, 1)
        self.assertIn("Fantasy", media.item.genres)

    @patch("integrations.imports.storyteller.StorytellerClient.get_position")
    @patch("integrations.imports.storyteller.StorytellerClient.get_books")
    def test_marks_book_completed_at_threshold(self, mock_books, mock_position):
        """Reaching the finished threshold marks the book as completed."""
        finished_ms = 1_700_003_600_000
        mock_books.return_value = [{"uuid": "done", "title": "Dune"}]
        mock_position.return_value = _position(0.97, timestamp=finished_ms)

        counts, warnings = StorytellerImporter(self.user).import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")

        media = Book.objects.get(user=self.user)
        self.assertEqual(media.status, Status.COMPLETED.value)
        self.assertEqual(media.progress, 100)
        self.assertEqual(
            media.end_date,
            datetime.fromtimestamp(finished_ms / 1000, tz=UTC),
        )

    @patch("integrations.imports.storyteller.StorytellerClient.get_position")
    @patch("integrations.imports.storyteller.StorytellerClient.get_books")
    def test_skips_books_without_progress(self, mock_books, mock_position):
        """Books with no position or zero progress are not imported."""
        mock_books.return_value = [
            {"uuid": "not-started", "title": "Unread"},
            {"uuid": "no-position", "title": "Also Unread"},
        ]
        mock_position.side_effect = [_position(0.0), None]

        counts, warnings = StorytellerImporter(self.user).import_data()

        self.assertEqual(counts, {})
        self.assertEqual(warnings, "")
        self.assertFalse(Book.objects.filter(user=self.user).exists())
        self.assertFalse(
            Item.objects.filter(source=Sources.STORYTELLER.value).exists(),
        )

    @patch("integrations.imports.storyteller.services.get_media_metadata")
    @patch("integrations.imports.storyteller.services.search")
    @patch("integrations.imports.storyteller.StorytellerClient.get_position")
    @patch("integrations.imports.storyteller.StorytellerClient.get_books")
    def test_resolves_to_provider_item_and_computes_progress_in_pages(
        self,
        mock_books,
        mock_position,
        mock_search,
        mock_get_media_metadata,
    ):
        """A matched book is stored under the real provider with page-based progress."""
        mock_books.return_value = [
            {"uuid": "mb", "title": "Mistborn", "authors": ["Brandon Sanderson"]},
        ]
        mock_position.return_value = _position(0.5)
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
            "max_progress": 540,
            "genres": ["Fantasy"],
            "details": {
                "author": "Brandon Sanderson",
                "publisher": "Tor",
                "isbn": ["9780765311788"],
            },
        }

        importer = StorytellerImporter(self.user)
        importer.enable_provider_enrichment = True
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")

        media = Book.objects.get(user=self.user)
        self.assertEqual(media.status, Status.IN_PROGRESS.value)
        self.assertEqual(media.progress, 270)
        # The item is keyed on the real provider, not a synthetic storyteller id.
        self.assertEqual(media.item.source, Sources.HARDCOVER.value)
        self.assertEqual(media.item.media_id, "314")
        self.assertEqual(media.item.number_of_pages, 540)
        self.assertEqual(media.item.image, "https://covers.example/mistborn.jpg")
        self.assertEqual(media.item.publishers, "Tor")
        self.assertEqual(media.item.genres, ["Fantasy"])
        self.assertFalse(
            Item.objects.filter(source=Sources.STORYTELLER.value).exists(),
        )

    @patch("integrations.imports.storyteller.services.get_media_metadata")
    @patch("integrations.imports.storyteller.services.search")
    @patch("integrations.imports.storyteller.StorytellerClient.get_position")
    @patch("integrations.imports.storyteller.StorytellerClient.get_books")
    def test_uses_author_to_reject_same_title_wrong_author(
        self,
        mock_books,
        mock_position,
        mock_search,
        mock_get_media_metadata,
    ):
        """A same-title book by a different author must not be matched."""
        mock_books.return_value = [
            {"uuid": "sf", "title": "Storm Front", "authors": ["Jim Butcher"]},
        ]
        mock_position.return_value = _position(0.4)
        # Search lists the wrong-author book first.
        mock_search.return_value = {
            "results": [
                {"media_id": "castle", "title": "Storm Front"},
                {"media_id": "butcher", "title": "Storm Front"},
            ],
        }
        metadata_by_id = {
            "castle": {
                "media_id": "castle",
                "source": Sources.HARDCOVER.value,
                "title": "Storm Front",
                "max_progress": 300,
                "details": {"author": "Richard Castle"},
            },
            "butcher": {
                "media_id": "butcher",
                "source": Sources.HARDCOVER.value,
                "title": "Storm Front",
                "max_progress": 320,
                "details": {"author": "Jim Butcher"},
            },
        }
        mock_get_media_metadata.side_effect = (
            lambda _mt, media_id, _src: metadata_by_id[media_id]
        )

        importer = StorytellerImporter(self.user)
        importer.enable_provider_enrichment = True
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")
        media = Book.objects.get(user=self.user)
        self.assertEqual(media.item.media_id, "butcher")
        self.assertEqual(media.progress, 128)

    @patch("integrations.imports.storyteller.services.get_media_metadata")
    @patch("integrations.imports.storyteller.services.search")
    @patch("integrations.imports.storyteller.StorytellerClient.get_position")
    @patch("integrations.imports.storyteller.StorytellerClient.get_books")
    def test_reuses_existing_library_item_without_duplicating(
        self,
        mock_books,
        mock_position,
        mock_search,
        mock_get_media_metadata,
    ):
        """A book already in the library is updated in place, not duplicated."""
        existing_item = Item.objects.create(
            media_id="133778",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Memories of Ice",
            image="https://covers.example/moi.jpg",
            number_of_pages=900,
        )
        Book.objects.create(
            user=self.user,
            item=existing_item,
            status=Status.PLANNING.value,
            progress=0,
        )

        mock_books.return_value = [{"uuid": "moi", "title": "Memories of Ice"}]
        mock_position.return_value = _position(0.5)
        mock_search.return_value = {
            "results": [
                {
                    "media_id": "133778",
                    "source": Sources.HARDCOVER.value,
                    "title": "Memories of Ice",
                },
            ],
        }
        mock_get_media_metadata.return_value = {
            "media_id": "133778",
            "source": Sources.HARDCOVER.value,
            "media_type": MediaTypes.BOOK.value,
            "title": "Memories of Ice",
            "max_progress": 900,
            "details": {},
        }

        importer = StorytellerImporter(self.user)
        importer.enable_provider_enrichment = True
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")
        # No duplicate item or book row was created.
        self.assertEqual(
            Item.objects.filter(media_type=MediaTypes.BOOK.value).count(),
            1,
        )
        self.assertEqual(Book.objects.filter(user=self.user).count(), 1)
        media = Book.objects.get(user=self.user)
        self.assertEqual(media.item_id, existing_item.id)
        self.assertEqual(media.status, Status.IN_PROGRESS.value)
        self.assertEqual(media.progress, 450)

    @patch("integrations.imports.storyteller.services.get_media_metadata")
    @patch("integrations.imports.storyteller.services.search")
    @patch("integrations.imports.storyteller.StorytellerClient.get_position")
    @patch("integrations.imports.storyteller.StorytellerClient.get_books")
    def test_library_match_wins_over_a_different_provider(
        self,
        mock_books,
        mock_position,
        mock_search,
        mock_get_media_metadata,
    ):
        """An existing library copy is reused over a different provider match."""
        existing_item = Item.objects.create(
            media_id="999",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="Storm Front",
            authors=["Jim Butcher"],
            image="https://covers.example/sf.jpg",
            number_of_pages=320,
        )
        Book.objects.create(
            user=self.user,
            item=existing_item,
            status=Status.PLANNING.value,
            progress=0,
        )

        mock_books.return_value = [
            {
                "uuid": "sf",
                "title": "Storm Front",
                "authors": [{"name": "Jim Butcher"}],
            },
        ]
        mock_position.return_value = _position(0.5)
        # A provider would resolve to a different (OpenLibrary) edition.
        mock_search.return_value = {
            "results": [{"media_id": "OL44227744M", "title": "Storm Front"}],
        }
        mock_get_media_metadata.return_value = {
            "media_id": "OL44227744M",
            "source": Sources.OPENLIBRARY.value,
            "title": "Storm Front",
            "max_progress": 320,
            "details": {"author": "Jim Butcher"},
        }

        importer = StorytellerImporter(self.user)
        importer.enable_provider_enrichment = True
        counts, warnings = importer.import_data()

        self.assertEqual(counts.get(MediaTypes.BOOK.value), 1)
        self.assertEqual(warnings, "")
        # The OpenLibrary edition must not be created; the library copy is reused.
        self.assertFalse(
            Item.objects.filter(source=Sources.OPENLIBRARY.value).exists(),
        )
        self.assertEqual(
            Item.objects.filter(media_type=MediaTypes.BOOK.value).count(),
            1,
        )
        mock_search.assert_not_called()
        self.assertEqual(Book.objects.filter(user=self.user).count(), 1)
        media = Book.objects.get(user=self.user)
        self.assertEqual(media.item_id, existing_item.id)
        self.assertEqual(media.status, Status.IN_PROGRESS.value)
        self.assertEqual(media.progress, 160)

    @patch("integrations.imports.storyteller.StorytellerClient.get_position")
    @patch("integrations.imports.storyteller.StorytellerClient.get_books")
    def test_preserves_start_date_across_syncs(self, mock_books, mock_position):
        """Re-syncing an in-progress book should not overwrite the start date."""
        mock_books.return_value = [{"uuid": "abc", "title": "The Hobbit"}]
        mock_position.return_value = _position(0.3)
        StorytellerImporter(self.user).import_data()
        first_start = Book.objects.get(user=self.user).start_date

        mock_position.return_value = _position(0.6)
        StorytellerImporter(self.user).import_data()
        media = Book.objects.get(user=self.user)

        self.assertEqual(media.start_date, first_start)
        self.assertEqual(media.progress, 60)

    def test_storyteller_source_book_resolves_from_local_item(self):
        """get_media_metadata for a storyteller item reads local data, no network."""
        Item.objects.create(
            media_id="6e302b6bfe709e009329",
            source=Sources.STORYTELLER.value,
            media_type=MediaTypes.BOOK.value,
            title="Memories of Ice",
            image="https://covers.example/moi.jpg",
            number_of_pages=900,
            authors=["Steven Erikson"],
        )

        metadata = services.get_media_metadata(
            MediaTypes.BOOK.value,
            "6e302b6bfe709e009329",
            Sources.STORYTELLER.value,
        )

        self.assertEqual(metadata["title"], "Memories of Ice")
        self.assertEqual(metadata["source"], Sources.STORYTELLER.value)
        self.assertEqual(metadata["max_progress"], 900)
        self.assertEqual(metadata["image"], "https://covers.example/moi.jpg")

    @patch("integrations.imports.storyteller.StorytellerClient.get_books")
    def test_marks_connection_broken_on_auth_error(self, mock_books):
        """Auth failures mark the account broken and raise an import error."""
        mock_books.side_effect = StorytellerAuthError(
            "Storyteller token is invalid or expired",
        )

        with self.assertRaises(MediaImportError):
            StorytellerImporter(self.user).import_data()

        self.user.storyteller_account.refresh_from_db()
        self.assertTrue(self.user.storyteller_account.connection_broken)
        self.assertIn(
            "invalid or expired",
            self.user.storyteller_account.last_error_message,
        )
