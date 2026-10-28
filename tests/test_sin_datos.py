"""
Qué pasa cuando falta un dato (2026-10-28): VM sin lectura, VM con error de acceso, RAM imposible,
hospital sin iDRAC. La regla: un dato faltante NUNCA se convierte en un valor (0, 100 %, "sin
novedades"); se informa como faltante.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from informes_ia import ai_report, audiencia, render
from informes_ia.config_dinamica import ConfiguracionGlobal, KpiMamografia, KpiRadiologia, ConfigMirth
from informes_ia.event_tracker import analizar_historial

T0 = datetime(2026, 10, 1, 0, 0)
CONFIG = ConfiguracionGlobal(
    offline_minutes=15, disk_threshold=90, temp_amb_max=27, temp_cpu_max=75, cpu_host_max=85, ram_host_max=95,
    cpu_vm_max=90, ram_vm_max=90, enable_fans=True, enable_power=True, enable_raid=True,
    kpi_rad=KpiRadiologia(False, 24, [], []), kpi_mamo=KpiMamografia(False, 7), mirth=ConfigMirth(False, 100, []),
    kpi_execution_time="08:00", global_alert_emails=[],
)


def _reporte(ts, vm, idrac=True):
    phy = {"telemetry": {"cpu": {"usage_percent": 10.0}, "ram": {"total_gb": 188, "used_gb": 90, "usage_percent": 48.0}}}
    if idrac:
        phy["sensors"] = {"temperatures": [{"name": "CPU1 Temp", "value": 50, "status": "OK"}],
                          "power": {"supplies": [{"name": "PS1", "watts": 300, "status": "OK"}]}}
    return {"envelope": {"timestamp": ts.isoformat()}, "physical_layer": phy, "virtual_layer": [vm]}


def _analizar(vm_por_minuto, idrac=True, minutos=60):
    filas = []
    for m in range(0, minutos + 1, 5):
        ts = T0 + timedelta(minutes=m)
        filas.append((json.dumps(_reporte(ts, vm_por_minuto(m), idrac)), "OK", ts))
    return analizar_historial(filas, CONFIG, T0, T0 + timedelta(minutes=minutos))


def _vm(estado="Online", motivo="ok", telemetry=None):
    return {"id": "APPV", "type": "vm", "state": estado, "state_reason": motivo,
            "telemetry": telemetry if telemetry is not None else
            {"cpu": {"usage_percent": 20.0}, "ram": {"total_gb": 48, "used_gb": 20, "usage_percent": 41.0}}}


def test_vm_con_error_de_acceso_no_es_vm_apagada():
    # De 00:20 a 00:40 la VM responde pero falla WMI (credenciales): no está apagada.
    r = _analizar(lambda m: _vm("Offline", "wmi_error", {}) if 20 <= m <= 40 else _vm())
    assert [e for e in r["eventos"] if e["tipo"] == "nodo_offline"] == []
    assert r["disponibilidad_vms_pct"]["APPV"] == 100.0           # esas muestras no cuentan
    assert r["vms_sin_acceso_reportes"]["APPV"] == 5


def test_vm_apagada_de_verdad_si_es_evento():
    r = _analizar(lambda m: _vm("Offline", "port_closed", {}) if 20 <= m <= 40 else _vm())
    assert [e["duracion_min"] for e in r["eventos"] if e["tipo"] == "nodo_offline"] == [20.0]


def test_offline_de_un_reporte_es_ruido():
    r = _analizar(lambda m: _vm("Offline", "port_closed", {}) if m == 30 else _vm())
    assert [e for e in r["eventos"] if e["tipo"] == "nodo_offline"] == []


def test_vm_sin_telemetria_no_inventa_cpu_ni_ram():
    # Online pero sin lectura de CPU/RAM: no hay eventos de rendimiento (antes no hubo valor que evaluar).
    r = _analizar(lambda m: _vm(telemetry={}))
    assert not [e for e in r["eventos"] if e["tipo"] in ("cpu_vm_alto", "ram_vm_alto")]


def test_ram_imposible_no_es_100():
    # Sin FreePhysicalMemory el agente manda usado == total (100 %): no es una lectura real.
    falsa = {"cpu": {"usage_percent": 0.0}, "ram": {"total_gb": 48, "used_gb": 48, "usage_percent": 100.0}}
    r = _analizar(lambda m: _vm(telemetry=falsa))
    assert not [e for e in r["eventos"] if e["tipo"] == "ram_vm_alto"]


def test_sin_idrac_el_texto_lo_pone_el_codigo():
    r = _analizar(lambda m: _vm(), idrac=False)
    assert r["reportes_con_dato"].get("energia", 0) == 0 and r["reportes_con_dato"].get("temperatura", 0) == 0
    resumen = {"infraestructura": r, "control_calidad": ["Sin datos de energía (fuentes) en el período."]}
    ia = {"meta": {"tipo": "cliente"}, "resumen": {"texto": "x"},
          "infraestructura": {"energia": "Energía estable y sin novedades.", "termica": "Temperatura óptima.", "mensaje": ""}}

    cliente = ai_report.completar_con_cifras(json.loads(json.dumps(ia)), resumen, {"humanos": []})
    assert cliente["infraestructura"]["energia"] == "" and cliente["infraestructura"]["termica"] == ""
    html = render.renderizar_html(cliente)
    assert "Energía" not in html and "Estado de la Infraestructura" not in html     # sección vacía: no va

    interno = ai_report.completar_con_cifras({**json.loads(json.dumps(ia)), "meta": {"tipo": "interno"}},
                                             resumen, {"humanos": []})
    assert interno["infraestructura"]["energia"].startswith("Sin datos de energía")
    assert interno["infraestructura"]["termica"].startswith("Sin datos de temperatura")

    vista = audiencia.vista_individual({"infraestructura": r}, {"humanos": []}, "cliente", {"id": "X", "nombre": "X"})
    assert {"energía", "temperatura"} <= set(vista["datos_no_disponibles"])


# --- Hallazgos del primer informe real (H05, 2026-10-28) ----------------------------------

def test_ram_que_oscila_alrededor_del_umbral_es_un_episodio():
    # 89 % / 91 % alternados durante una hora: antes eran 6 eventos, ahora 1 episodio.
    oscila = lambda m: _vm(telemetry={"cpu": {"usage_percent": 20.0},
                                       "ram": {"total_gb": 48, "used_gb": 44, "usage_percent": 91.0 if (m // 5) % 2 == 0 else 89.0}})
    r = _analizar(oscila)
    ram = [e for e in r["eventos"] if e["tipo"] == "ram_vm_alto"]
    assert len(ram) == 1 and ram[0]["detalle"]["cortes_unidos"] == 7 and ram[0]["duracion_min"] == 60.0


def test_ssl_en_error_no_es_certificado_vencido():
    from informes_ia.software_auditor import analizar_software
    filas = [
        {"app_name": "ssl_certificate", "component_id": "https://visor", "status_value": "ERROR",
         "metric_value": 0, "extra_data": {"last_error": "timed out"}, "timestamp": T0},
        {"app_name": "ssl_certificate", "component_id": "https://portal", "status_value": "WARNING",
         "metric_value": 12, "extra_data": {}, "timestamp": T0},
    ]
    ssl = analizar_software(filas, CONFIG)["certificados_ssl"]
    assert [c["url"] for c in ssl["con_alerta"]] == ["https://portal"]
    assert ssl["no_se_pudo_verificar"][0]["motivo"] == "timed out"
    assert ssl["no_se_pudo_verificar"][0]["dias_restantes"] is None


def test_sin_cpu_ni_ram_del_host_no_hay_grafico(tmp_path):
    from informes_ia import db

    class Fuente:
        def obtener_serie_grafico(self, *a):
            return [(T0 + timedelta(minutes=5 * i), None, None) for i in range(10)]
    assert db.generar_grafico_historico(Fuente(), "H05", "x", "y", tmp_path / "g.png") is None


# --- Informe de cliente de H05 (2026-10-28) ------------------------------------------------

def test_cliente_no_cuenta_picos_de_uso_ni_dice_gestionados():
    eventos = [{"tipo": "ram_vm_alto", "entidad": "APPV"}] * 480 + [{"tipo": "nodo_offline", "entidad": "PRTV"}] * 15 \
        + [{"tipo": "disco_lleno", "entidad": "APPV D:"}]
    resumen = {"infraestructura": {"eventos": eventos, "uptime_pct": 100.0, "reportes_con_dato": {}}}
    vista = audiencia.vista_individual(resumen, {"humanos": []}, "cliente", {"id": "H05", "nombre": "H05"})
    assert vista["incidentes"] == {"total_detectados": 16,
                                   "por_area": {"servidores y equipos": 15, "capacidad": 1}}
    ia = {"meta": {"tipo": "cliente"}, "resumen": {"texto": "x"}, "infraestructura": {"mensaje": ""}}
    cliente = ai_report.completar_con_cifras(ia, resumen, {"humanos": []})
    assert cliente["incidencias"]["internas"] == 16
    interno = ai_report.completar_con_cifras({**ia, "meta": {"tipo": "interno"}}, resumen, {"humanos": []})
    assert interno["incidencias"]["internas"] == 496


def test_cliente_sin_host_ni_idrac_no_muestra_sd_ni_seccion_vacia():
    r = _analizar(lambda m: _vm(), idrac=False)
    r["metricas"] = {}
    resumen = {"infraestructura": r}
    ia = {"meta": {"tipo": "cliente"}, "resumen": {"texto": "x"}, "infraestructura": {"energia": "", "termica": "", "mensaje": ""},
          "incidencias": {"analisis": "a"}, "calidad": {}, "recomendacion": "r"}
    html = render.renderizar_html(ai_report.completar_con_cifras(ia, resumen, {"humanos": []}))
    assert "s/d" not in html.split("Comparación")[0] and "Estado de la Infraestructura" not in html
    assert "2. Gestión de Incidencias" in html and "3. Calidad" in html
    interno = render.renderizar_html(ai_report.completar_con_cifras({**ia, "meta": {"tipo": "interno"}}, resumen, {"humanos": []}))
    assert "s/d" in interno and "2. Estado de la Infraestructura" in interno and "3. Gestión de Incidencias" in interno
