import hashlib
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from celery import states
from celery.result import AsyncResult
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django_celery_beat.models import PeriodicTask
from django_celery_results.models import TaskResult

from app.models import Item, MediaTypes, Sources, Status
from users import helpers

EXCLUDED_SEARCH_TYPES = [MediaTypes.SEASON.value, MediaTypes.EPISODE.value]

VALID_SEARCH_TYPES = [
    value for value in MediaTypes.values if value not in EXCLUDED_SEARCH_TYPES
]


def generate_token():
    """Generate a user token."""
    return secrets.token_urlsafe(24)


class HomeSortChoices(models.TextChoices):
    """Choices for home page sort options."""

    UPCOMING = "upcoming", "Upcoming"
    RECENT = "recent", "Recent"
    COMPLETION = "completion", "Completion"
    EPISODES_LEFT = "episodes_left", "Episodes Left"
    TITLE = "title", "Title"


class MediaSortChoices(models.TextChoices):
    """Choices for media list sort options."""

    SCORE = "score", "Rating"
    TITLE = "title", "Title"
    AUTHOR = "author", "Author"
    POPULARITY = "popularity", "Popularity"
    PROGRESS = "progress", "Progress"
    RUNTIME = "runtime", "Runtime"
    TIME_TO_BEAT = "time_to_beat", "Time to Beat"
    PLAYS = "plays", "Plays"
    TIME_WATCHED = "time_watched", "Time Watched"
    RELEASE_DATE = "release_date", "Release Date"
    DATE_ADDED = "date_added", "Date Added"
    START_DATE = "start_date", "Start Date"
    END_DATE = "end_date", "Last Watched"
    TIME_LEFT = "time_left", "Time Left"


class MediaStatusChoices(models.TextChoices):
    """Choices for media list status options."""

    ALL = "All", "All"
    COMPLETED = Status.COMPLETED.value, Status.COMPLETED.label
    IN_PROGRESS = Status.IN_PROGRESS.value, Status.IN_PROGRESS.label
    PLANNING = Status.PLANNING.value, Status.PLANNING.label
    PAUSED = Status.PAUSED.value, Status.PAUSED.label
    DROPPED = Status.DROPPED.value, Status.DROPPED.label


class DirectionChoices(models.TextChoices):
    """Choices for sort direction options."""

    ASC = "asc", "Ascending"
    DESC = "desc", "Descending"


class LayoutChoices(models.TextChoices):
    """Choices for media list layout options."""

    GRID = "grid", "Grid"
    TABLE = "table", "Table"


class CalendarLayoutChoices(models.TextChoices):
    """Choices for calendar layout options."""

    GRID = "grid", "Grid"
    LIST = "list", "List"


class ListSortChoices(models.TextChoices):
    """Choices for list sort options."""

    LAST_ITEM_ADDED = "last_item_added", "Last Item Added"
    LAST_WATCHED = "last_watched", "Last Watched"
    NAME = "name", "Name"
    ITEMS_COUNT = "items_count", "Items Count"
    NEWEST_FIRST = "newest_first", "Newest First"


class ListDetailSortChoices(models.TextChoices):
    """Choices for list detail sort options."""

    DATE_ADDED = "date_added", "Date Added"
    CUSTOM = "custom", "Custom"
    TITLE = "title", "Title"
    MEDIA_TYPE = "media_type", "Media Type"
    RATING = "rating", "Rating"
    PROGRESS = "progress", "Progress"
    RELEASE_DATE = "release_date", "Release Date"
    START_DATE = "start_date", "Start Date"
    END_DATE = "end_date", "End Date"


class DateFormatChoices(models.TextChoices):
    """Choices for date format preferences."""

    SYSTEM_DEFAULT = "system_default", "System default (locale)"
    ISO_8601 = "iso_8601", "ISO 8601"
    MONTH_D_YYYY = "month_d_yyyy", "Month D, YYYY"
    D_MON_YYYY = "d_mon_yyyy", "D Mon YYYY"
    M_D_YYYY = "m_d_yyyy", "M/D/YYYY"
    D_M_YYYY = "d_m_yyyy", "D/M/YYYY"
    DD_MM_YYYY = "dd_mm_yyyy", "DD.MM.YYYY"
    YYYY_MM_DD = "yyyy_mm_dd", "YYYY/MM/DD"


class TimeFormatChoices(models.TextChoices):
    """Choices for time format preferences."""

    SYSTEM_DEFAULT = "system_default", "System default (locale)"
    H_MM_AMPM = "h_mm_ampm", "12-hour (h:mm AM/PM)"
    HH_MM_AMPM = "hh_mm_ampm", "12-hour, leading zero (hh:mm AM/PM)"
    HH_MM = "hh_mm", "24-hour (HH:mm)"
    HH_MM_SS = "hh_mm_ss", "24-hour with seconds (HH:mm:ss)"


class RatingScaleChoices(models.TextChoices):
    """Choices for rating scale preferences."""

    TEN = "10", "1-10 stars"
    FIVE = "5", "1-5 stars"


class ActivityHistoryViewChoices(models.TextChoices):
    """Choices for which activity history view to show on the statistics page."""

    HEATMAP = "heatmap", "Activity Heatmap"
    STACKED = "stacked", "Stacked Bar Chart"


class StatisticsRangeChoices(models.TextChoices):
    """Choices for predefined statistics date ranges."""

    TODAY = "Today", "Today"
    YESTERDAY = "Yesterday", "Yesterday"
    THIS_WEEK = "This Week", "This Week"
    LAST_7_DAYS = "Last 7 Days", "Last 7 Days"
    THIS_MONTH = "This Month", "This Month"
    LAST_30_DAYS = "Last 30 Days", "Last 30 Days"
    LAST_90_DAYS = "Last 90 Days", "Last 90 Days"
    THIS_YEAR = "This Year", "This Year"
    LAST_6_MONTHS = "Last 6 Months", "Last 6 Months"
    LAST_12_MONTHS = "Last 12 Months", "Last 12 Months"
    ALL_TIME = "All Time", "All Time"


class TopTalentSortChoices(models.TextChoices):
    """Choices for sorting top cast/crew/studio cards on statistics."""

    PLAYS = "plays", "Plays"
    TIME = "time", "Time"
    TITLES = "titles", "Titles"


class GameLoggingStyleChoices(models.TextChoices):
    """Choices for how game history entries are displayed."""

    SESSIONS = "sessions", "Sessions"
    REPEATS = "repeats", "Repeats"


class MobileGridLayoutChoices(models.TextChoices):
    """Choices for mobile grid layout preference."""

    COMFORTABLE = "comfortable", "Comfortable (2 columns)"
    COMPACT = "compact", "Compact (3 columns)"


class MediaCardSubtitleDisplayChoices(models.TextChoices):
    """Choices for media card subtitle visibility."""

    HOVER = "hover", "On hover"
    ALWAYS = "always", "Always visible"


class TitleDisplayPreferenceChoices(models.TextChoices):
    """Choices for how item titles are displayed across the app."""

    LOCALIZED = "localized", "Show Localized Titles"
    ORIGINAL = "original", "Show Original Titles"
    AUTO = "auto", "Auto (if available)"


class PlannedHomeDisplayChoices(models.TextChoices):
    """Choices for how planned items are displayed on home page."""

    DISABLED = "disabled", "Disabled"
    COMBINED = "combined", "Combined"
    SEPARATED = "separated", "Separated"


class JellyseerrDefaultAddedStatusChoices(models.TextChoices):
    """Choices for status applied to media added via Jellyseerr webhook."""

    PLANNING = Status.PLANNING.value, Status.PLANNING.label
    IN_PROGRESS = Status.IN_PROGRESS.value, Status.IN_PROGRESS.label


class QuickWatchDateChoices(models.TextChoices):
    """Choices for quick watch date behavior when bulk-marking media as completed."""

    CURRENT_DATE = "current_date", "Current Date"
    RELEASE_DATE = "release_date", "Release Date"
    NO_DATE = "no_date", "No Date"


class MetadataSourceDefaultChoices(models.TextChoices):
    """Choices for library metadata defaults."""

    TMDB = Sources.TMDB.value, Sources.TMDB.label
    TVDB = Sources.TVDB.value, Sources.TVDB.label
    MAL = Sources.MAL.value, Sources.MAL.label


class AnimeLibraryModeChoices(models.TextChoices):
    """Choices for where grouped anime should surface in the UI."""

    ANIME = MediaTypes.ANIME.value, "Anime Library"
    TV = MediaTypes.TV.value, "TV Library"
    BOTH = "both", "Both Libraries"


class User(AbstractUser):
    """Custom user model."""

    is_demo = models.BooleanField(default=False)

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
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    tv_status = models.CharField(
        max_length=20,
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
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    season_status = models.CharField(
        max_length=20,
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
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    movie_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
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
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    anime_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
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
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    manga_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
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
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    game_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
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
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
    )
    boardgame_status = models.CharField(
        max_length=20,
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
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    book_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
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
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    comic_status = models.CharField(
        max_length=20,
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
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices,
    )
    music_status = models.CharField(
        max_length=20,
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
        max_length=20,
        default=MediaSortChoices.TITLE,
        choices=MediaSortChoices.choices,
    )
    podcast_status = models.CharField(
        max_length=20,
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

    # Watch provider region
    watch_provider_region = models.CharField(
        max_length=5,
        default="UNSET",
        help_text="Region to show watch providers for",
    )
    tv_metadata_source_default = models.CharField(
        max_length=20,
        default=MetadataSourceDefaultChoices.TMDB,
        choices=[
            (MetadataSourceDefaultChoices.TMDB, MetadataSourceDefaultChoices.TMDB.label),
            (MetadataSourceDefaultChoices.TVDB, MetadataSourceDefaultChoices.TVDB.label),
        ],
        help_text="Default metadata provider for TV details and search tabs.",
    )
    anime_metadata_source_default = models.CharField(
        max_length=20,
        default=MetadataSourceDefaultChoices.MAL,
        choices=[
            (MetadataSourceDefaultChoices.MAL, MetadataSourceDefaultChoices.MAL.label),
            (MetadataSourceDefaultChoices.TMDB, MetadataSourceDefaultChoices.TMDB.label),
            (MetadataSourceDefaultChoices.TVDB, MetadataSourceDefaultChoices.TVDB.label),
        ],
        help_text="Default metadata provider for Anime details and search tabs.",
    )
    anime_library_mode = models.CharField(
        max_length=20,
        default=AnimeLibraryModeChoices.ANIME,
        choices=AnimeLibraryModeChoices.choices,
        help_text="Where grouped anime entries should surface in the UI.",
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
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices,
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

    date_format = models.CharField(
        max_length=20,
        default=DateFormatChoices.SYSTEM_DEFAULT,
        choices=DateFormatChoices.choices,
    )

    time_format = models.CharField(
        max_length=20,
        default=TimeFormatChoices.SYSTEM_DEFAULT,
        choices=TimeFormatChoices.choices,
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
    top_talent_sort_by = models.CharField(
        max_length=20,
        default=TopTalentSortChoices.PLAYS,
        choices=TopTalentSortChoices.choices,
        help_text="Sort metric for top cast/crew/studio cards on the Statistics page",
    )

    activity_history_view = models.CharField(
        max_length=20,
        default=ActivityHistoryViewChoices.HEATMAP,
        choices=ActivityHistoryViewChoices.choices,
        help_text="Which activity history visualization to show on the Statistics page",
    )
    mobile_grid_layout = models.CharField(
        max_length=20,
        default=MobileGridLayoutChoices.COMPACT,
        choices=MobileGridLayoutChoices.choices,
        help_text="Number of columns to show on mobile layouts",
    )
    quick_season_update_mobile = models.BooleanField(
        default=False,
        help_text="Show the quick season update button on mobile episode lists",
    )
    show_planned_on_home = models.CharField(
        max_length=20,
        default=PlannedHomeDisplayChoices.DISABLED,
        choices=PlannedHomeDisplayChoices.choices,
        help_text="Show planned items on the home screen alongside in-progress items",
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
    book_comic_manga_progress_percentage = models.BooleanField(
        default=False,
        help_text="Track book, comic, and manga progress as percentage instead of pages/issues/chapters",
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
                name="boardgame_status_valid",
                condition=models.Q(boardgame_status__in=MediaStatusChoices.values),
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
                condition=models.Q(anime_library_mode__in=AnimeLibraryModeChoices.values),
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
                condition=models.Q(activity_history_view__in=ActivityHistoryViewChoices.values),
            ),
            models.CheckConstraint(
                name="media_card_subtitle_display_valid",
                condition=models.Q(media_card_subtitle_display__in=MediaCardSubtitleDisplayChoices.values),
            ),
            models.CheckConstraint(
                name="title_display_preference_valid",
                condition=models.Q(title_display_preference__in=TitleDisplayPreferenceChoices.values),
            ),
            models.CheckConstraint(
                name="statistics_default_range_valid",
                condition=models.Q(statistics_default_range__in=StatisticsRangeChoices.values),
            ),
            models.CheckConstraint(
                name="top_talent_sort_by_valid",
                condition=models.Q(top_talent_sort_by__in=TopTalentSortChoices.values),
            ),
            models.CheckConstraint(
                name="list_detail_sort_valid",
                condition=models.Q(list_detail_sort__in=ListDetailSortChoices.values),
            ),
            models.CheckConstraint(
                name="list_detail_status_valid",
                condition=models.Q(list_detail_status__in=MediaStatusChoices.values),
            ),
            models.CheckConstraint(
                name="tv_status_valid",
                condition=models.Q(tv_status__in=MediaStatusChoices.values),
            ),
            models.CheckConstraint(
                name="season_status_valid",
                condition=models.Q(season_status__in=MediaStatusChoices.values),
            ),
            models.CheckConstraint(
                name="movie_status_valid",
                condition=models.Q(movie_status__in=MediaStatusChoices.values),
            ),
            models.CheckConstraint(
                name="anime_status_valid",
                condition=models.Q(anime_status__in=MediaStatusChoices.values),
            ),
            models.CheckConstraint(
                name="manga_status_valid",
                condition=models.Q(manga_status__in=MediaStatusChoices.values),
            ),
            models.CheckConstraint(
                name="game_status_valid",
                condition=models.Q(game_status__in=MediaStatusChoices.values),
            ),
            models.CheckConstraint(
                name="book_status_valid",
                condition=models.Q(book_status__in=MediaStatusChoices.values),
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
                name="music_status_valid",
                condition=models.Q(music_status__in=MediaStatusChoices.values),
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
                name="podcast_status_valid",
                condition=models.Q(podcast_status__in=MediaStatusChoices.values),
            ),
            models.CheckConstraint(
                name="quick_watch_date_valid",
                condition=models.Q(quick_watch_date__in=QuickWatchDateChoices.values),
            ),
            models.CheckConstraint(
                name="rating_scale_valid",
                condition=models.Q(rating_scale__in=RatingScaleChoices.values),
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
            if isinstance(existing, dict) and ("order" in existing or "hidden" in existing):
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
        if self.rating_scale_max == 5:
            score_decimal = score_decimal / Decimal("2")
        return score_decimal.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    def scale_score_for_storage(self, score):
        """Convert display scores to internal 0-10 scale for storage."""
        score_decimal = self._coerce_score_decimal(score)
        if score_decimal is None:
            return None
        if self.rating_scale_max == 5:
            score_decimal = score_decimal * Decimal("2")
        score_decimal = score_decimal.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if score_decimal < 0:
            return Decimal("0")
        if score_decimal > 10:
            return Decimal("10")
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
            "trakt": ["Import from Trakt"],
            "simkl": ["Import from SIMKL"],
            "myanimelist": ["Import from MyAnimeList"],
            "anilist": ["Import from AniList"],
            "kitsu": ["Import from Kitsu"],
            "yamtrack": ["Import from Yamtrack"],
            "hltb": ["Import from HowLongToBeat"],
            "steam": ["Import from Steam"],
            "imdb": ["Import from IMDB"],
            "goodreads": ["Import from GoodReads"],
            "plex": ["Import from Plex", "Sync Plex Watchlist"],
            "audiobookshelf": ["Import from Audiobookshelf"],
            "pocketcasts": ["Import from Pocket Casts"],
            "lastfm": ["Import from Last.fm History"],
        }
        schedule_task_names = {
            **result_task_names,
            "audiobookshelf": ["Import from Audiobookshelf (Recurring)"],
            "pocketcasts": ["Import from Pocket Casts (Recurring)"],
            "lastfm": ["Poll Last.fm for all users"],
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

        # Get all task results for this user
        task_results = TaskResult.objects.filter(
            task_result_filters,
            task_name__in=result_import_task_names,
        ).order_by(
            "-date_done",
        )  # Most recent first

        # Build results list
        results = []
        for task in task_results:
            if task.status in {states.PENDING, states.STARTED}:
                async_result = AsyncResult(task.task_id)
                if async_result.status != task.status:
                    task.status = async_result.status
                    task.result = async_result.result
                    task.date_done = timezone.now()
                    task.save(update_fields=["status", "result", "date_done"])

            source = result_task_to_source[task.task_name]
            processed_task = helpers.process_task_result(task)
            results.append(
                {
                    "task": processed_task,
                    "source": source,
                    "date": task.date_done,
                    "status": task.status,
                    "summary": processed_task.summary,
                    "errors": processed_task.errors,
                },
            )

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
        if hasattr(self, "lastfm_account") and self.lastfm_account and self.lastfm_account.is_connected:
            lastfm_task = PeriodicTask.objects.filter(
                task="Poll Last.fm for all users",
                enabled=True,
            ).select_related("interval").first()

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
        task_results = TaskResult.objects.filter(
            task_kwargs__contains=task_result_filter_text,
            task_name=export_task_name,
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

        issuer = "Yamtrack"
        return pyotp.TOTP(self.authenticator_secret).provisioning_uri(
            name=self.username,
            issuer_name=issuer,
        )

    def verify_totp_code(self, code):
        """Return True when the supplied TOTP code is valid."""
        if not self.authenticator_secret:
            return False

        import pyotp

        return bool(pyotp.TOTP(self.authenticator_secret).verify(str(code).strip(), valid_window=1))

    def generate_recovery_codes(self, count=8):
        """Generate one-time recovery codes and persist their hashes."""
        if count <= 0:
            return []

        self.recovery_codes.all().delete()
        codes = []
        for _ in range(count):
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
        ordering = ["-created_at"]

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
