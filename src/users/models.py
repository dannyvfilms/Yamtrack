import hashlib
import secrets
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from celery import states
from celery.result import AsyncResult
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_celery_beat.models import PeriodicTask
from django_celery_results.models import TaskResult

from app.models import Item, MediaTypes, Sources, Status
from integrations import import_progress
from users import helpers

EXCLUDED_SEARCH_TYPES = [MediaTypes.SEASON.value, MediaTypes.EPISODE.value]

VALID_SEARCH_TYPES = [
    value for value in MediaTypes.values if value not in EXCLUDED_SEARCH_TYPES
]

VALID_HOME_SCREEN_MEDIA_TYPES = [
    value for value in MediaTypes.values if value != MediaTypes.EPISODE.value
]

MULTI_STATUS_PREFERENCE_FIELDS = {
    "tv_status",
    "season_status",
    "movie_status",
    "anime_status",
    "manga_status",
    "game_status",
    "boardgame_status",
    "book_status",
    "comic_status",
    "music_status",
    "podcast_status",
    "list_detail_status",
}
# Score-scaling constants: a user's display scale is either 1-5 or the
# internal storage scale of 0-10 (see RatingScaleChoices).
FIVE_POINT_RATING_SCALE = 5
MAX_INTERNAL_RATING_SCORE = 10


def generate_token():
    """Generate a user token."""
    return secrets.token_urlsafe(24)


class FloppyUserManager(DjangoUserManager):
    """User manager with an explicit helper for disposable test accounts."""

    def create_test_user(self, username, email=None, password=None, **extra_fields):
        """Create a user that is excluded from normal user pickers."""
        extra_fields["is_test_account"] = True
        return self.create_user(
            username=username,
            email=email,
            password=password,
            **extra_fields,
        )


class HomeSortChoices(models.TextChoices):
    """Choices for home page sort options."""

    UPCOMING = "upcoming", _("Upcoming")
    RECENT = "recent", _("Recent")
    COMPLETION = "completion", _("Completion")
    EPISODES_LEFT = "episodes_left", _("Episodes Left")
    TITLE = "title", _("Title")
    RANDOM = "random", _("Random")


class MediaSortChoices(models.TextChoices):
    """Choices for media list sort options."""

    SCORE = "score", _("Rating")
    CRITIC_RATING = "critic_rating", _("Critic Rating")
    TITLE = "title", _("Title")
    AUTHOR = "author", _("Author")
    POPULARITY = "popularity", _("Popularity")
    PROGRESS = "progress", _("Progress")
    RUNTIME = "runtime", _("Runtime")
    TIME_TO_BEAT = "time_to_beat", _("Time to Beat")
    PLATFORM = "platform", _("Platform")
    PLAYS = "plays", _("Plays")
    TIME_WATCHED = "time_watched", _("Time Watched")
    RELEASE_DATE = "release_date", _("Release Date")
    DATE_ADDED = "date_added", _("Date Added")
    START_DATE = "start_date", _("Start Date")
    END_DATE = "end_date", _("Last Watched")
    NEXT_EPISODE_AIR_DATE = "next_episode_air_date", _("Episode Air Date")
    TIME_LEFT = "time_left", _("Time Left")


GAME_LIKE_MEDIA_TYPES = {MediaTypes.GAME.value, MediaTypes.BOARDGAME.value}
READING_MEDIA_TYPES = {
    MediaTypes.BOOK.value,
    MediaTypes.COMIC.value,
    MediaTypes.MANGA.value,
}
LISTENING_MEDIA_TYPES = {MediaTypes.MUSIC.value, MediaTypes.PODCAST.value}


def relabel_end_date_sort_choice(media_type, choices):
    """Use media-type-appropriate wording for the END_DATE sort choice."""
    end_date_label = None
    if media_type in GAME_LIKE_MEDIA_TYPES:
        end_date_label = _("Last Played")
    elif media_type in READING_MEDIA_TYPES:
        end_date_label = _("Last Read")
    elif media_type in LISTENING_MEDIA_TYPES:
        end_date_label = _("Last Listened")

    if end_date_label is None:
        return choices
    return [
        (value, end_date_label)
        if value == MediaSortChoices.END_DATE
        else (value, label)
        for value, label in choices
    ]


class MediaStatusChoices(models.TextChoices):
    """Choices for media list status options."""

    ALL = "All", _("All")
    COMPLETED = Status.COMPLETED.value, _("Completed")
    IN_PROGRESS = Status.IN_PROGRESS.value, _("In Progress")
    PLANNING = Status.PLANNING.value, _("Planning")
    PAUSED = Status.PAUSED.value, _("Paused")
    DROPPED = Status.DROPPED.value, _("Dropped")


class DirectionChoices(models.TextChoices):
    """Choices for sort direction options."""

    ASC = "asc", _("Ascending")
    DESC = "desc", _("Descending")


class LayoutChoices(models.TextChoices):
    """Choices for media list layout options."""

    GRID = "grid", _("Grid")
    TABLE = "table", _("Table")


class CalendarLayoutChoices(models.TextChoices):
    """Choices for calendar layout options."""

    GRID = "grid", _("Grid")
    LIST = "list", _("List")


class ListSortChoices(models.TextChoices):
    """Choices for list sort options."""

    LAST_ITEM_ADDED = "last_item_added", _("Last Item Added")
    LAST_WATCHED = "last_watched", _("Last Watched")
    NAME = "name", _("Name")
    ITEMS_COUNT = "items_count", _("Items Count")
    NEWEST_FIRST = "newest_first", _("Newest First")


class ListDetailSortChoices(models.TextChoices):
    """Choices for list detail sort options."""

    DATE_ADDED = "date_added", _("Date Added")
    CUSTOM = "custom", _("Custom")
    TITLE = "title", _("Title")
    MEDIA_TYPE = "media_type", _("Media Type")
    RATING = "rating", _("Rating")
    PROGRESS = "progress", _("Progress")
    STATUS = "status", _("Status")
    RELEASE_DATE = "release_date", _("Release Date")
    START_DATE = "start_date", _("Start Date")
    END_DATE = "end_date", _("End Date")
    PLATFORM = "platform", _("Platform")


class DateFormatChoices(models.TextChoices):
    """Choices for date format preferences."""

    SYSTEM_DEFAULT = "system_default", _("System default")
    ISO_8601 = "iso_8601", _("ISO 8601")
    MONTH_D_YYYY = "month_d_yyyy", _("Month D, YYYY")
    D_MON_YYYY = "d_mon_yyyy", _("D Mon YYYY")
    M_D_YYYY = "m_d_yyyy", _("M/D/YYYY")
    D_M_YYYY = "d_m_yyyy", _("D/M/YYYY")
    DD_MM_YYYY = "dd_mm_yyyy", _("DD.MM.YYYY")
    YYYY_MM_DD = "yyyy_mm_dd", _("YYYY/MM/DD")
    LONG_EU = "long_eu", _("18 Jan, 2026")


class ThemeChoices(models.TextChoices):
    """Choices for UI theme preference."""

    SYSTEM = "system", _("System default")
    LIGHT = "light", _("Light")
    DARK = "dark", _("Dark")
    CATPPUCCIN_MOCHA = "catppuccin_mocha", _("Catppuccin Mocha")
    DRACULA = "dracula", _("Dracula")
    NORD = "nord", _("Nord")
    GRUVBOX = "gruvbox", _("Gruvbox")
    OLED = "oled", _("OLED")
    GLASS = "glass", _("Glass cinema")
    PLEX = "plex", _("Plex inspired")
    PROJECTOR = "projector", _("Projector")
    VIDEO_STORE = "video_store", _("Video store")
    CUSTOM = "custom", _("Custom palette")


class UiLanguageChoices(models.TextChoices):
    """Choices for UI display language preference."""

    AUTO = "auto", _("Auto (browser language)")
    EN = "en", "English"
    DE = "de", "Deutsch"
    ES = "es", "Español"
    FR = "fr", "Français"


class LogoStyleChoices(models.TextChoices):
    """Choices for the Floppy logo style preference."""

    COLORFUL = "colorful", _("Original color")
    MONOCHROME = "monochrome", _("Monochrome")
    TEXT = "text", _("Text")
    CUSTOM = "custom", _("Custom image")
    HIDDEN = "hidden", _("Hidden")


class LogoTextFontChoices(models.TextChoices):
    """Safe local font stacks available to text wordmarks."""

    DISPLAY = "display", _("Floppy display")
    SANS = "sans", _("Clean sans")
    SERIF = "serif", _("Editorial serif")
    MONO = "mono", _("Technical mono")


class LogoTextWeightChoices(models.IntegerChoices):
    """Font weights available to text wordmarks."""

    REGULAR = 400, _("Regular")
    MEDIUM = 500, _("Medium")
    SEMIBOLD = 600, _("Semibold")
    BOLD = 700, _("Bold")
    EXTRABOLD = 800, _("Extra bold")
    BLACK = 900, _("Black")


LOGO_TEXT_SIZES = tuple(range(16, 41))
LOGO_TEXT_SPACINGS = tuple(range(-2, 7))


class TimeFormatChoices(models.TextChoices):
    """Choices for time format preferences."""

    SYSTEM_DEFAULT = "system_default", _("System default")
    H_MM_AMPM = "h_mm_ampm", _("12-hour (h:mm AM/PM)")
    HH_MM_AMPM = "hh_mm_ampm", _("12-hour, leading zero (hh:mm AM/PM)")
    HH_MM = "hh_mm", _("24-hour (HH:mm)")
    HH_MM_SS = "hh_mm_ss", _("24-hour with seconds (HH:mm:ss)")


class WeekStartDayChoices(models.TextChoices):
    """Choices for week start day preference."""

    MONDAY = "monday", _("Monday")
    SUNDAY = "sunday", _("Sunday")


class RatingScaleChoices(models.TextChoices):
    """Choices for rating scale preferences."""

    TEN = "10", _("1-10 stars")
    FIVE = "5", _("1-5 stars")


class ActivityHistoryViewChoices(models.TextChoices):
    """Choices for which activity history view to show on the statistics page."""

    HEATMAP = "heatmap", _("Activity Heatmap")
    STACKED = "stacked", _("Stacked Bar Chart")


class DurationFormatChoices(models.TextChoices):
    """Choices for how long durations are displayed."""

    HOURS_MINUTES = "hours_minutes", _("Hours and minutes (500h 30min)")
    LONG_UNITS = "long_units", _("Days and hours (20d 20h 30min)")


class StatisticsRangeChoices(models.TextChoices):
    """Choices for predefined statistics date ranges."""

    TODAY = "Today", _("Today")
    YESTERDAY = "Yesterday", _("Yesterday")
    THIS_WEEK = "This Week", _("This Week")
    LAST_7_DAYS = "Last 7 Days", _("Last 7 Days")
    THIS_MONTH = "This Month", _("This Month")
    LAST_30_DAYS = "Last 30 Days", _("Last 30 Days")
    LAST_90_DAYS = "Last 90 Days", _("Last 90 Days")
    THIS_YEAR = "This Year", _("This Year")
    LAST_6_MONTHS = "Last 6 Months", _("Last 6 Months")
    LAST_12_MONTHS = "Last 12 Months", _("Last 12 Months")
    ALL_TIME = "All Time", _("All Time")


class ImportFrequencyChoices(models.TextChoices):
    """Import frequency choices."""

    ONCE = "once", _("One Time Import")
    DAILY = "daily", _("Every Day")
    TWO_DAYS = "2days", _("Every 2 Days")


class ImportModeChoices(models.TextChoices):
    """Import mode choices."""

    NEW = "new", _("Only Sync New Items")
    OVERWRITE = "overwrite", _("Sync New Items and Overwrite Existing")
    WATCHLIST = "watchlist", _("Import Watchlist Data Only")
    UPDATE_COLLECTION = "update_collection", _("Update Collection Metadata Only")


class OnboardingStatusChoices(models.TextChoices):
    """Progress state for the first-run setup wizard."""

    NOT_STARTED = "not_started", _("Not Started")
    IN_PROGRESS = "in_progress", _("In Progress")
    COMPLETED = "completed", _("Completed")


class OnboardingStepChoices(models.TextChoices):
    """Step to resume the first-run setup wizard on."""

    MEDIA_TYPES = "media_types", _("Choose Media Types")
    SERVICES = "services", _("Choose Services")
    SERVICES_SUMMARY = "services_summary", _("Review Services")
    SERVICE_SETUP = "service_setup", _("Connect Services")
    IMPORT_STATUS = "import_status", _("Import Status")
    INTEGRATION_SETUP = "integration_setup", _("Set Up Scrobbling")
    DONE = "done", _("Done")


class TopTalentSortChoices(models.TextChoices):
    """Choices for sorting top cast/crew/studio cards on statistics."""

    PLAYS = "plays", _("Plays")
    TIME = "time", _("Time")
    TITLES = "titles", _("Titles")


class GenreSortChoices(models.TextChoices):
    """Choices for sorting the Taste Signals Top Genres panel on statistics."""

    TIME = "time", _("Time")
    PLAYS = "plays", _("Plays")


class StatisticsCompareChoices(models.TextChoices):
    """Choices for the default comparison mode on the statistics page."""

    PREVIOUS_PERIOD = "previous_period", _("Previous period")
    LAST_YEAR = "last_year", _("Last year")
    NONE = "none", _("No comparison")


class GameLoggingStyleChoices(models.TextChoices):
    """Choices for how game history entries are displayed."""

    SESSIONS = "sessions", _("Sessions")
    REPEATS = "repeats", _("Repeats")


class MobileGridLayoutChoices(models.TextChoices):
    """Choices for mobile grid layout preference."""

    COMFORTABLE = "comfortable", _("Comfortable (2 columns)")
    COMPACT = "compact", _("Compact (3 columns)")


class QuickSeasonUpdateChoices(models.TextChoices):
    """Controls quick season update buttons and the next-episode pill on home cards."""

    NONE = "none", _("None")
    SEASON_UPDATE = "season_update", _("Quick Season Update buttons only")
    NEXT_EPISODE = "next_episode", _("Next Episode button only")
    BOTH = "both", _("Both")


class MediaCardSubtitleDisplayChoices(models.TextChoices):
    """Choices for media card subtitle visibility."""

    HOVER = "hover", _("On hover")
    ALWAYS = "always", _("Always visible")


class TitleDisplayPreferenceChoices(models.TextChoices):
    """Choices for how item titles are displayed across the app."""

    LOCALIZED = "localized", _("Show Localized Titles")
    ORIGINAL = "original", _("Show Original Titles")
    AUTO = "auto", _("Auto (if available)")


class PlannedHomeDisplayChoices(models.TextChoices):
    """Choices for how planned items are displayed on home page."""

    DISABLED = "disabled", _("Disabled")
    COMBINED = "combined", _("Combined")
    SEPARATED = "separated", _("Separated")


class HomeScreenRowTypeChoices(models.TextChoices):
    """Supported row sources for the configurable home screen."""

    LIBRARY_QUERY = "library_query", _("Library Row")
    CUSTOM_LIST = "custom_list", _("List / Smart List")
    RECENTLY_UNRATED = "recently_unrated", _("Recently Played - Not Rated")


# kept: unrenamed model field/class names and help_text below (avoids a migration; see plan)
class JellyseerrDefaultAddedStatusChoices(models.TextChoices):
    """Choices for status applied to media added via Jellyseerr webhook."""

    PLANNING = Status.PLANNING.value, _("Planning")
    IN_PROGRESS = Status.IN_PROGRESS.value, _("In Progress")


class QuickWatchDateChoices(models.TextChoices):
    """Choices for quick watch date behavior when bulk-marking media as completed."""

    CURRENT_DATE = "current_date", _("Current Date")
    RELEASE_DATE = "release_date", _("Release Date")
    NO_DATE = "no_date", _("No Date")


class MetadataSourceDefaultChoices(models.TextChoices):
    """Choices for library metadata defaults."""

    TMDB = Sources.TMDB.value, Sources.TMDB.label
    TVDB = Sources.TVDB.value, Sources.TVDB.label
    MAL = Sources.MAL.value, Sources.MAL.label


class AnimeLibraryModeChoices(models.TextChoices):
    """Choices for where grouped anime should surface in the UI."""

    ANIME = MediaTypes.ANIME.value, _("Anime Library")
    TV = MediaTypes.TV.value, _("TV Library")
    BOTH = "both", _("Both Libraries")


class SessionDurationChoices(models.IntegerChoices):
    """Choices for how long a login session persists."""

    ONE_DAY = 86400, _("1 day")
    ONE_WEEK = 604800, _("1 week")
    TWO_WEEKS = 1209600, _("2 weeks")
    ONE_MONTH = 2592000, _("30 days")
    THREE_MONTHS = 7776000, _("90 days")


class User(AbstractUser):
    """Custom user model."""

    is_demo = models.BooleanField(default=False)
    is_test_account = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Exclude this account from normal user pickers and sharing controls.",
    )

    objects = FloppyUserManager()

    last_search_type = models.CharField(
        max_length=10,
        default=MediaTypes.TV.value,
        choices=MediaTypes.choices,
    )

    last_discover_type = models.CharField(
        max_length=10,
        default="",
        blank=True,
    )

    home_sort = models.CharField(
        max_length=20,
        default=HomeSortChoices.UPCOMING,
        choices=HomeSortChoices,
    )

    # Media type preferences: TV Shows
    tv_enabled = models.BooleanField(default=True)
    tv_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices,
    )
    tv_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    tv_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    tv_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
    )

    # Media type preferences: TV Seasons
    season_enabled = models.BooleanField(default=True)
    season_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices,
    )
    season_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    season_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    season_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
    )

    # Media type preferences: Movies
    movie_enabled = models.BooleanField(default=True)
    movie_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices,
    )
    movie_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    movie_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    movie_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
    )
    movie_show_each_play = models.BooleanField(
        default=False,
        help_text="Show each play/entry as its own row instead of aggregating duplicates",
    )

    # Media type preferences: Anime
    anime_enabled = models.BooleanField(default=True)
    anime_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.TABLE,
        choices=LayoutChoices,
    )
    anime_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    anime_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    anime_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
    )
    anime_show_each_play = models.BooleanField(
        default=False,
        help_text="Show each play/entry as its own row instead of aggregating duplicates",
    )

    # Media type preferences: Manga
    manga_enabled = models.BooleanField(default=True)
    manga_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.TABLE,
        choices=LayoutChoices,
    )
    manga_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    manga_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    manga_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
    )
    manga_show_each_play = models.BooleanField(
        default=False,
        help_text="Show each play/entry as its own row instead of aggregating duplicates",
    )

    # Media type preferences: Games
    game_enabled = models.BooleanField(default=True)
    game_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices,
    )
    game_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    game_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    game_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
    )
    game_show_each_play = models.BooleanField(
        default=False,
        help_text="Show each play/entry as its own row instead of aggregating duplicates",
    )

    # Media type preferences: Board Games
    boardgame_enabled = models.BooleanField(default=True)
    boardgame_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices.choices,
    )
    boardgame_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    boardgame_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
    )
    boardgame_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices.choices,
    )

    # Media type preferences: Books
    book_enabled = models.BooleanField(default=True)
    book_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices,
    )
    book_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    book_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    book_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
    )
    book_show_each_play = models.BooleanField(
        default=False,
        help_text="Show each play/entry as its own row instead of aggregating duplicates",
    )

    # Media type preferences: Comics
    comic_enabled = models.BooleanField(default=True)
    comic_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices,
    )
    comic_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    comic_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    comic_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
    )

    # Media type preferences: Music
    music_enabled = models.BooleanField(default=True)
    music_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices,
    )
    music_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    music_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    music_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices.choices,
    )

    # Podcast preferences
    podcast_enabled = models.BooleanField(default=True)
    podcast_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices.choices,
    )
    podcast_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    podcast_sort = models.CharField(
        max_length=32,
        default=MediaSortChoices.TITLE,
        choices=MediaSortChoices.choices,
    )
    podcast_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
    )

    # UI preferences
    clickable_media_cards = models.BooleanField(
        default=False,
        help_text="Hide hover overlay on touch devices",
    )
    media_card_subtitle_display = models.CharField(
        max_length=20,
        default=MediaCardSubtitleDisplayChoices.HOVER,
        choices=MediaCardSubtitleDisplayChoices.choices,
        help_text="Control when media card subtitles are visible",
    )
    title_display_preference = models.CharField(
        max_length=20,
        default=TitleDisplayPreferenceChoices.LOCALIZED,
        choices=TitleDisplayPreferenceChoices.choices,
        help_text="Preferred title variant to display in the UI",
    )

    # Tracking settings
    quick_watch_date = models.CharField(
        max_length=20,
        default=QuickWatchDateChoices.CURRENT_DATE,
        choices=QuickWatchDateChoices,
        help_text="Date to use when bulk-marking media as completed",
    )
    rating_scale = models.CharField(
        max_length=2,
        default=RatingScaleChoices.TEN,
        choices=RatingScaleChoices.choices,
        help_text="Preferred rating scale for user scores",
    )

    # Progress visibility preferences
    progress_bar = models.BooleanField(
        default=True,
        help_text="Show progress bar",
    )
    hide_completed_recommendations = models.BooleanField(
        default=False,
        help_text="Hide completed media in recommendations",
    )
    hide_zero_rating = models.BooleanField(
        default=False,
        help_text="Hide zero ratings from media cards",
    )
    obfuscate_episodes = models.BooleanField(
        default=False,
        help_text="Blur unseen episode thumbnails to avoid spoilers",
    )

    # Watch provider region
    watch_provider_region = models.CharField(
        max_length=5,
        default="UNSET",
        help_text="Region to show watch providers for",
    )
    metadata_language = models.CharField(
        max_length=10,
        default="",
        blank=True,
        help_text=(
            "Preferred language for TV/movie metadata and cover art. "
            "Falls back to the server default when unset."
        ),
    )
    tv_metadata_source_default = models.CharField(
        max_length=20,
        default=MetadataSourceDefaultChoices.TMDB,
        choices=[
            (
                MetadataSourceDefaultChoices.TMDB,
                MetadataSourceDefaultChoices.TMDB.label,
            ),
            (
                MetadataSourceDefaultChoices.TVDB,
                MetadataSourceDefaultChoices.TVDB.label,
            ),
        ],
        help_text="Default metadata provider for TV details and search tabs.",
    )
    anime_metadata_source_default = models.CharField(
        max_length=20,
        # TMDB by default so the Anime library gets real season/episode trees,
        # matching how TV Shows behaves. MAL stays available for users who
        # prefer its per-cour entries. Existing users keep their stored value.
        default=MetadataSourceDefaultChoices.TMDB,
        choices=[
            (MetadataSourceDefaultChoices.MAL, MetadataSourceDefaultChoices.MAL.label),
            (
                MetadataSourceDefaultChoices.TMDB,
                MetadataSourceDefaultChoices.TMDB.label,
            ),
            (
                MetadataSourceDefaultChoices.TVDB,
                MetadataSourceDefaultChoices.TVDB.label,
            ),
        ],
        help_text="Default metadata provider for Anime details and search tabs.",
    )
    anime_library_mode = models.CharField(
        max_length=20,
        default=AnimeLibraryModeChoices.ANIME,
        choices=AnimeLibraryModeChoices.choices,
        help_text="Where grouped anime entries should surface in the UI.",
    )
    stats_split_tv_anime = models.BooleanField(
        default=False,
        help_text="When anime is disabled in sidebar, show TVDB-tagged anime as a separate Anime bucket in Statistics.",
    )

    # Calendar preferences
    calendar_layout = models.CharField(
        max_length=20,
        default=CalendarLayoutChoices.GRID,
        choices=CalendarLayoutChoices,
    )

    # Lists preferences
    lists_sort = models.CharField(
        max_length=20,
        default=ListSortChoices.LAST_ITEM_ADDED,
        choices=ListSortChoices,
    )
    lists_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    list_detail_sort = models.CharField(
        max_length=20,
        default=ListDetailSortChoices.DATE_ADDED,
        choices=ListDetailSortChoices,
    )
    list_detail_status = models.CharField(
        max_length=128,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
    )
    list_detail_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices,
    )

    # Notification settings
    notification_urls = models.TextField(
        blank=True,
        help_text="Apprise URLs for notifications",
    )
    notification_excluded_items = models.ManyToManyField(
        Item,
        related_name="excluded_by_users",
        blank=True,
        help_text="Items excluded from notifications",
    )
    release_notifications_enabled = models.BooleanField(
        default=True,
        help_text="Receive notifications for recently released media",
    )
    daily_digest_enabled = models.BooleanField(
        default=True,
        help_text="Receive a daily digest of upcoming releases",
    )
    premiere_notifications_enabled = models.BooleanField(
        default=True,
        help_text="Receive a weekly digest of new show and season premieres",
    )

    # Account recovery and authenticator settings
    authenticator_secret = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="TOTP secret used by authenticator apps",
    )
    authenticator_enabled = models.BooleanField(
        default=False,
        help_text="Whether authenticator app verification is enabled",
    )
    authenticator_confirmed_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp when authenticator setup was confirmed",
    )

    # Integration settings
    token = models.CharField(
        max_length=32,
        unique=True,
        default=generate_token,
        help_text="Token for external integrations",
    )
    plex_usernames = models.TextField(
        blank=True,
        help_text="Comma-separated list of Plex usernames for webhook matching",
    )
    plex_webhook_libraries = models.JSONField(
        blank=True,
        null=True,
        default=None,
        help_text=(
            "List of Plex webhook library keys to accept. "
            "Null means all available libraries."
        ),
    )
    plex_webhook_last_received_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp of the last Plex webhook received",
    )
    plex_webhook_last_error = models.TextField(
        blank=True,
        default="",
        help_text="Last Plex webhook error message",
    )
    plex_webhook_last_error_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp of the last Plex webhook error",
    )
    plex_webhook_token_rotated_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When the API token was regenerated (update webhook URLs)",
    )
    jellyfin_mark_played_enabled = models.BooleanField(
        default=False,
        help_text="Process Jellyfin MarkPlayed webhook events",
    )
    jellyfin_mark_unplayed_enabled = models.BooleanField(
        default=False,
        help_text="Process Jellyfin MarkUnplayed webhook events",
    )

    jellyseerr_enabled = models.BooleanField(
        default=False,
        help_text="Enable Jellyseerr webhook auto-add for this user",
    )
    jellyseerr_allowed_usernames = models.TextField(
        blank=True,
        help_text=(
            "Comma-separated list of Jellyseerr usernames allowed to trigger adds. "
            "Blank = allow all."
        ),
    )
    jellyseerr_trigger_statuses = models.TextField(
        blank=True,
        help_text=(
            "Comma-separated Jellyseerr media_status values that trigger add "
            "(e.g. PENDING,PROCESSING,AVAILABLE). Blank = default behaviour (skips UNKNOWN)."
        ),
    )
    jellyseerr_default_added_status = models.CharField(
        max_length=20,
        choices=JellyseerrDefaultAddedStatusChoices.choices,
        default=Status.PLANNING.value,
        help_text="Status to set when adding media via Jellyseerr webhook",
    )
    tmdb_proxy_url = models.TextField(
        blank=True,
        help_text=(
            "Encrypted outbound proxy URL for TMDB requests "
            "(e.g. socks5://user:pass@host:port)"
        ),
    )
    date_format = models.CharField(
        max_length=20,
        default=DateFormatChoices.SYSTEM_DEFAULT,
        choices=DateFormatChoices.choices,
    )

    theme = models.CharField(
        max_length=20,
        default=ThemeChoices.SYSTEM,
        choices=ThemeChoices.choices,
    )

    custom_theme = models.JSONField(
        default=dict,
        blank=True,
        help_text="Validated custom application color palette",
    )

    detail_page_layouts = models.JSONField(
        default=dict,
        blank=True,
        help_text="Visible and ordered sections for each detail page family",
    )

    ui_language = models.CharField(
        max_length=10,
        default=UiLanguageChoices.AUTO,
        choices=UiLanguageChoices.choices,
        help_text="Preferred UI display language",
    )

    logo_style = models.CharField(
        max_length=12,
        default=LogoStyleChoices.COLORFUL,
        choices=LogoStyleChoices.choices,
        help_text="Preferred Floppy logo style",
    )

    logo_text = models.CharField(
        max_length=32,
        default="Floppy",
        help_text="Short navigation wordmark",
    )

    logo_text_font = models.CharField(
        max_length=12,
        default=LogoTextFontChoices.DISPLAY,
        choices=LogoTextFontChoices.choices,
        help_text="Font family used by the navigation wordmark",
    )

    logo_text_size = models.PositiveSmallIntegerField(
        default=23,
        choices=[(value, f"{value}px") for value in LOGO_TEXT_SIZES],
        help_text="Font size used by the navigation wordmark",
    )

    logo_text_weight = models.PositiveSmallIntegerField(
        default=LogoTextWeightChoices.EXTRABOLD,
        choices=LogoTextWeightChoices.choices,
        help_text="Font weight used by the navigation wordmark",
    )

    logo_text_spacing = models.SmallIntegerField(
        default=-1,
        choices=[(value, f"{value}px") for value in LOGO_TEXT_SPACINGS],
        help_text="Letter spacing used by the navigation wordmark",
    )

    custom_logo_data = models.TextField(
        blank=True,
        default="",
        help_text="Normalized custom navigation logo",
    )

    time_format = models.CharField(
        max_length=20,
        default=TimeFormatChoices.SYSTEM_DEFAULT,
        choices=TimeFormatChoices.choices,
    )

    week_start_day = models.CharField(
        max_length=10,
        default=WeekStartDayChoices.MONDAY,
        choices=WeekStartDayChoices.choices,
    )

    import_frequency = models.CharField(
        max_length=10,
        default=ImportFrequencyChoices.ONCE,
        choices=ImportFrequencyChoices.choices,
    )
    import_time = models.CharField(
        max_length=5,
        default="00:00",
    )
    import_mode = models.CharField(
        max_length=20,
        default=ImportModeChoices.NEW,
        choices=ImportModeChoices.choices,
    )

    onboarding_status = models.CharField(
        max_length=20,
        default=OnboardingStatusChoices.NOT_STARTED,
        choices=OnboardingStatusChoices.choices,
        help_text="Progress state for the first-run setup wizard.",
    )
    onboarding_step = models.CharField(
        max_length=20,
        default=OnboardingStepChoices.MEDIA_TYPES,
        choices=OnboardingStepChoices.choices,
        help_text="Step to resume the first-run setup wizard on.",
    )
    onboarding_selected_sources = models.JSONField(
        default=list,
        blank=True,
        help_text="Source slugs chosen to connect during the setup wizard.",
    )
    onboarding_skipped_sources = models.JSONField(
        default=list,
        blank=True,
        help_text="Source slugs explicitly skipped during the setup wizard.",
    )
    onboarding_connected_sources = models.JSONField(
        default=list,
        blank=True,
        help_text="Source slugs successfully connected during the setup wizard.",
    )
    onboarding_skipped_integrations = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Source slugs whose realtime integration (e.g. a webhook) was "
            "explicitly skipped during the setup wizard."
        ),
    )

    game_logging_style = models.CharField(
        max_length=20,
        default=GameLoggingStyleChoices.REPEATS,
        choices=GameLoggingStyleChoices.choices,
        help_text="How game entries are displayed on the History page",
    )

    statistics_default_range = models.CharField(
        max_length=20,
        default=StatisticsRangeChoices.LAST_12_MONTHS,
        choices=StatisticsRangeChoices.choices,
        help_text="Default predefined range for the Statistics page",
    )
    statistics_compare_mode = models.CharField(
        max_length=20,
        default=StatisticsCompareChoices.PREVIOUS_PERIOD,
        choices=StatisticsCompareChoices.choices,
        help_text="Default comparison mode for finite ranges on the Statistics page",
    )
    top_talent_sort_by = models.CharField(
        max_length=20,
        default=TopTalentSortChoices.PLAYS,
        choices=TopTalentSortChoices.choices,
        help_text="Sort metric for top cast/crew/studio cards on the Statistics page",
    )
    genre_sort_by = models.CharField(
        max_length=20,
        default=GenreSortChoices.TIME,
        choices=GenreSortChoices.choices,
        help_text="Sort metric for the Top Genres panel on the Statistics page",
    )
    studio_sort_by = models.CharField(
        max_length=20,
        default=GenreSortChoices.PLAYS,
        choices=GenreSortChoices.choices,
        help_text="Sort metric for the Studio Footprint card on the Statistics page",
    )

    activity_history_view = models.CharField(
        max_length=20,
        default=ActivityHistoryViewChoices.HEATMAP,
        choices=ActivityHistoryViewChoices.choices,
        help_text="Which activity history visualization to show on the Statistics page",
    )
    duration_format = models.CharField(
        max_length=20,
        default=DurationFormatChoices.HOURS_MINUTES,
        choices=DurationFormatChoices.choices,
        help_text="How long durations are displayed on the Statistics page",
    )
    mobile_grid_layout = models.CharField(
        max_length=20,
        default=MobileGridLayoutChoices.COMPACT,
        choices=MobileGridLayoutChoices.choices,
        help_text="Number of columns to show on mobile layouts",
    )
    quick_season_update_mobile = models.CharField(
        max_length=20,
        default=QuickSeasonUpdateChoices.NONE,
        choices=QuickSeasonUpdateChoices.choices,
        help_text="Controls quick season update buttons and next-episode pill on home screen cards",
    )
    show_planned_on_home = models.CharField(
        max_length=20,
        default=PlannedHomeDisplayChoices.DISABLED,
        choices=PlannedHomeDisplayChoices.choices,
        help_text="Show planned items on the home screen alongside in-progress items",
    )
    home_show_media_type_headers = models.BooleanField(
        default=False,
        help_text="Show a media-type header (icon + name) above each group of home screen rows",
    )
    home_screen_media_type_order = models.JSONField(
        default=list,
        blank=True,
        help_text="User's preferred order of media-type sections on the Home screen",
    )
    sidebar_media_type_order = models.JSONField(
        default=list,
        blank=True,
        help_text="User's preferred order of media types in the sidebar",
    )
    auto_pause_in_progress_enabled = models.BooleanField(
        default=False,
        help_text="Automatically pause stale in-progress items",
    )
    auto_pause_rules = models.JSONField(
        default=list,
        blank=True,
        help_text="Auto-pause rules with per-library week thresholds",
    )
    table_column_prefs = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-library table column order and hidden keys",
    )
    pinned_watch_providers = models.JSONField(
        default=list,
        blank=True,
        help_text="Watch-provider names pinned/favorited by the user, promoted out of 'More'",
    )
    book_comic_manga_progress_percentage = models.BooleanField(
        default=False,
        help_text="Track book, comic, and manga progress as percentage instead of pages/issues/chapters",
    )
    session_duration = models.IntegerField(
        default=SessionDurationChoices.TWO_WEEKS,
        choices=SessionDurationChoices.choices,
        help_text="How long a login session persists before requiring re-authentication",
    )

    class Meta:
        """Meta options for the model."""

        ordering = ["username"]
        constraints = [
            models.CheckConstraint(
                name="last_search_type_valid",
                condition=models.Q(last_search_type__in=VALID_SEARCH_TYPES),
            ),
            models.CheckConstraint(
                name="home_sort_valid",
                condition=models.Q(home_sort__in=HomeSortChoices.values),
            ),
            models.CheckConstraint(
                name="tv_layout_valid",
                condition=models.Q(tv_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="season_layout_valid",
                condition=models.Q(season_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="movie_layout_valid",
                condition=models.Q(movie_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="anime_layout_valid",
                condition=models.Q(anime_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="manga_layout_valid",
                condition=models.Q(manga_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="game_layout_valid",
                condition=models.Q(game_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="book_layout_valid",
                condition=models.Q(book_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="tv_sort_valid",
                condition=models.Q(tv_sort__in=MediaSortChoices.values),
            ),
            models.CheckConstraint(
                name="tv_direction_valid",
                condition=models.Q(tv_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="season_sort_valid",
                condition=models.Q(season_sort__in=MediaSortChoices.values),
            ),
            models.CheckConstraint(
                name="season_direction_valid",
                condition=models.Q(season_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="movie_sort_valid",
                condition=models.Q(movie_sort__in=MediaSortChoices.values),
            ),
            models.CheckConstraint(
                name="movie_direction_valid",
                condition=models.Q(movie_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="anime_sort_valid",
                condition=models.Q(anime_sort__in=MediaSortChoices.values),
            ),
            models.CheckConstraint(
                name="anime_direction_valid",
                condition=models.Q(anime_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="manga_sort_valid",
                condition=models.Q(manga_sort__in=MediaSortChoices.values),
            ),
            models.CheckConstraint(
                name="manga_direction_valid",
                condition=models.Q(manga_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="game_sort_valid",
                condition=models.Q(game_sort__in=MediaSortChoices.values),
            ),
            models.CheckConstraint(
                name="game_direction_valid",
                condition=models.Q(game_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="boardgame_layout_valid",
                condition=models.Q(boardgame_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="boardgame_sort_valid",
                condition=models.Q(boardgame_sort__in=MediaSortChoices.values),
            ),
            models.CheckConstraint(
                name="boardgame_direction_valid",
                condition=models.Q(boardgame_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="book_sort_valid",
                condition=models.Q(book_sort__in=MediaSortChoices.values),
            ),
            models.CheckConstraint(
                name="book_direction_valid",
                condition=models.Q(book_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="comic_direction_valid",
                condition=models.Q(comic_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="calendar_layout_valid",
                condition=models.Q(calendar_layout__in=CalendarLayoutChoices.values),
            ),
            models.CheckConstraint(
                name="tv_metadata_source_default_valid",
                condition=models.Q(
                    tv_metadata_source_default__in=[
                        MetadataSourceDefaultChoices.TMDB,
                        MetadataSourceDefaultChoices.TVDB,
                    ],
                ),
            ),
            models.CheckConstraint(
                name="anime_metadata_source_default_valid",
                condition=models.Q(
                    anime_metadata_source_default__in=[
                        MetadataSourceDefaultChoices.MAL,
                        MetadataSourceDefaultChoices.TMDB,
                        MetadataSourceDefaultChoices.TVDB,
                    ],
                ),
            ),
            models.CheckConstraint(
                name="anime_library_mode_valid",
                condition=models.Q(
                    anime_library_mode__in=AnimeLibraryModeChoices.values
                ),
            ),
            models.CheckConstraint(
                name="lists_sort_valid",
                condition=models.Q(lists_sort__in=ListSortChoices.values),
            ),
            models.CheckConstraint(
                name="lists_direction_valid",
                condition=models.Q(lists_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="activity_history_view_valid",
                condition=models.Q(
                    activity_history_view__in=ActivityHistoryViewChoices.values
                ),
            ),
            models.CheckConstraint(
                name="duration_format_valid",
                condition=models.Q(duration_format__in=DurationFormatChoices.values),
            ),
            models.CheckConstraint(
                name="media_card_subtitle_display_valid",
                condition=models.Q(
                    media_card_subtitle_display__in=MediaCardSubtitleDisplayChoices.values
                ),
            ),
            models.CheckConstraint(
                name="title_display_preference_valid",
                condition=models.Q(
                    title_display_preference__in=TitleDisplayPreferenceChoices.values
                ),
            ),
            models.CheckConstraint(
                name="statistics_default_range_valid",
                condition=models.Q(
                    statistics_default_range__in=StatisticsRangeChoices.values
                ),
            ),
            models.CheckConstraint(
                name="statistics_compare_mode_valid",
                condition=models.Q(
                    statistics_compare_mode__in=StatisticsCompareChoices.values
                ),
            ),
            models.CheckConstraint(
                name="top_talent_sort_by_valid",
                condition=models.Q(top_talent_sort_by__in=TopTalentSortChoices.values),
            ),
            models.CheckConstraint(
                name="genre_sort_by_valid",
                condition=models.Q(genre_sort_by__in=GenreSortChoices.values),
            ),
            models.CheckConstraint(
                name="studio_sort_by_valid",
                condition=models.Q(studio_sort_by__in=GenreSortChoices.values),
            ),
            models.CheckConstraint(
                name="list_detail_sort_valid",
                condition=models.Q(list_detail_sort__in=ListDetailSortChoices.values),
            ),
            models.CheckConstraint(
                name="list_detail_layout_valid",
                condition=models.Q(list_detail_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="music_layout_valid",
                condition=models.Q(music_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="music_sort_valid",
                condition=models.Q(music_sort__in=MediaSortChoices.values),
            ),
            models.CheckConstraint(
                name="music_direction_valid",
                condition=models.Q(music_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="podcast_layout_valid",
                condition=models.Q(podcast_layout__in=LayoutChoices.values),
            ),
            models.CheckConstraint(
                name="podcast_sort_valid",
                condition=models.Q(podcast_sort__in=MediaSortChoices.values),
            ),
            models.CheckConstraint(
                name="podcast_direction_valid",
                condition=models.Q(podcast_direction__in=DirectionChoices.values),
            ),
            models.CheckConstraint(
                name="quick_watch_date_valid",
                condition=models.Q(quick_watch_date__in=QuickWatchDateChoices.values),
            ),
            models.CheckConstraint(
                name="rating_scale_valid",
                condition=models.Q(rating_scale__in=RatingScaleChoices.values),
            ),
            models.CheckConstraint(
                name="week_start_day_valid",
                condition=models.Q(week_start_day__in=WeekStartDayChoices.values),
            ),
        ]

    def update_preference(self, field_name, new_value):
        """
        Update user preference if the new value is valid and different from current.

        Args:
            field_name: The name of the field to update
            new_value: The new value to set

        Returns:
            The value that was set (or the original value if invalid)
        """
        # If no new value provided, return current value
        if new_value is None:
            return getattr(self, field_name)

        # Special case for last_search_type
        if field_name == "last_search_type" and new_value not in VALID_SEARCH_TYPES:
            return getattr(self, field_name)

        # Media-type status preferences hold a comma-joined list of statuses
        # (multi-select filter), so each token is validated individually
        # instead of the field's own single-choice `choices`.
        if field_name in MULTI_STATUS_PREFERENCE_FIELDS:
            tokens = [token for token in str(new_value or "").split(",") if token]
            if any(token not in MediaStatusChoices.values for token in tokens):
                return getattr(self, field_name)
            current_value = getattr(self, field_name)
            if new_value != current_value:
                setattr(self, field_name, new_value)
                self.save(update_fields=[field_name])
            return new_value

        field = self._meta.get_field(field_name)
        # Check if the field has choices
        if hasattr(field, "choices") and field.choices:
            # Get valid values from field choices
            valid_values = [choice[0] for choice in field.choices]

            # If the new value is not valid, return current value
            if new_value not in valid_values:
                return getattr(self, field_name)

        # Get current value
        current_value = getattr(self, field_name)

        # Update if different
        if new_value != current_value:
            setattr(self, field_name, new_value)
            self.save(update_fields=[field_name])

        return new_value

    def update_column_prefs(self, media_type, table_type, order, hidden):
        """Persist sanitized table prefs where order/hidden represent flexible columns."""
        prefs = dict(self.table_column_prefs or {})
        existing = prefs.get(media_type, {})

        if table_type == "media":
            if isinstance(existing, dict) and (
                not existing or "order" in existing or "hidden" in existing
            ):
                media_prefs = dict(existing)
                media_prefs["order"] = list(order)
                media_prefs["hidden"] = list(hidden)
                prefs[media_type] = media_prefs
            elif isinstance(existing, dict):
                scoped_prefs = dict(existing)
                scoped_prefs["media"] = {
                    "order": list(order),
                    "hidden": list(hidden),
                }
                prefs[media_type] = scoped_prefs
            else:
                prefs[media_type] = {
                    "order": list(order),
                    "hidden": list(hidden),
                }
        else:
            if isinstance(existing, dict) and (
                "order" in existing or "hidden" in existing
            ):
                scoped_prefs = {
                    key: value
                    for key, value in existing.items()
                    if key not in {"order", "hidden"}
                }
                scoped_prefs["media"] = {
                    "order": list(existing.get("order", [])),
                    "hidden": list(existing.get("hidden", [])),
                }
            elif isinstance(existing, dict):
                scoped_prefs = dict(existing)
            else:
                scoped_prefs = {}

            scoped_prefs[table_type] = {
                "order": list(order),
                "hidden": list(hidden),
            }
            prefs[media_type] = scoped_prefs

        if prefs != self.table_column_prefs:
            self.table_column_prefs = prefs
            self.save(update_fields=["table_column_prefs"])

        return prefs[media_type]

    def toggle_pinned_provider(self, provider_name):
        """Pin or unpin a watch-provider name, returning the updated pinned list."""
        pinned = list(self.pinned_watch_providers or [])
        if provider_name in pinned:
            pinned.remove(provider_name)
        else:
            pinned.append(provider_name)

        self.pinned_watch_providers = pinned
        self.save(update_fields=["pinned_watch_providers"])

        return pinned

    @property
    def rating_scale_max(self):
        """Return the max rating value for the user's configured scale."""
        try:
            return int(self.rating_scale)
        except (TypeError, ValueError):
            return 10

    def _coerce_score_decimal(self, score):
        """Coerce a score into a Decimal, returning None on failure."""
        if score is None:
            return None
        if isinstance(score, Decimal):
            return score
        try:
            return Decimal(str(score))
        except (InvalidOperation, TypeError, ValueError):
            return None

    def scale_score_for_display(self, score):
        """Convert internal scores (0-10) to the user's display scale."""
        score_decimal = self._coerce_score_decimal(score)
        if score_decimal is None:
            return None
        if self.rating_scale_max == FIVE_POINT_RATING_SCALE:
            score_decimal = score_decimal / Decimal(2)
        return score_decimal.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    def scale_score_for_storage(self, score):
        """Convert display scores to internal 0-10 scale for storage."""
        score_decimal = self._coerce_score_decimal(score)
        if score_decimal is None:
            return None
        if self.rating_scale_max == FIVE_POINT_RATING_SCALE:
            score_decimal = score_decimal * Decimal(2)
        score_decimal = score_decimal.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if score_decimal < 0:
            return Decimal(0)
        if score_decimal > MAX_INTERNAL_RATING_SCORE:
            return Decimal(10)
        return score_decimal

    def format_score_for_display(self, score):
        """Return score formatted for display based on rating scale."""
        score_decimal = self.scale_score_for_display(score)
        if score_decimal is None:
            return None
        if score_decimal == score_decimal.to_integral_value():
            return int(score_decimal)
        return float(score_decimal)

    def resolve_watch_date(self, now, release_date):
        """
        Resolve the appropriate watch date based on user preference.

        Args:
            now: Pre-calculated current datetime
            release_date: The release/air date for the specific media item

        Returns:
            datetime or None based on user preference
        """
        if self.quick_watch_date == QuickWatchDateChoices.NO_DATE:
            return None

        if self.quick_watch_date == QuickWatchDateChoices.RELEASE_DATE:
            return release_date  # Will be None if not available in metadata

        # CURRENT_DATE is the default
        return now

    def get_enabled_media_types(self):
        """Return a list of enabled media type values based on user preferences."""
        enabled_types = []

        for media_type in MediaTypes.values:
            if media_type == MediaTypes.EPISODE.value:
                continue

            enabled_field = f"{media_type}_enabled"
            if getattr(self, enabled_field, False):
                enabled_types.append(media_type)

        return enabled_types

    def get_sidebar_media_types(self):
        """Return enabled media types in the user's preferred sidebar order."""
        enabled_types = [
            media_type
            for media_type in self.get_enabled_media_types()
            if media_type != MediaTypes.COMIC_ISSUE.value
        ]
        preferred_order = self.sidebar_media_type_order or []
        ordered = [
            media_type for media_type in preferred_order if media_type in enabled_types
        ]
        return ordered + [
            media_type for media_type in enabled_types if media_type not in ordered
        ]

    def get_active_media_types(self):
        """Return a list of active media type values based on user preferences."""
        enabled_types = self.get_enabled_media_types()

        # Legacy fallback: if a historical user record predates `season_enabled`
        # but has TV enabled, include seasons as active.
        season_pref = getattr(self, "season_enabled", None)
        if (
            MediaTypes.TV.value in enabled_types
            and MediaTypes.SEASON.value not in enabled_types
            and season_pref is None
        ):
            enabled_types.insert(0, MediaTypes.SEASON.value)

        return enabled_types

    def get_auto_pause_rule(self, media_type: str):
        """Return the most specific auto-pause rule for a media type."""
        if not self.auto_pause_in_progress_enabled:
            return None

        if not self.auto_pause_rules:
            return None

        # Exact match overrides "all"
        for rule in self.auto_pause_rules:
            if rule.get("library") == media_type:
                return rule

        for rule in self.auto_pause_rules:
            if rule.get("library") == "all":
                return rule

        return None

    def get_import_tasks(self):
        """Return import tasks history and schedules for the user."""
        result_task_names = {
            "trakt": [
                "Import from Trakt",
                "Import Trakt data export",
                "Import Trakt collection CSV",
            ],
            "simkl": ["Import from SIMKL"],
            "myanimelist": ["Import from MyAnimeList"],
            "anilist": ["Import from AniList"],
            "kitsu": ["Import from Kitsu"],
            "yamtrack": ["Import from Yamtrack"],
            "hltb": ["Import from HowLongToBeat"],
            "grouvee": ["Import from Grouvee"],
            "steam": ["Import from Steam"],
            "xbox": ["Import from Xbox", "Import from Xbox (Recurring)"],
            "psn": ["Import from PSN", "Import from PSN (Recurring)"],
            "imdb": ["Import from IMDB"],
            "goodreads": [
                "Import from Goodreads",
                "Import from GoodReads",
                "integrations.tasks.import_goodreads",
            ],
            "mdblist": ["Import from MDBList", "Import MDBList Lists"],
            "plex": ["Import from Plex", "Sync Plex Watchlist"],
            "jellyfin_playback_reporting": [
                "Import from Jellyfin Playback Reporting",
            ],
            "radarr": ["Import from Radarr", "Import from Radarr (Recurring)"],
            "sonarr": ["Import from Sonarr", "Import from Sonarr (Recurring)"],
            "audiobookshelf": [
                "Import from Audiobookshelf",
                "Import from Audiobookshelf (Recurring)",
            ],
            "storyteller": [
                "Import from Storyteller",
                "Import from Storyteller (Recurring)",
            ],
            "koreader": ["Import from KOReader"],
            "pocketcasts": [
                "Import from Pocket Casts",
                "Import from Pocket Casts (Recurring)",
            ],
            "gpodder": ["Import from GPodder", "Import from GPodder (Recurring)"],
            "stremio": [
                "Import from Stremio",
                "Import from Stremio (Recurring)",
            ],
            "lastfm": ["Import from Last.fm History"],
            "hardcover": ["Import from Hardcover"],
            "storygraph": ["Import from StoryGraph"],
            "koito": ["Import from Koito History"],
        }
        schedule_task_names = {
            **result_task_names,
            "radarr": ["Import from Radarr (Recurring)"],
            "sonarr": ["Import from Sonarr (Recurring)"],
            "audiobookshelf": ["Import from Audiobookshelf (Recurring)"],
            "storyteller": ["Import from Storyteller (Recurring)"],
            "pocketcasts": ["Import from Pocket Casts (Recurring)"],
            "gpodder": ["Import from GPodder (Recurring)"],
            "xbox": ["Import from Xbox (Recurring)"],
            "psn": ["Import from PSN (Recurring)"],
            "stremio": ["Import from Stremio (Recurring)"],
            "lastfm": ["Poll Last.fm for all users"],
            "koito": ["Poll Koito for user"],
        }

        # Reverse mapping to get source from task name
        result_task_to_source = {
            task_name: source
            for source, task_names in result_task_names.items()
            for task_name in task_names
        }
        result_import_task_names = list(result_task_to_source)
        schedule_task_to_source = {
            task_name: source
            for source, task_names in schedule_task_names.items()
            for task_name in task_names
        }
        schedule_import_task_names = list(schedule_task_to_source)

        task_result_filters = (
            Q(task_kwargs__contains=f"'user_id': {self.id},")
            | Q(task_kwargs__contains=f"'user_id': {self.id}" + "}")
            | Q(task_kwargs__contains=f'"user_id": {self.id},')
            | Q(task_kwargs__contains=f'"user_id": {self.id}' + "}")
        )

        # Get all task results for this user (last 7 days only).
        # Exclude stale PENDING records (created >30 min ago and never updated)
        # — these represent tasks lost to worker crashes or queue buildup.
        seven_days_ago = timezone.now() - timedelta(days=7)
        pending_cutoff = timezone.now() - timedelta(minutes=30)
        task_results = (
            TaskResult.objects.filter(
                task_result_filters,
                task_name__in=result_import_task_names,
                date_created__gte=seven_days_ago,
            )
            .exclude(
                status=states.PENDING,
                date_created__lt=pending_cutoff,
            )
            .order_by("-date_done", "-date_created")
        )

        # Build results list
        results = []
        for task in task_results:
            if task.status in {states.PENDING, states.STARTED}:
                async_result = AsyncResult(task.task_id)
                if async_result.status != task.status:
                    task.status = async_result.status
                    task.result = async_result.result
                    task.traceback = async_result.traceback
                    task.date_done = async_result.date_done or timezone.now()
                    task.save(
                        update_fields=["status", "result", "traceback", "date_done"]
                    )
            elif task.status == states.FAILURE and not task.traceback:
                async_result = AsyncResult(task.task_id)
                if async_result.traceback:
                    task.traceback = async_result.traceback
                    task.save(update_fields=["traceback"])

            source = result_task_to_source[task.task_name]
            processed_task = helpers.process_task_result(task)

            progress = None
            if task.status == states.STARTED:
                progress = import_progress.get_progress(task.task_id)

            results.append(
                {
                    "task": processed_task,
                    "source": source,
                    "date": task.date_done,
                    "status": task.status,
                    "summary": processed_task.summary,
                    "errors": processed_task.errors,
                    "progress_current": progress.get("current") if progress else None,
                    "progress_total": progress.get("total") if progress else None,
                    "progress_label": progress.get("label") if progress else None,
                    "progress_percent": (
                        round(progress["current"] / progress["total"] * 100, 1)
                        if progress and progress.get("total")
                        else None
                    ),
                },
            )

        # Synthetic history entry for Last.fm automatic polling (global task has no per-user result)
        if (
            hasattr(self, "lastfm_account")
            and self.lastfm_account
            and self.lastfm_account.is_connected
            and self.lastfm_account.last_sync_at
            and self.lastfm_account.last_sync_at >= seven_days_ago
        ):
            results.append(
                {
                    "task": None,
                    "source": "lastfm",
                    "date": self.lastfm_account.last_sync_at,
                    "status": states.SUCCESS,
                    "summary": "Automatic Last.fm sync completed.",
                    "errors": None,
                },
            )

        results.sort(key=lambda r: r["date"] or seven_days_ago, reverse=True)

        # Get periodic tasks with their crontab schedules
        # Match both "user_id": X, (with comma) and "user_id": X} (without comma, last field)
        periodic_tasks_filter = (
            Q(kwargs__contains=f"'user_id': {self.id},")
            | Q(kwargs__contains=f"'user_id': {self.id}" + "}")
            | Q(kwargs__contains=f'"user_id": {self.id},')
            | Q(kwargs__contains=f'"user_id": {self.id}' + "}")
        )
        periodic_tasks = PeriodicTask.objects.filter(
            periodic_tasks_filter,
            task__in=schedule_import_task_names,
            enabled=True,
        ).select_related("crontab", "interval")

        # Build schedules list
        schedules = []
        for periodic_task in periodic_tasks:
            source = schedule_task_to_source.get(periodic_task.task, "unknown")

            # Skip if source is unknown (task not in our mapping)
            if source == "unknown":
                continue

            # Extract username from task name if available
            username = ""
            if " for " in periodic_task.name:
                # Handle both " at " and " (every" patterns
                username_part = periodic_task.name.split(" for ")[1]
                if " at " in username_part:
                    username = username_part.split(" at ")[0]
                elif " (every" in username_part:
                    username = username_part.split(" (every")[0]
                else:
                    username = username_part

            schedule_info = helpers.get_next_run_info(periodic_task)
            if schedule_info:
                schedules.append(
                    {
                        "task": periodic_task,
                        "source": source,
                        "username": username,
                        "last_run": periodic_task.last_run_at,
                        "next_run": schedule_info["next_run"],
                        "schedule": schedule_info["frequency"],
                        "mode": schedule_info["mode"],
                    },
                )

        # Check for global Last.fm task (uses IntervalSchedule, not user-specific)
        if (
            hasattr(self, "lastfm_account")
            and self.lastfm_account
            and self.lastfm_account.is_connected
        ):
            lastfm_task = (
                PeriodicTask.objects.filter(
                    task="Poll Last.fm for all users",
                    enabled=True,
                )
                .select_related("interval")
                .first()
            )

            if lastfm_task and lastfm_task.interval:
                # Calculate next run from interval schedule
                last_run = lastfm_task.last_run_at
                if last_run:
                    # Calculate next run based on interval
                    interval_minutes = lastfm_task.interval.every
                    next_run = last_run + timedelta(minutes=interval_minutes)
                else:
                    # If never run, use start_time or current time
                    next_run = lastfm_task.start_time or timezone.now()
                    interval_minutes = lastfm_task.interval.every

                # Get username from account
                username = self.lastfm_account.lastfm_username

                schedules.append(
                    {
                        "task": lastfm_task,
                        "source": "lastfm",
                        "username": username,
                        "last_run": lastfm_task.last_run_at,
                        "next_run": next_run,
                        "schedule": f"Every {interval_minutes} minutes",
                        "mode": "Only New Items",
                    },
                )

        return {
            "results": results,
            "schedules": schedules,
        }

    def get_export_tasks(self):
        """Return export backup task history and schedules for the user."""
        export_task_name = "Scheduled backup export"

        # Get task results for this user
        task_result_filter_text = f"'user_id': {self.id},"
        seven_days_ago = timezone.now() - timedelta(days=7)
        task_results = TaskResult.objects.filter(
            task_kwargs__contains=task_result_filter_text,
            task_name=export_task_name,
            date_done__gte=seven_days_ago,
        ).order_by("-date_done")

        results = []
        for task in task_results:
            processed_task = helpers.process_task_result(task)
            results.append(
                {
                    "task": processed_task,
                    "date": task.date_done,
                    "status": task.status,
                    "summary": processed_task.summary,
                    "errors": processed_task.errors,
                },
            )

        # Get periodic export schedules
        periodic_tasks_filter_text = f'"user_id": {self.id}'
        periodic_tasks = PeriodicTask.objects.filter(
            task=export_task_name,
            kwargs__contains=periodic_tasks_filter_text,
            enabled=True,
        ).select_related("crontab")

        schedules = []
        for periodic_task in periodic_tasks:
            schedule_info = helpers.get_export_next_run_info(periodic_task)
            if schedule_info:
                schedules.append(
                    {
                        "task": periodic_task,
                        "last_run": periodic_task.last_run_at,
                        "next_run": schedule_info["next_run"],
                        "schedule": schedule_info["frequency"],
                        "media_types": schedule_info["media_types"],
                        "include_lists": schedule_info["include_lists"],
                        "include_collection": schedule_info["include_collection"],
                    },
                )

        return {
            "results": results,
            "schedules": schedules,
        }

    @property
    def has_authenticator_configured(self):
        """Return whether this user has a confirmed authenticator setup."""
        return self.authenticator_enabled and bool(self.authenticator_secret)

    def get_or_create_authenticator_secret(self):
        """Return existing authenticator secret or create one."""
        if self.authenticator_secret:
            return self.authenticator_secret

        import pyotp

        self.authenticator_secret = pyotp.random_base32()
        self.save(update_fields=["authenticator_secret"])
        return self.authenticator_secret

    def build_totp_uri(self):
        """Build provisioning URI for authenticator apps."""
        if not self.authenticator_secret:
            return ""

        import pyotp

        issuer = "Floppy"
        return pyotp.TOTP(self.authenticator_secret).provisioning_uri(
            name=self.username,
            issuer_name=issuer,
        )

    def verify_totp_code(self, code):
        """Return True when the supplied TOTP code is valid."""
        if not self.authenticator_secret:
            return False

        import pyotp

        return bool(
            pyotp.TOTP(self.authenticator_secret).verify(
                str(code).strip(), valid_window=1
            )
        )

    def generate_recovery_codes(self, count=8):
        """Generate one-time recovery codes and persist their hashes."""
        if count <= 0:
            return []

        self.recovery_codes.all().delete()
        codes = []
        for _index in range(count):
            raw_code = secrets.token_hex(4).upper()
            codes.append(raw_code)
            UserRecoveryCode.objects.create(
                user=self,
                code_hash=UserRecoveryCode.hash_code(raw_code),
            )
        return codes

    def regenerate_token(self):
        """Regenerate the user's token."""
        self.token = generate_token()
        self.plex_webhook_token_rotated_at = timezone.now()
        self.save(update_fields=["token", "plex_webhook_token_rotated_at"])

    def mark_plex_webhook_received(self, when=None):
        """Record a successful Plex webhook delivery."""
        when = when or timezone.now()
        self.plex_webhook_last_received_at = when
        self.plex_webhook_last_error = ""
        self.plex_webhook_last_error_at = None
        self.plex_webhook_token_rotated_at = None
        self.save(
            update_fields=[
                "plex_webhook_last_received_at",
                "plex_webhook_last_error",
                "plex_webhook_last_error_at",
                "plex_webhook_token_rotated_at",
            ],
        )

    def mark_plex_webhook_error(self, message, when=None):
        """Record a Plex webhook error for UI visibility."""
        when = when or timezone.now()
        self.plex_webhook_last_error = message
        self.plex_webhook_last_error_at = when
        self.save(
            update_fields=[
                "plex_webhook_last_error",
                "plex_webhook_last_error_at",
            ],
        )


class UserRecoveryCode(models.Model):
    """Single-use recovery code for self-service password reset."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="recovery_codes",
    )
    code_hash = models.CharField(max_length=64, db_index=True)
    used_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Model and field configuration."""

        ordering = ["-created_at"]

    def __str__(self):
        """Return a readable label for this user recovery code."""
        return f"{self.user}"

    @staticmethod
    def hash_code(raw_code):
        """Return a deterministic hash for a recovery code."""
        normalized = str(raw_code).strip().upper()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def matches(self, raw_code):
        """Check if a raw code matches this stored hash."""
        candidate = self.hash_code(raw_code)
        return secrets.compare_digest(candidate, self.code_hash)

    def mark_used(self):
        """Mark this code as used."""
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])


class HomeScreenRow(models.Model):
    """Persisted home screen row configuration owned by a user."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="home_screen_rows",
    )
    media_type = models.CharField(
        max_length=16,
        choices=MediaTypes.choices,
    )
    position = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=True)
    title = models.CharField(max_length=100, blank=True, default="")
    row_type = models.CharField(
        max_length=32,
        choices=HomeScreenRowTypeChoices.choices,
        default=HomeScreenRowTypeChoices.LIBRARY_QUERY,
    )
    custom_list = models.ForeignKey(
        "lists.CustomList",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="home_screen_rows",
    )
    sort_by = models.CharField(max_length=32, default=MediaSortChoices.TITLE)
    direction = models.CharField(
        max_length=4,
        default=DirectionChoices.ASC,
        choices=DirectionChoices.choices,
    )
    filters = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model and field configuration."""

        ordering = ["media_type", "position", "id"]
        constraints = [
            models.CheckConstraint(
                name="home_screen_row_media_type_valid",
                condition=models.Q(media_type__in=VALID_HOME_SCREEN_MEDIA_TYPES),
            ),
            models.CheckConstraint(
                name="home_screen_row_row_type_valid",
                condition=models.Q(row_type__in=HomeScreenRowTypeChoices.values),
            ),
            models.CheckConstraint(
                name="home_screen_row_direction_valid",
                condition=models.Q(direction__in=DirectionChoices.values),
            ),
        ]
        indexes = [
            models.Index(fields=["user", "media_type", "position"]),
            models.Index(fields=["user", "enabled"]),
        ]

    def __str__(self):
        """Return a compact label for admin/debug use."""
        return f"{self.user_id}:{self.media_type}:{self.row_type}:{self.position}"
