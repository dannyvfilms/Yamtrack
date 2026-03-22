import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from app.discover.schemas import CandidateItem, RowResult
from app.models import (
    MediaTypes,
    Sources,
)


class DiscoverActionRowPartialTests(TestCase):
    """Regression coverage for row-scoped Discover action updates."""

    def setUp(self):
        self.credentials = {
            "username": "discover-row-user",
            "password": "secret123",
        }
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def _row(self, *, key: str, title: str, media_id: str) -> RowResult:
        return RowResult(
            key=key,
            title=title,
            mission="Mission",
            why="Why",
            source="local",
            items=[
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source=Sources.TMDB.value,
                    media_id=media_id,
                    title=f"{title} Movie",
                    image="https://example.com/poster.jpg",
                ),
            ],
        )

    @patch("app.views.discover_tab_cache.update_undo_snapshot")
    @patch("app.views.discover_tab_cache.apply_cached_action")
    @patch("app.views.discover_tab_cache.invalidate_for_feedback_change")
    @patch("app.views.discover_tab_cache.store_undo_snapshot", return_value="undo-dismiss")
    def test_discover_action_returns_only_updated_row_fragment(
        self,
        _mock_store_undo_snapshot,
        mock_invalidate_for_feedback_change,
        mock_apply_cached_action,
        mock_update_undo_snapshot,
    ):
        mock_apply_cached_action.return_value = [
            self._row(
                key="all_time_greats_unseen",
                title="All Time Greats",
                media_id="2001",
            ),
            self._row(
                key="comfort_rewatches",
                title="Comfort Rewatches",
                media_id="2002",
            ),
        ]

        response = self.client.post(
            reverse("discover_action"),
            {
                "action": "dismiss",
                "candidate_media_type": MediaTypes.MOVIE.value,
                "source": Sources.TMDB.value,
                "media_id": "1100988",
                "active_media_type": MediaTypes.MOVIE.value,
                "show_more": "0",
                "row_key": "all_time_greats_unseen",
                "title": "Dismiss Me",
            },
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('id="discover-row-all_time_greats_unseen"', content)
        self.assertIn("All Time Greats", content)
        self.assertNotIn("Comfort Rewatches", content)
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["discoverActionComplete"]["action"], "dismiss")
        self.assertEqual(trigger["discoverActionComplete"]["undo_token"], "undo-dismiss")
        mock_invalidate_for_feedback_change.assert_called_once_with(
            self.user.id,
            MediaTypes.MOVIE.value,
        )
        mock_update_undo_snapshot.assert_called_once()
