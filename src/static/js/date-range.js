function dateRangePicker(options = {}) {
  const {
    initialRangeName = "",
    initialStartDate = "",
    initialEndDate = "",
    initialCompareMode = "previous_period",
    refreshUrl = "",
    compareModeUpdateUrl = "",
    csrfToken = "",
  } = options;

  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const defaultStartDate = new Date(today);
  defaultStartDate.setFullYear(defaultStartDate.getFullYear() - 1);

  const predefinedRanges = [
    { name: "Today", displayName: "Today" },
    { name: "Yesterday", displayName: "Yesterday" },
    { name: "This Week", displayName: "This week" },
    { name: "Last 7 Days", displayName: "Last 7 days" },
    { name: "This Month", displayName: "Month to date" },
    { name: "Last 30 Days", displayName: "Last 30 days" },
    { name: "Last 90 Days", displayName: "Last 90 days" },
    { name: "This Year", displayName: "Year to date" },
    { name: "Last 6 Months", displayName: "Last 6 months" },
    { name: "Last 12 Months", displayName: "Last 12 months" },
    { name: "All Time", displayName: "All time" },
  ];

  const comparisonOptions = [
    { value: "previous_period", label: "Previous period" },
    { value: "last_year", label: "Last year" },
    { value: "none", label: "No comparison" },
  ];

  return {
    isRangeOpen: false,
    isCompareOpen: false,
    activeTab: "predefined",
    selectedRange: initialRangeName || "Last 12 Months",
    startDate: initialStartDate || formatDateForInput(defaultStartDate),
    endDate: initialEndDate || formatDateForInput(today),
    customRangeLabel: "",
    compareMode: initialCompareMode,
    refreshing: false,
    predefinedRanges,
    comparisonOptions,

    init() {
      const urlParams = new URLSearchParams(window.location.search);
      const startDateParam = urlParams.get("start-date");
      const endDateParam = urlParams.get("end-date");
      const compareParam = urlParams.get("compare");

      if (startDateParam && endDateParam) {
        this.startDate = startDateParam;
        this.endDate = endDateParam;
      } else if (initialStartDate && initialEndDate) {
        this.startDate = initialStartDate;
        this.endDate = initialEndDate;
      } else if (initialRangeName) {
        this.updateDatesFromRange(initialRangeName);
      }

      this.detectRangeFromDates(initialRangeName);
      this.compareMode = this.normalizeCompareMode(compareParam || initialCompareMode);
    },

    toggleRangeDropdown() {
      this.isRangeOpen = !this.isRangeOpen;
      if (this.isRangeOpen) {
        this.isCompareOpen = false;
      }
    },

    toggleCompareDropdown() {
      if (!this.hasFiniteRange()) {
        return;
      }

      this.isCompareOpen = !this.isCompareOpen;
      if (this.isCompareOpen) {
        this.isRangeOpen = false;
      }
    },

    hasFiniteRange() {
      return Boolean(
        this.startDate &&
          this.endDate &&
          this.startDate !== "all" &&
          this.endDate !== "all",
      );
    },

    normalizeCompareMode(mode) {
      if (!this.hasFiniteRange()) {
        return "none";
      }

      return this.comparisonOptions.some((option) => option.value === mode)
        ? mode
        : "previous_period";
    },

    getRangeDisplayName(rangeName = this.selectedRange) {
      const range = this.predefinedRanges.find((entry) => entry.name === rangeName);
      return range ? range.displayName : rangeName;
    },

    rangeTriggerLabel() {
      return this.isKnownPredefinedRange(this.selectedRange)
        ? this.getRangeDisplayName(this.selectedRange)
        : "Custom range";
    },

    currentRangeSummaryLabel() {
      if (!this.hasFiniteRange()) {
        return "All activity";
      }
      return this.formatDateRange(this.startDate, this.endDate);
    },

    comparisonTriggerLabel() {
      const option = this.comparisonOptions.find(
        (entry) => entry.value === this.compareMode,
      );
      return option ? option.label : "Previous period";
    },

    comparisonSummaryLabel(mode = this.compareMode) {
      if (mode === "none") {
        return "";
      }

      const range = this.getComparisonRange(mode);
      if (!range) {
        return "";
      }

      return this.formatDateRange(range.start, range.end);
    },

    isComparisonDisabled(mode) {
      return mode !== "none" && !this.hasFiniteRange();
    },

    async selectComparisonMode(mode) {
      if (this.isComparisonDisabled(mode) || this.compareMode === mode) {
        this.isCompareOpen = false;
        return;
      }

      const previousMode = this.compareMode;
      this.compareMode = mode;
      this.isCompareOpen = false;
      try {
        await this.saveCompareModePreference(mode);
      } catch (error) {
        this.compareMode = previousMode;
        console.error("Failed to save statistics compare mode:", error);
        return;
      }
      this.applyDateFilter();
    },

    selectPredefinedRange(rangeName) {
      this.selectedRange = rangeName;
      this.updateDatesFromRange(rangeName);
      this.isRangeOpen = false;
      this.applyDateFilter();
    },

    updateDatesFromRange(rangeName) {
      const range = this.calculateRangeDates(rangeName);
      if (!range) {
        return;
      }

      this.startDate = range.start;
      this.endDate = range.end;
      this.compareMode = this.normalizeCompareMode(this.compareMode);
    },

    calculateRangeDates(rangeName) {
      const rangeToday = new Date();
      rangeToday.setHours(0, 0, 0, 0);
      let start = new Date(rangeToday);
      let end = new Date(rangeToday);

      switch (rangeName) {
        case "Today":
          break;
        case "Yesterday":
          start.setDate(start.getDate() - 1);
          end = new Date(start);
          break;
        case "This Week": {
          const dayOfWeek = rangeToday.getDay();
          const diffToMonday = dayOfWeek === 0 ? 6 : dayOfWeek - 1;
          start.setDate(start.getDate() - diffToMonday);
          break;
        }
        case "Last 7 Days":
          start.setDate(start.getDate() - 6);
          break;
        case "This Month":
          start = new Date(rangeToday.getFullYear(), rangeToday.getMonth(), 1);
          break;
        case "Last 30 Days":
          start.setDate(start.getDate() - 29);
          break;
        case "Last 90 Days":
          start.setDate(start.getDate() - 89);
          break;
        case "This Year":
          start = new Date(rangeToday.getFullYear(), 0, 1);
          break;
        case "Last 6 Months":
          start = new Date(rangeToday);
          start.setMonth(start.getMonth() - 6);
          if (start.getDate() !== rangeToday.getDate()) {
            start = new Date(start.getFullYear(), start.getMonth() + 1, 0);
          }
          break;
        case "Last 12 Months":
          start = new Date(rangeToday);
          start.setFullYear(start.getFullYear() - 1);
          if (start.getDate() !== rangeToday.getDate()) {
            start = new Date(start.getFullYear(), start.getMonth() + 1, 0);
          }
          break;
        case "All Time":
          return { start: "all", end: "all" };
        default:
          return null;
      }

      return {
        start: formatDateForInput(start),
        end: formatDateForInput(end),
      };
    },

    getPredefinedRangeDatesLabel(rangeName) {
      const range = this.calculateRangeDates(rangeName);
      if (!range) {
        return "";
      }
      if (range.start === "all" && range.end === "all") {
        return "All activity";
      }
      return this.formatDateRange(range.start, range.end);
    },

    updateDateRange() {
      if (this.hasFiniteRange() && parseLocalDate(this.endDate) < parseLocalDate(this.startDate)) {
        this.endDate = this.startDate;
      }

      this.customRangeLabel = this.formatDateRange(this.startDate, this.endDate);
      this.compareMode = this.normalizeCompareMode(this.compareMode);
    },

    applyCustomRange() {
      this.customRangeLabel = this.formatDateRange(this.startDate, this.endDate);
      this.selectedRange = this.customRangeLabel;
      this.isRangeOpen = false;
      this.applyDateFilter();
    },

    applyDateFilter() {
      const url = new URL(window.location.href);
      url.searchParams.set("start-date", this.startDate);
      url.searchParams.set("end-date", this.endDate);
      url.searchParams.set("compare", this.normalizeCompareMode(this.compareMode));
      window.location.href = url.toString();
    },

    async saveCompareModePreference(mode) {
      if (!compareModeUpdateUrl) {
        return;
      }

      const body = new URLSearchParams();
      body.set("compare_mode", mode);

      const response = await fetch(compareModeUpdateUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": csrfToken,
          "X-Requested-With": "XMLHttpRequest",
        },
        body: body.toString(),
      });

      const data = await response.json();
      if (!response.ok || !data.success) {
        throw new Error(data.error || "Failed to save compare mode");
      }
    },

    formatDisplayDate(dateString) {
      if (!dateString || dateString === "all") {
        return "All time";
      }

      const date = parseLocalDate(dateString);
      const format = this.getDateFormat();

      if (!format) {
        return date.toLocaleDateString(undefined, {
          month: "short",
          day: "numeric",
          year: "numeric",
        });
      }

      return this.formatDateByDjangoFormat(date, format);
    },

    getDateFormat() {
      const scriptTag = document.querySelector("script[data-date-format]");
      const selectedFormat = scriptTag?.dataset.dateFormat;
      const dateFormats = this.getDateFormatValues();

      if (
        selectedFormat &&
        (!dateFormats.length || dateFormats.includes(selectedFormat))
      ) {
        return selectedFormat;
      }

      return dateFormats[0] || "";
    },

    getDateFormatValues() {
      const formatsElement = document.getElementById("date_format_values");

      if (!formatsElement?.textContent) {
        return [];
      }

      try {
        const dateFormats = JSON.parse(formatsElement.textContent);
        return Array.isArray(dateFormats) ? dateFormats : [];
      } catch {
        return [];
      }
    },

    formatDateByDjangoFormat(date, djangoFormat) {
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      const shortMonth = date.toLocaleString(undefined, { month: "short" });
      const longMonth = date.toLocaleString(undefined, { month: "long" });
      const shortWeekday = date.toLocaleString(undefined, { weekday: "short" });
      const longWeekday = date.toLocaleString(undefined, { weekday: "long" });
      const ordinalSuffix = this.getOrdinalSuffix(date.getDate());

      const formatters = {
        d: () => day,
        D: () => shortWeekday,
        F: () => longMonth,
        j: () => String(date.getDate()),
        l: () => longWeekday,
        m: () => month,
        M: () => shortMonth,
        n: () => String(date.getMonth() + 1),
        S: () => ordinalSuffix,
        y: () => String(year).slice(-2),
        Y: () => String(year),
      };

      let formattedDate = "";
      let isEscaped = false;

      for (const character of djangoFormat) {
        if (isEscaped) {
          formattedDate += character;
          isEscaped = false;
        } else if (character === "\\") {
          isEscaped = true;
        } else {
          formattedDate += formatters[character]?.() ?? character;
        }
      }

      return formattedDate;
    },

    getOrdinalSuffix(day) {
      if (day >= 11 && day <= 13) {
        return "th";
      }
      switch (day % 10) {
        case 1:
          return "st";
        case 2:
          return "nd";
        case 3:
          return "rd";
        default:
          return "th";
      }
    },

    formatDateRange(start, end) {
      if (!start || !end) {
        return "";
      }

      if (start === "all" && end === "all") {
        return "All activity";
      }

      const startLabel = this.formatDisplayDate(start);
      const endLabel = this.formatDisplayDate(end);
      return start === end ? startLabel : `${startLabel} - ${endLabel}`;
    },

    getComparisonRange(mode = this.compareMode) {
      if (this.isComparisonDisabled(mode)) {
        return null;
      }

      const currentStart = parseLocalDate(this.startDate);
      const currentEnd = parseLocalDate(this.endDate);
      let compareStart = new Date(currentStart);
      let compareEnd = new Date(currentEnd);

      if (mode === "previous_period") {
        const durationDays = Math.round(
          (currentEnd.getTime() - currentStart.getTime()) / 86400000,
        ) + 1;
        compareEnd = new Date(currentStart);
        compareEnd.setDate(compareEnd.getDate() - 1);
        compareStart = new Date(compareEnd);
        compareStart.setDate(compareStart.getDate() - (durationDays - 1));
      } else if (mode === "last_year") {
        compareStart = new Date(currentStart);
        compareEnd = new Date(currentEnd);
        compareStart.setFullYear(compareStart.getFullYear() - 1);
        compareEnd.setFullYear(compareEnd.getFullYear() - 1);
      } else {
        return null;
      }

      return {
        start: formatDateForInput(compareStart),
        end: formatDateForInput(compareEnd),
      };
    },

    detectRangeFromDates(preservedRangeName = "") {
      if (this.isKnownPredefinedRange(preservedRangeName)) {
        this.selectedRange = preservedRangeName;
        return;
      }

      if (this.startDate === "all" && this.endDate === "all") {
        this.selectedRange = "All Time";
        return;
      }

      const matchingRange = this.predefinedRanges.find((range) => {
        const calculated = this.calculateRangeDates(range.name);
        return (
          calculated &&
          calculated.start === this.startDate &&
          calculated.end === this.endDate
        );
      });

      if (matchingRange) {
        this.selectedRange = matchingRange.name;
        return;
      }

      this.customRangeLabel = this.formatDateRange(this.startDate, this.endDate);
      this.selectedRange = this.customRangeLabel;
    },

    isKnownPredefinedRange(rangeName) {
      return this.predefinedRanges.some((range) => range.name === rangeName);
    },

    async refreshStatistics() {
      if (!refreshUrl) {
        console.error("Refresh URL not available");
        return;
      }

      const isPredefinedRange = this.isKnownPredefinedRange(this.selectedRange);

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
          // Signal the stats Alpine component to start CacheUpdater and show the
          // banner. All page reloads go through CacheUpdater which guards on
          // data.exists before reloading, preventing blank-page races.
          window.dispatchEvent(new CustomEvent('stats-cache-rebuild-started'));

          // Poll only to know when to stop the button spinner.
          const maxAttempts = 180;
          let attempts = 0;

          const pollForCompletion = async () => {
            attempts += 1;
            try {
              const params = new URLSearchParams({
                cache_type: "statistics",
                range_name: this.selectedRange,
              });
              const statusResponse = await fetch(
                `/api/cache-status/?${params.toString()}`,
              );

              if (statusResponse.ok) {
                const statusData = await statusResponse.json();
                const stillRefreshing =
                  statusData.is_refreshing ||
                  statusData.refresh_scheduled ||
                  !statusData.exists;

                if (!stillRefreshing || attempts >= maxAttempts) {
                  this.refreshing = false;
                } else {
                  setTimeout(pollForCompletion, 1000);
                }
              } else if (attempts >= 5) {
                this.refreshing = false;
              } else {
                setTimeout(pollForCompletion, 1000);
              }
            } catch (error) {
              console.error("Error polling cache status:", error);
              if (attempts >= 5) {
                this.refreshing = false;
              } else {
                setTimeout(pollForCompletion, 1000);
              }
            }
          };

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

function parseLocalDate(dateString) {
  const [year, month, day] = dateString.split("-").map(Number);
  return new Date(year, month - 1, day);
}

function formatDateForInput(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

if (typeof window !== "undefined") {
  // Keep the controller on window for Alpine expressions in the statistics page.
  // The separate asset is loaded outside the inline Alpine scope.
  window.dateRangePicker = dateRangePicker;
}
