from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import history_cache, tasks
from app.log_safety import stable_hmac
from app.models import Album, Artist, Item, MediaTypes, Movie, Music, Sources, Status, Track


class HistoryMonthCacheTests(TestCase):
    def setUp(self):
        """Create a user with one history entry and a cold cache."""
        cache.clear()
        self.credentials = {"username": "history-fallback", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        item = Item.objects.create(
            media_id="movie-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Fallback Movie",
            image="http://example.com/movie.jpg",
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )
        cache.clear()
        self.client.login(**self.credentials)
        self.logging_style = history_cache._normalize_logging_style(None, self.user)
        self.today_key = history_cache.history_day_key(timezone.localtime())

    def tearDown(self):
        """Reset cache state between tests."""
        cache.clear()

    def _active_month_day_keys(self):
        month_prefix = timezone.localdate().strftime("%Y%m")
        index_days = history_cache.build_history_index(self.user, self.logging_style)
        return [day_key for day_key in index_days if day_key.startswith(month_prefix)]

    @patch("app.history_cache.schedule_history_day_cache_coverage")
    @patch("app.history_cache.schedule_history_refresh")
    def test_history_view_repairs_cold_month_inline_without_scheduling_refresh(
        self,
        mock_schedule_history_refresh,
        mock_schedule_history_day_cache_coverage,
    ):
        response = self.client.get(reverse("history"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["history_refreshing"])
        self.assertTrue(response.context["history_days"])
        self.assertEqual(
            response.context["history_days"][0]["entries"][0]["title"],
            "Fallback Movie",
        )
        self.assertEqual(mock_schedule_history_refresh.call_count, 0)

        active_month_day_keys = self._active_month_day_keys()
        cache_keys = [
            history_cache._day_cache_key(self.user.id, self.logging_style, day_key)
            for day_key in active_month_day_keys
        ]
        cached_payloads = cache.get_many(cache_keys)
        self.assertEqual(len(cached_payloads), len(cache_keys))
        self.assertEqual(len(response.context["history_days"]), len(active_month_day_keys))
        mock_schedule_history_day_cache_coverage.assert_called_once_with(
            self.user.id,
            self.logging_style,
            countdown=15,
        )

    @patch("app.history_cache.schedule_history_day_cache_coverage")
    @patch("app.history_cache.schedule_history_refresh")
    def test_history_view_repairs_partial_month_miss_inline_without_refreshing(
        self,
        mock_schedule_history_refresh,
        mock_schedule_history_day_cache_coverage,
    ):
        first_response = self.client.get(reverse("history"))

        self.assertEqual(first_response.status_code, 200)
        self.assertFalse(first_response.context["history_refreshing"])
        mock_schedule_history_day_cache_coverage.reset_mock()

        cache.delete_many(
            [
                history_cache._day_cache_key(
                    self.user.id,
                    self.logging_style,
                    self.today_key,
                ),
            ],
        )

        response = self.client.get(reverse("history"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["history_refreshing"])
        self.assertTrue(response.context["history_days"])
        self.assertEqual(
            response.context["history_days"][0]["entries"][0]["title"],
            "Fallback Movie",
        )
        self.assertEqual(mock_schedule_history_refresh.call_count, 0)
        self.assertIsNotNone(
            cache.get(
                history_cache._day_cache_key(
                    self.user.id,
                    self.logging_style,
                    self.today_key,
                ),
            ),
        )
        self.assertEqual(
            len(response.context["history_days"]),
            len(self._active_month_day_keys()),
        )
        mock_schedule_history_day_cache_coverage.assert_called_once_with(
            self.user.id,
            self.logging_style,
            countdown=15,
        )

    def test_refresh_history_cache_repairs_missing_index_day_payloads(self):
        history_cache.refresh_history_cache(self.user.id, logging_style=self.logging_style)

        cache.delete(
            history_cache._day_cache_key(
                self.user.id,
                self.logging_style,
                self.today_key,
            ),
        )

        history_cache.refresh_history_cache(self.user.id, logging_style=self.logging_style)

        index_entry = cache.get(history_cache._cache_key(self.user.id, self.logging_style))
        self.assertIsNotNone(index_entry)
        self.assertIn(self.today_key, index_entry["days"])
        self.assertIsNotNone(
            cache.get(
                history_cache._day_cache_key(
                    self.user.id,
                    self.logging_style,
                    self.today_key,
                ),
            ),
        )

    @patch("app.history_cache_lifecycle.schedule_history_refresh", return_value=True)
    def test_invalidate_history_days_keeps_existing_payload_until_refresh_overwrites_it(
        self,
        mock_schedule_history_refresh,
    ):
        history_cache.refresh_history_cache(self.user.id, logging_style=self.logging_style)
        cache_key = history_cache._day_cache_key(
            self.user.id,
            self.logging_style,
            self.today_key,
        )
        cached_payload = cache.get(cache_key)

        history_cache.invalidate_history_days(
            self.user.id,
            day_keys=[self.today_key],
            logging_styles=(self.logging_style,),
            reason="test_change",
        )

        self.assertEqual(cache.get(cache_key), cached_payload)
        mock_schedule_history_refresh.assert_called_once_with(
            self.user.id,
            self.logging_style,
            warm_days=0,
            day_keys=[self.today_key],
        )


class HistoryRefreshSchedulingTests(TestCase):
    def setUp(self):
        """Create a user for lock cleanup tests."""
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="history-scheduling",
            password="12345",
        )

    def tearDown(self):
        """Reset cache state between tests."""
        cache.clear()

    @patch("app.tasks.refresh_history_cache_task.apply_async", side_effect=RuntimeError("broker unavailable"))
    def test_schedule_history_refresh_clears_locks_when_enqueue_fails(self, _mock_apply_async):
        day_key = timezone.localdate().isoformat()

        scheduled = history_cache.schedule_history_refresh(
            self.user.id,
            "repeats",
            day_keys=[day_key],
            allow_inline=False,
        )

        self.assertFalse(scheduled)

        normalized_day_key = history_cache.history_day_key(day_key)
        lock_key = history_cache._refresh_lock_key(self.user.id, "repeats")
        dedupe_hash = stable_hmac(
            normalized_day_key,
            namespace="history_refresh_days",
            length=10,
        )
        dedupe_key = f"{lock_key}_days_{dedupe_hash}"

        self.assertIsNone(cache.get(lock_key))
        self.assertIsNone(cache.get(dedupe_key))

    @patch("app.tasks.refresh_history_cache_task.apply_async")
    def test_schedule_history_refresh_uses_interactive_priority(self, mock_apply_async):
        scheduled = history_cache.schedule_history_refresh(
            self.user.id,
            "repeats",
            allow_inline=False,
        )

        self.assertTrue(scheduled)
        mock_apply_async.assert_called_once()
        self.assertEqual(
            mock_apply_async.call_args.kwargs["priority"],
            settings.CELERY_TASK_PRIORITY_INTERACTIVE,
        )

    @patch("app.tasks.repair_history_day_cache_coverage_task.apply_async")
    def test_schedule_history_day_cache_coverage_uses_background_priority(
        self,
        mock_apply_async,
    ):
        scheduled = history_cache.schedule_history_day_cache_coverage(
            self.user.id,
            "repeats",
            debounce_seconds=0,
        )

        self.assertTrue(scheduled)
        mock_apply_async.assert_called_once()
        self.assertEqual(
            mock_apply_async.call_args.kwargs["priority"],
            settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )

    @patch("app.tasks.repair_history_day_cache_coverage_task.apply_async")
    @patch("app.tasks.interactive_request_active", return_value=True)
    def test_repair_history_day_cache_coverage_task_defers_for_interactive_requests(
        self,
        _mock_interactive_request_active,
        mock_apply_async,
    ):
        result = tasks.repair_history_day_cache_coverage_task(
            self.user.id,
            "repeats",
            batch_size=25,
        )

        self.assertEqual(
            result,
            {"skipped": True, "reason": "interactive_request_active"},
        )
        mock_apply_async.assert_called_once_with(
            kwargs={
                "user_id": self.user.id,
                "logging_style": "repeats",
                "batch_size": 25,
            },
            countdown=tasks.HISTORY_COVERAGE_REPAIR_INTERACTIVE_RETRY_SECONDS,
            priority=settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )

    @patch("app.tasks.repair_history_day_cache_coverage_task.apply_async")
    @patch(
        "app.history_cache.repair_history_day_cache_coverage",
        return_value={"remaining": 10},
    )
    def test_repair_history_day_cache_coverage_task_uses_slower_requeue_interval(
        self,
        _mock_repair_history_day_cache_coverage,
        mock_apply_async,
    ):
        tasks.repair_history_day_cache_coverage_task(
            self.user.id,
            "repeats",
            batch_size=25,
        )

        mock_apply_async.assert_called_once_with(
            kwargs={
                "user_id": self.user.id,
                "logging_style": "repeats",
                "batch_size": 25,
            },
            countdown=tasks.HISTORY_COVERAGE_REPAIR_REQUEUE_SECONDS,
            priority=settings.CELERY_TASK_PRIORITY_BACKGROUND,
        )


class MusicHistoryOwnershipTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="history-music-owner",
            password="12345",
        )
        self.other_user = get_user_model().objects.create_user(
            username="history-music-other",
            password="12345",
        )
        self.artist = Artist.objects.create(name="History Artist")
        self.album = Album.objects.create(title="History Album", artist=self.artist)
        self.track = Track.objects.create(
            album=self.album,
            title="History Track",
            track_number=1,
            duration_ms=180000,
        )
        self.played_at = timezone.now().replace(second=0, microsecond=0)
        self.day_key = history_cache.history_day_key(self.played_at)

    def tearDown(self):
        cache.clear()

    def _create_music_play(self, *, user, media_id, title):
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MUSIC.value,
            title=title,
            image="http://example.com/music.jpg",
            runtime_minutes=3,
        )
        music = Music.objects.create(
            item=item,
            user=user,
            album=self.album,
            artist=self.artist,
            track=self.track,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=self.played_at,
            end_date=self.played_at,
        )
        music.history.all().update(history_user=None)
        return music

    def test_build_history_day_keeps_owned_music_with_null_history_user(self):
        self._create_music_play(
            user=self.user,
            media_id="owned-music",
            title="Owned Music",
        )

        day = history_cache.build_history_day(self.user, self.day_key)

        self.assertIsNotNone(day)
        music_entries = [
            entry for entry in day["entries"] if entry["media_type"] == MediaTypes.MUSIC.value
        ]
        self.assertEqual(len(music_entries), 1)
        self.assertEqual(music_entries[0]["title"], "History Album")
        self.assertEqual(music_entries[0]["play_count"], 1)
        self.assertIn(self.day_key, history_cache.build_history_index(self.user, "repeats"))

    def test_build_history_day_excludes_foreign_music_with_null_history_user(self):
        self._create_music_play(
            user=self.other_user,
            media_id="foreign-music",
            title="Foreign Music",
        )

        self.assertIsNone(history_cache.build_history_day(self.user, self.day_key))
        self.assertNotIn(self.day_key, history_cache.build_history_index(self.user, "repeats"))

    def test_repair_history_day_cache_coverage_repairs_in_batches(self):
        current_item = Item.objects.create(
            media_id="movie-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Current Movie",
            image="http://example.com/movie-1.jpg",
        )
        Movie.objects.create(
            item=current_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )

        item = Item.objects.create(
            media_id="movie-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Second Movie",
            image="http://example.com/movie-2.jpg",
        )
        previous_day = timezone.now() - timedelta(days=1)
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=previous_day,
            end_date=previous_day,
        )

        logging_style = history_cache._normalize_logging_style(None, self.user)
        history_cache.refresh_history_cache(self.user.id, logging_style=logging_style)
        index_entry = cache.get(history_cache._cache_key(self.user.id, logging_style))
        self.assertIsNotNone(index_entry)
        day_keys = index_entry["days"][:2]
        cache.delete_many(
            [
                history_cache._day_cache_key(self.user.id, logging_style, day_key)
                for day_key in day_keys
            ],
        )

        result = history_cache.repair_history_day_cache_coverage(
            self.user.id,
            logging_style=logging_style,
            batch_size=1,
        )

        self.assertEqual(result["rebuilt"], 1)
        self.assertEqual(result["remaining"], 1)
