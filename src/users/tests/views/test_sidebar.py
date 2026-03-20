from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from unittest.mock import patch

from app.models import MediaTypes
from users.models import AnimeLibraryModeChoices, MetadataSourceDefaultChoices


class SidebarViewTests(TestCase):
    """Tests for the sidebar view."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "testuser", "password": "testpass123"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_preferences_get(self):
        """Test GET request to preferences view."""
        response = self.client.get(reverse("preferences"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "users/preferences.html")

        self.assertIn("media_types", response.context)
        self.assertIn(MediaTypes.TV.value, response.context["media_types"])
        self.assertIn(MediaTypes.MOVIE.value, response.context["media_types"])
        self.assertNotIn(MediaTypes.EPISODE.value, response.context["media_types"])

    def test_sidebar_post_update_preferences(self):
        """Test POST request to update preferences."""
        self.user.tv_enabled = True
        self.user.movie_enabled = True
        self.user.anime_enabled = True
        self.user.save()

        response = self.client.post(
            reverse("preferences"),
            {
                "media_types_checkboxes": [MediaTypes.TV.value, MediaTypes.ANIME.value],
            },
        )
        self.assertRedirects(response, reverse("preferences"))

        self.user.refresh_from_db()
        self.assertTrue(self.user.tv_enabled)
        self.assertFalse(self.user.movie_enabled)
        self.assertTrue(self.user.anime_enabled)

        messages = list(get_messages(response.wsgi_request))
        self.assertEqual(len(messages), 1)
        self.assertIn("Settings updated", str(messages[0]))

    def test_sidebar_post_demo_user(self):
        """Test POST request from a demo user to preferences."""
        self.user.is_demo = True
        self.user.tv_enabled = True
        self.user.movie_enabled = False
        self.user.save()

        response = self.client.post(
            reverse("preferences"),
            {
                "media_types_checkboxes": [MediaTypes.TV.value, MediaTypes.MOVIE.value],
            },
        )
        self.assertRedirects(response, reverse("preferences"))

        self.user.refresh_from_db()
        self.assertTrue(self.user.tv_enabled)
        self.assertFalse(self.user.movie_enabled)

        messages = list(get_messages(response.wsgi_request))
        self.assertEqual(len(messages), 1)
        self.assertIn("view-only for demo accounts", str(messages[0]))

    @override_settings(TVDB_API_KEY="")
    @patch("users.views.tmdb.watch_provider_regions")
    def test_preferences_get_hides_tvdb_when_not_configured(self, mock_watch_provider_regions):
        """TVDB preference controls should stay disabled until credentials exist."""
        mock_watch_provider_regions.return_value = [("UNSET", "Not set")]

        response = self.client.get(reverse("preferences"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["tvdb_enabled"])
        self.assertNotContains(response, "TheTVDB</option>", html=False)
        self.assertContains(response, "TVDB unavailable until")

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("users.views.tmdb.watch_provider_regions")
    def test_preferences_post_updates_metadata_provider_defaults(self, mock_watch_provider_regions):
        """Preferences POST should persist metadata provider defaults and library mode."""
        mock_watch_provider_regions.return_value = [("UNSET", "Not set")]

        response = self.client.post(
            reverse("preferences"),
            {
                "date_format": self.user.date_format,
                "time_format": self.user.time_format,
                "activity_history_view": self.user.activity_history_view,
                "game_logging_style": self.user.game_logging_style,
                "mobile_grid_layout": self.user.mobile_grid_layout,
                "media_card_subtitle_display": self.user.media_card_subtitle_display,
                "title_display_preference": self.user.title_display_preference,
                "top_talent_sort_by": self.user.top_talent_sort_by,
                "rating_scale": self.user.rating_scale,
                "progress_bar": "1" if self.user.progress_bar else "0",
                "hide_completed_recommendations": "1" if self.user.hide_completed_recommendations else "0",
                "hide_zero_rating": "1" if self.user.hide_zero_rating else "0",
                "quick_season_update_mobile": "1" if self.user.quick_season_update_mobile else "0",
                "book_comic_manga_progress_percentage": "1" if self.user.book_comic_manga_progress_percentage else "0",
                "show_planned_on_home": self.user.show_planned_on_home,
                "auto_pause_enabled": "1" if self.user.auto_pause_in_progress_enabled else "0",
                "auto_pause_rules": "[]",
                "watch_provider_region": "UNSET",
                "tv_metadata_source_default": MetadataSourceDefaultChoices.TVDB,
                "anime_metadata_source_default": MetadataSourceDefaultChoices.TMDB,
                "anime_library_mode": AnimeLibraryModeChoices.BOTH,
            },
        )

        self.assertRedirects(response, reverse("preferences"))

        self.user.refresh_from_db()
        self.assertEqual(self.user.tv_metadata_source_default, MetadataSourceDefaultChoices.TVDB)
        self.assertEqual(self.user.anime_metadata_source_default, MetadataSourceDefaultChoices.TMDB)
        self.assertEqual(self.user.anime_library_mode, AnimeLibraryModeChoices.BOTH)
