#!/usr/bin/env bash
# Set up Cloud Scheduler jobs for Claven.
# Idempotent — safe to run on every deploy.
#
# Required env vars:
#   INTERNAL_API_SECRET — Bearer token for /internal/* endpoints
#   GCP_PROJECT_ID — GCP project ID
#
# Usage:
#   ./scripts/setup-scheduler.sh

set -euo pipefail

REGION="us-central1"
API_BASE="https://api.claven.app"

if [ -z "${INTERNAL_API_SECRET:-}" ]; then
  echo "Error: INTERNAL_API_SECRET not set" >&2
  exit 1
fi

create_or_update_job() {
  local name="$1"
  local schedule="$2"
  local uri="$3"

  if gcloud scheduler jobs describe "$name" --location="$REGION" --project="${GCP_PROJECT_ID:-}" >/dev/null 2>&1; then
    echo "Updating existing job: $name"
    gcloud scheduler jobs update http "$name" \
      --location="$REGION" \
      --schedule="$schedule" \
      --uri="$uri" \
      --http-method=POST \
      --update-headers="Authorization=Bearer ${INTERNAL_API_SECRET}" \
      --time-zone="UTC" \
      --quiet
  else
    echo "Creating new job: $name"
    gcloud scheduler jobs create http "$name" \
      --location="$REGION" \
      --schedule="$schedule" \
      --uri="$uri" \
      --http-method=POST \
      --headers="Authorization=Bearer ${INTERNAL_API_SECRET}" \
      --time-zone="UTC" \
      --quiet
  fi
}

# Daily watch renewal at 6:00 AM UTC
create_or_update_job "claven-renew-watches" "0 6 * * *" "${API_BASE}/internal/renew-watches"

# Hourly poll fallback
create_or_update_job "claven-poll" "0 * * * *" "${API_BASE}/internal/poll"

create_or_update_job "claven-sunset" "*/15 * * * *" "${API_BASE}/internal/sunset"

echo "Scheduler jobs configured."
