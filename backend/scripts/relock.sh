#!/usr/bin/env bash
# Regenerate backend/requirements-dev.lock with SHA-256 hashes for every artifact (PLAN.md §6, T6).
# pip-tools runs from a throwaway venv so it never enters the project's own lockfile.
set -euo pipefail
cd "$(dirname "$0")/.."

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
python3 -m venv "$tmp/venv"
"$tmp/venv/bin/pip" install --quiet "pip-tools==7.7.*"
"$tmp/venv/bin/pip-compile" \
  --quiet \
  --generate-hashes \
  --strip-extras \
  --extra dev --extra mock \
  --output-file requirements-dev.lock \
  pyproject.toml
echo "Wrote requirements-dev.lock. Review the diff before committing."
