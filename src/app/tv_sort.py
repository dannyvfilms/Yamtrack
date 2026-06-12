import logging

from django.core.cache import cache

from app.models import Item, MediaTypes, Status
from app.statistics import parse_runtime_to_minutes

logger = logging.getLogger(__name__)


def _sort_tv_media_by_time_left(media_list, direction="asc"):
    """Sort TV media by time left with explicit grouping order.

    Group order:
      1) Active (episodes_left > 0 for non-dropped statuses) by least total time left first
      2) In-Progress caught-up (episodes_left == 0) newest end_date first
      3) Completed (episodes_left == 0) newest end_date first
      4) Dropped (episodes_left may be 0 or > 0) newest end_date first
      5) Unreleased/unknown runtime at the very end
    """
    # Pre-load all episode runtimes for every show in one query so the inner
    # helpers can avoid per-show/per-season DB hits during the sort.
    _all_keys = {
        (m.item.media_id, m.item.source)
        for m in media_list
        if getattr(m, "item", None) is not None
    }
    _episode_runtime_index: dict[tuple, dict[int, dict[int, int]]] = {}
    if _all_keys:
        _all_media_ids = {mid for mid, _ in _all_keys}
        _all_sources = {src for _, src in _all_keys}
        _rows = Item.objects.filter(
            media_type=MediaTypes.EPISODE.value,
            media_id__in=_all_media_ids,
            source__in=_all_sources,
            runtime_minutes__isnull=False,
        ).exclude(runtime_minutes__in=[999998, 999999]).values(
            "media_id", "source", "season_number", "episode_number", "runtime_minutes",
        )
        for row in _rows:
            show_key = (row["media_id"], row["source"])
            _episode_runtime_index.setdefault(show_key, {}).setdefault(
                row["season_number"], {}
            )[row["episode_number"]] = row["runtime_minutes"]

    def _calc_unwatched_runtime_total(
        media,
        episodes_left_count,
        *,
        breakdown_override=None,
        progress_override=None,
    ):
        """Sum actual runtimes for unwatched episodes instead of using averages.

        Returns (total_runtime, episodes_with_data) or (None, 0) if no data available.
        """
        breakdown = (
            breakdown_override
            if breakdown_override is not None
            else getattr(media, "released_episode_breakdown", {})
        )
        if not breakdown:
            return None, 0

        total_runtime = 0
        episodes_with_runtime_data = 0
        remaining_progress = (
            media.progress if progress_override is None else progress_override
        )

        # Process seasons in order to determine which episodes are unwatched
        for season_num in sorted(breakdown.keys()):
            season_episode_count = breakdown[season_num]

            if remaining_progress >= season_episode_count:
                # User has watched all episodes in this season
                remaining_progress -= season_episode_count
            else:
                # User is partway through this season or hasn't started it
                watched_in_season = remaining_progress
                remaining_progress = 0

                show_key = (media.item.media_id, media.item.source)
                season_ep_runtimes = _episode_runtime_index.get(show_key, {}).get(season_num, {})
                runtimes = [
                    rt
                    for ep_num, rt in season_ep_runtimes.items()
                    if ep_num > watched_in_season
                ]
                if runtimes:
                    total_runtime += sum(runtimes)
                    episodes_with_runtime_data += len(runtimes)
                    logger.debug(
                        f"{media.item.title} S{season_num}: {len(runtimes)} unwatched eps "
                        f"(after ep {watched_in_season}), runtime sum={sum(runtimes)}min",
                    )

        if episodes_with_runtime_data > 0:
            return total_runtime, episodes_with_runtime_data
        return None, 0

    def _calc_runtime_minutes(media):
        """Best-effort average runtime in minutes for a TV show (fallback only)."""
        runtime_minutes = None
        # FIRST: Check locally stored runtime (but exclude fallback markers)
        if hasattr(media, "item") and media.item.runtime_minutes:
            # Exclude fallback values: 999998 (aired but runtime unknown) and 999999 (unknown runtime)
            if media.item.runtime_minutes < 999998:
                runtime_minutes = media.item.runtime_minutes
                logger.debug(f"Using stored runtime for {media.item.title}: {runtime_minutes}min")
            else:
                logger.debug(f"Skipping invalid runtime marker ({media.item.runtime_minutes}min) for {media.item.title}")

        if not runtime_minutes:
            # SECOND: Check pre-loaded episode runtime index (avoids per-show DB query)
            show_key = (media.item.media_id, media.item.source)
            all_ep_runtimes = [
                rt
                for season_data in _episode_runtime_index.get(show_key, {}).values()
                for rt in season_data.values()
            ]
            if all_ep_runtimes:
                runtime_minutes = round(sum(all_ep_runtimes) / len(all_ep_runtimes))
                logger.debug(
                    f"Using average episode runtime for {media.item.title}: "
                    f"{runtime_minutes}min (from {len(all_ep_runtimes)} episodes)",
                )

        if not runtime_minutes:
            # THIRD: Check cached season data (avg_runtime field from season metadata)
            season_cache_key = f"tmdb_season_{media.item.media_id}_1"
            cached_season_data = cache.get(season_cache_key)
            if cached_season_data and cached_season_data.get("details", {}).get("runtime"):
                runtime_str = cached_season_data["details"]["runtime"]
                runtime_minutes = parse_runtime_to_minutes(runtime_str)
                if runtime_minutes and runtime_minutes > 0:
                    logger.debug(f"Using cached season avg runtime for {media.item.title}: {runtime_minutes}min")
            # Try other seasons if season 1 didn't work
            if not runtime_minutes:
                for season_num in [2, 3, 4, 5]:
                    season_cache_key = f"tmdb_season_{media.item.media_id}_{season_num}"
                    cached_season_data = cache.get(season_cache_key)
                    if cached_season_data and cached_season_data.get("details", {}).get("runtime"):
                        runtime_str = cached_season_data["details"]["runtime"]
                        runtime_minutes = parse_runtime_to_minutes(runtime_str)
                        if runtime_minutes and runtime_minutes > 0:
                            logger.debug(f"Using cached season {season_num} avg runtime for {media.item.title}: {runtime_minutes}min")
                            break

        # FOURTH: Use industry standard fallback
        if not runtime_minutes or runtime_minutes <= 0:
            if media.item.source == "tmdb":
                runtime_minutes = 30
            elif media.item.source == "mal":
                runtime_minutes = 23
            else:
                runtime_minutes = 30
            logger.debug(f"Using fallback runtime for {media.item.title}: {runtime_minutes}min")
        return runtime_minutes

    def _get_total_time_left(
        media,
        episodes_left,
        *,
        breakdown_override=None,
        progress_override=None,
    ):
        """Get total time left by summing actual unwatched episode runtimes, with fallback."""
        # First, try to sum actual unwatched episode runtimes
        total_runtime, eps_with_data = _calc_unwatched_runtime_total(
            media,
            episodes_left,
            breakdown_override=breakdown_override,
            progress_override=progress_override,
        )

        if total_runtime is not None and eps_with_data == episodes_left:
            # We have runtime data for all unwatched episodes - use exact sum
            logger.debug(
                f"{media.item.title}: Using exact sum of {eps_with_data} unwatched episodes = {total_runtime}min",
            )
            return total_runtime
        if total_runtime is not None and eps_with_data > 0:
            # Partial data: use what we have + estimate for missing episodes
            missing_eps = episodes_left - eps_with_data
            avg_runtime = total_runtime / eps_with_data
            estimated_missing = int(missing_eps * avg_runtime)
            final_total = total_runtime + estimated_missing
            logger.debug(
                f"{media.item.title}: Partial data - {eps_with_data} eps={total_runtime}min + "
                f"{missing_eps} eps estimated={estimated_missing}min (avg {avg_runtime:.0f}min/ep)",
            )
            return final_total
        # No runtime data for unwatched episodes - fall back to average method
        runtime = _calc_runtime_minutes(media)
        if not runtime or runtime <= 0:
            runtime = 30
        total = episodes_left * runtime
        logger.debug(
            f"{media.item.title}: Fallback to average - {episodes_left} eps × {runtime}min = {total}min",
        )
        return total

    def _end_date_for_sort(media):
        # Prefer aggregated_end_date when present, else media.end_date
        return getattr(media, "aggregated_end_date", None) or getattr(media, "end_date", None) or getattr(media, "progressed_at", None) or getattr(media, "created_at", None)

    def _effective_max_progress(media):
        """Prefer annotated max_progress; fallback to DB episodes to avoid negatives."""
        annotated = getattr(media, "max_progress", 0) or 0
        if annotated <= 0 or annotated < media.progress:
            total_from_db = 0
            # Use prefetched seasons/episodes when available
            if hasattr(media, "seasons"):
                for season in media.seasons.all():
                    if getattr(season.item, "season_number", 0) and hasattr(season, "episodes"):
                        max_ep_num = 0
                        for ep in season.episodes.all():
                            ep_num = getattr(ep.item, "episode_number", 0) or 0
                            max_ep_num = max(max_ep_num, ep_num)
                        total_from_db += max_ep_num
            return max(annotated, total_from_db)
        return annotated

    def _build_time_left_sort_context(media, effective_max):
        """Build a sort-only remaining-episodes view for TV time-left ordering."""
        base_progress = media.progress
        breakdown = getattr(media, "released_episode_breakdown", {}) or {}
        context = {
            "episodes_left": max(effective_max - base_progress, 0),
            "progress": base_progress,
            "breakdown": breakdown,
        }

        if getattr(media, "status", Status.IN_PROGRESS.value) == Status.DROPPED.value:
            return context

        seasons = [
            season
            for season in media.seasons.all()
            if getattr(season.item, "season_number", 0)
        ]
        if not seasons or not breakdown:
            return context

        dropped_season_numbers = {
            season.item.season_number
            for season in seasons
            if season.status == Status.DROPPED.value
        }
        if not dropped_season_numbers:
            return context

        filtered_breakdown = {
            season_num: count
            for season_num, count in breakdown.items()
            if season_num not in dropped_season_numbers
        }
        if filtered_breakdown == breakdown:
            return context

        included_progress = sum(
            season.progress
            for season in seasons
            if season.status != Status.DROPPED.value
        )
        logger.debug(
            "%s: excluding dropped seasons from time_left sort: %s",
            media.item.title,
            sorted(dropped_season_numbers),
        )
        return {
            "episodes_left": max(sum(filtered_breakdown.values()) - included_progress, 0),
            "progress": included_progress,
            "breakdown": filtered_breakdown,
        }

    # Explicit bucketing for deterministic grouping
    active_statuses = {Status.IN_PROGRESS.value, Status.PLANNING.value, Status.PAUSED.value}
    group_active = []           # episodes_left > 0 and status in active_statuses
    group_inprog_zero = []      # status == IN_PROGRESS and episodes_left == 0
    group_completed = []        # status == COMPLETED and episodes_left == 0
    group_dropped = []          # status == DROPPED
    group_tail = []             # everything else (unreleased/unknown)

    for media in media_list:
        # Compute effective episodes_left
        if not hasattr(media, "max_progress"):
            group_tail.append(media)
            continue

        if media.progress is None:
            group_tail.append(media)
            continue

        annotated_max = getattr(media, "max_progress", None)
        status = getattr(media, "status", Status.IN_PROGRESS.value)

        # Keep sorting fast by relying on scheduled calendar refreshes.
        fallback_max = _effective_max_progress(media) or 0
        effective_max = max(annotated_max or 0, fallback_max, media.progress)

        media.max_progress = effective_max
        time_left_context = _build_time_left_sort_context(media, effective_max)
        episodes_left = time_left_context["episodes_left"]

        # Debug shows that should have episodes left but show 0
        if media.progress > 0 and episodes_left == 0 and media.item.title in ["Taskmaster", "Rent-a-Girlfriend", "The Last of Us"]:
            logger.debug(f"DEBUG 0 episodes: {media.item.title} - progress={media.progress}, max_progress={effective_max}, episodes_left={episodes_left}")

        status = getattr(media, "status", Status.IN_PROGRESS.value)

        if status == Status.DROPPED.value:
            group_dropped.append(media)
            continue

        if episodes_left == 0 and status == Status.IN_PROGRESS.value:
            group_inprog_zero.append(media)
            continue

        if episodes_left == 0 and status == Status.COMPLETED.value:
            group_completed.append(media)
            continue

        if episodes_left > 0 and status in active_statuses:
            group_active.append((media, time_left_context))
            continue

        group_tail.append(media)

    # Sort each group
    # 1) Active by least total minutes left
    def _active_key(entry):
        media, time_left_context = entry
        episodes_left = time_left_context["episodes_left"]
        # Use sum of actual unwatched episode runtimes instead of average
        total = _get_total_time_left(
            media,
            episodes_left,
            breakdown_override=time_left_context["breakdown"],
            progress_override=time_left_context["progress"],
        )
        # Store the display values using non-property attributes
        media.episodes_left_display = episodes_left
        if total > 0:
            hours = int(total // 60)
            minutes = int(total % 60)
            if hours > 0:
                media.time_left_display = f"{hours}h {minutes}m"
            else:
                media.time_left_display = f"{minutes}m"
        else:
            media.time_left_display = f"{episodes_left} ep" if episodes_left > 0 else "-"
        return (total, media.item.title.lower())
    group_active_sorted = [m for (m, _) in sorted(group_active, key=_active_key)]

    # 2) In-Progress caught-up by newest end_date
    for m in group_inprog_zero:
        m.episodes_left_display = 0
        m.time_left_display = "0m"
    group_inprog_zero_sorted = sorted(
        group_inprog_zero,
        key=lambda m: (-( _end_date_for_sort(m).timestamp() if _end_date_for_sort(m) else float("-inf") ), m.item.title.lower()),
    )

    # 3) Completed by newest end_date
    for m in group_completed:
        m.episodes_left_display = 0
        m.time_left_display = "0m"
    group_completed_sorted = sorted(
        group_completed,
        key=lambda m: (-( _end_date_for_sort(m).timestamp() if _end_date_for_sort(m) else float("-inf") ), m.item.title.lower()),
    )

    # 4) Dropped - show remaining content (sorted by least time left)
    for m in group_dropped:
        # Debug logging for first few dropped shows
        if not hasattr(m, "_debug_logged"):
            m._debug_logged = True
            logger.debug(f"Dropped show: {m.item.title} - progress={m.progress}, max_progress={getattr(m, 'max_progress', 'MISSING')}, hasattr={hasattr(m, 'max_progress')}")

        # Calculate episodes remaining (not watched)
        if hasattr(m, "max_progress") and hasattr(m, "progress") and m.max_progress > 0:
            episodes_left = m.max_progress - m.progress
            episodes_left = max(episodes_left, 0)
            m.episodes_left_display = episodes_left

            if episodes_left > 0:
                # Use sum of actual unwatched episode runtimes
                total = _get_total_time_left(m, episodes_left)
                hours = int(total // 60)
                minutes = int(total % 60)
                if hours > 0:
                    m.time_left_display = f"{hours}h {minutes}m"
                else:
                    m.time_left_display = f"{minutes}m"
                # Store total for sorting
                m._time_left_total = total
            else:
                m.time_left_display = "0m"
                m._time_left_total = 0
        else:
            # No max_progress data - show as unknown
            logger.debug(f"Dropped show NO DATA: {m.item.title} - Setting '-' display")
            m.episodes_left_display = 0
            m.time_left_display = "-"
            m._time_left_total = 0

    # Sort dropped by least time left (ascending), then by title
    group_dropped_sorted = sorted(
        group_dropped,
        key=lambda m: (getattr(m, "_time_left_total", 0), m.item.title.lower()),
    )

    # 5) Tail (unreleased/unknown) - set display values
    for m in group_tail:
        m.episodes_left_display = 0
        m.time_left_display = "-"

    sorted_list = (
        group_active_sorted
        + group_inprog_zero_sorted
        + group_completed_sorted
        + group_dropped_sorted
        + group_tail
    )
    logger.debug(
        "DEBUG: Group counts -> active: %d, inprog_zero: %d, completed: %d, dropped: %d, tail: %d",
        len(group_active_sorted), len(group_inprog_zero_sorted), len(group_completed_sorted), len(group_dropped_sorted), len(group_tail),
    )

    # Log first 10 items for debugging
    logger.debug("DEBUG: First 10 sorted shows:")
    for i, media in enumerate(sorted_list[:10]):
        episodes_left = (media.max_progress or 0) - (media.progress or 0) if hasattr(media, "max_progress") else 0
        logger.debug(f"  {i+1}. {media.item.title} - Episodes left: {episodes_left}, Status: {getattr(media, 'status', 'Unknown')}")

    if direction == "desc":
        return list(reversed(sorted_list))

    return sorted_list
