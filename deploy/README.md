# Deploying AI Signal

## GitHub Pages

GitHub Actions runs the Python sweep and publishes the resulting HTML, assets, and briefing JSON as a static site. Visitors can browse, search, and filter reports and view the published watchlist. Update `watchlist.json` in the repository to change what the Pages site tracks. Treat everything in that watchlist as public. [GitHub Pages publishing sources](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site)

1. In the GitHub repository, open **Settings → Pages → Build and deployment** and select **GitHub Actions** as the source. A repository maintainer or admin can configure it. Availability depends on repository visibility and your GitHub plan. [GitHub Pages setup](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site#publishing-with-a-custom-github-actions-workflow)
2. Push the project, including [the publishing workflow](../.github/workflows/publish-pages.yml), to `main`. The workflow runs on pushes to `main`. If your default branch has a different name, also change the `push.branches` value in the workflow.
3. To publish immediately or refresh on demand, open **Actions → Publish AI Signal → Run workflow**, choose the default branch, and start the run. The build tests the app, fetches the briefing, then passes the generated artifact to the `github-pages` deployment job. [GitHub Pages workflow requirements](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
4. Open the URL on the successful **Deploy to GitHub Pages** job or under **Settings → Pages**. For a standard project site the URL looks like `https://<owner>.github.io/<repo>/`.

The workflow is scheduled for **05:47, 13:47, and 21:47 IST** each day. Scheduled runs can start late. Each successful run keeps the latest report per day for up to 14 days and saves the snapshot as an Actions artifact for the next run. If every feed fails, the build stops before deployment and GitHub Pages continues serving the last successful briefing. [Scheduled workflow behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

### Preview the Pages build locally

~~~bash
python tools/build_pages.py --output _site
python -m http.server 8000 --bind 127.0.0.1 --directory _site
~~~

Open [the local Pages preview](http://127.0.0.1:8000). The build uses the versioned watchlist and live public feeds. `--previous` accepts a prior briefing JSON if you want to carry its archive into a new local build. The Python app remains available for deployments where people need to edit a shared watchlist or start a sweep from the browser.

## Cloud Run

One script, `deploy.sh`, sets up (or updates) everything: the web app, the
bucket it reads and writes, the daily sweep job, and the scheduler that
triggers it.

```
Cloud Scheduler ──(once a day)──▶ Cloud Run Job (ai-signal-sweep)
                                        │
                                        ▼
                                  gs://ai-signal-feed
                                   watchlist / archive / logs
                                        ▲
                                        │
                              Cloud Run Service (ai-signal-web)
                                   the app people open
```

Two separate Cloud Run resources, deliberately — not one service wearing
two hats:

- **`ai-signal-web`** is what people open in a browser: edit the watchlist,
  or click **Run sweep** to run a live briefing. Its edits to the watchlist
  go straight to the bucket, so everyone sees the same list. **A *Force
  refresh* result is not saved anywhere** — it's shown once, to whoever
  clicked it, and then gone. That's a real, known gap (see *What isn't
  wired up yet* below), not this deploy setup's doing.
- **`ai-signal-sweep`** is a Cloud Run *Job*: `sweep_runner.py`, run once a
  day by Cloud Scheduler. This is the one that writes `archive/*.json`
  and `logs/*.log` to the bucket — the durable, shared history.

If you've seen this described as "Scheduler → Cloud Run → `/refresh`
endpoint," that's the gist but not quite the mechanism: hitting an HTTP
endpoint would tie up a web request for the ~1–2 minutes a sweep takes
and, as above, wouldn't save its result anywhere. A Job triggered directly
is what actually persists a daily history.

## Before you run it

- The [gcloud CLI](https://cloud.google.com/sdk/docs/install), logged in:
  `gcloud auth login`.
- A GCP project with billing enabled, and permission in it to create
  service accounts, Cloud Run services/jobs, a bucket, and a scheduler
  job (Editor, or the equivalent narrower roles).
- Nothing else. The script contains no credentials, no keys, no
  hardcoded account — it acts entirely as whoever runs it.

## Running it

```bash
PROJECT_ID=your-project-id ./deploy/deploy.sh
```

That's the only required setting. Everything else has a default (see the
table below) and can be overridden the same way:

```bash
PROJECT_ID=your-project-id REGION=us-central1 SCHEDULE="0 3 * * *" ./deploy/deploy.sh
```

**Run it again whenever you push a change.** It rebuilds the image and
updates the existing service, job, and scheduler in place — it does not
create duplicates. Re-running with no code changes is also safe; each
step either uses an update-in-place command or checks first and skips
anything that already exists.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `PROJECT_ID` | *(required)* | |
| `REGION` | `asia-south1` | |
| `BUCKET` | `ai-signal-feed` | Created if missing. |
| `SERVICE_ACCOUNT_NAME` | `ai-signal-runner` | Runs both the service and the job. |
| `SERVICE_NAME` | `ai-signal-web` | |
| `JOB_NAME` | `ai-signal-sweep` | |
| `SCHEDULER_NAME` | `ai-signal-daily` | |
| `SCHEDULE` | `0 6 * * *` | Cron, in `TIME_ZONE`. |
| `TIME_ZONE` | `Asia/Kolkata` | |
| `ALLOW_UNAUTHENTICATED` | `false` | See *Access* below — leave this `false` unless you've deliberately decided otherwise. |
| `ENABLE_GOOGLE_NEWS` | `false` | See *Google News* below. |
| `BRAVE_API_KEY` | *(unset)* | If set, stored in Secret Manager, never in plain env vars or in this script. |

## Access

`ai-signal-web` is deployed **private** by default (`--no-allow-unauthenticated`).
That's deliberate: the app's `/api/watchlist` and `/api/refresh` endpoints
have no login check of their own (see
[`ai_signal/live/local_server.py`](../ai_signal/live/local_server.py)),
so a public URL would let anyone on the internet overwrite the watchlist
or trigger sweeps. The script prints the exact command to grant a
teammate access after it finishes; it looks like:

```bash
gcloud run services add-iam-policy-binding ai-signal-web \
  --region=REGION --project=PROJECT_ID \
  --member=user:name@example.com --role=roles/run.invoker
```

For broader, no-per-person-grant access, put
[Identity-Aware Proxy](https://cloud.google.com/iap) in front of it
instead and set `ALLOW_UNAUTHENTICATED=false` regardless (IAP sits in
front of a private service, not a public one). That's a one-time console
step this script doesn't automate.

## Google News

Off by default. Two independent reasons, either one is enough on its own:

1. Google's News RSS endpoint states it's for personal, non-commercial
   use.
2. In practice it has gone through hours-long windows where *every*
   query fails with `HTTP 503` — not a rate limit that eases off, a hard
   block. A production deployment shouldn't depend on a source that can
   go fully dark with no warning.

Set `ENABLE_GOOGLE_NEWS=true` only as a deliberate call, understanding
both of those.

## Verifying it worked

```bash
# Run the daily sweep right now, instead of waiting for the schedule
gcloud scheduler jobs run ai-signal-daily --location=REGION --project=PROJECT_ID

# Confirm it actually wrote something
gcloud storage ls gs://ai-signal-feed/archive/

# Watch it run
gcloud run jobs executions list --job=ai-signal-sweep --region=REGION --project=PROJECT_ID
```

## Reports

Live manual and scheduled sweeps write to the same archive. Keep the Cloud Run service private so watchlist edits and sweep requests require authorized access.
