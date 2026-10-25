#!/usr/bin/env bash
# Prints the CHANGELOG.md section for one version (without its heading). Exits 1 if there is none.
#   scripts/release-notes.sh 0.4.1
set -euo pipefail
exec python3 "$(dirname "$0")/changelog.py" notes "${1:?usage: release-notes.sh <version>}"
