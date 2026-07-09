"""
Worker: el proceso que toma los reportes encolados y los procesa.

Sin esta pieza el flujo asíncrono no funciona: `solicitar_reporte()` deja el
reporte en EN_ESPERA y nadie lo levanta. El worker es ese "alguien".

Diseñado para correr de DOS formas, con el mismo código:

  1) Como THREAD dentro de la aplicación principal (lo más simple, y lo
     recomendado para un servidor con una sola instancia de la app):

        from informes_ia.worker import WorkerReportes
        worker = WorkerReportes(servicio)
        worker.iniciar_en_thread()      # no bloquea
        ...
        worker.detener()                # al apagar la app

  2) Como PROCESO APARTE (systemd, docker, etc.), si mañana conviene
     desacoplarlo de la app:

        python -m informes_ia.worker

En ambos casos la lógica es idéntica. Cambiar de una a otra no toca este
archivo.

Garantías
---------
- **Claim atómico**: `almacen.reclamar_siguiente()` pasa EN_ESPERA -> EN_PROCESO
  con un UPDATE condicional. Si hubiera varios workers, sólo uno gana el
  reporte. Hoy hay uno solo, pero la protección es gratis.
- **Recuperación de huérfanos**: al arrancar, los reportes que quedaron en
  EN_PROCESO (porque el worker murió a mitad) se marcan ERROR. Ver doc 07.
- **Apagado ordenado**: ante SIGTERM/SIGINT (o `detener()`), termina el
  reporte en curso y recién ahí sale. No deja huérfanos por un apagado limpio.
"""
from __future__ import annotations

import signal
import threading
import time

from .historial.servicio import ServicioReportes
from .logging_utils import get_logger

log = get_logger(__name__)

INTERVALO_SONDEO_S = 5.0


class WorkerReportes:
    """
    Bucle que consume la cola de reportes. Agnóstico de cómo se lo ejecute
    (thread o proceso): sólo sabe reclamar, procesar y repetir.
    """

    def __init__(
        self,
        servicio: ServicioReportes,
        *,
        intervalo_sondeo_s: float = INTERVALO_SONDEO_S,
        recuperar_huerfanos_al_iniciar: bool = True,
    ):
        self.servicio = servicio
        self.intervalo_sondeo_s = intervalo_sondeo_s
        self.recuperar_huerfanos_al_iniciar = recuperar_huerfanos_al_iniciar

        self._detener = threading.Event()
        self._thread: threading.Thread | None = None

    # ── ciclo de vida ────────────────────────────────────────────────────

    def iniciar_en_thread(self) -> None:
        """Arranca el worker en un hilo daemon. No bloquea. Para embeber en la app."""
        if self._thread is not None and self._thread.is_alive():
            log.warning("El worker ya está corriendo")
            return
        self._detener.clear()
        self._thread = threading.Thread(target=self.correr, name="worker-reportes", daemon=True)
        self._thread.start()
        log.info("Worker iniciado en thread")

    def detener(self, timeout_s: float = 30.0) -> None:
        """
        Pide al worker que termine. Si está procesando un reporte, espera a
        que lo termine (hasta `timeout_s`) para no dejarlo huérfano.
        """
        log.info("Deteniendo worker (se espera a que termine el reporte en curso)...")
        self._detener.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            if self._thread.is_alive():
                log.warning("El worker no terminó en %.0fs; el reporte en curso quedará huérfano", timeout_s)
        log.info("Worker detenido")

    # ── el bucle ─────────────────────────────────────────────────────────

    def correr(self) -> None:
        """Bucle principal. Bloquea hasta que se llame `detener()`."""
        if self.recuperar_huerfanos_al_iniciar:
            huerfanos = self.servicio.almacen.recuperar_huerfanos()
            if huerfanos:
                log.warning(
                    "%d reporte(s) huérfano(s) marcados como error al arrancar. "
                    "Se pueden volver a solicitar a mano.", len(huerfanos)
                )

        log.info("Worker corriendo (sondeo cada %.0fs)", self.intervalo_sondeo_s)
        while not self._detener.is_set():
            try:
                procesado = self._procesar_uno()
            except Exception:
                # Un fallo acá no debe matar el worker: se loguea y sigue.
                log.exception("Error inesperado en el bucle del worker")
                procesado = False

            if not procesado:
                # cola vacía: esperar antes de volver a sondear.
                # `wait` en vez de `sleep` para reaccionar rápido a detener().
                self._detener.wait(self.intervalo_sondeo_s)

    def _procesar_uno(self) -> bool:
        """
        Reclama y procesa un reporte. Devuelve True si procesó alguno
        (así el bucle sigue de largo sin esperar, por si hay más en cola).
        """
        report_id = self.servicio.almacen.reclamar_siguiente()
        if report_id is None:
            return False

        inicio = time.monotonic()
        try:
            self.servicio.procesar(report_id)
        except Exception:
            # `procesar()` ya maneja sus errores y los deja en estado ERROR.
            # Esto es una red de seguridad por si algo se le escapa.
            log.exception("procesar(%s) lanzó una excepción no controlada", report_id)
        else:
            dur = time.monotonic() - inicio
            estado = self.servicio.consultar_estado(report_id)
            log.info("Reporte %s procesado en %.1fs -> %s", report_id, dur, estado)
        return True


# ── ejecución como proceso aparte ────────────────────────────────────────

def _construir_servicio() -> ServicioReportes:
    """Arma el servicio con el pipeline real, para el modo standalone."""
    from pathlib import Path

    from .config import get_settings
    from .historial.almacen import AlmacenReportes
    from .pipeline_real import PipelineReal

    settings = get_settings()
    pipeline = PipelineReal(settings)
    almacen = AlmacenReportes(Path("reportes_db.sqlite"))
    return ServicioReportes(
        almacen,
        generador_ia=pipeline.generador_ia,
        render_pdf=pipeline.render_pdf,
        generar_grafico=pipeline.generar_grafico_png,
        preparar_contexto=pipeline.preparar_contexto,
        backoff_base_s=2.0,
    )


def main() -> int:
    """Punto de entrada standalone:  python -m informes_ia.worker"""
    from .config import ConfigError

    try:
        servicio = _construir_servicio()
    except ConfigError as e:
        log.error("Config: %s", e)
        return 1

    worker = WorkerReportes(servicio)

    def _apagar(signum, _frame):
        log.info("Señal %s recibida", signal.Signals(signum).name)
        worker._detener.set()

    signal.signal(signal.SIGTERM, _apagar)
    signal.signal(signal.SIGINT, _apagar)

    worker.correr()   # bloquea hasta la señal
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
