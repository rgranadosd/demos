"""Observabilidad OpenTelemetry del agente OCR.

Este modulo **no configura** OpenTelemetry. El tracer provider, el propagador y
el exporter los instala el `sitecustomize` de AMP en el pod; aqui solo se
obtienen del API global. Montar un segundo provider partiria las trazas en dos.

Lo que si aporta:

- Configuracion por entorno (`OTEL_ENVIRONMENT`, `OTEL_GENAI_CAPTURE_CONTENT`...).
- Helpers para abrir spans de fase con estado y `error.type` consistentes.
- Instrumentos de metricas con etiquetas de baja cardinalidad.
- Un redactor que quita el contenido sensible que la instrumentacion automatica
  del SDK de OpenAI/Traceloop pone en los spans **antes de exportarlos**.

Si `opentelemetry` no esta instalado (ejecucion local sin el SDK de AMP), todo
degrada a no-ops y la aplicacion sigue funcionando.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger("ocr-agent.observabilidad")

AGENT_NAME = "ocr-agent"

try:  # pragma: no cover - depende del entorno de despliegue
    from opentelemetry import metrics, trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL = True
except ImportError:  # pragma: no cover
    _OTEL = False
    metrics = None  # type: ignore[assignment]
    trace = None  # type: ignore[assignment]
    Status = StatusCode = None  # type: ignore[assignment]


# --------------------------------------------------------------- configuracion


def _float_env(nombre: str) -> Optional[float]:
    valor = os.getenv(nombre, "").strip()
    if not valor:
        return None
    try:
        return float(valor)
    except ValueError:
        logger.warning("%s no es un numero, se ignora", nombre)
        return None


# Politica de captura de contenido. Tres niveles, no un booleano: con solo
# on/off habria que elegir entre privacidad y dejar sin nada que puntuar a los
# evaluadores de AMP, que leen gen_ai.input.messages y gen_ai.output.messages.
CAPTURA_NINGUNA = "none"
CAPTURA_REDACTADA = "redacted"
CAPTURA_COMPLETA = "full"

_ALIAS_CAPTURA = {
    "": CAPTURA_NINGUNA,
    "0": CAPTURA_NINGUNA,
    "false": CAPTURA_NINGUNA,
    "no": CAPTURA_NINGUNA,
    "off": CAPTURA_NINGUNA,
    "none": CAPTURA_NINGUNA,
    "redacted": CAPTURA_REDACTADA,
    "redactado": CAPTURA_REDACTADA,
    "1": CAPTURA_COMPLETA,
    "true": CAPTURA_COMPLETA,
    "yes": CAPTURA_COMPLETA,
    "si": CAPTURA_COMPLETA,
    "on": CAPTURA_COMPLETA,
    "full": CAPTURA_COMPLETA,
}


def _nivel_captura() -> str:
    bruto = os.getenv("OTEL_GENAI_CAPTURE_CONTENT", "").strip().lower()
    nivel = _ALIAS_CAPTURA.get(bruto)
    if nivel is None:
        logger.warning(
            "OTEL_GENAI_CAPTURE_CONTENT='%s' no es un valor conocido (none|redacted|full); "
            "se aplica 'none'",
            bruto,
        )
        return CAPTURA_NINGUNA
    return nivel


@dataclass(frozen=True)
class Config:
    entorno: str
    version_servicio: str
    version_agente: str
    version_esquema: str
    captura: str
    umbral_confianza: Optional[float]
    max_intentos: int

    @property
    def es_produccion(self) -> bool:
        return self.entorno.lower() in ("prod", "production", "produccion", "producción")

    @property
    def capturar_contenido(self) -> bool:
        return self.captura != CAPTURA_NINGUNA


def _leer_config() -> Config:
    version = (
        os.getenv("OTEL_SERVICE_VERSION", "").strip()
        or os.getenv("AMP_COMPONENT_VERSION", "").strip()
        or os.getenv("GIT_COMMIT_SHA", "").strip()[:12]
        or "unknown"
    )
    try:
        max_intentos = max(1, int(os.getenv("EXPENSE_OCR_MAX_ATTEMPTS", "2")))
    except ValueError:
        max_intentos = 2
    return Config(
        entorno=os.getenv("OTEL_ENVIRONMENT", "development").strip() or "development",
        version_servicio=version,
        # Version del workflow/prompt del agente. Baja cardinalidad a proposito:
        # se cambia a mano cuando cambia el contrato, no en cada build.
        version_agente=os.getenv("OCR_AGENT_WORKFLOW_VERSION", "").strip() or version,
        version_esquema=os.getenv("EXPENSE_OCR_SCHEMA_VERSION", "expense-v1").strip() or "expense-v1",
        # Nunca por defecto: solo con un flag explicito.
        captura=_nivel_captura(),
        umbral_confianza=_float_env("EXPENSE_OCR_REVIEW_CONFIDENCE_THRESHOLD"),
        max_intentos=max_intentos,
    )


CONFIG = _leer_config()


def recargar_config() -> Config:
    """Relee el entorno. Solo lo usan los tests y el arranque."""
    global CONFIG
    CONFIG = _leer_config()
    return CONFIG


# ------------------------------------------------------------ motivos de revision

REVISION_NINGUNA = "none"
REVISION_JSON_INVALIDO = "invalid_json"
REVISION_ESQUEMA_INVALIDO = "schema_invalid"
REVISION_BAJA_CONFIANZA = "low_confidence"
REVISION_ILEGIBLE = "illegible"
REVISION_SIN_TOTAL = "missing_total"
REVISION_ERROR_MODELO = "model_error"


# ------------------------------------------------------------------- spans


class _SpanNulo:
    """Sustituto de un span cuando no hay SDK de OpenTelemetry."""

    def set_attribute(self, *_args, **_kwargs) -> None:
        return None

    def set_attributes(self, *_args, **_kwargs) -> None:
        return None

    def add_event(self, *_args, **_kwargs) -> None:
        return None

    def record_exception(self, *_args, **_kwargs) -> None:
        return None

    def set_status(self, *_args, **_kwargs) -> None:
        return None

    def is_recording(self) -> bool:
        return False


def obtener_tracer():
    if not _OTEL:
        return None
    return trace.get_tracer(f"amp.{AGENT_NAME}")


def _limpiar(atributos: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Quita los None: OTel no admite valores nulos como atributo."""
    if not atributos:
        return {}
    return {k: v for k, v in atributos.items() if v is not None}


@contextmanager
def span_fase(nombre: str, atributos: Optional[Dict[str, Any]] = None, contexto=None) -> Iterator[Any]:
    """Abre un span hijo de fase. Si no hay OTel, devuelve un span nulo."""
    tracer = obtener_tracer()
    if tracer is None:
        yield _SpanNulo()
        return
    with tracer.start_as_current_span(nombre, context=contexto, attributes=_limpiar(atributos)) as span:
        yield span


def marcar_error(span, error_type: str, exc: Optional[BaseException] = None,
                 atributos: Optional[Dict[str, Any]] = None) -> None:
    """Estado ERROR + excepcion registrada + `error.type` de baja cardinalidad.

    Los tres a la vez. `success=false` a secas no lo sustituye: quien consulta
    las trazas filtra por el estado estandar de OTel, no por un booleano propio.
    """
    if span is None:
        return
    try:
        span.set_attribute("error.type", error_type)
        for clave, valor in _limpiar(atributos).items():
            span.set_attribute(clave, valor)
        if exc is not None:
            span.record_exception(exc)
        if _OTEL:
            span.set_status(Status(StatusCode.ERROR, error_type))
    except Exception:  # pragma: no cover - la observabilidad nunca tumba el flujo
        logger.debug("no se pudo marcar el error en el span", exc_info=True)


def marcar_ok(span) -> None:
    if span is None or not _OTEL:
        return
    try:
        span.set_status(Status(StatusCode.OK))
    except Exception:  # pragma: no cover
        pass


def contexto_entrante(cabeceras: Optional[Dict[str, str]]):
    """Continua la traza W3C que venga en `traceparent`, si la hay.

    En el pod solo esta instrumentado `requests`: no hay instrumentacion ASGI,
    asi que nadie extrae el contexto de la peticion entrante y cada llamada
    abriria una traza nueva.
    """
    if not cabeceras or not _OTEL:
        return None
    try:
        from opentelemetry.propagate import extract

        return extract({k.lower(): v for k, v in cabeceras.items()})
    except Exception:  # pragma: no cover
        return None


def ids_de_traza() -> Dict[str, str]:
    """`trace_id`/`span_id` en hexadecimal para correlacionar los logs."""
    if not _OTEL:
        return {}
    try:
        ctx = trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return {}
        return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
    except Exception:  # pragma: no cover
        return {}


# ------------------------------------------------------------------ metricas


class _InstrumentoNulo:
    def add(self, *_args, **_kwargs) -> None:
        return None

    def record(self, *_args, **_kwargs) -> None:
        return None


class _Metricas:
    """Instrumentos OTel. Si no hay meter provider, el API ya es un no-op."""

    def __init__(self) -> None:
        if not _OTEL:
            nulo = _InstrumentoNulo()
            self.tamano_documento = nulo
            self.duracion_agente = nulo
            self.duracion_preproceso = nulo
            self.duracion_validacion = nulo
            self.peticiones = nulo
            self.exitos = nulo
            self.fallos = nulo
            self.revisiones = nulo
            self.reintentos = nulo
            return

        meter = metrics.get_meter(f"amp.{AGENT_NAME}")
        self.tamano_documento = meter.create_histogram(
            "expense.ocr.document.size", unit="By", description="Tamano del justificante recibido"
        )
        self.duracion_agente = meter.create_histogram(
            "expense.ocr.agent.duration", unit="s", description="Duracion total del analisis"
        )
        self.duracion_preproceso = meter.create_histogram(
            "expense.ocr.preprocess.duration", unit="s", description="Duracion del preprocesado"
        )
        self.duracion_validacion = meter.create_histogram(
            "expense.ocr.validation.duration", unit="s", description="Duracion de la validacion de esquema"
        )
        self.peticiones = meter.create_counter("expense.ocr.requests")
        self.exitos = meter.create_counter("expense.ocr.successes")
        self.fallos = meter.create_counter("expense.ocr.failures")
        self.revisiones = meter.create_counter("expense.ocr.review_required")
        self.reintentos = meter.create_counter("expense.ocr.retries")


_METRICAS: Optional[_Metricas] = None


def metricas() -> _Metricas:
    global _METRICAS
    if _METRICAS is None:
        _METRICAS = _Metricas()
    return _METRICAS


def etiquetas_base(modelo: Optional[str] = None, mime_type: Optional[str] = None,
                   type_hint: Optional[str] = None) -> Dict[str, Any]:
    """Etiquetas de metrica. Todas enumeradas o de dominio cerrado.

    Nada de document id, user id, comercio, fecha, total ni request id: cada
    valor distinto es una serie temporal nueva y eso es lo que revienta una
    plataforma de metricas.
    """
    return _limpiar(
        {
            "environment": CONFIG.entorno,
            "model": modelo,
            "document.mime_type": mime_type,
            "document.type_hint": type_hint,
        }
    )


# ------------------------------------------------------------------- redactor

# Atributos que leen los evaluadores de nivel agente de AMP. En modo
# `redacted` se conservan con el PII enmascarado; sin ellos el monitor se
# ejecuta pero no tiene nada que puntuar.
CLAVES_MENSAJES = ("gen_ai.input.messages", "gen_ai.output.messages")

# Contenido crudo que escribe la instrumentacion automatica de OpenAI/Traceloop:
# lleva el prompt entero y el marcador de la imagen. Nunca sobrevive a `redacted`.
PREFIJOS_CONTENIDO = (
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.system_instructions",
    "llm.prompts",
    "llm.completions",
    "traceloop.entity.input",
    "traceloop.entity.output",
)

# Claves del JSON del gasto con datos personales o de negocio. En `redacted` se
# sustituyen por un marcador: la estructura y los importes siguen siendo
# evaluables, el comercio y el texto libre no salen del pod.
CLAVES_PII = ("comercio", "resumen", "descripcion", "fecha", "direccion", "nif", "cif")

# Atributos que jamas deben salir, capture o no capture contenido.
CLAVES_PROHIBIDAS = (
    "gen_ai.request.image",
    "expense.document.image.data",
    "expense.document.filename",
    "amp.entrada.nombre",
)

REDACTADO = "[redactado]"


def _redactar_endpoint(valor: Any) -> Any:
    """Deja solo el host logico del gateway: sin puerto, namespace ni ruta."""
    try:
        partes = urlsplit(str(valor))
        return partes.hostname or REDACTADO
    except Exception:  # pragma: no cover
        return REDACTADO


def _enmascarar(nodo: Any) -> Any:
    """Recorre el JSON del mensaje y tapa las claves con datos personales.

    Baja tambien por los strings que a su vez son JSON: el contrato de
    `gen_ai.output.messages` es una lista de mensajes cuyo `content` es el JSON
    del gasto ya serializado, y ahi es donde esta el comercio.
    """
    if isinstance(nodo, dict):
        return {
            k: (REDACTADO if k.lower() in CLAVES_PII and v is not None else _enmascarar(v))
            for k, v in nodo.items()
        }
    if isinstance(nodo, list):
        return [_enmascarar(v) for v in nodo]
    if isinstance(nodo, str):
        try:
            anidado = json.loads(nodo)
        except ValueError:
            return nodo
        if isinstance(anidado, (dict, list)):
            return json.dumps(_enmascarar(anidado), ensure_ascii=False)
    return nodo


def redactar_mensajes(valor: Any) -> Any:
    """Deja el mensaje evaluable pero sin PII.

    Si no es JSON, no hay forma de saber que hay dentro y se sustituye entero:
    adivinar donde esta el dato sensible en texto libre es como se filtran.
    """
    try:
        datos = json.loads(valor) if isinstance(valor, str) else valor
    except (ValueError, TypeError):
        return REDACTADO

    if isinstance(datos, (dict, list)):
        return json.dumps(_enmascarar(datos), ensure_ascii=False)
    return REDACTADO


def _sin_imagen(valor: Any) -> Any:
    """La imagen no sale nunca, ni con `full`.

    `full` es para depurar el prompt en local, no para volcar el justificante
    entero en la traza: son cientos de KB por span y es el dato mas sensible
    que maneja el agente.
    """
    if isinstance(valor, str) and ("base64," in valor or valor.startswith("data:")):
        return "[imagen no exportada]"
    return valor


def redactar_atributos(atributos: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Aplica la politica de privacidad a los atributos de un span.

    Se conservan tokens, modelo, latencia, estado, finish reason y todo lo
    tecnico: quitarlos dejaria la traza inutil, que es justo lo que se quiere
    evitar.
    """
    if not atributos:
        return {}
    nivel = CONFIG.captura
    salida: Dict[str, Any] = {}
    for clave, valor in atributos.items():
        if clave in CLAVES_PROHIBIDAS:
            continue
        if clave in ("gen_ai.openai.api_base", "server.address", "llm.request.api_base"):
            salida[clave] = _redactar_endpoint(valor)
            continue
        if nivel == CAPTURA_COMPLETA:
            salida[clave] = _sin_imagen(valor)
            continue
        if clave in CLAVES_MENSAJES:
            if nivel == CAPTURA_REDACTADA:
                salida[clave] = redactar_mensajes(valor)
            continue
        if clave.startswith(PREFIJOS_CONTENIDO):
            continue
        salida[clave] = _sin_imagen(valor)
    return salida


class _SpanRedactado:
    """Proxy de solo lectura sobre un span terminado, con atributos filtrados.

    Un `SpanProcessor` no puede modificar un span ya cerrado, asi que la
    redaccion se hace en el ultimo punto posible: envolviendo el exporter.
    """

    __slots__ = ("_span", "_attributes")

    def __init__(self, span, atributos):
        self._span = span
        self._attributes = atributos

    @property
    def attributes(self):
        return self._attributes

    def __getattr__(self, nombre):
        return getattr(self._span, nombre)


class RedactorExporter:
    """Envuelve un `SpanExporter` y filtra el contenido sensible."""

    def __init__(self, exporter):
        self._exporter = exporter

    def export(self, spans: Sequence[Any]):
        return self._exporter.export([self._filtrar(s) for s in spans])

    def _filtrar(self, span):
        try:
            originales = dict(span.attributes or {})
        except Exception:  # pragma: no cover
            return span
        redactados = redactar_atributos(originales)
        if redactados == originales:
            return span
        return _SpanRedactado(span, redactados)

    def shutdown(self):
        return self._exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000):
        return self._exporter.force_flush(timeout_millis)

    def __getattr__(self, nombre):
        return getattr(self._exporter, nombre)


def _procesadores(provider) -> list:
    activo = getattr(provider, "_active_span_processor", None)
    if activo is None:
        return []
    hijos = getattr(activo, "_span_processors", None)
    return list(hijos) if hijos else [activo]


def _sabe_exportar(valor) -> bool:
    return callable(getattr(valor, "export", None)) and callable(getattr(valor, "shutdown", None))


def envolver_exporter(procesador) -> bool:
    """Sustituye el exporter del procesador por el redactor.

    Se busca por pato, el atributo que sabe exportar, en vez de asignar
    `span_exporter`: desde el SDK 1.3x esa es una propiedad de solo lectura y
    el exporter vive en `_batch_processor._exporter`. Asignarla lanzaba
    AttributeError y la redaccion se quedaba sin instalar **en silencio
    salvo por un WARNING**, que es justo como se cuela un fallo de privacidad.
    """
    for portador in (procesador, getattr(procesador, "_batch_processor", None)):
        if portador is None:
            continue
        for nombre, valor in list(vars(portador).items()):
            if isinstance(valor, RedactorExporter):
                return True
            if _sabe_exportar(valor):
                try:
                    setattr(portador, nombre, RedactorExporter(valor))
                except Exception:  # pragma: no cover
                    continue
                return True
    return False


def instalar_redactor() -> str:
    """Engancha el redactor a los exporters del provider que AMP ya instalo.

    No crea provider ni exporter nuevos: localiza los que hay y les pone el
    filtro delante. Si el provider no es el del SDK (proxy, no-op o un
    provider propio), no hay nada que envolver y se dice.
    """
    if not _OTEL:
        return "sin OpenTelemetry: no hay nada que redactar"
    # Se instala en los tres niveles: incluso con `full` hay que impedir que la
    # imagen en base64 acabe en un atributo.
    try:
        provider = trace.get_tracer_provider()
        procesadores = _procesadores(provider)
        envueltos = sum(1 for p in procesadores if envolver_exporter(p))
        if not envueltos:
            return (
                f"AVISO: no se pudo envolver ningun exporter de {len(procesadores)} "
                "procesador(es); el contenido de la instrumentacion automatica sale SIN redactar"
            )
        return f"redactor '{CONFIG.captura}' instalado en {envueltos} exporter(s)"
    except Exception as exc:  # pragma: no cover
        logger.warning("no se pudo instalar el redactor de trazas: %s", exc)
        return f"redactor no instalado: {exc}"


# ------------------------------------------------------------- logs correlados


class FiltroCorrelacion(logging.Filter):
    """Anade trace_id, span_id, servicio y entorno a cada registro."""

    def filter(self, record: logging.LogRecord) -> bool:
        ids = ids_de_traza()
        record.trace_id = ids.get("trace_id", "")
        record.span_id = ids.get("span_id", "")
        record.service_name = os.getenv("OTEL_SERVICE_NAME", AGENT_NAME)
        record.environment = CONFIG.entorno
        return True


def instalar_correlacion_de_logs(logger_objetivo: logging.Logger) -> None:
    for handler in logging.getLogger().handlers or []:
        handler.addFilter(FiltroCorrelacion())
    logger_objetivo.addFilter(FiltroCorrelacion())
