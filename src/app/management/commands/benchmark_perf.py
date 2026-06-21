"""Management command to benchmark performance of key endpoints.

Run before and after optimizations to measure improvement:
    python manage.py benchmark_perf --username alice
    python manage.py benchmark_perf --username alice --verbose
"""

import statistics
import time

from django import conf
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, reset_queries
from django.test import Client


ENDPOINTS = [
    ("GET", "/settings/home-screen"),
    ("GET", "/"),
    ("GET", "/medialist/tv"),
    ("GET", "/medialist/season"),
    ("GET", "/health/"),
]

RUNS = 3


class Command(BaseCommand):
    help = "Benchmark response time and query count for key slow endpoints."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            required=True,
            help="Username to authenticate as for the benchmark requests.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print individual SQL queries for each endpoint.",
        )
        parser.add_argument(
            "--runs",
            type=int,
            default=RUNS,
            help=f"Number of times to hit each endpoint (median is reported). Default: {RUNS}.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        username = options["username"]
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"User '{username}' not found.")

        # Enable query logging and allow the test client's default host.
        original_debug = conf.settings.DEBUG
        original_allowed_hosts = conf.settings.ALLOWED_HOSTS
        conf.settings.DEBUG = True
        conf.settings.ALLOWED_HOSTS = list(original_allowed_hosts) + ["testserver", "localhost"]

        client = Client()
        client.force_login(user)

        runs = options["runs"]
        verbose = options["verbose"]

        col_w = [32, 9, 15, 16]
        header = (
            f"{'Endpoint':<{col_w[0]}} | {'Queries':>{col_w[1]}} | "
            f"{'SQL time (ms)':>{col_w[2]}} | {'Wall time (ms)':>{col_w[3]}}"
        )
        divider = "-" * len(header)

        self.stdout.write("")
        self.stdout.write(header)
        self.stdout.write(divider)

        for method, path in ENDPOINTS:
            wall_times = []
            query_counts = []
            sql_times = []
            last_queries = []

            for _ in range(runs):
                reset_queries()
                t0 = time.perf_counter()
                if method == "GET":
                    client.get(path)
                else:
                    client.post(path)
                wall_ms = (time.perf_counter() - t0) * 1000
                captured = list(connection.queries)
                wall_times.append(wall_ms)
                query_counts.append(len(captured))
                sql_times.append(sum(float(q["time"]) * 1000 for q in captured))
                last_queries = captured

            label = f"{method} {path}"
            q_median = int(statistics.median(query_counts))
            sql_median = statistics.median(sql_times)
            wall_median = statistics.median(wall_times)

            self.stdout.write(
                f"{label:<{col_w[0]}} | {q_median:>{col_w[1]}} | "
                f"{sql_median:>{col_w[2]}.1f} | {wall_median:>{col_w[3]}.1f}"
            )

            if verbose and last_queries:
                self.stdout.write("")
                for i, q in enumerate(last_queries, 1):
                    ms = float(q["time"]) * 1000
                    self.stdout.write(f"  [{i:03d}] {ms:6.1f}ms  {q['sql'][:120]}")
                self.stdout.write("")

        self.stdout.write(divider)
        self.stdout.write(
            f"(median of {runs} runs per endpoint; "
            f"first run warms Django caches)"
        )
        self.stdout.write("")

        conf.settings.DEBUG = original_debug
        conf.settings.ALLOWED_HOSTS = original_allowed_hosts
