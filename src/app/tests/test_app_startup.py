from importlib import import_module
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings

from app.apps import AppConfig as YamtrackAppConfig
from app.models import Item, MediaTypes, Sources
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
        )
        self.assertEqual(cache.get(version_key), "pending")

        cache.set(version_key, "done", timeout=None)
        config._schedule_genre_backfill_reconcile()

        mock_apply_async.assert_called_once()

    @patch("app.tasks.reconcile_genre_backfill.apply_async")
    def test_schedule_genre_backfill_reconcile_overrides_stale_done_when_candidates_exist(
        self,
        mock_apply_async,
    ):
        version_key = f"genre_backfill_reconciled_v{GENRE_BACKFILL_VERSION}"
        cache.set(version_key, "done", timeout=None)
        Item.objects.create(
            media_id="startup-stale-done",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Startup Stale Done",
            image="https://example.com/startup-stale-done.jpg",
            genres=["Drama"],
        )
        config = YamtrackAppConfig("app", import_module("app"))

        config._schedule_genre_backfill_reconcile()

        mock_apply_async.assert_called_once_with(
            kwargs={"strategy_version": GENRE_BACKFILL_VERSION},
            countdown=0,
        )
        self.assertEqual(cache.get(version_key), "pending")

    def test_settings_include_genre_backfill_reconcile_fallback_schedule(self):
        schedule = settings.CELERY_BEAT_SCHEDULE["ensure_genre_backfill_reconcile"]

        self.assertEqual(schedule["task"], "Ensure genre backfill reconcile")
        self.assertEqual(schedule["schedule"], 60 * 5)
        self.assertEqual(schedule["kwargs"]["batch_size"], 1500)
