"""Per-tab availability for the Discover tab bar.

A tab is greyed-out with a tooltip when the API key its provider needs is not
configured. Several providers ship with a bundled shared key (see
``config/settings.py``); a present value -- bundled or user-supplied -- counts as
available, so only genuinely-unset keys disable a tab.
"""

from __future__ import annotations

from django.conf import settings

from app.discover.tabs import default_tab, get_tabs

# capability_key -> (human label, env var to set)
_PROVIDER_INFO: dict[str, tuple[str, str]] = {
    "TMDB_API": ("TMDb", "TMDB_API"),
    "TRAKT_API": ("Trakt", "TRAKT_API"),
    "MAL_API": ("MyAnimeList", "MAL_API"),
    "COMICVINE_API": ("Comic Vine", "COMICVINE_API"),
    "BGG_API_TOKEN": ("BoardGameGeek", "BGG_API_TOKEN"),
    "LASTFM_API_KEY": ("Last.fm", "LASTFM_API_KEY"),
    "IGDB_ID": ("IGDB", "IGDB_ID / IGDB_SECRET"),
}


def _capability_available(capability_key: str | None) -> bool:
    """Return True when the provider key gating a tab is configured."""
    if capability_key is None:
        return True
    if not getattr(settings, capability_key, ""):
        return False
    # IGDB needs both a client id and secret to authenticate.
    if capability_key == "IGDB_ID":
        return bool(getattr(settings, "IGDB_SECRET", ""))
    return True


def _tooltip(capability_key: str | None) -> str | None:
    if capability_key is None:
        return None
    label, env_var = _PROVIDER_INFO.get(capability_key, (capability_key, capability_key))
    return f"Set the {env_var} environment variable with your {label} API key to enable this tab."


def first_enabled_tab(media_type: str) -> str | None:
    """Return the first tab whose key is enabled, falling back to the first tab.

    The fallback keeps a tab selected even when every provider key is missing, so
    the page never renders without an active tab.
    """
    for tab in get_tabs(media_type):
        if _capability_available(tab.capability_key):
            return tab.key
    return default_tab(media_type)


def tab_availability(media_type: str) -> dict[str, dict[str, object]]:
    """Return ``{tab_key: {"enabled": bool, "tooltip": str | None}}`` for the media type."""
    availability: dict[str, dict[str, object]] = {}
    for tab in get_tabs(media_type):
        available = _capability_available(tab.capability_key)
        availability[tab.key] = {
            "enabled": available,
            "tooltip": None if available else _tooltip(tab.capability_key),
        }
    return availability
