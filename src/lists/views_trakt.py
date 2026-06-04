"""
Views for the Trakt OAuth import flow.

Covers: storing Trakt client credentials, initiating the OAuth authorization,
and handling the OAuth callback to kick off the async list-import task.
"""

import logging
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from integrations.imports import helpers as import_helpers
from integrations.imports import trakt as trakt_imports
from integrations.models import TraktAccount
from lists import tasks as list_tasks
from lists.views_helpers import _get_trakt_credentials

logger = logging.getLogger(__name__)


@login_required
@require_POST
def trakt_lists_credentials(request):
    """Store Trakt client credentials for list imports."""
    client_id = request.POST.get("client_id", "").strip()
    client_secret = request.POST.get("client_secret", "").strip()

    if not client_id or not client_secret:
        messages.error(request, "Trakt client ID and secret are required.")
        return redirect("lists")

    try:
        TraktAccount.objects.update_or_create(
            user=request.user,
            defaults={
                "client_id": import_helpers.encrypt(client_id),
                "client_secret": import_helpers.encrypt(client_secret),
            },
        )
    except Exception as error:
        logger.error("Failed to store Trakt credentials for user %s: %s", request.user.username, error)
        messages.error(request, "Failed to save Trakt credentials. Please try again.")
        return redirect("lists")

    messages.success(request, "Trakt credentials saved. You can now authorize Trakt.")
    return redirect("lists")


@login_required
@require_POST
def trakt_lists_oauth(request):
    """Start the Trakt OAuth flow for list imports."""
    redirect_uri = request.build_absolute_uri(reverse("trakt_lists_callback"))
    credentials = _get_trakt_credentials(request.user)
    if not credentials:
        messages.error(request, "Add your Trakt client ID and secret before authorizing.")
        return redirect("lists")

    client_id, _client_secret = credentials
    state_token = secrets.token_urlsafe(32)
    request.session[state_token] = {"source": "trakt_lists"}
    request.session.modified = True

    # Build query string manually to match the working trakt_oauth pattern
    # This ensures the redirect_uri is sent exactly as registered
    url = "https://trakt.tv/oauth/authorize"
    logger.debug(f"Trakt OAuth redirect URI: {redirect_uri}")

    return redirect(
        f"{url}?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&state={state_token}",
    )


@login_required
@require_GET
def trakt_lists_callback(request):
    """Handle Trakt OAuth callback and import lists."""
    state_token = request.GET.get("state")

    if not state_token:
        logger.error("Trakt OAuth callback missing state parameter")
        messages.error(request, "Invalid Trakt authorization request. Missing state parameter.")
        return redirect("lists")

    state_data = request.session.pop(state_token, None)

    if not state_data:
        logger.error(f"Trakt OAuth callback: state token '{state_token}' not found in session")
        messages.error(
            request,
            "Invalid or expired Trakt authorization request. Please try again - make sure to complete the authorization process without closing your browser.",
        )
        return redirect("lists")

    credentials = _get_trakt_credentials(request.user)
    if not credentials:
        messages.error(request, "Trakt credentials are missing. Please add them and try again.")
        return redirect("lists")

    client_id, client_secret = credentials

    try:
        oauth_callback = trakt_imports.handle_oauth_callback(
            request,
            redirect_uri=request.build_absolute_uri(reverse("trakt_lists_callback")),
            client_id=client_id,
            client_secret=client_secret,
        )
        # Queue the import task asynchronously so we can redirect immediately
        list_tasks.import_trakt_lists_task.delay(
            request.user.id,
            oauth_callback["access_token"],
            client_id=client_id,
        )
        messages.info(request, "Trakt authorization successful. Your lists are being imported in the background.")
    except import_helpers.MediaImportError as error:
        messages.error(request, f"Trakt list import failed: {error}")
        return redirect("lists")

    return redirect("lists")
