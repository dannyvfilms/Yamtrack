import base64
import json
import logging
from io import BytesIO

import apprise
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_not_required, login_required
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Q
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django_celery_beat.models import PeriodicTask

from app import history_cache, statistics_cache
from app.models import Item, MediaTypes, Status
from app.providers import tmdb
from app.services import metadata_resolution
from app.templatetags import app_tags
from integrations import exports
from integrations import plex
from integrations.models import PlexAccount
from integrations.plex_watchlist import WATCHLIST_TASK_NAME
from users.forms import (
    AuthenticatorSetupForm,
    NotificationSettingsForm,
    PasswordChangeForm,
    PasswordRecoveryForm,
    RegenerateRecoveryCodesForm,
    UserUpdateForm,
)
from users.models import (
    ActivityHistoryViewChoices,
    AnimeLibraryModeChoices,
    DateFormatChoices,
    GameLoggingStyleChoices,
    MetadataSourceDefaultChoices,
    MediaCardSubtitleDisplayChoices,
    MobileGridLayoutChoices,
    PlannedHomeDisplayChoices,
    QuickWatchDateChoices,
    RatingScaleChoices,
    TitleDisplayPreferenceChoices,
    TopTalentSortChoices,
    TimeFormatChoices,
)

try:
    import qrcode
except ModuleNotFoundError:  # pragma: no cover - optional dependency guard
    qrcode = None


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


def _get_stored_plex_account(user):
    """Return the user's stored Plex account when it has a token."""
    plex_account = getattr(user, "plex_account", None)
    if plex_account and not plex_account.plex_token:
        return None
    return plex_account


def _refresh_cached_plex_sections(account: PlexAccount) -> tuple[list[dict], str | None]:
    """Refresh and persist Plex library sections when the cache is stale."""
    cached_sections = account.sections or []
    needs_refresh = _should_refresh_plex_sections(account) or not cached_sections

    if not needs_refresh:
        return cached_sections, None

    try:
        account.sections = plex.list_sections(account.plex_token)
        account.sections_refreshed_at = timezone.now()
        account.save(
            update_fields=["sections", "sections_refreshed_at"],
        )
    except plex.PlexAuthError:
        return cached_sections, "Plex token expired or revoked. Please reconnect."
    except Exception as exc:  # pragma: no cover - defensive
        return cached_sections, f"Could not refresh Plex libraries: {exc}"

    return account.sections or [], None


def _build_qr_data_uri(provisioning_uri: str) -> str:
    """Return a base64 PNG data URI for an authenticator provisioning URI."""
    if not provisioning_uri:
        return ""

    if qrcode is None:
        logger.warning("qrcode package is unavailable; skipping authenticator QR rendering")
        return ""

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(provisioning_uri)
    qr.make(fit=True)

    qr_image = qr.make_image(fill_color="black", back_color="white")
    output = BytesIO()
    qr_image.save(output, format="PNG")
    encoded_png = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded_png}"


@require_http_methods(["GET", "POST"])
def account(request):
    """Update the user's account and account security settings."""
    user_form = UserUpdateForm(instance=request.user)
    password_form = PasswordChangeForm(user=request.user)
    authenticator_form = AuthenticatorSetupForm(user=request.user)
    recovery_codes_form = RegenerateRecoveryCodesForm(user=request.user)
    fresh_recovery_codes = None
    show_authenticator_setup = not request.user.has_authenticator_configured

    if request.method == "POST":
        action = request.POST.get("action", "")

        if "username" in request.POST:
            user_form = UserUpdateForm(request.POST, instance=request.user)

            if user_form.is_valid():
                user_form.save()
                messages.success(request, "Your username has been updated!")
                logger.info("Successful username change for user: %s", request.user.username)
                return redirect("account")

            logger.warning(
                "Failed username change for user: %s - %s",
                request.user.username,
                list(user_form.errors.keys()),
            )

        elif any(key in request.POST for key in ["old_password", "new_password1", "new_password2"]):
            password_form = PasswordChangeForm(user=request.user, data=request.POST)

            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Your password has been updated!")
                logger.info("Successful password change for user: %s", request.user.username)
                return redirect("account")

            logger.warning(
                "Failed password change for user: %s - %s",
                request.user.username,
                list(password_form.errors.keys()),
            )

        elif action == "enable_authenticator":
            show_authenticator_setup = True
            authenticator_form = AuthenticatorSetupForm(request.POST, user=request.user)
            if authenticator_form.is_valid():
                request.user.authenticator_enabled = True
                request.user.authenticator_confirmed_at = timezone.now()
                request.user.save(update_fields=["authenticator_enabled", "authenticator_confirmed_at"])
                fresh_recovery_codes = request.user.generate_recovery_codes()
                show_authenticator_setup = False
                messages.success(
                    request,
                    "Authenticator app enabled. Save your recovery codes now—if you lose both, you cannot self-recover.",
                )

        elif action == "start_authenticator_setup":
            request.user.authenticator_enabled = False
            request.user.authenticator_secret = ""
            request.user.authenticator_confirmed_at = None
            request.user.save(
                update_fields=[
                    "authenticator_enabled",
                    "authenticator_secret",
                    "authenticator_confirmed_at",
                ],
            )
            show_authenticator_setup = True
            messages.info(
                request,
                "Scan and verify a code from your new authenticator app to finish setup.",
            )

        elif action == "disable_authenticator":
            request.user.authenticator_enabled = False
            request.user.authenticator_secret = ""
            request.user.authenticator_confirmed_at = None
            request.user.save(
                update_fields=[
                    "authenticator_enabled",
                    "authenticator_secret",
                    "authenticator_confirmed_at",
                ],
            )
            show_authenticator_setup = True
            messages.warning(request, "Authenticator app deactivated.")

        elif action == "regenerate_recovery_codes":
            recovery_codes_form = RegenerateRecoveryCodesForm(request.POST, user=request.user)
            if recovery_codes_form.is_valid():
                fresh_recovery_codes = request.user.generate_recovery_codes()
                messages.success(
                    request,
                    "Recovery codes regenerated. Store them securely now.",
                )

    authenticator_secret = ""
    authenticator_uri = ""
    authenticator_qr_data_uri = ""
    if show_authenticator_setup:
        authenticator_secret = request.user.get_or_create_authenticator_secret()
        authenticator_uri = request.user.build_totp_uri()
        authenticator_qr_data_uri = _build_qr_data_uri(authenticator_uri)

    context = {
        "user_form": user_form,
        "password_form": password_form,
        "authenticator_form": authenticator_form,
        "recovery_codes_form": recovery_codes_form,
        "authenticator_secret": authenticator_secret,
        "authenticator_uri": authenticator_uri,
        "authenticator_qr_data_uri": authenticator_qr_data_uri,
        "show_authenticator_setup": show_authenticator_setup,
        "unused_recovery_code_count": request.user.recovery_codes.filter(used_at__isnull=True).count(),
        "fresh_recovery_codes": fresh_recovery_codes,
    }

    return render(request, "users/account.html", context)


@login_not_required
@require_http_methods(["GET", "POST"])
def password_recover(request):
    """Recover password using recovery code and optional authenticator app code."""
    form = PasswordRecoveryForm()

    if request.method == "POST":
        form = PasswordRecoveryForm(request.POST)
        if form.is_valid():
            user = form.save()
            logger.info("Successful self-service password recovery for user: %s", user.username)
            messages.success(request, "Password updated. Sign in with your new password.")
            return redirect("account_login")

        logger.warning("Failed self-service password recovery attempt")

    return render(request, "users/password_recover.html", {"form": form})


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
                "<p>This is a test notification from YamTrack.</p>"
                "<p>If you're seeing this, "
                "your notifications are working correctly!</p>"
            ),
            body_format=apprise.NotifyFormat.HTML,
        )

        if result:
            messages.success(request, "Test notification sent successfully!")
        else:
            messages.error(request, "Failed to send test notification.")
    except Exception:
        logger.exception("Error sending notification")

    return redirect("notifications")


@require_http_methods(["GET", "POST"])
def sidebar(request):
    """Render the sidebar settings page (media types visibility and UI preferences)."""
    # Get all media types except episode
    media_types = [mt.value for mt in MediaTypes if mt.value != MediaTypes.EPISODE.value]
    
    if request.method == "POST":
        # Prevent demo users from updating preferences
        if request.user.is_demo:
            messages.error(request, "This section is view-only for demo accounts.")
            return redirect("sidebar")
        
        fields_to_update = []
        
        # Handle clickable_media_cards preference
        clickable_media_cards = request.POST.get("clickable_media_cards") == "on"
        if request.user.clickable_media_cards != clickable_media_cards:
            request.user.clickable_media_cards = clickable_media_cards
            fields_to_update.append("clickable_media_cards")
        
        # Handle media types checkboxes
        selected_media_types = request.POST.getlist("media_types_checkboxes")
        for media_type in media_types:
            enabled_field = f"{media_type}_enabled"
            is_enabled = media_type in selected_media_types
            current_value = getattr(request.user, enabled_field, False)
            if current_value != is_enabled:
                setattr(request.user, enabled_field, is_enabled)
                fields_to_update.append(enabled_field)
        
        if fields_to_update:
            request.user.save(update_fields=fields_to_update)
            messages.success(request, "Settings updated successfully.")
        else:
            messages.info(request, "No changes to save.")
        
        return redirect("sidebar")
    
    context = {
        "media_types": media_types,
    }
    return render(request, "users/sidebar.html", context)


@require_GET
def ui_preferences(request):
    """Redirect to sidebar page (UI preferences renamed to Sidebar)."""
    return redirect("sidebar")


@require_http_methods(["GET", "POST"])
def preferences(request):
    """Render the preferences settings page."""
    media_types = [mt.value for mt in MediaTypes if mt.value != MediaTypes.EPISODE.value]
    active_libraries = [
        library
        for library in request.user.get_active_media_types()
        if library in AUTO_PAUSE_MEDIA_TYPES
    ]
    library_labels = {"all": "All Libraries"}
    for library in active_libraries:
        library_labels[library] = app_tags.media_type_readable_plural(library)
    try:
        watch_provider_regions = tmdb.watch_provider_regions()
    except Exception as exc:  # pragma: no cover - defensive provider fallback
        logger.warning("Could not load TMDB watch provider regions: %s", exc)
        watch_provider_regions = [("UNSET", "Not set")]
    tv_metadata_source_choices = [
        (choice.value, choice.label)
        for choice in metadata_resolution.available_metadata_sources(
            MediaTypes.TV.value,
        )
    ]
    anime_metadata_source_choices = [
        (choice.value, choice.label)
        for choice in metadata_resolution.available_metadata_sources(
            MediaTypes.ANIME.value,
        )
    ]
    tvdb_enabled = metadata_resolution.provider_is_enabled(
        MetadataSourceDefaultChoices.TVDB,
    )

    if request.method == "POST":
        # Prevent demo users from updating preferences
        if request.user.is_demo:
            messages.error(request, "This section is view-only for demo accounts.")
            return redirect("preferences")

        # Process form submission for user preferences
        selected_media_types = request.POST.getlist("media_types_checkboxes")
        date_format = request.POST.get("date_format")
        time_format = request.POST.get("time_format")
        activity_history_view = request.POST.get("activity_history_view")
        game_logging_style = request.POST.get("game_logging_style")
        mobile_grid_layout = request.POST.get("mobile_grid_layout")
        media_card_subtitle_display = request.POST.get("media_card_subtitle_display")
        title_display_preference = request.POST.get("title_display_preference")
        top_talent_sort_by = request.POST.get("top_talent_sort_by")
        rating_scale = request.POST.get("rating_scale")
        tv_metadata_source_default = request.POST.get("tv_metadata_source_default")
        anime_metadata_source_default = request.POST.get("anime_metadata_source_default")
        anime_library_mode = request.POST.get("anime_library_mode")
        progress_bar_raw = request.POST.get("progress_bar")
        hide_completed_recommendations_raw = request.POST.get("hide_completed_recommendations")
        hide_zero_rating_raw = request.POST.get("hide_zero_rating")
        quick_season_update_mobile = request.POST.get("quick_season_update_mobile") == "1"
        book_comic_manga_progress_percentage = request.POST.get("book_comic_manga_progress_percentage") == "1"

        fields_to_update = []
        rating_scale_changed = False
        top_talent_sort_changed = False

        # Backwards-compatible handling for older clients/tests that still submit
        # media library checkboxes to the preferences endpoint.
        if "media_types_checkboxes" in request.POST:
            for media_type in media_types:
                enabled_field = f"{media_type}_enabled"
                is_enabled = media_type in selected_media_types
                current_value = getattr(request.user, enabled_field, False)
                if current_value != is_enabled:
                    setattr(request.user, enabled_field, is_enabled)
                    fields_to_update.append(enabled_field)

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
                history_cache.invalidate_history_cache(request.user.id)
                history_cache.schedule_history_refresh(request.user.id, game_logging_style, debounce_seconds=0)

        if (
            mobile_grid_layout
            and mobile_grid_layout in [choice[0] for choice in MobileGridLayoutChoices.choices]
        ):
            if request.user.mobile_grid_layout != mobile_grid_layout:
                request.user.mobile_grid_layout = mobile_grid_layout
                fields_to_update.append("mobile_grid_layout")

        if (
            media_card_subtitle_display
            and media_card_subtitle_display
            in [choice[0] for choice in MediaCardSubtitleDisplayChoices.choices]
        ):
            if request.user.media_card_subtitle_display != media_card_subtitle_display:
                request.user.media_card_subtitle_display = media_card_subtitle_display
                fields_to_update.append("media_card_subtitle_display")

        if (
            title_display_preference
            and title_display_preference
            in [choice[0] for choice in TitleDisplayPreferenceChoices.choices]
        ):
            if request.user.title_display_preference != title_display_preference:
                request.user.title_display_preference = title_display_preference
                fields_to_update.append("title_display_preference")

        if (
            top_talent_sort_by
            and top_talent_sort_by in [choice[0] for choice in TopTalentSortChoices.choices]
        ):
            if request.user.top_talent_sort_by != top_talent_sort_by:
                request.user.top_talent_sort_by = top_talent_sort_by
                fields_to_update.append("top_talent_sort_by")
                top_talent_sort_changed = True

        if rating_scale and rating_scale in [choice[0] for choice in RatingScaleChoices.choices]:
            if request.user.rating_scale != rating_scale:
                request.user.rating_scale = rating_scale
                fields_to_update.append("rating_scale")
                rating_scale_changed = True

        if progress_bar_raw is not None:
            progress_bar = progress_bar_raw == "1"
            if request.user.progress_bar != progress_bar:
                request.user.progress_bar = progress_bar
                fields_to_update.append("progress_bar")

        if hide_completed_recommendations_raw is not None:
            hide_completed_recommendations = hide_completed_recommendations_raw == "1"
            if request.user.hide_completed_recommendations != hide_completed_recommendations:
                request.user.hide_completed_recommendations = hide_completed_recommendations
                fields_to_update.append("hide_completed_recommendations")

        if hide_zero_rating_raw is not None:
            hide_zero_rating = hide_zero_rating_raw == "1"
            if request.user.hide_zero_rating != hide_zero_rating:
                request.user.hide_zero_rating = hide_zero_rating
                fields_to_update.append("hide_zero_rating")

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

        if request.user.book_comic_manga_progress_percentage != book_comic_manga_progress_percentage:
            request.user.book_comic_manga_progress_percentage = book_comic_manga_progress_percentage
            fields_to_update.append("book_comic_manga_progress_percentage")

        provider_region = request.POST.get("watch_provider_region", "")
        if provider_region in [region[0] for region in watch_provider_regions]:
            if request.user.watch_provider_region != provider_region:
                request.user.watch_provider_region = provider_region
                fields_to_update.append("watch_provider_region")
        else:
            if request.user.watch_provider_region != "UNSET":
                request.user.watch_provider_region = "UNSET"
                fields_to_update.append("watch_provider_region")

        if tv_metadata_source_default in {
            choice[0] for choice in tv_metadata_source_choices
        }:
            if request.user.tv_metadata_source_default != tv_metadata_source_default:
                request.user.tv_metadata_source_default = tv_metadata_source_default
                fields_to_update.append("tv_metadata_source_default")

        if anime_metadata_source_default in {
            choice[0] for choice in anime_metadata_source_choices
        }:
            if request.user.anime_metadata_source_default != anime_metadata_source_default:
                request.user.anime_metadata_source_default = anime_metadata_source_default
                fields_to_update.append("anime_metadata_source_default")

        if anime_library_mode in [choice[0] for choice in AnimeLibraryModeChoices.choices]:
            if request.user.anime_library_mode != anime_library_mode:
                request.user.anime_library_mode = anime_library_mode
                fields_to_update.append("anime_library_mode")

        if fields_to_update:
            request.user.save(update_fields=fields_to_update)
            request.user.refresh_from_db()
            if rating_scale_changed:
                history_cache.invalidate_history_cache(
                    request.user.id,
                    force=True,
                    logging_styles=("sessions", "repeats"),
                )
            if rating_scale_changed or top_talent_sort_changed:
                statistics_cache.invalidate_statistics_cache(request.user.id)
                statistics_cache.schedule_all_ranges_refresh(
                    request.user.id,
                    debounce_seconds=0,
                )
        success_message = (
            "Settings updated successfully."
            if "media_types_checkboxes" in request.POST
            else "Preferences updated successfully."
        )
        messages.success(request, success_message)
        return redirect("preferences")

    context = {
        "media_types": media_types,
        "active_libraries": active_libraries,
        "auto_pause_enabled": request.user.auto_pause_in_progress_enabled,
        "auto_pause_rules_json": json.dumps(request.user.auto_pause_rules or []),
        "library_labels_json": json.dumps(library_labels),
        "watch_provider_choices": watch_provider_regions,
        "tv_metadata_source_choices": tv_metadata_source_choices,
        "anime_metadata_source_choices": anime_metadata_source_choices,
        "anime_library_mode_choices": AnimeLibraryModeChoices.choices,
        "tvdb_enabled": tvdb_enabled,
    }

    return render(request, "users/preferences.html", context)


@require_GET
def integrations(request):
    """Render the integrations settings page."""
    user = request.user
    last_received = user.plex_webhook_last_received_at
    rotated_at = user.plex_webhook_token_rotated_at
    plex_webhook_needs_update = False
    if rotated_at:
        plex_webhook_needs_update = not last_received or last_received < rotated_at

    plex_account = getattr(user, "plex_account", None)
    plex_library_options: list[dict] = []
    selected_plex_webhook_libraries: list[str] = []

    if plex_account and plex_account.plex_token:
        if _should_refresh_plex_sections(plex_account):
            try:
                plex_account.sections = plex.list_sections(plex_account.plex_token)
                plex_account.sections_refreshed_at = timezone.now()
                plex_account.save(update_fields=["sections", "sections_refreshed_at"])
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Could not refresh Plex libraries for webhook settings for user %s: %s",
                    user.username,
                    exc,
                )

        sections = plex_account.sections or []
        for section in sections:
            machine_identifier = section.get("machine_identifier")
            section_id = section.get("id")
            if not machine_identifier or not section_id:
                continue
            library_value = f"{machine_identifier}::{section_id}"
            plex_library_options.append(
                {
                    "value": library_value,
                    "label": section.get("title") or f"Library {section_id}",
                    "server_name": section.get("server_name") or "",
                },
            )

        selected_plex_webhook_libraries = user.plex_webhook_libraries or [
            option["value"] for option in plex_library_options
        ]

    return render(
        request,
        "users/integrations.html",
        {
            "user": user,
            "plex_webhook_needs_update": plex_webhook_needs_update,
            "plex_library_options_json": json.dumps(plex_library_options),
            "selected_plex_webhook_libraries_json": json.dumps(selected_plex_webhook_libraries),
        },
    )


@require_GET
def import_data(request):
    """Render the import data settings page."""
    import_tasks = request.user.get_import_tasks()
    plex_account = _get_stored_plex_account(request.user)
    plex_sections = plex_account.sections or [] if plex_account else []

    # Get Audiobookshelf account
    audiobookshelf_account = getattr(request.user, "audiobookshelf_account", None)
    if audiobookshelf_account:
        audiobookshelf_account.refresh_from_db()

    # Get Pocket Casts account
    pocketcasts_account = getattr(request.user, "pocketcasts_account", None)
    # Refresh from DB to get latest connection_broken status
    if pocketcasts_account:
        pocketcasts_account.refresh_from_db()

    # Get Last.fm account
    lastfm_account = getattr(request.user, "lastfm_account", None)
    if lastfm_account:
        lastfm_account.refresh_from_db()

    # Get Last.fm periodic task status
    lastfm_periodic_task = None
    lastfm_poll_interval = getattr(settings, "LASTFM_POLL_INTERVAL_MINUTES", 15)
    lastfm_history_status_label = "Not started"
    lastfm_history_current_page = None
    lastfm_history_total_pages = None
    lastfm_history_can_start = False
    lastfm_history_button_label = "Import full history"
    if lastfm_account:
        from django_celery_beat.models import PeriodicTask

        lastfm_periodic_task = PeriodicTask.objects.filter(
            task="Poll Last.fm for all users",
            enabled=True,
        ).first()
        # Get actual interval from task if it exists
        if lastfm_periodic_task and lastfm_periodic_task.interval:
            lastfm_poll_interval = lastfm_periodic_task.interval.every
        if lastfm_account.history_import_status != "idle":
            lastfm_history_status_label = lastfm_account.get_history_import_status_display()
        lastfm_history_total_pages = lastfm_account.history_import_total_pages
        if lastfm_history_total_pages:
            if lastfm_account.history_import_status == "completed":
                lastfm_history_current_page = lastfm_history_total_pages
            elif lastfm_account.history_import_next_page:
                lastfm_history_current_page = min(
                    lastfm_account.history_import_next_page,
                    lastfm_history_total_pages,
                )
        lastfm_history_can_start = lastfm_account.history_import_can_start
        if lastfm_account.history_import_status in {"completed", "failed"}:
            lastfm_history_button_label = "Reimport full history"

    context = {
        "user": request.user,
        "import_tasks": import_tasks,
        "plex_account": plex_account,
        "plex_sections": plex_sections,
        "plex_sections_json": json.dumps(plex_sections),
        "audiobookshelf_account": audiobookshelf_account,
        "pocketcasts_account": pocketcasts_account,
        "lastfm_account": lastfm_account,
        "lastfm_periodic_task": lastfm_periodic_task,
        "lastfm_poll_interval": lastfm_poll_interval,
        "lastfm_history_status_label": lastfm_history_status_label,
        "lastfm_history_current_page": lastfm_history_current_page,
        "lastfm_history_total_pages": lastfm_history_total_pages,
        "lastfm_history_can_start": lastfm_history_can_start,
        "lastfm_history_button_label": lastfm_history_button_label,
    }
    return render(request, "users/import_data.html", context)


@require_GET
def import_data_plex_status(request):
    """Verify the stored Plex account without blocking the initial page render."""
    plex_account = _get_stored_plex_account(request.user)
    if not plex_account:
        return JsonResponse(
            {
                "state": "disconnected",
                "error": "",
            },
        )

    try:
        account_data = plex.fetch_account(plex_account.plex_token)
    except plex.PlexAuthError:
        return JsonResponse(
            {
                "state": "error",
                "error": "Plex token expired or revoked. Please reconnect.",
            },
        )
    except Exception as exc:  # pragma: no cover - defensive
        return JsonResponse(
            {
                "state": "error",
                "error": str(exc),
            },
        )

    username = account_data.get("username")
    if username and username != plex_account.plex_username:
        plex_account.plex_username = username
        plex_account.save(update_fields=["plex_username"])

    return JsonResponse(
        {
            "state": "connected",
            "error": "",
        },
    )


@require_GET
def import_data_plex_sections(request):
    """Refresh cached Plex library sections for the import page in the background."""
    plex_account = _get_stored_plex_account(request.user)
    if not plex_account:
        return JsonResponse(
            {
                "sections": [],
                "error": "",
            },
        )

    sections, error = _refresh_cached_plex_sections(plex_account)
    return JsonResponse(
        {
            "sections": sections,
            "error": error or "",
        },
    )


@require_GET
def export_data(request):
    """Render the export data settings page."""
    media_types = [mt.value for mt in MediaTypes if mt.value not in (MediaTypes.EPISODE.value, MediaTypes.SEASON.value)]
    export_tasks = request.user.get_export_tasks()
    context = {
        "user": request.user,
        "media_types": media_types,
        "export_tasks": export_tasks,
        "backup_dir": settings.BACKUP_DIR,
    }
    return render(request, "users/export_data.html", context)


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
        if task.task == WATCHLIST_TASK_NAME:
            PlexAccount.objects.filter(user=request.user).update(
                watchlist_sync_enabled=False,
            )
        task.delete()
        messages.success(request, "Import schedule deleted.")
    except PeriodicTask.DoesNotExist:
        messages.error(request, "Import schedule not found.")
    return redirect("import_data")


@require_POST
def create_export_schedule(request):
    """Create a one-time export or a recurring scheduled export."""
    import datetime as dt

    from django_celery_beat.models import CrontabSchedule

    if request.user.is_demo:
        messages.error(request, "This section is view-only for demo accounts.")
        return redirect("export_data")

    frequency = request.POST.get("frequency", "once")
    export_time = request.POST.get("time", "03:00")
    selected_media_types = request.POST.getlist("media_types") or request.POST.getlist("media_types_checkboxes")
    include_lists = request.POST.get("include_lists") == "on"

    media_types = selected_media_types if selected_media_types else None

    def build_export_response():
        now = timezone.localtime()
        return StreamingHttpResponse(
            streaming_content=exports.generate_rows(
                request.user,
                media_types=media_types,
                include_lists=include_lists,
            ),
            content_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="yamtrack_{now}.csv"'},
        )

    if frequency == "once":
        logger.info("User %s started one-time CSV export", request.user.username)
        return build_export_response()

    try:
        parsed_time = dt.datetime.strptime(export_time, "%H:%M").time()
    except ValueError:
        messages.error(request, "Invalid export time.")
        return redirect("export_data")

    # Check for existing schedule
    existing = PeriodicTask.objects.filter(
        task="Scheduled backup export",
        kwargs__contains=f'"user_id": {request.user.id}',
        enabled=True,
    ).first()
    if existing:
        messages.error(request, "A backup schedule already exists. Delete it first to create a new one.")
        return redirect("export_data")

    if frequency == "daily":
        day_of_week = "*"
    elif frequency == "2days":
        day_of_week = "*/2"
    elif frequency == "weekly":
        day_of_week = "0"  # Sunday
    else:
        messages.error(request, "Invalid export frequency.")
        return redirect("export_data")

    crontab, _ = CrontabSchedule.objects.get_or_create(
        hour=parsed_time.hour,
        minute=parsed_time.minute,
        day_of_week=day_of_week,
        timezone=timezone.get_default_timezone(),
    )

    task_kwargs = {
        "user_id": request.user.id,
        "include_lists": include_lists,
    }
    if selected_media_types:
        task_kwargs["media_types"] = selected_media_types

    task_name = f"Backup export for {request.user.username} at {parsed_time} {frequency}"
    PeriodicTask.objects.create(
        name=task_name,
        task="Scheduled backup export",
        crontab=crontab,
        kwargs=json.dumps(task_kwargs),
        start_time=timezone.now(),
        enabled=True,
    )

    logger.info(
        "User %s created recurring export schedule (%s) and started CSV export",
        request.user.username,
        frequency,
    )
    return build_export_response()


@require_POST
def delete_export_schedule(request):
    """Delete a scheduled backup export."""
    task_name = request.POST.get("task_name")
    try:
        task = PeriodicTask.objects.get(
            name=task_name,
            kwargs__contains=f'"user_id": {request.user.id}',
        )
        task.delete()
        messages.success(request, "Backup schedule deleted.")
    except PeriodicTask.DoesNotExist:
        messages.error(request, "Backup schedule not found.")
    return redirect("export_data")


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


@require_POST
def update_plex_webhook_libraries(request):
    """Update selected Plex libraries allowed for webhook events."""
    redirect_target = request.POST.get("next") or "integrations"
    selected_libraries = request.POST.getlist("plex_webhook_libraries")

    deduplicated_libraries: list[str] = []
    seen: set[str] = set()
    for library in selected_libraries:
        value = (library or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        deduplicated_libraries.append(value)

    plex_account = getattr(request.user, "plex_account", None)
    valid_library_values: list[str] = []
    if plex_account and plex_account.plex_token:
        sections = plex_account.sections or []
        for section in sections:
            machine_identifier = section.get("machine_identifier")
            section_id = section.get("id")
            if machine_identifier and section_id:
                valid_library_values.append(f"{machine_identifier}::{section_id}")

    if valid_library_values:
        deduplicated_libraries = [
            value for value in deduplicated_libraries if value in valid_library_values
        ]

    request.user.plex_webhook_libraries = deduplicated_libraries
    request.user.save(update_fields=["plex_webhook_libraries"])
    messages.success(request, "Plex webhook libraries updated successfully")
    return redirect(redirect_target)


@login_required
@require_POST
def update_jellyseerr_settings(request):
    """Update Jellyseerr integration settings for the current user."""
    user = request.user

    raw_enabled = request.POST.get("jellyseerr_enabled")
    if raw_enabled is None:
        enabled = False
    else:
        enabled = str(raw_enabled).strip().lower() in {
            "on",
            "1",
            "true",
            "yes",
            "enabled",
        }

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
        ],
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
