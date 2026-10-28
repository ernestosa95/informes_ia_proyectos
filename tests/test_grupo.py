"""Informes de grupo (grupo.py + pipeline) y aviso de cambios de estado al server."""
from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

from informes_ia import grupo, render
from informes_ia.config_dinamica import get_configuracion
from informes_ia.historial.almacen import AlmacenReportes
from informes_ia.historial.estados import Estado
from informes_ia.historial.servicio import FalloRed, ServicioReportes
from informes_ia.pipeline_real import PipelineReal

from .conftest import FIN, HOSPITAL, INICIO

DESDE = INICIO.strftime("%Y-%m-%d %H:%M:%S")
HASTA = FIN.strftime("%Y-%m-%d %H:%M:%S")
GRUPO = {"id": 1, "nombre": "Provincia de Prueba", "hospital_ids": [HOSPITAL, "P98", "P98"]}


def _resumen(fuente):
    return grupo.generar_resumen_grupo(fuente, GRUPO, DESDE, HASTA, config=get_configuracion(fuente))


def test_filas_por_hospital_con_fuentes_livianas(fuente):
    r = _resumen(fuente)
    filas = {f["hospital_id"]: f for f in r["hospitales"]}
    assert list(filas) == [HOSPITAL, "P98"]                 # sin repetidos

    p99 = filas[HOSPITAL]
    assert p99["uptime_pct"] == 75.0                         # mismo cálculo que el individual
    assert p99["incidentes"] == 2                            # la de [BAJA] y la de antes del período no cuentan
    assert p99["incidentes_criticos"] == 1                   # VM_OFFLINE
    assert p99["incidentes_abiertos_al_cierre"] == 1         # VM_RAM sin cerrar
    assert p99["bases_sin_backup"] == 2                      # ExtensaPACS (52 h) y master (nunca)
    assert p99["mirth_detenidos"] == 1 and p99["mirth_cola_maxima"] == 150
    assert p99["bases_con_error_checkdb"] == 1
    assert p99["motivos_atencion"][0] == "disponibilidad 75.0%"

    assert filas["P98"]["uptime_pct"] == 100.0
    assert filas["P98"]["motivos_atencion"] == []


def test_agregados_y_hospitales_que_requieren_atencion(fuente):
    r = _resumen(fuente)
    ag = r["agregados"]
    assert (ag["hospitales"], ag["hospitales_con_datos"], ag["hospitales_que_requieren_atencion"]) == (2, 2, 1)
    assert ag["uptime_promedio_pct"] == 87.5
    assert [h["hospital_id"] for h in r["hospitales_que_requieren_atencion"]] == [HOSPITAL]
    assert any("Sin KPIs" in a for a in r["control_calidad"])


def test_hospital_que_nunca_reporto_no_es_una_caida(fuente):
    # Caché sin agente propio o incorporado después (HUPC-C1, 28/10): no requiere atención.
    r = grupo.generar_resumen_grupo(fuente, {"id": 2, "nombre": "X", "hospital_ids": ["P98", "NO_EXISTE"]},
                                    DESDE, HASTA, config=get_configuracion(fuente))
    filas = {f["hospital_id"]: f for f in r["hospitales"]}
    assert filas["NO_EXISTE"]["motivos_atencion"] == []
    assert "NO_EXISTE" not in [h["hospital_id"] for h in r["hospitales_que_requieren_atencion"]]
    assert any("NO_EXISTE" in a and "sin agente propio" in a for a in r["control_calidad"])


def test_hospital_que_dejo_de_reportar_requiere_atencion(fuente):
    # P99 reporta de 00:00 a 04:00; de 06:00 a 10:00 ya no: eso sí es una caída a revisar.
    despues = lambda h: (FIN + timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")
    r = grupo.generar_resumen_grupo(fuente, {"id": 3, "nombre": "Y", "hospital_ids": [HOSPITAL]},
                                    despues(2), despues(6), config=get_configuracion(fuente))
    assert r["hospitales"][0]["motivos_atencion"] == ["sin reportes en el período"]
    assert any(HOSPITAL in a and "caída real" in a for a in r["control_calidad"])


def test_puntos_pegados_en_un_parrafo_van_uno_por_linea():
    from informes_ia import ai_report
    data = {"puntos_a_revisar": "[ALTA] A/x — y → z. [MEDIA] B/x — y → z. [BAJA] C/x — y → z."}
    ai_report._separar_puntos(data)
    assert data["puntos_a_revisar"].splitlines() == ["[ALTA] A/x — y → z.", "[MEDIA] B/x — y → z.", "[BAJA] C/x — y → z."]


def test_nombres_de_campos_en_el_texto_quedan_avisados():
    from informes_ia import audiencia
    avisos = audiencia.revisar_vocabulario(
        {"resumen": {"texto": "Actividad clínica no disponible (estudios_totales es null). Canal [MIRTH_H05] SYSTEM_ERROR."},
         "puntos_a_revisar": "[BAJA] x"}, "interno")
    assert any("estudios_totales" in a and "null" in a and "MIRTH" not in a for a in avisos)


def test_cliente_no_ve_reportes_ni_alertas_en_la_comparacion():
    from informes_ia import audiencia
    comp = {"periodo_anterior": {}, "anterior": {"uptime_pct": 99.6, "alertas": 2, "estudios_totales": 1822, "reportes": 7732},
            "actual": {"uptime_pct": 100.0, "alertas": 19, "estudios_totales": 1015, "reportes": 7703}}
    vista = audiencia.vista_individual({"comparacion": comp}, {"humanos": []}, "cliente", {"id": "H05"})
    assert vista["comparacion"]["actual"] == {"uptime_pct": 100.0, "estudios_totales": 1015}
    interno = audiencia.vista_individual({"comparacion": comp}, {"humanos": []}, "interno", {"id": "H05"})
    assert interno["comparacion"]["actual"]["alertas"] == 19


class _ClienteFalso:
    def __init__(self, texto):
        self.models = SimpleNamespace(generate_content=lambda **k: SimpleNamespace(text=texto, model_version="m"))


_JSON_GRUPO = {
    "meta": {"tipo": "interno", "fecha": "01/10/2026", "hospital": "Provincia de Prueba"},
    "resumen": {"texto": "Un hospital con problemas."},
    "situacion_general": "Disponibilidad dispar.",
    "hospitales_atencion": "P99: disponibilidad baja.",
    "recomendacion": "Revisar P99.",
}


def test_pipeline_de_grupo_de_punta_a_punta(fuente):
    settings = SimpleNamespace(gemini_api_key="x", gemini_model_json="m", database_url=fuente)
    pipe = PipelineReal(settings, client=_ClienteFalso(json.dumps(_JSON_GRUPO)))
    peticion = {"alcance": "grupo", "grupo": GRUPO, "hospital_id": "GRUPO-1",
                "fecha_inicio": DESDE, "fecha_fin": HASTA, "tipo_reporte": "interno"}

    contexto = pipe.preparar_contexto(peticion)
    data = pipe.generador_ia(contexto)
    assert data["meta"]["alcance"] == "grupo"
    assert data["resumen"]["uptime"] == "87,50%"
    assert [h["hospital_id"] for h in data["tabla_hospitales"]] == [HOSPITAL, "P98"]

    png = pipe.generar_grafico_png(peticion)
    assert png and png[:4] == b"\x89PNG"

    html = render.renderizar_html(data)
    assert "INFORME DE GRUPO" in html and "Hospital Sano" in html and "disponibilidad 75.0%" in html
    assert "Control de calidad" in html                      # informe interno


def test_aviso_de_cambios_de_estado(tmp_path):
    avisos = []
    servicio = ServicioReportes(
        AlmacenReportes(tmp_path / "h.sqlite"),
        lambda ctx: {"meta": {}, "resumen": {}},
        render_pdf=lambda d, g: b"", generar_grafico=lambda p: None,
        al_cambiar_estado=lambda rid, estado: avisos.append(estado),
    )
    ok = servicio.solicitar_reporte({"hospital_id": HOSPITAL})
    servicio.procesar(ok)
    servicio.aprobar(ok)

    def caida(_ctx):
        raise FalloRed("caída")
    servicio.generador_ia = caida
    mal = servicio.solicitar_reporte({"hospital_id": HOSPITAL})
    servicio.procesar(mal)
    assert avisos == [Estado.FINALIZADO.value, Estado.APROBADO.value, Estado.ERROR.value]


def test_un_aviso_que_falla_no_afecta_al_informe(tmp_path):
    def roto(_rid, _estado):
        raise RuntimeError("Asana caído")
    servicio = ServicioReportes(AlmacenReportes(tmp_path / "h.sqlite"), lambda ctx: {"meta": {}, "resumen": {}},
                                render_pdf=lambda d, g: b"", generar_grafico=lambda p: None, al_cambiar_estado=roto)
    rid = servicio.solicitar_reporte({"hospital_id": HOSPITAL})
    servicio.procesar(rid)
    assert servicio.consultar_estado(rid) == Estado.FINALIZADO.value
