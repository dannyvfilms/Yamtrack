"""Shared adapter instances for Discover providers."""

from app.discover.providers.tmdb_adapter import TMDbDiscoverAdapter
from app.discover.providers.trakt_adapter import TraktDiscoverAdapter

TMDB_ADAPTER = TMDbDiscoverAdapter()
TRAKT_ADAPTER = TraktDiscoverAdapter()
