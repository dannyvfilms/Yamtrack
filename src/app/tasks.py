"""Celery tasks for the app."""

import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from app import history_cache
from app.models import Item, MediaTypes
from app.providers import services

logger = logging.getLogger(__name__)


@shared_task
def populate_runtime_data_batch(batch_size=10, delay_seconds=1.0):
    """Populate runtime data for a batch of items that don't have it."""
    import time

    from app.statistics import parse_runtime_to_minutes

    # Get items that need runtime data (exclude manual items, episodes, and previously failed items)
    items_to_update = Item.objects.filter(
        runtime_minutes__isnull=True,
        media_type__in=[MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value],  # Episodes get runtime from season metadata, not individually
        source__in=["tmdb", "mal", "simkl"],  # Only process items from providers that have runtime data
    ).exclude(
        runtime_minutes=999999,  # Exclude items marked as failed
    ).order_by("id")[:batch_size]

    if not items_to_update.exists():
        logger.info("No items need runtime data")
        return {"updated": 0, "errors": 0}

    updated_count = 0
    error_count = 0

    for item in items_to_update:
        try:
            # Get metadata from provider
            metadata = services.get_media_metadata(
                item.media_type.lower(),
                item.media_id,
                item.source,
            )

            # Check if metadata is None or doesn't have the expected structure
            if not metadata:
                logger.warning(f"No metadata returned for {item.title} ({item.media_type}, {item.source})")
                error_count += 1
                continue

            if not isinstance(metadata, dict):
                logger.warning(f"Invalid metadata format for {item.title}: {type(metadata)}")
                error_count += 1
                continue

            if not metadata.get("details"):
                logger.warning(f"No details in metadata for {item.title}")
                error_count += 1
                continue

            details = metadata["details"]
            runtime_str = details.get("runtime")

            if not runtime_str:
                logger.warning(f"No runtime data available for {item.title}")
                error_count += 1

                # Mark item as failed to avoid endless retries
                try:
                    with transaction.atomic():
                        item.runtime_minutes = 999999  # Use 999999 as a "failed" marker
                        item.save()
                    logger.warning(f"Marked {item.title} as failed (runtime_minutes=999999) - no runtime data available")
                except Exception as save_error:
                    logger.error(f"Failed to mark {item.title} as failed: {save_error}")

                continue

            runtime_minutes = parse_runtime_to_minutes(runtime_str)

            if runtime_minutes is None:
                logger.warning(f"Failed to parse runtime '{runtime_str}' for {item.title}")
                error_count += 1

                # Mark item as failed to avoid endless retries
                try:
                    with transaction.atomic():
                        item.runtime_minutes = 999999  # Use 999999 as a "failed" marker
                        item.save()
                    logger.warning(f"Marked {item.title} as failed (runtime_minutes=999999) - failed to parse runtime")
                except Exception as save_error:
                    logger.error(f"Failed to mark {item.title} as failed: {save_error}")

                continue

            # Update the item
            with transaction.atomic():
                item.runtime_minutes = runtime_minutes
                item.save()

            updated_count += 1
            logger.info(f"Updated runtime for {item.title}: {runtime_minutes} minutes")

            # Add delay to avoid rate limiting
            if delay_seconds > 0:
                time.sleep(delay_seconds)

        except Exception as e:
            error_count += 1
            logger.error(f"Error updating runtime for {item.title}: {e}")

            # Mark item as failed by setting runtime_minutes to 999999 to avoid endless retries
            # This is a simple way to skip items that consistently fail
            try:
                with transaction.atomic():
                    item.runtime_minutes = 999999  # Use 999999 as a "failed" marker
                    item.save()
                logger.warning(f"Marked {item.title} as failed (runtime_minutes=999999) to avoid endless retries")
            except Exception as save_error:
                logger.error(f"Failed to mark {item.title} as failed: {save_error}")

    logger.info(f"Runtime population batch completed: {updated_count} updated, {error_count} errors")

    # Check if there are more items to process (exclude previously failed items)
    remaining_items = Item.objects.filter(
        runtime_minutes__isnull=True,
        media_type__in=[MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value],
        source__in=["tmdb", "mal", "simkl"],
    ).exclude(
        runtime_minutes=999999,  # Exclude items marked as failed
    ).count()

    if remaining_items > 0:
        logger.info(f"Found {remaining_items} remaining items. Scheduling next batch...")
        # Schedule the next batch with a small delay
        populate_runtime_data_batch.apply_async(
            kwargs={"batch_size": batch_size, "delay_seconds": delay_seconds},
            countdown=5,  # 5 second delay between batches
        )
        return {
            "updated": updated_count,
            "errors": error_count,
            "remaining_items": remaining_items,
            "next_batch_scheduled": True,
        }
    logger.info("🎉 All runtime data population completed! No more items need processing.")

    # Mark as completed in cache to prevent repeated runs
    from django.core.cache import cache
    cache.set("runtime_population_completed", True, timeout=3600)  # 1 hour

    return {
        "updated": updated_count,
        "errors": error_count,
        "remaining_items": 0,
        "next_batch_scheduled": False,
        "completion_message": "All runtime data populated successfully!",
    }


@shared_task
def refresh_history_cache_task(
    user_id: int,
    logging_style: str = "repeats",
    warm_days: int | None = None,
    *args,
    **kwargs,
):
    """Rebuild the cached History page for a user."""
    if logging_style not in ("sessions", "repeats"):
        for candidate in (logging_style, *args, kwargs.get("logging_style")):
            if candidate in ("sessions", "repeats"):
                logging_style = candidate
                break
        else:
            logging_style = "repeats"
    if warm_days is None:
        for candidate in (*args, kwargs.get("warm_days")):
            if candidate is None:
                continue
            try:
                warm_days = int(candidate)
                break
            except (TypeError, ValueError):
                continue
    if warm_days is not None and warm_days < 0:
        warm_days = None
    history_cache.refresh_history_cache(
        user_id,
        logging_style=logging_style,
        warm_days=warm_days,
    )


@shared_task
def refresh_statistics_cache_task(user_id: int, range_name: str):
    """Rebuild the cached Statistics page for a user and range."""
    from app import statistics_cache
    statistics_cache.refresh_statistics_cache(user_id, range_name)


@shared_task
def populate_runtime_data_continuous():
    """Populate runtime data for ALL items that don't have it (startup task)."""
    from django.core.cache import cache

    from app.models import Item, MediaTypes

    # Check if runtime population has already been completed recently (within last hour)
    cache_key = "runtime_population_completed"
    if cache.get(cache_key):
        # Check if episodes also need runtime data
        episodes_needing_runtime = Item.objects.filter(
            runtime_minutes__isnull=True,
            media_type=MediaTypes.EPISODE.value,
            source__in=["tmdb", "mal", "simkl"],
        ).exclude(
            runtime_minutes=999999,
        ).count()

        if episodes_needing_runtime > 0:
            logger.info(f"Runtime population completed for movies/TV/anime, but {episodes_needing_runtime} episodes still need runtime data. Starting episode population...")
            # Clear the cache and continue with episode population
            cache.delete(cache_key)
        else:
            logger.info("Runtime population already completed recently - skipping")
            return {"total_items": 0, "batches_scheduled": 0, "message": "Already completed recently"}

    # Count total items that need runtime data (exclude previously failed items)
    total_items = Item.objects.filter(
        runtime_minutes__isnull=True,
        media_type__in=[MediaTypes.MOVIE.value, MediaTypes.TV.value, MediaTypes.ANIME.value],
        source__in=["tmdb", "mal", "simkl"],
    ).exclude(
        runtime_minutes=999999,  # Exclude items marked as failed
    ).count()

    if total_items == 0:
        # Check if episodes also need runtime data
        episodes_needing_runtime = Item.objects.filter(
            runtime_minutes__isnull=True,
            media_type=MediaTypes.EPISODE.value,
            source__in=["tmdb", "mal", "simkl"],
        ).exclude(
            runtime_minutes=999999,
        ).count()

        if episodes_needing_runtime > 0:
            logger.info(f"No movies/TV/anime need runtime data, but {episodes_needing_runtime} episodes still need runtime data. Starting episode population...")
            # Start episode population
            episode_result = populate_episode_runtime_data.delay()
            return {
                "total_items": 0,
                "episode_task_id": episode_result.id,
                "message": f"Movies/TV/anime up to date, starting episode population for {episodes_needing_runtime} episodes",
            }
        logger.info("No items need runtime data - all up to date!")
        # Mark as completed for 1 hour to prevent repeated runs
        cache.set(cache_key, True, timeout=3600)
        return {"total_items": 0, "batches_scheduled": 0, "message": "All up to date - marked as completed"}

    logger.info(f"Found {total_items} items that need runtime data. Starting comprehensive population...")

    # Start the first batch - it will chain itself if more items remain
    first_batch = populate_runtime_data_batch.delay(batch_size=20, delay_seconds=1.0)

    # Also start episode runtime population
    episode_result = populate_episode_runtime_data.delay()

    return {
        "total_items": total_items,
        "first_task_id": first_batch.id,
        "episode_task_id": episode_result.id,
        "message": "Started comprehensive runtime population for movies/TV/anime and episodes. Check logs for progress.",
    }


@shared_task
def enrich_music_library_task(user_id: int):
    """Post-import enrichment/dedupe for a user's music library."""
    from app.models import Album, Artist, Music
    from app.services.music import (
        merge_artist_records,
        prefetch_album_covers,
        resolve_artist_mbid,
        sync_artist_discography,
    )
    from app.services.music_scrobble import dedupe_artist_albums
    from app.services.music_validation import validate_music_library

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning("enrich_music_library_task: user %s no longer exists", user_id)
        return {"artists": 0, "synced": 0, "deduped": 0}

    # Skip expensive validation before enrichment - run after to see improvements
    # Fast runtime backfill already runs immediately after import for statistics
    logger.info(
        "enrich_music_library_task: Starting enrichment for user %s",
        user_id,
    )

    artist_ids = (
        Music.objects.filter(user=user)
        .exclude(artist_id__isnull=True)
        .values_list("artist_id", flat=True)
        .distinct()
    )

    artists = list(Artist.objects.filter(id__in=artist_ids))
    artists_without_mbid = [a for a in artists if not a.musicbrainz_id]
    artists_with_mbid = [a for a in artists if a.musicbrainz_id]

    # Log sample names to verify we're seeing the full set (not just "A" names)
    sample_without_mbid = [a.name for a in artists_without_mbid[:10]] if artists_without_mbid else []
    sample_with_mbid = [a.name for a in artists_with_mbid[:10]] if artists_with_mbid else []

    logger.info(
        "enrich_music_library_task: Found %d total artists (%d without MBID, %d with MBID). "
        "Sample without MBID: %s. Sample with MBID: %s",
        len(artists),
        len(artists_without_mbid),
        len(artists_with_mbid),
        sample_without_mbid,
        sample_with_mbid,
    )

    synced = 0
    deduped = 0
    attached = 0
    merged = 0
    no_match = 0
    skipped_already_has_mbid = 0
    skipped_artist_names_sample = []  # Sample of skipped artist names
    total_candidates = 0
    albums_tracks_populated = 0
    albums_to_populate: list[int] = []  # Collect albums for background track population
    artists_for_covers: list[int] = []
    defer_covers = getattr(settings, "MUSIC_DEFER_COVER_PREFETCH", True)

    # Phase 1: Fast runtime backfill from existing tracks (DB-only, immediate)
    from app.models import Item
    from app.services.music_scrobble import _runtime_minutes_from_ms

    music_with_runtime = (
        Music.objects.filter(user=user, item__runtime_minutes__isnull=True)
        .exclude(track__duration_ms__isnull=True)
        .select_related("item", "track")
    )

    items_to_update_runtime = []
    for music in music_with_runtime:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                items_to_update_runtime.append(music.item)

    if items_to_update_runtime:
        Item.objects.bulk_update(items_to_update_runtime, ["runtime_minutes"], batch_size=500)
        logger.info(
            "enrich_music_library_task: Backfilled %d runtimes from existing tracks",
            len(items_to_update_runtime),
        )

    # Phase 2: API operations (MBID resolution, discography sync, track population)
    artists_processed_count = 0
    for idx, artist in enumerate(artists):
        artists_processed_count += 1
        # Log progress every 50 artists to track if we're processing the full list
        if artists_processed_count % 50 == 0 or artists_processed_count == len(artists):
            logger.info(
                "enrich_music_library_task: Progress - processed %d/%d artists (current: '%s', id=%s)",
                artists_processed_count,
                len(artists),
                artist.name if artist.name else "Unknown",
                artist.id,
            )
        # Heal blank names that slipped in during fast import
        if not (artist.name or "").strip():
            artist.name = "Unknown Artist"
            artist.save(update_fields=["name"])

        # If missing MBID, try to attach a safe one
        if artist.musicbrainz_id:
            # Artist already has MBID, skip MBID resolution
            skipped_already_has_mbid += 1
            # Collect sample names (first 20) for logging
            if len(skipped_artist_names_sample) < 20:
                skipped_artist_names_sample.append(artist.name)
        else:
            logger.info(
                "enrich_music_library_task: Processing artist '%s' (id=%s, no MBID, sort_name='%s')",
                artist.name,
                artist.id,
                artist.sort_name or "",
            )
            try:
                mbid, cand_count, variant = resolve_artist_mbid(
                    artist.name or "",
                    artist.sort_name or "",
                )
                total_candidates += cand_count
                logger.info(
                    "enrich_music_library_task: resolve_artist_mbid('%s', '%s') returned: mbid=%s, candidates=%d, variant='%s'",
                    artist.name or "",
                    artist.sort_name or "",
                    mbid or "None",
                    cand_count,
                    variant or "None",
                )
                if mbid:
                    logger.info(
                        "enrich_music_library_task: attempting to attach MBID %s to artist '%s' (id=%s) via variant '%s'",
                        mbid,
                        artist.name,
                        artist.id,
                        variant or "None",
                    )
                    try:
                        artist.musicbrainz_id = mbid
                        artist.save(update_fields=["musicbrainz_id"])
                        attached += 1
                        logger.info(
                            "enrich_music_library_task: SUCCESS - attached MBID %s to '%s' (id=%s) via '%s' (candidates=%d)",
                            mbid,
                            artist.name,
                            artist.id,
                            variant or "None",
                            cand_count,
                        )
                    except IntegrityError as integrity_err:
                        logger.info(
                            "enrich_music_library_task: IntegrityError attaching MBID %s to '%s' (id=%s) - MBID already exists, attempting merge",
                            mbid,
                            artist.name,
                            artist.id,
                        )
                        # Merge into the existing artist that already owns this MBID
                        existing = Artist.objects.filter(musicbrainz_id=mbid).first()
                        if existing:
                            logger.info(
                                "enrich_music_library_task: found existing artist '%s' (id=%s, MBID=%s) to merge into",
                                existing.name,
                                existing.id,
                                existing.musicbrainz_id,
                            )
                            try:
                                artist = merge_artist_records(artist, existing)
                                # Refresh from DB to ensure we have a valid saved instance
                                if artist.pk:
                                    artist.refresh_from_db()
                                merged += 1
                                logger.info(
                                    "enrich_music_library_task: SUCCESS - merged artist '%s' (id=%s) into '%s' (id=%s, MBID=%s) via variant '%s'",
                                    artist.name if hasattr(artist, "name") else "Unknown",
                                    artist.id if hasattr(artist, "id") else "Unknown",
                                    existing.name,
                                    existing.id,
                                    mbid,
                                    variant or "None",
                                )
                            except Exception as merge_exc:
                                logger.warning(
                                    "enrich_music_library_task: merge FAILED for '%s' (id=%s) into '%s' (id=%s, MBID=%s): %s",
                                    artist.name if hasattr(artist, "name") else "Unknown",
                                    artist.id if hasattr(artist, "id") else "Unknown",
                                    existing.name,
                                    existing.id,
                                    mbid,
                                    merge_exc,
                                    exc_info=True,
                                )
                                # After failed merge, artist might be invalid - skip remaining processing for this artist
                                if not artist.pk:
                                    logger.warning(
                                        "enrich_music_library_task: artist '%s' invalid after failed merge, skipping remaining processing for this artist, continuing with next",
                                        artist.name if hasattr(artist, "name") else "Unknown",
                                    )
                                    continue
                        else:
                            logger.warning(
                                "enrich_music_library_task: MBID attach failed for '%s' (id=%s) - MBID %s conflicts but no target artist found (variant '%s', error: %s)",
                                artist.name,
                                artist.id,
                                mbid,
                                variant or "None",
                                integrity_err,
                            )
                else:
                    no_match += 1
                    logger.info(
                        "enrich_music_library_task: NO MATCH - resolve_artist_mbid returned None for '%s' (id=%s, candidates=%d, variant='%s')",
                        artist.name,
                        artist.id,
                        cand_count,
                        variant or "None",
                    )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "enrich_music_library_task: EXCEPTION - MBID resolution failed for '%s' (id=%s): %s",
                    artist.name,
                    artist.id,
                    exc,
                    exc_info=True,
                )

        # Skip remaining processing if artist became invalid (e.g., deleted during merge)
        if not artist.pk:
            logger.debug(
                "enrich_music_library_task: skipping remaining processing for artist '%s' (no pk after MBID resolution)",
                artist.name if hasattr(artist, "name") else "Unknown",
            )
            continue

        if artist.musicbrainz_id:
            try:
                sync_artist_discography(artist, force=False)
                synced += 1
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Discography sync failed for %s: %s", artist.name, exc)

        try:
            dedupe_artist_albums(artist)
            deduped += 1
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Album dedupe failed for %s: %s", artist.name, exc)

        # Collect albums that need track population (defer to background for speed)
        # Only collect albums with MBIDs - can't populate tracks without them
        # Ensure artist is saved (has PK) and still exists before filtering
        # After failed merges, the artist might be deleted or invalid
        if artist.pk:
            # Verify artist still exists in DB (might have been deleted during failed merge)
            try:
                Artist.objects.get(pk=artist.pk)
            except Artist.DoesNotExist:
                logger.debug("Artist %s (pk=%s) no longer exists, skipping album collection", artist.name, artist.pk)
            else:
                for album in Album.objects.filter(
                    artist_id=artist.pk,
                    tracks_populated=False,
                ).exclude(
                    musicbrainz_release_id__isnull=True,
                    musicbrainz_release_group_id__isnull=True,
                ):
                    albums_to_populate.append(album.id)

        # Link Music entries to populated tracks by recording_id to unlock runtimes
        try:
            from app.models import Track as TrackModel

            # Ensure artist is saved before filtering
            if artist.pk:
                music_without_track = Music.objects.filter(
                    artist_id=artist.pk,
                    track__isnull=True,
                    item__media_id__isnull=False,
                    album__isnull=False,
                )
            else:
                music_without_track = Music.objects.none()

            if music_without_track.exists() and artist.pk:
                track_map = {
                    t.musicbrainz_recording_id: t.id
                    for t in TrackModel.objects.filter(
                        album__artist_id=artist.pk,
                        musicbrainz_recording_id__isnull=False,
                    )
                }
                to_update = []
                for music in music_without_track:
                    track_id = track_map.get(music.item.media_id)
                    if track_id:
                        music.track_id = track_id
                        to_update.append(music)
                if to_update:
                    Music.objects.bulk_update(to_update, ["track"])
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Music->Track relink failed for artist %s: %s", artist.id, exc)

        # Either queue cover prefetch for later or do it inline (configurable)
        if defer_covers and artist.musicbrainz_id:
            artists_for_covers.append(artist.id)
        elif artist.musicbrainz_id:
            try:
                prefetch_album_covers(artist, limit=None)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Cover prefetch failed for artist %s: %s", artist.id, exc)

    # Phase 3: Final runtime backfill from newly populated/linked tracks (if any)
    # This catches tracks that got duration_ms during enrichment
    music_with_new_runtime = (
        Music.objects.filter(user=user, item__runtime_minutes__isnull=True)
        .exclude(track__duration_ms__isnull=True)
        .select_related("item", "track")
    )

    items_final_runtime = []
    for music in music_with_new_runtime:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                items_final_runtime.append(music.item)

    if items_final_runtime:
        Item.objects.bulk_update(items_final_runtime, ["runtime_minutes"], batch_size=500)
        logger.info(
            "enrich_music_library_task: Backfilled %d additional runtimes from newly linked tracks",
            len(items_final_runtime),
        )

    cover_task_id = None
    if defer_covers and artists_for_covers:
        result = prefetch_album_covers_batch.delay(artists_for_covers, limit_per_artist=5)
        cover_task_id = result.id

    # Queue track population as background task (only for albums with MBIDs)
    # Pass user_id so we can link tracks and backfill runtime after population
    track_population_task_id = None
    if albums_to_populate:
        result = populate_album_tracks_batch.delay(albums_to_populate, user_id=user.id)
        track_population_task_id = result.id
        logger.info(
            "enrich_music_library_task: Queued track population for %d albums in background",
            len(albums_to_populate),
        )

    # Run validation after enrichment (optional - can be disabled for speed)
    run_validation = getattr(settings, "MUSIC_ENRICHMENT_VALIDATION", False)
    validation_result = None

    if run_validation:
        validation_after = validate_music_library(user)
        validation_result = {
            "after": validation_after,
        }
        logger.info(
            "enrich_music_library_task: Completed enrichment for user %s. "
            "Summary: %d total artists (%d skipped - already had MBID, %d processed without MBID). "
            "Results: attached %d MBIDs, merged %d, no match %d, synced %d discographies. "
            "Validation: %d tracks, %d artists (%d with MBID), %d albums (%d with tracks). "
            "Sample skipped artists: %s",
            user_id,
            len(artists),
            skipped_already_has_mbid,
            len(artists_without_mbid),
            attached,
            merged,
            no_match,
            synced,
            validation_after["unique_tracks"],
            validation_after["artists"]["total"],
            validation_after["artists"]["with_mbid"],
            validation_after["albums"]["total"],
            validation_after["albums"]["with_tracks_populated"],
            skipped_artist_names_sample[:10] if skipped_artist_names_sample else [],
        )
    else:
        logger.info(
            "enrich_music_library_task: Completed enrichment for user %s. "
            "Summary: %d total artists (%d skipped - already had MBID, %d processed without MBID). "
            "Results: attached %d MBIDs, merged %d, no match %d, synced %d discographies. "
            "Sample skipped artists: %s",
            user_id,
            len(artists),
            skipped_already_has_mbid,
            len(artists_without_mbid),
            attached,
            merged,
            no_match,
            synced,
            skipped_artist_names_sample[:10] if skipped_artist_names_sample else [],
        )

    return {
        "artists": len(artists),
        "synced": synced,
        "deduped": deduped,
        "attached_mbid": attached,
        "merged_artists": merged,
        "no_mbid_match": no_match,
        "skipped_already_has_mbid": skipped_already_has_mbid,
        "candidate_sum": total_candidates,
        "albums_tracks_populated": albums_tracks_populated,
        "albums_queued_for_tracks": len(albums_to_populate),
        "cover_task_id": cover_task_id,
        "track_population_task_id": track_population_task_id,
        "validation": validation_result,
    }


@shared_task
def fast_runtime_backfill_task(user_id: int):
    """Fast runtime backfill from existing Track durations - runs immediately after import.
    
    This is the critical path for statistics to work. Backfills runtime from:
    1. Track.duration_ms (if tracks already have duration from Plex)
    2. Direct lookup from album tracklists (if tracks are populated but not linked)
    
    This runs BEFORE enrichment to get statistics working immediately.
    """
    from app.models import Item, Music, Track
    from app.services.music_scrobble import _runtime_minutes_from_ms

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning("fast_runtime_backfill_task: user %s no longer exists", user_id)
        return {"backfilled": 0}

    # Strategy 1: Backfill from linked Track.duration_ms (fastest path)
    music_with_track_duration = (
        Music.objects.filter(
            user=user,
            item__runtime_minutes__isnull=True,
            track__duration_ms__isnull=False,
        )
        .select_related("item", "track")
    )

    items_to_update = []
    for music in music_with_track_duration:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                items_to_update.append(music.item)

    # Bulk update items
    if items_to_update:
        Item.objects.bulk_update(items_to_update, ["runtime_minutes"], batch_size=500)
        logger.info(
            "fast_runtime_backfill_task: Backfilled %d runtimes from linked Track records",
            len(items_to_update),
        )

    # Strategy 2: Backfill from album tracklists (for tracks not yet linked)
    # Find Music entries without runtime that have albums with populated tracks
    music_with_album_tracks = (
        Music.objects.filter(
            user=user,
            item__runtime_minutes__isnull=True,
            album__tracks_populated=True,
            item__media_id__isnull=False,
        )
        .exclude(track__duration_ms__isnull=False)  # Skip if already linked
        .select_related("item", "album")
    )

    additional_items = []
    for music in music_with_album_tracks:
        if not music.item or not music.item.media_id or not music.album:
            continue

        # Try to find track in album's tracklist by recording ID
        track = Track.objects.filter(
            album=music.album,
            musicbrainz_recording_id=music.item.media_id,
            duration_ms__isnull=False,
        ).first()

        if track and track.duration_ms:
            runtime = _runtime_minutes_from_ms(track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                additional_items.append(music.item)

    # Bulk update additional items
    if additional_items:
        Item.objects.bulk_update(additional_items, ["runtime_minutes"], batch_size=500)
        logger.info(
            "fast_runtime_backfill_task: Backfilled %d runtimes from album tracklists",
            len(additional_items),
        )

    total_backfilled = len(items_to_update) + len(additional_items)
    return {"backfilled": total_backfilled}


@shared_task
def populate_album_tracks_batch(album_ids: list[int], user_id: int | None = None):
    """Populate tracks for a batch of albums in the background.
    
    This defers the slow API operations (1 req/sec per album) to background
    so enrichment task completes faster.
    
    After populating tracks, automatically links Music entries to tracks and
    backfills runtime data.
    
    Args:
        album_ids: List of album IDs to populate tracks for
        user_id: Optional user ID - if provided, links tracks and backfills runtime after population
    """
    from app.models import Album
    from app.services.music import (
        backfill_music_runtimes,
        link_music_to_tracks,
        populate_album_tracks,
    )

    populated = 0
    skipped_no_release_id = 0
    skipped_already_populated = 0

    for album_id in album_ids:
        try:
            album = Album.objects.filter(id=album_id).first()
            if not album:
                continue

            if album.tracks_populated:
                skipped_already_populated += 1
                continue

            # Skip albums without MBIDs - can't populate tracks without them
            if not album.musicbrainz_release_id and not album.musicbrainz_release_group_id:
                continue

            count = populate_album_tracks(album)
            if count > 0:
                populated += 1
            elif album.musicbrainz_release_group_id and not album.musicbrainz_release_id:
                skipped_no_release_id += 1
        except Exception as exc:
            logger.warning("Track populate failed for album %s: %s", album_id, exc)

    if skipped_no_release_id > 0:
        logger.info(
            "populate_album_tracks_batch: Skipped %d albums that couldn't get release_id from release_group",
            skipped_no_release_id,
        )

    logger.info(
        "populate_album_tracks_batch: Populated tracks for %d albums (skipped: %d no release_id, %d already populated)",
        populated,
        skipped_no_release_id,
        skipped_already_populated,
    )

    # After populating tracks, link Music entries to tracks and backfill runtime
    if populated > 0 and user_id:
        try:
            User = get_user_model()
            user = User.objects.get(id=user_id)

            # Link Music entries to newly populated tracks
            link_result = link_music_to_tracks(user)

            # Backfill runtime from all available sources
            backfill_result = backfill_music_runtimes(user)

            logger.info(
                "populate_album_tracks_batch: After populating %d albums, linked %d Music->Track, backfilled %d runtimes",
                populated,
                link_result.get("linked", 0),
                backfill_result.get("backfilled", 0),
            )
        except User.DoesNotExist:
            logger.warning("populate_album_tracks_batch: User %s not found, skipping track linking", user_id)
        except Exception as exc:
            logger.warning("Failed to link tracks/backfill runtime after track population: %s", exc)

    return {
        "albums": len(album_ids),
        "populated": populated,
        "skipped_no_release_id": skipped_no_release_id,
        "skipped_already_populated": skipped_already_populated,
    }


@shared_task
def enrich_albums_task(user_id: int):
    """Post-import enrichment for albums - resolve MBIDs and populate tracks.
    
    This task processes albums that don't have MusicBrainz IDs, similar to how
    enrich_music_library_task processes artists. Uses the same proven search/matching
    logic from resolve_artist_mbid adapted for albums.
    """
    from app.models import Album, AlbumTracker, Item, Music
    from app.services.music import (
        backfill_music_runtimes,
        link_music_to_tracks,
        populate_album_tracks,
        resolve_album_mbid,
    )
    from app.services.music_scrobble import _runtime_minutes_from_ms

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.warning("enrich_albums_task: user %s no longer exists", user_id)
        return {"albums": 0, "attached_mbid": 0, "merged": 0}

    logger.info(
        "enrich_albums_task: Starting album enrichment for user %s",
        user_id,
    )

    # Get all albums for this user that need MBIDs
    # Albums are linked to users through Music entries
    album_ids = (
        Music.objects.filter(user=user)
        .exclude(album_id__isnull=True)
        .values_list("album_id", flat=True)
        .distinct()
    )

    albums = list(Album.objects.filter(id__in=album_ids))
    albums_without_mbid = [
        a
        for a in albums
        if not a.musicbrainz_release_id and not a.musicbrainz_release_group_id
    ]
    albums_with_mbid = [
        a
        for a in albums
        if a.musicbrainz_release_id or a.musicbrainz_release_group_id
    ]

    # Log sample names to verify we're seeing the full set
    sample_without_mbid = (
        [f"{a.title} - {a.artist.name if a.artist else 'Unknown'}" for a in albums_without_mbid[:10]]
        if albums_without_mbid
        else []
    )
    sample_with_mbid = (
        [f"{a.title} - {a.artist.name if a.artist else 'Unknown'}" for a in albums_with_mbid[:10]]
        if albums_with_mbid
        else []
    )

    logger.info(
        "enrich_albums_task: Found %d total albums (%d without MBID, %d with MBID). "
        "Sample without MBID: %s. Sample with MBID: %s",
        len(albums),
        len(albums_without_mbid),
        len(albums_with_mbid),
        sample_without_mbid,
        sample_with_mbid,
    )

    attached = 0
    merged = 0
    no_match = 0
    skipped_already_has_mbid = 0
    skipped_album_names_sample = []
    total_candidates = 0
    albums_to_populate: list[int] = []

    # Phase 1: Fast runtime backfill from existing tracks (DB-only, immediate)
    music_with_runtime = (
        Music.objects.filter(user=user, item__runtime_minutes__isnull=True)
        .exclude(track__duration_ms__isnull=True)
        .select_related("item", "track")
    )

    items_to_update_runtime = []
    for music in music_with_runtime:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                items_to_update_runtime.append(music.item)

    if items_to_update_runtime:
        Item.objects.bulk_update(items_to_update_runtime, ["runtime_minutes"], batch_size=500)
        logger.info(
            "enrich_albums_task: Backfilled %d runtimes from existing tracks",
            len(items_to_update_runtime),
        )

    # Phase 2: MBID resolution for albums
    albums_processed_count = 0
    for album in albums:
        albums_processed_count += 1
        # Log progress every 50 albums
        if albums_processed_count % 50 == 0 or albums_processed_count == len(albums):
            logger.info(
                "enrich_albums_task: Progress - processed %d/%d albums (current: '%s', id=%s)",
                albums_processed_count,
                len(albums),
                album.title if album.title else "Unknown",
                album.id,
            )

        # If missing MBID, try to attach one
        if album.musicbrainz_release_id or album.musicbrainz_release_group_id:
            skipped_already_has_mbid += 1
            if len(skipped_album_names_sample) < 20:
                skipped_album_names_sample.append(
                    f"{album.title} - {album.artist.name if album.artist else 'Unknown'}",
                )
        else:
            artist_name = album.artist.name if album.artist else None
            logger.info(
                "enrich_albums_task: Processing album '%s' (id=%s, artist='%s', no MBID)",
                album.title,
                album.id,
                artist_name or "Unknown",
            )
            try:
                release_group_id, release_id, cand_count, variant = resolve_album_mbid(
                    album.title or "",
                    artist_name,
                )
                total_candidates += cand_count
                logger.info(
                    "enrich_albums_task: resolve_album_mbid('%s', '%s') returned: release_group_id=%s, release_id=%s, candidates=%d, variant='%s'",
                    album.title or "",
                    artist_name or "None",
                    release_group_id or "None",
                    release_id or "None",
                    cand_count,
                    variant or "None",
                )
                if release_group_id or release_id:
                    logger.info(
                        "enrich_albums_task: attempting to attach MBIDs to album '%s' (id=%s) via variant '%s'",
                        album.title,
                        album.id,
                        variant or "None",
                    )
                    try:
                        # Update album with MBIDs
                        update_fields = []
                        if release_group_id and not album.musicbrainz_release_group_id:
                            album.musicbrainz_release_group_id = release_group_id
                            update_fields.append("musicbrainz_release_group_id")
                        if release_id and not album.musicbrainz_release_id:
                            album.musicbrainz_release_id = release_id
                            update_fields.append("musicbrainz_release_id")

                        if update_fields:
                            album.save(update_fields=update_fields)
                            attached += 1
                            logger.info(
                                "enrich_albums_task: SUCCESS - attached MBIDs to '%s' (id=%s) via '%s' (candidates=%d)",
                                album.title,
                                album.id,
                                variant or "None",
                                cand_count,
                            )
                    except IntegrityError as integrity_err:
                        logger.info(
                            "enrich_albums_task: IntegrityError attaching MBIDs to '%s' (id=%s) - MBID already exists, attempting merge",
                            album.title,
                            album.id,
                        )
                        # Find existing album with this release_group_id
                        existing = None
                        if release_group_id:
                            existing = Album.objects.filter(
                                musicbrainz_release_group_id=release_group_id,
                            ).exclude(id=album.id).first()
                        if not existing and release_id:
                            existing = Album.objects.filter(
                                musicbrainz_release_id=release_id,
                            ).exclude(id=album.id).first()

                        if existing:
                            logger.info(
                                "enrich_albums_task: found existing album '%s' (id=%s, release_group_id=%s) to merge into",
                                existing.title,
                                existing.id,
                                existing.musicbrainz_release_group_id or "None",
                            )
                            try:
                                # Merge album into existing (similar to _merge_album_into_target logic)
                                updates = set()
                                if (
                                    (not existing.image or existing.image == settings.IMG_NONE)
                                    and album.image
                                    and album.image != settings.IMG_NONE
                                ):
                                    existing.image = album.image
                                    updates.add("image")
                                if not existing.musicbrainz_release_id and album.musicbrainz_release_id:
                                    existing.musicbrainz_release_id = album.musicbrainz_release_id
                                    updates.add("musicbrainz_release_id")
                                if not existing.musicbrainz_release_group_id and album.musicbrainz_release_group_id:
                                    existing.musicbrainz_release_group_id = album.musicbrainz_release_group_id
                                    updates.add("musicbrainz_release_group_id")
                                if not existing.release_date and album.release_date:
                                    existing.release_date = album.release_date
                                    updates.add("release_date")
                                if not existing.release_type and album.release_type:
                                    existing.release_type = album.release_type
                                    updates.add("release_type")
                                if updates:
                                    existing.save(update_fields=list(updates))

                                # Merge album trackers
                                for tracker in AlbumTracker.objects.filter(album=album):
                                    existing_tracker = AlbumTracker.objects.filter(
                                        user=tracker.user,
                                        album=existing,
                                    ).first()
                                    if existing_tracker:
                                        if (
                                            tracker.start_date
                                            and (
                                                not existing_tracker.start_date
                                                or tracker.start_date < existing_tracker.start_date
                                            )
                                        ):
                                            existing_tracker.start_date = tracker.start_date
                                            existing_tracker.save(update_fields=["start_date"])
                                        tracker.delete()
                                    else:
                                        tracker.album = existing
                                        tracker.save(update_fields=["album"])

                                # Re-point music entries to the canonical album
                                Music.objects.filter(album=album).update(album=existing, track=None)

                                # Delete the source album
                                album.delete()
                                album = existing  # Use existing for further processing
                                merged += 1
                                logger.info(
                                    "enrich_albums_task: SUCCESS - merged album '%s' (id=%s) into '%s' (id=%s, release_group_id=%s) via variant '%s'",
                                    album.title if hasattr(album, "title") else "Unknown",
                                    album.id if hasattr(album, "id") else "Unknown",
                                    existing.title,
                                    existing.id,
                                    existing.musicbrainz_release_group_id or "None",
                                    variant or "None",
                                )
                            except Exception as merge_exc:
                                logger.warning(
                                    "enrich_albums_task: merge FAILED for '%s' (id=%s) into '%s' (id=%s): %s",
                                    album.title if hasattr(album, "title") else "Unknown",
                                    album.id if hasattr(album, "id") else "Unknown",
                                    existing.title,
                                    existing.id,
                                    merge_exc,
                                    exc_info=True,
                                )
                                # After failed merge, album might be invalid - skip remaining processing
                                if not album.pk:
                                    logger.warning(
                                        "enrich_albums_task: album '%s' invalid after failed merge, skipping remaining processing",
                                        album.title if hasattr(album, "title") else "Unknown",
                                    )
                                    continue
                        else:
                            logger.warning(
                                "enrich_albums_task: MBID attach failed for '%s' (id=%s) - MBID conflicts but no target album found (variant '%s', error: %s)",
                                album.title,
                                album.id,
                                variant or "None",
                                integrity_err,
                            )
                else:
                    no_match += 1
                    logger.info(
                        "enrich_albums_task: NO MATCH - resolve_album_mbid returned None for '%s' (id=%s, candidates=%d, variant='%s')",
                        album.title,
                        album.id,
                        cand_count,
                        variant or "None",
                    )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "enrich_albums_task: EXCEPTION - MBID resolution failed for '%s' (id=%s): %s",
                    album.title,
                    album.id,
                    exc,
                    exc_info=True,
                )

        # Skip remaining processing if album became invalid (e.g., deleted during merge)
        if not album.pk:
            logger.debug(
                "enrich_albums_task: skipping remaining processing for album '%s' (no pk after MBID resolution)",
                album.title if hasattr(album, "title") else "Unknown",
            )
            continue

        # Collect albums that need track population (only albums with MBIDs)
        if album.pk and (album.musicbrainz_release_id or album.musicbrainz_release_group_id):
            if not album.tracks_populated:
                albums_to_populate.append(album.id)

    # Phase 3: Populate tracks for albums that now have MBIDs
    populated_count = 0
    for album_id in albums_to_populate:
        try:
            album = Album.objects.filter(id=album_id).first()
            if not album:
                continue
            if album.tracks_populated:
                continue
            # Skip albums without MBIDs - can't populate tracks without them
            if not album.musicbrainz_release_id and not album.musicbrainz_release_group_id:
                continue

            count = populate_album_tracks(album)
            if count > 0:
                populated_count += 1
        except Exception as exc:
            logger.warning("Track populate failed for album %s: %s", album_id, exc)

    logger.info(
        "enrich_albums_task: Populated tracks for %d albums",
        populated_count,
    )

    # Phase 4: Link Music entries to tracks and backfill runtime
    if populated_count > 0:
        try:
            # Link Music entries to newly populated tracks
            link_result = link_music_to_tracks(user)

            # Backfill runtime from all available sources
            backfill_result = backfill_music_runtimes(user)

            logger.info(
                "enrich_albums_task: After populating tracks, linked %d Music->Track, backfilled %d runtimes",
                link_result.get("linked", 0),
                backfill_result.get("backfilled", 0),
            )
        except Exception as exc:
            logger.warning("Failed to link tracks/backfill runtime after track population: %s", exc)

    # Phase 5: Final runtime backfill from newly populated/linked tracks
    music_with_new_runtime = (
        Music.objects.filter(user=user, item__runtime_minutes__isnull=True)
        .exclude(track__duration_ms__isnull=True)
        .select_related("item", "track")
    )

    items_final_runtime = []
    for music in music_with_new_runtime:
        if music.track and music.track.duration_ms and music.item:
            runtime = _runtime_minutes_from_ms(music.track.duration_ms)
            if runtime:
                music.item.runtime_minutes = runtime
                items_final_runtime.append(music.item)

    if items_final_runtime:
        Item.objects.bulk_update(items_final_runtime, ["runtime_minutes"], batch_size=500)
        logger.info(
            "enrich_albums_task: Backfilled %d additional runtimes from newly linked tracks",
            len(items_final_runtime),
        )

    logger.info(
        "enrich_albums_task: Completed enrichment for user %s. "
        "Summary: %d total albums (%d skipped - already had MBID, %d processed without MBID). "
        "Results: attached %d MBIDs, merged %d, no match %d, populated tracks for %d albums. "
        "Sample skipped albums: %s",
        user_id,
        len(albums),
        skipped_already_has_mbid,
        len(albums_without_mbid),
        attached,
        merged,
        no_match,
        populated_count,
        skipped_album_names_sample[:10] if skipped_album_names_sample else [],
    )

    return {
        "albums": len(albums),
        "attached_mbid": attached,
        "merged_albums": merged,
        "no_mbid_match": no_match,
        "skipped_already_has_mbid": skipped_already_has_mbid,
        "candidate_sum": total_candidates,
        "albums_tracks_populated": populated_count,
    }


@shared_task
def prefetch_album_covers_batch(artist_ids: list[int], limit_per_artist: int | None = 10):
    """Prefetch album covers for a batch of artists (run after enrichment)."""
    from app.models import Artist
    from app.services.music import prefetch_album_covers

    updated = 0
    for artist_id in artist_ids:
        artist = Artist.objects.filter(id=artist_id, musicbrainz_id__isnull=False).first()
        if not artist:
            continue
        try:
            updated += prefetch_album_covers(artist, limit=limit_per_artist)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Cover batch prefetch failed for artist %s: %s", artist_id, exc)
    return {"artists": len(artist_ids), "covers_updated": updated}


@shared_task
def populate_episode_runtime_data():
    """Populate runtime data for episodes by syncing season metadata."""
    import time

    from app.models import Item, MediaTypes
    from app.providers import services
    from app.statistics import parse_runtime_to_minutes

    # Find episodes that need runtime data
    episodes_needing_runtime = Item.objects.filter(
        runtime_minutes__isnull=True,
        media_type=MediaTypes.EPISODE.value,
        source__in=["tmdb", "mal", "simkl"],
    ).exclude(
        runtime_minutes=999999,
    )

    if not episodes_needing_runtime.exists():
        logger.info("No episodes need runtime data")
        return {"updated": 0, "errors": 0, "message": "No episodes need runtime data"}

    updated_count = 0
    error_count = 0
    processed_seasons = set()

    for episode in episodes_needing_runtime:
        try:
            # Create a season key to avoid processing the same season multiple times
            season_key = (episode.media_id, episode.source, episode.season_number)

            if season_key in processed_seasons:
                continue

            processed_seasons.add(season_key)

            # Get season metadata to populate episode runtime data
            season_metadata = services.get_media_metadata(
                "tv_with_seasons",
                episode.media_id,
                episode.source,
                [episode.season_number],
            )

            if not season_metadata or f"season/{episode.season_number}" not in season_metadata:
                logger.warning(f"No season metadata for {episode.title} S{episode.season_number}")
                error_count += 1
                continue

            season_data = season_metadata[f"season/{episode.season_number}"]

            # Process episodes to get runtime data
            from app.providers import tmdb
            episodes_metadata = tmdb.process_episodes(season_data, [])

            # Update episodes with runtime data
            for ep_data in episodes_metadata:
                if ep_data.get("runtime"):
                    episode_item, created = Item.objects.update_or_create(
                        media_id=episode.media_id,
                        source=episode.source,
                        media_type=MediaTypes.EPISODE.value,
                        season_number=episode.season_number,
                        episode_number=ep_data["episode_number"],
                        defaults={
                            "title": episode.title,  # Keep existing title
                            "image": ep_data.get("image", episode.image),
                            "runtime_minutes": parse_runtime_to_minutes(ep_data["runtime"]),
                        },
                    )

                    if not created and episode_item.runtime_minutes:
                        updated_count += 1
                        logger.info(f"Updated runtime for {episode_item.title} S{episode.season_number}E{ep_data['episode_number']}: {episode_item.runtime_minutes} minutes")

            # Small delay to avoid rate limiting
            time.sleep(0.1)

        except Exception as e:
            logger.error(f"Error processing episode {episode.title}: {e}")
            error_count += 1
            continue

    logger.info(f"Episode runtime population completed: {updated_count} episodes updated, {error_count} errors")

    # Mark runtime population as completed since both movies/TV/anime and episodes are done
    from django.core.cache import cache
    cache.set("runtime_population_completed", True, timeout=3600)  # 1 hour
    logger.info("🎉 All runtime data population completed! Movies, TV shows, anime, and episodes all processed.")

    return {
        "updated": updated_count,
        "errors": error_count,
        "message": f"Processed {len(processed_seasons)} seasons, updated {updated_count} episodes. All runtime data population completed!",
    }
