# Pocket Casts Full-History Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken history-driven import with a full per-podcast iteration that imports played state for all 41 subscribed podcasts.

**Architecture:** Three new helper methods (`_fetch_show_play_states`, `_fetch_show_full_metadata`, `_build_episode_data`) join two complementary APIs per podcast, producing the exact dict shape `_process_episode()` already expects. `import_data()` loop is replaced; everything downstream stays untouched.

**Tech Stack:** Python, Django, `requests` (already imported), `services.api_request` (already used in file), Pocket Casts REST APIs.

---

## File map

| File | Change |
|------|--------|
| `~/yamtrack-configs/docker-compose.yaml` | Add bind-mount for the extracted pocketcasts.py |
| `~/yamtrack-configs/pocketcasts_import.py` | **New host file** — copy of container file, all code changes land here |

The Python source lives inside the Docker image. We copy it to a host path, bind-mount it back into the container, then edit the host copy. The container sees changes immediately on restart without rebuilding the image.

---

## Task 1: Extract the source file and wire up the volume mount

**Files:**
- Create: `~/yamtrack-configs/pocketcasts_import.py` (via docker cp)
- Modify: `~/yamtrack-configs/docker-compose.yaml`

- [ ] **Step 1: Copy the file out of the running container**

```bash
docker cp yamtrack:/yamtrack/integrations/imports/pocketcasts.py \
  ~/yamtrack-configs/pocketcasts_import.py
```

Expected: no output, file appears at host path.

- [ ] **Step 2: Add volume mount to docker-compose.yaml**

In `~/yamtrack-configs/docker-compose.yaml`, find the `volumes:` block under the `yamtrack:` service:

```yaml
    volumes:
      - ~/yamtrack-data/db:/yamtrack/db
      - ~/yamtrack-configs/supervisord.conf:/etc/supervisord.conf
```

Replace with:

```yaml
    volumes:
      - ~/yamtrack-data/db:/yamtrack/db
      - ~/yamtrack-configs/supervisord.conf:/etc/supervisord.conf
      - ~/yamtrack-configs/pocketcasts_import.py:/yamtrack/integrations/imports/pocketcasts.py
```

- [ ] **Step 3: Restart the container**

```bash
cd ~/yamtrack-configs && docker compose up -d --force-recreate yamtrack
```

- [ ] **Step 4: Verify the mount is live**

```bash
docker exec yamtrack head -5 /yamtrack/integrations/imports/pocketcasts.py
```

Expected: the first 5 lines of the Python file (imports starting with `import hashlib`). If you see the original image's file, the mount didn't take — double-check the volume path.

---

## Task 2: Add the new API base URL constant and comment `_fetch_history`

**Files:**
- Modify: `~/yamtrack-configs/pocketcasts_import.py`

- [ ] **Step 1: Add the `POCKETCASTS_PODCAST_API_BASE_URL` constant**

Find (around line 37):

```python
POCKETCASTS_API_BASE_URL = "https://api.pocketcasts.com"
```

Replace with:

```python
POCKETCASTS_API_BASE_URL = "https://api.pocketcasts.com"
POCKETCASTS_PODCAST_API_BASE_URL = "https://podcast-api.pocketcasts.com"
```

- [ ] **Step 2: Add deprecation comment to `_fetch_history`**

Find (around line 1195):

```python
    def _fetch_history(self):
        """Fetch history from API (returns last 100 episodes only)."""
```

Replace with:

```python
    def _fetch_history(self):
        """Fetch history from API (returns last 100 episodes only).

        NOTE: This method is no longer called from import_data(). The main import
        now uses _fetch_show_play_states() + _fetch_show_full_metadata() per podcast,
        which correctly handles completed episodes (history only returns in-progress
        episodes — they vanish from history the moment you finish them).
        Left in place in case other Yamtrack code paths reference it.
        """
```

- [ ] **Step 3: Verify the file parses cleanly**

```bash
docker exec yamtrack python3 -c "import ast, sys; ast.parse(open('/yamtrack/integrations/imports/pocketcasts.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd ~/yamtrack-configs
git add pocketcasts_import.py docker-compose.yaml
git commit -m "feat: wire up pocketcasts_import.py as bind-mount, add podcast-api constant"
```

---

## Task 3: Add `_fetch_show_play_states` method

**Files:**
- Modify: `~/yamtrack-configs/pocketcasts_import.py`

- [ ] **Step 1: Insert the method between `_fetch_history` and `_process_episode`**

Find this exact text (the last two lines of `_fetch_history` plus the `_process_episode` definition line):

```python
            msg = f"Pocket Casts API error: {e.response.status_code}"
            raise MediaImportError(msg) from e

    def _process_episode(self, episode_data, defer_completion_date=False):
```

Replace with:

```python
            msg = f"Pocket Casts API error: {e.response.status_code}"
            raise MediaImportError(msg) from e

    def _fetch_show_play_states(self, podcast_uuid):
        """Fetch per-episode play states for one podcast.

        Calls POST /user/podcast/episodes with {"uuid": podcast_uuid}.
        Returns a dict keyed by episode UUID:
            {uuid: {playingStatus, playedUpTo, isDeleted, starred, duration, bookmarks, ...}}
        Returns {} on any failure so the caller can skip the show gracefully.
        """
        url = f"{POCKETCASTS_API_BASE_URL}/user/podcast/episodes"
        access_token = self._get_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "X-App-Language": "en",
            "X-User-Region": "global",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
        }
        try:
            response = services.api_request(
                "POCKETCASTS", "POST", url,
                params={"uuid": podcast_uuid},
                headers=headers,
            )
            episodes = response.get("episodes", [])
            return {ep["uuid"]: ep for ep in episodes if "uuid" in ep}
        except Exception as e:
            logger.warning(
                "Failed to fetch play states for podcast %s: %s", podcast_uuid, e
            )
            return {}

    def _process_episode(self, episode_data, defer_completion_date=False):
```

- [ ] **Step 2: Verify syntax**

```bash
docker exec yamtrack python3 -c "import ast, sys; ast.parse(open('/yamtrack/integrations/imports/pocketcasts.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd ~/yamtrack-configs
git add pocketcasts_import.py
git commit -m "feat: add _fetch_show_play_states — POST /user/podcast/episodes per show"
```

---

## Task 4: Add `_fetch_show_full_metadata` method

**Files:**
- Modify: `~/yamtrack-configs/pocketcasts_import.py`

- [ ] **Step 1: Insert the method after `_fetch_show_play_states`**

Find this exact text (the last line of `_fetch_show_play_states` plus the `_process_episode` definition line):

```python
            return {}

    def _process_episode(self, episode_data, defer_completion_date=False):
```

Replace with:

```python
            return {}

    def _fetch_show_full_metadata(self, podcast_uuid):
        """Fetch full episode metadata for one podcast from the public (unauthenticated) API.

        GET https://podcast-api.pocketcasts.com/podcast/full/<podcast_uuid>
        Follows the 302 redirect to the CDN JSON automatically.
        Returns a dict keyed by episode UUID:
            {uuid: {title, slug, published, url, file_type, duration, type, season, number}}
        Handles has_more_episodes pagination up to 10 pages (safety cap).
        Returns {} on any failure so the caller can skip the show gracefully.
        """
        all_episodes = {}
        page = 1
        max_pages = 10

        while page <= max_pages:
            url = f"{POCKETCASTS_PODCAST_API_BASE_URL}/podcast/full/{podcast_uuid}"
            params = {} if page == 1 else {"page": page}
            try:
                response = requests.get(url, params=params, timeout=30, allow_redirects=True)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
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

    def _process_episode(self, episode_data, defer_completion_date=False):
```

- [ ] **Step 2: Verify syntax**

```bash
docker exec yamtrack python3 -c "import ast, sys; ast.parse(open('/yamtrack/integrations/imports/pocketcasts.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd ~/yamtrack-configs
git add pocketcasts_import.py
git commit -m "feat: add _fetch_show_full_metadata — GET podcast-api.pocketcasts.com/podcast/full"
```

---

## Task 5: Add `_build_episode_data` method

**Files:**
- Modify: `~/yamtrack-configs/pocketcasts_import.py`

- [ ] **Step 1: Insert the method after `_fetch_show_full_metadata`**

Find this exact text (last lines of `_fetch_show_full_metadata` plus `_process_episode` definition):

```python
        return all_episodes

    def _process_episode(self, episode_data, defer_completion_date=False):
```

Replace with:

```python
        return all_episodes

    def _build_episode_data(self, play_state, metadata_ep, podcast_uuid, podcast_meta):
        """Merge play-state and episode metadata into the shape _process_episode() expects.

        Args:
            play_state:   dict from /user/podcast/episodes (uuid, playingStatus, playedUpTo, ...)
            metadata_ep:  dict from podcast-api.pocketcasts.com/podcast/full (uuid, title, published, ...)
            podcast_uuid: str — the podcast's Pocket Casts UUID
            podcast_meta: dict from /user/podcast/list (title, author, slug, ...)
        """
        return {
            "uuid":          play_state["uuid"],
            "podcastUuid":   podcast_uuid,
            "podcastTitle":  podcast_meta.get("title", ""),
            "author":        podcast_meta.get("author", ""),
            "podcastSlug":   podcast_meta.get("slug", ""),
            "title":         metadata_ep.get("title", "Unknown Episode"),
            "slug":          metadata_ep.get("slug", ""),
            "published":     metadata_ep.get("published", ""),
            "url":           metadata_ep.get("url", ""),
            "fileType":      metadata_ep.get("file_type", ""),
            "duration":      metadata_ep.get("duration") or play_state.get("duration", 0),
            "episodeType":   metadata_ep.get("type", "full"),
            "episodeSeason": metadata_ep.get("season"),
            "episodeNumber": metadata_ep.get("number"),
            "playingStatus": play_state.get("playingStatus", 0),
            "playedUpTo":    play_state.get("playedUpTo", 0),
            "starred":       play_state.get("starred", False),
            "isDeleted":     play_state.get("isDeleted", False),
            "bookmarks":     play_state.get("bookmarks", []),
        }

    def _process_episode(self, episode_data, defer_completion_date=False):
```

- [ ] **Step 2: Verify syntax**

```bash
docker exec yamtrack python3 -c "import ast, sys; ast.parse(open('/yamtrack/integrations/imports/pocketcasts.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
cd ~/yamtrack-configs
git add pocketcasts_import.py
git commit -m "feat: add _build_episode_data — pure merge of play-state + metadata"
```

---

## Task 6: Rewrite the `import_data()` first-pass loop

**Files:**
- Modify: `~/yamtrack-configs/pocketcasts_import.py`

Two edits: (1) remove history fetch + early exit, (2) replace the episode loop with the per-podcast nested loop.

- [ ] **Step 1: Remove the history fetch and early-exit block**

Find (around line 277):

```python
        # Fetch history (last 100 episodes only, no pagination)
        episodes = self._fetch_history()
        episodes = self._dedupe_history(episodes)

        sync_window_end = timezone.now()
        sync_window_start = self.account.last_sync_at or (sync_window_end - timedelta(hours=2))
        self._sync_window_start = sync_window_start
        self._sync_window_end = sync_window_end
        self._existing_history_items = self._get_history_items_in_range(sync_window_start, sync_window_end)

        if not episodes:
            logger.info("No episodes found for Pocket Casts user %s", self.user.username)
            return {}, ""

        # Check if this is first import
        is_first_import = not Podcast.objects.filter(user=self.user).exists()
```

Replace with:

```python
        sync_window_end = timezone.now()
        sync_window_start = self.account.last_sync_at or (sync_window_end - timedelta(hours=2))
        self._sync_window_start = sync_window_start
        self._sync_window_end = sync_window_end
        self._existing_history_items = self._get_history_items_in_range(sync_window_start, sync_window_end)

        # Check if this is first import
        is_first_import = not Podcast.objects.filter(user=self.user).exists()
```

- [ ] **Step 2: Replace the episode loop with the per-podcast nested loop**

Find (around line 298 after the previous edit):

```python
        # First pass: process episodes and collect new completed ones
        for episode_data in episodes:
            episode_uuid = episode_data.get("uuid")
            # Check if this episode is new (not in existing_podcasts)
            is_new = (episode_uuid, Sources.POCKETCASTS.value) not in self.existing_podcasts

            # Process the episode (but don't set completion_date yet for new ones)
            self._process_episode(episode_data, defer_completion_date=not is_first_import and is_new)

            # If this is a new completed episode (not first import), collect it for inference
            if not is_first_import and is_new:
                playing_status = episode_data.get("playingStatus", 0)
                duration = episode_data.get("duration", 0)
                played_up_to = episode_data.get("playedUpTo", 0)
                published = None
                if episode_data.get("published"):
                    try:
                        published = datetime.fromisoformat(episode_data["published"].replace("Z", "+00:00"))
                        if published and timezone.is_naive(published):
                            published = timezone.make_aware(published)
                    except (ValueError, AttributeError):
                        pass

                # Check if completed using same logic as _calculate_progress_delta
                # (status 3 with significant progress, or played up to duration with 5 second tolerance)
                epsilon = 5
                # Only mark as completed if there's significant progress to avoid false positives
                significant_progress = duration > 0 and (played_up_to > 60 or played_up_to > duration * 0.1)
                is_completed = (
                    (playing_status == 3 and significant_progress) or
                    (duration > 0 and played_up_to >= duration - epsilon)
                )

                if is_completed and published:
                    new_completed_podcasts.append((episode_data, duration, published))
```

Replace with:

```python
        # First pass: iterate every subscribed podcast and process all episodes with known
        # play state. For each show we call two APIs:
        #   - POST /user/podcast/episodes      → play state per episode UUID (auth required)
        #   - GET  podcast-api.pocketcasts.com/podcast/full/<uuid>  → title/date/slug (no auth)
        # Inner-joining on episode UUID gives the full shape _process_episode() expects.
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
                    # Play record exists but episode no longer in public feed (deleted/unlisted).
                    logger.debug("Episode %s has play state but no metadata; skipping", ep_uuid)
                    continue

                episode_data = self._build_episode_data(
                    play_state, metadata_ep, podcast_uuid, podcast_meta
                )
                episode_uuid = ep_uuid
                # Check if this episode is new (not in existing_podcasts)
                is_new = (episode_uuid, Sources.POCKETCASTS.value) not in self.existing_podcasts

                # Process the episode (but don't set completion_date yet for new ones)
                self._process_episode(episode_data, defer_completion_date=not is_first_import and is_new)

                # If this is a new completed episode (not first import), collect it for inference
                if not is_first_import and is_new:
                    playing_status = episode_data.get("playingStatus", 0)
                    duration = episode_data.get("duration", 0)
                    played_up_to = episode_data.get("playedUpTo", 0)
                    published = None
                    if episode_data.get("published"):
                        try:
                            published = datetime.fromisoformat(episode_data["published"].replace("Z", "+00:00"))
                            if published and timezone.is_naive(published):
                                published = timezone.make_aware(published)
                        except (ValueError, AttributeError):
                            pass

                    # Check if completed using same logic as _calculate_progress_delta
                    # (status 3 with significant progress, or played up to duration with 5 second tolerance)
                    epsilon = 5
                    # Only mark as completed if there's significant progress to avoid false positives
                    significant_progress = duration > 0 and (played_up_to > 60 or played_up_to > duration * 0.1)
                    is_completed = (
                        (playing_status == 3 and significant_progress) or
                        (duration > 0 and played_up_to >= duration - epsilon)
                    )

                    if is_completed and published:
                        new_completed_podcasts.append((episode_data, duration, published))
```

- [ ] **Step 3: Verify syntax**

```bash
docker exec yamtrack python3 -c "import ast, sys; ast.parse(open('/yamtrack/integrations/imports/pocketcasts.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
cd ~/yamtrack-configs
git add pocketcasts_import.py
git commit -m "fix: replace history-driven loop with full per-podcast iteration in import_data()"
```

---

## Task 7: Restart container and verify the import

- [ ] **Step 1: Restart the container to load the edited file**

```bash
cd ~/yamtrack-configs && docker compose restart yamtrack
```

- [ ] **Step 2: Tail logs and trigger the import**

In one terminal, stream logs:

```bash
docker logs -f yamtrack 2>&1 | grep -i "pocket\|import\|episode\|podcast\|error\|warn"
```

In another terminal (or via the Yamtrack web UI), trigger the Pocket Casts import manually. If there is no UI button, you can invoke it via Django shell:

```bash
docker exec -it yamtrack python3 /yamtrack/manage.py shell -c "
from django.contrib.auth import get_user_model
from integrations.imports.pocketcasts import PocketCastsImporter
User = get_user_model()
user = User.objects.first()
importer = PocketCastsImporter(user, mode='full')
result = importer.import_data()
print(result)
"
```

- [ ] **Step 3: Check expected outcomes**

After the import completes, verify in the Yamtrack web UI:

1. **All 41 podcasts appear** (previously only 1 appeared on first import).
2. **"What the Shell?"** shows ~49 completed episodes + 1 unplayed (the most recent).
3. **"Zo, Opgelost"** shows the correct mix of completed / in-progress / unplayed episodes.
4. **No `value too long` errors** in the logs (DB columns were already widened in a prior session).
5. **No duplicate episodes** created (the title+date dedup in `_process_episode()` handles this).

- [ ] **Step 4: Check the log output for per-podcast progress**

```bash
docker logs yamtrack 2>&1 | grep -E "podcast|episode|import" | tail -50
```

You should see log lines for each of the 41 podcasts being processed, not just 1.
