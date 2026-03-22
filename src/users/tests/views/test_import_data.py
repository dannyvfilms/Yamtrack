from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_celery_results.models import TaskResult

from integrations.models import LastFMAccount, LastFMHistoryImportStatus


class ImportDataViewTests(TestCase):
    """Tests for the import data settings view."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "importuser", "password": "testpass123"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_import_data_ignores_structured_recurring_wrapper_results(self):
        """Recurring wrapper payloads should not break the import page."""
        TaskResult.objects.create(
            task_id="task-recurring",
            task_name="Import from Audiobookshelf (Recurring)",
            task_kwargs=(f'{{"user_id": {self.user.id}}}'),
            status="SUCCESS",
            date_done=timezone.now(),
            result='["child-task-id", null]',
        )

        response = self.client.get(reverse("import_data"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "users/import_data.html")

    def test_import_data_shows_lastfm_history_status_and_action(self):
        """Last.fm card should render history backfill status and rerun controls."""
        LastFMAccount.objects.create(
            user=self.user,
            lastfm_username="listener",
            last_fetch_timestamp_uts=1700000000,
            history_import_status=LastFMHistoryImportStatus.FAILED,
            history_import_total_pages=6,
            history_import_next_page=2,
            history_import_last_error_message="Temporary Last.fm error",
        )

        response = self.client.get(reverse("import_data"))

        self.assertContains(response, "Full history import:")
        self.assertContains(response, "Failed")
        self.assertContains(response, "Page 2 of 6")
        self.assertContains(response, "Reimport full history")
