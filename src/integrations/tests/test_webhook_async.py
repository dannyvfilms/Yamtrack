"""Tests for asynchronous webhook processing.

Webhook views validate the request shape synchronously, then enqueue
integrations.tasks.process_webhook so external API lookups and DB writes
never block a web worker.
"""

import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from simple_history.models import HistoricalRecords

from integrations import tasks


class WebhookViewEnqueueTests(TestCase):
    """Webhook views enqueue the processing task instead of running inline."""

    def setUp(self):
        """Create a user and client."""
        self.client = Client()
        self.user = get_user_model().objects.create_user(
            username="webhookuser",
            password="12345",
            token="hook-token",
        )

    @patch("integrations.tasks.process_webhook.delay")
    def test_jellyfin_enqueues_task(self, mock_delay):
        """A valid Jellyfin payload is enqueued with the parsed payload."""
        url = reverse("jellyfin_webhook", kwargs={"token": "hook-token"})
        payload = {"Event": "Stop", "Item": {"Type": "Episode"}}
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        mock_delay.assert_called_once_with("jellyfin", payload, self.user.id)

    @patch("integrations.tasks.process_webhook.delay")
    def test_plex_enqueues_task(self, mock_delay):
        """A valid Plex payload is enqueued with the parsed payload."""
        url = reverse("plex_webhook", kwargs={"token": "hook-token"})
        payload = {"event": "media.scrobble"}
        response = self.client.post(url, data={"payload": json.dumps(payload)})
        self.assertEqual(response.status_code, 200)
        mock_delay.assert_called_once_with("plex", payload, self.user.id)

    @patch("integrations.tasks.process_webhook.delay")
    def test_emby_enqueues_task(self, mock_delay):
        """A valid Emby payload is enqueued with the parsed payload."""
        url = reverse("emby_webhook", kwargs={"token": "hook-token"})
        payload = {"Event": "playback.stop"}
        response = self.client.post(url, data={"data": json.dumps(payload)})
        self.assertEqual(response.status_code, 200)
        mock_delay.assert_called_once_with("emby", payload, self.user.id)

    @patch("integrations.tasks.process_webhook.delay")
    def test_jellyseerr_enqueues_task(self, mock_delay):
        """A valid Jellyseerr payload is enqueued with the parsed payload."""
        url = reverse("jellyseerr_webhook", kwargs={"token": "hook-token"})
        payload = {"notification_type": "MEDIA_APPROVED"}
        response = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        mock_delay.assert_called_once_with("jellyseerr", payload, self.user.id)

    @patch("integrations.tasks.process_webhook.delay")
    def test_invalid_token_does_not_enqueue(self, mock_delay):
        """An invalid token returns 401 without touching the queue."""
        url = reverse("plex_webhook", kwargs={"token": "wrong-token"})
        response = self.client.post(url, data={"payload": "{}"})
        self.assertEqual(response.status_code, 401)
        mock_delay.assert_not_called()

    @patch("integrations.tasks.process_webhook.delay")
    def test_missing_plex_payload_does_not_enqueue(self, mock_delay):
        """A missing Plex payload returns 400, marks the error, no enqueue."""
        url = reverse("plex_webhook", kwargs={"token": "hook-token"})
        response = self.client.post(url, data={})
        self.assertEqual(response.status_code, 400)
        mock_delay.assert_not_called()
        self.user.refresh_from_db()
        self.assertIn("Missing payload", self.user.plex_webhook_last_error or "")


class ProcessWebhookTaskTests(TestCase):
    """Behavior of the process_webhook task itself."""

    def setUp(self):
        """Create a user."""
        self.user = get_user_model().objects.create_user(
            username="taskuser",
            password="12345",
            token="task-token",
        )

    @patch("integrations.webhooks.plex.PlexWebhookProcessor.process_payload")
    def test_plex_success_marks_received(self, mock_process):
        """A successful Plex run records the webhook as received."""
        tasks.process_webhook("plex", {"event": "media.scrobble"}, self.user.id)
        mock_process.assert_called_once()
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.plex_webhook_last_received_at)

    @patch("integrations.webhooks.plex.PlexWebhookProcessor.process_payload")
    def test_plex_failure_marks_error_and_reraises(self, mock_process):
        """A failing Plex run marks the error for the user and re-raises."""
        mock_process.side_effect = ValueError("boom")
        with self.assertRaises(ValueError):
            tasks.process_webhook("plex", {"event": "media.scrobble"}, self.user.id)
        self.user.refresh_from_db()
        self.assertIn("processing failed", self.user.plex_webhook_last_error or "")

    @patch("integrations.webhooks.jellyfin.JellyfinWebhookProcessor.process_payload")
    def test_history_user_context_set_during_processing(self, mock_process):
        """History rows created during processing are attributed to the user."""
        seen = {}

        def capture_context(_payload, _user):
            request = getattr(HistoricalRecords.context, "request", None)
            seen["user"] = getattr(request, "user", None)

        mock_process.side_effect = capture_context
        tasks.process_webhook("jellyfin", {"Event": "Stop"}, self.user.id)
        self.assertEqual(seen["user"], self.user)
        self.assertFalse(hasattr(HistoricalRecords.context, "request"))

    def test_missing_user_logs_and_returns(self):
        """A deleted user is logged and skipped without raising."""
        missing_id = self.user.id + 999
        with self.assertLogs("integrations.tasks", level="WARNING") as logs:
            tasks.process_webhook("jellyfin", {"Event": "Stop"}, missing_id)
        self.assertIn("missing user", logs.output[0])


class WebhookTaskRoutingTests(SimpleTestCase):
    """Webhook processing must never wait behind imports or backfills."""

    def test_webhook_task_routes_to_interactive_queue_at_top_priority(self):
        """The task runs on the interactive worker with interactive priority."""
        route = settings.CELERY_TASK_ROUTES[tasks.process_webhook.name]
        self.assertEqual(route["queue"], "interactive")
        self.assertEqual(
            route["priority"],
            settings.CELERY_TASK_PRIORITY_INTERACTIVE,
        )
