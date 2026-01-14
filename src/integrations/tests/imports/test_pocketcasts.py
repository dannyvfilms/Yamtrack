from datetime import UTC, datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import Item, MediaTypes, Podcast, Sources, Status
from integrations.imports.pocketcasts import PocketCastsImporter
from integrations.models import PocketCastsAccount


class PocketCastsInferenceTests(TestCase):
    """Tests for Pocket Casts completion time inference logic."""

    def setUp(self):
        """Set up test fixtures."""
        User = get_user_model()
        self.user = User.objects.create_user(username="testuser", password="pass")  # noqa: S106
        self.sync_start = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        self.sync_end = datetime(2025, 1, 1, 14, 0, tzinfo=UTC)
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
        """Create an in-progress podcast with history record at the given date."""
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
        """Anchored completion uses remaining time."""
        episode_uuid = "episode-anchor"
        # 40 min progress on 60 min podcast = 20 min remaining
        self._create_in_progress_history(episode_uuid, 40, self.sync_start)

        inferred = self.importer._infer_completion_date(
            3600,  # 60 min total duration
            self.sync_start,
            self.sync_end,
            [],
            [],
            self.sync_start,
            episode_uuid,
            self.sync_start,
        )

        # 60 min - 40 min progress = 20 min remaining
        self.assertEqual(inferred, self.sync_start + timedelta(minutes=20))

    def test_infer_completion_without_anchor_uses_hash_distribution(self):
        """Non-anchored completion uses hash-based distribution across window."""
        episode_uuid = "episode-no-anchor"

        inferred = self.importer._infer_completion_date(
            1800,  # 30 min duration
            self.sync_start,
            self.sync_end,
            [],
            [],
            self.sync_start,
            episode_uuid,
            self.sync_start,
        )

        # Should be within the window, NOT at sync_end
        self.assertGreaterEqual(inferred, self.sync_start)
        self.assertLessEqual(inferred, self.sync_end)
        self.assertNotEqual(inferred, self.sync_end)
        # With boundary avoidance, should not be exactly at sync_start either
        self.assertGreater(inferred, self.sync_start + timedelta(seconds=30))

    def test_infer_completion_conflict_with_scrobble(self):
        """Anchored completion pushed after scrobbled music block."""
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

        # Should be pushed after the scrobble block ends
        self.assertGreaterEqual(inferred, scrobble_end)
        self.assertLessEqual(inferred, self.sync_end)

    def test_infer_completion_long_duration_not_at_window_end(self):
        """Long podcasts (> window) should NOT land at sync_window_end."""
        episode_uuid = "episode-long"

        inferred = self.importer._infer_completion_date(
            4 * 60 * 60,  # 4 hours (longer than 2-hour window)
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
        # Key test: should NOT be at sync_window_end (the old buggy behavior)
        self.assertNotEqual(inferred, self.sync_end)
        # With boundary avoidance, should be at least 60s from end
        self.assertLess(inferred, self.sync_end - timedelta(seconds=30))

    def test_multiple_completions_use_inferred_podcasts_blocking(self):
        """Multiple completions get different times via blocked intervals."""
        first_uuid = "episode-first"
        second_uuid = "episode-second"
        third_uuid = "episode-third"

        # Track inferred podcasts as blocked intervals (like import_data does)
        inferred_podcasts = []

        first_completion = self.importer._infer_completion_date(
            1800,
            self.sync_start,
            self.sync_end,
            [],
            [],
            self.sync_start,
            first_uuid,
            self.sync_start,
            inferred_podcasts=inferred_podcasts,
        )
        inferred_podcasts.append((first_completion, 300))

        second_completion = self.importer._infer_completion_date(
            1800,
            self.sync_start,
            self.sync_end,
            [],
            [],
            self.sync_start + timedelta(minutes=1),
            second_uuid,
            self.sync_start,
            inferred_podcasts=inferred_podcasts,
        )
        inferred_podcasts.append((second_completion, 300))

        third_completion = self.importer._infer_completion_date(
            1800,
            self.sync_start,
            self.sync_end,
            [],
            [],
            self.sync_start + timedelta(minutes=2),
            third_uuid,
            self.sync_start,
            inferred_podcasts=inferred_podcasts,
        )

        # All should be different
        completions = {first_completion, second_completion, third_completion}
        self.assertEqual(len(completions), 3, "All three completions should be unique")

        # All within window
        for c in completions:
            self.assertGreaterEqual(c, self.sync_start)
            self.assertLessEqual(c, self.sync_end)

    def test_last_in_progress_record_across_duplicates(self):
        """In-progress record found across multiple Podcast rows."""
        episode_uuid = "episode-dup"
        first_podcast = self._create_in_progress_history(
            episode_uuid, 30, self.sync_start
        )

        Podcast.objects.create(
            user=self.user,
            item=first_podcast.item,
            status=Status.COMPLETED.value,
            progress=60,
        )

        last_date, last_progress = self.importer._get_last_in_progress_record(
            episode_uuid
        )
        self.assertEqual(last_date, self.sync_start)
        self.assertEqual(last_progress, 30)

    def test_boundary_avoidance_in_fallback(self):
        """Fallback completion time avoids landing on gap boundaries."""
        # Create blocked intervals that leave a gap at the end (12:00-13:30 blocked)
        scrobble_end = self.sync_start + timedelta(hours=1, minutes=30)
        existing_history = [(scrobble_end, 90 * 60, "music", True)]

        episode_uuid = "episode-boundary-test"

        inferred = self.importer._infer_completion_date(
            1800,  # 30 min
            self.sync_start,
            self.sync_end,
            existing_history,
            [],
            self.sync_start,
            episode_uuid,
            self.sync_start,
        )

        # Should be in the gap (13:30-14:00) but not exactly at 14:00
        self.assertGreaterEqual(inferred, scrobble_end)
        self.assertLessEqual(inferred, self.sync_end)
        # Should not land exactly at sync_end (boundary avoidance)
        self.assertNotEqual(inferred, self.sync_end)

    def test_build_blocked_intervals_includes_inferred_podcasts(self):
        """_build_blocked_intervals includes previously inferred podcasts."""
        inferred_time = self.sync_start + timedelta(hours=1)
        inferred_podcasts = [(inferred_time, 300)]  # 5 min buffer

        blocked = self.importer._build_blocked_intervals(
            [],  # No scrobbled items
            self.sync_start,
            self.sync_end,
            inferred_podcasts=inferred_podcasts,
        )

        # Should have one blocked interval around the inferred time
        self.assertEqual(len(blocked), 1)
        start, end = blocked[0]
        # Should contain the inferred time
        self.assertLessEqual(start, inferred_time)
        self.assertGreaterEqual(end, inferred_time)


class PocketCastsDistributionSimulatorTest(TestCase):
    """Simulator-style test to verify completion time distribution."""

    def setUp(self):
        """Set up test fixtures."""
        User = get_user_model()
        self.user = User.objects.create_user(username="simuser", password="pass")  # noqa: S106
        PocketCastsAccount.objects.create(
            user=self.user,
            access_token="token",
            last_sync_at=datetime(2025, 1, 1, 10, 0, tzinfo=UTC),
        )
        self.importer = PocketCastsImporter(self.user, "new")

    def test_50_episodes_distribution_not_stacked(self):
        """50 fake episodes should be distributed, not stacked at sync times."""
        sync_start = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
        sync_end = datetime(2025, 1, 1, 14, 0, tzinfo=UTC)

        completions = []
        inferred_podcasts = []

        # Simulate 50 episodes completing in the same window
        for i in range(50):
            episode_uuid = f"sim-episode-{i:03d}"
            duration = 1800 + (i * 60)  # 30-79 minute episodes

            completion = self.importer._infer_completion_date(
                duration,
                sync_start,
                sync_end,
                [],
                [],
                sync_start + timedelta(minutes=i),
                episode_uuid,
                sync_start,
                inferred_podcasts=inferred_podcasts,
            )
            completions.append(completion)
            inferred_podcasts.append((completion, 300))

        # Verify all are within window
        for c in completions:
            self.assertGreaterEqual(c, sync_start)
            self.assertLessEqual(c, sync_end)

        # Verify distribution: count how many land in each 10-minute bucket
        buckets = [0] * 12  # 12 ten-minute buckets in 2 hours
        for c in completions:
            offset_minutes = int((c - sync_start).total_seconds() / 60)
            bucket = min(offset_minutes // 10, 11)
            buckets[bucket] += 1

        # No single bucket should have more than 40% of completions (20 episodes)
        max_bucket = max(buckets)
        self.assertLess(max_bucket, 20, f"Bucket distribution too clustered: {buckets}")

        # Verify uniqueness: most should be unique (some collision allowed)
        unique_times = len(set(completions))
        self.assertGreater(
            unique_times, 40, f"Too many duplicate times: {unique_times}/50 unique"
        )

        # Verify boundary avoidance: check minute :00 stacking
        minute_zero_count = sum(1 for c in completions if c.minute == 0)
        # Should have very few at minute :00 (less than 10%)
        self.assertLess(
            minute_zero_count, 5, f"Too many at minute :00: {minute_zero_count}/50"
        )
