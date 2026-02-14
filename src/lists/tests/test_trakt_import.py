from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import MediaTypes
from lists.imports import trakt
from lists.models import CustomList


class TraktListImportTests(TestCase):
    """Tests for Trakt custom list import helpers."""

    def setUp(self):
        """Create a user for import tests."""
        self.user = get_user_model().objects.create_user(
            username="trakt-import-user",
        )

    @patch("lists.imports.trakt._make_trakt_request")
    def test_get_trakt_list_items_paginates_until_empty_page(self, mock_make_request):
        """List item fetch should continue until Trakt returns an empty page."""
        mock_make_request.side_effect = [
            [{"id": 1}, {"id": 2}],
            [{"id": 3}],
            [],
        ]

        items = trakt._get_trakt_list_items("token-123", "42", client_id="client-id")

        self.assertEqual(len(items), 3)
        self.assertEqual(
            mock_make_request.call_args_list,
            [
                call(
                    "token-123",
                    "https://api.trakt.tv/users/me/lists/42/items?page=1&limit=1000",
                    client_id="client-id",
                ),
                call(
                    "token-123",
                    "https://api.trakt.tv/users/me/lists/42/items?page=2&limit=1000",
                    client_id="client-id",
                ),
                call(
                    "token-123",
                    "https://api.trakt.tv/users/me/lists/42/items?page=3&limit=1000",
                    client_id="client-id",
                ),
            ],
        )

    @patch("app.tasks.enqueue_episode_runtime_backfill")
    @patch("lists.imports.trakt._get_metadata")
    @patch("lists.imports.trakt._get_trakt_watchlist_items")
    @patch("lists.imports.trakt._get_trakt_list_items")
    @patch("lists.imports.trakt._get_trakt_lists")
    def test_import_trakt_lists_imports_episode_entries(
        self,
        mock_get_lists,
        mock_get_list_items,
        mock_get_watchlist_items,
        mock_get_metadata,
        mock_enqueue_episode_runtime_backfill,
    ):
        """Episode entries from Trakt lists should be imported as episode items."""
        del mock_enqueue_episode_runtime_backfill
        mock_get_lists.return_value = [
            {
                "ids": {"trakt": 123},
                "name": "Episode Picks",
                "privacy": "private",
            },
        ]
        mock_get_list_items.return_value = [
            {
                "type": "episode",
                "show": {
                    "title": "My Show",
                    "ids": {"tmdb": 555},
                },
                "episode": {
                    "season": 2,
                    "number": 3,
                },
            },
        ]
        mock_get_watchlist_items.return_value = []
        mock_get_metadata.return_value = {
            "title": "My Show",
            "episode_title": "Episode 3",
            "image": "http://example.com/episode.jpg",
        }

        trakt.import_trakt_lists(self.user, "token-123", client_id="client-id")

        imported_list = CustomList.objects.get(owner=self.user, source_id="123")
        imported_item = imported_list.items.get()

        self.assertEqual(imported_item.media_type, MediaTypes.EPISODE.value)
        self.assertEqual(imported_item.media_id, "555")
        self.assertEqual(imported_item.season_number, 2)
        self.assertEqual(imported_item.episode_number, 3)
        mock_get_metadata.assert_called_once_with(
            MediaTypes.EPISODE.value,
            "555",
            "My Show",
            season_number=2,
            episode_number=3,
        )
