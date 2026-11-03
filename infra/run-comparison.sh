#!/usr/bin/env bash
# Start three concurrent executions of the job, one per model, all running
# TASK=explain-code against the same repo/diff range, and wait for all three.
#
# Each manual `az containerapp job start` call returns as soon as the
# execution is accepted (see run-job.sh, which polls separately afterward),
# so calling it three times in a row starts three executions within seconds
# of each other — no backgrounding needed for the start calls themselves.
#
# Usage: ./infra/run-comparison.sh
# Models compared, in order: haiku sonnet opus (override with MODELS="...")

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_az

: "${MODELS:=haiku sonnet opus}"
read -r -a MODEL_LIST <<< "${MODELS}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
echo "==> run id: ${RUN_ID}"

EXECUTIONS=()
for model in "${MODEL_LIST[@]}"; do
  # `job start --env-vars` replaces the execution's entire env var list rather
  # than patching specific keys, so the secrets normally supplied by the job's
  # stored config (ANTHROPIC_API_KEY, STORAGE_CONNECTION_STRING) must be
  # repeated here or the run fails with "ANTHROPIC_API_KEY is not set".
  OVERRIDE_ENV="TASK=explain-code MODEL=${model} RUN_ID=${RUN_ID} TARGET_REPO_URL=${TARGET_REPO_URL} BASE_REF=${BASE_REF} HEAD_REF=${HEAD_REF} ANTHROPIC_API_KEY=secretref:anthropic-api-key STORAGE_CONNECTION_STRING=secretref:storage-connection-string BLOB_CONTAINER=${BLOB_CONTAINER}"
  # `--env-vars` also drops OTEL_CAPTURE etc. unless repeated here.
  [[ -n "${OTEL_CAPTURE:-}" ]] && OVERRIDE_ENV+=" OTEL_CAPTURE=${OTEL_CAPTURE}"
  [[ -n "${OTEL_LOG_USER_PROMPTS:-}" ]] && OVERRIDE_ENV+=" OTEL_LOG_USER_PROMPTS=${OTEL_LOG_USER_PROMPTS}"
  # shellcheck disable=SC2206
  OVERRIDES=(${OVERRIDE_ENV})
  echo "==> starting ${JOB_NAME} (model=${model})"
  # --image is required here even though it never changes: the CLI requires it
  # whenever any Container Argument (including --env-vars) is set.
  EXECUTION="$(az containerapp job start \
    --name "${JOB_NAME}" \
    --resource-group "${RESOURCE_GROUP}" \
    --image "${IMAGE}" \
    --env-vars "${OVERRIDES[@]}" \
    --query name --output tsv)"
  echo "    execution: ${EXECUTION}"
  EXECUTIONS+=("${model}:${EXECUTION}")
done

echo "==> waiting for all ${#EXECUTIONS[@]} executions (polling every 10s)"
FAILED=0
PENDING=("${EXECUTIONS[@]}")
while [[ ${#PENDING[@]} -gt 0 ]]; do
  sleep 10
  STILL_PENDING=()
  for entry in "${PENDING[@]}"; do
    model="${entry%%:*}"
    execution="${entry#*:}"
    STATUS="$(az containerapp job execution show \
      --name "${JOB_NAME}" \
      --resource-group "${RESOURCE_GROUP}" \
      --job-execution-name "${execution}" \
      --query 'properties.status' --output tsv 2>/dev/null || echo Unknown)"

    case "${STATUS}" in
      Succeeded)
        echo "    [${model}] ${execution}: Succeeded"
        ;;
      Failed|Stopped|Degraded)
        echo "    [${model}] ${execution}: ${STATUS}"
        FAILED=1
        ;;
      *)
        STILL_PENDING+=("${entry}")
        ;;
    esac
  done
  # The plain form errors under bash 3.2 (macOS's default /bin/bash) with
  # "unbound variable" when STILL_PENDING has zero elements and `set -u` is
  # active; this expansion form is the portable way to copy a possibly-empty
  # array.
  PENDING=("${STILL_PENDING[@]+"${STILL_PENDING[@]}"}")
done

echo
echo "Reports uploaded under run id ${RUN_ID}:"
echo "  az storage blob list --container-name ${BLOB_CONTAINER} --prefix '${RUN_ID}/' \\"
echo "    --connection-string \"\$(az storage account show-connection-string --name ${STORAGE_ACCOUNT_NAME} --resource-group ${RESOURCE_GROUP} --query connectionString --output tsv)\" \\"
echo "    --output table"
echo
echo "Per-execution logs: ./infra/logs.sh <execution>"

exit "${FAILED}"
