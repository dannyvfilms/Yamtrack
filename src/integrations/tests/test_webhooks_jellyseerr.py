from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from app.models import TV, Item, Movie, Status
from users.models import User


class JellyseerrWebhookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="john", password="pw")
        self.user.token = "x" * 32
        self.user.jellyseerr_enabled = True
        self.user.jellyseerr_trigger_statuses = "PENDING,PROCESSING,AVAILABLE,PARTIALLY_AVAILABLE"
        self.user.jellyseerr_allowed_usernames = ""
        self.user.jellyseerr_default_added_status = Status.PLANNING.value
        self.user.save()

    def _url(self, token=None):
        return reverse("jellyseerr_webhook", kwargs={"token": token or self.user.token})

    def test_invalid_token_returns_401(self):
        resp = self.client.post(self._url("badtoken"), data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 401)

    def test_missing_payload_returns_400(self):
        resp = self.client.post(self._url(), data=b"", content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_invalid_json_returns_400(self):
        resp = self.client.post(self._url(), data="{notjson", content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    @patch("app.providers.services.get_media_metadata")
    def test_adds_movie_as_planning(self, mock_meta):
        mock_meta.return_value = {
            "title": "Example Movie",
            "image": "https://example.com/movie.jpg",
        }

        payload = {
            "media_type": "movie",
            "media_tmdbid": "123",
            "media_status": "PENDING",
            "requestedBy_username": "alice",
        }

        resp = self.client.post(self._url(), data=payload, content_type="application/json")
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(Item.objects.count(), 1)
        self.assertEqual(Movie.objects.count(), 1)

        m = Movie.objects.first()
        self.assertEqual(m.user_id, self.user.id)
        self.assertEqual(m.status, Status.PLANNING.value)
        self.assertEqual(m.item.source, "tmdb")
        self.assertEqual(m.item.media_id, "123")

    @patch("app.providers.services.get_media_metadata")
    def test_allowlist_blocks_non_matching_requester(self, mock_meta):
        mock_meta.return_value = {
            "title": "Blocked Movie",
            "image": "https://example.com/blocked.jpg",
        }

        self.user.jellyseerr_allowed_usernames = "bob"
        self.user.save(update_fields=["jellyseerr_allowed_usernames"])

        payload = {
            "media_type": "movie",
            "media_tmdbid": "555",
            "media_status": "PENDING",
            "requestedBy_username": "alice",
        }

        resp = self.client.post(self._url(), data=payload, content_type="application/json")
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(Movie.objects.count(), 0)
        self.assertEqual(Item.objects.count(), 0)

    @patch("app.providers.services.get_media_metadata")
    def test_adds_tv_as_in_progress_sets_start_date(self, mock_meta):
        mock_meta.return_value = {
            "title": "Example Show",
            "image": "https://example.com/show.jpg",
        }

        self.user.jellyseerr_default_added_status = Status.IN_PROGRESS.value
        self.user.save(update_fields=["jellyseerr_default_added_status"])

        payload = {
            "media_type": "tv",
            "media_tmdbid": "777",
            "media_status": "PENDING",
            "requestedBy_username": "alice",
        }

        resp = self.client.post(self._url(), data=payload, content_type="application/json")
        self.assertEqual(resp.status_code, 200)

        self.assertEqual(TV.objects.count(), 1)
        tv = TV.objects.first()
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        self.assertIsNotNone(tv.start_date)
