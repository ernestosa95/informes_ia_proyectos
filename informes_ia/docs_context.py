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

import google.generativeai as genai
from google.api_core.exceptions import NotFound

from .logging_utils import get_logger

log = get_logger(__name__)


def _leer_cache(cache_file: Path) -> dict[str, Any]:
    if cache_file.exists():
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _guardar_cache(cache_file: Path, cache: dict[str, Any]) -> None:
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def cargar_contexto_persistente(carpeta_docs: Path, cache_file: Path) -> list[Any]:
    """
    Devuelve la lista de archivos ACTIVE en Gemini listos para adjuntar
    a un prompt, subiendo o re-subiendo lo que haga falta.
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

    for archivo_local in carpeta_docs.glob("*.pdf"):
        nombre = archivo_local.name

        if nombre in cache:
            datos = cache[nombre]
            try:
                remoto = genai.get_file(datos["name"])
                if remoto.state.name == "ACTIVE":
                    archivos_listos.append(remoto)
                    cache_actualizado[nombre] = datos
                    continue
            except NotFound:
                log.warning("Manual '%s' ya no existe en Gemini, se re-sube.", nombre)

        log.info("Subiendo manual: %s", nombre)
        try:
            subido = genai.upload_file(path=archivo_local)
            while subido.state.name == "PROCESSING":
                time.sleep(2)
                subido = genai.get_file(subido.name)
            if subido.state.name == "ACTIVE":
                archivos_listos.append(subido)
                cache_actualizado[nombre] = {"name": subido.name, "uri": subido.uri}
                cambios = True
            else:
                log.error("Manual '%s' terminó en estado %s", nombre, subido.state.name)
        except Exception:
            log.exception("Error subiendo manual '%s'", nombre)

    if cambios:
        _guardar_cache(cache_file, cache_actualizado)

    return archivos_listos
