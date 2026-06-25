# Contributing

This is a personal fork of [FuzzyGrim/Yamtrack](https://github.com/FuzzyGrim/Yamtrack). Contributions are welcome but the bar is practical: changes should be clean, minimal, and safe to land.

---

## PR Size and Scope

**Default to small.** A PR that does one thing clearly is easier to review and more likely to land than a large PR that does several things.

Large line counts are not automatically a problem. A large PR that adds a complete new integration in one focused area, with clean commits and screenshots, is fine. A large PR that adds a UI feature, fixes pre-existing lint violations, restructures unrelated templates, and introduces UI patterns that don't exist elsewhere in the app is not — even if each individual change is reasonable.

The question to ask before opening: **Is every line in this PR directly load-bearing for the stated goal?**

If the answer is no, split it. Common offsets worth their own PR:

- Lint/ruff/style cleanup — easy to review in isolation, gets closed when bundled with behavior changes
- Refactors that aren't required by the feature
- Test additions for existing behavior
- Unrelated template or CSS tidying spotted along the way

**For agents:** Before opening a PR, check the diff stat. If the line count or commit count feels high relative to the stated goal, stop and ask the user whether to proceed, split the work, or descope. A PR that surprises the reviewer is a PR that gets closed.

---

## Commits

**One logical commit per reviewable unit of work** — not one commit per agentic turn or tool call.

**For agents:** Scan commit titles before opening a PR. Capitalization and minor phrasing are not worth raising. Stop and ask the user if any message would make a reviewer wince — `wip`, `fix`, `asdf`, `update stuff`, `agent turn 4`, a bare timestamp, or anything that gives no indication of what changed.

**Commit message quality:** Use a short imperative title that describes what the commit does. Capitalization, missing periods, and minor phrasing preferences are not worth flagging. What is worth flagging — and an agent must stop and raise with the user before opening the PR — are messages that would make a reviewer genuinely wince: `wip`, `fix`, `asdf`, `changes`, `update stuff`, `agent turn 4`, timestamps as the title, or anything that gives no indication of what changed. If the history has messages like that, ask the user whether to clean them up before the PR opens.

---

## Branch Policy

**All PRs must target `latest`, not `dev`.**

- `dev` is a strict mirror of upstream. It is never edited here.
- `latest` is the fork integration branch. This is where all feature work, fixes, and upstream syncs land.
- `release` is for container publication and is not a development target.

Opening a PR against `dev` will be closed and redirected.

---

## Pull Request Requirements

Every PR must include these in the description:

**1. Problem** — What is broken, missing, or suboptimal? Link the issue if one exists (`Fixes #123` / `Refs #456`).

**2. Solution** — What did you change and why? One or two sentences is fine; complex changes may need more.

**3. Validation** — What did you run? List commands and outcomes. See the validation section below for guidance on what's required.

**4. Screenshots** — Required for any change that touches templates, CSS, layout, or UI behavior. Before and after if the change is a visual fix. A single "after" shot is fine for new UI. Skip only for backend-only or migration-only changes.

---

## AI-Assisted Contributions

If an AI agent (Claude Code, Cursor, GitHub Copilot, Codex, etc.) generated or substantially shaped the code you're submitting, say so in the PR description under an **AI Assistance** section:

```
## AI Assistance
Generated with Claude Code (claude-sonnet-4-6). Reviewed and tested manually.
```

This is not a disqualifier. It helps with review triage and lets the maintainer calibrate how closely to inspect the change. Undisclosed AI-generated PRs that contain subtle bugs are harder to catch and harder to attribute; disclosed ones get the right level of scrutiny.

If an agent submitted the PR directly (no human author), the agent must:

- Target `latest` (not `dev`)
- Include the problem, solution, validation, and screenshots sections above
- Include the AI Assistance section with the model/tool name
- Confirm screenshots were captured for any UI change
- Check the diff stat before opening: if the scope feels large relative to the stated goal, pause and confirm with the user before submitting
- Confirm commits are organized by logical unit, not by agentic turn
- Confirm no pre-existing lint in unrelated code was fixed as a side effect of the PR (fixing lint in lines you already touched is fine)

---

## Validation

Match validation to risk. See `AGENTS.md` for the full matrix. Short version:

| Change type | Minimum check |
|---|---|
| Copy, labels, static content | None required |
| CSS / Tailwind spacing | Visual screenshot |
| Template or UI logic | Screenshot + `ruff check src` |
| Python behavior | `ruff check src` + targeted test |
| Model or migration change | `python manage.py check_migration_hygiene --strict` + full test suite |
| `dev` → `latest` upstream sync | Full migration sync gate (see PR template) |

Never skip validation for migrations, models, auth, permissions, webhooks, Celery tasks, or cache behavior.

---

## Style

- Python 3.12. Ruff configured in `pyproject.toml` (88-char line limit, migrations excluded).
- Templates: djlint config in `pyproject.toml`. CSS: Stylelint config in `.stylelintrc`.
- Tailwind output is committed at `src/static/css/main.css`. Run `tailwindcss -i ./static/css/input.css -o ./static/css/main.css` after any template or class changes.
- Commit messages: short imperative title, optional 1–3 bullet body, then issue lines.
- Do not mix behavior changes, refactors, and tests in a single PR unless the scope clearly demands it.

**UI consistency:** New UI must match existing patterns in the app — spacing, card styles, chip styles, color tokens, layout conventions. If your change introduces a visual pattern that doesn't appear anywhere else in the app, flag it explicitly in the PR description and justify it. Unexplained novel UI is a reason to close a PR.

**Lint cleanup:** If you spot pre-existing ruff/lint violations while working, do not fix them in the same PR. Either open a separate lint-only PR (welcome and easy to review) or leave a note. Fixing unrelated lint mid-feature PR inflates the diff and obscures the actual change.

---

## What Gets Closed Without Review

- PRs targeting `dev`
- PRs with no description
- PRs with no problem/solution statement
- UI changes with no screenshots
- PRs that introduce UI patterns with no precedent elsewhere in the app and no explanation
- PRs where lint/style cleanup is bundled with behavior changes
- PRs where commit count suggests the history was never organized (17 commits for a single UI feature is a signal)
