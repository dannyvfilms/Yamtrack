from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import get_random_string
from django_celery_results.models import TaskResult

from integrations.models import LastFMAccount, LastFMHistoryImportStatus, PlexAccount
from integrations.plex import PlexAuthError


class ImportDataViewTests(TestCase):
    """Tests for the import data settings view."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "importuser", "password": "testpass123"}
        self.plex_token = get_random_string(16)
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

    @patch("users.views.plex.list_sections")
    @patch("users.views.plex.fetch_account")
    def test_import_data_skips_live_plex_checks_during_initial_render(
        self,
        mock_fetch_account,
        mock_list_sections,
    ):
        """The import page should render from cached Plex state."""
        PlexAccount.objects.create(
            user=self.user,
            plex_token=self.plex_token,
            plex_username="listener",
        )

        response = self.client.get(reverse("import_data"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Checking")
        mock_fetch_account.assert_not_called()
        mock_list_sections.assert_not_called()

    @patch(
        "users.views.plex.fetch_account",
        return_value={"username": "updated-listener"},
    )
    def test_import_data_plex_status_verifies_connection_lazily(
        self,
        mock_fetch_account,
    ):
        """The lazy status endpoint should verify the token."""
        PlexAccount.objects.create(
            user=self.user,
            plex_token=self.plex_token,
            plex_username="listener",
        )

        response = self.client.get(reverse("import_data_plex_status"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "state": "connected",
                "error": "",
            },
        )

        self.user.refresh_from_db()
        self.assertEqual(self.user.plex_account.plex_username, "updated-listener")
        mock_fetch_account.assert_called_once_with(self.plex_token)

    @patch("users.views.plex.fetch_account", side_effect=PlexAuthError("bad token"))
    def test_import_data_plex_status_returns_error_state_for_invalid_token(
        self,
        _mock_fetch_account,
    ):
        """The lazy Plex status endpoint should surface invalid-token failures."""
        PlexAccount.objects.create(
            user=self.user,
            plex_token=self.plex_token,
            plex_username="listener",
        )

        response = self.client.get(reverse("import_data_plex_status"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "state": "error",
                "error": "Plex token expired or revoked. Please reconnect.",
            },
        )

    @patch(
        "users.views.plex.list_sections",
        return_value=[
            {
                "machine_identifier": "server-1",
                "id": "12",
                "title": "Movies",
                "server_name": "Living Room",
                "type": "movie",
            },
        ],
    )
    def test_import_data_plex_sections_refreshes_libraries_lazily(
        self,
        mock_list_sections,
    ):
        """The lazy libraries endpoint should refresh cached sections."""
        PlexAccount.objects.create(
            user=self.user,
            plex_token=self.plex_token,
            plex_username="listener",
        )

        response = self.client.get(reverse("import_data_plex_sections"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "sections": [
                    {
                        "machine_identifier": "server-1",
                        "id": "12",
                        "title": "Movies",
                        "server_name": "Living Room",
                        "type": "movie",
                    },
                ],
                "error": "",
            },
        )

        self.user.refresh_from_db()
        self.assertEqual(self.user.plex_account.sections[0]["title"], "Movies")
        self.assertIsNotNone(self.user.plex_account.sections_refreshed_at)
        mock_list_sections.assert_called_once_with(self.plex_token)
