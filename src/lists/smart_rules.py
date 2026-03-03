"""Smart list rule normalization, option building, and item matching."""

from __future__ import annotations

import datetime
from collections.abc import Iterable

from django.apps import apps
from django.utils import timezone

from app.models import CollectionEntry, Item, ItemTag, MediaTypes, Sources, Status

SMART_FILTER_KEYS = (
    "status",
    "rating",
    "collection",
    "genre",
    "year",
    "release",
    "source",
    "search",
    "language",
    "country",
    "platform",
    "origin",
    "format",
    "tag",
    "tag_exclude",
)

SMART_FILTER_DEFAULTS = {
    "status": "all",
    "rating": "all",
    "collection": "all",
    "genre": "",
    "year": "",
    "release": "all",
    "source": "",
    "search": "",
    "language": "",
    "country": "",
    "platform": "",
    "origin": "",
    "format": "",
    "tag": "",
    "tag_exclude": "",
}

RATING_CHOICES = {"all", "rated", "not_rated"}
COLLECTION_CHOICES = {"all", "collected", "not_collected"}
RELEASE_CHOICES = {"all", "released", "not_released"}
SHOW_COLLECTION_MEDIA_TYPES = {
    MediaTypes.TV.value,
    MediaTypes.ANIME.value,
    MediaTypes.SEASON.value,
}
LANGUAGE_MEDIA_TYPES = {
    MediaTypes.TV.value,
    MediaTypes.MOVIE.value,
    MediaTypes.ANIME.value,
    MediaTypes.PODCAST.value,
}
COUNTRY_MEDIA_TYPES = LANGUAGE_MEDIA_TYPES
PLATFORM_MEDIA_TYPES = {MediaTypes.GAME.value}
ORIGIN_MEDIA_TYPES = {MediaTypes.MUSIC.value}
FORMAT_MEDIA_TYPES = {MediaTypes.BOOK.value, MediaTypes.MANGA.value, MediaTypes.COMIC.value}


def _normalize_filter_value(value) -> str:
    return str(value or "").strip().lower()


def _release_date_from_value(value):
    if value is None:
        return None
    if isinstance(value, datetime.date) and not hasattr(value, "hour"):
        return value
    if hasattr(value, "date"):
        try:
            if hasattr(value, "utcoffset") and timezone.is_aware(value):
                return timezone.localtime(value).date()
        except Exception:
            pass
        try:
            return value.date()
        except Exception:
            return None
    return None


def _matches_release_filter_value(release_value, filter_value: str, today):
    if filter_value == "all":
        return True
    release_date = _release_date_from_value(release_value)
    if not release_date:
        return filter_value == "not_released"
    if filter_value == "released":
        return release_date <= today
    if filter_value == "not_released":
        return release_date > today
    return True


def _extract_languages(item: Item) -> list[str]:
    languages = getattr(item, "languages", None) or []
    if isinstance(languages, list):
        return [str(value).strip() for value in languages if str(value).strip()]
    language_value = str(languages).strip()
    return [language_value] if language_value else []


def _extract_country(item: Item) -> str:
    country = getattr(item, "country", "")
    return str(country).strip()


def _extract_platforms(item: Item) -> list[str]:
    platforms = getattr(item, "platforms", None) or []
    if isinstance(platforms, list):
        return [str(value).strip() for value in platforms if str(value).strip()]
    platform_value = str(platforms).strip()
    return [platform_value] if platform_value else []


def _payload_get(payload, key: str, default=""):
    if hasattr(payload, "get"):
        return payload.get(key, default)
    if isinstance(payload, dict):
        return payload.get(key, default)
    return default


def _payload_getlist(payload, key: str) -> list[str]:
    if hasattr(payload, "getlist"):
        return [str(value) for value in payload.getlist(key)]

    if isinstance(payload, dict):
        value = payload.get(key)
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, Iterable):
            return [str(entry) for entry in value]
        return [str(value)]

    return []


def get_available_media_types(owner) -> list[str]:
    """Return enabled media types that can participate in smart rules."""
    if owner and hasattr(owner, "get_enabled_media_types"):
        enabled = list(owner.get_enabled_media_types())
    else:
        enabled = [
            media_type
            for media_type in MediaTypes.values
            if media_type != MediaTypes.EPISODE.value
        ]

    # Keep list smart rules at show/media granularity.
    enabled = [media_type for media_type in enabled if media_type != MediaTypes.EPISODE.value]

    # Remove duplicates while preserving order.
    deduped = []
    seen = set()
    for media_type in enabled:
        if media_type not in MediaTypes.values:
            continue
        if media_type in seen:
            continue
        seen.add(media_type)
        deduped.append(media_type)
    return deduped


def normalize_rule_payload(payload, owner):
    """Normalize and validate smart-rule payload for persistence/matching."""
    available_media_types = get_available_media_types(owner)

    selected_media_types = _payload_getlist(payload, "media_types") or _payload_getlist(
        payload,
        "type",
    )
    normalized_media_types = []
    seen = set()
    for media_type in selected_media_types:
        value = str(media_type).strip().lower()
        if value not in available_media_types:
            continue
        if value in seen:
            continue
        seen.add(value)
        normalized_media_types.append(value)

    status = str(_payload_get(payload, "status", "all") or "all").strip()
    if not status or status.lower() == "all" or status == "All":
        status = "all"
    elif status not in Status.values:
        status = "all"

    rating = str(_payload_get(payload, "rating", "all") or "all").strip().lower()
    if rating not in RATING_CHOICES:
        rating = "all"

    collection = str(_payload_get(payload, "collection", "all") or "all").strip().lower()
    if collection not in COLLECTION_CHOICES:
        collection = "all"

    release = str(_payload_get(payload, "release", "all") or "all").strip().lower()
    if release not in RELEASE_CHOICES:
        release = "all"

    year = str(_payload_get(payload, "year", "") or "").strip().lower()
    if year and year != "unknown" and not year.isdigit():
        year = ""

    source = str(_payload_get(payload, "source", "") or "").strip().lower()
    if source and source not in Sources.values:
        source = ""

    normalized = {
        "media_types": normalized_media_types,
        "status": status,
        "rating": rating,
        "collection": collection,
        "genre": str(_payload_get(payload, "genre", "") or "").strip(),
        "year": year,
        "release": release,
        "source": source,
        "search": str(_payload_get(payload, "search", "") or "").strip(),
        "language": str(_payload_get(payload, "language", "") or "").strip(),
        "country": str(_payload_get(payload, "country", "") or "").strip(),
        "platform": str(_payload_get(payload, "platform", "") or "").strip(),
        "origin": str(_payload_get(payload, "origin", "") or "").strip(),
        "format": str(_payload_get(payload, "format", "") or "").strip(),
        "tag": str(_payload_get(payload, "tag", "") or "").strip(),
        "tag_exclude": str(_payload_get(payload, "tag_exclude", "") or "").strip(),
    }
    return normalized


def normalize_list_rules(custom_list) -> dict:
    """Return normalized rules for a smart list, including excluded media types."""
    normalized_rules = normalize_rule_payload(
        {
            "media_types": custom_list.smart_media_types or [],
            **(custom_list.smart_filters or {}),
        },
        custom_list.owner,
    )

    excluded_media_types = {
        media_type
        for media_type in (custom_list.smart_excluded_media_types or [])
        if media_type in MediaTypes.values
    }
    if excluded_media_types:
        if normalized_rules["media_types"]:
            normalized_rules["media_types"] = [
                media_type
                for media_type in normalized_rules["media_types"]
                if media_type not in excluded_media_types
            ]
        else:
            normalized_rules["media_types"] = [
                media_type
                for media_type in get_available_media_types(custom_list.owner)
                if media_type not in excluded_media_types
            ]

    return normalized_rules


def _base_media_queryset(owner, media_type: str, status_filter: str = "all", search_query: str = ""):
    model = apps.get_model("app", media_type)
    if media_type == MediaTypes.EPISODE.value:
        queryset = model.objects.filter(related_season__user=owner)
        if status_filter != "all":
            queryset = queryset.filter(related_season__status=status_filter)
    else:
        queryset = model.objects.filter(user=owner)
        if status_filter != "all":
            queryset = queryset.filter(status=status_filter)

    if search_query:
        queryset = queryset.filter(item__title__icontains=search_query)

    return queryset.select_related("item")


def _target_media_types(owner, rules_media_types: list[str]) -> list[str]:
    available = get_available_media_types(owner)
    if rules_media_types:
        return [media_type for media_type in rules_media_types if media_type in available]
    return available


def _matches_item_filters(item: Item, rules: dict, today) -> bool:
    genre_filter = _normalize_filter_value(rules.get("genre"))
    year_filter = _normalize_filter_value(rules.get("year"))
    source_filter = _normalize_filter_value(rules.get("source"))
    language_filter = _normalize_filter_value(rules.get("language"))
    country_filter = _normalize_filter_value(rules.get("country"))
    platform_filter = _normalize_filter_value(rules.get("platform"))
    origin_filter = _normalize_filter_value(rules.get("origin"))
    release_filter = _normalize_filter_value(rules.get("release") or "all")

    if genre_filter:
        item_genres = getattr(item, "genres", None) or []
        if not any(_normalize_filter_value(genre) == genre_filter for genre in item_genres):
            return False

    if year_filter == "unknown":
        if getattr(item, "release_datetime", None):
            return False
    elif year_filter.isdigit():
        release_value = getattr(item, "release_datetime", None)
        release_year = getattr(release_value, "year", None) if release_value else None
        if release_year != int(year_filter):
            return False

    if source_filter and _normalize_filter_value(getattr(item, "source", "")) != source_filter:
        return False

    if release_filter and not _matches_release_filter_value(
        getattr(item, "release_datetime", None),
        release_filter,
        today,
    ):
        return False

    if language_filter:
        languages = _extract_languages(item)
        if not any(_normalize_filter_value(language) == language_filter for language in languages):
            return False

    if country_filter:
        country = _extract_country(item)
        if _normalize_filter_value(country) != country_filter:
            return False

    if origin_filter:
        origin = _extract_country(item)
        if _normalize_filter_value(origin) != origin_filter:
            return False

    if platform_filter:
        platforms = _extract_platforms(item)
        if not any(_normalize_filter_value(platform) == platform_filter for platform in platforms):
            return False

    format_filter = _normalize_filter_value(rules.get("format"))
    if format_filter:
        item_format = _normalize_filter_value(getattr(item, "format", "") or "")
        if item_format != format_filter:
            return False

    return True


def _matches_collection_filter(
    entry,
    media_type: str,
    collection_filter: str,
    collected_item_ids: set[int],
    collected_episode_pairs: set[tuple[str, str]],
) -> bool:
    if collection_filter == "all":
        return True

    item = getattr(entry, "item", None)
    if not item:
        return False

    has_collection = item.id in collected_item_ids
    if not has_collection and media_type in SHOW_COLLECTION_MEDIA_TYPES:
        has_collection = (str(item.media_id), str(item.source)) in collected_episode_pairs

    if collection_filter == "collected":
        return has_collection
    if collection_filter == "not_collected":
        return not has_collection
    return True


def _collection_filter_context(owner) -> tuple[set[int], set[tuple[str, str]]]:
    """Return cached collection lookup sets for smart list collection filtering."""
    collected_item_ids = set(
        CollectionEntry.objects.filter(user=owner).values_list("item_id", flat=True),
    )
    collected_episode_pairs = set(
        Item.objects.filter(
            id__in=collected_item_ids,
            media_type=MediaTypes.EPISODE.value,
        ).values_list("media_id", "source"),
    )
    return collected_item_ids, collected_episode_pairs


def collect_matching_item_ids(owner, normalized_rules: dict) -> set[int]:
    """Return matching Item IDs for a normalized smart-rule definition."""
    target_media_types = _target_media_types(owner, normalized_rules.get("media_types", []))
    if not target_media_types:
        return set()

    collection_filter = normalized_rules.get("collection", "all")
    rating_filter = normalized_rules.get("rating", "all")
    today = timezone.localdate()

    collected_item_ids, collected_episode_pairs = _collection_filter_context(owner)

    tag_filter = _normalize_filter_value(normalized_rules.get("tag"))
    tag_exclude = _normalize_filter_value(normalized_rules.get("tag_exclude"))
    tag_included_ids = None
    tag_excluded_ids = None
    if tag_filter:
        tag_included_ids = set(
            ItemTag.objects.filter(
                tag__user=owner,
                tag__name__iexact=tag_filter,
            ).values_list("item_id", flat=True)
        )
    if tag_exclude:
        tag_excluded_ids = set(
            ItemTag.objects.filter(
                tag__user=owner,
                tag__name__iexact=tag_exclude,
            ).values_list("item_id", flat=True)
        )

    matched_ids = set()
    for media_type in target_media_types:
        queryset = _base_media_queryset(
            owner=owner,
            media_type=media_type,
            status_filter=normalized_rules.get("status", "all"),
            search_query=normalized_rules.get("search", ""),
        )

        if rating_filter == "rated":
            queryset = queryset.filter(score__isnull=False)
        elif rating_filter == "not_rated":
            queryset = queryset.filter(score__isnull=True)

        for entry in queryset.iterator():
            item = getattr(entry, "item", None)
            if not item:
                continue

            if not _matches_item_filters(item, normalized_rules, today):
                continue

            if not _matches_collection_filter(
                entry=entry,
                media_type=media_type,
                collection_filter=collection_filter,
                collected_item_ids=collected_item_ids,
                collected_episode_pairs=collected_episode_pairs,
            ):
                continue

            if tag_included_ids is not None and item.id not in tag_included_ids:
                continue

            if tag_excluded_ids is not None and item.id in tag_excluded_ids:
                continue

            matched_ids.add(item.id)

    return matched_ids


def item_matches_rules(
    owner,
    item: Item,
    normalized_rules: dict,
    *,
    collection_context: tuple[set[int], set[tuple[str, str]]] | None = None,
) -> bool:
    """Return whether a single item currently matches a normalized rule set for an owner."""
    if not owner or not item:
        return False

    target_media_types = _target_media_types(owner, normalized_rules.get("media_types", []))
    if item.media_type not in target_media_types:
        return False

    queryset = _base_media_queryset(
        owner=owner,
        media_type=item.media_type,
        status_filter=normalized_rules.get("status", "all"),
        search_query=normalized_rules.get("search", ""),
    ).filter(item_id=item.id)

    rating_filter = normalized_rules.get("rating", "all")
    if rating_filter == "rated":
        queryset = queryset.filter(score__isnull=False)
    elif rating_filter == "not_rated":
        queryset = queryset.filter(score__isnull=True)

    today = timezone.localdate()
    if not _matches_item_filters(item, normalized_rules, today):
        return False

    tag_filter = _normalize_filter_value(normalized_rules.get("tag"))
    tag_exclude = _normalize_filter_value(normalized_rules.get("tag_exclude"))
    if tag_filter:
        has_tag = ItemTag.objects.filter(
            tag__user=owner,
            tag__name__iexact=tag_filter,
            item=item,
        ).exists()
        if not has_tag:
            return False
    if tag_exclude:
        has_excluded_tag = ItemTag.objects.filter(
            tag__user=owner,
            tag__name__iexact=tag_exclude,
            item=item,
        ).exists()
        if has_excluded_tag:
            return False

    collection_filter = normalized_rules.get("collection", "all")
    if collection_filter != "all":
        if collection_context is None:
            collection_context = _collection_filter_context(owner)
        collected_item_ids, collected_episode_pairs = collection_context
    else:
        collected_item_ids = set()
        collected_episode_pairs = set()

    for entry in queryset.iterator():
        if _matches_collection_filter(
            entry=entry,
            media_type=item.media_type,
            collection_filter=collection_filter,
            collected_item_ids=collected_item_ids,
            collected_episode_pairs=collected_episode_pairs,
        ):
            return True

    return False


def sync_smart_lists_for_item(owner, item: Item) -> dict[str, int]:
    """Incrementally sync smart-list membership for one owner/item combination."""
    if not owner or not item or not getattr(item, "id", None):
        return {"checked": 0, "added": 0, "removed": 0}

    from lists.models import CustomList, CustomListItem

    smart_lists = list(
        CustomList.objects.filter(owner=owner, is_smart=True).only(
            "id",
            "owner_id",
            "smart_media_types",
            "smart_excluded_media_types",
            "smart_filters",
        ),
    )
    if not smart_lists:
        return {"checked": 0, "added": 0, "removed": 0}

    smart_list_ids = [custom_list.id for custom_list in smart_lists]
    existing_memberships = set(
        CustomListItem.objects.filter(
            custom_list_id__in=smart_list_ids,
            item_id=item.id,
        ).values_list("custom_list_id", flat=True),
    )

    collection_context = None
    pending_adds = []
    pending_removals = []

    for custom_list in smart_lists:
        normalized_rules = normalize_list_rules(custom_list)
        if (
            normalized_rules.get("collection", "all") != "all"
            and collection_context is None
        ):
            collection_context = _collection_filter_context(owner)

        should_include = item_matches_rules(
            owner=owner,
            item=item,
            normalized_rules=normalized_rules,
            collection_context=collection_context,
        )
        currently_in_list = custom_list.id in existing_memberships

        if should_include and not currently_in_list:
            pending_adds.append(
                CustomListItem(
                    custom_list=custom_list,
                    item=item,
                    added_by=owner,
                ),
            )
        elif not should_include and currently_in_list:
            pending_removals.append(custom_list.id)

    if pending_adds:
        CustomListItem.objects.bulk_create(pending_adds, ignore_conflicts=True)
    if pending_removals:
        CustomListItem.objects.filter(
            custom_list_id__in=pending_removals,
            item_id=item.id,
        ).delete()

    return {
        "checked": len(smart_lists),
        "added": len(pending_adds),
        "removed": len(pending_removals),
    }


def build_rule_filter_data(owner, media_types: list[str], status: str, search: str):
    """Build menu options for smart-rule filters from matched candidate media."""
    target_media_types = _target_media_types(owner, media_types)

    item_ids = set()
    for media_type in target_media_types:
        queryset = _base_media_queryset(
            owner=owner,
            media_type=media_type,
            status_filter=status,
            search_query=search,
        )
        item_ids.update(queryset.values_list("item_id", flat=True))

    items = Item.objects.filter(id__in=item_ids).only(
        "genres",
        "release_datetime",
        "source",
        "languages",
        "country",
        "platforms",
        "format",
        "media_type",
    )

    _FORMAT_LABELS = {
        "hardcover": "Hardcover",
        "paperback": "Paperback",
        "ebook": "eBook",
        "audiobook": "Audiobook",
    }

    genres_set = set()
    years_set = set()
    sources_set = set()
    languages_set = set()
    countries_set = set()
    platforms_set = set()
    origins_set = set()
    formats_set = set()
    has_unknown_year = False

    for item in items:
        for genre in (item.genres or []):
            genre_value = str(genre).strip()
            if genre_value:
                genres_set.add(genre_value)

        release_datetime = getattr(item, "release_datetime", None)
        if release_datetime and getattr(release_datetime, "year", None):
            years_set.add(release_datetime.year)
        else:
            has_unknown_year = True

        if item.source:
            sources_set.add(item.source)

        languages_set.update(_extract_languages(item))

        country_value = _extract_country(item)
        if country_value:
            countries_set.add(country_value)
            origins_set.add(country_value)

        platforms_set.update(_extract_platforms(item))

        format_value = str(getattr(item, "format", "") or "").strip()
        if format_value:
            formats_set.add(format_value)

    source_labels = dict(Sources.choices)
    filter_data = {
        "genres": sorted(genres_set, key=lambda value: value.lower()),
        "years": [
            {"value": str(year), "label": str(year)}
            for year in sorted(years_set, reverse=True)
        ],
        "sources": [
            {"value": source, "label": source_labels.get(source, source)}
            for source in sorted(sources_set)
        ],
        "languages": [
            {
                "value": value,
                "label": value.upper() if len(value) <= 3 else value,
            }
            for value in sorted(languages_set)
        ],
        "countries": [
            {
                "value": value,
                "label": value.upper() if len(value) <= 3 else value,
            }
            for value in sorted(countries_set)
        ],
        "platforms": [
            {"value": value, "label": value}
            for value in sorted(platforms_set, key=lambda value: value.lower())
        ],
        "origins": [
            {
                "value": value,
                "label": value.upper() if len(value) <= 3 else value,
            }
            for value in sorted(origins_set)
        ],
        "show_languages": any(media_type in LANGUAGE_MEDIA_TYPES for media_type in target_media_types),
        "show_countries": any(media_type in COUNTRY_MEDIA_TYPES for media_type in target_media_types),
        "show_platforms": any(media_type in PLATFORM_MEDIA_TYPES for media_type in target_media_types),
        "show_origins": any(media_type in ORIGIN_MEDIA_TYPES for media_type in target_media_types),
        "formats": [
            {"value": value, "label": _FORMAT_LABELS.get(value, value.title())}
            for value in sorted(formats_set, key=lambda val: val.lower())
        ],
        "show_formats": any(media_type in FORMAT_MEDIA_TYPES for media_type in target_media_types),
    }

    if has_unknown_year:
        filter_data["years"].append({"value": "unknown", "label": "Unknown"})

    from app.models import Tag

    filter_data["tags"] = list(
        Tag.objects.filter(user=owner)
        .values_list("name", flat=True)
        .order_by("name")
    )

    return filter_data
