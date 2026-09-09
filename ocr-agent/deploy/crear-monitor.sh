#!/usr/bin/env bash
# Publica el monitor de evaluacion del agente OCR en Agent Manager.
#
# El API de AMP exige un JWT de usuario: la organizacion no va en la URL, sale
# del token (`OUIDFromRequest`). No hay forma de sacarlo sin sesion, asi que se
# copia de la consola — inspector web > Red > cualquier peticion a /api/v1 >
# cabecera Authorization — y se pasa por entorno:
#
#   AMP_TOKEN="eyJ..." ./deploy/crear-monitor.sh
#
# El token caduca en una hora. Si da 401, se vuelve a copiar.
#
# Alcance: fijo al proyecto telxius y al agente ocr-agent (ver abajo).
#
# Idempotencia: si el monitor ya existe el API responde 409. Para rehacerlo,
# borralo antes (DELETE .../monitors/radar-ocr-gastos) o cambia `name` en el JSON.
set -euo pipefail

ORG="${AMP_ORG:-default}"

# Proyecto y agente fijos, sin variable de entorno que los cambie: los patrones
# de `content_coverage` y `content_safety` estan escritos contra el JSON de
# `expense-v1`, asi que este monitor solo tiene sentido en el agente OCR de
# telxius. Apuntarlo al proyecto `default` — donde viven los agentes cpc-studio —
# crearia un monitor cuyos ejes darian 0 siempre.
PROY="telxius"
AGENTE="ocr-agent"

# La API sale publicada por el envoy del cluster, asi que normalmente no hace
# falta tunel. El port-forward queda como red de seguridad para cuando ese host
# no resuelve (otro contexto de kubectl, /etc/hosts sin la entrada...).
API="${AMP_API_BASE:-http://api.amp.localhost:8080}"
NS="${AMP_NAMESPACE:-wso2-amp}"
PUERTO="${AMP_PORT:-9000}"
PAYLOAD="$(cd "$(dirname "$0")" && pwd)/monitor-radar.json"

if [[ -z "${AMP_TOKEN:-}" ]]; then
  echo "falta AMP_TOKEN — copialo de la cabecera Authorization de la consola" >&2
  exit 1
fi
[[ -f "$PAYLOAD" ]] || { echo "no encuentro $PAYLOAD" >&2; exit 1; }

PF_PID=""
limpiar() { [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true; }
trap limpiar EXIT

if ! curl -sf -m 3 -o /dev/null "${API}/health" 2>/dev/null; then
  echo "${API} no responde; abriendo port-forward a ${NS}/amp-api:${PUERTO}"
  kubectl port-forward -n "$NS" "svc/amp-api" "${PUERTO}:9000" >/dev/null 2>&1 &
  PF_PID=$!
  API="http://127.0.0.1:${PUERTO}"
  for _ in $(seq 1 20); do
    curl -sf -m 1 -o /dev/null "${API}/health" 2>/dev/null && break
    sleep 0.5
  done
fi

RUTA="/api/v1/orgs/${ORG}/projects/${PROY}/agents/${AGENTE}/monitors"
echo "POST ${API}${RUTA}"

CODIGO=$(curl -sS -o /tmp/amp-monitor-respuesta.json -w '%{http_code}' \
  -X POST "${API}${RUTA}" \
  -H "Authorization: Bearer ${AMP_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data-binary "@${PAYLOAD}")

echo "HTTP ${CODIGO}"
python3 -m json.tool /tmp/amp-monitor-respuesta.json 2>/dev/null || cat /tmp/amp-monitor-respuesta.json
echo

case "$CODIGO" in
  20*) echo "monitor creado. La primera ejecucion arranca en menos de 60 s." ;;
  409) echo "ya existe: borralo o cambia el campo name del JSON." ;;
  401|403) echo "token invalido o sin permiso MonitorCreate." ;;
  404) echo "agente o entorno no encontrado: el monitor esta fijado a telxius/ocr-agent." ;;
  *)   echo "fallo — mira el cuerpo de la respuesta." ;;
esac
