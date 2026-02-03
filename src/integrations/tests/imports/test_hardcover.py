from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import Book, Status
from integrations.imports import hardcover

mock_path = Path(__file__).resolve().parent.parent / "mock_data"


class ImportHardcover(TestCase):
    """Test importing media from Hardcover CSV."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        def mock_get_media_metadata(media_type, media_id, source):
            title_map = {
                "1149853": "Alchemised",
                "999999": "DNF Book",
            }
            return {
                "media_id": media_id,
                "title": title_map.get(str(media_id), "Unknown"),
                "image": "https://example.com/cover.jpg",
            }

        def mock_search(media_type, query, page, source):
            return {
                "results": [
                    {
                        "media_id": "2222",
                        "source": source,
                        "media_type": media_type,
                        "title": "Mistborn: The Final Empire",
                        "image": "https://example.com/mistborn.jpg",
                    },
                ],
            }

        with patch(
            "integrations.imports.hardcover.services.get_media_metadata",
            side_effect=mock_get_media_metadata,
        ), patch(
            "integrations.imports.hardcover.services.search",
            side_effect=mock_search,
        ):
            with Path(mock_path / "import_hardcover.csv").open("rb") as file:
                self.import_results = hardcover.importer(file, self.user, "new")

    def test_import_counts(self):
        """Test basic counts of imported books."""
        self.assertEqual(Book.objects.filter(user=self.user).count(), 3)

    def test_status_mapping(self):
        """Test status mapping from Hardcover CSV."""
        planning = Book.objects.get(item__title="Alchemised")
        completed = Book.objects.get(item__title="Mistborn: The Final Empire")
        dropped = Book.objects.get(item__title="DNF Book")

        self.assertEqual(planning.status, Status.PLANNING.value)
        self.assertEqual(completed.status, Status.COMPLETED.value)
        self.assertEqual(dropped.status, Status.DROPPED.value)

    def test_progress_and_rating(self):
        """Test progress and rating parsing."""
        completed = Book.objects.get(item__title="Mistborn: The Final Empire")
        dropped = Book.objects.get(item__title="DNF Book")

        self.assertEqual(completed.progress, 864)
        self.assertEqual(completed.score, 10.0)
        self.assertEqual(dropped.progress, 0)

    def test_notes_prefer_private(self):
        """Private notes should override review text."""
        completed = Book.objects.get(item__title="Mistborn: The Final Empire")
        self.assertEqual(completed.notes, "I heard about this book")
