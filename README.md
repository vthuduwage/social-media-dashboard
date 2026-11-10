# Social Post Analytics Tracker (GCP)

Tracks views, likes, comments and shares for a set of public TikTok / Instagram /
Facebook posts every 30 minutes, stores every snapshot in Firestore, and serves a
public, read-only, mobile-friendly dashboard on Cloud Run.

```
Cloud Scheduler (*/30) ──► Cloud Run Job "post-collector" ──► Firestore "snapshots"
                              yt-dlp -j  →  Playwright fallback        │
                                                                       ▼
                                        Cloud Run service "post-dashboard" (public URL)
```

## What you get

For each post: **views, likes, comments, shares**, plus
**interactions = likes + comments + shares**. Across all posts, the dashboard
shows **total interactions** (the headline number), total views, total likes and
total comments, plus growth charts (24h / 7d / 30d) and the last-updated time.

Views are reported separately and are not counted as interactions.

## Repo layout

| Path | What it is |
|---|---|
| `collector/main.py` | Collector. Tries `yt-dlp -j --skip-download` first, then Playwright on the public page for any missing fields. Logs which method worked. Writes one Firestore doc per post per run. |
| `collector/posts.json` | The posts to track. Edit to add or remove posts. |
| `dashboard/app.py` | Flask app: `/` (page) and `/api/data` (JSON). Caches snapshots in memory and only reads new docs on refresh. |
| `dashboard/templates/index.html` | Dashboard UI (Chart.js is vendored in `dashboard/static/`, so there's no CDN dependency). |
| `deploy/*.sh` | Deploy scripts. Images are built with **Cloud Build**, so you don't need Docker locally. |

### Firestore document (`snapshots` collection)

```json
{
  "timestamp": "2026-11-10T15:30:12Z",   // when this post was read
  "run_ts":    "2026-11-10T15:30:00Z",   // start of the collector run (aligns charts)
  "platform":  "tiktok",
  "url":       "https://vt.tiktok.com/ZSp6sFsuS/",
  "views": 45210, "likes": 1520, "comments": 87, "shares": 33,
  "interactions": 1640,
  "status":  "ok",                        // ok | partial | error
  "method":  "yt-dlp",                    // yt-dlp | playwright | yt-dlp+playwright | none
  "errors":  {},                          // per-method error text when something failed
  "resolved_url": "...", "title": "...", "author": "...", "post_id": "..."
}
```

`status` is `ok` when views, likes and comments were all found. It's `partial` when
only some metrics were found, and `error` when none were.

## Deploy

Run these from **Cloud Shell**, or from any machine with `gcloud` logged in. The
project needs a billing account attached: Cloud Run, Cloud Build and Cloud
Scheduler all require one, even when usage stays inside the free tier.

```bash
export PROJECT_ID=post-analytics-$RANDOM          # a NEW, dedicated project
export BILLING_ACCOUNT=$(gcloud billing accounts list --format='value(name)' --limit 1)
./deploy/00a_create_project.sh  # create project, link billing, $1 budget alert
./deploy/00_setup.sh            # APIs, Artifact Registry, Firestore, service accounts
./deploy/01_test_collector.sh   # GATE: build + run the collector ONCE, print per-platform results
```

`01_test_collector.sh` prints a table like the one below. It shows each platform's
status, the method that worked, the counts, and the errors for anything that failed:

```
platform   status   method               views      likes  comments   shares
tiktok     ok       yt-dlp              45,210      1,520        87       33
instagram  error    none                   n/a        n/a       n/a      n/a
    yt-dlp: ERROR: [Instagram] ...: Requested content is not available, rate-limit reached or login required
    playwright: login wall (https://www.instagram.com/accounts/login/...)
...
Totals: views 45,210 | likes 1,520 | comments 87 | shares 33 | ALL INTERACTIONS (likes+comments+shares) 1,640
```

**Review that output before going further.** If Instagram or Facebook are
blocked from GCP, decide how to handle it before you deploy the rest. Once you're
happy with it:

```bash
./deploy/02_deploy_all.sh       # dashboard (public URL) + Cloud Scheduler every 30 min
```

Other useful commands:

```bash
gcloud scheduler jobs run post-collector-every-30m --location us-central1   # collect now
gcloud run jobs executions list --job post-collector --region us-central1   # history
./deploy/99_teardown.sh                                                     # remove everything except Firestore data
```

Settings such as `REGION`, the service names and `SCHEDULE` live in
`deploy/config.sh`. You can override any of them with environment variables.

If `02_deploy_all.sh` fails on `--allow-unauthenticated`, your organization
probably enforces the *Domain restricted sharing* policy, which blocks public
(`allUsers`) access. An org admin has to grant an exception for this project.

## Run locally

```bash
cd collector && pip install -r requirements.txt && python -m playwright install chromium
python main.py --dry-run          # prints results, no Firestore writes
```

```bash
cd dashboard && pip install -r requirements.txt
gcloud auth application-default login
GOOGLE_CLOUD_PROJECT=your-project-id python app.py   # http://localhost:8080
```

## How collection works

1. Resolve short or share links (`vt.tiktok.com`, `facebook.com/share/v/...`) to
   the canonical URL. Tracking query parameters are dropped for IG and FB.
2. **yt-dlp** (`-j --skip-download`) reads `view_count`, `like_count`,
   `comment_count` and `repost_count`.
3. If any of those are missing, **Playwright** (headless Chromium) opens the
   public page and reads:
   - TikTok: `playCount` / `diggCount` / `commentCount` / `shareCount` from the
     embedded rehydration JSON.
   - Instagram: `play_count` / `like_count` / `comment_count` from page or XHR
     JSON, then the `og:description` text ("1,204 likes, 56 comments - …").
   - Facebook: `video_view_count`, `reaction_count`, `comments.total_count` and
     `share_count` from page or GraphQL JSON, then visible "1.2K views" text.
4. Every post logs a line such as
   `[tiktok] status=ok method=yt-dlp values={...}`, and every run ends with a
   `SUMMARY {...}` JSON line.

The run exits non-zero only if **every** post failed, so one blocked platform
doesn't mark the whole scheduled execution as failed.

When a post fails, the dashboard keeps showing that metric's last good value.
The card's status badge shows the latest run's result.

## Expected costs

Pricing uses `us-central1`. The free-tier figures are per billing account per month.

| Service | Usage | Free tier | Expected cost |
|---|---|---|---|
| Cloud Run Jobs (collector, 1 vCPU / 1 GiB) | 48 runs/day ≈ 1,440/mo. At ~60–120 s per run: 86k–173k vCPU-s, 86k–173k GiB-s | 180k vCPU-s, 360k GiB-s (shared with the dashboard) | **$0** if runs average under ~2 min. If Playwright runs for every post on every run (~3 min), about **$1–2/mo** |
| Cloud Run service (dashboard, 256 MiB, scales to zero) | Light traffic | 2M requests, plus the compute above | **$0** |
| Firestore | ~144 writes/day. Reads are incremental and cached (a cold start reads ≤ 30 days ≈ 4.3k docs) | 20k writes, 50k reads per day, 1 GiB storage | **$0** |
| Cloud Scheduler | 1 job | 3 jobs | **$0** |
| Cloud Build | A few builds, ~5–10 min each (the collector installs Chromium) | Free build-minutes per month | **$0** |
| Artifact Registry | Collector image (Chromium headless shell only) + dashboard; the cleanup policy keeps only the latest version of each | 0.5 GB | **$0** if total stays under 0.5 GB, otherwise ~$0.10/GB/mo over |
| Cloud Storage | Build source/logs in a us-central1 bucket, auto-deleted after 1 day | 5 GB in us-central1 | **$0** |
| Cloud Logging | A few MB/month | 50 GiB | **$0** |

**Total: $0/month expected, and at most about $2/month in the worst case** if
Playwright has to run for every post on every run. The largest cost driver is
collector runtime. If one platform is permanently blocked, removing it from
`posts.json`, or changing `SCHEDULE` to hourly, halves that cost.

To get warned about unexpected charges, set a budget alert in the console
(Billing → Budgets & alerts), e.g. $5.

## Known limitations

- **Instagram and Facebook often block datacenter IPs** with login walls or rate
  limits, and Cloud Run egress IPs are datacenter IPs. That's why
  `01_test_collector.sh` exists. Workarounds, such as session cookies, a
  residential proxy, or the official Graph API for posts you own, are
  deliberately **not** built in yet.
- Facebook rarely exposes likes and comments publicly for share links, so expect
  `partial` there even when views work.
- Instagram reels often hide view counts from logged-out users.
- Scraped page structures change. When a platform changes its markup, update
  the regexes in `PATTERNS` in `collector/main.py`, and upgrade `yt-dlp` in
  `collector/requirements.txt`.
