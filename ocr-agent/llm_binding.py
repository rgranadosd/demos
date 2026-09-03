"""Descubrimiento del LLM que AMP le asigna al agente.

El agente NO decide contra qué modelo habla. Cuando se le engancha un LLM
provider desde Agent Manager, AMP inyecta en el pod un par de variables
``<PREFIJO>_<N>_URL`` + ``<PREFIJO>_<N>_API_KEY`` (por ejemplo
``TELXIUS_GASTOS_1_URL``). Esa URL es la del gateway, ya resoluble desde dentro
del cluster, y es la que hay que usar **tal cual**.

Ese es justo el mecanismo que permite cambiar de modelo sin tocar el código:
se cambia el provider en AMP, se redespliega, y el agente habla con otro LLM.

Este módulo replica el contrato que ya usan los agentes cpc-studio
(`common/llm_utils.py`), incluidas sus lecciones aprendidas:

- La autoridad inyectada se respeta. Reescribirla es como se provocaban 404:
  la petición aterrizaba en el edge de kgateway con un Host que no casaba con
  ninguna ruta.
- **No** se añade sufijo de path por defecto. El contrato de AMP es usar la URL
  como base del cliente OpenAI, que ya le pega ``/chat/completions``. Añadir un
  ``/v1`` de más hacía que Azure AI Foundry respondiera 404.
- Una URL externa (``*.localhost``) no resuelve desde un pod y sí hay que
  traducirla.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Optional
from urllib.parse import urlsplit

# TELXIUS_GASTOS_1_URL  ->  prefijo TELXIUS_GASTOS, indice 1
_BINDING_INDEXED = re.compile(r"^(?P<prefix>[A-Z0-9_]+?)_(?P<idx>\d+)_URL$")
# MISTRALAI_URL  ->  prefijo MISTRALAI
_BINDING_PLAIN = re.compile(r"^(?P<prefix>[A-Z0-9_]+?)_URL$")


class LLMBinding:
    def __init__(self, base_url: str, api_key: str, host: str, origen: str):
        self.base_url = base_url
        self.api_key = api_key
        self.host = host          # override de cabecera Host, vacío si no hace falta
        self.origen = origen      # qué variable lo resolvió, para diagnóstico

    def __repr__(self) -> str:  # pragma: no cover
        return f"LLMBinding(base_url={self.base_url!r}, origen={self.origen!r})"


def _buscar_par(env: Dict[str, str]):
    """Localiza el par URL/API_KEY que AMP haya inyectado."""
    for regexp, plantilla in (
        (_BINDING_INDEXED, "{prefix}_{idx}_API_KEY"),
        (_BINDING_PLAIN, "{prefix}_API_KEY"),
    ):
        for nombre, valor in env.items():
            match = regexp.match(nombre)
            if not match or not valor.strip().startswith("http"):
                continue
            hermano = plantilla.format(**match.groupdict())
            if hermano == nombre:
                continue
            if env.get(hermano, "").strip():
                return valor.strip(), env[hermano].strip(), nombre
    return "", "", ""


def resolver(env: Optional[Dict[str, str]] = None) -> Optional[LLMBinding]:
    """Devuelve el binding del gateway, o None si AMP no ha enganchado ninguno.

    Precedencia: AMP_LLM_URL/AMP_LLM_API_KEY explícitas por delante del par
    autodetectado, para que un despliegue pueda fijar nombres estables.
    """
    env = dict(os.environ) if env is None else env

    url = env.get("AMP_LLM_URL", "").strip()
    key = env.get("AMP_LLM_API_KEY", "").strip()
    origen = "AMP_LLM_URL" if url else ""

    if not url or not key:
        url, key, origen = _buscar_par(env)

    if not url or not key:
        return None

    partes = urlsplit(url)
    hostname = partes.hostname or ""
    contexto = partes.path.rstrip("/")
    externa = hostname == "localhost" or hostname.endswith(".localhost")

    if env.get("AMP_LLM_GATEWAY_AUTHORITY", "").strip():
        autoridad = env["AMP_LLM_GATEWAY_AUTHORITY"].strip()
        esquema = env.get("AMP_LLM_GATEWAY_SCHEME", "").strip() or partes.scheme or "http"
        host = hostname
    elif externa:
        # Publicada para fuera del cluster: desde un pod no resuelve.
        autoridad = "gateway-default.openchoreo-data-plane:19080"
        esquema = env.get("AMP_LLM_GATEWAY_SCHEME", "http").strip() or "http"
        host = hostname
    elif hostname and "." not in hostname:
        # Nombre de servicio pelado: solo resuelve en su propio namespace.
        ns = env.get("AMP_LLM_RUNTIME_NAMESPACE", "").strip()
        if ns and partes.port:
            autoridad = f"{hostname}.{ns}:{partes.port}"
        elif ns:
            autoridad = f"{hostname}.{ns}"
        else:
            autoridad = partes.netloc
        esquema = partes.scheme or "http"
        host = autoridad
    else:
        # El caso normal: AMP dio una dirección resoluble. Se usa tal cual.
        autoridad = partes.netloc
        esquema = partes.scheme or "http"
        host = ""

    sufijo = env.get("AMP_LLM_OPENAI_PATH", "").strip()
    if sufijo.lower() in ("none", "-"):
        sufijo = ""

    base_url = f"{esquema}://{autoridad}{contexto}{sufijo}".rstrip("/")
    return LLMBinding(base_url=base_url, api_key=key, host=host, origen=origen)


def modelo(env: Optional[Dict[str, str]] = None) -> str:
    """Modelo a usar. Sin fallback a propósito.

    Cuál es el modelo lo decide Agent Manager, no este código. Un valor por
    defecto aquí escondería una configuración ausente y haría que el agente
    hablara con un modelo que nadie eligió.
    """
    env = dict(os.environ) if env is None else env
    return (
        env.get("OCR_MODEL", "").strip()
        or env.get("AMP_GENAI_MODEL", "").strip()
    )
