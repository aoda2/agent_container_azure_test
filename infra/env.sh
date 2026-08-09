#!/usr/bin/env bash
# Shared configuration for the infra/*.sh scripts.
# Sourced, not executed. Override anything by exporting it first, or by
# putting `KEY=value` lines in a .env file at the repository root.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load .env if present (never commit it — see .gitignore).
if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO_ROOT}/.env"
  set +a
fi

# --- Azure resources -------------------------------------------------------
# ACR names are globally unique and must be 5-50 lowercase alphanumerics, so
# NAME_SUFFIX keeps repeat deploys from colliding with someone else's registry.
: "${LOCATION:=japaneast}"
: "${NAME_SUFFIX:=$(whoami | tr -cd 'a-z0-9' | cut -c1-8)}"
: "${RESOURCE_GROUP:=rg-agent-skill-${NAME_SUFFIX}}"
: "${ACR_NAME:=acragentskill${NAME_SUFFIX}}"
: "${ENVIRONMENT_NAME:=cae-agent-skill-${NAME_SUFFIX}}"
: "${JOB_NAME:=job-code-review}"
: "${IMAGE_NAME:=code-review-agent}"
: "${IMAGE_TAG:=latest}"

# --- Job configuration -----------------------------------------------------
# TASK=env-report is the cheap smoke test (a few cents). Switch to code-review
# for the realistic sample; see the cost table in README.md before you do.
: "${TASK:=env-report}"
: "${TARGET_REPO_URL:=https://github.com/anthropics/claude-agent-sdk-demos}"
: "${BASE_REF:=HEAD~5}"
: "${HEAD_REF:=HEAD}"
# Empty means "let the task pick" — haiku for env-report, sonnet for code-review.
: "${MODEL:=}"
: "${MAX_TURNS:=}"
: "${JOB_CPU:=1.0}"
: "${JOB_MEMORY:=2.0Gi}"
# Reviews are minutes, not seconds. Give the replica room before it is killed.
: "${REPLICA_TIMEOUT:=1800}"

IMAGE="${ACR_NAME}.azurecr.io/${IMAGE_NAME}:${IMAGE_TAG}"

require_az() {
  command -v az >/dev/null 2>&1 || {
    echo "error: the Azure CLI (az) is not installed." >&2
    echo "  https://learn.microsoft.com/cli/azure/install-azure-cli" >&2
    exit 1
  }
  az account show >/dev/null 2>&1 || {
    echo "error: not signed in. Run 'az login' first." >&2
    exit 1
  }
}

require_api_key() {
  [[ -n "${ANTHROPIC_API_KEY:-}" ]] || {
    echo "error: ANTHROPIC_API_KEY is not set (export it, or put it in .env)." >&2
    exit 1
  }
}

# A fresh subscription usually has these unregistered, which fails the first
# `az acr create` / `az containerapp env create` with MissingSubscriptionRegistration.
# Registering is a one-time, non-destructive, subscription-wide operation —
# safe to run on every deploy; already-registered namespaces return instantly.
require_providers() {
  local providers=(Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry)
  local pending=()

  for ns in "${providers[@]}"; do
    local state
    state="$(az provider show --namespace "${ns}" --query registrationState -o tsv 2>/dev/null || echo NotRegistered)"
    [[ "${state}" == "Registered" ]] || pending+=("${ns}")
  done
  [[ ${#pending[@]} -eq 0 ]] && return

  echo "==> registering resource providers (first time on this subscription): ${pending[*]}"
  for ns in "${pending[@]}"; do
    az provider register --namespace "${ns}" --output none
  done

  # Registration typically takes 1-3 minutes; poll rather than assume.
  echo "==> waiting for provider registration to complete"
  for ns in "${pending[@]}"; do
    for _ in $(seq 1 40); do
      state="$(az provider show --namespace "${ns}" --query registrationState -o tsv 2>/dev/null)"
      [[ "${state}" == "Registered" ]] && break
      sleep 15
    done
    if [[ "${state}" != "Registered" ]]; then
      echo "error: ${ns} did not finish registering in time (last state: ${state})." >&2
      echo "  Check with: az provider show --namespace ${ns} --query registrationState" >&2
      echo "  Then re-run this script." >&2
      exit 1
    fi
  done
}
