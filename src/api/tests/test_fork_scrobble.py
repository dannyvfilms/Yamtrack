# FORK: tests for the generic scrobble endpoint used by third-party
# playback clients (non Plex/Jellyfin/Emby).
from decimal import Decimal
from http import HTTPStatus as HTTP  # noqa: N814
from types import SimpleNamespace
from unittest.mock import patch

from django.test import tag

from app.models import Episode, Movie, Status

from .base import FloppyApiTestCase


class ScrobbleAuthTests(FloppyApiTestCase):
    """Auth is handled by the shared DRF auth classes."""

    def test_missing_token_rejected(self):
        """No auth header is rejected."""
        response = self.call_api(
            "post",
            "api_scrobble",
            payload={"action": "start", "media_type": "movie", "ids": {"tmdb": "603"}},
        )
        self.assertEqual(response.status_code, HTTP.FORBIDDEN)

    def test_invalid_token_rejected(self):
        """An unknown API key is rejected."""
        response = self.call_api(
            "post",
            "api_scrobble",
            payload={"action": "start", "media_type": "movie", "ids": {"tmdb": "603"}},
            headers=self.invalid_auth_headers,
        )
        self.assertEqual(response.status_code, HTTP.FORBIDDEN)


class ScrobbleValidationTests(FloppyApiTestCase):
    """400s for malformed scrobble payloads."""

    def _post(self, payload):
        return self.call_api(
            "post",
            "api_scrobble",
            payload=payload,
            headers=self.auth_headers,
        )

    def test_invalid_action_rejected(self):
        """An unsupported action value is rejected."""
        response = self._post(
            {"action": "resume", "media_type": "movie", "ids": {"tmdb": "603"}},
        )
        self.assertEqual(response.status_code, HTTP.BAD_REQUEST)

    def test_invalid_media_type_rejected(self):
        """An unsupported media_type value is rejected."""
        response = self._post(
            {"action": "start", "media_type": "tv", "ids": {"tmdb": "1001"}},
        )
        self.assertEqual(response.status_code, HTTP.BAD_REQUEST)

    def test_missing_ids_rejected(self):
        """At least one of tmdb/imdb/tvdb is required."""
        response = self._post({"action": "start", "media_type": "movie", "ids": {}})
        self.assertEqual(response.status_code, HTTP.BAD_REQUEST)

    def test_episode_without_season_episode_rejected(self):
        """Episode events require season_number/episode_number."""
        response = self._post(
            {"action": "start", "media_type": "episode", "ids": {"tmdb": "1668"}},
        )
        self.assertEqual(response.status_code, HTTP.BAD_REQUEST)


class ScrobbleLivePlaybackTests(FloppyApiTestCase):
    """'start'/'pause' only update the Now Playing card, no durable write."""

    @patch("api.fork_views_scrobble.live_playback.apply_playback_event")
    def test_start_updates_live_playback_only(self, mock_apply_event):
        """A start event calls apply_playback_event and writes no DB row."""
        response = self.call_api(
            "post",
            "api_scrobble",
            payload={
                "action": "start",
                "media_type": "movie",
                "ids": {"tmdb": "603"},
                "title": "The Matrix",
                "position_seconds": 0,
                "duration_seconds": 8160,
            },
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, HTTP.OK)
        mock_apply_event.assert_called_once()
        _, kwargs = mock_apply_event.call_args
        self.assertEqual(kwargs["user_id"], self.user1.id)
        self.assertEqual(kwargs["event_type"], "media.play")
        self.assertEqual(kwargs["playback_media_type"], "movie")
        self.assertEqual(kwargs["media_id"], "603")
        self.assertFalse(
            Movie.objects.filter(user=self.user1, item__media_id="603").exists(),
        )

    @patch("api.fork_views_scrobble.live_playback.apply_playback_event")
    def test_pause_updates_live_playback_only(self, mock_apply_event):
        """A pause event maps to the media.pause event type."""
        response = self.call_api(
            "post",
            "api_scrobble",
            payload={"action": "pause", "media_type": "movie", "ids": {"tmdb": "603"}},
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, HTTP.OK)
        _, kwargs = mock_apply_event.call_args
        self.assertEqual(kwargs["event_type"], "media.pause")
        self.assertFalse(
            Movie.objects.filter(user=self.user1, item__media_id="603").exists(),
        )

    @patch("app.providers.mal.anime")
    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data")
    @patch("api.fork_views_scrobble.live_playback.apply_playback_event")
    def test_anidb_episode_pins_the_card_to_the_mal_cour(
        self,
        mock_apply_event,
        mock_mapping,
        mock_mal,
    ):
        """An anidb id on a flat-MAL user resolves the Now Playing card cour."""
        self.user1.anime_enabled = True
        self.user1.anime_metadata_source_default = "mal"
        self.user1.save()
        mock_mapping.return_value = {"anidb:3651:R": {"mal:849": {"1-": "1-"}}}
        mock_mal.return_value = {
            "title": "Suzumiya Haruhi no Yuutsu",
            "image": "https://example.com/haruhi.jpg",
            "max_progress": 14,
        }

        response = self.call_api(
            "post",
            "api_scrobble",
            payload={
                "action": "start",
                "media_type": "episode",
                "ids": {"tvdb": "9350138", "anidb": "3651"},
                "series_title": "Suzumiya Haruhi no Yuutsu",
                "season_number": 1,
                "episode_number": 1,
            },
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, HTTP.OK)
        _, kwargs = mock_apply_event.call_args
        self.assertEqual(kwargs["source"], "mal")
        self.assertEqual(kwargs["media_id"], "849")
        self.assertEqual(kwargs["episode_number"], 1)
        self.assertEqual(kwargs["series_title"], "Suzumiya Haruhi no Yuutsu")
        self.assertEqual(kwargs["image"], "https://example.com/haruhi.jpg")

    @patch("api.fork_views_scrobble.live_playback.apply_playback_event")
    def test_live_playback_failure_does_not_fail_request(self, mock_apply_event):
        """A best-effort live-card failure never surfaces as a request error."""
        mock_apply_event.side_effect = RuntimeError("cache down")
        response = self.call_api(
            "post",
            "api_scrobble",
            payload={"action": "start", "media_type": "movie", "ids": {"tmdb": "603"}},
            headers=self.auth_headers,
        )
        self.assertEqual(response.status_code, HTTP.OK)


class ScrobbleStopTests(FloppyApiTestCase):
    """'stop' persists a durable watch/progress update."""

    @tag("network")
    def test_stop_completed_marks_movie_completed(self):
        """A completed stop event creates a completed Movie instance."""
        response = self.call_api(
            "post",
            "api_scrobble",
            payload={
                "action": "stop",
                "media_type": "movie",
                "ids": {"tmdb": "603"},
                "title": "The Matrix",
                "completed": True,
            },
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, HTTP.OK)
        movie = Movie.objects.get(item__media_id="603", user=self.user1)
        self.assertEqual(movie.status, Status.COMPLETED.value)

    @tag("network")
    def test_stop_episode_dedup_within_five_seconds(self):
        """Two rapid duplicate stop events create only one Episode play."""
        payload = {
            "action": "stop",
            "media_type": "episode",
            "ids": {"tvdb": "303821", "imdb": "tt0583459"},
            "series_title": "Friends",
            "season_number": 1,
            "episode_number": 1,
            "completed": True,
        }
        first = self.call_api(
            "post",
            "api_scrobble",
            payload=payload,
            headers=self.auth_headers,
        )
        second = self.call_api(
            "post",
            "api_scrobble",
            payload=payload,
            headers=self.auth_headers,
        )

        self.assertEqual(first.status_code, HTTP.OK)
        self.assertEqual(second.status_code, HTTP.OK)
        self.assertEqual(
            Episode.objects.filter(
                item__media_id="1668",
                item__season_number=1,
                item__episode_number=1,
            ).count(),
            1,
        )

    def _stop_episode_payload(self, **overrides):
        payload = {
            "action": "stop",
            "media_type": "episode",
            "ids": {"tmdb": "1001"},
            "series_title": "TV Show 1",
            "season_number": 1,
            "episode_number": 1,
            "completed": True,
        }
        payload.update(overrides)
        return payload

    def _resolved_episode_item(self):
        """A stand-in for the Item resolve_video_item would return."""
        return SimpleNamespace(
            media_id="1001",
            source="tmdb",
            season_number=1,
            episode_number=1,
        )

    @patch(
        "api.fork_views_scrobble.fork_views_playback.resolve_video_item",
    )
    @patch(
        "api.fork_views_scrobble.GenericScrobbleProcessor.process_payload",
        return_value=None,
    )
    def test_stop_with_score_sets_episode_score(
        self,
        _mock_process,
        mock_resolve_item,
    ):
        """A stop event carrying a score sets it on all plays of the episode."""
        mock_resolve_item.return_value = self._resolved_episode_item()

        response = self.call_api(
            "post",
            "api_scrobble",
            payload=self._stop_episode_payload(score="8.3"),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, HTTP.OK)
        scores = set(
            Episode.objects.filter(
                related_season=self.season_medias[0],
                item__episode_number=1,
            ).values_list("score", flat=True),
        )
        self.assertEqual(scores, {Decimal("8.3")})

    @patch(
        "api.fork_views_scrobble.GenericScrobbleProcessor.process_payload",
        return_value=None,
    )
    def test_stop_with_invalid_score_rejected_before_processing(
        self,
        mock_process,
    ):
        """An out-of-range score 400s without touching the processor."""
        response = self.call_api(
            "post",
            "api_scrobble",
            payload=self._stop_episode_payload(score="11"),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, HTTP.BAD_REQUEST)
        mock_process.assert_not_called()

    @patch(
        "api.fork_views_scrobble.fork_views_playback.resolve_video_item",
    )
    @patch(
        "api.fork_views_scrobble.GenericScrobbleProcessor.process_payload",
        return_value=None,
    )
    def test_stop_score_ignored_for_movie(self, _mock_process, mock_resolve_item):
        """A score sent alongside a movie stop event is a no-op."""
        response = self.call_api(
            "post",
            "api_scrobble",
            payload={
                "action": "stop",
                "media_type": "movie",
                "ids": {"tmdb": "603"},
                "title": "The Matrix",
                "completed": True,
                "score": "8.3",
            },
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, HTTP.OK)
        mock_resolve_item.assert_not_called()

    @patch(
        "api.fork_views_scrobble.apply_episode_score",
        side_effect=RuntimeError("db down"),
    )
    @patch(
        "api.fork_views_scrobble.fork_views_playback.resolve_video_item",
    )
    @patch(
        "api.fork_views_scrobble.GenericScrobbleProcessor.process_payload",
        return_value=None,
    )
    def test_stop_score_failure_does_not_fail_request(
        self,
        _mock_process,
        mock_resolve_item,
        _mock_apply_score,
    ):
        """A best-effort score-write failure never surfaces as a request error."""
        mock_resolve_item.return_value = self._resolved_episode_item()

        response = self.call_api(
            "post",
            "api_scrobble",
            payload=self._stop_episode_payload(score="8.3"),
            headers=self.auth_headers,
        )

        self.assertEqual(response.status_code, HTTP.OK)

    def test_stop_resolution_failure_returns_404(self):
        """A provider failure while resolving media returns 404."""
        with patch(
            "app.providers.tmdb.movie",
            side_effect=RuntimeError("tmdb down"),
        ):
            response = self.call_api(
                "post",
                "api_scrobble",
                payload={
                    "action": "stop",
                    "media_type": "movie",
                    "ids": {"tmdb": "603"},
                    "completed": True,
                },
                headers=self.auth_headers,
            )

        self.assertEqual(response.status_code, HTTP.NOT_FOUND)
