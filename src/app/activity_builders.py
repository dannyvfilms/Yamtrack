import logging

from django.core.cache import cache
from django.core.paginator import EmptyPage, Paginator

from app import helpers
from app import statistics as stats
from app.models import BasicMedia, MediaTypes, Sources, Status
from app.services import game_lengths as game_length_services

logger = logging.getLogger(__name__)

DETAIL_EPISODES_PER_PAGE = 25


def _format_detail_activity_duration(total_minutes, suffix):
    """Return a detail subtitle duration string for a total-minute value."""
    if not total_minutes:
        return None

    total_minutes = int(total_minutes)
    total_hours, remainder_minutes = divmod(total_minutes, 60)
    if total_hours > 0:
        return f"{total_hours}h {remainder_minutes}min {suffix}"
    return f"{total_minutes}min {suffix}"


def _build_detail_activity_subtitle(media_type, media_metadata, current_instance=None, play_stats=None):
    """Return a shared subtitle payload for tracked detail pages."""
    if not current_instance and not play_stats:
        return None

    media_metadata = media_metadata if isinstance(media_metadata, dict) else {}
    play_stats = play_stats if isinstance(play_stats, dict) else {}
    max_progress = media_metadata.get("max_progress")

    def build_progress_text(value, include_max=False):
        if value in (None, ""):
            return None
        progress_text = f"Progress: {value}"
        if include_max and max_progress:
            progress_text += f"/{max_progress}"
        return progress_text

    date_start = (
        play_stats.get("first_played")
        or getattr(current_instance, "subtitle_start_date", None)
        or getattr(current_instance, "aggregated_start_date", None)
        or getattr(current_instance, "start_date", None)
    )
    date_end = (
        play_stats.get("last_played")
        or getattr(current_instance, "subtitle_end_date", None)
        or getattr(current_instance, "aggregated_end_date", None)
        or getattr(current_instance, "end_date", None)
    )
    duration_text = None
    collapse_same_day = bool(play_stats.get("same_play_day"))

    if media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        primary_text = build_progress_text(
            getattr(current_instance, "formatted_progress", None),
            include_max=True,
        )
        duration_text = _format_detail_activity_duration(
            play_stats.get("total_minutes"),
            "watched",
        )
    elif media_type == MediaTypes.MOVIE.value:
        total_plays = play_stats.get("total_plays")
        if not total_plays:
            return None
        primary_text = "Watched once" if total_plays == 1 else f"Watched {total_plays} times"
        duration_text = _format_detail_activity_duration(
            play_stats.get("total_minutes"),
            "watched",
        )
    elif media_type in (
        MediaTypes.BOOK.value,
        MediaTypes.COMIC.value,
        MediaTypes.MANGA.value,
    ):
        primary_text = build_progress_text(
            getattr(current_instance, "formatted_progress", None),
            include_max=True,
        )
    elif media_type == MediaTypes.GAME.value:
        progress_value = (
            getattr(current_instance, "formatted_aggregated_progress", None)
            if getattr(current_instance, "aggregated_progress", None) is not None
            else getattr(current_instance, "formatted_progress", None)
        )
        primary_text = build_progress_text(progress_value)
    elif media_type in (MediaTypes.BOARDGAME.value, MediaTypes.MUSIC.value):
        progress_value = (
            getattr(current_instance, "formatted_aggregated_progress", None)
            if getattr(current_instance, "aggregated_progress", None) is not None
            else getattr(current_instance, "formatted_progress", None)
        )
        primary_text = build_progress_text(progress_value)
        if media_type == MediaTypes.MUSIC.value:
            duration_text = _format_detail_activity_duration(
                play_stats.get("total_minutes"),
                "listened",
            )
    elif media_type == MediaTypes.PODCAST.value:
        total_plays = play_stats.get("total_plays")
        if max_progress and total_plays:
            primary_text = f"Progress: {total_plays}/{max_progress}"
        else:
            primary_text = build_progress_text(
                getattr(current_instance, "formatted_progress", None),
            )
        duration_text = _format_detail_activity_duration(
            play_stats.get("total_minutes"),
            "listened",
        )
    else:
        return None

    if not primary_text and not date_start and not date_end and not duration_text:
        return None

    return {
        "primary_text": primary_text,
        "date_start": date_start,
        "date_end": date_end,
        "duration_text": duration_text,
        "collapse_same_day": collapse_same_day,
    }


def _build_detail_activity_state(
    media_type,
    media_metadata,
    current_instance=None,
    user_medias=None,
    public_view=False,
):
    """Return the activity subtitle payload for tracked detail pages."""
    play_stats = None
    activity_subtitle = None
    user_medias = list(user_medias or [])

    if (
        not public_view
        and current_instance
        and user_medias
        and media_type
        in [
            MediaTypes.BOOK.value,
            MediaTypes.COMIC.value,
            MediaTypes.GAME.value,
            MediaTypes.BOARDGAME.value,
            MediaTypes.ANIME.value,
            MediaTypes.MANGA.value,
            MediaTypes.MOVIE.value,
            MediaTypes.MUSIC.value,
            MediaTypes.PODCAST.value,
            MediaTypes.TV.value,
        ]
    ):
        if media_type == MediaTypes.TV.value or (
            media_type == MediaTypes.ANIME.value
            and hasattr(current_instance, "seasons")
        ):
            # Calculate TV and grouped-anime play stats from watched episodes.
            total_minutes = 0
            episode_count = 0
            first_played = None
            last_played = None

            # Iterate through all seasons and episodes
            seasons = current_instance.seasons.all().select_related("item").prefetch_related(
                "episodes__item",
            )
            for season in seasons:
                episodes = season.episodes.all().select_related("item")
                for episode in episodes:
                    # Only count episodes that have been watched (have end_date)
                    if not episode.end_date:
                        continue

                    # Get runtime for this episode
                    try:
                        runtime_minutes = stats._calculate_episode_time_from_cache(episode, logger)
                        if runtime_minutes > 0:
                            total_minutes += runtime_minutes
                            episode_count += 1

                            # Track first and last played dates
                            if first_played is None or episode.end_date < first_played:
                                first_played = episode.end_date
                            if last_played is None or episode.end_date > last_played:
                                last_played = episode.end_date
                    except (ValueError, AttributeError):
                        # Skip episodes without runtime data
                        continue

            # Only create play_stats if we have watched episodes
            if episode_count > 0:
                play_stats = {
                    "first_played": first_played,
                    "last_played": last_played,
                    "total_minutes": total_minutes,
                    "total_hours": total_minutes // 60,
                    "total_minutes_remainder": total_minutes % 60,
                    "episode_count": episode_count,
                }
        elif media_type == MediaTypes.ANIME.value:
            # Flat anime entries track episode progress directly on the media row.
            BasicMedia.objects._aggregate_item_data(current_instance, user_medias)
            aggregated_progress = getattr(current_instance, "aggregated_progress", None)
            if aggregated_progress is None:
                aggregated_progress = current_instance.progress or 0

            play_stats = {
                "first_played": getattr(current_instance, "aggregated_start_date", None)
                or current_instance.start_date,
                "last_played": getattr(current_instance, "aggregated_end_date", None)
                or current_instance.end_date,
            }
            current_instance.subtitle_start_date = play_stats["first_played"]
            current_instance.subtitle_end_date = play_stats["last_played"]

            runtime_minutes = current_instance._get_known_item_runtime_minutes()
            total_progress = int(aggregated_progress or 0)
            if runtime_minutes and total_progress > 0:
                total_minutes = runtime_minutes * total_progress
                play_stats.update(
                    {
                        "total_minutes": total_minutes,
                        "total_hours": total_minutes // 60,
                        "total_minutes_remainder": total_minutes % 60,
                    },
                )
        else:
            # Generic non-TV calculation based on aggregated item activity.
            BasicMedia.objects._aggregate_item_data(current_instance, user_medias)
            aggregated_progress = getattr(current_instance, "aggregated_progress", None)
            if aggregated_progress is None:
                aggregated_progress = current_instance.progress or 0

            play_stats = {
                "first_played": getattr(current_instance, "aggregated_start_date", None)
                or current_instance.start_date,
                "last_played": getattr(current_instance, "aggregated_end_date", None)
                or current_instance.end_date,
            }

            if media_type == MediaTypes.GAME.value:
                total_minutes = int(aggregated_progress or 0)
                play_stats.update(
                    {
                        "total_minutes": total_minutes,
                        "total_hours": total_minutes // 60,
                        "total_minutes_remainder": total_minutes % 60,
                    },
                )
                days_played = set()
                total_minutes_for_avg = 0
                for entry in user_medias:
                    entry_minutes = entry.progress or 0
                    if entry_minutes <= 0:
                        continue
                    total_minutes_for_avg += entry_minutes
                    days_played.update(stats._get_entry_play_dates(entry))
                total_days = len(days_played)
                if total_days:
                    avg_minutes = int(round(total_minutes_for_avg / total_days))
                else:
                    avg_minutes = 0
                play_stats["avg_time_per_day"] = helpers.minutes_to_hhmm(avg_minutes)
            elif media_type == MediaTypes.MOVIE.value:
                total_plays = int(aggregated_progress or 0)
                play_stats["total_plays"] = total_plays

                range_start_candidates = []
                range_end_candidates = []
                for entry in user_medias:
                    range_start = entry.start_date or entry.end_date or entry.created_at
                    range_end = entry.end_date or entry.start_date or entry.created_at
                    if range_start:
                        range_start_candidates.append(range_start)
                    if range_end:
                        range_end_candidates.append(range_end)

                if range_start_candidates:
                    play_stats["first_played"] = min(range_start_candidates)
                if range_end_candidates:
                    play_stats["last_played"] = max(range_end_candidates)

                first_played = play_stats.get("first_played")
                last_played = play_stats.get("last_played")
                if first_played and last_played:
                    first_played_local = stats._localize_datetime(first_played)
                    last_played_local = stats._localize_datetime(last_played)
                    if first_played_local and last_played_local:
                        play_stats["same_play_day"] = (
                            first_played_local.date() == last_played_local.date()
                        )

                runtime_minutes = current_instance._get_known_item_runtime_minutes()
                if runtime_minutes and total_plays > 0:
                    total_minutes = runtime_minutes * total_plays
                    play_stats.update(
                        {
                            "total_minutes": total_minutes,
                            "total_hours": total_minutes // 60,
                            "total_minutes_remainder": total_minutes % 60,
                        },
                    )
            elif media_type == MediaTypes.MUSIC.value:
                total_plays = int(aggregated_progress or 0)
                play_stats["total_plays"] = total_plays

                runtime_minutes = current_instance._get_known_item_runtime_minutes()
                if runtime_minutes and total_plays > 0:
                    total_minutes = runtime_minutes * total_plays
                    play_stats.update(
                        {
                            "total_minutes": total_minutes,
                            "total_hours": total_minutes // 60,
                            "total_minutes_remainder": total_minutes % 60,
                        },
                    )
            elif media_type == MediaTypes.PODCAST.value:
                total_progress_seconds = int(aggregated_progress or 0)
                total_minutes = total_progress_seconds // 60
                completed_entries = sum(
                    1
                    for entry in user_medias
                    if entry.end_date or entry.status == Status.COMPLETED.value
                )
                play_stats.update(
                    {
                        "total_minutes": total_minutes,
                        "total_hours": total_minutes // 60,
                        "total_minutes_remainder": total_minutes % 60,
                        "total_plays": completed_entries or len(user_medias),
                    },
                )
            else:
                play_stats["total_plays"] = int(aggregated_progress or 0)

        activity_subtitle = _build_detail_activity_subtitle(
            media_type,
            media_metadata,
            current_instance,
            play_stats,
        )

    return play_stats, activity_subtitle


def _detail_episode_number_for_pagination(episode):
    """Return a display-friendly episode number from a detail episode payload."""
    if isinstance(episode, dict):
        episode_number = episode.get("episode_number")
    else:
        episode_number = getattr(episode, "episode_number", None)

    try:
        return int(episode_number) if episode_number is not None else None
    except (TypeError, ValueError):
        return episode_number


def _detail_episode_page_label(page_episodes, start_index, end_index):
    """Return a human-readable label for an episode page range."""
    if page_episodes:
        first_episode_number = _detail_episode_number_for_pagination(page_episodes[0])
        last_episode_number = _detail_episode_number_for_pagination(page_episodes[-1])
        if first_episode_number is not None and last_episode_number is not None:
            if first_episode_number == last_episode_number:
                return f"Episode {first_episode_number}"
            return f"Episodes {first_episode_number}-{last_episode_number}"

    display_start = start_index + 1
    display_end = end_index
    if display_start == display_end:
        return f"Episode {display_start}"
    return f"Episodes {display_start}-{display_end}"


def _paginate_detail_episodes(
    request,
    episodes,
    *,
    page_param="episode_page",
    per_page=DETAIL_EPISODES_PER_PAGE,
):
    """Slice long episode lists for detail pages and build the next batch link."""
    episode_list = list(episodes or [])
    if not episode_list:
        return episode_list, None

    paginator = Paginator(episode_list, per_page)

    try:
        requested_page = int(request.GET.get(page_param, 1))
    except (TypeError, ValueError):
        requested_page = 1
    if requested_page < 1:
        requested_page = 1

    try:
        page_obj = paginator.page(requested_page)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    load_more = None
    if page_obj.has_next():
        next_page_number = page_obj.next_page_number()
        next_start_index = (next_page_number - 1) * per_page
        next_end_index = min(next_start_index + per_page, paginator.count)
        next_query = request.GET.copy()
        next_query[page_param] = str(next_page_number)
        load_more = {
            "querystring": next_query.urlencode(),
            "label": _detail_episode_page_label(
                episode_list[next_start_index:next_end_index],
                next_start_index,
                next_end_index,
            ),
        }

    return list(page_obj.object_list), load_more


def _normalize_detail_episode_actions(episodes):
    """Ensure detail-page episode dicts default to enabled actions unless disabled."""
    normalized_episodes = []
    for episode in episodes or []:
        if isinstance(episode, dict):
            normalized_episode = dict(episode)
            normalized_episode.setdefault("actions_enabled", True)
            normalized_episodes.append(normalized_episode)
            continue
        normalized_episodes.append(episode)
    return normalized_episodes


def _should_queue_game_lengths_refresh(detail_item):
    """Return whether a background game-length refresh should be queued."""
    if not detail_item:
        return False
    if detail_item.source != Sources.IGDB.value or detail_item.media_type != MediaTypes.GAME.value:
        return False
    if not detail_item.provider_game_lengths:
        return True
    return detail_item.provider_game_lengths_match == "igdb_fallback"


def _get_game_lengths_refresh_lock(detail_item, *, force=False, fetch_hltb=True):
    """Return an active game-length refresh lock, clearing stale or legacy values."""
    if not detail_item:
        return None

    lock_key = game_length_services.get_game_lengths_refresh_lock_key(
        detail_item.id,
        force=force,
        fetch_hltb=fetch_hltb,
    )
    refresh_lock = cache.get(lock_key)
    if refresh_lock is None:
        return None

    if refresh_lock is True or game_length_services.is_game_lengths_refresh_lock_stale(refresh_lock):
        cache.delete(lock_key)
        return None
    return refresh_lock


def _queue_game_lengths_refresh(detail_item, *, force=False, fetch_hltb=True):
    """Schedule a background game-length refresh once per debounce window."""
    if not detail_item:
        return False

    lock_key = game_length_services.get_game_lengths_refresh_lock_key(
        detail_item.id,
        force=force,
        fetch_hltb=fetch_hltb,
    )
    if _get_game_lengths_refresh_lock(detail_item, force=force, fetch_hltb=fetch_hltb) is not None:
        return False

    lock_payload = game_length_services.build_game_lengths_refresh_lock(
        force=force,
        fetch_hltb=fetch_hltb,
    )
    if not cache.add(
        lock_key,
        lock_payload,
        timeout=game_length_services.GAME_LENGTHS_REFRESH_TTL,
    ):
        if _get_game_lengths_refresh_lock(detail_item, force=force, fetch_hltb=fetch_hltb) is not None:
            return False
        if not cache.add(
            lock_key,
            lock_payload,
            timeout=game_length_services.GAME_LENGTHS_REFRESH_TTL,
        ):
            return False

    try:
        from app.tasks import refresh_item_game_lengths

        refresh_item_game_lengths.delay(
            detail_item.id,
            force=force,
            fetch_hltb=fetch_hltb,
        )
    except Exception:
        cache.delete(lock_key)
        logger.warning(
            "game_lengths_refresh_schedule_failed item_id=%s media_id=%s",
            detail_item.id,
            detail_item.media_id,
            exc_info=True,
        )
        return False
    return True


def _annotate_home_card_images(media_items):
    """Annotate home-card image overrides for media that need display fallbacks."""
    season_items = [
        media
        for media in media_items
        if getattr(getattr(media, "item", None), "media_type", None) == MediaTypes.SEASON.value
    ]
    if season_items:
        BasicMedia.objects._fix_missing_season_images(season_items)
