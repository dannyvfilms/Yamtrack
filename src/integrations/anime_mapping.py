"""Shared anime ID mapping loader used by webhooks and grouped-anime migration."""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

from app.providers import services

CACHE_KEY = "anime_mapping_data"
REMOTE_URL = (
    "https://raw.githubusercontent.com/Kometa-Team/Anime-IDs/refs/heads/master/"
    "anime_ids.json"
)
TEST_FIXTURE_PATH = Path(__file__).resolve().parent / "tests" / "mock_data" / "anime_mapping.json"


def load_mapping_data() -> dict:
    """Return the cached anime ID mapping payload."""
    data = cache.get(CACHE_KEY)
    if data is not None:
        return data

    if settings.TESTING and TEST_FIXTURE_PATH.exists():
        with TEST_FIXTURE_PATH.open() as file_handle:
            data = json.load(file_handle)
    else:
        data = services.api_request("ANIME_MAPPING", "GET", REMOTE_URL)

    data = data or {}
    cache.set(CACHE_KEY, data)
    return data


def _normalize_mal_ids(mal_value) -> set[str]:
    """Return MAL IDs normalized from scalar or comma-separated values."""
    if mal_value in (None, ""):
        return set()
    if isinstance(mal_value, int):
        return {str(mal_value)}
    return {
        str(part).strip()
        for part in str(mal_value).split(",")
        if str(part).strip()
    }


def find_entries_for_mal_id(mal_id: str | int) -> list[dict]:
    """Return mapping entries that include the MAL ID."""
    normalized = str(mal_id)
    return [
        entry
        for entry in load_mapping_data().values()
        if normalized in _normalize_mal_ids(entry.get("mal_id"))
    ]


def resolve_provider_series_id(mal_id: str | int, provider: str) -> str | None:
    """Resolve a grouped-provider series ID for a MAL title."""
    entries = find_entries_for_mal_id(mal_id)
    if not entries:
        return None

    if provider == "tvdb":
        for entry in entries:
            if entry.get("tvdb_id") not in (None, ""):
                return str(entry["tvdb_id"])
        return None

    if provider == "tmdb":
        for key in ("tmdb_show_id", "tmdb_id", "tmdb_tv_id"):
            for entry in entries:
                if entry.get(key) not in (None, ""):
                    return str(entry[key])
        return None

    return None
