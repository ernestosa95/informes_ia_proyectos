"""
Subpaquete `historial`: capa de estados, persistencia y flujo asíncrono
del módulo (prototipo).

Ver docs/07-almacenamiento-y-estados.md.
"""
from .almacen import AlmacenReportes, Reporte
from .estados import Estado, TipoFallo
from .servicio import FalloRed, ServicioReportes
from .validacion import RespuestaInvalida, validar_estructura

__all__ = [
    "AlmacenReportes",
    "Reporte",
    "Estado",
    "TipoFallo",
    "ServicioReportes",
    "FalloRed",
    "RespuestaInvalida",
    "validar_estructura",
]
