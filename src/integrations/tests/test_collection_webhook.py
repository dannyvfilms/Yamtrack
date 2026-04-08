"""Tests for collection metadata update from webhooks."""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import CollectionEntry, Item, MediaTypes, Movie, Sources, Status
from integrations import plex as plex_api
from integrations.tasks import (
    fetch_collection_metadata_for_item,
    update_collection_metadata_from_plex_webhook,
)
from integrations.webhooks.plex import PlexWebhookProcessor


class CollectionWebhookTest(TestCase):
    """Test collection metadata updates from webhooks."""

    def setUp(self):
        """Set up test data."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.item = Item.objects.create(
            media_id="1234",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

    @patch("integrations.tasks.plex_api.fetch_metadata")
    @patch("integrations.tasks.extract_collection_metadata_from_plex")
    def test_update_collection_metadata_from_plex_webhook(self, mock_extract, mock_fetch):
        """Test collection metadata update from Plex webhook task."""
        # Mock Plex metadata response
        mock_plex_metadata = {
            "Media": [
                {
                    "videoResolution": "1080",
                    "videoCodec": "hevc",
                    "audioCodec": "dca",
                    "audioChannels": "6",
                    "container": "mkv",
                }
            ]
        }
        mock_fetch.return_value = mock_plex_metadata

        # Mock collection metadata extraction
        mock_extract.return_value = {
            "resolution": "1080p",
            "hdr": "HDR10",
            "audio_codec": "DTS",
            "audio_channels": "5.1",
            "media_type": "digital",
        }

        # Call the task
        result = update_collection_metadata_from_plex_webhook(
            user_id=self.user.id,
            item_id=self.item.id,
            rating_key="12345",
            plex_uri="http://plex.example.com",
            plex_token="test_token",
        )

        # Verify collection entry was created
        entry = CollectionEntry.objects.get(user=self.user, item=self.item)
        self.assertEqual(entry.resolution, "1080p")
        self.assertEqual(entry.hdr, "HDR10")
        self.assertEqual(entry.audio_codec, "DTS")
        self.assertEqual(entry.audio_channels, "5.1")
        self.assertEqual(entry.media_type, "digital")
        self.assertIsNotNone(result)

    def test_plex_webhook_queues_collection_update(self):
        """Test that Plex webhook queues collection metadata update."""
        processor = PlexWebhookProcessor()

        # Create a mock payload with rating key
        payload = {
            "event": "media.scrobble",
            "Account": {"title": "test"},
            "Metadata": {
                "type": "Movie",
                "ratingKey": "12345",
                "title": "Test Movie",
            },
            "Server": {"uri": "http://plex.example.com"},
        }

        # Mock Plex account
        from integrations.models import PlexAccount

        plex_account = PlexAccount.objects.create(
            user=self.user,
            plex_token="test_token",
            plex_username="test",
        )

        # Mock the task
        with patch("integrations.webhooks.plex.tasks.update_collection_metadata_from_plex_webhook.delay") as mock_task:
            # Test the method directly
            processor._queue_collection_metadata_update(payload, self.user, self.item)

            # Verify task was called
            mock_task.assert_called_once_with(
                self.user.id,
                self.item.id,
                "12345",
                "http://plex.example.com",
                "test_token",
            )

    @patch("integrations.tasks.logger.warning")
    @patch("integrations.tasks.plex_api.fetch_section_all_items")
    @patch("integrations.tasks.plex_api.list_resources")
    def test_fetch_collection_metadata_timeout_logs_without_traceback(
        self,
        mock_list_resources,
        mock_fetch_section_all_items,
        mock_logger_warning,
    ):
        """Expected Plex lookup failures should not emit stack traces."""
        from integrations.models import PlexAccount

        PlexAccount.objects.create(
            user=self.user,
            plex_token="test_token",
            plex_username="test",
            sections=[
                {
                    "id": "1",
                    "title": "Movies",
                    "type": "movie",
                    "uri": "http://plex.example.com",
                    "machine_identifier": "test_machine",
                }
            ],
        )
        mock_list_resources.return_value = []
        mock_fetch_section_all_items.side_effect = [
            ([], 9502),
            plex_api.PlexClientError(
                "HTTPSConnectionPool(host='plex.example.com', port=443): Read timed out.",
            ),
        ]

        result = fetch_collection_metadata_for_item(self.user.id, self.item.id)

        self.assertIsNone(result)
        matching_calls = [
            call
            for call in mock_logger_warning.call_args_list
            if call.args and "Error searching section" in call.args[0]
        ]
        self.assertTrue(matching_calls)
        self.assertNotIn("exc_info", matching_calls[0].kwargs)
