#!/usr/bin/env bash
# Provision everything and deploy the job. Safe to re-run: each step is
# idempotent, so this doubles as the "push a new image version" script.

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_az
require_api_key
require_providers

echo "==> resource group: ${RESOURCE_GROUP} (${LOCATION})"
az group create \
  --name "${RESOURCE_GROUP}" \
  --location "${LOCATION}" \
  --output none

echo "==> container registry: ${ACR_NAME}"
if ! az acr show --name "${ACR_NAME}" --resource-group "${RESOURCE_GROUP}" --output none 2>/dev/null; then
  # admin-enabled keeps the sample to username/password auth. For production,
  # drop it and give the job a managed identity with the AcrPull role instead.
  az acr create \
    --name "${ACR_NAME}" \
    --resource-group "${RESOURCE_GROUP}" \
    --sku Basic \
    --admin-enabled true \
    --output none
fi

# Build in ACR rather than locally: no local Docker daemon needed, and the
# image is produced for linux/amd64 regardless of your laptop's architecture.
echo "==> building ${IMAGE} in ACR (this uploads the build context)"
az acr build \
  --registry "${ACR_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --image "${IMAGE_NAME}:${IMAGE_TAG}" \
  --platform linux/amd64 \
  --file Dockerfile \
  "${REPO_ROOT}"

ACR_USERNAME="$(az acr credential show --name "${ACR_NAME}" --query username --output tsv)"
ACR_PASSWORD="$(az acr credential show --name "${ACR_NAME}" --query 'passwords[0].value' --output tsv)"

echo "==> storage account: ${STORAGE_ACCOUNT_NAME}"
if ! az storage account show --name "${STORAGE_ACCOUNT_NAME}" --resource-group "${RESOURCE_GROUP}" --output none 2>/dev/null; then
  az storage account create \
    --name "${STORAGE_ACCOUNT_NAME}" \
    --resource-group "${RESOURCE_GROUP}" \
    --location "${LOCATION}" \
    --sku Standard_LRS \
    --output none
fi

STORAGE_CONNECTION_STRING="$(az storage account show-connection-string \
  --name "${STORAGE_ACCOUNT_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --query connectionString \
  --output tsv)"

# Idempotent on its own: succeeds whether or not the container already exists.
az storage container create \
  --name "${BLOB_CONTAINER}" \
  --connection-string "${STORAGE_CONNECTION_STRING}" \
  --output none

echo "==> container apps environment: ${ENVIRONMENT_NAME}"
if ! az containerapp env show --name "${ENVIRONMENT_NAME}" --resource-group "${RESOURCE_GROUP}" --output none 2>/dev/null; then
  az containerapp env create \
    --name "${ENVIRONMENT_NAME}" \
    --resource-group "${RESOURCE_GROUP}" \
    --location "${LOCATION}" \
    --output none
fi

# A manual job holds no state between executions, so re-deploying by deleting
# and recreating avoids the create/update flag differences in `az containerapp
# job update` (secrets and registry credentials are set through separate
# subcommands there). Past executions are discarded with the job.
if az containerapp job show --name "${JOB_NAME}" --resource-group "${RESOURCE_GROUP}" --output none 2>/dev/null; then
  echo "==> replacing existing job: ${JOB_NAME}"
  az containerapp job delete \
    --name "${JOB_NAME}" \
    --resource-group "${RESOURCE_GROUP}" \
    --yes \
    --output none
fi

# The API key and storage connection string are stored as Container Apps
# secrets and the env vars only hold secretrefs, so neither appears in the
# job's plaintext configuration.
JOB_ENV_VARS=(
  "ANTHROPIC_API_KEY=secretref:anthropic-api-key"
  "STORAGE_CONNECTION_STRING=secretref:storage-connection-string"
  "BLOB_CONTAINER=${BLOB_CONTAINER}"
  "TASK=${TASK}"
)
# Only set the repo variables for the task that uses them, and only pass MODEL
# and MAX_TURNS when overridden — otherwise the task's own defaults apply.
if [[ "${TASK}" == "code-review" || "${TASK}" == "explain-code" ]]; then
  JOB_ENV_VARS+=("TARGET_REPO_URL=${TARGET_REPO_URL}" "BASE_REF=${BASE_REF}" "HEAD_REF=${HEAD_REF}")
fi
[[ -n "${MODEL}" ]] && JOB_ENV_VARS+=("MODEL=${MODEL}")
[[ -n "${MAX_TURNS}" ]] && JOB_ENV_VARS+=("MAX_TURNS=${MAX_TURNS}")

echo "==> creating job: ${JOB_NAME} (task=${TASK})"
az containerapp job create \
  --name "${JOB_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --environment "${ENVIRONMENT_NAME}" \
  --trigger-type Manual \
  --parallelism 1 \
  --replica-completion-count 1 \
  --replica-timeout "${REPLICA_TIMEOUT}" \
  --replica-retry-limit 0 \
  --image "${IMAGE}" \
  --cpu "${JOB_CPU}" \
  --memory "${JOB_MEMORY}" \
  --registry-server "${ACR_NAME}.azurecr.io" \
  --registry-username "${ACR_USERNAME}" \
  --registry-password "${ACR_PASSWORD}" \
  --secrets "anthropic-api-key=${ANTHROPIC_API_KEY}" "storage-connection-string=${STORAGE_CONNECTION_STRING}" \
  --env-vars "${JOB_ENV_VARS[@]}" \
  --output none

cat <<EOF

Deployed.

  registry : ${ACR_NAME}.azurecr.io
  image    : ${IMAGE}
  storage  : ${STORAGE_ACCOUNT_NAME} (container: ${BLOB_CONTAINER})
  job      : ${JOB_NAME}  (manual trigger, task=${TASK})

Run it:         ./infra/run-job.sh
Compare models: ./infra/run-comparison.sh
Tail logs:      ./infra/logs.sh
Tear down:      ./infra/cleanup.sh
EOF
