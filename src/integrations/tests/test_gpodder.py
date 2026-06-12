from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule
from django_celery_beat.models import PeriodicTask

from integrations import gpodder_api, tasks
from integrations.imports import helpers
from integrations.models import GPodderAccount


class GPodderApiTests(SimpleTestCase):
    """Tests for the GPodder API helpers."""

    def test_normalize_server_url_defaults_to_https(self):
        self.assertEqual(
            gpodder_api.normalize_server_url("demo.example.com/"),
            "https://demo.example.com",
        )

    @patch("integrations.gpodder_api.requests.request")
    def test_verify_login_success(self, mock_request):
        mock_request.return_value.status_code = 200
        mock_request.return_value.text = ""

        gpodder_api.verify_login(
            gpodder_api.GPodderCredentials(
                server_url="https://gpodder.net",
                username="listener",
                password="secret",
            ),
        )

        self.assertEqual(mock_request.call_args.kwargs["auth"], ("listener", "secret"))

    @patch("integrations.gpodder_api.requests.request")
    def test_verify_login_failure(self, mock_request):
        mock_request.return_value.status_code = 401
        mock_request.return_value.text = "nope"

        with self.assertRaises(gpodder_api.GPodderAuthError):
            gpodder_api.verify_login(
                gpodder_api.GPodderCredentials(
                    server_url="https://gpodder.net",
                    username="listener",
                    password="bad",
                ),
            )


class GPodderViewAndTaskTests(TestCase):
    """Tests for GPodder views and task registration."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="listener",
            password="pass12345",  # noqa: S106
        )
        self.client.login(username="listener", password="pass12345")

    @patch("integrations.views.tasks.import_gpodder.delay")
    @patch("integrations.views.gpodder_api.verify_login")
    def test_connect_success_creates_account_and_schedule(self, mock_verify_login, mock_delay):
        response = self.client.post(
            reverse("gpodder_connect"),
            {
                "server_url": "demo.example.com/",
                "username": "listener",
                "password": "secret",
                "device_filter": "phone",
            },
        )

        self.assertRedirects(response, reverse("import_data"))
        account = GPodderAccount.objects.get(user=self.user)
        self.assertEqual(helpers.decrypt(account.server_url), "https://demo.example.com")
        self.assertEqual(account.device_filter, "phone")
        self.assertEqual(account.device_id, f"yamtrack-{self.user.id}")
        self.assertTrue(
            PeriodicTask.objects.filter(task="Import from GPodder (Recurring)").exists(),
        )
        mock_verify_login.assert_called_once()
        mock_delay.assert_called_once_with(user_id=self.user.id, mode="new")

    @patch("integrations.views.gpodder_api.verify_login", side_effect=gpodder_api.GPodderAuthError("bad"))
    def test_connect_failure_shows_error(self, _mock_verify_login):
        response = self.client.post(
            reverse("gpodder_connect"),
            {
                "server_url": "https://gpodder.net",
                "username": "listener",
                "password": "bad",
            },
            follow=True,
        )

        self.assertContains(response, "Invalid GPodder username or password.")
        self.assertFalse(GPodderAccount.objects.exists())

    @patch("integrations.views.tasks.import_gpodder.delay")
    def test_manual_import_creates_schedule_if_missing(self, mock_delay):
        GPodderAccount.objects.create(
            user=self.user,
            server_url=helpers.encrypt("https://gpodder.net"),
            username=helpers.encrypt("listener"),
            password=helpers.encrypt("secret"),
            device_id=f"yamtrack-{self.user.id}",
        )

        response = self.client.post(reverse("import_gpodder"))

        self.assertRedirects(response, reverse("import_data"))
        self.assertTrue(
            PeriodicTask.objects.filter(task="Import from GPodder (Recurring)").exists(),
        )
        mock_delay.assert_called_once_with(user_id=self.user.id, mode="new")

    def test_disconnect_removes_account_and_schedule(self):
        GPodderAccount.objects.create(
            user=self.user,
            server_url=helpers.encrypt("https://gpodder.net"),
            username=helpers.encrypt("listener"),
            password=helpers.encrypt("secret"),
            device_id=f"yamtrack-{self.user.id}",
        )
        crontab = CrontabSchedule.objects.create(
            minute="0",
            hour="*/2",
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
            timezone=timezone.get_default_timezone(),
        )
        PeriodicTask.objects.create(
            name="Import from GPodder for listener (every 2 hours)",
            task="Import from GPodder (Recurring)",
            kwargs=f'{{"user_id": {self.user.id}}}',
            crontab=crontab,
        )

        response = self.client.post(reverse("gpodder_disconnect"))

        self.assertRedirects(response, reverse("import_data"))
        self.assertFalse(GPodderAccount.objects.exists())
        self.assertFalse(PeriodicTask.objects.filter(task="Import from GPodder (Recurring)").exists())

    def test_recurring_task_skips_when_lock_exists(self):
        cache.set("gpodder_import_lock_99", "1", timeout=60)
        try:
            result = tasks.import_gpodder_recurring(99)
        finally:
            cache.delete("gpodder_import_lock_99")
        self.assertEqual(result, "Skipped: import already in progress")
