from datetime import date, datetime
from zoneinfo import ZoneInfo

from events.models import SentinelDatetime


def _build_sentinel_datetime(value_date):
    """Return a UTC sentinel datetime for a release date."""
    return datetime(
        value_date.year,
        value_date.month,
        value_date.day,
        SentinelDatetime.HOUR,
        SentinelDatetime.MINUTE,
        SentinelDatetime.SECOND,
        SentinelDatetime.MICROSECOND,
        tzinfo=ZoneInfo("UTC"),
    )


def date_parser(date_value):
    """Parse partial date strings or date objects into a sentinel datetime."""
    if isinstance(date_value, datetime):
        return _build_sentinel_datetime(date_value.date())

    if isinstance(date_value, date):
        return _build_sentinel_datetime(date_value)

    year_only_parts = 1
    year_month_parts = 2
    default_month_day = "-01-01"
    default_day = "-01"
    date_str = str(date_value)
    parts = date_str.split("-")
    if len(parts) == year_only_parts:
        date_str += default_month_day
    elif len(parts) == year_month_parts:
        date_str += default_day

    return _build_sentinel_datetime(date.fromisoformat(date_str))
