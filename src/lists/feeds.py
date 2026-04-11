from django.apps import apps
from django.contrib.auth.decorators import login_not_required
from django.contrib.syndication.views import Feed
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.feedgenerator import Rss201rev2Feed
from django.views.decorators.http import require_GET

from app.models import MediaManager, MediaTypes, Sources
from app.providers import tmdb
from app.templatetags.app_tags import media_url
from lists.models import CustomList, CustomListItem


class YamtrackRssFeed(Rss201rev2Feed):
    """RSS feed generator with Yamtrack-specific item metadata."""

    yamtrack_namespace = "https://yamtrack.dannyvfilms.com/ns/rss"

    def rss_attributes(self):
        """Expose the Yamtrack namespace on the RSS root element."""
        attrs = super().rss_attributes()
        attrs["xmlns:yamtrack"] = self.yamtrack_namespace
        return attrs

    def add_item_elements(self, handler, item):
        """Add standard RSS elements plus Yamtrack extensions."""
        super().add_item_elements(handler, item)
        handler.addQuickElement("yamtrack:status", item.get("status", ""))
        handler.addQuickElement("yamtrack:image_url", item.get("image_url", ""))
        handler.addQuickElement("yamtrack:description", item.get("feed_description", ""))


class PublicListFeed(Feed):
    """RSS feed for public custom lists."""

    feed_type = YamtrackRssFeed

    def _attach_owner_media_statuses(self, list_items, owner):
        """Attach owner tracking status and metadata to feed items."""
        media_manager = MediaManager()
        item_ids_by_media_type = {}

        for list_item in list_items:
            item_ids_by_media_type.setdefault(list_item.item.media_type, []).append(list_item.item_id)

        status_by_item_id = {}
        for media_type, item_ids in item_ids_by_media_type.items():
            try:
                model = apps.get_model("app", media_type)
            except LookupError:
                continue

            if media_type == MediaTypes.EPISODE.value:
                queryset = model.objects.filter(
                    item_id__in=item_ids,
                    related_season__user=owner,
                ).select_related("item", "related_season")
            else:
                queryset = model.objects.filter(
                    item_id__in=item_ids,
                    user=owner,
                ).select_related("item")

            entries_by_item = {}
            for entry in queryset:
                entries_by_item.setdefault(entry.item_id, []).append(entry)

            for item_id, entries in entries_by_item.items():
                entries.sort(key=lambda entry: entry.created_at, reverse=True)
                display_media = entries[0]
                if len(entries) > 1:
                    media_manager._aggregate_item_data(display_media, entries)

                status_by_item_id[item_id] = (
                    getattr(display_media, "aggregated_status", None)
                    or getattr(display_media, "status", None)
                    or ""
                )

        for list_item in list_items:
            list_item.feed_status = status_by_item_id.get(list_item.item_id, "")
            list_item.feed_description = self._build_item_description(list_item.item)

    def _build_item_description(self, item):
        """Return a local feed description without provider lookups."""
        manual_metadata = getattr(item, "manual_metadata", None) or {}
        manual_synopsis = str(manual_metadata.get("synopsis") or "").strip()
        if manual_synopsis:
            return manual_synopsis
        return self._fallback_item_description(item)

    def _fallback_item_description(self, item):
        """Return the fallback item description."""
        media_type = item.get_media_type_display()
        source = item.source.upper()
        return f"{media_type} from {source}"

    def get_object(self, request, list_reference):
        """Return the public list or raise 404."""
        custom_list = CustomList.objects.get_public_list(list_reference)
        if custom_list is None:
            msg = "List not found"
            raise Http404(msg)
        self.request = request
        return custom_list

    def title(self, obj):
        """Return the feed title."""
        return f"{obj.name} - Yamtrack"

    def link(self, obj):
        """Return the list detail URL."""
        return self.request.build_absolute_uri(reverse("list_detail", args=[obj.public_reference]))

    def description(self, obj):
        """Return the feed description."""
        return obj.description or f"Public list from {obj.owner.username}"

    def items(self, obj):
        """Return list items."""
        list_items = list(
            CustomListItem.objects.filter(custom_list=obj)
            .select_related("item")
            .order_by("-date_added")
        )
        self._attach_owner_media_statuses(list_items, obj.owner)
        return list_items

    def item_title(self, item):
        """Return the item title."""
        return item.item.title

    def item_description(self, item):
        """Return the item description."""
        return getattr(item, "feed_description", self._build_item_description(item.item))

    def item_link(self, item):
        """Return the item URL."""
        return self.request.build_absolute_uri(media_url(item.item))

    def item_pubdate(self, item):
        """Return the item publication date."""
        return timezone.localtime(item.date_added)

    def item_extra_kwargs(self, item):
        """Expose extra item metadata for RSS consumers."""
        return {
            "status": getattr(item, "feed_status", ""),
            "image_url": item.item.image or "",
            "feed_description": getattr(item, "feed_description", ""),
        }


@login_not_required
@require_GET
def list_rss_feed(request, list_reference):
    """Wrapper view for RSS feed to ensure login_not_required is applied."""
    feed = PublicListFeed()
    return feed(request, list_reference)


@login_not_required
@require_GET
def list_json(request, list_reference):
    """Return JSON export for public lists in Radarr or Sonarr format."""
    custom_list = CustomList.objects.get_public_list(list_reference)
    if custom_list is None:
        msg = "List not found"
        raise Http404(msg)

    arr_type = request.GET.get("arr", "").lower()
    if arr_type not in ("radarr", "sonarr"):
        return JsonResponse(
            {"error": "Invalid or missing 'arr' parameter. Use ?arr=radarr or ?arr=sonarr"},
            status=400,
        )

    if arr_type == "radarr":
        # Filter for TMDB movies
        items = CustomListItem.objects.filter(
            custom_list=custom_list,
            item__source=Sources.TMDB.value,
            item__media_type=MediaTypes.MOVIE.value,
        ).select_related("item")

        json_data = [{"id": int(item.item.media_id)} for item in items]
    else:  # sonarr
        # Filter for TMDB TV shows
        items = CustomListItem.objects.filter(
            custom_list=custom_list,
            item__source=Sources.TMDB.value,
            item__media_type=MediaTypes.TV.value,
        ).select_related("item")

        json_data = []
        for list_item in items:
            # Fetch TMDB metadata (cached) to get TVDB ID
            try:
                metadata = tmdb.tv(list_item.item.media_id)
                tvdb_id = metadata.get("tvdb_id")
                if tvdb_id:  # Skip if no TVDB mapping available
                    json_data.append({"tvdbId": int(tvdb_id)})
            except Exception:
                # Skip items where metadata fetch fails
                continue

    return JsonResponse(json_data, safe=False)
