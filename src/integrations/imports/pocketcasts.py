import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta

import jwt
import requests
from django.conf import settings
from django.utils import timezone

import app
from app.models import MediaTypes, Sources, Status, PodcastShow, PodcastEpisode, Podcast
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
        except integration_models.PocketCastsAccount.DoesNotExist:
            msg = "Pocket Casts account not connected"
            raise MediaImportError(msg)

        if not self.account.is_connected:
            msg = "Pocket Casts account not connected"
            raise MediaImportError(msg)

        self.existing_media = helpers.get_existing_media(user)
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

        # Fetch history (last 100 episodes only, no pagination)
        episodes = self._fetch_history()

        if not episodes:
            logger.info("No episodes found for Pocket Casts user %s", self.user.username)
            return {}, ""

        # Process each episode
        for episode_data in episodes:
            self._process_episode(episode_data)

        # Cleanup and bulk create
        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)
        
        # Record history for newly created podcasts
        if hasattr(self, '_pending_history'):
            for episode_uuid, delta_seconds, import_time in self._pending_history:
                # Reload podcast from DB after bulk create
                try:
                    podcast = Podcast.objects.get(
                        user=self.user,
                        item__media_id=episode_uuid,
                        item__source=Sources.POCKETCASTS.value,
                    )
                    self._record_history(podcast, delta_seconds, import_time)
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

        return imported_counts, "\n".join(self.warnings) if self.warnings else ""

    def _ensure_valid_token(self):
        """Ensure we have a valid access token."""
        if not self.account.access_token:
            msg = "No access token available"
            raise MediaImportError(msg)
        
        # Check if token is expired (but we can't refresh without refresh token)
        if self.account.is_token_expired:
            logger.warning("Pocket Casts token is expired for user %s. User may need to reconnect.", self.user.username)
            # Try to use it anyway - it might still work or the expiration might be wrong

    def _refresh_token(self):
        """Refresh the access token using the refresh token."""
        url = f"{POCKETCASTS_API_BASE_URL}/user/refresh"
        
        try:
            decrypted_refresh_token = decrypt(self.account.refresh_token)
        except Exception as e:
            logger.error("Failed to decrypt refresh token: %s", e)
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
                    self.account.token_expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
            except Exception:
                # If we can't parse, set expiration to 1 hour from now as fallback
                self.account.token_expires_at = timezone.now() + timedelta(hours=1)
            
            self.account.save()
            logger.info("Successfully refreshed Pocket Casts token for user %s", self.user.username)
            
        except requests.HTTPError as e:
            if e.response.status_code == requests.codes.unauthorized:
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
                            pass
                    except Exception:
                        pass
                
                msg = "Authentication failed. Your token may have expired. Please reconnect your Pocket Casts account."
                raise MediaImportError(msg) from e
            msg = f"Pocket Casts API error: {e.response.status_code}"
            raise MediaImportError(msg) from e

    def _process_episode(self, episode_data):
        """Process single episode: create/update show/episode, calculate delta."""
        try:
            episode_uuid = episode_data.get("uuid")
            podcast_uuid = episode_data.get("podcastUuid")
            
            if not episode_uuid or not podcast_uuid:
                logger.warning("Skipping episode with missing UUIDs: %s", episode_data)
                return

            # Note: We import deleted episodes too - they're still in history
            # We'll mark them as deleted in the database but still track them
            is_deleted = episode_data.get("isDeleted", False)

            # Ensure show exists
            show, _ = PodcastShow.objects.get_or_create(
                podcast_uuid=podcast_uuid,
                defaults={
                    "title": episode_data.get("podcastTitle", "Unknown Show"),
                    "slug": episode_data.get("podcastSlug", ""),
                    "author": episode_data.get("author", ""),
                },
            )
            
            # Update show fields if we have new data
            if episode_data.get("podcastTitle") and show.title != episode_data["podcastTitle"]:
                show.title = episode_data["podcastTitle"]
                show.save(update_fields=["title"])
            if episode_data.get("author") and show.author != episode_data["author"]:
                show.author = episode_data["author"]
                show.save(update_fields=["author"])
            
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

            # Check if we should process this media
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
            if runtime_minutes and item.runtime_minutes != runtime_minutes:
                item.runtime_minutes = runtime_minutes
                item.save(update_fields=["runtime_minutes"])

            # Get existing podcast or create new
            existing_podcast = self.existing_podcasts.get((episode_uuid, Sources.POCKETCASTS.value))
            
            # Extract progress data
            playing_status = episode_data.get("playingStatus", 0)  # 2=in-progress, 3=completed
            played_up_to = episode_data.get("playedUpTo", 0)  # in seconds
            duration_seconds = duration or 0
            
            # Calculate progress delta and determine status
            old_played_up_to = existing_podcast.played_up_to_seconds if existing_podcast else 0
            old_status = existing_podcast.last_seen_status if existing_podcast else None
            
            delta_seconds, new_status, progress_minutes = self._calculate_progress_delta(
                old_played_up_to,
                played_up_to,
                duration_seconds,
                playing_status,
                old_status,
            )

            # Create or update podcast entry
            if existing_podcast:
                # Update existing
                existing_podcast.item = item
                existing_podcast.show = show
                existing_podcast.episode = episode
                existing_podcast.status = new_status
                existing_podcast.progress = progress_minutes  # Store in minutes
                existing_podcast.played_up_to_seconds = played_up_to
                existing_podcast.last_seen_status = playing_status
                
                # Set end_date if completed
                if new_status == Status.COMPLETED.value and not existing_podcast.end_date:
                    existing_podcast.end_date = timezone.now()
                
                # Save to create history entry (Media.history tracks progress changes)
                existing_podcast.save()
                
                # Record history for delta time (create history entry manually)
                if delta_seconds > 0:
                    self._record_history(existing_podcast, delta_seconds, timezone.now())
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
                    start_date=timezone.now() if progress_minutes > 0 else None,
                    end_date=timezone.now() if new_status == Status.COMPLETED.value else None,
                    notes="Imported from Pocket Casts",
                )
                
                self.bulk_media[MediaTypes.PODCAST.value].append(podcast)
                
                # Store delta for history recording after bulk create
                # We'll record history after the podcast is saved
                if delta_seconds > 0:
                    if not hasattr(self, '_pending_history'):
                        self._pending_history = []
                    # Store episode_uuid and delta for lookup after bulk create
                    self._pending_history.append((episode_uuid, delta_seconds, timezone.now()))

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

