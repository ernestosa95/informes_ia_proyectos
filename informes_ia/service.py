"""
Modo sincrónico: genera un informe completo en el momento y lo deja en disco
(JSON + PNG + PDF). Para la CLI (main.py) y para probar.

Usa exactamente las mismas piezas que el modo asíncrono (pipeline_real.py),
así hay un solo camino de generación. La diferencia es que acá no hay
borrador ni aprobación: el PDF sale directo. Para el dashboard usar el modo
asíncrono (historial/servicio.py + worker.py), que guarda el borrador para
revisión humana antes de producir el PDF.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from . import render
from .config import Settings, get_settings
from .historial.servicio import FalloRed
from .historial.validacion import RespuestaInvalida, validar_estructura
from .logging_utils import get_logger
from .pipeline_real import HospitalInexistente, PipelineReal, SinDatos

log = get_logger(__name__)

TipoReporte = Literal["cliente", "interno"]

# Intentos de la llamada a la IA en modo sincrónico (en el asíncrono manda historial/estados.py).
INTENTOS_IA = 2


class HospitalNoEncontrado(RuntimeError):
    pass


class DatosInsuficientes(RuntimeError):
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
    pipeline: PipelineReal | None = None,
) -> ResultadoReporte:
    """
    Genera un reporte completo (gráfico + JSON + PDF) para un hospital y rango de fechas.

    fecha_inicio / fecha_fin: strings "YYYY-MM-DD HH:MM:SS" (hora local, como la base).
    tipo_reporte: "cliente" (estratégico) o "interno" (técnico/auditoría).
    settings / pipeline: opcionales, para inyectar configuración o dobles en tests.

    Lanza HospitalNoEncontrado, DatosInsuficientes, GeneracionIAFallida.
    """
    settings = settings or get_settings()
    pipeline = pipeline or PipelineReal(settings)
    peticion = {"hospital_id": hospital_id, "fecha_inicio": fecha_inicio,
                "fecha_fin": fecha_fin, "tipo_reporte": tipo_reporte}

    log.info("--- Generando reporte %s para %s (%s -> %s) ---", tipo_reporte, hospital_id, fecha_inicio, fecha_fin)

    try:
        contexto = pipeline.preparar_contexto(peticion)
    except HospitalInexistente as e:
        raise HospitalNoEncontrado(str(e)) from e
    except SinDatos as e:
        raise DatosInsuficientes(str(e)) from e

    data = None
    for intento in range(1, INTENTOS_IA + 1):
        try:
            data = pipeline.generador_ia(contexto)
            validar_estructura(data, set())
            break
        except (FalloRed, RespuestaInvalida) as e:
            log.warning("Intento %d/%d de la IA falló: %s", intento, INTENTOS_IA, e)
            if intento == INTENTOS_IA:
                raise GeneracionIAFallida(str(e)) from e
            time.sleep(getattr(e, "retry_delay_s", None) or 5)

    ruta_base = settings.carpeta_reportes / hospital_id
    ruta_base.mkdir(parents=True, exist_ok=True)

    ruta_json = ruta_base / f"data_{tipo_reporte}.json"
    render.guardar_json(data, ruta_json)

    ruta_grafico = None
    png = pipeline.generar_grafico_png(peticion)
    if png:
        ruta_grafico = ruta_base / f"grafico_{tipo_reporte}.png"
        ruta_grafico.write_bytes(png)

    ruta_pdf = ruta_base / f"Reporte_{tipo_reporte}_{datetime.now().strftime('%Y%m%d')}.pdf"
    render.renderizar_pdf_desde_json(data, ruta_grafico, ruta_pdf)

    log.info("Proceso completado: %s", ruta_pdf)
    return ResultadoReporte(
        hospital_id=hospital_id,
        tipo_reporte=tipo_reporte,
        ruta_json=ruta_json,
        ruta_pdf=ruta_pdf,
        ruta_grafico=ruta_grafico,
        data=data,
    )


def re_renderizar_pdf(ruta_json: Path, ruta_grafico: Path | None, ruta_pdf: Path) -> Path:
    """
    Re-genera sólo el PDF a partir de un JSON ya existente, sin volver a
    llamar a la IA. Útil para iterar sobre el diseño de la plantilla.
    """
    data = render.cargar_json(ruta_json)
    return render.renderizar_pdf_desde_json(data, ruta_grafico, ruta_pdf)
