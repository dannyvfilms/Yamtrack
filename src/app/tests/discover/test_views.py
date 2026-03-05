from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.discover.schemas import CandidateItem, DiscoverPayload, RowResult
from users.models import DateFormatChoices


class DiscoverViewTests(TestCase):
    """Tests for Discover page and rows endpoints."""

    def setUp(self):
        self.credentials = {"username": "discover-view-user", "password": "secret123"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])
        self.client.login(**self.credentials)

    @patch("app.views.discover.get_discover_payload")
    def test_discover_page_renders(self, mock_get_payload):
        mock_get_payload.return_value = DiscoverPayload(
            media_type="all",
            rows=[],
            show_more=False,
        )

        response = self.client.get(reverse("discover"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/discover.html")
        self.assertIn("discover_media_options", response.context)
        mock_get_payload.assert_called_once_with(self.user, "all", show_more=False, include_debug=False)

    @patch("app.views.discover.get_discover_payload")
    def test_discover_page_passes_debug_flag(self, mock_get_payload):
        mock_get_payload.return_value = DiscoverPayload(
            media_type="movie",
            rows=[],
            show_more=True,
        )

        response = self.client.get(
            reverse("discover"),
            {"media_type": "movie", "show_more": "1", "discover_debug": "1"},
        )

        self.assertEqual(response.status_code, 200)
        mock_get_payload.assert_called_once_with(self.user, "movie", show_more=True, include_debug=True)

    @patch("app.views.discover.get_discover_rows")
    def test_discover_rows_respects_query_params(self, mock_get_rows):
        mock_get_rows.return_value = []

        response = self.client.get(
            reverse("discover_rows"),
            {"media_type": "movie", "show_more": "1"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/discover_rows.html")
        mock_get_rows.assert_called_once_with(self.user, "movie", show_more=True, include_debug=False)

    @patch("app.views.discover.get_discover_rows")
    def test_discover_rows_passes_debug_flag(self, mock_get_rows):
        mock_get_rows.return_value = []

        response = self.client.get(
            reverse("discover_rows"),
            {"media_type": "movie", "show_more": "0", "discover_debug": "1"},
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        mock_get_rows.assert_called_once_with(self.user, "movie", show_more=False, include_debug=True)

    @patch("app.views.discover.get_discover_payload")
    def test_discover_page_renders_date_and_row_specific_score_label_for_new_rows(self, mock_get_payload):
        candidate = CandidateItem(
            media_type="movie",
            source="tmdb",
            media_id="9999",
            title="Match Test Movie",
            image="https://example.com/9999.jpg",
            release_date="2026-03-04",
            final_score=0.912,
        )
        mock_get_payload.return_value = DiscoverPayload(
            media_type="movie",
            rows=[
                RowResult(
                    key="top_picks_for_you",
                    title="Top Picks For You",
                    mission="Personal Taste Match",
                    why="New-to-you movies tailored to your taste.",
                    source="local",
                    items=[candidate],
                ),
            ],
            show_more=False,
        )

        response = self.client.get(reverse("discover"), {"media_type": "movie", "show_more": "0"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "91% Taste match")

    @patch("app.views.discover.get_discover_payload")
    def test_discover_page_renders_date_only_subtitle_when_match_missing(self, mock_get_payload):
        candidate = CandidateItem(
            media_type="movie",
            source="tmdb",
            media_id="9998",
            title="Date Only Movie",
            image="https://example.com/9998.jpg",
            release_date="2026-03-04",
        )
        mock_get_payload.return_value = DiscoverPayload(
            media_type="movie",
            rows=[
                RowResult(
                    key="coming_soon",
                    title="Coming Soon",
                    mission="Anticipation",
                    why="Upcoming releases to watchlist",
                    source="trakt",
                    items=[candidate],
                ),
            ],
            show_more=False,
        )

        response = self.client.get(reverse("discover"), {"media_type": "movie", "show_more": "0"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2026-03-04")
        self.assertNotContains(response, "% match")

    @patch("app.views.discover.get_discover_payload")
    def test_discover_page_renders_score_only_subtitle_with_top_picks_label_when_date_missing(self, mock_get_payload):
        candidate = CandidateItem(
            media_type="movie",
            source="tmdb",
            media_id="9997",
            title="Match Only Movie",
            image="https://example.com/9997.jpg",
            final_score=0.87,
        )
        mock_get_payload.return_value = DiscoverPayload(
            media_type="movie",
            rows=[
                RowResult(
                    key="top_picks_for_you",
                    title="Top Picks For You",
                    mission="Personal Taste Match",
                    why="New-to-you movies tailored to your taste.",
                    source="local",
                    items=[candidate],
                ),
            ],
            show_more=False,
        )

        response = self.client.get(reverse("discover"), {"media_type": "movie", "show_more": "0"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "87% Taste match")
