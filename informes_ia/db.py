"""
Módulo 2: Datos SQL.

Este módulo sólo lee de SQLite, no interpreta ni agrega nada — la
agregación vive en event_tracker.py / backlog_analyzer.py / software_auditor.py
(ver preprocess.py, que orquesta los tres). Expone:

- obtener_config_hospital: metadata básica (nombre, proyecto Asana)
- generar_grafico_historico: PNG de CPU/RAM en el rango pedido
- obtener_historial_crudo / obtener_uso_crudo / obtener_software_crudo:
  fetchers crudos de las 3 tablas de telemetría/uso/software
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from dateutil import parser

from .logging_utils import get_logger

log = get_logger(__name__)


def _conectar(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"No se encontró la base de datos en {db_path}")
    return sqlite3.connect(db_path)


def obtener_config_hospital(db_path: Path, hospital_id: str) -> dict[str, Any] | None:
    with _conectar(db_path) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT nombre, asana_project_id FROM hospitales_metadata WHERE hospital_id = ?",
                (hospital_id,),
            )
            res = cursor.fetchone()
        except sqlite3.Error:
            log.exception("Error consultando metadata del hospital %s", hospital_id)
            return None
    return {"nombre": res[0], "asana_id": res[1]} if res else None


def generar_grafico_historico(
    db_path: Path, hospital_id: str, inicio: str, fin: str, ruta_salida: Path
) -> Path | None:
    log.info("Generando gráfico histórico para %s (%s a %s)", hospital_id, inicio, fin)

    with _conectar(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT timestamp, host_cpu_usage, host_ram_usage, full_json_data
               FROM reportes_historicos
               WHERE hospital_id = ? AND timestamp BETWEEN ? AND ?
               ORDER BY timestamp ASC""",
            (hospital_id, inicio, fin),
        )
        rows = cursor.fetchall()

    if not rows:
        log.warning("Sin datos históricos para %s en el rango pedido", hospital_id)
        return None

    fechas, cpu_pct, ram_pct = [], [], []
    total_ram_gb = 16.0
    try:
        last_json = json.loads(rows[-1][3])
        total_ram_gb = last_json.get("physical_host", {}).get("ram_total_gb", 16.0)
    except (json.JSONDecodeError, TypeError, KeyError):
        log.debug("No se pudo leer ram_total_gb del último registro, se usa default 16GB")

    for r in rows:
        try:
            dt = parser.parse(r[0])
            fechas.append(dt)
            cpu_pct.append(r[1] if r[1] else 0)
            ram_pct.append(((r[2] if r[2] else 0) / total_ram_gb) * 100)
        except (ValueError, TypeError, ZeroDivisionError):
            continue

    if not fechas:
        return None

    plt.style.use("bmh")
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(fechas, cpu_pct, label="CPU (%)", color="#3498db", linewidth=2)
    ax.fill_between(fechas, cpu_pct, color="#3498db", alpha=0.1)
    ax.plot(fechas, ram_pct, label="RAM (%)", color="#27ae60", linewidth=2)
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
    db_path: Path, hospital_id: str, inicio: str, fin: str
) -> list[tuple[str, str | None]]:
    """
    Filas crudas de `reportes_historicos` para alimentar EventTracker
    (informes_ia.event_tracker). Devuelve (full_json_data, host_status)
    ordenado por timestamp ascendente.
    """
    with _conectar(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT full_json_data, host_status FROM reportes_historicos
               WHERE hospital_id = ? AND timestamp BETWEEN ? AND ?
               ORDER BY timestamp ASC""",
            (hospital_id, inicio, fin),
        )
        return cursor.fetchall()


def obtener_uso_crudo(
    db_path: Path, hospital_id: str, inicio: str, fin: str
) -> list[tuple[str, str]]:
    """
    Filas crudas de `reportes_uso` para alimentar BacklogAnalyzer
    (informes_ia.backlog_analyzer). Devuelve (kpi_json_data, timestamp)
    ordenado por timestamp ascendente.
    """
    with _conectar(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT kpi_json_data, timestamp FROM reportes_uso
               WHERE hospital_id = ? AND timestamp BETWEEN ? AND ?
               ORDER BY timestamp ASC""",
            (hospital_id, inicio, fin),
        )
        return cursor.fetchall()


def obtener_software_crudo(
    db_path: Path, hospital_id: str, inicio: str, fin: str
) -> list[dict[str, Any]]:
    """
    Filas crudas de `software_monitoring` para alimentar SoftwareAuditor
    (informes_ia.software_auditor). Devuelve dicts con las columnas
    nombradas, ordenado por timestamp ascendente.
    """
    with _conectar(db_path) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """SELECT app_name, component_id, status_value, metric_value, extra_data, timestamp
               FROM software_monitoring
               WHERE hospital_id = ? AND timestamp BETWEEN ? AND ?
               ORDER BY timestamp ASC""",
            (hospital_id, inicio, fin),
        )
        return [dict(row) for row in cursor.fetchall()]
