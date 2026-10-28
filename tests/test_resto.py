"""
Pendientes 6-8 y propuestas A, E y F de la revisión del 2026-10-27: módulos de
software nuevos, servicios caídos, control de calidad, errores de Gemini,
reintentos por tipo, cifras desde el código, Asana y plantilla.
Gemini se reemplaza por un cliente falso: no hay llamadas reales.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from google.genai import errors as genai_errors

from informes_ia import ai_report, asana_client, preprocess, render
from informes_ia.config_dinamica import get_configuracion
from informes_ia.historial.almacen import AlmacenReportes
from informes_ia.historial.estados import Estado
from informes_ia.historial.servicio import FalloRed, ServicioReportes
from informes_ia.historial.validacion import RespuestaInvalida
from informes_ia.pipeline_real import ContextoReporte, PipelineReal

from .conftest import FIN, HOSPITAL, INICIO

DESDE = INICIO.strftime("%Y-%m-%d %H:%M:%S")
HASTA = FIN.strftime("%Y-%m-%d %H:%M:%S")


def _resumen(fuente):
    return preprocess.generar_resumen(fuente, HOSPITAL, DESDE, HASTA, config=get_configuracion(fuente))


# --- 8. módulos de software nuevos y servicios ---------------------------------------------

def test_backups_toma_el_ultimo_aunque_sea_anterior_al_periodo(fuente):
    backups = _resumen(fuente)["software"]["backups_sql"]
    assert backups["bases"] == 3
    alertas = {b["base"]: b for b in backups["con_alerta"]}
    assert set(alertas) == {"ExtensaPACS", "master"}        # 52 h y nunca; ExtensaRadio (5 h) no
    assert alertas["ExtensaPACS"]["horas_desde_el_backup"] == 52.0
    assert alertas["master"]["ultimo_backup"] is None


def test_checkdb_dicom_y_portal(fuente):
    sw = _resumen(fuente)["software"]
    assert [c["base"] for c in sw["integridad_bases_checkdb"]["con_problema"]] == ["ExtensaPACS"]
    regla = sw["autoenrute_dicom"]["con_cola"][0]
    assert (regla["etiqueta"], regla["cola_maxima_periodo"], regla["cola_actual"]) == ("CR1 → PACS", 40, 40)
    assert sw["portal_paciente"][0]["actual"] == 900


def test_servicio_detenido_es_evento(fuente):
    eventos = [e for e in _resumen(fuente)["infraestructura"]["eventos"] if e["tipo"] == "servicio_detenido"]
    assert [e["entidad"] for e in eventos] == ["ARP99APPV:SLRadioService"]
    assert eventos[0]["duracion_min"] == 15


def test_control_calidad_avisa_uptime_bajo_y_kpis_faltantes(fuente):
    avisos = _resumen(fuente)["control_calidad"]
    assert any("Uptime de 75.0%" in a for a in avisos)
    assert any("KPIs de RIS/PACS" in a for a in avisos)
    assert not any("energía" in a for a in avisos)          # sí hay datos de energía


# --- 6. errores de Gemini y reintentos ------------------------------------------------------

class _ClienteFalso:
    """Imita genai.Client: models.generate_content devuelve o lanza lo que se le indique."""

    def __init__(self, *respuestas):
        self._respuestas = list(respuestas)
        self.models = SimpleNamespace(generate_content=self._generar)

    def _generar(self, **_kwargs):
        r = self._respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(text=r, model_version="gemini-prueba-001")


def _pipeline(cliente) -> PipelineReal:
    settings = SimpleNamespace(gemini_api_key="x", gemini_model_json="gemini-prueba")
    return PipelineReal(settings, client=cliente)


def _contexto(resumen=None, humanos=()) -> ContextoReporte:
    return ContextoReporte(
        resumen=resumen or {"infraestructura": {"uptime_pct": 99.5, "eventos": [{"tipo": "ram_vm_alto"}]}},
        asana_txt={"humanos": list(humanos), "automaticos": []},
        hospital_nombre="Hospital de Prueba", pdfs=[], tipo_reporte="cliente",
    )


_JSON_IA = {
    "meta": {"tipo": "cliente", "fecha": "01/10/2026", "hospital": "Hospital de Prueba"},
    "resumen": {"texto": "Período estable.", "uptime": "100%"},        # uptime inventado: se pisa
    "infraestructura": {"energia": "Sin novedades.", "termica": "Normal.", "mensaje": ""},
    "incidencias": {"analisis": "Un evento de RAM.", "externas": 40, "internas": 99},  # se pisan
    "calidad": {"estabilidad": "Buena.", "caso_destacado": "Ninguno."},
    "recomendacion": "Seguir monitoreando.",
}


def test_429_se_traduce_a_falloRed_con_el_retry_delay():
    error = genai_errors.ClientError(429, {"error": {
        "code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED",
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "52s"}]}})
    with pytest.raises(FalloRed) as exc:
        _pipeline(_ClienteFalso(error)).generador_ia(_contexto())
    assert exc.value.retry_delay_s == 52.0


def test_texto_que_no_es_json_es_respuesta_invalida():
    with pytest.raises(RespuestaInvalida):
        _pipeline(_ClienteFalso("Lo siento, no puedo.")).generador_ia(_contexto())


def test_las_cifras_las_pone_el_codigo_y_no_la_ia():
    data = _pipeline(_ClienteFalso(json.dumps(_JSON_IA))).generador_ia(_contexto(humanos=["CASO: x"]))
    assert data["resumen"]["uptime"] == "99,50%"
    assert data["incidencias"]["externas"] == 1
    assert data["incidencias"]["internas"] == 0          # informe de cliente: los picos de RAM no cuentan
    assert data["cifras"]["eventos_por_tipo"] == {"ram_vm_alto": 1}
    assert data["meta"]["modelo"] == "gemini-prueba-001"


def test_reintentos_cuentan_por_tipo_de_fallo(tmp_path):
    """
    red, respuesta inválida, red, OK: 3 fallos en total, pero 2 de red (tope 3) y 1 de
    respuesta (tope 2): no se agota ninguna política. Con un único contador se cortaba
    en el tercer fallo.
    """
    secuencia = [FalloRed("caída"), RespuestaInvalida("faltan claves"), FalloRed("caída"), dict(_JSON_IA)]

    def generador(_contexto):
        r = secuencia.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    servicio = ServicioReportes(AlmacenReportes(tmp_path / "h.sqlite"), generador,
                                render_pdf=lambda d, g: b"", generar_grafico=lambda p: None)
    rid = servicio.solicitar_reporte({"hospital_id": HOSPITAL, "secciones_a_mostrar": []})
    servicio.procesar(rid)
    rep = servicio.almacen.obtener(rid)
    assert rep.estado == Estado.FINALIZADO.value
    assert rep.intentos == 3


# --- 7 y F. Asana ---------------------------------------------------------------------------

def test_tarea_de_continuacion_es_automatica():
    notas = ("🔁 CONTINUACIÓN: esta tarea se abrió porque la anterior alcanzó 1000 comentarios y ya "
             "está finalizada. Tarea anterior: https://app.asana.com/0/0/1204567890123456\n\n"
             "INCIDENTE DETECTADO - TECNOMONITOR V3\n\n        🏥 Hospital: H05")
    assert asana_client._es_automatico({"notes": notas})
    assert not asana_client._es_automatico({"notes": "El cliente pide revisar el PACS"})


def test_fechas_del_periodo_en_hora_argentina():
    dt = asana_client._parse_fecha("2026-10-01 00:00:00", ZoneInfo("America/Argentina/Buenos_Aires"))
    assert dt == datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)


def test_anonimiza_datos_personales_y_respeta_datos_tecnicos():
    texto = ("DNI 30.123.456, cuil 20-30123456-7, juan.perez@hospital.gob.ar, tel 0261 4123456. "
             "IP 10.0.0.110 puerto 29200, 1500 estudios, VM ARP19APPV, 27/10 10:30")
    limpio = asana_client.anonimizar(texto)
    for dato in ("30.123.456", "30123456", "juan.perez", "4123456"):
        assert dato not in limpio
    for dato in ("10.0.0.110", "29200", "1500 estudios", "ARP19APPV", "27/10 10:30"):
        assert dato in limpio


# --- A. plantilla ---------------------------------------------------------------------------

@pytest.mark.parametrize("tipo", ["cliente", "interno"])
def test_plantilla_muestra_cifras_del_codigo(tipo):
    resumen = {"periodo": {"desde": DESDE, "hasta": HASTA},
               "infraestructura": {"uptime_pct": 75.0, "eventos": [],
                                   "metricas": {"cpu_host": {"avg": 12.5}, "ram_host": {"avg": 50.0}}},
               "control_calidad": ["Uptime de 75.0%: verificar"]}
    data = ai_report.completar_con_cifras(json.loads(json.dumps(_JSON_IA)), resumen, {"humanos": []})
    data["meta"]["tipo"] = tipo
    html = render.renderizar_html(data)
    assert "75,00%" in html and "12.5%" in html and "2026-10-01 al 2026-10-01" in html
    assert ("Control de calidad" in html) == (tipo == "interno")
