from unittest.mock import patch

from django.test import TestCase

from app.models import Item, MediaTypes, Sources
from app.providers import trakt
from app.services import trakt_popularity


class TraktProviderTests(TestCase):
    @patch("app.providers.trakt.services.api_request")
    def test_lookup_by_external_id_normalizes_movie_payload(self, mock_api_request):
        mock_api_request.return_value = [
            {
                "type": "movie",
                "movie": {
                    "title": "Deadpool",
                    "rating": 8.0,
                    "votes": 1200000,
                    "ids": {
                        "trakt": 1,
                        "slug": "deadpool",
                        "imdb": "tt1431045",
                        "tmdb": 293660,
                    },
                },
            },
        ]

        payload = trakt.lookup_by_external_id(
            "tmdb",
            "293660",
            media_type=MediaTypes.MOVIE.value,
        )

        self.assertEqual(payload["rating"], 8.0)
        self.assertEqual(payload["votes"], 1200000)
        self.assertEqual(payload["matched_id_type"], "tmdb")
        self.assertEqual(payload["trakt_ids"]["tmdb"], "293660")
        self.assertEqual(payload["trakt_ids"]["imdb"], "tt1431045")

    @patch("app.providers.trakt.services.api_request")
    def test_lookup_by_external_id_normalizes_show_payload(self, mock_api_request):
        mock_api_request.return_value = [
            {
                "type": "show",
                "show": {
                    "title": "Breaking Bad",
                    "rating": 9.0,
                    "votes": 500000,
                    "ids": {
                        "trakt": 2,
                        "slug": "breaking-bad",
                        "tvdb": 81189,
                        "imdb": "tt0903747",
                    },
                },
            },
        ]

        payload = trakt.lookup_by_external_id(
            "tvdb",
            "81189",
            media_type=MediaTypes.TV.value,
        )

        self.assertEqual(payload["rating"], 9.0)
        self.assertEqual(payload["votes"], 500000)
        self.assertEqual(payload["matched_id_type"], "tvdb")
        self.assertEqual(payload["trakt_ids"]["tvdb"], "81189")
        self.assertEqual(payload["trakt_ids"]["imdb"], "tt0903747")

    @patch("app.providers.trakt.services.api_request")
    def test_lookup_by_external_id_normalizes_season_payload(self, mock_api_request):
        mock_api_request.side_effect = [
            [
                {
                    "type": "show",
                    "show": {
                        "title": "The Gilded Age",
                        "rating": 8.1,
                        "votes": 12345,
                        "ids": {
                            "trakt": 152334,
                            "slug": "the-gilded-age",
                            "tmdb": 81723,
                            "tvdb": 384696,
                        },
                    },
                },
            ],
            [
                {
                    "number": 1,
                    "rating": 7.88048,
                    "votes": 1849,
                    "ids": {
                        "trakt": 998877,
                        "tvdb": 1978241,
                    },
                },
            ],
        ]

        payload = trakt.lookup_by_external_id(
            "tmdb",
            "81723",
            media_type=MediaTypes.SEASON.value,
            season_number=1,
        )

        self.assertEqual(payload["rating"], 7.88048)
        self.assertEqual(payload["votes"], 1849)
        self.assertEqual(payload["season_number"], 1)
        self.assertEqual(payload["trakt_ids"]["tmdb"], "81723")
        self.assertEqual(payload["trakt_ids"]["tvdb"], "1978241")
        self.assertEqual(mock_api_request.call_count, 2)


class TraktPopularityServiceTests(TestCase):
    @patch("app.services.trakt_popularity.trakt_provider.lookup_by_external_id")
    @patch("app.services.trakt_popularity.metadata_resolution.resolve_provider_media_id")
    def test_mal_anime_lookup_falls_back_to_resolved_tmdb_id(
        self,
        mock_resolve_provider_media_id,
        mock_lookup_by_external_id,
    ):
        item = Item.objects.create(
            media_id="52991",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Frieren",
            image="https://example.com/frieren.jpg",
        )
        mock_resolve_provider_media_id.side_effect = ["1396", None]
        mock_lookup_by_external_id.return_value = {
            "rating": 8.9,
            "votes": 123456,
            "trakt_ids": {"tmdb": "1396"},
            "matched_id_type": "tmdb",
        }

        payload = trakt_popularity.lookup_item_summary(
            item,
            route_media_type=MediaTypes.ANIME.value,
        )

        self.assertEqual(payload["matched_id_type"], "tmdb")
        mock_resolve_provider_media_id.assert_any_call(
            item,
            Sources.TMDB.value,
            route_media_type=MediaTypes.ANIME.value,
        )
        mock_lookup_by_external_id.assert_called_once_with(
            "tmdb",
            "1396",
            media_type=MediaTypes.ANIME.value,
            season_number=None,
        )

    def test_compute_popularity_score_is_deterministic(self):
        score = trakt_popularity.compute_popularity_score(8.0, 1200000)

        self.assertIsNotNone(score)
        self.assertAlmostEqual(score, 17793.4652, places=4)

    def test_estimate_rank_from_score_tracks_fixture_distribution(self):
        metrics = trakt_popularity.evaluate_calibration_fixture()
        ranked_items = sorted(metrics["items"], key=lambda item: item["predicted_rank"])
        top_score = ranked_items[0]["score"]
        bottom_score = ranked_items[-1]["score"]

        self.assertEqual(trakt_popularity.estimate_rank_from_score(top_score), 1)
        self.assertEqual(
            trakt_popularity.estimate_rank_from_score(bottom_score),
            metrics["count"],
        )

    def test_calibration_fixture_regression_stays_within_expected_tolerance(self):
        metrics = trakt_popularity.evaluate_calibration_fixture()

        self.assertEqual(metrics["count"], 202)
        # Trakt's ordering reflects internal engagement data (not just rating×votes),
        # so some divergence from our formula is expected — especially for MCU films
        # ranked above higher-rated classics.  These thresholds guard against gross
        # regressions while leaving room for the unavoidable systematic gap.
        self.assertLessEqual(metrics["mae"], 42.0)
        self.assertLessEqual(metrics["max_abs_error"], 170)
        self.assertGreaterEqual(metrics["top_ten_overlap"], 5)

    @patch("app.services.trakt_popularity.lookup_item_summary")
    def test_refresh_trakt_popularity_persists_fields(self, mock_lookup_item_summary):
        item = Item.objects.create(
            media_id="293660",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Deadpool",
            image="https://example.com/deadpool.jpg",
            provider_external_ids={"imdb_id": "tt1431045"},
        )
        mock_lookup_item_summary.return_value = {
            "rating": 8.0,
            "votes": 1200000,
            "trakt_ids": {
                "tmdb": "293660",
                "imdb": "tt1431045",
            },
            "matched_id_type": "tmdb",
            "matched_lookup_value": "293660",
        }

        result = trakt_popularity.refresh_trakt_popularity(
            item,
            route_media_type=MediaTypes.MOVIE.value,
            force=True,
        )

        item.refresh_from_db()
        self.assertEqual(item.trakt_rating, 8.0)
        self.assertEqual(item.trakt_rating_count, 1200000)
        self.assertIsNotNone(item.trakt_popularity_score)
        self.assertEqual(item.trakt_popularity_rank, result["rank"])
        self.assertIsNotNone(item.trakt_popularity_fetched_at)

    @patch("app.services.trakt_popularity.lookup_item_summary")
    def test_refresh_trakt_popularity_persists_season_fields(self, mock_lookup_item_summary):
        item = Item.objects.create(
            media_id="81723",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="The Gilded Age Season 1",
            image="https://example.com/season.jpg",
            season_number=1,
        )
        mock_lookup_item_summary.return_value = {
            "rating": 7.88048,
            "votes": 1849,
            "trakt_ids": {
                "trakt": "998877",
                "tmdb": "81723",
            },
            "matched_id_type": "tmdb",
            "matched_lookup_value": "81723",
        }

        result = trakt_popularity.refresh_trakt_popularity(
            item,
            route_media_type=MediaTypes.SEASON.value,
            force=True,
        )

        item.refresh_from_db()
        self.assertEqual(item.trakt_rating, 7.88048)
        self.assertEqual(item.trakt_rating_count, 1849)
        self.assertIsNotNone(item.trakt_popularity_score)
        self.assertEqual(item.trakt_popularity_rank, result["rank"])
        self.assertIsNotNone(item.trakt_popularity_fetched_at)
