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

from . import audiencia
from .logging_utils import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class PerfilReporte:
    rol: str
    tono: str
    instrucciones: str


PERFILES: dict[str, PerfilReporte] = {
    "cliente": PerfilReporte(
        rol="RESPONSABLE DE CUENTA (CUSTOMER SUCCESS)",
        tono=(
            "Claro, cordial y profesional, para la dirección de un hospital. Sin jerga técnica. "
            "Pone en valor el servicio con datos concretos (disponibilidad, actividad, incidentes "
            "gestionados) y presenta los riesgos del hospital como recomendaciones."
        ),
        instrucciones="""
        - resumen.texto: disponibilidad del período y los dos o tres hechos más relevantes, con cifras.
        - infraestructura.energia / termica / mensaje: estado del equipamiento físico y del entorno,
          en lenguaje de gestión. Si esa área figura en datos_no_disponibles, devolvé el campo
          vacío ("") y no la menciones en ningún otro texto.
        - incidencias.analisis: cuántos incidentes detectó y gestionó el servicio y en qué áreas;
          si hubo casos de gestión del hospital, cuáles se atendieron.
        - calidad.estabilidad: tendencia frente al período anterior si la hay; si no, estabilidad del período.
        - calidad.caso_destacado: un hecho concreto que muestre el valor del servicio.
        - recomendacion: las recomendaciones_sugeridas, priorizadas y explicadas para la dirección.
        """,
    ),
    "interno": PerfilReporte(
        rol="INGENIERO SRE SENIOR DEL EQUIPO DE TECNOMONITOR",
        tono="Técnico, directo y preciso. Para el equipo propio: incluye lo que hay que mejorar.",
        instrucciones="""
        - resumen.texto: estado real del hospital en el período, con cifras.
        - infraestructura.energia / termica / mensaje: hallazgos de hardware, temperatura, red y
          capacidad con identificadores y duraciones. Si no hay datos de energía o de temperatura
          (reportes_con_dato en 0), escribí sólo que faltan esos datos.
        - incidencias.analisis: eventos por tipo (cantidad, minutos, abiertos al cierre), software
          (Mirth, backups, CHECKDB, DICOM, portal) y casos de Asana relevantes.
        - calidad.estabilidad: comparación con el período anterior y patrones (recurrencias, horarios).
        - calidad.caso_destacado: el incidente o hallazgo más importante, con su detalle.
        - puntos_a_revisar: una línea por punto, de mayor a menor prioridad, con el formato
          "[ALTA|MEDIA|BAJA] tema — detalle con datos → acción concreta". Incluí también lo que
          dependa de nosotros (monitoreo, agente, configuración, datos faltantes del control de calidad).
        - recomendacion: las acciones inmediatas más importantes.
        """,
    ),
}

# Reglas que valen para cualquier perfil.
REGLAS_DE_DATOS = """
REGLAS SOBRE LOS DATOS (obligatorias, por encima del tono):
- Usá sólo lo que está en los DATOS DEL PERÍODO. No supongas causas, proyectos,
  migraciones, personas ni hechos que no figuren ahí.
- Podés citar cifras que estén en los datos, tal cual (redondeadas a lo sumo a un
  decimal). No inventes ni calcules cifras nuevas: totales, cifras clave y tabla los
  completa el sistema.
- Si un área no tiene datos (null, vacía o listada como no disponible), no afirmes nada
  sobre ella: en un informe interno decí que faltan datos; en uno para el cliente, omitila.
- Si un valor parece contradictorio, no lo expliques ni lo destaques.
- Tendencias: sólo con el bloque "comparacion"; sin él, no hables de mejora o empeoramiento.
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


def _con_puntos_a_revisar(esquema: dict[str, Any]) -> dict[str, Any]:
    """El mismo esquema más el campo `puntos_a_revisar` (sólo informes internos)."""
    nuevo = {**esquema, "properties": {**esquema["properties"], "puntos_a_revisar": {"type": "string"}}}
    nuevo["required"] = [*esquema["required"], "puntos_a_revisar"]
    return nuevo


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
    hospital_id: str | None = None,
) -> str:
    perfil = PERFILES.get(tipo_reporte)
    if perfil is None:
        raise TipoReporteInvalido(
            f"Tipo de reporte '{tipo_reporte}' no reconocido. Opciones: {list(PERFILES)}"
        )
    vista = audiencia.vista_individual(resumen, asana_casos, tipo_reporte,
                                       {"id": hospital_id, "nombre": hospital_nombre})
    return f"""
    ACTÚA COMO {perfil.rol} DE "TECNOMONITOR", el servicio de monitoreo de la
    infraestructura tecnológica y de los sistemas clínicos del hospital.
    Redactá los textos del informe {tipo_reporte.upper()} de {hospital_nombre}.
    TONO: {perfil.tono}
    {REGLAS_DE_DATOS}
    {audiencia.VOCABULARIO[tipo_reporte]}
    {audiencia.diccionario(tipo_reporte)}
    DATOS DEL PERÍODO (ya calculados por el sistema; son tu única fuente):

    {json.dumps(vista, ensure_ascii=False, default=str)}

    QUÉ ESCRIBIR EN CADA CAMPO:
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
    hospital_id: str | None = None,
) -> dict[str, Any]:
    """
    Devuelve el JSON de textos de la IA (sin cifras: ver completar_con_cifras).
    Lanza ErrorAPIGemini o RespuestaNoJSON; nunca devuelve None.
    """
    log.info("Generando JSON del informe (%s) con modelo %s", tipo_reporte, model_name)

    prompt = _construir_prompt(resumen, asana_casos, hospital_nombre, tipo_reporte, hospital_id)
    esquema = _con_puntos_a_revisar(ESQUEMA_RESPUESTA) if tipo_reporte == audiencia.INTERNO else ESQUEMA_RESPUESTA
    log.info("Prompt de %d caracteres (%s)", len(prompt), tipo_reporte)
    return _llamar_gemini(client, [prompt, *pdfs], esquema, model_name)


def _llamar_gemini(
    client: genai.Client, contenido: list[Any], esquema: dict[str, Any], model_name: str
) -> dict[str, Any]:
    """Llamada común a Gemini con JSON Schema forzado. Lanza ErrorAPIGemini o RespuestaNoJSON."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=esquema,
        temperature=0.2,   # bajo: el informe tiene que ser fiel a los datos, no creativo
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

    data.setdefault("meta", {})["alcance"] = "hospital"
    periodo = resumen.get("periodo") or {}
    if periodo.get("desde") and periodo.get("hasta"):
        data["meta"]["periodo"] = f"{str(periodo['desde'])[:10]} al {str(periodo['hasta'])[:10]}"
    data.setdefault("resumen", {})["uptime"] = _fmt_pct(infra.get("uptime_pct"))
    incid = data.setdefault("incidencias", {})
    incid["externas"] = len(asana_casos.get("humanos", []))
    incid["internas"] = len(eventos)

    metricas = infra.get("metricas") or {}
    tipo = (data.get("meta") or {}).get("tipo", audiencia.INTERNO)

    # Áreas sin un solo dato en el período (ej. sin iDRAC): el texto lo pone el código, no la IA.
    # El esquema obliga a la IA a escribir estos campos y, sin datos, tendería a un "sin novedades".
    con_dato = infra.get("reportes_con_dato") or {}
    texto_infra = data.setdefault("infraestructura", {})
    for campo, clave, nombre in (("energia", "energia", "energía (fuentes)"), ("termica", "temperatura", "temperatura")):
        if infra and not con_dato.get(clave):
            texto_infra[campo] = ("" if tipo == audiencia.CLIENTE else
                                  f"Sin datos de {nombre} en el período: iDRAC no configurado o sin respuesta.")

    data["cifras"] = {
        "uptime_pct": infra.get("uptime_pct"),
        "uptime_definicion": (infra.get("uptime_definicion") if tipo == audiencia.INTERNO
                              else "porcentaje del período con los sistemas monitoreados en línea"),
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
    data["comparacion"] = resumen.get("comparacion")
    data["control_calidad"] = (resumen.get("control_calidad") or []) + audiencia.revisar_vocabulario(data, tipo)
    return data


def _contar_por_tipo(eventos: list[dict[str, Any]]) -> dict[str, int]:
    conteo: dict[str, int] = {}
    for e in eventos:
        conteo[e.get("tipo", "?")] = conteo.get(e.get("tipo", "?"), 0) + 1
    return conteo


# ── informes de GRUPO (grupo.py) ────────────────────────────────────────────

ESQUEMA_RESPUESTA_GRUPO: dict[str, Any] = {
    "type": "object",
    "properties": {
        "meta": ESQUEMA_RESPUESTA["properties"]["meta"],
        "resumen": ESQUEMA_RESPUESTA["properties"]["resumen"],
        "situacion_general": {"type": "string"},
        "hospitales_atencion": {"type": "string"},
        "recomendacion": {"type": "string"},
    },
    "required": ["meta", "resumen", "situacion_general", "hospitales_atencion", "recomendacion"],
}


def _construir_prompt_grupo(resumen: dict[str, Any], tipo_reporte: str) -> str:
    perfil = PERFILES.get(tipo_reporte)
    if perfil is None:
        raise TipoReporteInvalido(
            f"Tipo de reporte '{tipo_reporte}' no reconocido. Opciones: {list(PERFILES)}"
        )
    nombre = (resumen.get("grupo") or {}).get("nombre") or "el grupo"
    vista = audiencia.vista_grupo(resumen, tipo_reporte)
    if tipo_reporte == audiencia.CLIENTE:
        campos = """
    - resumen.texto: estado del conjunto en el período, en dos o tres oraciones con cifras.
    - situacion_general: disponibilidad, incidentes gestionados y actividad clínica del grupo, con la
      tendencia frente al período anterior si la hay. Destacá los hospitales con mejor desempeño.
    - hospitales_atencion: RECOMENDACIONES POR HOSPITAL, sólo para los que tienen
      recomendaciones_sugeridas, en tono constructivo. Si ninguno tiene, decí que no hay
      recomendaciones particulares.
    - recomendacion: recomendaciones para el conjunto, priorizadas."""
    else:
        campos = """
    - resumen.texto: estado real del grupo en el período, con cifras.
    - situacion_general: disponibilidad, incidentes (críticos y abiertos al cierre), software y actividad,
      con patrones comunes (varios hospitales con el mismo problema) y comparación con el período anterior.
    - hospitales_atencion: los de "hospitales_que_requieren_atencion", del más al menos urgente, con sus
      motivos y datos.
    - puntos_a_revisar: una línea por punto, "[ALTA|MEDIA|BAJA] hospital/tema — detalle → acción",
      incluyendo lo que dependa de nosotros y los datos faltantes del control de calidad.
    - recomendacion: acciones inmediatas para el grupo."""
    return f"""
    ACTÚA COMO {perfil.rol} DE "TECNOMONITOR".
    Redactá los textos del informe {tipo_reporte.upper()} CONSOLIDADO del grupo de hospitales "{nombre}".
    TONO: {perfil.tono}
    {REGLAS_DE_DATOS}
    {audiencia.VOCABULARIO[tipo_reporte]}
    {audiencia.diccionario(tipo_reporte)}
    El informe se publica con una TABLA de una fila por hospital (la arma el sistema con los
    mismos datos). No la repitas: los textos dan la lectura de conjunto y lo que importa.

    DATOS DEL GRUPO:

    {json.dumps(vista, ensure_ascii=False, default=str)}

    QUÉ ESCRIBIR:{campos}

    En "meta" poné tipo "{tipo_reporte}", fecha "{datetime.now().strftime('%d/%m/%Y')}" y
    hospital "{nombre}".
    """


def generar_json_grupo(
    client: genai.Client, resumen: dict[str, Any], tipo_reporte: str, model_name: str
) -> dict[str, Any]:
    """Textos del informe de grupo. Lanza ErrorAPIGemini o RespuestaNoJSON."""
    log.info("Generando JSON del informe de grupo (%s) con modelo %s", tipo_reporte, model_name)
    prompt = _construir_prompt_grupo(resumen, tipo_reporte)
    esquema = (_con_puntos_a_revisar(ESQUEMA_RESPUESTA_GRUPO) if tipo_reporte == audiencia.INTERNO
               else ESQUEMA_RESPUESTA_GRUPO)
    log.info("Prompt de grupo de %d caracteres (%s)", len(prompt), tipo_reporte)
    return _llamar_gemini(client, [prompt], esquema, model_name)


def completar_con_cifras_grupo(data: dict[str, Any], resumen: dict[str, Any]) -> dict[str, Any]:
    """Cifras, tabla por hospital y control de calidad del grupo (las pone el código)."""
    ag = resumen.get("agregados") or {}
    meta = data.setdefault("meta", {})
    meta["alcance"] = "grupo"
    meta["grupo"] = resumen.get("grupo")
    periodo = resumen.get("periodo") or {}
    if periodo.get("desde") and periodo.get("hasta"):
        meta["periodo"] = f"{str(periodo['desde'])[:10]} al {str(periodo['hasta'])[:10]}"
    data.setdefault("resumen", {})["uptime"] = _fmt_pct(ag.get("uptime_promedio_pct"))
    data["cifras"] = dict(ag)
    data["tabla_hospitales"] = [
        {
            "hospital_id": f.get("hospital_id"),
            "nombre": f.get("nombre"),
            "uptime_pct": f.get("uptime_pct"),
            "incidentes": f.get("incidentes"),
            "incidentes_criticos": f.get("incidentes_criticos"),
            "bases_sin_backup": f.get("bases_sin_backup"),
            "mirth_detenidos": f.get("mirth_detenidos"),
            "estudios_totales": f.get("estudios_totales"),
            "motivos_atencion": f.get("motivos_atencion") or [],
            "recomendaciones_cliente": f.get("recomendaciones_cliente") or [],
        }
        for f in resumen.get("hospitales") or []
    ]
    data["comparacion"] = resumen.get("comparacion")
    tipo = meta.get("tipo", audiencia.INTERNO)
    data["control_calidad"] = (resumen.get("control_calidad") or []) + audiencia.revisar_vocabulario(data, tipo)
    return data
