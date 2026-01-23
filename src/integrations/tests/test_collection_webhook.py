"""Tests for collection metadata update from webhooks."""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import CollectionEntry, Item, MediaTypes, Movie, Sources, Status
from integrations.tasks import update_collection_metadata_from_plex_webhook
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
