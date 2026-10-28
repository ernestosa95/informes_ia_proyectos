"""
Consolidador de pre-procesamiento.

Esta es la pieza que reemplaza "pasarle todo el JSON crudo a la IA": en vez
de miles de snapshots por hospital (uno cada 5 min) mas las filas de uso y
software del mismo periodo, generar_resumen() devuelve un unico dict de
1-2 KB con estadisticas + eventos ya agrupados, que es lo unico que
ai_report.py termina mandando al modelo.

    reportes_historicos ---> EventTracker      (infra: CPU/RAM/temp/power/RAID/discos)
    reportes_uso        ---> BacklogAnalyzer   (KPIs clinicos RIS/PACS)
    software_monitoring ---> SoftwareAuditor   (SSL / logs Elasticsearch / Mirth)
                              |
                              v
                       generar_resumen() -> dict compacto -> ai_report.generar_json_con_ia()
"""
from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from . import backlog_analyzer, db, event_tracker, software_auditor
from .config_dinamica import ConfiguracionGlobal, get_configuracion
from .logging_utils import get_logger

log = get_logger(__name__)


class SinDatosParaElPeriodo(RuntimeError):
    pass


def generar_resumen(
    fuente: str | Path,
    hospital_id: str,
    fecha_inicio: str,
    fecha_fin: str,
    config: ConfiguracionGlobal | None = None,
) -> dict[str, Any]:
    """
    Punto de entrada unico del pre-procesamiento. `fuente` es la URL de la
    base (Postgres) o la ruta al SQLite (ver conexion.py). Lee las 3 tablas para el
    hospital y rango pedidos, corre los 3 analizadores, y devuelve el dict
    compacto listo para pasarle a la IA.

    Lanza SinDatosParaElPeriodo si NINGUNA de las 3 fuentes trajo filas
    (una fuente individual ausente, ej. un hospital sin canales Mirth, es
    normal y no corta el proceso).
    """
    config = config or get_configuracion(fuente)

    # El historial puede llegar como generador (FuenteServer: un mes son miles de reportes y no
    # conviene tenerlos todos en memoria): se mira si trae algo sin consumirlo.
    historial = iter(db.obtener_historial_crudo(fuente, hospital_id, fecha_inicio, fecha_fin))
    primera = next(historial, None)
    filas_historial = itertools.chain([primera], historial) if primera is not None else None
    filas_uso = db.obtener_uso_crudo(fuente, hospital_id, fecha_inicio, fecha_fin)
    filas_software = db.obtener_software_crudo(fuente, hospital_id, fecha_inicio, fecha_fin)
    backups = db.obtener_ultimos_backups(fuente, hospital_id, fecha_fin)

    if not filas_historial and not filas_uso and not filas_software:
        raise SinDatosParaElPeriodo(
            f"Ninguna de las 3 fuentes tiene datos para '{hospital_id}' "
            f"entre {fecha_inicio} y {fecha_fin}"
        )

    log.info("Pre-procesando %s: %d ventanas de uso, %d filas de software",
             hospital_id, len(filas_uso), len(filas_software))

    infra = (
        event_tracker.analizar_historial(filas_historial, config, fecha_inicio, fecha_fin)
        if filas_historial else None
    )
    clinico = backlog_analyzer.analizar_uso(filas_uso, config) if filas_uso else None
    software = (
        software_auditor.analizar_software(filas_software, config, backups=backups, fin=fecha_fin)
        if filas_software or backups else None
    )

    resumen = {
        "periodo": {"desde": fecha_inicio, "hasta": fecha_fin, "hospital_id": hospital_id},
        "infraestructura": infra,
        "actividad_clinica": clinico,
        "software": software,
    }
    resumen["comparacion"] = comparacion_periodo_anterior(fuente, hospital_id, fecha_inicio, fecha_fin, config)
    resumen["control_calidad"] = control_calidad(resumen)
    return resumen


# Debajo de esta proporción de reportes con un dato, se avisa que el dato es parcial.
_COBERTURA_MINIMA = 0.5


def control_calidad(resumen: dict[str, Any]) -> list[str]:
    """
    Avisos sobre los DATOS (no sobre el hospital) para quien revisa el borrador y para
    la IA: qué faltó o qué conviene verificar antes de aprobar. La IA tiene la regla de
    no afirmar nada sobre un punto marcado acá como sin datos.
    """
    avisos: list[str] = []
    infra = resumen.get("infraestructura")
    if not infra:
        avisos.append("Sin reportes de infraestructura en el período: no hay uptime ni métricas de hardware.")
    else:
        uptime = infra.get("uptime_pct")
        if uptime is not None and uptime < 90:
            avisos.append(
                f"Uptime de {uptime}%: verificar en el dashboard antes de aprobar "
                "(puede ser un corte real o el agente sin enviar)."
            )
        total = infra.get("muestras_analizadas") or 0
        con_dato = infra.get("reportes_con_dato") or {}
        nombres = {"energia": "energía (fuentes)", "temperatura": "temperaturas",
                   "raid": "RAID / discos físicos", "red": "latencia de red"}
        for clave, nombre in nombres.items():
            n = con_dato.get(clave, 0)
            if total and n == 0:
                avisos.append(f"Sin datos de {nombre} en el período (iDRAC no configurado o sin respuesta).")
            elif total and n / total < _COBERTURA_MINIMA:
                avisos.append(f"Datos de {nombre} sólo en {n} de {total} reportes: el análisis de ese punto es parcial.")
    if not resumen.get("actividad_clinica"):
        avisos.append("Sin KPIs de RIS/PACS en el período.")
    if not resumen.get("software"):
        avisos.append("Sin datos de software (Mirth, SSL, backups, CHECKDB) en el período.")
    return avisos


def cifras_periodo(fuente, hospital_id: str, inicio: str, fin: str, config: ConfiguracionGlobal) -> dict[str, Any]:
    """
    Cifras básicas de un período con fuentes livianas (horas de reporte, alertas y KPIs),
    para comparar con el período anterior sin releer los reportes completos.
    """
    from .conexion import a_datetime
    from .event_tracker import cobertura

    horas = db.obtener_horas_reporte(fuente, hospital_id, inicio, fin)
    uptime, _ = cobertura(horas, a_datetime(inicio), a_datetime(fin), config.offline_minutes)
    alertas = [a for a in db.obtener_alertas(fuente, hospital_id, inicio, fin)
               if not str(a.get("mensaje") or "").startswith("[BAJA]")]
    uso = db.obtener_uso_crudo(fuente, hospital_id, inicio, fin)
    estudios = None
    if uso:
        mods = backlog_analyzer.analizar_uso(uso, config).get("actividad_por_modalidad") or {}
        estudios = sum(m.get("totales", 0) for m in mods.values())
    return {"uptime_pct": uptime, "alertas": len(alertas), "estudios_totales": estudios, "reportes": len(horas)}


def periodo_anterior(inicio: str, fin: str) -> tuple[str, str]:
    """
    El período de igual duración inmediatamente anterior. Termina un segundo antes del
    inicio: los límites son inclusivos y el reporte del instante de inicio es del período actual.
    """
    from datetime import timedelta

    from .conexion import a_datetime
    d0, d1 = a_datetime(inicio), a_datetime(fin)
    largo = d1 - d0
    fmt = "%Y-%m-%d %H:%M:%S"
    return (d0 - largo).strftime(fmt), (d0 - timedelta(seconds=1)).strftime(fmt)


def comparacion_periodo_anterior(fuente, hospital_id: str, inicio: str, fin: str,
                                 config: ConfiguracionGlobal) -> dict[str, Any] | None:
    """Cifras del período y del anterior, para que la IA hable de tendencia con datos."""
    try:
        ini_ant, fin_ant = periodo_anterior(inicio, fin)
        anterior = cifras_periodo(fuente, hospital_id, ini_ant, fin_ant, config)
        actual = cifras_periodo(fuente, hospital_id, inicio, fin, config)
    except Exception:
        log.exception("No se pudo comparar con el período anterior de %s", hospital_id)
        return None
    if not anterior["reportes"]:
        return None   # sin datos del período anterior (hospital nuevo): no hay comparación
    return {"periodo_anterior": {"desde": ini_ant, "hasta": fin_ant}, "anterior": anterior, "actual": actual}
