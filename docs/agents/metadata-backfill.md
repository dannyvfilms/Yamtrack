# Metadata Backfill System

## Overview

The metadata backfill system ensures all Items in the database have comprehensive metadata available for filtering, sorting, and statistics without relying on temporary cache storage.

## How It Works

### New Items
- When items are added to your library, metadata is immediately fetched and stored in the database
- The `metadata_fetched_at` timestamp is set, indicating metadata has been processed
- All filtering fields (country, languages, platforms, etc.) are populated automatically

### Existing Items (Legacy Data)
- Items without `metadata_fetched_at` timestamp have never had metadata fetched
- The backfill system processes these items in batches
- Even if a provider has no metadata available, the timestamp is set to prevent infinite retries

### Automatic Backfill

The system runs automatically with intelligent batch sizing:

#### 1. Calendar Reload Integration (Primary Method)
- Runs as part of the daily calendar refresh
- **Initial backfill**: Processes 5,000 items per run when >1,000 items remain
- **Cleanup mode**: Processes 1,000 items per run when <1,000 items remain
- **Maintenance mode**: Skips when all items are processed
- Typically completes entire library in 1-2 calendar refreshes

#### 2. Daily Scheduled Task (Backup)
- Runs every day at 3 AM as separate task
- Processes 1,000 items per run
- Acts as safety net for any missed items

### Manual Backfill

For Docker users or initial setup, you can manually trigger backfill:

```bash
# Process all items that have never been checked
python manage.py backfill_item_metadata

# Test with a small batch
python manage.py backfill_item_metadata --limit 100

# Process specific media type
python manage.py backfill_item_metadata --media-type tv
python manage.py backfill_item_metadata --media-type movie
python manage.py backfill_item_metadata --media-type anime

# Force re-fetch for all items (rarely needed)
python manage.py backfill_item_metadata --force
```

## What Gets Stored

The following metadata fields are fetched and stored:

| Field | Type | Used By | Description |
|-------|------|---------|-------------|
| `country` | String | TV, Movie, Anime, Podcast | Origin country |
| `languages` | Array | TV, Movie, Anime, Podcast | Available languages |
| `platforms` | Array | Game | Gaming platforms |
| `format` | String | All | Media format (TV, Movie, OVA, etc.) |
| `status` | String | TV, Movie, Anime, Manga | Production status |
| `studios` | Array | TV, Movie, Anime | Production studios |
| `themes` | Array | Game | Game themes |
| `authors` | Array | Book, Manga, Comic | Authors/creators |
| `publishers` | String | Book, Comic | Publisher name |
| `isbn` | Array | Book | ISBN numbers |
| `source_material` | String | Anime | Source (manga, novel, original) |
| `creators` | Array | Comic | Comic creators |
| `runtime` | String | TV, Movie, Anime | Formatted runtime |
| `metadata_fetched_at` | Timestamp | All | When metadata was last fetched |

## Monitoring

You can check the backfill progress:

```bash
# Check how many items still need metadata
python manage.py shell -c "from app.models import Item; print(f'Items pending: {Item.objects.filter(metadata_fetched_at__isnull=True).count()}')"

# Check when an item was last updated
python manage.py shell -c "from app.models import Item; item = Item.objects.first(); print(f'Last fetched: {item.metadata_fetched_at}')"
```

## Benefits

✅ **No-op after first check**: Items are only processed once
✅ **Efficient**: Only processes items that truly need processing
✅ **Resilient**: Errors don't cause infinite retries
✅ **Auditable**: Timestamp shows when metadata was fetched
✅ **Automatic**: Runs daily without user intervention
✅ **Non-blocking**: Doesn't slow down normal operations

## Configuration

The backfill schedule can be adjusted in `config/settings.py`:

```python
CELERY_BEAT_SCHEDULE = {
    "backfill_item_metadata": {
        "task": "Backfill item metadata",
        "schedule": crontab(hour=3, minute=0),  # Change time here
        "kwargs": {"batch_size": 1000},  # Backup task batch size
    },
}
```

The calendar reload integration uses adaptive batch sizing:
- `>1000 items remaining`: 5,000 items per run
- `<1000 items remaining`: 1,000 items per run
- `0 items remaining`: Skips backfill entirely

## For Docker Users

If you're running Yamtrack in Docker and don't have terminal access:

1. **Initial Backfill**: Automatically processes 5,000 items per calendar refresh
2. **Estimated Time**:
   - Library with 50,000 items: ~10 calendar refreshes (10 days)
   - Library with 10,000 items: ~2 calendar refreshes (2 days)
   - Library with 5,000 items: 1 calendar refresh (1 day)
3. **Zero Configuration**: Works automatically, no settings changes needed
4. **New Items**: All new items added to your library get metadata immediately - no waiting needed

## Troubleshooting

**Q: Items still don't show up in filters**
- Check if metadata was actually fetched: Look at `metadata_fetched_at` timestamp
- Verify the provider has metadata for that item
- Clear your cache and try again

**Q: Backfill seems slow**
- By design! It processes items gradually to avoid API rate limits
- Increase `batch_size` in settings if needed
- Run manual backfill once for faster initial population

**Q: How do I know when backfill is complete?**
- Run: `Item.objects.filter(metadata_fetched_at__isnull=True).count()`
- When this returns 0, all items have been processed

## Technical Details

- **Database Field**: `metadata_fetched_at` (DateTimeField, nullable)
- **Task Name**: `Backfill item metadata`
- **Location**: `app/tasks.py:backfill_item_metadata_task`
- **Command**: `app/management/commands/backfill_item_metadata.py`
- **Schedule**: Defined in `config/settings.py:CELERY_BEAT_SCHEDULE`
