"""History highlight card building: first/last play, today-in-history, today releases."""

import logging
import random
from collections import defaultdict
from datetime import date, datetime

from django.apps import apps
from django.conf import settings
from django.core.cache import cache
from django.db.models.functions import ExtractDay, ExtractMonth
from django.utils import timezone

from app import history_cache
from app import statistics as stats
from app.models import MediaTypes, Sources

logger = logging.getLogger(__name__)


def _history_entry_card_payload(entry):
    if not entry:
        return None
    item = entry.get("item")
    if not item:
        return None
    played_at = entry.get("played_at_local")
    if isinstance(played_at, str):
        try:
            played_at = datetime.fromisoformat(played_at)
            if timezone.is_naive(played_at):
                played_at = timezone.make_aware(
                    played_at,
                    timezone.get_current_timezone(),
                )
        except ValueError:
            played_at = None
    fallback_image = entry.get("poster") or getattr(item, "image", "")
    title = entry.get("display_title") or entry.get("title")
    if not title and isinstance(item, dict):
        title = (
            item.get("localized_title")
            or item.get("title")
            or item.get("original_title")
        )
    elif not title:
        title = (
            getattr(item, "localized_title", None)
            or getattr(item, "title", None)
            or getattr(item, "original_title", None)
        )
    if not title:
        show = entry.get("show")
        if isinstance(show, dict):
            title = show.get("title")
        else:
            title = getattr(show, "title", None)
    if not title:
        album = entry.get("album")
        if isinstance(album, dict):
            title = album.get("title")
        else:
            title = getattr(album, "title", None)
    if not title:
        media_type = entry.get("media_type") or getattr(item, "media_type", None)
        title = {
            MediaTypes.MOVIE.value: "Movie",
            MediaTypes.TV.value: "TV Show",
            MediaTypes.EPISODE.value: "Episode",
            MediaTypes.GAME.value: "Game",
            MediaTypes.BOARDGAME.value: "Board Game",
            MediaTypes.MUSIC.value: "Music",
            MediaTypes.PODCAST.value: "Podcast",
            MediaTypes.BOOK.value: "Book",
            MediaTypes.COMIC.value: "Comic",
            MediaTypes.MANGA.value: "Manga",
            MediaTypes.ANIME.value: "Anime",
        }.get(media_type, "Item")
    return {
        "entry": entry,
        "item": item,
        "media_type": entry.get("media_type") or getattr(item, "media_type", None),
        "title": title,
        "image": _get_horizontal_history_image(item, fallback_image, allow_network=True),
        "played_at": played_at,
    }


def _cached_horizontal_backdrop(item) -> str | None:
    """Return a cached horizontal backdrop without triggering provider lookups."""
    if not item:
        return None

    if isinstance(item, dict):
        source = item.get("source")
        media_type = item.get("media_type")
        media_id = item.get("media_id")
    else:
        source = getattr(item, "source", None)
        media_type = getattr(item, "media_type", None)
        media_id = getattr(item, "media_id", None)

    if not source or not media_type or not media_id:
        return None

    if source == Sources.TMDB.value:
        backdrop_media_type = media_type
        if media_type in (MediaTypes.EPISODE.value, MediaTypes.SEASON.value, MediaTypes.ANIME.value):
            backdrop_media_type = MediaTypes.TV.value
        if backdrop_media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
            cached_backdrop = cache.get(f"tmdb_backdrop_{backdrop_media_type}_{media_id}")
            if cached_backdrop and cached_backdrop != settings.IMG_NONE:
                return cached_backdrop

    if source == Sources.TVDB.value and media_type in (
        MediaTypes.TV.value,
        MediaTypes.SEASON.value,
        MediaTypes.EPISODE.value,
        MediaTypes.ANIME.value,
    ):
        ext_ids = (
            item.get("provider_external_ids") if isinstance(item, dict)
            else getattr(item, "provider_external_ids", None)
        ) or {}
        tmdb_id = ext_ids.get("tmdb_id")
        if tmdb_id:
            cached_backdrop = cache.get(f"tmdb_backdrop_tv_{tmdb_id}")
            if cached_backdrop and cached_backdrop != settings.IMG_NONE:
                return cached_backdrop

    if source == Sources.IGDB.value and media_type == MediaTypes.GAME.value:
        cached_backdrop = cache.get(f"igdb_backdrop_{media_id}")
        if cached_backdrop and cached_backdrop != settings.IMG_NONE:
            return cached_backdrop

    return None


def _get_horizontal_history_image(item, fallback_image, *, allow_network=True):
    """Prefer horizontal artwork when available, matching list hub behavior."""
    if not item:
        return fallback_image or settings.IMG_NONE

    # Handle both dict (serialized) and model instance
    if isinstance(item, dict):
        image = fallback_image or item.get("image", "")
        source = item.get("source")
        media_type = item.get("media_type")
        media_id = item.get("media_id")
    else:
        image = fallback_image or getattr(item, "image", "")
        source = getattr(item, "source", None)
        media_type = getattr(item, "media_type", None)
        media_id = getattr(item, "media_id", None)

    if not source or not media_type or not media_id:
        return image or settings.IMG_NONE

    cached_backdrop = _cached_horizontal_backdrop(item)
    if cached_backdrop:
        return cached_backdrop

    if not allow_network:
        return image or settings.IMG_NONE

    try:
        from lists.models import CustomList
    except Exception:
        return image or settings.IMG_NONE

    # For episodes/seasons/anime, use the TV show's media_id to get the backdrop.
    # Episodes/seasons share the same media_id as their TV show; TMDB stores
    # anime as TV shows, so anime items (source=tmdb, media_type=anime) are
    # fetched via the /tv/{id} endpoint too.
    if source == Sources.TMDB.value and media_type in (
        MediaTypes.EPISODE.value,
        MediaTypes.SEASON.value,
        MediaTypes.ANIME.value,
    ):
        try:
            backdrop_url = CustomList()._get_tmdb_backdrop(MediaTypes.TV.value, media_id)
            if backdrop_url and backdrop_url != settings.IMG_NONE:
                return backdrop_url
        except Exception:
            pass

    if source == Sources.TMDB.value and media_type in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
        try:
            backdrop_url = CustomList()._get_tmdb_backdrop(media_type, media_id)
            if backdrop_url and backdrop_url != settings.IMG_NONE:
                return backdrop_url
        except Exception:
            pass

    # TVDB items store a tmdb_id cross-reference in provider_external_ids
    # (populated by the TVDB provider when it finds a TMDB match).
    # TV, Season, Episode, and Anime are all valid TVDB types.
    if source == Sources.TVDB.value and media_type in (
        MediaTypes.TV.value,
        MediaTypes.SEASON.value,
        MediaTypes.EPISODE.value,
        MediaTypes.ANIME.value,
    ):
        ext_ids = (
            item.get("provider_external_ids") if isinstance(item, dict)
            else getattr(item, "provider_external_ids", None)
        ) or {}
        tmdb_id = ext_ids.get("tmdb_id")
        if tmdb_id:
            try:
                backdrop_url = CustomList()._get_tmdb_backdrop(MediaTypes.TV.value, tmdb_id)
                if backdrop_url and backdrop_url != settings.IMG_NONE:
                    return backdrop_url
            except Exception:
                pass

    if source == Sources.IGDB.value and media_type == MediaTypes.GAME.value:
        try:
            backdrop_url = CustomList()._get_igdb_backdrop(media_id)
            if backdrop_url and backdrop_url != settings.IMG_NONE:
                return backdrop_url
        except Exception:
            pass

    return image or settings.IMG_NONE


def _normalize_history_highlight_images(history_highlights):
    """Ensure highlight cards prefer horizontal artwork, even for cached payloads."""
    if not isinstance(history_highlights, dict):
        return

    for key in ("first_play", "last_play", "today_in_history", "today_in_user_history"):
        entry = history_highlights.get(key)
        if not isinstance(entry, dict):
            continue
        fallback = entry.get("image") or entry.get("poster")
        entry["image"] = _get_horizontal_history_image(
            entry.get("item"),
            fallback,
            allow_network=True,
        )


def _select_history_entry_for_day(day_payload, pick_earliest=False, pick_latest=False):
    if not day_payload:
        return None
    entries = day_payload.get("entries") or []
    if not entries:
        return None
    if pick_earliest:
        entry = entries[-1]
    elif pick_latest:
        entry = entries[0]
    else:
        entry = random.choice(entries)
    return _history_entry_card_payload(entry)


def _get_today_history_entries(user):
    today = timezone.localdate()
    day_keys = _get_history_index_days(user)
    matching_dates = []
    for day_key in day_keys:
        try:
            day_date = date.fromisoformat(day_key)
        except ValueError:
            continue
        if day_date.month == today.month and day_date.day == today.day:
            matching_dates.append(day_date)

    if not matching_dates:
        return None, None

    available_years = sorted({day_date.year for day_date in matching_dates})
    selected_year = random.choice(available_years)
    year_dates = [day_date for day_date in matching_dates if day_date.year == selected_year]
    selected_date = random.choice(year_dates) if year_dates else None
    if not selected_date:
        return None, None

    day_payload = _get_history_day_payload(user, selected_date)
    return _select_history_entry_for_day(day_payload), selected_year


def _get_today_release_entry(user):
    today = timezone.localdate()
    active_types = list(getattr(user, "get_active_media_types", lambda: [])())
    if not active_types:
        active_types = list(MediaTypes.values)
    include_podcasts = MediaTypes.PODCAST.value in active_types
    active_types = [
        media_type
        for media_type in active_types
        if media_type not in (MediaTypes.EPISODE.value, MediaTypes.PODCAST.value)
    ]

    items_by_year = defaultdict(list)
    seen_item_ids = set()

    for media_type in active_types:
        model = apps.get_model("app", media_type)
        qs = (
            model.objects.filter(user=user, item__release_datetime__isnull=False)
            .select_related("item")
            .annotate(
                release_month=ExtractMonth("item__release_datetime"),
                release_day=ExtractDay("item__release_datetime"),
            )
            .filter(release_month=today.month, release_day=today.day)
        )
        for media in qs:
            item = getattr(media, "item", None)
            if not item or item.id in seen_item_ids:
                continue
            seen_item_ids.add(item.id)
            release_dt = getattr(item, "release_datetime", None)
            if not release_dt:
                continue
            localized = stats._localize_datetime(release_dt)
            if not localized:
                continue
            release_date = localized.date()
            items_by_year[release_date.year].append({
                "item": item,
                "media_type": item.media_type,
                "title": item.title,
                "image": _get_horizontal_history_image(item, item.image, allow_network=True),
                "release_date": release_date,
            })

    Episode = apps.get_model("app", "Episode")
    episode_qs = (
        Episode.objects.filter(
            related_season__user=user,
            item__release_datetime__isnull=False,
        )
        .select_related(
            "item",
            "related_season__item",
            "related_season__related_tv__item",
        )
        .annotate(
            release_month=ExtractMonth("item__release_datetime"),
            release_day=ExtractDay("item__release_datetime"),
        )
        .filter(release_month=today.month, release_day=today.day)
    )
    for episode in episode_qs:
        episode_item = getattr(episode, "item", None)
        if not episode_item or episode_item.id in seen_item_ids:
            continue
        seen_item_ids.add(episode_item.id)
        release_dt = getattr(episode_item, "release_datetime", None)
        if not release_dt:
            continue
        localized = stats._localize_datetime(release_dt)
        if not localized:
            continue
        release_date = localized.date()
        display_title = history_cache._get_episode_display_title(episode)
        episode_poster = history_cache._get_episode_poster(episode)
        items_by_year[release_date.year].append({
            "item": episode_item,
            "media_type": MediaTypes.EPISODE.value,
            "title": display_title or episode_item.title,
            "image": _get_horizontal_history_image(
                episode_item,
                episode_poster,
                allow_network=True,
            ),
            "release_date": release_date,
        })

    if include_podcasts:
        Podcast = apps.get_model("app", "Podcast")
        podcast_base = Podcast.objects.filter(user=user).select_related("item", "episode", "show")
        podcast_qs = (
            podcast_base.filter(episode__published__isnull=False)
            .annotate(
                release_month=ExtractMonth("episode__published"),
                release_day=ExtractDay("episode__published"),
            )
            .filter(release_month=today.month, release_day=today.day)
        )
        for podcast in podcast_qs:
            item = getattr(podcast, "item", None)
            if not item or item.id in seen_item_ids:
                continue
            release_dt = getattr(getattr(podcast, "episode", None), "published", None)
            localized = stats._localize_datetime(release_dt) if release_dt else None
            if not localized:
                continue
            release_date = localized.date()
            show = None
            if getattr(podcast, "episode", None) and podcast.episode.show:
                show = podcast.episode.show
            if not show:
                show = podcast.show
            image = (show.image if show and show.image else None) or item.image
            title = item.title or getattr(getattr(podcast, "episode", None), "title", "")
            seen_item_ids.add(item.id)
            items_by_year[release_date.year].append({
                "item": item,
                "media_type": MediaTypes.PODCAST.value,
                "title": title,
                "image": _get_horizontal_history_image(
                    item,
                    image,
                    allow_network=False,
                ),
                "release_date": release_date,
            })

        podcast_fallback_qs = (
            podcast_base.filter(
                episode__published__isnull=True,
                item__release_datetime__isnull=False,
            )
            .annotate(
                release_month=ExtractMonth("item__release_datetime"),
                release_day=ExtractDay("item__release_datetime"),
            )
            .filter(release_month=today.month, release_day=today.day)
        )
        for podcast in podcast_fallback_qs:
            item = getattr(podcast, "item", None)
            if not item or item.id in seen_item_ids:
                continue
            release_dt = getattr(item, "release_datetime", None)
            localized = stats._localize_datetime(release_dt) if release_dt else None
            if not localized:
                continue
            release_date = localized.date()
            show = None
            if getattr(podcast, "episode", None) and podcast.episode.show:
                show = podcast.episode.show
            if not show:
                show = podcast.show
            image = (show.image if show and show.image else None) or item.image
            title = item.title or getattr(getattr(podcast, "episode", None), "title", "")
            seen_item_ids.add(item.id)
            items_by_year[release_date.year].append({
                "item": item,
                "media_type": MediaTypes.PODCAST.value,
                "title": title,
                "image": _get_horizontal_history_image(
                    item,
                    image,
                    allow_network=False,
                ),
                "release_date": release_date,
            })

    if not items_by_year:
        return None, None

    available_years = sorted(items_by_year.keys())
    selected_year = random.choice(available_years)
    selected_item = random.choice(items_by_year[selected_year]) if items_by_year[selected_year] else None
    if not selected_item:
        return None, None
    return selected_item, selected_year


def _get_history_index_days(user):
    logging_style = history_cache._normalize_logging_style(None, user)
    cache_entry = cache.get(history_cache._cache_key(user.id, logging_style))
    if isinstance(cache_entry, dict):
        days = cache_entry.get("days")
        if isinstance(days, list):
            return days

    day_keys = history_cache.build_history_index(user, logging_style_override=logging_style)
    history_cache.cache_history_index(user.id, logging_style, day_keys)
    return day_keys


def _get_history_day_payload(user, day_value):
    logging_style = history_cache._normalize_logging_style(None, user)
    day_key = history_cache._day_key_from_value(day_value)
    if not day_key:
        return None

    cache_key = history_cache._day_cache_key(user.id, logging_style, day_key)
    cached_payload = cache.get(cache_key)
    if cached_payload:
        return history_cache._deserialize_history_day(cached_payload)

    return history_cache.build_history_day(
        user,
        day_key,
        logging_style_override=logging_style,
    )


def _get_range_history_boundary_days(user, start_date, end_date):
    # Deferred import: _normalize_day_value lives in statistics_cache; importing
    # at module level would create a load-time circular dependency.
    from app.statistics_cache import _normalize_day_value

    start_day = _normalize_day_value(start_date)
    end_day = _normalize_day_value(end_date)
    index_day_keys = _get_history_index_days(user)
    if not index_day_keys:
        return None, None

    matching_days = []
    for day_key in index_day_keys:
        day_value = _normalize_day_value(day_key)
        if not day_value:
            continue
        if start_day and day_value < start_day:
            continue
        if end_day and day_value > end_day:
            continue
        matching_days.append(day_value)

    if not matching_days:
        return None, None
    return matching_days[-1], matching_days[0]
