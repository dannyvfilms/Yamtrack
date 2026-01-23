import logging

from celery import shared_task
from django.contrib.auth import get_user_model

import events
from app.collection_helpers import (
    extract_collection_metadata_from_plex,
)
from app.helpers import is_item_collected
from app.mixins import disable_fetch_releases
from app.models import CollectionEntry, Item, MediaTypes
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

    # Queue collection metadata update task for media server imports
    _queue_post_import_collection_update(user_id, importer_func)

    return format_import_message(imported_counts, warnings)


def _queue_post_import_collection_update(user_id, importer_func):
    """Queue collection metadata update task after import if applicable.

    Args:
        user_id: User ID
        importer_func: The importer function that was called
    """
    # Check if this is a media server import that supports collection updates
    # Compare by function reference
    import integrations.imports.plex as plex_import_module
    if importer_func == plex_import_module.importer:
        # Queue Plex collection update (run after calendar reload with a delay)
        update_collection_metadata_from_plex.apply_async(
            args=("all", user_id),
            countdown=60,  # Run 60 seconds after import to allow calendar reload to complete
        )
        logger.info("Queued post-import collection metadata update for user %s", user_id)
    # TODO: Add Jellyfin and Emby when their importers are available


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


@shared_task(name="Update collection metadata from Plex webhook")
def update_collection_metadata_from_plex_webhook(
    user_id,
    item_id,
    rating_key,
    plex_uri,
    plex_token,
):
    """Update collection metadata from Plex webhook event.

    Args:
        user_id: User ID
        item_id: Item ID in Yamtrack
        rating_key: Plex rating key
        plex_uri: Plex server URI
        plex_token: Plex authentication token
    """
    from integrations import plex as plex_api

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        item = Item.objects.get(id=item_id)
    except (User.DoesNotExist, Item.DoesNotExist) as exc:
        logger.warning(
            "Cannot update collection metadata: %s (user_id=%s, item_id=%s)",
            exc,
            user_id,
            item_id,
        )
        return None

    # Fetch detailed metadata from Plex
    try:
        plex_metadata = plex_api.fetch_metadata(plex_token, plex_uri, rating_key)
    except Exception as exc:
        logger.warning(
            "Failed to fetch Plex metadata for collection update: %s (rating_key=%s)",
            exc,
            rating_key,
        )
        return None

    if not plex_metadata:
        logger.debug("No Plex metadata returned for rating_key=%s", rating_key)
        return None

    # Extract collection metadata
    collection_metadata = extract_collection_metadata_from_plex(plex_metadata)

    # Get or create collection entry
    entry, created = CollectionEntry.objects.get_or_create(
        user=user,
        item=item,
        defaults=collection_metadata,
    )

    if not created:
        # Update existing entry
        for key, value in collection_metadata.items():
            if value:  # Only update non-empty values
                setattr(entry, key, value)
        entry.save()

    logger.info(
        "Updated collection metadata for %s - %s (created=%s)",
        user.username,
        item.title,
        created,
    )

    return entry.id


@shared_task(name="Update collection metadata from Plex")
def update_collection_metadata_from_plex(library, user_id):
    """Update collection metadata for existing Yamtrack items from Plex server.

    This task queries the Plex server for items that match existing Yamtrack items
    and updates their collection metadata without performing a full import.

    Args:
        library: Plex library identifier (e.g., "all" or "machine_id::section_id")
        user_id: User ID
    """
    from integrations import plex as plex_api
    from app.collection_helpers import extract_collection_metadata_from_plex
    from app.helpers import is_item_collected
    from app.models import Item

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning("Cannot update collection metadata: user %s not found", user_id)
        return {"error": "User not found"}

    plex_account = getattr(user, "plex_account", None)
    if not plex_account or not plex_account.plex_token:
        logger.warning("Cannot update collection metadata: Plex not connected for user %s", user.username)
        return {"error": "Plex not connected"}

    try:
        resources = plex_api.list_resources(plex_account.plex_token)
    except plex_api.PlexAuthError as exc:
        logger.warning("Plex token expired for user %s: %s", user.username, exc)
        return {"error": "Plex token expired"}

    # Get target sections
    sections = plex_account.sections or []
    if not sections:
        sections = plex_api.list_sections(plex_account.plex_token)
        plex_account.sections = sections
        plex_account.sections_refreshed_at = timezone.now()
        plex_account.save(update_fields=["sections", "sections_refreshed_at"])

    if library != "all":
        try:
            machine_id, section_id = library.split("::", 1)
            sections = [
                s for s in sections
                if s.get("machine_identifier") == machine_id
                and str(s.get("id")) == str(section_id)
            ]
        except ValueError:
            logger.warning("Invalid Plex library selection: %s", library)
            return {"error": "Invalid library selection"}

    if not sections:
        logger.warning("No Plex sections found for user %s", user.username)
        return {"error": "No sections found"}

    updated_count = 0
    error_count = 0

    # Get user's tracked media items (Movies, TV, Music) that could have collection entries
    from app.models import Movie, TV, Music
    user_movies = Movie.objects.filter(user=user).select_related("item")
    user_tv = TV.objects.filter(user=user).select_related("item")
    user_music = Music.objects.filter(user=user).select_related("item")

    all_user_items = list(user_movies.values_list("item_id", flat=True))
    all_user_items.extend(user_tv.values_list("item_id", flat=True))
    all_user_items.extend(user_music.values_list("item_id", flat=True))

    if not all_user_items:
        logger.info("No tracked media found for user %s, nothing to update", user.username)
        return {"updated": 0, "errors": 0, "message": "No tracked media found"}

    user_items = Item.objects.filter(id__in=all_user_items).select_related()

    # For each section, we'll need to query Plex library and match items
    # This is a simplified implementation - in practice, you'd want to:
    # 1. Query Plex library for all items in the section
    # 2. Match them with Yamtrack items by external IDs (TMDB, IMDB, TVDB, etc.)
    # 3. For matched items, fetch detailed metadata and update collection entries

    # For now, we'll use a simpler approach: iterate through user's items and try to find
    # them in Plex by querying Plex history (which we already have access to)
    # This is less efficient but works with existing infrastructure

    for section in sections:
        section_type = (section.get("type") or "").lower()
        if section_type not in ("movie", "show", "artist", "music"):
            continue

        # Get server URI
        connections = []
        if section.get("uri"):
            connections.append(section.get("uri"))
        # Add connections from resources
        for resource in resources:
            if resource.get("machine_identifier") == section.get("machine_identifier"):
                for conn in resource.get("connections", []):
                    uri = conn.get("uri") if isinstance(conn, dict) else conn
                    if uri and uri not in connections:
                        connections.append(uri)

        if not connections:
            logger.warning("No connections found for section %s", section.get("title"))
            continue

        plex_uri = connections[0]  # Use first available connection

        # Fetch recent history from this section to get rating keys
        # This is a workaround - ideally we'd query the full library
        try:
            history_entries, _ = plex_api.fetch_history(
                plex_account.plex_token,
                plex_uri,
                section.get("id"),
                start=0,
                size=1000,  # Get up to 1000 recent items
            )

            # Build a mapping of external IDs to rating keys from history
            rating_key_map = {}  # Maps (media_type, external_id) -> rating_key
            for entry in history_entries:
                rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                if not rating_key:
                    continue

                # Extract external IDs from entry
                guids = entry.get("Guid", [])
                if not guids:
                    single_guid = entry.get("guid")
                    if single_guid:
                        guids = [{"id": single_guid}]

                for guid in guids:
                    guid_value = guid.get("id") if isinstance(guid, dict) else guid
                    if not guid_value:
                        continue

                    guid_lower = guid_value.lower()
                    # Extract TMDB ID
                    if "tmdb" in guid_lower or "themoviedb" in guid_lower:
                        import re
                        match = re.search(r"\d+", guid_value)
                        if match:
                            tmdb_id = match.group(0)
                            item_type = "movie" if section_type == "movie" else "tv"
                            rating_key_map[(item_type, tmdb_id)] = rating_key

            # Now match user items with rating keys and update collection metadata
            for item in user_items:
                if item.media_type not in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
                    continue

                # Try to find rating key for this item
                rating_key = rating_key_map.get((item.media_type, item.media_id))
                if not rating_key:
                    continue

                # Fetch detailed metadata and update collection
                try:
                    plex_metadata = plex_api.fetch_metadata(
                        plex_account.plex_token,
                        plex_uri,
                        str(rating_key),
                    )
                    if not plex_metadata:
                        continue

                    collection_metadata = extract_collection_metadata_from_plex(plex_metadata)
                    if not any(collection_metadata.values()):
                        continue  # No metadata to update

                    # Get or create collection entry
                    entry, created = CollectionEntry.objects.get_or_create(
                        user=user,
                        item=item,
                        defaults=collection_metadata,
                    )

                    if not created:
                        # Update existing entry
                        for key, value in collection_metadata.items():
                            if value:  # Only update non-empty values
                                setattr(entry, key, value)
                        entry.save()

                    updated_count += 1
                    logger.debug(
                        "Updated collection metadata for %s - %s",
                        user.username,
                        item.title,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to update collection metadata for %s: %s",
                        item.title,
                        exc,
                    )
                    error_count += 1

        except Exception as exc:
            logger.warning("Failed to process section %s: %s", section.get("title"), exc)
            error_count += 1
            continue

    return {
        "updated": updated_count,
        "errors": error_count,
        "message": f"Updated collection metadata for {updated_count} items",
    }
