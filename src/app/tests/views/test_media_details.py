from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from app import statistics_cache
from app.models import (
    Album,
    AlbumTracker,
    Artist,
    ArtistTracker,
    Anime,
    BoardGame,
    Book,
    Comic,
    CreditRoleType,
    Episode,
    Game,
    Item,
    ItemTag,
    ItemPersonCredit,
    Manga,
    MetadataProviderPreference,
    MediaTypes,
    Music,
    Movie,
    Person,
    Podcast,
    PodcastEpisode,
    PodcastShow,
    PodcastShowTracker,
    Season,
    Sources,
    Status,
    Tag,
    TV,
    Track,
)
from app.services import game_lengths as game_length_services
from app.services.metadata_resolution import MetadataResolutionResult
from integrations.models import PlexAccount
from users.models import DateFormatChoices, RatingScaleChoices


class MediaDetailsViewTests(TestCase):
    """Test the media details views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def _use_iso_dates(self):
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])

    def _assert_activity_subtitle_without_stats_cards(
        self,
        response,
        primary_text,
        date_text,
        duration_text=None,
    ):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, primary_text)
        self.assertContains(response, date_text)
        if duration_text is not None:
            self.assertContains(response, duration_text)
        self.assertNotContains(response, "FIRST PLAYED")
        self.assertNotContains(response, "LAST PLAYED")
        self.assertNotContains(response, "WATCHED HOURS")
        self.assertNotContains(response, "TOTAL HOURS")
        self.assertNotContains(response, "AVG TIME")

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_view(self, mock_get_metadata):
        """Test the media details view."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Test Movie")
        self.assertContains(
            response,
            'class="order-1 mt-5 mb-6 flex flex-col gap-3 sm:order-2 sm:flex-row sm:flex-wrap sm:items-center"',
            html=False,
        )

        mock_get_metadata.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "238",
            Sources.TMDB.value,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_top_action_row_between_chips_and_description(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "synopsis": "Test overview",
            "score": 7.6,
            "score_count": 42000,
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('class="order-1 mb-6 flex flex-col gap-3 sm:order-2 sm:flex-row sm:flex-wrap sm:items-center"', content)
        self.assertIn('class="flex w-full items-center gap-2 sm:w-auto sm:flex-wrap"', content)
        self.assertIn(
            'class="inline-flex h-11 w-full items-center justify-center rounded-xl border border-white/10 bg-[#2a2f35] text-gray-100 shadow-sm transition-colors duration-200 hover:bg-[#343a40] cursor-pointer sm:size-11 sm:w-11"',
            content,
        )
        self.assertIn("Add to tracker", content)
        self.assertIn('title="Add to custom lists"', content)
        self.assertIn('title="Manage tags"', content)
        self.assertIn('title="Sync metadata with provider"', content)
        self.assertNotIn('<h2 class="text-xl font-bold mb-4">Actions</h2>', content)
        self.assertNotIn('mt-4 p-3 rounded-lg w-full flex items-center', content)
        self.assertIn("mobilePreviewText", content)
        self.assertIn("fullSynopsisText:", content)
        self.assertIn(
            'class="inline whitespace-nowrap align-baseline ml-1 text-[13px] font-medium text-indigo-400 hover:text-indigo-300 focus:outline-none transition-colors cursor-pointer"',
            content,
        )
        self.assertIn(
            'class="mt-2 inline-flex text-[13px] font-medium text-indigo-400 hover:text-indigo-300 focus:outline-none transition-colors cursor-pointer sm:hidden"',
            content,
        )
        self.assertIn(
            'class="hidden text-indigo-400 hover:text-indigo-300 text-sm mt-2 focus:outline-none transition-colors cursor-pointer sm:inline-flex"',
            content,
        )
        self.assertLess(content.index("tmdb-logo.png"), content.index("Add to tracker"))
        self.assertLess(content.index("Add to tracker"), content.index("Test overview"))

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_related_sections_use_mobile_card_grid_preferences(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Show",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "synopsis": "Test overview",
            "details": {},
            "related": {
                "seasons": [
                    {
                        "media_id": "238",
                        "title": "Test Show",
                        "media_type": MediaTypes.SEASON.value,
                        "source": Sources.TMDB.value,
                        "season_number": 1,
                        "season_title": "Season 1",
                        "image": "http://example.com/season1.jpg",
                    }
                ],
            },
            "cast": [
                {
                    "name": "Actor One",
                    "image": "http://example.com/person.jpg",
                    "role": "Lead",
                }
            ],
            "crew": [
                {
                    "name": "Crew One",
                    "image": "http://example.com/person2.jpg",
                    "department": "Directing",
                }
            ],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "238",
                    "title": "test-show",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('class="flex flex-col md:flex-row gap-8 md:gap-10 mb-2 md:mb-8"', content)
        self.assertIn('class="flex flex-col-reverse md:flex-row gap-0 md:gap-10"', content)
        self.assertIn('class="detail-media-grid"', content)
        self.assertIn("window.matchMedia('(max-width: 768px)').matches", content)
        self.assertIn("document.body.dataset.mobileGrid === 'comfortable' ? 4 : 6", content)
        self.assertIn('class="w-full md:w-1/4 md:max-w-[250px] mx-auto lg:mx-0"', content)
        self.assertIn('class="w-full md:w-3/4"', content)
        self.assertIn(
            'class="mt-4 inline-flex text-sm font-medium text-indigo-400 hover:text-indigo-300 focus:outline-none transition-colors cursor-pointer md:hidden"',
            content,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_notes_as_section_above_related_content(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "synopsis": "Test overview",
            "details": {},
            "related": {},
            "cast": [
                {
                    "name": "Actor One",
                    "image": "http://example.com/person.jpg",
                    "role": "Lead",
                }
            ],
            "crew": [],
        }
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            end_date=datetime(2026, 3, 20, 18, 0, tzinfo=UTC),
            notes="## Great notes\n\nThis movie rules.",
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertContains(response, '<h2 class="text-xl font-bold">Your Notes</h2>', html=False)
        self.assertContains(response, 'aria-label="Edit notes"', html=False)
        self.assertContains(
            response,
            'style="max-height: 12rem; overflow: hidden;"',
            html=False,
        )
        self.assertContains(
            response,
            ":style=\"isExpanded ? 'max-height: none; overflow: visible;' : 'max-height: 12rem; overflow: hidden;'\"",
            html=False,
        )
        self.assertLess(content.index("Test overview"), content.index("Your Notes"))
        self.assertLess(content.index("Your Notes"), content.index("Cast"))
        self.assertNotIn(">Edit<", content)
        self.assertNotIn("YOUR NOTES", content)

    @patch("app.services.music.needs_discography_sync", return_value=False)
    @patch("app.services.music_scrobble.dedupe_artist_albums")
    @patch("app.providers.musicbrainz.get_artist")
    def test_music_artist_details_renders_shared_media_details_template(
        self,
        mock_get_artist,
        _mock_dedupe_artist_albums,
        _mock_needs_discography_sync,
    ):
        artist = Artist.objects.create(
            name="Test Artist",
            musicbrainz_id="artist-mbid",
            image="http://example.com/artist.jpg",
            discography_synced_at=timezone.now(),
        )
        Album.objects.create(
            title="Debut Album",
            artist=artist,
            musicbrainz_release_id="release-mbid",
            release_date=datetime(2024, 1, 15, tzinfo=UTC).date(),
            image="http://example.com/album.jpg",
        )
        ArtistTracker.objects.create(
            user=self.user,
            artist=artist,
            status=Status.IN_PROGRESS.value,
            score=8,
            notes="Artist notes",
            start_date=datetime(2026, 1, 10, 18, 0, tzinfo=UTC),
        )
        mock_get_artist.return_value = {
            "type": "Group",
            "country": "US",
            "genres": [{"name": "indie"}],
            "tags": [],
            "rating": 4.2,
            "rating_count": 132,
            "bio": "Artist biography",
            "image": "http://example.com/artist.jpg",
        }

        response = self.client.get(
            reverse(
                "music_artist_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "test-artist",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")
        self.assertNotContains(response, "app/music_artist_detail.html")
        self.assertEqual(response.context["music_detail_kind"], "artist")
        self.assertContains(response, "Discography")
        self.assertContains(response, "Debut Album")
        self.assertContains(
            response,
            f'hx-get="{reverse("artist_track_modal", args=[artist.id])}?instance_id=',
            html=False,
        )

    @patch("app.services.music_scrobble.is_incomplete_album", return_value=False)
    @patch("app.services.music_scrobble.dedupe_artist_albums")
    def test_music_album_details_renders_shared_media_details_template(
        self,
        _mock_dedupe_artist_albums,
        _mock_is_incomplete_album,
    ):
        artist = Artist.objects.create(
            name="Test Artist",
            musicbrainz_id="artist-mbid",
            image="http://example.com/artist.jpg",
        )
        album = Album.objects.create(
            title="Debut Album",
            artist=artist,
            musicbrainz_release_id="release-mbid",
            tracks_populated=True,
            image="http://example.com/album.jpg",
            release_type="Album",
            release_date=datetime(2024, 1, 15, tzinfo=UTC).date(),
        )
        track = Track.objects.create(
            album=album,
            title="Track One",
            track_number=1,
            duration_ms=180000,
        )
        album_tracker = AlbumTracker.objects.create(
            user=self.user,
            album=album,
            status=Status.COMPLETED.value,
            score=7,
            notes="Album notes",
            start_date=datetime(2026, 2, 1, 18, 0, tzinfo=UTC),
            end_date=datetime(2026, 2, 2, 18, 0, tzinfo=UTC),
        )
        item = Item.objects.create(
            media_id="recording-1",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title="Track One",
            image="http://example.com/album.jpg",
        )
        Music.objects.create(
            item=item,
            user=self.user,
            artist=artist,
            album=album,
            track=track,
            status=Status.COMPLETED.value,
            start_date=datetime(2026, 2, 1, 18, 0, tzinfo=UTC),
            end_date=datetime(2026, 2, 2, 18, 0, tzinfo=UTC),
        )

        response = self.client.get(
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "test-artist",
                    "album_id": album.id,
                    "album_slug": "debut-album",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")
        self.assertNotContains(response, "app/music_album_detail.html")
        self.assertEqual(response.context["music_detail_kind"], "album")
        self.assertContains(response, "Track One")
        self.assertContains(response, artist.name)
        self.assertContains(response, "Debut Album")
        self.assertContains(
            response,
            f'hx-get="{reverse("album_track_modal", args=[album.id])}?instance_id={album_tracker.id}',
            html=False,
        )

    def test_legacy_music_artist_detail_redirects_to_canonical_route(self):
        artist = Artist.objects.create(name="Redirect Artist")

        response = self.client.get(reverse("artist_detail", args=[artist.id]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse(
                "music_artist_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "redirect-artist",
                },
            ),
        )

    def test_legacy_music_album_detail_redirects_to_canonical_route(self):
        artist = Artist.objects.create(name="Redirect Artist")
        album = Album.objects.create(title="Redirect Album", artist=artist)

        response = self.client.get(reverse("album_detail", args=[album.id]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "redirect-artist",
                    "album_id": album.id,
                    "album_slug": "redirect-album",
                },
            ),
        )

    def test_music_album_details_redirects_when_artist_path_is_wrong(self):
        artist = Artist.objects.create(name="Right Artist")
        wrong_artist = Artist.objects.create(name="Wrong Artist")
        album = Album.objects.create(title="Redirect Album", artist=artist)

        response = self.client.get(
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": wrong_artist.id,
                    "artist_slug": "wrong-artist",
                    "album_id": album.id,
                    "album_slug": "redirect-album",
                },
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "right-artist",
                    "album_id": album.id,
                    "album_slug": "redirect-album",
                },
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_links_action_with_source_and_external_links(
        self,
        mock_get_metadata,
    ):
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "source_url": "https://www.themoviedb.org/movie/238",
            "external_links": {
                "IMDb": "https://www.imdb.com/title/tt0111161/",
            },
            "synopsis": "Test overview",
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('title="View source and external links"', content)
        self.assertIn("Source", content)
        self.assertIn("The Movie Database", content)
        self.assertIn("External links", content)
        self.assertIn("Letterboxd", content)
        self.assertIn("IMDb", content)
        self.assertIn("imdb-logo.png", content)
        self.assertIn("https://www.themoviedb.org/movie/238", content)
        self.assertIn("https://www.imdb.com/title/tt0111161/", content)
        self.assertNotIn("Tracking Source", content)
        self.assertNotIn("Metadata Source", content)
        self.assertNotIn("EXTERNAL LINKS", content)
        self.assertEqual(
            response.context["detail_link_sections"],
            [
                {
                    "title": "Source",
                    "entries": [
                        {
                            "label": "The Movie Database",
                            "url": "https://www.themoviedb.org/movie/238",
                            "chip_classes": "border-cyan-400/18 bg-cyan-500/[0.07]",
                            "badge_classes": "border-cyan-400/28 bg-cyan-500/14",
                            "accent_classes": "text-cyan-100",
                            "logo_src": "/static/img/tmdb-logo.png",
                            "fallback_text": "TMDB",
                        }
                    ],
                },
                {
                    "title": "External links",
                    "entries": [
                        {
                            "label": "Letterboxd",
                            "url": "https://letterboxd.com/tmdb/238",
                            "chip_classes": "border-emerald-400/18 bg-emerald-500/[0.07]",
                            "badge_classes": "border-emerald-400/28 bg-emerald-500/14",
                            "accent_classes": "text-emerald-100",
                            "logo_src": None,
                            "fallback_text": "LB",
                        },
                        {
                            "label": "IMDb",
                            "url": "https://www.imdb.com/title/tt0111161/",
                            "chip_classes": "border-amber-400/18 bg-amber-500/[0.07]",
                            "badge_classes": "border-amber-400/28 bg-amber-500/14",
                            "accent_classes": "text-amber-100",
                            "logo_src": "/static/img/imdb-logo.png",
                            "fallback_text": "IMDb",
                        },
                    ],
                },
            ],
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_uses_tvdb_and_wikidata_logos_in_link_sections(
        self,
        mock_get_metadata,
    ):
        mock_get_metadata.return_value = {
            "media_id": "81189",
            "title": "Test Show",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TVDB.value,
            "image": "http://example.com/image.jpg",
            "source_url": "https://www.thetvdb.com/dereferrer/series/81189",
            "external_links": {
                "Wikidata": "https://www.wikidata.org/wiki/Q83495",
            },
            "synopsis": "Test overview",
            "details": {},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TVDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "81189",
                    "title": "test-show",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("tvdb-logo.png", content)
        self.assertIn("wikidata-logo.png", content)

        source_entry = response.context["detail_link_sections"][0]["entries"][0]
        external_entry = response.context["detail_link_sections"][1]["entries"][0]
        self.assertEqual(source_entry["label"], "TheTVDB")
        self.assertEqual(source_entry["chip_classes"], "border-teal-400/18 bg-teal-500/[0.07]")
        self.assertEqual(source_entry["logo_src"], "/static/img/tvdb-logo.png")
        self.assertEqual(external_entry["label"], "Wikidata")
        self.assertEqual(external_entry["chip_classes"], "border-sky-400/18 bg-sky-500/[0.07]")
        self.assertEqual(external_entry["logo_src"], "/static/img/wikidata-logo.png")

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_uses_mal_logo_in_link_sections(
        self,
        mock_get_metadata,
    ):
        mock_get_metadata.return_value = {
            "media_id": "52991",
            "title": "Frieren",
            "media_type": MediaTypes.MANGA.value,
            "source": Sources.MAL.value,
            "image": "http://example.com/image.jpg",
            "source_url": "https://myanimelist.net/manga/52991/Sousou_no_Frieren",
            "synopsis": "Test overview",
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.MANGA.value,
                    "media_id": "52991",
                    "title": "frieren",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "myanimelist-logo.svg")

        source_entry = response.context["detail_link_sections"][0]["entries"][0]
        self.assertEqual(source_entry["label"], "MyAnimeList")
        self.assertEqual(source_entry["chip_classes"], "border-indigo-400/18 bg-indigo-500/[0.07]")
        self.assertEqual(source_entry["logo_src"], "/static/img/myanimelist-logo.svg")

    @patch("app.views._queue_game_lengths_refresh", return_value=True)
    @patch("app.providers.services.get_media_metadata")
    def test_media_details_uses_igdb_and_hltb_logos_in_link_sections(
        self,
        mock_get_metadata,
        _mock_queue_game_lengths_refresh,
    ):
        mock_get_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "image": "http://example.com/image.jpg",
            "source_url": "https://www.igdb.com/games/dispatch",
            "external_links": {
                "HowLongToBeat": "https://howlongtobeat.com/game/160618",
            },
            "synopsis": "Test overview",
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                    "title": "dispatch",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("igdb-logo.png", content)
        self.assertIn("hltb-logo.png", content)

        source_entry = response.context["detail_link_sections"][0]["entries"][0]
        external_entry = response.context["detail_link_sections"][1]["entries"][0]
        self.assertEqual(source_entry["label"], "Internet Game Database")
        self.assertEqual(source_entry["chip_classes"], "border-orange-400/18 bg-orange-500/[0.07]")
        self.assertEqual(source_entry["logo_src"], "/static/img/igdb-logo.png")
        self.assertEqual(external_entry["label"], "HowLongToBeat")
        self.assertEqual(external_entry["chip_classes"], "border-amber-400/18 bg-amber-500/[0.07]")
        self.assertEqual(external_entry["logo_src"], "/static/img/hltb-logo.png")

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_tag_preview_sections_next_to_links(
        self,
        mock_get_metadata,
    ):
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
            genres=["Drama", "Mystery"],
        )
        tag = Tag.objects.create(user=self.user, name="Prestige TV")
        ItemTag.objects.create(tag=tag, item=item)

        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "source_url": "https://www.themoviedb.org/movie/238",
            "genres": ["Drama", "Mystery"],
            "synopsis": "Test overview",
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertLess(
            content.index('title="View source and external links"'),
            content.index('title="Manage tags"'),
        )
        self.assertLess(
            content.index('title="Manage tags"'),
            content.index('title="Add to custom lists"'),
        )
        self.assertIn("Genres", content)
        self.assertIn("Tags", content)
        self.assertIn("Drama", content)
        self.assertIn("Mystery", content)
        self.assertIn("Prestige TV", content)
        self.assertEqual(
            response.context["detail_tag_sections"],
            [
                {
                    "title": "Genres",
                    "entries": [
                        {
                            "label": "Drama",
                            "chip_classes": "border-violet-400/18 bg-violet-500/[0.07] text-violet-100",
                        },
                        {
                            "label": "Mystery",
                            "chip_classes": "border-violet-400/18 bg-violet-500/[0.07] text-violet-100",
                        },
                    ],
                },
                {
                    "title": "Tags",
                    "entries": [
                        {
                            "label": "Prestige TV",
                            "chip_classes": "border-slate-400/18 bg-slate-500/[0.07] text-slate-100",
                        }
                    ],
                },
            ],
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_hides_streaming_section_when_watch_provider_region_disabled(
        self,
        mock_get_metadata,
    ):
        self.user.watch_provider_region = "UNSET"
        self.user.save(update_fields=["watch_provider_region"])
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "providers": {
                "US": {
                    "flatrate": [
                        {
                            "provider_name": "Netflix",
                            "logo_path": "/netflix.jpg",
                        },
                    ],
                },
            },
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "STREAMING")
        self.assertNotContains(
            response,
            "Watch provider region is not configured.",
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_prefers_stored_item_image_over_provider_image(
        self,
        mock_get_metadata,
    ):
        item = Item.objects.create(
            media_id="377938",
            source=Sources.HARDCOVER.value,
            media_type=MediaTypes.BOOK.value,
            title="The Lord of the Rings",
            image="https://images.example.com/custom-cover.jpg",
        )
        mock_get_metadata.return_value = {
            "media_id": "377938",
            "title": "The Lord of the Rings",
            "media_type": MediaTypes.BOOK.value,
            "source": Sources.HARDCOVER.value,
            "image": "https://images.example.com/provider-cover.jpg",
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.HARDCOVER.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "377938",
                    "title": "the-lord-of-the-rings",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media"]["image"], item.image)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_repairs_stringified_title_payloads_on_existing_item(
        self,
        mock_get_metadata,
    ):
        item = Item.objects.create(
            media_id="81189",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="{'language': 'jpn', 'name': 'Sōdo Āto Onrain'}",
            original_title="{'language': 'jpn', 'name': 'Sōdo Āto Onrain'}",
            localized_title="{'language': 'jpn', 'name': 'Sōdo Āto Onrain'}",
            image="https://example.com/cover.jpg",
        )
        mock_get_metadata.return_value = {
            "media_id": "81189",
            "title": {"language": "jpn", "name": "Sōdo Āto Onrain"},
            "original_title": {"language": "jpn", "name": "Sōdo Āto Onrain"},
            "localized_title": {"language": "jpn", "name": "Sōdo Āto Onrain"},
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "image": "https://example.com/provider-cover.jpg",
            "details": {},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "81189",
                    "title": "sword-art-online",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sōdo Āto Onrain")
        self.assertNotContains(response, "{'language': 'jpn', 'name': 'Sōdo Āto Onrain'}")

        item.refresh_from_db()
        self.assertEqual(item.title, "Sōdo Āto Onrain")
        self.assertEqual(item.original_title, "Sōdo Āto Onrain")
        self.assertEqual(item.localized_title, "Sōdo Āto Onrain")

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_refreshes_stale_tvdb_titles_with_english_localized_text(
        self,
        mock_get_metadata,
    ):
        item = Item.objects.create(
            media_id="259640",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            title="ソードアート・オンライン",
            original_title="ソードアート・オンライン",
            localized_title="ソードアート・オンライン",
            image="https://example.com/cover.jpg",
        )
        mock_get_metadata.return_value = {
            "media_id": "259640",
            "title": "Sword Art Online",
            "original_title": "ソードアート・オンライン",
            "localized_title": "Sword Art Online",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TVDB.value,
            "image": "https://example.com/provider-cover.jpg",
            "synopsis": "English overview",
            "details": {},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TVDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "259640",
                    "title": "sword-art-online",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sword Art Online")

        item.refresh_from_db()
        self.assertEqual(item.title, "Sword Art Online")
        self.assertEqual(item.original_title, "ソードアート・オンライン")
        self.assertEqual(item.localized_title, "Sword Art Online")

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_persists_movie_recommendation_metadata(self, mock_get_metadata):
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "provider_keywords": ["Whodunit", "Holiday"],
            "provider_certification": "PG",
            "provider_collection_id": "44",
            "provider_collection_name": "Mystery Collection",
            "details": {
                "country": "US",
                "studios": ["Pixar Animation Studios"],
                "certification": "PG",
            },
            "cast": [],
            "crew": [],
            "studios_full": [],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.provider_keywords, ["Whodunit", "Holiday"])

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_trakt_score_card_when_data_exists(self, mock_get_metadata):
        Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
            trakt_rating=7.88048,
            trakt_rating_count=123456,
            trakt_popularity_rank=9,
            trakt_popularity_score=3210.5,
            trakt_popularity_fetched_at=timezone.now(),
        )
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "trakt-logo.svg")
        self.assertContains(response, "7.8")
        self.assertNotContains(response, "7.88048")
        self.assertContains(response, "123,456 ratings")
        self.assertContains(
            response,
            'class="order-2 mt-0 mb-5 flex w-full items-center justify-between gap-0.5 sm:order-1 sm:mt-4 sm:flex-wrap sm:justify-start sm:gap-2"',
            html=False,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_hides_trakt_score_card_without_data(self, mock_get_metadata):
        Item.objects.create(
            media_id="239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="No Trakt Movie",
            image="http://example.com/image.jpg",
        )
        mock_get_metadata.return_value = {
            "media_id": "239",
            "title": "No Trakt Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "239",
                    "title": "no-trakt-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "trakt-logo.svg")

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_source_score_chip_with_tmdb_logo(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "score": 7.6,
            "score_count": 42000,
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "tmdb-logo.png")
        self.assertContains(response, "7.6")
        self.assertContains(response, "42,000 votes")
        self.assertContains(
            response,
            'class="order-2 mt-0 mb-5 flex w-full items-center justify-between gap-0.5 sm:order-1 sm:mt-4 sm:flex-wrap sm:justify-start sm:gap-2"',
            html=False,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_source_score_chip_with_mal_logo(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "52991",
            "title": "Frieren",
            "media_type": MediaTypes.MANGA.value,
            "source": Sources.MAL.value,
            "image": "http://example.com/image.jpg",
            "score": 8.9,
            "score_count": 123456,
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.MANGA.value,
                    "media_id": "52991",
                    "title": "frieren",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "myanimelist-logo.svg")
        self.assertContains(response, "8.9")
        self.assertContains(response, "123,456 votes")

    @patch("app.views._queue_game_lengths_refresh", return_value=True)
    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_source_score_chip_with_igdb_logo(
        self,
        mock_get_metadata,
        _mock_queue_game_lengths_refresh,
    ):
        mock_get_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "image": "http://example.com/image.jpg",
            "score": 83,
            "score_count": 12000,
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                    "title": "dispatch",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "igdb-logo.png")
        self.assertContains(response, "83")
        self.assertContains(response, "12,000 votes")

    @patch("app.providers.services.get_media_metadata")
    def test_tv_media_details_uses_same_title_spacing_as_score_chips(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "1668",
            "title": "Test TV Show",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "score": 7.6,
            "score_count": 42000,
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<div class="mb-3 sm:mb-1 text-center md:text-start">',
            html=False,
        )
        self.assertContains(response, '<h1 class="text-3xl font-bold">Test TV Show</h1>', html=False)

    @patch("app.providers.services.get_media_metadata")
    def test_tv_media_details_renders_progress_and_date_subtitle_without_history_card(
        self,
        mock_get_metadata,
    ):
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])
        item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
            image="http://example.com/image.jpg",
        )
        tv = TV.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Season 1",
            image="http://example.com/season.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        episode_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode 1",
            image="http://example.com/episode1.jpg",
            season_number=1,
            episode_number=1,
            runtime_minutes=45,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
        )
        mock_get_metadata.return_value = {
            "media_id": "1668",
            "title": "Test TV Show",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 8,
            "details": {},
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertContains(response, "Progress: 1/8")
        self.assertContains(response, "2026-03-01 - 2026-03-12")
        self.assertContains(response, "1h 30min watched")
        self.assertIn('<div class="mb-3 sm:mb-1 text-center md:text-start">', content)
        self.assertIn(
            'class="flex items-center justify-center gap-0.5 whitespace-nowrap text-[13px] tracking-[-0.01em] sm:hidden"',
            content,
        )
        self.assertIn("1h 30min (1/8)", content)
        self.assertIn('class="hidden flex-wrap items-center justify-center gap-y-1 sm:flex md:justify-start"', content)
        self.assertNotContains(response, "Your History")
        self.assertNotContains(response, "FIRST PLAYED")
        self.assertNotContains(response, "LAST PLAYED")
        self.assertNotContains(response, "WATCHED HOURS")

    @patch("app.providers.services.get_media_metadata")
    def test_movie_media_details_renders_watch_subtitle_above_score_chips(
        self,
        mock_get_metadata,
    ):
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "score": 7.6,
            "score_count": 42000,
            "details": {},
            "related": {},
        }
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
            runtime_minutes=95,
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 1, 14, 0, tzinfo=UTC),
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            start_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 12, 14, 0, tzinfo=UTC),
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["play_stats"]["total_minutes"], 190)
        self.assertContains(response, "Watched 2 times")
        self.assertContains(response, "2026-03-01 - 2026-03-12")
        self.assertContains(response, "3h 10min watched")
        self.assertContains(
            response,
            'class="order-2 mt-0 mb-5 flex w-full items-center justify-between gap-0.5 sm:order-1 sm:mt-4 sm:flex-wrap sm:justify-start sm:gap-2"',
            html=False,
        )
        self.assertContains(response, 'aria-label="More tracking actions"', html=False)
        self.assertContains(response, "Add new entry")
        self.assertContains(response, '"is_create": true', html=False)
        self.assertNotContains(response, "Your History")
        self.assertNotContains(response, "FIRST PLAYED")
        self.assertNotContains(response, "LAST PLAYED")
        self.assertNotContains(response, "TOTAL HOURS")

    @patch("app.providers.services.get_media_metadata")
    def test_movie_media_details_uses_play_date_range_when_repeats_only_have_end_dates(
        self,
        mock_get_metadata,
    ):
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "score": 7.6,
            "score_count": 42000,
            "details": {},
            "related": {},
        }
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
            runtime_minutes=95,
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            end_date=datetime(2019, 11, 19, 21, 0, tzinfo=UTC),
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            end_date=datetime(2020, 11, 28, 20, 0, tzinfo=UTC),
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            end_date=datetime(2025, 11, 28, 19, 0, tzinfo=UTC),
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["play_stats"]["first_played"],
            datetime(2019, 11, 19, 21, 0, tzinfo=UTC),
        )
        self.assertEqual(
            response.context["play_stats"]["last_played"],
            datetime(2025, 11, 28, 19, 0, tzinfo=UTC),
        )
        self.assertContains(response, "Watched 3 times")
        self.assertContains(response, "2019-11-19 - 2025-11-28")

    @patch("app.providers.services.get_media_metadata")
    def test_reading_media_details_render_activity_subtitle_without_stats_cards(
        self,
        mock_get_metadata,
    ):
        self._use_iso_dates()
        cases = [
            (
                MediaTypes.BOOK.value,
                Sources.OPENLIBRARY.value,
                "OL100M",
                "Tracked Book",
                Book,
            ),
            (
                MediaTypes.MANGA.value,
                Sources.MANGAUPDATES.value,
                "72274276213",
                "Tracked Manga",
                Manga,
            ),
            (
                MediaTypes.COMIC.value,
                Sources.COMICVINE.value,
                "4000-1",
                "Tracked Comic",
                Comic,
            ),
        ]

        for media_type, source, media_id, title, model in cases:
            with self.subTest(media_type=media_type):
                mock_get_metadata.return_value = {
                    "media_id": media_id,
                    "title": title,
                    "media_type": media_type,
                    "source": source,
                    "image": "http://example.com/cover.jpg",
                    "max_progress": 320,
                    "details": {},
                    "related": {},
                }
                item = Item.objects.create(
                    media_id=media_id,
                    source=source,
                    media_type=media_type,
                    title=title,
                    image="http://example.com/cover.jpg",
                )
                model.objects.create(
                    item=item,
                    user=self.user,
                    status=Status.IN_PROGRESS.value,
                    progress=120,
                    start_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
                    end_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
                )

                response = self.client.get(
                    reverse(
                        "media_details",
                        kwargs={
                            "source": source,
                            "media_type": media_type,
                            "media_id": media_id,
                            "title": title.lower().replace(" ", "-"),
                        },
                    ),
                )

                self._assert_activity_subtitle_without_stats_cards(
                    response,
                    "Progress: 120/320",
                    "2026-03-01 - 2026-03-12",
                )

    @patch("app.providers.services.get_media_metadata")
    def test_game_media_details_renders_activity_subtitle_without_stats_cards(
        self,
        mock_get_metadata,
    ):
        self._use_iso_dates()
        mock_get_metadata.return_value = {
            "media_id": "game-123",
            "title": "Tracked Game",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "image": "http://example.com/game.jpg",
            "max_progress": 1000,
            "details": {},
            "related": {},
        }
        item = Item.objects.create(
            media_id="game-123",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Tracked Game",
            image="http://example.com/game.jpg",
            provider_game_lengths={"igdb": {"summary": {"normally_seconds": 8100}}},
            provider_game_lengths_source="igdb",
        )
        Game.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=135,
            start_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "game-123",
                    "title": "tracked-game",
                },
            ),
        )

        self._assert_activity_subtitle_without_stats_cards(
            response,
            "Progress: 2h 15min",
            "2026-03-01 - 2026-03-12",
        )

    @patch("app.providers.services.get_media_metadata")
    def test_boardgame_media_details_renders_activity_subtitle_without_stats_cards(
        self,
        mock_get_metadata,
    ):
        self._use_iso_dates()
        mock_get_metadata.return_value = {
            "media_id": "13",
            "title": "Tracked Board Game",
            "media_type": MediaTypes.BOARDGAME.value,
            "source": Sources.BGG.value,
            "image": "http://example.com/boardgame.jpg",
            "max_progress": 20,
            "details": {},
            "related": {},
        }
        item = Item.objects.create(
            media_id="13",
            source=Sources.BGG.value,
            media_type=MediaTypes.BOARDGAME.value,
            title="Tracked Board Game",
            image="http://example.com/boardgame.jpg",
        )
        BoardGame.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=7,
            start_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.BGG.value,
                    "media_type": MediaTypes.BOARDGAME.value,
                    "media_id": "13",
                    "title": "tracked-board-game",
                },
            ),
        )

        self._assert_activity_subtitle_without_stats_cards(
            response,
            "Progress: 7 plays",
            "2026-03-01 - 2026-03-12",
        )

    @patch("app.providers.services.get_media_metadata")
    def test_music_media_details_renders_activity_subtitle_without_stats_cards(
        self,
        mock_get_metadata,
    ):
        self._use_iso_dates()
        mock_get_metadata.return_value = {
            "media_id": "track-1",
            "title": "Tracked Song",
            "media_type": MediaTypes.MUSIC.value,
            "source": Sources.MUSICBRAINZ.value,
            "image": "http://example.com/track.jpg",
            "details": {},
            "related": {},
        }
        item = Item.objects.create(
            media_id="track-1",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title="Tracked Song",
            image="http://example.com/track.jpg",
            runtime_minutes=4,
        )
        Music.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=3,
            start_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 1, 12, 10, tzinfo=UTC),
        )
        Music.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=4,
            start_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 12, 12, 10, tzinfo=UTC),
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MUSICBRAINZ.value,
                    "media_type": MediaTypes.MUSIC.value,
                    "media_id": "track-1",
                    "title": "tracked-song",
                },
            ),
        )

        self._assert_activity_subtitle_without_stats_cards(
            response,
            "Progress: 7 plays",
            "2026-03-01 - 2026-03-12",
            "28min listened",
        )

    def test_podcast_show_media_details_renders_activity_subtitle(self):
        self._use_iso_dates()
        show = PodcastShow.objects.create(
            podcast_uuid="itunes:1002937870",
            title="Tracked Podcast",
            author="Host",
            image="http://example.com/podcast.jpg",
            rss_feed_url="",
        )
        PodcastShowTracker.objects.create(
            user=self.user,
            show=show,
            status=Status.IN_PROGRESS.value,
        )
        episode_one = PodcastEpisode.objects.create(
            show=show,
            episode_uuid="pod-ep-1",
            title="Episode One",
            duration=1800,
        )
        episode_two = PodcastEpisode.objects.create(
            show=show,
            episode_uuid="pod-ep-2",
            title="Episode Two",
            duration=2700,
        )
        PodcastEpisode.objects.create(
            show=show,
            episode_uuid="pod-ep-3",
            title="Episode Three",
            duration=1800,
        )
        item_one = Item.objects.create(
            media_id="pod-ep-1",
            source=Sources.POCKETCASTS.value,
            media_type=MediaTypes.PODCAST.value,
            title="Episode One",
            image="http://example.com/podcast.jpg",
        )
        item_two = Item.objects.create(
            media_id="pod-ep-2",
            source=Sources.POCKETCASTS.value,
            media_type=MediaTypes.PODCAST.value,
            title="Episode Two",
            image="http://example.com/podcast.jpg",
        )
        Podcast.objects.create(
            item=item_one,
            user=self.user,
            show=show,
            episode=episode_one,
            status=Status.COMPLETED.value,
            progress=1800,
            start_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 1, 12, 30, tzinfo=UTC),
        )
        Podcast.objects.create(
            item=item_two,
            user=self.user,
            show=show,
            episode=episode_two,
            status=Status.COMPLETED.value,
            progress=2700,
            start_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 12, 12, 45, tzinfo=UTC),
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.POCKETCASTS.value,
                    "media_type": MediaTypes.PODCAST.value,
                    "media_id": show.podcast_uuid,
                    "title": "tracked-podcast",
                },
            ),
        )

        self._assert_activity_subtitle_without_stats_cards(
            response,
            "Progress: 2/3",
            "2026-03-01 - 2026-03-12",
            "1h 15min listened",
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_your_score_chip_with_edit_rating(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "details": {},
            "related": {},
        }
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            score=8,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edit rating")
        self.assertContains(response, "x-text=\"formatRating(rating)\">8<", html=False)
        self.assertContains(response, "x-text=\"rating ? 'Edit rating' : 'Add rating'\"", html=False)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_your_score_chip_with_five_point_scale_suffix(
        self,
        mock_get_metadata,
    ):
        self.user.rating_scale = RatingScaleChoices.FIVE.value
        self.user.save(update_fields=["rating_scale"])
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "details": {},
            "related": {},
        }
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            score=8,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edit rating")
        self.assertContains(response, "ratingScaleMax: 5", html=False)
        self.assertContains(response, "x-text=\"formatRating(rating)\">4/5<", html=False)
        self.assertContains(response, "x-text=\"rating ? 'Edit rating' : 'Add rating'\"", html=False)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_your_score_chip_with_add_rating_when_empty(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "max_progress": 1,
            "details": {},
            "related": {},
        }
        item = Item.objects.create(
            media_id="238",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Test Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=item,
            user=self.user,
            status=Status.PLANNING.value,
            score=None,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add rating")
        self.assertNotContains(response, "Click to edit")
        self.assertContains(response, "x-text=\"rating ? 'Edit rating' : 'Add rating'\"", html=False)

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_anime_details_keep_source_labels_but_move_metadata_controls_to_modal(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        """Flat anime details keep source context while metadata controls live in the modal."""
        base_metadata = {
            "media_id": "52991",
            "title": "Frieren",
            "original_title": "Sousou no Frieren",
            "localized_title": "Frieren",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "source_url": "https://myanimelist.net/anime/52991",
            "external_links": {
                "TVDB": "https://www.thetvdb.com/dereferrer/series/9350138",
            },
            "display_source_url": "https://www.thetvdb.com/dereferrer/series/9350138",
            "max_progress": 28,
            "image": "https://example.com/frieren.jpg",
            "synopsis": "A mage looks back.",
            "details": {"episodes": 28},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }
        mock_get_metadata.return_value = base_metadata
        item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        Anime.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=1,
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
                "original_title": "Sousou no Frieren",
                "localized_title": "Frieren: Beyond Journey's End",
                "related": {
                    "seasons": [
                        {
                            "season_number": 1,
                            "max_progress": None,
                            "episode_count": 28,
                            "first_air_date": timezone.now(),
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
                "media_details",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "52991",
                    "title": "frieren",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tracking Source")
        self.assertContains(response, "Metadata Source")
        self.assertContains(response, "https://myanimelist.net/anime/52991")
        self.assertContains(response, "https://www.thetvdb.com/dereferrer/series/9350138")
        self.assertNotContains(response, "Metadata Provider")
        self.assertNotContains(response, "Grouped Series Preview")
        self.assertNotContains(response, "Migrate to Grouped Series")
        self.assertTrue(response.context["can_migrate_grouped_anime"])

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_flat_anime_media_details_render_tv_style_subtitle(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])
        base_metadata = {
            "media_id": "52991",
            "title": "Frieren",
            "original_title": "Sousou no Frieren",
            "localized_title": "Frieren",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "source_url": "https://myanimelist.net/anime/52991",
            "max_progress": 28,
            "image": "https://example.com/frieren.jpg",
            "synopsis": "A mage looks back.",
            "details": {"episodes": 28, "runtime": "24m"},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }
        mock_get_metadata.return_value = base_metadata
        item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
            runtime_minutes=24,
        )
        Anime.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=3,
            start_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
            end_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
        )
        mock_resolve_detail_metadata.return_value = MetadataResolutionResult(
            display_provider=Sources.MAL.value,
            identity_provider=Sources.MAL.value,
            mapping_status="identity",
            header_metadata=base_metadata,
            grouped_preview=None,
            provider_media_id="52991",
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "52991",
                    "title": "frieren",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Progress: 3/28")
        self.assertContains(response, "2026-03-01 - 2026-03-12")
        self.assertContains(response, "1h 12min watched")
        self.assertContains(
            response,
            'class="mt-4 mb-5 flex flex-wrap gap-2"',
            html=False,
        )
        self.assertNotContains(response, "FIRST PLAYED")
        self.assertNotContains(response, "LAST PLAYED")
        self.assertNotContains(response, "WATCHED HOURS")

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_flat_mal_anime_details_explain_missing_episode_cards(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        base_metadata = {
            "media_id": "527",
            "title": "Pokemon",
            "original_title": "Pokemon",
            "localized_title": "Pokemon",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "source_url": "https://myanimelist.net/anime/527",
            "max_progress": 276,
            "image": "https://example.com/pokemon.jpg",
            "synopsis": "Pocket monsters.",
            "details": {"episodes": 276},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }
        mock_get_metadata.return_value = base_metadata
        item = Item.objects.create(
            media_id="527",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Pokemon",
            image="https://example.com/pokemon.jpg",
        )
        mock_resolve_detail_metadata.side_effect = lambda *_args, **_kwargs: MetadataResolutionResult(
            display_provider=Sources.MAL.value,
            identity_provider=Sources.MAL.value,
            mapping_status="identity",
            header_metadata={
                **base_metadata,
                "details": dict(base_metadata["details"]),
                "related": dict(base_metadata["related"]),
                "cast": list(base_metadata["cast"]),
                "crew": list(base_metadata["crew"]),
                "studios_full": list(base_metadata["studios_full"]),
            },
            grouped_preview=None,
            provider_media_id=item.media_id,
            grouped_preview_target=None,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "527",
                    "title": "pokemon",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Episode cards are not available from MyAnimeList metadata.",
        )
        self.assertContains(
            response,
            "Switch the metadata provider to TVDB or TMDB from Add to tracker",
        )

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.views.metadata_resolution.anime_mapping.find_entries_for_mal_id")
    @patch("app.views.anime_mapping.resolve_provider_series_id")
    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_flat_mal_anime_details_render_cross_season_episode_cards(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
        mock_resolve_provider_series_id,
        mock_find_entries_for_mal_id,
    ):
        base_metadata = {
            "media_id": "527",
            "title": "Pokemon",
            "original_title": "Pokemon",
            "localized_title": "Pokemon",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "source_url": "https://myanimelist.net/anime/527",
            "max_progress": 4,
            "image": "https://example.com/pokemon.jpg",
            "synopsis": "Pocket monsters.",
            "details": {"episodes": 4},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }
        grouped_series_metadata = {
            "media_id": "76703",
            "source": Sources.TVDB.value,
            "media_type": MediaTypes.ANIME.value,
            "title": "Pokemon",
            "related": {
                "seasons": [
                    {
                        "season_number": 1,
                        "episode_count": 2,
                    },
                    {
                        "season_number": 2,
                        "episode_count": 2,
                    },
                ],
            },
        }
        grouped_payload = grouped_series_metadata | {
            "season/1": {
                "media_id": "76703",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "season_title": "Indigo League",
                "details": {"episodes": 2},
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": "1997-04-01",
                        "image": "https://example.com/pokemon-ep1.jpg",
                        "name": "Pokemon, I Choose You!",
                        "overview": "Episode one.",
                        "runtime": 24,
                    },
                    {
                        "episode_number": 2,
                        "air_date": "1997-04-08",
                        "image": "https://example.com/pokemon-ep2.jpg",
                        "name": "Pokemon Emergency!",
                        "overview": "Episode two.",
                        "runtime": 24,
                    },
                ],
            },
            "season/2": {
                "media_id": "76703",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 2,
                "season_title": "Orange Islands",
                "details": {"episodes": 2},
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": "1999-01-28",
                        "image": "https://example.com/pokemon-ep3.jpg",
                        "name": "Pallet Party Panic",
                        "overview": "Episode three.",
                        "runtime": 24,
                    },
                    {
                        "episode_number": 2,
                        "air_date": "1999-02-04",
                        "image": "https://example.com/pokemon-ep4.jpg",
                        "name": "A Scare in the Air",
                        "overview": "Episode four.",
                        "runtime": 24,
                    },
                ],
            },
        }

        def metadata_side_effect(
            media_type,
            media_id,
            source,
            season_numbers=None,
            episode_number=None,
        ):
            del media_id, episode_number
            if media_type == MediaTypes.ANIME.value and source == Sources.TVDB.value:
                return grouped_series_metadata
            if media_type == "tv_with_seasons" and source == Sources.TVDB.value:
                self.assertEqual(season_numbers, [1, 2])
                return grouped_payload
            return base_metadata

        mock_get_metadata.side_effect = metadata_side_effect
        mock_resolve_provider_series_id.return_value = "76703"
        mock_find_entries_for_mal_id.return_value = [
            {
                "tvdb_id": 76703,
                "tvdb_season": -1,
                "tvdb_epoffset": 0,
                "mal_id": 527,
            },
        ]
        item = Item.objects.create(
            media_id="527",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Pokemon",
            image="https://example.com/pokemon.jpg",
        )
        mock_resolve_detail_metadata.side_effect = lambda *_args, **_kwargs: MetadataResolutionResult(
            display_provider=Sources.MAL.value,
            identity_provider=Sources.MAL.value,
            mapping_status="identity",
            header_metadata={
                **base_metadata,
                "details": dict(base_metadata["details"]),
                "related": dict(base_metadata["related"]),
                "cast": list(base_metadata["cast"]),
                "crew": list(base_metadata["crew"]),
                "studios_full": list(base_metadata["studios_full"]),
            },
            grouped_preview=None,
            provider_media_id=item.media_id,
            grouped_preview_target=None,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "527",
                    "title": "pokemon",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pokemon, I Choose You!")
        self.assertContains(response, "Pokemon Emergency!")
        self.assertContains(response, "Pallet Party Panic")
        self.assertContains(response, "A Scare in the Air")
        self.assertContains(response, 'title="Track Episode"', count=4)
        self.assertNotContains(
            response,
            "Episode cards are not available from MyAnimeList metadata.",
        )
        preview_episodes = response.context["media"]["episodes"]
        self.assertTrue(all(episode["actions_enabled"] for episode in preview_episodes))
        self.assertEqual(preview_episodes[2]["season_number"], 2)
        self.assertEqual(preview_episodes[2]["episode_number"], 1)
        self.assertEqual(preview_episodes[2]["display_episode_number"], 3)
        self.assertEqual(preview_episodes[2]["source"], Sources.TVDB.value)
        self.assertEqual(preview_episodes[2]["media_id"], "76703")

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_flat_mal_anime_details_render_mapped_episode_cards(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        base_metadata = {
            "media_id": "52991",
            "title": "Frieren",
            "original_title": "Sousou no Frieren",
            "localized_title": "Frieren",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "source_url": "https://myanimelist.net/anime/52991",
            "max_progress": 3,
            "image": "https://example.com/frieren.jpg",
            "synopsis": "A mage looks back.",
            "details": {"episodes": 3},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }
        grouped_payload = {
            "media_id": "9350138",
            "source": Sources.TVDB.value,
            "media_type": MediaTypes.TV.value,
            "title": "Frieren: Beyond Journey's End",
            "related": {
                "seasons": [
                    {
                        "season_number": 1,
                        "episode_count": 3,
                    },
                ],
            },
            "season/1": {
                "media_id": "9350138",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "season_title": "Season 1",
                "details": {"episodes": 3},
                "episodes": [
                    {
                        "episode_number": 1,
                        "air_date": "2023-09-29",
                        "image": "https://example.com/ep1.jpg",
                        "name": "The Journey's End",
                        "overview": "Episode one.",
                        "runtime": 24,
                    },
                    {
                        "episode_number": 2,
                        "air_date": "2023-10-06",
                        "image": "https://example.com/ep2.jpg",
                        "name": "It Didn't Have to Be Magic...",
                        "overview": "Episode two.",
                        "runtime": 24,
                    },
                    {
                        "episode_number": 3,
                        "air_date": "2023-10-13",
                        "image": "https://example.com/ep3.jpg",
                        "name": "Killing Magic",
                        "overview": "Episode three.",
                        "runtime": 24,
                    },
                ],
            },
        }

        def metadata_side_effect(media_type, *_args, **_kwargs):
            if media_type == "tv_with_seasons":
                return grouped_payload
            return base_metadata

        mock_get_metadata.side_effect = metadata_side_effect
        item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        mock_resolve_detail_metadata.side_effect = lambda *_args, **_kwargs: MetadataResolutionResult(
            display_provider=Sources.MAL.value,
            identity_provider=Sources.MAL.value,
            mapping_status="identity",
            header_metadata={
                **base_metadata,
                "details": dict(base_metadata["details"]),
                "related": dict(base_metadata["related"]),
                "cast": list(base_metadata["cast"]),
                "crew": list(base_metadata["crew"]),
                "studios_full": list(base_metadata["studios_full"]),
            },
            grouped_preview=None,
            provider_media_id=item.media_id,
            grouped_preview_target=None,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "52991",
                    "title": "frieren",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The Journey&#x27;s End")
        self.assertContains(response, "It Didn&#x27;t Have to Be Magic...")
        self.assertContains(response, "Killing Magic")
        self.assertNotContains(
            response,
            "Episode cards are not available from MyAnimeList metadata.",
        )

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.views.metadata_resolution.anime_mapping.find_entries_for_mal_id")
    @patch("app.views.anime_mapping.resolve_provider_series_id")
    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_flat_mal_anime_details_paginate_long_episode_preview(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
        mock_resolve_provider_series_id,
        mock_find_entries_for_mal_id,
    ):
        base_metadata = {
            "media_id": "527",
            "title": "Pokemon",
            "original_title": "Pokemon",
            "localized_title": "Pokemon",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "source_url": "https://myanimelist.net/anime/527",
            "max_progress": 30,
            "image": "https://example.com/pokemon.jpg",
            "synopsis": "Pocket monsters.",
            "details": {"episodes": 30},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }
        grouped_series_metadata = {
            "media_id": "76703",
            "source": Sources.TVDB.value,
            "media_type": MediaTypes.ANIME.value,
            "title": "Pokemon",
            "related": {
                "seasons": [
                    {
                        "season_number": 1,
                        "episode_count": 30,
                    },
                ],
            },
        }
        grouped_payload = grouped_series_metadata | {
            "season/1": {
                "media_id": "76703",
                "source": Sources.TVDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "season_title": "Indigo League",
                "details": {"episodes": 30},
                "episodes": [
                    {
                        "episode_number": number,
                        "air_date": f"1997-04-{number:02d}",
                        "image": f"https://example.com/pokemon-{number}.jpg",
                        "name": f"Mapped Episode {number:02d}",
                        "overview": f"Episode {number}.",
                        "runtime": 24,
                    }
                    for number in range(1, 31)
                ],
            },
        }

        def metadata_side_effect(
            media_type,
            media_id,
            source,
            season_numbers=None,
            episode_number=None,
        ):
            del media_id, episode_number
            if media_type == MediaTypes.ANIME.value and source == Sources.TVDB.value:
                return {
                    **grouped_series_metadata,
                    "related": {
                        "seasons": list(grouped_series_metadata["related"]["seasons"]),
                    },
                }
            if media_type == "tv_with_seasons" and source == Sources.TVDB.value:
                self.assertEqual(season_numbers, [1])
                return {
                    **grouped_payload,
                    "related": {
                        "seasons": list(grouped_payload["related"]["seasons"]),
                    },
                    "season/1": {
                        **grouped_payload["season/1"],
                        "details": dict(grouped_payload["season/1"]["details"]),
                        "episodes": list(grouped_payload["season/1"]["episodes"]),
                    },
                }
            return {
                **base_metadata,
                "details": dict(base_metadata["details"]),
                "related": dict(base_metadata["related"]),
                "cast": list(base_metadata["cast"]),
                "crew": list(base_metadata["crew"]),
                "studios_full": list(base_metadata["studios_full"]),
            }

        mock_get_metadata.side_effect = metadata_side_effect
        mock_resolve_provider_series_id.return_value = "76703"
        mock_find_entries_for_mal_id.return_value = [
            {
                "tvdb_id": 76703,
                "tvdb_season": 1,
                "tvdb_epoffset": 0,
                "mal_id": 527,
            },
        ]
        item = Item.objects.create(
            media_id="527",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Pokemon",
            image="https://example.com/pokemon.jpg",
        )
        mock_resolve_detail_metadata.side_effect = lambda *_args, **_kwargs: MetadataResolutionResult(
            display_provider=Sources.MAL.value,
            identity_provider=Sources.MAL.value,
            mapping_status="identity",
            header_metadata={
                **base_metadata,
                "details": dict(base_metadata["details"]),
                "related": dict(base_metadata["related"]),
                "cast": list(base_metadata["cast"]),
                "crew": list(base_metadata["crew"]),
                "studios_full": list(base_metadata["studios_full"]),
            },
            grouped_preview=None,
            provider_media_id=item.media_id,
            grouped_preview_target=None,
        )

        page_one = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "527",
                    "title": "pokemon",
                },
            ),
        )

        self.assertEqual(page_one.status_code, 200)
        self.assertEqual(len(page_one.context["media"]["episodes"]), 25)
        self.assertEqual(page_one.context["episode_load_more"]["label"], "Episodes 26-30")
        self.assertContains(page_one, "Mapped Episode 25")
        self.assertNotContains(page_one, "Mapped Episode 26")
        self.assertContains(page_one, "Show Episodes 26-30")

        page_two = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "527",
                    "title": "pokemon",
                },
            ),
            {"episode_page": 2},
        )

        self.assertEqual(page_two.status_code, 200)
        self.assertEqual(len(page_two.context["media"]["episodes"]), 5)
        self.assertEqual(
            [episode["episode_number"] for episode in page_two.context["media"]["episodes"]],
            [26, 27, 28, 29, 30],
        )
        self.assertIsNone(page_two.context["episode_load_more"])
        self.assertContains(page_two, "Mapped Episode 26")
        self.assertContains(page_two, "Mapped Episode 30")
        self.assertNotContains(page_two, "Mapped Episode 25")
        self.assertNotContains(
            page_one,
            reverse(
                "lists_modal",
                args=[Sources.TVDB.value, MediaTypes.EPISODE.value, "76703", 1, 1],
            ),
        )

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_grouped_anime_media_details_render_tv_style_subtitle(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])
        item = Item.objects.create(
            media_id="9350138",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Frieren: Beyond Journey's End",
            image="https://example.com/frieren-grouped.jpg",
        )
        tv = TV.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="9350138",
            source=Sources.TVDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Season 1",
            image="https://example.com/frieren-season.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        episode_item = Item.objects.create(
            media_id="9350138",
            source=Sources.TVDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode 1",
            image="https://example.com/frieren-episode.jpg",
            season_number=1,
            episode_number=1,
            runtime_minutes=24,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
        )
        base_metadata = {
            "media_id": "9350138",
            "title": "Frieren: Beyond Journey's End",
            "original_title": "Sousou no Frieren",
            "localized_title": "Frieren: Beyond Journey's End",
            "media_type": MediaTypes.ANIME.value,
            "identity_media_type": MediaTypes.TV.value,
            "library_media_type": MediaTypes.ANIME.value,
            "source": Sources.TVDB.value,
            "source_url": "https://www.thetvdb.com/dereferrer/series/9350138",
            "max_progress": 28,
            "image": "https://example.com/frieren-grouped.jpg",
            "synopsis": "A mage looks back.",
            "details": {"episodes": 28, "runtime": "24m"},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
            "external_links": {},
        }
        mock_get_metadata.return_value = base_metadata
        mock_resolve_detail_metadata.return_value = MetadataResolutionResult(
            display_provider=Sources.TVDB.value,
            identity_provider=Sources.TVDB.value,
            mapping_status="identity",
            header_metadata=base_metadata,
            grouped_preview=None,
            provider_media_id="9350138",
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TVDB.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "9350138",
                    "title": "frieren-beyond-journeys-end",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Progress: 1/28")
        self.assertContains(response, "2026-03-01 - 2026-03-12")
        self.assertContains(response, "48min watched")
        self.assertContains(
            response,
            'class="mt-4 mb-5 flex flex-wrap gap-2"',
            html=False,
        )
        self.assertNotContains(response, "FIRST PLAYED")
        self.assertNotContains(response, "LAST PLAYED")
        self.assertNotContains(response, "WATCHED HOURS")

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    def test_update_metadata_provider_preference_saves_override(self):
        """Saving a metadata provider override should persist a user preference only."""
        item = Item.objects.create(
            media_id="1396",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Breaking Bad",
            image="https://example.com/breaking-bad.jpg",
        )

        response = self.client.post(
            reverse(
                "update_metadata_provider_preference",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "1396",
                },
            ),
            {"provider": Sources.TVDB.value},
        )

        self.assertEqual(response.status_code, 302)
        preference = MetadataProviderPreference.objects.get(user=self.user, item=item)
        self.assertEqual(preference.provider, Sources.TVDB.value)
        item.refresh_from_db()
        self.assertEqual(item.source, Sources.TMDB.value)

    @patch("app.views.metadata_resolution.resolve_detail_metadata")
    @patch("app.providers.services.get_media_metadata")
    def test_migrated_flat_anime_shows_grouped_banner(
        self,
        mock_get_metadata,
        mock_resolve_detail_metadata,
    ):
        """Migrated legacy anime routes should link users to the grouped series."""
        base_metadata = {
            "media_id": "52991",
            "title": "Frieren",
            "original_title": "Sousou no Frieren",
            "localized_title": "Frieren",
            "media_type": MediaTypes.ANIME.value,
            "source": Sources.MAL.value,
            "max_progress": 28,
            "image": "https://example.com/frieren.jpg",
            "synopsis": "A mage looks back.",
            "details": {"episodes": 28},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
        }
        mock_get_metadata.return_value = base_metadata
        flat_item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        grouped_item = Item.objects.create(
            media_id="9350138",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Frieren: Beyond Journey's End",
            image="https://example.com/grouped.jpg",
        )
        Anime.all_objects.create(
            item=flat_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=28,
            migrated_to_item=grouped_item,
            migrated_at=timezone.now(),
        )

        mock_resolve_detail_metadata.return_value = MetadataResolutionResult(
            display_provider=Sources.MAL.value,
            identity_provider=Sources.MAL.value,
            mapping_status="identity",
            header_metadata=base_metadata,
            grouped_preview=None,
            provider_media_id="52991",
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MAL.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "52991",
                    "title": "frieren",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already been migrated to grouped series tracking")
        self.assertContains(response, "Open grouped series")

    @patch("app.providers.services.get_media_metadata")
    def test_game_media_details_renders_cached_hltb_tables(self, mock_get_metadata):
        Item.objects.create(
            media_id="325609",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Dispatch",
            image="https://example.com/dispatch.jpg",
            provider_external_ids={
                "hltb_game_id": 160618,
                "steam_app_id": 2592160,
                "itch_id": 0,
                "ign_uuid": "84fb8aca-cd19-4ff6-8919-c1b8ef5fa88a",
            },
            provider_game_lengths={
                "active_source": "hltb",
                "hltb": {
                    "game_id": 160618,
                    "url": "https://howlongtobeat.com/game/160618",
                    "summary": {
                        "main_minutes": 512,
                        "main_plus_minutes": 614,
                        "completionist_minutes": 1191,
                        "all_styles_minutes": 555,
                    },
                    "counts": {
                        "main": 1261,
                        "main_plus": 364,
                        "completionist": 108,
                        "all_styles": 1733,
                    },
                    "single_player_table": [
                        {
                            "label": "Main Story",
                            "count": 1261,
                            "average_minutes": 514,
                            "median_minutes": 510,
                            "rushed_minutes": 376,
                            "leisure_minutes": 634,
                        },
                    ],
                    "platform_table": [
                        {
                            "platform": "PC",
                            "count": 1479,
                            "main_minutes": 518,
                            "main_plus_minutes": 624,
                            "completionist_minutes": 1201,
                            "fastest_minutes": 240,
                            "slowest_minutes": 2581,
                        },
                    ],
                    "external_ids": {
                        "steam_app_id": 2592160,
                        "itch_id": 0,
                        "ign_uuid": "84fb8aca-cd19-4ff6-8919-c1b8ef5fa88a",
                    },
                    "raw": {},
                },
                "igdb": {
                    "game_id": 325609,
                    "summary": {
                        "hastily_seconds": 32400,
                        "normally_seconds": 32400,
                        "completely_seconds": 46800,
                        "count": 13,
                    },
                    "raw": [],
                },
            },
            provider_game_lengths_source="hltb",
            provider_game_lengths_match="steam_verified",
        )
        mock_get_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "source_url": "https://www.igdb.com/games/dispatch",
            "image": "https://example.com/dispatch.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "Main game",
                "release_date": "2025-10-22",
                "platforms": ["PC", "PlayStation 5"],
            },
            "genres": ["Action"],
            "related": {},
            "external_links": {
                "HowLongToBeat": "https://howlongtobeat.com/?q=Dispatch",
            },
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                    "title": "dispatch",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Time to Beat")
        self.assertContains(response, "How Long to Beat")
        self.assertContains(response, "Main Story")
        self.assertContains(response, 'href="https://howlongtobeat.com/game/160618"', html=False)
        self.assertNotContains(response, "Based on 1,733 submissions.")
        self.assertNotContains(response, "SINGLE-PLAYER")
        self.assertNotContains(response, "Playstyle")
        self.assertEqual(
            response.context["media"]["external_links"]["HowLongToBeat"],
            "https://howlongtobeat.com/game/160618",
        )

    @patch("app.providers.services.get_media_metadata")
    def test_grouped_anime_details_do_not_render_hltb_links(self, mock_get_metadata):
        Item.objects.create(
            media_id="259640",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Sword Art Online",
            image="https://example.com/sao.jpg",
            provider_external_ids={"hltb_game_id": 160618},
        )
        mock_get_metadata.return_value = {
            "media_id": "259640",
            "title": "Sword Art Online",
            "localized_title": "Sword Art Online",
            "media_type": MediaTypes.ANIME.value,
            "identity_media_type": MediaTypes.TV.value,
            "library_media_type": MediaTypes.ANIME.value,
            "source": Sources.TVDB.value,
            "source_url": "https://www.thetvdb.com/dereferrer/series/259640",
            "image": "https://example.com/sao.jpg",
            "synopsis": "Players are trapped inside a virtual world.",
            "details": {"format": "TV"},
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
            "external_links": {
                "TVDB": "https://www.thetvdb.com/dereferrer/series/259640",
            },
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TVDB.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "259640",
                    "title": "sword-art-online",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "How Long to Beat")
        self.assertNotIn("HowLongToBeat", response.context["media"]["external_links"])

    @patch("app.views._queue_game_lengths_refresh", return_value=True)
    @patch("app.providers.services.get_media_metadata")
    def test_game_media_details_renders_igdb_fallback_and_queues_hltb_refresh(
        self,
        mock_get_metadata,
        mock_queue_game_lengths_refresh,
    ):
        Item.objects.create(
            media_id="325609",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Dispatch",
            image="https://example.com/dispatch.jpg",
            provider_game_lengths={
                "active_source": "igdb",
                "igdb": {
                    "game_id": 325609,
                    "summary": {
                        "hastily_seconds": 32400,
                        "normally_seconds": 32400,
                        "completely_seconds": 46800,
                        "count": 13,
                    },
                    "raw": [{"game_id": 325609}],
                },
            },
            provider_game_lengths_source="igdb",
            provider_game_lengths_match="igdb_fallback",
        )
        mock_get_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "source_url": "https://www.igdb.com/games/dispatch",
            "image": "https://example.com/dispatch.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "Main game",
                "release_date": "2025-10-22",
                "platforms": ["PC", "PlayStation 5"],
            },
            "genres": ["Action"],
            "related": {},
            "external_links": {
                "HowLongToBeat": "https://howlongtobeat.com/?q=Dispatch",
            },
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                    "title": "dispatch",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Internet Games Database")
        self.assertContains(response, 'href="https://www.igdb.com/games/dispatch"', html=False)
        self.assertContains(response, "Normally")
        self.assertContains(response, "13 submissions")
        mock_queue_game_lengths_refresh.assert_called_once()

    @patch("app.views._queue_game_lengths_refresh", return_value=True)
    @patch("app.providers.services.get_media_metadata")
    def test_game_media_details_queues_background_fetch_when_missing_game_lengths(
        self,
        mock_get_metadata,
        mock_queue_game_lengths_refresh,
    ):
        mock_get_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "source_url": "https://www.igdb.com/games/dispatch",
            "image": "https://example.com/dispatch.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "Main game",
                "release_date": "2025-10-22",
                "platforms": ["PC", "PlayStation 5"],
            },
            "genres": ["Action"],
            "related": {},
            "external_links": {
                "HowLongToBeat": "https://howlongtobeat.com/?q=Dispatch",
            },
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                    "title": "dispatch",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fetching cached time-to-beat data in the background.")
        self.assertTrue(
            Item.objects.filter(
                media_id="325609",
                source=Sources.IGDB.value,
                media_type=MediaTypes.GAME.value,
            ).exists(),
        )
        mock_queue_game_lengths_refresh.assert_called_once()

    @patch("app.providers.services.get_media_metadata")
    @patch("app.views._queue_game_lengths_refresh")
    def test_game_media_details_shows_pending_when_refresh_lock_exists(
        self,
        mock_queue_game_lengths_refresh,
        mock_get_metadata,
    ):
        item = Item.objects.create(
            media_id="325609",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Dispatch",
            image="https://example.com/dispatch.jpg",
        )
        cache.set(
            game_length_services.get_game_lengths_refresh_lock_key(
                item.id,
                force=False,
                fetch_hltb=True,
            ),
            game_length_services.build_game_lengths_refresh_lock(
                force=False,
                fetch_hltb=True,
            ),
            timeout=game_length_services.GAME_LENGTHS_REFRESH_TTL,
        )
        mock_get_metadata.return_value = {
            "media_id": "325609",
            "title": "Dispatch",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "source_url": "https://www.igdb.com/games/dispatch",
            "image": "https://example.com/dispatch.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "Main game",
                "release_date": "2025-10-22",
                "platforms": ["PC", "PlayStation 5"],
            },
            "genres": ["Action"],
            "related": {},
            "external_links": {
                "HowLongToBeat": "https://howlongtobeat.com/?q=Dispatch",
            },
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "325609",
                    "title": "dispatch",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fetching cached time-to-beat data in the background.")
        mock_queue_game_lengths_refresh.assert_not_called()

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_view(self, mock_process_episodes, mock_get_metadata):
        """Test the season details view."""
        mock_get_metadata.side_effect = lambda *_args, **_kwargs: {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Season 1",
                "season_title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "episodes": [],
            },
        }

        mock_process_episodes.return_value = [
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "season_number": 1,
                "episode_number": 1,
                "name": "Episode 1",
                "air_date": "2023-01-01",
                "watched": False,
            },
        ]

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_details.html")

        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Season 1")
        self.assertEqual(len(response.context["media"]["episodes"]), 1)
        self.assertEqual(response.context["display_provider"], Sources.TMDB.value)
        self.assertEqual(response.context["identity_provider"], Sources.TMDB.value)
        self.assertContains(
            response,
            reverse(
                "lists_modal",
                args=[Sources.TMDB.value, MediaTypes.EPISODE.value, "1668", 1, 1],
            ),
        )

        mock_get_metadata.assert_called_once_with(
            "tv_with_seasons",
            "1668",
            Sources.TMDB.value,
            [1],
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_paginate_long_episode_lists(
        self,
        mock_process_episodes,
        mock_get_metadata,
    ):
        mock_get_metadata.side_effect = lambda *_args, **_kwargs: {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Season 1",
                "season_title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "episodes": [],
            },
        }
        mock_process_episodes.return_value = [
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "season_number": 1,
                "episode_number": number,
                "title": f"Long Episode {number:02d}",
                "image": "http://example.com/episode.jpg",
                "air_date": f"2023-01-{number:02d}",
                "overview": "",
                "runtime": "24m",
            }
            for number in range(1, 31)
        ]

        page_one = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(page_one.status_code, 200)
        self.assertEqual(len(page_one.context["media"]["episodes"]), 25)
        self.assertEqual(page_one.context["episode_load_more"]["label"], "Episodes 26-30")
        self.assertContains(page_one, "Long Episode 25")
        self.assertNotContains(page_one, "Long Episode 26")
        self.assertContains(page_one, "Show Episodes 26-30")

        page_two = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
            {"episode_page": 2},
        )

        self.assertEqual(page_two.status_code, 200)
        self.assertEqual(len(page_two.context["media"]["episodes"]), 5)
        self.assertIsNone(page_two.context["episode_load_more"])
        self.assertContains(page_two, "Long Episode 26")
        self.assertContains(page_two, "Long Episode 30")
        self.assertNotContains(page_two, "Long Episode 25")

    @patch("app.views.trakt_popularity_service.refresh_trakt_popularity")
    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_refreshes_and_renders_trakt_score(
        self,
        mock_process_episodes,
        mock_get_metadata,
        mock_refresh_trakt_popularity,
    ):
        def _refresh(item, *, route_media_type, force):
            item.trakt_rating = 7.88048
            item.trakt_rating_count = 1849
            item.trakt_popularity_rank = 25
            item.trakt_popularity_score = 998.1
            item.trakt_popularity_fetched_at = timezone.now()
            item.save(
                update_fields=[
                    "trakt_rating",
                    "trakt_rating_count",
                    "trakt_popularity_rank",
                    "trakt_popularity_score",
                    "trakt_popularity_fetched_at",
                ],
            )
            return {
                "rating": item.trakt_rating,
                "votes": item.trakt_rating_count,
                "score": item.trakt_popularity_score,
                "rank": item.trakt_popularity_rank,
            }

        mock_refresh_trakt_popularity.side_effect = _refresh
        show_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
            image="http://example.com/image.jpg",
        )
        related_tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )
        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Season 1",
            image="http://example.com/season.jpg",
            season_number=1,
        )
        Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.COMPLETED.value,
            related_tv=related_tv,
        )
        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "episodes": [],
            },
        }
        mock_process_episodes.return_value = []

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "trakt-logo.svg")
        self.assertContains(response, "7.8")
        self.assertNotContains(response, "7.88048")
        self.assertContains(response, "1,849 ratings")
        mock_refresh_trakt_popularity.assert_called_once()
        self.assertTrue(
            Item.objects.filter(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                season_number=1,
                trakt_rating=7.88048,
                trakt_rating_count=1849,
            ).exists(),
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_prefers_stored_item_image_over_provider_image(
        self,
        mock_process_episodes,
        mock_get_metadata,
    ):
        mock_process_episodes.return_value = []
        Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            image="https://images.example.com/custom-season.jpg",
            season_number=1,
        )
        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/provider-season.jpg",
                "episodes": [],
            },
        }

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["media"]["image"],
            "https://images.example.com/custom-season.jpg",
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_swaps_show_and_season_heading_sizes(
        self,
        mock_process_episodes,
        mock_get_metadata,
    ):
        mock_process_episodes.return_value = []
        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Test TV Show",
                "season_title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "score": 7.6,
                "score_count": 42000,
                "episodes": [],
            },
        }

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertRegex(
            content,
            r'<div class="mb-1 text-center md:text-start">\s*<div class="inline-flex items-center gap-2 md:flex md:gap-2">\s*<h1 class="text-3xl font-bold cursor-pointer hover:text-indigo-500 transition-colors duration-200">\s*<a href="[^"]+">Test TV Show</a>\s*</h1>',
        )
        self.assertIn(
            'class="flex flex-col gap-y-4 md:flex-row md:gap-y-0 items-center justify-between mb-1"',
            content,
        )
        self.assertIn('class="relative hidden md:block"', content)
        self.assertIn('<h2 class="text-sm font-medium text-gray-400 md:hidden">Season 1</h2>', content)
        self.assertIn(
            'class="hidden flex-wrap items-center justify-start gap-y-1 text-center text-sm font-medium text-gray-400 md:flex md:text-start"',
            content,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_hides_low_value_alternative_title_tooltip_for_english_locale(
        self,
        mock_get_metadata,
    ):
        mock_get_metadata.return_value = {
            "media_id": "15121",
            "title": "The Sound of Music",
            "original_title": "サウンド・オブ・ミュージック",
            "localized_title": "The Sound of Music",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/image.jpg",
            "synopsis": "Test overview",
            "details": {},
            "related": {},
        }

        with override("en"):
            response = self.client.get(
                reverse(
                    "media_details",
                    kwargs={
                        "source": Sources.TMDB.value,
                        "media_type": MediaTypes.MOVIE.value,
                        "media_id": "15121",
                        "title": "the-sound-of-music",
                    },
                ),
            )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("<h1 class=\"text-3xl font-bold\">The Sound of Music</h1>", content)
        self.assertNotIn('aria-label="Show alternative title"', content)

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_anime_season_details_use_tooltip_for_alt_season_title(
        self,
        mock_process_episodes,
        mock_get_metadata,
    ):
        mock_process_episodes.return_value = []
        Item.objects.create(
            media_id="259640",
            source=Sources.TVDB.value,
            media_type=MediaTypes.TV.value,
            library_media_type=MediaTypes.ANIME.value,
            title="Sword Art Online",
            image="https://example.com/sao.jpg",
        )
        mock_get_metadata.return_value = {
            "title": "Sword Art Online",
            "media_id": "259640",
            "source": Sources.TVDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "https://example.com/sao.jpg",
            "season/3": {
                "title": "Sword Art Online",
                "season_title": "Alicization",
                "media_id": "259640",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TVDB.value,
                "image": "https://example.com/alicization.jpg",
                "episodes": [],
            },
        }

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TVDB.value,
                    "media_id": "259640",
                    "title": "sword-art-online",
                    "season_number": 3,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertRegex(
            content,
            r'<h1 class="text-3xl font-bold cursor-pointer hover:text-indigo-500 transition-colors duration-200">\s*<a href="[^"]+">Sword Art Online</a>\s*</h1>\s*<div class="relative shrink-0"',
        )
        self.assertIn('aria-label="Show alternative title"', content)
        self.assertIn('<h2 class="text-sm font-medium text-gray-400">Season 3</h2>', content)
        self.assertIn("<p>Alicization</p>", content)

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_renders_progress_and_date_subtitle_without_history_card(
        self,
        mock_process_episodes,
        mock_get_metadata,
    ):
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])
        mock_process_episodes.return_value = []
        show_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
            image="http://example.com/image.jpg",
        )
        related_tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Season 1",
            image="http://example.com/season.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=related_tv,
            status=Status.IN_PROGRESS.value,
        )
        Episode.objects.create(
            item=Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Episode 1",
                image="http://example.com/episode1.jpg",
                season_number=1,
                episode_number=1,
            ),
            related_season=season,
            end_date=datetime(2026, 3, 1, 12, 0, tzinfo=UTC),
        )
        Episode.objects.create(
            item=Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Episode 2",
                image="http://example.com/episode2.jpg",
                season_number=1,
                episode_number=2,
            ),
            related_season=season,
            end_date=datetime(2026, 3, 12, 12, 0, tzinfo=UTC),
        )
        mock_get_metadata.return_value = {
            "title": "Test TV Show",
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "image": "http://example.com/image.jpg",
            "season/1": {
                "title": "Test TV Show",
                "season_title": "Season 1",
                "media_id": "1668",
                "media_type": MediaTypes.SEASON.value,
                "source": Sources.TMDB.value,
                "image": "http://example.com/season.jpg",
                "max_progress": 8,
                "episodes": [],
            },
        }

        response = self.client.get(
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "1668",
                    "title": "test-tv-show",
                    "season_number": 1,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn(
            'class="mb-3 flex flex-col items-center justify-between gap-y-4 md:mb-1 md:flex-row md:gap-y-0"',
            content,
        )
        self.assertIn('<h2 class="text-sm font-medium text-gray-400 md:hidden">Season 1</h2>', content)
        self.assertIn(
            'class="mt-3 flex flex-wrap items-center justify-center gap-y-1 text-center text-sm font-medium text-gray-400 md:hidden"',
            content,
        )
        self.assertRegex(
            content,
            r'class="hidden flex-wrap items-center justify-start gap-y-1 text-center text-sm font-medium text-gray-400 md:flex md:text-start">\s*<h2 class="text-sm font-medium text-gray-400">Season 1</h2>\s*<span class="mx-2 text-gray-600">•</span>\s*<span class="text-sm font-medium text-gray-400">\s*Progress: 2/8\s*</span>\s*<span class="mx-2 text-gray-600">•</span>\s*<span class="text-sm font-medium text-gray-400">\s*2026-03-01 - 2026-03-12\s*</span>',
        )
        self.assertNotIn("Your History", content)

    @patch("integrations.tasks.fetch_collection_metadata_for_item.delay")
    @patch("app.providers.services.get_media_metadata")
    def test_game_details_skips_collection_autofetch(
        self,
        mock_get_metadata,
        mock_fetch_delay,
    ):
        """Game details should not trigger collection auto-fetch."""
        mock_get_metadata.return_value = {
            "media_id": "game-123",
            "title": "Test Game",
            "media_type": MediaTypes.GAME.value,
            "source": Sources.IGDB.value,
            "image": "http://example.com/game.jpg",
            "overview": "Test overview",
            "release_date": "2023-01-01",
        }

        Item.objects.create(
            media_id="game-123",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title="Test Game",
            image="http://example.com/game.jpg",
        )

        PlexAccount.objects.create(
            user=self.user,
            plex_token="plex-token",
            plex_username="plex-user",
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.IGDB.value,
                    "media_type": MediaTypes.GAME.value,
                    "media_id": "game-123",
                    "title": "test-game",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["fetching_collection_data"])
        self.assertIsNone(response.context["item_id_for_polling"])
        mock_fetch_delay.assert_not_called()

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_renders_cast_and_crew_links(self, mock_get_metadata):
        """Movie details should render cast/crew links to person pages."""
        mock_get_metadata.return_value = {
            "media_id": "238",
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/movie/238",
            "image": "http://example.com/image.jpg",
            "synopsis": "Test synopsis",
            "details": {"format": "Movie"},
            "cast": [
                {
                    "person_id": "10",
                    "name": "John Actor",
                    "role": "Hero",
                },
            ],
            "crew": [
                {
                    "person_id": "11",
                    "name": "Jane Director",
                    "role": "Director",
                    "department": "Directing",
                },
            ],
            "studios_full": [
                {
                    "studio_id": "20",
                    "name": "Studio One",
                },
            ],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                    "title": "test-movie",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "John Actor")
        self.assertContains(response, "Jane Director")
        self.assertContains(response, "Studio One")
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.TMDB.value,
                    "person_id": "10",
                    "name": "john-actor",
                },
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_tv_details_view_adds_specials_from_regular_path(self, mock_get_metadata):
        """TV details should show a specials season when season 0 is enriched."""
        def metadata_side_effect(
            media_type,
            media_id,
            source,
            season_numbers=None,
            episode_number=None,
        ):
            del episode_number
            self.assertEqual(media_id, "114410")
            self.assertEqual(source, Sources.TMDB.value)
            if media_type == MediaTypes.TV.value:
                return {
                    "media_id": "114410",
                    "title": "Chainsaw Man",
                    "media_type": MediaTypes.TV.value,
                    "source": Sources.TMDB.value,
                    "image": "http://example.com/show.jpg",
                    "tvdb_id": "10196540",
                    "details": {
                        "runtime": "24m",
                        "first_air_date": "2022-10-12",
                    },
                    "related": {
                        "seasons": [
                            {
                                "source": Sources.TMDB.value,
                                "media_type": MediaTypes.SEASON.value,
                                "media_id": "114410",
                                "title": "Chainsaw Man",
                                "season_number": 1,
                                "season_title": "Chainsaw Man",
                                "image": settings.IMG_NONE,
                            },
                        ],
                    },
                    "cast": [],
                    "crew": [],
                    "studios_full": [],
                }

            self.assertEqual(media_type, "tv_with_seasons")
            if season_numbers == [0]:
                return {
                    "season/0": {
                        "season_number": 0,
                        "season_title": "Specials",
                    },
                    "related": {
                        "seasons": [
                            {
                                "source": Sources.TMDB.value,
                                "media_type": MediaTypes.SEASON.value,
                                "media_id": "114410",
                                "title": "Chainsaw Man",
                                "season_number": 0,
                                "season_title": "Specials",
                                "image": "http://example.com/specials.jpg",
                            },
                            {
                                "source": Sources.TMDB.value,
                                "media_type": MediaTypes.SEASON.value,
                                "media_id": "114410",
                                "title": "Chainsaw Man",
                                "season_number": 1,
                                "season_title": "Chainsaw Man",
                                "image": "http://example.com/season1.jpg",
                            },
                        ],
                    },
                }

            self.assertEqual(season_numbers, [0, 1])
            return {
                "season/0": {
                    "season_number": 0,
                    "season_title": "Specials",
                    "image": "http://example.com/specials.jpg",
                    "details": {"episodes": 1},
                },
                "season/1": {
                    "season_number": 1,
                    "season_title": "Season 1",
                    "image": "http://example.com/season1.jpg",
                    "details": {"episodes": 12},
                },
            }

        mock_get_metadata.side_effect = metadata_side_effect

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "114410",
                    "title": "chainsaw-man",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        seasons = response.context["media"]["related"]["seasons"]
        self.assertEqual(seasons[0]["item"]["season_number"], 0)
        self.assertContains(
            response,
            reverse(
                "season_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_id": "114410",
                    "title": "chainsaw-man",
                    "season_number": 0,
                },
            ),
        )
        self.assertEqual(mock_get_metadata.call_count, 3)
        self.assertEqual(
            mock_get_metadata.call_args_list[0].args,
            (
                MediaTypes.TV.value,
                "114410",
                Sources.TMDB.value,
            ),
        )
        self.assertEqual(
            mock_get_metadata.call_args_list[1].args,
            (
                "tv_with_seasons",
                "114410",
                Sources.TMDB.value,
                [0],
            ),
        )
        self.assertEqual(
            mock_get_metadata.call_args_list[2].args,
            (
                "tv_with_seasons",
                "114410",
                Sources.TMDB.value,
                [0, 1],
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_tv_details_view_uses_special_watch_for_show_end_date(
        self,
        mock_get_metadata,
    ):
        """TV details should show the most recent special watch in the history card."""
        watched_main = datetime(2023, 8, 28, 12, 0, tzinfo=UTC)
        watched_special = datetime(2026, 3, 12, 12, 0, tzinfo=UTC)

        tv_item = Item.objects.create(
            media_id="114410",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Chainsaw Man",
            image="http://example.com/show.jpg",
        )
        tv = TV.objects.create(
            user=self.user,
            item=tv_item,
            status=Status.IN_PROGRESS.value,
        )

        season_one_item = Item.objects.create(
            media_id="114410",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Chainsaw Man",
            image="http://example.com/season1.jpg",
            season_number=1,
        )
        season_one = Season.objects.create(
            user=self.user,
            item=season_one_item,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )
        Episode.objects.create(
            item=Item.objects.create(
                media_id="114410",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Episode 12",
                image="http://example.com/ep12.jpg",
                season_number=1,
                episode_number=12,
            ),
            related_season=season_one,
            end_date=watched_main,
        )

        specials_item = Item.objects.create(
            media_id="114410",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Chainsaw Man",
            image="http://example.com/specials.jpg",
            season_number=0,
        )
        specials = Season.objects.create(
            user=self.user,
            item=specials_item,
            related_tv=tv,
            status=Status.COMPLETED.value,
        )
        Episode.objects.create(
            item=Item.objects.create(
                media_id="114410",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Special 1",
                image="http://example.com/s00e01.jpg",
                season_number=0,
                episode_number=1,
            ),
            related_season=specials,
            end_date=watched_special,
        )

        mock_get_metadata.return_value = {
            "media_id": "114410",
            "title": "Chainsaw Man",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "image": "http://example.com/show.jpg",
            "details": {
                "runtime": "24m",
            },
            "related": {
                "seasons": [
                    {
                        "source": Sources.TMDB.value,
                        "media_type": MediaTypes.SEASON.value,
                        "media_id": "114410",
                        "title": "Chainsaw Man",
                        "season_number": 0,
                        "season_title": "Specials",
                        "image": "http://example.com/specials.jpg",
                    },
                    {
                        "source": Sources.TMDB.value,
                        "media_type": MediaTypes.SEASON.value,
                        "media_id": "114410",
                        "title": "Chainsaw Man",
                        "season_number": 1,
                        "season_title": "Season 1",
                        "image": "http://example.com/season1.jpg",
                    },
                ],
            },
            "cast": [],
            "crew": [],
            "studios_full": [],
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "114410",
                    "title": "chainsaw-man",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_instance"].end_date, watched_special)
        self.assertEqual(response.context["current_instance"].progress, 12)
        self.assertContains(response, 'title="Specials">Specials</a>', html=False)
        self.assertContains(response, 'title="Season 1">Season 1</a>', html=False)

    @patch("app.providers.services.get_media_metadata")
    def test_anime_details_view_renders_named_season_card_titles(self, mock_get_metadata):
        def metadata_side_effect(
            media_type,
            media_id,
            source,
            season_numbers=None,
            episode_number=None,
        ):
            del episode_number
            self.assertEqual(media_id, "76703")
            self.assertEqual(source, Sources.TVDB.value)
            if media_type == MediaTypes.ANIME.value:
                return {
                    "media_id": "76703",
                    "title": "Pokemon",
                    "media_type": MediaTypes.ANIME.value,
                    "source": Sources.TVDB.value,
                    "image": "https://example.com/pokemon.jpg",
                    "details": {
                        "episodes": 82,
                    },
                    "related": {
                        "seasons": [
                            {
                                "source": Sources.TVDB.value,
                                "media_type": MediaTypes.SEASON.value,
                                "media_id": "76703",
                                "title": "Pokemon",
                                "season_number": 1,
                                "season_title": "Pokemon",
                                "image": "https://example.com/indigo-league.jpg",
                            },
                            {
                                "source": Sources.TVDB.value,
                                "media_type": MediaTypes.SEASON.value,
                                "media_id": "76703",
                                "title": "Pokemon",
                                "season_number": 2,
                                "season_title": "Pokemon",
                                "image": "https://example.com/orange-islands.jpg",
                            },
                        ],
                    },
                    "cast": [],
                    "crew": [],
                    "studios_full": [],
                }

            self.assertEqual(media_type, "tv_with_seasons")
            self.assertEqual(season_numbers, [1, 2])
            return {
                "season/1": {
                    "season_number": 1,
                    "season_title": "Indigo League",
                    "image": "https://example.com/indigo-league.jpg",
                    "details": {"episodes": 52},
                },
                "season/2": {
                    "season_number": 2,
                    "season_title": "Orange Islands",
                    "image": "https://example.com/orange-islands.jpg",
                    "details": {"episodes": 36},
                },
            }

        mock_get_metadata.side_effect = metadata_side_effect

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TVDB.value,
                    "media_type": MediaTypes.ANIME.value,
                    "media_id": "76703",
                    "title": "pokemon",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'title="Indigo League">Indigo League</a>',
            html=False,
        )
        self.assertContains(
            response,
            'title="Orange Islands">Orange Islands</a>',
            html=False,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_backfills_author_credits_and_renders_links(
        self,
        mock_get_metadata,
    ):
        mock_get_metadata.return_value = {
            "media_id": "OL123M",
            "title": "Linked Book",
            "media_type": MediaTypes.BOOK.value,
            "source": Sources.OPENLIBRARY.value,
            "source_url": "https://openlibrary.org/books/OL123M",
            "image": "http://example.com/book.jpg",
            "synopsis": "Book synopsis",
            "max_progress": 300,
            "details": {
                "author": ["Open Author"],
                "publish_date": "2000-01-01",
            },
            "authors_full": [
                {
                    "person_id": "OL1A",
                    "name": "Open Author",
                    "image": "http://example.com/author.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
            ],
            "related": {},
        }

        item = Item.objects.create(
            media_id="OL123M",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Linked Book",
            image="http://example.com/book.jpg",
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=300,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "OL123M",
                    "title": "linked-book",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        author_person = Person.objects.get(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL1A",
        )
        self.assertTrue(
            ItemPersonCredit.objects.filter(
                item=item,
                person=author_person,
                role_type=CreditRoleType.AUTHOR.value,
            ).exists(),
        )
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "person_id": "OL1A",
                    "name": "open-author",
                },
            ),
        )
        html = response.content.decode()
        self.assertEqual(
            html.count('text-sm font-semibold text-gray-400">AUTHOR</h3>'),
            1,
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_uses_authors_full_fallback_without_item(self, mock_get_metadata):
        mock_get_metadata.return_value = {
            "media_id": "72274276213",
            "title": "Metadata Only Manga",
            "media_type": MediaTypes.MANGA.value,
            "source": Sources.MANGAUPDATES.value,
            "source_url": "https://www.mangaupdates.com/series/72274276213",
            "image": "http://example.com/manga.jpg",
            "synopsis": "Manga synopsis",
            "details": {
                "authors": ["Manga Author"],
            },
            "authors_full": [
                {
                    "person_id": "55",
                    "name": "Manga Author",
                    "image": "http://example.com/manga-author.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
            ],
            "related": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.MANGAUPDATES.value,
                    "media_type": MediaTypes.MANGA.value,
                    "media_id": "72274276213",
                    "title": "metadata-only-manga",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ItemPersonCredit.objects.count(), 0)
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.MANGAUPDATES.value,
                    "person_id": "55",
                    "name": "manga-author",
                },
            ),
        )

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_refreshes_stale_author_cache_and_renders_links(
        self,
        mock_get_metadata,
    ):
        stale_metadata = {
            "media_id": "OL999M",
            "title": "Cached Book",
            "media_type": MediaTypes.BOOK.value,
            "source": Sources.OPENLIBRARY.value,
            "source_url": "https://openlibrary.org/books/OL999M",
            "image": "http://example.com/book.jpg",
            "synopsis": "Book synopsis",
            "max_progress": 320,
            "details": {
                "author": ["Cached Author"],
                "publish_date": "1999-01-01",
            },
            "related": {},
        }
        refreshed_metadata = {
            **stale_metadata,
            "authors_full": [
                {
                    "person_id": "OL9A",
                    "name": "Cached Author",
                    "image": "http://example.com/author.jpg",
                    "role": "Author",
                    "sort_order": 0,
                },
            ],
        }
        call_count = {"count": 0}

        def _metadata_side_effect(*_args, **_kwargs):
            call_count["count"] += 1
            if call_count["count"] == 1:
                return stale_metadata
            return refreshed_metadata

        mock_get_metadata.side_effect = _metadata_side_effect

        item = Item.objects.create(
            media_id="OL999M",
            source=Sources.OPENLIBRARY.value,
            media_type=MediaTypes.BOOK.value,
            title="Cached Book",
            image="http://example.com/book.jpg",
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=320,
            start_date=timezone.now(),
            end_date=timezone.now(),
        )

        cache_key = f"{Sources.OPENLIBRARY.value}_{MediaTypes.BOOK.value}_OL999M"
        cache.set(cache_key, stale_metadata)

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "OL999M",
                    "title": "cached-book",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        detail_calls = [
            call
            for call in mock_get_metadata.call_args_list
            if call.args[:3]
            == (
                MediaTypes.BOOK.value,
                "OL999M",
                Sources.OPENLIBRARY.value,
            )
        ]
        self.assertGreaterEqual(len(detail_calls), 2)
        self.assertContains(
            response,
            reverse(
                "person_detail",
                kwargs={
                    "source": Sources.OPENLIBRARY.value,
                    "person_id": "OL9A",
                    "name": "cached-author",
                },
            ),
        )

        author_person = Person.objects.get(
            source=Sources.OPENLIBRARY.value,
            source_person_id="OL9A",
        )
        self.assertTrue(
            ItemPersonCredit.objects.filter(
                item=item,
                person=author_person,
                role_type=CreditRoleType.AUTHOR.value,
            ).exists(),
        )

    def test_podcast_media_details_renders_for_show_with_no_user_plays(self):
        """Podcast details should render even when episodes have no play history."""
        show = PodcastShow.objects.create(
            podcast_uuid="itunes:1002937870",
            title="Dear Hank & John",
            author="Hank and John",
            image="http://example.com/podcast.jpg",
            rss_feed_url="",
        )
        PodcastEpisode.objects.create(
            show=show,
            episode_uuid="dhj-episode-1",
            title="Episode One",
            duration=3600,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.POCKETCASTS.value,
                    "media_type": MediaTypes.PODCAST.value,
                    "media_id": show.podcast_uuid,
                    "title": "dear-hank-john",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dear Hank &amp; John")
        self.assertContains(response, "Episode One")
        self.assertNotContains(response, "Mark All Played")

    def test_podcast_episode_fragment_renders_for_show_with_no_user_plays(self):
        """Podcast episode HTMX fragments should render when no play history exists."""
        show = PodcastShow.objects.create(
            podcast_uuid="itunes:1002937870",
            title="Dear Hank & John",
            author="Hank and John",
            image="http://example.com/podcast.jpg",
            rss_feed_url="",
        )
        PodcastEpisode.objects.create(
            show=show,
            episode_uuid="dhj-episode-2",
            title="Episode Two",
            duration=1800,
        )

        response = self.client.get(
            reverse("podcast_episodes_api", kwargs={"show_id": show.id}),
            {"format": "html", "page": 1, "page_size": 20},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Episode Two")

    @patch("app.tasks.enqueue_genre_backfill_items", return_value=1)
    def test_media_details_genre_update_refreshes_reading_top_genres(self, _mock_enqueue_genre_backfill_items):
        """Saving reading genres from details should invalidate stale day caches."""
        played_at = timezone.now() - timedelta(days=30)
        item = Item.objects.create(
            media_id="377938",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="The Lord of the Rings",
            image="http://example.com/book.jpg",
            genres=[],
        )
        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.COMPLETED.value,
            progress=900,
            start_date=played_at,
            end_date=played_at,
        )

        statistics_cache.build_stats_for_day(self.user.id, played_at.date())
        stale_stats = statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        self.assertEqual(stale_stats["book_consumption"]["top_genres"], [])

        with patch("app.providers.services.get_media_metadata") as mock_get_metadata:
            mock_get_metadata.return_value = {
                "media_id": "377938",
                "title": "The Lord of the Rings",
                "media_type": MediaTypes.BOOK.value,
                "source": Sources.MANUAL.value,
                "image": "http://example.com/book.jpg",
                "max_progress": 1178,
                "genres": ["Fantasy"],
                "details": {"number_of_pages": 1178},
            }
            response = self.client.get(
                reverse(
                    "media_details",
                    kwargs={
                        "source": Sources.MANUAL.value,
                        "media_type": MediaTypes.BOOK.value,
                        "media_id": "377938",
                        "title": "the-lord-of-the-rings",
                    },
                ),
            )
        self.assertEqual(response.status_code, 200)

        item.refresh_from_db()
        self.assertEqual(item.genres, ["Fantasy"])

        statistics_cache.invalidate_statistics_cache(self.user.id, "All Time")
        refreshed_stats = statistics_cache.refresh_statistics_cache(self.user.id, "All Time")
        refreshed_genres = [entry["name"] for entry in refreshed_stats["book_consumption"]["top_genres"]]
        self.assertIn("Fantasy", refreshed_genres)

    @patch("app.providers.services.get_media_metadata")
    def test_tv_media_details_uses_episode_runtime_fallback_when_metadata_runtime_missing(
        self,
        mock_get_metadata,
    ):
        """TV details should show a derived runtime when provider runtime is missing."""
        show_item = Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Bridgerton",
            image="http://example.com/show.jpg",
            runtime_minutes=999999,
        )
        Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Episode 1",
            runtime_minutes=52,
        )
        Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            title="Episode 2",
            runtime_minutes=54,
        )

        mock_get_metadata.return_value = {
            "media_id": "91239",
            "title": "Bridgerton",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/tv/91239",
            "image": "http://example.com/show.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "TV",
                "runtime": None,
                "seasons": 1,
            },
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
            "providers": {},
            "external_links": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "91239",
                    "title": "bridgerton",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["media"]["details"]["runtime"], "53m")
        show_item.refresh_from_db()
        self.assertEqual(show_item.runtime_minutes, 999999)

    @patch("app.providers.services.get_media_metadata")
    def test_tv_media_details_play_stats_skip_placeholder_episode_runtimes(
        self,
        mock_get_metadata,
    ):
        """TV details totals should ignore placeholder episode runtimes."""
        watched_at = timezone.now()
        show_item = Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Bridgerton",
            image="http://example.com/show.jpg",
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Bridgerton",
            image="http://example.com/season.jpg",
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        valid_episode_item = Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Episode 1",
            runtime_minutes=45,
        )
        placeholder_episode_item = Item.objects.create(
            media_id="91239",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            title="Episode 2",
            runtime_minutes=999998,
        )
        Episode.objects.create(
            item=valid_episode_item,
            related_season=season,
            end_date=watched_at,
        )
        Episode.objects.create(
            item=placeholder_episode_item,
            related_season=season,
            end_date=watched_at + timedelta(minutes=1),
        )

        mock_get_metadata.return_value = {
            "media_id": "91239",
            "title": "Bridgerton",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/tv/91239",
            "image": "http://example.com/show.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "TV",
                "runtime": None,
                "seasons": 1,
            },
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
            "providers": {},
            "external_links": {},
        }

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "91239",
                    "title": "bridgerton",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["play_stats"]["total_minutes"], 45)
        self.assertEqual(response.context["play_stats"]["episode_count"], 1)

    @patch("app.providers.services.get_media_metadata")
    def test_tv_media_details_show_total_runtime_uses_same_calculation_as_media_list(
        self,
        mock_get_metadata,
    ):
        """TV details should show shared total runtime while watched time moves into the subtitle."""
        now = timezone.now()
        show_item = Item.objects.create(
            media_id="fallout-runtime-shared",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Shared Runtime Show",
            image="http://example.com/show.jpg",
            runtime_minutes=25,
        )
        tv = TV.objects.create(
            item=show_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="fallout-runtime-shared",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            title="Shared Runtime Show",
            image="http://example.com/season.jpg",
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )

        first_episode_item = Item.objects.create(
            media_id="fallout-runtime-shared",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            title="Episode 1",
            runtime_minutes=52,
            release_datetime=now - timedelta(days=3),
        )
        second_episode_item = Item.objects.create(
            media_id="fallout-runtime-shared",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=2,
            title="Episode 2",
            runtime_minutes=58,
            release_datetime=now - timedelta(days=2),
        )
        third_episode_item = Item.objects.create(
            media_id="fallout-runtime-shared",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=3,
            title="Episode 3",
            runtime_minutes=47,
            release_datetime=now - timedelta(days=1),
        )

        Episode.objects.create(
            item=first_episode_item,
            related_season=season,
            end_date=now - timedelta(days=1),
        )
        Episode.objects.create(
            item=second_episode_item,
            related_season=season,
            end_date=now,
        )
        Episode.objects.create(
            item=third_episode_item,
            related_season=season,
        )

        mock_get_metadata.return_value = {
            "media_id": "fallout-runtime-shared",
            "title": "Shared Runtime Show",
            "media_type": MediaTypes.TV.value,
            "source": Sources.TMDB.value,
            "source_url": "https://www.themoviedb.org/tv/fallout-runtime-shared",
            "image": "http://example.com/show.jpg",
            "synopsis": "Test synopsis",
            "details": {
                "format": "TV",
                "runtime": "25m",
                "seasons": 1,
                "episodes": 3,
            },
            "related": {},
            "cast": [],
            "crew": [],
            "studios_full": [],
            "providers": {},
            "external_links": {},
        }

        detail_response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.TV.value,
                    "media_id": "fallout-runtime-shared",
                    "title": "shared-runtime-show",
                },
            ),
        )

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.context["media"]["details"]["runtime"], "25m")
        self.assertEqual(detail_response.context["media"]["details"]["total_runtime"], "2h 37min")
        self.assertEqual(detail_response.context["play_stats"]["total_minutes"], 110)
        self.assertContains(detail_response, "1h 50min watched")
        self.assertContains(detail_response, "TOTAL RUNTIME")
        self.assertContains(detail_response, "2h 37min")
        self.assertNotContains(detail_response, "FIRST PLAYED")
        self.assertNotContains(detail_response, "LAST PLAYED")
        self.assertNotContains(detail_response, "WATCHED HOURS")

        list_response = self.client.get(
            reverse("medialist", args=[MediaTypes.TV.value])
            + "?layout=table&search=Shared+Runtime+Show",
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "2h 37min")

    @patch("app.providers.openlibrary.book")
    def test_audiobookshelf_book_details_does_not_call_openlibrary(
        self,
        mock_openlibrary_book,
    ):
        """Audiobookshelf detail pages should render using local metadata."""
        item = Item.objects.create(
            media_id="f9e2ce45ec9315a7c54c",
            source=Sources.AUDIOBOOKSHELF.value,
            media_type=MediaTypes.BOOK.value,
            title="The Blade Itself",
            image="https://img.example/blade.jpg",
            runtime_minutes=1320,
            authors=["Joe Abercrombie"],
            format="audiobook",
        )

        Book.objects.create(
            user=self.user,
            item=item,
            status=Status.IN_PROGRESS.value,
            progress=60,
        )

        response = self.client.get(
            reverse(
                "media_details",
                kwargs={
                    "source": Sources.AUDIOBOOKSHELF.value,
                    "media_type": MediaTypes.BOOK.value,
                    "media_id": "f9e2ce45ec9315a7c54c",
                    "title": "the-blade-itself",
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The Blade Itself")
        mock_openlibrary_book.assert_not_called()
