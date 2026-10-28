#!/usr/bin/env bash
# Deploy to Google Cloud Run as a public MCP endpoint (https://<service-url>/mcp).
#
#   PROJECT=my-gcp-project ./deploy.sh
#
# The schedule cache lives in a Cloud Storage bucket mounted at /data, so the GTFS feed
# is downloaded at most once a day no matter how often instances start. max-instances=1
# keeps the SEPTA API cache and rate limit (realtime.py) global to the whole service;
# one instance handles many concurrent requests, and it scales to zero when idle.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to your GCP project id}"
REGION="${REGION:-us-east4}"
SERVICE="${SERVICE:-septa-mcp}"
BUCKET="${BUCKET:-${PROJECT}-septa-mcp-cache}"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project "$PROJECT"

if ! gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$BUCKET" --project "$PROJECT" --location "$REGION" \
    --uniform-bucket-level-access
fi
# Cloud Run runs as the default compute service account unless told otherwise.
SA="$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')-compute@developer.gserviceaccount.com"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$SA" --role roles/storage.objectUser >/dev/null

gcloud run deploy "$SERVICE" \
  --project "$PROJECT" --region "$REGION" --source . \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 1 --concurrency 80 \
  --cpu 1 --memory 512Mi --timeout 60 \
  --execution-environment gen2 \
  --add-volume "name=cache,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount "volume=cache,mount-path=/data"

echo "MCP endpoint: $(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format 'value(status.url)')/mcp"
