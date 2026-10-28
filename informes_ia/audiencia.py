"""
Política por PÚBLICO del informe: qué datos ve la IA, cómo se le explican y con
qué vocabulario tiene que escribir. Todo lo que distingue un informe para el
CLIENTE de uno INTERNO vive acá (decisión del 2026-10-28).

    cliente   Dirección del hospital / ministerio / obra social. Se informa el valor
              del servicio: disponibilidad, actividad, incidentes detectados y
              gestionados, recomendaciones. NO se muestran debilidades operativas
              (demoras, colas, borradores pendientes, huecos de monitoreo, errores
              de integración, tickets internos) ni identificadores técnicos. Los
              riesgos del hospital se presentan como recomendaciones.

    interno   Equipo de TecnoMonitor. Todo el detalle técnico, con identificadores,
              duraciones y causas cuando estén en los datos, y una lista de puntos a
              revisar con prioridad y acción.

La regla de no mostrar se aplica en dos lugares, porque la IA puede equivocarse:
  1. la VISTA: los datos que el cliente no debe ver no se le pasan a la IA;
  2. el CONTROL de vocabulario sobre el texto generado: si igual aparece un
     término de la lista, queda un aviso en `control_calidad` del borrador para
     quien lo revisa (el PDF del cliente nunca muestra ese aviso).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from .conexion import a_datetime

CLIENTE = "cliente"
INTERNO = "interno"

# ── eventos: nombre técnico y grupo que ve el cliente ───────────────────────

# Un evento que termina a menos de 10 min del fin del período sigue abierto al cierre.
_TOLERANCIA_CIERRE = timedelta(minutes=10)

CATEGORIAS_EVENTO: dict[str, tuple[str, str]] = {
    # tipo (event_tracker)     nombre interno                         grupo para el cliente
    "nodo_offline":          ("VM o equipo fuera de línea",           "servidores y equipos"),
    "servicio_detenido":     ("servicio de Windows detenido",         "servidores y equipos"),
    "fuente_poder_fallo":    ("fuente de energía con falla",          "infraestructura física"),
    "ventilador_fallo":      ("ventilador con falla",                 "infraestructura física"),
    "raid_degradado":        ("RAID / disco físico degradado",        "infraestructura física"),
    "temp_cpu_alta":         ("temperatura de CPU alta",              "infraestructura física"),
    "temp_ambiente_alta":    ("temperatura ambiente alta",            "infraestructura física"),
    "disco_lleno":           ("disco de VM por encima del umbral",    "capacidad"),
    "cpu_host_alto":         ("CPU del host alta",                    "rendimiento"),
    "ram_host_alto":         ("RAM del host alta",                    "rendimiento"),
    "cpu_vm_alto":           ("CPU de VM alta",                       "rendimiento"),
    "ram_vm_alto":           ("RAM de VM alta",                       "rendimiento"),
}


def resumir_eventos(eventos: list[dict[str, Any]], fin: Any, detalle_max: int = 25) -> dict[str, Any]:
    """
    Eventos agrupados por tipo (cantidad, minutos totales, abiertos al cierre, los más
    largos) en vez de la lista cruda, que en un mes puede tener cientos de entradas.
    """
    fin_dt = a_datetime(fin)
    por_tipo: dict[str, dict[str, Any]] = {}
    for e in eventos:
        t = e.get("tipo", "?")
        g = por_tipo.setdefault(t, {"tipo": t, "descripcion": CATEGORIAS_EVENTO.get(t, (t, ""))[0],
                                    "cantidad": 0, "minutos_totales": 0.0, "abiertos_al_cierre": 0,
                                    "entidades": set(), "_lista": []})
        g["cantidad"] += 1
        g["minutos_totales"] += e.get("duracion_min") or 0
        g["entidades"].add(e.get("entidad"))
        fin_e = a_datetime(e.get("fin"))
        if fin_dt is not None and fin_e is not None and fin_e >= fin_dt - _TOLERANCIA_CIERRE:
            g["abiertos_al_cierre"] += 1
        g["_lista"].append(e)
    salida = []
    for g in sorted(por_tipo.values(), key=lambda x: -x["minutos_totales"]):
        mas_largos = sorted(g.pop("_lista"), key=lambda e: -(e.get("duracion_min") or 0))[:3]
        g["entidades"] = sorted(x for x in g["entidades"] if x)
        g["minutos_totales"] = round(g["minutos_totales"])
        g["mas_largos"] = [{"entidad": e.get("entidad"), "inicio": e.get("inicio"),
                            "duracion_min": e.get("duracion_min"), "detalle": e.get("detalle")} for e in mas_largos]
        salida.append(g)
    detalle = sorted(eventos, key=lambda e: -(e.get("duracion_min") or 0))[:detalle_max]
    return {"total": len(eventos), "por_tipo": salida, "mas_largos": detalle}



# Lo que el cliente NO ve como "incidente": los picos de uso (CPU/RAM). En las APPV el SQL Server
# usa casi toda la RAM a propósito, así que contarlos daba cientos de "incidentes" que nadie atendió
# (H05, 28/10: 480 de 496). Siguen en el informe interno.
GRUPOS_NO_CLIENTE = {"rendimiento"}


def eventos_cliente(eventos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in eventos
            if CATEGORIAS_EVENTO.get(e.get("tipo", ""), ("", ""))[1] not in GRUPOS_NO_CLIENTE]


def _eventos_para_cliente(eventos: list[dict[str, Any]]) -> dict[str, Any]:
    """Sólo cantidades por grupo, sin entidades técnicas ni duraciones."""
    grupos: dict[str, int] = {}
    for e in eventos_cliente(eventos):
        grupo = CATEGORIAS_EVENTO.get(e.get("tipo", ""), ("", "otros eventos técnicos"))[1] or "otros eventos técnicos"
        grupos[grupo] = grupos.get(grupo, 0) + 1
    return {"total_detectados": sum(grupos.values()), "por_area": grupos}


# ── diccionario de datos (para que la IA entienda cada campo) ────────────────

DICCIONARIO_COMUN = """
DICCIONARIO DE DATOS (cómo leer el resumen):
- periodo: fechas del informe y su duración en días.
- disponibilidad / uptime_pct: % del período en que los sistemas del hospital estuvieron
  en línea y reportando. 100 % = sin interrupciones. Por debajo de 99 % es relevante.
- comparacion: las mismas cifras del período anterior de igual duración. Usala para hablar
  de tendencia (mejoró / empeoró / se mantuvo) SOLO si la diferencia es clara.
- actividad_clinica: volúmenes del RIS/PACS del período (estudios totales, ejecutados,
  con imagen, informados como definitivos), por modalidad (CR, CT, MR, US, MG...).
"""

DICCIONARIO_INTERNO = """
- eventos.por_tipo: EPISODIOS detectados sobre la telemetría (cortes separados por pocos minutos ya
  vienen unidos en un solo episodio; "cortes_unidos" dice cuántos), agrupados:
  cantidad, minutos totales, abiertos_al_cierre (siguen sin resolverse al terminar el
  período) y los más largos con su entidad (VM, disco, sensor) y detalle.
- huecos_sin_reportes: intervalos sin datos del agente (> offline_minutes). Pueden ser una
  caída real o el agente sin enviar: no lo afirmes sin otro dato que lo confirme.
- disponibilidad_vms_pct: % de reportes con cada VM/equipo en línea.
- software.canales_mirth: canales de integración HL7 con problema (estado distinto de
  STARTED, cola máxima sobre el umbral o errores en el período).
- software.certificados_ssl: con_alerta = vencen dentro del umbral de días;
  no_se_pudo_verificar = el agente no pudo conectarse a revisarlos (red, DNS, puerto, con el
  motivo). Eso NO significa que estén vencidos: es un problema de verificación a revisar.
- software.backups_sql: bases sin backup completo dentro del umbral de horas.
- software.integridad_bases_checkdb: bases con errores de DBCC CHECKDB.
- software.autoenrute_dicom: reglas de envío DICOM con cola (estudios esperando envío).
- software.portal_paciente: estudios por estado de publicación al portal.
- actividad_clinica.backlog_radiologia: borradores pendientes al cierre (aproximación).
- control_calidad: datos faltantes o dudosos. Mencionalos en puntos_a_revisar.
- casos_asana: pedidos y seguimientos escritos por personas (contexto de gestión).
"""

DICCIONARIO_CLIENTE = """
- incidentes.total_detectados: eventos que el monitoreo preventivo detectó en el período, por
  área (infraestructura física, servidores y equipos, capacidad). Es una detección automática:
  NO todos requirieron una intervención, así que no digas que fueron "gestionados",
  "resueltos" ni "atendidos" uno por uno.
- recomendaciones_sugeridas: puntos que conviene que el hospital atienda; redactalos como
  recomendaciones constructivas.
- casos_de_gestion: títulos de los pedidos del hospital atendidos en el período.
- datos_no_disponibles: áreas sin datos en el período: no digas nada sobre ellas.
"""

# ── vocabulario ──────────────────────────────────────────────────────────────

VOCABULARIO = {
    CLIENTE: """
VOCABULARIO PARA EL CLIENTE (obligatorio):
- Escribí para una dirección de hospital, sin jerga técnica: "servidores", "sistemas
  clínicos", "integraciones", "resguardo de datos", "seguridad", "equipamiento".
- NO menciones: demoras, atrasos, colas, pendientes, borradores, backlog, tiempos de
  espera, huecos o cortes del monitoreo, el agente, tickets internos, errores de software
  propio, ni nombres técnicos (VMs como ARP19APPV, MSSQLSERVER, Mirth, iDRAC, Logstash,
  Elastic, CHECKDB, RAID, DICOM, HL7).
- Los incidentes se cuentan como "detectados por el monitoreo preventivo". Lo que el servicio
  atendió son los casos_de_gestion (pedidos del hospital): de esos sí podés decir que se atendieron.
- No exageres ni agregues efectos que no están en los datos ("garantizando la continuidad",
  "sin comprometer..."): describí lo que pasó.
- Los riesgos del hospital (backups, certificados, equipamiento) van SOLO como
  recomendaciones, con tono constructivo, nunca como fallas.
- No prometas niveles de servicio ni culpes a terceros.
""",
    INTERNO: """
VOCABULARIO INTERNO:
- Técnico y directo. Usá los identificadores (VM, servicio, canal, base, regla) y las
  duraciones tal como figuran en los datos.
- Separá hechos (lo que dicen los datos) de hipótesis (marcalas como "posible causa").
- Señalá explícitamente lo que hay que revisar o mejorar, aunque sea responsabilidad nuestra
  (monitoreo, agente, configuración, demoras de atención).
""",
}

# Términos que no pueden aparecer en un texto para el cliente (se avisa al revisor).
_PROHIBIDOS_CLIENTE = [
    r"demor\w*", r"atras\w*", r"\bcolas?\b", r"\bpendientes?\b", r"borrador\w*", r"backlog",
    r"tiempos? de espera", r"\bhuecos?\b", r"\bagente\b", r"\btickets?\b", r"\bmirth\b", r"\bidrac\b",
    r"logstash", r"elastic\w*", r"checkdb", r"\braid\b", r"\bdicom\b", r"\bhl7\b", r"mssqlserver",
    r"\bAR[A-Z]{0,2}\d{1,3}[A-Z]{2,5}V\b", r"\bsin monitoreo\b", r"\bfalla\w* (?:del|de nuestro) (?:servicio|monitoreo)",
]
_REGEX_CLIENTE = re.compile("|".join(_PROHIBIDOS_CLIENTE), re.IGNORECASE)

# Campos de texto de la IA que se revisan.
_CAMPOS_TEXTO = [
    ("resumen", "texto"), ("infraestructura", "energia"), ("infraestructura", "termica"),
    ("infraestructura", "mensaje"), ("incidencias", "analisis"), ("calidad", "estabilidad"),
    ("calidad", "caso_destacado"), ("recomendacion",), ("situacion_general",), ("hospitales_atencion",),
    ("puntos_a_revisar",),
]


def _texto(data: dict[str, Any], ruta: tuple[str, ...]) -> str:
    cur: Any = data
    for k in ruta:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(k)
    return cur if isinstance(cur, str) else ""


def revisar_vocabulario(data: dict[str, Any], audiencia: str) -> list[str]:
    """Avisos para el revisor del borrador (van a control_calidad, nunca al PDF del cliente)."""
    avisos: list[str] = []
    if audiencia == CLIENTE:
        for ruta in _CAMPOS_TEXTO:
            encontrados = sorted({m.group(0).lower() for m in _REGEX_CLIENTE.finditer(_texto(data, ruta))})
            if encontrados:
                avisos.append(f"Texto para el cliente ({'.'.join(ruta)}) menciona: {', '.join(encontrados)}. "
                              "Revisar antes de aprobar.")
    elif not _texto(data, ("puntos_a_revisar",)).strip():
        avisos.append("El informe interno no trae puntos a revisar: completarlos o confirmar que no hay.")
    return avisos


# ── vistas: lo que recibe la IA según el público ─────────────────────────────

def _periodo(resumen: dict[str, Any]) -> dict[str, Any]:
    p = dict(resumen.get("periodo") or {})
    d0, d1 = a_datetime(p.get("desde")), a_datetime(p.get("hasta"))
    if d0 and d1:
        p["dias"] = round((min(d1, datetime.now()) - d0).total_seconds() / 86400, 1)
    return p


def _actividad_cliente(clinico: dict[str, Any] | None) -> dict[str, Any] | None:
    if not clinico:
        return None
    return {
        "por_modalidad": {
            mod: {k: v for k, v in m.items() if k in ("totales", "ejecutados", "con_imagen", "definitivos")}
            for mod, m in (clinico.get("actividad_por_modalidad") or {}).items()
        },
        "almacenamiento_pacs_por_equipo": clinico.get("almacenamiento_pacs_por_aet"),
    }


def _recomendaciones_cliente_individual(resumen: dict[str, Any]) -> list[str]:
    sw = resumen.get("software") or {}
    infra = resumen.get("infraestructura") or {}
    rec = []
    if (sw.get("backups_sql") or {}).get("con_alerta"):
        rec.append("reforzar la política de resguardo (backups) de las bases de datos")
    if (sw.get("integridad_bases_checkdb") or {}).get("con_problema"):
        rec.append("programar una revisión preventiva de integridad de las bases de datos")
    if (sw.get("certificados_ssl") or {}).get("con_alerta"):
        rec.append("renovar los certificados de seguridad próximos a vencer")
    tipos = {e.get("tipo") for e in infra.get("eventos") or []}
    if "disco_lleno" in tipos:
        rec.append("revisar junto al servicio el uso del almacenamiento de los servidores")
    if tipos & {"fuente_poder_fallo", "ventilador_fallo", "raid_degradado"}:
        rec.append("evaluar el estado del equipamiento con eventos de hardware detectados")
    if tipos & {"temp_cpu_alta", "temp_ambiente_alta"}:
        rec.append("revisar la climatización de la sala de servidores")
    return rec


def _no_disponibles(resumen: dict[str, Any]) -> list[str]:
    """Áreas sin datos, en lenguaje neutro (para que la IA no afirme nada sobre ellas)."""
    faltan = []
    con_dato = (resumen.get("infraestructura") or {}).get("reportes_con_dato") or {}
    nombres = {"energia": "energía", "temperatura": "temperatura", "raid": "almacenamiento físico"}
    if resumen.get("infraestructura"):
        faltan += [n for clave, n in nombres.items() if not con_dato.get(clave)]
    if not resumen.get("actividad_clinica"):
        faltan.append("actividad clínica")
    return faltan


def vista_individual(resumen: dict[str, Any], asana: dict[str, list[str]], audiencia: str,
                     hospital: dict[str, Any]) -> dict[str, Any]:
    infra = resumen.get("infraestructura") or {}
    eventos = infra.get("eventos") or []
    base = {
        "hospital": hospital,
        "periodo": _periodo(resumen),
        "disponibilidad_pct": infra.get("uptime_pct"),
        "comparacion": resumen.get("comparacion"),
    }
    if audiencia == CLIENTE:
        return {
            **base,
            "incidentes": _eventos_para_cliente(eventos),
            "rendimiento_promedio": {k: (v or {}).get("avg") for k, v in (infra.get("metricas") or {}).items()
                                     if k in ("cpu_host", "ram_host")},
            "actividad_clinica": _actividad_cliente(resumen.get("actividad_clinica")),
            "recomendaciones_sugeridas": _recomendaciones_cliente_individual(resumen),
            "casos_de_gestion": [c.splitlines()[0].replace("CASO: ", "") for c in asana.get("humanos", [])
                                 if c and not c.startswith("[NOTA")],
            "datos_no_disponibles": _no_disponibles(resumen),
        }
    return {
        **base,
        "huecos_sin_reportes": infra.get("huecos_sin_reportes"),
        "disponibilidad_vms_pct": infra.get("disponibilidad_vms_pct"),
        "metricas": infra.get("metricas"),
        "alertas_suitestensa_pico": infra.get("alertas_suitestensa_pico"),
        "eventos": resumir_eventos(eventos, (resumen.get("periodo") or {}).get("hasta")),
        "software": resumen.get("software"),
        "actividad_clinica": resumen.get("actividad_clinica"),
        "casos_asana": {"humanos": asana.get("humanos", []),
                        "automaticos_cantidad": len(asana.get("automaticos", []))},
        "control_calidad": resumen.get("control_calidad") or [],
    }


def vista_grupo(resumen: dict[str, Any], audiencia: str) -> dict[str, Any]:
    base = {"grupo": resumen.get("grupo"), "periodo": _periodo(resumen), "comparacion": resumen.get("comparacion")}
    ag = resumen.get("agregados") or {}
    if audiencia == CLIENTE:
        return {
            **base,
            "agregados": {k: ag.get(k) for k in ("hospitales", "uptime_promedio_pct", "incidentes_totales",
                                                  "estudios_totales")},
            "hospitales": [
                {"hospital_id": f.get("hospital_id"), "nombre": f.get("nombre"),
                 "disponibilidad_pct": f.get("uptime_pct"),
                 "alertas_detectadas": f.get("incidentes"),
                 "estudios_totales": f.get("estudios_totales"),
                 "recomendaciones_sugeridas": f.get("recomendaciones_cliente") or []}
                for f in resumen.get("hospitales") or []
            ],
        }
    return {**base, "agregados": ag, "hospitales": resumen.get("hospitales"),
            "hospitales_que_requieren_atencion": resumen.get("hospitales_que_requieren_atencion"),
            "control_calidad": resumen.get("control_calidad") or []}


def diccionario(audiencia: str) -> str:
    return DICCIONARIO_COMUN + (DICCIONARIO_CLIENTE if audiencia == CLIENTE else DICCIONARIO_INTERNO)
