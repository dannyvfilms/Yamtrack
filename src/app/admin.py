import contextlib

from django.apps import apps
from django.contrib import admin
from django.contrib.admin.sites import AlreadyRegistered

from app.models import (
    Episode,
    Item,
)


# Custom ModelAdmin classes with search functionality
class ItemAdmin(admin.ModelAdmin):
    """Custom admin for Item model with search and filter options."""

    search_fields = ["title", "media_id", "source"]
    list_display = [
        "title",
        "media_id",
        "season_number",
        "episode_number",
        "media_type",
        "source",
    ]
    list_filter = ["media_type", "source"]


class EpisodeAdmin(admin.ModelAdmin):
    """Custom admin for Episode model with search and filter options."""

    search_fields = ["item__title", "related_season__item__title"]
    list_display = ["__str__", "end_date"]


class MediaAdmin(admin.ModelAdmin):
    """Custom admin for regular media model with search and filter options."""

    search_fields = ["item__title", "user__username", "notes"]
    list_display = ["__str__", "status", "score", "user"]
    list_filter = ["status"]


# Register models with custom admin classes
admin.site.register(Item, ItemAdmin)
admin.site.register(Episode, EpisodeAdmin)


class ArtistAdmin(admin.ModelAdmin):
    """Custom admin for Artist model."""

    search_fields = ["name", "musicbrainz_id"]
    list_display = ["name", "sort_name", "musicbrainz_id"]


class AlbumAdmin(admin.ModelAdmin):
    """Custom admin for Album model."""

    search_fields = ["title", "musicbrainz_release_id", "artist__name"]
    list_display = ["title", "artist", "release_date"]
    list_filter = ["release_date"]


# Auto-register remaining models
app_models = apps.get_app_config("app").get_models()
# Models that don't use MediaAdmin (either registered separately or excluded)
SpecialModels = ["Item", "Episode", "BasicMedia", "Artist", "Album"]
for model in app_models:
    if (
        not model.__name__.startswith("Historical")
        and model.__name__ not in SpecialModels
    ):
        with contextlib.suppress(AlreadyRegistered):
            admin.site.register(model, MediaAdmin)


# Register Artist and Album with custom admin classes
from app.models import Artist, Album  # noqa: E402

admin.site.register(Artist, ArtistAdmin)
admin.site.register(Album, AlbumAdmin)
