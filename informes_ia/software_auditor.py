"""
SoftwareAuditor: procesa `software_monitoring`, una tabla EAV donde
`app_name` define el "dialecto" de `extra_data`. Tres dialectos vistos:

- ssl_certificate: metric_value = días restantes de un certificado.
- elasticsearch: alertas de logs de SUITESTENSA con severidad (LOW/MEDIUM/
  HIGH/WARNING) y un conteo (metric_value).
- mirth: una fila por canal (component_id = "[INSTANCIA] canal") con su
  estado (STARTED/STOPPED/...), la cola actual en metric_value (`queued`) y
  en extra_data los contadores acumulados recibidos/enviados/errored desde que
  arrancó el canal (main.py del server).
- sql_integrity: DBCC CHECKDB tras un reinicio de SQL, una fila por base
  (status OK/ERROR/NOT_ONLINE, metric_value = errores).
- sql_backup: último backup completo por base; se lee aparte
  (db.obtener_ultimos_backups) porque su fecha puede ser anterior al período.
- dicom_routing: cola de cada regla de autoenrute (metric_value = pendientes).
- patient_portal: estudios por estado de publicación del portal (metric_value).

TODOs abiertos (documentados, sin resolver todavía — ver README):
- No confirmado si `elasticsearch.metric_value` es un conteo acumulado
  desde que existe la alerta, o una ventana rodante. Por ahora reportamos
  el ÚLTIMO valor visto en el período + el máximo, para no asumir de más.
- No existe una clave `ssl_expiration_warning_days` en `configuracion`;
  usamos `ConfiguracionGlobal.ssl_warning_days` (default 30) hasta que se
  defina un valor oficial.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .conexion import a_datetime, a_dict
from .config_dinamica import ConfiguracionGlobal
from .logging_utils import get_logger

log = get_logger(__name__)


def _parse_extra(extra_data: Any) -> dict[str, Any]:
    """extra_data llega como dict (Postgres) o como texto JSON (SQLite)."""
    return a_dict(extra_data) or {}


def _entero(valor: Any) -> int:
    try:
        return int(valor or 0)
    except (TypeError, ValueError):
        return 0


_ORDEN_SEVERIDAD = {"LOW": 0, "WARNING": 1, "MEDIUM": 2, "HIGH": 3}


def _rango_severidad(valor: str | None) -> int:
    return _ORDEN_SEVERIDAD.get((valor or "").upper(), -1)


def analizar_software(
    filas: list[dict[str, Any]],
    config: ConfiguracionGlobal,
    backups: list[dict[str, Any]] | None = None,
    fin: Any = None,
) -> dict[str, Any]:
    """
    filas: lista de dicts con las columnas crudas de `software_monitoring`
    (app_name, component_id, status_value, metric_value, extra_data,
    timestamp), ya ordenadas por timestamp ascendente, para UN hospital.
    backups: último backup por base hasta el fin del período (db.obtener_ultimos_backups).
    fin: fin del período, para calcular la antigüedad de los backups.
    """
    certificados: dict[str, dict[str, Any]] = {}
    alertas_es: dict[str, dict[str, Any]] = {}
    canales_mirth: dict[str, dict[str, Any]] = {}
    checkdb: dict[str, dict[str, Any]] = {}
    reglas_dicom: dict[str, dict[str, Any]] = {}
    portal: dict[str, dict[str, Any]] = {}

    for fila in filas:
        app_name = fila.get("app_name")
        extra = _parse_extra(fila.get("extra_data"))

        if app_name == "ssl_certificate":
            # status ERROR = el agente no pudo conectarse a verificar el certificado (red, DNS,
            # puerto): NO es un certificado vencido. Sin días; el server los guarda como 0.
            url = fila.get("component_id", "?")
            no_verificado = str(fila.get("status_value") or "").upper() == "ERROR"
            certificados[url] = {
                "url": url,
                "status": fila.get("status_value"),
                "dias_restantes": None if no_verificado else fila.get("metric_value"),
                "expiration_date": extra.get("expiration_date"),
                "no_se_pudo_verificar": no_verificado,
                "motivo": (extra.get("last_error") or "error de conexión al verificar") if no_verificado else None,
            }

        elif app_name == "elasticsearch":
            codigo = fila.get("component_id", "?")
            actual = alertas_es.get(codigo)
            valor = fila.get("metric_value", 0) or 0
            if actual is None:
                alertas_es[codigo] = {
                    "codigo": codigo,
                    "titulo": extra.get("titulo"),
                    "severidad_max": fila.get("status_value"),
                    "conteo_ultimo": valor,
                    "conteo_maximo_periodo": valor,
                }
            else:
                actual["conteo_ultimo"] = valor
                actual["conteo_maximo_periodo"] = max(actual["conteo_maximo_periodo"], valor)
                if _rango_severidad(fila.get("status_value")) > _rango_severidad(actual["severidad_max"]):
                    actual["severidad_max"] = fila.get("status_value")

        elif app_name == "mirth":
            # Clave = canal. Antes era la instancia y todos los canales de una
            # instancia se pisaban entre sí (quedaba uno solo).
            canal = fila.get("component_id") or "?"
            cola = _entero(fila.get("metric_value"))
            errored = _entero(extra.get("errored"))
            c = canales_mirth.get(canal)
            if c is None:
                c = canales_mirth[canal] = {
                    "canal": canal,
                    "instancia": extra.get("instancia"),
                    "cola_maxima_periodo": cola,
                    "errores_en_periodo": 0,
                    "_errored_anterior": errored,
                }
            else:
                # errored es acumulado desde que arrancó el canal: se suma lo que
                # crece; si baja, el canal se reinició y el valor nuevo es todo nuevo.
                delta = errored - c["_errored_anterior"]
                c["errores_en_periodo"] += delta if delta >= 0 else errored
                c["_errored_anterior"] = errored
                c["cola_maxima_periodo"] = max(c["cola_maxima_periodo"], cola)
            c["estado"] = fila.get("status_value")
            c["cola_actual"] = cola
            c["ultimo_error"] = extra.get("last_error") or None
            c["ultima_lectura"] = str(fila.get("timestamp"))

        elif app_name == "sql_integrity":
            base = fila.get("component_id") or "?"
            checkdb[base] = {
                "base": base,
                "estado": fila.get("status_value"),
                "errores": _entero(fila.get("metric_value")),
                "fecha": str(fila.get("timestamp")),
                "detalle": extra.get("detail") or None,
            }

        elif app_name == "dicom_routing":
            regla = fila.get("component_id") or "?"
            cola = _entero(fila.get("metric_value"))
            r_ = reglas_dicom.setdefault(regla, {"regla": regla, "cola_maxima_periodo": 0})
            r_["etiqueta"] = extra.get("label") or r_.get("etiqueta")
            r_["cola_actual"] = cola
            r_["cola_maxima_periodo"] = max(r_["cola_maxima_periodo"], cola)

        elif app_name == "patient_portal":
            estado = fila.get("component_id") or "?"
            total = _entero(fila.get("metric_value"))
            p_ = portal.setdefault(estado, {"estado": estado, "maximo_periodo": 0})
            p_["descripcion"] = fila.get("status_value") or extra.get("state") or p_.get("descripcion")
            p_["actual"] = total
            p_["maximo_periodo"] = max(p_["maximo_periodo"], total)

        elif app_name == "sql_backup":
            pass  # se resuelve con `backups` (ver docstring)

        else:
            log.debug("app_name desconocido en software_monitoring: %s", app_name)

    certificados_alerta = [
        c for c in certificados.values()
        if isinstance(c["dias_restantes"], (int, float)) and c["dias_restantes"] < config.ssl_warning_days
    ]
    certificados_no_verificados = [c for c in certificados.values() if c["no_se_pudo_verificar"]]

    canales_con_problema = []
    for c in canales_mirth.values():
        c.pop("_errored_anterior", None)
        if (
            (c["estado"] or "").upper() != "STARTED"
            or c["cola_maxima_periodo"] > config.mirth.queued_threshold
            or c["errores_en_periodo"] > 0
        ):
            canales_con_problema.append(c)

    alertas_altas = [
        a for a in alertas_es.values()
        if _rango_severidad(a["severidad_max"]) >= _rango_severidad("HIGH")
    ]

    fin_dt = a_datetime(fin) or datetime.now()
    estado_backups = []
    for b in backups or []:
        nunca = (b.get("status_value") or "").upper() == "NEVER"
        fecha = a_datetime(b.get("timestamp"))
        horas = None if nunca or fecha is None else round((fin_dt - fecha).total_seconds() / 3600, 1)
        estado_backups.append({
            "base": b.get("component_id"),
            "ultimo_backup": None if nunca else str(fecha),
            "horas_desde_el_backup": horas,
            "alerta": nunca or (horas is not None and horas > config.sql_backup_max_hours),
        })

    return {
        "integridad_bases_checkdb": {
            "bases_chequeadas": len(checkdb),
            "con_problema": [c for c in checkdb.values() if (c["estado"] or "").upper() != "OK"],
        } if checkdb else None,
        "backups_sql": {
            "umbral_horas": config.sql_backup_max_hours,
            "bases": len(estado_backups),
            "con_alerta": [b for b in estado_backups if b["alerta"]],
        } if estado_backups else None,
        "autoenrute_dicom": {
            "reglas": len(reglas_dicom),
            "con_cola": sorted(
                (r_ for r_ in reglas_dicom.values() if r_["cola_maxima_periodo"] > 0),
                key=lambda r_: r_["cola_maxima_periodo"], reverse=True,
            )[:10],
        } if reglas_dicom else None,
        "portal_paciente": list(portal.values()) or None,
        "certificados_ssl": {
            "total_monitoreados": len(certificados),
            "con_alerta": certificados_alerta,
            "no_se_pudo_verificar": certificados_no_verificados,
            "umbral_dias_aviso": config.ssl_warning_days,
        },
        "logs_elasticsearch": {
            "total_codigos_distintos": len(alertas_es),
            "alertas_severidad_alta": alertas_altas,
            "detalle_completo": list(alertas_es.values()),
        },
        "canales_mirth": {
            "total_canales": len(canales_mirth),
            "con_problema": canales_con_problema,
            "umbral_cola": config.mirth.queued_threshold,
        },
    }
