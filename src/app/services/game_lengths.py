"""Persisted game length metadata helpers for IGDB games."""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import quote_plus

from django.conf import settings
from django.utils import timezone

from app import helpers
from app.models import Item, MediaTypes, Sources
from app.providers import igdb
from app.providers import services as provider_services

logger = logging.getLogger(__name__)

HLTB_BASE_URL = "https://howlongtobeat.com"
HLTB_BROWSER_HEADERS = {
    "Accept": "application/json",
    "Origin": HLTB_BASE_URL,
    "Referer": HLTB_BASE_URL,
    "User-Agent": "Mozilla/5.0",
}
HLTB_HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "Referer": f"{HLTB_BASE_URL}/",
    "User-Agent": "Mozilla/5.0",
}
HLTB_TITLE_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
HLTB_GAME_URL_RE = re.compile(r"/game/(\d+)")
HLTB_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
HLTB_MATCH_AMBIGUOUS = "ambiguous"
HLTB_MATCH_DIRECT = "direct_url"
HLTB_MATCH_EXACT = "exact_title_year"
HLTB_MATCH_STEAM = "steam_verified"
GAME_LENGTH_SOURCE_HLTB = "hltb"
GAME_LENGTH_SOURCE_IGDB = "igdb"
GAME_LENGTHS_REFRESH_TTL = 60 * 15
GAME_LENGTHS_REFRESH_STALE_SECONDS = 60 * 5


def refresh_game_lengths(
    item: Item,
    igdb_metadata: dict[str, Any] | None = None,
    force: bool = False,
    *,
    fetch_hltb: bool = True,
) -> dict[str, Any]:
    """Refresh persisted game-length metadata for an IGDB-backed game item."""
    if item.source != Sources.IGDB.value or item.media_type != MediaTypes.GAME.value:
        return item.provider_game_lengths or {}

    existing_payload = item.provider_game_lengths or {}
    if not force and existing_payload:
        if not fetch_hltb:
            return existing_payload
        if item.provider_game_lengths_source == GAME_LENGTH_SOURCE_HLTB:
            return existing_payload
        if item.provider_game_lengths_match == HLTB_MATCH_AMBIGUOUS:
            return existing_payload

    metadata = igdb_metadata if isinstance(igdb_metadata, dict) else provider_services.get_media_metadata(
        item.media_type,
        item.media_id,
        item.source,
    )

    result = {
        "active_source": existing_payload.get("active_source") or GAME_LENGTH_SOURCE_IGDB,
    }
    if isinstance(existing_payload.get("hltb"), dict):
        result["hltb"] = existing_payload["hltb"]

    igdb_payload = fetch_igdb_time_to_beat(item.media_id)
    result["igdb"] = igdb_payload

    external_ids = dict(item.provider_external_ids or {})
    external_ids.update(_extract_igdb_external_ids(metadata))

    match_type = item.provider_game_lengths_match or (
        HLTB_MATCH_DIRECT
        if item.provider_game_lengths_source == GAME_LENGTH_SOURCE_HLTB
        else "igdb_fallback"
    )

    if fetch_hltb:
        try:
            candidate = resolve_hltb_candidate(metadata)
        except Exception:
            logger.warning(
                "hltb_resolver_failed media_id=%s title=%s",
                item.media_id,
                metadata.get("title"),
                exc_info=True,
            )
            candidate = None

        if candidate and candidate.get("detail"):
            detail = candidate["detail"]
            result["hltb"] = detail
            result["active_source"] = GAME_LENGTH_SOURCE_HLTB
            match_type = candidate["match"]
            external_ids.update(_extract_hltb_external_ids(detail))
        elif candidate and candidate.get("match") == HLTB_MATCH_AMBIGUOUS:
            result["active_source"] = GAME_LENGTH_SOURCE_IGDB
            match_type = HLTB_MATCH_AMBIGUOUS
            result.pop("hltb", None)
    elif not result.get("hltb"):
        result["active_source"] = GAME_LENGTH_SOURCE_IGDB
        match_type = match_type or "igdb_fallback"

    if result.get("active_source") != GAME_LENGTH_SOURCE_HLTB:
        result.pop("hltb", None)
        match_type = match_type or "igdb_fallback"

    if not result.get("hltb") and not result.get("igdb"):
        raise ValueError(f"No game length data available for item {item.id}")

    item.provider_external_ids = external_ids
    item.provider_game_lengths = result
    item.provider_game_lengths_source = result["active_source"]
    item.provider_game_lengths_match = match_type
    item.provider_game_lengths_fetched_at = timezone.now()
    item.save(
        update_fields=[
            "provider_external_ids",
            "provider_game_lengths",
            "provider_game_lengths_source",
            "provider_game_lengths_match",
            "provider_game_lengths_fetched_at",
        ],
    )
    return result


def resolve_hltb_candidate(igdb_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Resolve an IGDB metadata payload to a confident HLTB candidate."""
    payload = igdb_metadata if isinstance(igdb_metadata, dict) else {}
    direct_url = ((payload.get("external_links") or {}).get("HowLongToBeat") or "").strip()
    direct_hltb_id = _extract_hltb_id(direct_url)
    if direct_hltb_id:
        return {
            "match": HLTB_MATCH_DIRECT,
            "hltb_id": direct_hltb_id,
            "detail": fetch_hltb_detail(direct_hltb_id),
        }

    title = (payload.get("title") or "").strip()
    release_year = _extract_release_year(payload)
    if not title or release_year is None:
        return None

    search_payload = fetch_hltb_search(title)
    candidates = list(search_payload.get("data") or [])
    if not candidates:
        return None

    exact_candidates = [
        candidate
        for candidate in candidates
        if _normalize_title(candidate.get("game_name")) == _normalize_title(title)
        and _coerce_int(candidate.get("release_world")) == release_year
    ]
    if not exact_candidates:
        return {"match": HLTB_MATCH_AMBIGUOUS, "candidates": candidates}

    candidate_detail_cache: dict[int, dict[str, Any]] = {}
    steam_app_id = _coerce_int((_extract_igdb_external_ids(payload)).get("steam_app_id"))

    if len(exact_candidates) == 1:
        candidate = exact_candidates[0]
        detail = _get_hltb_detail_from_cache(candidate_detail_cache, candidate["game_id"])
        return {
            "match": _match_label_for_detail(detail, steam_app_id, HLTB_MATCH_EXACT),
            "hltb_id": candidate["game_id"],
            "detail": detail,
        }

    overlapping_platforms = [
        candidate
        for candidate in exact_candidates
        if _has_platform_overlap(payload, candidate)
    ]
    if len(overlapping_platforms) == 1:
        candidate = overlapping_platforms[0]
        detail = _get_hltb_detail_from_cache(candidate_detail_cache, candidate["game_id"])
        return {
            "match": _match_label_for_detail(detail, steam_app_id, HLTB_MATCH_EXACT),
            "hltb_id": candidate["game_id"],
            "detail": detail,
        }

    if steam_app_id:
        steam_matches = []
        for candidate in exact_candidates:
            detail = _get_hltb_detail_from_cache(candidate_detail_cache, candidate["game_id"])
            if _coerce_int((detail.get("external_ids") or {}).get("steam_app_id")) == steam_app_id:
                steam_matches.append((candidate, detail))
        if len(steam_matches) == 1:
            candidate, detail = steam_matches[0]
            return {
                "match": HLTB_MATCH_STEAM,
                "hltb_id": candidate["game_id"],
                "detail": detail,
            }

    return {"match": HLTB_MATCH_AMBIGUOUS, "candidates": exact_candidates}


def fetch_hltb_search(title: str) -> dict[str, Any]:
    """Search HLTB for a game title using the current tokenized finder flow."""
    query = (title or "").strip()
    if not query:
        raise ValueError("HLTB search title cannot be empty")

    init_response = provider_services.session.get(
        f"{HLTB_BASE_URL}/api/finder/init",
        params={"t": int(time.time() * 1000)},
        headers=HLTB_BROWSER_HEADERS,
        timeout=settings.REQUEST_TIMEOUT,
    )
    init_response.raise_for_status()
    token = init_response.json()["token"]

    search_response = provider_services.session.post(
        f"{HLTB_BASE_URL}/api/finder",
        json={
            "searchType": "games",
            "searchTerms": [query],
            "searchPage": 1,
            "size": 20,
            "searchOptions": {
                "games": {
                    "userId": 0,
                    "platform": "",
                    "sortCategory": "popular",
                    "rangeCategory": "main",
                    "rangeTime": {"min": None, "max": None},
                    "gameplay": {
                        "perspective": "",
                        "flow": "",
                        "genre": "",
                        "difficulty": "",
                    },
                    "rangeYear": {"min": "", "max": ""},
                    "modifier": "",
                },
                "users": {"sortCategory": "postcount"},
                "lists": {"sortCategory": "follows"},
                "filter": "",
                "sort": 0,
                "randomizer": 0,
            },
            "useCache": True,
        },
        headers={
            **HLTB_BROWSER_HEADERS,
            "Content-Type": "application/json",
            "x-auth-token": token,
        },
        timeout=settings.REQUEST_TIMEOUT,
    )
    search_response.raise_for_status()
    return search_response.json()


def fetch_hltb_detail(hltb_id: int | str) -> dict[str, Any]:
    """Fetch and normalize a HLTB game detail page."""
    normalized_id = _coerce_int(hltb_id)
    if not normalized_id:
        raise ValueError(f"Invalid HLTB id: {hltb_id!r}")

    response = provider_services.session.get(
        f"{HLTB_BASE_URL}/game/{normalized_id}",
        headers=HLTB_HTML_HEADERS,
        timeout=settings.REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    match = HLTB_NEXT_DATA_RE.search(response.text)
    if not match:
        raise ValueError(f"HLTB detail page missing __NEXT_DATA__ for {normalized_id}")

    next_data = json.loads(match.group(1))
    page_props = (((next_data.get("props") or {}).get("pageProps")) or {})
    game_data = (((page_props.get("game") or {}).get("data")) or {})
    game_rows = list(game_data.get("game") or [])
    if not game_rows:
        raise ValueError(f"HLTB detail payload missing game rows for {normalized_id}")
    profile = game_rows[0]

    return {
        "game_id": normalized_id,
        "url": f"{HLTB_BASE_URL}/game/{normalized_id}",
        "title": profile.get("game_name") or "",
        "release_year": _extract_release_year_from_hltb_detail(profile),
        "summary": {
            "main_minutes": _seconds_to_minutes(profile.get("comp_main")),
            "main_plus_minutes": _seconds_to_minutes(profile.get("comp_plus")),
            "completionist_minutes": _seconds_to_minutes(profile.get("comp_100")),
            "all_styles_minutes": _seconds_to_minutes(profile.get("comp_all")),
        },
        "counts": {
            "main": _coerce_int(profile.get("comp_main_count")) or 0,
            "main_plus": _coerce_int(profile.get("comp_plus_count")) or 0,
            "completionist": _coerce_int(profile.get("comp_100_count")) or 0,
            "all_styles": _coerce_int(profile.get("comp_all_count")) or 0,
        },
        "single_player_table": _build_single_player_table(profile),
        "platform_table": _build_platform_table(game_data.get("platformData") or []),
        "external_ids": _extract_hltb_profile_ids(profile),
        "raw": page_props,
    }


def fetch_igdb_time_to_beat(igdb_id: int | str) -> dict[str, Any]:
    """Fetch IGDB's official time-to-beat data for a game id."""
    game_id = _coerce_int(igdb_id)
    if not game_id:
        raise ValueError(f"Invalid IGDB id: {igdb_id!r}")

    access_token = igdb.get_access_token()
    response = provider_services.api_request(
        Sources.IGDB.value,
        "POST",
        f"{igdb.base_url}/game_time_to_beats",
        data=(
            "fields game_id,hastily,normally,completely,count;"
            f" where game_id = {game_id};"
        ),
        headers={
            "Client-ID": settings.IGDB_ID,
            "Authorization": f"Bearer {access_token}",
        },
    )
    raw_rows = list(response or [])
    row = raw_rows[0] if raw_rows else {}
    return {
        "game_id": game_id,
        "summary": {
            "hastily_seconds": _coerce_int(row.get("hastily")) or 0,
            "normally_seconds": _coerce_int(row.get("normally")) or 0,
            "completely_seconds": _coerce_int(row.get("completely")) or 0,
            "count": _coerce_int(row.get("count")) or 0,
        },
        "raw": raw_rows,
    }


def get_hltb_search_url(title: str | None) -> str | None:
    """Return a basic HLTB search URL for a game title."""
    query = (title or "").strip()
    if not query:
        return None
    return f"{HLTB_BASE_URL}/?q={quote_plus(query)}"


def get_game_lengths_refresh_lock_key(
    item_id: int,
    *,
    force: bool = False,
    fetch_hltb: bool = True,
) -> str:
    """Return the cache key used to debounce game-length refresh tasks."""
    return f"game_lengths_refresh:{item_id}:{int(force)}:{int(fetch_hltb)}"


def build_game_lengths_refresh_lock(
    *,
    queued_at: datetime | None = None,
    force: bool = False,
    fetch_hltb: bool = True,
) -> dict[str, Any]:
    """Return the cache payload stored while a game-length refresh is pending."""
    queued = queued_at or timezone.now()
    return {
        "queued_at": queued.timestamp(),
        "force": bool(force),
        "fetch_hltb": bool(fetch_hltb),
    }


def is_game_lengths_refresh_lock_stale(lock_payload: Any) -> bool:
    """Return whether a cached game-length refresh lock is too old to trust."""
    if not isinstance(lock_payload, dict):
        return True
    try:
        queued_at = float(lock_payload["queued_at"])
    except (KeyError, TypeError, ValueError):
        return True
    return (timezone.now().timestamp() - queued_at) >= GAME_LENGTHS_REFRESH_STALE_SECONDS


def _build_single_player_table(profile: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [
        ("main", "Main Story", "comp_main"),
        ("main_plus", "Main + Extras", "comp_plus"),
        ("completionist", "Completionist", "comp_100"),
        ("all_styles", "All PlayStyles", "comp_all"),
    ]
    data = []
    for key, label, prefix in rows:
        prefix_base = prefix.removeprefix("comp_")
        data.append(
            {
                "key": key,
                "label": label,
                "count": _coerce_int(profile.get(f"{prefix}_count")) or 0,
                "average_minutes": _seconds_to_minutes(profile.get(prefix)),
                "median_minutes": _seconds_to_minutes(profile.get(f"{prefix}_med")),
                "rushed_minutes": _seconds_to_minutes(profile.get(f"{prefix}_l")),
                "leisure_minutes": _seconds_to_minutes(profile.get(f"{prefix}_h")),
                "average_source_seconds": _coerce_int(profile.get(f"{prefix}_avg")) or 0,
                "source_key": prefix_base,
            },
        )
    return data


def _build_platform_table(platform_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    table = []
    for row in platform_rows:
        table.append(
            {
                "platform": row.get("platform") or "",
                "count": _coerce_int(row.get("count_comp")) or 0,
                "main_minutes": _seconds_to_minutes(row.get("comp_main")),
                "main_plus_minutes": _seconds_to_minutes(row.get("comp_plus")),
                "completionist_minutes": _seconds_to_minutes(row.get("comp_100")),
                "fastest_minutes": _seconds_to_minutes(row.get("comp_low")),
                "slowest_minutes": _seconds_to_minutes(row.get("comp_high")),
            },
        )
    return table


def _extract_hltb_profile_ids(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "hltb_game_id": _coerce_int(profile.get("game_id")) or 0,
        "steam_app_id": _coerce_int(profile.get("profile_steam")) or 0,
        "itch_id": _coerce_int(profile.get("profile_itch")) or 0,
        "ign_uuid": profile.get("profile_ign") or "",
    }


def _extract_hltb_external_ids(detail: dict[str, Any]) -> dict[str, Any]:
    external_ids = detail.get("external_ids") or {}
    return {
        "hltb_game_id": _coerce_int(detail.get("game_id")) or _coerce_int(external_ids.get("hltb_game_id")) or 0,
        "steam_app_id": _coerce_int(external_ids.get("steam_app_id")) or 0,
        "itch_id": _coerce_int(external_ids.get("itch_id")) or 0,
        "ign_uuid": external_ids.get("ign_uuid") or "",
    }


def _extract_igdb_external_ids(metadata: dict[str, Any] | None) -> dict[str, Any]:
    payload = metadata if isinstance(metadata, dict) else {}
    external_ids = payload.get("external_ids") or {}
    result = {}
    for key in ("steam_app_id", "itch_id", "ign_uuid", "hltb_game_id"):
        if key in external_ids:
            result[key] = external_ids[key]
    direct_hltb_id = _extract_hltb_id(((payload.get("external_links") or {}).get("HowLongToBeat")))
    if direct_hltb_id:
        result["hltb_game_id"] = direct_hltb_id
    return result


def _normalize_title(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value))
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower()
    normalized = HLTB_TITLE_NORMALIZE_RE.sub(" ", normalized)
    return " ".join(normalized.split())


def _extract_release_year(metadata: dict[str, Any]) -> int | None:
    release_dt = helpers.extract_release_datetime(metadata or {})
    return release_dt.year if release_dt else None


def _extract_release_year_from_hltb_detail(profile: dict[str, Any]) -> int | None:
    for field_name in ("release_world", "release_na", "release_eu", "release_jp"):
        value = profile.get(field_name)
        if not value:
            continue
        if isinstance(value, int):
            return value
        text = str(value)
        try:
            return int(text[:4])
        except (TypeError, ValueError):
            continue
    return None


def _coerce_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _seconds_to_minutes(value: Any) -> int:
    seconds = _coerce_int(value) or 0
    if seconds <= 0:
        return 0
    return int(round(seconds / 60))


def _extract_hltb_id(url: str | None) -> int | None:
    if not isinstance(url, str):
        return None
    match = HLTB_GAME_URL_RE.search(url)
    if not match:
        return None
    return _coerce_int(match.group(1))


def _get_hltb_detail_from_cache(cache: dict[int, dict[str, Any]], hltb_id: int) -> dict[str, Any]:
    if hltb_id not in cache:
        cache[hltb_id] = fetch_hltb_detail(hltb_id)
    return cache[hltb_id]


def _match_label_for_detail(detail: dict[str, Any], steam_app_id: int | None, default: str) -> str:
    if steam_app_id and _coerce_int((detail.get("external_ids") or {}).get("steam_app_id")) == steam_app_id:
        return HLTB_MATCH_STEAM
    return default


def _has_platform_overlap(igdb_metadata: dict[str, Any], candidate: dict[str, Any]) -> bool:
    igdb_platforms = {
        _normalize_title(platform)
        for platform in ((igdb_metadata.get("details") or {}).get("platforms") or [])
        if _normalize_title(platform)
    }
    hltb_platforms = {
        _normalize_title(platform)
        for platform in str(candidate.get("profile_platform") or "").split(",")
        if _normalize_title(platform)
    }
    return bool(igdb_platforms and hltb_platforms and igdb_platforms.intersection(hltb_platforms))
