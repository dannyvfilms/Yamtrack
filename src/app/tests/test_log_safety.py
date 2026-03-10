from django.test import SimpleTestCase

from app.log_safety import exception_summary, presence_map, safe_url, stable_hmac


class LogSafetyTests(SimpleTestCase):
    def test_exception_summary_includes_status_without_message(self):
        response = type("Response", (), {"status_code": 404})()
        exc = Exception("contains secrets")
        exc.response = response

        self.assertEqual(exception_summary(exc), "Exception(status=404)")

    def test_presence_map_redacts_values(self):
        values = {"tmdb_id": "123", "imdb_id": "", "tvdb_id": None}

        self.assertEqual(
            presence_map(values, ("tmdb_id", "imdb_id", "tvdb_id")),
            {"tmdb_id": True, "imdb_id": False, "tvdb_id": False},
        )

    def test_safe_url_drops_query_and_fragment(self):
        self.assertEqual(
            safe_url("https://example.com/library?token=secret#frag"),
            "https://example.com/library",
        )

    def test_stable_hmac_is_namespaced_and_deterministic(self):
        digest = stable_hmac("value", namespace="discover")

        self.assertEqual(digest, stable_hmac("value", namespace="discover"))
        self.assertNotEqual(digest, stable_hmac("value", namespace="history"))
