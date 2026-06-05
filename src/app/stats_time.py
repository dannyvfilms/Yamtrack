"""stats_time.py — Time calculation pipeline for the statistics layer.

Contains the full chain from raw media entries to total minutes watched,
covering TV episodes, movies, anime, games, and music.  Also re-houses
the music runtime lookup helper that was previously scattered across
statistics.py and stats_music.py.
"""
import logging

from django.db import transaction

from app import providers
from app.models import MediaTypes, Track
from app.stats_utils import (
    _format_hours_minutes,
    _get_activity_datetime,
    _infer_user_from_user_media,
    _is_media_in_date_range,
    _iter_media_list,
    parse_runtime_to_minutes,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Game time
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Season / episode helpers
# ---------------------------------------------------------------------------

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
        from app.providers import tmdb  # noqa: PLC0415
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


# ---------------------------------------------------------------------------
# TV / anime time
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Movie / generic media time
# ---------------------------------------------------------------------------

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
    from app.models import Item  # noqa: PLC0415
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


# ---------------------------------------------------------------------------
# Music time
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Main aggregator
# ---------------------------------------------------------------------------

def calculate_minutes_per_media_type(user_media, start_date, end_date, user=None):
    """Return total minutes watched per media type within the date range."""
    minutes_per_type = {}

    for media_type, media_list in user_media.items():
        total_minutes = 0

        if media_type == MediaTypes.PODCAST.value:
            # Podcast: sum runtime from completed plays in history records
            from app.stats_podcast import _collect_podcast_play_data, _get_podcast_history_data  # noqa: PLC0415
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


def get_hours_per_media_type(user_media, start_date, end_date, minutes_per_type=None, duration_format="hours_minutes"):
    """Calculate total hours watched per media type within the date range."""
    if minutes_per_type is None:
        minutes_per_type = calculate_minutes_per_media_type(user_media, start_date, end_date)
    hours = {}
    for media_type, total_minutes in minutes_per_type.items():
        if media_type == MediaTypes.BOARDGAME.value:
            hours[media_type] = f"{total_minutes} play{'s' if total_minutes != 1 else ''}"
        else:
            hours[media_type] = _format_hours_minutes(total_minutes, duration_format)
    return hours
