import calendar
import datetime
import heapq
import itertools
import logging
from collections import Counter, defaultdict

from dateutil.relativedelta import relativedelta
from django.apps import apps
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
    Episode,
    MediaManager,
    MediaTypes,
    Season,
    Status,
    Track,
)
from app.statistics_cache import STATISTICS_TOP_N, STATISTICS_TOP_RATED_OVERALL
from app.templatetags import app_tags
from users.models import WeekStartDayChoices

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
        # media_list may be a list of querysets (combined anime bucket)
        first_media = None
        if isinstance(media_list, _CombinedMediaBucket):
            first_media = media_list.first()
        else:
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

    _tv_ids = None  # saved for grouped-anime pass after the main loop

    for model in media_models:
        media_type = model.__name__.lower()
        queryset = None

        if model == TV:
            _tv_ids = base_episodes.values_list(
                "related_season__related_tv",
                flat=True,
            ).distinct()
            # Exclude grouped anime (library_media_type="anime") — they belong in the anime bucket
            queryset = TV.objects.filter(
                id__in=_tv_ids,
                status__in=[Status.IN_PROGRESS.value, Status.COMPLETED.value, Status.DROPPED.value, Status.PAUSED.value],
            ).exclude(
                item__library_media_type=MediaTypes.ANIME.value,
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
                                related_season__related_tv__in=_tv_ids,
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

    # Pull grouped anime (TV-structured, library_media_type="anime") into the anime bucket
    if _tv_ids is not None and base_episodes is not None:
        _grouped_anime_qs = (
            TV.objects.filter(
                id__in=_tv_ids,
                item__library_media_type=MediaTypes.ANIME.value,
                status__in=[
                    Status.IN_PROGRESS.value,
                    Status.COMPLETED.value,
                    Status.DROPPED.value,
                    Status.PAUSED.value,
                ],
            )
            .select_related("item")
            .prefetch_related(
                Prefetch(
                    "seasons",
                    queryset=Season.objects.filter(
                        status__in=[
                            Status.IN_PROGRESS.value,
                            Status.COMPLETED.value,
                            Status.DROPPED.value,
                            Status.PAUSED.value,
                        ],
                    )
                    .select_related("item")
                    .prefetch_related(
                        Prefetch(
                            "episodes",
                            queryset=base_episodes.filter(
                                related_season__related_tv__in=_tv_ids,
                            ),
                        ),
                    ),
                ),
            )
        )
        _grouped_anime_count = _grouped_anime_qs.count()
        if _grouped_anime_count > 0:
            anime_key = MediaTypes.ANIME.value
            if anime_key in user_media:
                # Combine with flat anime (MAL) queryset already in the bucket
                user_media[anime_key] = _CombinedMediaBucket(user_media[anime_key], _grouped_anime_qs)
                media_count[anime_key] = media_count.get(anime_key, 0) + _grouped_anime_count
            else:
                user_media[anime_key] = _grouped_anime_qs
                media_count[anime_key] = _grouped_anime_count
            media_count["total"] += _grouped_anime_count

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


class _CombinedMediaBucket:
    """Wraps two querysets (e.g. flat anime + grouped anime TV) into one iterable bucket."""

    def __init__(self, *querysets):
        self._querysets = querysets

    def __iter__(self):
        for qs in self._querysets:
            yield from qs

    def exists(self):
        return any(qs.exists() for qs in self._querysets)

    def first(self):
        for qs in self._querysets:
            obj = qs.first()
            if obj is not None:
                return obj
        return None

    def select_related(self, *args):
        return _CombinedMediaBucket(*(qs.select_related(*args) for qs in self._querysets))

    def count(self):
        return sum(qs.count() for qs in self._querysets)

    def values(self, *fields):
        # Returns a lazy iterator of dicts; used only to drive status/score counts
        return _CombinedValuesResult(self._querysets, fields)


class _CombinedValuesResult:
    """Wraps .values(...).annotate(count=Count('id')) results from multiple querysets."""

    def __init__(self, querysets, fields):
        self._querysets = querysets
        self._fields = fields
        self._annotation = None
        self._annotation_field = None

    def annotate(self, **kwargs):
        self._annotation = kwargs
        if kwargs:
            self._annotation_field = next(iter(kwargs))
        return self

    def __iter__(self):
        if not self._annotation:
            for qs in self._querysets:
                yield from qs.values(*self._fields)
            return
        # Merge counts from each queryset
        merged: dict = {}
        for qs in self._querysets:
            for row in qs.values(*self._fields).annotate(**self._annotation):
                key = tuple(row[f] for f in self._fields)
                if key not in merged:
                    merged[key] = dict(zip(self._fields, key))
                    merged[key][self._annotation_field] = 0
                merged[key][self._annotation_field] += row[self._annotation_field]
        yield from merged.values()


def _iter_media_list(media_list):
    """Iterate over a queryset or a _CombinedMediaBucket."""
    if isinstance(media_list, _CombinedMediaBucket):
        yield from media_list
    else:
        yield from media_list


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
            status_counts[count_data["status"]] = (
                status_counts.get(count_data["status"], 0) + count_data["count"]
            )
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
    top_rated_count = STATISTICS_TOP_RATED_OVERALL
    # Per-media-type top rated (for the new compact cards)
    top_rated_by_type = {}
    top_rated_per_type_count = STATISTICS_TOP_N

    counter = itertools.count()  # Ensures stable sorting for equal scores

    # Infer user from user_media for fetching all entries
    user = _infer_user_from_user_media(user_media)
    score_scale_max = user.rating_scale_max if user else 10
    score_range = range(score_scale_max + 1)

    for media_type, media_list in user_media.items():
        score_counts = dict.fromkeys(score_range, 0)
        # _all_qs: list of individual querysets; handles combined anime bucket (flat + grouped TV)
        _all_qs = (
            list(media_list._querysets)
            if isinstance(media_list, _CombinedMediaBucket)
            else [media_list]
        )

        # Group media by item to identify which items appear in the date range
        media_by_item = defaultdict(list)
        for _qs in _all_qs:
            for media in _qs.select_related("item"):
                item = getattr(media, "item", None)
                key = item.id if item else media.id
                media_by_item[key].append(media)

        # For each item that appears in the date range, fetch ALL entries (not just date-filtered)
        # to find the aggregated score, even if the score was set outside the date range
        deduped_scored = {}
        if user:
            # Derive model class(es) from actual queryset(s); handles grouped anime (TV) in anime bucket
            _model_classes_by_item_id = {}
            for _qs in _all_qs:
                for entry in _qs.select_related("item"):
                    item = getattr(entry, "item", None)
                    if item:
                        _model_classes_by_item_id[item.id] = type(entry)

            # Get all unique item IDs from items that appear in the date range
            item_ids_in_range = set()
            item_id_to_key_map = {}  # Map item.id -> key used in media_by_item
            for key, entries in media_by_item.items():
                for entry in entries:
                    item = getattr(entry, "item", None)
                    if item:
                        item_ids_in_range.add(item.id)
                        item_id_to_key_map[item.id] = key

            # Fetch ALL entries for these items (not just date-filtered ones),
            # querying each model class separately in case the bucket is mixed (flat + grouped anime)
            if item_ids_in_range:
                _ids_by_model = defaultdict(set)
                for item_id, model_cls in _model_classes_by_item_id.items():
                    if item_id in item_ids_in_range:
                        _ids_by_model[model_cls].add(item_id)

                all_entries_by_item_id: dict = defaultdict(list)
                for model_cls, item_ids_for_model in _ids_by_model.items():
                    _entries_q = model_cls.objects.filter(
                        user=user,
                        item_id__in=item_ids_for_model,
                    ).select_related("item").order_by("-created_at")
                    for entry in _entries_q:
                        _item = getattr(entry, "item", None)
                        if _item:
                            all_entries_by_item_id[_item.id].append(entry)

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

        for media in _iter_media_list(queryset):
            # Grouped anime items are TV model instances with seasons; expand to season level
            if hasattr(media, "seasons"):
                seasons_qs = getattr(media, "seasons", None)
                if seasons_qs is None:
                    continue
                for season in seasons_qs.all():
                    _tl_local_start = (
                        timezone.localdate(season.start_date) if season.start_date else None
                    )
                    _tl_local_end = (
                        timezone.localdate(season.end_date) if season.end_date else None
                    )
                    if season.start_date and season.end_date:
                        _cur = _tl_local_start
                        while _cur <= _tl_local_end:
                            timeline[f"{calendar.month_name[_cur.month]} {_cur.year}"].append(season)
                            _cur += relativedelta(months=1)
                            _cur = _cur.replace(day=1)
                    elif season.start_date:
                        timeline[f"{calendar.month_name[_tl_local_start.month]} {_tl_local_start.year}"].append(season)
                    elif season.end_date:
                        timeline[f"{calendar.month_name[_tl_local_end.month]} {_tl_local_end.year}"].append(season)
                continue

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

    week_start_sunday = user.week_start_day == WeekStartDayChoices.SUNDAY
    start_date_aligned = get_aligned_week_start(start_date, week_start_sunday=week_start_sunday)

    combined_data = get_filtered_historical_data(start_date_aligned, end_date, user)

    # update start_date values from historical records if not provided
    if start_date is None:
        dates = [item["date"] for item in combined_data]
        start_date = datetime.datetime.combine(
            min(dates) if dates else timezone.localdate(),
            datetime.time.min,
        )
        start_date_aligned = get_aligned_week_start(start_date, week_start_sunday=week_start_sunday)

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

    base_weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if week_start_sunday:
        weekday_labels = [base_weekdays[6], *base_weekdays[:6]]
    else:
        weekday_labels = base_weekdays

    # Generate months list with their week-start day counts
    months = []
    mondays_per_month = []
    current_month = date_range[0].strftime("%b")
    monday_count = 0
    week_start_weekday = 6 if week_start_sunday else 0

    for current_date in date_range:
        if current_date.weekday() == week_start_weekday:
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
        "weekday_labels": weekday_labels,
        "stats": {
            "most_active_day": most_active_day,
            "most_active_day_percentage": day_percentage,
            "current_streak": streaks["current_streak"],
            "longest_streak": streaks["longest_streak"],
            "longest_streak_start": streaks["longest_streak_start"],
            "longest_streak_end": streaks["longest_streak_end"],
        },
    }


def get_aligned_week_start(datetime_obj, *, week_start_sunday=False):
    """Get the week-start day of the week containing the given date."""
    if datetime_obj is None:
        return None

    target_weekday = 6 if week_start_sunday else 0  # Sun=6, Mon=0
    days_to_subtract = (datetime_obj.weekday() - target_weekday) % 7
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

        for media_data in _iter_media_list(media_list):
            media = getattr(media_data, "media", media_data)

            if media_type == MediaTypes.TV.value:
                tv_minutes, _ = _calculate_tv_time(media, start_date, end_date, logger)
                total_minutes += tv_minutes
                continue

            if media_type == MediaTypes.ANIME.value:
                # Grouped anime uses TV model (seasons + episodes); flat anime uses progress field
                if hasattr(media, "seasons"):
                    anime_minutes, _ = _calculate_tv_time(media, start_date, end_date, logger)
                else:
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
    top_genres = _compute_movie_tv_top_genres(play_details, limit=STATISTICS_TOP_N)

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
    top_genres = _compute_movie_tv_top_genres(play_details, limit=STATISTICS_TOP_N)

    return {
        "hours": hours_breakdown,
        "plays": plays_breakdown,
        "charts": charts,
        "has_data": total_plays > 0,
        "top_genres": top_genres,
    }


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
def _compute_movie_tv_top_genres(play_details, limit=STATISTICS_TOP_N):
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
            for media in _iter_media_list(media_list):
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
            for media in _iter_media_list(media_list):
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
            for tv in _iter_media_list(media_list):
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

        # Anime: grouped anime uses episode-level data; flat anime uses progress * runtime
        elif media_type == MediaTypes.ANIME.value:
            for media in _iter_media_list(media_list):
                if hasattr(media, "seasons"):
                    # Grouped anime (TV model) — distribute by episode end_date
                    seasons = getattr(media, "seasons", None)
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
                            try:
                                ep_minutes = _calculate_episode_time_from_cache(episode, logger)
                            except Exception:
                                ep_minutes = 0
                            if ep_minutes and ep_minutes > 0:
                                label = ep_date.isoformat()
                                if media_type in per_type_minutes and label in per_type_minutes[media_type]:
                                    per_type_minutes[media_type][label] += ep_minutes
                else:
                    # Flat anime (Anime model) — total minutes from cached runtime * progress
                    episode_count = getattr(media, "progress", 0) or 0
                    if episode_count <= 0:
                        continue
                    minutes = _get_anime_runtime_from_cache(media, episode_count, logger, "(daily aggregation)")
                    if not minutes or minutes <= 0:
                        continue

                    media_start = getattr(media, "start_date", None)
                    media_end = getattr(media, "end_date", None)
                    if media_start and media_end:
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
            for media in _iter_media_list(media_list):
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
            for media in _iter_media_list(media_list):
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

            for media in _iter_media_list(media_list):
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

            for media in _iter_media_list(media_list):
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
            for media in _iter_media_list(media_list):
                total_time_minutes = 0
                episode_count = 0

                if normalized_type == "tv":
                    total_time_minutes, episode_count = _calculate_tv_time(media, start_date, end_date, logger)
                elif normalized_type == "anime":
                    # Grouped anime uses TV model (seasons + episodes)
                    if hasattr(media, "seasons"):
                        total_time_minutes, episode_count = _calculate_tv_time(media, start_date, end_date, logger)
                    else:
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

        # Take top 50 for games, top 10 for other media types
        limit = 50 if normalized_type == "game" else 10
        top_played[normalized_type] = media_with_progress[:limit]

    return top_played


# ---------------------------------------------------------------------------
# Re-exports from extracted submodules — keeps all callers using
# `from app import statistics as stats` fully transparent.
# ---------------------------------------------------------------------------
from app.stats_podcast import (  # noqa: E402,F401
    _collect_podcast_play_data,
    _compute_podcast_top_lists,
    _get_podcast_history_data,
    _get_podcast_runtime_minutes,
    get_podcast_consumption_stats,
)
from app.stats_reading import (  # noqa: E402,F401
    _build_reading_top_authors,
    _build_weighted_media_charts,
    _extract_cached_item_authors,
    _extract_item_authors,
    _fetch_reading_items_with_authors,
    _format_reading_unit,
    _normalize_item_author_names,
    _reading_entry_in_range,
    get_reading_consumption_stats,
)
from app.stats_game import (  # noqa: E402,F401
    DAILY_AVERAGE_BANDS,
    _build_daily_average_band_top_games,
    _build_daily_average_distribution_chart,
    _build_game_hours_charts,
    _collect_game_data,
    _collect_game_play_data,
    _compute_game_platform_breakdown,
    _compute_game_top_daily_average,
    _compute_game_top_genres,
    _game_entry_in_range,
    _get_daily_average_band_index,
    get_game_consumption_stats,
)
from app.stats_music import (  # noqa: E402,F401
    COUNTRY_NAME_MAP,
    _collect_music_play_data,
    _compute_music_top_lists,
    _compute_music_top_rollups,
    _country_name_from_code,
    _hydrate_music_metadata_for_rollups,
    _parse_release_date_str,
    get_music_consumption_stats,
)
