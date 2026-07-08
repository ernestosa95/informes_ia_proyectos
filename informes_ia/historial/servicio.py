"""
Orquestador del flujo asíncrono (prototipo).

Expone las tres operaciones del doc 07:
    solicitar_reporte(peticion) -> report_id     (inmediato)
    consultar_estado(report_id) -> str
    obtener_reporte(report_id)  -> bytes (PDF)

En este PROTOTIPO no hay worker real: `procesar(report_id)` se llama a mano
(o desde el test) para simular lo que hará el worker en producción. Eso
permite ver la máquina de estados moverse sin comprometer todavía la
decisión de infraestructura (thread / cron / cola).

La llamada a la IA está inyectada como dependencia (`generador_ia`), así el
prototipo corre sin API real de Gemini y puede simular fallos para ejercitar
los reintentos.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Protocol

from ..logging_utils import get_logger
from .almacen import AlmacenReportes
from .estados import Estado, TipoFallo, max_intentos
from .validacion import RespuestaInvalida, validar_estructura

log = get_logger(__name__)


class FalloRed(Exception):
    """No llegó respuesta usable de la IA (timeout, rate limit, API caída)."""

    def __init__(self, mensaje: str, retry_delay_s: float | None = None):
        super().__init__(mensaje)
        # Segundos que la API pide esperar antes de reintentar (ej. 429 de Gemini).
        # Si viene, se respeta en vez del backoff fijo.
        self.retry_delay_s = retry_delay_s


class PreparadorContexto(Protocol):
    """
    Fase que corre UNA vez por reporte (no se reintenta): lee la DB,
    pre-procesa, arma el resumen. Devuelve un 'contexto' opaco que se le
    pasa después al generador de IA. Puede lanzar RespuestaInvalida para
    casos que no tiene sentido reintentar (hospital inexistente, sin datos).
    """
    def __call__(self, peticion: dict[str, Any]) -> Any: ...


class GeneradorIA(Protocol):
    """
    Contrato de la pieza que llama a la IA. Recibe el contexto ya preparado
    (no la petición cruda), así NO re-preprocesa en cada reintento.

    Debe devolver un dict (ya parseado). Puede lanzar:
      - FalloRed          → política de red (3 intentos)
      - RespuestaInvalida → política de contenido (2 intentos)
    """
    def __call__(self, contexto: Any) -> dict[str, Any]: ...


class ServicioReportes:
    def __init__(
        self,
        almacen: AlmacenReportes,
        generador_ia: GeneradorIA,
        *,
        render_pdf: Callable[[dict[str, Any], bytes | None], bytes],
        generar_grafico: Callable[[dict[str, Any]], bytes | None] | None = None,
        preparar_contexto: PreparadorContexto | None = None,
        backoff_base_s: float = 0.0,  # 0 en tests; >0 en producción
    ):
        self.almacen = almacen
        self.generador_ia = generador_ia
        self.render_pdf = render_pdf
        self.generar_grafico = generar_grafico or self._simular_grafico
        # Si no se inyecta preparador, el contexto ES la petición (compat. prototipo).
        self.preparar_contexto = preparar_contexto or (lambda peticion: peticion)
        self.backoff_base_s = backoff_base_s

    # ── API pública (doc 07) ─────────────────────────────────────────────

    def solicitar_reporte(self, peticion: dict[str, Any]) -> str:
        """Crea el reporte y lo deja encolado. Devuelve el report_id al instante."""
        report_id = self.almacen.crear(peticion)
        self.almacen.set_estado(report_id, Estado.EN_ESPERA)
        return report_id

    def consultar_estado(self, report_id: str) -> str | None:
        rep = self.almacen.obtener(report_id)
        return rep.estado if rep else None

    def obtener_reporte(self, report_id: str) -> bytes:
        """Reconstruye el PDF desde data_json + grafico_png. No re-llama a la IA."""
        rep = self.almacen.obtener(report_id)
        if rep is None:
            raise KeyError(f"No existe el reporte {report_id}")
        if rep.estado != Estado.FINALIZADO.value:
            raise ValueError(f"El reporte {report_id} no está finalizado (estado={rep.estado})")
        grafico = self.almacen.obtener_grafico(report_id)
        return self.render_pdf(rep.data_json, grafico)

    # ── procesamiento (lo hará el worker; acá se llama a mano) ────────────

    def procesar(self, report_id: str) -> None:
        """
        Toma un reporte encolado y lo lleva hasta FINALIZADO o ERROR.

        Fase 1 (una vez, NO se reintenta): preparar el contexto — leer DB,
                pre-procesar, armar el resumen. Es lo caro (miles de filas).
        Fase 2 (reintentable): llamar a la IA sobre el contexto ya listo.
        """
        rep = self.almacen.obtener(report_id)
        if rep is None:
            log.error("procesar(): no existe %s", report_id)
            return

        self.almacen.set_estado(report_id, Estado.EN_PROCESO)
        secciones = set(rep.peticion.get("secciones_a_mostrar", []))

        # ── Fase 1: preparación (una sola vez) ──────────────────────────
        try:
            contexto = self.preparar_contexto(rep.peticion)
        except RespuestaInvalida as e:
            # Casos que no tiene sentido reintentar: hospital inexistente,
            # sin datos para el período, etc.
            self.almacen.set_estado(report_id, Estado.ERROR,
                                    error_mensaje=f"preparacion: {e}")
            return
        except Exception as e:
            self.almacen.set_estado(report_id, Estado.ERROR,
                                    error_mensaje=f"preparacion falló: {e}")
            return

        # ── Fase 2: llamada a la IA (con reintentos) ────────────────────
        try:
            data_json = self._intentar_con_reintentos(report_id, contexto, secciones)
        except _AgotadoError as e:
            self.almacen.set_estado(report_id, Estado.ERROR, error_mensaje=str(e))
            return

        # Éxito: generar y guardar el gráfico.
        grafico_png = self.generar_grafico(rep.peticion)
        self.almacen.guardar_resultado(report_id, data_json, grafico_png)

    def _intentar_con_reintentos(
        self, report_id: str, contexto: Any, secciones: set[str]
    ) -> dict[str, Any]:
        """
        Ejecuta la llamada a la IA + validación sobre el contexto YA preparado,
        con reintentos cuya cantidad depende del tipo de fallo. Devuelve el
        data_json válido o lanza _AgotadoError.
        """
        while True:
            try:
                data = self.generador_ia(contexto)       # puede lanzar FalloRed
                validar_estructura(data, secciones)       # puede lanzar RespuestaInvalida
                return data

            except (FalloRed, RespuestaInvalida) as e:
                tipo = TipoFallo.RED if isinstance(e, FalloRed) else TipoFallo.RESPUESTA_INVALIDA
                tope = max_intentos(tipo)
                n = self.almacen.incrementar_intentos(report_id)  # nº de intentos totales hechos

                if n >= tope:
                    msg = f"{tipo.value}: {e} (agotado: {n}/{tope} intentos)"
                    log.warning("Reporte %s -> %s", report_id, msg)
                    raise _AgotadoError(msg) from e

                self.almacen.set_estado(report_id, Estado.REINTENTANDO,
                                        error_mensaje=f"{tipo.value}: {e}")
                self._esperar_antes_de_reintentar(e, n)

    def _esperar_antes_de_reintentar(self, error: Exception, intento_n: int) -> None:
        """
        Decide cuánto esperar. Si la API indicó un retry_delay (ej. 429 de
        Gemini con 'retry in 52s'), se respeta. Si no, backoff exponencial.
        """
        retry_delay = getattr(error, "retry_delay_s", None)
        if retry_delay:
            log.info("La API pidió esperar %.0fs antes de reintentar", retry_delay)
            time.sleep(retry_delay)
        elif self.backoff_base_s:
            time.sleep(self.backoff_base_s * (2 ** (intento_n - 1)))

    # ── helpers de simulación (reemplazables por lo real) ────────────────

    def _simular_grafico(self, peticion: dict[str, Any]) -> bytes:
        # En producción: db.generar_grafico_historico(...) y leer el PNG.
        return b"\x89PNG\r\n\x1a\n" + b"FAKE-PNG-BYTES"


class _AgotadoError(Exception):
    pass
