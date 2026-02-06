# Dev -> Release Diff Report (TEMP)

Generated: January 30, 2026  
Scope: `git diff dev..release` (local `dev` vs `release` branch)  
Purpose: Human-readable staging doc for wiki updates (no code dumps).

---

## Executive Summary
- Release adds **new media types (Music, Podcast, Board Game)** plus a **Collection** system with rich metadata.
- The **Statistics** and **History** experiences are heavily rebuilt: cached, filterable, and chart-rich.
- **Integrations expand** to Plex/Pocket Casts/Last.fm/Jellyseerr/Jellyfin/Emby with new import flows and webhooks.
- **Preferences and UI** get major upgrades: new Settings navigation, date/time formats, mobile layout options, sort direction toggles, and more.
- Infrastructure gains **runtime population tasks**, SQLite resiliency, cache status APIs, and improved Docker metadata.

---

## 1) Navigation, Layout & Global UI
- **New Settings navigation** (sidebar in Settings pages) and a **dedicated Preferences page** alongside Account/Notifications/Integrations/Import/Export/Advanced/About.
- **Base layout refinements**: updated padding/margins, higher z-index header, improved mobile sidebar behavior, and per-user mobile layout data attributes.
- **Public view support**: new `base_public.html` with simplified header/footer and no sidebar/search (used for public pages).
- **Search bar enhancements**:
  - Dynamic media-type dropdown now respects enabled media types.
  - **Book barcode scanning** via ZXing and image upload; inline error feedback.
- **Media card hover behavior** improved with dynamic title expansion (JS calculates extra height) and optional subtitle display setting.
- **New icons** added (camera, microphone, speaker, signal, shield-alert, globe), plus updated icon usage in navigation and templates.

---

## 2) Media Types & Data Model Expansion
New/extended models compared to `dev`:
- **BoardGame**, **Music**, **Podcast** media types.
- Music hierarchy: **Artist**, **Album**, **Track**, plus **ArtistTracker** and **AlbumTracker**.
- Podcast hierarchy: **PodcastShow**, **PodcastEpisode**, **PodcastShowTracker**.
- **CollectionEntry** with physical/format metadata (resolution, HDR, audio codec/channels, bitrate, 3D, etc.).
- **MetadataBackfillState** for tracking metadata refresh.

New sources/providers:
- **BoardGameGeek** (`bgg`), **MusicBrainz** (music), **Pocket Casts** (podcasts).
- Existing providers updated (TMDB/MAL/IGDB/OpenLibrary/Hardcover/ComicVine/MangaUpdates) to support new fields and edge cases.

---

## 3) Music Features (New)
User-facing:
- **Music search** results split into artists + releases (`search_music.html`).
- **Artist detail** and **Album detail** pages with discography grids, track modals, and cover art support.
- **Track/Album/Artist tracking modals**, scoring updates, and delete-all-plays actions.
- **Music stats** (top artists/albums/tracks, genres/decades/countries, play charts).

Backend:
- MusicBrainz provider, discography sync services, cover prefetching, scrobble services, and validation tooling.
- New management command: `validate_music_library`.

---

## 4) Podcast Features (New)
User-facing:
- **Podcast show detail** page, episode list, and track modal flow (mirrors music UX).
- **Mark all played** action for a show.
- **Podcast stats** (plays by time period, top shows/episodes, etc.).

Backend:
- Pocket Casts provider + import pipeline.
- **RSS fallback** (iTunes ID -> RSS) for podcast metadata and episode sync.
- Podcast models integrate with history + runtime calculations.

---

## 5) Collection System (New)
User-facing:
- **Collection add/edit/remove** flow + new collection modal.
- **Collection list page** and **collection filter** in media list views (all/collected/not collected).
- **Media details show collection metadata** (resolution, HDR, audio, bitrate, collected date).
- **TV/Season/Episode** pages show collection-based episode/season stats and metadata.
- **Music albums** show aggregated collection metadata from tracked tracks.

Backend:
- Collection metadata extraction from **Plex/Jellyfin**.
- Cached **Plex rating key** storage for faster matching.
- Background metadata fetch (triggered when collection data is missing).
- Collection API endpoint for live "collection ready" status.

---

## 6) History Page Overhaul
User-facing:
- New **History page** with caching, pagination, and filter handling (media type, genre, logging style).
- New **history card component** and improved timeline formatting.
- "Refreshing in background" UX with automatic reload after cache rebuild.

Backend:
- **history_cache.py**: day-bucket caching, stale detection, warm-cache settings.
- **cache-status API** endpoints for history.
- Preference-aware date formatting and per-user logging style (e.g., game sessions vs repeats).

---

## 7) Statistics Overhaul
User-facing:
- **Range picker** with predefined ranges + custom range (including "All Time").
- **Refresh button** and cache status banners for background recomputation.
- Rich **chart suite**: distribution charts, stacked scores/status, and per-media activity charts.
- Expanded **media-specific sections** (TV, Movies, Games, Music, Podcasts, Anime, Board Games).
- More contextual "Top" cards (top genres, top rated, top played, daily averages, etc).

Backend:
- **statistics_cache.py**: precomputed ranges with stale detection and refresh scheduling.
- Per-day caches for custom ranges to avoid full scans.
- Runtime-based calculations now spread across all media types (including music/podcast).

---

## 8) Home & Media Lists
User-facing:
- **Sort direction toggles** (asc/desc), per-type sort preferences stored per user.
- **Time-left sort** for TV with better grouping and display.
- New **collection filter** and rating filter in list views.
- **Music/podcast list views** show tracked artists/shows instead of individual tracks/episodes.
- Responsive grid/table layout controls; mobile layout preferences.

Backend:
- **time_left caching** to speed expensive sorts.
- Aggregated/deduped logic to avoid duplicates when rendering lists.

---

## 9) Media Details & Tracking
User-facing:
- Detail pages now surface **collection stats/metadata** and show "fetching" state when data loads in background.
- **Podcast show detail** includes episode tracking + history aggregate.
- **Music details** include artist/album linking + track history.

Backend:
- Track/save flows expanded for music and podcast.
- History aggregation for podcast and music to avoid duplicate counts.

---

## 10) Lists & Recommendations
User-facing:
- **List recommendations** flow (submit/approve/deny) with UI components and activity logging.
- **Public list outputs**: RSS feed + JSON export (Radarr/Sonarr compatible).
- List pages redesigned with new grid UI, summary panels, and activity views.

Backend:
- New list visibility options, list activity model, and recommendation model.
- `diagnose_lists` management command + list tasks and feeds.

---

## 11) Integrations & Imports
New/expanded integrations:
- **Plex** (connect/import) + collection metadata extraction.
- **Pocket Casts** (connect/import).
- **Last.fm** (connect + manual poll + scrobble services).
- **Jellyfin / Emby / Jellyseerr** webhooks.
- Expanded Trakt + Simkl flows.

Resilience & edge-case handling:
- Plex GUID parsing + rating key caching.
- TMDB episode edge cases handled in import logic.
- **SQLite lock / I/O retry** helpers used in import workflows.

---

## 12) Preferences & Settings
New/expanded user preferences:
- **Date format** and **time format** options.
- **Activity History view** preference (statistics page visualization).
- **Mobile grid layout** options + "quick season update" for mobile.
- **Media card subtitle display** behavior.
- **Auto-pause** rules for stale in-progress items.
- **Game logging style** (sessions vs repeats).
- **Statistics default range** selection.
- **Media card subtitle display**, **list detail defaults**, **show planned on home**, **progress % toggles**, etc.

Sidebar settings now include:
- Media type visibility toggles.
- Touch-device hover overlay preference.

Integration preferences:
- **Jellyseerr** allowed usernames, trigger statuses, default status on add.

---

## 13) Background Tasks, Caching & Resilience
- New Celery tasks for **runtime population**, **statistics caching**, **music validation/scrobbling**, and integration workflows.
- **Runtime population** can be scheduled on startup (guarded by cache availability + non-Celery process).
- **Database retry middleware**: automatic retry on SQLite lock/disk I/O errors.
- **SQLite PRAGMA tuning** (WAL + busy timeout + synchronous setting).
- **Sessions moved to Redis cache** to avoid DB contention.
- **cache-status API** for both history and statistics, with client-side polling.
- **Service worker** added for static assets caching.

---

## 14) Deployment / Infra / Config Changes
- Dockerfile adds repo metadata stage (fork owner detection), `COMMIT_SHA` support, and gunicorn config file.
- Docker Compose now points to fork images, adds network wiring, and `ADMIN_ENABLED` env variable.
- PostgreSQL compose now uses named volume instead of `./db`.
- Settings changes include:
  - `DEBUG` default true (local dev oriented).
  - Cookie path fixes for `BASE_URL`.
  - Plex + Last.fm + BGG env vars.
  - Commit hash + fork owner detection for UI/footer.
  - Expanded Celery accept content list.

---

## 15) Docs, Tests & Tooling
- New agent docs under `docs/agents/` (caching, music, collection, integrations, etc.).
- Test suite restructured by domain (models/views/providers/integrations).
- Added `stylelintrc` and Djlint indentation config.
- New dependency: `defusedxml`.

---

## 16) Static Assets & Styling
- **Tailwind input & compiled CSS** updated; `tailwind.css` added (legacy output).
- New JS modules: barcode scanner, cache updater, statistics charts, date-range picker.
- New assets/logos: Last.fm, Plex, Pocket Casts.

---

## 17) Potential Cleanup / Noise
- `.DS_Store` files appear in `src/`, `src/app/`, `src/static/`, `src/templates/`, `src/users/` - likely accidental.

---

# Wiki Update Staging Checklist

## Core Features
- Add/refresh **Media Types** page: Board Games, Music, Podcasts, Collection.
- Add **Music** guide (search -> track -> artist/album detail -> scrobble).
- Add **Podcast** guide (show detail, RSS fallback, episode tracking).
- Add **Collection** guide (Plex/Jellyfin metadata, collection list, filter).

## UI & UX
- Update **Navigation/Settings** docs: new Preferences/Sidebar sections.
- Document **Search barcode scanning** for books.
- Document **History page** filters and cache refresh behavior.
- Document **Statistics page** range picker, refresh, and chart sections.

## Integrations
- Update **Plex** docs (collection metadata, GUID handling, rating key cache).
- Add **Pocket Casts** integration instructions + import.
- Add **Last.fm** connection/polling notes.
- Add **Jellyseerr/Jellyfin/Emby** webhook configuration.

## Preferences & Personalization
- Document **date/time format** settings.
- Document **auto-pause** rules (stale in-progress).
- Document **mobile layout** + quick-season-update toggle.
- Document **media card subtitle** display options.
- Document **statistics default range** preference.

## Deployment / Ops
- Update **Docker & env vars** docs: `COMMIT_SHA`, `PLEX_*`, `LASTFM_*`, `BGG_API_TOKEN`, runtime population flags.
- Mention **SQLite tuning** defaults and Redis-backed sessions.

## Lists & Sharing
- Update **Lists** docs for recommendations, list activity, visibility, RSS/JSON exports.

---

## Open Questions / Follow-ups for Wiki
- Confirm which pages should use `base_public.html` and public view behavior.
- Decide if `tailwind.css` is still legacy or should be documented.
- Clarify the intended user-facing flow for Collection (simple list vs richer UI).

---