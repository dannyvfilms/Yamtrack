import secrets

from django.contrib.auth.models import AbstractUser
from django.db import models
from django_celery_beat.models import PeriodicTask
from django_celery_results.models import TaskResult

from app.models import Item, MediaTypes, Status
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
    PROGRESS = "progress", "Progress"
    START_DATE = "start_date", "Start Date"
    END_DATE = "end_date", "End Date"
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
    NAME = "name", "Name"
    ITEMS_COUNT = "items_count", "Items Count"
    NEWEST_FIRST = "newest_first", "Newest First"


class ListDetailSortChoices(models.TextChoices):
    """Choices for list detail sort options."""

    DATE_ADDED = "date_added", "Date Added"
    TITLE = "title", "Title"
    MEDIA_TYPE = "media_type", "Media Type"
    RATING = "rating", "Rating"
    PROGRESS = "progress", "Progress"
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


class GameLoggingStyleChoices(models.TextChoices):
    """Choices for how game history entries are displayed."""

    SESSIONS = "sessions", "Sessions"
    REPEATS = "repeats", "Repeats"


class MobileGridLayoutChoices(models.TextChoices):
    """Choices for mobile grid layout preference."""

    COMFORTABLE = "comfortable", "Comfortable (2 columns)"
    COMPACT = "compact", "Compact (3 columns)"


class PlannedHomeDisplayChoices(models.TextChoices):
    """Choices for how planned items are displayed on home page."""

    DISABLED = "disabled", "Disabled"
    COMBINED = "combined", "Combined"
    SEPARATED = "separated", "Separated"


class JellyseerrDefaultAddedStatusChoices(models.TextChoices):
    """Choices for status applied to media added via Jellyseerr webhook."""

    PLANNING = Status.PLANNING.value, Status.PLANNING.label
    IN_PROGRESS = Status.IN_PROGRESS.value, Status.IN_PROGRESS.label


class User(AbstractUser):
    """Custom user model."""

    is_demo = models.BooleanField(default=False)

    last_search_type = models.CharField(
        max_length=10,
        default=MediaTypes.TV.value,
        choices=MediaTypes.choices,
    )

    home_sort = models.CharField(
        max_length=20,
        default=HomeSortChoices.UPCOMING,
        choices=HomeSortChoices.choices,
    )

    # Media type preferences: TV Shows
    tv_enabled = models.BooleanField(default=True)
    tv_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices.choices,
    )
    tv_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    tv_sort = models.CharField(
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
    )
    tv_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices.choices,
    )

    # Media type preferences: TV Seasons
    season_enabled = models.BooleanField(default=True)
    season_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices.choices,
    )
    season_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    season_sort = models.CharField(
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
    )
    season_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices.choices,
    )

    # Media type preferences: Movies
    movie_enabled = models.BooleanField(default=True)
    movie_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices.choices,
    )
    movie_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    movie_sort = models.CharField(
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
    )
    movie_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices.choices,
    )

    # Media type preferences: Anime
    anime_enabled = models.BooleanField(default=True)
    anime_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.TABLE,
        choices=LayoutChoices.choices,
    )
    anime_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    anime_sort = models.CharField(
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
    )
    anime_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices.choices,
    )

    # Media type preferences: Manga
    manga_enabled = models.BooleanField(default=True)
    manga_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.TABLE,
        choices=LayoutChoices.choices,
    )
    manga_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    manga_sort = models.CharField(
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
    )
    manga_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices.choices,
    )

    # Media type preferences: Games
    game_enabled = models.BooleanField(default=True)
    game_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices.choices,
    )
    game_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    game_sort = models.CharField(
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
    )
    game_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices.choices,
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
        choices=LayoutChoices.choices,
    )
    book_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    book_sort = models.CharField(
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
    )
    book_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices.choices,
    )

    # Media type preferences: Comics
    comic_enabled = models.BooleanField(default=True)
    comic_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices.choices,
    )
    comic_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    comic_sort = models.CharField(
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
    )
    comic_status = models.CharField(
        max_length=20,
        default=MediaStatusChoices.ALL,
        choices=MediaStatusChoices.choices,
    )

    # Media type preferences: Music
    music_enabled = models.BooleanField(default=True)
    music_layout = models.CharField(
        max_length=20,
        default=LayoutChoices.GRID,
        choices=LayoutChoices.choices,
    )
    music_direction = models.CharField(
        max_length=4,
        default=DirectionChoices.DESC,
        choices=DirectionChoices.choices,
    )
    music_sort = models.CharField(
        max_length=20,
        default=MediaSortChoices.SCORE,
        choices=MediaSortChoices.choices,
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
        choices=MediaStatusChoices.choices,
    )

    # UI preferences
    clickable_media_cards = models.BooleanField(
        default=False,
        help_text="Hide hover overlay on touch devices",
    )

    # Calendar preferences
    calendar_layout = models.CharField(
        max_length=20,
        default=CalendarLayoutChoices.GRID,
        choices=CalendarLayoutChoices.choices,
    )

    # Lists preferences
    lists_sort = models.CharField(
        max_length=20,
        default=ListSortChoices.LAST_ITEM_ADDED,
        choices=ListSortChoices.choices,
    )
    list_detail_sort = models.CharField(
        max_length=20,
        default=ListDetailSortChoices.DATE_ADDED,
        choices=ListDetailSortChoices.choices,
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
                name="lists_sort_valid",
                condition=models.Q(lists_sort__in=ListSortChoices.values),
            ),
            models.CheckConstraint(
                name="activity_history_view_valid",
                condition=models.Q(activity_history_view__in=ActivityHistoryViewChoices.values),
            ),
            models.CheckConstraint(
                name="statistics_default_range_valid",
                condition=models.Q(statistics_default_range__in=StatisticsRangeChoices.values),
            ),
            models.CheckConstraint(
                name="list_detail_sort_valid",
                condition=models.Q(list_detail_sort__in=ListDetailSortChoices.values),
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

        # Add season if TV is enabled (and season isn't already in the list)
        if (
            MediaTypes.TV.value in enabled_types
            and MediaTypes.SEASON.value not in enabled_types
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
        import_tasks = {
            "trakt": "Import from Trakt",
            "simkl": "Import from SIMKL",
            "myanimelist": "Import from MyAnimeList",
            "anilist": "Import from AniList",
            "kitsu": "Import from Kitsu",
            "yamtrack": "Import from Yamtrack",
        "hltb": "Import from HowLongToBeat",
        "steam": "Import from Steam",
        "imdb": "Import from IMDB",
        "goodreads": "Import from GoodReads",
        "plex": "Import from Plex",
        "pocketcasts": "Import from Pocket Casts (Recurring)",
        "lastfm": "Poll Last.fm for all users",
    }

        # Reverse mapping to get source from task name
        task_to_source = {v: k for k, v in import_tasks.items()}

        task_result_filter_text = f"'user_id': {self.id},"

        # Get all task results for this user
        task_results = TaskResult.objects.filter(
            task_kwargs__contains=task_result_filter_text,
            task_name__in=import_tasks.values(),
        ).order_by(
            "-date_done",
        )  # Most recent first

        # Build results list
        results = []
        for task in task_results:
            source = task_to_source[task.task_name]
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
        periodic_tasks_filter_text = f'"user_id": {self.id}'
        periodic_tasks = PeriodicTask.objects.filter(
            task__in=import_tasks.values(),
            kwargs__contains=periodic_tasks_filter_text,
            enabled=True,
        ).select_related("crontab")

        # Build schedules list
        schedules = []
        for periodic_task in periodic_tasks:
            source = task_to_source.get(periodic_task.task, "unknown")

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
        from datetime import timedelta

        from django.utils import timezone

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

    def regenerate_token(self):
        """Regenerate the user's token."""
        self.token = generate_token()
        self.save(update_fields=["token"])
