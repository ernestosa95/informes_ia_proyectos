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
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .config_dinamica import ConfiguracionGlobal
from .logging_utils import get_logger
from .normalizacion import SnapshotNormalizado, parse_snapshot

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
            "raid_degradado": _RastreadorUmbral("raid_degradado", "alta"),
            "nodo_offline": _RastreadorUmbral("nodo_offline", "alta"),
        }
        # Muestras crudas para calcular avg/max/p95 al final (no requiere
        # guardar todo el JSON, sólo los escalares que nos interesan).
        self._muestras_cpu_host: list[float] = []
        self._muestras_ram_host: list[float] = []
        self._muestras_temp_cpu: list[float] = []
        self._muestras_latencia: list[float] = []
        self._total_snapshots = 0
        self._snapshots_online = 0
        self._alertas_suitestensa_max = 0

    def procesar_fila(self, raw_json_str: str, host_status: str | None = None) -> None:
        try:
            raw = json.loads(raw_json_str)
        except (json.JSONDecodeError, TypeError):
            return

        snap = parse_snapshot(raw)
        self._procesar_snapshot(snap, host_status)

    def _procesar_snapshot(self, snap: SnapshotNormalizado, host_status: str | None) -> None:
        cfg = self.config
        ts = snap.timestamp
        self._total_snapshots += 1
        if (host_status or "").lower() == "online":
            self._snapshots_online += 1

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

        if cfg.enable_raid:
            for ctrl in snap.controladores_raid:
                status = (ctrl.get("status") or "").upper()
                en_falla = bool(status) and status != "OK"
                self._rastreadores["raid_degradado"].evaluar(
                    ctrl.get("name", "?"), en_falla, ts,
                    {"controlador": ctrl.get("name"), "status": status},
                )

        for nodo in snap.nodos_virtuales:
            en_falla_offline = nodo.state.lower() != "online"
            self._rastreadores["nodo_offline"].evaluar(
                nodo.entidad_id, en_falla_offline, ts,
                {"tipo": nodo.tipo, "state": nodo.state, "state_reason": nodo.state_reason},
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

    def resumen(self) -> dict[str, Any]:
        eventos: list[Evento] = []
        for rastreador in self._rastreadores.values():
            eventos.extend(rastreador.finalizar())
        eventos.sort(key=lambda e: e.inicio)

        uptime_pct = (
            round(self._snapshots_online / self._total_snapshots * 100, 1)
            if self._total_snapshots else None
        )

        return {
            "muestras_analizadas": self._total_snapshots,
            "uptime_pct": uptime_pct,
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
    filas: list[tuple[str, str | None]], config: ConfiguracionGlobal
) -> dict[str, Any]:
    """
    filas: lista de (full_json_data, host_status), ya ordenadas por timestamp asc.
    """
    tracker = EventTracker(config)
    for raw_json_str, host_status in filas:
        tracker.procesar_fila(raw_json_str, host_status)
    return tracker.resumen()
