from unittest.mock import patch

from django.test import TestCase

from app.discover.providers.tmdb_adapter import TMDbDiscoverAdapter
from app.models import DiscoverApiCache, MediaTypes


class TMDbDiscoverAdapterTests(TestCase):
    """Tests for TMDb Discover adapter cache behavior."""

    def setUp(self):
        DiscoverApiCache.objects.all().delete()

    @patch("app.discover.providers.tmdb_adapter.services.api_request")
    def test_trending_uses_db_cache_after_first_fetch(self, mock_api_request):
        def _side_effect(_provider, _method, url, params=None):
            if url.endswith("/genre/movie/list"):
                return {"genres": [{"id": 28, "name": "Action"}]}
            if url.endswith("/trending/movie/day"):
                return {
                    "results": [
                        {
                            "id": 11,
                            "title": "Mock Movie",
                            "poster_path": "/poster.jpg",
                            "genre_ids": [28],
                            "popularity": 10.0,
                            "vote_average": 7.5,
                            "vote_count": 100,
                            "release_date": "2025-01-01",
                        },
                    ],
                }
            raise AssertionError(f"Unexpected URL called: {url}")

        mock_api_request.side_effect = _side_effect

        adapter = TMDbDiscoverAdapter()
        first = adapter.trending(MediaTypes.MOVIE.value, limit=10)
        second = adapter.trending(MediaTypes.MOVIE.value, limit=10)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0].title, "Mock Movie")
        self.assertEqual(mock_api_request.call_count, 2)
