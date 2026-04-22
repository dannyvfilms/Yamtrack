# Jellyfin Integration Features - Final Implementation Plan

## Overview

This document describes the correct implementation approach for two Jellyfin integration features based on the final clarification from the user.

---

## Feature #1: Provider Priority for Tracking Source

**Goal**: When enabled, track episodes in shows using the USER'S PREFERRED PROVIDER (MAL/TVDB/etc.) instead of always using TMDB.

### User-Visible Behavior

When this setting is **OFF** (default):
- Current behavior preserved: Episodes are tracked under TMDB source

When this setting is **ON**:
- For TV shows: If user's `tv_metadata_source_default` is set to MAL, episodes get tracked under MAL (`Item.source = "mal"`)
- For anime: If user's `anime_metadata_source_default` is set to MAL, episodes get tracked under MAL
- If preferred provider ID can't be found/resolved → fall back to TMDB tracking

### Example Scenario

User has `tv_metadata_source_default = "mal"`:
1. Jellyfin sends webhook with TMDB ID "1396" (Breaking Bad)
2. System looks up Breaking Bad's MAL ID via cross-provider mapping
3. Creates/updates episode under MAL source: `Item.source = "mal"`, `Item.media_id = "4501"`

### Technical Implementation

**File**: `src/integrations/webhooks/jellyfin.py`

Add methods to resolve media IDs to user's preferred provider:

```python
def _get_jellyfin_provider_priority(self, user, media_type):
    """Return ordered list of providers to try for webhook resolution.
    
    Returns ['mal', 'tmdb', 'tvdb'] if user prefers MAL for anime.
    Falls back to ['tmdb', 'tvdb', 'imdb'] when disabled or for movies.
    """
    if not getattr(user, 'jellyfin_provider_priority_enabled', False):
        return [Sources.TMDB.value, Sources.TVDB.value, Sources.IMDB.value]
    
    if media_type == MediaTypes.TV.value:
        preferred = getattr(user, 'tv_metadata_source_default', Sources.TMDB.value)
    elif media_type == MediaTypes.ANIME.value:
        preferred = getattr(user, 'anime_metadata_source_default', Sources.MAL.value)
    else:  # Movie - always use TMDB
        return [Sources.TMDB.value, Sources.TVDB.value, Sources.IMDB.value]
    
    all_providers = [Sources.TMDB.value, Sources.TVDB.value, Sources.MAL.value]
    if preferred in all_providers:
        return [preferred] + [p for p in all_providers if p != preferred]
    return [Sources.TMDB.value, Sources.TVDB.value, Sources.IMDB.value]

def _resolve_media_id_to_preferred_source(self, user, media_type, ids, season_number, episode_number):
    """Resolve media ID to user's preferred provider and return (media_id, source).
    
    If jellyfin_provider_priority_enabled is True:
    - Try to find the show's ID in user's preferred provider
    - Return (preferred_media_id, preferred_source) tuple
    - Fall back to (tmdb_id, 'tmdb') if preferred provider not available
    
    Returns (None, None) if fallback needed.
    """
    if not getattr(user, 'jellyfin_provider_priority_enabled', False):
        return None, None
    
    provider_order = self._get_jellyfin_provider_priority(user, media_type)
    
    for provider in provider_order:
        ext_id = ids.get(f"{provider}_id")
        if not ext_id:
            continue
        
        try:
            if provider == Sources.MAL.value and media_type == MediaTypes.ANIME.value:
                # Found MAL ID directly - use it!
                return str(ext_id), Sources.MAL.value
            
            elif provider == Sources.TMDB.value:
                # Need to find equivalent MAL/TVDB ID
                # Use cross-provider lookup via metadata_resolution or API
                tmdb_id = int(ext_id)
                
                # Try to find MAL ID via TMDB's find API with tvdb_id parameter
                # Or use existing ItemProviderLink table
                # This requires looking up the show's metadata to get external IDs
                
                # For now, check if we have any existing item with this TMDB ID
                # that has a different source preference
                pass
            
            # Continue trying other providers...
            
        except Exception as exc:
            logger.debug("Failed lookup via %s: %s", provider, exc)
            continue
    
    # No provider succeeded - indicate fallback needed
    return None, None
```

**Key Point**: Don't convert everything to TMDB. Instead:
1. Extract preferred provider ID from payload if available
2. If only TMDB provided, look up cross-provider mapping to find preferred provider's ID
3. Create/update items using the preferred provider's source

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

Modify `_find_existing_item()` to search across ALL sources:

```python
def _find_existing_item(self, user, media_type, ids, season_number=None, episode_number=None):
    """Find existing tracked item by ANY known provider ID.
    
    Searches for items matching tmdb_id, tvdb_id, imdb_id, mal_id, etc.
    Only returns items where the user has a tracking instance with a status.
    
    Returns (item, created) tuple where created=False if match found.
    """
    if not getattr(user, 'jellyfin_match_existing_enabled', False):
        return None, True
    
    from django.db.models import Q
    from app.models import ItemProviderLink, Movie, TV
    
    # Helper to check if user has tracking instance for an item
    def has_tracking_instance(item):
        if media_type == MediaTypes.MOVIE.value:
            return Movie.objects.filter(item=item, user=user).exists()
        elif media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
            return TV.objects.filter(item=item, user=user).exists()
        return False
    
    # Search by direct ID matches across all sources
    id_sources = [
        ('tmdb_id', Sources.TMDB.value),
        ('tvdb_id', Sources.TVDB.value),
        ('imdb_id', Sources.TMDB.value),  # IMDB stored on TMDB items
    ]
    
    if media_type == MediaTypes.ANIME.value:
        id_sources.append(('mal_id', Sources.MAL.value))
    
    for field_name, source in id_sources:
        if ids.get(field_name):
            try:
                if field_name == 'imdb_id':
                    # IMDB stored in provider_external_ids
                    item = Item.objects.get(
                        media_type=media_type,
                        source=source,
                        provider_external_ids__contains={field_name: str(ids[field_name])},
                    )
                else:
                    item = Item.objects.get(
                        media_type=media_type,
                        source=source,
                        media_id=ids[field_name],
                    )
                
                if item.user == user and has_tracking_instance(item):
                    return item, False
            except Item.DoesNotExist:
                pass
    
    # Also check ItemProviderLink for cross-provider mappings
    if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        provider_links = ItemProviderLink.objects.filter(
            provider__in=['tmdb', 'tvdb', 'mal', 'igdb'],
            provider_media_type=media_type,
        )
        
        for link in provider_links[:10]:
            try:
                item = link.item
                if item.user == user and item.media_type == media_type and has_tracking_instance(item):
                    return item, False
            except:
                continue
    
    return None, True
```

Then in `_process_tv()`:

```python
def _process_tv(self, payload, user, ids, season_number=None, episode_number=None):
    """Process TV episode webhook with priority order:
    1. Feature #2: Match existing tracked show
    2. Feature #1: Resolve to preferred provider
    3. Fallback: Normal TMDB-first
    """
    # Feature #2: Check for existing tracked show FIRST
    existing_item, created = self._find_existing_item(
        user, MediaTypes.TV.value, ids, season_number, episode_number
    )
    
    if existing_item and not created:
        logger.info("Found existing item for TV episode, updating under %s", existing_item.source)
        # Update progress under EXISTING item's source (not forced to TMDB)
        self._update_existing_item(existing_item, payload, user)
        return
    
    # Feature #1: Try to resolve to user's preferred provider
    resolved_media_id, resolved_source = self._resolve_media_id_to_preferred_source(
        user, MediaTypes.TV.value, ids, season_number, episode_number
    )
    
    if resolved_media_id and resolved_source:
        # Track under user's preferred provider
        logger.info("Tracking under preferred provider: %s", resolved_source)
        # Call custom handler that creates items with resolved_source
        self._handle_tv_episode_with_source(resolved_media_id, resolved_source, season_number, episode_number, payload, user)
        return
    
    # Fallback: Normal TMDB-first processing
    super()._process_tv(payload, user, ids, season_number, episode_number)
```

---

## Files Already Modified

These files have been partially implemented and may need adjustment:

1. **`src/users/models.py`**: Added `jellyfin_provider_priority_enabled` and `jellyfin_match_existing_enabled` fields ✓
2. **`src/users/migrations/0090_add_jellyfin_settings.py`**: Migration file created ✓
3. **`src/users/views.py`**: Added `update_jellyfin_settings()` POST handler ✓
4. **`src/templates/users/integrations.html`**: Added UI toggle switches ✓

---

## Files That Need Complete Rewrite

1. **`src/integrations/webhooks/jellyfin.py`**: 
   - Current `_find_tv_media_id()` tries to resolve to TMDB - needs rewrite
   - Current `_process_tv()` calls parent which forces TMDB - needs rewrite
   - Need `_handle_tv_episode_with_source()` method to handle arbitrary source creation

2. **`docs/agents/jellyfin_integration.md`**: Documentation needs updating

---

## Key Clarifications

1. **Feature #1 does NOT convert to TMDB**: It should create/update items using the USER'S PREFERRED SOURCE (MAL/TVDB/etc.), not convert everything to TMDB.

2. **Feature #2 preserves original source**: When finding an existing tracked show, update progress under THAT show's existing source, don't force it to TMDB.

3. **Processing priority**: Feature #2 (match existing) > Feature #1 (preferred source) > Fallback (TMDB)

4. **Cross-provider lookup**: To implement Feature #1, need to:
   - Look up existing items' external IDs via `ItemProviderLink` table
   - Or use TMDB/TVDB/MAL APIs to find cross-provider mappings
   - The key insight: if user prefers MAL and Jellyfin sends TMDB ID, find the show's MAL ID via cross-provider lookup

---

## Testing Strategy

### Test Cases for Feature #1

```python
def test_feature_1_tracks_under_preferred_source():
    """If user prefers MAL for anime, webhooks should track under MAL."""
    user.tv_metadata_source_default = "mal"
    user.jellyfin_provider_priority_enabled = True
    user.save()
    
    # Jellyfin sends TMDB ID
    payload = {..., "ProviderIds": {"Tmdb": "1396"}, ...}
    
    # Should create/update item with source="tmdb" -> NO!
    # Should create/update item with source="mal" -> YES!
```

### Test Cases for Feature #2

```python
def test_feature_2_updates_existing_mal_entry():
    """If user has show tracked under MAL, updates should go to MAL entry."""
    # Create existing MAL entry
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

---

## Next Steps for New AI Session

1. Read the current state of `src/integrations/webhooks/jellyfin.py`
2. Understand how `_handle_tv_episode` creates items with fixed TMDB source
3. Create new methods that allow specifying arbitrary source during item creation
4. Implement `_find_existing_item()` to search across ALL sources
5. Implement `_resolve_media_id_to_preferred_source()` for cross-provider lookup
6. Modify `_process_tv()` to follow priority order: Feature #2 > Feature #1 > Fallback
7. Update documentation
