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
    # Base de TecnoMonitor de la que se leen los datos: URL de Postgres
    # (DATABASE_URL, como en el server) o, si no está, el archivo SQLite db_path.
    database_url: str
    cache_file: Path
    carpeta_docs: Path
    carpeta_reportes: Path
    # Base propia del módulo (historial de informes, estados, borradores).
    historial_db: Path
    # Zona horaria de las fechas del período y de la base (el server guarda hora local).
    zona_horaria: str
    # Anonimizar los textos de Asana (DNI, mails, teléfonos) antes de mandarlos a Gemini.
    anonimizar_asana: bool

    @staticmethod
    def load() -> "Settings":
        gemini_api_key = os.getenv("GEMINI_API_KEY")
        if not gemini_api_key:
            raise ConfigError(
                "Falta GEMINI_API_KEY en el entorno (.env). "
                "No se puede continuar sin esta credencial."
            )

        base_dir = Path(os.getenv("INFORMES_IA_BASE_DIR", ".")).resolve()
        db_path = base_dir / os.getenv("DB_FILENAME", "lab_monitor.db")

        return Settings(
            gemini_api_key=gemini_api_key,
            asana_access_token=os.getenv("ASANA_ACCESS_TOKEN"),  # opcional
            # Conviene fijar una versión (ej. "gemini-2.5-flash"): con un alias "-latest"
            # el modelo cambia sin aviso y dos informes iguales pueden salir distintos.
            # El modelo que respondió queda en meta.modelo de cada informe.
            gemini_model_json=os.getenv("GEMINI_MODEL_JSON", "gemini-flash-latest"),
            gemini_model_datos=os.getenv("GEMINI_MODEL_DATOS", "gemini-flash-latest"),
            db_path=db_path,
            database_url=os.getenv("DATABASE_URL") or str(db_path),
            cache_file=base_dir / os.getenv("CACHE_FILENAME", "docs_cache.json"),
            carpeta_docs=base_dir / os.getenv("DOCS_DIRNAME", "docs"),
            carpeta_reportes=base_dir / os.getenv("REPORTES_DIRNAME", "reportes"),
            historial_db=base_dir / os.getenv("HISTORIAL_DB_FILENAME", "reportes_db.sqlite"),
            zona_horaria=os.getenv("ZONA_HORARIA", "America/Argentina/Buenos_Aires"),
            anonimizar_asana=os.getenv("ASANA_ANONIMIZAR", "1") != "0",
        )


def get_settings() -> Settings:
    """Punto de entrada único para obtener configuración validada."""
    return Settings.load()
