from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class SnmpConfig:
    host: str
    community: str = "public"
    port: int = 161
    timeout_seconds: float = 2.0
    retries: int = 1


@dataclass(slots=True)
class CollectorConfig:
    poll_interval_seconds: int = 10
    rediscover_every_cycles: int = 60
    database_path: str = "database/gt34.sqlite3"
    log_path: str = "logs/gt34-monitor.log"
    keep_days: int = 90


@dataclass(slots=True)
class ThresholdConfig:
    # SNR thresholds
    snr_warning_below_db: float | None = None
    snr_error_below_db: float | None = None

    # RF input-level thresholds
    rf_warning_below_dbm: float | None = None
    rf_error_below_dbm: float | None = None
    rf_warning_above_dbm: float | None = None
    rf_error_above_dbm: float | None = None


@dataclass(slots=True)
class ServiceConfig:
    # Legacy single-service aliases retained for backward compatibility with
    # the existing database/diagnostic fields.
    expected: dict[str, str] = field(default_factory=dict)

    # Production multiplex baseline:
    #   "module.channel" -> {SID: expected service name}
    expected_mux: dict[str, dict[int, str]] = field(default_factory=dict)

    def expected_for(
        self,
        module: int,
        channel: int,
        fallback: str | None = None,
    ) -> str | None:
        return self.expected.get(f"{module}.{channel}", fallback)

    def expected_mux_for(
        self,
        module: int,
        channel: int,
    ) -> dict[int, str]:
        return dict(self.expected_mux.get(f"{module}.{channel}", {}))


@dataclass(slots=True)
class AppConfig:
    snmp: SnmpConfig
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    services: ServiceConfig = field(default_factory=ServiceConfig)


def _parse_expected_mux(raw_services: dict) -> dict[str, dict[int, str]]:
    raw_mux = raw_services.get("expected_mux", {})
    if not isinstance(raw_mux, dict):
        raise ValueError("[services.expected_mux] must be a TOML table")

    result: dict[str, dict[int, str]] = {}

    for input_key, raw_services_for_input in raw_mux.items():
        if not isinstance(raw_services_for_input, dict):
            raise ValueError(
                f'[services.expected_mux."{input_key}"] must be a TOML table'
            )

        sid_map: dict[int, str] = {}

        for raw_sid, raw_name in raw_services_for_input.items():
            try:
                sid = int(raw_sid)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid DVB SID {raw_sid!r} in expected multiplex {input_key}"
                ) from exc

            if sid < 0 or sid > 65535:
                raise ValueError(
                    f"Invalid DVB SID {sid} in expected multiplex {input_key}"
                )

            name = str(raw_name).strip()
            if not name:
                raise ValueError(
                    f"Empty service name for SID {sid} in expected multiplex {input_key}"
                )

            sid_map[sid] = name

        result[str(input_key)] = sid_map

    return result


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    raw = tomllib.loads(p.read_text(encoding="utf-8"))

    if "snmp" not in raw or not raw["snmp"].get("host"):
        raise ValueError("config.toml must define [snmp] host")

    raw_services = raw.get("services", {})
    if not isinstance(raw_services, dict):
        raise ValueError("[services] must be a TOML table")

    legacy_expected = dict(raw_services.get("expected", {}))
    expected_mux = _parse_expected_mux(raw_services)

    return AppConfig(
        snmp=SnmpConfig(**raw["snmp"]),
        collector=CollectorConfig(**raw.get("collector", {})),
        thresholds=ThresholdConfig(**raw.get("thresholds", {})),
        services=ServiceConfig(
            expected={str(k): str(v) for k, v in legacy_expected.items()},
            expected_mux=expected_mux,
        ),
    )
