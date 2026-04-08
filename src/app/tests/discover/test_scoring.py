from django.test import SimpleTestCase

from app.discover.schemas import CandidateItem
from app.discover.scoring import (
    blended_world_quality,
    cosine_similarity,
    normalize_values,
    score_candidates,
    weighted_pearson_correlation,
)


class DiscoverScoringTests(SimpleTestCase):
    """Tests for Discover scoring helpers."""

    def test_cosine_similarity_returns_expected_value(self):
        score = cosine_similarity({"action": 1.0, "drama": 1.0}, {"action": 1.0})
        self.assertGreater(score, 0.7)
        self.assertLessEqual(score, 1.0)

    def test_normalize_values_no_variance_returns_half(self):
        normalized = normalize_values([5.0, 5.0, 5.0])
        self.assertEqual(normalized, [0.5, 0.5, 0.5])

    def test_score_candidates_orders_by_weighted_score(self):
        candidates = [
            CandidateItem(
                media_type="movie",
                source="tmdb",
                media_id="1",
                title="High Match",
                genres=["Action"],
                tags=["heist"],
                popularity=90.0,
                rating=8.4,
            ),
            CandidateItem(
                media_type="movie",
                source="tmdb",
                media_id="2",
                title="Low Match",
                genres=["Romance"],
                tags=["slow"],
                popularity=30.0,
                rating=6.8,
            ),
        ]
        profile = {
            "genre_affinity": {"action": 1.0},
            "recent_genre_affinity": {"action": 1.0},
            "tag_affinity": {"heist": 1.0},
        }

        scored = score_candidates(candidates, profile)
        self.assertEqual(scored[0].media_id, "1")
        self.assertGreater(scored[0].final_score, scored[1].final_score)

    def test_score_candidates_preserves_existing_breakdown_keys(self):
        candidates = [
            CandidateItem(
                media_type="movie",
                source="tmdb",
                media_id="1",
                title="Preserve Context",
                genres=["Action"],
                score_breakdown={"user_score": 9.0, "days_since_activity": 180.0},
            ),
        ]
        profile = {"genre_affinity": {"action": 1.0}}

        scored = score_candidates(candidates, profile)
        self.assertEqual(scored[0].score_breakdown["user_score"], 9.0)
        self.assertEqual(scored[0].score_breakdown["days_since_activity"], 180.0)
        self.assertIn("genre_match", scored[0].score_breakdown)

    def test_score_candidates_applies_negative_penalty(self):
        candidate = CandidateItem(
            media_type="movie",
            source="tmdb",
            media_id="1",
            title="Penalty Test",
            genres=["Action"],
            tags=["heist"],
            people=["Actor A"],
        )
        profile = {
            "genre_affinity": {"action": 1.0},
            "negative_genre_affinity": {"action": 1.0},
            "negative_tag_affinity": {"heist": 1.0},
            "negative_person_affinity": {"actor a": 1.0},
        }

        scored = score_candidates([candidate], profile)

        self.assertLess(scored[0].final_score, 0.4)
        self.assertGreater(scored[0].score_breakdown["negative_total_penalty"], 0.0)

    def test_weighted_pearson_correlation_tracks_positive_alignment(self):
        correlation = weighted_pearson_correlation(
            [0.9, 0.8, 0.4, 0.2],
            [0.88, 0.75, 0.45, 0.25],
            [1.0, 1.0, 0.8, 0.8],
        )

        self.assertGreater(correlation, 0.8)

    def test_blended_world_quality_handles_tmdb_only_trakt_only_and_blend(self):
        tmdb_only = blended_world_quality(
            provider_rating=8.4,
            provider_votes=12000,
            trakt_rating=None,
            trakt_votes=None,
        )
        trakt_only = blended_world_quality(
            provider_rating=None,
            provider_votes=None,
            trakt_rating=8.1,
            trakt_votes=4000,
        )
        blended = blended_world_quality(
            provider_rating=7.9,
            provider_votes=9000,
            trakt_rating=8.4,
            trakt_votes=3000,
        )

        self.assertEqual(tmdb_only["world_source_blend"], "tmdb_only")
        self.assertGreater(tmdb_only["world_quality"], 0.7)
        self.assertEqual(trakt_only["world_source_blend"], "trakt_only")
        self.assertGreater(trakt_only["world_quality"], 0.7)
        self.assertEqual(blended["world_source_blend"], "tmdb_trakt_blend")
        self.assertGreater(blended["world_quality"], blended["tmdb_world_quality"] - 0.01)
        self.assertLess(blended["world_quality"], blended["trakt_world_quality"] + 0.01)
