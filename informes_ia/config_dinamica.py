"""
Carga la tabla `configuracion` (clave/valor, global, sin hospital_id) y la
tipa en una estructura utilizable por los tres analizadores de preprocess.py.

Si mañana se agrega una tabla de overrides por hospital, este es el único
lugar que habría que tocar: el resto del código consume `ConfiguracionGlobal`,
no la tabla cruda.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .logging_utils import get_logger

log = get_logger(__name__)


def _split_csv(valor: str | None) -> list[str]:
    if not valor:
        return []
    return [v.strip() for v in valor.split(",") if v.strip()]


def _leer_tabla_kv(db_path: Path, tabla: str = "configuracion") -> dict[str, str]:
    if not db_path.exists():
        raise FileNotFoundError(f"No se encontró la base de datos en {db_path}")
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT clave, valor FROM {tabla}")
        return {clave: valor for clave, valor in cursor.fetchall()}


@dataclass(frozen=True)
class KpiRadiologia:
    enabled: bool
    threshold_hours: int
    modalidades: list[str]
    emails: list[str]


@dataclass(frozen=True)
class KpiMamografia:
    enabled: bool
    threshold_days: int


@dataclass(frozen=True)
class ConfigMirth:
    enabled: bool
    queued_threshold: int
    emails: list[str]


@dataclass(frozen=True)
class ConfiguracionGlobal:
    # Umbrales de infraestructura
    offline_minutes: int
    disk_threshold: int
    temp_amb_max: int
    temp_cpu_max: int
    cpu_host_max: int
    ram_host_max: int
    cpu_vm_max: int
    ram_vm_max: int

    # Flags de sensores de hardware
    enable_fans: bool
    enable_power: bool
    enable_raid: bool

    # KPIs de negocio (RIS/PACS)
    kpi_rad: KpiRadiologia
    kpi_mamo: KpiMamografia
    mirth: ConfigMirth

    kpi_execution_time: str
    global_alert_emails: list[str]

    # Umbrales que TODAVÍA no existen como clave explícita en `configuracion`
    # y que hoy vivimos con un default documentado. Ver README > TODOs.
    ssl_warning_days: int = field(default=30)

    @staticmethod
    def cargar(db_path: Path) -> "ConfiguracionGlobal":
        raw = _leer_tabla_kv(db_path)

        faltantes = [
            k
            for k in (
                "offline_minutes", "disk_threshold", "temp_amb_max", "temp_cpu_max",
                "cpu_host_max", "ram_host_max", "cpu_vm_max", "ram_vm_max",
                "enable_fans", "enable_power", "enable_raid",
            )
            if k not in raw
        ]
        if faltantes:
            log.warning("Faltan claves en `configuracion`: %s. Se usarán defaults conservadores.", faltantes)

        def _int(clave: str, default: int) -> int:
            try:
                return int(raw.get(clave, default))
            except (TypeError, ValueError):
                log.warning("Valor inválido para '%s'='%s', usando default %s", clave, raw.get(clave), default)
                return default

        def _bool(clave: str, default: bool) -> bool:
            v = raw.get(clave)
            return (v == "1") if v is not None else default

        return ConfiguracionGlobal(
            offline_minutes=_int("offline_minutes", 30),
            disk_threshold=_int("disk_threshold", 90),
            temp_amb_max=_int("temp_amb_max", 27),
            temp_cpu_max=_int("temp_cpu_max", 75),
            cpu_host_max=_int("cpu_host_max", 85),
            ram_host_max=_int("ram_host_max", 95),
            cpu_vm_max=_int("cpu_vm_max", 90),
            ram_vm_max=_int("ram_vm_max", 90),
            enable_fans=_bool("enable_fans", True),
            enable_power=_bool("enable_power", True),
            enable_raid=_bool("enable_raid", True),
            kpi_rad=KpiRadiologia(
                enabled=_bool("kpi_rad_alert_enabled", False),
                threshold_hours=_int("kpi_rad_threshold_hours", 24),
                modalidades=_split_csv(raw.get("kpi_rad_modalities")),
                emails=_split_csv(raw.get("kpi_rad_responsible_email")),
            ),
            kpi_mamo=KpiMamografia(
                enabled=_bool("kpi_mamo_alert_enabled", False),
                threshold_days=_int("kpi_mamo_threshold_days", 7),
            ),
            mirth=ConfigMirth(
                enabled=_bool("mirth_alert_enabled", False),
                queued_threshold=_int("mirth_queued_threshold", 100),
                emails=_split_csv(raw.get("mirth_responsible_email")),
            ),
            kpi_execution_time=raw.get("kpi_execution_time", "08:00"),
            global_alert_emails=_split_csv(raw.get("global_alert_responsible_email")),
        )


def get_configuracion(db_path: Path) -> ConfiguracionGlobal:
    return ConfiguracionGlobal.cargar(db_path)
