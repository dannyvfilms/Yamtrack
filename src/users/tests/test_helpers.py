import json
import zoneinfo
from datetime import datetime
from unittest.mock import Mock, patch

from django.test import TestCase
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask

from users import helpers


class HelpersTest(TestCase):
    """Test helper functions."""

    def test_process_task_result_failure_media_import_error(self):
        """Test processing a failed task with MediaImportError."""
        task = Mock()
        task.status = "FAILURE"
        task.result = json.dumps(
            {
                "exc_type": "MediaImportError",
                "exc_message": ["Test error message"],
            },
        )
        task.traceback = "Traceback info"

        processed_task = helpers.process_task_result(task)

        self.assertEqual(processed_task.summary, "Test error message")
        self.assertEqual(processed_task.errors, "Traceback info")

    def test_process_task_result_failure_unexpected_error(self):
        """Test processing a failed task with unexpected error."""
        task = Mock()
        task.status = "FAILURE"
        task.result = json.dumps(
            {
                "exc_type": "OtherError",
                "exc_message": ["Other error"],
            },
        )
        task.traceback = "Traceback info"

        processed_task = helpers.process_task_result(task)

        self.assertEqual(
            processed_task.summary,
            "Unexpected error occurred while processing the task.",
        )
        self.assertEqual(processed_task.errors, "Traceback info")

    def test_process_task_result_success_with_errors(self):
        """Test processing a successful task with errors."""
        task = Mock()
        task.status = "SUCCESS"
        error_title = "ERRORS:\n"  # Assuming this is ERROR_TITLE
        task.result = json.dumps(f"Summary text{error_title}Error details")
        task.traceback = None

        with patch("integrations.tasks.ERROR_TITLE", "ERRORS:\n"):
            processed_task = helpers.process_task_result(task)

        self.assertEqual(processed_task.summary, "Summary text")
        self.assertEqual(processed_task.errors, "Error details")

    def test_process_task_result_success_no_errors(self):
        """Test processing a successful task without errors."""
        task = Mock()
        task.status = "SUCCESS"
        task.result = json.dumps("Summary text only")
        task.traceback = None

        processed_task = helpers.process_task_result(task)

        self.assertEqual(processed_task.summary, "Summary text only")
        self.assertIsNone(processed_task.errors)

    def test_process_task_result_success_with_structured_list_payload(self):
        """Structured list payloads should not crash import history rendering."""
        task = Mock()
        task.status = "SUCCESS"
        task.result = json.dumps(["child-task-id", None])
        task.traceback = None

        processed_task = helpers.process_task_result(task)

        self.assertEqual(processed_task.summary, "Queued follow-up import task.")
        self.assertIsNone(processed_task.errors)

    def test_process_task_result_success_with_structured_dict_payload(self):
        """Structured dict payloads should be summarized for the UI."""
        task = Mock()
        task.status = "SUCCESS"
        task.result = json.dumps(
            {
                "processed": 2,
                "errors": 1,
                "total_accounts": 3,
            },
        )
        task.traceback = None

        processed_task = helpers.process_task_result(task)

        self.assertEqual(processed_task.summary, "Processed 2 of 3 account(s).")
        self.assertEqual(processed_task.errors, "1 account(s) reported errors.")

    def test_process_task_result_started(self):
        """Test processing a task that's currently running."""
        task = Mock()
        task.status = "STARTED"
        task.result = None
        task.traceback = None

        processed_task = helpers.process_task_result(task)

        self.assertEqual(processed_task.summary, "This task is currently running.")
        self.assertIsNone(processed_task.errors)

    def test_process_task_result_pending(self):
        """Test processing a pending task."""
        task = Mock()
        task.status = "PENDING"
        task.result = None
        task.traceback = None

        processed_task = helpers.process_task_result(task)

        self.assertEqual(
            processed_task.summary,
            "This task has been queued and is waiting to run.",
        )
        self.assertIsNone(processed_task.errors)

    @patch("django.utils.timezone.now")
    def test_get_next_run_info_daily(self, mock_now):
        """Test getting next run info for daily task."""
        # Set up mock current time
        current_time = datetime(2025, 2, 6, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        mock_now.return_value = current_time

        crontab = CrontabSchedule.objects.create(
            minute="0",
            hour="14",
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
            timezone="UTC",
        )
        periodic_task = PeriodicTask.objects.create(
            name="Daily Import",
            task="import_task",
            crontab=crontab,
        )

        next_run_info = helpers.get_next_run_info(periodic_task)

        expected_next_run = datetime(2025, 2, 6, 14, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(next_run_info["next_run"], expected_next_run)
        self.assertEqual(next_run_info["frequency"], "Every Day")

    @patch("django.utils.timezone.now")
    def test_get_next_run_info_every_2_days(self, mock_now):
        """Test getting next run info for every 2 days task."""
        # Thursday, so next run should be same day at 14:00
        current_time = datetime(2025, 2, 6, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        mock_now.return_value = current_time

        crontab = CrontabSchedule.objects.create(
            minute="0",
            hour="14",
            day_of_week="*/2",
            day_of_month="*",
            month_of_year="*",
            timezone="UTC",
        )
        periodic_task = PeriodicTask.objects.create(
            name="Every 2 Days Import",
            task="import_task",
            crontab=crontab,
        )

        next_run_info = helpers.get_next_run_info(periodic_task)

        # Since we're testing on Thursday (day 4), and it's before 14:00,
        # the next run should be the same day at 14:00
        expected_next_run = datetime(2025, 2, 6, 14, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(next_run_info["next_run"], expected_next_run)
        self.assertEqual(next_run_info["frequency"], "Every 2 days")

    @patch("django.utils.timezone.now")
    def test_get_next_run_info_every_2_days_after_todays_run(self, mock_now):
        """Test getting next run info for every 2 days."""
        # Thursday after scheduled time, so next run should be Saturday
        current_time = datetime(2025, 2, 6, 15, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        mock_now.return_value = current_time

        crontab = CrontabSchedule.objects.create(
            minute="0",
            hour="14",
            day_of_week="*/2",
            day_of_month="*",
            month_of_year="*",
            timezone="UTC",
        )
        periodic_task = PeriodicTask.objects.create(
            name="Every 2 Days Import",
            task="import_task",
            crontab=crontab,
        )

        next_run_info = helpers.get_next_run_info(periodic_task)

        # Since we're testing on Thursday after 14:00,
        # the next run should be Saturday at 14:00
        expected_next_run = datetime(2025, 2, 8, 14, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(next_run_info["next_run"], expected_next_run)
        self.assertEqual(next_run_info["frequency"], "Every 2 days")

    def test_get_next_run_info_custom_cron(self):
        """Test getting next run info for custom cron schedule."""
        crontab = CrontabSchedule.objects.create(
            minute="30",
            hour="*/4",
            day_of_week="1,3,5",
            day_of_month="*",
            month_of_year="*",
            timezone="UTC",
        )
        periodic_task = PeriodicTask.objects.create(
            name="Custom Import",
            task="import_task",
            crontab=crontab,
        )

        next_run_info = helpers.get_next_run_info(periodic_task)

        self.assertEqual(next_run_info["frequency"], "Cron: 30 */4 * * 1,3,5")

    def test_get_next_run_info_no_crontab(self):
        """Test getting next run info for task without crontab."""
        periodic_task = Mock()
        periodic_task.crontab = None
        periodic_task.interval = None

        next_run_info = helpers.get_next_run_info(periodic_task)
        self.assertIsNone(next_run_info)

    @patch("django.utils.timezone.now")
    def test_get_next_run_info_interval_watchlist(self, mock_now):
        """Test getting next run info for interval-based watchlist sync."""
        current_time = datetime(2025, 2, 6, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC"))
        mock_now.return_value = current_time

        interval = IntervalSchedule.objects.create(
            every=15,
            period=IntervalSchedule.MINUTES,
        )
        periodic_task = PeriodicTask.objects.create(
            name="Sync Plex Watchlist for test (every 15 minutes)",
            task="Sync Plex Watchlist",
            interval=interval,
            kwargs='{"user_id": 1, "mode": "watchlist"}',
            start_time=current_time,
        )

        next_run_info = helpers.get_next_run_info(periodic_task)

        expected_next_run = datetime(2025, 2, 6, 12, 15, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(next_run_info["next_run"], expected_next_run)
        self.assertEqual(next_run_info["frequency"], "Every 15 minutes")
        self.assertEqual(next_run_info["mode"], "Watchlist Sync")

    @patch("django.utils.timezone.now")
    def test_get_next_run_info_interval_rolls_forward_from_past_run(self, mock_now):
        """Test interval next-run calculation stays in the future."""
        current_time = datetime(2025, 2, 6, 12, 44, tzinfo=zoneinfo.ZoneInfo("UTC"))
        mock_now.return_value = current_time

        interval = IntervalSchedule.objects.create(
            every=15,
            period=IntervalSchedule.MINUTES,
        )
        periodic_task = PeriodicTask.objects.create(
            name="Sync Plex Watchlist for test (every 15 minutes)",
            task="Sync Plex Watchlist",
            interval=interval,
            kwargs='{"user_id": 1, "mode": "watchlist"}',
            start_time=datetime(2025, 2, 6, 11, 0, tzinfo=zoneinfo.ZoneInfo("UTC")),
            last_run_at=datetime(2025, 2, 6, 12, 0, tzinfo=zoneinfo.ZoneInfo("UTC")),
        )

        next_run_info = helpers.get_next_run_info(periodic_task)

        expected_next_run = datetime(2025, 2, 6, 12, 45, tzinfo=zoneinfo.ZoneInfo("UTC"))
        self.assertEqual(next_run_info["next_run"], expected_next_run)
