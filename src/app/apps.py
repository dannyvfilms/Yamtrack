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
        
        # Only schedule runtime population once per startup to avoid duplicates
        if (not settings.TESTING and 
            not getattr(settings, 'RUNTIME_POPULATION_DISABLED', False) and
            not hasattr(self, '_runtime_population_scheduled')):
            self._schedule_runtime_population()
            self._runtime_population_scheduled = True

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
