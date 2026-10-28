"""
Base de prueba con las tablas de TecnoMonitor que lee el módulo, en SQLite y,
si se define INFORMES_IA_TEST_PG_URL (una base vacía de Postgres), también en
Postgres. Los reportes copian la estructura que arma el agente 4.5
(tecnomonitor-agent/agent_logic.py): sensores, energía, RAID y red dentro de
physical_layer.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

INICIO = datetime(2026, 10, 1, 0, 0)
FIN = datetime(2026, 10, 1, 4, 0)
HOSPITAL = "P99"

# Tipos de columna JSON: en el server son JSON (Postgres json) salvo kpi_json_data, que es Text.
_DDL = {
    "sqlite": "TEXT",
    "postgresql": "JSON",
}


def snapshot(ts: datetime, *, ps2="OK", disco3="OK", vm_state="Online", ram_vm=40.0, svc="Running") -> dict:
    return {
        "envelope": {"schema_version": "4.5", "agent_version": "4.5.5", "hospital_id": HOSPITAL,
                     "timestamp": ts.isoformat()},
        "collection_meta": {"suitestensa_logs": {"new_alerts": 2}},
        "physical_layer": {
            "host_info": {"uptime_seconds": 86400},
            "telemetry": {
                "cpu": {"usage_percent": 12.5},
                "ram": {"total_gb": 188.0, "used_gb": 94.0, "usage_percent": 50.0},
            },
            "sensors": {
                "status": "OK",
                "temperatures": [{"name": "CPU1 Temp", "value": 55, "unit": "C", "status": "OK"},
                                 {"name": "System Board Inlet Temp", "value": 22, "unit": "C", "status": "OK"}],
                "fans": [{"name": "Fan1", "value": 5000, "unit": "RPM", "status": "OK"}],
                "power": {"watts_current": 300, "supplies": [
                    {"name": "PS1", "watts": 300.0, "status": "OK"},
                    {"name": "PS2", "watts": 0.0, "status": ps2},
                ]},
            },
            "storage_layer": {
                "controllers": [{"name": "PERC H730P", "status": "OK"}],
                "logical_volumes": [{"name": "Disk.Virtual.0", "status": "OK"},
                                    {"name": None, "status": "Unknown"}],   # error del iDRAC: se ignora
                "physical_drives": [{"slot": "0", "status": "OK"}, {"slot": "3", "status": disco3},
                                    {"slot": None, "status": "Unknown"}],   # error del iDRAC: se ignora
            },
            "network_health": {"cloud_latency_ms": 40, "cloud_status": "ok"},
        },
        "virtual_layer": [{
            "id": "ARP99APPV", "type": "vm", "os": "windows", "state": vm_state, "state_reason": "ok",
            "telemetry": {"cpu": {"usage_percent": 20.0},
                          "ram": {"total_gb": 48.0, "used_gb": 19.2, "usage_percent": ram_vm}},
            "storage": [{"mount_point": "C:", "usage_percent": 60.0}],
            "application_layer": {"services": [
                {"name": "MSSQLSERVER", "display_name": "SQL Server (MSSQLSERVER)", "state": "Running",
                 "vital_signs": {"cpu_percent": 1.0, "ram_mb": 6000.0}},
                {"name": "SLRadioService", "display_name": "SUITESTENSA Radio", "state": svc,
                 "vital_signs": {"cpu_percent": 0.0, "ram_mb": 200.0}},
            ]},
        }],
    }


def _crear_tablas(conn, json_type: str) -> None:
    conn.execute(text("CREATE TABLE hospitales_metadata (hospital_id VARCHAR PRIMARY KEY, nombre VARCHAR, asana_project_id VARCHAR)"))
    conn.execute(text("CREATE TABLE configuracion (clave VARCHAR PRIMARY KEY, valor VARCHAR)"))
    conn.execute(text(f"""CREATE TABLE reportes_historicos (id INTEGER PRIMARY KEY, hospital_id VARCHAR,
        timestamp TIMESTAMP, host_status VARCHAR, host_cpu_usage FLOAT, host_ram_usage FLOAT,
        power_watts INTEGER, full_json_data {json_type})"""))
    conn.execute(text("""CREATE TABLE alertas (id INTEGER PRIMARY KEY, hospital_id VARCHAR, tipo VARCHAR,
        mensaje VARCHAR, start_time TIMESTAMP, end_time TIMESTAMP, is_active INTEGER, asana_task_gid VARCHAR,
        reaperturas INTEGER)"""))
    conn.execute(text("CREATE TABLE reportes_uso (id INTEGER PRIMARY KEY, hospital_id VARCHAR, timestamp TIMESTAMP, kpi_json_data TEXT)"))
    conn.execute(text(f"""CREATE TABLE software_monitoring (id INTEGER PRIMARY KEY, hospital_id VARCHAR,
        app_name VARCHAR, component_id VARCHAR, status_value VARCHAR, metric_value INTEGER,
        extra_data {json_type}, timestamp TIMESTAMP)"""))


def _poblar(conn) -> None:
    conn.execute(text("INSERT INTO hospitales_metadata VALUES (:h, 'Hospital de Prueba', '123')"), {"h": HOSPITAL})
    conn.execute(text("INSERT INTO hospitales_metadata VALUES ('P98', 'Hospital Sano', '456')"))
    for clave, valor in {"offline_minutes": "15", "enable_power": "1", "enable_raid": "1", "enable_fans": "1",
                         "mirth_queued_threshold": "100"}.items():
        conn.execute(text("INSERT INTO configuracion VALUES (:c, :v)"), {"c": clave, "v": valor})

    # Reportes cada 5 min de 00:00 a 01:00 y de 02:00 a 04:00: hueco de 1 h -> uptime 75 %.
    # host_status = "OK" (salud del iDRAC, como guarda el server): no debe afectar el uptime.
    # PS2 y el disco del slot 3 fallan entre 02:30 y 03:00; la VM está Offline en 3 reportes.
    i = 0
    ts = INICIO
    while ts <= FIN:
        if not (INICIO + timedelta(hours=1) < ts < INICIO + timedelta(hours=2)):
            falla = INICIO + timedelta(hours=2, minutes=30) <= ts <= INICIO + timedelta(hours=3)
            offline = INICIO + timedelta(hours=3, minutes=5) <= ts <= INICIO + timedelta(hours=3, minutes=25)
            svc_parado = INICIO + timedelta(hours=3, minutes=30) <= ts <= INICIO + timedelta(hours=3, minutes=45)
            snap = snapshot(ts, ps2="Critical" if falla else "OK", disco3="Failed" if falla else "OK",
                            vm_state="Offline" if offline else "Online",
                            svc="Stopped" if svc_parado else "Running")
            i += 1
            conn.execute(
                text("""INSERT INTO reportes_historicos VALUES (:id, :h, :ts, 'OK', 12.5, 94.0, 300, :j)"""),
                {"id": i, "h": HOSPITAL, "ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "j": json.dumps(snap)},
            )
        ts += timedelta(minutes=5)

    # P98: reporta todo el período sin huecos, sin alertas -> no requiere atención.
    ts = INICIO
    while ts <= FIN:
        i += 1
        conn.execute(
            text("""INSERT INTO reportes_historicos VALUES (:id, 'P98', :ts, 'OK', 5.0, 40.0, 250, :j)"""),
            {"id": i, "ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "j": json.dumps(snapshot(ts))},
        )
        ts += timedelta(minutes=5)

    # Alertas del motor para P99: una crítica (VM apagada), una común (RAM) abierta al cierre
    # y una cerrada como [BAJA] (módulo dado de baja: no cuenta). Más una de antes del período.
    alertas = [
        ("VM_OFFLINE_ARP99APPV", "[OK] Normalizado", INICIO + timedelta(hours=3, minutes=10), INICIO + timedelta(hours=3, minutes=25)),
        ("VM_RAM_ARP99APPV", "[WARNING] RAM 92%", INICIO + timedelta(hours=1), None),
        ("MIRTH_[MIRTH_SE] OLD", "[BAJA] Módulo dado de baja", INICIO + timedelta(minutes=5), INICIO + timedelta(minutes=30)),
        ("DISK_ARP99APPV_C:", "[OK] Normalizado", INICIO - timedelta(days=3), INICIO - timedelta(days=2)),
    ]
    for n, (tipo, msg, ini, fin) in enumerate(alertas, start=1):
        conn.execute(
            text("INSERT INTO alertas VALUES (:id, :h, :t, :m, :i, :f, :a, NULL, 0)"),
            {"id": n, "h": HOSPITAL, "t": tipo, "m": msg, "i": ini.strftime("%Y-%m-%d %H:%M:%S"),
             "f": fin.strftime("%Y-%m-%d %H:%M:%S") if fin else None, "a": 0 if fin else 1},
        )

    # Mirth: dos canales de la MISMA instancia (antes se pisaban). Cola en metric_value;
    # errored acumulado 5 -> 8 -> 2 (reinicio del canal) = 3 + 2 = 5 errores en el período.
    filas = [
        ("[MIRTH_SE] ADT", "STARTED", 3, 5, INICIO + timedelta(minutes=5)),
        ("[MIRTH_SE] ADT", "STARTED", 150, 8, INICIO + timedelta(minutes=10)),
        ("[MIRTH_SE] ADT", "STARTED", 0, 2, INICIO + timedelta(minutes=15)),
        ("[MIRTH_SE] ORU", "STOPPED", 0, 0, INICIO + timedelta(minutes=5)),
    ]
    for n, (canal, estado, cola, errored, ts) in enumerate(filas, start=1):
        conn.execute(
            text("INSERT INTO software_monitoring VALUES (:id, :h, 'mirth', :c, :e, :q, :x, :ts)"),
            {"id": n, "h": HOSPITAL, "c": canal, "e": estado, "q": cola, "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
             "x": json.dumps({"instancia": "MIRTH_SE", "recibidos": 1000, "enviados": 990,
                              "errored": errored, "last_error": ""})},
        )

    # Backups (fecha = la del backup, puede ser anterior al período): ExtensaPACS de hace 52 h
    # (alerta), ExtensaRadio de hace 5 h (OK), master nunca (NEVER). CHECKDB con error en una
    # base. Una regla DICOM con cola. El portal con un estado.
    otras = [
        ("sql_backup", "ExtensaPACS", "OK", 0, {}, INICIO - timedelta(hours=48)),
        ("sql_backup", "ExtensaRadio", "OK", 0, {}, INICIO - timedelta(hours=1)),
        ("sql_backup", "master", "NEVER", 0, {}, INICIO + timedelta(minutes=5)),
        ("sql_integrity", "ExtensaPACS", "ERROR", 3, {"detail": "CHECKDB found 0 allocation errors and 3 consistency errors"},
         INICIO + timedelta(minutes=40)),
        ("sql_integrity", "ExtensaRadio", "OK", 0, {"detail": ""}, INICIO + timedelta(minutes=45)),
        ("dicom_routing", "7", "OK", 12, {"label": "CR1 → PACS"}, INICIO + timedelta(minutes=5)),
        ("dicom_routing", "7", "OK", 40, {"label": "CR1 → PACS"}, INICIO + timedelta(minutes=10)),
        ("patient_portal", "RIS:4", "Publicado", 900, {"state": "Publicado"}, INICIO + timedelta(minutes=5)),
    ]
    for n, (app, comp, estado, valor, extra, ts) in enumerate(otras, start=100):
        conn.execute(
            text("INSERT INTO software_monitoring VALUES (:id, :h, :a, :c, :e, :q, :x, :ts)"),
            {"id": n, "h": HOSPITAL, "a": app, "c": comp, "e": estado, "q": valor,
             "ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "x": json.dumps(extra)},
        )


def _motores():
    motores = ["sqlite"]
    if os.getenv("INFORMES_IA_TEST_PG_URL"):
        motores.append("postgresql")
    return motores


@pytest.fixture(params=_motores())
def fuente(request, tmp_path):
    """URL de una base de prueba poblada, en cada motor disponible."""
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'tm.db'}"
    else:
        url = os.environ["INFORMES_IA_TEST_PG_URL"]
    engine = create_engine(url)
    with engine.begin() as conn:
        for tabla in ("hospitales_metadata", "configuracion", "reportes_historicos", "reportes_uso", "software_monitoring", "alertas"):
            conn.execute(text(f"DROP TABLE IF EXISTS {tabla}"))
        _crear_tablas(conn, _DDL[engine.dialect.name])
        _poblar(conn)
    engine.dispose()
    return url
