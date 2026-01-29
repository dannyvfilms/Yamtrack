from django.contrib.auth.decorators import login_not_required
from django.contrib.syndication.views import Feed
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from app.models import MediaTypes, Sources
from app.providers import tmdb
from app.templatetags.app_tags import media_url
from lists.models import CustomList, CustomListItem


class PublicListFeed(Feed):
    """RSS feed for public custom lists."""

    def get_object(self, request, list_id):
        """Return the public list or raise 404."""
        custom_list = get_object_or_404(
            CustomList.objects.select_related("owner"),
            id=list_id,
        )
        if custom_list.visibility != "public":
            msg = "List not found"
            raise Http404(msg)
        self.request = request
        return custom_list

    def title(self, obj):
        """Return the feed title."""
        return f"{obj.name} - Yamtrack"

    def link(self, obj):
        """Return the list detail URL."""
        return self.request.build_absolute_uri(reverse("list_detail", args=[obj.id]))

    def description(self, obj):
        """Return the feed description."""
        return obj.description or f"Public list from {obj.owner.username}"

    def items(self, obj):
        """Return list items."""
        return (
            CustomListItem.objects.filter(custom_list=obj)
            .select_related("item")
            .order_by("-date_added")
        )

    def item_title(self, item):
        """Return the item title."""
        return item.item.title

    def item_description(self, item):
        """Return the item description."""
        media_type = item.item.get_media_type_display()
        source = item.item.source.upper()
        return f"{media_type} from {source}"

    def item_link(self, item):
        """Return the item URL."""
        return self.request.build_absolute_uri(media_url(item.item))

    def item_pubdate(self, item):
        """Return the item publication date."""
        return timezone.localtime(item.date_added)


@login_not_required
@require_GET
def list_rss_feed(request, list_id):
    """Wrapper view for RSS feed to ensure login_not_required is applied."""
    feed = PublicListFeed()
    return feed(request, list_id)


@login_not_required
@require_GET
def list_json(request, list_id):
    """Return JSON export for public lists in Radarr or Sonarr format."""
    custom_list = get_object_or_404(
        CustomList.objects.select_related("owner"),
        id=list_id,
    )
    if custom_list.visibility != "public":
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
