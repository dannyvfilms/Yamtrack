import logging
import re
import time

import requests
from defusedxml import ElementTree
from django.conf import settings
from pyrate_limiter import RedisBucket
from redis import ConnectionPool
from requests.adapters import HTTPAdapter
from requests_ratelimiter import LimiterAdapter, LimiterSession

from app.models import MediaTypes, Sources
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
    tmdb,
)

logger = logging.getLogger(__name__)


def get_redis_connection():
    """Return a Redis connection pool."""
    if settings.TESTING:
        import fakeredis  # noqa: PLC0415

        return fakeredis.FakeStrictRedis().connection_pool
    return ConnectionPool.from_url(settings.REDIS_URL)


redis_pool = get_redis_connection()

REDIS_PREFIX = getattr(settings, "REDIS_PREFIX", None)
bucket_name = f"{REDIS_PREFIX}_api" if REDIS_PREFIX else "api"

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
    "https://api.tvmaze.com",
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

        if self.status_code == 404:
            logger.warning("%s error: %s", provider, error.response.text)
        else:
            logger.error("%s error: %s", provider, error.response.text)

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
    logger.error("%s: %s", provider, error_msg)

    # Create a mock 404 error response
    mock_response = type(
        "obj",
        (object,),
        {
            "status_code": 404,
            "text": error_msg,
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

    metadata.setdefault("original_title", None)
    metadata.setdefault("localized_title", None)

    if not metadata.get("title"):
        metadata["title"] = (
            metadata.get("localized_title")
            or metadata.get("original_title")
            or ""
        )

    if not metadata.get("localized_title") and metadata.get("title"):
        metadata["localized_title"] = metadata["title"]

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

    metadata_retrievers = {
        MediaTypes.ANIME.value: lambda: mal.anime(media_id),
        MediaTypes.MANGA.value: lambda: (
            mangaupdates.manga(media_id)
            if source == Sources.MANGAUPDATES.value
            else mal.manga(media_id)
        ),
        MediaTypes.TV.value: lambda: tmdb.tv(media_id),
        "tv_with_seasons": lambda: tmdb.tv_with_seasons(media_id, season_numbers),
        MediaTypes.SEASON.value: tmdb_season_metadata,
        MediaTypes.EPISODE.value: lambda: tmdb.episode(
            media_id,
            season_numbers[0],
            episode_number,
        ),
        MediaTypes.MOVIE.value: lambda: tmdb.movie(media_id),
        MediaTypes.GAME.value: lambda: igdb.game(media_id),
        MediaTypes.BOOK.value: lambda: (
            hardcover.book(media_id)
            if source == Sources.HARDCOVER.value
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
            logger.debug("Music metadata lookup failed for %s: %s", media_id, exc)
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


def _lookup_by_numeric_id(media_type, query, source):  # noqa: PLR0911
    """Return full metadata for a media item identified by a numeric provider ID."""
    n = int(query)
    tv_types = (MediaTypes.TV.value, MediaTypes.SEASON.value, MediaTypes.EPISODE.value)
    if media_type == MediaTypes.MOVIE.value:
        return tmdb.movie(n)
    if media_type in tv_types:
        return tmdb.tv(n)
    if media_type == MediaTypes.ANIME.value:
        return mal.anime(n)
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
    metadata = None

    try:
        if _NUMERIC_ID_RE.match(query):
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
    # Attempt direct ID lookup on page 1 only
    if page == 1:
        id_result = search_by_id(media_type, query, source)
        if id_result is not None:
            return id_result

    search_handlers = {
        MediaTypes.MANGA.value: lambda: (
            mangaupdates.search(query, page)
            if source == Sources.MANGAUPDATES.value
            else mal.search(media_type, query, page)
        ),
        MediaTypes.ANIME.value: lambda: mal.search(media_type, query, page),
        MediaTypes.TV.value: lambda: tmdb.search(media_type, query, page),
        MediaTypes.MOVIE.value: lambda: tmdb.search(media_type, query, page),
        MediaTypes.SEASON.value: lambda: tmdb.search(MediaTypes.TV.value, query, page),
        MediaTypes.EPISODE.value: lambda: tmdb.search(MediaTypes.TV.value, query, page),
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
