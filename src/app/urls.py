from django.urls import path, register_converter

from app import converters, views

register_converter(converters.MediaTypeChecker, "media_type")
register_converter(converters.SourceChecker, "source")


urlpatterns = [
    path("", views.home, name="home"),
    path("discover", views.discover_page, name="discover"),
    path("discover/rows", views.discover_rows, name="discover_rows"),
    path("discover/refresh", views.refresh_discover, name="refresh_discover"),
    path("discover/action", views.discover_action, name="discover_action"),
    path("medialist/<media_type:media_type>", views.media_list, name="medialist"),
    path(
        "medialist/<media_type:media_type>/columns/",
        views.update_table_columns,
        name="medialist_columns",
    ),
    path("search", views.media_search, name="search"),
    path(
        "details/music/artist/<int:artist_id>/<slug:artist_slug>/",
        views.music_artist_details,
        name="music_artist_details",
    ),
    path(
        "details/music/artist/<int:artist_id>/<slug:artist_slug>/album/<int:album_id>/<slug:album_slug>/",
        views.music_album_details,
        name="music_album_details",
    ),
    path(
        "details/<source:source>/tv/<str:media_id>/<str:title>/season/<int:season_number>",
        views.season_details,
        name="season_details",
    ),
    path(
        "details/<source:source>/<media_type:media_type>/<path:media_id>/<str:title>",
        views.media_details,
        name="media_details",
    ),
    path(
        "update-score/<media_type:media_type>/<int:instance_id>",
        views.update_media_score,
        name="update_media_score",
    ),
    path(
        "update-episode-score/<int:season_id>/<int:episode_number>",
        views.update_episode_score,
        name="update_episode_score",
    ),
    path(
        "details/sync/<source:source>/<media_type:media_type>/<path:media_id>/<int:season_number>",
        views.sync_metadata,
        name="sync_metadata",
    ),
    path(
        "details/sync/<source:source>/<media_type:media_type>/<path:media_id>",
        views.sync_metadata,
        name="sync_metadata",
    ),
    path(
        "details/provider/<source:source>/<media_type:media_type>/<path:media_id>",
        views.update_metadata_provider_preference,
        name="update_metadata_provider_preference",
    ),
    path(
        "details/image/<int:item_id>",
        views.update_item_image,
        name="update_item_image",
    ),
    path(
        "details/migrate/<source:source>/<media_type:media_type>/<path:media_id>",
        views.migrate_grouped_anime,
        name="migrate_grouped_anime",
    ),
    path(
        "track_modal/<source:source>/<media_type:media_type>/<path:media_id>/<int:season_number>",
        views.track_modal,
        name="track_modal",
    ),
    path(
        "track_modal/<source:source>/<media_type:media_type>/<path:media_id>",
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
    path("episode_bulk_save", views.episode_bulk_save, name="episode_bulk_save"),
    path(
        "history_modal/<source:source>/<media_type:media_type>/<path:media_id>/<int:season_number>/<int:episode_number>",
        views.history_modal,
        name="history_modal",
    ),
    path(
        "history_modal/<source:source>/<media_type:media_type>/<path:media_id>/<int:season_number>",
        views.history_modal,
        name="history_modal",
    ),
    path(
        "history_modal/<source:source>/<media_type:media_type>/<path:media_id>",
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
    path("statistics/refresh", views.refresh_statistics, name="refresh_statistics"),
    path(
        "statistics/top-talent-sort",
        views.update_top_talent_sort,
        name="update_top_talent_sort",
    ),
    path("history", views.history, name="history"),
    path(
        "person/<source:source>/<str:person_id>/<slug:name>",
        views.person_detail,
        name="person_detail",
    ),
    path(
        "api/active-playback/",
        views.active_playback_fragment,
        name="active_playback_fragment",
    ),
    path("api/cache-status/", views.cache_status, name="cache_status"),
    path("serviceworker.js", views.service_worker, name="service_worker"),
    # Music hierarchy navigation
    path("music/artist/<int:artist_id>/", views.artist_detail, name="artist_detail"),
    path(
        "music/artist/<int:artist_id>/covers/",
        views.prefetch_artist_covers,
        name="prefetch_artist_covers",
    ),
    path(
        "music/artist/<int:artist_id>/update-score/",
        views.update_artist_score,
        name="update_artist_score",
    ),
    path("music/album/<int:album_id>/", views.album_detail, name="album_detail"),
    path(
        "music/album/<int:album_id>/update-score/",
        views.update_album_score,
        name="update_album_score",
    ),
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
        "music/album/<int:album_id>/track_modal/",
        views.album_track_modal,
        name="album_track_modal",
    ),
    path(
        "music/album/save/",
        views.album_save,
        name="album_save",
    ),
    path(
        "music/album/delete/",
        views.album_delete,
        name="album_delete",
    ),
    path(
        "music/song/save/",
        views.song_save,
        name="song_save",
    ),
    path(
        "podcast/episode/save/",
        views.podcast_save,
        name="podcast_save",
    ),
    path(
        "music/album/<int:album_id>/delete_plays/",
        views.delete_all_album_plays_view,
        name="delete_all_album_plays",
    ),
    path(
        "music/artist/<int:artist_id>/delete_plays/",
        views.delete_all_artist_plays_view,
        name="delete_all_artist_plays",
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
    # Podcast show hierarchy navigation
    path("podcast/show/<int:show_id>/", views.podcast_show_detail, name="podcast_show_detail"),
    path(
        "podcast/show/<int:show_id>/track_modal/",
        views.podcast_show_track_modal,
        name="podcast_show_track_modal",
    ),
    path(
        "podcast/show/<int:show_id>/episodes/",
        views.podcast_episodes_api,
        name="podcast_episodes_api",
    ),
    path(
        "podcast/show/<int:show_id>/mark-all-played/",
        views.podcast_mark_all_played,
        name="podcast_mark_all_played",
    ),
    path(
        "podcast/show/save/",
        views.podcast_show_save,
        name="podcast_show_save",
    ),
    path(
        "podcast/show/delete/",
        views.podcast_show_delete,
        name="podcast_show_delete",
    ),
    # Collection endpoints
    path("collection/", views.collection_list, name="collection_list"),
    path(
        "collection/<media_type:media_type>/",
        views.collection_list,
        name="collection_list_filtered",
    ),
    path("collection/add/", views.collection_add, name="collection_add"),
    path(
        "collection/<int:entry_id>/update/",
        views.collection_update,
        name="collection_update",
    ),
    path(
        "collection/<int:entry_id>/remove/",
        views.collection_remove,
        name="collection_remove",
    ),
    path(
        "collection/modal/<source:source>/<media_type:media_type>/<path:media_id>/",
        views.collection_modal,
        name="collection_modal",
    ),
    path(
        "api/collection-status/<int:item_id>/",
        views.collection_status_api,
        name="collection_status_api",
    ),
    # Tag endpoints
    path(
        "tags_modal/<source:source>/<media_type:media_type>/<path:media_id>/<int:season_number>/<int:episode_number>",
        views.tags_modal,
        name="tags_modal",
    ),
    path(
        "tags_modal/<source:source>/<media_type:media_type>/<path:media_id>/<int:season_number>",
        views.tags_modal,
        name="tags_modal",
    ),
    path(
        "tags_modal/<source:source>/<media_type:media_type>/<path:media_id>",
        views.tags_modal,
        name="tags_modal",
    ),
    path("tag_item_toggle", views.tag_item_toggle, name="tag_item_toggle"),
    path("tag_create", views.tag_create, name="tag_create"),
]
