from __future__ import annotations

import sqlite3
from pathlib import Path

from config import DATABASE_PATH


SEPARATOR = "=" * 100
SUB_SEPARATOR = "-" * 100


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def print_rows(rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("(no rows)")
        return

    columns = rows[0].keys()

    for row in rows:
        print(" | ".join(f"{column}={row[column]}" for column in columns))


def section(title: str) -> None:
    print()
    print(SEPARATOR)
    print(title)
    print(SEPARATOR)


def validate_poll_runs(conn: sqlite3.Connection) -> None:
    section("1. POLL RUNS")

    rows = conn.execute(
        """
        SELECT
            id,
            started_at,
            completed_at,
            success,
            duration_seconds,
            modules_attempted,
            modules_succeeded,
            error_message
        FROM poll_runs
        ORDER BY id
        """
    ).fetchall()

    print_rows(rows)


def validate_table_counts(conn: sqlite3.Connection) -> None:
    section("2. TABLE ROW COUNTS")

    tables = (
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
    )

    for table in tables:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()[0]

        print(f"{table:25s} {count}")


def validate_latest_tuner_samples(conn: sqlite3.Connection) -> None:
    section("3. LATEST TUNER SAMPLE FOR ALL 16 INPUTS")

    rows = conn.execute(
        """
        SELECT
            m.module_number,
            m.remote_ip,
            t.input_id,
            t.display_number,
            t.hwid,
            s.sampled_at,
            s.lock_state,
            s.rf_level_dbm,
            s.snr_db,
            s.ber_text,
            s.ber_value,
            s.frequency_raw,
            s.frequency_offset_raw,
            s.symbol_rate,
            s.modulation,
            s.fec,
            s.isi
        FROM tuners AS t
        JOIN modules AS m
            ON m.id = t.module_id
        JOIN tuner_samples AS s
            ON s.tuner_id = t.id
        WHERE s.id = (
            SELECT s2.id
            FROM tuner_samples AS s2
            WHERE s2.tuner_id = t.id
            ORDER BY s2.sampled_at DESC, s2.id DESC
            LIMIT 1
        )
        ORDER BY
            m.module_number,
            t.input_id
        """
    ).fetchall()

    print(f"Latest tuner rows: {len(rows)}")
    print()

    print_rows(rows)


def validate_latest_ts_samples(conn: sqlite3.Connection) -> None:
    section("4. LATEST TRANSPORT-STREAM SAMPLE FOR ALL INPUTS")

    rows = conn.execute(
        """
        SELECT
            m.module_number,
            t.input_id,
            s.sampled_at,
            s.current_bitrate_bps,
            s.min_bitrate_bps,
            s.max_bitrate_bps,
            s.tei_total,
            s.tei_delta,
            s.sync_error_total,
            s.sync_error_delta,
            s.input_cc_total,
            s.input_cc_delta
        FROM tuners AS t
        JOIN modules AS m
            ON m.id = t.module_id
        JOIN ts_samples AS s
            ON s.tuner_id = t.id
        WHERE s.id = (
            SELECT s2.id
            FROM ts_samples AS s2
            WHERE s2.tuner_id = t.id
            ORDER BY s2.sampled_at DESC, s2.id DESC
            LIMIT 1
        )
        ORDER BY
            m.module_number,
            t.input_id
        """
    ).fetchall()

    print(f"Latest TS rows: {len(rows)}")
    print()

    print_rows(rows)


def validate_latest_pcr_inputs(conn: sqlite3.Connection) -> None:
    section("5. LATEST PCR INPUT MONITOR SAMPLE")

    rows = conn.execute(
        """
        SELECT
            m.module_number,
            t.input_id,
            p.sampled_at,
            p.monitor_enabled,
            p.periodic_rate,
            p.buffer_jitter,
            p.buffer_size,
            p.buffer_level,
            p.fill_wait,
            p.freerunning,
            p.jitter_min,
            p.jitter_max,
            p.pcr_diff_max,

            p.ref_discontinuities_total,
            p.ref_discontinuities_delta,

            p.pcr_accuracy_errors_total,
            p.pcr_accuracy_errors_delta,

            p.pcr_repetition_errors_total,
            p.pcr_repetition_errors_delta,

            p.pcr_discontinuity_errors_total,
            p.pcr_discontinuity_errors_delta,

            p.to_wait_state_total,
            p.to_wait_state_delta,

            p.playout_fifo_reset_total,
            p.playout_fifo_reset_delta,

            p.unref_discontinuity_total,
            p.unref_discontinuity_delta,

            p.sample_ignored_total,
            p.sample_ignored_delta,

            p.into_freerunning_total,
            p.into_freerunning_delta,

            p.pcr_pid_changed_total,
            p.pcr_pid_changed_delta

        FROM tuners AS t
        JOIN modules AS m
            ON m.id = t.module_id
        JOIN pcr_input_samples AS p
            ON p.tuner_id = t.id

        WHERE p.id = (
            SELECT p2.id
            FROM pcr_input_samples AS p2
            WHERE p2.tuner_id = t.id
            ORDER BY p2.sampled_at DESC, p2.id DESC
            LIMIT 1
        )

        ORDER BY
            m.module_number,
            t.input_id
        """
    ).fetchall()

    print(f"Latest PCR input rows: {len(rows)}")
    print()

    print_rows(rows)


def validate_active_pcr_pids(conn: sqlite3.Connection) -> None:
    section("6. LATEST PCR PID RECORDS")

    rows = conn.execute(
        """
        SELECT
            m.module_number,
            t.input_id,
            p.pid,
            s.sampled_at,
            s.active,
            s.pcr_bitrate,
            s.stc_bitrate,
            s.stc_factor,
            s.jitter_min,
            s.jitter_max,
            s.pcr_diff_max,
            s.ref_discontinuities_total,
            s.ref_discontinuities_delta,
            s.pcr_accuracy_errors_total,
            s.pcr_accuracy_errors_delta,
            s.pcr_repetition_errors_total,
            s.pcr_repetition_errors_delta,
            s.pcr_discontinuity_errors_total,
            s.pcr_discontinuity_errors_delta
        FROM pcr_pid_samples AS s
        JOIN pids AS p
            ON p.id = s.pid_id
        JOIN tuners AS t
            ON t.id = p.tuner_id
        JOIN modules AS m
            ON m.id = t.module_id
        WHERE s.id = (
            SELECT s2.id
            FROM pcr_pid_samples AS s2
            WHERE s2.pid_id = p.id
            ORDER BY s2.sampled_at DESC, s2.id DESC
            LIMIT 1
        )
        ORDER BY
            m.module_number,
            t.input_id,
            p.pid
        """
    ).fetchall()

    active = [row for row in rows if row["active"] == 1]
    inactive = [row for row in rows if row["active"] == 0]

    print(f"Latest PCR PID records : {len(rows)}")
    print(f"Active                 : {len(active)}")
    print(f"Inactive/stale         : {len(inactive)}")

    print()
    print("ACTIVE PCR PIDS")
    print(SUB_SEPARATOR)
    print_rows(active)

    print()
    print("INACTIVE / STALE PCR PIDS")
    print(SUB_SEPARATOR)
    print_rows(inactive)


def validate_services(conn: sqlite3.Connection) -> None:
    section("7. CURRENT SERVICE / CHANNEL INVENTORY")

    rows = conn.execute(
        """
        SELECT
            m.module_number,
            t.input_id,
            ts.tsid,
            ts.onid,
            ts.network_id,
            s.service_id,
            s.service_name,
            s.provider_name,
            s.pmt_pid,
            s.pcr_pid,
            s.first_seen_at,
            s.last_seen_at
        FROM services AS s
        JOIN tuners AS t
            ON t.id = s.tuner_id
        JOIN modules AS m
            ON m.id = t.module_id
        LEFT JOIN transport_streams AS ts
            ON ts.tuner_id = t.id
        ORDER BY
            m.module_number,
            t.input_id,
            s.service_id
        """
    ).fetchall()

    print(f"Service records: {len(rows)}")
    print()

    print_rows(rows)


def validate_service_streams(conn: sqlite3.Connection) -> None:
    section("8. SERVICE ELEMENTARY STREAMS")

    rows = conn.execute(
        """
        SELECT
            m.module_number,
            t.input_id,
            s.service_id,
            s.service_name,
            ss.pid,
            ss.stream_type,
            ss.stream_type_name,
            ss.language,
            ss.first_seen_at,
            ss.last_seen_at
        FROM service_streams AS ss
        JOIN services AS s
            ON s.id = ss.service_id
        JOIN tuners AS t
            ON t.id = s.tuner_id
        JOIN modules AS m
            ON m.id = t.module_id
        ORDER BY
            m.module_number,
            t.input_id,
            s.service_id,
            ss.pid
        """
    ).fetchall()

    print(f"Service stream records: {len(rows)}")
    print()

    print_rows(rows)


def validate_counter_state(conn: sqlite3.Connection) -> None:
    section("9. COUNTER STATE")

    rows = conn.execute(
        """
        SELECT
            counter_key,
            counter_value,
            sampled_at
        FROM counter_state
        ORDER BY counter_key
        """
    ).fetchall()

    print(f"Counter-state records: {len(rows)}")
    print()

    print_rows(rows)


def validate_monitoring_events(conn: sqlite3.Connection) -> None:
    section("10. MONITORING EVENTS")

    rows = conn.execute(
        """
        SELECT
            e.id,
            e.occurred_at,
            m.module_number,
            t.input_id,
            p.pid,
            e.category,
            e.severity,
            e.metric,
            e.value,
            e.delta,
            e.message
        FROM monitoring_events AS e
        LEFT JOIN modules AS m
            ON m.id = e.module_id
        LEFT JOIN tuners AS t
            ON t.id = e.tuner_id
        LEFT JOIN pids AS p
            ON p.id = e.pid_id
        ORDER BY
            e.id
        """
    ).fetchall()

    print(f"Monitoring events: {len(rows)}")
    print()

    print_rows(rows)


def validate_latest_pid_errors(conn: sqlite3.Connection) -> None:
    section("11. LATEST PID COUNTER DELTAS > 0")

    rows = conn.execute(
        """
        SELECT
            m.module_number,
            t.input_id,
            p.pid,
            s.sampled_at,
            s.bitrate_bps,
            s.packet_count,
            s.packet_delta,
            s.cc_error_total,
            s.cc_error_delta,
            s.pcr_present,
            s.scrambled
        FROM pid_samples AS s
        JOIN pids AS p
            ON p.id = s.pid_id
        JOIN tuners AS t
            ON t.id = p.tuner_id
        JOIN modules AS m
            ON m.id = t.module_id
        WHERE s.id = (
            SELECT s2.id
            FROM pid_samples AS s2
            WHERE s2.pid_id = p.id
            ORDER BY s2.sampled_at DESC, s2.id DESC
            LIMIT 1
        )
        AND (
            COALESCE(s.cc_error_delta, 0) > 0
            OR COALESCE(s.packet_delta, 0) < 0
        )
        ORDER BY
            m.module_number,
            t.input_id,
            p.pid
        """
    ).fetchall()

    print(f"Latest PID records with suspicious deltas: {len(rows)}")
    print()

    print_rows(rows)


def validate_integrity(conn: sqlite3.Connection) -> None:
    section("12. SQLITE INTEGRITY / FOREIGN KEYS")

    integrity = conn.execute(
        "PRAGMA integrity_check"
    ).fetchall()

    print("PRAGMA integrity_check:")
    print_rows(integrity)

    print()

    fk_rows = conn.execute(
        "PRAGMA foreign_key_check"
    ).fetchall()

    print(f"Foreign-key violations: {len(fk_rows)}")

    if fk_rows:
        print_rows(fk_rows)
    else:
        print("(none)")


def main() -> None:
    print(SEPARATOR)
    print("WISI GT34 DATABASE VALIDATION")
    print(f"Database: {Path(DATABASE_PATH)}")
    print(SEPARATOR)

    if not Path(DATABASE_PATH).exists():
        raise FileNotFoundError(
            f"Database does not exist: {DATABASE_PATH}"
        )

    with open_db() as conn:
        validate_poll_runs(conn)
        validate_table_counts(conn)
        validate_latest_tuner_samples(conn)
        validate_latest_ts_samples(conn)
        validate_latest_pcr_inputs(conn)
        validate_active_pcr_pids(conn)
        validate_services(conn)
        validate_service_streams(conn)
        validate_counter_state(conn)
        validate_monitoring_events(conn)
        validate_latest_pid_errors(conn)
        validate_integrity(conn)

    print()
    print(SEPARATOR)
    print("DATABASE VALIDATION COMPLETE")
    print(SEPARATOR)


if __name__ == "__main__":
    main()