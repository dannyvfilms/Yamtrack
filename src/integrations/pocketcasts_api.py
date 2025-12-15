"""Helpers for interacting with the Pocket Casts API."""

import logging
from typing import Any, Dict

import jwt
import requests
from django.conf import settings
from django.utils import timezone
from datetime import datetime, timedelta, timezone as dt_timezone

logger = logging.getLogger(__name__)

POCKETCASTS_API_BASE_URL = "https://api.pocketcasts.com"


class PocketCastsClientError(Exception):
    """Base Pocket Casts API error."""


class PocketCastsAuthError(PocketCastsClientError):
    """Raised when Pocket Casts authentication fails or a token is invalid."""


def login(email: str, password: str) -> Dict[str, Any]:
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
        
        if "accessToken" not in data:
            raise PocketCastsAuthError("Invalid response from Pocket Casts login")
        
        return {
            "accessToken": data["accessToken"],
            "refreshToken": data.get("refreshToken", ""),
        }
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            raise PocketCastsAuthError("Invalid email or password")
        raise PocketCastsClientError(f"Pocket Casts API error: {e.response.status_code}") from e
    except requests.RequestException as e:
        raise PocketCastsClientError(f"Failed to connect to Pocket Casts: {e}") from e


def refresh_token(refresh_token: str) -> Dict[str, Any]:
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
            raise PocketCastsAuthError("Invalid response from token refresh")
        
        return {
            "accessToken": data["accessToken"],
            "refreshToken": data.get("refreshToken", refresh_token),
        }
    except requests.HTTPError as e:
        if e.response.status_code == 401:
            raise PocketCastsAuthError("Refresh token is invalid or expired")
        raise PocketCastsClientError(f"Pocket Casts API error: {e.response.status_code}") from e
    except requests.RequestException as e:
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
            return datetime.fromtimestamp(exp, tz=dt_timezone.utc)
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
            return True
        # 401 means invalid token
        if response.status_code == 401:
            return False
        # Other errors might be temporary, but we'll consider token potentially valid
        # if it's not an auth error
        return response.status_code < 500
    except requests.RequestException:
        # Network errors - can't validate, assume invalid to be safe
        return False


def get_podcast_list(access_token: str) -> Dict[str, Any]:
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

