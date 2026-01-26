#!/usr/bin/env python
"""Test script for collection update matching logic.

This script tests the matching logic on 10 movies and 1 TV show
starting alphabetically to diagnose matching issues.
"""
import os
import sys
import django

# Setup Django
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import logging
import time
from django.contrib.auth import get_user_model
from app.models import Movie, TV, Item, MediaTypes, CollectionEntry
from integrations import plex as plex_api
from app.collection_helpers import extract_collection_metadata_from_plex
from integrations.plex import extract_external_ids_from_guids
from django.utils import timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

User = get_user_model()


def test_collection_matching(user_id=1, movie_limit=10, tv_limit=1, rating_keys=None, skip_library_scan=False):
    """Test collection matching logic on a small subset of Yamtrack items.
    
    Args:
        user_id: User ID to test with
        movie_limit: Number of movies to test (default: 10)
        tv_limit: Number of TV shows to test (default: 1)
        rating_keys: Optional dict mapping (source, media_id) -> rating_key to skip library scan
        skip_library_scan: If True and rating_keys provided, skip library scanning entirely
    """
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error("User %s not found", user_id)
        return

    plex_account = getattr(user, "plex_account", None)
    if not plex_account or not plex_account.plex_token:
        logger.error("Plex not connected for user %s", user.username)
        return

    try:
        resources = plex_api.list_resources(plex_account.plex_token)
    except plex_api.PlexAuthError as exc:
        logger.error("Plex token expired: %s", exc)
        return

    logger.info("=" * 80)
    logger.info("Collection Matching Test - 10 Movies + 1 TV Show (Alphabetical)")
    logger.info("=" * 80)
    
    # Step 1: Get Yamtrack items alphabetically
    logger.info("\nStep 1: Getting Yamtrack items (alphabetically)...")
    
    # Get movies ordered by title
    movies = Movie.objects.filter(user=user).select_related("item").order_by("item__title")[:movie_limit]
    movie_items = [m.item for m in movies if m.item]
    
    # Get TV shows ordered by title
    tv_shows = TV.objects.filter(user=user).select_related("item").order_by("item__title")[:tv_limit]
    tv_items = [tv.item for tv in tv_shows if tv.item]
    
    test_items = movie_items + tv_items
    
    logger.info("Selected %d movies and %d TV shows:", len(movie_items), len(tv_items))
    for item in test_items:
        logger.info("  - %s (%s, source=%s, media_id=%s)", 
                   item.title, item.media_type, item.source, item.media_id)
    
    if not test_items:
        logger.error("No items found to test")
        return
    
    # Extract TMDB IDs we're looking for (for optimization)
    test_tmdb_ids = set()
    for item in test_items:
        if item.source == "tmdb":
            test_tmdb_ids.add(str(item.media_id))
    
    logger.info("Looking for %d TMDB IDs in Plex: %s", len(test_tmdb_ids), ", ".join(sorted(test_tmdb_ids)[:10]) + ("..." if len(test_tmdb_ids) > 10 else ""))
    
    # Step 2: Get Plex sections and build rating_key_map (only for test items)
    # First, try to get cached rating keys from CollectionEntry records
    rating_key_map = {}  # Maps (source, media_id) -> rating_key
    cached_count = 0
    
    # Get cached rating keys from CollectionEntry
    if test_items:
        item_ids = [item.id for item in test_items]
        cached_entries = CollectionEntry.objects.filter(
            user=user,
            item_id__in=item_ids,
            plex_rating_key__isnull=False,
        ).select_related("item")
        
        for entry in cached_entries:
            item = entry.item
            if entry.plex_rating_key:
                rating_key_map[(item.source, item.media_id)] = entry.plex_rating_key
                if item.source == "tmdb":
                    rating_key_map[("tmdb", item.media_id)] = entry.plex_rating_key
                cached_count += 1
        
        if cached_count > 0:
            logger.info("Found %d cached rating keys (out of %d test items)", cached_count, len(test_items))
    
    # Determine which items need library scanning
    items_needing_scan = [
        item for item in test_items
        if (item.source, item.media_id) not in rating_key_map and
           (item.source != "tmdb" or ("tmdb", item.media_id) not in rating_key_map)
    ]
    
    if skip_library_scan and rating_keys:
        logger.info("\nStep 2: Using provided rating_keys (skipping library scan)...")
        # Merge provided rating keys with cached ones
        rating_key_map.update(rating_keys)
        logger.info("Using %d provided rating keys (total: %d with cache)", len(rating_keys), len(rating_key_map))
    elif not items_needing_scan:
        logger.info("\nStep 2: All test items have cached rating keys, skipping library scan...")
        sections = plex_account.sections or []
        if not sections:
            sections = plex_api.list_sections(plex_account.plex_token)
    else:
                logger.info("\nStep 2: Querying Plex for %d items without cached rating keys...", len(items_needing_scan))
                
                sections = plex_account.sections or []
                if not sections:
                    sections = plex_api.list_sections(plex_account.plex_token)
                
                id_type_counts = {"tmdb": 0, "imdb": 0, "tvdb": 0}
                items_found = cached_count  # Start with cached items
                max_items_to_check = 2000  # Limit how many Plex items we check before giving up (reduced for small tests)
                
                # Build set of target TMDB IDs we're looking for (for early stopping)
                target_tmdb_ids = set()
                for item in items_needing_scan:
                    if item.source == "tmdb":
                        target_tmdb_ids.add(str(item.media_id))
                
                # Process each section
                for section in sections:
                    section_type = (section.get("type") or "").lower()
                    if section_type not in ("movie", "show"):
                        continue
                    
                    # Get server URI
                    connections = []
                    if section.get("uri"):
                        connections.append(section.get("uri"))
                    for resource in resources:
                        if resource.get("machine_identifier") == section.get("machine_identifier"):
                            for conn in resource.get("connections", []):
                                uri = conn.get("uri") if isinstance(conn, dict) else conn
                                if uri and uri not in connections:
                                    connections.append(uri)
                    
                    if not connections:
                        logger.warning("No connections found for section %s", section.get("title"))
                        continue
                    
                    plex_uri = connections[0]
                    section_key = section.get("key") or section.get("id")
                    if isinstance(section_key, str) and section_key.startswith("/library/sections/"):
                        section_key = section_key.split("/")[-1]
                    
                    logger.info("Processing section: %s (%s)", section.get("title"), section_type)
                    
                    try:
                        # Query items in section with pagination, but stop early if we find all test items
                        start = 0
                        page_size = 1000
                        total_items = None
                        processed = 0
                        detailed_metadata_fetched = 0
                        
                        while True:
                            # Stop if we've found all uncached items
                            if items_found >= len(target_tmdb_ids):
                                logger.info("  Found all %d uncached test items, stopping search", len(target_tmdb_ids))
                                break
                            
                            # Stop if we've checked too many items
                            if processed >= max_items_to_check:
                                logger.info("  Checked %d items, stopping search", max_items_to_check)
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
                                logger.info("  Section has %d total items (will check up to %d)", total_items, max_items_to_check)
                            
                            if not library_items:
                                break
                            
                            for entry in library_items:
                                # Stop early if we've found all uncached items
                                if items_found >= len(target_tmdb_ids):
                                    logger.info("  Found all %d uncached test items, stopping search", len(target_tmdb_ids))
                                    break
                                
                                rating_key = entry.get("ratingKey") or entry.get("ratingkey")
                                if not rating_key:
                                    continue
                                
                                processed += 1
                                
                                # Extract external IDs from entry
                                guids = entry.get("Guid", [])
                                if not guids:
                                    single_guid = entry.get("guid")
                                    if single_guid:
                                        guids = [{"id": single_guid}]
                                
                                external_ids = extract_external_ids_from_guids(guids)
                                
                                # Only fetch detailed metadata if:
                                # 1. We don't have external IDs yet (need to check detailed metadata)
                                # 2. We haven't found all uncached items (no point fetching if we're done)
                                # 3. The GUID is plex:// (Plex internal ID, external IDs might be in detailed metadata)
                                # 4. We're still actively searching (haven't processed too many items yet)
                                # For small tests (1-2 items), be very selective - only fetch if we're early in the search
                                max_metadata_fetches = max(50, len(target_tmdb_ids) * 10)  # Limit fetches for small tests
                                should_fetch_metadata = (
                                    not external_ids and 
                                    guids and 
                                    items_found < len(target_tmdb_ids) and
                                    detailed_metadata_fetched < max_metadata_fetches
                                )
                                
                                if should_fetch_metadata:
                                    guid_value = guids[0].get("id") if isinstance(guids[0], dict) else guids[0]
                                    if guid_value and guid_value.startswith("plex://"):
                                        try:
                                            detailed_metadata = plex_api.fetch_metadata(
                                                plex_account.plex_token,
                                                plex_uri,
                                                str(rating_key),
                                            )
                                            if detailed_metadata:
                                                detailed_metadata_fetched += 1
                                                detailed_guids = detailed_metadata.get("Guid", [])
                                                if not detailed_guids:
                                                    single_guid = detailed_metadata.get("guid")
                                                    if single_guid:
                                                        detailed_guids = [{"id": single_guid}]
                                                external_ids = extract_external_ids_from_guids(detailed_guids)
                                                # Rate limit: 50ms delay after each detailed metadata fetch
                                                time.sleep(0.05)
                                        except Exception as exc:
                                            logger.debug("Failed to fetch metadata for %s: %s", rating_key, exc)
                                            continue
                                
                                # Check if this item matches our uncached test items
                                tmdb_id = external_ids.get("tmdb_id") if external_ids else None
                                if tmdb_id and str(tmdb_id) in target_tmdb_ids:
                                    rating_key_map[("tmdb", str(tmdb_id))] = rating_key
                                    id_type_counts["tmdb"] += 1
                                    items_found += 1
                                    logger.info("  ✓ Found uncached test item: %s (TMDB:%s, ratingKey:%s)", 
                                               entry.get("title", "Unknown"), tmdb_id, rating_key)
                                    # Check again if we've found all items (after this match)
                                    if items_found >= len(target_tmdb_ids):
                                        logger.info("  Found all %d uncached test items, stopping search", len(target_tmdb_ids))
                                        break
                                
                                # Also store IMDB/TVDB IDs for fallback matching (but only if we might need them)
                                # Only store if we haven't found all items yet
                                if items_found < len(target_tmdb_ids):
                                    if "imdb_id" in external_ids:
                                        rating_key_map[("imdb", external_ids["imdb_id"])] = rating_key
                                        id_type_counts["imdb"] += 1
                                    
                                    if "tvdb_id" in external_ids:
                                        rating_key_map[("tvdb", str(external_ids["tvdb_id"]))] = rating_key
                                        id_type_counts["tvdb"] += 1
                                
                                # Log progress every 200 items (more frequent for small tests)
                                if processed % 200 == 0:
                                    logger.info("  Processed %d items (found %d/%d uncached test items, %d total mappings, fetched %d detailed metadata)",
                                               processed, items_found, len(target_tmdb_ids), len(rating_key_map), detailed_metadata_fetched)
                            
                            # Stop outer loop if we've found all uncached items
                            if items_found >= len(target_tmdb_ids):
                                break
                                
                            # Check if we need to paginate
                            start += len(library_items)
                            if start >= total or len(library_items) == 0:
                                break
                        
                        # Only log if we actually did scanning
                        if items_needing_scan:
                            logger.info("  Built rating_key_map: %d mappings (TMDB: %d, IMDB: %d, TVDB: %d) [%d cached, processed %d items, fetched %d detailed metadata, found %d/%d uncached test items]",
                                       len(rating_key_map), id_type_counts.get("tmdb", 0), 
                                       id_type_counts.get("imdb", 0), id_type_counts.get("tvdb", 0),
                                       cached_count, processed, detailed_metadata_fetched, items_found, len(target_tmdb_ids))
                    
                    except Exception as exc:
                        logger.warning("Failed to process section %s: %s", section.get("title"), exc, exc_info=True)
                        continue
    
    # Step 3: Test matching logic and create collection entries
    logger.info("\nStep 3: Testing matching logic and creating collection entries...")
    
    matches = {"tmdb": 0, "imdb": 0, "tvdb": 0}
    unmatched = []
    match_details = []
    entries_created = 0
    entries_updated = 0
    entries_skipped = 0
    
    # Build a map of rating_key -> (plex_uri, section) for quick lookup
    # We need sections even when skipping library scan to get plex_uri
    if skip_library_scan:
        sections = plex_account.sections or []
        if not sections:
            sections = plex_api.list_sections(plex_account.plex_token)
    
    rating_key_to_uri = {}
    for section in sections:
        section_type = (section.get("type") or "").lower()
        if section_type not in ("movie", "show"):
            continue
        connections = []
        if section.get("uri"):
            connections.append(section.get("uri"))
        for resource in resources:
            if resource.get("machine_identifier") == section.get("machine_identifier"):
                for conn in resource.get("connections", []):
                    uri = conn.get("uri") if isinstance(conn, dict) else conn
                    if uri and uri not in connections:
                        connections.append(uri)
        if connections:
            # Store URI for this section (we'll use it when we find a match)
            rating_key_to_uri[section_type] = connections[0]
    
    for item in test_items:
        if item.media_type not in (MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value):
            continue
        
        # Try TMDB match first (primary)
        rating_key = rating_key_map.get((item.source, item.media_id))
        match_type = None
        
        if not rating_key and item.source == "tmdb":
            rating_key = rating_key_map.get(("tmdb", item.media_id))
            if rating_key:
                match_type = "tmdb"
        
        # Fallback: If TMDB match failed, fetch TMDB metadata to get IMDB/TVDB IDs
        if not rating_key and item.source == "tmdb":
            try:
                from app.providers import services
                tmdb_metadata = services.get_media_metadata(
                    item.media_type,
                    item.media_id,
                    item.source,
                )
                
                if tmdb_metadata:
                    # For movies, fetch raw TMDB API response to get external_ids
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
                        if imdb_id:
                            rating_key = rating_key_map.get(("imdb", imdb_id))
                            if rating_key:
                                match_type = "imdb"
                                logger.debug("Matched %s by IMDB ID %s (fallback)", item.title, imdb_id)
                    elif item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
                        # TV shows: try TVDB first
                        tvdb_id = tmdb_metadata.get("tvdb_id")
                        if tvdb_id:
                            rating_key = rating_key_map.get(("tvdb", str(tvdb_id)))
                            if rating_key:
                                match_type = "tvdb"
                                logger.debug("Matched %s by TVDB ID %s (fallback)", item.title, tvdb_id)
                        
                        # Also try IMDB for TV shows
                        if not rating_key:
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
                            if imdb_id:
                                rating_key = rating_key_map.get(("imdb", imdb_id))
                                if rating_key:
                                    match_type = "imdb"
                                    logger.debug("Matched %s by IMDB ID %s (fallback)", item.title, imdb_id)
            except Exception as exc:
                logger.debug("Failed to fetch TMDB metadata for fallback matching %s: %s", item.title, exc)
        
        # Track the match and create collection entry
        if rating_key:
            if not match_type:
                match_type = "tmdb"  # Default to TMDB if match_type not set
            matches[match_type] += 1
            
            # Get Plex URI for this item
            section_type = "movie" if item.media_type == MediaTypes.MOVIE.value else "show"
            plex_uri = rating_key_to_uri.get(section_type)
            
            if plex_uri:
                try:
                    # Fetch detailed metadata from Plex (like the real import task)
                    plex_metadata = plex_api.fetch_metadata(
                        plex_account.plex_token,
                        plex_uri,
                        str(rating_key),
                    )
                    
                    if plex_metadata:
                        # Extract collection metadata
                        collection_metadata = extract_collection_metadata_from_plex(plex_metadata)
                        episode_list = []  # Will be populated for TV shows
                        
                        # For TV shows, fetch episode list (like the bulk import task)
                        # If show-level metadata is empty, also use aggregated metadata from episodes
                        if item.media_type in (MediaTypes.TV.value, MediaTypes.ANIME.value):
                            from integrations.tasks import _aggregate_tv_show_collection_metadata
                            
                            # Only fetch detailed episode metadata if show-level metadata is empty
                            # (needed for aggregation). Otherwise, just get episode lists.
                            fetch_episode_details = not any(collection_metadata.values())
                            
                            aggregated_metadata, episode_list = _aggregate_tv_show_collection_metadata(
                                plex_account.plex_token,
                                plex_uri,
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
                            
                            # Create episode-level collection entries (like bulk import does)
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
                                    from app.models import Item as ItemModel
                                    episode_item, episode_item_created = ItemModel.objects.get_or_create(
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
                                        "Failed to create collection entry for episode S%02dE%02d: %s",
                                        season_number,
                                        episode_number,
                                        exc,
                                    )
                                    continue
                            
                            if episode_entries_created > 0 or episode_entries_updated > 0:
                                logger.debug(
                                    "Created %d and updated %d episode collection entries for %s",
                                    episode_entries_created,
                                    episode_entries_updated,
                                    item.title,
                                )
                        
                        # Only create entry if we have some metadata
                        if any(collection_metadata.values()):
                            entry, created = CollectionEntry.objects.get_or_create(
                                user=user,
                                item=item,
                                defaults=collection_metadata,
                            )
                            
                            # Store rating key and URI for future bulk imports (cache for faster lookups)
                            if entry.plex_rating_key != rating_key or entry.plex_uri != plex_uri:
                                entry.plex_rating_key = rating_key
                                entry.plex_uri = plex_uri
                                entry.plex_rating_key_updated_at = timezone.now()
                            
                            if not created:
                                # Update existing entry
                                updated = False
                                for key, value in collection_metadata.items():
                                    if value:  # Only update non-empty values
                                        old_value = getattr(entry, key, None)
                                        if old_value != value:
                                            setattr(entry, key, value)
                                            updated = True
                                if updated or entry.plex_rating_key != rating_key:
                                    entry.updated_at = timezone.now()
                                    entry.save()
                                    entries_updated += 1
                                else:
                                    entries_skipped += 1
                            else:
                                # New entry - save to store rating key
                                entry.save()
                                entries_created += 1
                            
                            match_details.append({
                                "item": item.title,
                                "media_type": item.media_type,
                                "match_type": match_type,
                                "rating_key": rating_key,
                                "source": item.source,
                                "media_id": item.media_id,
                                "collection_entry_created": created,
                                "collection_entry_updated": not created and (updated if 'updated' in locals() else False),
                                "collection_metadata": {k: v for k, v in collection_metadata.items() if v},
                            })
                            if created:
                                action = "CREATED"
                            elif 'updated' in locals() and updated:
                                action = "UPDATED"
                            else:
                                action = "EXISTS"
                            logger.info("✓ MATCHED & %s: %s (%s) - matched by %s (rating_key=%s)", 
                                       action, item.title, item.media_type, match_type, rating_key)
                        else:
                            entries_skipped += 1
                            match_details.append({
                                "item": item.title,
                                "media_type": item.media_type,
                                "match_type": match_type,
                                "rating_key": rating_key,
                                "source": item.source,
                                "media_id": item.media_id,
                                "collection_entry_created": False,
                                "collection_metadata": None,
                                "reason": "No collection metadata found in Plex",
                            })
                            logger.warning("✓ MATCHED but NO METADATA: %s (%s) - matched by %s but no collection metadata (rating_key=%s)", 
                                          item.title, item.media_type, match_type, rating_key)
                    else:
                        entries_skipped += 1
                        match_details.append({
                            "item": item.title,
                            "media_type": item.media_type,
                            "match_type": match_type,
                            "rating_key": rating_key,
                            "source": item.source,
                            "media_id": item.media_id,
                            "collection_entry_created": False,
                            "collection_metadata": None,
                            "reason": "Failed to fetch Plex metadata",
                        })
                        logger.warning("✓ MATCHED but FETCH FAILED: %s (%s) - matched by %s but couldn't fetch metadata (rating_key=%s)", 
                                      item.title, item.media_type, match_type, rating_key)
                except Exception as exc:
                    entries_skipped += 1
                    match_details.append({
                        "item": item.title,
                        "media_type": item.media_type,
                        "match_type": match_type,
                        "rating_key": rating_key,
                        "source": item.source,
                        "media_id": item.media_id,
                        "collection_entry_created": False,
                        "collection_metadata": None,
                        "reason": f"Error: {exc}",
                    })
                    logger.warning("✓ MATCHED but ERROR: %s (%s) - matched by %s but error creating entry: %s", 
                                  item.title, item.media_type, match_type, exc)
            else:
                entries_skipped += 1
                match_details.append({
                    "item": item.title,
                    "media_type": item.media_type,
                    "match_type": match_type,
                    "rating_key": rating_key,
                    "source": item.source,
                    "media_id": item.media_id,
                    "collection_entry_created": False,
                    "collection_metadata": None,
                    "reason": "Could not find Plex URI",
                })
                logger.warning("✓ MATCHED but NO URI: %s (%s) - matched by %s but couldn't find Plex URI (rating_key=%s)", 
                              item.title, item.media_type, match_type, rating_key)
        else:
            unmatched.append({
                "item": item.title,
                "media_type": item.media_type,
                "source": item.source,
                "media_id": item.media_id,
            })
            logger.warning("✗ UNMATCHED: %s (%s) - source=%s, media_id=%s", 
                          item.title, item.media_type, item.source, item.media_id)
    
    # Step 4: Report results
    logger.info("\n" + "=" * 80)
    logger.info("RESULTS")
    logger.info("=" * 80)
    
    total_matched = sum(matches.values())
    total_items = len(test_items)
    match_rate = (total_matched / total_items * 100) if total_items > 0 else 0
    
    logger.info("Total test items: %d (%d movies, %d TV shows)", 
               total_items, len(movie_items), len(tv_items))
    logger.info("Matched: %d (%.1f%%)", total_matched, match_rate)
    logger.info("  - TMDB matches: %d", matches["tmdb"])
    logger.info("  - IMDB matches: %d", matches["imdb"])
    logger.info("  - TVDB matches: %d", matches["tvdb"])
    logger.info("Unmatched: %d", len(unmatched))
    logger.info("Collection entries:")
    logger.info("  - Created: %d", entries_created)
    logger.info("  - Updated: %d", entries_updated)
    logger.info("  - Skipped (no metadata/error): %d", entries_skipped)
    
    if unmatched:
        logger.info("\nUnmatched Items:")
        for item in unmatched:
            logger.info("  - %s (%s): source=%s, media_id=%s", 
                       item["item"], item["media_type"], 
                       item["source"], item["media_id"])
    
    if match_details:
        logger.info("\nMatched Items:")
        created_count = sum(1 for d in match_details if d.get("collection_entry_created") is True)
        updated_count = sum(1 for d in match_details if d.get("collection_entry_created") is False and d.get("collection_metadata"))
        no_metadata_count = sum(1 for d in match_details if not d.get("collection_metadata") and d.get("collection_entry_created") is not True)
        
        for detail in match_details:
            status = ""
            if detail.get("collection_entry_created") is True:
                status = " [ENTRY CREATED]"
            elif detail.get("collection_entry_created") is False and detail.get("collection_metadata"):
                status = " [ENTRY UPDATED]"
            elif detail.get("reason"):
                status = f" [NO ENTRY: {detail['reason']}]"
            
            logger.info("  - %s (%s): matched by %s%s", 
                       detail["item"], detail["media_type"], detail["match_type"], status)
        
        logger.info("\nCollection Entry Summary:")
        logger.info("  - Created: %d", created_count)
        logger.info("  - Updated: %d", updated_count)
        logger.info("  - No metadata/error: %d", no_metadata_count)
    
    return {
        "total_items": total_items,
        "matched": total_matched,
        "match_rate": match_rate,
        "matches_by_type": matches,
        "unmatched_count": len(unmatched),
        "unmatched": unmatched,
        "matched": match_details,
    }


if __name__ == "__main__":
    # For testing a single TV show, you can skip the library scan by providing rating_keys
    # Example: test_collection_matching(tv_limit=1, rating_keys={("tmdb", "12345"): "67890"}, skip_library_scan=True)
    # 
    # By default, the script will scan the library to find rating keys.
    # This is slow but necessary if you don't know the rating keys.
    # To test just the collection metadata fetching (the optimization), provide rating_keys.
    
    result = test_collection_matching(movie_limit=0, tv_limit=1)
    if result:
        print(f"\n{'='*80}")
        print(f"FINAL RESULT: {result['match_rate']:.1f}% match rate ({result['matched']}/{result['total_items']} items)")
        print(f"{'='*80}")
        sys.exit(0 if result['match_rate'] > 90 else 1)
    else:
        sys.exit(1)
