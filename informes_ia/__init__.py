"""
API pública: `from informes_ia import generar_reporte, ...`.

Los nombres se cargan recién al usarlos (PEP 562): importar un submódulo de
datos (db, preprocess, event_tracker...) no arrastra Gemini ni WeasyPrint,
que necesita librerías del sistema. Así esos módulos se pueden testear solos.
"""
from importlib import import_module

__all__ = [
    "generar_reporte",
    "re_renderizar_pdf",
    "ResultadoReporte",
    "HospitalNoEncontrado",
    "DatosInsuficientes",
    "GeneracionIAFallida",
]

__version__ = "2.1.0"


def __getattr__(nombre: str):
    if nombre in __all__:
        return getattr(import_module(".service", __name__), nombre)
    raise AttributeError(f"module {__name__!r} has no attribute {nombre!r}")
