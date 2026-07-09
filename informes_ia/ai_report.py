"""
Módulo 3: Generación del JSON del informe usando IA (Gemini).

Este módulo separa deliberadamente "qué se le pide a la IA" (los perfiles
de tono/rol) de "cómo se llama a la IA", para que agregar un nuevo tipo de
reporte (ej. "directorio", "regulatorio") sea sumar una entrada al dict
PERFILES sin tocar el resto del pipeline.

El contenido que ve la IA es el `resumen` ya pre-procesado por
preprocess.generar_resumen() (infra + actividad clínica + software), NO
las tablas crudas — ver preprocess.py para el porqué.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import google.generativeai as genai

from .logging_utils import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class PerfilReporte:
    rol: str
    tono: str
    instrucciones: str


PERFILES: dict[str, PerfilReporte] = {
    "cliente": PerfilReporte(
        rol="GERENTE DE CUENTAS (CUSTOMER SUCCESS)",
        tono="Estratégico, vendedor, enfocado en estabilidad y paz mental. NO técnico.",
        instrucciones="""
        - Resumen Ejecutivo: Destaca el uptime y la proactividad.
        - Gestión de Incidencias: Muestra cómo TecnoMonitor cuidó al cliente (Valor Agregado).
        - Recomendaciones: Sugerencias de negocio (ej: ampliar disco para futuro).
        """,
    ),
    "interno": PerfilReporte(
        rol="INGENIERO SRE SENIOR",
        tono="Crudo, directo, técnico. Enfocado en logs, errores y hardware.",
        instrucciones="""
        - Resumen Ejecutivo: Estado real del cluster.
        - Gestión de Incidencias: Lista de errores exactos.
        - Recomendaciones: Acciones inmediatas (ej: reiniciar servicio X, cambiar disco Y).
        """,
    ),
}


class TipoReporteInvalido(ValueError):
    pass


def _extraer_uptime(resumen: dict[str, Any]) -> str:
    infra = resumen.get("infraestructura") or {}
    uptime = infra.get("uptime_pct")
    return f"{uptime}%" if uptime is not None else "s/d"


def _contar_incidentes_internos(resumen: dict[str, Any]) -> int:
    infra = resumen.get("infraestructura") or {}
    return len(infra.get("eventos") or [])


def _construir_prompt(
    resumen: dict[str, Any],
    asana_casos: dict[str, list[str]],
    hospital_nombre: str,
    tipo_reporte: str,
) -> str:
    perfil = PERFILES.get(tipo_reporte)
    if perfil is None:
        raise TipoReporteInvalido(
            f"Tipo de reporte '{tipo_reporte}' no reconocido. Opciones: {list(PERFILES)}"
        )

    uptime = _extraer_uptime(resumen)
    incidentes_internos = _contar_incidentes_internos(resumen)
    resumen_json = json.dumps(resumen, ensure_ascii=False, default=str)

    humanos = asana_casos.get("humanos", [])
    automaticos = asana_casos.get("automaticos", [])
    n_humanos = len(humanos)

    bloque_humanos = "\n\n".join(humanos) if humanos else "(sin casos de gestión en el período)"
    bloque_automaticos = "\n".join(automaticos) if automaticos else "(ninguno)"

    return f"""
    ACTÚA COMO UN {perfil.rol} DE "TECNOMONITOR".
    Genera el JSON para el reporte {tipo_reporte} de {hospital_nombre}.
    TONO: {perfil.tono}

    A continuación tenés el RESUMEN PRE-PROCESADO del período (infraestructura,
    actividad clínica RIS/PACS y estado de software). Ya viene agregado y
    filtrado — no es la telemetría cruda. Usalo como única fuente de datos:

    {resumen_json}

    ── CASOS DE GESTIÓN (Asana, escritos por personas) ──
    Son pedidos, seguimientos y gestiones reales del cliente/equipo. ESTE es
    el contexto de negocio relevante. Hay {n_humanos} en el período:

    {bloque_humanos}

    ── INCIDENTES AUTOGESTIONADOS (Asana, generados por TecnoMonitor) ──
    Tickets que el propio sistema abrió y cerró automáticamente. YA están
    reflejados en el resumen de infraestructura de arriba — NO los cuentes
    como reclamos del cliente ni los dupliques en el análisis. Se listan
    sólo como referencia de que el monitoreo actuó:

    {bloque_automaticos}

    INSTRUCCIONES ESPECÍFICAS:
    {perfil.instrucciones}

    ESTRUCTURA JSON OBLIGATORIA (respetar exactamente estas claves):
    {{
        "meta": {{ "tipo": "{tipo_reporte}", "fecha": "{datetime.now().strftime('%d/%m/%Y')}", "hospital": "{hospital_nombre}" }},
        "resumen": {{ "uptime": "{uptime}", "texto": "..." }},
        "infraestructura": {{ "energia": "...", "termica": "...", "mensaje": "..." }},
        "incidencias": {{ "externas": {n_humanos}, "internas": {incidentes_internos}, "analisis": "..." }},
        "calidad": {{ "estabilidad": "...", "caso_destacado": "..." }},
        "recomendacion": "..."
    }}
    """


def generar_json_con_ia(
    resumen: dict[str, Any],
    asana_casos: dict[str, list[str]],
    hospital_nombre: str,
    pdfs: list[Any],
    tipo_reporte: str,
    model_name: str = "gemini-flash-latest",
) -> dict[str, Any] | None:
    log.info("Generando JSON del informe (%s) con modelo %s", tipo_reporte, model_name)

    prompt = _construir_prompt(resumen, asana_casos, hospital_nombre, tipo_reporte)
    model = genai.GenerativeModel(
        model_name, generation_config={"response_mime_type": "application/json"}
    )

    contenido: list[Any] = [prompt]
    if pdfs:
        contenido.extend(pdfs)

    try:
        res = model.generate_content(contenido)
        return json.loads(res.text)
    except json.JSONDecodeError:
        log.error("La IA no devolvió JSON válido. Respuesta cruda: %.500s", res.text)
        return None
    except Exception:
        log.exception("Error llamando a la API de Gemini")
        return None
