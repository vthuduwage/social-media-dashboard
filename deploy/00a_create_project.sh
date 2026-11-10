#!/usr/bin/env bash
# Create a NEW, dedicated GCP project, link billing (Cloud Run requires a billing
# account even when usage stays in the free tier) and add a $1 budget alert.
# Usage:
#   PROJECT_ID=post-analytics-xxxx BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX ./deploy/00a_create_project.sh
set -euo pipefail
: "${PROJECT_ID:?set PROJECT_ID (6-30 chars, lowercase, globally unique)}"
: "${BILLING_ACCOUNT:?set BILLING_ACCOUNT (gcloud billing accounts list)}"
: "${BUDGET_AMOUNT:=1}"

if ! gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "==> Creating project ${PROJECT_ID}"
  gcloud projects create "${PROJECT_ID}" --name "Post Analytics"
fi
gcloud config set project "${PROJECT_ID}" >/dev/null

echo "==> Linking billing account ${BILLING_ACCOUNT}"
gcloud billing projects link "${PROJECT_ID}" --billing-account "${BILLING_ACCOUNT}"

echo "==> Budget alert at \$${BUDGET_AMOUNT} (emails billing admins at 50/90/100%)"
gcloud services enable billingbudgets.googleapis.com --project "${PROJECT_ID}"
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format 'value(projectNumber)')"
if ! gcloud billing budgets list --billing-account "${BILLING_ACCOUNT}" \
      --format 'value(displayName)' 2>/dev/null | grep -qx "post-analytics-${PROJECT_ID}"; then
  gcloud billing budgets create --billing-account "${BILLING_ACCOUNT}" \
    --display-name "post-analytics-${PROJECT_ID}" \
    --budget-amount "${BUDGET_AMOUNT}USD" \
    --filter-projects "projects/${PROJECT_NUMBER}" \
    --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0
fi
echo "Project ready: ${PROJECT_ID}"
