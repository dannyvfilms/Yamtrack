import logging
from collections import defaultdict
from datetime import timedelta

from django.apps import apps
from django.db import connection, models
from django.db.models import (
    Count,
    Exists,
    F,
    Max,
    OuterRef,
    Prefetch,
    Q,
    Window,
)
from django.db.models.functions import RowNumber
from django.utils import timezone

import events
import users
from app.models.choices import MediaTypes, Sources, Status
from app.models.item import Item

logger = logging.getLogger(__name__)


def _normalize_media_list_filter_value(value):
    return str(value or "").strip().lower()


def _filter_queryset_by_item_json_array_ci(
    queryset,
    item_json_field: str,
    normalized_target: str,
):
    """Match Item JSON string arrays with case-insensitive element compare."""
    if not normalized_target:
        return queryset
    media_table = queryset.model._meta.db_table
    item_table = Item._meta.db_table
    col = Item._meta.get_field(item_json_field).column
    mt = connection.ops.quote_name(media_table)
    it = connection.ops.quote_name(item_table)
    cc = connection.ops.quote_name(col)
    id_col = connection.ops.quote_name("id")
    item_fk = connection.ops.quote_name("item_id")
    if connection.vendor == "postgresql":
        where_sql = f"""
            EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(
                    COALESCE(
                        (SELECT {it}.{cc}::jsonb FROM {it}
                         WHERE {it}.{id_col} = {mt}.{item_fk}),
                        '[]'::jsonb
                    )
                ) AS _arr_el
                WHERE LOWER(_arr_el::text) = %s
            )
        """
    elif connection.vendor == "sqlite":
        where_sql = f"""
            EXISTS (
                SELECT 1 FROM json_each(
                    COALESCE(
                        (SELECT {it}.{cc} FROM {it}
                         WHERE {it}.{id_col} = {mt}.{item_fk}),
                        '[]'
                    )
                )
                WHERE LOWER(json_each.value) = %s
            )
        """
    else:
        kw = {f"item__{item_json_field}__contains": [normalized_target]}
        return queryset.filter(**kw)
    return queryset.extra(where=[where_sql], params=[normalized_target])


class MediaManager(models.Manager):
    """Custom manager for media models."""

    def get_historical_models(self):
        """Return list of historical model names."""
        return [f"historical{media_type}" for media_type in MediaTypes.values]

    def resolve_direction(self, sort_filter, direction=None):
        """Normalize sort direction with per-field defaults."""
        normalized = (direction or "").lower()
        if normalized not in ("asc", "desc"):
            return self._default_direction(sort_filter)
        return normalized

    def _default_direction(self, sort_filter):
        """Return default direction for a sort key."""
        if sort_filter in (
            "author",
            "popularity",
            "runtime",
            "start_date",
            "title",
            "next_episode_air_date",
            "time_left",
            "time_to_beat",
        ):
            return "asc"
        return "desc"

    def _apply_list_sql_filters(self, queryset, user, media_type, filters):
        """Apply Item-level filters in SQL before window deduplication and materialization."""
        if not filters:
            return queryset

        genre = str(filters.get("genre") or "").strip()
        if genre:
            queryset = _filter_queryset_by_item_json_array_ci(
                queryset,
                "genres",
                _normalize_media_list_filter_value(genre),
            )
        implied_genre = str(filters.get("implied_genre") or "").strip()
        if implied_genre and media_type == MediaTypes.MUSIC.value:
            queryset = _filter_queryset_by_item_json_array_ci(
                queryset,
                "implied_genres",
                _normalize_media_list_filter_value(implied_genre),
            )

        year = str(filters.get("year") or "").strip()
        if year:
            normalized_year = _normalize_media_list_filter_value(year)
            if normalized_year == "unknown":
                queryset = queryset.filter(item__release_datetime__isnull=True)
            else:
                try:
                    queryset = queryset.filter(item__release_datetime__year=int(year))
                except (TypeError, ValueError):
                    pass

        release = str(filters.get("release") or "all").strip().lower()
        today = timezone.localdate()
        if release == "released":
            queryset = queryset.filter(
                item__release_datetime__isnull=False,
                item__release_datetime__date__lte=today,
            )
        elif release == "not_released":
            queryset = queryset.filter(
                Q(item__release_datetime__isnull=True)
                | Q(item__release_datetime__date__gt=today),
            )

        source = str(filters.get("source") or "").strip()
        if source:
            queryset = queryset.filter(item__source=source)

        if media_type in (MediaTypes.TV.value, MediaTypes.MOVIE.value, MediaTypes.ANIME.value):
            language = str(filters.get("language") or "").strip()
            if language:
                queryset = _filter_queryset_by_item_json_array_ci(
                    queryset,
                    "languages",
                    _normalize_media_list_filter_value(language),
                )
            country = str(filters.get("country") or "").strip()
            if country:
                queryset = queryset.filter(item__country__iexact=country)

        if media_type == MediaTypes.GAME.value:
            platform = str(filters.get("platform") or "").strip()
            if platform:
                normalized_platform = _normalize_media_list_filter_value(platform)
                CollectionEntry = apps.get_model("app", "CollectionEntry")
                explicit_collection_platforms = CollectionEntry.objects.filter(
                    user=user,
                    item_id=OuterRef("item_id"),
                ).exclude(resolution="")
                matching_collection_platforms = explicit_collection_platforms.filter(
                    resolution__iexact=platform,
                )
                platform_json_qs = _filter_queryset_by_item_json_array_ci(
                    queryset,
                    "platforms",
                    normalized_platform,
                )
                queryset = queryset.annotate(
                    has_collection_platform=Exists(explicit_collection_platforms),
                    matches_collection_platform=Exists(matching_collection_platforms),
                ).filter(
                    Q(matches_collection_platform=True)
                    | Q(
                        has_collection_platform=False,
                        pk__in=platform_json_qs.values("pk"),
                    ),
                )

        tag_included_ids = filters.get("tag_included_ids")
        if tag_included_ids is not None:
            queryset = queryset.filter(item_id__in=tag_included_ids)

        tag_excluded_ids = filters.get("tag_excluded_ids")
        if tag_excluded_ids is not None:
            queryset = queryset.exclude(item_id__in=tag_excluded_ids)

        return queryset

    def get_media_list(
        self,
        user,
        media_type,
        status_filter,
        sort_filter,
        search=None,
        direction=None,
        *,
        list_sql_filters=None,
    ):
        """Get a media list by type with filtering and sorting."""
        model = apps.get_model(app_label="app", model_name=media_type)
        direction = self.resolve_direction(sort_filter, direction)
        dup_state = {}

        # Build base queryset
        queryset = model.objects.filter(user=user.id)

        # Apply status filter
        if status_filter != users.models.MediaStatusChoices.ALL:
            queryset = queryset.filter(status=status_filter)

        # Apply search filter
        if search:
            queryset = queryset.filter(
                models.Q(item__title__icontains=search)
                | models.Q(item__media_id__icontains=search),
            )

        queryset = self._apply_list_sql_filters(queryset, user, media_type, list_sql_filters or {})

        # Handle duplicate entries by selecting the most recent record for each item
        has_progress_field = any(
            getattr(field, "attname", "") == "progress"
            for field in model._meta.get_fields()
            if getattr(field, "concrete", False)
        )
        if sort_filter == "progress" and has_progress_field:
            # For progress sorting, select the record with highest individual progress
            queryset = queryset.annotate(
                repeats=Window(
                    expression=Count("id"),
                    partition_by=[F("item")],
                ),
                row_number=Window(
                    expression=RowNumber(),
                    partition_by=[F("item")],
                    order_by=F("progress").desc(),
                ),
            ).filter(row_number=1)
        else:
            # For non-progress sorting, select the most recent record
            queryset = queryset.annotate(
                repeats=Window(
                    expression=Count("id"),
                    partition_by=[F("item")],
                ),
                row_number=Window(
                    expression=RowNumber(),
                    partition_by=[F("item")],
                    order_by=F("created_at").desc(),
                ),
            ).filter(row_number=1)

        queryset = queryset.select_related("item").defer(
            "item__isbn",
            "item__creators",
            "item__provider_keywords",
            "item__provider_external_ids",
            "item__provider_certification",
            "item__provider_collection_id",
            "item__provider_collection_name",
            "item__provider_game_lengths_match",
            "item__provider_game_lengths_fetched_at",
            "item__trakt_popularity_fetched_at",
            "item__metadata_fetched_at",
            "item__themes",
            "item__provider_popularity",
            "item__provider_rating_count",
            "item__trakt_rating",
            "item__trakt_rating_count",
            "item__trakt_popularity_score",
            "item__publishers",
            "item__source_material",
            "item__series_name",
        )
        queryset = self._apply_prefetch_related(queryset, media_type, list_mode=True)

        requires_presort_aggregation = (
            sort_filter in ("progress", "plays", "next_episode_air_date")
            and media_type not in (MediaTypes.TV.value, MediaTypes.SEASON.value)
        )

        # Generic progress sorting uses Python and reads aggregated_progress, so
        # duplicates must be aggregated before sorting in that specific path.
        if requires_presort_aggregation:
            queryset = self._aggregate_duplicate_data(queryset, user, media_type, dup_state)

        # Apply sorting AFTER aggregation
        if sort_filter:
            queryset = self._sort_media_list(queryset, sort_filter, media_type, direction)

        # Re-apply duplicate aggregation because SQL queryset operations in sorting
        # can materialize fresh model instances and drop dynamic aggregated attrs.
        return self._aggregate_duplicate_data(queryset, user, media_type, dup_state)

    def _aggregate_duplicate_data(self, queryset, user, media_type, dup_state=None):
        """Aggregate data from duplicate entries for each item."""
        # Materialize once — avoids re-evaluating the queryset (and re-firing all
        # prefetch queries) for both the ID-collection pass and the aggregation pass.
        # Lists pass through as-is, so callers that already hold a list are safe.
        media_list = queryset if isinstance(queryset, list) else list(queryset)

        queried_item_ids = frozenset(media.item_id for media in media_list)

        if not queried_item_ids:
            return media_list

        model = apps.get_model(app_label="app", model_name=media_type)

        if (
            dup_state is not None
            and dup_state.get("ids") == queried_item_ids
            and dup_state.get("groups") is not None
        ):
            media_by_item = dup_state["groups"]
        else:
            # Fetch ALL entries (across all statuses) for only the queried items.
            # Using all statuses is intentional: an item filtered as IN_PROGRESS may have
            # a more-recent COMPLETED entry that should determine its aggregated_status.
            all_media = model.objects.filter(
                user=user.id,
                item_id__in=queried_item_ids,
            ).select_related("item")
            all_media = self._apply_prefetch_related(all_media, media_type, list_mode=True)

            # Group media by item_id
            media_by_item = {}
            for media in all_media:
                media_by_item.setdefault(media.item_id, []).append(media)

            if dup_state is not None:
                dup_state["ids"] = queried_item_ids
                dup_state["groups"] = media_by_item

        for media in media_list:
            entries = media_by_item.get(media.item_id, [])
            if len(entries) > 1:
                self._aggregate_item_data(media, entries)

        return media_list

    def _aggregate_item_data(self, display_media, all_media_entries):
        """Aggregate data from multiple media entries for the same item."""
        # Aggregate progress:
        # - Movies: count completed entries as plays (legacy rows may have progress=0)
        # - Other media: sum raw progress values
        if getattr(display_media.item, "media_type", None) == MediaTypes.MOVIE.value:
            completed_entries = [
                entry
                for entry in all_media_entries
                if entry.end_date or entry.status == Status.COMPLETED.value
            ]
            total_progress = len(completed_entries)
        else:
            total_progress = sum(entry.progress for entry in all_media_entries)
        display_media.aggregated_progress = total_progress

        # Aggregate start date (earliest start date)
        start_dates = [entry.start_date for entry in all_media_entries if entry.start_date]
        if start_dates:
            display_media.aggregated_start_date = min(start_dates)
        else:
            display_media.aggregated_start_date = None

        # Aggregate end date (latest end date)
        end_dates = [entry.end_date for entry in all_media_entries if entry.end_date]
        if end_dates:
            display_media.aggregated_end_date = max(end_dates)
        else:
            display_media.aggregated_end_date = None

        # Aggregate status (most recent status by activity)
        latest_status = None
        latest_status_activity = None

        # Aggregate rating (find the most recent rating among all entries)
        # Since created_at only represents when the entry was first created,
        # we need to use a different approach to find the most recent rating
        # We'll prioritize entries with more recent activity (end_date, progressed_at)
        latest_rating = None
        latest_rating_activity = None

        for entry in all_media_entries:
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
                if latest_rating_activity is None or entry_activity > latest_rating_activity:
                    latest_rating_activity = entry_activity
                    latest_rating = entry.score
            else:
                entry_activity = entry.end_date or entry.progressed_at or entry.created_at

            if entry_activity and (
                latest_status_activity is None or entry_activity > latest_status_activity
            ):
                latest_status_activity = entry_activity
                latest_status = entry.status

        display_media.aggregated_status = latest_status or display_media.status

        if latest_rating is not None:
            display_media.aggregated_score = latest_rating
        else:
            display_media.aggregated_score = None

        # Store the number of repeats for display
        display_media.repeats = len(all_media_entries)

    def _apply_prefetch_related(self, queryset, media_type, list_mode=False):
        """Apply appropriate prefetch_related based on media type.

        list_mode=True narrows the episode queryset to only the fields required
        for list-view progress/date calculations, avoiding the cost of
        materializing full Item objects for every episode.
        """
        TV = apps.get_model("app", "TV")
        Season = apps.get_model("app", "Season")
        Episode = apps.get_model("app", "Episode")
        # Apply media-specific prefetches
        if media_type == MediaTypes.TV.value or (
            media_type == MediaTypes.ANIME.value and queryset.model == TV
        ):
            episode_qs = Episode.objects.select_related("item")
            if list_mode:
                # Load only the two fields accessed in the list path:
                # ep.item.episode_number and ep.end_date.  Deferring the
                # remaining ~30 Item columns cuts Django object instantiation
                # time proportionally for libraries with many episodes.
                episode_qs = episode_qs.only(
                    "id",
                    "end_date",
                    "related_season_id",
                    "item__id",
                    "item__episode_number",
                )
            return queryset.prefetch_related(
                Prefetch(
                    "seasons",
                    queryset=Season.objects.select_related("item"),
                ),
                Prefetch(
                    "seasons__item__event_set",
                    queryset=events.models.Event.objects.all(),
                    to_attr="prefetched_events",
                ),
                Prefetch(
                    "seasons__episodes",
                    queryset=episode_qs,
                ),
            )

        base_queryset = queryset.prefetch_related(
            Prefetch(
                "item__event_set",
                queryset=events.models.Event.objects.all(),
                to_attr="prefetched_events",
            ),
        )

        if media_type == MediaTypes.SEASON.value:
            return base_queryset.select_related("related_tv__item").prefetch_related(
                Prefetch(
                    "episodes",
                    queryset=Episode.objects.select_related("item"),
                ),
            )

        return base_queryset

    def _sort_media_list(self, queryset, sort_filter, media_type=None, direction=None):
        """Sort media list using SQL sorting with annotations for calculated fields."""
        direction = self.resolve_direction(sort_filter, direction)
        if media_type == MediaTypes.TV.value:
            return self._sort_tv_media_list(queryset, sort_filter, direction)
        if media_type == MediaTypes.SEASON.value:
            return self._sort_season_media_list(queryset, sort_filter, direction)

        return self._sort_generic_media_list(queryset, sort_filter, direction)

    def _next_episode_air_date_value(self, media):
        """Return the air datetime for the next episode in watch order."""
        item = getattr(media, "item", None)
        if item is None:
            return None

        def _is_usable_datetime(value):
            return value is not None and getattr(value, "year", 0) >= 1900

        def _progress_index():
            progress_value = getattr(media, "aggregated_progress", None)
            if progress_value is None:
                progress_value = getattr(media, "progress", 0) or 0
            try:
                index = int(progress_value)
            except (TypeError, ValueError):
                index = 0
            return max(index, 0)

        def _as_list(related):
            if related is None:
                return []
            if hasattr(related, "all"):
                return list(related.all())
            return list(related)

        def _ordered_events_for_item(source_item):
            events = [
                event
                for event in _as_list(getattr(source_item, "prefetched_events", None))
                if getattr(event, "content_number", None) is not None
            ]
            events.sort(key=lambda event: event.content_number)
            return events

        def _ordered_episodes_for_related(related):
            episodes = [
                episode
                for episode in _as_list(getattr(related, "episodes", None))
                if getattr(getattr(episode, "item", None), "episode_number", None) is not None
            ]
            episodes.sort(
                key=lambda episode: getattr(getattr(episode, "item", None), "episode_number", 0) or 0,
            )
            return episodes

        media_type = getattr(item, "media_type", None)
        progress_index = _progress_index()

        if media_type == MediaTypes.TV.value:
            candidates = []
            seasons = [
                season
                for season in _as_list(getattr(media, "seasons", None))
                if getattr(getattr(season, "item", None), "season_number", None) not in (None, 0)
            ]
            seasons.sort(
                key=lambda season: getattr(getattr(season, "item", None), "season_number", 0) or 0,
            )

            for season in seasons:
                season_item = getattr(season, "item", None)
                season_events = _ordered_events_for_item(season_item) if season_item else []
                if season_events:
                    candidates.extend(season_events)
                    continue

                candidates.extend(_ordered_episodes_for_related(season))

            if progress_index >= len(candidates):
                return None

            candidate = candidates[progress_index]
            air_date = getattr(candidate, "datetime", None)
            if air_date is None:
                air_date = getattr(getattr(candidate, "item", None), "release_datetime", None)
            return air_date if _is_usable_datetime(air_date) else None

        if media_type == MediaTypes.SEASON.value:
            candidates = _ordered_events_for_item(item)
            if not candidates:
                candidates = _ordered_episodes_for_related(media)

            if progress_index >= len(candidates):
                return None

            candidate = candidates[progress_index]
            air_date = getattr(candidate, "datetime", None)
            if air_date is None:
                air_date = getattr(getattr(candidate, "item", None), "release_datetime", None)
            return air_date if _is_usable_datetime(air_date) else None

        if media_type == MediaTypes.ANIME.value:
            candidates = _ordered_events_for_item(item)
            if not candidates:
                candidates = _ordered_episodes_for_related(media)

            if progress_index >= len(candidates):
                return None

            candidate = candidates[progress_index]
            air_date = getattr(candidate, "datetime", None)
            if air_date is None:
                air_date = getattr(getattr(candidate, "item", None), "release_datetime", None)
            return air_date if _is_usable_datetime(air_date) else None

        return None

    def _sort_media_items_by_next_episode_air_date(self, media_items, direction):
        """Sort media items by next episode air date with missing dates last."""
        with_dates = []
        without_dates = []

        for media in media_items:
            next_episode_air_date = self._next_episode_air_date_value(media)
            media.next_episode_air_date = next_episode_air_date
            if next_episode_air_date is not None:
                with_dates.append((media, next_episode_air_date))
            else:
                without_dates.append(media)

        if direction == "desc":
            with_dates.sort(
                key=lambda entry: (
                    entry[1],
                    getattr(getattr(entry[0], "item", None), "title", "").lower(),
                ),
                reverse=True,
            )
        else:
            with_dates.sort(
                key=lambda entry: (
                    entry[1],
                    getattr(getattr(entry[0], "item", None), "title", "").lower(),
                ),
            )

        without_dates.sort(
            key=lambda media: getattr(getattr(media, "item", None), "title", "").lower(),
        )
        return [media for media, _air_date in with_dates] + without_dates

    def _sort_tv_media_list(self, queryset, sort_filter, direction):
        """Sort TV media list based on the sort criteria."""
        if sort_filter == "start_date":
            # Annotate with the minimum start_date from related seasons/episodes
            queryset = queryset.annotate(
                calculated_start_date=models.Min(
                    "seasons__episodes__end_date",
                    filter=models.Q(seasons__item__season_number__gt=0),
                ),
            )
            order = (
                models.F("calculated_start_date").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_start_date").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "end_date":
            # Annotate with the maximum end_date from related seasons/episodes
            queryset = queryset.annotate(
                calculated_end_date=models.Max(
                    "seasons__episodes__end_date",
                    filter=models.Q(seasons__item__season_number__gt=0),
                ),
            )
            order = (
                models.F("calculated_end_date").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_end_date").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "next_episode_air_date":
            return self._sort_media_items_by_next_episode_air_date(list(queryset), direction)

        if sort_filter == "progress":
            # Annotate with the sum of episodes watched (excluding season 0)
            queryset = queryset.annotate(
                # Count episodes in regular seasons (season_number > 0)
                calculated_progress=models.Count(
                    "seasons__episodes",
                    filter=models.Q(seasons__item__season_number__gt=0),
                ),
            )
            order = (
                models.F("calculated_progress").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_progress").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "time_left":
            # For time_left sorting, we need custom Python sorting
            # Return queryset as-is for custom sorting in views
            return queryset

        # Default to generic sorting
        return self._sort_generic_media_list(queryset, sort_filter, direction)

    def _sort_season_media_list(self, queryset, sort_filter, direction):
        """Sort Season media list based on the sort criteria."""
        if sort_filter == "start_date":
            # Annotate with the minimum end_date from related episodes
            queryset = queryset.annotate(
                calculated_start_date=models.Min("episodes__end_date"),
            )
            order = (
                models.F("calculated_start_date").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_start_date").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "end_date":
            # Annotate with the maximum end_date from related episodes
            queryset = queryset.annotate(
                calculated_end_date=models.Max("episodes__end_date"),
            )
            order = (
                models.F("calculated_end_date").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_end_date").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "next_episode_air_date":
            return self._sort_media_items_by_next_episode_air_date(list(queryset), direction)

        if sort_filter == "progress":
            # Annotate with the maximum episode number
            queryset = queryset.annotate(
                calculated_progress=models.Max("episodes__item__episode_number"),
            )
            order = (
                models.F("calculated_progress").asc(nulls_last=True)
                if direction == "asc"
                else models.F("calculated_progress").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        # Default to generic sorting
        return self._sort_generic_media_list(queryset, sort_filter, direction)

    def _sort_generic_media_list(self, queryset, sort_filter, direction):
        """Apply generic sorting logic for all media types."""
        if sort_filter == "author":
            return self._sort_media_list_by_author(list(queryset), direction)

        if sort_filter == "next_episode_air_date":
            return self._sort_media_items_by_next_episode_air_date(list(queryset), direction)

        # Handle progress sorting specially to use aggregated progress
        if sort_filter in ("progress", "plays"):
            # Since we're now sorting after aggregation, we can use the aggregated_progress attribute
            # Convert to list for Python-based sorting since aggregated_progress is a Python attribute
            media_list = list(queryset)
            return sorted(
                media_list,
                key=lambda x: (getattr(x, "aggregated_progress", x.progress), x.item.title.lower()),
                reverse=(direction == "desc"),
            )

        # Handle sorting by date fields with special null handling
        if sort_filter in ("start_date", "end_date", "date_added"):
            sort_field = "created_at" if sort_filter == "date_added" else sort_filter
            order = (
                models.F(sort_field).asc(nulls_last=True)
                if direction == "asc"
                else models.F(sort_field).desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "release_date":
            order = (
                models.F("item__release_datetime").asc(nulls_last=True)
                if direction == "asc"
                else models.F("item__release_datetime").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "popularity":
            order = (
                models.F("item__trakt_popularity_rank").asc(nulls_last=True)
                if direction == "asc"
                else models.F("item__trakt_popularity_rank").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        if sort_filter == "critic_rating":
            order = (
                models.F("item__provider_rating").asc(nulls_last=True)
                if direction == "asc"
                else models.F("item__provider_rating").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        # Handle sorting by Item fields
        item_fields = [f.name for f in Item._meta.fields]
        if sort_filter in item_fields:
            if sort_filter == "title":
                # Case-insensitive title sorting
                expr = models.functions.Lower("item__title")
                order = expr.asc() if direction == "asc" else expr.desc()
                return queryset.order_by(order)
            # Default sorting for other Item fields
            order = (
                models.F(f"item__{sort_filter}").asc(nulls_last=True)
                if direction == "asc"
                else models.F(f"item__{sort_filter}").desc(nulls_last=True)
            )
            return queryset.order_by(order, models.functions.Lower("item__title"))

        # Default sorting by media field
        order = (
            models.F(sort_filter).asc(nulls_last=True)
            if direction == "asc"
            else models.F(sort_filter).desc(nulls_last=True)
        )
        return queryset.order_by(order, models.functions.Lower("item__title"))

    def _sort_media_list_by_author(self, media_list, direction):
        """Sort media items by their first persisted author, keeping missing authors last."""

        def _primary_author(media):
            authors = getattr(getattr(media, "item", None), "authors", None) or []
            if not isinstance(authors, list):
                authors = [authors]
            for author in authors:
                author_text = str(author).strip()
                if author_text:
                    return author_text
            return ""

        with_author = []
        without_author = []
        for media in media_list:
            if _primary_author(media):
                with_author.append(media)
            else:
                without_author.append(media)

        with_author.sort(
            key=lambda media: (
                _primary_author(media).lower(),
                getattr(getattr(media, "item", None), "title", "").lower(),
            ),
            reverse=direction == "desc",
        )
        without_author.sort(
            key=lambda media: getattr(getattr(media, "item", None), "title", "").lower(),
        )
        return with_author + without_author

    def get_in_progress(self, user, sort_by, items_limit, specific_media_type=None):
        """Get a media list of in progress media by type."""
        list_by_type = {}
        media_types = self._get_media_types_to_process(user, specific_media_type)

        # Get user preference for planned items display mode
        planned_mode = getattr(user, "show_planned_on_home", users.models.PlannedHomeDisplayChoices.DISABLED)

        def filter_by_latest_status(media_list, desired_status):
            """Filter media entries by their most recent status across duplicates."""
            if not media_list:
                return media_list
            filtered = []
            for media in media_list:
                latest_status = getattr(media, "aggregated_status", None) or getattr(media, "status", None)
                if latest_status == desired_status:
                    filtered.append(media)
            return filtered

        for media_type in media_types:
            base_media_type = media_type

            # Get base media list for in-progress media
            in_progress_list = self.get_media_list(
                user=user,
                media_type=base_media_type,
                status_filter=Status.IN_PROGRESS.value,
                sort_filter=None,
            )
            in_progress_list = list(in_progress_list)
            in_progress_list = filter_by_latest_status(in_progress_list, Status.IN_PROGRESS.value)

            # Get planned items if needed
            planned_list = []
            if planned_mode != users.models.PlannedHomeDisplayChoices.DISABLED:
                planned_queryset = self.get_media_list(
                    user=user,
                    media_type=base_media_type,
                    status_filter=Status.PLANNING.value,
                    sort_filter=None,
                )
                planned_list = filter_by_latest_status(list(planned_queryset), Status.PLANNING.value)

            # Handle different modes
            if planned_mode == users.models.PlannedHomeDisplayChoices.DISABLED:
                # Only in-progress items
                media_list = in_progress_list
                if not media_list:
                    continue

                # Process in-progress items
                self.annotate_max_progress(media_list, base_media_type)
                self._annotate_next_event(media_list)

                if base_media_type == MediaTypes.SEASON.value:
                    self._fix_missing_season_images(media_list)

                sorted_list = self._sort_in_progress_media(media_list, sort_by)
                total_count = len(sorted_list)
                if specific_media_type:
                    paginated_list = sorted_list[items_limit:]
                else:
                    paginated_list = sorted_list[:items_limit]

                list_by_type[base_media_type] = {
                    "items": paginated_list,
                    "total": total_count,
                }

            elif planned_mode == users.models.PlannedHomeDisplayChoices.COMBINED:
                # Combine in-progress and planned items
                media_list = list(in_progress_list)
                existing_item_ids = {media.item.id for media in media_list}
                for planned_media in planned_list:
                    if planned_media.item.id not in existing_item_ids:
                        media_list.append(planned_media)
                        existing_item_ids.add(planned_media.item.id)

                if not media_list:
                    continue

                # Process combined items
                self.annotate_max_progress(media_list, base_media_type)
                self._annotate_next_event(media_list)

                if base_media_type == MediaTypes.SEASON.value:
                    self._fix_missing_season_images(media_list)

                sorted_list = self._sort_in_progress_media(media_list, sort_by)
                total_count = len(sorted_list)
                if specific_media_type:
                    paginated_list = sorted_list[items_limit:]
                else:
                    paginated_list = sorted_list[:items_limit]

                list_by_type[base_media_type] = {
                    "items": paginated_list,
                    "total": total_count,
                }

            elif planned_mode == users.models.PlannedHomeDisplayChoices.SEPARATED:
                # Separated mode: two distinct sections
                # Determine which sections to process based on specific_media_type request
                process_in_progress = True
                process_planned = True

                if specific_media_type:
                    if specific_media_type.endswith("_in_progress"):
                        process_planned = False
                    elif specific_media_type.endswith("_planned"):
                        process_in_progress = False

                # Handle in-progress section
                if process_in_progress and in_progress_list:
                    in_progress_processed = list(in_progress_list)
                    self.annotate_max_progress(in_progress_processed, base_media_type)
                    self._annotate_next_event(in_progress_processed)

                    if base_media_type == MediaTypes.SEASON.value:
                        self._fix_missing_season_images(in_progress_processed)

                    sorted_in_progress = self._sort_in_progress_media(in_progress_processed, sort_by)
                    total_in_progress = len(sorted_in_progress)

                    if specific_media_type and specific_media_type.endswith("_in_progress"):
                        paginated_in_progress = sorted_in_progress[items_limit:]
                    else:
                        paginated_in_progress = sorted_in_progress[:items_limit]

                    list_by_type[f"{base_media_type}_in_progress"] = {
                        "items": paginated_in_progress,
                        "total": total_in_progress,
                        "section_label": "In Progress",
                        "media_type": base_media_type,
                    }

                # Handle planned section
                if process_planned and planned_list:
                    planned_processed = list(planned_list)
                    self.annotate_max_progress(planned_processed, base_media_type)
                    self._annotate_next_event(planned_processed)

                    if base_media_type == MediaTypes.SEASON.value:
                        self._fix_missing_season_images(planned_processed)

                    sorted_planned = self._sort_in_progress_media(planned_processed, sort_by)
                    total_planned = len(sorted_planned)

                    if specific_media_type and specific_media_type.endswith("_planned"):
                        paginated_planned = sorted_planned[items_limit:]
                    else:
                        paginated_planned = sorted_planned[:items_limit]

                    list_by_type[f"{base_media_type}_planned"] = {
                        "items": paginated_planned,
                        "total": total_planned,
                        "section_label": "Planned",
                        "media_type": base_media_type,
                    }

        return list_by_type

    def get_recently_unrated(self, user, days=7):
        """Return recently played media items without a user score."""
        cutoff = timezone.now() - timedelta(days=days)
        recent_items = []
        media_types = self._get_media_types_to_process(user, None)

        def resolve_last_played(media):
            if media.item.media_type == MediaTypes.SEASON.value:
                return (
                    getattr(media, "last_watched", None)
                    or media.progressed_at
                    or media.created_at
                )
            return media.end_date or media.progressed_at or media.created_at

        for media_type in media_types:
            model = apps.get_model(app_label="app", model_name=media_type)

            rated_item_ids = model.objects.filter(
                user=user.id,
                score__isnull=False,
            ).values("item_id")

            queryset = (
                model.objects.filter(
                    user=user.id,
                    score__isnull=True,
                    status=Status.COMPLETED.value,
                )
                .exclude(item_id__in=rated_item_ids)
            )

            if media_type == MediaTypes.SEASON.value:
                queryset = queryset.filter(
                    episodes__end_date__gte=cutoff,
                ).annotate(
                    last_watched=Max("episodes__end_date"),
                )
                order_by_fields = [
                    F("last_watched").desc(nulls_last=True),
                    F("created_at").desc(),
                ]
            else:
                queryset = queryset.filter(
                    end_date__gte=cutoff,
                )
                order_by_fields = [
                    F("progressed_at").desc(nulls_last=True),
                    F("end_date").desc(nulls_last=True),
                    F("created_at").desc(),
                ]

            select_related_fields = ["item"]
            if media_type == MediaTypes.PODCAST.value:
                select_related_fields.append("show")
            elif media_type == MediaTypes.MUSIC.value:
                select_related_fields.append("album")

            queryset = queryset.annotate(
                repeats=Window(
                    expression=Count("id"),
                    partition_by=[F("item")],
                ),
                row_number=Window(
                    expression=RowNumber(),
                    partition_by=[F("item")],
                    order_by=order_by_fields,
                ),
            ).filter(row_number=1).select_related(*select_related_fields)

            queryset = self._apply_prefetch_related(queryset, media_type)
            items = list(queryset)
            for media in items:
                media.last_played_at = resolve_last_played(media)
                media.use_podcast_show = (
                    media.item.media_type == MediaTypes.PODCAST.value
                    and getattr(media, "show", None)
                )
            recent_items.extend(items)

        return sorted(
            recent_items,
            key=lambda media: media.last_played_at or media.created_at,
            reverse=True,
        )

    def _get_media_types_to_process(self, user, specific_media_type):
        """Determine which media types to process based on user settings."""
        if specific_media_type:
            # Extract base media_type if it has a suffix (e.g., "movie_in_progress" -> "movie")
            if "_" in specific_media_type:
                base_type = specific_media_type.rsplit("_", 1)[0]
                return [base_type]
            return [specific_media_type]

        media_types = [
            media_type
            for media_type in user.get_active_media_types()
            if media_type != MediaTypes.TV.value
        ]

        # Home should continue to include TV seasons when TV shows are enabled.
        if getattr(user, "tv_enabled", False) and MediaTypes.SEASON.value not in media_types:
            media_types.insert(0, MediaTypes.SEASON.value)

        return media_types

    def _annotate_next_event(self, media_list):
        """Annotate next_event for media items."""
        current_time = timezone.now()

        for media in media_list:
            # Get future events sorted by datetime
            future_events = sorted(
                [
                    event
                    for event in getattr(media.item, "prefetched_events", [])
                    if event.datetime > current_time
                ],
                key=lambda e: e.datetime,
            )

            media.next_event = future_events[0] if future_events else None

    def _fix_missing_season_images(self, season_list):
        """Annotate season cards with a display fallback without persisting it."""
        from app import helpers

        for season in season_list:
            item = getattr(season, "item", None)
            if item is None:
                continue

            show_item = getattr(getattr(season, "related_tv", None), "item", None)
            if show_item is None:
                show_item = (
                    Item.objects.filter(
                        media_id=item.media_id,
                        source=item.source,
                        media_type=MediaTypes.TV.value,
                    )
                    .only("image")
                    .first()
                )
            show_image = getattr(show_item, "image", None)
            primary_image = item.image

            # Older rows may have the TV show's poster copied onto the season item.
            # Keep using it visually, but mark it as a fallback rather than real
            # season art when it matches the tracked show poster exactly.
            if (
                helpers.has_real_image(primary_image)
                and helpers.has_real_image(show_image)
                and primary_image == show_image
            ):
                primary_image = None

            image, image_source = helpers.resolve_image_with_fallback(
                primary_image,
                show_image,
            )
            season.card_image_override = image
            season.card_image_source = image_source

    def _sort_in_progress_media(self, media_list, sort_by):
        """Sort in-progress media based on the sort criteria."""
        # Define primary sort functions based on sort_by
        primary_sort_functions = {
            users.models.HomeSortChoices.UPCOMING: lambda x: (
                x.next_event is None,
                x.next_event.datetime if x.next_event else None,
            ),
            users.models.HomeSortChoices.RECENT: lambda x: (
                -timezone.datetime.timestamp(
                    x.progressed_at if x.progressed_at is not None else x.created_at,
                )
            ),
            users.models.HomeSortChoices.COMPLETION: lambda x: (
                x.max_progress is None,
                -(
                    x.progress / x.max_progress * 100
                    if x.max_progress and x.max_progress > 0
                    else 0
                ),
            ),
            users.models.HomeSortChoices.EPISODES_LEFT: lambda x: (
                x.max_progress is None,
                (x.max_progress - x.progress if x.max_progress else 0),
            ),
            users.models.HomeSortChoices.TITLE: lambda x: x.item.title.lower(),
        }

        primary_sort_function = primary_sort_functions[sort_by]

        return sorted(
            media_list,
            key=lambda x: (
                primary_sort_function(x),
                -timezone.datetime.timestamp(
                    x.progressed_at if x.progressed_at is not None else x.created_at,
                ),
                x.item.title.lower(),
            ),
        )

    def annotate_max_progress(self, media_list, media_type):
        """Annotate max_progress for all media items."""
        current_datetime = timezone.now()

        if media_type in (MediaTypes.MOVIE.value, MediaTypes.COMIC_ISSUE.value):
            for media in media_list:
                media.max_progress = 1
            return

        if media_type == MediaTypes.TV.value:
            self._annotate_tv_released_episodes(media_list, current_datetime)
            return

        if media_type == MediaTypes.ANIME.value:
            grouped_media = [
                media
                for media in media_list
                if getattr(getattr(media, "item", None), "media_type", None)
                == MediaTypes.TV.value
            ]
            flat_media = [media for media in media_list if media not in grouped_media]
            if grouped_media:
                self._annotate_tv_released_episodes(grouped_media, current_datetime)
            if not flat_media:
                return
            media_list = flat_media

        if media_type == MediaTypes.SEASON.value:
            # For seasons, use metadata max_progress instead of database annotation
            # The metadata value is more accurate as it reflects the actual total episodes
            # from the provider, not just episodes with release_datetime set
            from app.providers import services
            for season in media_list:
                try:
                    season_metadata = services.get_media_metadata(
                        MediaTypes.SEASON.value,
                        season.item.media_id,
                        season.item.source,
                        [season.item.season_number],
                    )
                    # Use metadata max_progress if available, otherwise fall back to annotation
                    metadata_max_progress = season_metadata.get("max_progress")
                    if metadata_max_progress is not None:
                        season.max_progress = metadata_max_progress
                    else:
                        # Fall back to database annotation if metadata doesn't have max_progress
                        self._annotate_season_released_episodes([season], current_datetime)
                except Exception:
                    # If metadata fetch fails, fall back to database annotation
                    self._annotate_season_released_episodes([season], current_datetime)
            return

        if media_type == MediaTypes.BOOK.value:
            for media in media_list:
                media.max_progress = media.item.number_of_pages or None
            return

        if media_type in {
            MediaTypes.ANIME.value,
            MediaTypes.MANGA.value,
            MediaTypes.COMIC.value,
        }:
            from app import custom_metadata  # noqa: PLC0415

            manual_item_ids = set()
            for media in media_list:
                if media.item.source != Sources.MANUAL.value:
                    continue
                manual_item_ids.add(media.item.id)
                details = custom_metadata.build_manual_detail_payload(media.item)
                media.max_progress = custom_metadata.manual_max_progress(
                    media.item,
                    details,
                    fallback_max_progress=getattr(media, "max_progress", None),
                )
            if all(
                media.item.source == Sources.MANUAL.value
                for media in media_list
            ):
                return
        else:
            manual_item_ids = set()

        # For other media types, calculate max_progress from events
        # Create a dictionary mapping item_id to max content_number
        max_progress_dict = {}

        item_ids = [media.item.id for media in media_list]

        # Fetch all relevant events in a single query
        events_data = events.models.Event.objects.filter(
            item_id__in=item_ids,
            datetime__lte=current_datetime,
        ).values("item_id", "content_number")

        # Process events to find max content number per item
        for event in events_data:
            item_id = event["item_id"]
            content_number = event["content_number"]
            if content_number is not None:
                current_max = max_progress_dict.get(item_id, 0)
                max_progress_dict[item_id] = max(current_max, content_number)

        for media in media_list:
            if media.item.id in manual_item_ids:
                continue
            media.max_progress = max_progress_dict.get(media.item.id)

    def _annotate_tv_released_episodes(self, tv_list, current_datetime):
        """Annotate TV shows with the number of released episodes."""
        if not tv_list:
            return

        media_keys = {(tv.item.media_id, tv.item.source) for tv in tv_list}
        media_ids = {media_id for media_id, _ in media_keys}
        media_sources = {source for _, source in media_keys}

        released_by_show: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)

        episode_rows = (
            Item.objects.filter(
                media_type=MediaTypes.EPISODE.value,
                media_id__in=media_ids,
                source__in=media_sources,
                release_datetime__isnull=False,
                release_datetime__lte=current_datetime,
                season_number__gt=0,
            )
            .values("media_id", "source", "season_number")
            .annotate(max_episode=models.Max("episode_number"))
        )

        for row in episode_rows:
            key = (row["media_id"], row["source"])
            season_number = row["season_number"]
            max_episode = row["max_episode"] or 0
            released_by_show[key][season_number] = max(
                released_by_show[key].get(season_number, 0),
                max_episode,
            )

        released_events = (
            events.models.Event.objects.filter(
                item__media_id__in=media_ids,
                item__source__in=media_sources,
                item__media_type=MediaTypes.SEASON.value,
                item__season_number__gt=0,
                datetime__lte=current_datetime,
                content_number__isnull=False,
            )
            .exclude(datetime__year__lt=1900)
            .values(
                "item__media_id",
                "item__source",
                "item__season_number",
            )
            .annotate(max_episode=models.Max("content_number"))
        )

        for row in released_events:
            key = (row["item__media_id"], row["item__source"])
            season_number = row["item__season_number"]
            max_episode = row["max_episode"] or 0
            released_by_show[key][season_number] = max(
                released_by_show[key].get(season_number, 0),
                max_episode,
            )

        for tv in tv_list:
            key = (tv.item.media_id, tv.item.source)
            breakdown = released_by_show.get(key, {})
            tv.released_episode_breakdown = breakdown
            if breakdown:
                dropped_season_numbers = (
                    {
                        season.item.season_number
                        for season in tv.seasons.all()
                        if season.status == Status.DROPPED.value
                        and season.item.season_number != 0
                    }
                    if tv.pk is not None
                    else set()
                )
                effective_count = sum(
                    count
                    for season_num, count in breakdown.items()
                    if season_num not in dropped_season_numbers
                )
                tv.max_progress = effective_count or None
            else:
                tv.max_progress = None
                if tv.item.source == Sources.MANUAL.value:
                    from app import custom_metadata  # noqa: PLC0415

                    details = custom_metadata.build_manual_detail_payload(tv.item)
                    tv.max_progress = custom_metadata.manual_max_progress(
                        tv.item,
                        details,
                        fallback_max_progress=None,
                    )

    def _annotate_season_released_episodes(self, season_list, current_datetime):
        """Annotate seasons with the number of released episodes."""
        if not season_list:
            return

        season_keys = {
            (season.item.media_id, season.item.source, season.item.season_number)
            for season in season_list
        }
        media_ids = {media_id for media_id, _, _ in season_keys}
        media_sources = {source for _, source, _ in season_keys}
        season_numbers = {season_number for _, _, season_number in season_keys if season_number is not None}

        released_by_season: dict[tuple[str, str, int], int] = {}

        episode_rows = (
            Item.objects.filter(
                media_type=MediaTypes.EPISODE.value,
                media_id__in=media_ids,
                source__in=media_sources,
                season_number__in=season_numbers,
                release_datetime__isnull=False,
                release_datetime__lte=current_datetime,
            )
            .values("media_id", "source", "season_number")
            .annotate(max_episode=models.Max("episode_number"))
        )

        for row in episode_rows:
            key = (row["media_id"], row["source"], row["season_number"])
            max_episode = row["max_episode"] or 0
            released_by_season[key] = max(released_by_season.get(key, 0), max_episode)

        released_events = (
            events.models.Event.objects.filter(
                item__media_id__in=media_ids,
                item__source__in=media_sources,
                item__media_type=MediaTypes.SEASON.value,
                item__season_number__in=season_numbers,
                datetime__lte=current_datetime,
                content_number__isnull=False,
            )
            .exclude(datetime__year__lt=1900)
            .values(
                "item__media_id",
                "item__source",
                "item__season_number",
            )
            .annotate(max_episode=models.Max("content_number"))
        )

        for row in released_events:
            key = (row["item__media_id"], row["item__source"], row["item__season_number"])
            max_episode = row["max_episode"] or 0
            released_by_season[key] = max(released_by_season.get(key, 0), max_episode)

        for season in season_list:
            key = (season.item.media_id, season.item.source, season.item.season_number)
            season.max_progress = released_by_season.get(key)
            if (
                season.max_progress is None
                and season.item.source == Sources.MANUAL.value
            ):
                from app import custom_metadata  # noqa: PLC0415

                details = custom_metadata.build_manual_detail_payload(season.item)
                season.max_progress = custom_metadata.manual_max_progress(
                    season.item,
                    details,
                    fallback_max_progress=None,
                )

    def fetch_media_for_items(self, media_types, item_ids, user, status_filter=None):
        """Fetch media objects for given items, optionally filtering by status.

        Args:
            media_types: Iterable of media type strings to query
            item_ids: QuerySet or list of item IDs to fetch media for
            user: User to filter media by
            status_filter: Optional status value to filter by

        Returns:
            dict mapping item_id to media object
        """
        media_by_item_id = {}

        for media_type in media_types:
            model = apps.get_model("app", media_type)

            if media_type == MediaTypes.EPISODE.value:
                filter_kwargs = {
                    "item__in": item_ids,
                    "related_season__user": user,
                }
                if status_filter:
                    filter_kwargs["related_season__status"] = status_filter
            else:
                filter_kwargs = {
                    "item__in": item_ids,
                    "user": user,
                }
                if status_filter:
                    filter_kwargs["status"] = status_filter

            queryset = model.objects.filter(**filter_kwargs).select_related("item")
            queryset = self._apply_prefetch_related(queryset, media_type)
            self.annotate_max_progress(queryset, media_type)

            for entry in queryset:
                media_by_item_id.setdefault(entry.item_id, entry)

        return media_by_item_id

    def get_media(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get user media object given the media type and item."""
        if media_type == MediaTypes.ANIME.value:
            Anime = apps.get_model("app", "Anime")
            TV = apps.get_model("app", "TV")
            try:
                return Anime.objects.get(id=instance_id, user=user)
            except Anime.DoesNotExist:
                return TV.objects.get(
                    id=instance_id,
                    user=user,
                    item__library_media_type=MediaTypes.ANIME.value,
                )

        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._get_media_params(
            user,
            media_type,
            instance_id,
        )

        return model.objects.get(**params)

    def get_media_prefetch(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get user media object with prefetch_related applied."""
        if media_type == MediaTypes.ANIME.value:
            Anime = apps.get_model("app", "Anime")
            TV = apps.get_model("app", "TV")
            anime_queryset = Anime.objects.filter(id=instance_id, user=user)
            if anime_queryset.exists():
                anime_queryset = self._apply_prefetch_related(
                    anime_queryset,
                    media_type,
                )
                self.annotate_max_progress(anime_queryset, media_type)
                return anime_queryset[0]

            tv_queryset = TV.objects.filter(
                id=instance_id,
                user=user,
                item__library_media_type=MediaTypes.ANIME.value,
            )
            tv_queryset = self._apply_prefetch_related(tv_queryset, media_type)
            self.annotate_max_progress(tv_queryset, media_type)
            return tv_queryset[0]

        model = apps.get_model(app_label="app", model_name=media_type)
        params = self._get_media_params(
            user,
            media_type,
            instance_id,
        )

        queryset = model.objects.filter(**params)

        queryset = self._apply_prefetch_related(queryset, media_type)
        self.annotate_max_progress(queryset, media_type)

        return queryset[0]

    def _get_media_params(
        self,
        user,
        media_type,
        instance_id,
    ):
        """Get the common filter parameters for media queries."""
        params = {"id": instance_id}

        if media_type == MediaTypes.EPISODE.value:
            params["related_season__user"] = user
        else:
            params["user"] = user

        return params

    def filter_media(
        self,
        user,
        media_id,
        media_type,
        source,
        season_number=None,
        episode_number=None,
    ):
        """Filter media objects based on parameters."""
        if media_type == MediaTypes.ANIME.value and source in {
            Sources.TMDB.value,
            Sources.TVDB.value,
        }:
            model = apps.get_model("app", "TV")
        else:
            model = apps.get_model(app_label="app", model_name=media_type)
        params = self._filter_media_params(
            media_type,
            media_id,
            source,
            user,
            season_number,
            episode_number,
        )

        return model.objects.filter(**params)

    def filter_media_prefetch(
        self,
        user,
        media_id,
        media_type,
        source,
        season_number=None,
        episode_number=None,
    ):
        """Filter user media object with prefetch_related applied."""
        queryset = self.filter_media(
            user,
            media_id,
            media_type,
            source,
            season_number,
            episode_number,
        )
        queryset = self._apply_prefetch_related(queryset, media_type)
        queryset = queryset.select_related("item")
        self.annotate_max_progress(queryset, media_type)

        return queryset

    def _filter_media_params(
        self,
        media_type,
        media_id,
        source,
        user,
        season_number=None,
        episode_number=None,
    ):
        """Get the common filter parameters for media queries."""
        params = {
            "item__media_type": media_type,
            "item__source": source,
            "item__media_id": media_id,
        }

        if media_type == MediaTypes.ANIME.value and source in {
            Sources.TMDB.value,
            Sources.TVDB.value,
        }:
            params["item__media_type"] = MediaTypes.TV.value
            params["item__library_media_type"] = MediaTypes.ANIME.value

        if media_type == MediaTypes.SEASON.value:
            params["item__season_number"] = season_number
            params["user"] = user
        elif media_type == MediaTypes.EPISODE.value:
            params["item__season_number"] = season_number
            params["item__episode_number"] = episode_number
            params["related_season__user"] = user
        else:
            params["user"] = user

        return params

