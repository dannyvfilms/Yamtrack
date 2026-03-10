import hashlib
import logging
import os
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import jwt
import requests
from django.db import transaction
from django.utils import timezone

import app
from app.log_safety import exception_summary, safe_url
from app.models import (
    Episode,
    MediaTypes,
    Movie,
    Music,
    Podcast,
    PodcastEpisode,
    PodcastShow,
    Sources,
    Status,
)
from app.providers import services
from integrations import models as integration_models
from integrations.imports import helpers
from integrations.imports.helpers import (
    MediaImportError,
    decrypt,
    encrypt,
)

logger = logging.getLogger(__name__)

POCKETCASTS_API_BASE_URL = "https://api.pocketcasts.com"


def _cleanup_duplicate_episodes_global():
    """Clean up duplicate podcast episodes globally (standalone function).
    
    Finds duplicate episodes (same show, title, published date, different UUIDs)
    and merges them. Can be called from tasks that don't have a PocketCastsImporter instance.
    
    Returns:
        dict: Statistics about the cleanup (duplicates_removed, episodes_merged, items_merged)
    """
    from app.models import Item

    stats = {
        "duplicates_removed": 0,
        "episodes_merged": 0,
        "items_merged": 0,
    }

    # Find all duplicate episodes grouped by show, title (normalized), and published date (date portion only)
    # Use TruncDate to group by date portion, handling timezone differences
    # We'll need to normalize titles (lowercase, strip) in Python since Django doesn't have a strip function
    # For now, use Lower() for case-insensitive matching - we'll handle whitespace in the filter

    # First, get all episodes with their normalized data
    all_episodes_data = {}
    for episode in PodcastEpisode.objects.select_related("show").all():
        show_id = episode.show_id
        title_normalized = episode.title.lower().strip() if episode.title else ""
        published_date = episode.published.date() if episode.published else None

        key = (show_id, title_normalized, published_date)
        if key not in all_episodes_data:
            all_episodes_data[key] = []
        all_episodes_data[key].append(episode)

    # Find duplicate groups
    duplicate_groups = {k: v for k, v in all_episodes_data.items() if len(v) > 1}

    with transaction.atomic():
        for (show_id, title_normalized, published_date), episodes_list in duplicate_groups.items():
            # Sort episodes by id (higher id = more recent)
            episodes_list_sorted = sorted(episodes_list, key=lambda ep: ep.id)

            if len(episodes_list_sorted) <= 1:
                continue

            # Choose which episode to keep
            # Prefer Pocket Casts UUID format (36 chars with 4 hyphens)
            kept_episode = None
            for episode in episodes_list_sorted:
                is_pocketcasts_uuid = (
                    len(episode.episode_uuid) == 36 and
                    episode.episode_uuid.count("-") == 4
                )
                if is_pocketcasts_uuid:
                    kept_episode = episode
                    break

            # If no Pocket Casts UUID found, use most recent (last in sorted list)
            if not kept_episode:
                kept_episode = episodes_list_sorted[-1]

            duplicate_episodes = [ep for ep in episodes_list_sorted if ep.id != kept_episode.id]

            # Merge each duplicate episode
            for dup_episode in duplicate_episodes:
                try:
                    # Find the Item for the duplicate episode
                    dup_item = Item.objects.filter(
                        media_id=dup_episode.episode_uuid,
                        source=Sources.POCKETCASTS.value,
                        media_type=MediaTypes.PODCAST.value,
                    ).first()

                    # Find the Item for the kept episode (create if it doesn't exist)
                    kept_item, _ = Item.objects.get_or_create(
                        media_id=kept_episode.episode_uuid,
                        source=Sources.POCKETCASTS.value,
                        media_type=MediaTypes.PODCAST.value,
                        defaults={
                            "title": kept_episode.title,
                            "image": dup_item.image if dup_item else "",
                        },
                    )

                    # Update all Podcast entries that reference the duplicate episode
                    podcasts_updated = Podcast.objects.filter(episode=dup_episode).update(episode=kept_episode)

                    # Update all Podcast entries that reference the duplicate item
                    items_updated = Podcast.objects.filter(item=dup_item).update(item=kept_item) if dup_item else 0

                    # Delete the duplicate item if it exists and is different from kept item
                    if dup_item and dup_item.id != kept_item.id:
                        dup_item.delete()
                        stats["items_merged"] += 1

                    # Delete the duplicate episode
                    dup_episode.delete()
                    stats["duplicates_removed"] += 1
                    stats["episodes_merged"] += 1

                    logger.info(
                        "Merged duplicate episode: kept %s (%s), removed %s (%s), updated %d podcasts, %d items",
                        kept_episode.episode_uuid,
                        kept_episode.title,
                        dup_episode.episode_uuid,
                        dup_episode.title,
                        podcasts_updated + items_updated,
                        items_updated if dup_item else 0,
                    )
                except Exception as e:
                    logger.error(
                        "Failed to merge duplicate episode %s: %s",
                        dup_episode.episode_uuid,
                        e,
                        exc_info=True,
                    )

    return stats


def importer(identifier, user, mode):
    """Import the user's podcast history from Pocket Casts."""
    pocketcasts_importer = PocketCastsImporter(user, mode)
    return pocketcasts_importer.import_data()


class PocketCastsImporter:
    """Class to handle importing user podcast data from Pocket Casts."""

    def __init__(self, user, mode):
        """Initialize the importer with user details and mode.

        Args:
            user: Django user object to import data for
            mode (str): Import mode ("new" or "overwrite")
        """
        self.user = user
        self.mode = mode
        self.warnings = []

        try:
            self.account = user.pocketcasts_account
            # Refresh from DB to get latest connection_broken status
            self.account.refresh_from_db()
        except integration_models.PocketCastsAccount.DoesNotExist:
            msg = "Pocket Casts account not connected"
            raise MediaImportError(msg)

        # We need either credentials (email/password), access token, or refresh token to proceed
        has_credentials = bool(self.account.email and self.account.password)
        has_access_token = bool(self.account.access_token and self.account.access_token.strip())
        has_refresh_token = bool(self.account.refresh_token)

        if not has_credentials and not has_access_token and not has_refresh_token:
            logger.error(
                "Pocket Casts account has no usable credentials "
                "(email_present=%s access_token_present=%s refresh_token_present=%s)",
                bool(self.account.email),
                has_access_token,
                has_refresh_token,
            )
            msg = "Pocket Casts account not connected"
            raise MediaImportError(msg)

        # If we have credentials but no access token, try to login immediately
        if not has_access_token and has_credentials:
            logger.info("No access token but credentials exist, attempting login for user %s", self.user.username)
            try:
                self._login_with_credentials()
                logger.info("Successfully logged in from credentials for user %s", self.user.username)
            except Exception as e:
                logger.error("Failed to login when access token was missing: %s", e)
                # Mark as broken but don't fail yet - let _ensure_valid_token handle it
                self.account.connection_broken = True
                self.account.save()
        # If we have a refresh token but no access token (and no credentials), try to refresh immediately
        elif not has_access_token and has_refresh_token:
            logger.info("No access token but refresh token exists, attempting refresh for user %s", self.user.username)
            try:
                self._refresh_token()
                logger.info("Successfully refreshed token from refresh token for user %s", self.user.username)
            except Exception as e:
                logger.error("Failed to refresh token when access token was missing: %s", e)
                # Mark as broken but don't fail yet - let _ensure_valid_token handle it
                self.account.connection_broken = True
                self.account.save()

        # Allow import even if connection_broken - we'll attempt refresh/login in _ensure_valid_token

        self.existing_media = helpers.get_existing_media(user)
        # Capture last sync so we can anchor inferred completion times to this window
        self.previous_sync_at = self.account.last_sync_at
        self.to_delete = defaultdict(lambda: defaultdict(set))
        self.bulk_media = defaultdict(list)
        self.debug_uuid = os.getenv("POCKETCASTS_DEBUG_UUID")
        self.infer_debug = os.getenv("POCKETCASTS_INFER_DEBUG", "").lower() in {"1", "true", "yes", "on"}
        self._sync_window_start = None
        self._sync_window_end = None
        self._existing_history_items = None
        
        # Track existing podcasts to calculate deltas
        # Use Sources.POCKETCASTS.value for consistency with lookup keys
        self.existing_podcasts = {}
        for podcast in (
            Podcast.objects.filter(user=user)
            .select_related("item", "episode", "show")
            .order_by("-created_at")
        ):
            if podcast.item.source != Sources.POCKETCASTS.value:
                continue
            key = (podcast.item.media_id, Sources.POCKETCASTS.value)
            if key not in self.existing_podcasts:
                self.existing_podcasts[key] = podcast
        
        # Track shows we've processed to sync episodes from RSS
        self.processed_shows = set()

        logger.info(
            "Initialized Pocket Casts importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Import user's Pocket Casts history."""
        # Refresh token if needed
        self._ensure_valid_token()

        # Fetch podcast list to get show metadata (descriptions, images)
        from integrations import pocketcasts_api
        access_token = self._get_access_token()
        podcast_list_data = pocketcasts_api.get_podcast_list(access_token)
        self.podcast_metadata = {
            podcast["uuid"]: podcast
            for podcast in podcast_list_data.get("podcasts", [])
        }

        # Fetch history (last 100 episodes only, no pagination)
        episodes = self._fetch_history()
        episodes = self._dedupe_history(episodes)

        sync_window_end = timezone.now()
        sync_window_start = self.account.last_sync_at or (sync_window_end - timedelta(hours=2))
        self._sync_window_start = sync_window_start
        self._sync_window_end = sync_window_end
        self._existing_history_items = self._get_history_items_in_range(sync_window_start, sync_window_end)

        if not episodes:
            logger.info("No episodes found for Pocket Casts user %s", self.user.username)
            return {}, ""

        # Check if this is first import
        is_first_import = not Podcast.objects.filter(user=self.user).exists()

        # Collect new completed podcasts for inference (if not first import)
        new_completed_podcasts = []  # List of (episode_data, duration_seconds, published_date)

        # First pass: process episodes and collect new completed ones
        for episode_data in episodes:
            episode_uuid = episode_data.get("uuid")
            # Check if this episode is new (not in existing_podcasts)
            is_new = (episode_uuid, Sources.POCKETCASTS.value) not in self.existing_podcasts

            # Process the episode (but don't set completion_date yet for new ones)
            self._process_episode(episode_data, defer_completion_date=not is_first_import and is_new)

            # If this is a new completed episode (not first import), collect it for inference
            if not is_first_import and is_new:
                playing_status = episode_data.get("playingStatus", 0)
                duration = episode_data.get("duration", 0)
                played_up_to = episode_data.get("playedUpTo", 0)
                published = None
                if episode_data.get("published"):
                    try:
                        published = datetime.fromisoformat(episode_data["published"].replace("Z", "+00:00"))
                        if published and timezone.is_naive(published):
                            published = timezone.make_aware(published)
                    except (ValueError, AttributeError):
                        pass

                # Check if completed using same logic as _calculate_progress_delta
                # (status 3 with significant progress, or played up to duration with 5 second tolerance)
                epsilon = 5
                # Only mark as completed if there's significant progress to avoid false positives
                significant_progress = duration > 0 and (played_up_to > 60 or played_up_to > duration * 0.1)
                is_completed = (
                    (playing_status == 3 and significant_progress) or
                    (duration > 0 and played_up_to >= duration - epsilon)
                )

                if is_completed and published:
                    new_completed_podcasts.append((episode_data, duration, published))

        # Second pass: infer completion dates for new completed podcasts
        if new_completed_podcasts and not is_first_import:
            # Get sync window
            sync_window_end = self._sync_window_end or timezone.now()
            sync_window_start = self._sync_window_start or self.account.last_sync_at or (sync_window_end - timedelta(hours=2))
            previous_sync_at = self.account.last_sync_at

            # Get existing history items in the window
            existing_history = self._existing_history_items or self._get_history_items_in_range(sync_window_start, sync_window_end)

            # Sort podcasts by published date for consistent sequencing
            new_completed_podcasts_sorted = sorted(new_completed_podcasts, key=lambda x: x[2])  # Sort by published_date

            # Track inferred podcast completions as blocked intervals for subsequent inferences
            # This prevents multiple podcasts from getting overlapping completion times
            inferred_podcasts = []  # List of (end_time, buffer_seconds)

            # Infer completion dates for each new podcast in order
            for episode_data, duration_seconds, published_date in new_completed_podcasts_sorted:
                episode_uuid = episode_data.get("uuid")

                # Infer completion date, passing previously inferred podcasts as blocked intervals
                debug_context = {
                    "played_up_to": episode_data.get("playedUpTo"),
                    "old_played_up_to": 0,
                    "old_status": None,
                    "new_status": Status.COMPLETED.value,
                    "duration_seconds": duration_seconds,
                }
                inferred_date = self._infer_completion_date(
                    duration_seconds,
                    sync_window_start,
                    sync_window_end,
                    existing_history,
                    [],  # other_new_podcasts no longer used for spacing
                    published_date,
                    episode_uuid,
                    previous_sync_at,
                    debug_context=debug_context,
                    inferred_podcasts=inferred_podcasts,
                )

                # Track this completion as a blocked interval for subsequent podcasts
                # Use a buffer proportional to podcast duration (min 2 min, max 5 min)
                buffer_seconds = max(120, min(duration_seconds // 10, 300))
                inferred_podcasts.append((inferred_date, buffer_seconds))

                # Update the podcast's completion_date in bulk_media
                # Find the podcast in bulk_media and update it
                for podcast in self.bulk_media.get(MediaTypes.PODCAST.value, []):
                    if podcast.item.media_id == episode_uuid:
                        podcast.end_date = inferred_date
                        logger.debug(
                            "Inferred completion_date for episode %s: %s (published: %s, duration: %d seconds)",
                            episode_data.get("title", "Unknown"),
                            inferred_date,
                            published_date,
                            duration_seconds,
                        )
                        # Update pending history timestamp to inferred date for this episode
                        if hasattr(self, "_pending_history"):
                            updated_history = []
                            for ep_uuid, delta_seconds, history_timestamp in self._pending_history:
                                if ep_uuid == episode_uuid:
                                    updated_history.append((ep_uuid, delta_seconds, inferred_date))
                                else:
                                    updated_history.append((ep_uuid, delta_seconds, history_timestamp))
                            self._pending_history = updated_history
                        break

        # Cleanup and bulk create
        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)

        # Record history for newly created podcasts
        if hasattr(self, "_pending_history"):
            for episode_uuid, delta_seconds, history_timestamp in self._pending_history:
                # Reload podcast from DB after bulk create
                try:
                    podcast = Podcast.objects.get(
                        user=self.user,
                        item__media_id=episode_uuid,
                        item__source=Sources.POCKETCASTS.value,
                    )
                    self._record_history(podcast, delta_seconds, history_timestamp)
                except Podcast.DoesNotExist:
                    logger.warning("Could not find podcast after bulk create for history recording: %s", episode_uuid)

        # Sync episodes from RSS feeds for processed shows
        for show in self.processed_shows:
            if show.rss_feed_url:
                try:
                    self._sync_episodes_from_rss(show, show.rss_feed_url)
                except Exception as e:
                    logger.warning("Failed to sync episodes from RSS for show %s: %s", show.title, e)
                    self.warnings.append(f"Failed to sync episodes for {show.title}: {e!s}")

        # Update last sync time
        self.account.last_sync_at = timezone.now()
        self.account.save(update_fields=["last_sync_at"])

        # Clean up duplicate episodes
        cleanup_stats = self._cleanup_duplicate_episodes()
        if cleanup_stats.get("duplicates_removed", 0) > 0:
            logger.info(
                "Cleaned up %d duplicate podcast episodes for user %s",
                cleanup_stats["duplicates_removed"],
                self.user.username,
            )

        imported_counts = {
            media_type: len(media_list)
            for media_type, media_list in self.bulk_media.items()
        }

        logger.info(
            "Pocket Casts import completed for user %s: %s",
            self.user.username,
            imported_counts,
        )

        # Trigger cache refresh if any podcasts were imported
        # (bulk_create doesn't trigger signals, so we need to manually refresh)
        if MediaTypes.PODCAST.value in imported_counts and imported_counts[MediaTypes.PODCAST.value] > 0:
            from app import statistics_cache
            from app.history_cache import schedule_history_refresh

            logger.debug("Triggering cache refresh for user %s after podcast import", self.user.username)
            schedule_history_refresh(self.user.id)
            statistics_cache.schedule_all_ranges_refresh(self.user.id)

        return imported_counts, "\n".join(self.warnings) if self.warnings else ""

    def _disconnect_account(self, reason="Refresh token failed", clear_credentials=False):
        """Mark the Pocket Casts account as disconnected.
        
        Args:
            reason: Reason for disconnection (for logging)
            clear_credentials: If True, clear all tokens. If False, preserve tokens but mark as broken.
        """
        logger.warning("Marking Pocket Casts account as disconnected for user %s: %s", self.user.username, reason)

        if clear_credentials:
            # Clear all tokens (full disconnect)
            self.account.access_token = ""
            self.account.refresh_token = None
            self.account.token_expires_at = None
            logger.info("Cleared all credentials for user %s", self.user.username)
        else:
            # Just mark as broken, preserve credentials for later refresh
            self.account.connection_broken = True
            logger.info("Marked connection as broken (credentials preserved) for user %s", self.user.username)

        self.account.save()

        # Delete periodic import task
        from django_celery_beat.models import PeriodicTask
        PeriodicTask.objects.filter(
            task="Import from Pocket Casts (Recurring)",
            kwargs__contains=f'"user_id": {self.user.id}',
        ).delete()
        logger.info("Removed scheduled imports for user %s", self.user.username)

    def _ensure_valid_token(self):
        """Ensure we have a valid access token.
        
        Prefers login with credentials over refresh token when credentials are available,
        as login is more reliable than refresh tokens which may expire or be revoked.
        """
        has_credentials = bool(self.account.email and self.account.password)

        # If no access token, try to get one
        if not self.account.access_token:
            if has_credentials:
                # Prefer login when credentials are available
                logger.info("No access token available, attempting login with credentials for user %s", self.user.username)
                try:
                    self._login_with_credentials()
                    logger.info("Successfully logged in for user %s", self.user.username)
                    return
                except Exception as e:
                    logger.error("Failed to login when access token was missing: %s", e)
                    # If login fails, try refresh token as fallback (legacy accounts)
                    if self.account.refresh_token:
                        logger.info("Login failed, attempting refresh token fallback for user %s", self.user.username)
                        try:
                            self._refresh_token()
                            logger.info("Successfully refreshed token for user %s", self.user.username)
                            return
                        except Exception:
                            pass  # Will raise below
                    msg = "No access token available and authentication failed"
                    raise MediaImportError(msg) from e
            elif self.account.refresh_token:
                # Legacy: only refresh token available
                logger.info("No access token available, attempting refresh for user %s", self.user.username)
                try:
                    self._refresh_token()
                    logger.info("Successfully refreshed token for user %s", self.user.username)
                    return
                except Exception as e:
                    logger.error("Failed to refresh token when access token was missing: %s", e)
                    msg = "No access token available and refresh failed"
                    raise MediaImportError(msg) from e
            else:
                msg = "No access token available and no credentials or refresh token"
                raise MediaImportError(msg)

        # Check if token is expired
        if self.account.is_token_expired:
            if has_credentials:
                # Prefer login when credentials are available (more reliable)
                logger.info("Pocket Casts token is expired for user %s. Attempting login with credentials.", self.user.username)
                try:
                    self._login_with_credentials()
                    logger.info("Successfully logged in to refresh expired token for user %s", self.user.username)
                    return
                except Exception as login_error:
                    logger.warning("Login failed for expired token, trying refresh token fallback: %s", login_error)
                    # Fall back to refresh token if login fails
                    if self.account.refresh_token:
                        try:
                            self._refresh_token()
                            logger.info("Successfully refreshed expired token for user %s", self.user.username)
                            return
                        except Exception:
                            pass  # Will raise below
                    # Both login and refresh failed
                    raise MediaImportError("Token expired and both login and refresh failed") from login_error
            elif self.account.refresh_token:
                # Legacy: only refresh token available
                logger.info("Pocket Casts token is expired for user %s. Attempting to refresh.", self.user.username)
                try:
                    self._refresh_token()
                    logger.info("Successfully refreshed expired token for user %s", self.user.username)
                    return
                except requests.HTTPError as e:
                    # If refresh fails with 401, _refresh_token will handle fallback to login if credentials exist
                    # For legacy accounts without credentials, disconnect
                    if e.response and e.response.status_code == requests.codes.unauthorized:
                        if not has_credentials:
                            self._disconnect_account("Refresh token is invalid or expired")
                            msg = "Refresh token is invalid. Please reconnect your Pocket Casts account."
                            raise MediaImportError(msg) from e
                    # For other HTTP errors, log and try to continue
                    logger.warning("Failed to refresh expired token for user %s: %s", self.user.username, e)
                except Exception as e:
                    # For non-HTTP errors, log but try to continue
                    logger.warning("Failed to refresh expired token for user %s: %s", self.user.username, e)
            else:
                logger.warning("Pocket Casts token is expired for user %s and no refresh token or credentials available. User may need to reconnect.", self.user.username)
                # Try to use it anyway - it might still work or the expiration might be wrong

    def _login_with_credentials(self):
        """Login to Pocket Casts using stored email and password credentials.
        
        This method decrypts the stored credentials, calls the login API,
        and stores the resulting tokens.
        
        Raises:
            MediaImportError: If credentials are missing, decryption fails, or login fails
        """
        if not self.account.email or not self.account.password:
            msg = "No credentials available for login"
            raise MediaImportError(msg)

        try:
            decrypted_email = decrypt(self.account.email)
            decrypted_password = decrypt(self.account.password)
        except Exception as e:
            logger.error("Failed to decrypt credentials for user %s: %s", self.user.username, e)
            msg = "Failed to decrypt stored credentials"
            raise MediaImportError(msg) from e

        # Call login API
        from integrations import pocketcasts_api
        try:
            logger.info("Attempting to login with credentials for user %s", self.user.username)
            login_response = pocketcasts_api.login(decrypted_email, decrypted_password)

            access_token = login_response["accessToken"]
            refresh_token = login_response.get("refreshToken", "")

            # Encrypt and store new tokens
            self.account.access_token = encrypt(access_token)
            if refresh_token:
                self.account.refresh_token = encrypt(refresh_token)

            # Parse expiration from JWT
            try:
                decoded = jwt.decode(access_token, options={"verify_signature": False})
                exp = decoded.get("exp")
                if exp:
                    self.account.token_expires_at = datetime.fromtimestamp(exp, tz=UTC)
            except Exception:
                # If we can't parse, set expiration to 1 hour from now as fallback
                self.account.token_expires_at = timezone.now() + timedelta(hours=1)

            # Clear connection_broken flag on successful login
            self.account.connection_broken = False
            self.account.save()
            logger.info("Successfully logged in to Pocket Casts for user %s", self.user.username)

        except pocketcasts_api.PocketCastsAuthError as e:
            logger.error("Pocket Casts login failed for user %s: %s", self.user.username, e)
            # Mark as broken but preserve credentials (user might fix password)
            self.account.connection_broken = True
            self.account.save()
            msg = "Invalid email or password. Please update your credentials in settings."
            raise MediaImportError(msg) from e
        except Exception as e:
            logger.error("Failed to login to Pocket Casts for user %s: %s", self.user.username, e)
            msg = f"Failed to login to Pocket Casts: {e}"
            raise MediaImportError(msg) from e

    def _get_history_items_in_range(self, start_time, end_time):
        """Get all history items with end_date in the specified time range.
        
        Args:
            start_time: Start of time range (datetime)
            end_time: End of time range (datetime)
            
        Returns:
            List of tuples: (end_date, duration_seconds, media_type, is_scrobbled)
            - end_date: When the item was completed
            - duration_seconds: Duration of the item in seconds (None if unknown)
            - media_type: Type of media ('music', 'podcast', 'episode', 'movie')
            - is_scrobbled: True if item has precise timestamp (Music/Episode), False otherwise
            Sorted by end_date ascending
        """
        history_items = []

        # Music - scrobbled items with precise timestamps
        music_items = Music.objects.filter(
            user=self.user,
            end_date__isnull=False,
            end_date__gte=start_time,
            end_date__lte=end_time,
        ).select_related("item", "track")

        for music in music_items:
            # Get duration from track or item runtime
            duration_seconds = None
            if music.track and music.track.duration_ms:
                duration_seconds = music.track.duration_ms // 1000
            elif music.item and music.item.runtime_minutes:
                duration_seconds = music.item.runtime_minutes * 60

            history_items.append((music.end_date, duration_seconds, "music", True))

        # Podcasts - already imported podcasts
        podcast_items = Podcast.objects.filter(
            user=self.user,
            end_date__isnull=False,
            end_date__gte=start_time,
            end_date__lte=end_time,
        ).select_related("item", "episode")

        for podcast in podcast_items:
            duration_seconds = None
            if podcast.episode and podcast.episode.duration:
                duration_seconds = podcast.episode.duration
            elif podcast.item and podcast.item.runtime_minutes:
                duration_seconds = podcast.item.runtime_minutes * 60

            history_items.append((podcast.end_date, duration_seconds, "podcast", False))

        # Episodes (TV) - scrobbled items with precise timestamps
        episode_items = Episode.objects.filter(
            related_season__user=self.user,
            end_date__isnull=False,
            end_date__gte=start_time,
            end_date__lte=end_time,
        ).select_related("item")

        for episode in episode_items:
            duration_seconds = None
            if episode.item and episode.item.runtime_minutes:
                duration_seconds = episode.item.runtime_minutes * 60

            history_items.append((episode.end_date, duration_seconds, "episode", True))

        # Movies
        movie_items = Movie.objects.filter(
            user=self.user,
            end_date__isnull=False,
            end_date__gte=start_time,
            end_date__lte=end_time,
        ).select_related("item")

        for movie in movie_items:
            duration_seconds = None
            if movie.item and movie.item.runtime_minutes:
                duration_seconds = movie.item.runtime_minutes * 60

            history_items.append((movie.end_date, duration_seconds, "movie", False))

        # Sort by end_date ascending
        history_items.sort(key=lambda x: x[0])

        return history_items

    def _get_inference_window(self):
        sync_window_end = self._sync_window_end or timezone.now()
        sync_window_start = self._sync_window_start or self.previous_sync_at or (sync_window_end - timedelta(hours=2))
        existing_history = self._existing_history_items
        if existing_history is None:
            existing_history = self._get_history_items_in_range(sync_window_start, sync_window_end)
        return sync_window_start, sync_window_end, existing_history

    def _stable_hash_int(self, value):
        value_str = "" if value is None else str(value)
        digest = hashlib.sha256(value_str.encode("utf-8")).hexdigest()
        return int(digest[:8], 16)

    def _merge_intervals(self, intervals):
        if not intervals:
            return []
        intervals.sort(key=lambda x: x[0])
        merged = []
        for start, end in intervals:
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            elif end > merged[-1][1]:
                merged[-1][1] = end
        return [(start, end) for start, end in merged]

    def _build_blocked_intervals(self, existing_history_items, sync_window_start, sync_window_end, inferred_podcasts=None):
        """Build blocked intervals from scrobbled items and previously inferred podcast completions.
        
        Args:
            existing_history_items: List of (end_date, duration, type, is_scrobbled)
            sync_window_start: Start of the sync window
            sync_window_end: End of the sync window
            inferred_podcasts: Optional list of (end_time, buffer_seconds) tuples
        """
        intervals = []
        
        # Add scrobbled items (music, etc.) as blocked intervals
        for end_date, duration, _, is_scrobbled in existing_history_items:
            if not is_scrobbled or not end_date:
                continue
            duration_seconds = max(0, duration or 0)
            if duration_seconds > 0:
                start = end_date - timedelta(seconds=duration_seconds)
            else:
                start = end_date
            if end_date < sync_window_start or start > sync_window_end:
                continue
            start = max(start, sync_window_start)
            end = min(end_date, sync_window_end)
            intervals.append((start, end))
        
        # Add previously inferred podcast completions as blocked intervals
        # This prevents multiple podcasts from overlapping
        if inferred_podcasts:
            for end_time, buffer_seconds in inferred_podcasts:
                if not end_time:
                    continue
                # Create a small blocked interval around the inferred completion time
                buffer = max(60, min(buffer_seconds, 300))  # 1-5 minute buffer
                start = end_time - timedelta(seconds=buffer)
                end = end_time + timedelta(seconds=60)  # Small buffer after
                # Clamp to window
                if end < sync_window_start or start > sync_window_end:
                    continue
                start = max(start, sync_window_start)
                end = min(end, sync_window_end)
                intervals.append((start, end))
        
        return self._merge_intervals(intervals)

    def _has_gap_of_length(self, duration_seconds, sync_window_start, sync_window_end, blocked_intervals):
        """Check if there's a gap large enough for the placement buffer.
        
        Uses the same capped duration as _fit_completion_after_blocked for consistency.
        """
        # Use capped duration for gap checking (same as fitting logic)
        check_duration = min(duration_seconds, 300)  # Max 5 minute buffer
        
        if check_duration <= 0:
            return True
        cursor = sync_window_start
        for start, end in blocked_intervals:
            if (start - cursor).total_seconds() >= check_duration:
                return True
            if end > cursor:
                cursor = end
        return (sync_window_end - cursor).total_seconds() >= check_duration

    def _compute_gaps(self, sync_window_start, sync_window_end, blocked_intervals):
        gaps = []
        cursor = sync_window_start
        for start, end in blocked_intervals:
            if start > cursor:
                gaps.append((cursor, start))
            if end > cursor:
                cursor = end
        if cursor < sync_window_end:
            gaps.append((cursor, sync_window_end))
        return gaps

    def _fit_completion_after_blocked(self, base_completion_time, duration_seconds, sync_window_start, sync_window_end, blocked_intervals):
        """Find a valid completion time that doesn't overlap with blocked intervals.
        
        We're placing an END time, not reserving the full playback span. The duration_seconds
        is used to determine a "virtual start" for overlap checking, but the key is finding
        a completion time within the window that doesn't conflict with blocked intervals.
        
        For non-anchored cases, duration_seconds is already capped to a small value (5 min),
        making this more flexible. For anchored cases, it's the remaining time.
        """
        max_end = sync_window_end
        
        # For placement purposes, use a reasonable buffer (not full duration for long podcasts)
        # This prevents "can't fit 4 hours in 2 hour window" from triggering fallback
        placement_buffer = min(duration_seconds, 300)  # Max 5 minute buffer for fitting
        
        min_end = sync_window_start + timedelta(seconds=placement_buffer)
        if min_end > max_end:
            # Even the minimum buffer doesn't fit - just place at window end
            min_end = sync_window_start + timedelta(seconds=60)  # 1 minute minimum
            if min_end > max_end:
                return sync_window_end

        candidate_end = min(max(base_completion_time, min_end), max_end)
        candidate_start = candidate_end - timedelta(seconds=placement_buffer)

        for start, end in blocked_intervals:
            if end <= candidate_start:
                continue
            if start >= candidate_end:
                break
            # Conflict: push candidate to after this blocked interval
            candidate_start = end
            candidate_end = candidate_start + timedelta(seconds=placement_buffer)
            if candidate_end > max_end:
                # Can't fit after this blocked interval, but we might find a gap earlier
                # Return None to trigger fallback which has better gap-finding logic
                return None

        return candidate_end

    def _fallback_completion_time(self, episode_uuid, sync_window_start, sync_window_end, blocked_intervals):
        """Place completion time in an available gap using hash-based distribution.
        
        Applies boundary avoidance to prevent landing exactly on gap start/end,
        which would cause clustering on sync window boundaries.
        """
        window_seconds = int((sync_window_end - sync_window_start).total_seconds())
        if window_seconds <= 0:
            return sync_window_end

        gaps = self._compute_gaps(sync_window_start, sync_window_end, blocked_intervals)
        hash_int = self._stable_hash_int(episode_uuid)
        
        if gaps:
            # Select a gap using hash (prefer larger gaps by weighting)
            # Sort gaps by size descending for better distribution
            sorted_gaps = sorted(gaps, key=lambda g: (g[1] - g[0]).total_seconds(), reverse=True)
            gap_start, gap_end = sorted_gaps[hash_int % len(sorted_gaps)]
            gap_seconds = int((gap_end - gap_start).total_seconds())
            
            if gap_seconds <= 0:
                return gap_start
            
            # Boundary avoidance: don't land within 60s of gap boundaries
            # This prevents stacking at sync_window_start or sync_window_end
            buffer = min(60, gap_seconds // 4)  # At most 25% of gap, max 60 seconds
            
            if gap_seconds > buffer * 2:
                # Enough room for buffers on both ends
                effective_gap = gap_seconds - (buffer * 2)
                offset_seconds = buffer + (hash_int % effective_gap)
            else:
                # Small gap: just use middle or hash distribution
                offset_seconds = hash_int % gap_seconds
            
            return gap_start + timedelta(seconds=offset_seconds)

        # No gaps: distribute across entire window with boundary avoidance
        buffer = min(60, window_seconds // 4)
        if window_seconds > buffer * 2:
            effective_window = window_seconds - (buffer * 2)
            offset_seconds = buffer + (hash_int % effective_window)
        else:
            offset_seconds = hash_int % max(1, window_seconds)
        
        return sync_window_start + timedelta(seconds=offset_seconds)

    def _log_completion_inference(
        self,
        episode_uuid,
        debug_context,
        previous_sync_at,
        sync_window_start,
        sync_window_end,
        last_in_progress_date,
        last_progress_minutes,
        base_completion_time,
        final_completion_time,
        fallback_reason,
        anchor_used,
    ):
        if not self.infer_debug:
            return
        logger.debug(
            "Pocket Casts completion inference: episode=%s duration=%s played_up_to=%s old_played_up_to=%s "
            "old_status=%s new_status=%s previous_sync_at=%s sync_window_start=%s sync_window_end=%s "
            "last_in_progress_date=%s last_progress_minutes=%s base_completion=%s final_completion=%s "
            "anchor_used=%s fallback_reason=%s",
            episode_uuid,
            debug_context.get("duration_seconds"),
            debug_context.get("played_up_to"),
            debug_context.get("old_played_up_to"),
            debug_context.get("old_status"),
            debug_context.get("new_status"),
            previous_sync_at,
            sync_window_start,
            sync_window_end,
            last_in_progress_date,
            last_progress_minutes,
            base_completion_time,
            final_completion_time,
            anchor_used,
            fallback_reason,
        )

    def _get_last_in_progress_record(self, episode_uuid):
        """Get the last in-progress history record for an episode.
        
        Args:
            episode_uuid: The episode UUID to search for
            
        Returns:
            tuple: (history_date, progress_minutes) or (None, None) if not found
        """
        from django.apps import apps
        HistoricalPodcast = apps.get_model("app", "HistoricalPodcast")

        podcast_ids = list(
            Podcast.objects.filter(
                user=self.user,
                item__media_id=episode_uuid,
                item__source=Sources.POCKETCASTS.value,
            ).values_list("id", flat=True)
        )
        if not podcast_ids:
            return None, None

        # Find the most recent history record where end_date is None and status is IN_PROGRESS
        last_record = (
            HistoricalPodcast.objects.filter(
                id__in=podcast_ids,
                end_date__isnull=True,
                status=Status.IN_PROGRESS.value,
            )
            .order_by("-history_date")
            .first()
        )

        if last_record:
            return last_record.history_date, last_record.progress

        return None, None

    def _infer_completion_date(
        self,
        podcast_duration_seconds,
        sync_window_start,
        sync_window_end,
        existing_history_items,
        other_new_podcasts,
        this_podcast_published,
        episode_uuid,
        previous_sync_at,
        debug_context=None,
        inferred_podcasts=None,
    ):
        """Infer completion date for a podcast by fitting it into timeline gaps.
        
        For anchored cases (prior in-progress snapshot exists): uses remaining time
        to calculate when the episode likely completed.
        
        For non-anchored cases (98% of completions): uses hash-based distribution
        across the window since we have no information about when listening started.
        
        Args:
            inferred_podcasts: List of (end_time, buffer_seconds) for already-inferred
                podcasts in this sync, used to prevent overlapping placements.
        """
        debug_context = debug_context or {}
        duration_seconds = max(0, podcast_duration_seconds or 0)
        debug_context.setdefault("duration_seconds", duration_seconds)
        inferred_podcasts = inferred_podcasts or []

        last_in_progress_date, last_progress_minutes = self._get_last_in_progress_record(episode_uuid)
        window_seconds = int((sync_window_end - sync_window_start).total_seconds())
        if window_seconds <= 0:
            completion_time = sync_window_end
            self._log_completion_inference(
                episode_uuid,
                debug_context,
                previous_sync_at,
                sync_window_start,
                sync_window_end,
                last_in_progress_date,
                last_progress_minutes,
                None,
                completion_time,
                "invalid_window",
                False,
            )
            return completion_time

        anchor_used = bool(last_in_progress_date and last_progress_minutes is not None)
        base_completion_time = None
        placement_duration_seconds = duration_seconds

        if anchor_used:
            # Anchored path: use remaining time (e.g., 4-hour podcast with 20 min left)
            # This correctly places the completion near anchor_time + remaining_time
            progress_seconds = last_progress_minutes * 60
            remaining_seconds = max(0, duration_seconds - progress_seconds)
            anchor_time = previous_sync_at or sync_window_start
            if last_in_progress_date >= anchor_time:
                anchor_time = last_in_progress_date
            base_completion_time = anchor_time + timedelta(seconds=remaining_seconds)
            placement_duration_seconds = remaining_seconds
        else:
            # Non-anchored path: we have NO information about when listening started.
            # A completed 4-hour podcast in a 2-hour window could have ended at any time.
            # Use hash-based distribution across the entire window for fair placement.
            hash_offset = self._stable_hash_int(episode_uuid) % window_seconds
            base_completion_time = sync_window_start + timedelta(seconds=hash_offset)
            # Use a small placement duration for gap-fitting (just needs to "fit" the end time)
            placement_duration_seconds = min(300, duration_seconds)  # 5 min max for fitting

        # Build blocked intervals including previously inferred podcast completions
        blocked_intervals = self._build_blocked_intervals(
            existing_history_items, sync_window_start, sync_window_end, inferred_podcasts
        )
        fallback_reason = None
        completion_time = None

        if not self._has_gap_of_length(placement_duration_seconds, sync_window_start, sync_window_end, blocked_intervals):
            fallback_reason = "no_gap_for_duration"
        else:
            completion_time = self._fit_completion_after_blocked(
                base_completion_time,
                placement_duration_seconds,
                sync_window_start,
                sync_window_end,
                blocked_intervals,
            )
            if completion_time is None:
                fallback_reason = "no_gap_after_base"

        if completion_time is None:
            completion_time = self._fallback_completion_time(
                episode_uuid,
                sync_window_start,
                sync_window_end,
                blocked_intervals,
            )

        self._log_completion_inference(
            episode_uuid,
            debug_context,
            previous_sync_at,
            sync_window_start,
            sync_window_end,
            last_in_progress_date,
            last_progress_minutes,
            base_completion_time,
            completion_time,
            fallback_reason,
            anchor_used,
        )

        return completion_time

    def _refresh_token(self):
        """Refresh the access token using the refresh token."""
        url = f"{POCKETCASTS_API_BASE_URL}/user/refresh"

        try:
            decrypted_refresh_token = decrypt(self.account.refresh_token)
        except Exception as e:
            logger.error("Failed to decrypt refresh token: %s", e)
            # If we can't decrypt, the token is corrupted - disconnect
            self._disconnect_account("Refresh token decryption failed - token may be corrupted")
            msg = "Invalid refresh token"
            raise MediaImportError(msg) from e

        payload = {"refreshToken": decrypted_refresh_token}
        headers = {
            "Content-Type": "application/json",
            "Accept": "*/*",
        }

        try:
            response = services.api_request("POCKETCASTS", "POST", url, params=payload, headers=headers)

            if "accessToken" not in response:
                msg = "Invalid response from token refresh"
                raise MediaImportError(msg)

            # Encrypt and store new tokens
            self.account.access_token = encrypt(response["accessToken"])
            if "refreshToken" in response:
                self.account.refresh_token = encrypt(response["refreshToken"])

            # Parse expiration from JWT
            try:
                decoded = jwt.decode(response["accessToken"], options={"verify_signature": False})
                exp = decoded.get("exp")
                if exp:
                    self.account.token_expires_at = datetime.fromtimestamp(exp, tz=UTC)
            except Exception:
                # If we can't parse, set expiration to 1 hour from now as fallback
                self.account.token_expires_at = timezone.now() + timedelta(hours=1)

            # Clear connection_broken flag on successful refresh
            self.account.connection_broken = False
            self.account.save()
            logger.info("Successfully refreshed Pocket Casts token for user %s", self.user.username)

        except requests.HTTPError as e:
            if e.response.status_code == requests.codes.unauthorized:
                # Refresh token is invalid - try falling back to login if we have credentials
                has_credentials = bool(self.account.email and self.account.password)
                if has_credentials:
                    logger.warning("Refresh token returned 401, falling back to login with credentials for user %s", self.user.username)
                    try:
                        self._login_with_credentials()
                        logger.info("Successfully recovered from refresh failure using login for user %s", self.user.username)
                        return  # Successfully logged in, tokens are now stored
                    except MediaImportError:
                        # Login also failed - mark as broken but preserve credentials
                        logger.error("Both refresh and login failed for user %s", self.user.username)
                        self.account.connection_broken = True
                        self.account.save()
                        msg = "Token refresh failed and login with stored credentials also failed. Please update your credentials."
                        raise MediaImportError(msg) from e
                else:
                    # No credentials available - disconnect the account (legacy behavior)
                    self._disconnect_account("Refresh token returned 401 unauthorized")
                    msg = "Invalid refresh token. Please reconnect your Pocket Casts account."
                    raise MediaImportError(msg) from e
            msg = f"Token refresh failed: {e.response.status_code}"
            raise MediaImportError(msg) from e

    def _get_access_token(self):
        """Get decrypted access token."""
        try:
            return decrypt(self.account.access_token)
        except Exception as e:
            logger.error("Failed to decrypt access token: %s", e)
            msg = "Invalid access token"
            raise MediaImportError(msg) from e

    def _fetch_history(self):
        """Fetch history from API (returns last 100 episodes only)."""
        url = f"{POCKETCASTS_API_BASE_URL}/user/history"
        access_token = self._get_access_token()

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "X-App-Language": "en",
            "X-User-Region": "global",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
        }

        payload = {}

        try:
            response = services.api_request("POCKETCASTS", "POST", url, params=payload, headers=headers)

            if "episodes" not in response:
                logger.warning("No episodes in Pocket Casts response for user %s", self.user.username)
                return []

            episodes = response["episodes"]
            logger.info(
                "Found %d episodes for Pocket Casts user %s",
                len(episodes),
                self.user.username,
            )
            return episodes

        except requests.HTTPError as e:
            if e.response.status_code == requests.codes.unauthorized:
                # Try refreshing token if we have a refresh token
                if self.account.refresh_token:
                    logger.info("Unauthorized, attempting token refresh")
                    try:
                        self._refresh_token()
                        access_token = self._get_access_token()
                        headers["Authorization"] = f"Bearer {access_token}"

                        try:
                            response = services.api_request("POCKETCASTS", "POST", url, params=payload, headers=headers)
                            if "episodes" in response:
                                return response["episodes"]
                        except requests.HTTPError:
                            # If retry still fails, disconnect
                            self._disconnect_account("Token refresh succeeded but API calls still return 401")
                            msg = "Authentication failed after token refresh. Please reconnect your Pocket Casts account."
                            raise MediaImportError(msg) from e
                    except requests.HTTPError as refresh_error:
                        # Refresh failed with HTTP error - if it's 401, disconnect
                        if refresh_error.response and refresh_error.response.status_code == requests.codes.unauthorized:
                            self._disconnect_account("Refresh token returned 401 during API call")
                        msg = "Authentication failed. Your token may have expired. Please reconnect your Pocket Casts account."
                        raise MediaImportError(msg) from refresh_error
                    except Exception as refresh_error:
                        # Other refresh errors - log but don't disconnect (might be temporary)
                        logger.error("Token refresh failed during API call: %s", refresh_error)
                        msg = "Authentication failed. Your token may have expired. Please reconnect your Pocket Casts account."
                        raise MediaImportError(msg) from e
                else:
                    # No refresh token and got 401 - disconnect
                    self._disconnect_account("Access token invalid and no refresh token available")
                    msg = "Authentication failed. Your token may have expired. Please reconnect your Pocket Casts account."
                    raise MediaImportError(msg) from e
            msg = f"Pocket Casts API error: {e.response.status_code}"
            raise MediaImportError(msg) from e

    def _process_episode(self, episode_data, defer_completion_date=False):
        """Process single episode: create/update show/episode, calculate delta.
        
        Args:
            episode_data: Episode data from API
            defer_completion_date: If True, don't set completion_date (will be inferred later)
        """
        try:
            episode_uuid = episode_data.get("uuid")
            podcast_uuid = episode_data.get("podcastUuid")

            if not episode_uuid or not podcast_uuid:
                logger.warning("Skipping episode with missing UUIDs: %s", episode_data)
                return
            incoming_uuid = episode_uuid
            if self.debug_uuid and episode_uuid == self.debug_uuid:
                logger.info(
                    "Processing Pocket Casts history entry %s: title=%s published=%s playingStatus=%s playedUpTo=%s",
                    episode_uuid,
                    episode_data.get("title", "Unknown Episode"),
                    episode_data.get("published"),
                    episode_data.get("playingStatus"),
                    episode_data.get("playedUpTo"),
                )

            # Note: We import deleted episodes too - they're still in history
            # We'll mark them as deleted in the database but still track them
            is_deleted = episode_data.get("isDeleted", False)

            # Get show metadata from podcast list if available
            show_metadata = getattr(self, "podcast_metadata", {}).get(podcast_uuid, {})

            # Get show title and author for artwork fetching
            show_title = episode_data.get("podcastTitle", show_metadata.get("title", "Unknown Show"))
            show_author = episode_data.get("author", show_metadata.get("author", ""))

            # Construct Pocket Casts image URL (requires auth, so we'll try to replace it)
            from integrations import pocketcasts_api
            pocketcasts_image_url = pocketcasts_api.get_podcast_image_url(podcast_uuid, size=130)

            # Ensure show exists first
            show, created = PodcastShow.objects.get_or_create(
                podcast_uuid=podcast_uuid,
                defaults={
                    "title": show_title,
                    "slug": episode_data.get("podcastSlug", show_metadata.get("slug", "")),
                    "author": show_author,
                    "image": pocketcasts_image_url,  # Temporary, will try to replace
                    "description": show_metadata.get("description", "") or show_metadata.get("descriptionHtml", ""),
                },
            )

            # Update show fields if we have new data
            updated = False
            if episode_data.get("podcastTitle") and show.title != episode_data["podcastTitle"]:
                show.title = episode_data["podcastTitle"]
                updated = True
            if episode_data.get("author") and show.author != episode_data["author"]:
                show.author = episode_data["author"]
                updated = True
            # Update from podcast list metadata if available
            if show_metadata:
                # Update description if we have one and show doesn't
                description = show_metadata.get("description") or show_metadata.get("descriptionHtml", "")
                if description and (not show.description or show.description != description):
                    show.description = description
                    updated = True

            # Always try to discover RSS feed URL if we don't have one
            # Check for RSS feed URL in metadata first
            # The podcast list might have 'url' (website) but not explicit RSS feed
            # We'll check common field names
            rss_feed_url = (
                show_metadata.get("rssUrl")
                or show_metadata.get("rss_url")
                or show_metadata.get("feedUrl")
                or show_metadata.get("feed_url")
            )

            # If no RSS feed URL found in metadata and show doesn't have one, try to discover it from iTunes
            itunes_artwork = None
            if not rss_feed_url and not show.rss_feed_url:
                from integrations import pocketcasts_artwork
                logger.debug("Attempting to discover RSS feed URL from iTunes for %s", show.title)
                itunes_artwork, itunes_feed_url = pocketcasts_artwork.fetch_podcast_artwork_and_rss(
                    show_title=show.title,
                    author=show.author,
                )
                if itunes_feed_url:
                    rss_feed_url = itunes_feed_url
                    logger.info(
                        "Discovered RSS feed URL from iTunes for %s: %s",
                        show.title,
                        safe_url(rss_feed_url),
                    )
                else:
                    logger.debug("No RSS feed URL found in iTunes results for %s", show.title)

            # Store RSS feed URL if we found one and show doesn't have it
            if rss_feed_url and not show.rss_feed_url:
                show.rss_feed_url = rss_feed_url
                updated = True

            # Fetch artwork from alternative sources if needed
            # Only fetch if image is empty or is a Pocket Casts authenticated URL
            should_fetch_artwork = (
                not show.image
                or show.image == ""
                or show.image.startswith(POCKETCASTS_API_BASE_URL)
            )

            if should_fetch_artwork:
                from integrations import pocketcasts_artwork

                # Try to fetch artwork from alternative sources
                # Use artwork from iTunes if we already fetched it above
                alternative_artwork = None
                if itunes_artwork:
                    alternative_artwork = itunes_artwork
                else:
                    # Try other sources (RSS feed, Podcast Index, or iTunes again)
                    alternative_artwork = pocketcasts_artwork.fetch_podcast_artwork(
                        podcast_uuid=podcast_uuid,
                        show_title=show.title,
                        author=show.author,
                        rss_feed_url=rss_feed_url or show.rss_feed_url,
                    )

                if alternative_artwork:
                    show.image = alternative_artwork
                    updated = True
                    logger.debug("Fetched alternative artwork for %s: %s", show.title, alternative_artwork)
                elif not show.image or show.image == "":
                    # Fallback to Pocket Casts URL (even though it requires auth)
                    # At least it's stored for potential future use
                    show.image = pocketcasts_image_url
                    updated = True

            if updated:
                show.save(update_fields=["title", "author", "image", "description", "rss_feed_url"])

            # Track this show for RSS episode sync
            self.processed_shows.add(show)

            # Ensure show tracker exists (similar to ArtistTracker for music)
            from app.models import PodcastShowTracker
            PodcastShowTracker.objects.get_or_create(
                user=self.user,
                show=show,
                defaults={
                    "status": Status.IN_PROGRESS.value,
                },
            )

            # Parse published date
            published = None
            published_raw = episode_data.get("published")
            if published_raw:
                published_ts = self._parse_history_timestamp(published_raw)
                if published_ts is not None:
                    published = datetime.fromtimestamp(published_ts, tz=UTC)
                else:
                    logger.debug("Failed to parse published date: %s", published_raw)

            # Ensure episode exists
            # First try to get by UUID (most reliable)
            duration = episode_data.get("duration", 0)  # in seconds
            episode = None
            try:
                episode = PodcastEpisode.objects.get(episode_uuid=episode_uuid)
                created = False
            except PodcastEpisode.DoesNotExist:
                # If not found by UUID, try to match by title + published date
                # This prevents duplicates when RSS sync creates episodes with different GUIDs
                if episode_data.get("title") and published:
                    title_key = (episode_data["title"].lower().strip(), published.date())
                    matching_episodes = PodcastEpisode.objects.filter(
                        show=show,
                        title__iexact=episode_data["title"].strip(),
                        published__date=published.date(),
                    )
                    if matching_episodes.exists():
                        episode = matching_episodes.first()
                        # Check if there's already an episode with the Pocket Casts UUID
                        existing_uuid_episode = PodcastEpisode.objects.filter(episode_uuid=episode_uuid).first()
                        if existing_uuid_episode and existing_uuid_episode.id != episode.id:
                            # There's already an episode with this UUID, merge the duplicate
                            logger.info(
                                "Found duplicate episode: episode %s (UUID: %s) matches by title+date, "
                                "but episode %s (UUID: %s) already exists with Pocket Casts UUID. "
                                "Merging duplicate episode.",
                                episode.id,
                                episode.episode_uuid,
                                existing_uuid_episode.id,
                                episode_uuid,
                            )
                            # Find Items for both episodes
                            from app.models import Item
                            duplicate_item = Item.objects.filter(
                                media_id=episode.episode_uuid,
                                source=Sources.POCKETCASTS.value,
                                media_type=MediaTypes.PODCAST.value,
                            ).first()
                            existing_item = Item.objects.filter(
                                media_id=episode_uuid,
                                source=Sources.POCKETCASTS.value,
                                media_type=MediaTypes.PODCAST.value,
                            ).first()

                            # Update any Podcast entries pointing to the duplicate episode/item to point to existing ones
                            Podcast.objects.filter(episode=episode).update(episode=existing_uuid_episode)
                            if duplicate_item and existing_item and duplicate_item.id != existing_item.id:
                                Podcast.objects.filter(item=duplicate_item).update(item=existing_item)
                                duplicate_item.delete()

                            # Delete the duplicate episode
                            episode.delete()
                            episode = existing_uuid_episode
                            episode_uuid = episode.episode_uuid
                        else:
                            episode_uuid = self._resolve_episode_uuid(episode, episode_uuid)
                        created = False
                if not episode and episode_data.get("title"):
                    matching_episodes = PodcastEpisode.objects.filter(
                        show=show,
                        title__iexact=episode_data["title"].strip(),
                    )
                    if matching_episodes.count() == 1:
                        episode = matching_episodes.first()
                        episode_uuid = self._resolve_episode_uuid(episode, episode_uuid)
                        created = False

                # If still no match, create new episode
                if not episode:
                    episode = PodcastEpisode.objects.create(
                        episode_uuid=episode_uuid,
                        show=show,
                        title=episode_data.get("title", "Unknown Episode"),
                        slug=episode_data.get("slug", ""),
                        published=published,
                        duration=duration,
                        audio_url=episode_data.get("url", ""),
                        episode_number=episode_data.get("episodeNumber") or episode_data.get("episode_number", 0),
                        season_number=episode_data.get("episodeSeason") or episode_data.get("season_number", 0),
                        file_type=episode_data.get("fileType", ""),
                        episode_type=episode_data.get("episodeType", ""),
                        is_deleted=is_deleted,
                    )
                    created = True

            # Update is_deleted flag if it changed
            if not created and episode.is_deleted != is_deleted:
                episode.is_deleted = is_deleted
                episode.save(update_fields=["is_deleted"])

            # Update episode if we have new data
            updated = False
            if duration and episode.duration != duration:
                episode.duration = duration
                updated = True
            if published and episode.published != published:
                episode.published = published
                updated = True
            if episode_data.get("url") and episode.audio_url != episode_data["url"]:
                episode.audio_url = episode_data["url"]
                updated = True
            if updated:
                episode.save()

            # Get existing podcast or create new
            existing_podcast = self.existing_podcasts.get((episode_uuid, Sources.POCKETCASTS.value))

            # Check if we should process this media
            if existing_podcast:
                # Skip processing if episode is already completed to prevent duplicates
                if existing_podcast.status == Status.COMPLETED.value and existing_podcast.end_date:
                    logger.debug(
                        "Skipping already-completed episode %s (Podcast ID: %s)",
                        episode_data.get("title", "Unknown"),
                        existing_podcast.id
                    )
                    return
                # In "new" mode we still want to update progress/end_date for existing podcasts
                if self.mode == "overwrite":
                    self.to_delete[MediaTypes.PODCAST.value][Sources.POCKETCASTS.value].add(episode_uuid)
            elif not helpers.should_process_media(
                self.existing_media,
                self.to_delete,
                MediaTypes.PODCAST.value,
                Sources.POCKETCASTS.value,
                episode_uuid,
                self.mode,
            ):
                return

            # Get or create Item
            runtime_minutes = duration // 60 if duration else None
            item, _ = app.models.Item.objects.get_or_create(
                media_id=episode_uuid,
                source=Sources.POCKETCASTS.value,
                media_type=MediaTypes.PODCAST.value,
                defaults={
                    "title": episode_data.get("title", "Unknown Episode"),
                    "image": "",  # No artwork in history API
                    "runtime_minutes": runtime_minutes,
                    "release_datetime": published,
                },
            )

            # Update item if needed
            item_update_fields = []
            if runtime_minutes and item.runtime_minutes != runtime_minutes:
                item.runtime_minutes = runtime_minutes
                item_update_fields.append("runtime_minutes")
            if published and item.release_datetime != published:
                item.release_datetime = published
                item_update_fields.append("release_datetime")
            if item_update_fields:
                item.save(update_fields=item_update_fields)
            
            # Fallback lookup: if dict lookup failed, try querying by Item directly
            # This handles cases where episode UUID changed due to duplicate episode merging
            # or where existing_podcasts dict was built with stale UUIDs
            if not existing_podcast:
                existing_podcast = Podcast.objects.filter(item=item, user=self.user).order_by('-created_at').first()
                if existing_podcast:
                    # Cache it in the dict for future lookups in this import
                    self.existing_podcasts[(episode_uuid, Sources.POCKETCASTS.value)] = existing_podcast
                    logger.debug(
                        "Found existing podcast via fallback lookup by Item for episode %s (UUID: %s)",
                        episode_data.get("title", "Unknown"),
                        episode_uuid
                    )
                    # Skip processing if episode is already completed to prevent duplicates
                    if existing_podcast.status == Status.COMPLETED.value and existing_podcast.end_date:
                        logger.debug(
                            "Skipping already-completed episode %s (Podcast ID: %s)",
                            episode_data.get("title", "Unknown"),
                            existing_podcast.id
                        )
                        return
            if self.debug_uuid and (
                incoming_uuid == self.debug_uuid or episode_uuid == self.debug_uuid
            ):
                logger.info(
                    "Resolved episode UUID %s (incoming %s). Existing podcast: %s",
                    episode_uuid,
                    incoming_uuid,
                    existing_podcast.id if existing_podcast else None,
                )
            
            # Extract progress data
            playing_status = episode_data.get("playingStatus", 0)  # 2=in-progress, 3=completed
            played_up_to = episode_data.get("playedUpTo", 0)  # in seconds
            duration_seconds = duration or 0
            if playing_status == 3 and duration_seconds and not played_up_to:
                played_up_to = duration_seconds

            latest_podcast = existing_podcast
            if not latest_podcast:
                latest_podcast = Podcast.objects.filter(user=self.user, item=item).order_by("-created_at").first()
            if self._is_duplicate_completion(
                latest_podcast,
                played_up_to,
                duration_seconds,
                playing_status,
            ):
                if self.debug_uuid and (
                    incoming_uuid == self.debug_uuid or episode_uuid == self.debug_uuid
                ):
                    logger.info(
                        "Skipping duplicate completed episode %s (UUID: %s)",
                        episode_data.get("title", "Unknown"),
                        episode_uuid,
                    )
                else:
                    logger.debug(
                        "Skipping duplicate completed episode %s (UUID: %s)",
                        episode_data.get("title", "Unknown"),
                        episode_uuid,
                    )
                return
            
            # Calculate progress delta and determine status
            if existing_podcast:
                old_played_up_to = existing_podcast.played_up_to_seconds
                if old_played_up_to is None:
                    # If we previously marked as completed but never stored played_up_to, treat as fully played
                    if existing_podcast.status == Status.COMPLETED.value and duration_seconds:
                        old_played_up_to = duration_seconds
                    else:
                        old_played_up_to = 0
                old_status = (
                    existing_podcast.last_seen_status
                    if existing_podcast.last_seen_status is not None
                    else existing_podcast.status
                )
            else:
                old_played_up_to = 0
                old_status = None

            delta_seconds, new_status, progress_minutes = self._calculate_progress_delta(
                old_played_up_to,
                played_up_to,
                duration_seconds,
                playing_status,
                old_status,
            )

            # If we already marked this episode completed, avoid re-counting plays
            already_completed = existing_podcast and existing_podcast.status == Status.COMPLETED.value
            if already_completed:
                # Always set delta_seconds to 0 for already-completed episodes
                # regardless of new_status to prevent duplicate history entries
                delta_seconds = 0
                progress_minutes = existing_podcast.progress

            # Estimate completion date
            completion_date = None
            if already_completed and existing_podcast.end_date:
                completion_date = existing_podcast.end_date
            elif already_completed and self.previous_sync_at:
                completion_date = self.previous_sync_at

            should_set_completion_date = new_status == Status.COMPLETED.value

            if should_set_completion_date and existing_podcast:
                # Existing podcast just completed or missing completion data
                if completion_date is None:
                    if (
                        existing_podcast.status != Status.COMPLETED.value
                        or not existing_podcast.end_date
                        or delta_seconds > 0
                    ):
                        episode_uuid = episode_data.get("uuid")
                        sync_window_start, sync_window_end, existing_history = self._get_inference_window()
                        debug_context = {
                            "played_up_to": played_up_to,
                            "old_played_up_to": old_played_up_to,
                            "old_status": old_status,
                            "new_status": new_status,
                            "duration_seconds": duration_seconds,
                        }
                        completion_date = self._infer_completion_date(
                            duration_seconds,
                            sync_window_start,
                            sync_window_end,
                            existing_history,
                            [],
                            published,
                            episode_uuid,
                            self.previous_sync_at,
                            debug_context=debug_context,
                        )
            elif should_set_completion_date and published:
                if defer_completion_date:
                    # Will be inferred later in import_data()
                    completion_date = None
                    logger.debug(
                        "Deferring completion_date inference for episode %s (published: %s, duration: %d seconds)",
                        episode_data.get("title", "Unknown"),
                        published,
                        duration_seconds,
                    )
                else:
                    # First import: use published + duration
                    if duration_seconds:
                        completion_date = published + timedelta(seconds=duration_seconds)
                    else:
                        completion_date = published
                    if completion_date and timezone.is_naive(completion_date):
                        completion_date = timezone.make_aware(completion_date)
                    logger.debug(
                        "Calculated completion_date for episode %s: published=%s, duration=%s, completion_date=%s",
                        episode_data.get("title", "Unknown"),
                        published,
                        duration_seconds,
                        completion_date,
                    )

            # Create or update podcast entry
            if existing_podcast:
                # Track if any fields actually changed
                fields_changed = False

                # Only update fields if they've actually changed
                if existing_podcast.item != item:
                    existing_podcast.item = item
                    fields_changed = True
                if existing_podcast.show != show:
                    existing_podcast.show = show
                    fields_changed = True
                if existing_podcast.episode != episode:
                    existing_podcast.episode = episode
                    fields_changed = True
                if existing_podcast.status != new_status:
                    existing_podcast.status = new_status
                    fields_changed = True
                if existing_podcast.progress != progress_minutes:
                    existing_podcast.progress = progress_minutes  # Store in minutes
                    fields_changed = True
                if existing_podcast.played_up_to_seconds != played_up_to:
                    existing_podcast.played_up_to_seconds = played_up_to
                    fields_changed = True
                if existing_podcast.last_seen_status != playing_status:
                    existing_podcast.last_seen_status = playing_status
                    fields_changed = True

                # Set end_date if completed (use estimated completion date)
                # For already-completed episodes, don't update end_date if it already exists
                # to prevent HistoricalRecords from creating duplicate history entries
                if new_status == Status.COMPLETED.value and completion_date:
                    # Never update end_date if episode is already completed and end_date exists
                    if already_completed and existing_podcast.end_date is not None:
                        # Preserve existing end_date for already-completed episodes
                        pass
                    elif existing_podcast.end_date is None:
                        # Only update if missing
                        existing_podcast.end_date = completion_date
                        fields_changed = True
                        logger.debug(
                            "Updated end_date for existing podcast %s: %s",
                            episode_data.get("title", "Unknown"),
                            completion_date,
                        )
                    elif not already_completed:
                        # Only update if different AND episode is newly completing (not already completed)
                        if existing_podcast.end_date != completion_date:
                            existing_podcast.end_date = completion_date
                            fields_changed = True
                            logger.debug(
                                "Updated end_date for newly completed podcast %s: %s",
                                episode_data.get("title", "Unknown"),
                                completion_date,
                            )

                # Only save if there are actual changes to prevent unnecessary history entries
                if fields_changed:
                    existing_podcast.save()

                # Record history for delta time (create history entry manually)
                # Use completion_date for history timestamp if available, otherwise use published date
                history_timestamp = completion_date or published or timezone.now()
                if delta_seconds > 0:
                    self._record_history(existing_podcast, delta_seconds, history_timestamp)
            else:
                # Final safety check: if no entry found via dictionary/fallback,
                # check if ANY completed entry exists for this Item
                # This prevents duplicates when multiple Podcast entries share the same key
                # and dictionary lookup fails or only finds one of them
                existing_completed = Podcast.objects.filter(
                    item=item,
                    user=self.user,
                    status=Status.COMPLETED.value
                ).exclude(end_date__isnull=True).exists()
                
                if existing_completed:
                    logger.debug(
                        "Skipping episode %s - already has completed Podcast entry(ies) for Item %s, "
                        "but not found via dictionary/fallback lookup",
                        episode_data.get("title", "Unknown"),
                        item.id
                    )
                    return
                
                # Create new
                podcast = Podcast(
                    item=item,
                    user=self.user,
                    show=show,
                    episode=episode,
                    status=new_status,
                    progress=progress_minutes,
                    played_up_to_seconds=played_up_to,
                    last_seen_status=playing_status,
                    start_date=published if progress_minutes > 0 else None,  # Use published date as start
                    end_date=completion_date if new_status == Status.COMPLETED.value else None,
                    notes="Imported from Pocket Casts",
                )

                self.bulk_media[MediaTypes.PODCAST.value].append(podcast)

                # Store delta for history recording after bulk create
                # We'll record history after the podcast is saved
                if delta_seconds > 0:
                    if not hasattr(self, "_pending_history"):
                        self._pending_history = []
                    # Store episode_uuid, delta, and timestamp for lookup after bulk create
                    history_timestamp = completion_date or published or timezone.now()
                    self._pending_history.append((episode_uuid, delta_seconds, history_timestamp))

        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Failed to process Pocket Casts episode %s: %s", episode_data.get("uuid"), e)
            self.warnings.append(f"{episode_data.get('title', 'Unknown')}: {e!s}")

    def _calculate_progress_delta(self, old_played_up_to, new_played_up_to, duration, playing_status, old_status):
        """Calculate time delta between imports, determine status, and return progress.
        
        Returns:
            tuple: (delta_seconds, status, progress_minutes)
        """
        # Clamp values to duration
        old_played = min(old_played_up_to, duration) if duration > 0 else old_played_up_to
        new_played = min(new_played_up_to, duration) if duration > 0 else new_played_up_to

        # Calculate delta (ignore negative deltas - user scrubbed backward)
        delta = max(0, new_played - old_played)

        # Determine if completed
        epsilon = 5  # 5 second tolerance
        # Only mark as completed if:
        # 1. Playing status is 3 (completed) AND played_up_to is significant (> 60 seconds or > 10% of duration)
        # 2. OR played_up_to is within 5 seconds of duration
        # This prevents false positives where Pocket Casts marks episodes as completed but played_up_to is 0
        significant_progress = duration > 0 and (new_played > 60 or new_played > duration * 0.1)
        is_completed = (
            (playing_status == 3 and significant_progress) or
            (duration > 0 and new_played >= duration - epsilon)
        )

        # Determine status
        if is_completed:
            status = Status.COMPLETED.value
            progress_minutes = (duration // 60) if duration > 0 else 0
        elif delta > 0 or (old_status != 2 and playing_status == 2):
            # Progress made or newly in-progress
            status = Status.IN_PROGRESS.value
            progress_minutes = (new_played // 60) if new_played > 0 else 0
        elif old_status == Status.IN_PROGRESS.value and playing_status == 2:
            # Still in progress, no new progress
            status = Status.IN_PROGRESS.value
            progress_minutes = (new_played // 60) if new_played > 0 else 0
        # Default to in-progress if we have any progress
        elif new_played > 0:
            status = Status.IN_PROGRESS.value
            progress_minutes = (new_played // 60)
        else:
            status = Status.PLANNING.value
            progress_minutes = 0

        return delta, status, progress_minutes

    def _is_duplicate_completion(self, existing_podcast, played_up_to, duration_seconds, playing_status):
        """Return True when an incoming completed entry matches an existing completed play."""
        if playing_status != 3:
            return False
        if not existing_podcast or existing_podcast.status != Status.COMPLETED.value or not existing_podcast.end_date:
            return False

        epsilon = 5
        if played_up_to and existing_podcast.played_up_to_seconds:
            if abs(existing_podcast.played_up_to_seconds - played_up_to) <= epsilon:
                return True

        if duration_seconds and played_up_to >= duration_seconds - epsilon:
            return True

        if duration_seconds:
            duration_minutes = duration_seconds // 60
            if existing_podcast.progress and existing_podcast.progress >= duration_minutes:
                return True

        return False

    def _record_history(self, podcast, delta_seconds, import_time):
        """Record play history entry for delta time.
        
        Creates a historical record entry for the time listened.
        """
        if delta_seconds <= 0:
            return
        
        # Check for duplicate history entry by comparing end_date (actual play completion time)
        # instead of history_date (when the history record was created)
        latest_history = podcast.history.filter(end_date__isnull=False).order_by("-end_date").first()
        if latest_history and latest_history.end_date and import_time:
            # Check if we're trying to record history with the same or very similar end_date
            time_diff = abs((import_time - latest_history.end_date).total_seconds())
            if time_diff < 300:  # Within 5 minutes
                logger.debug(
                    "Skipping duplicate history entry for podcast %s (end_date difference: %d seconds)",
                    podcast.id,
                    time_diff
                )
                return

        # Convert to minutes for history
        delta_minutes = delta_seconds // 60
        if delta_minutes == 0 and delta_seconds > 0:
            delta_minutes = 1  # At least 1 minute if any time was spent

        # Create history entry by updating progress
        # HistoricalRecords will automatically create a history entry
        old_progress = podcast.progress
        new_progress = min(podcast.progress + delta_minutes, podcast.item.runtime_minutes or 999999)

        if new_progress > old_progress:
            podcast.progress = new_progress
            podcast.save()

            # Reset progress if we just wanted to record history
            # (This is a bit of a hack, but ensures history is recorded)
            if new_progress > old_progress + delta_minutes:
                # We went over, adjust back
                podcast.progress = old_progress + delta_minutes
                podcast.save()

    def _sync_episodes_from_rss(self, show, rss_feed_url):
        """Sync episodes from RSS feed and merge with existing episodes.
        
        Fetches all episodes from RSS feed and creates/updates PodcastEpisode
        records. This ensures we have the complete episode list, not just
        what's in Pocket Casts history.
        
        Args:
            show: PodcastShow instance
            rss_feed_url: RSS feed URL to fetch from
        """
        from app.models import PodcastEpisode
        from integrations import podcast_rss

        # Fetch episodes from RSS
        rss_episodes = podcast_rss.fetch_episodes_from_rss(rss_feed_url)

        if not rss_episodes:
            logger.debug("No episodes found in RSS feed for show %s", show.title)
            return

        # Get existing episodes for this show
        existing_episodes = {
            episode.episode_uuid: episode
            for episode in PodcastEpisode.objects.filter(show=show)
        }

        # Also create a lookup by title + published date for fuzzy matching
        existing_by_title_date = {}
        for episode in existing_episodes.values():
            if episode.title and episode.published:
                key = (episode.title.lower().strip(), episode.published.date())
                existing_by_title_date[key] = episode

        created_count = 0
        updated_count = 0

        for rss_ep in rss_episodes:
            # Try to find matching episode
            matched_episode = None

            # First try by GUID if available
            if rss_ep.get("guid"):
                # Check if GUID matches any episode_uuid
                for ep_uuid, episode in existing_episodes.items():
                    if ep_uuid == rss_ep["guid"]:
                        matched_episode = episode
                        break

            # If no GUID match, try by title + published date
            if not matched_episode and rss_ep.get("title") and rss_ep.get("published"):
                title_key = (rss_ep["title"].lower().strip(), rss_ep["published"].date())
                matched_episode = existing_by_title_date.get(title_key)

            if matched_episode:
                # Update existing episode
                updated = False
                update_fields = []

                # If UUID differs and we have RSS GUID, update to RSS GUID
                # This ensures consistency when Pocket Casts UUID and RSS GUID differ
                # But prefer keeping Pocket Casts UUID format if it looks like one (has hyphens in UUID format)
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
                    updated_count += 1
            else:
                # Create new episode
                # Generate a UUID for the episode if RSS doesn't have one
                episode_uuid = rss_ep.get("guid")
                if not episode_uuid:
                    # Use a hash of title + published date as fallback UUID
                    import hashlib
                    uuid_str = f"{rss_ep.get('title', '')}{rss_ep.get('published', '')}"
                    episode_uuid = hashlib.md5(uuid_str.encode()).hexdigest()[:36]

                # Check if this UUID already exists (shouldn't happen, but be safe)
                if episode_uuid in existing_episodes:
                    continue

                new_episode = PodcastEpisode.objects.create(
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
                logger.debug("Created new episode from RSS: %s", new_episode.title)

        logger.info(
            "Synced episodes from RSS for show %s: %d created, %d updated",
            show.title,
            created_count,
            updated_count,
        )

    def _cleanup_duplicate_episodes(self):
        """Clean up duplicate podcast episodes.
        
        Calls the global cleanup function and handles warnings.
        
        Returns:
            dict: Statistics about the cleanup (duplicates_removed, episodes_merged, items_merged)
        """
        stats = _cleanup_duplicate_episodes_global()
        return stats

    def _parse_history_timestamp(self, value):
        """Parse a history timestamp into epoch seconds."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 1_000_000_000_000:
                timestamp /= 1000
            return timestamp
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if timezone.is_naive(parsed):
                    parsed = parsed.replace(tzinfo=UTC)
                return parsed.timestamp()
            except ValueError:
                return None
        return None

    def _get_history_event_timestamp(self, episode_data):
        """Return a usable event timestamp if present in history data."""
        for field in (
            "playedAt",
            "played_at",
            "completedAt",
            "completed_at",
            "modifiedAt",
            "modified_at",
            "lastModified",
            "last_modified",
            "timestamp",
        ):
            if field in episode_data and episode_data[field]:
                parsed = self._parse_history_timestamp(episode_data[field])
                if parsed is not None:
                    return parsed
        return None

    def _history_entry_sort_key(self, episode_data):
        """Return a sort key to pick the best entry for a duplicate UUID."""
        playing_status = episode_data.get("playingStatus", 0)
        is_completed = 1 if playing_status == 3 else 0
        played_up_to = episode_data.get("playedUpTo") or 0
        event_time = self._get_history_event_timestamp(episode_data) or 0
        return (is_completed, event_time, played_up_to)

    def _is_better_history_entry(self, candidate, existing):
        """Return True if candidate is a better entry than existing."""
        return self._history_entry_sort_key(candidate) > self._history_entry_sort_key(existing)

    def _should_keep_existing_episode_uuid(self, episode):
        """Return True if the episode UUID already has tracked identity we should preserve."""
        if Podcast.objects.filter(episode=episode).exists():
            return True
        return app.models.Item.objects.filter(
            media_id=episode.episode_uuid,
            source=Sources.POCKETCASTS.value,
            media_type=MediaTypes.PODCAST.value,
        ).exists()

    def _resolve_episode_uuid(self, episode, incoming_uuid):
        """Return the UUID to use for tracking this episode."""
        if episode.episode_uuid == incoming_uuid:
            return incoming_uuid

        if self._should_keep_existing_episode_uuid(episode):
            if self.debug_uuid and (
                incoming_uuid == self.debug_uuid or episode.episode_uuid == self.debug_uuid
            ):
                logger.info(
                    "Keeping existing episode UUID %s for %s (incoming %s)",
                    episode.episode_uuid,
                    episode.title,
                    incoming_uuid,
                )
            return episode.episode_uuid

        if self.debug_uuid and (
            incoming_uuid == self.debug_uuid or episode.episode_uuid == self.debug_uuid
        ):
            logger.info(
                "Updating episode UUID %s -> %s for %s",
                episode.episode_uuid,
                incoming_uuid,
                episode.title,
            )

        episode.episode_uuid = incoming_uuid
        episode.save(update_fields=["episode_uuid"])
        return incoming_uuid

    def _dedupe_history(self, episodes):
        """Deduplicate history entries by episode UUID."""
        if not episodes:
            return episodes

        deduped = {}
        extras = []

        for episode_data in episodes:
            episode_uuid = episode_data.get("uuid")
            if self.debug_uuid and episode_uuid == self.debug_uuid:
                logger.info(
                    "Pocket Casts history raw entry for %s: %s",
                    episode_uuid,
                    episode_data,
                )
            if not episode_uuid:
                extras.append(episode_data)
                continue

            existing = deduped.get(episode_uuid)
            if not existing or self._is_better_history_entry(episode_data, existing):
                deduped[episode_uuid] = episode_data

        if len(deduped) + len(extras) < len(episodes):
            logger.debug(
                "Deduped Pocket Casts history: %d -> %d entries",
                len(episodes),
                len(deduped) + len(extras),
            )

        return list(deduped.values()) + extras
    
    def _discover_rss_feed_url(self, show_title, author=None):
        """Discover RSS feed URL from iTunes API.
        
        Args:
            show_title: Podcast show title
            author: Podcast author (optional)
            
        Returns:
            RSS feed URL or None if not found
        """
        try:

            import requests

            # Build search query
            if author:
                query = f"{show_title} {author}"
            else:
                query = show_title

            # iTunes API expects URL-encoded query
            params = {
                "term": query,
                "media": "podcast",
                "limit": 5,  # Get top 5 results
            }

            ITUNES_API_BASE = "https://itunes.apple.com/search"
            response = requests.get(
                ITUNES_API_BASE,
                params=params,
                headers={"User-Agent": "Yamtrack/1.0 (https://github.com/FuzzyGrim/Yamtrack)"},
                timeout=10,
            )
            response.raise_for_status()

            data = response.json()
            results = data.get("results", [])

            if not results:
                return None

            # Try to find best match by title
            show_title_lower = show_title.lower()
            for result in results:
                result_title = result.get("collectionName", "").lower()
                # Check if titles are similar (exact match or one contains the other)
                if (
                    result_title == show_title_lower
                    or show_title_lower in result_title
                    or result_title in show_title_lower
                ):
                    feed_url = result.get("feedUrl")
                    if feed_url:
                        logger.debug(
                            "Discovered RSS feed URL from iTunes for %s: %s",
                            show_title,
                            safe_url(feed_url),
                        )
                        return feed_url

            # If no exact match, use first result's feed URL
            if results:
                feed_url = results[0].get("feedUrl")
                if feed_url:
                    logger.debug(
                        "Discovered RSS feed URL from iTunes (first result) for %s: %s",
                        show_title,
                        safe_url(feed_url),
                    )
                    return feed_url

        except Exception as e:
            logger.debug(
                "Failed to discover RSS feed URL from iTunes for %s: %s",
                show_title,
                exception_summary(e),
            )

        return None
