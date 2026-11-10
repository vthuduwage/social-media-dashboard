#!/usr/bin/env bash
# One-time project setup: APIs, Artifact Registry, Firestore, service accounts.
# Safe to re-run.
source "$(dirname "$0")/config.sh"

echo "==> Enabling APIs in ${PROJECT_ID}"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com cloudscheduler.googleapis.com logging.googleapis.com \
  iam.googleapis.com storage.googleapis.com

echo "==> Artifact Registry repo ${AR_REPO}"
gcloud artifacts repositories describe "${AR_REPO}" --location "${REGION}" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${AR_REPO}" --location "${REGION}" \
    --repository-format docker --description "Social post analytics images"
# Keep only the newest version of each image so storage stays near the 0.5 GB free tier.
cat > /tmp/ar-cleanup.json <<'JSON'
[
  {"name": "keep-latest-1", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 1}},
  {"name": "delete-rest", "action": {"type": "Delete"}, "condition": {"tagState": "ANY"}}
]
JSON
gcloud artifacts repositories set-cleanup-policies "${AR_REPO}" --location "${REGION}" \
  --policy /tmp/ar-cleanup.json --no-dry-run >/dev/null

echo "==> Build staging bucket ${BUILD_BUCKET} (us-central1, objects auto-deleted after 1 day)"
gcloud storage buckets describe "${BUILD_BUCKET}" >/dev/null 2>&1 || \
  gcloud storage buckets create "${BUILD_BUCKET}" --location "${REGION}" --uniform-bucket-level-access
echo '{"rule":[{"action":{"type":"Delete"},"condition":{"age":1}}]}' > /tmp/build-lifecycle.json
gcloud storage buckets update "${BUILD_BUCKET}" --lifecycle-file /tmp/build-lifecycle.json >/dev/null

echo "==> Firestore (default) database (Native mode, free tier)"
gcloud firestore databases describe --database="(default)" >/dev/null 2>&1 || \
  gcloud firestore databases create --database="(default)" --location "${FIRESTORE_LOCATION}" --type firestore-native

echo "==> Service accounts"
for sa in collector-sa dashboard-sa scheduler-sa; do
  gcloud iam service-accounts describe "${sa}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "${sa}" --display-name "${sa}"
done
gcloud projects add-iam-policy-binding "${PROJECT_ID}" --condition=None \
  --member "serviceAccount:${COLLECTOR_SA}" --role roles/datastore.user >/dev/null
gcloud projects add-iam-policy-binding "${PROJECT_ID}" --condition=None \
  --member "serviceAccount:${DASHBOARD_SA}" --role roles/datastore.viewer >/dev/null

echo "==> Cloud Build service account permissions"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format 'value(projectNumber)')"
for sa in "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" "${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"; do
  for role in roles/artifactregistry.writer roles/logging.logWriter roles/storage.objectViewer; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" --condition=None \
      --member "serviceAccount:${sa}" --role "${role}" >/dev/null 2>&1 || true
  done
done

echo "Setup complete."
