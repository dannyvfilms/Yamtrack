from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.db import models
from django.test import SimpleTestCase

migration_0043 = import_module(
    "users.migrations.0043_add_boardgame_preferences",
)
migration_0067 = import_module(
    "users.migrations.0067_remove_user_tv_sort_valid_and_more",
)
migration_0090 = import_module(
    "users.migrations.0090_homescreenrow",
)


class _FakeCursor:
    def __init__(self, fetchall_results=None):
        self.fetchall_results = list(fetchall_results or [])
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


class Migration0043NormalizationTests(SimpleTestCase):
    """Regression tests for migration 0043 last_search_type normalization."""

    def test_add_constraint_operation_normalizes_last_search_type_first(self):
        """AddConstraint should backfill values before checking the constraint."""
        operation = migration_0043.AddConstraintIfNotExists(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=models.Q(last_search_type__in=["tv"]),
                name="last_search_type_valid",
            ),
        )
        fake_model = SimpleNamespace(_meta=SimpleNamespace(db_table="users_user"))
        to_state = SimpleNamespace(
            apps=SimpleNamespace(get_model=lambda _app, _model: fake_model),
        )
        schema_editor = SimpleNamespace(connection=SimpleNamespace(vendor="sqlite"))

        with (
            patch.object(
                migration_0043,
                "_normalize_invalid_last_search_type_values",
            ) as normalize_mock,
            patch.object(migration_0043, "_constraint_exists", return_value=True),
        ):
            operation.database_forwards(
                "users",
                schema_editor,
                from_state=SimpleNamespace(),
                to_state=to_state,
            )

        normalize_mock.assert_called_once_with(schema_editor)

    def test_normalizes_invalid_last_search_type_values(self):
        """Legacy invalid values are coerced to 'tv' before adding the check."""
        cursor = _FakeCursor()
        schema_editor = SimpleNamespace(
            connection=SimpleNamespace(cursor=lambda: cursor),
        )

        migration_0043._normalize_invalid_last_search_type_values(schema_editor)

        self.assertEqual(
            cursor.calls,
            [
                (
                    "UPDATE users_user SET last_search_type = %s "
                    "WHERE last_search_type IS NULL OR last_search_type = '' "
                    "OR last_search_type NOT IN (%s,%s,%s,%s,%s,%s,%s,%s)",
                    [
                        "tv",
                        "tv",
                        "movie",
                        "anime",
                        "manga",
                        "game",
                        "book",
                        "comic",
                        "boardgame",
                    ],
                ),
            ],
        )


class _FakeHomeScreenRow:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Migration0090HomeScreenSeedTests(SimpleTestCase):
    """Regression tests for home screen seed defaults."""

    def test_enabled_media_types_for_user_respects_sidebar_flags(self):
        user = SimpleNamespace(
            tv_enabled=True,
            season_enabled=True,
            movie_enabled=False,
            anime_enabled=True,
        )

        enabled = migration_0090._enabled_media_types_for_user(user)

        self.assertEqual(enabled[:3], ["tv", "season", "anime"])
        self.assertNotIn("movie", enabled)

    def test_default_rows_include_planning_when_legacy_preference_was_enabled(self):
        user = SimpleNamespace(
            id=7,
            season_enabled=True,
            home_sort="completion",
            show_planned_on_home="combined",
        )

        rows = migration_0090._default_rows_for_user(user, _FakeHomeScreenRow)

        self.assertEqual(len(rows), 2)
        self.assertEqual([row.position for row in rows], [0, 1])
        self.assertEqual([row.row_type for row in rows], ["library_query", "library_query"])
        self.assertEqual(rows[0].filters["status"], "In progress")
        self.assertEqual(rows[0].sort_by, "completion")
        self.assertEqual(rows[0].direction, "desc")
        self.assertEqual(rows[1].filters["status"], "Planning")
        self.assertEqual(rows[1].sort_by, "completion")
        self.assertEqual(rows[1].direction, "desc")

    def test_default_rows_skip_planning_when_legacy_preference_was_disabled(self):
        user = SimpleNamespace(
            id=9,
            movie_enabled=True,
            home_sort="title",
            show_planned_on_home="disabled",
        )

        rows = migration_0090._default_rows_for_user(user, _FakeHomeScreenRow)

        self.assertEqual(len(rows), 1)
        self.assertEqual([row.row_type for row in rows], ["library_query"])
        self.assertEqual(rows[0].direction, "asc")

    def test_default_rows_translate_legacy_upcoming_to_old_home_shape(self):
        user = SimpleNamespace(
            id=11,
            tv_enabled=True,
            season_enabled=True,
            movie_enabled=True,
            home_sort="upcoming",
            show_planned_on_home="disabled",
        )

        rows = migration_0090._default_rows_for_user(user, _FakeHomeScreenRow)

        self.assertEqual([row.media_type for row in rows], ["season", "movie"])
        self.assertEqual(rows[0].sort_by, "upcoming")
        self.assertEqual(rows[0].direction, "asc")
        self.assertEqual(rows[1].sort_by, "recent")
        self.assertEqual(rows[1].direction, "desc")
