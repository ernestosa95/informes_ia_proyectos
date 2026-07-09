"""
Prueba AISLADA de la integración con Asana.

Llama sólo a obtener_tareas_asana() y muestra lo que trae. NO toca Gemini,
NO genera PDF, NO gasta cuota de IA. Sirve para depurar la conexión con
Asana sin correr el flujo completo.

Uso:
  python -m informes_ia.prueba_asana --hospital P03 \\
      --desde "2026-01-06 00:00:00" --hasta "2026-07-08 00:00:00"
"""
from __future__ import annotations

import argparse
import sys

from . import asana_client, db
from .config import ConfigError, get_settings
from .logging_utils import get_logger

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Prueba aislada de Asana (sin Gemini)")
    p.add_argument("--hospital", required=True)
    p.add_argument("--desde", required=True, help="'YYYY-MM-DD HH:MM:SS'")
    p.add_argument("--hasta", required=True, help="'YYYY-MM-DD HH:MM:SS'")
    args = p.parse_args(argv)

    try:
        settings = get_settings()
    except ConfigError as e:
        log.error("Config: %s", e)
        return 1

    if not settings.asana_access_token:
        print("\n⚠️  No hay ASANA_ACCESS_TOKEN en el .env. Agregalo y reintentá.\n")
        return 1

    # Resolver el project_id del hospital desde hospitales_metadata
    config_hospital = db.obtener_config_hospital(settings.db_path, args.hospital)
    if not config_hospital:
        print(f"\n⚠️  Hospital '{args.hospital}' no está en hospitales_metadata.\n")
        return 1

    project_id = config_hospital["asana_id"]
    print(f"\n{'='*70}\nPRUEBA AISLADA DE ASANA\n{'='*70}")
    print(f"  hospital    : {args.hospital} ({config_hospital['nombre']})")
    print(f"  proyecto    : {project_id}")
    print(f"  rango       : {args.desde}  →  {args.hasta}\n")

    casos = asana_client.obtener_tareas_asana(
        settings.asana_access_token, project_id, args.desde, args.hasta
    )

    humanos = casos.get("humanos", [])
    automaticos = casos.get("automaticos", [])

    print(f"\n{'─'*70}\nCASOS HUMANOS (gestión): {len(humanos)}\n{'─'*70}")
    for i, t in enumerate(humanos, 1):
        print(f"\n[H{i}] {'-'*58}")
        print(t)

    print(f"\n{'─'*70}\nINCIDENTES AUTOMÁTICOS (TecnoMonitor): {len(automaticos)}\n{'─'*70}")
    for i, t in enumerate(automaticos, 1):
        print(f"  [A{i}] {t}")

    if not humanos and not automaticos:
        print("\n(No se trajeron casos. Puede ser correcto — quizá no hay tareas")
        print(" en ese rango — o revisá el proyecto/token si esperabas resultados.)")

    print(f"\n{'='*70}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
