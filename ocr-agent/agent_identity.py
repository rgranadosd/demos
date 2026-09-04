"""Identidad del agente en ThunderID.

AMP provisiona el agente como principal de primera clase en ThunderID y le
inyecta al pod sus credenciales OAuth2 (`AMP_AGENTID_*`), pero **no las conecta
con las trazas**: el `sitecustomize` que instala la instrumentacion solo llama a
`Traceloop.init()` y no menciona esas variables. Resultado: la traza dice quien
es el agente solo porque el codigo lleva su nombre escrito a mano.

Este modulo cierra ese hueco. Pide el token propio del agente
(`client_credentials`) y saca de el la identidad **firmada por ThunderID**, para
publicarla en el span.

Es solo para observabilidad. Si ThunderID no responde, el analisis sigue: que un
problema de trazabilidad tumbe el servicio seria peor que no saber quien actuo.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("ocr-agent.identidad")

# Margen para no usar un token que caduca mientras viaja.
_MARGEN_SEGUNDOS = 30
_TIEMPO_ESPERA = 5.0

ACTOR_AGENTE = "agent"
ORIGEN_TOKEN_AGENTE = "agent_token"
ORIGEN_API_KEY = "api_key"
ORIGEN_SIN_RESOLVER = "unresolved"


@dataclass(frozen=True)
class IdentidadAgente:
    """Lo que ThunderID dice del agente. `agent_id` es el `sub` del token."""

    agent_id: Optional[str]
    client_id: Optional[str]
    issuer: Optional[str]
    origen: str

    @property
    def resuelta(self) -> bool:
        return self.agent_id is not None

    def atributos(self) -> Dict[str, Any]:
        """Atributos de span. Todos de baja cardinalidad y sin secretos."""
        datos = {
            "auth.actor.type": ACTOR_AGENTE,
            "auth.source": self.origen,
            "gen_ai.agent.id": self.agent_id,
            "auth.issuer": self.issuer,
            # Aun no hay delegacion: el agente actua siempre por su cuenta.
            # Pasara a true cuando exista el token OBO del usuario.
            "auth.delegation": False,
        }
        return {k: v for k, v in datos.items() if v is not None}


SIN_IDENTIDAD = IdentidadAgente(None, None, None, ORIGEN_API_KEY)


def _config() -> Dict[str, str]:
    return {
        "client_id": os.getenv("AMP_AGENTID_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("AMP_AGENTID_CLIENT_SECRET", "").strip(),
        "token_endpoint": os.getenv("AMP_AGENTID_TOKEN_ENDPOINT", "").strip(),
        "scopes": os.getenv("AMP_AGENTID_SCOPES", "").strip(),
    }


def _claims(token: str) -> Dict[str, Any]:
    """Lee el payload del JWT sin verificar la firma.

    No se valida: el token acaba de llegar de ThunderID por una conexion que ya
    hemos autenticado con el client secret, y aqui solo se usa para etiquetar
    una traza. Verificar firma haria falta si con esto se autorizara algo, que
    no es el caso.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return {}


class ProveedorIdentidad:
    """Cachea el token del agente hasta que caduca."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._identidad: Optional[IdentidadAgente] = None
        self._expira: float = 0.0

    def identidad(self) -> IdentidadAgente:
        ahora = time.time()
        with self._lock:
            if self._identidad is not None and ahora < self._expira:
                return self._identidad
            identidad, validez = self._pedir_token()
            self._identidad = identidad
            # Un fallo no se cachea tanto como un exito: si ThunderID vuelve,
            # queremos recuperar la identidad sin esperar una hora.
            self._expira = ahora + (validez if identidad.resuelta else 60)
            return identidad

    def _pedir_token(self):
        cfg = _config()
        if not (cfg["client_id"] and cfg["client_secret"] and cfg["token_endpoint"]):
            return SIN_IDENTIDAD, 300

        datos = {"grant_type": "client_credentials"}
        if cfg["scopes"]:
            datos["scope"] = cfg["scopes"]

        try:
            respuesta = httpx.post(
                cfg["token_endpoint"],
                data=datos,
                auth=(cfg["client_id"], cfg["client_secret"]),
                timeout=_TIEMPO_ESPERA,
            )
            respuesta.raise_for_status()
            cuerpo = respuesta.json()
        except Exception as exc:
            logger.warning("no se pudo resolver la identidad del agente: %s", _sin_secretos(exc))
            return IdentidadAgente(None, cfg["client_id"], None, ORIGEN_SIN_RESOLVER), 60

        claims = _claims(cuerpo.get("access_token", ""))
        sub = claims.get("sub")
        if not sub:
            logger.warning("ThunderID devolvio un token sin 'sub'")
            return IdentidadAgente(None, cfg["client_id"], None, ORIGEN_SIN_RESOLVER), 60

        validez = int(cuerpo.get("expires_in", 3600)) - _MARGEN_SEGUNDOS
        return (
            IdentidadAgente(
                agent_id=str(sub),
                client_id=cfg["client_id"],
                issuer=claims.get("iss"),
                origen=ORIGEN_TOKEN_AGENTE,
            ),
            max(validez, 30),
        )


def _sin_secretos(exc: BaseException) -> str:
    """Recorta el mensaje a host y motivo: las URLs internas llevan namespace."""
    texto = str(exc)
    try:
        endpoint = os.getenv("AMP_AGENTID_TOKEN_ENDPOINT", "")
        host = urlsplit(endpoint).hostname
        if host:
            texto = texto.replace(endpoint, host)
    except Exception:  # pragma: no cover
        pass
    return f"{type(exc).__name__}: {texto[:200]}"


_PROVEEDOR = ProveedorIdentidad()


def identidad_agente() -> IdentidadAgente:
    return _PROVEEDOR.identidad()


def reiniciar_cache() -> None:
    """Solo para los tests."""
    global _PROVEEDOR
    _PROVEEDOR = ProveedorIdentidad()


def describir() -> str:
    """Linea para el log de arranque."""
    ident = identidad_agente()
    if ident.resuelta:
        return f"identidad de agente resuelta en ThunderID: {ident.agent_id}"
    if ident.origen == ORIGEN_SIN_RESOLVER:
        return "AVISO: ThunderID no respondio; la traza no llevara identidad verificada"
    return "sin AMP_AGENTID_*: el agente se identifica solo por su nombre en el codigo"
