"""
Módulo 2: acceso a datos.

Funciones de lectura que delegan en una FUENTE (fuentes.py): `fuente` puede
ser una URL / ruta (esquema viejo, `FuenteSQL`) o un objeto que cumpla
`FuenteDatos` (en producción, el `FuenteServer` del server, que lee Postgres).
No interpreta ni agrega nada: la agregación vive en event_tracker.py /
backlog_analyzer.py / software_auditor.py / grupo.py.

También genera los gráficos (PNG) de los informes.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # sin display: corre en el server/worker
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from .conexion import a_datetime
from .fuentes import resolver
from .logging_utils import get_logger

log = get_logger(__name__)



def obtener_config_hospital(fuente, hospital_id: str) -> dict[str, Any] | None:
    return resolver(fuente).obtener_config_hospital(hospital_id)


def leer_configuracion(fuente) -> dict[str, str]:
    return resolver(fuente).leer_configuracion()


def obtener_historial_crudo(fuente, hospital_id: str, inicio: str, fin: str):
    """(reporte completo, host_status, hora) por reporte, en orden cronológico."""
    return resolver(fuente).obtener_historial_crudo(hospital_id, inicio, fin)


def obtener_horas_reporte(fuente, hospital_id: str, inicio: str, fin: str) -> list[Any]:
    return resolver(fuente).obtener_horas_reporte(hospital_id, inicio, fin)


def obtener_uso_crudo(fuente, hospital_id: str, inicio: str, fin: str):
    return resolver(fuente).obtener_uso_crudo(hospital_id, inicio, fin)


def obtener_software_crudo(fuente, hospital_id: str, inicio: str, fin: str) -> list[dict[str, Any]]:
    return resolver(fuente).obtener_software_crudo(hospital_id, inicio, fin)


def obtener_ultimos_backups(fuente, hospital_id: str, fin: str) -> list[dict[str, Any]]:
    return resolver(fuente).obtener_ultimos_backups(hospital_id, fin)


def obtener_alertas(fuente, hospital_id: str, inicio: str, fin: str) -> list[dict[str, Any]]:
    return resolver(fuente).obtener_alertas(hospital_id, inicio, fin)


def obtener_estado_componentes(fuente, hospital_id: str, app_name: str, inicio: str, fin: str) -> list[dict[str, Any]]:
    return resolver(fuente).obtener_estado_componentes(hospital_id, app_name, inicio, fin)


def generar_grafico_historico(fuente, hospital_id: str, inicio: str, fin: str, ruta_salida: Path) -> Path | None:
    """CPU y RAM del host, en %."""
    log.info("Generando gráfico histórico para %s (%s a %s)", hospital_id, inicio, fin)
    rows = resolver(fuente).obtener_serie_grafico(hospital_id, inicio, fin)
    if not rows:
        log.warning("Sin datos históricos para %s en el rango pedido", hospital_id)
        return None

    fechas: list[datetime] = []
    serie_cpu: list[float] = []
    serie_ram: list[float] = []
    for ts, cpu, ram in rows:
        dt = a_datetime(ts)
        if dt is None:
            continue
        fechas.append(dt)
        # None = punto sin dato: matplotlib corta la línea ahí en vez de dibujar un 0.
        serie_cpu.append(float(cpu) if cpu is not None else float("nan"))
        serie_ram.append(float(ram) if ram is not None else float("nan"))
    if not fechas:
        return None
    # Sin un solo valor de CPU ni de RAM del host (ej. hospital sin datos del hipervisor, H05
    # 2026-10-28) el gráfico sería un recuadro vacío: mejor no mostrarlo.
    if all(v != v for v in serie_cpu + serie_ram):     # todos NaN
        log.info("Sin CPU/RAM del host para %s: se omite el gráfico", hospital_id)
        return None

    plt.style.use("bmh")
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(fechas, serie_cpu, label="CPU (%)", color="#3498db", linewidth=2)
    ax.fill_between(fechas, serie_cpu, color="#3498db", alpha=0.1)
    ax.plot(fechas, serie_ram, label="RAM (%)", color="#27ae60", linewidth=2)
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


def generar_grafico_grupo(filas: list[dict[str, Any]], titulo: str, ruta_salida: Path) -> Path | None:
    """Barras horizontales: disponibilidad de cada hospital del grupo (en verde, ámbar o rojo)."""
    datos = [(f["hospital_id"], f["uptime_pct"]) for f in filas if f.get("uptime_pct") is not None]
    if not datos:
        return None
    datos.sort(key=lambda d: d[1])
    etiquetas = [d[0] for d in datos]
    valores = [d[1] for d in datos]
    colores = ["#c0392b" if v < 95 else "#f39c12" if v < 99 else "#27ae60" for v in valores]

    plt.style.use("bmh")
    fig, ax = plt.subplots(figsize=(10, max(2.5, 0.32 * len(datos) + 1)))
    ax.barh(etiquetas, valores, color=colores)
    ax.set_xlim(min(80.0, min(valores) - 2), 100)
    ax.axvline(99, color="#7f8c8d", linestyle="--", linewidth=1)
    ax.set_title(titulo, fontsize=12, pad=10, color="#2c3e50")
    ax.set_xlabel("Disponibilidad (%)")
    ax.tick_params(axis="y", labelsize=8)
    ax.grid(True, axis="x", alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()
    ruta_salida.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(ruta_salida, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return ruta_salida
