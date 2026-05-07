"""Helpers for synchronizing person/studio metadata."""

from __future__ import annotations

from datetime import date

from django.db import transaction

from app.models import (
    CREDITS_BACKFILL_VERSION,
    CreditRoleType,
    Item,
    ItemPersonCredit,
    ItemStudioCredit,
    MediaTypes,
    MetadataBackfillField,
    MetadataBackfillState,
    Person,
    PersonGender,
    Sources,
    Studio,
)

TMDB_SHOW_REGULAR_CAST_SORT_ORDER_CUTOFF = 100


def _coerce_iso_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _coerce_gender(value):
    normalized = str(value or "").strip().lower()
    if normalized in {
        PersonGender.FEMALE.value,
        PersonGender.MALE.value,
        PersonGender.NON_BINARY.value,
    }:
        return normalized
    if normalized in {"1", "female", "f"}:
        return PersonGender.FEMALE.value
    if normalized in {"2", "male", "m"}:
        return PersonGender.MALE.value
    if normalized in {"3", "non-binary", "non_binary", "nb"}:
        return PersonGender.NON_BINARY.value
    return PersonGender.UNKNOWN.value


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_regular_show_cast_credit(source, sort_order):
    """Return whether a show-level cast credit should count as series-regular fallback."""
    if source != Sources.TMDB.value:
        return True
    return (
        sort_order is not None
        and sort_order < TMDB_SHOW_REGULAR_CAST_SORT_ORDER_CUTOFF
    )


def is_usable_tv_show_credit(source, role_type, sort_order):
    """Return whether a show-level TV credit is usable as attribution fallback."""
    if role_type != CreditRoleType.CAST.value:
        return True
    return is_regular_show_cast_credit(source, sort_order)


def current_credits_backfill_item_ids(item_ids):
    """Return item IDs whose credits are backed by the current TMDB strategy."""
    normalized_ids = []
    for item_id in item_ids or []:
        try:
            parsed = int(item_id)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            normalized_ids.append(parsed)
    if not normalized_ids:
        return set()
    return set(
        MetadataBackfillState.objects.filter(
            field=MetadataBackfillField.CREDITS,
            item_id__in=normalized_ids,
            give_up=False,
            fail_count=0,
            last_success_at__isnull=False,
            strategy_version__gte=CREDITS_BACKFILL_VERSION,
        ).values_list("item_id", flat=True),
    )


def usable_credits_backfill_item_ids(item_ids):
    """Return item IDs whose stored TMDB credits remain usable for reads."""
    normalized_ids = []
    for item_id in item_ids or []:
        try:
            parsed = int(item_id)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            normalized_ids.append(parsed)
    if not normalized_ids:
        return set()
    return set(
        MetadataBackfillState.objects.filter(
            field=MetadataBackfillField.CREDITS,
            item_id__in=normalized_ids,
            last_success_at__isnull=False,
            strategy_version__gte=CREDITS_BACKFILL_VERSION,
        ).values_list("item_id", flat=True),
    )


def missing_credits_backfill_item_ids(item_ids):
    """Return TMDB item IDs that still need credits backfill."""
    normalized_ids = []
    for item_id in item_ids or []:
        try:
            parsed = int(item_id)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            normalized_ids.append(parsed)
    normalized_ids = sorted(set(normalized_ids))
    if not normalized_ids:
        return []

    candidate_items = list(
        Item.objects.filter(
            id__in=normalized_ids,
            source=Sources.TMDB.value,
            media_type__in=[
                MediaTypes.MOVIE.value,
                MediaTypes.TV.value,
                MediaTypes.SEASON.value,
                MediaTypes.EPISODE.value,
            ],
        ).values("id", "media_type"),
    )
    if not candidate_items:
        return []

    candidate_ids = {row["id"] for row in candidate_items}
    media_type_by_id = {row["id"]: row["media_type"] for row in candidate_items}
    current_credit_ids = current_credits_backfill_item_ids(candidate_ids)

    person_credit_ids = set(
        ItemPersonCredit.objects.filter(item_id__in=candidate_ids).values_list("item_id", flat=True),
    )
    cast_credit_ids = set(
        ItemPersonCredit.objects.filter(
            item_id__in=candidate_ids,
            role_type=CreditRoleType.CAST.value,
        ).values_list("item_id", flat=True),
    )
    studio_credit_ids = set(
        ItemStudioCredit.objects.filter(item_id__in=candidate_ids).values_list("item_id", flat=True),
    )

    missing_ids = []
    for item_id in sorted(candidate_ids):
        media_type = media_type_by_id.get(item_id)
        has_people = item_id in person_credit_ids
        has_cast = item_id in cast_credit_ids
        has_studios = item_id in studio_credit_ids
        if media_type == MediaTypes.SEASON.value:
            if not has_cast or item_id not in current_credit_ids:
                missing_ids.append(item_id)
            continue
        if media_type == MediaTypes.TV.value:
            if not has_cast or not has_studios or item_id not in current_credit_ids:
                missing_ids.append(item_id)
            continue
        if media_type == MediaTypes.EPISODE.value:
            if not has_people or item_id not in current_credit_ids:
                missing_ids.append(item_id)
            continue
        if not has_people or not has_studios:
            missing_ids.append(item_id)
    return missing_ids


def should_count_tv_show_credit_for_episode(
    source,
    role_type,
    sort_order,
    season_has_usable_credits,
    show_has_current_credits,
):
    """Return whether a show-level TV credit should count for a played episode."""
    if season_has_usable_credits:
        return False
    if not show_has_current_credits:
        return False
    if sort_order is None:
        return True
    return is_usable_tv_show_credit(source, role_type, sort_order)


def _normalize_credit_rows(rows):
    normalized = []
    for row in rows or []:
        person_id = row.get("person_id") or row.get("id")
        if person_id is None:
            continue
        normalized.append(
            {
                "person_id": str(person_id),
                "name": (row.get("name") or "").strip(),
                "image": (row.get("image") or "").strip(),
                "known_for_department": (row.get("known_for_department") or "").strip(),
                "gender": _coerce_gender(row.get("gender")),
                "role": (row.get("role") or row.get("character") or row.get("job") or "").strip(),
                "department": (row.get("department") or "").strip(),
                "sort_order": _as_int(
                    row["order"] if "order" in row and row["order"] is not None
                    else row.get("sort_order")
                ),
            },
        )
    return normalized


def _normalize_studio_rows(rows):
    normalized = []
    for row in rows or []:
        studio_id = row.get("studio_id") or row.get("id")
        if studio_id is None:
            continue
        normalized.append(
            {
                "studio_id": str(studio_id),
                "name": (row.get("name") or "").strip(),
                "logo": (row.get("logo") or "").strip(),
                "sort_order": _as_int(
                    row["order"] if "order" in row and row["order"] is not None
                    else row.get("sort_order")
                ),
            },
        )
    return normalized


@transaction.atomic
def sync_item_credits_from_metadata(item, metadata):
    """Persist cast/crew and studios for an item from normalized metadata."""
    if not item or not isinstance(metadata, dict):
        return

    has_people_payload = "cast" in metadata or "crew" in metadata
    has_studio_payload = "studios_full" in metadata

    cast_rows = _normalize_credit_rows(metadata.get("cast", []))
    crew_rows = _normalize_credit_rows(metadata.get("crew", []))
    studio_rows = _normalize_studio_rows(metadata.get("studios_full", []))

    if has_people_payload:
        people_by_source_id = {}
        for row in cast_rows + crew_rows:
            person, _ = Person.objects.update_or_create(
                source=item.source,
                source_person_id=row["person_id"],
                defaults={
                    "name": row["name"] or "Unknown Person",
                    "image": row["image"],
                    "known_for_department": row["known_for_department"],
                    "gender": row["gender"],
                },
            )
            people_by_source_id[row["person_id"]] = person

        ItemPersonCredit.objects.filter(
            item=item,
            role_type__in=(
                CreditRoleType.CAST.value,
                CreditRoleType.CREW.value,
            ),
        ).delete()
        credits_to_create = []

        for row in cast_rows:
            person = people_by_source_id.get(row["person_id"])
            if not person:
                continue
            credits_to_create.append(
                ItemPersonCredit(
                    item=item,
                    person=person,
                    role_type=CreditRoleType.CAST.value,
                    role=row["role"],
                    department=row["department"],
                    sort_order=row["sort_order"],
                ),
            )

        for row in crew_rows:
            person = people_by_source_id.get(row["person_id"])
            if not person:
                continue
            credits_to_create.append(
                ItemPersonCredit(
                    item=item,
                    person=person,
                    role_type=CreditRoleType.CREW.value,
                    role=row["role"],
                    department=row["department"],
                    sort_order=row["sort_order"],
                ),
            )

        if credits_to_create:
            ItemPersonCredit.objects.bulk_create(credits_to_create, ignore_conflicts=True)

    if has_studio_payload:
        studios_by_source_id = {}
        for row in studio_rows:
            studio, _ = Studio.objects.update_or_create(
                source=item.source,
                source_studio_id=row["studio_id"],
                defaults={
                    "name": row["name"] or "Unknown Studio",
                    "logo": row["logo"],
                },
            )
            studios_by_source_id[row["studio_id"]] = studio

        ItemStudioCredit.objects.filter(item=item).delete()
        studio_links = []
        for row in studio_rows:
            studio = studios_by_source_id.get(row["studio_id"])
            if not studio:
                continue
            studio_links.append(
                ItemStudioCredit(
                    item=item,
                    studio=studio,
                    sort_order=row["sort_order"],
                ),
            )
        if studio_links:
            ItemStudioCredit.objects.bulk_create(studio_links, ignore_conflicts=True)


def _normalize_author_rows(rows):
    normalized = []
    for row in rows or []:
        person_id = row.get("person_id") or row.get("id")
        if person_id is None:
            continue
        normalized.append(
            {
                "person_id": str(person_id),
                "name": (row.get("name") or "").strip(),
                "image": (row.get("image") or "").strip(),
                "known_for_department": (
                    row.get("known_for_department")
                    or row.get("department")
                    or "Author"
                ).strip(),
                "gender": _coerce_gender(row.get("gender")),
                "role": (row.get("role") or "").strip(),
                "department": (row.get("department") or "").strip(),
                "sort_order": _as_int(
                    row["order"] if "order" in row and row["order"] is not None
                    else row.get("sort_order")
                ),
            },
        )
    return normalized


@transaction.atomic
def sync_item_author_credits(item, authors_full):
    """Persist author credits for an item from normalized metadata."""
    if not item:
        return

    author_rows = _normalize_author_rows(authors_full)
    ItemPersonCredit.objects.filter(
        item=item,
        role_type=CreditRoleType.AUTHOR.value,
    ).delete()

    if not author_rows:
        return

    people_by_source_id = {}
    for row in author_rows:
        person, _ = Person.objects.update_or_create(
            source=item.source,
            source_person_id=row["person_id"],
            defaults={
                "name": row["name"] or "Unknown Person",
                "image": row["image"],
                "known_for_department": row["known_for_department"],
                "gender": row["gender"],
            },
        )
        people_by_source_id[row["person_id"]] = person

    credits_to_create = []
    for row in author_rows:
        person = people_by_source_id.get(row["person_id"])
        if not person:
            continue
        credits_to_create.append(
            ItemPersonCredit(
                item=item,
                person=person,
                role_type=CreditRoleType.AUTHOR.value,
                role=row["role"],
                department=row["department"],
                sort_order=row["sort_order"],
            ),
        )

    if credits_to_create:
        ItemPersonCredit.objects.bulk_create(credits_to_create, ignore_conflicts=True)


@transaction.atomic
def upsert_person_profile(source, source_person_id, metadata):
    """Create or update a local person profile from provider metadata."""
    if (
        source not in Sources.values
        or not source_person_id
        or not isinstance(metadata, dict)
    ):
        return None

    person, _ = Person.objects.update_or_create(
        source=source,
        source_person_id=str(source_person_id),
        defaults={
            "name": (metadata.get("name") or "").strip() or "Unknown Person",
            "image": (metadata.get("image") or "").strip(),
            "known_for_department": (metadata.get("known_for_department") or "").strip(),
            "biography": (metadata.get("biography") or "").strip(),
            "gender": _coerce_gender(metadata.get("gender")),
            "birth_date": _coerce_iso_date(metadata.get("birth_date")),
            "death_date": _coerce_iso_date(metadata.get("death_date")),
            "place_of_birth": (metadata.get("place_of_birth") or "").strip(),
        },
    )
    return person
