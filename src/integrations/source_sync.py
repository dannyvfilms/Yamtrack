"""Helpers for reconciling collection metadata across multiple sources."""

from django.utils import timezone

from app.models import CollectionEntry
from integrations.models import CollectionSourceState

QUALITY_RANKS = {
    "480p": 1,
    "576p": 2,
    "720p": 3,
    "1080p": 4,
    "1440p": 5,
    "2160p": 6,
    "4k": 6,
}


def _quality_rank(label: str) -> int:
    normalized = str(label or "").strip().lower()
    if not normalized:
        return 0
    if normalized in QUALITY_RANKS:
        return QUALITY_RANKS[normalized]
    for key, rank in QUALITY_RANKS.items():
        if key in normalized:
            return rank
    return 0


def upsert_collection_source_state(*, user, item, source: str, quality_label: str = "", source_updated_at=None):
    """Persist a source-specific collection snapshot and reconcile CollectionEntry quality."""
    if source_updated_at is None:
        source_updated_at = timezone.now()

    CollectionSourceState.objects.update_or_create(
        user=user,
        item=item,
        source=source,
        defaults={
            "quality_label": quality_label or "",
            "last_source_updated_at": source_updated_at,
        },
    )

    entry, created = CollectionEntry.objects.get_or_create(user=user, item=item)
    all_states = CollectionSourceState.objects.filter(user=user, item=item)

    best_state = None
    for state in all_states:
        if best_state is None:
            best_state = state
            continue
        state_rank = _quality_rank(state.quality_label)
        best_rank = _quality_rank(best_state.quality_label)
        if state_rank > best_rank:
            best_state = state
            continue
        if state_rank == best_rank and (state.last_source_updated_at or timezone.now()) > (
            best_state.last_source_updated_at or timezone.now()
        ):
            best_state = state

    resolved_quality = (best_state.quality_label if best_state else "") or ""
    if created or entry.resolution != resolved_quality:
        entry.resolution = resolved_quality
        entry.save(update_fields=["resolution", "updated_at"])

    return entry
