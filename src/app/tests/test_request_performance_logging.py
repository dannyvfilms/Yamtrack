"""Tests for RequestPerformanceLoggingMiddleware."""

from django.http import HttpResponse
from django.test import RequestFactory, TestCase
from django.test.utils import override_settings

from app.middleware import RequestPerformanceLoggingMiddleware
from app.models import Item


class RequestPerformanceLoggingMiddlewareTests(TestCase):
    """Verify slow/query-heavy requests are logged and fast ones are not."""

    def setUp(self):
        """Create a request factory."""
        self.factory = RequestFactory()

    def _run(self, view):
        middleware = RequestPerformanceLoggingMiddleware(view)
        return middleware(self.factory.get("/test-path"))

    @override_settings(
        PERF_LOG_ENABLED=True,
        PERF_LOG_SLOW_REQUEST_MS=0,
        PERF_LOG_QUERY_COUNT_THRESHOLD=10_000,
    )
    def test_logs_slow_request(self):
        """A request over the duration threshold is logged."""
        with self.assertLogs("app.middleware", level="INFO") as logs:
            response = self._run(lambda _request: HttpResponse("ok"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("slow_request", logs.output[0])
        self.assertIn("path=/test-path", logs.output[0])

    @override_settings(
        PERF_LOG_ENABLED=True,
        PERF_LOG_SLOW_REQUEST_MS=10_000,
        PERF_LOG_QUERY_COUNT_THRESHOLD=1,
    )
    def test_logs_query_heavy_request(self):
        """A request over the query-count threshold is logged."""
        def view(_request):
            list(Item.objects.all())
            return HttpResponse("ok")

        with self.assertLogs("app.middleware", level="INFO") as logs:
            self._run(view)
        self.assertIn("queries=1", logs.output[0])

    @override_settings(
        PERF_LOG_ENABLED=True,
        PERF_LOG_SLOW_REQUEST_MS=10_000,
        PERF_LOG_QUERY_COUNT_THRESHOLD=10_000,
    )
    def test_fast_request_not_logged(self):
        """A fast, light request is not logged."""
        with self.assertNoLogs("app.middleware", level="INFO"):
            self._run(lambda _request: HttpResponse("ok"))

    @override_settings(PERF_LOG_ENABLED=False, PERF_LOG_SLOW_REQUEST_MS=0)
    def test_disabled_via_setting(self):
        """The middleware is a no-op when disabled."""
        with self.assertNoLogs("app.middleware", level="INFO"):
            response = self._run(lambda _request: HttpResponse("ok"))
        self.assertEqual(response.status_code, 200)
