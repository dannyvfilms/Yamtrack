import logging

from django.db import IntegrityError
from django.utils import timezone

from app import providers
from app.models import TV, Item, MediaTypes, Movie, Sources, Status

logger = logging.getLogger(__name__)


class JellyseerrWebhookProcessor:
    """
    Processor for Jellyseerr webhook events.

    Expected payload keys (configure in Jellyseerr webhook JSON):
      - media_type: "movie" or "tv"
      - media_tmdbid: TMDB ID
      - media_status: UNKNOWN|PENDING|PROCESSING|PARTIALLY_AVAILABLE|AVAILABLE
      - requestedBy_username or notifyuser_username
    """

    VALID_JELLYSEERR_STATUSES = {
        "UNKNOWN",
        "PENDING",
        "PROCESSING",
        "PARTIALLY_AVAILABLE",
        "AVAILABLE",
    }

    def process_payload(self, payload, user):
        if not getattr(user, "jellyseerr_enabled", False):
            logger.debug(
                "Jellyseerr webhook ignored: user %s has jellyseerr_enabled=False",
                user.username,
            )
            return

        media_type = (payload.get("media_type") or "").strip().lower()
        if media_type not in ("movie", "tv"):
            logger.warning(
                "Jellyseerr webhook ignored: unsupported media_type=%r",
                media_type,
            )
            return

        raw_tmdb_id = payload.get("media_tmdbid")
        tmdb_id = self._coerce_int_string(raw_tmdb_id)
        if not tmdb_id:
            logger.warning(
                "Jellyseerr webhook ignored: missing/invalid media_tmdbid=%r",
                raw_tmdb_id,
            )
            return

        media_status = (payload.get("media_status") or "").strip().upper()
        if not media_status:
            logger.warning("Jellyseerr webhook ignored: missing media_status")
            return
        if media_status not in self.VALID_JELLYSEERR_STATUSES:
            logger.warning(
                "Jellyseerr webhook ignored: unknown media_status=%r",
                media_status,
            )
            return

        trigger_statuses = self._parse_csv_upper(
            getattr(user, "jellyseerr_trigger_statuses", ""),
        )
        if trigger_statuses:
            if media_status not in trigger_statuses:
                logger.debug(
                    "Jellyseerr webhook ignored: status %s not in trigger set %s",
                    media_status,
                    sorted(trigger_statuses),
                )
                return
        # Default behaviour: do not add at UNKNOWN unless user explicitly configures it.
        elif media_status == "UNKNOWN":
            logger.debug("Jellyseerr webhook ignored: default behaviour skips UNKNOWN")
            return

        requester = (
            (payload.get("requestedBy_username") or "").strip()
            or (payload.get("notifyuser_username") or "").strip()
        )
        allowed_requesters = self._parse_csv_lower(
            getattr(user, "jellyseerr_allowed_usernames", ""),
        )
        if allowed_requesters:
            if not requester:
                logger.debug(
                    "Jellyseerr webhook ignored: requester missing but allowlist configured",
                )
                return
            if requester.lower() not in allowed_requesters:
                logger.debug(
                    "Jellyseerr webhook ignored: requester %r not in allowlist %s",
                    requester,
                    sorted(allowed_requesters),
                )
                return

        desired_status = (
            getattr(user, "jellyseerr_default_added_status", None)
            or Status.PLANNING.value
        )
        if desired_status not in (Status.PLANNING.value, Status.IN_PROGRESS.value):
            desired_status = Status.PLANNING.value

        yamtrack_media_type = (
            MediaTypes.MOVIE.value if media_type == "movie" else MediaTypes.TV.value
        )

        logger.info(
            "Jellyseerr accepted: user=%s requester=%r type=%s tmdb=%s status=%s -> yamtrack_status=%s",
            user.username,
            requester,
            yamtrack_media_type,
            tmdb_id,
            media_status,
            desired_status,
        )

        item = self._get_or_create_tmdb_item(yamtrack_media_type, tmdb_id)
        if not item:
            return

        self._get_or_create_user_media(user, item, yamtrack_media_type, desired_status)

    def _get_or_create_tmdb_item(self, media_type, tmdb_id):
        try:
            metadata = providers.services.get_media_metadata(
                media_type,
                tmdb_id,
                Sources.TMDB.value,
            )
        except Exception as exc:
            logger.warning(
                "Jellyseerr: failed TMDB metadata for %s/%s: %s",
                media_type,
                tmdb_id,
                exc,
            )
            return None

        title = metadata.get("title") or f"TMDB {tmdb_id}"
        image = metadata.get("image") or "https://example.com/placeholder.jpg"

        try:
            item, created = Item.objects.get_or_create(
                media_id=str(tmdb_id),
                source=Sources.TMDB.value,
                media_type=media_type,
                defaults={"title": title, "image": image},
            )
        except IntegrityError as exc:
            logger.warning(
                "Jellyseerr: Item create race for %s/%s: %s",
                media_type,
                tmdb_id,
                exc,
            )
            try:
                item = Item.objects.get(
                    media_id=str(tmdb_id),
                    source=Sources.TMDB.value,
                    media_type=media_type,
                )
            except Item.DoesNotExist:
                return None
            created = False

        updates = []
        if not item.title and title:
            item.title = title
            updates.append("title")
        if not item.image and image:
            item.image = image
            updates.append("image")

        if updates:
            item.save(update_fields=updates)

        if created:
            logger.info("Jellyseerr: created Item %s/%s (%s)", media_type, tmdb_id, item.title)

        return item

    def _get_or_create_user_media(self, user, item, media_type, desired_status):
        defaults = {"status": desired_status}

        model = Movie if media_type == MediaTypes.MOVIE.value else TV
        model_fields = {field.name for field in model._meta.fields}

        if desired_status == Status.IN_PROGRESS.value and "start_date" in model_fields:
            defaults["start_date"] = timezone.now().replace(second=0, microsecond=0)

        try:
            media_obj, _created = model.objects.get_or_create(
                user=user,
                item=item,
                defaults=defaults,
            )

        except IntegrityError:
            media_obj = model.objects.filter(user=user, item=item).first()

        return media_obj

    @staticmethod
    def _coerce_int_string(value):
        if value is None:
            return None
        try:
            return str(int(str(value).strip()))
        except Exception:
            return None

    @staticmethod
    def _parse_csv_lower(value):
        if not value:
            return set()
        parts = [p.strip().lower() for p in str(value).split(",")]
        return {p for p in parts if p}

    @staticmethod
    def _parse_csv_upper(value):
        if not value:
            return set()
        parts = [p.strip().upper() for p in str(value).split(",")]
        return {p for p in parts if p}
