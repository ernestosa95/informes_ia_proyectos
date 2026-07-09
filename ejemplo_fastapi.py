"""
EJEMPLO de integración del módulo en una aplicación FastAPI.

No es parte del módulo: es una referencia de cómo la aplicación principal
lo consume, con el worker corriendo como thread embebido.

  pip install fastapi uvicorn
  uvicorn ejemplo_fastapi:app --reload
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Response

from informes_ia.config import get_settings
from informes_ia.historial.almacen import AlmacenReportes
from informes_ia.historial.servicio import EstadoInvalido, ServicioReportes
from informes_ia.pipeline_real import PipelineReal
from informes_ia.worker import WorkerReportes

# ── construcción del servicio (una vez, al arrancar) ─────────────────────

settings = get_settings()
pipeline = PipelineReal(settings)
almacen = AlmacenReportes(Path("reportes_db.sqlite"))

servicio = ServicioReportes(
    almacen,
    generador_ia=pipeline.generador_ia,
    render_pdf=pipeline.render_pdf,
    generar_grafico=pipeline.generar_grafico_png,
    preparar_contexto=pipeline.preparar_contexto,
    backoff_base_s=2.0,
)

worker = WorkerReportes(servicio)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # arranca el worker con la app; lo detiene ordenadamente al apagar
    worker.iniciar_en_thread()
    yield
    worker.detener()


app = FastAPI(lifespan=lifespan)


# ── helpers ──────────────────────────────────────────────────────────────

def _usuario_actual() -> dict[str, Any]:
    """
    En tu app real, esto sale de la sesión/JWT.
    El módulo NO autentica: confía en lo que le pases.
    """
    return {"id": "ernesto", "nombre": "Ernesto", "rol": "admin"}


# ── endpoints ────────────────────────────────────────────────────────────

@app.post("/reportes", status_code=202)
def solicitar(peticion: dict[str, Any] = Body(...)):
    """Encola un reporte. Devuelve el id al instante (202 Accepted)."""
    report_id = servicio.solicitar_reporte(peticion, solicitado_por=_usuario_actual())
    return {"report_id": report_id, "estado": servicio.consultar_estado(report_id)}


@app.get("/reportes/{report_id}/estado")
def estado(report_id: str):
    est = servicio.consultar_estado(report_id)
    if est is None:
        raise HTTPException(404, "No existe el reporte")
    return {"report_id": report_id, "estado": est}


@app.get("/reportes/{report_id}/json")
def obtener_json(report_id: str):
    """El JSON para que el usuario lo revise/edite."""
    try:
        return servicio.obtener_json(report_id)
    except KeyError:
        raise HTTPException(404, "No existe el reporte")
    except EstadoInvalido as e:
        raise HTTPException(409, str(e))


@app.put("/reportes/{report_id}/json")
def guardar_json(report_id: str, data: dict[str, Any] = Body(...)):
    """Guarda las correcciones del usuario. Se puede llamar varias veces."""
    try:
        servicio.guardar_json_editado(report_id, data)
    except KeyError:
        raise HTTPException(404, "No existe el reporte")
    except EstadoInvalido as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/reportes/{report_id}/aprobar")
def aprobar(report_id: str):
    """Aprueba el borrador. A partir de acá es inmutable y hay PDF."""
    try:
        servicio.aprobar(report_id, aprobado_por=_usuario_actual())
    except KeyError:
        raise HTTPException(404, "No existe el reporte")
    except EstadoInvalido as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "estado": servicio.consultar_estado(report_id)}


@app.get("/reportes/{report_id}/pdf")
def descargar_pdf(report_id: str):
    """Reconstruye el PDF. Sólo funciona si el reporte está APROBADO."""
    try:
        pdf = servicio.obtener_reporte(report_id)
    except KeyError:
        raise HTTPException(404, "No existe el reporte")
    except EstadoInvalido as e:
        raise HTTPException(409, str(e))   # ej. todavía es un borrador
    return Response(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="reporte_{report_id[:8]}.pdf"'},
    )


@app.get("/reportes")
def listar(usuario_id: str | None = None):
    """Historial. Opcionalmente filtrado por quien lo solicitó."""
    reportes = (
        almacen.listar_por_solicitante(usuario_id) if usuario_id else almacen.listar()
    )
    return [
        {
            "report_id": r.report_id,
            "estado": r.estado,
            "creado_en": r.creado_en,
            "solicitado_por": r.solicitado_por,
            "aprobado_por": r.aprobado_por,
            "error": r.error_mensaje,
        }
        for r in reportes
    ]
