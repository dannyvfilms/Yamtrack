# ruff: noqa: D102, D101

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from app.discover import capabilities, provider_candidates, tabs
from app.discover.provider_candidates import (
    _current_anime_season,
    _mal_anime_ranking_candidates,
    _mal_anime_season_candidates,
    _previous_anime_season,
)
from app.discover.schemas import CandidateItem, RowResult
from app.discover.tabs import TAB_REGISTRY


class TabRegistryTests(TestCase):
    """The tab registry stays consistent with the dispatch and providers."""

    def test_every_media_type_defaults_to_trending(self):
        for media_type in TAB_REGISTRY:
            self.assertEqual(tabs.default_tab(media_type), "trending")

    def test_get_tab_returns_definition(self):
        tab = tabs.get_tab("anime", "this_season")
        self.assertIsNotNone(tab)
        self.assertEqual(tab.row_key, "mal_this_season")
        self.assertIsNone(tabs.get_tab("anime", "nope"))

    @patch("app.discover.provider_candidates._api_cached_results", return_value=[])
    @patch("app.discover.provider_candidates.TRAKT_ADAPTER")
    @patch("app.discover.provider_candidates.TMDB_ADAPTER")
    def test_new_tab_row_keys_dispatch_to_a_builder(
        self,
        mock_tmdb,
        mock_trakt,
        _mock_cache,
    ):
        # Tab-only row keys (not the legacy registry keys) must resolve in the
        # tab dispatcher rather than silently returning nothing.
        for method in ("trending", "top_rated", "current_cycle", "airing_today"):
            getattr(mock_tmdb, method).return_value = []
        mock_trakt.movie_boxoffice.return_value = []
        legacy = {"trending_right_now", "all_time_greats_unseen", "coming_soon"}
        for media_type, tab_list in TAB_REGISTRY.items():
            for tab in tab_list:
                if tab.row_key in legacy:
                    continue
                result = provider_candidates._tab_row_candidates(
                    media_type,
                    tab.row_key,
                )
                self.assertIsInstance(
                    result,
                    list,
                    msg=f"{media_type}/{tab.row_key} did not dispatch to a builder",
                )


class AnimeSeasonHelperTests(TestCase):
    def test_current_and_previous_season(self):
        # 2026-06-22 -> spring; previous -> winter.
        self.assertEqual(_current_anime_season(), (2026, "spring"))
        self.assertEqual(_previous_anime_season(2026, "spring"), (2026, "winter"))
        self.assertEqual(_previous_anime_season(2026, "winter"), (2025, "fall"))


class MalAnimeBuilderTests(TestCase):
    NODE = {
        "id": 5114,
        "title": "Fullmetal Alchemist: Brotherhood",
        "main_picture": {"medium": "http://img/m.jpg", "large": "http://img/l.jpg"},
        "mean": 9.1,
        "num_scoring_users": 2000,
        "num_list_users": 3000,
        "genres": [{"id": 1, "name": "Action"}],
        "start_date": "2009-04-05",
    }

    @patch("app.discover.provider_candidates.services.api_request")
    def test_ranking_builder_normalizes_nodes(self, mock_api_request):
        mock_api_request.return_value = {
            "data": [{"node": self.NODE, "ranking": {"rank": 1}}],
        }
        candidates = _mal_anime_ranking_candidates(
            ranking_type="all",
            row_key="mal_anime_top_rated",
            source_reason="MAL ranking",
        )
        self.assertEqual(len(candidates), 1)
        item = candidates[0]
        self.assertEqual(item.media_id, "5114")
        self.assertEqual(item.media_type, "anime")
        self.assertEqual(item.source, "mal")
        self.assertEqual(item.rating, 9.1)
        self.assertEqual(item.row_key, "mal_anime_top_rated")
        self.assertIn("Action", item.genres)

    @patch("app.discover.provider_candidates.services.api_request")
    def test_season_builder_uses_member_count_for_popularity(self, mock_api_request):
        mock_api_request.return_value = {"data": [{"node": self.NODE}]}
        candidates = _mal_anime_season_candidates(
            year=2026,
            season="spring",
            row_key="mal_this_season",
            source_reason="MAL current season",
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].popularity, 3000.0)
        self.assertEqual(candidates[0].row_key, "mal_this_season")


class TabAvailabilityTests(TestCase):
    @override_settings(MAL_API="real-key")
    def test_mal_backed_tabs_enabled_when_key_present(self):
        availability = capabilities.tab_availability("anime")
        self.assertTrue(availability["this_season"]["enabled"])
        self.assertIsNone(availability["this_season"]["tooltip"])

    @override_settings(LASTFM_API_KEY="")
    def test_lastfm_tabs_disabled_without_key(self):
        availability = capabilities.tab_availability("music")
        self.assertFalse(availability["trending"]["enabled"])
        self.assertIn("LASTFM_API_KEY", availability["trending"]["tooltip"])
        # MusicBrainz-backed tab needs no key and stays enabled.
        self.assertTrue(availability["coming_soon"]["enabled"])

    @override_settings(IGDB_ID="id", IGDB_SECRET="")
    def test_igdb_needs_both_id_and_secret(self):
        availability = capabilities.tab_availability("game")
        self.assertFalse(availability["top_rated"]["enabled"])


class DiscoverTabViewTests(TestCase):
    def setUp(self):
        self.credentials = {"username": "tab-user", "password": "secret123"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.warmup_patcher = patch(
            "app.middleware.discover_tab_cache.maybe_schedule_user_warmup",
            return_value=0,
        )
        self.warmup_patcher.start()
        self.client.login(**self.credentials)

    def tearDown(self):
        self.warmup_patcher.stop()

    def _row(self, row_key="mal_anime_top_rated"):
        return RowResult(
            key=row_key,
            title="Top Rated",
            mission="",
            why="The highest rated anime of all time.",
            source="mal",
            items=[
                CandidateItem(
                    media_type="anime",
                    source="mal",
                    media_id="5114",
                    title="Fullmetal Alchemist: Brotherhood",
                ),
            ],
        )

    @override_settings(MAL_API="real-key")
    @patch("app.discover_views.discover.get_discover_tab_row")
    def test_enabled_tab_returns_row_fragment(self, mock_get_tab_row):
        mock_get_tab_row.return_value = self._row()
        response = self.client.get(
            reverse("discover_tab"),
            {"media_type": "anime", "tab": "top_rated"},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/discover_row.html")
        self.assertContains(response, "Fullmetal Alchemist: Brotherhood")
        mock_get_tab_row.assert_called_once()

    def test_unknown_tab_is_rejected(self):
        response = self.client.get(
            reverse("discover_tab"),
            {"media_type": "anime", "tab": "does-not-exist"},
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(LASTFM_API_KEY="")
    @patch("app.discover_views.discover.get_discover_tab_row")
    def test_disabled_tab_is_rejected(self, mock_get_tab_row):
        response = self.client.get(
            reverse("discover_tab"),
            {"media_type": "music", "tab": "top_artists"},
        )
        self.assertEqual(response.status_code, 400)
        mock_get_tab_row.assert_not_called()
