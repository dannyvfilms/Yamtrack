"""Tests for home-screen row title destination links."""

from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import MediaTypes
from lists.models import CustomList
from users.home_screen import home_row_destination_url
from users.models import HomeScreenRow, HomeScreenRowTypeChoices


class HomeRowLinkTests(TestCase):
    """Validate the URL a home row title links to."""

    def setUp(self):
        """Create a user for the row owner."""
        self.user = get_user_model().objects.create_user(
            username="rowlink",
            password="pass",  # noqa: S106
        )

    def test_library_query_row_link_has_sort_direction_layout_filters(self):
        """Library-query row link carries sort/direction/layout/filters."""
        row = HomeScreenRow.objects.create(
            user=self.user,
            media_type=MediaTypes.BOOK.value,
            row_type=HomeScreenRowTypeChoices.LIBRARY_QUERY,
            sort_by="date_added",
            direction="desc",
            filters={"status": "In progress"},
        )

        parsed = urlparse(home_row_destination_url(row, self.user))
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/medialist/book")
        self.assertEqual(query["sort"], ["date_added"])
        self.assertEqual(query["direction"], ["desc"])
        self.assertIn("layout", query)
        self.assertEqual(query["status"], ["In progress"])

    def test_recently_unrated_row_links_with_not_rated_filter(self):
        """A recently-unrated row links to the media list filtered to unrated items."""
        row = HomeScreenRow.objects.create(
            user=self.user,
            media_type=MediaTypes.MOVIE.value,
            row_type=HomeScreenRowTypeChoices.RECENTLY_UNRATED,
            sort_by="title",
            direction="asc",
            filters={},
        )

        parsed = urlparse(home_row_destination_url(row, self.user))
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/medialist/movie")
        self.assertEqual(query["rating"], ["not_rated"])

    def test_custom_list_row_links_to_list_detail(self):
        """A custom-list row links to the list detail page (with a compatible sort)."""
        custom_list = CustomList.objects.create(owner=self.user, name="My List")
        row = HomeScreenRow.objects.create(
            user=self.user,
            media_type=MediaTypes.BOOK.value,
            row_type=HomeScreenRowTypeChoices.CUSTOM_LIST,
            custom_list=custom_list,
            sort_by="title",
            direction="asc",
        )

        url = home_row_destination_url(row, self.user)
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.path, f"/list/{custom_list.public_reference}")
        # "title" is a valid list-detail sort, so it is carried through.
        self.assertEqual(query["sort"], ["title"])
        self.assertEqual(query["direction"], ["asc"])
