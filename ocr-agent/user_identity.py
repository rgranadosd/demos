"""Identidad de la persona que pide el analisis.

El agente ya publica quien ejecuta (`gen_ai.agent.id`, firmado por ThunderID).
Esto anade **por cuenta de quien**: si la peticion trae un token de usuario, se
valida contra las claves publicas de ThunderID y su `sub` va al span.

La firma se comprueba de verdad. Un `user.id` sacado de un token sin verificar
no vale nada: cualquiera podria mandar un JWT inventado y atribuirle un gasto a
otra persona. Por eso, si llega un token y no es valido, la peticion se rechaza
con 401 en vez de ignorarlo en silencio.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("ocr-agent.usuario")

try:
    import jwt
    from jwt import PyJWK

    _JWT = True
except ImportError:  # pragma: no cover - sin PyJWT no se puede verificar nada
    _JWT = False

ACTOR_AGENTE = "agent"
ORIGEN_TOKEN_USUARIO = "obo_token"
_TIEMPO_ESPERA = 5.0
_TTL_JWKS = 3600


class TokenDeUsuarioInvalido(Exception):
    """Llego un token pero no se puede confiar en el."""

    def __init__(self, error_type: str, motivo: str):
        super().__init__(motivo)
        self.error_type = error_type
        self.motivo = motivo


@dataclass(frozen=True)
class IdentidadUsuario:
    """El `sub` del token y, solo con opt-in, sus datos de perfil.

    `email` y `username` son PII: por defecto no salen de la aplicación. El
    `sub` de ThunderID ya es un identificador opaco, así que no hace falta
    seudonimizarlo encima.
    """

    user_id: Optional[str]
    issuer: Optional[str]
    username: Optional[str] = None
    email: Optional[str] = None

    @property
    def presente(self) -> bool:
        return self.user_id is not None

    def atributos(self) -> Dict[str, Any]:
        if not self.presente:
            return {}
        atributos = {
            "user.id": self.user_id,
            "auth.delegation": True,
            "auth.source": ORIGEN_TOKEN_USUARIO,
        }
        if _capturar_pii_usuario():
            if self.username:
                atributos["user.username"] = self.username
            if self.email:
                atributos["user.email"] = self.email
        return atributos


SIN_USUARIO = IdentidadUsuario(None, None)


def _capturar_pii_usuario() -> bool:
    return os.getenv("OTEL_CAPTURE_USER_PII", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _jwks_uri() -> str:
    """De donde bajar las claves publicas.

    Por defecto se deriva del endpoint de token, que ya apunta a la ruta del
    gateway que el sandbox permite. ThunderID publica ambos bajo /oauth2/.
    """
    explicito = os.getenv("AGENTID_JWKS_URI", "").strip()
    if explicito:
        return explicito
    token = (
        os.getenv("AGENTID_TOKEN_ENDPOINT", "").strip()
        or os.getenv("AMP_AGENTID_TOKEN_ENDPOINT", "").strip()
    )
    return token.replace("/oauth2/token", "/oauth2/jwks") if token else ""


def _emisor_esperado() -> str:
    return os.getenv("AGENTID_ISSUER", "").strip()


class _Claves:
    """Cachea el JWKS. Se refresca si aparece un `kid` desconocido."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._por_kid: Dict[str, Any] = {}
        self._expira = 0.0

    def clave(self, kid: Optional[str]):
        with self._lock:
            if kid and kid in self._por_kid and time.time() < self._expira:
                return self._por_kid[kid]
            self._descargar()
            if kid:
                # Con `kid` declarado, o esta en el JWKS o no vale. Caer en la
                # unica clave disponible aceptaria un token que dice venir de
                # una clave que no conocemos.
                if kid in self._por_kid:
                    return self._por_kid[kid]
            elif len(self._por_kid) == 1:
                return next(iter(self._por_kid.values()))
        raise TokenDeUsuarioInvalido("unknown_signing_key", "la clave del token no esta en el JWKS")

    def _descargar(self) -> None:
        uri = _jwks_uri()
        if not uri:
            raise TokenDeUsuarioInvalido("jwks_unavailable", "no hay JWKS configurado")
        cabeceras = {}
        clave_gateway = os.getenv("AMP_AGENT_API_KEY", "").strip()
        if clave_gateway and os.getenv("AGENTID_TOKEN_ENDPOINT", "").strip():
            cabeceras["x-amp-api-key"] = clave_gateway
        try:
            respuesta = httpx.get(uri, headers=cabeceras or None, timeout=_TIEMPO_ESPERA)
            respuesta.raise_for_status()
            claves = respuesta.json().get("keys") or []
        except Exception as exc:
            logger.warning("no se pudo descargar el JWKS: %s", type(exc).__name__)
            raise TokenDeUsuarioInvalido("jwks_unavailable", "no se pudo descargar el JWKS")

        self._por_kid = {}
        for jwk in claves:
            try:
                self._por_kid[jwk.get("kid") or ""] = PyJWK.from_dict(jwk).key
            except Exception:
                continue
        self._expira = time.time() + _TTL_JWKS

    def reiniciar(self) -> None:
        with self._lock:
            self._por_kid = {}
            self._expira = 0.0


_CLAVES = _Claves()


def reiniciar_cache() -> None:
    """Solo para los tests."""
    _CLAVES.reiniciar()


def _token_de(cabeceras: Optional[Dict[str, str]]) -> str:
    if not cabeceras:
        return ""
    valor = ""
    for clave, contenido in cabeceras.items():
        if clave.lower() == "authorization":
            valor = contenido or ""
            break
    partes = valor.split(None, 1)
    if len(partes) == 2 and partes[0].lower() == "bearer":
        return partes[1].strip()
    return ""


def identidad_usuario(cabeceras: Optional[Dict[str, str]]) -> IdentidadUsuario:
    """Valida el token de usuario si lo hay.

    Sin token devuelve `SIN_USUARIO` y el analisis sigue como hasta ahora: no
    todas las peticiones vienen de una persona. Con un token invalido lanza,
    porque tragarselo seria aceptar una identidad falsificada.
    """
    token = _token_de(cabeceras)
    if not token:
        return SIN_USUARIO

    if not _JWT:
        raise TokenDeUsuarioInvalido("jwt_library_missing", "no hay PyJWT para verificar la firma")

    try:
        cabecera = jwt.get_unverified_header(token)
    except Exception:
        raise TokenDeUsuarioInvalido("malformed_token", "el token no es un JWT")

    clave = _CLAVES.clave(cabecera.get("kid"))
    emisor = _emisor_esperado()

    try:
        claims = jwt.decode(
            token,
            key=clave,
            algorithms=[cabecera.get("alg") or "RS256"],
            issuer=emisor or None,
            options={
                "require": ["exp", "sub"],
                "verify_aud": False,
                "verify_iss": bool(emisor),
            },
        )
    except Exception as exc:
        tipo = {
            "ExpiredSignatureError": "expired_token",
            "InvalidIssuerError": "wrong_issuer",
            "InvalidSignatureError": "bad_signature",
            "MissingRequiredClaimError": "incomplete_token",
        }.get(type(exc).__name__, "invalid_user_token")
        raise TokenDeUsuarioInvalido(tipo, f"token de usuario rechazado ({tipo})")

    return IdentidadUsuario(
        user_id=str(claims["sub"]),
        issuer=claims.get("iss"),
        username=claims.get("username") or claims.get("preferred_username"),
        email=claims.get("email"),
    )
