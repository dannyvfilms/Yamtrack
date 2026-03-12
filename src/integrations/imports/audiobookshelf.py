"""Audiobookshelf importer for audiobook progress."""

import hashlib
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.utils import timezone

import app
from app import helpers as app_helpers
from app.log_safety import exception_summary
from app.models import MediaTypes, Sources, Status
from app.providers import services
from integrations.imports.helpers import MediaImportError, decrypt
from integrations.models import AudiobookshelfAccount

logger = logging.getLogger(__name__)
HTTP_BAD_REQUEST = 400
BOOK_METADATA_PROVIDER_ORDER = (
    Sources.HARDCOVER.value,
    Sources.OPENLIBRARY.value,
)
TITLE_MATCH_THRESHOLD = 0.72


class AudiobookshelfClientError(Exception):
    """Base ABS client error."""


class AudiobookshelfAuthError(AudiobookshelfClientError):
    """ABS auth failure."""


class AudiobookshelfClient:
    """Thin API client for Audiobookshelf."""

    def __init__(self, base_url: str, token: str):
        """Initialize with server base URL and API token."""
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, path: str):
        url = f"{self.base_url}{path}"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=20,
        )
        if response.status_code in (401, 403):
            msg = "Audiobookshelf token is invalid or expired"
            raise AudiobookshelfAuthError(msg)
        if response.status_code >= HTTP_BAD_REQUEST:
            msg = (
                "Audiobookshelf request failed "
                f"({response.status_code}) for {path}"
            )
            raise AudiobookshelfClientError(msg)
        return response.json()

    def get_me(self):
        """Return authenticated user payload."""
        return self._request("/api/me")

    def get_library_item(self, library_item_id: str):
        """Return metadata for an Audiobookshelf library item."""
        return self._request(f"/api/items/{library_item_id}")


def importer(identifier, user, mode):  # noqa: ARG001
    """Import Audiobookshelf progress for a user."""
    return AudiobookshelfImporter(user).import_data()


class AudiobookshelfImporter:
    """Import progress from Audiobookshelf."""

    def __init__(self, user):
        """Initialize importer and validate account access."""
        self.user = user
        try:
            self.account = user.audiobookshelf_account
        except AudiobookshelfAccount.DoesNotExist as error:
            msg = "Connect Audiobookshelf before importing"
            raise MediaImportError(msg) from error

        token = decrypt(self.account.api_token)
        self.client = AudiobookshelfClient(self.account.base_url, token)
        self.warnings = []
        self.enable_provider_enrichment = not settings.TESTING

    def import_data(self):
        """Import changed progress entries since the cursor."""
        imported_counts = defaultdict(int)
        try:
            me = self.client.get_me()
        except AudiobookshelfAuthError as error:
            self.account.connection_broken = True
            self.account.last_error_message = str(error)
            self.account.save(
                update_fields=[
                    "connection_broken",
                    "last_error_message",
                    "updated_at",
                ],
            )
            raise MediaImportError(str(error)) from error

        progress_entries = me.get("mediaProgress") or []
        last_sync_ms = self.account.last_sync_ms or 0
        book_progress_entries = self._book_progress_entries(progress_entries)
        existing_items = self._existing_book_items(book_progress_entries)
        changed_entries, unchanged_entries = self._partition_book_progress_entries(
            book_progress_entries,
            last_sync_ms,
        )
        max_seen, changed_processed = self._import_changed_books(
            changed_entries,
            existing_items,
            imported_counts,
            last_sync_ms,
        )

        if changed_entries:
            logger.info(
                "Audiobookshelf changed book entries processed=%s imported=%s",
                len(changed_entries),
                changed_processed,
            )

        repair_candidates, skipped_healthy_repairs = self._select_repair_candidates(
            unchanged_entries,
            existing_items,
        )

        if unchanged_entries:
            logger.info(
                "Audiobookshelf unchanged repair candidates=%s skipped_healthy=%s",
                len(repair_candidates),
                skipped_healthy_repairs,
            )

        repaired_count = self._repair_unchanged_books(
            repair_candidates,
            existing_items,
            imported_counts,
        )

        if repair_candidates:
            logger.info(
                "Audiobookshelf unchanged book repairs applied=%s",
                repaired_count,
            )

        self.account.last_sync_ms = max_seen
        self.account.last_sync_at = timezone.now()
        self.account.connection_broken = False
        self.account.last_error_message = ""
        self.account.save(
            update_fields=[
                "last_sync_ms",
                "last_sync_at",
                "connection_broken",
                "last_error_message",
                "updated_at",
            ],
        )

        return dict(imported_counts), "\n".join(dict.fromkeys(self.warnings))

    def _upsert_book(
        self,
        progress_entry: dict[str, Any],
        item_metadata: dict[str, Any],
    ):
        library_item_id = str(progress_entry.get("libraryItemId"))
        media_id = self._stable_media_id(self.account.base_url, library_item_id)

        media_info = item_metadata.get("media") or {}
        metadata = (
            media_info.get("metadata")
            or item_metadata.get("mediaMetadata")
            or {}
        )

        fallback_title = f"Audiobookshelf {library_item_id}"
        title = metadata.get("title") or item_metadata.get("title") or fallback_title

        authors_list = self._extract_author_names(metadata)
        isbn_values = self._extract_isbns(metadata)
        publishers = self._extract_publisher(metadata)
        genres = self._extract_genres(metadata)
        series_name, series_position = self._extract_series_info(metadata)

        image = self._normalize_cover_url(item_metadata.get("coverPath"))
        should_enrich = (
            self._should_prefer_provider_cover(image)
            or not authors_list
        )
        provider_metadata = (
            self._resolve_provider_metadata(
                title=title,
                authors=authors_list,
                isbns=isbn_values,
            )
            if should_enrich and self.enable_provider_enrichment
            else None
        )

        if self._should_prefer_provider_cover(image):
            image = provider_metadata.get("image") if isinstance(provider_metadata, dict) else image
        if not image:
            image = settings.IMG_NONE

        provider_authors = self._extract_provider_authors(provider_metadata)
        if not authors_list and provider_authors:
            authors_list = provider_authors

        provider_isbns = self._extract_provider_isbns(provider_metadata)
        if not isbn_values and provider_isbns:
            isbn_values = provider_isbns

        if not publishers:
            publishers = self._extract_provider_publisher(provider_metadata)
        if not genres:
            genres = self._extract_provider_genres(provider_metadata)
        release_datetime = (
            app_helpers.extract_release_datetime(provider_metadata)
            if isinstance(provider_metadata, dict)
            else None
        )
        if not series_name:
            series_name = provider_metadata.get("series_name") if isinstance(provider_metadata, dict) else None
        if series_position is None:
            series_position = provider_metadata.get("series_position") if isinstance(provider_metadata, dict) else None

        provider_title = provider_metadata.get("title") if isinstance(provider_metadata, dict) else None
        title_fields = app.models.Item.title_fields_from_metadata(
            {
                "title": title,
                "original_title": provider_metadata.get("original_title")
                if isinstance(provider_metadata, dict)
                else None,
                "localized_title": provider_metadata.get("localized_title")
                if isinstance(provider_metadata, dict)
                else None,
            },
            fallback_title=provider_title or title,
        )

        if not title_fields.get("original_title"):
            title_fields["original_title"] = title_fields.get("title") or title
        if not title_fields.get("localized_title"):
            title_fields["localized_title"] = title_fields.get("title") or title
        duration_seconds = (
            media_info.get("duration")
            or progress_entry.get("duration")
            or 0
        )
        runtime_minutes = int(duration_seconds // 60) if duration_seconds else None
        metadata_fetched_at = timezone.now()

        item, _ = app.models.Item.objects.update_or_create(
            media_id=media_id,
            source=Sources.AUDIOBOOKSHELF.value,
            media_type=MediaTypes.BOOK.value,
            defaults={
                **title_fields,
                "title": title,
                "image": image,
                "authors": authors_list,
                "isbn": isbn_values,
                "publishers": publishers,
                "genres": genres,
                "runtime_minutes": runtime_minutes,
                "release_datetime": release_datetime,
                "series_name": series_name,
                "series_position": series_position,
                "format": "audiobook",
                "metadata_fetched_at": metadata_fetched_at,
            },
        )

        progress_seconds = int(progress_entry.get("currentTime") or 0)
        progress_minutes = max(0, progress_seconds // 60)
        is_finished = bool(
            progress_entry.get("isFinished")
            or progress_entry.get("progress") == 1
        )

        if is_finished:
            status = Status.COMPLETED.value
        elif progress_minutes > 0:
            status = Status.IN_PROGRESS.value
        else:
            status = Status.PLANNING.value

        finished_at = self._parse_datetime(progress_entry.get("finishedAt"))
        started_at = self._parse_datetime(progress_entry.get("startedAt"))

        media, _ = app.models.Book.objects.update_or_create(
            user=self.user,
            item=item,
            defaults={
                "progress": progress_minutes,
                "status": status,
                "start_date": started_at,
                "end_date": finished_at if is_finished else None,
                "notes": "Format: Audiobook (Audiobookshelf)",
            },
        )
        return media

    def _stable_media_id(self, base_url: str, library_item_id: str):
        value = f"{base_url.strip().lower()}::{library_item_id}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]

    def _book_progress_entries(self, progress_entries):
        book_progress_entries = []
        for entry in progress_entries:
            library_item_id = entry.get("libraryItemId")
            if not library_item_id:
                continue

            # Skip podcast episode progress in v1.
            if entry.get("episodeId"):
                continue

            book_progress_entries.append((str(library_item_id), entry))
        return book_progress_entries

    def _partition_book_progress_entries(self, book_progress_entries, last_sync_ms):
        changed_entries = []
        unchanged_entries = []
        for library_item_id, entry in book_progress_entries:
            if int(entry.get("lastUpdate") or 0) > last_sync_ms:
                changed_entries.append((library_item_id, entry))
            else:
                unchanged_entries.append((library_item_id, entry))
        return changed_entries, unchanged_entries

    def _import_changed_books(
        self,
        changed_entries,
        existing_items,
        imported_counts,
        last_sync_ms,
    ):
        max_seen = last_sync_ms
        changed_processed = 0
        for library_item_id, entry in changed_entries:
            max_seen = max(max_seen, int(entry.get("lastUpdate") or 0))

            item_metadata = self._fetch_library_item(library_item_id)
            if item_metadata is None:
                continue

            media = self._upsert_book(entry, item_metadata)
            if media:
                imported_counts[MediaTypes.BOOK.value] += 1
                changed_processed += 1
                existing_items[library_item_id] = media.item
        return max_seen, changed_processed

    def _select_repair_candidates(self, unchanged_entries, existing_items):
        repair_candidates = []
        skipped_healthy_repairs = 0
        for library_item_id, entry in unchanged_entries:
            if self._needs_book_repair(existing_items.get(library_item_id)):
                repair_candidates.append((library_item_id, entry))
                continue

            skipped_healthy_repairs += 1
            logger.debug(
                "Audiobookshelf repair skipped for healthy book library_item_id=%s",
                library_item_id,
            )
        return repair_candidates, skipped_healthy_repairs

    def _repair_unchanged_books(
        self,
        repair_candidates,
        existing_items,
        imported_counts,
    ):
        repaired_count = 0
        for library_item_id, entry in repair_candidates:
            item_metadata = self._fetch_library_item(library_item_id)
            if item_metadata is None:
                continue

            media = self._upsert_book(entry, item_metadata)
            if media:
                imported_counts[MediaTypes.BOOK.value] += 1
                repaired_count += 1
                existing_items[library_item_id] = media.item
        return repaired_count

    def _existing_book_items(self, progress_entries):
        media_id_map = {
            library_item_id: self._stable_media_id(
                self.account.base_url,
                library_item_id,
            )
            for library_item_id, _entry in progress_entries
        }
        if not media_id_map:
            return {}

        items = app.models.Item.objects.filter(
            media_id__in=media_id_map.values(),
            source=Sources.AUDIOBOOKSHELF.value,
            media_type=MediaTypes.BOOK.value,
        )
        items_by_media_id = {item.media_id: item for item in items}
        return {
            library_item_id: items_by_media_id.get(media_id)
            for library_item_id, media_id in media_id_map.items()
            if media_id in items_by_media_id
        }

    def _fetch_library_item(self, library_item_id: str):
        try:
            return self.client.get_library_item(library_item_id)
        except AudiobookshelfClientError:
            self.warnings.append(f"{library_item_id}: failed to fetch metadata")
            return None

    def _needs_book_repair(self, item):
        if item is None:
            return True

        return any(
            (
                self._should_prefer_provider_cover(item.image),
                not item.authors,
                not item.isbn,
                not item.publishers,
                not item.genres,
                item.release_datetime is None,
                not item.original_title,
                not item.localized_title,
                item.metadata_fetched_at is None,
            ),
        )

    def _extract_author_names(self, metadata: dict[str, Any]):
        raw_authors = metadata.get("authors")
        if not raw_authors:
            fallback_author = metadata.get("authorName") or metadata.get("author")
            raw_authors = [fallback_author] if fallback_author else []
        if not isinstance(raw_authors, list):
            raw_authors = [raw_authors]

        authors = []
        for raw_author in raw_authors:
            if isinstance(raw_author, dict):
                value = raw_author.get("name") or raw_author.get("author")
            else:
                value = raw_author
            normalized = str(value or "").strip()
            if normalized:
                authors.append(normalized)
        return list(dict.fromkeys(authors))

    def _extract_isbns(self, metadata: dict[str, Any]):
        candidates = []
        for key in ("isbn", "isbn10", "isbn13", "isbn_10", "isbn_13"):
            value = metadata.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif value:
                candidates.append(value)

        identifiers = metadata.get("identifiers")
        if isinstance(identifiers, dict):
            for key in ("isbn", "isbn10", "isbn13", "isbn_10", "isbn_13"):
                value = identifiers.get(key)
                if isinstance(value, list):
                    candidates.extend(value)
                elif value:
                    candidates.append(value)

        normalized = []
        for candidate in candidates:
            value = candidate
            if isinstance(candidate, dict):
                value = (
                    candidate.get("value")
                    or candidate.get("identifier")
                    or candidate.get("isbn")
                )
            isbn = self._normalize_isbn(value)
            if isbn:
                normalized.append(isbn)

        return list(dict.fromkeys(normalized))

    def _normalize_isbn(self, value):
        if not value:
            return ""
        candidate = re.sub(r"[^0-9Xx]", "", str(value)).upper()
        if len(candidate) in (10, 13):
            return candidate
        return ""

    def _extract_publisher(self, metadata: dict[str, Any]):
        raw = metadata.get("publisher") or metadata.get("publishers")
        if isinstance(raw, list):
            raw = raw[0] if raw else ""
        return str(raw or "").strip()

    def _extract_genres(self, metadata: dict[str, Any]):
        raw_genres = metadata.get("genres") or metadata.get("genre") or []
        if not isinstance(raw_genres, list):
            raw_genres = [raw_genres] if raw_genres else []
        normalized = [str(value).strip() for value in raw_genres if str(value).strip()]
        return list(dict.fromkeys(normalized))

    def _extract_series_info(self, metadata: dict[str, Any]):
        series_name = metadata.get("seriesName")
        series_position = metadata.get("seriesSequence")
        if not series_name:
            raw_series = metadata.get("series")
            if isinstance(raw_series, list) and raw_series:
                first_series = raw_series[0]
                if isinstance(first_series, dict):
                    series_name = first_series.get("name")
                    if series_position is None:
                        series_position = (
                            first_series.get("sequence")
                            or first_series.get("position")
                            or first_series.get("seriesSequence")
                        )
                else:
                    series_name = first_series
        normalized_name = str(series_name).strip() if series_name else None
        try:
            normalized_position = float(series_position) if series_position is not None else None
        except (TypeError, ValueError):
            normalized_position = None
        return normalized_name, normalized_position

    def _normalize_cover_url(self, cover_value):
        if not cover_value:
            return ""
        cover = str(cover_value).strip()
        if not cover:
            return ""

        parsed = urlparse(cover)
        if parsed.scheme in {"http", "https"}:
            return cover
        if parsed.scheme:
            return ""
        if cover.startswith("/"):
            return f"{self.account.base_url.rstrip('/')}{cover}"
        return urljoin(f"{self.account.base_url.rstrip('/')}/", cover)

    def _should_prefer_provider_cover(self, image_url):
        if not image_url or image_url == settings.IMG_NONE:
            return True
        parsed_image = urlparse(image_url)
        if parsed_image.scheme not in {"http", "https"}:
            return True

        abs_host = urlparse(self.account.base_url).netloc.lower()
        image_host = parsed_image.netloc.lower()
        is_abs_api_cover = (
            image_host == abs_host
            and "/api/items/" in parsed_image.path
            and "/cover" in parsed_image.path
        )
        return is_abs_api_cover

    def _resolve_provider_metadata(self, title: str, authors: list[str], isbns: list[str]):
        if not title:
            return None

        search_plan = []
        for isbn in isbns:
            search_plan.append((isbn, True))

        author_hint = authors[0] if authors else ""
        if author_hint:
            search_plan.append((f"{title} {author_hint}".strip(), False))
        search_plan.append((title, False))

        seen_queries = set()
        for provider_source in BOOK_METADATA_PROVIDER_ORDER:
            for query, is_isbn_query in search_plan:
                normalized_query = query.strip()
                if not normalized_query:
                    continue
                dedupe_key = (provider_source, normalized_query)
                if dedupe_key in seen_queries:
                    continue
                seen_queries.add(dedupe_key)

                try:
                    response = services.search(
                        MediaTypes.BOOK.value,
                        normalized_query,
                        1,
                        provider_source,
                    )
                except Exception as error:  # noqa: BLE001
                    logger.debug(
                        "Audiobookshelf metadata search failed provider=%s isbn_query=%s error=%s",
                        provider_source,
                        is_isbn_query,
                        exception_summary(error),
                    )
                    continue

                results = response.get("results", []) if isinstance(response, dict) else []
                if not results:
                    continue

                candidate = (
                    results[0]
                    if is_isbn_query
                    else self._pick_best_title_match(results, title)
                )
                if not candidate:
                    continue

                try:
                    provider_metadata = services.get_media_metadata(
                        MediaTypes.BOOK.value,
                        str(candidate.get("media_id")),
                        provider_source,
                    )
                except Exception as error:  # noqa: BLE001
                    logger.debug(
                        "Audiobookshelf metadata fetch failed provider=%s error=%s",
                        provider_source,
                        exception_summary(error),
                    )
                    continue

                if self._provider_metadata_matches(
                    provider_metadata,
                    title=title,
                    authors=authors,
                    isbns=isbns,
                ):
                    return provider_metadata

        return None

    def _pick_best_title_match(self, results: list[dict[str, Any]], title: str):
        best_result = None
        best_score = 0.0

        for result in results[:5]:
            candidate_title = str(result.get("title") or "").strip()
            score = self._title_similarity(title, candidate_title)
            if score > best_score:
                best_score = score
                best_result = result

        if best_result and best_score >= TITLE_MATCH_THRESHOLD:
            return best_result
        return None

    def _provider_metadata_matches(
        self,
        provider_metadata: dict[str, Any] | None,
        title: str,
        authors: list[str],
        isbns: list[str],
    ):
        if not isinstance(provider_metadata, dict):
            return False

        provider_isbns = set(self._extract_provider_isbns(provider_metadata))
        if provider_isbns and set(isbns).intersection(provider_isbns):
            return True

        provider_title = str(provider_metadata.get("title") or "").strip()
        title_score = self._title_similarity(title, provider_title)
        if title_score < TITLE_MATCH_THRESHOLD:
            return False

        provider_authors = self._extract_provider_authors(provider_metadata)
        if not authors or not provider_authors:
            return True

        normalized_target_authors = {
            self._normalize_name(author)
            for author in authors
            if self._normalize_name(author)
        }
        normalized_provider_authors = {
            self._normalize_name(author)
            for author in provider_authors
            if self._normalize_name(author)
        }
        if normalized_target_authors.intersection(normalized_provider_authors):
            return True
        return title_score >= 0.88

    def _title_similarity(self, left: str, right: str):
        normalized_left = self._normalize_name(left)
        normalized_right = self._normalize_name(right)
        if not normalized_left or not normalized_right:
            return 0.0
        return SequenceMatcher(None, normalized_left, normalized_right).ratio()

    def _normalize_name(self, value):
        normalized = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
        return re.sub(r"\s+", " ", normalized).strip()

    def _extract_provider_authors(self, provider_metadata: dict[str, Any] | None):
        if not isinstance(provider_metadata, dict):
            return []

        details = provider_metadata.get("details", {})
        if not isinstance(details, dict):
            details = {}

        raw_authors = details.get("authors") or details.get("author") or []
        if isinstance(raw_authors, str):
            raw_authors = [part.strip() for part in raw_authors.split(",") if part.strip()]
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

    def _extract_provider_isbns(self, provider_metadata: dict[str, Any] | None):
        if not isinstance(provider_metadata, dict):
            return []
        details = provider_metadata.get("details", {})
        if not isinstance(details, dict):
            details = {}

        raw_isbns = details.get("isbn") or []
        if not isinstance(raw_isbns, list):
            raw_isbns = [raw_isbns] if raw_isbns else []

        normalized = []
        for raw_isbn in raw_isbns:
            isbn = self._normalize_isbn(raw_isbn)
            if isbn:
                normalized.append(isbn)
        return list(dict.fromkeys(normalized))

    def _extract_provider_publisher(self, provider_metadata: dict[str, Any] | None):
        if not isinstance(provider_metadata, dict):
            return ""
        details = provider_metadata.get("details", {})
        if not isinstance(details, dict):
            details = {}
        raw = details.get("publishers") or details.get("publisher") or ""
        if isinstance(raw, list):
            raw = raw[0] if raw else ""
        return str(raw or "").strip()

    def _extract_provider_genres(self, provider_metadata: dict[str, Any] | None):
        if not isinstance(provider_metadata, dict):
            return []
        raw_genres = provider_metadata.get("genres") or []
        if not isinstance(raw_genres, list):
            raw_genres = [raw_genres] if raw_genres else []
        normalized = [str(value).strip() for value in raw_genres if str(value).strip()]
        return list(dict.fromkeys(normalized))

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
