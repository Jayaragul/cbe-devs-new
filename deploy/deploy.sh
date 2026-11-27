#!/usr/bin/env bash
# Deploys/updates AI Signal on Cloud Run: the web app, the bucket it reads and
# writes, the daily sweep job, and the scheduler that triggers it.
#
# Idempotent: running this again updates the existing service, job and
# scheduler in place rather than creating duplicates. Every gcloud call
# below either uses a create-or-update verb directly (`run deploy`,
# `run jobs deploy`), or checks with `describe` first and creates only if
# missing, or is naturally a no-op the second time (enabling an API,
# granting an IAM binding, applying a lifecycle policy).
#
# No credentials of any kind live in this file. It runs entirely as
# whoever invokes it -- your own `gcloud auth login` session, or a CI
# job's own workload identity -- and everything it grants (bucket access,
# invoker permission) it grants to a *service account for the app*, never
# to the person running the script. If BRAVE_API_KEY is set in your shell
# when you run this, its value goes straight into Secret Manager and is
# never written to disk or echoed.
#
# Requires: gcloud, already authenticated (`gcloud auth login`) as an
# identity with permission to create these resources in PROJECT_ID.
#
# Usage:
#   PROJECT_ID=your-project ./deploy/deploy.sh
#
# All other settings are optional and have defaults -- see the CONFIG
# section below, or deploy/README.md for the full list and what each one
# does.

set -euo pipefail

# --------------------------------------------------------------------------
# CONFIG -- override any of these by setting the same-named env var before
# running this script. Only PROJECT_ID has no default.
# --------------------------------------------------------------------------
: "${PROJECT_ID:?Set PROJECT_ID to your GCP project id, e.g. PROJECT_ID=my-project ./deploy/deploy.sh}"

REGION="${REGION:-asia-south1}"
BUCKET="${BUCKET:-ai-signal-feed}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-ai-signal-runner}"
REPO_NAME="${REPO_NAME:-ai-signal}"
IMAGE_NAME="${IMAGE_NAME:-ai-signal}"
SERVICE_NAME="${SERVICE_NAME:-ai-signal-web}"
JOB_NAME="${JOB_NAME:-ai-signal-sweep}"
SCHEDULER_NAME="${SCHEDULER_NAME:-ai-signal-daily}"
SCHEDULE="${SCHEDULE:-0 6 * * *}"
TIME_ZONE="${TIME_ZONE:-Asia/Kolkata}"
JOB_TASK_TIMEOUT="${JOB_TASK_TIMEOUT:-30m}"
SERVICE_REQUEST_TIMEOUT="${SERVICE_REQUEST_TIMEOUT:-900}"
SERVICE_MEMORY="${SERVICE_MEMORY:-1Gi}"

# Whether the web app is reachable without a Google login. Left private
# (false) by default: /api/watchlist and /api/refresh have no auth check
# of their own (see ai_signal/live/local_server.py), so a public service
# would let anyone on the internet overwrite the watchlist or trigger
# sweeps. Set ALLOW_UNAUTHENTICATED=true only once you have put something
# else in front of it (e.g. Identity-Aware Proxy) or accept that risk.
ALLOW_UNAUTHENTICATED="${ALLOW_UNAUTHENTICATED:-false}"

# Off by default. Google's News RSS endpoint states it is for personal,
# non-commercial use, and independent of that has been observed going
# through hours-long full-block windows (every query failing with
# HTTP 503) -- see ai_signal/live/google_news.py's module docstring
# and the AI_SIGNAL_GOOGLE_NEWS entry in .env.example. Set to "true" only
# once that trade-off has been made deliberately for this deployment.
ENABLE_GOOGLE_NEWS="${ENABLE_GOOGLE_NEWS:-false}"

# Optional. If set, stored in Secret Manager and mounted as an env var on
# both the service and the job -- never written into this script, an
# image layer, or plain Cloud Run env var config.
BRAVE_API_KEY="${BRAVE_API_KEY:-}"

SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${IMAGE_NAME}:latest"
SECRET_NAME="ai-signal-brave-api-key"

log() { printf '\n==> %s\n' "$1"; }

# --------------------------------------------------------------------------
# 0. Preflight
# --------------------------------------------------------------------------
log "Checking gcloud is installed and you're logged in"
command -v gcloud >/dev/null 2>&1 || { echo "gcloud CLI not found. Install it: https://cloud.google.com/sdk/docs/install"; exit 1; }

if [ -z "$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null)" ]; then
  echo "No active gcloud account. Run: gcloud auth login"
  exit 1
fi
ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)')"
echo "Running as: ${ACTIVE_ACCOUNT}"
echo "Project:    ${PROJECT_ID}"
echo "Region:     ${REGION}"

gcloud config set project "${PROJECT_ID}" >/dev/null

# --------------------------------------------------------------------------
# 1. APIs -- enabling an already-enabled API is a no-op.
# --------------------------------------------------------------------------
log "Enabling required APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  --project="${PROJECT_ID}"

# --------------------------------------------------------------------------
# 2. Artifact Registry repository
# --------------------------------------------------------------------------
log "Artifact Registry repository (${REPO_NAME})"
if gcloud artifacts repositories describe "${REPO_NAME}" \
    --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "Already exists, leaving as is."
else
  gcloud artifacts repositories create "${REPO_NAME}" \
    --repository-format=docker --location="${REGION}" --project="${PROJECT_ID}"
fi

# --------------------------------------------------------------------------
# 3. Service account the app runs as
# --------------------------------------------------------------------------
log "Service account (${SA_EMAIL})"
if gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "Already exists, leaving as is."
else
  gcloud iam service-accounts create "${SERVICE_ACCOUNT_NAME}" \
    --display-name="AISignal runner" --project="${PROJECT_ID}"
fi

# --------------------------------------------------------------------------
# 4. Bucket -- watchlist, archive, logs. Created only if missing; the
#    lifecycle policy and IAM binding are re-applied every run regardless,
#    since both are naturally idempotent (they replace/no-op, not append).
# --------------------------------------------------------------------------
log "Bucket (gs://${BUCKET})"
if gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  echo "Already exists, leaving as is."
else
  gcloud storage buckets create "gs://${BUCKET}" \
    --project="${PROJECT_ID}" --location="${REGION}" \
    --uniform-bucket-level-access
fi

log "Applying retention policy (deploy/lifecycle.json -- archive/ and logs/ only, 30 days)"
gcloud storage buckets update "gs://${BUCKET}" \
  --lifecycle-file="$(dirname "$0")/lifecycle.json" --project="${PROJECT_ID}"

log "Granting ${SA_EMAIL} object access on gs://${BUCKET}"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin" --project="${PROJECT_ID}" >/dev/null

# --------------------------------------------------------------------------
# 5. Optional secret (Brave Search key)
# --------------------------------------------------------------------------
SECRET_FLAG=()
if [ -n "${BRAVE_API_KEY}" ]; then
  log "Storing BRAVE_API_KEY in Secret Manager (${SECRET_NAME})"
  if gcloud secrets describe "${SECRET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    printf '%s' "${BRAVE_API_KEY}" | gcloud secrets versions add "${SECRET_NAME}" \
      --data-file=- --project="${PROJECT_ID}" >/dev/null
  else
    printf '%s' "${BRAVE_API_KEY}" | gcloud secrets create "${SECRET_NAME}" \
      --data-file=- --replication-policy=automatic --project="${PROJECT_ID}" >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "${SECRET_NAME}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" --project="${PROJECT_ID}" >/dev/null
  SECRET_FLAG=(--set-secrets="BRAVE_API_KEY=${SECRET_NAME}:latest")
else
  echo "(BRAVE_API_KEY not set in your shell -- skipping Secret Manager step; unset on the service/job too)"
fi

# --------------------------------------------------------------------------
# 6. Build the image. Re-buildable by design: this always builds and
#    pushes ":latest" fresh from the current source tree.
# --------------------------------------------------------------------------
log "Building and pushing ${IMAGE}"
gcloud builds submit --tag "${IMAGE}" --project="${PROJECT_ID}" "$(dirname "$0")/.."

# --------------------------------------------------------------------------
# 7. Env vars shared by the service and the job
# --------------------------------------------------------------------------
ENV_VARS="AI_SIGNAL_STORAGE=gcs,AI_SIGNAL_BUCKET=${BUCKET}"
if [ "${ENABLE_GOOGLE_NEWS}" = "true" ]; then
  ENV_VARS="${ENV_VARS},AI_SIGNAL_GOOGLE_NEWS=1"
fi

# --------------------------------------------------------------------------
# 8. Web service -- `run deploy` is itself create-or-update.
# --------------------------------------------------------------------------
log "Deploying web service (${SERVICE_NAME})"
AUTH_FLAG="--no-allow-unauthenticated"
[ "${ALLOW_UNAUTHENTICATED}" = "true" ] && AUTH_FLAG="--allow-unauthenticated"

gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --service-account="${SA_EMAIL}" \
  --set-env-vars="${ENV_VARS}" \
  ${SECRET_FLAG[@]+"${SECRET_FLAG[@]}"} \
  --timeout="${SERVICE_REQUEST_TIMEOUT}" \
  --memory="${SERVICE_MEMORY}" \
  ${AUTH_FLAG}

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')"

# --------------------------------------------------------------------------
# 9. Daily sweep job -- `run jobs deploy` is also create-or-update.
# --------------------------------------------------------------------------
log "Deploying sweep job (${JOB_NAME})"
gcloud run jobs deploy "${JOB_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --service-account="${SA_EMAIL}" \
  --set-env-vars="${ENV_VARS}" \
  ${SECRET_FLAG[@]+"${SECRET_FLAG[@]}"} \
  --command=python \
  --args=sweep_runner.py \
  --task-timeout="${JOB_TASK_TIMEOUT}" \
  --max-retries=1

log "Letting ${SA_EMAIL} invoke ${JOB_NAME} (needed for the scheduler call below)"
gcloud run jobs add-iam-policy-binding "${JOB_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/run.invoker" >/dev/null

# --------------------------------------------------------------------------
# 10. Scheduler -- no unified "deploy" verb here, so check then branch.
# --------------------------------------------------------------------------
JOB_RUN_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"

log "Scheduling ${JOB_NAME} (${SCHEDULE}, ${TIME_ZONE})"
if gcloud scheduler jobs describe "${SCHEDULER_NAME}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${SCHEDULER_NAME}" \
    --location="${REGION}" --project="${PROJECT_ID}" \
    --schedule="${SCHEDULE}" --time-zone="${TIME_ZONE}" \
    --uri="${JOB_RUN_URI}" --http-method=POST \
    --oauth-service-account-email="${SA_EMAIL}"
else
  gcloud scheduler jobs create http "${SCHEDULER_NAME}" \
    --location="${REGION}" --project="${PROJECT_ID}" \
    --schedule="${SCHEDULE}" --time-zone="${TIME_ZONE}" \
    --uri="${JOB_RUN_URI}" --http-method=POST \
    --oauth-service-account-email="${SA_EMAIL}"
fi

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------
log "Done"
cat <<SUMMARY

  Web app:     ${SERVICE_URL}
               $( [ "${ALLOW_UNAUTHENTICATED}" = "true" ] && echo "(public)" || echo "(private -- grant access with the command below)" )
  Sweep job:   ${JOB_NAME}  (region ${REGION})
  Schedule:    "${SCHEDULE}" ${TIME_ZONE}, via ${SCHEDULER_NAME}
  Bucket:      gs://${BUCKET}
  Google News: ${ENABLE_GOOGLE_NEWS}

  Try the schedule right now, without waiting for it to fire:
    gcloud scheduler jobs run ${SCHEDULER_NAME} --location=${REGION} --project=${PROJECT_ID}
    gcloud storage ls gs://${BUCKET}/archive/

  Grant a teammate access to the private web app:
    gcloud run services add-iam-policy-binding ${SERVICE_NAME} \\
      --region=${REGION} --project=${PROJECT_ID} \\
      --member=user:THEIR_EMAIL --role=roles/run.invoker

SUMMARY
