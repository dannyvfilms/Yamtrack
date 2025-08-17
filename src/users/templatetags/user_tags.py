from django import template
from django.templatetags.static import static
from django.utils.html import format_html

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
        from users.models import DateFormatChoices
        from django.utils import timezone, formats
        from datetime import datetime
        
        # Handle string dates by parsing them first
        if isinstance(date, str):
            try:
                # Try to parse common date formats
                for fmt in ['%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f']:
                    try:
                        date = datetime.strptime(date, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    # If we can't parse the string, return it as-is
                    return date
            except Exception:
                # If parsing fails, return the original string
                return date
        
        # Ensure we have a datetime object
        if not hasattr(date, 'month'):
            return str(date)
        
        local_dt = timezone.localtime(date)
        
        if user.date_format == DateFormatChoices.SYSTEM_DEFAULT:
            return formats.date_format(local_dt, "DATE_FORMAT")
        elif user.date_format == DateFormatChoices.ISO_8601:
            return local_dt.strftime("%Y-%m-%d")
        elif user.date_format == DateFormatChoices.MONTH_D_YYYY:
            return local_dt.strftime("%b %-d, %Y")
        elif user.date_format == DateFormatChoices.D_MON_YYYY:
            return local_dt.strftime("%-d %b %Y")
        elif user.date_format == DateFormatChoices.M_D_YYYY:
            return local_dt.strftime("%-m/%-d/%Y")
        elif user.date_format == DateFormatChoices.D_M_YYYY:
            return local_dt.strftime("%-d/%-m/%Y")
        elif user.date_format == DateFormatChoices.DD_MM_YYYY:
            return local_dt.strftime("%d.%m.%Y")
        else:
            return formats.date_format(local_dt, "DATE_FORMAT")
    except Exception:
        # Fallback to default format if there's an error
        try:
            from django.utils import formats
            return formats.date_format(date, "DATE_FORMAT")
        except Exception:
            # If all else fails, return the original value as a string
            return str(date)


@register.filter
def user_time_format(datetime_obj, user):
    """Format a time according to user's time format preference."""
    if not datetime_obj or not user:
        return ""
    
    try:
        from users.models import TimeFormatChoices
        from django.utils import timezone, formats
        from datetime import datetime
        
        # Handle string dates by parsing them first
        if isinstance(datetime_obj, str):
            try:
                # Try to parse common datetime formats
                for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M', '%H:%M:%S', '%H:%M']:
                    try:
                        datetime_obj = datetime.strptime(datetime_obj, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    # If we can't parse the string, return it as-is
                    return datetime_obj
            except Exception:
                # If parsing fails, return the original string
                return datetime_obj
        
        # Ensure we have a datetime object
        if not hasattr(datetime_obj, 'hour'):
            return str(datetime_obj)
        
        local_dt = timezone.localtime(datetime_obj)
        
        if user.time_format == TimeFormatChoices.SYSTEM_DEFAULT:
            return formats.date_format(local_dt, "TIME_FORMAT")
        elif user.time_format == TimeFormatChoices.H_MM_AMPM:
            return local_dt.strftime("%-I:%M %p")
        elif user.time_format == TimeFormatChoices.HH_MM_AMPM:
            return local_dt.strftime("%I:%M %p")
        elif user.time_format == TimeFormatChoices.HH_MM:
            return local_dt.strftime("%H:%M")
        elif user.time_format == TimeFormatChoices.HH_MM_SS:
            return local_dt.strftime("%H:%M:%S")
        else:
            return formats.date_format(local_dt, "TIME_FORMAT")
    except Exception:
        # Fallback to default format if there's an error
        try:
            from django.utils import formats
            return formats.date_format(datetime_obj, "TIME_FORMAT")
        except Exception:
            # If all else fails, return the original value as a string
            return str(datetime_obj)


@register.filter
def user_datetime_format(datetime_obj, user):
    """Format a datetime according to user's date and time format preferences."""
    if not datetime_obj or not user:
        return ""
    
    try:
        from datetime import datetime
        
        # Handle string dates by parsing them first
        if isinstance(datetime_obj, str):
            try:
                # Try to parse common datetime formats
                for fmt in ['%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M', '%Y-%m-%d']:
                    try:
                        datetime_obj = datetime.strptime(datetime_obj, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    # If we can't parse the string, return it as-is
                    return datetime_obj
            except Exception:
                # If parsing fails, return the original string
                return datetime_obj
        
        # Ensure we have a datetime object
        if not hasattr(datetime_obj, 'month') or not hasattr(datetime_obj, 'hour'):
            return str(datetime_obj)
        
        date_part = user_date_format(datetime_obj, user)
        time_part = user_time_format(datetime_obj, user)
        return f"{date_part} {time_part}"
    except Exception:
        # Fallback to default format if there's an error
        try:
            from django.utils import formats
            return formats.date_format(datetime_obj, "DATETIME_FORMAT")
        except Exception:
            # If all else fails, return the original value as a string
            return str(datetime_obj)
