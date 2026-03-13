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
        this.mediaType = options.mediaType || null;
        this.showMore = Boolean(options.showMore);
        this.pollInterval = options.pollInterval || 2500; // 2.5 seconds
        this.timeout = options.timeout || 120000; // 120 seconds (increased to handle longer refreshes)
        this.startTime = Date.now();
        this.pollTimer = null;
        this.isPolling = false;
        this.wasRefreshing = false; // Track if we were refreshing in previous poll
        this.lastBuiltAt = null; // Track built_at to detect fresh builds even if lock lingers
        this.onUpdateCallback = options.onUpdate || null;
        this.onRefreshCompleteCallback = options.onRefreshComplete || null;
    }

    currentRefreshActive(data) {
        if (!data) {
            return false;
        }

        const waitingOnPendingStatisticsRange =
            this.cacheType === 'statistics' &&
            ((!data.exists || data.is_stale) && data.any_range_refreshing);

        return Boolean(
            data.is_refreshing ||
            data.refresh_scheduled ||
            data.metadata_refreshing ||
            waitingOnPendingStatisticsRange
        );
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
            } else if (this.cacheType === 'discover' && this.mediaType) {
                params.append('media_type', this.mediaType);
                params.append('show_more', this.showMore ? '1' : '0');
            }

            const response = await fetch(`/api/cache-status/?${params.toString()}`);

            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }

            const data = await response.json();
            const prevBuiltAt = this.lastBuiltAt;
            const refreshActive = this.currentRefreshActive(data);
            console.log('CacheUpdater: Poll result', {
                exists: data.exists,
                is_stale: data.is_stale,
                is_refreshing: data.is_refreshing,
                recently_built: data.recently_built,
                any_range_refreshing: data.any_range_refreshing,
                wasRefreshing: this.wasRefreshing,
                wasRefreshingBefore: this.wasRefreshing, // Show before update
                built_at: data.built_at
            });

            // Check if refresh just completed (was refreshing, now not)
            const refreshJustCompleted = this.wasRefreshing && !refreshActive;
            // Detect if cache was rebuilt even though lock still looks active
            const builtAtChanged = this.wasRefreshing && data.built_at && data.built_at !== prevBuiltAt;

            // Update wasRefreshing for next poll (but keep old value for this check)
            const wasRefreshingBefore = this.wasRefreshing;
            this.wasRefreshing = refreshActive;
            // Track latest built_at
            this.lastBuiltAt = data.built_at || null;

            // Only reload if we've been polling for at least 2 seconds
            // This ensures we were actually waiting for the refresh, not just detecting
            // a refresh that completed before we started polling (which would cause loops)
            const hasBeenPolling = Date.now() - this.startTime >= 2000;

            // Reload only when we were actively waiting and the current cache
            // has transitioned to a fresh, ready state during polling.
            const currentRangeDone = hasBeenPolling && (
                refreshJustCompleted ||
                builtAtChanged
            ) && data.exists && !data.is_stale && !refreshActive;

            // Reload as soon as the current cache is rebuilt. Other ranges may still
            // be refreshing in the background, but they should not block the active page.
            const shouldUpdate = currentRangeDone;

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
            if (refreshActive) {
                console.log('CacheUpdater: Still refreshing, continuing to poll');
                this.pollTimer = setTimeout(() => this.poll(), this.pollInterval);
            } else if (data.exists && !data.is_stale && !refreshActive) {
                // If we were waiting for a refresh, we should have caught it above.
                // Otherwise, the current page is already up to date.
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
            } else if (data.exists && data.is_stale) {
                // Cache exists but is stale - only poll if a refresh is actually running
                if (this.cacheType === 'statistics' && !refreshActive && !data.any_range_refreshing) {
                    console.log('CacheUpdater: Cache is stale but not refreshing, stopping');
                    this.stop();
                    if (this.onRefreshCompleteCallback) {
                        this.onRefreshCompleteCallback(false, 'stale_no_refresh');
                    }
                } else {
                    console.log('CacheUpdater: Cache is stale, continuing to poll');
                    this.pollTimer = setTimeout(() => this.poll(), this.pollInterval);
                }
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
        // Mark that this is a cache-triggered reload with timestamp to prevent loops
        if (this.cacheType === 'history') {
            sessionStorage.setItem('history_cache_reload', Date.now().toString());
        }
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
