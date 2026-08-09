#!/usr/bin/env bash
# Gather the facts the env-report skill needs, in one command.
#
# Deliberately a single script rather than several shell commands: the agent is
# only granted permission to run this exact path, so it cannot run arbitrary
# bash — and one tool call keeps the smoke test to a couple of cents.

set -uo pipefail

check() {
  # check <label> <command...>  — never fails the script, reports the error instead.
  local label="$1"
  shift
  local value
  if value="$("$@" 2>&1 | head -1)"; then
    printf '%s: %s\n' "${label}" "${value}"
  else
    printf '%s: ERROR (%s)\n' "${label}" "${value:-command failed}"
  fi
}

check "os_arch" uname -sm
check "python" python3 --version
check "git" git --version

printf 'working_directory: %s\n' "$(pwd)"

out="${OUTPUT_DIR:-/out}"
if [[ -d "${out}" && -w "${out}" ]]; then
  printf 'writable_output_dir: %s (writable)\n' "${out}"
else
  printf 'writable_output_dir: %s (missing or read-only)\n' "${out}"
fi
