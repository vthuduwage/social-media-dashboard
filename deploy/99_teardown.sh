#!/usr/bin/env bash
# Remove the scheduler, job, service and images. Firestore data is kept.
source "$(dirname "$0")/config.sh"
gcloud scheduler jobs delete "${SCHEDULER_JOB}" --location "${REGION}" --quiet || true
gcloud run services delete "${DASHBOARD_SERVICE}" --region "${REGION}" --quiet || true
gcloud run jobs delete "${COLLECTOR_JOB}" --region "${REGION}" --quiet || true
gcloud artifacts repositories delete "${AR_REPO}" --location "${REGION}" --quiet || true
echo "Done. Firestore 'snapshots' collection left in place."
