from django.test import Client, TestCase, override_settings


@override_settings(
    DEBUG=False,
    ROOT_URLCONF="app.tests.urls_error_pages",
    ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"],
)
class ErrorPageTests(TestCase):
    """Verify custom error pages render copyable traceback panels."""

    def setUp(self):
        """Set up non-raising clients for error-page assertions."""
        self.client = Client()
        self.client.raise_request_exception = False
        self.csrf_client = Client(enforce_csrf_checks=True)
        self.csrf_client.raise_request_exception = False

    def assert_traceback_panel(self, response, panel_id):
        """Assert the shared traceback panel rendered on the response."""
        expected_status = response.status_code
        self.assertContains(
            response,
            f'data-copy-target="#{panel_id}"',
            status_code=expected_status,
            html=False,
        )
        self.assertContains(
            response,
            f'id="{panel_id}"',
            status_code=expected_status,
            html=False,
        )
        self.assertContains(
            response,
            "Copy this block when opening a ticket",
            status_code=expected_status,
            html=False,
        )

    def test_bad_request_page_includes_copyable_traceback(self):
        """The 400 page should expose a copyable traceback report."""
        response = self.client.get("/boom-400/")

        self.assertEqual(response.status_code, 400)
        self.assert_traceback_panel(response, "error-report-400")
        self.assertContains(
            response,
            "SuspiciousOperation: Broken payload",
            status_code=400,
            html=False,
        )

    def test_permission_denied_page_includes_copyable_traceback(self):
        """The 403 page should expose a copyable traceback report."""
        response = self.client.get("/boom-403/")

        self.assertEqual(response.status_code, 403)
        self.assert_traceback_panel(response, "error-report-403")
        self.assertContains(
            response,
            "PermissionDenied: Forbidden area",
            status_code=403,
            html=False,
        )

    def test_not_found_page_includes_copyable_traceback(self):
        """The 404 page should expose a copyable traceback report."""
        response = self.client.get("/boom-404/")

        self.assertEqual(response.status_code, 404)
        self.assert_traceback_panel(response, "error-report-404")
        self.assertContains(
            response,
            "Http404: Missing object",
            status_code=404,
            html=False,
        )

    def test_server_error_page_includes_copyable_traceback(self):
        """The 500 page should expose a copyable traceback report."""
        response = self.client.get("/boom-500/")

        self.assertEqual(response.status_code, 500)
        self.assert_traceback_panel(response, "error-report-500")
        self.assertContains(
            response,
            "RuntimeError: Kaboom",
            status_code=500,
            html=False,
        )

    def test_csrf_failure_page_includes_copyable_report(self):
        """The CSRF failure page should expose a copyable diagnostic report."""
        response = self.csrf_client.post("/csrf-protected/")

        self.assertEqual(response.status_code, 403)
        self.assert_traceback_panel(response, "error-report-403-csrf")
        self.assertContains(
            response,
            "CSRF verification failed",
            status_code=403,
            html=False,
        )
        self.assertContains(
            response,
            "Traceback unavailable",
            status_code=403,
            html=False,
        )
