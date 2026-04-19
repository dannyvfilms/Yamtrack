from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import history_cache
from app.log_safety import stable_hmac
from app.models import Item, MediaTypes, Movie, Sources, Status


class HistoryMonthCacheTests(TestCase):
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
        self.logging_style = history_cache._normalize_logging_style(None, self.user)
        self.today_key = history_cache.history_day_key(timezone.localtime())

    def tearDown(self):
        """Reset cache state between tests."""
        cache.clear()

    def _active_month_day_keys(self):
        month_prefix = timezone.localdate().strftime("%Y%m")
        index_days = history_cache.build_history_index(self.user, self.logging_style)
        return [day_key for day_key in index_days if day_key.startswith(month_prefix)]

    @patch("app.history_cache.schedule_history_refresh")
    def test_history_view_repairs_cold_month_inline_without_scheduling_refresh(
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
        self.assertEqual(mock_schedule_history_refresh.call_count, 0)

        active_month_day_keys = self._active_month_day_keys()
        cache_keys = [
            history_cache._day_cache_key(self.user.id, self.logging_style, day_key)
            for day_key in active_month_day_keys
        ]
        cached_payloads = cache.get_many(cache_keys)
        self.assertEqual(len(cached_payloads), len(cache_keys))
        self.assertEqual(len(response.context["history_days"]), len(active_month_day_keys))

    @patch("app.history_cache.schedule_history_refresh")
    def test_history_view_repairs_partial_month_miss_inline_without_refreshing(
        self,
        mock_schedule_history_refresh,
    ):
        first_response = self.client.get(reverse("history"))

        self.assertEqual(first_response.status_code, 200)
        self.assertFalse(first_response.context["history_refreshing"])

        cache.delete_many(
            [
                history_cache._day_cache_key(
                    self.user.id,
                    self.logging_style,
                    self.today_key,
                ),
            ],
        )

        response = self.client.get(reverse("history"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["history_refreshing"])
        self.assertTrue(response.context["history_days"])
        self.assertEqual(
            response.context["history_days"][0]["entries"][0]["title"],
            "Fallback Movie",
        )
        self.assertEqual(mock_schedule_history_refresh.call_count, 0)
        self.assertIsNotNone(
            cache.get(
                history_cache._day_cache_key(
                    self.user.id,
                    self.logging_style,
                    self.today_key,
                ),
            ),
        )
        self.assertEqual(
            len(response.context["history_days"]),
            len(self._active_month_day_keys()),
        )

    def test_refresh_history_cache_repairs_missing_index_day_payloads(self):
        history_cache.refresh_history_cache(self.user.id, logging_style=self.logging_style)

        cache.delete(
            history_cache._day_cache_key(
                self.user.id,
                self.logging_style,
                self.today_key,
            ),
        )

        history_cache.refresh_history_cache(self.user.id, logging_style=self.logging_style)

        index_entry = cache.get(history_cache._cache_key(self.user.id, self.logging_style))
        self.assertIsNotNone(index_entry)
        self.assertIn(self.today_key, index_entry["days"])
        self.assertIsNotNone(
            cache.get(
                history_cache._day_cache_key(
                    self.user.id,
                    self.logging_style,
                    self.today_key,
                ),
            ),
        )

    @patch("app.history_cache.schedule_history_refresh", return_value=True)
    def test_invalidate_history_days_keeps_existing_payload_until_refresh_overwrites_it(
        self,
        mock_schedule_history_refresh,
    ):
        history_cache.refresh_history_cache(self.user.id, logging_style=self.logging_style)
        cache_key = history_cache._day_cache_key(
            self.user.id,
            self.logging_style,
            self.today_key,
        )
        cached_payload = cache.get(cache_key)

        history_cache.invalidate_history_days(
            self.user.id,
            day_keys=[self.today_key],
            logging_styles=(self.logging_style,),
            reason="test_change",
        )

        self.assertEqual(cache.get(cache_key), cached_payload)
        mock_schedule_history_refresh.assert_called_once_with(
            self.user.id,
            self.logging_style,
            warm_days=0,
            day_keys=[self.today_key],
        )


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
