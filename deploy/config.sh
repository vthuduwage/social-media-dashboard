#!/usr/bin/env bash
# Shared settings. Override any of these by exporting them before running a script.
set -euo pipefail

: "${PROJECT_ID:=$(gcloud config get-value project 2>/dev/null)}"
if [[ -z "${PROJECT_ID}" ]]; then
  echo "Set PROJECT_ID (export PROJECT_ID=my-project) or run: gcloud config set project <id>" >&2
  exit 1
fi

# us-central1 is a Tier-1 region (cheapest Cloud Run pricing, Always Free eligible).
: "${REGION:=us-central1}"
: "${FIRESTORE_LOCATION:=us-central1}"
: "${AR_REPO:=social-analytics}"
: "${COLLECTOR_JOB:=post-collector}"
: "${DASHBOARD_SERVICE:=post-dashboard}"
: "${SCHEDULER_JOB:=post-collector-every-30m}"
: "${SCHEDULE:=*/30 * * * *}"
: "${TIME_ZONE:=Etc/UTC}"

# Regional (us-central1) bucket for Cloud Build source/logs, so it sits inside the
# Cloud Storage free tier instead of the default multi-region bucket.
BUILD_BUCKET="gs://${PROJECT_ID}-build-src"
BUILD_FLAGS=(--gcs-source-staging-dir "${BUILD_BUCKET}/source" --gcs-log-dir "${BUILD_BUCKET}/logs")

COLLECTOR_SA="collector-sa@${PROJECT_ID}.iam.gserviceaccount.com"
DASHBOARD_SA="dashboard-sa@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="scheduler-sa@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"
COLLECTOR_IMAGE="${IMAGE_BASE}/collector:latest"
DASHBOARD_IMAGE="${IMAGE_BASE}/dashboard:latest"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
gcloud config set project "${PROJECT_ID}" >/dev/null 2>&1
