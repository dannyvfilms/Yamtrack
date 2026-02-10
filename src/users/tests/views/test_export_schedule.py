from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django_celery_beat.models import PeriodicTask


class ExportScheduleTests(TestCase):
    """Tests for combined one-time export and recurring export schedule flow."""

    def setUp(self):
        """Create and login user for the tests."""
        self.credentials = {"username": "testuser", "password": "testpass123"}
        get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_export_once_returns_csv_without_schedule(self):
        """One-time export should return CSV response and not create a schedule."""
        response = self.client.post(
            reverse("create_export_schedule"),
            {
                "frequency": "once",
                "media_types": ["tv", "movie"],
                "include_lists": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment; filename=", response["Content-Disposition"])
        self.assertEqual(PeriodicTask.objects.count(), 0)

    def test_recurring_export_creates_schedule_and_returns_csv(self):
        """Recurring export should create schedule and return immediate CSV."""
        response = self.client.post(
            reverse("create_export_schedule"),
            {
                "frequency": "daily",
                "time": "04:30",
                "media_types": ["tv", "movie"],
                "include_lists": "on",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment; filename=", response["Content-Disposition"])

        self.assertEqual(PeriodicTask.objects.count(), 1)
        task = PeriodicTask.objects.first()
        self.assertEqual(task.task, "Scheduled backup export")
        self.assertIn('"user_id"', task.kwargs)
        self.assertIn('"media_types": ["tv", "movie"]', task.kwargs)
        self.assertIn('"include_lists": true', task.kwargs)

    def test_invalid_frequency_redirects_without_schedule(self):
        """Invalid frequency should redirect and avoid schedule creation."""
        response = self.client.post(
            reverse("create_export_schedule"),
            {
                "frequency": "monthly",
                "time": "04:30",
            },
        )

        self.assertRedirects(response, reverse("export_data"))
        self.assertEqual(PeriodicTask.objects.count(), 0)
