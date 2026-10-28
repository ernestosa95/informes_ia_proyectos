"""
Resumen de un GRUPO de hospitales (una provincia, los de PAMI, los de un proyecto...).

Formato decidido (2026-10-27): un texto consolidado del grupo + una tabla con una
fila por hospital. Las filas las calcula este módulo; la IA recibe las filas y los
agregados y sólo redacta. Una sola llamada a la IA por informe, sin importar
cuántos hospitales tenga el grupo.

A diferencia del informe individual, no relee el JSON completo de cada reporte
(sería ~50 KB cada 5 min por hospital): usa fuentes livianas.

    disponibilidad   horas de reporte (db.obtener_horas_reporte) -> event_tracker.cobertura
    incidentes       tabla `alertas` del motor del server (db.obtener_alertas): lo mismo
                     que se vio en el dashboard y en Asana
    software         último estado por componente (db.obtener_estado_componentes)
                     y último backup por base (db.obtener_ultimos_backups)
    actividad        reportes_uso (pocas filas por día) -> backlog_analyzer

Los grupos los define el server (tablas grupos_hospitales); acá llegan ya resueltos:
{"id", "nombre", "hospital_ids": [...]}.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from . import backlog_analyzer, db
from .conexion import a_datetime
from .config_dinamica import ConfiguracionGlobal, get_configuracion
from .event_tracker import cobertura
from .logging_utils import get_logger

log = get_logger(__name__)

# Categoría de una alerta según el prefijo de su tipo (alerts_engine del server).
# Las de la primera lista son "críticas" para el criterio de atención.
_CATEGORIAS: list[tuple[str, str, bool]] = [
    ("OFFLINE", "hospital sin reportar", True),
    ("VM_OFFLINE_", "VM apagada", True),
    ("RAID_", "RAID / discos físicos", True),
    ("PSU_", "fuente de energía", True),
    ("FAN_", "ventiladores", True),
    ("SQLBACKUP", "backups SQL", True),
    ("CHECKDB", "integridad de bases", True),
    ("SVC_", "servicio detenido", True),
    ("DISK_", "disco de VM", False),
    ("VM_RAM_", "RAM de VM", False),
    ("VM_CPU_", "CPU de VM", False),
    ("VM_UPTIME_", "reinicio de VM", False),
    ("HOST_", "host", False),
    ("TEMP_", "temperatura", False),
    ("NETWORK_", "red", False),
    ("MIRTH_", "integraciones Mirth", False),
    ("DICOM_", "autoenrute DICOM", False),
    ("PORTAL_", "portal paciente", False),
    ("KPI_", "actividad RIS/PACS", False),
    ("SSL", "certificados SSL", False),
]

# Por debajo de esta disponibilidad el hospital "necesita atención".
UPTIME_ATENCION = 99.0


def _categoria(tipo: str) -> tuple[str, bool]:
    for prefijo, nombre, critica in _CATEGORIAS:
        if tipo.startswith(prefijo):
            return nombre, critica
    return "otras", False


def _fila_hospital(
    fuente: str | Path, hospital_id: str, inicio: str, fin: str, config: ConfiguracionGlobal
) -> dict[str, Any]:
    meta = db.obtener_config_hospital(fuente, hospital_id) or {}
    fila: dict[str, Any] = {"hospital_id": hospital_id, "nombre": meta.get("nombre") or hospital_id}

    horas = db.obtener_horas_reporte(fuente, hospital_id, inicio, fin)
    uptime, huecos = cobertura(horas, a_datetime(inicio), a_datetime(fin), config.offline_minutes)
    fila["uptime_pct"] = uptime
    fila["reportes"] = len(horas)
    fila["hueco_mas_largo_min"] = huecos[0]["duracion_min"] if huecos else 0

    # Incidentes: alertas del motor. Las cerradas con "[BAJA]" son de módulos dados de baja
    # (REQ-03): no son incidentes del hospital.
    alertas = [a for a in db.obtener_alertas(fuente, hospital_id, inicio, fin)
               if not str(a.get("mensaje") or "").startswith("[BAJA]")]
    por_categoria: dict[str, int] = {}
    criticas = 0
    for a in alertas:
        nombre, critica = _categoria(str(a.get("tipo") or ""))
        por_categoria[nombre] = por_categoria.get(nombre, 0) + 1
        criticas += critica
    abiertas_al_cierre = sum(1 for a in alertas if a.get("end_time") is None
                             or (a_datetime(a["end_time"]) or datetime.max) > (a_datetime(fin) or datetime.max))
    fila["incidentes"] = len(alertas)
    fila["incidentes_criticos"] = criticas
    fila["incidentes_abiertos_al_cierre"] = abiertas_al_cierre
    fila["incidentes_por_categoria"] = dict(sorted(por_categoria.items(), key=lambda x: -x[1]))

    # Software.
    canales = db.obtener_estado_componentes(fuente, hospital_id, "mirth", inicio, fin)
    fila["mirth_canales"] = len(canales)
    fila["mirth_detenidos"] = sum(1 for c in canales if (c.get("status_value") or "").upper() != "STARTED")
    fila["mirth_cola_maxima"] = max((int(c.get("maximo") or 0) for c in canales), default=0)
    certificados = db.obtener_estado_componentes(fuente, hospital_id, "ssl_certificate", inicio, fin)
    fila["ssl_por_vencer"] = sum(
        1 for c in certificados
        if (c.get("status_value") or "").upper() == "ERROR"
        or (c.get("metric_value") is not None and int(c["metric_value"]) < config.ssl_warning_days)
    )
    fin_dt = a_datetime(fin) or datetime.now()
    backups = db.obtener_ultimos_backups(fuente, hospital_id, fin)
    fila["bases_sin_backup"] = sum(
        1 for b in backups
        if (b.get("status_value") or "").upper() == "NEVER"
        or ((fin_dt - (a_datetime(b.get("timestamp")) or fin_dt)).total_seconds() / 3600 > config.sql_backup_max_hours)
    )
    checkdb = db.obtener_estado_componentes(fuente, hospital_id, "sql_integrity", inicio, fin)
    fila["bases_con_error_checkdb"] = sum(1 for c in checkdb if (c.get("status_value") or "").upper() != "OK")

    # Actividad clínica (totales del período).
    uso = db.obtener_uso_crudo(fuente, hospital_id, inicio, fin)
    if uso:
        clinico = backlog_analyzer.analizar_uso(uso, config)
        mods = clinico.get("actividad_por_modalidad") or {}
        fila["estudios_totales"] = sum(m.get("totales", 0) for m in mods.values())
        fila["estudios_con_imagen"] = sum(m.get("con_imagen", 0) for m in mods.values())
    else:
        fila["estudios_totales"] = None

    fila["motivos_atencion"] = _motivos(fila)
    return fila


def _motivos(f: dict[str, Any]) -> list[str]:
    """Por qué un hospital necesita atención (vacío = sin novedades). Orden = prioridad."""
    m: list[str] = []
    if not f["reportes"]:
        return ["sin reportes en el período"]
    if f["uptime_pct"] is not None and f["uptime_pct"] < UPTIME_ATENCION:
        m.append(f"disponibilidad {f['uptime_pct']}%")
    if f["incidentes_criticos"]:
        m.append(f"{f['incidentes_criticos']} incidente(s) crítico(s)")
    if f["bases_sin_backup"]:
        m.append(f"{f['bases_sin_backup']} base(s) sin backup reciente")
    if f["bases_con_error_checkdb"]:
        m.append(f"{f['bases_con_error_checkdb']} base(s) con error de integridad")
    if f["mirth_detenidos"]:
        m.append(f"{f['mirth_detenidos']} canal(es) Mirth detenido(s)")
    if f["ssl_por_vencer"]:
        m.append(f"{f['ssl_por_vencer']} certificado(s) SSL por vencer")
    if f["incidentes_abiertos_al_cierre"]:
        m.append(f"{f['incidentes_abiertos_al_cierre']} incidente(s) abierto(s) al cierre")
    return m


def generar_resumen_grupo(
    fuente: str | Path,
    grupo: dict[str, Any],
    fecha_inicio: str,
    fecha_fin: str,
    config: ConfiguracionGlobal | None = None,
) -> dict[str, Any]:
    """Resumen compacto del grupo: agregados + una fila por hospital + control de calidad."""
    config = config or get_configuracion(fuente)
    ids = list(dict.fromkeys(grupo.get("hospital_ids") or []))   # sin repetidos, en orden
    if not ids:
        raise ValueError(f"El grupo '{grupo.get('nombre')}' no tiene hospitales")

    log.info("Resumen de grupo '%s': %d hospitales", grupo.get("nombre"), len(ids))
    filas = []
    for hid in ids:
        try:
            filas.append(_fila_hospital(fuente, hid, fecha_inicio, fecha_fin, config))
        except Exception:
            log.exception("No se pudo resumir %s; queda como sin datos", hid)
            filas.append({"hospital_id": hid, "nombre": hid, "reportes": 0, "uptime_pct": None,
                          "motivos_atencion": ["error al leer sus datos"]})

    con_datos = [f for f in filas if f.get("reportes")]
    uptimes = [f["uptime_pct"] for f in con_datos if f.get("uptime_pct") is not None]
    atencion = sorted(
        (f for f in filas if f.get("motivos_atencion")),
        key=lambda f: (-len(f["motivos_atencion"]), f.get("uptime_pct") if f.get("uptime_pct") is not None else -1),
    )
    estudios = [f["estudios_totales"] for f in con_datos if f.get("estudios_totales") is not None]

    agregados = {
        "hospitales": len(filas),
        "hospitales_con_datos": len(con_datos),
        "hospitales_que_requieren_atencion": len(atencion),
        "uptime_promedio_pct": round(sum(uptimes) / len(uptimes), 2) if uptimes else None,
        "uptime_minimo_pct": min(uptimes) if uptimes else None,
        "incidentes_totales": sum(f.get("incidentes", 0) for f in con_datos),
        "incidentes_criticos_totales": sum(f.get("incidentes_criticos", 0) for f in con_datos),
        "estudios_totales": sum(estudios) if estudios else None,
    }

    avisos = []
    sin_datos = [f["hospital_id"] for f in filas if not f.get("reportes")]
    if sin_datos:
        avisos.append(f"Sin reportes en el período: {', '.join(sin_datos)}. Verificar si es una caída real o un hospital sin agente.")
    if len(estudios) < len(con_datos):
        avisos.append(f"Sin KPIs de RIS/PACS en {len(con_datos) - len(estudios)} hospital(es): el total de estudios es parcial.")

    return {
        "periodo": {"desde": fecha_inicio, "hasta": fecha_fin},
        "grupo": {"id": grupo.get("id"), "nombre": grupo.get("nombre")},
        "agregados": agregados,
        "hospitales": filas,
        "hospitales_que_requieren_atencion": [
            {"hospital_id": f["hospital_id"], "nombre": f["nombre"], "motivos": f["motivos_atencion"]}
            for f in atencion
        ],
        "control_calidad": avisos,
    }
