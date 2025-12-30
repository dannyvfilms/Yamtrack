import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from app import cache_utils, history_cache
from app.models import (
    TV,
    Anime,
    BasicMedia,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from app.templatetags import app_tags
from app.views import _sort_tv_media_by_time_left
from events.models import Event
from users.models import HomeSortChoices


class HomeViewTests(TestCase):
    """Test the home view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        # Create a season for the TV show
        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        # Create episodes for the season
        for i in range(1, 6):  # Create 5 episodes
            episode_item = Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Test TV Show",
                image="http://example.com/image.jpg",
                season_number=1,
                episode_number=i,
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=timezone.now() - timezone.timedelta(days=i),
            )

        # Create anime
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

    def test_home_view(self):
        """Test the home view displays in-progress media."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/home.html")

        # Check that both media items are in the context
        self.assertIn("list_by_type", response.context)
        self.assertIn(MediaTypes.SEASON.value, response.context["list_by_type"])
        self.assertIn(MediaTypes.ANIME.value, response.context["list_by_type"])

        # Check that sort choices are in the context
        self.assertIn("sort_choices", response.context)
        self.assertEqual(response.context["sort_choices"], HomeSortChoices.choices)

        # Verify Season progress is calculated correctly (5 episodes)
        season = response.context["list_by_type"][MediaTypes.SEASON.value]
        self.assertEqual(len(season["items"]), 1)
        self.assertEqual(season["items"][0].progress, 5)

    def test_home_view_recently_unrated_section(self):
        """Recently played, unrated items appear on the home view."""
        movie_item = Item.objects.create(
            media_id="recent-movie",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Recent Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            end_date=timezone.now() - timezone.timedelta(days=1),
            score=None,
        )

        response = self.client.get(reverse("home"))

        recent_section = response.context["list_by_type"].get("recently_not_rated")
        self.assertIsNotNone(recent_section)
        self.assertTrue(
            any(media.item.id == movie_item.id for media in recent_section["items"]),
        )

    def test_home_view_with_sort(self):
        """Test the home view with sorting parameter."""
        response = self.client.get(reverse("home") + "?sort=completion")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "completion")

        # Check that user preference was updated
        self.user.refresh_from_db()
        self.assertEqual(self.user.home_sort, "completion")

    @patch("app.providers.services.get_media_metadata")
    def test_home_view_htmx_load_more(self, mock_get_media_metadata):
        """Test the HTMX load more functionality."""
        # Mock the API response
        mock_get_media_metadata.return_value = {
            "title": "Test TV Show",
            "image": "http://example.com/image.jpg",
            "season/1": {
                "episodes": [{"id": 1}, {"id": 2}, {"id": 3}],  # 3 episodes
            },
            "related": {
                "seasons": [
                    {"season_number": 1, "image": "http://example.com/image.jpg"},
                ],  # Only one season
            },
        }

        # Create TV shows (just enough to test load more)
        for i in range(6, 20):  # Create 14 more TV shows (we already have 1)
            # Create a season for each TV show
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

            # Create an episode for each season
            episode_item = Item.objects.create(
                media_id=str(i),
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title=f"Test TV Show {i}",
                image="http://example.com/image.jpg",
                season_number=1,
                episode_number=1,
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=timezone.now(),
            )

        # Now test the load more functionality
        headers = {"HTTP_HX_REQUEST": "true"}
        response = self.client.get(
            reverse("home") + "?load_media_type=season",
            **headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/home_grid.html")

        # Check that media_list is in the context
        self.assertIn("media_list", response.context)

        # Check the structure of media_list
        self.assertIn("items", response.context["media_list"])
        self.assertIn("total", response.context["media_list"])

        # Since we're loading more (items after the first 14),
        # we should have at least 1 item in the response
        self.assertEqual(len(response.context["media_list"]["items"]), 1)
        self.assertEqual(
            response.context["media_list"]["total"],
            15,
        )  # 15 TV shows total

class MediaListViewTests(TestCase):
    """Test the media list view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        movies_id = ["278", "238", "129", "424", "680"]
        num_completed = 3
        # Create some media items for testing
        for i in range(1, 6):
            item = Item.objects.create(
                media_id=movies_id[i - 1],
                source=Sources.TMDB.value,
                media_type=MediaTypes.MOVIE.value,
                title=f"Test Movie {i}",
                image="http://example.com/image.jpg",
            )
            status = (
                Status.COMPLETED.value
                if i < num_completed
                else Status.IN_PROGRESS.value
            )
            Movie.objects.create(
                item=item,
                user=self.user,
                status=status,
                progress=1 if i < num_completed else 0,
                score=i,
            )

    def test_media_list_view(self):
        """Test the media list view displays media items."""
        response = self.client.get(reverse("medialist", args=[MediaTypes.MOVIE.value]))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")

        # Check that media items are in the context
        self.assertIn("media_list", response.context)
        self.assertEqual(response.context["media_list"].paginator.count, 5)

        # Check that filter options are in the context
        self.assertIn("sort_choices", response.context)
        self.assertIn("status_choices", response.context)
        self.assertEqual(response.context["media_type"], MediaTypes.MOVIE.value)
        self.assertEqual(
            response.context["media_type_plural"],
            app_tags.media_type_readable_plural(MediaTypes.MOVIE.value).lower(),
        )

    def test_media_list_with_filters(self):
        """Test the media list view with filters."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value])
            + "?status=Completed&sort=score&layout=table",
        )

        self.assertEqual(response.status_code, 200)

        # Check that filters are applied
        self.assertEqual(
            response.context["current_status"],
            Status.COMPLETED.value,
        )
        self.assertEqual(response.context["current_sort"], "score")
        self.assertEqual(response.context["current_layout"], "table")

        # Check that the media list is filtered
        self.assertEqual(response.context["media_list"].paginator.count, 3)

        # Check that user preferences were updated
        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_status, Status.COMPLETED.value)
        self.assertEqual(self.user.movie_sort, "score")
        self.assertEqual(self.user.movie_layout, "table")

    def test_media_list_rating_filter(self):
        """Test the media list view with rating filter."""
        unrated_item = Item.objects.create(
            media_id="unrated",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Unrated Movie",
            image="http://example.com/image.jpg",
        )
        Movie.objects.create(
            item=unrated_item,
            user=self.user,
            status=Status.COMPLETED.value,
            progress=1,
            score=None,
        )

        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?rating=not_rated",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_rating"], "not_rated")
        self.assertEqual(response.context["media_list"].paginator.count, 1)

    def test_media_list_persists_direction(self):
        """Sort direction should be remembered across requests."""
        url = reverse("medialist", args=[MediaTypes.MOVIE.value])

        first_response = self.client.get(f"{url}?sort=title&direction=asc")
        self.assertEqual(first_response.context["current_direction"], "asc")

        self.user.refresh_from_db()
        self.assertEqual(self.user.movie_sort, "title")
        self.assertEqual(self.user.movie_direction, "asc")

        second_response = self.client.get(url)
        self.assertEqual(second_response.context["current_sort"], "title")
        self.assertEqual(second_response.context["current_direction"], "asc")

    @patch("app.views.services.get_media_metadata")
    @patch("app.models.providers.services.get_media_metadata")
    def test_time_left_cache_invalidated_on_episode_watch(
        self,
        mock_models_metadata,
        mock_views_metadata,
    ):
        """Ensure time-left cache is invalidated when an episode is watched."""

        def fake_metadata(media_type, media_id, source, extra=None):
            if media_type == MediaTypes.SEASON.value:
                return {
                    "episodes": [
                        {
                            "episode_number": 1,
                            "runtime": "45m",
                            "image": "http://example.com/still.jpg",
                        },
                    ],
                }
            if media_type == MediaTypes.TV.value:
                return {
                    "max_progress": 1,
                    "title": "Secret Service",
                    "related": {"seasons": []},
                }
            if media_type == "tv_with_seasons":
                season_number = extra[0] if extra else 1
                return {
                    "title": "Secret Service",
                    "related": {"seasons": [{"season_number": season_number}]},
                    f"season/{season_number}": {
                        "episodes": [
                            {
                                "episode_number": 1,
                                "runtime": "45m",
                                "image": "http://example.com/still.jpg",
                            },
                        ],
                    },
                }
            return {}

        mock_models_metadata.side_effect = fake_metadata
        mock_views_metadata.side_effect = fake_metadata

        tv_item = Item.objects.create(
            media_id="secret-service",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Secret Service",
            image="http://example.com/tv.jpg",
            runtime_minutes=45,
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.create(
            media_id="secret-service",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Secret Service",
            image="http://example.com/season.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )

        Event.objects.create(
            item=season_item,
            content_number=1,
            datetime=timezone.now() - timezone.timedelta(days=1),
        )

        cache.clear()
        url = reverse("medialist", args=[MediaTypes.TV.value])
        response = self.client.get(f"{url}?sort=time_left")
        self.assertEqual(response.status_code, 200)

        page = response.context["media_list"]
        self.assertGreater(page.paginator.count, 0)
        first_media = page.object_list[0]
        self.assertNotEqual(getattr(first_media, "time_left_display", None), "0m")

        status_filter = response.context["current_status"]
        direction = response.context["current_direction"]
        rating_filter = response.context["current_rating"]
        cache_key = cache_utils.build_time_left_cache_key(
            self.user.id,
            MediaTypes.TV.value,
            status_filter,
            "",
            direction,
            rating_filter,
        )
        self.assertIsNotNone(cache.get(cache_key))

        season.watch(1, timezone.now())

        self.assertIsNone(cache.get(cache_key))

        response_after = self.client.get(f"{url}?sort=time_left")
        self.assertEqual(response_after.status_code, 200)
        page_after = response_after.context["media_list"]
        updated_media = page_after.object_list[0]
        self.assertEqual(getattr(updated_media, "time_left_display", None), "0m")

    @patch("app.views.Item.fetch_releases")
    def test_time_left_ignores_future_episode_release_dates(self, mock_fetch_releases):
        """Ensure future-dated episodes are not counted in time left calculations."""
        mock_fetch_releases.side_effect = AssertionError("fetch_releases should not be called")

        now = timezone.now()
        tv_item = Item.objects.create(
            media_id="next-level-chef",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Next Level Chef",
            image="http://example.com/tv.jpg",
            runtime_minutes=42,
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=1,
        )

        season_item = Item.objects.create(
            media_id="next-level-chef",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Next Level Chef",
            image="http://example.com/season.jpg",
            season_number=1,
        )
        Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )

        Item.objects.create(
            media_id="next-level-chef",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Next Level Chef",
            image="http://example.com/tv.jpg",
            season_number=1,
            episode_number=1,
            release_datetime=now - timezone.timedelta(days=1),
        )
        Item.objects.create(
            media_id="next-level-chef",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Next Level Chef",
            image="http://example.com/tv.jpg",
            season_number=1,
            episode_number=2,
            release_datetime=now + timezone.timedelta(days=7),
        )

        Event.objects.create(
            item=season_item,
            content_number=1,
            datetime=now - timezone.timedelta(days=1),
        )
        Event.objects.create(
            item=season_item,
            content_number=2,
            datetime=now + timezone.timedelta(days=7),
        )

        BasicMedia.objects.annotate_max_progress([tv], MediaTypes.TV.value)
        sorted_media = _sort_tv_media_by_time_left([tv])
        self.assertEqual(sorted_media[0].max_progress, 1)
        self.assertEqual(sorted_media[0].episodes_left_display, 0)

    @patch("app.views.Item.fetch_releases")
    def test_time_left_counts_episode_released_today(self, mock_fetch_releases):
        """Episodes released today should be considered available."""
        mock_fetch_releases.side_effect = AssertionError("fetch_releases should not be called")

        now = timezone.now()
        tv_item = Item.objects.create(
            media_id="cooking-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Cooking Show",
            image="http://example.com/tv.jpg",
            runtime_minutes=42,
        )
        tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=0,
        )

        season_item = Item.objects.create(
            media_id="cooking-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Cooking Show",
            image="http://example.com/season.jpg",
            season_number=1,
        )
        Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv,
            status=Status.IN_PROGRESS.value,
        )

        Item.objects.create(
            media_id="cooking-show",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Cooking Show",
            image="http://example.com/tv.jpg",
            season_number=1,
            episode_number=1,
            release_datetime=now,
        )

        Event.objects.create(
            item=season_item,
            content_number=1,
            datetime=now,
        )

        BasicMedia.objects.annotate_max_progress([tv], MediaTypes.TV.value)
        sorted_media = _sort_tv_media_by_time_left([tv])
        self.assertEqual(sorted_media[0].max_progress, 1)
        self.assertEqual(sorted_media[0].episodes_left_display, 1)

    def test_media_list_htmx_request(self):
        """Test the media list view with HTMX request."""
        headers = {"HTTP_HX_REQUEST": "true"}

        # Test grid layout
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=grid",
            **headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_grid_items.html")

        # Test table layout
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]) + "?layout=table",
            **headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/media_table_items.html")


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
        # Mock the search results
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

        # Check that user preference was updated
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_search_type, MediaTypes.MOVIE.value)

        # Verify the search function was called with correct parameters
        mock_search.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "test",
            1,
            Sources.TMDB.value,
        )


class MediaDetailsViewTests(TestCase):
    """Test the media details views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @patch("app.providers.services.get_media_metadata")
    def test_media_details_view(self, mock_get_metadata):
        """Test the media details view."""
        # Mock the metadata
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

        # Check that media metadata is in the context
        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Test Movie")

        # Verify the get_media_metadata function was called with correct parameters
        mock_get_metadata.assert_called_once_with(
            MediaTypes.MOVIE.value,
            "238",
            Sources.TMDB.value,
        )

    @patch("app.providers.services.get_media_metadata")
    @patch("app.providers.tmdb.process_episodes")
    def test_season_details_view(self, mock_process_episodes, mock_get_metadata):
        """Test the season details view."""
        # Mock the metadata
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

        # Mock the processed episodes
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

        # Use the correct URL pattern for season_details
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

        # Check that season metadata is in the context
        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"]["title"], "Season 1")
        self.assertEqual(len(response.context["media"]["episodes"]), 1)

        # Verify the get_media_metadata function was called with correct parameters
        mock_get_metadata.assert_called_once_with(
            "tv_with_seasons",
            "1668",
            Sources.TMDB.value,
            [1],
        )


class TrackModalViewTests(TestCase):
    """Test the track modal view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        # Create a media item for testing
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

        # Check that form and media are in the context
        self.assertIn("form", response.context)
        self.assertIn("media", response.context)
        self.assertEqual(response.context["media"], self.movie)
        self.assertEqual(response.context["return_url"], "/home")

    @patch("app.providers.services.get_media_metadata")
    def test_track_modal_view_new_media(self, mock_get_metadata):
        """Test the track modal view for new media."""
        # Mock the metadata
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

        # Check that form is in the context with initial data
        self.assertIn("form", response.context)
        self.assertEqual(response.context["form"].initial["media_id"], "278")
        self.assertEqual(
            response.context["form"].initial["media_type"],
            MediaTypes.MOVIE.value,
        )


class HistoryViewCacheTests(TestCase):
    """Test history view cache usage."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)
        cache.clear()

    def _seed_history_cache(self, day_count):
        history_days = []
        for offset in range(day_count):
            day_date = timezone.localdate() - datetime.timedelta(days=offset)
            played_at = timezone.make_aware(
                datetime.datetime.combine(day_date, datetime.time(12, 0)),
            )
            entry = {
                "media_type": MediaTypes.MOVIE.value,
                "item": {
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": f"movie-{offset}",
                    "source": Sources.TMDB.value,
                    "title": f"Movie {offset}",
                },
                "poster": "http://example.com/poster.jpg",
                "title": f"Movie {offset}",
                "display_title": f"Movie {offset}",
                "status": Status.COMPLETED.value,
                "play_count": 1,
                "episode_label": None,
                "episode_code": None,
                "played_at_local": played_at,
                "runtime_minutes": 0,
                "runtime_display": None,
                "instance_id": offset + 1,
                "entry_key": f"movie-{offset}",
            }
            history_days.append(
                {
                    "date": day_date,
                    "weekday": day_date.strftime("%A"),
                    "date_display": day_date.strftime("%B %d, %Y"),
                    "entries": [entry],
                    "total_minutes": 0,
                    "total_runtime_display": "0min",
                },
            )

        history_cache.cache_history_payloads(self.user.id, "repeats", history_days)

    def test_history_view_uses_index_cache(self):
        """Unfiltered history view uses cached index/day payloads."""
        self._seed_history_cache(day_count=35)

        with patch("app.history_cache.get_history_days") as get_history_days:
            response = self.client.get(reverse("history"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_days"], 35)
        self.assertEqual(response.context["total_pages"], 2)
        self.assertEqual(
            len(response.context["history_days"]),
            history_cache.HISTORY_DAYS_PER_PAGE,
        )
        get_history_days.assert_not_called()

    def test_history_view_page_two(self):
        """Second page only returns remaining cached days."""
        self._seed_history_cache(day_count=35)

        response = self.client.get(reverse("history") + "?page=2")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_page"], 2)
        self.assertEqual(len(response.context["history_days"]), 5)


class HistoryModalViewTests(TestCase):
    """Test the history modal view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        # Create a media item with history
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

        # Update the movie to create history
        self.movie.status = Status.COMPLETED.value
        self.movie.progress = 1
        self.movie.score = 8
        self.movie.save()

    def test_history_modal_view(self):
        """Test the history modal view."""
        response = self.client.get(
            reverse(
                "history_modal",
                kwargs={
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.MOVIE.value,
                    "media_id": "238",
                },
            )
            + "?return_url=/home",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/fill_history.html")

        # Check that timeline entries are in the context
        self.assertIn("timeline", response.context)
        self.assertGreater(len(response.context["timeline"]), 0)

        # Check that the first entry has changes
        first_entry = response.context["timeline"][0]
        self.assertIn("changes", first_entry)
        self.assertGreater(len(first_entry["changes"]), 0)


class DeleteHistoryRecordViewTests(TestCase):
    """Test the delete history record view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        # Create a media item with history
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

        # Update the movie to create history
        self.movie.status = Status.COMPLETED.value
        self.movie.progress = 1
        self.movie.score = 8
        self.movie.save()

        # Get the history record and manually set the history_user
        self.history = self.movie.history.first()
        self.history_id = self.history.history_id

        # Manually update the history_user field
        self.history.history_user = self.user
        self.history.save()

    def test_delete_history_record(self):
        """Test deleting a history record."""
        response = self.client.delete(
            reverse(
                "delete_history_record",
                kwargs={
                    "media_type": MediaTypes.MOVIE.value,
                    "history_id": self.history_id,
                },
            ),
        )

        self.assertEqual(response.status_code, 200)

        # Check that the history record was deleted
        self.assertEqual(
            self.movie.history.filter(history_id=self.history_id).count(),
            0,
        )

    def test_delete_nonexistent_history_record(self):
        """Test deleting a nonexistent history record."""
        response = self.client.delete(
            reverse(
                "delete_history_record",
                kwargs={
                    "media_type": MediaTypes.MOVIE.value,
                    "history_id": 999999,
                },
            ),
        )

        self.assertEqual(response.status_code, 404)


class StatisticsViewTests(TestCase):
    """Test the statistics view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_statistics_view_default_date_range(self):
        """Test the statistics view with default date range (last year)."""
        # Call the view
        response = self.client.get(reverse("statistics"))

        # Check response
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/statistics.html")

        # Check that all expected context variables are present
        self.assertIn("media_count", response.context)
        self.assertIn("activity_data", response.context)
        self.assertIn("media_type_distribution", response.context)
        self.assertIn("score_distribution", response.context)
        self.assertIn("status_distribution", response.context)
        self.assertIn("status_pie_chart_data", response.context)
        self.assertNotIn("timeline", response.context)

    def test_statistics_view_uses_saved_predefined_range(self):
        """Saved predefined range is used when no date params are provided."""
        self.user.statistics_default_range = "Yesterday"
        self.user.save(update_fields=["statistics_default_range"])

        response = self.client.get(reverse("statistics"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_range_name"], "Yesterday")

    def test_statistics_view_updates_preferred_range(self):
        """Selecting a predefined range updates the user's preference."""
        today_str = timezone.localdate().strftime("%Y-%m-%d")

        response = self.client.get(
            reverse("statistics") + f"?start-date={today_str}&end-date={today_str}",
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.statistics_default_range, "Today")

    def test_statistics_view_custom_date_range(self):
        """Test the statistics view with custom date range."""
        # Custom date range
        start_date = "2023-01-01"
        end_date = "2023-12-31"

        # Call the view with custom date range
        response = self.client.get(
            reverse("statistics") + f"?start-date={start_date}&end-date={end_date}",
        )

        # Check response
        self.assertEqual(response.status_code, 200)

        # Check that all expected context variables are present
        self.assertIn("media_count", response.context)
        self.assertIn("activity_data", response.context)
        self.assertIn("media_type_distribution", response.context)
        self.assertIn("score_distribution", response.context)
        self.assertIn("status_distribution", response.context)
        self.assertIn("status_pie_chart_data", response.context)
        self.assertNotIn("timeline", response.context)

    def test_statistics_view_invalid_date_format(self):
        """Test the statistics view with invalid date format."""
        # Invalid date format
        start_date = "01/01/2023"  # MM/DD/YYYY instead of YYYY-MM-DD
        end_date = "2023/12/31"

        # Call the view with invalid date format
        response = self.client.get(
            reverse("statistics") + f"?start-date={start_date}&end-date={end_date}",
        )

        # If we get here, the view handled the invalid date format
        self.assertEqual(response.status_code, 200)

        date_is_none = (
            response.context["start_date"] is None
            and response.context["end_date"] is None
        )

        self.assertTrue(date_is_none)


class CreateMedia(TestCase):
    """Test the creation of media objects through views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    @override_settings(MEDIA_ROOT=("create_media"))
    def test_create_anime(self):
        """Test the creation of a TV object."""
        Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/image.jpg",
        )
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "1",
                "source": Sources.MAL.value,
                "media_type": MediaTypes.ANIME.value,
                "status": Status.PLANNING.value,
                "progress": 0,
                "repeats": 0,
            },
        )
        self.assertEqual(
            Anime.objects.filter(item__media_id="1", user=self.user).exists(),
            True,
        )

    @override_settings(MEDIA_ROOT=("create_media"))
    def test_create_tv(self):
        """Test the creation of a TV object through views."""
        Item.objects.create(
            media_id="5895",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Friends",
            image="http://example.com/image.jpg",
        )
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "5895",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.TV.value,
                "status": Status.PLANNING.value,
            },
        )
        self.assertEqual(
            TV.objects.filter(item__media_id="5895", user=self.user).exists(),
            True,
        )

    def test_create_season(self):
        """Test the creation of a Season through views."""
        Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        self.client.post(
            reverse("media_save"),
            {
                "media_id": "1668",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.SEASON.value,
                "season_number": 1,
                "status": Status.PLANNING.value,
            },
        )
        self.assertEqual(
            Season.objects.filter(item__media_id="1668", user=self.user).exists(),
            True,
        )

    def test_create_episodes(self):
        """Test the creation of Episode through views."""
        self.client.post(
            reverse("episode_save"),
            {
                "media_id": "1668",
                "season_number": 1,
                "episode_number": 1,
                "source": Sources.TMDB.value,
                "date": "2023-06-01T00:00",
            },
        )
        self.assertEqual(
            Episode.objects.filter(
                item__media_id="1668",
                related_season__user=self.user,
                item__episode_number=1,
            ).exists(),
            True,
        )


class EditMedia(TestCase):
    """Test the editing of media objects through views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_edit_movie_score(self):
        """Test the editing of a movie score."""
        item = Item.objects.create(
            media_id="10494",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Perfect Blue",
            image="http://example.com/image.jpg",
        )
        movie = Movie.objects.create(
            item=item,
            user=self.user,
            score=9,
            progress=1,
            status=Status.COMPLETED.value,
            notes="Nice",
            start_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
            end_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
        )

        self.client.post(
            reverse("media_save"),
            {
                "instance_id": movie.id,
                "media_id": "10494",
                "source": Sources.TMDB.value,
                "media_type": MediaTypes.MOVIE.value,
                "score": 10,
                "progress": 1,
                "status": Status.COMPLETED.value,
                "notes": "Nice",
            },
        )
        self.assertEqual(Movie.objects.get(item__media_id="10494").score, 10)


class DeleteMedia(TestCase):
    """Test the deletion of media objects through views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item_season = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        self.season = Season.objects.create(
            item=self.item_season,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        self.item_ep = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
            episode_number=1,
        )
        self.episode = Episode.objects.create(
            item=self.item_ep,
            related_season=self.season,
            end_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
        )

    def test_delete_tv(self):
        """Test the deletion of a tv through views."""
        self.assertEqual(TV.objects.filter(user=self.user).count(), 1)
        tv_obj = TV.objects.get(user=self.user)

        self.client.post(
            reverse("media_delete"),
            data={
                "instance_id": tv_obj.id,
                "media_type": MediaTypes.TV.value,
            },
        )

        self.assertEqual(Movie.objects.filter(user=self.user).count(), 0)

    def test_delete_season(self):
        """Test the deletion of a season through views."""
        self.client.post(
            reverse(
                "media_delete",
            ),
            data={"instance_id": self.season.id, "media_type": MediaTypes.SEASON.value},
        )

        self.assertEqual(Season.objects.filter(user=self.user).count(), 0)
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            0,
        )

    def test_unwatch_episode(self):
        """Test unwatching of an episode through views."""
        self.client.post(
            reverse("media_delete"),
            data={
                "instance_id": self.episode.id,
                "media_type": MediaTypes.EPISODE.value,
            },
        )

        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            0,
        )


class ProgressEditSeason(TestCase):
    """Test for editing a season progress through views."""

    def setUp(self):
        """Prepare the database with a season and an episode."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item_season = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        self.season = Season.objects.create(
            item=self.item_season,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        item_ep = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Friends",
            image="http://example.com/image.jpg",
            season_number=1,
            episode_number=1,
        )
        Episode.objects.create(
            item=item_ep,
            related_season=self.season,
            end_date=datetime.datetime(2023, 6, 1, 0, 0, tzinfo=datetime.UTC),
        )

    def test_progress_increase(self):
        """Test the increase of progress for a season."""
        self.client.post(
            reverse(
                "progress_edit",
                kwargs={
                    "media_type": MediaTypes.SEASON.value,
                    "instance_id": self.season.id,
                },
            ),
            {
                "operation": "increase",
            },
        )

        self.assertEqual(
            Episode.objects.filter(item__media_id="1668").count(),
            2,
        )

        # episode with media_id "1668" and episode_number 2 should exist
        self.assertTrue(
            Episode.objects.filter(
                item__media_id="1668",
                item__episode_number=2,
            ).exists(),
        )

    def test_progress_decrease(self):
        """Test the decrease of progress for a season."""
        self.client.post(
            reverse(
                "progress_edit",
                kwargs={
                    "media_type": MediaTypes.SEASON.value,
                    "instance_id": self.season.id,
                },
            ),
            {
                "operation": "decrease",
            },
        )

        self.assertEqual(
            Episode.objects.filter(item__media_id="1668").count(),
            0,
        )


class ProgressEditAnime(TestCase):
    """Test for editing an anime progress through views."""

    def setUp(self):
        """Prepare the database with an anime."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        self.item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Cowboy Bebop",
            image="http://example.com/image.jpg",
        )
        self.anime = Anime.objects.create(
            item=self.item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=2,
        )

    def test_progress_increase(self):
        """Test the increase of progress for an anime."""
        self.client.post(
            reverse(
                "progress_edit",
                kwargs={
                    "media_type": MediaTypes.ANIME.value,
                    "instance_id": self.anime.id,
                },
            ),
            {
                "operation": "increase",
            },
        )

        self.assertEqual(Anime.objects.get(item__media_id="1").progress, 3)

    def test_progress_decrease(self):
        """Test the decrease of progress for an anime."""
        self.client.post(
            reverse(
                "progress_edit",
                kwargs={
                    "media_type": MediaTypes.ANIME.value,
                    "instance_id": self.anime.id,
                },
            ),
            {
                "operation": "decrease",
            },
        )

        self.assertEqual(Anime.objects.get(item__media_id="1").progress, 1)


class CreateEntryViewTests(TestCase):
    """Test the create entry view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_create_entry_get(self):
        """Test the GET method of create_entry view."""
        response = self.client.get(reverse("create_entry"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/create_entry.html")
        self.assertIn("media_types", response.context)

        # Verify media_types contains expected values
        self.assertEqual(response.context["media_types"], MediaTypes.values)

    def test_create_entry_post_movie(self):
        """Test creating a movie entry."""
        form_data = {
            "title": "Test Movie",
            "media_type": MediaTypes.MOVIE.value,
            "status": Status.COMPLETED.value,
            "score": 8,
            "progress": 1,
            "start_date": "2023-01-01T00:00",
            "end_date": "2023-01-02T00:00",
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        # Check redirect
        self.assertRedirects(response, reverse("create_entry"))

        # Verify item was created
        self.assertTrue(
            Item.objects.filter(
                title="Test Movie",
                media_type=MediaTypes.MOVIE.value,
            ).exists(),
        )

        # Verify media was created
        movie = Movie.objects.get(item__title="Test Movie")
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.score, 8)
        self.assertEqual(movie.progress, 1)
        self.assertEqual(movie.user, self.user)

    def test_create_entry_post_tv(self):
        """Test creating a TV show entry."""
        form_data = {
            "title": "Test TV Show",
            "media_type": MediaTypes.TV.value,
            "status": Status.IN_PROGRESS.value,
            "score": 7,
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        # Check redirect
        self.assertRedirects(response, reverse("create_entry"))

        # Verify item was created
        self.assertTrue(
            Item.objects.filter(
                title="Test TV Show",
                media_type=MediaTypes.TV.value,
            ).exists(),
        )

        # Verify media was created
        tv = TV.objects.get(item__title="Test TV Show")
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        self.assertEqual(tv.score, 7)
        self.assertEqual(tv.user, self.user)

    def test_create_entry_post_season(self):
        """Test creating a season entry with parent TV."""
        # First create a parent TV show
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="TV Show",
        )
        parent_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        form_data = {
            "title": "TV Show",
            "media_type": MediaTypes.SEASON.value,
            "season_number": 1,
            "parent_tv": parent_tv.id,
            "status": Status.IN_PROGRESS.value,
            "score": 7,
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        # Check redirect
        self.assertRedirects(response, reverse("create_entry"))

        # Verify item was created
        self.assertTrue(
            Item.objects.filter(
                title="TV Show",
                media_type=MediaTypes.SEASON.value,
                season_number=1,
            ).exists(),
        )

        # Verify media was created with correct relationship
        season = Season.objects.get(item__title="TV Show")
        self.assertEqual(season.status, Status.IN_PROGRESS.value)
        self.assertEqual(season.score, 7)
        self.assertEqual(season.user, self.user)
        self.assertEqual(season.related_tv, parent_tv)

    def test_create_entry_post_episode(self):
        """Test creating an episode entry with parent season."""
        # First create a parent TV show and season
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="TV Show",
        )
        parent_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        season_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="TV Show",
            season_number=1,
        )
        parent_season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=parent_tv,
            status=Status.IN_PROGRESS.value,
        )

        form_data = {
            "title": "TV Show",
            "media_type": MediaTypes.EPISODE.value,
            "season_number": 1,
            "episode_number": 1,
            "parent_season": parent_season.id,
            "end_date": "2023-01-02T00:00",
        }

        response = self.client.post(reverse("create_entry"), form_data, follow=True)

        # Check redirect
        self.assertRedirects(response, reverse("create_entry"))

        # Verify item was created
        self.assertTrue(
            Item.objects.filter(
                title="TV Show",
                media_type=MediaTypes.EPISODE.value,
                season_number=1,
                episode_number=1,
            ).exists(),
        )

        # Verify media was created with correct relationship
        episode = Episode.objects.get(item__title="TV Show")
        self.assertEqual(episode.related_season, parent_season)
        end_date_local = timezone.localtime(episode.end_date)
        self.assertEqual(end_date_local.strftime("%Y-%m-%d %H:%M"), "2023-01-02 00:00")

    def test_create_entry_post_duplicate_item(self):
        """Test creating a duplicate item."""
        # First create an item
        tv_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="TV Show",
        )
        parent_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        season_item = Item.objects.create(
            media_id="1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="TV Show",
            season_number=1,
        )
        Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=parent_tv,
            status=Status.IN_PROGRESS.value,
        )

        # Count items before the test
        initial_count = Item.objects.count()

        # Try to create the same season again
        form_data = {
            "title": "TV Show",
            "media_type": MediaTypes.SEASON.value,
            "season_number": 1,
            "parent_tv": parent_tv.id,
            "status": Status.IN_PROGRESS.value,
            "score": 7,
            "repeats": 0,
        }

        with transaction.atomic():
            self.client.post(reverse("create_entry"), form_data)

        # No new item should be created
        self.assertEqual(Item.objects.count(), initial_count)


class SearchParentViewTests(TestCase):
    """Test the parent search views."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        # Create some manual TV shows and seasons for testing
        tv_item1 = Item.objects.create(
            media_id="111",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Test TV Show",
        )
        self.tv1 = TV.objects.create(
            item=tv_item1,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        tv_item2 = Item.objects.create(
            media_id="222",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.TV.value,
            title="Another TV Show",
        )
        self.tv2 = TV.objects.create(
            item=tv_item2,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        season_item1 = Item.objects.create(
            media_id="111",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Test Season",
            season_number=1,
        )
        self.season1 = Season.objects.create(
            item=season_item1,
            user=self.user,
            related_tv=self.tv1,
            status=Status.IN_PROGRESS.value,
        )

        season_item2 = Item.objects.create(
            media_id="222",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.SEASON.value,
            title="Another Season",
            season_number=1,
        )
        self.season2 = Season.objects.create(
            item=season_item2,
            user=self.user,
            related_tv=self.tv2,
            status=Status.IN_PROGRESS.value,
        )

    def test_search_parent_tv_short_query(self):
        """Test search_parent_tv with a query that's too short."""
        response = self.client.get(reverse("search_parent_tv") + "?q=T")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_tv.html")
        self.assertNotIn("results", response.context)

    def test_search_parent_tv_valid_query(self):
        """Test search_parent_tv with a valid query."""
        response = self.client.get(reverse("search_parent_tv") + "?q=Test")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_tv.html")
        self.assertIn("results", response.context)
        self.assertIn("query", response.context)

        # Should find the "Test TV Show"
        self.assertEqual(len(response.context["results"]), 1)
        self.assertEqual(response.context["results"][0], self.tv1)
        self.assertEqual(response.context["query"], "Test")

    def test_search_parent_tv_no_results(self):
        """Test search_parent_tv with a query that returns no results."""
        response = self.client.get(reverse("search_parent_tv") + "?q=NonExistent")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_tv.html")
        self.assertIn("results", response.context)

        # Should find no results
        self.assertEqual(len(response.context["results"]), 0)

    def test_search_parent_season_short_query(self):
        """Test search_parent_season with a query that's too short."""
        response = self.client.get(reverse("search_parent_season") + "?q=T")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_tv.html")
        self.assertNotIn("results", response.context)

    def test_search_parent_season_valid_query(self):
        """Test search_parent_season with a valid query."""
        response = self.client.get(reverse("search_parent_season") + "?q=Test")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_season.html")
        self.assertIn("results", response.context)
        self.assertIn("query", response.context)

        # Should find the "Test Season"
        self.assertEqual(len(response.context["results"]), 1)
        self.assertEqual(response.context["results"][0], self.season1)
        self.assertEqual(response.context["query"], "Test")

    def test_search_parent_season_no_results(self):
        """Test search_parent_season with a query that returns no results."""
        response = self.client.get(reverse("search_parent_season") + "?q=NonExistent")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/search_parent_season.html")
        self.assertIn("results", response.context)

        # Should find no results
        self.assertEqual(len(response.context["results"]), 0)
