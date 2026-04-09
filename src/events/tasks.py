import logging

from celery import shared_task
from django.contrib.auth import get_user_model

from app.models import Item
from app.services import auto_pause
from events import notifications
from events.calendar.main import fetch_releases

logger = logging.getLogger(__name__)


def _normalize_user_id(user_or_id):
    """Coerce a User instance or scalar value into a user ID."""
    if user_or_id is None:
        return None
    candidate = getattr(user_or_id, "pk", user_or_id)
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


def _normalize_item_ids(item_ids):
    """Coerce a list of Item instances or scalar values into item IDs."""
    if item_ids is None:
        return None

    normalized = []
    for item in item_ids:
        candidate = getattr(item, "pk", item)
        try:
            normalized.append(int(candidate))
        except (TypeError, ValueError):
            continue
    return normalized


@shared_task(name="Reload calendar", ignore_result=True)
def reload_calendar(user_id=None, item_ids=None, user=None, items_to_process=None):
    """Refresh the calendar with latest dates for all users."""
    normalized_user_id = _normalize_user_id(user_id)
    if normalized_user_id is None:
        normalized_user_id = _normalize_user_id(user)

    normalized_item_ids = _normalize_item_ids(item_ids)
    if normalized_item_ids is None:
        normalized_item_ids = _normalize_item_ids(items_to_process)

    resolved_user = None
    if normalized_user_id is not None:
        User = get_user_model()
        resolved_user = User.objects.filter(id=normalized_user_id).first()
        if resolved_user is None:
            logger.warning("Skipping calendar reload for missing user_id=%s", normalized_user_id)
            return "User not found"
        logger.info("Reloading calendar for user: %s", resolved_user.username)
    else:
        logger.info("Reloading calendar for all users")

    resolved_items = None
    if normalized_item_ids is not None:
        item_lookup = Item.objects.in_bulk(normalized_item_ids)
        resolved_items = [
            item_lookup[item_id]
            for item_id in normalized_item_ids
            if item_id in item_lookup
        ]
        missing_item_ids = [
            item_id for item_id in normalized_item_ids if item_id not in item_lookup
        ]
        if missing_item_ids:
            logger.info(
                "Calendar reload skipped %d missing item IDs",
                len(missing_item_ids),
            )

    result = fetch_releases(
        user=resolved_user,
        items_to_process=resolved_items,
    )

    if resolved_user is None and normalized_item_ids is None:
        auto_pause.auto_pause_stale_items()
        # Only refresh podcast feeds during full calendar runs.
        try:
            refresh_podcast_episodes()
        except Exception as e:
            logger.error("Failed to refresh podcast episodes during calendar reload: %s", e)

        # Backfill metadata for items that have never been fetched
        # Use aggressive batch size to complete initial backfill quickly
        try:
            from app.tasks import backfill_item_metadata_task, count_release_backfill_items

            remaining_metadata_count = Item.objects.filter(metadata_fetched_at__isnull=True).count()
            remaining_release_count = count_release_backfill_items()

            # Use larger batch for initial metadata imports, then keep release backfill
            # running nightly so stale cached metadata can be corrected over time.
            if remaining_metadata_count > 1000:
                batch_size = 5000  # Aggressive initial backfill
                logger.info(
                    "Initial metadata backfill: processing %s items (batch of 5000)",
                    remaining_metadata_count,
                )
            elif remaining_metadata_count > 0:
                batch_size = 1000  # Cleanup mode
                logger.info(
                    "Metadata backfill cleanup: processing remaining %s items",
                    remaining_metadata_count,
                )
            elif remaining_release_count > 0:
                batch_size = 1000  # Release-date maintenance mode
                logger.info(
                    "Release-date backfill maintenance: processing remaining %s items",
                    remaining_release_count,
                )
            else:
                batch_size = 0  # Skip if nothing to do

            if batch_size > 0:
                backfill_result = backfill_item_metadata_task(batch_size=batch_size)
                logger.info(
                    (
                        "Metadata backfill completed: %s successful, %s release dates updated, "
                        "%s errors, %s metadata remaining, %s release remaining"
                    ),
                    backfill_result.get("success_count", 0),
                    backfill_result.get("release_updated_count", 0),
                    backfill_result.get("error_count", 0),
                    backfill_result.get("remaining_metadata", 0),
                    backfill_result.get("remaining_release", 0),
                )
        except Exception as e:
            logger.error("Failed to backfill metadata during calendar reload: %s", e)

    return result


@shared_task(name="Send release notifications")
def send_release_notifications():
    """Send notifications for recently released media."""
    logger.info("Starting recent release notification task")

    return notifications.send_releases()


@shared_task(name="Send daily digest")
def send_daily_digest_notifications():
    """Send daily digest of today's releases."""
    logger.info("Starting daily digest task")

    return notifications.send_daily_digest()


@shared_task(name="Refresh podcast episodes")
def refresh_podcast_episodes():
    """Refresh episode lists from RSS feeds for all podcast shows.
    
    Fetches latest episodes from RSS feeds and updates the database.
    This ensures we have the complete episode list, including new episodes
    that haven't been listened to yet.
    """
    from app.models import PodcastShow
    from integrations import podcast_rss

    logger.info("Starting podcast episode refresh task")

    # Get all shows with RSS feed URLs
    shows = PodcastShow.objects.filter(rss_feed_url__isnull=False).exclude(rss_feed_url="")

    if not shows.exists():
        logger.info("No podcast shows with RSS feed URLs found")
        return "No shows to refresh"

    updated_count = 0
    error_count = 0

    for show in shows:
        try:
            # Use the sync method from PocketCastsImporter
            # We need a user instance, but for periodic refresh we'll use the first user
            # who has this show tracked, or create a dummy importer instance
            # Actually, let's just call the RSS sync directly
            rss_episodes = podcast_rss.fetch_episodes_from_rss(show.rss_feed_url)

            if not rss_episodes:
                logger.debug("No episodes found in RSS feed for show %s", show.title)
                continue

            # Get existing episodes
            from app.models import PodcastEpisode
            existing_episodes = {
                episode.episode_uuid: episode
                for episode in PodcastEpisode.objects.filter(show=show)
            }

            # Also create lookup by title + published date
            existing_by_title_date = {}
            for episode in existing_episodes.values():
                if episode.title and episode.published:
                    key = (episode.title.lower().strip(), episode.published.date())
                    existing_by_title_date[key] = episode

            created_count = 0
            updated_count_show = 0

            for rss_ep in rss_episodes:
                matched_episode = None

                # Try by GUID
                if rss_ep.get("guid"):
                    matched_episode = existing_episodes.get(rss_ep["guid"])

                # Try by title + date
                if not matched_episode and rss_ep.get("title") and rss_ep.get("published"):
                    title_key = (rss_ep["title"].lower().strip(), rss_ep["published"].date())
                    matched_episode = existing_by_title_date.get(title_key)

                if matched_episode:
                    # Update existing
                    updated = False
                    update_fields = []

                    # If UUID differs and we have RSS GUID, update to RSS GUID
                    # This ensures consistency when Pocket Casts UUID and RSS GUID differ
                    # But prefer keeping Pocket Casts UUID format if it looks like one
                    if rss_ep.get("guid") and matched_episode.episode_uuid != rss_ep["guid"]:
                        # Only update if the matched episode doesn't look like a Pocket Casts UUID
                        # Pocket Casts UUIDs typically have hyphens in specific positions
                        is_pocketcasts_uuid = len(matched_episode.episode_uuid) == 36 and matched_episode.episode_uuid.count("-") == 4
                        if not is_pocketcasts_uuid:
                            logger.info(
                                "Updating episode UUID from %s to %s for episode %s (RSS GUID)",
                                matched_episode.episode_uuid,
                                rss_ep["guid"],
                                matched_episode.title,
                            )
                            matched_episode.episode_uuid = rss_ep["guid"]
                            updated = True
                            update_fields.append("episode_uuid")

                    if rss_ep.get("title") and matched_episode.title != rss_ep["title"]:
                        matched_episode.title = rss_ep["title"]
                        updated = True
                        update_fields.append("title")
                    if rss_ep.get("published") and matched_episode.published != rss_ep["published"]:
                        matched_episode.published = rss_ep["published"]
                        updated = True
                        update_fields.append("published")
                    if rss_ep.get("duration") and matched_episode.duration != rss_ep["duration"]:
                        matched_episode.duration = rss_ep["duration"]
                        updated = True
                        update_fields.append("duration")
                    if rss_ep.get("audio_url") and matched_episode.audio_url != rss_ep["audio_url"]:
                        matched_episode.audio_url = rss_ep["audio_url"]
                        updated = True
                        update_fields.append("audio_url")
                    if rss_ep.get("episode_number") is not None and matched_episode.episode_number != rss_ep["episode_number"]:
                        matched_episode.episode_number = rss_ep["episode_number"]
                        updated = True
                        update_fields.append("episode_number")
                    if rss_ep.get("season_number") is not None and matched_episode.season_number != rss_ep["season_number"]:
                        matched_episode.season_number = rss_ep["season_number"]
                        updated = True
                        update_fields.append("season_number")

                    if updated:
                        matched_episode.save(update_fields=update_fields)
                        updated_count_show += 1
                else:
                    # Create new episode
                    import hashlib
                    episode_uuid = rss_ep.get("guid")
                    if not episode_uuid:
                        uuid_str = f"{rss_ep.get('title', '')}{rss_ep.get('published', '')}"
                        episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                    if episode_uuid in existing_episodes:
                        continue

                    PodcastEpisode.objects.create(
                        show=show,
                        episode_uuid=episode_uuid,
                        title=rss_ep.get("title", "Unknown Episode"),
                        published=rss_ep.get("published"),
                        duration=rss_ep.get("duration"),
                        audio_url=rss_ep.get("audio_url", ""),
                        episode_number=rss_ep.get("episode_number"),
                        season_number=rss_ep.get("season_number"),
                    )
                    created_count += 1

            if created_count > 0 or updated_count_show > 0:
                logger.info(
                    "Refreshed episodes for show %s: %d created, %d updated",
                    show.title,
                    created_count,
                    updated_count_show,
                )
                updated_count += 1

        except Exception as e:
            logger.error("Failed to refresh episodes for show %s: %s", show.title, e, exc_info=True)
            error_count += 1

    # Clean up duplicate episodes after refreshing
    try:
        from integrations.imports.pocketcasts import _cleanup_duplicate_episodes_global

        logger.info("Running duplicate episode cleanup after RSS refresh")
        cleanup_stats = _cleanup_duplicate_episodes_global()
        if cleanup_stats.get("duplicates_removed", 0) > 0:
            logger.info(
                "Cleaned up %d duplicate podcast episodes after RSS refresh",
                cleanup_stats["duplicates_removed"],
            )
    except Exception as e:
        logger.error("Failed to cleanup duplicate episodes: %s", e, exc_info=True)
        # Don't fail the whole task if cleanup fails

    result = f"Refreshed {updated_count} shows, {error_count} errors"
    logger.info("Podcast episode refresh completed: %s", result)
    return result
