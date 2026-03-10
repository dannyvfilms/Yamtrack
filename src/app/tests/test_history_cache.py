from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import history_cache
from app.log_safety import stable_hmac
from app.models import Item, MediaTypes, Movie, Sources, Status


class HistoryCacheFallbackTests(TestCase):
    def setUp(self):
        """Create a user with one history entry and a cold cache."""
        cache.clear()
        self.credentials = {"username": "history-fallback", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        item = Item.objects.create(
            media_id="movie-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Fallback Movie",
            image="http://example.com/movie.jpg",
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )
        cache.clear()
        self.client.login(**self.credentials)

    def tearDown(self):
        """Reset cache state between tests."""
        cache.clear()

    @patch("app.history_cache.schedule_history_refresh", return_value=False)
    def test_history_view_builds_month_inline_when_refresh_cannot_be_scheduled(
        self,
        mock_schedule_history_refresh,
    ):
        response = self.client.get(reverse("history"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["history_refreshing"])
        self.assertTrue(response.context["history_days"])
        self.assertEqual(
            response.context["history_days"][0]["entries"][0]["title"],
            "Fallback Movie",
        )
        self.assertEqual(mock_schedule_history_refresh.call_count, 1)

        day_key = history_cache.history_day_key(timezone.localtime())
        cache_key = history_cache._day_cache_key(
            self.user.id,
            history_cache._normalize_logging_style(None, self.user),
            day_key,
        )
        self.assertIsNotNone(cache.get(cache_key))


class HistoryRefreshSchedulingTests(TestCase):
    def setUp(self):
        """Create a user for lock cleanup tests."""
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="history-scheduling",
            password="12345",
        )

    def tearDown(self):
        """Reset cache state between tests."""
        cache.clear()

    @patch("app.tasks.refresh_history_cache_task.apply_async", side_effect=RuntimeError("broker unavailable"))
    def test_schedule_history_refresh_clears_locks_when_enqueue_fails(self, _mock_apply_async):
        day_key = timezone.localdate().isoformat()

        scheduled = history_cache.schedule_history_refresh(
            self.user.id,
            "repeats",
            day_keys=[day_key],
            allow_inline=False,
        )

        self.assertFalse(scheduled)

        normalized_day_key = history_cache.history_day_key(day_key)
        lock_key = history_cache._refresh_lock_key(self.user.id, "repeats")
        dedupe_hash = stable_hmac(
            normalized_day_key,
            namespace="history_refresh_days",
            length=10,
        )
        dedupe_key = f"{lock_key}_days_{dedupe_hash}"

        self.assertIsNone(cache.get(lock_key))
        self.assertIsNone(cache.get(dedupe_key))
