#!/bin/bash
# deploy.sh — Build, push, and deploy VoiceScope to Google Cloud Run.
#
# Usage:
#   ./deploy.sh <GCP_PROJECT_ID>
#
# Prerequisites:
#   - gcloud CLI installed and authenticated (gcloud auth login)
#   - Docker installed and running
#   - .env file with all required variables (see .env.example)
#
# The script will:
#   1. Build the Docker image locally
#   2. Push it to Google Container Registry
#   3. Deploy to Cloud Run (us-central1, allow unauthenticated)
#   4. Print the live service URL

set -euo pipefail

SERVICE_NAME="voicescope"
REGION="us-central1"
IMAGE_TAG="latest"

# ── Validate arguments ─────────────────────────────────────────────────

if [ $# -lt 1 ]; then
    echo "Usage: ./deploy.sh <GCP_PROJECT_ID>"
    echo ""
    echo "Example: ./deploy.sh my-hackathon-project-123"
    exit 1
fi

PROJECT_ID="$1"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:${IMAGE_TAG}"

# ── Check prerequisites ───────────────────────────────────────────────

if ! command -v gcloud &> /dev/null; then
    echo "ERROR: gcloud CLI not found."
    echo ""
    echo "Manual steps (run these yourself):"
    echo "  1. Install gcloud: https://cloud.google.com/sdk/docs/install"
    echo "  2. gcloud auth login"
    echo "  3. gcloud auth configure-docker"
    echo "  4. docker build -t ${IMAGE} ."
    echo "  5. docker push ${IMAGE}"
    echo "  6. gcloud run deploy ${SERVICE_NAME} \\"
    echo "       --image ${IMAGE} \\"
    echo "       --region ${REGION} \\"
    echo "       --platform managed \\"
    echo "       --allow-unauthenticated \\"
    echo "       --port 8080 \\"
    echo "       --memory 512Mi \\"
    echo "       --project ${PROJECT_ID}"
    exit 1
fi

if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker not found. Install Docker Desktop and try again."
    exit 1
fi

# ── Load env vars from .env if present ─────────────────────────────────

ENV_VARS=""
if [ -f .env ]; then
    echo "Loading environment variables from .env..."
    # Read each KEY=VALUE line, skip comments and blank lines
    while IFS='=' read -r key value; do
        # Skip comments and empty lines
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        # Strip surrounding quotes from value
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"
        if [ -n "$ENV_VARS" ]; then
            ENV_VARS="${ENV_VARS},${key}=${value}"
        else
            ENV_VARS="${key}=${value}"
        fi
    done < .env
fi

# ── Build ──────────────────────────────────────────────────────────────

echo ""
echo "=== Building Docker image ==="
echo "Image: ${IMAGE}"
docker build -t "${IMAGE}" .

# ── Push ───────────────────────────────────────────────────────────────

echo ""
echo "=== Configuring Docker for GCR ==="
gcloud auth configure-docker --quiet --project "${PROJECT_ID}"

echo ""
echo "=== Pushing to Container Registry ==="
docker push "${IMAGE}"

# ── Deploy ─────────────────────────────────────────────────────────────

echo ""
echo "=== Deploying to Cloud Run ==="

DEPLOY_CMD=(
    gcloud run deploy "${SERVICE_NAME}"
    --image "${IMAGE}"
    --region "${REGION}"
    --platform managed
    --allow-unauthenticated
    --port 8080
    --memory 512Mi
    --project "${PROJECT_ID}"
)

if [ -n "$ENV_VARS" ]; then
    DEPLOY_CMD+=(--set-env-vars "${ENV_VARS}")
fi

"${DEPLOY_CMD[@]}"

# ── Print the service URL ──────────────────────────────────────────────

echo ""
echo "=== Deployment complete ==="
SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
    --region "${REGION}" \
    --platform managed \
    --project "${PROJECT_ID}" \
    --format 'value(status.url)')

echo ""
echo "VoiceScope is live at: ${SERVICE_URL}"
echo ""
echo "Next steps:"
echo "  1. Set WEBHOOK_URL in your .env to: ${SERVICE_URL}"
echo "  2. Redeploy if WEBHOOK_URL changed (Patter needs it for Twilio callbacks)"
echo "  3. Open ${SERVICE_URL} in a browser to test the UI"
