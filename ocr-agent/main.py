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
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
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
    los atributos amp.entrada.* del span de agente.
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


AGENT_NAME = "ocr-agent"

logger.info("instrumentacion: %s", _silenciar_subida_de_imagenes())

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


def _dimensiones(contenido: bytes) -> Optional[str]:
    """Ancho x alto leyendo la cabecera, sin depender de Pillow.

    Interesa en la traza: una captura de webcam a 1920x1080 y una miniatura de
    320x240 dan resultados muy distintos, y sin este dato no hay forma de saber
    cual se envio cuando una extraccion sale mal.
    """
    try:
        if contenido[:8] == b"\x89PNG\r\n\x1a\n":
            w = int.from_bytes(contenido[16:20], "big")
            h = int.from_bytes(contenido[20:24], "big")
            return f"{w}x{h}"
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
                    return f"{w}x{h}"
                i += 2 + int.from_bytes(contenido[i + 2:i + 4], "big")
    except Exception:
        pass
    return None


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


def _analizar(contenido: bytes, mime_type: str) -> Analisis:
    cliente, modelo, _ = _cliente()
    imagen_b64 = base64.b64encode(contenido).decode()

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


def _contexto_entrante(cabeceras: Optional[Dict[str, str]]):
    """Continua la traza que venga en la cabecera `traceparent`, si la hay.

    En el pod solo esta instrumentado `requests`: no hay instrumentacion de
    FastAPI ni de ASGI, asi que nadie extrae el contexto W3C de la peticion
    entrante y cada llamada abria una traza nueva. Extrayendolo aqui, el span
    del agente cuelga de lo que haya iniciado el cliente y el recorrido
    completo queda bajo un mismo trace id.
    """
    if not cabeceras:
        return None
    try:
        from opentelemetry.propagate import extract
        return extract({k.lower(): v for k, v in cabeceras.items()})
    except Exception:
        return None


def _con_span_de_agente(contenido: bytes, mime_type: str,
                        origen: str = "api", nombre: str = "",
                        cabeceras: Optional[Dict[str, str]] = None) -> Analisis:
    """Abre el span de agente que exige el contrato de instrumentación de AMP.

    `gen_ai.operation.name` tiene que ser uno de los seis valores de la
    enumeración. Con cualquier otro, AMP no deriva el kind y el span queda mudo
    — sin icono, sin ficha de agente y sin evaluadores — y no avisa de nada.
    """
    if not _OTEL:
        return _analizar(contenido, mime_type)

    tracer = trace.get_tracer(f"amp.{AGENT_NAME}")
    with tracer.start_as_current_span(
        "analizar_gasto",
        context=_contexto_entrante(cabeceras),
        attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.system": os.getenv("AMP_GENAI_SYSTEM", "openai"),
            "gen_ai.agent.name": AGENT_NAME,
            "traceloop.span.kind": "agent",
            # De donde viene la imagen y como es. El SDK de Traceloop intenta
            # subir el blob a /v2/traces/.../images, un endpoint de Traceloop
            # Cloud que el gateway de AMP no implementa (404), asi que sin esto
            # la traza no diria nada de la entrada. Van en el namespace amp.*
            # porque no son claves del contrato semconv, solo atributos
            # buscables.
            "amp.entrada.origen": origen,
            "amp.entrada.mime": mime_type,
            "amp.entrada.bytes": len(contenido),
            "amp.entrada.dimensiones": _dimensiones(contenido) or "desconocidas",
            "amp.entrada.nombre": nombre or "(sin nombre)",
        },
    ) as span:
        resultado = _analizar(contenido, mime_type)
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
def analizar(peticion: PeticionBase64, request: Request) -> Analisis:
    """Analiza un justificante. Nunca registra nada."""
    try:
        contenido = base64.b64decode(peticion.imagen_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="imagen_base64 no es base64 valido")
    try:
        return _con_span_de_agente(contenido, peticion.mime_type, peticion.origen,
                                   cabeceras=dict(request.headers))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("fallo el analisis")
        raise HTTPException(status_code=502, detail=f"error llamando al gateway: {exc}")


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
    try:
        return _con_span_de_agente(contenido, fichero.content_type or "image/jpeg",
                                   origen, fichero.filename or "",
                                   cabeceras=dict(request.headers))
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
