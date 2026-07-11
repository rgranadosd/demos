"""Trace en vivo de la arquitectura para el demo.

Pinta cada interacción con las piezas WSO2 con una etiqueta de color para que
el cliente vea el flujo en tiempo real:

    [IS]    -> WSO2 Identity Server (autenticación de usuario, tokens, SCIM)
    [APIM]  -> WSO2 API Manager / Gateway (tokens de app, LLM, Shopify, MCP)

Módulo sin dependencias internas (evita imports circulares): cualquier módulo
del agente puede hacer `from trace_log import trace`.
"""

from __future__ import annotations

import os
import sys
import threading

# Lock de salida COMPARTIDO: el spinner "pensando" (ui_console.ThinkingIndicator)
# y estas trazas escriben en el mismo terminal desde hilos distintos. Serializamos
# ambas escrituras con este lock para que nunca se solapen/entremezclen.
OUTPUT_LOCK = threading.RLock()

_RESET = "\033[0m"

# Etiqueta por sistema (negrita + color)
_TAGS = {
    "IS": "\033[1;35m",     # magenta  -> Identity Server (9453)
    "APIM": "\033[1;34m",   # azul     -> API Manager / Gateway (9443/8243)
}
_ACTION = "\033[36m"        # cian     -> qué se está haciendo
_DETAIL = "\033[2;37m"      # gris tenue -> detalle (endpoint, scope, etc.)
_STATUS_OK = "\033[1;32m"   # verde
_STATUS_ERR = "\033[1;31m"  # rojo

# Trazas activas por defecto; se pueden apagar por env (WSO2_TRACE=0) o en runtime
# con set_enabled(False) — p.ej. el flag --no-debug del CLI para un demo limpio.
_ENABLED = os.getenv("WSO2_TRACE", "1").strip().lower() not in ("0", "false", "no", "off")


def set_enabled(value: bool) -> None:
    """Activa/desactiva las trazas [IS]/[APIM] en tiempo de ejecución."""
    global _ENABLED
    _ENABLED = bool(value)


def is_enabled() -> bool:
    return _ENABLED


def _colorize() -> bool:
    # Colorea si es TTY o si se fuerza; sin color, sigue imprimiendo el texto plano.
    return sys.stdout.isatty() or os.getenv("WSO2_TRACE_FORCE_COLOR", "").strip().lower() in ("1", "true", "yes")


def trace(system: str, action: str, detail: str = "", status: str | int | None = None) -> None:
    """Imprime una línea de trace: [SYSTEM] acción  detalle  (status).

    system: "IS" | "APIM" (cualquier otro valor se pinta en cian).
    action: descripción corta de la operación.
    detail: endpoint, scope requerido, tool, etc. (opcional).
    status: código HTTP o estado; se colorea verde (2xx) / rojo (resto).
    """
    if not _ENABLED:
        return

    system = (system or "").upper()

    if _colorize():
        tag = _TAGS.get(system, "\033[1;36m")
        line = f"{tag}[{system}]{_RESET} {_ACTION}{action}{_RESET}"
        if detail:
            line += f"  {_DETAIL}{detail}{_RESET}"
        if status is not None:
            try:
                code = int(str(status).split()[0])
                ok = 200 <= code < 400
            except (ValueError, IndexError):
                ok = str(status).lower() in ("ok", "200")
            color = _STATUS_OK if ok else _STATUS_ERR
            line += f"  {color}[{status}]{_RESET}"
    else:
        line = f"[{system}] {action}"
        if detail:
            line += f"  {detail}"
        if status is not None:
            line += f"  [{status}]"

    # Borra la línea actual del terminal antes de imprimir, por si el spinner
    # (alineado a la derecha) dejó un resto. Serializado con el spinner via lock.
    with OUTPUT_LOCK:
        if sys.stdout.isatty():
            sys.stdout.write("\r\033[2K")
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def scope_for(method: str, path: str) -> str:
    """Devuelve el scope OAuth2 que el gateway exige para una operación Shopify
    (solo para mostrarlo en el trace; la imposición real la hace APIM)."""
    p = (path or "").split("?")[0]
    m = (method or "").upper()
    if m == "GET":
        return "view_products"
    if m == "PUT" and "/products/" in p:
        return "update_prices"
    # POST/DELETE sobre collects / custom_collections -> Home Page
    return "update_descriptions"
