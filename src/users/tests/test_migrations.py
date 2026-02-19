from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock

from django.test import SimpleTestCase

migration_0067 = import_module(
    "users.migrations.0067_remove_user_tv_sort_valid_and_more",
)


class _FakeCursor:
    def __init__(self, fetchall_results):
        self.fetchall_results = list(fetchall_results)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self.fetchall_results.pop(0)


class Migration0067NormalizationTests(SimpleTestCase):
    """Regression tests for migration 0067 SQLite normalization."""

    def test_skips_for_non_sqlite_connections(self):
        """The guard clause should no-op when not using SQLite."""
        schema_editor = SimpleNamespace(
            connection=SimpleNamespace(vendor="postgresql", cursor=MagicMock()),
        )

        migration_0067._normalize_invalid_auto_pause_rules(None, schema_editor)

        schema_editor.connection.cursor.assert_not_called()

    def test_normalizes_invalid_sqlite_values_before_table_remake(self):
        """Invalid JSON scalars and blobs are rewritten to an empty JSON list."""
        cursor = _FakeCursor(
            fetchall_results=[
                [(0, "id"), (1, "auto_pause_rules")],
                [
                    (1, "[]"),
                    (2, "{}"),
                    (3, '"oops"'),
                    (4, ""),
                    (5, "auto_pause_rules"),
                    (6, b"\x80\x81"),
                    (7, None),
                ],
            ],
        )
        schema_editor = SimpleNamespace(
            connection=SimpleNamespace(vendor="sqlite", cursor=lambda: cursor),
        )

        migration_0067._normalize_invalid_auto_pause_rules(None, schema_editor)

        update_calls = [
            (sql, params)
            for sql, params in cursor.calls
            if sql.startswith("UPDATE users_user SET auto_pause_rules")
        ]

        self.assertEqual(
            update_calls,
            [
                (
                    "UPDATE users_user SET auto_pause_rules = %s WHERE id = %s",
                    ["[]", 2],
                ),
                (
                    "UPDATE users_user SET auto_pause_rules = %s WHERE id = %s",
                    ["[]", 3],
                ),
                (
                    "UPDATE users_user SET auto_pause_rules = %s WHERE id = %s",
                    ["[]", 4],
                ),
                (
                    "UPDATE users_user SET auto_pause_rules = %s WHERE id = %s",
                    ["[]", 5],
                ),
                (
                    "UPDATE users_user SET auto_pause_rules = %s WHERE id = %s",
                    ["[]", 6],
                ),
                (
                    "UPDATE users_user SET auto_pause_rules = %s WHERE id = %s",
                    ["[]", 7],
                ),
            ],
        )
