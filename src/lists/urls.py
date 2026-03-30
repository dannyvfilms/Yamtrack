from django.urls import path

from lists import feeds, views

urlpatterns = [
    path("user/<str:username>", views.user_profile, name="user_profile"),
    path("lists", views.lists, name="lists"),
    path(
        "lists_modal/<source:source>/<media_type:media_type>/<path:media_id>/<int:season_number>/<int:episode_number>",
        views.lists_modal,
        name="lists_modal",
    ),
    path(
        "lists_modal/<source:source>/<media_type:media_type>/<path:media_id>/<int:season_number>",
        views.lists_modal,
        name="lists_modal",
    ),
    path(
        "lists_modal/<source:source>/<media_type:media_type>/<path:media_id>",
        views.lists_modal,
        name="lists_modal",
    ),
    path("list/<int:list_id>", views.list_detail, name="list_detail"),
    path(
        "list/<int:list_id>/columns",
        views.update_list_table_columns,
        name="list_detail_columns",
    ),
    path(
        "list/<int:list_id>/smart-rules",
        views.smart_rules_update,
        name="list_smart_rules_update",
    ),
    path("list/<int:list_id>/rss", feeds.list_rss_feed, name="list_rss"),
    path("list/<int:list_id>/json", feeds.list_json, name="list_json"),
    path("list/create", views.create, name="list_create"),
    path("list/edit", views.edit, name="list_edit"),
    path("list/delete", views.delete, name="list_delete"),
    path(
        "list/<int:list_id>/reorder",
        views.reorder_list_item,
        name="list_reorder_item",
    ),
    path(
        "list/<int:list_id>/reorder-all",
        views.reorder_list_items_all,
        name="list_reorder_all",
    ),
    path(
        "list/<int:list_id>/add",
        views.add_list_item_page,
        name="list_add_item",
    ),
    path(
        "list/<int:list_id>/add/search",
        views.add_list_item_search,
        name="list_add_item_search",
    ),
    path(
        "list/<int:list_id>/add/submit",
        views.add_list_item_submit,
        name="list_add_item_submit",
    ),
    path(
        "lists/import/trakt/credentials",
        views.trakt_lists_credentials,
        name="trakt_lists_credentials",
    ),
    path("lists/import/trakt", views.trakt_lists_oauth, name="trakt_lists_oauth"),
    path(
        "lists/import/trakt/callback",
        views.trakt_lists_callback,
        name="trakt_lists_callback",
    ),
    path("list_item_toggle", views.list_item_toggle, name="list_item_toggle"),
    # Recommendation URLs
    path(
        "list/<int:list_id>/recommend",
        views.recommend_item_page,
        name="recommend_item",
    ),
    path(
        "list/<int:list_id>/recommend/search",
        views.recommend_search,
        name="recommend_search",
    ),
    path(
        "list/<int:list_id>/recommend/submit",
        views.submit_recommendation,
        name="submit_recommendation",
    ),
    path(
        "list/<int:list_id>/recommendations",
        views.list_recommendations,
        name="list_recommendations",
    ),
    path(
        "list/<int:list_id>/activity",
        views.list_activity,
        name="list_activity",
    ),
    path(
        "list/<int:list_id>/recommendations/<int:recommendation_id>/approve",
        views.approve_recommendation,
        name="approve_recommendation",
    ),
    path(
        "list/<int:list_id>/recommendations/<int:recommendation_id>/deny",
        views.deny_recommendation,
        name="deny_recommendation",
    ),
    path(
        "api/fetch_release_year",
        views.fetch_release_year,
        name="fetch_release_year",
    ),
]
