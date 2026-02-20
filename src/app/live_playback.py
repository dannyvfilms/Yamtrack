"""Cache-backed live playback state for webhook-driven now-playing UI."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from app.models import Item, MediaTypes, Sources

logger = logging.getLogger(__name__)

PLAYBACK_CACHE_PREFIX = "active_playback_v1"
PLAYBACK_CACHE_TIMEOUT_SECONDS = 6 * 60 * 60
PLAYBACK_HARD_STALE_SECONDS = 4 * 60 * 60
PLAYBACK_PAUSE_STALE_SECONDS = 45 * 60
PLAYBACK_SCROBBLE_BUFFER_SECONDS = 30        # small buffer after calculated end time
PLAYBACK_SCROBBLE_FALLBACK_SECONDS = 15 * 60  # fallback when duration unavailable
PLAYBACK_STOP_GRACE_SECONDS = 60

PLAYBACK_STATUS_PLAYING = "playing"
PLAYBACK_STATUS_PAUSED = "paused"
PLAYBACK_STATUS_STOPPED = "stopped"


def _cache_key(user_id: int) -> str:
    return f"{PLAYBACK_CACHE_PREFIX}:{user_id}"


def _coerce_int(value, default=None):
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _now_ts(now=None) -> int:
    current = now or timezone.now()
    return int(current.timestamp())


def _extract_ms_value(payload: dict, *keys) -> int | None:
    metadata = payload.get("Metadata", {}) or {}
    for key in keys:
        value = metadata.get(key)
        if value is None:
            value = payload.get(key)
        parsed = _coerce_int(value)
        if parsed is not None and parsed >= 0:
            return parsed
    return None


def _extract_duration_seconds(payload: dict) -> int | None:
    duration_ms = _extract_ms_value(payload, "duration", "Duration")
    if duration_ms is None:
        return None
    return max(0, duration_ms // 1000)


def _extract_offset_seconds(payload: dict) -> int | None:
    offset_ms = _extract_ms_value(payload, "viewOffset", "ViewOffset")
    if offset_ms is None:
        return None
    return max(0, offset_ms // 1000)


def _extract_episode_numbers(payload: dict) -> tuple[int | None, int | None]:
    metadata = payload.get("Metadata", {}) or {}
    season_number = _coerce_int(metadata.get("parentIndex"))
    episode_number = _coerce_int(metadata.get("index"))
    return season_number, episode_number


def set_user_playback_state(user_id: int, state: dict) -> None:
    """Write playback state dict to cache."""
    cache.set(
        _cache_key(user_id),
        state,
        timeout=PLAYBACK_CACHE_TIMEOUT_SECONDS,
    )


def clear_user_playback_state(user_id: int) -> None:
    """Remove playback state from cache."""
    cache.delete(_cache_key(user_id))


def _state_matches(
    state: dict | None,
    *,
    rating_key: str | None,
    media_id: str | None,
    playback_media_type: str | None,
) -> bool:
    if not state:
        return False

    stored_rating_key = str(state.get("rating_key") or "").strip()
    if rating_key and stored_rating_key:
        return stored_rating_key == rating_key

    stored_media_id = str(state.get("media_id") or "").strip()
    if media_id and stored_media_id:
        return (
            str(media_id).strip() == stored_media_id
            and state.get("media_type") == playback_media_type
        )

    return False


def apply_playback_event(  # noqa: C901, PLR0912
    *,
    user_id: int,
    event_type: str,
    playback_media_type: str | None,
    media_id: str | None = None,
    source: str = Sources.TMDB.value,
    rating_key: str | None = None,
    title: str | None = None,
    series_title: str | None = None,
    episode_title: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    view_offset_seconds: int | None = None,
    duration_seconds: int | None = None,
) -> None:
    """Update live playback cache state from a webhook event.

    All fields are pre-extracted by the caller (Plex / Jellyfin
    processor) so this function is source-agnostic.  Event types
    use the normalised ``media.*`` naming regardless of origin.
    """
    if playback_media_type not in (
        MediaTypes.MOVIE.value,
        MediaTypes.EPISODE.value,
    ):
        return

    if not event_type:
        return

    if event_type == "media.resume":
        event_type = "media.play"

    key = _cache_key(user_id)
    existing_state = cache.get(key)
    now_ts = _now_ts()

    if event_type == "media.stop":
        if existing_state and (_state_matches(
            existing_state,
            rating_key=rating_key,
            media_id=media_id,
            playback_media_type=playback_media_type,
        ) or not rating_key):
            # Grace period instead of immediate deletion — keeps the
            # card visible across auto-play transitions and brief gaps.
            existing_state["status"] = PLAYBACK_STATUS_STOPPED
            existing_state["stop_expires_at_ts"] = (
                now_ts + PLAYBACK_STOP_GRACE_SECONDS
            )
            set_user_playback_state(user_id, existing_state)
        return

    if event_type not in ("media.play", "media.pause", "media.scrobble"):
        return

    offset_seconds = view_offset_seconds
    dur_seconds = duration_seconds

    if _state_matches(
        existing_state,
        rating_key=rating_key,
        media_id=media_id,
        playback_media_type=playback_media_type,
    ):
        if offset_seconds is None:
            offset_seconds = _coerce_int(
                existing_state.get("view_offset_seconds"), 0,
            )
        if dur_seconds is None:
            dur_seconds = _coerce_int(
                existing_state.get("duration_seconds"), 0,
            )

    is_paused = event_type == "media.pause"
    status = PLAYBACK_STATUS_PAUSED if is_paused else PLAYBACK_STATUS_PLAYING

    state = {
        "event_type": event_type,
        "media_type": playback_media_type,
        "media_id": str(media_id) if media_id is not None else None,
        "source": source,
        "rating_key": rating_key or None,
        "title": title,
        "series_title": series_title,
        "episode_title": episode_title,
        "season_number": season_number,
        "episode_number": episode_number,
        "view_offset_seconds": max(0, offset_seconds or 0),
        "duration_seconds": max(0, dur_seconds or 0),
        "status": status,
        "updated_at_ts": now_ts,
        "expires_at_ts": now_ts + PLAYBACK_HARD_STALE_SECONDS,
        "pause_expires_at_ts": None,
        "scrobble_expires_at_ts": None,
    }

    if event_type == "media.pause":
        state["pause_expires_at_ts"] = now_ts + PLAYBACK_PAUSE_STALE_SECONDS
    elif event_type == "media.scrobble":
        dur = dur_seconds or 0
        off = offset_seconds or 0
        if dur > 0:
            remaining = max(0, dur - off)
            state["scrobble_expires_at_ts"] = (
                now_ts + remaining + PLAYBACK_SCROBBLE_BUFFER_SECONDS
            )
        else:
            state["scrobble_expires_at_ts"] = (
                now_ts + PLAYBACK_SCROBBLE_FALLBACK_SECONDS
            )

    set_user_playback_state(user_id, state)


def apply_plex_event(
    *,
    user_id: int,
    payload: dict,
    playback_media_type: str | None,
    media_id: str | None = None,
    source: str = Sources.TMDB.value,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> None:
    """Plex-specific wrapper: extract fields and delegate."""
    event_type = payload.get("event")
    if not event_type:
        return

    metadata = payload.get("Metadata", {}) or {}
    raw_rk = metadata.get("ratingKey") or metadata.get("ratingkey") or ""

    payload_season, payload_episode = _extract_episode_numbers(payload)

    apply_playback_event(
        user_id=user_id,
        event_type=event_type,
        playback_media_type=playback_media_type,
        media_id=media_id,
        source=source,
        rating_key=str(raw_rk).strip() or None,
        title=metadata.get("title"),
        series_title=metadata.get("grandparentTitle"),
        episode_title=(
            metadata.get("title")
            if playback_media_type == MediaTypes.EPISODE.value
            else None
        ),
        season_number=(
            season_number if season_number is not None
            else payload_season
        ),
        episode_number=(
            episode_number if episode_number is not None
            else payload_episode
        ),
        view_offset_seconds=_extract_offset_seconds(payload),
        duration_seconds=_extract_duration_seconds(payload),
    )


def _estimate_progress_seconds(state: dict, now_ts: int) -> int:
    offset_seconds = max(0, _coerce_int(state.get("view_offset_seconds"), 0))
    duration_seconds = max(0, _coerce_int(state.get("duration_seconds"), 0))
    updated_at_ts = _coerce_int(state.get("updated_at_ts"), now_ts)
    status = state.get("status")

    estimated = offset_seconds
    if status == PLAYBACK_STATUS_PLAYING:
        estimated += max(0, now_ts - updated_at_ts)

    if duration_seconds:
        estimated = min(estimated, duration_seconds)
    return max(0, estimated)


def get_user_playback_state(user_id: int, now=None) -> dict | None:
    """Return active playback state if it is still valid."""
    state = cache.get(_cache_key(user_id))
    if not state:
        return None

    now_ts = _now_ts(now)
    expires_at_ts = _coerce_int(state.get("expires_at_ts"), 0)
    if expires_at_ts and now_ts >= expires_at_ts:
        clear_user_playback_state(user_id)
        return None

    if state.get("status") == PLAYBACK_STATUS_PAUSED:
        pause_expires_at_ts = _coerce_int(state.get("pause_expires_at_ts"), 0)
        if pause_expires_at_ts and now_ts >= pause_expires_at_ts:
            clear_user_playback_state(user_id)
            return None

    if state.get("status") == PLAYBACK_STATUS_STOPPED:
        stop_expires_at_ts = _coerce_int(state.get("stop_expires_at_ts"), 0)
        if stop_expires_at_ts and now_ts >= stop_expires_at_ts:
            clear_user_playback_state(user_id)
            return None

    scrobble_expires_at_ts = _coerce_int(state.get("scrobble_expires_at_ts"), 0)
    if scrobble_expires_at_ts and now_ts >= scrobble_expires_at_ts:
        clear_user_playback_state(user_id)
        return None

    state_copy = dict(state)
    state_copy["estimated_progress_seconds"] = _estimate_progress_seconds(
        state_copy, now_ts,
    )
    return state_copy


def _format_clock(total_seconds: int) -> str:
    seconds = max(0, int(total_seconds or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _resolve_state_item(state: dict):
    """Look up a DB Item matching the playback state."""
    media_id = str(state.get("media_id") or "").strip()
    source = state.get("source") or Sources.TMDB.value
    playback_media_type = state.get("media_type")

    if not media_id:
        return None

    if playback_media_type == MediaTypes.MOVIE.value:
        return Item.objects.filter(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.MOVIE.value,
        ).first()

    if playback_media_type == MediaTypes.EPISODE.value:
        season_number = _coerce_int(state.get("season_number"))
        episode_number = _coerce_int(state.get("episode_number"))

        if season_number is not None and episode_number is not None:
            episode_item = Item.objects.filter(
                media_id=media_id,
                source=source,
                media_type=MediaTypes.EPISODE.value,
                season_number=season_number,
                episode_number=episode_number,
            ).first()
            if episode_item:
                return episode_item

        tv_item = Item.objects.filter(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.TV.value,
        ).first()
        if tv_item:
            return tv_item

        if season_number is not None:
            return Item.objects.filter(
                media_id=media_id,
                source=source,
                media_type=MediaTypes.SEASON.value,
                season_number=season_number,
            ).first()

    return None


def _slugify_title(title: str, media_id: str | None = None) -> str:
    """Slugify a title, matching the template ``slug`` filter behaviour."""
    from unidecode import unidecode  # noqa: PLC0415
    from urllib.parse import quote  # noqa: PLC0415

    cleaned = slugify(title)
    if not cleaned:
        cleaned = slugify(quote(unidecode(title), safe=""))
    if not cleaned:
        fallback = str(media_id) if media_id else "item"
        cleaned = slugify(fallback) or "item"
    return cleaned


def _build_details_url(state: dict) -> str:
    """Build a URL to the media details page for the playing item."""
    media_id = state.get("media_id")
    source = state.get("source") or Sources.TMDB.value
    playback_media_type = state.get("media_type")
    if not media_id:
        return reverse("home")

    title = (state.get("series_title") or state.get("title") or "").strip()
    slug_title = _slugify_title(title, media_id)

    if playback_media_type == MediaTypes.EPISODE.value:
        season_number = _coerce_int(state.get("season_number"))
        if season_number is not None:
            return reverse(
                "season_details",
                kwargs={
                    "source": source,
                    "media_id": media_id,
                    "title": slug_title,
                    "season_number": season_number,
                },
            )
        # No season number — fall back to TV show details
        return reverse(
            "media_details",
            kwargs={
                "source": source,
                "media_type": MediaTypes.TV.value,
                "media_id": media_id,
                "title": slug_title,
            },
        )

    return reverse(
        "media_details",
        kwargs={
            "source": source,
            "media_type": playback_media_type or MediaTypes.MOVIE.value,
            "media_id": media_id,
            "title": slug_title,
        },
    )


def _resolve_card_title(state, state_item):
    """Resolve the main display title from playback state."""
    media_type = state.get("media_type")
    if media_type == MediaTypes.EPISODE.value:
        title = (state.get("series_title") or "").strip()
    else:
        title = (state.get("title") or "").strip()
    if not title and state_item:
        title = state_item.title
    return title or "Now Playing"


def _resolve_card_subtitle(state, title):
    """Resolve the subtitle line (episode code + title) for episodes."""
    if state.get("media_type") != MediaTypes.EPISODE.value:
        return None, None
    season_number = _coerce_int(state.get("season_number"))
    episode_number = _coerce_int(state.get("episode_number"))
    episode_code = None
    if season_number is not None and episode_number is not None:
        episode_code = f"S{season_number:02d}E{episode_number:02d}"
    episode_title = (state.get("episode_title") or "").strip()
    if episode_code and episode_title and episode_title != title:
        return episode_code, f"{episode_code} • {episode_title}"
    if episode_code:
        return episode_code, episode_code
    if episode_title and episode_title != title:
        return None, episode_title
    return episode_code, None


def _resolve_progress(state, state_item):
    """Compute duration, progress, display string, and percent."""
    duration = max(0, _coerce_int(state.get("duration_seconds"), 0))
    if not duration and state_item and state_item.runtime_minutes:
        duration = max(0, int(state_item.runtime_minutes) * 60)
    progress = max(0, _coerce_int(
        state.get("estimated_progress_seconds"), 0,
    ))
    if duration:
        progress = min(progress, duration)
    if duration:
        display = f"{_format_clock(progress)} / {_format_clock(duration)}"
        percent = round((progress / duration) * 100, 2)
    else:
        display = _format_clock(progress)
        percent = 0
    return duration, progress, display, percent


def _resolve_show_media_id(state, state_item, source):
    """Resolve the TV show-level TMDB ID for backdrop fallback."""
    state_media_id = str(state.get("media_id") or "").strip()

    if state_item:
        return str(state_item.media_id or "").strip()

    # state_item is None — try finding the TV show by series_title.
    series_title = (state.get("series_title") or "").strip()
    if series_title:
        tv_item = Item.objects.filter(
            title=series_title,
            source=source,
            media_type=MediaTypes.TV.value,
        ).first()
        if tv_item:
            return str(tv_item.media_id or "").strip()

    return state_media_id


def _fetch_episode_still(show_id, season_number, episode_number):
    """Fetch episode still image from TMDB episode API."""
    try:
        from app.providers import tmdb  # noqa: PLC0415

        ep_data = tmdb.episode(show_id, season_number, episode_number)
        image = ep_data.get("image")
        if image and image != settings.IMG_NONE:
            return image
    except Exception:  # noqa: BLE001, S110
        pass
    return None


def _resolve_landscape_image(state, state_item):  # noqa: C901
    """Resolve a landscape image for the playback card.

    For episodes: prefers the episode-specific still (the same image
    shown on the media-details page) over the TV show backdrop.
    For movies: uses the TMDB movie backdrop.
    """
    media_type = state.get("media_type")
    source = state.get("source") or Sources.TMDB.value

    # ── Episode: prefer the episode still ──────────────────────
    if (
        media_type == MediaTypes.EPISODE.value
        and source == Sources.TMDB.value
    ):
        # 1. Episode Item already in DB → use its stored still
        if (
            state_item
            and state_item.image
            and state_item.image != settings.IMG_NONE
        ):
            return state_item.image

        # 2. Fetch the episode still from TMDB episode API
        show_id = _resolve_show_media_id(state, state_item, source)
        season = _coerce_int(state.get("season_number"))
        episode = _coerce_int(state.get("episode_number"))
        if show_id and season is not None and episode is not None:
            still = _fetch_episode_still(show_id, season, episode)
            if still:
                return still

        # 3. Fall back to TV show backdrop
        if show_id:
            try:
                from lists.models import CustomList  # noqa: PLC0415

                backdrop = CustomList()._get_tmdb_backdrop(
                    MediaTypes.TV.value, show_id,
                )
                if backdrop and backdrop != settings.IMG_NONE:
                    return backdrop
            except Exception:  # noqa: BLE001, S110
                pass

        return settings.IMG_NONE

    # ── Movie / other: use backdrop ────────────────────────────
    media_id = str(state.get("media_id") or "").strip()
    if media_id and source == Sources.TMDB.value:
        try:
            from lists.models import CustomList  # noqa: PLC0415

            lookup_type = media_type
            if media_type in (
                MediaTypes.EPISODE.value,
                MediaTypes.SEASON.value,
            ):
                lookup_type = MediaTypes.TV.value

            backdrop = CustomList()._get_tmdb_backdrop(
                lookup_type, media_id,
            )
            if backdrop and backdrop != settings.IMG_NONE:
                return backdrop
        except Exception:  # noqa: BLE001, S110
            pass

    if state_item and state_item.image:
        return state_item.image

    return settings.IMG_NONE


def build_home_playback_card(user) -> dict | None:
    """Build template context for the home-page live playback card."""
    state = get_user_playback_state(user.id)
    if not state:
        return None

    state_item = _resolve_state_item(state)
    title = _resolve_card_title(state, state_item)
    episode_code, subtitle = _resolve_card_subtitle(state, title)
    duration_seconds, _, progress_display, progress_percent = (
        _resolve_progress(state, state_item)
    )

    image = _resolve_landscape_image(state, state_item)

    status = state.get("status") or PLAYBACK_STATUS_PLAYING

    episode_title = (state.get("episode_title") or "").strip() or None

    return {
        "title": title,
        "subtitle": subtitle,
        "episode_code": episode_code,
        "episode_title": episode_title,
        "image": image,
        "status": status,
        "status_label": (
            "Paused"
            if status in (PLAYBACK_STATUS_PAUSED, PLAYBACK_STATUS_STOPPED)
            else "Playing"
        ),
        "details_url": _build_details_url(state),
        "progress_display": progress_display,
        "progress_percent": progress_percent,
        "offset_seconds": max(0, _coerce_int(state.get("view_offset_seconds"), 0)),
        "duration_seconds": duration_seconds,
        "updated_at_ts": _coerce_int(state.get("updated_at_ts"), _now_ts()),
    }
