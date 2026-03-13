import logging

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)

base_url = "https://api.hardcover.app/v1/graphql"


def handle_error(error):
    """Handle Hardcover API errors."""
    error_resp = error.response
    status_code = error_resp.status_code

    try:
        error_json = error_resp.json()
    except requests.exceptions.JSONDecodeError as json_error:
        logger.exception("Failed to decode JSON response")
        raise services.ProviderAPIError(Sources.HARDCOVER.value, error) from json_error

    if status_code == requests.codes.unauthorized:
        details = error_json["error"]
        raise services.ProviderAPIError(Sources.HARDCOVER.value, error, details)

    raise services.ProviderAPIError(Sources.HARDCOVER.value, error)


def search(query, page):
    """Search for books on Hardcover."""
    cache_key = (
        f"search_{Sources.HARDCOVER.value}_{MediaTypes.BOOK.value}_{query}_{page}"
    )
    data = cache.get(cache_key)

    if data is None:
        search_query = """
        query SearchBooks($query: String!, $per_page: Int!, $page: Int!) {
          search(
            query: $query,
            query_type: "Book",
            per_page: $per_page,
            page: $page,
          ) {
            results
          }
        }
        """

        variables = {
            "query": query,
            "per_page": settings.PER_PAGE,
            "page": page,
        }

        try:
            response = services.api_request(
                Sources.HARDCOVER.value,
                "POST",
                base_url,
                params={"query": search_query, "variables": variables},
                headers={"Authorization": settings.HARDCOVER_API},
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        # Check for GraphQL errors in the response
        if "errors" in response:
            error_messages = [err.get("message", "Unknown error") for err in response["errors"]]
            logger.error("GraphQL errors from Hardcover API: %s", error_messages)
            # Return empty results on GraphQL errors
            return helpers.format_search_response(page, settings.PER_PAGE, 0, [])

        # Check if data key exists
        if "data" not in response or "search" not in response["data"]:
            logger.error("Invalid response structure from Hardcover API: %s", response)
            return helpers.format_search_response(page, settings.PER_PAGE, 0, [])

        search_data = response["data"].get("search")
        if not isinstance(search_data, dict):
            logger.warning(
                "Invalid Hardcover search payload for query=%r page=%s: %s",
                query,
                page,
                response,
            )
            return helpers.format_search_response(page, settings.PER_PAGE, 0, [])

        results_data = search_data.get("results")
        if not isinstance(results_data, dict):
            logger.warning(
                "Invalid Hardcover search results payload for query=%r page=%s: %s",
                query,
                page,
                response,
            )
            return helpers.format_search_response(page, settings.PER_PAGE, 0, [])

        hits = results_data.get("hits") or []
        results = [
            {
                "media_id": hit["document"]["id"],
                "source": Sources.HARDCOVER.value,
                "media_type": MediaTypes.BOOK.value,
                "title": hit["document"]["title"],
                "image": get_image_url(hit["document"]),
                "year": get_year(hit["document"].get("release_date")),
            }
            for hit in hits
        ]
        total_results = results_data.get("found") or 0

        data = helpers.format_search_response(
            page,
            settings.PER_PAGE,
            total_results,
            results,
        )

        cache.set(cache_key, data)

    return data


def book(media_id):
    """Get metadata for a book from Hardcover."""
    cache_key = f"{Sources.HARDCOVER.value}_{MediaTypes.BOOK.value}_{media_id}"
    data = cache.get(cache_key)

    if data is None:
        book_query = """
        query GetBookDetails($book_id: Int!) {
          books_by_pk(id: $book_id) {
            id
            title
            cached_image(path: "url")
            description
            cached_tags(path: "Genre")
            rating
            ratings_count
            pages
            release_date
            slug
            cached_contributors
            default_cover_edition {
              edition_format
              isbn_13
              isbn_10
              release_date
            }
            featured_book_series {
              position
              series_id
            }
          }
        }
        """

        variables = {
            "book_id": int(media_id),
        }

        try:
            response = services.api_request(
                Sources.HARDCOVER.value,
                "POST",
                base_url,
                params={"query": book_query, "variables": variables},
                headers={"Authorization": settings.HARDCOVER_API},
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        # Check for GraphQL errors in the response
        if "errors" in response:
            error_messages = [err.get("message", "Unknown error") for err in response["errors"]]
            logger.warning("GraphQL errors from Hardcover API: %s", error_messages)
            # Continue processing if we can still get book data despite errors

        # Check if data key exists
        if "data" not in response:
            logger.error("No 'data' key in Hardcover API response: %s", response)
            services.raise_not_found_error(
                Sources.HARDCOVER.value, media_id, "book",
            )

        book_data = response["data"].get("books_by_pk")

        if not book_data:
            services.raise_not_found_error(
                Sources.HARDCOVER.value,
                media_id,
                "book",
            )

        edition_details = get_edition_details(book_data.get("default_cover_edition"))
        series_data = process_series_data(book_data.get("featured_book_series"))

        related = {}
        if series_data.get("books"):
            # Use specific series name if available, otherwise default to "Series"
            series_title = series_data.get("name") or "Series"
            related[series_title] = series_data["books"]
        authors_full = get_authors_full(book_data.get("cached_contributors"))
        author_names = [author.get("name") for author in authors_full if author.get("name")]

        data = {
            "media_id": book_data["id"],
            "source": Sources.HARDCOVER.value,
            "source_url": f"https://hardcover.app/books/{book_data['slug']}",
            "media_type": MediaTypes.BOOK.value,
            "title": book_data["title"],
            "max_progress": book_data.get("pages"),
            "image": book_data.get("cached_image") or settings.IMG_NONE,
            "synopsis": book_data.get("description") or "No synopsis available.",
            "genres": get_tags(book_data.get("cached_tags")),
            "score": get_ratings(book_data.get("rating")),
            "score_count": book_data.get("ratings_count", 0),
            "series_name": series_data.get("name"),
            "series_position": series_data.get("position"),
            "details": {
                "format": edition_details.get("format"),
                "number_of_pages": book_data.get("pages"),
                "publish_date": edition_details.get("release_date")
                or book_data.get("release_date"),
                "author": ", ".join(author_names) if author_names else None,
                "publisher": edition_details.get("publisher"),
                "isbn": edition_details.get("isbn"),
            },
            "authors_full": authors_full,
            "related": related,
        }

        cache.set(cache_key, data)

    return data


def get_tags(tags_data):
    """Get processed tags/genres from API data."""
    if not tags_data:
        return None
    return [tag["tag"] for tag in tags_data]


def _extract_image_url(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return (
            value.get("url")
            or value.get("medium_url")
            or value.get("large_url")
            or value.get("original_url")
            or ""
        )
    return ""


def get_authors_full(contributors):
    """Normalize Hardcover contributor payload into authors_full rows."""
    if not contributors or not isinstance(contributors, list):
        return []

    authors = []
    seen = set()
    for index, contributor in enumerate(contributors):
        if not isinstance(contributor, dict):
            continue

        author_data = contributor.get("author")
        if not isinstance(author_data, dict):
            author_data = contributor

        person_id = (
            author_data.get("id")
            or contributor.get("author_id")
            or contributor.get("id")
        )
        name = (
            (author_data.get("name") if isinstance(author_data, dict) else None)
            or contributor.get("name")
            or ""
        ).strip()
        if person_id is None or not name:
            continue

        dedupe_key = str(person_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        image = (
            _extract_image_url(author_data.get("cached_image") if isinstance(author_data, dict) else "")
            or _extract_image_url(author_data.get("image") if isinstance(author_data, dict) else "")
            or _extract_image_url(contributor.get("cached_image"))
            or _extract_image_url(contributor.get("image"))
            or settings.IMG_NONE
        )
        role = (
            contributor.get("contribution")
            or contributor.get("role")
            or contributor.get("type")
            or "Author"
        )
        sort_order = contributor.get("position")
        if sort_order is None:
            sort_order = index

        authors.append(
            {
                "person_id": str(person_id),
                "name": name,
                "image": image,
                "role": role,
                "sort_order": sort_order,
            },
        )

    return authors


def get_ratings(rating_data):
    """Get processed rating from API data."""
    if not rating_data:
        return None
    return round(float(rating_data) * 2, 1)


def get_edition_details(edition_data):
    """Get processed edition details from API data."""
    if not edition_data:
        return {}

    isbns = []
    if edition_data.get("isbn_10"):
        isbns.append(edition_data["isbn_10"])
    if edition_data.get("isbn_13"):
        isbns.append(edition_data["isbn_13"])

    publisher_name = None
    if edition_data.get("publisher"):
        publisher_name = edition_data["publisher"].get("name")

    return {
        "format": edition_data.get("edition_format") or "Unknown",
        "publisher": publisher_name,
        "isbn": isbns or None,
        "release_date": edition_data.get("release_date"),
    }


def get_year(date_value):
    """Extract a publication year from a date string."""
    if not date_value:
        return None

    try:
        return int(str(date_value).split("-")[0])
    except (TypeError, ValueError):
        return None


def get_image_url(response):
    """Get the cover image URL for a book."""
    if response.get("image") and response["image"].get("url"):
        return response["image"]["url"]
    return settings.IMG_NONE


def author_profile(author_id):
    """Return metadata for a Hardcover author profile."""
    cache_key = f"{Sources.HARDCOVER.value}_person_{author_id}"
    data = cache.get(cache_key)
    if data is not None:
        return data

    profile_query = """
    query GetAuthorProfile($author_id: Int!) {
      authors_by_pk(id: $author_id) {
        id
        name
        bio
        cached_image(path: "url")
        born_date
        death_date
        location
        contributions(limit: 200, order_by: {id: desc}) {
          contribution
          book {
            id
            title
            slug
            release_date
            cached_image(path: "url")
          }
        }
      }
    }
    """

    variables = {
        "author_id": int(author_id),
    }

    try:
        response = services.api_request(
            Sources.HARDCOVER.value,
            "POST",
            base_url,
            params={"query": profile_query, "variables": variables},
            headers={"Authorization": settings.HARDCOVER_API},
        )
    except requests.exceptions.HTTPError as error:
        handle_error(error)

    author_data = (response.get("data") or {}).get("authors_by_pk") or {}
    if "errors" in response:
        error_messages = [err.get("message", "Unknown error") for err in response["errors"]]
        logger.error("GraphQL errors from Hardcover API (author profile): %s", error_messages)
        if not author_data:
            services.raise_not_found_error(Sources.HARDCOVER.value, author_id, "author")

    if not author_data:
        services.raise_not_found_error(Sources.HARDCOVER.value, author_id, "author")

    bibliography = []
    seen_book_ids = set()
    for contribution in author_data.get("contributions", []) or []:
        if not isinstance(contribution, dict):
            continue
        entry = contribution.get("book")
        if not isinstance(entry, dict):
            continue
        media_id = entry.get("id")
        title = entry.get("title")
        if media_id is None or not title:
            continue
        book_id = str(media_id)
        if book_id in seen_book_ids:
            continue
        seen_book_ids.add(book_id)
        bibliography.append(
            {
                "media_id": book_id,
                "source": Sources.HARDCOVER.value,
                "media_type": MediaTypes.BOOK.value,
                "title": title,
                "image": entry.get("cached_image") or settings.IMG_NONE,
                "year": get_year(entry.get("release_date")),
                "role": contribution.get("contribution") or "Author",
            },
        )

    data = {
        "person_id": str(author_data.get("id") or author_id),
        "source": Sources.HARDCOVER.value,
        "name": author_data.get("name") or "",
        "image": author_data.get("cached_image") or settings.IMG_NONE,
        "biography": author_data.get("bio") or "",
        "known_for_department": "Author",
        "birth_date": author_data.get("born_date"),
        "death_date": author_data.get("death_date"),
        "place_of_birth": author_data.get("location") or "",
        "bibliography": bibliography,
    }
    cache.set(cache_key, data)
    return data


def get_series_details(series_id):
    """Fetch series details including all books in the series."""
    series_query = """
    query GetSeriesBooks($series_id: Int!) {
      book_series(where: {series_id: {_eq: $series_id}, featured: {_eq: true}}, order_by: {position: asc}) {
        position
        book {
          id
          title
          slug
          rating
          ratings_count
          pages
          release_date
          compilation
          book_category_id
          cached_image(path: "url")
        }
      }
    }
    """

    variables = {"series_id": series_id}

    try:
        response = services.api_request(
            Sources.HARDCOVER.value,
            "POST",
            base_url,
            params={"query": series_query, "variables": variables},
            headers={"Authorization": settings.HARDCOVER_API},
        )
    except requests.exceptions.HTTPError as error:
        logger.warning("Failed to fetch series details: %s", error)
        return None

    if "errors" in response:
        logger.warning("GraphQL errors fetching series: %s", response["errors"])
        return None

    return response.get("data", {}).get("book_series", [])


def process_series_data(featured_series):
    """Process series data from Hardcover API."""
    if not featured_series:
        return {}

    series_id = featured_series.get("series_id")
    series_name = None
    series_books = []

    if series_id:
        series_items = get_series_details(series_id)
        
        if series_items:
            # Note: The Hardcover API currently doesn't expose the Series object directly via GraphQL
            # for the series_id returned in featured_book_series, nor does book_series link to it.
            # So we cannot get the series name easily. We default to None, which will result in "Series" as the label.

            # Deduplicate by position, picking the one with highest ratings_count
            best_by_position = {}
            for item in series_items:
                pos = item.get("position")
                if pos is None:
                    continue
                
                book_data = item.get("book")
                if not book_data:
                    continue

                # Filter out placeholders, bonus chapters, and bundles
                title = book_data.get("title", "") or ""
                if "untitled" in title.lower():
                    continue
                if "bonus chapter" in title.lower():
                    continue
                if book_data.get("compilation") or book_data.get("book_category_id") == 8:
                    continue

                # Use ratings_count as a proxy for "most representative" edition
                ratings = book_data.get("ratings_count", 0) or 0
                
                if pos not in best_by_position:
                    best_by_position[pos] = item
                else:
                    existing_ratings = best_by_position[pos].get("book", {}).get("ratings_count", 0) or 0
                    if ratings > existing_ratings:
                        best_by_position[pos] = item

            # Sort by position
            sorted_positions = sorted(best_by_position.keys())

            # Process books in the series
            limit_books = 25  # Limit number of books to show in collection
            for pos in sorted_positions[:limit_books]:
                item = best_by_position[pos]
                book_data = item.get("book")
                
                series_books.append(
                    {
                        "media_id": book_data["id"],
                        "source": Sources.HARDCOVER.value,
                        "media_type": MediaTypes.BOOK.value,
                        "title": book_data["title"],
                        "image": book_data.get("cached_image") or "https://assets.hardcover.app/static/covers/cover4.webp",
                        "year": get_year(book_data.get("release_date")),
                        "series_position": pos,
                    }
                )

    # Return available position info
    return {
        "name": series_name,
        "position": featured_series.get("position"),
        "books": series_books,
    }
