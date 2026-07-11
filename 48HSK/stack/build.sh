#!/usr/bin/env bash
# Construye las 3 imágenes del stack 48HSK con Podman, horneando la config/datos
# ACTUALES de APIM, IS y la carpeta 48HSK (agente + MCP + AI Gateway demo).
#
# IMPORTANTE: para un snapshot LIMPIO de H2, para APIM e IS antes de construir
#   (api-manager.sh stop / wso2server.sh stop). Este script avisa si siguen vivos.
#
# Uso:  ./build.sh            (construye apim, is y charlas)
#       ./build.sh apim       (solo una)
set -euo pipefail

# --- Rutas de origen (ajústalas si tu layout cambia) ---
APIM_SRC="${APIM_SRC:-/Users/rafaelgd/Develop/wso2/demos/apim}"
IS_SRC="${IS_SRC:-/Users/rafaelgd/Develop/wso2/demos/IS/wso2is-7.3.0}"
STACK_DIR="$(cd "$(dirname "$0")" && pwd)"
CHARLAS_SRC="${CHARLAS_SRC:-$(cd "$STACK_DIR/.." && pwd)}"   # .../48HSK
BUILD_DIR="$STACK_DIR/.build"

PODMAN="${PODMAN:-podman}"
TAG="${TAG:-48hsk}"

WSO2_EXCLUDES=(
  --exclude 'repository/logs/***'
  --exclude 'tmp/***'
  --exclude '/backup'
  --exclude 'repository/components/default/configuration/org.eclipse.osgi'          # backup transitorio del config-mapper (rompe el arranque en Linux)
  --exclude '**/*.lck'
  --exclude '**/*.lock.db'
  --exclude '**/*.trace.db'
  --exclude 'heap-dump*'
  --exclude 'wso2carbon.pid'
)

warn_if_running() {
  local name="$1" pattern="$2"
  if pgrep -f "$pattern" >/dev/null 2>&1; then
    echo "⚠️  $name parece estar CORRIENDO. Para un snapshot limpio de H2, párala antes:"
    echo "      $name → stop"
    read -r -p "    ¿Continuar de todos modos (snapshot en caliente)? [y/N] " ans
    [[ "${ans:-N}" =~ ^[Yy]$ ]] || { echo "Abortado."; exit 1; }
  fi
}

snap_wso2() {  # $1=src  $2=dest_payload
  mkdir -p "$2"
  rsync -a --delete "${WSO2_EXCLUDES[@]}" "$1"/ "$2"/
  rm -rf "$2/backup"   # por si un snapshot previo lo dejó (config-mapper transitorio)
}

# Aplica TODAS las custom policies (48HSK/custom-policies/*.tar.gz) al payload del
# build, de forma que la imagen SIEMPRE las lleva horneadas, sea cual sea el estado
# del APIM nativo. Cada policy trae su build.sh (compila el fragment → dropins del
# payload, registra el handler en el deployment.toml del payload, etc.).
CUSTOM_POLICIES_DIR="${CUSTOM_POLICIES_DIR:-$(cd "$STACK_DIR/.." && pwd)/custom-policies}"
apply_custom_policies() {  # $1 = APIM_HOME destino (el payload)
  local home="$1"
  [ -d "$CUSTOM_POLICIES_DIR" ] || return 0
  shopt -s nullglob
  local tgzs=("$CUSTOM_POLICIES_DIR"/*.tar.gz)
  [ ${#tgzs[@]} -gt 0 ] || { echo "  (sin custom policies)"; return 0; }
  export JAVA_HOME="${JAVA_HOME:-/Users/rafaelgd/java/jdk-21.0.11+10/Contents/Home}"
  export PATH="$JAVA_HOME/bin:$PATH"
  local tmp; tmp="$(mktemp -d)"
  for tgz in "${tgzs[@]}"; do
    echo "▶ aplicando custom policy $(basename "$tgz") al payload"
    tar -xzf "$tgz" -C "$tmp"
  done
  for pol in "$tmp"/*/; do
    [ -f "$pol/build.sh" ] && ( cd "$pol" && APIM_HOME="$home" bash build.sh >/dev/null )
  done
  rm -rf "$tmp"
}

build_apim() {
  warn_if_running "APIM" "carbon.home=$APIM_SRC"
  echo "▶ snapshot APIM…"; snap_wso2 "$APIM_SRC" "$BUILD_DIR/apim/payload"
  apply_custom_policies "$BUILD_DIR/apim/payload"   # SIEMPRE aplica las custom policies
  cp "$STACK_DIR/apim/Dockerfile" "$BUILD_DIR/apim/Dockerfile"
  echo "▶ build wso2-apim:$TAG"; "$PODMAN" build -t "wso2-apim:$TAG" "$BUILD_DIR/apim"
}

build_is() {
  warn_if_running "IS" "wso2is-7.3.0"
  echo "▶ snapshot IS…"; snap_wso2 "$IS_SRC" "$BUILD_DIR/is/payload"
  cp "$STACK_DIR/is/Dockerfile" "$BUILD_DIR/is/Dockerfile"
  echo "▶ build wso2-is:$TAG"; "$PODMAN" build -t "wso2-is:$TAG" "$BUILD_DIR/is"
}

build_charlas() {
  local dest="$BUILD_DIR/charlas/payload"
  echo "▶ snapshot 48HSK (AGENT + MCP + AI GATEWAY)…"
  mkdir -p "$dest"
  rsync -a --delete \
    --exclude 'venv' --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '*.log' --exclude 'token_cache.json' --exclude '.git' --exclude 'node_modules' \
    "$CHARLAS_SRC/AGENT" "$CHARLAS_SRC/MCP" "$CHARLAS_SRC/AI GATEWAY" "$dest"/
  cp "$STACK_DIR/charlas/Dockerfile" "$BUILD_DIR/charlas/Dockerfile"
  echo "▶ build charlas-48hsk:$TAG"; "$PODMAN" build -t "charlas-48hsk:$TAG" "$BUILD_DIR/charlas"
}

case "${1:-all}" in
  apim) build_apim ;;
  is) build_is ;;
  charlas) build_charlas ;;
  all) build_apim; build_is; build_charlas ;;
  *) echo "uso: $0 [apim|is|charlas|all]"; exit 1 ;;
esac
echo "✅ Build terminado. Imágenes:"; "$PODMAN" images | grep -E "wso2-apim|wso2-is|charlas-48hsk" || true
