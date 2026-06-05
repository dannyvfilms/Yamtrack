from django.test import RequestFactory, TestCase

from app.models import Item, MediaTypes, Sources
from app.views import trakt_series_graph_fragment


class TraktSeriesGraphFragmentTests(TestCase):
    """Coverage for Trakt series graph polling behavior."""

    def test_specials_do_not_keep_show_graph_polling(self):
        """Season 0 episodes should not hold the show-level poll open."""
        Item.objects.create(
            media_id="show-1",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            title="Special 1",
            season_number=0,
            episode_number=1,
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

        request = RequestFactory().get("/app/api/trakt-series-graph/tmdb/show-1/")
        response = trakt_series_graph_fragment(request, Sources.TMDB.value, "show-1")

        content = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('hx-trigger="every 5s"', content)
        self.assertNotIn("Fetching remaining episodes", content)
