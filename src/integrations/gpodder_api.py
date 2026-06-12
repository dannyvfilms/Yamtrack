"""Client helpers for GPodder-compatible sync servers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from django.conf import settings

from app.log_safety import exception_summary, safe_url

logger = logging.getLogger(__name__)

USER_AGENT = "Yamtrack/1.0 (https://github.com/FuzzyGrim/Yamtrack)"
DEFAULT_SERVER_URL = "https://gpodder.net"


class GPodderError(Exception):
    """Base error for GPodder API calls."""


class GPodderAuthError(GPodderError):
    """Authentication failed."""


class GPodderClientError(GPodderError):
    """Unexpected or invalid response from the upstream server."""


@dataclass(frozen=True)
class GPodderCredentials:
    """Resolved connection details for a GPodder account."""

    server_url: str
    username: str
    password: str


def normalize_server_url(server_url: str | None) -> str:
    """Normalize a user-supplied GPodder server URL."""
    value = (server_url or DEFAULT_SERVER_URL).strip()
    if not value:
        value = DEFAULT_SERVER_URL
    if "://" not in value:
        value = f"https://{value}"

    parts = urlsplit(value)
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def api_url(server_url: str, path: str) -> str:
    """Return an absolute API URL for the configured server."""
    base = f"{normalize_server_url(server_url).rstrip('/')}/"
    return urljoin(base, path.lstrip("/"))


def normalize_external_url(url: str | None) -> str:
    """Normalize external feed and enclosure URLs for matching."""
    value = (url or "").strip()
    if not value:
        return ""

    parts = urlsplit(value)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    path = parts.path or ""
    if path.endswith("/"):
        path = path.rstrip("/")

    query_items = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
    ]
    query = urlencode(sorted(query_items))
    return urlunsplit((scheme, netloc, path, query, ""))


def action_candidates(action: dict) -> set[str]:
    """Return the normalized URLs that might identify an episode action."""
    candidates = set()
    for key in ("episode", "url"):
        normalized = normalize_external_url(action.get(key))
        if normalized:
            candidates.add(normalized)

    for mapping_key in ("update_urls", "updated_urls"):
        mappings = action.get(mapping_key)
        if not isinstance(mappings, list):
            continue
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            for candidate_key in ("old", "new", "episode", "url"):
                normalized = normalize_external_url(mapping.get(candidate_key))
                if normalized:
                    candidates.add(normalized)
    return candidates


def subscription_candidates(feed_url: str, action: dict | None = None) -> set[str]:
    """Return normalized URLs that might identify a podcast feed."""
    candidates = {normalize_external_url(feed_url)}
    action = action or {}
    for key in ("podcast", "feed"):
        normalized = normalize_external_url(action.get(key))
        if normalized:
            candidates.add(normalized)
    for mapping_key in ("update_urls", "updated_urls"):
        mappings = action.get(mapping_key)
        if not isinstance(mappings, list):
            continue
        for mapping in mappings:
            if not isinstance(mapping, dict):
                continue
            for candidate_key in ("old", "new", "podcast", "feed"):
                normalized = normalize_external_url(mapping.get(candidate_key))
                if normalized:
                    candidates.add(normalized)
    return {candidate for candidate in candidates if candidate}


def _request(method: str, server_url: str, path: str, *, username: str, password: str, **kwargs):
    url = api_url(server_url, path)
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    timeout = kwargs.pop("timeout", settings.REQUEST_TIMEOUT)
    try:
        response = requests.request(
            method,
            url,
            auth=(username, password),
            headers=headers,
            timeout=timeout,
            **kwargs,
        )
    except requests.RequestException as exc:
        logger.warning("GPodder request failed for %s: %s", safe_url(url), exception_summary(exc))
        raise GPodderClientError(str(exc)) from exc

    if response.status_code in {401, 403}:
        raise GPodderAuthError("Invalid GPodder credentials.")
    if response.status_code >= 400:
        raise GPodderClientError(
            f"GPodder request failed with status {response.status_code}: {response.text[:300]}"
        )
    return response


def verify_login(credentials: GPodderCredentials) -> None:
    """Verify credentials against the login endpoint."""
    _request(
        "POST",
        credentials.server_url,
        f"/api/2/auth/{credentials.username}/login.json",
        username=credentials.username,
        password=credentials.password,
    )


def register_device(credentials: GPodderCredentials, device_id: str) -> None:
    """Best-effort registration of the Yamtrack device."""
    payload = {"caption": "Yamtrack", "type": "server"}
    _request(
        "POST",
        credentials.server_url,
        f"/api/2/devices/{credentials.username}/{device_id}.json",
        username=credentials.username,
        password=credentials.password,
        json=payload,
    )


def fetch_subscriptions(credentials: GPodderCredentials) -> list[str]:
    """Fetch the user's current subscription list."""
    response = _request(
        "GET",
        credentials.server_url,
        f"/subscriptions/{credentials.username}.json",
        username=credentials.username,
        password=credentials.password,
    )
    payload = response.json()
    if not isinstance(payload, list):
        raise GPodderClientError("Invalid GPodder subscriptions response.")
    return [subscription for subscription in payload if isinstance(subscription, str)]


def fetch_episode_actions(
    credentials: GPodderCredentials,
    *,
    since: int | None,
    device: str = "",
) -> tuple[list[dict], int | None]:
    """Fetch raw episode actions since the last successful cursor."""
    params = {}
    if since is not None:
        params["since"] = since
    if device:
        params["device"] = device

    response = _request(
        "GET",
        credentials.server_url,
        f"/api/2/episodes/{credentials.username}.json",
        username=credentials.username,
        password=credentials.password,
        params=params,
    )
    payload = response.json()
    if not isinstance(payload, dict):
        raise GPodderClientError("Invalid GPodder episode actions response.")

    actions = payload.get("actions", [])
    timestamp = payload.get("timestamp")
    if not isinstance(actions, list):
        raise GPodderClientError("Invalid GPodder episode actions payload.")
    if timestamp is not None:
        try:
            timestamp = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise GPodderClientError("Invalid GPodder episode actions cursor.") from exc
    return [action for action in actions if isinstance(action, dict)], timestamp
