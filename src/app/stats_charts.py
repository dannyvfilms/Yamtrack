"""stats_charts.py — Chart.js dataset builders for the statistics layer.

Pure data-transform functions; no database access, no I/O.  All functions
return dicts ready to be serialised as Chart.js chart configs.
"""
import calendar
import datetime
from collections import Counter, defaultdict

from app.stats_utils import _localize_datetime


# ---------------------------------------------------------------------------
# Metric breakdown (rates over time)
# ---------------------------------------------------------------------------

def _compute_metric_breakdown(total_value, datetimes, start_date, end_date):
    """Return aggregate totals alongside per-year/month/day rates."""
    breakdown = {
        "total": total_value,
        "per_year": 0,
        "per_month": 0,
        "per_day": 0,
    }

    if total_value == 0 or not datetimes:
        return breakdown

    range_start = start_date or min(datetimes)
    range_end = end_date or max(datetimes)

    if range_start > range_end:
        range_start, range_end = range_end, range_start

    range_start = _localize_datetime(range_start)
    range_end = _localize_datetime(range_end)

    start_date_only = range_start.date()
    end_date_only = range_end.date()

    total_days = (end_date_only - start_date_only).days + 1
    if total_days <= 0:
        total_days = 1

    # Avoid exaggerated projections when the range is shorter than a month/year (e.g., new data)
    total_years = max(total_days / 365.25, 1)
    total_months = max(total_days / 30.4375, 1)

    breakdown["per_year"] = total_value / total_years if total_years else total_value
    breakdown["per_month"] = total_value / total_months if total_months else total_value
    breakdown["per_day"] = total_value / total_days if total_days else total_value

    return breakdown


# ---------------------------------------------------------------------------
# Single-series and grouped chart builders
# ---------------------------------------------------------------------------

def _build_single_series_chart(labels, values, color, dataset_label):
    """Return a Chart.js-friendly dataset for a single-series bar chart."""
    if not values or sum(values) == 0:
        return {"labels": [], "datasets": []}

    return {
        "labels": labels,
        "datasets": [
            {
                "label": dataset_label,
                "data": values,
                "background_color": color,
            },
        ],
    }


def _format_hour_label(hour):
    """Return a human-friendly label for an hour of day."""
    if hour == 0:
        return "12am"
    if hour < 12:
        return f"{hour}am"
    if hour == 12:
        return "12pm"
    return f"{hour - 12}pm"


def _build_media_charts(datetimes, color, dataset_label):
    """Build grouped chart datasets for the provided datetimes."""
    empty_chart = {"labels": [], "datasets": []}

    if not datetimes:
        return {
            "by_year": empty_chart,
            "by_month": empty_chart,
            "by_weekday": empty_chart,
            "by_time_of_day": empty_chart,
        }

    year_counts = Counter(dt.year for dt in datetimes)
    sorted_years = sorted(year_counts)
    year_labels = [str(year) for year in sorted_years]
    year_values = [year_counts[year] for year in sorted_years]

    month_counts = Counter(dt.month for dt in datetimes)
    month_labels = [calendar.month_abbr[i] for i in range(1, 13)]
    month_values = [month_counts.get(i, 0) for i in range(1, 13)]

    weekday_map = {
        0: "Mon",
        1: "Tue",
        2: "Wed",
        3: "Thu",
        4: "Fri",
        5: "Sat",
        6: "Sun",
    }
    weekday_order = [6, 0, 1, 2, 3, 4, 5]
    weekday_counts = Counter(dt.weekday() for dt in datetimes)
    weekday_labels = [weekday_map[index] for index in weekday_order]
    weekday_values = [weekday_counts.get(index, 0) for index in weekday_order]

    hour_counts = Counter(dt.hour for dt in datetimes)
    hour_labels = [_format_hour_label(hour) for hour in range(24)]
    hour_values = [hour_counts.get(hour, 0) for hour in range(24)]

    return {
        "by_year": _build_single_series_chart(
            year_labels,
            year_values,
            color,
            dataset_label,
        ),
        "by_month": _build_single_series_chart(
            month_labels,
            month_values,
            color,
            dataset_label,
        ),
        "by_weekday": _build_single_series_chart(
            weekday_labels,
            weekday_values,
            color,
            dataset_label,
        ),
        "by_time_of_day": _build_single_series_chart(
            hour_labels,
            hour_values,
            color,
            dataset_label,
        ),
    }


# ---------------------------------------------------------------------------
# Specialised distribution charts
# ---------------------------------------------------------------------------

def _build_completed_length_distribution_chart(values, unit_name, color):
    """Build chart showing distribution of completed item lengths."""
    empty_chart = {"labels": [], "datasets": []}
    if not values:
        return empty_chart

    values = [value for value in values if value and value > 0]
    if not values:
        return empty_chart

    # Define unit bands (completed length).
    bands = [
        (0, 50, "1-50"),
        (50, 100, "51-100"),
        (100, 200, "101-200"),
        (200, 300, "201-300"),
        (300, 500, "301-500"),
        (500, 800, "501-800"),
        (800, 1200, "801-1200"),
        (1200, float("inf"), "1200+"),
    ]

    band_counts = [0] * len(bands)
    for value in values:
        for i, (min_units, max_units, _) in enumerate(bands):
            if min_units < value <= max_units:
                band_counts[i] += 1
                break
        else:
            if value > bands[-1][0]:
                band_counts[-1] += 1

    labels = [label for _, _, label in bands]
    dataset_label = f"Completed {unit_name}s"
    return _build_single_series_chart(labels, band_counts, color, dataset_label)


def _build_release_year_chart(release_datetimes, color, dataset_label):
    """Build chart for items released per year."""
    empty_chart = {"labels": [], "datasets": []}
    if not release_datetimes:
        return empty_chart

    year_totals = defaultdict(int)
    for release_dt in release_datetimes:
        if not release_dt:
            continue
        if isinstance(release_dt, datetime.datetime):
            year_totals[release_dt.year] += 1
        else:
            try:
                year_totals[release_dt.year] += 1
            except AttributeError:
                continue

    if not year_totals:
        return empty_chart

    sorted_years = sorted(year_totals.keys())
    year_labels = [str(year) for year in sorted_years]
    year_values = [year_totals[year] for year in sorted_years]
    return _build_single_series_chart(year_labels, year_values, color, dataset_label)
