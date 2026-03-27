from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from app.models import (
    Album,
    AlbumTracker,
    Artist,
    ArtistTracker,
    MediaTypes,
    Sources,
)
from users.models import MetadataSourceDefaultChoices


class MediaSearchViewTests(TestCase):
    """Test the media search view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.search")
    def test_media_search_view(self, mock_search):
        """Test the media search view."""
        mock_search.return_value = {
            "page": 1,
            "total_results": 1,
            "total_pages": 1,
            "results": [
                {
                    "media_id": "238",
                    "title": "Test Movie",
                    "media_type": MediaTypes.MOVIE.value,
                    "source": Sources.TMDB.value,
                    "image": "http://example.com/image.jpg",
                },
            ],
        }

        response = self.client.get(
            reverse("search") + "?media_type=movie&q=test",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/search.html")

        self.user.refresh_from_db()
        self.assertEqual(self.user.last_search_type, MediaTypes.MOVIE.value)

        mock_search.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "test",
            1,
            Sources.TMDB.value,
        )

    @patch("app.providers.services.search")
    def test_music_search_view_uses_shared_template(
        self,
        mock_search,
    ):
        """Music search should render grouped artist/album sections."""
        artist = Artist.objects.create(name="Pentatonix")
        album = Album.objects.create(
            title="Evergreen",
            artist=artist,
            image="http://example.com/local-album.jpg",
        )
        ArtistTracker.objects.create(user=self.user, artist=artist, score=8.5)
        AlbumTracker.objects.create(user=self.user, album=album, score=9.0)

        mock_search.return_value = {
            "artists": [
                {
                    "artist_id": "mb-artist-1",
                    "name": "Pentatonix",
                    "type": "Group",
                    "begin_year": "2011",
                    "disambiguation": "",
                    "image": "http://example.com/remote-artist.jpg",
                },
            ],
            "releases": [
                {
                    "release_id": "mb-release-1",
                    "title": "A Pentatonix Christmas",
                    "artist_name": "Pentatonix",
                    "release_date": "2016-10-21",
                    "image": "http://example.com/remote-album.jpg",
                },
            ],
            "tracks": {
                "page": 1,
                "total_results": 0,
                "total_pages": 0,
                "results": [],
            },
        }

        response = self.client.get(
            reverse("search") + "?media_type=music&q=Pentatonix",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/search.html")
        self.assertContains(response, "In Your Library")
        self.assertContains(response, "Online Results")
        self.assertContains(response, "Evergreen")
        self.assertContains(response, "A Pentatonix Christmas")
        self.assertContains(response, "Pentatonix")
        self.assertContains(
            response,
            reverse(
                "music_artist_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "pentatonix",
                },
            ),
        )
        self.assertContains(
            response,
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "pentatonix",
                    "album_id": album.id,
                    "album_slug": "evergreen",
                },
            ),
        )
        self.assertContains(response, "http://example.com/remote-artist.jpg")
        self.assertContains(response, "http://example.com/remote-album.jpg")
        self.assertNotIn("Tracks</h3>", response.content.decode())

        mock_search.assert_called_once_with(
            MediaTypes.MUSIC.value,
            "Pentatonix",
            1,
            Sources.MUSICBRAINZ.value,
        )

    @patch("app.providers.services.search")
    def test_music_local_album_search_matches_artist_name(self, mock_search):
        """Music local albums should include artist-name matches."""
        artist = Artist.objects.create(name="Pentatonix")
        album = Album.objects.create(title="The Lucky Ones", artist=artist)
        AlbumTracker.objects.create(user=self.user, album=album)

        mock_search.return_value = {
            "artists": [],
            "releases": [],
            "tracks": {
                "page": 1,
                "total_results": 0,
                "total_pages": 0,
                "results": [],
            },
        }

        response = self.client.get(
            reverse("search") + "?media_type=music&q=Pentatonix",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The Lucky Ones")

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.providers.services.search")
    def test_anime_search_defaults_to_user_metadata_provider(self, mock_search):
        """Anime search should honor the user's configured default metadata source."""
        self.user.anime_metadata_source_default = MetadataSourceDefaultChoices.TVDB
        self.user.save(update_fields=["anime_metadata_source_default"])
        mock_search.return_value = {
            "page": 1,
            "total_results": 0,
            "total_pages": 0,
            "results": [],
        }

        response = self.client.get(
            reverse("search") + "?media_type=anime&q=chainsaw",
        )

        self.assertEqual(response.status_code, 200)
        mock_search.assert_called_once_with(
            MediaTypes.ANIME.value,
            "chainsaw",
            1,
            Sources.TVDB.value,
        )
