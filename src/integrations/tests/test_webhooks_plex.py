import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from app import live_playback
from app.models import (
    TV,
    Anime,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Status,
)
from integrations.webhooks.plex import PlexWebhookProcessor


class PlexWebhookTests(TestCase):
    """Tests for Plex webhook."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {
            "username": "testuser",
            "token": "test-token",
            "plex_usernames": "testuser",
        }
        self.user = get_user_model().objects.create_superuser(**self.credentials)
        self.user.anime_enabled = True
        self.user.music_enabled = True
        self.user.save()
        self.url = reverse("plex_webhook", kwargs={"token": "test-token"})
        self.fetch_mapping_patcher = patch(
            "integrations.webhooks.base.BaseWebhookProcessor._fetch_mapping_data",
            return_value={
                "3651": {
                    "mal_id": "849",
                },
                "anime_episode": {
                    "tvdb_id": 9350138,
                    "tvdb_season": 1,
                    "tvdb_epoffset": 0,
                    "mal_id": "52991",
                },
                "anime_movie": {
                    "tmdb_movie_id": 10494,
                    "mal_id": "437",
                },
            },
        )
        self.fetch_mapping_patcher.start()

        def fake_tv_with_seasons(media_id, season_numbers):
            media_id = str(media_id)
            seasons = {}
            for season_number in season_numbers:
                seasons[f"season/{season_number}"] = {
                    "image": "",
                    "episodes": [
                        {"episode_number": 1, "runtime": 30},
                        {"episode_number": 2, "runtime": 30},
                    ],
                }

            title = "Dummy"
            tvdb_id = 1
            if media_id in ("1668", "85987"):
                title = "Friends"
                tvdb_id = 303821
            elif media_id == "3946240":
                title = "Frieren: Beyond Journey's End"
                tvdb_id = 9350138
            elif media_id == "18664":
                title = "Cake Boss"
                tvdb_id = 107671

            related_seasons = [{"season_number": sn} for sn in season_numbers]
            return {
                "tvdb_id": tvdb_id,
                "title": title,
                "image": "",
                "related": {"seasons": related_seasons},
                **seasons,
            }

        self.tv_with_seasons_patcher = patch(
            "app.providers.tmdb.tv_with_seasons",
            side_effect=fake_tv_with_seasons,
        )
        self.tv_with_seasons_patcher.start()
        self.movie_patcher = patch(
            "app.providers.tmdb.movie",
            return_value={
                "title": "Dummy Movie",
                "image": "",
                "max_progress": 1,
            },
        )
        self.movie_patcher.start()

        def fake_get_media_metadata(media_type, media_id, source, season_numbers=None):
            if media_type == "tv_with_seasons":
                return fake_tv_with_seasons(media_id, season_numbers or [])
            if media_type == MediaTypes.SEASON.value:
                return fake_tv_with_seasons(media_id, season_numbers or [])
            if media_type == MediaTypes.ANIME.value:
                max_progress = 1 if str(media_id) == "437" else 12
                return {
                    "max_progress": max_progress,
                    "title": "Dummy Anime",
                    "image": "",
                }
            return {
                "max_progress": 1,
                "title": "Metadata Title",
                "image": "",
                "related": {
                    "seasons": [
                        {"season_number": 1, "image": ""},
                        {"season_number": 15, "image": ""},
                    ],
                },
                "season/1": {"episodes": [{"episode_number": 1, "runtime": 30}]},
            }

        self.metadata_patcher = patch(
            "app.providers.services.get_media_metadata",
            side_effect=fake_get_media_metadata,
        )
        self.metadata_patcher.start()

        # Avoid external MAL requests during anime handling
        self.mal_anime_patcher = patch(
            "app.providers.mal.anime",
            return_value={
                "title": "Dummy Anime",
                "image": "",
                "max_progress": 12,
            },
        )
        self.mal_anime_patcher.start()

    def tearDown(self):
        live_playback.clear_user_playback_state(self.user.id)
        self.fetch_mapping_patcher.stop()
        self.tv_with_seasons_patcher.stop()
        self.movie_patcher.stop()
        self.metadata_patcher.stop()
        self.mal_anime_patcher.stop()

    def test_invalid_token(self):
        """Test webhook with invalid token returns 401."""
        url = reverse("plex_webhook", kwargs={"token": "invalid-token"})
        response = self.client.post(url, data={}, content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_tv_episode_mark_played(self):
        """Test webhook handles TV episode mark played event."""
        payload = {
            "event": "media.scrobble",
            "Account": {
                "title": "testuser",
            },
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Friends",
                "index": 1,
                "parentIndex": 1,
                "Guid": [
                    {
                        "id": "imdb://tt0583459",
                    },
                    {
                        "id": "tmdb://85987",
                    },
                    {
                        "id": "tvdb://303821",
                    },
                ],
            },
        }

        data = {
            "payload": json.dumps(payload),
        }

        response = self.client.post(
            self.url,
            data=data,
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)

        # Verify objects were created
        tv_item = Item.objects.get(media_type=MediaTypes.TV.value, media_id="85987")
        self.assertEqual(tv_item.title, "Friends")

        tv = TV.objects.get(item=tv_item, user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

        season = Season.objects.get(
            item__media_id="85987",
            item__season_number=1,
        )
        self.assertEqual(season.status, Status.IN_PROGRESS.value)

        episode = Episode.objects.get(
            item__media_id="85987",
            item__season_number=1,
            item__episode_number=1,
        )
        self.assertIsNotNone(episode.end_date)

    def test_play_event_stores_live_playback_state(self):
        """Play events should create live playback state for the home card."""
        payload = {
            "event": "media.play",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Friends",
                "title": "The One with the Sonogram at the End",
                "index": 1,
                "parentIndex": 1,
                "ratingKey": "rk-episode-1",
                "duration": 2666000,
                "viewOffset": 1447000,
                "Guid": [
                    {"id": "imdb://tt0583459"},
                    {"id": "tmdb://85987"},
                    {"id": "tvdb://303821"},
                ],
            },
        }

        response = self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)

        state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(state)
        self.assertEqual(state["media_type"], MediaTypes.EPISODE.value)
        # media_id should be the TV *show* TMDB ID (resolved via TVDB/IMDB
        # find API), not the episode-level tmdb_id from the Plex GUIDs.
        self.assertEqual(state["media_id"], "1668")
        self.assertEqual(state["status"], live_playback.PLAYBACK_STATUS_PLAYING)
        self.assertEqual(state["season_number"], 1)
        self.assertEqual(state["episode_number"], 1)
        self.assertEqual(state["duration_seconds"], 2666)
        self.assertEqual(state["view_offset_seconds"], 1447)

    def test_pause_and_stop_events_update_live_playback_state(self):
        """Pause should keep card state; stop should transition to stopped with grace period."""
        play_payload = {
            "event": "media.play",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Friends",
                "title": "The One with the Sonogram at the End",
                "index": 1,
                "parentIndex": 1,
                "ratingKey": "rk-episode-2",
                "duration": 2666000,
                "viewOffset": 600000,
                "Guid": [
                    {"id": "imdb://tt0583459"},
                    {"id": "tmdb://85987"},
                    {"id": "tvdb://303821"},
                ],
            },
        }
        pause_payload = {
            "event": "media.pause",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Friends",
                "title": "The One with the Sonogram at the End",
                "index": 1,
                "parentIndex": 1,
                "ratingKey": "rk-episode-2",
                "duration": 2666000,
                "viewOffset": 721000,
                "Guid": [
                    {"id": "imdb://tt0583459"},
                    {"id": "tmdb://85987"},
                    {"id": "tvdb://303821"},
                ],
            },
        }
        stop_payload = {
            "event": "media.stop",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Friends",
                "title": "The One with the Sonogram at the End",
                "index": 1,
                "parentIndex": 1,
                "ratingKey": "rk-episode-2",
                "Guid": [
                    {"id": "imdb://tt0583459"},
                    {"id": "tmdb://85987"},
                    {"id": "tvdb://303821"},
                ],
            },
        }

        play_response = self.client.post(
            self.url,
            data={"payload": json.dumps(play_payload)},
            format="multipart",
        )
        self.assertEqual(play_response.status_code, 200)

        pause_response = self.client.post(
            self.url,
            data={"payload": json.dumps(pause_payload)},
            format="multipart",
        )
        self.assertEqual(pause_response.status_code, 200)

        paused_state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(paused_state)
        self.assertEqual(paused_state["status"], live_playback.PLAYBACK_STATUS_PAUSED)
        self.assertEqual(paused_state["view_offset_seconds"], 721)

        stop_response = self.client.post(
            self.url,
            data={"payload": json.dumps(stop_payload)},
            format="multipart",
        )
        self.assertEqual(stop_response.status_code, 200)
        # Stop now uses a grace period instead of immediate deletion,
        # so the state should still exist with "stopped" status.
        stopped_state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(stopped_state)
        self.assertEqual(
            stopped_state["status"],
            live_playback.PLAYBACK_STATUS_STOPPED,
        )

    @patch("app.providers.tmdb.search", return_value={"results": []})
    @patch("app.providers.tmdb.find")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_tv_episode_tmdb_episode_id_falls_back_to_find_lookup(
        self,
        mock_tv_with_seasons,
        mock_find,
        mock_tmdb_search,
    ):
        """Episode-level TMDB IDs should recover via TVDB/IMDB find lookup."""

        def fake_tv_with_seasons(media_id, season_numbers):
            if str(media_id) == "1515183":
                raise Exception("TMDB 404")
            if str(media_id) == "73586":
                return {
                    "tvdb_id": "361315",
                    "title": "Yellowstone",
                    "image": "",
                    "season/1": {
                        "image": "",
                        "episodes": [{"episode_number": 4, "runtime": 42}],
                    },
                    "related": {"seasons": [{"season_number": 1}]},
                }
            raise AssertionError(f"Unexpected TMDB ID requested: {media_id}")

        mock_tv_with_seasons.side_effect = fake_tv_with_seasons

        def fake_find(external_id, external_source):
            if external_source in {"tvdb_id", "imdb_id"}:
                return {
                    "tv_episode_results": [
                        {
                            "show_id": 73586,
                            "season_number": 1,
                            "episode_number": 4,
                        },
                    ],
                    "tv_results": [],
                }
            raise AssertionError(
                f"Unexpected find lookup: {external_source}={external_id}",
            )

        mock_find.side_effect = fake_find

        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Yellowstone (2018)",
                "title": "The Long Black Train",
                "index": 4,
                "parentIndex": 1,
                "Guid": [
                    {"id": "imdb://tt8075162"},
                    {"id": "tmdb://1515183"},
                    {"id": "tvdb://6725919"},
                ],
            },
        }

        response = self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)

        episode = Episode.objects.get(
            item__media_id="73586",
            item__season_number=1,
            item__episode_number=4,
        )
        self.assertIsNotNone(episode.end_date)

        self.assertEqual(str(mock_tv_with_seasons.call_args_list[0].args[0]), "1515183")
        self.assertEqual(str(mock_tv_with_seasons.call_args_list[1].args[0]), "73586")
        mock_find.assert_called()
        mock_tmdb_search.assert_not_called()

    def test_movie_mark_played(self):
        """Test webhook handles movie mark played event."""
        payload = {
            "event": "media.scrobble",
            "Account": {
                "title": "testuser",
            },
            "Metadata": {
                "type": "movie",
                "title": "The Matrix",
                "Guid": [
                    {
                        "id": "imdb://tt0133093",
                    },
                    {
                        "id": "tmdb://603",
                    },
                    {
                        "id": "tvdb://169",
                    },
                ],
            },
        }

        data = {
            "payload": json.dumps(payload),
        }

        response = self.client.post(
            self.url,
            data=data,
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)

        # Verify movie was created and marked as completed
        movie = Movie.objects.get(
            item__media_id="603",
            user=self.user,
        )
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.progress, 1)

    def test_movie_rating_webhook_uses_plex_user_rating_scale(self):
        """Ratings from Plex userRating should stay on a 0-10 scale."""
        payload = {
            "event": "media.rate",
            "Account": {
                "title": "testuser",
            },
            "Metadata": {
                "type": "movie",
                "title": "The Matrix",
                "userRating": 5,
                "Guid": [
                    {
                        "id": "tmdb://603",
                    },
                ],
            },
        }

        response = self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)

        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.score, 5)

    def test_anime_movie_mark_played(self):
        """Test webhook handles movie mark played event."""
        payload = {
            "event": "media.scrobble",
            "Account": {
                "title": "testuser",
            },
            "Metadata": {
                "type": "movie",
                "title": "Perfect Blue",
                "Guid": [
                    {
                        "id": "imdb://tt0156887",
                    },
                    {
                        "id": "tmdb://10494",
                    },
                    {
                        "id": "tvdb://3807",
                    },
                ],
            },
        }

        data = {
            "payload": json.dumps(payload),
        }

        response = self.client.post(
            self.url,
            data=data,
            format="multipart",
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
            "event": "media.scrobble",
            "Account": {
                "title": "testuser",
            },
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Frieren: Beyond Journey's End",
                "index": 1,
                "parentIndex": 1,
                "Guid": [
                    {
                        "id": "imdb://tt23861604",
                    },
                    {
                        "id": "tmdb://3946240",
                    },
                    {
                        "id": "tvdb://9350138",
                    },
                ],
            },
        }

        data = {
            "payload": json.dumps(payload),
        }

        response = self.client.post(
            self.url,
            data=data,
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)

        # Verify anime was created
        anime = Anime.objects.get(
            item__media_id="52991",
            user=self.user,
        )
        self.assertEqual(anime.progress, 1)

    def test_ignored_event_types(self):
        """Test webhook ignores irrelevant event types."""
        payload = {
            "event": "media.something_else",
            "Account": {
                "title": "testuser",
            },
            "Metadata": {
                "type": "movie",
                "title": "Movie",
                "Guid": [
                    {
                        "id": "imdb://tt12345",
                    },
                    {
                        "id": "tmdb://12345",
                    },
                    {
                        "id": "tvdb://12345",
                    },
                ],
            },
        }

        data = {
            "payload": json.dumps(payload),
        }

        response = self.client.post(
            self.url,
            data=data,
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Movie.objects.count(), 0)

    def test_missing_tmdb_id(self):
        """Test webhook handles missing TMDB ID gracefully."""
        payload = {
            "event": "media.scrobble",
            "Account": {
                "title": "testuser",
            },
            "Metadata": {
                "type": "movie",
                "title": "The Matrix",
                "Guid": [],
            },
        }
        data = {
            "payload": json.dumps(payload),
        }

        response = self.client.post(
            self.url,
            data=data,
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        # We now match via title fallback if IDs are missing
        self.assertEqual(Movie.objects.count(), 1)
        self.assertEqual(Movie.objects.first().item.title, "Dummy Movie")

    def test_repeated_watch(self):
        """Test webhook handles repeated watches."""
        payload = {
            "event": "media.scrobble",
            "Account": {
                "title": "testuser",
            },
            "Metadata": {
                "type": "movie",
                "title": "The Matrix",
                "Guid": [
                    {
                        "id": "imdb://tt0133093",
                    },
                    {
                        "id": "tmdb://603",
                    },
                    {
                        "id": "tvdb://169",
                    },
                ],
            },
        }

        data = {
            "payload": json.dumps(payload),
        }

        # First watch
        response = self.client.post(
            self.url,
            data=data,
            format="multipart",
        )

        # Second watch
        response = self.client.post(
            self.url,
            data=data,
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.filter(item__media_id="603")
        self.assertEqual(movie.count(), 2)
        self.assertEqual(movie[0].status, Status.COMPLETED.value)
        self.assertEqual(movie[1].status, Status.COMPLETED.value)

    @patch("integrations.webhooks.plex.music_scrobble.record_music_playback")
    def test_music_play_event(self, mock_scrobble):
        """Test Plex music play delegates to the scrobble service."""
        mock_scrobble.return_value = None
        payload = {
            "event": "media.play",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "track",
                "title": "Test Song",
                "parentTitle": "Test Album",
                "grandparentTitle": "Test Artist",
                "duration": 200000,
                "ratingKey": "987",
                "Guid": [
                    {"id": "musicbrainz://recording/00000000-1111-2222-3333-444444444444"},
                ],
            },
        }
        response = self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        mock_scrobble.assert_called_once()
        event = mock_scrobble.call_args[0][0]
        self.assertEqual(event.track_title, "Test Song")
        self.assertEqual(event.artist_name, "Test Artist")
        self.assertFalse(event.completed)
        self.assertEqual(
            event.external_ids.get("musicbrainz_recording"),
            "00000000-1111-2222-3333-444444444444",
        )

    @patch("integrations.webhooks.plex.music_scrobble.record_music_playback")
    def test_music_event_respects_user_setting(self, mock_scrobble):
        """Music webhooks are ignored when music is disabled for the user."""
        self.user.music_enabled = False
        self.user.save()
        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "track",
                "title": "Test Song",
                "parentTitle": "Test Album",
                "grandparentTitle": "Test Artist",
                "duration": 200000,
            },
        }

        response = self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        mock_scrobble.assert_not_called()

    def test_username_matching(self):
        """Test Plex username matching functionality."""
        test_cases = [
            # stored, incoming, should_match
            ("testuser", "testuser", True),  # Exact match
            ("testuser", "TestUser", True),  # Case insensitive
            ("testuser", " testuser ", True),  # Whitespace handling
            ("testuser", "testuser2", False),  # Different username
            ("testuser1,testuser2", "testuser1", True),  # First in list
            ("testuser1, testuser2", "testuser1", True),  # comma and space
            ("testuser1,testuser2", "testuser3", False),  # Not in list
        ]

        base_payload = {
            "event": "media.scrobble",
            "Metadata": {
                "type": "movie",
                "title": "Test Movie",
                "Guid": [{"id": "tmdb://123"}],
            },
        }

        for i, (stored_usernames, incoming_username, should_match) in enumerate(
            test_cases,
        ):
            with self.subTest(
                f"Case {i + 1}: {stored_usernames} vs {incoming_username}",
            ):
                self.user.plex_usernames = stored_usernames
                self.user.save()
                payload = base_payload.copy()
                payload["Account"] = {"title": incoming_username}

                response = self.client.post(
                    self.url,
                    data={"payload": json.dumps(payload)},
                    format="multipart",
                )

                if should_match:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(Movie.objects.count(), 1)
                    Movie.objects.all().delete()  # Clean up for next test
                else:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(Movie.objects.count(), 0)

    def test_anime_episode_anidb_guid_mark_played(self):
        """Test webhook handles anime episode with anidb guid."""
        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "index": 1,
                "parentIndex": 1,
                "guid": "com.plexapp.agents.hama://anidb-3651/1/1?lang=en"
            },
        }

        data = {"payload": json.dumps(payload)}

        response = self.client.post(
            self.url,
            data=data,
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)

        # Verify anime was created and marked as in progress
        anime = Anime.objects.get(
            item__media_id="849",
            user=self.user,
        )
        self.assertEqual(anime.status, Status.IN_PROGRESS.value)
        self.assertEqual(anime.progress, 1)

    def test_extract_external_ids(self):
        """Test extraction of external IDs from Plex webhook payload."""
        # Setup test payload
        payload = {
            "Metadata": {
                "Guid": [
                    {"id": "tmdb://12345"},
                    {"id": "imdb://tt67890"},
                    {"id": "tvdb://98765"},
                ],
            },
        }

        # Execute
        result = PlexWebhookProcessor()._extract_external_ids(payload)

        # Assert
        expected = {
            "tmdb_id": "12345",
            "imdb_id": "tt67890",
            "tvdb_id": "98765",
            "plex_guid": None,
            "anidb_id": None,
        }

        self.assertEqual(result, expected)

    def test_extract_external_ids_from_guid_string(self):
        """Test extraction of external IDs from Plex webhook payload."""
        # Setup test payload
        payload = {
            "Metadata": {
                "guid": "com.plexapp.agents.hama://anidb-12345/1/1?lang=en",
            },
        }

        # Execute
        result = PlexWebhookProcessor()._extract_external_ids(payload)

        # Assert
        expected = {
            "tmdb_id": None,
            "imdb_id": None,
            "tvdb_id": None,
            "plex_guid": None,
            "anidb_id": "12345",
        }

        self.assertEqual(result, expected)

    def test_extract_external_ids_missing_data(self):
        """Test handling of missing or empty data."""
        payload = {"Metadata": {"Guid": []}}

        result = PlexWebhookProcessor()._extract_external_ids(payload)

        expected = {
            "tmdb_id": None,
            "imdb_id": None,
            "tvdb_id": None,
            "plex_guid": None,
            "anidb_id": None,
        }
        self.assertEqual(result, expected)

    def test_extract_external_ids_agent_formats(self):
        """Test extraction from Plex agent GUID formats."""
        payload = {
            "Metadata": {
                "Guid": [
                    {"id": "com.plexapp.agents.themoviedb://12345?lang=en"},
                    {"id": "com.plexapp.agents.imdb://tt67890?lang=en"},
                    {"id": "com.plexapp.agents.thetvdb://98765/1/1?lang=en"},
                ],
            },
        }

        result = PlexWebhookProcessor()._extract_external_ids(payload)

        expected = {
            "tmdb_id": "12345",
            "imdb_id": "tt67890",
            "tvdb_id": "98765",
            "plex_guid": None,
            "anidb_id": None,
        }
        self.assertEqual(result, expected)

    @patch("app.providers.tmdb.tv_with_seasons")
    @patch("app.providers.tmdb.search")
    def test_tv_episode_with_plex_guid_fallback(
        self,
        mock_tmdb_search,
        mock_tv_with_seasons,
    ):
        """Test webhook resolves plex:// GUID via TMDB search."""
        mock_tmdb_search.return_value = {
            "results": [
                {
                    "media_id": "18664",
                    "title": "Cake Boss",
                },
            ],
        }
        mock_tv_with_seasons.return_value = {
            "tvdb_id": "107671",
            "title": "Cake Boss",
            "image": "",
            "season/15": {
                "image": "",
                "episodes": [
                    {"episode_number": 2, "runtime": 30},
                ],
            },
            "related": {"seasons": [{"season_number": 15}]},
        }

        payload = {
            "event": "media.scrobble",
            "Account": {
                "title": "testuser",
            },
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Cake Boss",
                "index": 2,
                "parentIndex": 15,
                "guid": "plex://episode/66abff6b88824f5224a8b6db",
            },
        }

        data = {
            "payload": json.dumps(payload),
        }

        response = self.client.post(
            self.url,
            data=data,
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)

        episode = Episode.objects.get(
            item__media_id="18664",
            item__season_number=15,
            item__episode_number=2,
        )
        self.assertIsNotNone(episode.end_date)
        # Call might happen via title search if GUID resolution falls back
        mock_tmdb_search.assert_called()


class LivePlaybackScrobbleClearingTests(TestCase):
    """Unit tests for scrobble-based now-playing card expiry calculation."""

    def setUp(self):
        """Set up a test user and clear any cached playback state."""
        self.client = Client()
        self.credentials = {
            "username": "testuser",
            "token": "test-token",
            "plex_usernames": "testuser",
        }
        self.user = get_user_model().objects.create_superuser(**self.credentials)
        self.url = reverse("plex_webhook", kwargs={"token": "test-token"})
        live_playback.clear_user_playback_state(self.user.id)

    def test_scrobble_with_duration_and_offset_sets_calculated_expiry(self):
        """Scrobble with known duration/offset sets expiry from remaining time."""
        now = timezone.now()
        duration = 2666  # seconds (~44 min episode)
        offset = 2265    # ~85% through -> 401 seconds remaining

        live_playback.apply_playback_event(
            user_id=self.user.id,
            event_type="media.scrobble",
            playback_media_type="episode",
            media_id="1668",
            duration_seconds=duration,
            view_offset_seconds=offset,
        )

        now_ts = int(now.timestamp())
        remaining = duration - offset
        buffer = live_playback.PLAYBACK_SCROBBLE_BUFFER_SECONDS
        expected = now_ts + remaining + buffer
        raw = cache.get(f"{live_playback.PLAYBACK_CACHE_PREFIX}:{self.user.id}")
        self.assertIsNotNone(raw)
        self.assertAlmostEqual(raw["scrobble_expires_at_ts"], expected, delta=5)

    def test_scrobble_without_duration_uses_fallback_expiry(self):
        """Scrobble with no duration uses the fallback grace period."""
        now = timezone.now()

        live_playback.apply_playback_event(
            user_id=self.user.id,
            event_type="media.scrobble",
            playback_media_type="movie",
            media_id="550",
            duration_seconds=None,
            view_offset_seconds=None,
        )

        now_ts = int(now.timestamp())
        raw = cache.get(f"{live_playback.PLAYBACK_CACHE_PREFIX}:{self.user.id}")
        self.assertIsNotNone(raw)
        expected = now_ts + live_playback.PLAYBACK_SCROBBLE_FALLBACK_SECONDS
        self.assertAlmostEqual(raw["scrobble_expires_at_ts"], expected, delta=5)

    def test_scrobble_state_clears_after_content_ends(self):
        """State clears when get_user_playback_state is called past scrobble expiry."""
        now = timezone.now()
        duration = 2666
        offset = 2265
        remaining = duration - offset  # 401 seconds

        live_playback.apply_playback_event(
            user_id=self.user.id,
            event_type="media.scrobble",
            playback_media_type="episode",
            media_id="1668",
            duration_seconds=duration,
            view_offset_seconds=offset,
        )

        buffer = live_playback.PLAYBACK_SCROBBLE_BUFFER_SECONDS
        before_expiry = now + timedelta(seconds=remaining + buffer - 1)
        self.assertIsNotNone(
            live_playback.get_user_playback_state(self.user.id, now=before_expiry),
        )

        after_expiry = now + timedelta(seconds=remaining + buffer + 1)
        self.assertIsNone(
            live_playback.get_user_playback_state(self.user.id, now=after_expiry),
        )

    def test_library_filter_rejects_unselected_library(self):
        """Webhook events should be ignored when library is not selected."""
        self.user.plex_webhook_libraries = ["machine-a::1"]
        self.user.save(update_fields=["plex_webhook_libraries"])

        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Server": {"uuid": "machine-a"},
            "Metadata": {
                "type": "episode",
                "librarySectionID": "2",
                "grandparentTitle": "Friends",
                "index": 1,
                "parentIndex": 1,
                "Guid": [{"id": "tmdb://85987"}],
            },
        }

        with patch.object(PlexWebhookProcessor, "_process_media") as mock_process_media:
            response = self.client.post(
                self.url,
                data={"payload": json.dumps(payload)},
                format="multipart",
            )

        self.assertEqual(response.status_code, 200)
        mock_process_media.assert_not_called()

    def test_library_filter_accepts_selected_library(self):
        """Webhook events should be accepted when library is selected."""
        self.user.plex_webhook_libraries = ["machine-a::2"]
        self.user.save(update_fields=["plex_webhook_libraries"])

        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Server": {"uuid": "machine-a"},
            "Metadata": {
                "type": "episode",
                "librarySectionID": "2",
                "grandparentTitle": "Friends",
                "index": 1,
                "parentIndex": 1,
                "Guid": [{"id": "tmdb://85987"}],
            },
        }

        with patch.object(PlexWebhookProcessor, "_process_media") as mock_process_media:
            response = self.client.post(
                self.url,
                data={"payload": json.dumps(payload)},
                format="multipart",
            )

        self.assertEqual(response.status_code, 200)
        mock_process_media.assert_called_once()
