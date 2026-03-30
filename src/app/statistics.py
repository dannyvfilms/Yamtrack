import calendar
import datetime
import heapq
import itertools
import logging
from collections import Counter, defaultdict

from dateutil.relativedelta import relativedelta
from django.apps import apps
from django.core.cache import cache
from django.db import models, transaction
from django.db.models import (
    Prefetch,
    Q,
)
from django.utils import timezone

from app import config, providers
from app.models import (
    TV,
    BasicMedia,
    CreditRoleType,
    Episode,
    ItemPersonCredit,
    MediaManager,
    MediaTypes,
    Season,
    Status,
    Track,
)
from app.templatetags import app_tags

logger = logging.getLogger(__name__)

MEDIA_TYPE_HOURS_ORDER = [
    MediaTypes.TV.value,
    MediaTypes.MOVIE.value,
    MediaTypes.GAME.value,
    MediaTypes.PODCAST.value,
    MediaTypes.BOARDGAME.value,
    MediaTypes.ANIME.value,
    MediaTypes.MUSIC.value,
]


def _infer_user_from_user_media(user_media):
    """Best-effort helper to derive user from user_media querysets."""
    if not user_media:
        return None

    for media_list in user_media.values():
        if media_list is None:
            continue
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


def get_user_media(user, start_date, end_date):
    """Get all media items and their counts for a user within date range."""
    media_models = [
        apps.get_model(app_label="app", model_name=media_type)
        for media_type in user.get_active_media_types()
    ]
    user_media = {}
    media_count = {"total": 0}

    # Cache the base episodes query
    base_episodes = None
    if TV in media_models or Season in media_models:
        if start_date is None and end_date is None:
            # No date filtering for "All Time"
            base_episodes = Episode.objects.filter(
                related_season__user=user,
            )
        else:
            base_episodes = Episode.objects.filter(
                related_season__user=user,
                end_date__range=(start_date, end_date),
            )

    for model in media_models:
        media_type = model.__name__.lower()
        queryset = None

        if model == TV:
            tv_ids = base_episodes.values_list(
                "related_season__related_tv",
                flat=True,
            ).distinct()
            queryset = TV.objects.filter(
                id__in=tv_ids,
                status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
            ).prefetch_related(
                Prefetch(
                    "seasons",
                    queryset=Season.objects.filter(
                        status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
                    ).select_related(
                        "item",
                    ).prefetch_related(
                        Prefetch(
                            "episodes",
                            queryset=base_episodes.filter(
                                related_season__related_tv__in=tv_ids,
                            ),
                        ),
                    ),
                ),
            )
        elif model == Season:
            season_ids = base_episodes.values_list(
                "related_season",
                flat=True,
            ).distinct()
            queryset = Season.objects.filter(
                id__in=season_ids,
                status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
            ).prefetch_related(
                Prefetch("episodes", queryset=base_episodes),
            )
        # For other models, apply date filtering conditionally
        elif start_date is None and end_date is None:
            # No date filtering for "All Time"
            queryset = model.objects.filter(
                user=user,
                status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
            )
        else:
            queryset = model.objects.filter(
                user=user,
                status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
            ).filter(
                # Case 1: Media has both start_date and end_date
                # Include if ranges overlap
                # (exclude if media ends before filter start or starts after filter end)
                (
                    Q(start_date__isnull=False)
                    & Q(end_date__isnull=False)
                    & ~(Q(end_date__lt=start_date) | Q(start_date__gt=end_date))
                )
                |
                # Case 2: Media only has start_date (end_date is null)
                # Include if start_date is within filter range
                (
                    Q(start_date__isnull=False)
                    & Q(end_date__isnull=True)
                    & Q(start_date__gte=start_date)
                    & Q(start_date__lte=end_date)
                )
                |
                # Case 3: Media only has end_date (start_date is null)
                # Include if end_date is within filter range
                (
                    Q(start_date__isnull=True)
                    & Q(end_date__isnull=False)
                    & Q(end_date__gte=start_date)
                    & Q(end_date__lte=end_date)
                ),
            )

        queryset = queryset.select_related("item")
        user_media[media_type] = queryset
        count = queryset.count()
        media_count[media_type] = count
        media_count["total"] += count

    logger.info(
        "%s - Retrieved media %s",
        user,
        "for all time" if start_date is None else f"from {start_date} to {end_date}",
    )
    return user_media, media_count


def get_media_type_distribution(media_count, minutes_per_type=None):
    """Get data formatted for Chart.js pie chart."""
    # Define colors for each media type
    # Format for Chart.js
    chart_data = {
        "labels": [],
        "datasets": [
            {
                "data": [],
                "backgroundColor": [],
            },
        ],
    }

    dataset = chart_data["datasets"][0]

    if minutes_per_type:
        dataset["value_label"] = "Hours"
        dataset["value_suffix"] = "h"
        dataset["value_decimals"] = 1

        ordered_types = list(MEDIA_TYPE_HOURS_ORDER)
        ordered_types.extend(
            [media_type for media_type in minutes_per_type if media_type not in ordered_types],
        )

        for media_type in ordered_types:
            total_minutes = minutes_per_type.get(media_type, 0) or 0
            if total_minutes <= 0:
                continue
            hours = round(total_minutes / 60, 2)
            if hours <= 0:
                continue
            label = app_tags.media_type_readable(media_type)
            chart_data["labels"].append(label)
            dataset["data"].append(hours)
            dataset["backgroundColor"].append(
                config.get_stats_color(media_type),
            )
        return chart_data

    # Only include media types with counts > 0
    for media_type, count in media_count.items():
        if media_type != "total" and count > 0:
            # Format label with first letter capitalized
            label = app_tags.media_type_readable(media_type)
            chart_data["labels"].append(label)
            dataset["data"].append(count)
            dataset["backgroundColor"].append(
                config.get_stats_color(media_type),
            )
    return chart_data


def get_status_distribution(user_media):
    """Get status distribution for each media type within date range."""
    distribution = {}
    total_completed = 0
    # Define status order to ensure consistent stacking
    status_order = list(Status.values)
    for media_type, media_list in user_media.items():
        status_counts = dict.fromkeys(status_order, 0)
        counts = media_list.values("status").annotate(count=models.Count("id"))
        for count_data in counts:
            status_counts[count_data["status"]] = count_data["count"]
            if count_data["status"] == Status.COMPLETED.value:
                total_completed += count_data["count"]

        distribution[media_type] = status_counts

    # Format the response for charting
    return {
        "labels": [app_tags.media_type_readable(x) for x in distribution],
        "datasets": [
            {
                "label": status,
                "data": [
                    distribution[media_type][status] for media_type in distribution
                ],
                "background_color": get_status_color(status),
                "total": sum(
                    distribution[media_type][status] for media_type in distribution
                ),
            }
            for status in status_order
        ],
        "total_completed": total_completed,
    }


def get_status_pie_chart_data(status_distribution):
    """Get status distribution as a pie chart."""
    # Format for Chart.js pie chart
    chart_data = {
        "labels": [],
        "datasets": [
            {
                "data": [],
                "backgroundColor": [],
            },
        ],
    }

    # Process each status dataset
    for dataset in status_distribution["datasets"]:
        status_label = dataset["label"]
        status_count = dataset["total"]
        status_color = dataset["background_color"]

        # Only include statuses with counts > 0
        if status_count > 0:
            chart_data["labels"].append(status_label)
            chart_data["datasets"][0]["data"].append(status_count)
            chart_data["datasets"][0]["backgroundColor"].append(status_color)

    return chart_data


def get_score_distribution(user_media):
    """Get score distribution for each media type within date range."""
    distribution = {}
    total_scored = 0
    total_score_sum = 0

    # Global top rated (for backward compatibility with existing "ALL MEDIA" section)
    top_rated = []
    top_rated_count = 14
    # Per-media-type top rated (for the new compact cards)
    top_rated_by_type = {}
    top_rated_per_type_count = 20  # Match the limit used in other cards

    counter = itertools.count()  # Ensures stable sorting for equal scores

    # Infer user from user_media for fetching all entries
    user = _infer_user_from_user_media(user_media)
    score_scale_max = user.rating_scale_max if user else 10
    score_range = range(score_scale_max + 1)

    for media_type, media_list in user_media.items():
        score_counts = dict.fromkeys(score_range, 0)
        media_list = media_list.select_related("item")

        # Group media by item to identify which items appear in the date range
        media_by_item = defaultdict(list)
        for media in media_list:
            item = getattr(media, "item", None)
            key = item.id if item else media.id
            media_by_item[key].append(media)

        # For each item that appears in the date range, fetch ALL entries (not just date-filtered)
        # to find the aggregated score, even if the score was set outside the date range
        deduped_scored = {}
        if user:
            # Get the model class for this media type
            model_class = apps.get_model("app", media_type)
            
            # Get all unique item IDs from items that appear in the date range
            item_ids_in_range = set()
            item_id_to_key_map = {}  # Map item.id -> key used in media_by_item
            for key, entries in media_by_item.items():
                for entry in entries:
                    item = getattr(entry, "item", None)
                    if item:
                        item_ids_in_range.add(item.id)
                        item_id_to_key_map[item.id] = key
            
            # Fetch ALL entries for these items (not just date-filtered ones)
            if item_ids_in_range:
                all_entries_query = model_class.objects.filter(
                    user=user,
                    item_id__in=item_ids_in_range,
                ).select_related("item").order_by("-created_at")
                
                # Group all entries by item ID
                all_entries_by_item_id = defaultdict(list)
                for entry in all_entries_query:
                    item = getattr(entry, "item", None)
                    if item:
                        all_entries_by_item_id[item.id].append(entry)
                
                # Now aggregate scores from ALL entries (not just date-filtered ones)
                for item_id in item_ids_in_range:
                    # Get the key used in media_by_item for this item
                    key = item_id_to_key_map.get(item_id)
                    if key is None:
                        continue
                    
                    # Use entries from date range as display media (for activity date calculation)
                    display_entries = media_by_item.get(key, [])
                    if not display_entries:
                        continue
                    
                    # Use ALL entries to find aggregated score
                    all_entries = all_entries_by_item_id.get(item_id, [])
                    if not all_entries:
                        continue
                    
                    display_media = display_entries[0]  # Use first entry from date range as display
                    
                    # Aggregate score from ALL entries (regardless of date)
                    latest_rating = None
                    latest_activity = None
                    
                    for entry in all_entries:
                        if entry.score is not None:
                            # Determine the most recent activity for this entry
                            entry_activity = None
                            if entry.end_date:
                                entry_activity = entry.end_date
                            elif entry.progressed_at:
                                entry_activity = entry.progressed_at
                            else:
                                entry_activity = entry.created_at
                            
                            # If this entry has more recent activity, use its rating
                            if latest_activity is None or entry_activity > latest_activity:
                                latest_activity = entry_activity
                                latest_rating = entry.score
                    
                    score_to_use = latest_rating
                    # Set aggregated_score for consistency with other code paths
                    if score_to_use is not None:
                        display_media.aggregated_score = score_to_use
                    
                    # Only include if there's a score
                    if score_to_use is not None:
                        dates = [d for d in (display_media.end_date, display_media.start_date) if d]
                        activity_date = max(dates) if dates else display_media.created_at
                        deduped_scored[key] = {
                            "media": display_media,
                            "activity_date": activity_date,
                            "score": score_to_use,
                        }
            else:
                # Fallback: no items with item_id, use original logic
                for item_id, entries in media_by_item.items():
                    if len(entries) == 1:
                        # Single entry - use it directly
                        media = entries[0]
                        score_to_use = media.score
                        display_media = media
                    else:
                        # Multiple entries - aggregate to find most recent score
                        display_media = entries[0]  # Use first entry as display
                        latest_rating = None
                        latest_activity = None

                        for entry in entries:
                            if entry.score is not None:
                                # Determine the most recent activity for this entry
                                entry_activity = None
                                if entry.end_date:
                                    entry_activity = entry.end_date
                                elif entry.progressed_at:
                                    entry_activity = entry.progressed_at
                                else:
                                    entry_activity = entry.created_at

                                # If this entry has more recent activity, use its rating
                                if latest_activity is None or entry_activity > latest_activity:
                                    latest_activity = entry_activity
                                    latest_rating = entry.score

                        score_to_use = latest_rating
                        # Set aggregated_score for consistency with other code paths
                        if score_to_use is not None:
                            display_media.aggregated_score = score_to_use

                    # Only include if there's a score
                    if score_to_use is not None:
                        dates = [d for d in (display_media.end_date, display_media.start_date) if d]
                        activity_date = max(dates) if dates else display_media.created_at
                        deduped_scored[item_id] = {
                            "media": display_media,
                            "activity_date": activity_date,
                            "score": score_to_use,
                        }
        else:
            # Fallback: no user available, use original logic
            for item_id, entries in media_by_item.items():
                if len(entries) == 1:
                    # Single entry - use it directly
                    media = entries[0]
                    score_to_use = media.score
                    display_media = media
                else:
                    # Multiple entries - aggregate to find most recent score
                    display_media = entries[0]  # Use first entry as display
                    latest_rating = None
                    latest_activity = None

                    for entry in entries:
                        if entry.score is not None:
                            # Determine the most recent activity for this entry
                            entry_activity = None
                            if entry.end_date:
                                entry_activity = entry.end_date
                            elif entry.progressed_at:
                                entry_activity = entry.progressed_at
                            else:
                                entry_activity = entry.created_at

                            # If this entry has more recent activity, use its rating
                            if latest_activity is None or entry_activity > latest_activity:
                                latest_activity = entry_activity
                                latest_rating = entry.score

                    score_to_use = latest_rating
                    # Set aggregated_score for consistency with other code paths
                    if score_to_use is not None:
                        display_media.aggregated_score = score_to_use

                # Only include if there's a score
                if score_to_use is not None:
                    dates = [d for d in (display_media.end_date, display_media.start_date) if d]
                    activity_date = max(dates) if dates else display_media.created_at
                    deduped_scored[item_id] = {
                        "media": display_media,
                        "activity_date": activity_date,
                        "score": score_to_use,
                    }

        deduped_media = [entry["media"] for entry in deduped_scored.values()]

        # Initialize per-type heap for this media type
        type_top_rated = []
        type_counter = itertools.count()

        for entry_data in deduped_scored.values():
            media = entry_data["media"]
            score_value = entry_data["score"]
            score_value_scaled = float(score_value)
            if score_scale_max == 5:
                score_value_scaled = score_value_scaled / 2

            # Add to global top rated (for backward compatibility)
            if len(top_rated) < top_rated_count:
                heapq.heappush(
                    top_rated,
                    (float(score_value), next(counter), media),
                )
            else:
                heapq.heappushpop(
                    top_rated,
                    (float(score_value), next(counter), media),
                )

            # Add to per-type top rated
            if len(type_top_rated) < top_rated_per_type_count:
                heapq.heappush(
                    type_top_rated,
                    (float(score_value), next(type_counter), media),
                )
            else:
                heapq.heappushpop(
                    type_top_rated,
                    (float(score_value), next(type_counter), media),
                )

            binned_score = int(score_value_scaled)
            if binned_score > score_scale_max:
                binned_score = score_scale_max
            score_counts[binned_score] += 1
            total_scored += 1
            total_score_sum += score_value_scaled

        distribution[media_type] = score_counts

        # Sort and annotate per-type top rated
        type_top_rated_sorted = [
            media for _, _, media in sorted(type_top_rated, key=lambda x: (-x[0], x[1]))
        ]
        top_rated_by_type[media_type] = _annotate_top_rated_media(type_top_rated_sorted)

    average_score = (
        round(total_score_sum / total_scored, 2) if total_scored > 0 else None
    )

    top_rated_media = [
        media for _, _, media in sorted(top_rated, key=lambda x: (-x[0], x[1]))
    ]

    top_rated_media = _annotate_top_rated_media(top_rated_media)

    return {
        "labels": [str(score) for score in score_range],
        "datasets": [
            {
                "label": app_tags.media_type_readable(media_type),
                "data": [distribution[media_type][score] for score in score_range],
                "background_color": config.get_stats_color(media_type),
            }
            for media_type in distribution
        ],
        "average_score": average_score,
        "total_scored": total_scored,
        "scale_max": score_scale_max,
    }, top_rated_media, top_rated_by_type


def _annotate_top_rated_media(top_rated_media):
    """Apply prefetch_related and annotate max_progress for top rated media."""
    if not top_rated_media:
        return top_rated_media

    # Group by media type to batch database operations
    media_by_type = {}
    for media in top_rated_media:
        media_type = media.item.media_type
        if media_type not in media_by_type:
            media_by_type[media_type] = []
        media_by_type[media_type].append(media)

    media_manager = MediaManager()

    for media_type, media_list in media_by_type.items():
        model = apps.get_model(app_label="app", model_name=media_type)
        media_ids = [media.id for media in media_list]

        # Fetch fresh instances with proper relationships and annotations
        queryset = model.objects.filter(id__in=media_ids)
        queryset = media_manager._apply_prefetch_related(queryset, media_type)
        media_manager.annotate_max_progress(queryset, media_type)

        prefetched_media_map = {media.id: media for media in queryset}

        # Replace original instances with enhanced ones
        for i, media in enumerate(top_rated_media):
            if media.item.media_type == media_type:
                top_rated_media[i] = prefetched_media_map[media.id]

    return top_rated_media


def get_status_color(status):
    """Get the color for the status of the media."""
    try:
        return config.get_status_stats_color(status)
    except KeyError:
        return "rgba(201, 203, 207)"


def get_timeline(user_media):
    """Build a timeline of media consumption organized by month-year."""
    timeline = defaultdict(list)

    # Process each media type
    for media_type, queryset in user_media.items():
        # If we have TV objects but seasons are hidden from the sidebar,
        # the TV queryset will still include prefetched seasons. Add
        # seasons from TV objects to the timeline so they appear here.
        if media_type == MediaTypes.TV.value:
            if MediaTypes.SEASON.value not in user_media:
                for tv in queryset:
                    seasons_qs = getattr(tv, "seasons", None)
                    if seasons_qs is None:
                        continue
                    for media in seasons_qs.all():
                        # media here is a Season instance
                        local_start_date = (
                            timezone.localdate(media.start_date) if media.start_date else None
                        )
                        local_end_date = (
                            timezone.localdate(media.end_date) if media.end_date else None
                        )

                        if media.start_date and media.end_date:
                            # add media to all months between start and end
                            current_date = local_start_date
                            while current_date <= local_end_date:
                                year = current_date.year
                                month = current_date.month
                                month_name = calendar.month_name[month]
                                month_year = f"{month_name} {year}"

                                timeline[month_year].append(media)

                                # Move to next month
                                current_date += relativedelta(months=1)
                                current_date = current_date.replace(day=1)
                        elif media.start_date:
                            # If only start date, add to the start month
                            year = local_start_date.year
                            month = local_start_date.month
                            month_name = calendar.month_name[month]
                            month_year = f"{month_name} {year}"

                            timeline[month_year].append(media)
                        elif media.end_date:
                            # If only end date, add to the end month
                            year = local_end_date.year
                            month = local_end_date.month
                            month_name = calendar.month_name[month]
                            month_year = f"{month_name} {year}"

                            timeline[month_year].append(media)
            # TV timeline activity is represented by seasons, not TV shells.
            continue

        for media in queryset:
            local_start_date = (
                timezone.localdate(media.start_date) if media.start_date else None
            )
            local_end_date = (
                timezone.localdate(media.end_date) if media.end_date else None
            )

            if media.start_date and media.end_date:
                # add media to all months between start and end
                current_date = local_start_date
                while current_date <= local_end_date:
                    year = current_date.year
                    month = current_date.month
                    month_name = calendar.month_name[month]
                    month_year = f"{month_name} {year}"

                    timeline[month_year].append(media)

                    # Move to next month
                    current_date += relativedelta(months=1)
                    current_date = current_date.replace(day=1)
            elif media.start_date:
                # If only start date, add to the start month
                year = local_start_date.year
                month = local_start_date.month
                month_name = calendar.month_name[month]
                month_year = f"{month_name} {year}"

                timeline[month_year].append(media)
            elif media.end_date:
                # If only end date, add to the end month
                year = local_end_date.year
                month = local_end_date.month
                month_name = calendar.month_name[month]
                month_year = f"{month_name} {year}"

                timeline[month_year].append(media)

    # Convert to sorted dictionary with media sorted by start date
    # Create a list sorted by year and month in reverse order
    sorted_items = []
    for month_year, media_list in timeline.items():
        month_name, year_str = month_year.split()
        year = int(year_str)
        month = list(calendar.month_name).index(month_name)
        sorted_items.append((month_year, media_list, year, month))

    # Sort by year and month in reverse chronological order
    sorted_items.sort(key=lambda x: (x[2], x[3]), reverse=True)

    # Create the final result dictionary
    result = {}
    for month_year, media_list, _, _ in sorted_items:
        # Sort the media list using our custom sort key
        result[month_year] = sorted(media_list, key=time_line_sort_key, reverse=True)
    return result


def time_line_sort_key(media):
    """Sort media items in the timeline."""
    if media.end_date is not None:
        return timezone.localdate(media.end_date)
    return timezone.localdate(media.start_date)


def _convert_chart_to_day_minutes(daily_hours_data):
    """Convert Chart.js formatted daily hours data to day_minutes_by_type format.

    Args:
        daily_hours_data: {"labels": ["2025-01-01", ...], "datasets": [...]}

    Returns:
        Dict mapping media_type -> {date_iso_str -> minutes}
    """
    day_minutes_by_type = {}
    labels = daily_hours_data.get("labels", [])
    datasets = daily_hours_data.get("datasets", [])

    for dataset in datasets:
        # Use a generic key since we just need total minutes per day
        media_type = dataset.get("label", "unknown")
        data = dataset.get("data", [])

        if media_type not in day_minutes_by_type:
            day_minutes_by_type[media_type] = {}

        for i, hours in enumerate(data):
            if i < len(labels):
                date_str = labels[i]
                # Convert hours back to minutes
                minutes = float(hours) * 60 if hours else 0
                day_minutes_by_type[media_type][date_str] = minutes

    return day_minutes_by_type


def get_activity_data(user, start_date, end_date, daily_hours_data=None):
    """Get daily activity counts for the activity calendar.

    Args:
        user: The user to get activity data for
        start_date: Start of the date range
        end_date: End of the date range
        daily_hours_data: Optional Chart.js formatted daily hours data from
            get_daily_hours_by_media_type(). If provided, used for more accurate
            "most active day" calculation.
    """
    if end_date is None:
        end_date = timezone.localtime()

    start_date_aligned = get_aligned_monday(start_date)

    combined_data = get_filtered_historical_data(start_date_aligned, end_date, user)

    # update start_date values from historical records if not provided
    if start_date is None:
        dates = [item["date"] for item in combined_data]
        start_date = datetime.datetime.combine(
            min(dates) if dates else timezone.localdate(),
            datetime.time.min,
        )
        start_date_aligned = get_aligned_monday(start_date)

    # Aggregate counts by date
    date_counts = {}
    for item in combined_data:
        date = item["date"]
        date_counts[date] = date_counts.get(date, 0) + item["count"]

    date_range = [
        start_date_aligned.date() + datetime.timedelta(days=x)
        for x in range((end_date.date() - start_date_aligned.date()).days + 1)
    ]

    # Calculate most active day using daily hours data if available
    has_chart_data = (
        daily_hours_data
        and daily_hours_data.get("labels")
        and daily_hours_data.get("datasets")
    )
    if has_chart_data:
        # Convert Chart.js format to day_minutes_by_type format
        day_minutes_by_type = _convert_chart_to_day_minutes(daily_hours_data)
        most_active_day, day_percentage = calculate_most_active_weekday(
            day_minutes_by_type,
            date_range,
        )
    else:
        # Fallback to legacy calculation for backward compatibility
        most_active_day, day_percentage = calculate_day_of_week_stats(
            date_counts,
            start_date.date(),
        )

    streaks = calculate_streak_details(
        date_counts,
        end_date.date(),
    )

    # Create complete date range including padding days
    activity_data = [
        {
            "date": current_date.strftime("%Y-%m-%d"),
            "count": date_counts.get(current_date, 0),
            "level": get_level(date_counts.get(current_date, 0)),
        }
        for current_date in date_range
    ]

    # Format data into calendar weeks
    calendar_weeks = [activity_data[i : i + 7] for i in range(0, len(activity_data), 7)]

    # Generate months list with their Monday counts
    months = []
    mondays_per_month = []
    current_month = date_range[0].strftime("%b")
    monday_count = 0

    for current_date in date_range:
        if current_date.weekday() == 0:  # Monday
            month = current_date.strftime("%b")

            if current_month != month:
                if current_month is not None:
                    if monday_count > 1:
                        months.append(current_month)
                        mondays_per_month.append(monday_count)
                    else:
                        months.append("")
                        mondays_per_month.append(monday_count)
                current_month = month
                monday_count = 0

            monday_count += 1
    # For the last month
    if monday_count > 1:
        months.append(current_month)
        mondays_per_month.append(monday_count)

    return {
        "calendar_weeks": calendar_weeks,
        "months": list(zip(months, mondays_per_month, strict=False)),
        "stats": {
            "most_active_day": most_active_day,
            "most_active_day_percentage": day_percentage,
            "current_streak": streaks["current_streak"],
            "longest_streak": streaks["longest_streak"],
            "longest_streak_start": streaks["longest_streak_start"],
            "longest_streak_end": streaks["longest_streak_end"],
        },
    }


def get_aligned_monday(datetime_obj):
    """Get the Monday of the week containing the given date."""
    if datetime_obj is None:
        return None

    days_to_subtract = datetime_obj.weekday()  # 0=Monday, 6=Sunday
    return datetime_obj - datetime.timedelta(days=days_to_subtract)


def get_level(count):
    """Calculate intensity level (0-4) based on count."""
    thresholds = [0, 3, 6, 9]
    for i, threshold in enumerate(thresholds):
        if count <= threshold:
            return i
    return 4


def get_filtered_historical_data(start_date, end_date, user):
    """Return [{"date": datetime.date, "count": int}]."""
    historical_models = BasicMedia.objects.get_historical_models()
    local_tz = timezone.get_current_timezone()

    day_buckets = defaultdict(int)

    for model_name in historical_models:
        model = apps.get_model("app", model_name)

        qs = model.objects.filter(history_user_id=user)

        if start_date:
            qs = qs.filter(history_date__gte=start_date)
        if end_date:
            qs = qs.filter(history_date__lte=end_date)

        # We only need the timestamp, stream results to keep memory usage flat
        for ts in qs.values_list("history_date", flat=True).iterator(chunk_size=2_000):
            aware_ts = timezone.localtime(ts, local_tz)

            day_buckets[aware_ts.date()] += 1

    combined_data = [
        {"date": day, "count": count} for day, count in day_buckets.items()
    ]

    logger.info("%s - built historical data (%s rows)", user, len(combined_data))
    return combined_data


def calculate_day_of_week_stats(date_counts, start_date):
    """Calculate the most active day of the week based on activity frequency.

    Returns the day name and its percentage of total activity.
    """
    # Initialize counters for each day of the week
    day_counts = defaultdict(int)
    total_active_days = 0

    # Count occurrences of each day of the week where activity happened
    for date in date_counts:
        if date < start_date:
            continue
        if date_counts[date] > 0:
            day_name = date.strftime("%A")  # Get full day name
            day_counts[day_name] += 1
            total_active_days += 1

    if not total_active_days:
        return None, 0

    # Find the most active day
    most_active_day = max(day_counts.items(), key=lambda x: x[1])
    percentage = (most_active_day[1] / total_active_days) * 100

    return most_active_day[0], round(percentage)


def calculate_most_active_weekday(day_minutes_by_type, day_list):
    """Calculate most active weekday based on total consumption minutes.

    Uses the same data source as 'Played Hours by Media Type' chart to ensure
    the most active day is calculated from the same filtered data range.

    Args:
        day_minutes_by_type: Dict mapping media_type -> {date_iso_str -> minutes}
        day_list: List of date objects in the filtered range

    Returns:
        (weekday_name, percentage) or (None, 0) if no data.
    """
    weekday_minutes = defaultdict(float)

    for day in day_list:
        day_str = day.isoformat()
        day_total = 0
        for minutes_map in day_minutes_by_type.values():
            day_total += minutes_map.get(day_str, 0)
        if day_total > 0:
            weekday_name = day.strftime("%A")
            weekday_minutes[weekday_name] += day_total

    if not weekday_minutes:
        return None, 0

    total_minutes = sum(weekday_minutes.values())
    most_active = max(weekday_minutes.items(), key=lambda x: x[1])
    percentage = (most_active[1] / total_minutes) * 100

    return most_active[0], round(percentage)


def calculate_streak_details(date_counts, end_date):
    """Return current/longest streak counts plus their date ranges."""
    active_dates = sorted(
        [date for date, count in date_counts.items() if count > 0],
    )

    if not active_dates:
        return {
            "current_streak": 0,
            "current_streak_start": None,
            "current_streak_end": None,
            "longest_streak": 0,
            "longest_streak_start": None,
            "longest_streak_end": None,
        }

    active_set = set(active_dates)

    longest_streak = 1
    longest_start = active_dates[0]
    longest_end = active_dates[0]

    streak_start = active_dates[0]
    prev_date = active_dates[0]

    for current_date in active_dates[1:]:
        if (current_date - prev_date).days == 1:
            prev_date = current_date
            continue

        streak_len = (prev_date - streak_start).days + 1
        if streak_len > longest_streak or (streak_len == longest_streak and prev_date > longest_end):
            longest_streak = streak_len
            longest_start = streak_start
            longest_end = prev_date

        streak_start = current_date
        prev_date = current_date

    streak_len = (prev_date - streak_start).days + 1
    if streak_len > longest_streak or (streak_len == longest_streak and prev_date > longest_end):
        longest_streak = streak_len
        longest_start = streak_start
        longest_end = prev_date

    if end_date in active_set:
        current_end = end_date
        current_start = current_end
        while (current_start - datetime.timedelta(days=1)) in active_set:
            current_start -= datetime.timedelta(days=1)
        current_streak = (current_end - current_start).days + 1
    else:
        current_streak = 0
        current_start = None
        current_end = None

    return {
        "current_streak": current_streak,
        "current_streak_start": current_start,
        "current_streak_end": current_end,
        "longest_streak": longest_streak,
        "longest_streak_start": longest_start,
        "longest_streak_end": longest_end,
    }


def calculate_streaks(date_counts, end_date):
    """Calculate current and longest activity streaks."""
    streaks = calculate_streak_details(date_counts, end_date)
    return streaks["current_streak"], streaks["longest_streak"]


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








def _format_hours_minutes(total_minutes):
    """Format total minutes into hours and minutes string."""
    if total_minutes > 0:
        try:
            total_minutes = int(total_minutes)
        except (TypeError, ValueError):
            return "0h 0min"
        hours = total_minutes // 60
        remaining_minutes = total_minutes % 60

        # Always show both hours and minutes for consistency
        return f"{hours}h {remaining_minutes}min"
    return "0h 0min"


def _get_activity_datetime(media):
    """Return the most representative datetime for media activity."""
    for attr in ("end_date", "start_date", "created_at"):
        value = getattr(media, attr, None)
        if value:
            return value
    return None


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


def _calculate_game_time_in_range(media, start_date, end_date):
    """Return game minutes to count within the requested date range."""
    game_total_minutes = getattr(media, "progress", 0) or 0
    if game_total_minutes <= 0:
        return 0

    game_start_date = media.start_date.date() if media.start_date else None
    game_end_date = media.end_date.date() if media.end_date else None

    if game_start_date and game_end_date:
        game_total_days = (game_end_date - game_start_date).days + 1
        if game_total_days <= 0:
            game_total_days = 1

        if start_date and end_date:
            filter_start = start_date.date() if hasattr(start_date, "date") else start_date
            filter_end = end_date.date() if hasattr(end_date, "date") else end_date

            intersection_start = max(game_start_date, filter_start)
            intersection_end = min(game_end_date, filter_end)

            if intersection_start <= intersection_end:
                intersection_days = (intersection_end - intersection_start).days + 1
                if intersection_days > 0:
                    minutes_per_day = game_total_minutes / game_total_days
                    return minutes_per_day * intersection_days
            return 0

        return game_total_minutes

    if not start_date and not end_date:
        return game_total_minutes

    return 0


def calculate_minutes_per_media_type(user_media, start_date, end_date, user=None):
    """Return total minutes watched per media type within the date range."""
    minutes_per_type = {}

    for media_type, media_list in user_media.items():
        total_minutes = 0

        if media_type == MediaTypes.PODCAST.value:
            # Podcast: sum runtime from completed plays in history records
            podcast_user = user or _infer_user_from_user_media(user_media)
            podcast_history_records, podcasts_lookup = _get_podcast_history_data(
                podcast_user,
                start_date,
                end_date,
            )
            _, play_details = _collect_podcast_play_data(
                podcast_history_records,
                podcasts_lookup,
                start_date,
                end_date,
            )
            total_minutes += sum(runtime for _, _, runtime in play_details)
            minutes_per_type[media_type] = total_minutes
            continue

        for media_data in media_list:
            media = getattr(media_data, "media", media_data)

            if media_type == MediaTypes.TV.value:
                tv_minutes, _ = _calculate_tv_time(media, start_date, end_date, logger)
                total_minutes += tv_minutes
                continue

            if media_type == MediaTypes.ANIME.value:
                anime_minutes, _ = _calculate_anime_time(media, start_date, end_date, logger)
                total_minutes += anime_minutes
                continue

            if media_type == MediaTypes.MOVIE.value:
                activity_dt = _get_activity_datetime(media)
                if start_date and end_date:
                    if not activity_dt or activity_dt < start_date or activity_dt > end_date:
                        continue
                total_minutes += _calculate_movie_time(
                    media,
                    start_date,
                    end_date,
                    media_type,
                    logger,
                )
                continue

            if media_type == MediaTypes.GAME.value:
                if (
                    media.end_date
                    and start_date
                    and end_date
                    and start_date <= media.end_date <= end_date
                ) or (not start_date and not end_date):
                    total_minutes += media.progress
                continue

            if media_type == MediaTypes.BOARDGAME.value:
                if (
                    media.end_date
                    and start_date
                    and end_date
                    and start_date <= media.end_date <= end_date
                ) or (
                    media.start_date
                    and start_date
                    and end_date
                    and start_date <= media.start_date <= end_date
                ) or (not start_date and not end_date):
                    total_minutes += media.progress
                continue

            if media_type == MediaTypes.MUSIC.value:
                # Music: sum up runtime for each play (history record) within date range
                music_minutes = _calculate_music_time(media, start_date, end_date, logger)
                total_minutes += music_minutes
                continue

            if not _is_media_in_date_range(media, start_date, end_date):
                continue

            total_minutes += 60

        minutes_per_type[media_type] = total_minutes

    return minutes_per_type


def get_hours_per_media_type(user_media, start_date, end_date, minutes_per_type=None):
    """Calculate total hours watched per media type within the date range."""
    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(user_media, start_date, end_date)
    hours = {}
    for media_type, total_minutes in minutes_per_type.items():
        if media_type == MediaTypes.BOARDGAME.value:
            hours[media_type] = f"{total_minutes} play{'s' if total_minutes != 1 else ''}"
        else:
            hours[media_type] = _format_hours_minutes(total_minutes)
    return hours





def _get_season_metadata(media, season, season_metadata_cache, logger):
    """Get season metadata, using cache if available."""
    if season.item.season_number not in season_metadata_cache:
        try:
            season_metadata = providers.services.get_media_metadata(
                "season",
                media.item.media_id,
                media.item.source,
                [season.item.season_number],  # Note: season_numbers is a list
            )
            season_metadata_cache[season.item.season_number] = season_metadata
        except Exception as e:
            logger.warning(f"Failed to get season {season.item.season_number} metadata for {media.item.title}: {e}")
            season_metadata_cache[season.item.season_number] = None

    return season_metadata_cache[season.item.season_number]


def _get_season_metadata_with_episodes(media, season, logger):
    """Get season metadata with processed episodes that include runtime data."""
    try:
        # Get season metadata from provider
        season_metadata = providers.services.get_media_metadata(
            "season",
            media.item.media_id,
            media.item.source,
            [season.item.season_number],
        )

        if not season_metadata:
            logger.error(f"No season metadata available for {media.item.title} S{season.item.season_number}")
            return None

        # Get episodes from database for this season
        episodes_in_db = season.episodes.all()

        # Process episodes through TMDB to get runtime data
        from app.providers import tmdb
        season_metadata["episodes"] = tmdb.process_episodes(
            season_metadata,
            episodes_in_db,
        )

        return season_metadata

    except Exception as e:
        logger.error(f"Failed to get season metadata with episodes for {media.item.title} S{season.item.season_number}: {e}")
        return None


def _calculate_episode_time_from_data(episode_data, logger):
    """Calculate episode time from processed episode data."""
    if "runtime" not in episode_data or not episode_data["runtime"]:
        raise ValueError(f"Runtime data missing for episode {episode_data.get('episode_number', 'unknown')}")

    runtime_str = episode_data["runtime"]
    episode_minutes = parse_runtime_to_minutes(runtime_str)

    if episode_minutes is None:
        raise ValueError(f"Failed to parse runtime '{runtime_str}' for episode {episode_data.get('episode_number', 'unknown')}")

    return episode_minutes


def _calculate_episode_time_from_cache(episode, logger):
    """Calculate episode time from cached runtime data."""
    runtime_minutes = getattr(getattr(episode, "item", None), "runtime_minutes", None)
    if not runtime_minutes:
        logger.warning(f"Runtime data missing for episode {episode.item.episode_number if episode.item else 'unknown'}, skipping")
        return 0  # Skip this episode instead of failing

    if runtime_minutes >= 999998:
        logger.warning(
            "Runtime placeholder %s for episode %s, skipping",
            runtime_minutes,
            episode.item.episode_number if episode.item else "unknown",
        )
        return 0  # Skip this episode instead of failing

    return runtime_minutes


def _is_episode_in_range(episode, start_date, end_date):
    """Check if episode is within the specified date range."""
    if episode.end_date and start_date and end_date:
        return start_date <= episode.end_date <= end_date
    if not start_date and not end_date:
        # All time - include all episodes
        return True
    return False




def _calculate_tv_time(media, start_date, end_date, logger):
    """Calculate total time for TV shows using cached runtime data."""
    total_time_minutes = 0
    episode_count = 0

    if not hasattr(media, "seasons"):
        return total_time_minutes, episode_count

    for season in media.seasons.all():
        if not hasattr(season, "episodes"):
            continue

        for episode in season.episodes.all():
            # Check if episode is within date range
            if not _is_episode_in_range(episode, start_date, end_date):
                continue

            try:
                episode_count += 1
                total_time_minutes += _calculate_episode_time_from_cache(episode, logger)
            except ValueError as e:
                logger.warning(f"Skipping episode due to missing runtime: {e}")
                # Continue processing other episodes instead of failing completely
                continue

    return total_time_minutes, episode_count


def _calculate_anime_time(media, start_date, end_date, logger):
    """Calculate total time for anime using cached runtime data."""
    total_time_minutes = 0
    episode_count = 0

    # Check if anime is within date range
    if media.end_date and start_date and end_date:
        if start_date <= media.end_date <= end_date:
            episode_count = media.progress
            total_time_minutes = _get_anime_runtime_from_cache(media, episode_count, logger, "(date range)")
    elif not start_date and not end_date:
        # All time
        episode_count = media.progress
        total_time_minutes = _get_anime_runtime_from_cache(media, episode_count, logger, "(all time)")

    return total_time_minutes, episode_count




def _get_anime_runtime_from_cache(media, episode_count, logger, context=""):
    """Get anime runtime in minutes from cached runtime data."""
    if not hasattr(media, "item") or not media.item:
        logger.warning(f"Runtime data missing for anime (no item) {context}, skipping")
        return 0  # Skip this anime instead of failing

    if not media.item.runtime_minutes:
        logger.warning(f"Runtime data missing for anime '{media.item.title}' {context}, skipping")
        return 0  # Skip this anime instead of failing

    logger.debug(f"Anime '{media.item.title}' {context}: using cached runtime {media.item.runtime_minutes} minutes per episode")
    return episode_count * media.item.runtime_minutes


def _get_media_runtime_from_cache(media, logger, context=""):
    """Get media runtime in minutes from cached runtime data."""
    if not hasattr(media, "item") or not media.item:
        logger.warning(f"Runtime data missing for media (no item) {context}, skipping")
        return 0  # Skip this media instead of failing

    runtime_minutes = getattr(media.item, "runtime_minutes", None)
    # Exclude fallback values: 999998 (aired but runtime unknown) and 999999 (unknown runtime)
    if runtime_minutes and runtime_minutes < 999998:
        logger.debug(
            f"Media '{media.item.title}' {context}: using cached runtime {runtime_minutes} minutes",
        )
        return runtime_minutes

    # Check database directly to see if another task just saved runtime
    # This helps prevent race conditions when multiple tasks run in parallel
    from app.models import Item
    db_runtime = Item.objects.filter(id=media.item.id).values_list("runtime_minutes", flat=True).first()
    # Exclude fallback values: 999998 (aired but runtime unknown) and 999999 (unknown runtime)
    if db_runtime and db_runtime < 999998:
        logger.debug(
            f"Media '{media.item.title}' {context}: using database runtime {db_runtime} minutes (saved by another task)",
        )
        # Update in-memory object to reflect database state
        media.item.runtime_minutes = db_runtime
        return db_runtime

    metadata_runtime = None
    try:
        metadata = _get_media_metadata_for_statistics(media)
    except ValueError as exc:  # pragma: no cover - rely on logging for visibility
        logger.warning(str(exc))
        metadata = None

    if metadata:
        candidates = [
            metadata.get("runtime_minutes"),
            metadata.get("runtime"),
        ]
        details = metadata.get("details") if isinstance(metadata, dict) else None
        if isinstance(details, dict):
            candidates.append(details.get("runtime"))

        for candidate in candidates:
            if candidate is None:
                continue
            if isinstance(candidate, (int, float)):
                if candidate > 0:
                    metadata_runtime = int(candidate)
                    break
            else:
                parsed = parse_runtime_to_minutes(candidate)
                if parsed:
                    metadata_runtime = parsed
                    break

    # Exclude fallback values: 999998 (aired but runtime unknown) and 999999 (unknown runtime)
    if metadata_runtime and metadata_runtime < 999998:
        logger.debug(
            f"Media '{media.item.title}' {context}: fetched runtime {metadata_runtime} minutes",
        )
        if hasattr(media.item, "runtime_minutes"):
            try:
                with transaction.atomic():
                    media.item.runtime_minutes = metadata_runtime
                    media.item.save(update_fields=["runtime_minutes"])
                    media.item.refresh_from_db()  # Ensure consistency
            except Exception as exc:
                logger.warning(
                    f"Failed to save runtime for '{media.item.title}' {context}: {exc}",
                )
                # Continue with metadata_runtime value even if save fails
        return metadata_runtime

    logger.warning(
        f"Runtime data missing for media '{getattr(media.item, 'title', 'unknown')}' {context}, skipping",
    )
    return 0  # Skip this media instead of failing


def _get_media_metadata_for_statistics(media):
    """Get media metadata for statistics calculations."""
    # Use the same approach as media details page to get metadata
    try:
        normalized_type = media.item.media_type.lower()
        return providers.services.get_media_metadata(
            normalized_type,
            media.item.media_id,
            media.item.source,
        )
    except Exception as e:
        raise ValueError(f"Failed to get metadata for {media.item.title}: {e}")


def _calculate_movie_time(media, start_date, end_date, normalized_type, logger):
    """Calculate total time for movies and other media types using cached runtime data."""
    total_time_minutes = 0

    # Check if media is within date range
    if media.end_date and start_date and end_date:
        if start_date <= media.end_date <= end_date:
            total_time_minutes = _get_media_runtime_from_cache(media, logger, "(date range)")
    elif not start_date and not end_date:
        # All time
        total_time_minutes = _get_media_runtime_from_cache(media, logger, "(all time)")

    return total_time_minutes


def _calculate_music_time(media, start_date, end_date, logger):
    """Calculate total time for music plays using history records within date range.
    
    We deduplicate by end_date - each unique end_date represents one play event.
    Multiple history records with the same end_date are metadata updates, not separate plays.
    
    Additionally, we prefer history records where history_date is close to end_date,
    as those are more likely to be the actual play event rather than later metadata updates.
    """
    total_minutes = 0

    # Get the track runtime (in minutes)
    runtime_minutes = _get_music_runtime_minutes(media)
    if runtime_minutes <= 0:
        return 0

    # Get all history records ordered by history_date (oldest first)
    history_records = list(media.history.all().order_by("history_date"))

    if not history_records:
        return 0

    # Group history records by end_date to deduplicate
    # Each unique end_date represents one play, even if there are multiple history records
    # We'll use the history record closest to the end_date as the "canonical" one
    plays_by_end_date = {}  # end_date -> (history_record, history_date)

    for history_record in history_records:
        history_end_date = getattr(history_record, "end_date", None)
        history_date = getattr(history_record, "history_date", None)

        # Skip records without end_date (not a completed play)
        if not history_end_date or not history_date:
            continue

        # If we haven't seen this end_date, or this history_record is closer to the end_date,
        # use this one as the canonical record for this play
        if history_end_date not in plays_by_end_date:
            plays_by_end_date[history_end_date] = (history_record, history_date)
        else:
            # Prefer the history record where history_date is closest to end_date
            # (within reason - if history_date is way after end_date, it's likely a metadata update)
            existing_history_date = plays_by_end_date[history_end_date][1]
            time_diff_existing = abs((existing_history_date - history_end_date).total_seconds())
            time_diff_current = abs((history_date - history_end_date).total_seconds())

            # Prefer the one closer to end_date, but only if it's within 24 hours
            # (metadata updates can happen days/weeks later)
            if time_diff_current < time_diff_existing and time_diff_current < 86400:  # 24 hours
                plays_by_end_date[history_end_date] = (history_record, history_date)

    # Count unique plays within date range
    for play_end_date, (history_record, _) in plays_by_end_date.items():
        # Check if within date range
        if start_date and end_date:
            if start_date <= play_end_date <= end_date:
                total_minutes += runtime_minutes
        else:
            # All time - include all plays
            total_minutes += runtime_minutes

    return total_minutes


def _get_music_runtime_minutes(music_entry, track_duration_cache=None):
    """Get runtime in minutes from a Music entry, checking track and item.

    track_duration_cache (optional) should mirror history cache behavior:
      - (album_id, track_title) -> duration_ms
      - ("recording", recording_id) -> duration_ms
    """
    # First try the linked Track's duration_ms
    if music_entry.track and music_entry.track.duration_ms:
        return music_entry.track.duration_ms // 60000  # ms to minutes

    # Fall back to item runtime_minutes
    if music_entry.item and music_entry.item.runtime_minutes:
        return music_entry.item.runtime_minutes

    if music_entry.item:
        # Try to look up duration from cache (built from album tracklist)
        if track_duration_cache:
            if music_entry.album_id:
                title_key = (music_entry.album_id, music_entry.item.title)
                duration_ms = track_duration_cache.get(title_key)
                if duration_ms:
                    return duration_ms // 60000
            if music_entry.item.media_id:
                recording_key = ("recording", music_entry.item.media_id)
                duration_ms = track_duration_cache.get(recording_key)
                if duration_ms:
                    return duration_ms // 60000

        # Try to look up from album tracklist by recording ID
        if music_entry.album_id and music_entry.item.media_id:
            track = Track.objects.filter(
                album_id=music_entry.album_id,
                musicbrainz_recording_id=music_entry.item.media_id,
                duration_ms__isnull=False,
            ).first()
            if track:
                return track.duration_ms // 60000

        # Try to look up from album tracklist by title
        if music_entry.album_id and music_entry.item.title:
            track = Track.objects.filter(
                album_id=music_entry.album_id,
                title__iexact=music_entry.item.title,
                duration_ms__isnull=False,
            ).first()
            if track:
                return track.duration_ms // 60000

    return 0


def _localize_datetime(value):
    """Return the datetime converted to the current timezone if aware."""
    if value is None:
        return None

    if timezone.is_naive(value):
        return value
    return timezone.localtime(value)


def _compute_metric_breakdown(total_value, datetimes, start_date, end_date):
    """Return aggregate totals alongside per-year/month/day rates."""
    breakdown = {
        "total": total_value,
        "per_year": 0,
        "per_month": 0,
        "per_day": 0,
    }

    if total_value == 0 or not datetimes:
        return breakdown

    range_start = start_date or min(datetimes)
    range_end = end_date or max(datetimes)

    if range_start > range_end:
        range_start, range_end = range_end, range_start

    range_start = _localize_datetime(range_start)
    range_end = _localize_datetime(range_end)

    start_date_only = range_start.date()
    end_date_only = range_end.date()

    total_days = (end_date_only - start_date_only).days + 1
    if total_days <= 0:
        total_days = 1

    # Avoid exaggerated projections when the range is shorter than a month/year (e.g., new data)
    total_years = max(total_days / 365.25, 1)
    total_months = max(total_days / 30.4375, 1)

    breakdown["per_year"] = total_value / total_years if total_years else total_value
    breakdown["per_month"] = total_value / total_months if total_months else total_value
    breakdown["per_day"] = total_value / total_days if total_days else total_value

    return breakdown


def _build_single_series_chart(labels, values, color, dataset_label):
    """Return a Chart.js-friendly dataset for a single-series bar chart."""
    if not values or sum(values) == 0:
        return {"labels": [], "datasets": []}

    return {
        "labels": labels,
        "datasets": [
            {
                "label": dataset_label,
                "data": values,
                "background_color": color,
            },
        ],
    }


def _format_hour_label(hour):
    """Return a human-friendly label for an hour of day."""
    if hour == 0:
        return "12am"
    if hour < 12:
        return f"{hour}am"
    if hour == 12:
        return "12pm"
    return f"{hour - 12}pm"


def _build_media_charts(datetimes, color, dataset_label):
    """Build grouped chart datasets for the provided datetimes."""
    empty_chart = {"labels": [], "datasets": []}

    if not datetimes:
        return {
            "by_year": empty_chart,
            "by_month": empty_chart,
            "by_weekday": empty_chart,
            "by_time_of_day": empty_chart,
        }

    year_counts = Counter(dt.year for dt in datetimes)
    sorted_years = sorted(year_counts)
    year_labels = [str(year) for year in sorted_years]
    year_values = [year_counts[year] for year in sorted_years]

    month_counts = Counter(dt.month for dt in datetimes)
    month_labels = [calendar.month_abbr[i] for i in range(1, 13)]
    month_values = [month_counts.get(i, 0) for i in range(1, 13)]

    weekday_map = {
        0: "Mon",
        1: "Tue",
        2: "Wed",
        3: "Thu",
        4: "Fri",
        5: "Sat",
        6: "Sun",
    }
    weekday_order = [6, 0, 1, 2, 3, 4, 5]
    weekday_counts = Counter(dt.weekday() for dt in datetimes)
    weekday_labels = [weekday_map[index] for index in weekday_order]
    weekday_values = [weekday_counts.get(index, 0) for index in weekday_order]

    hour_counts = Counter(dt.hour for dt in datetimes)
    hour_labels = [_format_hour_label(hour) for hour in range(24)]
    hour_values = [hour_counts.get(hour, 0) for hour in range(24)]

    return {
        "by_year": _build_single_series_chart(
            year_labels,
            year_values,
            color,
            dataset_label,
        ),
        "by_month": _build_single_series_chart(
            month_labels,
            month_values,
            color,
            dataset_label,
        ),
        "by_weekday": _build_single_series_chart(
            weekday_labels,
            weekday_values,
            color,
            dataset_label,
        ),
        "by_time_of_day": _build_single_series_chart(
            hour_labels,
            hour_values,
            color,
            dataset_label,
        ),
    }


def _collect_episode_datetimes(tv_queryset, start_date, end_date):
    """Return localized episode completion datetimes for the queryset."""
    datetimes = []

    if tv_queryset is None:
        return datetimes

    for tv in tv_queryset:
        seasons = getattr(tv, "seasons", None)
        if seasons is None:
            continue

        for season in seasons.all():
            episodes = getattr(season, "episodes", None)
            if episodes is None:
                continue

            for episode in episodes.all():
                if not episode.end_date:
                    continue
                if not _is_episode_in_range(episode, start_date, end_date):
                    continue
                localized_date = _localize_datetime(episode.end_date)
                datetimes.append(localized_date)

    return datetimes


def _collect_movie_datetimes(movie_queryset, start_date, end_date):
    """Return localized movie completion datetimes for the queryset."""
    datetimes = []

    if movie_queryset is None:
        return datetimes

    for movie in movie_queryset:
        activity_date = _get_activity_datetime(movie)
        if activity_date is None:
            continue

        if start_date and end_date:
            if not (start_date <= activity_date <= end_date):
                continue

        datetimes.append(_localize_datetime(activity_date))

    return datetimes


def _collect_movie_play_data(movie_queryset, start_date, end_date):
    """Collect movie play datetimes and per-play runtime.
    
    Returns:
        tuple: (list of datetimes, list of (movie_entry, datetime, runtime_minutes) tuples)
    """
    datetimes = []
    play_details = []  # (movie_entry, datetime, runtime_minutes)

    if movie_queryset is None:
        return datetimes, play_details

    import logging
    logger = logging.getLogger(__name__)

    for movie in movie_queryset:
        activity_date = _get_activity_datetime(movie)
        if activity_date is None:
            continue

        if start_date and end_date:
            if not (start_date <= activity_date <= end_date):
                continue

        # Get runtime for this movie
        runtime_minutes = _get_media_runtime_from_cache(movie, logger, context="movie play data")
        if runtime_minutes <= 0:
            # Skip if no runtime available
            continue

        localized_date = _localize_datetime(activity_date)
        datetimes.append(localized_date)
        play_details.append((movie, localized_date, runtime_minutes))

    return datetimes, play_details


def _collect_tv_play_data(tv_queryset, start_date, end_date):
    """Collect TV episode play datetimes and per-play runtime.
    
    Returns:
        tuple: (list of datetimes, list of (episode_entry, datetime, runtime_minutes) tuples)
    """
    datetimes = []
    play_details = []  # (episode_entry, datetime, runtime_minutes)

    if tv_queryset is None:
        return datetimes, play_details

    import logging
    logger = logging.getLogger(__name__)

    for tv in tv_queryset:
        seasons = getattr(tv, "seasons", None)
        if seasons is None:
            continue

        for season in seasons.all():
            episodes = getattr(season, "episodes", None)
            if episodes is None:
                continue

            for episode in episodes.all():
                if not episode.end_date:
                    continue
                if not _is_episode_in_range(episode, start_date, end_date):
                    continue

                # Get runtime for this episode
                runtime_minutes = _get_media_runtime_from_cache(episode, logger, context="TV episode play data")
                if runtime_minutes <= 0:
                    # Skip if no runtime available
                    continue

                localized_date = _localize_datetime(episode.end_date)
                datetimes.append(localized_date)
                play_details.append((episode, localized_date, runtime_minutes))

    return datetimes, play_details


def get_tv_consumption_stats(user_media, start_date, end_date, minutes_per_type=None):
    """Return aggregate metrics and chart data for TV episode activity."""
    tv_queryset = (user_media or {}).get(MediaTypes.TV.value)
    episode_datetimes = _collect_episode_datetimes(tv_queryset, start_date, end_date)

    # Collect play details for genre calculation
    _, play_details = _collect_tv_play_data(tv_queryset, start_date, end_date)

    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(
            user_media or {},
            start_date,
            end_date,
            user=user,
        )

    total_minutes = minutes_per_type.get(MediaTypes.TV.value, 0)
    total_hours = total_minutes / 60 if total_minutes else 0
    total_plays = len(episode_datetimes)

    hours_breakdown = _compute_metric_breakdown(
        total_hours,
        episode_datetimes,
        start_date,
        end_date,
    )
    plays_breakdown = _compute_metric_breakdown(
        total_plays,
        episode_datetimes,
        start_date,
        end_date,
    )

    color = config.get_stats_color(MediaTypes.TV.value)
    chart_label = "Episode Plays"
    charts = _build_media_charts(episode_datetimes, color, chart_label)

    # Compute top genres
    top_genres = _compute_movie_tv_top_genres(play_details, limit=20)

    return {
        "hours": hours_breakdown,
        "plays": plays_breakdown,
        "charts": charts,
        "has_data": total_plays > 0,
        "top_genres": top_genres,
    }


def get_movie_consumption_stats(user_media, start_date, end_date, minutes_per_type=None):
    """Return aggregate metrics and chart data for movie activity."""
    movie_queryset = (user_media or {}).get(MediaTypes.MOVIE.value)
    movie_datetimes = _collect_movie_datetimes(movie_queryset, start_date, end_date)

    # Collect play details for genre calculation
    _, play_details = _collect_movie_play_data(movie_queryset, start_date, end_date)

    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(user_media or {}, start_date, end_date)

    total_minutes = minutes_per_type.get(MediaTypes.MOVIE.value, 0)
    total_hours = total_minutes / 60 if total_minutes else 0
    total_plays = len(movie_datetimes)

    hours_breakdown = _compute_metric_breakdown(
        total_hours,
        movie_datetimes,
        start_date,
        end_date,
    )
    plays_breakdown = _compute_metric_breakdown(
        total_plays,
        movie_datetimes,
        start_date,
        end_date,
    )

    color = config.get_stats_color(MediaTypes.MOVIE.value)
    chart_label = "Movie Plays"
    charts = _build_media_charts(movie_datetimes, color, chart_label)

    # Compute top genres
    top_genres = _compute_movie_tv_top_genres(play_details, limit=20)

    return {
        "hours": hours_breakdown,
        "plays": plays_breakdown,
        "charts": charts,
        "has_data": total_plays > 0,
        "top_genres": top_genres,
    }


def _reading_entry_in_range(entry, start_date, end_date):
    """Return True if a reading entry overlaps the requested date range."""
    if not (start_date and end_date):
        return True

    filter_start = start_date.date() if hasattr(start_date, "date") else start_date
    filter_end = end_date.date() if hasattr(end_date, "date") else end_date

    entry_start = entry.start_date.date() if entry.start_date else None
    entry_end = entry.end_date.date() if entry.end_date else None

    if entry_start and entry_end:
        return not (entry_end < filter_start or entry_start > filter_end)
    if entry_end:
        return filter_start <= entry_end <= filter_end
    if entry_start:
        return filter_start <= entry_start <= filter_end

    activity_datetime = _get_activity_datetime(entry)
    if activity_datetime is None:
        return False
    activity_date = _localize_datetime(activity_datetime).date()
    return filter_start <= activity_date <= filter_end


def _format_reading_unit(value, unit_name):
    numeric = int(round(value or 0))
    unit_lower = (unit_name or "Unit").lower()
    if numeric == 1:
        return f"{numeric} {unit_lower}"
    return f"{numeric} {unit_lower}s"


def _normalize_item_author_names(raw_authors):
    """Normalize stored item authors into a unique list of display names."""
    if not raw_authors:
        return []
    if not isinstance(raw_authors, list):
        raw_authors = [raw_authors]

    names = []
    seen = set()
    for raw_author in raw_authors:
        if isinstance(raw_author, dict):
            author_name = (
                raw_author.get("name")
                or raw_author.get("person")
                or raw_author.get("author")
            )
        else:
            author_name = raw_author

        author_text = str(author_name).strip() if author_name else ""
        if not author_text:
            continue

        dedupe_key = author_text.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        names.append(author_text)

    return names


def _extract_cached_item_authors(item):
    """Return author names from provider cache for legacy items without persisted authors."""
    if not item:
        return []

    cache_key = f"{item.source}_{item.media_type}_{item.media_id}"
    cached = cache.get(cache_key)
    if not isinstance(cached, dict):
        return []

    details = cached.get("details") if isinstance(cached.get("details"), dict) else {}
    raw_authors = (
        details.get("authors")
        or details.get("author")
        or details.get("people")
        or cached.get("authors")
        or cached.get("author")
        or cached.get("people")
    )
    author_names = _normalize_item_author_names(raw_authors)
    if author_names:
        return author_names
    return _normalize_item_author_names(cached.get("authors_full"))


def _fetch_reading_items_with_authors(item_ids):
    """Fetch Item objects with author credits prefetched for reading rollups."""
    if not item_ids:
        return {}

    Item = apps.get_model("app", "Item")
    author_credit_prefetch = Prefetch(
        "person_credits",
        queryset=ItemPersonCredit.objects.filter(
            role_type=CreditRoleType.AUTHOR.value,
        ).select_related("person"),
        to_attr="prefetched_author_credits",
    )
    return {
        item.id: item
        for item in Item.objects.filter(id__in=item_ids).prefetch_related(author_credit_prefetch)
    }


def _extract_item_authors(item):
    """Return normalized author payloads for an item, preferring persisted credits."""
    if not item:
        return []

    authors = []
    seen = set()

    author_credits = getattr(item, "prefetched_author_credits", None)
    if author_credits is None and hasattr(item, "person_credits"):
        author_credits = item.person_credits.filter(
            role_type=CreditRoleType.AUTHOR.value,
        ).select_related("person")

    for credit in author_credits or []:
        person = getattr(credit, "person", None)
        name = (getattr(person, "name", "") or "").strip()
        person_id = getattr(person, "source_person_id", "")
        source = getattr(person, "source", "")
        if not name or not source or not person_id:
            continue

        dedupe_key = (source, str(person_id))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        authors.append(
            {
                "name": name,
                "image": getattr(person, "image", "") or "",
                "source": source,
                "person_id": str(person_id),
            },
        )

    if authors:
        return authors

    fallback_names = _normalize_item_author_names(getattr(item, "authors", None))
    if not fallback_names:
        fallback_names = _extract_cached_item_authors(item)

    for name in fallback_names:
        authors.append(
            {
                "name": name,
                "image": "",
                "source": "",
                "person_id": "",
            },
        )
    return authors


def _build_reading_top_authors(item_units, unit_name, limit=20):
    """Aggregate reading units by author for top-author overview cards."""
    author_stats = defaultdict(
        lambda: {
            "units": 0,
            "titles": set(),
            "name": "",
            "image": "",
            "source": "",
            "person_id": "",
        },
    )

    for item, units in item_units:
        if not item or (units or 0) <= 0:
            continue

        item_id = getattr(item, "id", None)
        for author in _extract_item_authors(item):
            name = (author.get("name") or "").strip()
            if not name:
                continue

            source = author.get("source") or ""
            person_id = author.get("person_id") or ""
            if source and person_id:
                dedupe_key = (source, str(person_id))
            else:
                dedupe_key = ("name", name.casefold())

            payload = author_stats[dedupe_key]
            payload["units"] += units
            if item_id is not None:
                payload["titles"].add(item_id)
            for field in ("name", "image", "source", "person_id"):
                if author.get(field) and not payload.get(field):
                    payload[field] = author.get(field)

    top_authors = sorted(
        author_stats.values(),
        key=lambda item: (-item["units"], -len(item["titles"]), item["name"].lower()),
    )[:limit]

    return [
        {
            "name": payload["name"],
            "image": payload["image"],
            "source": payload["source"],
            "person_id": payload["person_id"],
            "units": payload["units"],
            "titles": len(payload["titles"]),
            "formatted_units": _format_reading_unit(payload["units"], unit_name),
        }
        for payload in top_authors
    ]


def _build_weighted_media_charts(weighted_datetimes, color, dataset_label):
    """Build grouped chart datasets where each datetime contributes weighted value."""
    empty_chart = {"labels": [], "datasets": []}
    if not weighted_datetimes:
        return {
            "by_year": empty_chart,
            "by_month": empty_chart,
            "by_weekday": empty_chart,
            "by_time_of_day": empty_chart,
        }

    year_totals = Counter()
    month_totals = Counter()
    weekday_totals = Counter()
    hour_totals = Counter()

    for dt, value in weighted_datetimes:
        numeric_value = float(value or 0)
        if numeric_value <= 0:
            continue
        year_totals[dt.year] += numeric_value
        month_totals[dt.month] += numeric_value
        weekday_totals[dt.weekday()] += numeric_value
        hour_totals[dt.hour] += numeric_value

    sorted_years = sorted(year_totals.keys())
    year_labels = [str(year) for year in sorted_years]
    year_values = [year_totals[year] for year in sorted_years]

    month_labels = [calendar.month_abbr[i] for i in range(1, 13)]
    month_values = [month_totals.get(i, 0) for i in range(1, 13)]

    weekday_map = {
        0: "Mon",
        1: "Tue",
        2: "Wed",
        3: "Thu",
        4: "Fri",
        5: "Sat",
        6: "Sun",
    }
    weekday_order = [6, 0, 1, 2, 3, 4, 5]
    weekday_labels = [weekday_map[index] for index in weekday_order]
    weekday_values = [weekday_totals.get(index, 0) for index in weekday_order]

    hour_labels = [_format_hour_label(hour) for hour in range(24)]
    hour_values = [hour_totals.get(hour, 0) for hour in range(24)]

    return {
        "by_year": _build_single_series_chart(year_labels, year_values, color, dataset_label),
        "by_month": _build_single_series_chart(month_labels, month_values, color, dataset_label),
        "by_weekday": _build_single_series_chart(weekday_labels, weekday_values, color, dataset_label),
        "by_time_of_day": _build_single_series_chart(hour_labels, hour_values, color, dataset_label),
    }


def get_reading_consumption_stats(user_media, start_date, end_date, media_type):
    """Return aggregate metrics and chart/top-list data for book/comic/manga activity."""
    queryset = (user_media or {}).get(media_type)
    if queryset is None:
        queryset = []
    elif hasattr(queryset, "select_related"):
        queryset = queryset.select_related("item")

    unit_name = config.get_unit(media_type, short=False) or "Unit"
    media_label_map = {
        MediaTypes.BOOK.value: "Books Finished",
        MediaTypes.COMIC.value: "Comics Finished",
        MediaTypes.MANGA.value: "Manga Finished",
    }
    completion_label = media_label_map.get(media_type, "Items Finished")
    release_label_map = {
        MediaTypes.BOOK.value: "Books Released",
        MediaTypes.COMIC.value: "Comics Released",
        MediaTypes.MANGA.value: "Manga Released",
    }
    release_label = release_label_map.get(media_type, "Items Released")
    chart_label = f"{unit_name}s Read"
    color = config.get_stats_color(media_type)

    grouped_entries = defaultdict(list)
    units_by_day = defaultdict(float)
    release_datetimes = []
    release_item_ids = set()
    for entry in list(queryset):
        if not getattr(entry, "item", None):
            continue
        if not _reading_entry_in_range(entry, start_date, end_date):
            continue
        grouped_entries[entry.item.id].append(entry)

        release_dt = getattr(entry.item, "release_datetime", None)
        if release_dt and entry.item.id not in release_item_ids:
            release_item_ids.add(entry.item.id)
            release_datetimes.append(release_dt)

        total_units = entry.progress or 0
        if total_units <= 0:
            continue

        activity_dt = _get_activity_datetime(entry) or entry.created_at
        start_dt = entry.start_date
        end_dt = entry.end_date
        start_local = _localize_datetime(start_dt).date() if start_dt else None
        end_local = _localize_datetime(end_dt).date() if end_dt else None
        filter_start = start_date.date() if start_date else None
        filter_end = end_date.date() if end_date else None

        if start_local and end_local and start_local <= end_local:
            span_start = start_local
            span_end = end_local
            if filter_start and span_start < filter_start:
                span_start = filter_start
            if filter_end and span_end > filter_end:
                span_end = filter_end
            total_days = (span_end - span_start).days + 1
            per_day = total_units / total_days if total_days else total_units
            for offset in range(total_days):
                day = span_start + datetime.timedelta(days=offset)
                units_by_day[day.isoformat()] += per_day
        else:
            activity_local = _localize_datetime(activity_dt)
            if activity_local:
                activity_day = activity_local.date()
                if filter_start and activity_day < filter_start:
                    continue
                if filter_end and activity_day > filter_end:
                    continue
                units_by_day[activity_day.isoformat()] += total_units

    weighted_datetimes = []
    weighted_datetimes_only = []
    completed_datetimes = []
    completed_lengths = []
    top_items = []
    author_item_units = []
    genre_stats = defaultdict(lambda: {"units": 0, "title_ids": set(), "name": ""})
    item_lengths = []
    scored_items = []
    longest_item = None
    shortest_item = None
    total_units = 0
    items_with_authors = _fetch_reading_items_with_authors(grouped_entries.keys())

    for item_id, entries in grouped_entries.items():
        total_item_units = sum((entry.progress or 0) for entry in entries)
        if total_item_units <= 0:
            total_item_units = 0

        latest_entry = max(
            entries,
            key=lambda entry: _get_activity_datetime(entry) or entry.created_at,
        )
        latest_activity = _get_activity_datetime(latest_entry) or latest_entry.created_at
        localized_activity = _localize_datetime(latest_activity)

        if total_item_units > 0:
            weighted_datetimes.append((localized_activity, total_item_units))
            weighted_datetimes_only.append(localized_activity)
            total_units += total_item_units
            author_item_units.append(
                (
                    items_with_authors.get(item_id) or latest_entry.item,
                    total_item_units,
                ),
            )

            top_items.append(
                {
                    "media": latest_entry,
                    "units": total_item_units,
                    "entry_count": len(entries),
                    "formatted_units": _format_reading_unit(total_item_units, unit_name),
                }
            )

            for genre in _coerce_genre_list(getattr(latest_entry.item, "genres", [])):
                key = str(genre).title()
                genre_stats[key]["units"] += total_item_units
                genre_stats[key]["name"] = key
                genre_stats[key]["title_ids"].add(item_id)

        completed_candidates = [
            entry
            for entry in entries
            if entry.status == Status.COMPLETED.value
        ]
        if completed_candidates:
            latest_completed = max(
                completed_candidates,
                key=lambda entry: _get_activity_datetime(entry) or entry.created_at,
            )
            completed_dt = _get_activity_datetime(latest_completed) or latest_completed.created_at
            completed_datetimes.append(_localize_datetime(completed_dt))
            completed_length = latest_completed.progress or getattr(latest_completed.item, "number_of_pages", 0) or 0
            if completed_length > 0:
                completed_lengths.append(completed_length)

        pages_value = getattr(latest_entry.item, "number_of_pages", None)
        if pages_value and pages_value > 0:
            item_lengths.append(pages_value)
            if longest_item is None or pages_value > longest_item["value"]:
                longest_item = {"media": latest_entry, "value": pages_value}
            if shortest_item is None or pages_value < shortest_item["value"]:
                shortest_item = {"media": latest_entry, "value": pages_value}

        score_value = getattr(latest_entry, "aggregated_score", None)
        if score_value is None:
            score_value = latest_entry.score
        if score_value is not None:
            scored_items.append(float(score_value))

    top_items = sorted(top_items, key=lambda item: item["units"], reverse=True)[:20]

    top_genres = []
    for payload in sorted(
        genre_stats.values(),
        key=lambda item: (item["units"], len(item["title_ids"])),
        reverse=True,
    )[:20]:
        top_genres.append(
            {
                "name": payload["name"],
                "units": payload["units"],
                "titles": len(payload["title_ids"]),
                "formatted_units": _format_reading_unit(payload["units"], unit_name),
            }
        )
    top_authors = _build_reading_top_authors(author_item_units, unit_name, limit=20)

    avg_length = round(sum(item_lengths) / len(item_lengths), 1) if item_lengths else 0
    avg_rating = round(sum(scored_items) / len(scored_items), 2) if scored_items else None

    charts = _build_weighted_media_charts(weighted_datetimes, color, chart_label)
    completion_charts = _build_media_charts(completed_datetimes, color, completion_label)
    completed_length_chart = _build_completed_length_distribution_chart(completed_lengths, unit_name, color)
    release_chart = _build_release_year_chart(release_datetimes, color, release_label)

    units_breakdown = _compute_metric_breakdown(
        total_units,
        weighted_datetimes_only,
        start_date,
        end_date,
    )
    completion_breakdown = _compute_metric_breakdown(
        len(completed_datetimes),
        completed_datetimes,
        start_date,
        end_date,
    )

    return {
        "units": units_breakdown,
        "completions": completion_breakdown,
        "charts": charts,
        "completion_charts": {
            "by_year": completion_charts["by_year"],
            "by_month": completion_charts["by_month"],
        },
        "completed_length_chart": completed_length_chart,
        "release_chart": release_chart,
        "has_data": total_units > 0 or len(completed_datetimes) > 0,
        "unit_name": unit_name,
        "unit_label": chart_label,
        "completion_label": completion_label,
        "top_items": top_items,
        "top_authors": top_authors,
        "top_genres": top_genres,
        "highlights": {
            "longest_item": longest_item,
            "shortest_item": shortest_item,
            "average_length": avg_length,
            "average_rating": avg_rating,
        },
    }


def _game_entry_in_range(game, start_date, end_date):
    """Return True if a game entry overlaps the requested date range."""
    if not (start_date and end_date):
        return True

    filter_start = start_date.date() if hasattr(start_date, "date") else start_date
    filter_end = end_date.date() if hasattr(end_date, "date") else end_date

    game_start = game.start_date.date() if game.start_date else None
    game_end = game.end_date.date() if game.end_date else None

    if game_start and game_end:
        return not (game_end < filter_start or game_start > filter_end)
    if game_end:
        return filter_start <= game_end <= filter_end
    if game_start:
        return filter_start <= game_start <= filter_end

    activity_datetime = _get_activity_datetime(game)
    if activity_datetime is None:
        return False
    activity_date = _localize_datetime(activity_datetime).date()
    return filter_start <= activity_date <= filter_end


def _collect_game_data(game_queryset, start_date, end_date):
    """Collect game data with hours, dates, and daily averages.
    
    Returns:
        list of dicts with keys: game, hours, start_date, end_date, daily_average, activity_datetime
    """
    game_data = []

    if game_queryset is None:
        return game_data

    games_by_item = defaultdict(list)
    for game in list(game_queryset):
        if not getattr(game, "item", None):
            continue
        if not _game_entry_in_range(game, start_date, end_date):
            continue
        games_by_item[game.item.id].append(game)

    for entries in games_by_item.values():
        total_minutes = sum((entry.progress or 0) for entry in entries)
        total_hours = total_minutes / 60 if total_minutes else 0
        if total_hours <= 0:
            continue

        activity_datetime = None
        for entry in entries:
            entry_activity = _get_activity_datetime(entry)
            if entry_activity and (activity_datetime is None or entry_activity > activity_datetime):
                activity_datetime = entry_activity
        if activity_datetime is None:
            continue

        start_dates = []
        end_dates = []
        segments = []
        days_played = set()
        total_minutes_for_avg = 0

        for entry in entries:
            entry_minutes = entry.progress or 0
            entry_start = entry.start_date
            entry_end = entry.end_date

            if entry_start:
                start_dates.append(timezone.localtime(entry_start).date())
            if entry_end:
                end_dates.append(timezone.localtime(entry_end).date())

            if entry_minutes > 0:
                total_minutes_for_avg += entry_minutes
                days_played.update(_get_entry_play_dates(entry))

            if entry_start and entry_end:
                start_local = timezone.localtime(entry_start).date()
                end_local = timezone.localtime(entry_end).date()

                if entry_minutes > 0:
                    segments.append({
                        "start_date": start_local,
                        "end_date": end_local,
                        "hours": entry_minutes / 60,
                        "activity_datetime": _get_activity_datetime(entry),
                    })
            elif entry_minutes > 0:
                segments.append({
                    "start_date": None,
                    "end_date": None,
                    "hours": entry_minutes / 60,
                    "activity_datetime": _get_activity_datetime(entry),
                })

        total_days = len(days_played)
        if total_days:
            daily_average_hours = (total_minutes_for_avg / total_days) / 60
        else:
            daily_average_hours = 0

        game_data.append({
            "game": max(
                entries,
                key=lambda entry: _get_activity_datetime(entry) or entry.created_at,
            ),
            "hours": total_hours,
            "start_date": min(start_dates) if start_dates else None,
            "end_date": max(end_dates) if end_dates else None,
            "daily_average": daily_average_hours,
            "activity_datetime": activity_datetime,
            "segments": segments,
        })

    return game_data


def _collect_game_play_data(game_queryset, start_date, end_date):
    """Collect game play data for genre calculation.
    
    Returns:
        tuple: (list of datetimes, list of (game_entry, datetime, runtime_minutes) tuples)
    """
    datetimes = []
    play_details = []  # (game_entry, datetime, runtime_minutes)

    if game_queryset is None:
        return datetimes, play_details

    for game in game_queryset:
        activity_date = _get_activity_datetime(game)
        if activity_date is None:
            continue

        # Check if game is within date range (similar logic to _collect_game_data)
        if not _game_entry_in_range(game, start_date, end_date):
            continue
        
        # Get runtime in minutes (from progress field)
        runtime_minutes = game.progress or 0
        if runtime_minutes <= 0:
            continue

        localized_date = _localize_datetime(activity_date)
        datetimes.append(localized_date)
        play_details.append((game, localized_date, runtime_minutes))

    return datetimes, play_details


def _build_game_hours_charts(game_data, start_date, end_date, color, dataset_label):
    """Build hours-by-year and hours-by-month charts for games.
    
    Hours are evenly distributed across the date range of each game.
    """
    empty_chart = {"labels": [], "datasets": []}

    if not game_data:
        return {
            "by_year": empty_chart,
            "by_month": empty_chart,
        }

    # Initialize counters
    year_hours = defaultdict(float)
    month_hours = defaultdict(float)

    # Determine filter range dates
    if start_date and hasattr(start_date, "date"):
        filter_start_date = start_date.date()
    elif start_date:
        filter_start_date = start_date
    else:
        filter_start_date = None

    if end_date and hasattr(end_date, "date"):
        filter_end_date = end_date.date()
    elif end_date:
        filter_end_date = end_date
    else:
        filter_end_date = None

    for data in game_data:
        game = data["game"]
        total_hours = data["hours"]
        game_start = data["start_date"]
        game_end = data["end_date"]
        segments = data.get("segments")

        if segments:
            for segment in segments:
                segment_hours = segment.get("hours", 0) or 0
                if segment_hours <= 0:
                    continue

                segment_start = segment.get("start_date")
                segment_end = segment.get("end_date")

                if not segment_start or not segment_end:
                    activity_dt = segment.get("activity_datetime") or data.get("activity_datetime")
                    if not activity_dt:
                        continue

                    activity_date = _localize_datetime(activity_dt).date()
                    if filter_start_date and filter_end_date:
                        if not (filter_start_date <= activity_date <= filter_end_date):
                            continue
                    year_hours[activity_date.year] += segment_hours
                    month_hours[activity_date.month] += segment_hours
                    continue

                segment_total_days = (segment_end - segment_start).days + 1
                if segment_total_days <= 0:
                    segment_total_days = 1

                hours_per_day = segment_hours / segment_total_days

                range_start = segment_start
                range_end = segment_end
                if filter_start_date and filter_end_date:
                    range_start = max(range_start, filter_start_date)
                    range_end = min(range_end, filter_end_date)
                    if range_start > range_end:
                        continue

                current_date = range_start
                while current_date <= range_end:
                    if not filter_start_date or filter_start_date <= current_date <= filter_end_date:
                        year_hours[current_date.year] += hours_per_day
                        month_hours[current_date.month] += hours_per_day
                    current_date += datetime.timedelta(days=1)
            continue
        
        if not game_start or not game_end:
            # If no date range, assign all hours to activity date
            activity_date = _localize_datetime(data["activity_datetime"]).date()
            if filter_start_date and filter_end_date:
                if filter_start_date <= activity_date <= filter_end_date:
                    year_hours[activity_date.year] += total_hours
                    month_hours[activity_date.month] += total_hours
            elif not filter_start_date and not filter_end_date:
                year_hours[activity_date.year] += total_hours
                month_hours[activity_date.month] += total_hours
            continue

        # Calculate daily average based on full game duration
        game_total_days = (game_end - game_start).days + 1
        if game_total_days <= 0:
            game_total_days = 1

        # Calculate hours per day based on full game duration
        hours_per_day = total_hours / game_total_days

        # Calculate date range (intersection with filter range if applicable)
        range_start = game_start
        range_end = game_end

        if filter_start_date and filter_end_date:
            # Intersect with filter range
            range_start = max(range_start, filter_start_date)
            range_end = min(range_end, filter_end_date)
            if range_start > range_end:
                continue

        # Aggregate hours by year and month
        current_date = range_start
        while current_date <= range_end:
            if not filter_start_date or filter_start_date <= current_date <= filter_end_date:
                year_hours[current_date.year] += hours_per_day
                month_hours[current_date.month] += hours_per_day

            current_date += datetime.timedelta(days=1)

    # Build year chart
    if year_hours:
        sorted_years = sorted(year_hours.keys())
        year_labels = [str(year) for year in sorted_years]
        year_values = [year_hours[year] for year in sorted_years]
        year_chart = _build_single_series_chart(year_labels, year_values, color, dataset_label)
    else:
        year_chart = empty_chart

    # Build month chart
    month_labels = [calendar.month_abbr[i] for i in range(1, 13)]
    month_values = [month_hours.get(i, 0) for i in range(1, 13)]
    month_chart = _build_single_series_chart(month_labels, month_values, color, dataset_label)

    return {
        "by_year": year_chart,
        "by_month": month_chart,
    }


DAILY_AVERAGE_BANDS = [
    (0, 5/60, "5 min"),             # 0 to 5 minutes
    (5/60, 15/60, "15 min"),        # 5 to 15 minutes
    (15/60, 30/60, "30 min"),       # 15 to 30 minutes
    (30/60, 1, "60 min"),           # 30 minutes to 1 hour
    (1, 2, "2 hr"),                 # 1 to 2 hours
    (2, 4, "4 hr"),                 # 2 to 4 hours
    (4, float("inf"), "4+ hr"),     # 4+ hours
]


def _get_daily_average_band_index(daily_avg_hours):
    """Return the band index for a given daily average hours value."""
    for i, (min_hours, max_hours, _) in enumerate(DAILY_AVERAGE_BANDS):
        if min_hours <= daily_avg_hours < max_hours:
            return i
    if daily_avg_hours >= DAILY_AVERAGE_BANDS[-1][0]:
        return len(DAILY_AVERAGE_BANDS) - 1
    return -1


def _build_daily_average_band_top_games(game_data, limit=5):
    """Build a dict mapping each band label to the top N games by daily average.

    Each entry in the returned dict is a list of serialisable dicts:
        {"title": str, "image": str, "formatted_daily_average": str}

    Only game_data dicts that contain a "game" key (Game model instance with an
    .item FK) will contribute title/image; dicts with only bare fields (cache path)
    are expected to already have "title" and "image" injected by the caller.
    """
    from app.helpers import minutes_to_hhmm

    band_games = {label: [] for _, _, label in DAILY_AVERAGE_BANDS}

    for data in game_data:
        daily_avg_hours = data.get("daily_average", 0)
        if daily_avg_hours <= 0:
            continue
        idx = _get_daily_average_band_index(daily_avg_hours)
        if idx < 0:
            continue
        band_label = DAILY_AVERAGE_BANDS[idx][2]

        game_obj = data.get("game")
        if game_obj is not None:
            item = getattr(game_obj, "item", None)
            title = item.title if item else ""
            image = item.image if item else ""
        else:
            title = data.get("title", "")
            image = data.get("image", "")

        daily_avg_minutes = daily_avg_hours * 60
        band_games[band_label].append({
            "title": title,
            "image": image,
            "formatted_daily_average": minutes_to_hhmm(daily_avg_minutes) + "/day",
            "_sort_key": daily_avg_hours,
        })

    result = {}
    for label, games in band_games.items():
        sorted_games = sorted(games, key=lambda g: g["_sort_key"], reverse=True)[:limit]
        for g in sorted_games:
            g.pop("_sort_key", None)
        if sorted_games:
            result[label] = sorted_games

    return result


def _build_daily_average_distribution_chart(game_data, color, dataset_label):
    """Build chart showing distribution of games by daily average time bands.

    Returns chart data with labels (time bands) and values (number of games),
    plus a ``top_games_per_band`` key with the top 5 games per band (serialisable).
    """
    empty_chart = {"labels": [], "datasets": [], "top_games_per_band": {}}

    if not game_data:
        return empty_chart

    bands = DAILY_AVERAGE_BANDS

    # Count games in each band
    band_counts = [0] * len(bands)

    for data in game_data:
        daily_avg_hours = data["daily_average"]
        idx = _get_daily_average_band_index(daily_avg_hours)
        if 0 <= idx < len(bands):
            band_counts[idx] += 1

    # Extract labels
    labels = [label for _, _, label in bands]

    # Build chart and attach top-games-per-band tooltip data
    chart = _build_single_series_chart(labels, band_counts, color, dataset_label)
    chart["top_games_per_band"] = _build_daily_average_band_top_games(game_data)
    return chart


def _build_completed_length_distribution_chart(values, unit_name, color):
    """Build chart showing distribution of completed item lengths."""
    empty_chart = {"labels": [], "datasets": []}
    if not values:
        return empty_chart

    values = [value for value in values if value and value > 0]
    if not values:
        return empty_chart

    # Define unit bands (completed length).
    bands = [
        (0, 50, "1-50"),
        (50, 100, "51-100"),
        (100, 200, "101-200"),
        (200, 300, "201-300"),
        (300, 500, "301-500"),
        (500, 800, "501-800"),
        (800, 1200, "801-1200"),
        (1200, float("inf"), "1200+"),
    ]

    band_counts = [0] * len(bands)
    for value in values:
        for i, (min_units, max_units, _) in enumerate(bands):
            if min_units < value <= max_units:
                band_counts[i] += 1
                break
        else:
            if value > bands[-1][0]:
                band_counts[-1] += 1

    labels = [label for _, _, label in bands]
    dataset_label = f"Completed {unit_name}s"
    return _build_single_series_chart(labels, band_counts, color, dataset_label)


def _build_release_year_chart(release_datetimes, color, dataset_label):
    """Build chart for items released per year."""
    empty_chart = {"labels": [], "datasets": []}
    if not release_datetimes:
        return empty_chart

    year_totals = defaultdict(int)
    for release_dt in release_datetimes:
        if not release_dt:
            continue
        if isinstance(release_dt, datetime.datetime):
            year_totals[release_dt.year] += 1
        else:
            try:
                year_totals[release_dt.year] += 1
            except AttributeError:
                continue

    if not year_totals:
        return empty_chart

    sorted_years = sorted(year_totals.keys())
    year_labels = [str(year) for year in sorted_years]
    year_values = [year_totals[year] for year in sorted_years]
    return _build_single_series_chart(year_labels, year_values, color, dataset_label)


def _compute_game_top_genres(play_details, limit=20):
    """Compute top genres from game play details using stored genres and cache.

    Args:
        play_details: List of (game_entry, datetime, runtime_minutes) tuples
        limit: Number of genres to return

    Returns:
        list of genre dicts with name, minutes, games, formatted_duration
    """
    from django.core.cache import cache

    from app.helpers import minutes_to_hhmm
    from app.models import Sources

    genre_stats = defaultdict(
        lambda: {"minutes": 0, "game_ids": set(), "name": ""}
    )

    for game, dt, runtime in play_details:
        minutes = runtime or 0

        # Get genres from stored item or cached metadata only (don't trigger API calls)
        genres = []
        if hasattr(game, "item") and game.item:
            genres = _coerce_genre_list(getattr(game.item, "genres", None))

            if not genres:
                # Try to get genres from cache directly
                cache_key = f"{Sources.IGDB.value}_{MediaTypes.GAME.value}_{game.item.media_id}"
                cached_metadata = cache.get(cache_key)
                
                if cached_metadata:
                    # Extract genres from cached metadata
                    genres_raw = cached_metadata.get("genres", [])
                    if genres_raw:
                        genres = _coerce_genre_list(genres_raw)
                    # Also check details.genres if top-level is empty
                    if not genres:
                        details = cached_metadata.get("details", {})
                        if isinstance(details, dict):
                            genres_raw = details.get("genres", [])
                            if genres_raw:
                                genres = _coerce_genre_list(genres_raw)
                
                if genres and genres != game.item.genres:
                    game.item.genres = genres
                    game.item.save(update_fields=["genres"])
        
        game_id = None
        if hasattr(game, "item") and game.item:
            game_id = game.item_id
        elif hasattr(game, "id"):
            game_id = game.id

        for genre in genres:
            key = str(genre).title()
            genre_stats[key]["minutes"] += minutes
            genre_stats[key]["name"] = key
            if game_id is not None:
                genre_stats[key]["game_ids"].add(game_id)

    # Sort by minutes (descending), then by games (descending)
    items = sorted(
        genre_stats.values(),
        key=lambda x: (x["minutes"], len(x["game_ids"])),
        reverse=True,
    )[:limit]

    # Format durations
    for item in items:
        item["formatted_duration"] = minutes_to_hhmm(item["minutes"])
        item["games"] = len(item["game_ids"])
        item["plays"] = item["games"]
        item.pop("game_ids", None)

    return items


def _compute_game_top_daily_average(game_data, limit=20):
    """Compute top games by daily average time spent.
    
    Args:
        game_data: List of game data dicts from _collect_game_data
        limit: Number of games to return
        
    Returns:
        list of dicts with game info and daily_average
    """
    from app.helpers import minutes_to_hhmm

    # Filter games with valid daily averages and sort
    games_with_average = [
        data for data in game_data
        if data["daily_average"] > 0
    ]

    # Sort by daily average (descending)
    sorted_games = sorted(
        games_with_average,
        key=lambda x: x["daily_average"],
        reverse=True,
    )[:limit]

    # Format results
    results = []
    for data in sorted_games:
        game = data["game"]
        daily_avg_hours = data["daily_average"]
        daily_avg_minutes = daily_avg_hours * 60

        results.append({
            "game": game,
            "daily_average_hours": daily_avg_hours,
            "daily_average_minutes": daily_avg_minutes,
            "formatted_daily_average": minutes_to_hhmm(daily_avg_minutes) + "/day",
            "total_hours": data["hours"],
            "formatted_total": minutes_to_hhmm(data["hours"] * 60),
        })

    return results


def _compute_game_platform_breakdown(game_data, user):
    """Compute a breakdown of hours and unique game counts per platform.

    Platform detection priority:
      1. CollectionEntry.resolution  (user's explicitly chosen platform)
      2. item.platforms if exactly one platform listed on the Item
      3. Skip (multi-platform game with no collection data)

    Args:
        game_data: List of game data dicts from _collect_game_data (each has
                   a "game" key pointing to a Game model instance and "hours").
        user: The Django user instance used to query CollectionEntry.

    Returns:
        List of dicts sorted by hours desc:
            {"name": str, "games": int, "hours": float, "formatted_hours": str}
    """
    from app.helpers import minutes_to_hhmm
    from app.models import CollectionEntry

    if not game_data or not user:
        return []

    # Collect all unique item_ids from this period's game_data
    item_to_hours = {}  # item_id -> total hours across all entries for that item
    item_obj_map = {}   # item_id -> item object (for platforms list)
    for data in game_data:
        game = data.get("game")
        if not game:
            continue
        item = getattr(game, "item", None)
        if not item:
            continue
        item_id = item.id
        item_to_hours[item_id] = item_to_hours.get(item_id, 0) + data["hours"]
        item_obj_map[item_id] = item

    if not item_to_hours:
        return []

    # Fetch collection entries for these items so we can read the resolution field
    collection_platform_map = {}  # item_id -> platform string
    ce_qs = CollectionEntry.objects.filter(
        user=user,
        item_id__in=list(item_to_hours.keys()),
    ).values("item_id", "resolution")
    for ce in ce_qs:
        resolution = (ce.get("resolution") or "").strip()
        if resolution:
            collection_platform_map[ce["item_id"]] = resolution

    # Aggregate per platform
    platform_hours = defaultdict(float)
    platform_game_ids = defaultdict(set)

    for item_id, hours in item_to_hours.items():
        item = item_obj_map.get(item_id)
        if not item:
            continue

        # Priority 1: collection entry resolution
        platform = collection_platform_map.get(item_id)

        # Priority 2: single IGDB platform
        if not platform:
            item_platforms = item.platforms if isinstance(item.platforms, list) else []
            item_platforms = [str(p).strip() for p in item_platforms if str(p).strip()]
            if len(item_platforms) == 1:
                platform = item_platforms[0]

        # Skip if we can't determine the platform
        if not platform:
            continue

        platform_hours[platform] += hours
        platform_game_ids[platform].add(item_id)

    if not platform_hours:
        return []

    results = []
    for platform_name, hours in platform_hours.items():
        results.append({
            "name": platform_name,
            "games": len(platform_game_ids[platform_name]),
            "hours": hours,
            "formatted_hours": minutes_to_hhmm(hours * 60),
        })

    return sorted(results, key=lambda x: x["hours"], reverse=True)


def get_game_consumption_stats(user_media, start_date, end_date, minutes_per_type=None, user=None):
    """Return aggregate metrics and chart data for game activity."""
    if user is None:
        user = _infer_user_from_user_media(user_media)

    game_queryset = (user_media or {}).get(MediaTypes.GAME.value)
    game_data = _collect_game_data(game_queryset, start_date, end_date)

    # Collect play details for genre calculation
    _, play_details = _collect_game_play_data(game_queryset, start_date, end_date)

    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(user_media or {}, start_date, end_date)

    total_minutes = minutes_per_type.get(MediaTypes.GAME.value, 0)
    total_hours = total_minutes / 60 if total_minutes else 0

    # Get activity datetimes for breakdown calculation
    game_datetimes = [
        _localize_datetime(data["activity_datetime"])
        for data in game_data
        if data["activity_datetime"]
    ]

    hours_breakdown = _compute_metric_breakdown(
        total_hours,
        game_datetimes,
        start_date,
        end_date,
    )

    color = config.get_stats_color(MediaTypes.GAME.value)
    chart_label = "Game Hours"

    # Build hours charts
    hours_charts = _build_game_hours_charts(game_data, start_date, end_date, color, chart_label)

    # Build daily average distribution chart (includes top_games_per_band for tooltip)
    daily_avg_chart = _build_daily_average_distribution_chart(game_data, color, "Games")

    # Combine charts
    charts = {
        "by_year": hours_charts["by_year"],
        "by_month": hours_charts["by_month"],
        "by_daily_average": daily_avg_chart,
    }

    # Compute top genres using stored genres, fall back to cached metadata only
    top_genres = _compute_game_top_genres(play_details, limit=20)

    # Compute top daily average games
    top_daily_average_games = _compute_game_top_daily_average(game_data, limit=20)

    # Compute platform breakdown
    platform_breakdown = _compute_game_platform_breakdown(game_data, user)

    # has_data should be True if we have any game data, not just hours
    has_data = len(game_data) > 0 or total_hours > 0

    return {
        "hours": hours_breakdown,
        "charts": charts,
        "has_data": has_data,
        "top_genres": top_genres,
        "top_daily_average_games": top_daily_average_games,
        "platform_breakdown": platform_breakdown,
    }


def _collect_music_play_data(music_queryset, start_date, end_date):
    """Collect music play datetimes and per-play runtime from history records.
    
    Returns:
        tuple: (list of datetimes, list of (music_entry, datetime, runtime_minutes) tuples)
    """
    datetimes = []
    play_details = []  # (music_entry, datetime, runtime_minutes)

    if music_queryset is None:
        return datetimes, play_details

    for music in music_queryset:
        runtime_minutes = _get_music_runtime_minutes(music)

        # Get all history records ordered by history_date (oldest first)
        history_records = list(music.history.all().order_by("history_date"))

        # Group history records by end_date to deduplicate
        # Each unique end_date represents one play, even if there are multiple history records
        plays_by_end_date = {}  # end_date -> (history_record, history_date)

        for history_record in history_records:
            history_end_date = getattr(history_record, "end_date", None)
            history_date = getattr(history_record, "history_date", None)

            # Skip records without end_date (not a completed play)
            if not history_end_date or not history_date:
                continue

            # If we haven't seen this end_date, or this history_record is closer to the end_date,
            # use this one as the canonical record for this play
            if history_end_date not in plays_by_end_date:
                plays_by_end_date[history_end_date] = (history_record, history_date)
            else:
                # Prefer the history record where history_date is closest to end_date
                # (within reason - if history_date is way after end_date, it's likely a metadata update)
                existing_history_date = plays_by_end_date[history_end_date][1]
                time_diff_existing = abs((existing_history_date - history_end_date).total_seconds())
                time_diff_current = abs((history_date - history_end_date).total_seconds())

                # Prefer the one closer to end_date, but only if it's within 24 hours
                # (metadata updates can happen days/weeks later)
                if time_diff_current < time_diff_existing and time_diff_current < 86400:  # 24 hours
                    plays_by_end_date[history_end_date] = (history_record, history_date)

        # Process unique plays within date range
        for play_end_date, (history_record, _) in plays_by_end_date.items():
            # Check if within date range
            if start_date and end_date:
                if not (start_date <= play_end_date <= end_date):
                    continue

            localized_date = _localize_datetime(play_end_date)
            datetimes.append(localized_date)
            play_details.append((music, localized_date, runtime_minutes))

    return datetimes, play_details


def _compute_music_top_lists(play_details, limit=5):
    """Compute top artists, albums, and tracks by total listening time.
    
    Args:
        play_details: List of (music_entry, datetime, runtime_minutes) tuples
        limit: Number of items to return per list
        
    Returns:
        dict with top_artists, top_albums, top_tracks lists
    """
    from app.helpers import minutes_to_hhmm

    # Aggregate by artist, album, and track
    artist_stats = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": "", "image": "", "id": None})
    album_stats = defaultdict(
        lambda: {
            "minutes": 0,
            "plays": 0,
            "title": "",
            "artist": "",
            "artist_id": None,
            "artist_name": "",
            "image": "",
            "id": None,
        },
    )
    track_stats = defaultdict(
        lambda: {
            "minutes": 0,
            "plays": 0,
            "title": "",
            "artist": "",
            "album": "",
            "album_image": "",
            "album_id": None,
            "album_artist_id": None,
            "album_artist_name": "",
            "id": None,
        },
    )

    for music, dt, runtime in play_details:
        # Track stats (use music.id as key since each Music is a unique track entry)
        track_key = music.id
        track_stats[track_key]["minutes"] += runtime
        track_stats[track_key]["plays"] += 1
        track_stats[track_key]["title"] = music.item.title if music.item else "Unknown"
        track_stats[track_key]["id"] = music.id

        # Prefer the explicit music.artist link, but fall back to album.artist so
        # canonical artist/album URLs can still be built from rolled-up stats data.
        album = music.album
        artist = music.artist or getattr(album, "artist", None)

        if artist:
            track_stats[track_key]["artist"] = artist.name
            artist_stats[artist.id]["minutes"] += runtime
            artist_stats[artist.id]["plays"] += 1
            artist_stats[artist.id]["name"] = artist.name
            artist_stats[artist.id]["image"] = artist.image or ""
            artist_stats[artist.id]["id"] = artist.id

        if album:
            track_stats[track_key]["album"] = album.title
            track_stats[track_key]["album_image"] = album.image or track_stats[track_key]["album_image"]
            track_stats[track_key]["album_id"] = album.id
            track_stats[track_key]["album_artist_id"] = artist.id if artist else None
            track_stats[track_key]["album_artist_name"] = artist.name if artist else ""
            album_stats[album.id]["minutes"] += runtime
            album_stats[album.id]["plays"] += 1
            album_stats[album.id]["title"] = album.title
            album_stats[album.id]["artist"] = artist.name if artist else "Unknown"
            album_stats[album.id]["artist_id"] = artist.id if artist else None
            album_stats[album.id]["artist_name"] = artist.name if artist else ""
            album_stats[album.id]["image"] = album.image or ""
            album_stats[album.id]["id"] = album.id

    # Sort by minutes and take top N
    top_artists = sorted(artist_stats.values(), key=lambda x: x["minutes"], reverse=True)[:limit]
    top_albums = sorted(album_stats.values(), key=lambda x: x["minutes"], reverse=True)[:limit]
    top_tracks = sorted(track_stats.values(), key=lambda x: x["minutes"], reverse=True)[:limit]

    album_artist_lookup = {
        album_id: {
            "artist_id": values.get("artist_id"),
            "artist_name": values.get("artist_name"),
        }
        for album_id, values in album_stats.items()
        if values.get("artist_id") is not None or values.get("artist_name")
    }

    for album_item in top_albums:
        artist_data = album_artist_lookup.get(album_item.get("id"))
        if not artist_data:
            continue
        if album_item.get("artist_id") is None:
            album_item["artist_id"] = artist_data.get("artist_id")
        if not album_item.get("artist_name"):
            album_item["artist_name"] = artist_data.get("artist_name", "")

    for track_item in top_tracks:
        artist_data = album_artist_lookup.get(track_item.get("album_id"))
        if not artist_data:
            continue
        if track_item.get("album_artist_id") is None:
            track_item["album_artist_id"] = artist_data.get("artist_id")
        if not track_item.get("album_artist_name"):
            track_item["album_artist_name"] = artist_data.get("artist_name", "")

    # Format durations
    for item in top_artists + top_albums + top_tracks:
        item["formatted_duration"] = minutes_to_hhmm(item["minutes"])

    return {
        "top_artists": top_artists,
        "top_albums": top_albums,
        "top_tracks": top_tracks,
    }


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


# Country name mapping (ISO 3166-1 alpha-2 -> English name)
COUNTRY_NAME_MAP = {
    "AD": "Andorra",
    "AE": "United Arab Emirates",
    "AF": "Afghanistan",
    "AG": "Antigua and Barbuda",
    "AI": "Anguilla",
    "AL": "Albania",
    "AM": "Armenia",
    "AO": "Angola",
    "AQ": "Antarctica",
    "AR": "Argentina",
    "AS": "American Samoa",
    "AT": "Austria",
    "AU": "Australia",
    "AW": "Aruba",
    "AX": "Aland Islands",
    "AZ": "Azerbaijan",
    "BA": "Bosnia and Herzegovina",
    "BB": "Barbados",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BF": "Burkina Faso",
    "BG": "Bulgaria",
    "BH": "Bahrain",
    "BI": "Burundi",
    "BJ": "Benin",
    "BL": "Saint Barthelemy",
    "BM": "Bermuda",
    "BN": "Brunei Darussalam",
    "BO": "Bolivia, Plurinational State of",
    "BQ": "Bonaire, Sint Eustatius and Saba",
    "BR": "Brazil",
    "BS": "Bahamas",
    "BT": "Bhutan",
    "BV": "Bouvet Island",
    "BW": "Botswana",
    "BY": "Belarus",
    "BZ": "Belize",
    "CA": "Canada",
    "CC": "Cocos (Keeling) Islands",
    "CD": "Congo, Democratic Republic of the",
    "CF": "Central African Republic",
    "CG": "Congo",
    "CH": "Switzerland",
    "CI": "Cote d'Ivoire",
    "CK": "Cook Islands",
    "CL": "Chile",
    "CM": "Cameroon",
    "CN": "China",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "CU": "Cuba",
    "CV": "Cabo Verde",
    "CW": "Curacao",
    "CX": "Christmas Island",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DJ": "Djibouti",
    "DK": "Denmark",
    "DM": "Dominica",
    "DO": "Dominican Republic",
    "DZ": "Algeria",
    "EC": "Ecuador",
    "EE": "Estonia",
    "EG": "Egypt",
    "EH": "Western Sahara",
    "ER": "Eritrea",
    "ES": "Spain",
    "ET": "Ethiopia",
    "FI": "Finland",
    "FJ": "Fiji",
    "FK": "Falkland Islands (Malvinas)",
    "FM": "Micronesia, Federated States of",
    "FO": "Faroe Islands",
    "FR": "France",
    "GA": "Gabon",
    "GB": "United Kingdom of Great Britain and Northern Ireland",
    "GD": "Grenada",
    "GE": "Georgia",
    "GF": "French Guiana",
    "GG": "Guernsey",
    "GH": "Ghana",
    "GI": "Gibraltar",
    "GL": "Greenland",
    "GM": "Gambia",
    "GN": "Guinea",
    "GP": "Guadeloupe",
    "GQ": "Equatorial Guinea",
    "GR": "Greece",
    "GS": "South Georgia and the South Sandwich Islands",
    "GT": "Guatemala",
    "GU": "Guam",
    "GW": "Guinea-Bissau",
    "GY": "Guyana",
    "HK": "Hong Kong",
    "HM": "Heard Island and McDonald Islands",
    "HN": "Honduras",
    "HR": "Croatia",
    "HT": "Haiti",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IM": "Isle of Man",
    "IN": "India",
    "IO": "British Indian Ocean Territory",
    "IQ": "Iraq",
    "IR": "Iran, Islamic Republic of",
    "IS": "Iceland",
    "IT": "Italy",
    "JE": "Jersey",
    "JM": "Jamaica",
    "JO": "Jordan",
    "JP": "Japan",
    "KE": "Kenya",
    "KG": "Kyrgyzstan",
    "KH": "Cambodia",
    "KI": "Kiribati",
    "KM": "Comoros",
    "KN": "Saint Kitts and Nevis",
    "KP": "Korea, Democratic People's Republic of",
    "KR": "Korea, Republic of",
    "KW": "Kuwait",
    "KY": "Cayman Islands",
    "KZ": "Kazakhstan",
    "LA": "Lao People's Democratic Republic",
    "LB": "Lebanon",
    "LC": "Saint Lucia",
    "LI": "Liechtenstein",
    "LK": "Sri Lanka",
    "LR": "Liberia",
    "LS": "Lesotho",
    "LT": "Lithuania",
    "LU": "Luxembourg",
    "LV": "Latvia",
    "LY": "Libya",
    "MA": "Morocco",
    "MC": "Monaco",
    "MD": "Moldova, Republic of",
    "ME": "Montenegro",
    "MF": "Saint Martin (French part)",
    "MG": "Madagascar",
    "MH": "Marshall Islands",
    "MK": "North Macedonia",
    "ML": "Mali",
    "MM": "Myanmar",
    "MN": "Mongolia",
    "MO": "Macao",
    "MP": "Northern Mariana Islands",
    "MQ": "Martinique",
    "MR": "Mauritania",
    "MS": "Montserrat",
    "MT": "Malta",
    "MU": "Mauritius",
    "MV": "Maldives",
    "MW": "Malawi",
    "MX": "Mexico",
    "MY": "Malaysia",
    "MZ": "Mozambique",
    "NA": "Namibia",
    "NC": "New Caledonia",
    "NE": "Niger",
    "NF": "Norfolk Island",
    "NG": "Nigeria",
    "NI": "Nicaragua",
    "NL": "Netherlands, Kingdom of the",
    "NO": "Norway",
    "NP": "Nepal",
    "NR": "Nauru",
    "NU": "Niue",
    "NZ": "New Zealand",
    "OM": "Oman",
    "PA": "Panama",
    "PE": "Peru",
    "PF": "French Polynesia",
    "PG": "Papua New Guinea",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PM": "Saint Pierre and Miquelon",
    "PN": "Pitcairn",
    "PR": "Puerto Rico",
    "PS": "Palestine, State of",
    "PT": "Portugal",
    "PW": "Palau",
    "PY": "Paraguay",
    "QA": "Qatar",
    "RE": "Reunion",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russian Federation",
    "RW": "Rwanda",
    "SA": "Saudi Arabia",
    "SB": "Solomon Islands",
    "SC": "Seychelles",
    "SD": "Sudan",
    "SE": "Sweden",
    "SG": "Singapore",
    "SH": "Saint Helena, Ascension and Tristan da Cunha",
    "SI": "Slovenia",
    "SJ": "Svalbard and Jan Mayen",
    "SK": "Slovakia",
    "SL": "Sierra Leone",
    "SM": "San Marino",
    "SN": "Senegal",
    "SO": "Somalia",
    "SR": "Suriname",
    "SS": "South Sudan",
    "ST": "Sao Tome and Principe",
    "SV": "El Salvador",
    "SX": "Sint Maarten (Dutch part)",
    "SY": "Syrian Arab Republic",
    "SZ": "Eswatini",
    "TC": "Turks and Caicos Islands",
    "TD": "Chad",
    "TF": "French Southern Territories",
    "TG": "Togo",
    "TH": "Thailand",
    "TJ": "Tajikistan",
    "TK": "Tokelau",
    "TL": "Timor-Leste",
    "TM": "Turkmenistan",
    "TN": "Tunisia",
    "TO": "Tonga",
    "TR": "Turkiye",
    "TT": "Trinidad and Tobago",
    "TV": "Tuvalu",
    "TW": "Taiwan, Province of China",
    "TZ": "Tanzania, United Republic of",
    "UA": "Ukraine",
    "UG": "Uganda",
    "UM": "United States Minor Outlying Islands",
    "US": "United States of America",
    "UY": "Uruguay",
    "UZ": "Uzbekistan",
    "VA": "Holy See",
    "VC": "Saint Vincent and the Grenadines",
    "VE": "Venezuela, Bolivarian Republic of",
    "VG": "Virgin Islands (British)",
    "VI": "Virgin Islands (U.S.)",
    "VN": "Viet Nam",
    "VU": "Vanuatu",
    "WF": "Wallis and Futuna",
    "WS": "Samoa",
    "YE": "Yemen",
    "YT": "Mayotte",
    "ZA": "South Africa",
    "ZM": "Zambia",
    "ZW": "Zimbabwe",
}


def _country_name_from_code(code: str) -> str:
    """Return the full country name for an ISO alpha-2 code."""
    if not code:
        return "Unknown"
    code = str(code).upper()
    return COUNTRY_NAME_MAP.get(code, code)


def _parse_release_date_str(date_str):
    """Parse a MusicBrainz date string (YYYY, YYYY-MM, YYYY-MM-DD) to date."""
    if not date_str:
        return None
    try:
        if len(date_str) >= 10:
            return datetime.datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        if len(date_str) == 7:
            return datetime.datetime.strptime(date_str, "%Y-%m").date()
        if len(date_str) == 4:
            return datetime.datetime.strptime(date_str, "%Y").date()
    except ValueError:
        return None
    return None


def _hydrate_music_metadata_for_rollups(music_queryset):
    """Ensure artists/albums have genres/country/release_date without manual visits.
    
    Currently we only use locally stored metadata to avoid extra provider calls.
    """
    # No-op placeholder: relies on metadata stored at creation/sync time.


def _compute_music_top_rollups(play_details, limit=5):
    """Compute top genres, decades, and countries from music play details."""
    from app.helpers import minutes_to_hhmm

    genre_stats = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": ""})
    decade_stats = defaultdict(lambda: {"minutes": 0, "plays": 0, "label": ""})
    country_stats = defaultdict(lambda: {"minutes": 0, "plays": 0, "code": ""})

    for music, dt, runtime in play_details:
        minutes = runtime or 0

        # Genres: prefer album genres, fall back to artist genres
        genres = []
        if getattr(music, "album", None) and music.album.genres:
            genres = _coerce_genre_list(music.album.genres)
        elif getattr(music, "artist", None) and music.artist.genres:
            genres = _coerce_genre_list(music.artist.genres)
        elif getattr(music, "track", None) and music.track.genres:
            genres = _coerce_genre_list(music.track.genres)

        for genre in genres:
            key = str(genre).title()
            genre_stats[key]["minutes"] += minutes
            genre_stats[key]["plays"] += 1
            genre_stats[key]["name"] = key

        # Decades: from album release_date if available
        release_date = getattr(music.album, "release_date", None) if getattr(music, "album", None) else None
        if release_date and release_date.year:
            decade_label = f"{(release_date.year // 10) * 10}s"
            decade_stats[decade_label]["minutes"] += minutes
            decade_stats[decade_label]["plays"] += 1
            decade_stats[decade_label]["label"] = decade_label

        # Countries: from artist.country
        country_code = ""
        if getattr(music, "artist", None) and music.artist.country:
            country_code = music.artist.country
        if country_code:
            code_upper = country_code.upper()
            country_stats[code_upper]["minutes"] += minutes
            country_stats[code_upper]["plays"] += 1
            country_stats[code_upper]["code"] = code_upper
            country_stats[code_upper]["name"] = _country_name_from_code(code_upper)

    def _format_top(stat_map, label_key):
        items = sorted(
            stat_map.values(),
            key=lambda x: (x["minutes"], x["plays"]),
            reverse=True,
        )[:limit]
        for item in items:
            item["formatted_duration"] = minutes_to_hhmm(item["minutes"])
        return items

    return {
        "top_genres": _format_top(genre_stats, "name"),
        "top_decades": _format_top(decade_stats, "label"),
        "top_countries": _format_top(country_stats, "code"),
    }


def _compute_movie_tv_top_genres(play_details, limit=20):
    """Compute top genres from movie/TV play details.
    
    Args:
        play_details: List of (media_entry, datetime, runtime_minutes) tuples
        limit: Number of genres to return
        
    Returns:
        list of genre dicts with name, minutes, plays, formatted_duration
    """
    from app.helpers import minutes_to_hhmm
    from app.models import Episode

    genre_stats = defaultdict(lambda: {"minutes": 0, "plays": 0, "name": ""})

    for media, dt, runtime in play_details:
        minutes = runtime or 0

        # Get genres from media.item.details or metadata
        genres = []

        # For TV episodes, get genres from the parent TV show
        # For movies, get genres directly from the movie
        media_to_use = media
        if isinstance(media, Episode):
            # Episode -> Season -> TV show
            if hasattr(media, "related_season") and media.related_season:
                if hasattr(media.related_season, "related_tv") and media.related_season.related_tv:
                    media_to_use = media.related_season.related_tv
                else:
                    # Skip if we can't get the TV show
                    continue
            else:
                # Skip if we can't get the season
                continue

        if hasattr(media_to_use, "item") and media_to_use.item:
            # Try to get genres from item details
            try:
                metadata = _get_media_metadata_for_statistics(media_to_use)
                if metadata:
                    details = metadata.get("details") if isinstance(metadata, dict) else None
                    if isinstance(details, dict):
                        genres_raw = details.get("genres", [])
                        if genres_raw:
                            genres = _coerce_genre_list(genres_raw)
                    # Also check top-level genres
                    if not genres:
                        genres_raw = metadata.get("genres", [])
                        if genres_raw:
                            genres = _coerce_genre_list(genres_raw)
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                # Skip this media if metadata retrieval fails
                import logging
                logger = logging.getLogger(__name__)
                logger.debug(f"Skipping genre calculation for {getattr(media_to_use.item, 'title', 'unknown')}: {e}")
                continue

        for genre in genres:
            key = str(genre).title()
            genre_stats[key]["minutes"] += minutes
            genre_stats[key]["plays"] += 1
            genre_stats[key]["name"] = key

    # Sort by minutes (descending), then by plays (descending)
    items = sorted(
        genre_stats.values(),
        key=lambda x: (x["minutes"], x["plays"]),
        reverse=True,
    )[:limit]

    # Format durations
    for item in items:
        item["formatted_duration"] = minutes_to_hhmm(item["minutes"])

    return items


def get_music_consumption_stats(user_media, start_date, end_date, minutes_per_type=None):
    """Return aggregate metrics and chart data for music activity.
    
    This is similar to TV/Movie consumption stats but uses minutes instead of hours
    and includes top artists, albums, and tracks.
    """
    music_queryset = (user_media or {}).get(MediaTypes.MUSIC.value)

    # Prefetch related data for efficiency
    # Note: history manager from simple_history cannot be prefetched, so we access it directly in the loop
    # Clear any existing prefetches that might include 'history' (which can't be prefetched)
    if music_queryset is not None:
        # Get the model and recreate queryset to avoid any problematic prefetches
        model = music_queryset.model
        # Get the IDs from the original queryset
        music_ids = list(music_queryset.values_list("id", flat=True))
        if music_ids:
            # Recreate queryset with only safe prefetches
            music_queryset = model.objects.filter(id__in=music_ids).select_related("item", "artist", "album")
        else:
            music_queryset = None

    music_datetimes, play_details = _collect_music_play_data(music_queryset, start_date, end_date)

    # Hydrate missing metadata (genres, country, release_date) from stored data only (no provider calls)
    if music_queryset is not None:
        _hydrate_music_metadata_for_rollups(music_queryset)

    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(user_media or {}, start_date, end_date)

    total_minutes = minutes_per_type.get(MediaTypes.MUSIC.value, 0)
    total_plays = len(music_datetimes)

    # For music, we use minutes breakdown instead of hours
    minutes_breakdown = _compute_metric_breakdown(
        total_minutes,
        music_datetimes,
        start_date,
        end_date,
    )
    plays_breakdown = _compute_metric_breakdown(
        total_plays,
        music_datetimes,
        start_date,
        end_date,
    )

    color = config.get_stats_color(MediaTypes.MUSIC.value)
    chart_label = "Music Plays"
    charts = _build_media_charts(music_datetimes, color, chart_label)

    # Compute top lists
    top_lists = _compute_music_top_lists(play_details, limit=20)
    meta_lists = _compute_music_top_rollups(play_details, limit=20)

    return {
        "minutes": minutes_breakdown,
        "plays": plays_breakdown,
        "charts": charts,
        "has_data": total_plays > 0,
        "top_artists": top_lists["top_artists"],
        "top_albums": top_lists["top_albums"],
        "top_tracks": top_lists["top_tracks"],
        "top_genres": meta_lists["top_genres"],
        "top_decades": meta_lists["top_decades"],
        "top_countries": meta_lists["top_countries"],
    }


def _get_podcast_runtime_minutes(podcast_entry, history_record=None):
    """Get runtime in minutes from a Podcast entry, checking episode and item."""
    # First try the linked PodcastEpisode's duration (in seconds)
    if podcast_entry.episode and podcast_entry.episode.duration:
        return podcast_entry.episode.duration // 60  # seconds to minutes

    # Fall back to item runtime_minutes
    if podcast_entry.item and podcast_entry.item.runtime_minutes:
        return podcast_entry.item.runtime_minutes

    # Fall back to progress (already in minutes, but represents listened time, not total)
    # This is less ideal but better than nothing
    if history_record and history_record.progress and history_record.progress > 0:
        return history_record.progress
    if podcast_entry.progress and podcast_entry.progress > 0:
        return podcast_entry.progress

    return 0


def _get_podcast_history_data(user, start_date, end_date):
    """Return podcast history records and a lookup for metadata."""
    if not user:
        return [], {}

    from app.models import Podcast
    HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")

    podcast_history_records = HistoricalPodcast.objects.filter(
        Q(history_user=user) | Q(history_user__isnull=True),
        end_date__isnull=False,
    )

    if start_date:
        podcast_history_records = podcast_history_records.filter(end_date__gte=start_date)
    if end_date:
        podcast_history_records = podcast_history_records.filter(end_date__lte=end_date)

    podcast_history_records = list(podcast_history_records.order_by("history_date"))
    podcast_ids = {record.id for record in podcast_history_records if record.id}
    if not podcast_ids:
        return podcast_history_records, {}

    podcasts_lookup = {
        podcast.id: podcast
        for podcast in Podcast.objects.filter(
            id__in=podcast_ids,
            user=user,
        ).select_related("item", "show", "episode", "episode__show")
    }

    return podcast_history_records, podcasts_lookup


def _collect_podcast_play_data(podcast_history_records, podcasts_lookup, start_date, end_date):
    """Collect podcast play datetimes and per-play runtime from history records.

    We deduplicate by end_date per podcast entry to avoid counting metadata updates.

    Returns:
        tuple: (list of datetimes, list of (podcast_entry, datetime, runtime_minutes) tuples)
    """
    datetimes = []
    play_details = []  # (podcast_entry, datetime, runtime_minutes)

    if not podcast_history_records:
        logger.debug("Podcast history records empty in _collect_podcast_play_data")
        return datetimes, play_details

    logger.debug(
        "Found %d podcast history records for date range %s to %s",
        len(podcast_history_records),
        start_date,
        end_date,
    )

    # Group history records by podcast id and end_date to deduplicate plays
    plays_by_podcast = defaultdict(dict)  # podcast_id -> end_date -> (history_record, history_date)

    for history_record in podcast_history_records:
        podcast_id = getattr(history_record, "id", None)
        history_end_date = getattr(history_record, "end_date", None)
        history_date = getattr(history_record, "history_date", None)

        if not podcast_id or not history_end_date or not history_date:
            continue

        if start_date and end_date and not (start_date <= history_end_date <= end_date):
            continue

        plays_for_podcast = plays_by_podcast[podcast_id]
        if history_end_date not in plays_for_podcast:
            plays_for_podcast[history_end_date] = (history_record, history_date)
        else:
            existing_history_date = plays_for_podcast[history_end_date][1]
            time_diff_existing = abs((existing_history_date - history_end_date).total_seconds())
            time_diff_current = abs((history_date - history_end_date).total_seconds())

            if time_diff_current < time_diff_existing and time_diff_current < 86400:
                plays_for_podcast[history_end_date] = (history_record, history_date)

    for podcast_id, plays_for_podcast in plays_by_podcast.items():
        podcast = podcasts_lookup.get(podcast_id)
        if not podcast:
            continue

        for play_end_date, (history_record, _) in plays_for_podcast.items():
            runtime_minutes = _get_podcast_runtime_minutes(podcast, history_record)
            if runtime_minutes <= 0:
                continue

            localized_date = _localize_datetime(play_end_date)
            datetimes.append(localized_date)
            play_details.append((podcast, localized_date, runtime_minutes))

    logger.debug("Collected %d podcast plays", len(datetimes))
    return datetimes, play_details


def _compute_podcast_top_lists(play_details, limit=20):
    """Compute top shows by plays, listening time, and longest episodes.
    
    Args:
        play_details: List of (podcast_entry, datetime, runtime_minutes) tuples
        limit: Number of items to return per list
        
    Returns:
        dict with most_played (by show), most_listened (by show), longest_episodes lists
    """
    from app.helpers import minutes_to_hhmm

    # Aggregate by show for most_played and most_listened
    show_stats = defaultdict(lambda: {
        "minutes": 0,
        "plays": 0,
        "title": "",
        "show": "",
        "show_id": None,
        "podcast_uuid": None,
        "slug": "",
        "image": "",
    })

    # Aggregate by episode for longest_episodes
    episode_stats = defaultdict(lambda: {
        "title": "",
        "show": "",
        "show_id": None,
        "podcast_uuid": None,
        "slug": "",
        "episode_id": None,
        "image": "",
        "duration_seconds": 0,
    })

    for podcast, dt, runtime in play_details:
        # Aggregate by show for most_played and most_listened
        if podcast.show:
            show_key = podcast.show.id
            show_stats[show_key]["show_id"] = show_key
            show_stats[show_key]["show"] = podcast.show.title
            show_stats[show_key]["title"] = podcast.show.title  # Use show title as display title
            # Always set podcast_uuid if available (it should be the same for all podcasts of the same show)
            if podcast.show.podcast_uuid:
                show_stats[show_key]["podcast_uuid"] = podcast.show.podcast_uuid
            show_stats[show_key]["slug"] = podcast.show.slug or ""
            show_stats[show_key]["image"] = podcast.show.image or ""
        else:
            # Fallback if no show
            show_key = podcast.id
            show_stats[show_key]["show_id"] = None
            show_stats[show_key]["show"] = "Unknown Show"
            show_stats[show_key]["title"] = podcast.item.title if podcast.item else "Unknown Show"
            show_stats[show_key]["image"] = podcast.item.image if podcast.item else ""

        # Aggregate show stats
        show_stats[show_key]["minutes"] += runtime
        show_stats[show_key]["plays"] += 1

        # Aggregate by episode for longest_episodes
        if podcast.episode:
            episode_key = podcast.episode.id
            episode_stats[episode_key]["episode_id"] = episode_key
            episode_stats[episode_key]["title"] = podcast.episode.title
            episode_stats[episode_key]["duration_seconds"] = podcast.episode.duration or 0
        else:
            # Fallback to podcast.id if no episode link
            episode_key = podcast.id
            episode_stats[episode_key]["episode_id"] = episode_key
            episode_stats[episode_key]["title"] = podcast.item.title if podcast.item else "Unknown Episode"
            # Try to get duration from item
            if podcast.item and podcast.item.runtime_minutes:
                episode_stats[episode_key]["duration_seconds"] = podcast.item.runtime_minutes * 60

        # Get show info for episode stats
        if podcast.show:
            episode_stats[episode_key]["show"] = podcast.show.title
            episode_stats[episode_key]["show_id"] = podcast.show.id
            # Always set podcast_uuid if available (it should be the same for all podcasts of the same show)
            if podcast.show.podcast_uuid:
                episode_stats[episode_key]["podcast_uuid"] = podcast.show.podcast_uuid
            episode_stats[episode_key]["slug"] = podcast.show.slug or ""
            episode_stats[episode_key]["image"] = podcast.show.image or ""
        elif podcast.item:
            episode_stats[episode_key]["image"] = podcast.item.image or ""

    # Ensure podcast_uuid is populated for all shows (look up from show_id if missing)
    from app.models import PodcastShow
    for show_stat in show_stats.values():
        if show_stat["show_id"] and not show_stat["podcast_uuid"]:
            try:
                show = PodcastShow.objects.get(id=show_stat["show_id"])
                if show.podcast_uuid:
                    show_stat["podcast_uuid"] = show.podcast_uuid
                    show_stat["slug"] = show.slug or show_stat["slug"]
            except PodcastShow.DoesNotExist:
                pass

    # Most played shows (by number of plays)
    most_played = sorted(
        show_stats.values(),
        key=lambda x: (x["plays"], x["minutes"]),
        reverse=True,
    )[:limit]

    # Most listened shows (by total minutes)
    most_listened = sorted(
        show_stats.values(),
        key=lambda x: (x["minutes"], x["plays"]),
        reverse=True,
    )[:limit]

    # Ensure podcast_uuid is populated for all episodes (look up from show_id if missing)
    for episode_stat in episode_stats.values():
        if episode_stat["show_id"] and not episode_stat["podcast_uuid"]:
            try:
                show = PodcastShow.objects.get(id=episode_stat["show_id"])
                if show.podcast_uuid:
                    episode_stat["podcast_uuid"] = show.podcast_uuid
                    episode_stat["slug"] = show.slug or episode_stat["slug"]
            except PodcastShow.DoesNotExist:
                pass

    # Longest episodes (by duration_seconds, only episodes with duration)
    longest_episodes = sorted(
        [ep for ep in episode_stats.values() if ep["duration_seconds"] > 0],
        key=lambda x: x["duration_seconds"],
        reverse=True,
    )[:limit]

    # Format durations
    for item in most_played + most_listened:
        item["formatted_duration"] = minutes_to_hhmm(item["minutes"])

    # Format longest episodes duration (from seconds)
    for item in longest_episodes:
        hours = item["duration_seconds"] // 3600
        minutes = (item["duration_seconds"] % 3600) // 60
        if hours > 0:
            item["formatted_duration"] = f"{hours}h {minutes}m"
        else:
            item["formatted_duration"] = f"{minutes}m"

    return {
        "most_played": most_played,
        "most_listened": most_listened,
        "longest_episodes": longest_episodes,
    }


def get_podcast_consumption_stats(user_media, start_date, end_date, minutes_per_type=None, user=None):
    """Return aggregate metrics and chart data for podcast activity.

    This is similar to music consumption stats but for podcasts.

    Args:
        user_media: Dictionary of media querysets by type
        start_date: Start date for filtering
        end_date: End date for filtering
        minutes_per_type: Pre-calculated minutes per media type (optional)
        user: User object (required to query all podcasts)
    """
    if user is None:
        user = _infer_user_from_user_media(user_media)

    if not user:
        logger.warning("get_podcast_consumption_stats: No user available, returning empty stats")
        return {
            "minutes": _compute_metric_breakdown(0, [], start_date, end_date),
            "plays": _compute_metric_breakdown(0, [], start_date, end_date),
            "charts": _build_media_charts([], config.get_stats_color(MediaTypes.PODCAST.value), "Podcast Plays"),
            "has_data": False,
            "most_played": [],
            "most_listened": [],
            "longest_episodes": [],
        }

    podcast_history_records, podcasts_lookup = _get_podcast_history_data(
        user,
        start_date,
        end_date,
    )
    podcast_datetimes, play_details = _collect_podcast_play_data(
        podcast_history_records,
        podcasts_lookup,
        start_date,
        end_date,
    )
    logger.debug(
        "get_podcast_consumption_stats: Collected %d datetimes, %d play_details",
        len(podcast_datetimes),
        len(play_details),
    )
    
    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(user_media or {}, start_date, end_date)

    total_minutes = minutes_per_type.get(MediaTypes.PODCAST.value, 0)
    total_plays = len(podcast_datetimes)

    # For podcasts, we use minutes breakdown (same as music)
    minutes_breakdown = _compute_metric_breakdown(
        total_minutes,
        podcast_datetimes,
        start_date,
        end_date,
    )
    plays_breakdown = _compute_metric_breakdown(
        total_plays,
        podcast_datetimes,
        start_date,
        end_date,
    )

    color = config.get_stats_color(MediaTypes.PODCAST.value)
    chart_label = "Podcast Plays"
    charts = _build_media_charts(podcast_datetimes, color, chart_label)

    # Compute top lists
    top_lists = _compute_podcast_top_lists(play_details, limit=20)

    return {
        "minutes": minutes_breakdown,
        "plays": plays_breakdown,
        "charts": charts,
        "has_data": total_plays > 0,
        "most_played": top_lists["most_played"],
        "most_listened": top_lists["most_listened"],
        "longest_episodes": top_lists["longest_episodes"],
    }


def get_daily_hours_by_media_type(user_media, start_date, end_date):
    """Build Chart.js-friendly stacked bar data where X axis is dates (inclusive)
    between start_date and end_date and Y axis is hours per media type per day.

    Currently implemented for movies; other media types included as zeros and can
    be expanded later.
    """
    # If no date range is provided (All Time), infer a sensible range from
    # available media activity dates so the chart can show a meaningful span.
    if not start_date or not end_date:
        # Gather all candidate activity datetimes from the provided media
        candidate_dates = []
        for media_list in user_media.values():
            for media in media_list:
                activity_dt = _get_activity_datetime(media)
                if activity_dt:
                    candidate_dates.append(_localize_datetime(activity_dt))

        if not candidate_dates:
            # No activity dates available -> nothing to chart
            return {"labels": [], "datasets": []}

        # Derive start/end from min/max activity datetimes
        min_dt = min(candidate_dates)
        max_dt = max(candidate_dates)
        # Convert to naive date boundaries for the rest of the function
        start_date = datetime.datetime.combine(min_dt.date(), datetime.time.min)
        end_date = datetime.datetime.combine(max_dt.date(), datetime.time.max)
        # Ensure they are timezone-aware in the current timezone
        try:
            start_date = timezone.make_aware(start_date)
            end_date = timezone.make_aware(end_date)
        except Exception:
            # If awareness fails, fall back to original naive datetimes
            pass

    # Normalize to dates (without time)
    start_date_dt = start_date.date()
    end_date_dt = end_date.date()
    if start_date_dt > end_date_dt:
        start_date_dt, end_date_dt = end_date_dt, start_date_dt

    # Build list of date labels in ISO format (YYYY-MM-DD)
    num_days = (end_date_dt - start_date_dt).days + 1
    labels = [(start_date_dt + datetime.timedelta(days=i)).isoformat() for i in range(num_days)]

    # Prepare per-media-type mapping of date -> minutes
    per_type_minutes = {mt: dict.fromkeys(labels, 0) for mt in user_media.keys()}

    # We'll need the runtime lookup function and logger
    for media_type, media_list in user_media.items():
        # Movies
        if media_type == MediaTypes.MOVIE.value:
            for media in media_list:
                activity_dt = _get_activity_datetime(media)
                if activity_dt is None:
                    continue
                activity_date = _localize_datetime(activity_dt).date()
                if activity_date < start_date_dt or activity_date > end_date_dt:
                    continue

                # Get runtime in minutes from cache (will attempt metadata fetch if missing)
                minutes = _get_media_runtime_from_cache(media, logger, "(daily aggregation)")
                if not minutes or minutes <= 0:
                    continue

                label = activity_date.isoformat()
                if label in per_type_minutes[media_type]:
                    per_type_minutes[media_type][label] += minutes

        # TV shows / Seasons: use per-episode end_date and runtime from episode cache
        elif media_type == MediaTypes.TV.value or media_type == MediaTypes.SEASON.value:
            for tv in media_list:
                seasons = getattr(tv, "seasons", None)
                if seasons is None:
                    continue
                for season in seasons.all():
                    episodes = getattr(season, "episodes", None)
                    if episodes is None:
                        continue
                    for episode in episodes.all():
                        if not episode.end_date:
                            continue
                        ep_date = _localize_datetime(episode.end_date).date()
                        if ep_date < start_date_dt or ep_date > end_date_dt:
                            continue
                        # runtime from cached episode data
                        try:
                            minutes = _calculate_episode_time_from_cache(episode, logger)
                        except Exception:
                            minutes = 0
                        if minutes and minutes > 0:
                            label = ep_date.isoformat()
                            if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                                per_type_minutes[media_type][label] += minutes

        # Anime: prefer per-media runtime * progress; if a start/end range exists on the media, distribute evenly, otherwise assign to activity date
        elif media_type == MediaTypes.ANIME.value:
            for media in media_list:
                # total minutes from cached runtime per episode * progress
                episode_count = getattr(media, "progress", 0) or 0
                if episode_count <= 0:
                    continue
                minutes = _get_anime_runtime_from_cache(media, episode_count, logger, "(daily aggregation)")
                if not minutes or minutes <= 0:
                    continue

                # Determine distribution date range for this media
                media_start = getattr(media, "start_date", None)
                media_end = getattr(media, "end_date", None)
                if media_start and media_end:
                    # distribute evenly across overlap with requested range
                    ds = max(media_start.date(), start_date_dt)
                    de = min(media_end.date(), end_date_dt)
                    if ds > de:
                        continue
                    days = (de - ds).days + 1
                    per_day = minutes / days
                    for i in range(days):
                        d = (ds + datetime.timedelta(days=i)).isoformat()
                        if media_type in per_type_minutes and d in per_type_minutes[media_type]:
                            per_type_minutes[media_type][d] += per_day
                else:
                    activity_dt = _get_activity_datetime(media)
                    if not activity_dt:
                        continue
                    label = _localize_datetime(activity_dt).date().isoformat()
                    if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                        per_type_minutes[media_type][label] += minutes

        # Music: assign runtime to each play date from history records
        elif media_type == MediaTypes.MUSIC.value:
            for media in media_list:
                runtime_minutes = _get_music_runtime_minutes(media)
                if runtime_minutes <= 0:
                    continue

                # Each history record represents a play
                for history_record in media.history.all():
                    history_end_date = getattr(history_record, "end_date", None)
                    if not history_end_date:
                        continue

                    play_date = _localize_datetime(history_end_date).date()
                    if play_date < start_date_dt or play_date > end_date_dt:
                        continue

                    label = play_date.isoformat()
                    if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                        per_type_minutes[media_type][label] += runtime_minutes

        # Podcasts: use history records so deleted plays don't appear
        elif media_type == MediaTypes.PODCAST.value:
            podcast_user = _infer_user_from_user_media(user_media)
            podcast_history_records, podcasts_lookup = _get_podcast_history_data(
                podcast_user,
                start_date,
                end_date,
            )
            _, play_details = _collect_podcast_play_data(
                podcast_history_records,
                podcasts_lookup,
                start_date,
                end_date,
            )

            for _, play_dt, runtime_minutes in play_details:
                if not play_dt or runtime_minutes <= 0:
                    continue

                completion_date = play_dt.date()
                if completion_date < start_date_dt or completion_date > end_date_dt:
                    continue

                label = completion_date.isoformat()
                if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                    per_type_minutes[media_type][label] += runtime_minutes

        # Manga, Games, Books, Comics: use progress field and distribute evenly across item's date span
        elif media_type in (
            MediaTypes.MANGA.value,
            MediaTypes.GAME.value,
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.BOARDGAME.value,
        ):
            for media in media_list:
                total_progress = getattr(media, "progress", 0) or 0
                if not total_progress or total_progress <= 0:
                    continue

                # For games, progress is stored in minutes; for others we follow user instruction and treat 'progress' as an amount to distribute
                total_minutes = total_progress

                media_start = getattr(media, "start_date", None)
                media_end = getattr(media, "end_date", None)
                if media_start and media_end:
                    ds = max(media_start.date(), start_date_dt)
                    de = min(media_end.date(), end_date_dt)
                    if ds > de:
                        continue
                    days = (de - ds).days + 1
                    per_day = total_minutes / days
                    for i in range(days):
                        d = (ds + datetime.timedelta(days=i)).isoformat()
                        if media_type in per_type_minutes and d in per_type_minutes[media_type]:
                            per_type_minutes[media_type][d] += per_day
                else:
                    activity_dt = _get_activity_datetime(media)
                    if not activity_dt:
                        continue
                    label = _localize_datetime(activity_dt).date().isoformat()
                    if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                        per_type_minutes[media_type][label] += total_minutes

    # Build datasets for Chart.js: convert minutes -> hours (float)
    datasets = []
    ordered_types = list(MEDIA_TYPE_HOURS_ORDER)
    ordered_types.extend(
        [media_type for media_type in per_type_minutes.keys() if media_type not in ordered_types]
    )
    for media_type in ordered_types:
        date_map = per_type_minutes.get(media_type)
        if not date_map:
            continue
        # Skip media types that have zero total minutes
        total = sum(date_map.values())
        if total == 0:
            continue

        datasets.append({
            "label": app_tags.media_type_readable(media_type),
            "data": [round(date_map[d] / 60, 2) for d in labels],
            "background_color": config.get_stats_color(media_type),
        })

    return {"labels": labels, "datasets": datasets}


def get_top_played_media(user_media, start_date, end_date):
    """Get top played media by total time spent within date range.
    
    Returns a dictionary with media types as keys and lists of top media items.
    Each media item includes total_time_minutes, formatted_duration, and episode_count.
    """
    import logging

    from app.helpers import minutes_to_hhmm

    logger = logging.getLogger(__name__)
    top_played = {}

    # Define the media types we want to show
    target_media_types = ["movie", "tv", "game", "boardgame", "anime", "music"]

    for media_type, media_list in user_media.items():
        # Normalize media type to match our target types
        normalized_type = media_type.lower()
        if normalized_type not in target_media_types:
            continue

        if not media_list.exists():
            continue

        # Get media items with their progress and metadata
        media_with_progress = []

        if normalized_type == "movie":
            aggregated_movies = {}

            for media in media_list:
                total_time_minutes = _calculate_movie_time(media, start_date, end_date, normalized_type, logger)
                if total_time_minutes <= 0:
                    continue

                item = getattr(media, "item", None)
                if not item:
                    continue

                # Use item id when available, fallback to (media_id, source) tuple
                item_key = getattr(item, "id", None)
                if item_key is None:
                    item_key = (getattr(item, "media_id", None), getattr(item, "source", None))

                activity = media.end_date or media.start_date or media.created_at
                if item_key not in aggregated_movies:
                    aggregated_movies[item_key] = {
                        "media": media,
                        "total_time_minutes": total_time_minutes,
                        "formatted_duration": None,  # populated after aggregation
                        "episode_count": 0,
                        "last_activity": activity,
                        "play_count": 1,
                        "_media_activity": activity,
                    }
                else:
                    entry = aggregated_movies[item_key]
                    entry["total_time_minutes"] += total_time_minutes
                    entry["play_count"] += 1

                    if activity and (entry["last_activity"] is None or activity > entry["last_activity"]):
                        entry["last_activity"] = activity

                    current_media_activity = entry.get("_media_activity")
                    if activity and (current_media_activity is None or activity > current_media_activity):
                        entry["media"] = media
                        entry["_media_activity"] = activity

            for entry in aggregated_movies.values():
                entry["formatted_duration"] = minutes_to_hhmm(entry["total_time_minutes"])
                entry.pop("_media_activity", None)
                media_with_progress.append(entry)
        elif normalized_type == "game":
            aggregated_games = {}

            for media in media_list:
                total_time_minutes = _calculate_game_time_in_range(media, start_date, end_date)
                if total_time_minutes <= 0:
                    continue

                item = getattr(media, "item", None)
                if not item:
                    continue

                # Use item id when available, fallback to (media_id, source) tuple
                item_key = getattr(item, "id", None)
                if item_key is None:
                    item_key = (getattr(item, "media_id", None), getattr(item, "source", None))

                activity = media.end_date or media.start_date or media.created_at
                if item_key not in aggregated_games:
                    aggregated_games[item_key] = {
                        'media': media,
                        'total_time_minutes': total_time_minutes,
                        'formatted_duration': None,  # populated after aggregation
                        'episode_count': 0,
                        'last_activity': activity,
                        'play_count': 1,
                        '_media_activity': activity,
                    }
                else:
                    entry = aggregated_games[item_key]
                    entry['total_time_minutes'] += total_time_minutes
                    entry['play_count'] += 1

                    if activity and (entry['last_activity'] is None or activity > entry['last_activity']):
                        entry['last_activity'] = activity

                    current_media_activity = entry.get('_media_activity')
                    if activity and (current_media_activity is None or activity > current_media_activity):
                        entry['media'] = media
                        entry['_media_activity'] = activity

            for entry in aggregated_games.values():
                entry['formatted_duration'] = minutes_to_hhmm(entry['total_time_minutes'])
                entry.pop('_media_activity', None)
                media_with_progress.append(entry)
        else:
            for media in media_list:
                total_time_minutes = 0
                episode_count = 0

                if normalized_type == "tv":
                    total_time_minutes, episode_count = _calculate_tv_time(media, start_date, end_date, logger)
                elif normalized_type == "anime":
                    total_time_minutes, episode_count = _calculate_anime_time(media, start_date, end_date, logger)
                elif normalized_type == "boardgame":
                    if (
                        media.end_date
                        and start_date
                        and end_date
                        and start_date <= media.end_date <= end_date
                    ) or (
                        media.start_date
                        and start_date
                        and end_date
                        and start_date <= media.start_date <= end_date
                    ) or (not start_date and not end_date):
                        total_time_minutes += media.progress
                elif normalized_type == "music":
                    # Music: sum runtime for each play (history record) within date range
                    total_time_minutes = _calculate_music_time(media, start_date, end_date, logger)
                    # Count plays for display - deduplicate by end_date (each unique end_date = one play)
                    play_count = 0
                    history_records = list(media.history.all().order_by("history_date"))

                    # Group by end_date to deduplicate
                    unique_end_dates = set()
                    for history_record in history_records:
                        history_end_date = getattr(history_record, "end_date", None)
                        if not history_end_date:
                            continue
                        unique_end_dates.add(history_end_date)

                    # Count unique plays within date range
                    for play_end_date in unique_end_dates:
                        if start_date and end_date:
                            if start_date <= play_end_date <= end_date:
                                play_count += 1
                        else:
                            play_count += 1

                    episode_count = play_count  # Reuse episode_count for plays
                else:
                    # For movies and other media types, get runtime from metadata
                    total_time_minutes = _calculate_movie_time(media, start_date, end_date, normalized_type, logger)

                if total_time_minutes > 0:
                    formatted_duration = minutes_to_hhmm(total_time_minutes)
                    if normalized_type == "boardgame":
                        formatted_duration = f"{total_time_minutes} play{'s' if total_time_minutes != 1 else ''}"

                    media_with_progress.append({
                        "media": media,
                        "total_time_minutes": total_time_minutes,
                        "formatted_duration": formatted_duration,
                        "episode_count": episode_count,
                        "last_activity": media.end_date or media.start_date or media.created_at,
                        "play_count": 1,
                    })

        # Sort by total time, then by most recent activity
        media_with_progress.sort(
            key=lambda x: (x["total_time_minutes"], x["last_activity"]),
            reverse=True,
        )

        # Take top 20 for games, top 10 for other media types
        limit = 20 if normalized_type == "game" else 10
        top_played[normalized_type] = media_with_progress[:limit]

    return top_played
