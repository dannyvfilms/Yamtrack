import logging
import requests
from collections import defaultdict

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Max, Min
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from app import (
    credits,
    custom_metadata,
    helpers,
    metadata_utils,
)
from app.models import CollectionEntry, Episode, Item, MediaTypes, MetadataProviderPreference, Sources
from app.providers import services, tmdb
from app.services import (
    anime_migration,
    bulk_episode_tracking,
    metadata_resolution,
)
from app.services import game_lengths as game_length_services
from app.services import trakt_popularity as trakt_popularity_service
from app.log_safety import exception_summary, safe_url
from integrations import anime_mapping

logger = logging.getLogger(__name__)


@login_required
@require_POST
def update_metadata_provider_preference(request, source, media_type, media_id):
    """Persist a per-item metadata display-provider override."""
    provider = (request.POST.get("provider") or "").strip()
    return_url = helpers.normalize_navigation_url(request.POST.get("return_url"))

    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
    )
    lookup = {
        "media_id": media_id,
        "source": source,
        "media_type": tracking_media_type,
    }
    if metadata_resolution.is_grouped_anime_route(media_type, source=source):
        lookup["library_media_type"] = MediaTypes.ANIME.value

    item = get_object_or_404(Item, **lookup)
    allowed_providers = {
        choice.value
        for choice in metadata_resolution.available_metadata_provider_options(
            media_type,
            identity_provider=item.source,
        )
    }
    if provider not in allowed_providers:
        messages.error(request, "That metadata provider is not available for this title.")
    else:
        if (
            provider == Sources.MANUAL.value
            and custom_metadata.supports_custom_provider(media_type)
        ):
            current_display_metadata = _resolve_current_display_metadata_payload(
                user=request.user,
                item=item,
                media_type=media_type,
                media_id=media_id,
                source=source,
            )
            custom_metadata.snapshot_custom_metadata(item, current_display_metadata)

        MetadataProviderPreference.objects.update_or_create(
            user=request.user,
            item=item,
            defaults={"provider": provider},
        )
        messages.success(request, "Metadata provider updated.")

    if return_url and (
        return_url.startswith("/")
        or url_has_allowed_host_and_scheme(
            return_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return redirect(return_url)

    return redirect(
        "media_details",
        source=source,
        media_type=media_type,
        media_id=media_id,
        title=title if (title := item.get_display_title(request.user)) else "item",
    )


@login_required
@require_POST
def update_item_image(request, item_id):
    """Persist an image URL override for an item the user already tracks."""
    return_url = helpers.normalize_navigation_url(request.POST.get("return_url"))
    image_url = (request.POST.get("image_url") or "").strip()

    item = get_object_or_404(Item, id=item_id)
    media_model = apps.get_model("app", item.media_type)
    if not media_model.objects.filter(user=request.user, item=item).exists():
        messages.error(request, "You can only update images for items in your library.")
        return helpers.redirect_back(request)

    if not image_url:
        messages.error(request, "Enter an image URL to save.")
        return helpers.redirect_back(request)

    if item.image != image_url:
        item.image = image_url
        item.save(update_fields=["image"])
        messages.success(request, "Image URL updated.")
    else:
        messages.success(request, "Image URL already matches this item.")

    if return_url and (
        return_url.startswith("/")
        or url_has_allowed_host_and_scheme(
            return_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return redirect(return_url)

    return helpers.redirect_back(request)


@login_required
@require_POST
def update_manual_item_metadata(request, item_id):
    """Persist custom metadata overrides for a tracked manual item."""
    return_url = helpers.normalize_navigation_url(request.POST.get("return_url"))
    item = get_object_or_404(Item, id=item_id)
    media_model = apps.get_model("app", item.media_type)
    if not media_model.objects.filter(user=request.user, item=item).exists():
        messages.error(request, "You can only update metadata for items in your library.")
        return helpers.redirect_back(request)

    if not custom_metadata.supports_custom_metadata(item):
        messages.error(request, "Metadata overrides are not available for this item.")
        return helpers.redirect_back(request)

    form = custom_metadata.ManualMetadataForm(
        request.POST,
        item=item,
        prefix="metadata",
    )
    if form.is_valid():
        update_fields = form.save()
        if update_fields:
            messages.success(request, "Custom metadata updated.")
        else:
            messages.success(request, "Custom metadata already matches this item.")
    else:
        logger.error(form.errors.as_json())
        helpers.form_error_messages(form, request)

    if return_url and (
        return_url.startswith("/")
        or url_has_allowed_host_and_scheme(
            return_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return redirect(return_url)

    return helpers.redirect_back(request)


def _resolve_current_display_metadata_payload(
    *,
    user,
    item,
    media_type: str,
    media_id: str,
    source: str,
):
    """Return the metadata payload currently shown for a tracked item."""
    base_metadata = services.get_media_metadata(
        media_type,
        media_id,
        source,
    )
    current_provider = metadata_resolution.get_preferred_provider(
        user,
        item,
        media_type,
        requested_source=source,
    )
    if current_provider == Sources.MANUAL.value:
        return custom_metadata.build_custom_overlay_metadata(base_metadata, item)
    if current_provider == source:
        return base_metadata

    provider_media_id = metadata_resolution.resolve_provider_media_id(
        item,
        current_provider,
        route_media_type=media_type,
    )
    if not provider_media_id:
        return base_metadata

    return services.get_media_metadata(
        metadata_resolution.provider_route_media_type(
            media_type,
            current_provider,
        ),
        provider_media_id,
        current_provider,
    )


@login_required
@require_POST
def migrate_grouped_anime(request, source, media_type, media_id):
    """Explicitly migrate a flat MAL anime entry into grouped TV-style tracking."""
    return_url = helpers.normalize_navigation_url(request.POST.get("return_url"))
    provider = (request.POST.get("provider") or "").strip()

    item = get_object_or_404(
        Item,
        media_id=media_id,
        source=source,
        media_type=MediaTypes.ANIME.value,
    )
    allowed_providers = {Sources.TMDB.value, Sources.TVDB.value}
    if media_type != MediaTypes.ANIME.value or source != Sources.MAL.value:
        messages.error(request, "Only flat MAL anime can be migrated to grouped series.")
    elif provider not in allowed_providers:
        messages.error(request, "Choose TMDB or TVDB before migrating this anime.")
    else:
        try:
            result = anime_migration.migrate_flat_anime_to_grouped(
                request.user,
                item,
                provider,
            )
        except anime_migration.AnimeMigrationError as error:
            messages.error(request, str(error))
        else:
            messages.success(
                request,
                "Migrated this anime into grouped series tracking.",
            )
            grouped_item = result.grouped_tv.item
            grouped_title = grouped_item.get_display_title(request.user) or "item"
            return redirect(
                "media_details",
                source=grouped_item.source,
                media_type=MediaTypes.ANIME.value,
                media_id=grouped_item.media_id,
                title=grouped_title,
            )

    if return_url and url_has_allowed_host_and_scheme(return_url, allowed_hosts=None):
        return redirect(return_url)

    return redirect(
        "media_details",
        source=source,
        media_type=media_type,
        media_id=media_id,
        title=item.get_display_title(request.user) or "item",
    )


def _build_missing_season_metadata(
    tv_metadata,
    media_id,
    source,
    season_number,
    episodes_in_db,
    *,
    season_item=None,
    show_item=None,
):
    """Build minimal season metadata from local items when provider data is missing."""
    tv_metadata = tv_metadata or {}
    episodes_by_number = defaultdict(list)
    episode_item_by_number = {}

    for episode in episodes_in_db:
        item = getattr(episode, "item", None)
        episode_number = getattr(item, "episode_number", None)
        if episode_number is None:
            continue
        episodes_by_number[episode_number].append(episode)
        if item is not None:
            episode_item_by_number.setdefault(episode_number, item)

    for episode_item in Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.EPISODE.value,
        season_number=season_number,
    ).order_by("episode_number", "id"):
        if episode_item.episode_number is None:
            continue
        episode_item_by_number.setdefault(episode_item.episode_number, episode_item)

    episode_numbers = sorted(set(episodes_by_number) | set(episode_item_by_number))
    fallback_episodes = []
    tv_image = helpers.first_real_image(
        getattr(show_item, "image", None),
        tv_metadata.get("image"),
        getattr(season_item, "image", None),
        default=settings.IMG_NONE,
    )
    show_title = (
        tv_metadata.get("title")
        or getattr(show_item, "title", "")
        or getattr(season_item, "title", "")
    )

    for episode_number in episode_numbers:
        history_entries = episodes_by_number.get(episode_number, [])
        episode_item = episode_item_by_number.get(episode_number)
        air_date = None
        runtime = None
        title = f"Episode {episode_number}"
        primary_image = getattr(episode_item, "image", None)

        if (
            helpers.has_real_image(primary_image)
            and helpers.has_real_image(tv_image)
            and primary_image == tv_image
        ):
            primary_image = None

        if episode_item:
            if episode_item.release_datetime:
                air_date = episode_item.release_datetime
            if (
                episode_item.runtime_minutes
                and episode_item.runtime_minutes < 999998
            ):
                runtime = tmdb.get_readable_duration(episode_item.runtime_minutes)
            if episode_item.title and episode_item.title != show_title:
                title = episode_item.title

        episode_image, image_source = helpers.resolve_image_with_fallback(
            primary_image,
            tv_image,
        )

        fallback_episodes.append(
            {
                "media_id": media_id,
                "media_type": MediaTypes.EPISODE.value,
                "source": source,
                "season_number": season_number,
                "episode_number": episode_number,
                "air_date": air_date,
                "image": episode_image,
                "image_source": image_source,
                "title": title,
                "overview": "",
                "history": history_entries,
                "runtime": runtime,
                "item": episode_item,
            },
        )

    max_episode_number = max(episode_numbers) if episode_numbers else None
    details = {}
    if max_episode_number:
        details["episodes"] = max_episode_number

    air_dates = [ep["air_date"] for ep in fallback_episodes if ep.get("air_date")]
    if air_dates:
        details["first_air_date"] = min(air_dates)
        details["last_air_date"] = max(air_dates)

    source_url = tv_metadata.get("source_url") or ""
    if source == Sources.TMDB.value:
        source_url = f"https://www.themoviedb.org/tv/{media_id}/season/{season_number}"

    synopsis = tv_metadata.get("synopsis") or ""
    if not synopsis and show_item is not None:
        synopsis = (show_item.manual_metadata or {}).get("synopsis") or ""

    return {
        "media_id": media_id,
        "source": source,
        "media_type": MediaTypes.SEASON.value,
        "title": show_title,
        "season_title": f"Season {season_number}",
        "image": helpers.first_real_image(
            getattr(season_item, "image", None),
            tv_image,
            default=settings.IMG_NONE,
        ),
        "season_number": season_number,
        "synopsis": synopsis or "No synopsis available.",
        "genres": tv_metadata.get("genres") or getattr(show_item, "genres", []) or [],
        "max_progress": max_episode_number,
        "score": None,
        "score_count": None,
        "details": details,
        "episodes": fallback_episodes,
        "related": {},
        "source_url": source_url,
        "tvdb_id": tv_metadata.get("tvdb_id"),
        "external_links": tv_metadata.get("external_links"),
    }


def _get_local_show_item(media_id, source):
    """Return the locally stored show item for a season route, if available."""
    show_item = Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.TV.value,
    ).first()
    if show_item is not None:
        return show_item
    return Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.ANIME.value,
    ).first()


def _build_local_related_seasons(media_id, source, show_title, show_image):
    """Return locally persisted season rows for the season dropdown."""
    episode_stats = {
        row["season_number"]: row
        for row in Item.objects.filter(
            media_id=media_id,
            source=source,
            media_type=MediaTypes.EPISODE.value,
            season_number__isnull=False,
        )
        .values("season_number")
        .annotate(
            max_progress=Max("episode_number"),
            first_air_date=Min("release_datetime"),
        )
    }

    related_seasons = []
    for season_item in Item.objects.filter(
        media_id=media_id,
        source=source,
        media_type=MediaTypes.SEASON.value,
        season_number__isnull=False,
    ).order_by("season_number", "id"):
        season_number = season_item.season_number
        if season_number is None:
            continue

        season_title = "Specials" if season_number == 0 else f"Season {season_number}"
        season_stats = episode_stats.get(season_number, {})
        related_seasons.append(
            {
                "source": source,
                "media_type": MediaTypes.SEASON.value,
                "media_id": media_id,
                "title": show_title,
                "season_title": season_title,
                "season_header_title": season_title,
                "season_number": season_number,
                "image": helpers.first_real_image(
                    season_item.image,
                    show_image,
                    default=settings.IMG_NONE,
                ),
                "max_progress": season_stats.get("max_progress") or 0,
                "first_air_date": season_stats.get("first_air_date"),
            },
        )

    return related_seasons


def _build_local_tv_with_seasons_metadata(
    media_id,
    source,
    *,
    tv_metadata=None,
    show_item=None,
    season_item=None,
):
    """Return TV metadata enriched with locally persisted season rows."""
    tv_metadata = dict(tv_metadata or {})
    show_item = show_item or _get_local_show_item(media_id, source)
    show_title = (
        tv_metadata.get("title")
        or getattr(show_item, "title", "")
        or getattr(season_item, "title", "")
    )
    show_image = helpers.first_real_image(
        getattr(show_item, "image", None),
        tv_metadata.get("image"),
        getattr(season_item, "image", None),
        default=settings.IMG_NONE,
    )

    related = dict(tv_metadata.get("related") or {})
    provider_related_seasons = []
    seen_season_numbers = set()
    for season in related.get("seasons") or []:
        if not isinstance(season, dict):
            continue
        season_copy = dict(season)
        raw_season_number = season_copy.get("season_number")
        try:
            normalized_season_number = (
                int(raw_season_number) if raw_season_number is not None else None
            )
        except (TypeError, ValueError):
            normalized_season_number = None
        season_copy["season_number"] = normalized_season_number
        if normalized_season_number is not None:
            season_title = (
                "Specials"
                if normalized_season_number == 0
                else f"Season {normalized_season_number}"
            )
            season_copy.setdefault("title", show_title)
            season_copy.setdefault("season_title", season_title)
            season_copy.setdefault(
                "season_header_title",
                season_copy.get("season_title") or season_title,
            )
            season_copy.setdefault("media_id", media_id)
            season_copy.setdefault("media_type", MediaTypes.SEASON.value)
            season_copy.setdefault("source", source)
            season_copy.setdefault("image", show_image)
        provider_related_seasons.append(season_copy)
        seen_season_numbers.add(normalized_season_number)

    local_related_seasons = _build_local_related_seasons(
        media_id,
        source,
        show_title,
        show_image,
    )
    for local_season in local_related_seasons:
        if local_season["season_number"] not in seen_season_numbers:
            provider_related_seasons.append(local_season)

    provider_related_seasons.sort(
        key=lambda season: (
            season.get("season_number") is None,
            season.get("season_number")
            if season.get("season_number") is not None
            else 999999,
        ),
    )
    related["seasons"] = provider_related_seasons

    provider_external_ids = getattr(show_item, "provider_external_ids", {}) or {}
    tv_metadata.update(
        {
            "media_id": media_id,
            "source": source,
            "media_type": MediaTypes.TV.value,
            "title": show_title,
            "image": show_image,
            "synopsis": tv_metadata.get("synopsis")
            or ((show_item.manual_metadata or {}).get("synopsis") if show_item else "")
            or "",
            "genres": tv_metadata.get("genres")
            or getattr(show_item, "genres", [])
            or [],
            "related": related,
            "tvdb_id": (
                tv_metadata.get("tvdb_id")
                or provider_external_ids.get("tvdb_id")
            ),
        },
    )
    tv_metadata.setdefault("source_url", "")
    tv_metadata.setdefault("external_links", {})
    return tv_metadata


def _save_provider_metadata_status(item, status):
    """Persist provider metadata status when it changes."""
    if item is None or item.provider_metadata_status == status:
        return item
    item.provider_metadata_status = status
    item.save(update_fields=["provider_metadata_status"])
    return item


def _flat_anime_episode_preview_candidates(user, metadata_resolution_result=None):
    """Return grouped providers to try for flat MAL anime episode previews."""
    candidates = []

    def add_candidate(provider):
        if (
            provider in metadata_resolution.GROUPED_ANIME_PROVIDERS
            and metadata_resolution.provider_is_enabled(provider)
            and provider not in candidates
        ):
            candidates.append(provider)

    if metadata_resolution_result is not None:
        add_candidate(metadata_resolution_result.display_provider)

    if user and getattr(user, "is_authenticated", False):
        add_candidate(
            metadata_resolution.metadata_default_source(
                user,
                MediaTypes.ANIME.value,
            ),
        )

    add_candidate(Sources.TVDB.value)
    add_candidate(Sources.TMDB.value)
    return candidates


def _flat_anime_preview_season_numbers(
    grouped_series_metadata,
    grouped_preview_target,
):
    """Return grouped season numbers needed for a flat anime episode slice."""
    if not isinstance(grouped_preview_target, dict):
        return []

    season_number = grouped_preview_target.get("season_number")
    try:
        season_number = int(season_number) if season_number is not None else None
    except (TypeError, ValueError):
        season_number = None

    if season_number is not None and season_number >= 0:
        return [season_number]

    related = grouped_series_metadata.get("related") if isinstance(grouped_series_metadata, dict) else {}
    seasons = related.get("seasons") if isinstance(related, dict) else []
    target_total = grouped_preview_target.get("episode_total")
    try:
        target_total = int(target_total) if target_total is not None else None
    except (TypeError, ValueError):
        target_total = None
    episode_offset = grouped_preview_target.get("episode_offset") or 0
    try:
        episode_offset = int(episode_offset)
    except (TypeError, ValueError):
        episode_offset = 0

    sortable_seasons = []
    for season in seasons:
        if not isinstance(season, dict):
            continue
        raw_number = season.get("season_number")
        try:
            normalized_number = int(raw_number)
        except (TypeError, ValueError):
            continue
        sortable_seasons.append((normalized_number, season))

    sortable_seasons.sort(key=lambda pair: pair[0])

    season_numbers = []
    covered_episodes = 0
    for normalized_number, season in sortable_seasons:
        if normalized_number < 0 or normalized_number == 0:
            continue
        season_numbers.append(normalized_number)
        episode_count = (
            season.get("episode_count")
            or (season.get("details") or {}).get("episodes")
            or season.get("max_progress")
        )
        try:
            episode_count = int(episode_count)
        except (TypeError, ValueError):
            episode_count = None
        if episode_count is not None:
            covered_episodes += episode_count
        if (
            target_total is not None
            and episode_count is not None
            and covered_episodes >= episode_offset + target_total
        ):
            break

    if season_numbers:
        return season_numbers

    if any(number == 0 for number, _season in sortable_seasons):
        return [0]
    return []


def _flat_anime_preview_episode_rows(grouped_preview, grouped_preview_target):
    """Return mapped episode rows for a flat anime preview."""
    if not isinstance(grouped_preview, dict) or not isinstance(grouped_preview_target, dict):
        return []

    target_total = grouped_preview_target.get("episode_total")
    try:
        target_total = int(target_total) if target_total is not None else None
    except (TypeError, ValueError):
        target_total = None
    episode_offset = grouped_preview_target.get("episode_offset") or 0
    try:
        episode_offset = int(episode_offset)
    except (TypeError, ValueError):
        episode_offset = 0
    target_season = grouped_preview_target.get("season_number")
    try:
        target_season = int(target_season) if target_season is not None else None
    except (TypeError, ValueError):
        target_season = None

    def season_rows(season_number):
        season_payload = grouped_preview.get(f"season/{season_number}")
        if not isinstance(season_payload, dict):
            return []
        season_title = season_payload.get("season_title") or (
            "Specials" if season_number == 0 else f"Season {season_number}"
        )
        rows = []
        for raw_episode in season_payload.get("episodes") or []:
            provider_episode_number = raw_episode.get("episode_number")
            if provider_episode_number is None:
                continue
            rows.append(
                {
                    "season_number": season_number,
                    "season_title": season_title,
                    "provider_episode_number": provider_episode_number,
                    "raw_episode": raw_episode,
                },
            )
        return rows

    ordered_rows = []
    if target_season is not None and target_season >= 0:
        ordered_rows.extend(season_rows(target_season))
    else:
        season_numbers = _flat_anime_preview_season_numbers(
            grouped_preview,
            grouped_preview_target,
        )
        if not season_numbers:
            season_numbers = sorted(
                {
                    int(key.split("/", 1)[1])
                    for key, value in grouped_preview.items()
                    if key.startswith("season/")
                    and isinstance(value, dict)
                    and key.split("/", 1)[1].lstrip("-").isdigit()
                    and int(key.split("/", 1)[1]) >= 0
                },
            )
        for season_number in season_numbers:
            ordered_rows.extend(season_rows(season_number))

    if episode_offset > 0:
        ordered_rows = ordered_rows[episode_offset:]
    if target_total is not None:
        ordered_rows = ordered_rows[:target_total]

    mapped_rows = []
    for mapped_episode_number, row in enumerate(ordered_rows, start=1):
        mapped_rows.append(
            {
                **row,
                "mapped_episode_number": mapped_episode_number,
            },
        )
    return mapped_rows


def _build_flat_anime_episode_preview(
    request,
    *,
    detail_item,
    media_id,
    base_metadata,
    metadata_resolution_result=None,
    retry_max_retries: int | None = None,
    on_persistence_deferred=None,
):
    """Return a read-only mapped episode slice for flat MAL anime details."""
    if not isinstance(base_metadata, dict):
        return None

    identity_source = detail_item.source if detail_item else base_metadata.get("source")
    identity_media_type = (
        detail_item.media_type if detail_item else base_metadata.get("media_type")
    )
    if identity_source != Sources.MAL.value or identity_media_type != MediaTypes.ANIME.value:
        return None

    if base_metadata.get("episodes"):
        return None

    provider = None
    provider_media_id = None
    grouped_preview = None
    grouped_preview_target = None

    if metadata_resolution_result is not None:
        provider = metadata_resolution_result.display_provider
        provider_media_id = metadata_resolution_result.provider_media_id
        grouped_preview = metadata_resolution_result.grouped_preview
        grouped_preview_target = metadata_resolution_result.grouped_preview_target

    if (
        provider not in metadata_resolution.GROUPED_ANIME_PROVIDERS
        or not provider_media_id
        or not isinstance(grouped_preview, dict)
        or not isinstance(grouped_preview_target, dict)
    ):
        provider = None
        provider_media_id = None
        grouped_preview = None
        grouped_preview_target = None

        for candidate in _flat_anime_episode_preview_candidates(
            request.user if request.user.is_authenticated else None,
            metadata_resolution_result,
        ):
            candidate_media_id = (
                metadata_resolution.resolve_provider_media_id(
                    detail_item,
                    candidate,
                    route_media_type=MediaTypes.ANIME.value,
                    persistence_mode="best_effort",
                    retry_max_retries=retry_max_retries,
                    on_deferred=on_persistence_deferred,
                )
                if detail_item is not None
                else anime_mapping.resolve_provider_series_id(media_id, candidate)
            )
            if not candidate_media_id:
                continue

            preview_target = metadata_resolution._grouped_preview_target(
                item=detail_item,
                media_id=media_id,
                provider=candidate,
                provider_media_id=candidate_media_id,
                base_metadata=base_metadata,
                grouped_preview=None,
            )
            if not isinstance(preview_target, dict):
                continue

            season_number = preview_target.get("season_number")
            if season_number is None:
                continue

            season_numbers = _flat_anime_preview_season_numbers(
                {},
                preview_target,
            )
            if season_numbers:
                preview_payload = services.get_media_metadata(
                    "tv_with_seasons",
                    candidate_media_id,
                    candidate,
                    season_numbers,
                )
            else:
                grouped_series_metadata = services.get_media_metadata(
                    MediaTypes.ANIME.value,
                    candidate_media_id,
                    candidate,
                )
                season_numbers = _flat_anime_preview_season_numbers(
                    grouped_series_metadata,
                    preview_target,
                )
                if not season_numbers:
                    continue
                preview_payload = services.get_media_metadata(
                    "tv_with_seasons",
                    candidate_media_id,
                    candidate,
                    season_numbers,
                )

            preview_payload = metadata_resolution._enrich_grouped_preview(
                preview_payload,
            )
            if not any(
                isinstance(preview_payload.get(f"season/{number}"), dict)
                for number in season_numbers
            ):
                continue
            preview_target = metadata_resolution._grouped_preview_target(
                item=detail_item,
                media_id=media_id,
                provider=candidate,
                provider_media_id=candidate_media_id,
                base_metadata=base_metadata,
                grouped_preview=preview_payload,
            )
            if not isinstance(preview_target, dict):
                continue

            provider = candidate
            provider_media_id = candidate_media_id
            grouped_preview = preview_payload
            grouped_preview_target = preview_target
            break

    if not isinstance(grouped_preview_target, dict) or not isinstance(grouped_preview, dict):
        return None

    preview_rows = _flat_anime_preview_episode_rows(
        grouped_preview,
        grouped_preview_target,
    )
    if not preview_rows:
        return None

    history_by_episode_key = defaultdict(list)
    item_by_episode_key = {}
    collection_entry_by_episode_key = {}
    rating_season_id_by_episode_key = {}
    preview_episode_keys = {
        (row["season_number"], row["provider_episode_number"])
        for row in preview_rows
    }
    preview_season_numbers = sorted({row["season_number"] for row in preview_rows})

    if request.user.is_authenticated:
        tracked_episodes = list(
            Episode.objects.filter(
                related_season__related_tv__user=request.user,
                item__media_id=provider_media_id,
                item__source=provider,
                item__media_type=MediaTypes.EPISODE.value,
                item__season_number__in=preview_season_numbers,
            )
            .select_related("item")
            .order_by("-end_date", "-id"),
        )

        for tracked_episode in tracked_episodes:
            episode_item = getattr(tracked_episode, "item", None)
            episode_key = (
                getattr(episode_item, "season_number", None),
                getattr(episode_item, "episode_number", None),
            )
            if None in episode_key or episode_key not in preview_episode_keys:
                continue
            history_by_episode_key[episode_key].append(tracked_episode)
            item_by_episode_key.setdefault(episode_key, episode_item)
            rating_season_id_by_episode_key.setdefault(
                episode_key,
                tracked_episode.related_season_id,
            )

        if item_by_episode_key:
            collection_entries = (
                CollectionEntry.objects.filter(
                    user=request.user,
                    item_id__in=[item.id for item in item_by_episode_key.values()],
                )
                .select_related("item")
                .order_by("-collected_at", "-id")
            )
            for entry in collection_entries:
                episode_key = (
                    entry.item.season_number,
                    entry.item.episode_number,
                )
                if (
                    None not in episode_key
                    and episode_key in preview_episode_keys
                    and episode_key not in collection_entry_by_episode_key
                ):
                    collection_entry_by_episode_key[episode_key] = entry

    episodes = []
    episode_backdrop = None
    tvdb_episode_images_by_season = {}
    if provider == Sources.TMDB.value:
        episode_backdrop = helpers.get_tmdb_backdrop_image(
            MediaTypes.TV.value,
            provider_media_id,
        )

    for row in preview_rows:
        raw_episode = row["raw_episode"]
        season_number = row["season_number"]
        provider_episode_number = row["provider_episode_number"]
        episode_key = (season_number, provider_episode_number)
        episode_number = row["mapped_episode_number"]
        tvdb_episode_image = None

        if provider == Sources.TMDB.value:
            if season_number not in tvdb_episode_images_by_season:
                season_payload = grouped_preview.get(f"season/{season_number}", {})
                season_tvdb_id = None
                if isinstance(season_payload, dict):
                    season_tvdb_id = season_payload.get("tvdb_id")
                if not season_tvdb_id and isinstance(grouped_preview, dict):
                    season_tvdb_id = grouped_preview.get("tvdb_id")
                tvdb_episode_images_by_season[season_number] = (
                    tmdb.get_tvdb_episode_image_map(
                        season_tvdb_id,
                        season_number,
                        tmdb_media_id=provider_media_id,
                    )
                )
            tvdb_episode_image = tvdb_episode_images_by_season.get(
                season_number,
                {},
            ).get(str(provider_episode_number))

        image, image_source = helpers.resolve_image_with_fallback(
            tmdb.get_image_url(raw_episode["still_path"])
            if raw_episode.get("still_path")
            else None,
            tvdb_episode_image,
            helpers.first_real_image(raw_episode.get("image"), default=None),
            episode_backdrop,
        )

        runtime_value = raw_episode.get("runtime")
        runtime = (
            tmdb.get_readable_duration(runtime_value)
            if isinstance(runtime_value, (int, float)) and runtime_value > 0
            else runtime_value
        )

        episodes.append(
            {
                "media_id": provider_media_id,
                "media_type": MediaTypes.EPISODE.value,
                "source": provider,
                "season_number": season_number,
                "episode_number": provider_episode_number,
                "display_episode_number": episode_number,
                "provider_episode_number": provider_episode_number,
                "season_title": row["season_title"],
                "air_date": bulk_episode_tracking.coerce_episode_datetime(
                    raw_episode.get("air_date"),
                ),
                "image": image,
                "image_source": image_source,
                "title": raw_episode.get("name")
                or raw_episode.get("title")
                or f"Episode {episode_number}",
                "overview": raw_episode.get("overview") or "",
                "runtime": runtime,
                "history": history_by_episode_key.get(episode_key, []),
                "item": item_by_episode_key.get(episode_key),
                "collection_entry": collection_entry_by_episode_key.get(
                    episode_key,
                ),
                "rating_season_id": rating_season_id_by_episode_key.get(
                    episode_key,
                ),
                "library_media_type": MediaTypes.ANIME.value,
            },
        )

    return episodes or None


@require_POST
def sync_metadata(request, source, media_type, media_id, season_number=None):
    """Refresh the metadata for a media item."""
    def _sync_redirect_response():
        if request.headers.get("HX-Request"):
            return HttpResponse(
                status=204,
                headers={
                    "HX-Redirect": request.POST["next"],
                },
            )
        return helpers.redirect_back(request)

    def _restore_cached_metadata(cache_key, cached_metadata, cached_ttl):
        if cached_metadata is None:
            return

        timeout = (
            cached_ttl
            if isinstance(cached_ttl, int | float) and cached_ttl > 0
            else settings.CACHE_TIMEOUT
        )
        cache.set(cache_key, cached_metadata, timeout=timeout)

    if source == Sources.MANUAL.value:
        msg = "Manual items cannot be synced."
        messages.error(request, msg)
        return HttpResponse(
            msg,
            status=400,
            headers={"HX-Redirect": request.POST.get("next", "/")},
        )

    tracking_media_type = metadata_resolution.get_tracking_media_type(
        media_type,
        source=source,
    )
    cache_key = f"{source}_{tracking_media_type}_{media_id}"
    if media_type == MediaTypes.SEASON.value:
        cache_key += f"_{season_number}"

    cached_metadata = cache.get(cache_key)
    ttl = cache.ttl(cache_key)
    logger.debug("%s - Cache TTL for: %s", cache_key, ttl)

    if ttl is not None and ttl > (settings.CACHE_TIMEOUT - 3):
        msg = "The data was recently synced, please wait a few seconds."
        messages.error(request, msg)
        logger.error(msg)
    else:
        deleted = cache.delete(cache_key)
        logger.debug("%s - Old cache deleted: %s", cache_key, deleted)

        try:
            metadata = services.get_media_metadata(
                media_type,
                media_id,
                source,
                [season_number],
            )
        except (requests.exceptions.RequestException, services.ProviderAPIError) as exc:
            _restore_cached_metadata(cache_key, cached_metadata, ttl)
            provider_label = Sources(source).label
            logger.warning(
                "metadata_manual_refresh_failed cache_key=%s media_id=%s source=%s error=%s",
                cache_key,
                media_id,
                source,
                exception_summary(exc),
            )
            if isinstance(exc, services.ProviderAPIError):
                msg = str(exc)
            else:
                msg = (
                    f"Could not sync with {provider_label} right now because the provider "
                    "could not be reached."
                )
            if cached_metadata is not None:
                msg += " Cached data has been kept."
            messages.error(request, msg)
            return _sync_redirect_response()

        # Extract number_of_pages for books
        number_of_pages = None
        if media_type == MediaTypes.BOOK.value:
            number_of_pages = metadata.get("max_progress") or metadata.get("details", {}).get("number_of_pages")

        item, _ = Item.objects.update_or_create(
            media_id=media_id,
            source=source,
            media_type=tracking_media_type,
            season_number=season_number,
            defaults={
                **Item.title_fields_from_metadata(metadata),
                "library_media_type": (
                    metadata.get("library_media_type")
                    or media_type
                ),
                "image": metadata["image"],
                "number_of_pages": number_of_pages,
            },
        )

        # Update number_of_pages if it wasn't set but we have it now
        if media_type == MediaTypes.BOOK.value and not item.number_of_pages and number_of_pages:
            item.number_of_pages = number_of_pages
            item.save(update_fields=["number_of_pages"])

        metadata_update_fields = metadata_utils.apply_item_genres(
            item,
            metadata_utils.extract_metadata_genres(metadata),
        )
        metadata_update_fields.extend(metadata_utils.apply_item_metadata(item, metadata))
        if metadata_update_fields:
            metadata_update_fields = list(dict.fromkeys(metadata_update_fields))
            item.metadata_fetched_at = timezone.now()
            metadata_update_fields.append("metadata_fetched_at")
            item.save(update_fields=metadata_update_fields)

        if source == Sources.IGDB.value and media_type == MediaTypes.GAME.value:
            try:
                game_length_services.refresh_game_lengths(
                    item,
                    igdb_metadata=metadata,
                    force=True,
                    fetch_hltb=True,
                )
            except Exception as exc:
                logger.warning(
                    "game_lengths_manual_refresh_failed item_id=%s media_id=%s error=%s",
                    item.id,
                    item.media_id,
                    exception_summary(exc),
                )
                messages.warning(
                    request,
                    "Game length metadata could not be refreshed. Cached data will be used if available.",
                )

        metadata_resolution.upsert_provider_links(
            item,
            metadata,
            provider=source,
            provider_media_type=tracking_media_type,
            season_number=season_number,
        )

        if trakt_popularity_service.supports_route_media_type(media_type):
            try:
                trakt_popularity_service.refresh_trakt_popularity(
                    item,
                    route_media_type=media_type,
                    force=True,
                )
            except Exception as exc:
                logger.warning(
                    "trakt_popularity_manual_refresh_failed item_id=%s media_id=%s error=%s",
                    item.id,
                    item.media_id,
                    exception_summary(exc),
                )
                messages.warning(
                    request,
                    "Trakt popularity metadata could not be refreshed. Cached data will be used if available.",
                )

        if source == Sources.TMDB.value and tracking_media_type in (
            MediaTypes.MOVIE.value,
            MediaTypes.TV.value,
            MediaTypes.SEASON.value,
        ):
            credits.sync_item_credits_from_metadata(item, metadata)

        title = metadata["title"]
        if season_number:
            title += f" - Season {season_number}"

        if media_type == MediaTypes.SEASON.value:
            # Store raw episodes before processing (for runtime extraction)
            raw_episodes = metadata.get("episodes", [])

            metadata["episodes"] = tmdb.process_episodes(
                metadata,
                [],
            )

            # Create a dictionary of existing episodes keyed by episode number
            existing_episodes = {
                ep.episode_number: ep
                for ep in Item.objects.filter(
                    source=source,
                    media_type=MediaTypes.EPISODE.value,
                    media_id=media_id,
                    season_number=season_number,
                )
            }

            episodes_to_update = []
            episode_count = 0

            # Create a lookup for raw episode data by episode_number
            raw_episode_map = {
                ep["episode_number"]: ep
                for ep in raw_episodes
            }

            for episode_data in metadata["episodes"]:
                episode_number = episode_data["episode_number"]
                if episode_number in existing_episodes:
                    episode_item = existing_episodes[episode_number]
                    title_fields = Item.title_fields_from_metadata(metadata)
                    episode_item.title = title_fields["title"]
                    episode_item.original_title = title_fields["original_title"]
                    episode_item.localized_title = title_fields["localized_title"]
                    episode_item.image = episode_data["image"]

                    # Extract and update release_datetime from TMDB air_date
                    air_date = episode_data.get("air_date")
                    if air_date is not None:
                        # air_date is already converted to datetime by process_episodes
                        # or it's None if TMDB returned null
                        # Use same logic as process_season_episodes: only store meaningful dates
                        if hasattr(air_date, "year") and air_date.year > 1900:
                            episode_item.release_datetime = air_date
                        else:
                            episode_item.release_datetime = None
                    # If air_date is None, don't update release_datetime (keep existing or None)

                    # Extract and update runtime_minutes from raw episode data
                    raw_episode = raw_episode_map.get(episode_number)
                    if raw_episode and raw_episode.get("runtime") is not None:
                        # Raw episode runtime is an integer (minutes) from TMDB
                        runtime_minutes = int(raw_episode["runtime"])
                        if runtime_minutes > 0:
                            episode_item.runtime_minutes = runtime_minutes

                    episodes_to_update.append(episode_item)
                    episode_count += 1

            logger.info(
                "Found %s existing episodes to update for %s",
                episode_count,
                title,
            )

            if episodes_to_update:
                updated_count = Item.objects.bulk_update(
                    episodes_to_update,
                    [
                        "title",
                        "original_title",
                        "localized_title",
                        "image",
                        "release_datetime",
                        "runtime_minutes",
                    ],
                    batch_size=100,
                )
                logger.info(
                    "Successfully updated %s episodes for %s (including release_datetime and runtime_minutes)",
                    updated_count,
                    title,
                )

        item.fetch_releases(delay=False)

        # Sync rating from Plex if user has Plex connected and webhooks configured
        _sync_plex_rating(request, item, media_type)

        msg = f"{title} was synced to {Sources(source).label} successfully."
        messages.success(request, msg)

    return _sync_redirect_response()


def _sync_plex_rating(request, item, media_type):
    """Sync user rating from Plex for a specific item.

    This is called when syncing metadata if the user has Plex connected
    and webhooks configured (indicating they want Plex integration).
    """
    from app.models import CollectionEntry, MediaTypes, Status
    from integrations import plex as plex_api

    # Check if user has Plex connected and webhooks configured
    plex_account = getattr(request.user, "plex_account", None)
    if not plex_account or not plex_account.plex_token:
        return

    # Check if user has webhooks configured (has plex_usernames set)
    if not getattr(request.user, "plex_usernames", None):
        return

    # Only sync ratings for Movies and TV shows
    if media_type not in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
        return

    logger.info("Attempting to sync Plex rating for media_type=%s", media_type)

    # Try to get rating key from cached CollectionEntry
    rating_key = None
    plex_uri = None

    collection_entry = CollectionEntry.objects.filter(
        user=request.user,
        item=item,
        plex_rating_key__isnull=False,
        plex_uri__isnull=False,
    ).first()

    if collection_entry:
        rating_key = collection_entry.plex_rating_key
        plex_uri = collection_entry.plex_uri
        logger.debug("Using cached Plex rating key for rating sync")
    else:
        # Search for item in Plex library
        try:
            resources = plex_api.list_resources(plex_account.plex_token)
        except Exception as exc:
            logger.debug(
                "Failed to list Plex resources for rating sync: %s",
                exception_summary(exc),
            )
            return

        # Get sections
        sections = plex_account.sections or []
        if not sections:
            try:
                sections = plex_api.list_sections(plex_account.plex_token)
            except Exception as exc:
                logger.debug(
                    "Failed to list Plex sections for rating sync: %s",
                    exception_summary(exc),
                )
                return

        # Find matching item in Plex
        for section in sections:
            section_type = (section.get("type") or "").lower()
            if media_type == MediaTypes.MOVIE.value and section_type != "movie":
                continue
            if media_type == MediaTypes.TV.value and section_type != "show":
                continue

            section_uri = section.get("uri")
            if not section_uri:
                continue

            try:
                # Search library items (first 100 should be enough for most cases)
                library_items, total = plex_api.fetch_section_all_items(
                    plex_account.plex_token,
                    section_uri,
                    str(section.get("key") or section.get("id")),
                    start=0,
                    size=100,
                )

                for plex_item in library_items:
                    # Extract external IDs
                    guids = plex_item.get("Guid", [])
                    if not guids:
                        single_guid = plex_item.get("guid")
                        if single_guid:
                            guids = [{"id": single_guid}]

                    external_ids = plex_api.extract_external_ids_from_guids(guids)

                    # Check if this matches our item
                    matches = False
                    if item.source == "tmdb" and external_ids.get("tmdb_id") == str(item.media_id):
                        matches = True
                    elif item.source == "imdb" and external_ids.get("imdb_id") == item.media_id:
                        matches = True
                    elif item.source == "tvdb" and external_ids.get("tvdb_id") == str(item.media_id):
                        matches = True

                    if matches:
                        rating_key = plex_item.get("ratingKey") or plex_item.get("ratingkey")
                        plex_uri = section_uri
                        logger.info("Found matching Plex item for rating sync")
                        break

                if rating_key:
                    break
            except Exception as exc:
                logger.debug(
                    "Failed to search Plex section for rating sync: %s",
                    exception_summary(exc),
                )
                continue

    if not rating_key or not plex_uri:
        logger.debug("Could not find Plex rating key for rating sync")
        return

    # Fetch metadata from Plex to get user rating
    # Use longer timeout for rating sync (30 seconds)
    try:
        plex_metadata = plex_api.fetch_metadata(
            plex_account.plex_token,
            plex_uri,
            str(rating_key),
            timeout=30,
        )
    except Exception as exc:
        logger.warning(
            "Failed to fetch Plex metadata for rating sync: %s",
            exception_summary(exc),
        )
        # Try HTTPS if HTTP failed, or vice versa
        if plex_uri.startswith("http://"):
            https_uri = plex_uri.replace("http://", "https://")
            logger.debug("Retrying Plex rating sync with HTTPS: %s", safe_url(https_uri))
            try:
                plex_metadata = plex_api.fetch_metadata(
                    plex_account.plex_token,
                    https_uri,
                    str(rating_key),
                    timeout=30,
                )
            except Exception as https_exc:
                logger.debug(
                    "HTTPS retry also failed during Plex rating sync: %s",
                    exception_summary(https_exc),
                )
                return
        elif plex_uri.startswith("https://"):
            http_uri = plex_uri.replace("https://", "http://")
            logger.debug("Retrying Plex rating sync with HTTP: %s", safe_url(http_uri))
            try:
                plex_metadata = plex_api.fetch_metadata(
                    plex_account.plex_token,
                    http_uri,
                    str(rating_key),
                    timeout=30,
                )
            except Exception as http_exc:
                logger.debug(
                    "HTTP retry also failed during Plex rating sync: %s",
                    exception_summary(http_exc),
                )
                return
        else:
            return

    if not plex_metadata:
        logger.debug("No Plex metadata returned for rating sync")
        return

    user_rating = plex_metadata.get("userRating")
    if user_rating is None:
        logger.debug("No userRating found in Plex metadata for rating sync")
        return

    # Check if this is a rating removal event (-1.0)
    try:
        rating_float = float(user_rating)
        if rating_float == -1.0:
            logger.info("Detected Plex rating removal event for media_type=%s", media_type)
            # Remove rating from existing instances only
            if media_type == MediaTypes.MOVIE.value:
                from app.models import Movie
                movie_instance = Movie.objects.filter(item=item, user=request.user).first()
                if movie_instance:
                    movie_instance.score = None
                    movie_instance.save(update_fields=["score"])
                    logger.info("Removed movie rating from Plex sync")
                else:
                    logger.debug("No movie instance found to remove Plex rating")
            elif media_type == MediaTypes.TV.value:
                from app.models import TV
                tv_instance = TV.objects.filter(item=item, user=request.user).first()
                if tv_instance:
                    tv_instance.score = None
                    tv_instance.save(update_fields=["score"])
                    logger.info("Removed TV rating from Plex sync")
                else:
                    logger.debug("No TV instance found to remove Plex rating")
            return
    except (TypeError, ValueError):
        logger.debug("Invalid rating value returned during Plex sync")
        return

    # Normalize rating (Plex userRating is typically 0-10, Yamtrack uses 0-10)
    if rating_float <= 10:
        normalized_rating = rating_float
    elif rating_float <= 100:
        normalized_rating = rating_float / 10
    else:
        logger.debug("Rating from Plex sync was out of expected range")
        return

    normalized_rating = round(normalized_rating, 1)
    if normalized_rating < 0 or normalized_rating > 10:
        logger.debug("Normalized Plex rating was out of range")
        return

    if normalized_rating is None:
        logger.debug("Invalid normalized rating returned during Plex sync")
        return

    # Apply rating to media instance
    if media_type == MediaTypes.MOVIE.value:
        from app.models import Movie
        movie_instance = Movie.objects.filter(item=item, user=request.user).first()
        if movie_instance:
            movie_instance.score = normalized_rating
            movie_instance.save(update_fields=["score"])
            logger.info("Synced Plex movie rating")
        else:
            # Create movie instance if it doesn't exist
            Movie.objects.create(
                item=item,
                user=request.user,
                status=Status.COMPLETED.value,
                progress=1,
                score=normalized_rating,
            )
            logger.info("Created movie instance from Plex rating sync")
    elif media_type == MediaTypes.TV.value:
        from app.models import TV
        tv_instance = TV.objects.filter(item=item, user=request.user).first()
        if tv_instance:
            tv_instance.score = normalized_rating
            tv_instance.save(update_fields=["score"])
            logger.info("Synced Plex TV rating")
        else:
            # Create TV instance if it doesn't exist
            TV.objects.create(
                item=item,
                user=request.user,
                status=Status.IN_PROGRESS.value,
                score=normalized_rating,
            )
            logger.info("Created TV instance from Plex rating sync")
