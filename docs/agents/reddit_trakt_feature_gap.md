# Reddit r/trakt Thread Feature Gap Analysis (2026-02-03)

Source: Thread excerpt provided in task (r/trakt: "I'm building a Movies Tracker but can't find my differentiation. Please help!").
Scope: Map requested features to current Yamtrack (release branch) capabilities and highlight gaps.

---

## Thread Feature Requests (normalized)
- Scrobbling support (general playback sync)
- All-in-one tracker for movies, series, anime
- Jellyfin support
- Community comments + translation (multi-language discussion)
- Trakt support + stats
- Browse/discover new media
- Mobile + web support
- Not locked in (import/export)
- Export everything
- Rewatch support
- Combine TV + movies
- Rich metadata (actors, production companies) + filtering + "see what else they did"
- Lists for movies, series, seasons, episodes, people
- Copy/duplicate lists
- Filter by streaming service
- Calendar + continue streaming
- Stats (watchtime, genres, services/production companies)
- External links (IMDb, etc.)
- 10-star rating
- Collection/library of owned media
- Notes
- Status (ongoing/cancelled)
- Quick add-to-list popup
- Original titles
- Powerful filters (genre, language, year, metadata)
- Different views/sorting (list, poster, etc.)
- Auto-lists based on filters
- Tags
- Streaming sources worldwide
- Custom searches (Fandom, Rotten Tomatoes, etc.)
- Collections of lists
- Not interested / hide
- Bulk select + copy/delete
- Compare lists (include/exclude)
- Hide items from auto lists
- List folders

---

## Already Supported in Yamtrack
- All-in-one tracker (movies, TV, anime, plus books, games, comics, board games, music, podcasts).
  - Media types: `src/app/models.py` (MediaTypes)
- Trakt integration (import watch history and lists).
  - Imports + OAuth: `src/integrations/imports/trakt.py`, `src/integrations/views.py`, `src/templates/users/integrations.html`
- Stats dashboard with charts, time ranges, genres, watchtime, etc.
  - `src/app/statistics.py`, `src/app/statistics_cache.py`, `src/templates/app/statistics.html`
- Calendar + upcoming release tracking.
  - `src/events/calendar.py`, `src/events/tasks.py`, `src/templates/app/calendar.html`
- Mobile + web support (responsive UI + PWA).
  - `src/templates/base.html`, `src/static/`, README "Mobile-First Experience"
- Import/export to avoid lock-in (Yamtrack CSV export + import).
  - Export: `src/integrations/exports.py`
  - Import: `src/integrations/imports/yamtrack.py`
- Lists with public sharing, recommendations, activity, and list tags.
  - `src/lists/models.py`, `src/lists/views.py`, `src/templates/lists/*`
- Quick add-to-list modal (small popup UI).
  - `src/lists/views.py` (lists_modal), `src/templates/lists/components/fill_lists.html`, `src/templates/app/components/media_card.html`
- External links (IMDb/TVDB/Wikidata) in details.
  - `src/app/providers/tmdb.py` (external_links), `src/templates/app/media_details.html`
- 10-star rating + notes.
  - `src/app/models.py` (score, notes), `src/templates/app/components/*track*`
- Collection/library of owned media + collection filters.
  - `src/app/models.py` (CollectionEntry), `src/app/views.py` (collection_*), `src/templates/app/collection_list.html`
- Powerful filters and sorting on media lists (genre, year, source, language, country, platform).
  - `src/app/views.py` (media_list filters), `src/templates/app/media_list.html`
- Multiple list/grid/table views + sort direction toggles.
  - `src/templates/app/media_list.html`, `src/app/views.py`

---

## Partially Supported (Needs Extensions)
- Scrobbling support (general playback sync)
  - Current: Plex/Emby/Jellyfin webhooks + Last.fm music scrobbles.
  - Missing: broader player coverage (e.g., Trakt scrobble, local players, mobile apps).
  - Implementation notes: add new webhook processors or polling integrations, plus user settings and mapping.
  - Likely files: `src/integrations/webhooks/*`, `src/integrations/tasks.py`, `src/integrations/views.py`, `src/integrations/models.py`, `src/templates/users/integrations.html`

- Jellyfin support
  - Current: Jellyfin webhooks (playback events) and collection metadata parsing.
  - Missing: full Jellyfin library import/sync.
  - Implementation notes: add Jellyfin API client + importer (similar to Plex), expose settings.
  - Likely files: `src/integrations/imports/`, `src/integrations/views.py`, `src/integrations/tasks.py`, `src/integrations/models.py`, `src/templates/users/integrations.html`

- Rewatch support
  - Current: repeated history records for episodes/music/podcasts; history view supports repeats.
  - Missing: explicit rewatch UI (per-item rewatch counter) for all media types.
  - Implementation notes: add repeat logging UX for movies/TV and surface counts.
  - Likely files: `src/app/views.py`, `src/app/history_cache.py`, `src/templates/app/media_details.html`, `src/templates/app/history.html`

- Metadata galore (actors/production companies) + filtering
  - Current: metadata details exist for some providers; external links shown.
  - Missing: cast/crew/companies stored + filterable + "see more from X" views.
  - Implementation notes: cache credits/companies per item; add People/Company models or JSON; add filters and detail pages.
  - Likely files: `src/app/providers/tmdb.py`, `src/app/models.py`, `src/app/views.py`, `src/templates/app/media_details.html`, `src/templates/app/media_list.html`

- Lists for "people" (actors, etc.)
  - Current: lists support items (media) only.
  - Missing: list entries for people and non-media entities.
  - Implementation notes: add polymorphic list items (GenericForeignKey) or new list item types.
  - Likely files: `src/lists/models.py`, `src/lists/views.py`, `src/templates/lists/*`, migrations

- Status (ongoing/cancelled)
  - Current: user tracking status is shown; provider status not consistently surfaced or filterable.
  - Missing: show/season metadata status in UI + filter.
  - Implementation notes: store provider status in Item metadata; display and filter it.
  - Likely files: `src/app/providers/tmdb.py`, `src/app/models.py`, `src/app/views.py`, `src/templates/app/media_details.html`

- Tags
  - Current: list tags exist; media tags do not.
  - Missing: per-media user tags and tag filters.
  - Implementation notes: add tag model/M2M on Media or Item, update UI and filters.
  - Likely files: `src/app/models.py`, `src/app/views.py`, `src/templates/app/media_details.html`, `src/templates/app/media_list.html`

- "All-in-one" movies/series/anime view
  - Current: per-type media lists + history combines types.
  - Missing: dedicated combined library view (single list that merges multiple types).
  - Implementation notes: add aggregated query + unified filters; add route + template.
  - Likely files: `src/app/views.py`, `src/app/urls.py`, `src/templates/app/media_list.html` (or new template)

---

## Not Yet Supported (New Work Required)
- Community comments + translation
  - Summary: add discussion threads, comment models, translation API integration, moderation.
  - Files: `src/app/models.py` (new models) or new app, `src/app/views.py`, `src/app/urls.py`, `src/templates/app/*`, `src/static/js/*`, migrations

- Browse/discover media (curated/trending)
  - Summary: add discovery endpoints using provider APIs (TMDB trending/popular), add caching and UI.
  - Files: `src/app/providers/tmdb.py`, `src/app/views.py`, `src/app/urls.py`, `src/templates/app/discover.html`

- Filter by streaming service + worldwide providers
  - Summary: store watch-provider data (per region) and expose filters.
  - Files: `src/app/providers/tmdb.py`, `src/app/models.py` (store providers), `src/app/views.py`, `src/templates/app/media_list.html`, `src/app/config.py`

- Auto-lists based on filters
  - Summary: allow saved filters as dynamic lists; update list rendering to pull query results.
  - Files: `src/lists/models.py`, `src/lists/views.py`, `src/lists/templates/*`, `src/lists/tasks.py` (optional refresh)

- Copy/duplicate lists
  - Summary: add list cloning endpoint and UI action.
  - Files: `src/lists/views.py`, `src/lists/urls.py`, `src/templates/lists/list_detail.html`, tests

- Bulk select + copy/delete (lists)
  - Summary: add multi-select UI and bulk actions for list items.
  - Files: `src/templates/lists/list_detail.html`, `src/static/js/*`, `src/lists/views.py`

- Compare lists (include/exclude)
  - Summary: list comparison view with intersection/diff logic.
  - Files: `src/lists/views.py`, `src/lists/urls.py`, `src/templates/lists/compare.html`, tests

- List folders / collections of lists
  - Summary: add list folder model and UI grouping.
  - Files: `src/lists/models.py`, `src/lists/views.py`, `src/templates/lists/custom_lists.html`, migrations

- Not interested / hide items
  - Summary: add per-user hide list and apply to recommendations/search.
  - Files: `src/app/models.py`, `src/app/views.py`, `src/app/providers/services.py`, `src/templates/app/*`

- Original titles
  - Summary: display original title metadata (from TMDB/MAL) and allow filter/search.
  - Files: `src/app/providers/tmdb.py`, `src/app/providers/mal.py`, `src/app/models.py`, `src/templates/app/media_details.html`

- Custom searches (Fandom/Rotten Tomatoes/etc.)
  - Summary: add outbound search links in media details or integrate provider APIs.
  - Files: `src/templates/app/media_details.html`, `src/app/config.py` (link templates), optional new provider modules

- List everything including people (actors/crew) + list folders + list collections
  - Summary: requires People/Company models and list polymorphism; see partial notes above.
  - Files: `src/app/models.py`, `src/lists/models.py`, `src/lists/views.py`, templates, migrations

---

## Quick Notes
- The "lists import" gap mentioned in the thread is now covered by the Yamtrack CSV import/export updates (media + lists).
- Some requests are non-product (pricing, Discord support); those are outside the codebase but can be addressed in README/community docs.
