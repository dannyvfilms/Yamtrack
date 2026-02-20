from datetime import datetime

from django import template
from django.templatetags.static import static
from django.utils import formats, timezone
from django.utils.html import format_html

from users.models import DateFormatChoices, TimeFormatChoices

register = template.Library()


@register.filter
def get_attr(obj, attr):
    """Get attribute from object dynamically."""
    return getattr(obj, attr, None)


SOURCES_CONFIG = {
    "kitsu": {
        "name": "Kitsu",
        "logo": static("img/kitsu-logo.png"),
    },
    "trakt": {
        "name": "Trakt",
        "logo": static("img/trakt-logo.svg"),
    },
    "myanimelist": {
        "name": "MyAnimeList",
        "logo": static("img/mal-logo.ico"),
    },
    "anilist": {
        "name": "AniList",
        "logo": static("img/anilist-logo.svg"),
    },
    "simkl": {
        "name": "SIMKL",
        "logo": static("img/simkl-logo.png"),
    },
    "yamtrack": {
        "name": "YamTrack",
        "logo": static("favicon/apple-touch-icon.png"),
    },
    "hltb": {
        "name": "HowLongToBeat",
        "logo": static("img/hltb-logo.png"),
    },
    "imdb": {
        "name": "IMDB",
        "logo": static("img/imdb-logo.png"),
    },
    "steam": {
        "name": "Steam",
        "logo": static("img/steam-logo.ico"),
    },
    "goodreads": {
        "name": "GoodReads",
        "logo": static("img/logo-goodreads.svg"),
    },
    "plex": {
        "name": "Plex",
        "logo": static("img/plex-logo.svg"),
    },
    "audiobookshelf": {
        "name": "Audiobookshelf",
        "logo": static("img/audiobookshelf-logo.svg"),
    },
    "pocketcasts": {
        "name": "Pocket Casts",
        "logo": static("img/pocketcasts-logo.svg"),
    },
    "lastfm": {
        "name": "Last.fm",
        "logo": static("img/lastfm-logo.png"),
    },
    "hardcover": {
        "name": "Hardcover",
        "logo": static("img/hardcover-logo.png"),
    },
}


@register.simple_tag
def source_display(source_name):
    """Generate HTML display for a media source with logo and name."""
    info = SOURCES_CONFIG.get(source_name)
    if not info:
        return ""

    html = f"""
        <div class="flex items-center">
            <img alt="{info["name"]}" class="w-6 h-6 mr-2" src="{info["logo"]}">
            <h4 class="font-medium">{info["name"]}</h4>
        </div>
    """

    return format_html(html)


@register.filter
def date_format_display(format_value):
    """Display the human-readable name for date format values."""
    format_display_map = {
        "system_default": "System default (locale) — Aug 12, 2025 / 12 Aug 2025",
        "iso_8601": "ISO 8601 — 2025-08-12",
        "month_d_yyyy": "Month D, YYYY — Aug 12, 2025",
        "d_mon_yyyy": "D Mon YYYY — 12 Aug 2025",
        "m_d_yyyy": "M/D/YYYY — 08/12/2025",
        "d_m_yyyy": "D/M/YYYY — 12/08/2025",
        "dd_mm_yyyy": "DD.MM.YYYY — 12.08.2025",
        "yyyy_mm_dd": "YYYY/MM/DD — 2025/08/12",
    }
    return format_display_map.get(format_value, format_value)


@register.filter
def time_format_display(format_value):
    """Display the human-readable name for time format values."""
    format_display_map = {
        "system_default": "System default (locale) — 6:45 PM / 18:45",
        "h_mm_ampm": "12-hour (h:mm AM/PM) — 6:45 PM",
        "hh_mm_ampm": "12-hour, leading zero (hh:mm AM/PM) — 06:45 PM",
        "hh_mm": "24-hour (HH:mm) — 18:45",
        "hh_mm_ss": "24-hour with seconds (HH:mm:ss) — 18:45:00",
    }
    return format_display_map.get(format_value, format_value)


@register.filter
def user_date_format(date, user):
    """Format a date according to user's date format preference."""
    if not date or not user:
        return ""

    try:
        # Simplified version - just handle the basic case
        if isinstance(date, str):
            try:
                # Try to parse the date string
                date_obj = datetime.strptime(date, "%Y-%m-%d")
                date = timezone.make_aware(date_obj, timezone.get_current_timezone())
            except (ValueError, TypeError):
                # If parsing fails, return the original string
                return date

        # Ensure we have a datetime object
        if not hasattr(date, "month"):
            return str(date)

        local_dt = timezone.localtime(date)

        # Simple formatting based on user preference
        if user.date_format == DateFormatChoices.ISO_8601:
            return local_dt.strftime("%Y-%m-%d")
        if user.date_format == DateFormatChoices.MONTH_D_YYYY:
            return local_dt.strftime("%b %d, %Y")
        if user.date_format == DateFormatChoices.D_MON_YYYY:
            return local_dt.strftime("%d %b %Y")
        if user.date_format == DateFormatChoices.M_D_YYYY:
            return f"{local_dt.month}/{local_dt.day}/{local_dt.year}"
        if user.date_format == DateFormatChoices.D_M_YYYY:
            return f"{local_dt.day}/{local_dt.month}/{local_dt.year}"
        if user.date_format == DateFormatChoices.DD_MM_YYYY:
            return local_dt.strftime("%d.%m.%Y")
        if user.date_format == DateFormatChoices.YYYY_MM_DD:
            return f"{local_dt.year}/{local_dt.month:02d}/{local_dt.day:02d}"
        # Default to system format
        return formats.date_format(local_dt, "DATE_FORMAT")

    except (ValueError, TypeError, AttributeError):
        # Fallback to default format if there's an error
        try:
            return formats.date_format(date, "DATE_FORMAT")
        except (ValueError, TypeError, AttributeError):
            # If all else fails, return the original value as a string
            return str(date)


@register.filter
def user_time_format(datetime_obj, user):
    """Format a time according to user's time format preference."""
    if not datetime_obj or not user:
        return ""

    try:
        # Parse string dates if needed
        datetime_obj = _parse_datetime_string(datetime_obj)

        # Ensure we have a datetime object
        if not hasattr(datetime_obj, "hour"):
            return str(datetime_obj)

        local_dt = timezone.localtime(datetime_obj)
        return _format_time_by_preference(local_dt, user, formats)

    except (ValueError, TypeError, AttributeError):
        # Fallback to default format if there's an error
        try:
            return formats.date_format(datetime_obj, "TIME_FORMAT")
        except (ValueError, TypeError, AttributeError):
            # If all else fails, return the original value as a string
            return str(datetime_obj)


def _parse_datetime_string(datetime_obj):
    """Parse string dates into datetime objects."""
    if isinstance(datetime_obj, str):
        try:
            # Try to parse common datetime formats
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"]:
                try:
                    return datetime.strptime(datetime_obj, fmt)
                except ValueError:
                    continue
            # If we can't parse the string, return it as-is
            return datetime_obj
        except (ValueError, TypeError):
            # If parsing fails, return the original string
            return datetime_obj
    return datetime_obj


def _format_time_by_preference(local_dt, user, formats):
    """Format time according to user preference."""
    if user.time_format == TimeFormatChoices.SYSTEM_DEFAULT:
        return formats.date_format(local_dt, "TIME_FORMAT")
    if user.time_format == TimeFormatChoices.H_MM_AMPM:
        # Use %I and manually remove leading zero for cross-platform compatibility
        hour = str(local_dt.hour % 12 or 12)  # Convert 0 to 12 for 12-hour format
        return f"{hour}:{local_dt.strftime('%M %p')}"
    if user.time_format == TimeFormatChoices.HH_MM_AMPM:
        return local_dt.strftime("%I:%M %p")
    if user.time_format == TimeFormatChoices.HH_MM:
        return local_dt.strftime("%H:%M")
    if user.time_format == TimeFormatChoices.HH_MM_SS:
        return local_dt.strftime("%H:%M:%S")
    return formats.date_format(local_dt, "TIME_FORMAT")


@register.filter
def user_datetime_format(datetime_obj, user):
    """Format a datetime according to user's date and time format preferences."""
    if not datetime_obj or not user:
        return ""

    try:
        # Parse string dates if needed
        datetime_obj = _parse_datetime_string(datetime_obj)

        # Ensure we have a datetime object
        if not hasattr(datetime_obj, "month") or not hasattr(datetime_obj, "hour"):
            return str(datetime_obj)

        date_part = user_date_format(datetime_obj, user)
        time_part = user_time_format(datetime_obj, user)
        return f"{date_part} {time_part}"
    except (ValueError, TypeError, AttributeError):
        # Fallback to default format if there's an error
        try:
            return formats.date_format(datetime_obj, "DATETIME_FORMAT")
        except (ValueError, TypeError, AttributeError):
            # If all else fails, return the original value as a string
            return str(datetime_obj)
