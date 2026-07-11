#!/usr/bin/env bash
# Levanta el stack 48HSK en un POD de Podman. Los 3 contenedores comparten el
# namespace de red del pod → mismo localhost → la config actual (localhost:9443/
# 8243/9453/8000/28080) funciona SIN reescribir nada.
set -euo pipefail
PODMAN="${PODMAN:-podman}"
TAG="${TAG:-48hsk}"
POD="${POD:-wso2-48hsk}"

if "$PODMAN" pod exists "$POD" 2>/dev/null; then
  echo "El pod '$POD' ya existe. Bájalo primero con ./down.sh"; exit 1
fi

echo "▶ creando pod '$POD' (puertos publicados al host)…"
"$PODMAN" pod create --name "$POD" \
  -p 9443:9443 -p 8243:8243 -p 8280:8280 \
  -p 9453:9453 \
  -p 8000:8000 -p 28080:28080 -p 8502:8502

echo "▶ arrancando IS…";      "$PODMAN" run -d --pod "$POD" --name is      --restart on-failure "wso2-is:$TAG"
echo "▶ arrancando APIM…";    "$PODMAN" run -d --pod "$POD" --name apim    --restart on-failure "wso2-apim:$TAG"
echo "▶ arrancando charlas…"; "$PODMAN" run -d --pod "$POD" --name charlas --restart on-failure "charlas-48hsk:$TAG"

cat <<EOF

✅ Stack levantándose. WSO2 tarda ~1-2 min en estar listo.
   Logs:      podman logs -f apim   |   podman logs -f is   |   podman logs -f charlas
   IS:        https://localhost:9453/carbon   (admin/admin)
   APIM:      https://localhost:9443/publisher (admin/admin)  · gateway https://localhost:8243
   Agente:    podman exec -it charlas bash -lc 'cd /opt/charlas/48HSK/AGENT && python agent_gpt4.py'
   Parar:     ./down.sh
EOF
