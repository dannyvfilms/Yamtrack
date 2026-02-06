"""Helpers for synchronizing cast/crew/studio metadata."""

from __future__ import annotations

from datetime import date

from django.db import transaction

from app.models import (
    CreditRoleType,
    ItemPersonCredit,
    ItemStudioCredit,
    Person,
    PersonGender,
    Sources,
    Studio,
)


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
                "sort_order": _as_int(row.get("order") or row.get("sort_order")),
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
                "sort_order": _as_int(row.get("order") or row.get("sort_order")),
            },
        )
    return normalized


@transaction.atomic
def sync_item_credits_from_metadata(item, metadata):
    """Persist cast/crew and studios for an item from normalized metadata."""
    if not item or item.source != Sources.TMDB.value or not isinstance(metadata, dict):
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
                source=Sources.TMDB.value,
                source_person_id=row["person_id"],
                defaults={
                    "name": row["name"] or "Unknown Person",
                    "image": row["image"],
                    "known_for_department": row["known_for_department"],
                    "gender": row["gender"],
                },
            )
            people_by_source_id[row["person_id"]] = person

        ItemPersonCredit.objects.filter(item=item).delete()
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
                source=Sources.TMDB.value,
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


@transaction.atomic
def upsert_person_profile(source, source_person_id, metadata):
    """Create or update a local person profile from provider metadata."""
    if source != Sources.TMDB.value or not source_person_id or not isinstance(metadata, dict):
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
