import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from app import live_playback
from app.models import TV, Anime, Episode, Item, MediaTypes, Movie, Season, Status
from integrations.webhooks.jellyfin import JellyfinWebhookProcessor


class JellyfinWebhookTests(TestCase):
    """Tests for Jellyfin webhook."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "testuser", "token": "test-token"}
        self.user = get_user_model().objects.create_superuser(**self.credentials)
        self.url = reverse("jellyfin_webhook", kwargs={"token": "test-token"})

    def tearDown(self):
        """Clear cached playback state created by webhook tests."""
        live_playback.clear_user_playback_state(self.user.id)

    def test_invalid_token(self):
        """Test webhook with invalid token returns 401."""
        url = reverse("jellyfin_webhook", kwargs={"token": "invalid-token"})
        response = self.client.post(url, data={}, content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_tv_episode_mark_played(self):
        """Test webhook handles TV episode mark played event."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "The One Where Monica Gets a Roommate",
                "ProviderIds": {
                    "Tvdb": "303821",
                    "Imdb": "tt0583459",
                },
                "SeriesName": "Friends",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify objects were created
        tv_item = Item.objects.get(media_type=MediaTypes.TV.value, media_id="1668")
        self.assertEqual(tv_item.title, "Friends")

        tv = TV.objects.get(item=tv_item, user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

        season = Season.objects.get(
            item__media_id="1668",
            item__season_number=1,
        )
        self.assertEqual(season.status, Status.IN_PROGRESS.value)

        episode = Episode.objects.get(
            item__media_id="1668",
            item__season_number=1,
            item__episode_number=1,
        )
        self.assertIsNotNone(episode.end_date)

    def test_movie_mark_played(self):
        """Test webhook handles movie mark played event."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "Type": "Movie",
                "ProviderIds": {"Tmdb": "603"},
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify movie was created and marked as completed
        movie = Movie.objects.get(
            item__media_id="603",
            user=self.user,
        )
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.progress, 1)

    @patch("app.providers.mal.anime")
    @patch("app.providers.tmdb.find")
    def test_anime_movie_mark_played(self, mock_tmdb_find, mock_mal_anime):
        """Test webhook handles movie mark played event."""
        mock_tmdb_find.return_value = {
            "movie_results": [{"id": 10494}],
        }
        mock_mal_anime.return_value = {
            "media_id": "437",
            "title": "Perfect Blue",
            "image": "https://example.com/perfect-blue.jpg",
            "max_progress": 1,
        }
        payload = {
            "Event": "Stop",
            "Item": {
                "Name": "Perfect Blue",
                "ProductionYear": 1997,
                "Type": "Movie",
                "ProviderIds": {"Imdb": "tt0156887"},
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify movie was created and marked as completed
        movie = Anime.objects.get(
            item__media_id="437",
            user=self.user,
        )
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.progress, 1)

    def test_anime_episode_mark_played(self):
        """Test webhook handles anime episode mark played event."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "The Journey's End",
                "ProviderIds": {
                    "Tvdb": "9350138",
                    "Imdb": "tt23861604",
                },
                "UserData": {"Played": True},
                "SeriesName": "Frieren: Beyond Journey's End",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        # Verify anime was created and marked as in progress
        anime = Anime.objects.get(
            item__media_id="52991",
            user=self.user,
        )
        self.assertEqual(anime.status, Status.IN_PROGRESS.value)
        self.assertEqual(anime.progress, 1)

    @patch("integrations.webhooks.base.anime_mapping.load_mapping_data")
    @patch("app.providers.mal.anime")
    @patch("app.providers.tmdb.tv_with_seasons")
    @patch("app.providers.tmdb.find")
    def test_anime_episode_prefers_tmdb_mapping_for_later_season(
        self,
        mock_find,
        mock_tv_with_seasons,
        mock_mal_anime,
        mock_load_mapping_data,
    ):
        """TMDB grouped-anime mappings should win when TVDB mapping disagrees."""
        mock_find.return_value = {
            "tv_episode_results": [
                {
                    "show_id": 12345,
                    "season_number": 2,
                    "episode_number": 11,
                },
            ],
            "tv_results": [],
        }
        mock_tv_with_seasons.return_value = {
            "media_id": "12345",
            "title": "Hell's Paradise",
            "tvdb_id": "402474",
            "season/2": {"episodes": [{"episode_number": 11}]},
        }
        mock_load_mapping_data.return_value = {
            "hells-paradise-tvdb": {
                "tvdb_id": "402474",
                "tvdb_season": 2,
                "tvdb_epoffset": -13,
                "mal_id": "46569",
            },
            "hells-paradise-tmdb": {
                "tmdb_show_id": "12345",
                "tmdb_season": 2,
                "tmdb_epoffset": 0,
                "mal_id": "60067",
            },
        }

        def mal_side_effect(media_id):
            if str(media_id) == "60067":
                return {
                    "media_id": "60067",
                    "title": "Hell's Paradise 2nd Season",
                    "image": "https://example.com/hells-paradise-s2.jpg",
                    "max_progress": 12,
                }
            if str(media_id) == "46569":
                return {
                    "media_id": "46569",
                    "title": "Hell's Paradise",
                    "image": "https://example.com/hells-paradise-s1.jpg",
                    "max_progress": 13,
                }
            msg = f"Unexpected MAL ID requested: {media_id}"
            raise AssertionError(msg)

        mock_mal_anime.side_effect = mal_side_effect

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "Episode 11",
                "ProviderIds": {
                    "Tmdb": "12345",
                    "Tvdb": "402474",
                },
                "UserData": {"Played": True},
                "SeriesName": "Hell's Paradise",
                "ParentIndexNumber": 2,
                "IndexNumber": 11,
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        anime = Anime.objects.get(item__media_id="60067", user=self.user)
        self.assertEqual(anime.status, Status.IN_PROGRESS.value)
        self.assertEqual(anime.progress, 11)
        self.assertFalse(
            Anime.objects.filter(item__media_id="46569", user=self.user).exists(),
        )

    @patch("integrations.webhooks.base.BaseWebhookProcessor._handle_tv_episode")
    @patch("integrations.webhooks.base.anime_mapping.load_mapping_data")
    @patch("app.providers.mal.anime")
    @patch("app.providers.tmdb.tv_with_seasons")
    @patch("app.providers.tmdb.find")
    def test_anime_episode_falls_back_to_tv_when_mapping_progress_is_impossible(
        self,
        mock_find,
        mock_tv_with_seasons,
        mock_mal_anime,
        mock_load_mapping_data,
        mock_handle_tv_episode,
    ):
        """Impossible anime progress should not create a bogus flat anime entry."""
        mock_find.return_value = {
            "tv_episode_results": [
                {
                    "show_id": 12345,
                    "season_number": 2,
                    "episode_number": 11,
                },
            ],
            "tv_results": [],
        }
        mock_tv_with_seasons.return_value = {
            "media_id": "12345",
            "title": "Hell's Paradise",
            "tvdb_id": "402474",
            "season/2": {"episodes": [{"episode_number": 11}]},
        }
        mock_load_mapping_data.return_value = {
            "hells-paradise-tvdb": {
                "tvdb_id": "402474",
                "tvdb_season": 2,
                "tvdb_epoffset": -13,
                "mal_id": "46569",
            },
        }
        mock_mal_anime.return_value = {
            "media_id": "46569",
            "title": "Hell's Paradise",
            "image": "https://example.com/hells-paradise-s1.jpg",
            "max_progress": 13,
        }

        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "Episode 11",
                "ProviderIds": {
                    "Tvdb": "402474",
                },
                "UserData": {"Played": True},
                "SeriesName": "Hell's Paradise",
                "ParentIndexNumber": 2,
                "IndexNumber": 11,
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Anime.objects.count(), 0)
        mock_handle_tv_episode.assert_called_once()
        self.assertEqual(
            mock_handle_tv_episode.call_args.args[:3],
            (12345, 2, 11),
        )

    def test_ignored_event_types(self):
        """Test webhook ignores irrelevant event types."""
        payload = {
            "Event": "SomeOtherEvent",
            "Item": {
                "Type": "Movie",
                "ProviderIds": {"Tmdb": "12345"},
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Movie.objects.count(), 0)

    def test_missing_tmdb_id(self):
        """Test webhook handles missing TMDB ID gracefully."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "ProviderIds": {},
                "UserData": {"Played": True},
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Movie.objects.count(), 0)

    def test_mark_unplayed(self):
        """Test webhook handles not finished events."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "Type": "Movie",
                "ProviderIds": {"Tmdb": "603"},
                "UserData": {"Played": False},
            },
        }
        self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603")
        self.assertEqual(movie.progress, 0)
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)

    def test_repeated_watch(self):
        """Test webhook handles repeated watches."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "ProductionYear": 1999,
                "Name": "The Matrix",
                "ProviderIds": {"Tmdb": "603"},
                "UserData": {"Played": True},
            },
        }

        # First watch
        self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        # Second watch
        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.filter(item__media_id="603")
        self.assertEqual(movie.count(), 2)
        self.assertEqual(movie[0].status, Status.COMPLETED.value)
        self.assertEqual(movie[1].status, Status.COMPLETED.value)

    def test_extract_external_ids(self):
        """Test extracting external IDs from provider payload."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "ProviderIds": {
                    "Tmdb": "603",
                    "Tvdb": "169",
                },
            },
        }

        expected = {
            "tmdb_id": "603",
            "imdb_id": None,
            "tvdb_id": "169",
        }

        result = JellyfinWebhookProcessor()._extract_external_ids(payload)
        if result != expected:
            msg = f"Expected {expected}, got {result}"
            raise AssertionError(msg)

    def test_extract_external_ids_empty(self):
        """Test handling empty provider payload."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "Name": "The Matrix",
                "ProductionYear": 1999,
                "ProviderIds": {},
            },
        }

        expected = {
            "tmdb_id": None,
            "imdb_id": None,
            "tvdb_id": None,
        }

        result = JellyfinWebhookProcessor()._extract_external_ids(payload)
        if result != expected:
            msg = f"Expected {expected}, got {result}"
            raise AssertionError(msg)

    def test_extract_external_ids_missing(self):
        """Test handling missing ProviderIds."""
        payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Movie",
                "Name": "The Matrix",
                "ProductionYear": 1999,
            },
        }
        expected = {
            "tmdb_id": None,
            "imdb_id": None,
            "tvdb_id": None,
        }

        result = JellyfinWebhookProcessor()._extract_external_ids(payload)
        if result != expected:
            msg = f"Expected {expected}, got {result}"
            raise AssertionError(msg)

    @patch("app.providers.tmdb.find")
    def test_play_event_stores_live_playback_state(self, mock_find):
        """Play events should create live playback state for the home card."""
        mock_find.return_value = {
            "tv_episode_results": [
                {
                    "show_id": 1668,
                    "season_number": 1,
                    "episode_number": 1,
                },
            ],
            "tv_results": [],
        }

        payload = {
            "Event": "Play",
            "Item": {
                "Type": "Episode",
                "Name": "The One Where Monica Gets a Roommate",
                "Id": "jf-episode-1",
                "SeriesName": "Friends",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "RunTimeTicks": 26660000000,
                "ProviderIds": {
                    "Tvdb": "303821",
                    "Imdb": "tt0583459",
                },
                "UserData": {"Played": False},
            },
            "PlaybackPositionTicks": 14470000000,
        }

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(state)
        self.assertEqual(state["media_type"], MediaTypes.EPISODE.value)
        self.assertEqual(state["media_id"], "1668")
        self.assertEqual(state["status"], live_playback.PLAYBACK_STATUS_PLAYING)
        self.assertEqual(state["season_number"], 1)
        self.assertEqual(state["episode_number"], 1)
        self.assertEqual(state["duration_seconds"], 2666)
        self.assertEqual(state["view_offset_seconds"], 1447)

    @patch("app.providers.tmdb.find")
    def test_pause_and_stop_events_update_live_playback_state(self, mock_find):
        """Pause should keep card state; stop should transition to stopped."""
        mock_find.return_value = {
            "tv_episode_results": [
                {
                    "show_id": 1668,
                    "season_number": 1,
                    "episode_number": 1,
                },
            ],
            "tv_results": [],
        }

        play_payload = {
            "Event": "Play",
            "Item": {
                "Type": "Episode",
                "Name": "The One Where Monica Gets a Roommate",
                "Id": "jf-episode-2",
                "SeriesName": "Friends",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "RunTimeTicks": 26660000000,
                "ProviderIds": {
                    "Tvdb": "303821",
                    "Imdb": "tt0583459",
                },
                "UserData": {"Played": False},
            },
            "PlaybackPositionTicks": 6000000000,
        }
        pause_payload = {
            "Event": "Pause",
            "Item": {
                "Type": "Episode",
                "Name": "The One Where Monica Gets a Roommate",
                "Id": "jf-episode-2",
                "SeriesName": "Friends",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "RunTimeTicks": 26660000000,
                "ProviderIds": {
                    "Tvdb": "303821",
                    "Imdb": "tt0583459",
                },
                "UserData": {"Played": False},
            },
            "PlaybackPositionTicks": 7210000000,
        }
        stop_payload = {
            "Event": "Stop",
            "Item": {
                "Type": "Episode",
                "Name": "The One Where Monica Gets a Roommate",
                "Id": "jf-episode-2",
                "SeriesName": "Friends",
                "ParentIndexNumber": 1,
                "IndexNumber": 1,
                "ProviderIds": {
                    "Tvdb": "303821",
                    "Imdb": "tt0583459",
                },
                "UserData": {"Played": True},
            },
        }

        # Play
        play_response = self.client.post(
            self.url,
            data=json.dumps(play_payload),
            content_type="application/json",
        )
        self.assertEqual(play_response.status_code, 200)

        # Pause
        pause_response = self.client.post(
            self.url,
            data=json.dumps(pause_payload),
            content_type="application/json",
        )
        self.assertEqual(pause_response.status_code, 200)

        paused_state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(paused_state)
        self.assertEqual(
            paused_state["status"], live_playback.PLAYBACK_STATUS_PAUSED,
        )
        self.assertEqual(paused_state["view_offset_seconds"], 721)

        # Stop
        stop_response = self.client.post(
            self.url,
            data=json.dumps(stop_payload),
            content_type="application/json",
        )
        self.assertEqual(stop_response.status_code, 200)

        stopped_state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(stopped_state)
        self.assertEqual(
            stopped_state["status"],
            live_playback.PLAYBACK_STATUS_STOPPED,
        )
