import traceback

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone


def format_exception_traceback(exception: BaseException | None) -> str:
    """Return a formatted traceback for an exception."""
    if exception is None:
        return "Traceback unavailable."

    exception_traceback = getattr(exception, "__traceback__", None)
    if exception_traceback is None:
        return f"{exception.__class__.__name__}: {exception}"

    return "".join(
        traceback.format_exception(
            type(exception),
            exception,
            exception_traceback,
        ),
    ).strip()


def _display_value(value: object, default: str = "-") -> str:
    """Return a safe, printable string for diagnostics."""
    if value is None:
        return default
    if isinstance(value, str) and not value:
        return default
    return str(value)


def _safe_request_value(
    request: HttpRequest,
    attr_name: str,
    default: str = "-",
) -> str:
    """Read a request attribute or bound method without raising."""
    try:
        value = getattr(request, attr_name)
        if callable(value):
            value = value()
    except Exception:  # noqa: BLE001
        return default
    return _display_value(value, default=default)


def build_error_report(
    request: HttpRequest,
    status_code: int,
    title: str,
    *,
    error_message: str | None = None,
    exception: BaseException | None = None,
    traceback_text: str | None = None,
    extra_lines: list[str] | None = None,
) -> str:
    """Build a copyable diagnostic report for an error page."""
    user = getattr(request, "user", None)
    is_authenticated = bool(getattr(user, "is_authenticated", False))
    session = getattr(request, "session", None)
    session_key = getattr(session, "session_key", None)
    meta = getattr(request, "META", {})

    report_lines = [f"Error: {status_code} {title}"]

    if error_message:
        report_lines.append(f"Message: {error_message}")

    if extra_lines:
        report_lines.extend(line for line in extra_lines if line)

    report_lines.extend(
        [
            f"Time (server): {timezone.now().isoformat()}",
            f"Method: {_display_value(getattr(request, 'method', None))}",
            f"Path: {_display_value(getattr(request, 'path', None))}",
            f"Full path: {_safe_request_value(request, 'get_full_path')}",
            f"URL: {_safe_request_value(request, 'build_absolute_uri')}",
            f"Host: {_safe_request_value(request, 'get_host')}",
            f"Scheme: {_display_value(getattr(request, 'scheme', None))}",
            (
                "User: "
                f"{_display_value(getattr(user, 'username', None), 'anonymous')}"
                if is_authenticated
                else "User: anonymous"
            ),
            f"Authenticated: {'yes' if is_authenticated else 'no'}",
            f"Session key present: {'yes' if session_key else 'no'}",
            f"Origin: {_display_value(meta.get('HTTP_ORIGIN'))}",
            f"Referer: {_display_value(meta.get('HTTP_REFERER'))}",
            f"User-Agent: {_display_value(meta.get('HTTP_USER_AGENT'))}",
            f"Remote-Addr: {_display_value(meta.get('REMOTE_ADDR'))}",
            f"X-Forwarded-For: {_display_value(meta.get('HTTP_X_FORWARDED_FOR'))}",
            f"X-Forwarded-Proto: {_display_value(meta.get('HTTP_X_FORWARDED_PROTO'))}",
            "",
            "Traceback:",
            traceback_text or format_exception_traceback(exception),
        ],
    )

    return "\n".join(report_lines)


def render_error_page(
    request: HttpRequest,
    template_name: str,
    *,
    status_code: int,
    page_title: str,
    heading: str,
    error_message: str,
    exception: BaseException | None = None,
    traceback_text: str | None = None,
    error_report_id: str | None = None,
    extra_lines: list[str] | None = None,
    extra_context: dict[str, object] | None = None,
) -> HttpResponse:
    """Render a custom error page with a copyable traceback panel."""
    context = {
        "error_status_code": status_code,
        "error_page_title": page_title,
        "error_heading": heading,
        "error_message": error_message,
        "error_report_id": error_report_id or f"error-report-{status_code}",
        "error_report_title": "Traceback",
        "error_report_note": (
            "Copy this block when opening a ticket so the traceback and request "
            "details are preserved."
        ),
        "error_report": build_error_report(
            request,
            status_code,
            page_title,
            error_message=error_message,
            exception=exception,
            traceback_text=traceback_text,
            extra_lines=extra_lines,
        ),
    }

    if extra_context:
        context.update(extra_context)

    return render(request, template_name, context, status=status_code)


def bad_request(request: HttpRequest, exception: BaseException) -> HttpResponse:
    """Render the custom 400 page."""
    return render_error_page(
        request,
        "400.html",
        status_code=400,
        page_title="Bad Request",
        heading="Bad Request",
        error_message=(
            "The server could not understand the request due to invalid syntax or "
            "missing parameters."
        ),
        exception=exception,
    )


def permission_denied(request: HttpRequest, exception: BaseException) -> HttpResponse:
    """Render the custom 403 page."""
    return render_error_page(
        request,
        "403.html",
        status_code=403,
        page_title="Forbidden",
        heading="Access Forbidden",
        error_message=(
            "You don't have permission to access this resource. Please log in or "
            "contact an administrator."
        ),
        exception=exception,
    )


def page_not_found(request: HttpRequest, exception: BaseException) -> HttpResponse:
    """Render the custom 404 page."""
    return render_error_page(
        request,
        "404.html",
        status_code=404,
        page_title="Page Not Found",
        heading="Page Not Found",
        error_message="The page you are looking for doesn't exist or has been moved.",
        exception=exception,
    )


def server_error(request: HttpRequest) -> HttpResponse:
    """Render the custom 500 page."""
    exception = getattr(request, "_yamtrack_captured_exception", None)
    traceback_text = getattr(request, "_yamtrack_captured_traceback", None)
    return render_error_page(
        request,
        "500.html",
        status_code=500,
        page_title="Server Error",
        heading="Something Went Wrong",
        error_message="We're sorry, but there was an error processing your request.",
        exception=exception,
        traceback_text=traceback_text,
    )


def csrf_failure(
    request: HttpRequest,
    reason: str = "",
    template_name: str = "403_csrf.html",
) -> HttpResponse:
    """Render the custom CSRF failure page."""
    return render_error_page(
        request,
        template_name,
        status_code=403,
        page_title="Forbidden",
        heading="Access Forbidden",
        error_message=(
            "CSRF verification failed for this request. This usually means the CSRF "
            "cookie was missing, expired, or didn't match the form token."
        ),
        traceback_text=(
            "Traceback unavailable. Django rejected the request during CSRF "
            "validation before a view exception was raised."
        ),
        error_report_id="error-report-403-csrf",
        extra_lines=[
            "Type: CSRF verification failed",
            f"Reason: {reason or '(no reason provided)'}",
            (
                "CSRF cookie present: "
                f"{'yes' if getattr(request, 'COOKIES', {}).get('csrftoken') else 'no'}"
            ),
        ],
        extra_context={"reason": reason},
    )
