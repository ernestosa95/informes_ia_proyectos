"""
Módulo 3: Generación del JSON del informe usando IA (Gemini, SDK `google-genai`).

Este módulo separa deliberadamente "qué se le pide a la IA" (los perfiles
de tono/rol) de "cómo se llama a la IA", para que agregar un nuevo tipo de
reporte (ej. "directorio", "regulatorio") sea sumar una entrada al dict
PERFILES sin tocar el resto del pipeline.

LAS CIFRAS NO LAS ESCRIBE LA IA. La IA sólo redacta textos; uptime, cantidad
de incidencias y métricas salen del resumen pre-procesado y se escriben en el
JSON con `completar_con_cifras()` después de la respuesta. Antes la IA copiaba
el uptime y, cuando el dato estaba mal (0 %), inventaba una causa para el
cliente (informe de P03, 2026-07). El prompt además le prohíbe explicar algo
que no esté en los datos.

Errores: la llamada no se traga excepciones. `ErrorAPIGemini` (red, cuota,
API caída; con el `retry_delay_s` que pida un 429) y `RespuestaNoJSON` los
traduce quien llama a su política de reintentos.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

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
        tono=(
            "Claro, cordial y profesional, para una dirección de hospital. NO técnico. "
            "Positivo cuando los datos lo justifican; si hubo problemas, se dicen con "
            "claridad junto con lo que se hizo o se recomienda."
        ),
        instrucciones="""
        - Resumen Ejecutivo: disponibilidad del período y los hechos más relevantes.
        - Gestión de Incidencias: qué detectó TecnoMonitor y cómo se gestionó.
        - Recomendaciones: sugerencias concretas que se desprendan de los datos (ej: ampliar un disco que está cerca del límite).
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

# Reglas que valen para cualquier perfil.
REGLAS_DE_DATOS = """
REGLAS SOBRE LOS DATOS (obligatorias, por encima del tono):
- Usá sólo lo que está en el resumen y en los casos de Asana. No supongas causas,
  proyectos, migraciones, personas ni hechos que no figuren ahí.
- No escribas cifras (uptime, cantidades, porcentajes) que no estén en el resumen;
  las cifras del informe las completa el sistema.
- Si una sección del resumen viene en null o figura en "control_calidad" como sin
  datos, decí que no hay datos para ese punto. No la des por buena.
- Si un valor parece incorrecto o contradictorio, no lo expliques: limitate a no
  destacarlo.
"""

# Estructura que la IA debe devolver (JSON Schema, forzado por la API).
ESQUEMA_RESPUESTA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "meta": {
            "type": "object",
            "properties": {"tipo": {"type": "string"}, "fecha": {"type": "string"}, "hospital": {"type": "string"}},
            "required": ["tipo", "fecha", "hospital"],
        },
        "resumen": {
            "type": "object",
            "properties": {"texto": {"type": "string"}},
            "required": ["texto"],
        },
        "infraestructura": {
            "type": "object",
            "properties": {
                "energia": {"type": "string"},
                "termica": {"type": "string"},
                "mensaje": {"type": "string"},
            },
            "required": ["energia", "termica", "mensaje"],
        },
        "incidencias": {
            "type": "object",
            "properties": {"analisis": {"type": "string"}},
            "required": ["analisis"],
        },
        "calidad": {
            "type": "object",
            "properties": {"estabilidad": {"type": "string"}, "caso_destacado": {"type": "string"}},
            "required": ["estabilidad", "caso_destacado"],
        },
        "recomendacion": {"type": "string"},
    },
    "required": ["meta", "resumen", "infraestructura", "incidencias", "calidad", "recomendacion"],
}


class TipoReporteInvalido(ValueError):
    pass


class ErrorAPIGemini(Exception):
    """No llegó respuesta usable: red, cuota (429), API caída. Reintentable."""

    def __init__(self, mensaje: str, retry_delay_s: float | None = None, codigo: int | None = None):
        super().__init__(mensaje)
        self.retry_delay_s = retry_delay_s
        self.codigo = codigo


class RespuestaNoJSON(Exception):
    """La API respondió pero el texto no es un objeto JSON."""


def extraer_retry_delay(error: Exception) -> float | None:
    """
    Segundos que pide esperar un 429 de Gemini: el `retryDelay` de RetryInfo
    en los detalles del error, o el "retry in Xs" del mensaje.
    """
    detalles = getattr(error, "details", None)
    if isinstance(detalles, dict):
        for d in (detalles.get("error") or {}).get("details") or []:
            if isinstance(d, dict) and str(d.get("@type", "")).endswith("RetryInfo"):
                m = re.match(r"(\d+(?:\.\d+)?)s", str(d.get("retryDelay", "")))
                if m:
                    return float(m.group(1))
    m = re.search(r"retry in (\d+(?:\.\d+)?)s", str(error))
    return float(m.group(1)) if m else None


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

    resumen_json = json.dumps(resumen, ensure_ascii=False, default=str)

    humanos = asana_casos.get("humanos", [])
    automaticos = asana_casos.get("automaticos", [])

    bloque_humanos = "\n\n".join(humanos) if humanos else "(sin casos de gestión en el período)"
    bloque_automaticos = "\n".join(automaticos) if automaticos else "(ninguno)"

    return f"""
    ACTÚA COMO UN {perfil.rol} DE "TECNOMONITOR".
    Redactá los textos del reporte {tipo_reporte} de {hospital_nombre}.
    TONO: {perfil.tono}
    {REGLAS_DE_DATOS}
    A continuación tenés el RESUMEN PRE-PROCESADO del período (infraestructura,
    actividad clínica RIS/PACS y estado de software). Ya viene agregado y
    filtrado — no es la telemetría cruda. Usalo como única fuente de datos:

    {resumen_json}

    ── CASOS DE GESTIÓN (Asana, escritos por personas) ──
    Son pedidos, seguimientos y gestiones reales del cliente/equipo. ESTE es
    el contexto de negocio relevante. Hay {len(humanos)} en el período:

    {bloque_humanos}

    ── INCIDENTES AUTOGESTIONADOS (Asana, generados por TecnoMonitor) ──
    Tickets que el propio sistema abrió y cerró automáticamente. YA están
    reflejados en el resumen de infraestructura de arriba — NO los cuentes
    como reclamos del cliente ni los dupliques en el análisis. Se listan
    sólo como referencia de que el monitoreo actuó:

    {bloque_automaticos}

    INSTRUCCIONES ESPECÍFICAS:
    {perfil.instrucciones}

    En "meta" poné tipo "{tipo_reporte}", fecha "{datetime.now().strftime('%d/%m/%Y')}" y
    hospital "{hospital_nombre}". Cada texto, en uno o dos párrafos breves.
    """


def generar_json_con_ia(
    client: genai.Client,
    resumen: dict[str, Any],
    asana_casos: dict[str, list[str]],
    hospital_nombre: str,
    pdfs: list[Any],
    tipo_reporte: str,
    model_name: str,
) -> dict[str, Any]:
    """
    Devuelve el JSON de textos de la IA (sin cifras: ver completar_con_cifras).
    Lanza ErrorAPIGemini o RespuestaNoJSON; nunca devuelve None.
    """
    log.info("Generando JSON del informe (%s) con modelo %s", tipo_reporte, model_name)

    prompt = _construir_prompt(resumen, asana_casos, hospital_nombre, tipo_reporte)
    contenido: list[Any] = [prompt, *pdfs]
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=ESQUEMA_RESPUESTA,
        temperature=0.4,
    )

    try:
        res = client.models.generate_content(model=model_name, contents=contenido, config=config)
    except genai_errors.APIError as e:
        raise ErrorAPIGemini(
            f"Gemini respondió {e.code} {e.status}: {e.message}",
            retry_delay_s=extraer_retry_delay(e), codigo=e.code,
        ) from e
    except Exception as e:  # red, timeout, DNS: también reintentable
        raise ErrorAPIGemini(f"Error de comunicación con Gemini: {e}") from e

    texto = res.text or ""
    try:
        data = json.loads(texto)
    except json.JSONDecodeError as e:
        raise RespuestaNoJSON(f"La IA no devolvió JSON válido: {texto[:300]!r}") from e
    if not isinstance(data, dict):
        raise RespuestaNoJSON(f"La IA devolvió {type(data).__name__}, no un objeto JSON")

    data.setdefault("meta", {})["modelo"] = getattr(res, "model_version", None) or model_name
    return data


# ── cifras y control de calidad (las pone el código, no la IA) ──────────────

def _fmt_pct(valor: Any) -> str:
    return f"{valor:.2f}%".replace(".", ",") if isinstance(valor, (int, float)) else "s/d"


def completar_con_cifras(
    data: dict[str, Any], resumen: dict[str, Any], asana_casos: dict[str, list[str]]
) -> dict[str, Any]:
    """
    Escribe en el JSON de la IA las cifras calculadas por el pre-procesamiento
    (pisa cualquier número que la IA haya puesto) y el control de calidad de
    datos, para que quien revisa el borrador vea qué faltó.
    """
    infra = resumen.get("infraestructura") or {}
    eventos = infra.get("eventos") or []
    software = resumen.get("software") or {}

    periodo = resumen.get("periodo") or {}
    if periodo.get("desde") and periodo.get("hasta"):
        data.setdefault("meta", {})["periodo"] = f"{str(periodo['desde'])[:10]} al {str(periodo['hasta'])[:10]}"
    data.setdefault("resumen", {})["uptime"] = _fmt_pct(infra.get("uptime_pct"))
    incid = data.setdefault("incidencias", {})
    incid["externas"] = len(asana_casos.get("humanos", []))
    incid["internas"] = len(eventos)

    metricas = infra.get("metricas") or {}
    data["cifras"] = {
        "uptime_pct": infra.get("uptime_pct"),
        "uptime_definicion": infra.get("uptime_definicion"),
        "huecos_sin_reportes": (infra.get("huecos_sin_reportes") or {}).get("cantidad"),
        "cpu_host_promedio_pct": (metricas.get("cpu_host") or {}).get("avg"),
        "ram_host_promedio_pct": (metricas.get("ram_host") or {}).get("avg"),
        "temp_cpu_maxima_c": (metricas.get("temp_cpu") or {}).get("max"),
        "eventos_por_tipo": _contar_por_tipo(eventos),
        "disponibilidad_vms_pct": infra.get("disponibilidad_vms_pct") or {},
        "canales_mirth_con_problema": len((software.get("canales_mirth") or {}).get("con_problema") or []),
        "bases_sin_backup_reciente": len((software.get("backups_sql") or {}).get("con_alerta") or []),
        "certificados_ssl_con_alerta": len((software.get("certificados_ssl") or {}).get("con_alerta") or []),
    }
    data["control_calidad"] = resumen.get("control_calidad") or []
    return data


def _contar_por_tipo(eventos: list[dict[str, Any]]) -> dict[str, int]:
    conteo: dict[str, int] = {}
    for e in eventos:
        conteo[e.get("tipo", "?")] = conteo.get(e.get("tipo", "?"), 0) + 1
    return conteo
