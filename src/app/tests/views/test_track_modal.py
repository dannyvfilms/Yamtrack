from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from app.models import (
    Anime,
    Item,
    MediaTypes,
    Movie,
    Podcast,
    PodcastEpisode,
    PodcastShow,
    Sources,
    Status,
)
from app.services.metadata_resolution import MetadataResolutionResult


class TrackModalViewTests(TestCase):
    """Test the track modal view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.mock_get_media_metadata = patch(
            "app.models.providers.services.get_media_metadata",
            return_value={"max_progress": 1},
        )
        self.mock_fetch_releases = patch("app.models.Item.fetch_releases")
        self.mock_get_media_metadata.start()
        self.mock_fetch_releases.start()
        self.addCleanup(self.mock_get_media_metadata.stop)
        self.addCleanup(self.mock_fetch_releases.stop)

        self.item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        self.movie = Movie.objects.create(
            item=self.item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

    def test_track_modal_view_existing_media(self):
        """Test the track modal view for existing media."""
        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            )
            + "?return_url=/home",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track.html")

        self.assertIn("form", response.context)
        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"], self.movie)
        self.assertEqual(response.context["return_url"], "/home")
        self.assertTrue(response.context["metadata_tab_available"])
        general_field_names = [
            field.name for field in response.context["general_fields"]
        ]
        self.assertEqual(general_field_names[:2], ["score", "status"])
        self.assertEqual(
            [field.name for field in response.context["metadata_fields"]],
            ["image_url"],
        )
        self.assertContains(response, "General")
        self.assertContains(response, "Metadata")
        self.assertContains(response, "Image URL")
        self.assertContains(response, "Save Image")
        self.assertNotContains(response, "Metadata Provider")

    @patch("app.providers.services.get_media_metadata")
    def test_track_modal_view_new_media(self, mock_get_metadata):
        """Test the track modal view for new media."""
        mock_get_metadata.return_value = {
            "media_id": "278",
            "title": "New Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
        }

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "278",
                },
            )
            + "?return_url=/home&title=New+Movie",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track.html")

        self.assertIn("form", response.context)
        self.assertEqual(response.context["form"].initial["media_id"], "278")
        self.assertEqual(
            response.context["form"].initial["media_type"],
            MediaTypes.MOVIE.value,
        )
        self.assertEqual(
            response.context["form"].initial["image_url"],
            "http://example.com/image.jpg",
        )
        self.assertContains(
            response,
            "Save this image from the General tab when you add or update the entry.",
        )
        self.assertNotContains(response, "Save Image")

    def test_update_item_image(self):
        """Existing tracked items should allow image overrides from metadata."""
        response = self.client.post(
            reverse("update_item_image", args=[self.item.id]),
            {
                "image_url": "https://images.example.com/updated-poster.jpg",
                "return_url": "/home",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/home")

        self.item.refresh_from_db()
        self.assertEqual(
            self.item.image,
            "https://images.example.com/updated-poster.jpg",
        )

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_track_modal_renders_metadata_sidebar_for_anime(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        """Anime tracking modal should expose a separate metadata tab."""
        anime_item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        base_metadata = {
            "media_id": "52991",
            "title": "Frieren",
            "original_title": "Sousou no Frieren",
            "localized_title": "Frieren",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "image": "https://example.com/frieren.jpg",
            "max_progress": 28,
            "details": {"episodes": 28},
            "related": {},
        }
        mock_get_metadata.return_value = base_metadata
        anime = Anime.objects.create(
            item=anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=12,
        )
        mock_resolve_detail_metadata.return_value = MetadataResolutionResult(
            display_provider=Sources.TVDB.value,
            identity_provider=Sources.MAL.value,
            mapping_status="mapped",
            header_metadata=base_metadata,
            grouped_preview={
                "media_id": "9350138",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.ANIME.value,
                "title": "Frieren: Beyond Journey's End",
                "related": {
                    "seasons": [
                        {
                            "season_number": 1,
                            "episode_count": 28,
                            "is_mapped_target": True,
                            "mapped_episode_start": 1,
                            "mapped_episode_end": 28,
                        },
                    ],
                },
            },
            provider_media_id="9350138",
            grouped_preview_target={
                "season_number": 1,
                "season_title": "Season 1",
                "episode_start": 1,
                "episode_end": 28,
            },
        )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "52991",
                },
            )
            + f"?instance_id={anime.id}&return_url=/details/mal/anime/52991/frieren",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track.html")
        self.assertTrue(response.context["metadata_tab_available"])
        self.assertContains(response, "General")
        self.assertContains(response, "Metadata")
        self.assertContains(response, "Metadata Provider")
        self.assertContains(response, "Convert to Grouped Series")
        self.assertContains(response, "This MAL entry would convert to")
        self.assertContains(response, "Conversion target")


class PodcastTrackModalViewTests(TestCase):
    """Podcast-specific track modal behavior."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_podcast_track_modal_shows_delete_for_in_progress_play(self):
        """Podcast episode modal should allow deleting an in-progress play."""
        show = PodcastShow.objects.create(
            podcast_uuid="show-uuid-1",
            title="Show Title",
            image="http://example.com/show.jpg",
        )
        episode = PodcastEpisode.objects.create(
            show=show,
            episode_uuid="episode-uuid-1",
            title="Episode Title",
            duration=1577,
        )
        item = Item.objects.create(
            media_id=episode.episode_uuid,
            source=Sources.POCKETCASTS.value,
            media_type=MediaTypes.PODCAST.value,
            title=episode.title,
            image=show.image,
        )
        podcast = Podcast.objects.create(
            item=item,
            user=self.user,
            show=show,
            episode=episode,
            status=Status.IN_PROGRESS.value,
            progress=10,
        )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.POCKETCASTS.value,
                    "media_type": MediaTypes.PODCAST.value,
                    "media_id": episode.episode_uuid,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track_song.html")
        self.assertContains(response, "In-Progress Play")
        self.assertContains(
            response,
            f'name="instance_id" value="{podcast.id}"',
            html=False,
        )
        self.assertContains(response, 'name="media_type" value="podcast"', html=False)
