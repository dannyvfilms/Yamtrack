from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.discover import cache_repo
from app.discover.schemas import CandidateItem
from app.discover.service import (
    _apply_comfort_confidence,
    _build_comfort_debug_payload,
    _comfort_candidates,
    _comfort_match_signal,
    _prefer_strong_phase_opening_window,
    _apply_wildcard_novelty,
    _top_picks_candidates,
    get_discover_rows,
)
from app.models import Item, MediaTypes, Movie, Sources, Status


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

        self.assertEqual(mock_popular.call_count, 1)
        self.assertEqual(mock_popular.call_args_list[0].kwargs, {"page": 1, "limit": 100})
        self.assertEqual(len(canon_row.items), 12)
        self.assertTrue(all(item.media_id not in {str(index) for index in range(1, 89)} for item in canon_row.items))
        self.assertIn("89", {item.media_id for item in canon_row.items})

        cached_payload, _ = cache_repo.get_row_cache(
            self.user.id,
            MediaTypes.MOVIE.value,
            "all_time_greats_unseen",
        )
        self.assertIsNotNone(cached_payload)
        self.assertEqual(cached_payload.get("meta", {}).get("adaptive_pull_target"), 100)

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service.TMDB_ADAPTER.top_rated", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.upcoming", return_value=[])
    @patch("app.discover.service.TMDB_ADAPTER.current_cycle", return_value=[])
    @patch("app.discover.service.get_tracked_keys_by_media_type", return_value=set())
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated", return_value=[])
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_all_time_greats_unseen_keeps_overlap_items_when_cached_overlap(
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
        self.assertIn("89", canon_ids)
        self.assertIn("90", canon_ids)
        self.assertIn("100", canon_ids)
        self.assertNotIn("101", canon_ids)

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
    @patch("app.discover.service._comfort_candidates")
    @patch("app.discover.service._top_picks_candidates")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_movie_rows_render_exactly_five_in_expected_order(
        self,
        mock_trending,
        mock_popular,
        mock_anticipated,
        mock_top_picks,
        mock_comfort,
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

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        self.assertEqual(
            [row.key for row in rows],
            [
                "trending_right_now",
                "all_time_greats_unseen",
                "coming_soon",
                "top_picks_for_you",
                "comfort_rewatches",
            ],
        )

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
    @patch("app.discover.service._comfort_candidates", return_value=[])
    @patch("app.discover.service._top_picks_candidates")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_top_picks_row_backfills_after_dedupe_using_buffered_candidates(
        self,
        mock_trending,
        mock_popular,
        mock_anticipated,
        mock_top_picks,
        _mock_comfort,
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

        duplicate_ids = ["100", "101", "102", "200", "201", "202", "300", "301", "302", "100", "200", "300"]
        top_pick_candidates = [
            candidate(media_id, f"Pick duplicate {index}")
            for index, media_id in enumerate(duplicate_ids, start=1)
        ]
        top_pick_candidates.extend(
            candidate(str(600 + index), f"Pick fresh {index}") for index in range(12)
        )
        mock_top_picks.return_value = top_pick_candidates

        rows = get_discover_rows(self.user, MediaTypes.MOVIE.value, show_more=False)
        top_picks_row = next(row for row in rows if row.key == "top_picks_for_you")

        self.assertEqual(len(top_picks_row.items), 12)
        self.assertTrue(all(item.media_id.startswith("6") for item in top_picks_row.items))

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
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
    @patch("app.discover.service._comfort_candidates", return_value=[])
    @patch("app.discover.service._top_picks_candidates")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_anticipated")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_popular")
    @patch("app.discover.service.TRAKT_ADAPTER.movie_watched_weekly")
    def test_top_picks_row_sets_debug_payload_when_enabled(
        self,
        mock_trending,
        mock_popular,
        mock_anticipated,
        mock_top_picks,
        _mock_comfort,
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
                score_breakdown={
                    "phase_fit": 0.7,
                    "hot_recency": 0.1,
                    "phase_family_contribution": 0.3,
                    "hot_recency_contribution": 0.02,
                    "rating_contribution": 0.04,
                    "rewatch_contribution": 0.0,
                },
            )

        mock_trending.return_value = [candidate("100", "Trending 1"), candidate("101", "Trending 2"), candidate("102", "Trending 3")]
        mock_popular.return_value = [candidate("200", "Canon 1"), candidate("201", "Canon 2"), candidate("202", "Canon 3")]
        mock_anticipated.return_value = [candidate("300", "Soon 1"), candidate("301", "Soon 2"), candidate("302", "Soon 3")]
        mock_top_picks.return_value = [
            candidate("400", "Pick 1"),
            candidate("401", "Pick 2"),
            candidate("402", "Pick 3"),
        ]

        rows = get_discover_rows(
            self.user,
            MediaTypes.MOVIE.value,
            show_more=False,
            include_debug=True,
        )
        top_picks_row = next(row for row in rows if row.key == "top_picks_for_you")

        self.assertIsNotNone(top_picks_row.debug_payload)
        self.assertIn("top_candidates", top_picks_row.debug_payload)
        self.assertIn("tag_signal", top_picks_row.debug_payload)
        self.assertGreaterEqual(len(top_picks_row.debug_payload["top_candidates"]), 1)

    @patch("app.discover.service.get_or_compute_taste_profile", return_value={})
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

    def test_comfort_confidence_ranks_by_recent_watch_similarity(self):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="recent_match",
                title="Recent Match",
                genres=["Science Fiction", "Thriller"],
                final_score=0.55,
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 120.0,
                    "recency_bonus": 0.9,
                    "phase_genre_bonus": 0.95,
                    "recency_tag_bonus": 0.7,
                    "phase_tag_bonus": 0.8,
                    "rewatch_count": 1.0,
                    "genre_match": 0.7,
                    "tag_match": 0.5,
                },
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="high_rated",
                title="High Rated",
                genres=["Romance", "Comedy"],
                final_score=0.6,
                score_breakdown={
                    "user_score": 10.0,
                    "days_since_activity": 460.0,
                    "recency_bonus": 0.1,
                    "phase_genre_bonus": 0.2,
                    "recency_tag_bonus": 0.1,
                    "phase_tag_bonus": 0.1,
                    "rewatch_count": 1.0,
                    "genre_match": 0.2,
                    "tag_match": 0.1,
                },
            ),
        ]

        profile = {
            "phase_genre_affinity": {
                "science fiction": 1.0,
                "thriller": 0.8,
                "action": 0.5,
            },
        }

        boosted = _apply_comfort_confidence(candidates, profile)

        # Recent-match candidate should rank above high-rated candidate
        self.assertEqual(boosted[0].media_id, "recent_match")
        self.assertGreater(boosted[0].final_score, boosted[1].final_score)
        self.assertTrue(
            all(c.display_score is not None for c in boosted),
        )
        # Match genres populated from profile overlap
        self.assertIn(
            "match_genres", boosted[0].score_breakdown,
        )
        self.assertIn(
            "Science Fiction", boosted[0].score_breakdown["match_genres"],
        )

    def test_comfort_confidence_unrated_not_penalized(self):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="unrated_phase",
                title="Unrated Phase Fit",
                genres=["Animation", "Family"],
                score_breakdown={
                    "days_since_activity": 180.0,
                    "recency_bonus": 0.8,
                    "phase_genre_bonus": 1.0,
                    "recency_tag_bonus": 0.8,
                    "phase_tag_bonus": 1.0,
                    "rewatch_count": 1.0,
                    "genre_match": 0.8,
                    "tag_match": 0.7,
                },
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="rated_weak",
                title="Rated But Weak Fit",
                genres=["Crime"],
                score_breakdown={
                    "user_score": 9.5,
                    "days_since_activity": 180.0,
                    "recency_bonus": 0.2,
                    "phase_genre_bonus": 0.2,
                    "recency_tag_bonus": 0.1,
                    "phase_tag_bonus": 0.1,
                    "rewatch_count": 1.0,
                    "genre_match": 0.2,
                    "tag_match": 0.1,
                },
            ),
        ]

        reranked = _apply_comfort_confidence(
            candidates,
            {"phase_genre_affinity": {"animation": 1.0, "family": 0.8}},
        )

        self.assertEqual(reranked[0].media_id, "unrated_phase")
        self.assertEqual(reranked[0].score_breakdown["rating_confidence"], 0.5)
        self.assertGreater(reranked[0].final_score, reranked[1].final_score)

    def test_comfort_confidence_rewatch_boost(self):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="rewatched",
                title="Rewatched",
                genres=["Comedy"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 220.0,
                    "recency_bonus": 0.6,
                    "phase_genre_bonus": 0.7,
                    "recency_tag_bonus": 0.4,
                    "phase_tag_bonus": 0.5,
                    "rewatch_count": 4.0,
                    "genre_match": 0.6,
                    "tag_match": 0.4,
                },
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="single_watch",
                title="Single Watch",
                genres=["Comedy"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 220.0,
                    "recency_bonus": 0.6,
                    "phase_genre_bonus": 0.7,
                    "recency_tag_bonus": 0.4,
                    "phase_tag_bonus": 0.5,
                    "rewatch_count": 1.0,
                    "genre_match": 0.6,
                    "tag_match": 0.4,
                },
            ),
        ]

        reranked = _apply_comfort_confidence(
            candidates,
            {"phase_genre_affinity": {"comedy": 1.0}},
        )

        self.assertEqual(reranked[0].media_id, "rewatched")
        self.assertGreater(
            reranked[0].score_breakdown["rewatch_bonus"],
            reranked[1].score_breakdown["rewatch_bonus"],
        )

    def test_comfort_confidence_decorrelates_hot_recency_overlap(self):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="overlap",
                title="Overlap Fit",
                genres=["Drama"],
                tags=["Character Study"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 200.0,
                    "recency_bonus": 0.62,
                    "phase_genre_bonus": 0.6,
                    "recency_tag_bonus": 0.66,
                    "phase_tag_bonus": 0.64,
                    "rewatch_count": 1.0,
                    "genre_match": 0.6,
                    "tag_match": 0.6,
                },
            ),
        ]

        reranked = _apply_comfort_confidence(
            candidates,
            {"phase_genre_affinity": {"drama": 1.0}},
        )
        breakdown = reranked[0].score_breakdown

        self.assertGreater(breakdown["hot_recency_base"], 0.0)
        self.assertLess(breakdown["hot_recency"], breakdown["hot_recency_base"])
        self.assertGreaterEqual(breakdown["phase_hot_overlap_ratio"], 0.0)
        self.assertLess(breakdown["phase_hot_overlap_ratio"], 1.0)
        self.assertGreaterEqual(breakdown["hot_recency_incremental"], 0.0)

    def test_comfort_confidence_uses_tag_coverage_mode_for_hot_recency(self):
        sparse_candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="sparse",
                title="Sparse",
                genres=["Drama"],
                tags=[],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 200.0,
                    "recency_bonus": 0.95,
                    "phase_genre_bonus": 0.25,
                    "recency_tag_bonus": 0.95,
                    "phase_tag_bonus": 0.1,
                    "rewatch_count": 1.0,
                    "genre_match": 0.6,
                    "tag_match": 0.2,
                    "recent_history_tag_coverage": 0.1,
                },
            ),
        ]
        rich_candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="rich",
                title="Rich",
                genres=["Drama"],
                tags=["Cozy"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 200.0,
                    "recency_bonus": 0.95,
                    "phase_genre_bonus": 0.25,
                    "recency_tag_bonus": 0.95,
                    "phase_tag_bonus": 0.1,
                    "rewatch_count": 1.0,
                    "genre_match": 0.6,
                    "tag_match": 0.2,
                    "recent_history_tag_coverage": 0.8,
                },
            ),
        ]

        sparse_ranked = _apply_comfort_confidence(
            sparse_candidates,
            {"phase_genre_affinity": {"drama": 1.0}},
        )
        rich_ranked = _apply_comfort_confidence(
            rich_candidates,
            {"phase_genre_affinity": {"drama": 1.0}, "phase_tag_affinity": {"cozy": 1.0}},
        )

        sparse = sparse_ranked[0].score_breakdown
        rich = rich_ranked[0].score_breakdown
        self.assertEqual(sparse["tag_signal_mode"], "tag_sparse")
        self.assertEqual(rich["tag_signal_mode"], "tag_rich")
        self.assertLess(sparse["hot_recency_mode_multiplier"], 1.0)
        self.assertEqual(rich["hot_recency_mode_multiplier"], 1.0)
        self.assertGreater(rich["hot_recency_contribution"], sparse["hot_recency_contribution"])

    def test_comfort_confidence_rewatch_needs_phase_support(self):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="legacy_rewatch",
                title="Legacy Rewatch",
                genres=["Drama"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 220.0,
                    "recency_bonus": 0.2,
                    "phase_genre_bonus": 0.2,
                    "recency_tag_bonus": 0.2,
                    "phase_tag_bonus": 0.2,
                    "rewatch_count": 6.0,
                    "genre_match": 0.2,
                    "tag_match": 0.2,
                },
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="phase_fit",
                title="Phase Fit",
                genres=["Animation"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 220.0,
                    "recency_bonus": 0.55,
                    "phase_genre_bonus": 0.85,
                    "recency_tag_bonus": 0.5,
                    "phase_tag_bonus": 0.8,
                    "rewatch_count": 1.0,
                    "genre_match": 0.7,
                    "tag_match": 0.7,
                },
            ),
        ]

        reranked = _apply_comfort_confidence(
            candidates,
            {"phase_genre_affinity": {"animation": 1.0}},
        )

        self.assertEqual(reranked[0].media_id, "phase_fit")
        legacy = next(candidate for candidate in reranked if candidate.media_id == "legacy_rewatch")
        self.assertLess(legacy.score_breakdown["rewatch_gate"], 0.5)

    def test_comfort_confidence_prefers_strong_phase_in_opening_window(self):
        candidates = []
        for index in range(12):
            candidates.append(
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source="tmdb",
                    media_id=f"medium-{index}",
                    title=f"Medium {index}",
                    genres=["Drama"],
                    score_breakdown={
                        "phase_pool_medium": 1.0,
                    },
                    final_score=0.80 - (index * 0.01),
                ),
            )
        candidates.append(
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="strong-below",
                title="Strong Below",
                genres=["Drama"],
                score_breakdown={
                    "phase_pool_strong": 1.0,
                },
                final_score=0.78,
            ),
        )
        reranked = _prefer_strong_phase_opening_window(candidates)

        opening_ids = {candidate.media_id for candidate in reranked[:12]}
        self.assertIn("strong-below", opening_ids)
        self.assertTrue(
            any(
                float(candidate.score_breakdown.get("strong_phase_promoted_opening", 0.0)) >= 1.0
                for candidate in reranked[:12]
            ),
        )

    def test_comfort_candidates_includes_unrated(self):
        rated_item = Item.objects.create(
            media_id="4001",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Rated Old Favorite",
            image="http://example.com/rated.jpg",
            genres=["Action"],
        )
        unrated_old_item = Item.objects.create(
            media_id="4002",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Unrated Dormant",
            image="http://example.com/unrated-old.jpg",
            genres=["Drama"],
        )
        unrated_recent_item = Item.objects.create(
            media_id="4003",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Unrated Recent",
            image="http://example.com/unrated-recent.jpg",
            genres=["Comedy"],
        )

        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            Movie.objects.create(
                item=rated_item,
                user=self.user,
                score=9,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=130),
            )
            Movie.objects.create(
                item=unrated_old_item,
                user=self.user,
                score=None,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=140),
            )
            Movie.objects.create(
                item=unrated_recent_item,
                user=self.user,
                score=None,
                status=Status.COMPLETED.value,
                end_date=timezone.now() - timedelta(days=20),
            )

        candidates = _comfort_candidates(
            self.user,
            MediaTypes.MOVIE.value,
            row_key="comfort_rewatches",
            source_reason="Past favorite",
            older_than_days=30,
            min_score=8.0,
        )
        candidate_ids = {candidate.media_id for candidate in candidates}

        self.assertIn("4001", candidate_ids)
        self.assertIn("4002", candidate_ids)
        self.assertNotIn("4003", candidate_ids)

    def test_comfort_candidates_biases_phase_evidence_with_limited_backfill(self):
        phase_media_ids = {"5001", "5002"}
        with patch("app.models.providers.services.get_media_metadata", return_value={"max_progress": 1}):
            for media_id in sorted(phase_media_ids):
                item = Item.objects.create(
                    media_id=media_id,
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.MOVIE.value,
                    title=f"Phase {media_id}",
                    image=f"http://example.com/{media_id}.jpg",
                    genres=["Animation"],
                )
                Movie.objects.create(
                    item=item,
                    user=self.user,
                    score=8,
                    status=Status.COMPLETED.value,
                    end_date=timezone.now() - timedelta(days=180),
                )

            for index in range(8):
                media_id = str(6000 + index)
                item = Item.objects.create(
                    media_id=media_id,
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.MOVIE.value,
                    title=f"Broad {media_id}",
                    image=f"http://example.com/{media_id}.jpg",
                    genres=["Action"],
                )
                Movie.objects.create(
                    item=item,
                    user=self.user,
                    score=10,
                    status=Status.COMPLETED.value,
                    end_date=timezone.now() - timedelta(days=180),
                )

        candidates = _comfort_candidates(
            self.user,
            MediaTypes.MOVIE.value,
            row_key="comfort_rewatches",
            source_reason="Past favorite",
            older_than_days=90,
            min_score=8.0,
            profile_payload={"phase_genre_affinity": {"animation": 1.0}},
        )
        top_two = {candidate.media_id for candidate in candidates[:2]}
        weak_count = sum(
            1
            for candidate in candidates
            if "action" in {genre.lower() for genre in (candidate.genres or [])}
        )

        self.assertEqual(top_two, phase_media_ids)
        self.assertEqual(len(candidates), 8)
        self.assertEqual(weak_count, 6)

    @patch("app.discover.service._is_holiday_window", return_value=False)
    def test_comfort_confidence_applies_out_of_season_holiday_penalty(self, _mock_window):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="holiday",
                title="The Man Who Invented Christmas",
                tags=["Christmas"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 220.0,
                    "recency_bonus": 0.7,
                    "phase_genre_bonus": 0.7,
                    "recency_tag_bonus": 0.7,
                    "phase_tag_bonus": 0.7,
                    "rewatch_count": 1.0,
                    "genre_match": 0.7,
                    "tag_match": 0.7,
                },
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="neutral",
                title="Anytime Comfort",
                tags=["Feel Good"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 220.0,
                    "recency_bonus": 0.7,
                    "phase_genre_bonus": 0.7,
                    "recency_tag_bonus": 0.7,
                    "phase_tag_bonus": 0.7,
                    "rewatch_count": 1.0,
                    "genre_match": 0.7,
                    "tag_match": 0.7,
                },
            ),
        ]

        reranked = _apply_comfort_confidence(
            candidates,
            {
                "phase_genre_affinity": {"comedy": 1.0},
                "phase_tag_affinity": {"feel good": 1.0},
            },
        )

        by_id = {candidate.media_id: candidate for candidate in reranked}
        self.assertLess(by_id["holiday"].score_breakdown["seasonal_adjustment"], 0.0)
        self.assertEqual(by_id["neutral"].score_breakdown["seasonal_adjustment"], 0.0)
        self.assertLess(by_id["holiday"].final_score, by_id["neutral"].final_score)

    def test_comfort_match_signal_prefers_phase_tags(self):
        signal = _comfort_match_signal(
            {
                "phase_tag_affinity": {"cozy": 1.0, "musical": 0.8, "family": 0.7},
                "phase_genre_affinity": {"drama": 1.0, "action": 0.8},
            },
        )
        self.assertEqual(signal, "Driven by your current Cozy, Musical, Family Comfort phase")

    def test_comfort_match_signal_formats_generic_genres_with_descriptive_labels(self):
        signal = _comfort_match_signal(
            {
                "phase_genre_affinity": {"drama": 1.0, "comedy": 0.9, "action": 0.8},
            },
        )
        self.assertEqual(
            signal,
            "Driven by your current Character Drama, Feel-Good Comedy, Popcorn Action phase",
        )

    def test_comfort_confidence_phase_lane_quota_promotes_recent_lane(self):
        candidates: list[CandidateItem] = []
        for index in range(12):
            candidates.append(
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source="tmdb",
                    media_id=f"broad-{index}",
                    title=f"Broad {index}",
                    genres=["Action"],
                    tags=["General"],
                    score_breakdown={
                        "user_score": 9.0,
                        "days_since_activity": 300.0,
                        "recency_bonus": 0.8,
                        "phase_genre_bonus": 0.25,
                        "recency_tag_bonus": 0.2,
                        "phase_tag_bonus": 0.2,
                        "rewatch_count": 2.0,
                        "genre_match": 0.8,
                        "tag_match": 0.4,
                    },
                ),
            )

        for index in range(3):
            candidates.append(
                CandidateItem(
                    media_type=MediaTypes.MOVIE.value,
                    source="tmdb",
                    media_id=f"phase-{index}",
                    title=f"Phase {index}",
                    genres=["Animation"],
                    tags=["Cozy"],
                    score_breakdown={
                        "user_score": 7.0,
                        "days_since_activity": 120.0,
                        "recency_bonus": 0.3,
                        "phase_genre_bonus": 0.35,
                        "recency_tag_bonus": 0.3,
                        "phase_tag_bonus": 0.35,
                        "rewatch_count": 1.0,
                        "genre_match": 0.3,
                        "tag_match": 0.3,
                    },
                ),
            )

        reranked = _apply_comfort_confidence(
            candidates,
            {
                "phase_genre_affinity": {"animation": 1.0},
                "phase_tag_affinity": {"cozy": 1.0},
            },
        )
        top_twelve = reranked[:12]
        phase_count = sum(
            1 for candidate in top_twelve
            if "animation" in {genre.lower() for genre in (candidate.genres or [])}
            or "cozy" in {tag.lower() for tag in (candidate.tags or [])}
        )
        self.assertGreaterEqual(phase_count, 3)

    def test_comfort_confidence_display_calibration_strengthens_top_band(self):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="top",
                title="Top",
                genres=["Animation"],
                tags=["Cozy"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 200.0,
                    "recency_bonus": 0.8,
                    "phase_genre_bonus": 0.85,
                    "recency_tag_bonus": 0.8,
                    "phase_tag_bonus": 0.85,
                    "rewatch_count": 2.0,
                    "genre_match": 0.8,
                    "tag_match": 0.8,
                },
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="mid",
                title="Mid",
                genres=["Comedy"],
                tags=["Feel Good"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 160.0,
                    "recency_bonus": 0.5,
                    "phase_genre_bonus": 0.55,
                    "recency_tag_bonus": 0.5,
                    "phase_tag_bonus": 0.55,
                    "rewatch_count": 1.0,
                    "genre_match": 0.5,
                    "tag_match": 0.5,
                },
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="low",
                title="Low",
                genres=["Drama"],
                tags=["Classic"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 140.0,
                    "recency_bonus": 0.3,
                    "phase_genre_bonus": 0.3,
                    "recency_tag_bonus": 0.2,
                    "phase_tag_bonus": 0.2,
                    "rewatch_count": 1.0,
                    "genre_match": 0.3,
                    "tag_match": 0.3,
                },
            ),
        ]
        reranked = _apply_comfort_confidence(
            candidates,
            {"phase_genre_affinity": {"animation": 1.0}, "phase_tag_affinity": {"cozy": 1.0}},
        )
        final_scores = [float(candidate.final_score or 0.0) for candidate in reranked]
        display_scores = [float(candidate.display_score or 0.0) for candidate in reranked]

        self.assertEqual(
            [candidate.media_id for candidate in reranked],
            ["top", "mid", "low"],
        )
        self.assertGreaterEqual(display_scores[0], 0.8)
        self.assertGreater(display_scores[0], final_scores[0])
        self.assertGreater(display_scores[1], final_scores[1])
        self.assertGreater(display_scores[2], final_scores[2])
        self.assertGreaterEqual(final_scores[0], final_scores[1])
        self.assertGreaterEqual(final_scores[1], final_scores[2])
        self.assertGreaterEqual(display_scores[0], display_scores[1])
        self.assertGreaterEqual(display_scores[1], display_scores[2])

    @patch("app.discover.service._is_holiday_window", return_value=False)
    def test_comfort_debug_payload_exposes_spread_and_penalty_stack(self, _mock_window):
        candidates = [
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="holiday",
                title="The Man Who Invented Christmas",
                tags=["Christmas"],
                genres=["Drama"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 200.0,
                    "recency_bonus": 0.75,
                    "phase_genre_bonus": 0.75,
                    "recency_tag_bonus": 0.75,
                    "phase_tag_bonus": 0.75,
                    "rewatch_count": 2.0,
                    "genre_match": 0.75,
                    "tag_match": 0.75,
                    "phase_pool_backfill": 1.0,
                },
            ),
            CandidateItem(
                media_type=MediaTypes.MOVIE.value,
                source="tmdb",
                media_id="clean",
                title="Clean Fit",
                tags=["Cozy"],
                genres=["Animation"],
                score_breakdown={
                    "user_score": 8.0,
                    "days_since_activity": 200.0,
                    "recency_bonus": 0.6,
                    "phase_genre_bonus": 0.65,
                    "recency_tag_bonus": 0.6,
                    "phase_tag_bonus": 0.65,
                    "rewatch_count": 1.0,
                    "genre_match": 0.6,
                    "tag_match": 0.6,
                    "phase_pool_strong": 1.0,
                },
            ),
        ]
        reranked = _apply_comfort_confidence(
            candidates,
            {"phase_genre_affinity": {"animation": 1.0}, "phase_tag_affinity": {"cozy": 1.0}},
        )
        payload = _build_comfort_debug_payload(reranked, top_n=2)

        self.assertIn("score_distribution", payload)
        self.assertIn("penalty_stack", payload)
        self.assertIn("contribution_totals", payload)
        self.assertIn("dampener_totals", payload)
        self.assertIn("tag_signal", payload)
        self.assertIn("top_candidates", payload)
        self.assertEqual(len(payload["top_candidates"]), 2)
        self.assertIn("raw_spread", payload["score_distribution"])
        self.assertIn("compressed_raw", payload["score_distribution"])
        self.assertIn("multi_penalty_count", payload["penalty_stack"])
        self.assertIn("phase_family", payload["contribution_totals"])
        self.assertIn("hot_recency", payload["contribution_totals"])
        self.assertIn("seasonality", payload["dampener_totals"])
        self.assertIn("opening_era", payload["dampener_totals"])
        self.assertIn("mode", payload["tag_signal"])
        self.assertIn("candidate_tag_coverage_top_n", payload["tag_signal"])
        self.assertIn("recent_history_tag_coverage", payload["tag_signal"])
        self.assertIn("phase_pool_source", payload["top_candidates"][0])
        self.assertIn("raw_final_score", payload["top_candidates"][0])
        self.assertIn("phase_family_contribution", payload["top_candidates"][0])
        self.assertIn("hot_recency_contribution", payload["top_candidates"][0])
        self.assertIn("seasonality_dampener_contribution", payload["top_candidates"][0])
        self.assertIn("diversity_dampener_contribution", payload["top_candidates"][0])
        self.assertIn("tag_signal_mode", payload["top_candidates"][0])
