import logging
from unittest import mock
from unittest.mock import patch

from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone

from app.models import (
    Anime,
    Episode,
    Item,
    ItemProviderLink,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
    TV,
)
from app.providers import services
from integrations import tasks
from integrations.imports import helpers, plex
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
    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data", return_value={})
    @patch("integrations.imports.plex.app.providers.tvdb.enabled", return_value=False)
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
        _mock_tvdb_enabled,
        _mock_mapping_data,
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

        with (
            patch("integrations.webhooks.base.app.providers.tmdb.find") as mock_find,
            patch("integrations.webhooks.base.app.providers.tmdb.search") as mock_tmdb_search,
        ):
            # First find fails
            mock_find.return_value = {"tv_results": [], "tv_episode_results": []}
            mock_tmdb_search.return_value = mock_search.return_value

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

    def test_existing_season_is_reused_when_resolved_tv_id_differs(self):
        """Resolved imports should reuse an existing season instead of inserting a duplicate."""
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
        season_item = Item.objects.create(
            title="Yellowstone",
            media_id="73586",
            media_type=MediaTypes.SEASON.value,
            source=Sources.TMDB.value,
            image="https://example.com/season1.jpg",
            season_number=1,
        )
        existing_season = Season.objects.create(
            item=season_item,
            user=self.user,
            related_tv=existing_tv,
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

        self.assertEqual(len(importer.bulk_media[MediaTypes.SEASON.value]), 0)
        self.assertEqual(len(importer.bulk_media[MediaTypes.EPISODE.value]), 1)
        self.assertEqual(
            importer.bulk_media[MediaTypes.EPISODE.value][0].related_season,
            existing_season,
        )

        helpers.bulk_create_media(importer.bulk_media, self.user)

        self.assertEqual(
            Season.objects.filter(related_tv=existing_tv, item__season_number=1).count(),
            1,
        )
        self.assertEqual(Episode.objects.filter(related_season=existing_season).count(), 1)

    @patch("integrations.imports.plex.plex_api.fetch_section_all_items")
    @patch("integrations.imports.plex.PlexWebhookProcessor._find_tv_media_id")
    @patch("integrations.imports.plex.services.get_media_metadata")
    @patch("integrations.imports.plex.plex_api.fetch_metadata")
    @patch("integrations.imports.plex.plex_api.list_users")
    @patch("integrations.imports.plex.plex_api.fetch_history")
    @patch("integrations.imports.plex.plex_api.list_resources")
    @patch("integrations.imports.plex.plex_api.list_sections")
    @patch("integrations.imports.plex.plex_api.fetch_account")
    def test_episode_import_uses_library_media_type_in_item_lookup(
        self,
        mock_fetch_account,
        mock_list_sections,
        mock_list_resources,
        mock_fetch_history,
        mock_list_users,
        mock_fetch_metadata,
        mock_get_metadata,
        mock_find_tv_media_id,
        mock_fetch_section_items,
    ):
        """Episode imports should target the normalized library media type lookup."""
        Item.objects.create(
            title="Yellowstone",
            media_id="1515183",
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.ANIME.value,
            source=Sources.TMDB.value,
            image="https://example.com/anime-episode.jpg",
            season_number=1,
            episode_number=4,
        )
        Item.objects.create(
            title="Yellowstone",
            media_id="1515183",
            media_type=MediaTypes.EPISODE.value,
            library_media_type=MediaTypes.EPISODE.value,
            source=Sources.TMDB.value,
            image="https://example.com/episode.jpg",
            season_number=1,
            episode_number=4,
        )

        mock_fetch_account.return_value = {"id": "4441952"}
        mock_list_sections.return_value = [
            {"id": "1", "machine_identifier": "machine", "title": "TV", "type": "show"}
        ]
        mock_list_resources.return_value = [
            {"machine_identifier": "machine", "connections": [{"uri": "http://plex"}]}
        ]
        mock_list_users.return_value = []
        mock_fetch_metadata.return_value = None
        mock_fetch_section_items.return_value = ([], 0)
        mock_find_tv_media_id.return_value = ("73586", 1, 4)
        mock_fetch_history.return_value = (
            [
                {
                    "type": "episode",
                    "title": "Going Back to Cali",
                    "grandparentTitle": "Yellowstone",
                    "parentIndex": 1,
                    "index": 4,
                    "guid": "tmdb://1515183",
                    "viewedAt": 1700000000,
                    "accountID": "4441952",
                    "ratingKey": "rk-yellowstone",
                    "key": "/metadata/rk-yellowstone",
                }
            ],
            1,
        )
        mock_get_metadata.return_value = {
            "media_id": "73586",
            "title": "Yellowstone",
            "original_title": "Yellowstone",
            "localized_title": "Yellowstone",
            "image": "https://example.com/show.jpg",
            "tvdb_id": "361315",
            "season/1": {
                "image": "https://example.com/season1.jpg",
                "episodes": [
                    {
                        "episode_number": 4,
                        "still_path": "/episode4.jpg",
                    }
                ],
            },
        }

        plex.importer("machine::1", self.user, "new")

        self.assertEqual(
            Item.objects.filter(
                media_id="1515183",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
            ).count(),
            2,
        )
        imported_episode = Episode.objects.get(related_season__user=self.user)
        self.assertEqual(
            imported_episode.item.library_media_type,
            MediaTypes.EPISODE.value,
        )
        self.assertFalse(
            Episode.objects.filter(
                related_season__user=self.user,
                item__library_media_type=MediaTypes.ANIME.value,
            ).exists()
        )


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


class TestPlexAnimeImportRouting(TestCase):
    """Regression coverage for Plex imports that mix TV and anime history."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="plexanime")
        self.account = PlexAccount.objects.create(
            user=self.user,
            plex_token="token",
            plex_username="plexanime",
            plex_account_id="111",
        )
        self.user.plex_usernames = "plexanime"
        self.user.save()

    def _importer(self):
        return PlexHistoryImporter(
            user=self.user,
            account=self.account,
            mode="new",
            library="machine::1",
        )

    def _record(
        self,
        tmdb_id,
        season,
        episode,
        *,
        tvdb_id=None,
        title="Episode",
        series_title="Series",
        viewed_at=1700000000,
    ):
        return {
            "tmdb_id": str(tmdb_id),
            "external_ids": {
                "tmdb_id": str(tmdb_id),
                "tvdb_id": str(tvdb_id) if tvdb_id else None,
                "imdb_id": None,
                "anidb_id": None,
                "plex_guid": None,
            },
            "season_number": season,
            "episode_number": episode,
            "watched_at": timezone.datetime.fromtimestamp(
                viewed_at,
                tz=timezone.get_current_timezone(),
            ).replace(second=0, microsecond=0),
            "viewed_at_ts": viewed_at,
            "plex_rating_key": f"rk-{tmdb_id}-{season}-{episode}",
            "rating": None,
            "title": title,
            "series_title": series_title,
            "guid": [{"id": f"tmdb://{tmdb_id}"}],
        }

    def _metadata(self, tmdb_id, title, *, tvdb_id=None, seasons=None):
        metadata = {
            "media_id": str(tmdb_id),
            "title": title,
            "original_title": title,
            "localized_title": title,
            "image": "https://example.com/show.jpg",
            "tvdb_id": str(tvdb_id) if tvdb_id else None,
        }
        for season, episodes in (seasons or {}).items():
            metadata[f"season/{season}"] = {
                "image": f"https://example.com/{tmdb_id}/s{season}.jpg",
                "max_progress": max(episodes),
                "episodes": [{"episode_number": episode} for episode in episodes],
            }
        return metadata

    def _create_mal_mapping(self, mal_id, provider, provider_id, *, season, offset=0):
        item = Item.objects.create(
            media_id=str(mal_id),
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title=f"Anime {mal_id}",
            image="https://example.com/anime.jpg",
        )
        ItemProviderLink.objects.create(
            item=item,
            provider=provider,
            provider_media_id=str(provider_id),
            provider_media_type=MediaTypes.TV.value,
            season_number=season,
            episode_offset=offset,
        )
        return item

    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data", return_value={})
    @patch("integrations.imports.plex.app.providers.tvdb.enabled", return_value=False)
    @patch("integrations.imports.plex.app.providers.mal.anime")
    def test_mixed_tv_and_anime_imports_do_not_share_tv_rows(
        self,
        mock_mal,
        _mock_tvdb_enabled,
        _mock_mapping_data,
    ):
        """A mixed Plex show library should keep TV rows and Anime progress separate."""
        mock_mal.return_value = {
            "title": "One Piece Anime",
            "image": "https://example.com/one-piece-anime.jpg",
            "max_progress": 37,
        }
        self._create_mal_mapping(
            "21",
            Sources.TVDB.value,
            "tvdb-anime",
            season=2,
        )
        importer = self._importer()
        importer._episode_records = [
            self._record(
                "tmdb-live-action",
                2,
                episode,
                title=f"Live Action {episode}",
                series_title="ONE PIECE",
                viewed_at=1700000000 + episode,
            )
            for episode in range(1, 9)
        ] + [
            self._record(
                "tmdb-anime",
                2,
                episode,
                tvdb_id="tvdb-anime",
                title=f"Anime {episode}",
                series_title="One Piece",
                viewed_at=1700001000 + episode,
            )
            for episode in range(1, 9)
        ]
        importer._tv_metadata_cache = {
            "tmdb-live-action": self._metadata(
                "tmdb-live-action",
                "ONE PIECE",
                seasons={2: range(1, 9)},
            ),
            "tmdb-anime": self._metadata(
                "tmdb-anime",
                "One Piece",
                tvdb_id="tvdb-anime",
                seasons={1: range(1, 37)},
            ),
        }

        importer._build_bulk_media()

        self.assertEqual(len(importer.bulk_media[MediaTypes.TV.value]), 1)
        self.assertEqual(len(importer.bulk_media[MediaTypes.SEASON.value]), 1)
        self.assertEqual(len(importer.bulk_media[MediaTypes.EPISODE.value]), 8)
        self.assertEqual(
            Anime.objects.get(user=self.user, item__media_id="21").progress,
            8,
        )
        self.assertFalse(
            any("season 2 not found" in warning for warning in importer.warnings),
        )
        self.assertFalse(
            Item.objects.filter(
                source=Sources.TMDB.value,
                media_id="tmdb-anime",
                media_type__in=[MediaTypes.TV.value, MediaTypes.SEASON.value],
            ).exists(),
        )

    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data", return_value={})
    @patch("integrations.imports.plex.app.providers.tvdb.enabled", return_value=False)
    @patch("integrations.imports.plex.app.providers.mal.anime")
    def test_anime_mapping_advances_to_highest_imported_episode(
        self,
        mock_mal,
        _mock_tvdb_enabled,
        _mock_mapping_data,
    ):
        """Multiple Plex anime history rows should not stop at the first episode."""
        mock_mal.return_value = {
            "title": "Progress Anime",
            "image": "https://example.com/progress.jpg",
            "max_progress": 91,
        }
        self._create_mal_mapping("91", Sources.TVDB.value, "tvdb-progress", season=3)
        importer = self._importer()
        importer._episode_records = [
            self._record(
                "tmdb-progress",
                3,
                episode,
                tvdb_id="tvdb-progress",
                viewed_at=1700010000 + episode,
            )
            for episode in range(1, 90)
        ]
        importer._tv_metadata_cache = {
            "tmdb-progress": self._metadata(
                "tmdb-progress",
                "Progress Anime",
                tvdb_id="tvdb-progress",
                seasons={1: range(1, 13)},
            ),
        }

        importer._build_bulk_media()

        self.assertEqual(
            Anime.objects.get(user=self.user, item__media_id="91").progress,
            89,
        )
        self.assertEqual(len(importer.bulk_media[MediaTypes.EPISODE.value]), 0)

    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data", return_value={})
    @patch("integrations.imports.plex.app.providers.tvdb.series_has_anime_genre", return_value=True)
    @patch("integrations.imports.plex.app.providers.tvdb.tv")
    @patch("integrations.imports.plex.app.providers.tvdb.enabled", return_value=True)
    def test_unmapped_anime_special_is_skipped_without_tv_progress(
        self,
        _mock_tvdb_enabled,
        mock_tvdb_tv,
        _mock_has_anime_genre,
        _mock_mapping_data,
    ):
        """Season-0 anime rows need explicit mapping and should not corrupt TV rows."""
        mock_tvdb_tv.return_value = {
            "title": "Special Anime",
            "genres": ["Anime"],
            "provider_external_ids": {"mal_id": "999"},
        }
        importer = self._importer()
        importer._episode_records = [
            self._record(
                "tmdb-special",
                0,
                1,
                tvdb_id="tvdb-special",
                title="Special",
                series_title="Special Anime",
            )
        ]
        importer._tv_metadata_cache = {
            "tmdb-special": self._metadata(
                "tmdb-special",
                "Special Anime",
                tvdb_id="tvdb-special",
                seasons={0: [1]},
            ),
        }

        importer._build_bulk_media()

        self.assertFalse(Anime.objects.filter(user=self.user).exists())
        self.assertEqual(len(importer.bulk_media[MediaTypes.EPISODE.value]), 0)
        self.assertIn("no MAL episode mapping found", "\n".join(importer.warnings))

    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data", return_value={})
    @patch("integrations.imports.plex.app.providers.tvdb.enabled", return_value=False)
    def test_normal_tv_import_creates_all_history_episodes(
        self,
        _mock_tvdb_enabled,
        _mock_mapping_data,
    ):
        """A normal TV show with 16 watched entries should import all 16 episodes."""
        importer = self._importer()
        importer._episode_records = [
            self._record(
                "tmdb-tv",
                1,
                episode,
                title=f"Episode {episode}",
                series_title="Sixteen Episode Show",
                viewed_at=1700020000 + episode,
            )
            for episode in range(1, 17)
        ]
        importer._tv_metadata_cache = {
            "tmdb-tv": self._metadata(
                "tmdb-tv",
                "Sixteen Episode Show",
                seasons={1: range(1, 17)},
            ),
        }

        importer._build_bulk_media()

        self.assertEqual(len(importer.bulk_media[MediaTypes.TV.value]), 1)
        self.assertEqual(len(importer.bulk_media[MediaTypes.SEASON.value]), 1)
        self.assertEqual(len(importer.bulk_media[MediaTypes.EPISODE.value]), 16)
        self.assertFalse(Anime.objects.filter(user=self.user).exists())

    @patch("integrations.webhooks.anime_mappings.fetch_mapping_data", return_value={})
    @patch("integrations.imports.plex.app.providers.tvdb.enabled", return_value=False)
    @patch("integrations.imports.plex.plex_api.fetch_section_all_items")
    def test_library_ratings_do_not_mark_unwatched_movies_completed(
        self,
        mock_fetch_section_items,
        _mock_tvdb_enabled,
        _mock_mapping_data,
    ):
        """Ratings/collection scans alone must not create watched Movie rows."""
        mock_fetch_section_items.return_value = (
            [
                {
                    "title": "Unwatched Rated Movie",
                    "userRating": 8,
                    "Guid": [{"id": "com.plexapp.agents.themoviedb://12345?lang=en"}],
                }
            ],
            1,
        )
        importer = self._importer()
        importer._import_ratings_from_library(
            {
                "id": "1",
                "key": "1",
                "title": "Movies",
                "type": "movie",
            },
            "http://plex",
            token="token",
        )
        importer._build_bulk_media()

        self.assertFalse(Movie.objects.filter(user=self.user).exists())
        self.assertEqual(
            importer._library_ratings[("tmdb", "12345")],
            8,
        )


class TestPlexPostImportSideEffects(TestCase):
    """Plex imports should refresh dependent caches and queue collection refresh."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="plexsideeffects")

    @patch("integrations.tasks.update_collection_metadata_from_plex.apply_async")
    @patch("app.statistics_cache.schedule_all_ranges_refresh")
    @patch("integrations.tasks.history_cache.invalidate_history_cache")
    @patch("integrations.tasks.events.tasks.reload_calendar.delay")
    @patch("integrations.imports.plex.importer")
    def test_plex_import_schedules_history_statistics_and_collection_refresh(
        self,
        mock_importer,
        mock_reload_calendar,
        mock_invalidate_history,
        mock_schedule_stats,
        mock_collection_refresh,
    ):
        mock_importer.return_value = ({"created": 1}, "")

        result = tasks.import_media(mock_importer, "all", self.user.id, "new")

        self.assertIn("1 created", result)
        mock_reload_calendar.assert_called_once()
        mock_invalidate_history.assert_called_once_with(self.user.id, force=True)
        mock_schedule_stats.assert_called_once_with(self.user.id)
        mock_collection_refresh.assert_called_once_with(
            args=("all", self.user.id),
            countdown=60,
        )


class TestPlexMultiServerImport(TestCase):
    """Tests for multi-server / shared-library import resilience."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="multiserveruser")
        self.account = PlexAccount.objects.create(
            user=self.user,
            plex_token="personal-token",
            plex_username="multiserveruser",
            plex_account_id="9999",
        )
        self.user.plex_usernames = "multiserveruser"
        self.user.save()

    # ------------------------------------------------------------------
    # Test 1: section access_token is used instead of the personal token
    # ------------------------------------------------------------------
    @patch("integrations.imports.plex.plex_api.fetch_section_all_items")
    @patch("integrations.imports.plex.plex_api.fetch_metadata")
    @patch("integrations.imports.plex.plex_api.list_users")
    @patch("integrations.imports.plex.plex_api.fetch_history")
    @patch("integrations.imports.plex.plex_api.list_resources")
    @patch("integrations.imports.plex.plex_api.list_sections")
    @patch("integrations.imports.plex.plex_api.fetch_account")
    def test_friend_server_section_uses_server_access_token(
        self,
        mock_fetch_account,
        mock_list_sections,
        mock_list_resources,
        mock_fetch_history,
        mock_list_users,
        mock_fetch_metadata,
        mock_fetch_section_items,
    ):
        """fetch_history must be called with the section's access_token, not the personal token."""
        mock_fetch_account.return_value = {"id": "9999"}
        mock_list_users.return_value = []
        mock_fetch_metadata.return_value = None
        mock_fetch_section_items.return_value = ([], 0)

        # Section from a friend's server carries a server-specific access_token
        friend_token = "friend-server-token"
        mock_list_sections.return_value = [
            {
                "id": "5",
                "machine_identifier": "friend-machine",
                "title": "Friend Movies",
                "type": "movie",
                "access_token": friend_token,
                "uri": "http://friend-plex",
            }
        ]
        mock_list_resources.return_value = [
            {
                "machine_identifier": "friend-machine",
                "access_token": friend_token,
                "connections": [{"uri": "http://friend-plex"}],
            }
        ]
        mock_fetch_history.return_value = ([], 0)

        plex.importer("friend-machine::5", self.user, "new")

        # The token passed to fetch_history must be the friend-server token
        calls = mock_fetch_history.call_args_list
        self.assertTrue(len(calls) >= 1, "fetch_history should have been called")
        for call in calls:
            token_used = call[0][0]
            self.assertEqual(
                token_used,
                friend_token,
                f"Expected friend-server token, got '{token_used}'",
            )

    # ------------------------------------------------------------------
    # Test 2: auth failure on one section becomes a warning, not a crash
    # ------------------------------------------------------------------
    @patch("integrations.imports.plex.plex_api.fetch_section_all_items")
    @patch("integrations.imports.plex.plex_api.fetch_metadata")
    @patch("integrations.imports.plex.plex_api.list_users")
    @patch("integrations.imports.plex.plex_api.fetch_history")
    @patch("integrations.imports.plex.plex_api.list_resources")
    @patch("integrations.imports.plex.plex_api.list_sections")
    @patch("integrations.imports.plex.plex_api.fetch_account")
    def test_section_auth_failure_becomes_warning_not_exception(
        self,
        mock_fetch_account,
        mock_list_sections,
        mock_list_resources,
        mock_fetch_history,
        mock_list_users,
        mock_fetch_metadata,
        mock_fetch_section_items,
    ):
        """A PlexAuthError from a single section must not abort the whole import."""
        from integrations.plex import PlexAuthError

        mock_fetch_account.return_value = {"id": "9999"}
        mock_list_users.return_value = []
        mock_fetch_metadata.return_value = None
        mock_fetch_section_items.return_value = ([], 0)

        mock_list_sections.return_value = [
            {
                "id": "1",
                "machine_identifier": "my-machine",
                "title": "My Movies",
                "type": "movie",
                "access_token": "my-token",
                "server_name": "My Server",
                "uri": "http://my-plex",
            },
            {
                "id": "2",
                "machine_identifier": "friend-machine",
                "title": "Friend TV",
                "type": "show",
                "access_token": "bad-friend-token",
                "server_name": "Friend Server",
                "uri": "http://friend-plex",
            },
        ]
        mock_list_resources.return_value = [
            {
                "machine_identifier": "my-machine",
                "access_token": "my-token",
                "connections": [{"uri": "http://my-plex"}],
            },
            {
                "machine_identifier": "friend-machine",
                "access_token": "bad-friend-token",
                "connections": [{"uri": "http://friend-plex"}],
            },
        ]

        def history_side_effect(token, uri, *args, **kwargs):
            if "friend" in uri:
                raise PlexAuthError("token rejected by friend server")
            return ([], 0)

        mock_fetch_history.side_effect = history_side_effect

        # Should not raise — auth failure on one section must become a warning
        counts, warnings = plex.importer("all", self.user, "new")

        self.assertIn("Friend TV", warnings)
        self.assertIn("Friend Server", warnings)


class TestOverwriteMetadataFailureSafety(TestCase):
    """Regression tests for issue #252: overwrite import must not permanently delete media
    when TMDB metadata is unavailable (404) or when the import aborts mid-run."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="testuser")
        self.account = PlexAccount.objects.create(
            user=self.user,
            plex_token="token",
            plex_username="testuser",
            plex_account_id="111",
        )
        self.user.plex_usernames = "testuser"
        self.user.save()

    def _make_base_patches(self):
        """Return common patcher objects used by every test in this class."""
        return [
            patch("integrations.imports.plex.plex_api.fetch_account", return_value={"id": "111"}),
            patch("integrations.imports.plex.plex_api.list_users", return_value=[]),
            patch("integrations.imports.plex.plex_api.fetch_metadata", return_value=None),
            patch("integrations.imports.plex.plex_api.fetch_section_all_items", return_value=([], 0)),
            patch("integrations.imports.plex.plex_api.list_sections", return_value=[
                {"id": "1", "machine_identifier": "m", "title": "TV", "type": "show"},
            ]),
            patch("integrations.imports.plex.plex_api.list_resources", return_value=[
                {"machine_identifier": "m", "connections": [{"uri": "http://plex"}]},
            ]),
        ]

    def test_tv_show_preserved_when_tmdb_returns_404_in_overwrite(self):
        """A TV show that TMDB can no longer resolve (404) must survive an overwrite import."""
        # Pre-create the show in Yamtrack as if previously imported
        item = Item.objects.create(
            media_id="9999",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="For All Mankind",
        )
        tv = TV.objects.create(user=self.user, item=item, status=Status.IN_PROGRESS.value)

        episode_history_entry = {
            "type": "episode",
            "grandparentTitle": "For All Mankind",
            "parentIndex": 1,
            "index": 1,
            "guid": "tmdb://9999",
            "viewedAt": 1700000000,
            "accountID": "111",
        }

        def metadata_side_effect(media_type, media_id, source, **kwargs):
            # Simulate TMDB returning 404 for this show
            from app.providers.services import ProviderAPIError
            err = ProviderAPIError("tmdb", Exception("Not Found"))
            err.status_code = 404
            raise err

        patches = self._make_base_patches() + [
            patch("integrations.imports.plex.plex_api.fetch_history", return_value=([episode_history_entry], 1)),
            patch("integrations.imports.plex.services.get_media_metadata", side_effect=metadata_side_effect),
            patch("integrations.imports.plex.services.search", return_value={"results": []}),
        ]
        for p in patches:
            p.start()
        try:
            plex.importer("m::1", self.user, "overwrite")
        finally:
            for p in patches:
                p.stop()

        # The show must still exist — it could not be re-created so it should not have been deleted
        self.assertTrue(
            TV.objects.filter(user=self.user, item__media_id="9999").exists(),
            "TV show was deleted during overwrite import despite TMDB 404 — data loss bug",
        )

    def test_tv_show_preserved_when_tmdb_raises_during_metadata_warm(self):
        """If TMDB raises a non-404 error during metadata warm-up, the import must abort
        before deleting any existing records."""
        item = Item.objects.create(
            media_id="8888",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="For All Mankind",
        )
        tv = TV.objects.create(user=self.user, item=item, status=Status.IN_PROGRESS.value)

        episode_history_entry = {
            "type": "episode",
            "grandparentTitle": "For All Mankind",
            "parentIndex": 1,
            "index": 1,
            "guid": "tmdb://8888",
            "viewedAt": 1700000000,
            "accountID": "111",
        }

        call_count = {"n": 0}

        def metadata_side_effect(media_type, media_id, source, **kwargs):
            call_count["n"] += 1
            from app.providers.services import ProviderAPIError
            err = ProviderAPIError("tmdb", Exception("Rate limit exceeded"))
            err.status_code = 429
            raise err

        patches = self._make_base_patches() + [
            patch("integrations.imports.plex.plex_api.fetch_history", return_value=([episode_history_entry], 1)),
            patch("integrations.imports.plex.services.get_media_metadata", side_effect=metadata_side_effect),
            patch("integrations.imports.plex.services.search", return_value={"results": []}),
        ]
        started = [p.start() for p in patches]
        try:
            with self.assertRaises(Exception):
                plex.importer("m::1", self.user, "overwrite")
        finally:
            for p in patches:
                p.stop()

        # The show must survive — the import should have aborted before cleanup
        self.assertTrue(
            TV.objects.filter(user=self.user, item__media_id="8888").exists(),
            "TV show was deleted before TMDB error propagated — delete happened too early",
        )
