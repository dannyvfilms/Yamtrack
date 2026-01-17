import logging

from celery import shared_task
from django.contrib.auth import get_user_model

import events
from app.mixins import disable_fetch_releases
from app.models import MediaTypes
from app.templatetags import app_tags
from integrations.imports import (
    anilist,
    goodreads,
    helpers,
    hltb,
    imdb,
    kitsu,
    mal,
    plex,
    pocketcasts,
    simkl,
    steam,
    trakt,
    yamtrack,
)

logger = logging.getLogger(__name__)
ERROR_TITLE = "\n\n\n Couldn't import the following media: \n\n"


def format_media_type_display(count, media_type):
    """Format media type display with proper pluralization."""
    if count == 0:
        return None
    if count == 1:
        return f"{count} {dict(MediaTypes.choices).get(media_type, media_type)}"
    return f"{count} {app_tags.media_type_readable_plural(media_type)}"


def format_import_message(imported_counts, warning_messages=None):
    """Format the import result message based on counts and warnings."""
    parts = []

    # Handle music specially - show both play events and unique tracks
    music_play_events = imported_counts.get(MediaTypes.MUSIC.value, 0)
    music_unique_tracks = imported_counts.get("music_unique_tracks", 0)

    if music_play_events > 0:
        if music_unique_tracks > 0:
            # Show both play events and unique tracks
            parts.append(
                f"{music_play_events} music play event{'s' if music_play_events != 1 else ''} "
                f"({music_unique_tracks} unique track{'s' if music_unique_tracks != 1 else ''})",
            )
        else:
            # Fallback to standard format if unique tracks not available
            parts.append(format_media_type_display(music_play_events, MediaTypes.MUSIC.value))

    # Add other media types (excluding music which we handled above)
    media_type_values = set(MediaTypes.values)
    for media_type, count in imported_counts.items():
        if (
            media_type == MediaTypes.MUSIC.value
            or media_type == "music_unique_tracks"
            or media_type not in media_type_values
        ):
            continue
        formatted = format_media_type_display(count, media_type)
        if formatted:
            parts.append(formatted)

    parts = [p for p in parts if p is not None]

    if not parts:
        info_message = "No media was imported."
    else:
        info_message = f"Imported {helpers.join_with_commas_and(parts)}."

    metric_parts = []
    metric_mappings = [
        ("created", "created"),
        ("updated", "updated"),
        ("skipped_missing_ids", "skipped (missing IDs)"),
        ("skipped_existing", "skipped (existing)"),
        ("skipped_unknown_type", "skipped (unknown type)"),
        ("skipped_other_user", "skipped (other users)"),
    ]
    for key, label in metric_mappings:
        value = imported_counts.get(key)
        if value:
            metric_parts.append(f"{value} {label}")

    if metric_parts:
        info_message = f"{info_message} {helpers.join_with_commas_and(metric_parts)}."

    if warning_messages:
        return f"{info_message} {ERROR_TITLE} {warning_messages}"
    return info_message


def import_media(importer_func, identifier, user_id, mode, oauth_username=None):
    """Handle the import process for different media services."""
    user = get_user_model().objects.get(id=user_id)

    with disable_fetch_releases():
        if oauth_username is None:
            imported_counts, warnings = importer_func(
                identifier,
                user,
                mode,
            )
        else:
            imported_counts, warnings = importer_func(
                identifier,
                user,
                mode,
                username=oauth_username,
            )

    events.tasks.reload_calendar.delay()

    return format_import_message(imported_counts, warnings)


@shared_task(name="Import from Trakt")
def import_trakt(user_id, mode, token=None, username=None):
    """Celery task for importing media data from Trakt.

    Can import using either OAuth (token provided) or public username.
    """
    return import_media(trakt.importer, token, user_id, mode, username)


@shared_task(name="Import from SIMKL")
def import_simkl(token, user_id, mode, username=None):  # noqa: ARG001
    """Celery task for importing media data from SIMKL."""
    return import_media(simkl.importer, token, user_id, mode)


@shared_task(name="Import from MyAnimeList")
def import_mal(username, user_id, mode):
    """Celery task for importing anime and manga data from MyAnimeList."""
    return import_media(mal.importer, username, user_id, mode)


@shared_task(name="Import from AniList")
def import_anilist(user_id, mode, token=None, username=None):
    """Celery task for importing media data from AniList."""
    return import_media(anilist.importer, token, user_id, mode, username)


@shared_task(name="Import from Kitsu")
def import_kitsu(username, user_id, mode):
    """Celery task for importing anime and manga data from Kitsu."""
    return import_media(kitsu.importer, username, user_id, mode)


@shared_task(name="Import from Yamtrack")
def import_yamtrack(file, user_id, mode):
    """Celery task for importing media data from Yamtrack."""
    return import_media(yamtrack.importer, file, user_id, mode)


@shared_task(name="Import from HowLongToBeat")
def import_hltb(file, user_id, mode):
    """Celery task for importing media data from HowLongToBeat."""
    return import_media(hltb.importer, file, user_id, mode)


@shared_task(name="Import from Steam")
def import_steam(username, user_id, mode):
    """Celery task for importing game data from Steam."""
    return import_media(steam.importer, username, user_id, mode)


@shared_task(name="Import from IMDB")
def import_imdb(file, user_id, mode):
    """Celery task for importing media data from IMDB."""
    return import_media(imdb.importer, file, user_id, mode)


@shared_task(name="Import from GoodReads")
def import_goodreads(file, user_id, mode):
    """Celery task for importing media data from GoodReads."""
    return import_media(goodreads.importer, file, user_id, mode)


@shared_task(name="Import from Plex")
def import_plex(library, user_id, mode, username=None):  # noqa: ARG001
    """Celery task for importing media data from Plex."""
    return import_media(plex.importer, library, user_id, mode)


@shared_task(name="Import from Pocket Casts")
def import_pocketcasts(user_id, mode="new"):
    """Celery task for importing podcast history from Pocket Casts."""
    return import_media(pocketcasts.importer, None, user_id, mode)


@shared_task(name="Import from Pocket Casts (Recurring)")
def import_pocketcasts_history(user_id):
    """Recurring import task for Pocket Casts (called every 2 hours via Celery beat)."""
    return import_pocketcasts.delay(user_id, mode="new")


@shared_task(name="Poll Last.fm for all users")
def poll_all_lastfm_scrobbles():
    """Global task to poll Last.fm for all connected users.

    This task processes all users with active Last.fm connections in batches.
    Uses one global periodic schedule (not per-user) to avoid schedule table bloat.
    """
    from django.utils import timezone

    from integrations import lastfm_api
    from integrations.models import LastFMAccount
    from integrations.webhooks import lastfm as lastfm_webhooks

    # Get all connected users
    accounts = LastFMAccount.objects.filter(connection_broken=False).select_related("user")

    if not accounts.exists():
        logger.debug("No Last.fm accounts to poll")
        return {"processed": 0, "errors": 0, "message": "No accounts to poll"}

    logger.info("Polling Last.fm for %d users", accounts.count())

    from app import statistics_cache

    processor = lastfm_webhooks.LastFMScrobbleProcessor()
    processed_count = 0
    error_count = 0
    batch_size = 10
    import random
    import time

    # Collect affected day_keys per user for batch cache refresh
    user_affected_days: dict[int, set] = {}

    # Process in batches with jitter to avoid thundering herd
    accounts_list = list(accounts)
    random.shuffle(accounts_list)  # Randomize order

    for i, account in enumerate(accounts_list):
        # Add jitter between batches
        if i > 0 and i % batch_size == 0:
            time.sleep(random.uniform(0.5, 2.0))

        try:
            # Check if user has music enabled
            if not getattr(account.user, "music_enabled", False):
                logger.debug(
                    "Skipping Last.fm poll for user %s: music disabled",
                    account.user.username,
                )
                continue

            # Calculate from_timestamp with 60-second overlap for safety
            from_timestamp_uts = None
            if account.last_fetch_timestamp_uts:
                from_timestamp_uts = account.last_fetch_timestamp_uts - 60

            # Fetch all tracks (handles pagination)
            try:
                tracks = lastfm_api.get_all_recent_tracks(
                    username=account.lastfm_username,
                    from_timestamp_uts=from_timestamp_uts,
                    extended=1,
                )
            except lastfm_api.LastFMRateLimitError as e:
                logger.warning(
                    "Rate limit exceeded for user %s, will retry next cycle: %s",
                    account.user.username,
                    e,
                )
                # Don't mark as broken for rate limits, just skip this cycle
                error_count += 1
                continue
            except lastfm_api.LastFMClientError as e:
                # Invalid username or user not found
                logger.error(
                    "Last.fm client error for user %s: %s",
                    account.user.username,
                    e,
                )
                account.connection_broken = True
                account.failure_count += 1
                account.last_error_code = "6"  # Invalid user
                account.last_error_message = str(e)
                account.last_failed_at = timezone.now()
                account.save(
                    update_fields=[
                        "connection_broken",
                        "failure_count",
                        "last_error_code",
                        "last_error_message",
                        "last_failed_at",
                    ],
                )
                error_count += 1
                continue
            except lastfm_api.LastFMAPIError as e:
                logger.error(
                    "Last.fm API error for user %s: %s",
                    account.user.username,
                    e,
                )
                account.failure_count += 1
                account.last_error_code = "unknown"
                account.last_error_message = str(e)[:500]  # Truncate long messages
                account.last_failed_at = timezone.now()
                account.save(
                    update_fields=[
                        "failure_count",
                        "last_error_code",
                        "last_error_message",
                        "last_failed_at",
                    ],
                )
                error_count += 1
                continue

            # Process tracks
            stats = processor.process_tracks(tracks, account.user)

            # Collect affected day_keys for this user
            affected_day_keys = stats.get("affected_day_keys", set())
            if affected_day_keys:
                user_id = account.user.id
                if user_id not in user_affected_days:
                    user_affected_days[user_id] = set()
                user_affected_days[user_id].update(affected_day_keys)

            # Update timestamp to most recent track's timestamp
            # Find the latest timestamp from processed tracks
            latest_timestamp_uts = account.last_fetch_timestamp_uts or 0
            for track in tracks:
                date_attr = track.get("date", {})
                date_uts = date_attr.get("uts")
                if date_uts:
                    try:
                        track_timestamp = int(date_uts)
                        if track_timestamp > latest_timestamp_uts:
                            latest_timestamp_uts = track_timestamp
                    except (ValueError, TypeError):
                        continue

            # Update account on success
            account.last_fetch_timestamp_uts = latest_timestamp_uts
            account.last_sync_at = timezone.now()
            account.connection_broken = False
            account.failure_count = 0
            account.last_error_code = ""
            account.last_error_message = ""
            account.last_failed_at = None
            account.save(
                update_fields=[
                    "last_fetch_timestamp_uts",
                    "last_sync_at",
                    "connection_broken",
                    "failure_count",
                    "last_error_code",
                    "last_error_message",
                    "last_failed_at",
                ],
            )

            processed_count += 1
            logger.info(
                "Successfully polled Last.fm for user %s: %d processed, %d skipped, %d errors",
                account.user.username,
                stats["processed"],
                stats["skipped"],
                stats["errors"],
            )

        except Exception as e:
            logger.error(
                "Unexpected error polling Last.fm for user %s: %s",
                account.user.username,
                e,
                exc_info=True,
            )
            account.failure_count += 1
            account.last_error_code = "exception"
            account.last_error_message = str(e)[:500]
            account.last_failed_at = timezone.now()
            account.save(
                update_fields=[
                    "failure_count",
                    "last_error_code",
                    "last_error_message",
                    "last_failed_at",
                ],
            )
            error_count += 1

    # Trigger batch cache refresh for all affected users
    # Note: History cache invalidation is already handled by per-track signals,
    # so we only need to refresh statistics cache here
    for user_id, affected_day_keys in user_affected_days.items():
        if affected_day_keys:
            statistics_cache.invalidate_statistics_days(
                user_id,
                day_values=list(affected_day_keys),
                reason="lastfm_batch_import",
            )
            statistics_cache.schedule_all_ranges_refresh(user_id)

    logger.info(
        "Last.fm polling completed: %d users processed, %d errors",
        processed_count,
        error_count,
    )

    return {
        "processed": processed_count,
        "errors": error_count,
        "total_accounts": accounts.count(),
    }
