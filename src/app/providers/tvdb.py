"""TVDB metadata provider."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from app import helpers
from app.models import MediaTypes, Sources
from app.providers import services, tmdb

logger = logging.getLogger(__name__)

base_url = "https://api4.thetvdb.com/v4"
TVDB_CACHE_NAMESPACE = f"{Sources.TVDB.value}_v3"
TOKEN_CACHE_KEY = f"{TVDB_CACHE_NAMESPACE}_access_token"
TOKEN_CACHE_TIMEOUT = 60 * 60 * 12
PREFERRED_TRANSLATION_CODES = ("eng", "en", "eng-us", "en-us")


def _cache_key(*parts: object) -> str:
    """Return a versioned TVDB cache key."""
    return "_".join([TVDB_CACHE_NAMESPACE, *[str(part) for part in parts]])


def enabled() -> bool:
    """Return whether TVDB is configured."""
    return bool(settings.TVDB_API_KEY)


def handle_error(error):
    """Handle TVDB API errors."""
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code == requests.codes.unauthorized:
        cache.delete(TOKEN_CACHE_KEY)
    raise services.ProviderAPIError(Sources.TVDB.value, error)


def _unwrap_data(payload: Any):
    """Return the data envelope payload when present."""
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _coerce_list(value) -> list:
    """Normalize a scalar-or-list response into a list."""
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _normalize_text_value(value) -> str | None:
    """Collapse provider text payloads into a displayable string."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, dict):
        for key in (
            "name",
            "title",
            "value",
            "text",
            "overview",
            "overviewText",
            "originalName",
            "seriesName",
            "episodeName",
        ):
            normalized = _normalize_text_value(value.get(key))
            if normalized:
                return normalized
        return None
    if isinstance(value, list):
        for entry in value:
            normalized = _normalize_text_value(entry)
            if normalized:
                return normalized
        return None

    text = str(value).strip()
    return text or None


def _normalize_language_code(value) -> str:
    """Return a normalized language code string."""
    if not value:
        return ""
    code = str(value).strip().lower().replace("_", "-")
    if code.startswith("eng"):
        return "eng"
    if code.startswith("en"):
        return "en"
    if code == "english":
        return "eng"
    return code


def _is_preferred_translation_code(code: str) -> bool:
    """Return whether a language code matches the preferred UI locale."""
    return _normalize_language_code(code) in PREFERRED_TRANSLATION_CODES


def _translation_language(entry: dict | None) -> str:
    """Return the language code attached to a translation row."""
    if not isinstance(entry, dict):
        return ""

    for key in (
        "language",
        "languageCode",
        "lang",
        "locale",
        "abbreviation",
        "twoLetterCode",
        "threeLetterCode",
        "iso6391",
        "iso6392",
        "iso_639_1",
        "iso_639_2",
    ):
        raw_value = entry.get(key)
        if isinstance(raw_value, dict):
            raw_value = (
                raw_value.get("code")
                or raw_value.get("abbreviation")
                or raw_value.get("name")
            )
        normalized = _normalize_language_code(raw_value)
        if normalized:
            return normalized

    return ""


def _translation_entry_value(entry: Any, key: str) -> str | None:
    """Return a translated field value from a translation row."""
    if not isinstance(entry, dict):
        return _normalize_text_value(entry)

    candidate_keys = [key]
    if key == "name":
        candidate_keys.extend(
            ["title", "value", "text", "seriesName", "episodeName", "seasonName"],
        )
    elif key == "overview":
        candidate_keys.extend(["overviewText", "text", "value", "description"])
    else:
        candidate_keys.extend(["value", "text"])

    for candidate in candidate_keys:
        normalized = _normalize_text_value(entry.get(candidate))
        if normalized:
            return normalized

    return None


def _pick_preferred_translation(entries, key: str) -> str | None:
    """Return the preferred translated value from an entry list."""
    fallback = None
    for entry in _coerce_list(entries):
        value = _translation_entry_value(entry, key)
        if not value:
            continue
        if isinstance(entry, dict) and _is_preferred_translation_code(
            _translation_language(entry),
        ):
            return value
        if fallback is None:
            fallback = value
    return fallback


def _get_token() -> str:
    """Return a cached TVDB bearer token."""
    token = cache.get(TOKEN_CACHE_KEY)
    if token:
        return token

    payload = {"apikey": settings.TVDB_API_KEY}
    if settings.TVDB_PIN:
        payload["pin"] = settings.TVDB_PIN

    try:
        response = services.api_request(
            Sources.TVDB.value,
            "POST",
            f"{base_url}/login",
            params=payload,
            headers={"Content-Type": "application/json"},
        )
    except requests.exceptions.HTTPError as error:
        handle_error(error)

    token = (_unwrap_data(response) or {}).get("token")
    if not token:
        msg = "TVDB login did not return a token"
        raise ValueError(msg)

    cache.set(TOKEN_CACHE_KEY, token, timeout=TOKEN_CACHE_TIMEOUT)
    return token


def _request(path: str, *, params=None, retry: bool = True):
    """Make an authenticated TVDB request."""
    if not enabled():
        msg = "TVDB is not configured"
        raise ValueError(msg)

    try:
        return services.api_request(
            Sources.TVDB.value,
            "GET",
            f"{base_url}/{path.lstrip('/')}",
            params=params,
            headers={
                "Authorization": f"Bearer {_get_token()}",
                "Accept": "application/json",
            },
        )
    except requests.exceptions.HTTPError as error:
        if (
            retry
            and getattr(getattr(error, "response", None), "status_code", None)
            == requests.codes.unauthorized
        ):
            cache.delete(TOKEN_CACHE_KEY)
            return _request(path, params=params, retry=False)
        handle_error(error)


def _parse_date(value):
    """Return a timezone-aware datetime for known TVDB date strings."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)

    value = str(value).strip()
    if not value:
        return None

    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if timezone.is_aware(dt) else timezone.make_aware(dt)
        except ValueError:
            continue

    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return dt if timezone.is_aware(dt) else timezone.make_aware(dt)


def _get_name(row: dict | None) -> str:
    """Return the best available title for a TVDB entity."""
    row = row or {}
    return (
        _normalize_text_value(row.get("name"))
        or _normalize_text_value(row.get("seriesName"))
        or _normalize_text_value(row.get("episodeName"))
        or _normalize_text_value(row.get("title"))
        or ""
    )


def _find_translation(row: dict | None, *keys: str):
    """Return translated text from nested translation payloads."""
    row = row or {}
    translations = row.get("translations") or {}
    if isinstance(translations, dict):
        for lang_key in ("eng", "en", "eng-US"):
            lang_payload = translations.get(lang_key)
            if not isinstance(lang_payload, dict):
                continue
            for key in keys:
                value = _normalize_text_value(lang_payload.get(key))
                if value:
                    return value

        # Some TVDB payloads store translations in arrays keyed by name/overview.
        for key in keys:
            nested = translations.get(key)
            value = _pick_preferred_translation(nested, key)
            if value:
                return value
    return None


def _get_translation(entity_type: str, entity_id: Any, *, language: str = "eng"):
    """Return a cached TVDB translation payload for an entity when available."""
    if not entity_id:
        return {}

    cache_key = _cache_key("translation", entity_type, entity_id, language)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        payload = (
            _unwrap_data(_request(f"{entity_type}/{entity_id}/translations/{language}"))
            or {}
        )
    except services.ProviderAPIError:
        payload = {}

    cache.set(cache_key, payload)
    return payload


def _with_preferred_translation(row: dict | None, entity_type: str):
    """Attach the preferred translation payload to a TVDB entity."""
    row = row or {}
    entity_id = row.get("id")
    if not entity_id:
        return row

    translation = _get_translation(entity_type, entity_id)
    if not isinstance(translation, dict) or not translation:
        return row

    updated = dict(row)
    translations = updated.get("translations") or {}
    if not isinstance(translations, dict):
        translations = {}
    else:
        translations = dict(translations)

    preferred_payload = translations.get("eng")
    if not isinstance(preferred_payload, dict):
        preferred_payload = {}
    preferred_payload.update(
        {key: value for key, value in translation.items() if value not in (None, "")},
    )
    translations["eng"] = preferred_payload
    updated["translations"] = translations
    return updated


def _get_title_fields(row: dict | None):
    """Return normalized title fields for TVDB entities."""
    row = row or {}
    localized_title = _find_translation(row, "name") or _get_name(row)
    original_title = (
        _normalize_text_value(row.get("originalName"))
        or _normalize_text_value(row.get("original_name"))
        or _normalize_text_value(row.get("aliases"))
        or localized_title
    )
    return {
        "title": localized_title or original_title or "",
        "original_title": original_title,
        "localized_title": localized_title or original_title,
    }


def _get_image(row: dict | None):
    """Return the best image URL for a TVDB entity."""
    row = row or {}
    for key in ("image", "image_url", "thumbnail", "poster", "poster_url"):
        value = row.get(key)
        if value:
            return value
    artworks = row.get("artworks") or []
    for artwork in artworks:
        if not isinstance(artwork, dict):
            continue
        for key in ("image", "thumbnail", "url"):
            value = artwork.get(key)
            if value:
                return value
    return settings.IMG_NONE


def _get_genres(row: dict | None):
    """Return genre names for a TVDB entity."""
    genres = []
    for genre in _coerce_list((row or {}).get("genres")):
        if isinstance(genre, dict):
            name = genre.get("name")
        else:
            name = genre
        if name:
            genres.append(str(name))
    return genres or None


def _get_remote_ids_map(row: dict | None) -> dict[str, str]:
    """Return normalized remote IDs for a TVDB entity."""
    remote_ids: dict[str, str] = {}
    for remote_id in _coerce_list((row or {}).get("remoteIds")):
        if not isinstance(remote_id, dict):
            continue
        source_name = str(
            remote_id.get("sourceName")
            or remote_id.get("type")
            or remote_id.get("source")
            or ""
        ).lower()
        value = str(remote_id.get("id") or remote_id.get("value") or "").strip()
        if not value:
            continue
        if "imdb" in source_name:
            remote_ids["imdb_id"] = value
        elif "tmdb" in source_name or "themoviedb" in source_name:
            remote_ids["tmdb_id"] = value
        elif "tvdb" in source_name:
            remote_ids["tvdb_id"] = value
        elif "mal" in source_name or "myanimelist" in source_name:
            remote_ids["mal_id"] = value
        elif "anilist" in source_name:
            remote_ids["anilist_id"] = value

    if (row or {}).get("id"):
        remote_ids.setdefault("tvdb_id", str(row["id"]))
    return remote_ids


def _get_external_links(row: dict | None):
    """Return external links for a TVDB entity."""
    remote_ids = _get_remote_ids_map(row)
    links = {
        "TVDB": f"https://www.thetvdb.com/dereferrer/series/{remote_ids['tvdb_id']}"
        if remote_ids.get("tvdb_id")
        else None,
        "IMDb": f"https://www.imdb.com/title/{remote_ids['imdb_id']}/"
        if remote_ids.get("imdb_id")
        else None,
        "TMDB": f"https://www.themoviedb.org/tv/{remote_ids['tmdb_id']}"
        if remote_ids.get("tmdb_id")
        else None,
        "MyAnimeList": f"https://myanimelist.net/anime/{remote_ids['mal_id']}"
        if remote_ids.get("mal_id")
        else None,
        "AniList": f"https://anilist.co/anime/{remote_ids['anilist_id']}"
        if remote_ids.get("anilist_id")
        else None,
    }
    return {name: url for name, url in links.items() if url}


def _get_synopsis(row: dict | None):
    """Return overview text for a TVDB entity."""
    row = row or {}
    return (
        _find_translation(row, "overview")
        or _normalize_text_value(row.get("overview"))
        or _normalize_text_value(row.get("overviewText"))
        or "No synopsis available."
    )


def _coerce_float(value) -> float | None:
    """Return a numeric float when possible."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value) -> int | None:
    """Return a numeric int when possible."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _get_rating_pair(row: dict | None) -> tuple[float | None, int | None]:
    """Return a normalized TVDB rating and vote count pair."""
    row = row or {}
    candidates = (
        (
            _coerce_float(row.get("siteRating")),
            _coerce_int(row.get("siteRatingCount")),
            False,
        ),
        (
            _coerce_float(row.get("averageRating")),
            _coerce_int(row.get("scoreCount") or row.get("siteRatingCount")),
            True,
        ),
        (
            _coerce_float(row.get("averageScore")),
            _coerce_int(row.get("scoreCount") or row.get("siteRatingCount")),
            True,
        ),
        (
            _coerce_float(row.get("score")),
            _coerce_int(row.get("scoreCount") or row.get("siteRatingCount")),
            False,
        ),
    )

    for score, score_count, allow_percent_scale in candidates:
        if score is None:
            continue
        if 0 <= score <= 10:
            return round(score, 1), score_count
        if allow_percent_scale and 10 < score <= 100:
            return round(score / 10, 1), score_count

    return None, None


def _get_score(row: dict | None):
    """Return a normalized average score."""
    score, _score_count = _get_rating_pair(row)
    return score


def _get_score_count(row: dict | None):
    """Return a normalized vote count."""
    _score, score_count = _get_rating_pair(row)
    return score_count


def _get_company_names(row: dict | None):
    """Return production company names."""
    companies = []
    for company in _coerce_list((row or {}).get("companies")):
        if isinstance(company, dict):
            name = company.get("name")
        else:
            name = company
        if name:
            companies.append(str(name))
    return companies or None


def _season_type_name(row: dict | None) -> str:
    """Return the human-readable season type name."""
    row = row or {}
    season_type = row.get("type") or {}
    if isinstance(season_type, dict):
        return str(season_type.get("name") or season_type.get("type") or "").strip()
    return str(season_type or "").strip()


def _season_number(row: dict | None):
    """Return the normalized season number."""
    row = row or {}
    for key in ("number", "seasonNumber", "season_number"):
        value = row.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _is_aired_order(row: dict | None) -> bool:
    """Return True when a season belongs to the default aired order."""
    type_name = _season_type_name(row).lower()
    if not type_name:
        return True
    return type_name in {"default", "aired order", "official order"}


def _pick_series_seasons(series_data: dict | None):
    """Return the default-aired-order season list."""
    seasons = _coerce_list((series_data or {}).get("seasons"))
    filtered = [season for season in seasons if _is_aired_order(season)]
    if not filtered:
        filtered = seasons
    filtered.sort(
        key=lambda season: (
            _season_number(season) is None,
            _season_number(season) if _season_number(season) is not None else 999999,
            str(season.get("id") or ""),
        ),
    )
    return filtered


def _season_related_entry(series_data: dict, season_data: dict, *, media_type: str):
    """Return a related-season card entry."""
    season_no = _season_number(season_data)
    episode_rows = _coerce_list(season_data.get("episodes"))
    episode_count = season_data.get("episodeCount")
    if episode_count is None and episode_rows:
        episode_count = len(episode_rows)
    first_air = None
    last_air = None
    if episode_rows:
        air_dates = [air for air in (_parse_date(ep.get("aired")) for ep in episode_rows) if air]
        if air_dates:
            first_air = min(air_dates)
            last_air = max(air_dates)

    return {
        "source": Sources.TVDB.value,
        "media_type": MediaTypes.SEASON.value,
        "image": _get_image(season_data) or _get_image(series_data),
        "media_id": str(series_data.get("id")),
        **_get_title_fields(series_data),
        "season_number": season_no,
        "season_title": _get_name(season_data) or ("Specials" if season_no == 0 else f"Season {season_no}"),
        "first_air_date": first_air,
        "last_air_date": last_air,
        "max_progress": episode_count,
        "episode_count": episode_count,
        "details": {
            "episodes": episode_count,
        },
        "library_media_type": media_type,
        "identity_media_type": MediaTypes.TV.value,
    }


def _normalize_characters(series_data: dict | None):
    """Return normalized cast/crew rows."""
    cast_rows = []
    crew_rows = []
    for character in _coerce_list((series_data or {}).get("characters")):
        if not isinstance(character, dict):
            continue
        people = character.get("personName") or character.get("peopleType") or {}
        person_name = character.get("personName") or character.get("name") or ""
        if not person_name and isinstance(people, dict):
            person_name = people.get("name") or ""
        person_id = (
            character.get("peopleId")
            or character.get("personId")
            or character.get("id")
        )
        row = {
            "person_id": str(person_id or ""),
            "name": person_name,
            "image": character.get("image") or settings.IMG_NONE,
            "known_for_department": character.get("type") or "",
            "gender": "unknown",
            "department": "Acting",
            "role": character.get("name") or character.get("character") or "",
            "order": character.get("sort") or character.get("order"),
        }
        if str(character.get("type") or "").lower() in {"actor", "guest star", "voice"}:
            cast_rows.append(row)
        else:
            row["department"] = character.get("type") or "Crew"
            crew_rows.append(row)
    cast_rows.sort(key=lambda value: (value.get("order") is None, value.get("order") or 999999))
    crew_rows.sort(key=lambda value: (value.get("department") or "", value.get("order") or 999999))
    return cast_rows, crew_rows


def _build_series_metadata(series_data: dict, *, media_type: str):
    """Return normalized series metadata."""
    seasons = _pick_series_seasons(series_data)
    cast_rows, crew_rows = _normalize_characters(series_data)
    episode_runtime = series_data.get("averageRuntime") or series_data.get("runtime")
    details = {
        "format": "TV",
        "first_air_date": _parse_date(
            series_data.get("firstAired") or series_data.get("first_air_time"),
        ),
        "last_air_date": _parse_date(series_data.get("lastAired")),
        "status": (series_data.get("status") or {}).get("name")
        if isinstance(series_data.get("status"), dict)
        else series_data.get("status"),
        "seasons": len(seasons),
        "episodes": series_data.get("numberOfEpisodes")
        or series_data.get("episodes")
        or None,
        "runtime": tmdb.get_readable_duration(episode_runtime),
        "studios": _get_company_names(series_data),
        "country": None,
        "languages": None,
    }

    remote_ids = _get_remote_ids_map(series_data)
    return {
        "media_id": str(series_data.get("id")),
        "source": Sources.TVDB.value,
        "source_url": f"https://www.thetvdb.com/dereferrer/series/{series_data.get('id')}",
        "media_type": media_type,
        **_get_title_fields(series_data),
        "max_progress": details["episodes"],
        "image": _get_image(series_data),
        "synopsis": _get_synopsis(series_data),
        "genres": _get_genres(series_data),
        "score": _get_score(series_data),
        "score_count": _get_score_count(series_data),
        "details": details,
        "cast": cast_rows,
        "crew": crew_rows,
        "studios_full": [],
        "related": {
            "seasons": [
                _season_related_entry(series_data, season, media_type=media_type)
                for season in seasons
            ],
            "recommendations": [],
        },
        "tvdb_id": str(series_data.get("id")),
        "external_links": _get_external_links(series_data),
        "providers": {},
        "provider_external_ids": remote_ids,
        "identity_media_type": MediaTypes.TV.value,
        "library_media_type": media_type,
    }


def _normalize_episode_rows(season_data: dict | None):
    """Return normalized episode rows for a season."""
    normalized = []
    for episode in _coerce_list((season_data or {}).get("episodes")):
        if not isinstance(episode, dict):
            continue
        air_date = (
            _parse_date(episode.get("aired"))
            or _parse_date(episode.get("firstAired"))
            or _parse_date(episode.get("airDate"))
        )
        normalized.append(
            {
                "episode_number": episode.get("number") or episode.get("episodeNumber"),
                "air_date": air_date,
                "still_path": None,
                "image": _get_image(episode),
                "name": _get_name(episode),
                "overview": _get_synopsis(episode),
                "runtime": episode.get("runtime") or episode.get("airsAfterSeason"),
            },
        )
    normalized.sort(
        key=lambda episode: (
            episode.get("episode_number") is None,
            episode.get("episode_number") if episode.get("episode_number") is not None else 999999,
        ),
    )
    return normalized


def _normalize_season_metadata(series_data: dict, season_data: dict, *, media_type: str):
    """Return normalized season metadata."""
    episodes = _normalize_episode_rows(season_data)
    runtimes = [episode["runtime"] for episode in episodes if isinstance(episode.get("runtime"), int)]
    total_runtime = sum(runtimes) if runtimes else 0
    air_dates = [episode["air_date"] for episode in episodes if episode.get("air_date")]
    season_no = _season_number(season_data)
    return {
        "source": Sources.TVDB.value,
        "media_type": MediaTypes.SEASON.value,
        "season_title": _get_name(season_data) or ("Specials" if season_no == 0 else f"Season {season_no}"),
        "max_progress": episodes[-1]["episode_number"] if episodes else 0,
        "image": _get_image(season_data) or _get_image(series_data),
        "season_number": season_no,
        "synopsis": _get_synopsis(season_data),
        "score": _get_score(season_data),
        "score_count": _get_score_count(season_data),
        "details": {
            "first_air_date": min(air_dates) if air_dates else None,
            "last_air_date": max(air_dates) if air_dates else None,
            "episodes": len(episodes),
            "runtime": tmdb.get_readable_duration(sum(runtimes) / len(runtimes)) if runtimes else None,
            "total_runtime": tmdb.get_readable_duration(total_runtime) if total_runtime else None,
        },
        "episodes": episodes,
        "providers": {},
        "media_id": str(series_data.get("id")),
        **_get_title_fields(series_data),
        "tvdb_id": str(series_data.get("id")),
        "external_links": _get_external_links(series_data),
        "genres": _get_genres(series_data),
        "source_url": f"https://www.thetvdb.com/dereferrer/series/{series_data.get('id')}",
        "identity_media_type": MediaTypes.TV.value,
        "library_media_type": media_type,
    }


def _season_cache_key(media_id, season_number, media_type):
    return _cache_key(media_type, media_id, season_number)


def search_remote_id(remote_id: str):
    """Return raw TVDB remote-ID search results."""
    cache_key = _cache_key("remoteid", remote_id)
    data = cache.get(cache_key)
    if data is None:
        data = _unwrap_data(_request(f"search/remoteid/{remote_id}")) or []
        cache.set(cache_key, data)
    return data


def search(media_type, query, page):
    """Search TVDB for TV or grouped anime titles."""
    cache_key = _cache_key("search", media_type, query, page)
    data = cache.get(cache_key)
    if data is not None:
        return data

    results = _coerce_list(
        _unwrap_data(
            _request(
                "search",
                params={
                    "query": query,
                    "type": "series",
                    "page": max(page - 1, 0),
                    "lang": "eng",
                },
            ),
        ),
    )

    normalized_results = []
    for row in results:
        if not isinstance(row, dict):
            continue
        title_fields = _get_title_fields(row)
        result = {
            "media_id": str(row.get("tvdb_id") or row.get("id")),
            "source": Sources.TVDB.value,
            "media_type": media_type,
            "identity_media_type": MediaTypes.TV.value,
            "library_media_type": media_type,
            **title_fields,
            "image": _get_image(row),
            "year": row.get("year") or tmdb.get_year({"first_air_date": row.get("firstAired")}),
        }
        if result["media_id"]:
            normalized_results.append(result)

    data = helpers.format_search_response(page, 20, len(normalized_results), normalized_results)
    cache.set(cache_key, data)
    return data


def tv(media_id, *, routed_media_type=MediaTypes.TV.value):
    """Return normalized TVDB series metadata."""
    cache_key = _cache_key(routed_media_type, media_id)
    data = cache.get(cache_key)
    if data is None:
        response = _with_preferred_translation(
            _unwrap_data(_request(f"series/{media_id}/extended")) or {},
            "series",
        )
        data = _build_series_metadata(response, media_type=routed_media_type)
        cache.set(cache_key, data)
    return data


def tv_with_seasons(media_id, season_numbers, *, routed_media_type=MediaTypes.TV.value):
    """Return a TVDB series payload enriched with selected seasons."""
    normalized_numbers = []
    for season_number in season_numbers:
        try:
            normalized_numbers.append(int(season_number))
        except (TypeError, ValueError):
            continue

    series_metadata = tv(media_id, routed_media_type=routed_media_type)
    if not normalized_numbers:
        return series_metadata

    series_data = _with_preferred_translation(
        _unwrap_data(_request(f"series/{media_id}/extended")) or {},
        "series",
    )
    seasons_by_number = {
        _season_number(season): season
        for season in _pick_series_seasons(series_data)
        if _season_number(season) is not None
    }

    season_payloads = {}
    for season_number in normalized_numbers:
        cache_key = _season_cache_key(media_id, season_number, routed_media_type)
        season_metadata = cache.get(cache_key)
        if season_metadata is None:
            season_row = seasons_by_number.get(season_number)
            if not season_row:
                continue
            season_id = season_row.get("id")
            if not season_id:
                continue
            season_data = _with_preferred_translation(
                _unwrap_data(_request(f"seasons/{season_id}/extended")) or {},
                "seasons",
            )
            season_metadata = _normalize_season_metadata(
                series_data,
                season_data,
                media_type=routed_media_type,
            )
            cache.set(cache_key, season_metadata)
        season_payloads[f"season/{season_number}"] = season_metadata

    return series_metadata | season_payloads


def episode(media_id, season_number, episode_number, *, routed_media_type=MediaTypes.TV.value):
    """Return normalized episode metadata from a TVDB season payload."""
    season_payload = tv_with_seasons(
        media_id,
        [season_number],
        routed_media_type=routed_media_type,
    ).get(f"season/{season_number}", {})
    series_payload = tv(media_id, routed_media_type=routed_media_type)
    matched_episode = None
    for episode_row in season_payload.get("episodes", []):
        if str(episode_row.get("episode_number")) == str(episode_number):
            matched_episode = episode_row
            break

    matched_episode = matched_episode or {}
    return {
        "title": season_payload.get("title") or series_payload.get("title") or "",
        "original_title": season_payload.get("original_title") or series_payload.get("original_title"),
        "localized_title": season_payload.get("localized_title") or series_payload.get("localized_title"),
        "season_title": season_payload.get("season_title") or f"Season {season_number}",
        "episode_title": matched_episode.get("name") or f"Episode {episode_number}",
        "image": matched_episode.get("image") or settings.IMG_NONE,
        "cast": [],
        "crew": [],
    }


def get_episode_airstamp_map(tvdb_id):
    """Return precise air datetimes for all default-order episodes."""
    cache_key = _cache_key("episode_map", tvdb_id)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    response = _unwrap_data(
        _request(
            f"series/{tvdb_id}/episodes/default",
            params={"page": 0},
        ),
    ) or {}
    episode_rows = response.get("episodes") or response.get("data") or []
    result = {}
    for row in episode_rows:
        if not isinstance(row, dict):
            continue
        season_number = row.get("seasonNumber") or row.get("season") or row.get("airedSeason")
        episode_number = row.get("number") or row.get("episodeNumber") or row.get("airedEpisodeNumber")
        air_date = _parse_date(row.get("aired") or row.get("firstAired") or row.get("airDate"))
        if season_number is None or episode_number is None or air_date is None:
            continue
        result[f"{int(season_number)}_{int(episode_number)}"] = air_date.isoformat()

    cache.set(cache_key, result)
    return result


def build_specials_season(tvdb_id, *, media_id, source, tv_data):
    """Return a TMDB-compatible specials season payload using TVDB episode data."""
    season_payload = tv_with_seasons(
        str(tvdb_id),
        [0],
        routed_media_type=tv_data.get("library_media_type") or MediaTypes.TV.value,
    ).get("season/0")
    if not season_payload:
        return None

    season_payload = dict(season_payload)
    season_payload["source"] = source
    season_payload["media_type"] = MediaTypes.SEASON.value
    season_payload["media_id"] = str(media_id)
    season_payload["title"] = tv_data.get("title") or season_payload.get("title") or ""
    season_payload["original_title"] = tv_data.get("original_title")
    season_payload["localized_title"] = tv_data.get("localized_title")
    season_payload["image"] = season_payload.get("image") or tv_data.get("image") or settings.IMG_NONE
    season_payload["synopsis"] = season_payload.get("synopsis") or tv_data.get("synopsis") or "No synopsis available."
    season_payload["genres"] = tv_data.get("genres") or season_payload.get("genres")
    season_payload["source_url"] = tv_data.get("external_links", {}).get("TVDB") or season_payload.get("source_url")
    season_payload["external_links"] = tv_data.get("external_links") or season_payload.get("external_links") or {}
    season_payload["tvdb_id"] = str(tvdb_id)
    return season_payload
