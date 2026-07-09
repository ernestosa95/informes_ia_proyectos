"""
Estados del ciclo de vida de un reporte y políticas de reintento.

Ver docs/07-almacenamiento-y-estados.md. Este módulo es la fuente de verdad
de los nombres de estado y de cuántos reintentos corresponden a cada tipo
de fallo.
"""
from __future__ import annotations

from enum import Enum


class Estado(str, Enum):
    """
    Estados posibles de un reporte. Hereda de str para que se serialice
    directo a la columna TEXT de la DB y se compare con strings sin fricción.
    """
    INICIADO = "iniciado"          # recién creado, aún no encolado
    EN_ESPERA = "en_espera"        # encolado, esperando worker
    EN_PROCESO = "en_proceso"      # leyendo DB, pre-procesando, armando prompt
    REINTENTANDO = "reintentando"  # falló la IA, reintentando según política
    ERROR = "error"                # terminal: agotó reintentos
    FINALIZADO = "finalizado"      # la IA respondió: JSON = BORRADOR editable
    APROBADO = "aprobado"          # terminal: un humano revisó y aprobó

    @property
    def es_terminal(self) -> bool:
        """
        Terminales = no hay más transiciones automáticas ni manuales.
        OJO: `finalizado` NO es terminal: espera revisión humana
        (guardar_json_editado / aprobar). Ver doc 07.
        """
        return self in (Estado.ERROR, Estado.APROBADO)

    @property
    def es_editable(self) -> bool:
        """Sólo un borrador finalizado admite edición del JSON."""
        return self is Estado.FINALIZADO


class TipoFallo(str, Enum):
    """Clasifica por qué falló la llamada a la IA, para elegir la política."""
    RED = "red"                    # no llegó respuesta usable (timeout, rate limit, API caída)
    RESPUESTA_INVALIDA = "respuesta_invalida"  # llegó pero JSON malformado o faltan claves


# Política por tipo de fallo, expresada en INTENTOS TOTALES (no reintentos):
#   RED = 3  →  1 intento inicial + 2 reintentos = 3 llamadas como máximo.
#   RESPUESTA_INVALIDA = 2  →  1 intento inicial + 1 reintento = 2 llamadas.
# (Doc 07: "red = 3 intentos, respuesta inválida = 2 intentos".)
INTENTOS_POR_TIPO: dict[TipoFallo, int] = {
    TipoFallo.RED: 3,
    TipoFallo.RESPUESTA_INVALIDA: 2,
}


def max_intentos(tipo: TipoFallo) -> int:
    return INTENTOS_POR_TIPO[tipo]
