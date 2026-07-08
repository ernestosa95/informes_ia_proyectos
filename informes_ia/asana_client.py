"""
Módulo Asana: trae tickets del proyecto asociado al hospital y los enriquece
con custom fields (equipo resolutor L1/L2, conclusión) para dar contexto
adicional a la IA al momento de redactar el informe.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asana
from dateutil import parser

from .logging_utils import get_logger

log = get_logger(__name__)


def obtener_tareas_asana(
    asana_access_token: str | None, project_id: str, fecha_inicio_str: str
) -> list[str]:
    if not asana_access_token:
        log.info("Sin ASANA_ACCESS_TOKEN configurado, se omite la consulta a Asana.")
        return []

    log.info("Consultando Asana (proyecto %s)...", project_id)
    client = asana.Client.access_token(asana_access_token)
    tareas: list[str] = []

    try:
        try:
            fecha_min = datetime.strptime(fecha_inicio_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            fecha_min = datetime.now() - timedelta(days=30)

        if fecha_min.tzinfo is None:
            fecha_min = fecha_min.replace(tzinfo=timezone.utc)

        tasks = client.tasks.find_by_project(
            project_id,
            opt_fields=[
                "name",
                "created_at",
                "completed_at",
                "completed",
                "modified_at",
                "custom_fields.name",
                "custom_fields.display_value",
            ],
            modified_since=fecha_min.isoformat(),
        )

        for t in tasks:
            created_at = parser.parse(t["created_at"]) if t.get("created_at") else None
            completed_at = parser.parse(t["completed_at"]) if t.get("completed_at") else None

            es_relevante = (
                (created_at and created_at >= fecha_min)
                or (completed_at and completed_at >= fecha_min)
                or not t["completed"]
            )
            if not es_relevante:
                continue

            fecha_mostrar = completed_at if completed_at else created_at
            fecha_str = fecha_mostrar.strftime("%d/%m") if fecha_mostrar else "??"
            estado_icon = "✅" if t["completed"] else "⚠️"

            equipo_responsable = "Sin asignar"
            conclusion_final = ""
            for cf in t.get("custom_fields", []):
                nombre_campo = cf.get("name")
                valor_campo = cf.get("display_value")
                if nombre_campo == "Estado CSAC" and valor_campo:
                    equipo_responsable = valor_campo
                if nombre_campo == "Conclusión" and valor_campo:
                    conclusion_final = f" -> Resolución: {valor_campo}"

            info_ticket = (
                f"Ticket: {t['name']} ({estado_icon} {fecha_str}) "
                f"[{equipo_responsable}]{conclusion_final}"
            )
            tareas.append(info_ticket)

    except Exception:
        log.exception("Error consultando Asana para el proyecto %s", project_id)

    log.info("%d tickets procesados desde Asana", len(tareas))
    return tareas
