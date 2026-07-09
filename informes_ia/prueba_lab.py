"""
Prueba de flujo COMPLETO en laboratorio.

Corre el flujo asíncrono de punta a punta con el pipeline REAL:
  pre-procesamiento (lab_monitor.db) → Gemini → gráfico PNG → PDF reconstruido,
pasando por toda la máquina de estados y guardando en la DB propia del módulo.

Está desconectado de la interfaz principal: lo dispara este script, no el
dashboard. El worker tampoco es real: se llama procesar() en el momento.

Requisitos (en el .env o el entorno):
  - GEMINI_API_KEY
  - lab_monitor.db accesible (INFORMES_IA_BASE_DIR)

Uso:
  python -m informes_ia.prueba_lab --hospital P03 \\
      --desde "2026-07-06 00:00:00" --hasta "2026-07-08 00:00:00" --tipo cliente
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, get_settings
from .historial.almacen import AlmacenReportes
from .historial.estados import Estado
from .historial.servicio import EstadoInvalido, ServicioReportes
from .logging_utils import get_logger
from .pipeline_real import PipelineReal

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Prueba de flujo completo en laboratorio")
    p.add_argument("--hospital", required=True)
    p.add_argument("--desde", required=True, help="'YYYY-MM-DD HH:MM:SS'")
    p.add_argument("--hasta", required=True, help="'YYYY-MM-DD HH:MM:SS'")
    p.add_argument("--tipo", choices=["cliente", "interno"], default="cliente")
    p.add_argument("--db-historial", default="reportes_db.sqlite",
                   help="Ruta de la DB propia del módulo (default: reportes_db.sqlite)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # 1. Validar entorno antes de tocar nada
    try:
        settings = get_settings()
    except ConfigError as e:
        log.error("Config: %s", e)
        return 1
    if not settings.db_path.exists():
        log.error("No se encontró lab_monitor.db en %s", settings.db_path)
        return 1

    print(f"\n{'='*70}\nPRUEBA DE FLUJO COMPLETO — LAB\n{'='*70}")
    print(f"  hospital     : {args.hospital}")
    print(f"  rango        : {args.desde}  →  {args.hasta}")
    print(f"  tipo         : {args.tipo}")
    print(f"  base datos   : {settings.db_path}")
    print(f"  db historial : {args.db_historial}")

    # 2. Montar la capa async con el pipeline REAL inyectado
    almacen = AlmacenReportes(Path(args.db_historial))
    pipeline = PipelineReal(settings)
    svc = ServicioReportes(
        almacen,
        generador_ia=pipeline.generador_ia,
        render_pdf=pipeline.render_pdf,
        generar_grafico=pipeline.generar_grafico_png,
        preparar_contexto=pipeline.preparar_contexto,
        backoff_base_s=2.0,   # backoff si la API no indica retry_delay
    )

    # 3. La petición (como la mandaría el dashboard)
    peticion = {
        "hospital_id": args.hospital,
        "fecha_inicio": args.desde,
        "fecha_fin": args.hasta,
        "tipo_reporte": args.tipo,
        "fuentes_a_procesar": {"infraestructura": True, "clinico": True, "software": True},
        "secciones_a_mostrar": ["infraestructura", "incidencias", "calidad", "recomendacion"],
        "texto_orientativo": "",
    }

    # 4. FLUJO: solicitar → (worker simulado) procesar → obtener
    usuario = {"id": "ernesto", "nombre": "Ernesto", "rol": "admin"}

    print(f"\n{'─'*70}\n[1] solicitar_reporte()\n{'─'*70}")
    report_id = svc.solicitar_reporte(peticion, solicitado_por=usuario)
    print(f"  report_id : {report_id}")
    print(f"  estado    : {svc.consultar_estado(report_id)}  (esperado: en_espera)")

    print(f"\n{'─'*70}\n[2] procesar()  ← acá corre el pipeline real (puede tardar)\n{'─'*70}")
    svc.procesar(report_id)

    estado_final = svc.consultar_estado(report_id)
    rep = almacen.obtener(report_id)
    print(f"\n{'─'*70}\n[3] resultado\n{'─'*70}")
    print(f"  estado final : {estado_final}")
    print(f"  intentos     : {rep.intentos}")
    if rep.error_mensaje:
        print(f"  error        : {rep.error_mensaje}")

    if estado_final != Estado.FINALIZADO.value:
        print("\n⚠️  El reporte no finalizó. Revisá el error de arriba y los logs.")
        return 1

    # 5. Revisión humana: el JSON es un BORRADOR hasta que se apruebe
    print(f"\n{'─'*70}\n[4] revisión: obtener_json() → (editar) → aprobar()\n{'─'*70}")
    data = svc.obtener_json(report_id)
    print(f"  JSON recuperado. Claves: {list(data.keys())}")
    print(f"  resumen.texto (IA): {str(data.get('resumen', {}).get('texto'))[:70]}...")

    # el PDF todavía no está disponible: es un borrador
    try:
        svc.obtener_reporte(report_id)
        print("  ⚠️  ERROR: no debería dar PDF de un borrador")
        return 1
    except EstadoInvalido:
        print("  PDF de borrador: bloqueado correctamente (falta aprobar)")

    # acá la app principal mostraría el JSON al usuario para editarlo.
    # Simulamos una corrección de texto:
    if "resumen" in data:
        data["resumen"]["texto"] = (data["resumen"].get("texto", "") + " [revisado]")
        svc.guardar_json_editado(report_id, data)
        print("  guardar_json_editado(): JSON actualizado (estado no cambia)")

    aprobador = {"id": "sofia", "nombre": "Sofía", "rol": "gerente"}
    svc.aprobar(report_id, aprobado_por=aprobador)
    print(f"  aprobar(): estado -> {svc.consultar_estado(report_id)}")

    # 6. Reconstruir el PDF desde la DB (sin re-llamar a la IA)
    print(f"\n{'─'*70}\n[5] obtener_reporte()  ← reconstruye PDF desde lo guardado\n{'─'*70}")
    pdf_bytes = svc.obtener_reporte(report_id)
    salida = Path(f"prueba_lab_{args.hospital}_{args.tipo}.pdf")
    salida.write_bytes(pdf_bytes)
    print(f"  PDF reconstruido: {len(pdf_bytes)} bytes → {salida.resolve()}")

    print(f"\n{'='*70}\n✓ FLUJO COMPLETO OK — estado={estado_final}\n{'='*70}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
