"""
El pipeline real: las piezas que generan un informe. Lo usan los dos modos:
el asíncrono (historial/servicio.py + worker.py) y el sincrónico
(service.generar_reporte). Es el único lugar que conecta:

    preprocess.generar_resumen()      → pre-procesa la base de TecnoMonitor (fase preparación)
    ai_report.generar_json_con_ia()   → llama a Gemini                       (fase reintentable)
    ai_report.completar_con_cifras()  → cifras y control de calidad desde el código
    db.generar_grafico_historico()    → PNG real
    render.renderizar_pdf_desde_json()→ PDF real

Puntos clave de diseño (ARQUITECTURA.md):
  - La preparación (leer DB, pre-procesar, Asana, manuales) corre UNA vez: va
    en `preparar_contexto`, NO se reintenta.
  - Sólo la llamada a Gemini es reintentable: va en `generador_ia`, que
    recibe el contexto ya preparado.
  - Si Gemini devuelve 429 con un retryDelay, se respeta (FalloRed.retry_delay_s).
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google import genai

from . import ai_report, asana_client, db, docs_context, preprocess, render
from .config import Settings, get_settings
from .config_dinamica import get_configuracion
from .historial.servicio import FalloRed
from .historial.validacion import RespuestaInvalida
from .logging_utils import get_logger

log = get_logger(__name__)


class HospitalInexistente(RespuestaInvalida):
    """El hospital no está en hospitales_metadata. No se reintenta."""


class SinDatos(RespuestaInvalida):
    """Ninguna de las fuentes tiene datos para el período. No se reintenta."""


@dataclass
class ContextoReporte:
    """Todo lo que la llamada a la IA necesita, ya pre-procesado UNA vez."""
    resumen: dict[str, Any]
    asana_txt: dict[str, list[str]]
    hospital_nombre: str
    pdfs: list[Any]
    tipo_reporte: str


class PipelineReal:
    """
    Provee las piezas que ServicioReportes inyecta:
      - preparar_contexto(peticion) -> ContextoReporte   (una vez)
      - generador_ia(contexto)      -> dict              (reintentable)
      - generar_grafico_png(peticion) -> bytes | None
      - render_pdf(data, png)       -> bytes
    """

    def __init__(self, settings: Settings | None = None, client: genai.Client | None = None):
        self.settings = settings or get_settings()
        self.client = client or genai.Client(api_key=self.settings.gemini_api_key)

    # ── FASE 1: preparación (una vez, NO se reintenta) ───────────────────

    def preparar_contexto(self, peticion: dict[str, Any]) -> ContextoReporte:
        hospital_id = peticion["hospital_id"]
        fecha_inicio = peticion["fecha_inicio"]
        fecha_fin = peticion["fecha_fin"]
        tipo_reporte = peticion.get("tipo_reporte", "cliente")
        fuente = self.settings.database_url

        config_hospital = db.obtener_config_hospital(fuente, hospital_id)
        if not config_hospital:
            raise HospitalInexistente(f"hospital '{hospital_id}' no está en hospitales_metadata")

        config_dinamica = get_configuracion(fuente)
        try:
            resumen = preprocess.generar_resumen(
                fuente, hospital_id, fecha_inicio, fecha_fin, config=config_dinamica
            )
        except preprocess.SinDatosParaElPeriodo as e:
            raise SinDatos(f"sin datos: {e}") from e

        asana_txt = asana_client.obtener_tareas_asana(
            self.settings.asana_access_token, config_hospital["asana_id"],
            fecha_inicio, fecha_fin,
            zona_horaria=self.settings.zona_horaria,
            anonimizar=self.settings.anonimizar_asana,
        )
        if not self.settings.asana_access_token:
            resumen.setdefault("control_calidad", []).append(
                "Asana sin configurar: no se incluyeron casos de gestión ni tickets."
            )
        pdfs = docs_context.cargar_contexto_persistente(
            self.client, self.settings.carpeta_docs, self.settings.cache_file
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
        Error de API (incluido el 429 con su retryDelay) -> FalloRed;
        texto que no es JSON -> RespuestaInvalida.
        """
        try:
            data = ai_report.generar_json_con_ia(
                self.client, contexto.resumen, contexto.asana_txt, contexto.hospital_nombre,
                contexto.pdfs, contexto.tipo_reporte,
                model_name=self.settings.gemini_model_json,
            )
        except ai_report.ErrorAPIGemini as e:
            raise FalloRed(str(e), retry_delay_s=e.retry_delay_s) from e
        except ai_report.RespuestaNoJSON as e:
            raise RespuestaInvalida(str(e)) from e

        return ai_report.completar_con_cifras(data, contexto.resumen, contexto.asana_txt)

    # ── gráfico y render ─────────────────────────────────────────────────

    def generar_grafico_png(self, peticion: dict[str, Any]) -> bytes | None:
        """Genera el PNG real y devuelve sus bytes (para guardar como BLOB)."""
        hospital_id = peticion["hospital_id"]
        with tempfile.TemporaryDirectory() as td:
            ruta = Path(td) / "grafico.png"
            resultado = db.generar_grafico_historico(
                self.settings.database_url, hospital_id,
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
