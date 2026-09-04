#!/usr/bin/env python3
"""Servidor de la demo visual del agente OCR.

Sirve `chat.html` y hace de intermediario con el agente. El navegador nunca ve
la API key: la petición llega aquí sin credencial y sale hacia el gateway con
la cabecera `X-API-Key` puesta desde `.env`. Así la demo se puede proyectar sin
enseñar el secreto en las DevTools.

Solo librería estándar. Arranca con:

    ./servidor.py            # http://127.0.0.1:8800
    ./servidor.py --port 9000
"""

import argparse
import json
import secrets
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AQUI = os.path.dirname(os.path.abspath(__file__))


def cargar_env():
    """Carga `.env` sin dependencias. El entorno existente tiene precedencia."""
    ruta = os.path.join(AQUI, ".env")
    if not os.path.isfile(ruta):
        return
    with open(ruta) as fh:
        for linea in fh:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


cargar_env()

AGENTE_URL = os.getenv(
    "OCR_AGENT_URL",
    "http://default-default.am-gateway.localhost:19080/ocr-agent-ocr-agent-endpoint",
).rstrip("/")
API_KEY = os.getenv("OCR_AGENT_API_KEY", "")


def reenviar(imagen: bytes, nombre: str, tipo: str, origen: str = "fichero"):
    """Manda la imagen al agente y devuelve (codigo, cuerpo_json).

    Se reenvia tambien de donde viene la imagen. No cambia el analisis: el
    agente lo deja en la traza, que es la unica forma de distinguir despues una
    captura de camara de un fichero arrastrado.
    """
    frontera = f"----ocr{uuid.uuid4().hex}"
    cuerpo = b"".join([
        f"--{frontera}\r\n".encode(),
        f'Content-Disposition: form-data; name="origen"\r\n\r\n'.encode(),
        f"{origen}\r\n".encode(),
        f"--{frontera}\r\n".encode(),
        f'Content-Disposition: form-data; name="fichero"; filename="{nombre}"\r\n'.encode(),
        f"Content-Type: {tipo}\r\n\r\n".encode(),
        imagen,
        f"\r\n--{frontera}--\r\n".encode(),
    ])
    # Contexto W3C: el cliente decide el trace id, y el agente cuelga sus spans
    # de aqui en vez de abrir una traza nueva. Asi el recorrido completo —
    # navegador, este proxy, gateway y agente — comparte un unico identificador
    # que se puede buscar en AMP.
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    cabeceras = {
        "Content-Type": f"multipart/form-data; boundary={frontera}",
        "traceparent": f"00-{trace_id}-{span_id}-01",
    }
    if API_KEY:
        cabeceras["X-API-Key"] = API_KEY

    inicio = time.time()
    req = urllib.request.Request(
        f"{AGENTE_URL}/gastos/analizar/fichero", data=cuerpo,
        headers=cabeceras, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            datos = json.loads(resp.read().decode())
        datos["_segundos"] = round(time.time() - inicio, 1)
        datos["_trace_id"] = trace_id
        return 200, datos
    except urllib.error.HTTPError as err:
        detalle = err.read().decode(errors="replace")[:500]
        pista = {
            401: "El gateway ha rechazado la credencial. Revisa OCR_AGENT_API_KEY en .env",
            404: "La ruta no existe. Revisa OCR_AGENT_URL en .env",
        }.get(err.code, "")
        return err.code, {"error": f"HTTP {err.code}", "detalle": detalle,
                          "pista": pista, "_trace_id": trace_id}
    except urllib.error.URLError as err:
        return 502, {"error": "sin conexión con el agente", "detalle": str(err.reason),
                     "pista": "¿Está el cluster levantado y LM Studio arrancado?"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # menos ruido en pantalla durante la demo
        if "/api/" in (args[0] if args else ""):
            sys.stderr.write(f"  → {args[0]}\n")

    def _responder(self, codigo, cuerpo, tipo="application/json; charset=utf-8"):
        datos = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        # Sin esto el navegador se queda con la versión cacheada de chat.html y
        # los cambios no aparecen aunque recargues. En una demo que se retoca
        # sobre la marcha, eso hace perder un rato buscando un fallo que no existe.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(datos)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/chat.html"):
            with open(os.path.join(AQUI, "chat.html"), "rb") as fh:
                return self._responder(200, fh.read(), "text/html; charset=utf-8")
        if self.path == "/api/config":
            return self._responder(200, {
                "agente_url": AGENTE_URL,
                "con_credencial": bool(API_KEY),
            })
        return self._responder(404, {"error": "no encontrado"})

    def do_POST(self):
        if self.path != "/api/analizar":
            return self._responder(404, {"error": "no encontrado"})

        largo = int(self.headers.get("Content-Length", 0))
        if largo <= 0:
            return self._responder(400, {"error": "cuerpo vacío"})
        crudo = self.rfile.read(largo)

        # El navegador manda multipart; se extrae el fichero sin dependencias.
        tipo_cab = self.headers.get("Content-Type", "")
        if "boundary=" not in tipo_cab:
            return self._responder(400, {"error": "falta boundary"})
        b = tipo_cab.split("boundary=")[1].strip().strip('"').encode()

        nombre, tipo, imagen, origen = "captura.png", "image/png", None, "fichero"
        for parte in crudo.split(b"--" + b):
            if b'name="origen"' in parte and b"filename=" not in parte:
                _, _, val = parte.partition(b"\r\n\r\n")
                origen = val.strip().decode(errors="replace") or origen
                continue
            if b"filename=" not in parte:
                continue
            cabeceras, _, datos = parte.partition(b"\r\n\r\n")
            texto = cabeceras.decode(errors="replace")
            if 'filename="' in texto:
                nombre = texto.split('filename="')[1].split('"')[0] or nombre
            if "Content-Type:" in texto:
                tipo = texto.split("Content-Type:")[1].strip().splitlines()[0].strip() or tipo
            imagen = datos.rstrip(b"\r\n-")
            break

        if not imagen:
            return self._responder(400, {"error": "no se ha recibido ninguna imagen"})

        codigo, respuesta = reenviar(imagen, nombre, tipo, origen)
        return self._responder(codigo, respuesta)


def main():
    p = argparse.ArgumentParser(description="Demo visual del agente OCR de gastos.")
    p.add_argument("--port", type=int, default=8800)
    p.add_argument("--no-abrir", action="store_true", help="no abrir el navegador")
    args = p.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    print(f"\n  \033[1mDemo del agente OCR de gastos\033[0m")
    print(f"  agente      {AGENTE_URL}")
    print(f"  credencial  {'X-API-Key desde .env' if API_KEY else '⚠ ninguna (dará 401)'}")
    print(f"  interfaz    {url}\n")
    if not API_KEY:
        print("  \033[33m⚠ Sin OCR_AGENT_API_KEY en .env el gateway rechazará las llamadas.\033[0m\n")

    if not args.no_abrir:
        webbrowser.open(url)
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  parado\n")


if __name__ == "__main__":
    main()
