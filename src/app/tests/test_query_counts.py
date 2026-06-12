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

from app.models import (
    TV,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
)

logging.disable(logging.DEBUG)

SHOW_COUNT = 10
SEASONS_PER_SHOW = 4
EPISODES_PER_SEASON = 5

# Query budgets. Exact counts are pinned so regressions fail loudly;
# update deliberately when page behavior changes.
HOME_PAGE_MAX_QUERIES = 106
TV_LIST_RUNTIME_SORT_MAX_QUERIES = 35
TV_LIST_TABLE_PAGE_MAX_QUERIES = 32


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

    def test_home_page_query_budget(self):
        """Home page stays within its query budget."""
        self._assert_query_budget("/", HOME_PAGE_MAX_QUERIES, "home page")

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
