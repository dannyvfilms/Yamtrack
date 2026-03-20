"""Metadata provider resolution and grouped-anime helper utilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings

from app import config
from app.models import (
    Item,
    ItemProviderLink,
    MediaTypes,
    MetadataProviderPreference,
    Sources,
)
from app.providers import services
from integrations import anime_mapping

GROUPED_ANIME_PROVIDERS = {
    Sources.TMDB.value,
    Sources.TVDB.value,
}

PROVIDER_EXTERNAL_ID_KEYS = {
    Sources.TMDB.value: "tmdb_id",
    Sources.TVDB.value: "tvdb_id",
    Sources.MAL.value: "mal_id",
}


@dataclass(slots=True)
class MetadataResolutionResult:
    """Resolved provider payload for a details page."""

    display_provider: str
    identity_provider: str
    mapping_status: str
    header_metadata: dict
    grouped_preview: dict | None
    provider_media_id: str | None
    preference: MetadataProviderPreference | None = None
    grouped_preview_target: dict | None = None


def provider_is_enabled(provider: str) -> bool:
    """Return whether a provider is configured for live use."""
    if provider == Sources.TVDB.value:
        return bool(settings.TVDB_API_KEY)
    return True


def available_metadata_sources(media_type: str) -> list[Sources]:
    """Return configured metadata sources for a route media type."""
    candidates = []
    for source in config.get_sources(media_type) or []:
        provider = source.value if isinstance(source, Sources) else str(source)
        if provider_is_enabled(provider):
            candidates.append(
                source if isinstance(source, Sources) else Sources(provider),
            )
    return candidates


def metadata_default_source(user, media_type: str) -> str:
    """Return the effective default display provider for a media type."""
    provider = None
    if user and getattr(user, "is_authenticated", False):
        if media_type == MediaTypes.TV.value:
            provider = getattr(user, "tv_metadata_source_default", None)
        elif media_type == MediaTypes.ANIME.value:
            provider = getattr(user, "anime_metadata_source_default", None)

    provider = provider or config.get_default_source_name(media_type).value
    if provider_is_enabled(provider):
        return provider

    available = available_metadata_sources(media_type)
    return available[0].value if available else provider


def get_tracking_media_type(
    media_type: str,
    *,
    source: str | None = None,
    identity_media_type: str | None = None,
) -> str:
    """Return the persisted Item/media model type for a route."""
    if (
        media_type == MediaTypes.ANIME.value
        and (
            identity_media_type == MediaTypes.TV.value
            or source in GROUPED_ANIME_PROVIDERS
        )
    ):
        return MediaTypes.TV.value
    return identity_media_type or media_type


def get_library_media_type(
    media_type: str,
    *,
    library_media_type: str | None = None,
) -> str:
    """Return the library bucket used for the route."""
    return library_media_type or media_type


def is_grouped_anime_route(
    media_type: str,
    *,
    source: str | None = None,
    identity_media_type: str | None = None,
    library_media_type: str | None = None,
) -> bool:
    """Return True when an anime route is backed by TV structure."""
    return (
        media_type == MediaTypes.ANIME.value
        and get_tracking_media_type(
            media_type,
            source=source,
            identity_media_type=identity_media_type,
        )
        == MediaTypes.TV.value
        and get_library_media_type(
            media_type,
            library_media_type=library_media_type,
        )
        == MediaTypes.ANIME.value
    )


def item_uses_grouped_anime(item: Item | None) -> bool:
    """Return True when an Item is a grouped anime title stored on TV rows."""
    return bool(
        item
        and item.media_type == MediaTypes.TV.value
        and item.library_media_type == MediaTypes.ANIME.value
    )


def provider_route_media_type(route_media_type: str, provider: str) -> str:
    """Return the provider-facing media type for a routed request."""
    if (
        route_media_type == MediaTypes.ANIME.value
        and provider in GROUPED_ANIME_PROVIDERS
    ):
        return MediaTypes.ANIME.value
    return route_media_type


def get_media_model_name(media_type: str, *, source: str | None = None) -> str:
    """Return the concrete media model backing a route."""
    if media_type == MediaTypes.ANIME.value and source in GROUPED_ANIME_PROVIDERS:
        return MediaTypes.TV.value
    return media_type


def get_preferred_provider(
    user,
    item: Item | None,
    route_media_type: str,
    *,
    requested_source: str | None = None,
) -> str:
    """Return the effective display provider for a detail route."""
    identity_provider = (
        item.source
        if item
        else requested_source or metadata_default_source(user, route_media_type)
    )
    allowed = {source.value for source in available_metadata_sources(route_media_type)}

    preference = None
    if (
        user
        and getattr(user, "is_authenticated", False)
        and item is not None
        and route_media_type in {MediaTypes.TV.value, MediaTypes.ANIME.value}
    ):
        preference = MetadataProviderPreference.objects.filter(
            user=user,
            item=item,
        ).first()

    if (
        preference
        and preference.provider in allowed
        and provider_is_enabled(preference.provider)
    ):
        provider = preference.provider
    elif identity_provider in allowed and provider_is_enabled(identity_provider):
        provider = identity_provider
    else:
        provider = metadata_default_source(user, route_media_type)

    if provider not in allowed:
        provider = identity_provider
    return provider


def _normalize_external_ids(metadata: dict | None, *, provider: str | None = None) -> dict[str, str]:
    """Return a normalized external-ID payload from provider metadata."""
    metadata = metadata or {}
    external_ids = dict(metadata.get("provider_external_ids") or {})

    if metadata.get("tvdb_id"):
        external_ids.setdefault("tvdb_id", str(metadata["tvdb_id"]))

    media_id = metadata.get("media_id")
    if provider == Sources.TMDB.value and media_id:
        external_ids.setdefault("tmdb_id", str(media_id))
    if provider == Sources.TVDB.value and media_id:
        external_ids.setdefault("tvdb_id", str(media_id))
    if provider == Sources.MAL.value and media_id:
        external_ids.setdefault("mal_id", str(media_id))

    return {
        key: str(value)
        for key, value in external_ids.items()
        if value not in (None, "")
    }


def upsert_provider_links(
    item: Item | None,
    metadata: dict | None,
    *,
    provider: str | None = None,
    provider_media_type: str | None = None,
    season_number: int | None = None,
    episode_offset: int | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Persist cross-provider IDs discovered in provider metadata."""
    if item is None or not isinstance(metadata, dict):
        return {}

    normalized_provider = provider or metadata.get("source") or item.source
    normalized_media_type = (
        provider_media_type
        or metadata.get("identity_media_type")
        or metadata.get("media_type")
        or item.media_type
    )

    external_ids = _normalize_external_ids(metadata, provider=normalized_provider)
    if extra_metadata:
        metadata_payload = dict(extra_metadata)
    else:
        metadata_payload = {}

    if normalized_provider and metadata.get("media_id"):
        link_defaults = {
            "provider_media_id": str(metadata["media_id"]),
            "metadata": metadata_payload,
        }
        if episode_offset is not None:
            link_defaults["episode_offset"] = episode_offset
        provider_link, _ = ItemProviderLink.objects.update_or_create(
            item=item,
            provider=normalized_provider,
            provider_media_type=normalized_media_type,
            season_number=season_number,
            defaults=link_defaults,
        )
        external_key = PROVIDER_EXTERNAL_ID_KEYS.get(provider_link.provider)
        if external_key and provider_link.provider_media_id:
            external_ids.setdefault(external_key, provider_link.provider_media_id)

    for candidate_provider, external_key in PROVIDER_EXTERNAL_ID_KEYS.items():
        external_id = external_ids.get(external_key)
        if not external_id:
            continue
        ItemProviderLink.objects.update_or_create(
            item=item,
            provider=candidate_provider,
            provider_media_type=normalized_media_type,
            season_number=season_number,
            defaults={
                "provider_media_id": external_id,
                "metadata": metadata_payload,
            },
        )

    if external_ids:
        merged_external_ids = dict(item.provider_external_ids or {})
        if merged_external_ids != (merged_external_ids | external_ids):
            item.provider_external_ids = merged_external_ids | external_ids
            item.save(update_fields=["provider_external_ids"])

    return external_ids


def resolve_provider_media_id(
    item: Item | None,
    provider: str,
    *,
    route_media_type: str,
    season_number: int | None = None,
) -> str | None:
    """Return the mapped provider ID for a tracked item."""
    if item is None:
        return None

    provider_media_type = get_tracking_media_type(
        route_media_type,
        source=provider,
    )

    if item.source == provider and item.media_type == provider_media_type:
        return str(item.media_id)

    provider_link = (
        ItemProviderLink.objects.filter(
            item=item,
            provider=provider,
            provider_media_type=provider_media_type,
            season_number=season_number,
        )
        .order_by("-updated_at")
        .first()
    )
    if provider_link:
        return provider_link.provider_media_id

    external_key = PROVIDER_EXTERNAL_ID_KEYS.get(provider)
    if external_key:
        external_ids = item.provider_external_ids or {}
        if external_ids.get(external_key):
            return str(external_ids[external_key])

    if (
        item.source == Sources.MAL.value
        and route_media_type == MediaTypes.ANIME.value
        and provider in GROUPED_ANIME_PROVIDERS
    ):
        mapped_series_id = anime_mapping.resolve_provider_series_id(
            item.media_id,
            provider,
        )
        if mapped_series_id:
            ItemProviderLink.objects.update_or_create(
                item=item,
                provider=provider,
                provider_media_type=provider_media_type,
                season_number=season_number,
                defaults={"provider_media_id": str(mapped_series_id)},
            )
            return str(mapped_series_id)

    return None


def _overlay_header_metadata(base_metadata: dict, overlay_metadata: dict, *, provider: str) -> dict:
    """Overlay display metadata onto the tracked metadata shell."""
    if not isinstance(base_metadata, dict):
        return overlay_metadata

    tracking_external_links = dict(base_metadata.get("external_links") or {})
    display_external_links = dict(overlay_metadata.get("external_links") or {})
    merged_external_links = dict(tracking_external_links)
    merged_external_links.update(
        {
            name: url
            for name, url in display_external_links.items()
            if name and url
        },
    )

    merged = dict(base_metadata)
    for key in (
        "title",
        "original_title",
        "localized_title",
        "image",
        "synopsis",
        "genres",
        "score",
        "score_count",
        "tvdb_id",
    ):
        if key in overlay_metadata and overlay_metadata.get(key) not in (None, ""):
            merged[key] = overlay_metadata[key]

    merged["display_source"] = provider
    merged["display_source_url"] = overlay_metadata.get("source_url")
    merged["display_external_links"] = display_external_links
    merged["tracking_source_url"] = base_metadata.get("source_url")
    merged["tracking_external_links"] = tracking_external_links
    merged["external_links"] = merged_external_links
    merged["source_url"] = base_metadata.get("source_url")
    merged.setdefault("identity_media_type", base_metadata.get("identity_media_type"))
    merged.setdefault("library_media_type", base_metadata.get("library_media_type"))
    return merged


def _provider_series_id(entry: dict, provider: str) -> str | None:
    """Return the grouped-series ID for a mapping entry/provider pair."""
    if provider == Sources.TVDB.value:
        value = entry.get("tvdb_id")
        return str(value) if value not in (None, "") else None

    if provider == Sources.TMDB.value:
        for key in ("tmdb_show_id", "tmdb_id", "tmdb_tv_id"):
            value = entry.get(key)
            if value not in (None, ""):
                return str(value)

    return None


def _provider_season_number(entry: dict, provider: str) -> int | None:
    """Return the mapped grouped-season number for a mapping entry."""
    keys = ["tvdb_season"] if provider == Sources.TVDB.value else [
        "tmdb_season",
        "tvdb_season",
        "season",
    ]
    for key in keys:
        value = entry.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _provider_episode_offset(entry: dict, provider: str) -> int:
    """Return the mapped grouped-season episode offset for a mapping entry."""
    keys = ["tvdb_epoffset"] if provider == Sources.TVDB.value else [
        "tmdb_epoffset",
        "tvdb_epoffset",
    ]
    for key in keys:
        value = entry.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _safe_int(value) -> int | None:
    """Return an int for scalar provider values when possible."""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _enrich_grouped_preview(grouped_preview: dict | None) -> dict | None:
    """Fill grouped-preview season cards from fetched season payloads when needed."""
    if not isinstance(grouped_preview, dict):
        return grouped_preview

    related = grouped_preview.get("related")
    seasons = related.get("seasons") if isinstance(related, dict) else None
    if not isinstance(seasons, list):
        return grouped_preview

    enriched_seasons = []
    for season in seasons:
        if not isinstance(season, dict):
            enriched_seasons.append(season)
            continue

        merged = dict(season)
        season_number = merged.get("season_number")
        season_payload = (
            grouped_preview.get(f"season/{season_number}")
            if season_number is not None
            else None
        )
        if isinstance(season_payload, dict):
            payload_details = season_payload.get("details") or {}
            if merged.get("max_progress") in (None, ""):
                merged["max_progress"] = season_payload.get("max_progress")
            if merged.get("episode_count") in (None, ""):
                merged["episode_count"] = payload_details.get("episodes")
            merged_details = dict(merged.get("details") or {})
            if merged_details.get("episodes") in (None, ""):
                merged_details["episodes"] = (
                    merged.get("episode_count")
                    or payload_details.get("episodes")
                    or season_payload.get("max_progress")
                )
            if merged.get("first_air_date") in (None, ""):
                merged["first_air_date"] = (
                    merged_details.get("first_air_date")
                    or payload_details.get("first_air_date")
                )
            merged["details"] = merged_details

        enriched_seasons.append(merged)

    grouped_preview = dict(grouped_preview)
    grouped_preview["related"] = dict(related or {})
    grouped_preview["related"]["seasons"] = enriched_seasons
    return grouped_preview


def _grouped_preview_target(
    *,
    item: Item | None,
    media_id: str,
    provider: str,
    provider_media_id: str | None,
    base_metadata: dict,
    grouped_preview: dict | None,
) -> dict | None:
    """Return the grouped season/episode target for a flat MAL anime entry."""
    if provider not in GROUPED_ANIME_PROVIDERS or not provider_media_id:
        return None

    identity_provider = item.source if item else base_metadata.get("source")
    identity_media_type = item.media_type if item else base_metadata.get("media_type")
    if (
        identity_provider != Sources.MAL.value
        or identity_media_type != MediaTypes.ANIME.value
    ):
        return None

    mapping_entries = anime_mapping.find_entries_for_mal_id(media_id)
    matching_entries = [
        entry
        for entry in mapping_entries
        if _provider_series_id(entry, provider) == str(provider_media_id)
    ]
    if not matching_entries:
        return None

    mapping_entry = matching_entries[0]
    season_number = _provider_season_number(mapping_entry, provider)
    episode_offset = _provider_episode_offset(mapping_entry, provider)
    episode_total = _safe_int((base_metadata.get("details") or {}).get("episodes"))
    if episode_total is None:
        episode_total = _safe_int(base_metadata.get("max_progress"))

    target = {
        "season_number": season_number,
        "episode_offset": episode_offset,
        "episode_total": episode_total,
    }
    if season_number is not None:
        target["episode_start"] = episode_offset + 1
        if episode_total is not None:
            target["episode_end"] = episode_offset + episode_total

    if not isinstance(grouped_preview, dict) or season_number is None:
        return target

    related = grouped_preview.get("related") or {}
    seasons = related.get("seasons") if isinstance(related, dict) else []
    season_payload = grouped_preview.get(f"season/{season_number}")
    if not isinstance(season_payload, dict):
        season_payload = next(
            (
                season
                for season in seasons
                if isinstance(season, dict)
                and season.get("season_number") == season_number
            ),
            None,
        )

    if isinstance(season_payload, dict):
        payload_details = season_payload.get("details") or {}
        target["season_title"] = (
            season_payload.get("season_title")
            or ("Specials" if season_number == 0 else f"Season {season_number}")
        )
        target["season_episode_count"] = (
            _safe_int(season_payload.get("episode_count"))
            or _safe_int(payload_details.get("episodes"))
            or _safe_int(season_payload.get("max_progress"))
        )
        target["first_air_date"] = (
            season_payload.get("first_air_date")
            or payload_details.get("first_air_date")
        )
    else:
        target["season_title"] = (
            "Specials" if season_number == 0 else f"Season {season_number}"
        )

    return target


def _annotate_grouped_preview_target(
    grouped_preview: dict | None,
    grouped_preview_target: dict | None,
) -> dict | None:
    """Mark the grouped-preview season card that the flat anime maps into."""
    if not isinstance(grouped_preview, dict) or not isinstance(grouped_preview_target, dict):
        return grouped_preview

    target_season = grouped_preview_target.get("season_number")
    related = grouped_preview.get("related")
    seasons = related.get("seasons") if isinstance(related, dict) else None
    if not isinstance(seasons, list):
        return grouped_preview

    annotated_seasons = []
    for season in seasons:
        if not isinstance(season, dict):
            annotated_seasons.append(season)
            continue

        merged = dict(season)
        if merged.get("season_number") == target_season:
            merged["is_mapped_target"] = True
            merged["mapped_episode_start"] = grouped_preview_target.get("episode_start")
            merged["mapped_episode_end"] = grouped_preview_target.get("episode_end")
            merged["mapped_episode_total"] = grouped_preview_target.get("episode_total")
        annotated_seasons.append(merged)

    grouped_preview = dict(grouped_preview)
    grouped_preview["related"] = dict(related or {})
    grouped_preview["related"]["seasons"] = annotated_seasons
    return grouped_preview


def resolve_detail_metadata(
    user,
    *,
    item: Item | None,
    route_media_type: str,
    media_id: str,
    source: str,
    base_metadata: dict,
) -> MetadataResolutionResult:
    """Resolve the detail-page display provider and overlay metadata when mapped."""
    provider = get_preferred_provider(
        user,
        item,
        route_media_type,
        requested_source=source,
    )
    preference = None
    if user and getattr(user, "is_authenticated", False) and item is not None:
        preference = MetadataProviderPreference.objects.filter(
            user=user,
            item=item,
        ).first()

    identity_provider = item.source if item else source
    mapping_status = "identity"
    provider_media_id = media_id if provider == identity_provider else None
    header_metadata = base_metadata
    grouped_preview = None
    grouped_preview_target = None

    if provider != identity_provider:
        provider_media_id = resolve_provider_media_id(
            item,
            provider,
            route_media_type=route_media_type,
        )
        if provider_media_id:
            overlay_metadata = services.get_media_metadata(
                provider_route_media_type(route_media_type, provider),
                provider_media_id,
                provider,
            )
            header_metadata = _overlay_header_metadata(
                base_metadata,
                overlay_metadata,
                provider=provider,
            )
            mapping_status = "mapped"
            if route_media_type == MediaTypes.ANIME.value and provider in GROUPED_ANIME_PROVIDERS:
                grouped_preview = services.get_media_metadata(
                    "tv_with_seasons",
                    provider_media_id,
                    provider,
                    [season.get("season_number") for season in (overlay_metadata.get("related", {}) or {}).get("seasons", []) if season.get("season_number") is not None],
                )
                grouped_preview = _enrich_grouped_preview(grouped_preview)
                grouped_preview_target = _grouped_preview_target(
                    item=item,
                    media_id=media_id,
                    provider=provider,
                    provider_media_id=provider_media_id,
                    base_metadata=base_metadata,
                    grouped_preview=grouped_preview,
                )
                grouped_preview = _annotate_grouped_preview_target(
                    grouped_preview,
                    grouped_preview_target,
                )
        else:
            mapping_status = "missing"
    elif item is not None and isinstance(base_metadata, dict):
        upsert_provider_links(
            item,
            base_metadata,
            provider=identity_provider,
            provider_media_type=get_tracking_media_type(
                route_media_type,
                source=identity_provider,
            ),
        )

    return MetadataResolutionResult(
        display_provider=provider,
        identity_provider=identity_provider,
        mapping_status=mapping_status,
        header_metadata=header_metadata,
        grouped_preview=grouped_preview,
        provider_media_id=provider_media_id,
        preference=preference,
        grouped_preview_target=grouped_preview_target,
    )
