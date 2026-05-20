# Coding Agent Instructions

## Operating Principles

Keep it simple. Simple is better than complex.
Make the smallest maintainable change that solves the actual request.
Prefer existing patterns over new abstractions.
Avoid broad refactors, speculative helpers, and clever architecture unless clearly justified.
Do not build for hypothetical future use. Implement the current need cleanly and stop there.
Use judgment. Read enough surrounding code to understand the existing pattern, then avoid unnecessary exploration. Validate based on risk.
Assume the user is a principal engineer.
Optimize for correctness, speed, judgment, and token efficiency.
Correct the user when appropriate.

## Success Criteria

Done means:

- the requested behavior is implemented
- the change is minimal and follows existing patterns (Unless a large task was assigned)
- risky behavior was validated, or validation was intentionally skipped with a reason
- remaining risks are stated plainly

## Context Discipline

Protect context aggressively.

As tool output, file reads, and conversation history grow, useful signal gets diluted. Keep active context focused on the current decision.

Before opening files or running broad searches, ask:

1. What exact question am I answering?
2. Which file, symbol, route, or component is most likely relevant?
3. Can I inspect a narrower slice first?
4. Can `rg`, imports, references, or file names locate the answer?

Prefer targeted searches, focused file sections, nearby call sites, diffs, capped logs, and targeted test output.

Avoid dumping full files, full logs, unrelated directories, or broad repo exploration after the relevant code is found.

When context gets large, summarize the current task state and keep only:

- decisions
- relevant file paths
- changed behavior
- unresolved risks

## Subagents

Use subagents only when they save context, save time, or materially improve output quality.

For research, review, and exploration tasks, avoid confirmation bias. Do not pass a preferred conclusion. Ask the subagent to investigate, compare, or verify, and require evidence, tradeoffs, uncertainty, and better alternatives.

Good uses:

- repo exploration
- scoped implementation
- QA or review
- documentation/API checks
- web research
- unfamiliar code research
- copywriting/content variants

Avoid subagents for trivial work the main agent can finish faster.

When using a subagent, assign a narrow task and require:

- findings
- files inspected
- files changed, if any
- validation run, if any
- risks or uncertainty

The main agent owns final judgment and integration.

## Code Changes

Prefer direct edits using available environment tools like `apply_patch`

Before adding helpers, maps, files, abstractions, or validation layers, ask:

1. Can this be done inline?
2. Can existing code already do this?
3. Is this solving the exact issue?
4. Is reuse or readability clearly improved?

Do not create new abstractions, helper layers, provider interfaces, background tasks, docs, or config files unless the current task clearly needs them.
Before adding a new function, class, setting, management command, or integration hook, check whether an existing pattern already solves the same problem.

For bugs, patch the narrow failing path first.
For small behavior changes, make the direct edit first.
Avoid unrelated cleanup.

Split work into reviewable patches when possible:

- behavior change
- mechanical refactor
- tests
- docs

Do not mix these unless the user explicitly asks for a broad rewrite.

For complex tasks:

- identify the minimal path through the codebase
- split work into small patches
- validate only the risky parts
- keep a short running summary of decisions, changed files, and remaining risks

## Validation

Match validation to risk.

Skip validation by default for low-risk changes and say so plainly.

Never skip validation when touching:

- migrations
- models
- importers
- webhooks
- auth
- permissions
- settings
- Celery tasks or background jobs
- cache behavior
- persisted data
- external APIs
- upgrade paths
- any PR that spans more than one app boundary unless explicitly told not to validate

Low-risk examples:

- copy changes
- labels
- static content
- CSS or Tailwind spacing
- small JSX structure changes
- minor refactors with no behavior change

Also validate when:

- a previous command failed
- the user asked for validation
- the change affects multiple routes, components, or packages

Prefer targeted tests first, then `ruff check src`, then broader test runs only when risk justifies it.

Prefer the cheapest useful check:

1. targeted test
2. type check affected package
3. lint affected files
4. build only when build behavior matters

Do not run a full test suite or full build unless risk justifies it or the user asks.

## Command Output

Protect context usage. **Any command with unknown or potentially large output must be byte-capped.**

Default pattern:

```bash
COMMAND 2>&1 | head -c 4000
```

For logs or recent failures:

```bash
COMMAND 2>&1 | tail -c 4000
```

Do not rely on line limits as the only cap. A single line can be huge. Avoid using only:

```bash
head -n
tail -n
sed -n '1,20p'
```

Scope before printing content:

- list files with `rg -l` before printing matches
- count matches with `rg -c` before reading them
- search specific paths instead of whole directories
- use `rg -m`, `--max-count`, `--max-filesize`, and small context when useful
- inspect file size before reading unknown generated files, logs, JSONL, or minified JSON

For commands where the exit code matters, capture output first, print a capped amount, then exit with the original status:

```bash
tmp="$(mktemp)"
COMMAND >"$tmp" 2>&1
status=$?
tail -c 5000 "$tmp"
rm -f "$tmp"
exit "$status"
```

Avoid unbounded output from:

```bash
cat path/to/file
rg -n "term" .
find .
ls -R
git diff
npm test
npm run build
select *
```

Use bounded versions instead:

```bash
rg -l "term" . | head -c 2000
rg -n -m 20 "term" src 2>&1 | head -c 2000
git diff -- path/to/file 2>&1 | head -c 6000
find . -type f 2>&1 | head -c 2000
```

If the capped output is insufficient, narrow the command. Do not repeatedly increase the cap unless the task requires more context.

## Communication

Before editing, state the approach only for non-trivial tasks.

During complex work, keep updates very short:

- what was found
- what changed
- what risk remains

After work, summarize:

- what changed
- files touched
- validation run, or why skipped
- remaining risk

Keep summaries short. Do not explain obvious edits.

Oververbosity:low

# Yamtrack Notes
Yamtrack is a Django 5.2 app for self-hosted media tracking with Celery workers and Redis. Tailwind CSS output is committed under `src/static/css/`, and templates load `src/static/css/main.css` via `src/templates/base.html`.

## Branch Policy
- `dev` must be an exact mirror of upstream `FuzzyGrim/Yamtrack:dev` with no fork-only commits or edits. You will never update this branch.
- Any local `dev` divergence should be reset/fast-forwarded back to upstream before merging. `dev` will always be a perfect mirror of upstream.
- `latest` is the fork integration branch for day-to-day feature work and upstream sync merges.
- `release` is for versioned release/container publication flow, not the primary integration branch.

## Merge Workflow
This workspace is the fork `dannyvfilms/Yamtrack`, branch `latest`. When syncing upstream `dev` into `latest`, treat the merge as a conflict-resolution task where upstream `dev` brings maintenance changes and `latest` preserves fork features.

High-level rules:
- Use upstream `dev` as the source of truth for dependency versions, security/bugfix patches, small refactors, settings/config changes, CSS cleanups, and tests.
- Use `latest` as the source of truth for fork-specific features and behavior changes.
- When logic overlaps, merge intent from both sides rather than choosing one side wholesale.

Current app areas to preserve while merging:
- Progress, history, and statistics: time-left sorting, time-watched views, history filters, cached range stats, Top Played, media-hours cards, reading/music/podcast stats, comparison tooling, and dropped-show fixes.
- Lists and sharing: public/private lists, smart lists, public profiles, recommendations, list tags, drag-and-drop ordering, release-date sorting, completion indicators, RSS/JSON feeds, and backup export/import.
- Media coverage and collection: music, podcasts, books, comics, manga, games, board games, collection/owned-media flows, person/author pages, localized titles, runtime chips, and grouped-anime handling.
- Integrations, imports, and webhooks: Trakt, Plex, Jellyfin, Jellyseerr, Pocket Casts, Last.fm, Audiobookshelf, TVDB, Steam, Plex-only GUID handling, TMDB episode edge cases, SQLite lock handling during import, and auto-pause for stale in-progress items.
- UI, mobile, and performance: mobile layouts, compact grids, filter controls, media card/timeline styling, statistics layout refinements, quick season updates, runtime/history caching, startup guards, and iOS/SQLite resiliency.
- Preferences and settings: sort-direction toggles/indicators, ratings-list sort fixes, aggregate-duplicates behavior, subtitle/date-time/rating-scale preferences, and visibility/sidebar/search settings.
- Deployment and install: Docker build improvements, Redis-unavailability guard in AppConfig.ready(), and README/install updates.

Conflict-resolution steps:
1. Scan for conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`) and resolve every file.
2. For each conflict, understand both sides; keep upstream maintenance changes and layer them into fork features.
3. For views/templates/runtime/history/statistics/lists/preferences/import/webhook logic, start from `latest` behavior and integrate upstream improvements.
4. For migration conflicts:
   - Same migration numbers on different branches are valid in Django; resolve with merge migrations.
   - Keep upstream migration filenames unchanged in `latest`.
   - Renumber only fork-local migrations that are unpushed/unreleased.
   - Never rename or rewrite any migration file that exists in `origin/latest` or any `v*` release tag.
5. For lists/configs (.gitignore, CSS classes, INSTALLED_APPS, URLs, etc.), keep the union and deduplicate.
6. Remove all conflict markers; run linters/tests and fix underlying issues, not just tests.
7. For upstream syncs, run the hard gates that apply to the change:
   - `cd src && python manage.py makemigrations --merge`
   - `cd src && python manage.py check_migration_hygiene --strict`
   - `scripts/replay_upgrade_matrix.sh --from-tag <previous_release_tag> --to-ref latest --db sqlite,postgres --with-drift-scenarios`
   - `coverage run src/manage.py test app users integrations lists events --parallel`
8. If a choice is unavoidable, prioritize fork-visible features while honoring upstream data contracts/integrations.

## Repository Map
- `src/` Django project code (apps: `app`, `users`, `lists`, `integrations`, `events`; config in `config/`).
- `src/templates/` and `src/static/` for UI templates and CSS assets.
- `src/static/css/main.css` is the committed Tailwind output loaded by templates.
- `src/db/` local SQLite artifacts.
- `docs/agents/` issue and workflow notes.
- `.github/workflows/` CI definitions.
- `Dockerfile`, `docker-compose*.yml`, `entrypoint.sh`, `nginx.conf`, `supervisord.conf` for container runtime.
- `wiki/` is a separate Git repository for the project wiki (edit and commit there, not in this repo).

## Workflow Notes
- Keep wiki pages in `wiki/` so they can be edited locally and pushed to the wiki repo.
- Treat `wiki/` as its own git repo (not a submodule); run commits/pushes from `wiki/`.
- Do not add `wiki/` to the main repo index; it should remain untracked here.
- Primary local development is source-run Django with Redis, Celery worker/beat, and Tailwind watcher.
- Secondary Docker usage is for deployment or quick smoke runs; the compose files use the prebuilt `ghcr.io/dannyvfilms/yamtrack` image.

## Agent Docs
- `docs/agents/media_type_integration.md`: playbook for adding new media types safely.
- `docs/agents/music_integration.md`: music-specific data model and UI integration notes.
- `docs/agents/pocketcasts_workflow.md`: Pocket Casts import/schedule workflow details.
- `docs/agents/migration_sync_playbook.md`: required hard-gate flow for upstream syncs and migration drift replay.

## Local Commands
- Install dev dependencies: `python -m pip install -U -r requirements-dev.txt`
- Run migrations: `cd src && python manage.py migrate`
- Run the app: `cd src && python manage.py runserver`
- Run Celery: `cd src && celery -A config worker --beat --scheduler django --loglevel DEBUG`
- Run Tailwind: `cd src && tailwindcss -i ./static/css/input.css -o ./static/css/main.css --watch`
- For local setup, see `README.md` for the required `.env` values and Redis startup details.

## Frontend Tooling
- Tailwind CLI install (supported): `brew install tailwindcss`.
- Alternatives: `npm/pnpm/yarn add -D tailwindcss` and run `npx tailwindcss ...`, or download the standalone Tailwind binary and add it to `PATH`.
- Note: `README.md` may reference output to `tailwind.css`; the supported committed output path is `src/static/css/main.css`.
- If a local watcher, shell alias, or editor task still writes `src/static/css/tailwind.css`, repoint it to `src/static/css/main.css`.

## Testing
- Quick confidence: `ruff check src`
- Migration sync confidence: `cd src && python manage.py check_migration_hygiene --strict`
- Upstream sync replay: `scripts/replay_upgrade_matrix.sh --from-tag <previous_release_tag> --to-ref latest --db sqlite,postgres --with-drift-scenarios`
- When the change justifies it: `coverage run src/manage.py test app users integrations lists events --parallel`
- `playwright install` is only needed for integration tests that import Playwright (`src/app/tests/test_integration.py`, `src/lists/tests/test_integration.py`).
- `src/manage.py` sets `DJANGO_SETTINGS_MODULE=config.test_settings` for tests.
- `config.test_settings` uses fakeredis and sets `CELERY_TASK_ALWAYS_EAGER=True`.

## Style & Conventions
- Python target is 3.12 (see `Dockerfile` and CI).
- Ruff config lives in `pyproject.toml` and excludes `migrations/`.
- Djlint config is in `pyproject.toml`; Stylelint config is in `.stylelintrc`.
- After model changes, keep migration files under `src/*/migrations/` and run `cd src && python manage.py migrate`.
- Media type changes follow `docs/agents/media_type_integration.md` (MediaTypes enum + `media_type_config` wiring).

## PR / Commit Expectations
- **Never commit unless the user explicitly asks.** Finishing a task, passing tests, or reaching a natural stopping point does not justify an automatic commit. Wait for a direct instruction such as "commit this", "commit the changes", or "make a commit".
- **Never amend a commit the user has not seen.** If a hook fails after a commit attempt, fix the issue and create a new commit — do not amend.
- CI fails PRs that modify `.github/workflows/**` (see `.github/workflows/app-tests.yml`).
- Large changes should be split into reviewable PRs or clearly justified if they cannot be.
- Review summaries should call out behavior changes, files touched, validation run, and remaining risk.
- Commit messages should use a short imperative title, then 1–3 bullet clarifications in the body. Optional issue lines: `Fixes #123` / `Refs #456`.

## Security / Safety Notes
- `.env` contains secrets and API keys; do not commit it.
- Docker entrypoint runs migrations and changes ownership inside the container (`entrypoint.sh`).
- Docker compose stores data in `./db`; local dev SQLite lives under `src/db/`.
