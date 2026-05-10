"""Retry helpers for transient SQLite database errors."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db.utils import OperationalError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

LOCK_ERROR_SIGNALS = (
    "database is locked",
    "database table is locked",
    "database file is locked",
)

DISK_IO_ERROR_SIGNALS = (
    "disk i/o error",
    "disk i/o",
    "i/o error",
    "unable to open database file",
    "readonly database",
)


def is_lock_error(error: BaseException) -> bool:
    """Return True if the OperationalError was caused by a SQLite lock."""
    message = str(error).lower()
    return any(signal in message for signal in LOCK_ERROR_SIGNALS)


def is_disk_io_error(error: BaseException) -> bool:
    """Return True if the OperationalError was caused by a disk I/O error."""
    message = str(error).lower()
    return any(signal in message for signal in DISK_IO_ERROR_SIGNALS)


def is_retryable_error(error: BaseException) -> bool:
    """Return True if the OperationalError is retryable."""
    return is_lock_error(error) or is_disk_io_error(error)


def _error_type(error: BaseException) -> str:
    return "disk I/O" if is_disk_io_error(error) else "lock"


@dataclass(slots=True)
class RetryableDatabaseOutcome:
    """Result wrapper for retryable database work."""

    value: Any
    deferred: bool = False


def run_retryable_db_operation(
    operation: Callable[[], Any],
    *,
    mode: str = "required",
    fallback: Any = None,
    operation_name: str = "database operation",
    operation_logger: logging.Logger | None = None,
    max_retries: int = 5,
    base_delay: float = 0.1,
    backoff: float = 2.0,
    on_deferred: Callable[[OperationalError], None] | None = None,
) -> RetryableDatabaseOutcome:
    """Run database work with retry/backoff for transient SQLite failures."""
    if mode not in {"required", "best_effort"}:
        msg = f"Unsupported retryable database mode: {mode}"
        raise ValueError(msg)

    active_logger = operation_logger or logger
    attempt = 0

    while True:
        try:
            return RetryableDatabaseOutcome(operation(), deferred=False)
        except OperationalError as error:
            if not is_retryable_error(error):
                raise

            if attempt >= max_retries:
                if mode != "best_effort":
                    raise

                if on_deferred is not None:
                    on_deferred(error)

                active_logger.warning(
                    "Deferring best-effort %s after %s %s error attempts",
                    operation_name,
                    attempt + 1,
                    _error_type(error),
                )
                value = fallback() if callable(fallback) else fallback
                return RetryableDatabaseOutcome(value, deferred=True)

            sleep_for = base_delay * (backoff**attempt)
            active_logger.warning(
                (
                    "Retrying %s after %s error "
                    "(attempt %s/%s, sleeping %.2fs)"
                ),
                operation_name,
                _error_type(error),
                attempt + 1,
                max_retries,
                sleep_for,
            )
            time.sleep(sleep_for)
            attempt += 1
