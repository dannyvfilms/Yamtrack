# PR #1 Review Cleanup — Design

**Date:** 2026-04-20
**PR:** https://github.com/WybeBosch/Yamtrack/pull/1 (`fix/pocketcasts-full-history-import`)
**Scope:** Minimum viable merge — fix correctness, safety, and drift issues flagged by Copilot and the PR author. Defer rate limiting, lock overhaul, `services.api_request` history-recording micro-optimization, and broader test authoring to follow-ups.

## Context

PR #1 rewrites the Pocket Casts importer to fetch full podcast history (replacing the history-endpoint approach that was capped at ~100 items and only tracked in-progress episodes). It also removes the RSS-based episode refresh and backfill code paths. Review surfaced 15 comments across 10 distinct issues. This design addresses the correctness and safety ones without overhauling subsystems.

## Issues addressed

| # | Source | Issue | In scope |
|---|--------|-------|----------|
| 1 | W1 + C2 + C8 | Migration ping-pong: `0111` widens `episode_uuid` to 500, `0114` shrinks back to 36 | Yes |
| 2 | W2 + C1 | `0114` accidentally removes three `Item` indexes (drift from missing `Meta.indexes`) | Yes |
| 3 | W8 | `processed_shows` set is dead code (init + add, no reader) | Yes |
| 4 | W3 | `_fetch_show_play_states` bare-except swallows 401, silently empties all remaining shows | Yes |
| 5 | C5 + W5 | `_fetch_show_full_metadata` uses raw `requests.get`, bypassing `services.api_request` | Yes |
| 6 | C7 | `podcast_mark_all_played` docstring no longer matches behavior (RSS backfill removed) | Yes (docstring only) |
| 7 | W7 | `refresh_podcast_episodes` removal is a behavior change, not dead code | Yes (PR description note) |
| 8 | C4 + W10 | No test coverage for new fetch/join code paths | Yes (4 focused tests) |
| 9 | W4 | Hardcoded 10-page cap, no rate limiting across per-show loop | **Deferred** |
| 10 | C3 + W6 | Cache lock TTL (600s) marginal for large accounts | **Deferred** |
| 11 | C6 | Multi-query history recording (`exists()` + `count()` + `first()`) | **Deferred** |

## Design

### 1. Migrations

Unreleased branch — safe to delete/rewrite.

- **Delete** `src/app/migrations/0111_alter_podcastepisode_episode_uuid.py`. The widen was motivated by RSS GUIDs that this PR removes.
- **Delete** `src/app/migrations/0114_remove_item_app_item_metadata_fetched_idx_and_more.py`. The shrink-back is unnecessary without `0111`; the three `RemoveIndex` operations are drift from missing `Meta.indexes` on `Item`.
- **Edit** `src/app/migrations/0112_alter_item_media_id.py`: change `dependencies = [("app", "0111_...")]` to `dependencies = [("app", "0110_item_manual_metadata")]`.
- **Edit** `src/app/models.py` — add an `indexes = [...]` block to `Item.Meta` re-declaring the three indexes added in `0106_add_item_query_indexes.py`:
  - `models.Index(fields=["metadata_fetched_at"], name="app_item_metadata_fetched_idx")`
  - `models.Index(fields=["release_datetime"], name="app_item_release_dt_idx")`
  - `models.Index(fields=["trakt_popularity_rank"], name="app_item_trakt_pop_rank_idx")`

Post-condition: migration chain is `0110 → 0112 → 0113`. `python manage.py makemigrations --check` reports no pending changes.

**Local dev DB note:** if the author's local DB already has `0111` / `0114` applied, they'll need one of:
- `python manage.py migrate app 0110` before pulling this change (rolls back to pre-0111), then pull and `migrate` forward; or
- accept that the local DB has a wider `episode_uuid` column than the model declares (harmless — the model says 36, the DB accepts up to 500, Django only reads/writes within 36). Fresh clones and production deploys are unaffected.

### 2. Dead code removal (`src/integrations/imports/pocketcasts.py`)

- Remove line `self.processed_shows = set()` at `:255`.
- Remove line `self.processed_shows.add(show)` at `:1466`.

### 3. Auth error handling (`src/integrations/imports/pocketcasts.py`)

Approach: loud failure on 401, graceful skip on everything else. This makes token expiry during long imports visible (user retries, pre-loop `_ensure_valid_token()` handles refresh) instead of silently producing an empty import.

In `_fetch_show_play_states`:

```python
try:
    response = services.api_request(...)
    episodes = response.get("episodes", [])
    return {ep["uuid"]: ep for ep in episodes if "uuid" in ep}
except requests.HTTPError as e:
    if e.response is not None and e.response.status_code == 401:
        msg = "Pocket Casts token expired during import — please retry"
        raise MediaImportError(msg) from e
    logger.warning("HTTP error fetching play states for podcast %s: %s", podcast_uuid, e)
    return {}
except Exception as e:  # noqa: BLE001
    logger.warning("Failed to fetch play states for podcast %s: %s", podcast_uuid, e)
    return {}
```

The same `HTTPError`-then-`Exception` pattern is applied in `_fetch_show_full_metadata` after the switch to `services.api_request`.

### 4. Shared HTTP wrapper (`src/integrations/imports/pocketcasts.py`)

Replace the raw `requests.get(url, params=params, timeout=30, allow_redirects=True)` block in `_fetch_show_full_metadata` (around `:1261`) with:

```python
response = services.api_request("POCKETCASTS", "GET", url, params=params)
```

`services.api_request` handles `settings.REQUEST_TIMEOUT`, 429 retry with `Retry-After`, and error mapping. Redirect following is on by default in `requests.Session`. JSON parsing is built in — `response` is already the parsed dict.

### 5. Docstring fix (`src/app/views.py`)

Update the `podcast_mark_all_played` docstring (around `:12276`) from:

> "Mark all unplayed episodes for a podcast show as completed on their release date."

to:

> "Mark all episodes of this podcast currently in the library as completed on their release date. Episodes not yet imported from Pocket Casts are not included — run a Pocket Casts import first to fetch the full episode list."

No behavior change. No template/UI copy change.

### 6. PR description note

Use `gh pr edit 1 --body <new body>` to add under the existing `## Notes` section:

> - Removed `refresh_podcast_episodes` task and its calendar-reload invocation. New episodes for subscribed podcasts now appear only after the next Pocket Casts import (recurring task runs every 2 hours), not via RSS refresh on calendar reload.

### 7. Tests (`src/integrations/tests/imports/test_pocketcasts.py`)

Add a new `PocketCastsImportFlowTests(TestCase)` class at the end of the file. Reuse the test-account/user setup pattern from `PocketCastsInferenceTests` (`cls.setUpTestData`).

Mock targets (use `unittest.mock.patch`):

- `integrations.pocketcasts_api.get_podcast_list`
- `integrations.imports.pocketcasts.PocketCastsImporter._ensure_valid_token` (no-op)
- `integrations.imports.pocketcasts.PocketCastsImporter._get_access_token` (return `"fake-token"`)
- `app.providers.services.api_request`

Tests:

1. **`test_import_happy_path`** — one podcast in the subscribe list, two episodes: `uuid-a` `playingStatus=3` (played), `uuid-b` `playingStatus=2` (in progress, `playedUpTo=300`). Full-metadata returns both. Assert: 2 `Podcast` rows, one `status=COMPLETED`, one `status=IN_PROGRESS` with `progress` set.

2. **`test_import_skips_episode_with_missing_metadata`** — play state has two episodes, full-metadata returns only `uuid-a`. Assert: 1 `Podcast` row for `uuid-a`, no rows for `uuid-b`, no exceptions.

3. **`test_fetch_full_metadata_pagination`** — call `_fetch_show_full_metadata` directly. Mock `services.api_request` to return `{"podcast": {"episodes": [{"uuid": "e1", ...}]}, "has_more_episodes": True}` on the first call and `{"podcast": {"episodes": [{"uuid": "e2", ...}]}, "has_more_episodes": False}` on the second. Assert: both `e1` and `e2` in result dict; `services.api_request` called twice; second call's `params` includes `page=2`.

4. **`test_fetch_play_states_401_raises_media_import_error`** — mock `services.api_request` to raise `requests.HTTPError` with a response mock whose `status_code == 401`. Assert: calling `_fetch_show_play_states` raises `MediaImportError` whose message mentions "token".

Each test ≤ 40 lines. No shared network. No real DB beyond Django's transactional test case defaults.

### 8. Completion gate

Run in order from repo root, all must pass clean:

```bash
./venv/bin/ruff check --fix src/
./venv/bin/ruff format src/
./venv/bin/python src/manage.py makemigrations --check
./venv/bin/python src/manage.py test integrations.tests.imports.test_pocketcasts
```

Only commit once all four pass.

## Out of scope (explicitly deferred)

- **Rate limiting / page-cap configurability** (W4). Requires a real-world page-size observation first.
- **Cache lock design** (C3, W6). Current 600s is marginal but not broken for typical accounts; a DB-row lock is the right fix but that's a subsystem change.
- **History recording multi-query** (C6). Query-count optimization, not correctness.
- **Test coverage for `_build_episode_data`, `_process_episode` episode-number+date fallback, rate limiting, lock behavior**. The 4 tests above cover the fetch/join surface; the rest belong to follow-ups.
- **Removing `docs/agents/` files**. Kept intentionally by the author.

## Post-merge follow-up issues to file

- Rate-limiting + configurable page cap for `_fetch_show_full_metadata`.
- DB-row lock for Pocket Casts import concurrency.
- History-recording query consolidation.
- Expanded test coverage for the full-history import path.
