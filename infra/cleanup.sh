#!/usr/bin/env bash
# Delete everything this sample created. The resource group holds the registry,
# the Container Apps environment (and its Log Analytics workspace), and the job,
# so removing the group removes all of it.

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
require_az

echo "This deletes the resource group '${RESOURCE_GROUP}' and everything in it:"
az resource list --resource-group "${RESOURCE_GROUP}" --query '[].{name:name, type:type}' --output table 2>/dev/null \
  || { echo "  (resource group not found — nothing to do)"; exit 0; }

read -r -p "Type the resource group name to confirm: " CONFIRM
[[ "${CONFIRM}" == "${RESOURCE_GROUP}" ]] || { echo "aborted"; exit 1; }

az group delete --name "${RESOURCE_GROUP}" --yes --no-wait
echo "Deletion started in the background."
