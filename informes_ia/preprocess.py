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
