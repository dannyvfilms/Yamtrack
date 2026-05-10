# Pocket Casts Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a one-shot Node script that logs into Pocket Casts via Playwright, captures the SPA's API traffic in a HAR file, fires a battery of REST probes against candidate history endpoints, and writes a discovery report identifying which endpoint(s) return full per-episode played-state.

**Architecture:** Single self-contained script in `pocketcasts-discovery/`. Uses parent folder's `node_modules` for Playwright (Node walks upward). Two complementary discovery axes in one run: HAR capture (ground truth — whatever the SPA hits) and REST probes (hypothesis tests — the candidate endpoints from the Pi-side AI). Three artifacts: `network.har`, `probes.json`, `report.md`.

**Tech Stack:** Node.js (ESM), Playwright (Chromium), built-in `fetch`, `node:fs`.

**Notes for the executor:**
- This folder is **not a git repo** today. Skip all `git add`/`git commit` steps the plan template normally calls for. If/when this becomes a repo, the `.gitignore` written in Task 1 already covers credentials and outputs.
- This is an exploratory third-party-API tool — TDD is not applicable (we can't unit-test against the unknown thing we're discovering). Each task's verification step is to run the script (or the relevant chunk) end-to-end against the real Pocket Casts account and confirm the expected stdout / artifact.
- Working directory throughout: your local project folder.

---

## Task 1: Scaffold the subfolder

**Files:**
- Create: `pocketcasts-discovery/.gitignore`
- Create: `pocketcasts-discovery/README.md`
- Create: `pocketcasts-discovery/pocketcasts_credentials.json`
- Create: `pocketcasts-discovery/output/.gitkeep`

- [ ] **Step 1: Create the directory tree**

The `pocketcasts-discovery/` and `pocketcasts-discovery/docs/` and `pocketcasts-discovery/output/` directories already exist from the brainstorming session. Verify with:

```bash
ls pocketcasts-discovery/
```

Expected: `docs/  output/`

- [ ] **Step 2: Write `.gitignore`**

Create `pocketcasts-discovery/.gitignore`:

```
pocketcasts_credentials.json
output/
!output/.gitkeep
node_modules/
```

- [ ] **Step 3: Write `README.md`**

Create `pocketcasts-discovery/README.md`:

````markdown
# Pocket Casts API Discovery

One-shot Playwright script that logs into Pocket Casts, captures the web SPA's API traffic, and probes candidate endpoints to find which one(s) expose full per-episode played-state. Result is a discovery report used to patch the Pocket Casts integration in dannyvfilms/Yamtrack.

See `docs/2026-04-20-pocketcasts-discovery-design.md` for context.

## Setup

Uses the parent folder's `node_modules` (Playwright already installed there). No `npm install` needed in this subfolder.

Create `pocketcasts_credentials.json` (gitignored):

```json
{ "email": "you@example.com", "password": "..." }
```

## Run

```bash
node pocketcasts-discovery/pocketcasts_discovery.mjs
```

Headless mode:

```bash
HEADLESS=1 node pocketcasts-discovery/pocketcasts_discovery.mjs
```

## Artifacts (in `output/`)

- `network.har` — full Chromium network log (Authorization headers scrubbed). Importable into Chrome DevTools.
- `probes.json` — raw responses from each REST probe.
- `report.md` — human-readable summary + recommendation.

If login fails, `output/login-failure.png` is written and the script exits 1.
````

- [ ] **Step 4: Write the credentials file**

Create `pocketcasts-discovery/pocketcasts_credentials.json` with the credentials from the original task brief:

```json
{
  "email": "your-pocketcasts@email.com",
  "password": "your-password-here"
}
```

- [ ] **Step 5: Add `output/.gitkeep`**

Create empty file `pocketcasts-discovery/output/.gitkeep` so the directory is preserved if the contents are ever cleaned.

- [ ] **Step 6: Verify scaffolding**

```bash
ls -la pocketcasts-discovery/
```

Expected: `.gitignore`, `README.md`, `pocketcasts_credentials.json`, `docs/`, `output/`.

---

## Task 2: Smoke-test Playwright resolves from parent

**Files:**
- Create: `pocketcasts-discovery/pocketcasts_discovery.mjs`

- [ ] **Step 1: Write minimal script**

Create `pocketcasts-discovery/pocketcasts_discovery.mjs`:

```javascript
import { chromium } from 'playwright';

console.log('playwright loaded:', typeof chromium.launch === 'function' ? 'ok' : 'missing');
```

- [ ] **Step 2: Run it**

```bash
node pocketcasts-discovery/pocketcasts_discovery.mjs
```

Expected stdout: `playwright loaded: ok`

If this fails with "Cannot find package 'playwright'", confirm the parent folder's `node_modules` exists (`ls node_modules/playwright`). Node walks up from the script's directory to find it.

---

## Task 3: Load credentials, launch Chromium, navigate to login page

**Files:**
- Modify: `pocketcasts-discovery/pocketcasts_discovery.mjs` (replace contents)

- [ ] **Step 1: Replace script with credential-loading + Chromium launch**

```javascript
import { chromium } from 'playwright';
import { readFileSync, existsSync, mkdirSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = __dirname;
const OUTPUT_DIR = resolve(ROOT, 'output');
const CREDS_PATH = resolve(ROOT, 'pocketcasts_credentials.json');
const HEADLESS = process.env.HEADLESS === '1';

function loadCredentials() {
  if (!existsSync(CREDS_PATH)) {
    console.error(`Missing ${CREDS_PATH}`);
    console.error(`Create it with: {"email": "...", "password": "..."}`);
    process.exit(1);
  }
  const creds = JSON.parse(readFileSync(CREDS_PATH, 'utf8'));
  if (!creds.email || !creds.password) {
    console.error('credentials file missing email or password');
    process.exit(1);
  }
  return creds;
}

function ensureOutputDir() {
  if (!existsSync(OUTPUT_DIR)) mkdirSync(OUTPUT_DIR, { recursive: true });
}

async function main() {
  ensureOutputDir();
  const creds = loadCredentials();
  console.log(`✓ credentials loaded for ${creds.email}`);

  const browser = await chromium.launch({ headless: HEADLESS, slowMo: HEADLESS ? 0 : 100 });
  const context = await browser.newContext();
  const page = await context.newPage();

  await page.goto('https://pocketcasts.com/user/login');
  console.log('✓ login page loaded');

  await page.waitForTimeout(3000);

  await context.close();
  await browser.close();
  console.log('✓ browser closed');
}

main().catch(e => {
  console.error(e);
  process.exit(1);
});
```

- [ ] **Step 2: Run it**

```bash
node pocketcasts-discovery/pocketcasts_discovery.mjs
```

Expected:
- Chromium window opens (headed)
- Pocket Casts login page renders
- Closes after ~3 seconds
- Stdout shows all three `✓` lines

---

## Task 4: Login flow with failure screenshot

**Files:**
- Modify: `pocketcasts-discovery/pocketcasts_discovery.mjs`

- [ ] **Step 1: Add `login()` function**

Add this above `main()`:

```javascript
async function login(page, creds) {
  await page.goto('https://pocketcasts.com/user/login');
  await page.getByRole('textbox', { name: /email/i }).fill(creds.email);
  await page.getByRole('textbox', { name: /password/i }).fill(creds.password);
  await page.getByRole('button', { name: /(sign in|log in|login)/i }).click();
  try {
    await page.waitForURL('**/podcasts', { timeout: 30000 });
    console.log('✓ logged in');
  } catch (e) {
    const shotPath = resolve(OUTPUT_DIR, 'login-failure.png');
    await page.screenshot({ path: shotPath, fullPage: true });
    console.error(`login failed; see ${shotPath}`);
    throw e;
  }
}
```

- [ ] **Step 2: Replace the navigation in `main()` with the login call**

Replace:

```javascript
  await page.goto('https://pocketcasts.com/user/login');
  console.log('✓ login page loaded');

  await page.waitForTimeout(3000);
```

With:

```javascript
  await login(page, creds);
  await page.waitForTimeout(2000);
```

- [ ] **Step 3: Run it**

```bash
node pocketcasts-discovery/pocketcasts_discovery.mjs
```

Expected:
- Browser logs in, lands on `/podcasts`
- Stdout shows `✓ logged in`

If the role-based selectors miss, the script writes `output/login-failure.png` — open it, identify the actual selectors (e.g., `input#email`), and adjust the `login()` function. Common fixes: `page.locator('input[type="email"]')` / `input[type="password"]` / `button[type="submit"]`.

---

## Task 5: Enable HAR recording

**Files:**
- Modify: `pocketcasts-discovery/pocketcasts_discovery.mjs`

- [ ] **Step 1: Add HAR path constant**

Below the existing `OUTPUT_DIR` line, add:

```javascript
const HAR_PATH = resolve(OUTPUT_DIR, 'network.har');
```

- [ ] **Step 2: Pass `recordHar` to `newContext()`**

In `main()`, change:

```javascript
  const context = await browser.newContext();
```

to:

```javascript
  const context = await browser.newContext({ recordHar: { path: HAR_PATH } });
```

- [ ] **Step 3: Run and verify HAR is written**

```bash
rm -f "pocketcasts-discovery/output/network.har"
node pocketcasts-discovery/pocketcasts_discovery.mjs
ls -la pocketcasts-discovery/output/network.har
```

Expected: `network.har` exists, size > 50KB (login page alone produces plenty of traffic).

Sanity check the HAR contains Pocket Casts traffic:

```bash
node -e "const h=JSON.parse(require('fs').readFileSync('pocketcasts-discovery/output/network.har','utf8')); console.log('entries:', h.log.entries.length, 'pocketcasts hits:', h.log.entries.filter(e => e.request.url.includes('pocketcasts.com')).length);"
```

Expected: both numbers > 0.

---

## Task 6: Extract bearer token from localStorage

**Files:**
- Modify: `pocketcasts-discovery/pocketcasts_discovery.mjs`

- [ ] **Step 1: Add `extractToken()` function**

Add above `main()`:

```javascript
async function extractToken(page) {
  const all = await page.evaluate(() => {
    const out = {};
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      out[k] = localStorage.getItem(k);
    }
    return out;
  });
  for (const [k, v] of Object.entries(all)) {
    if (typeof v === 'string' && v.startsWith('eyJ')) {
      console.log(`✓ token from localStorage["${k}"]: ${v.slice(0, 8)}...${v.slice(-4)}`);
      return v;
    }
    try {
      const parsed = JSON.parse(v);
      const candidate = parsed?.accessToken || parsed?.access_token || parsed?.token;
      if (typeof candidate === 'string' && candidate.startsWith('eyJ')) {
        console.log(`✓ token from localStorage["${k}"].accessToken: ${candidate.slice(0, 8)}...${candidate.slice(-4)}`);
        return candidate;
      }
    } catch {}
  }
  console.warn('✗ token not found. localStorage keys present:', Object.keys(all));
  return null;
}
```

- [ ] **Step 2: Call it after login in `main()`**

After `await login(page, creds);`, add:

```javascript
  const token = await extractToken(page);
```

(Remove the now-redundant `await page.waitForTimeout(2000);` line.)

- [ ] **Step 3: Run it**

```bash
node pocketcasts-discovery/pocketcasts_discovery.mjs
```

Expected: `✓ token from localStorage[...]: eyJ...XXXX`.

If it fails, the `localStorage keys present` log shows what to extend the function to look at. Common alternatives: `sessionStorage`, or the token may live in a cookie — adjust `extractToken` accordingly.

---

## Task 7: SPA navigation to provoke API calls

**Files:**
- Modify: `pocketcasts-discovery/pocketcasts_discovery.mjs`

- [ ] **Step 1: Add `navigateSPA()` function**

Add above `main()`:

```javascript
async function navigateSPA(page) {
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});

  const wts = page.locator('text=What the Shell?').first();
  if (await wts.count() > 0) {
    await wts.click().catch(() => {});
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    console.log('✓ visited What the Shell?');
  } else {
    const first = page.locator('a[href*="/podcasts/"]').first();
    if (await first.count() > 0) {
      await first.click().catch(() => {});
      await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
      console.log('✓ visited first podcast (What the Shell? not found)');
    }
  }

  await page.goto('https://pocketcasts.com/podcasts');
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});

  for (const label of ['History', 'Stats', 'Profile', 'Listening History', 'Filters']) {
    const link = page.locator(`text=${label}`).first();
    if (await link.count() > 0) {
      await link.click().catch(() => {});
      await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
      console.log(`✓ visited ${label}`);
    }
  }
}
```

- [ ] **Step 2: Call it in `main()`**

After `const token = await extractToken(page);`, add:

```javascript
  await navigateSPA(page);
```

- [ ] **Step 3: Run and verify HAR has more traffic now**

```bash
rm -f "pocketcasts-discovery/output/network.har"
node pocketcasts-discovery/pocketcasts_discovery.mjs
node -e "const h=JSON.parse(require('fs').readFileSync('pocketcasts-discovery/output/network.har','utf8')); const api=h.log.entries.filter(e => e.request.url.includes('api.pocketcasts.com')); console.log('api.pocketcasts.com calls:', api.length); console.log('unique endpoints:'); console.log([...new Set(api.map(e => new URL(e.request.url).pathname))].sort().join('\n'));"
```

Expected: at least 5+ API calls, unique endpoints include things like `/user/login`, `/user/podcast/list`, `/user/history` and ideally novel ones we hadn't predicted.

---

## Task 8: REST probe runner

**Files:**
- Modify: `pocketcasts-discovery/pocketcasts_discovery.mjs`

- [ ] **Step 1: Add `probe()` and `countEpisodes()` helpers**

Add above `main()`:

```javascript
function countEpisodes(body) {
  if (!body || typeof body !== 'object') return null;
  for (const key of ['episodes', 'history', 'items', 'episodeList']) {
    if (Array.isArray(body[key])) return body[key].length;
  }
  return null;
}

async function probe(endpoint, payload, token) {
  const url = `https://api.pocketcasts.com${endpoint}`;
  const start = Date.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 60000);
  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
        'Accept': '*/*',
        'X-App-Language': 'en',
        'X-User-Region': 'global',
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15',
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const text = await res.text();
    let body;
    try { body = JSON.parse(text); } catch { body = text; }
    return {
      endpoint, payload,
      status: res.status,
      durationMs: Date.now() - start,
      responseBody: body,
      episodeCount: countEpisodes(body),
    };
  } catch (e) {
    return {
      endpoint, payload,
      status: 0,
      durationMs: Date.now() - start,
      error: String(e),
      episodeCount: null,
    };
  } finally {
    clearTimeout(timer);
  }
}
```

- [ ] **Step 2: Smoke-test the probe in `main()`**

After `await navigateSPA(page);`, temporarily add:

```javascript
  if (token) {
    const test = await probe('/user/podcast/list', {}, token);
    console.log('test probe /user/podcast/list:', test.status, 'episodes:', test.episodeCount, 'sample keys:', test.responseBody && typeof test.responseBody === 'object' ? Object.keys(test.responseBody) : 'not object');
  }
```

- [ ] **Step 3: Run it**

```bash
node pocketcasts-discovery/pocketcasts_discovery.mjs
```

Expected output includes: `test probe /user/podcast/list: 200 episodes: null sample keys: [ 'podcasts' ]` (or similar — the key may be `podcasts` rather than `episodes`, that's fine — `episodeCount: null` is correct here because this endpoint returns shows not episodes).

Once verified, **delete the temporary smoke-test block** before continuing to Task 9.

---

## Task 9: Run all candidate probes + save raw responses

**Files:**
- Modify: `pocketcasts-discovery/pocketcasts_discovery.mjs`

- [ ] **Step 1: Add `PROBES_PATH` constant**

Below `HAR_PATH`:

```javascript
const PROBES_PATH = resolve(OUTPUT_DIR, 'probes.json');
```

- [ ] **Step 2: Add `runProbes()` function**

Above `main()`:

```javascript
async function runProbes(token, harPath) {
  const probes = [];

  const baselineList = await probe('/user/podcast/list', {}, token);
  probes.push(baselineList);

  let knownUuid = null;
  if (baselineList.responseBody?.podcasts?.length) {
    const wts = baselineList.responseBody.podcasts.find(p => /What the Shell/i.test(p.title));
    knownUuid = (wts || baselineList.responseBody.podcasts[0])?.uuid;
    console.log(`✓ probe target podcast UUID: ${knownUuid}`);
  }

  const probeList = [
    ['/user/history', {}],
    ['/user/history', { count: 1000 }],
    ['/user/history', { limit: 100, offset: 0 }],
    ['/user/history', { page: 2 }],
    ['/user/history', { count: 1000, page: 1 }],
    ['/user/stats', {}],
    ['/user/episodes', {}],
    ['/user/listening_history', {}],
  ];
  if (knownUuid) {
    probeList.push(['/user/podcast/episodes', { uuid: knownUuid }]);
    probeList.push(['/user/podcast/episodes', { podcastUuid: knownUuid }]);
  }

  for (const novel of discoverNovelEndpointsFromHar(harPath, probeList)) {
    probeList.push([novel, {}]);
  }

  for (const [ep, payload] of probeList) {
    const result = await probe(ep, payload, token);
    console.log(`  probe ${ep} ${JSON.stringify(payload)} → ${result.status} (episodes: ${result.episodeCount ?? 'n/a'})`);
    probes.push(result);
  }

  return probes;
}

function discoverNovelEndpointsFromHar(harPath, alreadyProbing) {
  if (!existsSync(harPath)) return [];
  const har = JSON.parse(readFileSync(harPath, 'utf8'));
  const have = new Set(alreadyProbing.map(([ep]) => ep));
  const novel = new Set();
  for (const e of har.log?.entries || []) {
    const u = new URL(e.request.url);
    if (u.host !== 'api.pocketcasts.com') continue;
    if (!u.pathname.startsWith('/user/')) continue;
    if (have.has(u.pathname)) continue;
    if (u.pathname === '/user/login' || u.pathname === '/user/login_pocket' || u.pathname === '/user/token') continue;
    novel.add(u.pathname);
  }
  return [...novel];
}
```

Note: the HAR is only flushed when `context.close()` is called. In `main()` the probes need to run *before* context close to use the live token, but also benefit from the HAR being available to mine. We'll flush HAR-then-probe (closing the context first releases the HAR file).

- [ ] **Step 3: Restructure `main()` to close context before probing**

Replace the body of `main()` from `const browser = await chromium.launch...` through the end with:

```javascript
  const browser = await chromium.launch({ headless: HEADLESS, slowMo: HEADLESS ? 0 : 100 });
  const context = await browser.newContext({ recordHar: { path: HAR_PATH } });
  const page = await context.newPage();

  let token = null;
  try {
    await login(page, creds);
    token = await extractToken(page);
    await navigateSPA(page);
  } finally {
    await context.close();
    await browser.close();
  }
  console.log('✓ browser closed (HAR flushed)');

  let probes = [];
  if (token) {
    probes = await runProbes(token, HAR_PATH);
    writeFileSync(PROBES_PATH, JSON.stringify(probes, null, 2));
    console.log(`✓ wrote ${PROBES_PATH}`);
  } else {
    console.warn('skipping REST probes; no token');
  }
```

Also add `writeFileSync` to the `node:fs` import at the top:

```javascript
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
```

- [ ] **Step 4: Run it**

```bash
node pocketcasts-discovery/pocketcasts_discovery.mjs
```

Expected: stdout shows each probe with status code and episode count; `output/probes.json` exists and is valid JSON with one entry per probe.

Verify:

```bash
node -e "const p=JSON.parse(require('fs').readFileSync('pocketcasts-discovery/output/probes.json','utf8')); console.log('probe count:', p.length); console.log('statuses:', p.map(x => x.endpoint + ' ' + JSON.stringify(x.payload) + ' → ' + x.status + ' (' + (x.episodeCount ?? 'n/a') + ')').join('\n'));"
```

---

## Task 10: HAR scrub — strip Authorization headers

**Files:**
- Modify: `pocketcasts-discovery/pocketcasts_discovery.mjs`

- [ ] **Step 1: Add `scrubHAR()` function**

Above `main()`:

```javascript
function scrubHAR(harPath) {
  const har = JSON.parse(readFileSync(harPath, 'utf8'));
  for (const e of har.log?.entries || []) {
    for (const h of e.request?.headers || []) {
      if (h.name.toLowerCase() === 'authorization') h.value = 'Bearer ***';
    }
    for (const h of e.response?.headers || []) {
      if (['set-cookie', 'authorization'].includes(h.name.toLowerCase())) h.value = '***';
    }
  }
  writeFileSync(harPath, JSON.stringify(har, null, 2));
}
```

- [ ] **Step 2: Call it in `main()`**

After the `console.log('✓ browser closed (HAR flushed)');` line:

```javascript
  scrubHAR(HAR_PATH);
  console.log(`✓ scrubbed Authorization headers in ${HAR_PATH}`);
```

- [ ] **Step 3: Run and verify**

```bash
node pocketcasts-discovery/pocketcasts_discovery.mjs
grep -c 'Bearer eyJ' pocketcasts-discovery/output/network.har || echo "no leaked tokens"
grep -c '"value": "Bearer \*\*\*"' pocketcasts-discovery/output/network.har
```

Expected: first command prints `no leaked tokens` (or `0`); second prints a positive integer.

---

## Task 11: Generate `report.md`

**Files:**
- Modify: `pocketcasts-discovery/pocketcasts_discovery.mjs`

- [ ] **Step 1: Add `REPORT_PATH` constant**

Below `PROBES_PATH`:

```javascript
const REPORT_PATH = resolve(OUTPUT_DIR, 'report.md');
```

- [ ] **Step 2: Add `summarizeHAR()` and `writeReport()` functions**

Above `main()`:

```javascript
function summarizeHAR(harPath) {
  const har = JSON.parse(readFileSync(harPath, 'utf8'));
  const seen = new Map();
  for (const e of har.log?.entries || []) {
    const u = new URL(e.request.url);
    if (u.host !== 'api.pocketcasts.com') continue;
    const key = `${e.request.method} ${u.pathname}`;
    if (!seen.has(key)) {
      const referer = e.request.headers.find(h => h.name.toLowerCase() === 'referer')?.value;
      seen.set(key, { count: 0, firstReferer: referer });
    }
    seen.get(key).count++;
  }
  return [...seen.entries()].map(([endpoint, v]) => ({ endpoint, ...v }));
}

function writeReport(harSummary, probes) {
  const lines = [
    '# Pocket Casts API Discovery Report',
    '',
    `Generated: ${new Date().toISOString()}`,
    '',
    '## SPA traffic summary',
    '',
    'Endpoints called by `pocketcasts.com` SPA during the run.',
    '',
    '| Endpoint | Calls | First seen on |',
    '|----------|-------|----------------|',
  ];
  for (const e of harSummary.sort((a, b) => b.count - a.count)) {
    lines.push(`| \`${e.endpoint}\` | ${e.count} | ${e.firstReferer ?? '—'} |`);
  }
  lines.push('');

  lines.push('## Probe results', '');
  lines.push('| Endpoint | Payload | Status | Episodes | Notes |');
  lines.push('|----------|---------|--------|----------|-------|');
  for (const p of probes) {
    const note = p.error ?? (p.status >= 200 && p.status < 300 ? 'ok' : `http ${p.status}`);
    lines.push(`| \`${p.endpoint}\` | \`${JSON.stringify(p.payload)}\` | ${p.status} | ${p.episodeCount ?? 'n/a'} | ${note} |`);
  }
  lines.push('');

  lines.push('## Recommendation', '');
  const successful = probes.filter(p => p.status === 200 && typeof p.episodeCount === 'number' && p.episodeCount > 0);
  if (successful.length === 0) {
    lines.push('No probe returned a countable episode list. Inspect `network.har` manually to see what the SPA actually hits.');
  } else {
    successful.sort((a, b) => b.episodeCount - a.episodeCount);
    const winner = successful[0];
    lines.push(`Best probe: \`POST ${winner.endpoint}\` with payload \`${JSON.stringify(winner.payload)}\` returned **${winner.episodeCount} episodes**.`);
    lines.push('');
    const baseline = probes.find(p => p.endpoint === '/user/history' && JSON.stringify(p.payload) === '{}');
    if (baseline) {
      lines.push(`Baseline (currently used by Yamtrack): \`POST /user/history {}\` returned ${baseline.episodeCount ?? 'n/a'} episodes.`);
      lines.push('');
    }
    lines.push('Patch the Yamtrack `pocketcasts_import` task to use the winning endpoint+payload above.');
  }

  writeFileSync(REPORT_PATH, lines.join('\n'));
}
```

- [ ] **Step 3: Call it in `main()`**

After the `scrubHAR(HAR_PATH);` lines:

```javascript
  const harSummary = summarizeHAR(HAR_PATH);
  writeReport(harSummary, probes);
  console.log(`✓ wrote ${REPORT_PATH}`);
```

- [ ] **Step 4: Run it**

```bash
node pocketcasts-discovery/pocketcasts_discovery.mjs
cat pocketcasts-discovery/output/report.md
```

Expected: a clean markdown report with three sections, a populated SPA traffic table, a probe-results table, and a recommendation paragraph.

---

## Task 12: End-to-end smoke run + sanity check

- [ ] **Step 1: Clean previous output and re-run from scratch**

```bash
rm -f pocketcasts-discovery/output/network.har pocketcasts-discovery/output/probes.json pocketcasts-discovery/output/report.md pocketcasts-discovery/output/login-failure.png
node pocketcasts-discovery/pocketcasts_discovery.mjs
```

Expected: all `✓` log lines, no warnings, exit 0.

- [ ] **Step 2: Verify all three artifacts exist and look healthy**

```bash
ls -la pocketcasts-discovery/output/
node -e "const p=JSON.parse(require('fs').readFileSync('pocketcasts-discovery/output/probes.json','utf8')); console.log('probes:', p.length, '| any 200s:', p.filter(x => x.status === 200).length, '| max episodes:', Math.max(...p.map(x => x.episodeCount ?? 0)));"
```

Expected: `network.har`, `probes.json`, `report.md` present; probes count > 0; at least one 200; max episodes ideally > 1 (we're hoping to find an endpoint that beats the broken baseline).

- [ ] **Step 3: Confirm no token leaks**

```bash
grep -c 'Bearer eyJ' pocketcasts-discovery/output/network.har pocketcasts-discovery/output/report.md pocketcasts-discovery/output/probes.json
```

Expected: all three counts are `0`. (`probes.json` contains response bodies but never the request Authorization header; HAR is scrubbed; report renders bearer values nowhere.)

- [ ] **Step 4: Read `report.md` and judge**

Open `pocketcasts-discovery/output/report.md` in an editor or print it:

```bash
cat pocketcasts-discovery/output/report.md
```

The "Recommendation" section should name an endpoint+payload that returned more episodes than the broken `/user/history {}` baseline. If yes — discovery succeeded; hand the report (plus the HAR) to the Pi-side AI for the fork patch. If no — the SPA traffic summary still tells us what the website itself hits, which is the next investigation lead.

---

## Definition of done

All 12 tasks checked. Running `node pocketcasts-discovery/pocketcasts_discovery.mjs` from a clean state produces:
- `output/network.har` (scrubbed of Authorization headers)
- `output/probes.json` (one entry per probe with status + episode count)
- `output/report.md` (SPA traffic table + probe table + recommendation)

…and stdout reports `✓` for each milestone. The recommendation in `report.md` either identifies a usable endpoint or honestly states that none of the probed endpoints worked and points the reader at the HAR for follow-up.
