import logging
import math
from collections import Counter, defaultdict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, Paginator
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from app import helpers
from app.forms import CollectionEntryForm
from app.log_safety import exception_summary
from app.models import CollectionEntry, Game, Item, MediaTypes, Status
from app.providers import services
from app.services import metadata_resolution
from integrations.models import CollectionSourceState

logger = logging.getLogger(__name__)


@require_GET
def collection_list(request, media_type=None):
    """Display user's collection, optionally filtered by media_type."""
    collection = helpers.get_user_collection(request.user, media_type)
    paginator = Paginator(collection, 20)
    page_number = request.GET.get("page", 1)

    try:
        page_obj = paginator.page(page_number)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return render(
        request,
        "app/collection_list.html",
        {
            "collection_entries": page_obj,
            "media_type": media_type,
        },
    )


def _collection_redirect(request):
    """Redirect to a safe next URL when present, otherwise collection list."""
    next_url = request.GET.get("next") or request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("collection_list")


@require_POST
def collection_add(request):
    """Add a new owned copy to collection (with optional metadata)."""
    item_id = request.POST.get("item_id")
    if not item_id:
        if request.headers.get("HX-Request"):
            return HttpResponseBadRequest("Item ID is required")
        messages.error(request, "Item ID is required")
        return _collection_redirect(request)

    try:
        item = Item.objects.get(id=item_id)
    except Item.DoesNotExist:
        if request.headers.get("HX-Request"):
            return HttpResponseBadRequest("Item not found")
        messages.error(request, "Item not found")
        return _collection_redirect(request)

    post_data = request.POST.copy()
    post_data["item"] = item.id

    form = CollectionEntryForm(
        post_data,
        user=request.user,
        collection_media_type=item.media_type,
    )

    if form.is_valid():
        entry = form.save(commit=False)
        entry.user = request.user
        entry.item = item
        entry.save()

        if item.media_type == MediaTypes.GAME.value:
            game_exists = Game.objects.filter(user=request.user, item=item).exists()
            if not game_exists:
                Game.objects.create(
                    user=request.user,
                    item=item,
                    status=Status.PLANNING.value,
                    progress=0,
                )

        collected_at = form.cleaned_data.get("collected_at")
        if collected_at:
            CollectionEntry.objects.filter(id=entry.id).update(collected_at=collected_at)
            entry.collected_at = collected_at
        messages.success(request, f"Added {item.title} to collection")
        if request.headers.get("HX-Request"):
            return JsonResponse({"success": True, "message": f"Added {item.title} to collection"})
    else:
        helpers.form_error_messages(form, request)
        if request.headers.get("HX-Request"):
            return JsonResponse({"success": False, "errors": form.errors}, status=400)
    return _collection_redirect(request)


@require_POST
def collection_update(request, entry_id):
    """Update collection entry metadata."""
    try:
        entry = CollectionEntry.objects.get(id=entry_id, user=request.user)
    except CollectionEntry.DoesNotExist:
        from django.http import Http404

        raise Http404("Collection entry not found")

    form = CollectionEntryForm(
        request.POST,
        instance=entry,
        user=request.user,
        collection_media_type=entry.item.media_type,
    )
    if form.is_valid():
        entry = form.save()
        collected_at = form.cleaned_data.get("collected_at")
        if collected_at:
            CollectionEntry.objects.filter(id=entry.id).update(collected_at=collected_at)
            entry.collected_at = collected_at
        messages.success(request, f"Updated collection entry for {entry.item.title}")
        if request.headers.get("HX-Request"):
            return JsonResponse({"success": True, "message": "Updated collection entry"})
    else:
        helpers.form_error_messages(form, request)
        if request.headers.get("HX-Request"):
            return JsonResponse({"success": False, "errors": form.errors}, status=400)
    return _collection_redirect(request)


@require_POST
def collection_remove(request, entry_id):
    """Remove item from collection."""
    try:
        entry = CollectionEntry.objects.get(id=entry_id, user=request.user)
    except CollectionEntry.DoesNotExist:
        from django.http import Http404

        raise Http404("Collection entry not found")

    item_title = entry.item.title
    entry.delete()
    messages.success(request, f"Removed {item_title} from collection")

    if request.headers.get("HX-Request"):
        return JsonResponse({"success": True, "message": f"Removed {item_title} from collection"})
    return _collection_redirect(request)


@require_POST
def collection_remove_season(request, season_item_id):
    """Remove Sonarr-backed collected episode rows for a season summary chip."""
    season_item = get_object_or_404(
        Item,
        id=season_item_id,
        media_type=MediaTypes.SEASON.value,
    )
    season_title = (
        "Specials"
        if season_item.season_number == 0
        else f"Season {season_item.season_number}"
    )
    deleted_count, _deleted_objects = CollectionEntry.objects.filter(
        user=request.user,
        item__media_id=season_item.media_id,
        item__source=season_item.source,
        item__media_type=MediaTypes.EPISODE.value,
        item__season_number=season_item.season_number,
        item__source_states__user=request.user,
        item__source_states__source="sonarr",
    ).delete()

    if deleted_count:
        messages.success(request, f"Removed {season_title} from collection")
    else:
        messages.error(request, f"No collected episodes found for {season_title}")

    if request.headers.get("HX-Request"):
        message = (
            f"Removed {season_title} from collection"
            if deleted_count
            else f"No collected episodes found for {season_title}"
        )
        return JsonResponse(
            {
                "success": bool(deleted_count),
                "message": message,
            },
            status=200 if deleted_count else 404,
        )
    return _collection_redirect(request)


def _collection_source_labels_by_item_id(user, item_ids):
    """Return source labels grouped by item id for collection auditing."""
    if not item_ids:
        return {}

    source_labels = dict(CollectionSourceState.SOURCE_CHOICES)
    source_labels_by_item_id = defaultdict(list)
    for state in CollectionSourceState.objects.filter(
        user=user,
        item_id__in=item_ids,
    ).order_by("source"):
        label = source_labels.get(state.source, state.source.title())
        if label not in source_labels_by_item_id[state.item_id]:
            source_labels_by_item_id[state.item_id].append(label)
    return source_labels_by_item_id


def _collection_quality_labels_by_item_id(user, item_ids, *, source=None):
    """Return reported quality labels grouped by item id."""
    if not item_ids:
        return {}

    source_states = CollectionSourceState.objects.filter(
        user=user,
        item_id__in=item_ids,
    ).exclude(quality_label="")
    if source:
        source_states = source_states.filter(source=source)

    quality_labels_by_item_id = {}
    for state in source_states.order_by(
        "source",
        "-last_source_updated_at",
        "-last_synced_at",
        "-id",
    ):
        quality_labels_by_item_id.setdefault(state.item_id, state.quality_label)
    return quality_labels_by_item_id


def _most_common_quality_label(labels):
    """Return the most common non-empty quality label."""
    normalized_labels = [
        str(label).strip()
        for label in labels
        if str(label or "").strip()
    ]
    if not normalized_labels:
        return ""
    return Counter(normalized_labels).most_common(1)[0][0]


def _format_collection_progress(label, collected_count, total_count):
    """Render a consistent progress label for modal audit rows."""
    progress = f"{label}: {collected_count}/{total_count}"
    if total_count > 0:
        progress += f" • {math.floor((collected_count / total_count) * 100)}%"
    return progress


def _format_collection_progress_value(collected_count, total_count):
    """Render a progress value without the leading label."""
    progress = f"{collected_count}/{total_count}"
    if total_count > 0:
        progress += f" • {math.floor((collected_count / total_count) * 100)}%"
    return progress


def _sonarr_episode_collection_entries(user, item, *, season_number=None):
    """Return episode collection rows that are backed by Sonarr source state."""
    episode_entries = CollectionEntry.objects.filter(
        user=user,
        item__media_id=item.media_id,
        item__source=item.source,
        item__media_type=MediaTypes.EPISODE.value,
        item__source_states__user=user,
        item__source_states__source="sonarr",
    )
    if season_number is not None:
        episode_entries = episode_entries.filter(item__season_number=season_number)

    return list(
        episode_entries.select_related("item").order_by(
            "item__season_number",
            "item__episode_number",
            "-collected_at",
            "-id",
        ),
    )


def _item_has_collection_source_state(user, item, *, source=None):
    """Return True when the current item still has sync-owned source state."""
    source_states = CollectionSourceState.objects.filter(
        user=user,
        item=item,
    )
    if source:
        source_states = source_states.filter(source=source)
    return source_states.exists()


def _build_collection_season_audit_entries(user, item):
    """Return season-level Sonarr summaries for TV/anime show modal auditing."""
    supported_media_types = {
        MediaTypes.TV.value,
        MediaTypes.ANIME.value,
    }
    if item.media_type not in supported_media_types:
        return []

    season_items = list(
        Item.objects.filter(
            media_id=item.media_id,
            source=item.source,
            media_type=MediaTypes.SEASON.value,
        )
        .exclude(season_number=0)
        .order_by("season_number", "id"),
    )
    if not season_items:
        return []

    episode_items = list(
        Item.objects.filter(
            media_id=item.media_id,
            source=item.source,
            media_type=MediaTypes.EPISODE.value,
        )
        .exclude(season_number=0)
        .order_by("season_number", "episode_number", "id"),
    )
    sonarr_episode_entries = _sonarr_episode_collection_entries(user, item)
    if not sonarr_episode_entries:
        return []

    season_item_ids = [season_item.id for season_item in season_items]
    sonarr_episode_item_ids = [entry.item_id for entry in sonarr_episode_entries]
    source_labels_by_item_id = _collection_source_labels_by_item_id(
        user,
        season_item_ids + sonarr_episode_item_ids,
    )
    quality_labels_by_item_id = _collection_quality_labels_by_item_id(
        user,
        sonarr_episode_item_ids,
        source="sonarr",
    )

    episode_item_ids_by_season_number = defaultdict(set)
    for episode_item in episode_items:
        if episode_item.season_number is None:
            continue
        episode_item_ids_by_season_number[episode_item.season_number].add(episode_item.id)

    collected_episode_ids_by_season_number = defaultdict(set)
    for episode_entry in sonarr_episode_entries:
        season_number = episode_entry.item.season_number
        if season_number is None:
            continue
        collected_episode_ids_by_season_number[season_number].add(episode_entry.item_id)

    season_audit_entries = []
    for season_item in season_items:
        season_number = season_item.season_number
        if season_number is None:
            continue

        total_episodes = len(episode_item_ids_by_season_number.get(season_number, set()))
        collected_episode_ids = collected_episode_ids_by_season_number.get(season_number, set())
        collected_count = min(total_episodes, len(collected_episode_ids))
        if collected_count == 0:
            continue

        source_labels = []
        for episode_item_id in sorted(collected_episode_ids):
            for label in source_labels_by_item_id.get(episode_item_id, []):
                if label not in source_labels:
                    source_labels.append(label)
        if not source_labels:
            source_labels = ["Sonarr"]

        collection_entry = helpers.get_season_collection_metadata(user, season_item) or {
            "resolution": "",
            "hdr": "",
            "audio_codec": "",
            "audio_channels": "",
            "bitrate": None,
            "media_type": "",
            "is_3d": False,
            "collected_at": None,
        }
        season_title = "Specials" if season_number == 0 else f"Season {season_number}"
        progress_value = _format_collection_progress_value(
            collected_count,
            total_episodes,
        )
        quality_label = _most_common_quality_label(
            [
                quality_labels_by_item_id.get(episode_item_id, "")
                for episode_item_id in sorted(collected_episode_ids)
            ],
        )
        season_audit_entries.append(
            {
                "collection_entry": collection_entry,
                "season_item_id": season_item.id,
                "title": season_title,
                "display_title": f"{season_title}: {progress_value}",
                "progress_label": _format_collection_progress(
                    "Collected Episodes",
                    collected_count,
                    total_episodes,
                ),
                "source_labels": source_labels,
                "quality_label": quality_label,
            },
        )

    return season_audit_entries


def _build_collection_episode_audit_entries(user, item):
    """Return episode-level Sonarr rows for season modal auditing."""
    if item.media_type != MediaTypes.SEASON.value:
        return []

    episode_entries = _sonarr_episode_collection_entries(
        user,
        item,
        season_number=item.season_number,
    )
    if not episode_entries:
        return []

    item_ids = {entry.item_id for entry in episode_entries}
    source_labels_by_item_id = _collection_source_labels_by_item_id(user, item_ids)
    quality_labels_by_item_id = _collection_quality_labels_by_item_id(
        user,
        item_ids,
        source="sonarr",
    )

    audit_entries = []
    for entry in episode_entries:
        episode_item = entry.item
        season_number = episode_item.season_number or 0
        episode_number = episode_item.episode_number or 0
        title = episode_item.title or f"Episode {episode_item.episode_number or 0}"
        audit_entries.append(
            {
                "entry": entry,
                "title": f"S{season_number:02d}E{episode_number:02d} - {title}",
                "source_labels": source_labels_by_item_id.get(entry.item_id) or ["Manual"],
                "quality_label": quality_labels_by_item_id.get(entry.item_id, ""),
            },
        )

    return audit_entries


@never_cache
@require_GET
def collection_modal(request, source, media_type, media_id):
    """Return modal HTML for adding and managing collection entries."""

    def _parse_optional_int(value):
        if value in (None, "", "null"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    season_number = _parse_optional_int(request.GET.get("season_number"))
    episode_number = _parse_optional_int(request.GET.get("episode_number"))
    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
    )

    lookup = {
        "media_id": media_id,
        "source": source,
        "media_type": tracking_media_type,
    }
    if metadata_resolution.is_grouped_anime_route(media_type, source=source):
        lookup["library_media_type"] = MediaTypes.ANIME.value

    if media_type == MediaTypes.SEASON.value:
        if season_number is None:
            if request.headers.get("HX-Request"):
                return HttpResponseBadRequest("Season number is required")
            messages.error(request, "Season number is required")
            return redirect("home")
        lookup["season_number"] = season_number
    elif media_type == MediaTypes.EPISODE.value:
        if season_number is None or episode_number is None:
            if request.headers.get("HX-Request"):
                return HttpResponseBadRequest("Season and episode numbers are required")
            messages.error(request, "Season and episode numbers are required")
            return redirect("home")
        lookup["season_number"] = season_number
        lookup["episode_number"] = episode_number

    item = Item.objects.filter(**lookup).first()
    metadata = None
    needs_metadata = item is None or media_type == MediaTypes.GAME.value

    if needs_metadata:
        try:
            metadata = services.get_media_metadata(
                media_type,
                media_id,
                source,
                [season_number] if season_number is not None else None,
                episode_number=episode_number,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "Collection modal metadata lookup failed: %s",
                exception_summary(exc),
            )

    if not item:
        item_defaults = {
            **Item.title_fields_from_metadata(metadata or {}),
            "library_media_type": ((metadata or {}).get("library_media_type") or media_type),
            "image": settings.IMG_NONE,
        }
        try:
            if not item_defaults.get("title"):
                item_defaults["title"] = (
                    (metadata or {}).get("season_title")
                    or (metadata or {}).get("name")
                    or ""
                )
            item_defaults["image"] = (metadata or {}).get("image") or settings.IMG_NONE

            if media_type == MediaTypes.BOOK.value:
                item_defaults["number_of_pages"] = (
                    (metadata or {}).get("max_progress")
                    or (metadata or {}).get("details", {}).get("number_of_pages")
                )

            if (metadata or {}).get("details", {}).get("runtime"):
                from app.statistics import parse_runtime_to_minutes

                runtime_minutes = parse_runtime_to_minutes((metadata or {})["details"]["runtime"])
                if runtime_minutes:
                    item_defaults["runtime_minutes"] = runtime_minutes
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "Collection modal metadata lookup failed while building defaults: %s",
                exception_summary(exc),
            )

        item, _ = Item.objects.get_or_create(
            **lookup,
            defaults=item_defaults,
        )

    platform_choices = None
    if media_type == MediaTypes.GAME.value:
        platforms = (metadata or {}).get("details", {}).get("platforms") or []
        if platforms:
            platform_choices = platforms

    existing_entries = helpers.get_item_collection_entries(request.user, item)
    existing_entry = existing_entries.first()
    season_audit_entries = _build_collection_season_audit_entries(request.user, item)
    episode_audit_entries = _build_collection_episode_audit_entries(request.user, item)
    visible_existing_entries = list(existing_entries)
    if (
        (season_audit_entries or episode_audit_entries)
        and _item_has_collection_source_state(request.user, item, source="sonarr")
    ):
        visible_existing_entries = []
    form = CollectionEntryForm(
        user=request.user,
        collection_media_type=item.media_type,
        collection_choices_override={"resolution": platform_choices} if platform_choices else None,
    )
    form.fields["item"].initial = item.id

    return_url = request.GET.get("return_url", "")
    collection_fields = getattr(form, "collection_fields", [])

    response = render(
        request,
        "app/components/collection_modal.html",
        {
            "item": item,
            "entry": existing_entry,
            "existing_entries": existing_entries,
            "visible_existing_entries": visible_existing_entries,
            "season_audit_entries": season_audit_entries,
            "episode_audit_entries": episode_audit_entries,
            "form": form,
            "return_url": return_url,
            "collection_fields": collection_fields,
        },
    )
    response["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    response["Vary"] = "Cookie, HX-Request"
    return response


@login_required
@require_GET
@never_cache
def collection_status_api(request, item_id):
    """API endpoint to check if collection entry exists for an item."""
    from app.helpers import is_item_collected

    try:
        item = Item.objects.get(id=item_id)
        collection_entry = is_item_collected(request.user, item)

        return JsonResponse(
            {
                "has_collection_data": collection_entry is not None,
                "item_id": item_id,
            },
        )
    except Item.DoesNotExist:
        return JsonResponse({"error": "Item not found"}, status=404)
