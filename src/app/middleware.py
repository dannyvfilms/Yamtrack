import logging
import time

from django.db.utils import OperationalError
from django.shortcuts import render

from app.providers import services
from integrations.imports.helpers import is_retryable_error

logger = logging.getLogger(__name__)


class DatabaseRetryMiddleware:
    """Middleware to retry requests when database operations fail with retryable errors."""

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
                # Only retry if it's a retryable error and we haven't exceeded max retries
                if not is_retryable_error(error) or attempt >= max_retries:
                    # Re-raise if not retryable or max retries exceeded
                    raise

                # Only retry GET requests to avoid side effects
                if request.method != "GET":
                    logger.error(
                        "Database error on %s request, not retrying: %s",
                        request.method,
                        error,
                    )
                    raise

                error_type = "disk I/O" if "i/o" in str(error).lower() else "lock"
                sleep_for = base_delay * (backoff**attempt)
                logger.warning(
                    "Retrying %s request due to %s error (attempt %s/%s, sleeping %.2fs)",
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
            # If we get here, retries were exhausted or it was a non-GET request
            error_type = "disk I/O" if "i/o" in str(exception).lower() else "lock"
            logger.error(
                "Database %s error on %s %s: %s",
                error_type,
                request.method,
                request.path,
                exception,
            )
            return render(
                request,
                "500.html",
                {
                    "error_message": (
                        f"Database {error_type} error. Please try again in a moment."
                    ),
                },
                status=503,  # Service Unavailable
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
            return render(
                request,
                "500.html",
                {
                    "error_message": str(exception),
                    "provider": exception.provider,
                },
                status=500,
            )
        return None
