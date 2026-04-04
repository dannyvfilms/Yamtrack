# ruff: noqa: D102, S106

from datetime import timedelta
from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from app.discover import tab_cache
from app.discover.schemas import CandidateItem, RowResult
from app.models import DiscoverApiCache, DiscoverRowCache, DiscoverTasteProfile


class DiscoverTabCacheTests(TestCase):
    """Unit tests for the Redis-backed Discover tab cache layer."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="discover-cache-user",
            password="secret123",
        )
        self.factory = RequestFactory()
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _row(self, title="Cached Movie", *, reserve_title=None):
        return RowResult(
            key="top_picks_for_you",
            title="Top Picks For You",
            mission="Personal Taste Match",
            why="Tailored picks.",
            source="local",
            items=[
                CandidateItem(
                    media_type="movie",
                    source="tmdb",
                    media_id="101",
                    title=title,
                    image="https://example.com/poster.jpg",
                    final_score=0.91,
                ),
            ],
            reserve_items=[
                CandidateItem(
                    media_type="movie",
                    source="tmdb",
                    media_id="102",
                    title=reserve_title or "Reserve Movie",
                    image="https://example.com/reserve.jpg",
                    final_score=0.74,
                ),
            ]
            if reserve_title is not None
            else [],
        )

    @patch("app.discover.service.get_discover_rows")
    def test_get_tab_rows_uses_cached_payload_without_rebuild(
        self, mock_get_discover_rows
    ):
        tab_cache.set_tab_cache(self.user.id, "movie", [self._row()])

        rows = tab_cache.get_tab_rows(self.user, "movie", show_more=False)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].items[0].title, "Cached Movie")
        mock_get_discover_rows.assert_not_called()

    @patch("app.discover.tab_cache.schedule_tab_refresh", return_value=True)
    @patch("app.discover.service.get_discover_rows")
    def test_get_tab_rows_cold_miss_returns_empty_and_schedules_refresh(
        self,
        mock_get_discover_rows,
        mock_schedule,
    ):
        rows = tab_cache.get_tab_rows(self.user, "movie", show_more=False)

        self.assertEqual(rows, [])
        mock_get_discover_rows.assert_not_called()
        mock_schedule.assert_called_once_with(
            self.user.id,
            "movie",
            show_more=False,
            debounce_seconds=tab_cache.DISCOVER_PRIORITY_REFRESH_DEBOUNCE_SECONDS,
            countdown=tab_cache.DISCOVER_PRIORITY_REFRESH_COUNTDOWN,
        )

    @patch("app.discover.tab_cache.schedule_tab_refresh", return_value=True)
    def test_get_tab_rows_marks_stale_payload_and_schedules_immediate_refresh(
        self, mock_schedule
    ):
        tab_cache.set_tab_cache(self.user.id, "movie", [self._row()])
        tab_cache.bump_activity_version(self.user.id, "movie")

        rows = tab_cache.get_tab_rows(self.user, "movie", show_more=False)

        self.assertTrue(rows[0].is_stale)
        self.assertEqual(rows[0].source_state, "stale")
        mock_schedule.assert_called_once_with(
            self.user.id,
            "movie",
            show_more=False,
            debounce_seconds=tab_cache.DISCOVER_PRIORITY_REFRESH_DEBOUNCE_SECONDS,
            countdown=tab_cache.DISCOVER_PRIORITY_REFRESH_COUNTDOWN,
        )

    @patch("app.discover.service.get_discover_rows")
    def test_discover_debug_bypasses_tab_cache(self, mock_get_discover_rows):
        mock_get_discover_rows.return_value = [self._row(title="Debug Movie")]
        tab_cache.set_tab_cache(
            self.user.id, "movie", [self._row(title="Cached Movie")]
        )

        rows = tab_cache.get_tab_rows(
            self.user,
            "movie",
            show_more=False,
            include_debug=True,
        )

        self.assertEqual(rows[0].items[0].title, "Debug Movie")
        mock_get_discover_rows.assert_called_once_with(
            self.user,
            "movie",
            show_more=False,
            include_debug=True,
            defer_artwork=True,
        )

    def test_set_tab_cache_preserves_reserve_items(self):
        tab_cache.set_tab_cache(
            self.user.id,
            "movie",
            [self._row(reserve_title="Backfill Movie")],
        )

        rows = tab_cache.get_tab_rows(self.user, "movie", show_more=False)

        self.assertEqual(rows[0].reserve_items[0].title, "Backfill Movie")

    @patch("app.discover.tab_cache.schedule_tab_refresh", return_value=True)
    def test_get_tab_status_schedules_refresh_for_stale_payload(self, mock_schedule):
        tab_cache.set_tab_cache(self.user.id, "movie", [self._row()])
        tab_cache.bump_activity_version(self.user.id, "movie")

        status = tab_cache.get_tab_status(self.user.id, "movie", show_more=False)

        self.assertTrue(status["exists"])
        self.assertTrue(status["is_stale"])
        self.assertTrue(status["is_refreshing"])
        self.assertTrue(status["refresh_scheduled"])
        mock_schedule.assert_called_once_with(
            self.user.id,
            "movie",
            show_more=False,
            debounce_seconds=tab_cache.DISCOVER_PRIORITY_REFRESH_DEBOUNCE_SECONDS,
            countdown=tab_cache.DISCOVER_PRIORITY_REFRESH_COUNTDOWN,
        )

    @patch("app.tasks.refresh_discover_tab_cache.apply_async")
    @override_settings(DISCOVER_TASKS_EAGER_REFRESH=True)
    def test_schedule_tab_refresh_deduplicates_requests(self, mock_apply_async):
        first = tab_cache.schedule_tab_refresh(self.user.id, "movie", show_more=False)
        second = tab_cache.schedule_tab_refresh(self.user.id, "movie", show_more=False)

        self.assertTrue(first)
        self.assertFalse(second)
        mock_apply_async.assert_called_once()

    @patch("app.tasks.refresh_discover_tab_cache.apply_async")
    @override_settings(DISCOVER_TASKS_EAGER_REFRESH=True)
    def test_schedule_tab_refresh_replaces_stale_lock_when_forced(
        self, mock_apply_async
    ):
        lock_key = tab_cache._refresh_lock_key(
            self.user.id,
            "movie",
            show_more=False,
        )
        cache.set(
            lock_key,
            {"started_at": (timezone.now() - timedelta(minutes=10)).isoformat()},
            timeout=tab_cache.DISCOVER_TAB_REFRESH_LOCK_TTL,
        )

        scheduled = tab_cache.schedule_tab_refresh(
            self.user.id,
            "movie",
            show_more=False,
            force=True,
        )

        self.assertTrue(scheduled)
        mock_apply_async.assert_called_once()

    @patch("app.discover.tab_cache.has_fresh_tab_cache")
    @patch("app.discover.tab_cache.schedule_tab_refresh")
    def test_warm_sibling_tabs_skips_redundant_work_for_all_media(
        self,
        mock_schedule_tab_refresh,
        mock_has_fresh_tab_cache,
    ):
        tab_cache.warm_sibling_tabs(
            self.user,
            tab_cache.ALL_MEDIA_KEY,
            show_more=False,
        )

        mock_has_fresh_tab_cache.assert_not_called()
        mock_schedule_tab_refresh.assert_not_called()

    def test_apply_cached_action_removes_item_and_promotes_reserve(self):
        tab_cache.set_tab_cache(
            self.user.id,
            "movie",
            [self._row(title="Remove Me", reserve_title="Promoted Movie")],
        )

        rows = tab_cache.apply_cached_action(
            self.user.id,
            "movie",
            "movie",
            media_id="101",
            source="tmdb",
            show_more=False,
        )

        self.assertIsNotNone(rows)
        self.assertEqual(rows[0].items[0].title, "Promoted Movie")
        self.assertEqual(rows[0].reserve_items, [])

    @patch("app.discover.service.hydrate_visible_row_artwork")
    def test_apply_cached_action_hydrates_promoted_reserve_artwork(
        self,
        mock_hydrate_visible_row_artwork,
    ):
        row = self._row(title="Remove Me", reserve_title="Promoted Movie")
        row.key = "all_time_greats_unseen"
        row.source = "provider"
        row.reserve_items[0].image = settings.IMG_NONE

        def hydrate(row_to_hydrate, *, allow_remote=True):
            self.assertFalse(allow_remote)
            row_to_hydrate.items[0].image = "https://example.com/hydrated.jpg"

        mock_hydrate_visible_row_artwork.side_effect = hydrate
        tab_cache.set_tab_cache(self.user.id, "movie", [row])

        rows = tab_cache.apply_cached_action(
            self.user.id,
            "movie",
            "movie",
            media_id="101",
            source="tmdb",
            show_more=False,
        )

        self.assertIsNotNone(rows)
        self.assertEqual(rows[0].items[0].title, "Promoted Movie")
        self.assertEqual(rows[0].items[0].image, "https://example.com/hydrated.jpg")
        mock_hydrate_visible_row_artwork.assert_called_once()

    @patch("app.discover.service.hydrate_visible_row_artwork")
    def test_apply_cached_action_skips_hydration_for_non_active_tabs(
        self,
        mock_hydrate_visible_row_artwork,
    ):
        movie_row = self._row(title="Remove Me", reserve_title="Movie Replacement")
        movie_row.reserve_items[0].image = settings.IMG_NONE
        all_row = RowResult(
            key="trending_right_now",
            title="Trending",
            mission="Global picks",
            why="Popular now.",
            source="trakt",
            items=[
                CandidateItem(
                    media_type="movie",
                    source="tmdb",
                    media_id="101",
                    title="Remove Me",
                    image="https://example.com/poster.jpg",
                ),
            ],
            reserve_items=[
                CandidateItem(
                    media_type="movie",
                    source="tmdb",
                    media_id="103",
                    title="All Replacement",
                    image=settings.IMG_NONE,
                ),
            ],
        )
        tab_cache.set_tab_cache(self.user.id, "movie", [movie_row])
        tab_cache.set_tab_cache(self.user.id, "all", [all_row])

        rows = tab_cache.apply_cached_action(
            self.user.id,
            "movie",
            "movie",
            media_id="101",
            source="tmdb",
            show_more=False,
        )

        self.assertIsNotNone(rows)
        self.assertEqual(rows[0].items[0].title, "Movie Replacement")
        mock_hydrate_visible_row_artwork.assert_called_once()

    def test_store_and_restore_undo_snapshot_restores_prior_rows(self):
        original_rows = [self._row(title="Before Undo", reserve_title="Undo Reserve")]
        tab_cache.set_tab_cache(self.user.id, "movie", original_rows)
        token = tab_cache.store_undo_snapshot(
            self.user.id,
            action="dismiss",
            active_media_type="movie",
            candidate_media_type="movie",
            show_more=False,
            side_effect={"kind": "dismiss", "feedback_id": 1},
        )
        tab_cache.set_tab_cache(
            self.user.id,
            "movie",
            [self._row(title="After Action", reserve_title="Changed Reserve")],
            optimistic_refreshing=True,
        )

        restored = tab_cache.restore_undo_snapshot(self.user.id, token)

        self.assertIsNotNone(restored)
        self.assertEqual(restored["rows"][0].items[0].title, "Before Undo")
        self.assertEqual(restored["rows"][0].reserve_items[0].title, "Undo Reserve")

    def test_mark_active_from_request_reads_discover_next_url(self):
        request = self.factory.post(
            "/lists/recommend/",
            {"next": "/discover?media_type=movie&show_more=1"},
        )
        request.user = self.user

        context = tab_cache.mark_active_from_request(request)

        self.assertIsNotNone(context)
        self.assertEqual(context.media_type, "movie")
        self.assertTrue(context.show_more)
        active = tab_cache.get_active_context(self.user.id)
        self.assertEqual(active.media_type, "movie")
        self.assertTrue(active.show_more)

    @patch("app.discover.service.get_discover_rows", return_value=[])
    def test_refresh_tab_cache_clears_provider_cache_for_manual_refresh(
        self, mock_get_discover_rows
    ):
        now = timezone.now()
        DiscoverApiCache.objects.create(
            provider="trakt",
            endpoint="/movies/anticipated",
            params_hash="hash-trakt",
            payload={"results": []},
            expires_at=now + timedelta(hours=1),
        )
        DiscoverApiCache.objects.create(
            provider="tmdb",
            endpoint="/trending/movie/day",
            params_hash="hash-tmdb",
            payload={"results": []},
            expires_at=now + timedelta(hours=1),
        )

        tab_cache.refresh_tab_cache(
            self.user,
            "movie",
            clear_provider_cache=True,
        )

        self.assertFalse(DiscoverApiCache.objects.filter(provider="trakt").exists())
        self.assertTrue(DiscoverApiCache.objects.filter(provider="tmdb").exists())
        mock_get_discover_rows.assert_called_once_with(
            self.user,
            "movie",
            show_more=False,
            include_debug=False,
            defer_artwork=False,
        )

    @patch("app.discover.service.get_discover_rows", return_value=[])
    def test_refresh_tab_cache_force_preserves_taste_profile(self, mock_get_discover_rows):
        expires_at = timezone.now() + timedelta(hours=1)
        DiscoverRowCache.objects.create(
            user=self.user,
            media_type="movie",
            row_key="top_picks_for_you",
            payload={},
            expires_at=expires_at,
        )
        DiscoverTasteProfile.objects.create(
            user=self.user,
            media_type="movie",
            expires_at=expires_at,
        )

        tab_cache.refresh_tab_cache(
            self.user,
            "movie",
            force=True,
        )

        self.assertFalse(
            DiscoverRowCache.objects.filter(
                user=self.user,
                media_type="movie",
            ).exists(),
        )
        self.assertTrue(
            DiscoverTasteProfile.objects.filter(
                user=self.user,
                media_type="movie",
            ).exists(),
        )
        mock_get_discover_rows.assert_called_once_with(
            self.user,
            "movie",
            show_more=False,
            include_debug=False,
            defer_artwork=False,
        )

    @patch("app.discover.tab_cache.schedule_tab_refresh", return_value=True)
    @patch("app.discover.service.get_discover_rows")
    def test_refresh_tab_cache_skips_superseded_payloads(
        self,
        mock_get_discover_rows,
        mock_schedule_tab_refresh,
    ):
        starting_version = tab_cache.get_activity_version(self.user.id, "movie")
        tab_cache.set_tab_cache(
            self.user.id,
            "movie",
            [self._row(title="Optimistic Movie")],
            activity_version=starting_version,
            optimistic_refreshing=True,
        )

        def build_rows(*args, **kwargs):
            tab_cache.bump_activity_version(self.user.id, "movie")
            return [self._row(title="Superseded Movie")]

        mock_get_discover_rows.side_effect = build_rows

        rows = tab_cache.refresh_tab_cache(self.user, "movie")

        payload, is_stale = tab_cache.get_tab_cache(self.user.id, "movie", show_more=False)
        self.assertEqual(rows[0].items[0].title, "Superseded Movie")
        self.assertEqual(payload["rows"][0]["items"][0]["title"], "Optimistic Movie")
        self.assertTrue(is_stale)
        mock_schedule_tab_refresh.assert_called_once_with(
            self.user.id,
            "movie",
            show_more=False,
            debounce_seconds=tab_cache.DISCOVER_PRIORITY_REFRESH_DEBOUNCE_SECONDS,
            countdown=tab_cache.DISCOVER_PRIORITY_REFRESH_COUNTDOWN,
            force=False,
            clear_provider_cache=False,
        )

    @patch("app.discover.tab_cache.schedule_tab_refresh", return_value=True)
    def test_invalidate_for_media_change_bumps_versions_and_clears_lower_caches(
        self, mock_schedule
    ):
        expires_at = timezone.now() + timedelta(hours=1)
        for media_type in ("movie", "all"):
            DiscoverRowCache.objects.create(
                user=self.user,
                media_type=media_type,
                row_key="top_picks_for_you",
                payload={},
                expires_at=expires_at,
            )
            DiscoverTasteProfile.objects.create(
                user=self.user,
                media_type=media_type,
                expires_at=expires_at,
            )

        movie_version = tab_cache._get_activity_version(self.user.id, "movie")
        all_version = tab_cache._get_activity_version(self.user.id, "all")

        targets = tab_cache.invalidate_for_media_change(self.user.id, "movie")

        self.assertEqual(targets, ["movie", "all"])
        self.assertFalse(
            DiscoverRowCache.objects.filter(
                user=self.user, media_type__in=["movie", "all"]
            ).exists(),
        )
        self.assertFalse(
            DiscoverTasteProfile.objects.filter(
                user=self.user, media_type__in=["movie", "all"]
            ).exists(),
        )
        self.assertNotEqual(
            movie_version, tab_cache._get_activity_version(self.user.id, "movie")
        )
        self.assertNotEqual(
            all_version, tab_cache._get_activity_version(self.user.id, "all")
        )
        self.assertEqual(mock_schedule.call_count, 2)

    @patch("app.discover.tab_cache.schedule_tab_refresh", return_value=True)
    def test_schedule_user_tab_warmup_prioritizes_all_then_enabled_tabs(
        self, mock_schedule
    ):
        with patch.object(
            self.user,
            "get_enabled_media_types",
            return_value=["movie", "tv"],
        ):
            scheduled = tab_cache.schedule_user_tab_warmup(self.user)

        self.assertEqual(scheduled, 3)
        mock_schedule.assert_has_calls(
            [
                call(
                    self.user.id,
                    "all",
                    show_more=False,
                    debounce_seconds=tab_cache.DISCOVER_PRIORITY_REFRESH_DEBOUNCE_SECONDS,
                    countdown=tab_cache.DISCOVER_PRIORITY_REFRESH_COUNTDOWN,
                ),
                call(
                    self.user.id,
                    "movie",
                    show_more=False,
                    debounce_seconds=tab_cache.DISCOVER_WARM_SIBLING_DEBOUNCE_SECONDS,
                    countdown=tab_cache.DISCOVER_WARM_SIBLING_COUNTDOWN + 1,
                ),
                call(
                    self.user.id,
                    "tv",
                    show_more=False,
                    debounce_seconds=tab_cache.DISCOVER_WARM_SIBLING_DEBOUNCE_SECONDS,
                    countdown=tab_cache.DISCOVER_WARM_SIBLING_COUNTDOWN + 2,
                ),
            ],
        )

    @patch("app.discover.tab_cache.schedule_user_tab_warmup", return_value=2)
    def test_maybe_schedule_user_warmup_is_throttled(self, mock_schedule_warmup):
        first = tab_cache.maybe_schedule_user_warmup(self.user, throttle_seconds=60)
        second = tab_cache.maybe_schedule_user_warmup(self.user, throttle_seconds=60)

        self.assertEqual(first, 2)
        self.assertEqual(second, 0)
        mock_schedule_warmup.assert_called_once_with(
            self.user,
            media_types=["all"],
            prioritize_media_type="all",
            show_more=False,
        )
