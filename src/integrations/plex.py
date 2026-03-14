"""Helpers for interacting with the Plex APIs."""

import logging
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree

import requests
from django.conf import settings
from requests import RequestException

from app.log_safety import exception_summary

logger = logging.getLogger(__name__)


class PlexClientError(Exception):
    """Base Plex API error."""


class PlexAuthError(PlexClientError):
    """Raised when Plex authentication fails or a token is invalid."""


def _headers(token: str | None = None) -> dict[str, str]:
    """Return common Plex headers."""
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": settings.PLEX_PRODUCT,
        "X-Plex-Version": settings.PLEX_PLATFORM_VERSION,
        "X-Plex-Platform": settings.PLEX_PLATFORM,
        "X-Plex-Device": settings.PLEX_DEVICE,
        "X-Plex-Client-Identifier": settings.PLEX_CLIENT_IDENTIFIER,
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def create_pin() -> dict[str, Any]:
    """Create a Plex pin for the OAuth-style auth flow."""
    response = requests.post(
        "https://plex.tv/api/v2/pins",
        headers=_headers(),
        params={"strong": "true"},
        timeout=10,
    )
    _raise_for_auth(response)
    data = _parse_response(response)

    pin_id = data.get("id") or data.get("pin_id") or data.get("pin", {}).get("id")
    code = data.get("code") or data.get("pin", {}).get("code")

    if not pin_id or not code:
        raise PlexClientError("Unexpected response when creating Plex pin")

    return {"id": pin_id, "code": code}


def build_auth_url(code: str, redirect_uri: str) -> str:
    """Return the Plex auth URL for the pin."""
    encoded_redirect = quote_plus(redirect_uri)
    query = (
        f"clientID={settings.PLEX_CLIENT_IDENTIFIER}"
        f"&code={code}"
        f"&context%5Bdevice%5D%5Bproduct%5D={settings.PLEX_PRODUCT}"
        f"&context%5Bdevice%5D%5Bplatform%5D={settings.PLEX_PLATFORM}"
        f"&context%5Bdevice%5D%5Bdevice%5D={settings.PLEX_DEVICE}"
        f"&forwardUrl={encoded_redirect}"
    )
    return f"https://app.plex.tv/auth#?{query}"


def poll_pin(pin_id: str) -> str:
    """Poll the Plex pin endpoint to exchange for an auth token."""
    response = requests.get(
        f"https://plex.tv/api/v2/pins/{pin_id}",
        headers=_headers(),
        timeout=10,
    )
    _raise_for_auth(response)

    data = _parse_response(response)
    token = data.get("authToken") or data.get("auth_token") or data.get("token")

    if not token:
        raise PlexAuthError("Plex did not return an authentication token")

    return token


def fetch_account(token: str) -> dict[str, Any]:
    """Fetch Plex account details for the given token."""
    response = requests.get(
        "https://plex.tv/api/v2/user",
        headers=_headers(token),
        timeout=10,
    )
    _raise_for_auth(response)

    data = _parse_response(response)
    user = data.get("user") or data

    username = (
        user.get("username")
        or user.get("title")
        or user.get("friendlyName")
        or user.get("email")
    )

    return {
        "username": username,
        "id": user.get("id") or user.get("uuid"),
    }


def list_users(token: str) -> list[dict[str, Any]]:
    """Return Plex users available to the account (home + shared)."""
    users: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_user(user: dict[str, Any]) -> None:
        account_id = (
            user.get("id")
            or user.get("uuid")
            or user.get("account_id")
            or user.get("accountID")
        )
        if not account_id:
            return
        account_id = str(account_id)
        if account_id in seen:
            return
        seen.add(account_id)
        users.append(user)

    # Home users (JSON)
    try:
        response = requests.get(
            "https://plex.tv/api/v2/home/users",
            headers=_headers(token),
            timeout=10,
        )
        _raise_for_auth(response)
        payload = response.json()
        for user in _extract_users_payload(payload):
            if isinstance(user, dict):
                add_user(user)
    except (RequestException, ValueError) as exc:
        logger.debug("Could not fetch Plex home users: %s", exception_summary(exc))

    # Shared users (XML or JSON)
    try:
        response = requests.get(
            "https://plex.tv/api/users",
            headers=_headers(token),
            timeout=10,
        )
        _raise_for_auth(response)

        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type:
            payload = response.json()
            for user in _extract_users_payload(payload):
                if isinstance(user, dict):
                    add_user(user)
        else:
            root = ElementTree.fromstring(response.text or "")
            for node in root.findall("User"):
                add_user(dict(node.attrib))
    except (RequestException, ValueError, ElementTree.ParseError) as exc:
        logger.debug("Could not fetch Plex users: %s", exception_summary(exc))

    return users


def list_resources(token: str) -> list[dict[str, Any]]:
    """Return Plex server resources for the account."""
    response = requests.get(
        "https://plex.tv/api/resources",
        headers=_headers(token),
        params={"includeHttps": 1, "includeRelay": 1},
        timeout=15,
        verify=settings.PLEX_SSL_VERIFY,
    )
    _raise_for_auth(response)

    content = response.text or ""
    try:
        return _parse_resources_xml(content, fallback_token=token)
    except ElementTree.ParseError as exc:  # pragma: no cover - defensive
        logger.warning("Failed to parse Plex resources XML: %s", exception_summary(exc))
        return []


def list_sections(token: str) -> list[dict[str, Any]]:
    """Return all accessible library sections for the account."""
    sections: list[dict[str, Any]] = []
    seen = set()

    for server in list_resources(token):
        server_token = server.get("access_token") or token
        for connection in _sorted_connections(server.get("connections", [])):
            uri = connection.get("uri")
            if not uri:
                continue
            try:
                server_sections = _fetch_sections_from_connection(
                    connection,
                    server,
                    server_token,
                )
            except PlexClientError as exc:
                logger.info(
                    "Skipping Plex connection %s for %s: %s",
                    uri,
                    server.get("name"),
                    exc,
                )
                continue

            for section in server_sections:
                key = (section.get("id"), section.get("machine_identifier"))
                if key in seen:
                    continue
                seen.add(key)
                sections.append(section)

            # Break once we've successfully pulled sections for this server
            if server_sections:
                break

    return sections


def fetch_watchlist(
    token: str,
    start: int = 0,
    size: int = 100,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch a page of Plex Discover watchlist items."""
    params = {
        "X-Plex-Container-Start": start,
        "X-Plex-Container-Size": size,
    }

    try:
        response = requests.get(
            "https://discover.provider.plex.tv/library/sections/watchlist/all",
            headers=_headers(token),
            params=params,
            timeout=20,
            verify=settings.PLEX_SSL_VERIFY,
        )
    except RequestException as exc:
        raise PlexClientError(str(exc)) from exc
    _raise_for_auth(response)

    content_type = response.headers.get("Content-Type", "")
    if "json" not in content_type:
        raise PlexClientError("Unexpected Plex watchlist response format")

    payload = response.json()
    container = payload.get("MediaContainer") or payload
    entries = _extract_watchlist_entries(container)
    total = (
        container.get("totalSize")
        or container.get("size")
        or container.get("MetadataCount")
        or len(entries)
    )
    return entries, _coerce_int(total, len(entries))


def fetch_watchlist_metadata(token: str, rating_key: str) -> dict[str, Any]:
    """Fetch the full Discover metadata payload for a watchlist item."""
    try:
        response = requests.get(
            f"https://discover.provider.plex.tv/library/metadata/{rating_key}",
            headers=_headers(token),
            timeout=20,
            verify=settings.PLEX_SSL_VERIFY,
        )
    except RequestException as exc:
        raise PlexClientError(str(exc)) from exc
    _raise_for_auth(response)

    content_type = response.headers.get("Content-Type", "")
    if "json" not in content_type:
        raise PlexClientError("Unexpected Plex watchlist metadata response format")

    payload = response.json()
    container = payload.get("MediaContainer") or payload
    metadata = container.get("Metadata")
    if not isinstance(metadata, list) or not metadata:
        raise PlexClientError("Plex watchlist metadata payload was empty")

    return metadata[0]


def fetch_history(
    token: str,
    uri: str,
    section_id: str | None,
    start: int,
    size: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch Plex watch/listen history for a section."""
    page_size = size or settings.PLEX_HISTORY_PAGE_SIZE
    params = {
        "X-Plex-Token": token,
        "sort": "viewedAt:desc",
        "X-Plex-Container-Start": start,
        "X-Plex-Container-Size": page_size,
    }
    if section_id and section_id != "all":
        params["librarySectionID"] = section_id

    try:
        response = requests.get(
            f"{uri}/status/sessions/history/all",
            headers=_headers(token),
            params=params,
            timeout=20,
            verify=settings.PLEX_SSL_VERIFY,
        )
    except RequestException as exc:
        raise PlexClientError(str(exc)) from exc
    _raise_for_auth(response)

    content_type = response.headers.get("Content-Type", "")
    if "json" in content_type:
        payload = response.json()
        container = payload.get("MediaContainer") or {}
        entries = container.get("Metadata") or []
        total = container.get("totalSize") or container.get("size") or len(entries)
        return entries, _coerce_int(total, len(entries))

    try:
        entries, total = _parse_history_xml(response.text)
    except ElementTree.ParseError as exc:  # pragma: no cover - defensive
        raise PlexClientError(f"Could not parse Plex history: {exc}") from exc
    return entries, total


def fetch_section_all_items(
    token: str,
    uri: str,
    section_key: str,
    start: int = 0,
    size: int | None = None,
    max_retries: int = 3,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch all items from a Plex library section.
    
    Args:
        token: Plex authentication token
        uri: Plex server URI
        section_key: Section key (from section.get("key") or section.get("id"))
        start: Starting offset for pagination
        size: Number of items to fetch per page
        max_retries: Maximum number of retry attempts for network errors
        
    Returns:
        Tuple of (items list, total count)
    """
    import time
    
    page_size = size or settings.PLEX_HISTORY_PAGE_SIZE
    params = {
        "X-Plex-Token": token,
        "X-Plex-Container-Start": start,
        "X-Plex-Container-Size": page_size,
    }
    
    # Ensure section_key is numeric (section ID) or use it as-is if it's already a path
    if not section_key.startswith("/"):
        section_key = f"/library/sections/{section_key}"
    
    last_exc = None
    for attempt in range(max_retries):
        try:
            response = requests.get(
                f"{uri}{section_key}/all",
                headers=_headers(token),
                params=params,
                timeout=30,  # Increased timeout
                verify=settings.PLEX_SSL_VERIFY,
            )
            _raise_for_auth(response)
            break  # Success, exit retry loop
        except (RequestException, TimeoutError, ConnectionError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                # Exponential backoff: 1s, 2s, 4s
                wait_time = 2 ** attempt
                logger.debug(
                    "Plex API request failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1,
                    max_retries,
                    wait_time,
                    exc,
                )
                time.sleep(wait_time)
            else:
                # Last attempt failed
                logger.warning(
                    "Plex API request failed after %d attempts: %s",
                    max_retries,
                    exc,
                )
                raise PlexClientError(f"Failed after {max_retries} attempts: {exc}") from exc
    
    if last_exc:
        raise PlexClientError(str(last_exc)) from last_exc

    content_type = response.headers.get("Content-Type", "")
    if "json" in content_type:
        payload = response.json()
        container = payload.get("MediaContainer") or {}
        entries = container.get("Metadata") or []
        total = container.get("totalSize") or container.get("size") or len(entries)
        return entries, _coerce_int(total, len(entries))

    try:
        entries, total = _parse_history_xml(response.text)
    except ElementTree.ParseError as exc:  # pragma: no cover - defensive
        raise PlexClientError(f"Could not parse Plex library items: {exc}") from exc
    return entries, total


def fetch_metadata(token: str, uri: str, rating_key: str, timeout: int = 20) -> dict[str, Any] | None:
    """Fetch rich metadata for a history item.
    
    Args:
        token: Plex authentication token
        uri: Plex server URI
        rating_key: Plex rating key
        timeout: Request timeout in seconds (default: 20)
    """
    try:
        response = requests.get(
            f"{uri}/library/metadata/{rating_key}",
            headers=_headers(token),
            params={"X-Plex-Token": token},
            timeout=timeout,
            verify=settings.PLEX_SSL_VERIFY,
        )
    except RequestException as exc:
        raise PlexClientError(str(exc)) from exc
    if response.status_code == 404:
        return None
    _raise_for_auth(response)

    content_type = response.headers.get("Content-Type", "")
    if "json" in content_type:
        container = response.json().get("MediaContainer") or {}
        metadata = container.get("Metadata") or []
        return metadata[0] if metadata else None

    try:
        entries, _ = _parse_history_xml(response.text)
    except ElementTree.ParseError as exc:  # pragma: no cover - defensive
        raise PlexClientError(f"Could not parse Plex metadata: {exc}") from exc
    return entries[0] if entries else None


def _fetch_sections_from_connection(
    connection: dict[str, Any],
    server: dict[str, Any],
    token: str,
) -> list[dict[str, Any]]:
    """Fetch library sections from a specific Plex server connection."""
    uri = connection.get("uri")
    if not uri:
        raise PlexClientError("Connection is missing a URI")

    try:
        response = requests.get(
            f"{uri}/library/sections",
            headers=_headers(token),
            params={"X-Plex-Token": token},
            timeout=10,
            verify=settings.PLEX_SSL_VERIFY,
        )
    except RequestException as exc:
        raise PlexClientError(str(exc)) from exc
    if response.status_code == 401:
        raise PlexAuthError("Plex token is unauthorized for this server")

    if not response.ok:
        raise PlexClientError(f"Failed to fetch library sections: {response.text}")

    content_type = response.headers.get("Content-Type", "")
    if "json" in content_type:
        payload = response.json()
        container = payload.get("MediaContainer") or {}
        directories = container.get("Directory") or []
    else:
        try:
            directories = _parse_sections_xml(response.text)
        except ElementTree.ParseError as exc:  # pragma: no cover - defensive
            raise PlexClientError(f"Could not parse library response: {exc}") from exc

    sections: list[dict[str, Any]] = []
    for directory in directories:
        attrs = directory if isinstance(directory, dict) else getattr(directory, "attrib", {})
        sections.append(
            {
                "id": attrs.get("key"),
                "title": attrs.get("title"),
                "type": attrs.get("type"),
                "server_name": server.get("name"),
                "machine_identifier": server.get("machine_identifier"),
                "uri": uri,
            },
        )
    return sections


def _sorted_connections(connections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort connections to prefer relay/HTTPS endpoints over local ones."""

    def score(conn: dict[str, Any]):
        uri = conn.get("uri", "")
        protocol = (conn.get("protocol") or "").lower()
        is_https = uri.startswith("https://") or protocol == "https"
        is_relay = str(conn.get("relay", "")).lower() in ("1", "true")
        is_local = str(conn.get("local", "")).lower() in ("1", "true")
        # Lower scores first: relay > non-local > https > local
        return (
            0 if is_relay else 1,
            0 if not is_local else 1,
            0 if is_https else 1,
        )

    try:
        return sorted(connections, key=score)
    except Exception:  # pragma: no cover - defensive
        return connections


def _parse_sections_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse a Plex library sections XML payload."""
    root = ElementTree.fromstring(xml_text)
    return [child.attrib for child in root.findall("Directory")]


def _parse_resources_xml(
    xml_text: str,
    fallback_token: str | None = None,
) -> list[dict[str, Any]]:
    """Parse Plex resources XML payload into server connection info."""
    root = ElementTree.fromstring(xml_text)
    servers: list[dict[str, Any]] = []

    for device in root.findall("Device"):
        provides = device.attrib.get("provides", "")
        if "server" not in provides:
            continue

        connections = [
            dict(conn.attrib)
            for conn in device.findall("Connection")
            if conn.attrib.get("uri")
        ]

        servers.append(
            {
                "name": device.attrib.get("name"),
                "machine_identifier": device.attrib.get("clientIdentifier"),
                "access_token": device.attrib.get("accessToken") or fallback_token,
                "connections": connections,
            },
        )

    return servers


def _parse_history_xml(xml_text: str) -> tuple[list[dict[str, Any]], int]:
    """Parse Plex history XML into dictionaries."""
    root = ElementTree.fromstring(xml_text)
    entries: list[dict[str, Any]] = []

    for child in root:
        data = dict(child.attrib)
        data["type"] = data.get("type") or child.tag.lower()
        data["Guid"] = [guid.attrib for guid in child.findall("Guid")]
        entries.append(data)

    total_size = _coerce_int(root.attrib.get("totalSize") or root.attrib.get("size"), len(entries))
    return entries, total_size


def _parse_response(response: requests.Response) -> dict[str, Any]:
    """Parse Plex API responses that may be JSON or XML."""
    content_type = response.headers.get("Content-Type", "")

    if "json" in content_type:
        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise PlexClientError("Invalid JSON from Plex") from exc

    try:
        root = ElementTree.fromstring(response.text or "")
    except ElementTree.ParseError as exc:  # pragma: no cover - defensive
        raise PlexClientError("Invalid XML from Plex") from exc

    # Flatten simple XML responses (like pin endpoints)
    data = dict(root.attrib)
    for child in root:
        data[child.tag.lower()] = child.attrib
    return data


def _extract_users_payload(payload: Any) -> list[dict[str, Any]]:
    """Extract user lists from Plex API payloads."""
    if isinstance(payload, dict):
        users = payload.get("users")
        if isinstance(users, list):
            return users

        container = payload.get("MediaContainer") or {}
        users = container.get("User")
        if isinstance(users, list):
            return users
        if isinstance(users, dict):
            return [users]

    return []


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort int conversion with fallback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_external_ids_from_guids(guids: list[dict[str, Any] | str]) -> dict[str, str]:
    """Extract external IDs (TMDB, IMDB, TVDB) from Plex GUIDs.
    
    Args:
        guids: List of GUID dictionaries or strings from Plex metadata
        
    Returns:
        Dictionary with keys: 'tmdb_id', 'imdb_id', 'tvdb_id', 'plex_guid' (if found)
    """
    import re
    
    external_ids = {}
    
    for guid in guids:
        guid_value = guid.get("id") if isinstance(guid, dict) else guid
        if not guid_value:
            continue
            
        guid_lower = guid_value.lower()

        if guid_lower.startswith("plex://") and "plex_guid" not in external_ids:
            external_ids["plex_guid"] = guid_value.split("plex://", 1)[1]
        
        # Extract TMDB ID
        if "tmdb" in guid_lower or "themoviedb" in guid_lower:
            match = re.search(r"\d+", guid_value)
            if match and "tmdb_id" not in external_ids:
                external_ids["tmdb_id"] = match.group(0)
        
        # Extract IMDB ID (changed from elif to if so all IDs can be extracted)
        if "imdb" in guid_lower:
            match = re.search(r"tt\d+", guid_value)
            if match and "imdb_id" not in external_ids:
                external_ids["imdb_id"] = match.group(0)
        
        # Extract TVDB ID (changed from elif to if so all IDs can be extracted)
        if "tvdb" in guid_lower:
            match = re.search(r"\d+", guid_value)
            if match and "tvdb_id" not in external_ids:
                external_ids["tvdb_id"] = match.group(0)
    
    return external_ids


def _extract_watchlist_entries(container: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize common Plex watchlist payload shapes into entry dictionaries."""
    metadata = container.get("Metadata")
    if isinstance(metadata, list):
        return metadata

    hub_entries = container.get("Hub")
    if isinstance(hub_entries, list):
        collected: list[dict[str, Any]] = []
        for hub in hub_entries:
            hub_metadata = hub.get("Metadata")
            if isinstance(hub_metadata, list):
                collected.extend(hub_metadata)
        if collected:
            return collected

    return []


def _raise_for_auth(response: requests.Response):
    """Raise auth errors consistently."""
    if response.status_code == 401:
        raise PlexAuthError("Plex token is invalid or expired")
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise PlexClientError(
            f"Plex request failed with status {response.status_code}",
        ) from exc
