import logging

import requests
from django.conf import settings
from django.core.cache import cache

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services

logger = logging.getLogger(__name__)
base_url = "https://api.themoviedb.org/3"
base_params = {
    "api_key": settings.TMDB_API,
    "language": settings.TMDB_LANG,
}


def handle_error(error):
    """Handle TMDB API errors."""
    error_resp = error.response
    status_code = error_resp.status_code

    try:
        error_json = error_resp.json()
    except requests.exceptions.JSONDecodeError as json_error:
        logger.exception("Failed to decode JSON response")
        raise services.ProviderAPIError(Sources.TMDB.value, error) from json_error

    # Handle authentication errors
    if status_code == requests.codes.unauthorized:
        details = error_json.get("status_message")
        if details:
            # Remove trailing period if present
            details = details.rstrip(".")
            raise services.ProviderAPIError(Sources.TMDB.value, error, details)

    raise services.ProviderAPIError(
        Sources.TMDB.value,
        error,
    )


def get_external_links(external_ids):
    """Build external links dictionary from TMDB external_ids response."""
    links = {}

    if external_ids.get("imdb_id"):
        links["IMDb"] = f"https://www.imdb.com/title/{external_ids['imdb_id']}/"

    if external_ids.get("tvdb_id"):
        links["TVDB"] = (
            f"https://www.thetvdb.com/dereferrer/series/{external_ids['tvdb_id']}"
        )

    if external_ids.get("wikidata_id"):
        links["Wikidata"] = (
            f"https://www.wikidata.org/wiki/{external_ids['wikidata_id']}"
        )

    return links


def search(media_type, query, page):
    """Search for media on TMDB."""
    cache_key = f"search_{Sources.TMDB.value}_{media_type}_{query}_{page}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/search/{media_type}"

        params = {
            **base_params,
            "query": query,
            "page": page,
        }

        if settings.TMDB_NSFW:
            params["include_adult"] = "true"

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        results = [
            {
                "media_id": media["id"],
                "source": Sources.TMDB.value,
                "media_type": media_type,
                "title": get_title(media),
                "image": get_image_url(media["poster_path"]),
                "year": get_year(media),
            }
            for media in response["results"]
        ]

        total_results = response["total_results"]
        per_page = 20  # TMDB always returns 20 results per page
        data = helpers.format_search_response(
            page,
            per_page,
            total_results,
            results,
        )

        cache.set(cache_key, data)

    return data


def find(external_id, external_source):
    """Search for media on TMDB."""
    cache_key = f"find_{Sources.TMDB.value}_{external_id}_{external_source}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/find/{external_id}"

        params = {
            **base_params,
            "external_source": external_source,
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        cache.set(cache_key, response)
        return response

    return data


def movie(media_id):
    """Return the metadata for the selected movie from The Movie Database."""
    cache_key = f"{Sources.TMDB.value}_{MediaTypes.MOVIE.value}_{media_id}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/movie/{media_id}"
        params = {
            **base_params,
            "append_to_response": "recommendations,external_ids,credits",
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
            if response.get("belongs_to_collection", {}) is not None and (
                collection_id := response.get("belongs_to_collection", {}).get("id")
            ):
                collection_response = services.api_request(
                    Sources.TMDB.value,
                    "GET",
                    f"{base_url}/collection/{collection_id}",
                    params={**base_params},
                )
            else:
                collection_response = {}
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        # Filter out collection items from recommendations, to avoid duplicates
        collection_items = get_collection(collection_response)
        collection_ids = [item["media_id"] for item in collection_items]
        recommended_items = response.get("recommendations", {}).get("results", [])
        filtered_recommendations = [
            item for item in recommended_items if item["id"] not in collection_ids
        ]
        data = {
            "media_id": media_id,
            "source": Sources.TMDB.value,
            "source_url": f"https://www.themoviedb.org/movie/{media_id}",
            "media_type": MediaTypes.MOVIE.value,
            "title": response["title"],
            "max_progress": 1,
            "image": get_image_url(response["poster_path"]),
            "synopsis": get_synopsis(response["overview"]),
            "genres": get_genres(response["genres"]),
            "score": get_score(response["vote_average"]),
            "score_count": response["vote_count"],
            "details": {
                "format": "Movie",
                "release_date": get_start_date(response["release_date"]),
                "status": response["status"],
                "runtime": get_readable_duration(response["runtime"]),
                "studios": get_companies(response["production_companies"]),
                "country": get_country(response["production_countries"]),
                "languages": get_languages(response["spoken_languages"]),
            },
            "cast": get_cast_credits(response.get("credits", {})),
            "crew": get_crew_credits(response.get("credits", {})),
            "studios_full": get_companies_full(response.get("production_companies")),
            "related": {
                collection_response.get("name", "collection"): collection_items,
                "recommendations": get_related(
                    filtered_recommendations[:15],
                    MediaTypes.MOVIE.value,
                ),
            },
            "external_links": get_external_links(response.get("external_ids", {})),
        }

        cache.set(cache_key, data)

    return data


def get_cached_seasons(media_id, season_numbers):
    """Check cache for seasons and return cached data and list of uncached seasons."""
    cached_data = {}
    uncached_seasons = []

    for season_number in season_numbers:
        season_cache_key = (
            f"{Sources.TMDB.value}_{MediaTypes.SEASON.value}_{media_id}_{season_number}"
        )
        season_data = cache.get(season_cache_key)
        if season_data:
            cached_data[f"season/{season_number}"] = season_data
        else:
            uncached_seasons.append(season_number)

    return cached_data, uncached_seasons


def enrich_season_with_tv_data(season_data, tv_data, media_id, season_number):
    """Add TV show metadata to season metadata."""
    from django.conf import settings

    season_data["media_id"] = media_id
    season_data["source_url"] = (
        f"https://www.themoviedb.org/tv/{media_id}/season/{season_number}"
    )
    season_data["title"] = tv_data["title"]
    season_data["tvdb_id"] = tv_data["tvdb_id"]
    season_data["external_links"] = tv_data["external_links"]
    season_data["genres"] = tv_data["genres"]
    if season_data["synopsis"] == "No synopsis available.":
        season_data["synopsis"] = tv_data["synopsis"]
    # Use TV show poster as fallback if season doesn't have its own poster
    # Check if image is None, empty, or the default placeholder
    season_image = season_data.get("image")
    if not season_image or season_image == settings.IMG_NONE:
        season_data["image"] = tv_data.get("image")
    return season_data


def fetch_and_cache_seasons(media_id, season_numbers, tv_data):
    """Fetch uncached seasons from API and cache them."""
    url = f"{base_url}/tv/{media_id}"
    base_append = "recommendations,external_ids,aggregate_credits"
    max_seasons_per_request = 18
    fetched_tv_data = tv_data
    result_data = {}

    for i in range(0, len(season_numbers), max_seasons_per_request):
        season_subset = season_numbers[i : i + max_seasons_per_request]
        append_text = ",".join([f"season/{season}" for season in season_subset])

        params = {
            **base_params,
            "append_to_response": f"{base_append},{append_text}",
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        # Cache TV metadata if we haven't fetched it yet
        if fetched_tv_data is None:
            fetched_tv_data = process_tv(response)
            tv_cache_key = f"{Sources.TMDB.value}_{MediaTypes.TV.value}_{media_id}"
            cache.set(tv_cache_key, fetched_tv_data)

        # Process and cache each season
        for season_number in season_subset:
            season_key = f"season/{season_number}"
            if season_key not in response:
                logger.warning(
                    "Season %s not found in %s with ID %s; skipping season",
                    season_number,
                    Sources.TMDB.label,
                    media_id,
                )
                continue

            season_data = process_season(response[season_key])

            season_data = enrich_season_with_tv_data(
                season_data,
                fetched_tv_data,
                media_id,
                season_number,
            )
            cache.set(
                f"{Sources.TMDB.value}_{MediaTypes.SEASON.value}_{media_id}_{season_number}",
                season_data,
            )
            result_data[season_key] = season_data

    return result_data, fetched_tv_data


def tv_with_seasons(media_id, season_numbers):
    """Return the metadata for the tv show with seasons appended to the response."""
    if not season_numbers:
        return tv(media_id)

    tv_cache_key = f"{Sources.TMDB.value}_{MediaTypes.TV.value}_{media_id}"
    tv_data = cache.get(tv_cache_key)

    cached_seasons, uncached_seasons = get_cached_seasons(media_id, season_numbers)

    if tv_data is None and not uncached_seasons:
        tv_data = tv(media_id)

    if uncached_seasons:
        fetched_seasons, fetched_tv_data = fetch_and_cache_seasons(
            media_id,
            uncached_seasons,
            tv_data,
        )

        if tv_data is None:
            tv_data = fetched_tv_data

        cached_seasons.update(fetched_seasons)

    return tv_data | cached_seasons


def tv(media_id):
    """Return the metadata for the selected tv show from The Movie Database."""
    cache_key = f"{Sources.TMDB.value}_{MediaTypes.TV.value}_{media_id}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/tv/{media_id}"
        params = {
            **base_params,
            "append_to_response": "recommendations,external_ids,aggregate_credits",
        }

        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        data = process_tv(response)
        cache.set(cache_key, data)

    return data


def process_tv(response):
    """Process the metadata for the selected tv show from The Movie Database."""
    num_episodes = response["number_of_episodes"]
    next_episode = response.get("next_episode_to_air")
    last_episode = response.get("last_episode_to_air")
    return {
        "media_id": response["id"],
        "source": Sources.TMDB.value,
        "source_url": f"https://www.themoviedb.org/tv/{response['id']}",
        "media_type": MediaTypes.TV.value,
        "title": response["name"],
        "max_progress": num_episodes,
        "image": get_image_url(response["poster_path"]),
        "synopsis": get_synopsis(response["overview"]),
        "genres": get_genres(response["genres"]),
        "score": get_score(response["vote_average"]),
        "score_count": response["vote_count"],
        "details": {
            "format": "TV",
            "first_air_date": get_start_date(response["first_air_date"]),
            "last_air_date": get_start_date(response["last_air_date"]),
            "status": response["status"],
            "seasons": response["number_of_seasons"],
            "episodes": num_episodes,
            "runtime": get_runtime_tv(response["episode_run_time"]),
            "studios": get_companies(response["production_companies"]),
            "country": get_country(response["production_countries"]),
            "languages": get_languages(response["spoken_languages"]),
        },
        "cast": get_cast_credits(
            response.get("aggregate_credits", {}),
            is_aggregate=True,
        ),
        "crew": get_crew_credits(
            response.get("aggregate_credits", {}),
            is_aggregate=True,
        ),
        "studios_full": get_companies_full(response.get("production_companies")),
        "related": {
            "seasons": get_related(
                response["seasons"],
                MediaTypes.SEASON.value,
                response,
            ),
            "recommendations": get_related(
                response.get("recommendations", {}).get("results", [])[:15],
                MediaTypes.TV.value,
            ),
        },
        "tvdb_id": response.get("external_ids", {}).get("tvdb_id"),
        "external_links": get_external_links(response.get("external_ids", {})),
        "last_episode_season": last_episode["season_number"] if last_episode else None,
        "next_episode_season": next_episode["season_number"] if next_episode else None,
    }


def process_season(response):
    """Process the metadata for the selected season from The Movie Database."""
    episodes = response["episodes"]
    num_episodes = len(episodes)

    runtimes = []
    total_runtime = 0
    score_count = 0

    for episode in episodes:
        if episode["runtime"] is not None:
            runtimes.append(episode["runtime"])
            total_runtime += episode["runtime"]
        score_count += episode["vote_count"]

    avg_runtime = (
        get_readable_duration(sum(runtimes) / len(runtimes)) if runtimes else None
    )
    total_runtime = get_readable_duration(total_runtime) if total_runtime else None

    return {
        "source": Sources.TMDB.value,
        "media_type": MediaTypes.SEASON.value,
        "season_title": response["name"],
        "max_progress": episodes[-1]["episode_number"] if episodes else 0,
        "image": get_image_url(response["poster_path"]),
        "season_number": response["season_number"],
        "synopsis": get_synopsis(response["overview"]),
        "score": get_score(response["vote_average"]),
        "score_count": score_count,
        "details": {
            "first_air_date": get_start_date(response["air_date"]),
            "last_air_date": get_end_date(response),
            "episodes": num_episodes,
            "runtime": avg_runtime,
            "total_runtime": total_runtime,
        },
        "episodes": response["episodes"],
    }


def get_format(media_type):
    """Return media_type capitalized."""
    if media_type == MediaTypes.TV.value:
        return "TV"
    return "Movie"


def get_image_url(path):
    """Return the image URL for the media."""
    # when no image, value from response is null
    # e.g movie: 445290
    if path:
        return f"https://image.tmdb.org/t/p/w500{path}"
    return settings.IMG_NONE


def get_title(response):
    """Return the title for the media."""
    # tv shows have name instead of title
    try:
        return response["title"]
    except KeyError:
        return response["name"]


def get_year(media):
    """Extract a release or first air year from a TMDB search result."""
    date_value = media.get("release_date") or media.get("first_air_date")
    if not date_value:
        return None

    try:
        return int(str(date_value).split("-")[0])
    except (TypeError, ValueError):
        return None


def get_start_date(date):
    """Return the start date for the media."""
    # when unknown date, value from response is empty string
    # e.g movie: 445290
    if date == "" or not date:
        return None

    try:
        from datetime import datetime

        from django.utils import timezone

        # TMDB returns dates in YYYY-MM-DD format
        if isinstance(date, str):
            # Parse the date string and convert to timezone-aware datetime
            date_obj = datetime.strptime(date, "%Y-%m-%d")
            return timezone.make_aware(date_obj, timezone.get_current_timezone())

        return date
    except (ValueError, TypeError):
        # If parsing fails, return the original value
        return date


def get_end_date(response):
    """Return the last air date for the season."""
    if response["episodes"]:
        last_episode_date = response["episodes"][-1]["air_date"]
        if last_episode_date:
            try:
                from datetime import datetime

                from django.utils import timezone

                # TMDB returns dates in YYYY-MM-DD format
                date_obj = datetime.strptime(last_episode_date, "%Y-%m-%d")
                return timezone.make_aware(date_obj, timezone.get_current_timezone())
            except (ValueError, TypeError):
                # If parsing fails, return the original value
                return last_episode_date

        return last_episode_date

    return None


def get_synopsis(text):
    """Return the synopsis for the media."""
    # when unknown synopsis, value from response is empty string
    # e.g movie: 445290
    if text == "":
        return "No synopsis available."
    return text


def get_readable_duration(duration):
    """Convert duration in minutes to a readable format."""
    # if unknown movie runtime, value from response is 0
    # e.g movie: 274613
    if duration:
        hours, minutes = divmod(int(duration), 60)
        return f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
    return None


def get_runtime_tv(runtime):
    """Return the runtime for the tv show."""
    # when unknown runtime, value from response is empty list
    # e.g: tv:66672
    if runtime:
        return get_readable_duration(runtime[0])
    return None


def season_scores_count(response):
    """Return the scores count for the season."""
    return sum(episode["vote_count"] for episode in response["episodes"])


def get_genres(genres):
    """Return the genres for the media."""
    # when unknown genres, value from response is empty list
    # e.g tv: 24795
    if genres:
        return [genre["name"] for genre in genres]
    return None


def get_country(countries):
    """Return the production country for the media."""
    # when unknown production country, value from response is empty list
    # e.g tv: 24795
    if countries:
        return countries[0]["name"]
    return None


def get_languages(languages):
    """Return the languages for the media."""
    # when unknown spoken languages, value from response is empty list
    # e.g tv: 24795
    if languages:
        return [language["english_name"] for language in languages]
    return None


def get_companies(companies):
    """Return the production companies for the media."""
    # when unknown production companies, value from response is empty list
    # e.g tv: 24795
    if companies:
        return [company["name"] for company in companies[:3]]
    return None


def get_profile_image_url(path):
    """Return a profile image URL for cast/crew members."""
    if path:
        return f"https://image.tmdb.org/t/p/w185{path}"
    return settings.IMG_NONE


def get_gender(value):
    """Normalize TMDB gender integer into a stable string."""
    if value == 1:
        return "female"
    if value == 2:
        return "male"
    if value == 3:
        return "non_binary"
    return "unknown"


def get_cast_credits(credits_data, is_aggregate=False):
    """Return normalized cast entries."""
    cast_entries = []
    cast_list = credits_data.get("cast", []) if isinstance(credits_data, dict) else []

    for cast in cast_list:
        role_value = cast.get("character", "")
        if is_aggregate:
            roles = cast.get("roles", []) or []
            if roles:
                top_role = max(roles, key=lambda role: role.get("episode_count") or 0)
                role_value = top_role.get("character") or role_value

        cast_entries.append(
            {
                "person_id": str(cast.get("id")),
                "name": cast.get("name", ""),
                "image": get_profile_image_url(cast.get("profile_path")),
                "known_for_department": cast.get("known_for_department", ""),
                "gender": get_gender(cast.get("gender")),
                "department": cast.get("known_for_department", "Acting"),
                "role": role_value or "",
                "order": cast.get("order"),
            },
        )

    cast_entries.sort(key=lambda row: (row.get("order") is None, row.get("order") or 999999))
    return cast_entries


def get_crew_credits(credits_data, is_aggregate=False):
    """Return normalized crew entries."""
    crew_entries = []
    crew_list = credits_data.get("crew", []) if isinstance(credits_data, dict) else []

    for crew in crew_list:
        department = crew.get("department", "")

        if is_aggregate:
            jobs = crew.get("jobs", []) or []
            if not jobs:
                crew_entries.append(
                    {
                        "person_id": str(crew.get("id")),
                        "name": crew.get("name", ""),
                        "image": get_profile_image_url(crew.get("profile_path")),
                        "known_for_department": crew.get("known_for_department", ""),
                        "gender": get_gender(crew.get("gender")),
                        "department": department,
                        "role": "",
                        "order": crew.get("order"),
                    },
                )
                continue

            seen_jobs = set()
            for job_data in jobs:
                job_name = (job_data.get("job") or "").strip()
                if not job_name or job_name.lower() in seen_jobs:
                    continue
                seen_jobs.add(job_name.lower())
                crew_entries.append(
                    {
                        "person_id": str(crew.get("id")),
                        "name": crew.get("name", ""),
                        "image": get_profile_image_url(crew.get("profile_path")),
                        "known_for_department": crew.get("known_for_department", ""),
                        "gender": get_gender(crew.get("gender")),
                        "department": department or job_data.get("department", ""),
                        "role": job_name,
                        "order": crew.get("order"),
                    },
                )
            continue

        crew_entries.append(
            {
                "person_id": str(crew.get("id")),
                "name": crew.get("name", ""),
                "image": get_profile_image_url(crew.get("profile_path")),
                "known_for_department": crew.get("known_for_department", ""),
                "gender": get_gender(crew.get("gender")),
                "department": department,
                "role": crew.get("job", "") or "",
                "order": crew.get("order"),
            },
        )

    crew_entries.sort(
        key=lambda row: (
            row.get("department", "").lower(),
            row.get("order") is None,
            row.get("order") or 999999,
        ),
    )
    return crew_entries


def get_companies_full(companies):
    """Return normalized studio/company entries."""
    studios = []
    for company in companies or []:
        studios.append(
            {
                "studio_id": str(company.get("id")),
                "name": company.get("name", ""),
                "logo": get_profile_image_url(company.get("logo_path")),
                "origin_country": company.get("origin_country", ""),
            },
        )
    studios.sort(key=lambda row: row.get("name", "").lower())
    return studios


def get_score(score):
    """Return the score for the media with one decimal place."""
    # when unknown score, value from response is 0.0

    return round(score, 1)


def get_related(related_medias, media_type, parent_response=None):
    """Return list of related media for the selected media."""
    related = []
    for media in related_medias:
        # For seasons, use TV show poster as fallback if season doesn't have its own poster
        if media_type == MediaTypes.SEASON.value and parent_response:
            season_poster_path = media.get("poster_path")
            # If season doesn't have a poster, use TV show poster as fallback
            if season_poster_path:
                season_image = get_image_url(season_poster_path)
            else:
                season_image = get_image_url(parent_response.get("poster_path"))
        else:
            season_image = get_image_url(media["poster_path"])

        data = {
            "source": Sources.TMDB.value,
            "media_type": media_type,
            "image": season_image,
        }
        if media_type == MediaTypes.SEASON.value:
            data["media_id"] = parent_response["id"]
            data["title"] = parent_response["name"]
            data["season_number"] = media["season_number"]
            data["season_title"] = media["name"]
            # Use the same date processing logic as process_season for consistency
            data["first_air_date"] = get_start_date(media["air_date"])
            # For last_air_date, we need to simulate get_end_date logic since we don't have episode data here
            # This will be updated when the detailed season data is fetched
            data["last_air_date"] = None
            data["max_progress"] = media["episode_count"]
        else:
            data["media_id"] = media["id"]
            data["title"] = get_title(media)
            data["year"] = get_year(media)
        related.append(data)
    return related


def get_collection(collection_response):
    """Format media collection list to match related media."""
    def date_key(media):
        date = media.get("release_date", "")
        if date is None or date == "":
            # If release date is unknown, sort by title after known releases
            title = get_title(media)
            date = f"9999-99-99-{title}"
        return date

    parts = sorted(collection_response.get("parts", []), key=date_key)
    return [
        {
            "source": Sources.TMDB.value,
            "media_type": MediaTypes.MOVIE.value,
            "image": get_image_url(media["poster_path"]),
            "media_id": media["id"],
            "title": get_title(media),
            "year": get_year(media),
        }
        for media in parts
    ]


def _person_filmography_entries(combined_credits):
    """Normalize cast and crew filmography entries."""
    entries = []
    cast = combined_credits.get("cast", []) if isinstance(combined_credits, dict) else []
    crew = combined_credits.get("crew", []) if isinstance(combined_credits, dict) else []

    for media in cast:
        media_type = media.get("media_type")
        if media_type not in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
            continue
        entries.append(
            {
                "media_id": str(media.get("id")),
                "source": Sources.TMDB.value,
                "media_type": media_type,
                "title": get_title(media),
                "image": get_image_url(media.get("poster_path")),
                "year": get_year(media),
                "release_date": get_start_date(
                    media.get("release_date") or media.get("first_air_date"),
                ),
                "credit_type": "cast",
                "role": media.get("character") or "",
                "department": "Acting",
            },
        )

    for media in crew:
        media_type = media.get("media_type")
        if media_type not in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
            continue
        entries.append(
            {
                "media_id": str(media.get("id")),
                "source": Sources.TMDB.value,
                "media_type": media_type,
                "title": get_title(media),
                "image": get_image_url(media.get("poster_path")),
                "year": get_year(media),
                "release_date": get_start_date(
                    media.get("release_date") or media.get("first_air_date"),
                ),
                "credit_type": "crew",
                "role": media.get("job") or "",
                "department": media.get("department") or "",
            },
        )

    # Deduplicate by media + credit + role in case TMDB returns duplicates.
    deduped = {}
    for entry in entries:
        key = (
            entry["media_type"],
            entry["media_id"],
            entry["credit_type"],
            entry["role"],
        )
        if key not in deduped:
            deduped[key] = entry

    filmography = list(deduped.values())
    filmography.sort(
        key=lambda entry: (
            entry.get("release_date") is None,
            entry.get("release_date") or 0,
            entry.get("year") or 0,
        ),
        reverse=True,
    )
    return filmography


def person(person_id):
    """Return metadata for a TMDB person profile."""
    cache_key = f"{Sources.TMDB.value}_person_{person_id}"
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/person/{person_id}"
        params = {
            **base_params,
            "append_to_response": "combined_credits,external_ids",
        }
        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        data = {
            "person_id": str(response.get("id")),
            "source": Sources.TMDB.value,
            "name": response.get("name", ""),
            "image": get_profile_image_url(response.get("profile_path")),
            "biography": response.get("biography") or "",
            "known_for_department": response.get("known_for_department") or "",
            "gender": get_gender(response.get("gender")),
            "birth_date": response.get("birthday"),
            "death_date": response.get("deathday"),
            "place_of_birth": response.get("place_of_birth") or "",
            "filmography": _person_filmography_entries(
                response.get("combined_credits", {}),
            ),
        }

        cache.set(cache_key, data)

    return data


def process_episodes(season_metadata, episodes_in_db):
    """Process the episodes for the selected season."""
    episodes_metadata = []

    # Convert the queryset to a dictionary for efficient lookups
    tracked_episodes = {}
    for ep in episodes_in_db:
        episode_number = ep.item.episode_number
        if episode_number not in tracked_episodes:
            tracked_episodes[episode_number] = []
        tracked_episodes[episode_number].append(ep)

    for episode in season_metadata["episodes"]:
        episode_number = episode["episode_number"]

        # Convert air_date to datetime object if it's a string
        air_date = episode["air_date"]
        if air_date and isinstance(air_date, str):
            try:
                from datetime import datetime

                from django.utils import timezone

                # TMDB returns dates in YYYY-MM-DD format
                date_obj = datetime.strptime(air_date, "%Y-%m-%d")
                air_date = timezone.make_aware(date_obj, timezone.get_current_timezone())
            except (ValueError, TypeError):
                # If parsing fails, keep the original value
                pass

        episodes_metadata.append(
            {
                "media_id": season_metadata["media_id"],
                "media_type": MediaTypes.EPISODE.value,
                "source": Sources.TMDB.value,
                "season_number": season_metadata["season_number"],
                "episode_number": episode_number,
                "air_date": air_date,  # when unknown, response returns null
                "image": get_image_url(episode["still_path"]),
                "title": episode["name"],
                "overview": episode["overview"],
                "history": tracked_episodes.get(episode_number, []),
                "runtime": get_readable_duration(episode["runtime"]),
            },
        )
    return episodes_metadata


def find_next_episode(episode_number, episodes_metadata):
    """Find the next episode number."""
    # Find the current episode in the sorted list
    current_episode_index = None
    for index, episode in enumerate(episodes_metadata):
        if episode["episode_number"] == episode_number:
            current_episode_index = index
            break

    # If episode not found or it's the last episode, return None
    if current_episode_index is None or current_episode_index + 1 >= len(
        episodes_metadata,
    ):
        return None

    # Return the next episode number
    return episodes_metadata[current_episode_index + 1]["episode_number"]


def episode(media_id, season_number, episode_number):
    """Return the metadata for the selected episode from The Movie Database."""
    cache_key = (
        f"{Sources.TMDB.value}_{MediaTypes.EPISODE.value}_{media_id}_{season_number}_{episode_number}"
    )
    data = cache.get(cache_key)

    if data is None:
        url = f"{base_url}/tv/{media_id}/season/{season_number}/episode/{episode_number}"
        params = {
            **base_params,
            "append_to_response": "credits",
        }
        try:
            response = services.api_request(
                Sources.TMDB.value,
                "GET",
                url,
                params=params,
            )
        except requests.exceptions.HTTPError as error:
            handle_error(error)

        tv_metadata = tv_with_seasons(media_id, [season_number])
        season_metadata = tv_metadata.get(f"season/{season_number}", {})

        # TMDB episode payload exposes both regular cast and guest stars.
        # Prefer guest stars for episode-level crediting to avoid attributing
        # season-regular cast to every episode when episode-specific guests exist.
        cast_rows = response.get("guest_stars", []) or []
        if not cast_rows:
            cast_rows = response.get("credits", {}).get("cast", []) or []

        crew_rows = response.get("credits", {}).get("crew", [])
        if not crew_rows:
            crew_rows = response.get("crew", []) or []

        data = {
            "title": season_metadata.get("title") or tv_metadata.get("title") or "",
            "season_title": season_metadata.get("season_title") or f"Season {season_number}",
            "episode_title": response.get("name") or f"Episode {episode_number}",
            "image": get_image_url(response.get("still_path")),
            "cast": get_cast_credits({"cast": cast_rows}),
            "crew": get_crew_credits({"crew": crew_rows}),
        }
        cache.set(cache_key, data)

    return data
