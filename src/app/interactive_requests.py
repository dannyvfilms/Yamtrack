"""Shared helpers for marking active interactive browser requests."""

from __future__ import annotations

from django.core.cache import cache
from django.http import HttpRequest

INTERACTIVE_REQUEST_CACHE_KEY = "interactive_request_active"
INTERACTIVE_REQUEST_TTL_SECONDS = 30
_INTERACTIVE_REQUEST_EXCLUDED_PREFIXES = (
    "/api/",
    "/media/",
    "/static/",
    "/_debug/",
)
_INTERACTIVE_REQUEST_EXCLUDED_PATHS = {"/serviceworker.js"}


def should_mark_interactive_request(request: HttpRequest) -> bool:
    """Return whether this request should suppress best-effort maintenance work."""
    if request.method not in {"GET", "HEAD"}:
        return False

    path = request.path_info or ""
    if path in _INTERACTIVE_REQUEST_EXCLUDED_PATHS or path.startswith(
        _INTERACTIVE_REQUEST_EXCLUDED_PREFIXES,
    ):
        return False

    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        return True

    accept = request.headers.get("Accept", "")
    return not accept or "text/html" in accept or "*/*" in accept


def mark_interactive_request() -> None:
    """Refresh the shared interactive-request marker."""
    cache.set(
        INTERACTIVE_REQUEST_CACHE_KEY,
        True,
        timeout=INTERACTIVE_REQUEST_TTL_SECONDS,
    )


def interactive_request_active() -> bool:
    """Return whether a recent interactive browser request is active."""
    return bool(cache.get(INTERACTIVE_REQUEST_CACHE_KEY))
