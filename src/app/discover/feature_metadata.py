"""Normalization helpers for richer Discover movie metadata features."""

from __future__ import annotations

from datetime import date, datetime

from app.models import CreditRoleType

KEYWORD_STOPLIST = {
    "3d",
    "aftercreditsstinger",
    "duringcreditsstinger",
    "film",
    "imax",
    "movie",
    "post credits scene",
    "post-credit scene",
    "postcreditsscene",
    "sequel",
}

SIGNAL_LABEL_STOPLIST = {
    *KEYWORD_STOPLIST,
    "feature film",
}

STUDIO_ALIASES = {
    "disneytoon studios": "disney",
    "illumination entertainment": "illumination",
    "pixar animation studios": "pixar",
    "walt disney animation studios": "disney",
    "walt disney pictures": "disney",
}


def _normalize_whitespace(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def normalize_keyword(value) -> str:
    """Return a normalized keyword or an empty string when unusable."""
    key = _normalize_whitespace(value).replace("-", " ")
    if not key or key in KEYWORD_STOPLIST:
        return ""
    return key


def normalize_studio(value) -> str:
    """Return a normalized studio label."""
    key = _normalize_whitespace(value).replace("&", "and")
    return STUDIO_ALIASES.get(key, key)


def normalize_collection(value) -> str:
    """Return a normalized collection label."""
    return _normalize_whitespace(value)


def normalize_person_name(value) -> str:
    """Return a normalized person name."""
    return _normalize_whitespace(value)


def normalize_certification(value) -> str:
    """Return a normalized rating bucket."""
    key = str(value or "").strip().upper()
    if not key:
        return ""
    if key in {"NR", "NOT RATED", "UNRATED"}:
        return "UNRATED"
    return key


def runtime_bucket_label(runtime_minutes) -> str:
    """Return the runtime bucket label used by Discover."""
    try:
        minutes = int(runtime_minutes)
    except (TypeError, ValueError):
        return ""

    if minutes <= 0 or minutes >= 999998:
        return ""
    if minutes < 90:
        return "<90"
    if minutes < 110:
        return "90_109"
    if minutes < 130:
        return "110_129"
    return "130_plus"


def release_decade_label(release_value) -> str:
    """Return decade label like 1990s from a date-ish value."""
    year = None
    if isinstance(release_value, datetime):
        year = release_value.year
    elif isinstance(release_value, date):
        year = release_value.year
    else:
        text = str(release_value or "").strip()
        if len(text) >= 4 and text[:4].isdigit():
            year = int(text[:4])

    if year is None or year <= 0:
        return ""
    return f"{(year // 10) * 10}s"


def normalize_features(values, normalizer) -> list[str]:
    """Normalize, dedupe, and preserve the original order of feature values."""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        cleaned = normalizer(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def is_director_credit(role_type: str, role: str, department: str) -> bool:
    """Return True when the credit identifies a director."""
    if role_type != CreditRoleType.CREW.value:
        return False
    return normalize_person_name(role) == "director" or normalize_person_name(department) == "directing"
