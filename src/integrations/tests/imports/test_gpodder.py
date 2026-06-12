from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import Item, MediaTypes, Podcast, PodcastEpisode, PodcastShow, Sources, Status
from integrations.imports import gpodder as gpodder_import
from integrations.imports.helpers import MediaImportError, encrypt
from integrations.models import GPodderAccount


class GPodderImporterTests(TestCase):
    """Tests for the GPodder importer."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="listener", password="pass")  # noqa: S106
        self.account = GPodderAccount.objects.create(
            user=self.user,
            server_url=encrypt("https://gpodder.net"),
            username=encrypt("listener"),
            password=encrypt("secret"),
            device_id=f"yamtrack-{self.user.id}",
        )

    @patch("integrations.imports.gpodder.gpodder_api.register_device")
    @patch("integrations.imports.gpodder.gpodder_api.fetch_episode_actions")
    @patch("integrations.imports.gpodder.gpodder_api.fetch_subscriptions")
    @patch("integrations.imports.gpodder.gpodder_api.verify_login")
    @patch("integrations.imports.gpodder.podcast_rss.fetch_episodes_from_rss")
    @patch("integrations.imports.gpodder.podcast_rss.fetch_show_metadata_from_rss")
    def test_initial_sync_creates_show_episode_and_progress(
        self,
        mock_show_metadata,
        mock_fetch_rss_episodes,
        _mock_verify_login,
        mock_fetch_subscriptions,
        mock_fetch_actions,
        _mock_register_device,
    ):
        mock_fetch_subscriptions.return_value = ["https://example.com/feed.xml"]
        mock_show_metadata.return_value = {
            "title": "Example Show",
            "description": "Desc",
            "author": "Host",
        }
        mock_fetch_rss_episodes.return_value = [
            {
                "title": "Episode 1",
                "published": timezone.now(),
                "duration": 300,
                "audio_url": "https://cdn.example.com/ep1.mp3",
                "guid": "ep-1",
            },
        ]
        mock_fetch_actions.return_value = (
            [
                {
                    "action": "play",
                    "podcast": "https://example.com/feed.xml",
                    "episode": "https://cdn.example.com/ep1.mp3",
                    "timestamp": "2026-01-01T12:00:00Z",
                    "position": 120,
                    "total": 300,
                },
            ],
            77,
        )

        counts, warnings = gpodder_import.importer(None, self.user, "new")

        self.assertEqual(warnings, [])
        self.assertEqual(counts[MediaTypes.PODCAST.value], 1)
        show = PodcastShow.objects.get()
        self.assertEqual(show.source, Sources.GPODDER.value)
        item = Item.objects.get(source=Sources.GPODDER.value, media_type=MediaTypes.PODCAST.value)
        podcast = Podcast.objects.get(item=item, user=self.user)
        self.assertEqual(podcast.status, Status.IN_PROGRESS.value)
        self.assertEqual(podcast.played_up_to_seconds, 120)
        self.account.refresh_from_db()
        self.assertEqual(self.account.episode_actions_since, 77)

    @patch("integrations.imports.gpodder.gpodder_api.register_device")
    @patch("integrations.imports.gpodder.gpodder_api.fetch_episode_actions")
    @patch("integrations.imports.gpodder.gpodder_api.fetch_subscriptions")
    @patch("integrations.imports.gpodder.gpodder_api.verify_login")
    @patch("integrations.imports.gpodder.podcast_rss.fetch_episodes_from_rss")
    @patch("integrations.imports.gpodder.podcast_rss.fetch_show_metadata_from_rss")
    def test_incremental_sync_updates_to_completion_and_stays_idempotent(
        self,
        mock_show_metadata,
        mock_fetch_rss_episodes,
        _mock_verify_login,
        mock_fetch_subscriptions,
        mock_fetch_actions,
        _mock_register_device,
    ):
        now = timezone.now()
        show = PodcastShow.objects.create(
            podcast_uuid="gp_existing",
            source=Sources.GPODDER.value,
            title="Example Show",
            rss_feed_url="https://example.com/feed.xml",
        )
        episode = PodcastEpisode.objects.create(
            show=show,
            episode_uuid="ep-1",
            title="Episode 1",
            audio_url="https://cdn.example.com/ep1.mp3",
            duration=300,
            published=now,
        )
        item = Item.objects.create(
            media_id="ep-1",
            source=Sources.GPODDER.value,
            media_type=MediaTypes.PODCAST.value,
            title="Episode 1",
            image="https://example.com/image.jpg",
            runtime_minutes=5,
            release_datetime=now,
        )
        Podcast.objects.create(
            user=self.user,
            item=item,
            show=show,
            episode=episode,
            status=Status.IN_PROGRESS.value,
            progress=2,
            played_up_to_seconds=120,
            last_seen_status=2,
        )

        mock_fetch_subscriptions.return_value = ["https://example.com/feed.xml"]
        mock_show_metadata.return_value = {"title": "Example Show"}
        mock_fetch_rss_episodes.return_value = [
            {
                "title": "Episode 1",
                "published": now,
                "duration": 300,
                "audio_url": "https://cdn.example.com/ep1.mp3",
                "guid": "ep-1",
            },
        ]
        mock_fetch_actions.return_value = (
            [
                {
                    "action": "play",
                    "podcast": "https://example.com/feed.xml",
                    "episode": "https://cdn.example.com/ep1.mp3",
                    "timestamp": "2026-01-01T12:05:00Z",
                    "position": 300,
                    "total": 300,
                },
                {
                    "action": "play",
                    "podcast": "https://example.com/feed.xml",
                    "episode": "https://cdn.example.com/ep1.mp3",
                    "timestamp": "2026-01-01T12:05:00Z",
                    "position": 300,
                    "total": 300,
                },
            ],
            88,
        )

        counts, _ = gpodder_import.importer(None, self.user, "new")

        podcast = Podcast.objects.get(user=self.user, item=item)
        self.assertEqual(counts[MediaTypes.PODCAST.value], 1)
        self.assertEqual(podcast.status, Status.COMPLETED.value)
        self.assertEqual(podcast.end_date.isoformat().replace("+00:00", "Z"), "2026-01-01T12:05:00Z")
        self.assertEqual(Podcast.objects.filter(user=self.user, item=item).count(), 1)

    @patch("integrations.imports.gpodder.GPodderImporter._process_action", side_effect=RuntimeError("boom"))
    @patch(
        "integrations.imports.gpodder.gpodder_api.fetch_episode_actions",
        return_value=(
            [
                {
                    "action": "play",
                    "timestamp": "2026-01-01T12:00:00Z",
                    "position": 120,
                    "total": 300,
                },
            ],
            91,
        ),
    )
    @patch("integrations.imports.gpodder.gpodder_api.fetch_subscriptions", return_value=[])
    @patch("integrations.imports.gpodder.gpodder_api.verify_login")
    @patch("integrations.imports.gpodder.gpodder_api.register_device")
    def test_failed_processing_does_not_advance_cursor(
        self,
        _mock_register_device,
        _mock_verify_login,
        _mock_fetch_subscriptions,
        _mock_fetch_episode_actions,
        _mock_process_action,
    ):
        with self.assertRaises(RuntimeError):
            gpodder_import.importer(None, self.user, "new")

        self.account.refresh_from_db()
        self.assertIsNone(self.account.episode_actions_since)

    @patch(
        "integrations.imports.gpodder.gpodder_api.verify_login",
        side_effect=gpodder_import.gpodder_api.GPodderAuthError("nope"),
    )
    def test_invalid_credentials_raise_media_import_error(self, _mock_verify_login):
        with self.assertRaises(MediaImportError):
            gpodder_import.importer(None, self.user, "new")
