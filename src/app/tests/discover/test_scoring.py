from django.test import SimpleTestCase

from app.discover.schemas import CandidateItem
from app.discover.scoring import cosine_similarity, normalize_values, score_candidates


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
