#!/usr/bin/env bash
# Builds the release tarball from the committed tree (git archive: tracked files only).
#   scripts/build-release.sh           -> dist/uurtjie-<v>.tar.gz, dist/uurtjie.tar.gz, dist/SHA256SUMS
#   scripts/build-release.sh --check   -> only the version + changelog checks (CI)
# Prints "version=<v>" last, so a workflow can append it to $GITHUB_OUTPUT.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

v=$(python3 -c 'import uurtjie; print(uurtjie.__version__)')
mv=$(python3 -c 'import json; print(json.load(open("omarchy-plugin/jd.uurtjie/manifest.json"))["version"])')
if [[ "$v" != "$mv" ]]; then
  echo "version mismatch: uurtjie/__init__.py says $v, omarchy-plugin/jd.uurtjie/manifest.json says $mv" >&2
  exit 1
fi
scripts/release-notes.sh "$v" >/dev/null || { echo "add a '## [$v] - YYYY-MM-DD' section to CHANGELOG.md" >&2; exit 1; }

if [[ "${1:-}" != "--check" ]]; then
  [[ -z "$(git status --porcelain)" ]] || echo "warning: uncommitted changes are not in the tarball (built from HEAD)" >&2
  rm -rf dist && mkdir dist
  git archive --format=tar.gz --prefix=uurtjie/ -o "dist/uurtjie-$v.tar.gz" HEAD
  cp "dist/uurtjie-$v.tar.gz" dist/uurtjie.tar.gz
  (cd dist && sha256sum "uurtjie-$v.tar.gz" uurtjie.tar.gz > SHA256SUMS)
  echo "built dist/uurtjie-$v.tar.gz" >&2
fi
echo "version=$v"
