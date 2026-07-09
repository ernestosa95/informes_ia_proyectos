"""
Adaptador entre el pipeline real y la máquina de estados del historial.

El esqueleto de `historial/servicio.py` es agnóstico de CÓMO se genera el
reporte: recibe piezas inyectadas. Este módulo provee las implementaciones
REALES, conectando:

    preprocess.generar_resumen()      → pre-procesa lab_monitor.db   (fase preparación)
    ai_report.generar_json_con_ia()   → llama a Gemini               (fase reintentable)
    db.generar_grafico_historico()    → PNG real
    render.renderizar_pdf_desde_json()→ PDF real

Puntos clave de diseño (doc 07):
  - La preparación (leer DB, pre-procesar) corre UNA vez: va en
    `preparar_contexto`, NO se reintenta.
  - Sólo la llamada a Gemini es reintentable: va en `generador_ia`, que
    recibe el contexto ya preparado.
  - Si Gemini devuelve 429 con retry_delay, se extrae y se respeta.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import google.generativeai as genai

from . import ai_report, asana_client, db, docs_context, preprocess, render
from .config import Settings, get_settings
from .config_dinamica import get_configuracion
from .historial.servicio import FalloRed
from .historial.validacion import RespuestaInvalida
from .logging_utils import get_logger

log = get_logger(__name__)


@dataclass
class ContextoReporte:
    """Todo lo que la llamada a la IA necesita, ya pre-procesado UNA vez."""
    resumen: dict[str, Any]
    asana_txt: dict[str, list[str]]
    hospital_nombre: str
    pdfs: list[Any]
    tipo_reporte: str


def _extraer_retry_delay(error: Exception) -> float | None:
    """
    Extrae los segundos de 'Please retry in Xs' / 'retry_delay { seconds: X }'
    del error 429 de Gemini, si están presentes.
    """
    txt = str(error)
    m = re.search(r"retry in (\d+(?:\.\d+)?)s", txt)
    if m:
        return float(m.group(1))
    m = re.search(r"seconds:\s*(\d+)", txt)
    if m:
        return float(m.group(1))
    return None


class PipelineReal:
    """
    Provee las piezas que ServicioReportes inyecta:
      - preparar_contexto(peticion) -> ContextoReporte   (una vez)
      - generador_ia(contexto)      -> dict              (reintentable)
      - generar_grafico_png(peticion) -> bytes | None
      - render_pdf(data, png)       -> bytes
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        genai.configure(api_key=self.settings.gemini_api_key)

    # ── FASE 1: preparación (una vez, NO se reintenta) ───────────────────

    def preparar_contexto(self, peticion: dict[str, Any]) -> ContextoReporte:
        hospital_id = peticion["hospital_id"]
        fecha_inicio = peticion["fecha_inicio"]
        fecha_fin = peticion["fecha_fin"]
        tipo_reporte = peticion.get("tipo_reporte", "cliente")

        config_hospital = db.obtener_config_hospital(self.settings.db_path, hospital_id)
        if not config_hospital:
            raise RespuestaInvalida(f"hospital '{hospital_id}' no está en hospitales_metadata")

        config_dinamica = get_configuracion(self.settings.db_path)
        try:
            resumen = preprocess.generar_resumen(
                self.settings.db_path, hospital_id, fecha_inicio, fecha_fin, config=config_dinamica
            )
        except preprocess.SinDatosParaElPeriodo as e:
            raise RespuestaInvalida(f"sin datos: {e}") from e

        asana_txt = asana_client.obtener_tareas_asana(
            self.settings.asana_access_token, config_hospital["asana_id"],
            fecha_inicio, fecha_fin,
        )
        pdfs = docs_context.cargar_contexto_persistente(
            self.settings.carpeta_docs, self.settings.cache_file
        )

        log.info("Contexto preparado para %s (se hace una sola vez)", hospital_id)
        return ContextoReporte(
            resumen=resumen, asana_txt=asana_txt,
            hospital_nombre=config_hospital["nombre"], pdfs=pdfs,
            tipo_reporte=tipo_reporte,
        )

    # ── FASE 2: llamada a la IA (reintentable) ───────────────────────────

    def generador_ia(self, contexto: ContextoReporte) -> dict[str, Any]:
        """
        Sólo la llamada a Gemini. NO re-preprocesa: usa el contexto ya listo.
        Traduce el 429 (rate limit) a FalloRed con su retry_delay.
        """
        try:
            data = ai_report.generar_json_con_ia(
                contexto.resumen, contexto.asana_txt, contexto.hospital_nombre,
                contexto.pdfs, contexto.tipo_reporte,
                model_name=self.settings.gemini_model_json,
            )
        except Exception as e:
            raise FalloRed(f"error llamando a Gemini: {e}",
                           retry_delay_s=_extraer_retry_delay(e)) from e

        if data is None:
            raise FalloRed("Gemini no devolvió un JSON usable (respuesta None)")
        return data

    # ── gráfico y render ─────────────────────────────────────────────────

    def generar_grafico_png(self, peticion: dict[str, Any]) -> bytes | None:
        """Genera el PNG real y devuelve sus bytes (para guardar como BLOB)."""
        hospital_id = peticion["hospital_id"]
        with tempfile.TemporaryDirectory() as td:
            ruta = Path(td) / "grafico.png"
            resultado = db.generar_grafico_historico(
                self.settings.db_path, hospital_id,
                peticion["fecha_inicio"], peticion["fecha_fin"], ruta,
            )
            if resultado is None or not ruta.exists():
                return None
            return ruta.read_bytes()

    def render_pdf(self, data: dict[str, Any], grafico_png: bytes | None) -> bytes:
        """Reconstruye el PDF desde el JSON + el PNG guardado."""
        with tempfile.TemporaryDirectory() as td:
            ruta_grafico = None
            if grafico_png:
                ruta_grafico = Path(td) / "grafico.png"
                ruta_grafico.write_bytes(grafico_png)
            ruta_pdf = Path(td) / "reporte.pdf"
            render.renderizar_pdf_desde_json(data, ruta_grafico, ruta_pdf)
            return ruta_pdf.read_bytes()
