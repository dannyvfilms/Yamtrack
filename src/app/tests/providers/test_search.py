from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from app.models import MediaTypes, Sources
from app.providers import (
    hardcover,
    igdb,
    mal,
    mangaupdates,
    openlibrary,
    services,
    tmdb,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"


class Search(TestCase):
    """Test the external API calls for media search."""

    def test_anime(self):
        """Test the search method for anime.

        Assert that all required keys are present in each entry.
        """
        response = mal.search(MediaTypes.ANIME.value, "Cowboy Bebop", 1)

        required_keys = {"media_id", "media_type", "title", "image"}

        for anime in response["results"]:
            self.assertTrue(all(key in anime for key in required_keys))

    def test_anime_not_found(self):
        """Test the search method for anime with no results."""
        response = mal.search(MediaTypes.ANIME.value, "q", 1)

        self.assertEqual(response["results"], [])

    def test_mangaupdates(self):
        """Test the search method for manga.

        Assert that all required keys are present in each entry.
        """
        response = mangaupdates.search("One Piece", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for manga in response["results"]:
            self.assertTrue(all(key in manga for key in required_keys))

    def test_manga_not_found(self):
        """Test the search method for manga with no results."""
        response = mangaupdates.search("", 1)

        self.assertEqual(response["results"], [])

    def test_tv(self):
        """Test the search method for TV shows.

        Assert that all required keys are present in each entry.
        """
        response = tmdb.search(MediaTypes.TV.value, "Breaking Bad", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for tv in response["results"]:
            self.assertTrue(all(key in tv for key in required_keys))

    def test_games(self):
        """Test the search method for games.

        Assert that all required keys are present in each entry.
        """
        response = igdb.search("Persona 5", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for game in response["results"]:
            self.assertTrue(all(key in game for key in required_keys))

    def test_books(self):
        """Test the search method for books.

        Assert that all required keys are present in each entry.
        """
        response = openlibrary.search("The Name of the Wind", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for book in response["results"]:
            self.assertTrue(all(key in book for key in required_keys))

    def test_comics(self):
        """Test the search method for comics.

        Assert that all required keys are present in each entry.
        """
        response = igdb.search("Batman", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        for comic in response["results"]:
            self.assertTrue(all(key in comic for key in required_keys))

    def test_hardcover(self):
        """Test the search method for books from Hardcover.

        Assert that all required keys are present in each entry.
        """
        response = hardcover.search("1984 George Orwell", 1)
        required_keys = {"media_id", "media_type", "title", "image"}

        self.assertTrue(len(response["results"]) > 0)

        for book in response["results"]:
            self.assertTrue(all(key in book for key in required_keys))

    def test_hardcover_not_found(self):
        """Test the search method for books from Hardcover with no results."""
        response = hardcover.search("xjkqzptmvnsieurytowahdbfglc", 1)
        self.assertEqual(response["results"], [])


class SearchById(TestCase):
    """Test direct ID lookup via services.search_by_id and services.search."""

    def _make_metadata(self, media_type, source, media_id="238", title="Test Title"):
        return {
            "media_id": media_id,
            "source": source,
            "media_type": media_type,
            "title": title,
            "original_title": title,
            "localized_title": title,
            "image": "http://example.com/img.jpg",
            "max_progress": 1,
            "synopsis": "",
            "genres": [],
            "score": None,
            "score_count": None,
            "details": {},
            "related": {},
        }

    @patch("app.providers.tmdb.movie")
    def test_movie_by_tmdb_id(self, mock_movie):
        """search_by_id returns a single movie when query is a TMDB numeric ID."""
        mock_movie.return_value = self._make_metadata(
            MediaTypes.MOVIE.value, Sources.TMDB.value, "238", "The Godfather"
        )
        result = services.search_by_id(MediaTypes.MOVIE.value, "238")
        self.assertIsNotNone(result)
        self.assertEqual(result["total_results"], 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["media_id"], "238")
        self.assertEqual(result["results"][0]["title"], "The Godfather")
        mock_movie.assert_called_once_with(238)

    @patch("app.providers.tmdb.tv")
    def test_tv_by_tmdb_id(self, mock_tv):
        """search_by_id returns a single TV show when query is a TMDB numeric ID."""
        mock_tv.return_value = self._make_metadata(
            MediaTypes.TV.value, Sources.TMDB.value, "1396", "Breaking Bad"
        )
        result = services.search_by_id(MediaTypes.TV.value, "1396")
        self.assertIsNotNone(result)
        self.assertEqual(result["total_results"], 1)
        self.assertEqual(result["results"][0]["media_id"], "1396")
        mock_tv.assert_called_once_with(1396)

    @patch("app.providers.tmdb.tv")
    def test_season_type_uses_tv_lookup(self, mock_tv):
        """search_by_id for season media type looks up the parent TV show."""
        mock_tv.return_value = self._make_metadata(
            MediaTypes.TV.value, Sources.TMDB.value, "1396", "Breaking Bad"
        )
        result = services.search_by_id(MediaTypes.SEASON.value, "1396")
        self.assertIsNotNone(result)
        mock_tv.assert_called_once_with(1396)

    @patch("app.providers.mal.anime")
    def test_anime_by_mal_id(self, mock_anime):
        """search_by_id returns a single anime when query is a MAL numeric ID."""
        mock_anime.return_value = self._make_metadata(
            MediaTypes.ANIME.value, Sources.MAL.value, "1", "Cowboy Bebop"
        )
        result = services.search_by_id(MediaTypes.ANIME.value, "1")
        self.assertIsNotNone(result)
        self.assertEqual(result["results"][0]["media_id"], "1")
        mock_anime.assert_called_once_with(1)

    @patch("app.providers.igdb.game")
    def test_game_by_igdb_id(self, mock_game):
        """search_by_id returns a single game when query is an IGDB numeric ID."""
        mock_game.return_value = self._make_metadata(
            MediaTypes.GAME.value, Sources.IGDB.value, "119", "Minecraft"
        )
        result = services.search_by_id(MediaTypes.GAME.value, "119")
        self.assertIsNotNone(result)
        self.assertEqual(result["results"][0]["title"], "Minecraft")
        mock_game.assert_called_once_with(119)

    @patch("app.providers.openlibrary.book")
    def test_book_by_openlibrary_id(self, mock_book):
        """search_by_id returns a single book when query is an OL ID."""
        mock_book.return_value = self._make_metadata(
            MediaTypes.BOOK.value, Sources.OPENLIBRARY.value, "OL7353617M", "Some Book"
        )
        result = services.search_by_id(
            MediaTypes.BOOK.value, "OL7353617M", Sources.OPENLIBRARY.value
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["results"][0]["media_id"], "OL7353617M")
        mock_book.assert_called_once_with("OL7353617M")

    @patch("app.providers.hardcover.book")
    def test_book_by_hardcover_numeric_id(self, mock_book):
        """search_by_id returns a single book when query is a Hardcover numeric ID."""
        mock_book.return_value = self._make_metadata(
            MediaTypes.BOOK.value, Sources.HARDCOVER.value, "42", "Dune"
        )
        result = services.search_by_id(
            MediaTypes.BOOK.value, "42", Sources.HARDCOVER.value
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["results"][0]["title"], "Dune")
        mock_book.assert_called_once_with(42)

    def test_non_id_title_query_returns_none(self):
        """search_by_id returns None for a plain-text (non-ID) query."""
        result = services.search_by_id(MediaTypes.MOVIE.value, "The Godfather")
        self.assertIsNone(result)

    def test_ol_id_without_correct_media_type_returns_none(self):
        """An OL-format query for a non-book media type returns None."""
        result = services.search_by_id(MediaTypes.ANIME.value, "OL7353617M")
        self.assertIsNone(result)

    def test_numeric_id_with_provider_error_falls_back_to_none(self):
        """search_by_id returns None when the provider raises an exception."""
        with patch("app.providers.tmdb.movie", side_effect=Exception("API error")):
            result = services.search_by_id(MediaTypes.MOVIE.value, "99999999")
        self.assertIsNone(result)

    @patch("app.providers.tmdb.movie")
    def test_search_uses_id_lookup_on_page_1(self, mock_movie):
        """services.search() returns ID-lookup result on page 1 for a numeric query."""
        mock_movie.return_value = self._make_metadata(
            MediaTypes.MOVIE.value, Sources.TMDB.value, "238", "The Godfather"
        )
        result = services.search(MediaTypes.MOVIE.value, "238", 1, Sources.TMDB.value)
        self.assertEqual(result["total_results"], 1)
        self.assertEqual(result["results"][0]["media_id"], "238")

    @patch("app.providers.tmdb.search")
    @patch("app.providers.tmdb.movie")
    def test_search_skips_id_lookup_on_page_2(self, mock_movie, mock_tmdb_search):
        """services.search() skips ID lookup and uses text search on page 2."""
        mock_tmdb_search.return_value = {
            "page": 2,
            "total_results": 0,
            "total_pages": 1,
            "results": [],
        }
        services.search(MediaTypes.MOVIE.value, "238", 2, Sources.TMDB.value)
        mock_movie.assert_not_called()
        mock_tmdb_search.assert_called_once()
