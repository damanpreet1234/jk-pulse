# J&K Pulse

A free, self-updating news & mood tracker for Jammu & Kashmir. It watches
free public sources for J&K-related coverage, scores the tone of what it
finds, tracks it per district, and flags topics that suddenly spike — with
zero manual upkeep once it's set up, and zero dependency on any Claude/AI
plan or subscription.

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
   click *"Collect J&K pulse data"* on the left → *Run workflow* → *Run
   workflow*. Wait ~1-2 minutes, refresh, and check it finished with a
   green check. Then open your Pages URL from step 4 — you should see real
   data instead of "No data collected yet."

That's it. From here it runs itself every 4 hours automatically, and the
dashboard always shows the latest snapshot.

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
- **Add/remove watched districts**: edit `scripts/geo_reference.json`.
- **Tune what counts as a "spike"**: see `compute_spikes()` in
  `scripts/collect.py` (the `z_threshold` and `min_mentions` values).
- **Improve Reddit reliability**: swap the plain HTTP call in
  `fetch_reddit()` for a free registered Reddit app (PRAW) if the
  best-effort approach gets blocked too often for your liking.

## Repo layout

```
index.html                     the dashboard (GitHub Pages serves this)
scripts/collect.py             the data collector (runs on GitHub Actions)
scripts/geo_reference.json     J&K districts + region metadata
data/latest.json               current snapshot (read by the dashboard)
data/history.jsonl             compact time series, one line per run
data/raw/                      last run's raw article list (debugging)
.github/workflows/collect.yml  the free scheduled job definition
tests/test_collect.py          offline tests for the collector's logic
```
