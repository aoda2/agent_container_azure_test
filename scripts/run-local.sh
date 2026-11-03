#!/usr/bin/env bash
# Build and run the same image locally, before spending anything on Azure.
# The report goes to stdout; ./out/report.md gets a copy.
#
#   ANTHROPIC_API_KEY=sk-ant-... ./scripts/run-local.sh          # cheap smoke test
#   TASK=code-review ./scripts/run-local.sh                      # the real sample
#   TASK=code-review TARGET_REPO_URL=... BASE_REF=main~3 ./scripts/run-local.sh
#   TASK=explain-code TARGET_REPO_URL=... BASE_REF=main~3 ./scripts/run-local.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

[[ -f .env ]] && { set -a; source .env; set +a; }

: "${ANTHROPIC_API_KEY:?export ANTHROPIC_API_KEY (or put it in .env)}"
: "${TASK:=env-report}"
: "${TARGET_REPO_URL:=https://github.com/anthropics/claude-agent-sdk-demos}"
: "${BASE_REF:=HEAD~5}"
: "${HEAD_REF:=HEAD}"

IMAGE_LOCAL="${IMAGE_LOCAL:-agent-skill-sample:local}"

# Match the platform Azure Container Apps runs, so what you test here is what
# ships. On Apple silicon this runs under emulation and is noticeably slower.
echo "==> building ${IMAGE_LOCAL}"
docker build --platform linux/amd64 -t "${IMAGE_LOCAL}" .

mkdir -p out

RUN_ARGS=(
  --rm
  --platform linux/amd64
  -e "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
  -e "TASK=${TASK}"
  -e "OUTPUT_DIR=/out"
  -v "${REPO_ROOT}/out:/out"
)
# Only the repo-based tasks read these; MODEL/MAX_TURNS fall back to the
# task's own defaults when left unset.
if [[ "${TASK}" == "code-review" || "${TASK}" == "explain-code" ]]; then
  RUN_ARGS+=(-e "TARGET_REPO_URL=${TARGET_REPO_URL}" -e "BASE_REF=${BASE_REF}" -e "HEAD_REF=${HEAD_REF}")
fi
[[ -n "${MODEL:-}" ]] && RUN_ARGS+=(-e "MODEL=${MODEL}")
[[ -n "${MAX_TURNS:-}" ]] && RUN_ARGS+=(-e "MAX_TURNS=${MAX_TURNS}")
[[ -n "${STORAGE_CONNECTION_STRING:-}" ]] && RUN_ARGS+=(-e "STORAGE_CONNECTION_STRING=${STORAGE_CONNECTION_STRING}")
[[ -n "${BLOB_CONTAINER:-}" ]] && RUN_ARGS+=(-e "BLOB_CONTAINER=${BLOB_CONTAINER}")
[[ -n "${RUN_ID:-}" ]] && RUN_ARGS+=(-e "RUN_ID=${RUN_ID}")

echo "==> running task=${TASK}"
docker run "${RUN_ARGS[@]}" "${IMAGE_LOCAL}"

echo
echo "==> report written to ${REPO_ROOT}/out/report.md"
