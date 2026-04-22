# Jellyfin Integration Features - Implementation Plan

## Overview

This document describes the implementation of two Jellyfin integration features that allow tracking media under the user's preferred provider (MAL/TVDB/TMDB) instead of always using TMDB.

---

## Feature #1: Provider Priority for Tracking Source

**Goal**: When enabled, track episodes in shows using the USER'S PREFERRED PROVIDER (MAL/TVDB/etc.) instead of always using TMDB.

### User-Visible Behavior

When this setting is **OFF** (default):
- Current behavior preserved: Episodes are tracked under TMDB source

When this setting is **ON**:
- For TV shows: If user's `tv_metadata_source_default` is set to TVDB, episodes get tracked under TVDB (`Item.source = "tvdb"`)
- For anime: If user's `anime_metadata_source_default` is set to MAL, episodes get tracked under MAL (`Item.source = "mal"`)
- If preferred provider ID can't be found in webhook payload → fall back to TMDB tracking (normal base class behavior)

### Example Scenario

User has `tv_metadata_source_default = "tvdb"`:
1. Jellyfin sends webhook with TVDB ID "789" (Breaking Bad)
2. System finds TVDB ID directly in payload
3. Creates/updates episode under TVDB source: `Item.source = "tvdb"`, `Item.media_id = "789"`

### Technical Implementation

**File**: `src/integrations/webhooks/jellyfin.py`

#### Method Signatures

```python
def _get_jellyfin_preferred_source(self, user, media_type) -> str | None:
    """Return the user's preferred tracking source or None if disabled."""

def _resolve_media_id_to_preferred_source(self, user, media_type, ids, season_number, episode_number):
    """Resolve media ID to user's preferred provider.
    
    Returns (media_id, source, season, episode) tuple when preferred provider
    ID is found in payload, or (None, None, None, None) for fallback.
    """

def _process_tv(self, payload, user, ids, season_number=None, episode_number=None):
    """Process TV episode webhook with priority order:
    1. Feature #2: Match existing tracked show
    2. Feature #1: Resolve to preferred provider
    3. Fallback: Normal TMDB-first via super()._process_tv()
    """

def _get_tv_metadata(self, media_id, season_numbers, source):
    """Override hook method to fetch metadata from the correct provider.
    
    Base class uses TMDB; Jellyfin subclass adds TVDB support.
    """
```

#### Key Design Decisions

1. **Direct ID matching, not cross-provider lookup**: The system checks if the user's preferred provider ID exists *directly* in the webhook payload. No cross-provider API lookups needed.
2. **Fallback via base class**: When `_resolve_media_id_to_preferred_source` returns `(None, None, None, None)`, `_process_tv` calls `super()._process_tv()` which handles normal TMDB-first processing.
3. **Provider-aware metadata**: `_handle_tv_episode(source=...)` in the base class accepts an optional `source` parameter, and `_get_tv_metadata()` hook ensures metadata comes from the correct provider.

---

## Feature #2: Match Existing Tracked Items

**Goal**: Before creating new entries, check if the show is already tracked by the user (under ANY provider). If found, update progress under THAT provider's source.

### User-Visible Behavior

When this setting is **OFF** (default):
- Current behavior preserved: Always create new entries based on resolved IDs

When this setting is **ON**:
- Search for existing items by ALL known provider IDs (TMDB, TVDB, MAL, IMDB)
- If show is tracked under MAL but Jellyfin sends TMDB ID → update MAL entry
- Preserves original identity provider (`Item.source`)
- Prevents duplicate entries when same show tracked under different providers

### Processing Priority Order

1. **Feature #2 first**: Check for existing tracked show by any provider ID
   - If found: Update progress under THAT show's source
   - If not found: Continue to Feature #1
   
2. **Feature #1 second**: Resolve media ID to user's preferred provider
   - If successful: Track under preferred provider
   - If failed: Continue to fallback
   
3. **Fallback**: Normal TMDB-first processing (base class behavior)

### Example Scenario

User has "Breaking Bad" tracked under MAL (`Item.source = "mal"`, `Item.media_id = "4501"`):
1. Jellyfin sends webhook with TMDB ID "1396"
2. Feature #2 checks: Does user have an item with tmdb_id="1396"?
   - Yes, via ItemProviderLink table
3. Update progress under MAL source: `Item.source = "mal"`, `Item.media_id = "4501"`
4. Episode gets marked as watched on the MAL entry

### Technical Implementation

**File**: `src/integrations/webhooks/jellyfin.py`

#### Method Signatures

```python
def _find_existing_item(self, user, media_type, ids, season_number=None, episode_number=None):
    """Find existing tracked item by ANY known provider ID.
    
    Searches direct Item fields and ItemProviderLink table.
    Returns (item, created) tuple where created=False if match found.
    """

def _update_existing_item(self, item, payload, user):
    """Update progress on existing item without changing its identity provider."""

def _update_movie_instance(self, item, user, played, now):
    """Create or update a Movie tracking instance."""

def _update_tv_season_episode(self, item, payload, user, played, now):
    """Create or update Season/Episode tracking instances for existing TV show."""

def _process_movie(self, payload, user, ids):
    """Process movie webhook with Feature #2 check before falling to base class."""
```

---

## Architecture Summary

### Inheritance Chain

```
BaseWebhookProcessor._handle_tv_episode(media_id, season, episode, payload, user, *, source=None)
    ↓ _get_tv_metadata() hook
JellyfinWebhookProcessor._get_tv_metadata(media_id, season_numbers, source)
    ├── TMDB → app.providers.tmdb.tv_with_seasons()
    └── TVDB → app.providers.tvdb.tv_with_seasons()
```

### Key Methods

| Method | File | Purpose |
|--------|------|---------|
| `_get_jellyfin_preferred_source()` | jellyfin.py | Returns user's preferred source or `None` |
| `_resolve_media_id_to_preferred_source()` | jellyfin.py | Checks if preferred provider ID exists in payload |
| `_find_existing_item()` | jellyfin.py | Searches all sources for existing tracked items |
| `_process_tv()` | jellyfin.py | Routes through Feature #2 → Feature #1 → Fallback |
| `_handle_tv_episode()` | base.py | Core item/season/episode creation logic |
| `_get_tv_metadata()` | base.py/jellyfin.py | Hook for provider-specific metadata fetching |

---

## Live Playback State / Display

Both features also apply to the live playback state (the "Now Watching" home-page card).

When a Play/Pause/Stop event arrives from Jellyfin, `_update_live_playback_state`
resolves the `media_id` and `source` for the card using the same priority:

1. **`jellyfin_match_existing_enabled`**: If the user already tracks the show under
   a different provider, the card uses *that* provider's ID and source so the UI
   matches the tracking identity.
2. **`jellyfin_provider_priority_enabled`**: If no existing item is found but the
   user has a preferred metadata provider, the card uses the preferred provider's
   ID from the payload.
3. **Fallback**: TMDB ID from the payload (normal behavior).

This ensures the home-page card always reflects the same identity as the tracking
records, avoiding mismatches where the card shows "TMDB" but progress is tracked
under "MAL" or "TVDB".

---

## Files Modified

1. **`src/integrations/webhooks/base.py`**: Added `source` parameter to `_handle_tv_episode()`, added `_get_tv_metadata()` hook
2. **`src/integrations/webhooks/jellyfin.py`**: Complete rewrite with Feature #1 and #2 implementations
3. **`src/users/models.py`**: Added `jellyfin_provider_priority_enabled` and `jellyfin_match_existing_enabled` fields
4. **`src/users/migrations/0090_add_jellyfin_settings.py`**: Migration file
5. **`src/users/views.py`**: Added `update_jellyfin_settings()` POST handler
6. **`src/templates/users/integrations.html`**: Added UI toggle switches
7. **`docs/agents/jellyfin_integration.md`**: Updated documentation

---

## Key Clarifications

1. **Feature #1 uses direct ID matching**: Not cross-provider lookup. The system checks if the user's preferred provider ID exists directly in the webhook payload.
2. **Feature #2 preserves original source**: When finding an existing tracked show, updates go to THAT show's existing source.
3. **Processing priority**: Feature #2 (match existing) > Feature #1 (preferred source) > Fallback (TMDB)
4. **No redundant fallback logic**: `_get_jellyfin_preferred_source()` returns `None` for disabled settings, letting `_resolve_media_id_to_preferred_source()` naturally fall through to base class behavior.

---

## Testing Strategy

### Test Cases for Feature #1

```python
def test_feature_1_tracks_under_preferred_source():
    """If user prefers TVDB, webhooks should track under TVDB when TVDB ID is in payload."""
    user.tv_metadata_source_default = "tvdb"
    user.jellyfin_provider_priority_enabled = True
    user.save()
    
    # Jellyfin sends TVDB ID
    payload = {..., "ProviderIds": {"Tvdb": "789"}, ...}
    
    # Should create/update item with source="tvdb" -> YES!
```

### Test Cases for Feature #2

```python
def test_feature_2_updates_existing_mal_entry():
    """If user has show tracked under MAL, updates should go to MAL entry."""
    mal_item = Item.objects.create(
        media_id="4501",
        source=Sources.MAL.value,
        media_type=MediaTypes.TV.value,
        title="Breaking Bad",
        user=user,
    )
    TV.objects.create(item=mal_item, user=user, status=Status.IN_PROGRESS.value)
    
    # Jellyfin sends TMDB ID for same show
    payload = {..., "ProviderIds": {"Tmdb": "1396"}, ...}
    
    # Should update MAL entry, not create new TMDB entry
```
