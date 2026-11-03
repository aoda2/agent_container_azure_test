#!/usr/bin/env bash
# Start one execution of the job and wait for it to finish.
#
# Per-run overrides: any of TARGET_REPO_URL / BASE_REF / HEAD_REF / MODEL set
# in the environment are applied to this execution only, leaving the job's
# stored configuration untouched.

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_az

START_ARGS=(--name "${JOB_NAME}" --resource-group "${RESOURCE_GROUP}")

# --env-vars on `job start` replaces the execution's entire env var list
# rather than patching specific keys, so the secrets normally supplied by the
# job's stored config (ANTHROPIC_API_KEY, STORAGE_CONNECTION_STRING) must be
# repeated here, or the run fails with "ANTHROPIC_API_KEY is not set". List
# them first so OVERRIDE_ENV entries for the same key take precedence. The CLI
# also requires --image whenever any Container Argument (including
# --env-vars) is set, even though the image itself does not change.
if [[ -n "${OVERRIDE_ENV:-}" ]]; then
  BASE_ENV="ANTHROPIC_API_KEY=secretref:anthropic-api-key STORAGE_CONNECTION_STRING=secretref:storage-connection-string BLOB_CONTAINER=${BLOB_CONTAINER} TASK=${TASK}"
  # shellcheck disable=SC2206
  ALL_ENTRIES=(${BASE_ENV} ${OVERRIDE_ENV})

  # De-duplicate by key, keeping the last occurrence (so OVERRIDE_ENV wins
  # over BASE_ENV) — passing the same env var name twice to the API is
  # unsupported behavior we should not rely on.
  OVERRIDES=()
  for entry in "${ALL_ENTRIES[@]}"; do
    key="${entry%%=*}"
    kept=()
    for existing in "${OVERRIDES[@]+"${OVERRIDES[@]}"}"; do
      [[ "${existing%%=*}" == "${key}" ]] || kept+=("${existing}")
    done
    OVERRIDES=("${kept[@]+"${kept[@]}"}" "${entry}")
  done

  START_ARGS+=(--image "${IMAGE}" --env-vars "${OVERRIDES[@]}")
  echo "==> per-run overrides: ${OVERRIDE_ENV}"
fi

echo "==> starting ${JOB_NAME}"
EXECUTION="$(az containerapp job start "${START_ARGS[@]}" --query name --output tsv)"
echo "    execution: ${EXECUTION}"

echo "==> waiting for completion (polling every 10s)"
while true; do
  STATUS="$(az containerapp job execution show \
    --name "${JOB_NAME}" \
    --resource-group "${RESOURCE_GROUP}" \
    --job-execution-name "${EXECUTION}" \
    --query 'properties.status' --output tsv 2>/dev/null || echo Unknown)"

  case "${STATUS}" in
    Succeeded|Failed|Stopped|Degraded)
      echo "    status: ${STATUS}"
      break
      ;;
    *)
      echo "    status: ${STATUS:-Pending} ..."
      sleep 10
      ;;
  esac
done

echo
echo "Fetch the review report with:"
echo "  ./infra/logs.sh ${EXECUTION}"

[[ "${STATUS}" == "Succeeded" ]] || exit 1
