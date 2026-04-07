"""Bulk episode play helpers for TV, anime, and podcast tracking."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from simple_history.utils import bulk_create_with_history

import events
from app import cache_utils
from app.mixins import disable_fetch_releases
from app.models import (
    TV,
    Anime,
    Episode,
    Item,
    MediaTypes,
    MetadataProviderPreference,
    Podcast,
    PodcastEpisode,
    PodcastShow,
    PodcastShowTracker,
    Season,
    Sources,
    Status,
)
from app.providers import services
from app.services.tracking_hydration import ensure_item_metadata


@dataclass(slots=True)
class BulkEpisodePlayResult:
    """Outcome payload for a bulk episode logging request."""

    created_count: int
    replaced_episode_count: int
    grouped_item: Item | None = None
    grouped_redirect_media_type: str | None = None
    migrated_flat_anime: bool = False
    created_grouped_tracking: bool = False


def coerce_episode_datetime(value):
    """Return a timezone-aware datetime for provider episode timestamps."""
    if value in (None, ""):
        return None

    tz = timezone.get_current_timezone()

    def _make_aware(candidate):
        if timezone.is_naive(candidate):
            return timezone.make_aware(candidate, tz)
        return candidate

    if hasattr(value, "hour"):
        return _make_aware(value)

    if isinstance(value, str):
        parsed_datetime = parse_datetime(value.replace("Z", "+00:00"))
        if parsed_datetime is not None:
            return _make_aware(parsed_datetime)

        parsed_date = parse_date(value[:10])
        if parsed_date is not None:
            combined = timezone.datetime.combine(
                parsed_date,
                timezone.datetime.min.time(),
            )
            return timezone.make_aware(combined, tz)

    if hasattr(value, "year"):
        combined = timezone.datetime.combine(
            value,
            timezone.datetime.min.time(),
        )
        return timezone.make_aware(combined, tz)

    return None


def distribute_timestamps(
    start_dt,
    end_dt,
    total_count: int,
    *,
    fallback_dt=None,
):
    """Return evenly distributed, monotonically increasing timestamps."""
    if total_count <= 0:
        return []

    fallback_dt = fallback_dt or timezone.now()
    if start_dt and end_dt:
        if total_count == 1:
            timestamps = [end_dt]
        else:
            span_seconds = max((end_dt - start_dt).total_seconds(), 0)
            step_seconds = span_seconds / max(total_count - 1, 1)
            timestamps = [
                start_dt + timedelta(seconds=round(step_seconds * index))
                for index in range(total_count)
            ]
    elif end_dt:
        timestamps = [end_dt for _ in range(total_count)]
    elif start_dt:
        timestamps = [start_dt for _ in range(total_count)]
    else:
        timestamps = [fallback_dt for _ in range(total_count)]

    return ensure_increasing_datetimes(timestamps)


def _clamp_datetime(value, start_dt=None, end_dt=None):
    """Clamp a datetime to an optional inclusive window."""
    if start_dt and value < start_dt:
        return start_dt
    if end_dt and value > end_dt:
        return end_dt
    return value


def distribute_target_timestamps(
    target_datetimes,
    start_dt,
    end_dt,
    *,
    fallback_dt=None,
):
    """Bias timestamps toward air dates while fitting them into a chosen range."""
    total_count = len(target_datetimes)
    if total_count <= 0:
        return []

    fallback_dt = fallback_dt or end_dt or start_dt or timezone.now()
    normalized_targets = [
        coerce_episode_datetime(value) or fallback_dt
        for value in target_datetimes
    ]
    if not start_dt or not end_dt:
        return ensure_increasing_datetimes(normalized_targets)

    available_span = max((end_dt - start_dt).total_seconds(), 0)
    if total_count == 1:
        return [
            _clamp_datetime(normalized_targets[0], start_dt=start_dt, end_dt=end_dt),
        ]
    if available_span < total_count - 1:
        return distribute_timestamps(
            start_dt,
            end_dt,
            total_count,
            fallback_dt=fallback_dt,
        )

    ordered_targets = ensure_increasing_datetimes(normalized_targets)
    target_span = max((ordered_targets[-1] - ordered_targets[0]).total_seconds(), 0)
    if target_span <= 0:
        return distribute_timestamps(
            start_dt,
            end_dt,
            total_count,
            fallback_dt=fallback_dt,
        )

    mapped = []
    for target_dt in ordered_targets:
        progress = (target_dt - ordered_targets[0]).total_seconds() / target_span
        mapped.append(
            start_dt + timedelta(seconds=round(available_span * progress)),
        )

    fitted = [
        _clamp_datetime(candidate, start_dt=start_dt, end_dt=end_dt)
        for candidate in mapped
    ]
    for index in range(1, total_count):
        minimum = fitted[index - 1] + timedelta(seconds=1)
        if fitted[index] < minimum:
            fitted[index] = minimum

    if fitted[-1] > end_dt:
        fitted[-1] = end_dt
        for index in range(total_count - 2, -1, -1):
            latest_allowed = fitted[index + 1] - timedelta(seconds=1)
            if fitted[index] > latest_allowed:
                fitted[index] = latest_allowed
        if fitted[0] < start_dt:
            return distribute_timestamps(
                start_dt,
                end_dt,
                total_count,
                fallback_dt=fallback_dt,
            )

    return fitted


def ensure_increasing_datetimes(values):
    """Guarantee a strictly increasing datetime sequence while preserving order."""
    normalized = []
    current = None

    for value in values:
        candidate = coerce_episode_datetime(value) or timezone.now()
        if current is not None and candidate <= current:
            candidate = current + timedelta(seconds=1)
        normalized.append(candidate)
        current = candidate

    return normalized


def _season_title_from_payload(payload, season_number):
    title = payload.get("season_title") if isinstance(payload, dict) else None
    if title:
        return title
    if season_number == 0:
        return "Specials"
    return f"Season {season_number}"


def _episode_title_from_payload(payload, episode_number):
    return (
        payload.get("name")
        or payload.get("title")
        or f"Episode {episode_number}"
    )


def _podcast_selector_label(episode, selector_number):
    """Return a readable dropdown label for podcast episode ranges."""
    prefix = (
        f"E{episode.episode_number}"
        if episode.episode_number is not None
        else f"#{selector_number}"
    )
    label = f"{prefix} - {episode.title or f'Episode {selector_number}'}"
    if episode.published:
        label += f" ({timezone.localtime(episode.published).date().isoformat()})"
    return label


def _podcast_play_counts_for_show(user, show):
    """Return completed podcast play counts keyed by episode id."""
    rows = (
        Podcast.objects.filter(
            user=user,
            show=show,
            episode__isnull=False,
            end_date__isnull=False,
        )
        .values("episode_id")
        .annotate(play_count=Count("id"))
    )
    return {
        row["episode_id"]: row["play_count"]
        for row in rows
        if row["episode_id"] is not None
    }


def _podcast_domain(user, show):
    """Build a bulk-play selector domain for podcast shows."""
    episodes = list(PodcastEpisode.objects.filter(show=show))
    if not episodes:
        return None

    sort_fallback = timezone.now()
    episodes.sort(
        key=lambda episode: (
            episode.published is None,
            episode.published or sort_fallback,
            episode.episode_number is None,
            episode.episode_number or 0,
            episode.id,
        ),
    )

    play_counts = _podcast_play_counts_for_show(user, show)
    selector_episodes = []
    for order, episode in enumerate(episodes):
        selector_number = order + 1
        runtime_minutes = episode.duration // 60 if episode.duration else 0
        selector_episodes.append(
            {
                "order": order,
                "season_number": 1,
                "season_title": "Episodes",
                "episode_number": selector_number,
                "episode_title": episode.title or f"Episode {selector_number}",
                "selector_label": _podcast_selector_label(episode, selector_number),
                "air_date": coerce_episode_datetime(episode.published),
                "release_datetime": coerce_episode_datetime(episode.published),
                "existing_play_count": play_counts.get(episode.id, 0),
                "podcast_episode_id": episode.id,
                "podcast_episode_uuid": episode.episode_uuid,
                "runtime_minutes": runtime_minutes,
            },
        )

    return {
        "route_media_type": MediaTypes.PODCAST.value,
        "tracking_source": Sources.POCKETCASTS.value,
        "tracking_media_id": show.podcast_uuid,
        "tracking_media_type": MediaTypes.PODCAST.value,
        "identity_media_type": None,
        "library_media_type": MediaTypes.PODCAST.value,
        "season_payloads": {},
        "episodes": selector_episodes,
        "episode_lookup": {
            (1, episode["episode_number"]): episode
            for episode in selector_episodes
        },
        "season_episode_map": {1: selector_episodes},
        "seasons": [
            {
                "season_number": 1,
                "season_title": "Episodes",
                "episode_count": len(selector_episodes),
                "locked": True,
            }
        ],
        "default_first": {
            "season_number": 1,
            "episode_number": selector_episodes[0]["episode_number"],
        },
        "default_last": {
            "season_number": 1,
            "episode_number": selector_episodes[-1]["episode_number"],
        },
        "locked_season_number": 1,
        "hide_season_selectors": True,
        "mode_notice": "",
        "is_flat_anime_grouped_slice": False,
        "is_podcast_range": True,
        "podcast_show_id": show.id,
    }


def _play_counts_for_grouped_target(user, *, source, media_id, library_media_type):
    """Return existing grouped episode play counts keyed by season/episode number."""
    filters = Q(
        related_season__user=user,
        related_season__item__media_id=media_id,
        related_season__item__source=source,
    )
    if library_media_type == MediaTypes.ANIME.value:
        filters &= Q(
            related_season__related_tv__item__library_media_type=MediaTypes.ANIME.value,
        )

    rows = (
        Episode.objects.filter(filters)
        .values("item__season_number", "item__episode_number")
        .annotate(play_count=Count("id"))
    )

    return {
        (row["item__season_number"], row["item__episode_number"]): row["play_count"]
        for row in rows
    }


def _standard_grouped_domain(
    user,
    route_media_type: str,
    source: str,
    media_id: str,
    *,
    metadata_item,
    base_metadata,
):
    """Build a standard TV/grouped-anime episode domain."""
    related = base_metadata.get("related") if isinstance(base_metadata, dict) else {}
    seasons = related.get("seasons") if isinstance(related, dict) else []
    season_numbers = [
        season.get("season_number")
        for season in seasons
        if season.get("season_number") is not None
    ]
    if not season_numbers:
        return None

    tv_with_seasons = services.get_media_metadata(
        "tv_with_seasons",
        media_id,
        source,
        season_numbers,
    )
    season_payloads = {
        season_number: tv_with_seasons.get(f"season/{season_number}")
        for season_number in season_numbers
        if isinstance(tv_with_seasons.get(f"season/{season_number}"), dict)
    }
    if not season_payloads:
        return None

    library_media_type = (
        getattr(metadata_item, "library_media_type", None)
        or base_metadata.get("library_media_type")
        or route_media_type
    )
    play_counts = _play_counts_for_grouped_target(
        user,
        source=source,
        media_id=media_id,
        library_media_type=library_media_type,
    )
    return _build_domain_payload(
        route_media_type=route_media_type,
        source=source,
        media_id=media_id,
        library_media_type=library_media_type,
        season_payloads=season_payloads,
        play_counts=play_counts,
    )


def _flat_anime_grouped_domain(
    user,
    *,
    metadata_item,
    metadata_resolution_result,
):
    """Build a grouped season slice for a flat MAL anime route."""
    target = metadata_resolution_result.grouped_preview_target
    grouped_preview = metadata_resolution_result.grouped_preview
    provider = metadata_resolution_result.display_provider
    provider_media_id = metadata_resolution_result.provider_media_id
    if (
        not isinstance(target, dict)
        or provider not in {Sources.TMDB.value, Sources.TVDB.value}
        or not provider_media_id
        or not isinstance(grouped_preview, dict)
    ):
        return None

    season_number = target.get("season_number")
    if season_number is None:
        return None

    season_payload = grouped_preview.get(f"season/{season_number}")
    if not isinstance(season_payload, dict):
        return None

    episode_start = target.get("episode_start")
    episode_end = target.get("episode_end")
    filtered_episodes = []
    for episode in season_payload.get("episodes", []):
        episode_number = episode.get("episode_number")
        if episode_number is None:
            continue
        if episode_start is not None and episode_number < episode_start:
            continue
        if episode_end is not None and episode_number > episode_end:
            continue
        filtered_episodes.append(episode)

    if not filtered_episodes:
        return None

    sliced_payload = dict(season_payload)
    sliced_payload["episodes"] = filtered_episodes
    play_counts = _play_counts_for_grouped_target(
        user,
        source=provider,
        media_id=provider_media_id,
        library_media_type=MediaTypes.ANIME.value,
    )

    active_flat_exists = False
    if metadata_item is not None:
        active_flat_exists = Anime.objects.filter(
            user=user,
            item=metadata_item,
            migrated_to_item__isnull=True,
        ).exists()

    notice = (
        "This will migrate your MAL anime entry into grouped episode "
        "tracking before logging plays."
        if active_flat_exists
        else "This will create grouped episode tracking for this anime "
        "before logging plays."
    )
    domain = _build_domain_payload(
        route_media_type=MediaTypes.ANIME.value,
        source=provider,
        media_id=provider_media_id,
        library_media_type=MediaTypes.ANIME.value,
        season_payloads={season_number: sliced_payload},
        play_counts=play_counts,
        locked_season_number=season_number,
    )
    domain["flat_anime_item"] = metadata_item
    domain["active_flat_anime_exists"] = active_flat_exists
    domain["grouped_redirect_source"] = provider
    domain["grouped_redirect_media_id"] = provider_media_id
    domain["grouped_redirect_title"] = (
        grouped_preview.get("title")
        or grouped_preview.get("localized_title")
        or grouped_preview.get("original_title")
        or metadata_item.title
        if metadata_item is not None
        else ""
    )
    domain["mode_notice"] = notice
    domain["is_flat_anime_grouped_slice"] = True
    return domain


def _build_domain_payload(
    *,
    route_media_type: str,
    source: str,
    media_id: str,
    library_media_type: str,
    season_payloads: dict[int, dict],
    play_counts: dict[tuple[int, int], int],
    locked_season_number: int | None = None,
):
    """Convert season metadata into a flattened episode selector domain."""
    episodes = []
    season_episode_map = {}
    seasons = []

    for season_number, season_payload in season_payloads.items():
        season_title = _season_title_from_payload(season_payload, season_number)
        season_episodes = []
        for episode in season_payload.get("episodes") or []:
            episode_number = episode.get("episode_number")
            if episode_number is None:
                continue
            air_date = coerce_episode_datetime(episode.get("air_date"))
            payload = {
                "order": len(episodes),
                "season_number": season_number,
                "season_title": season_title,
                "episode_number": episode_number,
                "episode_title": _episode_title_from_payload(episode, episode_number),
                "air_date": air_date,
                "release_datetime": air_date,
                "existing_play_count": play_counts.get(
                    (season_number, episode_number),
                    0,
                ),
            }
            episodes.append(payload)
            season_episodes.append(payload)
        if season_episodes:
            season_episode_map[season_number] = season_episodes
            seasons.append(
                {
                    "season_number": season_number,
                    "season_title": season_title,
                    "episode_count": len(season_episodes),
                    "locked": locked_season_number == season_number,
                },
            )

    if not episodes:
        return None
    default_first_episode = next(
        (episode for episode in episodes if episode["season_number"] != 0),
        episodes[0],
    )

    return {
        "route_media_type": route_media_type,
        "tracking_source": source,
        "tracking_media_id": media_id,
        "tracking_media_type": MediaTypes.TV.value,
        "identity_media_type": (
            MediaTypes.TV.value
            if route_media_type == MediaTypes.ANIME.value
            else None
        ),
        "library_media_type": library_media_type,
        "season_payloads": season_payloads,
        "episodes": episodes,
        "episode_lookup": {
            (episode["season_number"], episode["episode_number"]): episode
            for episode in episodes
        },
        "season_episode_map": season_episode_map,
        "seasons": seasons,
        "default_first": {
            "season_number": default_first_episode["season_number"],
            "episode_number": default_first_episode["episode_number"],
        },
        "default_last": {
            "season_number": episodes[-1]["season_number"],
            "episode_number": episodes[-1]["episode_number"],
        },
        "locked_season_number": locked_season_number,
        "mode_notice": "",
        "is_flat_anime_grouped_slice": False,
    }


def build_episode_play_domain(
    user,
    route_media_type: str,
    source: str,
    media_id: str,
    *,
    metadata_item=None,
    base_metadata=None,
    metadata_resolution_result=None,
    podcast_show=None,
):
    """Return an episode selection domain for detail track modal tabs."""
    if route_media_type == MediaTypes.PODCAST.value:
        if source != Sources.POCKETCASTS.value:
            return None
        show = podcast_show or PodcastShow.objects.filter(podcast_uuid=media_id).first()
        if show is None:
            return None
        return _podcast_domain(user, show)

    if route_media_type not in {MediaTypes.TV.value, MediaTypes.ANIME.value}:
        return None

    if (
        route_media_type == MediaTypes.ANIME.value
        and source == Sources.MAL.value
        and metadata_resolution_result is not None
    ):
        return _flat_anime_grouped_domain(
            user,
            metadata_item=metadata_item,
            metadata_resolution_result=metadata_resolution_result,
        )

    if base_metadata is None:
        base_metadata = services.get_media_metadata(
            route_media_type,
            media_id,
            source,
        )

    return _standard_grouped_domain(
        user,
        route_media_type,
        source,
        media_id,
        metadata_item=metadata_item,
        base_metadata=base_metadata,
    )


def _resolve_grouped_target(user, domain):
    """Return the grouped TV row that should receive the bulk episode plays."""
    migrated_flat_anime = False
    created_grouped_tracking = False

    if domain.get("is_flat_anime_grouped_slice"):
        flat_anime_item = domain.get("flat_anime_item")
        provider = domain["tracking_source"]
        provider_media_id = domain["tracking_media_id"]

        if domain.get("active_flat_anime_exists") and flat_anime_item is not None:
            anime_migration = importlib.import_module("app.services.anime_migration")
            migration_result = anime_migration.migrate_flat_anime_to_grouped(
                user,
                flat_anime_item,
                provider,
            )
            grouped_tv = migration_result.grouped_tv
            migrated_flat_anime = True
        else:
            hydrated = ensure_item_metadata(
                user,
                MediaTypes.ANIME.value,
                provider_media_id,
                provider,
                identity_media_type=MediaTypes.TV.value,
                library_media_type=MediaTypes.ANIME.value,
            )
            grouped_tv, created = TV.objects.get_or_create(
                item=hydrated.item,
                user=user,
                defaults={
                    "status": Status.PLANNING.value,
                    "score": None,
                    "notes": "",
                },
            )
            MetadataProviderPreference.objects.update_or_create(
                user=user,
                item=grouped_tv.item,
                defaults={"provider": provider},
            )
            created_grouped_tracking = created
        return grouped_tv, migrated_flat_anime, created_grouped_tracking

    filters = {
        "user": user,
        "item__media_id": domain["tracking_media_id"],
        "item__source": domain["tracking_source"],
    }
    library_media_type = domain.get("library_media_type")
    if library_media_type:
        filters["item__library_media_type"] = library_media_type

    grouped_tv = TV.objects.filter(**filters).first()
    if grouped_tv is None:
        hydrated = ensure_item_metadata(
            user,
            domain["route_media_type"],
            domain["tracking_media_id"],
            domain["tracking_source"],
            identity_media_type=domain.get("identity_media_type"),
            library_media_type=library_media_type,
        )
        grouped_tv = TV.objects.create(
            item=hydrated.item,
            user=user,
            status=Status.PLANNING.value,
            score=None,
            notes="",
        )
        created_grouped_tracking = True

    return grouped_tv, migrated_flat_anime, created_grouped_tracking


def _season_item_defaults(grouped_tv, season_payload, *, library_media_type):
    return {
        **Item.title_fields_from_metadata(
            season_payload,
            fallback_title=grouped_tv.item.title,
        ),
        "library_media_type": library_media_type,
        "image": (
            season_payload.get("image")
            or grouped_tv.item.image
            or settings.IMG_NONE
        ),
    }


def _get_or_create_season_tracker(
    grouped_tv,
    season_number,
    season_payload,
    *,
    library_media_type,
):
    """Return the season tracker for a grouped TV/anime target."""
    season_defaults = _season_item_defaults(
        grouped_tv,
        season_payload,
        library_media_type=library_media_type,
    )
    season_item, season_item_created = Item.objects.get_or_create(
        media_id=grouped_tv.item.media_id,
        source=grouped_tv.item.source,
        media_type=MediaTypes.SEASON.value,
        season_number=season_number,
        defaults=season_defaults,
    )

    update_fields = []
    if season_item.library_media_type != library_media_type:
        season_item.library_media_type = library_media_type
        update_fields.append("library_media_type")
    season_image = (
        season_payload.get("image")
        or grouped_tv.item.image
        or settings.IMG_NONE
    )
    if season_image and season_item.image != season_image:
        season_item.image = season_image
        update_fields.append("image")
    if update_fields:
        season_item.save(update_fields=update_fields)

    season_tracker = Season.objects.filter(
        item=season_item,
        user=grouped_tv.user,
    ).first()
    if season_tracker is None:
        season_tracker = Season.objects.create(
            item=season_item,
            user=grouped_tv.user,
            related_tv=grouped_tv,
            status=Status.PLANNING.value,
            score=None,
            notes="",
        )
        season_tracker_created = True
    elif season_tracker.related_tv_id != grouped_tv.id:
        season_tracker.related_tv = grouped_tv
        season_tracker.save(update_fields=["related_tv"])
        season_tracker_created = False
    else:
        season_tracker_created = False

    return season_tracker, season_item_created or season_tracker_created


def _episode_delete_filter(selected_episodes):
    filters = Q()
    for episode in selected_episodes:
        filters |= Q(
            item__season_number=episode["season_number"],
            item__episode_number=episode["episode_number"],
        )
    return filters


def _get_or_create_podcast_episode_item(show, episode):
    """Return the trackable item for a podcast episode."""
    runtime_minutes = episode.duration // 60 if episode.duration else None
    defaults = {
        "title": episode.title,
        "image": show.image or settings.IMG_NONE,
    }
    if runtime_minutes:
        defaults["runtime_minutes"] = runtime_minutes
    if episode.published:
        defaults["release_datetime"] = episode.published

    item, created = Item.objects.get_or_create(
        media_id=episode.episode_uuid,
        source=Sources.POCKETCASTS.value,
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

    return item, created


def _apply_bulk_podcast_plays(
    user,
    domain,
    *,
    selected_episodes,
    write_mode: str,
    distribution_mode: str,
    start_date=None,
    end_date=None,
):
    """Persist a bulk range of podcast episode plays."""
    show = PodcastShow.objects.get(id=domain["podcast_show_id"])
    PodcastShowTracker.objects.get_or_create(
        user=user,
        show=show,
        defaults={"status": Status.IN_PROGRESS.value},
    )

    if distribution_mode == "air_date":
        timestamps = distribute_target_timestamps(
            [episode["air_date"] for episode in selected_episodes],
            start_date,
            end_date,
            fallback_dt=timezone.now().replace(second=0, microsecond=0),
        )
    else:
        timestamps = distribute_timestamps(
            start_date,
            end_date,
            len(selected_episodes),
            fallback_dt=timezone.now().replace(second=0, microsecond=0),
        )

    selected_episode_ids = [
        episode["podcast_episode_id"]
        for episode in selected_episodes
    ]
    episode_map = PodcastEpisode.objects.in_bulk(selected_episode_ids)
    created_count = 0
    replaced_episode_count = 0
    created_items = []

    with disable_fetch_releases():
        if write_mode == "replace" and selected_episode_ids:
            existing_entries = Podcast.objects.filter(
                user=user,
                show=show,
                episode_id__in=selected_episode_ids,
            )
            replaced_episode_count = existing_entries.count()
            if replaced_episode_count:
                existing_entries.delete()

        episodes_to_create = []
        for episode_payload, watched_at in zip(selected_episodes, timestamps, strict=False):
            episode = episode_map.get(episode_payload["podcast_episode_id"])
            if episode is None:
                continue
            item, item_created = _get_or_create_podcast_episode_item(show, episode)
            if item_created:
                created_items.append(item)
            episodes_to_create.append(
                Podcast(
                    item=item,
                    user=user,
                    show=show,
                    episode=episode,
                    status=Status.COMPLETED.value,
                    end_date=watched_at,
                    progress=episode_payload.get("runtime_minutes") or 0,
                ),
            )

        if episodes_to_create:
            bulk_create_with_history(episodes_to_create, Podcast)
            created_count = len(episodes_to_create)

    cache_utils.clear_time_left_cache_for_user(user.id)
    if created_items:
        events.tasks.reload_calendar.apply_async(
            kwargs={"item_ids": [item.id for item in created_items]},
            countdown=3,
        )

    return BulkEpisodePlayResult(
        created_count=created_count,
        replaced_episode_count=replaced_episode_count,
    )


def apply_bulk_episode_plays(
    user,
    domain,
    *,
    selected_episodes,
    write_mode: str,
    distribution_mode: str,
    start_date=None,
    end_date=None,
):
    """Persist a bulk episode play range against grouped TV/anime tracking."""
    if domain.get("is_podcast_range"):
        return _apply_bulk_podcast_plays(
            user,
            domain,
            selected_episodes=selected_episodes,
            write_mode=write_mode,
            distribution_mode=distribution_mode,
            start_date=start_date,
            end_date=end_date,
        )

    grouped_tv, migrated_flat_anime, created_grouped_tracking = _resolve_grouped_target(
        user,
        domain,
    )
    touched_seasons = {}
    created_count = 0
    replaced_episode_count = 0
    created_items = created_grouped_tracking

    if distribution_mode == "air_date":
        timestamps = distribute_target_timestamps(
            [episode["air_date"] for episode in selected_episodes],
            start_date,
            end_date,
            fallback_dt=timezone.now().replace(second=0, microsecond=0),
        )
    else:
        timestamps = distribute_timestamps(
            start_date,
            end_date,
            len(selected_episodes),
            fallback_dt=timezone.now().replace(second=0, microsecond=0),
        )

    with disable_fetch_releases():
        for episode in selected_episodes:
            season_number = episode["season_number"]
            season_payload = domain["season_payloads"][season_number]
            if season_number not in touched_seasons:
                season_tracker, season_created = _get_or_create_season_tracker(
                    grouped_tv,
                    season_number,
                    season_payload,
                    library_media_type=domain["library_media_type"],
                )
                touched_seasons[season_number] = season_tracker
                created_items = created_items or season_created
            season_tracker = touched_seasons[season_number]

        if write_mode == "replace":
            delete_filters = _episode_delete_filter(selected_episodes)
            if delete_filters:
                existing_count = Episode.objects.filter(
                    related_season__in=touched_seasons.values(),
                ).filter(delete_filters).count()
                if existing_count:
                    Episode.objects.filter(
                        related_season__in=touched_seasons.values(),
                    ).filter(delete_filters).delete()
                    replaced_episode_count = existing_count

        episodes_to_create = []
        for episode, watched_at in zip(selected_episodes, timestamps, strict=False):
            season_tracker = touched_seasons[episode["season_number"]]
            episode_item_exists = Item.objects.filter(
                media_id=season_tracker.item.media_id,
                source=season_tracker.item.source,
                media_type=MediaTypes.EPISODE.value,
                season_number=episode["season_number"],
                episode_number=episode["episode_number"],
            ).exists()
            episode_item = season_tracker.get_episode_item(
                episode["episode_number"],
                domain["season_payloads"][episode["season_number"]],
            )
            if episode_item.library_media_type != domain["library_media_type"]:
                episode_item.library_media_type = domain["library_media_type"]
                episode_item.save(update_fields=["library_media_type"])
            created_items = created_items or not episode_item_exists
            episodes_to_create.append(
                Episode(
                    related_season=season_tracker,
                    item=episode_item,
                    end_date=watched_at,
                ),
            )

        if episodes_to_create:
            bulk_create_with_history(episodes_to_create, Episode)
            created_count = len(episodes_to_create)

    for season_tracker in touched_seasons.values():
        season_tracker.refresh_from_db()
        season_tracker._sync_status_after_episode_change()

    cache_utils.clear_time_left_cache_for_user(user.id)
    if created_items:
        events.tasks.reload_calendar.apply_async(
            kwargs={"item_ids": [grouped_tv.item.id]},
            countdown=3,
        )

    return BulkEpisodePlayResult(
        created_count=created_count,
        replaced_episode_count=replaced_episode_count,
        grouped_item=(
            grouped_tv.item if domain.get("is_flat_anime_grouped_slice") else None
        ),
        grouped_redirect_media_type=(
            MediaTypes.ANIME.value
            if domain.get("is_flat_anime_grouped_slice")
            else None
        ),
        migrated_flat_anime=migrated_flat_anime,
        created_grouped_tracking=created_grouped_tracking,
    )
