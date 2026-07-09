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
    solicitado_por_id   TEXT,        -- id suelto: permite filtrar sin parsear
    solicitado_por      TEXT,        -- objeto completo {id, nombre, rol} en JSON
    aprobado_por_id     TEXT,
    aprobado_por        TEXT,
    aprobado_en         TEXT,
    -- operativos
    error_mensaje   TEXT,
    intentos        INTEGER NOT NULL DEFAULT 0,
    creado_en       TEXT NOT NULL,
    actualizado_en  TEXT NOT NULL
);
"""

# Se crea DESPUÉS de migrar las columnas: en una DB vieja, la columna
# indexada todavía no existe cuando corre el CREATE TABLE IF NOT EXISTS.
_INDICES = """
CREATE INDEX IF NOT EXISTS idx_reportes_solicitante ON reportes(solicitado_por_id);
"""

# Columnas agregadas después de la v1 del schema. Se aplican con ALTER TABLE
# sobre DBs existentes (SQLite no tiene "ADD COLUMN IF NOT EXISTS").
_COLUMNAS_NUEVAS = [
    ("solicitado_por_id", "TEXT"),
    ("solicitado_por", "TEXT"),
    ("aprobado_por_id", "TEXT"),
    ("aprobado_por", "TEXT"),
    ("aprobado_en", "TEXT"),
]


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
    solicitado_por: dict[str, Any] | None = None
    aprobado_por: dict[str, Any] | None = None
    aprobado_en: str | None = None

    @property
    def esta_aprobado(self) -> bool:
        return self.estado == Estado.APROBADO.value


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
            conn.executescript(_SCHEMA)      # crea la tabla si no existe
            self._migrar_columnas(conn)      # agrega columnas nuevas si faltan
            conn.executescript(_INDICES)     # recién ahora existen las columnas

    def _migrar_columnas(self, conn: sqlite3.Connection) -> None:
        """
        Agrega columnas nuevas a DBs creadas con un schema anterior.
        SQLite no soporta ADD COLUMN IF NOT EXISTS, así que se consulta
        primero qué columnas existen.
        """
        existentes = {row[1] for row in conn.execute("PRAGMA table_info(reportes)")}
        for nombre, tipo in _COLUMNAS_NUEVAS:
            if nombre not in existentes:
                conn.execute(f"ALTER TABLE reportes ADD COLUMN {nombre} {tipo}")
                log.info("Migración: columna '%s' agregada a `reportes`", nombre)

    # ── escritura ────────────────────────────────────────────────────────

    def crear(self, peticion: dict[str, Any], solicitado_por: dict[str, Any] | None = None) -> str:
        """
        Crea una fila nueva en estado INICIADO. Devuelve el report_id (UUID).

        solicitado_por: objeto {id, nombre, rol} del usuario que pide el
        informe. El módulo NO autentica: confía en lo que le pasa la app.
        """
        report_id = str(uuid.uuid4())
        ahora = _ahora()
        usuario_id = (solicitado_por or {}).get("id")
        usuario_json = json.dumps(solicitado_por, ensure_ascii=False) if solicitado_por else None
        with self._conectar() as conn:
            conn.execute(
                """INSERT INTO reportes
                   (report_id, estado, peticion_json, solicitado_por_id, solicitado_por,
                    intentos, creado_en, actualizado_en)
                   VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
                (report_id, Estado.INICIADO.value, json.dumps(peticion, ensure_ascii=False),
                 usuario_id, usuario_json, ahora, ahora),
            )
        log.info("Reporte creado %s (estado=%s, solicitante=%s)",
                 report_id, Estado.INICIADO.value, usuario_id or "s/d")
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

    def actualizar_data_json(self, report_id: str, data_json: dict[str, Any]) -> None:
        """
        Reemplaza el data_json (edición humana del borrador). NO cambia el
        estado: se puede llamar N veces mientras el reporte esté FINALIZADO.
        """
        with self._conectar() as conn:
            conn.execute(
                "UPDATE reportes SET data_json = ?, actualizado_en = ? WHERE report_id = ?",
                (json.dumps(data_json, ensure_ascii=False), _ahora(), report_id),
            )
        log.info("Reporte %s: data_json actualizado (edición)", report_id)

    def marcar_aprobado(self, report_id: str, aprobado_por: dict[str, Any] | None = None) -> None:
        """Transición FINALIZADO -> APROBADO, registrando quién aprobó."""
        ahora = _ahora()
        usuario_id = (aprobado_por or {}).get("id")
        usuario_json = json.dumps(aprobado_por, ensure_ascii=False) if aprobado_por else None
        with self._conectar() as conn:
            conn.execute(
                """UPDATE reportes
                   SET estado = ?, aprobado_por_id = ?, aprobado_por = ?,
                       aprobado_en = ?, actualizado_en = ?
                   WHERE report_id = ?""",
                (Estado.APROBADO.value, usuario_id, usuario_json, ahora, ahora, report_id),
            )
        log.info("Reporte %s APROBADO por %s", report_id, usuario_id or "s/d")

    # ── operaciones del worker ───────────────────────────────────────────

    def reclamar_siguiente(self) -> str | None:
        """
        Toma el reporte EN_ESPERA más antiguo y lo pasa a EN_PROCESO de forma
        ATÓMICA. Devuelve su report_id, o None si no hay nada pendiente.

        La atomicidad viene del `WHERE estado = 'en_espera'`: si dos workers
        intentan reclamar el mismo reporte, sólo uno hace rowcount==1. Hoy hay
        un solo worker, pero esto protege gratis si mañana hay varios.
        """
        with self._conectar() as conn:
            cur = conn.execute(
                "SELECT report_id FROM reportes WHERE estado = ? ORDER BY creado_en ASC LIMIT 1",
                (Estado.EN_ESPERA.value,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            report_id = row["report_id"]

            cur = conn.execute(
                """UPDATE reportes SET estado = ?, actualizado_en = ?
                   WHERE report_id = ? AND estado = ?""",
                (Estado.EN_PROCESO.value, _ahora(), report_id, Estado.EN_ESPERA.value),
            )
            if cur.rowcount != 1:
                # otro worker se lo llevó entre el SELECT y el UPDATE
                return None

        log.info("Worker reclamó el reporte %s", report_id)
        return report_id

    def recuperar_huerfanos(self) -> list[str]:
        """
        Marca como ERROR los reportes que quedaron clavados en EN_PROCESO
        (el worker murió mientras los procesaba).

        Se los marca error en vez de reencolarlos porque un reporte a medio
        procesar probablemente ya gastó un request de Gemini, y porque una
        muerte a mitad de proceso es una señal que conviene ver, no esconder.
        Son re-disparables a mano: la petición quedó guardada.
        """
        mensaje = "interrumpido: el worker se reinició mientras procesaba este reporte"
        with self._conectar() as conn:
            cur = conn.execute(
                "SELECT report_id FROM reportes WHERE estado = ?", (Estado.EN_PROCESO.value,)
            )
            ids = [r["report_id"] for r in cur.fetchall()]
            if ids:
                conn.execute(
                    f"""UPDATE reportes SET estado = ?, error_mensaje = ?, actualizado_en = ?
                        WHERE report_id IN ({','.join('?' * len(ids))})""",
                    [Estado.ERROR.value, mensaje, _ahora(), *ids],
                )
        for rid in ids:
            log.warning("Reporte huérfano recuperado -> error: %s", rid)
        return ids

    # ── lectura ──────────────────────────────────────────────────────────

    def obtener(self, report_id: str) -> Reporte | None:
        with self._conectar() as conn:
            cur = conn.execute("SELECT * FROM reportes WHERE report_id = ?", (report_id,))
            row = cur.fetchone()
        if row is None:
            return None

        def _json_col(nombre: str):
            val = row[nombre] if nombre in row.keys() else None
            return json.loads(val) if val else None

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
            solicitado_por=_json_col("solicitado_por"),
            aprobado_por=_json_col("aprobado_por"),
            aprobado_en=row["aprobado_en"] if "aprobado_en" in row.keys() else None,
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

    def listar_por_solicitante(self, usuario_id: str) -> list[Reporte]:
        """Reportes pedidos por un usuario. Usa el índice sobre solicitado_por_id."""
        with self._conectar() as conn:
            cur = conn.execute(
                "SELECT report_id FROM reportes WHERE solicitado_por_id = ? ORDER BY creado_en DESC",
                (usuario_id,),
            )
            ids = [r["report_id"] for r in cur.fetchall()]
        return [self.obtener(i) for i in ids]
