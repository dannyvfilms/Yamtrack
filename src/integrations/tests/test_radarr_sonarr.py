from datetime import UTC, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django_celery_beat.models import PeriodicTask

from app.models import MediaTypes, Sources
from integrations.imports import helpers, radarr, sonarr
from integrations.models import RadarrAccount, SonarrAccount


class ArrImportViewTests(TestCase):
    """Cover Radarr/Sonarr connect flows and recurring schedules."""

    def setUp(self):
        """Create an authenticated user for ARR view requests."""
        self.user = get_user_model().objects.create_user(
            username="arr-user",
        )
        self.client.force_login(self.user)

    @patch("integrations.views.tasks.import_radarr.delay")
    @patch("integrations.views.RadarrClient.healthcheck")
    def test_radarr_connect_creates_schedule_and_queues_initial_import(
        self,
        mock_healthcheck,
        mock_delay,
    ):
        """Connecting Radarr should persist credentials and enable recurring sync."""
        response = self.client.post(
            reverse("radarr_connect"),
            {
                "base_url": "https://radarr.local:7878",
                "api_key": "radarr-key",
            },
        )

        self.assertEqual(response.status_code, 302)
        account = RadarrAccount.objects.get(user=self.user)
        self.assertEqual(account.base_url, "https://radarr.local:7878")
        task = PeriodicTask.objects.get(task="Import from Radarr (Recurring)")
        self.assertTrue(task.enabled)
        self.assertEqual(
            task.name,
            f"Import from Radarr for {self.user.username} (every 2 hours)",
        )
        self.assertIn(f'"user_id": {self.user.id}', task.kwargs)
        mock_healthcheck.assert_called_once()
        mock_delay.assert_called_once_with(user_id=self.user.id, mode="new")

    @patch("integrations.views.tasks.import_radarr.delay")
    @patch("integrations.views.RadarrClient.healthcheck")
    @patch("integrations.views.timezone.now")
    def test_radarr_connect_starts_recurring_sync_on_next_boundary(
        self,
        mock_now,
        mock_healthcheck,
        mock_delay,
    ):
        """Connecting Radarr should not trigger the recurring task immediately."""
        mock_now.return_value = datetime(2026, 4, 24, 21, 10, tzinfo=UTC)

        response = self.client.post(
            reverse("radarr_connect"),
            {
                "base_url": "https://radarr.local:7878",
                "api_key": "radarr-key",
            },
        )

        self.assertEqual(response.status_code, 302)
        task = PeriodicTask.objects.get(task="Import from Radarr (Recurring)")
        self.assertEqual(task.start_time, datetime(2026, 4, 24, 22, 0, tzinfo=UTC))
        mock_healthcheck.assert_called_once()
        mock_delay.assert_called_once_with(user_id=self.user.id, mode="new")

    @patch("integrations.views.tasks.import_sonarr.delay")
    @patch("integrations.views.SonarrClient.healthcheck")
    def test_sonarr_connect_creates_schedule_and_queues_initial_import(
        self,
        mock_healthcheck,
        mock_delay,
    ):
        """Connecting Sonarr should persist credentials and enable recurring sync."""
        response = self.client.post(
            reverse("sonarr_connect"),
            {
                "base_url": "https://sonarr.local:8989",
                "api_key": "sonarr-key",
            },
        )

        self.assertEqual(response.status_code, 302)
        account = SonarrAccount.objects.get(user=self.user)
        self.assertEqual(account.base_url, "https://sonarr.local:8989")
        task = PeriodicTask.objects.get(task="Import from Sonarr (Recurring)")
        self.assertTrue(task.enabled)
        self.assertEqual(
            task.name,
            f"Import from Sonarr for {self.user.username} (every 2 hours)",
        )
        self.assertIn(f'"user_id": {self.user.id}', task.kwargs)
        mock_healthcheck.assert_called_once()
        mock_delay.assert_called_once_with(user_id=self.user.id, mode="new")


class ArrImporterTests(TestCase):
    """Cover Radarr/Sonarr metadata resolution during imports."""

    def setUp(self):
        """Create a user with connected ARR accounts."""
        self.user = get_user_model().objects.create_user(username="arr-importer")
        self.radarr_account = RadarrAccount.objects.create(
            user=self.user,
            base_url="https://radarr.local:7878",
            api_key=helpers.encrypt("radarr-key"),
        )
        self.sonarr_account = SonarrAccount.objects.create(
            user=self.user,
            base_url="https://sonarr.local:8989",
            api_key=helpers.encrypt("sonarr-key"),
        )

    @patch("integrations.imports.radarr.upsert_collection_source_state")
    @patch("integrations.imports.radarr.services.get_media_metadata")
    @patch("integrations.imports.radarr.RadarrClient.movies")
    def test_radarr_import_uses_media_type_first_metadata_lookup(
        self,
        mock_movies,
        mock_get_media_metadata,
        _mock_sync_state,
    ):
        """Radarr imports should call metadata lookup with the standard signature."""
        mock_movies.return_value = [
            {
                "title": "Arrival",
                "hasFile": True,
                "tmdbId": 26718,
                "movieFile": {"quality": {"quality": {"name": "Bluray-1080p"}}},
            },
        ]
        mock_get_media_metadata.return_value = {
            "title": "Arrival",
            "image": "https://example.com/arrival.jpg",
            "genres": [],
        }

        imported_counts, warnings = radarr.importer(None, self.user, "new")

        self.assertEqual(imported_counts[MediaTypes.MOVIE.value], 1)
        self.assertEqual(warnings, "")
        mock_get_media_metadata.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "26718",
            Sources.TMDB.value,
        )

    @patch("integrations.imports.sonarr.upsert_collection_source_state")
    @patch("integrations.imports.sonarr.services.get_media_metadata")
    @patch("integrations.imports.sonarr.SonarrClient.series")
    def test_sonarr_import_uses_media_type_first_metadata_lookup(
        self,
        mock_series,
        mock_get_media_metadata,
        _mock_sync_state,
    ):
        """Sonarr imports should call metadata lookup with the standard signature."""
        mock_series.return_value = [
            {
                "title": "Severance",
                "statistics": {"episodeFileCount": 9},
                "tmdbId": 95396,
            },
        ]
        mock_get_media_metadata.return_value = {
            "title": "Severance",
            "image": "https://example.com/severance.jpg",
            "genres": [],
        }

        imported_counts, warnings = sonarr.importer(None, self.user, "new")

        self.assertEqual(imported_counts[MediaTypes.TV.value], 1)
        self.assertEqual(warnings, "")
        mock_get_media_metadata.assert_called_once_with(
            MediaTypes.TV.value,
            "95396",
            Sources.TMDB.value,
        )
