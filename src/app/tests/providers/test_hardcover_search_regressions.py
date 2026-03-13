from unittest.mock import patch

from django.test import TestCase

from app.models import MediaTypes, Sources
from app.providers import hardcover, services


class HardcoverSearchRegressionTests(TestCase):
    """Regression coverage for Hardcover search edge cases."""

    @patch("app.providers.hardcover.services.api_request")
    def test_search_returns_empty_results_when_hardcover_results_are_null(
        self,
        mock_api_request,
    ):
        """Malformed Hardcover search payloads should fail closed, not 500."""
        query = "null-hardcover-results-regression"
        hardcover.cache.delete(
            f"search_{Sources.HARDCOVER.value}_{MediaTypes.BOOK.value}_{query}_1",
        )
        mock_api_request.return_value = {"data": {"search": {"results": None}}}

        response = hardcover.search(query, 1)

        self.assertEqual(response["results"], [])
        self.assertEqual(response["total_results"], 0)

    @patch("app.providers.hardcover.search")
    @patch("app.providers.hardcover.book")
    def test_services_search_skips_hardcover_id_lookup_for_large_numeric_queries(
        self,
        mock_book,
        mock_search,
    ):
        """Large ISBN values should be treated as text search, not Hardcover IDs."""
        mock_search.return_value = {
            "page": 1,
            "total_results": 0,
            "total_pages": 1,
            "results": [],
        }

        result = services.search(
            MediaTypes.BOOK.value,
            "9780063038936",
            1,
            Sources.HARDCOVER.value,
        )

        self.assertEqual(result["results"], [])
        mock_book.assert_not_called()
        mock_search.assert_called_once_with("9780063038936", 1)
