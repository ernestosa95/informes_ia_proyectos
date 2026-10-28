"""
EventTracker: recorre la serie temporal de snapshots UNA sola vez y agrupa
cruces de umbral consecutivos en un solo evento con inicio/fin/duración.

Esto reemplaza directamente el parche que tenía el script original:

    if incidentes > 50: incidentes = 1  # Ajuste para no contar cada minuto...

En vez de contar muestras y después "corregir" el número mágicamente, acá
un fallo continuo de 3 horas es UN evento con duracion_min=180, calculado
desde el diseño, no parcheado después.

Los umbrales se reciben desde `ConfiguracionGlobal` (tabla `configuracion`),
nunca hardcodeados.

UPTIME = COBERTURA EN EL TIEMPO. Es el % del período en que el hospital
estuvo reportando: un hueco entre dos reportes (o al principio/fin del
período) mayor que `offline_minutes` cuenta entero como caída, el mismo
umbral con el que el server declara un hospital OFFLINE. Antes se contaban
las filas con host_status == "online", pero esa columna guarda el estado de
salud del iDRAC ("OK", "Warning"...): el uptime daba siempre 0 % y la IA lo
"explicaba" inventando causas (informe de P03, 2026-07).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .conexion import a_datetime, a_dict
from .config_dinamica import ConfiguracionGlobal
from .logging_utils import get_logger
from .normalizacion import SnapshotNormalizado, parse_snapshot

# state_reason de una VM que responde pero no se pudo leer (alerts_engine/infra.py del server).
MOTIVOS_ACCESO = {"wmi_error", "ssh_error"}

log = get_logger(__name__)


@dataclass
class Evento:
    tipo: str
    entidad: str
    severidad: str
    inicio: datetime
    fin: datetime | None
    detalle: dict[str, Any] = field(default_factory=dict)

    @property
    def duracion_min(self) -> float | None:
        if self.fin is None:
            return None
        return round((self.fin - self.inicio).total_seconds() / 60, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tipo": self.tipo,
            "entidad": self.entidad,
            "severidad": self.severidad,
            "inicio": self.inicio.isoformat(),
            "fin": self.fin.isoformat() if self.fin else None,
            "duracion_min": self.duracion_min,
            "detalle": {k: v for k, v in self.detalle.items() if k != "_peor"},
        }


class _RastreadorUmbral:
    """
    Trackea un único evento abierto por clave (ej. por sensor, por VM, por
    disco). `evaluar()` se llama una vez por snapshot; internamente decide
    si abre, extiende o cierra el evento.
    """

    def __init__(self, tipo: str, severidad: str):
        self.tipo = tipo
        self.severidad = severidad
        self._abiertos: dict[str, Evento] = {}
        self.cerrados: list[Evento] = []

    def evaluar(self, clave: str, en_falla: bool, ts: datetime, detalle: dict[str, Any]) -> None:
        abierto = self._abiertos.get(clave)
        if en_falla:
            if abierto is None:
                self._abiertos[clave] = Evento(
                    tipo=self.tipo, entidad=clave, severidad=self.severidad,
                    inicio=ts, fin=ts, detalle=detalle,
                )
            else:
                abierto.fin = ts
                abierto.detalle = detalle
        else:
            if abierto is not None:
                self.cerrados.append(abierto)
                del self._abiertos[clave]

    def finalizar(self) -> list[Evento]:
        """Cierra los eventos que seguían abiertos al final del período analizado."""
        for ev in self._abiertos.values():
            self.cerrados.append(ev)
        self._abiertos.clear()
        return self.cerrados


def cobertura(
    horas: list[Any], inicio: datetime | None, fin: datetime | None, umbral_min: int
) -> tuple[float | None, list[dict[str, Any]]]:
    """
    (% del período con el hospital reportando, huecos sin reportes, más largos primero).
    Un hueco de más de `umbral_min` entre reportes, o al principio/fin del período,
    cuenta entero como caída. Sin inicio/fin se usan el primer y el último reporte.
    """
    ordenadas = sorted(h.replace(tzinfo=None) for h in (a_datetime(x) for x in horas) if h is not None)
    if not ordenadas:
        return None, []
    desde = inicio or ordenadas[0]
    hasta = min(fin or ordenadas[-1], datetime.now())
    total = (hasta - desde).total_seconds()
    if total <= 0:
        return None, []

    umbral = timedelta(minutes=umbral_min)
    huecos: list[tuple[datetime, datetime]] = []
    anterior = desde
    for h in ordenadas:
        if h < desde or h > hasta:
            continue
        if h - anterior > umbral:
            huecos.append((anterior, h))
        anterior = h
    if hasta - anterior > umbral:
        huecos.append((anterior, hasta))

    caido = sum((b - a).total_seconds() for a, b in huecos)
    pct = round(max(0.0, 100.0 * (1 - caido / total)), 2)
    detalle = [
        {"desde": a.isoformat(timespec="minutes"), "hasta": b.isoformat(timespec="minutes"),
         "duracion_min": round((b - a).total_seconds() / 60)}
        for a, b in sorted(huecos, key=lambda x: x[1] - x[0], reverse=True)
    ]
    return pct, detalle


class EventTracker:
    def __init__(self, config: ConfiguracionGlobal):
        self.config = config
        self._rastreadores: dict[str, _RastreadorUmbral] = {
            "temp_cpu_alta": _RastreadorUmbral("temp_cpu_alta", "media"),
            "temp_ambiente_alta": _RastreadorUmbral("temp_ambiente_alta", "baja"),
            "cpu_host_alto": _RastreadorUmbral("cpu_host_alto", "media"),
            "ram_host_alto": _RastreadorUmbral("ram_host_alto", "media"),
            "cpu_vm_alto": _RastreadorUmbral("cpu_vm_alto", "baja"),
            "ram_vm_alto": _RastreadorUmbral("ram_vm_alto", "baja"),
            "disco_lleno": _RastreadorUmbral("disco_lleno", "alta"),
            "fuente_poder_fallo": _RastreadorUmbral("fuente_poder_fallo", "alta"),
            "ventilador_fallo": _RastreadorUmbral("ventilador_fallo", "alta"),
            "raid_degradado": _RastreadorUmbral("raid_degradado", "alta"),
            "nodo_offline": _RastreadorUmbral("nodo_offline", "alta"),
            "servicio_detenido": _RastreadorUmbral("servicio_detenido", "media"),
        }
        # Muestras crudas para calcular avg/max/p95 al final (no requiere
        # guardar todo el JSON, sólo los escalares que nos interesan).
        self._muestras_cpu_host: list[float] = []
        self._muestras_ram_host: list[float] = []
        self._muestras_temp_cpu: list[float] = []
        self._muestras_latencia: list[float] = []
        self._total_snapshots = 0
        self._alertas_suitestensa_max = 0
        # Hora de cada reporte según la base (la que usa el server), para la cobertura.
        self._horas_reporte: list[datetime] = []
        # Por VM / equipo: [muestras online, muestras totales].
        self._muestras_nodo: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        # En cuántos reportes vino cada tipo de dato (control de calidad).
        self._con_dato: dict[str, int] = defaultdict(int)
        # Por VM: reportes en que no se pudo leer por error de acceso (WMI/SSH).
        self._sin_acceso: dict[str, int] = defaultdict(int)

    def procesar_fila(self, raw_json: Any, host_status: str | None = None, ts_db: Any = None) -> None:
        """
        raw_json: full_json_data (dict en Postgres, texto en SQLite).
        host_status: no se usa para el uptime (es la salud del iDRAC); se conserva por compatibilidad.
        ts_db: columna `timestamp` de la fila; si falta se usa la del envelope.
        """
        raw = a_dict(raw_json)
        if raw is None:
            return

        snap = parse_snapshot(raw)
        hora = a_datetime(ts_db) or snap.timestamp
        if hora is not None:
            self._horas_reporte.append(hora.replace(tzinfo=None))
        self._procesar_snapshot(snap)

    def _procesar_snapshot(self, snap: SnapshotNormalizado) -> None:
        cfg = self.config
        ts = snap.timestamp
        self._total_snapshots += 1
        for clave, hay in (
            ("energia", bool(snap.fuentes_poder)),
            ("temperatura", bool(snap.sensores)),
            ("raid", bool(snap.controladores_raid or snap.volumenes_raid or snap.discos_fisicos)),
            ("red", snap.network_latency_ms is not None),
            ("vms", bool(snap.nodos_virtuales)),
        ):
            if hay:
                self._con_dato[clave] += 1

        if snap.cpu_host_percent is not None:
            self._muestras_cpu_host.append(snap.cpu_host_percent)
            self._rastreadores["cpu_host_alto"].evaluar(
                "host", snap.cpu_host_percent > cfg.cpu_host_max, ts,
                {"valor_pct": snap.cpu_host_percent},
            )
        if snap.ram_host_percent is not None:
            self._muestras_ram_host.append(snap.ram_host_percent)
            self._rastreadores["ram_host_alto"].evaluar(
                "host", snap.ram_host_percent > cfg.ram_host_max, ts,
                {"valor_pct": snap.ram_host_percent},
            )

        for sensor in snap.sensores:
            nombre_lower = sensor.nombre.lower()
            if "cpu" in nombre_lower:
                self._muestras_temp_cpu.append(sensor.valor)
                self._rastreadores["temp_cpu_alta"].evaluar(
                    sensor.nombre, sensor.valor > cfg.temp_cpu_max, ts,
                    {"sensor": sensor.nombre, "temp_c": sensor.valor},
                )
            elif "inlet" in nombre_lower or "ambient" in nombre_lower:
                self._rastreadores["temp_ambiente_alta"].evaluar(
                    sensor.nombre, sensor.valor > cfg.temp_amb_max, ts,
                    {"sensor": sensor.nombre, "temp_c": sensor.valor},
                )

        if cfg.enable_power:
            for fuente in snap.fuentes_poder:
                # NOTA/TODO: en los datos reales, PS2 aparece con watts=0.0 y
                # status="OK" en el 100% de las muestras de varios hospitales:
                # es el patrón típico de una fuente redundante en standby, no
                # un fallo. Por eso NO alertamos por watts=0; alertamos sólo
                # si el propio `status` reportado no es "OK". Confirmar con
                # el proveedor del agente si existe un status más granular
                # (ej. "STANDBY" vs "ABSENT" vs "FAILED") para afinar esto.
                en_falla = fuente.status.upper() != "OK"
                self._rastreadores["fuente_poder_fallo"].evaluar(
                    fuente.nombre, en_falla, ts,
                    {"fuente": fuente.nombre, "watts": fuente.watts, "status": fuente.status},
                )

        if cfg.enable_fans:
            for fan in snap.ventiladores:
                self._rastreadores["ventilador_fallo"].evaluar(
                    fan.nombre, fan.status.upper() != "OK", ts,
                    {"ventilador": fan.nombre, "rpm": fan.valor, "status": fan.status},
                )

        if cfg.enable_raid:
            # Mismo criterio que alerts_engine/infra.py del server: OK u Online es sano;
            # volúmenes sin nombre o discos sin slot son respuestas con error del iDRAC
            # (PMMN 2026-10-03), no elementos reales, y se ignoran.
            for ctrl in snap.controladores_raid:
                status = ctrl.get("status") or ""
                en_falla = bool(status) and status not in ("OK", "Online")
                self._rastreadores["raid_degradado"].evaluar(
                    f"controlador {ctrl.get('name', '?')}", en_falla, ts,
                    {"controlador": ctrl.get("name"), "status": status},
                )
            for vol in snap.volumenes_raid:
                if not vol.get("name"):
                    continue
                status = vol.get("status") or "OK"
                self._rastreadores["raid_degradado"].evaluar(
                    f"volumen {vol['name']}", status not in ("OK", "Online"), ts,
                    {"volumen": vol["name"], "status": status},
                )
            for disco in snap.discos_fisicos:
                if not disco.get("slot"):
                    continue
                status = disco.get("status") or "OK"
                self._rastreadores["raid_degradado"].evaluar(
                    f"disco slot {disco['slot']}", status not in ("OK", "Online"), ts,
                    {"slot": disco["slot"], "status": status},
                )

        for nodo in snap.nodos_virtuales:
            # Offline por error de acceso (credenciales o permisos de WMI/SSH): la VM responde, no
            # está apagada; sólo no se pudo leer. Mismo criterio que alerts_engine/infra.py
            # (MOTIVOS_ACCESO). Esa muestra no cuenta para la disponibilidad ni abre evento.
            sin_acceso = nodo.state.lower() != "online" and (nodo.state_reason or "") in MOTIVOS_ACCESO
            if sin_acceso:
                self._sin_acceso[nodo.entidad_id] += 1
                continue
            en_falla_offline = nodo.state.lower() != "online"
            contador = self._muestras_nodo[nodo.entidad_id]
            contador[1] += 1
            if not en_falla_offline:
                contador[0] += 1
            self._rastreadores["nodo_offline"].evaluar(
                nodo.entidad_id, en_falla_offline, ts,
                {"tipo": nodo.tipo, "state": nodo.state, "state_reason": nodo.state_reason},
            )

            if nodo.tipo == "vm" and not en_falla_offline:
                # REQ-09: un servicio configurado que no está Running (Stopped, NotFound...).
                for svc in nodo.servicios:
                    self._rastreadores["servicio_detenido"].evaluar(
                        f"{nodo.entidad_id}:{svc.nombre}", svc.state.lower() != "running", ts,
                        {"vm": nodo.entidad_id, "servicio": svc.display_name, "state": svc.state},
                    )

            if nodo.tipo == "vm":
                if nodo.cpu_percent is not None:
                    self._rastreadores["cpu_vm_alto"].evaluar(
                        nodo.entidad_id, nodo.cpu_percent > cfg.cpu_vm_max, ts,
                        {"valor_pct": nodo.cpu_percent},
                    )
                if nodo.ram_percent is not None:
                    self._rastreadores["ram_vm_alto"].evaluar(
                        nodo.entidad_id, nodo.ram_percent > cfg.ram_vm_max, ts,
                        {"valor_pct": nodo.ram_percent},
                    )
                for disco in nodo.discos:
                    if disco.usage_percent is None:
                        continue
                    clave = f"{nodo.entidad_id}:{disco.mount_point}"
                    self._rastreadores["disco_lleno"].evaluar(
                        clave, disco.usage_percent > cfg.disk_threshold, ts,
                        {"vm": nodo.entidad_id, "mount": disco.mount_point, "usage_pct": disco.usage_percent},
                    )

        if snap.network_latency_ms is not None:
            self._muestras_latencia.append(snap.network_latency_ms)

        if snap.suitestensa_new_alerts is not None:
            self._alertas_suitestensa_max = max(self._alertas_suitestensa_max, snap.suitestensa_new_alerts)

    def _stats(self, valores: list[float]) -> dict[str, float] | None:
        if not valores:
            return None
        ordenados = sorted(valores)
        p95_idx = min(len(ordenados) - 1, int(len(ordenados) * 0.95))
        return {
            "avg": round(sum(valores) / len(valores), 1),
            "max": round(max(valores), 1),
            "p95": round(ordenados[p95_idx], 1),
        }

    def _cobertura(self, inicio: datetime | None, fin: datetime | None) -> tuple[float | None, list[dict[str, Any]]]:
        return cobertura(self._horas_reporte, inicio, fin, self.config.offline_minutes)

    def resumen(self, inicio: datetime | None = None, fin: datetime | None = None) -> dict[str, Any]:
        eventos: list[Evento] = []
        for rastreador in self._rastreadores.values():
            eventos.extend(rastreador.finalizar())
        # Un Offline de uno o dos reportes es ruido de lectura, no una VM apagada: el motor del
        # server recién alerta a los `offline_minutes`. Mismo criterio acá.
        eventos = [e for e in eventos if e.tipo != "nodo_offline"
                   or (e.duracion_min or 0) >= self.config.offline_minutes]
        eventos.sort(key=lambda e: e.inicio)

        uptime_pct, huecos = self._cobertura(inicio, fin)
        disponibilidad_nodos = {
            nodo: round(100.0 * online / total, 2)
            for nodo, (online, total) in sorted(self._muestras_nodo.items())
            if total
        }

        return {
            "muestras_analizadas": self._total_snapshots,
            "uptime_pct": uptime_pct,
            "uptime_definicion": (
                "% del período con el hospital enviando reportes; los huecos de más de "
                f"{self.config.offline_minutes} min sin reportes cuentan como caída"
            ),
            "huecos_sin_reportes": {"cantidad": len(huecos), "mas_largos": huecos[:10]},
            "disponibilidad_vms_pct": disponibilidad_nodos,
            "reportes_con_dato": dict(self._con_dato),
            "vms_sin_acceso_reportes": dict(self._sin_acceso),
            "metricas": {
                "cpu_host": self._stats(self._muestras_cpu_host),
                "ram_host": self._stats(self._muestras_ram_host),
                "temp_cpu": self._stats(self._muestras_temp_cpu),
                "latencia_red_ms": self._stats(self._muestras_latencia),
            },
            "alertas_suitestensa_pico": self._alertas_suitestensa_max,
            "eventos": [e.to_dict() for e in eventos],
        }


def analizar_historial(
    filas: list[tuple[Any, ...]],
    config: ConfiguracionGlobal,
    inicio: Any = None,
    fin: Any = None,
) -> dict[str, Any]:
    """
    filas: lista de (full_json_data, host_status[, timestamp]), ordenadas por timestamp asc.
    inicio / fin: el período pedido (texto o datetime), para medir la cobertura completa.
    """
    tracker = EventTracker(config)
    for fila in filas:
        tracker.procesar_fila(*fila)
    return tracker.resumen(a_datetime(inicio), a_datetime(fin))
