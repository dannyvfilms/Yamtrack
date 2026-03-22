from datetime import UTC
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from app.models import Music
from integrations import lastfm_sync, tasks
from integrations.lastfm_api import LastFMRecentTracksResult
from integrations.models import LastFMAccount, LastFMHistoryImportStatus


class LastFMViewTests(TestCase):
    """Cover Last.fm connect and manual import views."""

    def setUp(self):
        """Create an authenticated user for Last.fm view requests."""
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="lastfm-user",
        )
        self.client.force_login(self.user)

    @patch("integrations.views.tasks.import_lastfm_history.delay")
    @patch("integrations.views.tasks.poll_lastfm_for_user.delay")
    @patch("integrations.views.lastfm_api.get_recent_tracks")
    def test_lastfm_connect_creates_schedule_and_queues_tasks(
        self,
        mock_get_recent_tracks,
        mock_poll_delay,
        mock_history_delay,
    ):
        """Connecting Last.fm should create the poll and backfill jobs."""
        mock_get_recent_tracks.return_value = {
            "recenttracks": {"track": [], "@attr": {"totalPages": "1"}},
        }

        response = self.client.post(
            reverse("lastfm_connect"),
            {"lastfm_username": "listener"},
        )

        self.assertEqual(response.status_code, 302)
        account = LastFMAccount.objects.get(user=self.user)
        self.assertEqual(account.lastfm_username, "listener")
        self.assertEqual(
            account.history_import_status,
            LastFMHistoryImportStatus.QUEUED,
        )
        self.assertEqual(account.history_import_next_page, 1)
        self.assertIsNone(account.history_import_total_pages)
        self.assertTrue(
            PeriodicTask.objects.filter(
                task="Poll Last.fm for all users",
                enabled=True,
            ).exists(),
        )
        mock_poll_delay.assert_called_once_with(user_id=self.user.id)
        mock_history_delay.assert_called_once_with(user_id=self.user.id, reset=False)

    @patch("integrations.views.tasks.import_lastfm_history.delay")
    @patch("integrations.views.tasks.poll_lastfm_for_user.delay")
    @patch("integrations.views.lastfm_api.get_recent_tracks")
    def test_lastfm_connect_reuses_existing_schedule(
        self,
        mock_get_recent_tracks,
        mock_poll_delay,
        mock_history_delay,
    ):
        """Connecting again should reuse the existing recurring poll."""
        mock_get_recent_tracks.return_value = {
            "recenttracks": {"track": [], "@attr": {"totalPages": "1"}},
        }
        interval = IntervalSchedule.objects.create(
            every=15,
            period=IntervalSchedule.MINUTES,
        )
        PeriodicTask.objects.create(
            name="Poll Last.fm for all users (every 15 minutes)",
            task="Poll Last.fm for all users",
            interval=interval,
            enabled=True,
        )

        self.client.post(reverse("lastfm_connect"), {"lastfm_username": "listener"})

        self.assertEqual(
            PeriodicTask.objects.filter(task="Poll Last.fm for all users").count(),
            1,
        )
        mock_poll_delay.assert_called_once_with(user_id=self.user.id)
        mock_history_delay.assert_called_once_with(user_id=self.user.id, reset=False)

    @patch("integrations.views.tasks.poll_lastfm_for_user.delay")
    def test_poll_lastfm_manual_queues_per_user_task(self, mock_poll_delay):
        """Manual sync should enqueue only the current user's poll task."""
        LastFMAccount.objects.create(
            user=self.user,
            lastfm_username="listener",
            last_fetch_timestamp_uts=1700000000,
        )

        response = self.client.post(reverse("poll_lastfm_manual"))

        self.assertEqual(response.status_code, 302)
        mock_poll_delay.assert_called_once_with(user_id=self.user.id)

    @patch("integrations.views.tasks.import_lastfm_history.delay")
    def test_import_lastfm_history_manual_rejects_active_import(
        self,
        mock_history_delay,
    ):
        """Manual reruns should be blocked while a backfill is active."""
        LastFMAccount.objects.create(
            user=self.user,
            lastfm_username="listener",
            last_fetch_timestamp_uts=1700000000,
            history_import_status=LastFMHistoryImportStatus.QUEUED,
        )

        response = self.client.post(reverse("import_lastfm_history"), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Full Last.fm history import already running.")
        mock_history_delay.assert_not_called()

    @patch("integrations.views.tasks.import_lastfm_history.delay")
    def test_import_lastfm_history_manual_resets_failed_state_and_queues_task(
        self,
        mock_history_delay,
    ):
        """Manual reruns should reset failed history state before queueing."""
        account = LastFMAccount.objects.create(
            user=self.user,
            lastfm_username="listener",
            last_fetch_timestamp_uts=1700000000,
            history_import_status=LastFMHistoryImportStatus.FAILED,
            history_import_next_page=4,
            history_import_total_pages=12,
            history_import_last_error_message="Boom",
        )

        response = self.client.post(reverse("import_lastfm_history"))

        self.assertEqual(response.status_code, 302)
        account.refresh_from_db()
        self.assertEqual(
            account.history_import_status,
            LastFMHistoryImportStatus.QUEUED,
        )
        self.assertEqual(account.history_import_cutoff_uts, 1699999999)
        self.assertEqual(account.history_import_next_page, 1)
        self.assertIsNone(account.history_import_total_pages)
        self.assertEqual(account.history_import_last_error_message, "")
        mock_history_delay.assert_called_once_with(user_id=self.user.id, reset=False)


class LastFMTaskTests(TestCase):
    """Cover Last.fm incremental and history task behavior."""

    def setUp(self):
        """Create a connected Last.fm account for task tests."""
        self.user = get_user_model().objects.create_user(
            username="task-user",
        )
        self.account = LastFMAccount.objects.create(
            user=self.user,
            lastfm_username="listener",
            last_fetch_timestamp_uts=1700000000,
        )
        cache.clear()

    def tearDown(self):
        """Reset cache state between task tests."""
        cache.clear()

    @patch("integrations.lastfm_sync.sync_lastfm_account")
    def test_poll_lastfm_for_user_keeps_cursor_on_interrupted_fetch(self, mock_sync):
        """Interrupted pagination must not advance the incremental cursor."""
        mock_sync.return_value = {
            "tracks": [],
            "pages_fetched": 2,
            "total_pages": 4,
            "complete": False,
            "interrupted": True,
            "max_seen_uts": 1700000500,
            "processed": 2,
            "skipped": 0,
            "errors": 0,
            "affected_day_keys": set(),
        }

        result = tasks.poll_lastfm_for_user(self.user.id)

        self.account.refresh_from_db()
        self.assertEqual(self.account.last_fetch_timestamp_uts, 1700000000)
        self.assertEqual(self.account.last_error_code, "partial")
        self.assertEqual(result["errors"], 1)

    @patch("integrations.tasks.import_lastfm_history.delay")
    @patch("integrations.lastfm_sync.sync_lastfm_account")
    def test_import_lastfm_history_advances_page_and_requeues(
        self,
        mock_sync,
        mock_delay,
    ):
        """Partial history chunks should advance progress and requeue."""
        self.account.reset_history_import(1699999999)
        self.account.save(
            update_fields=[
                "history_import_status",
                "history_import_cutoff_uts",
                "history_import_next_page",
                "history_import_total_pages",
                "history_import_started_at",
                "history_import_completed_at",
                "history_import_last_error_message",
            ],
        )
        mock_sync.return_value = {
            "tracks": [],
            "pages_fetched": 5,
            "total_pages": 12,
            "complete": False,
            "interrupted": False,
            "max_seen_uts": 1699999999,
            "processed": 100,
            "skipped": 0,
            "errors": 0,
            "affected_day_keys": set(),
        }

        result = tasks.import_lastfm_history(self.user.id)

        self.account.refresh_from_db()
        self.assertEqual(
            self.account.history_import_status,
            LastFMHistoryImportStatus.QUEUED,
        )
        self.assertEqual(self.account.history_import_total_pages, 12)
        self.assertEqual(self.account.history_import_next_page, 6)
        self.assertEqual(self.account.last_fetch_timestamp_uts, 1700000000)
        self.assertIn("Continuing with page 6 of 12", result["message"])
        mock_delay.assert_called_once_with(user_id=self.user.id, reset=False)

    @patch("integrations.tasks._enqueue_lastfm_music_enrichment")
    @patch("integrations.lastfm_sync.sync_lastfm_account")
    def test_import_lastfm_history_completes_without_mutating_incremental_cursor(
        self,
        mock_sync,
        mock_enqueue_enrichment,
    ):
        """Finished history imports must leave the incremental cursor unchanged."""
        self.account.reset_history_import(1699999999)
        self.account.save(
            update_fields=[
                "history_import_status",
                "history_import_cutoff_uts",
                "history_import_next_page",
                "history_import_total_pages",
                "history_import_started_at",
                "history_import_completed_at",
                "history_import_last_error_message",
            ],
        )
        mock_sync.return_value = {
            "tracks": [],
            "pages_fetched": 1,
            "total_pages": 1,
            "complete": True,
            "interrupted": False,
            "max_seen_uts": 1699999999,
            "processed": 42,
            "skipped": 0,
            "errors": 0,
            "affected_day_keys": set(),
        }

        result = tasks.import_lastfm_history(self.user.id)

        self.account.refresh_from_db()
        self.assertEqual(
            self.account.history_import_status,
            LastFMHistoryImportStatus.COMPLETED,
        )
        self.assertIsNotNone(self.account.history_import_completed_at)
        self.assertEqual(self.account.last_fetch_timestamp_uts, 1700000000)
        self.assertEqual(self.account.history_import_next_page, 2)
        self.assertEqual(
            result["message"],
            "Completed Last.fm history import. Music enrichment queued.",
        )
        mock_enqueue_enrichment.assert_called_once_with(self.user.id)


class LastFMSyncHelperTests(TestCase):
    """Cover shared Last.fm sync helper behavior."""

    def setUp(self):
        """Create a Last.fm account for sync helper tests."""
        self.user = get_user_model().objects.create_user(
            username="sync-user",
        )
        self.account = LastFMAccount.objects.create(
            user=self.user,
            lastfm_username="listener",
        )

    @patch("integrations.lastfm_api.get_recent_tracks_window")
    def test_sync_lastfm_account_processes_tracks_oldest_first(
        self,
        mock_get_recent_tracks_window,
    ):
        """The sync helper should process scrobbles in chronological order."""
        older_uts = 1700000000
        newer_uts = 1700000600
        track_template = {
            "name": "Song",
            "artist": {"#text": "Artist"},
            "album": {"#text": "Album"},
        }
        mock_get_recent_tracks_window.return_value = LastFMRecentTracksResult(
            tracks=[
                {**track_template, "date": {"uts": str(newer_uts)}},
                {**track_template, "date": {"uts": str(older_uts)}},
            ],
            pages_fetched=1,
            total_pages=1,
            complete=True,
            interrupted=False,
            max_seen_uts=newer_uts,
        )

        lastfm_sync.sync_lastfm_account(
            self.account,
            to_timestamp_uts=newer_uts,
            fast_mode=True,
        )

        music = Music.objects.get(user=self.user)
        self.assertEqual(music.progress, 2)
        self.assertEqual(
            int(music.end_date.astimezone(UTC).timestamp()),
            newer_uts,
        )
