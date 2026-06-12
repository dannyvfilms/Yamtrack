"""Optional browser-level media-list benchmarks.

Run explicitly with:
RUN_PLAYWRIGHT_BENCHMARKS=1 python src/manage.py test app.tests.test_media_list_browser_benchmarks
"""

from __future__ import annotations

import json
import os
import time
import unittest

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from app.models import Item, MediaTypes
from app.tests.test_query_counts import (
    seed_anime_library,
    seed_movie_library,
    seed_tv_library,
    seed_untracked_manga_collection,
)
from lists.models import CustomList, CustomListItem

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional benchmark dependency
    sync_playwright = None


class MediaListBrowserBenchmarkTests(StaticLiveServerTestCase):
    """Measure navigation cost and follow-up release-year requests in a browser."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not os.environ.get("RUN_PLAYWRIGHT_BENCHMARKS"):
            raise unittest.SkipTest("RUN_PLAYWRIGHT_BENCHMARKS is not set")
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is not installed")
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        super().tearDownClass()

    def setUp(self):
        self.page = self.browser.new_page()
        self.release_year_requests: list[str] = []
        self.page.on(
            "requestfinished",
            lambda request: self.release_year_requests.append(request.url)
            if "/api/fetch_release_year" in request.url
            else None,
        )
        self.credentials = {
            "username": f"benchmark-{self._testMethodName}",
            "password": "12345",
        }
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.user.movie_enabled = True
        self.user.tv_enabled = True
        self.user.anime_enabled = True
        self.user.save(update_fields=["movie_enabled", "tv_enabled", "anime_enabled"])
        seed_movie_library(self.user, count=6)
        seed_tv_library(self.user, show_count=3)
        seed_anime_library(self.user, count=6)
        seed_untracked_manga_collection(self.user, count=12)
        self.custom_list = CustomList.objects.create(
            name="Browser Benchmark List",
            owner=self.user,
            visibility="public",
            public_slug="browser-benchmark-list",
        )
        CustomListItem.objects.bulk_create(
            [
                CustomListItem(
                    custom_list=self.custom_list,
                    item=item,
                    added_by=self.user,
                )
                for item in Item.objects.filter(
                    media_type__in=[MediaTypes.MOVIE.value, MediaTypes.TV.value],
                ).order_by("id")[:6]
            ],
        )
        self.page.goto(f"{self.live_server_url}/")
        self.page.get_by_placeholder("Enter your username").fill(
            self.credentials["username"],
        )
        self.page.get_by_placeholder("Enter your password").fill(
            self.credentials["password"],
        )
        self.page.get_by_role("button", name="Sign in").click()

    def tearDown(self):
        self.page.close()

    def _measure_navigation(self, path: str) -> dict[str, int | float | str]:
        self.release_year_requests.clear()
        started_at = time.perf_counter()
        self.page.goto(f"{self.live_server_url}{path}", wait_until="networkidle")
        duration_ms = (time.perf_counter() - started_at) * 1000
        html = self.page.content()
        return {
            "path": path,
            "duration_ms": round(duration_ms, 2),
            "release_year_requests": len(self.release_year_requests),
            "document_bytes": len(html.encode()),
            "rendered_cards": self.page.locator(".media-card").count(),
        }

    def test_media_list_browser_benchmarks(self):
        results = [
            self._measure_navigation("/medialist/movie"),
            self._measure_navigation("/medialist/manga"),
            self._measure_navigation("/medialist/manga?status=no_status"),
        ]
        print(json.dumps(results, sort_keys=True))
        for result in results:
            self.assertGreater(result["document_bytes"], 0)

    def test_app_page_browser_benchmarks(self):
        results = [
            self._measure_navigation("/"),
            self._measure_navigation("/medialist/tv"),
            self._measure_navigation(
                f"/list/{self.custom_list.public_reference}",
            ),
        ]
        print(json.dumps(results, sort_keys=True))
        for result in results:
            self.assertGreater(result["document_bytes"], 0)
