import logging
from unittest import mock
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone

from app.models import Episode, Item, MediaTypes, Movie, Sources, Status, TV
from app.providers import services
from integrations.imports import plex
from integrations.imports.plex import PlexHistoryImporter
from integrations.models import PlexAccount

# Suppress logging during tests
logging.getLogger("integrations.imports.plex").setLevel(logging.CRITICAL)


class TestPlexHybridImport(TestCase):
    """
    Tests for hybrid library handling (e.g., Movies in TV libraries) and ID resolution.
    Originally from test_plex_hybrid_resolution.py.
    """

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="testuser")
        self.account = PlexAccount.objects.create(
            user=self.user,
            plex_token="token",
            plex_username="testuser",
            plex_account_id="4441952",
        )
        self.user.plex_usernames = "testuser"
        self.user.save()

    @patch("integrations.imports.plex.plex_api.fetch_metadata")
    @patch("integrations.imports.plex.plex_api.list_users")
    @patch("integrations.imports.plex.services.search")
    @patch("integrations.imports.plex.services.get_media_metadata")
    @patch("integrations.imports.plex.plex_api.fetch_history")
    @patch("integrations.imports.plex.plex_api.list_resources")
    @patch("integrations.imports.plex.plex_api.list_sections")
    @patch("integrations.imports.plex.plex_api.fetch_account")
    def test_movie_in_tv_library_resolution(
        self,
        mock_fetch_account,
        mock_list_sections,
        mock_list_resources,
        mock_fetch_history,
        mock_get_metadata,
        mock_search,
        mock_list_users,
        mock_fetch_metadata,
    ):
        """Verify that a movie in a TV library is processed as a Movie when season info is missing."""
        mock_fetch_account.return_value = {"id": "4441952"}
        mock_list_sections.return_value = [
            {
                "id": "1",
                "machine_identifier": "machine",
                "title": "Anime",
                "type": "show",
            }
        ]
        mock_list_resources.return_value = [
            {"machine_identifier": "machine", "connections": [{"uri": "http://plex"}]}
        ]
        mock_list_users.return_value = []

        # Entry in 'show' library but missing season/episode
        entry = {
            "type": "movie",
            "title": "Jujutsu Kaisen 0",
            "guid": "tmdb://123",
            "viewedAt": 1700000000,
            "accountID": "4441952",
            "ratingKey": "rk123",
            "key": "/metadata/rk123",
        }
        mock_fetch_history.return_value = ([entry], 1)

        mock_get_metadata.return_value = {
            "title": "Jujutsu Kaisen 0",
            "media_id": 123,
            "media_type": "movie",
            "max_progress": 1,
            "image": "/p123.jpg",
            "summary": "Summary",
            "details": {"release_date": "2021-12-24"},
        }

        plex.importer("machine::1", self.user, "new")

        self.assertEqual(
            Movie.objects.filter(user=self.user, item__media_id="123").count(), 1
        )
        self.assertEqual(TV.objects.filter(user=self.user).count(), 0)

    @patch("integrations.imports.plex.services.search")
    @patch("integrations.imports.plex.plex_api.fetch_history")
    @patch("integrations.imports.plex.plex_api.list_resources")
    @patch("integrations.imports.plex.plex_api.list_sections")
    @patch("integrations.imports.plex.plex_api.fetch_account")
    @patch("integrations.imports.plex.plex_api.list_users")
    def test_skipped_user_diagnostics(
        self,
        mock_list_users,
        mock_fetch_account,
        mock_list_sections,
        mock_list_resources,
        mock_fetch_history,
        mock_search,
    ):
        """Verify that skipped users show names in logs if available."""
        mock_fetch_account.return_value = {"id": "4441952"}
        mock_list_sections.return_value = [
            {"id": "1", "machine_identifier": "machine", "title": "Anime", "type": "show"}
        ]
        mock_list_resources.return_value = [
            {"machine_identifier": "machine", "connections": [{"uri": "http://plex"}]}
        ]
        mock_list_users.return_value = [{"id": "999", "title": "OtherUser"}]

        entry = {
            "type": "episode",
            "title": "Ep 1",
            "accountID": "999",
            "viewedAt": 1700000000,
        }
        mock_fetch_history.return_value = ([entry], 1)

        importer = plex.PlexHistoryImporter(
            user=self.user, account=self.account, mode="new", library="machine::1"
        )
        importer._init_allowed_usernames()
        importer._init_allowed_account_ids()
        importer.import_data()

        self.assertIn("OtherUser (accountID=999)", importer._skipped_user_samples)

    @patch("integrations.imports.plex.plex_api.fetch_metadata")
    @patch("integrations.imports.plex.plex_api.list_users")
    @patch("integrations.imports.plex.services.search")
    @patch("integrations.imports.plex.services.get_media_metadata")
    @patch("integrations.imports.plex.plex_api.fetch_history")
    @patch("integrations.imports.plex.plex_api.list_resources")
    @patch("integrations.imports.plex.plex_api.list_sections")
    @patch("integrations.imports.plex.plex_api.fetch_account")
    def test_tvdb_resolution_fallback(
        self,
        mock_fetch_account,
        mock_list_sections,
        mock_list_resources,
        mock_fetch_history,
        mock_get_metadata,
        mock_search,
        mock_list_users,
        mock_fetch_metadata,
    ):
        """Verify that TVDB ID extraction works and falls back to title if initial find fails."""
        mock_fetch_account.return_value = {"id": "4441952"}
        mock_list_sections.return_value = [
            {"id": "1", "machine_identifier": "machine", "title": "Anime", "type": "show"}
        ]
        mock_list_resources.return_value = [
            {"machine_identifier": "machine", "connections": [{"uri": "http://plex"}]}
        ]
        mock_list_users.return_value = []

        entry = {
            "type": "show",
            "title": "Anime Show",
            "grandparentTitle": "Anime Show",
            "parentIndex": 1,
            "index": 1,
            "guid": "tvdb://456",
            "viewedAt": 1700000000,
            "accountID": "4441952",
            "ratingKey": "rk456",
            "key": "/metadata/rk456",
        }
        mock_fetch_history.return_value = ([entry], 1)

        # Mock search result for fallback in _record_episode_entry
        mock_search.return_value = {
            "results": [{"media_id": 789, "title": "Anime Show", "media_type": "tv"}]
        }

        # Mock metadata for the resolved ID
        def metadata_side_effect(
            media_type, media_id, source, season_numbers=None, **kwargs
        ):
            if str(media_id) == "789":
                return {
                    "title": "Anime Show",
                    "media_id": 789,
                    "media_type": "tv",
                    "max_progress": 1,
                    "image": "/tv789.jpg",
                    "summary": "TV Summary",
                    "related": {"seasons": [{"season_number": 1, "episode_count": 5}]},
                    "season/1": {
                        "episodes": [
                            {
                                "episode_number": 1,
                                "name": "Ep 1",
                                "still_path": "/p1.jpg",
                                "air_date": "2020-01-01",
                            }
                        ],
                        "name": "Season 1",
                        "season_number": 1,
                        "poster_path": "/s1.jpg",
                        "id": 789,
                    },
                }
            return None

        mock_get_metadata.side_effect = metadata_side_effect

        with patch("integrations.webhooks.plex.app.providers.tmdb.find") as mock_find:
            # First find fails
            mock_find.return_value = {"tv_results": [], "tv_episode_results": []}

            plex.importer("machine::1", self.user, "new")

        self.assertEqual(
            TV.objects.filter(user=self.user, item__media_id="789").count(), 1
        )
        self.assertEqual(
            Episode.objects.filter(
                related_season__related_tv__item__media_id="789"
            ).count(),
            1,
        )

    def test_existing_tv_import_keeps_resolved_metadata_when_cache_key_differs(self):
        """Existing-show imports should not lose fallback metadata when IDs differ."""
        tv_item = Item.objects.create(
            title="Yellowstone",
            media_id="73586",
            media_type=MediaTypes.TV.value,
            source=Sources.TMDB.value,
            image="https://example.com/show.jpg",
        )
        existing_tv = TV.objects.create(
            item=tv_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        importer = PlexHistoryImporter(
            user=self.user,
            account=self.account,
            mode="new",
            library="machine::1",
        )
        importer._episode_records = [
            {
                "tmdb_id": "1515183",
                "season_number": 1,
                "episode_number": 4,
                "watched_at": timezone.now().replace(second=0, microsecond=0),
                "viewed_at_ts": 1700000000,
                "plex_rating_key": "rk-yellowstone",
                "rating": None,
                "title": "Going Back to Cali",
                "series_title": "Yellowstone",
            }
        ]
        importer._tv_metadata_cache = {
            "1515183": {
                "media_id": "73586",
                "title": "Yellowstone",
                "original_title": "Yellowstone",
                "localized_title": "Yellowstone",
                "image": "https://example.com/show.jpg",
                "tvdb_id": "361315",
                "season/1": {
                    "image": "https://example.com/season1.jpg",
                    "episodes": [{"episode_number": 4}],
                },
            }
        }

        importer._build_bulk_media()

        self.assertEqual(len(importer.bulk_media[MediaTypes.SEASON.value]), 1)
        self.assertEqual(len(importer.bulk_media[MediaTypes.EPISODE.value]), 1)

        season = importer.bulk_media[MediaTypes.SEASON.value][0]
        episode = importer.bulk_media[MediaTypes.EPISODE.value][0]

        self.assertEqual(season.related_tv, existing_tv)
        self.assertEqual(season.item.media_id, "73586")
        self.assertEqual(season.item.title, "Yellowstone")
        self.assertEqual(episode.item.media_id, "1515183")
        self.assertEqual(episode.item.title, "Yellowstone")


class TestPlexImportScenarios(TestCase):
    """
    Tests specific user-reported regression scenarios and edge cases.
    Originally from test_plex_regression.py.
    """

    @mock.patch("integrations.imports.helpers.get_existing_media")
    def setUp(self, mock_get_existing_media):
        mock_get_existing_media.return_value = {}
        self.user = mock.Mock()
        self.account = mock.Mock()
        # Mock account.plex_account_id to avoid NoneType error in __init__
        self.account.plex_account_id = "12345"
        self.importer = PlexHistoryImporter(
            self.user, self.account, mode="new", library={"key": "1", "title": "Library"}
        )

    def _create_404_error(self):
        mock_response = mock.Mock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"
        mock_error = mock.Mock()
        mock_error.response = mock_response
        return services.ProviderAPIError(Sources.TMDB.value, mock_error)

    @mock.patch("integrations.imports.plex.services.search")
    @mock.patch("integrations.imports.plex.services.get_media_metadata")
    def test_the_studio_fallback(self, mock_get_metadata, mock_search):
        """Test fallback for 'The Studio (2025)' bad IDs."""
        bad_id = "5772141"
        correct_id = "247767"
        title = "The Studio"

        # Setup mocks
        error_404 = self._create_404_error()

        # Subsequent call returns valid metadata
        valid_metadata = {"id": correct_id, "title": title}

        def side_effect(method, media_id, source, **kwargs):
            if media_id == bad_id:
                raise error_404
            if media_id == correct_id:
                return valid_metadata
            return None

        mock_get_metadata.side_effect = side_effect

        # Search returns the correct show
        mock_search.return_value = {
            "results": [{"media_id": correct_id, "title": title}]
        }

        # Execute
        result = self.importer._get_tv_metadata(bad_id, {1}, title)

        # Verify
        self.assertEqual(result, valid_metadata)
        mock_search.assert_called_with(MediaTypes.TV.value, title, page=1)

        # Verify calls to get_media_metadata
        # 1. Bad ID
        mock_get_metadata.assert_any_call(
            "tv_with_seasons", bad_id, Sources.TMDB.value, season_numbers=[1]
        )
        # 2. Correct ID
        mock_get_metadata.assert_any_call(
            "tv_with_seasons", correct_id, Sources.TMDB.value, season_numbers=[1]
        )

    @mock.patch("integrations.imports.plex.services.search")
    @mock.patch("integrations.imports.plex.services.get_media_metadata")
    def test_foundation_fallback(self, mock_get_metadata, mock_search):
        """Test fallback for 'Foundation (2021)'."""
        bad_id = "6215884"
        correct_id = "93740"
        title = "Foundation"

        error_404 = self._create_404_error()

        mock_get_metadata.side_effect = lambda m, mid, s, **Kw: (
            (_ for _ in ()).throw(error_404) if mid == bad_id else {"id": mid}
        )

        mock_search.return_value = {"results": [{"media_id": correct_id}]}

        result = self.importer._get_tv_metadata(bad_id, {3}, title)

        self.assertEqual(result["id"], correct_id)
        mock_search.assert_called_with(MediaTypes.TV.value, title, page=1)

    @mock.patch("integrations.imports.plex.services.search")
    @mock.patch("integrations.imports.plex.services.get_media_metadata")
    def test_invincible_fallback(self, mock_get_metadata, mock_search):
        """Test fallback for 'Invincible'."""
        bad_id = "5678354"
        correct_id = "95557"
        title = "Invincible"

        error_404 = self._create_404_error()

        mock_get_metadata.side_effect = lambda m, mid, s, **kw: (
            (_ for _ in ()).throw(error_404) if mid == bad_id else {"id": mid}
        )

        mock_search.return_value = {"results": [{"media_id": correct_id}]}

        result = self.importer._get_tv_metadata(bad_id, {2, 3}, title)

        self.assertEqual(result["id"], correct_id)

    @mock.patch("integrations.imports.plex.services.search")
    @mock.patch("integrations.imports.plex.services.get_media_metadata")
    def test_yellowstone_fallback(self, mock_get_metadata, mock_search):
        """Test fallback for 'Yellowstone (2018)'."""
        bad_id = "5605563"
        correct_id = "73586"
        title = "Yellowstone"

        error_404 = self._create_404_error()

        mock_get_metadata.side_effect = lambda m, mid, s, **kw: (
            (_ for _ in ()).throw(error_404) if mid == bad_id else {"id": mid}
        )

        mock_search.return_value = {"results": [{"media_id": correct_id}]}

        # Test Season 5 request
        result = self.importer._get_tv_metadata(bad_id, {5}, title)
        self.assertEqual(result["id"], correct_id)

    @mock.patch("integrations.imports.plex.services.search")
    @mock.patch("integrations.imports.plex.services.get_media_metadata")
    def test_franklin_fallback_ambiguity(self, mock_get_metadata, mock_search):
        """Test fallback for 'Franklin (2024)' - verify correct ID usage."""
        bad_id = "4902608"
        returned_id = "1458"
        title = "Franklin"

        error_404 = self._create_404_error()

        mock_get_metadata.side_effect = lambda m, mid, s, **kw: (
            (_ for _ in ()).throw(error_404) if mid == bad_id else {"id": mid}
        )

        mock_search.return_value = {"results": [{"media_id": returned_id}]}

        result = self.importer._get_tv_metadata(bad_id, {1}, title)

        self.assertEqual(result["id"], returned_id)

    @mock.patch("integrations.imports.plex.services.get_media_metadata")
    def test_sesame_street_missing_seasons(self, mock_get_metadata):
        """Test Sesame Street valid ID but missing seasons."""
        tmdb_id = "47480"
        seasons = {1940, 1950, 1960}

        # services.get_media_metadata should return whatever it finds
        # It shouldn't crash.
        # We simulate a successful return (partial or empty seasons)
        mock_get_metadata.return_value = {
            "id": tmdb_id,
            "title": "Sesame Street",
            # No season keys for 1940/1950/1960
        }

        result = self.importer._get_tv_metadata(tmdb_id, seasons, "Sesame Street")

        self.assertEqual(result["id"], tmdb_id)
        # Should not raise exception
