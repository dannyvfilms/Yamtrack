import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone

import jwt
import requests
from django.conf import settings
from django.utils import timezone

import app
from app.models import MediaTypes, Sources, Status, PodcastShow, PodcastEpisode, Podcast, Music, Episode, Movie
from app.providers import services
from integrations import models as integration_models
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError, encrypt, decrypt

logger = logging.getLogger(__name__)

POCKETCASTS_API_BASE_URL = "https://api.pocketcasts.com"


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
            logger.error("Pocket Casts account has no credentials or tokens - email: %s, access_token: %s, refresh_token: %s", 
                        "exists" if self.account.email else "empty",
                        "exists" if has_access_token else "empty",
                        "exists" if has_refresh_token else "empty")
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
        
        # Track existing podcasts to calculate deltas
        self.existing_podcasts = {
            (podcast.item.media_id, podcast.item.source): podcast
            for podcast in Podcast.objects.filter(user=user).select_related("item", "episode", "show")
        }

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
                # (status 3 or played up to duration with 5 second tolerance)
                epsilon = 5
                is_completed = (
                    playing_status == 3 or 
                    (duration > 0 and played_up_to >= duration - epsilon)
                )
                
                if is_completed and published:
                    new_completed_podcasts.append((episode_data, duration, published))
        
        # Second pass: infer completion dates for new completed podcasts
        if new_completed_podcasts and not is_first_import:
            # Get sync window
            sync_window_end = timezone.now()
            sync_window_start = self.account.last_sync_at or (sync_window_end - timedelta(hours=2))
            
            # Get existing history items in the window
            existing_history = self._get_history_items_in_range(sync_window_start, sync_window_end)
            
            # Infer completion dates for each new podcast
            for episode_data, duration_seconds, published_date in new_completed_podcasts:
                episode_uuid = episode_data.get("uuid")
                
                # Get other new podcasts (excluding this one)
                other_podcasts = [(p, d, pub) for (e, d, pub) in new_completed_podcasts 
                                 if e.get("uuid") != episode_uuid]
                
                # Infer completion date
                inferred_date = self._infer_completion_date(
                    duration_seconds,
                    sync_window_start,
                    sync_window_end,
                    existing_history,
                    [(pub, d) for (_, d, pub) in other_podcasts],
                    published_date
                )
                
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
        if hasattr(self, '_pending_history'):
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

        # Update last sync time
        self.account.last_sync_at = timezone.now()
        self.account.save(update_fields=["last_sync_at"])

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
            from app.history_cache import schedule_history_refresh
            from app import statistics_cache
            
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
                    self.account.token_expires_at = datetime.fromtimestamp(exp, tz=dt_timezone.utc)
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
            if music.track and music.track.duration:
                duration_seconds = music.track.duration
            elif music.item and music.item.runtime_minutes:
                duration_seconds = music.item.runtime_minutes * 60
            
            history_items.append((music.end_date, duration_seconds, 'music', True))
        
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
            
            history_items.append((podcast.end_date, duration_seconds, 'podcast', False))
        
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
            
            history_items.append((episode.end_date, duration_seconds, 'episode', True))
        
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
            
            history_items.append((movie.end_date, duration_seconds, 'movie', False))
        
        # Sort by end_date ascending
        history_items.sort(key=lambda x: x[0])
        
        return history_items

    def _infer_completion_date(self, podcast_duration_seconds, sync_window_start, sync_window_end, existing_history_items, other_new_podcasts, this_podcast_published):
        """Infer completion date for a podcast by fitting it into timeline gaps.
        
        Args:
            podcast_duration_seconds: Duration of the podcast in seconds
            sync_window_start: Start of sync window (datetime)
            sync_window_end: End of sync window (datetime)
            existing_history_items: List from _get_history_items_in_range()
            other_new_podcasts: List of other new podcasts being processed, each as (published_date, duration_seconds)
            this_podcast_published: Published date of this podcast (for ordering)
            
        Returns:
            Inferred completion datetime
        """
        # Sort other new podcasts by published date (oldest first)
        sorted_other_podcasts = sorted(other_new_podcasts, key=lambda x: x[0])
        
        # Build timeline: combine existing items and other new podcasts
        timeline = []
        
        # Add existing history items
        for end_date, duration, media_type, is_scrobbled in existing_history_items:
            timeline.append({
                'end_date': end_date,
                'duration_seconds': duration,
                'media_type': media_type,
                'is_scrobbled': is_scrobbled,
                'is_new_podcast': False,
            })
        
        # Add other new podcasts (sorted by published date)
        for published_date, duration_seconds in sorted_other_podcasts:
            # Estimate start time (we'll refine this when placing)
            timeline.append({
                'end_date': None,  # Will be calculated
                'duration_seconds': duration_seconds,
                'media_type': 'podcast',
                'is_scrobbled': False,
                'is_new_podcast': True,
                'published_date': published_date,
            })
        
        # Sort timeline by end_date (existing items) or published_date (new podcasts)
        timeline.sort(key=lambda x: x['end_date'] if x['end_date'] else x.get('published_date', sync_window_start))
        
        # Determine this podcast's position in the ordered list
        this_podcast_index = 0
        for i, other_podcast in enumerate(sorted_other_podcasts):
            if other_podcast[0] < this_podcast_published:
                this_podcast_index = i + 1
            else:
                break
        
        # Find where to place this podcast
        # Start from sync_window_start
        current_time = sync_window_start
        
        # If there are other new podcasts before this one, we need to account for them
        # For now, we'll try to fit it after existing items and before/after other new podcasts
        
        # Strategy: Look for gaps in the timeline
        # 1. Try to fit after scrobbled items (Music/Episode) - these have precise timestamps
        # 2. Try to fit in gaps between items
        # 3. If no gap, place at end of window or before next scrobbled item
        
        # Check if podcast is too long for the window
        window_duration = (sync_window_end - sync_window_start).total_seconds()
        if podcast_duration_seconds and podcast_duration_seconds > window_duration:
            # Too long - place at end of window
            logger.debug("Podcast duration (%d seconds) exceeds window size (%d seconds), placing at end", 
                        podcast_duration_seconds, window_duration)
            return sync_window_end
        
        # Build timeline with start/end times for existing items only
        # (We'll handle other new podcasts separately)
        timeline_with_times = []
        for item in timeline:
            if item['is_new_podcast']:
                continue  # Skip other new podcasts for now
            
            item_end = item['end_date']
            item_duration = item['duration_seconds'] or 0
            
            # Estimate start time from end and duration
            item_start = item_end - timedelta(seconds=item_duration) if item_duration > 0 else item_end
            
            timeline_with_times.append({
                'start': item_start,
                'end': item_end,
                'duration': item_duration,
                'is_scrobbled': item['is_scrobbled'],
            })
        
        # Sort by end time
        timeline_with_times.sort(key=lambda x: x['end'])
        
        # Strategy 1: Try to place after scrobbled items (Music/Episode) - these have precise timestamps
        # Example: Music ends at 12:30pm, place podcast to end at 12:30pm + duration
        for i, item in enumerate(timeline_with_times):
            if not item['is_scrobbled']:
                continue
            
            # Calculate where podcast would end if we start it right after this scrobbled item
            podcast_start = item['end']
            podcast_end = podcast_start + timedelta(seconds=podcast_duration_seconds)
            
            # Check if it fits before the next item or end of window
            next_item = timeline_with_times[i + 1] if i + 1 < len(timeline_with_times) else None
            if next_item:
                gap_end = next_item['start']
            else:
                gap_end = sync_window_end
            
            # Check if podcast fits in this gap
            if podcast_end <= gap_end and podcast_start >= sync_window_start:
                logger.debug("Placing podcast after scrobbled item %s: start=%s, end=%s", 
                           item['end'], podcast_start, podcast_end)
                return podcast_end
        
        # Strategy 2: Try to fit in any gap between items
        prev_end = sync_window_start
        for item in timeline_with_times:
            gap_start = prev_end
            gap_end = item['start']
            gap_size = (gap_end - gap_start).total_seconds()
            
            if gap_size >= podcast_duration_seconds:
                # Fit it in this gap
                completion_time = gap_start + timedelta(seconds=podcast_duration_seconds)
                logger.debug("Placing podcast in gap: start=%s, end=%s", gap_start, completion_time)
                return completion_time
            
            prev_end = item['end']
        
        # Check gap after last item
        if timeline_with_times:
            last_item = timeline_with_times[-1]
            gap_start = last_item['end']
            gap_end = sync_window_end
            gap_size = (gap_end - gap_start).total_seconds()
            
            if gap_size >= podcast_duration_seconds:
                completion_time = gap_start + timedelta(seconds=podcast_duration_seconds)
                logger.debug("Placing podcast after last item: start=%s, end=%s", gap_start, completion_time)
                return completion_time
        
        # Strategy 3: No suitable gap found
        # Place before next scrobbled item, or at end of window
        for item in timeline_with_times:
            if item['is_scrobbled']:
                # Place before this scrobbled item (but make sure it's within window)
                completion_time = item['start'] - timedelta(seconds=1)
                if completion_time >= sync_window_start:
                    logger.debug("No gap found, placing before scrobbled item at %s", completion_time)
                    return completion_time
        
        # Last resort: place at end of window
        logger.debug("No suitable position found, placing at end of window: %s", sync_window_end)
        return sync_window_end

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
                    self.account.token_expires_at = datetime.fromtimestamp(exp, tz=dt_timezone.utc)
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
            
            # Fetch artwork from alternative sources if needed
            # Only fetch if image is empty or is a Pocket Casts authenticated URL
            should_fetch_artwork = (
                not show.image 
                or show.image == "" 
                or show.image.startswith(POCKETCASTS_API_BASE_URL)
            )
            
            if should_fetch_artwork:
                from integrations import pocketcasts_artwork
                
                # Check for RSS feed URL in metadata
                # The podcast list might have 'url' (website) but not explicit RSS feed
                # We'll check common field names
                rss_feed_url = (
                    show_metadata.get("rssUrl") 
                    or show_metadata.get("rss_url") 
                    or show_metadata.get("feedUrl")
                    or show_metadata.get("feed_url")
                )
                
                # Try to fetch artwork from alternative sources
                alternative_artwork = pocketcasts_artwork.fetch_podcast_artwork(
                    podcast_uuid=podcast_uuid,
                    show_title=show.title,
                    author=show.author,
                    rss_feed_url=rss_feed_url,
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
                show.save(update_fields=["title", "author", "image", "description"])
            
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
            if episode_data.get("published"):
                try:
                    published = datetime.fromisoformat(episode_data["published"].replace("Z", "+00:00"))
                    # Ensure timezone-aware
                    if published and timezone.is_naive(published):
                        published = timezone.make_aware(published)
                except (ValueError, AttributeError):
                    logger.debug("Failed to parse published date: %s", episode_data.get("published"))

            # Ensure episode exists
            duration = episode_data.get("duration", 0)  # in seconds
            episode, created = PodcastEpisode.objects.get_or_create(
                episode_uuid=episode_uuid,
                defaults={
                    "show": show,
                    "title": episode_data.get("title", "Unknown Episode"),
                    "slug": episode_data.get("slug", ""),
                    "published": published,
                    "duration": duration,
                    "audio_url": episode_data.get("url", ""),
                    "episode_number": episode_data.get("episodeNumber") or episode_data.get("episode_number", 0),
                    "season_number": episode_data.get("episodeSeason") or episode_data.get("season_number", 0),
                    "file_type": episode_data.get("fileType", ""),
                    "episode_type": episode_data.get("episodeType", ""),
                    "is_deleted": is_deleted,
                },
            )
            
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
                # In "new" mode we still want to update progress/end_date for existing podcasts
                if self.mode == "overwrite":
                    self.to_delete[MediaTypes.PODCAST.value][Sources.POCKETCASTS.value].add(episode_uuid)
            else:
                if not helpers.should_process_media(
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
            
            # Extract progress data
            playing_status = episode_data.get("playingStatus", 0)  # 2=in-progress, 3=completed
            played_up_to = episode_data.get("playedUpTo", 0)  # in seconds
            duration_seconds = duration or 0
            if playing_status == 3 and duration_seconds and not played_up_to:
                played_up_to = duration_seconds
            
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
                        completion_date = timezone.now()
                        if self.previous_sync_at:
                            completion_date = max(self.previous_sync_at, completion_date)
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
                # Only update if it's None or actually different
                if new_status == Status.COMPLETED.value and completion_date:
                    if existing_podcast.end_date is None or existing_podcast.end_date != completion_date:
                        existing_podcast.end_date = completion_date
                        fields_changed = True
                        logger.debug(
                            "Updated end_date for existing podcast %s: %s",
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
                    if not hasattr(self, '_pending_history'):
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
        is_completed = (
            playing_status == 3 or 
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
        else:
            # Default to in-progress if we have any progress
            if new_played > 0:
                status = Status.IN_PROGRESS.value
                progress_minutes = (new_played // 60)
            else:
                status = Status.PLANNING.value
                progress_minutes = 0
        
        return delta, status, progress_minutes

    def _record_history(self, podcast, delta_seconds, import_time):
        """Record play history entry for delta time.
        
        Creates a historical record entry for the time listened.
        """
        if delta_seconds <= 0:
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
