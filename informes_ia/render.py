"""
Módulo 4: Renderizado del PDF final.

Toma el JSON generado por la IA (el "estado") + el gráfico PNG y los
combina con la plantilla Jinja2 para producir el PDF (la "vista").
Separar estado de vista permite re-renderizar el PDF sin volver a
llamar a la IA si sólo cambia el diseño.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import __version__
from .logging_utils import get_logger

log = get_logger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "j2"]),
)

COLORES_POR_TIPO = {
    "cliente": ("#3498db", "INFORME ESTRATÉGICO"),
    "interno": ("#c0392b", "REPORTE TÉCNICO INTERNO"),
}


def renderizar_html(data: dict[str, Any], grafico_uri: str | None = None) -> str:
    """HTML del informe (sin WeasyPrint): sirve para previsualizar y para tests."""
    meta = data.get("meta") or {}
    tipo = meta.get("tipo", "interno")
    color_main, titulo_doc = COLORES_POR_TIPO.get(tipo, COLORES_POR_TIPO["interno"])
    pie = f"Generado por TecnoMonitor · informes_ia {__version__} · {tipo.upper()}"
    if meta.get("modelo"):
        pie += f" · redacción asistida por IA ({meta['modelo']})"
    plantilla = "reporte_grupo.html.j2" if meta.get("alcance") == "grupo" else "reporte.html.j2"
    template = _env.get_template(plantilla)
    return template.render(
        data=data, grafico=grafico_uri, color=color_main, titulo=titulo_doc, pie=pie.replace('"', "'"),
    )


def renderizar_pdf_desde_json(
    data: dict[str, Any], ruta_grafico: Path | None, ruta_salida_pdf: Path
) -> Path:
    log.info("Renderizando PDF -> %s", ruta_salida_pdf)

    from weasyprint import HTML  # acá y no arriba: necesita librerías del sistema

    grafico_uri = None
    if ruta_grafico is not None and Path(ruta_grafico).exists():
        grafico_uri = "file://" + str(Path(ruta_grafico).resolve())

    html_content = renderizar_html(data, grafico_uri)

    ruta_salida_pdf.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_content).write_pdf(ruta_salida_pdf)
    log.info("PDF generado: %s", ruta_salida_pdf)
    return ruta_salida_pdf


def cargar_json(ruta_json: Path) -> dict[str, Any]:
    with open(ruta_json, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_json(data: dict[str, Any], ruta_json: Path) -> Path:
    ruta_json.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return ruta_json
