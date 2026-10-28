#!/usr/bin/env bash
# Deploy to Google Cloud Run as a public MCP endpoint (https://<service-url>/mcp).
#
#   PROJECT=my-gcp-project ./deploy.sh
#
# The schedule cache lives in a Cloud Storage bucket mounted at /data, so the GTFS feeds
# survive cold starts. max-instances=1 keeps the SEPTA API cache and rate limit
# (realtime.py) global to the whole service; one instance handles many concurrent
# requests, and it scales to zero when idle.
#
# Timetables are refreshed by a Cloud Run job (septa-mcp --refresh-feeds) that Cloud
# Scheduler runs every Saturday night, writing to the same bucket; the service picks up
# the new files on its next request. The service only downloads feeds itself if the job
# hasn't run for 8 days, or the cached timetable has run out.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to your GCP project id}"
REGION="${REGION:-us-east4}"
SERVICE="${SERVICE:-septa-mcp}"
BUCKET="${BUCKET:-${PROJECT}-septa-mcp-cache}"
JOB="${JOB:-${SERVICE}-refresh}"
# Saturdays at 11:17 PM, Philadelphia time.
REFRESH_SCHEDULE="${REFRESH_SCHEDULE:-17 23 * * 6}"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com --project "$PROJECT"

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
  --cpu 1 --memory 1Gi --timeout 60 \
  --execution-environment gen2 \
  --set-env-vars SEPTA_GTFS_MAX_AGE_HOURS=192 \
  --add-volume "name=cache,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount "volume=cache,mount-path=/data"

# The refresh job runs the image just deployed.
IMAGE="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format 'value(spec.template.spec.containers[0].image)')"
gcloud run jobs deploy "$JOB" \
  --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --command septa-mcp --args=--refresh-feeds \
  --cpu 1 --memory 1Gi --task-timeout 15m --max-retries 2 \
  --execution-environment gen2 \
  --add-volume "name=cache,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount "volume=cache,mount-path=/data"

# Cloud Scheduler starts the job through the Cloud Run API, as the compute service account.
gcloud run jobs add-iam-policy-binding "$JOB" --project "$PROJECT" --region "$REGION" \
  --member "serviceAccount:$SA" --role roles/run.invoker >/dev/null
SCHEDULER_ARGS=(
  --project "$PROJECT" --location "$REGION"
  --schedule "$REFRESH_SCHEDULE" --time-zone America/New_York
  --uri "https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/$JOB:run"
  --http-method POST --oauth-service-account-email "$SA"
)
if gcloud scheduler jobs describe "$JOB" --project "$PROJECT" --location "$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$JOB" "${SCHEDULER_ARGS[@]}"
else
  gcloud scheduler jobs create http "$JOB" "${SCHEDULER_ARGS[@]}"
fi

echo "MCP endpoint: $(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" \
  --format 'value(status.url)')/mcp"
echo "Timetable refresh: '$REFRESH_SCHEDULE' (America/New_York). Run it now with:"
echo "  gcloud run jobs execute $JOB --project $PROJECT --region $REGION"
