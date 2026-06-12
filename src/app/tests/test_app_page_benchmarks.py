"""Benchmark representative app pages with before/after-friendly output."""

from __future__ import annotations

import json
import logging
import time

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from app.models import Item, MediaTypes
from app.tests.test_query_counts import (
    seed_anime_library,
    seed_movie_library,
    seed_tv_library,
)
from lists.models import CustomList, CustomListItem
from users.home_screen import ensure_home_screen_rows
from users.models import HomeScreenRowTypeChoices

logging.disable(logging.DEBUG)


class AppPageBenchmarkTests(TestCase):
    """Print cold and warm timings for home, home-row, list, and heavy library pages."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="apppageperf",
            password="12345",
        )
        cls.user.movie_enabled = True
        cls.user.tv_enabled = True
        cls.user.anime_enabled = True
        cls.user.save(update_fields=["movie_enabled", "tv_enabled", "anime_enabled"])
        seed_tv_library(cls.user)
        seed_movie_library(cls.user)
        seed_anime_library(cls.user)

        cls.custom_list = CustomList.objects.create(
            name="Benchmark List",
            owner=cls.user,
            visibility="public",
            public_slug="benchmark-list",
        )
        list_items = list(
            Item.objects.filter(
                media_type__in=[
                    MediaTypes.MOVIE.value,
                    MediaTypes.ANIME.value,
                    MediaTypes.TV.value,
                ],
            ).order_by("id")[:12],
        )
        CustomListItem.objects.bulk_create(
            [
                CustomListItem(
                    custom_list=cls.custom_list,
                    item=item,
                    added_by=cls.user,
                )
                for item in list_items
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
        cache.clear()
        self.client.force_login(self.user)

    def _measure(self, path: str, **request_kwargs) -> dict[str, int | float]:
        started_at = time.perf_counter()
        with CaptureQueriesContext(connection) as captured_queries:
            response = self.client.get(path, **request_kwargs)
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self.assertEqual(response.status_code, 200)
        return {
            "duration_ms": round(elapsed_ms, 2),
            "query_count": len(captured_queries.captured_queries),
            "response_bytes": len(response.content),
        }

    def _benchmark_route(self, path: str, **request_kwargs) -> dict[str, object]:
        cache.clear()
        self.client.force_login(self.user)
        cold = self._measure(path, **request_kwargs)
        warm = self._measure(path, **request_kwargs)
        return {
            "path": path,
            "cold_duration_ms": cold["duration_ms"],
            "warm_duration_ms": warm["duration_ms"],
            "cold_query_count": cold["query_count"],
            "warm_query_count": warm["query_count"],
            "response_bytes": cold["response_bytes"],
        }

    def test_app_page_benchmarks(self):
        results = [
            self._benchmark_route("/"),
            self._benchmark_route(
                f"/?load_row={self.tv_home_row.id}&offset=0",
                HTTP_HX_REQUEST="true",
            ),
            self._benchmark_route(reverse("list_detail", args=[self.custom_list.public_reference])),
            self._benchmark_route("/medialist/tv"),
            self._benchmark_route("/medialist/anime"),
        ]
        print(json.dumps(results, sort_keys=True))

        for result in results:
            self.assertGreaterEqual(result["response_bytes"], 0)
