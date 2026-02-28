## Summary
- Describe what changed and why.

## Validation
- List commands run and outcomes.

## Migration Sync Gate (Required for `dev` -> `latest` sync PRs)
- [ ] Conflicts resolved with upstream files preserved and fork behavior merged intentionally.
- [ ] Migration conflicts handled per policy (no rewrite of shared/released migrations).
- [ ] `cd src && python manage.py makemigrations --merge` run for affected apps.
- [ ] `cd src && python manage.py check_migration_hygiene --strict` passed.
- [ ] `scripts/replay_upgrade_matrix.sh --from-tag <previous_release_tag> --to-ref latest --db sqlite,postgres --with-drift-scenarios` passed.
- [ ] `coverage run src/manage.py test app users integrations lists events --parallel` passed.

## Notes
- Link relevant issues (for example: `Refs #101`).
