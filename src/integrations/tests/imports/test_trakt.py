from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from integrations.imports import helpers
from integrations.imports.trakt import TraktImporter, importer

mock_path = Path(__file__).resolve().parent.parent / "mock_data"
app_mock_path = (
    Path(__file__).resolve().parent.parent.parent.parent / "app" / "tests" / "mock_data"
)


class ImportTrakt(TestCase):
    """Test importing media from Trakt."""

    def setUp(self):
        """Create user for the tests."""
        credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**credentials)

    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_process_watched_movie(self, mock_get_metadata):
        """Test processing a movie entry."""
        movie_entry = {
            "type": "movie",
            "movie": {"title": "Test Movie", "ids": {"tmdb": 67890}},
            "watched_at": "2023-01-02T00:00:00.000Z",
        }

        mock_get_metadata.return_value = {
            "title": "Test Movie",
            "image": "movie_image.jpg",
        }

        trakt_importer = TraktImporter("test", self.user, "new")
        trakt_importer.process_watched_movie(movie_entry)

        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.MOVIE.value]), 1)
        self.assertEqual(len(trakt_importer.media_instances[MediaTypes.MOVIE.value]), 1)

        # Verify progress is set to 1 for completed movies
        movie_obj = trakt_importer.bulk_media[MediaTypes.MOVIE.value][0]
        self.assertEqual(movie_obj.progress, 1)

        # Process the same movie again to test repeat handling
        trakt_importer.process_watched_movie(movie_entry)
        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.MOVIE.value]), 2)

    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_process_watched_episode(self, mock_get_metadata):
        """Test processing an episode entry."""
        episode_entry = {
            "type": "episode",
            "episode": {"season": 1, "number": 1, "title": "Pilot"},
            "show": {"title": "Test Show", "ids": {"tmdb": 12345}},
            "watched_at": "2023-01-01T00:00:00.000Z",
        }

        def mock_metadata_side_effect(media_type, _, __, ___=None):
            if media_type == MediaTypes.TV.value:
                return {
                    "title": "Test Show",
                    "image": "tv_image.jpg",
                    "last_episode_season": 1,
                    "max_progress": 1,
                }
            if media_type == MediaTypes.SEASON.value:
                return {
                    "title": "Season 1",
                    "image": "season_image.jpg",
                    "episodes": [{"episode_number": 1, "still_path": "/still.jpg"}],
                    "max_progress": 1,
                }
            return None

        mock_get_metadata.side_effect = mock_metadata_side_effect

        trakt_importer = TraktImporter("testuser", self.user, "new")
        trakt_importer.process_watched_episode(episode_entry)

        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.TV.value]), 1)
        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.SEASON.value]), 1)
        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.EPISODE.value]), 1)

        # Process a replay of the same episode at a different time.
        trakt_importer.process_watched_episode(
            {
                **episode_entry,
                "watched_at": "2023-01-02T00:00:00.000Z",
            },
        )
        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.EPISODE.value]), 2)

    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_process_watched_episode_existing_show_imports_new_episode(
        self,
        mock_get_metadata,
    ):
        """New-mode import should add episodes even when the show already exists."""
        tv_item = Item.objects.get_or_create(
            media_id="12345",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            defaults={"title": "Test Show"},
        )[0]
        tv_obj = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.get_or_create(
            media_id="12345",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={"title": "Season 1"},
        )[0]
        season_obj = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv_obj,
            status=Status.IN_PROGRESS.value,
        )

        episode_entry = {
            "type": "episode",
            "episode": {"season": 1, "number": 2, "title": "Episode 2"},
            "show": {"title": "Test Show", "ids": {"tmdb": 12345}},
            "watched_at": "2023-01-02T00:00:00.000Z",
        }

        def mock_metadata_side_effect(media_type, _, __, ___=None):
            if media_type == MediaTypes.TV.value:
                return {
                    "title": "Test Show",
                    "image": "tv_image.jpg",
                    "last_episode_season": 1,
                    "max_progress": 2,
                }
            if media_type == MediaTypes.SEASON.value:
                return {
                    "title": "Season 1",
                    "image": "season_image.jpg",
                    "episodes": [
                        {"episode_number": 1, "still_path": "/still1.jpg"},
                        {"episode_number": 2, "still_path": "/still2.jpg"},
                    ],
                    "max_progress": 2,
                }
            return None

        mock_get_metadata.side_effect = mock_metadata_side_effect

        trakt_importer = TraktImporter("testuser", self.user, "new")
        trakt_importer.process_watched_episode(episode_entry)

        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.TV.value]), 0)
        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.SEASON.value]), 0)
        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.EPISODE.value]), 1)
        self.assertEqual(
            trakt_importer.bulk_media[MediaTypes.EPISODE.value][0].related_season_id,
            season_obj.id,
        )

    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_process_watchlist(self, mock_get_metadata, mock_make_request):
        """Test processing a watchlist entry."""
        watchlist_entry = {
            "listed_at": "2023-01-01T00:00:00.000Z",
            "type": "show",
            "show": {"title": "Watchlist Show", "ids": {"tmdb": 54321}},
        }

        mock_make_request.return_value = [watchlist_entry]
        mock_get_metadata.return_value = {
            "title": "Watchlist Show",
            "image": "show_image.jpg",
        }

        trakt_importer = TraktImporter("testuser", self.user, "new")
        trakt_importer.process_watchlist()

        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.TV.value]), 1)
        tv_obj = trakt_importer.bulk_media[MediaTypes.TV.value][0]
        self.assertEqual(tv_obj.status, Status.PLANNING.value)

    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_process_ratings(self, mock_get_metadata, mock_make_request):
        """Test processing a rating entry."""
        rating_entry = {
            "rated_at": "2023-01-01T00:00:00.000Z",
            "type": "movie",
            "movie": {"title": "Rated Movie", "ids": {"tmdb": 238}},
            "rating": 8,
        }

        mock_make_request.return_value = [rating_entry]
        mock_get_metadata.return_value = {
            "title": "Rated Movie",
            "image": "movie_image.jpg",
        }

        trakt_importer = TraktImporter("testuser", self.user, "new")
        trakt_importer.process_ratings()

        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.MOVIE.value]), 1)
        movie_obj = trakt_importer.bulk_media[MediaTypes.MOVIE.value][0]
        self.assertEqual(movie_obj.score, 8)

    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_process_comments(self, mock_get_metadata, mock_make_request):
        """Test processing paginated comments from Trakt."""
        # First page with one comment
        first_page = [
            {
                "type": "movie",
                "movie": {"title": "Commented Movie", "ids": {"tmdb": 123}},
                "comment": {
                    "comment": "Great movie!",
                    "updated_at": "2023-01-01T00:00:00.000Z",
                },
            },
        ]

        # Second empty page to stop pagination
        second_page = []

        mock_make_request.side_effect = [first_page, second_page]
        mock_get_metadata.return_value = {
            "title": "Commented Movie",
            "image": "movie_image.jpg",
        }

        trakt_importer = TraktImporter("testuser", self.user, "new")
        trakt_importer.process_comments()

        calls = mock_make_request.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("?page=1&limit=1000", calls[0].args[0])  # First page
        self.assertIn("?page=2&limit=1000", calls[1].args[0])  # Second page

        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.MOVIE.value]), 1)
        movie_obj = trakt_importer.bulk_media[MediaTypes.MOVIE.value][0]
        self.assertEqual(movie_obj.notes, "Great movie!")

    @patch("integrations.imports.trakt.TraktImporter._get_paginated_data")
    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_public_import_full_flow(
        self,
        mock_get_metadata,
        mock_make_request,
        mock_get_paginated,
    ):
        """Test full import flow with public username (no OAuth)."""
        mock_get_paginated.side_effect = [
            [
                {
                    "type": "movie",
                    "movie": {"title": "Public Movie", "ids": {"tmdb": 999}},
                    "watched_at": "2023-01-01T00:00:00.000Z",
                },
            ],
            [],  # Empty comments
        ]

        mock_make_request.return_value = []

        mock_get_metadata.return_value = {
            "title": "Public Movie",
            "image": "movie.jpg",
        }

        imported_counts, _ = importer(None, self.user, "new", "public_user")

        self.assertEqual(imported_counts[MediaTypes.MOVIE.value], 1)
        self.assertEqual(Movie.objects.filter(user=self.user).count(), 1)

    @patch("integrations.imports.trakt.TraktImporter._get_paginated_data")
    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_oauth_import_full_flow(
        self,
        mock_get_metadata,
        mock_make_request,
        mock_get_paginated,
    ):
        """Test full import flow with OAuth token."""
        mock_get_paginated.side_effect = [
            [],  # process_dropped — no dropped shows
            [
                {
                    "type": "movie",
                    "movie": {"title": "OAuth Movie", "ids": {"tmdb": 888}},
                    "watched_at": "2023-01-01T00:00:00.000Z",
                },
            ],
            [],  # Empty comments
        ]

        mock_make_request.return_value = []

        mock_get_metadata.return_value = {
            "title": "OAuth Movie",
            "image": "movie.jpg",
        }

        encrypted_token = helpers.encrypt("test_refresh_token")
        imported_counts, _ = importer(
            encrypted_token,
            self.user,
            "new",
            "oauth_user",
        )

        self.assertEqual(imported_counts[MediaTypes.MOVIE.value], 1)
        self.assertEqual(Movie.objects.filter(user=self.user).count(), 1)

    def test_trakt_importer_with_refresh_token(self):
        """Test TraktImporter initialization with refresh token."""
        encrypted_token = helpers.encrypt("test_token")
        importer = TraktImporter(
            "testuser",
            self.user,
            "new",
            refresh_token=encrypted_token,
        )

        self.assertEqual(importer.username, "testuser")
        self.assertEqual(importer.refresh_token, encrypted_token)
        self.assertEqual(importer.mode, "new")

    def test_trakt_importer_without_refresh_token(self):
        """Test TraktImporter initialization without refresh token (public)."""
        importer = TraktImporter("testuser", self.user, "new", refresh_token=None)

        self.assertEqual(importer.username, "testuser")
        self.assertIsNone(importer.refresh_token)
        self.assertEqual(importer.mode, "new")

    @patch("integrations.imports.trakt.TraktImporter._get_paginated_data")
    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_reimport_does_not_duplicate_episode_history(
        self,
        mock_get_metadata,
        mock_make_request,
        mock_get_paginated,
    ):
        """Running the same Trakt sync twice should not create duplicate episodes."""
        episode_entry = {
            "type": "episode",
            "episode": {"season": 1, "number": 1, "title": "Pilot"},
            "show": {"title": "Repeat Show", "ids": {"tmdb": 12345}},
            "watched_at": "2023-01-01T00:00:00.000Z",
        }

        def mock_metadata_side_effect(media_type, _, __, ___=None):
            if media_type == MediaTypes.TV.value:
                return {
                    "title": "Repeat Show",
                    "image": "tv_image.jpg",
                    "last_episode_season": 1,
                    "max_progress": 1,
                }
            if media_type == MediaTypes.SEASON.value:
                return {
                    "title": "Season 1",
                    "image": "season_image.jpg",
                    "episodes": [{"episode_number": 1, "still_path": "/still.jpg"}],
                    "max_progress": 1,
                }
            return None

        mock_get_metadata.side_effect = mock_metadata_side_effect
        mock_get_paginated.side_effect = [
            [episode_entry],
            [],
            [episode_entry],
            [],
        ]
        mock_make_request.return_value = []

        first_counts, _ = importer(None, self.user, "new", "public_user")
        second_counts, _ = importer(None, self.user, "new", "public_user")

        self.assertEqual(first_counts[MediaTypes.EPISODE.value], 1)
        self.assertEqual(second_counts.get(MediaTypes.EPISODE.value, 0), 0)
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            1,
        )

    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    def test_process_episode_rating(self, mock_make_request):
        """Episode ratings from Trakt are applied to existing Episode records."""
        # Build the minimum DB state: TV → Season → Episode
        tv_item = Item.objects.get_or_create(
            media_id="12345",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            defaults={"title": "Test Show"},
        )[0]
        tv_obj = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        season_item = Item.objects.get_or_create(
            media_id="12345",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={"title": "Season 1"},
        )[0]
        season_obj = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=tv_obj,
            status=Status.IN_PROGRESS.value,
        )
        episode_item = Item.objects.get_or_create(
            media_id="12345",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=1,
            defaults={"title": "Pilot"},
        )[0]
        episode_obj = Episode.objects.create(
            item=episode_item,
            related_season=season_obj,
        )

        rating_entry = {
            "rated_at": "2023-01-01T00:00:00.000Z",
            "type": "episode",
            "show": {"title": "Test Show", "ids": {"tmdb": 12345}},
            "episode": {"season": 1, "number": 1, "title": "Pilot"},
            "rating": 8,
        }
        mock_make_request.return_value = [rating_entry]

        trakt_importer = TraktImporter("testuser", self.user, "new")
        trakt_importer.process_ratings()

        episode_obj.refresh_from_db()
        # Trakt rating 8 on a 10-point scale → stored as 8.0 (no scaling needed)
        self.assertIsNotNone(episode_obj.score)
        self.assertEqual(float(episode_obj.score), 8.0)

    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    def test_process_episode_rating_no_season(self, mock_make_request):
        """Episode rating is silently skipped when the season isn't tracked."""
        rating_entry = {
            "rated_at": "2023-01-01T00:00:00.000Z",
            "type": "episode",
            "show": {"title": "Untracked Show", "ids": {"tmdb": 99999}},
            "episode": {"season": 1, "number": 1, "title": "Pilot"},
            "rating": 7,
        }
        mock_make_request.return_value = [rating_entry]

        trakt_importer = TraktImporter("testuser", self.user, "new")
        # Should not raise; simply skips because no matching Season exists
        trakt_importer.process_ratings()
        self.assertEqual(
            Episode.objects.filter(related_season__user=self.user).count(),
            0,
        )

    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_last_episode_import_marks_season_and_tv_completed(self, mock_get_metadata):
        """Regression test for #202: daily sync marks season/TV completed when last episode imported."""
        TMDB_ID = 99999
        SEASON_NUMBER = 1
        TOTAL_EPISODES = 20

        # Pre-create TV, season in DB (simulates prior sync of eps 1-19)
        item_tv, _ = Item.objects.get_or_create(
            media_id=TMDB_ID,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            defaults={"title": "Test Show", "image": ""},
        )
        tv_obj = TV.objects.create(item=item_tv, user=self.user, status=Status.IN_PROGRESS.value)
        item_season, _ = Item.objects.get_or_create(
            media_id=TMDB_ID,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=SEASON_NUMBER,
            defaults={"title": "Test Show", "image": ""},
        )
        season_obj = Season.objects.create(
            item=item_season,
            user=self.user,
            related_tv=tv_obj,
            status=Status.IN_PROGRESS.value,
        )

        def mock_metadata(media_type, tmdb_id, title, season_number=None):
            if media_type == MediaTypes.TV.value:
                return {
                    "title": "Test Show",
                    "image": "",
                    "last_episode_season": SEASON_NUMBER,
                    "max_progress": TOTAL_EPISODES,
                }
            if media_type == MediaTypes.SEASON.value:
                return {
                    "title": "Season 1",
                    "image": "",
                    "episodes": [
                        {"episode_number": i, "still_path": None}
                        for i in range(1, TOTAL_EPISODES + 1)
                    ],
                    "max_progress": TOTAL_EPISODES,
                }
            return None

        mock_get_metadata.side_effect = mock_metadata

        entry = {
            "type": "episode",
            "episode": {"season": SEASON_NUMBER, "number": TOTAL_EPISODES, "title": "Finale"},
            "show": {"title": "Test Show", "ids": {"tmdb": TMDB_ID}},
            "watched_at": "2024-06-01T00:00:00.000Z",
        }

        trakt_importer = TraktImporter("testuser", self.user, "new")
        trakt_importer.process_watched_episode(entry)
        helpers.bulk_create_media(trakt_importer.bulk_media, self.user)

        # This is the persistence step that the fix adds to import_data()
        from simple_history.utils import bulk_update_with_history
        if trakt_importer.completed_seasons:
            bulk_update_with_history(trakt_importer.completed_seasons, Season, fields=["status"])
        if trakt_importer.completed_tvs:
            bulk_update_with_history(trakt_importer.completed_tvs, TV, fields=["status"])

        season_obj.refresh_from_db()
        tv_obj.refresh_from_db()
        self.assertEqual(season_obj.status, Status.COMPLETED.value)
        self.assertEqual(tv_obj.status, Status.COMPLETED.value)

    # ------------------------------------------------------------------
    # Episode rating import — gap coverage
    # ------------------------------------------------------------------

    def _make_tv_season_episode(self, tmdb_id, season_num, episode_num, initial_score=None):
        """Helper: create TV → Season → Episode hierarchy and return all three objects."""
        tv_item, _ = Item.objects.get_or_create(
            media_id=str(tmdb_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            defaults={"title": "Test Show"},
        )
        tv_obj, _ = TV.objects.get_or_create(
            item=tv_item,
            user=self.user,
            defaults={"status": Status.IN_PROGRESS.value},
        )
        season_item, _ = Item.objects.get_or_create(
            media_id=str(tmdb_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=season_num,
            defaults={"title": f"Season {season_num}"},
        )
        season_obj, _ = Season.objects.get_or_create(
            item=season_item,
            user=self.user,
            defaults={"related_tv": tv_obj, "status": Status.IN_PROGRESS.value},
        )
        episode_item, _ = Item.objects.get_or_create(
            media_id=str(tmdb_id),
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=season_num,
            episode_number=episode_num,
            defaults={"title": f"S{season_num}E{episode_num}"},
        )
        kwargs = {"related_season": season_obj}
        if initial_score is not None:
            kwargs["score"] = initial_score
        episode_obj, _ = Episode.objects.get_or_create(item=episode_item, **kwargs)
        return tv_obj, season_obj, episode_obj

    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    def test_episode_rating_multiple_episodes(self, mock_make_request):
        """All episode ratings in a single batch are applied correctly."""
        TMDB_ID = 55500
        _, _, ep1 = self._make_tv_season_episode(TMDB_ID, 1, 1)
        _, _, ep2 = self._make_tv_season_episode(TMDB_ID, 1, 2)
        _, _, ep3 = self._make_tv_season_episode(TMDB_ID, 1, 3)

        mock_make_request.return_value = [
            {
                "rated_at": "2024-01-01T00:00:00.000Z",
                "type": "episode",
                "show": {"title": "Test Show", "ids": {"tmdb": TMDB_ID}},
                "episode": {"season": 1, "number": 1, "title": "Pilot"},
                "rating": 7,
            },
            {
                "rated_at": "2024-01-01T00:00:00.000Z",
                "type": "episode",
                "show": {"title": "Test Show", "ids": {"tmdb": TMDB_ID}},
                "episode": {"season": 1, "number": 2, "title": "Episode 2"},
                "rating": 8,
            },
            {
                "rated_at": "2024-01-01T00:00:00.000Z",
                "type": "episode",
                "show": {"title": "Test Show", "ids": {"tmdb": TMDB_ID}},
                "episode": {"season": 1, "number": 3, "title": "Episode 3"},
                "rating": 9,
            },
        ]

        TraktImporter("testuser", self.user, "new").process_ratings()

        ep1.refresh_from_db()
        ep2.refresh_from_db()
        ep3.refresh_from_db()
        self.assertEqual(float(ep1.score), 7.0)
        self.assertEqual(float(ep2.score), 8.0)
        self.assertEqual(float(ep3.score), 9.0)

    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    def test_episode_rating_overwrites_existing_score(self, mock_make_request):
        """A new rating overwrites an episode's pre-existing score."""
        TMDB_ID = 55501
        _, _, episode_obj = self._make_tv_season_episode(TMDB_ID, 1, 1, initial_score="5.0")

        mock_make_request.return_value = [
            {
                "rated_at": "2024-01-01T00:00:00.000Z",
                "type": "episode",
                "show": {"title": "Test Show", "ids": {"tmdb": TMDB_ID}},
                "episode": {"season": 1, "number": 1, "title": "Pilot"},
                "rating": 9,
            }
        ]

        TraktImporter("testuser", self.user, "new").process_ratings()

        episode_obj.refresh_from_db()
        self.assertEqual(float(episode_obj.score), 9.0)

    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    def test_episode_rating_no_episode_row(self, mock_make_request):
        """Episode rating is silently skipped when Season exists but Episode row doesn't."""
        TMDB_ID = 55502
        tv_item, _ = Item.objects.get_or_create(
            media_id=str(TMDB_ID),
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            defaults={"title": "Test Show"},
        )
        tv_obj, _ = TV.objects.get_or_create(
            item=tv_item,
            user=self.user,
            defaults={"status": Status.IN_PROGRESS.value},
        )
        season_item, _ = Item.objects.get_or_create(
            media_id=str(TMDB_ID),
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=1,
            defaults={"title": "Season 1"},
        )
        Season.objects.get_or_create(
            item=season_item,
            user=self.user,
            defaults={"related_tv": tv_obj, "status": Status.IN_PROGRESS.value},
        )
        # Intentionally no Episode row created

        mock_make_request.return_value = [
            {
                "rated_at": "2024-01-01T00:00:00.000Z",
                "type": "episode",
                "show": {"title": "Test Show", "ids": {"tmdb": TMDB_ID}},
                "episode": {"season": 1, "number": 1, "title": "Pilot"},
                "rating": 8,
            }
        ]

        # Should not raise; no Episode created
        TraktImporter("testuser", self.user, "new").process_ratings()
        self.assertEqual(Episode.objects.filter(related_season__user=self.user).count(), 0)

    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    def test_episode_rating_no_tmdb_id(self, mock_make_request):
        """Episode rating is silently skipped when the show has no TMDB ID."""
        mock_make_request.return_value = [
            {
                "rated_at": "2024-01-01T00:00:00.000Z",
                "type": "episode",
                "show": {"title": "No-ID Show", "ids": {"tmdb": None}},
                "episode": {"season": 1, "number": 1, "title": "Pilot"},
                "rating": 8,
            }
        ]

        # Should not raise; no DB writes
        TraktImporter("testuser", self.user, "new").process_ratings()
        self.assertEqual(Episode.objects.filter(related_season__user=self.user).count(), 0)

    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_episode_rating_mixed_payload(self, mock_get_metadata, mock_make_request):
        """Movie and episode ratings in the same payload are each handled correctly."""
        TMDB_ID = 55503
        _, _, episode_obj = self._make_tv_season_episode(TMDB_ID, 1, 1)

        mock_get_metadata.return_value = {"title": "Rated Movie", "image": "img.jpg"}
        mock_make_request.return_value = [
            {
                "rated_at": "2024-01-01T00:00:00.000Z",
                "type": "movie",
                "movie": {"title": "Rated Movie", "ids": {"tmdb": 77777}},
                "rating": 7,
            },
            {
                "rated_at": "2024-01-01T00:00:00.000Z",
                "type": "episode",
                "show": {"title": "Test Show", "ids": {"tmdb": TMDB_ID}},
                "episode": {"season": 1, "number": 1, "title": "Pilot"},
                "rating": 9,
            },
        ]

        trakt_importer = TraktImporter("testuser", self.user, "new")
        trakt_importer.process_ratings()

        # Movie rating queued in bulk_media
        self.assertEqual(len(trakt_importer.bulk_media[MediaTypes.MOVIE.value]), 1)
        self.assertEqual(trakt_importer.bulk_media[MediaTypes.MOVIE.value][0].score, 7)

        # Episode score written directly to DB
        episode_obj.refresh_from_db()
        self.assertEqual(float(episode_obj.score), 9.0)

    @patch("integrations.imports.trakt.TraktImporter._get_paginated_data")
    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_episode_rating_survives_full_import_flow(
        self, mock_get_metadata, mock_make_request, mock_get_paginated
    ):
        """Episode ratings are applied when running the full import_data() pipeline."""
        from integrations.imports.trakt import importer

        TMDB_ID = 55504
        _, _, episode_obj = self._make_tv_season_episode(TMDB_ID, 1, 1)

        # process_history + process_comments use paginated data; empty here
        mock_get_paginated.return_value = []
        # process_watchlist and process_ratings both call _make_api_request
        mock_make_request.side_effect = [
            [],  # watchlist call — empty
            [    # ratings call — one episode entry
                {
                    "rated_at": "2024-01-01T00:00:00.000Z",
                    "type": "episode",
                    "show": {"title": "Test Show", "ids": {"tmdb": TMDB_ID}},
                    "episode": {"season": 1, "number": 1, "title": "Pilot"},
                    "rating": 8,
                }
            ],
        ]
        mock_get_metadata.return_value = {"title": "Test Show", "image": "img.jpg"}

        importer(None, self.user, "new", "public_user")

        episode_obj.refresh_from_db()
        self.assertIsNotNone(episode_obj.score)
        self.assertEqual(float(episode_obj.score), 8.0)

    @patch("integrations.imports.trakt.TraktImporter._get_paginated_data")
    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_episode_rating_applied_on_first_ever_import(
        self, mock_get_metadata, mock_make_request, mock_get_paginated
    ):
        """Ratings land correctly when history and ratings are imported in the same run.

        Regression test: process_history() buffers new Season/Episode objects in
        bulk_media without writing them to the DB.  process_ratings() then runs
        before bulk_create_media() commits those rows, so a plain DB lookup finds
        nothing and silently drops the rating.  The fix checks media_instances for
        in-flight objects from the same run.
        """
        from integrations.imports.trakt import importer

        TMDB_ID = 55505
        # No pre-existing DB rows — simulates a brand-new Yamtrack account

        episode_entry = {
            "type": "episode",
            "episode": {"season": 1, "number": 1, "title": "Pilot"},
            "show": {"title": "New Show", "ids": {"tmdb": TMDB_ID}},
            "watched_at": "2024-01-01T00:00:00.000Z",
        }
        rating_entry = {
            "rated_at": "2024-01-01T00:00:00.000Z",
            "type": "episode",
            "show": {"title": "New Show", "ids": {"tmdb": TMDB_ID}},
            "episode": {"season": 1, "number": 1, "title": "Pilot"},
            "rating": 9,
        }

        def metadata_side_effect(media_type, tmdb_id, *args, **kwargs):
            if media_type == MediaTypes.TV.value:
                return {
                    "title": "New Show",
                    "image": "img.jpg",
                    "last_episode_season": None,
                }
            if media_type == MediaTypes.SEASON.value:
                return {
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.SEASON.value,
                    "season_title": "Season 1",
                    "season_number": 1,
                    "max_progress": 6,
                    "image": "img.jpg",
                    "episodes": [
                        {"episode_number": 1, "still_path": None},
                    ],
                    "score": 0,
                    "score_count": 0,
                    "synopsis": "",
                    "details": {},
                    "cast": [],
                    "crew": [],
                }
            return None

        mock_get_metadata.side_effect = metadata_side_effect
        # process_history uses _get_paginated_data; history returns one episode watch
        # process_comments uses _get_paginated_data; empty
        mock_get_paginated.side_effect = [
            [episode_entry],  # history
            [],               # comments
        ]
        # process_watchlist and process_ratings use _make_api_request
        mock_make_request.side_effect = [
            [],             # watchlist — empty
            [rating_entry], # ratings — one episode entry
        ]

        importer(None, self.user, "new", "public_user")

        episode_obj = Episode.objects.filter(
            related_season__user=self.user,
            item__episode_number=1,
        ).first()
        self.assertIsNotNone(episode_obj, "Episode should have been created by history import")
        self.assertIsNotNone(episode_obj.score, "Episode score should be set from Trakt rating")
        self.assertEqual(float(episode_obj.score), 9.0)

    # ------------------------------------------------------------------
    # Dropped show status import
    # ------------------------------------------------------------------

    @patch("integrations.imports.trakt.TraktImporter._get_paginated_data")
    def test_process_dropped_collects_ids(self, mock_get_paginated):
        """process_dropped() populates dropped_tmdb_ids from the hidden endpoint."""
        mock_get_paginated.return_value = [
            {"type": "show", "show": {"title": "Dropped Show", "ids": {"tmdb": 11111}}},
            {"type": "show", "show": {"title": "Also Dropped", "ids": {"tmdb": 22222}}},
            {"type": "movie", "movie": {"title": "Hidden Movie", "ids": {"tmdb": 33333}}},
        ]
        encrypted_token = helpers.encrypt("test_token")
        trakt_importer = TraktImporter("testuser", self.user, "new", refresh_token=encrypted_token)
        trakt_importer.process_dropped()

        self.assertIn("11111", trakt_importer.dropped_tmdb_ids)
        self.assertIn("22222", trakt_importer.dropped_tmdb_ids)
        # Movie-type hidden entries should be ignored
        self.assertNotIn("33333", trakt_importer.dropped_tmdb_ids)
        self.assertEqual(len(trakt_importer.dropped_tmdb_ids), 2)

    @patch("integrations.imports.trakt.TraktImporter._get_paginated_data")
    def test_process_dropped_skipped_without_oauth(self, mock_get_paginated):
        """process_dropped() is a no-op for public (non-OAuth) imports."""
        trakt_importer = TraktImporter("testuser", self.user, "new")
        trakt_importer.process_dropped()

        mock_get_paginated.assert_not_called()
        self.assertEqual(len(trakt_importer.dropped_tmdb_ids), 0)

    @patch("integrations.imports.trakt.TraktImporter._get_paginated_data")
    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_dropped_show_status_on_first_import(
        self, mock_get_metadata, mock_make_request, mock_get_paginated
    ):
        """A show that is both watched and dropped lands in DB with status Dropped."""
        from integrations.imports.trakt import importer

        TMDB_ID = 66601
        episode_entry = {
            "type": "episode",
            "episode": {"season": 1, "number": 1, "title": "Pilot"},
            "show": {"title": "Dropped Show", "ids": {"tmdb": TMDB_ID}},
            "watched_at": "2024-01-01T00:00:00.000Z",
        }

        def metadata_side_effect(media_type, tmdb_id, *args, **kwargs):
            if media_type == MediaTypes.TV.value:
                return {"title": "Dropped Show", "image": "img.jpg", "last_episode_season": None}
            if media_type == MediaTypes.SEASON.value:
                return {
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.SEASON.value,
                    "season_title": "Season 1",
                    "season_number": 1,
                    "max_progress": 6,
                    "image": "img.jpg",
                    "episodes": [{"episode_number": 1, "still_path": None}],
                    "score": 0,
                    "score_count": 0,
                    "synopsis": "",
                    "details": {},
                    "cast": [],
                    "crew": [],
                }
            return None

        mock_get_metadata.side_effect = metadata_side_effect
        mock_get_paginated.side_effect = [
            # process_dropped — show is hidden/dropped
            [{"type": "show", "show": {"title": "Dropped Show", "ids": {"tmdb": TMDB_ID}}}],
            [episode_entry],  # process_history
            [],               # process_comments
        ]
        mock_make_request.side_effect = [[], []]  # watchlist, ratings

        encrypted_token = helpers.encrypt("test_token")
        importer(encrypted_token, self.user, "new", "oauth_user")

        tv_obj = TV.objects.filter(user=self.user, item__media_id=str(TMDB_ID)).first()
        self.assertIsNotNone(tv_obj)
        self.assertEqual(tv_obj.status, Status.DROPPED.value)

    @patch("integrations.imports.trakt.TraktImporter._get_paginated_data")
    @patch("integrations.imports.trakt.TraktImporter._make_api_request")
    @patch("integrations.imports.trakt.TraktImporter._get_metadata")
    def test_dropped_show_updates_existing_tv(
        self, mock_get_metadata, mock_make_request, mock_get_paginated
    ):
        """A recurring import updates an existing IN_PROGRESS TV show to Dropped."""
        from integrations.imports.trakt import importer

        TMDB_ID = 66602
        # Pre-existing TV show in DB marked as IN_PROGRESS
        tv_item, _ = Item.objects.get_or_create(
            media_id=str(TMDB_ID),
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            defaults={"title": "Ongoing Show"},
        )
        tv_obj = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        episode_entry = {
            "type": "episode",
            "episode": {"season": 1, "number": 1, "title": "Old Episode"},
            "show": {"title": "Ongoing Show", "ids": {"tmdb": TMDB_ID}},
            "watched_at": "2024-01-01T00:00:00.000Z",
        }

        def metadata_side_effect(media_type, tmdb_id, *args, **kwargs):
            if media_type == MediaTypes.TV.value:
                return {"title": "Ongoing Show", "image": "img.jpg", "last_episode_season": None}
            if media_type == MediaTypes.SEASON.value:
                return {
                    "source": Sources.TMDB.value,
                    "media_type": MediaTypes.SEASON.value,
                    "season_title": "Season 1",
                    "season_number": 1,
                    "max_progress": 6,
                    "image": "img.jpg",
                    "episodes": [{"episode_number": 1, "still_path": None}],
                    "score": 0,
                    "score_count": 0,
                    "synopsis": "",
                    "details": {},
                    "cast": [],
                    "crew": [],
                }
            return None

        mock_get_metadata.side_effect = metadata_side_effect
        mock_get_paginated.side_effect = [
            # process_dropped — show is now dropped
            [{"type": "show", "show": {"title": "Ongoing Show", "ids": {"tmdb": TMDB_ID}}}],
            [episode_entry],  # process_history
            [],               # process_comments
        ]
        mock_make_request.side_effect = [[], []]  # watchlist, ratings

        encrypted_token = helpers.encrypt("test_token")
        importer(encrypted_token, self.user, "new", "oauth_user")

        tv_obj.refresh_from_db()
        self.assertEqual(tv_obj.status, Status.DROPPED.value)
