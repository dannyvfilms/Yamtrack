import asyncio
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings

from app.credits import _normalize_credit_rows
from app.models import Episode, Item, MediaTypes, Sources
from app.providers import (
    comicvine,
    hardcover,
    igdb,
    mal,
    mangaupdates,
    manual,
    openlibrary,
    services,
    tmdb,
    tvdb,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"


class Metadata(TestCase):
    """Test the external API calls for media details."""

    def test_anime(self):
        """Test the metadata method for anime."""
        response = mal.anime("1")
        self.assertEqual(response["title"], "Cowboy Bebop")
        self.assertEqual(response["details"]["start_date"], "1998-04-03")
        self.assertEqual(response["details"]["status"], "Finished")
        self.assertEqual(response["details"]["episodes"], 26)

    @patch("requests.Session.get")
    def test_anime_unknown(self, mock_data):
        """Test the metadata method for anime with mostly unknown data."""
        with Path(mock_path / "metadata_anime_unknown.json").open() as file:
            anime_response = json.load(file)
        mock_data.return_value.json.return_value = anime_response
        mock_data.return_value.status_code = 200

        # anime without picture, synopsis, duration, or number of episodes
        response = mal.anime("0")
        self.assertEqual(response["title"], "Unknown Example")
        self.assertEqual(response["image"], settings.IMG_NONE)
        self.assertEqual(response["synopsis"], "No synopsis available.")
        self.assertEqual(response["details"]["episodes"], None)
        self.assertEqual(response["details"]["runtime"], None)

    def test_manga(self):
        """Test the metadata method for manga."""
        response = mal.manga("1")
        self.assertEqual(response["title"], "Monster")
        self.assertEqual(response["details"]["start_date"], "1994-12-05")
        self.assertEqual(response["details"]["status"], "Finished")
        self.assertEqual(response["details"]["number_of_chapters"], 162)

    def test_mangaupdates(self):
        """Test the metadata method for manga from mangaupdates."""
        response = mangaupdates.manga("72274276213")
        self.assertEqual(response["title"], "Monster")
        self.assertEqual(response["details"]["year"], "1994")
        self.assertEqual(response["details"]["format"], "Manga")

    def test_tv(self):
        """Test the metadata method for TV shows."""
        response = tmdb.tv("1396")
        self.assertEqual(response["title"], "Breaking Bad")
        self.assertEqual(response["details"]["first_air_date"].date().isoformat(), "2008-01-20")
        self.assertEqual(response["details"]["status"], "Ended")
        self.assertEqual(response["details"]["episodes"], 62)

    def test_tmdb_original_title_does_not_backfill_from_random_alternative_when_original_exists(self):
        response = {
            "title": "The Sound of Music",
            "original_title": "The Sound of Music",
            "alternative_titles": {
                "titles": [
                    {"iso_3166_1": "JP", "title": "サウンド・オブ・ミュージック"},
                ],
            },
        }

        self.assertEqual(tmdb.get_original_title(response), "The Sound of Music")

    @patch("app.providers.tvdb.build_specials_season")
    @patch("app.providers.tmdb.services.api_request")
    @override_settings(TVDB_API_KEY="test-tvdb-key")
    def test_tv_with_seasons_adds_specials_when_tmdb_lacks_season_zero(
        self,
        mock_api_request,
        mock_build_specials_season,
    ):
        """TV details should synthesize season 0 only from TVDB-linked fallback data."""
        tmdb.cache.clear()
        mock_build_specials_season.return_value = {
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.SEASON.value,
            "media_id": "114410",
            "title": "Chainsaw Man",
            "original_title": "Chainsaw Man",
            "localized_title": "Chainsaw Man",
            "season_title": "Specials",
            "season_number": 0,
            "max_progress": 1,
            "image": "https://example.com/s0.jpg",
            "synopsis": "TVDB-only special.",
            "details": {"episodes": 1},
            "episodes": [
                {
                    "episode_number": 1,
                    "air_date": "2022-10-04T00:00:00+00:00",
                    "still_path": None,
                    "image": "https://example.com/s0e1.jpg",
                    "name": "Special 1",
                    "overview": "TVDB-only special.",
                    "runtime": 12,
                },
            ],
            "providers": {},
            "source_url": "https://www.thetvdb.com/dereferrer/series/10196540",
            "tvdb_id": "10196540",
            "external_links": {
                "TVDB": "https://www.thetvdb.com/dereferrer/series/10196540",
            },
        }

        def _mock_api_request(source, _method, url, params=None):  # noqa: ARG001
            if source == Sources.TMDB.value and url.endswith("/tv/114410"):
                return {
                    "id": 114410,
                    "name": "Chainsaw Man",
                    "original_name": "Chainsaw Man",
                    "poster_path": "/chainsaw.jpg",
                    "overview": "A test show",
                    "genres": [],
                    "vote_average": 8.4,
                    "vote_count": 10,
                    "production_companies": [],
                    "production_countries": [],
                    "spoken_languages": [],
                    "recommendations": {"results": []},
                    "external_ids": {"tvdb_id": "10196540"},
                    "watch/providers": {"results": {}},
                    "aggregate_credits": {"cast": [], "crew": []},
                    "alternative_titles": {"results": []},
                    "episode_run_time": [24],
                    "first_air_date": "2022-10-12",
                    "last_air_date": "2022-12-28",
                    "status": "Returning Series",
                    "number_of_seasons": 1,
                    "number_of_episodes": 12,
                    "seasons": [
                        {
                            "season_number": 1,
                            "name": "Season 1",
                            "air_date": "2022-10-12",
                            "episode_count": 12,
                            "poster_path": None,
                        },
                    ],
                }

            raise AssertionError(f"Unexpected request in test: {source} {url}")

        mock_api_request.side_effect = _mock_api_request

        result = tmdb.tv_with_seasons("114410", [0])
        processed_episodes = tmdb.process_episodes(result["season/0"], [])

        self.assertEqual(result["season/0"]["season_title"], "Specials")
        self.assertEqual(result["season/0"]["details"]["episodes"], 1)
        self.assertEqual(
            result["season/0"]["source_url"],
            "https://www.thetvdb.com/dereferrer/series/10196540",
        )
        self.assertTrue(
            any(
                season.get("season_number") == 0
                for season in result["related"]["seasons"]
            ),
        )
        self.assertEqual(processed_episodes[0]["title"], "Special 1")
        self.assertEqual(
            processed_episodes[0]["image"],
            "https://example.com/s0e1.jpg",
        )
        self.assertEqual(
            processed_episodes[0]["air_date"].date().isoformat(),
            "2022-10-04",
        )
        mock_build_specials_season.assert_called_once()

    @patch("app.providers.tvdb.build_specials_season")
    @patch("app.providers.tmdb.services.api_request")
    @override_settings(TVDB_API_KEY="")
    def test_tv_with_seasons_skips_specials_fallback_when_tvdb_unconfigured(
        self,
        mock_api_request,
        mock_build_specials_season,
    ):
        """TMDB TV details should not invoke TVDB specials fallback when disabled."""
        tmdb.cache.clear()

        def _mock_api_request(source, _method, url, params=None):  # noqa: ARG001
            if source == Sources.TMDB.value and url.endswith("/tv/114410"):
                return {
                    "id": 114410,
                    "name": "Chainsaw Man",
                    "original_name": "Chainsaw Man",
                    "poster_path": "/chainsaw.jpg",
                    "overview": "A test show",
                    "genres": [],
                    "vote_average": 8.4,
                    "vote_count": 10,
                    "production_companies": [],
                    "production_countries": [],
                    "spoken_languages": [],
                    "recommendations": {"results": []},
                    "external_ids": {"tvdb_id": "10196540"},
                    "watch/providers": {"results": {}},
                    "aggregate_credits": {"cast": [], "crew": []},
                    "alternative_titles": {"results": []},
                    "episode_run_time": [24],
                    "first_air_date": "2022-10-12",
                    "last_air_date": "2022-12-28",
                    "status": "Returning Series",
                    "number_of_seasons": 1,
                    "number_of_episodes": 12,
                    "seasons": [
                        {
                            "season_number": 1,
                            "name": "Season 1",
                            "air_date": "2022-10-12",
                            "episode_count": 12,
                            "poster_path": None,
                        },
                    ],
                }

            raise AssertionError(f"Unexpected request in test: {source} {url}")

        mock_api_request.side_effect = _mock_api_request

        result = tmdb.tv_with_seasons("114410", [0])

        self.assertEqual(result["tvdb_id"], "10196540")
        self.assertNotIn("season/0", result)
        mock_build_specials_season.assert_not_called()

    @patch("app.providers.tvdb.build_specials_season")
    @patch("app.providers.tmdb.services.api_request")
    @override_settings(TVDB_API_KEY="test-tvdb-key")
    def test_tv_with_seasons_ignores_missing_tvdb_specials_payload(
        self,
        mock_api_request,
        mock_build_specials_season,
    ):
        """Missing TVDB specials payloads should not crash season 0 fallback."""
        tmdb.cache.clear()
        mock_build_specials_season.return_value = None

        def _mock_api_request(source, _method, url, params=None):  # noqa: ARG001
            if source == Sources.TMDB.value and url.endswith("/tv/114410"):
                return {
                    "id": 114410,
                    "name": "Chainsaw Man",
                    "original_name": "Chainsaw Man",
                    "poster_path": "/chainsaw.jpg",
                    "overview": "A test show",
                    "genres": [],
                    "vote_average": 8.4,
                    "vote_count": 10,
                    "production_companies": [],
                    "production_countries": [],
                    "spoken_languages": [],
                    "recommendations": {"results": []},
                    "external_ids": {"tvdb_id": "10196540"},
                    "watch/providers": {"results": {}},
                    "aggregate_credits": {"cast": [], "crew": []},
                    "alternative_titles": {"results": []},
                    "episode_run_time": [24],
                    "first_air_date": "2022-10-12",
                    "last_air_date": "2022-12-28",
                    "status": "Returning Series",
                    "number_of_seasons": 1,
                    "number_of_episodes": 12,
                    "seasons": [
                        {
                            "season_number": 1,
                            "name": "Season 1",
                            "air_date": "2022-10-12",
                            "episode_count": 12,
                            "poster_path": None,
                        },
                    ],
                }

            raise AssertionError(f"Unexpected request in test: {source} {url}")

        mock_api_request.side_effect = _mock_api_request

        result = tmdb.tv_with_seasons("114410", [0])

        self.assertEqual(result["tvdb_id"], "10196540")
        self.assertNotIn("season/0", result)
        mock_build_specials_season.assert_called_once()

    @patch("app.providers.tvdb.build_specials_season")
    @patch("app.providers.tmdb.services.api_request")
    @override_settings(TVDB_API_KEY="test-tvdb-key")
    def test_tv_with_seasons_normalizes_string_season_zero(
        self,
        mock_api_request,
        mock_build_specials_season,
    ):
        """String season numbers from routes should still trigger specials fallback."""
        tmdb.cache.clear()
        mock_build_specials_season.return_value = {
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.SEASON.value,
            "media_id": "114410",
            "title": "Chainsaw Man",
            "original_title": "Chainsaw Man",
            "localized_title": "Chainsaw Man",
            "season_title": "Specials",
            "season_number": 0,
            "max_progress": 1,
            "image": "https://example.com/s0.jpg",
            "synopsis": "TVDB-only special.",
            "details": {"episodes": 1},
            "episodes": [
                {
                    "episode_number": 1,
                    "air_date": "2022-10-04T00:00:00+00:00",
                    "still_path": None,
                    "image": "https://example.com/s0e1.jpg",
                    "name": "Special 1",
                    "overview": "TVDB-only special.",
                    "runtime": 12,
                },
            ],
            "providers": {},
            "source_url": "https://www.thetvdb.com/dereferrer/series/10196540",
            "tvdb_id": "10196540",
            "external_links": {
                "TVDB": "https://www.thetvdb.com/dereferrer/series/10196540",
            },
        }

        def _mock_api_request(source, _method, url, params=None):  # noqa: ARG001
            if source == Sources.TMDB.value and url.endswith("/tv/114410"):
                return {
                    "id": 114410,
                    "name": "Chainsaw Man",
                    "original_name": "Chainsaw Man",
                    "poster_path": "/chainsaw.jpg",
                    "overview": "A test show",
                    "genres": [],
                    "vote_average": 8.4,
                    "vote_count": 10,
                    "production_companies": [],
                    "production_countries": [],
                    "spoken_languages": [],
                    "recommendations": {"results": []},
                    "external_ids": {"tvdb_id": "10196540"},
                    "watch/providers": {"results": {}},
                    "aggregate_credits": {"cast": [], "crew": []},
                    "alternative_titles": {"results": []},
                    "episode_run_time": [24],
                    "first_air_date": "2022-10-12",
                    "last_air_date": "2022-12-28",
                    "status": "Returning Series",
                    "number_of_seasons": 1,
                    "number_of_episodes": 12,
                    "seasons": [
                        {
                            "season_number": 1,
                            "name": "Season 1",
                            "air_date": "2022-10-12",
                            "episode_count": 12,
                            "poster_path": None,
                        },
                    ],
                }

            raise AssertionError(f"Unexpected request in test: {source} {url}")

        mock_api_request.side_effect = _mock_api_request

        result = tmdb.tv_with_seasons("114410", ["0"])

        self.assertEqual(result["season/0"]["season_title"], "Specials")
        self.assertEqual(result["season/0"]["max_progress"], 1)
        mock_build_specials_season.assert_called_once()

    @patch("app.providers.tvdb._request")
    def test_tvdb_episode_map_normalizes_precise_airstamps(
        self,
        mock_request,
    ):
        """TVDB episode maps should prefer precise default-order airstamps."""
        cache.clear()
        mock_request.return_value = {
            "data": {
                "episodes": [
                    {
                        "seasonNumber": 1,
                        "number": 1,
                        "aired": "2022-10-04T22:00:00+00:00",
                    },
                ],
            },
        }

        result = tvdb.get_episode_airstamp_map("10196540")

        self.assertEqual(result["1_1"], "2022-10-04T22:00:00+00:00")
        mock_request.assert_called_once()

    @patch("app.providers.tmdb.timezone.localdate")
    @patch("app.providers.tmdb.services.api_request")
    def test_tv_changes(self, mock_api_request, mock_localdate):
        """Test fetching changed TV ids from TMDB."""
        mock_localdate.return_value = date(2026, 4, 5)
        mock_api_request.return_value = {
            "results": [{"id": 1}, {"id": 2}],
            "total_pages": 1,
        }

        result = tmdb.tv_changes()

        self.assertEqual(result, {"1", "2"})
        _, kwargs = mock_api_request.call_args
        self.assertEqual(kwargs["params"]["start_date"], "2026-04-02")
        self.assertEqual(kwargs["params"]["end_date"], "2026-04-05")
        self.assertEqual(kwargs["params"]["page"], 1)

    @patch("app.providers.tmdb.timezone.localdate")
    @patch("app.providers.tmdb.services.api_request")
    def test_tv_changes_across_pages(self, mock_api_request, mock_localdate):
        """Test TMDB TV changes pagination and deduplication."""
        mock_localdate.return_value = date(2026, 4, 5)
        mock_api_request.side_effect = [
            {
                "results": [{"id": 1}, {"id": 2}],
                "total_pages": 2,
            },
            {
                "results": [{"id": 2}, {"id": 3}],
                "total_pages": 2,
            },
        ]

        result = tmdb.tv_changes()

        self.assertEqual(result, {"1", "2", "3"})
        self.assertEqual(mock_api_request.call_count, 2)

    @patch("app.providers.tmdb.timezone.localdate")
    @patch("app.providers.tmdb.services.api_request")
    def test_movie_changes(self, mock_api_request, mock_localdate):
        """Test fetching changed movie ids from TMDB."""
        mock_localdate.return_value = date(2026, 4, 5)
        mock_api_request.return_value = {
            "results": [{"id": 10}, {"id": 20}],
            "total_pages": 1,
        }

        result = tmdb.movie_changes()

        self.assertEqual(result, {"10", "20"})
        _, kwargs = mock_api_request.call_args
        self.assertEqual(kwargs["params"]["start_date"], "2026-04-02")
        self.assertEqual(kwargs["params"]["end_date"], "2026-04-05")
        self.assertEqual(kwargs["params"]["page"], 1)

    @patch("app.providers.tmdb.timezone.localdate")
    @patch("app.providers.tmdb.services.api_request")
    def test_movie_changes_across_pages(self, mock_api_request, mock_localdate):
        """Test TMDB movie changes pagination and deduplication."""
        mock_localdate.return_value = date(2026, 4, 5)
        mock_api_request.side_effect = [
            {
                "results": [{"id": 10}, {"id": 20}],
                "total_pages": 2,
            },
            {
                "results": [{"id": 20}, {"id": 30}],
                "total_pages": 2,
            },
        ]

        result = tmdb.movie_changes()

        self.assertEqual(result, {"10", "20", "30"})
        self.assertEqual(mock_api_request.call_count, 2)

    def test_tmdb_process_episodes(self):
        """Test the process_episodes function for TMDB episodes."""
        Item.objects.create(
            media_id="proc-5",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Process Episodes Test",
            image="http://example.com/process.jpg",
        )

        Item.objects.create(
            media_id="proc-5",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Process Episodes Test",
            image="http://example.com/process_s1.jpg",
            season_number=1,
        )

        for i in range(1, 4):
            Item.objects.create(
                media_id="proc-5",
                source=Sources.MANUAL.value,
                media_type=MediaTypes.EPISODE.value,
                title=f"Process Episode {i}",
                image=f"http://example.com/process_s1e{i}.jpg",
                season_number=1,
                episode_number=i,
            )

        season_metadata = {
            "media_id": "1396",  # Breaking Bad
            "season_number": 1,
            "episodes": [
                {
                    "episode_number": 1,
                    "air_date": "2008-01-20",
                    "still_path": "/path/to/still1.jpg",
                    "name": "Pilot",
                    "overview": "overview of the episode",
                    "runtime": 23,
                },
                {
                    "episode_number": 2,
                    "air_date": "2008-01-27",
                    "still_path": "/path/to/still2.jpg",
                    "name": "Cat's in the Bag...",
                    "overview": "overview of the episode",
                    "runtime": 23,
                },
                {
                    "episode_number": 3,
                    "air_date": "2008-02-10",
                    "still_path": "/path/to/still3.jpg",
                    "name": "...And the Bag's in the River",
                    "overview": "overview of the episode",
                    "runtime": 23,
                },
            ],
        }
        episode_item_1 = Item.objects.get(
            media_id="proc-5",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
        )
        episode_1 = Episode(item=episode_item_1)

        episode_item_2 = Item.objects.get(
            media_id="proc-5",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
        )
        episode_2 = Episode(item=episode_item_2)

        episodes_in_db = [episode_1, episode_2]

        # Call process_episodes
        result = tmdb.process_episodes(season_metadata, episodes_in_db)

        self.assertEqual(len(result), 3)

        self.assertEqual(result[0]["episode_number"], 1)
        self.assertEqual(result[0]["title"], "Pilot")
        self.assertEqual(result[0]["air_date"].date().isoformat(), "2008-01-20")
        self.assertTrue(result[0]["history"], [episode_1])

        self.assertEqual(result[1]["episode_number"], 2)
        self.assertEqual(result[1]["title"], "Cat's in the Bag...")
        self.assertEqual(result[1]["air_date"].date().isoformat(), "2008-01-27")
        self.assertTrue(result[1]["history"], [episode_2])

        self.assertEqual(result[2]["episode_number"], 3)
        self.assertEqual(result[2]["title"], "...And the Bag's in the River")
        self.assertEqual(result[2]["air_date"].date().isoformat(), "2008-02-10")
        self.assertFalse(result[2]["history"], [])

    @patch("app.providers.tmdb.tv_with_seasons")
    @patch("app.providers.tmdb.services.api_request")
    def test_tmdb_episode(self, mock_api_request, mock_tv_with_seasons):
        """Test the episode method for TMDB episodes."""
        cache_key = f"{Sources.TMDB.value}_{MediaTypes.EPISODE.value}_1396_1_1"
        tmdb.cache.delete(cache_key)

        mock_tv_with_seasons.return_value = {
            "title": "Breaking Bad",
            "season/1": {
                "title": "Breaking Bad",
                "season_title": "Season 1",
                "episodes": [
                    {
                        "episode_number": 1,
                        "name": "Pilot",
                        "still_path": "/path/to/still1.jpg",
                    },
                    {
                        "episode_number": 2,
                        "name": "Cat's in the Bag...",
                        "still_path": "/path/to/still2.jpg",
                    },
                ],
            },
        }
        def _mock_episode_request(_source, _method, url, params=None):  # noqa: ARG001
            if url.endswith("/episode/1"):
                return {
                    "name": "Pilot",
                    "still_path": "/path/to/still1.jpg",
                    "credits": {"cast": [], "crew": []},
                    "guest_stars": [],
                    "crew": [],
                }
            if url.endswith("/episode/3"):
                mock_response = MagicMock()
                mock_response.status_code = 404
                mock_response.text = "Episode not found"
                mock_response.json.return_value = {
                    "status_code": 34,
                    "status_message": "The resource you requested could not be found.",
                }
                error = requests.exceptions.HTTPError("404 Not Found")
                error.response = mock_response
                raise error
            raise AssertionError(f"Unexpected episode URL called in test: {url}")

        mock_api_request.side_effect = _mock_episode_request

        result = tmdb.episode("1396", "1", "1")

        self.assertEqual(result["title"], "Breaking Bad")
        self.assertEqual(result["season_title"], "Season 1")
        self.assertEqual(result["episode_title"], "Pilot")
        self.assertEqual(result["image"], tmdb.get_image_url("/path/to/still1.jpg"))

        with self.assertRaises(services.ProviderAPIError) as cm:
            tmdb.episode("1396", "1", "3")

        self.assertIn("The Movie Database API (HTTP 404)", str(cm.exception))

        mock_tv_with_seasons.assert_called_with("1396", ["1"])

    @patch("app.providers.tmdb.tv_with_seasons")
    @patch("app.providers.tmdb.services.api_request")
    def test_tmdb_episode_prefers_guest_stars_when_present(
        self,
        mock_api_request,
        mock_tv_with_seasons,
    ):
        """Episode cast should prefer guest stars over regular cast when both exist."""
        media_id = "episode-cast-priority"
        season_number = "1"
        episode_number = "1"
        cache_key = (
            f"{Sources.TMDB.value}_{MediaTypes.EPISODE.value}_{media_id}_{season_number}_{episode_number}"
        )
        tmdb.cache.delete(cache_key)

        mock_tv_with_seasons.return_value = {
            "title": "Sample Show",
            "season/1": {
                "title": "Sample Show",
                "season_title": "Season 1",
            },
        }
        mock_api_request.return_value = {
            "name": "Sample Episode",
            "still_path": "/sample.jpg",
            "credits": {
                "cast": [
                    {
                        "id": 11,
                        "name": "Regular Cast",
                        "character": "Lead",
                        "order": 0,
                        "known_for_department": "Acting",
                        "gender": 2,
                    },
                ],
                "crew": [],
            },
            "guest_stars": [
                {
                    "id": 22,
                    "name": "Guest Star",
                    "character": "Guest Role",
                    "order": 500,
                    "known_for_department": "Acting",
                    "gender": 1,
                },
            ],
            "crew": [],
        }

        result = tmdb.episode(media_id, season_number, episode_number)
        cast_names = [row["name"] for row in result["cast"]]

        self.assertEqual(cast_names, ["Guest Star"])

    def test_tmdb_find_next_episode(self):
        """Test the find_next_episode function."""
        episodes_metadata = [
            {"episode_number": 1, "title": "Episode 1"},
            {"episode_number": 2, "title": "Episode 2"},
            {"episode_number": 3, "title": "Episode 3"},
        ]

        next_episode = tmdb.find_next_episode(1, episodes_metadata)
        self.assertEqual(next_episode, 2)

        next_episode = tmdb.find_next_episode(3, episodes_metadata)
        self.assertIsNone(next_episode)

        next_episode = tmdb.find_next_episode(5, episodes_metadata)
        self.assertIsNone(next_episode)

    def test_movie(self):
        """Test the metadata method for movies."""
        response = tmdb.movie("10494")
        self.assertEqual(response["title"], "Perfect Blue")
        self.assertEqual(response["details"]["release_date"].date().isoformat(), "1998-02-28")
        self.assertEqual(response["details"]["status"], "Released")

    @patch("app.providers.tmdb.services.api_request")
    def test_movie_includes_keywords_certification_and_collection_metadata(self, mock_api_request):
        cache_key = f"{Sources.TMDB.value}_{MediaTypes.MOVIE.value}_999"
        tmdb.cache.delete(cache_key)
        mock_api_request.side_effect = [
            {
                "id": 999,
                "title": "Comfort Mystery",
                "original_title": "Comfort Mystery",
                "poster_path": "/comfort.jpg",
                "overview": "A mystery.",
                "genres": [{"id": 1, "name": "Mystery"}],
                "popularity": 77.5,
                "vote_average": 7.7,
                "vote_count": 1200,
                "status": "Released",
                "runtime": 102,
                "production_companies": [{"id": 44, "name": "Pixar Animation Studios", "logo_path": None}],
                "production_countries": [{"iso_3166_1": "US", "name": "United States of America"}],
                "spoken_languages": [{"english_name": "English"}],
                "credits": {"cast": [], "crew": []},
                "recommendations": {"results": []},
                "external_ids": {},
                "watch/providers": {"results": {}},
                "alternative_titles": {"titles": []},
                "keywords": {
                    "keywords": [
                        {"id": 10, "name": "Whodunit"},
                        {"id": 11, "name": "Holiday"},
                    ],
                },
                "release_dates": {
                    "results": [
                        {
                            "iso_3166_1": "US",
                            "release_dates": [{"certification": "PG"}],
                        },
                    ],
                },
                "belongs_to_collection": {"id": 321, "name": "Mystery Collection"},
            },
            {
                "id": 321,
                "name": "Mystery Collection",
                "parts": [],
            },
        ]

        response = tmdb.movie("999")

        self.assertEqual(response["provider_popularity"], 77.5)
        self.assertEqual(response["provider_rating"], 7.7)
        self.assertEqual(response["provider_rating_count"], 1200)
        self.assertEqual(response["provider_keywords"], ["Whodunit", "Holiday"])
        self.assertEqual(response["provider_certification"], "PG")
        self.assertEqual(response["provider_collection_id"], "321")
        self.assertEqual(response["provider_collection_name"], "Mystery Collection")
        self.assertEqual(response["details"]["certification"], "PG")

    @patch("requests.Session.get")
    def test_movie_unknown(self, mock_data):
        """Test the metadata method for movies with mostly unknown data."""
        with Path(mock_path / "metadata_movie_unknown.json").open() as file:
            movie_response = json.load(file)
        mock_data.return_value.json.return_value = movie_response
        mock_data.return_value.status_code = 200

        response = tmdb.movie("0")
        self.assertEqual(response["title"], "Unknown Movie")
        self.assertEqual(response["image"], settings.IMG_NONE)
        self.assertEqual(response["synopsis"], "No synopsis available.")
        self.assertEqual(response["details"]["release_date"], None)
        self.assertEqual(response["details"]["runtime"], None)
        self.assertEqual(response["genres"], None)
        self.assertEqual(response["details"]["studios"], None)
        self.assertEqual(response["details"]["country"], None)
        self.assertEqual(response["details"]["languages"], None)

    def test_games(self):
        """Test the metadata method for games."""
        response = igdb.game("1942")
        self.assertEqual(response["title"], "The Witcher 3: Wild Hunt")
        self.assertEqual(response["details"]["format"], "Main game")
        self.assertEqual(response["details"]["release_date"], "2015-05-19")
        self.assertEqual(
            response["details"]["themes"],
            ["Action", "Fantasy", "Open world"],
        )

    def test_game_non_numeric_id_raises_value_error(self):
        """Non-numeric IGDB IDs should raise ValueError before any API call."""
        with self.assertRaises(ValueError, msg="IGDB game IDs must be numeric"):
            igdb.game("game-123")

    def test_external_game_steam(self):
        """Test the external_game method for Steam games."""
        igdb_game_id = igdb.external_game("292030", igdb.ExternalGameSource.STEAM)

        self.assertEqual(igdb_game_id, 1942)

    def test_external_game_not_found(self):
        """Test the external_game method with non-existent Steam ID."""
        igdb_game_id = igdb.external_game("999999999", igdb.ExternalGameSource.STEAM)

        self.assertIsNone(igdb_game_id)

    def test_book(self):
        """Test the metadata method for books."""
        response = openlibrary.book("OL21733390M")
        self.assertEqual(response["title"], "Nineteen Eighty-Four")
        self.assertEqual(response["details"]["author"], ["George Orwell"])

    def test_comic(self):
        """Test the metadata method for comics."""
        response = comicvine.comic("155969")
        self.assertEqual(response["title"], "Ultimate Spider-Man")

    def test_hardcover_book(self):
        """Test the metadata method for books from Hardcover."""
        response = hardcover.book("377193")
        self.assertEqual(response["title"], "The Great Gatsby")
        self.assertEqual(response["details"]["author"], "F. Scott Fitzgerald")
        self.assertIn("Fiction", response["genres"])
        self.assertIn("Young Adult", response["genres"])
        self.assertIn("Classics", response["genres"])
        self.assertAlmostEqual(response["score"], 7.4, delta=0.1)

    def test_hardcover_book_unknown(self):
        """Test the metadata method for books from Hardcover with minimal data."""
        response = hardcover.book("1265528")
        self.assertEqual(response["title"], "MiNRS")
        self.assertEqual(response["details"]["author"], "Kevin Sylvester")
        self.assertEqual(response["details"]["publish_date"], "2015-09-22")
        # These fields should be None or default values
        self.assertEqual(response["synopsis"], "No synopsis available.")
        self.assertEqual(response["details"]["format"], "Unknown")
        self.assertIsNone(response["genres"])

    def test_hardcover_get_authors_full(self):
        authors = hardcover.get_authors_full(
            [
                {
                    "contribution": "Author",
                    "author": {
                        "id": 1,
                        "name": "Author One",
                        "cached_image": "http://example.com/a1.jpg",
                    },
                },
                {
                    "contribution": "Author",
                    "author": {
                        "id": 2,
                        "name": "Author Two",
                    },
                },
            ],
        )

        self.assertEqual(len(authors), 2)
        self.assertEqual(authors[0]["person_id"], "1")
        self.assertEqual(authors[0]["name"], "Author One")
        self.assertEqual(authors[0]["role"], "Author")

    @patch("app.providers.hardcover.services.api_request")
    def test_hardcover_author_profile_normalization(self, mock_api_request):
        hardcover.cache.delete(f"{Sources.HARDCOVER.value}_person_77")
        mock_api_request.return_value = {
            "data": {
                "authors_by_pk": {
                    "id": 77,
                    "name": "Hardcover Author",
                    "bio": "Hardcover bio",
                    "cached_image": "http://example.com/author.jpg",
                    "born_date": "1970-01-01",
                    "death_date": None,
                    "location": "London",
                    "contributions": [
                        {
                            "contribution": "Author",
                            "book": {
                                "id": 7001,
                                "title": "Hardcover Book",
                                "release_date": "2001-01-01",
                                "cached_image": "http://example.com/book.jpg",
                            },
                        },
                    ],
                },
            },
        }

        response = hardcover.author_profile("77")

        self.assertEqual(response["person_id"], "77")
        self.assertEqual(response["source"], Sources.HARDCOVER.value)
        self.assertEqual(response["name"], "Hardcover Author")
        self.assertEqual(response["known_for_department"], "Author")
        self.assertEqual(response["bibliography"][0]["media_id"], "7001")
        self.assertEqual(response["bibliography"][0]["media_type"], MediaTypes.BOOK.value)

    @patch("app.providers.openlibrary.fetch_author_data")
    def test_openlibrary_get_authors_full(self, mock_fetch_author_data):
        mock_fetch_author_data.side_effect = [
            {
                "person_id": "OL1A",
                "name": "Open Author",
                "image": "http://example.com/ol-author.jpg",
                "role": "Author",
                "sort_order": 0,
            },
        ]

        authors_full = asyncio.run(
            openlibrary.get_authors_full(
                {"authors": [{"author": {"key": "/authors/OL1A"}}]},
            ),
        )

        self.assertEqual(len(authors_full), 1)
        self.assertEqual(authors_full[0]["person_id"], "OL1A")
        self.assertEqual(authors_full[0]["name"], "Open Author")

    @patch("app.providers.openlibrary.services.api_request")
    def test_openlibrary_author_profile_normalization(self, mock_api_request):
        openlibrary.cache.delete(f"{Sources.OPENLIBRARY.value}_person_OL1A")

        def _mock_api_request(_source, _method, url, params=None, **kwargs):  # noqa: ARG001
            if url.endswith("/authors/OL1A.json"):
                return {
                    "name": "Open Author",
                    "bio": {"value": "Open bio"},
                    "photos": [1234],
                    "birth_date": "1940-01-01",
                    "death_date": None,
                }
            if url.endswith("/authors/OL1A/works.json"):
                return {
                    "entries": [
                        {
                            "key": "/works/OL1W",
                            "title": "Work One",
                            "first_publish_year": 1950,
                        },
                        {
                            "key": "/works/OL2W",
                            "title": "Work Two",
                        },
                    ],
                }
            if url.endswith("/works/OL1W/editions.json"):
                return {"entries": [{"key": "/books/OL123M"}]}
            if url.endswith("/works/OL2W/editions.json"):
                return {"entries": []}
            raise AssertionError(f"Unexpected URL in test: {url}")

        mock_api_request.side_effect = _mock_api_request
        response = openlibrary.author_profile("OL1A")

        self.assertEqual(response["person_id"], "OL1A")
        self.assertEqual(response["source"], Sources.OPENLIBRARY.value)
        self.assertEqual(response["name"], "Open Author")
        self.assertEqual(response["biography"], "Open bio")
        self.assertEqual(len(response["bibliography"]), 1)
        self.assertEqual(response["bibliography"][0]["media_id"], "OL123M")
        self.assertEqual(response["bibliography"][0]["media_type"], MediaTypes.BOOK.value)

    def test_comicvine_get_people_full_writer_only(self):
        people = comicvine.get_people_full(
            {
                "people": [
                    {"id": 1, "name": "Writer One", "role": "writer"},
                    {"id": 2, "name": "Artist One", "role": "artist"},
                    {"id": 3, "name": "Story Lead", "role": "story"},
                ],
            },
        )

        self.assertEqual(len(people), 2)
        self.assertEqual([person["person_id"] for person in people], ["1", "3"])

    @patch("app.providers.comicvine.services.api_request")
    def test_comicvine_person_profile_normalization(self, mock_api_request):
        comicvine.cache.delete(f"{Sources.COMICVINE.value}_person_44")
        mock_api_request.return_value = {
            "results": {
                "id": 44,
                "name": "Comic Writer",
                "deck": "Short bio",
                "image": {"medium_url": "http://example.com/cv-author.jpg"},
                "birth": "1970-01-01",
                "death": None,
                "hometown": "New York",
            },
        }

        response = comicvine.person_profile("44")

        self.assertEqual(response["person_id"], "44")
        self.assertEqual(response["source"], Sources.COMICVINE.value)
        self.assertEqual(response["name"], "Comic Writer")
        self.assertEqual(response["known_for_department"], "Writing")
        self.assertEqual(response["bibliography"], [])

    def test_mangaupdates_get_authors_full(self):
        authors = mangaupdates.get_authors_full(
            [
                {"id": 10, "name": "Mangaka One", "type": "Author"},
                {"id": 11, "name": "Mangaka Two", "type": "Artist"},
            ],
        )

        self.assertEqual(len(authors), 2)
        self.assertEqual(authors[0]["person_id"], "10")
        self.assertEqual(authors[0]["role"], "Author")

    @patch("app.providers.mangaupdates.services.api_request")
    def test_mangaupdates_author_profile_normalization(self, mock_api_request):
        mangaupdates.cache.delete(f"{Sources.MANGAUPDATES.value}_person_55")
        mock_api_request.return_value = {
            "id": 55,
            "name": "Manga Author",
            "description": "Manga bio",
            "image": {"url": {"original": "http://example.com/mu-author.jpg"}},
            "series_list": [
                {"series_id": 777, "title": "Series One", "year": "2010"},
            ],
        }

        response = mangaupdates.author_profile("55")

        self.assertEqual(response["person_id"], "55")
        self.assertEqual(response["source"], Sources.MANGAUPDATES.value)
        self.assertEqual(response["name"], "Manga Author")
        self.assertEqual(response["known_for_department"], "Author")
        self.assertEqual(response["bibliography"][0]["media_id"], "777")
        self.assertEqual(response["bibliography"][0]["media_type"], MediaTypes.MANGA.value)

    def test_manual_tv(self):
        """Test the metadata method for manually created TV shows."""
        Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Manual TV Show",
            image="http://example.com/manual.jpg",
        )

        Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Manual TV Show",
            image="http://example.com/manual_s1.jpg",
            season_number=1,
        )

        for i in range(1, 4):
            Item.objects.create(
                media_id="1",
                source=Sources.MANUAL.value,
                media_type=MediaTypes.EPISODE.value,
                title=f"Episode {i}",
                image=f"http://example.com/manual_s1e{i}.jpg",
                season_number=1,
                episode_number=i,
            )

        response = manual.metadata("1", MediaTypes.TV.value)

        self.assertEqual(response["title"], "Manual TV Show")
        self.assertEqual(response["media_id"], "1")
        self.assertEqual(response["source"], Sources.MANUAL.value)
        self.assertEqual(response["media_type"], MediaTypes.TV.value)
        self.assertEqual(response["synopsis"], "No synopsis available.")

        self.assertEqual(response["details"]["seasons"], 1)
        self.assertEqual(response["details"]["episodes"], 3)
        self.assertEqual(response["max_progress"], 3)
        self.assertEqual(len(response["related"]["seasons"]), 1)

        season_data = response["season/1"]
        self.assertEqual(season_data["season_number"], 1)
        self.assertEqual(season_data["max_progress"], 3)
        self.assertEqual(len(season_data["episodes"]), 3)

    def test_manual_movie(self):
        """Test the metadata method for manually created movies."""
        Item.objects.create(
            media_id="2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Manual Movie",
            image="http://example.com/manual_movie.jpg",
        )

        response = manual.metadata("2", MediaTypes.MOVIE.value)

        self.assertEqual(response["title"], "Manual Movie")
        self.assertEqual(response["media_id"], "2")
        self.assertEqual(response["source"], Sources.MANUAL.value)
        self.assertEqual(response["media_type"], MediaTypes.MOVIE.value)
        self.assertEqual(response["synopsis"], "No synopsis available.")
        self.assertEqual(response["max_progress"], 1)

    def test_manual_season(self):
        """Test the season method for manually created seasons."""
        Item.objects.create(
            media_id="3",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Another TV Show",
            image="http://example.com/another.jpg",
        )

        Item.objects.create(
            media_id="3",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Another TV Show",
            image="http://example.com/another_s1.jpg",
            season_number=1,
        )

        for i in range(1, 3):
            Item.objects.create(
                media_id="3",
                source=Sources.MANUAL.value,
                media_type=MediaTypes.EPISODE.value,
                title=f"Episode {i}",
                image=f"http://example.com/another_s1e{i}.jpg",
                season_number=1,
                episode_number=i,
            )

        response = manual.season("3", 1)

        self.assertEqual(response["season_number"], 1)
        self.assertEqual(response["title"], "Another TV Show")
        self.assertEqual(response["season_title"], "Season 1")
        self.assertEqual(response["max_progress"], 2)
        self.assertEqual(len(response["episodes"]), 2)

    def test_manual_episode(self):
        """Test the episode method for manually created episodes."""
        Item.objects.create(
            media_id="4",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Third TV Show",
            image="http://example.com/third.jpg",
        )

        Item.objects.create(
            media_id="4",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Third TV Show",
            image="http://example.com/third_s1.jpg",
            season_number=1,
        )

        Item.objects.create(
            media_id="4",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            title="Special Episode",
            image="http://example.com/third_s1e1.jpg",
            season_number=1,
            episode_number=1,
        )

        response = manual.episode("4", 1, 1)

        self.assertEqual(response["media_type"], MediaTypes.EPISODE.value)
        self.assertEqual(response["title"], "Third TV Show")
        self.assertEqual(response["season_title"], "Season 1")
        self.assertEqual(response["episode_title"], "Special Episode")

        result = manual.episode("4", 1, 2)
        self.assertIsNone(result)

    def test_manual_process_episodes(self):
        """Test the process_episodes function for manual episodes."""
        Item.objects.create(
            media_id="5",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Process Episodes Test",
            image="http://example.com/process.jpg",
        )

        Item.objects.create(
            media_id="5",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Process Episodes Test",
            image="http://example.com/process_s1.jpg",
            season_number=1,
        )

        for i in range(1, 4):
            Item.objects.create(
                media_id="5",
                source=Sources.MANUAL.value,
                media_type=MediaTypes.EPISODE.value,
                title=f"Process Episode {i}",
                image=f"http://example.com/process_s1e{i}.jpg",
                season_number=1,
                episode_number=i,
            )

        season_metadata = {
            "season_number": 1,
            "episodes": [
                {
                    "media_id": "5",
                    "episode_number": 1,
                    "air_date": "2025-01-01",
                    "image": "http://example.com/process_s1e1.jpg",
                    "title": "Process Episode 1",
                },
                {
                    "media_id": "5",
                    "episode_number": 2,
                    "air_date": "2025-01-08",
                    "image": "http://example.com/process_s1e2.jpg",
                    "title": "Process Episode 2",
                },
                {
                    "media_id": "5",
                    "episode_number": 3,
                    "air_date": "2025-01-15",
                    "image": "http://example.com/process_s1e3.jpg",
                    "title": "Process Episode 3",
                },
            ],
        }

        ep_item1 = Item.objects.get(
            media_id="5",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
        )
        ep_item2 = Item.objects.get(
            media_id="5",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
        )

        episode_1 = Episode(item=ep_item1)
        episode_2 = Episode(item=ep_item2)

        episodes_in_db = [episode_1, episode_2]

        # Call process_episodes
        result = manual.process_episodes(season_metadata, episodes_in_db)

        self.assertEqual(len(result), 3)

        self.assertEqual(result[0]["episode_number"], 1)
        self.assertEqual(result[0]["title"], "Process Episode 1")
        self.assertEqual(result[0]["air_date"], "2025-01-01")
        self.assertTrue(result[0]["history"], [episode_1])

        self.assertEqual(result[1]["episode_number"], 2)
        self.assertEqual(result[1]["title"], "Process Episode 2")
        self.assertEqual(result[1]["air_date"], "2025-01-08")
        self.assertTrue(result[0]["history"], [episode_2])

        self.assertEqual(result[2]["episode_number"], 3)
        self.assertEqual(result[2]["title"], "Process Episode 3")
        self.assertEqual(result[2]["air_date"], "2025-01-15")
        self.assertFalse(result[2]["history"], [])

    def test_hardcover_get_tags(self):
        """Test the get_tags function from Hardcover provider."""
        tags_data = [{"tag": "Science Fiction"}, {"tag": "Fantasy"}]
        result = hardcover.get_tags(tags_data)
        self.assertEqual(result, ["Science Fiction", "Fantasy"])

        self.assertIsNone(hardcover.get_tags(None))

    def test_hardcover_get_ratings(self):
        """Test the get_ratings function from Hardcover provider."""
        self.assertEqual(hardcover.get_ratings(4.5), 9.0)

        self.assertIsNone(hardcover.get_ratings(None))

    def test_hardcover_get_edition_details(self):
        """Test the get_edition_details function from Hardcover provider."""
        edition_data = {
            "edition_format": "Paperback",
            "isbn_13": "9781234567890",
            "isbn_10": "1234567890",
            "publisher": {"name": "Test Publisher"},
        }

        result = hardcover.get_edition_details(edition_data)
        self.assertEqual(result["format"], "Paperback")
        self.assertEqual(result["publisher"], "Test Publisher")
        self.assertEqual(result["isbn"], ["1234567890", "9781234567890"])

        self.assertEqual(hardcover.get_edition_details(None), {})

        no_publisher = {
            "edition_format": "Paperback",
            "isbn_13": "9781234567890",
        }
        result = hardcover.get_edition_details(no_publisher)
        self.assertEqual(result["publisher"], None)

    def test_handle_error_hardcover_unauthorized(self):
        """Test the handle_error function with Hardcover unauthorized error."""
        mock_response = MagicMock()
        mock_response.status_code = 401  # Unauthorized
        mock_response.json.return_value = {"error": "Invalid API key"}

        error = requests.exceptions.HTTPError("401 Unauthorized")
        error.response = mock_response

        with self.assertRaises(services.ProviderAPIError) as cm:
            hardcover.handle_error(error)

        self.assertEqual(cm.exception.provider, Sources.HARDCOVER.value)

    def test_handle_error_hardcover_other(self):
        """Test the handle_error function with Hardcover other error."""
        mock_response = MagicMock()
        mock_response.status_code = 500  # Server error
        mock_response.json.return_value = {"error": "Server error"}

        error = requests.exceptions.HTTPError("500 Server Error")
        error.response = mock_response

        with self.assertRaises(services.ProviderAPIError) as cm:
            hardcover.handle_error(error)

        self.assertEqual(cm.exception.provider, Sources.HARDCOVER.value)

    def test_handle_error_hardcover_json_error(self):
        """Test the handle_error function with JSON decode error."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.side_effect = requests.exceptions.JSONDecodeError(
            "Invalid JSON",
            "",
            0,
        )

        error = requests.exceptions.HTTPError("500 Server Error")
        error.response = mock_response

        with self.assertRaises(services.ProviderAPIError) as cm:
            hardcover.handle_error(error)

        self.assertEqual(cm.exception.provider, Sources.HARDCOVER.value)


class CastOrderRegressionTests(TestCase):
    """Regression tests for issue #92 — first cast member (order=0) being dropped."""

    def test_get_cast_credits_order_zero_is_first(self):
        """Cast member with order=0 must sort before members with higher orders."""
        credits_data = {
            "cast": [
                {
                    "id": 2,
                    "name": "Second Actor",
                    "character": "Side Role",
                    "order": 1,
                    "known_for_department": "Acting",
                    "gender": 2,
                    "profile_path": None,
                },
                {
                    "id": 1,
                    "name": "Lead Actor",
                    "character": "Main Role",
                    "order": 0,
                    "known_for_department": "Acting",
                    "gender": 2,
                    "profile_path": None,
                },
            ],
        }
        result = tmdb.get_cast_credits(credits_data)
        self.assertEqual(result[0]["name"], "Lead Actor")
        self.assertEqual(result[0]["order"], 0)
        self.assertEqual(result[1]["name"], "Second Actor")

    def test_get_cast_credits_order_zero_not_treated_as_missing(self):
        """order=0 must not be conflated with order=None (missing order)."""
        credits_data = {
            "cast": [
                {
                    "id": 10,
                    "name": "No Order Actor",
                    "character": "Unknown Spot",
                    "order": None,
                    "known_for_department": "Acting",
                    "gender": 1,
                    "profile_path": None,
                },
                {
                    "id": 11,
                    "name": "First Billed",
                    "character": "Lead",
                    "order": 0,
                    "known_for_department": "Acting",
                    "gender": 2,
                    "profile_path": None,
                },
            ],
        }
        result = tmdb.get_cast_credits(credits_data)
        # order=0 must come before order=None
        self.assertEqual(result[0]["name"], "First Billed")
        self.assertEqual(result[1]["name"], "No Order Actor")

    def test_normalize_credit_rows_preserves_order_zero(self):
        """_normalize_credit_rows must store sort_order=0, not None."""
        rows = [
            {
                "person_id": "42",
                "name": "Top Billed",
                "image": "",
                "known_for_department": "Acting",
                "gender": "male",
                "role": "Hero",
                "department": "Acting",
                "order": 0,
            },
        ]
        result = _normalize_credit_rows(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["sort_order"], 0)
