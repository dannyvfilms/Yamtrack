"""Discover editorial tab registry.

Tabs are a thin projection over the row machinery: each tab renders a single
``row_key`` (see ``discover.registry`` / ``discover.provider_candidates``) as one
grid. Ordering matters -- the first tab per media type is the default selection.

The tab -> endpoint -> key mapping is documented in ``DISCOVER.md`` at the repo
root; keep the two in sync.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import MediaTypes


@dataclass(frozen=True, slots=True)
class TabDefinition:
    """Declarative editorial tab shown on the Discover page."""

    key: str
    """Stable identifier used in URLs and the row cache."""

    label: str
    """Human label rendered on the tab button."""

    row_key: str
    """Row key dispatched through ``_build_row_candidates``."""

    provider: str
    """Backing provider id (informational / debugging)."""

    capability_key: str | None = None
    """Settings attribute that gates this tab. ``None`` means always available."""


TAB_REGISTRY: dict[str, list[TabDefinition]] = {
    MediaTypes.MOVIE.value: [
        TabDefinition("trending", "Trending", "trending_right_now", "trakt", "TRAKT_API"),
        TabDefinition("trending_now", "Trending Now", "tmdb_trending", "tmdb", "TMDB_API"),
        TabDefinition("top_rated", "Top Rated", "tmdb_top_rated", "tmdb", "TMDB_API"),
        TabDefinition("popular", "Popular", "all_time_greats_unseen", "trakt", "TRAKT_API"),
        TabDefinition("now_playing", "Now Playing", "tmdb_now_playing", "tmdb", "TMDB_API"),
        TabDefinition("upcoming", "Upcoming", "coming_soon", "trakt", "TRAKT_API"),
        TabDefinition("box_office", "Box Office", "trakt_box_office", "trakt", "TRAKT_API"),
    ],
    MediaTypes.TV.value: [
        TabDefinition("trending", "Trending", "trending_right_now", "trakt", "TRAKT_API"),
        TabDefinition("trending_now", "Trending Now", "tmdb_trending", "tmdb", "TMDB_API"),
        TabDefinition("top_rated", "Top Rated", "tmdb_top_rated", "tmdb", "TMDB_API"),
        TabDefinition("popular", "Popular", "all_time_greats_unseen", "trakt", "TRAKT_API"),
        TabDefinition("on_the_air", "On The Air", "tmdb_on_the_air", "tmdb", "TMDB_API"),
        TabDefinition("airing_today", "Airing Today", "tmdb_airing_today", "tmdb", "TMDB_API"),
        TabDefinition("coming_soon", "Coming Soon", "coming_soon", "trakt", "TRAKT_API"),
    ],
    MediaTypes.ANIME.value: [
        TabDefinition("trending", "Trending", "trending_right_now", "trakt", "TRAKT_API"),
        TabDefinition("this_season", "This Season", "mal_this_season", "mal", "MAL_API"),
        TabDefinition("last_season", "Last Season", "mal_last_season", "mal", "MAL_API"),
        TabDefinition("top_rated", "Top Rated", "mal_anime_top_rated", "mal", "MAL_API"),
        TabDefinition("top_airing", "Top Airing", "mal_anime_airing", "mal", "MAL_API"),
        TabDefinition("most_popular", "Most Popular", "mal_anime_popular", "mal", "MAL_API"),
        TabDefinition("coming_soon", "Coming Soon", "mal_anime_upcoming", "mal", "MAL_API"),
    ],
    MediaTypes.MANGA.value: [
        TabDefinition("trending", "Trending", "trending_right_now", "mal", "MAL_API"),
        TabDefinition("top_rated", "Top Rated", "all_time_greats_unseen", "mal", "MAL_API"),
        TabDefinition("publishing_now", "Publishing Now", "mal_manga_publishing", "mal", "MAL_API"),
    ],
    MediaTypes.BOOK.value: [
        TabDefinition("trending", "Trending", "trending_right_now", "openlibrary", None),
        TabDefinition("this_week", "This Week", "openlibrary_weekly", "openlibrary", None),
        TabDefinition("this_month", "This Month", "openlibrary_monthly", "openlibrary", None),
        TabDefinition("this_year", "This Year", "openlibrary_yearly", "openlibrary", None),
        TabDefinition("coming_soon", "Coming Soon", "coming_soon", "openlibrary", None),
    ],
    MediaTypes.COMIC.value: [
        TabDefinition("trending", "Trending", "trending_right_now", "comicvine", "COMICVINE_API"),
        TabDefinition("coming_soon", "Coming Soon", "coming_soon", "comicvine", "COMICVINE_API"),
    ],
    MediaTypes.BOARDGAME.value: [
        TabDefinition("trending", "Hot", "trending_right_now", "bgg", "BGG_API_TOKEN"),
    ],
    MediaTypes.MUSIC.value: [
        TabDefinition("trending", "Trending", "trending_right_now", "lastfm", "LASTFM_API_KEY"),
        TabDefinition("coming_soon", "Coming Soon", "coming_soon", "musicbrainz", None),
        TabDefinition("top_artists", "Top Artists", "lastfm_top_artists", "lastfm", "LASTFM_API_KEY"),
    ],
    MediaTypes.PODCAST.value: [
        TabDefinition("trending", "Top", "trending_right_now", "itunes", None),
    ],
    MediaTypes.GAME.value: [
        TabDefinition("trending", "Trending", "trending_right_now", "igdb", "IGDB_ID"),
        TabDefinition("top_rated", "Top Rated", "igdb_top_rated", "igdb", "IGDB_ID"),
        TabDefinition("coming_soon", "Coming Soon", "igdb_coming_soon", "igdb", "IGDB_ID"),
    ],
}


def get_tabs(media_type: str) -> list[TabDefinition]:
    """Return ordered tab definitions for the media type (empty if unsupported)."""
    return TAB_REGISTRY.get(media_type, [])


def default_tab(media_type: str) -> str | None:
    """Return the default (first) tab key for the media type."""
    tabs = get_tabs(media_type)
    return tabs[0].key if tabs else None


def get_tab(media_type: str, tab_key: str) -> TabDefinition | None:
    """Return a single tab definition, or ``None`` when not found."""
    for tab in get_tabs(media_type):
        if tab.key == tab_key:
            return tab
    return None
