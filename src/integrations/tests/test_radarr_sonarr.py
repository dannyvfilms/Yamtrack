from datetime import UTC, datetime
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.db.utils import OperationalError
from django.test import TestCase
from django.urls import reverse
from django_celery_beat.models import PeriodicTask

from app.helpers import get_tv_show_collection_stats
from app.models import CollectionEntry, Item, MediaTypes, Sources
from integrations import tasks
from integrations.imports import helpers, radarr, sonarr
from integrations.models import CollectionSourceState, RadarrAccount, SonarrAccount
from integrations.source_sync import upsert_collection_source_state


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

    @patch("integrations.imports.sonarr.services.get_media_metadata")
    @patch("integrations.imports.sonarr.SonarrClient.episodes")
    @patch("integrations.imports.sonarr.SonarrClient.series")
    def test_sonarr_import_syncs_episode_collection_and_clears_legacy_show_state(
        self,
        mock_series,
        mock_episodes,
        mock_get_media_metadata,
    ):
        """Sonarr should replace the old show-level placeholder with episode rows."""
        show_item = Item.objects.create(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Severance",
            image="https://example.com/severance.jpg",
        )
        CollectionEntry.objects.create(
            user=self.user,
            item=show_item,
            media_type="digital",
            audio_codec="AAC",
            audio_channels="5.1",
            bitrate=1653,
        )
        CollectionSourceState.objects.create(
            user=self.user,
            item=show_item,
            source="sonarr",
        )

        mock_series.return_value = [
            {
                "id": 77,
                "title": "Severance",
                "statistics": {"episodeFileCount": 1},
                "tmdbId": 95396,
                "seasons": [{"seasonNumber": 1}],
            },
        ]
        mock_episodes.return_value = [
            {
                "seasonNumber": 1,
                "episodeNumber": 1,
                "title": "Good News About Hell",
                "airDateUtc": "2022-02-18T00:00:00Z",
                "episodeFileId": 11,
            },
            {
                "seasonNumber": 1,
                "episodeNumber": 2,
                "title": "Half Loop",
                "airDateUtc": "2022-02-25T00:00:00Z",
                "episodeFileId": 0,
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
        self.assertFalse(
            CollectionSourceState.objects.filter(
                user=self.user,
                item=show_item,
                source="sonarr",
            ).exists(),
        )
        self.assertFalse(
            CollectionEntry.objects.filter(
                user=self.user,
                item=show_item,
            ).exists(),
        )

        season_item = Item.objects.get(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
        )
        self.assertEqual(season_item.title, "Severance Season 1")
        self.assertIsNotNone(season_item.metadata_fetched_at)

        collected_episode = Item.objects.get(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
        )
        uncollected_episode = Item.objects.get(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
        )
        self.assertIsNotNone(collected_episode.metadata_fetched_at)
        self.assertIsNotNone(uncollected_episode.metadata_fetched_at)
        self.assertTrue(
            CollectionEntry.objects.filter(
                user=self.user,
                item=collected_episode,
            ).exists(),
        )
        self.assertFalse(
            CollectionEntry.objects.filter(
                user=self.user,
                item=uncollected_episode,
            ).exists(),
        )

        self.assertEqual(
            get_tv_show_collection_stats(
                self.user,
                show_item,
                metadata_episode_count=2,
            ),
            {
                "collected_seasons": 1,
                "total_seasons": 1,
                "collected_episodes": 1,
                "total_episodes": 2,
            },
        )

    @patch("integrations.imports.sonarr.services.get_media_metadata")
    @patch("integrations.imports.sonarr.SonarrClient.episodes")
    @patch("integrations.imports.sonarr.SonarrClient.series")
    def test_sonarr_import_marks_existing_seeded_rows_as_fetched(
        self,
        mock_series,
        mock_episodes,
        mock_get_media_metadata,
    ):
        """Sonarr imports should stamp pre-existing season/episode rows as seeded."""
        Item.objects.create(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Severance",
            image="https://example.com/severance.jpg",
        )
        season_item = Item.objects.create(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Severance Season 1",
            image="https://example.com/season1.jpg",
        )
        episode_item = Item.objects.create(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Good News About Hell",
            image="https://example.com/episode1.jpg",
        )

        mock_series.return_value = [
            {
                "id": 77,
                "title": "Severance",
                "statistics": {"episodeFileCount": 1},
                "tmdbId": 95396,
                "seasons": [{"seasonNumber": 1}],
            },
        ]
        mock_episodes.return_value = [
            {
                "seasonNumber": 1,
                "episodeNumber": 1,
                "title": "Good News About Hell",
                "airDateUtc": "2022-02-18T00:00:00Z",
                "episodeFileId": 11,
            },
        ]
        mock_get_media_metadata.return_value = {
            "title": "Severance",
            "image": "https://example.com/severance.jpg",
            "genres": [],
        }

        sonarr.importer(None, self.user, "new")

        season_item.refresh_from_db()
        episode_item.refresh_from_db()

        self.assertIsNotNone(season_item.metadata_fetched_at)
        self.assertIsNotNone(episode_item.metadata_fetched_at)

    @patch("integrations.imports.sonarr.SonarrClient.series")
    def test_sonarr_import_removes_episode_collection_when_series_has_no_files(
        self,
        mock_series,
    ):
        """A Sonarr series with no files should clear stale episode ownership."""
        Item.objects.create(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Severance",
            image="https://example.com/severance.jpg",
        )
        episode_item = Item.objects.create(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Good News About Hell",
            image="https://example.com/severance.jpg",
        )
        CollectionEntry.objects.create(user=self.user, item=episode_item)
        CollectionSourceState.objects.create(
            user=self.user,
            item=episode_item,
            source="sonarr",
        )

        mock_series.return_value = [
            {
                "id": 77,
                "title": "Severance",
                "statistics": {"episodeFileCount": 0},
                "tmdbId": 95396,
            },
        ]

        imported_counts, warnings = sonarr.importer(None, self.user, "new")

        self.assertEqual(imported_counts, {})
        self.assertEqual(warnings, "")
        self.assertFalse(
            CollectionSourceState.objects.filter(
                user=self.user,
                item=episode_item,
                source="sonarr",
            ).exists(),
        )
        self.assertFalse(
            CollectionEntry.objects.filter(
                user=self.user,
                item=episode_item,
            ).exists(),
        )

    @patch("integrations.imports.radarr.requests.get")
    def test_radarr_import_marks_connection_broken_on_timeout(self, mock_get):
        """Radarr timeouts should become handled import errors with account state."""
        mock_get.side_effect = requests.exceptions.ConnectTimeout("connect timed out")

        with self.assertRaises(helpers.MediaImportError) as cm:
            radarr.importer(None, self.user, "new")

        self.radarr_account.refresh_from_db()
        self.assertTrue(self.radarr_account.connection_broken)
        self.assertIn("Could not reach Radarr", str(cm.exception))
        self.assertIn("Could not reach Radarr", self.radarr_account.last_error_message)

    @patch("integrations.imports.sonarr.requests.get")
    def test_sonarr_import_marks_connection_broken_on_timeout(self, mock_get):
        """Sonarr timeouts should become handled import errors with account state."""
        mock_get.side_effect = requests.exceptions.ConnectTimeout("connect timed out")

        with self.assertRaises(helpers.MediaImportError) as cm:
            sonarr.importer(None, self.user, "new")

        self.sonarr_account.refresh_from_db()
        self.assertTrue(self.sonarr_account.connection_broken)
        self.assertIn("Could not reach Sonarr", str(cm.exception))
        self.assertIn("Could not reach Sonarr", self.sonarr_account.last_error_message)


class ArrImportTaskTests(TestCase):
    """Cover handled task failures for ARR imports."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="arr-task-user")

    @patch("integrations.tasks._media_imports.import_media")
    def test_import_radarr_task_returns_failure_message_for_expected_errors(self, mock_import_media):
        mock_import_media.side_effect = helpers.MediaImportError("Could not reach Radarr: connect timed out")

        result = tasks.import_radarr(user_id=self.user.id)

        self.assertEqual(result, "Radarr import failed: Could not reach Radarr: connect timed out")

    @patch("integrations.tasks._media_imports.import_media")
    def test_import_sonarr_task_returns_failure_message_for_expected_errors(self, mock_import_media):
        mock_import_media.side_effect = helpers.MediaImportError("Could not reach Sonarr: connect timed out")

        result = tasks.import_sonarr(user_id=self.user.id)

        self.assertEqual(result, "Sonarr import failed: Could not reach Sonarr: connect timed out")


class CollectionSourceSyncTests(TestCase):
    """Cover collection source sync durability helpers."""

    def setUp(self):
        """Create a user/item pair for source sync tests."""
        self.user = get_user_model().objects.create_user(username="source-sync-user")
        self.item = Item.objects.create(
            media_id="95396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Good News About Hell",
            image="https://example.com/severance.jpg",
        )

    @patch("integrations.source_sync._reconcile_collection_entry", return_value="ok")
    @patch("integrations.source_sync.CollectionSourceState.objects.update_or_create")
    def test_upsert_collection_source_state_retries_sqlite_locks(
        self,
        mock_update_or_create,
        mock_reconcile,
    ):
        """Source sync should retry Sonarr state writes when SQLite is locked."""
        mock_update_or_create.side_effect = [
            OperationalError("database is locked"),
            (CollectionSourceState(), True),
        ]

        result = upsert_collection_source_state(
            user=self.user,
            item=self.item,
            source="sonarr",
            quality_label="WEBDL-1080p",
        )

        self.assertEqual(result, "ok")
        self.assertEqual(mock_update_or_create.call_count, 2)
        mock_reconcile.assert_called_once_with(user=self.user, item=self.item)
