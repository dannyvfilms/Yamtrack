from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from app.models import Item, MediaTypes, Movie, Sources, Status, TV
from integrations import plex as plex_api
from integrations.models import PlexAccount, PlexWatchlistSyncItem
from integrations.plex_watchlist import (
    PlexWatchlistSyncService,
    WATCHLIST_SYNC_INTERVAL_MINUTES,
    WATCHLIST_TASK_NAME,
)


class PlexWatchlistProviderTests(TestCase):
    @patch("integrations.plex.requests.get")
    def test_fetch_watchlist_parses_metadata_payload(self, mock_get):
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json"}
        response.json.return_value = {
            "MediaContainer": {
                "Metadata": [{"ratingKey": "1", "type": "movie", "title": "Movie"}],
                "totalSize": 1,
            },
        }
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        entries, total = plex_api.fetch_watchlist("token")

        self.assertEqual(total, 1)
        self.assertEqual(entries[0]["title"], "Movie")

    @patch("integrations.plex.requests.get")
    def test_fetch_watchlist_parses_hub_payload(self, mock_get):
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json"}
        response.json.return_value = {
            "MediaContainer": {
                "Hub": [
                    {
                        "Metadata": [
                            {"ratingKey": "2", "type": "show", "title": "Show"},
                        ],
                    },
                ],
            },
        }
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        entries, total = plex_api.fetch_watchlist("token")

        self.assertEqual(total, 1)
        self.assertEqual(entries[0]["title"], "Show")

    @patch("integrations.plex.requests.get")
    def test_fetch_watchlist_metadata_parses_metadata_payload(self, mock_get):
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": "application/json"}
        response.json.return_value = {
            "MediaContainer": {
                "Metadata": [
                    {
                        "ratingKey": "1",
                        "type": "movie",
                        "title": "Movie",
                        "Guid": [{"id": "tmdb://123"}],
                    },
                ],
            },
        }
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        metadata = plex_api.fetch_watchlist_metadata("token", "1")

        self.assertEqual(metadata["title"], "Movie")
        self.assertEqual(metadata["Guid"][0]["id"], "tmdb://123")

    @patch("integrations.plex.requests.get")
    def test_fetch_watchlist_metadata_wraps_404_as_client_error(self, mock_get):
        response = Mock()
        response.status_code = 404
        response.raise_for_status.side_effect = requests.HTTPError("404 Client Error")
        mock_get.return_value = response

        with self.assertRaisesMessage(plex_api.PlexClientError, "status 404"):
            plex_api.fetch_watchlist_metadata("token", "missing")

    def test_extract_external_ids_from_guids_includes_plex_guid(self):
        ids = plex_api.extract_external_ids_from_guids(
            [
                {"id": "plex://movie/abc123"},
                {"id": "imdb://tt1234567"},
                {"id": "tmdb://456"},
                {"id": "tvdb://789"},
            ],
        )

        self.assertEqual(
            ids,
            {
                "plex_guid": "movie/abc123",
                "imdb_id": "tt1234567",
                "tmdb_id": "456",
                "tvdb_id": "789",
            },
        )


class PlexWatchlistSyncServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="watcher", password="pw")
        self.account = PlexAccount.objects.create(
            user=self.user,
            plex_token="token",
            plex_username="watcher",
            plex_account_id="acct-1",
        )

    def _movie_entry(self, guid="tmdb://123"):
        return {
            "ratingKey": "rk-movie",
            "type": "movie",
            "title": "Movie Title",
            "Guid": [{"id": guid}],
        }

    def _show_entry(self, guid="tmdb://321"):
        return {
            "ratingKey": "rk-show",
            "type": "show",
            "title": "Show Title",
            "Guid": [{"id": guid}],
        }

    def _metadata_side_effect(self, media_type, media_id, source):  # noqa: ARG002
        if media_type == MediaTypes.MOVIE.value:
            return {
                "title": "Movie Title",
                "image": "https://example.com/movie.jpg",
                "max_progress": 1,
            }
        return {
            "title": "Show Title",
            "image": "https://example.com/show.jpg",
            "max_progress": 1,
        }

    @patch("integrations.plex_watchlist.services.get_media_metadata")
    @patch("integrations.plex_watchlist.plex_api.fetch_watchlist")
    def test_first_sync_creates_planning_media_and_ledger(self, mock_fetch_watchlist, mock_metadata):
        mock_fetch_watchlist.return_value = (
            [self._movie_entry(), self._show_entry()],
            2,
        )
        mock_metadata.side_effect = self._metadata_side_effect

        counts, warnings = PlexWatchlistSyncService(self.user, self.account).sync()

        self.assertEqual(warnings, "")
        self.assertEqual(counts["created"], 2)
        self.assertEqual(counts[MediaTypes.MOVIE.value], 1)
        self.assertEqual(counts[MediaTypes.TV.value], 1)
        self.assertEqual(Movie.objects.get(user=self.user).status, Status.PLANNING.value)
        self.assertEqual(TV.objects.get(user=self.user).status, Status.PLANNING.value)
        self.assertEqual(PlexWatchlistSyncItem.objects.count(), 2)
        self.assertTrue(
            PlexWatchlistSyncItem.objects.filter(user=self.user, created_by_sync=True, is_active=True).count(),
        )

    @patch("integrations.plex_watchlist.services.get_media_metadata")
    @patch("integrations.plex_watchlist.plex_api.fetch_watchlist")
    def test_repeat_sync_is_idempotent(self, mock_fetch_watchlist, mock_metadata):
        mock_fetch_watchlist.return_value = ([self._movie_entry(), self._show_entry()], 2)
        mock_metadata.side_effect = self._metadata_side_effect

        service = PlexWatchlistSyncService(self.user, self.account)
        service.sync()
        counts, _ = service.sync()

        self.assertEqual(counts.get("created", 0), 0)
        self.assertEqual(counts.get("linked_existing", 0), 2)
        self.assertEqual(Movie.objects.filter(user=self.user).count(), 1)
        self.assertEqual(TV.objects.filter(user=self.user).count(), 1)
        self.assertEqual(PlexWatchlistSyncItem.objects.count(), 2)

    @patch("integrations.plex_watchlist.services.get_media_metadata")
    @patch("integrations.plex_watchlist.plex_api.fetch_watchlist")
    def test_existing_media_is_linked_without_status_overwrite(self, mock_fetch_watchlist, mock_metadata):
        mock_metadata.side_effect = self._metadata_side_effect
        item = Item.objects.create(
            media_id="123",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Existing Movie",
        )
        Movie.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
        )
        mock_fetch_watchlist.return_value = ([self._movie_entry()], 1)

        counts, _ = PlexWatchlistSyncService(self.user, self.account).sync()

        movie = Movie.objects.get(user=self.user, item=item)
        sync_item = PlexWatchlistSyncItem.objects.get(user=self.user, item=item)
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(counts.get("created", 0), 0)
        self.assertEqual(counts.get("linked_existing", 0), 1)
        self.assertFalse(sync_item.created_by_sync)
        self.assertTrue(sync_item.is_active)

    @patch("integrations.plex_watchlist.services.get_media_metadata")
    @patch("integrations.plex_watchlist.plex_api.fetch_watchlist")
    def test_removal_deletes_pristine_synced_media(self, mock_fetch_watchlist, mock_metadata):
        mock_fetch_watchlist.side_effect = [
            ([self._movie_entry()], 1),
            ([], 0),
        ]
        mock_metadata.side_effect = self._metadata_side_effect

        PlexWatchlistSyncService(self.user, self.account).sync()
        counts, _ = PlexWatchlistSyncService(self.user, self.account).sync()

        self.assertEqual(Movie.objects.filter(user=self.user).count(), 0)
        sync_item = PlexWatchlistSyncItem.objects.get(user=self.user)
        self.assertFalse(sync_item.is_active)
        self.assertIsNotNone(sync_item.removed_at)
        self.assertEqual(counts.get("removed", 0), 1)
        self.assertEqual(counts.get("deactivated", 0), 0)

    @patch("integrations.plex_watchlist.services.get_media_metadata")
    @patch("integrations.plex_watchlist.plex_api.fetch_watchlist")
    def test_removal_preserves_modified_media_and_deactivates_link(self, mock_fetch_watchlist, mock_metadata):
        mock_fetch_watchlist.side_effect = [
            ([self._movie_entry()], 1),
            ([], 0),
        ]
        mock_metadata.side_effect = self._metadata_side_effect

        PlexWatchlistSyncService(self.user, self.account).sync()
        movie = Movie.objects.get(user=self.user)
        movie.notes = "Keep this one"
        movie.save(update_fields=["notes"])

        counts, _ = PlexWatchlistSyncService(self.user, self.account).sync()

        self.assertEqual(Movie.objects.filter(user=self.user).count(), 1)
        sync_item = PlexWatchlistSyncItem.objects.get(user=self.user)
        self.assertFalse(sync_item.is_active)
        self.assertEqual(counts.get("removed", 0), 0)
        self.assertEqual(counts.get("deactivated", 0), 1)

    @patch("integrations.plex_watchlist.tmdb.find")
    @patch("integrations.plex_watchlist.services.get_media_metadata")
    @patch("integrations.plex_watchlist.plex_api.fetch_watchlist")
    def test_sync_resolves_tmdb_id_via_tvdb_lookup(
        self,
        mock_fetch_watchlist,
        mock_metadata,
        mock_tmdb_find,
    ):
        mock_fetch_watchlist.return_value = ([self._show_entry(guid="tvdb://456")], 1)
        mock_tmdb_find.return_value = {"tv_results": [{"id": 789}]}
        mock_metadata.side_effect = self._metadata_side_effect

        counts, _ = PlexWatchlistSyncService(self.user, self.account).sync()

        tv = TV.objects.get(user=self.user)
        self.assertEqual(tv.item.media_id, "789")
        self.assertEqual(counts["created"], 1)

    @patch("integrations.plex_watchlist.services.get_media_metadata")
    @patch("integrations.plex_watchlist.plex_api.fetch_watchlist_metadata")
    @patch("integrations.plex_watchlist.plex_api.fetch_watchlist")
    def test_sync_fetches_detail_metadata_when_list_payload_lacks_external_ids(
        self,
        mock_fetch_watchlist,
        mock_fetch_watchlist_metadata,
        mock_metadata,
    ):
        mock_fetch_watchlist.return_value = (
            [
                {
                    "ratingKey": "rk-movie",
                    "type": "movie",
                    "title": "Movie Title",
                    "guid": "plex://movie/abc123",
                },
            ],
            1,
        )
        mock_fetch_watchlist_metadata.return_value = {
            "ratingKey": "rk-movie",
            "type": "movie",
            "title": "Movie Title",
            "Guid": [{"id": "tmdb://123"}],
        }
        mock_metadata.side_effect = self._metadata_side_effect

        counts, warnings = PlexWatchlistSyncService(self.user, self.account).sync()

        self.assertEqual(warnings, "")
        self.assertEqual(counts["created"], 1)
        self.assertEqual(Movie.objects.get(user=self.user).item.media_id, "123")
        mock_fetch_watchlist_metadata.assert_called_once_with(self.account.plex_token, "rk-movie")

    @patch("integrations.plex_watchlist.plex_api.fetch_watchlist_metadata")
    @patch("integrations.plex_watchlist.plex_api.fetch_watchlist")
    def test_sync_warns_and_skips_when_detail_metadata_returns_404(
        self,
        mock_fetch_watchlist,
        mock_fetch_watchlist_metadata,
    ):
        mock_fetch_watchlist.return_value = (
            [
                {
                    "ratingKey": "rk-movie",
                    "type": "movie",
                    "title": "Movie Title",
                    "guid": "plex://movie/abc123",
                },
            ],
            1,
        )
        mock_fetch_watchlist_metadata.side_effect = plex_api.PlexClientError(
            "Plex request failed with status 404",
        )

        counts, warnings = PlexWatchlistSyncService(self.user, self.account).sync()

        self.assertEqual(counts["skipped_missing_ids"], 1)
        self.assertIn("Could not load Plex watchlist metadata for Movie Title", warnings)
        self.assertIn("Skipped Plex watchlist entry without resolvable IDs: Movie Title.", warnings)
        self.assertFalse(Movie.objects.filter(user=self.user).exists())


class PlexWatchlistViewTests(TestCase):
    def setUp(self):
        self.credentials = {"username": "watchview", "password": "pw"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        self.account = PlexAccount.objects.create(
            user=self.user,
            plex_token="token",
            plex_username="watchview",
            plex_account_id="acct-1",
            watchlist_sync_enabled=False,
            sections_refreshed_at=timezone.now(),
        )

    @patch("integrations.views.tasks.sync_plex_watchlist.delay")
    def test_import_plex_watchlist_mode_creates_interval_schedule(self, mock_delay):
        before_request = timezone.now()
        response = self.client.post(
            reverse("import_plex"),
            {
                "mode": "watchlist",
                "frequency": "daily",
                "time": "04:30",
                "plex_usernames": "watchview, WatchView",
            },
        )

        self.assertRedirects(response, reverse("import_data"))
        mock_delay.assert_called_once_with(user_id=self.user.id, mode="watchlist")

        self.user.refresh_from_db()
        self.assertEqual(self.user.plex_usernames, "watchview")
        self.assertTrue(self.user.plex_account.watchlist_sync_enabled)

        task = PeriodicTask.objects.get(task=WATCHLIST_TASK_NAME)
        self.assertIsNotNone(task.interval)
        self.assertEqual(task.interval.every, WATCHLIST_SYNC_INTERVAL_MINUTES)
        self.assertEqual(task.interval.period, IntervalSchedule.MINUTES)
        self.assertIn('"mode": "watchlist"', task.kwargs)
        self.assertGreaterEqual(
            task.start_time,
            before_request + timedelta(minutes=WATCHLIST_SYNC_INTERVAL_MINUTES) - timedelta(seconds=5),
        )

    def test_disable_watchlist_sync_removes_schedule(self):
        interval = IntervalSchedule.objects.create(
            every=WATCHLIST_SYNC_INTERVAL_MINUTES,
            period=IntervalSchedule.MINUTES,
        )
        PeriodicTask.objects.create(
            name="Sync Plex Watchlist for watchview (every 15 minutes)",
            task=WATCHLIST_TASK_NAME,
            interval=interval,
            kwargs=f'{{"user_id": {self.user.id}, "mode": "watchlist"}}',
            enabled=True,
        )
        self.account.watchlist_sync_enabled = True
        self.account.save(update_fields=["watchlist_sync_enabled"])

        response = self.client.post(reverse("plex_disable_watchlist"))

        self.assertRedirects(response, reverse("import_data"))
        self.account.refresh_from_db()
        self.assertFalse(self.account.watchlist_sync_enabled)
        self.assertFalse(PeriodicTask.objects.filter(task=WATCHLIST_TASK_NAME).exists())

    @patch("users.views.plex.fetch_account")
    def test_import_page_shows_watchlist_mode_ui(self, mock_fetch_account):
        mock_fetch_account.return_value = {"username": "watchview", "id": "acct-1"}
        interval = IntervalSchedule.objects.create(
            every=WATCHLIST_SYNC_INTERVAL_MINUTES,
            period=IntervalSchedule.MINUTES,
        )
        PeriodicTask.objects.create(
            name="Sync Plex Watchlist for watchview (every 15 minutes)",
            task=WATCHLIST_TASK_NAME,
            interval=interval,
            kwargs=f'{{"user_id": {self.user.id}, "mode": "watchlist"}}',
            enabled=True,
        )
        self.account.watchlist_sync_enabled = True
        self.account.watchlist_last_synced_at = timezone.now()
        self.account.save(
            update_fields=["watchlist_sync_enabled", "watchlist_last_synced_at"],
        )

        response = self.client.get(reverse("import_data"))

        self.assertContains(response, "Import Watchlist Data Only")
        self.assertContains(response, "Disable Watchlist Sync")
        self.assertContains(response, "Every 15 minutes")
        self.assertContains(response, "Watchlist Sync")

    @patch("users.views.plex.fetch_account")
    def test_import_page_hides_disable_action_when_watchlist_sync_disabled(self, mock_fetch_account):
        mock_fetch_account.return_value = {"username": "watchview", "id": "acct-1"}

        response = self.client.get(reverse("import_data"))

        self.assertContains(response, "Recurring sync:")
        self.assertNotContains(response, "Disable Watchlist Sync")
