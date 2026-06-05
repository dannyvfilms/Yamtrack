"""stats_activity.py — Timeline, activity calendar, and streak calculations.

Contains all datetime-heavy logic for the activity heatmap, month/year
timeline, streak computation, and day-of-week statistics.  Only external
dependencies are Django ORM, dateutil, and stats_utils.
"""
import calendar
import datetime
import logging
from collections import defaultdict

from dateutil.relativedelta import relativedelta
from django.apps import apps
from django.utils import timezone

from app.models import BasicMedia, MediaTypes
from app.stats_utils import _iter_media_list
from users.models import WeekStartDayChoices

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

def get_timeline(user_media):
    """Build a timeline of media consumption organized by month-year."""
    timeline = defaultdict(list)

    # Process each media type
    for media_type, queryset in user_media.items():
        # If we have TV objects but seasons are hidden from the sidebar,
        # the TV queryset will still include prefetched seasons. Add
        # seasons from TV objects to the timeline so they appear here.
        if media_type == MediaTypes.TV.value:
            if MediaTypes.SEASON.value not in user_media:
                for tv in queryset:
                    seasons_qs = getattr(tv, "seasons", None)
                    if seasons_qs is None:
                        continue
                    for media in seasons_qs.all():
                        # media here is a Season instance
                        local_start_date = (
                            timezone.localdate(media.start_date) if media.start_date else None
                        )
                        local_end_date = (
                            timezone.localdate(media.end_date) if media.end_date else None
                        )

                        if media.start_date and media.end_date:
                            # add media to all months between start and end
                            current_date = local_start_date
                            while current_date <= local_end_date:
                                year = current_date.year
                                month = current_date.month
                                month_name = calendar.month_name[month]
                                month_year = f"{month_name} {year}"

                                timeline[month_year].append(media)

                                # Move to next month
                                current_date += relativedelta(months=1)
                                current_date = current_date.replace(day=1)
                        elif media.start_date:
                            # If only start date, add to the start month
                            year = local_start_date.year
                            month = local_start_date.month
                            month_name = calendar.month_name[month]
                            month_year = f"{month_name} {year}"

                            timeline[month_year].append(media)
                        elif media.end_date:
                            # If only end date, add to the end month
                            year = local_end_date.year
                            month = local_end_date.month
                            month_name = calendar.month_name[month]
                            month_year = f"{month_name} {year}"

                            timeline[month_year].append(media)
            # TV timeline activity is represented by seasons, not TV shells.
            continue

        for media in _iter_media_list(queryset):
            # Grouped anime items are TV model instances with seasons; expand to season level
            if hasattr(media, "seasons"):
                seasons_qs = getattr(media, "seasons", None)
                if seasons_qs is None:
                    continue
                for season in seasons_qs.all():
                    _tl_local_start = (
                        timezone.localdate(season.start_date) if season.start_date else None
                    )
                    _tl_local_end = (
                        timezone.localdate(season.end_date) if season.end_date else None
                    )
                    if season.start_date and season.end_date:
                        _cur = _tl_local_start
                        while _cur <= _tl_local_end:
                            timeline[f"{calendar.month_name[_cur.month]} {_cur.year}"].append(season)
                            _cur += relativedelta(months=1)
                            _cur = _cur.replace(day=1)
                    elif season.start_date:
                        timeline[f"{calendar.month_name[_tl_local_start.month]} {_tl_local_start.year}"].append(season)
                    elif season.end_date:
                        timeline[f"{calendar.month_name[_tl_local_end.month]} {_tl_local_end.year}"].append(season)
                continue

            local_start_date = (
                timezone.localdate(media.start_date) if media.start_date else None
            )
            local_end_date = (
                timezone.localdate(media.end_date) if media.end_date else None
            )

            if media.start_date and media.end_date:
                # add media to all months between start and end
                current_date = local_start_date
                while current_date <= local_end_date:
                    year = current_date.year
                    month = current_date.month
                    month_name = calendar.month_name[month]
                    month_year = f"{month_name} {year}"

                    timeline[month_year].append(media)

                    # Move to next month
                    current_date += relativedelta(months=1)
                    current_date = current_date.replace(day=1)
            elif media.start_date:
                # If only start date, add to the start month
                year = local_start_date.year
                month = local_start_date.month
                month_name = calendar.month_name[month]
                month_year = f"{month_name} {year}"

                timeline[month_year].append(media)
            elif media.end_date:
                # If only end date, add to the end month
                year = local_end_date.year
                month = local_end_date.month
                month_name = calendar.month_name[month]
                month_year = f"{month_name} {year}"

                timeline[month_year].append(media)

    # Convert to sorted dictionary with media sorted by start date
    # Create a list sorted by year and month in reverse order
    sorted_items = []
    for month_year, media_list in timeline.items():
        month_name, year_str = month_year.split()
        year = int(year_str)
        month = list(calendar.month_name).index(month_name)
        sorted_items.append((month_year, media_list, year, month))

    # Sort by year and month in reverse chronological order
    sorted_items.sort(key=lambda x: (x[2], x[3]), reverse=True)

    # Create the final result dictionary
    result = {}
    for month_year, media_list, _, _ in sorted_items:
        # Sort the media list using our custom sort key
        result[month_year] = sorted(media_list, key=time_line_sort_key, reverse=True)
    return result


def time_line_sort_key(media):
    """Sort media items in the timeline."""
    if media.end_date is not None:
        return timezone.localdate(media.end_date)
    return timezone.localdate(media.start_date)


# ---------------------------------------------------------------------------
# Activity calendar
# ---------------------------------------------------------------------------

def _convert_chart_to_day_minutes(daily_hours_data):
    """Convert Chart.js formatted daily hours data to day_minutes_by_type format.

    Args:
        daily_hours_data: {"labels": ["2025-01-01", ...], "datasets": [...]}

    Returns:
        Dict mapping media_type -> {date_iso_str -> minutes}
    """
    day_minutes_by_type = {}
    labels = daily_hours_data.get("labels", [])
    datasets = daily_hours_data.get("datasets", [])

    for dataset in datasets:
        # Use a generic key since we just need total minutes per day
        media_type = dataset.get("label", "unknown")
        data = dataset.get("data", [])

        if media_type not in day_minutes_by_type:
            day_minutes_by_type[media_type] = {}

        for i, hours in enumerate(data):
            if i < len(labels):
                date_str = labels[i]
                # Convert hours back to minutes
                minutes = float(hours) * 60 if hours else 0
                day_minutes_by_type[media_type][date_str] = minutes

    return day_minutes_by_type


def get_activity_data(user, start_date, end_date, daily_hours_data=None):
    """Get daily activity counts for the activity calendar.

    Args:
        user: The user to get activity data for
        start_date: Start of the date range
        end_date: End of the date range
        daily_hours_data: Optional Chart.js formatted daily hours data from
            get_daily_hours_by_media_type(). If provided, used for more accurate
            "most active day" calculation.
    """
    if end_date is None:
        end_date = timezone.localtime()

    week_start_sunday = user.week_start_day == WeekStartDayChoices.SUNDAY
    start_date_aligned = get_aligned_week_start(start_date, week_start_sunday=week_start_sunday)

    combined_data = get_filtered_historical_data(start_date_aligned, end_date, user)

    # update start_date values from historical records if not provided
    if start_date is None:
        dates = [item["date"] for item in combined_data]
        start_date = datetime.datetime.combine(
            min(dates) if dates else timezone.localdate(),
            datetime.time.min,
        )
        start_date_aligned = get_aligned_week_start(start_date, week_start_sunday=week_start_sunday)

    # Aggregate counts by date
    date_counts = {}
    for item in combined_data:
        date = item["date"]
        date_counts[date] = date_counts.get(date, 0) + item["count"]

    date_range = [
        start_date_aligned.date() + datetime.timedelta(days=x)
        for x in range((end_date.date() - start_date_aligned.date()).days + 1)
    ]

    # Calculate most active day using daily hours data if available
    has_chart_data = (
        daily_hours_data
        and daily_hours_data.get("labels")
        and daily_hours_data.get("datasets")
    )
    if has_chart_data:
        # Convert Chart.js format to day_minutes_by_type format
        day_minutes_by_type = _convert_chart_to_day_minutes(daily_hours_data)
        most_active_day, day_percentage = calculate_most_active_weekday(
            day_minutes_by_type,
            date_range,
        )
    else:
        # Fallback to legacy calculation for backward compatibility
        most_active_day, day_percentage = calculate_day_of_week_stats(
            date_counts,
            start_date.date(),
        )

    streaks = calculate_streak_details(
        date_counts,
        end_date.date(),
    )

    # Create complete date range including padding days
    activity_data = [
        {
            "date": current_date.strftime("%Y-%m-%d"),
            "count": date_counts.get(current_date, 0),
            "level": get_level(date_counts.get(current_date, 0)),
        }
        for current_date in date_range
    ]

    # Format data into calendar weeks
    calendar_weeks = [activity_data[i : i + 7] for i in range(0, len(activity_data), 7)]

    base_weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if week_start_sunday:
        weekday_labels = [base_weekdays[6], *base_weekdays[:6]]
    else:
        weekday_labels = base_weekdays

    # Generate months list with their week-start day counts
    months = []
    mondays_per_month = []
    current_month = date_range[0].strftime("%b")
    monday_count = 0
    week_start_weekday = 6 if week_start_sunday else 0

    for current_date in date_range:
        if current_date.weekday() == week_start_weekday:
            month = current_date.strftime("%b")

            if current_month != month:
                if current_month is not None:
                    if monday_count > 1:
                        months.append(current_month)
                        mondays_per_month.append(monday_count)
                    else:
                        months.append("")
                        mondays_per_month.append(monday_count)
                current_month = month
                monday_count = 0

            monday_count += 1
    # For the last month
    if monday_count > 1:
        months.append(current_month)
        mondays_per_month.append(monday_count)

    return {
        "calendar_weeks": calendar_weeks,
        "months": list(zip(months, mondays_per_month, strict=False)),
        "weekday_labels": weekday_labels,
        "stats": {
            "most_active_day": most_active_day,
            "most_active_day_percentage": day_percentage,
            "current_streak": streaks["current_streak"],
            "longest_streak": streaks["longest_streak"],
            "longest_streak_start": streaks["longest_streak_start"],
            "longest_streak_end": streaks["longest_streak_end"],
        },
    }


def get_aligned_week_start(datetime_obj, *, week_start_sunday=False):
    """Get the week-start day of the week containing the given date."""
    if datetime_obj is None:
        return None

    target_weekday = 6 if week_start_sunday else 0  # Sun=6, Mon=0
    days_to_subtract = (datetime_obj.weekday() - target_weekday) % 7
    return datetime_obj - datetime.timedelta(days=days_to_subtract)


def get_level(count):
    """Calculate intensity level (0-4) based on count."""
    thresholds = [0, 3, 6, 9]
    for i, threshold in enumerate(thresholds):
        if count <= threshold:
            return i
    return 4


def get_filtered_historical_data(start_date, end_date, user):
    """Return [{"date": datetime.date, "count": int}]."""
    historical_models = BasicMedia.objects.get_historical_models()
    local_tz = timezone.get_current_timezone()

    day_buckets = defaultdict(int)

    for model_name in historical_models:
        model = apps.get_model("app", model_name)

        qs = model.objects.filter(history_user_id=user)

        if start_date:
            qs = qs.filter(history_date__gte=start_date)
        if end_date:
            qs = qs.filter(history_date__lte=end_date)

        # We only need the timestamp, stream results to keep memory usage flat
        for ts in qs.values_list("history_date", flat=True).iterator(chunk_size=2_000):
            aware_ts = timezone.localtime(ts, local_tz)

            day_buckets[aware_ts.date()] += 1

    combined_data = [
        {"date": day, "count": count} for day, count in day_buckets.items()
    ]

    logger.info("%s - built historical data (%s rows)", user, len(combined_data))
    return combined_data


# ---------------------------------------------------------------------------
# Day-of-week and streak statistics
# ---------------------------------------------------------------------------

def calculate_day_of_week_stats(date_counts, start_date):
    """Calculate the most active day of the week based on activity frequency.

    Returns the day name and its percentage of total activity.
    """
    # Initialize counters for each day of the week
    day_counts = defaultdict(int)
    total_active_days = 0

    # Count occurrences of each day of the week where activity happened
    for date in date_counts:
        if date < start_date:
            continue
        if date_counts[date] > 0:
            day_name = date.strftime("%A")  # Get full day name
            day_counts[day_name] += 1
            total_active_days += 1

    if not total_active_days:
        return None, 0

    # Find the most active day
    most_active_day = max(day_counts.items(), key=lambda x: x[1])
    percentage = (most_active_day[1] / total_active_days) * 100

    return most_active_day[0], round(percentage)


def calculate_most_active_weekday(day_minutes_by_type, day_list):
    """Calculate most active weekday based on total consumption minutes.

    Uses the same data source as 'Played Hours by Media Type' chart to ensure
    the most active day is calculated from the same filtered data range.

    Args:
        day_minutes_by_type: Dict mapping media_type -> {date_iso_str -> minutes}
        day_list: List of date objects in the filtered range

    Returns:
        (weekday_name, percentage) or (None, 0) if no data.
    """
    weekday_minutes = defaultdict(float)

    for day in day_list:
        day_str = day.isoformat()
        day_total = 0
        for minutes_map in day_minutes_by_type.values():
            day_total += minutes_map.get(day_str, 0)
        if day_total > 0:
            weekday_name = day.strftime("%A")
            weekday_minutes[weekday_name] += day_total

    if not weekday_minutes:
        return None, 0

    total_minutes = sum(weekday_minutes.values())
    most_active = max(weekday_minutes.items(), key=lambda x: x[1])
    percentage = (most_active[1] / total_minutes) * 100

    return most_active[0], round(percentage)


def calculate_streak_details(date_counts, end_date):
    """Return current/longest streak counts plus their date ranges."""
    active_dates = sorted(
        [date for date, count in date_counts.items() if count > 0],
    )

    if not active_dates:
        return {
            "current_streak": 0,
            "current_streak_start": None,
            "current_streak_end": None,
            "longest_streak": 0,
            "longest_streak_start": None,
            "longest_streak_end": None,
        }

    active_set = set(active_dates)

    longest_streak = 1
    longest_start = active_dates[0]
    longest_end = active_dates[0]

    streak_start = active_dates[0]
    prev_date = active_dates[0]

    for current_date in active_dates[1:]:
        if (current_date - prev_date).days == 1:
            prev_date = current_date
            continue

        streak_len = (prev_date - streak_start).days + 1
        if streak_len > longest_streak or (streak_len == longest_streak and prev_date > longest_end):
            longest_streak = streak_len
            longest_start = streak_start
            longest_end = prev_date

        streak_start = current_date
        prev_date = current_date

    streak_len = (prev_date - streak_start).days + 1
    if streak_len > longest_streak or (streak_len == longest_streak and prev_date > longest_end):
        longest_streak = streak_len
        longest_start = streak_start
        longest_end = prev_date

    if end_date in active_set:
        current_end = end_date
        current_start = current_end
        while (current_start - datetime.timedelta(days=1)) in active_set:
            current_start -= datetime.timedelta(days=1)
        current_streak = (current_end - current_start).days + 1
    else:
        current_streak = 0
        current_start = None
        current_end = None

    return {
        "current_streak": current_streak,
        "current_streak_start": current_start,
        "current_streak_end": current_end,
        "longest_streak": longest_streak,
        "longest_streak_start": longest_start,
        "longest_streak_end": longest_end,
    }


def calculate_streaks(date_counts, end_date):
    """Calculate current and longest activity streaks."""
    streaks = calculate_streak_details(date_counts, end_date)
    return streaks["current_streak"], streaks["longest_streak"]
