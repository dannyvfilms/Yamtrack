import logging
import sys
from django.apps import AppConfig
from django.conf import settings
from django.core.cache import cache


logger = logging.getLogger(__name__)


class AppConfig(AppConfig):
    """Default app config."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "app"

    def ready(self):
        """Import signals when the app is ready."""
        import app.signals  # noqa: F401, PLC0415
        
        # Skip scheduling if we're in a Celery worker process to avoid duplicates
        # Only schedule from the main Django process (runserver, management commands, etc.)
        is_celery_worker = any(
            'celery' in arg.lower() and ('worker' in arg.lower() or 'beat' in arg.lower())
            for arg in sys.argv
        )
        
        # Only schedule runtime population once per day to avoid duplicates
        # Use a 24-hour cache timeout and skip Celery worker processes
        # Wrap cache access in try/except to handle cases where Redis isn't available (e.g., during Docker build)
        cache_key = "runtime_population_startup_scheduled"
        try:
            cache_available = cache.add(cache_key, True, timeout=86400)  # 24 hours
        except Exception:
            # Redis/cache not available (e.g., during Docker build, collectstatic, etc.)
            # Skip scheduling to avoid errors during non-runtime operations
            cache_available = False
            logger.debug("Cache not available, skipping runtime population scheduling")
        
        if (
            not settings.TESTING
            and not getattr(settings, 'RUNTIME_POPULATION_DISABLED', False)
            and not is_celery_worker
            and cache_available
        ):
            self._schedule_runtime_population()

    def _schedule_runtime_population(self):
        """Schedule runtime population task to run once on startup."""
        try:
            from app.tasks import populate_runtime_data_continuous
            
            # Schedule the task to run in 60 seconds to allow the app to fully start
            # and avoid database access warnings
            populate_runtime_data_continuous.apply_async(countdown=60)
            logger.info("Scheduled runtime population task to run on startup")
        except Exception as e:
            logger.warning(f"Failed to schedule runtime population task: {e}")
