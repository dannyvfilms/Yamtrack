"""Django settings for Yamtrack project."""

import hashlib
import json
import os
import subprocess
import sys
import warnings
import zoneinfo
from pathlib import Path
from urllib.parse import urljoin, urlparse

from celery.schedules import crontab
from decouple import (
    Config,
    Csv,
    RepositorySecret,
    Undefined,
    UndefinedValueError,
    config,
    undefined,
)
from debug_toolbar.settings import PANELS_DEFAULTS
from django.core.cache import CacheKeyWarning
from django.db.backends.signals import connection_created

BASE_URL = config("BASE_URL", default=None)
if BASE_URL:
    FORCE_SCRIPT_NAME = BASE_URL

REDIS_PREFIX = config("REDIS_PREFIX", default=None)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


def secret(key, default=undefined, **kwargs):
    """Try to read a config value from a secret file.

    If only the filename is given, try to read from /run/secrets/<key>.
    If an absolute path is specified, try to read from this path.
    """
    if isinstance(default, Undefined):
        default = None

    file = config(key, default, **kwargs)

    if file is None:
        return undefined
    if file == default:
        return default

    path = Path(file)
    try:
        if path.is_absolute():
            secret_value = Config(RepositorySecret(path.parent))(
                path.stem,
                default,
                **kwargs,
            )
        else:
            secret_value = Config(RepositorySecret())(file, default, **kwargs)
    except (
        FileNotFoundError,
        IsADirectoryError,
        UndefinedValueError,
    ) as err:
        msg = f"File from {key} not found. Please check the path and filename."
        raise UndefinedValueError(msg) from err
    else:
        if isinstance(secret_value, str):
            return secret_value.strip()
        return secret_value


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/stable/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config(
    "SECRET",
    default=secret("SECRET_FILE", default="ifx7bdUWo5EwC2NQNihjRjOrW00Cdv5Y"),
)


# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config("DEBUG", default=True, cast=bool)
ENABLE_DEBUG_TOOLBAR = DEBUG and config(
    "ENABLE_DEBUG_TOOLBAR",
    default=True,
    cast=bool,
)
DEBUG_TOOLBAR_INCLUDE_TEMPLATES_PANEL = config(
    "DEBUG_TOOLBAR_INCLUDE_TEMPLATES_PANEL",
    default=False,
    cast=bool,
)

INTERNAL_IPS = ["127.0.0.1"]

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="*", cast=Csv())

if ALLOWED_HOSTS != ["*"]:
    if "localhost" not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append("localhost")
    if "127.0.0.1" not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append("127.0.0.1")


CSRF_TRUSTED_ORIGINS = config("CSRF", default="", cast=Csv())
CSRF_FAILURE_VIEW = "app.error_views.csrf_failure"

URLS = config("URLS", default="", cast=Csv())

for url in URLS:
    CSRF_TRUSTED_ORIGINS.append(url)
    ALLOWED_HOSTS.append(urlparse(url).hostname)

if BASE_URL:
    # Cookie paths must match FORCE_SCRIPT_NAME exactly to ensure browsers
    # send cookies with all requests under the base URL prefix
    CSRF_COOKIE_PATH = BASE_URL

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Application definition

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.admin",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "app",
    "events",
    "integrations",
    "lists",
    "users",
    "django_celery_beat",
    "django_celery_results",
    "django_select2",
    "simple_history",
    "widget_tweaks",
    "health_check",
    "health_check.cache",
    "health_check.storage",
    "health_check.contrib.migrations",
    "health_check.contrib.celery_ping",
    "health_check.contrib.redis",
    "health_check.contrib.db_heartbeat",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "django.contrib.humanize",
]

if ENABLE_DEBUG_TOOLBAR:
    INSTALLED_APPS.append("debug_toolbar")

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "app.middleware.DatabaseRetryMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "app.middleware.DiscoverWarmupMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "app.middleware.ProviderAPIErrorMiddleware",
    "app.middleware.ErrorCaptureMiddleware",
]

if ENABLE_DEBUG_TOOLBAR:
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.media",
                "app.context_processors.export_vars",
                "app.context_processors.media_enums",
                "django.template.context_processors.request",
            ],
        },
    },
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

WSGI_APPLICATION = "config.wsgi.application"

# Database
# https://docs.djangoproject.com/en/stable/ref/settings/#databases

# create db folder if it doesn't exist
Path(BASE_DIR / "db").mkdir(parents=True, exist_ok=True)

if config("DB_HOST", default=None):
    DB_POOL_ENABLED = config("DB_POOL_ENABLED", default=False, cast=bool)
    DB_POOL_MIN = config("DB_POOL_MIN", default=0, cast=int)
    DB_POOL_MAX = config("DB_POOL_MAX", default=2, cast=int)
    DB_POOL_TIMEOUT = config("DB_POOL_TIMEOUT", default=30, cast=int)
    db_options = {}
    if DB_POOL_ENABLED:
        db_options["pool"] = {
            "min_size": DB_POOL_MIN,
            "max_size": DB_POOL_MAX,
            "timeout": DB_POOL_TIMEOUT,
        }

    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": config("DB_HOST"),
            "NAME": config("DB_NAME", default=secret("DB_NAME_FILE")),
            "USER": config("DB_USER", default=secret("DB_USER_FILE")),
            "PASSWORD": config("DB_PASSWORD", default=secret("DB_PASSWORD_FILE")),
            "PORT": config("DB_PORT"),
            "OPTIONS": db_options,
        },
    }

    sslmode = config("DB_SSL_MODE", default=None)
    if sslmode:
        DATABASES["default"]["OPTIONS"]["sslmode"] = sslmode

    sslcertmode = config("DB_SSL_CERT_MODE", default=None)
    if sslcertmode:
        DATABASES["default"]["OPTIONS"]["sslcertmode"] = sslcertmode

else:
    SQLITE_BUSY_TIMEOUT_SECONDS = config(
        "SQLITE_BUSY_TIMEOUT_SECONDS",
        default=30,
        cast=int,
    )
    SQLITE_JOURNAL_MODE = config("SQLITE_JOURNAL_MODE", default="WAL")
    SQLITE_SYNCHRONOUS = config("SQLITE_SYNCHRONOUS", default="NORMAL")

    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db" / "db.sqlite3",
            "OPTIONS": {
                "timeout": SQLITE_BUSY_TIMEOUT_SECONDS,
            },
        },
    }

    def configure_sqlite_connection(sender, connection, **_kwargs):
        """Ensure SQLite connections wait for locks and use WAL."""
        if connection.vendor != "sqlite":
            return

        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute(f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE}")
            cursor.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS}")
            cursor.execute(
                f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}",
            )
        except Exception as error:
            # Log but don't raise - allow connection to proceed even if PRAGMA fails
            # This prevents disk I/O errors during connection setup from blocking all requests
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(
                "Failed to configure SQLite connection PRAGMA settings: %s",
                error,
            )
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    # Ignore errors when closing cursor
                    pass

    connection_created.connect(configure_sqlite_connection)

# Cache
# https://docs.djangoproject.com/en/stable/topics/cache/
CACHE_TIMEOUT = 86400  # 24 hours
REDIS_URL = config("REDIS_URL", default="redis://localhost:6379")
KEY_PREFIX = f"{REDIS_PREFIX}" if REDIS_PREFIX else ""
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "TIMEOUT": CACHE_TIMEOUT,
        "VERSION": 14,
        "KEY_PREFIX": KEY_PREFIX,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    },
}

# not using Memcached, ignore CacheKeyWarning
# https://docs.djangoproject.com/en/stable/topics/cache/#cache-key-warnings
warnings.simplefilter("ignore", CacheKeyWarning)

# Sessions
# Use Redis cache backend for sessions to avoid database dependency
# This improves resilience to disk I/O errors
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"


# Password validation
# https://docs.djangoproject.com/en/stable/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
]

# Logging
# https://docs.djangoproject.com/en/stable/topics/logging/
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        "requests_ratelimiter.requests_ratelimiter": {
            "level": "DEBUG" if DEBUG else "WARNING",
        },
        "psycopg": {
            "level": "DEBUG" if DEBUG else "WARNING",
        },
        "urllib3": {
            "level": "DEBUG" if DEBUG else "WARNING",
        },
    },
    "formatters": {
        "verbose": {
            # format consistent with gunicorn's
            "format": "[{asctime}] [{process}] [{levelname}] {message}",
            "datefmt": "%Y-%m-%d %H:%M:%S %z",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "level": "DEBUG" if DEBUG else "INFO",
        },
    },
    "root": {"handlers": ["console"], "level": "DEBUG" if DEBUG else "INFO"},
}

# Internationalization
# https://docs.djangoproject.com/en/stable/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = os.getenv("TZ", "UTC")

USE_I18N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/stable/howto/static-files/

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

STATICFILES_DIRS = [BASE_DIR / "static"]

if BASE_URL:
    STATIC_URL = f"{BASE_URL}/static/"

# Default primary key field type
# https://docs.djangoproject.com/en/stable/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Auth settings

LOGIN_URL = "account_login"

LOGIN_REDIRECT_URL = "home"

AUTH_USER_MODEL = "users.User"

# Yamtrack settings

# For CSV imports
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB


def _clean_metadata_value(value):
    """Normalize version metadata values from the environment."""
    if value is None:
        return None

    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() in {"unknown", "none"}:
        return None
    return cleaned


def _find_git_dir(start_dir=BASE_DIR):
    """Search upward from start_dir for a git directory or gitdir file."""
    start_path = Path(start_dir).resolve()

    for candidate in (start_path, *start_path.parents):
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return dot_git

        if not dot_git.is_file():
            continue

        try:
            dot_git_contents = dot_git.read_text().strip()
        except (OSError, UnicodeDecodeError):
            continue

        if not dot_git_contents.startswith("gitdir:"):
            continue

        _, git_dir_path = dot_git_contents.split(":", 1)
        git_dir = (candidate / git_dir_path.strip()).resolve()
        if git_dir.exists():
            return git_dir

    return None


def _read_git_ref(git_dir, ref_path):
    """Read a git ref from loose refs or packed-refs."""
    ref_file = git_dir / ref_path
    try:
        ref_value = ref_file.read_text().strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        ref_value = None

    if ref_value:
        return ref_value

    packed_refs = git_dir / "packed-refs"
    try:
        packed_ref_lines = packed_refs.read_text().splitlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    for raw_line in packed_ref_lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", "^")):
            continue

        try:
            sha, name = line.split(" ", 1)
        except ValueError:
            continue

        if name == ref_path:
            return sha

    return None


def _get_local_commit_hash(base_dir=BASE_DIR):
    """Return the current commit hash from the local git checkout if available."""
    git_rev_parse_command = ["git", "rev-parse", "HEAD"]
    try:
        git_rev = subprocess.run(
            git_rev_parse_command,
            cwd=base_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if git_rev:
            return git_rev
    except (OSError, subprocess.SubprocessError):
        pass

    git_dir = _find_git_dir(base_dir)
    if git_dir is None:
        return None

    head_file = git_dir / "HEAD"
    try:
        head_contents = head_file.read_text().strip()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None

    if head_contents.startswith("ref:"):
        _, ref_path = head_contents.split(":", 1)
        return _read_git_ref(git_dir, ref_path.strip())

    return head_contents or None


def _get_env_commit_hash():
    """Return the build/deployment commit hash from the environment."""
    return _clean_metadata_value(
        config("COMMIT_SHA", default=None)
        or config("GIT_COMMIT", default=None)
        or config("GITHUB_SHA", default=None),
    )


def _get_local_version(base_dir=BASE_DIR, commit_sha=None):
    """Return a version string from the local git checkout if available."""
    git_describe_command = ["git", "describe", "--tags", "--always", "--dirty"]
    try:
        git_describe = subprocess.run(
            git_describe_command,
            cwd=base_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if git_describe:
            return git_describe
    except (OSError, subprocess.SubprocessError):
        pass

    local_commit = commit_sha or _get_local_commit_hash(base_dir)
    if local_commit:
        return local_commit[:7]
    return None


def _select_commit_hash(local_commit_sha, env_commit_sha):
    """Prefer runtime checkout metadata over build metadata."""
    return local_commit_sha or env_commit_sha


def _select_version(local_version, local_commit_sha, env_version, env_commit_sha):
    """Pick the most accurate version string available for the running code."""
    if local_version:
        return local_version

    if env_version and (not local_commit_sha or local_commit_sha == env_commit_sha):
        return env_version

    if local_commit_sha:
        return local_commit_sha[:7]

    if env_version:
        return env_version

    if env_commit_sha:
        return env_commit_sha[:7]

    return "dev"


ENV_VERSION_RAW = _clean_metadata_value(config("VERSION", default=None))
ENV_COMMIT_SHA = _get_env_commit_hash()
LOCAL_COMMIT_SHA = _get_local_commit_hash()
LOCAL_VERSION = _get_local_version(commit_sha=LOCAL_COMMIT_SHA)

COMMIT_SHA = _select_commit_hash(LOCAL_COMMIT_SHA, ENV_COMMIT_SHA)
COMMIT_SHA_SHORT = COMMIT_SHA[:7] if COMMIT_SHA else None

VERSION = _select_version(
    LOCAL_VERSION,
    LOCAL_COMMIT_SHA,
    ENV_VERSION_RAW,
    ENV_COMMIT_SHA,
)


def _parse_repo_owner(value):
    if not value:
        return None
    value = value.strip()
    if value.startswith("git@") and ":" in value:
        value = value.split(":", 1)[1]
    parsed = urlparse(value)
    repo_path = parsed.path if parsed.netloc else value
    repo_path = repo_path.strip("/")
    if repo_path.endswith(".git"):
        repo_path = repo_path[:-4]
    if not repo_path:
        return None
    return repo_path.split("/", 1)[0]


def _parse_repo_slug(value):
    if not value:
        return None
    value = value.strip()
    if value.startswith("git@") and ":" in value:
        value = value.split(":", 1)[1]
    parsed = urlparse(value)
    repo_path = parsed.path if parsed.netloc else value
    repo_path = repo_path.strip("/")
    if repo_path.endswith(".git"):
        repo_path = repo_path[:-4]
    if "/" not in repo_path:
        return None
    owner, repo = repo_path.split("/", 1)
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"

def _read_fork_owner_file():
    file_paths = []
    configured_path = config("FORK_OWNER_FILE", default=None)
    if configured_path:
        file_paths.append(Path(configured_path))
    file_paths.append(BASE_DIR / ".fork_owner")
    file_paths.append(Path("/etc/yamtrack/fork_owner"))

    for path in file_paths:
        try:
            value = path.read_text().strip()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            continue
        if value:
            return value
    return None


def _get_fork_owner():
    owner = config("FORK_OWNER_NAME", default=None) or config("GITHUB_REPOSITORY_OWNER", default=None)
    if owner:
        return owner.strip()

    owner = _parse_repo_owner(config("GITHUB_REPOSITORY", default=None))
    if owner:
        return owner

    file_owner = _read_fork_owner_file()
    if file_owner:
        return _parse_repo_owner(file_owner) or file_owner.strip()

    try:
        git_remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=BASE_DIR,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        return _parse_repo_owner(git_remote)
    except (OSError, subprocess.SubprocessError):
        return None


def _get_fork_repository():
    for value in (
        config("FORK_REPOSITORY", default=None),
        config("GITHUB_REPOSITORY", default=None),
    ):
        repository = _parse_repo_slug(value)
        if repository:
            return repository

    file_owner = _read_fork_owner_file()
    repository = _parse_repo_slug(file_owner)
    if repository:
        return repository

    try:
        git_remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=BASE_DIR,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        return _parse_repo_slug(git_remote)
    except (OSError, subprocess.SubprocessError):
        return None


FORK_OWNER_NAME = _get_fork_owner()
FORK_OWNER_URL = config("FORK_OWNER_URL", default=None)
if not FORK_OWNER_URL:
    fork_repository = _get_fork_repository()
    if fork_repository:
        FORK_OWNER_URL = f"https://github.com/{fork_repository}"
    elif FORK_OWNER_NAME:
        FORK_OWNER_URL = f"https://github.com/{FORK_OWNER_NAME}"

ADMIN_ENABLED = config("ADMIN_ENABLED", default=False, cast=bool)

TRACK_TIME = config("TRACK_TIME", default=True, cast=bool)

BACKUP_DIR = config("BACKUP_DIR", default=str(BASE_DIR / "backups"))

# Runtime population settings
RUNTIME_POPULATION_DISABLED = config("RUNTIME_POPULATION_DISABLED", default=False, cast=bool)
RUNTIME_POPULATION_ON_STARTUP = config("RUNTIME_POPULATION_ON_STARTUP", default=False, cast=bool)
DISCOVER_WARMUP_ON_STARTUP = config(
    "DISCOVER_WARMUP_ON_STARTUP",
    default=not DEBUG,
    cast=bool,
)

TZ = zoneinfo.ZoneInfo(TIME_ZONE)

IMG_NONE = "https://www.themoviedb.org/assets/2/v4/glyphicons/basic/glyphicons-basic-38-picture-grey-c2ebdbb057f2a7614185931650f8cee23fa137b93812ccb132b9df511df1cfac.svg"

REQUEST_TIMEOUT = 120  # seconds
PER_PAGE = 24

TMDB_API = config(
    "TMDB_API",
    default=secret(
        "TMDB_API_FILE",
        "61572be02f0a068658828f6396aacf60",
    ),
)
TMDB_NSFW = config("TMDB_NSFW", default=False, cast=bool)
TMDB_LANG = config("TMDB_LANG", default="en")

TVDB_API_KEY = config(
    "TVDB_API_KEY",
    default=secret(
        "TVDB_API_KEY_FILE",
        "",
    ),
)
TVDB_PIN = config(
    "TVDB_PIN",
    default=secret(
        "TVDB_PIN_FILE",
        "",
    ),
)

MAL_API = config(
    "MAL_API",
    default=secret(
        "MAL_API_FILE",
        "25b5581dafd15b3e7d583bb79e9a1691",
    ),
)
MAL_NSFW = config("MAL_NSFW", default=False, cast=bool)

MU_NSFW = config("MU_NSFW", default=False, cast=bool)

IGDB_ID = config(
    "IGDB_ID",
    default=secret(
        "IGDB_ID_FILE",
        "8wqmm7x1n2xxtnz94lb8mthadhtgrt",
    ),
)
IGDB_SECRET = config(
    "IGDB_SECRET",
    default=secret(
        "IGDB_SECRET_FILE",
        "ovbq0hwscv58hu46yxn50hovt4j8kj",
    ),
)
IGDB_NSFW = config("IGDB_NSFW", default=False, cast=bool)

# BoardGameGeek API Token - Register at https://boardgamegeek.com/using_the_xml_api
BGG_API_TOKEN = config(
    "BGG_API_TOKEN",
    default=secret(
        "BGG_API_TOKEN_FILE",
        "92f43ab1-d1d5-4e18-8b82-d1f56dc12927",
    ),
)

STEAM_API_KEY = config(
    "STEAM_API_KEY",
    default=secret(
        "STEAM_API_KEY_FILE",
        "",
    ),  # Generate default key https://steamcommunity.com/dev/apikey
)

HARDCOVER_API = config(
    "HARDCOVER_API",
    default=secret(
        "HARDCOVER_API_FILE",
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJIYXJkY292ZXIiLCJ2ZXJzaW9uIjoiOCIsImp0"
        "aSI6ImJhNGNjZmUwLTgwZmQtNGI3NC1hZDdhLTlkNDM5ZTA5YWMzOSIsImFwcGxpY2F0aW9uSWQi"
        "OjIsInN1YiI6IjM0OTUxIiwiYXVkIjoiMSIsImlkIjoiMzQ5NTEiLCJsb2dnZWRJbiI6dHJ1ZSwi"
        "aWF0IjoxNzQ2OTc3ODc3LCJleHAiOjE3Nzg1MTM4NzcsImh0dHBzOi8vaGFzdXJhLmlvL2p3dC9j"
        "bGFpbXMiOnsieC1oYXN1cmEtYWxsb3dlZC1yb2xlcyI6WyJ1c2VyIl0sIngtaGFzdXJhLWRlZmF1"
        "bHQtcm9sZSI6InVzZXIiLCJ4LWhhc3VyYS1yb2xlIjoidXNlciIsIlgtaGFzdXJhLXVzZXItaWQi"
        "OiIzNDk1MSJ9LCJ1c2VyIjp7ImlkIjozNDk1MX19.edcEqLAeO3uH5xxBTFDKtyWwi-B-WfXX_yi"
        "LFdOAJ3c",
    ),
)

COMICVINE_API = config(
    "COMICVINE_API",
    default=secret(
        "COMICVINE_API_FILE",
        "cdab0706269e4bca03a096fbc39920dadf7e4992",
    ),
)

TRAKT_API = config(
    "TRAKT_API",
    default=secret(
        "TRAKT_API_FILE",
        "b4d9702b11cfaddf5e863001f68ce9d4394b678926e8a3f64d47bf69a55dd0fe",
    ),
)

TRAKT_API_SECRET = config(
    "TRAKT_API_SECRET",
    default=secret(
        "TRAKT_API_SECRET_FILE",
        "",
    ),
)

ANILIST_ID = config(
    "ANILIST_ID",
    default=secret(
        "ANILIST_ID_FILE",
        "",
    ),
)

ANILIST_SECRET = config(
    "ANILIST_SECRET",
    default=secret(
        "ANILIST_SECRET_FILE",
        "",
    ),
)

SIMKL_ID = config(
    "SIMKL_ID",
    default=secret(
        "SIMKL_ID_FILE",
        "a973e57e85d94068315d5ac29669d85da8abc0fb7aff1d22e00e04bdf1882578",
    ),
)
SIMKL_SECRET = config(
    "SIMKL_SECRET",
    default=secret(
        "SIMKL_SECRET_FILE",
        "1b548a88ac7884a757cc58a552842913a9337f3cab3a4905836c6dc305dda316",
    ),
)

DEFAULT_PLEX_CLIENT_IDENTIFIER = hashlib.sha256(SECRET_KEY.encode()).hexdigest()[:24]
PLEX_CLIENT_IDENTIFIER = config(
    "PLEX_CLIENT_IDENTIFIER",
    default=DEFAULT_PLEX_CLIENT_IDENTIFIER,
)
PLEX_PRODUCT = config("PLEX_PRODUCT", default="Yamtrack")
PLEX_DEVICE = config("PLEX_DEVICE", default="Yamtrack Importer")
PLEX_PLATFORM = config("PLEX_PLATFORM", default="Yamtrack")
PLEX_PLATFORM_VERSION = config("PLEX_PLATFORM_VERSION", default=VERSION)
PLEX_SSL_VERIFY = config("PLEX_SSL_VERIFY", default=False, cast=bool)
PLEX_SECTIONS_TTL_HOURS = config("PLEX_SECTIONS_TTL_HOURS", default=24, cast=int)
PLEX_HISTORY_PAGE_SIZE = config("PLEX_HISTORY_PAGE_SIZE", default=200, cast=int)
PLEX_HISTORY_MAX_ITEMS = config("PLEX_HISTORY_MAX_ITEMS", default=0, cast=int)

LASTFM_API_KEY = config("LASTFM_API_KEY", default="")
LASTFM_POLL_INTERVAL_MINUTES = config("LASTFM_POLL_INTERVAL_MINUTES", default=15, cast=int)
LASTFM_HISTORY_PAGES_PER_TASK = config("LASTFM_HISTORY_PAGES_PER_TASK", default=5, cast=int)

TESTING = False

HEALTHCHECK_CELERY_PING_TIMEOUT = config(
    "HEALTHCHECK_CELERY_PING_TIMEOUT",
    default=1,
    cast=int,
)

# Third party settings

DEBUG_TOOLBAR_CONFIG = {
    "SKIP_TEMPLATE_PREFIXES": (
        "django/forms/widgets/",
        "admin/widgets/",
    ),
    "ROOT_TAG_EXTRA_ATTRS": "hx-preserve",
}
DEBUG_TOOLBAR_PANELS = [
    panel
    for panel in PANELS_DEFAULTS
    if (
        DEBUG_TOOLBAR_INCLUDE_TEMPLATES_PANEL
        or panel != "debug_toolbar.panels.templates.TemplatesPanel"
    )
]

SELECT2_CACHE_BACKEND = "default"
SELECT2_JS = [
    "js/libraries/jquery-3.7.1.min.js",
    "js/libraries/select2-4.1.0.min.js",
]
SELECT2_I18N_PATH = "js/i18n"
SELECT2_CSS = [
    "css/libraries/select2-4.1.0.min.css",
]
SELECT2_THEME = "tailwindcss-4"

# Celery settings

CELERY_BROKER_URL = REDIS_URL
CELERY_TIMEZONE = TIME_ZONE

if REDIS_PREFIX:
    CELERY_BROKER_TRANSPORT_OPTIONS = {
        "global_keyprefix": f"{REDIS_PREFIX}",
        "queue_prefix": f"{REDIS_PREFIX}",
    }

CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_WORKER_CONCURRENCY = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 50
CELERY_BEAT_SYNC_EVERY = 10

CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 60 * 6  # 6 hours

CELERY_RESULT_EXTENDED = True
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_CACHE_BACKEND = "default"
CELERY_RESULT_EXPIRES = 60 * 60 * 24 * 7  # 7 days
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# https://docs.celeryq.dev/en/stable/userguide/configuration.html#task-serializer
CELERY_TASK_SERIALIZER = "pickle"
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std-setting-accept_content
CELERY_ACCEPT_CONTENT = ["application/json", "application/x-python-serialize", "application/x-pickle"]


DAILY_DIGEST_HOUR = config(
    "DAILY_DIGEST_HOUR",
    default=8,
    cast=int,
)
CELERY_BEAT_SCHEDULE = {
    "reload_calendar": {
        "task": "Reload calendar",
        "schedule": 60 * 60 * 24,  # every 24 hours
    },
    "send_release_notifications": {
        "task": "Send release notifications",
        "schedule": 60 * 10,  # every 10 minutes
    },
    "send_daily_digest": {
        "task": "Send daily digest",
        "schedule": crontab(hour=DAILY_DIGEST_HOUR, minute=0),
    },
    "backfill_item_metadata": {
        "task": "Backfill item metadata",
        "schedule": crontab(hour=3, minute=0),  # every day at 3 AM
        "kwargs": {
            "batch_size": 1000,
            "game_length_batch_size": 200,
        },  # Process 1000 items per run plus a bounded HLTB enrichment sweep.
    },
    "backfill_item_metadata_incremental": {
        "task": "Backfill item metadata",
        "schedule": crontab(minute="*/15"),  # every 15 minutes for gradual convergence
        "kwargs": {
            "batch_size": 150,
            "game_length_batch_size": 25,
        },
    },
    "nightly_metadata_quality_backfill": {
        "task": "Nightly metadata quality backfill",
        "schedule": crontab(hour=3, minute=30),  # every day at 3:30 AM
        "kwargs": {
            "genre_batch_size": 1500,
            "runtime_batch_size": 500,
            "episode_season_batch_size": 300,
            "credits_batch_size": 2500,
            "credits_scan_multiplier": 20,
            "trakt_popularity_batch_size": 300,
        },
    },
    "ensure_genre_backfill_reconcile": {
        "task": "Ensure genre backfill reconcile",
        "schedule": 60 * 5,  # every 5 minutes until current strategy version is reconciled
        "kwargs": {
            "batch_size": 1500,
        },
    },
    "warm_discover_api_cache": {
        "task": "Warm Discover API Cache",
        "schedule": 60 * 60,  # every 1 hour
    },
    "refresh_discover_profiles": {
        "task": "Refresh Discover Profiles",
        "schedule": crontab(hour=4, minute=0),  # every day at 4 AM
    },
}

IS_PROD = not any(cmd in sys.argv for cmd in ("runserver", "test"))
if IS_PROD:
    ALLAUTH_TRUSTED_CLIENT_IP_HEADER = "X-Real-IP"

# Allauth settings
if CSRF_TRUSTED_ORIGINS:
    # Check if all origins start with http:// or https://
    all_http = all(
        origin.startswith("http://") for origin in CSRF_TRUSTED_ORIGINS if origin
    )
    all_https = all(
        origin.startswith("https://") for origin in CSRF_TRUSTED_ORIGINS if origin
    )

    if all_http:
        ACCOUNT_DEFAULT_HTTP_PROTOCOL = "http"
    elif all_https:
        ACCOUNT_DEFAULT_HTTP_PROTOCOL = "https"
    else:
        # Mixed protocols or invalid formats, use config value
        ACCOUNT_DEFAULT_HTTP_PROTOCOL = config(
            "ACCOUNT_DEFAULT_HTTP_PROTOCOL",
            default="https",
        )
else:
    # Empty CSRF_TRUSTED_ORIGINS, default to http
    ACCOUNT_DEFAULT_HTTP_PROTOCOL = "http"

ACCOUNT_LOGOUT_REDIRECT_URL = config(
    "ACCOUNT_LOGOUT_REDIRECT_URL",
    default="/accounts/login/?loggedout=1",
)
ACCOUNT_SESSION_REMEMBER = True
ACCOUNT_USER_MODEL_EMAIL_FIELD = None
ACCOUNT_FORMS = {
    "login": "users.forms.CustomLoginForm",
    "signup": "users.forms.CustomSignupForm",
}

if BASE_URL:
    # Join base only if relative URL
    if not urlparse(ACCOUNT_LOGOUT_REDIRECT_URL).netloc:
        ACCOUNT_LOGOUT_REDIRECT_URL = urljoin(BASE_URL, ACCOUNT_LOGOUT_REDIRECT_URL)
    # Cookie paths must match FORCE_SCRIPT_NAME exactly to ensure browsers
    # send session cookies with all requests under the base URL prefix
    SESSION_COOKIE_PATH = BASE_URL

SOCIALACCOUNT_LOGIN_ON_GET = True

SOCIAL_PROVIDERS = config("SOCIAL_PROVIDERS", default="", cast=Csv())
INSTALLED_APPS += SOCIAL_PROVIDERS

SOCIALACCOUNT_PROVIDERS = config(
    "SOCIALACCOUNT_PROVIDERS",
    default=secret(
        "SOCIALACCOUNT_PROVIDERS_FILE",
        default="{}",
    ),
    cast=json.loads,
)

SOCIALACCOUNT_ONLY = config("SOCIALACCOUNT_ONLY", default=False, cast=bool)
if SOCIALACCOUNT_ONLY:
    ACCOUNT_EMAIL_VERIFICATION = "none"

REGISTRATION = config("REGISTRATION", default=True, cast=bool)
if not REGISTRATION:
    ACCOUNT_ADAPTER = "users.account_adapter.NoNewUsersAccountAdapter"

REDIRECT_LOGIN_TO_SSO = config("REDIRECT_LOGIN_TO_SSO", default=False, cast=bool)

# Configure LoginRequiredMiddleware to exclude static files
LOGIN_REQUIRED_EXEMPT = [
    r"^/static/.*$",
    r"^/favicon\.ico$",
    r"^/health/.*$",
    r"^/list/\d+/rss/?$",  # Public list RSS feeds
    r"^/list/\d+/json/?$",  # Public list JSON exports
]
