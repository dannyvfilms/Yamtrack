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
        is_celery_worker = _is_celery_worker_process()

        runtime_cache_available = self._add_startup_cache_key(
            "runtime_population_startup_scheduled",
        )
        discover_cache_available = self._add_startup_cache_key(
            "discover_tab_startup_scheduled",
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
            and discover_cache_available
        ):
            self._schedule_discover_startup_warmup()

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

    def _schedule_runtime_population(self):
        """Schedule runtime population task to run once on startup."""
        try:
            tasks = import_module("app.tasks")

            # Delay startup work until Django is fully initialized.
            tasks.populate_runtime_data_continuous.apply_async(countdown=60)
            logger.info("Scheduled runtime population task to run on startup")
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to schedule runtime population task: %s", error)

    def _schedule_discover_startup_warmup(self):
        """Schedule default Discover tab warmup shortly after startup."""
        try:
            tasks = import_module("app.tasks")
            tasks.warm_discover_startup_tabs.apply_async(countdown=90)
            logger.info("Scheduled Discover startup warmup")
        except Exception as error:  # noqa: BLE001
            logger.warning("Failed to schedule Discover startup warmup: %s", error)
