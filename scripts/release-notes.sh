#!/usr/bin/env bash
# Prints the CHANGELOG.md section for one version (without its heading). Exits 1 if there is none.
#   scripts/release-notes.sh 0.4.1
set -euo pipefail
v="${1:?usage: release-notes.sh <version>}"
root="$(cd "$(dirname "$0")/.." && pwd)"

notes=$(awk -v v="$v" '
  /^## \[/ { if (on) exit; on = index($0, "## [" v "]") == 1; next }
  on
' "$root/CHANGELOG.md" | sed -e '/./,$!d')     # drop leading blank lines

if [[ -z "${notes//[[:space:]]/}" ]]; then
  echo "no CHANGELOG.md section for [$v]" >&2
  exit 1
fi
printf '%s\n' "$notes"
