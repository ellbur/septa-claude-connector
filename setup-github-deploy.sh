#!/usr/bin/env bash
# One-time setup for .github/workflows/deploy.yml: lets GitHub Actions runs from this repo's
# deploy branch act as a deployer service account through Workload Identity Federation (no
# keys). Run it once, signed in as a project owner (Cloud Shell works):
#
#   PROJECT=my-gcp-project ./setup-github-deploy.sh
#
# then set the repository variables it prints.
set -euo pipefail

PROJECT="${PROJECT:?set PROJECT to your GCP project id}"
REPO="${REPO:-ellbur/septa-claude-connector}"
BRANCH="${BRANCH:-deploy}"
POOL="${POOL:-github}"
PROVIDER="${PROVIDER:-github}"
DEPLOYER="${DEPLOYER:-github-deployer}"

gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com --project "$PROJECT"

NUMBER="$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')"
SA="$DEPLOYER@$PROJECT.iam.gserviceaccount.com"
COMPUTE_SA="$NUMBER-compute@developer.gserviceaccount.com"

if ! gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$DEPLOYER" --project "$PROJECT" \
    --display-name "GitHub Actions deployer ($REPO)"
fi

# What deploy.sh does: enable APIs, create the cache bucket, deploy the service and the refresh
# job from source, and schedule the job.
for ROLE in roles/viewer roles/serviceusage.serviceUsageAdmin roles/storage.admin \
  roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.admin \
  roles/cloudscheduler.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" \
    --role "$ROLE" --condition None >/dev/null
done
# The service, the job and the scheduler all run as the default compute service account.
gcloud iam service-accounts add-iam-policy-binding "$COMPUTE_SA" --project "$PROJECT" \
  --member "serviceAccount:$SA" --role roles/iam.serviceAccountUser >/dev/null

if ! gcloud iam workload-identity-pools describe "$POOL" --project "$PROJECT" \
  --location global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL" --project "$PROJECT" --location global \
    --display-name "GitHub Actions"
fi
# Only runs on this repository's deploy branch can sign in.
CONDITION="assertion.repository == '$REPO' && assertion.ref == 'refs/heads/$BRANCH'"
if ! gcloud iam workload-identity-pools providers describe "$PROVIDER" --project "$PROJECT" \
  --location global --workload-identity-pool "$POOL" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --project "$PROJECT" \
    --location global --workload-identity-pool "$POOL" \
    --display-name "GitHub" --issuer-uri https://token.actions.githubusercontent.com \
    --attribute-mapping 'google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref' \
    --attribute-condition "$CONDITION"
else
  gcloud iam workload-identity-pools providers update-oidc "$PROVIDER" --project "$PROJECT" \
    --location global --workload-identity-pool "$POOL" --attribute-condition "$CONDITION"
fi

gcloud iam service-accounts add-iam-policy-binding "$SA" --project "$PROJECT" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/attribute.repository/$REPO" \
  >/dev/null

echo "Set these repository variables (Settings > Secrets and variables > Actions > Variables):"
echo "  GCP_PROJECT=$PROJECT"
echo "  GCP_WORKLOAD_IDENTITY_PROVIDER=projects/$NUMBER/locations/global/workloadIdentityPools/$POOL/providers/$PROVIDER"
echo "  GCP_SERVICE_ACCOUNT=$SA"
echo "Then deploy with: git push origin HEAD:$BRANCH"
