import logging
import re
import time
from difflib import SequenceMatcher

import requests
from defusedxml import ElementTree
from django.conf import settings
from pyrate_limiter import RedisBucket
from redis import ConnectionPool
from requests.adapters import HTTPAdapter
from requests_ratelimiter import LimiterAdapter, LimiterSession

from app import config
from app.log_safety import exception_summary, mapping_keys
from app.models import Item, MediaTypes, Sources
from app.providers import (
    bgg,
    comicvine,
    hardcover,
    igdb,
    mal,
    mangaupdates,
    manual,
    musicbrainz,
    openlibrary,
    pocketcasts,
    tvdb,
    tmdb,
)

logger = logging.getLogger(__name__)


def _audiobookshelf_book(media_id):
    """Return local metadata for an Audiobookshelf book item.

    Audiobookshelf library item IDs are not Open Library IDs, so attempting to
    resolve them via Open Library causes 404s and can break details pages.
    """
    from app.models import Item  # noqa: PLC0415

    item = Item.objects.filter(
        media_id=media_id,
        source=Sources.AUDIOBOOKSHELF.value,
        media_type=MediaTypes.BOOK.value,
    ).first()

    title = item.title if item else ""
    image = item.image if item and item.image else settings.IMG_NONE
    runtime_minutes = item.runtime_minutes if item else None
    authors = item.authors if item else []
    isbn = item.isbn if item else []
    genres = item.genres if item else []
    publishers = item.publishers if item else ""
    publish_date = (
        item.release_datetime.date().isoformat()
        if item and item.release_datetime
        else None
    )
    format_name = item.format if item and item.format else "audiobook"

    return {
        "media_id": str(media_id),
        "source": Sources.AUDIOBOOKSHELF.value,
        "media_type": MediaTypes.BOOK.value,
        "title": title,
        "image": image,
        "max_progress": runtime_minutes,
        "synopsis": "",
        "genres": genres,
        "related": {},
        "details": {
            "author": authors,
            "isbn": isbn,
            "publisher": publishers,
            "publish_date": publish_date,
            "format": format_name,
            "runtime_minutes": runtime_minutes,
        },
    }


def get_redis_connection():
    """Return a Redis connection pool."""
    if settings.TESTING:
        import fakeredis  # noqa: PLC0415

        return fakeredis.FakeStrictRedis().connection_pool
    return ConnectionPool.from_url(settings.REDIS_URL)


redis_pool = get_redis_connection()
bucket_name = f"{settings.REDIS_PREFIX}_api" if settings.REDIS_PREFIX else "api"

session = LimiterSession(
    per_second=5,
    bucket_class=RedisBucket,
    bucket_kwargs={"redis_pool": redis_pool, "bucket_name": bucket_name},
)

session.mount("http://", HTTPAdapter(max_retries=3))
session.mount("https://", HTTPAdapter(max_retries=3))

session.mount(
    "https://api.myanimelist.net/v2",
    LimiterAdapter(per_minute=30),
)
session.mount(
    "https://graphql.anilist.co",
    LimiterAdapter(per_minute=85),
)
session.mount(
    "https://api.igdb.com/v4",
    LimiterAdapter(per_second=3),
)
session.mount(
    "https://api4.thetvdb.com",
    LimiterAdapter(per_second=2),
)
session.mount(
    "https://comicvine.gamespot.com/api",
    LimiterAdapter(per_hour=190),
)
session.mount(
    "https://openlibrary.org",
    LimiterAdapter(per_minute=20),
)
session.mount(
    "https://api.hardcover.app/v1/graphql",
    LimiterAdapter(per_minute=50),
)
session.mount(
    "https://boardgamegeek.com/xmlapi2",
    LimiterAdapter(per_second=2),
)


class ProviderAPIError(Exception):
    """Exception raised when a provider API fails to respond."""

    def __init__(self, provider, error, details=None):
        """Initialize the exception with the provider name."""
        self.provider = provider
        self.status_code = error.response.status_code
        try:
            provider = Sources(provider).label
        except ValueError:
            provider = provider.title()

        response = error.response
        response_keys = []
        content_type = None
        if response is not None:
            raw_headers = getattr(response, "headers", None)
            headers = raw_headers if isinstance(raw_headers, dict) else {}
            raw_content_type = headers.get("Content-Type")
            if isinstance(raw_content_type, str):
                content_type = raw_content_type.split(";", 1)[0]

            if content_type and "json" in content_type:
                json_loader = getattr(response, "json", None)
                if callable(json_loader):
                    try:
                        response_keys = mapping_keys(json_loader())
                    except (TypeError, ValueError):
                        response_keys = []

        log_method = logger.warning if self.status_code == 404 else logger.error
        if response_keys:
            log_method(
                "%s api error status=%s response_keys=%s",
                provider,
                self.status_code,
                response_keys,
            )
        else:
            response_text = getattr(response, "text", "") if response is not None else ""
            body_length = len(response_text) if isinstance(response_text, str) else 0
            log_method(
                "%s api error status=%s content_type=%s body_length=%s",
                provider,
                self.status_code,
                content_type or None,
                body_length,
            )

        message = (
            f"There was an error contacting the {provider} API "
            f"(HTTP {self.status_code})"
        )
        if details:
            message += f": {details}"
        message += ". Check the logs for more details."
        super().__init__(message)


def raise_not_found_error(provider, media_id, media_type="item"):
    """
    Raise a 404 ProviderAPIError for when a media item is not found.

    Args:
        provider: The provider source value (e.g., Sources.COMICVINE.value)
        media_id: The media ID that was not found
        media_type: The type of media (e.g., "comic", "game", "book")
    """
    error_msg = f"{media_type.capitalize()} with ID {media_id} not found"
    logger.error("%s %s lookup failed with 404", provider, media_type)

    # Create a mock 404 error response
    mock_response = type(
        "obj",
        (object,),
        {
            "status_code": 404,
            "headers": {},
            "text": error_msg,
            "json": lambda self: {},
        },
    )()
    mock_error = requests.exceptions.HTTPError(response=mock_response)

    raise ProviderAPIError(provider, mock_error, error_msg)


def api_request(
    provider,
    method,
    url,
    params=None,
    data=None,
    headers=None,
    response_format="json",
):
    """Make a request to the API and return the response.

    Args:
        provider: Provider identifier for error messages
        method: HTTP method ("GET" or "POST")
        url: Request URL
        params: Query params for GET, JSON body for POST
        data: Raw data for POST
        headers: Request headers
        response_format: "json" (default) or "xml" for XML parsing

    Returns:
        Parsed JSON dict or ElementTree for XML
    """
    try:
        request_kwargs = {
            "url": url,
            "headers": headers,
            "timeout": settings.REQUEST_TIMEOUT,
        }

        if method == "GET":
            request_kwargs["params"] = params
            request_func = session.get
        elif method == "POST":
            request_kwargs["data"] = data
            request_kwargs["json"] = params
            request_func = session.post

        response = request_func(**request_kwargs)
        response.raise_for_status()

        if response_format == "xml":
            return ElementTree.fromstring(response.text)
        return response.json()

    except requests.exceptions.HTTPError as error:
        error_resp = error.response
        status_code = error_resp.status_code

        # handle rate limiting
        if status_code == requests.codes.too_many_requests:
            seconds_to_wait = int(error_resp.headers.get("Retry-After", 5))
            logger.warning("Rate limited, waiting %s seconds", seconds_to_wait)
            time.sleep(seconds_to_wait + 3)
            logger.info("Retrying request")
            return api_request(
                provider,
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                response_format=response_format,
            )

        raise error from None


def _ensure_title_fields(metadata):
    """Normalize metadata title fields across providers."""
    if not isinstance(metadata, dict):
        return metadata

    metadata.update(Item.title_fields_from_metadata(metadata))
    return metadata


def get_media_metadata(
    media_type,
    media_id,
    source,
    season_numbers=None,
    episode_number=None,
):
    """Return the metadata for the selected media."""
    if media_type == MediaTypes.MUSIC.value and source == Sources.MANUAL.value:
        return _ensure_title_fields(
            {
                "max_progress": None,
                "title": "",
                "image": "",
                "related": {},
                "details": {},
            },
        )

    if source == Sources.MANUAL.value:
        if media_type == MediaTypes.SEASON.value:
            return _ensure_title_fields(manual.season(media_id, season_numbers[0]))
        if media_type == MediaTypes.EPISODE.value:
            return _ensure_title_fields(manual.episode(media_id, season_numbers[0], episode_number))
        if media_type == "tv_with_seasons":
            media_type = MediaTypes.TV.value
        return _ensure_title_fields(manual.metadata(media_id, media_type))

    def tmdb_season_metadata():
        """Return TMDB season metadata or raise a not-found error."""
        seasons = tmdb.tv_with_seasons(media_id, season_numbers)
        season_key = f"season/{season_numbers[0]}"
        if season_key not in seasons:
            raise_not_found_error(
                Sources.TMDB.value,
                media_id,
                media_type=f"season {season_numbers[0]}",
            )
        return seasons[season_key]

    def tvdb_series_metadata(routed_media_type=MediaTypes.TV.value):
        """Return TVDB series metadata for TV or grouped anime routes."""
        return tvdb.tv(media_id, routed_media_type=routed_media_type)

    def tvdb_series_with_seasons(routed_media_type=MediaTypes.TV.value):
        """Return TVDB season bundle for TV or grouped anime routes."""
        return tvdb.tv_with_seasons(
            media_id,
            season_numbers,
            routed_media_type=routed_media_type,
        )

    def tvdb_season_metadata(routed_media_type=MediaTypes.TV.value):
        """Return TVDB season metadata or raise a not-found error."""
        seasons = tvdb_series_with_seasons(routed_media_type)
        season_key = f"season/{season_numbers[0]}"
        if season_key not in seasons:
            raise_not_found_error(
                Sources.TVDB.value,
                media_id,
                media_type=f"season {season_numbers[0]}",
            )
        return seasons[season_key]

    metadata_retrievers = {
        MediaTypes.ANIME.value: lambda: (
            mal.anime(media_id)
            if source == Sources.MAL.value
            else tmdb.tv(media_id) | {
                "media_type": MediaTypes.ANIME.value,
                "identity_media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.ANIME.value,
            }
            if source == Sources.TMDB.value
            else tvdb_series_metadata(MediaTypes.ANIME.value)
        ),
        MediaTypes.MANGA.value: lambda: (
            mangaupdates.manga(media_id)
            if source == Sources.MANGAUPDATES.value
            else mal.manga(media_id)
        ),
        MediaTypes.TV.value: lambda: (
            tmdb.tv(media_id)
            if source == Sources.TMDB.value
            else tvdb_series_metadata(MediaTypes.TV.value)
        ),
        "tv_with_seasons": lambda: (
            tmdb.tv_with_seasons(media_id, season_numbers)
            if source == Sources.TMDB.value
            else tvdb_series_with_seasons(MediaTypes.TV.value)
        ),
        MediaTypes.SEASON.value: (
            tmdb_season_metadata
            if source == Sources.TMDB.value
            else tvdb_season_metadata
        ),
        MediaTypes.EPISODE.value: lambda: (
            tmdb.episode(
                media_id,
                season_numbers[0],
                episode_number,
            )
            if source == Sources.TMDB.value
            else tvdb.episode(
                media_id,
                season_numbers[0],
                episode_number,
            )
        ),
        MediaTypes.MOVIE.value: lambda: tmdb.movie(media_id),
        MediaTypes.GAME.value: lambda: igdb.game(media_id),
        MediaTypes.BOOK.value: lambda: (
            hardcover.book(media_id)
            if source == Sources.HARDCOVER.value
            else _audiobookshelf_book(media_id)
            if source == Sources.AUDIOBOOKSHELF.value
            else openlibrary.book(media_id)
        ),
        MediaTypes.COMIC.value: lambda: comicvine.comic(media_id),
        MediaTypes.BOARDGAME.value: lambda: bgg.boardgame(media_id),
        MediaTypes.MUSIC.value: lambda: musicbrainz.recording(media_id),
        MediaTypes.PODCAST.value: lambda s=source: {
            "max_progress": None,
            "title": "",
            "image": "",
            "related": {},
            "details": {},
            # Podcasts use runtime_minutes from Item, not external metadata.
            "source": s,  # Add source to fix KeyError in template tag
        },
    }
    if media_type == MediaTypes.MUSIC.value:
        if not media_id or len(str(media_id)) < 30:
            return _ensure_title_fields(
                {
                    "max_progress": None,
                    "title": "",
                    "image": "",
                    "related": {},
                    "details": {},
                },
            )
        try:
            return _ensure_title_fields(metadata_retrievers[media_type]())
        except Exception as exc:  # pragma: no cover - defensive guard for bad IDs
            logger.debug(
                "Music metadata lookup failed source=%s error=%s",
                source,
                exception_summary(exc),
            )
            return _ensure_title_fields(
                {
                    "max_progress": None,
                    "title": "",
                    "image": "",
                    "related": {},
                    "details": {},
                },
            )

    return _ensure_title_fields(metadata_retrievers[media_type]())


# ---------------------------------------------------------------------------
# Direct ID lookup helpers
# ---------------------------------------------------------------------------

_NUMERIC_ID_RE = re.compile(r"^\d+$")
_OL_ID_RE = re.compile(r"^OL\d+[A-Za-z]$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_GRAPHQL_INT_MAX = 2_147_483_647
_ISBN_CLEAN_RE = re.compile(r"[^0-9Xx]+")


def _normalize_source_value(source):
    """Return a string provider value from enums or raw strings."""
    if isinstance(source, Sources):
        return source.value
    return str(source) if source is not None else None


def _resolve_search_source(media_type, source=None):
    """Return the effective search provider for a media type."""
    resolved = _normalize_source_value(source)
    if resolved:
        if resolved == Sources.TVDB.value and not tvdb.enabled():
            resolved = None
        else:
            return resolved

    default_source = config.get_default_source_name(media_type)
    default_value = _normalize_source_value(default_source)
    if default_value != Sources.TVDB.value or tvdb.enabled():
        return default_value

    for candidate in config.get_sources(media_type) or []:
        candidate_value = _normalize_source_value(candidate)
        if candidate_value == Sources.TVDB.value and not tvdb.enabled():
            continue
        if candidate_value:
            return candidate_value

    return default_value


def _metadata_to_search_result(metadata):
    """Convert a full metadata dict to a minimal search-result entry."""
    return {
        "media_id": str(metadata.get("media_id", "")),
        "source": metadata.get("source"),
        "media_type": metadata.get("media_type"),
        "title": metadata.get("title", ""),
        "original_title": metadata.get("original_title"),
        "localized_title": metadata.get("localized_title"),
        "image": metadata.get("image", settings.IMG_NONE),
    }


def _normalize_isbn_candidate(value):
    """Return a normalized ISBN-10/13 when the input passes checksum validation."""
    cleaned = _ISBN_CLEAN_RE.sub("", str(value or "")).upper()
    if len(cleaned) == 10 and re.fullmatch(r"\d{9}[\dX]", cleaned):
        total = 0
        for index, char in enumerate(cleaned):
            digit = 10 if char == "X" else int(char)
            total += (10 - index) * digit
        return cleaned if total % 11 == 0 else None

    if len(cleaned) == 13 and cleaned.isdigit():
        checksum = 0
        for index, char in enumerate(cleaned[:12]):
            checksum += int(char) * (1 if index % 2 == 0 else 3)
        expected = (10 - checksum % 10) % 10
        return cleaned if expected == int(cleaned[-1]) else None

    return None


def _normalize_name(value):
    normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _title_similarity(left, right):
    normalized_left = _normalize_name(left)
    normalized_right = _normalize_name(right)
    if not normalized_left or not normalized_right:
        return 0.0
    if normalized_left == normalized_right:
        return 1.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _extract_author_names(metadata):
    """Return normalized author names from provider metadata."""
    if not isinstance(metadata, dict):
        return []

    names = []
    authors_full = metadata.get("authors_full")
    if isinstance(authors_full, list):
        for author in authors_full:
            if isinstance(author, dict):
                name = str(author.get("name") or "").strip()
                if name:
                    names.append(name)

    details = metadata.get("details") or {}
    detail_authors = details.get("author")
    if isinstance(detail_authors, str):
        names.extend(part.strip() for part in detail_authors.split(",") if part.strip())
    elif isinstance(detail_authors, list):
        names.extend(str(part).strip() for part in detail_authors if str(part).strip())

    seen = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return seen


def _extract_isbns(metadata):
    """Return normalized ISBNs from provider metadata."""
    if not isinstance(metadata, dict):
        return set()

    details = metadata.get("details") or {}
    raw_isbns = details.get("isbn") or []
    if not isinstance(raw_isbns, list):
        raw_isbns = [raw_isbns]

    normalized = set()
    for raw_isbn in raw_isbns:
        isbn = _normalize_isbn_candidate(raw_isbn)
        if isbn:
            normalized.add(isbn)
    return normalized


def _resolve_hardcover_isbn_search(query, page):
    """Resolve ISBN queries against Hardcover using Open Library metadata."""
    from app import helpers  # noqa: PLC0415

    isbn = _normalize_isbn_candidate(query)
    if not isbn:
        return None

    try:
        ol_results = openlibrary.search(isbn, 1).get("results", [])
    except Exception:  # noqa: BLE001
        return None

    best_fallback_response = None
    for ol_result in ol_results[:3]:
        title = str(ol_result.get("title") or "").strip()
        authors = []
        target_isbns = {isbn}

        media_id = ol_result.get("media_id")
        if media_id:
            try:
                ol_metadata = openlibrary.book(str(media_id))
            except Exception:  # noqa: BLE001
                ol_metadata = None
            else:
                title = str(ol_metadata.get("title") or title).strip()
                authors = _extract_author_names(ol_metadata)
                target_isbns.update(_extract_isbns(ol_metadata))

        if not title:
            continue

        search_queries = []
        if authors:
            search_queries.append(f"{title} {authors[0]}".strip())
        search_queries.append(title)

        seen_queries = set()
        for search_query in search_queries:
            normalized_query = search_query.strip()
            if not normalized_query or normalized_query in seen_queries:
                continue
            seen_queries.add(normalized_query)

            try:
                hardcover_results = hardcover.search(normalized_query, page)
            except Exception:  # noqa: BLE001
                continue

            if not best_fallback_response and hardcover_results.get("results"):
                best_fallback_response = hardcover_results

            for result in hardcover_results.get("results", [])[:5]:
                media_id = result.get("media_id")
                if not media_id:
                    continue

                try:
                    hardcover_metadata = hardcover.book(media_id)
                except Exception:  # noqa: BLE001
                    continue

                hardcover_isbns = _extract_isbns(hardcover_metadata)
                if target_isbns.intersection(hardcover_isbns):
                    return helpers.format_search_response(
                        1,
                        1,
                        1,
                        [_metadata_to_search_result(hardcover_metadata)],
                    )

                hardcover_title = hardcover_metadata.get("title") or result.get("title")
                title_score = _title_similarity(title, hardcover_title)
                if title_score < 0.88:
                    continue

                candidate_authors = {
                    _normalize_name(author)
                    for author in _extract_author_names(hardcover_metadata)
                    if _normalize_name(author)
                }
                target_authors = {
                    _normalize_name(author)
                    for author in authors
                    if _normalize_name(author)
                }
                if not target_authors or target_authors.intersection(candidate_authors):
                    return helpers.format_search_response(
                        1,
                        1,
                        1,
                        [_metadata_to_search_result(hardcover_metadata)],
                    )

    return best_fallback_response


def _lookup_by_numeric_id(media_type, query, source):  # noqa: PLR0911
    """Return full metadata for a media item identified by a numeric provider ID."""
    n = int(query)
    tv_types = (MediaTypes.TV.value, MediaTypes.SEASON.value, MediaTypes.EPISODE.value)
    if media_type == MediaTypes.MOVIE.value:
        return tmdb.movie(n)
    if media_type in tv_types:
        return tmdb.tv(n) if source == Sources.TMDB.value else tvdb.tv(n)
    if media_type == MediaTypes.ANIME.value:
        if source == Sources.MAL.value:
            return mal.anime(n)
        if source == Sources.TMDB.value:
            return tmdb.tv(n) | {
                "media_type": MediaTypes.ANIME.value,
                "identity_media_type": MediaTypes.TV.value,
                "library_media_type": MediaTypes.ANIME.value,
            }
        return tvdb.tv(n, routed_media_type=MediaTypes.ANIME.value)
    if media_type == MediaTypes.MANGA.value:
        if source == Sources.MANGAUPDATES.value:
            return mangaupdates.manga(query)
        return mal.manga(n)
    if media_type == MediaTypes.GAME.value:
        return igdb.game(n)
    if media_type == MediaTypes.BOOK.value and source == Sources.HARDCOVER.value:
        return hardcover.book(n)
    if media_type == MediaTypes.COMIC.value:
        return comicvine.comic(query)
    if media_type == MediaTypes.BOARDGAME.value:
        return bgg.boardgame(query)
    return None


def search_by_id(media_type, query, source=None):
    """Try to look up a single media item directly by its provider ID.

    Returns a search-format response dict (1 result) when the query matches
    an ID pattern and the provider lookup succeeds, or ``None`` otherwise
    (triggering the normal text-search fallback).
    """
    from app import helpers  # noqa: PLC0415

    query = query.strip()
    source = _resolve_search_source(media_type, source)
    metadata = None

    try:
        if _NUMERIC_ID_RE.match(query):
            if (
                media_type == MediaTypes.BOOK.value
                and source == Sources.HARDCOVER.value
                and (
                    _normalize_isbn_candidate(query) is not None
                    or int(query) > _GRAPHQL_INT_MAX
                )
            ):
                return None
            metadata = _lookup_by_numeric_id(media_type, query, source)
        elif _OL_ID_RE.match(query) and media_type == MediaTypes.BOOK.value:
            metadata = openlibrary.book(query)
        elif _UUID_RE.match(query) and media_type == MediaTypes.MUSIC.value:
            metadata = musicbrainz.recording(query)
    except Exception:  # noqa: BLE001
        return None

    if not metadata or not metadata.get("title"):
        return None

    result = _metadata_to_search_result(metadata)
    return helpers.format_search_response(1, 1, 1, [result])


def search(media_type, query, page, source=None):
    """Search for media based on the query and return the results."""
    source = _resolve_search_source(media_type, source)

    # Attempt direct ID lookup on page 1 only
    if page == 1:
        id_result = search_by_id(media_type, query, source)
        if id_result is not None:
            return id_result

        if (
            media_type == MediaTypes.BOOK.value
            and source != Sources.OPENLIBRARY.value
        ):
            isbn_result = _resolve_hardcover_isbn_search(query, page)
            if isbn_result is not None:
                return isbn_result

    search_handlers = {
        MediaTypes.MANGA.value: lambda: (
            mangaupdates.search(query, page)
            if source == Sources.MANGAUPDATES.value
            else mal.search(media_type, query, page)
        ),
        MediaTypes.ANIME.value: lambda: (
            mal.search(media_type, query, page)
            if source == Sources.MAL.value
            else _annotate_grouped_anime_results(
                tmdb.search(MediaTypes.TV.value, query, page),
                source=Sources.TMDB.value,
            )
            if source == Sources.TMDB.value
            else tvdb.search(media_type, query, page)
        ),
        MediaTypes.TV.value: lambda: (
            tmdb.search(media_type, query, page)
            if source == Sources.TMDB.value
            else tvdb.search(media_type, query, page)
        ),
        MediaTypes.MOVIE.value: lambda: tmdb.search(media_type, query, page),
        MediaTypes.SEASON.value: lambda: (
            tmdb.search(MediaTypes.TV.value, query, page)
            if source == Sources.TMDB.value
            else tvdb.search(MediaTypes.TV.value, query, page)
        ),
        MediaTypes.EPISODE.value: lambda: (
            tmdb.search(MediaTypes.TV.value, query, page)
            if source == Sources.TMDB.value
            else tvdb.search(MediaTypes.TV.value, query, page)
        ),
        MediaTypes.GAME.value: lambda: igdb.search(query, page),
        MediaTypes.BOOK.value: lambda: (
            openlibrary.search(query, page)
            if source == Sources.OPENLIBRARY.value
            else hardcover.search(query, page)
        ),
        MediaTypes.COMIC.value: lambda: comicvine.search(query, page),
        MediaTypes.BOARDGAME.value: lambda: bgg.search(query, page),
        MediaTypes.MUSIC.value: lambda: musicbrainz.search_combined(query, page),
        MediaTypes.PODCAST.value: lambda: (
            pocketcasts.search(query, page)
            if source == Sources.POCKETCASTS.value
            else None
        ),
    }
    response = search_handlers[media_type]()

    if response is None:
        # Return empty results for non-pocketcasts podcast sources.
        from app import helpers

        return helpers.format_search_response(page, settings.PER_PAGE, 0, [])
    return response


def _annotate_grouped_anime_results(response, *, source):
    """Normalize grouped-provider anime search results to anime library metadata."""
    response = dict(response or {})
    results = []
    for row in response.get("results", []):
        normalized = dict(row)
        normalized["media_type"] = MediaTypes.ANIME.value
        normalized["identity_media_type"] = MediaTypes.TV.value
        normalized["library_media_type"] = MediaTypes.ANIME.value
        normalized["source"] = source
        results.append(normalized)
    response["results"] = results
    return response
