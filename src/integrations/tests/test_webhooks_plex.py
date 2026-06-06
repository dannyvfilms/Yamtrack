import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
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
    ProviderMetadataStatus,
    Season,
    Status,
)
from integrations.models import PlexAccount
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
            "integrations.webhooks.anime_mappings.fetch_mapping_data",
            return_value={
                "anidb:3651:R": {
                    "mal:849": {"1-": "1-"},
                },
                "tvdb_show:9350138:s1": {
                    "mal:52991": {"1-": "1-"},
                },
                "tmdb_movie:10494": {
                    "mal:437": {},
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
        def fake_tmdb_find(external_id, external_source):
            key = (str(external_id), external_source)
            if key in {
                ("303821", "tvdb_id"),
                ("tt0583459", "imdb_id"),
            }:
                return {
                    "tv_episode_results": [
                        {
                            "show_id": 1668,
                            "season_number": 1,
                            "episode_number": 1,
                        },
                    ],
                    "tv_results": [],
                }
            return {"tv_episode_results": [], "tv_results": [], "movie_results": []}

        self.tmdb_find_patcher = patch(
            "app.providers.tmdb.find",
            side_effect=fake_tmdb_find,
        )
        self.tmdb_find_patcher.start()
        self.movie_patcher = patch(
            "app.providers.tmdb.movie",
            return_value={
                "title": "Dummy Movie",
                "image": "",
                "max_progress": 1,
            },
        )
        self.movie_patcher.start()

        def fake_tmdb_search(media_type, query, page):
            if (
                media_type == MediaTypes.MOVIE.value
                and str(query).casefold() == "the matrix"
                and page == 1
            ):
                return {
                    "results": [
                        {
                            "media_id": "603",
                            "title": "The Matrix",
                        },
                    ],
                }
            return {"results": []}

        self.tmdb_search_patcher = patch(
            "app.providers.tmdb.search",
            side_effect=fake_tmdb_search,
        )
        self.tmdb_search_patcher.start()

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
        self.tmdb_find_patcher.stop()
        self.tmdb_search_patcher.stop()
        self.movie_patcher.stop()
        self.metadata_patcher.stop()
        self.mal_anime_patcher.stop()

    def _post_payload(self, payload):
        return self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

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
        # TVDB-first resolution maps tvdb://303821 → show TMDB ID 1668 (Friends).
        # The episode-level tmdb://85987 from Plex GUIDs is bypassed correctly.
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

    def test_movie_short_stop_clears_in_progress_row_and_playback_state(self):
        """Play events don't create rows; stop with viewOffset < 60s also creates nothing."""
        play_payload = {
            "event": "media.play",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "movie",
                "title": "The Matrix",
                "ratingKey": "rk-movie-1",
                "duration": 8100000,
                "viewOffset": 30000,
                "Guid": [
                    {"id": "tmdb://603"},
                ],
            },
        }
        stop_payload = {
            "event": "media.stop",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "movie",
                "title": "The Matrix",
                "ratingKey": "rk-movie-1",
                "duration": 8100000,
                "viewOffset": 30000,
                "Guid": [
                    {"id": "tmdb://603"},
                ],
            },
        }

        play_response = self._post_payload(play_payload)
        self.assertEqual(play_response.status_code, 200)
        self.assertFalse(
            Movie.objects.filter(
                item__media_id="603",
                user=self.user,
                status=Status.IN_PROGRESS.value,
            ).exists(),
        )

        stop_response = self._post_payload(stop_payload)
        self.assertEqual(stop_response.status_code, 200)

        self.assertFalse(
            Movie.objects.filter(
                item__media_id="603",
                user=self.user,
                status=Status.IN_PROGRESS.value,
            ).exists(),
        )
        stopped_state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(stopped_state)
        self.assertEqual(stopped_state["status"], live_playback.PLAYBACK_STATUS_STOPPED)

    def test_short_stop_only_applies_during_the_first_minute_of_playback(self):
        """A stop with viewOffset < 60s never creates an in-progress row."""
        play_payload = {
            "event": "media.play",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "movie",
                "title": "The Matrix",
                "ratingKey": "rk-movie-aged-start",
                "duration": 8100000,
                "viewOffset": 30000,
                "Guid": [
                    {"id": "tmdb://603"},
                ],
            },
        }
        stop_payload = {
            "event": "media.stop",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "movie",
                "title": "The Matrix",
                "ratingKey": "rk-movie-aged-start",
                "duration": 8100000,
                "viewOffset": 30000,
                "Guid": [
                    {"id": "tmdb://603"},
                ],
            },
        }

        play_response = self._post_payload(play_payload)
        self.assertEqual(play_response.status_code, 200)

        state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(state)
        state["started_at_ts"] = int(timezone.now().timestamp()) - 61
        live_playback.set_user_playback_state(self.user.id, state)

        stop_response = self._post_payload(stop_payload)
        self.assertEqual(stop_response.status_code, 200)

        self.assertFalse(
            Movie.objects.filter(
                item__media_id="603",
                user=self.user,
                status=Status.IN_PROGRESS.value,
            ).exists(),
        )
        stopped_state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(stopped_state)
        self.assertEqual(stopped_state["status"], live_playback.PLAYBACK_STATUS_STOPPED)

    def test_episode_short_stop_clears_tv_and_season_rows(self):
        """Play events don't create rows; stop with viewOffset < 60s creates nothing."""
        play_payload = {
            "event": "media.play",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Friends",
                "title": "The One with the Sonogram at the End",
                "index": 1,
                "parentIndex": 1,
                "ratingKey": "rk-episode-short",
                "duration": 2666000,
                "viewOffset": 30000,
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
                "ratingKey": "rk-episode-short",
                "duration": 2666000,
                "viewOffset": 30000,
                "Guid": [
                    {"id": "imdb://tt0583459"},
                    {"id": "tmdb://85987"},
                    {"id": "tvdb://303821"},
                ],
            },
        }

        play_response = self._post_payload(play_payload)
        self.assertEqual(play_response.status_code, 200)
        self.assertFalse(
            TV.objects.filter(item__media_id="85987", user=self.user).exists(),
        )
        self.assertFalse(
            Season.objects.filter(
                item__media_id="85987",
                item__season_number=1,
                user=self.user,
            ).exists(),
        )

        stop_response = self._post_payload(stop_payload)
        self.assertEqual(stop_response.status_code, 200)

        self.assertFalse(
            TV.objects.filter(item__media_id="85987", user=self.user).exists(),
        )
        self.assertFalse(
            Season.objects.filter(
                item__media_id="85987",
                item__season_number=1,
                user=self.user,
            ).exists(),
        )
        stopped_state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(stopped_state)
        self.assertEqual(stopped_state["status"], live_playback.PLAYBACK_STATUS_STOPPED)

    def test_pause_and_stop_events_update_live_playback_state(self):
        """Pause keeps card state; stop with sufficient viewOffset creates DB rows and transitions to stopped."""
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
                "viewOffset": 721000,
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
        # TVDB-first resolution → show tracked under correct show TMDB ID 1668.
        self.assertTrue(
            TV.objects.filter(item__media_id="1668", user=self.user).exists(),
        )
        self.assertTrue(
            Season.objects.filter(
                item__media_id="1668",
                item__season_number=1,
                user=self.user,
            ).exists(),
        )

    @patch(
        "integrations.webhooks.base.BaseWebhookProcessor._queue_collection_metadata_update_for_tv",
    )
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_tv_special_payload_fallback_is_cached_for_season_page(
        self,
        mock_tv_with_seasons,
        mock_queue_update,
    ):
        """Season 0 fallback from Plex should be cached as TMDB-shaped metadata."""
        cache.clear()
        mock_queue_update.return_value = None
        mock_tv_with_seasons.return_value = {
            "media_id": "114410",
            "title": "Chainsaw Man",
            "image": "https://example.com/show.jpg",
            "synopsis": "A test show",
            "genres": ["Animation"],
            "tvdb_id": "10196540",
            "external_links": {
                "TVDB": "https://www.thetvdb.com/dereferrer/series/10196540",
            },
            "related": {"seasons": [{"season_number": 1}]},
        }

        payload = {
            "event": "media.resume",
            "Metadata": {
                "title": "A Special Episode",
                "summary": "Webhook fallback episode metadata.",
                "duration": 720000,
                "originallyAvailableAt": "2024-05-01",
            },
        }

        PlexWebhookProcessor()._handle_tv_episode(
            "114410",
            0,
            1,
            payload,
            self.user,
        )

        cached_season = cache.get("tmdb_season_114410_0")
        cached_tv = cache.get("tmdb_tv_114410")

        self.assertIsNotNone(cached_season)
        self.assertEqual(cached_season["season_title"], "Specials")
        self.assertEqual(cached_season["episodes"][0]["name"], "A Special Episode")
        self.assertEqual(
            cached_season["source_url"],
            "https://www.thetvdb.com/dereferrer/series/10196540",
        )
        self.assertTrue(
            any(
                season.get("season_number") == 0
                for season in cached_tv["related"]["seasons"]
            ),
        )

    @patch("app.providers.tmdb.search", return_value={"results": []})
    @patch("app.providers.tmdb.find")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_missing_season_recovery_reattaches_history_to_resolved_show(
        self,
        mock_tv_with_seasons,
        mock_find,
        mock_tmdb_search,
    ):
        """Missing seasons should recover before creating TV/season rows."""

        def fake_tv_with_seasons(media_id, season_numbers):
            if str(media_id) == "203934":
                return {
                    "title": "MasterChef USA",
                    "image": "",
                    "tvdb_id": "wrong-tvdb",
                    "synopsis": "Wrong show metadata.",
                    "external_links": {},
                    "related": {"seasons": [{"season_number": 15}]},
                }
            if str(media_id) == "302124":
                return {
                    "title": "MasterChef",
                    "image": "",
                    "tvdb_id": "397849",
                    "synopsis": "Recovered show metadata.",
                    "external_links": {},
                    "related": {"seasons": [{"season_number": 16}]},
                    "season/16": {
                        "image": "",
                        "episodes": [{"episode_number": 1, "runtime": 42}],
                    },
                }
            raise AssertionError(f"Unexpected TMDB ID requested: {media_id}")

        mock_tv_with_seasons.side_effect = fake_tv_with_seasons
        mock_find.return_value = {
            "tv_episode_results": [
                {
                    "show_id": 302124,
                    "season_number": 16,
                    "episode_number": 1,
                },
            ],
            "tv_results": [],
        }

        payload = {
            "event": "media.scrobble",
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "MasterChef",
                "parentTitle": "Season 16",
                "title": "Auditions: Europe (1)",
                "summary": "Recovered episode metadata.",
                "duration": 2520000,
                "originallyAvailableAt": "2026-04-15",
                "index": 1,
                "parentIndex": 16,
                "Guid": [
                    {"id": "imdb://tt1234567"},
                    {"id": "tmdb://203934"},
                    {"id": "tvdb://397849"},
                ],
            },
        }

        PlexWebhookProcessor()._handle_tv_episode(
            "203934",
            16,
            1,
            payload,
            self.user,
        )

        self.assertTrue(
            TV.objects.filter(item__media_id="302124", user=self.user).exists(),
        )
        self.assertFalse(
            TV.objects.filter(item__media_id="203934", user=self.user).exists(),
        )
        self.assertTrue(
            Episode.objects.filter(
                item__media_id="302124",
                item__season_number=16,
                item__episode_number=1,
            ).exists(),
        )
        self.assertFalse(
            Episode.objects.filter(
                item__media_id="203934",
                item__season_number=16,
                item__episode_number=1,
            ).exists(),
        )
        mock_tmdb_search.assert_not_called()

    @patch("app.providers.tmdb.search", return_value={"results": []})
    @patch(
        "app.providers.tmdb.find",
        return_value={"tv_episode_results": [], "tv_results": []},
    )
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_missing_non_special_season_persists_local_only_flag(
        self,
        mock_tv_with_seasons,
        _mock_find,
        _mock_tmdb_search,
    ):
        """Unresolved missing seasons should stay visible but flagged as local-only."""
        mock_tv_with_seasons.return_value = {
            "title": "MasterChef USA",
            "image": "",
            "tvdb_id": "wrong-tvdb",
            "synopsis": "Wrong show metadata.",
            "external_links": {},
            "related": {"seasons": [{"season_number": 15}]},
        }

        payload = {
            "event": "media.scrobble",
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "MasterChef",
                "parentTitle": "Season 16",
                "title": "Auditions: Europe (1)",
                "summary": "Fallback episode metadata.",
                "duration": 2520000,
                "originallyAvailableAt": "2026-04-15",
                "index": 1,
                "parentIndex": 16,
                "Guid": [
                    {"id": "imdb://tt1234567"},
                    {"id": "tmdb://203934"},
                    {"id": "tvdb://397849"},
                ],
            },
        }

        PlexWebhookProcessor()._handle_tv_episode(
            "203934",
            16,
            1,
            payload,
            self.user,
        )

        season_item = Item.objects.get(
            media_id="203934",
            media_type=MediaTypes.SEASON.value,
            season_number=16,
        )
        self.assertEqual(
            season_item.provider_metadata_status,
            ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value,
        )

    @patch("app.providers.tmdb.tv_with_seasons")
    def test_provider_backed_season_webhook_clears_local_only_flag(
        self,
        mock_tv_with_seasons,
    ):
        """A later provider-backed season fetch should clear the local-only flag."""
        tv_item = Item.objects.create(
            media_id="203934",
            source="tmdb",
            media_type=MediaTypes.TV.value,
            title="MasterChef USA",
            image="",
        )
        related_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="203934",
            source="tmdb",
            media_type=MediaTypes.SEASON.value,
            season_number=16,
            title="MasterChef USA",
            image="",
            provider_metadata_status=(
                ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
            ),
        )
        Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=related_tv,
            status=Status.IN_PROGRESS.value,
        )
        mock_tv_with_seasons.return_value = {
            "title": "MasterChef USA",
            "image": "",
            "tvdb_id": "397849",
            "synopsis": "Recovered show metadata.",
            "external_links": {},
            "related": {"seasons": [{"season_number": 16}]},
            "season/16": {
                "image": "",
                "episodes": [{"episode_number": 1, "runtime": 42}],
            },
        }
        payload = {
            "event": "media.scrobble",
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "MasterChef USA",
                "parentTitle": "Season 16",
                "title": "Auditions: Europe (1)",
                "duration": 2520000,
                "originallyAvailableAt": "2026-04-15",
                "index": 1,
                "parentIndex": 16,
                "Guid": [
                    {"id": "imdb://tt1234567"},
                    {"id": "tmdb://203934"},
                    {"id": "tvdb://397849"},
                ],
            },
        }

        PlexWebhookProcessor()._handle_tv_episode(
            "203934",
            16,
            1,
            payload,
            self.user,
        )

        season_item.refresh_from_db()
        self.assertEqual(season_item.provider_metadata_status, "")

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

        # TVDB-first resolution skips the episode-level tmdb://1515183 and resolves
        # the show ID directly via TVDB: fake_find returns show_id=73586 for tvdb_id.
        self.assertEqual(str(mock_tv_with_seasons.call_args_list[0].args[0]), "73586")
        mock_find.assert_called()
        mock_tmdb_search.assert_not_called()

    @patch("app.providers.tmdb.find")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_tv_episode_tmdb_episode_id_collision_prefers_find_resolved_show(
        self,
        mock_tv_with_seasons,
        mock_find,
    ):
        """Episode-level TMDB IDs should not attach history to an unrelated valid show."""

        def fake_tv_with_seasons(media_id, season_numbers):
            if str(media_id) == "62085":
                return {
                    "tvdb_id": "999999",
                    "title": "Shades of Guilt",
                    "image": "",
                    "season/1": {
                        "image": "",
                        "episodes": [{"episode_number": 1, "runtime": 42}],
                    },
                    "related": {"seasons": [{"season_number": 1}]},
                }
            if str(media_id) == "1396":
                return {
                    "tvdb_id": "81189",
                    "title": "Breaking Bad",
                    "image": "",
                    "season/1": {
                        "image": "",
                        "episodes": [{"episode_number": 1, "runtime": 42}],
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
                            "show_id": 1396,
                            "season_number": 1,
                            "episode_number": 1,
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
                "grandparentTitle": "Breaking Bad",
                "title": "Pilot",
                "index": 1,
                "parentIndex": 1,
                "Guid": [
                    {"id": "imdb://tt0959621"},
                    {"id": "tmdb://62085"},
                    {"id": "tvdb://349232"},
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
            item__media_id="1396",
            item__season_number=1,
            item__episode_number=1,
        )
        self.assertIsNotNone(episode.end_date)
        self.assertFalse(
            Episode.objects.filter(
                item__media_id="62085",
                item__season_number=1,
                item__episode_number=1,
            ).exists(),
        )
        # TVDB-first resolution: fake_find maps tvdb://349232 → show_id=1396 directly.
        # The collision TMDB ID 62085 is never attempted as a show lookup.
        self.assertEqual(str(mock_tv_with_seasons.call_args_list[0].args[0]), "1396")

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

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.providers.tvdb.tv")
    @patch("app.providers.tmdb.resolve_tvdb_id_for_tmdb_show", return_value="900001")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_new_tv_episode_routes_to_anime_when_tvdb_confirms_anime(
        self,
        mock_tv_with_seasons,
        mock_resolve_tvdb_id,
        mock_tvdb_tv,
    ):
        """Brand-new TV shows should route to Anime when TVDB classifies them as Anime."""
        mock_tv_with_seasons.return_value = {
            "title": "Anime Candidate",
            "image": "",
            "tvdb_id": None,
            "related": {"seasons": [{"season_number": 1}]},
            "season/1": {
                "image": "",
                "episodes": [{"episode_number": 1, "runtime": 24}],
            },
        }
        mock_tvdb_tv.return_value = {
            "title": "Anime Candidate",
            "image": "",
            "provider_external_ids": {"mal_id": "52991"},
            "genres": ["Action", "Anime"],
            "external_links": {
                "TVDB": "https://www.thetvdb.com/dereferrer/series/900001",
            },
        }

        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Anime Candidate",
                "index": 1,
                "parentIndex": 1,
                "Guid": [{"id": "tmdb://777777"}],
            },
        }

        response = self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Anime.objects.filter(item__media_id="52991", user=self.user).exists(),
        )
        self.assertFalse(
            TV.objects.filter(item__media_id="777777", user=self.user).exists(),
        )
        mock_resolve_tvdb_id.assert_called_once_with("777777", mock_tv_with_seasons.return_value)
        mock_tvdb_tv.assert_called_once_with("900001")

    @patch("app.providers.tmdb.resolve_tvdb_id_for_tmdb_show")
    @patch("app.providers.tvdb.tv")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_new_tv_episode_stays_tv_when_anime_is_disabled(
        self,
        mock_tv_with_seasons,
        mock_tvdb_tv,
        mock_resolve_tvdb_id,
    ):
        """Anime routing should not run when the user has Anime disabled."""
        self.user.anime_enabled = False
        self.user.save(update_fields=["anime_enabled"])
        mock_tv_with_seasons.return_value = {
            "title": "Anime Candidate",
            "image": "",
            "tvdb_id": None,
            "related": {"seasons": [{"season_number": 1}]},
            "season/1": {
                "image": "",
                "episodes": [{"episode_number": 1, "runtime": 24}],
            },
        }

        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Anime Candidate",
                "index": 1,
                "parentIndex": 1,
                "Guid": [{"id": "tmdb://777778"}],
            },
        }

        response = self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            TV.objects.filter(item__media_id="777778", user=self.user).exists(),
        )
        self.assertFalse(
            Anime.objects.filter(item__media_id="52991", user=self.user).exists(),
        )
        mock_resolve_tvdb_id.assert_not_called()
        mock_tvdb_tv.assert_not_called()

    @override_settings(TVDB_API_KEY="")
    @patch("app.providers.tmdb.resolve_tvdb_id_for_tmdb_show")
    @patch("app.providers.tvdb.tv")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_new_tv_episode_stays_tv_when_tvdb_is_disabled(
        self,
        mock_tv_with_seasons,
        mock_tvdb_tv,
        mock_resolve_tvdb_id,
    ):
        """Anime routing should not run when TVDB is not configured."""
        mock_tv_with_seasons.return_value = {
            "title": "Anime Candidate",
            "image": "",
            "tvdb_id": None,
            "related": {"seasons": [{"season_number": 1}]},
            "season/1": {
                "image": "",
                "episodes": [{"episode_number": 1, "runtime": 24}],
            },
        }

        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Anime Candidate",
                "index": 1,
                "parentIndex": 1,
                "Guid": [{"id": "tmdb://777779"}],
            },
        }

        response = self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            TV.objects.filter(item__media_id="777779", user=self.user).exists(),
        )
        self.assertFalse(
            Anime.objects.filter(item__media_id="52991", user=self.user).exists(),
        )
        mock_resolve_tvdb_id.assert_not_called()
        mock_tvdb_tv.assert_not_called()

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.providers.tvdb.tv")
    @patch("app.providers.tmdb.resolve_tvdb_id_for_tmdb_show", return_value="900002")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_new_tv_episode_stays_tv_when_tvdb_genre_is_not_anime(
        self,
        mock_tv_with_seasons,
        mock_resolve_tvdb_id,
        mock_tvdb_tv,
    ):
        """Anime routing should fall back to TV when TVDB does not confirm Anime."""
        mock_tv_with_seasons.return_value = {
            "title": "Drama Candidate",
            "image": "",
            "tvdb_id": None,
            "related": {"seasons": [{"season_number": 1}]},
            "season/1": {
                "image": "",
                "episodes": [{"episode_number": 1, "runtime": 24}],
            },
        }
        mock_tvdb_tv.return_value = {
            "title": "Drama Candidate",
            "image": "",
            "provider_external_ids": {"mal_id": "52992"},
            "genres": ["Drama"],
            "external_links": {
                "TVDB": "https://www.thetvdb.com/dereferrer/series/900002",
            },
        }

        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Drama Candidate",
                "index": 1,
                "parentIndex": 1,
                "Guid": [{"id": "tmdb://777780"}],
            },
        }

        response = self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            TV.objects.filter(item__media_id="777780", user=self.user).exists(),
        )
        self.assertFalse(
            Anime.objects.filter(item__media_id="52992", user=self.user).exists(),
        )
        mock_resolve_tvdb_id.assert_called_once_with("777780", mock_tv_with_seasons.return_value)
        mock_tvdb_tv.assert_called_once_with("900002")

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

    def test_account_id_matching_accepts_connected_plex_owner(self):
        """Webhook user matching should accept the connected Plex account ID."""
        PlexAccount.objects.create(
            user=self.user,
            plex_token="token",
            plex_username="DannyVFilms",
            plex_account_id="4441952",
        )
        self.user.plex_usernames = ""
        self.user.save(update_fields=["plex_usernames"])

        payload = {
            "event": "media.scrobble",
            "Account": {
                "title": "managed-user",
                "id": "4441952",
            },
            "Metadata": {
                "type": "movie",
                "title": "Test Movie",
                "Guid": [{"id": "tmdb://123"}],
            },
        }

        response = self.client.post(
            self.url,
            data={"payload": json.dumps(payload)},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Movie.objects.count(), 1)

    def test_anime_episode_anidb_guid_mark_played(self):
        """Test webhook handles anime episode with anidb guid."""
        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "index": 1,
                "parentIndex": 1,
                "guid": "com.plexapp.agents.hama://anidb-3651/1/1?lang=en",
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

    @patch("app.providers.tmdb.search", return_value={"results": []})
    @patch("app.providers.tmdb.find")
    def test_resolve_external_ids_prefers_find_for_tvdb_guid(
        self,
        mock_find,
        mock_tmdb_search,
    ):
        """TVDB GUID resolution should use TMDB find results before title search."""

        def fake_find(external_id, external_source):
            self.assertEqual(str(external_id), "349232")
            self.assertEqual(external_source, "tvdb_id")
            return {
                "tv_episode_results": [
                    {
                        "show_id": 1396,
                        "season_number": 1,
                        "episode_number": 1,
                    },
                ],
                "tv_results": [],
                "movie_results": [],
            }

        mock_find.side_effect = fake_find

        payload = {
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Breaking Bad",
                "Guid": [
                    {"id": "tvdb://349232"},
                ],
            },
        }

        ids = PlexWebhookProcessor().resolve_external_ids(payload)

        self.assertEqual(ids["tmdb_id"], "1396")
        mock_tmdb_search.assert_not_called()

    def test_recover_tv_show_check_ignores_non_payload_tmdb_ids(self):
        """Resolved TMDB IDs should not be treated like raw Plex TMDB GUIDs."""
        payload = {
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Chainsaw Man",
                "guid": "plex://episode/66abff6b88824f5224a8b6db",
            },
        }
        ids = {
            "tmdb_id": "114410",
            "imdb_id": None,
            "tvdb_id": "10196540",
            "plex_guid": "episode/66abff6b88824f5224a8b6db",
            "anidb_id": None,
        }
        tv_metadata = {
            "tvdb_id": "397934",
            "title": "Chainsaw Man",
        }

        processor = PlexWebhookProcessor()
        should_recover = processor._should_recover_tv_show_from_external_ids(
            payload,
            ids,
            "114410",
            tv_metadata,
        )

        self.assertFalse(should_recover)

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

    def test_scrobble_uses_anime_library_media_type_when_show_already_tracked_as_anime(
        self,
    ):
        """Plex scrobble should land in the anime bucket when the show is already tracked
        as anime (TMDB-based, library_media_type='anime').  The Season Item created by
        the scrobble must carry library_media_type='anime' so it appears on the anime
        season page rather than the TV season page."""
        # Pre-create a Season Item with library_media_type='anime' to simulate the user
        # having previously tracked this show via the anime URL pathway.
        # TVDB-first resolution maps tvdb://303821 → show TMDB ID 1668 (Friends),
        # so pre-created items must use 1668 to be found by the scrobble handler.
        show_item, _ = Item.objects.get_or_create(
            media_id="1668",
            source="tmdb",
            media_type=MediaTypes.TV.value,
            defaults={"title": "Friends", "image": ""},
        )
        Item.objects.get_or_create(
            media_id="1668",
            source="tmdb",
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            library_media_type=MediaTypes.ANIME.value,
            defaults={"title": "Friends", "image": ""},
        )

        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Friends",
                "index": 1,
                "parentIndex": 1,
                "Guid": [
                    {"id": "imdb://tt0583459"},
                    {"id": "tmdb://85987"},
                    {"id": "tvdb://303821"},
                ],
            },
        }

        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 200)

        # Season Item should reuse the existing anime-typed item, not create a new
        # TV-typed one.
        self.assertEqual(
            Item.objects.filter(
                media_id="1668",
                source="tmdb",
                media_type=MediaTypes.SEASON.value,
                season_number=1,
                library_media_type=MediaTypes.ANIME.value,
            ).count(),
            1,
        )
        self.assertEqual(
            Item.objects.filter(
                media_id="1668",
                source="tmdb",
                media_type=MediaTypes.SEASON.value,
                season_number=1,
                library_media_type=MediaTypes.SEASON.value,
            ).count(),
            0,
        )

        # Episode should be recorded under the anime-typed season.
        episode = Episode.objects.get(
            item__media_id="1668",
            item__season_number=1,
            item__episode_number=1,
        )
        self.assertEqual(episode.item.library_media_type, MediaTypes.ANIME.value)

    def test_scrobble_does_not_crash_when_both_anime_and_tv_season_items_exist(
        self,
    ):
        """When a show has both TV-typed and anime-typed Season Items (user tracked
        it via both pathways), a scrobble must not raise MultipleObjectsReturned and
        should land in the anime bucket since anime Items exist."""
        # TVDB-first resolution → show tracked under correct show TMDB ID 1668.
        Item.objects.get_or_create(
            media_id="1668",
            source="tmdb",
            media_type=MediaTypes.TV.value,
            defaults={"title": "Friends", "image": ""},
        )
        Item.objects.get_or_create(
            media_id="1668",
            source="tmdb",
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            library_media_type=MediaTypes.SEASON.value,
            defaults={"title": "Friends", "image": ""},
        )
        Item.objects.get_or_create(
            media_id="1668",
            source="tmdb",
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            library_media_type=MediaTypes.ANIME.value,
            defaults={"title": "Friends", "image": ""},
        )

        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Metadata": {
                "type": "episode",
                "grandparentTitle": "Friends",
                "index": 2,
                "parentIndex": 1,
                "Guid": [
                    {"id": "imdb://tt0583459"},
                    {"id": "tmdb://85987"},
                    {"id": "tvdb://303821"},
                ],
            },
        }

        # Should not raise MultipleObjectsReturned.
        response = self._post_payload(payload)

        self.assertEqual(response.status_code, 200)

        # Anime-typed Season Items exist, so the scrobble lands in the anime bucket.
        episode = Episode.objects.get(
            item__media_id="1668",
            item__season_number=1,
            item__episode_number=2,
        )
        self.assertEqual(episode.item.library_media_type, MediaTypes.ANIME.value)


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

    def test_library_filter_rejection_logs_reason(self):
        """Rejected library-filtered events should log the reason at info level."""
        self.user.plex_webhook_libraries = ["machine-a::1"]
        self.user.save(update_fields=["plex_webhook_libraries"])

        payload = {
            "event": "media.scrobble",
            "Account": {"title": "testuser"},
            "Server": {"uuid": "machine-a"},
            "Metadata": {
                "type": "movie",
                "title": "Big Hero 6",
                "librarySectionID": "2",
                "Guid": [{"id": "tmdb://177572"}],
            },
        }

        with self.assertLogs("integrations.webhooks.plex", level="INFO") as captured:
            response = self.client.post(
                self.url,
                data={"payload": json.dumps(payload)},
                format="multipart",
            )

        self.assertEqual(response.status_code, 200)
        joined_logs = "\n".join(captured.output)
        self.assertIn("Ignored Plex webhook event=media.scrobble", joined_logs)
        self.assertIn("payload library machine-a::2 is not selected", joined_logs)

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
