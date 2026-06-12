"""Import podcast playback history from GPodder-compatible sync servers."""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify

from app import history_cache
from app.models import (
    Item,
    MediaTypes,
    Podcast,
    PodcastEpisode,
    PodcastShow,
    PodcastShowTracker,
    Sources,
    Status,
)
from integrations import gpodder_api, models as integration_models, podcast_rss
from integrations.imports.helpers import MediaImportError, decrypt

logger = logging.getLogger(__name__)


def importer(identifier, user, mode):
    """Import podcast history from a GPodder-compatible server."""
    gpodder_importer = GPodderImporter(user, mode)
    return gpodder_importer.import_data()


class GPodderImporter:
    """Importer for GPodder-compatible podcast history."""

    def __init__(self, user, mode):
        self.user = user
        self.mode = mode
        self.warnings = []
        try:
            self.account = user.gpodder_account
        except integration_models.GPodderAccount.DoesNotExist as exc:
            raise MediaImportError("GPodder account not connected.") from exc

        self.credentials = gpodder_api.GPodderCredentials(
            server_url=decrypt(self.account.server_url),
            username=decrypt(self.account.username),
            password=decrypt(self.account.password),
        )
        self._seen_fingerprints = set()
        self._episode_cache = {}
        self._show_cache = {}

    def import_data(self):
        """Run the end-to-end GPodder import."""
        try:
            gpodder_api.verify_login(self.credentials)
            self.account.connection_broken = False
            self.account.last_error_message = ""
            self.account.save(update_fields=["connection_broken", "last_error_message", "updated_at"])
        except gpodder_api.GPodderAuthError as exc:
            self.account.connection_broken = True
            self.account.last_error_message = str(exc)[:500]
            self.account.save(update_fields=["connection_broken", "last_error_message", "updated_at"])
            raise MediaImportError(str(exc)) from exc
        except gpodder_api.GPodderClientError as exc:
            self.account.last_error_message = str(exc)[:500]
            self.account.save(update_fields=["last_error_message", "updated_at"])
            raise MediaImportError(str(exc)) from exc

        try:
            gpodder_api.register_device(self.credentials, self.account.device_id)
        except gpodder_api.GPodderError as exc:
            logger.info("Skipping GPodder device registration for user %s: %s", self.user.username, exc)

        subscriptions = self._load_subscriptions()
        actions, next_cursor = gpodder_api.fetch_episode_actions(
            self.credentials,
            since=self.account.episode_actions_since,
            device=self.account.device_filter,
        )

        imported_counts = defaultdict(int)
        sorted_actions = sorted(actions, key=lambda action: self._parse_action_timestamp(action) or timezone.now())
        for action in sorted_actions:
            if action.get("action") != "play":
                continue
            if not self._has_listening_activity(action):
                continue
            if self._is_duplicate_action(action):
                continue
            processed = self._process_action(action, subscriptions)
            if processed:
                imported_counts[MediaTypes.PODCAST.value] += 1

        self.account.episode_actions_since = next_cursor
        self.account.last_sync_at = timezone.now()
        self.account.connection_broken = False
        self.account.last_error_message = ""
        self.account.save(
            update_fields=[
                "episode_actions_since",
                "last_sync_at",
                "connection_broken",
                "last_error_message",
                "updated_at",
            ],
        )

        history_cache.invalidate_history_cache(self.user.id, force=True)
        from app import statistics_cache

        statistics_cache.schedule_all_ranges_refresh(self.user.id)
        return dict(imported_counts), self.warnings

    def _load_subscriptions(self):
        """Return subscription metadata keyed by normalized feed URL."""
        subscriptions = {}
        feed_urls = gpodder_api.fetch_subscriptions(self.credentials)
        for raw_feed_url in feed_urls:
            normalized_feed = gpodder_api.normalize_external_url(raw_feed_url)
            if not normalized_feed:
                continue

            show = PodcastShow.objects.filter(rss_feed_url=raw_feed_url).first()
            if show is None:
                show = PodcastShow.objects.filter(rss_feed_url=normalized_feed).first()

            rss_metadata = {}
            rss_episodes = []
            try:
                rss_metadata = podcast_rss.fetch_show_metadata_from_rss(raw_feed_url)
                rss_episodes = podcast_rss.fetch_episodes_from_rss(raw_feed_url)
            except Exception as exc:  # noqa: BLE001
                self.warnings.append(f"Failed to refresh RSS feed {raw_feed_url}: {exc}")

            show = self._ensure_show(raw_feed_url, normalized_feed, rss_metadata, show=show)
            episode_map = {}
            for rss_episode in rss_episodes:
                episode = self._ensure_episode(show, rss_episode)
                for candidate in self._episode_candidates(rss_episode):
                    episode_map[candidate] = episode

            subscriptions[normalized_feed] = {
                "feed_url": raw_feed_url,
                "show": show,
                "episode_map": episode_map,
            }
        return subscriptions

    def _ensure_show(self, raw_feed_url, normalized_feed, rss_metadata, *, show=None):
        """Create or update a podcast show for a feed URL."""
        show = show or PodcastShow.objects.filter(rss_feed_url=raw_feed_url).first()
        if show is None:
            show = PodcastShow.objects.filter(rss_feed_url=normalized_feed).first()
        if show is None:
            show = PodcastShow.objects.filter(podcast_uuid=self._show_key(normalized_feed)).first()

        defaults = {
            "source": Sources.GPODDER.value,
            "title": rss_metadata.get("title") or slugify(normalized_feed).replace("-", " ")[:255] or "Podcast",
            "slug": slugify(rss_metadata.get("title") or normalized_feed)[:255],
            "author": rss_metadata.get("author", "")[:255],
            "description": rss_metadata.get("description", ""),
            "language": rss_metadata.get("language", "")[:10],
            "rss_feed_url": raw_feed_url,
        }

        if show is None:
            show = PodcastShow.objects.create(
                podcast_uuid=self._show_key(normalized_feed),
                **defaults,
            )
        else:
            update_fields = []
            for field, value in defaults.items():
                if value and getattr(show, field) != value:
                    setattr(show, field, value)
                    update_fields.append(field)
            if show.source != Sources.GPODDER.value:
                show.source = Sources.GPODDER.value
                update_fields.append("source")
            if update_fields:
                show.save(update_fields=update_fields)
        self._show_cache[normalized_feed] = show
        return show

    def _ensure_episode(self, show, rss_episode, *, fallback_audio_url="", fallback_duration=None, fallback_time=None):
        """Create or update a podcast episode using RSS data or action fallbacks."""
        audio_url = rss_episode.get("audio_url") or fallback_audio_url
        guid = rss_episode.get("guid") or audio_url or hashlib.md5(show.podcast_uuid.encode()).hexdigest()

        episode = PodcastEpisode.objects.filter(show=show, episode_uuid=guid).first()
        if episode is None and audio_url:
            episode = PodcastEpisode.objects.filter(show=show, audio_url=audio_url).first()

        title = rss_episode.get("title") or "Unknown Episode"
        published = rss_episode.get("published") or fallback_time
        duration = rss_episode.get("duration") or fallback_duration

        if episode is None:
            episode = PodcastEpisode.objects.create(
                show=show,
                episode_uuid=guid,
                title=title[:500],
                slug=slugify(title)[:255],
                published=published,
                duration=duration,
                audio_url=audio_url,
                episode_number=rss_episode.get("episode_number"),
                season_number=rss_episode.get("season_number"),
            )
        else:
            update_fields = []
            updates = {
                "title": title[:500],
                "slug": slugify(title)[:255],
                "published": published,
                "duration": duration,
                "audio_url": audio_url,
                "episode_number": rss_episode.get("episode_number"),
                "season_number": rss_episode.get("season_number"),
            }
            for field, value in updates.items():
                if value is not None and value != "" and getattr(episode, field) != value:
                    setattr(episode, field, value)
                    update_fields.append(field)
            if update_fields:
                episode.save(update_fields=update_fields)
        return episode

    def _process_action(self, action, subscriptions):
        """Apply a single GPodder play action to Yamtrack history."""
        action_time = self._parse_action_timestamp(action)
        if action_time is None:
            return False

        normalized_feed = gpodder_api.normalize_external_url(action.get("podcast"))
        subscription = subscriptions.get(normalized_feed)
        if subscription is None:
            fallback_show = self._ensure_show(
                action.get("podcast") or normalized_feed,
                normalized_feed,
                {},
                show=self._show_cache.get(normalized_feed),
            )
            subscription = {"feed_url": action.get("podcast") or normalized_feed, "show": fallback_show, "episode_map": {}}
            subscriptions[normalized_feed] = subscription

        show = subscription["show"]
        total_seconds = self._coerce_int(action.get("total"))
        position_seconds = self._coerce_int(action.get("position"))
        if total_seconds <= 0:
            total_seconds = None

        episode = None
        for candidate in gpodder_api.action_candidates(action):
            episode = subscription["episode_map"].get(candidate)
            if episode is not None:
                break
        if episode is None:
            episode = self._ensure_episode(
                show,
                {},
                fallback_audio_url=action.get("episode", ""),
                fallback_duration=total_seconds,
                fallback_time=action_time,
            )
            for candidate in gpodder_api.action_candidates(action):
                subscription["episode_map"][candidate] = episode

        item = self._ensure_item(show, episode, total_seconds)
        PodcastShowTracker.objects.get_or_create(
            user=self.user,
            show=show,
            defaults={"status": Status.IN_PROGRESS.value},
        )

        is_completed = self._is_completed(position_seconds, total_seconds)
        latest_entries = list(
            Podcast.objects.filter(user=self.user, item=item)
            .select_related("episode", "show", "item")
            .order_by("-created_at")
        )
        latest_in_progress = next((entry for entry in latest_entries if entry.end_date is None), None)
        latest_completed = next((entry for entry in latest_entries if entry.end_date is not None), None)

        status_value = Status.COMPLETED.value if is_completed else Status.IN_PROGRESS.value
        provider_status = 3 if is_completed else 2
        progress_minutes = max(1, position_seconds // 60) if position_seconds > 0 else 0

        if latest_in_progress is not None:
            if (
                latest_in_progress.played_up_to_seconds == position_seconds
                and latest_in_progress.end_date == (action_time if is_completed else None)
            ):
                return False
            self._update_podcast_row(
                latest_in_progress,
                show=show,
                episode=episode,
                status=status_value,
                progress_minutes=progress_minutes,
                position_seconds=position_seconds,
                provider_status=provider_status,
                action_time=action_time if is_completed else None,
            )
            return True

        if latest_completed is not None and is_completed:
            if self._is_duplicate_completion(latest_completed, position_seconds, action_time):
                return False

        if latest_completed is not None and not is_completed and latest_completed.end_date and action_time <= latest_completed.end_date:
            return False

        Podcast.objects.create(
            user=self.user,
            item=item,
            show=show,
            episode=episode,
            status=status_value,
            progress=progress_minutes,
            played_up_to_seconds=position_seconds or None,
            last_seen_status=provider_status,
            end_date=action_time if is_completed else None,
        )
        return True

    def _ensure_item(self, show, episode, total_seconds):
        """Return the trackable item for a podcast episode."""
        runtime_minutes = None
        if total_seconds:
            runtime_minutes = max(1, total_seconds // 60)
        elif episode.duration:
            runtime_minutes = max(1, episode.duration // 60)

        defaults = {
            "title": episode.title,
            "image": show.image or settings.IMG_NONE,
        }
        if runtime_minutes:
            defaults["runtime_minutes"] = runtime_minutes
        if episode.published:
            defaults["release_datetime"] = episode.published

        item, _ = Item.objects.get_or_create(
            media_id=episode.episode_uuid,
            source=Sources.GPODDER.value,
            media_type=MediaTypes.PODCAST.value,
            defaults=defaults,
        )
        update_fields = []
        if item.title != episode.title:
            item.title = episode.title
            update_fields.append("title")
        if runtime_minutes and item.runtime_minutes != runtime_minutes:
            item.runtime_minutes = runtime_minutes
            update_fields.append("runtime_minutes")
        if episode.published and item.release_datetime != episode.published:
            item.release_datetime = episode.published
            update_fields.append("release_datetime")
        if update_fields:
            item.save(update_fields=update_fields)
        return item

    def _update_podcast_row(
        self,
        podcast,
        *,
        show,
        episode,
        status,
        progress_minutes,
        position_seconds,
        provider_status,
        action_time,
    ):
        """Update an existing in-progress podcast row."""
        update_fields = []
        updates = {
            "show": show,
            "episode": episode,
            "status": status,
            "progress": progress_minutes,
            "played_up_to_seconds": position_seconds or None,
            "last_seen_status": provider_status,
        }
        if action_time is not None:
            updates["end_date"] = action_time

        for field, value in updates.items():
            if getattr(podcast, field) != value:
                setattr(podcast, field, value)
                update_fields.append(field)

        if update_fields:
            podcast.save(update_fields=update_fields)

    def _episode_candidates(self, rss_episode):
        candidates = set()
        for key in ("audio_url", "guid"):
            candidate = gpodder_api.normalize_external_url(rss_episode.get(key))
            if candidate:
                candidates.add(candidate)
        return candidates

    def _show_key(self, normalized_feed):
        return f"gp_{hashlib.md5(normalized_feed.encode()).hexdigest()}"

    def _parse_action_timestamp(self, action):
        timestamp = action.get("timestamp")
        if not timestamp:
            return None
        parsed = parse_datetime(timestamp)
        if parsed is None:
            return None
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed

    def _has_listening_activity(self, action):
        position = self._coerce_int(action.get("position"))
        total = self._coerce_int(action.get("total"))
        return position > 0 or self._is_completed(position, total)

    def _is_completed(self, position_seconds, total_seconds):
        if total_seconds is None or total_seconds <= 0 or position_seconds <= 0:
            return False
        significant_progress = position_seconds > 60 or position_seconds > total_seconds * 0.1
        return significant_progress and position_seconds >= total_seconds - 5

    def _is_duplicate_action(self, action):
        timestamp = action.get("timestamp", "")
        fingerprint = "|".join(
            [
                str(self.user.id),
                gpodder_api.normalize_external_url(action.get("podcast")),
                gpodder_api.normalize_external_url(action.get("episode")),
                timestamp,
                str(self._coerce_int(action.get("position"))),
            ]
        )
        if fingerprint in self._seen_fingerprints:
            return True
        self._seen_fingerprints.add(fingerprint)
        return False

    def _is_duplicate_completion(self, podcast, position_seconds, action_time):
        if podcast.end_date is None:
            return False
        if abs((action_time - podcast.end_date).total_seconds()) >= 300:
            return False
        return (podcast.played_up_to_seconds or 0) == (position_seconds or 0)

    def _coerce_int(self, value):
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0
