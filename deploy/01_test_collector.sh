#!/usr/bin/env bash
# GATE STEP: build the collector with Cloud Build, run it ONCE as a Cloud Run Job,
# and report which platforms work from GCP. Does NOT create the schedule or dashboard.
source "$(dirname "$0")/config.sh"

echo "==> Building collector image with Cloud Build (no local Docker)"
gcloud builds submit "${ROOT_DIR}/collector" --tag "${COLLECTOR_IMAGE}" --timeout 1800s

echo "==> Creating/updating Cloud Run Job ${COLLECTOR_JOB}"
JOB_FLAGS=(--image "${COLLECTOR_IMAGE}" --region "${REGION}"
  --service-account "${COLLECTOR_SA}" --cpu 1 --memory 2Gi
  --task-timeout 900s --max-retries 0 --tasks 1)
if gcloud run jobs describe "${COLLECTOR_JOB}" --region "${REGION}" >/dev/null 2>&1; then
  gcloud run jobs update "${COLLECTOR_JOB}" "${JOB_FLAGS[@]}"
else
  gcloud run jobs create "${COLLECTOR_JOB}" "${JOB_FLAGS[@]}"
fi

echo "==> Executing once (waiting for completion)"
EXEC="$(gcloud run jobs execute "${COLLECTOR_JOB}" --region "${REGION}" --wait \
          --format 'value(metadata.name)' || true)"
if [[ -z "${EXEC}" ]]; then
  EXEC="$(gcloud run jobs executions list --job "${COLLECTOR_JOB}" --region "${REGION}" \
            --limit 1 --format 'value(metadata.name)')"
fi
echo "Execution: ${EXEC}"

echo "==> Reading results from Cloud Logging"
FILTER="resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"${COLLECTOR_JOB}\" AND labels.\"run.googleapis.com/execution_name\"=\"${EXEC}\""
SUMMARY=""
for _ in $(seq 1 12); do
  SUMMARY="$(gcloud logging read "${FILTER} AND textPayload:\"SUMMARY \"" --limit 1 \
               --format 'value(textPayload)' --freshness 1h || true)"
  [[ -n "${SUMMARY}" ]] && break
  sleep 10
done

echo
echo "---- method / status log lines ----"
gcloud logging read "${FILTER} AND textPayload:\"status=\"" --limit 20 --order asc \
  --format 'value(textPayload)' --freshness 1h || true
echo

if [[ -z "${SUMMARY}" ]]; then
  echo "No SUMMARY line found yet. Check logs:"
  echo "  gcloud logging read '${FILTER}' --limit 100"
  exit 1
fi

python3 - "${SUMMARY#SUMMARY }" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
f = lambda v: "n/a" if v is None else f"{v:,}"
print(f"{'platform':<10} {'status':<8} {'method':<20} {'views':>12} {'likes':>10} {'comments':>9} {'shares':>8}")
for p in d["posts"]:
    print(f"{p['platform']:<10} {p['status']:<8} {p['method']:<20} {f(p['views']):>12} {f(p['likes']):>10} {f(p['comments']):>9} {f(p['shares']):>8}")
    if p.get("errors"):
        for k, v in p["errors"].items():
            print(f"    {k}: {v}")
t = d["totals"]
print(f"\nTotals: views {t['views']:,} | likes {t['likes']:,} | comments {t['comments']:,} | "
      f"shares {t['shares']:,} | ALL INTERACTIONS (likes+comments+shares) {t['interactions']:,}")
blocked = [p["platform"] for p in d["posts"] if p["status"] != "ok"]
if blocked:
    print(f"\n!! Not fully working from GCP: {', '.join(blocked)}. Review before running 02_deploy_all.sh.")
else:
    print("\nAll platforms OK from GCP. Next: ./deploy/02_deploy_all.sh")
PY
