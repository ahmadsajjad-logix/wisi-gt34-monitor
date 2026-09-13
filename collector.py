from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATABASE_PATH, WISI_HOST, WISI_MODULES
from parsers import (
    parse_pidmapper,
    parse_pcr_inputs,
    parse_ts_flux,
    parse_tsdb_input,
    parse_tuner_flux,
)
from wisi_client import WisiClient


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
        "pids": {"first_seen_at", "last_seen_at"},
        "transport_streams": {"first_seen_at", "last_seen_at"},
        "services": {"first_seen_at", "last_seen_at"},
        "service_streams": {"first_seen_at", "last_seen_at"},
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


def ensure_equipment(conn: sqlite3.Connection, sampled_at: str) -> dict[int, int]:
    conn.execute(
        """
        INSERT INTO chassis(host, name, created_at, last_seen_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(host) DO UPDATE SET
            last_seen_at = excluded.last_seen_at
        """,
        (WISI_HOST, "WISI-IRD-01", sampled_at, sampled_at),
    )

    chassis_id = conn.execute(
        "SELECT id FROM chassis WHERE host = ?",
        (WISI_HOST,),
    ).fetchone()[0]

    module_ids: dict[int, int] = {}

    for module_number, cfg in WISI_MODULES.items():
        conn.execute(
            """
            INSERT INTO modules(
                chassis_id, module_number, module_name,
                remote_identifier, remote_ip,
                created_at, last_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chassis_id, module_number) DO UPDATE SET
                module_name = excluded.module_name,
                remote_identifier = excluded.remote_identifier,
                remote_ip = excluded.remote_ip,
                last_seen_at = excluded.last_seen_at
            """,
            (
                chassis_id,
                module_number,
                cfg["name"],
                cfg["remote"],
                cfg["remote_ip"],
                sampled_at,
                sampled_at,
            ),
        )

        module_id = conn.execute(
            """
            SELECT id FROM modules
            WHERE chassis_id = ? AND module_number = ?
            """,
            (chassis_id, module_number),
        ).fetchone()[0]
        module_ids[module_number] = module_id

        for input_id in range(8):
            conn.execute(
                """
                INSERT INTO tuners(
                    module_id, input_id, hwid, display_number,
                    configured_name, first_seen_at, last_seen_at
                )
                VALUES (?, ?, NULL, ?, NULL, ?, ?)
                ON CONFLICT(module_id, input_id) DO UPDATE SET
                    display_number = excluded.display_number,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    module_id,
                    input_id,
                    input_id + 1,
                    sampled_at,
                    sampled_at,
                ),
            )

    return module_ids


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

    for service_id, svc in tsdb_data.get("services", {}).items():
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

    for input_id in range(8):
        tuner_id = get_tuner_id(conn, module_id, input_id)

        tuner_data = parsed["tuner"].get(input_id, {})
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
                tuner_id, sampled_at, lock_state, rf_level_dbm, snr_db,
                ber_text, ber_value, frequency_raw, frequency_offset_raw,
                symbol_rate, modulation, fec, isi, raw_source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tuner_id,
                sampled_at,
                tuner_data.get("lock_state"),
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
                f"{remote_ip}|{input_id}|{short_name}",
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
                f"{remote_ip}|{input_id}|pid|{pid}|packets",
                pid_data.get("packet_count"),
                sampled_at,
            )
            cc_delta, cc_reset = counter_delta(
                conn,
                f"{remote_ip}|{input_id}|pid|{pid}|cc",
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
                f"{remote_ip}|{input_id}|pcr_input|{field}",
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
                f"{remote_ip}|{input_id}|input_regulator|{field}",
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
                        f"{remote_ip}|{input_id}|pcr_pid|{pid}|{field}",
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


def collect_once() -> dict[str, Any]:
    started_perf = time.perf_counter()
    started_at = utc_now()
    modules_attempted = len(WISI_MODULES)
    modules_succeeded = 0
    error_messages: list[str] = []
    module_results: dict[int, dict[str, Any]] = {}

    client = WisiClient()

    with open_db() as conn:
        verify_schema(conn)
        module_ids = ensure_equipment(conn, started_at)

        poll_cursor = conn.execute(
            """
            INSERT INTO poll_runs(
                started_at, completed_at, success, duration_seconds,
                modules_attempted, modules_succeeded, error_message
            )
            VALUES (?, NULL, 0, NULL, ?, 0, NULL)
            """,
            (started_at, modules_attempted),
        )
        poll_run_id = int(poll_cursor.lastrowid)

        # Persist the audit row before starting the controlled collection
        # transaction. Measurement/inventory/counter-state writes below are
        # committed only if every configured module succeeds.
        conn.commit()

        collection_ok = False

        try:
            if not client.establish_session():
                raise RuntimeError("Unable to establish WISI chassis web session")

            # Begin one coherent collection transaction covering all modules.
            conn.execute("BEGIN")

            for module_number, cfg in WISI_MODULES.items():
                module_started = time.perf_counter()

                try:
                    snapshot = client.get_module_snapshot(cfg["remote"])

                    failed_resources = [
                        name
                        for name, result in snapshot.items()
                        if not result.get("ok")
                    ]
                    if failed_resources:
                        details = "; ".join(
                            f"{name}: {snapshot[name].get('error')}"
                            for name in failed_resources
                        )
                        raise RuntimeError(
                            "Resource retrieval failed: " + details
                        )

                    counts = store_module_snapshot(
                        conn,
                        module_number=module_number,
                        module_id=module_ids[module_number],
                        module_cfg=cfg,
                        snapshot=snapshot,
                        sampled_at=utc_now(),
                    )

                    modules_succeeded += 1
                    module_results[module_number] = {
                        "ok": True,
                        "elapsed_seconds": round(
                            time.perf_counter() - module_started, 3
                        ),
                        **counts,
                    }

                except Exception as exc:
                    message = (
                        f"Module {module_number}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    error_messages.append(message)
                    module_results[module_number] = {
                        "ok": False,
                        "elapsed_seconds": round(
                            time.perf_counter() - module_started, 3
                        ),
                        "error": message,
                    }
                    raise

            # Every configured module succeeded. Commit the entire collection
            # transaction as one coherent database state.
            conn.commit()
            collection_ok = True

        except Exception as exc:
            if conn.in_transaction:
                conn.rollback()

            # Avoid duplicating a module-level error already recorded above.
            if not error_messages:
                message = f"Collector: {type(exc).__name__}: {exc}"
                error_messages.append(message)

            # Any successful module result in memory was rolled back together
            # with the failed cycle. Mark that explicitly in the printed result.
            if modules_succeeded:
                for info in module_results.values():
                    if info.get("ok"):
                        info["committed"] = False

            modules_succeeded = 0

        completed_at = utc_now()
        duration = round(time.perf_counter() - started_perf, 3)
        success = collection_ok and modules_succeeded == modules_attempted

        # poll_runs is an audit record and must survive even when the coherent
        # measurement transaction above rolls back.
        conn.execute(
            """
            UPDATE poll_runs
            SET completed_at = ?,
                success = ?,
                duration_seconds = ?,
                modules_succeeded = ?,
                error_message = ?
            WHERE id = ?
            """,
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

    for module_number in sorted(result["modules"]):
        info = result["modules"][module_number]
        print("-" * 88)
        print(
            f"Module {module_number}: "
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


if __name__ == "__main__":
    print_result(collect_once())
