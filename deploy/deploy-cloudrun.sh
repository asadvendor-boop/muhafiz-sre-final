#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════════
# MuhafizSRE — Cloud Run Deployment Script
# Single container (Gateway + Dashboard) with GCS FUSE for SQLite persistence
# ═══════════════════════════════════════════════════════════════════════════════
#
# Usage:
#   ./deploy/deploy-cloudrun.sh                    # Deploy with defaults
#   ./deploy/deploy-cloudrun.sh --project my-proj  # Specify project
#   ./deploy/deploy-cloudrun.sh --region asia-south1  # Specify region
#
# Prerequisites:
#   1. gcloud CLI installed and authenticated
#   2. Secrets created: gemini-api-key, muhafiz-secret-key
#   3. GCS bucket created for SQLite persistence
# ═══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Configuration ──
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="muhafizsre"
IMAGE_NAME="us-central1-docker.pkg.dev/${PROJECT_ID}/muhafiz/${SERVICE_NAME}"
BUCKET_NAME="${PROJECT_ID}-muhafiz-data"

# Parse args
while [[ $# -gt 0 ]]; do
  case $1 in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region)  REGION="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  MuhafizSRE — Cloud Run Deployment                         ║"
echo "║  Project:  ${PROJECT_ID}"
echo "║  Region:   ${REGION}"
echo "║  Service:  ${SERVICE_NAME}"
echo "║  Bucket:   ${BUCKET_NAME}"
echo "╚═══════════════════════════════════════════════════════════════╝"

# ── Step 1: Enable APIs ──
echo ""
echo "━━━ Step 1: Enabling APIs ━━━"
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  --project="${PROJECT_ID}" \
  --quiet

# ── Step 2: Create Artifact Registry (if not exists) ──
echo ""
echo "━━━ Step 2: Artifact Registry ━━━"
gcloud artifacts repositories describe muhafiz \
  --location="${REGION}" \
  --project="${PROJECT_ID}" 2>/dev/null || \
gcloud artifacts repositories create muhafiz \
  --repository-format=docker \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --description="MuhafizSRE container images"
echo "  ✓ Registry ready"

# ── Step 3: Create GCS Bucket for SQLite (if not exists) ──
echo ""
echo "━━━ Step 3: GCS Bucket for SQLite ━━━"
if gsutil ls "gs://${BUCKET_NAME}" 2>/dev/null; then
  echo "  ✓ Bucket exists"
else
  gsutil mb -l "${REGION}" -p "${PROJECT_ID}" "gs://${BUCKET_NAME}"
  echo "  ✓ Bucket created"
fi

# ── Step 4: Verify Secrets ──
echo ""
echo "━━━ Step 4: Checking Secrets ━━━"
for secret in gemini-api-key muhafiz-secret-key; do
  if gcloud secrets describe "${secret}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "  ✓ ${secret} exists"
  else
    echo "  ✗ ${secret} NOT FOUND — create it first:"
    echo "    echo -n 'YOUR_VALUE' | gcloud secrets create ${secret} --data-file=- --project=${PROJECT_ID}"
    exit 1
  fi
done

# ── Step 5: Build Container ──
echo ""
echo "━━━ Step 5: Building Container (Cloud Build) ━━━"
gcloud builds submit \
  --project="${PROJECT_ID}" \
  --config deploy/cloudbuild.yaml \
  --timeout=600s

# ── Step 6: Deploy to Cloud Run ──
echo ""
echo "━━━ Step 6: Deploying to Cloud Run ━━━"
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE_NAME}:latest" \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --platform managed \
  --execution-environment gen2 \
  --allow-unauthenticated \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,MUHAFIZ_APPROVAL_SECRET=muhafiz-secret-key:latest" \
  --set-env-vars "MUHAFIZ_EXECUTION_MODE=simulated,MUHAFIZ_DB_PATH=/data/muhafiz.db" \
  --add-volume "name=muhafiz-data,type=cloud-storage,bucket=${BUCKET_NAME}" \
  --add-volume-mount "volume=muhafiz-data,mount-path=/data" \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 1 \
  --timeout 300s \
  --port 3000

# ── Step 7: Get URL ──
echo ""
echo "━━━ Step 7: Deployment Complete ━━━"
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="value(status.url)")

echo ""
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║  ✅ MuhafizSRE Deployed Successfully!                      ║"
echo "║                                                             ║"
echo "║  Dashboard: ${SERVICE_URL}"
echo "║  API:       ${SERVICE_URL}/api/incidents"
echo "║  Health:    ${SERVICE_URL}/api/incidents"
echo "║  Benchmark: ${SERVICE_URL}/benchmark"
echo "║                                                             ║"
echo "║  Scale-to-zero: min-instances=0                             ║"
echo "║  SQLite:        GCS FUSE → gs://${BUCKET_NAME}"
echo "║  Cold start:    ~8-12s (both services)                      ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
