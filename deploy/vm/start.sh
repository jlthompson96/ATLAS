#!/usr/bin/env bash
# Create (first run) or start the "atlas" ARM64 Debian 13 VM. Runbook: docs/runbooks/vm.md.
#
# The only mount is this repository, read-only, at /mnt/atlas. Its absolute path is computed here, so no
# machine-specific path is committed in atlas-vm.yaml.
set -euo pipefail

name="${ATLAS_VM:-atlas}"
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

command -v limactl >/dev/null || { echo "limactl not found: brew install lima" >&2; exit 1; }

if limactl list --format '{{.Name}}' 2>/dev/null | grep -qx "$name"; then
  exec limactl start "$name"
fi

mount_expr=".mounts = [{\"location\": \"${repo}\", \"mountPoint\": \"/mnt/atlas\", \"writable\": false}]"
exec limactl start --tty=false --name="$name" --set "$mount_expr" "$here/atlas-vm.yaml"
