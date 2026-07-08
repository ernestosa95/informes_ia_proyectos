"""
BacklogAnalyzer: agrega los registros de `reportes_uso` (actividad clínica
RIS/PACS) del período y aproxima el backlog de informes por modalidad
usando los umbrales de negocio de `configuracion` (kpi_rad_*, kpi_mamo_*).

LIMITACIÓN CONOCIDA (documentada, no resuelta): `reportes_uso` trae
conteos agregados por ventana de 8/24hs (`borradores`, `definitivos`, etc.
por equipo/modalidad), no el detalle de cada estudio individual con su
propio timestamp. Por eso "cuántos estudios llevan más de N horas sin
cerrar" acá es una APROXIMACIÓN: tomamos el conteo de `borradores` en el
último snapshot del período como proxy del backlog pendiente al cierre.
Si en el sistema origen (SUITESTENSA) existe una vista a nivel de estudio,
migrar este cálculo a eso será mucho más preciso. Ver README > TODOs.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from .config_dinamica import ConfiguracionGlobal
from .logging_utils import get_logger

log = get_logger(__name__)


@dataclass
class _AgregadoModalidad:
    totales: int = 0
    admitidos: int = 0
    ejecutados: int = 0
    con_imagen: int = 0
    borradores_ultimo: int = 0
    definitivos: int = 0
    suspendidos: int = 0


def analizar_uso(
    filas: list[tuple[str, str]], config: ConfiguracionGlobal
) -> dict[str, Any]:
    """
    filas: lista de (kpi_json_data, timestamp_str) del período, para UN hospital,
    ya ordenadas por timestamp ascendente (importa para tomar el "último
    snapshot" al calcular el backlog aproximado).
    """
    por_modalidad: dict[str, _AgregadoModalidad] = defaultdict(_AgregadoModalidad)
    almacenamiento_por_aet: dict[str, int] = defaultdict(int)
    logins_por_rol: dict[str, dict[str, int]] = defaultdict(lambda: {"usuarios_unicos": 0, "inicios_sesion": 0})

    ventanas_procesadas = 0

    for raw_json_str, _ts in filas:
        try:
            data = json.loads(raw_json_str)
        except (json.JSONDecodeError, TypeError):
            continue
        ventanas_procesadas += 1

        for item in data.get("ris", []) or []:
            mod = item.get("mod", "?")
            agg = por_modalidad[mod]
            agg.totales += item.get("totales", 0) or 0
            agg.admitidos += item.get("admitidos", 0) or 0
            agg.ejecutados += item.get("ejecutados", 0) or 0
            agg.con_imagen += item.get("con_imagen", 0) or 0
            agg.definitivos += item.get("definitivos", 0) or 0
            agg.suspendidos += item.get("suspendidos", 0) or 0
            # Backlog aproximado: nos quedamos con el valor del ÚLTIMO
            # snapshot procesado (no se suma, se reemplaza), porque
            # `borradores` es un conteo de "pendientes ahora", no un
            # delta acumulable entre ventanas.
            agg.borradores_ultimo = item.get("borradores", 0) or 0

        for item in data.get("pacs", []) or []:
            almacenamiento_por_aet[item.get("aet", "?")] += item.get("almacenados", 0) or 0

        for item in data.get("users", []) or []:
            rol = item.get("rol", "?")
            logins_por_rol[rol]["usuarios_unicos"] = max(
                logins_por_rol[rol]["usuarios_unicos"], item.get("usuarios_unicos", 0) or 0
            )
            logins_por_rol[rol]["inicios_sesion"] += item.get("inicios_sesion", 0) or 0

    backlog_rad = None
    if config.kpi_rad.enabled and config.kpi_rad.modalidades:
        backlog_rad = {
            mod: por_modalidad[mod].borradores_ultimo
            for mod in config.kpi_rad.modalidades
            if mod in por_modalidad
        }

    return {
        "ventanas_procesadas": ventanas_procesadas,
        "actividad_por_modalidad": {
            mod: {
                "totales": a.totales,
                "admitidos": a.admitidos,
                "ejecutados": a.ejecutados,
                "con_imagen": a.con_imagen,
                "definitivos": a.definitivos,
                "suspendidos": a.suspendidos,
                "borradores_pendientes_al_cierre": a.borradores_ultimo,
            }
            for mod, a in por_modalidad.items()
        },
        "backlog_radiologia": {
            "umbral_horas": config.kpi_rad.threshold_hours,
            "modalidades_monitoreadas": config.kpi_rad.modalidades,
            "pendientes_por_modalidad": backlog_rad,
            "nota": (
                "Aproximación: conteo de 'borradores' en el último snapshot del "
                "período. No refleja antigüedad individual por estudio."
            ),
        },
        "almacenamiento_pacs_por_aet": dict(almacenamiento_por_aet),
        "actividad_usuarios_por_rol": dict(logins_por_rol),
    }
