"""Pruebas de la identidad del usuario.

Se genera una pareja de claves RSA de verdad y se firman tokens con ella, para
que la comprobacion de firma sea real y no un mock que siempre dice que si.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import user_identity as ui  # noqa: E402

ISSUER = "http://default-default.thunder.amp.localhost:8080"
JWKS_URI = "http://gateway.invalid/thunder/oauth2/jwks"
USER_ID = "01a06d55-1234-7abc-9def-000000000001"
KID = "clave-de-prueba"


def _par_de_claves():
    privada = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numeros = privada.public_key().public_numbers()

    def b64(n):
        import base64

        largo = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(largo, "big")).rstrip(b"=").decode()

    jwk = {"kty": "RSA", "kid": KID, "alg": "RS256", "use": "sig",
           "n": b64(numeros.n), "e": b64(numeros.e)}
    pem = privada.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, jwk


PEM, JWK = _par_de_claves()
# Una segunda clave, para firmar un token que el JWKS no avala.
PEM_IMPOSTOR, _ = _par_de_claves()


def _token(pem=PEM, kid=KID, **cambios):
    claims = {
        "sub": USER_ID,
        "iss": ISSUER,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "email": "rafa@example.com",
        "username": "rafa",
    }
    claims.update(cambios)
    for k in [k for k, v in cambios.items() if v is None]:
        claims.pop(k, None)
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


def _cabeceras(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setenv("AGENTID_JWKS_URI", JWKS_URI)
    monkeypatch.setenv("AGENTID_ISSUER", ISSUER)
    monkeypatch.delenv("AGENTID_TOKEN_ENDPOINT", raising=False)
    monkeypatch.delenv("AMP_AGENT_API_KEY", raising=False)

    def _get(url, **_kwargs):
        return httpx.Response(200, json={"keys": [JWK]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(ui.httpx, "get", _get)
    ui.reiniciar_cache()
    yield
    ui.reiniciar_cache()


# ------------------------------------------------------------------- pruebas


def test_token_valido_da_el_usuario():
    ident = ui.identidad_usuario(_cabeceras(_token()))

    assert ident.presente
    assert ident.user_id == USER_ID

    attrs = ident.atributos()
    assert attrs["user.id"] == USER_ID
    assert attrs["auth.delegation"] is True
    assert attrs["auth.source"] == "obo_token"
    # Por defecto, los datos de perfil no salen en la traza.
    volcado = json.dumps(attrs)
    assert "rafa@example.com" not in volcado
    assert "rafa" not in volcado


def test_pii_de_usuario_requiere_opt_in(monkeypatch):
    monkeypatch.setenv("OTEL_CAPTURE_USER_PII", "true")

    attrs = ui.identidad_usuario(_cabeceras(_token())).atributos()

    assert attrs["user.username"] == "rafa"
    assert attrs["user.email"] == "rafa@example.com"


def test_sin_token_el_analisis_sigue():
    """No toda peticion viene de una persona; eso no es un error."""
    ident = ui.identidad_usuario({})

    assert not ident.presente
    assert ident.atributos() == {}


def test_firma_falsificada_se_rechaza():
    impostor = _token(pem=PEM_IMPOSTOR)

    with pytest.raises(ui.TokenDeUsuarioInvalido) as exc:
        ui.identidad_usuario(_cabeceras(impostor))

    assert exc.value.error_type == "bad_signature"


def test_token_caducado_se_rechaza():
    with pytest.raises(ui.TokenDeUsuarioInvalido) as exc:
        ui.identidad_usuario(_cabeceras(_token(exp=int(time.time()) - 10)))

    assert exc.value.error_type == "expired_token"


def test_emisor_distinto_se_rechaza():
    with pytest.raises(ui.TokenDeUsuarioInvalido) as exc:
        ui.identidad_usuario(_cabeceras(_token(iss="http://otro-idp.example")))

    assert exc.value.error_type == "wrong_issuer"


def test_token_sin_sub_se_rechaza():
    with pytest.raises(ui.TokenDeUsuarioInvalido) as exc:
        ui.identidad_usuario(_cabeceras(_token(sub=None)))

    assert exc.value.error_type == "incomplete_token"


def test_basura_en_authorization_se_rechaza():
    with pytest.raises(ui.TokenDeUsuarioInvalido) as exc:
        ui.identidad_usuario({"Authorization": "Bearer esto-no-es-un-jwt"})

    assert exc.value.error_type == "malformed_token"


def test_kid_desconocido_se_rechaza():
    with pytest.raises(ui.TokenDeUsuarioInvalido) as exc:
        ui.identidad_usuario(_cabeceras(_token(kid="otro-kid")))

    assert exc.value.error_type in ("unknown_signing_key", "bad_signature")


def test_jwks_inalcanzable_no_se_da_por_bueno(monkeypatch):
    def _falla(url, **_kwargs):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(ui.httpx, "get", _falla)
    ui.reiniciar_cache()

    with pytest.raises(ui.TokenDeUsuarioInvalido) as exc:
        ui.identidad_usuario(_cabeceras(_token()))

    assert exc.value.error_type == "jwks_unavailable"


def test_el_jwks_se_cachea(monkeypatch):
    descargas = []

    def _get(url, **_kwargs):
        descargas.append(url)
        return httpx.Response(200, json={"keys": [JWK]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(ui.httpx, "get", _get)
    ui.reiniciar_cache()

    for _ in range(4):
        ui.identidad_usuario(_cabeceras(_token()))

    assert len(descargas) == 1


def test_jwks_se_deriva_del_endpoint_de_token(monkeypatch):
    monkeypatch.delenv("AGENTID_JWKS_URI", raising=False)
    monkeypatch.setenv(
        "AGENTID_TOKEN_ENDPOINT", "http://gw.default-default:22893/thunder/oauth2/token"
    )

    assert ui._jwks_uri() == "http://gw.default-default:22893/thunder/oauth2/jwks"


def test_la_clave_de_agente_acompana_al_jwks_por_el_gateway(monkeypatch):
    monkeypatch.setenv("AGENTID_TOKEN_ENDPOINT", "http://gw/thunder/oauth2/token")
    monkeypatch.setenv("AMP_AGENT_API_KEY", "clave-de-agente")
    vistas = []

    def _get(url, **kwargs):
        vistas.append(kwargs.get("headers"))
        return httpx.Response(200, json={"keys": [JWK]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(ui.httpx, "get", _get)
    ui.reiniciar_cache()

    ui.identidad_usuario(_cabeceras(_token()))

    assert vistas[0] == {"x-amp-api-key": "clave-de-agente"}
