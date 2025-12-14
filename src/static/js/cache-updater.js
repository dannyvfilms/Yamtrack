/**
 * Cache Updater - Polls cache status and updates pages when fresh data is available
 * 
 * Usage:
 *   Initialize with: CacheUpdater.init(cacheType, options)
 *   Options: { rangeName, loggingStyle, pollInterval, timeout }
 */

class CacheUpdater {
    constructor(cacheType, options = {}) {
        this.cacheType = cacheType;
        this.rangeName = options.rangeName || null;
        this.loggingStyle = options.loggingStyle || 'repeats';
        this.pollInterval = options.pollInterval || 2500; // 2.5 seconds
        this.timeout = options.timeout || 120000; // 120 seconds (increased to handle longer refreshes)
        this.startTime = Date.now();
        this.pollTimer = null;
        this.isPolling = false;
        this.wasRefreshing = false; // Track if we were refreshing in previous poll
        this.onUpdateCallback = options.onUpdate || null;
        this.onRefreshCompleteCallback = options.onRefreshComplete || null;
    }

    /**
     * Start polling for cache updates
     */
    start() {
        if (this.isPolling) {
            return;
        }

        this.isPolling = true;
        this.startTime = Date.now();
        // Don't reset wasRefreshing here - it may have been set by the caller
        // to track the initial state (e.g., if refresh was already in progress)
        this.poll();
    }

    /**
     * Stop polling
     */
    stop() {
        this.isPolling = false;
        if (this.pollTimer) {
            clearTimeout(this.pollTimer);
            this.pollTimer = null;
        }
    }

    /**
     * Poll cache status endpoint
     */
    async poll() {
        if (!this.isPolling) {
            return;
        }

        // Check timeout
        if (Date.now() - this.startTime > this.timeout) {
            this.stop();
            if (this.onRefreshCompleteCallback) {
                this.onRefreshCompleteCallback(false, 'timeout');
            }
            return;
        }

        try {
            const params = new URLSearchParams({
                cache_type: this.cacheType,
            });

            if (this.cacheType === 'statistics' && this.rangeName) {
                params.append('range_name', this.rangeName);
            } else if (this.cacheType === 'history' && this.loggingStyle) {
                params.append('logging_style', this.loggingStyle);
            }

            const response = await fetch(`/api/cache-status/?${params.toString()}`);

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const data = await response.json();
            console.log('CacheUpdater: Poll result', {
                exists: data.exists,
                is_stale: data.is_stale,
                is_refreshing: data.is_refreshing,
                recently_built: data.recently_built,
                any_range_refreshing: data.any_range_refreshing,
                wasRefreshing: this.wasRefreshing
            });

            // Check if refresh just completed (was refreshing, now not)
            const refreshJustCompleted = this.wasRefreshing && !data.is_refreshing;

            // Update wasRefreshing for next poll (but keep old value for this check)
            const wasRefreshingBefore = this.wasRefreshing;
            this.wasRefreshing = data.is_refreshing;

            // If refresh just completed, update the page
            // This handles:
            // 1. Refresh completed during polling (refreshJustCompleted = true)
            // 2. We were waiting for refresh and now it's done (wasRefreshingBefore && !is_refreshing && exists && !stale)
            // 
            // For statistics: Also check if any other ranges are still refreshing.
            // Only reload when current range is done AND all ranges are done.
            // Note: We do NOT check recently_built here because if the cache was recently built
            // but we weren't waiting for it (wasRefreshingBefore = false), we shouldn't reload.
            // That would cause an infinite reload loop when the page loads after a refresh completes.
            const currentRangeDone = refreshJustCompleted ||
                (wasRefreshingBefore && !data.is_refreshing && data.exists && !data.is_stale);

            // For statistics, wait until all ranges are done before reloading
            // For history, reload as soon as current cache is done
            const shouldUpdate = currentRangeDone &&
                (this.cacheType !== 'statistics' || !data.any_range_refreshing);

            if (shouldUpdate) {
                console.log('CacheUpdater: Refresh completed, updating page', {
                    refreshJustCompleted,
                    recently_built: data.recently_built,
                    exists: data.exists,
                    is_stale: data.is_stale,
                    is_refreshing: data.is_refreshing,
                    any_range_refreshing: data.any_range_refreshing,
                    wasRefreshingBefore,
                    wasRefreshing: this.wasRefreshing,
                    currentRangeDone,
                    shouldUpdate
                });
                this.stop();
                if (this.onRefreshCompleteCallback) {
                    this.onRefreshCompleteCallback(true, 'complete');
                }
                // Trigger page update to show fresh data
                if (this.onUpdateCallback) {
                    this.onUpdateCallback();
                } else {
                    this.updatePage();
                }
                return;
            }

            // If still refreshing, continue polling
            if (data.is_refreshing) {
                console.log('CacheUpdater: Still refreshing, continuing to poll');
                this.pollTimer = setTimeout(() => this.poll(), this.pollInterval);
            } else if (data.exists && !data.is_stale && !data.is_refreshing) {
                // Cache exists, is fresh, and not refreshing
                // For statistics: if other ranges are still refreshing, continue polling
                if (this.cacheType === 'statistics' && data.any_range_refreshing) {
                    console.log('CacheUpdater: Current range done but other ranges still refreshing, continuing to poll', {
                        any_range_refreshing: data.any_range_refreshing
                    });
                    this.pollTimer = setTimeout(() => this.poll(), this.pollInterval);
                } else {
                    // If we were waiting for a refresh, we should have caught it above
                    // Otherwise, nothing to do
                    console.log('CacheUpdater: Cache is fresh and not refreshing, stopping', {
                        wasRefreshingBefore,
                        exists: data.exists,
                        is_stale: data.is_stale,
                        is_refreshing: data.is_refreshing,
                        any_range_refreshing: data.any_range_refreshing
                    });
                    this.stop();
                    if (this.onRefreshCompleteCallback) {
                        this.onRefreshCompleteCallback(true, 'complete');
                    }
                }
            } else if (data.exists && data.is_stale) {
                // Cache exists but is stale - continue polling in case refresh starts
                console.log('CacheUpdater: Cache is stale, continuing to poll');
                this.pollTimer = setTimeout(() => this.poll(), this.pollInterval);
            } else if (!data.exists) {
                // Cache doesn't exist and no refresh in progress, stop polling
                console.log('CacheUpdater: Cache does not exist, stopping');
                this.stop();
                if (this.onRefreshCompleteCallback) {
                    this.onRefreshCompleteCallback(false, 'no_cache');
                }
            }
        } catch (error) {
            console.error('Cache status poll error:', error);
            // Continue polling on error (might be temporary network issue)
            this.pollTimer = setTimeout(() => this.poll(), this.pollInterval);
        }
    }

    /**
     * Update the page content by reloading the current page
     */
    updatePage() {
        console.log('CacheUpdater: Updating page to show fresh data');
        // Reload the page to show fresh data
        // Using a small delay to ensure cache is fully updated
        setTimeout(() => {
            console.log('CacheUpdater: Reloading page now');
            window.location.reload();
        }, 300);
    }

    /**
     * Static method to initialize cache updater for a page
     */
    static init(cacheType, options = {}) {
        const updater = new CacheUpdater(cacheType, options);

        // Auto-start if cache is stale or refreshing
        // This will be checked on page load via Alpine.js
        return updater;
    }
}

// Export for use in modules
if (typeof module !== 'undefined' && module.exports) {
    module.exports = CacheUpdater;
}

// Make available globally
window.CacheUpdater = CacheUpdater;

