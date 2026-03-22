# ruff: noqa: D102, S106

from importlib import import_module
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from app.apps import AppConfig as YamtrackAppConfig
from app.middleware import DiscoverWarmupMiddleware
from app.tasks import warm_discover_startup_tabs


class DiscoverWarmupTests(TestCase):
    """Tests for Discover startup and request warmup scheduling."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="discover-warmup-user",
            password="secret123",
        )
        self.factory = RequestFactory()
        self.middleware = DiscoverWarmupMiddleware(
            lambda _request: HttpResponse("ok"),
        )

    @patch("app.middleware.discover_tab_cache.maybe_schedule_user_warmup")
    def test_middleware_schedules_warmup_for_authenticated_html_get(
        self, mock_schedule_warmup
    ):
        request = self.factory.get("/discover", HTTP_ACCEPT="text/html")
        request.user = self.user

        response = self.middleware(request)

        self.assertEqual(response.status_code, 200)
        mock_schedule_warmup.assert_called_once_with(self.user)

    @patch("app.middleware.discover_tab_cache.maybe_schedule_user_warmup")
    def test_middleware_skips_non_discover_html_requests(self, mock_schedule_warmup):
        request = self.factory.get("/library/", HTTP_ACCEPT="text/html")
        request.user = self.user

        self.middleware(request)

        mock_schedule_warmup.assert_not_called()

    @patch("app.middleware.discover_tab_cache.maybe_schedule_user_warmup")
    def test_middleware_skips_api_and_htmx_requests(self, mock_schedule_warmup):
        api_request = self.factory.get(
            "/api/cache-status/",
            HTTP_ACCEPT="application/json",
        )
        api_request.user = self.user
        self.middleware(api_request)

        htmx_request = self.factory.get(
            "/discover/rows/",
            HTTP_ACCEPT="text/html",
            HTTP_HX_REQUEST="true",
        )
        htmx_request.user = self.user
        self.middleware(htmx_request)

        mock_schedule_warmup.assert_not_called()

    @patch("app.discover.tab_cache.schedule_user_tab_warmup", return_value=1)
    def test_startup_task_warms_default_all_tab(self, mock_schedule_warmup):
        inactive_user = get_user_model().objects.create_user(
            username="inactive-discover-user",
            password="secret123",
            is_active=False,
        )

        result = warm_discover_startup_tabs()

        self.assertEqual(result["scheduled"], 1)
        self.assertEqual(result["users_count"], 1)
        mock_schedule_warmup.assert_called_once_with(
            self.user,
            media_types=["all"],
            prioritize_media_type="all",
            show_more=False,
        )
        self.assertNotEqual(inactive_user.id, self.user.id)

    @override_settings(TESTING=False, DISCOVER_WARMUP_ON_STARTUP=False)
    @patch("app.apps._is_celery_worker_process", return_value=False)
    def test_app_ready_skips_startup_discover_warmup_when_disabled(
        self,
        _mock_is_celery_worker,
    ):
        config = YamtrackAppConfig("app", import_module("app"))

        with (
            patch.object(config, "_add_startup_cache_key", return_value=True),
            patch.object(config, "_schedule_runtime_population"),
            patch.object(config, "_schedule_discover_startup_warmup") as mock_schedule,
        ):
            config.ready()

        mock_schedule.assert_not_called()

    @override_settings(TESTING=False, DISCOVER_WARMUP_ON_STARTUP=True)
    @patch("app.apps._is_celery_worker_process", return_value=False)
    def test_app_ready_schedules_startup_discover_warmup_when_enabled(
        self,
        _mock_is_celery_worker,
    ):
        config = YamtrackAppConfig("app", import_module("app"))

        with (
            patch.object(config, "_add_startup_cache_key", return_value=True),
            patch.object(config, "_schedule_runtime_population"),
            patch.object(config, "_schedule_discover_startup_warmup") as mock_schedule,
        ):
            config.ready()

        mock_schedule.assert_called_once_with()
