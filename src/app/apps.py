import logging
import sys
from importlib import import_module

from django.apps import AppConfig
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


def _is_celery_worker_process() -> bool:
    """Return whether the current process is a Celery worker or beat."""
    return any(
        "celery" in lowered_arg
        and ("worker" in lowered_arg or "beat" in lowered_arg)
        for arg in sys.argv
        for lowered_arg in [arg.lower()]
    )


class AppConfig(AppConfig):
    """Default app config."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "app"

    def ready(self):
        """Import signals when the app is ready."""
        import_module("app.signals")
        if not settings.TESTING:
            self._repair_celery_redis_bindings()
        is_celery_worker = _is_celery_worker_process()

        runtime_cache_available = self._add_startup_cache_key(
            "runtime_population_startup_scheduled",
        )
        discover_cache_available = self._add_startup_cache_key(
            "discover_tab_startup_scheduled",
        )
        history_cache_available = self._add_startup_cache_key(
            "history_day_coverage_startup_scheduled",
        )
        imdb_cache_available = self._add_startup_cache_key(
            "imdb_ratings_startup_scheduled",
        )

        if (
            not settings.TESTING
            and not getattr(settings, "RUNTIME_POPULATION_DISABLED", False)
            and getattr(settings, "RUNTIME_POPULATION_ON_STARTUP", False)
            and not is_celery_worker
            and runtime_cache_available
        ):
            self._schedule_runtime_population()

        if (
            not settings.TESTING
            and not is_celery_worker
            and getattr(settings, "DISCOVER_WARMUP_ON_STARTUP", False)
            and discover_cache_available
        ):
            self._schedule_discover_startup_warmup()

        if not settings.TESTING and not is_celery_worker and history_cache_available:
            self._schedule_history_day_coverage_warmup()

        if not settings.TESTING and not is_celery_worker:
            self._schedule_genre_backfill_reconcile()
            self._schedule_trakt_popularity_reconcile()
            if imdb_cache_available:
                self._schedule_imdb_ratings_bootstrap()

    def _add_startup_cache_key(self, cache_key: str) -> bool:
        """Return whether a once-per-day startup task can be scheduled."""
        try:
            return bool(cache.add(cache_key, 1, timeout=86400))
        except Exception:  # noqa: BLE001
            logger.debug(
                "Cache not available, skipping startup scheduling for %s",
                cache_key,
            )
            return False

    def _repair_celery_redis_bindings(self):
        """Normalize persisted Kombu Redis bindings after separator changes."""
        try:
            from app.celery_broker import repair_celery_redis_bindings  # noqa: PLC0415

            repair_summary = repair_celery_redis_bindings()
            if repair_summary["repaired"] or repair_summary["removed"]:
                logger.info(
                    (
                        "Normalized Kombu Redis bindings "
                        "(keys=%s members=%s repaired=%s removed=%s)"
                    ),
                    repair_summary["keys"],
                    repair_summary["members"],
                    repair_summary["repaired"],
                    repair_summary["removed"],
                )
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to normalize Kombu Redis bindings: %s", error)

    def _schedule_runtime_population(self):
        """Schedule runtime population task to run once on startup."""
        try:
            tasks = import_module("app.tasks")

            # Delay startup work until Django is fully initialized.
            tasks.populate_runtime_data_continuous.apply_async(
                countdown=60,
                priority=getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1),
            )
            logger.info("Scheduled runtime population task to run on startup")
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to schedule runtime population task: %s", error)

    def _schedule_discover_startup_warmup(self):
        """Schedule default Discover tab warmup shortly after startup."""
        try:
            tasks = import_module("app.tasks")
            tasks.warm_discover_startup_tabs.apply_async(
                countdown=90,
                priority=getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1),
            )
            logger.info("Scheduled Discover startup warmup")
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to schedule Discover startup warmup: %s", error)

    def _schedule_history_day_coverage_warmup(self):
        """Schedule low-priority history day cache coverage repair after startup."""
        try:
            tasks = import_module("app.tasks")
            tasks.warm_history_day_cache_coverage.apply_async(
                countdown=300,
                priority=getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1),
            )
            logger.info("Scheduled history day coverage warmup")
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to schedule history day coverage warmup: %s", error)

    def _schedule_genre_backfill_reconcile(self):
        """Schedule a one-time genre backfill reconcile."""
        try:
            tasks = import_module("app.tasks")
            version_key = f"genre_backfill_reconciled_v{tasks.GENRE_BACKFILL_VERSION}"
            status = cache.get(version_key)

            if status in {"done", "pending"}:
                return

            cache.set(version_key, "pending", timeout=300)

            try:
                tasks.reconcile_genre_backfill.apply_async(
                    kwargs={"strategy_version": tasks.GENRE_BACKFILL_VERSION},
                    countdown=0,
                    priority=getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1),
                )
            except Exception:
                cache.delete(version_key)
                raise

            logger.info(
                "Scheduled genre backfill reconcile (version=%s)",
                tasks.GENRE_BACKFILL_VERSION,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to schedule genre backfill reconcile: %s", error)

    def _schedule_trakt_popularity_reconcile(self):
        """Schedule Trakt popularity reconciliation on startup.

        Fires immediately when the formula version advances; once per day otherwise
        for catch-up.  Cache keys are only written after the task is successfully
        queued so a broker hiccup at startup never silently blocks future restarts.
        """
        try:
            from app.services.trakt_popularity import (  # noqa: PLC0415
                TRAKT_POPULARITY_SCORE_VERSION,
            )

            version_key = (
                "trakt_popularity_reconciled_"
                f"v{TRAKT_POPULARITY_SCORE_VERSION}"
            )
            daily_key = "trakt_popularity_reconcile_daily"

            version_status = cache.get(version_key)   # None | "pending" | "done"
            daily_status = cache.get(daily_key)        # None | 1

            version_done = version_status == "done"
            version_pending = version_status == "pending"

            if version_done and daily_status:
                return  # Already reconciled this version; daily catch-up also ran

            if version_pending and daily_status:
                return  # Task queued in the last 5 minutes; don't queue again

            is_version_recompute = not version_done

            tasks = import_module("app.tasks")
            tasks.reconcile_trakt_popularity.apply_async(
                kwargs={"score_version": TRAKT_POPULARITY_SCORE_VERSION},
                countdown=0 if is_version_recompute else 30,
                priority=getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1),
            )

            # Set keys only after successful queue so a failed apply_async
            # doesn't permanently block the next restart from trying again.
            if is_version_recompute:
                cache.set(version_key, "pending", timeout=300)
            cache.set(daily_key, 1, timeout=86400)

            logger.info(
                "Scheduled Trakt popularity reconcile (version_trigger=%s)",
                is_version_recompute,
            )
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to schedule Trakt popularity reconcile: %s", error)

    def _schedule_imdb_ratings_bootstrap(self):
        """Download IMDB dataset once on startup so ratings are immediately
        available without waiting for the nightly Celery Beat schedule."""
        try:
            from app.providers import imdb_datasets as _imdb_datasets  # noqa: PLC0415

            if not _imdb_datasets.is_enabled():
                return
            from app.models import Item  # noqa: PLC0415

            if Item.objects.filter(imdb_rating__isnull=False).exists():
                return
            tasks_imdb = import_module("app.tasks_imdb")
            tasks_imdb.refresh_imdb_ratings_from_datasets.apply_async(
                countdown=120,
                priority=getattr(settings, "CELERY_TASK_PRIORITY_BACKGROUND", 1),
            )
            logger.info("Scheduled IMDB ratings bootstrap on startup")
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to schedule IMDB ratings bootstrap: %s", error)
