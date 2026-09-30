from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATABASE_PATH, WELLAV_CMP201, WISI_CHASSIS
from parsers import (
    parse_pidmapper,
    parse_pcr_inputs,
    parse_ts_flux,
    parse_tsdb_input,
    parse_tuner_flux,
    parse_tuner_config,
)
from wisi_client import WisiClient


CONTINUOUS_INTERVAL_SECONDS = 5.0
LOG_DIR = Path("logs")
CONTINUOUS_LOG_PATH = LOG_DIR / "collector_continuous.log"

DYNAMIC_MAPPING_EVIDENCE_PATH = LOG_DIR / "dynamic_mapping_evidence.jsonl"
DYNAMIC_MAPPING_RESOURCE = "tsio/inputs_conf.xmlc"
TUNER_CONFIG_RESOURCE = "tuner.xmlc"


# Wellav CMP201 read-only acquisition endpoints.
WELLAV_API_TOKEN = os.getenv("WELLAV_API_TOKEN", "")
WELLAV_CONFIG_ENDPOINT = "GetSubboardParam.w"
WELLAV_STATUS_ENDPOINT = "GetInputTSStatus.w"
WELLAV_HTTP_TIMEOUT_SECONDS = 5.0


def configure_continuous_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("wisi_collector")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s"
        )
        file_handler = logging.FileHandler(
            CONTINUOUS_LOG_PATH,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


EXPECTED_TABLES = {
    "chassis",
    "modules",
    "tuners",
    "tuner_samples",
    "ts_samples",
    "pids",
    "pid_samples",
    "pcr_input_samples",
    "pcr_pid_samples",
    "transport_streams",
    "services",
    "service_streams",
    "service_samples",
    "counter_state",
    "monitoring_events",
    "poll_runs",
}

PCR_COUNTER_FIELDS = (
    "ref_discontinuities",
    "pcr_accuracy_errors",
    "pcr_repetition_errors",
    "pcr_discontinuity_indicator_errors",
)

INPUT_REGULATOR_COUNTER_FIELDS = (
    "to_wait_state",
    "playout_fifo_reset",
    "unref_discontinuity",
    "sample_ignored",
    "into_freerunning",
    "pcr_pid_changed",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def as_int_bool(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def open_db(path: Path | str = DATABASE_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def ensure_dynamic_mapping_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS input_tuner_mapping_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module_id INTEGER NOT NULL,
            sampled_at TEXT NOT NULL,
            configured_input_id INTEGER NOT NULL,
            configured_name TEXT,
            configured_uuid TEXT,
            input_enabled INTEGER,
            error_status TEXT,
            tuner_object_id INTEGER,
            hwid INTEGER,
            physical_port INTEGER,
            mapping_status TEXT NOT NULL,
            UNIQUE(module_id, sampled_at, configured_input_id)
        );
        CREATE INDEX IF NOT EXISTS idx_input_tuner_mapping_lookup
        ON input_tuner_mapping_samples(module_id, configured_input_id, sampled_at);

        CREATE TABLE IF NOT EXISTS tuner_config_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module_id INTEGER NOT NULL,
            sampled_at TEXT NOT NULL,
            tuner_object_id INTEGER NOT NULL,
            enabled INTEGER,
            state INTEGER,
            tuner_type INTEGER,
            type_name TEXT,
            frequency_raw INTEGER,
            frequency_mhz REAL,
            symbol_rate_raw INTEGER,
            symbol_rate_mbd REAL,
            polarisation_code INTEGER,
            polarisation TEXT,
            fec_config INTEGER,
            modulation_config INTEGER,
            is_id_config INTEGER,
            lnb INTEGER,
            lo_frequency_raw INTEGER,
            voltage INTEGER,
            tone INTEGER,
            UNIQUE(module_id, sampled_at, tuner_object_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tuner_config_lookup
        ON tuner_config_samples(module_id, tuner_object_id, sampled_at);
        """
    )
    conn.commit()



def ensure_wellav_schema(
    conn: sqlite3.Connection,
) -> None:
    """Create additive Wellav CMP201 acquisition tables.

    These tables are intentionally separate from the existing WISI tuner,
    transport-stream and service tables. No existing production table is
    altered or dropped.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS wellav_input_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            sampled_at TEXT NOT NULL,

            module_number INTEGER NOT NULL,
            module_ip TEXT NOT NULL,

            port INTEGER NOT NULL,
            channel INTEGER NOT NULL,
            ui_channel TEXT NOT NULL,

            enabled INTEGER,

            satellite_frequency_mhz REAL,
            symbol_rate_kbaud REAL,
            lnb_frequency_mhz REAL,
            lnb_power_support INTEGER,
            lnb_22khz INTEGER,

            lock_status INTEGER,

            total_bitrate_bps REAL,
            effective_bitrate_bps REAL,

            rf_level_dbm REAL,
            cn_raw REAL,
            cn_db REAL,

            packet_error_rate REAL,
            ber_count REAL,
            ber_exponent REAL,

            signal_type TEXT,
            frequency_tune REAL,
            link_margin REAL,
            fec_code_rate TEXT,
            modulation TEXT,

            program_count INTEGER,

            UNIQUE(
                module_ip,
                sampled_at,
                port,
                channel
            )
        );

        CREATE INDEX IF NOT EXISTS
        idx_wellav_input_samples_route_time
        ON wellav_input_samples(
            module_ip,
            port,
            channel,
            sampled_at
        );

        CREATE INDEX IF NOT EXISTS
        idx_wellav_input_samples_rf_time
        ON wellav_input_samples(
            satellite_frequency_mhz,
            symbol_rate_kbaud,
            sampled_at
        );


        CREATE TABLE IF NOT EXISTS wellav_service_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            sampled_at TEXT NOT NULL,

            module_number INTEGER NOT NULL,
            module_ip TEXT NOT NULL,

            port INTEGER NOT NULL,
            channel INTEGER NOT NULL,
            ui_channel TEXT NOT NULL,

            service_id INTEGER NOT NULL,
            service_name TEXT,

            UNIQUE(
                module_ip,
                sampled_at,
                port,
                channel,
                service_id
            )
        );

        CREATE INDEX IF NOT EXISTS
        idx_wellav_service_samples_route_time
        ON wellav_service_samples(
            module_ip,
            port,
            channel,
            sampled_at
        );

        CREATE INDEX IF NOT EXISTS
        idx_wellav_service_samples_sid_time
        ON wellav_service_samples(
            service_id,
            sampled_at
        );


        CREATE TABLE IF NOT EXISTS wellav_poll_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            started_at TEXT NOT NULL,
            completed_at TEXT,

            success INTEGER NOT NULL,

            duration_seconds REAL,

            modules_attempted INTEGER NOT NULL,
            modules_succeeded INTEGER NOT NULL,

            error_message TEXT
        );

        CREATE INDEX IF NOT EXISTS
        idx_wellav_poll_runs_started
        ON wellav_poll_runs(started_at);
        """
    )

    conn.commit()


def verify_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    present = {row[0] for row in rows}
    missing = sorted(EXPECTED_TABLES - present)
    if missing:
        raise RuntimeError(
            "Database schema is incomplete. Missing table(s): "
            + ", ".join(missing)
        )

    required_columns = {
        "tuners": {"first_seen_at", "last_seen_at"},
        "tuner_samples": {"enabled", "state", "disabled"},
        "pids": {"first_seen_at", "last_seen_at"},
        "transport_streams": {"first_seen_at", "last_seen_at"},
        "services": {"first_seen_at", "last_seen_at"},
        "service_streams": {"first_seen_at", "last_seen_at"},
        "service_samples": {
            "tuner_id", "sampled_at", "service_id", "service_name",
            "provider_name", "pmt_pid", "pcr_pid", "running_status",
            "elementary_stream_count",
        },
        "pcr_input_samples": {
            "pcr_discontinuity_errors_total",
            "pcr_discontinuity_errors_delta",
        },
        "pcr_pid_samples": {
            "pcr_discontinuity_errors_total",
            "pcr_discontinuity_errors_delta",
        },
    }

    schema_errors: list[str] = []
    for table, expected in required_columns.items():
        columns = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        absent = sorted(expected - columns)
        if absent:
            schema_errors.append(f"{table}: {', '.join(absent)}")

    if schema_errors:
        raise RuntimeError(
            "Database schema columns do not match Schema v1: "
            + "; ".join(schema_errors)
        )


def ensure_equipment(conn: sqlite3.Connection, sampled_at: str) -> dict[tuple[str, int], int]:
    module_ids: dict[tuple[str, int], int] = {}
    for host, chassis_cfg in WISI_CHASSIS.items():
        conn.execute(
            """INSERT INTO chassis(host, name, created_at, last_seen_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(host) DO UPDATE SET
                   name = excluded.name, last_seen_at = excluded.last_seen_at""",
            (host, chassis_cfg["name"], sampled_at, sampled_at),
        )
        chassis_id = int(conn.execute(
            "SELECT id FROM chassis WHERE host = ?", (host,)
        ).fetchone()[0])
        for module_number, cfg in chassis_cfg["modules"].items():
            conn.execute(
                """INSERT INTO modules(
                       chassis_id, module_number, module_name, remote_identifier,
                       remote_ip, created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(chassis_id, module_number) DO UPDATE SET
                       module_name = excluded.module_name,
                       remote_identifier = excluded.remote_identifier,
                       remote_ip = excluded.remote_ip,
                       last_seen_at = excluded.last_seen_at""",
                (chassis_id, module_number, cfg["name"], cfg["remote"],
                 cfg["remote_ip"], sampled_at, sampled_at),
            )
            module_id = int(conn.execute(
                "SELECT id FROM modules WHERE chassis_id=? AND module_number=?",
                (chassis_id,module_number),
            ).fetchone()[0])
            module_ids[(host,module_number)] = module_id
    return module_ids


def _xml_text(element: ET.Element | None, path: str) -> str | None:
    if element is None:
        return None
    child = element.find(path)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _xml_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_dynamic_input_relationships(xml_text: str) -> dict[int, dict[str, Any]]:
    """Parse WISI TS-input -> tuner-object relationships from inputs_conf.xmlc.

    This is evidence only. It does not change the existing production storage
    identity or alarm-policy path.
    """
    root = ET.fromstring(xml_text)
    relationships: dict[int, dict[str, Any]] = {}

    for element in root.iter("input"):
        input_id = _xml_int(element.get("id"))
        if input_id is None:
            continue

        typespec = element.find("typespec")
        relationships[input_id] = {
            "input_id": input_id,
            "configured_name": _xml_text(element, "name"),
            "uuid": _xml_text(element, "uuid"),
            "input_enabled": _xml_int(element.get("enabled")),
            "error_status": _xml_text(element, "error_status"),
            "tuner_object_id": (
                _xml_int(typespec.get("id")) if typespec is not None else None
            ),
            "hwid": _xml_int(_xml_text(typespec, "hwid")),
            "physical_port": _xml_int(_xml_text(typespec, "physical_port")),
        }

    return relationships


def append_dynamic_mapping_evidence(records: list[dict[str, Any]]) -> None:
    """Append non-alarming relationship evidence to a JSONL sidecar log."""
    if not records:
        return

    import json

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with DYNAMIC_MAPPING_EVIDENCE_PATH.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def ensure_tuner(
    conn: sqlite3.Connection,
    module_id: int,
    input_id: int,
    sampled_at: str,
) -> int:
    """Register/update one tuner that is actually present in the WISI response."""
    conn.execute(
        """INSERT INTO tuners(
               module_id,input_id,hwid,display_number,configured_name,
               first_seen_at,last_seen_at)
           VALUES (?, ?, NULL, ?, NULL, ?, ?)
           ON CONFLICT(module_id,input_id) DO UPDATE SET
               display_number=excluded.display_number,
               last_seen_at=excluded.last_seen_at""",
        (module_id, input_id, input_id + 1, sampled_at, sampled_at),
    )
    return get_tuner_id(conn, module_id, input_id)


def get_tuner_id(
    conn: sqlite3.Connection,
    module_id: int,
    input_id: int,
) -> int:
    row = conn.execute(
        "SELECT id FROM tuners WHERE module_id = ? AND input_id = ?",
        (module_id, input_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"No tuner row for module_id={module_id}, input_id={input_id}"
        )
    return int(row[0])


def counter_delta(
    conn: sqlite3.Connection,
    counter_key: str,
    current: int | None,
    sampled_at: str,
) -> tuple[int | None, bool]:
    """Return (delta, reset_detected) and persist current counter value.

    First observation -> delta 0.
    Monotonic increase -> current - previous.
    Decrease -> treated as counter reset, delta 0.
    None -> no state update and delta None.
    """
    if current is None:
        return None, False

    row = conn.execute(
        "SELECT counter_value FROM counter_state WHERE counter_key = ?",
        (counter_key,),
    ).fetchone()

    if row is None:
        delta = 0
        reset = False
    else:
        previous = int(row[0])
        if current >= previous:
            delta = current - previous
            reset = False
        else:
            delta = 0
            reset = True

    conn.execute(
        """
        INSERT INTO counter_state(counter_key, counter_value, sampled_at)
        VALUES (?, ?, ?)
        ON CONFLICT(counter_key) DO UPDATE SET
            counter_value = excluded.counter_value,
            sampled_at = excluded.sampled_at
        """,
        (counter_key, current, sampled_at),
    )

    return delta, reset


def record_counter_reset(
    conn: sqlite3.Connection,
    *,
    sampled_at: str,
    module_id: int,
    tuner_id: int | None,
    pid_id: int | None,
    metric: str,
    value: int,
) -> None:
    conn.execute(
        """
        INSERT INTO monitoring_events(
            occurred_at, module_id, tuner_id, pid_id,
            category, severity, metric, value, delta,
            message, acknowledged
        )
        VALUES (?, ?, ?, ?, 'counter_reset', 'info', ?, ?, 0, ?, 0)
        """,
        (
            sampled_at,
            module_id,
            tuner_id,
            pid_id,
            metric,
            value,
            f"Counter reset detected for {metric}; delta suppressed for this sample.",
        ),
    )


def get_or_create_pid(
    conn: sqlite3.Connection,
    tuner_id: int,
    pid: int,
    sampled_at: str,
) -> int:
    conn.execute(
        """
        INSERT INTO pids(tuner_id, pid, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tuner_id, pid) DO UPDATE SET
            last_seen_at = excluded.last_seen_at
        """,
        (tuner_id, pid, sampled_at, sampled_at),
    )
    return int(
        conn.execute(
            "SELECT id FROM pids WHERE tuner_id = ? AND pid = ?",
            (tuner_id, pid),
        ).fetchone()[0]
    )


def current_pcr_pid_is_active(
    *,
    tuner_data: dict[str, Any],
    ts_data: dict[str, Any],
    pid_input: dict[str, Any],
    pcr_pid: dict[str, Any],
    pid: int,
) -> bool:
    """Reject stale PCR records left behind by previous transport streams."""
    locked = tuner_data.get("lock_state") == 1
    ts_rate = ts_data.get("current_bitrate_bps") or 0
    pids = pid_input.get("pids", {})
    current_pid = pids.get(pid)

    if not locked or ts_rate <= 0 or current_pid is None:
        return False

    pcr_bitrate = pcr_pid.get("pcr_bitrate") or 0
    stc_bitrate = pcr_pid.get("stc_bitrate") or 0
    pid_says_pcr = current_pid.get("pcr_present") is True

    return bool(pid_says_pcr or pcr_bitrate > 0 or stc_bitrate > 0)


def upsert_transport_and_services(
    conn: sqlite3.Connection,
    *,
    tuner_id: int,
    tsdb_data: dict[str, Any],
    sampled_at: str,
) -> tuple[int, int]:
    tsid = tsdb_data.get("transport_stream_id")
    onid = tsdb_data.get("original_network_id")
    network_id = tsdb_data.get("network_id")

    # Avoid SQLite NULL-UNIQUE duplicate behavior by not creating a
    # meaningless transport-stream identity when both identifiers are absent.
    if tsid is not None or onid is not None:
        row = conn.execute(
            """
            SELECT id FROM transport_streams
            WHERE tuner_id = ?
              AND tsid IS ?
              AND onid IS ?
            ORDER BY id
            LIMIT 1
            """,
            (tuner_id, tsid, onid),
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO transport_streams(
                    tuner_id, tsid, onid, network_id, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (tuner_id, tsid, onid, network_id, sampled_at, sampled_at),
            )
        else:
            conn.execute(
                """
                UPDATE transport_streams
                SET network_id = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (network_id, sampled_at, int(row[0])),
            )

    service_count = 0
    stream_count = 0

    # Preserve authoritative service observations for this acquisition cycle.
    # services remains the catalogue; service_samples is per-cycle state.
    observed_services = tsdb_data.get("services", {}) or {}

    for service_id, svc in observed_services.items():
        streams = svc.get("streams", []) or []
        conn.execute(
            """
            INSERT OR REPLACE INTO service_samples(
                tuner_id, sampled_at, service_id, service_name, provider_name,
                pmt_pid, pcr_pid, running_status, elementary_stream_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tuner_id, sampled_at, int(service_id),
                svc.get("service_name"), svc.get("provider_name"),
                svc.get("pmt_pid"), svc.get("pcr_pid"),
                svc.get("running_status"), len(streams),
            ),
        )

    for service_id, svc in observed_services.items():
        service_count += 1

        conn.execute(
            """
            INSERT INTO services(
                tuner_id, service_id, service_name, provider_name,
                pmt_pid, pcr_pid, first_seen_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tuner_id, service_id) DO UPDATE SET
                service_name = excluded.service_name,
                provider_name = excluded.provider_name,
                pmt_pid = excluded.pmt_pid,
                pcr_pid = excluded.pcr_pid,
                last_seen_at = excluded.last_seen_at
            """,
            (
                tuner_id,
                service_id,
                svc.get("service_name"),
                svc.get("provider_name"),
                svc.get("pmt_pid"),
                svc.get("pcr_pid"),
                sampled_at,
                sampled_at,
            ),
        )

        service_db_id = int(
            conn.execute(
                """
                SELECT id FROM services
                WHERE tuner_id = ? AND service_id = ?
                """,
                (tuner_id, service_id),
            ).fetchone()[0]
        )

        for stream in svc.get("streams", []):
            pid = stream.get("pid")
            stream_type = stream.get("stream_type")

            # A stream without a PID cannot be monitored. Explicit lookup is
            # used so NULL stream_type does not create duplicate rows.
            if pid is None:
                continue

            existing = conn.execute(
                """
                SELECT id FROM service_streams
                WHERE service_id = ?
                  AND pid = ?
                  AND stream_type IS ?
                ORDER BY id
                LIMIT 1
                """,
                (service_db_id, pid, stream_type),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO service_streams(
                        service_id, pid, stream_type, stream_type_name,
                        language, first_seen_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        service_db_id,
                        pid,
                        stream_type,
                        stream.get("stream_type_name"),
                        stream.get("language"),
                        sampled_at,
                        sampled_at,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE service_streams
                    SET stream_type_name = ?, language = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        stream.get("stream_type_name"),
                        stream.get("language"),
                        sampled_at,
                        int(existing[0]),
                    ),
                )

            stream_count += 1

    return service_count, stream_count


def store_module_snapshot(
    conn: sqlite3.Connection,
    *,
    module_number: int,
    module_id: int,
    chassis_host: str,
    module_cfg: dict[str, Any],
    snapshot: dict[str, dict[str, Any]],
    sampled_at: str,
) -> dict[str, int]:
    parsed = {
        "tuner": parse_tuner_flux(snapshot["tuner_flux"]["text"]),
        "ts": parse_ts_flux(snapshot["ts_flux"]["text"]),
        "pid": parse_pidmapper(snapshot["pidmapper"]["text"]),
        "pcr": parse_pcr_inputs(snapshot["pcr"]["text"]),
        "tsdb": parse_tsdb_input(snapshot["tsdb_input"]["text"]),
    }

    counts = {
        "tuners": 0,
        "pid_samples": 0,
        "pcr_pid_samples": 0,
        "services": 0,
        "service_streams": 0,
        "counter_resets": 0,
    }

    remote_ip = module_cfg["remote_ip"]
    counter_scope = f"{chassis_host}|{remote_ip}"

    # Use the actual tuner/input IDs returned by WISI. Modules may expose
    # different input counts (for example 8 or 16).
    for input_id in sorted(parsed["tuner"]):
        tuner_id = ensure_tuner(conn, module_id, input_id, sampled_at)

        tuner_data = parsed["tuner"][input_id]
        ts_data = parsed["ts"].get(input_id, {})
        pid_input = parsed["pid"].get(input_id, {"pids": {}})
        pcr_data = parsed["pcr"].get(input_id, {})
        tsdb_data = parsed["tsdb"].get(input_id, {})

        hwid = pcr_data.get("hwid")
        if hwid is not None:
            conn.execute(
                "UPDATE tuners SET hwid = ?, last_seen_at = ? WHERE id = ?",
                (hwid, sampled_at, tuner_id),
            )
        else:
            conn.execute(
                "UPDATE tuners SET last_seen_at = ? WHERE id = ?",
                (sampled_at, tuner_id),
            )

        conn.execute(
            """
            INSERT INTO tuner_samples(
                tuner_id, sampled_at, lock_state, enabled, state, disabled,
                rf_level_dbm, snr_db,
                ber_text, ber_value, frequency_raw, frequency_offset_raw,
                symbol_rate, modulation, fec, isi, raw_source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tuner_id,
                sampled_at,
                tuner_data.get("lock_state"),
                as_int_bool(tuner_data.get("enabled")),
                tuner_data.get("state"),
                as_int_bool(tuner_data.get("disabled")),
                tuner_data.get("rf_level_dbm"),
                tuner_data.get("snr_db"),
                tuner_data.get("ber_text"),
                tuner_data.get("ber_value"),
                tuner_data.get("frequency_raw"),
                tuner_data.get("frequency_offset_raw"),
                tuner_data.get("symbol_rate"),
                tuner_data.get("modulation"),
                tuner_data.get("fec"),
                tuner_data.get("isi"),
                "tuner_flux.xmlc",
            ),
        )
        counts["tuners"] += 1

        ts_counter_specs = (
            ("tei", "tei_error_events"),
            ("sync", "sync_error_events"),
            ("input_cc", "cc_error_events"),
        )
        ts_totals: dict[str, int | None] = {}
        ts_deltas: dict[str, int | None] = {}

        for short_name, field_name in ts_counter_specs:
            current = pid_input.get(field_name)
            delta, reset = counter_delta(
                conn,
                f"{counter_scope}|{input_id}|{short_name}",
                current,
                sampled_at,
            )
            ts_totals[short_name] = current
            ts_deltas[short_name] = delta
            if reset and current is not None:
                counts["counter_resets"] += 1
                record_counter_reset(
                    conn,
                    sampled_at=sampled_at,
                    module_id=module_id,
                    tuner_id=tuner_id,
                    pid_id=None,
                    metric=short_name,
                    value=current,
                )

        conn.execute(
            """
            INSERT INTO ts_samples(
                tuner_id, sampled_at,
                current_bitrate_bps, min_bitrate_bps, max_bitrate_bps,
                tei_total, tei_delta,
                sync_error_total, sync_error_delta,
                input_cc_total, input_cc_delta,
                raw_source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tuner_id,
                sampled_at,
                ts_data.get("current_bitrate_bps"),
                ts_data.get("min_bitrate_bps"),
                ts_data.get("max_bitrate_bps"),
                ts_totals["tei"],
                ts_deltas["tei"],
                ts_totals["sync"],
                ts_deltas["sync"],
                ts_totals["input_cc"],
                ts_deltas["input_cc"],
                "tsio/inputs_conf_flux.xmlc + tsio/pidmapper.xmlc",
            ),
        )

        for pid, pid_data in pid_input.get("pids", {}).items():
            pid_id = get_or_create_pid(conn, tuner_id, pid, sampled_at)

            packet_delta, packet_reset = counter_delta(
                conn,
                f"{counter_scope}|{input_id}|pid|{pid}|packets",
                pid_data.get("packet_count"),
                sampled_at,
            )
            cc_delta, cc_reset = counter_delta(
                conn,
                f"{counter_scope}|{input_id}|pid|{pid}|cc",
                pid_data.get("cc_error_events"),
                sampled_at,
            )

            for reset, metric, value in (
                (packet_reset, "pid_packet_count", pid_data.get("packet_count")),
                (cc_reset, "pid_cc_error_events", pid_data.get("cc_error_events")),
            ):
                if reset and value is not None:
                    counts["counter_resets"] += 1
                    record_counter_reset(
                        conn,
                        sampled_at=sampled_at,
                        module_id=module_id,
                        tuner_id=tuner_id,
                        pid_id=pid_id,
                        metric=metric,
                        value=value,
                    )

            conn.execute(
                """
                INSERT INTO pid_samples(
                    pid_id, sampled_at, bitrate_bps,
                    packet_count, packet_delta,
                    cc_error_total, cc_error_delta,
                    pcr_present, scrambled,
                    pes_header, pes_scrambled, flags
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid_id,
                    sampled_at,
                    pid_data.get("bitrate_bps"),
                    pid_data.get("packet_count"),
                    packet_delta,
                    pid_data.get("cc_error_events"),
                    cc_delta,
                    as_int_bool(pid_data.get("pcr_present")),
                    as_int_bool(pid_data.get("scrambled")),
                    as_int_bool(pid_data.get("pes_header")),
                    as_int_bool(pid_data.get("pes_scrambled")),
                    pid_data.get("flags"),
                ),
            )
            counts["pid_samples"] += 1

        pcr_monitor = pcr_data.get("pcr_monitor", {})
        pcr_stats = pcr_monitor.get("statistics", {})
        input_stats = pcr_data.get("statistics", {})
        buffer_data = pcr_data.get("buffer", {})

        pcr_input_values: dict[str, int | None] = {}
        pcr_input_deltas: dict[str, int | None] = {}

        for field in PCR_COUNTER_FIELDS:
            current = pcr_stats.get(field)
            delta, reset = counter_delta(
                conn,
                f"{counter_scope}|{input_id}|pcr_input|{field}",
                current,
                sampled_at,
            )
            pcr_input_values[field] = current
            pcr_input_deltas[field] = delta
            if reset and current is not None:
                counts["counter_resets"] += 1
                record_counter_reset(
                    conn,
                    sampled_at=sampled_at,
                    module_id=module_id,
                    tuner_id=tuner_id,
                    pid_id=None,
                    metric=field,
                    value=current,
                )

        input_reg_values: dict[str, int | None] = {}
        input_reg_deltas: dict[str, int | None] = {}

        for field in INPUT_REGULATOR_COUNTER_FIELDS:
            current = input_stats.get(field)
            delta, reset = counter_delta(
                conn,
                f"{counter_scope}|{input_id}|input_regulator|{field}",
                current,
                sampled_at,
            )
            input_reg_values[field] = current
            input_reg_deltas[field] = delta
            if reset and current is not None:
                counts["counter_resets"] += 1
                record_counter_reset(
                    conn,
                    sampled_at=sampled_at,
                    module_id=module_id,
                    tuner_id=tuner_id,
                    pid_id=None,
                    metric=field,
                    value=current,
                )

        conn.execute(
            """
            INSERT INTO pcr_input_samples(
                tuner_id, sampled_at,
                monitor_enabled, periodic_rate,
                buffer_conf_jitter, buffer_jitter,
                buffer_size, buffer_level,
                fill_wait, freerunning,
                jitter_min, jitter_max, pcr_diff_max,
                ref_discontinuities_total, ref_discontinuities_delta,
                pcr_accuracy_errors_total, pcr_accuracy_errors_delta,
                pcr_repetition_errors_total, pcr_repetition_errors_delta,
                pcr_discontinuity_errors_total,
                pcr_discontinuity_errors_delta,
                to_wait_state_total, to_wait_state_delta,
                playout_fifo_reset_total, playout_fifo_reset_delta,
                unref_discontinuity_total, unref_discontinuity_delta,
                sample_ignored_total, sample_ignored_delta,
                into_freerunning_total, into_freerunning_delta,
                pcr_pid_changed_total, pcr_pid_changed_delta,
                raw_source
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                tuner_id,
                sampled_at,
                as_int_bool(pcr_monitor.get("enabled")),
                pcr_monitor.get("periodic_rate"),
                buffer_data.get("conf_jitter"),
                buffer_data.get("jitter"),
                buffer_data.get("size"),
                buffer_data.get("level"),
                as_int_bool(buffer_data.get("fill_wait")),
                as_int_bool(buffer_data.get("freerunning")),
                pcr_stats.get("jitter_min"),
                pcr_stats.get("jitter_max"),
                pcr_stats.get("pcr_diff_max"),
                pcr_input_values["ref_discontinuities"],
                pcr_input_deltas["ref_discontinuities"],
                pcr_input_values["pcr_accuracy_errors"],
                pcr_input_deltas["pcr_accuracy_errors"],
                pcr_input_values["pcr_repetition_errors"],
                pcr_input_deltas["pcr_repetition_errors"],
                pcr_input_values["pcr_discontinuity_indicator_errors"],
                pcr_input_deltas["pcr_discontinuity_indicator_errors"],
                input_reg_values["to_wait_state"],
                input_reg_deltas["to_wait_state"],
                input_reg_values["playout_fifo_reset"],
                input_reg_deltas["playout_fifo_reset"],
                input_reg_values["unref_discontinuity"],
                input_reg_deltas["unref_discontinuity"],
                input_reg_values["sample_ignored"],
                input_reg_deltas["sample_ignored"],
                input_reg_values["into_freerunning"],
                input_reg_deltas["into_freerunning"],
                input_reg_values["pcr_pid_changed"],
                input_reg_deltas["pcr_pid_changed"],
                "tsio/input_regulator/inputs.xmlc",
            ),
        )

        for pid, pcr_pid in pcr_data.get("pcr_pids", {}).items():
            active = current_pcr_pid_is_active(
                tuner_data=tuner_data,
                ts_data=ts_data,
                pid_input=pid_input,
                pcr_pid=pcr_pid,
                pid=pid,
            )

            pid_id = get_or_create_pid(conn, tuner_id, pid, sampled_at)
            stats = pcr_pid.get("statistics", {})
            values: dict[str, int | None] = {}
            deltas: dict[str, int | None] = {}

            for field in PCR_COUNTER_FIELDS:
                current = stats.get(field)

                # Stale inactive PCR records are retained for evidence, but do
                # not advance/reset production counter state.
                if active:
                    delta, reset = counter_delta(
                        conn,
                        f"{counter_scope}|{input_id}|pcr_pid|{pid}|{field}",
                        current,
                        sampled_at,
                    )
                else:
                    delta, reset = None, False

                values[field] = current
                deltas[field] = delta

                if reset and current is not None:
                    counts["counter_resets"] += 1
                    record_counter_reset(
                        conn,
                        sampled_at=sampled_at,
                        module_id=module_id,
                        tuner_id=tuner_id,
                        pid_id=pid_id,
                        metric=field,
                        value=current,
                    )

            conn.execute(
                """
                INSERT INTO pcr_pid_samples(
                    pid_id, sampled_at, active, last_seen_raw,
                    pcr_bitrate, stc_bitrate, stc_factor,
                    jitter_min, jitter_max, pcr_diff_max,
                    ref_discontinuities_total, ref_discontinuities_delta,
                    pcr_accuracy_errors_total, pcr_accuracy_errors_delta,
                    pcr_repetition_errors_total, pcr_repetition_errors_delta,
                    pcr_discontinuity_errors_total,
                    pcr_discontinuity_errors_delta
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid_id,
                    sampled_at,
                    as_int_bool(active),
                    pcr_pid.get("last_seen"),
                    pcr_pid.get("pcr_bitrate"),
                    pcr_pid.get("stc_bitrate"),
                    pcr_pid.get("stc_factor"),
                    stats.get("jitter_min"),
                    stats.get("jitter_max"),
                    stats.get("pcr_diff_max"),
                    values["ref_discontinuities"],
                    deltas["ref_discontinuities"],
                    values["pcr_accuracy_errors"],
                    deltas["pcr_accuracy_errors"],
                    values["pcr_repetition_errors"],
                    deltas["pcr_repetition_errors"],
                    values["pcr_discontinuity_indicator_errors"],
                    deltas["pcr_discontinuity_indicator_errors"],
                ),
            )
            counts["pcr_pid_samples"] += 1

        service_count, stream_count = upsert_transport_and_services(
            conn,
            tuner_id=tuner_id,
            tsdb_data=tsdb_data,
            sampled_at=sampled_at,
        )
        counts["services"] += service_count
        counts["service_streams"] += stream_count

    return counts


def fetch_chassis_snapshots(
    host: str,
    chassis_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Fetch both configured modules for one chassis using one WISI session.

    This function performs network I/O only. It does not touch SQLite.
    Modules remain sequential within a chassis; chassis are run in parallel.
    """
    chassis_started = time.perf_counter()
    client = WisiClient(host=host)

    if not client.establish_session():
        return {
            "host": host,
            "ok": False,
            "elapsed_seconds": round(time.perf_counter() - chassis_started, 3),
            "error": f"Unable to establish WISI chassis web session: {host}",
            "modules": {},
        }

    modules: dict[int, dict[str, Any]] = {}

    for module_number, cfg in chassis_cfg["modules"].items():
        t0 = time.perf_counter()
        try:
            snapshot = client.get_module_snapshot(cfg["remote"])

            # Parallel/non-alarming identity evidence. Failure of either
            # optional resource must not fail the existing production module poll.
            try:
                snapshot["dynamic_inputs_conf"] = client.get_resource(
                    cfg["remote"], DYNAMIC_MAPPING_RESOURCE
                )
            except Exception as exc:
                snapshot["dynamic_inputs_conf"] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

            try:
                snapshot["tuner_config"] = client.get_resource(
                    cfg["remote"], TUNER_CONFIG_RESOURCE
                )
            except Exception as exc:
                snapshot["tuner_config"] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

            failed = [
                name
                for name, result in snapshot.items()
                if name not in {"dynamic_inputs_conf", "tuner_config"}
                and not result.get("ok")
            ]
            if failed:
                details = "; ".join(
                    f"{name}: {snapshot[name].get('error')}" for name in failed
                )
                raise RuntimeError("Resource retrieval failed: " + details)

            modules[module_number] = {
                "ok": True,
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "snapshot": snapshot,
            }
        except Exception as exc:
            modules[module_number] = {
                "ok": False,
                "elapsed_seconds": round(time.perf_counter() - t0, 3),
                "error": f"{type(exc).__name__}: {exc}",
            }

    return {
        "host": host,
        "ok": all(info.get("ok") for info in modules.values()),
        "elapsed_seconds": round(time.perf_counter() - chassis_started, 3),
        "modules": modules,
    }



def wellav_read_api(
    ip: str,
    endpoint: str,
) -> dict[str, Any]:
    """Read one verified Wellav CMP201 JSON endpoint.

    The CMP201 web interface labels these requests as form-urlencoded, but the
    request body is raw JSON. This function performs read-only HTTP POSTs only.
    It never calls a Set*/Apply*/Clear* endpoint.
    """
    allowed = {
        WELLAV_CONFIG_ENDPOINT,
        WELLAV_STATUS_ENDPOINT,
    }

    if endpoint not in allowed:
        raise ValueError(
            f"Unsupported/non-read-only Wellav endpoint: {endpoint}"
        )

    chassis_type = int(WELLAV_CMP201["chassis_type"])

    if not WELLAV_API_TOKEN:
        raise RuntimeError(
            "WELLAV_API_TOKEN environment variable is not configured"
        )

    body = json.dumps(
        {
            "token": WELLAV_API_TOKEN,
            "chassistype": chassis_type,
        },
        separators=(",", ":"),
    ).encode("utf-8")

    url = f"http://{ip}/ajax/{endpoint}"

    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept":
                "application/json, text/javascript, */*; q=0.01",
            "Content-Type":
                "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With":
                "XMLHttpRequest",
            "Origin": f"http://{ip}",
            "Referer": f"http://{ip}/",
            "Cache-Control": "no-cache",
        },
    )

    with urllib.request.urlopen(
        request,
        timeout=WELLAV_HTTP_TIMEOUT_SECONDS,
    ) as response:
        raw = response.read().decode(
            "utf-8",
            errors="replace",
        )

    payload = json.loads(raw)

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Wellav {ip}/{endpoint} returned a non-object JSON response"
        )

    if payload.get("code") != 0:
        raise RuntimeError(
            f"Wellav {ip}/{endpoint} API error: "
            f"code={payload.get('code')} "
            f"description={payload.get('description')}"
        )

    data = payload.get("data")

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Wellav {ip}/{endpoint} returned no valid data object"
        )

    return data


def fetch_wellav_module_snapshot(
    module_number: int,
    module_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Fetch configuration and live status for one Wellav CMP201 module.

    Network I/O only. No SQLite writes are performed here.
    """
    started = time.perf_counter()
    ip = str(module_cfg["ip"])

    try:
        config = wellav_read_api(
            ip,
            WELLAV_CONFIG_ENDPOINT,
        )

        status = wellav_read_api(
            ip,
            WELLAV_STATUS_ENDPOINT,
        )

        return {
            "ok": True,
            "module_number": int(module_number),
            "module_name": module_cfg.get("name"),
            "ip": ip,
            "elapsed_seconds": round(
                time.perf_counter() - started,
                3,
            ),
            "config": config,
            "status": status,
        }

    except Exception as exc:
        return {
            "ok": False,
            "module_number": int(module_number),
            "module_name": module_cfg.get("name"),
            "ip": ip,
            "elapsed_seconds": round(
                time.perf_counter() - started,
                3,
            ),
            "error": f"{type(exc).__name__}: {exc}",
        }


def fetch_wellav_snapshots() -> dict[int, dict[str, Any]]:
    """Fetch all configured Wellav modules in parallel.

    This is intentionally independent from collect_once() for initial
    commissioning. Adding these functions does not change production polling.
    """
    modules = WELLAV_CMP201["modules"]

    results: dict[int, dict[str, Any]] = {}

    with ThreadPoolExecutor(
        max_workers=max(1, len(modules))
    ) as executor:

        futures = {
            executor.submit(
                fetch_wellav_module_snapshot,
                module_number,
                module_cfg,
            ): module_number
            for module_number, module_cfg in modules.items()
        }

        for future in as_completed(futures):
            module_number = futures[future]

            try:
                results[module_number] = future.result()
            except Exception as exc:
                cfg = modules[module_number]

                results[module_number] = {
                    "ok": False,
                    "module_number": module_number,
                    "module_name": cfg.get("name"),
                    "ip": cfg.get("ip"),
                    "elapsed_seconds": 0.0,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    return results



def _wellav_number(
    value: Any,
) -> float | None:
    """Return a numeric Wellav field without inventing a value."""
    if value is None or value == "":
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wellav_int(
    value: Any,
) -> int | None:
    """Return an integer Wellav field without inventing a value."""
    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def store_wellav_module_snapshot(
    conn: sqlite3.Connection,
    *,
    acquired: dict[str, Any],
    sampled_at: str,
) -> dict[str, int]:
    """Store one successfully acquired Wellav CMP201 module snapshot.

    Configuration/status rows are preserved for every physical input.

    Program/service rows are written only when that input is currently:
      - enabled,
      - demodulator locked, and
      - carrying a positive transport bitrate.

    This prevents the CMP201's retained stale program lists on disabled or
    unlocked slots from becoming current service-monitoring evidence.
    """
    if not acquired.get("ok"):
        raise ValueError(
            "Cannot store unsuccessful Wellav acquisition"
        )

    module_number = int(acquired["module_number"])
    module_ip = str(acquired["ip"])

    config = acquired.get("config") or {}
    status = acquired.get("status") or {}

    config_lookup: dict[
        tuple[int, int],
        dict[str, Any],
    ] = {}

    status_lookup: dict[
        tuple[int, int],
        dict[str, Any],
    ] = {}

    for port in config.get("portlist", []):
        port_id = int(port["port"])

        for channel in port.get("channellist", []):
            channel_id = int(channel["channel"])

            config_lookup[
                (port_id, channel_id)
            ] = channel

    for port in status.get("portlist", []):
        port_id = int(port["port"])

        for channel in port.get("channellist", []):
            channel_id = int(channel["channel"])

            status_lookup[
                (port_id, channel_id)
            ] = channel

    all_inputs = sorted(
        set(config_lookup)
        | set(status_lookup)
    )

    counts = {
        "inputs": 0,
        "services": 0,
        "service_observation_inputs": 0,
    }

    for port_id, channel_id in all_inputs:

        cfg = config_lookup.get(
            (port_id, channel_id),
            {},
        )

        live = status_lookup.get(
            (port_id, channel_id),
            {},
        )

        ui_channel = (
            f"{port_id + 1}.{channel_id + 1}"
        )

        enabled = _wellav_int(
            cfg.get("enable")
        )

        lock_status = _wellav_int(
            live.get("lockstatus")
        )

        total_bitrate = _wellav_number(
            live.get("totalbitrate")
        )

        programs = live.get("programlist")

        if not isinstance(programs, list):
            programs = []

        # C/N has been verified against the Wellav UI as tenths of dB.
        cn_raw = _wellav_number(
            live.get("cn")
        )

        cn_db = (
            cn_raw / 10.0
            if cn_raw is not None
            else None
        )

        conn.execute(
            """
            INSERT OR REPLACE INTO wellav_input_samples(
                sampled_at,
                module_number,
                module_ip,
                port,
                channel,
                ui_channel,
                enabled,
                satellite_frequency_mhz,
                symbol_rate_kbaud,
                lnb_frequency_mhz,
                lnb_power_support,
                lnb_22khz,
                lock_status,
                total_bitrate_bps,
                effective_bitrate_bps,
                rf_level_dbm,
                cn_raw,
                cn_db,
                packet_error_rate,
                ber_count,
                ber_exponent,
                signal_type,
                frequency_tune,
                link_margin,
                fec_code_rate,
                modulation,
                program_count
            )
            VALUES(
                ?,?,?,?,?,?,
                ?,?,?,?,?,?,
                ?,?,?,?,?,?,
                ?,?,?,?,?,?,
                ?,?,?
            )
            """,
            (
                sampled_at,
                module_number,
                module_ip,
                port_id,
                channel_id,
                ui_channel,

                enabled,

                _wellav_number(
                    cfg.get("satellitefrequency")
                ),

                _wellav_number(
                    cfg.get("symbolrate")
                ),

                _wellav_number(
                    cfg.get("lnbfrequency")
                ),

                _wellav_int(
                    cfg.get("lnbpowersupport")
                ),

                _wellav_int(
                    cfg.get("lnb22khz")
                ),

                lock_status,

                total_bitrate,

                _wellav_number(
                    live.get("effectivebitrate")
                ),

                _wellav_number(
                    live.get("rflevel")
                ),

                cn_raw,
                cn_db,

                _wellav_number(
                    live.get("packeterrorrate")
                ),

                _wellav_number(
                    live.get("bercnt")
                ),

                _wellav_number(
                    live.get("berexponent")
                ),

                (
                    None
                    if live.get("signaltype") is None
                    else str(live.get("signaltype"))
                ),

                _wellav_number(
                    live.get("frequencytune")
                ),

                _wellav_number(
                    live.get("linkmargin")
                ),

                (
                    None
                    if live.get("feccoderate") is None
                    else str(live.get("feccoderate"))
                ),

                (
                    None
                    if live.get("modulation") is None
                    else str(live.get("modulation"))
                ),

                len(programs),
            ),
        )

        counts["inputs"] += 1

        service_observation_available = (
            enabled == 1
            and lock_status == 1
            and total_bitrate is not None
            and total_bitrate > 0
            and len(programs) > 0
        )

        if not service_observation_available:
            continue

        counts[
            "service_observation_inputs"
        ] += 1

        seen_service_ids: set[int] = set()

        for program in programs:

            service_id = _wellav_int(
                program.get("serviceid")
            )

            if service_id is None:
                continue

            # Do not let malformed duplicate entries violate the
            # per-sample UNIQUE constraint.
            if service_id in seen_service_ids:
                continue

            seen_service_ids.add(
                service_id
            )

            service_name = str(
                program.get("servicename")
                or ""
            ).strip()

            conn.execute(
                """
                INSERT OR REPLACE INTO wellav_service_samples(
                    sampled_at,
                    module_number,
                    module_ip,
                    port,
                    channel,
                    ui_channel,
                    service_id,
                    service_name
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    sampled_at,
                    module_number,
                    module_ip,
                    port_id,
                    channel_id,
                    ui_channel,
                    service_id,
                    service_name,
                ),
            )

            counts["services"] += 1

    return counts


def collect_once() -> dict[str, Any]:
    """Run one controlled poll cycle.

    Network acquisition is parallel by chassis. SQLite parsing/storage remains
    single-threaded in the main thread. Each successful module is committed
    independently so one failed module cannot discard valid observations from
    other modules.
    """
    started_perf = time.perf_counter()
    started_at = utc_now()
    modules_attempted = sum(len(c["modules"]) for c in WISI_CHASSIS.values())
    modules_succeeded = 0
    error_messages: list[str] = []
    module_results: dict[str, dict[str, Any]] = {}

    # Wellav accounting is deliberately separate from the established WISI
    # poll_runs/success semantics.
    wellav_modules_attempted = len(WELLAV_CMP201["modules"])
    wellav_modules_succeeded = 0
    wellav_error_messages: list[str] = []
    wellav_module_results: dict[str, dict[str, Any]] = {}

    # Phase 1: WISI network acquisition only, four chassis in parallel.
    chassis_results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(WISI_CHASSIS))) as executor:
        futures = {
            executor.submit(fetch_chassis_snapshots, host, cfg): host
            for host, cfg in WISI_CHASSIS.items()
        }

        for future in as_completed(futures):
            host = futures[future]
            try:
                chassis_results[host] = future.result()
            except Exception as exc:
                chassis_results[host] = {
                    "host": host,
                    "ok": False,
                    "elapsed_seconds": 0.0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "modules": {},
                }

    # Phase 1B: Wellav CMP201 network acquisition only.
    #
    # This remains independent from WISI acquisition. Failure of Wellav
    # telemetry must not invalidate successfully acquired WISI observations.
    wellav_started_at = utc_now()
    wellav_started_perf = time.perf_counter()
    wellav_results = fetch_wellav_snapshots()

    # Phase 2: one main-thread SQLite writer.
    with open_db() as conn:
        ensure_dynamic_mapping_schema(conn)
        ensure_wellav_schema(conn)
        verify_schema(conn)
        module_ids = ensure_equipment(conn, started_at)

        poll_cursor = conn.execute(
            """INSERT INTO poll_runs(
                   started_at,completed_at,success,duration_seconds,
                   modules_attempted,modules_succeeded,error_message)
               VALUES (?,NULL,0,NULL,?,0,NULL)""",
            (started_at, modules_attempted),
        )
        poll_run_id = int(poll_cursor.lastrowid)
        conn.commit()

        # Store one coherent Wellav timestamp across all six modules in this
        # collector cycle. Each successful module commits independently.
        wellav_sampled_at = utc_now()

        for module_number, module_cfg in WELLAV_CMP201["modules"].items():
            key = (
                f"{WELLAV_CMP201['baseboard_ip']}"
                f"/M{module_number}"
            )

            acquired = wellav_results.get(module_number)

            if not acquired:
                msg = (
                    f"Wellav Module {module_number} "
                    f"({module_cfg['ip']}): "
                    f"no acquisition result returned"
                )

                wellav_error_messages.append(msg)

                wellav_module_results[key] = {
                    "ok": False,
                    "module_number": module_number,
                    "module_ip": module_cfg["ip"],
                    "error": msg,
                }

                continue

            if not acquired.get("ok"):
                msg = (
                    f"Wellav Module {module_number} "
                    f"({module_cfg['ip']}): "
                    f"{acquired.get('error', 'acquisition failed')}"
                )

                wellav_error_messages.append(msg)

                wellav_module_results[key] = {
                    "ok": False,
                    "module_number": module_number,
                    "module_ip": module_cfg["ip"],
                    "elapsed_seconds":
                        acquired.get("elapsed_seconds", 0.0),
                    "error": msg,
                }

                continue

            savepoint = f"wellav_module_{module_number}"

            conn.execute(
                f"SAVEPOINT {savepoint}"
            )

            try:
                counts = store_wellav_module_snapshot(
                    conn,
                    acquired=acquired,
                    sampled_at=wellav_sampled_at,
                )

                conn.execute(
                    f"RELEASE SAVEPOINT {savepoint}"
                )

                conn.commit()

                wellav_modules_succeeded += 1

                wellav_module_results[key] = {
                    "ok": True,
                    "module_number": module_number,
                    "module_ip": module_cfg["ip"],
                    "elapsed_seconds":
                        acquired.get("elapsed_seconds", 0.0),
                    "committed": True,
                    **counts,
                }

            except Exception as exc:
                conn.execute(
                    f"ROLLBACK TO SAVEPOINT {savepoint}"
                )

                conn.execute(
                    f"RELEASE SAVEPOINT {savepoint}"
                )

                conn.commit()

                msg = (
                    f"Wellav Module {module_number} "
                    f"({module_cfg['ip']}): "
                    f"{type(exc).__name__}: {exc}"
                )

                wellav_error_messages.append(msg)

                wellav_module_results[key] = {
                    "ok": False,
                    "module_number": module_number,
                    "module_ip": module_cfg["ip"],
                    "elapsed_seconds":
                        acquired.get("elapsed_seconds", 0.0),
                    "committed": False,
                    "error": msg,
                }

        wellav_completed_at = utc_now()

        wellav_duration = round(
            time.perf_counter() - wellav_started_perf,
            3,
        )

        wellav_success = (
            wellav_modules_succeeded
            == wellav_modules_attempted
        )

        conn.execute(
            """
            INSERT INTO wellav_poll_runs(
                started_at,
                completed_at,
                success,
                duration_seconds,
                modules_attempted,
                modules_succeeded,
                error_message
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                wellav_started_at,
                wellav_completed_at,
                1 if wellav_success else 0,
                wellav_duration,
                wellav_modules_attempted,
                wellav_modules_succeeded,
                (
                    "\n".join(wellav_error_messages)
                    if wellav_error_messages
                    else None
                ),
            ),
        )

        conn.commit()

        for host, chassis_cfg in WISI_CHASSIS.items():
            chassis_result = chassis_results.get(host)

            if not chassis_result:
                msg = f"{host}: no chassis acquisition result returned"
                error_messages.append(msg)
                for module_number in chassis_cfg["modules"]:
                    key = f"{host}/M{module_number}"
                    module_results[key] = {
                        "ok": False,
                        "host": host,
                        "module_number": module_number,
                        "elapsed_seconds": 0.0,
                        "error": msg,
                    }
                continue

            chassis_error = chassis_result.get("error")
            if chassis_error:
                error_messages.append(chassis_error)

            for module_number, cfg in chassis_cfg["modules"].items():
                key = f"{host}/M{module_number}"
                acquired = chassis_result.get("modules", {}).get(module_number)

                if not acquired:
                    msg = (
                        f"{host} Module {module_number}: "
                        f"{chassis_error or 'no module acquisition result returned'}"
                    )
                    error_messages.append(msg)
                    module_results[key] = {
                        "ok": False,
                        "host": host,
                        "module_number": module_number,
                        "elapsed_seconds": chassis_result.get("elapsed_seconds", 0.0),
                        "error": msg,
                    }
                    continue

                if not acquired.get("ok"):
                    msg = (
                        f"{host} Module {module_number}: "
                        f"{acquired.get('error', 'acquisition failed')}"
                    )
                    error_messages.append(msg)
                    module_results[key] = {
                        "ok": False,
                        "host": host,
                        "module_number": module_number,
                        "elapsed_seconds": acquired.get("elapsed_seconds", 0.0),
                        "error": msg,
                    }
                    continue

                savepoint = f"module_{host.replace('.', '_')}_{module_number}"
                conn.execute(f"SAVEPOINT {savepoint}")
                try:
                    module_sampled_at = utc_now()
                    module_id = module_ids[(host, module_number)]
                    counts = store_module_snapshot(
                        conn,
                        module_number=module_number,
                        module_id=module_id,
                        chassis_host=host,
                        module_cfg=cfg,
                        snapshot=acquired["snapshot"],
                        sampled_at=module_sampled_at,
                    )

                    tuner_config_result = acquired["snapshot"].get("tuner_config", {})
                    tuner_config_rows = 0
                    tuner_config_error: str | None = None

                    if tuner_config_result.get("ok"):
                        configured_tuners = parse_tuner_config(
                            tuner_config_result.get("text") or ""
                        )
                        for tuner_object_id in sorted(configured_tuners):
                            config_row = configured_tuners[tuner_object_id]
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO tuner_config_samples(
                                    module_id,sampled_at,tuner_object_id,
                                    enabled,state,tuner_type,type_name,
                                    frequency_raw,frequency_mhz,
                                    symbol_rate_raw,symbol_rate_mbd,
                                    polarisation_code,polarisation,
                                    fec_config,modulation_config,is_id_config,
                                    lnb,lo_frequency_raw,voltage,tone
                                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                """,
                                (
                                    module_id, module_sampled_at, tuner_object_id,
                                    as_int_bool(config_row.get("enabled")),
                                    config_row.get("state"),
                                    config_row.get("type"),
                                    config_row.get("type_name"),
                                    config_row.get("frequency_raw"),
                                    config_row.get("frequency_mhz"),
                                    config_row.get("symbol_rate_raw"),
                                    config_row.get("symbol_rate_mbd"),
                                    config_row.get("polarisation_code"),
                                    config_row.get("polarisation"),
                                    config_row.get("fec_config"),
                                    config_row.get("modulation_config"),
                                    config_row.get("is_id_config"),
                                    config_row.get("lnb"),
                                    config_row.get("lo_frequency_raw"),
                                    config_row.get("voltage"),
                                    config_row.get("tone"),
                                ),
                            )
                            tuner_config_rows += 1
                    else:
                        tuner_config_error = (
                            tuner_config_result.get("error") or "resource unavailable"
                        )

                    mapping_result = acquired["snapshot"].get("dynamic_inputs_conf", {})
                    mapping_records: list[dict[str, Any]] = []
                    mapping_error: str | None = None

                    if mapping_result.get("ok"):
                        relationships = parse_dynamic_input_relationships(
                            mapping_result.get("text") or ""
                        )
                        tuner_flux = parse_tuner_flux(
                            acquired["snapshot"]["tuner_flux"]["text"]
                        )
                        tsdb_inputs = parse_tsdb_input(
                            acquired["snapshot"]["tsdb_input"]["text"]
                        )
                        evidence_at = utc_now()

                        for configured_input_id in sorted(relationships):
                            relation = relationships[configured_input_id]
                            tuner_object_id = relation.get("tuner_object_id")
                            tuner_present = (
                                tuner_object_id is not None
                                and tuner_object_id in tuner_flux
                            )
                            mapping_status = (
                                "RESOLVED" if tuner_present else "UNRESOLVED"
                            )
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO input_tuner_mapping_samples(
                                    module_id,sampled_at,configured_input_id,
                                    configured_name,configured_uuid,input_enabled,
                                    error_status,tuner_object_id,hwid,physical_port,
                                    mapping_status
                                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                                """,
                                (
                                    module_id, module_sampled_at, configured_input_id,
                                    relation.get("configured_name"),
                                    relation.get("uuid"),
                                    relation.get("input_enabled"),
                                    relation.get("error_status"),
                                    tuner_object_id,
                                    relation.get("hwid"),
                                    relation.get("physical_port"),
                                    mapping_status,
                                ),
                            )
                            mapping_records.append({
                                "observed_at": evidence_at,
                                "sampled_at": module_sampled_at,
                                "host": host,
                                "module": module_number,
                                "remote_ip": cfg.get("remote_ip"),
                                "source": DYNAMIC_MAPPING_RESOURCE,
                                "mapping_status": mapping_status,
                                "resolved_tuner_present": tuner_present,
                                "configured_tsdb_present": configured_input_id in tsdb_inputs,
                                **relation,
                            })
                        append_dynamic_mapping_evidence(mapping_records)
                    else:
                        mapping_error = (
                            mapping_result.get("error") or "resource unavailable"
                        )

                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    conn.commit()

                    modules_succeeded += 1
                    module_results[key] = {
                        "ok": True,
                        "host": host,
                        "module_number": module_number,
                        "elapsed_seconds": acquired.get("elapsed_seconds", 0.0),
                        "committed": True,
                        "dynamic_mapping_rows": len(mapping_records),
                        "dynamic_mapping_resolved": sum(
                            1 for r in mapping_records
                            if r.get("mapping_status") == "RESOLVED"
                        ),
                        "dynamic_mapping_unresolved": sum(
                            1 for r in mapping_records
                            if r.get("mapping_status") == "UNRESOLVED"
                        ),
                        "dynamic_mapping_error": mapping_error,
                        "tuner_config_rows": tuner_config_rows,
                        "tuner_config_error": tuner_config_error,
                        **counts,
                    }
                except Exception as exc:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    conn.commit()
                    msg = (
                        f"{host} Module {module_number}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    error_messages.append(msg)
                    module_results[key] = {
                        "ok": False,
                        "host": host,
                        "module_number": module_number,
                        "elapsed_seconds": acquired.get("elapsed_seconds", 0.0),
                        "committed": False,
                        "error": msg,
                    }

        completed_at = utc_now()
        duration = round(time.perf_counter() - started_perf, 3)
        success = modules_succeeded == modules_attempted

        conn.execute(
            """UPDATE poll_runs SET completed_at=?,success=?,duration_seconds=?,
                   modules_succeeded=?,error_message=? WHERE id=?""",
            (
                completed_at,
                1 if success else 0,
                duration,
                modules_succeeded,
                "\n".join(error_messages) if error_messages else None,
                poll_run_id,
            ),
        )
        conn.commit()

    return {
        "poll_run_id": poll_run_id,
        "started_at": started_at,
        "completed_at": completed_at,
        "success": success,
        "duration_seconds": duration,
        "modules_attempted": modules_attempted,
        "modules_succeeded": modules_succeeded,
        "modules": module_results,
        "errors": error_messages,
        "wellav": {
            "started_at": wellav_started_at,
            "completed_at": wellav_completed_at,
            "sampled_at": wellav_sampled_at,
            "success": wellav_success,
            "duration_seconds": wellav_duration,
            "modules_attempted": wellav_modules_attempted,
            "modules_succeeded": wellav_modules_succeeded,
            "modules": wellav_module_results,
            "errors": wellav_error_messages,
        },
    }


def print_result(result: dict[str, Any]) -> None:
    print("=" * 88)
    print("WISI GT34 CONTROLLED COLLECTION CYCLE")
    print("=" * 88)
    print(f"Poll run ID      : {result['poll_run_id']}")
    print(f"Started          : {result['started_at']}")
    print(f"Completed        : {result['completed_at']}")
    print(f"Duration         : {result['duration_seconds']} s")
    print(
        f"Modules          : {result['modules_succeeded']}/"
        f"{result['modules_attempted']} succeeded"
    )
    print(f"Overall success  : {result['success']}")

    for module_key in sorted(result["modules"]):
        info = result["modules"][module_key]
        print("-" * 88)
        print(
            f"{module_key}: "
            f"{'OK' if info.get('ok') else 'FAILED'} "
            f"({info.get('elapsed_seconds')} s)"
        )
        if info.get("ok"):
            print(
                "  tuner_samples={tuners} pid_samples={pid_samples} "
                "pcr_pid_samples={pcr_pid_samples} services={services} "
                "service_streams={service_streams} resets={counter_resets}".format(
                    **info
                )
            )
        else:
            print(f"  {info.get('error')}")

    if result["errors"]:
        print("-" * 88)
        print("ERRORS")
        for error in result["errors"]:
            print(f"  - {error}")

    print("=" * 88)


def run_continuous(interval_seconds: float = CONTINUOUS_INTERVAL_SECONDS) -> None:
    """Run non-overlapping collection cycles on a monotonic start-to-start schedule."""
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be greater than zero")

    logger = configure_continuous_logging()
    logger.info(
        "Continuous collector starting | interval=%.3f s | pid=%s",
        interval_seconds,
        __import__("os").getpid(),
    )

    cycle_number = 0
    next_deadline = time.monotonic()

    try:
        while True:
            cycle_number += 1
            cycle_started = time.monotonic()

            try:
                result = collect_once()
                level = logging.INFO if result["success"] else logging.WARNING
                logger.log(
                    level,
                    "Cycle %d complete | poll_run_id=%s | success=%s | "
                    "modules=%s/%s | duration=%.3f s | errors=%d",
                    cycle_number,
                    result["poll_run_id"],
                    result["success"],
                    result["modules_succeeded"],
                    result["modules_attempted"],
                    result["duration_seconds"],
                    len(result["errors"]),
                )
            except Exception:
                logger.exception(
                    "Cycle %d failed with an unhandled exception",
                    cycle_number,
                )

            next_deadline += interval_seconds
            now = time.monotonic()
            remaining = next_deadline - now

            if remaining > 0:
                time.sleep(remaining)
            else:
                overrun = -remaining
                logger.warning(
                    "Cycle %d schedule overrun | %.3f s late | "
                    "cycle_elapsed=%.3f s",
                    cycle_number,
                    overrun,
                    now - cycle_started,
                )
                # If the process fell more than one whole interval behind,
                # discard missed slots rather than launching catch-up cycles.
                missed = int(overrun // interval_seconds)
                if missed:
                    next_deadline += missed * interval_seconds

    except KeyboardInterrupt:
        logger.info("Continuous collector stopped by operator")
        print("\nContinuous collector stopped.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WISI GT34 central collector"
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="run continuously on a 5-second start-to-start schedule",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.continuous:
        run_continuous()
    else:
        print_result(collect_once())
