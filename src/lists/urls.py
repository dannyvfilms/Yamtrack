from django.urls import path

from lists import feeds, views, views_add_reorder, views_recommendations, views_trakt

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
    path("list/create", views.create, name="list_create"),
    path("list/edit", views.edit, name="list_edit"),
    path("list/delete", views.delete, name="list_delete"),
    path("list/<slug:list_reference>", views.list_detail, name="list_detail"),
    path("list/<slug:list_reference>/rss", feeds.list_rss_feed, name="list_rss"),
    path("list/<slug:list_reference>/json", feeds.list_json, name="list_json"),
    path(
        "list/<int:list_id>/cover-image",
        views.list_cover_image,
        name="list_cover_image",
    ),
    path(
        "list/<int:list_id>/reorder",
        views_add_reorder.reorder_list_item,
        name="list_reorder_item",
    ),
    path(
        "list/<int:list_id>/reorder-all",
        views_add_reorder.reorder_list_items_all,
        name="list_reorder_all",
    ),
    path(
        "list/<int:list_id>/add",
        views_add_reorder.add_list_item_page,
        name="list_add_item",
    ),
    path(
        "list/<int:list_id>/add/search",
        views_add_reorder.add_list_item_search,
        name="list_add_item_search",
    ),
    path(
        "list/<int:list_id>/add/submit",
        views_add_reorder.add_list_item_submit,
        name="list_add_item_submit",
    ),
    path(
        "lists/import/trakt/credentials",
        views_trakt.trakt_lists_credentials,
        name="trakt_lists_credentials",
    ),
    path("lists/import/trakt", views_trakt.trakt_lists_oauth, name="trakt_lists_oauth"),
    path(
        "lists/import/trakt/callback",
        views_trakt.trakt_lists_callback,
        name="trakt_lists_callback",
    ),
    path("list_item_toggle", views.list_item_toggle, name="list_item_toggle"),
    # Recommendation URLs
    path(
        "list/<int:list_id>/recommend",
        views_recommendations.recommend_item_page,
        name="recommend_item",
    ),
    path(
        "list/<int:list_id>/recommend/search",
        views_recommendations.recommend_search,
        name="recommend_search",
    ),
    path(
        "list/<int:list_id>/recommend/submit",
        views_recommendations.submit_recommendation,
        name="submit_recommendation",
    ),
    path(
        "list/<int:list_id>/recommendations",
        views_recommendations.list_recommendations,
        name="list_recommendations",
    ),
    path(
        "list/<int:list_id>/activity",
        views_recommendations.list_activity,
        name="list_activity",
    ),
    path(
        "list/<int:list_id>/recommendations/<int:recommendation_id>/approve",
        views_recommendations.approve_recommendation,
        name="approve_recommendation",
    ),
    path(
        "list/<int:list_id>/recommendations/<int:recommendation_id>/deny",
        views_recommendations.deny_recommendation,
        name="deny_recommendation",
    ),
    path(
        "api/fetch_release_year",
        views.fetch_release_year,
        name="fetch_release_year",
    ),
]
