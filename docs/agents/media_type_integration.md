# Media Type Integration Playbook

This document explains how media types are defined and wired through the app so an agent can add a new media type safely.

## Core definitions
- Enums: `MediaTypes` and `Sources` in `src/app/models.py` govern valid `media_type` and `source` values everywhere (converters, forms, lookups).
- Config: `src/app/media_type_config.py` supplies per-type properties used by UI/logic (sources/default_source, sample_query/search URL, unicode/svg icons, verb, text/stats colors, date_key for releases, units, sample queries).
- URL validation: `src/app/converters.py` regexes are built from `MediaTypes.values`/`Sources.values`.

## Models & storage
- `Item` holds shared identity fields (media_id, source, media_type, title, image, runtime_minutes, season/episode numbers, release_datetime) with constraints preventing season/episode numbers on non-TV types and validating enum membership.
- Tracking models: abstract `Media` with history, score/progress/status/dates/notes, progress/status processors tied to provider `max_progress`. Concrete subclasses: `TV`, `Season`, `Anime`, `Manga`, `Movie`, `Game` (progress in minutes), `Book`, `Comic`, `Music`; `Episode` is separate.
- TV/Season cascade: `TV` aggregates seasons/episodes for progress/dates/status; `Season` manages episodes and syncs back to TV; `Episode.save` updates season/TV status.
- Music hierarchy: `Artist` and `Album` are container models (not Media subclasses). `Track` holds MusicBrainz metadata (like `Episode`). `Music` is the per-user tracking model. `ArtistTracker` and `AlbumTracker` provide TV-like tracking for artists/albums with status, score, dates, notes. See `docs/agents/music_integration.md` for full details.
- Manager: `BasicMedia.objects` (`MediaManager`) dynamically resolves models by `media_type`, applies prefetch (TV/Season episodes), annotates `max_progress`, aggregates duplicates, and sorts lists. Home/in-progress skips TV unless explicitly requested (`load_media_type`). Music list shows `ArtistTracker` entries instead of `Music` entries for a TV-like experience.
- Runtime: `Item.runtime_minutes` populated from metadata/episodes; special handling in stats/time-left; `999999` means “unknown”.

## User preferences, visibility, sidebar, search
- Per-type fields on `User` (`<type>_enabled/layout/sort/status`) in `src/users/models.py`; `get_enabled_media_types` reads them dynamically; `get_active_media_types` auto-inserts `season` if TV enabled.
- Sidebar settings view (`src/users/views.py:sidebar`, template `src/templates/users/sidebar.html`) toggles all `MediaTypes` except `episode`; `hide_from_search` hides disabled types from the search selector.
- Search selector (`src/templates/base.html`, `get_search_media_types`) excludes seasons by design; uses `user.last_search_type` unless current page type.

## UI surfaces
- Navigation: Sidebar entries from `get_sidebar_media_types` + `app_tags.icon` (icons from `media_type_config.svg_icon`).
- Home: `src/app/views.py:home` + `templates/app/home.html`/`components/home_grid.html`; uses `BasicMedia.get_in_progress`; Season entries show next event; units/colors from config.
- Media list: `views.media_list` + `templates/app/media_list.html` and table/grid components. Movies hide the progress column; TV has a `time_left` sort with custom runtime logic; episodes are never routed to standalone detail pages. Generic filters (status/sort/search/layout) operate per media_type.
- Detail pages: `media_details`/`season_details` (`templates/app/media_details.html`) render provider metadata; season view builds episodes via TMDB/manual. Sync button uses `sync_metadata`. Music uses dedicated templates: `music_artist_detail.html` (like TV show), `music_album_detail.html` (like Season).
- Search results: `templates/app/search.html` builds source tabs from `media_type_config.sources`; layout toggle; pagination generic.
- Custom lists: `lists`/`list_detail` views accept any `media_type`; MediaManager prefetch/annotate per type.
- Manual create: `ManualItemForm` + `create_entry` supports all types; seasons/episodes require parent TV/Season; title auto-filled from parent.
- Track modal/CRUD: `track_modal`, `media_save`, `media_delete`, `progress_edit`, `episode_save` all route on `media_type` string; forms in `src/app/forms.py` per type (Game uses duration field).
- History: the history modal `(views.history_modal)` dynamically resolves Historical<media_type> via apps.get_model. Any new Media subclass automatically gets a historical model through Media.history, so no extra wiring is needed beyond adding the media type itself.

## Statistics
- Data assembly in `src/app/statistics.py`: iterates `user.get_active_media_types`; if `season_enabled` is false, seasons are removed from counts. TV/Season queries prefetch episodes; others filter by date window.
- Charts: media type distribution, score/status distribution, activity heatmap, hours/plays per media type. Colors from `media_type_config.stats_color`.
- Spotlight sections are hard-coded for Movies, TV, Games, Anime (top played cards); new types won’t appear there without template edits.
- Runtime/units: Minutes per media type rely on `Item.runtime_minutes` or provider runtime; fallback 60 minutes for unknown types; movies use cached runtime; TV/anime use episode runtimes; games use progress minutes.
- Music rollups: genres/decades/countries are derived from album→artist metadata; country codes are expanded to full names for display.

## Calendar, releases, notifications
- Event creation (`src/events/calendar.py`): processes all types except seasons/episodes directly; skips manual sources. Branches:
  - Anime: AniList schedule via bulk query.
  - TV: pulls seasons/episodes from TMDB (+ TVMaze airstamps) and creates Season/Episode Items/events.
  - Comics: updates only if latest event within a year.
  - Others: uses `media_type_config.date_key` and `metadata["max_progress"]` to build events; movies have `content_number=None`.
  - MangaUpdates with max_progress but no end date uses sentinel datetime.
- `Item.fetch_releases` triggers calendar reload on status changes; seasons delegate to parent TV.
- Notifications (`src/events/notifications.py`): filters by user-enabled media types and exclusions; formats bodies with unicode icons; Season header labeled “TV Shows,” others uppercase media type.

## Providers, search, sync
- Routing in `src/app/providers/services.py`: tmdb(tv/movie/season/episode), mal/mangaupdates(anime/manga), igdb(game), hardcover/openlibrary(book), comicvine(comic), musicbrainz(music), manual fallback. Each returns a dict with `media_id/source/media_type/title/max_progress/image/synopsis/score/score_count/details/related` (+ runtime/episodes).
- Search routing matches provider; sample queries/URLs from `media_type_config`.
- Music search uses `search_combined()` which returns artists, albums, and tracks from MusicBrainz. Cover art is skipped during search (`skip_cover_art=True`) for performance; art loads when viewing artist/album pages.
- `sync_metadata` view: clears cache key, refetches metadata, updates Item title/image (season also bulk-updates episode posters), and triggers `item.fetch_releases`; blocks manual sources.

## Imports/exports, webhooks, automation
- CSV export (`src/integrations/exports.py`) iterates `MediaTypes.values`; prefetch for TV/Season→Episode; game progress formatted hh:mm.
- Imports (`src/integrations/imports/helpers.py` + source modules) loop over media types (skip season/episode) and expect concrete models per `media_type`. Overwrite mode deletes existing items by type/source. New types need an import handler to create items + trackers.
- Webhooks (`src/integrations/webhooks/*.py`) handle TV/Movie with optional anime mapping; music scrobbles are currently only supported from Plex webhooks (not Jellyfin/Emby) and normalized via `music_scrobble.record_music_playback`. Unknown media types are ignored. Extend mapping/handlers for new types.
- Auto-pause (`src/app/services/auto_pause.py` + `AUTO_PAUSE_MEDIA_TYPES` in `users/views.py`) covers game, movie, season, anime, manga, book, comic; extend maps for new types.

## Edge cases & special rules
- Season is auto-added to active types when TV is enabled; hiding seasons in settings removes them from stats and sidebar.
- Search selector excludes seasons/episodes by design (`EXCLUDED_SEARCH_TYPES` in `users/models.py`).
- Icons/colors/units/date_key must exist in `media_type_config` or templates/notifications will fail.
- Runtime defaults: 30m TMDB, 23m MAL; `999999` runtime is treated as unknown/skip in time-left/stats.
- Music has its own hierarchy: Artist → Album → Track (not managed by MediaManager). Music list shows `ArtistTracker` entries, not `Music` entries. Artist and Album pages use HTMX for async cover art loading. See `docs/agents/music_integration.md`.
- History view:
  - Movies show a play-count badge derived from the user’s local play history (aggregated by media_id/source) and default to `1 play` if no repeats are found.
  - Games obey the user preference `game_logging_style` (`sessions` vs `repeats`): sessions put the full progress on the end date with an hour-only badge; repeats spread progress evenly across the date range and keep the per-day chip. Games without start/end dates are excluded from history to avoid pinning them to “today.”
  - History cache key includes the logging style; preference changes invalidate and trigger a background refresh to keep the view instantaneous.
- Statistics spotlight cards and some UI badges are hard-coded for specific types; new types need explicit template additions there.
- Calendar events for comics have 1-year look-back; manga from MangaUpdates uses sentinel datetime; unknown date_key causes release generation to fail.

## Checklist to add a new media type
1. Enums & config: add to `MediaTypes`, add `media_type_config` entry (sources, default_source, sample_query, unicode/svg icon, verb, text/stats colors, date_key, unit, sample search URL behavior).
2. Model & migration: create a `Media` subclass if needed (override progress/time formatting), add User fields (`<type>_enabled/layout/sort/status` + migration), ensure history tracking if required.
3. Providers & search: add provider module or extend `providers/services.py` routing for metadata/search; ensure responses include `max_progress`, runtime, date fields referenced by `date_key`.
4. UI wiring: update sidebar/search visibility rules if the type should appear; ensure templates that branch on media_type (tables, cards, spotlight sections) include the new type where desired.
5. Calendar/notifications: implement release fetch logic if not covered by default date_key path; confirm `unicode_icon` exists; update notification grouping label if special casing is needed.
6. Imports/exports: add import handler; confirm `exports` iteration works (units/progress formatting if non-numeric).
7. Webhooks/automation: extend webhook media_type mapping if applicable; add to auto-pause maps if desired.
8. Testing: add coverage for list/search/detail/statistics/calendar release generation and notifications for the new type; verify sync and manual create flows.

## Music-specific notes
Music follows a hierarchical pattern (Artist → Album → Track) similar to TV (TV → Season → Episode). Key differences:
- Artist/Album are container models, not Media subclasses
- `ArtistTracker` and `AlbumTracker` handle user tracking (status, score, dates, notes)
- `Track` is the metadata catalog (populated from MusicBrainz)
- `Music` (Media subclass) tracks individual song listens
- Music list shows `ArtistTracker` entries, not `Music` entries
- Cover art loads asynchronously via HTMX for performance
- Wikipedia integration provides artist bios and photos

For comprehensive music documentation, see `docs/agents/music_integration.md`.
