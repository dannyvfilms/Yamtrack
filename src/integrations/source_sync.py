"""Helpers for reconciling collection metadata across multiple sources."""

from django.utils import timezone

from app.models import CollectionEntry
from integrations.imports.helpers import retry_on_lock
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


def _get_best_state(states):
    """Return the preferred source state for a collection entry."""
    best_state = None

    for state in states:
        if best_state is None:
            best_state = state
            continue

        state_rank = _quality_rank(state.quality_label)
        best_rank = _quality_rank(best_state.quality_label)
        if state_rank > best_rank:
            best_state = state
            continue

        if state_rank == best_rank and (
            state.last_source_updated_at or timezone.now()
        ) > (best_state.last_source_updated_at or timezone.now()):
            best_state = state

    return best_state


def _collection_entry_queryset(*, user, item):
    return CollectionEntry.objects.filter(user=user, item=item).order_by(
        "-updated_at",
        "-collected_at",
        "-id",
    )


def _entry_is_source_synced_placeholder(entry) -> bool:
    """Return True when an entry only contains source-sync-managed defaults."""
    return not any(
        [
            entry.media_type,
            entry.hdr,
            entry.audio_codec,
            entry.audio_channels,
            entry.bitrate is not None,
            entry.is_3d,
            entry.plex_rating_key,
            entry.plex_uri,
            entry.plex_rating_key_updated_at,
        ],
    )


def _reconcile_collection_entry(*, user, item):
    """Apply the current source-state snapshot to the durable collection entry."""
    states = list(CollectionSourceState.objects.filter(user=user, item=item))
    entries = _collection_entry_queryset(user=user, item=item)
    entry = entries.first()

    if not states:
        if (
            entry
            and entries.count() == 1
            and _entry_is_source_synced_placeholder(entry)
        ):
            entry.delete()
            return None
        return entry

    if entry is None:
        entry = CollectionEntry.objects.create(user=user, item=item)

    best_state = _get_best_state(states)
    resolved_quality = (best_state.quality_label if best_state else "") or ""
    if entry.resolution != resolved_quality:
        entry.resolution = resolved_quality
        entry.save(update_fields=["resolution", "updated_at"])

    return entry


def upsert_collection_source_state(
    *,
    user,
    item,
    source: str,
    quality_label: str = "",
    source_updated_at=None,
):
    """Persist a source snapshot and reconcile the durable collection entry."""
    if source_updated_at is None:
        source_updated_at = timezone.now()

    def _upsert_state():
        CollectionSourceState.objects.update_or_create(
            user=user,
            item=item,
            source=source,
            defaults={
                "quality_label": quality_label or "",
                "last_source_updated_at": source_updated_at,
            },
        )
        return _reconcile_collection_entry(user=user, item=item)

    return retry_on_lock(_upsert_state)


def remove_collection_source_state(*, user, item, source: str):
    """Remove a source-specific collection snapshot and reconcile the durable entry."""
    def _remove_state():
        CollectionSourceState.objects.filter(
            user=user,
            item=item,
            source=source,
        ).delete()
        return _reconcile_collection_entry(user=user, item=item)

    return retry_on_lock(_remove_state)
