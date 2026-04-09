"""Yamtrack base URL Configuration.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/stable/topics/http/urls/

"""

from allauth.account import views as allauth_account_views
from allauth.socialaccount import views as allauth_social_account_views
from allauth.urls import build_provider_urlpatterns
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.decorators import login_not_required
from django.urls import include, path
from health_check.views import MainView

handler400 = "app.error_views.bad_request"
handler403 = "app.error_views.permission_denied"
handler404 = "app.error_views.page_not_found"
handler500 = "app.error_views.server_error"

urlpatterns = [
    path("", include("app.urls")),
    path("", include("integrations.urls")),
    path("", include("users.urls")),
    path("", include("lists.urls")),
    path("", include("events.urls")),
    path("select2/", include("django_select2.urls")),
    path(
        "health/",
        login_not_required(MainView.as_view()),
    ),
]

# Build the accounts URLs
account_patterns = [
    # see allauth/account/urls.py
    # login, logout, signup, account_inactive
    path("login/", allauth_account_views.login, name="account_login"),
    path("logout/", allauth_account_views.logout, name="account_logout"),
    path("signup/", allauth_account_views.signup, name="account_signup"),
    path(
        "account_inactive/",
        allauth_account_views.account_inactive,
        name="account_inactive",
    ),
    # social account base urls, see allauth/socialaccount/urls.py
    path(
        "3rdparty/",
        include(
            [
                path(
                    "login/cancelled/",
                    allauth_social_account_views.login_cancelled,
                    name="socialaccount_login_cancelled",
                ),
                path(
                    "login/error/",
                    allauth_social_account_views.login_error,
                    name="socialaccount_login_error",
                ),
                path(
                    "signup/",
                    allauth_social_account_views.signup,
                    name="socialaccount_signup",
                ),
                path(
                    "",
                    allauth_social_account_views.connections,
                    name="socialaccount_connections",
                ),
            ],
        ),
    ),
    *build_provider_urlpatterns(),
]

# Add the accounts URLs to the main urlpatterns
urlpatterns.append(path("accounts/", include(account_patterns)))

if settings.ADMIN_ENABLED:
    urlpatterns.append(path("admin/", admin.site.urls))

# Add debug toolbar when explicitly enabled for local development
if settings.ENABLE_DEBUG_TOOLBAR:
    urlpatterns.append(path("__debug__/", include("debug_toolbar.urls")))

# Serve static files in development
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=str(settings.STATICFILES_DIRS[0]))
