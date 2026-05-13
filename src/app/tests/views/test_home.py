from datetime import UTC, datetime
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import live_playback
from app.models import (
    TV,
    Anime,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Podcast,
    PodcastEpisode,
    PodcastShow,
    ProviderMetadataStatus,
    Season,
    Sources,
    Status,
)
from users.models import DateFormatChoices, HomeScreenRow, HomeSortChoices


class HomeViewTests(TestCase):
    """Test the home view."""

    def _get_group(self, response, media_type):
        return next(
            (
                group
                for group in response.context["home_groups"]
                if group["media_type"] == media_type
            ),
            None,
        )

    def _get_first_row(self, response, media_type):
        group = self._get_group(response, media_type)
        self.assertIsNotNone(group)
        self.assertTrue(group["rows"])
        return group["rows"][0]

    def _get_media_entry(self, response, media_type, media_id):
        row = self._get_first_row(response, media_type)
        return next(
            entry
            for entry in row["items"]
            if entry.item.media_id == media_id
        )

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        season_item, _ = Item.objects.get_or_create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={
                "title": "Test TV Show",
                "image": "http://example.com/image.jpg",
            },
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        for i in range(1, 6):  # Create 5 episodes
            episode_item, _ = Item.objects.get_or_create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=i,
                defaults={
                    "title": "Test TV Show",
                    "image": "http://example.com/image.jpg",
                },
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=timezone.now() - timezone.timedelta(days=i),
            )

        anime_item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/image.jpg",
        )
        Anime.objects.create(
            item=anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=10,
        )

    def tearDown(self):
        live_playback.clear_user_playback_state(self.user.id)
        super().tearDown()

    def test_home_view(self):
        """Home should render grouped row data for configured libraries."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/home.html")
        self.assertIn("home_groups", response.context)

        season_row = self._get_first_row(response, MediaTypes.SEASON.value)
        anime_row = self._get_first_row(response, MediaTypes.ANIME.value)

        self.assertEqual(season_row["title"], "In Progress")
        self.assertEqual(season_row["title_main"], "In Progress")
        self.assertIsNone(season_row["title_detail"])
        self.assertEqual(len(season_row["items"]), 1)
        self.assertEqual(season_row["items"][0].media.progress, 5)
        self.assertEqual(len(anime_row["items"]), 1)
        self.assertIsNone(self._get_group(response, MediaTypes.TV.value))
        self.assertContains(
            response,
            'class="flex flex-nowrap gap-4 overflow-x-auto pb-3 pr-2 home-row-scrollbar-hidden"',
            html=False,
        )
        self.assertContains(response, 'data-home-row-sort-toggle="true"', html=False)
        self.assertContains(response, 'aria-label="Toggle sort direction for In Progress"', html=False)
        self.assertNotContains(response, "Sorted by Title • Ascending", html=False)
        self.assertContains(response, 'class="home-row-card w-44 shrink-0"', html=False)
        self.assertContains(response, 'data-home-row="true"', html=False)
        self.assertNotContains(response, '<h2 class="text-2xl font-semibold">', html=False)
        self.assertNotContains(response, "Load All")

    def test_home_view_planning_rows_show_full_release_date_subtitle(self):
        """Planning rows should show the full release date instead of only the year."""
        self.user.date_format = DateFormatChoices.ISO_8601
        self.user.save(update_fields=["date_format"])

        movie_item = Item.objects.create(
            media_id="planning-movie",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Planning Movie",
            image="http://example.com/planning-movie.jpg",
            release_datetime=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
        )
        Movie.objects.create(
            item=movie_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        self.client.get(reverse("home"))
        movie_row = HomeScreenRow.objects.get(
            user=self.user,
            media_type=MediaTypes.MOVIE.value,
            row_type="library_query",
            position=0,
        )
        movie_row.filters = {
            **(movie_row.filters or {}),
            "status": Status.PLANNING.value,
        }
        movie_row.save(update_fields=["filters"])

        response = self.client.get(reverse("home"))

        self.assertContains(response, "2026-05-12")

    def test_home_podcast_cards_use_standard_card_width(self):
        """Podcast Home rows should use the same card width as other media rows."""
        podcast_show = PodcastShow.objects.create(
            podcast_uuid="show-home-width",
            title="Home Width Podcast",
            image="http://example.com/podcast.jpg",
        )
        podcast_episode = PodcastEpisode.objects.create(
            show=podcast_show,
            episode_uuid="episode-home-width",
            title="Podcast Episode",
            duration=1800,
        )
        podcast_item = Item.objects.create(
            media_id="episode-home-width",
            source=Sources.POCKETCASTS.value,
            media_type=MediaTypes.PODCAST.value,
            title="Podcast Episode",
            image="http://example.com/podcast.jpg",
        )
        Podcast.objects.create(
            item=podcast_item,
            user=self.user,
            show=podcast_show,
            episode=podcast_episode,
            status=Status.IN_PROGRESS.value,
            progress=300,
        )

        response = self.client.get(reverse("home"))

        podcast_row = self._get_first_row(response, MediaTypes.PODCAST.value)
        self.assertEqual(podcast_row["card_width_class"], "w-44")
        self.assertContains(response, 'class="w-44 shrink-0"', html=False)
        self.assertNotContains(response, 'class="w-52 shrink-0"', html=False)

    def test_home_view_hides_disabled_sidebar_media_types_even_when_rows_exist(self):
        """Stored rows for disabled libraries should not render on Home."""
        self.client.get(reverse("home"))
        self.assertTrue(
            HomeScreenRow.objects.filter(
                user=self.user,
                media_type=MediaTypes.SEASON.value,
            ).exists(),
        )

        self.user.season_enabled = False
        self.user.save(update_fields=["season_enabled"])

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self._get_group(response, MediaTypes.SEASON.value))
        self.assertTrue(
            HomeScreenRow.objects.filter(
                user=self.user,
                media_type=MediaTypes.SEASON.value,
            ).exists(),
        )

    def test_home_view_uses_saved_home_sort_for_seeded_rows(self):
        """Legacy home sort should seed the default library rows."""
        self.user.home_sort = HomeSortChoices.COMPLETION
        self.user.save(update_fields=["home_sort"])

        response = self.client.get(reverse("home"))

        season_row = self._get_first_row(response, MediaTypes.SEASON.value)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(season_row["summary"], "Sorted by Completion • Descending")

    def test_home_view_defaults_seasons_to_upcoming_and_movies_to_recent(self):
        """Default rows should mirror the old Home behavior by media type."""
        movie_item = Item.objects.create(
            media_id="movie-default",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Default Movie",
            image="http://example.com/movie.jpg",
        )
        Movie.objects.create(
            item=movie_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=1,
        )

        response = self.client.get(reverse("home"))

        season_row = self._get_first_row(response, MediaTypes.SEASON.value)
        movie_row = self._get_first_row(response, MediaTypes.MOVIE.value)
        self.assertEqual(season_row["summary"], "Sorted by Upcoming • Ascending")
        self.assertEqual(season_row["summary_inline"], "Upcoming")
        self.assertEqual(movie_row["summary"], "Sorted by Recent • Descending")
        self.assertEqual(movie_row["summary_inline"], "Recent")
        self.assertEqual(season_row["title_main"], "In Progress")
        self.assertIsNone(season_row["title_detail"])
        self.assertEqual(movie_row["title_main"], "In Progress")
        self.assertIsNone(movie_row["title_detail"])

    def test_home_view_shows_filter_suffix_in_inline_title(self):
        """Filtered rows should keep the filter summary beside the main title."""
        self.client.get(reverse("home"))
        row = HomeScreenRow.objects.get(
            user=self.user,
            media_type=MediaTypes.SEASON.value,
            row_type="library_query",
            position=0,
        )
        row.filters = {
            **row.filters,
            "release": "not_released",
        }
        row.save(update_fields=["filters"])

        response = self.client.get(reverse("home"))

        season_row = self._get_first_row(response, MediaTypes.SEASON.value)
        self.assertEqual(season_row["title"], "In Progress • Not Released")
        self.assertEqual(season_row["title_main"], "In Progress")
        self.assertEqual(season_row["title_detail"], "Not Released")
        self.assertEqual(len(season_row["items"]), 1)
        self.assertEqual(season_row["summary_inline"], "Upcoming")

    def test_home_view_accepts_legacy_in_progress_status_alias(self):
        """Legacy seeded Home rows using the old status label should still render."""
        self.client.get(reverse("home"))
        row = HomeScreenRow.objects.get(
            user=self.user,
            media_type=MediaTypes.SEASON.value,
            row_type="library_query",
            position=0,
        )
        row.filters = {
            **row.filters,
            "status": "In Progress",
        }
        row.save(update_fields=["filters"])

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        season_row = self._get_first_row(response, MediaTypes.SEASON.value)
        self.assertEqual(season_row["title"], "In Progress")
        self.assertEqual(len(season_row["items"]), 1)

    def test_home_view_upcoming_falls_back_to_recent_activity_for_movies(self):
        """Legacy Upcoming behavior should sort no-event libraries by last activity."""
        older_item = Item.objects.create(
            media_id="movie-older",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Older Movie",
            image="http://example.com/older.jpg",
        )
        newer_item = Item.objects.create(
            media_id="movie-newer",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Newer Movie",
            image="http://example.com/newer.jpg",
        )
        older_movie = Movie.objects.create(
            item=older_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=1,
        )
        newer_movie = Movie.objects.create(
            item=newer_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=1,
        )
        older_movie.progressed_at = timezone.now() - timezone.timedelta(days=2)
        older_movie.save(update_fields=["progressed_at"])
        newer_movie.progressed_at = timezone.now() - timezone.timedelta(hours=1)
        newer_movie.save(update_fields=["progressed_at"])

        response = self.client.get(reverse("home"))

        movie_row = self._get_first_row(response, MediaTypes.MOVIE.value)
        self.assertEqual(
            [entry.item.media_id for entry in movie_row["items"][:2]],
            ["movie-newer", "movie-older"],
        )

    def test_home_view_includes_active_playback_card_context(self):
        """Active playback cache state should render above the TV seasons section."""
        now_ts = int(timezone.now().timestamp())
        live_playback.set_user_playback_state(
            self.user.id,
            {
                "event_type": "media.play",
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "rating_key": "rk-home-test",
                "title": "Episode Test Title",
                "series_title": "Test TV Show",
                "episode_title": "Episode Test Title",
                "season_number": 1,
                "episode_number": 3,
                "view_offset_seconds": 1447,
                "duration_seconds": 2666,
                "status": live_playback.PLAYBACK_STATUS_PLAYING,
                "updated_at_ts": now_ts,
                "expires_at_ts": now_ts + 600,
                "pause_expires_at_ts": None,
                "scrobble_expires_at_ts": None,
            },
        )

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        card = response.context.get("active_playback_card")
        self.assertIsNotNone(card)
        self.assertEqual(card["title"], "Test TV Show")
        self.assertEqual(card["episode_code"], "S01E03")
        self.assertIn(" / ", card["progress_display"])
        self.assertContains(response, "Now Playing")
        self.assertContains(response, "data-active-playback-card")

    def test_home_view_marks_season_show_poster_as_fallback(self):
        show_image = "https://images.example.com/show-poster.jpg"
        tv_item = Item.objects.create(
            media_id="fallback-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Fallback Show",
            image=show_image,
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="fallback-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Fallback Show",
            image=settings.IMG_NONE,
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        episode_item = Item.objects.create(
            media_id="fallback-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Fallback Episode",
            image=settings.IMG_NONE,
            season_number=1,
            episode_number=1,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=timezone.now(),
        )

        response = self.client.get(reverse("home"))

        fallback_season = self._get_media_entry(
            response,
            MediaTypes.SEASON.value,
            "fallback-show",
        ).media
        self.assertEqual(fallback_season.card_image_override, show_image)
        self.assertEqual(fallback_season.card_image_source, "fallback")
        self.assertContains(response, 'data-image-source="fallback"')

    def test_home_view_shows_local_only_chip_for_flagged_season(self):
        season_item = Item.objects.get(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
        )
        season_item.provider_metadata_status = (
            ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value
        )
        season_item.save(update_fields=["provider_metadata_status"])

        response = self.client.get(reverse("home"))

        flagged_season = self._get_media_entry(
            response,
            MediaTypes.SEASON.value,
            "1668",
        ).media
        self.assertEqual(
            flagged_season.item.provider_metadata_status,
            ProviderMetadataStatus.LOCAL_ONLY_MISSING_SEASON.value,
        )
        self.assertContains(response, "Local only")

    @patch("app.live_playback._fetch_episode_still")
    @patch("app.helpers.get_tmdb_backdrop_image")
    def test_home_playback_card_uses_backdrop_when_episode_image_is_inherited_poster(
        self,
        mock_get_tmdb_backdrop_image,
        mock_fetch_episode_still,
    ):
        show_image = "https://images.example.com/show-poster.jpg"
        backdrop_image = "https://images.example.com/show-backdrop.jpg"
        mock_fetch_episode_still.return_value = (None, "none")
        mock_get_tmdb_backdrop_image.return_value = backdrop_image

        tv_item = Item.objects.create(
            media_id="playback-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Playback Show",
            image=show_image,
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="playback-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Playback Show",
            image=show_image,
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )
        episode_item = Item.objects.create(
            media_id="playback-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Playback Episode",
            image=show_image,
            season_number=1,
            episode_number=2,
        )
        Episode.objects.create(
            item=episode_item,
            related_season=season,
            end_date=timezone.now(),
        )

        now_ts = int(timezone.now().timestamp())
        live_playback.set_user_playback_state(
            self.user.id,
            {
                "event_type": "media.play",
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "playback-show",
                "source": Sources.TMDB.value,
                "rating_key": "rk-backdrop-fallback",
                "title": "Playback Episode",
                "series_title": "Playback Show",
                "episode_title": "Playback Episode",
                "season_number": 1,
                "episode_number": 2,
                "view_offset_seconds": 180,
                "duration_seconds": 1800,
                "status": live_playback.PLAYBACK_STATUS_PLAYING,
                "updated_at_ts": now_ts,
                "expires_at_ts": now_ts + 600,
                "pause_expires_at_ts": None,
                "scrobble_expires_at_ts": None,
            },
        )

        response = self.client.get(reverse("home"))

        card = response.context["active_playback_card"]
        self.assertEqual(card["image"], backdrop_image)
        self.assertEqual(card["image_source"], "fallback")

    def test_home_view_htmx_load_more(self):
        """HTMX row expansion should return only the overflow items."""
        for i in range(6, 20):  # Create 14 more TV shows (we already have 1)
            season_item = Item.objects.create(
                media_id=str(i),
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title=f"Test TV Show {i}",
                image="http://example.com/image.jpg",
                season_number=1,
            )
            season = Season.objects.create(
                item=season_item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

            episode_item, _ = Item.objects.get_or_create(
                media_id=str(i),
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=1,
                defaults={
                    "title": f"Test TV Show {i}",
                    "image": "http://example.com/image.jpg",
                },
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=timezone.now(),
            )

        initial_response = self.client.get(reverse("home"))
        season_row = self._get_first_row(initial_response, MediaTypes.SEASON.value)
        self.assertContains(initial_response, 'data-loaded-count="14"', html=False)
        self.assertContains(initial_response, 'data-home-row-sentinel="true"', html=False)
        self.assertContains(initial_response, 'data-home-row-prefetch-distance="1152"', html=False)
        self.assertContains(initial_response, 'hx-trigger="home-row-load-more"', html=False)
        self.assertContains(
            initial_response,
            'hx-vals=\'js:{offset: Number(event.target.dataset.loadedCount || 0)}\'',
            html=False,
        )

        response = self.client.get(
            reverse("home") + f"?load_row={season_row['row_id']}&offset=14",
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/home_grid.html")
        self.assertIn("media_list", response.context)
        self.assertEqual(season_row["total"], 15)
        self.assertEqual(len(response.context["media_list"]["items"]), 1)
        self.assertEqual(response.context["media_list"]["total"], 15)
        self.assertContains(response, 'class="home-row-card w-44 shrink-0"', html=False)

    def test_active_playback_fragment_empty(self):
        """Fragment endpoint returns empty body when nothing is playing."""
        response = self.client.get(reverse("active_playback_fragment"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")

    def test_active_playback_fragment_with_state(self):
        """Fragment endpoint returns card HTML when something is playing."""
        now_ts = int(timezone.now().timestamp())
        live_playback.set_user_playback_state(
            self.user.id,
            {
                "event_type": "media.play",
                "media_type": MediaTypes.EPISODE.value,
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "rating_key": "rk-frag",
                "title": "Frag Episode",
                "series_title": "Test TV Show",
                "episode_title": "Frag Episode",
                "season_number": 1,
                "episode_number": 2,
                "view_offset_seconds": 300,
                "duration_seconds": 2400,
                "status": live_playback.PLAYBACK_STATUS_PLAYING,
                "updated_at_ts": now_ts,
                "expires_at_ts": now_ts + 600,
                "pause_expires_at_ts": None,
                "scrobble_expires_at_ts": None,
            },
        )
        response = self.client.get(reverse("active_playback_fragment"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Now Playing")
        self.assertContains(response, "data-active-playback-card")
        self.assertContains(response, "Test TV Show")
        self.assertContains(response, "S01E02")
