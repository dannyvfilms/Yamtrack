# ruff: noqa: D101, D102

from importlib import import_module
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings

from app.apps import AppConfig as YamtrackAppConfig
from app.tasks import GENRE_BACKFILL_VERSION


class AppStartupTests(TestCase):
    @override_settings(
        TESTING=False,
        DISCOVER_WARMUP_ON_STARTUP=False,
        RUNTIME_POPULATION_ON_STARTUP=False,
    )
    @patch("app.apps._is_celery_worker_process", return_value=False)
    def test_app_ready_schedules_genre_backfill_reconcile(
        self,
        _mock_is_celery_worker,
    ):
        config = YamtrackAppConfig("app", import_module("app"))

        with (
            patch.object(config, "_repair_celery_redis_bindings"),
            patch.object(config, "_schedule_history_day_coverage_warmup"),
            patch.object(config, "_schedule_genre_backfill_reconcile") as mock_schedule,
            patch.object(config, "_schedule_trakt_popularity_reconcile"),
        ):
            config.ready()

        mock_schedule.assert_called_once_with()

    @patch("app.tasks.reconcile_genre_backfill.apply_async")
    def test_schedule_genre_backfill_reconcile_marks_pending_until_worker_runs(
        self,
        mock_apply_async,
    ):
        version_key = f"genre_backfill_reconciled_v{GENRE_BACKFILL_VERSION}"
        cache.delete(version_key)
        config = YamtrackAppConfig("app", import_module("app"))

        config._schedule_genre_backfill_reconcile()
        config._schedule_genre_backfill_reconcile()

        mock_apply_async.assert_called_once_with(
            kwargs={"strategy_version": GENRE_BACKFILL_VERSION},
            countdown=0,
            priority=settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )
        self.assertEqual(cache.get(version_key), "pending")

        cache.set(version_key, "done", timeout=None)
        config._schedule_genre_backfill_reconcile()

        mock_apply_async.assert_called_once()

    @patch("app.tasks.is_genre_backfill_reconcile_complete")
    @patch("app.tasks.reconcile_genre_backfill.apply_async")
    def test_schedule_genre_backfill_reconcile_skips_done_cache_without_db_check(
        self,
        mock_apply_async,
        mock_is_complete,
    ):
        version_key = f"genre_backfill_reconciled_v{GENRE_BACKFILL_VERSION}"
        cache.set(version_key, "done", timeout=None)
        config = YamtrackAppConfig("app", import_module("app"))

        config._schedule_genre_backfill_reconcile()

        mock_apply_async.assert_not_called()
        mock_is_complete.assert_not_called()

    def test_settings_include_genre_backfill_reconcile_fallback_schedule(self):
        schedule = settings.CELERY_BEAT_SCHEDULE["ensure_genre_backfill_reconcile"]

        self.assertEqual(schedule["task"], "Ensure genre backfill reconcile")
        self.assertEqual(schedule["schedule"], 60 * 5)
        self.assertEqual(schedule["kwargs"]["batch_size"], 1500)
        self.assertEqual(
            schedule["options"]["priority"],
            settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )

    def test_celery_background_routes_and_prefetch_are_enabled(self):
        self.assertEqual(settings.CELERY_WORKER_PREFETCH_MULTIPLIER, 1)
        self.assertEqual(
            settings.CELERY_TASK_ROUTES["app.tasks.populate_*"]["priority"],
            settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )
        self.assertEqual(
            settings.CELERY_TASK_ROUTES["Backfill item metadata"]["priority"],
            settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )
        self.assertEqual(
            settings.CELERY_TASK_ROUTES["Warm History Day Cache Coverage"]["priority"],
            settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )
        self.assertEqual(
            settings.CELERY_TASK_ROUTES[
                "Repair History Day Cache Coverage"
            ]["priority"],
            settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )

    @patch("app.tasks.warm_history_day_cache_coverage.apply_async")
    def test_schedule_history_day_coverage_warmup_uses_background_priority(
        self,
        mock_apply_async,
    ):
        config = YamtrackAppConfig("app", import_module("app"))

        config._schedule_history_day_coverage_warmup()

        mock_apply_async.assert_called_once_with(
            countdown=120,
            priority=settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )

    def test_settings_include_history_day_coverage_fallback_schedule(self):
        schedule = settings.CELERY_BEAT_SCHEDULE["warm_history_day_cache_coverage"]

        self.assertEqual(schedule["task"], "Warm History Day Cache Coverage")
        self.assertEqual(schedule["schedule"], 60 * 60 * 2)
        self.assertEqual(
            schedule["options"]["priority"],
            settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )
