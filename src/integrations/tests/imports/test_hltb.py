from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    Game,
    Status,
)
from integrations.imports import (
    hltb,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


class ImportHowLongToBeat(TestCase):
    """Test importing media from HowLongToBeat CSV."""

    def setUp(self):
        """Create user for the tests."""
        self.search_patcher = patch("app.providers.services.search")
        self.mock_search = self.search_patcher.start()
        self.addCleanup(self.search_patcher.stop)
        self.mock_search.return_value = {
            "results": [
                {
                    "media_id": "re7",
                    "title": "Resident Evil 7: Biohazard",
                    "image": "",
                },
            ],
        }
        self.credentials = {"username": "test", "password": "***"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        with Path(mock_path / "import_hltb_game.csv").open("rb") as file:
            self.import_results = hltb.importer(file, self.user, "new")

    def test_import_counts(self):
        """Test basic counts of imported games."""
        self.assertEqual(Game.objects.filter(user=self.user).count(), 1)

    def test_historical_records(self):
        """Test historical records creation during import."""
        game = Game.objects.filter(user=self.user).first()
        self.assertEqual(game.history.count(), 1)
        self.assertEqual(
            game.history.first().history_date,
            datetime(2024, 2, 9, 15, 54, 48, tzinfo=UTC),
        )

    def test_import_old_csv_without_start_date(self):
        """Test old HLTB exports without Start Date can still import."""
        self.mock_search.return_value = {
            "results": [
                {
                    "media_id": "2048",
                    "title": "2048",
                    "image": "",
                },
            ],
        }
        user = get_user_model().objects.create_user(username="old-hltb")

        with Path(mock_path / "import_hltb_game_missing_start_date.csv").open(
            "rb",
        ) as file:
            hltb.importer(file, user, "new")

        game = Game.objects.get(user=user, item__media_id="2048")
        self.assertIsNone(game.start_date)
        self.assertIsNone(game.end_date)
        self.assertEqual(game.status, Status.DROPPED.value)
