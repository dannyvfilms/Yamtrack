"""Helpers for interacting with the Pocket Casts API."""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import requests
from django.utils import timezone

logger = logging.getLogger(__name__)

POCKETCASTS_API_BASE_URL = "https://api.pocketcasts.com"


class PocketCastsClientError(Exception):
    """Base Pocket Casts API error."""


class PocketCastsAuthError(PocketCastsClientError):
    """Raised when Pocket Casts authentication fails or a token is invalid."""


def login(email: str, password: str) -> dict[str, Any]:
    """Login to Pocket Casts with email and password.
    
    Returns:
        Dict with accessToken and refreshToken
    """
    url = f"{POCKETCASTS_API_BASE_URL}/user/login"
    payload = {"email": email, "password": password}
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Log response keys for debugging (but not the actual values for security)
        if isinstance(data, dict):
            logger.debug("Pocket Casts login response keys: %s", list(data.keys()))
            # Log a sanitized version of the response (hide token values)
            sanitized = {k: ("***" if "token" in k.lower() or "password" in k.lower() else v)
                        for k, v in data.items() if not isinstance(v, (dict, list))}
            logger.debug("Pocket Casts login response (sanitized): %s", sanitized)
        else:
            logger.debug("Pocket Casts login response type: %s", type(data).__name__)

        # Check for accessToken in various possible field names
        access_token = None
        refresh_token = None

        # First, check if data is nested (e.g., {"data": {...}})
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
            data = data["data"]

        # Try different possible field names
        if "accessToken" in data:
            access_token = data["accessToken"]
        elif "access_token" in data:
            access_token = data["access_token"]
        elif "token" in data:
            access_token = data["token"]
        elif isinstance(data, dict) and len(data) == 1:
            # Sometimes APIs return just the token as a single value
            first_value = next(iter(data.values()))
            if isinstance(first_value, str) and len(first_value) > 50:
                access_token = first_value

        if not access_token:
            # Log the response structure (sanitized) for debugging
            error_msg = "Invalid response from Pocket Casts login"
            if isinstance(data, dict):
                logger.error("Login response missing accessToken. Response keys: %s", list(data.keys()))
                # Check for error messages in the response
                if "error" in data:
                    error_msg = f"Pocket Casts error: {data['error']}"
                elif "message" in data:
                    error_msg = f"Pocket Casts message: {data['message']}"
            else:
                logger.error("Login response is not a dict. Type: %s, Value (first 200 chars): %s",
                           type(data).__name__, str(data)[:200])
            raise PocketCastsAuthError(error_msg)

        # Check for refreshToken in various possible field names
        if "refreshToken" in data:
            refresh_token = data["refreshToken"]
        elif "refresh_token" in data:
            refresh_token = data["refresh_token"]
        elif "refreshToken" in data:
            refresh_token = data.get("refreshToken", "")

        return {
            "accessToken": access_token,
            "refreshToken": refresh_token or "",
        }
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            raise PocketCastsAuthError("Invalid email or password")
        raise PocketCastsClientError(f"Pocket Casts API error: {e.response.status_code}") from e
    except requests.RequestException as e:
        raise PocketCastsClientError(f"Failed to connect to Pocket Casts: {e}") from e


def refresh_token(refresh_token: str) -> dict[str, Any]:
    """Refresh an access token using a refresh token.
    
    Returns:
        Dict with accessToken and refreshToken
    """
    url = f"{POCKETCASTS_API_BASE_URL}/user/refresh"
    payload = {"refreshToken": refresh_token}
    headers = {
        "Content-Type": "application/json",
        "Accept": "*/*",
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        if "accessToken" not in data:
            logger.error("Token refresh response missing accessToken. Response keys: %s", list(data.keys()) if isinstance(data, dict) else "not a dict")
            raise PocketCastsAuthError("Invalid response from token refresh")

        logger.debug("Token refresh successful")
        return {
            "accessToken": data["accessToken"],
            "refreshToken": data.get("refreshToken", refresh_token),
        }
    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response else None
        try:
            error_body = e.response.text[:500] if e.response else "No response"
            logger.error("Token refresh failed with status %d. Response: %s", status_code, error_body)
        except Exception:
            logger.error("Token refresh failed with status %d (could not read response body)", status_code)

        if status_code == 401:
            raise PocketCastsAuthError("Refresh token is invalid or expired")
        raise PocketCastsClientError(f"Pocket Casts API error: {status_code}") from e
    except requests.RequestException as e:
        logger.error("Network error during token refresh: %s", e)
        raise PocketCastsClientError(f"Failed to refresh token: {e}") from e


def parse_token_expiration(access_token: str) -> datetime:
    """Parse expiration time from JWT access token.
    
    Returns:
        datetime when token expires (UTC)
    """
    try:
        decoded = jwt.decode(access_token, options={"verify_signature": False})
        exp = decoded.get("exp")
        if exp:
            return datetime.fromtimestamp(exp, tz=UTC)
    except Exception:
        pass

    # Fallback: assume 1 hour expiration
    return timezone.now() + timedelta(hours=1)


def validate_token(access_token: str) -> bool:
    """Validate an access token by making a test API call.
    
    Args:
        access_token: The JWT access token to validate
        
    Returns:
        True if token is valid, False otherwise
    """
    url = f"{POCKETCASTS_API_BASE_URL}/user/history"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "X-App-Language": "en",
        "X-User-Region": "global",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    }

    try:
        response = requests.post(url, json={}, headers=headers, timeout=10)
        # 200 or 201 means valid token (even if no episodes)
        if response.status_code in (200, 201):
            logger.debug("Access token validation successful (status %d)", response.status_code)
            return True
        # 401 means invalid token
        if response.status_code == 401:
            try:
                response_text = response.text
                body_length = len(response_text) if isinstance(response_text, str) else 0
                logger.warning(
                    "Access token validation failed with 401 (response_length=%d)",
                    body_length,
                )
            except Exception:
                logger.warning("Access token validation failed with 401")
            return False
        # Other errors might be temporary, but we'll consider token potentially valid
        # if it's not an auth error
        logger.warning("Access token validation returned unexpected status %d", response.status_code)
        return response.status_code < 500
    except requests.RequestException as e:
        # Network errors - can't validate, assume invalid to be safe
        logger.error("Network error during access token validation: %s", e)
        return False


def get_podcast_list(access_token: str) -> dict[str, Any]:
    """Fetch the user's podcast list with metadata.
    
    Args:
        access_token: The JWT access token
        
    Returns:
        Dict with 'podcasts' list containing show metadata including descriptions
    """
    url = f"{POCKETCASTS_API_BASE_URL}/user/podcast/list"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "X-App-Language": "en",
        "X-User-Region": "global",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    }

    try:
        response = requests.post(url, json={}, headers=headers, timeout=10)
        # Don't raise on 401 - just return empty list so import can continue
        if response.status_code == 401:
            logger.warning("Unauthorized access to podcast list - token may be expired")
            return {"podcasts": []}
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error("Failed to fetch podcast list: %s", e)
        return {"podcasts": []}


def get_podcast_image_url(podcast_uuid: str, size: int = 130) -> str:
    """Get the image URL for a podcast show.
    
    Args:
        podcast_uuid: The podcast UUID
        size: Image size (130 appears to be standard, but other sizes may exist)
        
    Returns:
        URL to the podcast artwork image
    """
    return f"{POCKETCASTS_API_BASE_URL}/discover/images/{size}/{podcast_uuid}.jpg"
