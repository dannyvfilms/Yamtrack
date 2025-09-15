import logging
from django.apps import AppConfig
from django.conf import settings


logger = logging.getLogger(__name__)


class AppConfig(AppConfig):
    """Default app config."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "app"

    def ready(self):
        """Import signals when the app is ready."""
        import app.signals  # noqa: F401, PLC0415
        
        # Run runtime population task on startup (only in production, not during testing)
        if not settings.TESTING and not getattr(settings, 'RUNTIME_POPULATION_DISABLED', False):
            self._schedule_runtime_population()

    def _schedule_runtime_population(self):
        """Schedule runtime population task to run once on startup."""
        try:
            from app.tasks import populate_runtime_data_continuous
            
            # Schedule the task to run in 30 seconds to allow the app to fully start
            populate_runtime_data_continuous.apply_async(countdown=30)
            logger.info("Scheduled runtime population task to run on startup")
        except Exception as e:
            logger.warning(f"Failed to schedule runtime population task: {e}")
