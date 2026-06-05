"""Serialize and deserialize history day/entry dicts for cache storage."""

from datetime import datetime

from django.utils import timezone

from app.history_entry_builders import (
    _serialize_album,
    _serialize_item,
    _serialize_show,
)


def _serialize_history_entry(entry):
    data = dict(entry)
    data["item"] = _serialize_item(data.get("item"))
    data["album"] = _serialize_album(data.get("album"))
    data["show"] = _serialize_show(data.get("show"))
    data.pop("episode_modal", None)
    played_at = data.get("played_at_local")
    if isinstance(played_at, datetime):
        data["played_at_local"] = played_at.isoformat()
    return data


def _deserialize_history_entry(entry):
    data = dict(entry)
    played_at = data.get("played_at_local")
    if isinstance(played_at, str):
        try:
            parsed = datetime.fromisoformat(played_at)
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            data["played_at_local"] = parsed
        except ValueError:
            data["played_at_local"] = None
    return data


def _serialize_history_day(day):
    date_value = day.get("date")
    if hasattr(date_value, "isoformat"):
        date_value = date_value.isoformat()
    return {
        "date": date_value,
        "weekday": day.get("weekday", ""),
        "date_display": day.get("date_display", ""),
        "entries": [_serialize_history_entry(entry) for entry in day.get("entries", [])],
        "total_minutes": day.get("total_minutes", 0),
        "total_runtime_display": day.get("total_runtime_display", "0min"),
    }


def _deserialize_history_day(day):
    date_value = day.get("date")
    if isinstance(date_value, str):
        try:
            date_value = datetime.strptime(date_value, "%Y-%m-%d").date()
        except ValueError:
            date_value = None
    return {
        "date": date_value,
        "weekday": day.get("weekday", ""),
        "date_display": day.get("date_display", ""),
        "entries": [_deserialize_history_entry(entry) for entry in day.get("entries", [])],
        "total_minutes": day.get("total_minutes", 0),
        "total_runtime_display": day.get("total_runtime_display", "0min"),
    }
