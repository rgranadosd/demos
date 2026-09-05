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
import base64
import hashlib
import json
import secrets
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
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

# --------------------------------------------------------------- login OAuth2

# El flujo entero ocurre aqui, en el servidor: el navegador nunca ve el token
# de usuario, igual que nunca ve la API key. Solo recibe una cookie de sesion
# opaca. Asi la demo se puede proyectar con las DevTools abiertas.
THUNDER_ISSUER = os.getenv("THUNDER_ISSUER", "").rstrip("/")
THUNDER_CLIENT_ID = os.getenv("THUNDER_CLIENT_ID", "")
THUNDER_CLIENT_SECRET = os.getenv("THUNDER_CLIENT_SECRET", "")

LOGIN_ACTIVO = bool(THUNDER_ISSUER and THUNDER_CLIENT_ID)

_SESIONES = {}
_OIDC = {}


def _descubrir():
    """Lee los endpoints del `.well-known` una vez y los cachea."""
    if _OIDC or not THUNDER_ISSUER:
        return _OIDC
    try:
        with urllib.request.urlopen(
            f"{THUNDER_ISSUER}/.well-known/openid-configuration", timeout=10
        ) as resp:
            _OIDC.update(json.loads(resp.read().decode()))
    except Exception as exc:
        print(f"  \033[33m⚠ no se pudo leer el .well-known de ThunderID: {exc}\033[0m")
    return _OIDC


def _sesion_de(handler):
    galleta = handler.headers.get("Cookie", "")
    for trozo in galleta.split(";"):
        nombre, _, valor = trozo.strip().partition("=")
        if nombre == "ocr_sesion":
            return _SESIONES.get(valor)
    return None


def _url_de_autorizacion(redirect_uri):
    """Monta la URL de login con PKCE. Devuelve (url, sid, datos_de_sesion)."""
    verificador = secrets.token_urlsafe(64)
    reto = base64.urlsafe_b64encode(
        hashlib.sha256(verificador.encode()).digest()
    ).rstrip(b"=").decode()
    estado = secrets.token_urlsafe(24)
    sid = secrets.token_urlsafe(32)

    parametros = {
        "response_type": "code",
        "client_id": THUNDER_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid profile email",
        "state": estado,
        "code_challenge": reto,
        "code_challenge_method": "S256",
    }
    endpoint = _descubrir().get("authorization_endpoint", f"{THUNDER_ISSUER}/oauth2/authorize")
    url = f"{endpoint}?{urllib.parse.urlencode(parametros)}"
    return url, sid, {"estado": estado, "verificador": verificador, "redirect_uri": redirect_uri}


def _canjear_codigo(codigo, sesion):
    """Cambia el code por tokens. El secret no sale nunca del servidor."""
    endpoint = _descubrir().get("token_endpoint", f"{THUNDER_ISSUER}/oauth2/token")
    cuerpo = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": codigo,
        "redirect_uri": sesion["redirect_uri"],
        "client_id": THUNDER_CLIENT_ID,
        "code_verifier": sesion["verificador"],
    }).encode()
    cabeceras = {"Content-Type": "application/x-www-form-urlencoded"}
    if THUNDER_CLIENT_SECRET:
        credencial = base64.b64encode(
            f"{THUNDER_CLIENT_ID}:{THUNDER_CLIENT_SECRET}".encode()
        ).decode()
        cabeceras["Authorization"] = f"Basic {credencial}"

    req = urllib.request.Request(endpoint, data=cuerpo, headers=cabeceras, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _claims(token):
    """Payload del JWT, sin verificar firma.

    Aqui solo sirve para enseñar el nombre en la interfaz. Quien valida de
    verdad es el agente, que comprueba la firma contra el JWKS de ThunderID.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def reenviar(imagen: bytes, nombre: str, tipo: str, origen: str = "fichero",
             token_usuario: str = ""):
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
    # El agente valida este token contra el JWKS de ThunderID y publica el
    # `sub` como user.id en la traza. Sin el, el analisis sale sin persona.
    if token_usuario:
        cabeceras["Authorization"] = f"Bearer {token_usuario}"

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

    def _responder(self, codigo, cuerpo, tipo="application/json; charset=utf-8", cookie=None):
        datos = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        # Sin esto el navegador se queda con la versión cacheada de chat.html y
        # los cambios no aparecen aunque recargues. En una demo que se retoca
        # sobre la marcha, eso hace perder un rato buscando un fallo que no existe.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(datos)

    def _redirigir(self, destino, cookie=None):
        self.send_response(302)
        self.send_header("Location", destino)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _base(self):
        anfitrion = self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"
        return f"http://{anfitrion}"

    def do_GET(self):
        ruta, _, consulta = self.path.partition("?")

        if ruta in ("/", "/index.html", "/chat.html"):
            if LOGIN_ACTIVO and not _sesion_de(self):
                return self._redirigir("/login")
            with open(os.path.join(AQUI, "chat.html"), "rb") as fh:
                return self._responder(200, fh.read(), "text/html; charset=utf-8")

        if ruta == "/login":
            if not LOGIN_ACTIVO:
                return self._responder(503, {"error": "login no configurado"})
            url, sid, sesion = _url_de_autorizacion(f"{self._base()}/callback")
            _SESIONES[sid] = sesion
            galleta = f"ocr_sesion={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age=3600"
            return self._redirigir(url, galleta)

        if ruta == "/callback":
            parametros = urllib.parse.parse_qs(consulta)
            sesion = _sesion_de(self)
            if not sesion:
                return self._responder(400, {"error": "sesión no encontrada; vuelve a /login"})
            if parametros.get("error"):
                return self._responder(400, {"error": parametros["error"][0]})
            # Sin comprobar el state, cualquiera podria inducir un login ajeno.
            if parametros.get("state", [""])[0] != sesion.get("estado"):
                return self._responder(400, {"error": "state no coincide"})
            try:
                tokens = _canjear_codigo(parametros.get("code", [""])[0], sesion)
            except Exception as exc:
                return self._responder(502, {"error": "no se pudo canjear el código",
                                             "detalle": str(exc)[:300]})
            sesion["access_token"] = tokens.get("access_token", "")
            claims = _claims(tokens.get("id_token") or sesion["access_token"])
            # Solo lo justo para saludar en la interfaz; el sub es lo que cuenta.
            sesion["usuario"] = {
                "sub": claims.get("sub"),
                "nombre": claims.get("username") or claims.get("preferred_username")
                or claims.get("email") or claims.get("sub"),
            }
            sesion.pop("verificador", None)
            return self._redirigir("/")

        if ruta == "/logout":
            galleta = self.headers.get("Cookie", "")
            for trozo in galleta.split(";"):
                nombre, _, valor = trozo.strip().partition("=")
                if nombre == "ocr_sesion":
                    _SESIONES.pop(valor, None)
            return self._redirigir("/", "ocr_sesion=; Path=/; Max-Age=0")

        if ruta == "/api/config":
            sesion = _sesion_de(self) or {}
            return self._responder(200, {
                "agente_url": AGENTE_URL,
                "con_credencial": bool(API_KEY),
                "login_activo": LOGIN_ACTIVO,
                "usuario": sesion.get("usuario"),
            })
        return self._responder(404, {"error": "no encontrado"})

    def do_POST(self):
        if self.path != "/api/analizar":
            return self._responder(404, {"error": "no encontrado"})

        sesion = _sesion_de(self)
        if LOGIN_ACTIVO and not sesion:
            return self._responder(401, {
                "error": "login requerido",
                "pista": "Inicia sesión en ThunderID antes de analizar un justificante.",
            })

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

        sesion = sesion or {}
        codigo, respuesta = reenviar(imagen, nombre, tipo, origen,
                                     sesion.get("access_token", ""))
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
    print(f"  login       {THUNDER_ISSUER if LOGIN_ACTIVO else 'desactivado (sin THUNDER_ISSUER/CLIENT_ID)'}")
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
