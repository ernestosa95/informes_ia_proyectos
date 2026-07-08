"""
Normaliza un snapshot de `reportes_historicos.full_json_data` a una forma
única, sin importar la versión de schema (vimos 4.0 y 4.3 con diferencias
reales: `storage` vs `storage_layer`, `network_health` ausente en 4.0, etc.).

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


def _get(d: Any, *path, default=None):
    """Navega dicts anidados de forma segura, tolerando None en cualquier nivel."""
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


def parse_snapshot(raw: dict[str, Any]) -> SnapshotNormalizado:
    envelope = raw.get("envelope", {}) or {}
    hospital_id = envelope.get("hospital_id", "desconocido")
    schema_version = envelope.get("schema_version", "desconocida")
    timestamp = dateparser.parse(envelope["timestamp"]) if envelope.get("timestamp") else datetime.now()

    physical = raw.get("physical_layer", {}) or {}

    cpu_host = _get(physical, "telemetry", "cpu", "usage_percent")
    ram_host = _get(physical, "telemetry", "ram", "usage_percent")

    sensores = [
        Sensor(nombre=t.get("name", "?"), valor=t.get("value", 0), status=t.get("status", "?"))
        for t in _get(physical, "sensors", "temperatures", default=[])
    ]

    fuentes_poder = [
        FuentePoder(nombre=p.get("name", "?"), watts=p.get("watts", 0.0), status=p.get("status", "?"))
        for p in _get(physical, "power", "supplies", default=[])
    ]

    # `storage` (schema 4.3) vs `storage_layer` (schema 4.0)
    storage_block = raw.get("storage") or raw.get("storage_layer") or {}
    controladores_raid = storage_block.get("controllers", []) or []

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
                ram_percent=_get(telemetry, "ram", "usage_percent"),
                discos=discos,
                servicios=servicios,
            )
        )

    network = raw.get("network_health") or {}

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
    )
