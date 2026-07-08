"""
Punto de entrada por CLI.

Uso:
    python -m informes_ia.main --hospital H09 \\
        --desde "2026-01-25 00:00:00" --hasta "2026-02-08 23:59:59" \\
        --tipo cliente
"""
from __future__ import annotations

import argparse
import sys

from .config import ConfigError, get_settings
from .logging_utils import get_logger
from .service import DatosInsuficientes, GeneracionIAFallida, HospitalNoEncontrado, generar_reporte

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Genera un reporte de infraestructura TecnoMonitor")
    parser.add_argument("--hospital", required=True, help="ID del hospital, ej. H09")
    parser.add_argument("--desde", required=True, help="Fecha inicio 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument("--hasta", required=True, help="Fecha fin 'YYYY-MM-DD HH:MM:SS'")
    parser.add_argument(
        "--tipo", choices=["cliente", "interno"], default="cliente", help="Tipo de reporte"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = get_settings()
    except ConfigError as e:
        log.error(str(e))
        return 1

    try:
        resultado = generar_reporte(
            hospital_id=args.hospital,
            fecha_inicio=args.desde,
            fecha_fin=args.hasta,
            tipo_reporte=args.tipo,
            settings=settings,
        )
    except (HospitalNoEncontrado, DatosInsuficientes, GeneracionIAFallida) as e:
        log.error(str(e))
        return 1

    print(f"\n✨ Reporte generado: {resultado.ruta_pdf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
