# Pocket Casts API Discovery — Design

**Date:** 2026-04-20
**Status:** Approved (pending user review of this spec)

## Problem

The dannyvfilms/Yamtrack fork has a built-in Pocket Casts integration, but its bootstrap import is broken. It drives the import off `POST /user/history`, which only returns the user's currently in-progress episode(s) — not full historical played-state. Result for this user: 1 of 41 podcasts gets touched on first import, and that 1 podcast has 1 of 50 episodes flagged (instead of the 49 actually played).

The unofficial Pocket Casts API has no public docs. The web SPA at `pocketcasts.com/podcasts` clearly *does* know per-episode played-state (it renders it), so the endpoint(s) the integration is missing must exist somewhere.

## Goal

Discover which Pocket Casts API call(s) return the user's full listening history with played/completed state per episode, and produce a report the Pi-side AI can use to patch the YamTrack fork's `pocketcasts_import` task. After the patch, the fork's existing 2-hour sync handles ongoing updates on its own.

## Non-goals

- Generating a YamTrack-importable CSV/JSON. The fork doesn't import podcasts via CSV; podcasts come from the Pocket Casts source provider. The patch path is fixing the integration, not bypassing it.
- Cross-checking against the local `PocketCasts.opml`. `POST /user/podcast/list` returns the same subscription data; OPML adds nothing.
- Running on the Raspberry Pi. Discovery runs on Windows where Playwright is already installed; only the eventual code patch ships to the Pi.

## Architecture

Single Node script, `pocketcasts-discovery/pocketcasts_discovery.mjs`. Self-contained subfolder so it can be moved to its own repo later when the fork PR is opened. Imports `playwright` from the parent folder's `node_modules` (Node module resolution walks upward) so we don't duplicate the install.

Two complementary discovery axes in one run:

1. **HAR capture (ground truth).** Playwright launches Chromium with `recordHar` enabled, logs in via the real web UI, and navigates the SPA to provoke API calls. Whatever endpoint the SPA hits to render played-state is the answer.
2. **REST probes (hypothesis tests).** The script extracts the bearer token from the SPA's `localStorage`, then fires direct `fetch` calls at the candidate endpoints from the Pi-side AI's analysis. Cheap, fast, tests pagination/param hypotheses.

Both feed a single `report.md` summary plus raw `probes.json` and `network.har` for follow-up inspection.

## Layout

```
pocketcasts-discovery/
  pocketcasts_discovery.mjs       # the script
  pocketcasts_credentials.json    # gitignored; user creates
  .gitignore                      # ignores credentials + output/
  README.md                       # how to run, what artifacts mean
  output/                         # all run artifacts; safe to delete
    network.har
    probes.json
    report.md
  docs/
    2026-04-20-pocketcasts-discovery-design.md
```

## Script flow

1. Load credentials from `pocketcasts_credentials.json` (`{email, password}`). Fail loudly if missing.
2. Launch Chromium (headed by default for first runs, `HEADLESS=1` env var to override) with `recordHar: { path: 'output/network.har' }`.
3. Navigate to `https://pocketcasts.com/user/login`, fill the form, submit, wait for redirect to `/podcasts`.
4. Capture the bearer token from `localStorage` (key TBD-by-inspection at runtime; script logs the keys it found if extraction fails).
5. SPA navigation to provoke API calls:
   - Stay on `/podcasts` long enough for the initial subscription render.
   - Click into "What the Shell?" (the show the Pi-side AI confirmed has playback history).
   - Look for and visit any links/tabs labeled History, Stats, Profile, Filters, Listening History, etc. Script discovers these by scanning visible nav elements rather than hardcoding URLs we may not know.
   - Optional: try sort/filter controls on the podcasts list (e.g., "recently played") since those often trigger fresh API calls.
6. REST probes using the captured token, each wrapped in a 60s timeout and try/catch so one failure doesn't kill the run:
   - `POST /user/history` with payloads: `{}`, `{count: 1000}`, `{limit: 100, offset: 0}`, `{page: 2}`, `{count: 1000, page: 1}`
   - `POST /user/stats`
   - `POST /user/episodes`
   - `POST /user/listening_history`
   - `POST /user/podcast/list` (already known to work; included as a baseline + to source UUIDs)
   - `POST /user/podcast/episodes` with one show UUID pulled from `/user/podcast/list` (default to "What the Shell?" if matchable, else first show)
   - Any `api.pocketcasts.com/user/*` endpoint observed in the HAR that isn't already in this list
7. Write artifacts (see Output section).
8. Close the browser.

## Auth + secrets handling

- `pocketcasts_credentials.json` is the only place credentials live. `.gitignore` covers it.
- Bearer token is redacted from `report.md` (`Bearer ***`).
- HAR `Authorization` headers are scrubbed before write — Playwright's HAR includes them by default, which would leak the token to anyone reading the artifact.
- Output directory is also gitignored as a precaution; the user's listening data is in there.

## Output artifacts

**`output/network.har`** — full Chromium network log from the Playwright session, devtools-importable. Auth headers scrubbed. The Pi-side AI (or a human) can replay this to see exactly what the SPA does.

**`output/probes.json`** — raw responses keyed by `endpoint + payload`. Each entry: `{endpoint, payload, status, durationMs, responseBody, episodeCount}` where `episodeCount` is best-effort (counts items in arrays named `episodes`, `history`, `items`, etc., or notes "N/A").

**`output/report.md`** — human-readable summary with sections:
- *SPA traffic summary* — list of unique `api.pocketcasts.com/*` endpoints the SPA hit, ordered by first-seen, with the count of calls and a one-line note on what page was active when the call fired.
- *Probe results* — table: endpoint | payload | status | episodes returned | notes.
- *Recommended next step* — heuristic verdict: "endpoint X with payload Y returned N episodes (vs baseline of 1) — start the patch here." Generated mechanically from probe results, not LLM-style speculation.

## Error handling

- Missing credentials file → exit 1 with a clear message and example JSON.
- Login failure (no redirect to `/podcasts` within 30s) → screenshot to `output/login-failure.png`, exit 1.
- Token extraction failure → script continues with HAR-only mode, reports which `localStorage` keys *were* present so we can fix the extractor next run.
- Per-probe failures (4xx/5xx/timeout) → recorded in `probes.json` with the error, run continues.
- No `/podcasts` UUID for the per-show probe → that probe is skipped and noted.

## Testing strategy

This is a one-shot discovery script, not a long-lived service. "Testing" = running it end-to-end against the real Pocket Casts account and reading `report.md`. No unit tests planned. The script is small enough that the cost of writing tests outweighs the benefit for a tool we may run twice.

## Follow-up (out of scope for this spec)

Once the report identifies the right endpoint(s), the next session:
1. Write a separate plan for the Yamtrack fork patch.
2. Likely changes target the `pocketcasts_import` task path on the Pi.
3. Open a PR against `dannyvfilms/Yamtrack`.
