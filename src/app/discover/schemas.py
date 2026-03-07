"""Schemas used by the Discover feature."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CandidateItem:
    """Normalized candidate returned by Discover sources."""

    media_type: str
    source: str
    media_id: str
    title: str
    original_title: str | None = None
    localized_title: str | None = None
    image: str | None = None
    release_date: str | None = None
    activity_at: str | None = None
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)
    directors: list[str] = field(default_factory=list)
    lead_cast: list[str] = field(default_factory=list)
    collection_id: str | None = None
    collection_name: str | None = None
    certification: str | None = None
    runtime_bucket: str | None = None
    release_decade: str | None = None
    popularity: float | None = None
    rating: float | None = None
    rating_count: int | None = None
    row_key: str | None = None
    source_reason: str | None = None
    anchor_title: str | None = None
    score_breakdown: dict[str, float] = field(default_factory=dict)
    final_score: float | None = None
    display_score: float | None = None

    def identity(self) -> tuple[str, str, str]:
        """Return stable identity tuple used for dedupe/filtering."""
        return (self.media_type, self.source, str(self.media_id))

    def to_dict(self) -> dict[str, Any]:
        """Serialize candidate as JSON-compatible dict."""
        return {
            "media_type": self.media_type,
            "source": self.source,
            "media_id": str(self.media_id),
            "title": self.title,
            "original_title": self.original_title,
            "localized_title": self.localized_title,
            "image": self.image,
            "release_date": self.release_date,
            "activity_at": self.activity_at,
            "genres": list(self.genres),
            "tags": list(self.tags),
            "people": list(self.people),
            "keywords": list(self.keywords),
            "studios": list(self.studios),
            "directors": list(self.directors),
            "lead_cast": list(self.lead_cast),
            "collection_id": self.collection_id,
            "collection_name": self.collection_name,
            "certification": self.certification,
            "runtime_bucket": self.runtime_bucket,
            "release_decade": self.release_decade,
            "popularity": self.popularity,
            "rating": self.rating,
            "rating_count": self.rating_count,
            "row_key": self.row_key,
            "source_reason": self.source_reason,
            "anchor_title": self.anchor_title,
            "score_breakdown": dict(self.score_breakdown),
            "final_score": self.final_score,
            "display_score": self.display_score,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CandidateItem":
        """Create CandidateItem from dict payload."""
        return cls(
            media_type=str(payload.get("media_type", "")),
            source=str(payload.get("source", "")),
            media_id=str(payload.get("media_id", "")),
            title=str(payload.get("title", "")),
            original_title=payload.get("original_title"),
            localized_title=payload.get("localized_title"),
            image=payload.get("image"),
            release_date=payload.get("release_date"),
            activity_at=payload.get("activity_at"),
            genres=list(payload.get("genres") or []),
            tags=list(payload.get("tags") or []),
            people=list(payload.get("people") or []),
            keywords=list(payload.get("keywords") or []),
            studios=list(payload.get("studios") or []),
            directors=list(payload.get("directors") or []),
            lead_cast=list(payload.get("lead_cast") or []),
            collection_id=payload.get("collection_id"),
            collection_name=payload.get("collection_name"),
            certification=payload.get("certification"),
            runtime_bucket=payload.get("runtime_bucket"),
            release_decade=payload.get("release_decade"),
            popularity=payload.get("popularity"),
            rating=payload.get("rating"),
            rating_count=payload.get("rating_count"),
            row_key=payload.get("row_key"),
            source_reason=payload.get("source_reason"),
            anchor_title=payload.get("anchor_title"),
            score_breakdown=dict(payload.get("score_breakdown") or {}),
            final_score=payload.get("final_score"),
            display_score=payload.get("display_score"),
        )


@dataclass(frozen=True, slots=True)
class RowDefinition:
    """Declarative row definition used by registry and service."""

    key: str
    title: str
    mission: str
    why: str
    source: str
    min_items: int = 3
    show_more: bool = False
    allow_tracked: bool = False


@dataclass(slots=True)
class RowResult:
    """Rendered row payload returned to templates and row cache."""

    key: str
    title: str
    mission: str
    why: str
    source: str
    items: list[CandidateItem]
    reserve_items: list[CandidateItem] = field(default_factory=list)
    is_stale: bool = False
    show_more: bool = False
    source_state: str = "live"
    match_signal: str | None = None
    debug_payload: dict[str, Any] | None = None

    def to_dict(self, *, include_reserve: bool = False) -> dict[str, Any]:
        """Serialize row payload for DB row cache."""
        data: dict[str, Any] = {
            "key": self.key,
            "title": self.title,
            "mission": self.mission,
            "why": self.why,
            "source": self.source,
            "items": [item.to_dict() for item in self.items],
            "is_stale": self.is_stale,
            "show_more": self.show_more,
            "source_state": self.source_state,
        }
        if include_reserve and self.reserve_items:
            data["reserve_items"] = [item.to_dict() for item in self.reserve_items]
        if self.match_signal:
            data["match_signal"] = self.match_signal
        if self.debug_payload:
            data["debug_payload"] = dict(self.debug_payload)
        return data

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RowResult":
        """Deserialize row cache payload."""
        return cls(
            key=str(payload.get("key", "")),
            title=str(payload.get("title", "")),
            mission=str(payload.get("mission", "")),
            why=str(payload.get("why", "")),
            source=str(payload.get("source", "")),
            items=[
                CandidateItem.from_dict(item_payload)
                for item_payload in (payload.get("items") or [])
            ],
            reserve_items=[
                CandidateItem.from_dict(item_payload)
                for item_payload in (payload.get("reserve_items") or [])
            ],
            is_stale=bool(payload.get("is_stale", False)),
            show_more=bool(payload.get("show_more", False)),
            source_state=str(payload.get("source_state", "live")),
            match_signal=payload.get("match_signal"),
            debug_payload=payload.get("debug_payload"),
        )


@dataclass(slots=True)
class DiscoverPayload:
    """Top-level discover page payload."""

    media_type: str
    rows: list[RowResult]
    show_more: bool
