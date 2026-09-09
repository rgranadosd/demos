#!/usr/bin/env python3
"""Cambia la marca de la demo en los cuatro sitios donde vive.

    ./branding/rebrand.py "Madrid Digital" branding/assets/images/madrid-digital-logo.svg

Lo que toca, en orden:

  1. ThunderID / aplicacion OCR Chat -> `logoUrl` con el logo embebido en un
     `data:` URI. Es el unico hueco de imagen que el Gate 0.45 renderiza de
     verdad; el `images.logo` del tema se guarda pero no se pinta.
  2. ThunderID / `gate-config.js` -> `product_name`, favicon y logo del tema,
     todos apuntando al mismo `data:` URI mediante una variable `__LOGO__`.
  3. Cliente local -> el `<img>` de la caja `empresa` en `chat.html`, su ruta
     en el mapa de assets de `servidor.py`, y `branding.yml`.

Por que `data:` URI y no ficheros montados: montar el logo con `subPath` sobre
`assets/images/*.svg` tumba el pod de ThunderID -- kubelet falla al crear el
punto de montaje cuando hay varios subPath del mismo volumen en un directorio
("cannot create subdirectories ... not a directory"). Embebido en el config no
hay montaje, no hay ConfigMap de assets y el branding sobrevive a que se
recree el cluster.

Requiere: kubectl con contexto al cluster, node (para validar el JS antes de
aplicarlo) y la variable THUNDER_SYSTEM_SECRET con el secreto de
`amp-system-client`. Si no esta, se lee del Secret del cluster.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request

AQUI = pathlib.Path(__file__).resolve().parent
RAIZ = AQUI.parent

THUNDER = os.environ.get("THUNDER_URL", "http://default-default.thunder.amp.localhost:8080")
NS = os.environ.get("THUNDER_NS", "amp-thunder-default-default")
DEPLOY = "amp-thunder-default-default-deployment"
CM_CONFIG = "amp-thunder-default-default-config-map"
APP_ID = os.environ.get("OCR_CHAT_APP_ID", "01a070d5-1eb7-7eea-a3fa-3b3c7255f6d4")
CLIENT_ID = "amp-system-client"


def sh(*args: str, entrada: bytes | None = None) -> str:
    r = subprocess.run(args, input=entrada, capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"fallo: {' '.join(args)}\n{r.stderr.decode(errors='replace')[:400]}")
    return r.stdout.decode()


def paso(texto: str) -> None:
    print(f"\n\033[1m» {texto}\033[0m")


def secreto_sistema() -> str:
    s = os.environ.get("THUNDER_SYSTEM_SECRET", "").strip()
    if s:
        return s
    b64 = sh("kubectl", "get", "secret", "amp-thunder-default-default-system-client",
             "-n", NS, "-o", "jsonpath={.data.client-secret}").strip()
    if not b64:
        raise SystemExit("no hay THUNDER_SYSTEM_SECRET ni se pudo leer el Secret del cluster")
    return base64.b64decode(b64).decode()


def token() -> str:
    datos = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "system"}).encode()
    cred = base64.b64encode(f"{CLIENT_ID}:{secreto_sistema()}".encode()).decode()
    req = urllib.request.Request(f"{THUNDER}/oauth2/token", data=datos,
                                 headers={"Authorization": f"Basic {cred}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)["access_token"]


def api(metodo: str, ruta: str, cuerpo: dict | None, tok: str) -> dict:
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(f"{THUNDER}{ruta}", data=datos, method=metodo,
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def data_uri(fichero: pathlib.Path) -> str:
    tipo = mimetypes.guess_type(fichero.name)[0] or "application/octet-stream"
    return f"data:{tipo};base64," + base64.b64encode(fichero.read_bytes()).decode()


# --------------------------------------------------------------- ThunderID

def app_logo(uri: str, tok: str) -> None:
    """PUT completo: la API no soporta PATCH (405) y el export no trae el
    clientSecret, asi que hay que reponerlo del .env o el login se rompe."""
    app = api("GET", f"/applications/{APP_ID}", None, tok)
    (AQUI / "backup-ocrchat.json").write_text(json.dumps(app, indent=1))

    env = (RAIZ / ".env").read_text()
    m = re.search(r"^THUNDER_CLIENT_SECRET=(.+)$", env, re.M)
    if not m:
        raise SystemExit("falta THUNDER_CLIENT_SECRET en ocr-agent/.env")
    secreto = m.group(1).strip().strip("\"'")

    app["logoUrl"] = uri
    for cfg in app.get("inboundAuthConfig", []):
        if cfg.get("type") == "oauth2":
            cfg["config"]["clientSecret"] = secreto

    api("PUT", f"/applications/{APP_ID}", app, tok)
    de_vuelta = api("GET", f"/applications/{APP_ID}", None, tok)
    assert de_vuelta.get("logoUrl") == uri, "el logoUrl no se guardo"
    print(f"   logoUrl actualizado ({len(uri)} caracteres) y verificado")


def gate_config(nombre: str, uri: str) -> None:
    cm = json.loads(sh("kubectl", "get", "cm", CM_CONFIG, "-n", NS, "-o", "json"))
    (AQUI / "backup-gate-config.js").write_text(cm["data"]["gate-config.js"])
    cfg = cm["data"]["gate-config.js"]

    # el logo, una sola vez, en una variable
    cfg = re.sub(r'^var __LOGO__ = "[^"]*";\n\n', "", cfg, flags=re.M)
    cfg = cfg.replace("window.__THUNDERID_RUNTIME_CONFIG__ = {",
                      f'var __LOGO__ = "{uri}";\n\nwindow.__THUNDERID_RUNTIME_CONFIG__ = {{', 1)
    cfg = re.sub(r'"assets/images/[^"]*"', "__LOGO__", cfg)

    # la marca: product_name, label del tema, alt y title del logo
    cfg = re.sub(r'product_name: "[^"]*"', f'product_name: "{nombre}"', cfg)
    for campo in ("label", "alt", "title"):
        cfg = re.sub(rf'{campo}: "[^"]*"', f'{campo}: "{nombre}"', cfg)

    # se valida ejecutandolo: un config.js roto deja el Gate en blanco
    prueba = AQUI / ".gate-config.check.js"
    prueba.write_text("global.window = global;\n" + cfg +
                      "\nconst b = __THUNDERID_RUNTIME_CONFIG__.brand;"
                      "\nif (!b.product_name || !b.favicon.light.startsWith('data:'))"
                      " { throw new Error('config incompleto'); }"
                      "\nconsole.log('   config.js valido | product_name:', b.product_name);")
    try:
        print(sh("node", str(prueba)).rstrip())
    finally:
        prueba.unlink(missing_ok=True)

    parche = json.dumps({"data": {"gate-config.js": cfg}})
    sh("kubectl", "patch", "cm", CM_CONFIG, "-n", NS, "--type=merge", "--patch-file", "/dev/stdin",
       entrada=parche.encode())
    print("   ConfigMap parcheado")


def reiniciar_thunder(nombre: str) -> None:
    sh("kubectl", "rollout", "restart", "deploy", DEPLOY, "-n", NS)
    print("   reiniciando", end="", flush=True)
    for _ in range(40):
        time.sleep(5)
        print(".", end="", flush=True)
        filas = [l.split() for l in sh("kubectl", "get", "pods", "-n", NS,
                                       "--no-headers").splitlines() if l.strip()]
        if len(filas) == 1 and filas[0][2] == "Running":
            try:
                with urllib.request.urlopen(f"{THUNDER}/gate/config.js", timeout=5) as r:
                    servido = r.read().decode()
                if f'"{nombre}"' in servido and "data:" in servido:
                    print(f"\n   pod listo y sirviendo la marca «{nombre}»")
                    return
            except Exception:
                pass
    raise SystemExit("\n   el pod no llego a servir la marca nueva; revisa `kubectl get pods -n " + NS + "`")


# ------------------------------------------------------------ cliente local

def cliente(nombre: str, logo: pathlib.Path) -> None:
    destino = AQUI / "assets" / "images" / logo.name
    if logo.resolve() != destino.resolve():
        shutil.copy2(logo, destino)
    ruta_web = f"/branding/assets/images/{logo.name}"
    tipo = mimetypes.guess_type(logo.name)[0] or "application/octet-stream"

    # chat.html: el <img> de la caja "empresa"
    p = RAIZ / "chat.html"
    s = p.read_text()
    s2 = re.sub(r'(<div class="empresa"><img src=")[^"]*(" alt=")[^"]*(">)',
                rf'\g<1>{ruta_web}\g<2>{nombre}\g<3>', s)
    if s2 == s:
        raise SystemExit("no encontre el <img> de la caja «empresa» en chat.html")
    p.write_text(s2)

    # servidor.py: la ruta en el mapa de assets
    p = RAIZ / "servidor.py"
    s = p.read_text()
    if ruta_web not in s:
        ancla = '        if ruta in assets:'
        entrada = f'            "{ruta_web}": (\n                "{logo.name}", "{tipo}"\n            ),\n        }}\n'
        s = s.replace("        }\n" + ancla, entrada + ancla, 1)
        p.write_text(s)
    sh(sys.executable, "-m", "py_compile", str(p))

    # branding.yml: la fuente declarativa
    p = RAIZ / "branding.yml"
    s = p.read_text()
    s = re.sub(r"product_name: .*", f"product_name: {nombre}", s)
    s = re.sub(r"assets/images/[^\s]+\.(svg|png|jpg)", f"assets/images/{logo.name}", s)
    p.write_text(s)
    print(f"   chat.html, servidor.py y branding.yml -> {ruta_web}")


def reiniciar_cliente() -> None:
    salida = subprocess.run(["lsof", "-nP", "-iTCP:8800", "-sTCP:LISTEN"],
                            capture_output=True).stdout.decode()
    for linea in salida.splitlines()[1:]:
        os.kill(int(linea.split()[1]), 15)
    subprocess.Popen(["./servidor.py"], cwd=RAIZ,
                     stdout=open("/tmp/ocr-servidor.log", "wb"), stderr=subprocess.STDOUT)
    for _ in range(20):
        time.sleep(0.5)
        if subprocess.run(["lsof", "-nP", "-iTCP:8800", "-sTCP:LISTEN"],
                          capture_output=True).stdout:
            print("   cliente local reiniciado en http://127.0.0.1:8800")
            return
    raise SystemExit("el cliente local no volvio a levantar; mira /tmp/ocr-servidor.log")


def main() -> None:
    ap = argparse.ArgumentParser(description="Cambia la marca de la demo en ThunderID y en el cliente.")
    ap.add_argument("nombre", help='nombre de la marca, p.ej. "Madrid Digital"')
    ap.add_argument("logo", type=pathlib.Path, help="fichero SVG o PNG del logo")
    ap.add_argument("--solo-cliente", action="store_true", help="no tocar ThunderID")
    args = ap.parse_args()

    if not args.logo.is_file():
        raise SystemExit(f"no existe {args.logo}")
    uri = data_uri(args.logo)
    print(f"marca «{args.nombre}» · {args.logo.name} · {args.logo.stat().st_size} bytes "
          f"· data URI de {len(uri)} caracteres")

    if not args.solo_cliente:
        tok = token()
        paso("ThunderID · logo de la aplicacion OCR Chat")
        app_logo(uri, tok)
        paso("ThunderID · gate-config.js")
        gate_config(args.nombre, uri)
        paso("ThunderID · reinicio")
        reiniciar_thunder(args.nombre)

    paso("Cliente local")
    cliente(args.nombre, args.logo)
    reiniciar_cliente()

    print(f"\n\033[1mListo.\033[0m Recarga con ⇧⌘R. Vuelve a entrar: la sesion se ha caido.")


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (solo lo usa token())
    main()
