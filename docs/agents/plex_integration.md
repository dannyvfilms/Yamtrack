# Plex Integration & Import Logic

This document details the logic for importing Plex watch history and handling real-time webhooks. It focuses on how media identity is resolved, how hybrid libraries are handled, and how the system ensures data consistency with other providers (TMDB).

## Core Concepts

### 1. Identity Resolution
The system prioritizes **TMDB IDs** as the canonical source of truth. All Plex entries (history or webhook) must eventually resolve to a valid TMDB ID (`tmdb_id`) to be stored.

**Resolution Priority:**
1.  **Explicit TMDB ID**: Extracted directly from Plex GUIDs (e.g., `tmdb://123`).
2.  **IMDB/TVDB Lookup**: If only IMDB (`tt123`) or TVDB (`789`) IDs are present, the system queries the TMDB `find` API to resolve the corresponding `tmdb_id`.
3.  **Title Search Fallback**:
    -   If no external IDs are found (or they return 404s), the system performs a search against TMDB using the media title.
    -   **TV Shows**: Uses `grandparentTitle` (series title) or `title`. Match attempts to filter by year if available.
    -   **Movies**: Uses `title` and `year`.

**GUID extraction & conflicts:**
-   If any `tmdb://` GUID exists, we use that TMDB ID regardless of GUID order (this satisfies the "explicit TMDB ID" priority). Otherwise we prefer TVDB (if present) and then IMDB to resolve via TMDB `find`. Conflicting GUIDs of the same type are not reconciled. Very large numeric TMDB GUIDs can be treated as IMDB IDs when no IMDB is present.

**TV `tmdb_id` semantics:**
-   For TV history rows, `tmdb_id` represents the **show** TMDB ID, with season/episode numbers stored separately.
-   If Plex provides episode-level TMDB IDs or non-standard numbering (anime, specials, absolute order), resolution can fall back to title search during metadata lookup; unresolved episodes are skipped.

**Title search policy:**
-   Searches use `TMDB_LANG` (default `en`) with no region override and no original-title fallback.
-   When search results include dates and Plex provides a year (`originallyAvailableAt`/`year`), the first result whose `first_air_date`/`release_date` matches that year is used. Otherwise the top result is accepted as a best guess.
-   There is no strict exact-title or uniqueness requirement; if multiple plausible hits remain, the first result wins.

**TMDB 404 handling:**
-   TV metadata lookups that 404 will attempt a title-search remap (and use the remapped TMDB ID if found). Movie metadata 404s are treated as "not found" and the entry is skipped unless a title-search resolved the ID earlier.

**TMDB rate limits & caching:**
-   `find`, `search`, and movie/TV/season metadata are cached via Django cache (default 24h). The importer also keeps in-memory caches per run.
-   Requests go through a shared Redis-backed limiter (~5 req/s). HTTP 429 honors `Retry-After` plus a small buffer and retries; other HTTP errors surface as `ProviderAPIError` and are logged or bubbled up.

### 2. Data Contract & Invariants
Plex history import uses Plex's history endpoint as the canonical event stream. The importer treats TMDB-backed IDs as stable keys for dedupe and overwrite behavior.

**History source & pagination:**
-   Endpoint: `GET {server_uri}/status/sessions/history/all` (not "recently watched"), optionally filtered by `librarySectionID`.
-   Sorted newest-first (`sort=viewedAt:desc`) and paged with `X-Plex-Container-Start`/`X-Plex-Container-Size`. `PLEX_HISTORY_PAGE_SIZE` controls page size; `PLEX_HISTORY_MAX_ITEMS` (0 = no cap) limits how far back we fetch. There is no time windowing, so re-importing overlapping ranges is expected.

**Fields we rely on:**
-   IDs: `Guid`/`guid` entries with TMDB/IMDB/TVDB identifiers are required for deterministic resolution. Order is: resolve IDs from the history row (including title search when allowed); if missing, fetch `GET {server_uri}/library/metadata/{ratingKey}` to pull GUIDs (no title search in this step); if still missing, the movie/TV recorders may still fall back to title search when a title is available, otherwise the entry is skipped.
-   Titles: `title` or `grandparentTitle` is required for title-search fallback; Plex-only GUIDs without a title are skipped.
-   Timing: `viewedAt` or `lastViewedAt` (epoch seconds) is the authoritative `watched_at`. If missing, we fall back to import time; `viewCount`/`viewOffset` are ignored. Rows missing `viewedAt`/`lastViewedAt` are nondeterministic and can dedupe poorly across runs.
-   TV structure: `parentIndex` (season) and `index` (episode) must be numeric. Missing numbers can cause the entry to be skipped or treated as a movie in show libraries.
-   User mapping: `accountID` or username fields are used to filter per-user history when `plex_usernames` is set.

**Dedupe keys (`watched_at_minute`):**
-   Movies: `(tmdb_id, watched_at_minute)` where `watched_at_minute` is `watched_at` converted to local time and truncated to the minute (floor, not rounding).
-   Episodes: if `plex_rating_key` and `viewed_at_ts` exist, dedupe uses `("plex", plex_rating_key, viewed_at_ts)` (replacing the TMDB tuple); otherwise `("tmdb", tmdb_id, season, episode, watched_at_minute)`.
-   Because we truncate to minute, distinct plays within the same minute will dedupe for movies and for episodes when `plex_rating_key` + `viewed_at_ts` are missing. Dedupe uses local time; DST transitions can cause minute collisions (webhook dedupe uses a separate 5-second rule).

**Overwrite scope:**
-   TMDB-sourced means `Item.source == "tmdb"` (TMDB-backed metadata), regardless of which integration created the row.
-   Overwrite deletes TMDB-sourced items for the user whose `tmdb_id` appears in the current import run. There is no persisted library/server boundary, so the scope is "IDs seen in this import", not "this Plex section."
-   Filtering by `librarySectionID` only changes which IDs are seen in that run; it does not create a durable boundary for future overwrites.
-   Because overwrite is keyed by TMDB IDs, it can remove TMDB-backed items imported from other integrations (Trakt/Simkl/etc.) for the same user. Sources that do not use TMDB IDs are unaffected.

### 3. Import Semantics
The Plex History Importer (`src/integrations/imports/plex.py`) is designed to "replay" Plex history into the application's local database.

-   **Append-Only History**: History rows are treated as completed plays; partial progress is ignored. Re-importing the same window is expected and handled by dedupe (see data contract above).
-   **User Scoping**: Imports are scoped to `user.plex_usernames` (comma-separated). If the list is empty, the importer defaults to the connected account (the Plex account associated with the stored token). If multiple usernames are listed, their history can mix.
-   **Overwrite Mode**: Existing TMDB-backed entries in scope are removed before bulk creation (including TMDB-based entries from other integrations). Sources that do not use TMDB IDs are unaffected.

## Hybrid Library Handling (Movies in TV Libraries)
Plex allows "Anime" or "Documentary" libraries to contain both Movies and TV Shows, but often reports them with conflicting metadata (e.g., a Movie inside a "show" section).

**Logic Flow:**
1.  **Initial Detection**: Entries in a "show" section are initially assumed to be TV.
2.  **Episode Validation**: If the entry lacks `parentIndex` (Season) or `index` (Episode), it is flagged as a potential Movie.
3.  **Type Swap**: The system changes the context to `MediaTypes.MOVIE` and attempts resolution.
4.  **Fallback**:
    -   If the entry fails to resolve as a Movie (e.g., no TMDB match), it may fall back to `_record_episode_entry` if it had some TV-like characteristics.
    -   Conversely, if a standard Episode recording fails (e.g., missing episode number), the system attempts a "Last Ditch" `_record_movie_entry` to catch "Special" episodes that arguably map to Movies (common in Anime).

**Other Plex-specific shapes:**
-   Multi-episode files are not split; only the single `parentIndex`/`index` pair is used, so double-length specials may map to one episode or fall through to the movie fallback.
-   Extras/trailers/clips use unsupported Plex `type` values and are ignored.

## Webhook Processing
Real-time webhooks (`src/integrations/webhooks/plex.py`) share the same ID resolution logic as the importer.

-   **Scrobble vs. Play**: Only `media.scrobble` events (90% completion) count as "watched" history.
-   **User Mapping**: The webhook token selects the Yamtrack user, then the payload `Account.title` must match `user.plex_usernames`. If the username is missing or not allowed, the event is ignored.
-   **User Mapping Caveat**: Webhooks do not map Plex account IDs; if you allow multiple usernames, events can mix.
-   **Deduplication**: Episode scrobbles within 5 seconds of the last recorded play are treated as duplicates. Movies update an existing in-progress entry or create a new completed entry; there is no minute-based dedupe for webhooks.
-   **Dedupe Intent**: Webhook dedupe is tuned for bursty duplicate deliveries; import dedupe is tuned for replaying long history pages, so the rules intentionally differ.
-   **Reliability**: Webhooks are best-effort with no ordering guarantees. If a scrobble is missed, rerun the Plex history import (manual or scheduled) to reconcile gaps.
-   **Metadata**: Webhooks fetch TMDB metadata inline (cached); there is no separate refresh queue.

## Troubleshooting & logging

### Common Log Messages

-   **`Skipping Plex entry without external IDs`**: The entry had no GUIDs (plex:// only) and failed title search fallback. Use "Fix Match" in Plex to assign a valid agent match.
-   **`Plex TMDB ID ... not found; trying title search`**: The ID provided by Plex (likely from an old cache) 404'd on TMDB. The system automatically attempted a title search to fix it.
-   **`Resolved ... via title search`**: Success message indicating the fallback mechanism worked.

### Debugging
If imports are skipping unexpectedly:
1.  Check `summary_counts` in the import result.
2.  Enable `DEBUG` logging for `integrations.imports.plex` and `app.providers.services`.
3.  Verify if the item on Plex has a "Match" (Agent) assigned. Unmatched items often lack the metadata needed for resolution.
