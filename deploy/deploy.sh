#!/usr/bin/env bash
set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
PROJECT_ID="${GCP_PROJECT_ID:-neusis-platform}"
REGION="${GCP_REGION:-us-central1}"
CLUSTER_NAME="${GKE_CLUSTER_NAME:-attendee}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || echo latest)}"

REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/attendee"
IMAGE="${REGISTRY}/attendee:${IMAGE_TAG}"

echo "=== Attendee GKE Deployment ==="
echo "Project:  ${PROJECT_ID}"
echo "Region:   ${REGION}"
echo "Cluster:  ${CLUSTER_NAME}"
echo "Image:    ${IMAGE}"
echo ""

# ── Step 1: Build & Push Docker Image ────────────────────────────────────────
echo ">> Step 1: Building and pushing Docker image..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

docker build --platform linux/amd64 -t "${IMAGE}" .
docker push "${IMAGE}"

echo "   Image pushed: ${IMAGE}"

# ── Step 2: Connect to GKE ───────────────────────────────────────────────────
echo ">> Step 2: Connecting to GKE cluster..."
gcloud container clusters get-credentials "${CLUSTER_NAME}" \
  --region "${REGION}" \
  --project "${PROJECT_ID}"

# ── Step 3: Reserve Static IP (if not exists) ───────────────────────────────
echo ">> Step 3: Ensuring static IP exists..."
if ! gcloud compute addresses describe attendee-ip --global --project "${PROJECT_ID}" &>/dev/null; then
  gcloud compute addresses create attendee-ip --global --project "${PROJECT_ID}"
fi
STATIC_IP=$(gcloud compute addresses describe attendee-ip --global --project "${PROJECT_ID}" --format='value(address)')
echo "   Static IP: ${STATIC_IP}"

# ── Step 4: Apply Kubernetes Manifests ───────────────────────────────────────
echo ">> Step 4: Applying Kubernetes manifests..."

K8S_DIR="$(cd "$(dirname "$0")/k8s" && pwd)"

# Substitute placeholders in manifests
apply_manifest() {
  sed \
    -e "s|REGION-docker.pkg.dev/PROJECT_ID/attendee/attendee|${REGISTRY}/attendee|g" \
    -e "s|PROJECT_ID|${PROJECT_ID}|g" \
    -e "s|REGION|${REGION}|g" \
    "$1" | kubectl apply -f -
}

apply_manifest "${K8S_DIR}/namespace.yaml"
apply_manifest "${K8S_DIR}/service-accounts.yaml"
apply_manifest "${K8S_DIR}/rbac.yaml"
apply_manifest "${K8S_DIR}/configmap.yaml"

# Only apply secret.yaml if secrets don't already exist (don't overwrite)
if ! kubectl get secret app-secrets -n attendee &>/dev/null; then
  echo ""
  echo "   !! Secret 'app-secrets' does not exist yet."
  echo "   Edit deploy/k8s/secret.yaml with your actual values, then run:"
  echo "     kubectl apply -f deploy/k8s/secret.yaml"
  echo ""
  echo "   You need at minimum:"
  echo "     - DATABASE_URL (from: terraform output -raw database_url)"
  echo "     - REDIS_URL (from: terraform output -raw redis_url)"
  echo "     - DJANGO_SECRET_KEY (generate with: python -c \"import secrets; print(secrets.token_urlsafe(50))\")"
  echo "     - CREDENTIALS_ENCRYPTION_KEY"
  echo "     - CUBER_RELEASE_VERSION: ${IMAGE_TAG}"
  echo ""
  echo "   Then re-run this script."
  exit 1
else
  echo "   Secret 'app-secrets' exists, continuing."
fi

apply_manifest "${K8S_DIR}/service.yaml"
apply_manifest "${K8S_DIR}/ingress.yaml"
apply_manifest "${K8S_DIR}/deployments.yaml"

# ── Step 5: Update Deployment Images ────────────────────────────────────────
echo ">> Step 5: Updating deployment images to ${IMAGE_TAG}..."
kubectl set image deployment/attendee-web \
  web="${IMAGE}" \
  collectstatic="${IMAGE}" \
  migrate="${IMAGE}" \
  -n attendee

kubectl set image deployment/attendee-worker \
  worker="${IMAGE}" \
  -n attendee

kubectl set image deployment/attendee-scheduler \
  scheduler="${IMAGE}" \
  -n attendee

# Update release version in secret
kubectl patch secret app-secrets -n attendee \
  --type merge -p "{\"stringData\":{\"CUBER_RELEASE_VERSION\":\"${IMAGE_TAG}\"}}"

# ── Step 6: Wait for Rollout ────────────────────────────────────────────────
echo ">> Step 6: Waiting for rollout..."
kubectl rollout status deployment/attendee-web -n attendee --timeout=300s
kubectl rollout status deployment/attendee-worker -n attendee --timeout=300s
kubectl rollout status deployment/attendee-scheduler -n attendee --timeout=300s

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Your app is accessible at: http://${STATIC_IP}"
echo ""
echo "Check status:"
echo "  kubectl get pods -n attendee"
echo "  kubectl get ingress -n attendee"
echo ""
echo "When you're ready to add a domain + SSL:"
echo "  1. Point your domain's A record to ${STATIC_IP}"
echo "  2. Uncomment the ManagedCertificate in deploy/k8s/ingress.yaml"
echo "  3. Re-run this script"
