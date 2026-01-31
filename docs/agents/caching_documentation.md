# Cache Refresh Analysis (Single Source of Truth)

## Scope and intent
- This document enumerates every cache and cache-busting path in the codebase, including Redis caches, DB TTL guards, UI refresh logic, static asset cache busting, and service worker behavior.
- Goal: keep Yamtrack workable in a 768 MB environment by avoiding large single-pass rebuilds and favoring incremental, per-day caches. This is still a work in progress, not a solved problem.
- This document replaces issue-22 notes as the canonical caching reference.
- Boundaries: this does not cover browser caching outside our headers, any CDN/proxy caching if added later, or third-party provider rate limits (which can look like cache issues).

## Invariants (must always hold)
- Index must be stable; page slicing must not depend on cache hits.
- Day payload keys must be a pure function of `(user_id, logging_style, date, schema version)`.
- A refresh task must warm the days requested when `day_keys` are provided.

## Cache backend and defaults
- `src/config/settings.py`:
  - Redis cache via `django_redis` (`CACHES["default"]`), `CACHE_TIMEOUT=86400` (24h), `VERSION=11`, `KEY_PREFIX` from `REDIS_PREFIX`.
  - Session storage uses cache backend (`SESSION_ENGINE = django.contrib.sessions.backends.cache`).
  - `django-select2` uses Redis with `SELECT2_CACHE_PREFIX`.
- `src/config/test_settings.py`:
  - Cache timeout 5h with FakeRedis connection pool; Celery eager.

## Cache key versions (manual busts)
- History: `history_index_v14`, `history_day_v14`.
- Statistics: `statistics_page_v2`, plus per-day `stats:day`.
- Time-left: `time_left_sorted_v12`.
- Bump these prefixes to invalidate old keys without delete_pattern.

## History cache (per-day)
- Files: `src/app/history_cache.py`, `src/app/views.py`, `src/templates/app/history.html`, `src/static/js/cache-updater.js`.
- Keys:
  - Index: `history_index_v14_{user_id}_{logging_style}` (stores `day_keys` + `built_at`).
  - Day payloads: `history_day_v14_{user_id}_{logging_style}_{YYYYMMDD}`.
  - Lock: `history_refresh_lock_v14_{user_id}_{logging_style}` with `started_at` and optional `day_keys`.
- TTL/stale:
  - Day + index TTL: 6h (`HISTORY_CACHE_TIMEOUT`).
  - Stale after: 1h by default (`HISTORY_STALE_AFTER`, configurable).
  - Lock TTL: 120s (matches frontend polling); stale lock max age 5m.
  - `HISTORY_DAYS_PER_PAGE=30`, `HISTORY_WARM_DAYS` default 0.
  - `HISTORY_COLD_MISS_WARM_DAYS` default 30 (used when index is missing).
- Behavior (page-centric):
  - `get_cached_history_page()` fetches index, then day payloads for the requested page.
  - Index miss: schedule refresh (warm_days) and return empty to avoid inline rebuild.
  - Page-day miss: schedule refresh for those specific day keys and return empty.
  - Partial misses: inline build missing day payloads and cache them.
  - Stale index: serve cached data and schedule refresh.
  - Filters/date_filters/logging_style overrides bypass the per-day cache and build inline to avoid key explosion; filtered views are intentionally not cached.
- Refresh orchestration:
  - `schedule_history_refresh()` debounces via `cache.add`, writes lock payload, and queues Celery task. Falls back to inline refresh if Celery is unavailable.
  - `refresh_history_cache()` rebuilds index and optionally warms day payloads:
    - If `day_keys` provided, warms only those days ("page_days" mode).
    - Else warms `HISTORY_WARM_DAYS` from the newest end of the index.
- Invalidation triggers:
  - `invalidate_history_days()` deletes specific day payloads and schedules index rebuild.
  - `invalidate_history_cache()` clears only when no lock, unless `force=True`.
  - Signals (`src/app/signals.py`) invalidate days on Episode/Movie/Music/Podcast/Game/BoardGame changes.
  - `delete_history_record` forces invalidation because historical deletes do not fire model signals.
  - Changing `game_logging_style` invalidates and schedules refresh (`src/users/views.py`).

## Statistics cache (per-day + range)
- Files: `src/app/statistics_cache.py`, `src/app/views.py`, `src/templates/app/statistics.html`, `src/static/js/cache-updater.js`.
- Keys:
  - Range cache: `statistics_page_v2_{user_id}_{normalized_range}`.
  - Range lock: `statistics_page_v2_refresh_lock_{user_id}_{normalized_range}`.
  - Day cache: `stats:day:{user_id}:{YYYY-MM-DD}`.
  - Dirty set: `stats:dirty:{user_id}` (list of ISO dates).
  - History version: `stats:history_version:{user_id}` (bumped on invalidation).
  - Schedule dedupe: `stats:refresh:scheduled:{user_id}:{history_version}:{range}` (TTL 10m).
- TTL/stale:
  - Range TTL 6h; stale after 15m if no history_version.
  - Day TTL 30d (`STATISTICS_DAY_CACHE_TIMEOUT`).
  - Range lock TTL 5m; stale lock max age 5m.
  - Warm window: `STATISTICS_WARM_DAYS` default 2 (today + yesterday if in range).
- Behavior (incremental):
  - `refresh_statistics_cache()` builds only:
    - dirty days,
    - warm days,
    - missing day caches in the target range.
  - Range aggregates are built by summing cached day payloads.
  - If a day payload is missing during aggregation, it is built for that day only.
  - "All Time" uses sparse day list (`_get_sparse_activity_days`) instead of a contiguous range.
  - Non-predefined ranges are computed inline and not cached.
- Logging:
  - `stats_day_summary` includes `plays`, `missing_runtime`, `missing_genres`.
  - `stats_range_summary` logs days, refreshed, nonempty, elapsed, totals.
- Invalidation triggers:
  - `invalidate_statistics_days()` deletes day caches, updates dirty set, and bumps history_version.
  - Signals (`src/app/signals.py`) invalidate day caches on media changes and schedule all ranges.
  - `delete_history_record` invalidates stats days and schedules all ranges.

## Metadata refresh overlay (runtime + genre backfill)
- Statistics day builds detect missing runtime/genres and enqueue backfills:
  - Runtime backfill queue: `runtime_backfill_items_queue` + `runtime_backfill_items_scheduled`.
  - Episode runtime queue: `runtime_backfill_episode_queue` + `runtime_backfill_episode_scheduled`.
  - Genre backfill queue: `genre_backfill_items_queue` + `genre_backfill_items_scheduled`.
  - Queue TTL: 1h; scheduled keys short TTL (debounce).
- Backfill throttling is stored in DB (`MetadataBackfillState`):
  - Exponential backoff (`next_retry_at`) and `give_up` after 6 failures.
  - Logged as `metadata_backfill_retry_later` and `metadata_backfill_give_up`.
  - Runtime failures use `runtime_minutes=999999` sentinel to avoid repeat work.
- Metadata refresh banner:
  - `stats:metadata_refresh:{user_id}` lock (TTL 10m).
  - `stats:metadata_refresh_built:{user_id}` timestamp (TTL 30d).
  - `cache_status` exposes `metadata_refreshing`, `metadata_built_at`, `metadata_recently_built`.
  - `_schedule_metadata_statistics_refresh()` marks metadata refresh, invalidates stats days for affected items, and schedules all ranges.

## Cache status endpoint + UI refresh loop
- `src/app/views.py` `cache_status()`:
  - History: returns `exists`, `built_at`, `is_stale`, `is_refreshing`, `recently_built`.
  - Statistics: adds `any_range_refreshing`, `refresh_scheduled`, and metadata refresh fields.
  - Clears stale locks to avoid "refreshing" stuck states.
  - If stats cache is stale and no lock exists, it schedules a refresh during polling.
- `src/static/js/cache-updater.js`:
  - Poll interval 2.5s, timeout 120s.
  - Uses `built_at` + `wasRefreshing` to avoid reload loops.
  - For statistics, waits until the active range is done and no other ranges are refreshing.
- Templates:
  - `src/templates/app/history.html`: polling is skipped for filtered views (bypass cache).
  - `src/templates/app/statistics.html`: polling only for predefined ranges.

## Known failure modes (symptoms -> suspects)
- Page 1 works, pages 2+ blank: page miss schedules refresh, but task warms newest days instead of requested `day_keys`.
- Repeated `warmed=30` with continued page misses: day key mismatch or refresh task not honoring `day_keys`.
- UI stuck on "refreshing": stale lock not cleared (clock skew or lock payload missing `started_at`).
- "Warmed" logs but view still misses: key mismatch (logging_style default vs explicit, YYYYMMDD vs YYYY-MM-DD, or version mismatch).
- Inline rebuild spikes on filtered views: expected; filtered paths bypass cache and build inline.

## Ops runbook (verify + actions)
- Cold miss vs broken:
  - Expected: first request logs miss + scheduled refresh, second request after task logs hit>0 and renders entries.
  - Broken: refresh completes but page still shows hit=0/miss=30 for the same days.
- Log checks for history:
  - Good: `history_day_cache_cold_miss ... scheduled=True`, then `history_cache_refresh_done ... warmed=30`, then `history_day_cache_get_many ... hit>0`.
  - Bad: `history_cache_refresh_done ... warmed=30`, but subsequent `history_day_cache_get_many ... hit=0 miss=30`.
- Log checks for statistics:
  - Good: `stats_day_summary` for dirty day(s), `stats_range_summary` with refreshed>0.
  - Bad: `stats_range_summary` shows refreshed=0 while dirty set exists.
- Safe cache busting:
  - Bump versioned prefixes (`history_index_v14`, `history_day_v14`, `statistics_page_v2`, `time_left_sorted_v12`) to invalidate.
  - `clear_search_cache()` only clears `search_*` keys; it does not clear `bgg_search_*` or `musicbrainz_*`.

## Other Redis caches (application)
- Time-left sorted TV lists:
  - Key: `time_left_sorted_v12_{user_id}_{media_type}_{status}_{search}_{direction}_{rating}`.
  - TTL: 300s for list entries; registry TTL uses `CACHE_TIMEOUT`.
  - Registry key: `time_left_sorted_v12_registry_{user_id}`.
  - Invalidation: `cache_utils.clear_time_left_cache_for_user` on TV saves and explicit Season saves (Season overrides `save()` and now clears this cache).
  - Release sync throttle: `timeleft:release-sync:{source}:{media_id}` (TTL 1h).
- TMDB season cache (used by time-left + runtime fallback):
  - Key: `tmdb_season_{media_id}_{season_number}`.
  - Stored by TMDB provider; default TTL.
- Calendar:
  - `tvmaze_map_{tvdb_id}` cached with default TTL.
- Webhooks:
  - `anime_mapping_data` cached with default TTL.
- Runtime population guards:
  - `runtime_population_startup_scheduled` (24h, app startup).
  - `runtime_population_completed` (1h, tasks).
- Search cache clearing:
  - `clear_search_cache()` deletes `search_*` only (TMDB/MAL/IGDB/Hardcover/Pocket Casts).
  - `bgg_search_*`, `musicbrainz_*`, and other prefixes are not cleared by this endpoint.

## HTTP response cache control
- `@never_cache` on `track_modal` to prevent browser caching. The view also explicitly sets `Cache-Control: no-cache, no-store, must-revalidate`, `Pragma: no-cache`, and `Expires: 0` headers for Safari compatibility.
- `@never_cache` on `collection_modal` to prevent browser caching. The view also explicitly sets `Cache-Control: no-cache, no-store, must-revalidate, max-age=0`, `Pragma: no-cache`, `Expires: 0`, and `Vary: Cookie, HX-Request` to avoid Safari reusing stale modal HTML.
- `@never_cache` on `lists.views.lists` to avoid stale list data after items are added/removed. The view also explicitly sets `Cache-Control: no-cache, no-store, must-revalidate`, `Pragma: no-cache`, `Expires: 0`, and `Vary: Cookie` headers for Safari compatibility and to ensure user-specific responses aren't cached.
- `@never_cache` on `lists.views.list_detail` to avoid stale list grids/context rows after HTMX sorts and back/forward navigation.
- `@never_cache` on `app.views.media_list` to avoid stale media grids/context rows after HTMX sorts and back/forward navigation.
- `custom_lists.html` reloads on `pageshow` when `event.persisted` is true to bust Safari's back/forward cache restoring older list HTML.
- `list_detail.html` reloads on `pageshow` when `event.persisted` is true to bust Safari's back/forward cache restoring older list HTML.
- `media_list.html` reloads on `pageshow` when `event.persisted` is true for the same Safari back/forward cache issue.
- `media_details.html` reloads on `pageshow` when `event.persisted` is true to bust Safari's back/forward cache restoring the page.
- `custom_lists.html` disables HTMX history caching on `#lists-grid` (`hx-history="false"`) to prevent stale list grids from being restored.
- `list_detail.html` disables HTMX history caching on `#items-grid` (`hx-history="false"`) to prevent stale list grids from being restored.
- `custom_lists.html` sets `htmx.config.getCacheBusterParam = true` so HTMX GETs append `org.htmx.cache-buster`, which avoids Safari reusing cached list HTML.
- `list_detail.html` sets `htmx.config.getCacheBusterParam = true` so HTMX GETs append `org.htmx.cache-buster`, which avoids Safari reusing cached list HTML.
- `media_details.html` sets `htmx.config.getCacheBusterParam = true` for HTMX GET requests (including track modal requests). This prevents stale track modal HTML on previously visited pages. The configuration is set immediately and also on DOMContentLoaded as a fallback to ensure it's applied before any HTMX requests.
- `custom_lists.html` includes meta tags (`cache-control`, `pragma`, `expires`) in the `<head>` to provide additional cache-busting hints to browsers.
- Podcast episode list fragment sets `Cache-Control: no-cache, no-store, must-revalidate`, plus `Pragma`/`Expires`.
- `sync_metadata()` uses `cache.ttl()` to prevent immediate re-sync, and deletes provider cache keys when allowed.
- Static asset busting: `get_static_file_mtime()` appends `?mtime` to static URLs.
  - Applied to `css/main.css`, `js/date-range.js`, `js/statistics-charts.js`, and `js/barcode-scanner.js` (added for cache invalidation during development/debugging).
- Lists view uses annotated `items_count` instead of `items.count()` to avoid stale prefetch cache. The count is always computed fresh from the database via `Count("items", distinct=True)` annotation.

## Service worker caching (frontend)
- `src/static/js/serviceworker.js` caches a small list of static assets and additional static GETs.
- Does not cache HTML navigation or `/api/*` requests (keeps cache-status real-time).
- Versioned via `CACHE_NAME` and clears older caches on activate.

## Provider/API caches (Redis-backed)
Default TTL is `CACHE_TIMEOUT` unless specified.

- TMDB (`src/app/providers/tmdb.py`):
  - `search_TMDB_{media_type}_{query}_{page}`
  - `find_TMDB_{external_id}_{external_source}`
  - `TMDB_movie_{media_id}`, `TMDB_tv_{media_id}`
  - `tmdb_season_{media_id}_{season_number}`
- IGDB (`src/app/providers/igdb.py`):
  - Access token: `IGDB_access_token` (TTL `expires_in - 60`).
  - `external_game_IGDB_{source}_{external_id}`, `search_IGDB_game_{query}_{page}`, `IGDB_game_{media_id}`.
- MAL (`src/app/providers/mal.py`):
  - `search_MAL_{media_type}_{query}_{page}`, `MAL_anime_{media_id}`, `MAL_manga_{media_id}`.
- Hardcover (`src/app/providers/hardcover.py`):
  - `search_HARDCOVER_{media_type}_{query}_{page}`, `HARDCOVER_book_{media_id}`.
- ComicVine (`src/app/providers/comicvine.py`):
  - `search_COMICVINE_{media_type}_{query}_{page}`, `COMICVINE_comic_{media_id}`,
    `COMICVINE_issue_{media_id}`, `COMICVINE_similar_{publisher_id}_{current_id}`.
- OpenLibrary (`src/app/providers/openlibrary.py`):
  - `search_OPENLIBRARY_{media_type}_{query}_{page}`, `OPENLIBRARY_book_{media_id}`.
- MangaUpdates (`src/app/providers/mangaupdates.py`):
  - `search_MANGAUPDATES_{media_type}_{query}_{page}`, `MANGAUPDATES_manga_{media_id}`.
- BGG (`src/app/providers/bgg.py`):
  - `bgg_search_ids_{query}` (24h).
  - `bgg_search_page_{query}_p{page}` (24h).
  - `bgg_metadata_{media_id}` (7d).
- MusicBrainz (`src/app/providers/musicbrainz.py`):
  - `wikipedia_data_{title}` (7d, misses 1d).
  - `musicbrainz_cover_{release_id}_{release_group_id}` (7d).
  - `musicbrainz_search_{query}_p{page}` (24h, with `_no_art` variant).
  - `musicbrainz_recording_{media_id}` (7d).
  - `musicbrainz_artist_search_{query}_p{page}` (24h).
  - `musicbrainz_release_search_{query}_p{page}` (24h, with `_no_art` variant).
  - `musicbrainz_artist_{artist_id}` (7d).
  - `musicbrainz_artist_discography_v2_{artist_id}` (7d, with `_no_art` variant).
  - `musicbrainz_release_for_group_{release_group_id}` (7d).
  - `musicbrainz_release_{release_id}` (7d).
  - `musicbrainz_combined_search_{query}_p{page}` (24h).
- Pocket Casts (`src/app/providers/pocketcasts.py`):
  - `search_POCKETCASTS_podcast_{query}_{page}` (default TTL).
  - `itunes_lookup_{itunes_collection_id}` (7d).
- iTunes music artwork (`src/integrations/itunes_music_artwork.py`):
  - `itunes_album_artwork_{artist}_{album}` (7d, misses cached 7d).
  - `itunes_artist_artwork_{artist}` (7d, misses cached 7d).
- Pocket Casts artwork (`src/integrations/pocketcasts_artwork.py`):
  - `podcast_artwork_{podcast_uuid}` (7d, misses cached 1d).
- Other:
  - `tvmaze_map_{tvdb_id}` (calendar, default TTL).
  - `anime_mapping_data` (webhooks, default TTL).

## DB TTL guards and in-memory caches
- Plex sections refresh:
  - `_should_refresh_plex_sections()` uses `PlexAccount.sections_refreshed_at` with `PLEX_SECTIONS_TTL_HOURS`.
- Backfill guardrails:
  - `MetadataBackfillState` enforces retry windows and give-up behavior.
- Request-scope caches:
  - `history_cache.py` uses local maps (genre_cache, episode_title_map, track_duration_cache).
  - `statistics.py` uses `season_metadata_cache` and avoids API calls unless cached metadata exists.
- ORM prefetch cache invalidation:
  - `refresh_from_db()` + `prefetch_related_objects()` used after progress updates.

## 768 MB environment notes
- History and statistics moved to per-day caches to avoid scanning and aggregating huge ranges in one task.
- Cold misses schedule background refreshes and return empty payloads rather than inline rebuilding entire history.
- Statistics "All Time" uses a sparse day list (distinct activity days) instead of contiguous day buckets.
- This reduces memory spikes but does not fully guarantee 768 MB safety yet; continue profiling under real workloads.

## Redis memory notes
- Approx key count per user per logging_style: `~#days` day payloads + 1 index + locks. For long histories (10k+ days), key volume can be large even with 6h TTLs.
- Redis eviction policy matters in a 768 MB footprint (allkeys-lru vs volatile-lru changes whether hot caches survive long enough for paging).
