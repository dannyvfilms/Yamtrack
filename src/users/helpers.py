import json
import zoneinfo
from datetime import datetime

import croniter
from django.utils import timezone


def get_client_ip(request):
    """Return the client's IP address.

    Used when logging for user registration and login.
    """
    # get the user's IP address
    ip_address = request.headers.get("x-forwarded-for")

    # if the IP address is not available in HTTP_X_FORWARDED_FOR
    if not ip_address:
        ip_address = request.META.get("REMOTE_ADDR")

    return ip_address


def process_task_result(task):
    """Process task result based on status and format appropriately."""
    if task.status == "FAILURE":
        result_json = json.loads(task.result)
        if result_json["exc_type"] == "MediaImportError":
            task.summary = result_json["exc_message"][0]
            task.errors = task.traceback
        else:
            task.summary = "Unexpected error occurred while processing the task."
            task.errors = task.traceback
    elif task.status == "STARTED":
        task.summary = "This task is currently running."
        task.errors = None
    elif task.status == "SUCCESS":
        import integrations.tasks as integration_tasks

        result_json = json.loads(task.result)
        # Split by the error indicator
        parts = result_json.split(integration_tasks.ERROR_TITLE.strip())
        if len(parts) > 1:
            # We have both summary and errors
            task.summary = parts[0].strip()

            # Keep errors as a single string with newlines
            task.errors = parts[1].strip()
        else:
            # Only summary, no errors
            task.summary = result_json.strip()
            task.errors = None
    elif task.status == "PENDING":
        task.summary = "This task has been queued and is waiting to run."
        task.errors = None

    return task


def get_next_run_info(periodic_task):
    """Calculate next run time and frequency for a periodic task."""
    if not periodic_task.crontab:
        return None

    try:
        kwargs = json.loads(periodic_task.kwargs)
        mode = kwargs.get("mode", "new")  # Default to 'new' if not specified
    except json.JSONDecodeError:
        mode = "new"

    mode = "Only New Items" if mode == "new" else "Overwrite Existing"

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
