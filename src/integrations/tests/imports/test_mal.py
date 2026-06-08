import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    Anime,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Sources,
    Status,
)
from integrations.imports import (
    helpers,
    mal,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


class ImportMAL(TestCase):
    """Test importing media from MyAnimeList."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    @patch("requests.Session.get")
    def test_import_animelist(self, mock_request):
        """Basic test importing anime and manga from MyAnimeList."""
        with Path(mock_path / "import_mal_anime.json").open() as file:
            anime_response = json.load(file)
        with Path(mock_path / "import_mal_manga.json").open() as file:
            manga_response = json.load(file)

        anime_mock = MagicMock()
        anime_mock.json.return_value = anime_response
        manga_mock = MagicMock()
        manga_mock.json.return_value = manga_response
        mock_request.side_effect = [anime_mock, manga_mock]

        mal.importer("bloodthirstiness", self.user, "new")
        self.assertEqual(Anime.objects.filter(user=self.user).count(), 5)
        self.assertEqual(Manga.objects.filter(user=self.user).count(), 3)

        self.assertEqual(
            Anime.objects.filter(
                user=self.user,
                item__title="Ama Gli Animali",
            )
            .first()
            .item.image,
            settings.IMG_NONE,
        )
        self.assertEqual(
            Anime.objects.get(user=self.user, item__title="FLCL").status,
            Status.PAUSED.value,
        )
        self.assertEqual(
            Manga.objects.get(user=self.user, item__title="Fire Punch").score,
            7,
        )

        self.assertEqual(
            Anime.objects.filter(
                user=self.user,
                item__title="Chainsaw Man",
            )
            .first()
            .history.first()
            .history_date,
            datetime(2022, 12, 28, 19, 20, 54, tzinfo=UTC),
        )

    def test_user_not_found(self):
        """Test that an error is raised if the user is not found."""
        self.assertRaises(
            helpers.MediaImportError,
            mal.importer,
            "fhdsufdsu",
            self.user,
            "new",
        )

    @patch("app.models.media.Media.process_status")
    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data")
    @patch("requests.Session.get")
    def test_overwrite_syncs_to_existing_movie_entry(
        self, mock_request, mock_mapping, _mock_ps
    ):
        """MAL overwrite import also updates a corresponding Movie entry."""
        # MAL ID 32253 ("Ama Gli Animali", completed, score=7) maps to TMDB movie 99999
        mock_mapping.return_value = {"tmdb_movie:99999": {"mal:32253": {}}}

        movie_item = Item.objects.create(
            media_id="99999",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Ama Gli Animali",
            image="http://example.com/img.jpg",
        )
        Movie.objects.create(
            item=movie_item,
            user=self.user,
            progress=1,
            status=Status.COMPLETED.value,
            score=5,  # stale score — should be overwritten
        )

        with Path(mock_path / "import_mal_anime.json").open() as file:
            anime_response = json.load(file)
        with Path(mock_path / "import_mal_manga.json").open() as file:
            manga_response = json.load(file)

        anime_mock = MagicMock()
        anime_mock.json.return_value = anime_response
        manga_mock = MagicMock()
        manga_mock.json.return_value = manga_response
        mock_request.side_effect = [anime_mock, manga_mock]

        mal.importer("bloodthirstiness", self.user, "overwrite")

        movie = Movie.objects.get(item=movie_item, user=self.user)
        self.assertEqual(int(movie.score), 7)  # updated from MAL (was 5)
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)  # is_rewatching=True
