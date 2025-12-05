from django.urls import path, register_converter

from app import converters, views

register_converter(converters.MediaTypeChecker, "media_type")
register_converter(converters.SourceChecker, "source")


urlpatterns = [
    path("", views.home, name="home"),
    path("medialist/<media_type:media_type>", views.media_list, name="medialist"),
    path("search", views.media_search, name="search"),
    path(
        "details/<source:source>/<media_type:media_type>/<str:media_id>/<str:title>",
        views.media_details,
        name="media_details",
    ),
    path(
        "details/<source:source>/tv/<str:media_id>/<str:title>/season/<int:season_number>",
        views.season_details,
        name="season_details",
    ),
    path(
        "update-score/<media_type:media_type>/<int:instance_id>",
        views.update_media_score,
        name="update_media_score",
    ),
    path(
        "details/sync/<source:source>/<media_type:media_type>/<str:media_id>",
        views.sync_metadata,
        name="sync_metadata",
    ),
    path(
        "details/sync/<source:source>/<media_type:media_type>/<str:media_id>/<int:season_number>",
        views.sync_metadata,
        name="sync_metadata",
    ),
    path(
        "track_modal/<source:source>/<media_type:media_type>/<str:media_id>",
        views.track_modal,
        name="track_modal",
    ),
    path(
        "track_modal/<source:source>/<media_type:media_type>/<str:media_id>/<int:season_number>",
        views.track_modal,
        name="track_modal",
    ),
    path(
        "progress_edit/<media_type:media_type>/<int:instance_id>",
        views.progress_edit,
        name="progress_edit",
    ),
    path("media_save", views.media_save, name="media_save"),
    path("media_delete", views.media_delete, name="media_delete"),
    path("episode_save", views.episode_save, name="episode_save"),
    path(
        "history_modal/<source:source>/<media_type:media_type>/<str:media_id>",
        views.history_modal,
        name="history_modal",
    ),
    path(
        "history_modal/<source:source>/<media_type:media_type>/<str:media_id>/<int:season_number>",
        views.history_modal,
        name="history_modal",
    ),
    path(
        "history_modal/<source:source>/<media_type:media_type>/<str:media_id>/<int:season_number>/<int:episode_number>",
        views.history_modal,
        name="history_modal",
    ),
    path(
        "media/history/<str:media_type>/<int:history_id>/delete/",
        views.delete_history_record,
        name="delete_history_record",
    ),
    path("create", views.create_entry, name="create_entry"),
    path("search/parent_tv", views.search_parent_tv, name="search_parent_tv"),
    path(
        "search/parent_season",
        views.search_parent_season,
        name="search_parent_season",
    ),
    path("statistics", views.statistics, name="statistics"),
    path("history", views.history, name="history"),
    path("serviceworker.js", views.service_worker, name="service_worker"),
    # Music hierarchy navigation
    path("music/artist/<int:artist_id>/", views.artist_detail, name="artist_detail"),
    path("music/album/<int:album_id>/", views.album_detail, name="album_detail"),
    path(
        "music/artist/<int:artist_id>/sync/",
        views.sync_artist_discography_view,
        name="sync_artist_discography",
    ),
    path(
        "music/artist/<int:artist_id>/track_modal/",
        views.artist_track_modal,
        name="artist_track_modal",
    ),
    path(
        "music/artist/save/",
        views.artist_save,
        name="artist_save",
    ),
    path(
        "music/artist/delete/",
        views.artist_delete,
        name="artist_delete",
    ),
    path(
        "music/album/<int:album_id>/sync/",
        views.sync_album_metadata_view,
        name="sync_album_metadata",
    ),
    # Music search to create artist/album
    path(
        "music/artist/create/<str:musicbrainz_artist_id>/",
        views.create_artist_from_search,
        name="create_artist_from_search",
    ),
    path(
        "music/album/create/<str:musicbrainz_release_id>/",
        views.create_album_from_search,
        name="create_album_from_search",
    ),
]
