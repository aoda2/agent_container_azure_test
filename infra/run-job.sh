#!/usr/bin/env bash
# Start one execution of the job and wait for it to finish.
#
# Per-run overrides: any of TARGET_REPO_URL / BASE_REF / HEAD_REF / MODEL set
# in the environment are applied to this execution only, leaving the job's
# stored configuration untouched.

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_az

START_ARGS=(--name "${JOB_NAME}" --resource-group "${RESOURCE_GROUP}")

# --env-vars on `job start` overrides only the listed variables for this run.
if [[ -n "${OVERRIDE_ENV:-}" ]]; then
  # shellcheck disable=SC2206
  OVERRIDES=(${OVERRIDE_ENV})
  START_ARGS+=(--env-vars "${OVERRIDES[@]}")
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
