"""Pruebas de la instrumentacion y de la politica de privacidad.

Los spans se recogen con el exporter en memoria del SDK; el modelo se sustituye
por un doble, asi que no hay ninguna llamada al gateway.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import esquema  # noqa: E402
import main  # noqa: E402
import observabilidad as obs  # noqa: E402

PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
)

GASTO_OK = {
    "tipo_documento": "ticket",
    "comercio": "Bar Pepe",
    "fecha": "01/02/2026",
    "total": 61.49,
    "moneda": "EUR",
    "impuestos": 5.59,
    "base_imponible": 55.90,
    "metodo_pago": "tarjeta",
    "categoria_estimada": "restauracion",
    "lineas_principales": [{"descripcion": "MENU", "cantidad": 2, "precio_unitario": None, "importe": 55.90}],
    "resumen": "comida de trabajo",
    "legible": True,
    "advertencias": [],
}


# ------------------------------------------------------------------ andamiaje


@pytest.fixture(scope="session")
def exporter() -> InMemorySpanExporter:
    memoria = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memoria))
    trace.set_tracer_provider(provider)
    return memoria


@pytest.fixture(autouse=True)
def entorno_limpio(exporter, monkeypatch):
    exporter.clear()
    for var in ("OTEL_GENAI_CAPTURE_CONTENT", "OTEL_ENVIRONMENT", "EXPENSE_OCR_MAX_ATTEMPTS"):
        monkeypatch.delenv(var, raising=False)
    obs.recargar_config()
    yield
    obs.recargar_config()


def _respuesta(contenido: str, modelo: str = "qwen/qwen3-vl-8b"):
    return SimpleNamespace(
        model=modelo,
        choices=[SimpleNamespace(message=SimpleNamespace(content=contenido), finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    )


def _modelo_devuelve(monkeypatch, *respuestas):
    """Sustituye al cliente OpenAI. Cada elemento es una respuesta o una excepcion."""
    llamadas = {"n": 0}

    class _Completions:
        def create(self, **_kwargs):
            i = min(llamadas["n"], len(respuestas) - 1)
            llamadas["n"] += 1
            valor = respuestas[i]
            if isinstance(valor, BaseException):
                raise valor
            return valor

    cliente = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr(main, "_cliente", lambda: (cliente, "qwen/qwen3-vl-8b", None))
    return llamadas


def _spans(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


# -------------------------------------------------------------------- pruebas


def test_documento_invalido_marca_error_y_no_llama_al_modelo(exporter, monkeypatch):
    llamadas = _modelo_devuelve(monkeypatch, _respuesta(json.dumps(GASTO_OK)))

    with pytest.raises(main.ErrorDocumento) as exc:
        main._analizar_justificante(b"esto no es una imagen", "image/png")

    assert exc.value.error_type == "invalid_image"
    assert llamadas["n"] == 0, "no debe gastarse una llamada al gateway"

    spans = _spans(exporter)
    validate = spans["app.document.validate"]
    assert validate.status.status_code is StatusCode.ERROR
    assert validate.attributes["error.type"] == "invalid_image"
    assert validate.events, "la excepcion tiene que quedar registrada"

    raiz = spans["invoke_agent ocr-agent"]
    assert raiz.status.status_code is StatusCode.ERROR
    assert raiz.attributes["error.type"] == "invalid_image"


def test_respuesta_valida_produce_el_arbol_de_spans(exporter, monkeypatch):
    _modelo_devuelve(monkeypatch, _respuesta(json.dumps(GASTO_OK)))

    analisis = main._analizar_justificante(PNG_1x1, "image/png", origen="fichero")

    assert analisis.gasto.total == 61.49
    spans = _spans(exporter)
    for nombre in (
        "invoke_agent ocr-agent",
        "app.document.validate",
        "app.document.preprocess",
        "chat qwen/qwen3-vl-8b",
        "app.ocr.parse_json",
        "app.ocr.validate_schema",
        "app.ocr.quality_check",
    ):
        assert nombre in spans, f"falta el span {nombre}"

    raiz = spans["invoke_agent ocr-agent"]
    assert raiz.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert raiz.attributes["gen_ai.agent.name"] == "ocr-agent"
    # Sin AMP_AGENTID_* no hay firma de ThunderID, pero el actor se declara.
    assert raiz.attributes["auth.actor.type"] == "agent"
    assert raiz.attributes["auth.source"] == "api_key"
    assert raiz.attributes["auth.delegation"] is False
    assert raiz.attributes["expense.document.source"] == "upload"
    assert raiz.attributes["expense.document.type_hint"] == "ticket"
    assert raiz.attributes["expense.ocr.output_valid_json"] is True
    assert raiz.attributes["expense.ocr.output_schema_valid"] is True
    assert raiz.attributes["expense.ocr.review_required"] is False
    assert raiz.attributes["expense.ocr.review_reason"] == "none"
    assert raiz.attributes["expense.ocr.retry_count"] == 0
    assert raiz.attributes["gen_ai.usage.total_tokens"] == 150

    # Todas las fases cuelgan del span de agente, no de la raiz de la traza.
    assert spans["app.ocr.parse_json"].parent.span_id == raiz.context.span_id

    llm = spans["chat qwen/qwen3-vl-8b"]
    assert llm.attributes["gen_ai.operation.name"] == "chat"
    assert llm.attributes["gen_ai.response.model"] == "qwen/qwen3-vl-8b"
    assert llm.attributes["gen_ai.request.temperature"] == 0


def test_json_invalido_del_modelo(exporter, monkeypatch):
    _modelo_devuelve(monkeypatch, _respuesta("lo siento, no puedo leer el ticket"))

    analisis = main._analizar_justificante(PNG_1x1, "image/png")

    assert "el modelo no devolvió JSON válido" in analisis.advertencias
    spans = _spans(exporter)
    parse = spans["app.ocr.parse_json"]
    assert parse.attributes["expense.ocr.output_valid_json"] is False
    assert parse.attributes["error.type"] == "json_parse_error"
    assert parse.status.status_code is StatusCode.ERROR
    assert any(e.name == "expense.ocr.json_parse_failed" for e in parse.events)

    raiz = spans["invoke_agent ocr-agent"]
    assert raiz.attributes["expense.ocr.review_reason"] == "invalid_json"
    assert raiz.attributes["expense.ocr.review_required"] is True
    # Un intento de reparacion antes de rendirse.
    assert raiz.attributes["expense.ocr.retry_count"] == 1
    assert any(e.name == "gen_ai.retry" for e in raiz.events)


def test_json_valido_pero_esquema_invalido(exporter, monkeypatch):
    malo = dict(GASTO_OK, tipo_documento="albaran", total="61,49 EUR")
    _modelo_devuelve(monkeypatch, _respuesta(json.dumps(malo)))

    main._analizar_justificante(PNG_1x1, "image/png")

    spans = _spans(exporter)
    validacion = spans["app.ocr.validate_schema"]
    assert validacion.attributes["expense.ocr.output_schema_valid"] is False
    assert validacion.attributes["error.type"] == "json_schema_validation_error"
    reglas = set(validacion.attributes["expense.ocr.schema_failed_rules"])
    assert reglas == {"tipo_documento_invalido", "total_no_numerico"}
    # Solo el nombre de la regla: ni el importe ni el comercio salen del pod.
    assert "61,49" not in json.dumps(dict(validacion.attributes))

    raiz = spans["invoke_agent ocr-agent"]
    assert raiz.attributes["expense.ocr.review_reason"] == "schema_invalid"
    assert raiz.attributes["expense.document.type_hint"] == "unknown"


def test_documento_ilegible_pide_revision(exporter, monkeypatch):
    ilegible = dict(GASTO_OK, legible=False, total=None, advertencias=["foto movida"])
    _modelo_devuelve(monkeypatch, _respuesta(json.dumps(ilegible)))

    analisis = main._analizar_justificante(PNG_1x1, "image/png")

    assert analisis.legible is False
    calidad = _spans(exporter)["app.ocr.quality_check"]
    assert calidad.attributes["expense.ocr.review_required"] is True
    assert calidad.attributes["expense.ocr.review_reason"] == "illegible"
    assert calidad.attributes["expense.ocr.warning_count"] > 0
    # El recuento si, el texto de la advertencia no.
    assert "foto movida" not in json.dumps(dict(calidad.attributes))


def test_reintento_del_modelo_y_luego_exito(exporter, monkeypatch):
    import openai

    fallo = openai.APITimeoutError(request=None)
    llamadas = _modelo_devuelve(monkeypatch, fallo, _respuesta(json.dumps(GASTO_OK)))

    analisis = main._analizar_justificante(PNG_1x1, "image/png")

    assert llamadas["n"] == 2
    assert analisis.gasto.total == 61.49

    raiz = _spans(exporter)["invoke_agent ocr-agent"]
    reintentos = [e for e in raiz.events if e.name == "gen_ai.retry"]
    assert len(reintentos) == 1
    assert reintentos[0].attributes["retry.attempt"] == 1
    assert reintentos[0].attributes["error.type"] == "timeout"
    assert raiz.attributes["expense.ocr.retry_count"] == 1
    # El resultado final fue correcto: la raiz no se marca en ERROR.
    assert raiz.status.status_code is not StatusCode.ERROR


def test_error_de_modelo_no_reintentable(exporter, monkeypatch):
    import httpx
    import openai

    respuesta_http = httpx.Response(401, request=httpx.Request("POST", "http://gw/chat"))
    _modelo_devuelve(
        monkeypatch,
        openai.AuthenticationError("no autorizado", response=respuesta_http, body=None),
    )

    with pytest.raises(openai.AuthenticationError):
        main._analizar_justificante(PNG_1x1, "image/png")

    raiz = _spans(exporter)["invoke_agent ocr-agent"]
    assert raiz.status.status_code is StatusCode.ERROR
    assert raiz.attributes["error.type"] == "http_4xx"
    assert raiz.attributes["http.response.status_code"] == 401
    assert raiz.attributes["expense.ocr.review_reason"] == "model_error"


# ----------------------------------------------------------------- privacidad


def _atributos_exportados(exporter, nombre):
    span = _spans(exporter)[nombre]
    return obs.redactar_atributos(dict(span.attributes))


def test_produccion_no_exporta_contenido_ni_imagen(exporter, monkeypatch):
    monkeypatch.setenv("OTEL_ENVIRONMENT", "production")
    obs.recargar_config()
    _modelo_devuelve(monkeypatch, _respuesta(json.dumps(GASTO_OK)))

    main._analizar_justificante(
        PNG_1x1, "image/png", cabeceras={"X-Document-Id": "factura-4471"}
    )

    exportados = _atributos_exportados(exporter, "invoke_agent ocr-agent")
    volcado = json.dumps(exportados, default=str)

    assert obs.CONFIG.captura == "none"
    assert "gen_ai.input.messages" not in exportados
    assert "gen_ai.output.messages" not in exportados
    for sensible in ("Bar Pepe", "comida de trabajo", "base64", "data:image", "61.49", "factura-4471"):
        assert sensible not in volcado, f"se ha exportado {sensible}"

    # Lo tecnico sigue estando: sin esto la traza no serviria para nada.
    assert exportados["gen_ai.usage.total_tokens"] == 150
    assert exportados["gen_ai.request.model"] == "qwen/qwen3-vl-8b"
    assert exportados["expense.ocr.output_schema_valid"] is True
    assert exportados["expense.document.size_bytes"] == len(PNG_1x1)


def test_modo_redactado_conserva_los_mensajes_sin_pii(exporter, monkeypatch):
    monkeypatch.setenv("OTEL_ENVIRONMENT", "production")
    monkeypatch.setenv("OTEL_GENAI_CAPTURE_CONTENT", "redacted")
    obs.recargar_config()
    _modelo_devuelve(monkeypatch, _respuesta(json.dumps(GASTO_OK)))

    main._analizar_justificante(PNG_1x1, "image/png")

    exportados = _atributos_exportados(exporter, "invoke_agent ocr-agent")
    salida = exportados["gen_ai.output.messages"]

    # Los evaluadores de AMP siguen teniendo algo que puntuar...
    assert "gen_ai.input.messages" in exportados
    assert "61.49" in salida
    assert "ticket" in salida
    # ...pero sin el comercio ni el texto libre.
    assert "Bar Pepe" not in salida
    assert "comida de trabajo" not in salida
    assert obs.REDACTADO in salida


def test_hash_de_documento_necesita_sal(monkeypatch):
    monkeypatch.delenv("EXPENSE_OCR_ID_SALT", raising=False)
    assert main._hash_documento("factura-4471") is None

    monkeypatch.setenv("EXPENSE_OCR_ID_SALT", "una-sal-larga")
    opaco = main._hash_documento("factura-4471")
    assert opaco and "4471" not in opaco
    assert main._hash_documento("factura-4471") == opaco


@pytest.mark.parametrize("clase_procesador", [SimpleSpanProcessor, BatchSpanProcessor])
def test_el_redactor_envuelve_el_exporter_del_provider(clase_procesador):
    """En BatchSpanProcessor el exporter no es asignable: hay que ir a buscarlo.

    Probarlo solo con SimpleSpanProcessor daba verde y en el pod la redaccion
    no se instalaba, porque `span_exporter` es una propiedad de solo lectura
    desde el SDK 1.3x.
    """
    provider = TracerProvider()
    memoria = InMemorySpanExporter()
    provider.add_span_processor(clase_procesador(memoria))

    procesadores = obs._procesadores(provider)
    assert procesadores, "hay que encontrar los procesadores que ya instalo AMP"
    assert all(obs.envolver_exporter(p) for p in procesadores)

    tracer = provider.get_tracer("prueba")
    with tracer.start_as_current_span("x") as span:
        span.set_attribute("gen_ai.prompt.0.content", "data:image/png;base64,AAAA")
        span.set_attribute("gen_ai.usage.total_tokens", 7)
    provider.force_flush()

    atributos = memoria.get_finished_spans()[0].attributes
    assert "gen_ai.prompt.0.content" not in atributos
    assert atributos["gen_ai.usage.total_tokens"] == 7
    provider.shutdown()


def test_envolver_exporter_es_idempotente():
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(InMemorySpanExporter()))
    procesador = obs._procesadores(provider)[0]

    assert obs.envolver_exporter(procesador) is True
    assert obs.envolver_exporter(procesador) is True

    portador = procesador._batch_processor
    envueltos = [v for v in vars(portador).values() if isinstance(v, obs.RedactorExporter)]
    assert len(envueltos) == 1, "no debe apilarse un redactor sobre otro"
    provider.shutdown()


# -------------------------------------------------------------------- esquema


@pytest.mark.parametrize(
    "mutacion, regla",
    [
        ({"tipo_documento": "albaran"}, "tipo_documento_invalido"),
        ({"impuestos": "5,59"}, "impuestos_no_numerico"),
        ({"lineas_principales": {}}, "lineas_principales_no_lista"),
        ({"legible": "si"}, "legible_no_booleano"),
        ({"advertencias": "ninguna"}, "advertencias_no_lista"),
        ({"sobrante": 1}, "campo_desconocido"),
    ],
)
def test_reglas_del_esquema(mutacion, regla):
    datos = dict(GASTO_OK, **mutacion)
    assert regla in esquema.validar(datos)


def test_esquema_valido_no_devuelve_fallos():
    assert esquema.validar(GASTO_OK) == []


def test_campo_ausente():
    datos = {k: v for k, v in GASTO_OK.items() if k != "total"}
    assert "campo_ausente" in esquema.validar(datos)
