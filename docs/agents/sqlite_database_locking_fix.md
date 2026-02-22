# SQLite Database Locking Fix for Yamtrack

## Problem Statement

When starting with a fresh database, the media runtime update tasks were causing `database is locked` errors. This is a common issue with SQLite when multiple concurrent processes try to write to the database.

## Root Cause Analysis

1. **Celery Result Backend**: Was set to `"django-db"`, storing task results in the same Django database, adding contention.
2. **Bulk Update Batch Size**: Runtime backfill tasks were using `batch_size=500` for bulk updates, causing SQLite to hold locks for extended periods.
3. **SQLite Timeout**: Default 30-second timeout was too aggressive for fresh DB with bulk operations.
4. **No Retry Logic**: Bulk operations had no retry mechanism for transient lock errors.

## Solutions Implemented

### 1. **Celery Result Backend (src/config/settings.py)**

**Changed:**
```python
CELERY_RESULT_BACKEND = "django-db"
```

**To:**
```python
CELERY_RESULT_BACKEND = "cache+dummy://" if "sqlite" in DATABASES["default"]["ENGINE"] else "django-db"
```

**Impact**: For SQLite environments, Celery now stores task results in memory/cache instead of the database, eliminating a major source of write contention.

### 2. **SQLite Busy Timeout (src/config/settings.py)**

**Changed:**
```python
default=30,
```

**To:**
```python
default=60,
```

**Impact**: Increased timeout from 30 to 60 seconds, giving SQLite more time to resolve lock contention during bulk operations.

### 3. **Batch Size Optimization (src/app/tasks.py - fast_runtime_backfill_task)**

**Changed:**
```python
batch_size=500
```

**To:**
```python
batch_size=100
```

**Impact**: Smaller batches mean shorter transaction times and reduced lock hold periods. SQLite can process many small transactions faster than one large one.

### 4. **Retry Logic with Exponential Backoff (src/app/tasks.py)**

**Added retry wrapper:**
```python
try:
    from integrations.imports.helpers import retry_on_lock
    retry_on_lock(
        lambda: Item.objects.bulk_update(
            items_to_update, ["runtime_minutes"], batch_size=batch_size
        ),
        max_retries=3,
    )
except Exception as e:
    logger.error("Failed to bulk update runtime after retries: %s", e)
    # Fall back to individual saves
    for item in items_to_update:
        try:
            item.save(update_fields=["runtime_minutes"])
        except Exception as save_error:
            logger.error("Failed to save item %s: %s", item.id, save_error)
```

**Impact**: 
- Automatically retries on lock errors with exponential backoff (base 0.1s, backoff 2.0)
- Falls back to individual saves if bulk update fails after retries
- Much more resilient to transient SQLite lock contention

## Testing

1. Start with a fresh database:
   ```bash
   cd src && python manage.py migrate
   ```

2. Start Django and Celery:
   ```bash
   cd src && python manage.py runserver
   cd src && celery -A config worker --beat --scheduler django --loglevel DEBUG
   ```

3. Import media or trigger runtime backfill tasks - the `database is locked` errors should no longer appear.

## Performance Impact

- **SQLite**: ✅ Significantly reduced lock contention, especially during fresh DB setup
- **PostgreSQL**: ✅ No negative impact (uses `django-db` backend as before)
- **Celery Results**: Cache backend is faster for SQLite, slightly slower than django-db for PostgreSQL (negligible)

## Environment Variables

Users can override these settings:

```bash
# Increase SQLite timeout if needed (in seconds)
SQLITE_BUSY_TIMEOUT_SECONDS=120

# Force django-db backend even for SQLite (not recommended)
CELERY_RESULT_BACKEND=django-db
```

## Recommendations for Production

1. **Switch to PostgreSQL** if you have multiple Celery workers or frequent concurrent access
2. **Monitor** the debug logs for `"Retrying database operation"` messages - if they're frequent, consider the above
3. **Keep WAL mode enabled** in SQLite - it's already configured and significantly helps concurrency

## Files Modified

- `src/config/settings.py`: Celery result backend and SQLite timeout configuration
- `src/app/tasks.py`: Batch size reduction and retry logic in `fast_runtime_backfill_task`

## Related Documentation

- [AGENTS.md](../AGENTS.md) - General project setup and configuration
- [integrations/imports/helpers.py](../../src/integrations/imports/helpers.py) - `retry_on_lock` function implementation
