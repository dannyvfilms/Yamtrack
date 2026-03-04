"""Provider interfaces for Discover."""

from __future__ import annotations

from typing import Protocol

from app.discover.schemas import CandidateItem


class DiscoverProviderAdapter(Protocol):
    """Adapter protocol used by Discover service."""

    def trending(self, media_type: str, *, limit: int = 50) -> list[CandidateItem]:
        """Return trending candidates for media type."""

    def top_rated(self, media_type: str, *, limit: int = 50) -> list[CandidateItem]:
        """Return all-time top rated candidates for media type."""

    def upcoming(self, media_type: str, *, limit: int = 50) -> list[CandidateItem]:
        """Return upcoming candidates for media type."""

    def current_cycle(self, media_type: str, *, limit: int = 50) -> list[CandidateItem]:
        """Return currently active releases (now playing/on air/seasonal)."""

    def related(self, media_type: str, media_id: str, *, limit: int = 50) -> list[CandidateItem]:
        """Return recommendation candidates related to an anchor title."""

    def genre_discovery(
        self,
        media_type: str,
        genres: list[str],
        *,
        limit: int = 100,
    ) -> list[CandidateItem]:
        """Return discovery candidates scoped to top genres."""

    def check_capability(self) -> dict[str, bool]:
        """Run lightweight capability checks for critical endpoints."""
