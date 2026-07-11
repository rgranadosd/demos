#!/usr/bin/env bash
# Para y elimina el pod del stack 48HSK (las imágenes NO se borran).
set -euo pipefail
PODMAN="${PODMAN:-podman}"
POD="${POD:-wso2-48hsk}"
"$PODMAN" pod stop "$POD" 2>/dev/null || true
"$PODMAN" pod rm "$POD" 2>/dev/null || true
echo "✅ pod '$POD' parado y eliminado."
