"""
Módulo 2: Datos SQL.

Este módulo sólo lee de la base de TecnoMonitor (Postgres o SQLite, ver
conexion.py), no interpreta ni agrega nada — la agregación vive en
event_tracker.py / backlog_analyzer.py / software_auditor.py (ver
preprocess.py, que orquesta los tres). Expone:

- obtener_config_hospital: metadata básica (nombre, proyecto Asana)
- generar_grafico_historico: PNG de CPU/RAM del host en el rango pedido
- obtener_historial_crudo / obtener_uso_crudo / obtener_software_crudo:
  fetchers crudos de las 3 tablas de telemetría/uso/software

`fuente` es una URL de SQLAlchemy o la ruta a un archivo SQLite
(conexion.get_engine).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # sin display: corre en el server/worker
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .conexion import a_datetime, get_engine, json_escalar
from .logging_utils import get_logger

log = get_logger(__name__)


def obtener_config_hospital(fuente: str | Path, hospital_id: str) -> dict[str, Any] | None:
    engine = get_engine(fuente)
    try:
        with engine.connect() as conn:
            res = conn.execute(
                text("SELECT nombre, asana_project_id FROM hospitales_metadata WHERE hospital_id = :h"),
                {"h": hospital_id},
            ).fetchone()
    except SQLAlchemyError:
        log.exception("Error consultando metadata del hospital %s", hospital_id)
        return None
    return {"nombre": res[0], "asana_id": res[1]} if res else None


def generar_grafico_historico(
    fuente: str | Path, hospital_id: str, inicio: str, fin: str, ruta_salida: Path
) -> Path | None:
    """
    CPU y RAM del host, en %. La RAM sale de `physical_layer.telemetry.ram.usage_percent`
    del JSON: la columna `host_ram_usage` del server guarda GB usados, no porcentaje.
    Sólo se extraen esos escalares en SQL, sin traer el JSON completo de cada fila.
    """
    log.info("Generando gráfico histórico para %s (%s a %s)", hospital_id, inicio, fin)
    engine = get_engine(fuente)
    ram_pct = json_escalar(engine, "full_json_data", "physical_layer", "telemetry", "ram", "usage_percent")

    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""SELECT timestamp, host_cpu_usage, {ram_pct} AS ram_pct
                     FROM reportes_historicos
                     WHERE hospital_id = :h AND timestamp BETWEEN :ini AND :fin
                     ORDER BY timestamp ASC"""),
            {"h": hospital_id, "ini": inicio, "fin": fin},
        ).fetchall()

    if not rows:
        log.warning("Sin datos históricos para %s en el rango pedido", hospital_id)
        return None

    fechas: list[datetime] = []
    serie_cpu: list[float | None] = []
    serie_ram: list[float | None] = []
    for ts, cpu, ram in rows:
        dt = a_datetime(ts)
        if dt is None:
            continue
        fechas.append(dt)
        serie_cpu.append(float(cpu) if cpu is not None else None)
        serie_ram.append(float(ram) if ram is not None else None)

    if not fechas:
        return None

    plt.style.use("bmh")
    fig, ax = plt.subplots(figsize=(10, 4))
    # None = punto sin dato: matplotlib corta la línea ahí en vez de dibujar un 0.
    cpu_plot = [float("nan") if v is None else v for v in serie_cpu]
    ram_plot = [float("nan") if v is None else v for v in serie_ram]
    ax.plot(fechas, cpu_plot, label="CPU (%)", color="#3498db", linewidth=2)
    ax.fill_between(fechas, cpu_plot, color="#3498db", alpha=0.1)
    ax.plot(fechas, ram_plot, label="RAM (%)", color="#27ae60", linewidth=2)
    ax.axhline(y=90, color="#e74c3c", linestyle="--", linewidth=1, label="Crítico")
    ax.set_xlim(min(fechas), max(fechas))
    ax.set_ylim(0, 100)
    ax.set_title(f"Rendimiento: {hospital_id}", fontsize=12, pad=10, color="#2c3e50")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    fig.autofmt_xdate(rotation=0, ha="center")
    ax.grid(True, alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="upper left", frameon=True, facecolor="white", fontsize=9)
    plt.tight_layout()

    ruta_salida.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(ruta_salida, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return ruta_salida


def obtener_historial_crudo(
    fuente: str | Path, hospital_id: str, inicio: str, fin: str
) -> list[tuple[Any, str | None, Any]]:
    """
    Filas crudas de `reportes_historicos` para alimentar EventTracker
    (informes_ia.event_tracker). Devuelve (full_json_data, host_status, timestamp)
    ordenado por timestamp ascendente. full_json_data puede venir como dict
    (Postgres) o texto (SQLite); el timestamp, como datetime o texto.
    """
    with get_engine(fuente).connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text("""SELECT full_json_data, host_status, timestamp FROM reportes_historicos
                        WHERE hospital_id = :h AND timestamp BETWEEN :ini AND :fin
                        ORDER BY timestamp ASC"""),
                {"h": hospital_id, "ini": inicio, "fin": fin},
            )
        ]


def obtener_uso_crudo(
    fuente: str | Path, hospital_id: str, inicio: str, fin: str
) -> list[tuple[str, Any]]:
    """
    Filas crudas de `reportes_uso` para alimentar BacklogAnalyzer
    (informes_ia.backlog_analyzer). Devuelve (kpi_json_data, timestamp)
    ordenado por timestamp ascendente.
    """
    with get_engine(fuente).connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text("""SELECT kpi_json_data, timestamp FROM reportes_uso
                        WHERE hospital_id = :h AND timestamp BETWEEN :ini AND :fin
                        ORDER BY timestamp ASC"""),
                {"h": hospital_id, "ini": inicio, "fin": fin},
            )
        ]


def obtener_software_crudo(
    fuente: str | Path, hospital_id: str, inicio: str, fin: str
) -> list[dict[str, Any]]:
    """
    Filas crudas de `software_monitoring` para alimentar SoftwareAuditor
    (informes_ia.software_auditor). Devuelve dicts con las columnas
    nombradas, ordenado por timestamp ascendente.
    """
    with get_engine(fuente).connect() as conn:
        return [
            dict(r._mapping)
            for r in conn.execute(
                text("""SELECT app_name, component_id, status_value, metric_value, extra_data, timestamp
                        FROM software_monitoring
                        WHERE hospital_id = :h AND timestamp BETWEEN :ini AND :fin
                        ORDER BY timestamp ASC"""),
                {"h": hospital_id, "ini": inicio, "fin": fin},
            )
        ]


def obtener_ultimos_backups(fuente: str | Path, hospital_id: str, fin: str) -> list[dict[str, Any]]:
    """
    Último backup completo de cada base hasta `fin` (`software_monitoring.sql_backup`, REQ-06).
    Va aparte de obtener_software_crudo porque el server guarda cada backup con la fecha en
    que se hizo: el de una base puede ser anterior al período y igual es "el último".
    """
    with get_engine(fuente).connect() as conn:
        return [
            dict(r._mapping)
            for r in conn.execute(
                text("""SELECT s.component_id, s.status_value, s.timestamp
                        FROM software_monitoring s
                        JOIN (SELECT component_id, MAX(timestamp) AS ts FROM software_monitoring
                              WHERE hospital_id = :h AND app_name = 'sql_backup' AND timestamp <= :fin
                              GROUP BY component_id) u
                          ON u.component_id = s.component_id AND u.ts = s.timestamp
                        WHERE s.hospital_id = :h AND s.app_name = 'sql_backup'
                        ORDER BY s.component_id"""),
                {"h": hospital_id, "fin": fin},
            )
        ]
