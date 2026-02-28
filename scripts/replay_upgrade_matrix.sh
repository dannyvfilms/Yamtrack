#!/usr/bin/env bash
set -euo pipefail

FROM_TAG=""
TO_REF="latest"
DB_TARGETS="sqlite,postgres"
WITH_DRIFT_SCENARIOS=1

REPLAY_CONTAINER=""
REPLAY_PORT=""
WORKTREE_DIR=""
REPO_ROOT=""

usage() {
  cat <<'EOF'
Replay Yamtrack upgrade migrations across SQLite/Postgres.

Usage:
  scripts/replay_upgrade_matrix.sh --from-tag <tag> [options]

Options:
  --from-tag <tag>           Required starting release tag (for example: v26.2.19)
  --to-ref <ref>             Target ref to upgrade into (default: latest)
  --db <targets>             Comma-separated list: sqlite,postgres (default: sqlite,postgres)
  --with-drift-scenarios     Enable drift scenarios (default: enabled)
  --without-drift-scenarios  Disable drift scenarios
  -h, --help                 Show this help
EOF
}

log() {
  printf '[replay] %s\n' "$*"
}

die() {
  printf '[replay] ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [[ -n "$REPLAY_CONTAINER" ]]; then
    docker rm -f "$REPLAY_CONTAINER" >/dev/null 2>&1 || true
  fi

  if [[ -n "$WORKTREE_DIR" && -n "$REPO_ROOT" ]]; then
    git -C "$REPO_ROOT" worktree remove --force "$WORKTREE_DIR" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-tag)
      FROM_TAG="${2:-}"
      shift 2
      ;;
    --to-ref)
      TO_REF="${2:-}"
      shift 2
      ;;
    --db)
      DB_TARGETS="${2:-}"
      shift 2
      ;;
    --with-drift-scenarios)
      WITH_DRIFT_SCENARIOS=1
      shift
      ;;
    --without-drift-scenarios)
      WITH_DRIFT_SCENARIOS=0
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

if [[ -z "$FROM_TAG" ]]; then
  usage
  die "--from-tag is required."
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" ]]; then
  die "Run this script inside the repository."
fi

git -C "$REPO_ROOT" rev-parse --verify --quiet "${FROM_TAG}^{commit}" >/dev/null \
  || die "Unknown from-tag ref: $FROM_TAG"
git -C "$REPO_ROOT" rev-parse --verify --quiet "${TO_REF}^{commit}" >/dev/null \
  || die "Unknown to-ref: $TO_REF"

RUN_SQLITE=0
RUN_POSTGRES=0
IFS=',' read -r -a DB_ITEMS <<< "$DB_TARGETS"
for item in "${DB_ITEMS[@]}"; do
  normalized="$(printf '%s' "$item" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  case "$normalized" in
    sqlite)
      RUN_SQLITE=1
      ;;
    postgres)
      RUN_POSTGRES=1
      ;;
    "")
      ;;
    *)
      die "Unsupported DB target: $normalized (valid: sqlite,postgres)"
      ;;
  esac
done

if [[ "$RUN_SQLITE" -eq 0 && "$RUN_POSTGRES" -eq 0 ]]; then
  die "No valid --db targets provided."
fi

WORKTREE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/yamtrack-replay-XXXXXX")"
git -C "$REPO_ROOT" worktree add --detach "$WORKTREE_DIR" "$FROM_TAG" >/dev/null

checkout_ref() {
  local ref="$1"
  git -C "$WORKTREE_DIR" checkout --detach --quiet "$ref"
}

run_manage_sqlite() {
  (cd "$WORKTREE_DIR/src" && SECRET=dev DEBUG=True python manage.py "$@")
}

start_postgres() {
  command -v docker >/dev/null 2>&1 || die "docker is required for postgres replay."

  REPLAY_CONTAINER="yamtrack-upgrade-replay-$$"
  docker run -d --name "$REPLAY_CONTAINER" \
    -e POSTGRES_USER=postgres \
    -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_DB=postgres \
    -p 0:5432 \
    postgres:16-alpine >/dev/null

  REPLAY_PORT="$(
    docker port "$REPLAY_CONTAINER" 5432/tcp \
      | head -n 1 \
      | sed -E 's/.*:([0-9]+)$/\1/'
  )"
  [[ -n "$REPLAY_PORT" ]] || die "Failed to determine postgres host port."

  for _ in $(seq 1 30); do
    if docker exec "$REPLAY_CONTAINER" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done

  die "Postgres container did not become ready in time."
}

create_pg_db() {
  local db_name="$1"
  docker exec "$REPLAY_CONTAINER" psql -U postgres -d postgres \
    -c "DROP DATABASE IF EXISTS ${db_name};" >/dev/null
  docker exec "$REPLAY_CONTAINER" psql -U postgres -d postgres \
    -c "CREATE DATABASE ${db_name};" >/dev/null
}

run_manage_postgres() {
  local db_name="$1"
  shift
  (
    cd "$WORKTREE_DIR/src" && \
      SECRET=dev \
      DEBUG=True \
      DB_HOST=127.0.0.1 \
      DB_NAME="$db_name" \
      DB_USER=postgres \
      DB_PASSWORD=postgres \
      DB_PORT="$REPLAY_PORT" \
      python manage.py "$@"
  )
}

run_sqlite_replay() {
  log "SQLite replay: ${FROM_TAG} -> ${TO_REF}"

  checkout_ref "$FROM_TAG"
  rm -f "$WORKTREE_DIR/src/db/db.sqlite3"
  run_manage_sqlite migrate --noinput

  checkout_ref "$TO_REF"
  run_manage_sqlite migrate --noinput
}

run_postgres_replay() {
  local replay_db="yamtrack_replay_${$}_pg"
  log "Postgres replay: ${FROM_TAG} -> ${TO_REF}"

  checkout_ref "$FROM_TAG"
  create_pg_db "$replay_db"
  run_manage_postgres "$replay_db" migrate --noinput

  checkout_ref "$TO_REF"
  run_manage_postgres "$replay_db" migrate --noinput
}

run_postgres_drift_scenarios() {
  if [[ "$WITH_DRIFT_SCENARIOS" -ne 1 ]]; then
    log "Skipping drift scenarios (--without-drift-scenarios)."
    return
  fi

  checkout_ref "$TO_REF"
  if [[ ! -f "$WORKTREE_DIR/src/users/migrations/0067_remove_user_tv_sort_valid_and_more.py" ]]; then
    log "Skipping drift scenario: users.0067 migration not present in ${TO_REF}."
    return
  fi
  if [[ ! -f "$WORKTREE_DIR/src/users/migrations/0068_remove_user_tv_sort_valid_and_more.py" ]]; then
    log "Skipping drift scenario: users.0068 migration not present in ${TO_REF}."
    return
  fi

  local drift_db="yamtrack_drift_${$}_pg"
  log "Postgres drift scenario (#101 class): drop boardgame_sort_valid between users.0067 and users.0068"

  create_pg_db "$drift_db"
  run_manage_postgres "$drift_db" migrate users 0067_remove_user_tv_sort_valid_and_more --noinput
  docker exec "$REPLAY_CONTAINER" psql -U postgres -d "$drift_db" \
    -c "ALTER TABLE users_user DROP CONSTRAINT IF EXISTS boardgame_sort_valid;" >/dev/null
  run_manage_postgres "$drift_db" migrate users 0068_remove_user_tv_sort_valid_and_more --noinput
  run_manage_postgres "$drift_db" migrate --noinput
}

log "Starting upgrade replay matrix (from=${FROM_TAG}, to=${TO_REF}, db=${DB_TARGETS}, drift=${WITH_DRIFT_SCENARIOS})"

if [[ "$RUN_SQLITE" -eq 1 ]]; then
  run_sqlite_replay
fi

if [[ "$RUN_POSTGRES" -eq 1 ]]; then
  start_postgres
  run_postgres_replay
  run_postgres_drift_scenarios
fi

log "Upgrade replay matrix passed."
