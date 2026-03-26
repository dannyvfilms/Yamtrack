"""Plex history importer."""

import logging
import re
import urllib3

from collections import defaultdict

from datetime import UTC, datetime

from django.conf import settings
from django.utils import timezone

import app
from app.log_safety import exception_summary, presence_map
from app.models import MediaTypes, Sources, Status
from app.providers import services
from app.services.music import prefetch_album_covers

# Suppress InsecureRequestWarning (Plex local connections often use self-signed certs)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from integrations import plex as plex_api
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError
from integrations.webhooks.plex import PlexWebhookProcessor

logger = logging.getLogger(__name__)


def importer(library, user, mode):
    """Import Plex watch/listen history for the user."""
    account = getattr(user, "plex_account", None)
    if not account or not account.plex_token:
        raise MediaImportError("Plex is not connected for this user.")

    plex_importer = PlexHistoryImporter(
        user=user,
        account=account,
        mode=mode,
        library=library,
    )
    return plex_importer.import_data()


class PlexHistoryImporter:
    """Importer that replays Plex history through TMDB-backed bulk creation."""

    def __init__(self, user, account, mode, library, fast_mode=True):
        self.user = user
        self.account = account
        self.mode = mode
        self.library = library
        self.fast_mode = fast_mode
        self.processor = PlexWebhookProcessor()
        self.existing_media = helpers.get_existing_media(user)
        self.to_delete = defaultdict(lambda: defaultdict(set))
        self.bulk_media = defaultdict(list)
        self.media_instances = defaultdict(lambda: defaultdict(list))
        self.counts = defaultdict(int)
        self.summary_counts = defaultdict(int)
        self.warnings = []
        self.resources = []
        self._metadata_cache: dict[str, dict] = {}
        self._movie_records: list[dict] = []
        self._episode_records: list[dict] = []
        self._movie_ids: set[str] = set()
        self._tv_ids: set[str] = set()
        self._existing_movie_keys: set[tuple[str, datetime]] = set()
        self._existing_episode_keys: set[tuple[str, int, int, datetime]] = set()
        self._import_movie_keys: set[tuple[str, datetime]] = set()
        self._import_episode_keys: set[tuple] = set()
        self._movie_metadata_cache: dict[str, dict] = {}
        self._tv_metadata_cache: dict[str, dict] = {}
        self._tv_seasons_loaded: dict[str, set[int]] = defaultdict(set)
        self._account_id: str | None = (
            str(account.plex_account_id)
            if getattr(account, "plex_account_id", None)
            else None
        )
        self._allowed_usernames: list[str] = []
        self._allowed_account_ids: set[str] = set()
        self._account_id_to_username: dict[str, str] = {}
        self._skipped_user_count = 0
        self._skipped_user_samples: set[str] = set()
        self._artists_for_prefetch: set[int] = set()
        # Track unique music tracks (by item key) for counting purposes
        self._unique_music_tracks: set[tuple[str, str]] = set()
        # Store ratings from library items to apply during bulk media creation
        self._library_ratings: dict[tuple[str, str], float] = {}

    def import_data(self):
        """Import history for the selected library."""
        self._ensure_username_matches()
        self._ensure_account_id()
        self._init_allowed_usernames()
        self._init_allowed_account_ids()
        try:
            self.resources = plex_api.list_resources(self.account.plex_token)
        except plex_api.PlexAuthError as exc:
            raise MediaImportError("Plex token expired; reconnect and try again.") from exc

        sections = self._get_target_sections()
        if not sections:
            raise MediaImportError("No Plex libraries are available to import.")

        for section in sections:
            try:
                self._import_section(section)
            except MediaImportError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                msg = f"Unexpected error importing Plex section {section.get('title')}: {exc}"
                raise MediaImportUnexpectedError(msg) from exc

        if self.mode == "new":
            self._build_existing_dedupe_sets()

        if self.mode == "overwrite":
            helpers.cleanup_existing_media(self.to_delete, self.user)

        logger.info("Warming TV metadata cache...")
        self._warm_tv_metadata_cache()
        logger.info("Building bulk media instances...")
        self._build_bulk_media()
        logger.info("Finalizing bulk creation...")
        helpers.bulk_create_media(self.bulk_media, self.user)

        self._prefetch_collected_album_covers()
        self._enqueue_fast_runtime_backfill()
        self._enqueue_music_enrichment()

        result_counts = {
            media_type: len(media_list) for media_type, media_list in self.bulk_media.items()
        }
        if MediaTypes.MUSIC.value in self.counts:
            result_counts[MediaTypes.MUSIC.value] = self.counts[MediaTypes.MUSIC.value]
        if MediaTypes.MUSIC.value in result_counts:
            result_counts["music_unique_tracks"] = len(self._unique_music_tracks)

        result_counts.update(self.summary_counts)

        if self._skipped_user_count:
            samples = ", ".join(sorted(self._skipped_user_samples))
            if samples:
                self.warnings.append(
                    f"Skipped {self._skipped_user_count} Plex history entries for other users ({samples}).",
                )
            else:
                self.warnings.append(
                    f"Skipped {self._skipped_user_count} Plex history entries for other users.",
                )

        deduped_warnings = "\n".join(dict.fromkeys(self.warnings))
        return result_counts, deduped_warnings

    def _ensure_username_matches(self):
        """Persist the Plex username into the user's webhook allow list."""
        username = (self.account.plex_username or "").strip()
        if not username:
            return

        existing = [
            u.strip()
            for u in (self.user.plex_usernames or "").split(",")
            if u.strip()
        ]

        if username.lower() in [u.lower() for u in existing]:
            return

        updated = existing + [username]
        self.user.plex_usernames = ", ".join(updated)
        self.user.save(update_fields=["plex_usernames"])

    def _ensure_account_id(self):
        """Fetch and persist the Plex account id if missing."""
        if self._account_id:
            return

        try:
            account_info = plex_api.fetch_account(self.account.plex_token)
        except plex_api.PlexAuthError as exc:
            raise MediaImportError("Plex token expired; reconnect and try again.") from exc
        except plex_api.PlexClientError as exc:
            logger.warning(
                "Could not fetch Plex account ID: %s",
                exception_summary(exc),
            )
            return

        account_id = account_info.get("id")
        if account_id:
            self._account_id = str(account_id)
            self.account.plex_account_id = self._account_id
            self.account.save(update_fields=["plex_account_id"])

    def _init_allowed_usernames(self):
        """Initialize the allowed Plex usernames list."""
        usernames = [
            u.strip()
            for u in (self.user.plex_usernames or "").split(",")
            if u.strip()
        ]
        if not usernames and self.account.plex_username:
            usernames = [self.account.plex_username]
        self._allowed_usernames = [u.lower() for u in usernames]

    def _init_allowed_account_ids(self):
        """Resolve allowed usernames to Plex account IDs for history filtering."""
        if not self._allowed_usernames:
            if self._account_id:
                self._allowed_account_ids.add(str(self._account_id))
            return

        allowed_usernames = {name.lower() for name in self._allowed_usernames}
        resolved_usernames: set[str] = set()
        for username in allowed_usernames:
            if username.isdigit():
                self._allowed_account_ids.add(username)
                resolved_usernames.add(username)

        account_username = (self.account.plex_username or "").strip()
        if self._account_id and account_username:
            self._account_id_to_username.setdefault(str(self._account_id), account_username)
            if account_username.lower() in allowed_usernames:
                self._allowed_account_ids.add(str(self._account_id))
                # Plex server history uses "1" as the server-local owner ID
                # Add it when the connected account matches an allowed username
                self._allowed_account_ids.add("1")
                self._account_id_to_username.setdefault("1", account_username)
                resolved_usernames.add(account_username.lower())

        unresolved = [name for name in allowed_usernames if name not in resolved_usernames]

        try:
            plex_users = plex_api.list_users(self.account.plex_token)
        except plex_api.PlexAuthError as exc:
            if unresolved:
                raise MediaImportError("Plex token expired; reconnect and try again.") from exc
            logger.warning("Could not fetch Plex users for history diagnostics: Token expired")
            plex_users = []
        except plex_api.PlexClientError as exc:
            logger.warning(
                "Could not fetch Plex users for history filtering: %s",
                exception_summary(exc),
            )
            plex_users = []

        username_to_ids: dict[str, set[str]] = defaultdict(set)
        for user in plex_users:
            account_ids = {
                str(value)
                for key in ("id", "accountID", "accountId", "account_id", "uuid")
                if (value := user.get(key))
            }
            if not account_ids:
                continue

            for key in ("username", "title", "name", "friendlyName", "email"):
                value = user.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    username_to_ids[name.lower()].update(account_ids)
                    for account_id in account_ids:
                        self._account_id_to_username.setdefault(account_id, name)

        for name in unresolved:
            for account_id in username_to_ids.get(name, set()):
                self._allowed_account_ids.add(account_id)

        missing = [name for name in unresolved if name not in username_to_ids]
        if missing:
            self.warnings.append(
                "Could not map Plex usernames to account IDs for history filtering: "
                + ", ".join(sorted(set(missing))),
            )

    def _get_target_sections(self):
        """Return the sections the user selected or all if requested."""
        sections = self.account.sections or []
        if not sections:
            sections = plex_api.list_sections(self.account.plex_token)
            self.account.sections = sections
            self.account.sections_refreshed_at = timezone.now()
            self.account.save(update_fields=["sections", "sections_refreshed_at"])

        if self.library == "all":
            return sections

        try:
            machine_id, section_id = self.library.split("::", 1)
        except ValueError:
            raise MediaImportError("Invalid Plex library selection.")

        filtered = [
            section
            for section in sections
            if section.get("machine_identifier") == machine_id
            and str(section.get("id")) == str(section_id)
        ]

        if not filtered:
            raise MediaImportError("The selected Plex library is no longer available.")
        return filtered

    def _import_section(self, section: dict):
        """Fetch and ingest history for a single Plex section."""
        connections = self._connections_for_machine(section.get("machine_identifier"))
        if section.get("uri"):
            connections.insert(0, section.get("uri"))
        seen = []
        connections = [c for c in connections if c and not (c in seen or seen.append(c))]
        if not connections:
            raise MediaImportError(
                f"Could not find a Plex connection for {section.get('server_name') or 'server'}.",
            )

        section_type = (section.get("type") or "").lower()
        if section_type and section_type not in ("artist", "music", "movie", "show"):
            self.warnings.append(
                f"Plex library '{section.get('title') or section.get('id')}' "
                f"has unsupported type '{section_type}'; unsupported entries will be skipped.",
            )

        entries, uri_used = self._fetch_history_entries(connections, section.get("id"))

        for entry in entries:
            try:
                self._process_entry(entry, uri_used, section_type)
            except MediaImportError as exc:
                self.warnings.append(str(exc))
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Failed to import a Plex history entry: %s",
                    exception_summary(exc),
                )
                self.warnings.append(f"Failed to import a Plex entry: {exc}")

        logger.info(
            "Processed %s Plex history entries from library %s (Movies: %d, Episodes: %d)",
            len(entries),
            section.get("title") or section.get("id"),
            len(self._movie_records),
            len(self._episode_records),
        )

        # Fetch and apply ratings from library items
        try:
            self._import_ratings_from_library(section, uri_used)
        except Exception as exc:
            logger.warning(
                "Failed to import ratings from Plex library items: %s",
                exception_summary(exc),
            )
            self.warnings.append(
                f"Failed to import ratings from library items: {exc}",
            )

    def _fetch_history_entries(self, connections: list[str], section_id: str | None) -> tuple[list[dict], str]:
        """Pull all history pages up front to minimize per-page overhead, trying fallbacks."""
        entries: list[dict] = []
        start = 0
        max_items = settings.PLEX_HISTORY_MAX_ITEMS
        if not max_items or max_items < 1:
            max_items = None  # No cap
        page_size = settings.PLEX_HISTORY_PAGE_SIZE
        failures = []
        uri_index = 0
        uri_used = ""

        while uri_index < len(connections):
            uri = connections[uri_index]
            try:
                while max_items is None or start < max_items:
                    page, total = plex_api.fetch_history(
                        self.account.plex_token,
                        uri,
                        section_id,
                        start,
                        size=page_size,
                    )

                    if not page:
                        break

                    entries.extend(page)
                    start += len(page)
                    if len(page) < page_size or start >= total:
                        break
                uri_used = uri
                break
            except plex_api.PlexAuthError as exc:
                raise MediaImportError("Plex token expired; reconnect and try again.") from exc
            except plex_api.PlexClientError as exc:
                failures.append((uri, str(exc)))
                uri_index += 1
                start = 0
                entries = []
                continue

        logger.info(
            "Fetched %s Plex history entries for section %s (requested up to %s)",
            len(entries),
            section_id or "all",
            max_items if max_items is not None else "no limit",
        )
        if not entries and failures:
            raise MediaImportUnexpectedError(
                f"Could not fetch Plex history after trying connections: {failures}",
            )
        if max_items is None:
            return entries, uri_used
        return entries[:max_items], uri_used

    def _connections_for_machine(self, machine_identifier):
        """Return the sorted connection URIs for a server."""
        uris: list[str] = []
        for resource in self.resources:
            if resource.get("machine_identifier") != machine_identifier:
                continue
            connections = resource.get("connections") or []
            sorted_conns = plex_api._sorted_connections(connections)
            uris.extend([c.get("uri") for c in sorted_conns if c.get("uri")])
        return uris

    def _process_entry(self, entry: dict, uri: str, section_type: str | None = None):
        """Process a single history entry."""
        metadata = self._build_metadata(entry)
        media_type = metadata.get("type")
        logger.debug(
            "Processing Plex history entry type=%s section=%s",
            media_type,
            section_type,
        )
        if not self._is_allowed_history_user(metadata):
            return
        metadata["Guid"] = self._normalize_guid_list(
            metadata.get("Guid") or metadata.get("guid"),
        )

        payload = {"Metadata": metadata}
        media_type = self.processor._get_media_type(payload)

        # Context-aware media type resolution
        if section_type == "show":
            # If in a TV library, prefer TV type but allow fallback to Movie
            # if season/episode info is missing.
            is_episode = bool(metadata.get("parentIndex") or metadata.get("index"))
            if not is_episode and (not media_type or media_type == MediaTypes.TV.value):
                # Try to see if it works better as a movie
                media_type = MediaTypes.MOVIE.value
            elif not media_type or media_type == MediaTypes.MOVIE.value:
                media_type = MediaTypes.TV.value
        elif section_type == "movie" and not media_type:
            media_type = MediaTypes.MOVIE.value

        if media_type == MediaTypes.MUSIC.value:
            self._process_music_entry(metadata)
            return

        if media_type not in (MediaTypes.MOVIE.value, MediaTypes.TV.value):
            self._track_unknown_type(metadata)
            return

        metadata, ids = self._ensure_external_ids(metadata, uri, section_type)
        logger.debug(
            "Resolved Plex history ID presence: %s",
            presence_map(ids, ("tmdb_id", "imdb_id", "tvdb_id", "anidb_id")),
        )

        if not self._has_external_ids(ids):
            logger.debug(
                "No external IDs found for Plex history entry",
            )

        if not self._has_external_ids(ids):
            # Last ditch effort for TV shows: if we forced it to MOVIE due to missing season
            # but it has no IDs, try it as TV if it's a show library.
            if (
                section_type == "show"
                and media_type == MediaTypes.MOVIE.value
                and not self._has_external_ids(ids)
            ):
                media_type = MediaTypes.TV.value
                metadata, ids = self._ensure_external_ids(metadata, uri, section_type)

            if not self._has_external_ids(ids):
                if section_type == "show":
                    # Proceed to _record_episode_entry which has its own title fallback
                    pass
                else:
                    self._track_missing_ids(metadata)
                    return

        if media_type == MediaTypes.MOVIE.value:
            # If we're processing as a movie but it's a show library,
            # make sure it doesn't have season/episode info that would make it a TV show
            if section_type == "show" and (
                metadata.get("parentIndex") or metadata.get("index")
            ):
                if not self._record_episode_entry(metadata, ids):
                    # Fallback: if episode recording failed (e.g. missing season/episode numbers),
                    # try recording as a movie. This handles cases like Anime Specials (Movies)
                    # that are in TV libraries but lack standard S/E numbering.
                    logger.debug(
                        "Episode recording failed during Plex import; falling back to movie",
                    )
                    self._record_movie_entry(metadata, ids)
            else:
                self._record_movie_entry(metadata, ids)
        else:
            if not self._record_episode_entry(metadata, ids):
                # Fallback: if episode recording failed (e.g. missing season/episode numbers),
                # try recording as a movie. This handles cases like Anime Specials (Movies)
                # that are in TV libraries but lack standard S/E numbering.
                self._record_movie_entry(metadata, ids)

    def _process_music_entry(self, metadata: dict):
        """Replay music history entries through the webhook processor."""
        payload = {
            "event": "media.scrobble",
            "Account": {"title": self.account.plex_username or self.user.username},
            "Metadata": metadata,
            "_import_batch": True,
        }

        result = self.processor.process_payload(payload, self.user)
        if not result:
            return

        if getattr(result, "item", None):
            track_key = (result.item.media_id, result.item.source)
            self._unique_music_tracks.add(track_key)

        artist_id = getattr(result, "artist_id", None)
        if artist_id:
            self._artists_for_prefetch.add(artist_id)

        self.counts[MediaTypes.MUSIC.value] += 1

    def _is_allowed_history_user(self, metadata: dict) -> bool:
        """Return True when the history entry matches the selected Plex user."""
        account_id, username = self._extract_history_user(metadata)
        account_id_str = str(account_id) if account_id is not None else None
        logger.debug("Evaluating Plex history user against configured filters")

        if not self._allowed_usernames and not self._account_id:
            return True

        if self._allowed_usernames:
            if username:
                matches = username.lower() in self._allowed_usernames
                logger.debug(
                    "Checking Plex history username against configured username filters",
                )
                if not matches:
                    resolved_name = self._account_id_to_username.get(
                        account_id_str,
                        username,
                    )
                    self._record_user_skip(username=resolved_name, account_id=account_id_str)
                return matches

            if account_id_str:
                if self._allowed_account_ids:
                    matches = account_id_str in self._allowed_account_ids
                    logger.debug(
                        "Checking Plex history account ID against configured account filters",
                    )
                    if not matches:
                        resolved_name = self._account_id_to_username.get(
                            account_id_str,
                            username,
                        )
                        self._record_user_skip(
                            account_id=account_id_str,
                            username=resolved_name,
                        )
                    return matches

                logger.debug(
                    "Skipping Plex history entry; account ID mapping missing for configured usernames",
                )
                self._record_user_skip(username=username, account_id=account_id_str)
                return False

        if account_id_str and self._account_id:
            matches = account_id_str == str(self._account_id)
            logger.debug(
                "Checking Plex history account ID against connected account: %s",
                matches,
            )
            if not matches:
                resolved_name = self._account_id_to_username.get(
                    account_id_str,
                    username,
                )
                self._record_user_skip(account_id=account_id_str, username=resolved_name)
            return matches

        logger.debug(
            "Skipping Plex history entry; unable to determine user (keys: %s)",
            sorted(metadata.keys()),
        )
        self._record_user_skip(username=username, account_id=account_id_str)
        return False

    def _extract_history_user(self, metadata: dict) -> tuple[str | None, str | None]:
        """Extract account/user identity from Plex history metadata."""
        account_id = (
            metadata.get("accountID")
            or metadata.get("accountId")
            or metadata.get("account_id")
        )

        username_candidates: list[str] = []
        for key in (
            "username",
            "user",
            "account",
            "accountName",
            "userName",
            "friendlyName",
        ):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                username_candidates.append(value.strip())

        for block_key in ("account", "Account", "user", "User"):
            block = metadata.get(block_key)
            if isinstance(block, dict):
                for key in ("title", "username", "name", "email", "friendlyName"):
                    value = block.get(key)
                    if isinstance(value, str) and value.strip():
                        username_candidates.append(value.strip())

        username = username_candidates[0] if username_candidates else None
        if not username and account_id is not None:
            username = self._account_id_to_username.get(str(account_id))
        return account_id, username

    def _record_user_skip(self, username: str | None, account_id: str | None):
        """Track skipped entries that belong to other Plex users."""
        self._skipped_user_count += 1
        self.summary_counts["skipped_other_user"] += 1
        sample = None
        if username and account_id:
            sample = f"{username} (accountID={account_id})"
        elif username:
            sample = username
        elif account_id:
            sample = f"accountID={account_id}"
        if sample:
            if len(self._skipped_user_samples) < 5:
                self._skipped_user_samples.add(sample)

    def _track_unknown_type(self, metadata: dict):
        """Record a skipped entry with an unsupported media type."""
        self.summary_counts["skipped_unknown_type"] += 1
        media_type = metadata.get("type") or "unknown"
        title = self._get_entry_title(metadata)
        self.warnings.append(
            f"Skipping Plex entry with unsupported type '{media_type}': {title}",
        )

    def _track_missing_ids(self, metadata: dict, reason: str | None = None):
        """Record a skipped entry due to missing identifiers."""
        self.summary_counts["skipped_missing_ids"] += 1
        title = self._get_entry_title(metadata)
        if reason:
            self.warnings.append(f"Skipping Plex entry for {title}: {reason}")
        else:
            self.warnings.append(f"Skipping Plex entry without external IDs: {title}")

    def _get_entry_title(self, metadata: dict) -> str:
        """Return the best-effort title for a Plex history entry."""
        return (
            metadata.get("title")
            or metadata.get("grandparentTitle")
            or metadata.get("parentTitle")
            or "Unknown title"
        )

    def _ensure_external_ids(
        self,
        metadata: dict,
        uri: str,
        section_type: str | None = None,
    ) -> tuple[dict, dict]:
        """Ensure external IDs are populated, fetching Plex metadata if needed."""
        # Allow title search fallback for TV/Movie libraries to improve matching yields
        allow_title_search = section_type in ("show", "movie")
        ids = self.processor.resolve_external_ids(
            {"Metadata": metadata},
            allow_title_search=allow_title_search,
        )
        if self._has_external_ids(ids):
            return metadata, ids

        rating_key = metadata.get("ratingKey") or metadata.get("ratingkey")
        if not rating_key:
            return metadata, ids

        if rating_key in self._metadata_cache:
            details = self._metadata_cache[rating_key]
        else:
            try:
                details = plex_api.fetch_metadata(
                    self.account.plex_token,
                    uri,
                    rating_key,
                )
            except plex_api.PlexAuthError as exc:
                raise MediaImportError(
                    "Plex token expired; reconnect and try again.",
                ) from exc
            except plex_api.PlexClientError as exc:
                self.warnings.append(
                    f"Failed to fetch Plex metadata for {self._get_entry_title(metadata)}: {exc}",
                )
                details = None

            self._metadata_cache[rating_key] = details

        if not details:
            return metadata, ids

        merged = {**metadata, **details}
        merged["Guid"] = self._normalize_guid_list(
            merged.get("Guid") or merged.get("guid"),
        )
        ids = self.processor.resolve_external_ids(
            {"Metadata": merged},
            allow_title_search=False,
        )
        return merged, ids

    def _has_external_ids(self, ids: dict) -> bool:
        """Return True when any deterministic external ID is present."""
        return any(ids.get(key) for key in ("tmdb_id", "imdb_id", "tvdb_id"))

    def _should_process_media(self, media_type: str, media_id: str) -> bool:
        """Apply new/overwrite semantics for the resolved IDs."""
        return helpers.should_process_media(
            self.existing_media,
            self.to_delete,
            media_type,
            Sources.TMDB.value,
            str(media_id),
            self.mode,
        )

    def _record_movie_entry(self, metadata: dict, ids: dict) -> bool:
        """Store a normalized movie history record for bulk import."""
        tmdb_id = self._resolve_movie_tmdb_id(ids)
        logger.debug(
            "Recording Plex movie entry with ID presence=%s",
            presence_map(ids, ("tmdb_id", "imdb_id")),
        )
        imdb_id = ids.get("imdb_id")
        if not tmdb_id:
            # Try title search fallback for movies if TMDB ID is missing
            title = self._get_entry_title(metadata)
            if title:
                logger.debug("Movie TMDB ID missing; attempting Plex title fallback search")
                try:
                    from app.providers import services
                    search_results = services.search(
                        MediaTypes.MOVIE.value,
                        title,
                        page=1,
                    )
                    results = search_results.get("results") or []
                    if results:
                        tmdb_id = str(results[0].get("media_id"))
                        logger.info(
                            "Resolved Plex movie entry via title fallback search",
                        )
                except Exception as exc:
                    logger.warning(
                        "Movie title fallback search failed during Plex import: %s",
                        exception_summary(exc),
                    )

        if not tmdb_id:
            self._track_missing_ids(metadata, "missing TMDB/IMDB ID")
            return False

        tmdb_id = str(tmdb_id)
        if not self._should_process_media(MediaTypes.MOVIE.value, tmdb_id):
            self.summary_counts["skipped_existing"] += 1
            return True

        watched_at = self._get_played_at(metadata)
        if not watched_at:
            watched_at = timezone.now().replace(second=0, microsecond=0)

        # Plex history replays are treated as completed entries; partial progress is ignored.
        rating = self._normalize_rating(metadata.get("userRating"), metadata.get("title"))

        logger.debug("Recording normalized Plex movie history record")
        self._movie_records.append(
            {
                "tmdb_id": tmdb_id,
                "imdb_id": imdb_id,
                "watched_at": watched_at,
                "rating": rating,
                "title": metadata.get("title") or self._get_entry_title(metadata),
            },
        )
        self._movie_ids.add(tmdb_id)
        return True

    def _record_episode_entry(self, metadata: dict, ids: dict) -> bool:
        """
        Store a normalized episode history record for bulk import.

        Returns:
            bool: True if the entry was successfully recorded, False otherwise.
        """
        tmdb_id = ids.get("tmdb_id")
        logger.debug(
            "Recording Plex episode entry with ID presence=%s",
            presence_map(ids, ("tmdb_id", "imdb_id", "tvdb_id")),
        )
        try:
            # Use grandparentTitle (Series Title) for Tv search, falling back to title if needed
            series_search_title = metadata.get("grandparentTitle") or self._get_entry_title(metadata)
            media_id, found_season, found_episode = self.processor._find_tv_media_id(
                ids,
                series_search_title,
                allow_title_fallback=True,  # Ensure we try title search
            )
        except Exception as exc:
            logger.warning(
                "TV title fallback search failed during Plex import: %s",
                exception_summary(exc),
            )
            media_id = None

        if not media_id:
            logger.debug(
                "Failed to find TV match for Plex entry with ID presence=%s",
                presence_map(ids, ("tmdb_id", "imdb_id", "tvdb_id")),
            )
            self._track_missing_ids(metadata, "missing TMDB/TVDB/IMDB ID")
            return False

        season_number = metadata.get("parentIndex") or found_season
        episode_number = metadata.get("index") or found_episode
        if season_number is None or episode_number is None:
            # Don't log a warning yet; return False to allow fallback to Movie
            return False

        media_id = str(media_id)
        if not self._should_process_media(MediaTypes.TV.value, media_id):
            self.summary_counts["skipped_existing"] += 1
            return True

        watched_at = self._get_played_at(metadata)
        if not watched_at:
            watched_at = timezone.now().replace(second=0, microsecond=0)

        viewed_at_ts = metadata.get("viewedAt") or metadata.get("lastViewedAt")
        try:
            viewed_at_ts = int(viewed_at_ts) if viewed_at_ts is not None else None
        except (TypeError, ValueError):
            viewed_at_ts = None

        rating = self._normalize_rating(metadata.get("userRating"), metadata.get("title"))

        self._episode_records.append(
            {
                "tmdb_id": media_id,
                "season_number": season_number,
                "episode_number": episode_number,
                "watched_at": watched_at,
                "viewed_at_ts": viewed_at_ts,
                "plex_rating_key": metadata.get("ratingKey") or metadata.get("ratingkey"),
                "rating": rating,
                "title": metadata.get("title") or "Unknown Episode",
                "series_title": series_search_title,
            },
        )
        self._tv_ids.add(media_id)
        return True

    def _get_played_at(self, metadata: dict):
        """Extract played-at timestamp if provided by Plex history."""
        ts = metadata.get("viewedAt") or metadata.get("lastViewedAt")
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            return None

        played_at = datetime.fromtimestamp(ts_int, tz=UTC)
        return timezone.localtime(played_at)

    def _import_ratings_from_library(self, section: dict, uri: str):
        """Fetch ratings from Plex library items and apply them to imported media instances.
        
        This complements history import by fetching ratings from library items,
        which may have ratings even if they weren't in the watch history.
        """
        section_type = (section.get("type") or "").lower()
        if section_type not in ("movie", "show"):
            # Only import ratings for movies and TV shows
            return

        section_key = section.get("key") or section.get("id")
        if not section_key:
            logger.debug("No section key found, skipping rating import")
            return

        logger.info(
            "Fetching ratings from library items for section '%s'",
            section.get("title") or section.get("id"),
        )

        # Fetch all library items (paginated)
        ratings_map = {}  # Maps (source, media_id) -> rating
        start = 0
        page_size = settings.PLEX_HISTORY_PAGE_SIZE
        total_fetched = 0

        while True:
            try:
                items, total = plex_api.fetch_section_all_items(
                    self.account.plex_token,
                    uri,
                    str(section_key),
                    start=start,
                    size=page_size,
                )
            except plex_api.PlexAuthError as exc:
                raise MediaImportError("Plex token expired; reconnect and try again.") from exc
            except plex_api.PlexClientError as exc:
                logger.warning(
                    "Failed to fetch library items for rating import: %s",
                    exception_summary(exc),
                )
                break

            if not items:
                break

            for item in items:
                user_rating = item.get("userRating")
                if user_rating is None:
                    continue

                # Extract external IDs
                guids = item.get("Guid", [])
                if not guids:
                    single_guid = item.get("guid")
                    if single_guid:
                        guids = [{"id": single_guid}]

                external_ids = plex_api.extract_external_ids_from_guids(guids)
                
                # Normalize rating
                title = item.get("title") or "Unknown"
                normalized_rating = self._normalize_rating(user_rating, title)
                if normalized_rating is None:
                    continue

                # Store rating by external ID (prefer TMDB, fallback to IMDB/TVDB)
                if external_ids.get("tmdb_id"):
                    ratings_map[("tmdb", external_ids["tmdb_id"])] = normalized_rating
                if external_ids.get("imdb_id"):
                    ratings_map[("imdb", external_ids["imdb_id"])] = normalized_rating
                if external_ids.get("tvdb_id"):
                    ratings_map[("tvdb", external_ids["tvdb_id"])] = normalized_rating

            total_fetched += len(items)
            if len(items) < page_size or total_fetched >= total:
                break
            start += page_size

        if not ratings_map:
            logger.debug("No ratings found in Plex library items for the selected section")
            return

        logger.info(
            "Found %d ratings in library items for section '%s'",
            len(ratings_map),
            section.get("title") or section.get("id"),
        )

        # Store ratings to apply during bulk media creation
        self._library_ratings.update(ratings_map)


    def _normalize_rating(self, rating_value, title: str | None) -> float | None:
        """Normalize Plex rating values onto a 0-10 scale."""
        if rating_value in (None, ""):
            return None

        try:
            rating = float(rating_value)
        except (TypeError, ValueError):
            entry_title = title or "Unknown title"
            self.warnings.append(
                f"{entry_title}: invalid Plex rating '{rating_value}' - skipped",
            )
            return None

        if rating < 0:
            entry_title = title or "Unknown title"
            self.warnings.append(
                f"{entry_title}: invalid Plex rating '{rating_value}' - skipped",
            )
            return None

        if rating <= 10:
            rating = rating
        elif rating <= 100:
            rating /= 10
        else:
            entry_title = title or "Unknown title"
            self.warnings.append(
                f"{entry_title}: invalid Plex rating '{rating_value}' - skipped",
            )
            return None

        rating = round(rating, 1)
        if rating < 0 or rating > 10:
            entry_title = title or "Unknown title"
            self.warnings.append(
                f"{entry_title}: invalid Plex rating '{rating_value}' - skipped",
            )
            return None

        return rating

    def _resolve_movie_tmdb_id(self, ids: dict) -> str | None:
        """Resolve a TMDB ID for a movie entry."""
        tmdb_id = ids.get("tmdb_id")
        if tmdb_id:
            return str(tmdb_id)

        imdb_id = ids.get("imdb_id")
        if not imdb_id:
            return None

        try:
            response = app.providers.tmdb.find(imdb_id, "imdb_id")
        except services.ProviderAPIError as exc:
            self.warnings.append(f"TMDB lookup failed for IMDB {imdb_id}: {exc}")
            return None

        if response.get("movie_results"):
            return str(response["movie_results"][0]["id"])
        return None

    def _build_existing_dedupe_sets(self):
        """Collect existing movie/episode keys for replay-safe imports."""
        if self._movie_ids:
            existing_movies = (
                app.models.Movie.objects.filter(
                    user=self.user,
                    item__media_id__in=self._movie_ids,
                    item__source=Sources.TMDB.value,
                )
                .select_related("item")
            )
            for movie in existing_movies:
                if not movie.end_date:
                    continue
                key = (movie.item.media_id, self._round_datetime(movie.end_date))
                self._existing_movie_keys.add(key)

        if self._tv_ids:
            existing_episodes = (
                app.models.Episode.objects.filter(
                    related_season__user=self.user,
                    item__media_id__in=self._tv_ids,
                    item__source=Sources.TMDB.value,
                )
                .select_related("item", "related_season")
            )
            for episode in existing_episodes:
                if not episode.end_date:
                    continue
                key = (
                    episode.item.media_id,
                    episode.item.season_number,
                    episode.item.episode_number,
                    self._round_datetime(episode.end_date),
                )
                self._existing_episode_keys.add(key)

    def _build_bulk_media(self):
        """Convert collected history records into bulk media instances."""
        logger.info("Bulk importing movie entries: %d", len(self._movie_records))
        for record in sorted(
            self._movie_records,
            key=lambda item: item["watched_at"],
        ):
            if self._should_skip_movie_record(record):
                continue

            metadata = self._get_movie_metadata(record["tmdb_id"], record["title"])
            if not metadata:
                self._track_missing_ids(
                    {"title": record["title"]},
                    f"not found in {Sources.TMDB.label} with ID {record['tmdb_id']}",
                )
                continue

            actual_tmdb_id = str(metadata.get("media_id", record["tmdb_id"]))
            if actual_tmdb_id in self.media_instances[MediaTypes.MOVIE.value]:
                continue

            # Check if it already exists in the database (e.g. if ID was resolved to existing)
            existing = self.existing_media[MediaTypes.MOVIE.value][Sources.TMDB.value].get(
                actual_tmdb_id,
            )
            if existing and self.mode == "new":
                self.media_instances[MediaTypes.MOVIE.value][actual_tmdb_id] = [existing]
                continue

            item = self._get_or_create_item(
                MediaTypes.MOVIE.value,
                actual_tmdb_id,
                metadata,
            )

            movie_obj = app.models.Movie(
                item=item,
                user=self.user,
                end_date=record["watched_at"],
                status=Status.COMPLETED.value,
            )
            # Apply rating from history if available, otherwise try library rating
            if record["rating"] is not None:
                movie_obj.score = record["rating"]
            else:
                # Try to get rating from library items
                rating = self._library_ratings.get(("tmdb", actual_tmdb_id))
                if rating is None and record.get("imdb_id"):
                    rating = self._library_ratings.get(("imdb", record["imdb_id"]))
                if rating is not None:
                    movie_obj.score = rating

            movie_obj._history_date = record["watched_at"]
            self.bulk_media[MediaTypes.MOVIE.value].append(movie_obj)
            self.media_instances[MediaTypes.MOVIE.value][actual_tmdb_id] = [movie_obj]
            self.summary_counts["created"] += 1

        logger.info("Bulk importing tv entries: %d", len(self._episode_records))
        for record in sorted(
            self._episode_records,
            key=lambda item: item["watched_at"],
        ):
            if self._should_skip_episode_record(record):
                continue

            tv_metadata = self._tv_metadata_cache.get(record["tmdb_id"])
            if not tv_metadata:
                self._track_missing_ids(
                    {"grandparentTitle": record["title"]},
                    f"not found in {Sources.TMDB.label} with ID {record['tmdb_id']}",
                )
                continue

            season_metadata = tv_metadata.get(
                f"season/{record['season_number']}",
            )
            if not season_metadata:
                self._track_missing_ids(
                    {"grandparentTitle": record["title"]},
                    f"season {record['season_number']} not found in {Sources.TMDB.label}",
                )
                continue

            episode_exists = any(
                ep["episode_number"] == record["episode_number"]
                for ep in season_metadata.get("episodes", [])
            )
            if not episode_exists:
                item_identifier = (
                    f"{tv_metadata['title']} "
                    f"S{record['season_number']}E{record['episode_number']}"
                )
                self.warnings.append(
                    f"{item_identifier}: not found in {Sources.TMDB.label} "
                    f"with ID {record['tmdb_id']}.",
                )
                self.summary_counts["skipped_missing_ids"] += 1
                continue

            actual_tmdb_id = str(tv_metadata.get("media_id", record["tmdb_id"]))
            tv_key = f"{actual_tmdb_id}"
            
            if tv_key in self.media_instances[MediaTypes.TV.value]:
                tv_obj = self.media_instances[MediaTypes.TV.value][tv_key][0]
            else:
                # Check if it already exists in the database
                existing = self.existing_media[MediaTypes.TV.value][Sources.TMDB.value].get(
                    actual_tmdb_id,
                )
                if existing and self.mode == "new":
                    tv_obj = existing
                    # Apply rating from library items if available and different
                    rating = self._library_ratings.get(("tmdb", actual_tmdb_id))
                    if rating is None:
                        # Title/search fallback can resolve a different show ID than the
                        # original Plex GUID, so keep using the resolved metadata payload
                        # if the cache is not also keyed by the final TMDB ID.
                        resolved_tv_metadata = (
                            self._tv_metadata_cache.get(actual_tmdb_id) or tv_metadata
                        )
                        tvdb_id = resolved_tv_metadata.get("tvdb_id")
                        if tvdb_id:
                            rating = self._library_ratings.get(("tvdb", str(tvdb_id)))
                    if rating is not None and tv_obj.score != rating:
                        tv_obj.score = rating
                        tv_obj.save(update_fields=["score"])
                        logger.debug(
                            "Applied library rating to existing TV show during Plex import",
                        )
                    self.media_instances[MediaTypes.TV.value][tv_key] = [tv_obj]
                else:
                    tv_item = self._get_or_create_item(
                        MediaTypes.TV.value,
                        actual_tmdb_id,
                        tv_metadata,
                    )
                    tv_obj = app.models.TV(
                        item=tv_item,
                        user=self.user,
                        status=Status.IN_PROGRESS.value,
                    )
                    # Apply rating from library items if available
                    rating = self._library_ratings.get(("tmdb", actual_tmdb_id))
                    if rating is None:
                        # Try TVDB fallback
                        tvdb_id = tv_metadata.get("tvdb_id")
                        if tvdb_id:
                            rating = self._library_ratings.get(("tvdb", str(tvdb_id)))
                    if rating is not None:
                        tv_obj.score = rating
                    tv_obj._history_date = record["watched_at"]
                    self.bulk_media[MediaTypes.TV.value].append(tv_obj)
                    self.media_instances[MediaTypes.TV.value][tv_key] = [tv_obj]

            season_key = f"{actual_tmdb_id}:{record['season_number']}"
            if season_key not in self.media_instances[MediaTypes.SEASON.value]:
                season_image = season_metadata.get("image") or tv_metadata.get("image")
                season_item = self._get_or_create_item(
                    MediaTypes.SEASON.value,
                    actual_tmdb_id,
                    {
                        "title": tv_metadata["title"],
                        "original_title": tv_metadata.get("original_title"),
                        "localized_title": tv_metadata.get("localized_title"),
                        "image": season_image,
                    },
                    season_number=record["season_number"],
                )
                season_obj = app.models.Season(
                    item=season_item,
                    user=self.user,
                    related_tv=tv_obj,
                    status=Status.IN_PROGRESS.value,
                )
                season_obj._history_date = record["watched_at"]
                self.bulk_media[MediaTypes.SEASON.value].append(season_obj)
                self.media_instances[MediaTypes.SEASON.value][season_key] = [
                    season_obj,
                ]
            else:
                season_obj = self.media_instances[MediaTypes.SEASON.value][season_key][0]

            episode_image = self._get_episode_image(
                record["episode_number"],
                season_metadata,
            )
            episode_item = self._get_or_create_item(
                MediaTypes.EPISODE.value,
                record["tmdb_id"],
                {
                    "title": tv_metadata["title"],
                    "original_title": tv_metadata.get("original_title"),
                    "localized_title": tv_metadata.get("localized_title"),
                    "image": episode_image,
                },
                season_number=record["season_number"],
                episode_number=record["episode_number"],
            )
            episode_obj = app.models.Episode(
                item=episode_item,
                related_season=season_obj,
                end_date=record["watched_at"],
            )
            episode_obj._history_date = record["watched_at"]
            self.bulk_media[MediaTypes.EPISODE.value].append(episode_obj)
            self.summary_counts["created"] += 1

            self._update_completion_status(
                season_obj,
                tv_obj,
                record["season_number"],
                record["episode_number"],
                season_metadata,
                tv_metadata,
            )

    def _should_skip_movie_record(self, record: dict) -> bool:
        """Check for duplicate movie history records."""
        key = (record["tmdb_id"], self._round_datetime(record["watched_at"]))
        if key in self._import_movie_keys:
            self.summary_counts["skipped_existing"] += 1
            return True

        self._import_movie_keys.add(key)

        if self.mode == "new" and key in self._existing_movie_keys:
            self.summary_counts["skipped_existing"] += 1
            return True

        return False

    def _should_skip_episode_record(self, record: dict) -> bool:
        """Check for duplicate episode history records."""
        import_key = self._build_episode_import_key(record)
        if import_key in self._import_episode_keys:
            self.summary_counts["skipped_existing"] += 1
            return True

        self._import_episode_keys.add(import_key)

        if self.mode == "new":
            existing_key = (
                record["tmdb_id"],
                record["season_number"],
                record["episode_number"],
                self._round_datetime(record["watched_at"]),
            )
            if existing_key in self._existing_episode_keys:
                self.summary_counts["skipped_existing"] += 1
                return True

        return False

    def _build_episode_import_key(self, record: dict) -> tuple:
        """Build a dedupe key for episode imports."""
        if record.get("plex_rating_key") and record.get("viewed_at_ts"):
            return ("plex", record["plex_rating_key"], record["viewed_at_ts"])

        return (
            "tmdb",
            record["tmdb_id"],
            record["season_number"],
            record["episode_number"],
            self._round_datetime(record["watched_at"]),
        )

    def _round_datetime(self, value: datetime) -> datetime:
        """Round datetimes to minute precision for replay-safe matching."""
        return timezone.localtime(value).replace(second=0, microsecond=0)

    def _warm_tv_metadata_cache(self):
        """Fetch TV metadata with season payloads in bulk."""
        seasons_by_show: dict[str, set[int]] = defaultdict(set)
        series_titles: dict[str, str | None] = {}
        for record in self._episode_records:
            seasons_by_show[record["tmdb_id"]].add(record["season_number"])
            if record["tmdb_id"] not in series_titles:
                series_titles[record["tmdb_id"]] = record.get("series_title") or record.get("title")

        for tmdb_id, seasons in seasons_by_show.items():
            missing_seasons = seasons - self._tv_seasons_loaded[tmdb_id]
            if not missing_seasons and tmdb_id in self._tv_metadata_cache:
                continue

            metadata = self._get_tv_metadata(
                tmdb_id,
                missing_seasons or seasons,
                series_titles.get(tmdb_id),
            )
            if not metadata:
                continue

            if tmdb_id in self._tv_metadata_cache:
                existing = self._tv_metadata_cache[tmdb_id]
                for season_number in missing_seasons:
                    season_key = f"season/{season_number}"
                    if metadata.get(season_key):
                        existing[season_key] = metadata[season_key]
                self._tv_metadata_cache[tmdb_id] = existing
            else:
                self._tv_metadata_cache[tmdb_id] = metadata

            self._tv_seasons_loaded[tmdb_id].update(seasons)

    def _get_movie_metadata(self, tmdb_id: str, title: str | None) -> dict | None:
        """Fetch and cache movie metadata."""
        if tmdb_id in self._movie_metadata_cache:
            return self._movie_metadata_cache[tmdb_id]

        try:
            metadata = services.get_media_metadata(
                MediaTypes.MOVIE.value,
                tmdb_id,
                Sources.TMDB.value,
            )
        except services.ProviderAPIError as error:
            if getattr(error, "status_code", None) == 404:
                self.warnings.append(
                    f"{title or tmdb_id}: not found in {Sources.TMDB.label} with ID {tmdb_id}.",
                )
                return None
            raise

        self._movie_metadata_cache[tmdb_id] = metadata
        return metadata

    def _get_tv_metadata(
        self,
        tmdb_id: str,
        season_numbers: set[int],
        series_title: str | None = None,
    ) -> dict | None:
        """Fetch TV metadata for the selected seasons, with title search fallback."""
        try:
            return services.get_media_metadata(
                "tv_with_seasons",
                tmdb_id,
                Sources.TMDB.value,
                season_numbers=sorted(season_numbers),
            )
        except services.ProviderAPIError as error:
            if getattr(error, "status_code", None) == 404:
                # If ID lookup failed, try title search fallback if we have a title
                if series_title:
                    logger.info(
                        "Plex TMDB ID lookup failed; trying title fallback search",
                    )
                    try:
                        search_results = services.search(
                            MediaTypes.TV.value,
                            series_title,
                            page=1,
                        )
                        if search_results and search_results.get("results"):
                            new_tmdb_id = str(search_results["results"][0]["media_id"])
                            logger.info(
                                "Resolved Plex TV metadata via title fallback search",
                            )
                            # Retry with new ID
                            return services.get_media_metadata(
                                "tv_with_seasons",
                                new_tmdb_id,
                                Sources.TMDB.value,
                                season_numbers=sorted(season_numbers),
                            )
                        
                        # If title has year in parenthesis like "Show (YYYY)", try stripping it
                        clean_title = re.sub(r'\s*\(\d{4}\)$', '', series_title[:500])
                        if clean_title != series_title:
                            logger.info("Retrying Plex TV title fallback search with normalized title")
                            search_results = services.search(
                                MediaTypes.TV.value,
                                clean_title,
                                page=1,
                            )
                            if search_results and search_results.get("results"):
                                new_tmdb_id = str(search_results["results"][0]["media_id"])
                                logger.info(
                                    "Resolved Plex TV metadata via normalized title fallback search",
                                )
                                return services.get_media_metadata(
                                    "tv_with_seasons",
                                    new_tmdb_id,
                                    Sources.TMDB.value,
                                    season_numbers=sorted(season_numbers),
                                )
                    except Exception as fallback_exc:
                        logger.warning(
                            "Plex TV title fallback search failed: %s",
                            exception_summary(fallback_exc),
                        )

                self.warnings.append(
                    f"{series_title or tmdb_id}: not found in {Sources.TMDB.label} with ID {tmdb_id}.",
                )
                return None
            raise

    def _get_or_create_item(
        self,
        media_type: str,
        tmdb_id: str,
        metadata: dict,
        season_number: int | None = None,
        episode_number: int | None = None,
    ):
        """Get or create an item in the database."""
        item_kwargs = {
            "media_id": tmdb_id,
            "source": Sources.TMDB.value,
            "media_type": media_type,
        }

        if season_number is not None:
            item_kwargs["season_number"] = season_number

        if episode_number is not None:
            item_kwargs["episode_number"] = episode_number

        defaults = {
            **app.models.Item.title_fields_from_metadata(metadata),
            "image": metadata["image"],
        }

        item, _ = helpers.retry_on_lock(
            lambda: app.models.Item.objects.get_or_create(
                **item_kwargs,
                defaults=defaults,
            ),
        )
        return item

    def _get_episode_image(self, episode_number: int, season_metadata: dict) -> str:
        """Extract episode image URL from season metadata."""
        for episode in season_metadata.get("episodes", []):
            if episode.get("episode_number") == episode_number:
                if episode.get("still_path"):
                    return f"https://image.tmdb.org/t/p/w500{episode['still_path']}"
                if episode.get("image"):
                    return episode["image"]
                break
        return settings.IMG_NONE

    def _update_completion_status(
        self,
        season_obj,
        tv_obj,
        season_number: int,
        episode_number: int,
        season_metadata: dict,
        tv_metadata: dict,
    ):
        """Update completion status for season and TV show if applicable."""
        if episode_number == season_metadata.get("max_progress"):
            season_obj.status = Status.COMPLETED.value

            last_season = tv_metadata.get("last_episode_season")
            if last_season and last_season == season_number:
                tv_obj.status = Status.COMPLETED.value

    def _build_metadata(self, entry: dict) -> dict:
        """Normalize metadata shape expected by Plex webhook processor."""
        metadata = dict(entry)
        metadata.setdefault("Guid", entry.get("Guid") or [])

        # Standardize keys casing
        for key in list(metadata.keys()):
            lower_key = key[0].lower() + key[1:] if key and key[0].isupper() else key
            if lower_key not in metadata:
                metadata[lower_key] = metadata[key]

        # Fallback: some history rows come without ratingKey; use key if present
        if not metadata.get("ratingKey") and metadata.get("key"):
            metadata["ratingKey"] = metadata["key"]
            metadata["ratingkey"] = metadata["key"]

        # Ensure duration is set if available from nested Media block
        if not metadata.get("duration"):
            media_block = metadata.get("Media") or metadata.get("media")
            if isinstance(media_block, list) and media_block:
                dur = media_block[0].get("duration")
                if dur:
                    metadata["duration"] = dur
                elif media_block[0].get("Part"):
                    part = media_block[0]["Part"]
                    if isinstance(part, list) and part:
                        dur = part[0].get("duration")
                        if dur:
                            metadata["duration"] = dur

        # Cast numeric fields for consistency
        for key in (
            "parentIndex",
            "index",
            "duration",
            "viewedAt",
            "lastViewedAt",
            "viewOffset",
        ):
            if key in metadata and metadata[key] is not None:
                try:
                    metadata[key] = int(metadata[key])
                except (TypeError, ValueError):
                    pass

        return metadata

    def _normalize_guid_list(self, guid_list):
        """Ensure GUID payload is a list of dicts with id keys."""
        if not guid_list:
            return []

        normalized = []
        if isinstance(guid_list, (dict, str)):
            guid_list = [guid_list]

        for guid in guid_list:
            if isinstance(guid, dict):
                guid_id = guid.get("id") or guid.get("Id") or guid.get("guid")
                if guid_id:
                    normalized.append({"id": guid_id})
            elif isinstance(guid, str):
                normalized.append({"id": guid})

        return normalized

    def _enqueue_fast_runtime_backfill(self):
        """Kick off fast runtime backfill immediately after import for statistics."""
        from app.tasks import fast_runtime_backfill_task  # local import to avoid cycles

        if MediaTypes.MUSIC.value not in self.counts:
            return  # No music imported, skip

        try:
            fast_runtime_backfill_task.delay(self.user.id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "Could not enqueue fast runtime backfill task: %s",
                exception_summary(exc),
            )

    def _enqueue_music_enrichment(self):
        """Kick off a post-import enrichment/dedupe pass for this user's music."""
        from app.tasks import (  # local import to avoid cycles
            enrich_albums_task,
            enrich_music_library_task,
        )

        if MediaTypes.MUSIC.value not in self.counts:
            return  # No music imported, skip

        try:
            enrich_music_library_task.delay(self.user.id)
            # Schedule album enrichment to run after artist enrichment
            # This processes albums that don't have MBIDs (those that didn't match discography)
            enrich_albums_task.delay(self.user.id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(
                "Could not enqueue music enrichment task: %s",
                exception_summary(exc),
            )

    def _prefetch_collected_album_covers(self):
        """Fetch missing album covers after the full import completes."""
        if not self._artists_for_prefetch:
            return
        from app.models import (
            Artist,  # local import to avoid circular import at module load
        )

        for artist_id in self._artists_for_prefetch:
            try:
                artist = Artist.objects.get(id=artist_id)
            except Artist.DoesNotExist:
                continue
            try:
                prefetch_album_covers(artist, limit=None)
            except Exception as exc:  # pragma: no cover - defensive network guard
                logger.debug(
                    "Cover prefetch failed after Plex import: %s",
                    exception_summary(exc),
                )
