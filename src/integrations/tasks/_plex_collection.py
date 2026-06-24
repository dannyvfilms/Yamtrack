import logging
import time

import requests
from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from app.collection_helpers import extract_collection_metadata_from_plex
from app.log_safety import exception_summary, safe_url
from app.models import CollectionEntry, Item, MediaTypes
from integrations import plex as plex_api
from integrations.plex import extract_external_ids_from_guids
from integrations.tasks._import_helpers import _is_expected_plex_lookup_error

logger = logging.getLogger(__name__)


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
    logger.info(
        "Starting collection metadata update task (user_id=%s item_id=%s uri=%s)",
        user_id,
        item_id,
        safe_url(plex_uri),
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
        logger.debug(
            "Fetching Plex metadata for collection update via %s",
            safe_url(plex_uri),
        )
        plex_metadata = plex_api.fetch_metadata(plex_token, plex_uri, rating_key)
    except Exception as exc:
        # Check if this is a timeout (expected network issue)
        is_timeout = (
            "timeout" in str(exc).lower() or
            "ReadTimeout" in str(type(exc).__name__) or
            "TimeoutError" in str(type(exc).__name__)
        )
        
        if is_timeout:
            # Timeouts are expected - log at debug level
            logger.debug(
                "Timeout fetching Plex metadata via %s: %s",
                safe_url(plex_uri),
                exception_summary(exc),
            )
        else:
            # Other errors are more serious - log as warning
            logger.warning(
                "Failed to fetch Plex metadata for collection update via %s: %s. "
                "This may indicate the URI is incorrect or the server is unreachable.",
                safe_url(plex_uri),
                exception_summary(exc),
                exc_info=True,
            )
        
        # If HTTP failed, try HTTPS (some servers require HTTPS)
        if plex_uri.startswith("http://") and "500" in str(exc):
            https_uri = plex_uri.replace("http://", "https://")
            logger.debug("Retrying collection update with HTTPS: %s", safe_url(https_uri))
            try:
                plex_metadata = plex_api.fetch_metadata(plex_token, https_uri, rating_key)
                logger.info("Successfully fetched metadata using HTTPS URI")
            except Exception as https_exc:
                logger.debug(
                    "HTTPS retry for collection update also failed: %s",
                    exception_summary(https_exc),
                )
                return None
        else:
            return None

    if not plex_metadata:
        logger.debug("No Plex metadata returned for collection update")
        return None

    logger.debug("Received Plex metadata with keys: %s", list(plex_metadata.keys()))

    # Extract collection metadata
    collection_metadata = extract_collection_metadata_from_plex(plex_metadata)
    logger.debug(
        "Extracted collection metadata: %s",
        {k: v for k, v in collection_metadata.items() if v},
    )

    # Update the most recent entry for this item, or create a new one if none exists.
    entry = (
        CollectionEntry.objects.filter(
            user=user,
            item=item,
        )
        .order_by("-updated_at", "-collected_at", "-id")
        .first()
    )
    created = entry is None
    if created:
        entry = CollectionEntry(
            user=user,
            item=item,
            **collection_metadata,
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
                logger.debug("Updated cached Plex collection lookup details")
        else:
            logger.debug("No changes to collection entry")
    else:
        # New entry - save initial metadata and rating key cache.
        entry.save()
        if rating_key_updated:
            logger.debug("Stored cached Plex collection lookup details for new entry")

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
                    
                    # Update the most recent episode entry, or create one if needed.
                    episode_entry = (
                        CollectionEntry.objects.filter(
                            user=user,
                            item=episode_item,
                        )
                        .order_by("-updated_at", "-collected_at", "-id")
                        .first()
                    )
                    episode_entry_created = episode_entry is None
                    if episode_entry_created:
                        episode_entry = CollectionEntry(
                            user=user,
                            item=episode_item,
                            **episode_collection_metadata,
                        )
                        episode_entry.save()
                    
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
def fetch_collection_metadata_for_item(
    user_id,
    item_id,
    lookup_policy="cached_or_search",
):
    """Fetch collection metadata for a single item in the background.

    This is triggered when viewing a media details page for an item that doesn't
    have collection data yet. It attempts to find the item in Plex and create
    collection entries.

    Args:
        user_id: User ID
        item_id: Item ID in Yamtrack
        lookup_policy: Either ``cached_or_search`` or ``cached_only``
    """
    logger.info(
        "Starting collection metadata fetch for user_id=%s, item_id=%s",
        user_id,
        item_id,
    )

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        item = Item.objects.get(id=item_id)
    except (User.DoesNotExist, Item.DoesNotExist) as exc:
        logger.warning(
            "Cannot fetch collection metadata: %s (user_id=%s, item_id=%s)",
            exc,
            user_id,
            item_id,
        )
        return None

    plex_account = getattr(user, "plex_account", None)
    if not plex_account or not plex_account.plex_token:
        logger.info(
            "User %s does not have Plex connected, skipping collection fetch",
            user.username,
        )
        return None

    existing_entry = CollectionEntry.objects.filter(user=user, item=item).first()
    if existing_entry:
        logger.info(
            "Collection entry already exists for %s - %s (entry_id=%s)",
            user.username,
            item.title,
            existing_entry.id,
        )
        return existing_entry.id

    try:
        sections = plex_account.sections or []
        resources = []
        if lookup_policy != "cached_only":
            from integrations import plex as plex_api

            resources = plex_api.list_resources(plex_account.plex_token)
            if not sections:
                sections = plex_api.list_sections(plex_account.plex_token)

        lookup = _find_plex_rating_key_for_item(
            user,
            item,
            plex_account,
            sections=sections,
            resources=resources,
            lookup_policy=lookup_policy,
        )
    except Exception as exc:
        logger.warning(
            "Failed to fetch collection metadata for %s - %s: %s",
            user.username,
            item.title,
            exception_summary(exc),
            exc_info=True,
        )
        return None

    if not lookup:
        if lookup_policy == "cached_only":
            logger.info(
                "No cached Plex lookup found for %s - %s; skipping library search",
                user.username,
                item.title,
            )
            return None

        logger.info(
            "Could not find matching Plex item for %s - %s in any section",
            user.username,
            item.title,
        )
        return None

    rating_key, plex_uri, match_type = lookup
    logger.info(
        "Using %s Plex lookup for %s - %s",
        match_type or "resolved",
        user.username,
        item.title,
    )
    return update_collection_metadata_from_plex_webhook(
        user_id=user_id,
        item_id=item_id,
        rating_key=str(rating_key),
        plex_uri=plex_uri,
        plex_token=plex_account.plex_token,
    )


def _find_plex_rating_key_for_item(
    user,
    item,
    plex_account,
    sections=None,
    resources=None,
    available_uris=None,
    lookup_policy="cached_or_search",
):
    """Find Plex rating key for a Yamtrack item.

    Checks cached rating keys first, then searches Plex library if needed.

    Args:
        user: User object
        item: Item object to find rating key for
        plex_account: PlexAccount object
        sections: List of Plex sections
        resources: List of Plex resources
        available_uris: Optional list of Plex URIs to try (if None, will be determined)
        lookup_policy: Either ``cached_or_search`` or ``cached_only``

    Returns:
        Tuple of (rating_key, plex_uri, match_type) or None if not found.
        match_type can be: "cached", "tmdb", "imdb", "tvdb", or None
    """
    sections = sections or []
    resources = resources or []

    cached_entry = CollectionEntry.objects.filter(
        user=user,
        item=item,
        plex_rating_key__isnull=False,
        plex_uri__isnull=False,
    ).first()

    if cached_entry and cached_entry.plex_rating_key and cached_entry.plex_uri:
        logger.debug("Using cached Plex lookup for %s - %s", user.username, item.title)
        return (cached_entry.plex_rating_key, cached_entry.plex_uri, "cached")

    if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
        from app.models import Item as ItemModel

        episode_items = ItemModel.objects.filter(
            media_id=item.media_id,
            source=item.source,
            media_type=MediaTypes.EPISODE.value,
        )
        episode_item_ids = list(episode_items.values_list("id", flat=True))
        if episode_item_ids:
            cached_episode_entry = CollectionEntry.objects.filter(
                user=user,
                item_id__in=episode_item_ids,
                plex_rating_key__isnull=False,
                plex_uri__isnull=False,
            ).first()

            if cached_episode_entry:
                episode_rating_key = cached_episode_entry.plex_rating_key
                episode_plex_uri = cached_episode_entry.plex_uri

                try:
                    episode_metadata = plex_api.fetch_metadata(
                        plex_account.plex_token,
                        episode_plex_uri,
                        str(episode_rating_key),
                    )

                    if episode_metadata:
                        show_key = episode_metadata.get("grandparentKey")
                        if show_key and "/" in show_key:
                            show_rating_key_str = show_key.split("/")[-1]
                            try:
                                rating_key = str(int(show_rating_key_str))
                                logger.debug(
                                    "Derived show-level Plex lookup from episode metadata",
                                )
                                return (rating_key, episode_plex_uri, "cached")
                            except (ValueError, TypeError):
                                pass
                except Exception as exc:
                    logger.debug(
                        "Failed to derive show-level Plex lookup from episode metadata: %s",
                        exception_summary(exc),
                    )

    if lookup_policy == "cached_only":
        return None

    if available_uris is None:
        available_uris = []
        if sections:
            for section in sections:
                if section.get("uri") and section.get("uri") not in available_uris:
                    available_uris.append(section.get("uri"))

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
            logger.debug("No Plex URIs available for user %s", user.username)
            return None

    for section in sections:
        section_type = (section.get("type") or "").lower()
        if section_type not in ("movie", "show"):
            continue

        if item.media_type == MediaTypes.MOVIE.value and section_type != "movie":
            continue
        if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and section_type != "show":
            continue

        section_key = section.get("key") or section.get("id")
        if isinstance(section_key, str) and section_key.startswith("/library/sections/"):
            section_key = section_key.split("/")[-1]

        section_uri = None
        total = 0
        for uri_to_try in available_uris:
            try:
                _library_items, total = plex_api.fetch_section_all_items(
                    plex_account.plex_token,
                    uri_to_try,
                    str(section_key),
                    start=0,
                    size=1,
                )
                section_uri = uri_to_try
                break
            except Exception as uri_exc:
                logger.debug(
                    "Failed to connect to Plex URI %s: %s",
                    safe_url(uri_to_try),
                    exception_summary(uri_exc),
                )
                if uri_to_try == available_uris[-1]:
                    continue

        if not section_uri:
            continue

        try:
            if total > 0:
                library_items, _ = plex_api.fetch_section_all_items(
                    plex_account.plex_token,
                    section_uri,
                    str(section_key),
                    start=0,
                    size=min(100, total),
                )

                for entry in library_items:
                    guids = entry.get("Guid", [])
                    if not guids:
                        single_guid = entry.get("guid")
                        if single_guid:
                            guids = [{"id": single_guid}]

                    external_ids = extract_external_ids_from_guids(guids)

                    matches = False
                    match_type = None
                    if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                        matches = True
                        match_type = "tmdb"
                    elif item.source == "imdb" and external_ids.get("imdb_id") == item.media_id:
                        matches = True
                        match_type = "imdb"
                    elif item.source == "tvdb" and external_ids.get("tvdb_id") == str(item.media_id):
                        matches = True
                        match_type = "tvdb"

                    if matches:
                        rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                        if rating_key:
                            logger.debug(
                                "Found match in first 100 items by %s",
                                match_type,
                            )
                            return (rating_key, section_uri, match_type)

            if total > 100:
                page_size = 100
                max_pages_to_check = min(50, (total + page_size - 1) // page_size)

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

                        for entry in page_items:
                            guids = entry.get("Guid", [])
                            if not guids:
                                single_guid = entry.get("guid")
                                if single_guid:
                                    guids = [{"id": single_guid}]

                            external_ids = extract_external_ids_from_guids(guids)

                            matches = False
                            match_type = None
                            if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                                matches = True
                                match_type = "tmdb"
                            elif item.source == "imdb" and external_ids.get("imdb_id") == item.media_id:
                                matches = True
                                match_type = "imdb"
                            elif item.source == "tvdb" and external_ids.get("tvdb_id") == str(item.media_id):
                                matches = True
                                match_type = "tvdb"

                            if matches:
                                rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                                if rating_key:
                                    logger.debug(
                                        "Found match at position %d-%d by %s",
                                        start,
                                        start + len(page_items),
                                        match_type,
                                    )
                                    return (rating_key, section_uri, match_type)
                    except Exception as page_exc:
                        logger.debug("Error searching page %d: %s", page, page_exc)
                        continue
        except Exception as exc:
            if _is_expected_plex_lookup_error(exc):
                logger.warning(
                    "Error searching section '%s' for item %s: %s",
                    section.get("title"),
                    item.title,
                    exception_summary(exc),
                )
            else:
                logger.warning(
                    "Error searching section '%s' for item %s: %s",
                    section.get("title"),
                    item.title,
                    exc,
                    exc_info=True,
                )
            logger.info("Continuing to search other sections...")
            continue

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
        logger.debug("No key found in Plex show metadata")
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
            logger.debug(
                "Failed to fetch Plex show seasons (status=%s)",
                response.status_code,
            )
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
        logger.debug(
            "Error fetching Plex show seasons: %s",
            exception_summary(exc),
        )
        return result, []
    
    if not seasons:
        logger.debug("No seasons found in Plex show metadata")
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
                    logger.debug("No episode number found in Plex episode metadata")
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
                        logger.debug(
                            "Failed to fetch Plex episode metadata: %s",
                            exception_summary(exc),
                        )
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
            logger.debug("Error fetching episodes for season %s: %s", season_key, exception_summary(exc))
            continue
    
    if not all_episode_metadata:
        logger.debug("No Plex episode metadata found for aggregation")
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
        "Aggregated collection metadata from %d episodes for Plex show: %s",
        len(all_episode_metadata),
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
        logger.warning("Plex token expired for user %s: %s", user.username, exception_summary(exc))
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
    match_stats = {"tmdb": 0, "imdb": 0, "tvdb": 0, "unmatched": 0, "cached": 0}
    
    # Get counts before filtering
    from app.models import Movie, TV, Music, Anime
    user_movies_count = Movie.objects.filter(user=user).count()
    user_tv_count = TV.objects.filter(user=user).count()
    user_anime_count = Anime.objects.filter(user=user).count()
    user_music_count = Music.objects.filter(user=user).count()
    
    logger.info(
        "Starting collection metadata update for user %s: tracked items (Movies: %d, TV: %d, Anime: %d, Music: %d)",
        user.username,
        user_movies_count,
        user_tv_count,
        user_anime_count,
        user_music_count,
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
    
    # Get available URIs once for reuse
    available_uris = []
    if sections:
        for section in sections:
            if section.get("uri") and section.get("uri") not in available_uris:
                available_uris.append(section.get("uri"))
        
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

    # Process each section incrementally: process cached items first, then scan in batches
    import time
    start_time = time.time()
    
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
            # Get items for this section type
            section_items = [
                item for item in user_items
                if (item.media_type == MediaTypes.MOVIE.value and section_type == "movie") or
                   (item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value) and section_type == "show")
            ]
            
            if not section_items:
                continue
            
            logger.info(
                "Processing section '%s': %d items to check",
                section.get("title"),
                len(section_items),
            )
            
            # Step 1: Process cached items immediately (fast path)
            cached_processed = 0
            cached_updated = 0
            cached_errors = 0
            
            # Get cached entries for section items
            item_ids = [item.id for item in section_items]
            cached_entries = CollectionEntry.objects.filter(
                user=user,
                item_id__in=item_ids,
                plex_rating_key__isnull=False,
                plex_uri__isnull=False,
            ).select_related("item")
            
            cached_items_map = {entry.item_id: entry for entry in cached_entries}
            
            for item in section_items:
                cached_entry = cached_items_map.get(item.id)
                if cached_entry:
                    try:
                        # Use webhook function for consistency
                        entry_id = update_collection_metadata_from_plex_webhook(
                            user_id=user.id,
                            item_id=item.id,
                            rating_key=str(cached_entry.plex_rating_key),
                            plex_uri=cached_entry.plex_uri,
                            plex_token=plex_account.plex_token,
                        )
                        if entry_id:
                            cached_updated += 1
                            updated_count += 1
                            match_stats["cached"] = match_stats.get("cached", 0) + 1
                    except Exception as exc:
                        logger.warning(
                            "Failed to update cached item %s: %s",
                            item.title,
                            exc,
                        )
                        cached_errors += 1
                        error_count += 1
                    cached_processed += 1
            
            if cached_processed > 0:
                logger.info(
                    "Processed %d cached items in section '%s': %d updated, %d errors",
                    cached_processed,
                    section.get("title"),
                    cached_updated,
                    cached_errors,
                )
            
            # Step 2: Get items that need library scanning
            items_needing_scan = [
                item for item in section_items
                if not CollectionEntry.objects.filter(
                    user=user,
                    item=item,
                    plex_rating_key__isnull=False,
                ).exists()
            ]
            
            if not items_needing_scan:
                logger.info(
                    "All items in section '%s' processed (cached or already have entries)",
                    section.get("title"),
                )
                continue
            
            # Step 3: Scan library in batches and process matches incrementally
            logger.info(
                "Scanning library for %d uncached items in section '%s'",
                len(items_needing_scan),
                section.get("title"),
            )
            
            # Build set of items we're looking for (for early stopping)
            items_to_find = set((item.source, item.media_id) for item in items_needing_scan)
            items_found_set = set()
            
            # Build mapping of items by external ID for quick lookup
            items_by_external_id = {}
            for item in items_needing_scan:
                key = (item.source, item.media_id)
                items_by_external_id[key] = item
                # Also index by TMDB if source is tmdb
                if item.source == "tmdb":
                    items_by_external_id[("tmdb", item.media_id)] = item
            
            batch_size = 500
            start = 0
            total_items = None
            batch_processed = 0
            batch_matched = 0
            section_start_time = time.time()
            
            from integrations.plex import extract_external_ids_from_guids
            
            while True:
                # Early stopping: if we've found all items, stop scanning
                if len(items_found_set) >= len(items_to_find):
                    logger.info(
                        "Found all %d uncached items in section '%s', stopping scan",
                        len(items_to_find),
                        section.get("title"),
                    )
                    break
                
                # Fetch batch of library items
                try:
                    library_items, total = plex_api.fetch_section_all_items(
                        plex_account.plex_token,
                        plex_uri,
                        str(section_key),
                        start=start,
                        size=batch_size,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to fetch batch from section '%s' (start=%d): %s",
                        section.get("title"),
                        start,
                        exc,
                    )
                    break
                
                if total_items is None:
                    total_items = total
                    logger.info(
                        "Section '%s' has %d total items (scanning for %d uncached items)",
                        section.get("title"),
                        total_items,
                        len(items_to_find),
                    )
                
                if not library_items:
                    break
                
                # Process this batch: match items and update immediately
                batch_matches = 0
                for entry in library_items:
                    rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                    if not rating_key:
                        continue
                    
                    batch_processed += 1
                    
                    # Extract external IDs
                    guids = entry.get("Guid", [])
                    if not guids:
                        single_guid = entry.get("guid")
                        if single_guid:
                            guids = [{"id": single_guid}]
                    
                    external_ids = extract_external_ids_from_guids(guids)
                    
                    # If no external IDs, try fetching detailed metadata
                    if not external_ids and guids:
                        guid_value = guids[0].get("id") if isinstance(guids[0], dict) else guids[0]
                        if guid_value and guid_value.startswith("plex://"):
                            try:
                                detailed_metadata = plex_api.fetch_metadata(
                                    plex_account.plex_token,
                                    plex_uri,
                                    str(rating_key),
                                )
                                if detailed_metadata:
                                    detailed_guids = detailed_metadata.get("Guid", [])
                                    if not detailed_guids:
                                        single_guid = detailed_metadata.get("guid")
                                        if single_guid:
                                            detailed_guids = [{"id": single_guid}]
                                    external_ids = extract_external_ids_from_guids(detailed_guids)
                            except Exception as exc:
                                logger.debug(
                                    "Failed to fetch detailed Plex metadata during collection scan: %s",
                                    exception_summary(exc),
                                )
                    
                    # Try to match this Plex item with our items
                    matched_item = None
                    match_type = None
                    
                    if "tmdb_id" in external_ids:
                        tmdb_id = external_ids["tmdb_id"]
                        key = ("tmdb", tmdb_id)
                        if key in items_by_external_id:
                            matched_item = items_by_external_id[key]
                            match_type = "tmdb"
                    
                    if not matched_item and "imdb_id" in external_ids:
                        imdb_id = external_ids["imdb_id"]
                        key = ("imdb", imdb_id)
                        if key in items_by_external_id:
                            matched_item = items_by_external_id[key]
                            match_type = "imdb"
                    
                    if not matched_item and "tvdb_id" in external_ids:
                        tvdb_id = external_ids["tvdb_id"]
                        key = ("tvdb", tvdb_id)
                        if key in items_by_external_id:
                            matched_item = items_by_external_id[key]
                            match_type = "tvdb"
                    
                    # If we found a match, process it immediately
                    if matched_item:
                        item_key = (matched_item.source, matched_item.media_id)
                        if item_key not in items_found_set:
                            try:
                                # Use webhook function for consistency
                                entry_id = update_collection_metadata_from_plex_webhook(
                                    user_id=user.id,
                                    item_id=matched_item.id,
                                    rating_key=str(rating_key),
                                    plex_uri=plex_uri,
                                    plex_token=plex_account.plex_token,
                                )
                                if entry_id:
                                    items_found_set.add(item_key)
                                    batch_matches += 1
                                    batch_matched += 1
                                    updated_count += 1
                                    match_stats[match_type] = match_stats.get(match_type, 0) + 1
                            except Exception as exc:
                                logger.warning(
                                    "Failed to update item %s: %s",
                                    matched_item.title,
                                    exc,
                                )
                                error_count += 1
                
                # Log progress after each batch
                elapsed = time.time() - section_start_time
                items_remaining = len(items_to_find) - len(items_found_set)
                match_rate = (batch_matched / batch_processed * 100) if batch_processed > 0 else 0
                
                # Estimate time remaining
                if batch_processed > 0 and total_items:
                    items_per_second = batch_processed / elapsed if elapsed > 0 else 0
                    remaining_items = total_items - start - len(library_items)
                    estimated_seconds = remaining_items / items_per_second if items_per_second > 0 else 0
                    estimated_minutes = int(estimated_seconds / 60)
                else:
                    estimated_minutes = None
                
                logger.info(
                    "Processed %d/%d items in section '%s': %d matched this batch, %d total matched (%.1f%% overall), "
                    "%d/%d target items found. Updated: %d so far%s",
                    start + len(library_items),
                    total_items or "?",
                    section.get("title"),
                    batch_matches,
                    batch_matched,
                    match_rate,
                    len(items_found_set),
                    len(items_to_find),
                    updated_count,
                    f", ~{estimated_minutes} min remaining" if estimated_minutes is not None else "",
                )
                
                # Check if we need to paginate
                start += len(library_items)
                if start >= total or len(library_items) == 0:
                    break
            # Count unmatched items
            unmatched_count = len(items_needing_scan) - len(items_found_set)
            if unmatched_count > 0:
                match_stats["unmatched"] = match_stats.get("unmatched", 0) + unmatched_count

        except Exception as exc:
            logger.warning(
                "Failed to process section %s: %s",
                section.get("title"),
                exc,
                exc_info=True,
            )
            error_count += 1
            continue

        # Log final statistics for this section (before resetting)
        section_cached = match_stats.get("cached", 0)
        section_tmdb = match_stats.get("tmdb", 0)
        section_imdb = match_stats.get("imdb", 0)
        section_tvdb = match_stats.get("tvdb", 0)
        section_unmatched = match_stats.get("unmatched", 0)
        section_total = section_cached + section_tmdb + section_imdb + section_tvdb + section_unmatched
        
        if section_total > 0:
            logger.info(
                "Section '%s' matching statistics: Cached: %d, TMDB: %d, IMDB: %d, TVDB: %d, Unmatched: %d (Total: %d)",
                section.get("title"),
                section_cached,
                section_tmdb,
                section_imdb,
                section_tvdb,
                section_unmatched,
                section_total,
            )
        
        # Reset match_stats for next section (totals are accumulated in updated_count)
        match_stats["tmdb"] = 0
        match_stats["imdb"] = 0
        match_stats["tvdb"] = 0
        match_stats["unmatched"] = 0
        match_stats["cached"] = 0
    
    # Log final summary across all sections
    total_elapsed = time.time() - start_time
    
    logger.info(
        "Collection update task completed in %.1f minutes: %d items updated, %d errors",
        total_elapsed / 60,
        updated_count,
        error_count,
    )
    
    return {
        "updated": updated_count,
        "errors": error_count,
        "message": f"Updated collection metadata for {updated_count} items",
    }
