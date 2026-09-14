from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FleetIRD:
    name: str
    host: str
    enabled: bool = True
    baseline_path: Path | None = None


@dataclass(frozen=True, slots=True)
class ServiceBaseline:
    """Expected DVB services and carrier metadata keyed by module.channel."""

    expected_mux: dict[str, dict[int, str]]
    satellite_names: dict[str, str]

    @property
    def input_count(self) -> int:
        return len(self.expected_mux)

    @property
    def service_count(self) -> int:
        return sum(len(services) for services in self.expected_mux.values())

    def expected_for(self, module: int, channel: int) -> dict[int, str]:
        return dict(self.expected_mux.get(f"{module}.{channel}", {}))

    def satellite_for(self, module: int, channel: int) -> str | None:
        return self.satellite_names.get(f"{module}.{channel}")


def _read_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_fleet(config_path: str | Path) -> list[FleetIRD]:
    config_path = Path(config_path).resolve()
    data = _read_toml(config_path)
    raw_irds = data.get("fleet", {}).get("irds", [])

    if not raw_irds:
        raise ValueError(
            f"No [[fleet.irds]] entries found in {config_path}"
        )

    result: list[FleetIRD] = []
    seen_hosts: set[str] = set()

    for index, raw in enumerate(raw_irds, start=1):
        name = str(raw.get("name", "")).strip()
        host = str(raw.get("host", "")).strip()
        enabled = bool(raw.get("enabled", True))
        baseline_raw = str(raw.get("baseline", "")).strip()

        if not name:
            raise ValueError(f"Fleet entry #{index} has no name")
        if not host:
            raise ValueError(f"Fleet entry #{index} ({name}) has no host")
        if host in seen_hosts:
            raise ValueError(f"Duplicate fleet host: {host}")
        seen_hosts.add(host)

        baseline_path: Path | None = None
        if baseline_raw:
            baseline_path = Path(baseline_raw)
            if not baseline_path.is_absolute():
                baseline_path = config_path.parent / baseline_path
            baseline_path = baseline_path.resolve()

        result.append(
            FleetIRD(
                name=name,
                host=host,
                enabled=enabled,
                baseline_path=baseline_path,
            )
        )

    return result


def load_service_baseline(path: str | Path) -> ServiceBaseline:
    path = Path(path).resolve()
    data = _read_toml(path)

    raw_expected_mux = (
        data.get("services", {})
        .get("expected_mux", {})
    )

    if not isinstance(raw_expected_mux, dict):
        raise ValueError(
            f"[services.expected_mux] is missing or invalid in {path}"
        )

    expected_mux: dict[str, dict[int, str]] = {}

    for input_key, raw_services in raw_expected_mux.items():
        if not isinstance(raw_services, dict):
            raise ValueError(
                f'Expected table [services.expected_mux."{input_key}"] in {path}'
            )

        normalized: dict[int, str] = {}
        for raw_sid, raw_name in raw_services.items():
            try:
                sid = int(raw_sid)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid SID {raw_sid!r} for {input_key} in {path}"
                ) from exc

            if not 0 <= sid <= 65535:
                raise ValueError(
                    f"SID {sid} out of range for {input_key} in {path}"
                )

            name = str(raw_name).strip()
            if not name:
                raise ValueError(
                    f"Empty expected service name for {input_key} SID {sid}"
                )

            normalized[sid] = name

        expected_mux[str(input_key)] = normalized

    raw_carriers = data.get("carriers", {})
    if not isinstance(raw_carriers, dict):
        raise ValueError(f"[carriers] is invalid in {path}")

    satellite_names: dict[str, str] = {}

    for input_key, raw_metadata in raw_carriers.items():
        key = str(input_key)

        # Validate the carrier key using the same module.channel convention.
        parse_input_key(key)

        if not isinstance(raw_metadata, dict):
            raise ValueError(
                f'Expected table [carriers."{key}"] in {path}'
            )

        satellite_name = str(
            raw_metadata.get("satellite_name", "")
        ).strip()

        if not satellite_name:
            raise ValueError(
                f'Empty satellite_name for carrier {key} in {path}'
            )

        satellite_names[key] = satellite_name

    missing_metadata = set(expected_mux) - set(satellite_names)
    extra_metadata = set(satellite_names) - set(expected_mux)

    if missing_metadata:
        raise ValueError(
            f"Missing carrier metadata in {path}: "
            f"{sorted(missing_metadata)}"
        )

    if extra_metadata:
        raise ValueError(
            f"Carrier metadata without expected_mux in {path}: "
            f"{sorted(extra_metadata)}"
        )

    return ServiceBaseline(
        expected_mux=expected_mux,
        satellite_names=satellite_names,
    )


def parse_input_key(key: str) -> tuple[int, int]:
    parts = key.split(".")
    if len(parts) != 2:
        raise ValueError(f"Invalid input key {key!r}; expected 'module.channel'")

    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"Invalid input key {key!r}; expected integer module.channel"
        ) from exc
