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
    access_token = models.TextField(help_text="Encrypted JWT access token")
    refresh_token = models.TextField(
        blank=True,
        null=True,
        help_text="Encrypted refresh token",
    )
    token_expires_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
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
        - We have an access token, AND
        - Either the token is not expired, OR we have a refresh token to renew it
        """
        if not self.access_token:
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
