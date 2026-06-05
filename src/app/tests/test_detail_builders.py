from django.test import TestCase

from app.detail_builders import _build_series_graph_data
from app.models import Item, MediaTypes, Sources


class SeriesGraphBuilderTests(TestCase):
    """Focused coverage for the episode graph layout builder."""

    def test_include_unrated_preserves_episode_slots(self):
        """Unrated episodes should keep their grid positions during polling."""
        Item.objects.create(
            media_id="show-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Special 1",
            season_number=0,
            episode_number=1,
            trakt_rating=9.0,
            trakt_rating_count=50,
        )
        Item.objects.create(
            media_id="show-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode 1",
            season_number=1,
            episode_number=1,
            trakt_rating=8.0,
            trakt_rating_count=100,
        )
        Item.objects.create(
            media_id="show-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Episode 2",
            season_number=1,
            episode_number=2,
        )

        graph_data = _build_series_graph_data(
            Sources.TMDB.value,
            "show-1",
            use_trakt=True,
            include_unrated=True,
        )

        self.assertIsNotNone(graph_data)
        self.assertEqual([season["label"] for season in graph_data["seasons"]], ["S1"])
        self.assertEqual([row["ep"] for row in graph_data["episode_rows"]], [1, 2])
        self.assertEqual(graph_data["episode_rows"][0]["cells"][0]["score"], 8.0)
        self.assertIsNone(graph_data["episode_rows"][1]["cells"][0]["score"])
