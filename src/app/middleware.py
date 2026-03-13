import logging
import time

from django.db.utils import OperationalError
from django.http import HttpRequest
from django.urls import reverse

from app.discover import tab_cache as discover_tab_cache
from app.error_views import format_exception_traceback, render_error_page
from app.providers import services
from integrations.imports.helpers import is_retryable_error

logger = logging.getLogger(__name__)


class DatabaseRetryMiddleware:
    """Retry requests when database operations fail with retryable errors."""

    def __init__(self, get_response):
        """Initialize the middleware with the get_response callable."""
        self.get_response = get_response

    def __call__(self, request):
        """Process the request with retry logic for database errors."""
        max_retries = 5
        base_delay = 0.1
        backoff = 2.0
        attempt = 0

        while True:
            try:
                return self.get_response(request)
            except OperationalError as error:
                # Only retry retryable errors while under the retry cap.
                if not is_retryable_error(error) or attempt >= max_retries:
                    raise

                if request.method != "GET":
                    logger.exception(
                        "Database error on %s request, not retrying",
                        request.method,
                    )
                    raise

                error_type = "disk I/O" if "i/o" in str(error).lower() else "lock"
                sleep_for = base_delay * (backoff**attempt)
                logger.warning(
                    "Retrying %s after %s error (attempt %s/%s, sleeping %.2fs)",
                    request.path,
                    error_type,
                    attempt + 1,
                    max_retries,
                    sleep_for,
                )
                time.sleep(sleep_for)
                attempt += 1

    def process_exception(self, request, exception):
        """Handle exceptions that weren't caught in __call__."""
        if isinstance(exception, OperationalError) and is_retryable_error(exception):
            error_type = "disk I/O" if "i/o" in str(exception).lower() else "lock"
            logger.error(
                "Database %s error on %s %s: %s",
                error_type,
                request.method,
                request.path,
                exception,
            )
            return render_error_page(
                request,
                "500.html",
                status_code=503,
                page_title="Service Unavailable",
                heading="Service Unavailable",
                error_message=(
                    f"Database {error_type} error. Please try again in a moment."
                ),
                exception=exception,
            )
        return None


class ProviderAPIErrorMiddleware:
    """Middleware to handle ProviderAPIError exceptions."""

    def __init__(self, get_response):
        """Initialize the middleware with the get_response callable."""
        self.get_response = get_response

    def __call__(self, request):
        """Process the request and handle exceptions."""
        return self.get_response(request)

    def process_exception(self, request, exception):
        """Handle exceptions raised during request processing."""
        if isinstance(exception, services.ProviderAPIError):
            return render_error_page(
                request,
                "500.html",
                status_code=500,
                page_title="Server Error",
                heading="Something Went Wrong",
                error_message=str(exception),
                exception=exception,
                extra_lines=[
                    f"Provider: {exception.provider}",
                    f"Provider status: {exception.status_code}",
                ],
            )
        return None


class ErrorCaptureMiddleware:
    """Capture exception details so the 500 handler can render tracebacks."""

    def __init__(self, get_response):
        """Initialize the middleware with the get_response callable."""
        self.get_response = get_response

    def __call__(self, request):
        """Pass the request through the middleware chain."""
        return self.get_response(request)

    def process_exception(self, request, exception):
        """Persist traceback details for the custom error handler."""
        request._yamtrack_captured_exception = exception
        request._yamtrack_captured_traceback = format_exception_traceback(exception)


class DiscoverWarmupMiddleware:
    """Schedule Discover warmup in the background for active users."""

    def __init__(self, get_response):
        """Initialize the middleware with the get_response callable."""
        self.get_response = get_response

    def __call__(self, request):
        """Queue Discover warmup for eligible authenticated page requests."""
        if self._should_warm_discover(request):
            try:
                discover_tab_cache.maybe_schedule_user_warmup(request.user)
            except Exception as error:  # noqa: BLE001
                logger.debug(
                    "Skipping Discover warmup for %s due to error: %s",
                    request.path,
                    error,
                )
        return self.get_response(request)

    def _should_warm_discover(self, request: HttpRequest) -> bool:
        if (
            request.method not in {"GET", "HEAD"}
            or request.headers.get("HX-Request") == "true"
        ):
            return False

        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False) or not getattr(
            user,
            "id",
            None,
        ):
            return False

        path = request.path_info or ""
        if path == "/serviceworker.js" or path.startswith(
            ("/api/", "/admin/", "/static/", "/media/", "/_debug/"),
        ):
            return False
        normalized_path = path.rstrip("/") or "/"
        discover_path = reverse("discover").rstrip("/") or "/"
        if normalized_path != discover_path:
            return False
        if request.GET.get("discover_debug") in {"1", "true", "True"}:
            return False

        accept = request.headers.get("Accept", "")
        return not accept or "text/html" in accept or "*/*" in accept
