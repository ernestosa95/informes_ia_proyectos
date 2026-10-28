"""
Normaliza un snapshot de `reportes_historicos.full_json_data` a una forma
única, sin importar la versión de schema. Las rutas son las del agente
(`agent_logic.py`) y las mismas que lee el motor de alertas del server
(`alerts_engine/infra.py`): sensores, energía, RAID y red van dentro de
`physical_layer`. Las ubicaciones de nivel superior se aceptan como
alternativa para esquemas viejos.

Todo lo que sigue en el pipeline (event_tracker.py) consume `SnapshotNormalizado`,
nunca el JSON crudo directamente, así que agregar una v4.4 mañana implica
tocar sólo este archivo.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from dateutil import parser as dateparser


@dataclass(frozen=True)
class Sensor:
    nombre: str
    valor: float
    status: str


@dataclass(frozen=True)
class FuentePoder:
    nombre: str
    watts: float
    status: str


@dataclass(frozen=True)
class Disco:
    mount_point: str | None
    usage_percent: float | None
    status: str | None


@dataclass(frozen=True)
class ServicioApp:
    nombre: str
    display_name: str
    state: str
    cpu_percent: float | None
    ram_mb: float | None


@dataclass(frozen=True)
class NodoVirtual:
    """Puede ser una VM (`type: vm`) o un equipo de red (`type: eq`)."""
    entidad_id: str
    tipo: str
    state: str
    state_reason: str | None
    cpu_percent: float | None
    ram_percent: float | None
    discos: list[Disco] = field(default_factory=list)
    servicios: list[ServicioApp] = field(default_factory=list)


@dataclass(frozen=True)
class SnapshotNormalizado:
    hospital_id: str
    timestamp: datetime
    schema_version: str

    cpu_host_percent: float | None
    ram_host_percent: float | None
    sensores: list[Sensor]
    fuentes_poder: list[FuentePoder]
    controladores_raid: list[dict[str, Any]]

    nodos_virtuales: list[NodoVirtual]

    network_latency_ms: float | None
    network_status: str | None

    suitestensa_new_alerts: int | None

    # Igual que el motor de alertas del server (alerts_engine/infra.py):
    # volúmenes lógicos y discos físicos del iDRAC, y ventiladores.
    volumenes_raid: list[dict[str, Any]] = field(default_factory=list)
    discos_fisicos: list[dict[str, Any]] = field(default_factory=list)
    ventiladores: list[Sensor] = field(default_factory=list)


def _get(d: Any, *path, default=None):
    """Navega dicts anidados de forma segura, tolerando None en cualquier nivel."""
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def _ram_vm(telemetry: dict[str, Any]) -> float | None:
    """
    % de RAM de una VM, o None si no hay dato real. El agente convierte en 0 lo que WMI no
    devuelve: sin `FreePhysicalMemory`, la cuenta da usado == total (100 % falso); sin
    `TotalVisibleMemorySize`, 0 %. Windows siempre deja memoria libre, así que usado == total
    o total == 0 no son lecturas reales.
    """
    ram = _get(telemetry, "ram", default={}) or {}
    pct, usado, total = ram.get("usage_percent"), ram.get("used_gb"), ram.get("total_gb")
    if pct is None:
        return None
    if total is not None and (total <= 0 or (usado is not None and usado >= total)):
        return None
    return pct


def parse_snapshot(raw: dict[str, Any]) -> SnapshotNormalizado:
    envelope = raw.get("envelope", {}) or {}
    hospital_id = envelope.get("hospital_id", "desconocido")
    schema_version = envelope.get("schema_version", "desconocida")
    timestamp = dateparser.parse(envelope["timestamp"]) if envelope.get("timestamp") else datetime.now()

    physical = raw.get("physical_layer", {}) or {}

    cpu_host = _get(physical, "telemetry", "cpu", "usage_percent")
    ram_host = _get(physical, "telemetry", "ram", "usage_percent")

    # El agente pone sensores, almacenamiento y red DENTRO de physical_layer
    # (agent_logic.py; así los lee también el motor de alertas del server). Las
    # ubicaciones de nivel superior quedan como alternativa para esquemas viejos.
    sensores = [
        Sensor(nombre=t.get("name") or "?", valor=t.get("value") or 0, status=t.get("status") or "?")
        for t in _get(physical, "sensors", "temperatures", default=[])
        if t.get("value") is not None
    ]

    ventiladores = [
        Sensor(nombre=f.get("name") or "?", valor=f.get("value") or 0, status=f.get("status") or "?")
        for f in _get(physical, "sensors", "fans", default=[])
    ]

    supplies = _get(physical, "sensors", "power", "supplies", default=None)
    if supplies is None:
        supplies = _get(physical, "power", "supplies", default=[])
    fuentes_poder = [
        FuentePoder(nombre=p.get("name") or "?", watts=p.get("watts") or 0.0, status=p.get("status") or "?")
        for p in supplies
    ]

    storage_block = (
        physical.get("storage_layer") or raw.get("storage_layer") or raw.get("storage") or {}
    )
    controladores_raid = storage_block.get("controllers", []) or []
    volumenes_raid = storage_block.get("logical_volumes", []) or []
    discos_fisicos = storage_block.get("physical_drives", []) or []

    nodos_virtuales = []
    for nodo in raw.get("virtual_layer", []) or []:
        telemetry = nodo.get("telemetry", {}) or {}
        discos = [
            Disco(
                mount_point=d.get("mount_point"),
                usage_percent=d.get("usage_percent"),
                status=_get(d, "performance", "status"),
            )
            for d in nodo.get("storage", []) or []
        ]
        servicios = [
            ServicioApp(
                nombre=s.get("name", "?"),
                display_name=s.get("display_name", s.get("name", "?")),
                state=s.get("state", "?"),
                cpu_percent=_get(s, "vital_signs", "cpu_percent"),
                ram_mb=_get(s, "vital_signs", "ram_mb"),
            )
            for s in _get(nodo, "application_layer", "services", default=[])
        ]
        nodos_virtuales.append(
            NodoVirtual(
                entidad_id=nodo.get("id", "?"),
                tipo=nodo.get("type", "?"),
                state=nodo.get("state", "?"),
                state_reason=nodo.get("state_reason"),
                cpu_percent=_get(telemetry, "cpu", "usage_percent"),
                ram_percent=_ram_vm(telemetry),
                discos=discos,
                servicios=servicios,
            )
        )

    network = physical.get("network_health") or raw.get("network_health") or {}

    suitestensa_alerts = _get(raw, "collection_meta", "suitestensa_logs", "new_alerts")

    return SnapshotNormalizado(
        hospital_id=hospital_id,
        timestamp=timestamp,
        schema_version=schema_version,
        cpu_host_percent=cpu_host,
        ram_host_percent=ram_host,
        sensores=sensores,
        fuentes_poder=fuentes_poder,
        controladores_raid=controladores_raid,
        nodos_virtuales=nodos_virtuales,
        network_latency_ms=network.get("cloud_latency_ms"),
        network_status=network.get("cloud_status"),
        suitestensa_new_alerts=suitestensa_alerts,
        volumenes_raid=volumenes_raid,
        discos_fisicos=discos_fisicos,
        ventiladores=ventiladores,
    )
