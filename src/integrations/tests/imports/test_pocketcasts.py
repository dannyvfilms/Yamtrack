from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import Item, MediaTypes, Podcast, Sources, Status
from integrations.imports.pocketcasts import PocketCastsImporter
from integrations.models import PocketCastsAccount


class PocketCastsInferenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="testuser", password="pass")
        self.sync_start = datetime(2025, 1, 1, 12, 0, tzinfo=dt_timezone.utc)
        self.sync_end = datetime(2025, 1, 1, 14, 0, tzinfo=dt_timezone.utc)
        PocketCastsAccount.objects.create(
            user=self.user,
            access_token="token",
            last_sync_at=self.sync_start,
        )
        self.importer = PocketCastsImporter(self.user, "new")

    def _create_item(self, episode_uuid):
        return Item.objects.create(
            media_id=episode_uuid,
            source=Sources.POCKETCASTS.value,
            media_type=MediaTypes.PODCAST.value,
            title="Test Episode",
            image="http://example.com/episode.jpg",
        )

    def _create_in_progress_history(self, episode_uuid, progress_minutes, history_date):
        item = self._create_item(episode_uuid)
        podcast = Podcast.objects.create(
            user=self.user,
            item=item,
            status=Status.IN_PROGRESS.value,
            progress=progress_minutes,
        )
        history_record = podcast.history.order_by("-history_date").first()
        history_record.progress = progress_minutes
        history_record.status = Status.IN_PROGRESS.value
        history_record.end_date = None
        history_record.history_date = history_date
        history_record.save()
        return podcast

    def test_infer_completion_with_anchor(self):
        episode_uuid = "episode-anchor"
        self._create_in_progress_history(episode_uuid, 40, self.sync_start)

        inferred = self.importer._infer_completion_date(
            3600,
            self.sync_start,
            self.sync_end,
            [],
            [],
            self.sync_start,
            episode_uuid,
            self.sync_start,
        )

        self.assertEqual(inferred, self.sync_start + timedelta(minutes=20))

    def test_infer_completion_without_anchor(self):
        episode_uuid = "episode-no-anchor"

        inferred = self.importer._infer_completion_date(
            1800,
            self.sync_start,
            self.sync_end,
            [],
            [],
            self.sync_start,
            episode_uuid,
            self.sync_start,
        )

        self.assertEqual(inferred, self.sync_start + timedelta(minutes=30))
        self.assertNotEqual(inferred, self.sync_end)

    def test_infer_completion_conflict_with_scrobble(self):
        episode_uuid = "episode-conflict"
        self._create_in_progress_history(episode_uuid, 40, self.sync_start)
        scrobble_end = self.sync_start + timedelta(hours=1, minutes=20)
        existing_history = [(scrobble_end, 80 * 60, "music", True)]

        inferred = self.importer._infer_completion_date(
            3600,
            self.sync_start,
            self.sync_end,
            existing_history,
            [],
            self.sync_start,
            episode_uuid,
            self.sync_start,
        )

        self.assertEqual(inferred, self.sync_start + timedelta(hours=1, minutes=40))

    def test_infer_completion_long_duration_fallback(self):
        episode_uuid = "episode-long"

        inferred = self.importer._infer_completion_date(
            4 * 60 * 60,
            self.sync_start,
            self.sync_end,
            [],
            [],
            self.sync_start,
            episode_uuid,
            self.sync_start,
        )

        self.assertGreaterEqual(inferred, self.sync_start)
        self.assertLessEqual(inferred, self.sync_end)
        self.assertNotEqual(inferred, self.sync_end)

    def test_multiple_new_completions_sequence(self):
        first_uuid = "episode-first"
        second_uuid = "episode-second"

        first_completion = self.importer._infer_completion_date(
            1800,
            self.sync_start,
            self.sync_end,
            [],
            [],
            self.sync_start,
            first_uuid,
            self.sync_start,
        )

        other_podcasts = [(self.sync_start, 1800, first_completion)]
        second_completion = self.importer._infer_completion_date(
            1800,
            self.sync_start,
            self.sync_end,
            [],
            other_podcasts,
            self.sync_start + timedelta(minutes=1),
            second_uuid,
            self.sync_start,
        )

        self.assertNotEqual(first_completion, second_completion)
        self.assertGreater(second_completion, first_completion)

    def test_last_in_progress_record_across_duplicates(self):
        episode_uuid = "episode-dup"
        first_podcast = self._create_in_progress_history(episode_uuid, 30, self.sync_start)

        Podcast.objects.create(
            user=self.user,
            item=first_podcast.item,
            status=Status.COMPLETED.value,
            progress=60,
        )

        last_date, last_progress = self.importer._get_last_in_progress_record(episode_uuid)
        self.assertEqual(last_date, self.sync_start)
        self.assertEqual(last_progress, 30)
