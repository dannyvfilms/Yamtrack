from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import Http404, HttpResponse
from django.urls import path
from django.views.decorators.csrf import csrf_protect

import requests

from app.models import Sources
from app.providers import services


@login_not_required
def home(_request):
    """Return a simple home response for error template links."""
    return HttpResponse("home")


@login_not_required
def account_login(_request):
    """Return a simple login response for error template links."""
    return HttpResponse("login")


@login_not_required
def boom_400(_request):
    """Raise a bad-request exception for handler testing."""
    message = "Broken payload"
    raise SuspiciousOperation(message)


@login_not_required
def boom_403(_request):
    """Raise a permission-denied exception for handler testing."""
    message = "Forbidden area"
    raise PermissionDenied(message)


@login_not_required
def boom_404(_request):
    """Raise a not-found exception for handler testing."""
    message = "Missing object"
    raise Http404(message)


@login_not_required
def boom_500(_request):
    """Raise a generic exception for handler testing."""
    message = "Kaboom"
    raise RuntimeError(message)


@login_not_required
def boom_hardcover_401(_request):
    """Raise a Hardcover auth failure for middleware testing."""
    class MockResponse:
        status_code = 401
        headers = {"Content-Type": "application/json"}
        text = '{"error":"Unable to verify token"}'

        def json(self):
            return {"error": "Unable to verify token"}

    error = requests.exceptions.HTTPError(
        "401 Client Error: Unauthorized for url: https://api.hardcover.app/v1/graphql",
        response=MockResponse(),
    )
    raise services.ProviderAPIError(
        Sources.HARDCOVER.value,
        error,
        "Unable to verify token",
    )


@csrf_protect
@login_not_required
def csrf_protected(_request):
    """Return success when CSRF validation passes."""
    return HttpResponse("ok")


handler400 = "app.error_views.bad_request"
handler403 = "app.error_views.permission_denied"
handler404 = "app.error_views.page_not_found"
handler500 = "app.error_views.server_error"

urlpatterns = [
    path("", home, name="home"),
    path("accounts/login/", account_login, name="account_login"),
    path("boom-400/", boom_400),
    path("boom-403/", boom_403),
    path("boom-404/", boom_404),
    path("boom-500/", boom_500),
    path("boom-hardcover-401/", boom_hardcover_401),
    path("csrf-protected/", csrf_protected),
]
