# J&K Pulse

A free, self-updating news & mood tracker — for Jammu & Kashmir, and (in a
second section) for Pakistan's own media coverage. It watches free public
sources, scores the tone of what it finds, tracks it per district/province,
and flags topics that suddenly spike — with zero manual upkeep once it's set
up, and zero dependency on any Claude/AI plan or subscription.

The dashboard is a small app, not just a static page: a **Region** switcher
at the top toggles between "Jammu & Kashmir" and "Pakistan (Pakistani
media)" — each with its own tabs for Overview, By District/Province,
Search, and Sources; a date picker so you can look at any day in the last
30 (or "All available"); a search box over every collected headline; and
every topic/area row expands to show the actual source articles it's based
on, each linking straight to the original story.

**How it stays free and automatic:** GitHub itself does all the ongoing
work, using only its free tier.
- **GitHub Actions** (free scheduled runs) fetches fresh data every few
  hours and commits it into this repo. That commit *is* the database — no
  separate database service, no signup, nothing to pay for.
- **GitHub Pages** (free static hosting) serves `index.html`, which reads
  that data and renders the dashboard. This is your permanent, always-on
  "admin portal" — it's just a URL, open it anytime.

Once deployed, you never need to run anything yourself, and this has
nothing to do with your Claude account or plan — it runs entirely on
GitHub's infrastructure, forever, for free.

## What it actually tracks (and what it honestly can't)

Sources used, all free and requiring no paid API:
- **GDELT** — a free global news database with location tagging. No key needed.
- **Google News** (RSS search) — free, no key needed.
- **Reddit** (public search) — best-effort; Reddit can rate-limit or block
  this without notice, so treat it as a bonus signal, not a guarantee.
- **YouTube** — optional. Only runs if you add a free `YOUTUBE_API_KEY`
  repo secret (see below). Skipped cleanly if you don't.

**Not included, on purpose:** X/Twitter and Instagram/Facebook. As of 2026,
neither offers a free public search API — X charges per read, and
Instagram/Facebook only expose content you own, not public search. Adding
either later means paying for API access; nothing free will get you real
coverage from those two.

**What "mood" means here:** this measures the *tone of news coverage*
(via automated text scoring), not verified public opinion. The dashboard
says this explicitly, and district-level figures depend on a district's
name actually appearing in a story — they're best-effort signals, not a
census. Please read results with that in mind, especially for a region
where headlines can carry a lot of nuance a script can't fully capture.

## The Pakistan section

Switch the **Region** dropdown at the top of the dashboard to "Pakistan
(Pakistani media)" to see a parallel tracker for Pakistan, broken down by
its own provinces/territories (Punjab, Sindh, Khyber Pakhtunkhwa,
Balochistan, Gilgit-Baltistan, Azad Kashmir, Islamabad).

A few things worth understanding about how this one works:

- **It only looks at Pakistani-based outlets.** GDELT is queried with its
  `sourcecountry:pakistan` operator, and Google News is queried through its
  Pakistan edition (`gl=PK`). So this reflects what's actually circulating
  *in* Pakistani media (Dawn, Geo News, ARY News, Express Tribune, etc.) —
  not global coverage about Pakistan from outlets elsewhere.
- **"Tone toward India" is a separate number**, not the same as the
  region's general tone. It's computed only from the subset of that
  Pakistani coverage which mentions India, Modi, New Delhi, Bharat, or
  Kashmir — so you can see, at a glance, whether that specific slice of
  coverage is running warm or hostile, independent of Pakistan's overall
  news mood that day (which is mostly about unrelated domestic topics).
- **Punjab and Islamabad get disambiguated in queries** (an explicit
  "Pakistan" is added to those two searches) since "Punjab" is also the
  name of an Indian state — otherwise those two provinces' counts would be
  inflated by unrelated Indian-Punjab coverage.
- **One thing not yet confirmed against live data**: GDELT's
  `sourcecountry:` filter is used here exactly as GDELT's own
  documentation describes it (lowercase country name, no spaces), but this
  sandbox couldn't reach GDELT directly to verify it live. The very first
  scheduled run after you deploy this will be the real proof — check the
  Pakistan tab afterwards; if `sources_used` on the Sources tab looks empty
  or the Pakistan article counts look off, that operator may need
  adjusting (see `fetch_gdelt()`'s `extra` parameter in `scripts/collect.py`).

Everything else — the free/no-key sources, the "tone of coverage, not
verified opinion" caveat, the spike detection, the search/date-picker/
source-link UI — works identically to the J&K section, just scoped to
Pakistan's own media and provinces.

## One-time setup (about 5 minutes, no command line needed)

1. **Create a free GitHub account** at github.com, if you don't have one.
2. **Create a new repository**: click the `+` in the top right → *New
   repository*. Name it whatever you like (e.g. `jk-pulse`). Keep it
   **Public** (required for GitHub Pages and Actions to be free on a
   personal account). Don't initialize it with a README — leave it empty.
3. **Upload these files**: on your new repo's page, click *"uploading an
   existing file"* (or *Add file → Upload files*), then drag in everything
   from this folder — **including the hidden `.github` folder**. If your
   browser/OS hides it, use `git` instead (see "Alternative: using git"
   below), since the workflow file lives there.
4. **Turn on Pages**: Settings → Pages → under "Build and deployment",
   set Source to **"Deploy from a branch"**, branch **main**, folder
   **/ (root)** → Save. GitHub gives you a URL like
   `https://<your-username>.github.io/<repo-name>/` — that's your
   dashboard link, forever.
5. **Turn on Actions write access**: Settings → Actions → General →
   scroll to "Workflow permissions" → select **"Read and write
   permissions"** → Save. This lets the scheduled job commit fresh data
   back into the repo.
6. **Run it once manually** to confirm it works: go to the *Actions* tab →
   click *"Collect J&K + Pakistan pulse data"* on the left → *Run workflow*
   → *Run workflow*. This now collects both regions in one run, so it can
   take a few minutes longer than before — wait, refresh, and check it
   finished with a green check. Then open your Pages URL from step 4 — you
   should see real data instead of "No data collected yet," and the
   **Region** dropdown should let you switch to Pakistan too.

That's it. From here it runs itself every 4 hours automatically, and the
dashboard always shows the latest snapshot for both regions.

### Alternative: using git (if you're comfortable with it)

```
cd jk-pulse
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```
Then do steps 4-6 above in the repo's Settings/Actions tab.

## Optional: add YouTube as an extra source (still free)

1. Go to console.cloud.google.com → create a project (free) → enable the
   "YouTube Data API v3" → create an API key.
2. In your repo: Settings → Secrets and variables → Actions → New
   repository secret → name it `YOUTUBE_API_KEY`, paste the key.
3. Next scheduled run will pick it up automatically. No other changes needed.

## Adjusting things later

- **Run more/less often**: edit `.github/workflows/collect.yml`, change
  the `cron` line (currently `0 */4 * * *` = every 4 hours). Lower
  numbers mean fresher data but more GitHub Actions minutes used (still
  free at this scale, GitHub's free tier is generous).
- **Add/remove watched districts or provinces**: edit
  `scripts/geo_reference.json` (`priority_region.divisions` for J&K,
  `pakistan_region.provinces` for Pakistan).
- **Tune what counts as a "spike"**: see `compute_spikes()` in
  `scripts/collect.py` (the `z_threshold` and `min_mentions` values).
- **Improve Reddit reliability**: swap the plain HTTP call in
  `fetch_reddit()` for a free registered Reddit app (PRAW) if the
  best-effort approach gets blocked too often for your liking.
- **Add a third region**: `scripts/collect.py`'s `REGIONS` list is exactly
  what drives both regions today — each entry is a plain config dict (area
  list, query-disambiguation function, GDELT/Google News filters, whether
  it needs an "India tone"-style sub-score, etc.). A new region means
  adding one more dict there plus a matching entry in `REGION_CONFIGS` in
  `index.html`; the collection/scoring/search/history logic is shared.

## Editing this with VS Code (or any editor)

Yes — this is just a plain folder of HTML/Python/JSON files under normal
git version control. Nothing about it is tied to any particular tool.

1. Install [VS Code](https://code.visualstudio.com/) (free) if you don't
   have it.
2. **File → Open Folder…** and pick the same local `jk-pulse` folder that
   GitHub Desktop (or `git`) is already managing. VS Code detects the
   `.git` folder automatically and shows a **Source Control** icon in the
   left sidebar — that's the same git history GitHub Desktop uses, so the
   two can be used interchangeably (just don't run a commit in both at the
   exact same moment).
3. Edit any file (`index.html`, `scripts/collect.py`, etc.) and save.
4. Use the Source Control panel (or `Ctrl+Shift+G`) to stage, write a
   commit message, and commit — then click **Sync Changes** (or the
   "..." menu → Push) to push to GitHub. Or keep using GitHub Desktop for
   the commit/push step if you prefer its UI — both work on the same repo.
5. Optional but handy: install the **Python** extension in VS Code for
   syntax highlighting and linting while editing `scripts/collect.py`.

No special "integration" step is needed beyond that — VS Code just needs
`git` installed on your machine (it usually prompts you to install it if
missing) and to be pointed at the folder.

**One important thing to know**: the `data/` folder (including
`data/pakistan/`) holds real collected data once the site has run. Don't
let anything (a fresh copy of this repo, an old zip, a "restore" of these
files) overwrite `data/latest.json`, `data/history.jsonl`,
`data/search_index.json`, or their `data/pakistan/` equivalents with
placeholder/empty versions — that would erase your accumulated history.
Only `scripts/`, `index.html`, `.github/`, and the docs are meant to be
hand-edited; `data/` is meant to be written only by the GitHub Action.

## Repo layout

```
index.html                     the dashboard (GitHub Pages serves this; region switcher for both)
scripts/collect.py             the data collector (runs on GitHub Actions; both regions)
scripts/geo_reference.json     J&K districts + Pakistan provinces + region metadata
data/latest.json               J&K: current snapshot (top-of-page stats)
data/history.jsonl             J&K: compact time series, one line per run (trend chart)
data/search_index.json         J&K: rolling 30-day article index (search + date picker + source links)
data/raw/                      J&K: last run's raw article list (debugging)
data/pakistan/latest.json      Pakistan: current snapshot, incl. "tone toward India"
data/pakistan/history.jsonl    Pakistan: compact time series, one line per run
data/pakistan/search_index.json  Pakistan: rolling 30-day article index
data/pakistan/raw/             Pakistan: last run's raw article list (debugging)
.github/workflows/collect.yml  the free scheduled job definition (collects both regions)
tests/test_collect.py          offline tests for the collector's logic (both regions)
```
