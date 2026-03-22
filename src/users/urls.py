from django.urls import path

from users import views

urlpatterns = [
    path("accounts/password/recover/", views.password_recover, name="password_recover"),
    path("settings/account", views.account, name="account"),
    path("settings/notifications", views.notifications, name="notifications"),
    path("notifications/search/", views.search_items, name="search_notification_items"),
    path(
        "notifications/exclude/",
        views.exclude_item,
        name="exclude_notification_item",
    ),
    path(
        "notifications/include/",
        views.include_item,
        name="include_notification_item",
    ),
    path("test_notification", views.test_notification, name="test_notification"),
    path("settings/ui", views.ui_preferences, name="ui_preferences"),
    path("settings/sidebar", views.sidebar, name="sidebar"),
    path("settings/preferences", views.preferences, name="preferences"),
    path("settings/integrations", views.integrations, name="integrations"),
    path("settings/import", views.import_data, name="import_data"),
    path(
        "settings/import/plex-status",
        views.import_data_plex_status,
        name="import_data_plex_status",
    ),
    path(
        "settings/import/plex-sections",
        views.import_data_plex_sections,
        name="import_data_plex_sections",
    ),
    path("settings/export", views.export_data, name="export_data"),
    path("settings/advanced", views.advanced, name="advanced"),
    path("settings/about", views.about, name="about"),
    path(
        "delete_import_schedule",
        views.delete_import_schedule,
        name="delete_import_schedule",
    ),
    path(
        "create_export_schedule",
        views.create_export_schedule,
        name="create_export_schedule",
    ),
    path(
        "delete_export_schedule",
        views.delete_export_schedule,
        name="delete_export_schedule",
    ),
    path("regenerate_token", views.regenerate_token, name="regenerate_token"),
    path("clear_search_cache", views.clear_search_cache, name="clear_search_cache"),
    path(
        "update_plex_usernames",
        views.update_plex_usernames,
        name="update_plex_usernames",
    ),
    path(
        "update_plex_webhook_libraries",
        views.update_plex_webhook_libraries,
        name="update_plex_webhook_libraries",
    ),
    path(
        "settings/integrations/jellyseerr/",
        views.update_jellyseerr_settings,
        name="update_jellyseerr_settings",
    ),
]
