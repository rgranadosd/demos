#!/usr/bin/env bash
#
# Arranca/para el proxy OAuth de usuario del MCP WeatherMCP (escucha en :9096).
# VS Code se conecta a http://127.0.0.1:9096 (ver .vscode/mcp.json).
#
# El proxy es de autenticacion PEREZOSA (lazy): arranca y escucha al instante,
# SIN pedir login. El navegador solo se abre la primera vez que de verdad se usa
# una tool (get_current_weather, etc.) — asi lo marca la spec de autorizacion de
# MCP: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
#
# Uso:
#   ./weather-mcp-proxy.sh start     arranca el proxy en segundo plano (por defecto)
#   ./weather-mcp-proxy.sh stop      lo para
#   ./weather-mcp-proxy.sh restart   lo reinicia
#   ./weather-mcp-proxy.sh status    dice si esta vivo
#   ./weather-mcp-proxy.sh login     arranca; en el PRIMER uso real de una tool
#                                     fuerza la pantalla de login (en vez de SSO)

set -euo pipefail

PROXY="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mcp-oauth-proxy.py"
PORT=9096
LOG="/tmp/mcp-oauth-proxy.log"
PIDPAT="mcp-oauth-proxy.py"

is_up() { lsof -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; }

stop() {
  if pgrep -f "$PIDPAT" >/dev/null 2>&1; then
    pkill -f "$PIDPAT" 2>/dev/null || true
    sleep 1
    echo "proxy parado."
  else
    echo "no estaba corriendo."
  fi
}

start() {
  local extra="${1:-}"
  if is_up; then
    echo "ya hay algo escuchando en :$PORT. Usa restart si quieres relanzarlo."
    return 0
  fi
  echo "arrancando proxy${extra:+ ($extra)}..."
  nohup python3 "$PROXY" $extra > "$LOG" 2>&1 &
  for i in $(seq 1 10); do
    grep -q "escuchando en" "$LOG" 2>/dev/null && break
    sleep 1
  done
  if is_up; then
    echo "proxy arrancado y escuchando en http://127.0.0.1:$PORT"
    echo "(sin autenticar todavia: el login se pedira en el navegador la primera vez que se use una tool)"
  else
    echo "ERROR: el proxy no llego a escuchar. Ultimas lineas del log:"
    tail -8 "$LOG"
    return 1
  fi
}

case "${1:-start}" in
  start)   start ;;
  login)   start "--login" ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)
    if is_up; then echo "RUNNING (escuchando en :$PORT)"; pgrep -fl "$PIDPAT" || true
    else echo "STOPPED"; fi ;;
  *) echo "uso: $0 {start|stop|restart|status|login}"; exit 1 ;;
esac
