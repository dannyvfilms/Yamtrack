from django.urls import path

from integrations import views

urlpatterns = [
    path("import/trakt-oauth", views.trakt_oauth, name="trakt_oauth"),
    path(
        "import/trakt/private",
        views.import_trakt_private,
        name="import_trakt_private",
    ),
    path("import/trakt/public", views.import_trakt_public, name="import_trakt_public"),
    path("import/plex/connect", views.plex_connect, name="plex_connect"),
    path("import/plex/callback", views.plex_callback, name="plex_callback"),
    path("import/plex/disconnect", views.plex_disconnect, name="plex_disconnect"),
    path(
        "import/plex/watchlist/disable",
        views.plex_disable_watchlist,
        name="plex_disable_watchlist",
    ),
    path("import/plex", views.import_plex, name="import_plex"),
    path("import/simkl-oauth", views.simkl_oauth, name="simkl_oauth"),
    path(
        "import/simkl_private",
        views.import_simkl_private,
        name="import_simkl_private",
    ),
    path("import/mal", views.import_mal, name="import_mal"),
    path("import/anilist/oauth", views.anilist_oauth, name="import_anilist_oauth"),
    path(
        "import/anilist/private",
        views.import_anilist_private,
        name="import_anilist_private",
    ),
    path(
        "import/anilist/public",
        views.import_anilist_public,
        name="import_anilist_public",
    ),
    path("import/kitsu", views.import_kitsu, name="import_kitsu"),
    path("import/yamtrack", views.import_yamtrack, name="import_yamtrack"),
    path("import/hltb", views.import_hltb, name="import_hltb"),
    path("import/steam", views.import_steam, name="import_steam"),
    path("import/imdb", views.import_imdb, name="import_imdb"),
    path("import/goodreads", views.import_goodreads, name="import_goodreads"),
    path("import/hardcover", views.import_hardcover, name="import_hardcover"),
    path("import/audiobookshelf/connect", views.audiobookshelf_connect, name="audiobookshelf_connect"),
    path("import/audiobookshelf/disconnect", views.audiobookshelf_disconnect, name="audiobookshelf_disconnect"),
    path("import/audiobookshelf", views.import_audiobookshelf, name="import_audiobookshelf"),
    path("import/pocketcasts/connect", views.pocketcasts_connect, name="pocketcasts_connect"),
    path("import/pocketcasts/disconnect", views.pocketcasts_disconnect, name="pocketcasts_disconnect"),
    path("import/pocketcasts", views.import_pocketcasts, name="import_pocketcasts"),
    path("import/lastfm/connect", views.lastfm_connect, name="lastfm_connect"),
    path("import/lastfm/disconnect", views.lastfm_disconnect, name="lastfm_disconnect"),
    path("import/lastfm/history", views.import_lastfm_history_manual, name="import_lastfm_history"),
    path("import/lastfm/poll", views.poll_lastfm_manual, name="poll_lastfm_manual"),
    path("export/csv", views.export_csv, name="export_csv"),
    path(
        "webhook/jellyfin/<str:token>",
        views.jellyfin_webhook,
        name="jellyfin_webhook",
    ),
    path(
        "webhook/plex/<str:token>",
        views.plex_webhook,
        name="plex_webhook",
    ),
    path(
        "webhook/emby/<str:token>",
        views.emby_webhook,
        name="emby_webhook",
    ),
    path(
        "webhook/jellyseerr/<str:token>",
        views.jellyseerr_webhook,
        name="jellyseerr_webhook",
    ),

]
