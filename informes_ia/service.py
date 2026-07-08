"""
Servicio principal: orquesta los 4 módulos para producir un reporte completo.

Este es el punto de integración pensado para usarse desde otro backend
(ej. un endpoint FastAPI/Flask detrás del botón "Iniciar Análisis" del
dashboard), sin depender de constantes globales ni de ejecución por CLI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import google.generativeai as genai

from . import ai_report, asana_client, db, docs_context, preprocess, render
from .config import Settings, get_settings
from .config_dinamica import get_configuracion
from .logging_utils import get_logger

log = get_logger(__name__)

TipoReporte = Literal["cliente", "interno"]


class HospitalNoEncontrado(RuntimeError):
    pass


class DatosInsuficientes(preprocess.SinDatosParaElPeriodo):
    pass


class GeneracionIAFallida(RuntimeError):
    pass


@dataclass(frozen=True)
class ResultadoReporte:
    hospital_id: str
    tipo_reporte: str
    ruta_json: Path
    ruta_pdf: Path
    ruta_grafico: Path | None
    data: dict[str, Any]


def generar_reporte(
    hospital_id: str,
    fecha_inicio: str,
    fecha_fin: str,
    tipo_reporte: TipoReporte = "cliente",
    settings: Settings | None = None,
) -> ResultadoReporte:
    """
    Genera un reporte completo (gráfico + JSON de IA + PDF) para un hospital
    y rango de fechas dados.

    Parámetros
    ----------
    hospital_id: ID interno del hospital (ej. "H09").
    fecha_inicio / fecha_fin: strings "YYYY-MM-DD HH:MM:SS".
    tipo_reporte: "cliente" (estratégico) o "interno" (técnico/auditoría).
    settings: opcional, para inyectar configuración custom (tests, multi-tenant).

    Lanza
    -----
    HospitalNoEncontrado, DatosInsuficientes, GeneracionIAFallida
    """
    settings = settings or get_settings()
    genai.configure(api_key=settings.gemini_api_key)

    log.info("--- Generando reporte %s para %s (%s -> %s) ---", tipo_reporte, hospital_id, fecha_inicio, fecha_fin)

    config_hospital = db.obtener_config_hospital(settings.db_path, hospital_id)
    if not config_hospital:
        raise HospitalNoEncontrado(f"No hay configuración para el hospital '{hospital_id}'")

    ruta_base = settings.carpeta_reportes / hospital_id
    ruta_base.mkdir(parents=True, exist_ok=True)

    ruta_img = ruta_base / f"grafico_{tipo_reporte}.png"
    ruta_grafico = db.generar_grafico_historico(
        settings.db_path, hospital_id, fecha_inicio, fecha_fin, ruta_img
    )

    config_dinamica = get_configuracion(settings.db_path)
    try:
        resumen = preprocess.generar_resumen(
            settings.db_path, hospital_id, fecha_inicio, fecha_fin, config=config_dinamica
        )
    except preprocess.SinDatosParaElPeriodo as e:
        raise DatosInsuficientes(str(e)) from e

    asana_txt = asana_client.obtener_tareas_asana(
        settings.asana_access_token, config_hospital["asana_id"], fecha_inicio
    )

    pdfs = docs_context.cargar_contexto_persistente(settings.carpeta_docs, settings.cache_file)

    json_data = ai_report.generar_json_con_ia(
        resumen,
        asana_txt,
        config_hospital["nombre"],
        pdfs,
        tipo_reporte,
        model_name=settings.gemini_model_json,
    )
    if not json_data:
        raise GeneracionIAFallida("La IA no devolvió un JSON válido para este reporte")

    ruta_json = ruta_base / f"data_{tipo_reporte}.json"
    render.guardar_json(json_data, ruta_json)

    ruta_pdf = ruta_base / f"Reporte_{tipo_reporte}_{datetime.now().strftime('%Y%m%d')}.pdf"
    render.renderizar_pdf_desde_json(json_data, ruta_grafico, ruta_pdf)

    log.info("Proceso completado: %s", ruta_pdf)

    return ResultadoReporte(
        hospital_id=hospital_id,
        tipo_reporte=tipo_reporte,
        ruta_json=ruta_json,
        ruta_pdf=ruta_pdf,
        ruta_grafico=ruta_grafico,
        data=json_data,
    )


def re_renderizar_pdf(ruta_json: Path, ruta_grafico: Path | None, ruta_pdf: Path) -> Path:
    """
    Re-genera sólo el PDF a partir de un JSON ya existente, sin volver a
    llamar a la IA. Útil para iterar sobre el diseño de la plantilla.
    """
    data = render.cargar_json(ruta_json)
    return render.renderizar_pdf_desde_json(data, ruta_grafico, ruta_pdf)
