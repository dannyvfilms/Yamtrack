"""Storyteller importer for book reading progress.

Storyteller exposes an OAuth device-code flow plus a small REST API:

- ``POST /api/v2/device/start``       start device authorization
- ``POST /api/v2/device/token``       poll for the access token
- ``GET  /api/v2/books``              list books on the server
- ``GET  /api/v2/books/{uuid}/positions``  current reading position

Each position carries a Readium locator whose
``locator.locations.totalProgression`` is the 0-1 reading fraction. We turn
that into a Yamtrack book entry: in-progress while reading, and completed once
the fraction reaches the account's ``finished_threshold`` (default 0.95).
"""

import hashlib
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any

import requests
from django.conf import settings
from django.utils import timezone

import app
from app import helpers as app_helpers
from app.log_safety import exception_summary
from app.models import MediaTypes, Sources, Status
from app.providers import services
from integrations.imports.helpers import MediaImportError, decrypt
from integrations.models import StorytellerAccount

logger = logging.getLogger(__name__)

HTTP_BAD_REQUEST = 400
AUTH_TIMEOUT = 15
API_TIMEOUT = 20
TITLE_MATCH_THRESHOLD = 0.72
BOOK_METADATA_PROVIDER_ORDER = (
    Sources.HARDCOVER.value,
    Sources.OPENLIBRARY.value,
)


class StorytellerClientError(Exception):
    """Base Storyteller client error."""


class StorytellerAuthError(StorytellerClientError):
    """Storyteller auth failure (invalid/expired token)."""


class StorytellerClient:
    """Thin HTTP client for the Storyteller REST API."""

    def __init__(self, server_url: str, token: str | None = None):
        """Initialize with the server base URL and optional bearer token."""
        self.server_url = server_url.rstrip("/")
        self.token = token

    def _url(self, path: str) -> str:
        return f"{self.server_url}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    def _get(self, path: str):
        response = requests.get(
            self._url(path),
            headers=self._headers(),
            timeout=API_TIMEOUT,
        )
        if response.status_code in (401, 403):
            msg = "Storyteller token is invalid or expired"
            raise StorytellerAuthError(msg)
        if response.status_code >= HTTP_BAD_REQUEST:
            msg = f"Storyteller request failed ({response.status_code}) for {path}"
            raise StorytellerClientError(msg)
        return response.json()

    def start_device_auth(self) -> dict[str, Any]:
        """Begin the device authorization flow."""
        response = requests.post(
            self._url("/api/v2/device/start"),
            headers={**self._headers(), "content-type": "application/json"},
            data="{}",
            timeout=AUTH_TIMEOUT,
        )
        if response.status_code >= HTTP_BAD_REQUEST:
            msg = f"Could not start Storyteller login ({response.status_code})"
            raise StorytellerClientError(msg)
        return response.json()

    def poll_device_token(self, device_code: str):
        """Poll for the access token. Returns (data, status_code)."""
        response = requests.post(
            self._url("/api/v2/device/token"),
            headers={**self._headers(), "content-type": "application/json"},
            json={"device_code": device_code},
            timeout=AUTH_TIMEOUT,
        )
        try:
            data = response.json()
        except ValueError:
            data = {}
        return data, response.status_code

    def get_books(self) -> list[dict[str, Any]]:
        """Return the list of books on the server."""
        data = self._get("/api/v2/books")
        if isinstance(data, dict):
            return data.get("books") or data.get("results") or []
        return data or []

    def get_position(self, book_uuid: str):
        """Return the current reading position for a book, or None."""
        try:
            return self._get(f"/api/v2/books/{book_uuid}/positions")
        except StorytellerAuthError:
            raise
        except StorytellerClientError:
            return None


def importer(identifier, user, mode):  # noqa: ARG001
    """Import Storyteller reading progress for a user."""
    return StorytellerImporter(user).import_data()


class StorytellerImporter:
    """Import reading progress from a Storyteller server."""

    def __init__(self, user):
        """Initialize importer and validate account access."""
        self.user = user
        try:
            self.account = user.storyteller_account
        except StorytellerAccount.DoesNotExist as error:
            msg = "Connect Storyteller before importing"
            raise MediaImportError(msg) from error

        if not self.account.auth_token:
            msg = "Connect Storyteller before importing"
            raise MediaImportError(msg)

        token = decrypt(self.account.auth_token)
        self.client = StorytellerClient(self.account.server_url, token)
        self.warnings = []
        self.enable_provider_enrichment = not settings.TESTING

    def import_data(self):
        """Import currently-reading books and their progress."""
        imported_counts = defaultdict(int)
        self._library_items = self._build_library_index()

        try:
            books = self.client.get_books()
        except StorytellerAuthError as error:
            self._mark_broken(str(error))
            raise MediaImportError(str(error)) from error
        except StorytellerClientError as error:
            self._mark_broken(str(error))
            raise MediaImportError(str(error)) from error

        for book in books:
            uuid = book.get("uuid") or book.get("id")
            if not uuid:
                continue

            # The books list embeds the reading position; only fall back to the
            # per-book endpoint when it's absent.
            if "position" in book:
                position = book["position"]
            else:
                try:
                    position = self.client.get_position(str(uuid))
                except StorytellerAuthError as error:
                    self._mark_broken(str(error))
                    raise MediaImportError(str(error)) from error

            progression = self._extract_progression(position)
            if progression is None or progression <= 0:
                # Not started yet; we only track books the user is reading.
                continue

            media = self._upsert_book(book, str(uuid), progression, position)
            if media:
                imported_counts[MediaTypes.BOOK.value] += 1

        self.account.last_sync_at = timezone.now()
        self.account.connection_broken = False
        self.account.last_error_message = ""
        self.account.save(
            update_fields=[
                "last_sync_at",
                "connection_broken",
                "last_error_message",
                "updated_at",
            ],
        )

        return dict(imported_counts), "\n".join(dict.fromkeys(self.warnings))

    def _mark_broken(self, message: str):
        self.account.connection_broken = True
        self.account.last_error_message = message
        self.account.save(
            update_fields=[
                "connection_broken",
                "last_error_message",
                "updated_at",
            ],
        )

    def _extract_progression(self, position):
        """Pull the 0-1 total progression fraction out of a position payload."""
        if not isinstance(position, dict):
            return None
        locator = position.get("locator")
        if not isinstance(locator, dict):
            return None
        locations = locator.get("locations")
        if not isinstance(locations, dict):
            return None
        value = locations.get("totalProgression")
        try:
            progression = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(1.0, progression))

    def _position_timestamp(self, position):
        """Return the position's timestamp (ms) as a datetime, or None."""
        if not isinstance(position, dict):
            return None
        return self._parse_datetime(position.get("timestamp"))

    def _upsert_book(self, book, uuid, progression, position):
        title = (book.get("title") or f"Storyteller {uuid}").strip()
        authors_list = self._extract_author_names(book)
        storyteller_pages = self._extract_page_count(book)

        existing_item = self._find_existing_library_item(title, authors_list)
        resolved = (
            self._resolve_provider_item(title, authors_list)
            if existing_item is None and self.enable_provider_enrichment
            else None
        )

        if existing_item is not None:
            # The book is already in the user's library (under any source); reuse
            # it so we update progress in place instead of creating a duplicate.
            item = existing_item
            if not item.number_of_pages and storyteller_pages:
                item.number_of_pages = storyteller_pages
                item.save(update_fields=["number_of_pages"])
            number_of_pages = item.number_of_pages or storyteller_pages
        elif resolved:
            # Reuse the real provider item (e.g. hardcover/openlibrary) so the
            # book de-duplicates against the existing library and its details
            # page resolves natively.
            provider_source, provider_media_id, provider_metadata = resolved
            item = self._upsert_provider_item(
                provider_source,
                provider_media_id,
                title,
                provider_metadata,
                fallback_pages=storyteller_pages,
            )
            number_of_pages = item.number_of_pages or storyteller_pages
        else:
            # No provider match: keep a local Storyteller item. The details page
            # is served from local data via services._storyteller_book.
            item = self._upsert_local_item(
                uuid,
                title,
                authors_list,
                book,
                storyteller_pages,
            )
            number_of_pages = item.number_of_pages

        is_finished = self._is_finished(book, progression)
        if number_of_pages:
            if is_finished:
                progress_value = number_of_pages
            else:
                progress_value = max(1, round(progression * number_of_pages))
        else:
            # No page count available: fall back to storing whole-percent progress.
            progress_value = 100 if is_finished else max(1, round(progression * 100))

        position_time = self._position_timestamp(position)
        existing = (
            app.models.Book.objects.filter(user=self.user, item=item)
            .only("start_date", "end_date", "notes")
            .first()
        )
        existing_start = existing.start_date if existing else None
        existing_end = existing.end_date if existing else None
        start_date = existing_start or position_time or timezone.now()
        if is_finished:
            status = Status.COMPLETED.value
            end_date = existing_end or position_time or timezone.now()
        else:
            status = Status.IN_PROGRESS.value
            end_date = None

        # Preserve any notes the user already wrote on a pre-existing entry.
        notes = (existing.notes if existing else "") or "Synced from Storyteller"

        media, _ = app.models.Book.objects.update_or_create(
            user=self.user,
            item=item,
            defaults={
                "progress": progress_value,
                "status": status,
                "start_date": start_date,
                "end_date": end_date,
                "notes": notes,
            },
        )
        return media

    def _upsert_provider_item(
        self,
        source,
        media_id,
        fallback_title,
        metadata,
        fallback_pages=None,
    ):
        """Create or reuse the real provider item; never clobber an existing one."""
        provider_title = metadata.get("title") or fallback_title
        title_fields = app.models.Item.title_fields_from_metadata(
            {"title": provider_title},
            fallback_title=provider_title,
        )
        details = metadata.get("details") or {}
        pages = metadata.get("max_progress") or fallback_pages
        defaults = {
            **title_fields,
            "title": provider_title,
            "authors": self._extract_provider_authors(metadata),
            "isbn": self._normalize_list(details.get("isbn")),
            "publishers": self._first(
                details.get("publishers") or details.get("publisher"),
            ),
            "genres": self._normalize_list(metadata.get("genres")),
            "number_of_pages": pages,
            "release_datetime": app_helpers.extract_release_datetime(metadata),
            "series_name": metadata.get("series_name"),
            "series_position": metadata.get("series_position"),
            "metadata_fetched_at": timezone.now(),
        }
        image = metadata.get("image")
        if image and image != settings.IMG_NONE:
            defaults["image"] = image

        item, created = app.models.Item.objects.get_or_create(
            media_id=str(media_id),
            source=source,
            media_type=MediaTypes.BOOK.value,
            defaults=defaults,
        )
        # Backfill the page count on an existing item if it's missing, since we
        # need it for progress and it's a low-risk single field.
        if not created and not item.number_of_pages and pages:
            item.number_of_pages = pages
            item.save(update_fields=["number_of_pages"])
        return item

    def _upsert_local_item(self, uuid, title, authors_list, book, storyteller_pages):
        """Create or update a local Storyteller-source item (no provider match)."""
        media_id = self._stable_media_id(self.account.server_url, uuid)
        title_fields = app.models.Item.title_fields_from_metadata(
            {"title": title},
            fallback_title=title,
        )
        series_name, series_position = self._extract_series(book)
        item, _ = app.models.Item.objects.update_or_create(
            media_id=media_id,
            source=Sources.STORYTELLER.value,
            media_type=MediaTypes.BOOK.value,
            defaults={
                **title_fields,
                "title": title,
                "authors": authors_list,
                "genres": self._extract_tags(book),
                "number_of_pages": storyteller_pages,
                "series_name": series_name,
                "series_position": series_position,
                "image": settings.IMG_NONE,
                "format": "ebook",
                "metadata_fetched_at": timezone.now(),
            },
        )
        return item

    def _stable_media_id(self, server_url: str, uuid: str) -> str:
        value = f"{server_url.strip().lower()}::{uuid}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]

    def _build_library_index(self):
        """Return the user's existing book items (any source) for dedup matching."""
        items_by_id = {}
        books = app.models.Book.objects.filter(user=self.user).select_related("item")
        for book in books:
            items_by_id[book.item_id] = book.item
        return list(items_by_id.values())

    def _find_existing_library_item(self, title, authors):
        """Find a book already in the user's library by title (and author)."""
        weak = None
        for item in getattr(self, "_library_items", []):
            if not self._titles_match(title, item.title):
                continue
            verdict = self._classify_authors(authors, item.authors or [])
            if verdict == "match":
                return item
            if verdict == "unknown" and weak is None:
                weak = item
        return weak

    def _is_finished(self, book, progression):
        """Treat a book as finished by reading progress or Storyteller status."""
        if progression is not None and progression >= self.account.finished_threshold:
            return True
        status_name = ((book.get("status") or {}).get("name") or "").strip().lower()
        return status_name in {"read", "finished", "completed"}

    def _extract_page_count(self, book):
        """Return a usable page count from the Storyteller book payload."""
        ebook = book.get("ebook") or {}
        readaloud = book.get("readaloud") or {}
        candidates = (
            ebook.get("pageCount"),
            book.get("pageCount"),
            readaloud.get("pageCount"),
        )
        for value in candidates:
            try:
                pages = int(value)
            except (TypeError, ValueError):
                continue
            if pages > 0:
                return pages
        return None

    def _extract_series(self, book):
        """Return (series_name, series_position) from the Storyteller payload."""
        series = book.get("series")
        if not isinstance(series, list) or not series:
            return None, None
        # Prefer the featured series, otherwise the first one listed.
        entry = next((s for s in series if s.get("featured")), series[0])
        name = str(entry.get("name") or "").strip() or None
        raw_position = entry.get("position")
        try:
            position = float(raw_position) if raw_position is not None else None
        except (TypeError, ValueError):
            position = None
        return name, position

    def _extract_tags(self, book):
        """Return tag names from the Storyteller payload as genres."""
        tags = book.get("tags") or []
        return self._normalize_list(
            [tag.get("name") for tag in tags if isinstance(tag, dict)],
        )

    def _extract_author_names(self, book):
        narrators = {
            self._normalize_name(n.get("name"))
            for n in book.get("narrators") or []
            if isinstance(n, dict) and n.get("name")
        }

        raw_authors = book.get("authors")
        if not raw_authors:
            fallback = book.get("author") or book.get("authorName")
            raw_authors = [fallback] if fallback else []
        if not isinstance(raw_authors, list):
            raw_authors = [raw_authors]

        authors = []
        narrator_only = []
        for raw_author in raw_authors:
            if isinstance(raw_author, dict):
                value = raw_author.get("name") or raw_author.get("author")
            else:
                value = raw_author
            normalized = str(value or "").strip()
            if not normalized:
                continue
            # The payload sometimes lists narrators among authors; drop them so
            # the author used for matching is the real writer.
            if self._normalize_name(normalized) in narrators:
                narrator_only.append(normalized)
            else:
                authors.append(normalized)

        deduped = list(dict.fromkeys(authors))
        # If every listed author was also a narrator, keep them rather than
        # returning nothing.
        return deduped or list(dict.fromkeys(narrator_only))

    def _resolve_provider_item(self, title: str, authors: list[str]):
        """Match a Storyteller book to a real book provider.

        Returns ``(source, media_id, metadata)`` for the matched provider item,
        or ``None`` when nothing matches.
        """
        if not title:
            return None

        author_hint = authors[0] if authors else ""
        queries = []
        if author_hint:
            queries.append(f"{title} {author_hint}".strip())
        queries.append(title)

        # Score every candidate and keep the best across providers. Tiers:
        #   3 = author confirmed AND has a cover
        #   2 = author confirmed, no cover
        #   1 = title matches, author unknown (provider had no author to check)
        best_tier, best_result = 0, None
        seen = set()
        for provider_source in BOOK_METADATA_PROVIDER_ORDER:
            for query in queries:
                normalized_query = query.strip()
                if not normalized_query or (provider_source, normalized_query) in seen:
                    continue
                seen.add((provider_source, normalized_query))

                tier, result = self._match_provider_query(
                    provider_source,
                    normalized_query,
                    title,
                    authors,
                )
                if tier > best_tier:
                    best_tier, best_result = tier, result
                    if best_tier == 3:  # noqa: PLR2004 - can't do better
                        return best_result

        return best_result

    def _match_provider_query(self, provider_source, query, title, authors):
        """Search one provider; return ``(tier, result)`` for the best candidate.

        Candidates whose author conflicts with the book's author are rejected.
        Returns ``(0, None)`` when nothing usable is found.
        """
        try:
            response = services.search(MediaTypes.BOOK.value, query, 1, provider_source)
        except Exception as error:  # noqa: BLE001
            logger.debug(
                "Storyteller metadata search failed provider=%s error=%s",
                provider_source,
                exception_summary(error),
            )
            return 0, None

        results = response.get("results", []) if isinstance(response, dict) else []
        best_tier, best_result = 0, None
        for candidate in self._title_candidates(results, title):
            media_id = candidate.get("media_id")
            if not media_id:
                continue

            try:
                metadata = services.get_media_metadata(
                    MediaTypes.BOOK.value,
                    str(media_id),
                    provider_source,
                )
            except Exception as error:  # noqa: BLE001
                logger.debug(
                    "Storyteller metadata fetch failed provider=%s error=%s",
                    provider_source,
                    exception_summary(error),
                )
                continue

            if not self._titles_match(title, str(metadata.get("title") or "")):
                continue

            verdict = self._author_verdict(authors, metadata)
            if verdict == "conflict":
                continue
            tier = (
                (3 if self._has_cover(metadata) else 2)
                if verdict == "match"
                else 1
            )

            if tier > best_tier:
                best_tier = tier
                best_result = (provider_source, str(media_id), metadata)
                if best_tier == 3:  # noqa: PLR2004 - best possible for this query
                    break

        return best_tier, best_result

    def _has_cover(self, metadata):
        """Return True when provider metadata carries a real cover image."""
        image = (metadata.get("image") or "").strip()
        return bool(image) and image != settings.IMG_NONE

    def _title_candidates(self, results, title):
        """Return search results whose title matches, best title first (top 3)."""
        scored = []
        for result in results[:5]:
            candidate_title = str(result.get("title") or "")
            if not self._titles_match(title, candidate_title):
                continue
            scored.append((self._title_similarity(title, candidate_title), result))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [result for _score, result in scored[:3]]

    def _author_verdict(self, target_authors, metadata):
        """Classify a provider candidate's author as match/unknown/conflict."""
        return self._classify_authors(
            target_authors,
            self._extract_provider_authors(metadata),
        )

    def _classify_authors(self, target_authors, candidate_authors):
        """Classify author agreement as 'match', 'unknown', or 'conflict'."""
        if not target_authors:
            return "match"
        if not candidate_authors:
            return "unknown"
        if self._authors_overlap(target_authors, candidate_authors):
            return "match"
        return "conflict"

    def _authors_overlap(self, target_authors, provider_authors):
        """Return True if any author names plausibly refer to the same person."""
        target = {n for n in map(self._normalize_name, target_authors) if n}
        provider = {n for n in map(self._normalize_name, provider_authors) if n}
        if not target or not provider:
            return False
        if target & provider:
            return True
        for left in target:
            for right in provider:
                if left in right or right in left:
                    return True
                if left.split()[-1] == right.split()[-1]:  # shared surname
                    return True
        return False

    def _titles_match(self, left, right):
        """Match titles, tolerating subtitles (e.g. 'Mistborn' vs 'Mistborn: ...')."""
        left_n = self._normalize_name(left)
        right_n = self._normalize_name(right)
        if not left_n or not right_n:
            return False
        if left_n == right_n:
            return True
        if right_n.startswith(left_n) or left_n.startswith(right_n):
            return True
        return self._title_similarity(left, right) >= TITLE_MATCH_THRESHOLD

    def _title_similarity(self, left, right):
        left_n = self._normalize_name(left)
        right_n = self._normalize_name(right)
        if not left_n or not right_n:
            return 0.0
        return SequenceMatcher(None, left_n, right_n).ratio()

    def _normalize_name(self, value):
        normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
        return re.sub(r"\s+", " ", normalized).strip()

    def _extract_provider_authors(self, provider_metadata):
        details = {}
        if isinstance(provider_metadata, dict):
            details = provider_metadata.get("details", {})
        if not isinstance(details, dict):
            details = {}
        raw_authors = details.get("authors") or details.get("author") or []
        if isinstance(raw_authors, str):
            raw_authors = [p.strip() for p in raw_authors.split(",") if p.strip()]
        elif not isinstance(raw_authors, list):
            raw_authors = [raw_authors] if raw_authors else []

        authors = []
        for raw_author in raw_authors:
            if isinstance(raw_author, dict):
                value = raw_author.get("name") or raw_author.get("person")
            else:
                value = raw_author
            normalized = str(value or "").strip()
            if normalized:
                authors.append(normalized)
        return list(dict.fromkeys(authors))

    def _normalize_list(self, value):
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        return [str(v).strip() for v in value if str(v).strip()]

    def _first(self, value):
        if isinstance(value, list):
            value = value[0] if value else ""
        return str(value or "").strip()

    def _parse_datetime(self, value):
        if not value:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed)
            return parsed
        return None
