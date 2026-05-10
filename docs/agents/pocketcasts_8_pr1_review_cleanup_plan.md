# PR #1 Review Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the correctness and safety items from PR #1's review feedback (Copilot + author) without overhauling subsystems.

**Architecture:** Targeted edits across migrations (drop 2, fix deps, add model indexes), `pocketcasts.py` (remove dead code, loud 401 failure, shared HTTP wrapper), `views.py` (docstring), and `test_pocketcasts.py` (4 new tests). Finish with a completion gate that runs `ruff`, `makemigrations --check`, and the Pocket Casts test subset.

**Tech Stack:** Django 5.2, Python 3.12, `ruff` (lint + format), `requests`, `unittest.mock`, `gh` CLI.

**Spec:** `docs/agents/pocketcasts_7_pr1_review_cleanup_design.md`

**Commit style:** Single-line messages, no body, no Co-Authored-By trailer.

---

## File Structure

Files touched (all existing — no new files except this plan):

- **Delete:** `src/app/migrations/0111_alter_podcastepisode_episode_uuid.py`
- **Delete:** `src/app/migrations/0114_remove_item_app_item_metadata_fetched_idx_and_more.py`
- **Modify:** `src/app/migrations/0112_alter_item_media_id.py` (1 line — `dependencies`)
- **Modify:** `src/app/models.py` (add `indexes = [...]` inside `Item.Meta`)
- **Modify:** `src/integrations/imports/pocketcasts.py` (remove 2 dead lines, update 2 fetch methods)
- **Modify:** `src/app/views.py` (1 docstring)
- **Modify:** `src/integrations/tests/imports/test_pocketcasts.py` (add `PocketCastsImportFlowTests` class)
- **PR body:** `gh pr edit 1 --body "..."` (no file change)

---

## Task 1: Re-declare Item indexes in the model

**Why this is task 1:** `makemigrations --check` needs to report no drift after we delete `0114`. Adding `Meta.indexes` makes Django stop auto-generating `RemoveIndex` operations.

**Files:**
- Modify: `src/app/models.py` (around `:229` — `Item.Meta`)

- [ ] **Step 1: Read current `Item.Meta` to confirm insertion point**

Run: `grep -n "class Meta:" src/app/models.py | head -3`
Expected: first match is `Item.Meta` around line 229.

- [ ] **Step 2: Add `indexes` block inside `Item.Meta`**

Find the line `ordering = ["media_id"]` (around `:307`, immediately after the `constraints = [...]` list closes). Insert a new `indexes = [...]` block **before** `ordering = ["media_id"]`:

```python
        indexes = [
            models.Index(
                fields=["metadata_fetched_at"],
                name="app_item_metadata_fetched_idx",
            ),
            models.Index(
                fields=["release_datetime"],
                name="app_item_release_dt_idx",
            ),
            models.Index(
                fields=["trakt_popularity_rank"],
                name="app_item_trakt_pop_rank_idx",
            ),
        ]
        ordering = ["media_id"]
```

(Replaces the standalone `ordering = ["media_id"]` line with the block above.)

Index names match exactly those added in `0106_add_item_query_indexes.py`. Do not invent new names — Django keys indexes by name.

- [ ] **Step 3: Do not run migrations yet**

We still have `0114` in place, which removes these indexes. Running `makemigrations --check` now would show noise. We'll validate after task 2.

---

## Task 2: Delete migrations 0111 and 0114, fix 0112 dependency

**Files:**
- Delete: `src/app/migrations/0111_alter_podcastepisode_episode_uuid.py`
- Delete: `src/app/migrations/0114_remove_item_app_item_metadata_fetched_idx_and_more.py`
- Modify: `src/app/migrations/0112_alter_item_media_id.py` (line 7 — `dependencies`)

- [ ] **Step 1: Delete `0111`**

```bash
rm src/app/migrations/0111_alter_podcastepisode_episode_uuid.py
```

- [ ] **Step 2: Delete `0114`**

```bash
rm src/app/migrations/0114_remove_item_app_item_metadata_fetched_idx_and_more.py
```

- [ ] **Step 3: Re-point `0112`'s dependency to `0110`**

Edit `src/app/migrations/0112_alter_item_media_id.py`. Change:

```python
    dependencies = [
        ('app', '0111_alter_podcastepisode_episode_uuid'),
    ]
```

to:

```python
    dependencies = [
        ('app', '0110_item_manual_metadata'),
    ]
```

- [ ] **Step 4: Verify the migration chain is clean**

Run: `./venv/bin/python src/manage.py makemigrations --check --dry-run`
Expected: `No changes detected` (or equivalent "no pending migrations" output).

If Django reports pending operations on `Item` (index removes) or `PodcastEpisode.episode_uuid`, stop and inspect — either the `Meta.indexes` block in task 1 is wrong, or the dependency chain isn't correctly re-pointed. Do **not** accept an auto-generated migration that re-introduces the drift.

- [ ] **Step 5: Commit**

```bash
git add src/app/models.py src/app/migrations/
git commit -m "Drop episode_uuid widen/shrink churn and re-declare Item indexes"
```

---

## Task 3: Remove dead `processed_shows` references

**Files:**
- Modify: `src/integrations/imports/pocketcasts.py` (around `:255` and `:1466`)

- [ ] **Step 1: Remove the init**

Find in `src/integrations/imports/pocketcasts.py`:

```python
        # Track shows we've processed to sync episodes from RSS
        self.processed_shows = set()
```

Delete both lines (the comment and the assignment). Leave the surrounding blank lines as they were.

- [ ] **Step 2: Remove the add**

Find (around `:1465`):

```python
            # Track this show for RSS episode sync
            self.processed_shows.add(show)
```

Delete both lines.

- [ ] **Step 3: Verify no remaining references**

Run: `grep -n "processed_shows" src/integrations/imports/pocketcasts.py`
Expected: no output (exit code 1).

- [ ] **Step 4: Commit**

```bash
git add src/integrations/imports/pocketcasts.py
git commit -m "Remove dead processed_shows set left over from RSS sync"
```

---

## Task 4: TDD — 401 loud failure in `_fetch_show_play_states`

**Files:**
- Modify: `src/integrations/tests/imports/test_pocketcasts.py` (add new class at end of file)
- Modify: `src/integrations/imports/pocketcasts.py` (around `:1209-1241`)

- [ ] **Step 1: Add imports to the test file**

Open `src/integrations/tests/imports/test_pocketcasts.py`. Ensure the following imports are present near the top (only add the ones missing):

```python
from unittest.mock import MagicMock, patch

import requests

from integrations.imports.helpers import MediaImportError
```

Existing imports (`datetime`, `get_user_model`, `TestCase`, `app.models.*`, `PocketCastsImporter`, `PocketCastsAccount`) stay.

- [ ] **Step 2: Append the new test class and first test**

Append to the **end of the file**:

```python
class PocketCastsImportFlowTests(TestCase):
    """Tests for the full-history Pocket Casts import flow (fetch + join)."""

    def setUp(self):
        """Set up a user, account, and importer. _ensure_valid_token is patched off."""
        User = get_user_model()
        self.user = User.objects.create_user(username="flowuser", password="pass")  # noqa: S106
        PocketCastsAccount.objects.create(
            user=self.user,
            access_token="token",
        )
        self.importer = PocketCastsImporter(self.user, "new")

    def _http_error(self, status_code):
        """Build a requests.HTTPError with a response mock at the given status."""
        response = MagicMock()
        response.status_code = status_code
        error = requests.HTTPError(response=response)
        return error

    def test_fetch_play_states_401_raises_media_import_error(self):
        """401 from play-states endpoint raises MediaImportError so the run stops loudly."""
        with patch(
            "integrations.imports.pocketcasts.services.api_request",
            side_effect=self._http_error(401),
        ):
            with self.assertRaises(MediaImportError) as cm:
                self.importer._fetch_show_play_states("podcast-uuid")
        self.assertIn("token", str(cm.exception).lower())
```

- [ ] **Step 3: Run the new test — expect FAIL**

Run: `./venv/bin/python src/manage.py test integrations.tests.imports.test_pocketcasts.PocketCastsImportFlowTests.test_fetch_play_states_401_raises_media_import_error --verbosity=2`
Expected: FAIL. The current implementation swallows all exceptions in a bare-except and returns `{}`, so `MediaImportError` is never raised.

- [ ] **Step 4: Update `_fetch_show_play_states` to raise on 401**

In `src/integrations/imports/pocketcasts.py`, find the existing `try/except` block in `_fetch_show_play_states` (around `:1229-1241`):

```python
        try:
            response = services.api_request(
                "POCKETCASTS", "POST", url,
                params={"uuid": podcast_uuid},
                headers=headers,
            )
            episodes = response.get("episodes", [])
            return {ep["uuid"]: ep for ep in episodes if "uuid" in ep}
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to fetch play states for podcast %s: %s", podcast_uuid, e
            )
            return {}
```

Replace the `except` block with two branches — `HTTPError` first (with 401 special case), generic `Exception` second:

```python
        try:
            response = services.api_request(
                "POCKETCASTS", "POST", url,
                params={"uuid": podcast_uuid},
                headers=headers,
            )
            episodes = response.get("episodes", [])
            return {ep["uuid"]: ep for ep in episodes if "uuid" in ep}
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                msg = "Pocket Casts token expired during import — please retry"
                raise MediaImportError(msg) from e
            logger.warning(
                "HTTP error fetching play states for podcast %s: %s", podcast_uuid, e
            )
            return {}
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to fetch play states for podcast %s: %s", podcast_uuid, e
            )
            return {}
```

`requests` is already imported at the top of `pocketcasts.py`. `MediaImportError` is already imported from `integrations.imports.helpers`. No import changes needed.

- [ ] **Step 5: Run the test — expect PASS**

Run: `./venv/bin/python src/manage.py test integrations.tests.imports.test_pocketcasts.PocketCastsImportFlowTests.test_fetch_play_states_401_raises_media_import_error --verbosity=2`
Expected: PASS (`Ran 1 test ... OK`).

- [ ] **Step 6: Commit**

```bash
git add src/integrations/imports/pocketcasts.py src/integrations/tests/imports/test_pocketcasts.py
git commit -m "Raise MediaImportError on 401 during Pocket Casts play-state fetch"
```

---

## Task 5: TDD — switch `_fetch_show_full_metadata` to `services.api_request`

**Files:**
- Modify: `src/integrations/tests/imports/test_pocketcasts.py` (add second test to `PocketCastsImportFlowTests`)
- Modify: `src/integrations/imports/pocketcasts.py` (around `:1243-1289`)

- [ ] **Step 1: Add the pagination test**

Append inside `PocketCastsImportFlowTests` (directly after `test_fetch_play_states_401_raises_media_import_error`):

```python
    def test_fetch_full_metadata_pagination(self):
        """_fetch_show_full_metadata follows has_more_episodes pagination via services.api_request."""
        page_1 = {
            "podcast": {"episodes": [{"uuid": "e1", "title": "Ep 1"}]},
            "has_more_episodes": True,
        }
        page_2 = {
            "podcast": {"episodes": [{"uuid": "e2", "title": "Ep 2"}]},
            "has_more_episodes": False,
        }
        with patch(
            "integrations.imports.pocketcasts.services.api_request",
            side_effect=[page_1, page_2],
        ) as mock_api:
            result = self.importer._fetch_show_full_metadata("podcast-uuid")

        self.assertIn("e1", result)
        self.assertIn("e2", result)
        self.assertEqual(mock_api.call_count, 2)
        # Second call must include the pagination param
        second_call_kwargs = mock_api.call_args_list[1].kwargs
        self.assertEqual(second_call_kwargs.get("params"), {"page": 2})
```

- [ ] **Step 2: Run the test — expect FAIL**

Run: `./venv/bin/python src/manage.py test integrations.tests.imports.test_pocketcasts.PocketCastsImportFlowTests.test_fetch_full_metadata_pagination --verbosity=2`
Expected: FAIL. Current `_fetch_show_full_metadata` uses `requests.get`, not `services.api_request`, so the patched mock is never called and the real HTTP request either fails (offline test env) or doesn't return the mocked shape.

- [ ] **Step 3: Refactor `_fetch_show_full_metadata` to use `services.api_request`**

In `src/integrations/imports/pocketcasts.py`, find the current implementation (around `:1243-1289`):

```python
    def _fetch_show_full_metadata(self, podcast_uuid):
        """..."""
        all_episodes = {}
        page = 1
        max_pages = 10

        while page <= max_pages:
            url = f"{POCKETCASTS_PODCAST_API_BASE_URL}/podcast/full/{podcast_uuid}"
            params = {} if page == 1 else {"page": page}
            try:
                response = requests.get(
                    url, params=params, timeout=30, allow_redirects=True
                )
                response.raise_for_status()
                data = response.json()
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Failed to fetch full metadata for podcast %s (page %d): %s",
                    podcast_uuid, page, e,
                )
                break

            podcast_data = data.get("podcast", {})
            for ep in podcast_data.get("episodes", []):
                if "uuid" in ep:
                    all_episodes[ep["uuid"]] = ep

            if not data.get("has_more_episodes", False):
                break

            page += 1

        if page > max_pages:
            logger.warning(
                "Podcast %s has more than %d pages of episodes; stopped at page %d.",
                podcast_uuid, max_pages, max_pages,
            )

        return all_episodes
```

Replace the `try/except` block's body so the HTTP call goes through `services.api_request`. Keep the pagination and cap logic identical. The new inner block:

```python
        while page <= max_pages:
            url = f"{POCKETCASTS_PODCAST_API_BASE_URL}/podcast/full/{podcast_uuid}"
            params = {} if page == 1 else {"page": page}
            try:
                data = services.api_request(
                    "POCKETCASTS", "GET", url, params=params,
                )
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 401:
                    msg = "Pocket Casts token expired during import — please retry"
                    raise MediaImportError(msg) from e
                logger.warning(
                    "HTTP error fetching full metadata for podcast %s (page %d): %s",
                    podcast_uuid, page, e,
                )
                break
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Failed to fetch full metadata for podcast %s (page %d): %s",
                    podcast_uuid, page, e,
                )
                break

            podcast_data = data.get("podcast", {})
            for ep in podcast_data.get("episodes", []):
                if "uuid" in ep:
                    all_episodes[ep["uuid"]] = ep

            if not data.get("has_more_episodes", False):
                break

            page += 1
```

Notes:
- `services.api_request` returns the parsed JSON dict directly — no `.json()` call needed.
- The 401 branch mirrors task 4; even though the public endpoint shouldn't 401, keeping the shape consistent is cheap.
- Leave the `if page > max_pages: logger.warning(...)` block untouched after the loop.

- [ ] **Step 4: Run the test — expect PASS**

Run: `./venv/bin/python src/manage.py test integrations.tests.imports.test_pocketcasts.PocketCastsImportFlowTests.test_fetch_full_metadata_pagination --verbosity=2`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/integrations/imports/pocketcasts.py src/integrations/tests/imports/test_pocketcasts.py
git commit -m "Route Pocket Casts full-metadata fetch through services.api_request"
```

---

## Task 6: Add happy-path and missing-metadata skip tests

These tests validate the existing post-PR behavior and don't require code changes.

**Files:**
- Modify: `src/integrations/tests/imports/test_pocketcasts.py` (append 2 more tests to `PocketCastsImportFlowTests`)

- [ ] **Step 1: Append happy-path test**

Append inside `PocketCastsImportFlowTests`:

```python
    def test_import_happy_path(self):
        """End-to-end: one podcast, two episodes (played + in-progress) become Podcast rows."""
        podcast_list = {
            "podcasts": [{
                "uuid": "show-1",
                "title": "Test Show",
                "author": "Test Author",
                "description": "",
                "url": "",
            }],
        }
        play_states = {
            "uuid-played": {
                "uuid": "uuid-played",
                "playingStatus": 3,
                "playedUpTo": 1800,
                "duration": 1800,
            },
            "uuid-inprogress": {
                "uuid": "uuid-inprogress",
                "playingStatus": 2,
                "playedUpTo": 600,
                "duration": 1800,
            },
        }
        metadata_page = {
            "podcast": {
                "episodes": [
                    {
                        "uuid": "uuid-played",
                        "title": "Played Ep",
                        "published": "2026-01-01T00:00:00Z",
                        "duration": 1800,
                        "url": "https://example.com/played.mp3",
                    },
                    {
                        "uuid": "uuid-inprogress",
                        "title": "In-Progress Ep",
                        "published": "2026-01-02T00:00:00Z",
                        "duration": 1800,
                        "url": "https://example.com/inprogress.mp3",
                    },
                ],
            },
            "has_more_episodes": False,
        }

        with patch.object(PocketCastsImporter, "_ensure_valid_token"), \
             patch.object(PocketCastsImporter, "_get_access_token", return_value="fake"), \
             patch(
                 "integrations.imports.pocketcasts.pocketcasts_api.get_podcast_list",
                 return_value=podcast_list,
             ), \
             patch.object(
                 PocketCastsImporter, "_fetch_show_play_states", return_value=play_states,
             ), \
             patch.object(
                 PocketCastsImporter, "_fetch_show_full_metadata",
                 return_value={ep["uuid"]: ep for ep in metadata_page["podcast"]["episodes"]},
             ):
            importer = PocketCastsImporter(self.user, "new")
            importer.import_data()

        podcasts = Podcast.objects.filter(user=self.user).order_by("item__media_id")
        self.assertEqual(podcasts.count(), 2)
        statuses = {p.item.media_id: p.status for p in podcasts}
        self.assertEqual(statuses["uuid-played"], Status.COMPLETED.value)
        self.assertEqual(statuses["uuid-inprogress"], Status.IN_PROGRESS.value)
```

- [ ] **Step 2: Run the happy-path test — expect PASS**

Run: `./venv/bin/python src/manage.py test integrations.tests.imports.test_pocketcasts.PocketCastsImportFlowTests.test_import_happy_path --verbosity=2`
Expected: PASS.

If it fails, read the error carefully. The most likely issue is a missing field in the mocked `podcast_list` or `metadata_page` that `_build_episode_data` / `_process_episode` requires. Extend the fixtures with whatever the code reads; do **not** modify production code to make the test pass. If uncertain, re-read `_build_episode_data` (around `:1291`) and `_process_episode` (search for `def _process_episode`) to see exactly which keys are accessed.

- [ ] **Step 3: Append missing-metadata skip test**

Append inside `PocketCastsImportFlowTests`:

```python
    def test_import_skips_episode_with_missing_metadata(self):
        """When play state exists but metadata is missing, that episode is skipped."""
        podcast_list = {
            "podcasts": [{
                "uuid": "show-1",
                "title": "Test Show",
                "author": "Test Author",
                "description": "",
                "url": "",
            }],
        }
        play_states = {
            "uuid-a": {"uuid": "uuid-a", "playingStatus": 3, "playedUpTo": 1800, "duration": 1800},
            "uuid-b": {"uuid": "uuid-b", "playingStatus": 3, "playedUpTo": 1800, "duration": 1800},
        }
        # Only uuid-a present in metadata
        metadata_only_a = {
            "uuid-a": {
                "uuid": "uuid-a",
                "title": "Ep A",
                "published": "2026-01-01T00:00:00Z",
                "duration": 1800,
                "url": "https://example.com/a.mp3",
            },
        }

        with patch.object(PocketCastsImporter, "_ensure_valid_token"), \
             patch.object(PocketCastsImporter, "_get_access_token", return_value="fake"), \
             patch(
                 "integrations.imports.pocketcasts.pocketcasts_api.get_podcast_list",
                 return_value=podcast_list,
             ), \
             patch.object(
                 PocketCastsImporter, "_fetch_show_play_states", return_value=play_states,
             ), \
             patch.object(
                 PocketCastsImporter, "_fetch_show_full_metadata", return_value=metadata_only_a,
             ):
            importer = PocketCastsImporter(self.user, "new")
            importer.import_data()

        media_ids = set(
            Podcast.objects.filter(user=self.user).values_list("item__media_id", flat=True)
        )
        self.assertEqual(media_ids, {"uuid-a"})
```

- [ ] **Step 4: Run the missing-metadata test — expect PASS**

Run: `./venv/bin/python src/manage.py test integrations.tests.imports.test_pocketcasts.PocketCastsImportFlowTests.test_import_skips_episode_with_missing_metadata --verbosity=2`
Expected: PASS.

- [ ] **Step 5: Run the whole `PocketCastsImportFlowTests` class**

Run: `./venv/bin/python src/manage.py test integrations.tests.imports.test_pocketcasts.PocketCastsImportFlowTests --verbosity=2`
Expected: all 4 tests pass (`Ran 4 tests ... OK`).

- [ ] **Step 6: Commit**

```bash
git add src/integrations/tests/imports/test_pocketcasts.py
git commit -m "Add happy-path and missing-metadata tests for Pocket Casts import flow"
```

---

## Task 7: Fix `podcast_mark_all_played` docstring

**Files:**
- Modify: `src/app/views.py` (around `:12276`)

- [ ] **Step 1: Replace the docstring**

In `src/app/views.py`, find the function (search for `def podcast_mark_all_played(`). Replace the existing one-line docstring:

```python
    """Mark all unplayed episodes for a podcast show as completed on their release date."""
```

with:

```python
    """Mark all episodes of this podcast currently in the library as completed on their release date.

    Episodes not yet imported from Pocket Casts are not included — run a Pocket Casts
    import first to fetch the full episode list.
    """
```

(Convert to a multi-line docstring. Preserves pydocstyle pep257 — first line is a short summary, blank line, then description.)

- [ ] **Step 2: Commit**

```bash
git add src/app/views.py
git commit -m "Clarify podcast_mark_all_played docstring after RSS backfill removal"
```

---

## Task 8: Update PR description

**Files:** none (remote API change only).

- [ ] **Step 1: Fetch current PR body**

Run: `gh pr view 1 --json body -q .body > /tmp/pr1_body.md`

- [ ] **Step 2: Append behavior-change note under `## Notes`**

Open `/tmp/pr1_body.md`. Find the `## Notes` section (near the end). Add a new bullet under it:

```
- Removed `refresh_podcast_episodes` task and its calendar-reload invocation. New episodes for subscribed podcasts now appear only after the next Pocket Casts import (recurring task runs every 2 hours), not via RSS refresh on calendar reload.
```

(Insert this bullet **before** the existing `Migration Sync Gate steps are not applicable` bullet, so behavior changes are listed together.)

- [ ] **Step 3: Push the updated body**

Run: `gh pr edit 1 --body "$(cat /tmp/pr1_body.md)"`
Expected: `https://github.com/WybeBosch/Yamtrack/pull/1` printed.

- [ ] **Step 4: Verify**

Run: `gh pr view 1 --json body -q .body | grep -A1 "refresh_podcast_episodes"`
Expected: the new bullet is shown.

No git commit — this is a remote edit.

---

## Task 9: Completion gate

**Files:** whatever `ruff check --fix` and `ruff format` touch.

- [ ] **Step 1: Run `ruff check --fix` on the changed Python files**

Run: `./venv/bin/ruff check --fix src/`
Expected: `All checks passed!` or a summary of auto-fixes. If ruff surfaces a new error it can't auto-fix (most likely in our new test class or the edited `pocketcasts.py`), read the rule name, resolve the issue, and re-run.

- [ ] **Step 2: Run `ruff format`**

Run: `./venv/bin/ruff format src/`
Expected: `N files reformatted` or `N files left unchanged`. Ruff format is authoritative — accept its changes.

- [ ] **Step 3: Re-verify migration hygiene**

Run: `./venv/bin/python src/manage.py makemigrations --check --dry-run`
Expected: `No changes detected`. If this now fails after ruff format, something else drifted — investigate before continuing.

- [ ] **Step 4: Run the Pocket Casts test subset**

Run: `./venv/bin/python src/manage.py test integrations.tests.imports.test_pocketcasts --verbosity=2`
Expected: `Ran 13 tests ... OK` (9 existing inference/distribution tests + 4 new flow tests).

- [ ] **Step 5: If ruff made any changes, commit them**

Run: `git status --short`

If any files show as modified:

```bash
git add -u
git commit -m "Apply ruff auto-fixes and formatting"
```

If nothing changed, skip this step.

- [ ] **Step 6: Final verification — tests still pass after any lint changes**

Run: `./venv/bin/python src/manage.py test integrations.tests.imports.test_pocketcasts --verbosity=2`
Expected: `Ran 13 tests ... OK`.

Plan complete. Report to the user: "PR #1 review cleanup done. Ready to push."
