"""Tests for boosted (hx-boost) navigation handling in page views.

Boosted navigation sends HX-Request like any HTMX request, but the response
replaces the whole page, so views must return the full template instead of
the fragment they serve to in-page HTMX requests.
"""

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from app import helpers
from app.models import MediaTypes
from lists.models import CustomList


class IsHtmxFragmentTests(TestCase):
    """Test the is_htmx_fragment request helper."""

    def setUp(self):
        """Create a request factory."""
        self.factory = RequestFactory()

    def test_plain_request_is_not_fragment(self):
        """A non-HTMX request is not a fragment request."""
        request = self.factory.get("/")
        self.assertFalse(helpers.is_htmx_fragment(request))

    def test_htmx_request_is_fragment(self):
        """A plain HTMX request expects a fragment response."""
        request = self.factory.get("/", HTTP_HX_REQUEST="true")
        self.assertTrue(helpers.is_htmx_fragment(request))

    def test_boosted_request_is_not_fragment(self):
        """A boosted navigation request needs the full page."""
        request = self.factory.get(
            "/",
            HTTP_HX_REQUEST="true",
            HTTP_HX_BOOSTED="true",
        )
        self.assertFalse(helpers.is_htmx_fragment(request))

    def test_history_restore_request_is_not_fragment(self):
        """A history restore request needs the full page."""
        request = self.factory.get(
            "/",
            HTTP_HX_REQUEST="true",
            HTTP_HX_HISTORY_RESTORE_REQUEST="true",
        )
        self.assertFalse(helpers.is_htmx_fragment(request))


class BoostedNavigationViewTests(TestCase):
    """Test that page views return full pages for boosted navigation."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

    def test_media_list_htmx_request_returns_fragment(self):
        """In-page HTMX requests keep getting the grid fragment."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"<aside", response.content)

    def test_media_list_boosted_request_returns_full_page(self):
        """Boosted navigation to a media list renders the full page."""
        response = self.client.get(
            reverse("medialist", args=[MediaTypes.MOVIE.value]),
            HTTP_HX_REQUEST="true",
            HTTP_HX_BOOSTED="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/media_list.html")
        self.assertIn(b"<aside", response.content)

    def test_lists_htmx_request_returns_fragment(self):
        """In-page HTMX requests keep getting the list grid fragment."""
        response = self.client.get(
            reverse("lists"),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/list_grid.html")
        self.assertIn("HX-Boosted", response["Vary"])

    def test_lists_boosted_request_returns_full_page(self):
        """Boosted navigation to the lists page renders the full page."""
        response = self.client.get(
            reverse("lists"),
            HTTP_HX_REQUEST="true",
            HTTP_HX_BOOSTED="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/custom_lists.html")

    def test_list_detail_htmx_request_returns_fragment(self):
        """In-page HTMX requests keep getting the list items fragment."""
        custom_list = CustomList.objects.create(name="My List", owner=self.user)
        response = self.client.get(
            reverse("list_detail", args=[custom_list.public_reference]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/components/media_grid.html")

    def test_list_detail_boosted_request_returns_full_page(self):
        """Boosted navigation to a list detail renders the full page."""
        custom_list = CustomList.objects.create(name="My List", owner=self.user)
        response = self.client.get(
            reverse("list_detail", args=[custom_list.public_reference]),
            HTTP_HX_REQUEST="true",
            HTTP_HX_BOOSTED="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "lists/list_detail.html")
