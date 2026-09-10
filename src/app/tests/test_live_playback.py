"""Tests for cache-backed live playback state and request-path purity."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from app import live_playback
from app.models import Item, MediaTypes, PlaybackProgress, Sources


class ScrobbleProgressFloorTests(TestCase):
    """A scrobbled title is treated as being near its end."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scrobblefloor",
            password="pw",
        )

    def tearDown(self):
        live_playback.clear_user_playback_state(self.user.id)
        cache.clear()
        super().tearDown()

    @patch("app.live_playback._attach_resolved_image")
    def _scrobble(self, _mock_image, *, offset, duration=5340):
        live_playback.apply_playback_event(
            user_id=self.user.id,
            event_type="media.scrobble",
            playback_media_type=MediaTypes.MOVIE.value,
            media_id="701",
            source=Sources.TMDB.value,
            rating_key="rk-1",
            title="A Movie",
            view_offset_seconds=offset,
            duration_seconds=duration,
        )
        return live_playback.get_user_playback_state(self.user.id)

    @patch("app.live_playback._attach_resolved_image")
    def test_offsetless_scrobble_falls_back_to_the_threshold(self, _mock_image):
        """Plex sends the scrobble without one; the cached one can be stale."""
        self._scrobble(offset=1871)  # last position before a seek to the end
        state = self._scrobble(offset=None)

        self.assertEqual(state["view_offset_seconds"], 4806)

    def test_reported_offset_is_always_kept(self):
        """A server scrobbling below the threshold still knows best."""
        state = self._scrobble(offset=1871)

        self.assertEqual(state["view_offset_seconds"], 1871)

    @patch("app.live_playback._attach_resolved_image")
    def test_card_expires_shortly_after_an_offsetless_scrobble(self, _mock_image):
        """The card must not linger for the rest of the runtime."""
        self._scrobble(offset=1871)
        state = self._scrobble(offset=None)

        remaining = state["scrobble_expires_at_ts"] - state["updated_at_ts"]
        self.assertLessEqual(remaining, 5340 * 0.1 + 30)

    def test_missing_duration_keeps_the_fallback_expiry(self):
        """Without a duration there is nothing to take a share of."""
        state = self._scrobble(offset=1871, duration=None)

        remaining = state["scrobble_expires_at_ts"] - state["updated_at_ts"]
        self.assertEqual(remaining, live_playback.PLAYBACK_SCROBBLE_FALLBACK_SECONDS)

    @patch("app.live_playback._attach_resolved_image")
    def test_seeking_back_after_a_scrobble_is_not_floored(self, _mock_image):
        """Skipping back into the credits must not jump the card forward."""
        self._scrobble(offset=5300)

        live_playback.apply_playback_event(
            user_id=self.user.id,
            event_type="media.resume",
            playback_media_type=MediaTypes.MOVIE.value,
            media_id="701",
            source=Sources.TMDB.value,
            rating_key="rk-1",
            title="A Movie",
            view_offset_seconds=600,
            duration_seconds=5340,
        )

        state = live_playback.get_user_playback_state(self.user.id)
        self.assertEqual(state["view_offset_seconds"], 600)


class ApplyPlaybackEventImageTests(TestCase):
    """Image resolution happens when webhook events are applied."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="playbackimg",
            password="pw",
        )

    def tearDown(self):
        live_playback.clear_user_playback_state(self.user.id)
        cache.clear()
        super().tearDown()

    def _apply_play(self, **overrides):
        kwargs = {
            "user_id": self.user.id,
            "event_type": "media.play",
            "playback_media_type": MediaTypes.EPISODE.value,
            "media_id": "1396",
            "source": Sources.TMDB.value,
            "rating_key": "rk-1",
            "title": "Pilot",
            "series_title": "Breaking Bad",
            "episode_title": "Pilot",
            "season_number": 1,
            "episode_number": 1,
            "view_offset_seconds": 60,
            "duration_seconds": 3000,
        }
        kwargs.update(overrides)
        live_playback.apply_playback_event(**kwargs)

    @patch("app.live_playback._resolve_landscape_image")
    def test_play_event_stores_resolved_image_in_state(self, mock_resolve):
        mock_resolve.return_value = ("http://img.example/still.jpg", "primary")

        self._apply_play()

        state = live_playback.get_user_playback_state(self.user.id)
        self.assertEqual(state["image"], "http://img.example/still.jpg")
        self.assertEqual(state["image_source"], "primary")
        self.assertIn("image_resolved_at_ts", state)
        mock_resolve.assert_called_once()

    @patch("app.live_playback._resolve_landscape_image")
    def test_matching_state_carries_image_without_reresolving(self, mock_resolve):
        mock_resolve.return_value = ("http://img.example/still.jpg", "primary")

        self._apply_play()
        self._apply_play(event_type="media.pause", view_offset_seconds=120)

        state = live_playback.get_user_playback_state(self.user.id)
        self.assertEqual(state["image"], "http://img.example/still.jpg")
        self.assertEqual(state["status"], live_playback.PLAYBACK_STATUS_PAUSED)
        mock_resolve.assert_called_once()

    @patch("app.live_playback._resolve_landscape_image")
    def test_resolution_failure_leaves_state_usable(self, mock_resolve):
        mock_resolve.side_effect = RuntimeError("provider down")

        self._apply_play()

        state = live_playback.get_user_playback_state(self.user.id)
        self.assertIsNotNone(state)
        self.assertNotIn("image", state)

    @patch("app.live_playback._resolve_landscape_image")
    def test_caller_supplied_image_skips_resolution(self, mock_resolve):
        """A MAL cour card carries artwork the episode resolver cannot look up."""
        self._apply_play(
            source=Sources.MAL.value,
            media_id="849",
            image="https://example.com/haruhi.jpg",
        )

        state = live_playback.get_user_playback_state(self.user.id)
        self.assertEqual(state["image"], "https://example.com/haruhi.jpg")
        self.assertEqual(state["image_source"], "primary")
        mock_resolve.assert_not_called()


class FetchEpisodeStillCacheTests(TestCase):
    """Episode still lookups are cached, including failures."""

    def tearDown(self):
        cache.clear()
        super().tearDown()

    @patch("app.providers.tmdb.episode")
    def test_failure_is_negative_cached(self, mock_episode):
        mock_episode.side_effect = RuntimeError("tmdb 404")

        first = live_playback._fetch_episode_still("1", 2, 3)
        second = live_playback._fetch_episode_still("1", 2, 3)

        self.assertEqual(first, (None, "none"))
        self.assertEqual(second, (None, "none"))
        mock_episode.assert_called_once()

    @patch("app.providers.tmdb.episode")
    def test_success_is_cached(self, mock_episode):
        mock_episode.return_value = {
            "image": "http://img.example/ep.jpg",
            "image_source": "primary",
        }

        first = live_playback._fetch_episode_still("1", 2, 3)
        second = live_playback._fetch_episode_still("1", 2, 3)

        self.assertEqual(first, ("http://img.example/ep.jpg", "primary"))
        self.assertEqual(second, first)
        mock_episode.assert_called_once()


class RequestPathPurityTests(TestCase):
    """The playback card endpoints never call metadata providers."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="purity",
            password="pw",
        )
        self.client.force_login(self.user)

    def tearDown(self):
        live_playback.clear_user_playback_state(self.user.id)
        cache.clear()
        super().tearDown()

    def _seed_episode_state_without_image(self):
        now_ts = live_playback._now_ts()
        live_playback.set_user_playback_state(
            self.user.id,
            {
                "event_type": "media.play",
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "rating_key": "rk-1",
                "title": "Pilot",
                "series_title": "Breaking Bad",
                "episode_title": "Pilot",
                "season_number": 1,
                "episode_number": 1,
                "view_offset_seconds": 60,
                "duration_seconds": 3000,
                "started_at_ts": now_ts,
                "status": live_playback.PLAYBACK_STATUS_PLAYING,
                "updated_at_ts": now_ts,
                "expires_at_ts": now_ts + 3600,
                "pause_expires_at_ts": None,
                "scrobble_expires_at_ts": None,
            },
        )

    @patch("app.tasks.resolve_playback_image.delay")
    @patch(
        "app.providers.services.api_request",
        side_effect=AssertionError("provider called from request path"),
    )
    def test_active_playback_fragment_never_calls_providers(
        self,
        _mock_api,
        mock_fill,
    ):
        self._seed_episode_state_without_image()

        response = self.client.get(reverse("active_playback_fragment"))

        self.assertEqual(response.status_code, 200)
        mock_fill.assert_called_once_with(self.user.id)

    @patch("app.tasks.resolve_playback_image.delay")
    @patch(
        "app.providers.services.api_request",
        side_effect=AssertionError("provider called from request path"),
    )
    def test_card_uses_stored_image_without_enqueueing(self, _mock_api, mock_fill):
        self._seed_episode_state_without_image()
        state = cache.get(live_playback._cache_key(self.user.id))
        state["image"] = "http://img.example/still.jpg"
        state["image_source"] = "primary"
        live_playback.set_user_playback_state(self.user.id, state)

        card = live_playback.build_home_playback_card(self.user)

        self.assertEqual(card["image"], "http://img.example/still.jpg")
        mock_fill.assert_not_called()

    @patch("app.tasks.resolve_playback_image.delay")
    @patch(
        "app.providers.services.api_request",
        side_effect=AssertionError("provider called from request path"),
    )
    def test_fill_task_enqueue_is_guarded(self, _mock_api, mock_fill):
        self._seed_episode_state_without_image()

        live_playback.build_home_playback_card(self.user)
        live_playback.build_home_playback_card(self.user)

        mock_fill.assert_called_once_with(self.user.id)


class MalCourCardTests(TestCase):
    """A MAL-sourced episode card renders as a flat anime cour."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="malcard")
        self.user.anime_enabled = True
        self.user.save()

    def tearDown(self):
        live_playback.clear_user_playback_state(self.user.id)
        cache.clear()
        super().tearDown()

    def _seed(self):
        now_ts = live_playback._now_ts()
        live_playback.set_user_playback_state(
            self.user.id,
            {
                "event_type": "media.play",
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "849",
                "source": Sources.MAL.value,
                "series_title": "Suzumiya Haruhi no Yuutsu",
                "season_number": None,
                "episode_number": 3,
                "image": "https://example.com/haruhi.jpg",
                "image_source": "primary",
                "view_offset_seconds": 60,
                "duration_seconds": 1400,
                "started_at_ts": now_ts,
                "status": live_playback.PLAYBACK_STATUS_PLAYING,
                "updated_at_ts": now_ts,
                "expires_at_ts": now_ts + 3600,
                "pause_expires_at_ts": None,
                "scrobble_expires_at_ts": None,
            },
        )

    def test_card_links_to_the_anime_details_page(self):
        self._seed()
        card = live_playback.build_home_playback_card(self.user)
        self.assertIn(f"/{Sources.MAL.value}/{MediaTypes.ANIME.value}/849/", card["details_url"])
        self.assertEqual(card["image"], "https://example.com/haruhi.jpg")
        self.assertEqual(card["episode_code"], "E03")


class ResolveStateImageTaskTests(TestCase):
    """The background fill-in task resolves and persists the image."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="filltask",
            password="pw",
        )

    def tearDown(self):
        live_playback.clear_user_playback_state(self.user.id)
        cache.clear()
        super().tearDown()

    @patch("app.live_playback._resolve_landscape_image")
    def test_resolve_state_image_fills_missing_image(self, mock_resolve):
        mock_resolve.return_value = ("http://img.example/still.jpg", "primary")
        now_ts = live_playback._now_ts()
        live_playback.set_user_playback_state(
            self.user.id,
            {
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "status": live_playback.PLAYBACK_STATUS_PLAYING,
                "updated_at_ts": now_ts,
                "expires_at_ts": now_ts + 3600,
            },
        )

        live_playback.resolve_state_image(self.user.id)

        state = live_playback.get_user_playback_state(self.user.id)
        self.assertEqual(state["image"], "http://img.example/still.jpg")

    @patch("app.live_playback._resolve_landscape_image")
    def test_resolve_state_image_noops_when_image_present(self, mock_resolve):
        now_ts = live_playback._now_ts()
        live_playback.set_user_playback_state(
            self.user.id,
            {
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1396",
                "source": Sources.TMDB.value,
                "image": "http://img.example/still.jpg",
                "status": live_playback.PLAYBACK_STATUS_PLAYING,
                "updated_at_ts": now_ts,
                "expires_at_ts": now_ts + 3600,
            },
        )

        live_playback.resolve_state_image(self.user.id)

        mock_resolve.assert_not_called()


class PlaybackProgressAtomicTests(TestCase):
    """Webhook progress writes preserve fields that an event omits."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="progress-atomic")
        self.item = Item.objects.create(
            media_id="701",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Matrix",
            image="https://example.com/matrix.jpg",
        )

    def test_completion_only_preserves_position_and_known_duration(self):
        PlaybackProgress.objects.create(
            user=self.user,
            item=self.item,
            position_seconds=1200,
            duration_seconds=3600,
        )

        live_playback.store_playback_progress(
            self.user.id,
            item=self.item,
            event_type="media.scrobble",
            playback_media_type=MediaTypes.MOVIE.value,
            view_offset_seconds=None,
            duration_seconds=8160,
        )

        progress = PlaybackProgress.objects.get(user=self.user, item=self.item)
        self.assertEqual(progress.position_seconds, 1200)
        self.assertEqual(progress.duration_seconds, 3600)
        self.assertTrue(progress.completed)

    def test_completion_only_fills_missing_duration(self):
        PlaybackProgress.objects.create(
            user=self.user,
            item=self.item,
            position_seconds=1200,
        )

        live_playback.store_playback_progress(
            self.user.id,
            item=self.item,
            event_type="media.scrobble",
            playback_media_type=MediaTypes.MOVIE.value,
            view_offset_seconds=None,
            duration_seconds=8160,
        )

        progress = PlaybackProgress.objects.get(user=self.user, item=self.item)
        self.assertEqual(progress.position_seconds, 1200)
        self.assertEqual(progress.duration_seconds, 8160)
        self.assertTrue(progress.completed)

    def test_provider_incomplete_stop_overrides_completion_tail(self):
        PlaybackProgress.objects.create(
            user=self.user,
            item=self.item,
            position_seconds=8000,
            duration_seconds=8160,
            completed=True,
        )

        live_playback.store_playback_progress(
            self.user.id,
            item=self.item,
            event_type="media.stop",
            playback_media_type=MediaTypes.MOVIE.value,
            view_offset_seconds=8160,
            duration_seconds=8160,
            provider_completed=False,
        )

        self.assertFalse(
            PlaybackProgress.objects.get(user=self.user, item=self.item).completed,
        )


@skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row-lock semantics")
class PlaybackProgressConcurrencyTests(TransactionTestCase):
    """Concurrent webhook writes resolve to a serialized final state."""

    reset_sequences = True

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="progress-race")
        self.item = Item.objects.create(
            media_id="701",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="The Matrix",
            image="https://example.com/matrix.jpg",
        )

    def _run_concurrently(self, *events):
        barrier = Barrier(len(events))

        def run(event):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                live_playback.store_playback_progress(
                    self.user.id,
                    item=self.item,
                    event_type=event[0],
                    playback_media_type=MediaTypes.MOVIE.value,
                    view_offset_seconds=event[1],
                    duration_seconds=8160,
                    provider_completed=event[2],
                )
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(events)) as executor:
            return list(executor.map(run, events))

    def test_scrobble_and_pause_do_not_restore_a_stale_position(self):
        PlaybackProgress.objects.create(
            user=self.user,
            item=self.item,
            position_seconds=6000,
            duration_seconds=8160,
        )

        self._run_concurrently(
            ("media.scrobble", None, None),
            ("media.pause", 120, None),
        )

        progress = PlaybackProgress.objects.get(user=self.user, item=self.item)
        self.assertEqual(progress.position_seconds, 120)

    def test_scrobble_and_trailing_stop_keep_completion(self):
        PlaybackProgress.objects.create(
            user=self.user,
            item=self.item,
            position_seconds=6000,
            duration_seconds=8160,
        )

        self._run_concurrently(
            ("media.scrobble", None, None),
            ("media.stop", 8160, None),
        )

        progress = PlaybackProgress.objects.get(user=self.user, item=self.item)
        self.assertEqual(progress.position_seconds, 8160)
        self.assertTrue(progress.completed)
