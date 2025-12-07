"""Helpers for interacting with the Plex APIs."""

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus
from xml.etree import ElementTree

import requests
from requests import RequestException
from django.conf import settings

logger = logging.getLogger(__name__)


class PlexClientError(Exception):
    """Base Plex API error."""


class PlexAuthError(PlexClientError):
    """Raised when Plex authentication fails or a token is invalid."""


def _headers(token: str | None = None) -> Dict[str, str]:
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


def create_pin() -> Dict[str, Any]:
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


def fetch_account(token: str) -> Dict[str, Any]:
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


def list_resources(token: str) -> List[Dict[str, Any]]:
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
        logger.warning("Failed to parse Plex resources XML: %s", exc)
        return []


def list_sections(token: str) -> List[Dict[str, Any]]:
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


def fetch_history(
    token: str,
    uri: str,
    section_id: Optional[str],
    start: int,
    size: Optional[int] = None,
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


def fetch_metadata(token: str, uri: str, rating_key: str) -> Optional[Dict[str, Any]]:
    """Fetch rich metadata for a history item."""
    try:
        response = requests.get(
            f"{uri}/library/metadata/{rating_key}",
            headers=_headers(token),
            params={"X-Plex-Token": token},
            timeout=10,
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
    connection: Dict[str, Any],
    server: Dict[str, Any],
    token: str,
) -> List[Dict[str, Any]]:
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
            }
        )
    return sections


def _sorted_connections(connections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort connections to prefer relay/HTTPS endpoints over local ones."""

    def score(conn: Dict[str, Any]):
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


def _parse_sections_xml(xml_text: str) -> List[Dict[str, Any]]:
    """Parse a Plex library sections XML payload."""
    root = ElementTree.fromstring(xml_text)
    return [child.attrib for child in root.findall("Directory")]


def _parse_resources_xml(
    xml_text: str,
    fallback_token: Optional[str] = None,
) -> List[Dict[str, Any]]:
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
            }
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


def _parse_response(response: requests.Response) -> Dict[str, Any]:
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


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort int conversion with fallback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _raise_for_auth(response: requests.Response):
    """Raise auth errors consistently."""
    if response.status_code == 401:
        raise PlexAuthError("Plex token is invalid or expired")
    response.raise_for_status()
