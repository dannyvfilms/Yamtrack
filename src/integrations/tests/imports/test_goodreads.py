from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    Book,
    Status,
)
from integrations.imports import (
    goodreads,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


class ImportGoodreads(TestCase):
    """Test importing media from GoodReads CSV."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        with Path(mock_path / "import_goodreads.csv").open("rb") as file:
            self.import_results = goodreads.importer(file, self.user, "new")

    def test_import_counts(self):
        """Test basic counts of imported books."""
        self.assertEqual(Book.objects.filter(user=self.user).count(), 3)

    def test_historical_records(self):
        """Test historical records creation during import."""
        book = Book.objects.filter(user=self.user).first()
        self.assertEqual(book.history.count(), 1)

    def test_stored_progress(self):
        """Test progress of imported books."""
        read_book = Book.objects.get(status=Status.COMPLETED.value)
        self.assertEqual(read_book.status, Status.COMPLETED.value)
        self.assertEqual(read_book.progress, 994)

        read_book = Book.objects.get(status=Status.IN_PROGRESS.value)
        self.assertEqual(read_book.status, Status.IN_PROGRESS.value)
        self.assertEqual(read_book.progress, 0)

    def test_unknown_shelf_defaults_to_planning(self):
        """Unknown shelf values should default to planning instead of failing import."""
        headers = [
            "Book Id",
            "Title",
            "Author",
            "ISBN13",
            "My Rating",
            "Number of Pages",
            "Exclusive Shelf",
            "Date Added",
            "Date Read",
            "Private Notes",
        ]
        csv_payload = (
            ",".join(headers)
            + "\n"
            "1,Book with Unknown Shelf,Author,9780000000001,0,320,owned,,,\n"
        )

        resolved_book = {
            "media_id": "1001",
            "title": "Book with Unknown Shelf",
            "image": "",
        }
        with patch.object(
            goodreads.GoodReadsImporter,
            "_search_book",
            return_value=resolved_book,
        ):
            goodreads.importer(BytesIO(csv_payload.encode("utf-8")), self.user, "new")

        imported_book = Book.objects.get(user=self.user, item__media_id="1001")
        self.assertEqual(imported_book.status, Status.PLANNING.value)

    def test_import_handles_missing_goodreads_dates(self):
        """Rows without Goodreads date fields should not crash import."""
        headers = [
            "Book Id",
            "Title",
            "Author",
            "ISBN13",
            "My Rating",
            "Number of Pages",
            "Exclusive Shelf",
            "Date Added",
            "Date Read",
            "Private Notes",
        ]
        csv_payload = (
            ",".join(headers)
            + "\n"
            "2,Date-less Book,Author,9780000000002,4,220,read,,,\n"
        )

        resolved_book = {"media_id": "1002", "title": "Date-less Book", "image": ""}
        with patch.object(
            goodreads.GoodReadsImporter,
            "_search_book",
            return_value=resolved_book,
        ):
            goodreads.importer(BytesIO(csv_payload.encode("utf-8")), self.user, "new")

        imported_book = Book.objects.get(user=self.user, item__media_id="1002")
        self.assertEqual(imported_book.status, Status.COMPLETED.value)
        self.assertEqual(imported_book.progress, 220)
