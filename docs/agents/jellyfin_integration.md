# Jellyfin Integration & Webhook Logic

This document details the logic for processing Jellyfin real-time webhooks in Yamtrack. It focuses on how media identity is resolved, event handling semantics, and how the system ensures data consistency with other providers (TMDB). Note that unlike Plex, Jellyfin support currently consists only of webhook processing—no history import functionality exists.

## Core Concepts

### 1. Identity Resolution

The system prioritizes **TMDB IDs** as the canonical source of truth. All Jellyfin entries (from webhooks) must eventually resolve to a valid TMDB ID (`tmdb_id`) to be stored.

**Resolution Priority:**

1.  **Explicit TMDB ID**: Extracted directly from Jellyfin `ProviderIds.Tmdb`.
2.  **IMDB/TVDB Lookup**: If only IMDB (`tt123`) or TVDB (`789`) IDs are present, the system queries the TMDB `find` API to resolve the corresponding `tmdb_id`.
3.  **Title Search Fallback**:
    -   If no external IDs are found (or they return 404s), the system performs a search against TMDB using the media title.
    -   **TV Shows**: Uses `SeriesName` from the payload. Match attempts to filter by year if available.
    -   **Movies**: Uses `Name` and `ProductionYear`.

**GUID extraction & conflicts:**

-   Jellyfin payloads use `ProviderIds` dictionary directly (e.g., `{"Tmdb": "123", "Imdb": "tt456"}`), not GUID arrays like Plex. There is no GUID ordering ambiguity.
-   For TV shows, if an episode-level TMDB ID is provided but cannot be used directly as a show ID, the system falls back to TVDB/IMDB lookups or title search to resolve the correct show ID.

**TV `tmdb_id` semantics:**

-   For TV webhook entries, `tmdb_id` represents the **show** TMDB ID, with season/episode numbers extracted separately from `ParentIndexNumber` and `IndexNumber`.
-   If Jellyfin provides episode-level TMDB IDs or non-standard numbering (anime, specials, absolute order), resolution can fall back to title search during metadata lookup; unresolved episodes are skipped.

**Title search policy:**

-   Searches use `TMDB_LANG` (default `en`) with no region override and no original-title fallback.
-   When search results include dates and Jellyfin provides a year (`ProductionYear`), the first result whose `first_air_date`/`release_date` matches that year is used. Otherwise the top result is accepted as a best guess.
-   There is no strict exact-title or uniqueness requirement; if multiple plausible hits remain, the first result wins.

**TMDB 404 handling:**

-   TV metadata lookups that 404 will attempt a title-search remap (and use the remapped TMDB ID if found). Movie metadata 404s are treated as "not found" and the entry is skipped unless a title-search resolved the ID earlier.

**TMDB rate limits & caching:**

-   `find`, `search`, and movie/TV/season metadata are cached via Django cache (default 24h). The webhook processor also keeps in-memory caches per run.
-   Requests go through a shared Redis-backed limiter (~5 req/s). HTTP 429 honors `Retry-After` plus a small buffer and retries; other HTTP errors surface as `ProviderAPIError` and are logged or bubbled up.

### 2. Data Contract & Invariants

Jellyfin webhook processing uses incoming JSON payloads as the canonical event stream. The processor treats TMDB-backed IDs as stable keys for dedupe and overwrite behavior.

**Webhook endpoint & authentication:**

-   Endpoint: `POST /webhook/jellyfin/<token>` where `<token>` is the user's authentication token.
-   Authentication: Token-based; invalid tokens return HTTP 401.
-   Payload format: JSON object with `Event` and `Item` keys, structured according to the Jellyfin webhook plugin template.

**Fields we rely on:**

-   IDs: `ProviderIds.Tmdb`, `ProviderIds.Imdb`, `ProviderIds.Tvdb` from the `Item` object are required for deterministic resolution. If missing, title search fallback is attempted.
-   Titles: `Name` (for movies) or `SeriesName` (for TV) is required for title-search fallback; Jellyfin-only entries without a title are skipped.
-   Timing: `UserData.Played` indicates whether the item was marked as watched. Playback position (`PlaybackPositionTicks`) and duration (`RunTimeTicks`) are used for live playback state tracking.
-   TV structure: `ParentIndexNumber` (season) and `IndexNumber` (episode) must be numeric. Missing numbers can cause the entry to be skipped or treated as a movie in show libraries.
-   Item type: `Type` field determines media classification (`Episode`, `Movie`).

**Dedupe keys:**

-   Jellyfin does not use explicit dedupe keys like Plex's `watched_at_minute`. For episodes, duplicate detection compares the `end_date` of the latest recorded play with the current timestamp—if within 5 seconds, the record is skipped.
-   Because webhooks are best-effort and may deliver duplicates, the system uses temporal proximity rather than precise timestamps for deduplication.

### 3. Import Semantics

The Jellyfin Webhook Processor (`src/integrations/webhooks/jellyfin.py`) is designed to handle real-time playback events from Jellyfin servers.

-   **Event Types Supported**: `Play`, `Pause`, and `Stop`. Only `Stop` events trigger media tracking updates; `Play` and `Pause` update live playback state for UI purposes.
-   **User Scoping**: Webhooks are scoped to the user associated with the token in the URL path. Each user has their own webhook endpoint.
-   **Mark-as-Watched Behavior**: Entries with `UserData.Played == true` are treated as completed watches. The system creates or updates items accordingly (e.g., marks episodes as watched with an `end_date`, sets movie `progress` to 1).

## Hybrid Library Handling (Movies in TV Libraries)

Jellyfin, like Plex, allows libraries containing both Movies and TV Shows, but the webhook payload's `Type` field is more reliable for distinguishing them.

**Logic Flow:**

1.  **Initial Detection**: The `Item.Type` field determines whether the entry is an `Episode` or `Movie`.
2.  **Type Validation**: Episode entries require `SeriesName`, `ParentIndexNumber`, and `IndexNumber`. Missing values may cause the entry to be skipped.
3.  **Fallback**: If an episode-level lookup fails (e.g., wrong TMDB ID), the system attempts to resolve via TVDB/IMDB or title search.

**Other Jellyfin-specific shapes:**

-   Multi-episode files are not split; only the single `ParentIndexNumber`/`IndexNumber` pair is used.
-   Unsupported item types (Trailers, Playlists, etc.) are ignored.

## Webhook Processing

Real-time webhooks (`src/integrations/webhooks/jellyfin.py`) process incoming Jellyfin playback events.

-   **Scrobble vs. Play**: Unlike Plex which uses `media.scrobble` (90% completion), Jellyfin webhooks rely on the `Stop` event combined with `UserData.Played`. If `Played` is true, the entry is treated as completed.
-   **User Mapping**: The webhook token selects the Yamtrack user directly. There is no username matching step like Plex (which requires `Account.title` to match `user.plex_usernames`).
-   **Deduplication**: Episode scrobbles within 5 seconds of the last recorded play are treated as duplicates. This prevents bursty duplicate deliveries from corrupting progress state.
-   **Dedupe Intent**: Webhook dedupe is tuned for bursty duplicate deliveries common with Jellyfin's webhook plugin behavior.
-   **Reliability**: Webhooks are best-effort with no ordering guarantees. If an event is missed, users should restart playback to trigger a new event.
-   **Metadata**: Webhooks fetch TMDB metadata inline (cached); there is no separate refresh queue.

### Time Conversion

Jellyfin uses 100-nanosecond ticks for timing fields. The helper function `_ticks_to_seconds()` converts these to whole seconds:

```python
def _ticks_to_seconds(ticks) -> int | None:
    """Convert Jellyfin 100-nanosecond ticks to whole seconds."""
    if ticks is None:
        return None
    try:
        return max(0, int(ticks) // 10_000_000)
    except (TypeError, ValueError):
        return None
```

This affects:
-   `RunTimeTicks`: Duration of the media item
-   `PlaybackPositionTicks`: Current playback position

## Live Playback State

Jellyfin webhooks also update the live playback card shown on the home page. This state is managed via Django's cache framework (configurable backend, often Redis) and includes:

-   Media being played (movie or episode)
-   Current playback position
-   Total duration
-   User context

The `_update_live_playback_state()` method maps Jellyfin events to Yamtrack's internal event types (`media.play`, `media.pause`, `media.stop`) and applies them via `live_playback.apply_playback_event()`.

## Configuration & Setup

### Webhook Plugin Installation

Two installation paths are documented in the UI (`users/integrations.html`):

**Official Repository:**

1.  Install TMDB and TVDB metadata providers in Jellyfin.
2.  Install the Webhook plugin from the 'Jellyfin Stable' repo via Plugin Catalog.
3.  Configure notification settings:
    -   Notification Type: Playback Start and Playback Stop
    -   Events: Play and Stop
    -   Template: Custom JSON template (provided in UI)

**Unofficial Repository:**

1.  Add the unofficial plugin repository: `https://raw.githubusercontent.com/shemanaev/jellyfin-plugin-repo/master/manifest.json`
2.  Install the Webhook plugin from Plugins Catalog.
3.  Configure with default payload format.

### Webhook URL Format

```
{{ request.scheme }}://{{ request.get_host }}/webhook/jellyfin/{{ request.user.token }}
```

Users copy this URL into their Jellyfin webhook configuration along with the provided JSON template.

## Jellyfin Settings

Two optional settings can be configured in Settings → Integrations to customize how Jellyfin webhooks are processed:

### Feature #1: Provider Priority for Tracking Source (jellyfin_provider_priority_enabled)

When **enabled** (`jellyfin_provider_priority_enabled`), webhooks will attempt to track episodes under the user's preferred metadata provider (MAL / TVDB) rather than always using TMDB as the identity provider.

**Processing priority order (when both Feature #1 and Feature #2 are enabled):**

1. **Feature #2 first**: Check for existing tracked show by any provider ID
   - If found: Update progress under THAT show's source
   - If not found: Continue to Feature #1
   
2. **Feature #1 second**: Resolve media ID to user's preferred provider
   - If successful: Track under preferred provider
   - If failed: Continue to fallback
   
3. **Fallback**: Normal TMDB-first processing

**Behavior when enabled:**
- For TV shows: If `tv_metadata_source_default` is MAL, episodes get tracked under MAL (`Item.source = "mal"`)
- For anime: If `anime_metadata_source_default` is MAL, episodes get tracked under MAL
- If preferred provider ID can't be found/resolved → fall back to TMDB tracking

**Cross-provider resolution:**
- When Jellyfin provides only TMDB ID but user prefers MAL: System looks up the show's MAL ID via `ItemProviderLink` table or MAL/TMDB APIs
- The resolved MAL ID becomes the `Item.source` for the new entry

**Example scenario:**
1. User has `tv_metadata_source_default = "mal"` and `jellyfin_provider_priority_enabled = True`
2. Jellyfin sends webhook with TMDB ID "1396" (Breaking Bad)
3. System looks up Breaking Bad's MAL ID via cross-provider mapping
4. Creates episode under MAL source: `Item.source = "mal"`, `Item.media_id = "4501"`

**Use case:** Users who primarily track via MAL/TVDB but watch through Jellyfin (which provides TMDB IDs) want their library organized by their preferred provider rather than TMDB.

### Feature #2: Match Existing Tracked Items

When **enabled** (`jellyfin_match_existing_enabled`), attempts to find already-tracked items matching the incoming webhook data by ANY known provider ID (TMDB, TVDB, MAL, IMDB, etc.) instead of always creating new entries.

**Behavior:**
- Searches for existing items by all known external IDs in the payload
- Uses `ItemProviderLink` table to resolve cross-provider mappings
- Searches direct `Item` fields: `media_id` + `source`, and `provider_external_ids` JSON field
- If found, updates progress/history on existing item while preserving its original identity provider (`Item.source`)
- Prevents duplicate entries when the same show is tracked under different providers

**Search strategy:**
1. Direct lookups by source + ID (TMDB, TVDB, MAL for anime)
2. IMDB lookups via `provider_external_ids` JSON field
3. Cross-provider lookup via `ItemProviderLink` table (limited to 20 results)

**Example scenario:**
1. You add "Breaking Bad" via MAL → `Item.source = "mal"`, `Item.media_id = "4501"`
2. Jellyfin sends webhook with TMDB ID "1396"
3. With this setting ON: Finds existing MAL entry via `ItemProviderLink` table, updates progress
4. With this setting OFF: Creates new entry `Item.source = "tmdb"`, `Item.media_id = "1396"` (duplicate!)

**Note:** This feature takes priority over Feature #1 in the processing pipeline.

## Troubleshooting & Logging

### Common Log Messages

-   **`Ignoring Jellyfin webhook call because no ID was found.`**: The entry had no ProviderIds and failed title search fallback. Ensure Jellyfin items have proper metadata agent matches (TMDB/IMDB/TVDB).
-   **`No matching TMDB ID found for TV show`**: External IDs were present but TMDB lookups failed, and title search did not find a match. Check item metadata in Jellyfin.
-   **`Could not determine season/episode numbers for webhook payload`**: The episode lacked `ParentIndexNumber` or `IndexNumber`. Verify library configuration in Jellyfin.

### Debugging

If webhooks are skipping unexpectedly:

1.  Enable `DEBUG` logging for `integrations.webhooks.jellyfin` and `app.providers.services`.
2.  Verify the webhook payload structure by adding temporary logging in `process_payload()`.
3.  Confirm Jellyfin webhook plugin is configured for both "Play" and "Stop" events.
4.  Check that items have TMDB/IMDB/TVDB provider IDs assigned (not just local filenames).

## Differences from Plex Integration

| Aspect | Plex | Jellyfin |
|--------|------|----------|
| History Import | Yes (`src/integrations/imports/plex.py`) | Not implemented |
| Webhook Event Filter | `media.scrobble` only (90%) | All events; `UserData.Played` determines completion |
| User Mapping | Username matching via `Account.title` | Direct token-based auth |
| GUID Extraction | `Guid` array with ordering priority | `ProviderIds` dictionary (direct access) |
| Dedupe Strategy | Minute-based truncation + rating key | 5-second `end_date` comparison for episode records |
| Time Units | Epoch seconds | 100-nanosecond ticks |
| Hybrid Library Handling | Special case logic for Anime/docu libraries | Relies on `Type` field from payload |

## File Structure

-   `src/integrations/webhooks/jellyfin.py`: Main webhook processor class
-   `src/integrations/views.py`: HTTP endpoint handler (`jellyfin_webhook`)
-   `src/integrations/urls.py`: URL routing configuration
-   `src/integrations/tests/test_webhooks_jellyfin.py`: Unit and integration tests
-   `src/app/live_playback.py`: Live playback state management (shared with Plex)
-   `src/templates/users/integrations.html`: UI configuration page for users

## Future Considerations

-   **History Import**: A future enhancement could add a Jellyfin history importer similar to Plex, polling `/status/sessions/history/all` endpoint.
-   **Library Filtering**: Like Plex, Jellyfin could support filtering imports by library section ID.
-   **Username Mapping**: Could add optional username validation similar to Plex's `plex_usernames` feature.
