"""Benchmark-oriented regression coverage for media-list routes.

These tests report timing, query count, and payload size for representative
library pages. They intentionally keep assertions light so the same harness can
be used to capture before/after numbers during performance work.
"""

from __future__ import annotations

import json
import time

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from app.models import MediaTypes
from app.tests.test_query_counts import (
    seed_anime_library,
    seed_game_library,
    seed_movie_library,
    seed_tv_library,
    seed_untracked_manga_collection,
)


class MediaListBenchmarkTests(TestCase):
    """Capture cold/warm benchmark metrics for representative media-list routes."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="media-benchmark",
            password="12345",
        )
        cls.user.tv_enabled = True
        cls.user.save()
        seed_tv_library(cls.user, show_count=8)
        seed_movie_library(cls.user, count=10)
        seed_anime_library(cls.user, count=8)
        seed_game_library(cls.user, count=8)
        seed_untracked_manga_collection(cls.user, count=30)

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def _measure_route(self, url: str) -> dict[str, int | float | str]:
        started_at = time.perf_counter()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url, follow=True)
        duration_ms = (time.perf_counter() - started_at) * 1000
        self.assertEqual(response.status_code, 200)
        return {
            "url": url,
            "status": response.status_code,
            "duration_ms": round(duration_ms, 2),
            "queries": len(queries.captured_queries),
            "response_bytes": len(response.content),
        }

    def test_media_list_server_benchmarks(self):
        routes = [
            ("tv_default", "/medialist/tv"),
            ("movie_default", f"/medialist/{MediaTypes.MOVIE.value}"),
            ("anime_default", f"/medialist/{MediaTypes.ANIME.value}"),
            ("manga_default", f"/medialist/{MediaTypes.MANGA.value}"),
            ("manga_no_status", f"/medialist/{MediaTypes.MANGA.value}?status=no_status"),
            ("game_default", f"/medialist/{MediaTypes.GAME.value}"),
            ("book_empty", f"/medialist/{MediaTypes.BOOK.value}"),
            ("comic_empty", f"/medialist/{MediaTypes.COMIC.value}"),
        ]

        for label, url in routes:
            with self.subTest(route=label):
                cache.clear()
                cold = self._measure_route(url)
                warm = self._measure_route(url)
                print(
                    json.dumps(
                        {
                            "label": label,
                            "cold": cold,
                            "warm": warm,
                        },
                        sort_keys=True,
                    ),
                )
                self.assertLessEqual(warm["queries"], cold["queries"])
