from datetime import UTC, date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from app.models import (
    TV,
    Album,
    AlbumTracker,
    Anime,
    Artist,
    ArtistTracker,
    DiscoverFeedback,
    DiscoverFeedbackType,
    Episode,
    Item,
    MediaTypes,
    MetadataProviderPreference,
    Movie,
    Podcast,
    PodcastEpisode,
    PodcastShow,
    PodcastShowTracker,
    Season,
    Sources,
    Status,
)
from app.services.metadata_resolution import MetadataResolutionResult


def _tv_with_seasons_payload(media_id, source, *, title="Test Show", episode_count=3):
    episodes = [
        {
            "episode_number": episode_number,
            "name": f"Episode {episode_number}",
            "air_date": f"2024-01-0{episode_number}",
            "runtime": 24,
        }
        for episode_number in range(1, episode_count + 1)
    ]
    return {
        "media_id": media_id,
        "source": source,
        "media_type": MediaTypes.TV.value,
        "title": title,
        "image": "https://example.com/show.jpg",
        "related": {
            "seasons": [
                {
                    "season_number": 1,
                    "season_title": "Season 1",
                },
            ],
        },
        "season/1": {
            "season_number": 1,
            "season_title": "Season 1",
            "title": title,
            "image": "https://example.com/season.jpg",
            "episodes": episodes,
        },
    }


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

    def assert_release_shortcut_labels(self, response):
        """Release-date shortcut buttons should split mobile and desktop labels."""
        content = response.content.decode()
        self.assertEqual(
            content.count('track-release-shortcut-mobile-label">Release</span>'),
            2,
        )
        self.assertEqual(
            content.count('track-release-shortcut-desktop-label">Release Date</span>'),
            2,
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
        self.assertTrue(response.context["discover_tab_available"])
        self.assertFalse(response.context["is_hidden_from_discover"])
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
        self.assertContains(response, "Discover")
        self.assertContains(response, "Image URL")
        self.assertContains(response, "Save Image")
        self.assertContains(response, "Metadata Provider")
        self.assertContains(response, "Custom")
        self.assertContains(response, "Currently visible in Discover.")
        self.assertContains(response, 'hx-post="/discover/toggle-hidden"', html=False)
        self.assertContains(response, 'name="action"', html=False)
        self.assertNotContains(response, "Custom Metadata")

    def test_track_modal_view_existing_episode_exposes_score(self):
        """Episode history edits should use the standard modal with rating support."""
        tv_item = Item.objects.create(
            media_id="episode-show-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Episode Show",
            image="http://example.com/show.jpg",
        )
        tv = TV.objects.create(item=tv_item, user=self.user)
        season_item = Item.objects.create(
            media_id="episode-show-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Episode Show Season 1",
            image="http://example.com/season.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
        )
        episode_item = Item.objects.create(
            media_id="episode-show-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode Two",
            image="http://example.com/episode.jpg",
            season_number=1,
            episode_number=2,
        )
        episode = Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=datetime(2025, 1, 2, 12, 0, tzinfo=UTC),
            score=7,
        )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.EPISODE.value,
                    "media_id": "episode-show-1",
                    "season_number": 1,
                },
            )
            + f"?instance_id={episode.id}&standard_modal=1&return_url=/history",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track.html")
        self.assertEqual(response.context["media"], episode)
        self.assertEqual(
            [field.name for field in response.context["general_fields"]],
            ["score", "end_date"],
        )
        self.assertContains(response, 'name="score"', html=False)
        self.assertContains(response, 'value="7.0"', html=False)

    def test_track_modal_view_renders_release_date_shortcuts_for_existing_media(self):
        """Existing item-backed trackers should expose release-date shortcuts."""
        self.item.release_datetime = datetime(2024, 1, 15, tzinfo=UTC)
        self.item.runtime_minutes = 95
        self.item.save(update_fields=["release_datetime", "runtime_minutes"])

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "applyTrackModalReleaseDate(this, '2024-01-15', 'start_date')",
            html=False,
        )
        self.assertContains(
            response,
            "applyTrackModalReleaseDate(this, '2024-01-15', 'end_date', '95')",
            html=False,
        )
        self.assertContains(response, "Release date", count=2)
        self.assert_release_shortcut_labels(response)

    def test_track_modal_close_button_supports_split_button_wrapper(self):
        """The shared close button should work for edit/create split-button wrappers."""
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
        content = response.content.decode()
        self.assertIn('@click="', content)
        self.assertIn("if (modalState && modalState.createTrackOpen)", content)
        self.assertIn("if (modalState && modalState.editTrackOpen)", content)
        self.assertIn("if (modalState && modalState.trackOpen)", content)
        self.assertNotContains(response, 'onclick="closeTrackModal(this)"', html=False)

    def test_track_modal_view_uses_stored_discover_hidden_state(self):
        """Discover tab should reflect persisted hidden feedback for the item."""
        DiscoverFeedback.objects.create(
            user=self.user,
            item=self.item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["discover_tab_available"])
        self.assertTrue(response.context["is_hidden_from_discover"])
        self.assertContains(response, "Currently hidden in Discover.")
        self.assertContains(response, "name=\"action\"", html=False)

    def test_artist_track_modal_uses_shared_fill_track_shell(self):
        """Music artist trackers should render through the shared modal shell."""
        artist = Artist.objects.create(name="Test Artist")
        tracker = ArtistTracker.objects.create(
            user=self.user,
            artist=artist,
            status=Status.IN_PROGRESS.value,
        )

        response = self.client.get(
            reverse("artist_track_modal", args=[artist.id]) + "?return_url=/music",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track.html")
        self.assertEqual(response.context["title"], artist.name)
        self.assertEqual(response.context["general_existing_instance"], tracker)
        self.assertFalse(response.context["metadata_tab_available"])
        self.assertContains(response, "General")
        self.assertNotContains(response, "Metadata")

    def test_album_track_modal_uses_shared_fill_track_shell(self):
        """Music album trackers should render through the shared modal shell."""
        artist = Artist.objects.create(name="Test Artist")
        album = Album.objects.create(title="Test Album", artist=artist)
        tracker = AlbumTracker.objects.create(
            user=self.user,
            album=album,
            status=Status.COMPLETED.value,
        )

        response = self.client.get(
            reverse("album_track_modal", args=[album.id]) + "?return_url=/music",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track.html")
        self.assertEqual(response.context["title"], album.title)
        self.assertEqual(response.context["general_existing_instance"], tracker)
        self.assertFalse(response.context["metadata_tab_available"])
        self.assertContains(response, "General")
        self.assertNotContains(response, "Metadata")

    def test_album_track_modal_renders_release_date_shortcuts(self):
        """Album trackers should expose the shared release-date shortcut."""
        artist = Artist.objects.create(name="Test Artist")
        album = Album.objects.create(
            title="Test Album",
            artist=artist,
            release_date=date(2024, 2, 3),
        )

        response = self.client.get(
            reverse("album_track_modal", args=[album.id]) + "?return_url=/music",
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "applyTrackModalReleaseDate(this, '2024-02-03', 'start_date')",
            html=False,
        )
        self.assertContains(response, "Release date", count=2)
        self.assert_release_shortcut_labels(response)

    def test_artist_save_redirects_to_canonical_music_details(self):
        """Artist saves should land on the canonical shared details page."""
        artist = Artist.objects.create(name="Saved Artist")

        response = self.client.post(
            reverse("artist_save"),
            {
                "artist_id": artist.id,
                "status": Status.IN_PROGRESS.value,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse(
                "music_artist_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "saved-artist",
                },
            ),
        )

    def test_album_delete_redirects_to_canonical_music_details(self):
        """Album deletes should land on the canonical shared details page."""
        artist = Artist.objects.create(name="Saved Artist")
        album = Album.objects.create(title="Saved Album", artist=artist)
        AlbumTracker.objects.create(
            user=self.user,
            album=album,
            status=Status.IN_PROGRESS.value,
        )

        response = self.client.post(
            reverse("album_delete"),
            {
                "album_id": album.id,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "saved-artist",
                    "album_id": album.id,
                    "album_slug": "saved-album",
                },
            ),
        )

    @patch("app.services.music.sync_artist_discography")
    @patch("app.providers.musicbrainz.get_artist")
    def test_create_artist_from_search_redirects_to_canonical_music_details(
        self,
        mock_get_artist,
        mock_sync_artist_discography,
    ):
        """Artist search creates should redirect to the canonical shared details page."""
        mock_get_artist.return_value = {
            "name": "Fetched Artist",
            "sort_name": "Artist, Fetched",
            "country": "US",
            "genres": [{"name": "rock"}],
        }
        mock_sync_artist_discography.return_value = 0

        response = self.client.get(
            reverse("create_artist_from_search", args=["artist-mbid"]),
        )

        artist = Artist.objects.get(musicbrainz_id="artist-mbid")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse(
                "music_artist_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "fetched-artist",
                },
            ),
        )

    @patch("app.providers.musicbrainz.get_release")
    def test_create_album_from_search_redirects_to_canonical_music_details(
        self,
        mock_get_release,
    ):
        """Album search creates should redirect to the canonical shared details page."""
        mock_get_release.return_value = {
            "title": "Fetched Album",
            "artist_id": "artist-mbid",
            "artist_name": "Fetched Artist",
            "release_date": "2024-01-15",
            "image": "https://example.com/album.jpg",
            "genres": ["rock"],
        }

        response = self.client.get(
            reverse("create_album_from_search", args=["release-mbid"]),
        )

        album = Album.objects.get(musicbrainz_release_id="release-mbid")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": album.artist.id,
                    "artist_slug": "fetched-artist",
                    "album_id": album.id,
                    "album_slug": "fetched-album",
                },
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_track_modal_view_new_media(self, mock_get_metadata):
        """Test the track modal view for new media."""
        mock_get_metadata.return_value = {
            "media_id": "278",
            "title": "New Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "details": {
                "release_date": "2024-01-15",
                "runtime": "1h 35min",
            },
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
        self.assertContains(
            response,
            "applyTrackModalReleaseDate(this, '2024-01-15', 'end_date', '95')",
            html=False,
        )
        self.assertContains(response, "Release date", count=2)
        self.assert_release_shortcut_labels(response)
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

    def test_track_modal_renders_custom_metadata_form_for_manual_movie(self):
        """Manual/custom items should expose the full metadata editor."""
        manual_item = Item.objects.create(
            media_id="manual-movie-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Manual Movie",
            image="https://example.com/manual-movie.jpg",
        )
        Movie.objects.create(
            item=manual_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.MANUAL.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "manual-movie-1",
                },
            )
            + "?return_url=/home",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["metadata_tab_available"])
        self.assertTrue(response.context["can_update_metadata_provider"])
        self.assertTrue(response.context["can_edit_custom_metadata"])
        self.assertIsNotNone(response.context["manual_metadata_form"])
        self.assertEqual(
            list(response.context["manual_metadata_form"].fields.keys())[:6],
            [
                "title",
                "original_title",
                "localized_title",
                "image_url",
                "synopsis",
                "genres",
            ],
        )
        self.assertContains(response, "Custom Metadata")
        self.assertContains(response, "Metadata Provider")
        self.assertContains(response, "Display metadata is currently coming from")
        self.assertContains(response, "Custom")
        self.assertContains(response, "Release Date")
        self.assertContains(response, "Runtime")
        self.assertContains(response, "Save Metadata")
        self.assertNotContains(response, "Save Image")

    def test_track_modal_renders_custom_metadata_form_for_movie_using_custom_provider(self):
        """Tracked items should show the custom editor when Custom is selected."""
        self.item.manual_metadata = {
            "title": "Custom Display Movie",
            "original_title": "Custom Original",
            "localized_title": "Custom Localized",
            "image": "https://example.com/custom-display-movie.jpg",
            "synopsis": "A custom display synopsis.",
            "genres": ["Drama"],
            "details": {
                "release_date": "2024-02-01",
                "runtime": "2h 1min",
            },
        }
        self.item.save(update_fields=["manual_metadata"])
        MetadataProviderPreference.objects.create(
            user=self.user,
            item=self.item,
            provider=Sources.MANUAL.value,
        )

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
        self.assertTrue(response.context["can_update_metadata_provider"])
        self.assertTrue(response.context["can_edit_custom_metadata"])
        self.assertEqual(response.context["display_provider"], Sources.MANUAL.value)
        self.assertContains(response, "Metadata Provider")
        self.assertContains(response, "Custom Metadata")
        self.assertContains(response, "Save Metadata")
        self.assertNotContains(response, "Save Image")

    def test_update_manual_item_metadata(self):
        """Manual/custom metadata edits should persist on the underlying item."""
        manual_item = Item.objects.create(
            media_id="manual-movie-2",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Original Manual Movie",
            image="https://example.com/original-manual-movie.jpg",
        )
        Movie.objects.create(
            item=manual_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        response = self.client.post(
            reverse("update_manual_item_metadata", args=[manual_item.id]),
            {
                "return_url": "/home",
                "metadata-title": "Updated Manual Movie",
                "metadata-original_title": "Original Language Title",
                "metadata-localized_title": "Localized Manual Movie",
                "metadata-image_url": "https://images.example.com/manual-custom-poster.jpg",
                "metadata-synopsis": "A custom movie synopsis.",
                "metadata-genres": "Drama\nThriller",
                "metadata-release_date": "2024-01-15",
                "metadata-status": "Released",
                "metadata-runtime": "2h 10min",
                "metadata-studios": "Studio One, Studio Two",
                "metadata-country": "Japan",
                "metadata-languages": "Japanese\nEnglish",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/home")

        manual_item.refresh_from_db()
        self.assertEqual(manual_item.title, "Updated Manual Movie")
        self.assertEqual(manual_item.original_title, "Original Language Title")
        self.assertEqual(manual_item.localized_title, "Localized Manual Movie")
        self.assertEqual(
            manual_item.image,
            "https://images.example.com/manual-custom-poster.jpg",
        )
        self.assertEqual(manual_item.genres, ["Drama", "Thriller"])
        self.assertEqual(manual_item.studios, ["Studio One", "Studio Two"])
        self.assertEqual(manual_item.country, "Japan")
        self.assertEqual(manual_item.languages, ["Japanese", "English"])
        self.assertEqual(manual_item.runtime, "2h 10min")
        self.assertEqual(manual_item.runtime_minutes, 130)
        self.assertEqual(
            manual_item.release_datetime.date().isoformat(),
            "2024-01-15",
        )
        self.assertEqual(manual_item.manual_metadata["synopsis"], "A custom movie synopsis.")
        self.assertEqual(
            manual_item.manual_metadata["details"]["release_date"],
            "2024-01-15",
        )

    def test_update_manual_item_metadata_redirects_to_normalized_return_url(self):
        """Custom metadata saves should restore encoded list query separators."""
        MetadataProviderPreference.objects.create(
            user=self.user,
            item=self.item,
            provider=Sources.MANUAL.value,
        )

        response = self.client.post(
            reverse("update_manual_item_metadata", args=[self.item.id]),
            {
                "return_url": (
                    "/medialist/movie%3Fstatus%3DPlanning&sort%3Drelease_date"
                    "&direction%3Ddesc&layout%3Dgrid"
                ),
                "metadata-title": "Updated Test Movie",
                "metadata-original_title": "",
                "metadata-localized_title": "",
                "metadata-image_url": "https://images.example.com/updated-test-movie.jpg",
                "metadata-synopsis": "",
                "metadata-genres": "",
                "metadata-release_date": "",
                "metadata-status": "",
                "metadata-runtime": "",
                "metadata-studios": "",
                "metadata-country": "",
                "metadata-languages": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            "/medialist/movie?status=Planning&sort=release_date&direction=desc&layout=grid",
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

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_track_modal_renders_episode_plays_tab_for_tv(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        tv_item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="https://example.com/breaking-bad.jpg",
        )
        TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        tv_payload = {
            "media_id": "1396",
            "title": "Breaking Bad",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "image": "https://example.com/breaking-bad.jpg",
            "details": {"episodes": 3},
            "related": {
                "seasons": [
                    {"season_number": 1, "season_title": "Season 1"},
                ],
            },
        }
        mock_get_metadata.side_effect = lambda media_type, *_args, **_kwargs: (
            _tv_with_seasons_payload("1396", Sources.TMDB.value, title="Breaking Bad")
            if media_type == "tv_with_seasons"
            else tv_payload
        )
        mock_resolve_detail_metadata.return_value = MetadataResolutionResult(
            display_provider=Sources.TMDB.value,
            identity_provider=Sources.TMDB.value,
            mapping_status="identity",
            header_metadata=tv_payload,
            grouped_preview=None,
            provider_media_id="1396",
        )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "1396",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Episode Plays")
        self.assertEqual(
            response.context["episode_plays_form"].initial["first_episode_number"],
            1,
        )
        self.assertEqual(
            response.context["episode_plays_form"].initial["last_episode_number"],
            3,
        )
        self.assertEqual(
            response.context["episode_plays_form"]["distribution_mode"].value(),
            "air_date",
        )
        self.assertContains(response, "Air date", count=2)

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_track_modal_defaults_first_episode_to_season_one_over_specials(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        tv_payload = {
            "media_id": "1396",
            "title": "Breaking Bad",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "image": "https://example.com/breaking-bad.jpg",
            "details": {"episodes": 3},
            "related": {
                "seasons": [
                    {"season_number": 0, "season_title": "Specials"},
                    {"season_number": 1, "season_title": "Season 1"},
                ],
            },
        }
        tv_with_seasons = {
            **tv_payload,
            "season/0": {
                "season_number": 0,
                "season_title": "Specials",
                "title": "Breaking Bad",
                "image": "https://example.com/specials.jpg",
                "episodes": [
                    {
                        "episode_number": 1,
                        "name": "Special 1",
                        "air_date": "2023-12-01",
                        "runtime": 24,
                    },
                ],
            },
            "season/1": {
                "season_number": 1,
                "season_title": "Season 1",
                "title": "Breaking Bad",
                "image": "https://example.com/season1.jpg",
                "episodes": [
                    {
                        "episode_number": 1,
                        "name": "Episode 1",
                        "air_date": "2024-01-01",
                        "runtime": 24,
                    },
                    {
                        "episode_number": 2,
                        "name": "Episode 2",
                        "air_date": "2024-01-02",
                        "runtime": 24,
                    },
                ],
            },
        }
        mock_get_metadata.side_effect = lambda media_type, *_args, **_kwargs: (
            tv_with_seasons if media_type == "tv_with_seasons" else tv_payload
        )
        mock_resolve_detail_metadata.return_value = MetadataResolutionResult(
            display_provider=Sources.TMDB.value,
            identity_provider=Sources.TMDB.value,
            mapping_status="identity",
            header_metadata=tv_payload,
            grouped_preview=None,
            provider_media_id="1396",
        )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "1396",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["episode_plays_form"].initial["first_season_number"],
            1,
        )
        self.assertEqual(
            response.context["episode_plays_form"].initial["first_episode_number"],
            1,
        )

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_track_modal_renders_episode_plays_tab_for_grouped_anime(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        anime_item = Item.objects.create(
            media_id="9350138",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Frieren: Beyond Journey's End",
            image="https://example.com/frieren.jpg",
        )
        TV.objects.create(
            item=anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        anime_payload = {
            "media_id": "9350138",
            "title": "Frieren: Beyond Journey's End",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.TVDB.value,
            "image": "https://example.com/frieren.jpg",
            "details": {"episodes": 3},
            "related": {
                "seasons": [
                    {"season_number": 1, "season_title": "Season 1"},
                ],
            },
            "library_media_type": MediaTypes.ANIME.value,
            "identity_media_type": MediaTypes.TV.value,
        }
        mock_get_metadata.side_effect = lambda media_type, *_args, **_kwargs: (
            _tv_with_seasons_payload(
                "9350138",
                Sources.TVDB.value,
                title="Frieren: Beyond Journey's End",
            )
            if media_type == "tv_with_seasons"
            else anime_payload
        )
        mock_resolve_detail_metadata.return_value = MetadataResolutionResult(
            display_provider=Sources.TVDB.value,
            identity_provider=Sources.TVDB.value,
            mapping_status="identity",
            header_metadata=anime_payload,
            grouped_preview=None,
            provider_media_id="9350138",
        )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.TVDB.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "9350138",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Episode Plays")
        self.assertEqual(
            response.context["episode_plays_form"].initial["library_media_type"],
            MediaTypes.ANIME.value,
        )

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_track_modal_renders_mapped_episode_slice_for_flat_anime(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        mock_get_metadata.return_value = {"max_progress": 24}
        anime_item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        Anime.objects.create(
            item=anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=12,
        )
        base_metadata = {
            "media_id": "52991",
            "title": "Frieren",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "image": "https://example.com/frieren.jpg",
            "details": {"episodes": 12},
            "related": {},
        }
        grouped_preview = _tv_with_seasons_payload(
            "9350138",
            Sources.TVDB.value,
            title="Frieren: Beyond Journey's End",
            episode_count=24,
        )
        mock_get_metadata.return_value = base_metadata
        mock_resolve_detail_metadata.return_value = MetadataResolutionResult(
            display_provider=Sources.TVDB.value,
            identity_provider=Sources.MAL.value,
            mapping_status="mapped",
            header_metadata=base_metadata,
            grouped_preview=grouped_preview,
            provider_media_id="9350138",
            grouped_preview_target={
                "season_number": 1,
                "season_title": "Season 1",
                "episode_start": 13,
                "episode_end": 24,
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
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Episode Plays")
        self.assertContains(
            response,
            "This will migrate your MAL anime entry into grouped episode "
            "tracking before logging plays.",
        )
        self.assertEqual(
            response.context["episode_plays_form"].initial["first_episode_number"],
            13,
        )
        self.assertEqual(
            response.context["episode_plays_form"].initial["last_episode_number"],
            24,
        )

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_track_modal_hides_episode_plays_tab_for_unmapped_flat_anime(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        base_metadata = {
            "media_id": "52991",
            "title": "Frieren",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "image": "https://example.com/frieren.jpg",
            "details": {"episodes": 12},
            "related": {},
        }
        mock_get_metadata.return_value = base_metadata
        mock_resolve_detail_metadata.return_value = MetadataResolutionResult(
            display_provider=Sources.MAL.value,
            identity_provider=Sources.MAL.value,
            mapping_status="identity",
            header_metadata=base_metadata,
            grouped_preview=None,
            provider_media_id="52991",
            grouped_preview_target=None,
        )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "52991",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Episode Plays")
        self.assertFalse(response.context["episode_plays_tab_available"])


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

    def test_podcast_track_modal_can_force_standard_editor(self):
        """History cards should be able to request the full shared editor for podcast plays."""
        show = PodcastShow.objects.create(
            podcast_uuid="show-uuid-2",
            title="Show Title",
            image="http://example.com/show.jpg",
        )
        episode = PodcastEpisode.objects.create(
            show=show,
            episode_uuid="episode-uuid-2",
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
            status=Status.COMPLETED.value,
            progress=1800,
            score=8,
            notes="Needs a revisit",
        )

        response = self.client.get(
            reverse(
                "track_modal",
                kwargs={
                    "source": Sources.POCKETCASTS.value,
                    "media_type": MediaTypes.PODCAST.value,
                    "media_id": episode.episode_uuid,
                },
            )
            + "?standard_modal=1"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track.html")
        self.assertEqual(response.context["media"], podcast)
        self.assertContains(response, "General")
        self.assertContains(response, 'name="notes"', html=False)
        self.assertContains(response, 'name="score"', html=False)

    def test_podcast_show_track_modal_renders_episode_plays_tab(self):
        """Podcast show modal should expose bulk episode plays instead of mark-all CTA."""
        show = PodcastShow.objects.create(
            podcast_uuid="show-uuid-2",
            title="Show Title",
            image="http://example.com/show.jpg",
        )
        PodcastShowTracker.objects.create(
            user=self.user,
            show=show,
            status=Status.IN_PROGRESS.value,
        )
        PodcastEpisode.objects.create(
            show=show,
            episode_uuid="episode-uuid-2",
            title="Episode One",
            published=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
            duration=1200,
        )
        PodcastEpisode.objects.create(
            show=show,
            episode_uuid="episode-uuid-3",
            title="Episode Two",
            published=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
            duration=1500,
        )

        response = self.client.get(
            reverse("podcast_show_track_modal", kwargs={"show_id": show.id})
            + "?return_url=/details/pocketcasts/podcast/show-uuid-2/show-title",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_track.html")
        self.assertContains(response, "General")
        self.assertContains(response, "Episode Plays")
        self.assertNotContains(response, "Metadata")
        self.assertNotContains(response, "Mark All Played")
        self.assertContains(response, 'name="show_id"', html=False)
        self.assertEqual(
            response.context["episode_plays_form"].initial["first_episode_number"],
            1,
        )
        self.assertEqual(
            response.context["episode_plays_form"].initial["last_episode_number"],
            2,
        )
        self.assertTrue(response.context["episode_plays_domain"]["hideSeasonSelectors"])
