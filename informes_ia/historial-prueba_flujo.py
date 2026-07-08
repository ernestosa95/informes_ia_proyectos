"""
Prueba de flujo del prototipo de historial / máquina de estados.

No usa Gemini real ni lab_monitor.db: inyecta un generador IA simulado que
puede devolver JSON válido, JSON incompleto, o simular caídas de red, para
ejercitar los tres desenlaces y las dos políticas de reintento.

Correr:  python -m informes_ia.historial.prueba_flujo
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .almacen import AlmacenReportes
from .estados import Estado
from .servicio import FalloRed, ServicioReportes
from .validacion import RespuestaInvalida


# ── dobles de prueba ─────────────────────────────────────────────────────

def _json_completo(peticion: dict[str, Any]) -> dict[str, Any]:
    """Simula una IA que responde bien, respetando las secciones pedidas."""
    data = {
        "meta": {"tipo": peticion.get("tipo_reporte", "cliente"), "fecha": "08/07/2026",
                 "hospital": peticion.get("hospital_id", "?")},
        "resumen": {"uptime": "99.7%", "texto": "Todo estable."},
    }
    for sec in peticion.get("secciones_a_mostrar", []):
        if sec not in data:
            data[sec] = {"_": f"contenido de {sec}"}
    return data


def _render_fake(data: dict[str, Any], grafico: bytes | None) -> bytes:
    return f"PDF[{data['meta']['hospital']}|grafico={'sí' if grafico else 'no'}]".encode()


def _sep(t): print(f"\n{'='*66}\n{t}\n{'='*66}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    almacen = AlmacenReportes(tmp / "reportes_db.sqlite")

    peticion_base = {
        "hospital_id": "P03",
        "fecha_inicio": "2026-07-06 00:00:00",
        "fecha_fin": "2026-07-08 00:00:00",
        "tipo_reporte": "cliente",
        "fuentes_a_procesar": {"infraestructura": True, "clinico": True, "software": True},
        "secciones_a_mostrar": ["infraestructura", "incidencias", "calidad", "recomendacion"],
        "texto_orientativo": "Enfocar en estabilidad.",
    }

    # ── Caso 1: camino feliz ─────────────────────────────────────────────
    _sep("CASO 1 — Camino feliz (solicitar → procesar → obtener)")
    svc = ServicioReportes(almacen, _json_completo, render_pdf=_render_fake)
    rid = svc.solicitar_reporte(peticion_base)
    print(f"solicitar_reporte() devolvió id inmediato: {rid}")
    print(f"estado tras solicitar: {svc.consultar_estado(rid)}  (esperado: en_espera)")
    svc.procesar(rid)
    print(f"estado tras procesar:  {svc.consultar_estado(rid)}  (esperado: finalizado)")
    pdf = svc.obtener_reporte(rid)
    print(f"obtener_reporte() reconstruyó PDF: {pdf!r}")
    assert svc.consultar_estado(rid) == Estado.FINALIZADO.value
    assert b"P03" in pdf and b"grafico=s" in pdf

    # ── Caso 2: fallo de red que se recupera al 3er intento ──────────────
    _sep("CASO 2 — Red inestable: falla 2 veces, éxito al 3ro (política red=3)")
    intentos = {"n": 0}
    def ia_red_intermitente(pet):
        intentos["n"] += 1
        if intentos["n"] < 3:
            raise FalloRed(f"timeout simulado #{intentos['n']}")
        return _json_completo(pet)
    svc2 = ServicioReportes(almacen, ia_red_intermitente, render_pdf=_render_fake)
    rid2 = svc2.solicitar_reporte(peticion_base)
    svc2.procesar(rid2)
    rep2 = almacen.obtener(rid2)
    print(f"estado final: {rep2.estado}  (esperado: finalizado)")
    print(f"intentos totales registrados: {rep2.intentos}  (esperado: 2 fallos)")
    assert rep2.estado == Estado.FINALIZADO.value

    # ── Caso 3: red siempre caída → agota 3 intentos y queda en error ────
    _sep("CASO 3 — Red siempre caída: agota política (3 intentos) → error terminal")
    def ia_red_muerta(pet):
        raise FalloRed("API caída")
    svc3 = ServicioReportes(almacen, ia_red_muerta, render_pdf=_render_fake)
    rid3 = svc3.solicitar_reporte(peticion_base)
    svc3.procesar(rid3)
    rep3 = almacen.obtener(rid3)
    print(f"estado final: {rep3.estado}  (esperado: error)")
    print(f"intentos: {rep3.intentos}  (esperado: 3 intentos totales)")
    print(f"error_mensaje: {rep3.error_mensaje}")
    assert rep3.estado == Estado.ERROR.value
    assert rep3.intentos == 3
    assert "red" in rep3.error_mensaje

    # ── Caso 4: JSON incompleto (faltan claves) → política contenido=2 ──
    _sep("CASO 4 — JSON válido pero falta 'recomendacion' → inválido (2 intentos) → error")
    def ia_incompleta(pet):
        d = _json_completo(pet)
        d.pop("recomendacion", None)   # le sacamos una clave pedida
        return d
    svc4 = ServicioReportes(almacen, ia_incompleta, render_pdf=_render_fake)
    rid4 = svc4.solicitar_reporte(peticion_base)
    svc4.procesar(rid4)
    rep4 = almacen.obtener(rid4)
    print(f"estado final: {rep4.estado}  (esperado: error)")
    print(f"error_mensaje: {rep4.error_mensaje}")
    print(f"intentos: {rep4.intentos}  (esperado: 2 intentos totales)")
    assert rep4.estado == Estado.ERROR.value
    assert rep4.intentos == 2
    assert "respuesta_invalida" in rep4.error_mensaje
    assert "recomendacion" in rep4.error_mensaje

    # ── Caso 5: obtener_reporte sobre uno no finalizado → error controlado ─
    _sep("CASO 5 — obtener_reporte() sobre uno en error → ValueError controlado")
    try:
        svc4.obtener_reporte(rid4)
        print("ERROR: debería haber lanzado")
        return 1
    except ValueError as e:
        print(f"lanzó ValueError esperado: {e}")

    # ── Historial ────────────────────────────────────────────────────────
    _sep("HISTORIAL — todos los reportes registrados")
    for r in almacen.listar():
        print(f"  {r.report_id[:8]}…  estado={r.estado:11}  intentos={r.intentos}  "
              f"data={'sí' if r.data_json else 'no':3}  error={r.error_mensaje or '-'}")

    _sep("TODOS LOS ASSERTS PASARON ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
