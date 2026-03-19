from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import statistics_cache
from app.models import (
    Book,
    CreditRoleType,
    Episode,
    Item,
    ItemPersonCredit,
    MediaTypes,
    Person,
    PodcastEpisode,
    PodcastShow,
    Season,
    Sources,
    Status,
    TV,
)
from app.services import game_lengths as game_length_services
from integrations.models import PlexAccount


class MediaDetailsViewTests(TestCase):
    """Test the media details views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_view(self, mock_get_metadata):
        """Test the media details view."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Test Movie")

        mock_get_metadata.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "238",
            Sources.TMDB.value,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_prefers_stored_item_image_over_provider_image(
        self,
        mock_get_metadata,
    ):
        item = Item.objects.create(
            media_id="377938",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="The Lord of the Rings",
            image="https://images.example.com/custom-cover.jpg",
        )
        mock_get_metadata.return_value = {
            "media_id": "377938",
            "title": "The Lord of the Rings",
            "media_type": MediaTypes.BOOK.value,
            "source": Sources.HARDCOVER.value,
            "image": "https://images.example.com/provider-cover.jpg",
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.HARDCOVER.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "377938",
                    "title": "the-lord-of-the-rings",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media"]["image"], item.image)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_persists_movie_recommendation_metadata(self, mock_get_metadata):
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "provider_keywords": ["Whodunit", "Holiday"],
            "provider_certification": "PG",
            "provider_collection_id": "44",
            "provider_collection_name": "Mystery Collection",
            "details": {
                "country": "US",
                "studios": ["Pixar Animation Studios"],
                "certification": "PG",
            },
            "cast": [],
            "crew": [],
            "studios_full": [],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.provider_keywords, ["Whodunit", "Holiday"])
        self.assertEqual(item.provider_certification, "PG")
        self.assertEqual(item.provider_collection_id, "44")
        self.assertEqual(item.provider_collection_name, "Mystery Collection")

    @patch("app.providers.services.get_media_metadata")
    def test_game_media_details_renders_cached_hltb_tables(self, mock_get_metadata):
        Item.objects.create(
            media_id="325609",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Dispatch",
            image="https://example.com/dispatch.jpg",
            provider_external_ids={
                "hltb_game_id": 160618,
                "steam_app_id": 2592160,
                "itch_id": 0,
                "ign_uuid": "84fb8aca-cd19-4ff6-8919-c1b8ef5fa88a",
            },
            provider_game_lengths={
                "active_source": "hltb",
                "hltb": {
                    "game_id": 160618,
                    "url": "https://howlongtobeat.com/game/160618",
                    "summary": {
                        "main_minutes": 512,
                        "main_plus_minutes": 614,
                        "completionist_minutes": 1191,
                        "all_styles_minutes": 555,
                    },
                    "counts": {
                        "main": 1261,
                        "main_plus": 364,
                        "completionist": 108,
                        "all_styles": 1733,
                    },
                    "single_player_table": [
                        {
                            "label": "Main Story",
                            "count": 1261,
                            "average_minutes": 514,
                            "median_minutes": 510,
                            "rushed_minutes": 376,
                            "leisure_minutes": 634,
                        },
                    ],
                    "platform_table": [
                        {
                            "platform": "PC",
                            "count": 1479,
                            "main_minutes": 518,
                            "main_plus_minutes": 624,
                            "completionist_minutes": 1201,
                            "fastest_minutes": 240,
                            "slowest_minutes": 2581,
                        },
                    ],
                    "external_ids": {
                        "steam_app_id": 2592160,
                        "itch_id": 0,
                        "ign_uuid": "84fb8aca-cd19-4ff6-8919-c1b8ef5fa88a",
                    },
                    "raw": {},
                },
                "igdb": {
                    "game_id": 325609,
                    "summary": {
                        "hastily_seconds": 32400,
                        "normally_seconds": 32400,
                        "completely_seconds": 46800,
                        "count": 13,
                    },
                    "raw": [],
                },
            },
            provider_game_lengths_source="hltb",
            provider_game_lengths_match="steam_verified",
        )
        mock_get_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "source_url": "https://www.igdb.com/games/dispatch",
            "image": "https://example.com/dispatch.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "Main game",
                "release_date": "2025-10-22",
                "platforms": ["PC", "PlayStation 5"],
            },
            "genres": ["Action"],
            "related": {},
            "external_links": {
                "HowLongToBeat": "https://howlongtobeat.com/?q=Dispatch",
            },
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                    "title": "dispatch",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Time to Beat")
        self.assertContains(response, "How Long to Beat")
        self.assertContains(response, "Main Story")
        self.assertContains(response, 'href="https://howlongtobeat.com/game/160618"', html=False)
        self.assertNotContains(response, "Based on 1,733 submissions.")
        self.assertNotContains(response, "SINGLE-PLAYER")
        self.assertNotContains(response, "Playstyle")
        self.assertEqual(
            response.context["media"]["external_links"]["HowLongToBeat"],
            "https://howlongtobeat.com/game/160618",
        )

    @patch("app.views._queue_game_lengths_refresh", return_value=True)
    @patch("app.providers.services.get_media_metadata")
    def test_game_media_details_renders_igdb_fallback_and_queues_hltb_refresh(
        self,
        mock_get_metadata,
        mock_queue_game_lengths_refresh,
    ):
        Item.objects.create(
            media_id="325609",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Dispatch",
            image="https://example.com/dispatch.jpg",
            provider_game_lengths={
                "active_source": "igdb",
                "igdb": {
                    "game_id": 325609,
                    "summary": {
                        "hastily_seconds": 32400,
                        "normally_seconds": 32400,
                        "completely_seconds": 46800,
                        "count": 13,
                    },
                    "raw": [{"game_id": 325609}],
                },
            },
            provider_game_lengths_source="igdb",
            provider_game_lengths_match="igdb_fallback",
        )
        mock_get_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "source_url": "https://www.igdb.com/games/dispatch",
            "image": "https://example.com/dispatch.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "Main game",
                "release_date": "2025-10-22",
                "platforms": ["PC", "PlayStation 5"],
            },
            "genres": ["Action"],
            "related": {},
            "external_links": {
                "HowLongToBeat": "https://howlongtobeat.com/?q=Dispatch",
            },
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                    "title": "dispatch",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Internet Games Database")
        self.assertContains(response, 'href="https://www.igdb.com/games/dispatch"', html=False)
        self.assertContains(response, "Normally")
        self.assertContains(response, "13 submissions")
        mock_queue_game_lengths_refresh.assert_called_once()

    @patch("app.views._queue_game_lengths_refresh", return_value=True)
    @patch("app.providers.services.get_media_metadata")
    def test_game_media_details_queues_background_fetch_when_missing_game_lengths(
        self,
        mock_get_metadata,
        mock_queue_game_lengths_refresh,
    ):
        mock_get_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "source_url": "https://www.igdb.com/games/dispatch",
            "image": "https://example.com/dispatch.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "Main game",
                "release_date": "2025-10-22",
                "platforms": ["PC", "PlayStation 5"],
            },
            "genres": ["Action"],
            "related": {},
            "external_links": {
                "HowLongToBeat": "https://howlongtobeat.com/?q=Dispatch",
            },
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                    "title": "dispatch",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fetching cached time-to-beat data in the background.")
        self.assertTrue(
            Item.objects.filter(
                media_id="325609",
                source=Sources.IGDB.value,
                media_type=MediaTypes.GAME.value,
            ).exists(),
        )
        mock_queue_game_lengths_refresh.assert_called_once()

    @patch("app.providers.services.get_media_metadata")
    @patch("app.views._queue_game_lengths_refresh")
    def test_game_media_details_shows_pending_when_refresh_lock_exists(
        self,
        mock_queue_game_lengths_refresh,
        mock_get_metadata,
    ):
        item = Item.objects.create(
            media_id="325609",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Dispatch",
            image="https://example.com/dispatch.jpg",
        )
        cache.set(
            game_length_services.get_game_lengths_refresh_lock_key(
                item.id,
                force=False,
                fetch_hltb=True,
            ),
            game_length_services.build_game_lengths_refresh_lock(
                force=False,
                fetch_hltb=True,
            ),
            timeout=game_length_services.GAME_LENGTHS_REFRESH_TTL,
        )
        mock_get_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "source_url": "https://www.igdb.com/games/dispatch",
            "image": "https://example.com/dispatch.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "Main game",
                "release_date": "2025-10-22",
                "platforms": ["PC", "PlayStation 5"],
            },
            "genres": ["Action"],
            "related": {},
            "external_links": {
                "HowLongToBeat": "https://howlongtobeat.com/?q=Dispatch",
            },
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                    "title": "dispatch",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fetching cached time-to-beat data in the background.")
        mock_queue_game_lengths_refresh.assert_not_called()

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_view(self, mock_process_episodes, mock_get_metadata):
        """Test the season details view."""
        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "episodes": [],
            },
        }

        mock_process_episodes.return_value = [
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "season_number": 1,
                "episode_number": 1,
                "name": "Episode 1",
                "air_date": "2023-01-01",
                "watched": False,
            },
        ]

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Season 1")
        self.assertEqual(len(response.context["media"]["episodes"]), 1)
        self.assertContains(
            response,
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.EPISODE.value, "1668", 1, 1],
            ),
        )

        mock_get_metadata.assert_called_once_with(
            "tv_with_seasons",
            "1668",
            Sources.TMDB.value,
            [1],
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_prefers_stored_item_image_over_provider_image(
        self,
        mock_process_episodes,
        mock_get_metadata,
    ):
        mock_process_episodes.return_value = []
        Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            image="https://images.example.com/custom-season.jpg",
            season_number=1,
        )
        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/provider-season.jpg",
                "episodes": [],
            },
        }

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["media"]["image"],
            "https://images.example.com/custom-season.jpg",
        )

    @patch("integrations.tasks.fetch_collection_metadata_for_item.delay")
    @patch("app.providers.services.get_media_metadata")
    def test_game_details_skips_collection_autofetch(
        self,
        mock_get_metadata,
        mock_fetch_delay,
    ):
        """Game details should not trigger collection auto-fetch."""
        mock_get_metadata.return_value = {
            "media_id": "game-123",
            "title": "Test Game",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "image": "http://example.com/game.jpg",
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        Item.objects.create(
            media_id="game-123",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Test Game",
            image="http://example.com/game.jpg",
        )

        PlexAccount.objects.create(
            user=self.user,
            plex_token="plex-token",
            plex_username="plex-user",
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "game-123",
                    "title": "test-game",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["fetching_collection_data"])
        self.assertIsNone(response.context["item_id_for_polling"])
        mock_fetch_delay.assert_not_called()

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_cast_and_crew_links(self, mock_get_metadata):
        """Movie details should render cast/crew links to person pages."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/movie/238",
            "image": "http://example.com/image.jpg",
            "synopsis": "Test synopsis",
            "details": {"format": "Movie"},
            "cast": [
                {
                    "person_id": "10",
                    "name": "John Actor",
                    "role": "Hero",
                },
            ],
            "crew": [
                {
                    "person_id": "11",
                    "name": "Jane Director",
                    "role": "Director",
                    "department": "Directing",
                },
            ],
            "studios_full": [
                {
                    "studio_id": "20",
                    "name": "Studio One",
                },
            ],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "John Actor")
        self.assertContains(response, "Jane Director")
        self.assertContains(response, "Studio One")
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.TMDB.value,
                    "person_id": "10",
                    "name": "john-actor",
                },
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_tv_details_view_adds_specials_from_regular_path(self, mock_get_metadata):
        """TV details should show a specials season when season 0 is enriched."""
        mock_get_metadata.side_effect = [
            {
                "media_id": "114410",
                "title": "Chainsaw Man",
                "media_type": MediaTypes.TV.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/show.jpg",
                "tvdb_id": "10196540",
                "details": {
                    "runtime": "24m",
                    "first_air_date": "2022-10-12",
                },
                "related": {
                    "seasons": [
                        {
                            "source": Sources.TMDB.value,
                            "media_type": MediaTypes.SEASON.value,
                            "media_id": "114410",
                            "title": "Chainsaw Man",
                            "season_number": 1,
                            "season_title": "Season 1",
                            "image": settings.IMG_NONE,
                        },
                    ],
                },
                "cast": [],
                "crew": [],
                "studios_full": [],
            },
            {
                "media_id": "114410",
                "title": "Chainsaw Man",
                "media_type": MediaTypes.TV.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/show.jpg",
                "tvdb_id": "10196540",
                "details": {
                    "runtime": "24m",
                    "first_air_date": "2022-10-12",
                },
                "season/0": {
                    "season_number": 0,
                    "season_title": "Specials",
                },
                "related": {
                    "seasons": [
                        {
                            "source": Sources.TMDB.value,
                            "media_type": MediaTypes.SEASON.value,
                            "media_id": "114410",
                            "title": "Chainsaw Man",
                            "season_number": 0,
                            "season_title": "Specials",
                            "image": "http://example.com/specials.jpg",
                        },
                        {
                            "source": Sources.TMDB.value,
                            "media_type": MediaTypes.SEASON.value,
                            "media_id": "114410",
                            "title": "Chainsaw Man",
                            "season_number": 1,
                            "season_title": "Season 1",
                            "image": "http://example.com/season1.jpg",
                        },
                    ],
                },
                "cast": [],
                "crew": [],
                "studios_full": [],
            },
        ]

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "114410",
                    "title": "chainsaw-man",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        seasons = response.context["media"]["related"]["seasons"]
        self.assertEqual(seasons[0]["item"]["season_number"], 0)
        self.assertContains(
            response,
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "114410",
                    "title": "chainsaw-man",
                    "season_number": 0,
                },
            ),
        )
        self.assertEqual(mock_get_metadata.call_count, 2)
        self.assertEqual(
            mock_get_metadata.call_args_list[0].args,
            (
                MediaTypes.TV.value,
                "114410",
                Sources.TMDB.value,
            ),
        )
        self.assertEqual(
            mock_get_metadata.call_args_list[1].args,
            (
                "tv_with_seasons",
                "114410",
                Sources.TMDB.value,
                [0],
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_tv_details_view_uses_special_watch_for_show_end_date(
        self,
        mock_get_metadata,
    ):
        """TV details should show the most recent special watch in the history card."""
        watched_main = datetime(2023, 8, 28, 12, 0, tzinfo=UTC)
        watched_special = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)

        tv_item = Item.objects.create(
            media_id="114410",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Chainsaw Man",
            image="http://example.com/show.jpg",
        )
        tv = TV.objects.create(
            user=self.user,
            item=tv_item,
            status=Status.IN_PROGRESS.value,
        )

        season_one_item = Item.objects.create(
            media_id="114410",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Chainsaw Man",
            image="http://example.com/season1.jpg",
            season_number=1,
        )
        season_one = Season.objects.create(
            user=self.user,
            item=season_one_item,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )
        Episode.objects.create(
            item=Item.objects.create(
                media_id="114410",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Episode 12",
                image="http://example.com/ep12.jpg",
                season_number=1,
                episode_number=12,
            ),
            related_season=season_one,
            end_date=watched_main,
        )

        specials_item = Item.objects.create(
            media_id="114410",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Chainsaw Man",
            image="http://example.com/specials.jpg",
            season_number=0,
        )
        specials = Season.objects.create(
            user=self.user,
            item=specials_item,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )
        Episode.objects.create(
            item=Item.objects.create(
                media_id="114410",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Special 1",
                image="http://example.com/s00e01.jpg",
                season_number=0,
                episode_number=1,
            ),
            related_season=specials,
            end_date=watched_special,
        )

        mock_get_metadata.return_value = {
            "media_id": "114410",
            "title": "Chainsaw Man",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/show.jpg",
            "details": {
                "runtime": "24m",
            },
            "related": {
                "seasons": [
                    {
                        "source": Sources.TMDB.value,
                        "media_type": MediaTypes.SEASON.value,
                        "media_id": "114410",
                        "title": "Chainsaw Man",
                        "season_number": 0,
                        "season_title": "Specials",
                        "image": "http://example.com/specials.jpg",
                    },
                    {
                        "source": Sources.TMDB.value,
                        "media_type": MediaTypes.SEASON.value,
                        "media_id": "114410",
                        "title": "Chainsaw Man",
                        "season_number": 1,
                        "season_title": "Season 1",
                        "image": "http://example.com/season1.jpg",
                    },
                ],
            },
            "cast": [],
            "crew": [],
            "studios_full": [],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "114410",
                    "title": "chainsaw-man",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_instance"].end_date, watched_special)
        self.assertEqual(response.context["current_instance"].progress, 12)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_backfills_author_credits_and_renders_links(
        self,
        mock_get_metadata,
    ):
        mock_get_metadata.return_value = {
            "media_id": "OL123M",
            "title": "Linked Book",
            "media_type": MediaTypes.BOOK.value,
            "source": Sources.OPENLIBRARY.value,
            "source_url": "https://openlibrary.org/books/OL123M",
            "image": "http://example.com/book.jpg",
            "synopsis": "Book synopsis",
            "max_progress": 300,
            "details": {
                "author": ["Open Author"],
                "publish_date": "2000-01-01",
            },
            "authors_full": [
                {
                    "person_id": "OL1A",
                    "name": "Open Author",
                    "image": "http://example.com/author.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
            ],
            "related": {},
        }

        item = Item.objects.create(
            media_id="OL123M",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Linked Book",
            image="http://example.com/book.jpg",
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=300,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "OL123M",
                    "title": "linked-book",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        author_person = Person.objects.get(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL1A",
        )
        self.assertTrue(
            ItemPersonCredit.objects.filter(
                item=item,
                person=author_person,
                role_type=CreditRoleType.AUTHOR.value,
            ).exists(),
        )
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "person_id": "OL1A",
                    "name": "open-author",
                },
            ),
        )
        html = response.content.decode()
        self.assertEqual(
            html.count('text-sm font-semibold text-gray-400">AUTHOR</h3>'),
            1,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_uses_authors_full_fallback_without_item(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "72274276213",
            "title": "Metadata Only Manga",
            "media_type": MediaTypes.MANGA.value,
            "source": Sources.MANGAUPDATES.value,
            "source_url": "https://www.mangaupdates.com/series/72274276213",
            "image": "http://example.com/manga.jpg",
            "synopsis": "Manga synopsis",
            "details": {
                "authors": ["Manga Author"],
            },
            "authors_full": [
                {
                    "person_id": "55",
                    "name": "Manga Author",
                    "image": "http://example.com/manga-author.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
            ],
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MANGAUPDATES.value,
                    "media_type": MediaTypes.MANGA.value,
                    "media_id": "72274276213",
                    "title": "metadata-only-manga",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ItemPersonCredit.objects.count(), 0)
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.MANGAUPDATES.value,
                    "person_id": "55",
                    "name": "manga-author",
                },
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_refreshes_stale_author_cache_and_renders_links(
        self,
        mock_get_metadata,
    ):
        stale_metadata = {
            "media_id": "OL999M",
            "title": "Cached Book",
            "media_type": MediaTypes.BOOK.value,
            "source": Sources.OPENLIBRARY.value,
            "source_url": "https://openlibrary.org/books/OL999M",
            "image": "http://example.com/book.jpg",
            "synopsis": "Book synopsis",
            "max_progress": 320,
            "details": {
                "author": ["Cached Author"],
                "publish_date": "1999-01-01",
            },
            "related": {},
        }
        refreshed_metadata = {
            **stale_metadata,
            "authors_full": [
                {
                    "person_id": "OL9A",
                    "name": "Cached Author",
                    "image": "http://example.com/author.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
            ],
        }
        call_count = {"count": 0}

        def _metadata_side_effect(*_args, **_kwargs):
            call_count["count"] += 1
            if call_count["count"] == 1:
                return stale_metadata
            return refreshed_metadata

        mock_get_metadata.side_effect = _metadata_side_effect

        item = Item.objects.create(
            media_id="OL999M",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Cached Book",
            image="http://example.com/book.jpg",
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=320,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )

        cache_key = f"{Sources.OPENLIBRARY.value}_{MediaTypes.BOOK.value}_OL999M"
        cache.set(cache_key, stale_metadata)

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "OL999M",
                    "title": "cached-book",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        detail_calls = [
            call
            for call in mock_get_metadata.call_args_list
            if call.args[:3]
            == (
                MediaTypes.BOOK.value,
                "OL999M",
                Sources.OPENLIBRARY.value,
            )
        ]
        self.assertGreaterEqual(len(detail_calls), 2)
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "person_id": "OL9A",
                    "name": "cached-author",
                },
            ),
        )

        author_person = Person.objects.get(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL9A",
        )
        self.assertTrue(
            ItemPersonCredit.objects.filter(
                item=item,
                person=author_person,
                role_type=CreditRoleType.AUTHOR.value,
            ).exists(),
        )

    def test_podcast_media_details_renders_for_show_with_no_user_plays(self):
        """Podcast details should render even when episodes have no play history."""
        show = PodcastShow.objects.create(
            podcast_uuid="itunes:1002937870",
            title="Dear Hank & John",
            author="Hank and John",
            image="http://example.com/podcast.jpg",
            rss_feed_url="",
        )
        PodcastEpisode.objects.create(
            show=show,
            episode_uuid="dhj-episode-1",
            title="Episode One",
            duration=3600,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.POCKETCASTS.value,
                    "media_type": MediaTypes.PODCAST.value,
                    "media_id": show.podcast_uuid,
                    "title": "dear-hank-john",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dear Hank &amp; John")
        self.assertContains(response, "Episode One")

    def test_podcast_episode_fragment_renders_for_show_with_no_user_plays(self):
        """Podcast episode HTMX fragments should render when no play history exists."""
        show = PodcastShow.objects.create(
            podcast_uuid="itunes:1002937870",
            title="Dear Hank & John",
            author="Hank and John",
            image="http://example.com/podcast.jpg",
            rss_feed_url="",
        )
        PodcastEpisode.objects.create(
            show=show,
            episode_uuid="dhj-episode-2",
            title="Episode Two",
            duration=1800,
        )

        response = self.client.get(
            reverse("podcast_episodes_api", kwargs={"show_id": show.id}),
            {"format": "html", "page": 1, "page_size": 20},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Episode Two")

    @patch("app.tasks.enqueue_genre_backfill_items", return_value=1)
    def test_media_details_genre_update_refreshes_reading_top_genres(self, _mock_enqueue_genre_backfill_items):
        """Saving reading genres from details should invalidate stale day caches."""
        played_at = timezone.now() - timedelta(days=30)
        item = Item.objects.create(
            media_id="377938",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="The Lord of the Rings",
            image="http://example.com/book.jpg",
            genres=[],
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=900,
            start_date=played_at,
            end_date=played_at,
        )

        statistics_cache.build_stats_for_day(self.user.id, played_at.date())
        stale_stats = statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        self.assertEqual(stale_stats["book_consumption"]["top_genres"], [])

        with patch("app.providers.services.get_media_metadata") as mock_get_metadata:
            mock_get_metadata.return_value = {
                "media_id": "377938",
                "title": "The Lord of the Rings",
                "media_type": MediaTypes.BOOK.value,
                "source": Sources.MANUAL.value,
                "image": "http://example.com/book.jpg",
                "max_progress": 1178,
                "genres": ["Fantasy"],
                "details": {"number_of_pages": 1178},
            }
            response = self.client.get(
                reverse(
                    "media_details",
                    kwargs={
                        "source": Sources.MANUAL.value,
                        "media_type": MediaTypes.BOOK.value,
                        "media_id": "377938",
                        "title": "the-lord-of-the-rings",
                    },
                ),
            )
        self.assertEqual(response.status_code, 200)

        item.refresh_from_db()
        self.assertEqual(item.genres, ["Fantasy"])

        statistics_cache.invalidate_statistics_cache(self.user.id, "All Time")
        refreshed_stats = statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        refreshed_genres = [entry["name"] for entry in refreshed_stats["book_consumption"]["top_genres"]]
        self.assertIn("Fantasy", refreshed_genres)

    @patch("app.providers.services.get_media_metadata")
    def test_tv_media_details_uses_episode_runtime_fallback_when_metadata_runtime_missing(
        self,
        mock_get_metadata,
    ):
        """TV details should show a derived runtime when provider runtime is missing."""
        show_item = Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Bridgerton",
            image="http://example.com/show.jpg",
            runtime_minutes=999999,
        )
        Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Episode 1",
            runtime_minutes=52,
        )
        Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            title="Episode 2",
            runtime_minutes=54,
        )

        mock_get_metadata.return_value = {
            "media_id": "91239",
            "title": "Bridgerton",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/tv/91239",
            "image": "http://example.com/show.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "TV",
                "runtime": None,
                "seasons": 1,
            },
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
            "providers": {},
            "external_links": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "91239",
                    "title": "bridgerton",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media"]["details"]["runtime"], "53m")
        show_item.refresh_from_db()
        self.assertEqual(show_item.runtime_minutes, 999999)

    @patch("app.providers.services.get_media_metadata")
    def test_tv_media_details_play_stats_skip_placeholder_episode_runtimes(
        self,
        mock_get_metadata,
    ):
        """TV details totals should ignore placeholder episode runtimes."""
        watched_at = timezone.now()
        show_item = Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Bridgerton",
            image="http://example.com/show.jpg",
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Bridgerton",
            image="http://example.com/season.jpg",
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        valid_episode_item = Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Episode 1",
            runtime_minutes=45,
        )
        placeholder_episode_item = Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            title="Episode 2",
            runtime_minutes=999998,
        )
        Episode.objects.create(
            item=valid_episode_item,
            related_season=season,
            end_date=watched_at,
        )
        Episode.objects.create(
            item=placeholder_episode_item,
            related_season=season,
            end_date=watched_at + timedelta(minutes=1),
        )

        mock_get_metadata.return_value = {
            "media_id": "91239",
            "title": "Bridgerton",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/tv/91239",
            "image": "http://example.com/show.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "TV",
                "runtime": None,
                "seasons": 1,
            },
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
            "providers": {},
            "external_links": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "91239",
                    "title": "bridgerton",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["play_stats"]["total_minutes"], 45)
        self.assertEqual(response.context["play_stats"]["episode_count"], 1)

    @patch("app.providers.services.get_media_metadata")
    def test_tv_media_details_show_total_runtime_uses_same_calculation_as_media_list(
        self,
        mock_get_metadata,
    ):
        """TV details should show shared total runtime while play stats remain watched hours."""
        now = timezone.now()
        show_item = Item.objects.create(
            media_id="fallout-runtime-shared",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Shared Runtime Show",
            image="http://example.com/show.jpg",
            runtime_minutes=25,
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="fallout-runtime-shared",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Shared Runtime Show",
            image="http://example.com/season.jpg",
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )

        first_episode_item = Item.objects.create(
            media_id="fallout-runtime-shared",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Episode 1",
            runtime_minutes=52,
            release_datetime=now - timedelta(days=3),
        )
        second_episode_item = Item.objects.create(
            media_id="fallout-runtime-shared",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            title="Episode 2",
            runtime_minutes=58,
            release_datetime=now - timedelta(days=2),
        )
        third_episode_item = Item.objects.create(
            media_id="fallout-runtime-shared",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=3,
            title="Episode 3",
            runtime_minutes=47,
            release_datetime=now - timedelta(days=1),
        )

        Episode.objects.create(
            item=first_episode_item,
            related_season=season,
            end_date=now - timedelta(days=1),
        )
        Episode.objects.create(
            item=second_episode_item,
            related_season=season,
            end_date=now,
        )
        Episode.objects.create(
            item=third_episode_item,
            related_season=season,
        )

        mock_get_metadata.return_value = {
            "media_id": "fallout-runtime-shared",
            "title": "Shared Runtime Show",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/tv/fallout-runtime-shared",
            "image": "http://example.com/show.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "TV",
                "runtime": "25m",
                "seasons": 1,
                "episodes": 3,
            },
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
            "providers": {},
            "external_links": {},
        }

        detail_response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "fallout-runtime-shared",
                    "title": "shared-runtime-show",
                },
            ),
        )

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.context["media"]["details"]["runtime"], "25m")
        self.assertEqual(detail_response.context["media"]["details"]["total_runtime"], "2h 37min")
        self.assertEqual(detail_response.context["play_stats"]["total_minutes"], 110)
        self.assertContains(detail_response, "WATCHED HOURS")
        self.assertContains(detail_response, "TOTAL RUNTIME")
        self.assertContains(detail_response, "2h 37min")

        list_response = self.client.get(
            reverse("medialist", args=[MediaTypes.TV.value])
            + "?layout=table&search=Shared+Runtime+Show",
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "2h 37min")

    @patch("app.providers.openlibrary.book")
    def test_audiobookshelf_book_details_does_not_call_openlibrary(
        self,
        mock_openlibrary_book,
    ):
        """Audiobookshelf detail pages should render using local metadata."""
        item = Item.objects.create(
            media_id="f9e2ce45ec9315a7c54c",
            source=Sources.AUDIOBOOKSHELF.value,
            media_type=MediaTypes.BOOK.value,
            title="The Blade Itself",
            image="https://img.example/blade.jpg",
            runtime_minutes=1320,
            authors=["Joe Abercrombie"],
            format="audiobook",
        )

        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.IN_PROGRESS.value,
            progress=60,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.AUDIOBOOKSHELF.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "f9e2ce45ec9315a7c54c",
                    "title": "the-blade-itself",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The Blade Itself")
        mock_openlibrary_book.assert_not_called()
