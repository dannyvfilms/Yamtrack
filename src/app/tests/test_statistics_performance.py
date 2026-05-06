"""Focused benchmark helpers for warmed statistics page requests.

Run with:
    python src/manage.py test app.tests.test_statistics_performance -v 2
"""

import logging
import time
from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from app import statistics_cache
from app.models import Album, Artist, Book, Game, Item, MediaTypes, Movie, Music, Sources, Status

logging.disable(logging.DEBUG)


class StatisticsPerformanceBenchmarks(TestCase):
    """Print warmed-cache `/statistics` metrics for local benchmarking."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="statsperf",
            password="12345",
        )
        played_at = timezone.make_aware(
            datetime.combine(timezone.localdate(), datetime.min.time()),
            timezone.get_current_timezone(),
        )

        movie_item = Item.objects.create(
            media_id="stats-perf-movie-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.MOVIE.value,
            title="Stats Perf Movie",
            image="http://example.com/stats-perf-movie.jpg",
            runtime_minutes=115,
        )
        book_item = Item.objects.create(
            media_id="stats-perf-book-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.BOOK.value,
            title="Stats Perf Book",
            image="http://example.com/stats-perf-book.jpg",
        )
        game_item = Item.objects.create(
            media_id="stats-perf-game-1",
            source=Sources.MANUAL.value,
            media_type=MediaTypes.GAME.value,
            title="Stats Perf Game",
            image="http://example.com/stats-perf-game.jpg",
            platforms=["PC"],
        )
        artist = Artist.objects.create(
            name="Stats Perf Artist",
            image="http://example.com/stats-perf-artist.jpg",
        )
        album = Album.objects.create(
            title="Stats Perf Album",
            artist=artist,
            image="http://example.com/stats-perf-album.jpg",
        )
        track_item = Item.objects.create(
            media_id="stats-perf-track-1",
            source=Sources.MUSICBRAINZ.value,
            media_type=MediaTypes.MUSIC.value,
            title="Stats Perf Track",
            image="http://example.com/stats-perf-album.jpg",
            runtime_minutes=5,
        )

        Movie.objects.create(
            user=cls.user,
            item=movie_item,
            status=Status.COMPLETED.value,
            progress=1,
            score=8,
            start_date=played_at,
            end_date=played_at,
        )
        Book.objects.create(
            user=cls.user,
            item=book_item,
            status=Status.IN_PROGRESS.value,
            progress=180,
            start_date=played_at,
            end_date=played_at,
        )
        Game.objects.create(
            user=cls.user,
            item=game_item,
            status=Status.IN_PROGRESS.value,
            progress=64,
            start_date=played_at,
            end_date=played_at,
        )
        Music.objects.create(
            user=cls.user,
            item=track_item,
            artist=artist,
            album=album,
            status=Status.COMPLETED.value,
            start_date=played_at,
            end_date=played_at,
        )

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def test_statistics_view_warm_cache_benchmark(self):
        """Print warmed-cache wall clock, SQL, query count, and HTML size."""
        statistics_cache.invalidate_statistics_cache(self.user.id)
        statistics_cache.refresh_statistics_cache(self.user.id, "All Time")

        start = time.perf_counter()
        with CaptureQueriesContext(connection) as captured_queries:
            response = self.client.get(
                reverse("statistics") + "?start-date=all&end-date=all&compare=none",
            )
        elapsed_ms = (time.perf_counter() - start) * 1000
        sql_ms = sum(float(query["time"]) for query in captured_queries.captured_queries) * 1000
        html_bytes = len(response.content)

        print("\n[PERF] statistics warm cache:")
        print(f"  Wall-clock: {elapsed_ms:.0f}ms")
        print(f"  SQL time:   {sql_ms:.0f}ms")
        print(f"  Queries:    {len(captured_queries.captured_queries)}")
        print(f"  HTML bytes: {html_bytes}")

        self.assertEqual(response.status_code, 200)
