# Pocket Casts — Second Discovery Run Handoff

**Date:** 2026-04-20  
**For:** AI with Playwright on Windows (second discovery pass)

---

## What we're trying to fix

The user runs `dannyvfilms/Yamtrack` (a Pocket Casts-enabled fork of Yamtrack) on a Raspberry Pi. The fork's `pocketcasts_import` task is supposed to import the user's full listening history, but it only ever processes episodes that are currently *in progress* (status=2). All 40 other subscribed podcasts are ignored on every import.

The import is driven by `POST /user/history`, which only returns currently in-progress episodes. The user has 41 subscribed podcasts with hundreds of completed episodes — none of those appear in Yamtrack.

---

## What we already know

### Known working endpoints (all `https://api.pocketcasts.com`, bearer-auth)

**`POST /user/podcast/list`** — returns all 41 subscribed podcasts.  
Each entry has: `uuid`, `title`, `author`, `slug`, `lastEpisodeUuid`, `lastEpisodePlayingStatus`, `dateAdded`, etc.  
No per-episode played state.

**`POST /user/podcast/episodes`** with payload `{"uuid": "<podcast_uuid>"}` — returns ALL episodes for one show.  
Example: for "What the Shell?" (`uuid: c38cc6d0-e767-0139-6999-0acc26574db2`) it returned 50 episodes.  
Each entry has **only**:
```json
{
  "uuid": "01ae23a7-ddac-4b2b-b39e-7e8e067c61d0",
  "playingStatus": 3,
  "playedUpTo": 0,
  "isDeleted": false,
  "starred": false,
  "duration": 0,
  "bookmarks": [],
  "deselectedChapters": ""
}
```
`playingStatus`: 0 or 1 = unplayed, 2 = in-progress, 3 = completed.  
**Critical problem: no title, no published date, no URL.**

**`POST /user/history`** with payload `{}` — returns all currently in-progress (status=2) episodes.  
Each entry has full metadata: `uuid`, `title`, `publishedAt`, `podcastUuid`, `podcastTitle`, `playedUpTo`, `episodeNumber`, `duration`, `slug`, etc.  
**Critical problem: only returns status=2 (in-progress) episodes. Completed episodes never appear here.**

### Endpoints that returned nothing useful

- `POST /user/episodes` with `{"uuids": ["uuid1","uuid2","uuid3"]}` → 200 but 0 results
- `POST /user/stats` → 401
- `POST /user/listening_history` → 401
- `POST /user/podcast/episodes` with `{"podcastUuid": "..."}` → 400 (wrong key name, correct key is `uuid`)

---

## The core problem

`/user/podcast/episodes` gives us **play state per episode** but no way to identify which episode it is (no title, no date).

`/user/history` gives us **full metadata** but only for in-progress episodes.

Pocket Casts assigns its own internal UUIDs to episodes. RSS feeds use completely different GUIDs (e.g., podbean uses `whattheshell.podbean.com/efb13b86-7091-...`). These are unrelated identifiers with no shared key.

To match a completed episode from `/user/podcast/episodes` to an RSS feed entry, we need either:
- Its **title** (exact string match), or
- Its **published date** (match by date)

**The Pocket Casts web SPA clearly shows episode titles alongside their played state.** Whatever API call it makes to render that view is the endpoint we're missing.

---

## What to discover

### Priority 1 — What does the SPA call for the podcast episode list page?

Navigate to `pocketcasts.com/podcasts` → click on a specific podcast (e.g., "What the Shell?") → capture ALL API calls made to render that episode list (the page where you can see episode titles + play state badges).

The HAR from the first run may have missed this if the SPA rendered the episode list from cached state. Try:
1. Hard refresh after navigating to the podcast detail page
2. Toggle sort order (newest/oldest) — this often re-fetches
3. Click "Filters" or any filter options on the episode list

Look for any `api.pocketcasts.com` endpoint that returns an array of objects containing both a `uuid` AND a `title` (and ideally `publishedAt` or `published`).

### Priority 2 — Probe these specific endpoints

All `POST` to `https://api.pocketcasts.com` with bearer auth.

```
POST /user/podcast/episode   {"uuid": "<episode_uuid>", "podcastUuid": "<podcast_uuid>"}
POST /user/podcast/episode   {"episodeUuid": "<episode_uuid>", "podcastUuid": "<podcast_uuid>"}
POST /user/episodes          {"uuid": "<podcast_uuid>"}
POST /user/episodes          {"podcastUuid": "<podcast_uuid>"}
POST /user/episodes          {"uuids": ["<episode_uuid_1>", "<episode_uuid_2>"]}  (try with 1 UUID, not 3)
POST /episode/find           {"uuid": "<episode_uuid>"}
POST /episode/find           {"uuid": "<episode_uuid>", "podcastUuid": "<podcast_uuid>"}
```

Use these specific UUIDs (from the user's "What the Shell?" podcast):
- Podcast UUID: `c38cc6d0-e767-0139-6999-0acc26574db2`
- Example episode UUIDs from `/user/podcast/episodes` (all status=3 completed):
  - `01ae23a7-ddac-4b2b-b39e-7e8e067c61d0`
  - `02a5bce1-ee61-4890-8f78-c56db0f5b621`
  - `073ecb34-6d7c-41ae-8fd8-f067e903c457`

### Priority 3 — Navigate deeper in the SPA

After clicking into a podcast detail page, also try:
- Click on a specific episode title → any episode detail page/modal
- Look for a "History" or "Listening History" tab in the profile/account menu
- Try the profile icon → any history-like pages (the previous run's HAR showed calls from `pocketcasts.com/` root but may not have navigated into profile pages)
- Sort the episode list by "Recently Played" if that option exists

### Priority 4 — GET endpoints

The previous run only tried POST. Try:
```
GET /podcasts/<podcast_uuid>/episodes
GET /episodes/<episode_uuid>
GET /user/episode/<episode_uuid>
```

---

## What success looks like

Finding **any** response that contains both:
1. A Pocket Casts internal episode UUID (matching format of `01ae23a7-ddac-4b2b-b39e-7e8e067c61d0`)
2. An episode **title** (string) and/or **published date**

If such an endpoint exists, we can chain it:
1. `POST /user/podcast/list` → get all 41 podcast UUIDs
2. `POST /user/podcast/episodes` per podcast → get per-episode play states
3. NEW ENDPOINT per episode UUID → resolve title + date
4. Match resolved episodes to RSS feed entries by title+date
5. Import played state into Yamtrack

---

## Context about the discovery script

The first run used Playwright (headed Chromium, HAR capture + REST probes). The script is at `pocketcasts-discovery/pocketcasts_discovery.mjs`. Credentials are in `pocketcasts_credentials.json` (gitignored). Artifacts are in `output/`.

You can extend the existing script or write a new one. The bearer token can be extracted from `localStorage` after login (the first script already does this). Base URL for all API calls is `https://api.pocketcasts.com`.

Report format should match `output/report.md` (endpoint | payload | status | episodes/items returned | response sample) so the Pi-side AI can directly use it to write the patch.
