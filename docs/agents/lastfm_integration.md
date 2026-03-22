# Last.fm Integration

This document provides a comprehensive reference for the Last.fm scrobbling integration in Yamtrack, including the polling mechanism, scrobble processing, metadata resolution, and UI patterns.

## Overview

Last.fm integration allows users to import their listening history from Last.fm into Yamtrack. Unlike Plex webhooks (which push data), Last.fm uses a **polling-based approach** because Last.fm does not provide outgoing webhooks. The system periodically fetches new scrobbles from Last.fm's API and processes them through the same music playback pipeline used by other sources.

**Key Characteristics:**
- **Pull-based**: System polls Last.fm API for new scrobbles (no webhooks available)
- **Public scrobbles only**: Last.fm username must have public scrobbling enabled
- **App-level API key**: Uses a single API key configured in settings (no per-user OAuth)
- **Global periodic task**: One Celery task polls all connected users (not per-user schedules)
- **Incremental imports**: Only fetches scrobbles since last successful poll timestamp

## Architecture

```
┌─────────────┐
│   Last.fm   │
│     API     │
└──────┬──────┘
       │
       │ user.getRecentTracks
       │
┌──────▼─────────────────────────────────────┐
│  Celery Periodic Task                      │
│  (poll_all_lastfm_scrobbles)               │
│  - Runs every 15 minutes (configurable)   │
│  - Processes all connected users           │
└──────┬─────────────────────────────────────┘
       │
       │ For each user:
       │ 1. Fetch tracks since last_fetch_timestamp_uts
       │ 2. Process through LastFMScrobbleProcessor
       │ 3. Update last_fetch_timestamp_uts
       │
┌──────▼─────────────────────────────────────┐
│  LastFMScrobbleProcessor                   │
│  - Filters "now playing" tracks            │
│  - Extracts MBIDs (artist, track, album)   │
│  - Converts timestamps                      │
│  - Deduplicates exact matches              │
└──────┬─────────────────────────────────────┘
       │
       │ MusicPlaybackEvent
       │
┌──────▼─────────────────────────────────────┐
│  music_scrobble.record_music_playback()    │
│  - Resolves metadata (MusicBrainz)         │
│  - Creates/updates Artist/Album/Track      │
│  - Creates/updates Music (per-user)        │
│  - Ensures ArtistTracker/AlbumTracker      │
└────────────────────────────────────────────┘
```

## Data Model

### LastFMAccount (`src/integrations/models.py`)

Stores per-user Last.fm connection details and sync state.

```python
class LastFMAccount(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="lastfm_account",
    )
    lastfm_username = models.CharField(
        max_length=255,
        help_text="Last.fm username (public)",
    )
    last_fetch_timestamp_uts = models.IntegerField(
        null=True,
        blank=True,
        help_text="Unix timestamp (seconds) of last successful poll",
    )
    last_sync_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of last successful sync completion",
    )
    connection_broken = models.BooleanField(
        default=False,
        help_text="True if connection is broken (invalid username or persistent errors)",
    )
    failure_count = models.IntegerField(
        default=0,
        help_text="Number of consecutive failures",
    )
    last_error_code = models.CharField(
        max_length=10,
        blank=True,
        help_text="Last.fm API error code (e.g., '29' for rate limit)",
    )
    last_error_message = models.TextField(
        blank=True,
        help_text="Human-readable error message",
    )
    last_failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_connected(self):
        """Return True when we have a valid connection."""
        return bool(self.lastfm_username) and not self.connection_broken
```

**Key Fields:**
- `last_fetch_timestamp_uts`: Unix timestamp (seconds) of the most recent scrobble successfully imported. Used as the `from` parameter in subsequent polls to fetch only new scrobbles.
- `last_sync_at`: Django datetime of when the last sync completed (for UI display).
- `connection_broken`: Set to `True` when username is invalid or persistent errors occur. Prevents further polling attempts.
- `failure_count`: Tracks consecutive failures for monitoring/debugging.
- `history_import_status`: Separate state for the bounded full-history import (`idle`, `queued`, `running`, `failed`, `completed`).
- `history_import_cutoff_uts`: Fixed upper timestamp bound for the current history backfill.
- `history_import_next_page` / `history_import_total_pages`: Resume cursor for chunked history imports.
- `history_import_started_at` / `history_import_completed_at` / `history_import_last_error_message`: User-facing progress/error metadata for the import page.

## API Client (`src/integrations/lastfm_api.py`)

### Configuration

Requires a single app-level API key (no per-user OAuth):

```python
# settings.py
LASTFM_API_KEY = config("LASTFM_API_KEY", default="")
LASTFM_POLL_INTERVAL_MINUTES = config("LASTFM_POLL_INTERVAL_MINUTES", default=15, cast=int)
LASTFM_HISTORY_PAGES_PER_TASK = config("LASTFM_HISTORY_PAGES_PER_TASK", default=5, cast=int)
```

**API Key Setup:**
1. Register an application at https://www.last.fm/api/account/create
2. Copy the API key to `LASTFM_API_KEY` in your `.env` file
3. No API secret is needed (read-only access)

### Rate Limiting

Last.fm API has a **5 requests/second** limit. The client implements:
- Exponential backoff with jitter for rate limit errors (error code 29)
- Automatic retries (max 3 attempts) with increasing delays
- Request logging for debugging

**Current Implementation:**
- Rate limiting is **reactive only** (retries on error code 29)
- No global request throttle across users/pages
- Batching and jitter help distribute load, but at scale (many users + pagination) can still exceed 5 req/s
- Multiple workers or overlapping task runs can cause rate limit spikes
- **Future Enhancement**: Consider adding a shared Redis-based rate limiter to coordinate requests across all users and pages

### Core Functions

#### `get_recent_tracks()`

Fetches a single page of recent tracks for a user.

```python
def get_recent_tracks(
    username: str,
    from_timestamp_uts: int | None = None,
    limit: int = 200,
    page: int = 1,
    extended: int = 1,
) -> dict[str, Any]:
```

**Parameters:**
- `username`: Last.fm username (must have public scrobbling)
- `from_timestamp_uts`: Unix timestamp (seconds) - only fetch tracks after this time
- `limit`: Max tracks per page (capped at 200)
- `page`: Page number (1-indexed)
- `extended`: Include extended metadata (1 = yes, includes MBIDs)

**Returns:**
- Dict with `recenttracks` key containing track list and `@attr` pagination metadata

#### `get_all_recent_tracks()`

Fetches all recent tracks with automatic pagination.

```python
def get_all_recent_tracks(
    username: str,
    from_timestamp_uts: int | None = None,
    extended: int = 1,
) -> list[dict[str, Any]]:
```

**Behavior:**
- Automatically paginates through all pages
- Adds 0.2s delay between pages to be respectful
- Returns partial results if an error occurs mid-pagination (except for rate limit errors, which are re-raised)
- Logs total pages and track count

**Important: Cursor Safety on Partial Pagination**
- Pagination helpers now report whether a fetch completed cleanly or was interrupted mid-window.
- Incremental sync only advances `last_fetch_timestamp_uts` after a clean full pagination run.
- If pagination is interrupted, imported scrobbles are kept, but the forward cursor stays put so the next run can safely retry the same range.

### Error Handling

**Exception Hierarchy:**
- `LastFMAPIError`: Base exception for all API errors
- `LastFMRateLimitError`: Raised for rate limit errors (code 29)
- `LastFMClientError`: Raised for client errors like invalid user (code 6)

**Error Codes:**
- `6`: Invalid user / user not found
- `29`: Rate limit exceeded
- Other codes: Generic API errors

## Scrobble Processing (`src/integrations/webhooks/lastfm.py`)

### LastFMScrobbleProcessor

Processes Last.fm track data and converts it to `MusicPlaybackEvent` for the music scrobble service.

#### `process_track()`

Processes a single Last.fm track and records it as a scrobble.

**Filtering:**
- Skips "now playing" tracks (tracks with `@attr.nowplaying == "true"`)
- Requires `date.uts` (Unix timestamp) - tracks without timestamps are skipped
- Filters exact duplicates before processing (see deduplication below)

**Data Extraction:**
- **Track title**: `track.name`
- **Artist name**: `track.artist.#text` or `track.artist.name`
- **Album title**: `track.album.#text` or `track.album.name` (defaults to "Unknown Album" if missing)
- **MBIDs**: Extracts `mbid` from artist, track, and album objects
  - `musicbrainz_artist`: Artist MBID
  - `musicbrainz_recording`: Track/recording MBID
  - `musicbrainz_release`: Album/release MBID (Last.fm doesn't distinguish release vs release-group)

**Timestamp Conversion:**
- Converts `date.uts` (Unix timestamp in seconds) to timezone-aware Django datetime
- Uses UTC for initial conversion, then converts to user's local timezone

**Event Creation:**
```python
event = music_scrobble.MusicPlaybackEvent(
    user=user,
    track_title=track_title,
    artist_name=artist_name,
    album_title=album_title if album_title != "Unknown Album" else None,
    track_number=None,  # Last.fm doesn't provide track numbers
    duration_ms=None,  # Last.fm doesn't provide duration in scrobbles
    plex_rating_key=None,
    external_ids=external_ids,  # MBIDs extracted above
    completed=True,  # All Last.fm scrobbles are completed
    played_at=played_at,
    defer_cover_prefetch=False,
)
```

#### `_is_duplicate()`

Checks if a scrobble is an exact duplicate to prevent re-importing the same scrobble.

**Deduplication Logic:**
- Matches on `(user, end_date, artist_name, track_title, album_title)`
- Uses 1-second tolerance for `end_date` to handle timezone conversion edge cases
- **Exact string matching** on artist and track names (no normalization - whitespace/case/punctuation must match exactly)
- Album match handles `None` values (both missing or both present and equal)

**Current Limitations:**
- No string normalization (`.strip()`, case folding, whitespace collapsing)
- No MBID preference - deduplication uses raw names even when MBIDs exist
- Name variations (e.g., "feat." vs "featuring", punctuation differences) may not dedupe correctly
- **Future Enhancement**: Consider normalizing strings and preferring MBID + timestamp matching when MBIDs are available

**Why This Matters:**
- Last.fm API pagination can return overlapping results
- Manual syncs might fetch the same time range multiple times
- Prevents duplicate history entries

#### `process_tracks()`

Processes multiple tracks and collects statistics.

**Returns:**
```python
{
    "processed": int,  # Successfully processed tracks
    "skipped": int,    # Skipped (now playing, duplicates, etc.)
    "errors": int,     # Errors during processing
    "affected_day_keys": set,  # Day keys for cache invalidation
}
```

**Cache Invalidation:**
- Collects `day_key` for each successfully processed track
- Returns `affected_day_keys` set for batch cache refresh
- Used by the polling task to invalidate only affected days

**Day Key Calculation:**
- `day_key` is calculated from the scrobble's `end_date` (after timezone conversion)
- Uses `history_day_key()` which calls `_localize_datetime()` to ensure user's local timezone
- Day keys are **always in user-local date**, not UTC
- This ensures a late-night UTC scrobble (e.g., 2 AM UTC = 8 PM previous day in PST) invalidates the correct day for that user
- History cache and statistics cache both use the same timezone-aware day key calculation for consistency

## Polling Mechanism (`src/integrations/tasks.py`)

### Global Periodic Task

```python
@shared_task(name="Poll Last.fm for all users")
def poll_all_lastfm_scrobbles():
    """Global task to poll Last.fm for all connected users."""
```

**Scheduling:**
- Created automatically when first user connects
- Uses `django-celery-beat` with `IntervalSchedule` (default: 15 minutes)
- Single global task processes all connected users (not per-user schedules)
- Task name: `"Poll Last.fm for all users"`

### Per-User Tasks

- `Poll Last.fm for user`: one-user incremental sync used by the connect flow and the "Sync now" button.
- `Import from Last.fm History`: bounded full-history importer that processes up to `LASTFM_HISTORY_PAGES_PER_TASK` pages per run, then requeues itself until the backfill is complete.

**Why Global Task:**
- Avoids schedule table bloat (one task vs N tasks for N users)
- Easier to manage and monitor
- All users share the same poll interval

**Overlapping Task Runs:**
- **Current Implementation**: No distributed locking mechanism
- Multiple workers, slow cycles, or manual syncs during active runs can cause overlapping executions
- This can lead to:
  - Rate limit spikes (duplicate API requests)
  - Duplicate processing load (same users processed multiple times)
  - Cache refresh conflicts
- **Future Enhancement**: Add Redis-based global lock around the entire task to prevent concurrent execution

### Processing Flow

1. **Get Connected Users:**
   ```python
   accounts = LastFMAccount.objects.filter(connection_broken=False).select_related("user")
   ```

2. **For Each User:**
   - Check if user has `music_enabled` (skip if disabled)
   - Calculate `from_timestamp_uts`:
     - Use `last_fetch_timestamp_uts - 60` (60-second overlap for safety)
     - If no previous timestamp, fetch all scrobbles newer than account connect time
   - Fetch tracks through the shared account-scoped helper in `src/integrations/lastfm_sync.py`
   - Process tracks oldest-first through `LastFMScrobbleProcessor`
   - Update `last_fetch_timestamp_uts` to most recent track's timestamp
   - Update sync status fields

**Timestamp Semantics:**
- Last.fm API's `from` parameter uses **"greater than or equal"** semantics (inclusive)
- The 60-second overlap (`last_fetch_timestamp_uts - 60`) ensures we don't miss scrobbles due to:
  - Clock skew between systems
  - Scrobbles that occur exactly at the cursor timestamp
  - Timezone conversion edge cases
- The overlap is safe because deduplication prevents re-importing the same scrobble

3. **Error Handling:**
   - **Rate Limit (29)**: Log warning, skip this cycle, don't mark as broken, don't advance cursor
   - **Invalid User (6)**: Mark `connection_broken=True`, increment `failure_count`, store error details
   - **Other Errors**: Increment `failure_count`, store error details, don't mark as broken
   - **Exceptions**: Catch all, log error, increment `failure_count`, don't mark as broken
   - **On Success**: Reset `failure_count=0`, clear `connection_broken=False`, clear error fields

**Failure Tracking:**
- `failure_count` is reset to `0` on **any successful poll** (not after N successes)
- `connection_broken` is only set to `True` for error code 6 (invalid user)
- Other errors (network issues, API hiccups, etc.) increment `failure_count` but don't break the connection
- This prevents temporary issues from permanently disabling accounts
- **Note**: There's no threshold for "persistent errors" - only explicit invalid user errors break the connection

4. **Batch Cache Refresh:**
   - Collect `affected_day_keys` from all processed users
   - Invalidate statistics cache for affected days (batch operation)
   - Schedule statistics refresh for all ranges
   - History cache invalidation is handled by per-track signals (see below)

### Batch Processing

**Jitter and Randomization:**
- Shuffles user list to avoid thundering herd
- Adds random delay (0.5-2.0s) between batches of 10 users
- Helps distribute load and avoid rate limits

**Cache Refresh Strategy:**
- Per-track signals handle history cache invalidation (see `src/app/signals.py`)
- Batch statistics cache refresh after all users processed
- Only invalidates days that actually had new scrobbles

## Metadata Resolution

Last.fm scrobbles are processed through the same `music_scrobble.record_music_playback()` service used by other sources (Plex, manual entry). See `docs/agents/music_integration.md` for full details.

**Resolution Priority:**
1. **MusicBrainz IDs**: If MBIDs are present in `external_ids`, use them directly
2. **MusicBrainz Search**: If no MBIDs, search by `(artist_name, track_title, album_title)`
   - Excludes "Unknown Album" and "Unknown" from search query to avoid noisy results
3. **Fallback**: Creates entries with available metadata, enriches later

**Key Differences from Plex:**
- Last.fm provides MBIDs directly (when available)
- No track numbers or duration in scrobbles
- Album title may be missing (treated as `None`, not "Unknown Album")

## Track-Aware Deduplication

The music scrobble service implements **track-specific deduplication** to handle short tracks played in quick succession.

**How It Works:**
- Each track has its own `Music` record (via unique `Item`)
- Deduplication only applies to the **same track** played within 2 minutes
- Different tracks played within 2 minutes are **both fully logged**

**Implementation (`src/app/services/music_scrobble.py`):**
```python
# Track-specific deduplication: Only prevent progress increment if THIS SAME TRACK
# was played within 2 minutes. Different tracks have different Music records (via
# unique item), so they are always fully logged regardless of timing.
if prior_end and abs(played_at - prior_end) <= timedelta(minutes=2):
    # Same track played within 2 minutes: don't increment progress, but still record history
    new_progress = music.progress or 1
else:
    # Different track or same track after 2 minutes: increment progress normally
    new_progress = (music.progress or 0) + 1
```

**Behavior:**
- **Same track within 2 minutes**: Progress doesn't increment, but `end_date` is updated (creates history record)
- **Different tracks within 2 minutes**: Both tracks get separate `Music` records, both increment progress, both create history records
- **Same track after 2 minutes**: Progress increments normally

**Why This Matters:**
- Short tracks (< 2 minutes) can be played multiple times in quick succession
- Each play should be recorded in history, even if progress doesn't increment
- Different tracks should never interfere with each other's deduplication

## Cache Invalidation

### History Cache

**Per-Track Invalidation:**
- `Music` model's `post_save` signal triggers `invalidate_history_days()`
- Invalidates the specific day for the scrobble's `end_date`
- Schedules refresh with `day_keys` to warm affected days (see recent fix)

**Batch Invalidation:**
- Polling task collects `affected_day_keys` from all processed tracks
- History cache invalidation is handled by signals (not in batch task)
- Ensures immediate invalidation as tracks are processed

### Statistics Cache

**Batch Invalidation:**
- Polling task collects `affected_day_keys` per user
- After all users processed, invalidates statistics cache for affected days
- Schedules refresh for all statistics ranges

**Why Batch:**
- Reduces cache operations (one batch per user vs per-track)
- Statistics are less time-sensitive than history display

### Lock Cleanup

**History Cache Refresh Locks:**
- When `day_keys` are provided, both `lock_key` and `dedupe_key` are created
- `dedupe_key` is stored in lock payload for cleanup
- Both keys are deleted when refresh task completes
- Prevents stuck locks when refresh tasks complete

## UI Integration

### Connection Flow (`src/integrations/views.py`)

#### `lastfm_connect`

Connects a Last.fm account by username.

**Flow:**
1. Validates username by making test API call
2. Creates/updates `LastFMAccount` record
3. Sets `last_fetch_timestamp_uts` to **current time** for recurring incremental sync
4. Initializes separate history-import state with `history_import_cutoff_uts = current_time - 1`
5. Creates or refreshes the global periodic task if needed
6. Queues `poll_lastfm_for_user.delay(user_id=...)`
7. Queues `import_lastfm_history.delay(user_id=...)`

**Initial Import Behavior:**
- Recurring sync starts from connection time and continues using `last_fetch_timestamp_uts`.
- Full history import runs separately, bounded by `history_import_cutoff_uts`, so it cannot race the recurring cursor.
- The backfill is chunked and resumes page-by-page using `history_import_next_page`.

**Error Handling:**
- Invalid username: Shows error, doesn't create account
- API errors: Shows error message, redirects to import page
- Database errors: Logs error, shows generic error message

#### `lastfm_disconnect`

Disconnects Last.fm account.

**Flow:**
1. Deletes `LastFMAccount` record
2. Global periodic task remains (other users may still be connected)
3. Shows success message

#### `poll_lastfm_manual`

Manually triggers a sync (bypasses periodic schedule).

**Flow:**
1. Validates user has connected Last.fm account
2. Calls `poll_lastfm_for_user.delay(user_id=...)`
3. Shows success message
4. User can check import page for results

#### `import_lastfm_history_manual`

Manually starts or reruns a full history import.

**Flow:**
1. Validates the user has a healthy Last.fm connection
2. Rejects the request if the history import is already `queued` or `running`
3. Resets history state from `last_fetch_timestamp_uts - 1`
4. Calls `import_lastfm_history.delay(user_id=...)`

### Import Data Page (`src/templates/users/import_data.html`)

**Last.fm Card:**
- Shows connection status (Connected/Disconnected)
- Displays last sync time (if available)
- Shows separate full-history status and page progress
- Shows error message if connection is broken
- Shows history-import error details when a backfill fails
- Provides "Sync now" button for manual sync
- Provides "Import full history" / "Reimport full history" when allowed
- Provides "Disconnect" button
- Shows poll interval (e.g., "every 15 minutes")

**Active Periodic Imports:**
- Last.fm appears in "Active Periodic Imports" section
- Shows username, last run time, next run time, schedule
- Uses global task, but displays per-user in the list

### User Model Integration (`src/users/models.py`)

**`get_import_tasks()`:**
- Includes global Last.fm task in schedules when user has connected account
- Handles `IntervalSchedule` to display "Every X minutes"
- Extracts username from `LastFMAccount` for display

## Configuration

### Environment Variables

```bash
# Required
LASTFM_API_KEY=your_api_key_here

# Optional (default: 15)
LASTFM_POLL_INTERVAL_MINUTES=15

# Optional (default: 5 pages / 1000 scrobbles per task)
LASTFM_HISTORY_PAGES_PER_TASK=5
```

### Settings (`src/config/settings.py`)

```python
LASTFM_API_KEY = config("LASTFM_API_KEY", default="")
LASTFM_POLL_INTERVAL_MINUTES = config("LASTFM_POLL_INTERVAL_MINUTES", default=15, cast=int)
```

## Error Handling & Monitoring

### Connection States

**Connected:**
- `lastfm_username` is set
- `connection_broken = False`
- `is_connected` returns `True`

**Disconnected:**
- No `LastFMAccount` record exists
- Or `lastfm_username` is empty

**Broken:**
- `connection_broken = True`
- Usually caused by invalid username (error code 6)
- Prevents further polling attempts
- Error details stored in `last_error_code` and `last_error_message`

### Failure Tracking

**Fields:**
- `failure_count`: Incremented on each error
- `last_error_code`: Last.fm API error code (e.g., "6", "29")
- `last_error_message`: Human-readable error message
- `last_failed_at`: Timestamp of last failure

**Recovery:**
- User can manually sync to retry
- User can disconnect and reconnect with correct username
- Rate limit errors don't mark connection as broken (temporary)

### Logging

**Key Log Messages:**
- `"Polling Last.fm for %d users"`: Start of polling cycle
- `"Successfully polled Last.fm for user %s: %d processed, %d skipped, %d errors"`: User completion
- `"Last.fm polling completed: %d users processed, %d errors"`: Cycle completion
- `"Processed Last.fm scrobble for %s: %s - %s"`: Individual scrobble processed
- `"Skipping now playing track"`: Filtered out "now playing" items
- `"Skipping duplicate scrobble"`: Exact duplicate detected

## Troubleshooting

### Common Issues

**"User not found" Error:**
- Username is incorrect or doesn't exist
- User's scrobbling is not public (Last.fm privacy settings)
- Solution: Verify username, check Last.fm privacy settings

**Rate Limit Errors:**
- Too many requests in short time
- Solution: Increase `LASTFM_POLL_INTERVAL_MINUTES`, wait for retry

**No Scrobbles Imported:**
- Check if user has any scrobbles on Last.fm
- Verify `last_fetch_timestamp_uts` isn't too recent (might skip old scrobbles)
- Check logs for processing errors

**Stuck Cache Refresh:**
- Lock might be stuck if refresh task failed
- Solution: Lock auto-expires after 5 minutes, or restart Celery worker

**Missing Tracks:**
- Short tracks played within 2 minutes might not increment progress
- But history records should still be created (check history page)
- Different tracks should always be logged separately

### Debugging

**Enable Debug Logging:**
```python
# settings.py
LOGGING = {
    'loggers': {
        'integrations.lastfm_api': {'level': 'DEBUG'},
        'integrations.webhooks.lastfm': {'level': 'DEBUG'},
        'app.services.music_scrobble': {'level': 'DEBUG'},
    }
}
```

**Check Last.fm Account Status:**
```python
from integrations.models import LastFMAccount

account = LastFMAccount.objects.get(user=user)
print(f"Connected: {account.is_connected}")
print(f"Last sync: {account.last_sync_at}")
print(f"Last fetch timestamp: {account.last_fetch_timestamp_uts}")
print(f"Failure count: {account.failure_count}")
print(f"Error: {account.last_error_message}")
```

**Check Periodic Task:**
```python
from django_celery_beat.models import PeriodicTask

task = PeriodicTask.objects.filter(task="Poll Last.fm for all users").first()
print(f"Enabled: {task.enabled}")
print(f"Last run: {task.last_run_at}")
print(f"Interval: {task.interval.every} {task.interval.period}")
```

## Future Enhancements

Potential improvements not yet implemented:

- **OAuth Support**: Currently uses public API with username only. OAuth would allow private scrobbles.
- **Real-time Webhooks**: If Last.fm adds webhook support, could replace polling.
- **Scrobble Submission**: Currently only imports from Last.fm. Could add ability to scrobble TO Last.fm.
- **Advanced Filtering**: Filter by date range, artist, album, etc.
- **Batch Size Configuration**: Make batch size and jitter configurable.

## Related Documentation

- `docs/agents/music_integration.md`: Music media type implementation
- `docs/agents/plex_integration.md`: Plex integration (for comparison with webhook-based approach)
- `docs/agents/pocketcasts_workflow.md`: Pocket Casts integration (similar polling pattern)
