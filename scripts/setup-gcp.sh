#!/usr/bin/env bash
# One-time GCP setup for Claven.
# Run once after creating your GCP project.
#
# Usage:
#   ./scripts/setup-gcp.sh <project-id>
#
# Example:
#   ./scripts/setup-gcp.sh claven-prod
#
# After this runs it prints the exact values to paste into GitHub secrets.

set -euo pipefail

PROJECT_ID="${1:?Usage: ./scripts/setup-gcp.sh <project-id>}"
GITHUB_REPO="manubansal/claven"
REGION="us-central1"
REGISTRY="claven"
SA_NAME="claven-deploy"
RUN_SA_NAME="claven-run"
POOL="github-pool"
PROVIDER="github-provider"

echo ""
echo "==> Creating project: $PROJECT_ID"
gcloud projects create "$PROJECT_ID" --name="Claven" 2>/dev/null \
  || echo "    (project already exists, continuing)"

gcloud config set project "$PROJECT_ID"

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
RUN_SA_EMAIL="${RUN_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# ── APIs ──────────────────────────────────────────────────────────────────────
echo ""
echo "==> Enabling APIs (this takes ~60 seconds)..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  pubsub.googleapis.com \
  cloudscheduler.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  cloudresourcemanager.googleapis.com

# ── Artifact Registry ─────────────────────────────────────────────────────────
echo ""
echo "==> Creating Artifact Registry repository '$REGISTRY' in $REGION..."
gcloud artifacts repositories create "$REGISTRY" \
  --repository-format=docker \
  --location="$REGION" \
  --description="Claven container images" 2>/dev/null \
  || echo "    (repository already exists, continuing)"

# ── Service account ───────────────────────────────────────────────────────────
echo ""
echo "==> Creating service account '$SA_NAME'..."
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="Claven GitHub Deploy" 2>/dev/null \
  || echo "    (service account already exists, continuing)"

echo "==> Creating runtime service account '$RUN_SA_NAME'..."
gcloud iam service-accounts create "$RUN_SA_NAME" \
  --display-name="Claven Cloud Run runtime" 2>/dev/null \
  || echo "    (service account already exists, continuing)"

# Least privilege. The deployer pushes images, deploys the service as
# claven-run (and no other identity), and updates Scheduler jobs, reading only
# the secret those jobs send. Secrets, Pub/Sub and IAM are managed by hand.
echo "==> Granting IAM roles to $SA_EMAIL..."
for ROLE in \
  roles/run.developer \
  roles/artifactregistry.writer \
  roles/cloudscheduler.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$ROLE" \
    --condition=None \
    --quiet
done

gcloud iam service-accounts add-iam-policy-binding "$RUN_SA_EMAIL" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/iam.serviceAccountUser" --quiet

# ── Workload Identity Federation ──────────────────────────────────────────────
echo ""
echo "==> Creating Workload Identity Pool '$POOL'..."
gcloud iam workload-identity-pools create "$POOL" \
  --location="global" \
  --display-name="GitHub Actions" 2>/dev/null \
  || echo "    (pool already exists, continuing)"

echo "==> Creating OIDC provider '$PROVIDER'..."
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" \
  --location="global" \
  --workload-identity-pool="$POOL" \
  --display-name="GitHub Actions OIDC" \
  --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-condition="assertion.repository == '${GITHUB_REPO}' && assertion.environment == 'production'" 2>/dev/null \
  || echo "    (provider already exists, continuing)"

echo "==> Binding service account to pool (repo: $GITHUB_REPO)..."
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GITHUB_REPO}"

# ── Secrets ───────────────────────────────────────────────────────────────────
# These are placeholder secrets — update their values via:
#   echo -n "actual-value" | gcloud secrets versions add SECRET_NAME --data-file=-
echo ""
echo "==> Creating Secret Manager secrets (empty — update values separately)..."
for SECRET in database-url database-url-direct token-encryption-key oauth-client-secret internal-api-secret session-secret; do
  gcloud secrets create "$SECRET" \
    --replication-policy="automatic" 2>/dev/null \
    || echo "    (secret '$SECRET' already exists, continuing)"
  # The runtime reads each of its secrets; nothing else gets project-wide access.
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:${RUN_SA_EMAIL}" --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
done
gcloud secrets add-iam-policy-binding internal-api-secret \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/secretmanager.secretAccessor" --quiet >/dev/null

# ── Output ────────────────────────────────────────────────────────────────────
PROVIDER_FULL="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
ENCRYPTION_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REGISTRY}/server"

echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  Add these to GitHub → Settings → Secrets → Actions             ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║"
echo "║  GCP_PROJECT_ID"
echo "║    $PROJECT_ID"
echo "║"
echo "║  GCP_WORKLOAD_IDENTITY_PROVIDER"
echo "║    $PROVIDER_FULL"
echo "║"
echo "║  GCP_SERVICE_ACCOUNT"
echo "║    $SA_EMAIL"
echo "║"
echo "║  TOKEN_ENCRYPTION_KEY  (generated — save this somewhere safe)"
echo "║    $ENCRYPTION_KEY"
echo "║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  Add after Neon setup:                                           ║"
echo "║                                                                  ║"
echo "║  DATABASE_URL  (pooled connection string from Neon console)      ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "==> Also store secrets in Secret Manager for Cloud Run to consume:"
echo ""
echo "    echo -n '<neon-pooled-url>' | gcloud secrets versions add database-url --data-file=-"
echo "    echo -n '$ENCRYPTION_KEY'   | gcloud secrets versions add token-encryption-key --data-file=-"
echo ""
echo "==> IMPORTANT: Enable billing at"
echo "    https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT_ID"
echo ""
echo "Done. Container image base: $IMAGE_BASE"
