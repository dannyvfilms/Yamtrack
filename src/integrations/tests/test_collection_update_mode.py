"""Tests for collection update mode in imports."""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import CollectionEntry, Item, MediaTypes, Movie, Sources, Status
from integrations.models import PlexAccount
from integrations.tasks import update_collection_metadata_from_plex


class CollectionUpdateModeTest(TestCase):
    """Test collection update mode for Plex imports."""

    def setUp(self):
        """Set up test data."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

        self.plex_account = PlexAccount.objects.create(
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

        self.item = Item.objects.create(
            media_id="1234",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )

        # Create a tracked movie (mock metadata fetching to avoid API calls)
        with patch("app.providers.services.get_media_metadata") as mock_metadata:
            mock_metadata.return_value = {
                "title": "Test Movie",
                "image": "http://example.com/image.jpg",
                "max_progress": 1,
                "details": {"max_progress": 1},
            }
            self.movie = Movie.objects.create(
                user=self.user,
                item=self.item,
                status=Status.COMPLETED.value,
            )

    @patch("integrations.tasks.plex_api.list_resources")
    @patch("integrations.tasks.plex_api.fetch_history")
    @patch("integrations.tasks.plex_api.fetch_metadata")
    @patch("integrations.tasks.extract_collection_metadata_from_plex")
    def test_update_collection_metadata_from_plex(
        self,
        mock_extract,
        mock_fetch_metadata,
        mock_fetch_history,
        mock_list_resources,
    ):
        """Test collection metadata update from Plex update mode."""
        # Mock resources
        mock_list_resources.return_value = [
            {
                "machine_identifier": "test_machine",
                "connections": [{"uri": "http://plex.example.com"}],
            }
        ]

        # Mock history entries with matching item
        mock_fetch_history.return_value = (
            [
                {
                    "ratingKey": "12345",
                    "Guid": [{"id": "tmdb://1234"}],
                }
            ],
            "http://plex.example.com",
        )

        # Mock Plex metadata
        mock_plex_metadata = {
            "Media": [
                {
                    "videoResolution": "1080",
                    "videoCodec": "hevc",
                    "audioCodec": "dca",
                    "audioChannels": "6",
                }
            ]
        }
        mock_fetch_metadata.return_value = mock_plex_metadata

        # Mock collection metadata extraction
        mock_extract.return_value = {
            "resolution": "1080p",
            "hdr": "HDR10",
            "audio_codec": "DTS",
            "audio_channels": "5.1",
            "media_type": "digital",
        }

        # Call the task
        result = update_collection_metadata_from_plex(
            library="all",
            user_id=self.user.id,
        )

        # Verify result
        self.assertIn("updated", result)
        self.assertIn("errors", result)

        # If item was matched, verify collection entry was created/updated
        if result["updated"] > 0:
            entry = CollectionEntry.objects.get(user=self.user, item=self.item)
            self.assertEqual(entry.resolution, "1080p")
            self.assertEqual(entry.audio_codec, "DTS")
