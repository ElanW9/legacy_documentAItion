#!/usr/bin/env bash
# Decide which packages a docs run processes; used by .github/workflows/generate-docs.yml.
#
# Prints the arguments for `python -m docgen run` and, on GitHub Actions, also
# writes them to $GITHUB_OUTPUT as `args`. When the run is limited to changed
# files, their paths are written to changed.txt (docgen ignores non-PL/SQL paths).
#
#   push               diff against the previous push (BEFORE); if that commit
#                      is unknown (first push, force push): every out-of-date page
#   pull_request       diff against the base of the pull request (BASE_SHA)
#   workflow_dispatch  every out-of-date page; FULL_RUN=true: every page
#
# `--all` still skips pages whose stored cache key (source hash, prompts, model)
# matches, so only out-of-date pages are regenerated; `--force` regenerates all.
#
# Inputs (environment): EVENT_NAME, BEFORE, BASE_SHA, FULL_RUN, HEAD_SHA (default HEAD).
set -euo pipefail

head="${HEAD_SHA:-HEAD}"

known_commit() {
  # Empty or all zeros (GitHub's "no previous commit") or not in the cloned history.
  [ -n "$1" ] && [ -n "${1//0/}" ] && git cat-file -e "$1^{commit}" 2>/dev/null
}

diff_against() {
  git diff --name-only --no-renames "$1" "$head" > changed.txt
  echo "Changed files since $1:" >&2
  sed 's/^/  /' changed.txt >&2
  args="--files changed.txt"
}

case "${EVENT_NAME:-}" in
  push)
    if known_commit "${BEFORE:-}"; then
      diff_against "$BEFORE"
    else
      echo "Previous commit '${BEFORE:-}' unknown: every out-of-date page" >&2
      args="--all"
    fi
    ;;
  pull_request)
    if ! known_commit "${BASE_SHA:-}"; then
      echo "Base commit '${BASE_SHA:-}' not in history" >&2
      exit 1
    fi
    diff_against "$BASE_SHA"
    ;;
  workflow_dispatch)
    if [ "${FULL_RUN:-false}" = "true" ]; then
      echo "Manual full run: every page" >&2
      args="--all --force"
    else
      echo "Manual run: every out-of-date page" >&2
      args="--all"
    fi
    ;;
  *)
    echo "Unsupported event '${EVENT_NAME:-}'" >&2
    exit 1
    ;;
esac

echo "$args"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "args=$args" >> "$GITHUB_OUTPUT"
fi
