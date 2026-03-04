from unittest.mock import patch

from django.test import TestCase

from app.discover.providers.trakt_adapter import TraktDiscoverAdapter
from app.models import DiscoverApiCache, MediaTypes, Sources


class TraktDiscoverAdapterTests(TestCase):
    """Tests for Trakt Discover adapter cache behavior."""

    def setUp(self):
        DiscoverApiCache.objects.all().delete()

    @patch("app.discover.providers.trakt_adapter.services.api_request")
    def test_movie_watched_weekly_uses_db_cache_after_first_fetch(self, mock_api_request):
        mock_api_request.return_value = [
            {
                "watcher_count": 1200,
                "movie": {
                    "title": "Weekly Hit",
                    "released": "2025-11-01",
                    "genres": ["action", "thriller"],
                    "rating": 7.9,
                    "votes": 3200,
                    "ids": {"tmdb": 12345},
                },
            },
            {
                "watcher_count": 600,
                "movie": {
                    "title": "Missing TMDb ID",
                    "ids": {"tmdb": None},
                },
            },
        ]

        adapter = TraktDiscoverAdapter()
        first = adapter.movie_watched_weekly(limit=25)
        second = adapter.movie_watched_weekly(limit=25)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0].title, "Weekly Hit")
        self.assertEqual(first[0].media_type, MediaTypes.MOVIE.value)
        self.assertEqual(first[0].source, Sources.TMDB.value)
        self.assertEqual(first[0].release_date, "2025-11-01")
        self.assertEqual(mock_api_request.call_count, 1)

    @patch("app.discover.providers.trakt_adapter.services.api_request")
    def test_movie_popular_uses_db_cache_after_first_fetch(self, mock_api_request):
        mock_api_request.return_value = [
            {
                "title": "Popular Hit",
                "released": "2024-08-01",
                "genres": ["drama"],
                "rating": 8.1,
                "votes": 5400,
                "ids": {"tmdb": 54321},
            },
            {
                "title": "No TMDb",
                "ids": {"tmdb": None},
            },
        ]

        adapter = TraktDiscoverAdapter()
        first = adapter.movie_popular(page=1, limit=25)
        second = adapter.movie_popular(page=1, limit=25)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0].title, "Popular Hit")
        self.assertEqual(first[0].media_type, MediaTypes.MOVIE.value)
        self.assertEqual(first[0].source, Sources.TMDB.value)
        self.assertEqual(first[0].release_date, "2024-08-01")
        self.assertEqual(mock_api_request.call_count, 1)

    @patch("app.discover.providers.trakt_adapter.services.api_request")
    def test_movie_anticipated_uses_db_cache_after_first_fetch(self, mock_api_request):
        mock_api_request.return_value = [
            {
                "list_count": 900,
                "movie": {
                    "title": "Anticipated Hit",
                    "released": "2025-12-12",
                    "genres": ["sci-fi"],
                    "rating": 7.7,
                    "votes": 2100,
                    "ids": {"tmdb": 67890},
                },
            },
            {
                "list_count": 800,
                "movie": {
                    "title": "No TMDb",
                    "ids": {"tmdb": None},
                },
            },
        ]

        adapter = TraktDiscoverAdapter()
        first = adapter.movie_anticipated(page=1, limit=25)
        second = adapter.movie_anticipated(page=1, limit=25)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0].title, "Anticipated Hit")
        self.assertEqual(first[0].media_type, MediaTypes.MOVIE.value)
        self.assertEqual(first[0].source, Sources.TMDB.value)
        self.assertEqual(first[0].release_date, "2025-12-12")
        self.assertEqual(mock_api_request.call_count, 1)
