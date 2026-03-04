from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.discover import cache_repo
from app.discover.schemas import CandidateItem
from app.discover.service import (
    _apply_comfort_confidence,
    _apply_wildcard_novelty,
    _top_picks_candidates,
    get_discover_rows,
)
from app.models import MediaTypes, Status


class DiscoverServiceTests(TestCase):
    """Service-level Discover tests."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="discover-service-user",
            password="testpass",
        )

    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly", side_effect=RuntimeError("trakt down"))
    def test_row_failure_isolated_to_single_row(
        self,
        _mock_trending,
        _mock_popular,
        _mock_anticipated,
        _mock_current_cycle,
        _mock_upcoming,
        _mock_top_rated,
    ):
        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        self.assertIsInstance(rows, list)
        self.assertNotIn("trending_right_now", [row.key for row in rows])

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_trending_row_rebuilds_when_cached_source_changes(
        self,
        mock_trending,
        _mock_popular,
        _mock_anticipated,
        _mock_current_cycle,
        _mock_upcoming,
        _mock_top_rated,
        _mock_profile,
    ):
        stale_payload = {
            "key": "trending_right_now",
            "title": "Trending Right Now",
            "mission": "Cultural Moment",
            "why": "What everyone is watching",
            "source": "tmdb",
            "items": [
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source="tmdb",
                    media_id="1",
                    title="Old Cached Item",
                ).to_dict(),
            ],
            "is_stale": False,
            "show_more": False,
            "source_state": "cache",
        }
        cache_repo.set_row_cache(
            self.user.id,
            MediaTypes.MOVIE.value,
            "trending_right_now",
            stale_payload,
            ttl_seconds=3600,
        )

        mock_trending.return_value = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="101",
                title="Fresh One",
                image="https://example.com/101.jpg",
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="102",
                title="Fresh Two",
                image="https://example.com/102.jpg",
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="103",
                title="Fresh Three",
                image="https://example.com/103.jpg",
            ),
        ]

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        trending_row = next(row for row in rows if row.key == "trending_right_now")

        mock_trending.assert_called_once_with(limit=100)
        self.assertEqual(trending_row.source, "trakt")
        self.assertEqual(trending_row.why, "What everyone has been watching this week.")
        self.assertEqual([item.title for item in trending_row.items], ["Fresh One", "Fresh Two", "Fresh Three"])

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.get_tracked_keys_by_media_type")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_trending_row_overfetches_filters_and_caps_to_twelve(
        self,
        mock_trending,
        _mock_popular,
        _mock_anticipated,
        mock_tracked_keys,
        _mock_current_cycle,
        _mock_upcoming,
        _mock_top_rated,
        _mock_profile,
    ):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id=str(index),
                title=f"Movie {index}",
                image=f"https://example.com/{index}.jpg",
            )
            for index in range(1, 21)
        ]
        mock_trending.return_value = candidates

        blocked = {
            (MediaTypes.MOVIE.value, "tmdb", "1"),
            (MediaTypes.MOVIE.value, "tmdb", "2"),
            (MediaTypes.MOVIE.value, "tmdb", "3"),
        }

        def tracked_side_effect(user, media_type, statuses=None):
            if statuses == {
                Status.COMPLETED.value,
                Status.DROPPED.value,
                Status.PLANNING.value,
            }:
                return blocked
            return set()

        mock_tracked_keys.side_effect = tracked_side_effect

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        trending_row = next(row for row in rows if row.key == "trending_right_now")

        mock_trending.assert_called_once_with(limit=100)
        self.assertEqual(len(trending_row.items), 12)
        self.assertTrue(all(item.media_id not in {"1", "2", "3"} for item in trending_row.items))

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.services.get_media_metadata")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_trending_row_hydrates_missing_artwork_from_tmdb(
        self,
        mock_trending,
        _mock_popular,
        _mock_anticipated,
        mock_get_metadata,
        _mock_current_cycle,
        _mock_upcoming,
        _mock_top_rated,
        _mock_profile,
    ):
        mock_trending.return_value = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="201",
                title="Needs Art One",
                image=None,
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="202",
                title="Needs Art Two",
                image=None,
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="203",
                title="Needs Art Three",
                image=None,
            ),
        ]

        def metadata_side_effect(media_type, media_id, source, season_numbers=None, episode_number=None):
            return {"image": f"https://image.tmdb.org/t/p/w500/{media_id}.jpg"}

        mock_get_metadata.side_effect = metadata_side_effect

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        trending_row = next(row for row in rows if row.key == "trending_right_now")

        self.assertEqual(
            [item.image for item in trending_row.items],
            [
                "https://image.tmdb.org/t/p/w500/201.jpg",
                "https://image.tmdb.org/t/p/w500/202.jpg",
                "https://image.tmdb.org/t/p/w500/203.jpg",
            ],
        )

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_trending_row_rebuilds_cached_payload_with_missing_artwork(
        self,
        mock_trending,
        _mock_popular,
        _mock_anticipated,
        _mock_current_cycle,
        _mock_upcoming,
        _mock_top_rated,
        _mock_profile,
    ):
        stale_art_payload = {
            "key": "trending_right_now",
            "title": "Trending Right Now",
            "mission": "Cultural Moment",
            "why": "What everyone has been watching this week.",
            "source": "trakt",
            "items": [
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source="tmdb",
                    media_id="501",
                    title="Old Missing Art",
                    image="https://www.themoviedb.org/assets/2/v4/glyphicons/basic/glyphicons-basic-38-picture-grey-c2ebdbb057f2a7614185931650f8cee23fa137b93812ccb132b9df511df1cfac.svg",
                ).to_dict(),
            ],
            "is_stale": False,
            "show_more": False,
            "source_state": "cache",
        }
        cache_repo.set_row_cache(
            self.user.id,
            MediaTypes.MOVIE.value,
            "trending_right_now",
            stale_art_payload,
            ttl_seconds=3600,
        )

        mock_trending.return_value = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="601",
                title="Fresh With Art",
                image="https://example.com/fresh-with-art.jpg",
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="602",
                title="Fresh With Art 2",
                image="https://example.com/fresh-with-art-2.jpg",
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="603",
                title="Fresh With Art 3",
                image="https://example.com/fresh-with-art-3.jpg",
            ),
        ]

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        trending_row = next(row for row in rows if row.key == "trending_right_now")

        mock_trending.assert_called_once_with(limit=100)
        self.assertEqual(
            [item.title for item in trending_row.items],
            ["Fresh With Art", "Fresh With Art 2", "Fresh With Art 3"],
        )

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.get_tracked_keys_by_media_type")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_all_time_greats_unseen_expands_popular_fetch_and_persists_pull_hint(
        self,
        mock_trending,
        mock_popular,
        _mock_anticipated,
        mock_tracked_keys,
        _mock_current_cycle,
        _mock_upcoming,
        _mock_top_rated,
        _mock_profile,
    ):
        def build_candidate(index: int) -> CandidateItem:
            return CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id=str(index),
                title=f"Movie {index}",
                image=f"https://example.com/{index}.jpg",
            )

        page_one = [build_candidate(index) for index in range(1, 101)]
        page_two = [build_candidate(index) for index in range(101, 201)]
        mock_trending.return_value = [build_candidate(89)]

        def popular_side_effect(page, limit):
            if page == 1:
                return page_one[:limit]
            if page == 2:
                return page_two[:limit]
            return []

        mock_popular.side_effect = popular_side_effect

        blocked = {
            (MediaTypes.MOVIE.value, "tmdb", str(index))
            for index in range(1, 89)
        }

        def tracked_side_effect(user, media_type, statuses=None):
            if media_type == MediaTypes.MOVIE.value and statuses == {
                Status.COMPLETED.value,
                Status.DROPPED.value,
                Status.PLANNING.value,
            }:
                return blocked
            return set()

        mock_tracked_keys.side_effect = tracked_side_effect

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        canon_row = next(row for row in rows if row.key == "all_time_greats_unseen")

        self.assertEqual(mock_popular.call_count, 2)
        self.assertEqual(mock_popular.call_args_list[0].kwargs, {"page": 1, "limit": 100})
        self.assertEqual(mock_popular.call_args_list[1].kwargs, {"page": 2, "limit": 100})
        self.assertEqual(len(canon_row.items), 12)
        self.assertTrue(all(item.media_id not in {str(index) for index in range(1, 89)} for item in canon_row.items))
        self.assertNotIn("89", {item.media_id for item in canon_row.items})

        cached_payload, _ = cache_repo.get_row_cache(
            self.user.id,
            MediaTypes.MOVIE.value,
            "all_time_greats_unseen",
        )
        self.assertIsNotNone(cached_payload)
        self.assertEqual(cached_payload.get("meta", {}).get("adaptive_pull_target"), 200)

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.get_tracked_keys_by_media_type", return_value=set())
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_all_time_greats_unseen_refill_keeps_existing_items_when_cached_overlap(
        self,
        mock_trending,
        mock_popular,
        _mock_anticipated,
        _mock_tracked_keys,
        _mock_current_cycle,
        _mock_upcoming,
        _mock_top_rated,
        _mock_profile,
    ):
        def build_candidate(index: int) -> CandidateItem:
            return CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id=str(index),
                title=f"Movie {index}",
                image=f"https://example.com/{index}.jpg",
            )

        mock_trending.return_value = [
            build_candidate(89),
            build_candidate(5000),
            build_candidate(5001),
        ]
        mock_popular.return_value = [build_candidate(index) for index in range(89, 189)]

        cached_payload = {
            "key": "all_time_greats_unseen",
            "title": "All-Time Greats You Haven't Seen",
            "mission": "Canon",
            "why": "Must-watch classics still missing",
            "source": "trakt",
            "items": [build_candidate(index).to_dict() for index in range(89, 101)],
            "is_stale": False,
            "show_more": False,
            "source_state": "cache",
            "meta": {"adaptive_pull_target": 100},
        }
        cache_repo.set_row_cache(
            self.user.id,
            MediaTypes.MOVIE.value,
            "all_time_greats_unseen",
            cached_payload,
            ttl_seconds=3600,
        )

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        canon_row = next(row for row in rows if row.key == "all_time_greats_unseen")
        canon_ids = {item.media_id for item in canon_row.items}

        self.assertEqual(len(canon_row.items), 12)
        self.assertNotIn("89", canon_ids)
        self.assertIn("90", canon_ids)
        self.assertIn("100", canon_ids)
        self.assertIn("101", canon_ids)

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly", return_value=[])
    def test_all_time_greats_unseen_rebuilds_when_cached_schema_is_old(
        self,
        _mock_trending,
        mock_popular,
        _mock_anticipated,
        _mock_current_cycle,
        _mock_upcoming,
        _mock_top_rated,
        _mock_profile,
    ):
        stale_payload = {
            "key": "all_time_greats_unseen",
            "title": "All-Time Greats You Haven't Seen",
            "mission": "Canon",
            "why": "Must-watch classics still missing",
            "source": "trakt",
            "items": [
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source="tmdb",
                    media_id="999",
                    title="Old Cached Canon",
                ).to_dict(),
            ],
            "is_stale": False,
            "show_more": False,
            "source_state": "cache",
            "meta": {"adaptive_pull_target": 100, "schema_version": 1},
        }
        cache_repo.set_row_cache(
            self.user.id,
            MediaTypes.MOVIE.value,
            "all_time_greats_unseen",
            stale_payload,
            ttl_seconds=3600,
        )

        mock_popular.return_value = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="1001",
                title="Fresh Canon One",
                image="https://example.com/1001.jpg",
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="1002",
                title="Fresh Canon Two",
                image="https://example.com/1002.jpg",
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="1003",
                title="Fresh Canon Three",
                image="https://example.com/1003.jpg",
            ),
        ]

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        canon_row = next(row for row in rows if row.key == "all_time_greats_unseen")

        self.assertNotEqual([item.title for item in canon_row.items], ["Old Cached Canon"])
        self.assertGreaterEqual(mock_popular.call_count, 1)

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly", return_value=[])
    @patch("app.discover.service._comfort_candidates")
    def test_comfort_rewatches_rebuilds_when_cached_schema_is_old(
        self,
        mock_comfort_candidates,
        _mock_trending,
        _mock_popular,
        _mock_anticipated,
        _mock_current_cycle,
        _mock_upcoming,
        _mock_top_rated,
        _mock_profile,
    ):
        stale_payload = {
            "key": "comfort_rewatches",
            "title": "Comfort Rewatches",
            "mission": "Comfort",
            "why": "Favorites you loved, ready for a revisit.",
            "source": "local",
            "items": [
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source="tmdb",
                    media_id="old-1",
                    title="Old Comfort",
                    final_score=0.2,
                ).to_dict(),
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source="tmdb",
                    media_id="old-2",
                    title="Old Comfort 2",
                    final_score=0.2,
                ).to_dict(),
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source="tmdb",
                    media_id="old-3",
                    title="Old Comfort 3",
                    final_score=0.2,
                ).to_dict(),
            ],
            "is_stale": False,
            "show_more": False,
            "source_state": "cache",
        }
        cache_repo.set_row_cache(
            self.user.id,
            MediaTypes.MOVIE.value,
            "comfort_rewatches",
            stale_payload,
            ttl_seconds=3600,
        )

        mock_comfort_candidates.return_value = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="new-1",
                title="Fresh Comfort One",
                final_score=0.4,
                score_breakdown={"user_score": 9.5, "days_since_activity": 300.0},
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="new-2",
                title="Fresh Comfort Two",
                final_score=0.35,
                score_breakdown={"user_score": 9.0, "days_since_activity": 220.0},
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="new-3",
                title="Fresh Comfort Three",
                final_score=0.33,
                score_breakdown={"user_score": 8.5, "days_since_activity": 180.0},
            ),
        ]

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        comfort_row = next(row for row in rows if row.key == "comfort_rewatches")

        self.assertEqual(
            [item.title for item in comfort_row.items[:3]],
            ["Fresh Comfort One", "Fresh Comfort Two", "Fresh Comfort Three"],
        )
        self.assertNotIn("Old Comfort", [item.title for item in comfort_row.items])
        self.assertGreaterEqual(mock_comfort_candidates.call_count, 1)

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_coming_soon_row_uses_trakt_anticipated_in_third_slot(
        self,
        mock_trending,
        mock_popular,
        mock_anticipated,
        _mock_current_cycle,
        _mock_upcoming,
        _mock_top_rated,
        _mock_profile,
    ):
        def build_candidate(media_id: int, title: str) -> CandidateItem:
            return CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id=str(media_id),
                title=title,
                image=f"https://example.com/{media_id}.jpg",
            )

        mock_trending.return_value = [
            build_candidate(10001, "Trending One"),
            build_candidate(10002, "Trending Two"),
            build_candidate(10003, "Trending Three"),
        ]
        mock_popular.return_value = [
            build_candidate(20001, "Popular One"),
            build_candidate(20002, "Popular Two"),
            build_candidate(20003, "Popular Three"),
        ]
        mock_anticipated.return_value = [
            build_candidate(30001, "Upcoming One"),
            build_candidate(30002, "Upcoming Two"),
            build_candidate(30003, "Upcoming Three"),
        ]

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        keys = [row.key for row in rows]
        coming_soon_row = next(row for row in rows if row.key == "coming_soon")

        self.assertEqual(keys[:3], ["trending_right_now", "all_time_greats_unseen", "coming_soon"])
        mock_anticipated.assert_called_once_with(page=1, limit=100)
        self.assertEqual(coming_soon_row.source, "trakt")
        self.assertEqual(
            [item.title for item in coming_soon_row.items],
            ["Upcoming One", "Upcoming Two", "Upcoming Three"],
        )

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service._wildcard_candidates")
    @patch("app.discover.service._comfort_candidates")
    @patch("app.discover.service._top_picks_candidates")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_movie_rows_render_exactly_six_in_expected_order(
        self,
        mock_trending,
        mock_popular,
        mock_anticipated,
        mock_top_picks,
        mock_comfort,
        mock_wildcard,
        _mock_profile,
    ):
        def candidate(media_id: str, title: str) -> CandidateItem:
            return CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id=media_id,
                title=title,
                image=f"https://example.com/{media_id}.jpg",
                final_score=0.9,
            )

        mock_trending.return_value = [candidate("100", "Trending 1"), candidate("101", "Trending 2"), candidate("102", "Trending 3")]
        mock_popular.return_value = [candidate("200", "Canon 1"), candidate("201", "Canon 2"), candidate("202", "Canon 3")]
        mock_anticipated.return_value = [candidate("300", "Soon 1"), candidate("301", "Soon 2"), candidate("302", "Soon 3")]
        mock_top_picks.return_value = [candidate("400", "Pick 1"), candidate("401", "Pick 2"), candidate("402", "Pick 3")]
        mock_comfort.return_value = [candidate("500", "Comfort 1"), candidate("501", "Comfort 2"), candidate("502", "Comfort 3")]
        mock_wildcard.return_value = [candidate("600", "Wild 1"), candidate("601", "Wild 2"), candidate("602", "Wild 3")]

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        self.assertEqual(
            [row.key for row in rows],
            [
                "trending_right_now",
                "all_time_greats_unseen",
                "coming_soon",
                "top_picks_for_you",
                "comfort_rewatches",
                "wildcard_for_you",
            ],
        )

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service._wildcard_candidates")
    @patch("app.discover.service._comfort_candidates", return_value=[])
    @patch("app.discover.service._top_picks_candidates")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_wildcard_row_backfills_after_dedupe_using_buffered_candidates(
        self,
        mock_trending,
        mock_popular,
        mock_anticipated,
        mock_top_picks,
        _mock_comfort,
        mock_wildcard,
        _mock_profile,
    ):
        def candidate(media_id: str, title: str) -> CandidateItem:
            return CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id=media_id,
                title=title,
                image=f"https://example.com/{media_id}.jpg",
                final_score=0.8,
            )

        mock_trending.return_value = [candidate("100", "Trending 1"), candidate("101", "Trending 2"), candidate("102", "Trending 3")]
        mock_popular.return_value = [candidate("200", "Canon 1"), candidate("201", "Canon 2"), candidate("202", "Canon 3")]
        mock_anticipated.return_value = [candidate("300", "Soon 1"), candidate("301", "Soon 2"), candidate("302", "Soon 3")]

        top_pick_candidates = [candidate(str(400 + index), f"Pick {index}") for index in range(12)]
        mock_top_picks.return_value = top_pick_candidates

        wildcard_candidates = list(top_pick_candidates)
        wildcard_candidates.extend(
            candidate(str(600 + index), f"Wild {index}") for index in range(12)
        )
        mock_wildcard.return_value = wildcard_candidates

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        wildcard_row = next(row for row in rows if row.key == "wildcard_for_you")

        self.assertEqual(len(wildcard_row.items), 12)
        self.assertTrue(all(item.media_id.startswith("6") for item in wildcard_row.items))

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service._wildcard_candidates", return_value=[])
    @patch("app.discover.service._comfort_candidates", return_value=[])
    @patch("app.discover.service._top_picks_candidates", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_movie_rows_hide_rows_four_to_six_when_no_personalized_data(
        self,
        mock_trending,
        mock_popular,
        mock_anticipated,
        _mock_top_picks,
        _mock_comfort,
        _mock_wildcard,
        _mock_profile,
    ):
        def candidate(media_id: str, title: str) -> CandidateItem:
            return CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id=media_id,
                title=title,
                image=f"https://example.com/{media_id}.jpg",
            )

        mock_trending.return_value = [candidate("100", "Trending 1"), candidate("101", "Trending 2"), candidate("102", "Trending 3")]
        mock_popular.return_value = [candidate("200", "Canon 1"), candidate("201", "Canon 2"), candidate("202", "Canon 3")]
        mock_anticipated.return_value = [candidate("300", "Soon 1"), candidate("301", "Soon 2"), candidate("302", "Soon 3")]

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        self.assertEqual(
            [row.key for row in rows],
            ["trending_right_now", "all_time_greats_unseen", "coming_soon"],
        )

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service._wildcard_candidates")
    @patch("app.discover.service._comfort_candidates")
    @patch("app.discover.service._top_picks_candidates")
    @patch("app.discover.service.get_tracked_keys_by_media_type")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_wildcard_excludes_completed_dropped_in_progress_but_keeps_planning(
        self,
        mock_trending,
        mock_popular,
        mock_anticipated,
        mock_tracked_keys,
        mock_top_picks,
        mock_comfort,
        mock_wildcard,
        _mock_profile,
    ):
        def candidate(media_id: str, title: str) -> CandidateItem:
            return CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id=media_id,
                title=title,
                image=f"https://example.com/{media_id}.jpg",
            )

        mock_trending.return_value = [candidate("100", "Trending 1"), candidate("101", "Trending 2"), candidate("102", "Trending 3")]
        mock_popular.return_value = [candidate("200", "Canon 1"), candidate("201", "Canon 2"), candidate("202", "Canon 3")]
        mock_anticipated.return_value = [candidate("300", "Soon 1"), candidate("301", "Soon 2"), candidate("302", "Soon 3")]
        mock_top_picks.return_value = [
            candidate("400", "Pick 1"),
            candidate("401", "Pick 2"),
            candidate("402", "Pick 3"),
            candidate("403", "Pick 4"),
        ]
        mock_comfort.return_value = [candidate("500", "Comfort 1"), candidate("501", "Comfort 2"), candidate("502", "Comfort 3")]
        mock_wildcard.return_value = [
            candidate("600", "Wild 1"),
            candidate("601", "Wild 2"),
            candidate("602", "Wild 3"),
            candidate("603", "Wild 4"),
        ]

        wildcard_blocked_statuses = {
            Status.COMPLETED.value,
            Status.DROPPED.value,
            Status.IN_PROGRESS.value,
        }
        blocked_calls: list[set[str]] = []

        def tracked_side_effect(_user, media_type, statuses=None):
            if media_type != MediaTypes.MOVIE.value or statuses is None:
                return set()
            status_set = set(statuses)
            if status_set == wildcard_blocked_statuses:
                blocked_calls.append(status_set)
                return {
                    (MediaTypes.MOVIE.value, "tmdb", "600"),
                }
            return set()

        mock_tracked_keys.side_effect = tracked_side_effect

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        top_picks_row = next(row for row in rows if row.key == "top_picks_for_you")
        wildcard_row = next(row for row in rows if row.key == "wildcard_for_you")

        self.assertGreaterEqual(len(blocked_calls), 1)
        self.assertIn("400", {item.media_id for item in top_picks_row.items})
        self.assertNotIn("600", {item.media_id for item in wildcard_row.items})

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service._wildcard_candidates")
    @patch("app.discover.service._comfort_candidates")
    @patch("app.discover.service._top_picks_candidates")
    @patch("app.discover.service.get_tracked_keys_by_media_type")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_comfort_rewatches_allows_tracked_items(
        self,
        mock_trending,
        mock_popular,
        mock_anticipated,
        mock_tracked_keys,
        mock_top_picks,
        mock_comfort,
        mock_wildcard,
        _mock_profile,
    ):
        def candidate(media_id: str, title: str) -> CandidateItem:
            return CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id=media_id,
                title=title,
                image=f"https://example.com/{media_id}.jpg",
                final_score=0.8,
            )

        mock_trending.return_value = [candidate("100", "Trending 1"), candidate("101", "Trending 2"), candidate("102", "Trending 3")]
        mock_popular.return_value = [candidate("200", "Canon 1"), candidate("201", "Canon 2"), candidate("202", "Canon 3")]
        mock_anticipated.return_value = [candidate("300", "Soon 1"), candidate("301", "Soon 2"), candidate("302", "Soon 3")]
        mock_top_picks.return_value = [candidate("400", "Pick 1"), candidate("401", "Pick 2"), candidate("402", "Pick 3")]
        mock_wildcard.return_value = [candidate("600", "Wild 1"), candidate("601", "Wild 2"), candidate("602", "Wild 3")]
        mock_comfort.return_value = [
            candidate("900", "Comfort blocked"),
            candidate("901", "Comfort 2"),
            candidate("902", "Comfort 3"),
        ]
        mock_tracked_keys.return_value = {
            (MediaTypes.MOVIE.value, "tmdb", "900"),
        }

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        comfort_row = next(row for row in rows if row.key == "comfort_rewatches")
        self.assertIn("900", {item.media_id for item in comfort_row.items})

    def test_wildcard_novelty_rerank_boosts_lower_exposure_candidate(self):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="1",
                title="High Exposure",
                genres=["Action"],
                final_score=0.8,
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="2",
                title="Low Exposure",
                genres=["Mystery"],
                final_score=0.78,
            ),
        ]

        reranked = _apply_wildcard_novelty(
            candidates,
            {"recent_genre_affinity": {"action": 1.0, "mystery": 0.0}},
        )

        self.assertEqual(reranked[0].media_id, "2")
        self.assertGreater(reranked[0].final_score, reranked[1].final_score)
        self.assertIsNotNone(reranked[0].display_score)
        self.assertAlmostEqual(
            reranked[0].display_score,
            round(max(0.0, min(1.0, 0.45 + (reranked[0].final_score * 0.39))), 6),
        )

    @patch("app.discover.service.TMDB_ADAPTER.genre_discovery")
    @patch("app.discover.service.TMDB_ADAPTER.related")
    @patch("app.discover.service._planning_candidates")
    def test_top_picks_candidates_use_local_planning_pool(
        self,
        mock_planning,
        mock_related,
        mock_genre_discovery,
    ):
        mock_planning.return_value = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="1001",
                title="Plan One",
                genres=["Action"],
                popularity=80.0,
                rating=8.0,
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="1002",
                title="Plan Two",
                genres=["Drama"],
                popularity=70.0,
                rating=7.0,
            ),
        ]
        mock_related.return_value = []
        mock_genre_discovery.return_value = []

        profile_payload = {
            "genre_affinity": {"action": 1.0, "drama": 0.5},
            "recent_genre_affinity": {"action": 1.0},
        }
        candidates = _top_picks_candidates(
            self.user,
            MediaTypes.MOVIE.value,
            "top_picks_for_you",
            profile_payload,
        )

        self.assertEqual([item.media_id for item in candidates], ["1001", "1002"])
        self.assertTrue(all(item.display_score is not None for item in candidates))
        for candidate in candidates:
            expected_display = round(max(0.0, min(1.0, 0.42 + (candidate.final_score * 0.44))), 6)
            self.assertAlmostEqual(candidate.display_score, expected_display)
        mock_related.assert_not_called()
        mock_genre_discovery.assert_not_called()

    def test_comfort_confidence_boost_tracks_user_score_and_inactivity(self):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="high",
                title="High Comfort",
                final_score=0.55,
                score_breakdown={"user_score": 10.0, "days_since_activity": 460.0},
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="mid",
                title="Mid Comfort",
                final_score=0.6,
                score_breakdown={"user_score": 8.0, "days_since_activity": 120.0},
            ),
        ]

        boosted = _apply_comfort_confidence(candidates)

        self.assertEqual(boosted[0].media_id, "high")
        self.assertGreater(boosted[0].final_score, 0.8)
        self.assertGreater(boosted[0].final_score, boosted[1].final_score)
        self.assertTrue(all(candidate.display_score is not None for candidate in boosted))
        self.assertGreaterEqual(boosted[0].display_score, 0.8)
        self.assertGreater(boosted[0].display_score, boosted[0].final_score)
        self.assertGreater(boosted[1].display_score, boosted[1].final_score)
