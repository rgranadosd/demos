"""Contrato `expense-v1` del justificante y su validacion.

El prompt pide al modelo un JSON con unas claves concretas. Hasta ahora nadie
comprobaba que lo devuelto se pareciera a eso: pydantic ignoraba las claves
sobrantes y aceptaba tipos raros por coercion. Aqui se valida de verdad, y el
resultado se publica en la traza como `expense.ocr.output_schema_valid`.

Las reglas fallidas se identifican con **codigos cortos y cerrados**
(`total_no_numerico`, `tipo_documento_invalido`...). Nunca con valores del
documento: en la traza va el nombre de la regla, jamas el importe ni el
comercio.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

VERSION_ESQUEMA_POR_DEFECTO = "expense-v1"

TIPOS_DOCUMENTO = ("ticket", "factura", "recibo", "otro")

CAMPOS_ESPERADOS = (
    "tipo_documento",
    "comercio",
    "fecha",
    "total",
    "moneda",
    "impuestos",
    "base_imponible",
    "metodo_pago",
    "categoria_estimada",
    "lineas_principales",
    "resumen",
    "legible",
    "advertencias",
)

CAMPOS_LINEA = ("descripcion", "cantidad", "precio_unitario", "importe")

#: El contrato, en JSON Schema. Se publica en la documentacion y en `/health`
#: para que quien consuma el agente sepa contra que se valida.
ESQUEMA_JSON: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "expense-v1",
    "type": "object",
    "additionalProperties": False,
    "required": list(CAMPOS_ESPERADOS),
    "properties": {
        "tipo_documento": {"type": ["string", "null"], "enum": list(TIPOS_DOCUMENTO) + [None]},
        "comercio": {"type": ["string", "null"]},
        "fecha": {"type": ["string", "null"]},
        "total": {"type": ["number", "null"]},
        "moneda": {"type": ["string", "null"]},
        "impuestos": {"type": ["number", "null"]},
        "base_imponible": {"type": ["number", "null"]},
        "metodo_pago": {"type": ["string", "null"]},
        "categoria_estimada": {"type": ["string", "null"]},
        "lineas_principales": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "descripcion": {"type": ["string", "null"]},
                    "cantidad": {"type": ["number", "null"]},
                    "precio_unitario": {"type": ["number", "null"]},
                    "importe": {"type": ["number", "null"]},
                },
            },
        },
        "resumen": {"type": ["string", "null"]},
        "legible": {"type": "boolean"},
        "advertencias": {"type": "array", "items": {"type": "string"}},
    },
}

_CAMPOS_NUMERICOS = ("total", "impuestos", "base_imponible")
_CAMPOS_TEXTO = (
    "comercio",
    "fecha",
    "moneda",
    "metodo_pago",
    "categoria_estimada",
    "resumen",
)


def _es_numero(valor: Any) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool)


def validar(datos: Any) -> List[str]:
    """Devuelve la lista de codigos de regla incumplidos. Vacia = valido.

    Se para en la primera capa de cada rama: si `lineas_principales` no es una
    lista, no tiene sentido seguir mirando dentro.
    """
    fallos: List[str] = []

    if not isinstance(datos, dict):
        return ["raiz_no_objeto"]

    for campo in CAMPOS_ESPERADOS:
        if campo not in datos:
            fallos.append("campo_ausente")
            break

    if any(clave not in CAMPOS_ESPERADOS for clave in datos):
        fallos.append("campo_desconocido")

    tipo = datos.get("tipo_documento")
    if tipo is not None and tipo not in TIPOS_DOCUMENTO:
        fallos.append("tipo_documento_invalido")

    for campo in _CAMPOS_NUMERICOS:
        valor = datos.get(campo)
        if valor is not None and not _es_numero(valor):
            fallos.append(f"{campo}_no_numerico")

    for campo in _CAMPOS_TEXTO:
        valor = datos.get(campo)
        if valor is not None and not isinstance(valor, str):
            fallos.append("campo_texto_no_cadena")
            break

    lineas = datos.get("lineas_principales")
    if lineas is not None and not isinstance(lineas, list):
        fallos.append("lineas_principales_no_lista")
    elif isinstance(lineas, list):
        for linea in lineas:
            if not isinstance(linea, dict):
                fallos.append("linea_no_objeto")
                break
            if any(clave not in CAMPOS_LINEA for clave in linea):
                fallos.append("linea_campo_desconocido")
                break
            if any(
                linea.get(c) is not None and not _es_numero(linea.get(c))
                for c in ("cantidad", "precio_unitario", "importe")
            ):
                fallos.append("linea_importe_no_numerico")
                break

    if "legible" in datos and not isinstance(datos.get("legible"), bool):
        fallos.append("legible_no_booleano")

    advertencias = datos.get("advertencias")
    if advertencias is not None and not isinstance(advertencias, list):
        fallos.append("advertencias_no_lista")
    elif isinstance(advertencias, list) and any(not isinstance(a, str) for a in advertencias):
        fallos.append("advertencia_no_cadena")

    return fallos


def type_hint(datos: Any) -> str:
    """Etiqueta cerrada para la traza y las metricas.

    Solo devuelve un valor del enum del contrato; cualquier otra cosa que diga
    el modelo se convierte en `unknown`, porque un atributo con texto libre del
    documento es exactamente lo que no debe indexarse.
    """
    if not isinstance(datos, dict):
        return "unknown"
    tipo = datos.get("tipo_documento")
    if isinstance(tipo, str) and tipo.strip().lower() in TIPOS_DOCUMENTO:
        return tipo.strip().lower()
    return "unknown"


def resumen_validacion(datos: Any) -> Tuple[bool, List[str]]:
    fallos = validar(datos)
    return (not fallos), fallos


def campos_del_gasto(datos: Any, permitidos) -> Dict[str, Any]:
    """Filtra el JSON del modelo a los campos del modelo de datos de la app."""
    if not isinstance(datos, dict):
        return {}
    return {k: v for k, v in datos.items() if k in permitidos}


__all__ = [
    "CAMPOS_ESPERADOS",
    "CAMPOS_LINEA",
    "ESQUEMA_JSON",
    "TIPOS_DOCUMENTO",
    "VERSION_ESQUEMA_POR_DEFECTO",
    "campos_del_gasto",
    "resumen_validacion",
    "type_hint",
    "validar",
]
