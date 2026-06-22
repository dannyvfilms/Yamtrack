import json
import logging
import time
from uuid import uuid4

from django.apps import apps
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from app import discover
from app.discover import capabilities as discover_capabilities
from app.discover import tab_cache as discover_tab_cache
from app.discover import tabs as discover_tabs
from app.models import (
    TV,
    DiscoverFeedback,
    DiscoverFeedbackType,
    Item,
    MediaTypes,
    Season,
    Status,
)
from app.services import metadata_resolution
from app.signals import suppress_media_cache_change_signals
from app.templatetags import app_tags

logger = logging.getLogger(__name__)

DISCOVER_ALLOWED_MEDIA_TYPES = {
    MediaTypes.MOVIE.value,
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
    MediaTypes.MUSIC.value,
    MediaTypes.PODCAST.value,
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
    MediaTypes.MANGA.value,
    MediaTypes.GAME.value,
    MediaTypes.BOARDGAME.value,
}
DISCOVER_HIDDEN_SECTION = "hidden"
# Editorial registry rows now surfaced through the tab bar instead of stacked rows.
TABBED_EDITORIAL_ROW_KEYS = {
    "trending_right_now",
    "all_time_greats_unseen",
    "coming_soon",
}
DISCOVER_FAST_LOCAL_PLANNING_MEDIA_TYPES = {
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
}


def _coerce_discover_media_type(raw_media_type: str | None) -> str:
    media_type = (raw_media_type or "all").strip().lower()
    if media_type == "all":
        return "all"
    if media_type == DISCOVER_HIDDEN_SECTION:
        return DISCOVER_HIDDEN_SECTION
    if media_type in DISCOVER_ALLOWED_MEDIA_TYPES:
        return media_type
    return "all"


def _coerce_discover_debug(raw_debug: str | None) -> bool:
    return (raw_debug or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_discover_media_type_for_user(user, raw_media_type: str | None) -> str:
    media_type = _coerce_discover_media_type(raw_media_type)
    if media_type == DISCOVER_HIDDEN_SECTION:
        return DISCOVER_HIDDEN_SECTION
    return discover_tab_cache.resolve_media_type_for_user(user, media_type)


def _discover_media_options(user):
    enabled_media_types = [
        media_type
        for media_type in user.get_enabled_media_types()
        if media_type in DISCOVER_ALLOWED_MEDIA_TYPES
    ]
    if not enabled_media_types:
        enabled_media_types = sorted(DISCOVER_ALLOWED_MEDIA_TYPES)
    return [
        {"value": "all", "label": "All Media"},
        *[
            {
                "value": media_type,
                "label": app_tags.media_type_readable_plural(media_type),
            }
            for media_type in enabled_media_types
        ],
        {"value": DISCOVER_HIDDEN_SECTION, "label": "Hidden"},
    ]


def _discover_hidden_entries(user):
    return list(
        DiscoverFeedback.objects.filter(
            user=user,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        )
        .select_related("item")
        .order_by("-updated_at", "-id")
    )


def _media_type_has_tabs(media_type: str) -> bool:
    return bool(discover_tabs.get_tabs(media_type))


def _discover_tabs_payload(media_type: str, *, selected_tab: str):
    """Return the tab-bar payload (label, active state, availability) for templates."""
    availability = discover_capabilities.tab_availability(media_type)
    payload = []
    for tab in discover_tabs.get_tabs(media_type):
        state = availability.get(tab.key, {"enabled": True, "tooltip": None})
        payload.append(
            {
                "key": tab.key,
                "label": tab.label,
                "enabled": state["enabled"],
                "tooltip": state["tooltip"],
                "active": tab.key == selected_tab,
            },
        )
    return payload


def _resolve_discover_tab(request, media_type: str, rows):
    """Build the tab-bar payload, the selected tab's row, and the stacked rows.

    The default tab is the first *enabled* one (so a media type whose Trending
    provider key is missing opens on the first usable tab). When that default is
    the trending row, it is reused from the already-built ``rows`` (produced by
    the background tab cache, not rebuilt here); otherwise it is built on demand.
    Remaining editorial rows are dropped from the stacked list, leaving only the
    personalized rows.
    """
    selected_tab = discover_capabilities.first_enabled_tab(media_type)
    tab = discover_tabs.get_tab(media_type, selected_tab)
    rows_by_key = {row.key: row for row in rows}
    tab_row = rows_by_key.get(tab.row_key) if tab else None
    if tab is not None and tab_row is None and tab.row_key not in TABBED_EDITORIAL_ROW_KEYS:
        tab_row = discover.get_discover_tab_row(request.user, media_type, tab)
    stacked_rows = [row for row in rows if row.key not in TABBED_EDITORIAL_ROW_KEYS]
    return {
        "has_tabs": True,
        "discover_tabs": _discover_tabs_payload(media_type, selected_tab=selected_tab),
        "selected_tab": selected_tab,
        "tab_row": tab_row,
        "rows": stacked_rows,
    }


def _resolve_all_media_sections(request, rows):
    """Build per-media-type tabbed sections for the All Media view.

    Each enabled media type contributes one section (its Trending row by default)
    with its own tab bar; tabs swap that section's grid independently.
    """
    sections = []
    seen = set()
    for row in rows:
        media_type = row.component_media_type
        if (
            not media_type
            or media_type in seen
            or row.key != "trending_right_now"
            or not _media_type_has_tabs(media_type)
        ):
            continue
        seen.add(media_type)
        selected_tab = discover_capabilities.first_enabled_tab(media_type)
        tab = discover_tabs.get_tab(media_type, selected_tab)
        section_row = row
        if tab is not None and tab.row_key != row.key:
            section_row = discover.get_discover_tab_row(request.user, media_type, tab)
        sections.append(
            {
                "media_type": media_type,
                "label": app_tags.media_type_readable_plural(media_type),
                "tabs": _discover_tabs_payload(media_type, selected_tab=selected_tab),
                "selected_tab": selected_tab,
                "row": section_row,
            },
        )
    return sections


def _discover_rows_context(
    request,
    *,
    selected_media_type: str,
    show_more: bool,
    discover_debug: bool,
    rows,
):
    if selected_media_type == DISCOVER_HIDDEN_SECTION:
        hidden_discover_entries = _discover_hidden_entries(request.user)
        return {
            "selected_media_type": selected_media_type,
            "show_more": show_more,
            "discover_debug": discover_debug,
            "discover_loading": False,
            "discover_activity_version": "",
            "rows": [],
            "hidden_discover_entries": hidden_discover_entries,
            "hidden_discover_count": len(hidden_discover_entries),
        }

    discover_status = (
        discover_tab_cache.get_tab_status(
            request.user.id,
            selected_media_type,
            show_more=show_more,
        )
        if not discover_debug
        else None
    )
    context = {
        "selected_media_type": selected_media_type,
        "show_more": show_more,
        "discover_debug": discover_debug,
        "discover_loading": bool(discover_status and discover_status["is_refreshing"]),
        "discover_activity_version": (
            discover_tab_cache.get_activity_version(
                request.user.id,
                selected_media_type,
            )
            if not discover_debug
            else ""
        ),
        "has_tabs": False,
        "discover_tabs": [],
        "selected_tab": None,
        "tab_row": None,
        "all_media_sections": [],
        "rows": rows,
    }
    if _media_type_has_tabs(selected_media_type):
        context.update(_resolve_discover_tab(request, selected_media_type, rows))
    elif selected_media_type == "all":
        context["all_media_sections"] = _resolve_all_media_sections(request, rows)
    return context


def _apply_discover_response_headers(
    response,
    *,
    user_id: int,
    selected_media_type: str,
    show_more: bool,
    discover_debug: bool,
):
    response["X-Discover-Media-Type"] = selected_media_type
    response["X-Discover-Show-More"] = "1" if show_more else "0"
    if not discover_debug and selected_media_type != DISCOVER_HIDDEN_SECTION:
        response["X-Discover-Activity-Version"] = discover_tab_cache.get_activity_version(
            user_id,
            selected_media_type,
        )
    return response


def _render_discover_rows_fragment(
    request,
    *,
    selected_media_type: str,
    show_more: bool,
    discover_debug: bool,
    rows,
):
    response = render(
        request,
        "app/components/discover_rows.html",
        _discover_rows_context(
            request,
            selected_media_type=selected_media_type,
            show_more=show_more,
            discover_debug=discover_debug,
            rows=rows,
        ),
    )
    return _apply_discover_response_headers(
        response,
        user_id=request.user.id,
        selected_media_type=selected_media_type,
        show_more=show_more,
        discover_debug=discover_debug,
    )


def _render_discover_row_fragment(
    request,
    *,
    selected_media_type: str,
    show_more: bool,
    discover_debug: bool,
    row,
):
    response = render(
        request,
        "app/components/discover_row.html",
        {
            "selected_media_type": selected_media_type,
            "show_more": show_more,
            "discover_debug": discover_debug,
            "row": row,
        },
    )
    return _apply_discover_response_headers(
        response,
        user_id=request.user.id,
        selected_media_type=selected_media_type,
        show_more=show_more,
        discover_debug=discover_debug,
    )


def _discover_response_rows(
    user,
    *,
    selected_media_type: str,
    show_more: bool,
    discover_debug: bool,
):
    if selected_media_type == DISCOVER_HIDDEN_SECTION:
        return []
    if discover_debug:
        return discover.get_discover_rows(
            user,
            selected_media_type,
            show_more=show_more,
            include_debug=True,
            defer_artwork=False,
        )
    return discover_tab_cache.get_tab_rows(
        user,
        selected_media_type,
        show_more=show_more,
        include_debug=False,
        defer_artwork=False,
        allow_inline_bootstrap=True,
    )


def _discover_candidate_seed(request) -> dict:
    return {
        "fallback_title": request.POST.get("title", "").strip(),
        "fallback_image": request.POST.get("image", "").strip() or None,
        "fallback_release_date": request.POST.get("release_date", "").strip() or None,
    }


def _get_or_create_discover_item(media_type, media_id, source, season_number, seed):
    """Get or create a minimal Item for a dismiss action — no external API call."""
    item, _ = Item.objects.get_or_create(
        media_id=media_id,
        source=source,
        media_type=media_type,
        season_number=season_number,
        episode_number=None,
        defaults={
            "title": seed.get("fallback_title") or "",
            "image": seed.get("fallback_image") or "",
        },
    )
    return item


def _discover_model_for_media_type(
    media_type: str,
    *,
    source: str | None = None,
    identity_media_type: str | None = None,
):
    model_name = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
        identity_media_type=identity_media_type,
    )
    return apps.get_model(app_label="app", model_name=model_name)


def _discover_planning_instance(
    user,
    media_type: str,
    item: Item,
    *,
    source: str | None = None,
    identity_media_type: str | None = None,
):
    model = _discover_model_for_media_type(
        media_type,
        source=source or item.source,
        identity_media_type=identity_media_type or item.media_type,
    )
    return model.objects.filter(user=user, item=item).select_related("item").first()


def _mark_discover_stale_without_refresh(user_id: int, media_type: str) -> list[str]:
    """Mark Discover payloads stale without enqueueing background rebuilds."""
    targets = discover_tab_cache.get_user_target_media_types_for_change(
        user_id,
        media_type,
    )
    for target_media_type in targets:
        discover_tab_cache.bump_activity_version(user_id, target_media_type)
        discover_tab_cache.clear_lower_level_cache(user_id, target_media_type)
    return targets


def _invalidate_discover_after_action(
    user_id: int,
    media_type: str,
    *,
    discover_debug: bool,
    feedback_change: bool,
) -> list[str]:
    """Invalidate Discover after a quick action, avoiding debug-mode task overlap."""
    if discover_debug:
        return _mark_discover_stale_without_refresh(user_id, media_type)
    if feedback_change:
        return discover_tab_cache.invalidate_for_feedback_change(user_id, media_type)
    return discover_tab_cache.invalidate_for_media_change(user_id, media_type)


@login_required
@require_GET
def discover_page(request):
    """Render Discover page with selected media rows."""
    raw_param = request.GET.get("media_type")
    if raw_param is not None:
        selected_media_type = _resolve_discover_media_type_for_user(
            request.user,
            raw_param,
        )
        request.user.update_preference("last_discover_type", selected_media_type)
    else:
        selected_media_type = _resolve_discover_media_type_for_user(
            request.user,
            request.user.last_discover_type,
        )
    show_more = request.GET.get("show_more") in {"1", "true", "True"}
    discover_debug = _coerce_discover_debug(request.GET.get("discover_debug"))
    rows = _discover_response_rows(
        request.user,
        selected_media_type=selected_media_type,
        show_more=show_more,
        discover_debug=discover_debug,
    )
    if not discover_debug and selected_media_type != DISCOVER_HIDDEN_SECTION:
        discover_tab_cache.warm_sibling_tabs(
            request.user,
            selected_media_type,
            show_more=show_more,
        )
    context = _discover_rows_context(
        request,
        selected_media_type=selected_media_type,
        show_more=show_more,
        discover_debug=discover_debug,
        rows=rows,
    )
    context["discover_media_options"] = _discover_media_options(request.user)
    return render(request, "app/discover.html", context)


@login_required
@require_GET
def discover_rows(request):
    """Render Discover rows partial for HTMX row switching."""
    selected_media_type = _resolve_discover_media_type_for_user(
        request.user,
        request.GET.get("media_type"),
    )
    request.user.update_preference("last_discover_type", selected_media_type)
    show_more = request.GET.get("show_more") in {"1", "true", "True"}
    discover_debug = _coerce_discover_debug(request.GET.get("discover_debug"))
    rows = _discover_response_rows(
        request.user,
        selected_media_type=selected_media_type,
        show_more=show_more,
        discover_debug=discover_debug,
    )
    return _render_discover_rows_fragment(
        request,
        selected_media_type=selected_media_type,
        show_more=show_more,
        discover_debug=discover_debug,
        rows=rows,
    )


@login_required
@require_GET
def discover_tab(request):
    """Render a single editorial tab's grid for HTMX tab switching."""
    selected_media_type = _resolve_discover_media_type_for_user(
        request.user,
        request.GET.get("media_type"),
    )
    if not _media_type_has_tabs(selected_media_type):
        return HttpResponseBadRequest("Tabs are not available for this media type.")

    tab_key = (request.GET.get("tab") or "").strip()
    tab = discover_tabs.get_tab(selected_media_type, tab_key)
    if tab is None:
        return HttpResponseBadRequest("Unknown Discover tab.")

    availability = discover_capabilities.tab_availability(selected_media_type)
    if not availability.get(tab_key, {}).get("enabled", False):
        return HttpResponseBadRequest("This Discover tab is not available.")

    discover_debug = _coerce_discover_debug(request.GET.get("discover_debug"))
    tab_row = discover.get_discover_tab_row(request.user, selected_media_type, tab)

    # All-media sections swap only their grid (header + tab bar stay put) and keep
    # the "all" action context, so post-action refresh stays on the All Media tab.
    if request.GET.get("layout") == "grid":
        active_media_type = request.GET.get("active_media_type") or selected_media_type
        response = render(
            request,
            "app/components/discover_grid.html",
            {
                "row": tab_row,
                "discover_active_media_type": active_media_type,
                "show_more": False,
                "discover_debug": discover_debug,
            },
        )
        return _apply_discover_response_headers(
            response,
            user_id=request.user.id,
            selected_media_type=selected_media_type,
            show_more=False,
            discover_debug=discover_debug,
        )

    return _render_discover_row_fragment(
        request,
        selected_media_type=selected_media_type,
        show_more=False,
        discover_debug=discover_debug,
        row=tab_row,
    )


@login_required
@require_POST
def refresh_discover(request):
    """Invalidate the active Discover tab cache and queue a background refresh."""
    media_type = _resolve_discover_media_type_for_user(
        request.user,
        request.POST.get("media_type"),
    )
    show_more = request.POST.get("show_more") in {"1", "true", "True"}
    discover_tab_cache.mark_active(
        request.user.id,
        media_type,
        show_more=show_more,
    )

    discover_tab_cache.bump_activity_version(request.user.id, media_type)
    discover_tab_cache.clear_row_cache(request.user.id, media_type)
    discover_tab_cache.schedule_tab_refresh(
        request.user.id,
        media_type,
        show_more=show_more,
        debounce_seconds=discover_tab_cache.DISCOVER_PRIORITY_REFRESH_DEBOUNCE_SECONDS,
        countdown=discover_tab_cache.DISCOVER_PRIORITY_REFRESH_COUNTDOWN,
        force=True,
        clear_provider_cache=True,
    )

    return JsonResponse(
        {
            "ok": True,
            "media_type": media_type,
            "show_more": show_more,
            "targets": [media_type],
        },
    )


@login_required
@require_POST
def discover_action(request):
    """Handle Discover quick actions and return the updated rows fragment."""
    from app import views as view_barrel

    request_id = uuid4().hex[:8]
    request_started = time.monotonic()
    action = (request.POST.get("action") or "").strip().lower()
    active_media_type = _resolve_discover_media_type_for_user(
        request.user,
        request.POST.get("active_media_type"),
    )
    show_more = request.POST.get("show_more") in {"1", "true", "True"}
    discover_debug = _coerce_discover_debug(request.POST.get("discover_debug"))
    logger.info(
        "discover_action_start request_id=%s user_id=%s action=%s active_media_type=%s "
        "show_more=%s discover_debug=%s",
        request_id,
        request.user.id,
        action or "invalid",
        active_media_type,
        int(bool(show_more)),
        int(bool(discover_debug)),
    )
    discover_tab_cache.mark_active(
        request.user.id,
        active_media_type,
        show_more=show_more,
    )

    if action == "undo":
        undo_started = time.monotonic()
        undo_token = (request.POST.get("undo_token") or "").strip()
        snapshot = discover_tab_cache.get_undo_snapshot(request.user.id, undo_token)
        if not snapshot:
            return HttpResponseBadRequest("Invalid undo token")

        side_effect = snapshot.get("side_effect") or {}
        side_effect_kind = side_effect.get("kind")
        if side_effect_kind == "planning" and side_effect.get("instance_id"):
            model = _discover_model_for_media_type(
                side_effect.get("media_type"),
                source=side_effect.get("source"),
                identity_media_type=side_effect.get("identity_media_type"),
            )
            instance = model.objects.filter(
                id=side_effect["instance_id"],
                user=request.user,
            ).first()
            if instance:
                with suppress_media_cache_change_signals():
                    instance.delete()
                view_barrel._invalidate_discover_after_action(
                    request.user.id,
                    side_effect.get("media_type"),
                    discover_debug=discover_debug,
                    feedback_change=False,
                )
        elif side_effect_kind == "dismiss" and side_effect.get("feedback_id"):
            feedback = DiscoverFeedback.objects.filter(
                id=side_effect["feedback_id"],
                user=request.user,
            ).first()
            if feedback:
                media_type = feedback.item.media_type
                feedback.delete()
                view_barrel._invalidate_discover_after_action(
                    request.user.id,
                    media_type,
                    discover_debug=discover_debug,
                    feedback_change=True,
                )

        restored_snapshot = discover_tab_cache.restore_undo_snapshot(
            request.user.id,
            undo_token,
        )
        rows = (
            restored_snapshot.get("rows")
            if restored_snapshot and not discover_debug
            else None
        )
        if rows is None:
            rows = _discover_response_rows(
                request.user,
                selected_media_type=active_media_type,
                show_more=show_more,
                discover_debug=discover_debug,
            )

        response = _render_discover_rows_fragment(
            request,
            selected_media_type=active_media_type,
            show_more=show_more,
            discover_debug=discover_debug,
            rows=rows,
        )
        response["HX-Trigger"] = json.dumps(
            {
                "discoverActionComplete": {
                    "action": "undo",
                    "message": "Discover action undone.",
                },
            },
        )
        logger.info(
            "discover_action_complete request_id=%s user_id=%s action=undo active_media_type=%s "
            "rows=%s restored_snapshot=%s total_ms=%s",
            request_id,
            request.user.id,
            active_media_type,
            len(rows or []),
            int(bool(restored_snapshot)),
            int((time.monotonic() - undo_started) * 1000),
        )
        return response

    if action not in {"planning", "dismiss"}:
        return HttpResponseBadRequest("Invalid action")

    candidate_media_type = (request.POST.get("candidate_media_type") or "").strip().lower()
    source = (request.POST.get("source") or "").strip()
    media_id = (request.POST.get("media_id") or "").strip()
    identity_media_type = (request.POST.get("identity_media_type") or "").strip() or None
    library_media_type = (request.POST.get("library_media_type") or "").strip() or None
    if (
        candidate_media_type not in DISCOVER_ALLOWED_MEDIA_TYPES
        or not source
        or not media_id
    ):
        return HttpResponseBadRequest("Missing candidate fields")

    season_number = request.POST.get("season_number")
    season_number = int(season_number) if season_number not in (None, "") else None
    row_key = (request.POST.get("row_key") or "").strip()
    candidate_seed = _discover_candidate_seed(request)
    logger.info(
        "discover_action_candidate request_id=%s user_id=%s action=%s active_media_type=%s "
        "candidate_media_type=%s source=%s media_id=%s row_key=%s show_more=%s",
        request_id,
        request.user.id,
        action,
        active_media_type,
        candidate_media_type,
        source,
        media_id,
        row_key or "-",
        int(bool(show_more)),
    )

    undo_token: str | None = None
    message = ""
    _action_payloads: list[dict] | None = None
    action_stage_started = time.monotonic()
    mutation_ms = 0
    metadata_strategy = "-"
    if action == "planning":
        if candidate_media_type in DISCOVER_FAST_LOCAL_PLANNING_MEDIA_TYPES:
            hydrated = view_barrel.ensure_item_metadata_from_discover_seed(
                candidate_media_type,
                media_id,
                source,
                season_number,
                identity_media_type=identity_media_type,
                library_media_type=library_media_type,
                **candidate_seed,
            )
            metadata_strategy = "local_seed"
        else:
            hydrated = view_barrel.ensure_item_metadata(
                request.user,
                candidate_media_type,
                media_id,
                source,
                season_number,
                identity_media_type=identity_media_type,
                library_media_type=library_media_type,
                **candidate_seed,
            )
            metadata_strategy = "provider_fetch"
        existing_instance = _discover_planning_instance(
            request.user,
            candidate_media_type,
            hydrated.item,
            source=source,
            identity_media_type=identity_media_type,
        )
        if existing_instance:
            DiscoverFeedback.objects.filter(
                user=request.user,
                item=hydrated.item,
                feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
            ).delete()
            view_barrel._invalidate_discover_after_action(
                request.user.id,
                candidate_media_type,
                discover_debug=discover_debug,
                feedback_change=True,
            )
            message = f'"{hydrated.item.title}" is already in your library.'
        else:
            undo_token = discover_tab_cache.store_undo_snapshot(
                request.user.id,
                action="planning",
                active_media_type=active_media_type,
                candidate_media_type=candidate_media_type,
                show_more=show_more,
            )
            model = _discover_model_for_media_type(
                candidate_media_type,
                source=source,
                identity_media_type=identity_media_type,
            )
            instance_kwargs = {
                "item": hydrated.item,
                "user": request.user,
                "status": Status.PLANNING.value,
                "score": None,
                "notes": "",
            }
            if model not in {TV, Season}:
                instance_kwargs["progress"] = 0
                instance_kwargs["start_date"] = None
                instance_kwargs["end_date"] = None
            instance = model(**instance_kwargs)
            if candidate_media_type == MediaTypes.MUSIC.value:
                instance.artist = hydrated.artist
                instance.album = hydrated.album
                instance.track = hydrated.track
            if (
                candidate_media_type == MediaTypes.PODCAST.value
                and hydrated.podcast_show is not None
            ):
                instance.show = hydrated.podcast_show
            with suppress_media_cache_change_signals():
                instance.save()
            view_barrel._invalidate_discover_after_action(
                request.user.id,
                candidate_media_type,
                discover_debug=discover_debug,
                feedback_change=False,
            )
            if undo_token:
                discover_tab_cache.update_undo_snapshot(
                    request.user.id,
                    undo_token,
                    side_effect={
                        "kind": "planning",
                        "media_type": candidate_media_type,
                        "source": source,
                        "identity_media_type": identity_media_type,
                        "instance_id": instance.id,
                    },
                )
            message = f'Added "{hydrated.item.title}" to Planning.'
        mutation_ms = int((time.monotonic() - action_stage_started) * 1000)
    else:
        item = _get_or_create_discover_item(
            candidate_media_type,
            media_id,
            source,
            season_number,
            candidate_seed,
        )
        item_title = item.title or candidate_seed.get("fallback_title", "")
        _action_payloads = discover_tab_cache.collect_action_payloads(
            request.user.id,
            active_media_type,
            candidate_media_type,
        )
        existing_feedback = DiscoverFeedback.objects.filter(
            user=request.user,
            item=item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        ).first()
        if existing_feedback is None:
            undo_token = discover_tab_cache.store_undo_snapshot(
                request.user.id,
                action="dismiss",
                active_media_type=active_media_type,
                candidate_media_type=candidate_media_type,
                show_more=show_more,
                preloaded_payloads=_action_payloads,
            )
        feedback, created = DiscoverFeedback.objects.update_or_create(
            user=request.user,
            item=item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
            defaults={
                "source_context": "discover",
                "row_key": row_key,
            },
        )
        view_barrel._invalidate_discover_after_action(
            request.user.id,
            candidate_media_type,
            discover_debug=discover_debug,
            feedback_change=True,
        )
        if undo_token and created:
            discover_tab_cache.update_undo_snapshot(
                request.user.id,
                undo_token,
                side_effect={
                    "kind": "dismiss",
                    "media_type": candidate_media_type,
                    "feedback_id": feedback.id,
                },
            )
        elif not created:
            undo_token = None
        message = f'Hidden "{item_title}" from Discover.'
        mutation_ms = int((time.monotonic() - action_stage_started) * 1000)

    cache_patch_started = time.monotonic()
    rows = None
    if not discover_debug:
        rows = discover_tab_cache.apply_cached_action(
            request.user.id,
            active_media_type,
            candidate_media_type,
            media_id=media_id,
            source=source,
            show_more=show_more,
            preloaded_payloads=_action_payloads,
        )
    cache_patch_ms = int((time.monotonic() - cache_patch_started) * 1000)
    row_fetch_started = time.monotonic()
    if rows is None:
        rows = _discover_response_rows(
            request.user,
            selected_media_type=active_media_type,
            show_more=show_more,
            discover_debug=discover_debug,
        )
    row_fetch_ms = int((time.monotonic() - row_fetch_started) * 1000)

    render_started = time.monotonic()
    updated_row = None
    if row_key:
        updated_row = next((row for row in rows if row.key == row_key), None)

    if updated_row is not None:
        response = _render_discover_row_fragment(
            request,
            selected_media_type=active_media_type,
            show_more=show_more,
            discover_debug=discover_debug,
            row=updated_row,
        )
    else:
        response = _render_discover_rows_fragment(
            request,
            selected_media_type=active_media_type,
            show_more=show_more,
            discover_debug=discover_debug,
            rows=rows,
        )
    render_ms = int((time.monotonic() - render_started) * 1000)
    trigger_payload = {
        "action": action,
        "message": message,
        "active_media_type": active_media_type,
    }
    if undo_token:
        trigger_payload["undo_token"] = undo_token
    response["HX-Trigger"] = json.dumps(
        {
            "discoverActionComplete": trigger_payload,
        },
    )
    logger.info(
        "discover_action_complete request_id=%s user_id=%s action=%s active_media_type=%s "
        "candidate_media_type=%s source=%s media_id=%s row_key=%s rows=%s undo=%s "
        "metadata_strategy=%s mutation_ms=%s cache_patch_ms=%s row_fetch_ms=%s render_ms=%s total_ms=%s",
        request_id,
        request.user.id,
        action,
        active_media_type,
        candidate_media_type,
        source,
        media_id,
        row_key or "-",
        len(rows or []),
        int(bool(undo_token)),
        metadata_strategy,
        mutation_ms,
        cache_patch_ms,
        row_fetch_ms,
        render_ms,
        int((time.monotonic() - request_started) * 1000),
    )
    return response


def _build_track_modal_discover_tab_context(user, metadata_item):
    """Build shared Discover-tab context for the track modal."""
    return {
        "discover_tab_available": metadata_item is not None,
        "is_hidden_from_discover": (
            DiscoverFeedback.objects.filter(
                user=user,
                item=metadata_item,
                feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
            ).exists()
            if metadata_item
            else False
        ),
    }


@login_required
@require_POST
def discover_toggle_hidden(request):
    """Toggle the hidden status of an item from Discover."""
    from app import views as view_barrel

    item_id = request.POST.get("item_id")
    action = request.POST.get("action")
    if action not in {"hide", "unhide"}:
        return HttpResponseBadRequest("Invalid Discover visibility action.")

    item = get_object_or_404(Item, id=item_id)

    if action == "hide":
        DiscoverFeedback.objects.update_or_create(
            user=request.user,
            item=item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
            defaults={"source_context": "track_modal"},
        )
        message = f'Hidden "{item.title}" from Discover.'
    else:
        DiscoverFeedback.objects.filter(
            user=request.user,
            item=item,
            feedback_type=DiscoverFeedbackType.NOT_INTERESTED.value,
        ).delete()
        message = f'Showing "{item.title}" in Discover.'

    view_barrel._invalidate_discover_after_action(
        request.user.id,
        item.library_media_type or item.media_type,
        discover_debug=False,
        feedback_change=True,
    )

    context = {
        "item": item,
        **_build_track_modal_discover_tab_context(request.user, item),
    }

    response = render(request, "app/components/discover_tab_content.html", context)
    response["HX-Trigger"] = json.dumps(
        {
            "discoverActionComplete": {
                "action": action,
                "message": message,
            },
        },
    )
    return response
