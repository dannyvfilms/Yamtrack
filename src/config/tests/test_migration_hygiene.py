from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.test import TestCase

from app.management.commands import check_migration_hygiene


class _FakeGraph:
    def __init__(self, leaf_map):
        self._leaf_map = leaf_map

    def leaf_nodes(self, app_label):
        return self._leaf_map.get(app_label, [])


class MigrationHygieneCommandTests(TestCase):
    """Tests for migration hygiene command helpers and smoke behavior."""

    def test_command_passes_with_head_baseline_for_users(self):
        """The command should pass against HEAD baseline for a stable app graph."""
        output = StringIO()

        call_command(
            "check_migration_hygiene",
            base_ref="HEAD",
            apps="users",
            stdout=output,
        )

        self.assertIn("Migration hygiene checks passed", output.getvalue())

    def test_collect_multi_leaf_apps_flags_branch_splits(self):
        """Branch splits should be reported when an app has multiple leaf nodes."""
        graph = _FakeGraph(
            {
                "users": [
                    ("users", "0040_feature_branch_a"),
                    ("users", "0040_feature_branch_b"),
                ],
                "app": [("app", "0093_latest")],
            }
        )

        result = check_migration_hygiene._collect_multi_leaf_apps(
            graph, ["users", "app"]
        )

        self.assertEqual(
            result,
            {
                "users": [
                    "users.0040_feature_branch_a",
                    "users.0040_feature_branch_b",
                ]
            },
        )

    def test_find_risky_operations_detects_raw_schema_ops(self):
        """Raw migrations.AddConstraint should be detected as a risky operation."""
        with TemporaryDirectory() as tmp_dir:
            migration_path = Path(tmp_dir) / "9999_bad_migration.py"
            migration_path.write_text(
                (
                    "from django.db import migrations\n"
                    "\n"
                    "class Migration(migrations.Migration):\n"
                    "    operations = [\n"
                    "        migrations.AddConstraint(\n"
                    "            model_name='user',\n"
                    "            constraint=None,\n"
                    "        ),\n"
                    "        AddConstraintIfNotExists(\n"
                    "            model_name='user',\n"
                    "            constraint=None,\n"
                    "        ),\n"
                    "    ]\n"
                ),
                encoding="utf-8",
            )

            violations = check_migration_hygiene._find_risky_operations(
                migration_path,
                "src/users/migrations/9999_bad_migration.py",
            )

        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].operation, "AddConstraint")
        self.assertEqual(violations[0].line_number, 5)
