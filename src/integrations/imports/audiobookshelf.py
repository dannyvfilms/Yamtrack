"""Audiobookshelf importer for audiobook progress."""

import hashlib
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

import requests
from django.utils import timezone

import app
from app.models import MediaTypes, Sources, Status
from integrations.imports.helpers import MediaImportError, decrypt
from integrations.models import AudiobookshelfAccount

logger = logging.getLogger(__name__)
HTTP_BAD_REQUEST = 400


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
        changed = [
            p
            for p in progress_entries
            if int(p.get("lastUpdate") or 0) > last_sync_ms
        ]
        max_seen = last_sync_ms

        for entry in changed:
            library_item_id = entry.get("libraryItemId")
            if not library_item_id:
                continue

            max_seen = max(max_seen, int(entry.get("lastUpdate") or 0))

            # Skip podcast episode progress in v1.
            if entry.get("episodeId"):
                continue

            try:
                item_metadata = self.client.get_library_item(library_item_id)
            except AudiobookshelfClientError:
                self.warnings.append(f"{library_item_id}: failed to fetch metadata")
                continue

            media = self._upsert_book(entry, item_metadata)
            if media:
                imported_counts[MediaTypes.BOOK.value] += 1

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

        authors = metadata.get("authors") or []
        if isinstance(authors, list):
            authors_list = []
            for author in authors:
                if isinstance(author, dict):
                    normalized = author.get("name", "").strip()
                else:
                    normalized = str(author).strip()
                if normalized:
                    authors_list.append(normalized)
        else:
            authors_list = []

        image = item_metadata.get("coverPath") or ""
        duration_seconds = (
            media_info.get("duration")
            or progress_entry.get("duration")
            or 0
        )
        runtime_minutes = int(duration_seconds // 60) if duration_seconds else None

        title_fields = app.models.Item.title_fields_from_metadata(
            {"title": title},
            fallback_title=title,
        )
        item, _ = app.models.Item.objects.update_or_create(
            media_id=media_id,
            source=Sources.AUDIOBOOKSHELF.value,
            media_type=MediaTypes.BOOK.value,
            defaults={
                **title_fields,
                "title": title,
                "image": image,
                "authors": authors_list,
                "runtime_minutes": runtime_minutes,
                "format": "audiobook",
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

    def _parse_datetime(self, value):
        if not value:
            return None
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed)
            return parsed
        return None
