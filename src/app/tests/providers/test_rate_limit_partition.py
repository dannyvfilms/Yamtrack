"""Tests for process-role detection used to partition API rate limiters.

Background workers get their own, smaller global bucket so backfills and
imports can never exhaust the per-second budget web requests and the
interactive worker rely on.
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from app.providers import services


class ProcessRoleDetectionTests(SimpleTestCase):
    """get_process_role resolves the env label with a safe fallback."""

    def test_explicit_roles_from_environment(self):
        """The supervisord-provided label wins."""
        for role in ("web", "interactive", "background"):
            with patch.dict("os.environ", {"YAMTRACK_PROCESS_ROLE": role}):
                self.assertEqual(services.get_process_role(), role)

    def test_unknown_label_falls_back_to_argv_heuristic(self):
        """An unrecognized label is ignored in favor of the argv check."""
        with (
            patch.dict("os.environ", {"YAMTRACK_PROCESS_ROLE": "bogus"}),
            patch.object(services.sys, "argv", ["/usr/local/bin/celery"]),
        ):
            self.assertEqual(services.get_process_role(), "background")

    def test_unlabeled_celery_process_is_background(self):
        """Celery processes without the env var can't starve the web budget."""
        with (
            patch.dict("os.environ", {}, clear=False),
            patch.object(services.sys, "argv", ["celery", "worker"]),
        ):
            services.os.environ.pop("YAMTRACK_PROCESS_ROLE", None)
            self.assertEqual(services.get_process_role(), "background")

    def test_unlabeled_non_celery_process_is_web(self):
        """gunicorn and manage.py default to the web budget."""
        with (
            patch.dict("os.environ", {}, clear=False),
            patch.object(services.sys, "argv", ["gunicorn"]),
        ):
            services.os.environ.pop("YAMTRACK_PROCESS_ROLE", None)
            self.assertEqual(services.get_process_role(), "web")

    def test_background_role_uses_separate_smaller_bucket(self):
        """The module-level bucket split keys off the background role."""
        # The session is built at import time; assert the wiring constants so a
        # refactor can't silently merge the buckets back together.
        if services.PROCESS_ROLE == "background":
            self.assertTrue(services.bucket_key.endswith("_background"))
        else:
            self.assertFalse(services.bucket_key.endswith("_background"))
