"""
Validación de la respuesta de la IA (doc 06 > "Validación de la respuesta
de la IA" y doc 07 > "los tres desenlaces").

No alcanza con que la respuesta parsee: debe tener las claves pedidas.
`meta` y `resumen` son siempre obligatorias; las demás dependen de las
secciones que se pidió mostrar.
"""
from __future__ import annotations

from typing import Any

# Secciones que SIEMPRE deben estar (inocultables, doc 06).
CLAVES_OBLIGATORIAS = {"meta", "resumen"}


class RespuestaInvalida(Exception):
    """El JSON parseó pero no cumple el contrato de claves."""


def claves_esperadas(secciones_a_mostrar: set[str]) -> set[str]:
    """Las claves que el molde pidió: obligatorias + las secciones visibles."""
    return CLAVES_OBLIGATORIAS | set(secciones_a_mostrar)


def validar_estructura(data: Any, secciones_a_mostrar: set[str]) -> None:
    """
    Lanza RespuestaInvalida si `data` no es un dict con todas las claves
    esperadas. No valida el contenido interno de cada sección (eso lo
    tolera el render), sólo la presencia de las claves de primer nivel.
    """
    if not isinstance(data, dict):
        raise RespuestaInvalida(f"La respuesta no es un objeto JSON (es {type(data).__name__})")

    esperadas = claves_esperadas(secciones_a_mostrar)
    faltantes = esperadas - set(data.keys())
    if faltantes:
        raise RespuestaInvalida(f"faltan claves: {', '.join(sorted(faltantes))}")
