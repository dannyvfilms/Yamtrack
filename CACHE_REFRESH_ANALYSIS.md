# Cache Refresh Notification Analysis

## Pages That Need Cache Refresh Notifications

### ✅ Completed
1. **History Page** (`/history`)
   - ✅ Has cache refresh notification banner
   - ✅ Has auto-reload when refresh completes
   - ✅ Uses background refresh tasks
   - ✅ Has cache-status API endpoint support

### 🔧 Needs Fixing
2. **Statistics Page** (`/statistics`)
   - ⚠️ Has notification banner (but needs fixes)
   - ⚠️ Has partial polling logic (but needs completion)
   - ✅ Uses background refresh tasks
   - ✅ Has cache-status API endpoint support
   - **Issues to fix:**
     - Notification banner uses `$root.cacheRefreshing` which may not work correctly
     - Polling logic doesn't properly set `wasRefreshing` state
     - Missing proper `onUpdate` callback to reload page
     - Initial cache status check includes `recently_built` which could cause reload loop

### ❓ Needs Investigation
3. **TV Shows with time_left Sort** (`/medialist/tv?sort=time_left`)
   - Uses caching (5-minute TTL)
   - Cache is **immediately invalidated** when TV shows are updated (not background refresh)
   - Cache is rebuilt **inline** if missing (not background task)
   - **Current behavior:** Cache is cleared on TV save, then rebuilt on next page load
   - **Question:** Does this need background refresh notifications?
     - Pro: If sorting is expensive and user adds TV show, they might see stale data
     - Con: Cache is short-lived (5 min), rebuilds inline, no background task system exists
   - **Recommendation:** Probably doesn't need it unless sorting becomes very slow

## Implementation Details

### History Cache
- **Cache system:** `history_cache.py`
- **Refresh task:** `refresh_history_cache_task` (Celery)
- **Cache TTL:** 6 hours
- **Stale threshold:** 15 minutes
- **Refresh trigger:** Music/Episode/Movie changes via signals

### Statistics Cache
- **Cache system:** `statistics_cache.py`
- **Refresh task:** `refresh_statistics_cache_task` (Celery)
- **Cache TTL:** 6 hours
- **Stale threshold:** 15 minutes
- **Refresh trigger:** Media changes via signals
- **Multiple ranges:** All Time, Today, Yesterday, This Week, etc.

### Time Left Cache (TV Shows)
- **Cache system:** `cache_utils.py`
- **Refresh task:** None (rebuilds inline)
- **Cache TTL:** 5 minutes
- **Invalidation:** Immediate on TV save (not background)
- **Refresh trigger:** TV model save (clears cache immediately)

## Next Steps

1. **Fix Statistics Page** - Apply same fixes as history page:
   - Fix notification banner Alpine.js scope
   - Fix polling logic to properly track `wasRefreshing`
   - Add `onUpdate` callback to reload page
   - Remove `recently_built` from initial polling trigger

2. **Evaluate TV Shows time_left Cache** - Determine if background refresh is needed:
   - Measure sorting performance
   - Check if inline rebuild causes timeouts
   - If needed, implement background refresh system similar to history/statistics

