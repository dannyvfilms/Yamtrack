from unittest.mock import call, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from app import statistics_cache


class StatisticsRefreshSchedulingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="stats-refresh-user",
            password="secret123",
        )

    def tearDown(self):
        cache.clear()

    @patch("app.tasks.refresh_statistics_cache_task.apply_async")
    def test_schedule_statistics_refresh_uses_interactive_priority_by_default(
        self,
        mock_apply_async,
    ):
        scheduled = statistics_cache.schedule_statistics_refresh(
            self.user.id,
            "This Month",
            allow_inline=False,
        )

        self.assertTrue(scheduled)
        mock_apply_async.assert_called_once()
        self.assertEqual(
            mock_apply_async.call_args.kwargs["priority"],
            settings.CELERY_TASK_PRIORITY_INTERACTIVE,
        )

    @patch("app.statistics_cache.schedule_statistics_refresh")
    def test_schedule_all_ranges_refresh_prioritizes_preferred_and_cached_all_time(
        self,
        mock_schedule_statistics_refresh,
    ):
        self.user.statistics_default_range = "This Month"
        self.user.save(update_fields=["statistics_default_range"])
        cache.set(
            statistics_cache._cache_key(self.user.id, "All Time"),
            {"history_version": "cached"},
            timeout=60,
        )

        statistics_cache.schedule_all_ranges_refresh(
            self.user.id,
            debounce_seconds=0,
            countdown=3,
        )

        mock_schedule_statistics_refresh.assert_has_calls(
            [
                call(
                    self.user.id,
                    "This Month",
                    debounce_seconds=0,
                    countdown=3,
                    allow_inline=False,
                    priority=settings.CELERY_TASK_PRIORITY_FOLLOWUP,
                ),
                call(
                    self.user.id,
                    "All Time",
                    debounce_seconds=0,
                    countdown=3 + statistics_cache.STATISTICS_ALL_TIME_REFRESH_DELAY,
                    allow_inline=False,
                    priority=settings.CELERY_TASK_PRIORITY_BACKGROUND,
                ),
            ],
        )
        self.assertEqual(mock_schedule_statistics_refresh.call_count, 2)

    @patch("app.statistics_cache.schedule_statistics_refresh")
    def test_schedule_all_ranges_refresh_skips_uncached_all_time(
        self,
        mock_schedule_statistics_refresh,
    ):
        self.user.statistics_default_range = "Last 90 Days"
        self.user.save(update_fields=["statistics_default_range"])

        statistics_cache.schedule_all_ranges_refresh(
            self.user.id,
            debounce_seconds=0,
            countdown=5,
        )

        mock_schedule_statistics_refresh.assert_called_once_with(
            self.user.id,
            "Last 90 Days",
            debounce_seconds=0,
            countdown=5,
            allow_inline=False,
            priority=settings.CELERY_TASK_PRIORITY_FOLLOWUP,
        )
