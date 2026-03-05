from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app import live_playback
from app.models import (
    Anime,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
)
from users.models import HomeSortChoices


class HomeViewTests(TestCase):
    """Test the home view."""

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
        """Test the home view displays in-progress media."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/home.html")

        self.assertIn("list_by_type", response.context)
        self.assertIn(MediaTypes.SEASON.value, response.context["list_by_type"])
        self.assertIn(MediaTypes.ANIME.value, response.context["list_by_type"])

        self.assertIn("sort_choices", response.context)
        self.assertEqual(response.context["sort_choices"], HomeSortChoices.choices)

        season = response.context["list_by_type"][MediaTypes.SEASON.value]
        self.assertEqual(len(season["items"]), 1)
        self.assertEqual(season["items"][0].progress, 5)

    def test_home_view_includes_seasons_when_tv_enabled_and_season_disabled(self):
        """Home should still include TV seasons when TV is enabled."""
        self.user.tv_enabled = True
        self.user.season_enabled = False
        self.user.save(update_fields=["tv_enabled", "season_enabled"])

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertIn(MediaTypes.SEASON.value, response.context["list_by_type"])
        season = response.context["list_by_type"][MediaTypes.SEASON.value]
        self.assertEqual(len(season["items"]), 1)
        self.assertEqual(season["items"][0].progress, 5)

    def test_home_view_with_sort(self):
        """Test the home view with sorting parameter."""
        response = self.client.get(reverse("home") + "?sort=completion")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "completion")

        self.user.refresh_from_db()
        self.assertEqual(self.user.home_sort, "completion")

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
        self.assertContains(response, "data-active-playback-card")

    @patch("app.providers.services.get_media_metadata")
    def test_home_view_htmx_load_more(self, mock_get_media_metadata):
        """Test the HTMX load more functionality."""
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

        # Now test the load more functionality
        response = self.client.get(
            reverse("home") + "?load_media_type=season", headers={"hx-request": "true"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/home_grid.html")

        self.assertIn("media_list", response.context)

        self.assertIn("items", response.context["media_list"])
        self.assertIn("total", response.context["media_list"])

        # Since we're loading more (items after the first 14),
        # we should have at least 1 item in the response
        self.assertEqual(len(response.context["media_list"]["items"]), 1)
        self.assertEqual(
            response.context["media_list"]["total"],
            15,
        )  # 15 TV shows total

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
        self.assertContains(response, "data-active-playback-card")
        self.assertContains(response, "Test TV Show")
        self.assertContains(response, "S01E02")
