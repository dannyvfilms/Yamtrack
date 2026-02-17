from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class ServiceWorkerViewTests(TestCase):
    """Regression tests for the service worker endpoint contract."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="sw-test-user",
            password="test-pass-123",
        )
        self.client.force_login(self.user)

    def test_service_worker_sets_no_cache_headers_and_static_only_policy(self):
        response = self.client.get(reverse("service_worker"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertEqual(response["Cache-Control"], "no-cache, no-store, must-revalidate")
        self.assertEqual(response["Pragma"], "no-cache")
        self.assertEqual(response["Expires"], "0")

        body = response.content.decode()
        self.assertIn('const CACHE_NAME = "yamtrack-v3";', body)
        self.assertIn("const isSameOrigin = url.origin === self.location.origin;", body)
        self.assertIn('const isHtmxRequest = request.headers.get("HX-Request") === "true";', body)
        self.assertIn('!url.pathname.startsWith("/static/")', body)
        self.assertIn("caches.match(request)", body)
