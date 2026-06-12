"""Query-count regression pins for representative high-traffic pages.

These tests pin the number of SQL queries issued by key pages so that
N+1 regressions are caught in CI. The constants below are the budget for
each page; when an optimization lands, tighten the pin in the same commit
so the diff documents the win.

Run with: python src/manage.py test app.tests.test_query_counts -v 2
"""

import logging
from datetime import UTC, datetime, timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from app.models import (
    Anime,
    CollectionEntry,
    TV,
    Episode,
    Game,
    Item,
    Manga,
    MediaTypes,
    Movie,
    Season,
    Sources,
    Status,
)
from lists.models import CustomList, CustomListItem
from users.home_screen import ensure_home_screen_rows
from users.models import HomeScreenRowTypeChoices

logging.disable(logging.DEBUG)

SHOW_COUNT = 10
SEASONS_PER_SHOW = 4
EPISODES_PER_SEASON = 5

# Query budgets. Exact counts are pinned so regressions fail loudly;
# update deliberately when page behavior changes.
HOME_PAGE_MAX_QUERIES = 120
TV_LIST_RUNTIME_SORT_MAX_QUERIES = 38  # +3 from prefetch on all_media (prevents N+1 for duplicate-watch users)
TV_LIST_TABLE_PAGE_MAX_QUERIES = 35   # +3 from same prefetch
TV_LIST_DEFAULT_SORT_MAX_QUERIES = 24   # pinned after Fix 1+2+3 (was 2642 in production)
TV_LIST_TIME_LEFT_SORT_MAX_QUERIES = 26  # pinned after Fix 4 bulk runtime load (was ~400+ per-season queries)
MOVIE_LIST_DEFAULT_SORT_MAX_QUERIES = 14
ANIME_LIST_DEFAULT_SORT_MAX_QUERIES = 20
MANGA_LIST_DEFAULT_SORT_MAX_QUERIES = 14
MANGA_LIST_NO_STATUS_MAX_QUERIES = 18
GAME_LIST_DEFAULT_SORT_MAX_QUERIES = 18
HOME_ROW_FRAGMENT_MAX_QUERIES = 120
CUSTOM_LIST_DETAIL_MAX_QUERIES = 30


def seed_tv_library(user, show_count=SHOW_COUNT):
    """Create TV shows with seasons and episode items carrying runtimes."""
    air_date = datetime(2020, 1, 1, tzinfo=UTC)
    for show_index in range(show_count):
        media_id = f"qc_show_{show_index}"
        tv_item = Item.objects.create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title=f"Query Count Show {show_index}",
            image="https://example.com/show.jpg",
            release_datetime=air_date,
        )
        tv = TV.objects.create(
            item=tv_item,
            user=user,
            status=Status.IN_PROGRESS.value,
        )
        for season_number in range(1, SEASONS_PER_SHOW + 1):
            season_item = Item.objects.create(
                media_id=media_id,
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
                title=f"Query Count Show {show_index}",
                image="https://example.com/season.jpg",
                release_datetime=air_date,
            )
            season = Season.objects.create(
                item=season_item,
                related_tv=tv,
                user=user,
                status=Status.IN_PROGRESS.value,
            )
            episodes = []
            for episode_number in range(1, EPISODES_PER_SEASON + 1):
                episode_item = Item.objects.create(
                    media_id=media_id,
                    source=Sources.TMDB.value,
                    media_type=MediaTypes.EPISODE.value,
                    season_number=season_number,
                    episode_number=episode_number,
                    title=f"Query Count Show {show_index}",
                    image="https://example.com/episode.jpg",
                    release_datetime=air_date
                    + timedelta(days=season_number * 30 + episode_number),
                    runtime_minutes=25 + episode_number,
                )
                episodes.append(
                    Episode(
                        item=episode_item,
                        related_season=season,
                        end_date=air_date,
                    ),
                )
            Episode.objects.bulk_create(episodes)


def seed_movie_library(user, count=8):
    release_date = datetime(2021, 1, 1, tzinfo=UTC)
    for movie_index in range(count):
        item = Item.objects.create(
            media_id=f"qc_movie_{movie_index}",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title=f"Query Count Movie {movie_index}",
            image="https://example.com/movie.jpg",
            release_datetime=release_date + timedelta(days=movie_index),
            runtime_minutes=100 + movie_index,
        )
        Movie.objects.create(
            item=item,
            user=user,
            status=Status.IN_PROGRESS.value,
            progress=movie_index % 2,
        )


def seed_anime_library(user, count=6):
    release_date = datetime(2022, 1, 1, tzinfo=UTC)
    for anime_index in range(count):
        item = Item.objects.create(
            media_id=f"qc_anime_{anime_index}",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title=f"Query Count Anime {anime_index}",
            image="https://example.com/anime.jpg",
            release_datetime=release_date + timedelta(days=anime_index),
        )
        Anime.objects.create(
            item=item,
            user=user,
            status=Status.IN_PROGRESS.value,
            progress=max(anime_index - 1, 0),
        )


def seed_game_library(user, count=6):
    for game_index in range(count):
        item = Item.objects.create(
            media_id=f"qc_game_{game_index}",
            source=Sources.IGDB.value,
            media_type=MediaTypes.GAME.value,
            title=f"Query Count Game {game_index}",
            image="https://example.com/game.jpg",
            provider_game_lengths={
                "active_source": "hltb",
                "hltb": {
                    "summary": {"all_styles_minutes": 600 + game_index * 30},
                    "counts": {"all_styles": 10},
                },
            },
            provider_game_lengths_source="hltb",
            provider_game_lengths_match="exact_title_year",
        )
        Game.objects.create(
            item=item,
            user=user,
            status=Status.IN_PROGRESS.value,
            progress=10 + game_index,
        )
        CollectionEntry.objects.create(
            user=user,
            item=item,
            resolution=f"Platform {game_index}",
        )


def seed_untracked_manga_collection(user, count=5):
    for manga_index in range(count):
        item = Item.objects.create(
            media_id=f"qc_manga_{manga_index}",
            source=Sources.MAL.value,
            media_type=MediaTypes.MANGA.value,
            title=f"Query Count Manga {manga_index}",
            image="https://example.com/manga.jpg",
        )
        CollectionEntry.objects.create(user=user, item=item, media_type="paperback")


class QueryCountTests(TestCase):
    """Pin query counts for representative pages."""

    @classmethod
    def setUpTestData(cls):
        """Seed a TV library shared by all tests."""
        cls.user = get_user_model().objects.create_user(
            username="querycount",
            password="12345",
        )
        cls.user.tv_enabled = True
        cls.user.save()
        seed_tv_library(cls.user)
        seed_movie_library(cls.user)
        seed_anime_library(cls.user)
        seed_game_library(cls.user)
        seed_untracked_manga_collection(cls.user)
        cls.custom_list = CustomList.objects.create(
            name="Query Count Benchmark List",
            owner=cls.user,
        )
        CustomListItem.objects.bulk_create(
            [
                CustomListItem(
                    custom_list=cls.custom_list,
                    item=item,
                    added_by=cls.user,
                )
                for item in Item.objects.filter(
                    media_type__in=[
                        MediaTypes.TV.value,
                        MediaTypes.MOVIE.value,
                        MediaTypes.ANIME.value,
                    ],
                ).order_by("id")[:10]
            ],
        )
        ensure_home_screen_rows(cls.user)
        cls.tv_home_row = (
            cls.user.home_screen_rows.filter(
                media_type=MediaTypes.TV.value,
                row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            )
            .order_by("position", "id")
            .first()
        )

    def setUp(self):
        """Reset cache state and log in."""
        cache.clear()
        self.client.force_login(self.user)

    def _assert_query_budget(self, url, budget, label):
        with CaptureQueriesContext(connection) as context:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        count = len(context.captured_queries)
        self.assertLessEqual(
            count,
            budget,
            f"{label} issued {count} queries, budget is {budget}. "
            "If this increase is intentional, update the pin deliberately.",
        )

    def _assert_query_budget_with_kwargs(self, url, budget, label, **request_kwargs):
        with CaptureQueriesContext(connection) as context:
            response = self.client.get(url, **request_kwargs)
        self.assertEqual(response.status_code, 200)
        count = len(context.captured_queries)
        self.assertLessEqual(
            count,
            budget,
            f"{label} issued {count} queries, budget is {budget}. "
            "If this increase is intentional, update the pin deliberately.",
        )

    def test_home_page_query_budget(self):
        """Home page stays within its query budget."""
        self._assert_query_budget("/", HOME_PAGE_MAX_QUERIES, "home page")

    def test_home_row_fragment_query_budget(self):
        """Home-row HTMX fragment stays within its query budget."""
        self._assert_query_budget_with_kwargs(
            f"{reverse('home')}?load_row={self.tv_home_row.id}&offset=0",
            HOME_ROW_FRAGMENT_MAX_QUERIES,
            "home row fragment",
            HTTP_HX_REQUEST="true",
        )

    def test_tv_list_runtime_sort_query_budget(self):
        """Runtime-sorted TV list stays within its query budget."""
        self._assert_query_budget(
            "/medialist/tv?sort=runtime",
            TV_LIST_RUNTIME_SORT_MAX_QUERIES,
            "TV list sorted by runtime",
        )

    def test_tv_list_table_page_query_budget(self):
        """TV table layout stays within its query budget."""
        self._assert_query_budget(
            "/medialist/tv?layout=table",
            TV_LIST_TABLE_PAGE_MAX_QUERIES,
            "TV list table layout",
        )

    def test_tv_list_default_sort_query_budget(self):
        """Default TV list (no sort param) stays within budget — catches N+1 regressions."""
        self._assert_query_budget(
            "/medialist/tv",
            TV_LIST_DEFAULT_SORT_MAX_QUERIES,
            "TV list default sort",
        )

    def test_tv_list_time_left_sort_query_budget(self):
        """Time-left sort uses bulk episode runtime query, not per-season queries."""
        self._assert_query_budget(
            "/medialist/tv?sort=time_left",
            TV_LIST_TIME_LEFT_SORT_MAX_QUERIES,
            "TV list time_left sort",
        )

    def test_tv_list_cache_hit_query_budget(self):
        """Second TV list request hits the media-list cache, skipping the expensive build phase.

        Cache hit budget is higher than 0 because annotate_max_progress,
        prefill_episode_runtime_index, and auth/session queries still run
        post-pagination (they're cheap and page-specific).
        """
        self.client.get("/medialist/tv")  # warm the cache
        self._assert_query_budget("/medialist/tv", 20, "TV list cache hit")

    def test_movie_list_default_sort_query_budget(self):
        self._assert_query_budget(
            "/medialist/movie",
            MOVIE_LIST_DEFAULT_SORT_MAX_QUERIES,
            "movie list default sort",
        )

    def test_anime_list_default_sort_query_budget(self):
        self._assert_query_budget(
            "/medialist/anime",
            ANIME_LIST_DEFAULT_SORT_MAX_QUERIES,
            "anime list default sort",
        )

    def test_manga_list_default_sort_query_budget(self):
        self._assert_query_budget(
            "/medialist/manga",
            MANGA_LIST_DEFAULT_SORT_MAX_QUERIES,
            "manga list default sort",
        )

    def test_manga_list_no_status_query_budget(self):
        self._assert_query_budget(
            "/medialist/manga?status=no_status",
            MANGA_LIST_NO_STATUS_MAX_QUERIES,
            "manga list no status",
        )

    def test_game_list_default_sort_query_budget(self):
        self._assert_query_budget(
            "/medialist/game",
            GAME_LIST_DEFAULT_SORT_MAX_QUERIES,
            "game list default sort",
        )

    def test_custom_list_detail_query_budget(self):
        self._assert_query_budget(
            reverse("list_detail", args=[self.custom_list.public_reference]),
            CUSTOM_LIST_DETAIL_MAX_QUERIES,
            "custom list detail",
        )
