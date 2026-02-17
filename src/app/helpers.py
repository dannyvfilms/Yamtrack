from urllib.parse import parse_qsl, urlencode, urlparse

from django.apps import apps
from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.utils.encoding import iri_to_uri
from django.utils.http import url_has_allowed_host_and_scheme

from app.models import BasicMedia, CollectionEntry, MediaTypes, Status


def minutes_to_hhmm(total_minutes):
    """Convert total minutes to HH:MM format."""
    hours = int(total_minutes / 60)
    minutes = int(total_minutes % 60)
    if hours == 0:
        return f"{minutes}min"
    return f"{hours}h {minutes:02d}min"


def redirect_back(request):
    """Redirect to the previous page, removing the 'page' parameter if present."""
    if url_has_allowed_host_and_scheme(request.GET.get("next"), None):
        next_url = request.GET["next"]

        # Parse the URL
        parsed_url = urlparse(next_url)

        # Get the query parameters and remove params we don't want
        query_params = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
        query_params.pop("page", None)
        query_params.pop("load_media_type", None)

        # Reconstruct the URL
        new_query = urlencode(query_params)
        new_parts = list(parsed_url)
        new_parts[4] = new_query  # index 4 is the query part

        # Convert back to a URL string
        clean_url = iri_to_uri(parsed_url._replace(query=new_query).geturl())

        return HttpResponseRedirect(clean_url)

    return redirect("home")


def form_error_messages(form, request):
    """Display form errors as messages."""
    for field, errors in form.errors.items():
        for error in errors:
            messages.error(
                request,
                f"{field.replace('_', ' ').title()}: {error}",
            )


def format_search_response(page, per_page, total_results, results):
    """Format the search response for pagination."""
    return {
        "page": page,
        "total_results": total_results,
        "total_pages": total_results // per_page + 1,
        "results": results,
    }


def enrich_items_with_user_data(request, items, section_name=None, user=None):
    """Enrich a list of items with user tracking data."""
    if not items:
        return []

    # Use provided user or fall back to request.user
    # If user is provided, use it (should be authenticated list owner)
    # If user is None and request.user is AnonymousUser, skip enrichment
    if user is not None:
        target_user = user
    elif request.user.is_authenticated:
        target_user = request.user
    else:
        # Anonymous user with no provided user - return items without enrichment
        return [{"item": item, "media": None} for item in items]

    # All items are the same media type
    media_type = items[0]["media_type"]
    source = items[0]["source"]

    # Build Q objects for all items
    q_objects = Q()
    for item in items:
        filter_params = {
            "item__media_id": item["media_id"],
            "item__media_type": media_type,
            "item__source": source,
        }

        if media_type == MediaTypes.SEASON.value:
            filter_params["item__season_number"] = item.get("season_number")

        q_objects |= Q(**filter_params)

    q_objects &= Q(user=target_user)

    # Bulk fetch all media with prefetch
    model = apps.get_model(app_label="app", model_name=media_type)
    media_queryset = model.objects.filter(q_objects).select_related("item")
    media_queryset = BasicMedia.objects._apply_prefetch_related(
        media_queryset,
        media_type,
    )
    BasicMedia.objects.annotate_max_progress(media_queryset, media_type)

    # For podcasts, order by created_at descending to get most recent entry when multiple exist
    # This allows multiple plays of the same episode to be tracked separately in the DB
    # but we show the most recent one in the UI
    if media_type == MediaTypes.PODCAST.value:
        media_queryset = media_queryset.order_by("item__media_id", "item__source", "-created_at")

    # Create a lookup dictionary for fast matching
    # For podcasts with multiple entries, keep only the most recent one (first after ordering)
    media_lookup = {}
    media_grouped = {}
    for media in media_queryset:
        if media_type == MediaTypes.SEASON.value:
            key = (media.item.media_id, media.item.source, media.item.season_number)
        else:
            key = (media.item.media_id, media.item.source)

        media_grouped.setdefault(key, []).append(media)

        # Only store the first (most recent for podcasts) entry for each key
        if key not in media_lookup:
            media_lookup[key] = media

    # Aggregate duplicates for non-podcast media to expose total progress/recent status
    if media_type != MediaTypes.PODCAST.value:
        for key, entries in media_grouped.items():
            if len(entries) > 1 and key in media_lookup:
                BasicMedia.objects._aggregate_item_data(media_lookup[key], entries)

    # Enrich items with matched media
    enriched_items = []
    for item in items:
        if media_type == MediaTypes.SEASON.value:
            key = (str(item["media_id"]), item["source"], item.get("season_number"))
        else:
            key = (str(item["media_id"]), item["source"])

        media_item = media_lookup.get(key)
        if (
            getattr(target_user, "hide_completed_recommendations", False)
            and section_name == "recommendations"
            and media_item
            and media_item.status == Status.COMPLETED.value
        ):
            continue

        enriched_item = {
            "item": item,
            "media": media_item,
        }
        enriched_items.append(enriched_item)

    return enriched_items


def extract_release_datetime(metadata):
    """Extract release datetime from metadata dict."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    date_str = None
    for field in ["release_date", "first_air_date", "start_date", "publish_date"]:
        if metadata.get("details", {}).get(field):
            date_str = metadata["details"][field]
            break
        if metadata.get(field):
            date_str = metadata[field]
            break

    if not date_str:
        year = metadata.get("details", {}).get("year") or metadata.get("year")
        if year:
            try:
                return datetime(int(year), 1, 1, tzinfo=ZoneInfo("UTC"))
            except (ValueError, TypeError):
                return None
        return None

    if isinstance(date_str, datetime):
        if date_str.tzinfo is None:
            return date_str.replace(tzinfo=ZoneInfo("UTC"))
        return date_str

    date_str = str(date_str)
    format_lengths = {
        "%Y-%m-%d": 10,
        "%Y-%m": 7,
        "%Y": 4,
    }
    for fmt, length in format_lengths.items():
        try:
            dt = datetime.strptime(date_str[:length], fmt)
            return dt.replace(tzinfo=ZoneInfo("UTC"))
        except (ValueError, TypeError):
            continue

    return None


def get_user_collection(user, media_type=None):
    """Get user's collection entries with optional media type filtering.

    Args:
        user: Django user object
        media_type: Optional media type to filter by

    Returns:
        QuerySet of CollectionEntry objects
    """
    queryset = CollectionEntry.objects.filter(user=user).select_related("item")
    if media_type:
        queryset = queryset.filter(item__media_type=media_type)
    return queryset


def get_item_collection_entries(user, item):
    """Return all collection entries for a specific user/item pair."""
    return CollectionEntry.objects.filter(
        user=user,
        item=item,
    ).order_by("-collected_at", "-id")


def is_item_collected(user, item):
    """Check if a specific item is in user's collection.

    Args:
        user: Django user object
        item: Item object to check

    Returns:
        Most recently collected CollectionEntry object if found, None otherwise
    """
    return get_item_collection_entries(user, item).first()


def get_album_collection_metadata(user, album):
    """Get aggregated collection metadata for an album from all its tracks.
    
    For music albums, we aggregate collection metadata from all tracks that have
    collection entries. Returns the most common values (or first non-empty value)
    for fields that should be consistent across tracks (like audio_codec, audio_channels).
    
    Args:
        user: Django user object
        album: Album object
        
    Returns:
        Dictionary with collection metadata:
        - audio_codec: Most common audio codec across collected tracks
        - audio_channels: Most common audio channels across collected tracks
        - media_type: Most common media_type across collected tracks
        - has_collection: Boolean indicating if any tracks are collected
        - collected_count: Number of tracks with collection entries
    """
    from app.models import Music
    
    # Get all Music entries for this album
    music_entries = Music.objects.filter(
        user=user,
        album=album,
    ).select_related("item")
    
    # Get collection entries for all items in this album
    item_ids = [m.item_id for m in music_entries if m.item_id]
    if not item_ids:
        return {
            "has_collection": False,
            "collected_count": 0,
            "audio_codec": None,
            "audio_channels": None,
            "bitrate": None,
            "media_type": None,
        }
    
    collection_entries = CollectionEntry.objects.filter(
        user=user,
        item_id__in=item_ids,
    ).select_related("item")
    
    if not collection_entries.exists():
        return {
            "has_collection": False,
            "collected_count": 0,
            "audio_codec": None,
            "audio_channels": None,
            "media_type": None,
        }
    
    # Aggregate metadata - find most common values
    audio_codecs = {}
    audio_channels_list = {}
    bitrates = {}
    media_types = {}
    
    for entry in collection_entries:
        if entry.audio_codec:
            audio_codecs[entry.audio_codec] = audio_codecs.get(entry.audio_codec, 0) + 1
        if entry.audio_channels:
            audio_channels_list[entry.audio_channels] = audio_channels_list.get(entry.audio_channels, 0) + 1
        if entry.bitrate:
            bitrates[entry.bitrate] = bitrates.get(entry.bitrate, 0) + 1
        if entry.media_type:
            media_types[entry.media_type] = media_types.get(entry.media_type, 0) + 1
    
    # Get most common value (or first if tie)
    audio_codec = max(audio_codecs.items(), key=lambda x: x[1])[0] if audio_codecs else None
    audio_channels = max(audio_channels_list.items(), key=lambda x: x[1])[0] if audio_channels_list else None
    bitrate = max(bitrates.items(), key=lambda x: x[1])[0] if bitrates else None
    media_type = max(media_types.items(), key=lambda x: x[1])[0] if media_types else None
    
    return {
        "has_collection": True,
        "collected_count": collection_entries.count(),
        "audio_codec": audio_codec,
        "audio_channels": audio_channels,
        "bitrate": bitrate,
        "media_type": media_type,
    }


def get_collection_stats(user):
    """Get collection statistics for a user.

    Args:
        user: Django user object

    Returns:
        Dictionary with collection statistics:
        - total: Total number of collection entries
        - by_media_type: Count by media type
        - by_format: Count by media_type (format) field
    """
    collection = CollectionEntry.objects.filter(user=user)
    stats = {
        "total": collection.count(),
        "by_media_type": {},
        "by_format": {},
    }

    # Count by media type (Item.media_type)
    for entry in collection.select_related("item"):
        item_media_type = entry.item.media_type
        stats["by_media_type"][item_media_type] = (
            stats["by_media_type"].get(item_media_type, 0) + 1
        )

        # Count by format (CollectionEntry.media_type)
        if entry.media_type:
            stats["by_format"][entry.media_type] = (
                stats["by_format"].get(entry.media_type, 0) + 1
            )

    return stats


def get_artist_collection_stats(user, artist):
    """Get collection statistics for an artist.
    
    Args:
        user: Django user object
        artist: Artist object
        
    Returns:
        Dictionary with collection statistics:
        - collected_albums: Number of distinct albums with at least one collected track
        - collected_tracks: Total number of collected tracks from this artist
    """
    from app.models import Album, Music
    
    # Get all albums for this artist
    albums = Album.objects.filter(artist=artist)
    total_albums = albums.count()
    
    # Get all music entries (tracks) for these albums
    music_entries = Music.objects.filter(
        user=user,
        album__in=albums,
    ).select_related("item", "album")
    
    # Count total tracks (all tracks from all albums for this artist)
    total_tracks = music_entries.count()
    
    # Get collection entries for all items from this artist
    item_ids = [m.item_id for m in music_entries if m.item_id]
    if not item_ids:
        return {
            "collected_albums": 0,
            "total_albums": total_albums,
            "collected_tracks": 0,
            "total_tracks": total_tracks,
        }
    
    collection_entries = CollectionEntry.objects.filter(
        user=user,
        item_id__in=item_ids,
    ).select_related("item")
    
    if not collection_entries.exists():
        return {
            "collected_albums": 0,
            "total_albums": total_albums,
            "collected_tracks": 0,
            "total_tracks": total_tracks,
        }
    
    # Count distinct albums that have at least one collected track
    collected_album_ids = set()
    collected_track_count = 0
    
    for entry in collection_entries:
        # Find which album this track belongs to
        music_entry = music_entries.filter(item_id=entry.item_id).first()
        if music_entry and music_entry.album_id:
            collected_album_ids.add(music_entry.album_id)
        collected_track_count += 1
    
    return {
        "collected_albums": len(collected_album_ids),
        "total_albums": total_albums,
        "collected_tracks": collected_track_count,
        "total_tracks": total_tracks,
    }


def get_tv_show_collection_stats(user, tv_item, metadata_episode_count=None):
    """Get collection statistics for a TV show.
    
    Args:
        user: Django user object
        tv_item: Item object with media_type='tv' or 'anime'
        metadata_episode_count: Optional episode count from metadata (e.g., TMDB) to match Details pane
        
    Returns:
        Dictionary with collection statistics:
        - collected_seasons: Number of distinct seasons with at least one collected episode
        - collected_episodes: Total number of collected episodes from this show
        - total_seasons: Total number of seasons for this show
        - total_episodes: Total number of episodes for this show
    """
    from app.models import TV, Season, Episode, Item, MediaTypes
    
    # Always count total seasons and episodes from Item objects (all available, not just tracked)
    # This matches what the Details pane shows
    # Exclude Season 0 (Specials) to match Details pane behavior
    all_season_items = Item.objects.filter(
        media_id=tv_item.media_id,
        source=tv_item.source,
        media_type__in=[MediaTypes.SEASON.value],
    ).exclude(season_number=0)  # Exclude Season 0 (Specials)
    total_seasons = all_season_items.count()
    
    # Exclude episodes from Season 0 to match Details pane
    all_episode_items = Item.objects.filter(
        media_id=tv_item.media_id,
        source=tv_item.source,
        media_type__in=[MediaTypes.EPISODE.value],
    ).exclude(season_number=0)  # Exclude Season 0 episodes
    
    # Use metadata episode count if provided (matches Details pane), otherwise count from Items
    if metadata_episode_count is not None:
        total_episodes = metadata_episode_count
    else:
        total_episodes = all_episode_items.count()
    
    # Get collection entries for all seasons and episodes
    season_item_ids = list(all_season_items.values_list('id', flat=True))
    episode_item_ids = list(all_episode_items.values_list('id', flat=True))
    
    if not season_item_ids and not episode_item_ids:
        return {
            "collected_seasons": 0,
            "total_seasons": total_seasons,
            "collected_episodes": 0,
            "total_episodes": total_episodes,
        }
    
    # Get collection entries for seasons
    season_collection_entries = CollectionEntry.objects.filter(
        user=user,
        item_id__in=season_item_ids,
    ) if season_item_ids else CollectionEntry.objects.none()
    
    # Get collection entries for episodes
    episode_collection_entries = CollectionEntry.objects.filter(
        user=user,
        item_id__in=episode_item_ids,
    ) if episode_item_ids else CollectionEntry.objects.none()
    
    # Count distinct seasons that have at least one collected episode
    # A season is "collected" if either:
    # 1. The season Item itself has a collection entry, OR
    # 2. At least one episode in that season has a collection entry
    collected_season_ids = set()
    
    # Add seasons that have direct collection entries
    for entry in season_collection_entries:
        collected_season_ids.add(entry.item_id)
    
    # Add seasons that have at least one collected episode
    # We need to map episode items back to their season items
    for entry in episode_collection_entries:
        episode_item = all_episode_items.filter(id=entry.item_id).first()
        if episode_item and episode_item.season_number is not None:
            # Find the season item for this episode
            season_item = all_season_items.filter(
                season_number=episode_item.season_number,
            ).first()
            if season_item:
                collected_season_ids.add(season_item.id)
    
    return {
        "collected_seasons": len(collected_season_ids),
        "total_seasons": total_seasons,
        "collected_episodes": episode_collection_entries.count(),
        "total_episodes": total_episodes,
    }


def get_season_collection_stats(user, season_item):
    """Get collection statistics for a specific season.
    
    Args:
        user: Django user object
        season_item: Item object with media_type='season'
        
    Returns:
        Dictionary with collection statistics:
        - collected_episodes: Number of collected episodes in this season
        - total_episodes: Total number of episodes in this season
    """
    from app.models import Item, MediaTypes
    
    # Get all episodes for this season
    all_episode_items = Item.objects.filter(
        media_id=season_item.media_id,
        source=season_item.source,
        media_type=MediaTypes.EPISODE.value,
        season_number=season_item.season_number,
    )
    total_episodes = all_episode_items.count()
    
    if total_episodes == 0:
        return {
            "collected_episodes": 0,
            "total_episodes": 0,
        }
    
    # Get collection entries for episodes in this season
    episode_item_ids = list(all_episode_items.values_list('id', flat=True))
    episode_collection_entries = CollectionEntry.objects.filter(
        user=user,
        item_id__in=episode_item_ids,
    )
    
    collected_count = episode_collection_entries.count()
    
    # If no episode-level entries exist, check if there's a show-level collection entry
    # This is a heuristic: if the show is marked as collected, consider all episodes collected
    if collected_count == 0:
        try:
            tv_item = Item.objects.get(
                media_id=season_item.media_id,
                source=season_item.source,
                media_type=MediaTypes.TV.value,
            )
            show_collection_entry = CollectionEntry.objects.filter(
                user=user,
                item=tv_item,
            ).exists()
            
            # If show-level entry exists and no granular episode entries, consider all episodes collected
            if show_collection_entry:
                collected_count = total_episodes
        except Item.DoesNotExist:
            pass
    
    return {
        "collected_episodes": collected_count,
        "total_episodes": total_episodes,
    }


def get_season_collection_metadata(user, season_item):
    """Get aggregated collection metadata for a season from all its episodes.
    
    Similar to get_album_collection_metadata, this aggregates collection metadata
    from all episodes in the season that have collection entries. Returns the most
    common values for fields that should be consistent across episodes.
    
    Args:
        user: Django user object
        season_item: Item object with media_type='season'
        
    Returns:
        Dictionary with collection metadata (or None if no episodes are collected):
        - resolution: Most common resolution across collected episodes
        - hdr: Most common HDR format across collected episodes
        - audio_codec: Most common audio codec across collected episodes
        - audio_channels: Most common audio channels across collected episodes
        - bitrate: Most common bitrate across collected episodes
        - media_type: Most common media_type across collected episodes
        - is_3d: True if any episode is 3D
        - collected_at: Earliest collected_at date from all episodes
    """
    from app.models import Item, MediaTypes
    from django.db.models import Count
    
    # Get all episodes for this season
    all_episode_items = Item.objects.filter(
        media_id=season_item.media_id,
        source=season_item.source,
        media_type=MediaTypes.EPISODE.value,
        season_number=season_item.season_number,
    )
    
    if not all_episode_items.exists():
        return None
    
    # Get collection entries for episodes in this season
    episode_item_ids = list(all_episode_items.values_list('id', flat=True))
    collected_episodes = CollectionEntry.objects.filter(
        user=user,
        item_id__in=episode_item_ids,
    )
    
    if not collected_episodes.exists():
        # Check if there's a season-level or show-level collection entry
        season_collection_entry = CollectionEntry.objects.filter(
            user=user,
            item=season_item,
        ).first()
        
        if season_collection_entry:
            # Return the season-level entry metadata
            return {
                "resolution": season_collection_entry.resolution or "",
                "hdr": season_collection_entry.hdr or "",
                "audio_codec": season_collection_entry.audio_codec or "",
                "audio_channels": season_collection_entry.audio_channels or "",
                "bitrate": season_collection_entry.bitrate,
                "media_type": season_collection_entry.media_type or "",
                "is_3d": season_collection_entry.is_3d,
                "collected_at": season_collection_entry.collected_at,
            }
        
        # Check for show-level entry
        try:
            tv_item = Item.objects.get(
                media_id=season_item.media_id,
                source=season_item.source,
                media_type=MediaTypes.TV.value,
            )
            show_collection_entry = CollectionEntry.objects.filter(
                user=user,
                item=tv_item,
            ).first()
            
            if show_collection_entry:
                # Return the show-level entry metadata
                return {
                    "resolution": show_collection_entry.resolution or "",
                    "hdr": show_collection_entry.hdr or "",
                    "audio_codec": show_collection_entry.audio_codec or "",
                    "audio_channels": show_collection_entry.audio_channels or "",
                    "bitrate": show_collection_entry.bitrate,
                    "media_type": show_collection_entry.media_type or "",
                    "is_3d": show_collection_entry.is_3d,
                    "collected_at": show_collection_entry.collected_at,
                }
        except Item.DoesNotExist:
            pass
        
        return None
    
    # Aggregate the most common values for each metadata field
    def get_most_common(queryset, field_name):
        counts = (
            queryset.exclude(**{field_name: ""})
            .exclude(**{field_name: None})
            .values(field_name)
            .annotate(count=Count(field_name))
            .order_by("-count")
            .first()
        )
        return counts[field_name] if counts else None
    
    # Get most common values
    resolution = get_most_common(collected_episodes, "resolution")
    hdr = get_most_common(collected_episodes, "hdr")
    audio_codec = get_most_common(collected_episodes, "audio_codec")
    audio_channels = get_most_common(collected_episodes, "audio_channels")
    media_type = get_most_common(collected_episodes, "media_type")
    
    # For bitrate, get the most common non-null value
    bitrate_counts = (
        collected_episodes.exclude(bitrate=None)
        .values("bitrate")
        .annotate(count=Count("bitrate"))
        .order_by("-count")
        .first()
    )
    bitrate = bitrate_counts["bitrate"] if bitrate_counts else None
    
    # For is_3d, check if any episode is 3D
    is_3d = collected_episodes.filter(is_3d=True).exists()
    
    # Get earliest collected_at date
    earliest_collected = collected_episodes.order_by("collected_at").first()
    collected_at = earliest_collected.collected_at if earliest_collected else None
    
    return {
        "resolution": resolution or "",
        "hdr": hdr or "",
        "audio_codec": audio_codec or "",
        "audio_channels": audio_channels or "",
        "bitrate": bitrate,
        "media_type": media_type or "",
        "is_3d": is_3d,
        "collected_at": collected_at,
    }
