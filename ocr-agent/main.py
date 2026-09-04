"""Agente de análisis de justificantes de gasto.

Recibe la imagen de un ticket, factura o recibo, la interpreta con un modelo de
visión **a través del AI Gateway de WSO2 Agent Manager** y devuelve el gasto
estructurado.

Lo que demuestra: el agente no sabe con qué modelo habla. La URL y la
credencial del gateway se las inyecta AMP (ver `llm_binding.py`), así que
cambiar de LLM — el local en LM Studio, Mistral, Azure — es cambiar el provider
en Agent Manager y redesplegar. Sin tocar una línea.

Dos reglas del encargo se implementan **en código y no en el prompt**, porque
un prompt es una súplica y esto tiene que ser una garantía:

- *"Comprueba si los importes parecen cuadrar"* → la aritmética la hace Python
  (`_revisar_cuadre`). Pedirle sumas a un modelo de lenguaje es justo lo que no
  hay que hacer.
- *"No registres el gasto sin confirmación explícita"* → `/gastos/registrar`
  rechaza cualquier petición sin `confirmado: true` y responde con la vista
  previa. No hay camino por el que el modelo pueda registrar nada por su
  cuenta.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

import agent_identity
import esquema
import llm_binding
import observabilidad as obs
import user_identity

logger = logging.getLogger("ocr-agent")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s [trace_id=%(trace_id)s span_id=%(span_id)s] %(message)s",
)
obs.instalar_correlacion_de_logs(logger)

def _silenciar_subida_de_imagenes() -> str:
    """Evita el 404 del subidor de imagenes sin llenar la traza de base64.

    El sitecustomize de AMP hace Traceloop.init(api_endpoint=...), y eso engancha
    el subidor de imagenes del SDK, que postea el blob a
    /v2/traces/{trace}/spans/{span}/images — un endpoint de Traceloop Cloud que
    el gateway de AMP no implementa. Resultado: un span hijo en rojo con 404 en
    cada llamada multimodal.

    Poner Config.upload_base64_image a None NO sirve: el SDK entonces se salta
    el preprocesado y escribe la data URL entera en el atributo del span
    (chat_wrappers.py:447). Con una foto de webcam serian cientos de KB por
    traza.

    Asi que se sustituye por una funcion propia que no llama a nadie y devuelve
    un marcador legible. El SDK lo coloca en lugar de la imagen: sin 404, sin
    base64, y en la traza queda constancia de que hubo una imagen y de su
    tamano. Lo que importa de verdad — origen, mime, dimensiones — va aparte en
    los atributos expense.document.* del span de agente.
    """
    try:
        from opentelemetry.instrumentation.openai.shared.config import Config
    except ImportError:
        return "sin SDK de instrumentacion"

    async def _marcador(trace_id, span_id, nombre, base64_string):
        kb = (len(base64_string) * 3 // 4) // 1024
        return f"[imagen no almacenada · {nombre} · ~{kb} KB]"

    Config.upload_base64_image = _marcador
    return "subidor de imagenes sustituido por un marcador"


AGENT_NAME = obs.AGENT_NAME

# Tipos que el modelo de vision acepta y que sabemos medir. Cerrado a proposito:
# lo que no este aqui se rechaza en `app.document.validate` con 415, en vez de
# gastar una llamada al gateway para que falle alla.
MIMES_ACEPTADOS = ("image/jpeg", "image/jpg", "image/png", "image/webp")

_FIRMAS = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
}

try:
    TAMANO_MAXIMO_BYTES = int(os.getenv("EXPENSE_OCR_MAX_BYTES", str(10 * 1024 * 1024)))
except ValueError:  # pragma: no cover
    TAMANO_MAXIMO_BYTES = 10 * 1024 * 1024

# Origen del documento, enumerado. Cualquier otra cosa cae en "unknown": un
# atributo con texto libre del cliente es cardinalidad sin control.
_ORIGENES = {
    "api": "api",
    "upload": "upload",
    "fichero": "upload",
    "file": "upload",
    "camara": "camera",
    "camera": "camera",
    "cli": "cli",
    "email": "email",
}

logger.info("instrumentacion: %s", _silenciar_subida_de_imagenes())
logger.info("privacidad: %s", obs.instalar_redactor())
logger.info("identidad: %s", agent_identity.describir())

app = FastAPI(
    title="ocr-agent — análisis de justificantes de gasto",
    description=(
        "Interpreta tickets, facturas y recibos con un modelo de visión "
        "servido a través del AI Gateway de Agent Manager."
    ),
    version="1.0.0",
)


# --------------------------------------------------------------- modelo de datos


class LineaGasto(BaseModel):
    descripcion: Optional[str] = None
    cantidad: Optional[float] = None
    precio_unitario: Optional[float] = None
    importe: Optional[float] = None


class Gasto(BaseModel):
    tipo_documento: Optional[str] = Field(
        default=None, description="ticket | factura | recibo | otro"
    )
    comercio: Optional[str] = None
    fecha: Optional[str] = None
    total: Optional[float] = None
    moneda: Optional[str] = None
    impuestos: Optional[float] = None
    base_imponible: Optional[float] = None
    metodo_pago: Optional[str] = None
    categoria_estimada: Optional[str] = None
    lineas_principales: List[LineaGasto] = Field(default_factory=list)
    resumen: Optional[str] = None


class Analisis(BaseModel):
    gasto: Gasto
    legible: bool = True
    cuadre: Dict[str, Any] = Field(default_factory=dict)
    advertencias: List[str] = Field(default_factory=list)
    modelo: str
    tokens: Dict[str, int] = Field(default_factory=dict)
    registrado: bool = False


class PeticionBase64(BaseModel):
    imagen_base64: str = Field(description="Imagen del justificante, sin cabecera data:")
    mime_type: str = Field(default="image/jpeg")
    origen: str = Field(
        default="api",
        description="De donde procede: camara, fichero, cli, api. Queda en la traza.",
    )


class PeticionRegistro(BaseModel):
    gasto: Gasto
    confirmado: bool = Field(
        default=False,
        description="Debe ser true. Sin ella el agente devuelve la vista previa "
        "y no registra nada.",
    )


PROMPT = """Analizas justificantes de gasto: tickets, facturas y recibos.

Devuelve SOLO un objeto JSON válido, sin texto alrededor ni bloques de código,
con exactamente estas claves:

  tipo_documento      uno de: ticket, factura, recibo, otro
  comercio            nombre del establecimiento o emisor
  fecha               DD/MM/AAAA tal y como aparezca
  total               número
  moneda              código ISO si es deducible (EUR, USD...)
  impuestos           importe total de impuestos, número
  base_imponible      número, si aparece
  metodo_pago         efectivo, tarjeta, transferencia, ... si aparece
  categoria_estimada  p.ej. restauracion, transporte, alojamiento, material,
                      combustible, telecomunicaciones, otros
  lineas_principales  [ { descripcion, cantidad, precio_unitario, importe } ]
                      importe = el numero impreso en esa linea del documento.
                      NUNCA lo calcules ni lo multipliques.
                      precio_unitario = solo si el documento lo muestra en una
                      columna aparte; si no, null. En un ticket con formato
                      "2  MENU DEL DIA  29,00" el 29,00 es el importe de la
                      linea, no el precio por unidad.
  resumen             una frase describiendo el gasto
  legible             true o false
  advertencias        lista de textos: datos ilegibles, ausentes o dudosos

Reglas estrictas:
- Los importes van como número, sin símbolo de moneda ni separador de millares.
- Si un dato no aparece o no se lee, pon null y añade una advertencia. NO lo
  inventes, NO lo deduzcas y NO lo estimes.
- Si la imagen no permite leer el documento con fiabilidad, pon legible en false
  y explica en advertencias qué impide leerlo.
"""


# ------------------------------------------------------------------- utilidades


def _cliente() -> tuple:
    """Construye el cliente OpenAI contra el gateway que AMP haya inyectado."""
    binding = llm_binding.resolver()
    modelo = llm_binding.modelo()

    if binding is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "No hay ningún LLM provider enganchado a este agente. "
                "Asígnale uno desde Agent Manager: AMP inyecta la URL y la "
                "credencial del gateway al desplegar."
            ),
        )
    if not modelo:
        raise HTTPException(
            status_code=503,
            detail=(
                "No hay modelo configurado (OCR_MODEL o AMP_GENAI_MODEL). "
                "Qué modelo se usa lo decide Agent Manager, no el agente."
            ),
        )

    # AMP autentica al consumidor (agente -> gateway) con la clave de suscripción
    # en una cabecera dedicada, X-API-Key por defecto. El SDK de OpenAI inyecta
    # siempre "Authorization: Bearer <api_key>", y esa cabecera NO puede llegar
    # al proveedor: la seguridad del gateway es la dueña del Authorization
    # upstream (el "Bearer <clave del proveedor>" real). Si se deja pasar, el
    # gateway responde 401 "Valid API key required".
    #
    # Así que la clave viaja en la cabecera de consumidor y el Authorization se
    # borra en el hook de salida. Es el mismo patrón que usan los agentes
    # cpc-studio en common/llm_utils.py.
    consumer_header = os.getenv("AMP_LLM_CONSUMER_HEADER", "X-API-Key").strip() or "X-API-Key"

    def _quitar_authorization(request: httpx.Request) -> None:
        request.headers.pop("authorization", None)

    cabeceras = {
        consumer_header: binding.api_key,
        "API-Key": binding.api_key,  # la cabecera de consumidor propia del proxy
    }
    # El override de Host solo se manda cuando la dirección tuvo que traducirse.
    if binding.host:
        cabeceras["Host"] = binding.host

    http_client = httpx.Client(
        headers=cabeceras,
        timeout=300.0,
        event_hooks={"request": [_quitar_authorization]},
    )
    cliente = OpenAI(
        base_url=binding.base_url,
        api_key="amp-managed",  # placeholder: se elimina antes de salir
        http_client=http_client,
    )
    return cliente, modelo, binding


def _dimensiones(contenido: bytes) -> Optional[Tuple[int, int]]:
    """Ancho y alto leyendo la cabecera, sin depender de Pillow.

    Interesa en la traza: una captura de webcam a 1920x1080 y una miniatura de
    320x240 dan resultados muy distintos, y sin este dato no hay forma de saber
    cual se envio cuando una extraccion sale mal.
    """
    try:
        if contenido[:8] == b"\x89PNG\r\n\x1a\n":
            w = int.from_bytes(contenido[16:20], "big")
            h = int.from_bytes(contenido[20:24], "big")
            return (w, h)
        if contenido[:2] == b"\xff\xd8":            # JPEG: recorrer segmentos
            i = 2
            while i < len(contenido) - 9:
                if contenido[i] != 0xFF:
                    i += 1
                    continue
                marca = contenido[i + 1]
                if marca in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(contenido[i + 5:i + 7], "big")
                    w = int.from_bytes(contenido[i + 7:i + 9], "big")
                    return (w, h)
                i += 2 + int.from_bytes(contenido[i + 2:i + 4], "big")
    except Exception:
        pass
    return None


class ErrorDocumento(Exception):
    """Documento rechazado antes de gastar una llamada al modelo."""

    def __init__(self, error_type: str, mensaje: str, status_code: int = 400):
        super().__init__(mensaje)
        self.error_type = error_type
        self.mensaje = mensaje
        self.status_code = status_code


def _origen_normalizado(origen: str) -> str:
    return _ORIGENES.get((origen or "").strip().lower(), "unknown")


def _hash_documento(document_id: str) -> Optional[str]:
    """Identificador opaco del documento, con sal.

    Sin `EXPENSE_OCR_ID_SALT` no se emite nada: un hash sin sal de un id corto
    se revierte por fuerza bruta en segundos y dejaria de ser seudonimo.
    """
    sal = os.getenv("EXPENSE_OCR_ID_SALT", "").strip()
    if not document_id or not sal:
        return None
    return hmac.new(sal.encode(), document_id.encode(), hashlib.sha256).hexdigest()[:16]


def _validar_documento(contenido: bytes, mime_type: str) -> Dict[str, Any]:
    """Fase `app.document.validate`. Devuelve los datos tecnicos del fichero.

    Lo que no pase de aqui no llega al gateway: un 415 barato ahorra una
    llamada al modelo que iba a fallar de todas formas.
    """
    mime = (mime_type or "").split(";")[0].strip().lower()

    if not contenido:
        raise ErrorDocumento("corrupt_file", "el fichero llego vacio", 400)
    if mime not in MIMES_ACEPTADOS:
        raise ErrorDocumento(
            "unsupported_media_type",
            f"tipo no soportado: {mime or 'desconocido'}. Se aceptan {', '.join(MIMES_ACEPTADOS)}",
            415,
        )
    if len(contenido) > TAMANO_MAXIMO_BYTES:
        raise ErrorDocumento(
            "file_too_large",
            f"la imagen ocupa {len(contenido)} bytes y el maximo es {TAMANO_MAXIMO_BYTES}",
            413,
        )

    firma = next((v for k, v in _FIRMAS.items() if contenido.startswith(k)), None)
    dimensiones = _dimensiones(contenido)
    # WebP no lleva una firma en `_FIRMAS` y sus dimensiones no se leen aqui;
    # se acepta si el cliente lo declara, pero sin firma ni tamano conocidos.
    if firma is None and mime != "image/webp":
        raise ErrorDocumento("invalid_image", "el contenido no es un PNG ni un JPEG valido", 400)

    ancho, alto = dimensiones if dimensiones else (None, None)
    if ancho is not None and (ancho <= 0 or alto <= 0):
        raise ErrorDocumento("invalid_image", "la imagen declara dimensiones imposibles", 400)

    if ancho is None:
        orientacion = "unknown"
    elif ancho > alto:
        orientacion = "landscape"
    else:
        orientacion = "portrait"

    return {
        "mime_type": mime,
        "size_bytes": len(contenido),
        "width": ancho,
        "height": alto,
        "orientation": orientacion,
        # Sin render de PDF, un justificante es siempre una pagina.
        "page_count": 1,
    }


def _llm_autoinstrumentado() -> bool:
    """Dice si el SDK de OpenAI ya viene envuelto por la instrumentacion de AMP.

    Si lo esta, la llamada al modelo ya genera su propio span `openai.chat` y
    envolverla en otro span de cliente seria duplicar la misma semantica.

    Se mira si el modulo esta cargado, no si `Completions.create` tiene
    `__wrapped__`: el propio SDK de OpenAI decora ese metodo con
    `functools.wraps`, asi que ese atributo esta siempre y daria un falso
    positivo que dejaria la llamada al modelo sin ningun span.
    """
    return "opentelemetry.instrumentation.openai" in sys.modules


@contextmanager
def _span_llamada_modelo(modelo: str) -> Iterator[Any]:
    """Span de la llamada al modelo, solo si nadie mas lo esta creando."""
    if _llm_autoinstrumentado():
        yield None
        return
    with obs.span_fase(
        f"chat {modelo}",
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": os.getenv("AMP_GENAI_SYSTEM", "openai"),
            "gen_ai.request.model": modelo,
            "gen_ai.request.temperature": 0,
            "gen_ai.request.max_tokens": 1500,
        },
    ) as span:
        yield span


def _extraer_json(texto: str) -> Dict[str, Any]:
    """Rescata el JSON aunque venga envuelto en ```json ... ```."""
    limpio = texto.strip()
    if limpio.startswith("```"):
        limpio = limpio.split("```")[1]
        if limpio.startswith("json"):
            limpio = limpio[4:]
    limpio = limpio.strip()
    try:
        return json.loads(limpio)
    except json.JSONDecodeError:
        ini, fin = limpio.find("{"), limpio.rfind("}")
        if ini >= 0 and fin > ini:
            return json.loads(limpio[ini : fin + 1])
        raise


def _revisar_cuadre(gasto: Gasto) -> Dict[str, Any]:
    """Comprueba la aritmética en Python, no en el modelo.

    Un LLM puede transcribir bien y sumar mal, y en un justificante de gasto la
    suma es precisamente el dato que alguien va a auditar. Se admite 1 céntimo
    de tolerancia por redondeos.
    """
    resultado: Dict[str, Any] = {}
    tol = 0.01

    if gasto.base_imponible is not None and gasto.impuestos is not None and gasto.total is not None:
        esperado = round(gasto.base_imponible + gasto.impuestos, 2)
        resultado["base_mas_impuestos_igual_total"] = abs(esperado - gasto.total) <= tol
        resultado["total_calculado"] = esperado

    importes = [l.importe for l in gasto.lineas_principales if l.importe is not None]
    if importes:
        suma = round(sum(importes), 2)
        resultado["suma_lineas"] = suma
        referencia = gasto.base_imponible if gasto.base_imponible is not None else gasto.total
        if referencia is not None:
            resultado["lineas_cuadran"] = abs(suma - referencia) <= tol

    for linea in gasto.lineas_principales:
        if None not in (linea.cantidad, linea.precio_unitario, linea.importe):
            esperado = round(linea.cantidad * linea.precio_unitario, 2)
            if abs(esperado - linea.importe) > tol:
                resultado.setdefault("lineas_descuadradas", []).append(
                    {
                        "descripcion": linea.descripcion,
                        "importe_declarado": linea.importe,
                        "importe_calculado": esperado,
                    }
                )

    return resultado


def _tipo_error_modelo(exc: BaseException) -> Tuple[str, Optional[int]]:
    """Traduce la excepcion del SDK a un `error.type` corto y estable."""
    import openai

    codigo = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(exc, openai.APITimeoutError):
        return "timeout", codigo
    if isinstance(exc, openai.RateLimitError):
        return "rate_limited", codigo or 429
    if isinstance(exc, openai.APIConnectionError):
        return "connection_error", codigo
    if isinstance(exc, openai.APIStatusError):
        codigo = codigo or getattr(exc, "status_code", None)
        if codigo and 500 <= codigo < 600:
            return "http_5xx", codigo
        if codigo and 400 <= codigo < 500:
            return "http_4xx", codigo
    return "model_error", codigo


_ERRORES_REINTENTABLES = ("timeout", "rate_limited", "connection_error", "http_5xx")


def _invocar_modelo(cliente, modelo: str, imagen_b64: str, mime_type: str):
    with _span_llamada_modelo(modelo) as span_llm:
        respuesta = cliente.chat.completions.create(
            model=modelo,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{imagen_b64}"},
                        },
                    ],
                }
            ],
            max_tokens=1500,
            # Extraer datos no es creativo: la variabilidad aquí es ruido que rompe
            # la reproducibilidad de las evaluaciones.
            temperature=0,
        )
        if span_llm is not None:
            _anotar_respuesta_modelo(span_llm, respuesta)
        return respuesta


def _anotar_respuesta_modelo(span, respuesta) -> None:
    """Atributos GenAI del lado respuesta. Solo si el span es nuestro."""
    if span is None:
        return
    if getattr(respuesta, "model", None):
        span.set_attribute("gen_ai.response.model", respuesta.model)
    razones = [c.finish_reason for c in (respuesta.choices or []) if c.finish_reason]
    if razones:
        span.set_attribute("gen_ai.response.finish_reasons", razones)
    uso = getattr(respuesta, "usage", None)
    if uso:
        span.set_attribute("gen_ai.usage.input_tokens", uso.prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", uso.completion_tokens)
        span.set_attribute("gen_ai.usage.total_tokens", uso.total_tokens)


def _llamar_con_reintentos(contenido: bytes, mime_type: str, span_agente):
    """Llama al modelo reintentando los fallos pasajeros y el JSON ilegible.

    Cada intento genera su propio span de modelo — el auto-instrumentado si lo
    hay — para que se vea dónde falló y no se pise el resultado del anterior.
    El fallo de cada intento queda como evento `gen_ai.retry` en el span de
    agente, que solo acaba en ERROR si se agotan los intentos.
    """
    cliente, modelo, _ = _cliente()
    with obs.span_fase("app.document.preprocess", {
        "expense.document.preprocess.applied": False,
    }) as span_pre:
        inicio = time.monotonic()
        # TODO: no hay rotación, normalización de contraste, resize ni render de
        # PDF. Cuando se añadan, van aquí como subspans app.image.* / app.pdf.render
        # y hay que rellenar expense.document.preprocess.operations.
        imagen_b64 = base64.b64encode(contenido).decode()
        duracion_pre = time.monotonic() - inicio
        span_pre.set_attribute("expense.document.preprocess.operations", [])
    obs.metricas().duracion_preproceso.record(
        duracion_pre, obs.etiquetas_base(modelo=modelo, mime_type=mime_type)
    )

    reintentos = 0
    ultimo_error: Optional[str] = None
    while True:
        try:
            respuesta = _invocar_modelo(cliente, modelo, imagen_b64, mime_type)
        except Exception as exc:
            tipo, codigo = _tipo_error_modelo(exc)
            reintentable = tipo in _ERRORES_REINTENTABLES and reintentos + 1 < obs.CONFIG.max_intentos
            span_agente.add_event(
                "gen_ai.retry" if reintentable else "gen_ai.error",
                obs._limpiar({
                    "retry.attempt": reintentos + 1,
                    "retry.reason": tipo,
                    "error.type": tipo,
                    "http.response.status_code": codigo,
                }),
            )
            if not reintentable:
                raise
            reintentos += 1
            ultimo_error = tipo
            obs.metricas().reintentos.add(1, obs.etiquetas_base(modelo=modelo, mime_type=mime_type))
            continue

        crudo = respuesta.choices[0].message.content or ""
        datos, json_valido = _fase_parse_json(crudo)
        if json_valido or reintentos + 1 >= obs.CONFIG.max_intentos:
            return respuesta, modelo, datos, json_valido, reintentos

        # Reintento de reparación: mismo prompt, nueva llamada, span propio.
        span_agente.add_event(
            "gen_ai.retry",
            {
                "retry.attempt": reintentos + 1,
                "retry.reason": "invalid_json",
                "error.type": "json_parse_error",
            },
        )
        reintentos += 1
        ultimo_error = "json_parse_error"
        obs.metricas().reintentos.add(1, obs.etiquetas_base(modelo=modelo, mime_type=mime_type))


def _fase_parse_json(crudo: str) -> Tuple[Dict[str, Any], bool]:
    """Fase `app.ocr.parse_json`. La salida cruda no se guarda como atributo."""
    with obs.span_fase("app.ocr.parse_json", {"expense.ocr.output_length": len(crudo)}) as span:
        try:
            datos = _extraer_json(crudo)
        except (json.JSONDecodeError, IndexError, ValueError) as exc:
            span.add_event("expense.ocr.json_parse_failed")
            span.set_attribute("expense.ocr.output_valid_json", False)
            obs.marcar_error(span, "json_parse_error", exc)
            return {}, False
        if not isinstance(datos, dict):
            span.add_event("expense.ocr.json_parse_failed")
            span.set_attribute("expense.ocr.output_valid_json", False)
            obs.marcar_error(span, "json_parse_error")
            return {}, False
        span.set_attribute("expense.ocr.output_valid_json", True)
        return datos, True


def _fase_validar_esquema(datos: Dict[str, Any], json_valido: bool, modelo: str,
                          mime_type: str) -> Tuple[bool, List[str]]:
    """Fase `app.ocr.validate_schema`. Solo salen nombres de regla, no valores."""
    inicio = time.monotonic()
    with obs.span_fase(
        "app.ocr.validate_schema",
        {"expense.ocr.schema_version": obs.CONFIG.version_esquema},
    ) as span:
        if not json_valido:
            span.set_attribute("expense.ocr.output_schema_valid", False)
            duracion = time.monotonic() - inicio
            obs.metricas().duracion_validacion.record(
                duracion, obs.etiquetas_base(modelo=modelo, mime_type=mime_type)
            )
            return False, []
        valido, fallos = esquema.resumen_validacion(datos)
        span.set_attribute("expense.ocr.output_schema_valid", valido)
        if not valido:
            span.set_attribute("expense.ocr.schema_failed_rules", fallos)
            span.add_event("expense.ocr.schema_validation_failed", {"rules": fallos})
            obs.marcar_error(span, "json_schema_validation_error")
        duracion = time.monotonic() - inicio
    obs.metricas().duracion_validacion.record(
        duracion, obs.etiquetas_base(modelo=modelo, mime_type=mime_type)
    )
    return valido, fallos


def _fase_calidad(datos: Dict[str, Any], gasto: "Gasto", legible: bool,
                  json_valido: bool, esquema_valido: bool,
                  advertencias: List[str]) -> Tuple[bool, str]:
    """Fase `app.ocr.quality_check`. Reglas deterministas, sin confianza inventada.

    No se estima ninguna puntuación de confianza: el modelo no la da y
    fabricarla convertiría un dato inventado en una decisión de negocio.
    """
    with obs.span_fase("app.ocr.quality_check") as span:
        if not json_valido:
            motivo = obs.REVISION_JSON_INVALIDO
        elif not esquema_valido:
            motivo = obs.REVISION_ESQUEMA_INVALIDO
        elif not legible:
            motivo = obs.REVISION_ILEGIBLE
        elif gasto.total is None:
            motivo = obs.REVISION_SIN_TOTAL
        else:
            motivo = obs.REVISION_NINGUNA

        requiere = motivo != obs.REVISION_NINGUNA
        span.set_attributes({
            "expense.ocr.legible": legible,
            "expense.ocr.warning_count": len(advertencias),
            "expense.ocr.review_required": requiere,
            "expense.ocr.review_reason": motivo,
        })
        # TODO: `expense.ocr.confidence` solo cuando exista una fuente de
        # confianza real y calibrada; el modelo actual no la devuelve.
        return requiere, motivo


def _construir_gasto(datos: Dict[str, Any]) -> "Gasto":
    """Arma el gasto descartando los campos que el modelo devolvio mal tipados.

    El esquema ya ha dejado constancia del incumplimiento en la traza; aqui lo
    que toca es devolver lo que si se pudo leer, no tumbar la peticion entera
    por un `total` que vino como texto.
    """
    campos = esquema.campos_del_gasto(datos, Gasto.model_fields)
    try:
        return Gasto(**campos)
    except ValidationError as exc:
        for error in exc.errors():
            loc = error.get("loc") or ()
            if loc:
                campos.pop(loc[0], None)
    try:
        return Gasto(**campos)
    except ValidationError:
        return Gasto()


def _analizar(contenido: bytes, mime_type: str, span_agente) -> Tuple[Analisis, Dict[str, Any]]:
    respuesta, modelo, datos, json_valido, reintentos = _llamar_con_reintentos(
        contenido, mime_type, span_agente
    )

    advertencias: List[str] = []
    if not json_valido:
        advertencias.append("el modelo no devolvió JSON válido")

    esquema_valido, _fallos = _fase_validar_esquema(datos, json_valido, modelo, mime_type)

    legible = bool(datos.get("legible", True))
    advertencias.extend(datos.get("advertencias") or [])

    gasto = _construir_gasto(datos)

    # Regla 10: si no se lee, se pide otra foto en vez de devolver datos a medias.
    if not legible:
        advertencias.append(
            "La imagen no es suficientemente legible. Envía una nueva fotografía "
            "con mejor enfoque e iluminación, y el documento completo en el encuadre."
        )

    for campo, etiqueta in (
        ("total", "el importe total"),
        ("fecha", "la fecha"),
        ("comercio", "el comercio"),
    ):
        if getattr(gasto, campo) is None:
            advertencias.append(f"no se ha podido determinar {etiqueta}")

    cuadre = _revisar_cuadre(gasto)
    if cuadre.get("base_mas_impuestos_igual_total") is False:
        advertencias.append(
            f"base imponible + impuestos = {cuadre.get('total_calculado')}, "
            f"pero el documento declara {gasto.total}"
        )
    if cuadre.get("lineas_cuadran") is False:
        advertencias.append(
            f"las líneas suman {cuadre.get('suma_lineas')}, que no coincide con el documento"
        )
    if cuadre.get("lineas_descuadradas"):
        advertencias.append(
            f"{len(cuadre['lineas_descuadradas'])} línea(s) con cantidad x precio "
            "distinto del importe declarado"
        )

    uso = respuesta.usage
    tokens = (
        {"entrada": uso.prompt_tokens, "salida": uso.completion_tokens, "total": uso.total_tokens}
        if uso
        else {}
    )

    revisar, motivo = _fase_calidad(
        datos, gasto, legible, json_valido, esquema_valido, advertencias
    )

    analisis = Analisis(
        gasto=gasto,
        legible=legible,
        cuadre=cuadre,
        advertencias=advertencias,
        modelo=respuesta.model or modelo,
        tokens=tokens,
        registrado=False,
    )
    diagnostico = {
        "modelo": analisis.modelo,
        "json_valido": json_valido,
        "esquema_valido": esquema_valido,
        "reintentos": reintentos,
        "revisar": revisar,
        "motivo": motivo,
        "type_hint": esquema.type_hint(datos),
        "finish_reasons": [c.finish_reason for c in (respuesta.choices or []) if c.finish_reason],
    }
    return analisis, diagnostico


def _analizar_justificante(contenido: bytes, mime_type: str,
                           origen: str = "api",
                           cabeceras: Optional[Dict[str, str]] = None) -> Analisis:
    """Span raíz del agente: `invoke_agent ocr-agent`, con sus fases colgando.

    `gen_ai.operation.name` tiene que ser uno de los seis valores de la
    enumeración. Con cualquier otro, AMP no deriva el kind y el span queda mudo
    — sin icono, sin ficha de agente y sin evaluadores — y no avisa de nada.
    """
    cabeceras = {k.lower(): v for k, v in (cabeceras or {}).items()}
    origen_norm = _origen_normalizado(origen)
    inicio = time.monotonic()

    # Antes de abrir el span: un token falsificado no merece traza de analisis,
    # merece un 401.
    usuario = user_identity.identidad_usuario(cabeceras)

    atributos = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.system": os.getenv("AMP_GENAI_SYSTEM", "openai"),
        "gen_ai.agent.name": AGENT_NAME,
        "gen_ai.agent.version": obs.CONFIG.version_agente,
        "traceloop.span.kind": "agent",
        "service.version": obs.CONFIG.version_servicio,
        "deployment.environment.name": obs.CONFIG.entorno,
        "expense.ocr.schema_version": obs.CONFIG.version_esquema,
        "expense.document.source": origen_norm,
        "expense.document.mime_type": (mime_type or "").split(";")[0].strip().lower(),
        "expense.document.size_bytes": len(contenido),
        "expense.document.id_hash": _hash_documento(cabeceras.get("x-document-id", "")),
        # Solo se propaga un id de conversación que ya venga de fuera: fabricar
        # uno aquí no correlacionaría nada, sería un uuid por petición.
        "gen_ai.conversation.id": cabeceras.get("x-conversation-id") or None,
    }
    # Quién actúa, según ThunderID y no según una cadena escrita en el código.
    atributos.update(agent_identity.identidad_agente().atributos())
    # Y por cuenta de quién, si la petición trae un token de usuario.
    atributos.update(usuario.atributos())

    etiquetas = obs.etiquetas_base(mime_type=atributos["expense.document.mime_type"])
    obs.metricas().peticiones.add(1, etiquetas)
    obs.metricas().tamano_documento.record(len(contenido), etiquetas)

    with obs.span_fase(
        f"invoke_agent {AGENT_NAME}",
        atributos,
        contexto=obs.contexto_entrante(cabeceras),
    ) as span:
        try:
            with obs.span_fase("app.document.validate") as span_val:
                try:
                    documento = _validar_documento(contenido, mime_type)
                except ErrorDocumento as exc:
                    obs.marcar_error(span_val, exc.error_type, exc)
                    raise
                span_val.set_attributes(obs._limpiar({
                    "expense.document.image.width": documento["width"],
                    "expense.document.image.height": documento["height"],
                    "expense.document.image.orientation": documento["orientation"],
                    "expense.document.page_count": documento["page_count"],
                }))

            span.set_attributes(obs._limpiar({
                "expense.document.image.width": documento["width"],
                "expense.document.image.height": documento["height"],
                "expense.document.image.orientation": documento["orientation"],
                "expense.document.page_count": documento["page_count"],
            }))
            # TODO: no hay fase app.document.store; el justificante no se
            # persiste en ningún backend. Al añadirlo, instrumentar aquí con
            # `expense.document.store.backend` enumerado (s3 | filesystem | blob).

            analisis, diag = _analizar(contenido, mime_type, span)

            span.set_attributes({
                "gen_ai.request.model": analisis.modelo,
                "expense.document.type_hint": diag["type_hint"],
                "expense.ocr.output_valid_json": diag["json_valido"],
                "expense.ocr.output_schema_valid": diag["esquema_valido"],
                "expense.ocr.legible": analisis.legible,
                "expense.ocr.warning_count": len(analisis.advertencias),
                "expense.ocr.retry_count": diag["reintentos"],
                "expense.ocr.review_required": diag["revisar"],
                "expense.ocr.review_reason": diag["motivo"],
            })
            if diag["finish_reasons"]:
                span.set_attribute("gen_ai.response.finish_reasons", diag["finish_reasons"])
            if analisis.tokens:
                span.set_attributes({
                    "gen_ai.usage.input_tokens": analisis.tokens["entrada"],
                    "gen_ai.usage.output_tokens": analisis.tokens["salida"],
                    "gen_ai.usage.total_tokens": analisis.tokens["total"],
                })
            # Los evaluadores de AMP leen estos dos atributos. El redactor
            # decide qué sale de ellos según OTEL_GENAI_CAPTURE_CONTENT.
            span.set_attribute(
                "gen_ai.input.messages",
                json.dumps([{"role": "user", "content": "[imagen de justificante de gasto]"}]),
            )
            span.set_attribute(
                "gen_ai.output.messages",
                json.dumps([{"role": "assistant", "content": analisis.gasto.model_dump_json()}]),
            )

            etiquetas_fin = obs.etiquetas_base(
                modelo=analisis.modelo,
                mime_type=documento["mime_type"],
                type_hint=diag["type_hint"],
            )
            resultado = "review" if diag["revisar"] else "success"
            obs.metricas().exitos.add(1, dict(etiquetas_fin, result=resultado))
            if diag["revisar"]:
                obs.metricas().revisiones.add(
                    1, dict(etiquetas_fin, **{"error.type": diag["motivo"]})
                )
            logger.info(
                "analisis completado ocr_result_status=%s review_required=%s retries=%s",
                resultado, diag["revisar"], diag["reintentos"],
            )
            return analisis

        except ErrorDocumento as exc:
            obs.marcar_error(span, exc.error_type, exc)
            obs.metricas().fallos.add(1, dict(etiquetas, **{"error.type": exc.error_type}))
            logger.warning("documento rechazado error.type=%s", exc.error_type)
            raise
        except Exception as exc:
            tipo, codigo = _tipo_error_modelo(exc)
            obs.marcar_error(span, tipo, exc, obs._limpiar({"http.response.status_code": codigo}))
            span.set_attribute("expense.ocr.review_required", True)
            span.set_attribute("expense.ocr.review_reason", obs.REVISION_ERROR_MODELO)
            obs.metricas().fallos.add(1, dict(etiquetas, **{"error.type": tipo}))
            logger.error("fallo el analisis error.type=%s", tipo)
            raise
        finally:
            obs.metricas().duracion_agente.record(time.monotonic() - inicio, etiquetas)


# -------------------------------------------------------------------- endpoints


@app.get("/health")
def health() -> Dict[str, Any]:
    """Estado y, sobre todo, qué LLM le ha asignado AMP."""
    binding = llm_binding.resolver()
    identidad = agent_identity.identidad_agente()
    return {
        "status": "ok",
        "agente": AGENT_NAME,
        "llm_asignado": bool(binding),
        "llm_origen": binding.origen if binding else None,
        "llm_base_url": binding.base_url if binding else None,
        "modelo": llm_binding.modelo() or None,
        "identidad_agente": identidad.agent_id,
        "identidad_origen": identidad.origen,
    }


@app.post("/gastos/analizar", response_model=Analisis)
def analizar(peticion: PeticionBase64, request: Request) -> Analisis:
    """Analiza un justificante. Nunca registra nada."""
    try:
        contenido = base64.b64decode(peticion.imagen_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="imagen_base64 no es base64 valido")
    return _responder_analisis(contenido, peticion.mime_type, peticion.origen, request)


@app.post("/gastos/analizar/fichero", response_model=Analisis)
async def analizar_fichero(
    request: Request,
    fichero: UploadFile = File(...),
    origen: str = Form(default="fichero"),
) -> Analisis:
    """Igual que /gastos/analizar, subiendo el fichero directamente.

    `origen` dice si la imagen viene de la camara, de un fichero o del CLI. No
    cambia el analisis: queda en la traza para poder distinguirlos despues.
    """
    contenido = await fichero.read()
    return _responder_analisis(contenido, fichero.content_type or "image/jpeg", origen, request)


def _responder_analisis(contenido: bytes, mime_type: str, origen: str,
                        request: Request) -> Analisis:
    """Traduce el resultado del agente a la respuesta HTTP, sin filtrar detalles."""
    try:
        return _analizar_justificante(contenido, mime_type, origen, dict(request.headers))
    except user_identity.TokenDeUsuarioInvalido as exc:
        logger.warning("token de usuario rechazado error.type=%s", exc.error_type)
        raise HTTPException(status_code=401, detail=exc.motivo)
    except ErrorDocumento as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.mensaje)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("fallo el analisis")
        raise HTTPException(status_code=502, detail=f"error llamando al gateway: {exc}")


@app.post("/gastos/registrar")
def registrar(peticion: PeticionRegistro) -> Dict[str, Any]:
    """Registra el gasto, y solo con confirmación explícita.

    Sin `confirmado: true` devuelve la vista previa de lo que se enviaría y no
    registra nada. La regla vive aquí, en el código, no en el prompt: así no
    depende de que el modelo se porte bien.
    """
    if not peticion.confirmado:
        return {
            "registrado": False,
            "requiere_confirmacion": True,
            "mensaje": (
                "Estos son los datos que se enviarían. Revísalos y vuelve a "
                "llamar con confirmado=true para registrar el gasto."
            ),
            "datos_a_enviar": peticion.gasto.model_dump(),
        }

    # Aquí iría la integración con el sistema de gastos. En la demo se deja
    # explícito que no hay destino real, en vez de fingir un alta.
    with obs.span_fase("app.expense.persist", {
        "expense.persist.backend": "none",
        "expense.persist.operation": "create",
    }) as span:
        # TODO: al conectar el sistema de gastos, poner el backend real
        # (postgres | mysql | api) y result=success|failure. Nunca el id del
        # gasto, el comercio ni los importes.
        span.set_attribute("expense.persist.result", "skipped")
    logger.info("registro confirmado sin backend conectado")
    return {
        "registrado": False,
        "requiere_confirmacion": False,
        "mensaje": (
            "Confirmación recibida. No hay ningún sistema de gastos conectado "
            "en esta demo, así que no se ha dado de alta nada."
        ),
        "datos_a_enviar": peticion.gasto.model_dump(),
    }


if __name__ == "__main__":
    # Permite arrancarlo con `python main.py`, que es el comando por defecto del
    # formulario de AMP. Respeta PORT si la plataforma lo inyecta, en vez de
    # clavar el 8080 y quedarse sin recibir tráfico si asigna otro.
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
    )
