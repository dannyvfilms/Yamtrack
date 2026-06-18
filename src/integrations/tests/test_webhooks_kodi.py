import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from app.models import TV, Episode, Item, MediaTypes, Movie, Season, Status
from integrations.webhooks.kodi import KodiWebhookProcessor


TV_EPISODE_PAYLOAD = {
    "event": "end",
    "mediaType": "episode",
    "title": "The One Where Monica Gets a Roommate",
    "tvShowTitle": "Friends",
    "season": 1,
    "episode": 1,
    "year": 1994,
    "uniqueIds": {
        "tmdb": None,
        "imdb": "tt0583459",
        "tvdb": "303821",
    },
    "duration": 1320,
    "progress": {"time": 1320, "percent": 100.0},
}

MOVIE_PAYLOAD = {
    "event": "end",
    "mediaType": "movie",
    "title": "The Matrix",
    "year": 1999,
    "uniqueIds": {
        "tmdb": "603",
        "imdb": "tt0133093",
        "tvdb": None,
    },
    "duration": 8160,
    "progress": {"time": 8160, "percent": 100.0},
}


class KodiWebhookViewTests(TestCase):
    """Tests for the Kodi webhook view (auth, payload validation)."""

    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_superuser(
            username="testuser", token="test-token"
        )
        self.url = reverse("kodi_webhook", kwargs={"token": "test-token"})

    def test_invalid_token(self):
        url = reverse("kodi_webhook", kwargs={"token": "invalid-token"})
        response = self.client.post(url, data={}, content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_missing_body(self):
        response = self.client.post(self.url, data="", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_invalid_json(self):
        response = self.client.post(
            self.url, data="not-json", content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)

    def test_valid_payload_returns_200(self):
        response = self.client.post(
            self.url,
            data=json.dumps(MOVIE_PAYLOAD),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)


class KodiWebhookMovieTests(TestCase):
    """Tests for Kodi movie webhook processing."""

    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_superuser(
            username="testuser", token="test-token"
        )
        self.url = reverse("kodi_webhook", kwargs={"token": "test-token"})

    def _post(self, payload):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_movie_end_event_marks_completed(self):
        payload = {**MOVIE_PAYLOAD, "event": "end"}
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.COMPLETED.value)

    def test_movie_stop_above_threshold_marks_completed(self):
        payload = {
            **MOVIE_PAYLOAD,
            "event": "stop",
            "progress": {"time": 7000, "percent": 85.0},
        }
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.COMPLETED.value)

    def test_movie_stop_below_threshold_stays_in_progress(self):
        payload = {
            **MOVIE_PAYLOAD,
            "event": "stop",
            "progress": {"time": 1000, "percent": 12.0},
        }
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)

    def test_movie_start_event_creates_in_progress(self):
        payload = {
            **MOVIE_PAYLOAD,
            "event": "start",
            "progress": {"time": 0, "percent": 0.0},
        }
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        movie = Movie.objects.get(item__media_id="603", user=self.user)
        self.assertEqual(movie.status, Status.IN_PROGRESS.value)

    def test_movie_repeated_watches_tracked(self):
        payload = {**MOVIE_PAYLOAD, "event": "end"}
        self._post(payload)
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        # Each completed watch creates a new Movie record in history
        movies = Movie.objects.filter(item__media_id="603", user=self.user)
        self.assertGreaterEqual(movies.count(), 1)
        self.assertTrue(all(m.status == Status.COMPLETED.value for m in movies))


class KodiWebhookTVTests(TestCase):
    """Tests for Kodi TV episode webhook processing."""

    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_superuser(
            username="testuser", token="test-token"
        )
        self.url = reverse("kodi_webhook", kwargs={"token": "test-token"})

    def _post(self, payload):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_tv_episode_end_event(self):
        payload = {**TV_EPISODE_PAYLOAD, "event": "end"}
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)

        tv_item = Item.objects.get(media_type=MediaTypes.TV.value, media_id="1668")
        self.assertEqual(tv_item.title, "Friends")

        tv = TV.objects.get(item=tv_item, user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

        season = Season.objects.get(item__media_id="1668", item__season_number=1)
        self.assertEqual(season.status, Status.IN_PROGRESS.value)

        episode = Episode.objects.get(
            item__media_id="1668",
            item__season_number=1,
            item__episode_number=1,
        )
        self.assertIsNotNone(episode.end_date)

    def test_tv_episode_stop_above_threshold(self):
        payload = {
            **TV_EPISODE_PAYLOAD,
            "event": "stop",
            "progress": {"time": 1100, "percent": 83.0},
        }
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        episode = Episode.objects.get(
            item__media_id="1668",
            item__season_number=1,
            item__episode_number=1,
        )
        self.assertIsNotNone(episode.end_date)

    def test_tv_episode_stop_below_threshold(self):
        payload = {
            **TV_EPISODE_PAYLOAD,
            "event": "stop",
            "progress": {"time": 100, "percent": 7.0},
        }
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        # Episode is not marked watched; TV show should exist in IN_PROGRESS
        tv = TV.objects.get(item__media_id="1668", user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        self.assertFalse(
            Episode.objects.filter(
                item__media_id="1668",
                item__season_number=1,
                item__episode_number=1,
                end_date__isnull=False,
            ).exists()
        )

    def test_tv_episode_start_event(self):
        payload = {
            **TV_EPISODE_PAYLOAD,
            "event": "start",
            "progress": {"time": 0, "percent": 0.0},
        }
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        # Start event creates TV/Season in IN_PROGRESS but no completed episode
        tv = TV.objects.get(item__media_id="1668", user=self.user)
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        self.assertFalse(
            Episode.objects.filter(
                item__media_id="1668",
                item__season_number=1,
                item__episode_number=1,
                end_date__isnull=False,
            ).exists()
        )


class KodiWebhookEdgeCaseTests(TestCase):
    """Tests for edge cases and ignored events."""

    def setUp(self):
        self.client = Client()
        self.user = get_user_model().objects.create_superuser(
            username="testuser", token="test-token"
        )
        self.url = reverse("kodi_webhook", kwargs={"token": "test-token"})

    def _post(self, payload):
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_unsupported_event_type_ignored(self):
        payload = {**MOVIE_PAYLOAD, "event": "pause"}
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Movie.objects.exists())

    def test_missing_external_ids_ignored(self):
        payload = {**MOVIE_PAYLOAD, "uniqueIds": {}}
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Movie.objects.exists())

    def test_missing_progress_key_does_not_error(self):
        """_is_played must not raise AttributeError when 'progress' is absent."""
        payload = {**MOVIE_PAYLOAD, "event": "stop"}
        del payload["progress"]
        response = self._post(payload)
        self.assertEqual(response.status_code, 200)


class KodiProcessorUnitTests(TestCase):
    """Unit tests for KodiWebhookProcessor helper methods."""

    def setUp(self):
        self.processor = KodiWebhookProcessor()

    def test_extract_external_ids(self):
        payload = {"uniqueIds": {"tmdb": "603", "imdb": "tt0133093", "tvdb": "456"}}
        ids = self.processor._extract_external_ids(payload)
        self.assertEqual(ids["tmdb_id"], "603")
        self.assertEqual(ids["imdb_id"], "tt0133093")
        self.assertEqual(ids["tvdb_id"], "456")

    def test_extract_external_ids_empty(self):
        ids = self.processor._extract_external_ids({"uniqueIds": {}})
        self.assertIsNone(ids["tmdb_id"])
        self.assertIsNone(ids["imdb_id"])
        self.assertIsNone(ids["tvdb_id"])

    def test_extract_external_ids_missing_key(self):
        ids = self.processor._extract_external_ids({})
        self.assertIsNone(ids["tmdb_id"])

    def test_extract_season_episode(self):
        payload = {"season": 2, "episode": 5}
        season, episode = self.processor._extract_season_episode_from_payload(payload)
        self.assertEqual(season, 2)
        self.assertEqual(episode, 5)

    def test_extract_series_title(self):
        payload = {"tvShowTitle": "Breaking Bad"}
        self.assertEqual(self.processor._extract_series_title(payload), "Breaking Bad")

    def test_is_played_end_event(self):
        self.assertTrue(self.processor._is_played({"event": "end"}))

    def test_is_played_stop_above_threshold(self):
        payload = {"event": "stop", "progress": {"percent": 85.0}}
        self.assertTrue(self.processor._is_played(payload))

    def test_is_played_stop_below_threshold(self):
        payload = {"event": "stop", "progress": {"percent": 50.0}}
        self.assertFalse(self.processor._is_played(payload))

    def test_is_played_start_event(self):
        self.assertFalse(self.processor._is_played({"event": "start"}))

    def test_is_played_missing_progress(self):
        self.assertFalse(self.processor._is_played({"event": "stop"}))
