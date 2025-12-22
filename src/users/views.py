import json
import logging

import apprise
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_celery_beat.models import PeriodicTask

from app.models import Item, MediaTypes, Status
from app.templatetags import app_tags
from integrations import plex
from integrations.models import PlexAccount
from users.forms import NotificationSettingsForm, PasswordChangeForm, UserUpdateForm
from users.models import (
    ActivityHistoryViewChoices,
    DateFormatChoices,
    GameLoggingStyleChoices,
    MobileGridLayoutChoices,
    PlannedHomeDisplayChoices,
    TimeFormatChoices,
)

logger = logging.getLogger(__name__)


DEFAULT_AUTO_PAUSE_WEEKS = 16
AUTO_PAUSE_MEDIA_TYPES = [
    MediaTypes.GAME.value,
    MediaTypes.BOARDGAME.value,
    MediaTypes.MOVIE.value,
    MediaTypes.SEASON.value,
    MediaTypes.ANIME.value,
    MediaTypes.MANGA.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
]


def _normalize_auto_pause_rules(raw_rules: str, allowed_libraries: list[str]) -> list[dict]:
    """Validate and normalize submitted auto-pause rules."""
    try:
        parsed_rules = json.loads(raw_rules or "[]")
    except (TypeError, ValueError):
        parsed_rules = []

    if not isinstance(parsed_rules, list):
        return []

    normalized_rules: list[dict] = []
    allowed_set = set(allowed_libraries)
    allowed_set.add("all")

    for entry in parsed_rules:
        if not isinstance(entry, dict):
            continue

        library = entry.get("library")
        if library not in allowed_set:
            continue

        weeks = entry.get("weeks", DEFAULT_AUTO_PAUSE_WEEKS)
        try:
            weeks_val = int(weeks)
        except (TypeError, ValueError):
            weeks_val = DEFAULT_AUTO_PAUSE_WEEKS

        weeks_val = max(1, weeks_val)

        normalized_entry = {
            "library": library,
            "weeks": weeks_val,
        }

        existing_index = next(
            (index for index, rule in enumerate(normalized_rules) if rule["library"] == library),
            None,
        )

        if existing_index is not None:
            normalized_rules[existing_index] = normalized_entry
        else:
            normalized_rules.append(normalized_entry)

    return normalized_rules


def _should_refresh_plex_sections(account: PlexAccount) -> bool:
    """Return True if cached Plex sections should be refreshed."""
    if not account.sections_refreshed_at:
        return True

    expiry = account.sections_refreshed_at + timezone.timedelta(
        hours=settings.PLEX_SECTIONS_TTL_HOURS,
    )
    return timezone.now() >= expiry


@require_http_methods(["GET", "POST"])
def account(request):
    """Update the user's account and import/export data."""
    user_form = UserUpdateForm(instance=request.user)
    password_form = PasswordChangeForm(user=request.user)

    if request.method == "POST":
        # Handle username update
        if "username" in request.POST:
            user_form = UserUpdateForm(request.POST, instance=request.user)

            if user_form.is_valid():
                user_form.save()
                messages.success(request, "Your username has been updated!")
                logger.info(
                    "Successful username change for user: %s",
                    request.user.username,
                )
                return redirect("account")
            logger.warning(
                "Failed username change for user: %s - %s",
                request.user.username,
                list(user_form.errors.keys()),
            )

        # Handle password update
        elif any(
            key in request.POST
            for key in ["old_password", "new_password1", "new_password2"]
        ):
            password_form = PasswordChangeForm(user=request.user, data=request.POST)

            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(
                    request,
                    user,
                )
                messages.success(request, "Your password has been updated!")
                logger.info(
                    "Successful password change for user: %s",
                    request.user.username,
                )
                return redirect("account")
            logger.warning(
                "Failed password change for user: %s - %s",
                request.user.username,
                list(password_form.errors.keys()),
            )

    context = {
        "user_form": user_form,
        "password_form": password_form,
    }

    return render(request, "users/account.html", context)


@require_http_methods(["GET", "POST"])
def notifications(request):
    """Render the notifications settings page."""
    if request.method == "POST":
        form = NotificationSettingsForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Notification settings updated successfully!")
        else:
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, f"{error}")

        return redirect("notifications")

    form = NotificationSettingsForm(instance=request.user)

    return render(
        request,
        "users/notifications.html",
        {
            "form": form,
        },
    )


@require_GET
def search_items(request):
    """Search for items to exclude from notifications."""
    query = request.GET.get("q", "").strip()

    if not query or len(query) <= 1:
        return render(
            request,
            "users/components/search_results.html",
        )

    # Search for items that match the query
    items = (
        Item.objects.filter(
            Q(title__icontains=query),
        )
        .exclude(
            id__in=request.user.notification_excluded_items.values_list(
                "id",
                flat=True,
            ),
        )
        .distinct()[:10]
    )

    return render(
        request,
        "users/components/search_results.html",
        {"items": items, "query": query},
    )


@require_POST
def exclude_item(request):
    """Exclude an item from notifications."""
    item_id = request.POST["item_id"]
    item = get_object_or_404(Item, id=item_id)
    request.user.notification_excluded_items.add(item)

    # Return the updated excluded items list
    excluded_items = request.user.notification_excluded_items.all()

    return render(
        request,
        "users/components/excluded_items.html",
        {"excluded_items": excluded_items},
    )


@require_POST
def include_item(request):
    """Remove an item from the exclusion list."""
    item_id = request.POST["item_id"]
    item = get_object_or_404(Item, id=item_id)
    request.user.notification_excluded_items.remove(item)

    # Return the updated excluded items list
    excluded_items = request.user.notification_excluded_items.all()

    return render(
        request,
        "users/components/excluded_items.html",
        {"excluded_items": excluded_items},
    )


@require_GET
def test_notification(request):
    """Send a test notification to the user."""
    try:
        # Create Apprise instance
        apobj = apprise.Apprise()

        # Add all notification URLs
        notification_urls = [
            url.strip()
            for url in request.user.notification_urls.splitlines()
            if url.strip()
        ]
        if not notification_urls:
            messages.error(request, "No notification URLs configured.")
            return redirect("notifications")

        for url in notification_urls:
            apobj.add(url)

        # Send test notification
        result = apobj.notify(
            title="YamTrack Test Notification",
            body=(
                "This is a test notification from YamTrack. "
                "If you're seeing this, your notifications are working correctly!"
            ),
        )

        if result:
            messages.success(request, "Test notification sent successfully!")
        else:
            messages.error(request, "Failed to send test notification.")
    except Exception:
        logger.exception("Error sending notification")

    return redirect("notifications")


@require_http_methods(["GET", "POST"])
def ui_preferences(request):
    """Render the UI preferences settings page."""
    media_types = MediaTypes.values
    media_types.remove(MediaTypes.EPISODE.value)

    if request.method == "GET":
        return render(
            request,
            "users/ui_preferences.html",
            {"media_types": media_types},
        )

    # Prevent demo users from updating preferences
    if request.user.is_demo:
        messages.error(request, "This section is view-only for demo accounts.")
        return redirect("ui_preferences")

    # Process form submission
    request.user.clickable_media_cards = "clickable_media_cards" in request.POST
    media_types_checked = request.POST.getlist("media_types_checkboxes")

    # Update user preferences for each media type
    for media_type in media_types:
        setattr(
            request.user,
            f"{media_type}_enabled",
            media_type in media_types_checked,
        )

    # Save changes and redirect
    request.user.save()
    messages.success(request, "Settings updated.")

    return redirect("ui_preferences")


@require_http_methods(["GET", "POST"])
def preferences(request):
    """Render the preferences settings page."""
    active_libraries = [
        library
        for library in request.user.get_active_media_types()
        if library in AUTO_PAUSE_MEDIA_TYPES
    ]
    library_labels = {"all": "All Libraries"}
    for library in active_libraries:
        library_labels[library] = app_tags.media_type_readable_plural(library)

    if request.method == "POST":
        # Prevent demo users from updating preferences
        if request.user.is_demo:
            messages.error(request, "This section is view-only for demo accounts.")
            return redirect("preferences")

        # Process form submission for user preferences
        date_format = request.POST.get("date_format")
        time_format = request.POST.get("time_format")
        activity_history_view = request.POST.get("activity_history_view")
        game_logging_style = request.POST.get("game_logging_style")
        mobile_grid_layout = request.POST.get("mobile_grid_layout")
        quick_season_update_mobile = request.POST.get("quick_season_update_mobile") == "1"

        fields_to_update = []

        if date_format and date_format in [choice[0] for choice in DateFormatChoices.choices]:
            if request.user.date_format != date_format:
                request.user.date_format = date_format
                fields_to_update.append("date_format")

        if time_format and time_format in [choice[0] for choice in TimeFormatChoices.choices]:
            if request.user.time_format != time_format:
                request.user.time_format = time_format
                fields_to_update.append("time_format")

        if (
            activity_history_view
            and activity_history_view in [choice[0] for choice in ActivityHistoryViewChoices.choices]
        ):
            if request.user.activity_history_view != activity_history_view:
                request.user.activity_history_view = activity_history_view
                fields_to_update.append("activity_history_view")

        if (
            game_logging_style
            and game_logging_style in [choice[0] for choice in GameLoggingStyleChoices.choices]
        ):
            if request.user.game_logging_style != game_logging_style:
                request.user.game_logging_style = game_logging_style
                fields_to_update.append("game_logging_style")
                from app import history_cache

                history_cache.invalidate_history_cache(request.user.id)
                history_cache.schedule_history_refresh(request.user.id, game_logging_style, debounce_seconds=0)

        if (
            mobile_grid_layout
            and mobile_grid_layout in [choice[0] for choice in MobileGridLayoutChoices.choices]
        ):
            if request.user.mobile_grid_layout != mobile_grid_layout:
                request.user.mobile_grid_layout = mobile_grid_layout
                fields_to_update.append("mobile_grid_layout")

        if request.user.quick_season_update_mobile != quick_season_update_mobile:
            request.user.quick_season_update_mobile = quick_season_update_mobile
            fields_to_update.append("quick_season_update_mobile")

        show_planned_on_home = request.POST.get("show_planned_on_home", PlannedHomeDisplayChoices.DISABLED)

        if show_planned_on_home in [choice[0] for choice in PlannedHomeDisplayChoices.choices]:
            if request.user.show_planned_on_home != show_planned_on_home:
                request.user.show_planned_on_home = show_planned_on_home
                fields_to_update.append("show_planned_on_home")

        auto_pause_enabled = request.POST.get("auto_pause_enabled") == "1"
        raw_rules = request.POST.get("auto_pause_rules", "[]")
        normalized_rules = _normalize_auto_pause_rules(raw_rules, active_libraries)

        if request.user.auto_pause_in_progress_enabled != auto_pause_enabled:
            request.user.auto_pause_in_progress_enabled = auto_pause_enabled
            fields_to_update.append("auto_pause_in_progress_enabled")

        if request.user.auto_pause_rules != normalized_rules:
            request.user.auto_pause_rules = normalized_rules
            fields_to_update.append("auto_pause_rules")

        if fields_to_update:
            request.user.save(update_fields=fields_to_update)
            request.user.refresh_from_db()
        messages.success(request, "Preferences updated successfully.")
        return redirect("preferences")

    context = {
        "active_libraries": active_libraries,
        "auto_pause_enabled": request.user.auto_pause_in_progress_enabled,
        "auto_pause_rules_json": json.dumps(request.user.auto_pause_rules or []),
        "library_labels_json": json.dumps(library_labels),
    }

    return render(request, "users/preferences.html", context)


@require_GET
def integrations(request):
    """Render the integrations settings page."""
    return render(request, "users/integrations.html", {"user": request.user})


@require_GET
def import_data(request):
    """Render the import data settings page."""
    import_tasks = request.user.get_import_tasks()
    plex_account = getattr(request.user, "plex_account", None)
    if plex_account and not plex_account.plex_token:
        plex_account = None
    plex_sections: list[dict] = []
    plex_connected = False
    plex_error = None

    if plex_account:
        try:
            account_data = plex.fetch_account(plex_account.plex_token)
            plex_connected = True

            username = account_data.get("username")
            if username and username != plex_account.plex_username:
                plex_account.plex_username = username
                plex_account.save(update_fields=["plex_username"])
        except plex.PlexAuthError:
            plex_error = "Plex token expired or revoked. Please reconnect."
        except Exception as exc:  # pragma: no cover - defensive
            plex_error = str(exc)
        else:
            if _should_refresh_plex_sections(plex_account):
                try:
                    plex_account.sections = plex.list_sections(plex_account.plex_token)
                    plex_account.sections_refreshed_at = timezone.now()
                    plex_account.save(
                        update_fields=["sections", "sections_refreshed_at"],
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    if not plex_error:
                        plex_error = f"Could not refresh Plex libraries: {exc}"

            plex_sections = plex_account.sections or []

    # Get Pocket Casts account
    pocketcasts_account = getattr(request.user, "pocketcasts_account", None)
    # Refresh from DB to get latest connection_broken status
    if pocketcasts_account:
        pocketcasts_account.refresh_from_db()

    context = {
        "user": request.user,
        "import_tasks": import_tasks,
        "plex_account": plex_account if plex_connected else None,
        "plex_sections": plex_sections,
        "plex_error": plex_error,
        "pocketcasts_account": pocketcasts_account,
    }
    return render(request, "users/import_data.html", context)


@require_GET
def export_data(request):
    """Render the export data settings page."""
    return render(request, "users/export_data.html", {"user": request.user})


@require_GET
def advanced(request):
    """Render the advanced settings page."""
    return render(request, "users/advanced.html")


@require_GET
def about(request):
    """Render the about page."""
    return render(
        request,
        "users/about.html",
        {
            "user": request.user,
            "version": settings.VERSION,
            "commit": settings.COMMIT_SHA_SHORT,
        },
    )


@require_POST
def delete_import_schedule(request):
    """Delete an import schedule."""
    task_name = request.POST.get("task_name")
    try:
        task = PeriodicTask.objects.get(
            name=task_name,
            kwargs__contains=f'"user_id": {request.user.id}',
        )
        task.delete()
        messages.success(request, "Import schedule deleted.")
    except PeriodicTask.DoesNotExist:
        messages.error(request, "Import schedule not found.")
    return redirect("import_data")


@require_POST
def regenerate_token(request):
    """Regenerate the token for the user."""
    while True:
        try:
            request.user.regenerate_token()
            messages.success(request, "Token regenerated successfully.")
            break
        except IntegrityError:
            continue
    return redirect("integrations")


@require_POST
def update_plex_usernames(request):
    """Update the Plex usernames for the user."""
    usernames = request.POST.get("plex_usernames", "")
    redirect_target = request.POST.get("next") or "integrations"

    username_list = [u.strip() for u in usernames.split(",") if u.strip()]

    seen = set()
    deduplicated_usernames = [
        u for u in username_list if not (u in seen or seen.add(u))
    ]

    # Reconstruct with comma-space separation
    cleaned_usernames = ", ".join(deduplicated_usernames)

    if cleaned_usernames != request.user.plex_usernames:
        request.user.plex_usernames = cleaned_usernames
        request.user.save(update_fields=["plex_usernames"])
        messages.success(request, "Plex usernames updated successfully")

    return redirect(redirect_target)

@login_required
@require_POST
def update_jellyseerr_settings(request):
    user = request.user

    # Handle enabled/disabled dropdown (sends '1' or '0' as string)
    enabled = request.POST.get("jellyseerr_enabled") == "1"

    raw_trigger = (request.POST.get("jellyseerr_trigger_statuses") or "").strip()
    raw_allowed = (request.POST.get("jellyseerr_allowed_usernames") or "").strip()
    default_status = (request.POST.get("jellyseerr_default_added_status") or "").strip()

    # Validate + normalize default status
    valid_default_statuses = {Status.PLANNING.value, Status.IN_PROGRESS.value}
    if default_status not in valid_default_statuses:
        default_status = Status.PLANNING.value

    # Normalize trigger statuses: "pending, processing" -> "PENDING,PROCESSING"
    valid_jellyseerr_statuses = {
        "UNKNOWN",
        "PENDING",
        "PROCESSING",
        "PARTIALLY_AVAILABLE",
        "AVAILABLE",
    }

    if raw_trigger:
        tokens = [t.strip().upper() for t in raw_trigger.split(",") if t.strip()]
        unknown = [t for t in tokens if t not in valid_jellyseerr_statuses]
        if unknown:
            messages.error(
                request,
                "Jellyseerr trigger statuses contain invalid values: "
                + ", ".join(unknown)
                + ". Valid: "
                + ", ".join(sorted(valid_jellyseerr_statuses)),
            )
            return redirect(request.META.get("HTTP_REFERER", "/settings/integrations"))
        trigger_statuses = ",".join(tokens)
    else:
        # Blank means "default behaviour" (processor skips UNKNOWN)
        trigger_statuses = ""

    # Normalize allowed usernames: " bob, alice " -> "bob,alice"
    if raw_allowed:
        allowed_tokens = [t.strip() for t in raw_allowed.split(",") if t.strip()]
        allowed_usernames = ",".join(allowed_tokens)
    else:
        allowed_usernames = ""

    # Save
    user.jellyseerr_enabled = enabled
    user.jellyseerr_trigger_statuses = trigger_statuses
    user.jellyseerr_allowed_usernames = allowed_usernames
    user.jellyseerr_default_added_status = default_status
    user.save(
        update_fields=[
            "jellyseerr_enabled",
            "jellyseerr_trigger_statuses",
            "jellyseerr_allowed_usernames",
            "jellyseerr_default_added_status",
        ]
    )

    messages.success(request, "Jellyseerr settings saved.")
    return redirect(request.META.get("HTTP_REFERER", "/settings/integrations"))


@require_POST
def clear_search_cache(request):
    """Clear all cached search entries."""
    deleted = cache.delete_pattern("search_*")

    messages.success(
        request,
        f"Successfully cleared {deleted} search entr{pluralize(deleted, 'y,ies')}",
    )
    logger.info(
        "Successfully cleared %s search entries",
        deleted,
    )

    return redirect("advanced")
