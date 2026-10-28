"""
Política por público (audiencia.py): qué ve la IA en un informe para el cliente y en uno
interno, vocabulario, control del texto generado, comparación y plantillas.
"""
from __future__ import annotations

import json

from informes_ia import ai_report, audiencia, grupo, preprocess, render
from informes_ia.config_dinamica import get_configuracion

from .conftest import FIN, HOSPITAL, INICIO

DESDE = INICIO.strftime("%Y-%m-%d %H:%M:%S")
HASTA = FIN.strftime("%Y-%m-%d %H:%M:%S")
ASANA = {"humanos": ["CASO: Pedido de nuevo equipo de RX\nEstado: Abierta\nDescripcion: el jefe Juan comentó demoras"],
         "automaticos": ["🔴 P99 | VM_RAM_ARP99APPV — Completada"]}

# Lo que NUNCA puede llegar a un prompt para el cliente: identificadores técnicos y debilidades.
NO_PARA_CLIENTE = ["ARP99APPV", "[MIRTH_SE]", "huecos_sin_reportes", "borradores", "backlog",
                   "control_calidad", "VM_RAM", "comentó demoras", "SLRadioService", "ExtensaPACS"]


def _resumen(fuente):
    return preprocess.generar_resumen(fuente, HOSPITAL, DESDE, HASTA, config=get_configuracion(fuente))


def test_vista_cliente_no_lleva_datos_internos(fuente):
    vista = audiencia.vista_individual(_resumen(fuente), ASANA, "cliente", {"id": HOSPITAL, "nombre": "H"})
    texto = json.dumps(vista, ensure_ascii=False)
    for prohibido in NO_PARA_CLIENTE:
        assert prohibido not in texto, prohibido
    assert vista["disponibilidad_pct"] == 75.0
    assert vista["incidentes"]["por_area"]["infraestructura física"] >= 1      # PS2 y disco 3
    assert "reforzar la política de resguardo (backups) de las bases de datos" in vista["recomendaciones_sugeridas"]
    assert vista["casos_de_gestion"] == ["Pedido de nuevo equipo de RX"]       # sólo el título


def test_prompt_cliente_no_lleva_datos_internos(fuente):
    prompt = ai_report._construir_prompt(_resumen(fuente), ASANA, "Hospital de Prueba", "cliente", HOSPITAL)
    # "borradores" o "backlog" sí aparecen, pero en la guía de vocabulario (como términos prohibidos).
    for prohibido in [p for p in NO_PARA_CLIENTE if p not in ("borradores", "backlog")]:
        assert prohibido not in prompt, prohibido
    assert "VOCABULARIO PARA EL CLIENTE" in prompt and "DICCIONARIO DE DATOS" in prompt


def test_vista_interna_lleva_todo_el_detalle(fuente):
    vista = audiencia.vista_individual(_resumen(fuente), ASANA, "interno", {"id": HOSPITAL, "nombre": "H"})
    tipos = {g["tipo"]: g for g in vista["eventos"]["por_tipo"]}
    assert tipos["fuente_poder_fallo"]["entidades"] == ["PS2"]
    assert tipos["fuente_poder_fallo"]["minutos_totales"] == 30
    assert vista["huecos_sin_reportes"]["cantidad"] == 1
    assert vista["software"]["canales_mirth"]["total_canales"] == 2
    assert vista["casos_asana"]["humanos"] == ASANA["humanos"]
    assert vista["control_calidad"]
    prompt = ai_report._construir_prompt(_resumen(fuente), ASANA, "Hospital de Prueba", "interno", HOSPITAL)
    assert "ARP99APPV" in prompt and "puntos_a_revisar" in prompt


def test_control_de_vocabulario():
    data = {"resumen": {"texto": "Hubo demoras en la cola de ARP99APPV."}, "recomendacion": "Todo bien."}
    avisos = audiencia.revisar_vocabulario(data, "cliente")
    assert len(avisos) == 1 and "demoras" in avisos[0] and "arp99appv" in avisos[0] and "cola" in avisos[0]
    assert audiencia.revisar_vocabulario({"resumen": {"texto": "Servicio estable."}}, "cliente") == []
    assert "puntos a revisar" in audiencia.revisar_vocabulario({"resumen": {"texto": "x"}}, "interno")[0]


def test_cifras_por_publico_y_avisos_al_revisor(fuente):
    resumen = _resumen(fuente)
    ia = {"meta": {"tipo": "cliente"}, "resumen": {"texto": "Sin demoras."}, "recomendacion": "x"}
    data = ai_report.completar_con_cifras(ia, resumen, ASANA)
    assert data["cifras"]["uptime_definicion"] == "porcentaje del período con los sistemas monitoreados en línea"
    assert any("demoras" in a for a in data["control_calidad"])                # aviso al revisor
    assert "Sin demoras" in data["resumen"]["texto"]                            # pero no se toca el texto


def test_comparacion_con_el_periodo_anterior(fuente):
    medio = INICIO.replace(hour=2).strftime("%Y-%m-%d %H:%M:%S")
    comp = preprocess.comparacion_periodo_anterior(fuente, HOSPITAL, medio, HASTA, get_configuracion(fuente))
    assert comp["periodo_anterior"] == {"desde": DESDE, "hasta": INICIO.replace(hour=1, minute=59, second=59).strftime("%Y-%m-%d %H:%M:%S")}
    assert round(comp["anterior"]["uptime_pct"]) == 50                          # 00:00-01:59:59 con hueco de 1 h
    assert comp["actual"]["uptime_pct"] == 100.0
    assert _resumen(fuente)["comparacion"] is None                              # sin datos antes del período


def test_grupo_cliente_recomienda_y_no_muestra_debilidades(fuente):
    r = grupo.generar_resumen_grupo(fuente, {"id": 1, "nombre": "G", "hospital_ids": [HOSPITAL, "P98"]},
                                    DESDE, HASTA, config=get_configuracion(fuente))
    vista = json.dumps(audiencia.vista_grupo(r, "cliente"), ensure_ascii=False)
    for prohibido in ("motivos_atencion", "mirth", "bases_sin_backup", "sin reportes", "abierto"):
        assert prohibido not in vista, prohibido
    assert "reforzar la política de resguardo" in vista

    base = {"meta": {"tipo": "cliente", "fecha": "x", "hospital": "G"}, "resumen": {"texto": "t"},
            "situacion_general": "s", "hospitales_atencion": "h", "recomendacion": "r"}
    html_cliente = render.renderizar_html(ai_report.completar_con_cifras_grupo(dict(base), r))
    assert "Recomendaciones por hospital" in html_cliente and "Mirth detenidos" not in html_cliente
    interno = {**base, "meta": {**base["meta"], "tipo": "interno"},
               "puntos_a_revisar": "[ALTA] P99 — disponibilidad 75 % → revisar el agente\n[BAJA] P98 — sin novedades"}
    html_interno = render.renderizar_html(ai_report.completar_con_cifras_grupo(interno, r))
    assert "Hospitales que requieren atención" in html_interno and "Mirth detenidos" in html_interno
    assert "Puntos a revisar" in html_interno and "revisar el agente" in html_interno


def test_esquema_interno_exige_puntos_a_revisar():
    assert "puntos_a_revisar" in ai_report._con_puntos_a_revisar(ai_report.ESQUEMA_RESPUESTA)["required"]
    assert "puntos_a_revisar" not in ai_report.ESQUEMA_RESPUESTA["required"]
