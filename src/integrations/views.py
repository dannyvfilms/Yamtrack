"""Contains views for importing and exporting media data from various sources."""

import json
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

import users
from app.log_safety import exception_summary
from integrations import exports, lastfm_api, pocketcasts_api, tasks
from integrations import plex as plex_api
from integrations.imports import anilist, helpers, simkl, trakt
from integrations.models import AudiobookshelfAccount, LastFMAccount, PlexAccount, PocketCastsAccount
from integrations.lastfm_api import LastFMAPIError, LastFMClientError, LastFMRateLimitError
from integrations.plex_watchlist import WATCHLIST_SYNC_INTERVAL_MINUTES, WATCHLIST_TASK_NAME
from integrations.pocketcasts_api import PocketCastsAuthError
from integrations.imports.audiobookshelf import AudiobookshelfAuthError, AudiobookshelfClient
from integrations.webhooks import emby, jellyfin
from integrations.webhooks import jellyseerr as jellyseerr_webhooks
from integrations.webhooks import plex as plex_webhooks

logger = logging.getLogger(__name__)


def _read_uploaded_file(file):
    """Read uploaded file bytes for safe Celery serialization."""
    file.seek(0)
    return file.read()


def _save_plex_usernames(user, raw_usernames):
    """Persist de-duplicated Plex usernames for webhook filtering."""
    if raw_usernames is None:
        return

    username_list = [u.strip() for u in raw_usernames.split(",") if u.strip()]
    seen = set()
    deduplicated = [u for u in username_list if not (u.lower() in seen or seen.add(u.lower()))]
    cleaned_usernames = ", ".join(deduplicated)
    if cleaned_usernames != user.plex_usernames:
        user.plex_usernames = cleaned_usernames
        user.save(update_fields=["plex_usernames"])


def _plex_watchlist_task_filter(user_id):
    """Match a user's watchlist task regardless of JSON spacing/quotes."""
    return Q(kwargs__contains=f"'user_id': {user_id},") | Q(
        kwargs__contains=f"'user_id': {user_id}" + "}",
    ) | Q(
        kwargs__contains=f'"user_id": {user_id},',
    ) | Q(
        kwargs__contains=f'"user_id": {user_id}' + "}",
    )


def _ensure_plex_watchlist_schedule(user, plex_account):
    """Create or enable the per-user Plex watchlist interval schedule."""
    from django_celery_beat.models import IntervalSchedule, PeriodicTask

    next_interval_start = timezone.now() + timedelta(minutes=WATCHLIST_SYNC_INTERVAL_MINUTES)
    interval, _ = IntervalSchedule.objects.get_or_create(
        every=WATCHLIST_SYNC_INTERVAL_MINUTES,
        period=IntervalSchedule.MINUTES,
    )
    task_filter = PeriodicTask.objects.filter(
        _plex_watchlist_task_filter(user.id),
        task=WATCHLIST_TASK_NAME,
    )
    existing_task = task_filter.first()
    if existing_task:
        was_enabled = existing_task.enabled
        updated_fields = []
        desired_name = (
            f"{WATCHLIST_TASK_NAME} for "
            f"{plex_account.plex_username or user.username} "
            f"(every {WATCHLIST_SYNC_INTERVAL_MINUTES} minutes)"
        )
        desired_kwargs = json.dumps({"user_id": user.id, "mode": "watchlist"})
        if existing_task.name != desired_name:
            existing_task.name = desired_name
            updated_fields.append("name")
        if existing_task.interval_id != interval.id:
            existing_task.interval = interval
            updated_fields.append("interval")
        if existing_task.crontab_id is not None:
            existing_task.crontab = None
            updated_fields.append("crontab")
        if existing_task.clocked_id is not None:
            existing_task.clocked = None
            updated_fields.append("clocked")
        if existing_task.solar_id is not None:
            existing_task.solar = None
            updated_fields.append("solar")
        if existing_task.one_off:
            existing_task.one_off = False
            updated_fields.append("one_off")
        if existing_task.kwargs != desired_kwargs:
            existing_task.kwargs = desired_kwargs
            updated_fields.append("kwargs")
        if not existing_task.enabled:
            existing_task.enabled = True
            updated_fields.append("enabled")
        if existing_task.start_time is None or not was_enabled:
            existing_task.start_time = next_interval_start
            updated_fields.append("start_time")
        if updated_fields:
            existing_task.save(update_fields=updated_fields)
        return existing_task

    return PeriodicTask.objects.create(
        name=(
            f"{WATCHLIST_TASK_NAME} for "
            f"{plex_account.plex_username or user.username} "
            f"(every {WATCHLIST_SYNC_INTERVAL_MINUTES} minutes)"
        ),
        task=WATCHLIST_TASK_NAME,
        interval=interval,
        kwargs=json.dumps({"user_id": user.id, "mode": "watchlist"}),
        start_time=next_interval_start,
        enabled=True,
    )


def _disable_plex_watchlist_schedule(user):
    """Delete any per-user Plex watchlist periodic tasks."""
    from django_celery_beat.models import PeriodicTask

    return PeriodicTask.objects.filter(
        _plex_watchlist_task_filter(user.id),
        task=WATCHLIST_TASK_NAME,
    ).delete()


def _ensure_lastfm_poll_schedule():
    """Create or update the shared Last.fm polling schedule."""
    from django_celery_beat.models import IntervalSchedule, PeriodicTask

    poll_interval_minutes = getattr(settings, "LASTFM_POLL_INTERVAL_MINUTES", 15)
    interval, _ = IntervalSchedule.objects.get_or_create(
        every=poll_interval_minutes,
        period=IntervalSchedule.MINUTES,
    )
    task_name = f"Poll Last.fm for all users (every {poll_interval_minutes} minutes)"
    existing_task = PeriodicTask.objects.filter(task="Poll Last.fm for all users").first()

    if existing_task:
        updated_fields = []
        if existing_task.name != task_name:
            existing_task.name = task_name
            updated_fields.append("name")
        if existing_task.interval_id != interval.id:
            existing_task.interval = interval
            updated_fields.append("interval")
        if not existing_task.enabled:
            existing_task.enabled = True
            updated_fields.append("enabled")
        if existing_task.start_time is None:
            existing_task.start_time = timezone.now()
            updated_fields.append("start_time")
        if updated_fields:
            existing_task.save(update_fields=updated_fields)
        return existing_task, poll_interval_minutes

    return PeriodicTask.objects.create(
        name=task_name,
        task="Poll Last.fm for all users",
        interval=interval,
        start_time=timezone.now(),
        enabled=True,
    ), poll_interval_minutes


def _save_lastfm_history_reset(account, cutoff_uts: int):
    """Persist a fresh Last.fm history import state."""
    account.reset_history_import(cutoff_uts)
    account.save(
        update_fields=[
            "history_import_status",
            "history_import_cutoff_uts",
            "history_import_next_page",
            "history_import_total_pages",
            "history_import_started_at",
            "history_import_completed_at",
            "history_import_last_error_message",
        ],
    )


@require_POST
def trakt_oauth(request):
    """View for initiating Trakt OAuth2 authorization flow."""
    redirect_uri = request.build_absolute_uri(reverse("import_trakt_private"))
    url = "https://trakt.tv/oauth/authorize"
    state = {
        "mode": request.POST["mode"],
        "frequency": request.POST["frequency"],
        "time": request.POST["time"],
    }
    state_token = secrets.token_urlsafe(32)
    request.session[state_token] = state
    return redirect(
        f"{url}?client_id={settings.TRAKT_API}&redirect_uri={redirect_uri}&response_type=code&state={state_token}",
    )


@require_GET
def import_trakt_private(request):
    """View for handling Trakt OAuth2 callback and scheduling private import."""
    oauth_callback = trakt.handle_oauth_callback(request)
    enc_token = helpers.encrypt(oauth_callback["refresh_token"])
    state_token = request.GET["state"]

    frequency = request.session[state_token]["frequency"]
    mode = request.session[state_token]["mode"]
    import_time = request.session[state_token]["time"]

    if frequency == "once":
        tasks.import_trakt.delay(
            token=enc_token,
            user_id=request.user.id,
            mode=mode,
            username=oauth_callback["username"],
        )
        messages.info(request, "The task to import media from Trakt has been queued.")
    else:
        helpers.create_import_schedule(
            oauth_callback["username"],
            request,
            mode,
            frequency,
            import_time,
            "Trakt",
            token=enc_token,
        )
    return redirect("import_data")


@require_POST
def import_trakt_public(request):
    """View for importing Trakt data using public username."""
    username = request.POST.get("user")
    if not username:
        messages.error(request, "Trakt username is required.")
        return redirect("import_data")

    mode = request.POST["mode"]
    frequency = request.POST["frequency"]
    import_time = request.POST["time"]

    if frequency == "once":
        tasks.import_trakt.delay(
            user_id=request.user.id,
            mode=mode,
            username=username,
        )
        messages.info(request, "The task to import media from Trakt has been queued.")
    else:
        helpers.create_import_schedule(
            username=username,
            request=request,
            mode=mode,
            frequency=frequency,
            import_time=import_time,
            source="Trakt",
        )
    return redirect("import_data")


@require_POST
def plex_connect(request):
    """Initiate Plex authentication via the pin-based flow."""
    redirect_uri = request.build_absolute_uri(reverse("plex_callback"))
    state_token = secrets.token_urlsafe(16)

    try:
        pin = plex_api.create_pin()
    except plex_api.PlexClientError as exc:
        messages.error(request, f"Could not start Plex connection: {exc}")
        return redirect("import_data")
    except Exception as exc:  # pragma: no cover - defensive
        messages.error(request, f"Unexpected Plex error: {exc}")
        return redirect("import_data")

    request.session[state_token] = {
        "plex_pin_id": pin["id"],
        "plex_pin_code": pin["code"],
    }

    auth_url = plex_api.build_auth_url(pin["code"], f"{redirect_uri}?state={state_token}")
    return redirect(auth_url)


@require_GET
def plex_callback(request):
    """Handle Plex auth callback and persist the token."""
    state_token = request.GET.get("state")
    state_data = request.session.pop(state_token, None)

    if not state_data:
        messages.error(request, "Invalid or expired Plex authorization request.")
        return redirect("import_data")

    pin_id = state_data.get("plex_pin_id")
    try:
        plex_token = plex_api.poll_pin(pin_id)
    except plex_api.PlexAuthError as exc:
        messages.error(request, f"Plex authorization failed: {exc}")
        return redirect("import_data")
    except plex_api.PlexClientError as exc:  # pragma: no cover - defensive
        messages.error(request, f"Could not complete Plex authorization: {exc}")
        return redirect("import_data")
    except Exception as exc:  # pragma: no cover - defensive
        messages.error(request, f"Unexpected Plex response: {exc}")
        return redirect("import_data")

    try:
        account = plex_api.fetch_account(plex_token)
    except plex_api.PlexAuthError as exc:
        messages.error(request, f"Plex rejected the token: {exc}")
        return redirect("import_data")
    except plex_api.PlexClientError as exc:  # pragma: no cover - defensive
        messages.error(request, f"Could not read Plex account details: {exc}")
        return redirect("import_data")
    except Exception as exc:  # pragma: no cover - defensive
        messages.error(request, f"Unexpected Plex account response: {exc}")
        return redirect("import_data")

    sections: list[dict] = []
    try:
        sections = plex_api.list_sections(plex_token)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Connected to Plex but could not fetch libraries: %s", exception_summary(exc))
        messages.warning(
            request,
            "Connected to Plex, but could not load libraries yet. You can refresh from the import page.",
        )

    # Keep webhook allow list in sync
    username = (account.get("username") or "").strip()
    if username:
        existing = [
            u.strip()
            for u in (request.user.plex_usernames or "").split(",")
            if u.strip()
        ]
        if username.lower() not in [u.lower() for u in existing]:
            request.user.plex_usernames = ", ".join(existing + [username])
            request.user.save(update_fields=["plex_usernames"])

    defaults = {
        "plex_token": plex_token,
        "plex_username": account.get("username") or "",
        "plex_account_id": account.get("id") or "",
        "sections": sections,
        "sections_refreshed_at": timezone.now(),
    }

    if sections:
        defaults["server_name"] = sections[0].get("server_name")
        defaults["machine_identifier"] = sections[0].get("machine_identifier")

    PlexAccount.objects.update_or_create(
        user=request.user,
        defaults=defaults,
    )

    account_username = account.get("username") or "your Plex account"
    messages.success(request, f"Connected to Plex as {account_username}.")
    return redirect("import_data")


@require_POST
def plex_disconnect(request):
    """Remove stored Plex credentials."""
    _disable_plex_watchlist_schedule(request.user)
    PlexAccount.objects.filter(user=request.user).delete()
    messages.info(request, "Disconnected Plex.")
    return redirect("import_data")


@require_POST
def import_plex(request):
    """Queue a Plex history import for the current user."""
    plex_account = getattr(request.user, "plex_account", None)
    if not plex_account:
        messages.error(request, "Connect Plex before importing.")
        return redirect("import_data")

    library = request.POST.get("library") or "all"
    mode = request.POST.get("mode", "new")
    frequency = request.POST.get("frequency", "once")
    import_time = request.POST.get("time", "00:00")
    raw_usernames = request.POST.get("plex_usernames", "")

    _save_plex_usernames(request.user, raw_usernames)

    if mode == "watchlist":
        _ensure_plex_watchlist_schedule(request.user, plex_account)
        plex_account.watchlist_sync_enabled = True
        plex_account.save(update_fields=["watchlist_sync_enabled"])
        tasks.sync_plex_watchlist.delay(
            user_id=request.user.id,
            mode="watchlist",
        )
        messages.info(
            request,
            (
                "Plex watchlist sync queued. "
                f"Recurring syncs will run every {WATCHLIST_SYNC_INTERVAL_MINUTES} minutes."
            ),
        )
        return redirect("import_data")

    # Handle "update_collection" mode separately
    if mode == "update_collection":
        if frequency != "once":
            messages.error(request, "Collection update mode only supports one-time execution.")
            return redirect("import_data")

        tasks.update_collection_metadata_from_plex.delay(
            library=library,
            user_id=request.user.id,
        )
        messages.info(request, "The task to update collection metadata from Plex has been queued.")
        return redirect("import_data")

    if frequency != "once":
        helpers.create_import_schedule(
            username=plex_account.plex_username or request.user.username,
            request=request,
            mode=mode,
            frequency=frequency,
            import_time=import_time,
            source="Plex",
            extra_kwargs={"library": library},
        )
        return redirect("import_data")

    tasks.import_plex.delay(
        library=library,
        user_id=request.user.id,
        mode=mode,
    )
    messages.info(request, "The task to import media from Plex has been queued.")
    return redirect("import_data")


@require_POST
def plex_disable_watchlist(request):
    """Disable recurring Plex watchlist sync for the current user."""
    plex_account = getattr(request.user, "plex_account", None)
    if not plex_account:
        messages.error(request, "Connect Plex before changing watchlist sync.")
        return redirect("import_data")

    _disable_plex_watchlist_schedule(request.user)
    if plex_account.watchlist_sync_enabled:
        plex_account.watchlist_sync_enabled = False
        plex_account.save(update_fields=["watchlist_sync_enabled"])

    messages.info(request, "Disabled Plex watchlist sync.")
    return redirect("import_data")


@require_POST
def simkl_oauth(request):
    """View for initiating the SIMKL OAuth2 authorization flow."""
    redirect_uri = request.build_absolute_uri(reverse("import_simkl_private"))
    url = "https://simkl.com/oauth/authorize"

    state = {
        "mode": request.POST["mode"],
        "frequency": request.POST["frequency"],
        "time": request.POST["time"],
    }
    state_token = secrets.token_urlsafe(32)
    request.session[state_token] = state

    return redirect(
        f"{url}?client_id={settings.SIMKL_ID}&redirect_uri={redirect_uri}&response_type=code&state={state_token}",
    )


@require_GET
def import_simkl_private(request):
    """View for getting the SIMKL OAuth2 token."""
    oauth_callback = simkl.get_token(request)
    enc_token = helpers.encrypt(oauth_callback["access_token"])
    state_token = request.GET["state"]

    frequency = request.session[state_token]["frequency"]
    mode = request.session[state_token]["mode"]
    import_time = request.session[state_token]["time"]

    if frequency == "once":
        tasks.import_simkl.delay(token=enc_token, user_id=request.user.id, mode=mode)
        messages.info(request, "The task to import media from Simkl has been queued.")
    else:
        helpers.create_import_schedule(
            oauth_callback["username"],
            request,
            mode,
            frequency,
            import_time,
            "SIMKL",
            token=enc_token,
        )

    return redirect("import_data")


@require_POST
def import_mal(request):
    """View for importing anime and manga data from MyAnimeList."""
    username = request.POST.get("user")
    if not username:
        messages.error(request, "MyAnimeList username is required.")
        return redirect("import_data")

    mode = request.POST["mode"]
    frequency = request.POST["frequency"]

    if frequency == "once":
        tasks.import_mal.delay(username=username, user_id=request.user.id, mode=mode)
        messages.info(
            request,
            "The task to import media from MyAnimeList has been queued.",
        )
    else:
        import_time = request.POST["time"]
        helpers.create_import_schedule(
            username,
            request,
            mode,
            frequency,
            import_time,
            "MyAnimeList",
        )
    return redirect("import_data")


@require_POST
def anilist_oauth(request):
    """Initiate AniList OAuth flow."""
    redirect_uri = request.build_absolute_uri(reverse("import_anilist_private"))
    url = "https://anilist.co/api/v2/oauth/authorize"
    state = {
        "mode": request.POST["mode"],
        "frequency": request.POST["frequency"],
        "time": request.POST["time"],
    }

    state_token = secrets.token_urlsafe(32)
    request.session[state_token] = state

    return redirect(
        f"{url}?client_id={settings.ANILIST_ID}&redirect_uri={redirect_uri}&response_type=code&state={state_token}",
    )


@require_GET
def import_anilist_private(request):
    """View for getting the AniList OAuth2 token."""
    oauth_callback = anilist.get_token(request)
    enc_token = helpers.encrypt(oauth_callback["access_token"])
    state_token = request.GET["state"]
    username = oauth_callback["username"]

    if not username:
        messages.error(request, "AniList username is required.")
        return redirect("import_data")

    frequency = request.session[state_token]["frequency"]
    mode = request.session[state_token]["mode"]
    import_time = request.session[state_token]["time"]

    if frequency == "once":
        tasks.import_anilist.delay(
            user_id=request.user.id,
            mode=mode,
            username=username,
            token=enc_token,
        )
        messages.info(request, "AniList import queued.")
    else:
        helpers.create_import_schedule(
            username=username,
            request=request,
            mode=mode,
            frequency=frequency,
            import_time=import_time,
            source="AniList",
            token=enc_token,
        )
    return redirect("import_data")


@require_POST
def import_anilist_public(request):
    """View for importing anime and manga data from AniList."""
    username = request.POST.get("user")
    if not username:
        messages.error(request, "AniList username is required.")
        return redirect("import_data")

    mode = request.POST["mode"]
    frequency = request.POST["frequency"]
    import_time = request.POST["time"]

    if frequency == "once":
        tasks.import_anilist.delay(
            user_id=request.user.id,
            mode=mode,
            username=username,
        )
        messages.info(request, "AniList import queued.")
    else:
        helpers.create_import_schedule(
            username=username,
            request=request,
            mode=mode,
            frequency=frequency,
            import_time=import_time,
            source="AniList",
        )
    return redirect("import_data")


@require_POST
def import_kitsu(request):
    """View for importing anime and manga data from Kitsu by user ID."""
    kitsu_id = request.POST.get("user")
    if not kitsu_id:
        messages.error(request, "Kitsu user ID is required.")
        return redirect("import_data")

    mode = request.POST["mode"]
    frequency = request.POST["frequency"]

    if frequency == "once":
        tasks.import_kitsu.delay(username=kitsu_id, user_id=request.user.id, mode=mode)
        messages.info(request, "The task to import media from Kitsu has been queued.")
    else:
        import_time = request.POST["time"]
        helpers.create_import_schedule(
            kitsu_id,
            request,
            mode,
            frequency,
            import_time,
            "Kitsu",
        )
    return redirect("import_data")


@require_POST
def import_yamtrack(request):
    """View for importing anime and manga data from Yamtrack CSV."""
    file = request.FILES.get("yamtrack_csv")

    if not file:
        messages.error(request, "Yamtrack CSV file is required.")
        return redirect("import_data")

    mode = request.POST["mode"]
    tasks.import_yamtrack.delay(
        file=_read_uploaded_file(file),
        user_id=request.user.id,
        mode=mode,
    )
    messages.info(
        request,
        "The task to import media from Yamtrack CSV file has been queued.",
    )
    return redirect("import_data")


@require_POST
def import_hltb(request):
    """View for importing game date from HowLongToBeat."""
    file = request.FILES.get("hltb_csv")

    if not file:
        messages.error(request, "HowLongToBeat CSV file is required.")
        return redirect("import_data")

    mode = request.POST["mode"]
    tasks.import_hltb.delay(
        file=_read_uploaded_file(file),
        user_id=request.user.id,
        mode=mode,
    )
    messages.info(
        request,
        "The task to import media from HowLongToBeat CSV file has been queued.",
    )
    return redirect("import_data")


@require_POST
def import_steam(request):
    """View for importing game data from Steam."""
    steam_id = request.POST.get("user")
    if not steam_id:
        messages.error(request, "Steam ID is required.")
        return redirect("import_data")

    mode = request.POST["mode"]
    frequency = request.POST["frequency"]

    if frequency == "once":
        tasks.import_steam.delay(username=steam_id, user_id=request.user.id, mode=mode)
        messages.info(request, "The task to import media from Steam has been queued.")
    else:
        import_time = request.POST["time"]
        helpers.create_import_schedule(
            steam_id,
            request,
            mode,
            frequency,
            import_time,
            "Steam",
        )
    return redirect("import_data")




@require_POST
def audiobookshelf_connect(request):
    """Connect Audiobookshelf account using base URL + API token."""
    base_url = request.POST.get("base_url", "").strip()
    api_token = request.POST.get("api_token", "").strip()

    if not base_url or not api_token:
        messages.error(request, "Audiobookshelf base URL and API token are required.")
        return redirect("import_data")

    try:
        client = AudiobookshelfClient(base_url, api_token)
        client.get_me()
    except AudiobookshelfAuthError as exc:
        messages.error(request, str(exc))
        return redirect("import_data")
    except Exception as exc:
        messages.error(request, f"Failed to connect to Audiobookshelf: {exc}")
        return redirect("import_data")

    AudiobookshelfAccount.objects.update_or_create(
        user=request.user,
        defaults={
            "base_url": base_url,
            "api_token": helpers.encrypt(api_token),
            "connection_broken": False,
            "last_error_message": "",
        },
    )

    tasks.import_audiobookshelf.delay(user_id=request.user.id, mode="new")
    messages.success(request, "Connected Audiobookshelf. Initial import queued.")
    return redirect("import_data")


@require_POST
def audiobookshelf_disconnect(request):
    """Disconnect Audiobookshelf integration."""
    from django_celery_beat.models import PeriodicTask

    PeriodicTask.objects.filter(
        task="Import from Audiobookshelf (Recurring)",
        kwargs__contains=f'"user_id": {request.user.id}',
    ).delete()
    AudiobookshelfAccount.objects.filter(user=request.user).delete()
    messages.info(request, "Disconnected Audiobookshelf.")
    return redirect("import_data")


@require_POST
def import_audiobookshelf(request):
    """Queue Audiobookshelf import and ensure recurring schedule exists."""
    from django_celery_beat.models import CrontabSchedule, PeriodicTask

    account = getattr(request.user, "audiobookshelf_account", None)
    if not account:
        messages.error(request, "Connect Audiobookshelf before importing.")
        return redirect("import_data")

    tasks.import_audiobookshelf.delay(user_id=request.user.id, mode="new")

    existing_task = PeriodicTask.objects.filter(
        task="Import from Audiobookshelf (Recurring)",
        kwargs__contains=f'"user_id": {request.user.id}',
        enabled=True,
    ).first()

    if not existing_task:
        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute=0,
            hour="*/2",
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
            timezone=timezone.get_default_timezone(),
        )
        PeriodicTask.objects.create(
            name=f"Import from Audiobookshelf for {request.user.username} (every 2 hours)",
            task="Import from Audiobookshelf (Recurring)",
            crontab=crontab,
            kwargs=json.dumps({"user_id": request.user.id}),
            start_time=timezone.now(),
            enabled=True,
        )

    messages.info(request, "Audiobookshelf import queued.")
    return redirect("import_data")

@require_POST
def pocketcasts_connect(request):
    """Connect Pocket Casts account using email and password."""
    email = request.POST.get("email", "").strip()
    password = request.POST.get("password", "").strip()

    if not email:
        messages.error(request, "Email is required.")
        return redirect("import_data")

    if not password:
        messages.error(request, "Password is required.")
        return redirect("import_data")

    # Attempt to login with credentials
    try:
        logger.debug("Attempting Pocket Casts login with configured credentials")
        login_response = pocketcasts_api.login(email, password)
        access_token = login_response["accessToken"]
        refresh_token = login_response.get("refreshToken", "")

        logger.info("Successfully logged in to Pocket Casts for user %s", request.user.username)
    except PocketCastsAuthError as auth_error:
        logger.error("Pocket Casts login failed: %s", auth_error)
        messages.error(
            request,
            "Invalid email or password. For accounts created via 'Sign in with Apple' or 'Sign in with Google', "
            "please set a password first using Pocket Casts' 'Forgot Password' feature, then enter your email and new password here.",
        )
        return redirect("import_data")
    except Exception as e:
        logger.exception("Failed to login to Pocket Casts")
        messages.error(request, f"Failed to connect to Pocket Casts: {e}")
        return redirect("import_data")

    # Encrypt and store credentials and tokens
    try:
        encrypted_email = helpers.encrypt(email)
        encrypted_password = helpers.encrypt(password)
        encrypted_access = helpers.encrypt(access_token)
        encrypted_refresh = helpers.encrypt(refresh_token) if refresh_token else None

        # Parse expiration from JWT
        token_expires_at = pocketcasts_api.parse_token_expiration(access_token)

        PocketCastsAccount.objects.update_or_create(
            user=request.user,
            defaults={
                "email": encrypted_email,
                "password": encrypted_password,
                "access_token": encrypted_access,
                "refresh_token": encrypted_refresh,
                "token_expires_at": token_expires_at,
                "connection_broken": False,  # Clear broken flag on successful connection
            },
        )

        # Set up 2-hour recurring import if it doesn't exist
        from django_celery_beat.models import CrontabSchedule, PeriodicTask

        existing_task = PeriodicTask.objects.filter(
            task="Import from Pocket Casts (Recurring)",
            kwargs__contains=f'"user_id": {request.user.id}',
            enabled=True,
        ).first()

        if not existing_task:
            # Create crontab for every 2 hours (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22)
            crontab, _ = CrontabSchedule.objects.get_or_create(
                minute=0,
                hour="*/2",
                day_of_week="*",
                day_of_month="*",
                month_of_year="*",
                timezone=timezone.get_default_timezone(),
            )

            task_name = f"Import from Pocket Casts for {request.user.username} (every 2 hours)"
            PeriodicTask.objects.create(
                name=task_name,
                task="Import from Pocket Casts (Recurring)",
                crontab=crontab,
                kwargs=json.dumps({
                    "user_id": request.user.id,
                }),
                start_time=timezone.now(),
                enabled=True,
            )

            # Run initial import
            tasks.import_pocketcasts.delay(
                user_id=request.user.id,
                mode="new",
            )
            messages.success(request, "Connected to Pocket Casts successfully. Initial import queued. Recurring imports will run every 2 hours.")
        else:
            messages.success(request, "Connected to Pocket Casts successfully.")
    except Exception as e:
        logger.error("Failed to store Pocket Casts credentials: %s", e)
        messages.error(request, f"Failed to store credentials: {e}")

    return redirect("import_data")


@require_POST
def pocketcasts_disconnect(request):
    """Remove stored Pocket Casts credentials and delete periodic import task."""
    from django_celery_beat.models import PeriodicTask

    # Delete periodic import task if it exists
    PeriodicTask.objects.filter(
        task="Import from Pocket Casts (Recurring)",
        kwargs__contains=f'"user_id": {request.user.id}',
    ).delete()

    # Clear all credentials (full disconnect)
    PocketCastsAccount.objects.filter(user=request.user).delete()
    messages.info(request, "Disconnected Pocket Casts and removed scheduled imports.")
    return redirect("import_data")


@require_POST
def lastfm_connect(request):
    """Connect Last.fm account using username."""
    username = request.POST.get("lastfm_username", "").strip()

    if not username:
        messages.error(request, "Last.fm username is required.")
        return redirect("import_data")

    # Validate username by making a test API call
    try:
        logger.debug("Validating Last.fm username: %s", username)
        # Make a minimal API call to verify user exists and has public scrobbles
        lastfm_api.get_recent_tracks(username=username, limit=1, page=1)
        logger.info("Successfully validated Last.fm username: %s", username)
    except LastFMClientError as e:
        logger.error("Last.fm username validation failed: %s", e)
        messages.error(
            request,
            f"Invalid Last.fm username or user not found. Please check your username and ensure your scrobbles are public.",
        )
        return redirect("import_data")
    except LastFMRateLimitError as e:
        logger.error("Last.fm rate limit during validation: %s", e)
        messages.error(
            request,
            "Last.fm API rate limit exceeded. Please try again in a few moments.",
        )
        return redirect("import_data")
    except LastFMAPIError as e:
        logger.error("Last.fm API error during validation: %s", e)
        messages.error(request, f"Failed to connect to Last.fm: {e}")
        return redirect("import_data")
    except Exception as e:
        logger.error("Unexpected error validating Last.fm username: %s", e, exc_info=True)
        messages.error(request, f"Failed to connect to Last.fm: {e}")
        return redirect("import_data")

    # Store username and initialize sync state
    try:
        import time

        current_timestamp = int(time.time())

        lastfm_account, _ = LastFMAccount.objects.update_or_create(
            user=request.user,
            defaults={
                "lastfm_username": username,
                "last_fetch_timestamp_uts": current_timestamp,
                "connection_broken": False,
                "failure_count": 0,
                "last_error_code": "",
                "last_error_message": "",
                "last_failed_at": None,
            },
        )
        _save_lastfm_history_reset(lastfm_account, current_timestamp - 1)

        _ensure_lastfm_poll_schedule()
        poll_interval_minutes = getattr(settings, "LASTFM_POLL_INTERVAL_MINUTES", 15)
        tasks.poll_lastfm_for_user.delay(user_id=request.user.id)
        tasks.import_lastfm_history.delay(user_id=request.user.id, reset=False)
        messages.success(
            request,
            (
                "Connected to Last.fm successfully. Recurring syncs will run every "
                f"{poll_interval_minutes} minutes. Initial sync and full history import queued."
            ),
        )
    except Exception as e:
        logger.error("Failed to store Last.fm connection: %s", e, exc_info=True)
        messages.error(request, f"Failed to save Last.fm connection: {e}")

    return redirect("import_data")


@require_POST
def lastfm_disconnect(request):
    """Remove Last.fm connection."""
    LastFMAccount.objects.filter(user=request.user).delete()

    # If no users left, we could disable the periodic task, but we'll leave it
    # running - it will just skip if no users are connected
    # This allows the task to stay configured for future users

    messages.info(request, "Disconnected Last.fm.")
    return redirect("import_data")


@require_POST
def poll_lastfm_manual(request):
    """Manually trigger Last.fm polling for the current user."""
    lastfm_account = getattr(request.user, "lastfm_account", None)
    if not lastfm_account:
        messages.error(request, "Connect Last.fm before syncing.")
        return redirect("import_data")

    lastfm_account.refresh_from_db()
    if not lastfm_account.is_connected:
        messages.error(request, "Last.fm connection is broken. Please reconnect.")
        return redirect("import_data")

    tasks.poll_lastfm_for_user.delay(user_id=request.user.id)
    messages.info(request, "Last.fm sync queued. Scrobbles will be imported shortly.")
    return redirect("import_data")


@require_POST
def import_lastfm_history_manual(request):
    """Queue or rerun a full Last.fm history import for the current user."""
    lastfm_account = getattr(request.user, "lastfm_account", None)
    if not lastfm_account:
        messages.error(request, "Connect Last.fm before importing history.")
        return redirect("import_data")

    lastfm_account.refresh_from_db()
    if not lastfm_account.is_connected:
        messages.error(request, "Last.fm connection is broken. Please reconnect.")
        return redirect("import_data")

    if lastfm_account.history_import_is_active:
        messages.info(request, "Full Last.fm history import already running.")
        return redirect("import_data")

    import time

    cutoff_uts = (lastfm_account.last_fetch_timestamp_uts or int(time.time())) - 1
    _save_lastfm_history_reset(lastfm_account, cutoff_uts)
    tasks.import_lastfm_history.delay(user_id=request.user.id, reset=False)
    messages.info(request, "Full Last.fm history import queued.")
    return redirect("import_data")


@require_POST
def import_pocketcasts(request):
    """Queue a Pocket Casts history import for the current user.
    
    Pocket Casts always uses mode="new" and runs every 2 hours automatically.
    First import is "new", subsequent recurring imports are also "new".
    """
    pocketcasts_account = getattr(request.user, "pocketcasts_account", None)
    if not pocketcasts_account:
        messages.error(request, "Connect Pocket Casts before importing.")
        return redirect("import_data")

    # Refresh from DB to get latest status
    pocketcasts_account.refresh_from_db()

    # Allow sync even if connection is broken - importer will attempt refresh

    # Check if this is the first import (no existing schedule)
    from django_celery_beat.models import PeriodicTask

    existing_task = PeriodicTask.objects.filter(
        task="Import from Pocket Casts (Recurring)",
        kwargs__contains=f'"user_id": {request.user.id}',
        enabled=True,
    ).first()

    # Always use mode="new" for Pocket Casts
    mode = "new"

    if not existing_task:
        # First import - run immediately, then set up 2-hour schedule
        tasks.import_pocketcasts.delay(
            user_id=request.user.id,
            mode=mode,
        )
        messages.info(request, "The task to import media from Pocket Casts has been queued. Recurring imports will run every 2 hours.")

        # Set up 2-hour recurring schedule
        from django.utils import timezone as tz
        from django_celery_beat.models import CrontabSchedule

        # Create crontab for every 2 hours (0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22)
        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute=0,
            hour="*/2",
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
            timezone=tz.get_default_timezone(),
        )

        task_name = f"Import from Pocket Casts for {request.user.username} (every 2 hours)"
        PeriodicTask.objects.create(
            name=task_name,
            task="Import from Pocket Casts (Recurring)",
            crontab=crontab,
            kwargs=json.dumps({
                "user_id": request.user.id,
            }),
            start_time=tz.now(),
            enabled=True,
        )
    else:
        # Just run a manual import
        tasks.import_pocketcasts.delay(
            user_id=request.user.id,
            mode=mode,
        )
        messages.info(request, "The task to import media from Pocket Casts has been queued.")

    return redirect("import_data")


def import_imdb(request):
    """View for importing data from IMDB."""
    file = request.FILES.get("imdb_csv")

    if not file:
        messages.error(request, "IMDB CSV file is required.")
        return redirect("import_data")

    mode = request.POST["mode"]
    tasks.import_imdb.delay(
        file=_read_uploaded_file(file),
        user_id=request.user.id,
        mode=mode,
    )
    messages.info(
        request,
        "The task to import media from IMDB CSV file has been queued.",
    )
    return redirect("import_data")


@require_POST
def import_goodreads(request):
    """View for importing books data from GoodReads CSV."""
    file = request.FILES.get("goodreads_csv")

    if not file:
        messages.error(request, "GoodReads CSV file is required.")
        return redirect("import_data")

    mode = request.POST["mode"]
    tasks.import_goodreads.delay(
        file=_read_uploaded_file(file),
        user_id=request.user.id,
        mode=mode,
    )
    messages.info(
        request,
        "The task to import media from GoodReads CSV file has been queued.",
    )
    return redirect("import_data")


@require_POST
def import_hardcover(request):
    """View for importing books data from Hardcover CSV."""
    file = request.FILES.get("hardcover_csv")

    if not file:
        messages.error(request, "Hardcover CSV file is required.")
        return redirect("import_data")

    mode = request.POST["mode"]
    tasks.import_hardcover.delay(
        file=_read_uploaded_file(file),
        user_id=request.user.id,
        mode=mode,
    )
    messages.info(
        request,
        "The task to import media from Hardcover CSV file has been queued.",
    )
    return redirect("import_data")


@require_GET
def export_csv(request):
    """View for exporting all media data to a CSV file."""
    selected_media_types = request.GET.getlist("media_types")
    include_lists = request.GET.get("include_lists", "on") == "on"

    media_types = selected_media_types if selected_media_types else None

    now = timezone.localtime()
    response = StreamingHttpResponse(
        streaming_content=exports.generate_rows(
            request.user, media_types=media_types, include_lists=include_lists,
        ),
        content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="yamtrack_{now}.csv"'},
    )
    logger.info("User %s started CSV export", request.user.username)
    return response


@login_not_required
@csrf_exempt
@require_POST
def jellyfin_webhook(request, token):
    """Handle Jellyfin webhook notifications for media playback."""
    try:
        user = users.models.User.objects.get(token=token)
    except ObjectDoesNotExist:
        logger.warning(
            "Could not process Jellyfin webhook: Invalid token: %s",
            token,
        )
        return HttpResponse(status=401)

    # Attach User instance so history_user_id is populated
    request.user = user
    data = request.body
    if not data:
        logger.warning("Missing payload in Jellyfin webhook request")
        return HttpResponse("Missing payload", status=400)

    payload = json.loads(data)
    processor = jellyfin.JellyfinWebhookProcessor()
    processor.process_payload(payload, user)
    return HttpResponse(status=200)


@login_not_required
@csrf_exempt
@require_POST
def plex_webhook(request, token):
    """Handle Plex webhook notifications for media playback."""
    try:
        user = users.models.User.objects.get(token=token)
    except ObjectDoesNotExist:
        logger.warning(
            "Could not process Plex webhook: Invalid token: %s",
            token,
        )
        return HttpResponse(status=401)

    # Attach User instance so history_user_id is populated
    request.user = user

    # https://support.plex.tv/hc/en-us/articles/115002267687-Webhooks
    # As stated above, the payload is sent in JSON format inside a multipart
    # HTTP POST request. For the media.play and media.rate events, a second part of
    # the POST request contains a JPEG thumbnail for the media.

    data = request.POST.get("payload")
    if not data:
        logger.warning("Missing payload in Plex webhook request")
        user.mark_plex_webhook_error("Missing payload in Plex webhook request")
        return HttpResponse("Missing payload", status=400)

    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON payload in Plex webhook request")
        user.mark_plex_webhook_error("Invalid JSON payload in Plex webhook request")
        return HttpResponse("Invalid payload", status=400)

    event_type = payload.get("event")
    logger.info("Received Plex webhook request - Event: %s, User: %s", event_type, user.username)
    
    processor = plex_webhooks.PlexWebhookProcessor()
    try:
        processor.process_payload(payload, user)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Error processing Plex webhook payload")
        user.mark_plex_webhook_error(
            "Plex webhook processing failed. Check server logs for details.",
        )
        return HttpResponse("Webhook processing failed", status=500)

    user.mark_plex_webhook_received()
    return HttpResponse(status=200)


@login_not_required
@csrf_exempt
@require_POST
def emby_webhook(request, token):
    """Handle Emby webhook notifications for media playback."""
    try:
        user = users.models.User.objects.get(token=token)
    except ObjectDoesNotExist:
        logger.warning(
            "Could not process Emby webhook: Invalid token: %s",
            token,
        )
        return HttpResponse(status=401)

    # Attach User instance so history_user_id is populated
    request.user = user

    # The payload is sent in JSON format inside a multipart
    # HTTP POST request.

    data = request.POST.get("data")
    if not data:
        logger.warning("Missing payload in Emby webhook request")
        return HttpResponse("Missing payload", status=400)

    payload = json.loads(data)
    processor = emby.EmbyWebhookProcessor()
    processor.process_payload(payload, user)
    return HttpResponse(status=200)

@login_not_required
@csrf_exempt
@require_POST
def jellyseerr_webhook(request, token):
    """Handle Jellyseerr webhook notifications for requested/approved media."""
    try:
        user = users.models.User.objects.get(token=token)
    except ObjectDoesNotExist:
        logger.warning(
            "Could not process Jellyseerr webhook: Invalid token: %s",
            token,
        )
        return HttpResponse(status=401)

    # Attach User instance so history_user_id is populated consistently
    request.user = user

    data = request.body
    if not data:
        logger.warning("Missing payload in Jellyseerr webhook request")
        return HttpResponse("Missing payload", status=400)

    try:
        payload = json.loads(data)
    except Exception:
        logger.warning("Invalid JSON payload in Jellyseerr webhook request")
        return HttpResponse("Invalid JSON", status=400)

    processor = jellyseerr_webhooks.JellyseerrWebhookProcessor()
    processor.process_payload(payload, user)
    return HttpResponse(status=200)
