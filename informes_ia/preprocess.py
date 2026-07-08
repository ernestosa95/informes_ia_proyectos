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

from pathlib import Path
from typing import Any

from . import backlog_analyzer, db, event_tracker, software_auditor
from .config_dinamica import ConfiguracionGlobal, get_configuracion
from .logging_utils import get_logger

log = get_logger(__name__)


class SinDatosParaElPeriodo(RuntimeError):
    pass


def generar_resumen(
    db_path: Path,
    hospital_id: str,
    fecha_inicio: str,
    fecha_fin: str,
    config: ConfiguracionGlobal | None = None,
) -> dict[str, Any]:
    """
    Punto de entrada unico del pre-procesamiento. Lee las 3 tablas para el
    hospital y rango pedidos, corre los 3 analizadores, y devuelve el dict
    compacto listo para pasarle a la IA.

    Lanza SinDatosParaElPeriodo si NINGUNA de las 3 fuentes trajo filas
    (una fuente individual ausente, ej. un hospital sin canales Mirth, es
    normal y no corta el proceso).
    """
    config = config or get_configuracion(db_path)

    filas_historial = db.obtener_historial_crudo(db_path, hospital_id, fecha_inicio, fecha_fin)
    filas_uso = db.obtener_uso_crudo(db_path, hospital_id, fecha_inicio, fecha_fin)
    filas_software = db.obtener_software_crudo(db_path, hospital_id, fecha_inicio, fecha_fin)

    if not filas_historial and not filas_uso and not filas_software:
        raise SinDatosParaElPeriodo(
            f"Ninguna de las 3 fuentes tiene datos para '{hospital_id}' "
            f"entre {fecha_inicio} y {fecha_fin}"
        )

    log.info(
        "Pre-procesando %s: %d snapshots infra, %d ventanas de uso, %d filas de software",
        hospital_id, len(filas_historial), len(filas_uso), len(filas_software),
    )

    infra = event_tracker.analizar_historial(filas_historial, config) if filas_historial else None
    clinico = backlog_analyzer.analizar_uso(filas_uso, config) if filas_uso else None
    software = software_auditor.analizar_software(filas_software, config) if filas_software else None

    return {
        "periodo": {"desde": fecha_inicio, "hasta": fecha_fin, "hospital_id": hospital_id},
        "infraestructura": infra,
        "actividad_clinica": clinico,
        "software": software,
    }
