function dateRangePicker(options = {}) {
  const { initialRangeName = "", initialStartDate = "", initialEndDate = "", refreshUrl = "", csrfToken = "" } =
    options;
  const defaultStartDate = new Date(
    new Date().setFullYear(new Date().getFullYear() - 1)
  )
    .toISOString()
    .split("T")[0];
  const defaultEndDate = new Date().toISOString().split("T")[0];
  const hasInitialDates = Boolean(initialStartDate && initialEndDate);

  return {
    isOpen: false,
    activeTab: "predefined",
    selectedRange: initialRangeName || "Last 12 Months",
    startDate: hasInitialDates ? initialStartDate : defaultStartDate,
    endDate: hasInitialDates ? initialEndDate : defaultEndDate,
    customRangeLabel: "",
    refreshing: false,

    predefinedRanges: [
      { name: "Today" },
      { name: "Yesterday" },
      { name: "This Week" },
      { name: "Last 7 Days" },
      { name: "This Month" },
      { name: "Last 30 Days" },
      { name: "Last 90 Days" },
      { name: "This Year" },
      { name: "Last 6 Months" },
      { name: "Last 12 Months" },
      { name: "All Time" },
    ],

    init() {
      // Initialize dates from URL parameters if they exist
      const urlParams = new URLSearchParams(window.location.search);
      const startDateParam = urlParams.get("start-date");
      const endDateParam = urlParams.get("end-date");

      if (startDateParam && endDateParam) {
        this.startDate = startDateParam;
        this.endDate = endDateParam;

        // Try to determine which predefined range matches these dates
        this.detectRangeFromDates();
        return;
      }

      if (hasInitialDates) {
        this.startDate = initialStartDate;
        this.endDate = initialEndDate;
        this.detectRangeFromDates();
        return;
      }

      if (initialRangeName) {
        this.selectedRange = initialRangeName;
        this.updateDatesFromRange(initialRangeName);
      }
    },

    toggleDropdown() {
      this.isOpen = !this.isOpen;
    },

    selectPredefinedRange(rangeName) {
      this.selectedRange = rangeName;
      this.updateDatesFromRange(rangeName);
      this.isOpen = false;
      this.applyDateFilter();
    },

    updateDatesFromRange(rangeName) {
      const today = new Date();
      // Set time to start of day to avoid timezone issues
      today.setHours(0, 0, 0, 0);
      let start = new Date(today);
      let end = new Date(today);

      let shouldFormatDates = true;

      switch (rangeName) {
        case "Today":
          // Both start and end are today
          start = new Date(today);
          end = new Date(today);
          break;

        case "Yesterday":
          // Both start and end are yesterday
          start = new Date(today);
          start.setDate(start.getDate() - 1);
          end = new Date(start);
          break;

        case "This Week":
          // Start from Monday of current week
          const dayOfWeek = today.getDay(); // 0 is Sunday, 1 is Monday, etc.
          const diff = dayOfWeek === 0 ? 6 : dayOfWeek - 1; // Adjust to make Monday the first day
          start = new Date(today);
          start.setDate(start.getDate() - diff);
          end = new Date(today);
          break;

        case "Last 7 Days":
          start = new Date(today);
          start.setDate(start.getDate() - 6); // 6 days ago + today = 7 days
          end = new Date(today);
          break;

        case "This Month":
          // First day of current month to today
          start = new Date(today.getFullYear(), today.getMonth(), 1);
          end = new Date(today);
          break;

        case "Last 30 Days":
          start = new Date(today);
          start.setDate(start.getDate() - 29); // 29 days ago + today = 30 days
          end = new Date(today);
          break;

        case "Last 90 Days":
          start = new Date(today);
          start.setDate(start.getDate() - 89); // 89 days ago + today = 90 days
          end = new Date(today);
          break;

        case "This Year":
          // January 1st of current year to today
          start = new Date(today.getFullYear(), 0, 1);
          end = new Date(today);
          break;

        case "Last 6 Months":
          start = new Date(today);
          start.setMonth(start.getMonth() - 6);
          // If the day doesn't exist in the target month, it will roll over
          if (start.getDate() !== today.getDate()) {
            // If the days don't match, we rolled over to the next month
            // Set to the last day of the previous month
            start = new Date(start.getFullYear(), start.getMonth() + 1, 0);
          }
          end = new Date(today);
          break;

        case "Last 12 Months":
          start = new Date(today);
          start.setFullYear(start.getFullYear() - 1);
          // Handle the same day-of-month issue as with 6 months
          if (start.getDate() !== today.getDate()) {
            start = new Date(start.getFullYear(), start.getMonth() + 1, 0);
          }
          end = new Date(today);
          break;

        case "All Time":
          this.startDate = "all";
          this.endDate = "all";
          shouldFormatDates = false;
          break;
      }

      // Format dates as YYYY-MM-DD strings only if not "All Time"
      if (shouldFormatDates) {
        this.startDate = this.formatDateForInput(start);
        this.endDate = this.formatDateForInput(end);
      }
    },

    formatDateForInput(date) {
      // Format date as YYYY-MM-DD
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${year}-${month}-${day}`;
    },

    updateDateRange() {
      // Ensure end date is not before start date
      if (new Date(this.endDate) < new Date(this.startDate)) {
        this.endDate = this.startDate;
      }

      this.customRangeLabel = `${this.formatDisplayDate(
        this.startDate
      )} - ${this.formatDisplayDate(this.endDate)}`;
    },

    applyCustomRange() {
      this.selectedRange = this.customRangeLabel;
      this.isOpen = false;
      this.applyDateFilter();
    },

    applyDateFilter() {
      // Create URL with date parameters
      const url = new URL(window.location.href);
      url.searchParams.set("start-date", this.startDate);
      url.searchParams.set("end-date", this.endDate);

      // Navigate to the URL
      window.location.href = url.toString();
    },

    formatDisplayDate(dateString) {
      // Parse date string (YYYY-MM-DD) as local date to avoid timezone conversion issues
      // When using new Date("YYYY-MM-DD"), it's interpreted as UTC midnight,
      // which can shift to the previous day in local time zones ahead of UTC
      const [year, month, day] = dateString.split("-").map(Number);
      const date = new Date(year, month - 1, day); // month is 0-indexed
      return date.toLocaleDateString("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
      });
    },

    detectRangeFromDates() {
      // Check for All Time (arbitrary start date)
      if (this.startDate === "all" && this.endDate === "all") {
        this.selectedRange = "All Time";
        return;
      }
      // Parse the current start and end dates as local dates to avoid timezone conversion issues
      // When using new Date("YYYY-MM-DD"), it's interpreted as UTC midnight,
      // which can shift to the previous day in local time zones ahead of UTC
      const parseLocalDate = (dateString) => {
        const [year, month, day] = dateString.split("-").map(Number);
        return new Date(year, month - 1, day); // month is 0-indexed
      };
      const startDate = parseLocalDate(this.startDate);
      const endDate = parseLocalDate(this.endDate);

      // Get today's date with time set to 00:00:00
      const today = new Date();
      today.setHours(0, 0, 0, 0);

      // Helper function to check if two dates are the same day
      const isSameDay = (d1, d2) => {
        return (
          d1.getFullYear() === d2.getFullYear() &&
          d1.getMonth() === d2.getMonth() &&
          d1.getDate() === d2.getDate()
        );
      };

      // Check for Today
      if (isSameDay(startDate, today) && isSameDay(endDate, today)) {
        this.selectedRange = "Today";
        return;
      }

      // Check for Yesterday
      const yesterday = new Date(today);
      yesterday.setDate(yesterday.getDate() - 1);
      if (isSameDay(startDate, yesterday) && isSameDay(endDate, yesterday)) {
        this.selectedRange = "Yesterday";
        return;
      }

      // Check for This Week
      const thisWeekStart = new Date(today);
      const dayOfWeek = today.getDay();
      const diffToMonday = dayOfWeek === 0 ? 6 : dayOfWeek - 1;
      thisWeekStart.setDate(today.getDate() - diffToMonday);
      if (isSameDay(startDate, thisWeekStart) && isSameDay(endDate, today)) {
        this.selectedRange = "This Week";
        return;
      }

      // Check for Last 7 Days
      const last7DaysStart = new Date(today);
      last7DaysStart.setDate(today.getDate() - 6);
      if (isSameDay(startDate, last7DaysStart) && isSameDay(endDate, today)) {
        this.selectedRange = "Last 7 Days";
        return;
      }

      // Check for This Month
      const thisMonthStart = new Date(today.getFullYear(), today.getMonth(), 1);
      if (isSameDay(startDate, thisMonthStart) && isSameDay(endDate, today)) {
        this.selectedRange = "This Month";
        return;
      }

      // Check for Last 30 Days
      const last30DaysStart = new Date(today);
      last30DaysStart.setDate(today.getDate() - 29);
      if (isSameDay(startDate, last30DaysStart) && isSameDay(endDate, today)) {
        this.selectedRange = "Last 30 Days";
        return;
      }

      // Check for Last 90 Days
      const last90DaysStart = new Date(today);
      last90DaysStart.setDate(today.getDate() - 89);
      if (isSameDay(startDate, last90DaysStart) && isSameDay(endDate, today)) {
        this.selectedRange = "Last 90 Days";
        return;
      }

      // Check for This Year
      const thisYearStart = new Date(today.getFullYear(), 0, 1);
      if (isSameDay(startDate, thisYearStart) && isSameDay(endDate, today)) {
        this.selectedRange = "This Year";
        return;
      }

      // Check for Last 6 Months
      // This is more complex due to varying month lengths
      const last6MonthsStart = new Date(today);
      last6MonthsStart.setMonth(today.getMonth() - 6);
      // Adjust for month length differences
      if (last6MonthsStart.getDate() !== today.getDate()) {
        // We rolled over to the next month, adjust to last day of previous month
        last6MonthsStart.setDate(0);
      }

      // Allow a 1-day difference for month-end variations
      const isWithinOneDay = (d1, d2) => {
        const diff = Math.abs(d1.getTime() - d2.getTime());
        return diff <= 86400000; // 24 hours in milliseconds
      };

      if (
        isWithinOneDay(startDate, last6MonthsStart) &&
        isSameDay(endDate, today)
      ) {
        this.selectedRange = "Last 6 Months";
        return;
      }

      // Check for Last 12 Months
      const last12MonthsStart = new Date(today);
      last12MonthsStart.setFullYear(today.getFullYear() - 1);
      // Adjust for month length differences and leap years
      if (last12MonthsStart.getDate() !== today.getDate()) {
        last12MonthsStart.setDate(0);
      }

      if (
        isWithinOneDay(startDate, last12MonthsStart) &&
        isSameDay(endDate, today)
      ) {
        this.selectedRange = "Last 12 Months";
        return;
      }

      // If no match found, use custom range format
      this.customRangeLabel = `${this.formatDisplayDate(
        this.startDate
      )} - ${this.formatDisplayDate(this.endDate)}`;
      this.selectedRange = this.customRangeLabel;
    },

    async refreshStatistics() {
      if (!refreshUrl) {
        console.error("Refresh URL not available");
        return;
      }

      const isPredefinedRange = this.selectedRange && this.predefinedRanges.findIndex(r => r.name === this.selectedRange) !== -1;

      // For custom ranges, just reload the page (they're computed inline)
      if (!isPredefinedRange) {
        this.refreshing = true;
        setTimeout(() => {
          window.location.reload();
        }, 100);
        return;
      }

      this.refreshing = true;
      try {
        const formData = new FormData();
        formData.append("range_name", this.selectedRange);
        if (csrfToken) {
          formData.append("csrfmiddlewaretoken", csrfToken);
        }

        const response = await fetch(refreshUrl, {
          method: "POST",
          body: formData,
        });

        if (response.ok) {
          // Poll for cache completion instead of reloading immediately
          // This ensures the refresh is complete before reloading
          const maxAttempts = 60; // 60 attempts * 1 second = 60 seconds max
          let attempts = 0;
          
          const pollForCompletion = async () => {
            attempts++;
            try {
              const params = new URLSearchParams({
                cache_type: 'statistics',
                range_name: this.selectedRange
              });
              const statusResponse = await fetch(`/api/cache-status/?${params.toString()}`);
              
              if (statusResponse.ok) {
                const statusData = await statusResponse.json();
                
                // Check if refresh is complete:
                // 1. Not currently refreshing (is_refreshing includes refresh_lock, refresh_scheduled, metadata_refreshing)
                // 2. No other ranges are refreshing
                // 3. Cache exists and is fresh (recently built or not stale)
                const isComplete = !statusData.is_refreshing && 
                                  !statusData.any_range_refreshing && 
                                  statusData.exists && 
                                  (statusData.recently_built || !statusData.is_stale);
                
                if (isComplete) {
                  // Refresh is complete, reload the page to show fresh data
                  console.log('Statistics refresh complete, reloading page');
                  window.location.reload();
                } else if (attempts >= maxAttempts) {
                  // Timeout - reload anyway
                  console.warn('Statistics refresh polling timeout, reloading page');
                  window.location.reload();
                } else {
                  // Continue polling
                  setTimeout(pollForCompletion, 1000);
                }
              } else {
                // If status check fails, reload after a few attempts
                console.error('Failed to check cache status:', statusResponse.status);
                if (attempts >= 5) {
                  window.location.reload();
                } else {
                  setTimeout(pollForCompletion, 1000);
                }
              }
            } catch (error) {
              console.error("Error polling cache status:", error);
              // If polling fails, reload after a few attempts
              if (attempts >= 5) {
                window.location.reload();
              } else {
                setTimeout(pollForCompletion, 1000);
              }
            }
          };
          
          // Start polling after a short delay to give the refresh time to start
          setTimeout(pollForCompletion, 1000);
        } else {
          console.error("Failed to refresh statistics");
          this.refreshing = false;
        }
      } catch (error) {
        console.error("Error refreshing statistics:", error);
        this.refreshing = false;
      }
    },
  };
}
