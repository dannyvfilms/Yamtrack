from django.contrib.syndication.views import Feed
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone

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
