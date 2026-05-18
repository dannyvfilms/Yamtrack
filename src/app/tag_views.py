import json

from django.contrib import messages
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_GET, require_POST

from app import statistics as stats
from app.models import Item, ItemTag, MediaTypes, Tag
from app.providers import services
from app.services import metadata_resolution
from app.templatetags import app_tags


def _detail_request_url(request, *, fragment: str | None = None) -> str:
    """Return the current detail URL with an optional fragment query override."""
    query = request.GET.copy()
    query.pop("fragment", None)
    if fragment:
        query["fragment"] = fragment
    querystring = query.urlencode()
    if not querystring:
        return request.path
    return f"{request.path}?{querystring}"


def _resolve_detail_tag_genres(media_metadata, item, fallback_genres=None):
    """Return detail-page genres sourced from metadata, request state, or stored item data."""
    genres = []
    if isinstance(media_metadata, dict):
        details = media_metadata.get("details")
        genres = stats._coerce_genre_list(
            media_metadata.get("genres")
            or (details.get("genres") if isinstance(details, dict) else None)
            or media_metadata.get("genre")
            or (details.get("genre") if isinstance(details, dict) else None),
        )
    if not genres and fallback_genres:
        genres = stats._coerce_genre_list(fallback_genres)
    if not genres and item is not None:
        genres = list(item.genres or [])
    return genres


def _build_detail_tag_sections(media_metadata, item, user, fallback_genres=None):
    """Return grouped genre and tag preview sections for the media detail action row."""
    sections = []

    genres = _resolve_detail_tag_genres(
        media_metadata,
        item,
        fallback_genres=fallback_genres,
    )

    if genres:
        sections.append(
            {
                "title": "Genres",
                "entries": [
                    {
                        "label": genre,
                        "chip_classes": "border-violet-400/18 bg-violet-500/[0.07] text-violet-100",
                    }
                    for genre in genres
                ],
            }
        )

    tag_names = []
    is_authenticated_user = item is not None and getattr(user, "is_authenticated", False)
    if is_authenticated_user:
        tag_names = list(
            ItemTag.objects.filter(item=item, tag__user=user)
            .select_related("tag")
            .order_by("tag__name")
            .values_list("tag__name", flat=True)
        )

    if is_authenticated_user:
        tag_section = {
            "title": "Tags",
            "entries": [
                {
                    "label": tag_name,
                    "chip_classes": "border-slate-400/18 bg-slate-500/[0.07] text-slate-100",
                }
                for tag_name in tag_names
            ],
        }
        if not tag_names:
            tag_section["empty_label"] = "Click to add tags"

        sections.append(
            tag_section,
        )

    return sections


def _parse_detail_tag_preview_genres(raw_value):
    """Return a normalized genre list from a serialized detail-tag preview payload."""
    if not raw_value:
        return []
    try:
        parsed_value = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return []
    return stats._coerce_genre_list(parsed_value)


def _user_tags_for_item(user, item):
    """Return the user's tags annotated with whether they apply to the item."""
    from django.db import models as db_models

    return (
        Tag.objects.filter(user=user)
        .annotate(
            has_tag=db_models.Exists(
                ItemTag.objects.filter(
                    tag_id=db_models.OuterRef("id"),
                    item=item,
                ),
            ),
        )
        .order_by("name")
    )


def _render_tag_modal_response(request, item, preview_genres):
    """Render the tag modal plus OOB preview refresh for the current item."""
    modal_html = render_to_string(
        "app/components/fill_tags.html",
        {
            "item": item,
            "user_tags": _user_tags_for_item(request.user, item),
            "preview_genres_json": json.dumps(preview_genres),
        },
        request=request,
    )
    preview_html = render_to_string(
        "app/components/detail_tag_preview.html",
        {
            "preview_id": app_tags.component_id("tag-preview", item),
            "detail_tag_sections": _build_detail_tag_sections(
                {},
                item,
                request.user,
                fallback_genres=preview_genres,
            ),
            "swap_oob": True,
        },
        request=request,
    )
    return HttpResponse(modal_html + preview_html)


@require_GET
def tags_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    episode_number=None,
):
    """Return the modal showing all user tags and allowing to toggle them on an item."""
    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
    )
    lookup = {
        "media_id": media_id,
        "source": source,
        "media_type": tracking_media_type,
        "season_number": season_number,
        "episode_number": episode_number,
    }
    if metadata_resolution.is_grouped_anime_route(media_type, source=source):
        lookup["library_media_type"] = MediaTypes.ANIME.value

    try:
        item = Item.objects.get(**lookup)
    except Item.DoesNotExist:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
            episode_number,
        )
        item = Item.objects.create(
            media_id=media_id,
            source=source,
            media_type=tracking_media_type,
            season_number=season_number,
            episode_number=episode_number,
            library_media_type=metadata.get("library_media_type") or media_type,
            title=metadata["title"],
            image=metadata["image"],
        )

    preview_genres = _parse_detail_tag_preview_genres(
        request.GET.get("preview_genres_json"),
    )
    if not preview_genres:
        preview_genres = _resolve_detail_tag_genres({}, item)

    return render(
        request,
        "app/components/fill_tags.html",
        {
            "item": item,
            "user_tags": _user_tags_for_item(request.user, item),
            "preview_genres_json": json.dumps(preview_genres),
        },
    )


@require_POST
def tag_item_toggle(request):
    """Add or remove a tag from an item."""
    item_id = request.POST["item_id"]
    tag_id = request.POST["tag_id"]

    item = get_object_or_404(Item, id=item_id)
    tag = get_object_or_404(Tag, id=tag_id, user=request.user)

    existing = ItemTag.objects.filter(tag=tag, item=item)
    if existing.exists():
        existing.delete()
        has_tag = False
    else:
        ItemTag.objects.create(tag=tag, item=item)
        has_tag = True

    preview_genres = _parse_detail_tag_preview_genres(
        request.POST.get("preview_genres_json"),
    )
    preview_sections = _build_detail_tag_sections(
        {},
        item,
        request.user,
        fallback_genres=preview_genres,
    )
    button_html = render_to_string(
        "app/components/tag_item_button.html",
        {
            "tag": tag,
            "item": item,
            "has_tag": has_tag,
            "preview_genres_json": json.dumps(preview_genres),
        },
        request=request,
    )
    preview_html = render_to_string(
        "app/components/detail_tag_preview.html",
        {
            "preview_id": app_tags.component_id("tag-preview", item),
            "detail_tag_sections": preview_sections,
            "swap_oob": True,
        },
        request=request,
    )
    return HttpResponse(button_html + preview_html)


@require_POST
def tag_create(request):
    """Create a new tag for the user and optionally apply it to an item."""
    name = (request.POST.get("name") or "").strip()
    item_id = request.POST.get("item_id")

    if not name:
        return HttpResponseBadRequest("Tag name is required.")

    if Tag.objects.filter(user=request.user, name__iexact=name).exists():
        messages.error(request, f'Tag "{name}" already exists.')
    else:
        tag = Tag.objects.create(user=request.user, name=name)
        if item_id:
            try:
                item = Item.objects.get(id=item_id)
                ItemTag.objects.get_or_create(tag=tag, item=item)
            except Item.DoesNotExist:
                pass

    if item_id:
        try:
            item = Item.objects.get(id=item_id)
        except Item.DoesNotExist:
            return HttpResponseBadRequest("Item not found.")

        preview_genres = _parse_detail_tag_preview_genres(
            request.POST.get("preview_genres_json"),
        )
        return _render_tag_modal_response(request, item, preview_genres)

    return HttpResponse(status=204)


@require_POST
def tag_delete(request):
    """Delete a tag owned by the current user and refresh the tag modal."""
    tag_id = request.POST.get("tag_id")
    item_id = request.POST.get("item_id")

    if not tag_id:
        return HttpResponseBadRequest("Tag is required.")

    tag = get_object_or_404(Tag, id=tag_id, user=request.user)
    tag.delete()

    if item_id:
        try:
            item = Item.objects.get(id=item_id)
        except Item.DoesNotExist:
            return HttpResponseBadRequest("Item not found.")

        preview_genres = _parse_detail_tag_preview_genres(
            request.POST.get("preview_genres_json"),
        )
        return _render_tag_modal_response(request, item, preview_genres)

    return HttpResponse(status=204)
