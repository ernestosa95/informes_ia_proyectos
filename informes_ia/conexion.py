"""
Conexión a la base de TecnoMonitor, en SQLite o en Postgres.

Producción está en Postgres desde el 05/10/2026 (docs/14 del server); el
laboratorio puede seguir con un archivo SQLite. Mismo criterio que
`database.py` del server: si hay `DATABASE_URL` se usa esa URL, si no, el
archivo SQLite.

Todas las funciones de lectura reciben una `fuente`: una URL de SQLAlchemy
(`postgresql://...`, `sqlite:///...`) o la ruta a un archivo SQLite.

Diferencias entre motores que el resto del módulo no tiene que conocer:
  - columnas JSON: Postgres las devuelve ya como dict; SQLite como texto
    (`a_dict()` normaliza las dos).
  - fechas: Postgres devuelve datetime; SQLite, texto (`a_datetime()`).
  - extracción de un campo de un JSON en SQL: `json_escalar()`.
"""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from dateutil import parser as dateparser
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def _url(fuente: str | Path) -> str:
    texto = str(fuente)
    if "://" in texto:
        return texto
    ruta = Path(texto)
    if not ruta.exists():
        raise FileNotFoundError(f"No se encontró la base de datos en {ruta}")
    return f"sqlite:///{ruta.resolve()}"


@lru_cache(maxsize=8)
def _engine_para(url: str) -> Engine:
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})
    # pool_pre_ping: si Postgres se reinició, la conexión vieja se descarta en vez de fallar.
    return create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True)


def get_engine(fuente: str | Path) -> Engine:
    return _engine_para(_url(fuente))


def es_sqlite(engine: Engine) -> bool:
    return engine.dialect.name == "sqlite"


def json_escalar(engine: Engine, columna: str, *ruta: str) -> str:
    """
    Expresión SQL que extrae un escalar de una columna JSON, como número.
    `ruta` son las claves anidadas, ej. ("physical_layer", "telemetry", "ram", "usage_percent").
    Las claves salen del código, nunca del usuario.
    """
    if es_sqlite(engine):
        return f"json_extract({columna}, '$.{'.'.join(ruta)}')"
    flechas = "".join(f"->'{c}'" for c in ruta[:-1]) + f"->>'{ruta[-1]}'"
    return f"NULLIF({columna}{flechas}, '')::float"


def a_dict(valor: Any) -> dict[str, Any] | None:
    """JSON de la base (dict en Postgres, texto en SQLite) -> dict, o None si no se puede."""
    if valor is None:
        return None
    if isinstance(valor, dict):
        return valor
    try:
        resultado = json.loads(valor)
    except (json.JSONDecodeError, TypeError):
        return None
    return resultado if isinstance(resultado, dict) else None


def a_datetime(valor: Any) -> datetime | None:
    if valor is None or isinstance(valor, datetime):
        return valor
    try:
        return dateparser.parse(str(valor))
    except (ValueError, TypeError, OverflowError):
        return None
