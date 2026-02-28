# Migration Sync Playbook (`dev` -> `latest`)

This playbook defines the required migration process for syncing upstream `dev` into fork `latest`.

## Branch model
- `dev`: exact mirror of `upstream/dev`.
- `latest`: integration branch for fork features and upstream sync merges.
- `release`: versioned release/container publication flow.

## Migration policy
- Same migration numbers across branches are valid in Django.
- Resolve graph splits with merge migrations (`makemigrations --merge`), not wholesale rewrites.
- Keep upstream migration filenames unchanged in `latest`.
- Renumber only fork-local migrations that are unpushed and unreleased.
- Never rewrite migrations that already exist in `origin/latest` or any `v*` release tag.
- In fork-authored migrations, use idempotent wrappers for risky schema add/remove operations.

## Sync SOP (hard gate)
1. Update and verify mirror branch:
   - `git checkout dev`
   - `git fetch upstream`
   - `git reset --hard upstream/dev`
2. Merge upstream mirror into integration branch:
   - `git checkout latest`
   - `git merge --no-ff dev`
3. Resolve conflicts:
   - Keep upstream maintenance changes.
   - Keep fork-visible behavior and UX.
   - For migration conflicts, follow policy above.
4. Resolve migration graph:
   - `cd src && python manage.py makemigrations --merge`
   - Repeat until affected apps have one leaf node.
5. Run migration hygiene command:
   - `cd src && python manage.py check_migration_hygiene --strict`
6. Run dual-DB upgrade replay:
   - `scripts/replay_upgrade_matrix.sh --from-tag <previous_release_tag> --to-ref latest --db sqlite,postgres --with-drift-scenarios`
7. Run standard tests:
   - `coverage run src/manage.py test app users integrations lists events --parallel`
8. Do not merge sync work until all gates pass.

## Required drift scenario coverage
- Drift scenarios are executed in Postgres replay.
- Baseline scenario for issue class `#101`:
  1. Migrate to `users.0067_remove_user_tv_sort_valid_and_more`.
  2. Drop `boardgame_sort_valid` manually.
  3. Apply `users.0068_remove_user_tv_sort_valid_and_more`.
  4. Confirm migration succeeds.

## Troubleshooting guidance
- If `check_migration_hygiene` reports multiple leaf nodes:
  - Run `makemigrations --merge` for the app it reports.
- If risky raw operations are flagged:
  - Replace raw `migrations.AddConstraint/RemoveConstraint` (and index equivalents) with idempotent wrappers in fork-authored migrations.
- If replay passes on SQLite but fails on Postgres:
  - Treat Postgres failure as blocking and patch migration idempotency before merge.
