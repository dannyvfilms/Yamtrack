from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.client import RequestFactory
from django.urls import reverse
from django.utils import timezone

from app.models import Album, Artist, Item, MediaTypes, Sources
from app.templatetags import app_tags
from users.models import DateFormatChoices, TimeFormatChoices


class AppTagsTests(TestCase):
    """Test the app template tags."""

    def setUp(self):
        """Set up test data."""
        self.user = get_user_model().objects.create_user(
            username="templater",
            password="12345",
        )
        self.request_factory = RequestFactory()

        # Create a sample item for testing
        self.tv_item = Item(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
        )

        self.season_item = Item(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            season_number=1,
        )

        self.episode_item = Item(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Test TV Show",
            season_number=1,
            episode_number=1,
        )

        # Create a dict version for testing dict-based functions
        self.tv_dict = {
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.TV.value,
            "title": "Test TV Show",
        }

        self.season_dict = {
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.SEASON.value,
            "title": "Test TV Show",
            "season_number": 1,
        }

        self.episode_dict = {
            "media_id": "1668",
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.EPISODE.value,
            "title": "Test TV Show",
            "season_number": 1,
            "episode_number": 1,
        }

    @patch("pathlib.Path.stat")
    def test_get_static_file_mtime(self, mock_stat):
        """Test the get_static_file_mtime tag."""
        # Mock the stat method to return a fixed mtime
        mock_stat_result = MagicMock()
        mock_stat_result.st_mtime = 1234567890
        mock_stat.return_value = mock_stat_result

        # Test with a valid file
        result = app_tags.get_static_file_mtime("css/style.css")
        self.assertEqual(result, "?1234567890")

        # Test with file not found
        mock_stat.side_effect = OSError()
        result = app_tags.get_static_file_mtime("nonexistent.css")
        self.assertEqual(result, "")

    def test_no_underscore(self):
        """Test the no_underscore filter."""
        self.assertEqual(app_tags.no_underscore("hello_world"), "hello world")
        self.assertEqual(
            app_tags.no_underscore("test_string_with_underscores"),
            "test string with underscores",
        )
        self.assertEqual(
            app_tags.no_underscore("no_underscores_here"),
            "no underscores here",
        )

    def test_slug(self):
        """Test the slug filter."""
        # Test normal slugification
        self.assertEqual(app_tags.slug("Hello World"), "hello-world")

        # Test with special characters
        self.assertEqual(app_tags.slug("Anime: 31687"), "anime-31687")
        self.assertEqual(app_tags.slug("★★★"), "%E2%98%85%E2%98%85%E2%98%85")
        self.assertEqual(app_tags.slug("[Oshi no Ko]"), "oshi-no-ko")
        self.assertEqual(app_tags.slug("_____"), "_____")

    def test_title_preserve_acronyms(self):
        """Test acronym-preserving title casing."""
        self.assertEqual(app_tags.title_preserve_acronyms("rom"), "Rom")
        self.assertEqual(app_tags.title_preserve_acronyms("ROM"), "ROM")
        self.assertEqual(
            app_tags.title_preserve_acronyms("digital deluxe"),
            "Digital Deluxe",
        )

    def test_media_type_readable(self):
        """Test the media_type_readable filter."""
        # Test all media types from the MediaTypes class
        for media_type, label in MediaTypes.choices:
            self.assertEqual(app_tags.media_type_readable(media_type), label)

    def test_media_type_readable_plural(self):
        """Test the media_type_readable_plural filter."""
        # Test all media types from the MediaTypes class
        for media_type, label in MediaTypes.choices:
            singular = label

            # Special cases that don't change in plural form
            if singular.lower() in [
                MediaTypes.ANIME.value,
                MediaTypes.MANGA.value,
                MediaTypes.MUSIC.value,
            ]:
                expected = singular
            else:
                expected = f"{singular}s"

            self.assertEqual(app_tags.media_type_readable_plural(media_type), expected)

    def test_default_source(self):
        """Test the default_source filter."""
        # Test all media types from the MediaTypes class
        for media_type in MediaTypes.values:
            result = app_tags.default_source(media_type)

            # Check that it returns a non-empty string
            self.assertTrue(isinstance(result, str))
            self.assertTrue(len(result) > 0)

            # This implicitly checks that all media types are handled
        try:
            app_tags.default_source(media_type)
        except KeyError:
            self.fail(f"default_source raised KeyError for {media_type}")

    def test_media_past_verb(self):
        """Test the media_past_verb filter."""
        # Test all media types
        for media_type in MediaTypes.values:
            result = app_tags.media_past_verb(media_type)

            # Check that it returns a non-empty string
            self.assertTrue(isinstance(result, str))

    def test_sample_search(self):
        """Test the sample_search filter."""
        # Test all media types
        for media_type in MediaTypes.values:
            if media_type in (MediaTypes.SEASON.value, MediaTypes.EPISODE.value):
                # Skip season and episode for sample_search
                continue

            result = app_tags.sample_search(media_type)

            self.assertIn("/search", result)
            self.assertIn(f"media_type={media_type}", result)
            self.assertIn("q=", result)

    def test_media_color(self):
        """Test the media_color filter."""
        # Test all media types
        for media_type in MediaTypes.values:
            result = app_tags.media_color(media_type)

            # Check that it returns a non-empty string
            self.assertTrue(isinstance(result, str))

    def test_natural_day(self):
        """Test the natural_day filter."""
        # Create mock user with date_format preference
        mock_user = MagicMock()
        mock_user.date_format = DateFormatChoices.ISO_8601
        mock_user.time_format = TimeFormatChoices.HH_MM

        # Mock current date to March 29, 2025
        with patch("django.utils.timezone.now") as mock_now:
            # Use timezone.datetime to create timezone-aware datetimes
            mock_now.return_value = timezone.datetime(
                2025,
                3,
                29,
                12,
                0,
                0,
                tzinfo=timezone.get_current_timezone(),
            )

            # Test today
            today = timezone.datetime(
                2025,
                3,
                29,
                15,
                0,
                0,
                tzinfo=timezone.get_current_timezone(),
            )
            self.assertEqual(app_tags.natural_day(today, mock_user), "Today")

            # Test tomorrow
            tomorrow = timezone.datetime(
                2025,
                3,
                30,
                15,
                0,
                0,
                tzinfo=timezone.get_current_timezone(),
            )
            self.assertEqual(app_tags.natural_day(tomorrow, mock_user), "Tomorrow")

            # Test further away
            further = timezone.datetime(
                2025,
                4,
                10,
                15,
                0,
                0,
                tzinfo=timezone.get_current_timezone(),
            )
            self.assertEqual(
                app_tags.natural_day(further, mock_user),
                "2025-04-10 15:00",
            )

    def test_iso_date_format_respects_user_preference(self):
        """iso_date_format should handle user choice keys without raising."""
        iso_user = SimpleNamespace(date_format=DateFormatChoices.ISO_8601)
        month_user = SimpleNamespace(date_format=DateFormatChoices.MONTH_D_YYYY)

        self.assertEqual(
            app_tags.iso_date_format("2026-03-04", iso_user),
            "2026-03-04",
        )
        self.assertEqual(
            app_tags.iso_date_format("2026-03-04", month_user),
            "Mar 04, 2026",
        )
        self.assertEqual(
            app_tags.iso_date_format(timezone.datetime(2026, 3, 4).date(), iso_user),
            "2026-03-04",
        )
        self.assertEqual(
            app_tags.iso_date_format("not-a-date", iso_user),
            "not-a-date",
        )

    def test_music_artist_url_returns_canonical_details_path(self):
        """Music artists should resolve to the canonical shared details route."""
        artist = Artist.objects.create(name="The Amazing Artist")

        self.assertEqual(
            app_tags.music_artist_url(artist),
            reverse(
                "music_artist_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "the-amazing-artist",
                },
            ),
        )

    def test_music_album_url_returns_nested_canonical_details_path(self):
        """Music albums should resolve to the nested artist/album shared route."""
        artist = Artist.objects.create(name="The Amazing Artist")
        album = Album.objects.create(title="First Record", artist=artist)

        self.assertEqual(
            app_tags.music_album_url(album),
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "the-amazing-artist",
                    "album_id": album.id,
                    "album_slug": "first-record",
                },
            ),
        )

    def test_music_album_url_accepts_statistics_track_rollup_dict(self):
        """Track rollup dicts with album metadata should still resolve canonically."""
        self.assertEqual(
            app_tags.music_album_url(
                {
                    "album_id": 17,
                    "album": "Live at Home",
                    "album_artist_id": 9,
                    "album_artist_name": "Short Name",
                },
            ),
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": 9,
                    "artist_slug": "short-name",
                    "album_id": 17,
                    "album_slug": "live-at-home",
                },
            ),
        )

    def test_media_card_uses_canonical_music_album_url(self):
        """Music media cards should link through the nested shared album route."""
        artist = Artist.objects.create(name="Card Artist")
        album = Album.objects.create(title="Card Album", artist=artist)
        item = Item.objects.create(
            media_id="track-card-1",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title="Card Song",
            image="http://example.com/card-album.jpg",
        )
        request = self.request_factory.get("/library")
        request.user = self.user

        content = render_to_string(
            "app/components/media_card.html",
            {
                "item": item,
                "media": SimpleNamespace(
                    album=album,
                    status=None,
                    progress=None,
                    next_event=None,
                    episodes_left=0,
                ),
                "user": self.user,
                "title": item.title,
                "show_status_chip": False,
                "show_progress_chip": False,
            },
            request=request,
        )

        self.assertIn(
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "card-artist",
                    "album_id": album.id,
                    "album_slug": "card-album",
                },
            ),
            content,
        )

    def test_history_card_uses_canonical_music_album_url(self):
        """Music history cards should link to the shared nested album route."""
        artist = Artist.objects.create(name="History Artist")
        album = Album.objects.create(title="History Album", artist=artist)
        item = Item.objects.create(
            media_id="track-history-1",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title="History Song",
            image="http://example.com/history-album.jpg",
        )
        request = self.request_factory.get("/history")
        request.user = self.user

        content = render_to_string(
            "app/components/history_card.html",
            {
                "entry": SimpleNamespace(
                    media_type=MediaTypes.MUSIC.value,
                    album=album,
                    item=item,
                    poster=item.image,
                    status=None,
                    runtime_display=None,
                    display_title=item.title,
                    title=item.title,
                    played_at_local=timezone.now(),
                    time_range_display="6:00 PM",
                    play_count=1,
                    progress_display=None,
                    episode_label=None,
                    episode_code=None,
                    show=None,
                    score=None,
                    entry_key="music-entry-1",
                    instance_id=1,
                ),
                "card_class": "search-result-card-square",
                "history_mode": "history",
                "user": self.user,
            },
            request=request,
        )

        self.assertIn(
            reverse(
                "music_album_details",
                kwargs={
                    "artist_id": artist.id,
                    "artist_slug": "history-artist",
                    "album_id": album.id,
                    "album_slug": "history-album",
                },
            ),
            content,
        )

    def test_match_percent_clamps_and_rounds(self):
        """match_percent should clamp values to [0,100] and round."""
        self.assertEqual(app_tags.match_percent(0.9123), 91)
        self.assertEqual(app_tags.match_percent(1.6), 100)
        self.assertEqual(app_tags.match_percent(-0.4), 0)
        self.assertEqual(app_tags.match_percent(None), None)

    def test_media_url(self):
        """Test the media_url filter."""
        # Test with object for TV
        tv_url = app_tags.media_url(self.tv_item)
        expected_tv_url = reverse(
            "media_details",
            kwargs={
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "media_id": "1668",
                "title": "test-tv-show",
            },
        )
        self.assertEqual(tv_url, expected_tv_url)

        # Test with dict for TV
        tv_dict_url = app_tags.media_url(self.tv_dict)
        self.assertEqual(tv_dict_url, expected_tv_url)

        # Test with object for Season
        season_url = app_tags.media_url(self.season_item)
        expected_season_url = reverse(
            "season_details",
            kwargs={
                "source": Sources.TMDB.value,
                "media_id": "1668",
                "title": "test-tv-show",
                "season_number": 1,
            },
        )
        self.assertEqual(season_url, expected_season_url)

        # Test with dict for Season
        season_dict_url = app_tags.media_url(self.season_dict)
        self.assertEqual(season_dict_url, expected_season_url)

    def test_component_id(self):
        """Test the component_id tag."""
        # Test with object for TV
        tv_id = app_tags.component_id("card", self.tv_item)
        self.assertEqual(tv_id, "card-tv-1668")

        # Test with dict for TV
        tv_dict_id = app_tags.component_id("card", self.tv_dict)
        self.assertEqual(tv_dict_id, "card-tv-1668")

        # Test with object for Season
        season_id = app_tags.component_id("card", self.season_item)
        self.assertEqual(season_id, "card-season-1668-1")

        # Test with dict for Season
        season_dict_id = app_tags.component_id("card", self.season_dict)
        self.assertEqual(season_dict_id, "card-season-1668-1")

        # Test with object for Episode
        episode_id = app_tags.component_id("card", self.episode_item)
        self.assertEqual(episode_id, "card-episode-1668-1-1")

        # Test with dict for Episode
        episode_dict_id = app_tags.component_id("card", self.episode_dict)
        self.assertEqual(episode_dict_id, "card-episode-1668-1-1")

        # Objects without season/episode attributes should still resolve safely
        candidate_like = SimpleNamespace(
            media_type=MediaTypes.TV.value,
            media_id="1668",
        )
        self.assertEqual(app_tags.component_id("card", candidate_like), "card-tv-1668")

    def test_media_view_url(self):
        """Test the media_view_url tag."""
        # Test with object for TV
        tv_modal = app_tags.media_view_url("track_modal", self.tv_item)
        expected_tv_modal = reverse(
            "track_modal",
            kwargs={
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "media_id": "1668",
            },
        )
        self.assertEqual(tv_modal, expected_tv_modal)

        # Test with dict for TV
        tv_dict_modal = app_tags.media_view_url("track_modal", self.tv_dict)
        self.assertEqual(tv_dict_modal, expected_tv_modal)

        # Test with object for Episode
        episode_modal = app_tags.media_view_url("history_modal", self.episode_item)
        expected_episode_modal = reverse(
            "history_modal",
            kwargs={
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1668",
                "season_number": 1,
                "episode_number": 1,
            },
        )
        self.assertEqual(episode_modal, expected_episode_modal)

        # Test with dict for Episode
        episode_dict_modal = app_tags.media_view_url("history_modal", self.episode_dict)
        self.assertEqual(episode_dict_modal, expected_episode_modal)

        # Test with podcast ID containing path separators
        podcast_episode_dict = {
            "source": Sources.POCKETCASTS.value,
            "media_type": MediaTypes.PODCAST.value,
            "media_id": "gid://art19-episode-locator/V0/MCjgWTshRbS9H7f24imvk8a2E6Zsyb6NQJHy6B0h6hQ",
        }
        podcast_lists_modal = app_tags.media_view_url(
            "lists_modal",
            podcast_episode_dict,
        )
        expected_podcast_lists_modal = reverse(
            "lists_modal",
            kwargs={
                "source": Sources.POCKETCASTS.value,
                "media_type": MediaTypes.PODCAST.value,
                "media_id": podcast_episode_dict["media_id"],
            },
        )
        self.assertEqual(podcast_lists_modal, expected_podcast_lists_modal)

        # Objects without season/episode attributes should still resolve safely
        candidate_like = SimpleNamespace(
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            media_id="1668",
        )
        self.assertEqual(
            app_tags.media_view_url("track_modal", candidate_like),
            expected_tv_modal,
        )

    def test_unicode_icon(self):
        """Test the unicode_icon tag for all media types."""
        # Test all media types from MediaTypes
        for media_type in MediaTypes.values:
            try:
                result = app_tags.unicode_icon(media_type)
                # Just check that we get a non-empty string
                self.assertTrue(isinstance(result, str))
                self.assertTrue(len(result) > 0)
            except KeyError:
                self.fail(f"unicode_icon raised KeyError for {media_type}")

    def test_icon_media_types(self):
        """Test the icon tag for all media types."""
        # Test all media types from MediaTypes
        for media_type in MediaTypes.values:
            try:
                # Test with both active and inactive states
                active_result = app_tags.icon(media_type, is_active=True)
                inactive_result = app_tags.icon(media_type, is_active=False)

                # Just check that we get a non-empty string
                self.assertTrue(isinstance(active_result, str))
                self.assertTrue(len(active_result) > 0)
                self.assertTrue(isinstance(inactive_result, str))
                self.assertTrue(len(inactive_result) > 0)
            except KeyError:
                self.fail(f"icon raised KeyError for {media_type}")

    def test_show_media_score(self):
        """Test if we should show media rating or not."""
        # Create mock users
        mock_user_show = MagicMock()
        mock_user_show.hide_zero_rating = False

        mock_user_hide = MagicMock()
        mock_user_hide.hide_zero_rating = True

        # With hide_zero_rating=False, show all non-None scores
        self.assertTrue(app_tags.show_media_score(1, mock_user_show))
        self.assertTrue(app_tags.show_media_score(0, mock_user_show))
        self.assertFalse(app_tags.show_media_score(None, mock_user_show))

        # With hide_zero_rating=True, hide zero scores
        self.assertTrue(app_tags.show_media_score(1, mock_user_hide))
        self.assertFalse(app_tags.show_media_score(0, mock_user_hide))
        self.assertFalse(app_tags.show_media_score(None, mock_user_hide))
