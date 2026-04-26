from django.test import SimpleTestCase

from config.celery import app
from integrations import tasks


class GoodreadsTaskRegistrationTests(SimpleTestCase):
    """Tests for Goodreads Celery task registration."""

    def test_goodreads_task_registers_canonical_and_legacy_names(self):
        """Workers should accept both canonical and legacy Goodreads task names."""
        self.assertEqual(tasks.import_goodreads.name, tasks.GOODREADS_IMPORT_TASK_NAME)
        self.assertEqual(
            app.tasks[tasks.GOODREADS_IMPORT_TASK_NAME].name,
            tasks.GOODREADS_IMPORT_TASK_NAME,
        )

        for task_name in tasks.LEGACY_GOODREADS_IMPORT_TASK_NAMES:
            self.assertIn(task_name, app.tasks)
