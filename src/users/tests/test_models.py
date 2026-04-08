from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask
from django_celery_results.models import TaskResult

from users.models import (
    HomeSortChoices,
    MediaTypes,
    QuickWatchDateChoices,
)


class UserUpdatePreferenceTests(TestCase):
    """Tests for the User.update_preference method."""

    def setUp(self):
        """Set up test data."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    def test_update_preference_no_new_value(self):
        """Test update_preference when no new value is provided."""
        # Set initial value
        self.user.home_sort = HomeSortChoices.UPCOMING
        self.user.save()

        # Call update_preference with no new value
        result = self.user.update_preference("home_sort", None)

        # Should return current value
        self.assertEqual(result, HomeSortChoices.UPCOMING)
        # Should not change the value
        self.user.refresh_from_db()
        self.assertEqual(self.user.home_sort, HomeSortChoices.UPCOMING)

    def test_update_preference_same_value(self):
        """Test update_preference when the new value is the same as current."""
        # Set initial value
        self.user.home_sort = HomeSortChoices.UPCOMING
        self.user.save()

        # Call update_preference with same value
        result = self.user.update_preference("home_sort", HomeSortChoices.UPCOMING)

        # Should return current value
        self.assertEqual(result, HomeSortChoices.UPCOMING)
        # Should not change the value
        self.user.refresh_from_db()
        self.assertEqual(self.user.home_sort, HomeSortChoices.UPCOMING)

    def test_update_preference_valid_value(self):
        """Test update_preference with a valid new value."""
        # Set initial value
        self.user.home_sort = HomeSortChoices.UPCOMING
        self.user.save()

        # Call update_preference with new valid value
        result = self.user.update_preference("home_sort", HomeSortChoices.TITLE)

        # Should return new value
        self.assertEqual(result, HomeSortChoices.TITLE)
        # Should change the value
        self.user.refresh_from_db()
        self.assertEqual(self.user.home_sort, HomeSortChoices.TITLE)

    def test_update_preference_invalid_value(self):
        """Test update_preference with an invalid new value."""
        # Set initial value
        self.user.home_sort = HomeSortChoices.UPCOMING
        self.user.save()

        # Call update_preference with invalid value
        result = self.user.update_preference("home_sort", "invalid_value")

        # Should return current value
        self.assertEqual(result, HomeSortChoices.UPCOMING)
        # Should not change the value
        self.user.refresh_from_db()
        self.assertEqual(self.user.home_sort, HomeSortChoices.UPCOMING)

    def test_update_preference_boolean_field(self):
        """Test update_preference with a boolean field."""
        # Set initial value
        self.user.tv_enabled = True
        self.user.save()

        # Call update_preference with new value
        result = self.user.update_preference(field_name="tv_enabled", new_value=False)

        # Should return new value
        self.assertEqual(result, False)
        # Should change the value
        self.user.refresh_from_db()
        self.assertEqual(self.user.tv_enabled, False)

    def test_update_preference_last_search_type_valid(self):
        """Test update_preference with last_search_type and valid value."""
        # Set initial value
        self.user.last_search_type = MediaTypes.TV.value
        self.user.save()

        # Call update_preference with new valid value
        result = self.user.update_preference("last_search_type", MediaTypes.MOVIE.value)

        # Should return new value
        self.assertEqual(result, MediaTypes.MOVIE.value)
        # Should change the value
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_search_type, MediaTypes.MOVIE.value)

    def test_update_preference_last_search_type_invalid(self):
        """Test update_preference with last_search_type and invalid value."""
        # Set initial value
        self.user.last_search_type = MediaTypes.TV.value
        self.user.save()

        # Call update_preference with invalid value (SEASON is in EXCLUDED_SEARCH_TYPES)
        result = self.user.update_preference(
            "last_search_type",
            MediaTypes.SEASON.value,
        )

        # Should return current value
        self.assertEqual(result, MediaTypes.TV.value)
        # Should not change the value
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_search_type, MediaTypes.TV.value)

    def test_update_preference_daily_digest_enabled(self):
        """Test update_preference with daily_digest_enabled field."""
        # Set initial value
        self.user.daily_digest_enabled = True
        self.user.save()

        # Call update_preference with new value
        result = self.user.update_preference(
            field_name="daily_digest_enabled",
            new_value=False,
        )

        # Should return new value
        self.assertEqual(result, False)
        # Should change the value
        self.user.refresh_from_db()
        self.assertEqual(self.user.daily_digest_enabled, False)

    def test_update_preference_release_notifications_enabled(self):
        """Test update_preference with release_notifications_enabled field."""
        # Set initial value
        self.user.release_notifications_enabled = True
        self.user.save()

        # Call update_preference with new value
        result = self.user.update_preference(
            field_name="release_notifications_enabled",
            new_value=False,
        )

        # Should return new value
        self.assertEqual(result, False)
        # Should change the value
        self.user.refresh_from_db()
        self.assertEqual(self.user.release_notifications_enabled, False)

    def test_update_preference_top_talent_sort_by_valid(self):
        """Test update_preference with top_talent_sort_by and valid value."""
        self.user.top_talent_sort_by = "plays"
        self.user.save()

        result = self.user.update_preference("top_talent_sort_by", "time")

        self.assertEqual(result, "time")
        self.user.refresh_from_db()
        self.assertEqual(self.user.top_talent_sort_by, "time")

    def test_update_preference_top_talent_sort_by_invalid(self):
        """Test update_preference with top_talent_sort_by and invalid value."""
        self.user.top_talent_sort_by = "plays"
        self.user.save()

        result = self.user.update_preference("top_talent_sort_by", "invalid_mode")

        self.assertEqual(result, "plays")
        self.user.refresh_from_db()
        self.assertEqual(self.user.top_talent_sort_by, "plays")


class UserColumnPrefsTests(TestCase):
    """Tests for per-library table column preferences."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="columnprefs",
            password="12345",
        )

    def test_update_column_prefs_sets_order_and_hidden(self):
        self.user.update_column_prefs(
            media_type=MediaTypes.TV.value,
            table_type="media",
            order=["status", "progress"],
            hidden=["status"],
        )

        self.user.refresh_from_db()
        self.assertEqual(
            self.user.table_column_prefs[MediaTypes.TV.value]["order"],
            ["status", "progress"],
        )
        self.assertEqual(
            self.user.table_column_prefs[MediaTypes.TV.value]["hidden"],
            ["status"],
        )

    def test_update_column_prefs_overwrites_existing_values(self):
        self.user.table_column_prefs = {
            MediaTypes.TV.value: {"order": ["score"], "hidden": ["status"]},
        }
        self.user.save(update_fields=["table_column_prefs"])

        self.user.update_column_prefs(
            media_type=MediaTypes.TV.value,
            table_type="media",
            order=["progress", "score"],
            hidden=[],
        )

        self.user.refresh_from_db()
        self.assertEqual(
            self.user.table_column_prefs[MediaTypes.TV.value],
            {"order": ["progress", "score"], "hidden": []},
        )

    def test_update_column_prefs_scopes_list_prefs_separately(self):
        self.user.table_column_prefs = {
            MediaTypes.TV.value: {"order": ["score"], "hidden": ["status"]},
        }
        self.user.save(update_fields=["table_column_prefs"])

        self.user.update_column_prefs(
            media_type=MediaTypes.TV.value,
            table_type="list",
            order=["media_type", "status"],
            hidden=["status"],
        )

        self.user.refresh_from_db()
        self.assertEqual(
            self.user.table_column_prefs[MediaTypes.TV.value],
            {
                "media": {"order": ["score"], "hidden": ["status"]},
                "list": {
                    "order": ["media_type", "status"],
                    "hidden": ["status"],
                },
            },
        )


class UserGetImportTasksTests(TestCase):
    """Tests for the User.get_import_tasks method."""

    def setUp(self):
        """Set up test data."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.credentials_other = {"username": "otheruser", "password": "12345"}
        self.other_user = get_user_model().objects.create_user(
            **self.credentials_other,
        )

        # Create a crontab schedule for periodic tasks
        self.crontab = CrontabSchedule.objects.create(
            minute="0",
            hour="0",
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
        )

    @patch("users.helpers.process_task_result")
    def test_get_import_tasks_results(self, mock_process_task_result):
        """Test get_import_tasks returns correct task results."""
        # Create mock processed task
        mock_task = MagicMock()
        mock_task.summary = "Imported 10 items"
        mock_task.errors = []
        mock_task.mode = "overwrite"
        mock_process_task_result.return_value = mock_task

        # Create task results for the user
        TaskResult.objects.create(
            task_id="task1",
            task_name="Import from Trakt",
            task_kwargs=(f"{{'user_id': {self.user.id}, 'username': 'testuser'}}"),
            status="SUCCESS",
            date_done=timezone.now() - timedelta(days=1),
            result="{}",
        )

        TaskResult.objects.create(
            task_id="task2",
            task_name="Import from MyAnimeList",
            task_kwargs=(f"{{'user_id': {self.user.id}, 'username': 'testuser'}}"),
            status="FAILURE",
            date_done=timezone.now(),
            result="{}",
        )

        # Create task result for another user (should not be included)
        TaskResult.objects.create(
            task_id="task3",
            task_name="Import from Trakt",
            task_kwargs=(
                f"{{'user_id': {self.other_user.id}, 'username': 'otheruser'}}"
            ),
            status="SUCCESS",
            date_done=timezone.now(),
            result="{}",
        )

        # Get import tasks
        import_tasks = self.user.get_import_tasks()

        # Check results
        self.assertEqual(len(import_tasks["results"]), 2)

        # Check first result (most recent)
        self.assertEqual(import_tasks["results"][1]["task"], mock_task)
        self.assertEqual(import_tasks["results"][1]["source"], "trakt")
        self.assertEqual(import_tasks["results"][1]["status"], "SUCCESS")
        self.assertEqual(import_tasks["results"][1]["summary"], "Imported 10 items")
        self.assertEqual(import_tasks["results"][1]["errors"], [])

        # Check second result
        self.assertEqual(import_tasks["results"][0]["task"], mock_task)
        self.assertEqual(import_tasks["results"][0]["source"], "myanimelist")
        self.assertEqual(import_tasks["results"][0]["status"], "FAILURE")

    @patch("users.helpers.get_next_run_info")
    def test_get_import_tasks_schedules(self, mock_get_next_run_info):
        """Test get_import_tasks returns correct scheduled tasks."""
        # Create mock next run info
        mock_get_next_run_info.return_value = {
            "next_run": timezone.now() + timedelta(days=1),
            "frequency": "Daily at midnight",
            "mode": "overwrite",
        }

        # Create periodic tasks for the user
        periodic_task1 = PeriodicTask.objects.create(
            name="Import from Trakt for testuser at daily",
            task="Import from Trakt",
            kwargs=(f'{{"user_id": {self.user.id}, "username": "testuser"}}'),
            crontab=self.crontab,
            enabled=True,
        )

        periodic_task2 = PeriodicTask.objects.create(
            name="Import from AniList for testuser at weekly",
            task="Import from AniList",
            kwargs=(f'{{"user_id": {self.user.id}, "username": "testuser"}}'),
            crontab=self.crontab,
            enabled=True,
        )

        # Create disabled periodic task (should not be included)
        PeriodicTask.objects.create(
            name="Import from SIMKL for testuser at daily",
            task="Import from SIMKL",
            kwargs=(f'{{"user_id": {self.user.id}, "username": "testuser"}}'),
            crontab=self.crontab,
            enabled=False,
        )

        # Create periodic task for another user (should not be included)
        PeriodicTask.objects.create(
            name="Import from Trakt for otheruser at daily",
            task="Import from Trakt",
            kwargs=(f'{{"user_id": {self.other_user.id}, "username": "testuser"}}'),
            crontab=self.crontab,
            enabled=True,
        )

        # Get import tasks
        import_tasks = self.user.get_import_tasks()

        # Check schedules
        self.assertEqual(len(import_tasks["schedules"]), 2)

        # Check first schedule
        self.assertEqual(import_tasks["schedules"][0]["task"], periodic_task1)
        self.assertEqual(import_tasks["schedules"][0]["source"], "trakt")
        self.assertEqual(import_tasks["schedules"][0]["username"], "testuser")
        self.assertEqual(import_tasks["schedules"][0]["schedule"], "Daily at midnight")

        # Check second schedule
        self.assertEqual(import_tasks["schedules"][1]["task"], periodic_task2)
        self.assertEqual(import_tasks["schedules"][1]["source"], "anilist")
        self.assertEqual(import_tasks["schedules"][1]["username"], "testuser")

    @patch("users.helpers.process_task_result")
    @patch("users.helpers.get_next_run_info")
    def test_get_import_tasks_empty(
        self,
        mock_get_next_run_info,
        _,
    ):
        """Test get_import_tasks when there are no tasks."""
        # Set up mocks
        mock_get_next_run_info.return_value = None

        # Get import tasks
        import_tasks = self.user.get_import_tasks()

        # Check results
        self.assertEqual(len(import_tasks["results"]), 0)
        self.assertEqual(len(import_tasks["schedules"]), 0)

    @patch("users.helpers.process_task_result")
    @patch("users.helpers.get_next_run_info")
    def test_get_import_tasks_watchlist_mapping(
        self,
        mock_get_next_run_info,
        mock_process_task_result,
    ):
        """Watchlist sync results and schedules should map back to the Plex source."""
        processed_task = MagicMock()
        processed_task.summary = "Synced Plex watchlist."
        processed_task.errors = None
        mock_process_task_result.return_value = processed_task
        mock_get_next_run_info.return_value = {
            "next_run": timezone.now() + timedelta(minutes=15),
            "frequency": "Every 15 minutes",
            "mode": "Watchlist Sync",
        }

        TaskResult.objects.create(
            task_id="task-watchlist",
            task_name="Sync Plex Watchlist",
            task_kwargs=(f"{{'user_id': {self.user.id}, 'mode': 'watchlist'}}"),
            status="SUCCESS",
            date_done=timezone.now(),
            result="{}",
        )

        interval = IntervalSchedule.objects.create(
            every=15,
            period=IntervalSchedule.MINUTES,
        )
        periodic_task = PeriodicTask.objects.create(
            name="Sync Plex Watchlist for test (every 15 minutes)",
            task="Sync Plex Watchlist",
            kwargs=(f'{{"user_id": {self.user.id}, "mode": "watchlist"}}'),
            interval=interval,
            enabled=True,
        )

        import_tasks = self.user.get_import_tasks()

        self.assertEqual(len(import_tasks["results"]), 1)
        self.assertEqual(import_tasks["results"][0]["source"], "plex")
        self.assertEqual(len(import_tasks["schedules"]), 1)
        self.assertEqual(import_tasks["schedules"][0]["task"], periodic_task)
        self.assertEqual(import_tasks["schedules"][0]["source"], "plex")
        self.assertEqual(import_tasks["schedules"][0]["mode"], "Watchlist Sync")

    @patch("users.helpers.process_task_result")
    def test_get_import_tasks_unknown_source(self, mock_process_task_result):
        """Test get_import_tasks with an unknown task source."""
        # Create mock processed task
        mock_task = MagicMock()
        mock_task.summary = "Imported 10 items"
        mock_task.errors = []
        mock_task.mode = "overwrite"
        mock_process_task_result.return_value = mock_task

        # Create task result with unknown source
        TaskResult.objects.create(
            task_id="task1",
            task_name="Import from Unknown",
            task_kwargs=(f"{{'user_id': {self.user.id}, 'username': 'testuser'}}"),
            status="SUCCESS",
            date_done=timezone.now(),
            result="{}",
        )

        # Get import tasks
        import_tasks = self.user.get_import_tasks()

        # Check results
        self.assertEqual(len(import_tasks["results"]), 0)

    @patch("users.helpers.process_task_result")
    def test_get_import_tasks_uses_direct_audiobookshelf_results(
        self,
        mock_process_task_result,
    ):
        """Recurring wrapper results should not replace real Audiobookshelf imports."""
        mock_task = MagicMock()
        mock_task.summary = "Imported 1 book"
        mock_task.errors = None
        mock_process_task_result.return_value = mock_task

        TaskResult.objects.create(
            task_id="task-direct",
            task_name="Import from Audiobookshelf",
            task_kwargs=(f'{{"user_id": {self.user.id}}}'),
            status="SUCCESS",
            date_done=timezone.now(),
            result='"Imported 1 book"',
        )
        TaskResult.objects.create(
            task_id="task-recurring",
            task_name="Import from Audiobookshelf (Recurring)",
            task_kwargs=(f'{{"user_id": {self.user.id}}}'),
            status="SUCCESS",
            date_done=timezone.now() - timedelta(minutes=1),
            result='["child-task-id", null]',
        )

        import_tasks = self.user.get_import_tasks()

        self.assertEqual(len(import_tasks["results"]), 1)
        self.assertEqual(import_tasks["results"][0]["source"], "audiobookshelf")
        self.assertEqual(import_tasks["results"][0]["summary"], "Imported 1 book")
        mock_process_task_result.assert_called_once()

    @patch("users.helpers.process_task_result")
    def test_get_import_tasks_maps_lastfm_history_results(self, mock_process_task_result):
        """Last.fm history task results should appear under the Last.fm source."""
        mock_task = MagicMock()
        mock_task.summary = "Imported 42 Last.fm history scrobbles."
        mock_task.errors = None
        mock_process_task_result.return_value = mock_task

        TaskResult.objects.create(
            task_id="task-lastfm-history",
            task_name="Import from Last.fm History",
            task_kwargs=(f'{{"user_id": {self.user.id}}}'),
            status="SUCCESS",
            date_done=timezone.now(),
            result='"Imported 42 Last.fm history scrobbles."',
        )

        import_tasks = self.user.get_import_tasks()

        self.assertEqual(len(import_tasks["results"]), 1)
        self.assertEqual(import_tasks["results"][0]["source"], "lastfm")
        self.assertEqual(
            import_tasks["results"][0]["summary"],
            "Imported 42 Last.fm history scrobbles.",
        )
        mock_process_task_result.assert_called_once()

    @patch("users.models.AsyncResult")
    @patch("users.helpers.process_task_result")
    def test_get_import_tasks_reconciles_stale_pending_result(
        self,
        mock_process_task_result,
        mock_async_result,
    ):
        """Pending DB rows should reflect terminal Celery backend states."""
        processed_task = MagicMock()
        processed_task.summary = "Imported 3 movies."
        processed_task.errors = None
        mock_process_task_result.return_value = processed_task

        backend_result = MagicMock()
        backend_result.status = "SUCCESS"
        backend_result.result = "Imported 3 movies."
        mock_async_result.return_value = backend_result

        task_result = TaskResult.objects.create(
            task_id="task-pending",
            task_name="Import from Trakt",
            task_kwargs=(f'{{"user_id": {self.user.id}}}'),
            status="PENDING",
            result=None,
        )

        import_tasks = self.user.get_import_tasks()
        self.assertEqual(len(import_tasks["results"]), 1)
        self.assertEqual(import_tasks["results"][0]["status"], "SUCCESS")

        task_result.refresh_from_db()
        self.assertEqual(task_result.status, "SUCCESS")
        self.assertEqual(task_result.result, "Imported 3 movies.")


class UserResolveWatchDateTests(TestCase):
    """Tests for the User.resolve_watch_date method."""

    def setUp(self):
        """Set up test data."""
        self.QuickWatchDateChoices = QuickWatchDateChoices
        self.credentials = {"username": "test_watch", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.now = timezone.now()
        self.release_date = datetime(2020, 5, 15, 20, 0, tzinfo=UTC)

    def test_resolve_watch_date_current_date(self):
        """Test resolve_watch_date returns current date for CURRENT_DATE."""
        self.user.quick_watch_date = self.QuickWatchDateChoices.CURRENT_DATE
        self.user.save()

        result = self.user.resolve_watch_date(self.now, self.release_date)

        self.assertEqual(result, self.now)

    def test_resolve_watch_date_release_date(self):
        """Test resolve_watch_date returns release date for RELEASE_DATE."""
        self.user.quick_watch_date = self.QuickWatchDateChoices.RELEASE_DATE
        self.user.save()

        result = self.user.resolve_watch_date(self.now, self.release_date)

        self.assertEqual(result, self.release_date)

    def test_resolve_watch_date_release_date_none(self):
        """Test resolve_watch_date returns None when release_date is None."""
        self.user.quick_watch_date = self.QuickWatchDateChoices.RELEASE_DATE
        self.user.save()

        result = self.user.resolve_watch_date(self.now, None)

        self.assertIsNone(result)

    def test_resolve_watch_date_no_date(self):
        """Test resolve_watch_date returns None when preference is NO_DATE."""
        self.user.quick_watch_date = self.QuickWatchDateChoices.NO_DATE
        self.user.save()

        result = self.user.resolve_watch_date(self.now, self.release_date)

        self.assertIsNone(result)

    def test_resolve_watch_date_default_is_current_date(self):
        """Test that default preference is CURRENT_DATE."""
        self.assertEqual(
            self.user.quick_watch_date,
            self.QuickWatchDateChoices.CURRENT_DATE,
        )

        result = self.user.resolve_watch_date(self.now, self.release_date)

        self.assertEqual(result, self.now)
