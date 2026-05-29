import logging
import re
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db.utils import OperationalError
from django.http import JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.timezone import datetime
from django.views.decorators.http import require_GET, require_POST

from app import config, statistics_cache
from app import statistics as stats
from app.models import MediaTypes
from app.templatetags import app_tags
from users.models import (
    DateFormatChoices,
    StatisticsCompareChoices,
    TopTalentSortChoices,
)

DATE_FORMAT_DJANGO_MAP = {
    DateFormatChoices.SYSTEM_DEFAULT: "",
    DateFormatChoices.ISO_8601: "Y-m-d",
    DateFormatChoices.MONTH_D_YYYY: "M d, Y",
    DateFormatChoices.D_MON_YYYY: "d M Y",
    DateFormatChoices.M_D_YYYY: "n/j/Y",
    DateFormatChoices.D_M_YYYY: "j/n/Y",
    DateFormatChoices.DD_MM_YYYY: "d.m.Y",
    DateFormatChoices.YYYY_MM_DD: "Y/m/d",
    DateFormatChoices.LONG_EU: "j M, Y",
}

logger = logging.getLogger(__name__)

STATISTICS_COMPARE_PREVIOUS_PERIOD = "previous_period"
STATISTICS_COMPARE_LAST_YEAR = "last_year"
STATISTICS_COMPARE_NONE = "none"
STATISTICS_COMPARE_LABELS = {
    STATISTICS_COMPARE_PREVIOUS_PERIOD: "Previous period",
    STATISTICS_COMPARE_LAST_YEAR: "Last year",
    STATISTICS_COMPARE_NONE: "No comparison",
}
STATISTICS_CARD_LAST_YEAR_LABELS = {
    "This Year": "last year",
    "This Month": "last year",
}
_STATISTICS_HOURS_DISPLAY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)h\s+(\d+(?:\.\d+)?)min\s*$")


def _statistics_day_boundary(day_value: date | None, *, end_of_day: bool = False):
    if day_value is None:
        return None
    boundary_time = datetime.max.time() if end_of_day else datetime.min.time()
    return timezone.make_aware(
        datetime.combine(day_value, boundary_time),
        timezone.get_current_timezone(),
    )


def _format_statistics_range_label(user, start_date, end_date):
    if not start_date or not end_date:
        return "All Time"

    local_start = timezone.localdate(start_date)
    local_end = timezone.localdate(end_date)
    start_label = app_tags.date_format(_statistics_day_boundary(local_start), user)
    end_label = app_tags.date_format(_statistics_day_boundary(local_end), user)
    if local_start == local_end:
        return start_label
    return f"{start_label} - {end_label}"


def _get_statistics_card_range_label(user, selected_range_name, start_date, end_date):
    if selected_range_name:
        return selected_range_name
    return _format_statistics_range_label(user, start_date, end_date)


def _get_statistics_card_comparison_suffix(
    user,
    selected_range_name,
    compare_mode,
    comparison_start_date,
    comparison_end_date,
):
    if not comparison_start_date or not comparison_end_date:
        return ""

    if compare_mode == STATISTICS_COMPARE_LAST_YEAR:
        card_label = STATISTICS_CARD_LAST_YEAR_LABELS.get(selected_range_name)
        if card_label:
            return card_label

    return f"in {_format_statistics_range_label(user, comparison_start_date, comparison_end_date)}"


def _get_statistics_card_tooltip_labels(selected_range_name, compare_mode):
    current_label = (selected_range_name or "Current period").title()

    if compare_mode == STATISTICS_COMPARE_LAST_YEAR:
        comparison_label = STATISTICS_CARD_LAST_YEAR_LABELS.get(
            selected_range_name,
            STATISTICS_COMPARE_LABELS[compare_mode],
        )
    else:
        comparison_label = STATISTICS_COMPARE_LABELS.get(compare_mode, "")

    return current_label, comparison_label.title()


def _normalize_statistics_compare_mode(compare_mode, *, finite_range: bool):
    if not finite_range:
        return STATISTICS_COMPARE_NONE
    if compare_mode in STATISTICS_COMPARE_LABELS:
        return compare_mode
    return STATISTICS_COMPARE_PREVIOUS_PERIOD


def _resolve_statistics_comparison_range(start_date, end_date, compare_mode):
    if compare_mode == STATISTICS_COMPARE_NONE or not start_date or not end_date:
        return None, None

    local_start = timezone.localdate(start_date)
    local_end = timezone.localdate(end_date)
    if local_start > local_end:
        local_start, local_end = local_end, local_start

    if compare_mode == STATISTICS_COMPARE_PREVIOUS_PERIOD:
        duration_days = (local_end - local_start).days + 1
        compare_end = local_start - timedelta(days=1)
        compare_start = compare_end - timedelta(days=duration_days - 1)
    elif compare_mode == STATISTICS_COMPARE_LAST_YEAR:
        compare_start = local_start - relativedelta(years=1)
        compare_end = local_end - relativedelta(years=1)
    else:
        return None, None

    return (
        _statistics_day_boundary(compare_start),
        _statistics_day_boundary(compare_end, end_of_day=True),
    )


def _resolve_statistics_range_inputs(range_name, start_date_str, end_date_str):
    """Resolve statistics UI date inputs into aware datetimes."""
    if range_name in statistics_cache.PREDEFINED_RANGES:
        return statistics_cache._get_predefined_range_dates(range_name)

    if start_date_str == "all" and end_date_str == "all":
        return None, None

    start_date = None
    end_date = None
    if start_date_str and start_date_str != "all":
        start_date = _statistics_day_boundary(parse_date(start_date_str))
    if end_date_str and end_date_str != "all":
        end_date = _statistics_day_boundary(parse_date(end_date_str), end_of_day=True)

    return start_date, end_date


def _parse_statistics_total_display_to_minutes(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return 0.0

    cleaned = value.strip()
    if "play" in cleaned:
        try:
            return float(cleaned.split()[0])
        except (IndexError, ValueError):
            return 0.0

    match = _STATISTICS_HOURS_DISPLAY_RE.match(cleaned)
    if not match:
        return 0.0

    try:
        hours = float(match.group(1))
        minutes = float(match.group(2))
    except ValueError:
        return 0.0

    return (hours * 60) + minutes


def _get_statistics_minutes_by_type(statistics_data):
    if not isinstance(statistics_data, dict):
        return {}

    raw_minutes = statistics_data.get("minutes_per_media_type")
    if isinstance(raw_minutes, dict):
        return {
            media_type: float(total or 0)
            for media_type, total in raw_minutes.items()
        }

    return {
        media_type: _parse_statistics_total_display_to_minutes(total)
        for media_type, total in (statistics_data.get("hours_per_media_type") or {}).items()
    }


def _format_statistics_total_for_media_type(media_type, total):
    if media_type == MediaTypes.BOARDGAME.value:
        rounded_total = int(round(total or 0))
        return f"{rounded_total} play{'s' if rounded_total != 1 else ''}"
    return stats._format_hours_minutes(total or 0)


def _format_statistics_percent_change(delta_percent):
    formatted = f"{round(abs(float(delta_percent)), 1):.1f}"
    return formatted.rstrip("0").rstrip(".")


def _build_hours_per_media_type_comparison(
    current_minutes_by_type,
    comparison_minutes_by_type,
    compare_mode,
    comparison_suffix,
    current_period_label,
    comparison_period_label,
):
    if compare_mode == STATISTICS_COMPARE_NONE:
        return {
            media_type: {
                "badge": "",
                "badge_state": "none",
                "badge_short": "",
                "badge_classes": "",
                "details": "No comparison selected",
                "details_classes": "text-gray-500",
                "tooltip": None,
            }
            for media_type in current_minutes_by_type
        }

    media_types = list(dict.fromkeys([*stats.MEDIA_TYPE_HOURS_ORDER, *current_minutes_by_type.keys()]))
    comparisons = {}

    for media_type in media_types:
        current_total = float(current_minutes_by_type.get(media_type, 0) or 0)
        if current_total <= 0:
            continue

        current_display = _format_statistics_total_for_media_type(media_type, current_total)
        previous_total = float(comparison_minutes_by_type.get(media_type, 0) or 0)
        previous_display = _format_statistics_total_for_media_type(media_type, previous_total)
        tooltip = {
            "current_label": current_period_label,
            "current_total": current_display,
            "comparison_label": comparison_period_label,
            "comparison_total": previous_display,
        }

        if previous_total <= 0:
            comparisons[media_type] = {
                "badge": "New",
                "badge_state": "new",
                "badge_short": "New",
                "badge_classes": "stats-metric-delta-badge stats-metric-delta-badge-positive",
                "details": f"No activity {comparison_suffix}".strip(),
                "details_classes": "text-gray-400",
                "tooltip": tooltip,
            }
            continue

        delta_percent = ((current_total - previous_total) / previous_total) * 100

        if abs(delta_percent) < 0.05:
            comparisons[media_type] = {
                "badge": "No change",
                "badge_state": "neutral",
                "badge_short": "No change",
                "badge_classes": "stats-metric-delta-badge stats-metric-delta-badge-neutral",
                "details": f"vs {previous_display} {comparison_suffix}".strip(),
                "details_classes": "text-gray-400",
                "tooltip": tooltip,
            }
            continue

        is_positive = delta_percent > 0
        direction = "Up" if is_positive else "Down"
        tone_class = (
            "stats-metric-delta-badge stats-metric-delta-badge-positive"
            if is_positive
            else "stats-metric-delta-badge stats-metric-delta-badge-negative"
        )
        comparisons[media_type] = {
            "badge": f"{direction} {_format_statistics_percent_change(delta_percent)}%",
            "badge_state": direction.lower(),
            "badge_short": f"{_format_statistics_percent_change(delta_percent)}%",
            "badge_classes": tone_class,
            "details": f"vs {previous_display} {comparison_suffix}".strip(),
            "details_classes": "text-gray-400",
            "tooltip": tooltip,
        }

    return comparisons


@require_GET
def statistics(request):
    """Return the statistics page."""
    try:
        timeformat = "%Y-%m-%d"
        today = timezone.localdate()
        one_year_ago = today.replace(year=today.year - 1)

        start_date_param = request.GET.get("start-date")
        end_date_param = request.GET.get("end-date")

        if not start_date_param and not end_date_param:
            preferred_range = getattr(request.user, "statistics_default_range", None)
            if preferred_range not in statistics_cache.PREDEFINED_RANGES:
                preferred_range = "Last 12 Months"
            preferred_start, preferred_end = _get_predefined_range_date_strings(
                preferred_range,
                today,
                timeformat,
            )
            if preferred_start and preferred_end:
                start_date_str = preferred_start
                end_date_str = preferred_end
            else:
                start_date_str = one_year_ago.strftime(timeformat)
                end_date_str = today.strftime(timeformat)
        else:
            start_date_str = start_date_param or one_year_ago.strftime(timeformat)
            end_date_str = end_date_param or today.strftime(timeformat)

        if start_date_str == "all" and end_date_str == "all":
            start_date = None
            end_date = None
        else:
            start_date = parse_date(start_date_str)
            end_date = parse_date(end_date_str)

            if start_date and end_date:
                start_date = timezone.make_aware(
                    datetime.combine(start_date, datetime.min.time()),
                    timezone.get_current_timezone(),
                )
                end_date = timezone.make_aware(
                    datetime.combine(end_date, datetime.max.time()),
                    timezone.get_current_timezone(),
                )

        selected_range_name = _identify_predefined_range(start_date, end_date)

        if selected_range_name in statistics_cache.PREDEFINED_RANGES:
            request.user.update_preference("statistics_default_range", selected_range_name)

        statistics_data = statistics_cache.get_statistics_data(
            request.user,
            start_date,
            end_date,
            range_name=selected_range_name,
        )

        show_year_charts = selected_range_name in (None, "All Time")
        has_finite_range = start_date is not None and end_date is not None
        compare_mode_param = request.GET.get("compare")
        compare_mode_source = (
            compare_mode_param
            if compare_mode_param is not None
            else getattr(
                request.user,
                "statistics_compare_mode",
                StatisticsCompareChoices.PREVIOUS_PERIOD,
            )
        )
        selected_compare_mode = _normalize_statistics_compare_mode(
            compare_mode_source,
            finite_range=has_finite_range,
        )
        comparison_start_date, comparison_end_date = _resolve_statistics_comparison_range(
            start_date,
            end_date,
            selected_compare_mode,
        )
        comparison_range_name = _identify_predefined_range(
            comparison_start_date,
            comparison_end_date,
        )
        comparison_minutes_by_type = {}
        if comparison_start_date and comparison_end_date:
            comparison_minutes_by_type = statistics_cache.get_statistics_minutes_by_type(
                request.user,
                comparison_start_date,
                comparison_end_date,
                range_name=comparison_range_name,
            )

        selected_range_dates_label = _get_statistics_card_range_label(
            request.user,
            selected_range_name,
            start_date,
            end_date,
        )
        comparison_range_dates_label = _format_statistics_range_label(
            request.user,
            comparison_start_date,
            comparison_end_date,
        )
        comparison_card_suffix = _get_statistics_card_comparison_suffix(
            request.user,
            selected_range_name,
            selected_compare_mode,
            comparison_start_date,
            comparison_end_date,
        )
        current_tooltip_label, comparison_tooltip_label = _get_statistics_card_tooltip_labels(
            selected_range_name,
            selected_compare_mode,
        )
        hours_per_media_type_comparison = _build_hours_per_media_type_comparison(
            _get_statistics_minutes_by_type(statistics_data),
            comparison_minutes_by_type,
            selected_compare_mode,
            comparison_card_suffix,
            current_tooltip_label,
            comparison_tooltip_label,
        )

        top_rated_by_type = statistics_data.get("top_rated_by_type", {})
        top_rated_movie = top_rated_by_type.get("movie", [])
        top_rated_tv = top_rated_by_type.get("tv", [])
        top_rated_book = top_rated_by_type.get("book", [])
        top_rated_comic = top_rated_by_type.get("comic", [])
        top_rated_manga = top_rated_by_type.get("manga", [])

        start_date_str_for_url = start_date_str if start_date_str else ""
        end_date_str_for_url = end_date_str if end_date_str else ""

        context = {
            "user": request.user,
            "start_date": start_date,
            "end_date": end_date,
            "start_date_str": start_date_str_for_url,
            "end_date_str": end_date_str_for_url,
            "selected_range_name": selected_range_name,
            "selected_range_dates_label": selected_range_dates_label,
            "selected_compare_mode": selected_compare_mode,
            "selected_compare_label": STATISTICS_COMPARE_LABELS[selected_compare_mode],
            "comparison_range_dates_label": comparison_range_dates_label,
            "hours_per_media_type_comparison": hours_per_media_type_comparison,
            "media_count": statistics_data["media_count"],
            "activity_data": statistics_data["activity_data"],
            "media_type_distribution": statistics_data["media_type_distribution"],
            "score_distribution": statistics_data["score_distribution"],
            "top_rated": statistics_data["top_rated"],
            "top_rated_movie": top_rated_movie,
            "top_rated_tv": top_rated_tv,
            "top_rated_book": top_rated_book,
            "top_rated_comic": top_rated_comic,
            "top_rated_manga": top_rated_manga,
            "top_played": statistics_data["top_played"],
            "top_talent": statistics_data.get("top_talent", {}),
            "status_distribution": statistics_data["status_distribution"],
            "status_pie_chart_data": statistics_data["status_pie_chart_data"],
            "hours_per_media_type": statistics_data["hours_per_media_type"],
            "tv_consumption": statistics_data["tv_consumption"],
            "movie_consumption": statistics_data["movie_consumption"],
            "music_consumption": statistics_data["music_consumption"],
            "podcast_consumption": statistics_data["podcast_consumption"],
            "game_consumption": statistics_data["game_consumption"],
            "book_consumption": statistics_data.get("book_consumption", {}),
            "comic_consumption": statistics_data.get("comic_consumption", {}),
            "manga_consumption": statistics_data.get("manga_consumption", {}),
            "daily_hours_by_media_type": statistics_data["daily_hours_by_media_type"],
            "history_highlights": statistics_data.get("history_highlights", {}),
            "show_year_charts": show_year_charts,
            "user_django_date_format": DATE_FORMAT_DJANGO_MAP.get(
                request.user.date_format, ""
            ),
            "date_format_values": [
                fmt for fmt in DATE_FORMAT_DJANGO_MAP.values() if fmt
            ],
            "media_type_colors": {
                "tv": config.get_stats_color(MediaTypes.TV.value),
                "movie": config.get_stats_color(MediaTypes.MOVIE.value),
                "game": config.get_stats_color(MediaTypes.GAME.value),
                "music": config.get_stats_color(MediaTypes.MUSIC.value),
                "podcast": config.get_stats_color(MediaTypes.PODCAST.value),
            },
        }

        return render(request, "app/statistics.html", context)
    except OperationalError as error:
        logger.error("Database error in statistics view: %s", error, exc_info=True)
        timeformat = "%Y-%m-%d"
        today = timezone.localdate()
        one_year_ago = today.replace(year=today.year - 1)
        start_date_str = request.GET.get("start-date") or one_year_ago.strftime(timeformat)
        end_date_str = request.GET.get("end-date") or today.strftime(timeformat)

        empty_statistics_data = {
            "media_count": {},
            "activity_data": [],
            "media_type_distribution": {},
            "minutes_per_media_type": {},
            "hours_per_media_type": {},
            "media_type_colors": {
                "tv": config.get_stats_color(MediaTypes.TV.value),
                "movie": config.get_stats_color(MediaTypes.MOVIE.value),
                "game": config.get_stats_color(MediaTypes.GAME.value),
                "music": config.get_stats_color(MediaTypes.MUSIC.value),
                "podcast": config.get_stats_color(MediaTypes.PODCAST.value),
            },
            "score_distribution": {},
            "top_rated": [],
            "top_played": [],
            "top_talent": {},
            "status_distribution": {},
            "status_pie_chart_data": {},
            "hours_per_media_type": {},
            "tv_consumption": {},
            "movie_consumption": {},
            "music_consumption": {},
            "podcast_consumption": {},
            "game_consumption": {},
            "book_consumption": {},
            "comic_consumption": {},
            "manga_consumption": {},
            "daily_hours_by_media_type": {},
            "history_highlights": {},
        }

        error_start_date = (
            _statistics_day_boundary(parse_date(start_date_str))
            if start_date_str != "all"
            else None
        )
        error_end_date = (
            _statistics_day_boundary(parse_date(end_date_str), end_of_day=True)
            if end_date_str != "all"
            else None
        )
        has_finite_range = error_start_date is not None and error_end_date is not None
        compare_mode_param = request.GET.get("compare")
        compare_mode_source = (
            compare_mode_param
            if compare_mode_param is not None
            else getattr(
                request.user,
                "statistics_compare_mode",
                StatisticsCompareChoices.PREVIOUS_PERIOD,
            )
        )
        selected_compare_mode = _normalize_statistics_compare_mode(
            compare_mode_source,
            finite_range=has_finite_range,
        )
        selected_range_name = _identify_predefined_range(error_start_date, error_end_date)

        context = {
            "user": request.user,
            "start_date": parse_date(start_date_str) if start_date_str != "all" else None,
            "end_date": parse_date(end_date_str) if end_date_str != "all" else None,
            "start_date_str": start_date_str,
            "end_date_str": end_date_str,
            "selected_range_name": selected_range_name,
            "selected_range_dates_label": _get_statistics_card_range_label(
                request.user,
                selected_range_name,
                error_start_date,
                error_end_date,
            ),
            "selected_compare_mode": selected_compare_mode,
            "selected_compare_label": STATISTICS_COMPARE_LABELS[selected_compare_mode],
            "comparison_range_dates_label": "",
            "hours_per_media_type_comparison": {},
            "media_count": empty_statistics_data["media_count"],
            "activity_data": empty_statistics_data["activity_data"],
            "media_type_distribution": empty_statistics_data["media_type_distribution"],
            "score_distribution": empty_statistics_data["score_distribution"],
            "top_rated": empty_statistics_data["top_rated"],
            "top_rated_movie": [],
            "top_rated_tv": [],
            "top_rated_book": [],
            "top_rated_comic": [],
            "top_rated_manga": [],
            "top_played": empty_statistics_data["top_played"],
            "top_talent": empty_statistics_data["top_talent"],
            "status_distribution": empty_statistics_data["status_distribution"],
            "status_pie_chart_data": empty_statistics_data["status_pie_chart_data"],
            "hours_per_media_type": empty_statistics_data["hours_per_media_type"],
            "tv_consumption": empty_statistics_data["tv_consumption"],
            "movie_consumption": empty_statistics_data["movie_consumption"],
            "music_consumption": empty_statistics_data["music_consumption"],
            "podcast_consumption": empty_statistics_data["podcast_consumption"],
            "game_consumption": empty_statistics_data["game_consumption"],
            "book_consumption": empty_statistics_data["book_consumption"],
            "comic_consumption": empty_statistics_data["comic_consumption"],
            "manga_consumption": empty_statistics_data["manga_consumption"],
            "daily_hours_by_media_type": empty_statistics_data["daily_hours_by_media_type"],
            "history_highlights": empty_statistics_data["history_highlights"],
            "media_type_colors": empty_statistics_data["media_type_colors"],
            "show_year_charts": False,
            "database_error": True,
        }
        return render(request, "app/statistics.html", context)


@require_POST
def refresh_statistics(request):
    """Force refresh statistics cache for the current range."""
    range_name = request.POST.get("range_name")
    if not range_name:
        return JsonResponse({"error": "range_name is required"}, status=400)

    if range_name not in statistics_cache.PREDEFINED_RANGES:
        return JsonResponse({"error": "Invalid range_name"}, status=400)

    statistics_cache.invalidate_statistics_cache(request.user.id, range_name)
    statistics_cache.schedule_statistics_refresh(
        request.user.id,
        range_name,
        debounce_seconds=0,
        countdown=0,
        allow_inline=True,
    )

    return JsonResponse({"success": True, "message": "Statistics refresh scheduled"})


@require_POST
def update_top_talent_sort(request):
    """Autosave top talent sort preference from statistics page controls."""
    sort_by = request.POST.get("sort_by")
    range_name = request.POST.get("range_name")
    start_date_str = request.POST.get("start_date")
    end_date_str = request.POST.get("end_date")

    valid_sort_values = list(TopTalentSortChoices.values)
    if sort_by not in valid_sort_values:
        return JsonResponse(
            {
                "error": "Invalid sort_by",
                "valid_values": valid_sort_values,
            },
            status=400,
        )

    previous_sort = request.user.top_talent_sort_by
    updated_sort = request.user.update_preference("top_talent_sort_by", sort_by)
    changed = previous_sort != updated_sort
    requires_reload = False
    grid_html = ""

    if range_name in statistics_cache.PREDEFINED_RANGES:
        try:
            if statistics_cache.range_needs_top_talent_upgrade(request.user.id, range_name):
                statistics_cache.invalidate_statistics_cache(request.user.id, range_name)
                statistics_cache.refresh_statistics_cache(request.user.id, range_name)
                requires_reload = True
        except Exception as exc:  # pragma: no cover - best effort compatibility upgrade
            logger.debug(
                "top_talent_sort_upgrade_failed user_id=%s range=%s error=%s",
                request.user.id,
                range_name,
                exc,
            )

    if not requires_reload:
        start_date, end_date = _resolve_statistics_range_inputs(
            range_name,
            start_date_str,
            end_date_str,
        )
        top_talent = statistics_cache.get_top_talent_data(
            request.user,
            start_date,
            end_date,
            range_name=range_name,
        )
        selected_talent = top_talent
        by_sort = top_talent.get("by_sort") if isinstance(top_talent, dict) else None
        if isinstance(by_sort, dict):
            selected_talent = by_sort.get(updated_sort) or {}

        grid_html = render_to_string(
            "app/components/top_talent_grid.html",
            {
                "talent": selected_talent,
                "talent_sort": updated_sort,
                "IMG_NONE": settings.IMG_NONE,
            },
            request=request,
        )

    return JsonResponse(
        {
            "success": True,
            "sort_by": updated_sort,
            "changed": changed,
            "requires_reload": requires_reload,
            "grid_html": grid_html,
        },
    )


@require_POST
def update_statistics_compare_mode(request):
    """Autosave statistics comparison mode preference from page controls."""
    compare_mode = request.POST.get("compare_mode")

    valid_compare_values = list(StatisticsCompareChoices.values)
    if compare_mode not in valid_compare_values:
        return JsonResponse(
            {
                "error": "Invalid compare_mode",
                "valid_values": valid_compare_values,
            },
            status=400,
        )

    previous_mode = request.user.statistics_compare_mode
    updated_mode = request.user.update_preference("statistics_compare_mode", compare_mode)

    return JsonResponse(
        {
            "success": True,
            "changed": previous_mode != updated_mode,
            "compare_mode": updated_mode,
        },
    )


def _identify_predefined_range(start_date, end_date):
    if start_date is None and end_date is None:
        return "All Time"

    if not start_date or not end_date:
        return None

    local_start = timezone.localdate(start_date)
    local_end = timezone.localdate(end_date)
    today = timezone.localdate()

    if local_start == today and local_end == today:
        return "Today"

    yesterday = today - timedelta(days=1)
    if local_start == yesterday and local_end == yesterday:
        return "Yesterday"

    month_start = today.replace(day=1)
    if local_start == month_start and local_end == today:
        return "This Month"
    if local_start == month_start and local_end == today - timedelta(days=1):
        return "This Month"

    monday = today - timedelta(days=today.weekday())
    if local_start == monday and local_end == today:
        return "This Week"
    if local_start == monday and local_end == today - timedelta(days=1):
        return "This Week"

    if local_start == today - timedelta(days=6) and local_end == today:
        return "Last 7 Days"

    if local_start == today - timedelta(days=29) and local_end == today:
        return "Last 30 Days"

    if local_start == today - timedelta(days=89) and local_end == today:
        return "Last 90 Days"

    year_start = today.replace(month=1, day=1)
    if local_start == year_start and local_end == today:
        return "This Year"
    if local_start == year_start and local_end == today - timedelta(days=1):
        return "This Year"

    six_months_start = _adjust_month_delta(today, months=6)
    if _dates_close(local_start, six_months_start) and local_end == today:
        return "Last 6 Months"

    twelve_months_start = _adjust_month_delta(today, months=12)
    if _dates_close(local_start, twelve_months_start) and local_end == today:
        return "Last 12 Months"

    return None


def _get_predefined_range_date_strings(range_name, today, timeformat):
    if range_name == "All Time":
        return "all", "all"

    start_date = None
    end_date = today

    if range_name == "Today":
        start_date = today
    elif range_name == "Yesterday":
        start_date = today - timedelta(days=1)
        end_date = start_date
    elif range_name == "This Week":
        start_date = today - timedelta(days=today.weekday())
    elif range_name == "Last 7 Days":
        start_date = today - timedelta(days=6)
    elif range_name == "This Month":
        start_date = today.replace(day=1)
    elif range_name == "Last 30 Days":
        start_date = today - timedelta(days=29)
    elif range_name == "Last 90 Days":
        start_date = today - timedelta(days=89)
    elif range_name == "This Year":
        start_date = today.replace(month=1, day=1)
    elif range_name == "Last 6 Months":
        start_date = _adjust_month_delta(today, months=6)
    elif range_name == "Last 12 Months":
        start_date = _adjust_month_delta(today, months=12)

    if start_date is None:
        return None, None

    return start_date.strftime(timeformat), end_date.strftime(timeformat)


def _adjust_month_delta(reference_date, months):
    candidate = reference_date - relativedelta(months=months)
    if candidate.day != reference_date.day:
        candidate = (candidate.replace(day=1) + relativedelta(months=1)) - timedelta(days=1)
    return candidate


def _dates_close(date_one, date_two, tolerance_days=1):
    return abs((date_one - date_two).days) <= tolerance_days
