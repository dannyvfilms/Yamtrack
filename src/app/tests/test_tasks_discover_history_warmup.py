from unittest.mock import patch

from django.test import SimpleTestCase

from app.tasks_discover import warm_history_day_cache_coverage


class WarmHistoryDayCacheCoverageTaskTests(SimpleTestCase):
    @patch("app.tasks_discover.interactive_request_active", return_value=True)
    def test_skips_when_interactive_request_is_active(
        self,
        _mock_interactive_request_active,
    ):
        result = warm_history_day_cache_coverage()

        self.assertEqual(
            result,
            {
                "scheduled": 0,
                "users_count": 0,
                "logging_styles": ["sessions", "repeats"],
                "reason": "interactive_request_active",
            },
        )
