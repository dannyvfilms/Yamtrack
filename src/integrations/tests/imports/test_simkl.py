from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from app.models import (
    TV,
    Anime,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from integrations import tasks
from integrations.imports import (
    helpers,
    simkl,
)

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


class ImportSimkl(TestCase):
    """Test importing media from SIMKL."""

    def setUp(self):
        """Create user for the tests."""
        credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**credentials)
        self.importer = simkl.SimklImporter(
            helpers.encrypt("token"),
            self.user,
            "new",
        )

    @patch("integrations.imports.simkl.SimklImporter._get_user_list")
    def test_importer(
        self,
        user_list,
    ):
        """Test importing media from SIMKL."""
        user_list.return_value = {
            "shows": [
                {
                    "last_watched_at": "2023-01-02T00:00:00Z",
                    "show": {"title": "Breaking Bad", "ids": {"tmdb": 1396}},
                    "status": "watching",
                    "user_rating": 8,
                    "seasons": [
                        {
                            "number": 1,
                            "episodes": [
                                {"number": 1},
                                {"number": 2, "watched_at": "2023-01-02T00:00:00Z"},
                            ],
                        },
                    ],
                    "memo": {},
                },
            ],
            "movies": [
                {
                    "added_to_watchlist_at": "2023-01-01T00:00:00Z",
                    "movie": {"title": "Perfect Blue", "ids": {"tmdb": 10494}},
                    "status": "completed",
                    "user_rating": 9,
                    "last_watched_at": "2023-02-01T00:00:00Z",
                    "memo": {},
                },
            ],
            "anime": [
                {
                    "added_to_watchlist_at": "2023-01-01T00:00:00Z",
                    "show": {"title": "Example Anime", "ids": {"mal": 1}},
                    "status": "plantowatch",
                    "user_rating": 7,
                    "watched_episodes_count": 0,
                    "last_watched_at": None,
                    "memo": {"text": "Great series!"},
                },
            ],
        }

        imported_counts, warnings = self.importer.import_data()

        self.assertEqual(imported_counts[MediaTypes.TV.value], 1)
        self.assertEqual(imported_counts[MediaTypes.MOVIE.value], 1)
        self.assertEqual(imported_counts[MediaTypes.ANIME.value], 1)
        self.assertEqual(warnings, "")

        tv_item = Item.objects.get(media_type=MediaTypes.TV.value)
        self.assertEqual(tv_item.title, "Breaking Bad")
        tv_obj = TV.objects.get(item=tv_item)
        self.assertEqual(tv_obj.status, Status.IN_PROGRESS.value)
        self.assertEqual(tv_obj.score, 8)

        movie_item = Item.objects.get(media_type=MediaTypes.MOVIE.value)
        self.assertEqual(movie_item.title, "Perfect Blue")
        movie_obj = Movie.objects.get(item=movie_item)
        self.assertEqual(movie_obj.status, Status.COMPLETED.value)
        self.assertEqual(movie_obj.score, 9)
        self.assertEqual(movie_obj.progress, 1)

        anime_item = Item.objects.get(media_type=MediaTypes.ANIME.value)
        self.assertEqual(anime_item.title, "Cowboy Bebop")
        anime_obj = Anime.objects.get(item=anime_item)
        self.assertEqual(anime_obj.status, Status.PLANNING.value)
        self.assertEqual(anime_obj.score, 7)
        self.assertEqual(anime_obj.notes, "Great series!")

    def test_get_status(self):
        """Test mapping SIMKL status to internal status."""
        self.assertEqual(self.importer._get_status("completed"), Status.COMPLETED.value)
        self.assertEqual(
            self.importer._get_status("watching"),
            Status.IN_PROGRESS.value,
        )
        self.assertEqual(
            self.importer._get_status("plantowatch"),
            Status.PLANNING.value,
        )
        self.assertEqual(self.importer._get_status("hold"), Status.PAUSED.value)
        self.assertEqual(self.importer._get_status("dropped"), Status.DROPPED.value)
        self.assertEqual(
            self.importer._get_status("unknown"),
            Status.IN_PROGRESS.value,
        )  # Default case

    def test_get_date(self):
        """Test getting date from SIMKL."""
        self.assertEqual(
            self.importer._get_date("2023-01-01T00:00:00Z"),
            datetime(2023, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        self.assertIsNone(self.importer._get_date(None))

    @patch("integrations.imports.simkl.SimklImporter._get_user_list")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_season_status_logic_with_completed_seasons(
        self,
        mock_tv_with_seasons,
        mock_user_list,
    ):
        """Test that seasons are marked as completed when all episodes are watched."""
        mock_tv_with_seasons.return_value = {
            "title": "Breaking Bad",
            "image": "https://image.tmdb.org/t/p/w500/test.jpg",
            "season/1": {
                "image": "https://image.tmdb.org/t/p/w500/season1.jpg",
                "max_progress": 7,
                "episodes": [
                    {"episode_number": 1, "still_path": "/ep1.jpg"},
                    {"episode_number": 2, "still_path": "/ep2.jpg"},
                    {"episode_number": 3, "still_path": "/ep3.jpg"},
                    {"episode_number": 4, "still_path": "/ep4.jpg"},
                    {"episode_number": 5, "still_path": "/ep5.jpg"},
                    {"episode_number": 6, "still_path": "/ep6.jpg"},
                    {"episode_number": 7, "still_path": "/ep7.jpg"},
                ],
            },
            "season/2": {
                "image": "https://image.tmdb.org/t/p/w500/season2.jpg",
                "max_progress": 13,
            },
        }

        mock_user_list.return_value = {
            "shows": [
                {
                    "last_watched_at": "2023-01-15T00:00:00Z",
                    "show": {"title": "Breaking Bad", "ids": {"tmdb": 1396}},
                    "status": "watching",  # TV show is still in progress
                    "user_rating": 9,
                    "seasons": [
                        {
                            "number": 1,
                            "episodes": [
                                {"number": 1, "watched_at": "2023-01-01T00:00:00Z"},
                                {"number": 2, "watched_at": "2023-01-02T00:00:00Z"},
                                {"number": 3, "watched_at": "2023-01-03T00:00:00Z"},
                                {"number": 4, "watched_at": "2023-01-04T00:00:00Z"},
                                {"number": 5, "watched_at": "2023-01-05T00:00:00Z"},
                                {"number": 6, "watched_at": "2023-01-06T00:00:00Z"},
                                {"number": 7, "watched_at": "2023-01-07T00:00:00Z"},
                            ],
                        },
                    ],
                    "memo": {},
                },
            ],
            "movies": [],
            "anime": [],
        }

        imported_counts, _ = self.importer.import_data()

        self.assertEqual(imported_counts[MediaTypes.TV.value], 1)
        self.assertEqual(imported_counts[MediaTypes.SEASON.value], 1)
        self.assertEqual(
            imported_counts[MediaTypes.EPISODE.value],
            7,
        )

        tv_item = Item.objects.get(media_type=MediaTypes.TV.value)
        tv_obj = TV.objects.get(item=tv_item)
        self.assertEqual(tv_obj.status, Status.IN_PROGRESS.value)

        season1_item = Item.objects.get(
            media_type=MediaTypes.SEASON.value,
            season_number=1,
        )
        season1_obj = Season.objects.get(item=season1_item)
        self.assertEqual(
            season1_obj.status,
            Status.COMPLETED.value,
            "Season 1 should be completed when all episodes are watched",
        )

        season1_episodes = Episode.objects.filter(
            item__season_number=1,
            item__media_type=MediaTypes.EPISODE.value,
        )
        self.assertEqual(season1_episodes.count(), 7)

        for episode in season1_episodes:
            self.assertIsNotNone(episode.end_date)

    @patch("integrations.imports.simkl.SimklImporter._get_user_list")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_importer_skips_missing_tmdb_season_metadata_instead_of_crashing(
        self,
        mock_tv_with_seasons,
        mock_user_list,
    ):
        """Importer should continue when SIMKL seasons are absent from TMDB metadata."""
        mock_tv_with_seasons.return_value = {
            "title": "Breaking Bad",
            "image": "https://image.tmdb.org/t/p/w500/test.jpg",
            # season/6 intentionally missing
        }
        mock_user_list.return_value = {
            "shows": [
                {
                    "last_watched_at": "2023-01-02T00:00:00Z",
                    "show": {"title": "Breaking Bad", "ids": {"tmdb": 1396}},
                    "status": "watching",
                    "user_rating": 8,
                    "seasons": [
                        {
                            "number": 6,
                            "episodes": [
                                {"number": 1, "watched_at": "2023-01-02T00:00:00Z"},
                            ],
                        },
                    ],
                    "memo": {},
                },
            ],
            "movies": [],
            "anime": [],
        }

        imported_counts, warnings = self.importer.import_data()

        self.assertEqual(imported_counts[MediaTypes.TV.value], 1)
        self.assertNotIn(MediaTypes.SEASON.value, imported_counts)
        self.assertNotIn(MediaTypes.EPISODE.value, imported_counts)
        self.assertIn(
            f"missing {Sources.TMDB.label} metadata for season 6",
            warnings,
        )

    @override_settings(TVDB_API_KEY="test-tvdb-key")
    @patch("integrations.imports.simkl.SimklImporter._get_user_list")
    @patch("app.providers.tvdb.tv_with_seasons")
    @patch("app.providers.tmdb.tv_with_seasons")
    def test_importer_switches_show_to_tvdb_when_tmdb_seasons_are_incomplete(
        self,
        mock_tmdb_tv_with_seasons,
        mock_tvdb_tv_with_seasons,
        mock_user_list,
    ):
        """Importer should switch a show to TVDB when TMDB lacks requested seasons."""
        mock_tmdb_tv_with_seasons.return_value = {
            "title": "Jeopardy!",
            "image": "https://image.tmdb.org/t/p/w500/test.jpg",
            "tvdb_id": "76703",
        }
        mock_tvdb_tv_with_seasons.return_value = {
            "title": "Jeopardy!",
            "image": "https://example.com/jeopardy.jpg",
            "season/2019": {
                "image": "https://example.com/season-2019.jpg",
                "max_progress": 1,
                "episodes": [
                    {
                        "episode_number": 1,
                        "image": "https://example.com/episode-2019-1.jpg",
                    },
                ],
            },
        }
        mock_user_list.return_value = {
            "shows": [
                {
                    "last_watched_at": "2023-01-02T00:00:00Z",
                    "show": {
                        "title": "Jeopardy!",
                        "ids": {"tmdb": 1975, "tvdb": 76703},
                    },
                    "status": "watching",
                    "user_rating": 8,
                    "seasons": [
                        {
                            "number": 2019,
                            "episodes": [
                                {"number": 1, "watched_at": "2023-01-02T00:00:00Z"},
                            ],
                        },
                    ],
                    "memo": {},
                },
            ],
            "movies": [],
            "anime": [],
        }

        imported_counts, warnings = self.importer.import_data()

        self.assertEqual(imported_counts[MediaTypes.TV.value], 1)
        self.assertEqual(imported_counts[MediaTypes.SEASON.value], 1)
        self.assertEqual(imported_counts[MediaTypes.EPISODE.value], 1)
        self.assertIn("imported via TheTVDB", warnings)
        self.assertNotIn("missing TMDB metadata for season 2019", warnings)

        tv_item = Item.objects.get(media_type=MediaTypes.TV.value)
        season_item = Item.objects.get(media_type=MediaTypes.SEASON.value)
        episode_item = Item.objects.get(media_type=MediaTypes.EPISODE.value)

        self.assertEqual(tv_item.source, Sources.TVDB.value)
        self.assertEqual(tv_item.media_id, "76703")
        self.assertEqual(season_item.source, Sources.TVDB.value)
        self.assertEqual(episode_item.source, Sources.TVDB.value)
        self.assertEqual(
            episode_item.image,
            "https://example.com/episode-2019-1.jpg",
        )

    @patch("integrations.imports.simkl.SimklImporter._get_user_list")
    def test_history_month_view_updates_after_simkl_import_without_manual_refresh(
        self,
        mock_user_list,
    ):
        """SIMKL imports should appear on the month history page right away."""
        self.client.force_login(self.user)
        now = timezone.localtime()
        watch_dt = now.replace(day=2, hour=12, minute=0, second=0, microsecond=0)
        watched_at = watch_dt.astimezone(UTC).isoformat().replace(
            "+00:00",
            "Z",
        )

        # Prime an empty month cache.
        initial_response = self.client.get(
            reverse("history"),
            {"y": now.year, "m": now.month},
        )
        self.assertContains(initial_response, "No watch history yet")

        mock_user_list.return_value = {
            "shows": [],
            "movies": [
                {
                    "added_to_watchlist_at": watched_at,
                    "movie": {"title": "Perfect Blue", "ids": {"tmdb": 10494}},
                    "status": "completed",
                    "user_rating": 9,
                    "last_watched_at": watched_at,
                    "memo": {},
                },
            ],
            "anime": [],
        }

        tasks.import_media(
            simkl.importer,
            helpers.encrypt("token"),
            self.user.id,
            "new",
        )

        response = self.client.get(reverse("history"), {"y": now.year, "m": now.month})
        self.assertContains(response, "Perfect Blue")
