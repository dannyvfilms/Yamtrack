document.addEventListener("DOMContentLoaded", function () {
  Chart.register(ChartDataLabels);

  // Custom external tooltip for bar charts
  function customBarTooltip(context) {
    // External custom tooltip
    let tooltipEl = document.getElementById("chartjs-tooltip");

    // Create element if it doesn't exist
    if (!tooltipEl) {
      tooltipEl = document.createElement("div");
      tooltipEl.id = "chartjs-tooltip";
      tooltipEl.innerHTML = "<table></table>";
      document.body.appendChild(tooltipEl);
    }

    // Hide if no tooltip
    const tooltipModel = context.tooltip;
    if (tooltipModel.opacity === 0) {
      tooltipEl.style.opacity = 0;
      return;
    }

    // Set Text
    if (tooltipModel.body) {
      const chart = context.chart;
      const dataIndex = tooltipModel.dataPoints[0].dataIndex;
      const title = tooltipModel.title[0] || "";

      // Format title based on chart type
      let formattedTitle = title;
      if (chart.canvas.id === "scoreStackedChart") {
        const score = parseInt(title);
        if (score === 10) {
          formattedTitle = `Score: 10`;
        } else {
          formattedTitle = `Score: ${score}.0-${score}.9`;
        }
      }

      // Get all values for this stack and format to 1 decimal place
      let tableBody =
        '<thead><tr><th colspan="2">' +
        formattedTitle +
        "</th></tr></thead><tbody>";
      let stackTotal = 0;

      function fmt(v) {
        const n = Number(v) || 0;
        return n.toFixed(1);
      }

      chart.data.datasets.forEach((dataset, i) => {
        const raw = Number(dataset.data[dataIndex]) || 0;
        if (raw > 0) {
          stackTotal += raw;
          const bgColor = dataset.backgroundColor;
          const label = dataset.label || "";
          const value = fmt(raw);

          tableBody +=
            "<tr>" +
            '<td style="padding-right:15px;"><span style="display:inline-block;width:12px;height:12px;background:' +
            bgColor +
            ';margin-right:8px;border-radius:2px;"></span>' +
            label +
            ":</td>" +
            '<td style="text-align:right;font-weight:bold;">' +
            value +
            "</td>" +
            "</tr>";
        }
      });

      // Add total row (formatted)
      tableBody +=
        '<tr class="total-row">' +
        "<td>Total:</td>" +
        '<td style="text-align:right;font-weight:bold;">' +
        (stackTotal.toFixed ? stackTotal.toFixed(1) : Number(stackTotal).toFixed(1)) +
        "</td>" +
        "</tr>";

      tableBody += "</tbody>";

      const tableRoot = tooltipEl.querySelector("table");
      tableRoot.innerHTML = tableBody;
    }

    // Position and style the tooltip
    const position = context.chart.canvas.getBoundingClientRect();

    // Set tooltip styles
    tooltipEl.style.opacity = 1;
    tooltipEl.style.position = "absolute";
    tooltipEl.style.left =
      position.left + window.scrollX + tooltipModel.caretX + "px";
    tooltipEl.style.top =
      position.top + window.scrollY + tooltipModel.caretY + "px";
    tooltipEl.style.transform = "translate(-50%, -100%)";
    tooltipEl.style.pointerEvents = "none";
  }

  // Custom external tooltip for pie charts
  function customPieTooltip(context) {
    // External custom tooltip
    let tooltipEl = document.getElementById("chartjs-pie-tooltip");

    // Create element if it doesn't exist
    if (!tooltipEl) {
      tooltipEl = document.createElement("div");
      tooltipEl.id = "chartjs-pie-tooltip";
      document.body.appendChild(tooltipEl);
    }

    // Hide if no tooltip
    const tooltipModel = context.tooltip;
    if (tooltipModel.opacity === 0) {
      tooltipEl.style.opacity = 0;
      return;
    }

    // Set Text
    if (tooltipModel.body) {
      const dataPoint = tooltipModel.dataPoints[0];
      const label = dataPoint.label;
      const value = dataPoint.raw;

      // Calculate percentage
      const dataset = context.chart.data.datasets[dataPoint.datasetIndex];
      const total = dataset.data.reduce((sum, val) => sum + val, 0);
      const percentage = Math.round((value / total) * 100);

      // Create tooltip content
      let tooltipContent = `
        <div class="pie-label">${label}</div>
        <div class="pie-value">Count: ${value}</div>
        <div class="pie-percent">${percentage}%</div>
      `;

      tooltipEl.innerHTML = tooltipContent;
    }

    // Position and style the tooltip
    const position = context.chart.canvas.getBoundingClientRect();

    // Set tooltip styles
    tooltipEl.style.opacity = 1;
    tooltipEl.style.position = "absolute";
    tooltipEl.style.left =
      position.left + window.scrollX + tooltipModel.caretX + "px";
    tooltipEl.style.top =
      position.top + window.scrollY + tooltipModel.caretY + "px";
    tooltipEl.style.transform = "translate(-50%, -100%)";
    tooltipEl.style.pointerEvents = "none";
  }

  // Common configuration for pie charts
  const pieChartConfig = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      datalabels: {
        color: "#D1D5DB",
        font: { size: 12 },
        formatter: (value, ctx) => {
          const total = ctx.dataset.data.reduce((acc, data) => acc + data, 0);
          const percentage = Math.round((value / total) * 100);
          const label = ctx.chart.data.labels[ctx.dataIndex];
          return percentage > 5 ? `${label}\n${percentage}%` : "";
        },
        textAlign: "center",
        textStrokeColor: "rgba(0,0,0,0.5)",
        textStrokeWidth: 2,
        textShadowBlur: 5,
        textShadowColor: "rgba(0,0,0,0.5)",
        padding: 6,
      },
      legend: {
        position: "bottom",
        labels: {
          color: "#D1D5DB",
          padding: 20,
          usePointStyle: true,
          pointStyle: "rectRounded",
          generateLabels: function (chart) {
            const original =
              Chart.overrides.pie.plugins.legend.labels.generateLabels;
            const labels = original.call(this, chart);
            labels.forEach((label, i) => {
              label.text = `${label.text} (${chart.data.datasets[0].data[i]})`;
              label.strokeStyle = "transparent";
            });
            return labels;
          },
        },
        margin: { top: 20 },
      },
      tooltip: {
        enabled: false,
        external: customPieTooltip,
      },
    },
    layout: { padding: { bottom: 10 } },
    elements: {
      arc: {
        borderWidth: 1,
        borderColor: "#d3d3d3",
      },
    },
  };

  // Common configuration for bar charts
  const barChartConfig = {
    responsive: true,
    maintainAspectRatio: false,
    scales: {
      x: {
        stacked: true,
        grid: { color: "rgba(255, 255, 255, 0.1)" },
        ticks: { color: "#D1D5DB" },
      },
      y: {
        stacked: true,
        beginAtZero: true,
        grid: { color: "rgba(255, 255, 255, 0.1)" },
        ticks: { color: "#D1D5DB", precision: 0 },
      },
    },
    plugins: {
      legend: {
        position: "bottom",
        labels: {
          color: "#D1D5DB",
          padding: 20,
          boxWidth: 12,
          boxHeight: 12,
          usePointStyle: true,
          pointStyle: "rectRounded",
          textAlign: "center",
          font: {
            size: 12,
            lineHeight: 0.1,
          },
        },
      },
      tooltip: {
        enabled: false, // Disable default tooltip
        mode: "index",
        external: customBarTooltip,
      },
      // Disable datalabels for bar charts
      datalabels: {
        display: false,
      },
    },
    interaction: {
      mode: "index",
      intersect: false,
    },
  };

  // Helper function to process stacked bar data
  function processBarData(chartData) {
    return {
      labels: chartData.labels,
      datasets: chartData.datasets
        .map((dataset) => ({
          label: dataset.label,
          data: dataset.data,
          backgroundColor: dataset.background_color,
          borderColor: "rgba(255, 255, 255, 0.1)",
          borderRadius: 6,
          borderWidth: 1,
        }))
        .filter((dataset) => dataset.data.some((value) => value > 0)),
    };
  }

  // Helper function to safely initialize charts
  function initializeChartIfExists(elementId, chartType, data, options) {
    const element = document.getElementById(elementId);
    if (element) {
      return new Chart(element.getContext("2d"), {
        type: chartType,
        data: data,
        options: options,
      });
    }
    return null;
  }

  function initializeSingleSeriesBarChart(canvasId, dataElementId) {
    const dataElement = document.getElementById(dataElementId);
    if (!dataElement) {
      return null;
    }

    const rawData = JSON.parse(dataElement.textContent || "null");
    if (!rawData || !rawData.labels || rawData.labels.length === 0) {
      return null;
    }

    const chartOptions = JSON.parse(JSON.stringify(barChartConfig));
    chartOptions.scales.x.stacked = false;
    chartOptions.scales.y.stacked = false;
    if (chartOptions.plugins && chartOptions.plugins.legend) {
      chartOptions.plugins.legend.display = false;
    }

    return initializeChartIfExists(
      canvasId,
      "bar",
      processBarData(rawData),
      chartOptions,
    );
  }

  // Ensure the copied score chart wrapper matches Activity History height
  function matchScoreCopyHeight() {
    const activityEl = document.getElementById("activityHistory");
    const scoreCopyWrapper = document.getElementById("scoreCopyWrapper");
    const scoreCanvasWrapper = document.getElementById("scoreCopyCanvasWrapper");
    const scoreCanvas = document.getElementById("scoreStackedChartCopy");

    if (!scoreCopyWrapper || !scoreCanvasWrapper) return 0;

    // If Activity History is hidden (stacked view), fall back to a generous baseline height
    const minHeight = 320; // ~2x the default 150px canvas height
    const activityHeight = activityEl
      ? Math.max(activityEl.clientHeight || 0, activityEl.offsetHeight || 0)
      : 0;
    const desiredHeight = Math.max(
      minHeight,
      activityHeight ? Math.round(activityHeight * 2) : 0
    );

    scoreCopyWrapper.style.minHeight = desiredHeight + "px";
    scoreCanvasWrapper.style.minHeight = desiredHeight + "px";
    scoreCanvasWrapper.style.height = desiredHeight + "px";

    // Ensure the canvas element fills its parent (Chart.js may set inline size attributes)
    if (scoreCanvas) {
      scoreCanvas.style.height = "100%";
      scoreCanvas.style.width = "100%";
      scoreCanvas.style.minHeight = desiredHeight + "px";
      scoreCanvas.height = desiredHeight;
    }

    return desiredHeight;
  }

  // Create Media Type Distribution Chart
  const mediaTypeDistributionElement = document.getElementById(
    "media_type_distribution"
  );
  if (mediaTypeDistributionElement) {
    const mediaTypeData = JSON.parse(mediaTypeDistributionElement.textContent);
    initializeChartIfExists(
      "mediaTypeChart",
      "pie",
      mediaTypeData,
      pieChartConfig
    );
  }

  // Create Status Distribution Chart
  const statusPieChartElement = document.getElementById(
    "status_pie_chart_data"
  );
  if (statusPieChartElement) {
    const statusPieData = JSON.parse(statusPieChartElement.textContent);
    initializeChartIfExists(
      "statusChart",
      "pie",
      statusPieData,
      pieChartConfig
    );
  }

  // Create Status Stacked Bar Chart
  const statusDistributionElement = document.getElementById(
    "status_distribution"
  );
  if (statusDistributionElement) {
    const statusData = JSON.parse(statusDistributionElement.textContent);
    initializeChartIfExists(
      "statusStackedChart",
      "bar",
      processBarData(statusData),
      barChartConfig
    );
  }

  // Create Score Stacked Bar Chart
  const scoreDistributionElement =
    document.getElementById("score_distribution");
  if (scoreDistributionElement) {
    const scoreData = JSON.parse(scoreDistributionElement.textContent);
    const scoreChartOptions = JSON.parse(JSON.stringify(barChartConfig)); // Deep clone

    // Add score-specific configurations
    scoreChartOptions.scales.x.title = {
      display: true,
      text: "Score",
      color: "#D1D5DB",
      padding: { top: 10, bottom: 0 },
    };

    scoreChartOptions.scales.y.title = {
      display: true,
      text: "Number of Items",
      color: "#D1D5DB",
      padding: { top: 0, left: 10 },
    };

    scoreChartOptions.plugins.title = {
      display: true,
      text: `Average Score: ${scoreData.average_score} (${
        scoreData.total_scored
      } ${scoreData.total_scored === 1 ? "item" : "items"})`,
      color: "#D1D5DB",
      padding: { bottom: 10 },
      font: { size: 14 },
    };

    // Ensure tooltip is properly configured for score chart
    scoreChartOptions.plugins.tooltip = {
      enabled: false,
      mode: "index",
      intersect: false,
      external: customBarTooltip,
    };

    initializeChartIfExists(
      "scoreStackedChart",
      "bar",
      processBarData(scoreData),
      scoreChartOptions
    );
    // Ensure copy wrapper is sized to match Activity History BEFORE initializing the copy
    matchScoreCopyHeight();

    // Debug: log element presence and sizes to help diagnose blank chart issues
    try {
      const activityEl = document.getElementById("activityHistory");
      const copyWrapper = document.getElementById("scoreCopyWrapper");
      const copyCanvasWrapper = document.getElementById("scoreCopyCanvasWrapper");
      const copyCanvas = document.getElementById("scoreStackedChartCopy");
      console.debug("[stats] activityEl:", !!activityEl, "height:", activityEl ? activityEl.clientHeight : null);
      console.debug("[stats] copyWrapper:", !!copyWrapper, "minHeight:", copyWrapper ? copyWrapper.style.minHeight : null);
      console.debug("[stats] copyCanvasWrapper:", !!copyCanvasWrapper, "height:", copyCanvasWrapper ? copyCanvasWrapper.clientHeight : null);
      console.debug("[stats] copyCanvas:", !!copyCanvas, "clientH/clientW:", copyCanvas ? [copyCanvas.clientHeight, copyCanvas.clientWidth] : null);
    } catch (e) {
      // swallow debug errors
      console.debug("[stats] debug error", e);
    }

    // Prefer a daily-hours dataset for the copy (if provided by backend)
    const dailyHoursEl = document.getElementById("daily_hours_by_media_type");
    if (dailyHoursEl) {
      const dailyData = JSON.parse(dailyHoursEl.textContent || "null");
      if (dailyData && dailyData.labels && dailyData.labels.length > 0 && dailyData.datasets && dailyData.datasets.length > 0) {
        // Determine bucket size based on selected date range
        let startIso = null;
        let endIso = null;
        try {
          const startEl = document.getElementById("stats_start_date");
          const endEl = document.getElementById("stats_end_date");
          startIso = startEl ? JSON.parse(startEl.textContent || '""') : null;
          endIso = endEl ? JSON.parse(endEl.textContent || '""') : null;
        } catch (e) {
          console.debug('[stats] failed to read start/end JSON', e);
        }

        function chooseBucket(startIso, endIso, labels) {
          // Choose a bucket (day/week/month/year) by finding the
          // coarsest granularity that keeps the number of bars
          // reasonably small (target ~36 bars).
          // If start/end ISO are not provided (All Time), try to infer
          // them from the provided labels array (first/last date strings).
          let startIsoLocal = startIso;
          let endIsoLocal = endIso;
          if ((!startIsoLocal || !endIsoLocal) && Array.isArray(labels) && labels.length) {
            startIsoLocal = labels[0];
            endIsoLocal = labels[labels.length - 1];
          }
          if (!startIsoLocal || !endIsoLocal) return 'month';
          const start = new Date(startIsoLocal);
          const end = new Date(endIsoLocal);
          const msPerDay = 24 * 60 * 60 * 1000;
          const spanDays = Math.ceil((end - start) / msPerDay) + 1;

          const maxBars = 36;

          // Day: one label per day
          if (spanDays <= 31) return 'day';

          // Week: one label per ISO week (approx 7 days)
          const spanWeeks = Math.ceil(spanDays / 7);
          if (spanWeeks <= maxBars) return 'week';

          // Month: compute month diff inclusive
          const spanMonths = (end.getFullYear() - start.getFullYear()) * 12 + (end.getMonth() - start.getMonth()) + 1;
          if (spanMonths <= maxBars) return 'month';

          // Otherwise fall back to years
          return 'year';
        }

        function getWeekStartIso(d) {
          const date = new Date(d);
          // ISO week start: Monday
          const day = date.getDay(); // 0 Sun .. 6 Sat
          const diff = (day + 6) % 7; // days since Monday
          const wk = new Date(date);
          wk.setDate(date.getDate() - diff);
          wk.setHours(0,0,0,0);
          return wk.toISOString().slice(0,10);
        }

        function getMonthIso(d) {
          const date = new Date(d);
          return date.toISOString().slice(0,7); // YYYY-MM
        }

        function getYearIso(d) {
          const date = new Date(d);
          return String(date.getFullYear());
        }

        function formatBucketLabel(bucket, key, startIso, endIso) {
          const nowYear = new Date().getFullYear();
          let startYear = null;
          let endYear = null;
          try {
            if (startIso) startYear = new Date(startIso).getFullYear();
            if (endIso) endYear = new Date(endIso).getFullYear();
          } catch (e) {
            // ignore
          }

          if (bucket === 'day') {
            const d = new Date(key);
            const opts = { month: 'short', day: 'numeric' };
            // include year if span crosses years or not current year
            if (startYear && endYear && startYear !== endYear) {
              opts.year = 'numeric';
            } else if (d.getFullYear() !== nowYear) {
              opts.year = 'numeric';
            }
            return d.toLocaleDateString(navigator.language || 'en-US', opts);
          }

          if (bucket === 'week') {
            // key is ISO date for week start (YYYY-MM-DD)
            const d = new Date(key);
            const opts = { month: 'short', day: 'numeric' };
            if (startYear && endYear && startYear !== endYear) {
              opts.year = 'numeric';
            } else if (d.getFullYear() !== nowYear) {
              opts.year = 'numeric';
            }
            // Show a short date for the week (no "Week of" prefix)
            return d.toLocaleDateString(navigator.language || 'en-US', opts);
          }

          if (bucket === 'month') {
            // key is YYYY-MM
            const [yy, mm] = key.split('-');
            const date = new Date(`${key}-01`);
            // If the selected range is within the current year, show full month name only
            if (startYear && endYear && startYear === endYear && startYear === nowYear) {
              return date.toLocaleDateString(navigator.language || 'en-US', { month: 'long' });
            }
            // Otherwise show abbreviated month + year
            return date.toLocaleDateString(navigator.language || 'en-US', { month: 'short', year: 'numeric' });
          }

          if (bucket === 'year') {
            return String(key);
          }

          return key;
        }

        function aggregateDailyToBucket(dailyData, bucket) {
          const labels = dailyData.labels || [];

          // If we're using daily buckets, format the labels but keep the data as-is
          if (bucket === 'day') {
            const newLabels = labels.map((k) => formatBucketLabel('day', k, startIso, endIso));
            const newDatasets = dailyData.datasets.map((ds) => ({
              label: ds.label,
              data: ds.data.map((v) => Number(v) || 0),
              background_color: ds.background_color || ds.backgroundColor || ds.backgroundColor,
            }));

            return { labels: newLabels, datasets: newDatasets };
          }

          const bucketMap = new Map();

          labels.forEach((lbl, idx) => {
            let key;
            if (bucket === 'week') key = getWeekStartIso(lbl);
            else if (bucket === 'month') key = getMonthIso(lbl);
            else if (bucket === 'year') key = getYearIso(lbl);
            else key = lbl;

            if (!bucketMap.has(key)) {
              bucketMap.set(key, Array(dailyData.datasets.length).fill(0));
            }

            dailyData.datasets.forEach((ds, dsIndex) => {
              const value = Number(ds.data[idx]) || 0;
              const arr = bucketMap.get(key);
              arr[dsIndex] = +(arr[dsIndex] + value).toFixed(4);
            });
          });

          const rawKeys = Array.from(bucketMap.keys()).sort();
          const newLabels = rawKeys.map((k) => formatBucketLabel(bucket, k, startIso, endIso));
          const newDatasets = dailyData.datasets.map((ds, i) => ({
            label: ds.label,
            data: rawKeys.map((k) => bucketMap.get(k)[i] || 0),
            background_color: ds.background_color || ds.backgroundColor || ds.backgroundColor,
          }));

          return { labels: newLabels, datasets: newDatasets };
        }

  const bucket = chooseBucket(startIso, endIso, dailyData.labels);
        const aggregated = aggregateDailyToBucket(dailyData, bucket);
        const dailyOptions = JSON.parse(JSON.stringify(barChartConfig));
        dailyOptions.scales.x.stacked = true;
        dailyOptions.scales.y.stacked = true;
        // Remove x-axis title for the copy chart (we use the page heading instead)
        if (dailyOptions.scales && dailyOptions.scales.x) {
          dailyOptions.scales.x.title = { display: false };
        }
        dailyOptions.scales.y.title = {
          display: true,
          text: "Hours",
          color: "#D1D5DB",
          padding: { top: 0, left: 10 },
        };
        // Don't add an in-chart title for the copy chart; the page heading above
        // already displays "Played Hours by Media Type" in larger type.
        dailyOptions.plugins.tooltip = {
          enabled: false,
          mode: "index",
          intersect: false,
          external: customBarTooltip,
        };

        const dailyChart = initializeChartIfExists(
          "scoreStackedChartCopy",
          "bar",
          processBarData(aggregated),
          dailyOptions
        );

        if (dailyChart && typeof dailyChart.resize === "function") {
          dailyChart.resize();
        }
      }
    } else {
      // Fallback: initialize copy using score distribution data (legacy behavior)
      // Use the score chart options as a base but override title for the copy
      const fallbackOptions = JSON.parse(JSON.stringify(scoreChartOptions));
      fallbackOptions.plugins = fallbackOptions.plugins || {};
      // Don't set an in-chart title for the fallback; the page heading is used

      const scoreCopyChart = initializeChartIfExists(
        "scoreStackedChartCopy",
        "bar",
        processBarData(scoreData),
        fallbackOptions
      );

      if (scoreCopyChart && typeof scoreCopyChart.resize === "function") {
        scoreCopyChart.resize();
      }
    }
  }

  initializeSingleSeriesBarChart(
    "tvEpisodesByYearChart",
    "tv_episodes_by_year"
  );
  initializeSingleSeriesBarChart(
    "tvEpisodesByMonthChart",
    "tv_episodes_by_month"
  );
  initializeSingleSeriesBarChart(
    "tvEpisodesByWeekdayChart",
    "tv_episodes_by_weekday"
  );
  initializeSingleSeriesBarChart(
    "tvEpisodesByTimeChart",
    "tv_episodes_by_time"
  );

  initializeSingleSeriesBarChart(
    "moviePlaysByYearChart",
    "movie_plays_by_year"
  );
  initializeSingleSeriesBarChart(
    "moviePlaysByMonthChart",
    "movie_plays_by_month"
  );
  initializeSingleSeriesBarChart(
    "moviePlaysByWeekdayChart",
    "movie_plays_by_weekday"
  );
  initializeSingleSeriesBarChart(
    "moviePlaysByTimeChart",
    "movie_plays_by_time"
  );

  // Initial sizing and on resize for the copied score chart wrapper
  matchScoreCopyHeight();
  window.addEventListener("resize", function () {
    // Debounce-ish
    clearTimeout(window._scoreCopyResizeTimer);
    window._scoreCopyResizeTimer = setTimeout(matchScoreCopyHeight, 120);
  });
});
