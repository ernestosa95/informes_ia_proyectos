"""Informes de grupo (grupo.py + pipeline) y aviso de cambios de estado al server."""
from __future__ import annotations

import json
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


def test_hospital_sin_reportes_queda_marcado(fuente):
    r = grupo.generar_resumen_grupo(fuente, {"id": 2, "nombre": "X", "hospital_ids": ["P98", "NO_EXISTE"]},
                                    DESDE, HASTA, config=get_configuracion(fuente))
    filas = {f["hospital_id"]: f for f in r["hospitales"]}
    assert filas["NO_EXISTE"]["motivos_atencion"] == ["sin reportes en el período"]
    assert any("NO_EXISTE" in a for a in r["control_calidad"])


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
