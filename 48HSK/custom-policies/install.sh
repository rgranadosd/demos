#!/usr/bin/env bash
# Instala TODAS las custom policies (*.tar.gz de esta carpeta) en el APIM nativo
# y, por defecto, reconstruye la imagen wso2-apim del stack para que queden
# horneadas. Pensado para re-aplicarlas tras cada actualización del APIM nativo.
#
# Cada .tar.gz contiene una carpeta con su propio build.sh, que compila el
# mediador (javac) y despliega el JAR (dropins) + las secuencias Synapse.
#
# Variables:
#   APIM_HOME    (def: /Users/rafaelgd/Develop/wso2/demos/apim)  APIM nativo destino
#   JAVA_HOME    (def: JDK 21 del Mac)                     para javac
#   BUILD_IMAGE  (def: 1)                                  0 = no reconstruir imagen
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
APIM_HOME="${APIM_HOME:-/Users/rafaelgd/Develop/wso2/demos/apim}"
JAVA_HOME="${JAVA_HOME:-/Users/rafaelgd/java/jdk-21.0.11+10/Contents/Home}"
BUILD_IMAGE="${BUILD_IMAGE:-1}"
export JAVA_HOME
export PATH="$JAVA_HOME/bin:/opt/homebrew/bin:$PATH"

[ -d "$APIM_HOME" ] || { echo "APIM_HOME no existe: $APIM_HOME" >&2; exit 1; }

shopt -s nullglob
tgzs=("$DIR"/*.tar.gz)
[ ${#tgzs[@]} -gt 0 ] || { echo "No hay custom policies (*.tar.gz) en $DIR"; exit 0; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
for tgz in "${tgzs[@]}"; do
  echo "▶ descomprimiendo $(basename "$tgz")"
  tar -xzf "$tgz" -C "$tmp"
done

for pol in "$tmp"/*/; do
  [ -f "$pol/build.sh" ] || { echo "  (sin build.sh en $(basename "$pol"), omitida)"; continue; }
  echo "▶ instalando $(basename "$pol") en APIM_HOME=$APIM_HOME"
  ( cd "$pol" && APIM_HOME="$APIM_HOME" bash build.sh )
done
echo "✅ Custom policies instaladas en el APIM nativo."

if [ "$BUILD_IMAGE" = "1" ]; then
  echo "▶ reconstruyendo imagen wso2-apim (policies horneadas)…"
  ( cd "$DIR/../stack" && ./build.sh apim )
  echo "✅ Imagen reconstruida. Recuerda: ./down.sh && ./up.sh para usar la nueva."
fi
