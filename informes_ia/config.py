"""
Configuración central del módulo TecnoMonitor.

Todas las credenciales y rutas se leen de variables de entorno (.env),
nunca hardcodeadas en el código. Ver .env.example en la raíz del proyecto.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Se lanza cuando falta configuración obligatoria."""


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    asana_access_token: str | None
    gemini_model_datos: str
    gemini_model_json: str

    db_path: Path
    cache_file: Path
    carpeta_docs: Path
    carpeta_reportes: Path

    @staticmethod
    def load() -> "Settings":
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            raise ConfigError(
                "Falta GEMINI_API_KEY en el entorno (.env). "
                "No se puede continuar sin esta credencial."
            )

        base_dir = Path(os.getenv("INFORMES_IA_BASE_DIR", ".")).resolve()

        return Settings(
            gemini_api_key=gemini_api_key,
            asana_access_token=os.getenv("ASANA_ACCESS_TOKEN"),  # opcional
            gemini_model_json=os.getenv("GEMINI_MODEL_JSON", "gemini-flash-latest"),
            gemini_model_datos=os.getenv("GEMINI_MODEL_DATOS", "gemini-flash-latest"),
            db_path=base_dir / os.getenv("DB_FILENAME", "lab_monitor.db"),
            cache_file=base_dir / os.getenv("CACHE_FILENAME", "docs_cache.json"),
            carpeta_docs=base_dir / os.getenv("DOCS_DIRNAME", "docs"),
            carpeta_reportes=base_dir / os.getenv("REPORTES_DIRNAME", "reportes"),
        )


def get_settings() -> Settings:
    """Punto de entrada único para obtener configuración validada."""
    return Settings.load()
