"""Pruebas de la identidad del agente frente a ThunderID."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent_identity as ai  # noqa: E402

CLIENT_ID = "gj0Aplazex0Izx1zfQAOtA"
AGENT_ID = "01a06799-5869-7b1f-bd3f-6399a0406bb6"
ISSUER = "http://default-default.thunder.amp.localhost:8080"
ENDPOINT = "http://thunder.invalid/oauth2/token"


def _jwt(claims: dict) -> str:
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'RS256'})}.{b64(claims)}.firma-no-verificada"


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setenv("AMP_AGENTID_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AMP_AGENTID_CLIENT_SECRET", "un-secreto")
    monkeypatch.setenv("AMP_AGENTID_TOKEN_ENDPOINT", ENDPOINT)
    for var in ("AMP_AGENTID_SCOPES", "AGENTID_TOKEN_ENDPOINT", "AMP_AGENT_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    ai.reiniciar_cache()
    yield
    ai.reiniciar_cache()


def _responde(monkeypatch, cuerpo, status=200, llamadas=None):
    def _post(url, **kwargs):
        if llamadas is not None:
            llamadas.append(
                {
                    "url": url,
                    "auth": kwargs.get("auth"),
                    "scope": (kwargs.get("data") or {}).get("scope"),
                    "headers": kwargs.get("headers"),
                }
            )
        peticion = httpx.Request("POST", url)
        return httpx.Response(status, json=cuerpo, request=peticion)

    monkeypatch.setattr(ai.httpx, "post", _post)


def test_identidad_resuelta_desde_el_token(monkeypatch):
    llamadas = []
    _responde(
        monkeypatch,
        {"access_token": _jwt({"sub": AGENT_ID, "iss": ISSUER}), "expires_in": 3600},
        llamadas=llamadas,
    )

    ident = ai.identidad_agente()

    assert ident.resuelta
    assert ident.agent_id == AGENT_ID
    assert ident.issuer == ISSUER
    assert ident.origen == "agent_token"
    assert llamadas[0]["auth"] == (CLIENT_ID, "un-secreto")
    assert llamadas[0]["url"] == ENDPOINT
    # Sin ruta por gateway no se manda la clave de agente a ThunderID.
    assert llamadas[0]["headers"] is None

    attrs = ident.atributos()
    assert attrs["gen_ai.agent.id"] == AGENT_ID
    assert attrs["auth.actor.type"] == "agent"
    assert attrs["auth.source"] == "agent_token"
    assert attrs["auth.delegation"] is False
    # Ni el secret ni el token entero pueden acabar en un atributo.
    volcado = json.dumps(attrs)
    assert "un-secreto" not in volcado
    assert "firma-no-verificada" not in volcado


def test_el_token_se_cachea(monkeypatch):
    llamadas = []
    _responde(
        monkeypatch,
        {"access_token": _jwt({"sub": AGENT_ID, "iss": ISSUER}), "expires_in": 3600},
        llamadas=llamadas,
    )

    for _ in range(5):
        assert ai.identidad_agente().agent_id == AGENT_ID

    assert len(llamadas) == 1, "no se llama a ThunderID en cada peticion"


def test_scopes_se_piden_solo_si_los_hay(monkeypatch):
    llamadas = []
    _responde(
        monkeypatch,
        {"access_token": _jwt({"sub": AGENT_ID}), "expires_in": 60},
        llamadas=llamadas,
    )
    ai.identidad_agente()
    assert llamadas[0]["scope"] is None, "AMP_AGENTID_SCOPES vacio no debe mandarse"

    monkeypatch.setenv("AMP_AGENTID_SCOPES", "expense:read")
    ai.reiniciar_cache()
    ai.identidad_agente()
    assert llamadas[1]["scope"] == "expense:read"


def test_ruta_por_el_gateway_tiene_precedencia(monkeypatch):
    """La NetworkPolicy del sandbox no deja llegar al 8090 de ThunderID.

    Con `AGENTID_TOKEN_ENDPOINT` se sale por el 22893, que si esta permitido, y
    la clave de agente viaja en x-amp-api-key para no pisar el Authorization
    que ThunderID necesita.
    """
    gateway = "http://api-platform-gateway.default-default:22893/thunder/oauth2/token"
    monkeypatch.setenv("AGENTID_TOKEN_ENDPOINT", gateway)
    monkeypatch.setenv("AMP_AGENT_API_KEY", "clave-de-agente")
    ai.reiniciar_cache()

    llamadas = []
    _responde(
        monkeypatch,
        {"access_token": _jwt({"sub": AGENT_ID, "iss": ISSUER}), "expires_in": 3600},
        llamadas=llamadas,
    )

    ident = ai.identidad_agente()

    assert ident.agent_id == AGENT_ID
    assert llamadas[0]["url"] == gateway, "debe ganar sobre AMP_AGENTID_TOKEN_ENDPOINT"
    assert llamadas[0]["headers"] == {"x-amp-api-key": "clave-de-agente"}
    # Authorization sigue llevando el client_secret_basic del agente.
    assert llamadas[0]["auth"] == (CLIENT_ID, "un-secreto")


def test_la_clave_de_agente_no_sale_si_no_hay_gateway(monkeypatch):
    monkeypatch.setenv("AMP_AGENT_API_KEY", "clave-de-agente")
    ai.reiniciar_cache()
    llamadas = []
    _responde(monkeypatch, {"access_token": _jwt({"sub": AGENT_ID}), "expires_in": 60}, llamadas=llamadas)

    ai.identidad_agente()

    assert llamadas[0]["headers"] is None


def test_thunder_caido_no_tumba_el_agente(monkeypatch):
    def _falla(url, **_kwargs):
        raise httpx.ConnectError("connection refused", request=httpx.Request("POST", url))

    monkeypatch.setattr(ai.httpx, "post", _falla)

    ident = ai.identidad_agente()

    assert not ident.resuelta
    assert ident.origen == "unresolved"
    assert "gen_ai.agent.id" not in ident.atributos()
    # El actor sigue declarandose: lo que falta es la firma de ThunderID.
    assert ident.atributos()["auth.actor.type"] == "agent"


def test_error_http_no_filtra_la_url_interna(monkeypatch, caplog):
    monkeypatch.setenv(
        "AMP_AGENTID_TOKEN_ENDPOINT",
        "http://amp-thunder-default-default-service.amp-thunder-default-default.svc.cluster.local:8090/oauth2/token",
    )
    ai.reiniciar_cache()
    _responde(monkeypatch, {"error": "invalid_client"}, status=401)

    with caplog.at_level("WARNING"):
        ident = ai.identidad_agente()

    assert not ident.resuelta
    registro = caplog.text
    assert "svc.cluster.local:8090/oauth2/token" not in registro
    assert "un-secreto" not in registro


def test_sin_variables_no_hay_identidad(monkeypatch):
    for var in ("AMP_AGENTID_CLIENT_ID", "AMP_AGENTID_CLIENT_SECRET", "AMP_AGENTID_TOKEN_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    ai.reiniciar_cache()

    ident = ai.identidad_agente()

    assert not ident.resuelta
    assert ident.origen == "api_key"
    assert "sin AMP_AGENTID_" in ai.describir()


def test_token_sin_sub_no_se_da_por_bueno(monkeypatch):
    _responde(monkeypatch, {"access_token": _jwt({"iss": ISSUER}), "expires_in": 3600})

    ident = ai.identidad_agente()

    assert not ident.resuelta
    assert ident.origen == "unresolved"
