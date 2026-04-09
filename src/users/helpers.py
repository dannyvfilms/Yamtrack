import importlib
import json
import zoneinfo
from datetime import datetime, timedelta

import croniter
from django.utils import timezone


def _deserialize_task_result(result):
    """Return the stored task result in its native Python shape when possible."""
    if not isinstance(result, str):
        return result

    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return result


def _split_success_result(result_text):
    """Split a string success payload into summary and error details."""
    error_title = importlib.import_module("integrations.tasks").ERROR_TITLE.strip()
    parts = result_text.split(error_title)
    if len(parts) > 1:
        return parts[0].strip(), parts[1].strip()
    return result_text.strip(), None


def _format_dict_success_result(result):
    """Render a dict-based Celery success payload."""
    message = result.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip(), None

    if "processed" in result:
        processed = result.get("processed", 0)
        total_accounts = result.get("total_accounts")
        if total_accounts is None:
            summary = f"Processed {processed} account(s)."
        else:
            summary = f"Processed {processed} of {total_accounts} account(s)."

        errors = result.get("errors")
        if errors:
            return summary, f"{errors} account(s) reported errors."
        return summary, None

    return json.dumps(result, sort_keys=True), None


def _format_list_success_result(result):
    """Render a list-based Celery success payload."""
    if result and all(isinstance(item, str) for item in result):
        summary = result[0].strip() or "Task completed successfully."
        error_lines = [
            item.strip() for item in result[1:] if item and item.strip()
        ]
        return summary, "\n".join(error_lines) or None

    return "Queued follow-up import task.", None


def _format_structured_success_result(result):
    """Render a user-facing summary for non-string Celery success payloads."""
    if isinstance(result, dict):
        return _format_dict_success_result(result)

    if isinstance(result, list):
        return _format_list_success_result(result)

    if result is None:
        return "Task completed successfully.", None

    return str(result), None


def process_task_result(task):
    """Process task result based on status and format appropriately."""
    if task.status == "FAILURE":
        result_json = _deserialize_task_result(task.result)
        if (
            isinstance(result_json, dict)
            and result_json.get("exc_type") == "MediaImportError"
            and result_json.get("exc_message")
        ):
            task.summary = result_json["exc_message"][0]
            task.errors = task.traceback
        else:
            task.summary = "Unexpected error occurred while processing the task."
            task.errors = task.traceback
    elif task.status == "STARTED":
        task.summary = "This task is currently running."
        task.errors = None
    elif task.status == "SUCCESS":
        result_json = _deserialize_task_result(task.result)
        if isinstance(result_json, str):
            task.summary, task.errors = _split_success_result(result_json)
        else:
            task.summary, task.errors = _format_structured_success_result(result_json)
    elif task.status == "PENDING":
        task.summary = "This task has been queued and is waiting to run."
        task.errors = None

    return task


def get_next_run_info(periodic_task):
    """Calculate next run time and frequency for a periodic task."""
    try:
        kwargs = json.loads(periodic_task.kwargs)
        mode = kwargs.get("mode", "new")  # Default to 'new' if not specified
    except (AttributeError, TypeError, json.JSONDecodeError):
        mode = "new"

    mode_labels = {
        "new": "Only New Items",
        "overwrite": "Overwrite Existing",
        "update_collection": "Collection Metadata Only",
        "watchlist": "Watchlist Sync",
    }
    mode = mode_labels.get(mode, str(mode).replace("_", " ").title())

    if getattr(periodic_task, "interval", None):
        delta = _interval_to_timedelta(periodic_task.interval)
        if delta is None:
            return None

        now = timezone.now()
        base_time = periodic_task.last_run_at or periodic_task.start_time or now
        if base_time > now:
            next_run = base_time
        else:
            elapsed = now - base_time
            steps = int(elapsed // delta) + 1
            next_run = base_time + (delta * steps)

        return {
            "next_run": next_run,
            "frequency": _interval_frequency_display(periodic_task.interval),
            "mode": mode,
        }

    if not periodic_task.crontab:
        return None

    cron = periodic_task.crontab
    tz = zoneinfo.ZoneInfo(str(cron.timezone))
    now = timezone.now().astimezone(tz)

    # Create cron expression
    cron_expr = (
        f"{cron.minute} {cron.hour} {cron.day_of_month} "
        f"{cron.month_of_year} {cron.day_of_week}"
    )
    cron_iter = croniter.croniter(cron_expr, now)
    next_run = cron_iter.get_next(datetime)

    # Determine frequency
    if cron.day_of_week == "*":
        # Check for "every X hours" pattern by examining the cron expression
        # Pattern should be: "0 */X * * *" (minute=0, hour=*/X, all others *)
        cron_parts = cron_expr.split()
        if len(cron_parts) >= 2:
            minute_part = str(cron_parts[0])
            hour_part = str(cron_parts[1])
            # Check if it matches "every 2 hours" pattern
            if hour_part == "*/2" and minute_part in ("0", "00"):
                frequency = "Every 2 hours"
            # Check if it matches "every 3 hours" pattern (for backwards compatibility)
            elif hour_part == "*/3" and minute_part in ("0", "00"):
                frequency = "Every 3 hours"
            elif cron.day_of_month == "*" and cron.month_of_year == "*":
                frequency = "Every Day"
            else:
                frequency = "Every Day"
        elif cron.day_of_month == "*" and cron.month_of_year == "*":
            frequency = "Every Day"
        else:
            frequency = "Every Day"
    elif cron.day_of_week == "*/2":
        frequency = "Every 2 days"
    else:
        frequency = f"Cron: {cron_expr}"

    return {
        "next_run": next_run,
        "frequency": frequency,
        "mode": mode,
    }


def _interval_to_timedelta(interval):
    """Convert a Celery beat interval schedule into a timedelta."""
    multipliers = {
        "days": "days",
        "hours": "hours",
        "minutes": "minutes",
        "seconds": "seconds",
    }
    unit = multipliers.get(interval.period)
    if unit is None:
        return None
    return timedelta(**{unit: interval.every})


def _interval_frequency_display(interval):
    """Render a human-readable label for an interval schedule."""
    singular = {
        "days": "day",
        "hours": "hour",
        "minutes": "minute",
        "seconds": "second",
    }
    unit = singular.get(interval.period, interval.period)
    suffix = "" if interval.every == 1 else "s"
    return f"Every {interval.every} {unit}{suffix}"


def get_export_next_run_info(periodic_task):
    """Calculate next run time and frequency for a periodic export task."""
    if not periodic_task.crontab:
        return None

    try:
        kwargs = json.loads(periodic_task.kwargs)
        media_types = kwargs.get("media_types")
        include_lists = kwargs.get("include_lists", True)
    except json.JSONDecodeError:
        media_types = None
        include_lists = True

    cron = periodic_task.crontab
    tz = zoneinfo.ZoneInfo(str(cron.timezone))
    now = timezone.now().astimezone(tz)

    cron_expr = (
        f"{cron.minute} {cron.hour} {cron.day_of_month} "
        f"{cron.month_of_year} {cron.day_of_week}"
    )
    cron_iter = croniter.croniter(cron_expr, now)
    next_run = cron_iter.get_next(datetime)

    # Determine frequency
    if cron.day_of_week == "*/2":
        frequency = "Every 2 Days"
    elif cron.day_of_month == "*/7" or cron.day_of_week in ("0", "1", "2", "3", "4", "5", "6"):
        frequency = "Weekly"
    else:
        frequency = "Daily"

    return {
        "next_run": next_run,
        "frequency": frequency,
        "media_types": media_types,
        "include_lists": include_lists,
    }
