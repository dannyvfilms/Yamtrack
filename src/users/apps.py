from importlib import import_module

from django.apps import AppConfig


class UsersConfig(AppConfig):
    """Users app configuration."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "users"

    def ready(self):
        """Register user-related signal handlers."""
        import_module("users.signals")
