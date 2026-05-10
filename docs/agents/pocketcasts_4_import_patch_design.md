# Pocket Casts Import Patch — Design

**Date:** 2026-04-20  
**Status:** Approved

---

## Problem

The `dannyvfilms/Yamtrack` fork drives its Pocket Casts import off `POST /user/history`, which only returns episodes currently *in progress* (playingStatus=2). As soon as an episode is finished, it vanishes from history. Result: all 40 of 41 subscribed podcasts are silently ignored on every import; completed episodes are never recorded.

---

## Discovery findings (from prior discovery runs)

Two complementary endpoints, combined, give us everything we need:

**`POST https://api.pocketcasts.com/user/podcast/episodes`** (bearer auth)  
Payload: `{"uuid": "<podcast_uuid>"}`  
Returns all episodes the user has interacted with, keyed by episode UUID.  
Fields per episode: `uuid, playingStatus, playedUpTo, isDeleted, starred, duration, bookmarks, deselectedChapters`  
No title, no published date.

**`GET https://podcast-api.pocketcasts.com/podcast/full/<podcast_uuid>`** (no auth — follows 302 to CDN JSON)  
Returns full episode metadata for the entire show.  
Fields per episode: `uuid, title, slug, url, file_type, file_size, duration, published, type, season, number`  
Also returns `episode_count` and `has_more_episodes` at the top level.

Inner-joining these two on episode UUID gives per-episode played state + full metadata — everything `_process_episode()` already expects.

---

## What changes

### `import_data()` — restructured loop only

Remove the early-exit on empty history:
```python
# REMOVED:
if not episodes:
    return {}, ""
```

Replace the `_fetch_history()` → `for episode_data in episodes` block with a per-podcast loop:

```python
for podcast_uuid, podcast_meta in self.podcast_metadata.items():
    play_states = self._fetch_show_play_states(podcast_uuid)
    if not play_states:
        continue
    full_metadata = self._fetch_show_full_metadata(podcast_uuid)
    if not full_metadata:
        continue
    for ep_uuid, play_state in play_states.items():
        metadata_ep = full_metadata.get(ep_uuid)
        if not metadata_ep:
            continue  # episode in play states but absent from public feed — skip
        episode_data = self._build_episode_data(
            play_state, metadata_ep, podcast_uuid, podcast_meta
        )
        self._process_episode(episode_data, defer_completion_date=not is_first_import)
```

Everything after the loop (second-pass completion-date inference, `processed_shows` RSS sync, etc.) stays exactly as-is.

### `_fetch_history()` — kept, no longer called from `import_data()`

Leave the method in place. The original history-driven approach was fundamentally broken for tracking completions (episodes disappear from history the moment they finish), so we no longer call it from the main import flow. However, it is left in the codebase because it is unclear whether other parts of Yamtrack call it. Do not delete it.

Add a comment at the top of the method:
```python
# NOTE: This method is no longer called from import_data(). The main import
# now uses _fetch_show_play_states() + _fetch_show_full_metadata() per podcast,
# which correctly handles completed episodes (history only returns in-progress).
# Left in place in case other Yamtrack code paths reference it.
```

---

## Three new helper methods

### `_fetch_show_play_states(podcast_uuid) -> dict[str, dict]`

```
POST https://api.pocketcasts.com/user/podcast/episodes
{"uuid": podcast_uuid}
Authorization: Bearer <token>
```

Returns `{episode_uuid: {playingStatus, playedUpTo, isDeleted, starred, duration, bookmarks}}`.  
Returns `{}` on any failure (logs warning, caller skips the show).

### `_fetch_show_full_metadata(podcast_uuid) -> dict[str, dict]`

```
GET https://podcast-api.pocketcasts.com/podcast/full/<podcast_uuid>
(no auth — follows 302 redirect automatically)
```

Returns `{episode_uuid: {title, slug, published, url, file_type, duration, type, season, number}}`.  
Handles `has_more_episodes: true` by looping with `?page=2`, `?page=3`, etc. up to a safety cap of 10 pages. If the cap is hit, logs a warning and continues with what was collected.  
Returns `{}` on any failure (logs warning, caller skips the show).

### `_build_episode_data(play_state, metadata_ep, podcast_uuid, podcast_meta) -> dict`

Pure function, no I/O. Merges the two dicts into the shape `_process_episode()` already expects:

```python
{
    "uuid":          play_state["uuid"],
    "podcastUuid":   podcast_uuid,
    "podcastTitle":  podcast_meta["title"],
    "author":        podcast_meta["author"],
    "podcastSlug":   podcast_meta["slug"],
    "title":         metadata_ep["title"],
    "slug":          metadata_ep["slug"],
    "published":     metadata_ep["published"],
    "url":           metadata_ep["url"],
    "fileType":      metadata_ep["file_type"],
    "duration":      metadata_ep["duration"],
    "episodeType":   metadata_ep["type"],
    "episodeSeason": metadata_ep["season"],
    "episodeNumber": metadata_ep["number"],
    "playingStatus": play_state["playingStatus"],
    "playedUpTo":    play_state["playedUpTo"],
    "starred":       play_state["starred"],
    "isDeleted":     play_state["isDeleted"],
    "bookmarks":     play_state.get("bookmarks", []),
}
```

---

## Error handling

| Situation | Behaviour |
|-----------|-----------|
| `/user/podcast/episodes` fails (4xx/5xx/network) | Log warning, `play_states = {}`, skip show |
| `podcast-api.pocketcasts.com` fails | Log warning, `full_metadata = {}`, skip show |
| Episode UUID in play states but not in full metadata | Skip episode silently (deleted from feed) |
| `has_more_episodes: true` page loop hits 10-page cap | Log warning, continue with collected pages |
| One podcast fails | Does not abort the other 40 |

---

## What does NOT change

- `_process_episode()` — untouched. All create/match/deduplication/status/completion logic stays exactly as written.
- Second-pass completion-date inference block in `import_data()`.
- `processed_shows` tracking and any RSS sync that follows.
- `_fetch_history()` — kept but commented (see above).
- Auth/token management — bearer token still required for `/user/podcast/episodes`.

---

## API call volume

Per import run: 1 (`/user/podcast/list`) + 41 (`/user/podcast/episodes`) + 41 (`podcast-api.pocketcasts.com/podcast/full`) = **83 calls**.  
Previous: 2 calls (podcast list + history). Increase is acceptable for a background sync job.

---

## Testing strategy

Run the import against the real account after patching, verify:

1. All 41 podcasts appear in Yamtrack (previously: 1).
2. "What the Shell?" shows 49 completed + 1 unplayed (previously: 1 in-progress or none).
3. "Zo, Opgelost" shows the correct mix of completed / in-progress / unplayed.
4. No duplicate episodes created (title+date dedup in `_process_episode()` should handle it).
5. No `value too long for type character varying` errors (DB columns already widened in a prior session).

No unit tests planned — one-shot patch against a live account is the fastest verification for a fix this targeted.
