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
    @patch("app.providers.services.openlibrary.book")
    @patch("app.providers.services.openlibrary.search")
    def test_services_search_skips_hardcover_id_lookup_for_large_numeric_queries(
        self,
        mock_openlibrary_search,
        mock_openlibrary_book,
        mock_book,
        mock_search,
    ):
        """Large ISBN values should be treated as text search, not Hardcover IDs."""
        mock_openlibrary_search.return_value = {
            "page": 1,
            "total_results": 0,
            "total_pages": 1,
            "results": [],
        }
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
        mock_openlibrary_book.assert_not_called()
        mock_search.assert_called_once_with("9780063038936", 1)

    @patch("app.providers.services.hardcover.book")
    def test_search_by_id_skips_hardcover_lookup_for_valid_isbn10(
        self,
        mock_book,
    ):
        """Checksum-valid ISBN-10 queries should not be treated as Hardcover IDs."""
        result = services.search_by_id(
            MediaTypes.BOOK.value,
            "0312980388",
            Sources.HARDCOVER.value,
        )

        self.assertIsNone(result)
        mock_book.assert_not_called()

    @patch("app.providers.services.hardcover.book")
    @patch("app.providers.services.hardcover.search")
    @patch("app.providers.services.openlibrary.book")
    @patch("app.providers.services.openlibrary.search")
    def test_services_search_resolves_hardcover_isbn_queries_via_openlibrary_metadata(
        self,
        mock_openlibrary_search,
        mock_openlibrary_book,
        mock_hardcover_search,
        mock_hardcover_book,
    ):
        """ISBN searches should reuse Open Library metadata.

        The resolved metadata should point the user at the matching Hardcover book.
        """
        mock_openlibrary_search.return_value = {
            "page": 1,
            "total_results": 1,
            "total_pages": 1,
            "results": [
                {
                    "media_id": "OL123M",
                    "title": "Daniel's Story",
                    "source": Sources.OPENLIBRARY.value,
                    "media_type": MediaTypes.BOOK.value,
                    "image": "openlibrary.jpg",
                },
            ],
        }
        mock_openlibrary_book.return_value = {
            "media_id": "OL123M",
            "source": Sources.OPENLIBRARY.value,
            "media_type": MediaTypes.BOOK.value,
            "title": "Daniel's Story",
            "image": "openlibrary.jpg",
            "details": {
                "author": ["Carol Matas"],
                "isbn": ["9780590465885"],
            },
            "authors_full": [{"name": "Carol Matas"}],
        }
        mock_hardcover_search.return_value = {
            "page": 1,
            "total_results": 1,
            "total_pages": 1,
            "results": [
                {
                    "media_id": "103196",
                    "source": Sources.HARDCOVER.value,
                    "media_type": MediaTypes.BOOK.value,
                    "title": "Daniel's Story",
                    "image": "hardcover.jpg",
                },
            ],
        }
        mock_hardcover_book.return_value = {
            "media_id": "103196",
            "source": Sources.HARDCOVER.value,
            "media_type": MediaTypes.BOOK.value,
            "title": "Daniel's Story",
            "image": "hardcover.jpg",
            "details": {
                "author": "Carol Matas",
                "isbn": ["9780590465885"],
            },
            "authors_full": [{"name": "Carol Matas"}],
        }

        result = services.search(
            MediaTypes.BOOK.value,
            "9780590465885",
            1,
            Sources.HARDCOVER.value,
        )

        self.assertEqual(result["results"][0]["media_id"], "103196")
        self.assertEqual(result["results"][0]["title"], "Daniel's Story")
        mock_hardcover_search.assert_called_once_with("Daniel's Story Carol Matas", 1)
        mock_hardcover_book.assert_called_once_with("103196")
