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

# Wiki Reorg Plan (Outline + Prompts)

This is a comprehensive plan for the wiki, based on the diff report and current codebase. Each page proposal includes a research prompt and a drafting prompt to guide the next step.

## Big Picture Reorg
Target flow: Start here -> Use it -> Connect stuff -> Run it -> Fix it -> Deep reference.

## 0) Home (NEW)
- Home (DONE - drafted in `wiki/Home.md` on 2026-01-30): Research prompts: Summarize what Yamtrack is in this fork (media types, collection, cache-heavy stats/history, integrations) and identify quick-start links that match current flows. Drafting prompts: Write a front-door page with a short "What's new in Release" callout, top 4-6 links, and a one-paragraph product description.
- Sidebar: Research prompts: List the final page tree and preferred ordering from this outline; verify any existing sidebar constraints in the wiki platform. Drafting prompts: Create a sidebar that mirrors this structure in the same order.
- Footer update: Research prompts: Confirm whether to show fork owner name and commit SHA and where that information appears in-app. Drafting prompts: Add a short footer note about the fork and where to see version info.

## 1) Getting Started
- Getting Started (DONE - drafted in `wiki/Getting-Started.md` on 2026-01-30): Research prompts: Validate primary install path (Docker Compose), first-run steps, and required env vars in README + docker-compose. Drafting prompts: Provide a minimal step-by-step from clone to login, with "next steps" links.
- Upgrade Guide: dev -> release (DONE - drafted in `wiki/Upgrade-Guide-dev-release.md` on 2026-01-30): Research prompts: Identify migrations, new models, cache behavior changes, and integration additions from migrations and settings. Drafting prompts: Write an upgrade checklist and "things that might surprise you" section.

## 2) User Guide
- Using Yamtrack (DONE - drafted in `wiki/Using-Yamtrack.md` on 2026-01-30): Research prompts: Define the core mental model (track items, imports feed history, stats cached, collection is separate layer). Drafting prompts: Write a short hub page with links to core feature pages.
- History (DONE - drafted in `wiki/History.md` on 2026-01-30): Research prompts: Confirm filters, logging style, cache refresh UX, and how entries are generated. Drafting prompts: Explain filters, cache refresh messaging, and how to interpret timeline entries.
- Statistics (DONE - drafted in `wiki/Statistics.md` on 2026-01-30): Research prompts: Confirm range picker behavior, refresh workflow, caching, and media-specific sections in the template and JS. Drafting prompts: Document ranges, refresh button, and major chart groupings.
- Search and Add (DONE - drafted in `wiki/Search-and-Add.md` on 2026-01-30): Research prompts: Verify search dropdown behavior, enabled media types, and barcode scanning for books. Drafting prompts: Explain search modes and the barcode flow with error handling.
- Preferences (DONE - drafted in `wiki/Preferences.md` on 2026-01-30): Research prompts: Enumerate all preference fields added in users/models.py and users/preferences.html. Drafting prompts: Group settings by layout/date/time/history/auto-pause and explain defaults.
- Home and Media Lists (DONE - drafted in `wiki/Home-and-Media-Lists.md` on 2026-01-30): Research prompts: Explain list sorting/direction toggles, time-left sorting, filters, and music/podcast list behavior. Drafting prompts: Document list controls, saved preferences, and special-case list displays.
- Media Details and Tracking (DONE - drafted in `wiki/Media-Details-and-Tracking.md` on 2026-01-30): Research prompts: Confirm detail page actions, collection metadata panels, and music/podcast history links. Drafting prompts: Describe tracking actions, collection fetch banners, and music/podcast linking/aggregation.

## 3) Media Types
- Media Types Overview (NEW): Research prompts: List supported media types and their providers from app/config.py. Drafting prompts: Explain differences by media type at a high level.
- Music (NEW): Research prompts: Confirm hierarchy (Artist -> Album -> Track) and core UI flows (search, detail pages, tracking modals). Drafting prompts: Provide a usage guide and link to Last.fm integration.
- Podcasts (NEW): Research prompts: Confirm show/episode trackers, RSS fallback behavior, and Pocket Casts integration. Drafting prompts: Provide a usage guide and common troubleshooting notes.
- Board Games (NEW): Research prompts: Confirm provider details (BGG), data fields, and any import workflow. Drafting prompts: Provide a concise usage guide.

## 4) Collection System
- Collection (NEW hub): Research prompts: Confirm collection list page, filters, and how collection metadata is surfaced in media details. Drafting prompts: Explain the purpose of collection metadata and the user flows (add/edit/remove, filter).
- Collection Metadata Reference (NEW): Research prompts: Enumerate all collection fields and which integrations populate them (Plex/Jellyfin). Drafting prompts: Create a field reference table with examples.

## 5) Integrations and Imports
- Integrations Overview (NEW hub): Research prompts: Catalog integrations by category (account connections vs webhooks) and what each enables. Drafting prompts: Provide a one-page summary with links to each integration.
- Plex (NEW or upgrade existing): Research prompts: Confirm connect/import flow, collection metadata extraction, GUID parsing, rating key caching, and common issues. Drafting prompts: Create a practical guide and troubleshooting section.
- Jellyfin (NEW): Research prompts: Confirm webhook endpoints and collection metadata extraction. Drafting prompts: Document setup and expected payload behavior.
- Emby (NEW): Research prompts: Confirm webhook endpoints and any special handling. Drafting prompts: Document setup and expected behavior.
- Jellyseerr (NEW): Research prompts: Confirm webhook triggers and user settings (allowed usernames, default status). Drafting prompts: Document setup and how it affects list/status updates.
- Pocket Casts (NEW): Research prompts: Confirm connect/import flow and RSS fallback behavior. Drafting prompts: Document steps and common failure modes.
- Last.fm (NEW): Research prompts: Confirm connect, manual poll, and scrobble services. Drafting prompts: Explain how plays become history and stats.
- Trakt / Simkl / AniList / Steam (reorg): Research prompts: Confirm current import flows and edge-case handling. Drafting prompts: Standardize each page to the same format (connect -> import -> troubleshooting).
- Media Import Overview (rewrite): Research prompts: Map the shared import patterns (matching, retries, background tasks, lock mitigation). Drafting prompts: Provide a single overview and link out to provider pages.
- Yamtrack CSV Format (keep, move): Research prompts: Confirm supported fields and limitations. Drafting prompts: Include migration guidance and caveats.

## 6) Lists and Sharing
- Lists Overview (DONE - drafted in `wiki/5.-Lists.md` on 2026-01-30): Research prompts: Confirm list visibility modes, activity, and recommendation workflow. Drafting prompts: Document creating lists, managing visibility, and recommendations.
- Public Lists: RSS and JSON Exports (DONE - drafted in `wiki/5.-Lists.md` on 2026-01-30): Research prompts: Confirm RSS endpoint and JSON export formats (Radarr/Sonarr). Drafting prompts: Provide endpoint URLs, parameters, and example payloads.

## 7) Admin and Operations
- Admin Guide (DONE - merged into `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Collect all admin-related instructions from existing wiki drafts. Drafting prompts: Create one consolidated page with enable/login/manage steps.
- Configuration Overview (DONE - merged into `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Identify all config sources (env vars, docker, secrets). Drafting prompts: Explain minimal required vs optional settings and link to env var reference.
- Environment Variables (DONE - merged into `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Catalog env vars by domain (core, db, redis, celery, integrations). Drafting prompts: Build a grouped reference with examples.
- Docker Deployment (DONE - merged into `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Confirm compose defaults, image tags, COMMIT_SHA usage, ADMIN_ENABLED. Drafting prompts: Provide a primary Docker path with advanced notes.
- Database: SQLite vs Postgres (DONE - merged into `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Confirm SQLite tuning, retry middleware, and Postgres compose config. Drafting prompts: Offer guidance on when to use each and how to migrate.
- Redis and Sessions (DONE - merged into `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Confirm session backend settings and required Redis config. Drafting prompts: Explain why sessions moved to Redis and how to configure.
- Celery and Background Tasks (DONE - merged into `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Confirm runtime population, cache refresh tasks, and health checks. Drafting prompts: Document common worker setup and status checks.
- Host Under Subpath / Self-signed Certificates / Docker Secrets (DONE - merged into `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Validate existing docs still apply and any new settings. Drafting prompts: Update with current settings names and examples.
- Social Authentication (DONE - merged into `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Confirm providers and configuration env vars. Drafting prompts: Keep concise and link to upstream provider docs.

## 8) Troubleshooting (NEW)
- Troubleshooting Hub: Research prompts: Identify top failure modes from code and logs (imports stuck, cache refreshing, Plex mismatch, public pages). Drafting prompts: Provide symptom -> cause -> fix structure.
- Known Issues and Gotchas: Research prompts: Confirm .DS_Store cleanup guidance and expected first-run slowness. Drafting prompts: List known issues with mitigation steps.

## 9) Developer Notes (Optional)
- Architecture and Caching Notes (NEW): Research prompts: Map history_cache and statistics_cache lifecycle, cache-status API, and refresh scheduling. Drafting prompts: Explain architecture at a conceptual level.
- Providers and Data Sources (NEW): Research prompts: Map providers to media types and ID systems. Drafting prompts: Provide a quick reference for maintainers.
- Agent Docs Index (NEW): Research prompts: Inventory docs/agents files and summarize purpose. Drafting prompts: Provide a link list with 1-line summaries to avoid duplication.

## Existing Pages: Merge / Move / Keep
- Merge into Admin Guide (DONE - moved to `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Identify overlapping pages (Admin Page, Enabling Admin Interface, Logging In, Changing User to Admin, Creating Admin User, Admin Interface Overview). Drafting prompts: Merge into a single page with clear headings.
- Merge into Environment Variables (DONE - moved to `wiki/6.-Admin-and-Operations.md` on 2026-01-30): Research prompts: Identify Postgres-specific env var pages and SSL notes. Drafting prompts: Fold under a "Postgres" subheading within env vars.
- Merge into Media Import Overview: Research prompts: Identify current Media Sources / Media Import / Media Import Configuration pages. Drafting prompts: Create one overview and push details into per-integration pages.
- Keep standalone leaf pages: Research prompts: Confirm these pages still apply (Host under subpath, Self-signed certificates, Docker secrets). Drafting prompts: Update for current settings names and examples.

## Minimum Missing Essentials (First Draft Targets)
- Essentials list: Research prompts: Confirm these pages cover the highest-impact feature changes in release. Drafting prompts: Start with Home, Upgrade Guide, Preferences, History, Statistics, Collection, Music, Podcasts, Integrations Overview (with Plex, Pocket Casts, Last.fm, Jellyseerr/Jellyfin/Emby).
