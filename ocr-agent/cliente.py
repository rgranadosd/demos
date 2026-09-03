#!/usr/bin/env python3
"""Cliente externo del agente OCR de gastos.

Demuestra lo que importa: que el agente es alcanzable **desde fuera de Agent
Manager**, atravesando el gateway con su API key, sin túneles ni kubectl. Le
pasa la foto de un justificante y enseña el resultado por pantalla.

Solo usa la librería estándar: se puede copiar a cualquier máquina con Python 3
y funciona. Nada de pip install en mitad de una demo.

Uso:
    ./cliente.py ticket_ok.png
    ./cliente.py foto.jpg --url http://.../ocr-agent-ocr-agent-endpoint --api-key XXX
    ./cliente.py ticket_mal.png --json      # la respuesta cruda

Configuración por entorno (evita repetir argumentos):
    export OCR_AGENT_URL="http://default-default.am-gateway.localhost:19080/ocr-agent-ocr-agent-endpoint"
    export OCR_AGENT_API_KEY="tu-clave"
"""

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

URL_POR_DEFECTO = os.getenv(
    "OCR_AGENT_URL",
    "http://default-default.am-gateway.localhost:19080/ocr-agent-ocr-agent-endpoint",
)

# ─── presentación ────────────────────────────────────────────────────────────

VERDE, ROJO, AMARILLO, AZUL, GRIS, NEGRITA, FIN = (
    "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[90m", "\033[1m", "\033[0m"
)


def sin_color():
    global VERDE, ROJO, AMARILLO, AZUL, GRIS, NEGRITA, FIN
    VERDE = ROJO = AMARILLO = AZUL = GRIS = NEGRITA = FIN = ""


def titulo(texto):
    print(f"\n{NEGRITA}  ── {texto} {'─' * max(0, 58 - len(texto))}{FIN}")


def campo(etiqueta, valor):
    if valor is None or valor == "":
        valor = f"{GRIS}(no consta){FIN}"
    print(f"   {etiqueta:<14} {valor}")


def dinero(valor, moneda):
    if valor is None:
        return None
    return f"{valor:,.2f} {moneda or ''}".replace(",", " ").replace(".", ",").strip()


# ─── envío ───────────────────────────────────────────────────────────────────


def multipart(ruta):
    """Construye un cuerpo multipart/form-data a mano.

    `requests` haría esto en una línea, pero obligaría a instalarlo. Para un
    cliente de demostración que debe arrancar en cualquier portátil, veinte
    líneas de estándar salen más baratas que una dependencia.
    """
    frontera = f"----ocr{uuid.uuid4().hex}"
    nombre = os.path.basename(ruta)
    tipo = mimetypes.guess_type(nombre)[0] or "application/octet-stream"
    with open(ruta, "rb") as fh:
        contenido = fh.read()

    cuerpo = b"".join([
        f"--{frontera}\r\n".encode(),
        f'Content-Disposition: form-data; name="fichero"; filename="{nombre}"\r\n'.encode(),
        f"Content-Type: {tipo}\r\n\r\n".encode(),
        contenido,
        f"\r\n--{frontera}--\r\n".encode(),
    ])
    return cuerpo, f"multipart/form-data; boundary={frontera}", tipo, len(contenido)


def analizar(url_base, ruta, api_key, timeout):
    cuerpo, content_type, tipo_fichero, tam = multipart(ruta)
    url = url_base.rstrip("/") + "/gastos/analizar/fichero"

    cabeceras = {"Content-Type": content_type}
    if api_key:
        # El gateway autentica al consumidor con la clave en esta cabecera.
        cabeceras["X-API-Key"] = api_key

    print(f"   {GRIS}fichero  {os.path.basename(ruta)} · {tipo_fichero} · {tam:,} bytes{FIN}"
          .replace(",", "."))
    print(f"   {GRIS}destino  {url}{FIN}")
    print(f"   {GRIS}auth     {'X-API-Key' if api_key else 'sin credencial'}{FIN}")
    print(f"\n   analizando…", end="", flush=True)

    inicio = time.time()
    peticion = urllib.request.Request(url, data=cuerpo, headers=cabeceras, method="POST")
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as resp:
            datos = json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        detalle = err.read().decode(errors="replace")[:400]
        print(f"\r   {ROJO}HTTP {err.code}{FIN}                    ")
        if err.code == 401:
            print(f"   {GRIS}Falta la API key del agente, o no es válida."
                  f" Créala en la consola y pásala con --api-key.{FIN}")
        elif err.code == 404:
            print(f"   {GRIS}La ruta no existe. Revisa --url.{FIN}")
        print(f"   {detalle}")
        sys.exit(1)
    except urllib.error.URLError as err:
        print(f"\r   {ROJO}sin conexión{FIN}                    ")
        print(f"   {err.reason}")
        sys.exit(1)

    print(f"\r   {VERDE}respuesta en {time.time() - inicio:.1f}s{FIN}          ")
    return datos


# ─── informe ─────────────────────────────────────────────────────────────────


def mostrar(d):
    g = d.get("gasto", {})
    moneda = g.get("moneda")

    titulo("JUSTIFICANTE")
    campo("Tipo", g.get("tipo_documento"))
    campo("Comercio", g.get("comercio"))
    campo("Fecha", g.get("fecha"))
    campo("Total", dinero(g.get("total"), moneda))
    campo("Base", dinero(g.get("base_imponible"), moneda))
    campo("Impuestos", dinero(g.get("impuestos"), moneda))
    campo("Pago", g.get("metodo_pago"))
    campo("Categoría", g.get("categoria_estimada"))
    if g.get("resumen"):
        campo("Resumen", g["resumen"])

    lineas = g.get("lineas_principales") or []
    if lineas:
        titulo(f"LÍNEAS ({len(lineas)})")
        for l in lineas:
            cant = l.get("cantidad")
            cant = f"{cant:g}×" if cant else "  "
            imp = dinero(l.get("importe"), "") or "—"
            print(f"   {cant:>4} {(l.get('descripcion') or ''):<34} {imp:>12}")

    cuadre = d.get("cuadre") or {}
    if cuadre:
        titulo("COMPROBACIONES ARITMÉTICAS")
        etiquetas = {
            "base_mas_impuestos_igual_total": "base + impuestos = total",
            "lineas_cuadran": "las líneas suman la base",
        }
        for clave, texto in etiquetas.items():
            if clave in cuadre:
                ok = cuadre[clave]
                marca = f"{VERDE}✓{FIN}" if ok else f"{ROJO}✗{FIN}"
                print(f"   {marca} {texto}")
        if cuadre.get("total_calculado") is not None:
            print(f"   {GRIS}total calculado: {cuadre['total_calculado']}"
                  f" · suma de líneas: {cuadre.get('suma_lineas')}{FIN}")
        for mala in cuadre.get("lineas_descuadradas") or []:
            print(f"   {ROJO}✗{FIN} {mala.get('descripcion')}: declara "
                  f"{mala.get('importe_declarado')}, calculado "
                  f"{mala.get('importe_calculado')}")

    avisos = d.get("advertencias") or []
    titulo("ADVERTENCIAS")
    if not avisos:
        print(f"   {VERDE}ninguna{FIN}")
    for a in avisos:
        print(f"   {AMARILLO}▲{FIN} {a}")

    if not d.get("legible", True):
        print(f"\n   {ROJO}La imagen no es suficientemente legible."
              f" Haz otra foto.{FIN}")

    tok = d.get("tokens") or {}
    print(f"\n   {GRIS}modelo {d.get('modelo')} · {tok.get('total', '?')} tokens"
          f" ({tok.get('entrada', '?')} entrada / {tok.get('salida', '?')} salida){FIN}")
    print(f"   {GRIS}el gasto NO se ha registrado: hace falta confirmación explícita{FIN}\n")


def main():
    p = argparse.ArgumentParser(
        description="Cliente externo del agente OCR de gastos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("fichero", help="imagen del ticket, factura o recibo")
    p.add_argument("--url", default=URL_POR_DEFECTO, help="URL base del agente")
    p.add_argument("--api-key", default=os.getenv("OCR_AGENT_API_KEY", ""),
                   help="clave del agente (cabecera X-API-Key)")
    p.add_argument("--timeout", type=int, default=300, help="segundos (def. 300)")
    p.add_argument("--json", action="store_true", help="imprime la respuesta cruda")
    p.add_argument("--no-color", action="store_true")
    args = p.parse_args()

    if args.no_color or not sys.stdout.isatty():
        sin_color()

    if not os.path.isfile(args.fichero):
        print(f"{ROJO}No existe el fichero: {args.fichero}{FIN}")
        sys.exit(2)

    print(f"\n{NEGRITA}  Agente OCR de gastos{FIN}")
    datos = analizar(args.url, args.fichero, args.api_key, args.timeout)

    if args.json:
        print(json.dumps(datos, indent=2, ensure_ascii=False))
    else:
        mostrar(datos)


if __name__ == "__main__":
    main()
