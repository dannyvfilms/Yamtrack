from datetime import date, datetime, time, timedelta
from pathlib import Path

from django import template
from django.conf import settings
from django.urls import reverse
from django.utils import formats, timezone
from django.utils.dateparse import parse_date
from django.utils.html import format_html
from unidecode import unidecode

from app import config
from app.models import Item, MediaTypes, Sources, Status
from app.services import metadata_resolution
from users.models import TimeFormatChoices
from users.templatetags.user_tags import user_date_format, user_time_format

register = template.Library()


@register.simple_tag
def get_static_file_mtime(file_path):
    """Return the last modification time of a static file for cache busting."""
    # Check STATICFILES_DIRS first (for development), then STATIC_ROOT (for production)
    for static_dir in getattr(settings, "STATICFILES_DIRS", []):
        full_path = Path(static_dir) / file_path
        try:
            mtime = int(full_path.stat().st_mtime)
            return f"?{mtime}"
        except OSError:
            continue

    # Fall back to STATIC_ROOT
    full_path = Path(settings.STATIC_ROOT) / file_path
    try:
        mtime = int(full_path.stat().st_mtime)
        return f"?{mtime}"
    except OSError:
        # If file doesn't exist or can't be accessed
        return ""


@register.filter
def no_underscore(arg1):
    """Return the title case of the string."""
    return arg1.replace("_", " ")


@register.filter
def title_preserve_acronyms(value):
    """Title-case text while preserving all-uppercase acronyms."""
    if not isinstance(value, str):
        return value

    normalized = value.strip()
    if not normalized:
        return normalized

    if normalized.isupper():
        return normalized

    return normalized.title()


@register.filter
def slug(arg1):
    """Return the slug of the string.

    Sometimes slugify removes all characters from a string, so we need to
    urlencode the special characters first.
    e.g Anime: 31687
    """
    cleaned = template.defaultfilters.slugify(arg1)
    if cleaned == "":
        cleaned = template.defaultfilters.slugify(
            template.defaultfilters.urlencode(unidecode(arg1)),
        )
        if cleaned == "":
            cleaned = template.defaultfilters.urlencode(unidecode(arg1))

            if cleaned == "":
                cleaned = template.defaultfilters.urlencode(arg1)

    return cleaned


@register.filter
def date_format(datetime, user):
    """Format a datetime using user's preferred date format (date only, no time).

    Args:
        datetime: The datetime object to format
        user: User object to get preferred date format
    """
    return user_date_format(datetime, user)


@register.filter
def iso_date_format(value, user):
    """Format an ISO date string (YYYY-MM-DD) using user's preferred date format.

    If value is not a valid ISO date string, returns the original value.
    """
    if not value or not user:
        return value

    parsed_value = value
    if isinstance(value, str):
        parsed_value = parse_date(value)
        if parsed_value is None:
            return value
    elif not isinstance(value, (date, datetime)):
        return value

    # user_date_format expects datetime-like values for user-specific formatting.
    if isinstance(parsed_value, date) and not isinstance(parsed_value, datetime):
        parsed_value = timezone.make_aware(
            datetime.combine(parsed_value, time.min),
            timezone.get_current_timezone(),
        )

    return user_date_format(parsed_value, user)


@register.filter
def time_format(datetime, user):
    """Format a datetime using user's preferred time format (time only, no date)."""
    return user_time_format(datetime, user)


@register.filter
def datetime_format(datetime, user):
    """Format a datetime using user's preferred formats.

    Includes time only if TRACK_TIME setting is enabled.

    Args:
        datetime: The datetime object to format
        user: User object to get preferred date/time format
    """
    if not datetime:
        return None
    formatted_date = user_date_format(datetime, user)

    if settings.TRACK_TIME:
        formatted_time = user_time_format(datetime, user)
        if formatted_time:
            return f"{formatted_date} {formatted_time}"
    return formatted_date


@register.filter
def rating_scale_max(user):
    """Return the user's rating scale max (defaults to 10)."""
    if not user:
        return 10
    try:
        return user.rating_scale_max
    except AttributeError:
        return 10


@register.filter
def score_display(score, user):
    """Format a score using the user's rating scale."""
    if score is None:
        return None
    if not user:
        return score
    try:
        formatted = user.format_score_for_display(score)
    except AttributeError:
        return score
    return formatted if formatted is not None else score


@register.filter
def match_percent(value):
    """Convert a 0-1 match score into a clamped integer percentage."""
    if value is None:
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    normalized = max(0.0, min(1.0, normalized))
    return int(round(normalized * 100))


@register.filter
def is_list(arg1):
    """Return True if the object is a list."""
    return isinstance(arg1, list)


@register.filter
def source_readable(source):
    """Return the readable source name."""
    if not source:
        return ""
    try:
        return Sources(source).label
    except ValueError:
        return source


@register.filter
def media_type_readable(media_type):
    """Return the readable media type."""
    return MediaTypes(media_type).label


@register.filter
def media_type_readable_plural(media_type):
    """Return the readable media type in plural form."""
    singular = MediaTypes(media_type).label

    # Special cases that don't change in plural form
    if singular.lower() in [MediaTypes.ANIME.value, MediaTypes.MANGA.value, MediaTypes.MUSIC.value]:
        return singular

    return f"{singular}s"


@register.filter
def media_status_readable(media_status):
    """Return the readable media status."""
    return Status(media_status).label


@register.filter
def default_source(media_type):
    """Return the default source for the media type."""
    return config.get_default_source_name(media_type).label


@register.filter
def media_past_verb(media_type):
    """Return the past tense verb for the given media type."""
    return config.get_verb(media_type, past_tense=True)


@register.filter
def sample_search(media_type):
    """Return a sample search URL for the given media type using GET parameters."""
    return config.get_sample_search_url(media_type)


@register.filter
def short_unit(media_type):
    """Return the short unit for the media type."""
    try:
        return config.get_unit(media_type, short=True)
    except (KeyError, TypeError):
        return ""


@register.filter
def long_unit(media_type):
    """Return the long unit for the media type."""
    try:
        return config.get_unit(media_type, short=False)
    except (KeyError, TypeError):
        return ""


@register.filter
def safe_attr(obj, attr):
    """Safely get an attribute from an object, returning None if it doesn't exist."""
    if obj is None:
        return None
    return getattr(obj, attr, None)


def _normalize_title_value(value):
    return Item._normalize_title_value(value)


def _resolve_title_pair(item, preference):
    """Resolve display/alternate titles for dicts and model-like objects."""
    if isinstance(item, dict):
        title = item.get("title")
        original_title = item.get("original_title")
        localized_title = item.get("localized_title")
    else:
        title = getattr(item, "title", None)
        original_title = getattr(item, "original_title", None)
        localized_title = getattr(item, "localized_title", None)

    return Item.resolve_title_variants(
        title=title,
        original_title=original_title,
        localized_title=localized_title,
        preference=preference,
    )


@register.filter
def display_title(item, user):
    """Return the preferred display title for an item."""
    if not item:
        return ""

    if hasattr(item, "get_display_title"):
        return item.get_display_title(user=user)

    preference = getattr(user, "title_display_preference", "localized")
    display, _ = _resolve_title_pair(item, preference)
    return display


@register.filter
def alternative_title(item, user):
    """Return the alternate title (opposite of display title) for tooltip use."""
    if not item:
        return None

    if hasattr(item, "get_alternative_title"):
        return item.get_alternative_title(user=user)

    preference = getattr(user, "title_display_preference", "localized")
    _, alternative = _resolve_title_pair(item, preference)
    return alternative


@register.simple_tag
def season_card_title(item):
    """Return the best display title for a season card."""
    if not item:
        return ""

    if isinstance(item, dict):
        provider_title = item.get("season_title")
        season_number = item.get("season_number")
        fallback_title = item.get("title", "")
    else:
        provider_title = getattr(item, "season_title", None)
        season_number = getattr(item, "season_number", None)
        fallback_title = getattr(item, "title", "")

    normalized_provider_title = _normalize_title_value(provider_title)
    normalized_fallback_title = _normalize_title_value(fallback_title)
    if normalized_provider_title and normalized_provider_title != normalized_fallback_title:
        return normalized_provider_title

    try:
        season_number = int(season_number) if season_number is not None else None
    except (TypeError, ValueError):
        season_number = None

    if season_number == 0:
        return "Specials"
    if season_number is not None:
        return f"Season {season_number}"

    return normalized_fallback_title or ""


def _resolve_music_artist_url_target(value):
    """Resolve a music-artist-like value into an object or dict with id/name."""
    if not value:
        return None

    if isinstance(value, dict):
        nested_artist = value.get("artist")
        if nested_artist:
            return _resolve_music_artist_url_target(nested_artist)
        if value.get("id") is not None and value.get("name"):
            return value
        return None

    nested_artist = getattr(value, "artist", None)
    if nested_artist is not None and not (
        getattr(value, "id", None) is not None and getattr(value, "name", None)
    ):
        return _resolve_music_artist_url_target(nested_artist)

    if getattr(value, "id", None) is not None and getattr(value, "name", None):
        return value

    return None


def _resolve_music_album_url_target(value):
    """Resolve a music-album-like value into an object or dict with id/title."""
    if not value:
        return None

    if isinstance(value, dict):
        if value.get("album_id") is not None and value.get("album"):
            return {
                "id": value.get("album_id"),
                "title": value.get("album"),
                "artist_id": value.get("album_artist_id"),
                "artist_name": value.get("album_artist_name"),
            }
        nested_album = value.get("album")
        if isinstance(nested_album, dict):
            return _resolve_music_album_url_target(nested_album)
        if value.get("id") is not None and value.get("title"):
            return value
        return None

    nested_album = getattr(value, "album", None)
    if nested_album is not None and not (
        getattr(value, "id", None) is not None and getattr(value, "title", None)
    ):
        return _resolve_music_album_url_target(nested_album)

    if getattr(value, "id", None) is not None and getattr(value, "title", None):
        return value

    return None


def _music_slug(value, fallback):
    """Return a stable slug with a safe fallback."""
    normalized = slug(value or "")
    if normalized:
        return normalized
    fallback_value = str(fallback or "item")
    return slug(fallback_value) or "item"


@register.filter
def music_artist_url(artist):
    """Return the canonical shared media-details URL for a music artist."""
    resolved_artist = _resolve_music_artist_url_target(artist)
    if not resolved_artist:
        return ""

    if isinstance(resolved_artist, dict):
        artist_id = resolved_artist.get("id")
        artist_name = resolved_artist.get("name")
    else:
        artist_id = resolved_artist.id
        artist_name = resolved_artist.name

    if artist_id is None:
        return ""

    return reverse(
        "music_artist_details",
        kwargs={
            "artist_id": artist_id,
            "artist_slug": _music_slug(artist_name, artist_id),
        },
    )


@register.filter
def music_album_url(album):
    """Return the canonical shared media-details URL for a music album."""
    resolved_album = _resolve_music_album_url_target(album)
    if not resolved_album:
        return ""

    if isinstance(resolved_album, dict):
        album_id = resolved_album.get("id")
        album_title = resolved_album.get("title")
        artist = resolved_album.get("artist")
        artist_id = resolved_album.get("artist_id")
        artist_name = resolved_album.get("artist_name")
    else:
        album_id = resolved_album.id
        album_title = resolved_album.title
        artist = getattr(resolved_album, "artist", None)
        artist_id = getattr(artist, "id", None)
        artist_name = getattr(artist, "name", None)

    if isinstance(artist, dict):
        artist_id = artist.get("id", artist_id)
        artist_name = artist.get("name", artist_name)

    if album_id is None:
        return ""

    return reverse(
        "music_album_details",
        kwargs={
            "artist_id": artist_id or 0,
            "artist_slug": _music_slug(artist_name or "Unknown Artist", artist_id or "artist"),
            "album_id": album_id,
            "album_slug": _music_slug(album_title, album_id),
        },
    )


@register.filter
def release_year(item, media=None):
    """Return a best-effort release year from dicts or model instances."""
    if media and hasattr(media, "item"):
        release_dt = getattr(media.item, "release_datetime", None)
        if release_dt:
            return timezone.localtime(release_dt).year

    if not item:
        return None

    if isinstance(item, dict):
        year_value = item.get("year") or item.get("start_year")
        if not year_value:
            date_value = item.get("release_date") or item.get("first_air_date")
            if date_value:
                year_value = str(date_value).split("-")[0]
        try:
            return int(str(year_value)) if year_value is not None else None
        except (TypeError, ValueError):
            return None

    release_dt = getattr(item, "release_datetime", None)
    if release_dt:
        return timezone.localtime(release_dt).year

    return None


@register.filter
def sources(media_type):
    """Template filter to get source options for a media type."""
    return metadata_resolution.available_metadata_sources(media_type)


@register.simple_tag
def get_search_media_types(user):
    """Return available media types for search based on user preferences."""
    # Handle anonymous users by returning all media types
    if not user or not user.is_authenticated:
        enabled_types = [mt for mt in MediaTypes.values if mt != MediaTypes.EPISODE.value and mt != MediaTypes.SEASON.value]
    else:
        enabled_types = user.get_enabled_media_types()

    # Filter and format the types for search
    return [
        {
            "display": media_type_readable_plural(media_type),
            "value": media_type,
        }
        for media_type in enabled_types
        if media_type != MediaTypes.SEASON.value
    ]


@register.simple_tag
def get_sidebar_media_types(user):
    """Return available media types for sidebar navigation based on user preferences."""
    # Handle anonymous users by returning all media types
    if not user or not user.is_authenticated:
        enabled_types = [mt for mt in MediaTypes.values if mt != MediaTypes.EPISODE.value]
    else:
        enabled_types = user.get_enabled_media_types()

    # Format the types for sidebar
    return [
        {
            "media_type": media_type,
            "display_name": media_type_readable_plural(media_type),
        }
        for media_type in enabled_types
    ]


@register.filter
def media_color(media_type):
    """Return the color associated with the media type."""
    return config.get_text_color(media_type)


@register.filter
def status_color(status):
    """Return the color associated with the status."""
    return config.get_status_text_color(status)


@register.filter
def natural_day(datetime, user):
    """Format date with natural language (Today, Tomorrow, etc.)."""
    # Get today's date in the current timezone
    today = timezone.localdate()

    # Extract just the date part for comparison
    datetime_date = datetime.date()

    # Calculate the difference in days
    diff = datetime_date - today
    days = diff.days

    if days == 0:
        return "Today"
    if days == 1:
        return "Tomorrow"

    # For dates further away
    return datetime_format(datetime, user)


@register.filter
def user_event_time(event, user):
    """Format event time according to user's time format preference."""
    if not event or not user or event.is_sentinel_time:
        return ""

    try:
        local_dt = timezone.localtime(event.datetime)

        if user.time_format == TimeFormatChoices.SYSTEM_DEFAULT:
            time_str = formats.date_format(local_dt, "TIME_FORMAT")
        elif user.time_format == TimeFormatChoices.H_MM_AMPM:
            # Use %I and manually remove leading zero for cross-platform compatibility
            hour = str(local_dt.hour % 12 or 12)  # Convert 0 to 12 for 12-hour format
            time_str = f"{hour}:{local_dt.strftime('%M %p')}"
        elif user.time_format == TimeFormatChoices.HH_MM_AMPM:
            time_str = local_dt.strftime("%I:%M %p")
        elif user.time_format == TimeFormatChoices.HH_MM:
            time_str = local_dt.strftime("%H:%M")
        elif user.time_format == TimeFormatChoices.HH_MM_SS:
            time_str = local_dt.strftime("%H:%M:%S")
        else:
            time_str = formats.date_format(local_dt, "TIME_FORMAT")

        return f"at {time_str}"
    except (ValueError, TypeError, AttributeError):
        # Fallback to default format if there's an error
        local_dt = timezone.localtime(event.datetime)
        return f"at {local_dt.strftime('%H:%M')}"


@register.filter
def event_within_days(event, days):
    """Return True if an event is within the next N days (inclusive)."""
    if not event or not hasattr(event, "datetime"):
        return False

    try:
        days = int(days)
    except (TypeError, ValueError):
        return False

    try:
        event_date = timezone.localtime(event.datetime).date()
    except (ValueError, TypeError, AttributeError):
        return False

    today = timezone.localdate()
    delta_days = (event_date - today).days
    return 0 <= delta_days <= days


@register.filter
def media_url(media):
    """Return the media URL for both metadata and model object cases."""
    is_dict = isinstance(media, dict)

    if not is_dict and not hasattr(media, "media_type"):
        return ""

    # Get attributes using either dict access or object attribute
    media_type = (
        media.get("route_media_type") or media["media_type"]
        if is_dict
        else getattr(media, "route_media_type", None) or media.media_type
    )
    source = media["source"] if is_dict else media.source
    media_id = media["media_id"] if is_dict else media.media_id
    title = media["title"] if is_dict else media.title
    slug_title = slug(title)
    if not slug_title:
        fallback = str(media_id) if media_id is not None else "item"
        slug_title = slug(fallback) or "item"

    if media_type in [MediaTypes.SEASON.value, MediaTypes.EPISODE.value]:
        season_number = media["season_number"] if is_dict else media.season_number
        return reverse(
            "season_details",
            kwargs={
                "source": source,
                "media_id": media_id,
                "title": slug_title,
                "season_number": season_number,
            },
        )

    return reverse(
        "media_details",
        kwargs={
            "source": source,
            "media_type": media_type,
            "media_id": media_id,
            "title": slug_title,
        },
    )


@register.simple_tag
def media_view_url(view_name, media):
    """Return the modal URL for both metadata and model object cases."""
    is_dict = isinstance(media, dict)
    if not is_dict and not hasattr(media, "source"):
        return ""

    # Build kwargs using either dict access or object attribute
    kwargs = {
        "source": media["source"] if is_dict else media.source,
        "media_type": (
            media.get("route_media_type") or media["media_type"]
            if is_dict
            else getattr(media, "route_media_type", None) or media.media_type
        ),
        "media_id": str(media["media_id"] if is_dict else media.media_id),
    }

    # Handle season/episode numbers if they exist
    if is_dict:
        if "season_number" in media:
            kwargs["season_number"] = media["season_number"]
        if "episode_number" in media:
            kwargs["episode_number"] = media["episode_number"]
    else:
        season_number = getattr(media, "season_number", None)
        episode_number = getattr(media, "episode_number", None)
        if season_number is not None:
            kwargs["season_number"] = season_number
        if episode_number is not None:
            kwargs["episode_number"] = episode_number

    # collection_modal URL does not accept season/episode in the path
    if view_name == "collection_modal":
        kwargs.pop("season_number", None)
        kwargs.pop("episode_number", None)

    return reverse(view_name, kwargs=kwargs)


@register.simple_tag
def component_id(component_type, media, instance_id=None):
    """Return the component ID for both metadata and model object cases."""
    is_dict = isinstance(media, dict)

    if not is_dict and not hasattr(media, "media_type"):
        return ""

    # Get base attributes using either dict access or object attribute
    media_type = (
        media.get("route_media_type") or media["media_type"]
        if is_dict
        else getattr(media, "route_media_type", None) or media.media_type
    )
    media_id = media["media_id"] if is_dict else media.media_id

    component_id = f"{component_type}-{media_type}-{media_id}"

    # Handle season/episode numbers if they exist
    if is_dict:
        if "season_number" in media:
            component_id += f"-{media['season_number']}"
        if "episode_number" in media:
            component_id += f"-{media['episode_number']}"
    else:
        season_number = getattr(media, "season_number", None)
        episode_number = getattr(media, "episode_number", None)
        if season_number is not None:
            component_id += f"-{season_number}"
        if episode_number is not None:
            component_id += f"-{episode_number}"

    # Add instance id if provided
    if instance_id:
        component_id += f"-{instance_id}"

    return component_id


@register.simple_tag
def unicode_icon(name):
    """Return the Unicode icon for the media type."""
    return config.get_unicode_icon(name)


@register.simple_tag
def icon(name, is_active, extra_classes="w-5 h-5"):
    """Return the SVG icon for the given name."""
    base_svg = """<svg xmlns="http://www.w3.org/2000/svg"
                      width="24"
                      height="24"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      class="{active_class}{extra_classes}">
                      {content}
                 </svg>"""

    other_icons = {
        "home": (
            """<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path>
               <polyline points="9 22 9 12 15 12 15 22"></polyline>"""
        ),
        "create": (
            """<circle cx="12" cy="12" r="10"></circle>
               <path d="M8 12h8"></path>
               <path d="M12 8v8"></path>"""
        ),
        "statistics": (
            """<line x1="18" x2="18" y1="20" y2="10"></line>
               <line x1="12" x2="12" y1="20" y2="4"></line>
               <line x1="6" x2="6" y1="20" y2="14"></line>"""
        ),
        "history": (
            """<path d="M3 3v5h5"></path>
               <path d="M3.05 13a9 9 0 1 0 .5-5.5"></path>
               <path d="M12 7v5l3 3"></path>"""
        ),
        "lists": (
            """<path d="M12 10v6"></path>
               <path d="M9 13h6"></path>
               <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9
               L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"></path>"""
        ),
        "calendar": (
            """<path d="M8 2v4"></path>
               <path d="M16 2v4"></path>
               <rect width="18" height="18" x="3" y="4" rx="2"></rect>
               <path d="M3 10h18"></path>"""
        ),
        "settings": (
            """<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2
               2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73
               2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0
               0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2
               2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1
               1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2
               0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2
               2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0
               1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"></path>
               <circle cx="12" cy="12" r="3"></circle>"""
        ),
        "logout": (
            """<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path>
               <polyline points="16 17 21 12 16 7"></polyline>
               <line x1="21" x2="9" y1="12" y2="12"></line>"""
        ),
    }

    if name in MediaTypes.values:
        content = config.get_svg_icon(name)
    else:
        content = other_icons[name]
    active_class = "text-indigo-400 " if is_active else ""

    svg = base_svg.format(
        content=content,
        active_class=active_class,
        extra_classes=extra_classes,
    )

    return format_html(svg)


@register.filter
def str_equals(value, arg):
    """Return True if the string value is equal to the argument."""
    return str(value) == str(arg)


@register.filter
def get_range(value):
    """Return a range from 1 to the given value."""
    return range(1, int(value) + 1)


@register.simple_tag
def get_pagination_range(current_page, total_pages, window):
    """
    Return a list of page numbers to display in pagination.

    Args:
        current_page: The current page number
        total_pages: Total number of pages
        window: Number of pages to show before and after current page

    Returns:
        A list of page numbers and None values (for ellipses)
    """
    if total_pages <= 5 + window * 2:
        # If few pages, show all
        return list(range(1, total_pages + 1))

    # Calculate left and right boundaries
    left_boundary = max(2, current_page - window)
    right_boundary = min(total_pages - 1, current_page + window)

    # Add ellipsis indicators and page numbers
    result = [1]

    second_page = 2
    # Add left ellipsis if needed
    if left_boundary > second_page:
        result.append(None)  # None represents ellipsis

    # Add pages around current page
    result.extend(range(left_boundary, right_boundary + 1))

    # Add right ellipsis if needed
    if right_boundary < total_pages - 1:
        result.append(None)  # None represents ellipsis

    # Add last page if not already included
    if total_pages not in result:
        result.append(total_pages)

    return result


def _check_same_day_ranges(start_date, end_date, today):
    """Check for same-day date ranges like Today and Yesterday."""
    if start_date == end_date:
        if start_date == today:
            return "Today"
        if start_date == today - timedelta(days=1):
            return "Yesterday"
    return None


def _check_week_ranges(start_date, end_date, today):
    """Check for week-based date ranges."""
    days_diff = (end_date - start_date).days
    if days_diff == 6:  # 7 days including start and end
        if start_date == today - timedelta(days=6):
            return "This Week"
        if start_date == today - timedelta(days=13):
            return "Last Week"
        return "Last 7 Days"
    return None


def _check_month_ranges(start_date, end_date, today):
    """Check for month-based date ranges."""
    days_diff = (end_date - start_date).days
    if days_diff == 29:  # 30 days including start and end
        if start_date == today - timedelta(days=29):
            return "This Month"
        if start_date == today - timedelta(days=59):
            return "Last Month"
        return "Last 30 Days"
    return None


def _check_extended_ranges(start_date, end_date):
    """Check for extended date ranges like 90 days, 6 months, and 1 year."""
    days_diff = (end_date - start_date).days

    # Check for 90 days
    if days_diff == 89:  # 90 days including start and end
        return "Last 90 Days"

    # Check for 6 months (approximately 180 days)
    if 175 <= days_diff <= 185:
        return "Last 6 Months"

    # Check for year ranges
    if days_diff == 364:  # 365 days including start and end
        return "Last 12 Months"

    return None


def _is_predefined_date_range(start_date, end_date, today):
    """Check if the date range matches any predefined ranges."""
    # Check same-day ranges
    result = _check_same_day_ranges(start_date, end_date, today)
    if result:
        return result

    # Check week ranges
    result = _check_week_ranges(start_date, end_date, today)
    if result:
        return result

    # Check month ranges
    result = _check_month_ranges(start_date, end_date, today)
    if result:
        return result

    # Check extended ranges
    result = _check_extended_ranges(start_date, end_date)
    if result:
        return result

    return None


@register.filter
def order_by_end_date(queryset):
    """Order a queryset by end_date in ascending order (oldest first, chronological)."""
    if queryset is None:
        return queryset
    try:
        return queryset.order_by("end_date")
    except (AttributeError, TypeError):
        # If it's not a queryset or doesn't have end_date, return as-is
        return queryset


@register.filter
def format_date_range_display(start_date, end_date):
    """Format date range for display in card titles.
    
    Returns a human-readable string like "Last 12 Months" or "Date Range"
    based on whether it's a predefined range or custom dates.
    """
    if start_date is None and end_date is None:
        return "All Time"

    if start_date is None or end_date is None:
        return "Date Range"

    # Convert to date objects if they're datetime
    if hasattr(start_date, "date"):
        start_date = start_date.date()
    if hasattr(end_date, "date"):
        end_date = end_date.date()

    today = date.today()

    # Check for predefined ranges
    predefined_range = _is_predefined_date_range(start_date, end_date, today)
    if predefined_range:
        return predefined_range

    # If none of the predefined ranges match, return "Date Range"
    return "Date Range"


@register.filter
def filter_media_types(entries, media_types_str):
    """Filter entries to only include specified media types.

    Usage: {{ entries|filter_media_types:"music,podcast" }}

    Args:
        entries: List of history entry dicts with 'media_type' key
        media_types_str: Comma-separated list of media types to include

    Returns:
        List of entries matching the specified media types
    """
    if not entries or not media_types_str:
        return entries
    media_types = {mt.strip().lower() for mt in media_types_str.split(",")}
    return [
        entry for entry in entries
        if entry.get("media_type", "").lower() in media_types
    ]


@register.filter
def exclude_media_types(entries, media_types_str):
    """Filter entries to exclude specified media types.

    Usage: {{ entries|exclude_media_types:"music,podcast" }}

    Args:
        entries: List of history entry dicts with 'media_type' key
        media_types_str: Comma-separated list of media types to exclude

    Returns:
        List of entries NOT matching the specified media types
    """
    if not entries or not media_types_str:
        return entries
    media_types = {mt.strip().lower() for mt in media_types_str.split(",")}
    return [
        entry for entry in entries
        if entry.get("media_type", "").lower() not in media_types
    ]


@register.filter
def is_square_media_type(media_type):
    """Check if a media type uses square (1:1) aspect ratio artwork.

    Usage: {% if entry.media_type|is_square_media_type %}

    Returns:
        True for music and podcast types, False otherwise
    """
    if not media_type:
        return False
    return media_type.lower() in ("music", "podcast")


@register.filter
def filter_home_media_types(items, media_types_str):
    """Filter home page items to only include specified media types.

    Usage: {{ media_list.items|filter_home_media_types:"music,podcast" }}

    Args:
        items: List of BasicMedia objects with item.media_type attribute
        media_types_str: Comma-separated list of media types to include

    Returns:
        List of items matching the specified media types
    """
    if not items or not media_types_str:
        return items
    media_types = {mt.strip().lower() for mt in media_types_str.split(",")}
    return [
        item for item in items
        if getattr(getattr(item, "item", None), "media_type", "").lower() in media_types
    ]


@register.filter
def exclude_home_media_types(items, media_types_str):
    """Filter home page items to exclude specified media types.

    Usage: {{ media_list.items|exclude_home_media_types:"music,podcast" }}

    Args:
        items: List of BasicMedia objects with item.media_type attribute
        media_types_str: Comma-separated list of media types to exclude

    Returns:
        List of items NOT matching the specified media types
    """
    if not items or not media_types_str:
        return items
    media_types = {mt.strip().lower() for mt in media_types_str.split(",")}
    return [
        item for item in items
        if getattr(getattr(item, "item", None), "media_type", "").lower() not in media_types
    ]


@register.filter
def show_media_score(rating, user):
    """Return whether a media rating should be displayed for the user."""
    if rating is None:
        return False

    try:
        rating_value = float(rating)
    except (TypeError, ValueError):
        return True

    hide_zero = getattr(user, "hide_zero_rating", False)
    return not hide_zero or rating_value > 0
