from django.contrib import admin

from lists.models import (
    CustomList,
    CustomListItem,
    ListActivity,
    ListRecommendation,
)


class CustomListAdmin(admin.ModelAdmin):
    """Admin configuration for CustomList model."""

    search_fields = ["name", "description", "owner__username"]
    list_display = [
        "name",
        "owner",
        "visibility",
        "allow_recommendations",
        "item_count",
        "get_last_update",
    ]
    list_filter = ["owner", "visibility", "allow_recommendations"]
    raw_id_fields = ["owner"]
    autocomplete_fields = ["collaborators"]
    filter_horizontal = ["collaborators"]

    def item_count(self, obj):
        """Return the number of items in the list."""
        return obj.items.count()

    item_count.short_description = "Number of items"

    def get_last_update(self, obj):
        """Return the date of the last item added."""
        last_update = CustomListItem.objects.get_last_added_date(obj)
        return last_update or "-"

    get_last_update.short_description = "Last updated"


class CustomListItemAdmin(admin.ModelAdmin):
    """Admin configuration for CustomListItem model."""

    search_fields = [
        "item__title",
        "custom_list__name",
        "item__media_id",
        "added_by__username",
    ]
    list_display = ["item", "custom_list", "added_by", "date_added", "get_media_type"]
    list_filter = ["custom_list", "item__media_type", "custom_list__owner", "added_by"]
    raw_id_fields = ["item", "custom_list", "added_by"]
    autocomplete_fields = ["item", "custom_list"]
    readonly_fields = ["date_added"]

    def get_media_type(self, obj):
        """Return the media type of the item."""
        return obj.item.get_media_type_display()

    get_media_type.short_description = "Media Type"


class ListRecommendationAdmin(admin.ModelAdmin):
    """Admin configuration for ListRecommendation model."""

    search_fields = [
        "item__title",
        "custom_list__name",
        "recommended_by__username",
        "anonymous_name",
        "note",
    ]
    list_display = [
        "item",
        "custom_list",
        "get_recommender",
        "has_note",
        "date_recommended",
    ]
    list_filter = ["custom_list", "custom_list__owner"]
    raw_id_fields = ["item", "custom_list", "recommended_by"]
    readonly_fields = ["date_recommended"]

    def get_recommender(self, obj):
        """Return the display name of the recommender."""
        return obj.recommender_display_name

    get_recommender.short_description = "Recommended by"

    def has_note(self, obj):
        """Return whether the recommendation has a note."""
        return bool(obj.note)

    has_note.boolean = True
    has_note.short_description = "Has Note"


class ListActivityAdmin(admin.ModelAdmin):
    """Admin configuration for ListActivity model."""

    search_fields = [
        "custom_list__name",
        "user__username",
        "item__title",
        "details",
    ]
    list_display = ["custom_list", "user", "activity_type", "item", "timestamp"]
    list_filter = ["activity_type", "custom_list", "user"]
    raw_id_fields = ["custom_list", "user", "item"]
    readonly_fields = ["timestamp"]


admin.site.register(CustomList, CustomListAdmin)
admin.site.register(CustomListItem, CustomListItemAdmin)
admin.site.register(ListRecommendation, ListRecommendationAdmin)
admin.site.register(ListActivity, ListActivityAdmin)
