import calendar
import datetime
from collections import Counter, defaultdict

from django.apps import apps
from django.core.cache import cache
from django.db.models import Prefetch

from app import config
from app.models import CreditRoleType, ItemPersonCredit, MediaTypes, Status
from app.statistics_cache import STATISTICS_TOP_N


def _reading_entry_in_range(entry, start_date, end_date):
    """Return True if a reading entry overlaps the requested date range."""
    from app.statistics import _get_activity_datetime, _localize_datetime

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


def _build_reading_top_authors(item_units, unit_name, limit=STATISTICS_TOP_N):
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
    from app.statistics import _build_single_series_chart, _format_hour_label

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
    from app.statistics import (
        _build_completed_length_distribution_chart,
        _build_media_charts,
        _build_release_year_chart,
        _coerce_genre_list,
        _compute_metric_breakdown,
        _get_activity_datetime,
        _localize_datetime,
    )

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

    top_items = sorted(top_items, key=lambda item: item["units"], reverse=True)[:STATISTICS_TOP_N]

    top_genres = []
    for payload in sorted(
        genre_stats.values(),
        key=lambda item: (item["units"], len(item["title_ids"])),
        reverse=True,
    )[:STATISTICS_TOP_N]:
        top_genres.append(
            {
                "name": payload["name"],
                "units": payload["units"],
                "titles": len(payload["title_ids"]),
                "formatted_units": _format_reading_unit(payload["units"], unit_name),
            }
        )
    top_authors = _build_reading_top_authors(author_item_units, unit_name, limit=STATISTICS_TOP_N)

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
