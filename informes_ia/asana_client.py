"""
Módulo Asana: trae los casos (tareas) del proyecto asociado al hospital para
dar contexto a la IA sobre lo que el cliente reportó en el período.

Cada hospital tiene su propio proyecto en Asana (el id vive en
hospitales_metadata.asana_project_id).

Los casos se separan en DOS grupos, porque conviven dos naturalezas:
  - HUMANOS: casos escritos por personas (pedidos, seguimientos, gestión).
    Son el contexto valioso. Se traen completos: título + descripción +
    estado + TODOS los comentarios.
  - AUTOMÁTICOS: tickets autogenerados por TecnoMonitor (incidentes de
    infra que el propio sistema abre y cierra). Duplican lo que el
    pre-procesamiento ya detecta desde la telemetría, con menos precisión.
    Se traen sólo como título + estado (sin descripción ni comentarios),
    para que la IA sepa que existieron sin gastar tokens ni confundirlos
    con reclamos del cliente.

Un ticket es AUTOMÁTICO si la firma fija que pone el sistema
(FIRMA_AUTOMATICO) está al comienzo de su descripción. No se exige que sea
la primera línea: las tareas de continuación (cuando una alerta llega al tope
de comentarios, asana_conector.py del server) arrancan con "🔁 CONTINUACIÓN"
y recién después la firma. Todo lo demás es humano.

Fechas: el período llega en hora local (la de la base); se convierte con la
zona horaria configurada antes de comparar con Asana, que trabaja en UTC.
Antes se tomaba como UTC y el filtro quedaba corrido 3 horas.

Privacidad: los textos humanos (descripción y comentarios) se anonimizan
antes de mandarlos a Gemini: DNI, CUIL/CUIT, mails y teléfonos se reemplazan
por marcas. Los nombres propios no se pueden detectar de forma confiable:
quien escribe en Asana no debería poner datos de pacientes.

Sólo se incluyen tareas creadas o modificadas dentro del rango del reporte.
Tope de tareas MAX_TAREAS; si hay más, se avisa.

Escrito para la librería `asana` 5.x (Configuration + ApiClient + *Api).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import asana
from asana.rest import ApiException
from dateutil import parser

from .logging_utils import get_logger

log = get_logger(__name__)

# Tope de tareas a procesar por reporte.
MAX_TAREAS = 50

# Firma fija que TecnoMonitor pone al inicio de la descripción de los
# tickets que genera automáticamente. Es la señal para clasificar.
FIRMA_AUTOMATICO = "INCIDENTE DETECTADO - TECNOMONITOR"
# Hasta dónde se busca la firma (la línea de continuación ocupa ~250 caracteres).
_ALCANCE_FIRMA = 600

_PATRONES_ANONIMIZAR = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\b(?:20|23|24|27|30|33|34)-?\d{8}-?\d\b"), "[cuil]"),
    # Teléfonos: característica + número ("0261 4123456", "(011) 4123-4567", "+54 9 261 412-3456").
    (re.compile(r"(?<![\w/])(?:\+?54[\s-]?)?(?:9[\s-]?)?\(?0?\d{2,4}\)?[\s-]?\d{2,4}[\s-]?\d{4}(?![\w/:])"), "[telefono]"),
    (re.compile(r"\b\d{1,2}\.\d{3}\.\d{3}\b|\b\d{7,8}\b"), "[dni]"),
]


def anonimizar(texto: str) -> str:
    """Reemplaza DNI, CUIL/CUIT, mails y teléfonos por marcas."""
    for patron, marca in _PATRONES_ANONIMIZAR:
        texto = patron.sub(marca, texto)
    return texto


def _parse_fecha(fecha_str: str, zona: ZoneInfo, default_dias_atras: int = 30) -> datetime:
    """Fecha local del período -> datetime con zona (Asana compara en UTC)."""
    try:
        dt = datetime.strptime(fecha_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        dt = datetime.now() - timedelta(days=default_dias_atras)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=zona)
    return dt.astimezone(timezone.utc)


def _crear_clientes(token: str):
    """Arma los clientes de la API 5.x a partir del token."""
    cfg = asana.Configuration()
    cfg.access_token = token
    api_client = asana.ApiClient(cfg)
    return asana.TasksApi(api_client), asana.StoriesApi(api_client)


def _en_rango(t: dict, fecha_min: datetime, fecha_max: datetime) -> bool:
    """Una tarea es relevante si se creó, modificó o completó dentro del rango."""
    for clave in ("created_at", "modified_at", "completed_at"):
        val = t.get(clave)
        if not val:
            continue
        try:
            dt = parser.parse(val) if isinstance(val, str) else val
        except (ValueError, TypeError):
            continue
        if fecha_min <= dt <= fecha_max:
            return True
    return False


def _fmt_fecha(val) -> str:
    if not val:
        return "s/f"
    try:
        dt = parser.parse(val) if isinstance(val, str) else val
        return dt.strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return "s/f"


def _es_automatico(t: dict) -> bool:
    """True si la firma fija de TecnoMonitor está al comienzo de la descripción."""
    notes = (t.get("notes") or "").strip()
    return FIRMA_AUTOMATICO in notes[:_ALCANCE_FIRMA]


def _estado_str(t: dict) -> str:
    completado = t.get("completed", False)
    fecha_ref = t.get("completed_at") if completado else t.get("created_at")
    fecha_str = _fmt_fecha(fecha_ref)
    return f"Completada {fecha_str}" if completado else f"Abierta (creada {fecha_str})"


def _obtener_comentarios(stories_api, task_gid: str) -> list[str]:
    """
    Trae los comentarios (stories tipo 'comment') de una tarea. Una llamada
    por tarea. Si falla, devuelve lista vacía (no corta el proceso).
    """
    comentarios: list[str] = []
    try:
        stories = stories_api.get_stories_for_task(
            task_gid, opts={"opt_fields": "type,text,created_at"}
        )
        for s in stories:
            if s.get("type") == "comment" and s.get("text"):
                comentarios.append(s["text"].strip())
    except ApiException:
        log.warning("No se pudieron traer comentarios de la tarea %s", task_gid)
    return comentarios


def _formatear_humano(t: dict, stories_api, anonimizar_texto: bool = True) -> str:
    """Caso humano: completo, con descripción y comentarios (anonimizados)."""
    limpiar = anonimizar if anonimizar_texto else (lambda x: x)
    partes = [f"CASO: {limpiar(t.get('name', '(sin titulo)'))}", f"Estado: {_estado_str(t)}"]
    descripcion = (t.get("notes") or "").strip()
    if descripcion:
        partes.append(f"Descripcion: {limpiar(descripcion)}")
    comentarios = _obtener_comentarios(stories_api, t["gid"])
    if comentarios:
        partes.append("Comentarios:")
        partes.extend(f"  - {limpiar(c)}" for c in comentarios)
    return "\n".join(partes)


def _formatear_automatico(t: dict) -> str:
    """Caso automático: sólo título + estado (sin comentarios, ahorra tokens/llamadas)."""
    return f"{t.get('name', '(sin titulo)')} — {_estado_str(t)}"


def obtener_tareas_asana(
    asana_access_token: str | None,
    project_id: str,
    fecha_inicio_str: str,
    fecha_fin_str: str | None = None,
    zona_horaria: str = "America/Argentina/Buenos_Aires",
    anonimizar: bool = True,
) -> dict[str, Any]:
    """
    Devuelve un dict con dos grupos de casos del período:

        {
          "humanos":     [str, ...],   # completos (desc + comentarios)
          "automaticos": [str, ...],   # sólo título + estado
        }

    Si no hay token, devuelve ambas listas vacías.
    """
    vacio = {"humanos": [], "automaticos": []}

    if not asana_access_token:
        log.info("Sin ASANA_ACCESS_TOKEN configurado, se omite la consulta a Asana.")
        return vacio

    log.info("Consultando Asana (proyecto %s)...", project_id)
    tasks_api, stories_api = _crear_clientes(asana_access_token)

    zona = ZoneInfo(zona_horaria)
    fecha_min = _parse_fecha(fecha_inicio_str, zona)
    fecha_max = _parse_fecha(fecha_fin_str, zona) if fecha_fin_str else datetime.now(timezone.utc)

    humanos: list[str] = []
    automaticos: list[str] = []

    try:
        resultado = tasks_api.get_tasks_for_project(
            project_id,
            opts={
                "opt_fields": "name,notes,completed,created_at,modified_at,completed_at",
                "modified_since": fecha_min.isoformat(),
            },
        )
        todas = list(resultado)
        relevantes = [t for t in todas if _en_rango(t, fecha_min, fecha_max)]
        total = len(relevantes)

        if total > MAX_TAREAS:
            log.warning(
                "El proyecto %s tiene %d tareas en el rango; se procesan las primeras %d.",
                project_id, total, MAX_TAREAS,
            )
            relevantes = relevantes[:MAX_TAREAS]

        for t in relevantes:
            if _es_automatico(t):
                # Sólo título + estado. NO se piden comentarios (ahorra llamadas).
                automaticos.append(_formatear_automatico(t))
            else:
                # Humano: completo, con comentarios (llamada extra por tarea).
                humanos.append(_formatear_humano(t, stories_api, anonimizar))

        if total > MAX_TAREAS:
            humanos.append(f"[NOTA: habia {total} casos en el periodo; se incluyeron los primeros {MAX_TAREAS}.]")

    except ApiException as e:
        log.exception("Error consultando Asana para el proyecto %s (status %s)", project_id, getattr(e, "status", "?"))
    except Exception:
        log.exception("Error inesperado consultando Asana para el proyecto %s", project_id)

    log.info("Asana: %d casos humanos, %d automáticos", len(humanos), len(automaticos))
    return {"humanos": humanos, "automaticos": automaticos}
