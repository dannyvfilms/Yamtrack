# Project Overview
Yamtrack is a Django 5.2 app for self-hosted media tracking with Celery workers and Redis. Tailwind CSS output is currently committed under `src/static/css/`. Templates currently load `src/static/css/main.css` (see `src/templates/base.html`).

## Branch Policy (Fork)
- `dev` must be an exact mirror of upstream `FuzzyGrim/Yamtrack:dev` with no fork-only commits or edits.
- Any local `dev` divergence should be reset/fast-forwarded back to upstream before merging.

## Merge Workflow (Upstream Dev -> Release)
This workspace is the fork `dannyvfilms/Yamtrack`, branch `release`. When syncing upstream `dev` into `release`, treat the merge as a conflict-resolution task where upstream `dev` brings maintenance changes and `release` preserves fork features.

High-level rules:
- Use upstream `dev` as the source of truth for dependency versions, security/bugfix patches, small refactors, settings/config changes, CSS cleanups, and tests.
- Use `release` as the source of truth for fork-specific features and behavior changes.
- When logic overlaps, merge intent from both sides rather than choosing one side wholesale.

Important fork features to preserve while merging:
- Statistics/runtime/time-left features: statistics pages, runtime population/caching, episode runtimes, time_left sorting, dropped-show fixes, charts (daily buckets, 30-day played-hours, All Time).
- UI/layout improvements: mobile layouts, filter controls, media card/timeline styling, statistics layout refinements, responsive template updates.
- Preferences/sorting: preferences tab, sort-direction toggles/indicators, ratings-list sort fixes, aggregate-duplicates behavior.
- Plex/import/webhook resilience: Plex-only GUID handling, TMDB episode edge cases, SQLite lock handling during import, auto-pause for stale in-progress items.
- Deployment/infra tweaks: Docker build improvements, Redis-unavailability guard in AppConfig.ready(), README/install updates.

Conflict-resolution steps:
1. Scan for conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) and resolve every file.
2. For each conflict, understand both sides; keep upstream maintenance changes and layer them into fork features.
3. For views/templates/runtime/statistics/preferences/import/webhook logic, start from `release` behavior and integrate upstream improvements.
4. For lists/configs (.gitignore, CSS classes, INSTALLED_APPS, URLs, etc.), keep the union and deduplicate.
5. Remove all conflict markers; run linters/tests and fix underlying issues, not just tests.
6. If a choice is unavoidable, prioritize fork-visible features while honoring upstream data contracts/integrations.

## Repo Map
- `src/` Django project code (apps: `app`, `users`, `lists`, `integrations`, `events`; config in `config/`).
- `src/templates/` and `src/static/` for UI templates and CSS assets.
- `src/db/` local SQLite artifacts.
- `docs/agents/` issue and workflow notes.
- `.github/workflows/` CI definitions.
- `Dockerfile`, `docker-compose*.yml`, `entrypoint.sh`, `nginx.conf`, `supervisord.conf` for container runtime.

## Agent Docs
- `docs/agents/media_type_integration.md`: playbook for adding new media types safely.
- `docs/agents/music_integration.md`: music-specific data model and UI integration notes.
- `docs/agents/pocketcasts_workflow.md`: Pocket Casts import/schedule workflow details.
- `docs/agents/issue-22-history-crash.md`: history page OOM investigation and mitigation ideas.

## Blessed Workflows
- Primary (local dev): run Django from source with Redis, Celery worker/beat, and Tailwind watcher. Use this for code changes.
- Secondary (Docker): `docker-compose*.yml` currently uses the prebuilt `ghcr.io/dannyvfilms/yamtrack` image; use for deployment or quick smoke runs, not for local code changes.

## Quickstart (Local Dev)
Assumes the working directory is the repo root.

Install dev dependencies:

```bash
python -m pip install -U -r requirements-dev.txt
```

Start Redis:

```bash
docker run -d --name redis -p 6379:6379 --restart unless-stopped redis:8-alpine
```

Create `.env` in the repo root (from `README.md`):

```bash
TMDB_API=API_KEY
MAL_API=API_KEY
IGDB_ID=IGDB_ID
IGDB_SECRET=IGDB_SECRET
STEAM_API_KEY=STEAM_API_SECRET
SECRET=SECRET
DEBUG=True
```

Run migrations:

```bash
cd src && python manage.py migrate
```

First run (auth, if login is required):

```bash
cd src && python manage.py createsuperuser
```

Run services (separate terminals, from the repo root):

```bash
cd src && python manage.py runserver
```

```bash
cd src && celery -A config worker --beat --scheduler django --loglevel DEBUG
```

```bash
cd src && tailwindcss -i ./static/css/input.css -o ./static/css/main.css --watch
```

By default, the app runs at `http://localhost:8000`.

## Quickstart (Docker)
Docker (SQLite, prebuilt image):

```bash
docker-compose up -d
```

Docker (Postgres):

```bash
docker-compose -f docker-compose.postgres.yml up -d
```

By default, the app runs at `http://localhost:8000`.

Notes:
- Docker compose uses the prebuilt image `ghcr.io/dannyvfilms/yamtrack` (tags `:latest`, `:release`, `:dev`).
- `docker-compose.yml` stores SQLite data in `./db` and sets `SECRET`/`REDIS_URL` env vars.
- `docker-compose.postgres.yml` starts `postgres:16-alpine` and sets `DB_HOST/DB_NAME/DB_USER/DB_PASSWORD/DB_PORT`.
- Container entrypoint runs `python manage.py migrate --noinput` and supervisord runs nginx, gunicorn, celery worker, and celery beat.
- View container logs: `docker-compose logs -f yamtrack`.
- Reverse proxy deployments need `URLS` set (see `README.md`).

## Frontend Tooling
- Tailwind CLI install (supported): `brew install tailwindcss` (provides the `tailwindcss` binary used below). Currently, the repo does not document another install path.
- Alternatives: `npm/pnpm/yarn add -D tailwindcss` and run `npx tailwindcss ...`, or download the standalone Tailwind binary and add it to `PATH`.
- Tailwind input: `src/static/css/input.css`.
- Currently, templates load `static/css/main.css` via `src/templates/base.html` (this is the CSS the app uses).
- Regenerate and commit `src/static/css/main.css` when Tailwind changes:

```bash
cd src && tailwindcss -i ./static/css/input.css -o ./static/css/main.css --watch
```

- Note: `README.md` may reference output to `tailwind.css`; for local dev and committed CSS, the supported output path is `src/static/css/main.css`.
- `src/static/css/tailwind.css` exists but is not referenced by templates in this repo; treat it as legacy/unreferenced output unless usage is restored.
- Tailwind output headers currently show `tailwindcss v4.1.11` in `src/static/css/main.css` and `src/static/css/tailwind.css`.
- Stylelint config: `.stylelintrc`.
- Djlint config: `pyproject.toml`.

## Testing
Quick confidence (CI lint):

```bash
ruff check src
```

Full confidence (CI test flow):

```bash
playwright install
coverage run src/manage.py test app users integrations lists events --parallel
coverage combine
coverage report
coverage xml
```

Notes:
- `src/manage.py` sets `DJANGO_SETTINGS_MODULE=config.test_settings` for tests.
- `config.test_settings` uses fakeredis and sets `CELERY_TASK_ALWAYS_EAGER=True`.
- CI only injects secrets (`SECRET`, `TMDB_API`, `MAL_API`, `IGDB_ID`, `IGDB_SECRET`, `HARDCOVER_API`, `COMICVINE_API`) if they exist.
- `playwright install` is currently only needed for integration tests that import Playwright (`src/app/tests/test_integration.py`, `src/lists/tests/test_integration.py`).

## Style & Conventions
- Python target is 3.12 (see `Dockerfile` and CI).
- Ruff config lives in `pyproject.toml` and excludes `migrations/`.
- Djlint config is in `pyproject.toml`; Stylelint config is in `.stylelintrc`.
- After model changes, keep migration files under `src/*/migrations/` and run `cd src && python manage.py migrate`.
- Media type changes follow `docs/agents/media_type_integration.md` (MediaTypes enum + `media_type_config` wiring).

## PR / Commit Expectations
- CI fails PRs that modify `.github/workflows/**` (see `.github/workflows/app-tests.yml`).

### Commit Message Format
Short imperative title, then 1–3 bullet clarifications in the body. Optional issue lines: `Fixes #123` / `Refs #456`.

## Security / Safety Notes
- `.env` contains secrets and API keys; do not commit it.
- Docker entrypoint runs migrations and changes ownership inside the container (`entrypoint.sh`).
- Docker compose stores data in `./db`; local dev SQLite lives under `src/db/`.

### Unknowns / Needs confirmation
- Is there a preferred formatting command (e.g., `ruff format`, `djlint`, `stylelint`) beyond `ruff check`?
- Is there a supported seed-data command beyond `python manage.py createsuperuser`?
- Is pytest a supported/required runner (pytest config exists), or is `manage.py test` the only supported flow?
- Which environment variables are required to run the full test suite locally (if any beyond defaults)?
