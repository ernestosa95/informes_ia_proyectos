"""
Módulo 1: Gestión de manuales (contexto RAG persistente).

Sube PDFs de referencia (manuales técnicos) a Gemini Files API y cachea
el resultado en disco para no re-subir en cada corrida. Si un archivo
cacheado ya no está ACTIVE en el lado de Gemini, se re-sube.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import errors as genai_errors

from .logging_utils import get_logger

log = get_logger(__name__)

# Tope de espera mientras Gemini procesa un PDF subido. Antes no había tope y
# un archivo trabado en PROCESSING dejaba el reporte esperando para siempre.
ESPERA_MAX_PROCESSING_S = 120


def _estado(archivo: Any) -> str:
    estado = getattr(archivo, "state", None)
    return str(getattr(estado, "value", estado) or "")


def _leer_cache(cache_file: Path) -> dict[str, Any]:
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _guardar_cache(cache_file: Path, cache: dict[str, Any]) -> None:
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def cargar_contexto_persistente(client: genai.Client, carpeta_docs: Path, cache_file: Path) -> list[Any]:
    """
    Devuelve la lista de archivos ACTIVE en Gemini listos para adjuntar
    a un prompt, subiendo o re-subiendo lo que haga falta. Gemini borra los
    archivos subidos a las 48 h: la caché sólo evita re-subir dentro de ese plazo.
    """
    archivos_listos: list[Any] = []
    cache = _leer_cache(cache_file)

    if not carpeta_docs.exists():
        carpeta_docs.mkdir(parents=True, exist_ok=True)
        log.info("Carpeta de manuales creada vacía en %s", carpeta_docs)
        return archivos_listos

    log.info("Verificando manuales en %s...", carpeta_docs)
    cache_actualizado: dict[str, Any] = {}
    cambios = False

    for archivo_local in sorted(carpeta_docs.glob("*.pdf")):
        nombre = archivo_local.name

        if nombre in cache:
            try:
                remoto = client.files.get(name=cache[nombre]["name"])
                if _estado(remoto) == "ACTIVE":
                    archivos_listos.append(remoto)
                    cache_actualizado[nombre] = cache[nombre]
                    continue
            except genai_errors.ClientError:
                log.warning("Manual '%s' ya no está en Gemini (vencido o borrado), se re-sube.", nombre)

        log.info("Subiendo manual: %s", nombre)
        try:
            subido = client.files.upload(file=archivo_local)
            limite = time.monotonic() + ESPERA_MAX_PROCESSING_S
            while _estado(subido) == "PROCESSING" and time.monotonic() < limite:
                time.sleep(2)
                subido = client.files.get(name=subido.name)
            if _estado(subido) == "ACTIVE":
                archivos_listos.append(subido)
                cache_actualizado[nombre] = {"name": subido.name, "uri": subido.uri}
                cambios = True
            else:
                log.error("Manual '%s' quedó en estado %s; se sigue sin él", nombre, _estado(subido))
        except Exception:
            log.exception("Error subiendo manual '%s'; se sigue sin él", nombre)

    if cambios or set(cache_actualizado) != set(cache):
        _guardar_cache(cache_file, cache_actualizado)

    return archivos_listos
