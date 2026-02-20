"""Models for integration data."""

from django.conf import settings
from django.db import models
from django.utils import timezone


class PlexAccount(models.Model):
    """Store Plex authentication and cached library data for a user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="plex_account",
    )
    plex_token = models.CharField(max_length=255)
    plex_username = models.CharField(max_length=255)
    plex_account_id = models.CharField(max_length=255, blank=True, null=True)
    server_name = models.CharField(max_length=255, blank=True, null=True)
    machine_identifier = models.CharField(max_length=255, blank=True, null=True)
    sections = models.JSONField(default=list, blank=True)
    sections_refreshed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model options."""

        verbose_name = "Plex account"
        verbose_name_plural = "Plex accounts"

    def __str__(self):
        """Readable representation."""
        return f"PlexAccount({self.plex_username})"

    @property
    def is_connected(self):
        """Return True when we have a token stored."""
        return bool(self.plex_token)


class PocketCastsAccount(models.Model):
    """Store Pocket Casts authentication tokens for a user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pocketcasts_account",
    )
    access_token = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted JWT access token (cached from login)",
    )
    refresh_token = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted refresh token (cached from login)",
    )
    email = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted email address for login",
    )
    password = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted password for login",
    )
    token_expires_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    connection_broken = models.BooleanField(
        default=False,
        help_text="True if connection is broken (refresh failed) but credentials are preserved",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model options."""

        verbose_name = "Pocket Casts account"
        verbose_name_plural = "Pocket Casts accounts"

    def __str__(self):
        """Readable representation."""
        return f"PocketCastsAccount({self.user.username})"

    @property
    def is_connected(self):
        """Return True when we have a valid connection.
        
        A connection is valid if:
        - We have email AND password (can always re-login), OR
        - We have an access token (and it's not expired, or we have refresh token to renew it)
        - Connection is not marked as broken
        """
        # If we have credentials (email and password), we can always reconnect
        has_credentials = bool(self.email and self.password)

        # If connection is marked as broken and we don't have credentials, not connected
        if self.connection_broken and not has_credentials:
            return False

        # If we have credentials, we're connected (can always re-login)
        if has_credentials:
            return True

        # Legacy: check for access token
        if not self.access_token:
            return False

        # If connection is marked as broken, not connected
        if self.connection_broken:
            return False

        # If token is not expired, we're connected
        if not self.is_token_expired:
            return True

        # If token is expired but we have a refresh token, we can still refresh
        if self.refresh_token:
            return True

        # Token is expired and no refresh token - not connected
        return False

    @property
    def is_token_expired(self):
        """Return True if the token is expired."""
        if not self.token_expires_at:
            return False
        return timezone.now() >= self.token_expires_at


class AudiobookshelfAccount(models.Model):
    """Store Audiobookshelf connection settings and sync state for a user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="audiobookshelf_account",
    )
    base_url = models.URLField(help_text="Audiobookshelf server URL")
    api_token = models.TextField(help_text="Encrypted Audiobookshelf API token")
    sync_finished = models.BooleanField(
        default=True,
        help_text="Import finished items as completed entries",
    )
    create_missing = models.BooleanField(
        default=True,
        help_text="Create Yamtrack items when ABS items cannot be matched",
    )
    last_sync_ms = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Last imported Audiobookshelf progress timestamp (milliseconds)",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True)
    connection_broken = models.BooleanField(default=False)
    last_error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model options."""

        verbose_name = "Audiobookshelf account"
        verbose_name_plural = "Audiobookshelf accounts"

    def __str__(self):
        """Readable representation."""
        return f"AudiobookshelfAccount({self.user.username})"

    @property
    def is_connected(self):
        """Return True when the account appears connected."""
        return bool(self.base_url and self.api_token) and not self.connection_broken


class LastFMAccount(models.Model):
    """Store Last.fm username and sync state for a user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="lastfm_account",
    )
    lastfm_username = models.CharField(max_length=255)
    last_fetch_timestamp_uts = models.IntegerField(
        null=True,
        blank=True,
        help_text="Unix timestamp (seconds) of last successful poll",
    )
    last_sync_at = models.DateTimeField(null=True, blank=True)
    connection_broken = models.BooleanField(
        default=False,
        help_text="True if connection is broken (invalid username or persistent errors)",
    )
    failure_count = models.IntegerField(
        default=0,
        help_text="Number of consecutive failures",
    )
    last_error_code = models.CharField(
        max_length=10,
        blank=True,
        help_text="Last.fm API error code (e.g., '29' for rate limit)",
    )
    last_error_message = models.TextField(
        blank=True,
        help_text="Human-readable error message",
    )
    last_failed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model options."""

        verbose_name = "Last.fm account"
        verbose_name_plural = "Last.fm accounts"

    def __str__(self):
        """Readable representation."""
        return f"LastFMAccount({self.lastfm_username})"

    @property
    def is_connected(self):
        """Return True when we have a valid connection."""
        return bool(self.lastfm_username) and not self.connection_broken


class TraktAccount(models.Model):
    """Store Trakt API client credentials for a user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trakt_account",
    )
    client_id = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted Trakt client ID",
    )
    client_secret = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted Trakt client secret",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Model options."""

        verbose_name = "Trakt account"
        verbose_name_plural = "Trakt accounts"

    def __str__(self):
        """Readable representation."""
        return f"TraktAccount({self.user.username})"

    @property
    def is_configured(self):
        """Return True when client credentials are stored."""
        return bool(self.client_id and self.client_secret)
