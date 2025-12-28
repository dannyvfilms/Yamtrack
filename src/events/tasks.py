import logging

from celery import shared_task

from app.services import auto_pause
from events import calendar, notifications

logger = logging.getLogger(__name__)


@shared_task(name="Reload calendar")
def reload_calendar(user=None, items_to_process=None):
    """Refresh the calendar with latest dates for all users."""
    if user:
        logger.info("Reloading calendar for user: %s", user.username)
    else:
        logger.info("Reloading calendar for all users")

    result = calendar.fetch_releases(
        user=user,
        items_to_process=items_to_process,
    )

    if user is None:
        auto_pause.auto_pause_stale_items()
        # Also refresh podcast episodes from RSS feeds
        try:
            refresh_podcast_episodes()
        except Exception as e:
            logger.error("Failed to refresh podcast episodes during calendar reload: %s", e)

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
