"""
Errores 1 a 5 de la revisión del 2026-10-27, contra reportes con la estructura
del agente 4.5 y en los dos motores (SQLite y, si está configurado, Postgres).
"""
from __future__ import annotations

from sqlalchemy import text

from informes_ia import db, preprocess
from informes_ia.conexion import get_engine, json_escalar
from informes_ia.config_dinamica import get_configuracion

from .conftest import FIN, HOSPITAL, INICIO

DESDE = INICIO.strftime("%Y-%m-%d %H:%M:%S")
HASTA = FIN.strftime("%Y-%m-%d %H:%M:%S")


def _resumen(fuente):
    return preprocess.generar_resumen(fuente, HOSPITAL, DESDE, HASTA, config=get_configuracion(fuente))


def _eventos(resumen, tipo):
    return [e for e in resumen["infraestructura"]["eventos"] if e["tipo"] == tipo]


# --- 1. uptime -----------------------------------------------------------------------------

def test_uptime_es_cobertura_y_no_depende_de_host_status(fuente):
    infra = _resumen(fuente)["infraestructura"]
    # 4 h de período, hueco de 01:00 a 02:00 (> 15 min) -> 75 %. host_status es "OK" en todas
    # las filas: con la lógica vieja (host_status == "online") daba 0 %.
    assert infra["uptime_pct"] == 75.0
    assert infra["huecos_sin_reportes"]["cantidad"] == 1
    assert infra["huecos_sin_reportes"]["mas_largos"][0]["duracion_min"] == 60


def test_disponibilidad_por_vm(fuente):
    infra = _resumen(fuente)["infraestructura"]
    total = infra["muestras_analizadas"]
    assert infra["disponibilidad_vms_pct"]["ARP99APPV"] == round(100 * (total - 3) / total, 2)
    assert len(_eventos(_resumen(fuente), "nodo_offline")) == 1


# --- 2. energía, RAID y red dentro de physical_layer ----------------------------------------

def test_fuente_de_poder_en_falla_genera_evento(fuente):
    eventos = _eventos(_resumen(fuente), "fuente_poder_fallo")
    assert [e["entidad"] for e in eventos] == ["PS2"]
    assert eventos[0]["duracion_min"] == 30


def test_raid_evalua_discos_y_omite_respuestas_incompletas_del_idrac(fuente):
    eventos = _eventos(_resumen(fuente), "raid_degradado")
    # Solo el disco del slot 3; el volumen sin nombre y el disco sin slot se ignoran.
    assert [e["entidad"] for e in eventos] == ["disco slot 3"]


def test_latencia_de_red_y_temperaturas_se_leen(fuente):
    metricas = _resumen(fuente)["infraestructura"]["metricas"]
    assert metricas["latencia_red_ms"]["avg"] == 40
    assert metricas["temp_cpu"]["max"] == 55
    assert metricas["ram_host"]["avg"] == 50.0


# --- 3. gráfico: RAM en %, no GB/16 ---------------------------------------------------------

def test_ram_del_grafico_sale_del_porcentaje_del_json(fuente):
    engine = get_engine(fuente)
    expr = json_escalar(engine, "full_json_data", "physical_layer", "telemetry", "ram", "usage_percent")
    with engine.connect() as conn:
        valores = {r[0] for r in conn.execute(text(f"SELECT {expr} FROM reportes_historicos"))}
    # host_ram_usage es 94 (GB); con la lógica vieja el gráfico mostraba 94/16 = 587 %.
    assert valores == {50.0}


def test_genera_el_png(fuente, tmp_path):
    ruta = db.generar_grafico_historico(fuente, HOSPITAL, DESDE, HASTA, tmp_path / "g.png")
    assert ruta is not None and ruta.stat().st_size > 1000


# --- 4. Mirth: por canal, con la cola real ---------------------------------------------------

def test_mirth_un_registro_por_canal_con_cola_y_errores(fuente):
    mirth = _resumen(fuente)["software"]["canales_mirth"]
    assert mirth["total_canales"] == 2
    canales = {c["canal"]: c for c in mirth["con_problema"]}
    adt = canales["[MIRTH_SE] ADT"]
    assert adt["cola_actual"] == 0
    assert adt["cola_maxima_periodo"] == 150      # supera el umbral de 100
    assert adt["errores_en_periodo"] == 5         # 5->8 (+3), reinicio a 2 (+2)
    assert canales["[MIRTH_SE] ORU"]["estado"] == "STOPPED"


# --- 5. conexión por URL (SQLite y Postgres) ------------------------------------------------

def test_metadata_y_configuracion_por_url(fuente):
    assert db.obtener_config_hospital(fuente, HOSPITAL) == {"nombre": "Hospital de Prueba", "asana_id": "123"}
    assert db.obtener_config_hospital(fuente, "NO_EXISTE") is None
    assert get_configuracion(fuente).offline_minutes == 15
