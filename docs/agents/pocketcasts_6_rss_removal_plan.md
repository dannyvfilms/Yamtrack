# Remove RSS Sync and History Dead Code Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate duplicate podcast episodes by removing the `_sync_episodes_from_rss` call and deleting three dead methods (`_sync_episodes_from_rss`, `_fetch_history`, `_dedupe_history`).

**Architecture:** Single file change to `src/integrations/imports/pocketcasts.py`. Remove the 8-line RSS sync loop from `import_data()` and delete the three unused method bodies. No model changes, no migrations, no new files.

**Tech Stack:** Python 3.12, Django 5.2. Linting via `ruff` (run from repo root with `venv/bin/ruff check src`).

---

### Task 1: Remove RSS sync call loop and delete dead methods

**Files:**
- Modify: `src/integrations/imports/pocketcasts.py:444-451` (remove call loop)
- Modify: `src/integrations/imports/pocketcasts.py:1218-1293` (delete `_fetch_history`)
- Modify: `src/integrations/imports/pocketcasts.py:2136-2274` (delete `_sync_episodes_from_rss`)
- Modify: `src/integrations/imports/pocketcasts.py:2377-2409` (delete `_dedupe_history`)

- [ ] **Step 1: Remove the RSS sync call loop from `import_data()`**

In `src/integrations/imports/pocketcasts.py`, delete lines 444–451 (the comment and the for-loop):

```python
        # Sync episodes from RSS feeds for processed shows
        for show in self.processed_shows:
            if show.rss_feed_url:
                try:
                    self._sync_episodes_from_rss(show, show.rss_feed_url)
                except Exception as e:
                    logger.warning("Failed to sync episodes from RSS for show %s: %s", show.title, e)
                    self.warnings.append(f"Failed to sync episodes for {show.title}: {e!s}")
```

After deletion, line 453 (`# Update last sync time`) should follow directly after line 442 (`self._record_history(...)`).

- [ ] **Step 2: Delete the `_fetch_history` method (lines 1218–1293)**

Delete from `    def _fetch_history(self):` through the end of the method body (the blank line before `    def _fetch_show_play_states`). The method runs from line 1218 to line 1293 inclusive (blank line 1293 separating it from `_fetch_show_play_states` at 1294).

- [ ] **Step 3: Delete the `_sync_episodes_from_rss` method (lines 2136–2274)**

Delete from `    def _sync_episodes_from_rss(self, show, rss_feed_url):` through the end of its body (the blank line before `    def _cleanup_duplicate_episodes` at 2275).

- [ ] **Step 4: Delete the `_dedupe_history` method (lines 2377–2409)**

Delete from `    def _dedupe_history(self, episodes):` through the end of its body (the blank line before `    def _discover_rss_feed_url` at 2410). Note: there is trailing whitespace on line 2409 — delete that line too.

- [ ] **Step 5: Run ruff to verify no lint errors**

```bash
cd ~/yamtrack-configs/dev-git-folder/Yamtrack
venv/bin/ruff check src/integrations/imports/pocketcasts.py
```

Expected: no output (clean).

If `F401` unused import errors appear for any imports that were only used by the deleted methods (e.g. `podcast_rss`), remove those import lines too.

- [ ] **Step 6: Commit**

```bash
cd ~/yamtrack-configs/dev-git-folder/Yamtrack
git add src/integrations/imports/pocketcasts.py docs/superpowers/specs/2026-04-20-remove-rss-sync-dead-code.md docs/superpowers/plans/2026-04-20-remove-rss-sync-dead-code.md
git commit -m "Remove RSS sync call and dead history/RSS methods

- Drop _sync_episodes_from_rss call loop from import_data(): CDN
  endpoint already returns full episode list, RSS was creating
  duplicate episodes with mismatched titles (#N - Title vs Title)
- Delete _sync_episodes_from_rss, _fetch_history, _dedupe_history:
  all three had zero call sites after the import rework"
```
