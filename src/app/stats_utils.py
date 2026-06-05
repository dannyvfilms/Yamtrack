"""stats_utils.py — Shared pure utilities for the statistics layer.

Contains small helpers that are used across multiple stats_* modules and
some non-stats views (tag_views, media_details_views, etc.).  No database
writes; no calls to external providers.
"""
import datetime

from django.utils import timezone

from app.models import MediaTypes

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

MEDIA_TYPE_HOURS_ORDER = [
    MediaTypes.TV.value,
    MediaTypes.MOVIE.value,
    MediaTypes.GAME.value,
    MediaTypes.PODCAST.value,
    MediaTypes.BOARDGAME.value,
    MediaTypes.ANIME.value,
    MediaTypes.MUSIC.value,
]


# ---------------------------------------------------------------------------
# Combined-queryset helpers (anime bucket)
# ---------------------------------------------------------------------------

class _CombinedMediaBucket:
    """Wraps two querysets (e.g. flat anime + grouped anime TV) into one iterable bucket."""

    def __init__(self, *querysets):
        self._querysets = querysets

    def __iter__(self):
        for qs in self._querysets:
            yield from qs

    def exists(self):
        return any(qs.exists() for qs in self._querysets)

    def first(self):
        for qs in self._querysets:
            obj = qs.first()
            if obj is not None:
                return obj
        return None

    def select_related(self, *args):
        return _CombinedMediaBucket(*(qs.select_related(*args) for qs in self._querysets))

    def count(self):
        return sum(qs.count() for qs in self._querysets)

    def values(self, *fields):
        # Returns a lazy iterator of dicts; used only to drive status/score counts
        return _CombinedValuesResult(self._querysets, fields)


class _CombinedValuesResult:
    """Wraps .values(...).annotate(count=Count('id')) results from multiple querysets."""

    def __init__(self, querysets, fields):
        self._querysets = querysets
        self._fields = fields
        self._annotation = None
        self._annotation_field = None

    def annotate(self, **kwargs):
        self._annotation = kwargs
        if kwargs:
            self._annotation_field = next(iter(kwargs))
        return self

    def __iter__(self):
        if not self._annotation:
            for qs in self._querysets:
                yield from qs.values(*self._fields)
            return
        # Merge counts from each queryset
        merged: dict = {}
        for qs in self._querysets:
            for row in qs.values(*self._fields).annotate(**self._annotation):
                key = tuple(row[f] for f in self._fields)
                if key not in merged:
                    merged[key] = dict(zip(self._fields, key))
                    merged[key][self._annotation_field] = 0
                merged[key][self._annotation_field] += row[self._annotation_field]
        yield from merged.values()


def _iter_media_list(media_list):
    """Iterate over a queryset or a _CombinedMediaBucket."""
    if isinstance(media_list, _CombinedMediaBucket):
        yield from media_list
    else:
        yield from media_list


def _infer_user_from_user_media(user_media):
    """Best-effort helper to derive user from user_media querysets."""
    if not user_media:
        return None

    for media_list in user_media.values():
        if media_list is None:
            continue
        # media_list may be a list of querysets (combined anime bucket)
        first_media = None
        if isinstance(media_list, _CombinedMediaBucket):
            first_media = media_list.first()
        else:
            try:
                first_media = media_list.first()
            except (AttributeError, TypeError):
                try:
                    first_media = next(iter(media_list), None)
                except TypeError:
                    first_media = None

        if first_media is not None and hasattr(first_media, "user"):
            return first_media.user

    return None


# ---------------------------------------------------------------------------
# Runtime-string parsing
# ---------------------------------------------------------------------------

def parse_runtime_to_minutes(runtime_str):
    """Parse runtime string (e.g., '45m', '1h 30m', '2h', '12 min') to total minutes."""
    if not runtime_str:
        return None

    # Handle case where runtime_str is already an integer (minutes)
    if isinstance(runtime_str, int):
        return runtime_str

    # Convert to string if it's not already
    if not isinstance(runtime_str, str):
        runtime_str = str(runtime_str)

    try:
        # Handle MAL format: "12 min" (note the space before "min")
        if "h" in runtime_str and "min" in runtime_str:
            # Format like "1h 30min" or "2h 15min"
            parts = runtime_str.split()
            if len(parts) == 2:  # "1h 30min"
                hours = int(parts[0].replace("h", ""))
                minutes = int(parts[1].replace("min", ""))
                return hours * 60 + minutes
            return None
        if "h" in runtime_str and "m" in runtime_str:
            # Format like "1h 30m" or "2h 15m" (TMDB format)
            parts = runtime_str.split()
            if len(parts) == 2:  # "1h 30m"
                hours = int(parts[0].replace("h", ""))
                minutes = int(parts[1].replace("m", ""))
                return hours * 60 + minutes
            return None
        if "h" in runtime_str:
            # Format like "2h"
            hours = int(runtime_str.replace("h", ""))
            return hours * 60
        if "min" in runtime_str:
            # Format like "45min" or "12 min" (MAL format)
            minutes = int(runtime_str.replace("min", "").replace(" ", ""))
            return minutes
        if "m" in runtime_str:
            # Format like "45m" (TMDB format)
            minutes = int(runtime_str.replace("m", ""))
            return minutes
        return None
    except (ValueError, AttributeError):
        return None


def _is_media_in_date_range(media, start_date, end_date):
    """Check if media is within the specified date range."""
    if not start_date or not end_date:
        return True

    if hasattr(media, "end_date") and media.end_date:
        return start_date <= media.end_date <= end_date
    if hasattr(media, "start_date") and media.start_date:
        return start_date <= media.start_date <= end_date

    return False


# ---------------------------------------------------------------------------
# Duration formatting
# ---------------------------------------------------------------------------

def _format_long_units(total_minutes):
    """Format minutes using the largest applicable units (mo/d/h/min)."""
    total_minutes = int(total_minutes)
    if total_minutes < 60:
        return f"{total_minutes}min"
    if total_minutes < 1440:  # < 24 h
        hours, mins = divmod(total_minutes, 60)
        return f"{hours}h {mins}min"
    MONTH = 43800  # 30 × 24 × 60
    DAY = 1440
    HOUR = 60
    months, r = divmod(total_minutes, MONTH)
    days, r = divmod(r, DAY)
    hours, mins = divmod(r, HOUR)
    parts = []
    if months:
        parts.append(f"{months}mo")
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}min")
    return " ".join(parts)


def _format_hours_minutes(total_minutes, duration_format="hours_minutes"):
    """Format total minutes into a duration string."""
    if total_minutes > 0:
        try:
            total_minutes = int(total_minutes)
        except (TypeError, ValueError):
            return "0h 0min"
        if duration_format == "long_units":
            return _format_long_units(total_minutes)
        hours, mins = divmod(total_minutes, 60)
        return f"{hours}h {mins}min"
    return "0h 0min"


# ---------------------------------------------------------------------------
# Activity datetime helpers
# ---------------------------------------------------------------------------

def _get_activity_datetime(media):
    """Return the most representative datetime for media activity."""
    for attr in ("end_date", "start_date", "created_at"):
        value = getattr(media, attr, None)
        if value:
            return value
    return None


def _localize_datetime(value):
    """Return the datetime converted to the current timezone if aware."""
    if value is None:
        return None

    if timezone.is_naive(value):
        return value
    return timezone.localtime(value)


def _get_entry_play_dates(entry):
    """Return set of local dates covered by a play entry."""
    dates = set()
    entry_start = getattr(entry, "start_date", None)
    entry_end = getattr(entry, "end_date", None)

    if entry_start or entry_end:
        start_local = _localize_datetime(entry_start) if entry_start else None
        end_local = _localize_datetime(entry_end) if entry_end else None

        if start_local and end_local:
            start_date = start_local.date()
            end_date = end_local.date()
            if end_date < start_date:
                end_date = start_date
            current = start_date
            while current <= end_date:
                dates.add(current)
                current += datetime.timedelta(days=1)
        else:
            single = start_local or end_local
            if single:
                dates.add(single.date())
        return dates

    activity_dt = _get_activity_datetime(entry)
    if activity_dt:
        activity_local = _localize_datetime(activity_dt)
        if activity_local:
            dates.add(activity_local.date())
    return dates


# ---------------------------------------------------------------------------
# Genre normalization
# ---------------------------------------------------------------------------

def _coerce_genre_list(value):
    """Normalize a genre field (string, dict, or list) into a list of strings."""
    def _coerce_one(v):
        if not v:
            return None
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            # Common shapes: {"name": "Jazz"} or {"tag": "jazz"}
            return v.get("name") or v.get("tag") or v.get("label")
        return str(v)

    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        coerced = _coerce_one(value)
        return [coerced] if coerced else []
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            coerced = _coerce_one(v)
            if coerced:
                out.append(coerced)
        return out
    coerced = _coerce_one(value)
    return [coerced] if coerced else []
