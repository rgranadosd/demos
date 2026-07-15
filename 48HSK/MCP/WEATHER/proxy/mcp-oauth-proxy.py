#!/usr/bin/env python3
"""
Proxy OAuth de USUARIO para el MCP WeatherMCP (demo WSO2).

    VS Code ──http──> 127.0.0.1:9096 (este proxy) ──http──> APIM gateway :8280
                          │
                          └── authorization_code + PKCE contra WSO2 IS :9453
                              (login de USUARIO en el navegador). Renueva con
                              refresh_token; si caduca, vuelve a pedir login.

A diferencia de mcp-auth-proxy.py (client_credentials = identidad de la app),
aqui la identidad es la del USUARIO que inicia sesion. El IDE nunca ve un 401 ni
maneja OAuth: todo el flujo vive en este proxy, que SI levanta bien el callback
(lo que mcp-remote hacia mal). El gateway sigue validando JWT + suscripcion.

Autenticacion PEREZOSA (lazy), tal como marca la especificacion de MCP:
https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
El flujo canonico del spec es "peticion sin token -> 401 -> AHI empieza el OAuth".
Por eso este proxy NO se autentica al arrancar: arranca y escucha al instante, sin
token. Cada peticion se reenvia con el token en cache si hay uno valido, o SIN
cabecera Authorization si no lo hay (initialize/tools-list ya pasan asi por el
gateway). Solo cuando el gateway responde 401 de verdad se dispara el login (o el
refresh silencioso si hay refresh_token). Es decir: el usuario ve el login la
PRIMERA VEZ que usa de verdad una tool, no al levantar el servicio.

Uso:  python3 mcp-oauth-proxy.py           (login perezoso; SSO silencioso si hay sesion)
      python3 mcp-oauth-proxy.py --login   (en el primer uso real de una tool, fuerza
                                             la pantalla de login del IS en vez de SSO)
Credenciales del cliente OAuth: fichero "clientId:clientSecret" (CREDS_FILE) o env MCP_PROXY_CREDS.
"""
import base64
import hashlib
import http.client
import http.server
import json
import os
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse

LISTEN_HOST, LISTEN_PORT = "127.0.0.1", 9096            # lo que consume VS Code
GATEWAY_HOST, GATEWAY_PORT = "localhost", 8280           # APIM gateway (http)
IS_HOST, IS_PORT = "localhost", 9453                     # WSO2 IS (https)
SERVER_URL = "https://localhost:8243/weather-mcp/1.0.0/mcp"   # resource (audience)
CALLBACK_PORT = 9696
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/oauth/callback"
SCOPE = "openid email profile"
# CA de la demo (para validar TLS del IS). Por defecto la del stack WSO2; se
# puede sobreescribir con la env MCP_PROXY_CA.
CA_BUNDLE = os.environ.get(
    "MCP_PROXY_CA",
    "/Users/rafaelgd/Develop/wso2/demos/apim/.vscode/wso2-demo-ca.pem",
)
CREDS_FILE = os.environ.get("MCP_PROXY_CREDS_FILE", "/tmp/is_mcp_app_creds.txt")
REFRESH_MARGIN = 120

_lock = threading.Lock()
_tok = {"access": None, "refresh": None, "expires_at": 0, "user": None}
# Lista de 1 elemento (mutable) para poder "consumir" el flag --login la primera
# vez que de verdad haga falta autenticar, y no forzarlo en cada renovacion.
_startup_force_login = ["--login" in sys.argv]


def _creds():
    creds = os.environ.get("MCP_PROXY_CREDS")
    if not creds:
        with open(CREDS_FILE) as fh:
            creds = fh.read().strip()
    cid, csec = creds.split(":", 1)
    return cid, csec


def _is_ctx():
    return ssl.create_default_context(cafile=CA_BUNDLE)


def _basic():
    cid, csec = _creds()
    return base64.b64encode(f"{cid}:{csec}".encode()).decode(), cid


def _post_token(params):
    basic, _ = _basic()
    conn = http.client.HTTPSConnection(IS_HOST, IS_PORT, context=_is_ctx(), timeout=20)
    conn.request("POST", "/oauth2/token", urllib.parse.urlencode(params), {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    r = conn.getresponse(); data = json.loads(r.read()); conn.close()
    return data


def _store(data):
    _tok["access"] = data["access_token"]
    _tok["refresh"] = data.get("refresh_token", _tok["refresh"])
    _tok["expires_at"] = time.time() + int(data.get("expires_in", 3600))
    if data.get("id_token"):
        _tok["id_token"] = data["id_token"]
        try:
            p = data["id_token"].split(".")[1]; p += "=" * (-len(p) % 4)
            _tok["user"] = json.loads(base64.urlsafe_b64decode(p)).get("sub")
        except Exception:
            pass
    sys.stderr.write(f"[proxy] token de usuario '{_tok['user']}' (expira en {int(data.get('expires_in',0))}s)\n")


def _browser_logout():
    # Logout OIDC en el navegador con el id_token del propio usuario: limpia la
    # sesion SSO y la cookie "recuerdame". Sin credenciales de admin y valido para
    # CUALQUIER usuario. id_token_hint evita la pagina de confirmacion.
    idt = _tok.get("id_token")
    if not idt:
        return
    url = f"https://{IS_HOST}:{IS_PORT}/oidc/logout?" + urllib.parse.urlencode({"id_token_hint": idt})
    sys.stderr.write("[proxy] logout OIDC en el navegador (limpia sesion + recuerdame)...\n")
    subprocess.run(["open", url])
    time.sleep(4)  # dejar que el navegador procese el logout y borre las cookies


def _interactive_login(force=False):
    if force:
        _browser_logout()   # limpia sesion SSO + recuerdame para que el IS pida credenciales
    _, cid = _basic()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_hex(16)
    params = {
        "response_type": "code", "client_id": cid,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI, "state": state,
        "scope": SCOPE, "resource": SERVER_URL,
    }
    if force:
        params["prompt"] = "login"
    auth_url = f"https://{IS_HOST}:{IS_PORT}/oauth2/authorize?" + urllib.parse.urlencode(params)

    result = {}
    done = threading.Event()

    class CB(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            if q.get("code") and q.get("state") == state:
                result["code"] = q["code"]
                self.wfile.write("<h2>Login OK. Vuelve a VS Code.</h2>".encode())
            else:
                result["error"] = q.get("error", "respuesta inesperada")
                self.wfile.write(f"<h2>Error: {result['error']}</h2>".encode())
            done.set()

        def log_message(self, *a):
            pass

    class V6(http.server.HTTPServer):
        import socket
        address_family = socket.AF_INET6

    try:
        cb = V6(("::", CALLBACK_PORT), CB)
    except OSError:
        cb = http.server.HTTPServer(("0.0.0.0", CALLBACK_PORT), CB)
    threading.Thread(target=cb.serve_forever, daemon=True).start()
    sys.stderr.write("[proxy] abriendo navegador para login del IS...\n")
    subprocess.run(["open", auth_url])
    if not done.wait(timeout=300):
        cb.shutdown(); raise RuntimeError("timeout esperando el login")
    cb.shutdown()
    if "error" in result:
        raise RuntimeError(f"login error: {result['error']}")
    data = _post_token({
        "grant_type": "authorization_code", "code": result["code"],
        "redirect_uri": REDIRECT_URI, "code_verifier": verifier, "resource": SERVER_URL,
    })
    if "access_token" not in data:
        raise RuntimeError(f"token endpoint: {data}")
    _store(data)


def ensure_token(force_login=False):
    """Consigue un token, renovando o pidiendo login si hace falta. SOLO se debe
    llamar cuando ya sabemos que hace falta un token real (p.ej. tras un 401 del
    gateway) — no es la funcion que se usa para el chequeo barato de cada peticion,
    ese es _cached_token()."""
    with _lock:
        if not force_login and _tok["access"] and time.time() < _tok["expires_at"] - REFRESH_MARGIN:
            return _tok["access"]
        # intentar refresh silencioso
        if not force_login and _tok["refresh"]:
            data = _post_token({"grant_type": "refresh_token", "refresh_token": _tok["refresh"],
                                "scope": SCOPE, "resource": SERVER_URL})
            if "access_token" in data:
                _store(data); return _tok["access"]
            sys.stderr.write("[proxy] refresh fallo; se requiere login\n")
        _interactive_login(force=force_login)
        return _tok["access"]


def _cached_token():
    """Token valido YA en memoria, sin tocar la red ni disparar login. Se llama en
    CADA peticion (incluidas initialize/tools-list, que el gateway deja pasar sin
    token): si no hay token util, se reenvia sin cabecera Authorization y se deja
    que sea el propio gateway quien decida si la peticion necesitaba autenticarse."""
    if _tok["access"] and time.time() < _tok["expires_at"] - REFRESH_MARGIN:
        return _tok["access"]
    return None


def _consume_startup_force_login():
    # Aplica el --login (si se paso) UNA sola vez, en el primer login real que
    # haga falta; las siguientes renovaciones ya no fuerzan la pantalla de login.
    if _startup_force_login[0]:
        _startup_force_login[0] = False
        return True
    return False


HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer",
       "proxy-authorization", "proxy-authenticate", "upgrade", "host",
       "content-length", "authorization"}


def _inject_greeting(raw, user):
    # Prepone "Hola <usuario>" al texto que devuelven las tools, para que se vea
    # en el chat QUIEN esta autenticado. El nombre sale del token OAuth (sub), no
    # esta hardcodeado: cambia solo segun quien haga login.
    if not user or not raw:
        return raw

    def fix(obj):
        if not isinstance(obj, dict):
            return
        res = obj.get("result")
        if not isinstance(res, dict):
            return
        # Instruccion + saludo: Copilot resume la salida de la tool, asi que le
        # pedimos explicitamente que salude por su nombre (y el nombre queda tambien
        # visible en la salida cruda del tool call).
        greet = (f"[IMPORTANTE para el asistente: el usuario autenticado es '{user}'. "
                 f"Empieza tu respuesta saludandole: \"Hola {user}\".]\n\n"
                 f"Hola {user} \U0001F44B\n\n")
        marker = "[IMPORTANTE para el asistente"
        content = res.get("content")
        if isinstance(content, list):
            for item in content:
                if (isinstance(item, dict) and item.get("type") == "text"
                        and isinstance(item.get("text"), str)
                        and marker not in item["text"]):
                    item["text"] = greet + item["text"]
        sc = res.get("structuredContent")
        if isinstance(sc, dict) and isinstance(sc.get("result"), str) and marker not in sc["result"]:
            sc["result"] = greet + sc["result"]

    text = raw.decode("utf-8", "replace")
    if "data:" in text:  # SSE
        lines = []
        for line in text.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
                try:
                    obj = json.loads(payload)
                    fix(obj)
                    line = "data: " + json.dumps(obj, ensure_ascii=False)
                except Exception:
                    pass
            lines.append(line)
        return "\n".join(lines).encode("utf-8")
    try:
        obj = json.loads(text)
        fix(obj)
        return json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except Exception:
        return raw


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _relogin(self):
        # Fuerza la pantalla de login del IS y re-identifica al usuario, SIN
        # reiniciar el proxy. Metodo para la demo: abrir http://127.0.0.1:9096/relogin
        try:
            ensure_token(force_login=True)
            body = f"Re-login OK. Usuario: {_tok['user']}".encode()
            code = 200
        except Exception as e:
            body = f"Re-login fallo: {e}".encode()
            code = 500
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self):
        if urllib.parse.urlparse(self.path).path.rstrip("/") in ("/relogin", "/__relogin"):
            self._relogin()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        for attempt in (1, 2):
            # Chequeo barato: token en cache o nada. NO se autentica todavia.
            token = _cached_token()
            if token is None and attempt == 2:
                # Solo llegamos aqui si el gateway acaba de responder 401 de
                # verdad (ver abajo). Es el UNICO punto donde se dispara el
                # login/refresh: autenticacion perezosa, como marca el spec de MCP.
                force = _consume_startup_force_login()
                token = ensure_token(force_login=force)
            conn = http.client.HTTPConnection(GATEWAY_HOST, GATEWAY_PORT, timeout=120)
            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in HOP and k.lower() != "accept-encoding"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            headers["Host"] = f"{GATEWAY_HOST}:{GATEWAY_PORT}"
            if body is not None:
                headers["Content-Length"] = str(len(body))
            conn.request(self.command, self.path, body, headers)
            resp = conn.getresponse()
            if resp.status == 401 and attempt == 1:
                sys.stderr.write("[proxy] 401 del gateway; autenticando (bajo demanda)...\n")
                with _lock:
                    _tok["expires_at"] = 0   # invalida el cache para forzar refresh/login en el intento 2
                conn.close()
                continue
            break
        # Bufferizamos la respuesta para poder inyectar el saludo del usuario.
        payload = _inject_greeting(resp.read(), _tok.get("user"))
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        conn.close()

    do_GET = do_POST = do_DELETE = do_PUT = _forward

    def log_message(self, fmt, *a):
        sys.stderr.write("[proxy] %s\n" % (fmt % a))


if __name__ == "__main__":
    # Arranca y escucha SIN autenticar: el login (o el --login forzado) se
    # dispara en el primer uso real de una tool, no aqui.
    srv = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    sys.stderr.write(
        f"[proxy] escuchando en http://{LISTEN_HOST}:{LISTEN_PORT} -> gateway {GATEWAY_HOST}:{GATEWAY_PORT} "
        "(identidad de USUARIO; login pendiente hasta el primer uso real de una tool)\n"
    )
    srv.serve_forever()
