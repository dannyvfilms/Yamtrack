import logging
import time

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone

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

    logger.info(
        "Starting collection metadata update task (user_id=%s, item_id=%s, rating_key=%s, uri=%s)",
        user_id,
        item_id,
        rating_key,
        plex_uri,
    )

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

    logger.debug("Found user=%s, item=%s (media_type=%s)", user.username, item.title, item.media_type)

    # Fetch detailed metadata from Plex
    try:
        logger.debug("Fetching Plex metadata for rating_key=%s from uri=%s", rating_key, plex_uri)
        plex_metadata = plex_api.fetch_metadata(plex_token, plex_uri, rating_key)
    except Exception as exc:
        logger.warning(
            "Failed to fetch Plex metadata for collection update: %s (rating_key=%s, uri=%s). "
            "This may indicate the URI is incorrect or the server is unreachable.",
            exc,
            rating_key,
            plex_uri,
            exc_info=True,
        )
        # If HTTP failed, try HTTPS (some servers require HTTPS)
        if plex_uri.startswith("http://") and "500" in str(exc):
            https_uri = plex_uri.replace("http://", "https://")
            logger.debug("Retrying with HTTPS: %s", https_uri)
            try:
                plex_metadata = plex_api.fetch_metadata(plex_token, https_uri, rating_key)
                logger.info("Successfully fetched metadata using HTTPS URI")
            except Exception as https_exc:
                logger.debug("HTTPS retry also failed: %s", https_exc)
                return None
        else:
            return None

    if not plex_metadata:
        logger.debug("No Plex metadata returned for rating_key=%s", rating_key)
        return None

    logger.debug("Received Plex metadata with keys: %s", list(plex_metadata.keys()))

    # Extract collection metadata
    collection_metadata = extract_collection_metadata_from_plex(plex_metadata)
    logger.debug(
        "Extracted collection metadata: %s",
        {k: v for k, v in collection_metadata.items() if v},
    )

    # Get or create collection entry
    entry, created = CollectionEntry.objects.get_or_create(
        user=user,
        item=item,
        defaults=collection_metadata,
    )

    # Store rating key and URI for future bulk imports (cache for faster lookups)
    rating_key_updated = False
    if entry.plex_rating_key != rating_key or entry.plex_uri != plex_uri:
        entry.plex_rating_key = rating_key
        entry.plex_uri = plex_uri
        entry.plex_rating_key_updated_at = timezone.now()
        rating_key_updated = True

    if not created:
        # Update existing entry
        updated_fields = []
        for key, value in collection_metadata.items():
            if value:  # Only update non-empty values
                old_value = getattr(entry, key, None)
                if old_value != value:
                    setattr(entry, key, value)
                    updated_fields.append(f"{key}={old_value}->{value}")
        
        # Save if we have updates (collection metadata or rating key)
        if updated_fields or rating_key_updated:
            entry.save()
            if updated_fields:
                logger.debug("Updated collection entry fields: %s", ", ".join(updated_fields))
            if rating_key_updated:
                logger.debug("Updated cached rating key: %s (uri=%s)", rating_key, plex_uri)
        else:
            logger.debug("No changes to collection entry")
    else:
        # New entry - save to store rating key
        if rating_key_updated:
            entry.save()
            logger.debug("Stored cached rating key for new entry: %s (uri=%s)", rating_key, plex_uri)

    logger.info(
        "Collection metadata update completed for %s - %s (created=%s, entry_id=%s)",
        user.username,
        item.title,
        created,
        entry.id,
    )

    # For TV shows, also create episode-level collection entries
    if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        logger.info("TV show detected, creating episode-level collection entries for %s", item.title)
        try:
            # Use the aggregated metadata function to get episode-level data
            aggregated_metadata, episode_list = _aggregate_tv_show_collection_metadata(
                plex_token,
                plex_uri,
                rating_key,
                show_metadata=plex_metadata,
                fetch_episode_details=True,  # Always fetch episode details for webhooks
            )
            
            logger.info("Found %d episodes with collection metadata for %s", len(episode_list), item.title)
            
            episode_entries_created = 0
            episode_entries_updated = 0
            episode_entries_skipped = 0
            
            for episode_data in episode_list:
                season_number = episode_data["season_number"]
                episode_number = episode_data["episode_number"]
                episode_collection_metadata = episode_data["collection_metadata"]
                
                # Skip Season 0 (Specials) to match Details pane behavior
                if season_number == 0:
                    episode_entries_skipped += 1
                    continue
                
                # Find or create the episode Item
                try:
                    episode_item, episode_item_created = Item.objects.get_or_create(
                        media_id=item.media_id,
                        source=item.source,
                        media_type=MediaTypes.EPISODE.value,
                        season_number=season_number,
                        episode_number=episode_number,
                        defaults={
                            "title": f"Episode {episode_number}",
                            "image": item.image,
                        },
                    )
                    
                    # Create or update collection entry for this episode
                    episode_entry, episode_entry_created = CollectionEntry.objects.get_or_create(
                        user=user,
                        item=episode_item,
                        defaults=episode_collection_metadata,
                    )
                    
                    # Store rating key and URI for episode (if we can get it from episode metadata)
                    # Note: We'd need to fetch individual episode metadata to get episode rating keys
                    # For now, we'll just store the collection metadata
                    
                    if episode_entry_created:
                        episode_entries_created += 1
                        logger.debug("Created collection entry for episode S%02dE%02d of %s", season_number, episode_number, item.title)
                    else:
                        # Update existing entry
                        updated = False
                        for key, value in episode_collection_metadata.items():
                            if value:  # Only update non-empty values
                                old_value = getattr(episode_entry, key, None)
                                if old_value != value:
                                    setattr(episode_entry, key, value)
                                    updated = True
                        if updated:
                            episode_entry.updated_at = timezone.now()
                            episode_entry.save()
                            episode_entries_updated += 1
                            logger.debug("Updated collection entry for episode S%02dE%02d of %s", season_number, episode_number, item.title)
                            
                except Exception as exc:
                    logger.warning(
                        "Failed to create collection entry for episode S%02dE%02d of %s: %s",
                        season_number,
                        episode_number,
                        item.title,
                        exc,
                        exc_info=True,
                    )
                    continue
            
            logger.info(
                "Episode collection entries for %s: %d created, %d updated, %d skipped (Season 0)",
                item.title,
                episode_entries_created,
                episode_entries_updated,
                episode_entries_skipped,
            )
        except Exception as exc:
            logger.warning(
                "Failed to create episode-level collection entries for TV show %s: %s",
                item.title,
                exc,
                exc_info=True,
            )

    return entry.id


@shared_task(name="Fetch collection metadata for item")
def fetch_collection_metadata_for_item(user_id, item_id):
    """Fetch collection metadata for a single item in the background.
    
    This is triggered when viewing a media details page for an item that doesn't
    have collection data yet. It attempts to find the item in Plex and create
    collection entries.
    
    Args:
        user_id: User ID
        item_id: Item ID in Yamtrack
    """
    from django.contrib.auth import get_user_model
    from app.models import Item, MediaTypes, CollectionEntry
    from integrations import plex as plex_api
    from app.collection_helpers import extract_collection_metadata_from_plex
    from django.utils import timezone
    
    logger.info("Starting collection metadata fetch for user_id=%s, item_id=%s", user_id, item_id)
    
    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        item = Item.objects.get(id=item_id)
    except (User.DoesNotExist, Item.DoesNotExist) as exc:
        logger.warning("Cannot fetch collection metadata: %s (user_id=%s, item_id=%s)", exc, user_id, item_id)
        return None
    
    # Check if user has Plex connected
    plex_account = getattr(user, "plex_account", None)
    if not plex_account or not plex_account.plex_token:
        logger.info("User %s does not have Plex connected, skipping collection fetch", user.username)
        return None
    
    # Check if collection entry already exists
    existing_entry = CollectionEntry.objects.filter(user=user, item=item).first()
    if existing_entry:
        logger.info("Collection entry already exists for %s - %s (entry_id=%s)", user.username, item.title, existing_entry.id)
        return existing_entry.id
    
    # Step 1: Check for cached rating keys (fast path)
    rating_key = None
    plex_uri = None
    
    # Check show-level cached rating key first
    show_cached_entry = CollectionEntry.objects.filter(
        user=user,
        item=item,
        plex_rating_key__isnull=False,
    ).first()
    
    if show_cached_entry and show_cached_entry.plex_rating_key and show_cached_entry.plex_uri:
        rating_key = show_cached_entry.plex_rating_key
        plex_uri = show_cached_entry.plex_uri
        logger.info("Using cached show-level rating key for %s - %s (rating_key=%s)", user.username, item.title, rating_key)
    elif item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        # Check for cached rating key in any episode's collection entry
        from app.models import Item as ItemModel
        episode_items = ItemModel.objects.filter(
            media_id=item.media_id,
            source=item.source,
            media_type=MediaTypes.EPISODE.value,
        )
        episode_item_ids = list(episode_items.values_list('id', flat=True))
        if episode_item_ids:
            cached_episode_entry = CollectionEntry.objects.filter(
                user=user,
                item_id__in=episode_item_ids,
                plex_rating_key__isnull=False,
                plex_uri__isnull=False,
            ).first()
            
            if cached_episode_entry:
                # We have a cached rating key for an episode
                # Fetch episode metadata to get the show's rating key
                episode_rating_key = cached_episode_entry.plex_rating_key
                episode_plex_uri = cached_episode_entry.plex_uri
                
                logger.info("Found cached episode rating key, deriving show rating key from episode %s", episode_rating_key)
                try:
                    episode_metadata = plex_api.fetch_metadata(
                        plex_account.plex_token,
                        episode_plex_uri,
                        str(episode_rating_key),
                    )
                    
                    if episode_metadata:
                        # Get show rating key from episode's parentKey or librarySectionKey
                        # For episodes, parentKey points to the season, and we need to go up to the show
                        parent_key = episode_metadata.get("parentKey") or episode_metadata.get("grandparentKey")
                        if parent_key:
                            # parentKey might be a season, grandparentKey is the show
                            show_key = episode_metadata.get("grandparentKey")
                            if show_key:
                                # Extract rating key from the key path
                                # grandparentKey is usually like "/library/metadata/12345"
                                if "/" in show_key:
                                    show_rating_key_str = show_key.split("/")[-1]
                                    try:
                                        rating_key = str(int(show_rating_key_str))
                                        plex_uri = episode_plex_uri
                                        logger.info("Derived show rating key %s from episode %s", rating_key, episode_rating_key)
                                    except (ValueError, TypeError):
                                        pass
                except Exception as exc:
                    logger.debug("Failed to derive show rating key from episode: %s", exc)
    
    # If we found a cached rating key, use it directly
    if rating_key and plex_uri:
        logger.info("Using cached rating key for %s - %s (rating_key=%s)", user.username, item.title, rating_key)
        return update_collection_metadata_from_plex_webhook(
            user_id=user_id,
            item_id=item_id,
            rating_key=str(rating_key),
            plex_uri=plex_uri,
            plex_token=plex_account.plex_token,
        )
    
    # Step 2: If no cached rating key, search Plex library
    logger.info("No cached rating key found for %s - %s, searching Plex library", user.username, item.title)
    
    try:
        resources = plex_api.list_resources(plex_account.plex_token)
        sections = plex_account.sections or []
        if not sections:
            sections = plex_api.list_sections(plex_account.plex_token)
        
        # Get all available Plex URIs to try as fallbacks
        available_uris = []
        if sections:
            # Add section URIs
            for section in sections:
                if section.get("uri") and section.get("uri") not in available_uris:
                    available_uris.append(section.get("uri"))
            
            # Add connection URIs from resources
            for resource in resources:
                machine_id = resource.get("machine_identifier")
                if machine_id:
                    for section in sections:
                        if section.get("machine_identifier") == machine_id:
                            for conn in resource.get("connections", []):
                                uri = conn.get("uri") if isinstance(conn, dict) else conn
                                if uri and uri not in available_uris:
                                    available_uris.append(uri)
                            break
        
        if not available_uris:
            logger.warning("No Plex URIs available for user %s", user.username)
            return None
        
        # Use first URI as primary, others as fallbacks
        primary_uri = available_uris[0]
        logger.info("Using primary Plex URI: %s (have %d total URIs available)", primary_uri, len(available_uris))
        
        # Search for the item in Plex sections
        for section in sections:
            section_type = (section.get("type") or "").lower()
            if section_type not in ("movie", "show"):
                continue
            
            if item.media_type == MediaTypes.MOVIE.value and section_type != "movie":
                continue
            if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and section_type != "show":
                continue
            
            # Get section key
            section_key = section.get("key") or section.get("id")
            if isinstance(section_key, str) and section_key.startswith("/library/sections/"):
                section_key = section_key.split("/")[-1]
            
            logger.info("Searching section '%s' for %s - %s", section.get("title"), user.username, item.title)
            
            from integrations.plex import extract_external_ids_from_guids
            
            # Try each available URI until one works
            section_uri = None
            library_items = None
            total = 0
            
            for uri_to_try in available_uris:
                try:
                    # Get total items in section
                    library_items, total = plex_api.fetch_section_all_items(
                        plex_account.plex_token,
                        uri_to_try,
                        str(section_key),
                        start=0,
                        size=1,  # Just get the total count
                    )
                    # Success - use this URI for the rest of this section
                    section_uri = uri_to_try
                    break
                except Exception as uri_exc:
                    logger.debug("Failed to connect to Plex URI %s: %s", uri_to_try, uri_exc)
                    if uri_to_try == available_uris[-1]:
                        # Last URI failed, log and continue to next section
                        logger.warning("All Plex URIs failed for section '%s': %s", section.get("title"), uri_exc)
                        raise
            
            if not section_uri:
                continue
            
            try:
                logger.info("Section '%s' has %d total items (using URI: %s)", section.get("title"), total, section_uri)
                
                found_match = False
                rating_key = None
                item_title_lower = item.title.lower().strip()
                max_pages_to_check = 0  # Initialize for logging
                
                # Strategy: Use title-based matching with smart pagination
                # Since Plex libraries are typically sorted alphabetically, we can use a smarter approach
                # 1. Search first 100 items (fast path for common shows)
                # 2. If not found, use title-based matching to find likely positions
                # 3. Search around those positions
                
                # Fast path: Check first 100 items
                if total > 0:
                    library_items, _ = plex_api.fetch_section_all_items(
                        plex_account.plex_token,
                        section_uri,
                        str(section_key),
                        start=0,
                        size=min(100, total),
                    )
                    
                    logger.info("Checking first %d items in section '%s'", len(library_items), section.get("title"))
                    
                    for entry in library_items:
                        # Extract external IDs
                        guids = entry.get("Guid", [])
                        if not guids:
                            single_guid = entry.get("guid")
                            if single_guid:
                                guids = [{"id": single_guid}]
                        
                        external_ids = extract_external_ids_from_guids(guids)
                        
                        # Check if this matches our item by ID
                        matches = False
                        if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                            matches = True
                        elif item.source == "imdb" and external_ids.get("imdb_id") == item.media_id:
                            matches = True
                        elif item.source == "tvdb" and external_ids.get("tvdb_id") == str(item.media_id):
                            matches = True
                        
                        if matches:
                            rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                            found_match = True
                            logger.info("Found match in first 100 items by external ID")
                            break
                
                # If not found and library is large, use title-based search with smart pagination
                if not found_match and total > 100:
                    logger.info("Item not found in first 100 items, using title-based search (total: %d items)", total)
                    
                    # Strategy: Search in chunks, prioritizing title matches
                    # Since libraries are often sorted alphabetically, we can search more efficiently
                    # by checking items that might match by title first
                    
                    page_size = 100
                    max_pages_to_check = min(50, (total + page_size - 1) // page_size)  # Check up to 50 pages (5000 items)
                    
                    # Search through pages, prioritizing title matches
                    for page in range(1, max_pages_to_check + 1):
                        start = (page - 1) * page_size
                        
                        try:
                            page_items, _ = plex_api.fetch_section_all_items(
                                plex_account.plex_token,
                                section_uri,
                                str(section_key),
                                start=start,
                                size=page_size,
                            )
                            
                            if not page_items:
                                break
                            
                            # First pass: Check external IDs (most reliable)
                            for entry in page_items:
                                guids = entry.get("Guid", [])
                                if not guids:
                                    single_guid = entry.get("guid")
                                    if single_guid:
                                        guids = [{"id": single_guid}]
                                
                                external_ids = extract_external_ids_from_guids(guids)
                                
                                # Check if this matches our item by ID
                                matches = False
                                if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                                    matches = True
                                elif item.source == "imdb" and external_ids.get("imdb_id") == item.media_id:
                                    matches = True
                                elif item.source == "tvdb" and external_ids.get("tvdb_id") == str(item.media_id):
                                    matches = True
                                
                                if matches:
                                    rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                                    found_match = True
                                    logger.info("Found match at position %d-%d by external ID", start, start + len(page_items))
                                    break
                            
                            if found_match:
                                break
                            
                            # Second pass: Check title matches (if no external ID match)
                            # Only do this if we haven't found a match yet
                            for entry in page_items:
                                entry_title = entry.get("title", "").lower().strip()
                                
                                # Check for title match (exact or close)
                                title_matches = (
                                    entry_title == item_title_lower or
                                    item_title_lower in entry_title or
                                    entry_title in item_title_lower
                                )
                                
                                if title_matches:
                                    # Title matches - verify with external IDs if available
                                    guids = entry.get("Guid", [])
                                    if not guids:
                                        single_guid = entry.get("guid")
                                        if single_guid:
                                            guids = [{"id": single_guid}]
                                    
                                    external_ids = extract_external_ids_from_guids(guids)
                                    
                                    # If we have external IDs, verify they match
                                    if external_ids:
                                        if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                                            rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                                            found_match = True
                                            logger.info("Found match at position %d-%d by title + external ID", start, start + len(page_items))
                                            break
                                        # If external IDs don't match, skip (might be a different show with similar title)
                                    else:
                                        # No external IDs but title matches - use as fallback
                                        # This is less reliable but better than nothing
                                        rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                                        found_match = True
                                        logger.info("Found match at position %d-%d by title only (no external IDs)", start, start + len(page_items))
                                        break
                            
                            if found_match:
                                break
                            
                            # Log progress every 10 pages
                            if page % 10 == 0:
                                logger.info("Searched %d/%d pages (%d items) in section '%s'", page, max_pages_to_check, start + len(page_items), section.get("title"))
                                
                        except Exception as page_exc:
                            logger.debug("Error searching page %d (start=%d): %s", page, start, page_exc)
                            continue
                
                if found_match and rating_key and section_uri:
                    logger.info("Found matching Plex item for %s - %s (rating_key=%s) in section '%s'", 
                               user.username, item.title, rating_key, section.get("title"))
                    # Trigger webhook-style update
                    result = update_collection_metadata_from_plex_webhook(
                        user_id=user_id,
                        item_id=item_id,
                        rating_key=str(rating_key),
                        plex_uri=section_uri,
                        plex_token=plex_account.plex_token,
                    )
                    logger.info("Webhook task completed for %s - %s, returning entry_id=%s", 
                               user.username, item.title, result)
                    return result
                else:
                    searched_count = min(max_pages_to_check * 100, total) if max_pages_to_check > 0 and total > 100 else min(100, total)
                    logger.info("Could not find matching Plex item for %s - %s in section '%s' (searched %d/%d items)", 
                               user.username, item.title, section.get("title"), searched_count, total)
            except Exception as exc:
                logger.warning("Error searching section '%s' for item %s: %s", section.get("title"), item.title, exc, exc_info=True)
                logger.info("Continuing to search other sections...")
                continue
    except Exception as exc:
        logger.warning("Failed to fetch collection metadata for %s - %s: %s", user.username, item.title, exc, exc_info=True)
        return None
    
    logger.info("Could not find matching Plex item for %s - %s in any section", user.username, item.title)
    return None


def _aggregate_tv_show_collection_metadata(
    token: str, 
    uri: str, 
    show_rating_key: str, 
    show_metadata: dict | None = None,
    fetch_episode_details: bool = True
) -> tuple[dict, list]:
    """Aggregate collection metadata from all episodes of a TV show.
    
    Similar to how we aggregate music track metadata at the album level,
    this function fetches episodes and aggregates their collection metadata
    at the show level.
    
    Args:
        token: Plex authentication token
        uri: Plex server URI
        show_rating_key: Plex rating key for the TV show
        show_metadata: Optional already-fetched show metadata to avoid duplicate API call
        fetch_episode_details: If False, skip fetching detailed metadata for each episode
                              (only fetch episode lists for episode entry creation)
        
    Returns:
        Tuple of (aggregated_metadata_dict, episode_list) where:
        - aggregated_metadata_dict: Dictionary with aggregated collection metadata (most common values)
        - episode_list: List of dicts with keys: season_number, episode_number, collection_metadata
    """
    from integrations import plex as plex_api
    from app.collection_helpers import extract_collection_metadata_from_plex
    import requests
    from django.conf import settings
    
    result = {
        "resolution": "",
        "hdr": "",
        "audio_codec": "",
        "audio_channels": "",
        "bitrate": None,
        "media_type": "",
    }
    
    # Use provided show_metadata or fetch it
    if show_metadata is None:
        show_metadata = plex_api.fetch_metadata(token, uri, show_rating_key)
    
    if not show_metadata:
        return result, []
    
    show_key = show_metadata.get("key")
    if not show_key:
        logger.debug("No key found for show rating_key %s", show_rating_key)
        return result, []
    
    # The show key may already include /children, so check before appending
    if not show_key.endswith("/children"):
        seasons_key = f"{show_key}/children"
    else:
        seasons_key = show_key
    
    # Fetch seasons using the seasons key
    try:
        response = requests.get(
            f"{uri}{seasons_key}",
            headers=plex_api._headers(token),
            params={"X-Plex-Token": token},
            timeout=20,
            verify=settings.PLEX_SSL_VERIFY,
        )
        if not response.ok:
            logger.debug("Failed to fetch seasons for show %s: %s", show_rating_key, response.status_code)
            return result, []
        
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            payload = response.json()
            container = payload.get("MediaContainer") or {}
            # Seasons are typically in Metadata array (type="season")
            # Directory may contain aggregate entries like "All episodes"
            # Prefer Metadata, but fall back to Directory if needed
            metadata_seasons = [s for s in (container.get("Metadata") or []) if s.get("type") == "season"]
            if metadata_seasons:
                seasons = metadata_seasons
            else:
                # Fall back to Directory, but filter out aggregate entries
                seasons = [s for s in (container.get("Directory") or []) if "allLeaves" not in s.get("key", "")]
        else:
            # XML parsing would go here, but for now just return empty
            logger.debug("XML response not yet supported for season children")
            return result, []
    except Exception as exc:
        logger.debug("Error fetching seasons for show %s: %s", show_rating_key, exc)
        return result, []
    
    if not seasons:
        logger.debug("No seasons found for show rating_key %s", show_rating_key)
        return result, []
    
    # Collect metadata from all episodes across all seasons
    # Store both aggregated data and individual episode data
    all_episode_metadata = []  # For aggregation
    episode_list = []  # For individual episode entries
    
    for season in seasons:
        season_key = season.get("key")
        if not season_key:
            continue
        
        # Skip "All episodes" or similar aggregate entries
        if "allLeaves" in season_key:
            continue
        
        # Get season number from season metadata
        # For seasons, the index is the season number
        season_number = season.get("index")
        if season_number is None:
            logger.debug("No season number found for season key %s", season_key)
            continue
        
        # Season key may already include /children, so check before appending
        if not season_key.endswith("/children"):
            episodes_key = f"{season_key}/children"
        else:
            episodes_key = season_key
        
        # Fetch episodes using the episodes key
        try:
            season_response = requests.get(
                f"{uri}{episodes_key}",
                headers=plex_api._headers(token),
                params={"X-Plex-Token": token},
                timeout=20,
                verify=settings.PLEX_SSL_VERIFY,
            )
            if not season_response.ok:
                continue
            
            season_content_type = season_response.headers.get("Content-Type", "")
            if "json" in season_content_type:
                season_payload = season_response.json()
                season_container = season_payload.get("MediaContainer") or {}
                episodes = season_container.get("Metadata") or []
            else:
                continue
            
            # Extract collection metadata from each episode
            for episode in episodes:
                episode_rating_key = episode.get("ratingKey")
                if not episode_rating_key:
                    continue
                
                # Get episode number from episode metadata
                # For episodes, the index is the episode number
                episode_number = episode.get("index")
                if episode_number is None:
                    logger.debug("No episode number found for episode rating_key %s", episode_rating_key)
                    continue
                
                episode_collection = {}
                
                # Check if episode list response includes Media array with collection metadata
                episode_media = episode.get("Media")
                if episode_media and isinstance(episode_media, list) and len(episode_media) > 0:
                    # Try to extract collection metadata from episode list response
                    # Create a temporary metadata dict with Media array for extraction
                    temp_episode_metadata = {"Media": episode_media}
                    episode_collection = extract_collection_metadata_from_plex(temp_episode_metadata)
                
                # Only fetch detailed episode metadata if:
                # 1. fetch_episode_details is True AND
                # 2. We don't have collection metadata from the list response
                if fetch_episode_details and not any(episode_collection.values()):
                    try:
                        episode_metadata = plex_api.fetch_metadata(token, uri, str(episode_rating_key))
                        if episode_metadata:
                            episode_collection = extract_collection_metadata_from_plex(episode_metadata)
                    except Exception as exc:
                        logger.debug("Failed to fetch episode metadata for %s: %s", episode_rating_key, exc)
                        continue
                
                # Add to lists if we have collection metadata
                if any(episode_collection.values()):
                    # Add to aggregation list
                    all_episode_metadata.append(episode_collection)
                    # Add to episode list with season/episode numbers
                    episode_list.append({
                        "season_number": int(season_number),
                        "episode_number": int(episode_number),
                        "collection_metadata": episode_collection,
                    })
        except Exception as exc:
            logger.debug("Error fetching episodes for season %s: %s", season_key, exc)
            continue
    
    if not all_episode_metadata:
        logger.debug("No episode metadata found for show rating_key %s", show_rating_key)
        return result, []
    
    # Aggregate metadata - find most common values (like music album aggregation)
    resolutions = {}
    hdrs = {}
    audio_codecs = {}
    audio_channels_list = {}
    bitrates = {}
    media_types = {}
    
    for ep_meta in all_episode_metadata:
        if ep_meta.get("resolution"):
            resolutions[ep_meta["resolution"]] = resolutions.get(ep_meta["resolution"], 0) + 1
        if ep_meta.get("hdr"):
            hdrs[ep_meta["hdr"]] = hdrs.get(ep_meta["hdr"], 0) + 1
        if ep_meta.get("audio_codec"):
            audio_codecs[ep_meta["audio_codec"]] = audio_codecs.get(ep_meta["audio_codec"], 0) + 1
        if ep_meta.get("audio_channels"):
            audio_channels_list[ep_meta["audio_channels"]] = audio_channels_list.get(ep_meta["audio_channels"], 0) + 1
        if ep_meta.get("bitrate"):
            bitrates[ep_meta["bitrate"]] = bitrates.get(ep_meta["bitrate"], 0) + 1
        if ep_meta.get("media_type"):
            media_types[ep_meta["media_type"]] = media_types.get(ep_meta["media_type"], 0) + 1
    
    # Get most common value (or first if tie)
    result["resolution"] = max(resolutions.items(), key=lambda x: x[1])[0] if resolutions else ""
    result["hdr"] = max(hdrs.items(), key=lambda x: x[1])[0] if hdrs else ""
    result["audio_codec"] = max(audio_codecs.items(), key=lambda x: x[1])[0] if audio_codecs else ""
    result["audio_channels"] = max(audio_channels_list.items(), key=lambda x: x[1])[0] if audio_channels_list else ""
    result["bitrate"] = max(bitrates.items(), key=lambda x: x[1])[0] if bitrates else None
    result["media_type"] = max(media_types.items(), key=lambda x: x[1])[0] if media_types else ""
    
    logger.debug(
        "Aggregated collection metadata from %d episodes for show rating_key %s: %s",
        len(all_episode_metadata),
        show_rating_key,
        {k: v for k, v in result.items() if v},
    )
    
    return result, episode_list


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
    match_stats = {"tmdb": 0, "imdb": 0, "tvdb": 0, "unmatched": 0}
    
    logger.info(
        "Starting collection metadata update for user %s: %d tracked items (Movies: %d, TV: %d, Anime: %d, Music: %d)",
        user.username,
        user_items.count(),
        user_movies.count(),
        user_tv.count(),
        user_anime.count(),
        user_music.count(),
    )

    # Get user's tracked media items (Movies, TV, Anime, Music) that could have collection entries
    from app.models import Movie, TV, Music, Anime
    user_movies = Movie.objects.filter(user=user).select_related("item")
    user_tv = TV.objects.filter(user=user).select_related("item")
    user_anime = Anime.objects.filter(user=user).select_related("item")
    user_music = Music.objects.filter(user=user).select_related("item")

    all_user_items = list(user_movies.values_list("item_id", flat=True))
    all_user_items.extend(user_tv.values_list("item_id", flat=True))
    all_user_items.extend(user_anime.values_list("item_id", flat=True))
    all_user_items.extend(user_music.values_list("item_id", flat=True))

    if not all_user_items:
        logger.info("No tracked media found for user %s, nothing to update", user.username)
        return {"updated": 0, "errors": 0, "message": "No tracked media found"}

    user_items = Item.objects.filter(id__in=all_user_items).select_related()

    # Build a mapping of Yamtrack items by external IDs for efficient lookup
    # Maps (source, media_id) -> item for quick matching
    yamtrack_items_by_id = {}
    for item in user_items:
        if item.media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value):
            # Primary key: (source, media_id)
            yamtrack_items_by_id[(item.source, item.media_id)] = item

    # Process each section: query full library and match items
    for section in sections:
        section_type = (section.get("type") or "").lower()
        # Only process movie and show sections (Anime maps to show sections in Plex)
        if section_type not in ("movie", "show"):
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

        # Get section key for querying all items
        # Plex sections can have "key" (path like "/library/sections/1") or "id" (numeric)
        section_key = section.get("key") or section.get("id")
        if not section_key:
            logger.warning("Section %s has no key or id", section.get("title"))
            continue
        
        # If key is a path, extract just the numeric ID for the API call
        if isinstance(section_key, str) and section_key.startswith("/library/sections/"):
            section_key = section_key.split("/")[-1]

        try:
            # First, try to get cached rating keys from CollectionEntry records
            # This avoids scanning the library for items we've seen before via webhooks
            cached_rating_keys = {}  # Maps (source, media_id) -> (rating_key, plex_uri)
            cached_count = 0
            
            # Get cached rating keys for items in this section type
            section_items = [
                item for item in user_items
                if (item.media_type == MediaTypes.MOVIE.value and section_type == "movie") or
                   (item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and section_type == "show")
            ]
            
            if section_items:
                # Query CollectionEntry for cached rating keys
                item_ids = [item.id for item in section_items]
                cached_entries = CollectionEntry.objects.filter(
                    user=user,
                    item_id__in=item_ids,
                    plex_rating_key__isnull=False,
                ).select_related("item")
                
                for entry in cached_entries:
                    item = entry.item
                    if entry.plex_rating_key and entry.plex_uri:
                        # Store by (source, media_id) for easy lookup
                        cached_rating_keys[(item.source, item.media_id)] = (
                            entry.plex_rating_key,
                            entry.plex_uri,
                        )
                        # Also store by just TMDB if source is tmdb
                        if item.source == "tmdb":
                            cached_rating_keys[("tmdb", item.media_id)] = (
                                entry.plex_rating_key,
                                entry.plex_uri,
                            )
                        cached_count += 1
                
                logger.info(
                    "Found %d cached rating keys for section %s (out of %d items)",
                    cached_count,
                    section.get("title"),
                    len(section_items),
                )
            
            # Build list of items that need library scanning (no cached rating key)
            items_needing_scan = [
                item for item in section_items
                if (item.source, item.media_id) not in cached_rating_keys and
                   (item.source != "tmdb" or ("tmdb", item.media_id) not in cached_rating_keys)
            ]
            
            # Query all items in the section (not just history) with pagination
            # Only scan if we have items that need it
            rating_key_map = {}  # Maps (source, media_id) -> rating_key
            # Start with cached rating keys
            for key, (rating_key, uri) in cached_rating_keys.items():
                rating_key_map[key] = rating_key
            
            if not items_needing_scan:
                logger.info(
                    "All items in section %s have cached rating keys, skipping library scan",
                    section.get("title"),
                )
            else:
                logger.info(
                    "Scanning library for %d items without cached rating keys in section %s",
                    len(items_needing_scan),
                    section.get("title"),
                )
                
                # Build set of external IDs we're looking for (for early stopping)
                target_tmdb_ids = set()
                for item in items_needing_scan:
                    if item.source == "tmdb":
                        target_tmdb_ids.add(str(item.media_id))
                
                start = 0
                page_size = 1000
                total_items = None
                items_found = 0
                
                while True:
                    # Early stopping: if we've found all items needing scan, stop
                    if items_found >= len(target_tmdb_ids):
                        logger.info(
                            "Found all %d uncached items in section %s, stopping scan",
                            len(target_tmdb_ids),
                            section.get("title"),
                        )
                        break
                    library_items, total = plex_api.fetch_section_all_items(
                        plex_account.plex_token,
                        plex_uri,
                        str(section_key),
                        start=start,
                        size=page_size,
                    )
                    
                    if total_items is None:
                        total_items = total
                        logger.debug(
                            "Processing section %s: %d total items (scanning for %d uncached items)",
                            section.get("title"),
                            total_items,
                            len(target_tmdb_ids),
                        )
                    
                    if not library_items:
                        break
                    
                    # Build mapping of external IDs to rating keys from library items
                    processed_count = 0
                    for entry in library_items:
                        # Early stopping check
                        if items_found >= len(target_tmdb_ids):
                            break
                        
                        rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                        if not rating_key:
                            continue

                        processed_count += 1

                        # Extract external IDs from entry
                        guids = entry.get("Guid", [])
                        if not guids:
                            single_guid = entry.get("guid")
                            if single_guid:
                                guids = [{"id": single_guid}]

                        # Extract external IDs using helper function
                        external_ids = plex_api.extract_external_ids_from_guids(guids)
                        
                        # Track whether we fetched detailed metadata (for rate limiting)
                        fetched_detailed_metadata = False
                        
                        # If no external IDs found and we only have plex:// GUIDs, fetch detailed metadata
                        if not external_ids and guids:
                            guid_value = guids[0].get("id") if isinstance(guids[0], dict) else guids[0]
                            if guid_value and guid_value.startswith("plex://"):
                                # Fetch detailed metadata to get external IDs
                                try:
                                    detailed_metadata = plex_api.fetch_metadata(
                                        plex_account.plex_token,
                                        plex_uri,
                                        str(rating_key),
                                    )
                                    if detailed_metadata:
                                        fetched_detailed_metadata = True
                                        # Extract GUIDs from detailed metadata
                                        detailed_guids = detailed_metadata.get("Guid", [])
                                        if not detailed_guids:
                                            single_guid = detailed_metadata.get("guid")
                                            if single_guid:
                                                detailed_guids = [{"id": single_guid}]
                                        
                                        # Extract external IDs from detailed metadata
                                        external_ids = plex_api.extract_external_ids_from_guids(detailed_guids)
                                except Exception as exc:
                                    logger.debug(
                                        "Failed to fetch detailed metadata for ratingKey %s: %s",
                                        rating_key,
                                        exc,
                                    )
                                    continue
                        
                        # Store all external IDs in the map (TMDB, IMDB, TVDB)
                        # This allows matching by any external ID, not just TMDB
                        # Track if we found a target item for early stopping
                        if "tmdb_id" in external_ids:
                            tmdb_id = external_ids["tmdb_id"]
                            rating_key_map[("tmdb", tmdb_id)] = rating_key
                            if str(tmdb_id) in target_tmdb_ids:
                                items_found += 1
                        if "imdb_id" in external_ids:
                            imdb_id = external_ids["imdb_id"]
                            rating_key_map[("imdb", imdb_id)] = rating_key
                        if "tvdb_id" in external_ids:
                            tvdb_id = external_ids["tvdb_id"]
                            rating_key_map[("tvdb", tvdb_id)] = rating_key
                        
                        # Log progress every 100 items
                        if processed_count % 100 == 0:
                            logger.info(
                                "Processed %d/%d items in section %s (found %d with external IDs, %d/%d target items found)",
                                processed_count,
                                total_items,
                                section.get("title"),
                                len(rating_key_map),
                                items_found,
                                len(target_tmdb_ids),
                            )
                        
                        # Add small delay to avoid overwhelming Plex server (only when fetching detailed metadata)
                        if fetched_detailed_metadata:
                            time.sleep(0.05)  # 50ms delay between metadata fetches
                    
                    # Check if we need to paginate
                    start += len(library_items)
                    if start >= total or len(library_items) == 0:
                        break

            # Count external ID types found in this section
            tmdb_count = sum(1 for k in rating_key_map.keys() if k[0] == "tmdb")
            imdb_count = sum(1 for k in rating_key_map.keys() if k[0] == "imdb")
            tvdb_count = sum(1 for k in rating_key_map.keys() if k[0] == "tvdb")
            
            logger.info(
                "Built rating key map for section %s: %d total mappings (TMDB: %d, IMDB: %d, TVDB: %d)",
                section.get("title"),
                len(rating_key_map),
                tmdb_count,
                imdb_count,
                tvdb_count,
            )

            # Now match Yamtrack items with rating keys and update collection metadata
            for item in user_items:
                # Only process Movies, TV Shows, and Anime
                if item.media_type not in (MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value):
                    continue

                # For Anime, search in show sections
                if item.media_type == MediaTypes.ANIME.value and section_type != "show":
                    continue

                # Try to find rating key for this item
                # First check cached rating keys (fast path)
                cached_key_data = cached_rating_keys.get((item.source, item.media_id))
                if not cached_key_data and item.source == "tmdb":
                    cached_key_data = cached_rating_keys.get(("tmdb", item.media_id))
                
                rating_key = None
                match_type = None
                item_plex_uri = plex_uri  # Default to section URI
                
                if cached_key_data:
                    # Use cached rating key (fast path - no scanning needed)
                    rating_key, item_plex_uri = cached_key_data
                    match_type = "cached"
                    logger.debug("Using cached rating key for %s", item.title)
                else:
                    # Fallback to rating_key_map (from library scan)
                    # First try direct match by (source, media_id)
                    rating_key = rating_key_map.get((item.source, item.media_id))
                    
                    # If not found and source is TMDB, item.media_id should match
                    if not rating_key and item.source == "tmdb":
                        rating_key = rating_key_map.get(("tmdb", item.media_id))
                        if rating_key:
                            match_type = "tmdb"
                
                # Fallback: If TMDB match failed, fetch TMDB metadata to get IMDB/TVDB IDs and try matching by those
                if not rating_key and item.source == "tmdb":
                    try:
                        from app.providers import services
                        tmdb_metadata = services.get_media_metadata(
                            item.media_type,
                            item.media_id,
                            item.source,
                        )
                        
                        # Try IMDB match
                        if tmdb_metadata:
                            # For movies, need to fetch raw TMDB API response to get external_ids
                            if item.media_type == MediaTypes.MOVIE.value:
                                import requests
                                from django.conf import settings
                                url = f"https://api.themoviedb.org/3/movie/{item.media_id}"
                                params = {
                                    "api_key": settings.TMDB_API,
                                    "language": "en",
                                    "append_to_response": "external_ids",
                                }
                                raw_response = requests.get(url, params=params, timeout=10).json()
                                external_ids = raw_response.get("external_ids", {})
                                imdb_id = external_ids.get("imdb_id")
                            elif item.media_type == MediaTypes.TV.value:
                                # TV shows have tvdb_id directly in processed metadata
                                tvdb_id = tmdb_metadata.get("tvdb_id")
                                if tvdb_id:
                                    rating_key = rating_key_map.get(("tvdb", str(tvdb_id)))
                                if rating_key:
                                    match_type = "tvdb"
                                    logger.debug(
                                        "Matched %s by TVDB ID %s (fallback)",
                                        item.title,
                                        tvdb_id,
                                    )
                                
                                # Also try IMDB for TV shows
                                import requests
                                from django.conf import settings
                                url = f"https://api.themoviedb.org/3/tv/{item.media_id}"
                                params = {
                                    "api_key": settings.TMDB_API,
                                    "language": "en",
                                    "append_to_response": "external_ids",
                                }
                                raw_response = requests.get(url, params=params, timeout=10).json()
                                external_ids = raw_response.get("external_ids", {})
                                imdb_id = external_ids.get("imdb_id")
                            else:
                                imdb_id = None
                            
                            # Try IMDB match if we got an IMDB ID
                            if not rating_key and imdb_id:
                                rating_key = rating_key_map.get(("imdb", imdb_id))
                                if rating_key:
                                    match_type = "imdb"
                                    logger.debug(
                                        "Matched %s by IMDB ID %s (fallback)",
                                        item.title,
                                        imdb_id,
                                    )
                    except Exception as exc:
                        logger.debug(
                            "Failed to fetch TMDB metadata for fallback matching %s: %s",
                            item.title,
                            exc,
                        )

                if not rating_key:
                    match_stats["unmatched"] += 1
                    # Log unmatched items at debug level (can be enabled for troubleshooting)
                    if match_stats["unmatched"] % 100 == 0:
                        logger.debug(
                            "Unmatched items so far: %d (latest: %s - %s, source=%s, media_id=%s)",
                            match_stats["unmatched"],
                            item.title,
                            item.media_type,
                            item.source,
                            item.media_id,
                        )
                    continue
                
                # Track match type
                if match_type:
                    if match_type == "cached":
                        # Count cached matches separately for statistics
                        if "cached" not in match_stats:
                            match_stats["cached"] = 0
                        match_stats["cached"] += 1
                        match_type = "tmdb"  # Use tmdb for logging purposes
                    match_stats[match_type] += 1
                else:
                    match_stats["tmdb"] += 1  # Default to TMDB if match_type not set

                # Fetch detailed metadata and update collection
                try:
                    plex_metadata = plex_api.fetch_metadata(
                        plex_account.plex_token,
                        item_plex_uri,  # Use cached URI if available, otherwise section URI
                        str(rating_key),
                    )
                    if not plex_metadata:
                        continue

                    collection_metadata = extract_collection_metadata_from_plex(plex_metadata)
                    episode_list = []  # Will be populated for TV shows
                    
                    # For TV shows, always fetch episode list to create episode entries
                    # If show-level metadata is empty, also use aggregated metadata from episodes
                    if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
                        # Only fetch detailed episode metadata if show-level metadata is empty
                        # (needed for aggregation). Otherwise, just get episode lists.
                        fetch_episode_details = not any(collection_metadata.values())
                        
                        aggregated_metadata, episode_list = _aggregate_tv_show_collection_metadata(
                            plex_account.plex_token,
                            item_plex_uri,  # Use cached URI if available
                            str(rating_key),
                            show_metadata=plex_metadata,  # Pass already-fetched metadata to avoid duplicate call
                            fetch_episode_details=fetch_episode_details,
                        )
                        
                        # Only use aggregated metadata if show-level metadata is empty
                        if not any(collection_metadata.values()):
                            logger.debug(
                                "Show-level metadata empty for %s, using aggregated metadata from episodes",
                                item.title,
                            )
                            collection_metadata = aggregated_metadata
                        else:
                            logger.debug(
                                "Show-level metadata exists for %s, using it (episode list fetched for episode entries)",
                                item.title,
                            )
                    
                    if not any(collection_metadata.values()):
                        # If no show-level metadata, but we have episodes, still create episode entries
                        if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and episode_list:
                            logger.debug(
                                "No show-level metadata for %s, but creating episode entries",
                                item.title,
                            )
                        else:
                            # Log when we skip due to no metadata (helps diagnose why items aren't updated)
                            logger.debug(
                                "Skipping %s - no collection metadata found (matched by %s)",
                                item.title,
                                match_type or "tmdb",
                            )
                            continue  # No metadata to update

                    # Get or create collection entry (only collection, no Media instances)
                    # Only create if we have metadata
                    if any(collection_metadata.values()):
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
                            entry.updated_at = timezone.now()
                            entry.save()

                        # Store rating key and URI for future bulk imports (cache for faster lookups)
                        # Update if rating key changed or wasn't set
                        if entry.plex_rating_key != rating_key or entry.plex_uri != item_plex_uri:
                            entry.plex_rating_key = rating_key
                            entry.plex_uri = item_plex_uri
                            entry.plex_rating_key_updated_at = timezone.now()
                            entry.save(update_fields=["plex_rating_key", "plex_uri", "plex_rating_key_updated_at"])
                            logger.debug(
                                "Cached rating key for %s: %s (uri=%s)",
                                item.title,
                                rating_key,
                                item_plex_uri,
                            )

                        updated_count += 1
                        logger.debug(
                            "Updated collection metadata for %s - %s",
                            user.username,
                            item.title,
                        )
                    
                    # For TV shows, also create episode-level collection entries
                    if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and episode_list:
                        episode_entries_created = 0
                        episode_entries_updated = 0
                        
                        for episode_data in episode_list:
                            season_number = episode_data["season_number"]
                            episode_number = episode_data["episode_number"]
                            episode_collection_metadata = episode_data["collection_metadata"]
                            
                            # Skip Season 0 (Specials) to match Details pane behavior
                            if season_number == 0:
                                continue
                            
                            # Find or create the episode Item
                            try:
                                episode_item, episode_item_created = Item.objects.get_or_create(
                                    media_id=item.media_id,
                                    source=item.source,
                                    media_type=MediaTypes.EPISODE.value,
                                    season_number=season_number,
                                    episode_number=episode_number,
                                    defaults={
                                        "title": f"Episode {episode_number}",
                                        "image": item.image,
                                    },
                                )
                                
                                # Create or update collection entry for this episode
                                episode_entry, episode_entry_created = CollectionEntry.objects.get_or_create(
                                    user=user,
                                    item=episode_item,
                                    defaults=episode_collection_metadata,
                                )
                                
                                if episode_entry_created:
                                    episode_entries_created += 1
                                else:
                                    # Update existing entry
                                    updated = False
                                    for key, value in episode_collection_metadata.items():
                                        if value:  # Only update non-empty values
                                            old_value = getattr(episode_entry, key, None)
                                            if old_value != value:
                                                setattr(episode_entry, key, value)
                                                updated = True
                                    if updated:
                                        episode_entry.updated_at = timezone.now()
                                        episode_entry.save()
                                        episode_entries_updated += 1
                                        
                            except Exception as exc:
                                logger.debug(
                                    "Failed to create collection entry for episode S%02dE%02d of %s: %s",
                                    season_number,
                                    episode_number,
                                    item.title,
                                    exc,
                                )
                                continue
                        
                        if episode_entries_created > 0 or episode_entries_updated > 0:
                            logger.info(
                                "Created %d and updated %d episode collection entries for %s - %s",
                                episode_entries_created,
                                episode_entries_updated,
                                user.username,
                                item.title,
                            )
                except Exception as exc:
                    # Log timeout errors separately for better visibility
                    if "timeout" in str(exc).lower() or "ReadTimeout" in str(type(exc).__name__):
                        logger.warning(
                            "Timeout fetching collection metadata for %s: %s (rating_key=%s)",
                            item.title,
                            exc,
                            rating_key,
                        )
                    else:
                        logger.warning(
                            "Failed to update collection metadata for %s: %s (rating_key=%s)",
                            item.title,
                            exc,
                            rating_key,
                            exc_info=True,
                        )
                    error_count += 1

        except Exception as exc:
            logger.warning(
                "Failed to process section %s: %s",
                section.get("title"),
                exc,
                exc_info=True,
            )
            error_count += 1
            continue

        # Log final statistics for this section
        total_processed = sum(match_stats.values())
        if total_processed > 0:
            logger.info(
                "Section %s matching statistics: TMDB: %d, IMDB: %d, TVDB: %d, Unmatched: %d (Total: %d)",
                section.get("title"),
                match_stats["tmdb"],
                match_stats["imdb"],
                match_stats["tvdb"],
                match_stats["unmatched"],
                total_processed,
            )
        
        # Reset match_stats for next section
        match_stats = {"tmdb": 0, "imdb": 0, "tvdb": 0, "unmatched": 0}
    
    # Log final summary across all sections
    logger.info(
        "Collection update task completed: %d items updated, %d errors",
        updated_count,
        error_count,
    )
    
    return {
        "updated": updated_count,
        "errors": error_count,
        "message": f"Updated collection metadata for {updated_count} items",
    }
