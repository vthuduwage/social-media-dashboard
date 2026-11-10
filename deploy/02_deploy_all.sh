#!/usr/bin/env bash
# Deploy the dashboard (public URL) and the 30-minute Cloud Scheduler trigger.
# Run only after 01_test_collector.sh looks good.
source "$(dirname "$0")/config.sh"

echo "==> Building dashboard image with Cloud Build"
gcloud builds submit "${ROOT_DIR}/dashboard" --tag "${DASHBOARD_IMAGE}"

echo "==> Deploying Cloud Run service ${DASHBOARD_SERVICE} (public, read-only)"
gcloud run deploy "${DASHBOARD_SERVICE}" --image "${DASHBOARD_IMAGE}" --region "${REGION}" \
  --service-account "${DASHBOARD_SA}" --allow-unauthenticated \
  --cpu 1 --memory 256Mi --min-instances 0 --max-instances 2 --concurrency 80

echo "==> Allowing scheduler-sa to run the collector job"
gcloud run jobs add-iam-policy-binding "${COLLECTOR_JOB}" --region "${REGION}" \
  --member "serviceAccount:${SCHEDULER_SA}" --role roles/run.invoker >/dev/null

echo "==> Cloud Scheduler job ${SCHEDULER_JOB} (${SCHEDULE})"
RUN_URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${COLLECTOR_JOB}:run"
SCHED_FLAGS=(--location "${REGION}" --schedule "${SCHEDULE}" --time-zone "${TIME_ZONE}"
  --uri "${RUN_URI}" --http-method POST
  --oauth-service-account-email "${SCHEDULER_SA}"
  --oauth-token-scope "https://www.googleapis.com/auth/cloud-platform")
if gcloud scheduler jobs describe "${SCHEDULER_JOB}" --location "${REGION}" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "${SCHEDULER_JOB}" "${SCHED_FLAGS[@]}"
else
  gcloud scheduler jobs create http "${SCHEDULER_JOB}" "${SCHED_FLAGS[@]}"
fi

URL="$(gcloud run services describe "${DASHBOARD_SERVICE}" --region "${REGION}" --format 'value(status.url)')"
echo
echo "Dashboard: ${URL}"
echo "Collector runs every 30 minutes. Trigger now with:"
echo "  gcloud scheduler jobs run ${SCHEDULER_JOB} --location ${REGION}"
