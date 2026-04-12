# ruff: noqa: D102, S106

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.tasks import refresh_discover_rows, refresh_discover_tab_cache


class DiscoverTaskTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="discover-task-user",
            password="secret123",
        )

    @patch("app.discover.service.refresh_rows_for_user")
    @patch("app.discover.tab_cache.refresh_tab_cache")
    def test_refresh_discover_rows_skips_disabled_media_type(
        self,
        mock_refresh_tab_cache,
        mock_refresh_rows_for_user,
    ):
        self.user.comic_enabled = False
        self.user.save(update_fields=["comic_enabled"])

        result = refresh_discover_rows(self.user.id, "comic", ["coming_soon"])

        self.assertEqual(
            result,
            {
                "refreshed": 0,
                "reason": "disabled_media_type",
                "user_id": self.user.id,
            },
        )
        mock_refresh_rows_for_user.assert_not_called()
        mock_refresh_tab_cache.assert_not_called()

    @patch("app.discover.tab_cache.refresh_tab_cache")
    def test_refresh_discover_tab_cache_skips_disabled_media_type(
        self,
        mock_refresh_tab_cache,
    ):
        self.user.comic_enabled = False
        self.user.save(update_fields=["comic_enabled"])

        result = refresh_discover_tab_cache(self.user.id, "comic")

        self.assertEqual(
            result,
            {
                "refreshed": False,
                "reason": "disabled_media_type",
                "user_id": self.user.id,
            },
        )
        mock_refresh_tab_cache.assert_not_called()
