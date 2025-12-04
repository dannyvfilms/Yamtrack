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
    list_display = ["title", "artist", "release_date", "tracks_populated"]
    list_filter = ["release_date", "tracks_populated"]


class TrackAdmin(admin.ModelAdmin):
    """Custom admin for Track model."""

    search_fields = ["title", "musicbrainz_recording_id", "album__title"]
    list_display = ["title", "album", "track_number", "disc_number", "duration_formatted"]
    list_filter = ["album"]


# Auto-register remaining models
app_models = apps.get_app_config("app").get_models()
# Models that don't use MediaAdmin (either registered separately or excluded)
SpecialModels = ["Item", "Episode", "BasicMedia", "Artist", "Album", "Track", "ArtistTracker"]
for model in app_models:
    if (
        not model.__name__.startswith("Historical")
        and model.__name__ not in SpecialModels
    ):
        with contextlib.suppress(AlreadyRegistered):
            admin.site.register(model, MediaAdmin)


# Register Artist, Album, Track, and ArtistTracker with custom admin classes
from app.models import Artist, Album, Track, ArtistTracker  # noqa: E402


class ArtistTrackerAdmin(admin.ModelAdmin):
    """Admin for ArtistTracker."""

    list_display = ["user", "artist", "status", "score", "created_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["user__username", "artist__name"]
    raw_id_fields = ["user", "artist"]


admin.site.register(Artist, ArtistAdmin)
admin.site.register(Album, AlbumAdmin)
admin.site.register(Track, TrackAdmin)
admin.site.register(ArtistTracker, ArtistTrackerAdmin)
