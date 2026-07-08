"""
Almacén de reportes: la DB propia del módulo (SQLite), separada de
lab_monitor.db. Guarda el ciclo de vida y lo necesario para reconstruir el
PDF on-demand.

Ver docs/07-almacenamiento-y-estados.md para el modelo de datos y el porqué
de cada campo.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..logging_utils import get_logger
from .estados import Estado

log = get_logger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reportes (
    report_id       TEXT PRIMARY KEY,
    estado          TEXT NOT NULL,
    -- para reconstruir el PDF (autosuficiente)
    data_json       TEXT,
    grafico_png     BLOB,
    -- auditoría
    peticion_json   TEXT NOT NULL,
    -- operativos
    error_mensaje   TEXT,
    intentos        INTEGER NOT NULL DEFAULT 0,
    creado_en       TEXT NOT NULL,
    actualizado_en  TEXT NOT NULL
);
"""


@dataclass
class Reporte:
    """Vista tipada de una fila de la tabla reportes."""
    report_id: str
    estado: str
    peticion: dict[str, Any]
    data_json: dict[str, Any] | None
    tiene_grafico: bool
    error_mensaje: str | None
    intentos: int
    creado_en: str
    actualizado_en: str


def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AlmacenReportes:
    """
    Encapsula la DB propia. Cada método abre y cierra su conexión (SQLite,
    barato). El path por defecto es un archivo aparte de lab_monitor.db.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._crear_schema()

    def _conectar(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _crear_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conectar() as conn:
            conn.executescript(_SCHEMA)

    # ── escritura ────────────────────────────────────────────────────────

    def crear(self, peticion: dict[str, Any]) -> str:
        """Crea una fila nueva en estado INICIADO. Devuelve el report_id (UUID)."""
        report_id = str(uuid.uuid4())
        ahora = _ahora()
        with self._conectar() as conn:
            conn.execute(
                """INSERT INTO reportes
                   (report_id, estado, peticion_json, intentos, creado_en, actualizado_en)
                   VALUES (?, ?, ?, 0, ?, ?)""",
                (report_id, Estado.INICIADO.value, json.dumps(peticion, ensure_ascii=False), ahora, ahora),
            )
        log.info("Reporte creado %s (estado=%s)", report_id, Estado.INICIADO.value)
        return report_id

    def set_estado(
        self, report_id: str, estado: Estado, *, error_mensaje: str | None = None
    ) -> None:
        with self._conectar() as conn:
            conn.execute(
                "UPDATE reportes SET estado = ?, error_mensaje = ?, actualizado_en = ? WHERE report_id = ?",
                (estado.value, error_mensaje, _ahora(), report_id),
            )
        log.info("Reporte %s -> estado=%s%s", report_id, estado.value,
                 f" ({error_mensaje})" if error_mensaje else "")

    def incrementar_intentos(self, report_id: str) -> int:
        with self._conectar() as conn:
            conn.execute(
                "UPDATE reportes SET intentos = intentos + 1, actualizado_en = ? WHERE report_id = ?",
                (_ahora(), report_id),
            )
            cur = conn.execute("SELECT intentos FROM reportes WHERE report_id = ?", (report_id,))
            return cur.fetchone()["intentos"]

    def guardar_resultado(
        self, report_id: str, data_json: dict[str, Any], grafico_png: bytes | None
    ) -> None:
        """Guarda el JSON de la IA + el gráfico y pasa a FINALIZADO."""
        with self._conectar() as conn:
            conn.execute(
                """UPDATE reportes
                   SET data_json = ?, grafico_png = ?, estado = ?, error_mensaje = NULL, actualizado_en = ?
                   WHERE report_id = ?""",
                (json.dumps(data_json, ensure_ascii=False), grafico_png,
                 Estado.FINALIZADO.value, _ahora(), report_id),
            )
        log.info("Reporte %s finalizado (grafico=%s)", report_id, "sí" if grafico_png else "no")

    # ── lectura ──────────────────────────────────────────────────────────

    def obtener(self, report_id: str) -> Reporte | None:
        with self._conectar() as conn:
            cur = conn.execute("SELECT * FROM reportes WHERE report_id = ?", (report_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return Reporte(
            report_id=row["report_id"],
            estado=row["estado"],
            peticion=json.loads(row["peticion_json"]),
            data_json=json.loads(row["data_json"]) if row["data_json"] else None,
            tiene_grafico=row["grafico_png"] is not None,
            error_mensaje=row["error_mensaje"],
            intentos=row["intentos"],
            creado_en=row["creado_en"],
            actualizado_en=row["actualizado_en"],
        )

    def obtener_grafico(self, report_id: str) -> bytes | None:
        with self._conectar() as conn:
            cur = conn.execute("SELECT grafico_png FROM reportes WHERE report_id = ?", (report_id,))
            row = cur.fetchone()
        return row["grafico_png"] if row and row["grafico_png"] is not None else None

    def listar(self) -> list[Reporte]:
        with self._conectar() as conn:
            cur = conn.execute("SELECT report_id FROM reportes ORDER BY creado_en DESC")
            ids = [r["report_id"] for r in cur.fetchall()]
        return [self.obtener(i) for i in ids]
