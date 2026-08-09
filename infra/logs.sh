#!/usr/bin/env bash
# Print the console output of a job execution — which includes the Markdown
# review report, since run_agent.py writes the report to stdout and progress
# lines to stderr.
#
# Usage: ./infra/logs.sh [execution-name]   (defaults to the latest execution)

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_az

EXECUTION="${1:-}"
if [[ -z "${EXECUTION}" ]]; then
  EXECUTION="$(az containerapp job execution list \
    --name "${JOB_NAME}" \
    --resource-group "${RESOURCE_GROUP}" \
    --query 'sort_by([], &properties.startTime)[-1].name' \
    --output tsv)"
  echo "==> latest execution: ${EXECUTION}" >&2
fi

# Log ingestion into the environment's Log Analytics workspace lags the
# execution by roughly 30-60 seconds; an empty result usually just means
# "too early", not "no output".
if ! az containerapp job logs show \
  --name "${JOB_NAME}" \
  --resource-group "${RESOURCE_GROUP}" \
  --container "${JOB_NAME}" \
  --execution "${EXECUTION}" \
  --tail 500 \
  --format text
then
  cat >&2 <<EOF

Could not fetch logs via the CLI. Either the logs have not been ingested yet
(wait ~60s and retry), or your az containerapp extension is too old for
'job logs show' — run 'az extension update --name containerapp'.

You can also read them in the portal:
  Resource group ${RESOURCE_GROUP} > ${JOB_NAME} > Execution history > ${EXECUTION}
EOF
  exit 1
fi
