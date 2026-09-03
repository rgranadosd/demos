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
import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from openai import OpenAI
from pydantic import BaseModel, Field

import llm_binding

logger = logging.getLogger("ocr-agent")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

try:
    from opentelemetry import trace

    _OTEL = True
except ImportError:  # pragma: no cover
    _OTEL = False

AGENT_NAME = "ocr-agent"

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

    # El override de Host solo se manda cuando la dirección tuvo que traducirse.
    http_client = (
        httpx.Client(headers={"Host": binding.host}, timeout=300.0)
        if binding.host
        else httpx.Client(timeout=300.0)
    )
    cliente = OpenAI(
        base_url=binding.base_url, api_key=binding.api_key, http_client=http_client
    )
    return cliente, modelo, binding


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


def _analizar(imagen_b64: str, mime_type: str) -> Analisis:
    cliente, modelo, _ = _cliente()

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

    crudo = respuesta.choices[0].message.content or ""
    advertencias: List[str] = []
    try:
        datos = _extraer_json(crudo)
    except (json.JSONDecodeError, IndexError):
        advertencias.append("el modelo no devolvió JSON válido")
        datos = {}

    legible = bool(datos.get("legible", True))
    advertencias.extend(datos.get("advertencias") or [])

    gasto = Gasto(**{k: v for k, v in datos.items() if k in Gasto.model_fields})

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

    return Analisis(
        gasto=gasto,
        legible=legible,
        cuadre=cuadre,
        advertencias=advertencias,
        modelo=respuesta.model or modelo,
        tokens=tokens,
        registrado=False,
    )


def _con_span_de_agente(imagen_b64: str, mime_type: str) -> Analisis:
    """Abre el span de agente que exige el contrato de instrumentación de AMP.

    `gen_ai.operation.name` tiene que ser uno de los seis valores de la
    enumeración. Con cualquier otro, AMP no deriva el kind y el span queda mudo
    — sin icono, sin ficha de agente y sin evaluadores — y no avisa de nada.
    """
    if not _OTEL:
        return _analizar(imagen_b64, mime_type)

    tracer = trace.get_tracer(f"amp.{AGENT_NAME}")
    with tracer.start_as_current_span(
        "analizar_gasto",
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.system": os.getenv("AMP_GENAI_SYSTEM", "openai"),
            "gen_ai.agent.name": AGENT_NAME,
            "traceloop.span.kind": "agent",
        },
    ) as span:
        resultado = _analizar(imagen_b64, mime_type)
        # Los evaluadores de nivel agente leen estos dos atributos. Sin ellos el
        # monitor se ejecuta pero no tiene nada que puntuar.
        span.set_attribute(
            "gen_ai.input.messages",
            json.dumps([{"role": "user", "content": "[imagen de justificante de gasto]"}]),
        )
        span.set_attribute(
            "gen_ai.output.messages",
            json.dumps([{"role": "assistant", "content": resultado.gasto.model_dump_json()}]),
        )
        span.set_attribute("gen_ai.request.model", resultado.modelo)
        if resultado.tokens:
            span.set_attribute("gen_ai.usage.input_tokens", resultado.tokens["entrada"])
            span.set_attribute("gen_ai.usage.output_tokens", resultado.tokens["salida"])
        return resultado


# -------------------------------------------------------------------- endpoints


@app.get("/health")
def health() -> Dict[str, Any]:
    """Estado y, sobre todo, qué LLM le ha asignado AMP."""
    binding = llm_binding.resolver()
    return {
        "status": "ok",
        "agente": AGENT_NAME,
        "llm_asignado": bool(binding),
        "llm_origen": binding.origen if binding else None,
        "llm_base_url": binding.base_url if binding else None,
        "modelo": llm_binding.modelo() or None,
    }


@app.post("/gastos/analizar", response_model=Analisis)
def analizar(peticion: PeticionBase64) -> Analisis:
    """Analiza un justificante. Nunca registra nada."""
    try:
        return _con_span_de_agente(peticion.imagen_base64, peticion.mime_type)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("fallo el analisis")
        raise HTTPException(status_code=502, detail=f"error llamando al gateway: {exc}")


@app.post("/gastos/analizar/fichero", response_model=Analisis)
async def analizar_fichero(fichero: UploadFile = File(...)) -> Analisis:
    """Igual que /gastos/analizar, subiendo el fichero directamente."""
    contenido = await fichero.read()
    b64 = base64.b64encode(contenido).decode()
    try:
        return _con_span_de_agente(b64, fichero.content_type or "image/jpeg")
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
    logger.info("registro confirmado: %s", peticion.gasto.model_dump_json())
    return {
        "registrado": False,
        "requiere_confirmacion": False,
        "mensaje": (
            "Confirmación recibida. No hay ningún sistema de gastos conectado "
            "en esta demo, así que no se ha dado de alta nada."
        ),
        "datos_a_enviar": peticion.gasto.model_dump(),
    }
