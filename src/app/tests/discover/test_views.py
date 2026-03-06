# ruff: noqa: D102

from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.discover.schemas import CandidateItem, RowResult
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
        self.client.login(**self.credentials)

    def _row(
        self,
        *,
        final_score=None,
        release_date=None,
        row_key="top_picks_for_you",
        source="local",
    ):
        candidate = CandidateItem(
            media_type="movie",
            source="tmdb",
            media_id="9999",
            title="Match Test Movie",
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
        mock_get_tab_rows.assert_called_once_with(
            self.user,
            "all",
            show_more=False,
            include_debug=False,
            defer_artwork=True,
            allow_inline_bootstrap=False,
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
    @patch("app.views.discover_tab_cache.get_tab_rows")
    def test_discover_page_skips_sibling_warmup_in_debug_mode(
        self,
        mock_get_tab_rows,
        mock_warm_sibling_tabs,
        mock_get_tab_status,
    ):
        mock_get_tab_rows.return_value = []

        response = self.client.get(
            reverse("discover"),
            {"media_type": "movie", "show_more": "1", "discover_debug": "1"},
        )

        self.assertEqual(response.status_code, 200)
        mock_get_tab_rows.assert_called_once_with(
            self.user,
            "movie",
            show_more=True,
            include_debug=True,
            defer_artwork=True,
            allow_inline_bootstrap=False,
        )
        mock_get_tab_status.assert_not_called()
        mock_warm_sibling_tabs.assert_not_called()

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
            self.user,
            "movie",
            show_more=True,
            include_debug=False,
            defer_artwork=True,
            allow_inline_bootstrap=False,
        )
        mock_get_tab_status.assert_called_once_with(
            self.user.id,
            "movie",
            show_more=True,
        )
        self.assertContains(response, "Building Discover rows")

    @patch("app.views.discover_tab_cache.schedule_tab_refresh")
    @patch("app.views.discover_tab_cache.clear_lower_level_cache")
    @patch("app.views.discover_tab_cache.bump_activity_version")
    @patch("app.views.discover_tab_cache.mark_active")
    def test_refresh_discover_schedules_active_and_all_tabs(
        self,
        mock_mark_active,
        mock_bump_activity_version,
        mock_clear_lower_level_cache,
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
                "targets": ["movie", "all"],
            },
        )
        mock_mark_active.assert_called_once_with(
            self.user.id,
            "movie",
            show_more=True,
        )
        self.assertEqual(mock_bump_activity_version.call_count, 2)
        self.assertEqual(mock_clear_lower_level_cache.call_count, 2)
        mock_schedule_tab_refresh.assert_has_calls(
            [
                call(
                    self.user.id,
                    "movie",
                    show_more=True,
                    debounce_seconds=0,
                    countdown=0,
                    force=True,
                    clear_provider_cache=True,
                ),
                call(
                    self.user.id,
                    "all",
                    show_more=True,
                    debounce_seconds=0,
                    countdown=0,
                    force=True,
                    clear_provider_cache=True,
                ),
            ],
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
                row_key="coming_soon",
                source="trakt",
            ),
        ]
        mock_get_tab_status.return_value = {"is_refreshing": False}

        response = self.client.get(reverse("discover"), {"media_type": "movie"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2026-03-04")
        self.assertNotContains(response, "% match")
