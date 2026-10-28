"""
De dónde lee el módulo los datos de TecnoMonitor.

El módulo no consulta tablas directamente: le pide los datos a una FUENTE que
cumple el protocolo `FuenteDatos`. Hay dos:

- `FuenteSQL` (acá): esquema VIEJO de TecnoMonitor (`reportes_historicos`,
  `reportes_uso`, `software_monitoring` con el JSON completo de cada reporte),
  en SQLite o en cualquier motor con esas tablas. Sirve para el laboratorio
  (una copia de la base vieja) y para los tests.

- `FuenteServer` (en el server, `dashboard_app/informes_fuente.py`): usa la
  capa `datos/` del server, que lee tanto el SQLite viejo como el Postgres de
  producción (desde el 05/10/2026: métricas tipadas + inventario + reporte
  crudo, `postgres/esquema.sql`). Es la que se usa en producción: el server
  la inyecta en `PipelineReal(settings, fuente=...)`.

Contra el Postgres de producción el módulo NO puede usar `FuenteSQL`: ese
esquema no tiene `reportes_historicos`. Ver ARQUITECTURA.md > Fuentes de datos.

Formatos que devuelve cualquier fuente (los consumen preprocess, grupo y db):
  - fechas: datetime o texto ISO (se normalizan con conexion.a_datetime);
  - JSON: dict o texto (se normalizan con conexion.a_dict).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .conexion import get_engine, json_escalar
from .logging_utils import get_logger

log = get_logger(__name__)


@runtime_checkable
class FuenteDatos(Protocol):
    def obtener_config_hospital(self, hospital_id: str) -> dict[str, Any] | None:
        """{"nombre", "asana_id"} o None si el hospital no existe."""

    def leer_configuracion(self) -> dict[str, str]:
        """Tabla `configuracion` (clave -> valor)."""

    def obtener_historial_crudo(self, hospital_id: str, inicio: str, fin: str) -> Iterable[tuple[Any, Any, Any]]:
        """(reporte completo del agente, host_status, hora) por reporte, en orden cronológico."""

    def obtener_horas_reporte(self, hospital_id: str, inicio: str, fin: str) -> list[Any]:
        """Sólo la hora de cada reporte del período."""

    def obtener_serie_grafico(self, hospital_id: str, inicio: str, fin: str) -> list[tuple[Any, Any, Any]]:
        """(hora, CPU % del host, RAM % del host) para el gráfico."""

    def obtener_uso_crudo(self, hospital_id: str, inicio: str, fin: str) -> list[tuple[Any, Any]]:
        """(application_metrics, hora) de los KPIs de RIS/PACS del período."""

    def obtener_software_crudo(self, hospital_id: str, inicio: str, fin: str) -> list[dict[str, Any]]:
        """Lecturas de software del período: app_name, component_id, status_value, metric_value, extra_data, timestamp."""

    def obtener_ultimos_backups(self, hospital_id: str, fin: str) -> list[dict[str, Any]]:
        """Último backup por base hasta `fin`: component_id, status_value, timestamp."""

    def obtener_alertas(self, hospital_id: str, inicio: str, fin: str) -> list[dict[str, Any]]:
        """Alertas del motor abiertas en algún momento del período: tipo, mensaje, start_time, end_time."""

    def obtener_estado_componentes(self, hospital_id: str, app_name: str, inicio: str, fin: str) -> list[dict[str, Any]]:
        """Por componente: último component_id, status_value, metric_value, timestamp del período y `maximo`."""


def resolver(fuente: "str | Path | FuenteDatos") -> FuenteDatos:
    """Una URL / ruta se convierte en FuenteSQL; una fuente ya armada se devuelve tal cual."""
    if isinstance(fuente, (str, Path)):
        return FuenteSQL(fuente)
    return fuente


class FuenteSQL:
    """Esquema viejo de TecnoMonitor (ver docstring del módulo)."""

    def __init__(self, url_o_ruta: str | Path):
        self.engine = get_engine(url_o_ruta)

    def _filas(self, sql: str, params: dict[str, Any]) -> list[Any]:
        with self.engine.connect() as conn:
            return list(conn.execute(text(sql), params))

    def obtener_config_hospital(self, hospital_id: str) -> dict[str, Any] | None:
        try:
            filas = self._filas("SELECT nombre, asana_project_id FROM hospitales_metadata WHERE hospital_id = :h",
                                {"h": hospital_id})
        except SQLAlchemyError:
            log.exception("Error consultando metadata del hospital %s", hospital_id)
            return None
        return {"nombre": filas[0][0], "asana_id": filas[0][1]} if filas else None

    def leer_configuracion(self) -> dict[str, str]:
        return {c: v for c, v in self._filas("SELECT clave, valor FROM configuracion", {})}

    def obtener_historial_crudo(self, hospital_id, inicio, fin):
        return [tuple(r) for r in self._filas(
            """SELECT full_json_data, host_status, timestamp FROM reportes_historicos
               WHERE hospital_id = :h AND timestamp BETWEEN :ini AND :fin ORDER BY timestamp ASC""",
            {"h": hospital_id, "ini": inicio, "fin": fin})]

    def obtener_horas_reporte(self, hospital_id, inicio, fin):
        return [r[0] for r in self._filas(
            """SELECT timestamp FROM reportes_historicos
               WHERE hospital_id = :h AND timestamp BETWEEN :ini AND :fin ORDER BY timestamp ASC""",
            {"h": hospital_id, "ini": inicio, "fin": fin})]

    def obtener_serie_grafico(self, hospital_id, inicio, fin):
        # La RAM sale del JSON: la columna host_ram_usage guarda GB usados, no porcentaje.
        ram = json_escalar(self.engine, "full_json_data", "physical_layer", "telemetry", "ram", "usage_percent")
        return [tuple(r) for r in self._filas(
            f"""SELECT timestamp, host_cpu_usage, {ram} FROM reportes_historicos
                WHERE hospital_id = :h AND timestamp BETWEEN :ini AND :fin ORDER BY timestamp ASC""",
            {"h": hospital_id, "ini": inicio, "fin": fin})]

    def obtener_uso_crudo(self, hospital_id, inicio, fin):
        return [tuple(r) for r in self._filas(
            """SELECT kpi_json_data, timestamp FROM reportes_uso
               WHERE hospital_id = :h AND timestamp BETWEEN :ini AND :fin ORDER BY timestamp ASC""",
            {"h": hospital_id, "ini": inicio, "fin": fin})]

    def obtener_software_crudo(self, hospital_id, inicio, fin):
        return [dict(r._mapping) for r in self._filas(
            """SELECT app_name, component_id, status_value, metric_value, extra_data, timestamp
               FROM software_monitoring
               WHERE hospital_id = :h AND timestamp BETWEEN :ini AND :fin ORDER BY timestamp ASC""",
            {"h": hospital_id, "ini": inicio, "fin": fin})]

    def obtener_ultimos_backups(self, hospital_id, fin):
        # El server guarda cada backup con la fecha en que se hizo: el último de una base
        # puede ser anterior al período y sigue siendo "el último".
        return [dict(r._mapping) for r in self._filas(
            """SELECT s.component_id, s.status_value, s.timestamp
               FROM software_monitoring s
               JOIN (SELECT component_id, MAX(timestamp) AS ts FROM software_monitoring
                     WHERE hospital_id = :h AND app_name = 'sql_backup' AND timestamp <= :fin
                     GROUP BY component_id) u
                 ON u.component_id = s.component_id AND u.ts = s.timestamp
               WHERE s.hospital_id = :h AND s.app_name = 'sql_backup'
               ORDER BY s.component_id""",
            {"h": hospital_id, "fin": fin})]

    def obtener_alertas(self, hospital_id, inicio, fin):
        return [dict(r._mapping) for r in self._filas(
            """SELECT tipo, mensaje, start_time, end_time FROM alertas
               WHERE hospital_id = :h AND start_time <= :fin AND (end_time IS NULL OR end_time >= :ini)
               ORDER BY start_time ASC""",
            {"h": hospital_id, "ini": inicio, "fin": fin})]

    def obtener_estado_componentes(self, hospital_id, app_name, inicio, fin):
        return [dict(r._mapping) for r in self._filas(
            """SELECT s.component_id, s.status_value, s.metric_value, s.timestamp, m.maximo
               FROM software_monitoring s
               JOIN (SELECT component_id, MAX(timestamp) AS ts, MAX(metric_value) AS maximo
                     FROM software_monitoring
                     WHERE hospital_id = :h AND app_name = :a AND timestamp BETWEEN :ini AND :fin
                     GROUP BY component_id) m
                 ON m.component_id = s.component_id AND m.ts = s.timestamp
               WHERE s.hospital_id = :h AND s.app_name = :a
               ORDER BY s.component_id""",
            {"h": hospital_id, "a": app_name, "ini": inicio, "fin": fin})]
