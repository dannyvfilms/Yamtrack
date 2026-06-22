# ruff: noqa: D102

import json
from datetime import UTC, datetime
from unittest.mock import ANY, call, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.discover.schemas import CandidateItem, RowResult
from app.models import (
    TV,
    DiscoverFeedback,
    DiscoverFeedbackType,
    Item,
    MediaTypes,
    Movie,
    Sources,
    Status,
)
from app.services.tracking_hydration import HydratedItemResult
from users.models import DateFormatChoices


class DiscoverViewTests(TestCase):
    """Tests for Discover page endpoints and cache-status wiring."""

    def setUp(self):
        self.credentials = {
            "username": "discover-view-user",
            "password": "secret123",
        }
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])
        self.warmup_patcher = patch(
            "app.middleware.discover_tab_cache.maybe_schedule_user_warmup",
            return_value=0,
        )
        self.warmup_patcher.start()
        self.client.login(**self.credentials)

    def tearDown(self):
        self.warmup_patcher.stop()

    def _row(
        self,
        *,
        title="Match Test Movie",
        final_score=None,
        release_date=None,
        row_key="top_picks_for_you",
        source="local",
    ):
        candidate = CandidateItem(
            media_type="movie",
            source="tmdb",
            media_id="9999",
            title=title,
            image="https://example.com/9999.jpg",
            release_date=release_date,
            final_score=final_score,
        )
        return RowResult(
            key=row_key,
            title=(
                "Top Picks For You"
                if row_key == "top_picks_for_you"
                else "Coming Soon"
            ),
            mission=(
                "Personal Taste Match"
                if row_key == "top_picks_for_you"
                else "Anticipation"
            ),
            why=(
                "Tailored picks."
                if row_key == "top_picks_for_you"
                else "Upcoming releases."
            ),
            source=source,
            items=[candidate],
        )

    def _movie_item(self, media_id="9001", title="Action Movie"):
        return Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=title,
            image="https://example.com/movie.jpg",
        )

    @patch("app.views._invalidate_discover_after_action")
    def test_discover_toggle_hidden_hides_item_and_renders_save_control(
        self,
        mock_invalidate_discover_after_action,
    ):
        item = self._movie_item()

        response = self.client.post(
            reverse("discover_toggle_hidden"),
            {
                "item_id": item.id,
                "action": "hide",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            DiscoverFeedback.objects.filter(
                user=self.user,
                item=item,
                feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
            ).exists(),
        )
        self.assertContains(response, "Currently hidden in Discover.")
        self.assertContains(response, 'hx-post="/discover/toggle-hidden"', html=False)
        self.assertContains(response, 'name="action"', html=False)
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["discoverActionComplete"]["action"], "hide")
        mock_invalidate_discover_after_action.assert_called_once_with(
            self.user.id,
            MediaTypes.MOVIE.value,
            discover_debug=False,
            feedback_change=True,
        )

    def test_discover_toggle_hidden_rejects_invalid_action(self):
        item = self._movie_item()

        response = self.client.post(
            reverse("discover_toggle_hidden"),
            {
                "item_id": item.id,
                "action": "maybe",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            DiscoverFeedback.objects.filter(
                user=self.user,
                item=item,
                feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
            ).exists(),
        )

    @patch("app.views.discover_tab_cache.get_tab_status")
    @patch("app.views.discover_tab_cache.warm_sibling_tabs")
    @patch("app.views.discover_tab_cache.get_tab_rows")
    def test_discover_page_hidden_view_lists_items_by_recent_change(
        self,
        mock_get_tab_rows,
        mock_warm_sibling_tabs,
        mock_get_tab_status,
    ):
        older_item = self._movie_item(media_id="9002", title="Older Hidden")
        newer_item = self._movie_item(media_id="9003", title="Newer Hidden")
        older_feedback = DiscoverFeedback.objects.create(
            user=self.user,
            item=older_item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )
        newer_feedback = DiscoverFeedback.objects.create(
            user=self.user,
            item=newer_item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )
        DiscoverFeedback.objects.filter(pk=older_feedback.pk).update(
            updated_at=datetime(2026, 4, 14, 12, 0, tzinfo=UTC),
        )
        DiscoverFeedback.objects.filter(pk=newer_feedback.pk).update(
            updated_at=datetime(2026, 4, 15, 12, 0, tzinfo=UTC),
        )

        response = self.client.get(
            reverse("discover"),
            {
                "media_type": "hidden",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_media_type"], "hidden")
        self.assertEqual(response.context["hidden_discover_count"], 2)
        self.assertEqual(
            [entry.item.title for entry in response.context["hidden_discover_entries"]],
            ["Newer Hidden", "Older Hidden"],
        )
        self.assertContains(response, "Hidden From Discover")
        self.assertTrue(
            any(
                option["value"] == "hidden"
                for option in response.context["discover_media_options"]
            ),
        )
        mock_get_tab_rows.assert_not_called()
        mock_get_tab_status.assert_not_called()
        mock_warm_sibling_tabs.assert_not_called()

    @patch("app.views.discover_tab_cache.get_tab_status")
    @patch("app.views.discover_tab_cache.get_tab_rows")
    def test_discover_rows_hidden_view_returns_hidden_fragment_without_cache(
        self,
        mock_get_tab_rows,
        mock_get_tab_status,
    ):
        hidden_item = self._movie_item(media_id="9005", title="Hidden Fragment Item")
        DiscoverFeedback.objects.create(
            user=self.user,
            item=hidden_item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )

        response = self.client.get(
            reverse("discover_rows"),
            {
                "media_type": "hidden",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Discover-Media-Type"], "hidden")
        self.assertEqual(response["X-Discover-Show-More"], "0")
        self.assertNotIn("X-Discover-Activity-Version", response)
        self.assertContains(response, "Hidden From Discover")
        self.assertContains(response, "Hidden Fragment Item")
        mock_get_tab_rows.assert_not_called()
        mock_get_tab_status.assert_not_called()

    @patch("app.views.discover_tab_cache.get_tab_status")
    @patch("app.views.discover_tab_cache.warm_sibling_tabs")
    @patch("app.views.discover_tab_cache.get_tab_rows")
    def test_discover_page_uses_tab_cache(
        self,
        mock_get_tab_rows,
        mock_warm_sibling_tabs,
        mock_get_tab_status,
    ):
        mock_get_tab_rows.return_value = []
        mock_get_tab_status.return_value = {"is_refreshing": True}

        response = self.client.get(reverse("discover"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/discover.html")
        self.assertIn("discover_media_options", response.context)
        self.assertTrue(response.context["discover_loading"])
        self.assertContains(response, "Refreshing recommendations in background")
        self.assertContains(response, "js/cache-updater.js")
        mock_get_tab_rows.assert_called_once_with(
            ANY,
            "all",
            show_more=False,
            include_debug=False,
            defer_artwork=False,
            allow_inline_bootstrap=True,
        )
        mock_get_tab_status.assert_called_once_with(
            self.user.id,
            "all",
            show_more=False,
        )
        mock_warm_sibling_tabs.assert_called_once_with(
            self.user,
            "all",
            show_more=False,
        )

    @patch("app.views.discover_tab_cache.get_tab_status")
    @patch("app.views.discover_tab_cache.warm_sibling_tabs")
    @patch("app.views.discover.get_discover_rows")
    def test_discover_page_skips_sibling_warmup_in_debug_mode(
        self,
        mock_get_discover_rows,
        mock_warm_sibling_tabs,
        mock_get_tab_status,
    ):
        mock_get_discover_rows.return_value = []

        response = self.client.get(
            reverse("discover"),
            {"media_type": "movie", "show_more": "1", "discover_debug": "1"},
        )

        self.assertEqual(response.status_code, 200)
        mock_get_discover_rows.assert_called_once_with(
            ANY,
            "movie",
            show_more=True,
            include_debug=True,
            defer_artwork=False,
        )
        mock_get_tab_status.assert_not_called()
        mock_warm_sibling_tabs.assert_not_called()

    @patch("app.views.discover_tab_cache.get_tab_status")
    @patch("app.views.discover_tab_cache.warm_sibling_tabs")
    @patch("app.views.discover_tab_cache.get_tab_rows")
    def test_discover_page_falls_back_to_all_for_disabled_media_type(
        self,
        mock_get_tab_rows,
        mock_warm_sibling_tabs,
        mock_get_tab_status,
    ):
        self.user.comic_enabled = False
        self.user.save(update_fields=["comic_enabled"])
        mock_get_tab_rows.return_value = []
        mock_get_tab_status.return_value = {"is_refreshing": False}

        response = self.client.get(reverse("discover"), {"media_type": "comic"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_media_type"], "all")
        mock_get_tab_rows.assert_called_once_with(
            ANY,
            "all",
            show_more=False,
            include_debug=False,
            defer_artwork=False,
            allow_inline_bootstrap=True,
        )
        mock_warm_sibling_tabs.assert_called_once_with(
            self.user,
            "all",
            show_more=False,
        )

    @patch("app.views.discover_tab_cache.get_tab_status")
    @patch("app.views.discover_tab_cache.get_tab_rows")
    def test_discover_rows_uses_tab_cache(
        self,
        mock_get_tab_rows,
        mock_get_tab_status,
    ):
        mock_get_tab_rows.return_value = []
        mock_get_tab_status.return_value = {"is_refreshing": True}

        response = self.client.get(
            reverse("discover_rows"),
            {"media_type": "movie", "show_more": "1"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/discover_rows.html")
        mock_get_tab_rows.assert_called_once_with(
            ANY,
            "movie",
            show_more=True,
            include_debug=False,
            defer_artwork=False,
            allow_inline_bootstrap=True,
        )
        mock_get_tab_status.assert_called_once_with(
            self.user.id,
            "movie",
            show_more=True,
        )
        self.assertContains(response, "Building Discover rows")

    @patch("app.views.discover_tab_cache.schedule_tab_refresh")
    @patch("app.views.discover_tab_cache.clear_row_cache")
    @patch("app.views.discover_tab_cache.bump_activity_version")
    @patch("app.views.discover_tab_cache.mark_active")
    def test_refresh_discover_schedules_only_active_tab(
        self,
        mock_mark_active,
        mock_bump_activity_version,
        mock_clear_row_cache,
        mock_schedule_tab_refresh,
    ):
        response = self.client.post(
            reverse("refresh_discover"),
            {"media_type": "movie", "show_more": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "media_type": "movie",
                "show_more": True,
                "targets": ["movie"],
            },
        )
        mock_mark_active.assert_called_once_with(
            self.user.id,
            "movie",
            show_more=True,
        )
        mock_bump_activity_version.assert_called_once_with(
            self.user.id,
            "movie",
        )
        mock_clear_row_cache.assert_called_once_with(
            self.user.id,
            "movie",
        )
        mock_schedule_tab_refresh.assert_called_once_with(
            self.user.id,
            "movie",
            show_more=True,
            debounce_seconds=0,
            countdown=0,
            force=True,
            clear_provider_cache=True,
        )

    @patch("app.views.discover_tab_cache.schedule_tab_refresh")
    @patch("app.views.discover_tab_cache.clear_row_cache")
    @patch("app.views.discover_tab_cache.bump_activity_version")
    @patch("app.views.discover_tab_cache.mark_active")
    def test_refresh_discover_falls_back_to_all_for_disabled_media_type(
        self,
        mock_mark_active,
        mock_bump_activity_version,
        mock_clear_row_cache,
        mock_schedule_tab_refresh,
    ):
        self.user.comic_enabled = False
        self.user.save(update_fields=["comic_enabled"])

        response = self.client.post(
            reverse("refresh_discover"),
            {"media_type": "comic", "show_more": "0"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "ok": True,
                "media_type": "all",
                "show_more": False,
                "targets": ["all"],
            },
        )
        mock_mark_active.assert_called_once_with(
            self.user.id,
            "all",
            show_more=False,
        )
        mock_bump_activity_version.assert_called_once_with(self.user.id, "all")
        mock_clear_row_cache.assert_called_once_with(self.user.id, "all")
        mock_schedule_tab_refresh.assert_called_once_with(
            self.user.id,
            "all",
            show_more=False,
            debounce_seconds=0,
            countdown=0,
            force=True,
            clear_provider_cache=True,
        )

    @patch("app.views.discover_tab_cache.get_tab_status")
    def test_cache_status_delegates_to_discover_tab_cache(
        self,
        mock_get_tab_status,
    ):
        mock_get_tab_status.return_value = {
            "exists": True,
            "built_at": "2026-03-06T12:00:00+00:00",
            "is_stale": False,
            "is_refreshing": False,
            "recently_built": True,
            "refresh_scheduled": False,
        }

        response = self.client.get(
            reverse("cache_status"),
            {"cache_type": "discover", "media_type": "movie", "show_more": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), mock_get_tab_status.return_value)
        mock_get_tab_status.assert_called_once_with(
            self.user.id,
            "movie",
            show_more=True,
        )

    @patch("app.views.discover_tab_cache.get_tab_status")
    @patch("app.views.discover_tab_cache.warm_sibling_tabs")
    @patch("app.views.discover_tab_cache.get_tab_rows")
    def test_discover_page_renders_date_and_score_subtitle(
        self,
        mock_get_tab_rows,
        mock_warm_sibling_tabs,
        mock_get_tab_status,
    ):
        mock_get_tab_rows.return_value = [
            self._row(final_score=0.912, release_date="2026-03-04"),
        ]
        mock_get_tab_status.return_value = {"is_refreshing": False}

        response = self.client.get(reverse("discover"), {"media_type": "movie"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "91% Taste match")
        self.assertContains(response, "2026-03-04")
        mock_warm_sibling_tabs.assert_called_once()

    @patch("app.views.discover_tab_cache.get_tab_status")
    @patch("app.views.discover_tab_cache.warm_sibling_tabs")
    @patch("app.views.discover_tab_cache.get_tab_rows")
    def test_discover_page_renders_date_only_subtitle_when_score_missing(
        self,
        mock_get_tab_rows,
        _mock_warm_sibling_tabs,
        mock_get_tab_status,
    ):
        mock_get_tab_rows.return_value = [
            self._row(
                release_date="2026-03-04",
                row_key="trending_right_now",
                source="trakt",
            ),
        ]
        mock_get_tab_status.return_value = {"is_refreshing": False}

        response = self.client.get(reverse("discover"), {"media_type": "movie"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2026-03-04")
        self.assertNotContains(response, "% match")

    @patch("app.views.discover_tab_cache.get_tab_status")
    @patch("app.views.discover_tab_cache.warm_sibling_tabs")
    @patch("app.views.discover_tab_cache.get_tab_rows")
    def test_discover_page_renders_discover_quick_actions(
        self,
        mock_get_tab_rows,
        _mock_warm_sibling_tabs,
        mock_get_tab_status,
    ):
        mock_get_tab_rows.return_value = [self._row()]
        mock_get_tab_status.return_value = {"is_refreshing": False}

        response = self.client.get(reverse("discover"), {"media_type": "movie"})

        self.assertContains(response, "Add to Planning")
        self.assertContains(response, "Hide from Discover")
        self.assertNotContains(response, "View your activity history")

    @patch("app.models.Item.fetch_releases")
    @patch("app.views.discover_tab_cache.invalidate_for_media_change")
    @patch("app.views.discover_tab_cache.update_undo_snapshot")
    @patch("app.views.discover_tab_cache.apply_cached_action")
    @patch("app.views.discover_tab_cache.store_undo_snapshot", return_value="undo-123")
    @patch("app.views.ensure_item_metadata")
    def test_discover_action_planning_returns_rows_and_trigger(
        self,
        mock_ensure_item_metadata,
        _mock_store_undo_snapshot,
        mock_apply_cached_action,
        mock_update_undo_snapshot,
        mock_invalidate_for_media_change,
        _mock_fetch_releases,
    ):
        item = self._movie_item()
        mock_ensure_item_metadata.return_value = HydratedItemResult(
            item=item,
            metadata={},
            created=False,
        )
        mock_apply_cached_action.return_value = [self._row(title="Updated Movie")]

        response = self.client.post(
            reverse("discover_action"),
            {
                "action": "planning",
                "candidate_media_type": MediaTypes.MOVIE.value,
                "source": Sources.TMDB.value,
                "media_id": item.media_id,
                "active_media_type": MediaTypes.MOVIE.value,
                "show_more": "0",
                "row_key": "top_picks_for_you",
                "title": item.title,
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            Movie.objects.filter(
                user=self.user,
                item=item,
                status=Status.PLANNING.value,
            ).exists(),
        )
        self.assertEqual(response["X-Discover-Media-Type"], MediaTypes.MOVIE.value)
        self.assertEqual(response["X-Discover-Show-More"], "0")
        self.assertIn("X-Discover-Activity-Version", response)
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["discoverActionComplete"]["action"], "planning")
        self.assertEqual(trigger["discoverActionComplete"]["undo_token"], "undo-123")
        mock_update_undo_snapshot.assert_called_once()
        mock_invalidate_for_media_change.assert_called_once_with(
            self.user.id,
            MediaTypes.MOVIE.value,
        )

    @patch("app.models.Item.fetch_releases")
    @patch("app.views.discover_tab_cache.invalidate_for_media_change")
    @patch("app.views.discover_tab_cache.update_undo_snapshot")
    @patch("app.views.discover_tab_cache.apply_cached_action")
    @patch("app.views.discover_tab_cache.store_undo_snapshot", return_value="undo-tv")
    @patch("app.views.ensure_item_metadata")
    @patch("app.views.ensure_item_metadata_from_discover_seed")
    def test_discover_action_planning_tv_uses_local_seed_hydration(
        self,
        mock_ensure_item_metadata_from_discover_seed,
        mock_ensure_item_metadata,
        _mock_store_undo_snapshot,
        mock_apply_cached_action,
        mock_update_undo_snapshot,
        mock_invalidate_for_media_change,
        _mock_fetch_releases,
    ):
        item = Item.objects.create(
            media_id="tv-9001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Slow TV",
            image="https://example.com/tv.jpg",
        )
        mock_ensure_item_metadata_from_discover_seed.return_value = HydratedItemResult(
            item=item,
            metadata={},
            created=False,
        )
        mock_apply_cached_action.return_value = [self._row(title="Updated TV")]

        response = self.client.post(
            reverse("discover_action"),
            {
                "action": "planning",
                "candidate_media_type": MediaTypes.TV.value,
                "source": Sources.TMDB.value,
                "media_id": item.media_id,
                "active_media_type": MediaTypes.TV.value,
                "show_more": "0",
                "row_key": "all_time_greats_unseen",
                "title": item.title,
                "image": item.image,
                "release_date": "2026-01-01",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            TV.objects.filter(
                user=self.user,
                item=item,
                status=Status.PLANNING.value,
            ).exists(),
        )
        mock_ensure_item_metadata.assert_not_called()
        mock_ensure_item_metadata_from_discover_seed.assert_called_once_with(
            MediaTypes.TV.value,
            item.media_id,
            Sources.TMDB.value,
            None,
            identity_media_type=None,
            library_media_type=None,
            fallback_title=item.title,
            fallback_image=item.image,
            fallback_release_date="2026-01-01",
        )
        mock_update_undo_snapshot.assert_called_once()
        mock_invalidate_for_media_change.assert_called_once_with(
            self.user.id,
            MediaTypes.TV.value,
        )

    @patch("app.views.discover_tab_cache.update_undo_snapshot")
    @patch("app.views.discover_tab_cache.apply_cached_action")
    @patch("app.views.discover_tab_cache.invalidate_for_feedback_change")
    @patch("app.views.discover_tab_cache.store_undo_snapshot", return_value="undo-dismiss")
    @patch("app.views.ensure_item_metadata")
    def test_discover_action_dismiss_returns_rows_and_trigger(
        self,
        mock_ensure_item_metadata,
        _mock_store_undo_snapshot,
        mock_invalidate_for_feedback_change,
        mock_apply_cached_action,
        mock_update_undo_snapshot,
    ):
        item = self._movie_item(media_id="9002", title="Dismiss Me")
        mock_ensure_item_metadata.return_value = HydratedItemResult(
            item=item,
            metadata={},
            created=False,
        )
        mock_apply_cached_action.return_value = [self._row(title="Dismissed Replacement")]

        response = self.client.post(
            reverse("discover_action"),
            {
                "action": "dismiss",
                "candidate_media_type": MediaTypes.MOVIE.value,
                "source": Sources.TMDB.value,
                "media_id": item.media_id,
                "active_media_type": MediaTypes.MOVIE.value,
                "show_more": "0",
                "row_key": "top_picks_for_you",
                "title": item.title,
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            DiscoverFeedback.objects.filter(
                user=self.user,
                item=item,
                feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
            ).exists(),
        )
        self.assertEqual(response["X-Discover-Media-Type"], MediaTypes.MOVIE.value)
        self.assertEqual(response["X-Discover-Show-More"], "0")
        self.assertIn("X-Discover-Activity-Version", response)
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["discoverActionComplete"]["action"], "dismiss")
        self.assertEqual(trigger["discoverActionComplete"]["undo_token"], "undo-dismiss")
        mock_invalidate_for_feedback_change.assert_called_once_with(
            self.user.id,
            MediaTypes.MOVIE.value,
        )
        mock_update_undo_snapshot.assert_called_once()

    @patch("app.views.discover_tab_cache.restore_undo_snapshot")
    @patch("app.views.discover_tab_cache.get_undo_snapshot")
    @patch("app.views.discover_tab_cache.invalidate_for_feedback_change")
    def test_discover_action_undo_deletes_feedback_and_restores_rows(
        self,
        mock_invalidate_for_feedback_change,
        mock_get_undo_snapshot,
        mock_restore_undo_snapshot,
    ):
        item = self._movie_item(media_id="9003", title="Undo Movie")
        feedback = DiscoverFeedback.objects.create(
            user=self.user,
            item=item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )
        mock_get_undo_snapshot.return_value = {
            "side_effect": {
                "kind": "dismiss",
                "feedback_id": feedback.id,
            },
        }
        mock_restore_undo_snapshot.return_value = {"rows": [self._row(title="Restored Movie")]}

        response = self.client.post(
            reverse("discover_action"),
            {
                "action": "undo",
                "undo_token": "undo-dismiss",
                "active_media_type": MediaTypes.MOVIE.value,
                "show_more": "0",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            DiscoverFeedback.objects.filter(id=feedback.id).exists(),
        )
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["discoverActionComplete"]["action"], "undo")
        mock_invalidate_for_feedback_change.assert_called_once_with(
            self.user.id,
            MediaTypes.MOVIE.value,
        )

    @patch("app.views.discover.get_discover_rows")
    @patch("app.views.discover_tab_cache.clear_lower_level_cache")
    @patch("app.views.discover_tab_cache.bump_activity_version")
    @patch("app.views.discover_tab_cache.apply_cached_action")
    @patch("app.views.discover_tab_cache.invalidate_for_feedback_change")
    @patch("app.views.ensure_item_metadata")
    def test_discover_action_debug_bypasses_cache_fast_path(
        self,
        mock_ensure_item_metadata,
        mock_invalidate_for_feedback_change,
        mock_apply_cached_action,
        mock_bump_activity_version,
        mock_clear_lower_level_cache,
        mock_get_discover_rows,
    ):
        item = self._movie_item(media_id="9004", title="Debug Dismiss")
        mock_ensure_item_metadata.return_value = HydratedItemResult(
            item=item,
            metadata={},
            created=False,
        )
        mock_get_discover_rows.return_value = [self._row(title="Debug Replacement")]

        response = self.client.post(
            reverse("discover_action"),
            {
                "action": "dismiss",
                "candidate_media_type": MediaTypes.MOVIE.value,
                "source": Sources.TMDB.value,
                "media_id": item.media_id,
                "active_media_type": MediaTypes.MOVIE.value,
                "show_more": "0",
                "discover_debug": "1",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        mock_apply_cached_action.assert_not_called()
        mock_invalidate_for_feedback_change.assert_not_called()
        mock_bump_activity_version.assert_has_calls(
            [
                call(self.user.id, MediaTypes.MOVIE.value),
                call(self.user.id, "all"),
            ],
            any_order=False,
        )
        mock_clear_lower_level_cache.assert_has_calls(
            [
                call(self.user.id, MediaTypes.MOVIE.value),
                call(self.user.id, "all"),
            ],
            any_order=False,
        )
        mock_get_discover_rows.assert_called_once_with(
            ANY,
            MediaTypes.MOVIE.value,
            show_more=False,
            include_debug=True,
            defer_artwork=False,
        )

    @patch("app.models.Item.fetch_releases")
    @patch("app.views.discover_tab_cache.update_undo_snapshot")
    @patch("app.views.discover_tab_cache.store_undo_snapshot", return_value="undo-debug")
    @patch("app.views.discover.get_discover_rows")
    @patch("app.views.discover_tab_cache.clear_lower_level_cache")
    @patch("app.views.discover_tab_cache.bump_activity_version")
    @patch("app.views.discover_tab_cache.invalidate_for_media_change")
    @patch("app.views.discover_tab_cache.apply_cached_action")
    @patch("app.views.ensure_item_metadata")
    def test_discover_action_planning_debug_marks_discover_stale_without_enqueuing_refresh(
        self,
        mock_ensure_item_metadata,
        mock_apply_cached_action,
        mock_invalidate_for_media_change,
        mock_bump_activity_version,
        mock_clear_lower_level_cache,
        mock_get_discover_rows,
        _mock_store_undo_snapshot,
        mock_update_undo_snapshot,
        _mock_fetch_releases,
    ):
        item = self._movie_item(media_id="9005", title="Debug Planning")
        mock_ensure_item_metadata.return_value = HydratedItemResult(
            item=item,
            metadata={},
            created=False,
        )
        mock_get_discover_rows.return_value = [self._row(title="Debug Planning Replacement")]

        response = self.client.post(
            reverse("discover_action"),
            {
                "action": "planning",
                "candidate_media_type": MediaTypes.MOVIE.value,
                "source": Sources.TMDB.value,
                "media_id": item.media_id,
                "active_media_type": MediaTypes.MOVIE.value,
                "show_more": "0",
                "discover_debug": "1",
                "title": item.title,
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        mock_apply_cached_action.assert_not_called()
        mock_invalidate_for_media_change.assert_not_called()
        mock_bump_activity_version.assert_has_calls(
            [
                call(self.user.id, MediaTypes.MOVIE.value),
                call(self.user.id, "all"),
            ],
            any_order=False,
        )
        mock_clear_lower_level_cache.assert_has_calls(
            [
                call(self.user.id, MediaTypes.MOVIE.value),
                call(self.user.id, "all"),
            ],
            any_order=False,
        )
        mock_get_discover_rows.assert_called_once_with(
            ANY,
            MediaTypes.MOVIE.value,
            show_more=False,
            include_debug=True,
            defer_artwork=False,
        )
        mock_update_undo_snapshot.assert_called_once()
