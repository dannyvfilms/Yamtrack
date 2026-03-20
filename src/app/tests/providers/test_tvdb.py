from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from app.models import MediaTypes, Sources
from app.providers import tvdb


class TVDBProviderTests(TestCase):
    """Tests for TVDB metadata normalization and caching."""

    def setUp(self):
        cache.clear()

    @override_settings(TVDB_API_KEY="test-tvdb-key", TVDB_PIN="1234")
    @patch("app.providers.tvdb.services.api_request")
    def test_get_token_caches_login_response(self, mock_api_request):
        """TVDB login should cache the bearer token between requests."""
        mock_api_request.return_value = {"data": {"token": "cached-token"}}

        first = tvdb._get_token()
        second = tvdb._get_token()

        self.assertEqual(first, "cached-token")
        self.assertEqual(second, "cached-token")
        mock_api_request.assert_called_once()

    @patch("app.providers.tvdb._request")
    def test_tv_normalizes_series_metadata(self, mock_request):
        """Series metadata should normalize title fields, links, and seasons."""
        mock_request.return_value = {
            "data": {
                "id": 81189,
                "name": {"language": "eng", "name": "Breaking Bad"},
                "originalName": {"language": "eng", "name": "Breaking Bad"},
                "overview": "Chemistry teacher becomes kingpin.",
                "firstAired": "2008-01-20",
                "lastAired": "2013-09-29",
                "numberOfEpisodes": 62,
                "averageRuntime": 47,
                "status": {"name": "Ended"},
                "siteRating": "9.5",
                "siteRatingCount": "1000",
                "score": 859244,
                "remoteIds": [
                    {"sourceName": "TheMovieDB.com", "id": "1396"},
                    {"sourceName": "IMDb", "id": "tt0903747"},
                ],
                "seasons": [
                    {
                        "id": 101,
                        "number": 0,
                        "name": "Specials",
                        "type": {"name": "Aired Order"},
                        "episodes": [],
                    },
                    {
                        "id": 102,
                        "number": 1,
                        "name": "Season 1",
                        "type": {"name": "Aired Order"},
                        "episodes": [
                            {"aired": "2008-01-20"},
                            {"aired": "2008-01-27"},
                        ],
                    },
                ],
                "genres": [{"name": "Drama"}],
                "characters": [],
            },
        }

        result = tvdb.tv("81189")

        self.assertEqual(result["title"], "Breaking Bad")
        self.assertEqual(result["tvdb_id"], "81189")
        self.assertEqual(result["provider_external_ids"]["tmdb_id"], "1396")
        self.assertEqual(result["provider_external_ids"]["imdb_id"], "tt0903747")
        self.assertEqual(result["details"]["status"], "Ended")
        self.assertEqual(result["details"]["episodes"], 62)
        self.assertEqual(result["score"], 9.5)
        self.assertEqual(result["score_count"], 1000)
        self.assertEqual(result["related"]["seasons"][0]["season_number"], 0)
        self.assertIn("episode_count", result["related"]["seasons"][0])
        self.assertIn("details", result["related"]["seasons"][0])

    @patch("app.providers.tvdb._request")
    def test_tv_prefers_english_translation_payload_for_titles_and_synopsis(
        self,
        mock_request,
    ):
        """Series metadata should prefer English translation payloads when available."""
        mock_request.side_effect = [
            {
                "data": {
                    "id": 259640,
                    "name": {"language": "jpn", "name": "ソードアート・オンライン"},
                    "originalName": {"language": "jpn", "name": "ソードアート・オンライン"},
                    "overview": "日本語の概要",
                    "firstAired": "2012-07-08",
                    "status": {"name": "Ended"},
                    "seasons": [],
                    "characters": [],
                },
            },
            {
                "data": {
                    "name": "Sword Art Online",
                    "overview": "English overview",
                    "language": "eng",
                },
            },
        ]

        result = tvdb.tv("259640")

        self.assertEqual(result["title"], "Sword Art Online")
        self.assertEqual(result["localized_title"], "Sword Art Online")
        self.assertEqual(result["original_title"], "ソードアート・オンライン")
        self.assertEqual(result["synopsis"], "English overview")

    @patch("app.providers.tvdb.tv")
    @patch("app.providers.tvdb._request")
    def test_tv_with_seasons_normalizes_specials_episode_rows(
        self,
        mock_request,
        mock_tv,
    ):
        """Season payloads should normalize specials and episode rows."""
        mock_tv.return_value = {
            "media_id": "81189",
            "source": Sources.TVDB.value,
            "media_type": MediaTypes.TV.value,
            "title": "Breaking Bad",
            "original_title": "Breaking Bad",
            "localized_title": "Breaking Bad",
            "image": "https://example.com/show.jpg",
            "synopsis": "Chemistry teacher becomes kingpin.",
            "details": {"episodes": 62},
            "related": {"seasons": [{"season_number": 0}]},
            "external_links": {
                "TVDB": "https://www.thetvdb.com/dereferrer/series/81189",
            },
        }
        mock_request.side_effect = [
            {
                "data": {
                    "id": 81189,
                    "name": "Breaking Bad",
                    "seasons": [
                        {
                            "id": 101,
                            "number": 0,
                            "name": "Specials",
                            "type": {"name": "Aired Order"},
                        },
                    ],
                },
            },
            {"data": {}},
            {
                "data": {
                    "id": 101,
                    "number": 0,
                    "name": "Specials",
                    "type": {"name": "Aired Order"},
                    "episodes": [
                        {
                            "number": 1,
                            "aired": "2009-02-17T03:00:00+00:00",
                            "name": "Special 1",
                            "overview": "Behind the scenes.",
                            "image": "https://example.com/special1.jpg",
                            "runtime": 12,
                        },
                    ],
                },
            },
            {"data": {}},
        ]

        result = tvdb.tv_with_seasons("81189", [0])

        self.assertEqual(result["season/0"]["season_title"], "Specials")
        self.assertEqual(result["season/0"]["episodes"][0]["episode_number"], 1)
        self.assertEqual(
            result["season/0"]["episodes"][0]["air_date"].isoformat(),
            "2009-02-17T03:00:00+00:00",
        )
        self.assertEqual(
            result["season/0"]["episodes"][0]["image"],
            "https://example.com/special1.jpg",
        )

    @patch("app.providers.tvdb._request")
    def test_search_prefers_english_translation_rows(self, mock_request):
        """Search results should prefer English names from translation arrays."""
        mock_request.return_value = {
            "data": [
                {
                    "id": 259640,
                    "name": {"language": "jpn", "name": "ソードアート・オンライン"},
                    "translations": {
                        "name": [
                            {"language": "jpn", "name": "ソードアート・オンライン"},
                            {"language": "eng", "name": "Sword Art Online"},
                        ],
                    },
                    "firstAired": "2012-07-08",
                },
            ],
        }

        result = tvdb.search(MediaTypes.ANIME.value, "sword art online", 1)

        self.assertEqual(result["results"][0]["title"], "Sword Art Online")
        self.assertEqual(result["results"][0]["localized_title"], "Sword Art Online")

    @patch("app.providers.tvdb.tv")
    @patch("app.providers.tvdb.tv_with_seasons")
    def test_episode_returns_tmdb_compatible_episode_payload(
        self,
        mock_tv_with_seasons,
        mock_tv,
    ):
        """Episode lookups should produce TMDB-compatible title fields."""
        mock_tv.return_value = {
            "title": "Breaking Bad",
            "original_title": "Breaking Bad",
            "localized_title": "Breaking Bad",
        }
        mock_tv_with_seasons.return_value = {
            "season/1": {
                "title": "Breaking Bad",
                "original_title": "Breaking Bad",
                "localized_title": "Breaking Bad",
                "season_title": "Season 1",
                "episodes": [
                    {
                        "episode_number": 1,
                        "name": "Pilot",
                        "image": "https://example.com/pilot.jpg",
                    },
                ],
            },
        }

        result = tvdb.episode("81189", 1, 1)

        self.assertEqual(result["title"], "Breaking Bad")
        self.assertEqual(result["season_title"], "Season 1")
        self.assertEqual(result["episode_title"], "Pilot")
        self.assertEqual(result["image"], "https://example.com/pilot.jpg")

    @patch("app.providers.tvdb._request")
    def test_get_episode_airstamp_map_caches_precise_episode_times(self, mock_request):
        """Default-order episode maps should cache normalized airstamps."""
        mock_request.return_value = {
            "data": {
                "episodes": [
                    {
                        "seasonNumber": 1,
                        "number": 1,
                        "aired": "2008-01-20T22:00:00+00:00",
                    },
                    {
                        "seasonNumber": 1,
                        "number": 2,
                        "aired": "2008-01-27T22:00:00+00:00",
                    },
                ],
            },
        }

        first = tvdb.get_episode_airstamp_map("81189")
        second = tvdb.get_episode_airstamp_map("81189")

        self.assertEqual(first["1_1"], "2008-01-20T22:00:00+00:00")
        self.assertEqual(second, first)
        mock_request.assert_called_once()
