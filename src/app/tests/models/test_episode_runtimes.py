"""Tests for bulk episode runtime aggregation."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import (
    TV,
    Anime,
    Item,
    MediaTypes,
    Sources,
    Status,
    build_episode_runtime_index,
    prefill_episode_runtime_index,
)


def _create_episode_item(media_id, source, season_number, episode_number, runtime):
    """Create an episode Item row with the given runtime."""
    return Item.objects.create(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.EPISODE.value,
        season_number=season_number,
        episode_number=episode_number,
        title=f"{media_id} S{season_number}E{episode_number}",
        image="https://example.com/episode.jpg",
        runtime_minutes=runtime,
    )


class BuildEpisodeRuntimeIndexTests(TestCase):
    """build_episode_runtime_index fetches and groups runtimes correctly."""

    def test_groups_by_show_and_season(self):
        """Runtimes are keyed by (media_id, source) then season."""
        _create_episode_item("show_a", Sources.TMDB.value, 1, 1, 30)
        _create_episode_item("show_a", Sources.TMDB.value, 1, 2, 35)
        _create_episode_item("show_a", Sources.TMDB.value, 2, 1, 40)
        _create_episode_item("show_b", Sources.TMDB.value, 1, 1, 50)

        index = build_episode_runtime_index(
            {("show_a", Sources.TMDB.value), ("show_b", Sources.TMDB.value)},
        )
        show_a = index[("show_a", Sources.TMDB.value)]
        self.assertEqual(sorted(show_a[1]), [(1, 30), (2, 35)])
        self.assertEqual(show_a[2], [(1, 40)])
        self.assertEqual(index[("show_b", Sources.TMDB.value)][1], [(1, 50)])

    def test_excludes_sentinel_and_null_runtimes(self):
        """Sentinel runtimes (999998/999999) and nulls are excluded."""
        _create_episode_item("show_s", Sources.TMDB.value, 1, 1, 999999)
        _create_episode_item("show_s", Sources.TMDB.value, 1, 2, 999998)
        Item.objects.create(
            media_id="show_s",
            source=Sources.TMDB.value,
            media_type=MediaTypes.EPISODE.value,
            season_number=1,
            episode_number=3,
            title="No runtime",
            image="https://example.com/episode.jpg",
        )
        _create_episode_item("show_s", Sources.TMDB.value, 1, 4, 22)

        index = build_episode_runtime_index({("show_s", Sources.TMDB.value)})
        self.assertEqual(index[("show_s", Sources.TMDB.value)][1], [(4, 22)])

    def test_filters_cross_product_rows(self):
        """Rows for unrequested id/source combinations are dropped."""
        _create_episode_item("show_x", Sources.TMDB.value, 1, 1, 30)
        _create_episode_item("show_y", Sources.MAL.value, 1, 1, 24)
        # Cross-product row: requested ids x sources includes (show_x, MAL).
        _create_episode_item("show_x", Sources.MAL.value, 1, 1, 99)

        index = build_episode_runtime_index(
            {("show_x", Sources.TMDB.value), ("show_y", Sources.MAL.value)},
        )
        self.assertNotIn(("show_x", Sources.MAL.value), index)

    def test_empty_keys(self):
        """No keys means no query and an empty index."""
        with self.assertNumQueries(0):
            self.assertEqual(build_episode_runtime_index(set()), {})


class TotalRuntimeParityTests(TestCase):
    """total_runtime_minutes computes the same values as the old per-season SQL."""

    @classmethod
    def setUpTestData(cls):
        """Create the shared user."""
        cls.user = get_user_model().objects.create_user(
            username="runtimes",
            password="12345",
        )

    def _make_tv(self, media_id, breakdown):
        """Create a TV entry annotated with a released-episode breakdown."""
        item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=media_id,
            image="https://example.com/show.jpg",
        )
        tv = TV.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        tv.released_episode_breakdown = breakdown
        tv.max_progress = sum(breakdown.values())
        return tv

    def test_tv_full_data(self):
        """All released episodes have runtimes: plain sum."""
        for episode_number, runtime in ((1, 30), (2, 40)):
            _create_episode_item(
                "tv_full",
                Sources.TMDB.value,
                1,
                episode_number,
                runtime,
            )
        tv = self._make_tv("tv_full", {1: 2})
        self.assertEqual(tv.total_runtime_minutes, 70)

    def test_tv_partial_data_extrapolates(self):
        """Missing episodes are extrapolated from the average runtime."""
        _create_episode_item("tv_part", Sources.TMDB.value, 1, 1, 30)
        tv = self._make_tv("tv_part", {1: 3})
        # 1 known of 3 episodes: 30 + int(2 * 30.0) = 90
        self.assertEqual(tv.total_runtime_minutes, 90)

    def test_tv_unreleased_episodes_excluded(self):
        """Episodes beyond the released count don't contribute runtime."""
        _create_episode_item("tv_rel", Sources.TMDB.value, 1, 1, 30)
        _create_episode_item("tv_rel", Sources.TMDB.value, 1, 2, 999)
        tv = self._make_tv("tv_rel", {1: 1})
        self.assertEqual(tv.total_runtime_minutes, 30)

    def test_tv_no_breakdown(self):
        """Without a released-episode breakdown there is no estimate from items."""
        tv = self._make_tv("tv_nobd", {})
        tv.max_progress = 5
        # Falls back to per-episode runtime estimates; none exist here.
        self.assertIsNone(tv._calc_total_runtime_from_items(5))

    def test_anime_partial_data_extrapolates(self):
        """Anime aggregates across seasons and extrapolates missing episodes."""
        _create_episode_item("anime_a", Sources.MAL.value, 1, 1, 24)
        _create_episode_item("anime_a", Sources.MAL.value, 1, 2, 24)
        item = Item.objects.create(
            media_id="anime_a",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="anime_a",
            image="https://example.com/anime.jpg",
        )
        anime = Anime.objects.create(
            item=item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )
        anime.max_progress = 4
        # 48 known + int(2 * 24.0) extrapolated = 96
        self.assertEqual(anime.total_runtime_minutes, 96)

    def test_prefilled_media_does_not_query(self):
        """Prefilled entries compute runtime with zero additional queries."""
        for episode_number in (1, 2):
            _create_episode_item("tv_pre", Sources.TMDB.value, 1, episode_number, 30)
        tv = self._make_tv("tv_pre", {1: 2})

        prefill_episode_runtime_index([tv])
        with self.assertNumQueries(0):
            self.assertEqual(tv.total_runtime_minutes, 60)

    def test_unprefilled_media_queries_once_for_all_seasons(self):
        """The detail-page fallback issues one query regardless of season count."""
        for season_number in (1, 2, 3):
            _create_episode_item("tv_one", Sources.TMDB.value, season_number, 1, 30)
        tv = self._make_tv("tv_one", {1: 1, 2: 1, 3: 1})

        with self.assertNumQueries(1):
            self.assertEqual(tv.total_runtime_minutes, 90)
        # Memoized: no further queries.
        del tv._total_runtime_minutes_cache
        with self.assertNumQueries(0):
            self.assertEqual(tv.total_runtime_minutes, 90)

    def test_prefill_bulk_uses_single_query(self):
        """Prefilling many shows costs exactly one query."""
        shows = []
        for index in range(5):
            media_id = f"tv_bulk_{index}"
            _create_episode_item(media_id, Sources.TMDB.value, 1, 1, 30)
            shows.append(self._make_tv(media_id, {1: 1}))

        with self.assertNumQueries(1):
            prefill_episode_runtime_index(shows)
        with self.assertNumQueries(0):
            for tv in shows:
                self.assertEqual(tv.total_runtime_minutes, 30)
