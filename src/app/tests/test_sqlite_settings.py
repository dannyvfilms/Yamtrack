from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase

from config import settings as project_settings


class SQLiteSettingsTests(SimpleTestCase):
    """Regression coverage for SQLite-specific runtime settings."""

    def test_configure_sqlite_connection_uses_configured_busy_timeout(self):
        """SQLite connections should honor the configured timeout value."""
        cursor = MagicMock()
        connection = SimpleNamespace(
            vendor="sqlite",
            cursor=MagicMock(return_value=cursor),
        )

        with (
            patch.object(project_settings, "SQLITE_JOURNAL_MODE", "WAL"),
            patch.object(project_settings, "SQLITE_SYNCHRONOUS", "NORMAL"),
            patch.object(project_settings, "SQLITE_BUSY_TIMEOUT_SECONDS", 17),
        ):
            project_settings.configure_sqlite_connection(
                sender=None,
                connection=connection,
            )

        cursor.execute.assert_any_call("PRAGMA journal_mode=WAL")
        cursor.execute.assert_any_call("PRAGMA synchronous=NORMAL")
        cursor.execute.assert_any_call("PRAGMA busy_timeout=17000")
        cursor.close.assert_called_once()

    def test_discover_warmup_defaults_off_on_sqlite(self):
        """SQLite deployments should not auto-enable Discover warmup by default."""
        self.assertFalse(settings.DISCOVER_WARMUP_ON_STARTUP)
