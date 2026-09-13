from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import DATABASE_PATH
from alarm_policy import (
    BER_MAX,
    EXPECTED_ACTIVE_INPUTS,
    RF_LEVEL_MIN_DBM,
    SNR_MIN_DB,
)


# ---------------------------------------------------------------------------
# Alarm policy
# ---------------------------------------------------------------------------
#
# Objective digital-state/counter alarms are enabled immediately.
#
# RF/SNR/BER thresholds are deliberately disabled by default because a
# defensible limit depends on modulation/FEC/link design and an approved
# operational policy. They can be enabled later without changing the engine.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlarmThresholds:
    rf_level_min_dbm: float | None = None
    snr_min_db: float | None = None
    ber_max: float | None = None


DEFAULT_THRESHOLDS = AlarmThresholds(
    rf_level_min_dbm=RF_LEVEL_MIN_DBM,
    snr_min_db=SNR_MIN_DB,
    ber_max=BER_MAX,
)


@dataclass(frozen=True)
class Alarm:
    severity: str
    category: str
    metric: str
    module_number: int
    input_id: int
    tuner_id: int
    sampled_at: str
    message: str
    value: float | int | str | None = None
    delta: float | int | None = None
    pid: int | None = None


REQUIRED_COLUMNS: dict[str, set[str]] = {
    "modules": {
        "id",
        "module_number",
        "module_name",
        "remote_ip",
    },
    "tuners": {
        "id",
        "module_id",
        "input_id",
    },
    "tuner_samples": {
        "id",
        "tuner_id",
        "sampled_at",
        "lock_state",
        "rf_level_dbm",
        "snr_db",
        "ber_text",
        "ber_value",
    },
    "ts_samples": {
        "id",
        "tuner_id",
        "sampled_at",
        "current_bitrate_bps",
        "tei_delta",
        "sync_error_delta",
        "input_cc_delta",
    },
    "pids": {
        "id",
        "tuner_id",
        "pid",
    },
    "pid_samples": {
        "id",
        "pid_id",
        "sampled_at",
        "bitrate_bps",
        "cc_error_delta",
        "pcr_present",
        "scrambled",
    },
    "pcr_input_samples": {
        "id",
        "tuner_id",
        "sampled_at",
        "monitor_enabled",
        "periodic_rate",
        "freerunning",
        "ref_discontinuities_delta",
        "pcr_accuracy_errors_delta",
        "pcr_repetition_errors_delta",
        "pcr_discontinuity_errors_delta",
        "to_wait_state_delta",
        "playout_fifo_reset_delta",
        "unref_discontinuity_delta",
        "sample_ignored_delta",
        "into_freerunning_delta",
        "pcr_pid_changed_delta",
    },
    "pcr_pid_samples": {
        "id",
        "pid_id",
        "sampled_at",
        "active",
        "pcr_bitrate",
        "stc_bitrate",
        "ref_discontinuities_delta",
        "pcr_accuracy_errors_delta",
        "pcr_repetition_errors_delta",
        "pcr_discontinuity_errors_delta",
    },
}


SEVERITY_RANK = {
    "critical": 0,
    "warning": 1,
    "info": 2,
}


def open_db(
    path: Path | str = DATABASE_PATH,
) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def table_columns(
    conn: sqlite3.Connection,
    table: str,
) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }


def verify_schema(
    conn: sqlite3.Connection,
) -> None:
    errors: list[str] = []

    tables = {
        str(row["name"])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        ).fetchall()
    }

    for table, required in REQUIRED_COLUMNS.items():
        if table not in tables:
            errors.append(
                f"missing table: {table}"
            )
            continue

        actual = table_columns(
            conn,
            table,
        )

        missing = sorted(
            required - actual
        )

        if missing:
            errors.append(
                f"{table}: missing "
                + ", ".join(missing)
            )

    if errors:
        raise RuntimeError(
            "Alarm-engine schema validation failed: "
            + "; ".join(errors)
        )


def latest_tuner_rows(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """
    One latest tuner sample per physical WISI input, plus the TS sample from
    the same collection timestamp when available.
    """
    return conn.execute(
        """
        WITH latest_tuner AS (
            SELECT
                tuner_id,
                MAX(id) AS sample_id
            FROM tuner_samples
            GROUP BY tuner_id
        )
        SELECT
            m.module_number,
            m.module_name,
            m.remote_ip,
            t.id AS tuner_id,
            t.input_id,
            tsamp.sampled_at,
            tsamp.lock_state,
            tsamp.rf_level_dbm,
            tsamp.snr_db,
            tsamp.ber_text,
            tsamp.ber_value,
            x.current_bitrate_bps,
            x.tei_delta,
            x.sync_error_delta,
            x.input_cc_delta
        FROM latest_tuner lt
        JOIN tuner_samples tsamp
          ON tsamp.id = lt.sample_id
        JOIN tuners t
          ON t.id = tsamp.tuner_id
        JOIN modules m
          ON m.id = t.module_id
        LEFT JOIN ts_samples x
          ON x.tuner_id = t.id
         AND x.sampled_at = tsamp.sampled_at
        ORDER BY
            m.module_number,
            t.input_id
        """
    ).fetchall()


def latest_pcr_input_row(
    conn: sqlite3.Connection,
    *,
    tuner_id: int,
    sampled_at: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM pcr_input_samples
        WHERE tuner_id = ?
          AND sampled_at = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            tuner_id,
            sampled_at,
        ),
    ).fetchone()


def current_pid_rows(
    conn: sqlite3.Connection,
    *,
    tuner_id: int,
    sampled_at: str,
) -> list[sqlite3.Row]:
    """
    Only PID rows from the current tuner snapshot are considered live.
    Older PID rows are inventory/history and must not create current alarms.
    """
    return conn.execute(
        """
        SELECT
            p.id AS pid_id,
            p.pid,
            ps.sampled_at,
            ps.bitrate_bps,
            ps.cc_error_delta,
            ps.pcr_present,
            ps.scrambled
        FROM pids p
        JOIN pid_samples ps
          ON ps.pid_id = p.id
        WHERE p.tuner_id = ?
          AND ps.sampled_at = ?
        ORDER BY p.pid
        """,
        (
            tuner_id,
            sampled_at,
        ),
    ).fetchall()


def current_active_pcr_pid_rows(
    conn: sqlite3.Connection,
    *,
    tuner_id: int,
    sampled_at: str,
) -> list[sqlite3.Row]:
    """
    Alarm only on PCR PID rows explicitly marked active by collector.py.
    Stale/inactive PCR evidence is retained in the database but excluded.
    """
    return conn.execute(
        """
        SELECT
            p.pid,
            ps.sampled_at,
            ps.active,
            ps.pcr_bitrate,
            ps.stc_bitrate,
            ps.ref_discontinuities_delta,
            ps.pcr_accuracy_errors_delta,
            ps.pcr_repetition_errors_delta,
            ps.pcr_discontinuity_errors_delta
        FROM pids p
        JOIN pcr_pid_samples ps
          ON ps.pid_id = p.id
        WHERE p.tuner_id = ?
          AND ps.sampled_at = ?
          AND ps.active = 1
        ORDER BY p.pid
        """,
        (
            tuner_id,
            sampled_at,
        ),
    ).fetchall()


def positive(
    value: Any,
) -> bool:
    return (
        value is not None
        and float(value) > 0
    )


def add_delta_alarm(
    alarms: list[Alarm],
    *,
    row: sqlite3.Row,
    severity: str,
    category: str,
    metric: str,
    delta: Any,
    message: str,
    pid: int | None = None,
) -> None:
    if not positive(delta):
        return

    alarms.append(
        Alarm(
            severity=severity,
            category=category,
            metric=metric,
            module_number=int(
                row["module_number"]
            ),
            input_id=int(
                row["input_id"]
            ),
            tuner_id=int(
                row["tuner_id"]
            ),
            sampled_at=str(
                row["sampled_at"]
            ),
            message=message,
            value=None,
            delta=float(delta),
            pid=pid,
        )
    )


def evaluate_tuner(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    thresholds: AlarmThresholds,
) -> list[Alarm]:
    alarms: list[Alarm] = []

    module_number = int(
        row["module_number"]
    )
    input_id = int(
        row["input_id"]
    )
    tuner_id = int(
        row["tuner_id"]
    )
    sampled_at = str(
        row["sampled_at"]
    )

    lock_state = row["lock_state"]
    bitrate = row["current_bitrate_bps"]

    # ------------------------------------------------------------------
    # Objective state alarms
    # ------------------------------------------------------------------

    expected_active = (
        module_number,
        input_id,
    ) in EXPECTED_ACTIVE_INPUTS

    if expected_active and lock_state == 0:
        alarms.append(
            Alarm(
                severity="critical",
                category="signal",
                metric="demod_lock",
                module_number=module_number,
                input_id=input_id,
                tuner_id=tuner_id,
                sampled_at=sampled_at,
                value=0,
                message=(
                    "Expected-active demodulator is unlocked."
                ),
            )
        )

    elif (
        expected_active
        and lock_state == 1
        and bitrate is not None
        and float(bitrate) <= 0
    ):
        alarms.append(
            Alarm(
                severity="critical",
                category="transport_stream",
                metric="ts_bitrate",
                module_number=module_number,
                input_id=input_id,
                tuner_id=tuner_id,
                sampled_at=sampled_at,
                value=bitrate,
                message=(
                    "Demodulator is locked but "
                    "transport-stream bitrate is zero."
                ),
            )
        )

    # ------------------------------------------------------------------
    # Optional RF/SNR/BER policy thresholds
    # ------------------------------------------------------------------

    rf = row["rf_level_dbm"]
    if (
        thresholds.rf_level_min_dbm
        is not None
        and rf is not None
        and float(rf)
        < thresholds.rf_level_min_dbm
    ):
        alarms.append(
            Alarm(
                severity="warning",
                category="signal",
                metric="rf_level_dbm",
                module_number=module_number,
                input_id=input_id,
                tuner_id=tuner_id,
                sampled_at=sampled_at,
                value=float(rf),
                message=(
                    f"RF level {float(rf):.3f} dBm "
                    f"is below configured minimum "
                    f"{thresholds.rf_level_min_dbm:.3f} dBm."
                ),
            )
        )

    snr = row["snr_db"]
    if (
        thresholds.snr_min_db
        is not None
        and snr is not None
        and float(snr)
        < thresholds.snr_min_db
    ):
        alarms.append(
            Alarm(
                severity="warning",
                category="signal",
                metric="snr_db",
                module_number=module_number,
                input_id=input_id,
                tuner_id=tuner_id,
                sampled_at=sampled_at,
                value=float(snr),
                message=(
                    f"SNR {float(snr):.3f} dB "
                    f"is below configured minimum "
                    f"{thresholds.snr_min_db:.3f} dB."
                ),
            )
        )

    ber = row["ber_value"]
    if (
        thresholds.ber_max
        is not None
        and ber is not None
        and float(ber)
        > thresholds.ber_max
    ):
        alarms.append(
            Alarm(
                severity="warning",
                category="signal",
                metric="ber",
                module_number=module_number,
                input_id=input_id,
                tuner_id=tuner_id,
                sampled_at=sampled_at,
                value=float(ber),
                message=(
                    f"BER {float(ber):.6g} "
                    f"exceeds configured maximum "
                    f"{thresholds.ber_max:.6g}."
                ),
            )
        )

    # Do not raise TS/PCR counter alarms on an unlocked input.
    if lock_state != 1:
        return alarms

    # ------------------------------------------------------------------
    # TR 101 290 / TS input counter deltas
    # ------------------------------------------------------------------

    add_delta_alarm(
        alarms,
        row=row,
        severity="warning",
        category="transport_stream",
        metric="tei_delta",
        delta=row["tei_delta"],
        message=(
            "New Transport Error Indicator "
            "events detected in this interval."
        ),
    )

    add_delta_alarm(
        alarms,
        row=row,
        severity="warning",
        category="transport_stream",
        metric="sync_error_delta",
        delta=row["sync_error_delta"],
        message=(
            "New transport-stream synchronization "
            "errors detected in this interval."
        ),
    )

    add_delta_alarm(
        alarms,
        row=row,
        severity="warning",
        category="transport_stream",
        metric="input_cc_delta",
        delta=row["input_cc_delta"],
        message=(
            "New input continuity-counter errors "
            "detected in this interval."
        ),
    )

    # ------------------------------------------------------------------
    # PCR input-regulator deltas
    # ------------------------------------------------------------------
    #
    # WISI exposes several PCR counters both at input aggregate level and
    # per active PCR PID. Reporting both naively double-counts one incident.
    #
    # Policy:
    #   1. Emit per-PID PCR alarms when active PCR PID rows are available.
    #   2. For the same PCR metric, emit an input-level alarm only for a
    #      positive residual not accounted for by active PID deltas.
    #   3. Input-regulator-only counters (FIFO reset, freerunning, etc.)
    #      remain input-level because they have no PID equivalent.
    # ------------------------------------------------------------------

    pcr_input = latest_pcr_input_row(
        conn,
        tuner_id=tuner_id,
        sampled_at=sampled_at,
    )

    active_pcr_rows = current_active_pcr_pid_rows(
        conn,
        tuner_id=tuner_id,
        sampled_at=sampled_at,
    )

    pcr_metric_messages = {
        "ref_discontinuities_delta":
            "New PCR reference discontinuity events detected.",
        "pcr_accuracy_errors_delta":
            "New PCR accuracy-error events detected.",
        "pcr_repetition_errors_delta":
            "New PCR repetition-error events detected.",
        "pcr_discontinuity_errors_delta":
            "New PCR discontinuity-indicator error events detected.",
    }

    if pcr_input is not None:
        for metric, message in pcr_metric_messages.items():
            input_delta = pcr_input[metric]

            if not positive(input_delta):
                continue

            pid_total = sum(
                float(pid_row[metric] or 0)
                for pid_row in active_pcr_rows
                if pid_row[metric] is not None
            )

            residual = float(input_delta) - pid_total

            # Allow tiny floating-point noise only; these counters are
            # effectively integral event counts.
            if residual > 0.000001:
                alarms.append(
                    Alarm(
                        severity="warning",
                        category="pcr",
                        metric=metric,
                        module_number=module_number,
                        input_id=input_id,
                        tuner_id=tuner_id,
                        sampled_at=sampled_at,
                        delta=residual,
                        message=(
                            message
                            + " This input-level residual is not "
                              "accounted for by active PCR PID deltas."
                        ),
                    )
                )

        input_regulator_metrics: tuple[
            tuple[str, str, str],
            ...
        ] = (
            (
                "playout_fifo_reset_delta",
                "warning",
                "New playout FIFO reset events detected.",
            ),
            (
                "into_freerunning_delta",
                "warning",
                "PCR input regulator entered freerunning.",
            ),
            (
                "to_wait_state_delta",
                "info",
                "PCR input regulator entered wait state.",
            ),
            (
                "unref_discontinuity_delta",
                "info",
                "New unreferenced discontinuity events detected.",
            ),
            (
                "sample_ignored_delta",
                "info",
                "PCR input regulator ignored samples.",
            ),
            (
                "pcr_pid_changed_delta",
                "info",
                "PCR PID change event detected.",
            ),
        )

        for metric, severity, message in input_regulator_metrics:
            delta = pcr_input[metric]

            if positive(delta):
                alarms.append(
                    Alarm(
                        severity=severity,
                        category="pcr",
                        metric=metric,
                        module_number=module_number,
                        input_id=input_id,
                        tuner_id=tuner_id,
                        sampled_at=sampled_at,
                        delta=float(delta),
                        message=message,
                    )
                )

    # ------------------------------------------------------------------
    # PID continuity-counter deltas
    # ------------------------------------------------------------------

    for pid_row in current_pid_rows(
        conn,
        tuner_id=tuner_id,
        sampled_at=sampled_at,
    ):
        delta = pid_row[
            "cc_error_delta"
        ]

        if not positive(delta):
            continue

        pid = int(
            pid_row["pid"]
        )

        alarms.append(
            Alarm(
                severity="warning",
                category="pid",
                metric="pid_cc_error_delta",
                module_number=module_number,
                input_id=input_id,
                tuner_id=tuner_id,
                sampled_at=sampled_at,
                delta=float(delta),
                pid=pid,
                message=(
                    f"New continuity-counter errors "
                    f"detected on PID {pid}."
                ),
            )
        )

    # ------------------------------------------------------------------
    # Active PCR-PID deltas only
    # ------------------------------------------------------------------

    for pcr_pid_row in active_pcr_rows:
        pid = int(
            pcr_pid_row["pid"]
        )

        pid_pcr_metrics = (
            (
                "ref_discontinuities_delta",
                "New PCR reference discontinuity "
                f"events detected on PID {pid}.",
            ),
            (
                "pcr_accuracy_errors_delta",
                "New PCR accuracy errors "
                f"detected on PID {pid}.",
            ),
            (
                "pcr_repetition_errors_delta",
                "New PCR repetition errors "
                f"detected on PID {pid}.",
            ),
            (
                "pcr_discontinuity_errors_delta",
                "New PCR discontinuity-indicator "
                f"errors detected on PID {pid}.",
            ),
        )

        for metric, message in pid_pcr_metrics:
            delta = pcr_pid_row[
                metric
            ]

            if not positive(delta):
                continue

            alarms.append(
                Alarm(
                    severity="warning",
                    category="pcr_pid",
                    metric=metric,
                    module_number=module_number,
                    input_id=input_id,
                    tuner_id=tuner_id,
                    sampled_at=sampled_at,
                    delta=float(delta),
                    pid=pid,
                    message=message,
                )
            )

    return alarms


def evaluate_latest_snapshot(
    *,
    database_path: Path | str = DATABASE_PATH,
    thresholds: AlarmThresholds = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    with closing(
        open_db(database_path)
    ) as conn:
        verify_schema(conn)

        tuner_rows = latest_tuner_rows(
            conn
        )

        alarms: list[Alarm] = []

        states: list[
            dict[str, Any]
        ] = []

        for row in tuner_rows:
            tuner_alarms = evaluate_tuner(
                conn,
                row,
                thresholds=thresholds,
            )
            alarms.extend(
                tuner_alarms
            )

            states.append(
                {
                    "module_number": int(
                        row["module_number"]
                    ),
                    "input_id": int(
                        row["input_id"]
                    ),
                    "tuner_id": int(
                        row["tuner_id"]
                    ),
                    "sampled_at": str(
                        row["sampled_at"]
                    ),
                    "expected_active": (
                        int(row["module_number"]),
                        int(row["input_id"]),
                    ) in EXPECTED_ACTIVE_INPUTS,
                    "lock_state": (
                        None
                        if row["lock_state"] is None
                        else int(row["lock_state"])
                    ),
                    "rf_level_dbm": row[
                        "rf_level_dbm"
                    ],
                    "snr_db": row[
                        "snr_db"
                    ],
                    "ber_text": row[
                        "ber_text"
                    ],
                    "ber_value": row[
                        "ber_value"
                    ],
                    "current_bitrate_bps": row[
                        "current_bitrate_bps"
                    ],
                    "alarm_count": len(
                        tuner_alarms
                    ),
                }
            )

        alarms.sort(
            key=lambda alarm: (
                SEVERITY_RANK.get(
                    alarm.severity,
                    99,
                ),
                alarm.module_number,
                alarm.input_id,
                alarm.pid
                if alarm.pid is not None
                else -1,
                alarm.metric,
            )
        )

        return {
            "database_path": str(
                database_path
            ),
            "thresholds": thresholds,
            "states": states,
            "alarms": alarms,
            "tuner_count": len(
                states
            ),
            "alarm_count": len(
                alarms
            ),
        }


def format_bitrate(
    value: Any,
) -> str:
    if value is None:
        return "N/A"

    return (
        f"{float(value) / 1_000_000:.3f} Mb/s"
    )


def format_value(
    value: Any,
    *,
    decimals: int = 3,
) -> str:
    if value is None:
        return "N/A"

    if isinstance(
        value,
        (int, float),
    ):
        return f"{float(value):.{decimals}f}"

    return str(value)


def print_report(
    result: dict[str, Any],
) -> None:
    print("=" * 104)
    print(
        "WISI GT34 DERIVED MONITORING / "
        "ALARM ENGINE"
    )
    print("=" * 104)
    print(
        f"Database       : "
        f"{result['database_path']}"
    )
    print(
        f"Tuners checked : "
        f"{result['tuner_count']}"
    )
    print(
        f"Alarms found   : "
        f"{result['alarm_count']}"
    )

    thresholds: AlarmThresholds = (
        result["thresholds"]
    )

    print(
        "RF/SNR/BER     : "
        + (
            "threshold alarms disabled"
            if (
                thresholds.rf_level_min_dbm
                is None
                and thresholds.snr_min_db
                is None
                and thresholds.ber_max
                is None
            )
            else "configured thresholds active"
        )
    )

    print("-" * 104)
    print("LATEST TUNER STATES")
    print("-" * 104)

    for state in result["states"]:
        lock = state[
            "lock_state"
        ]

        lock_text = (
            "LOCKED"
            if lock == 1
            else (
                "UNLOCKED"
                if lock == 0
                else "UNKNOWN"
            )
        )

        policy_text = (
            "MON"
            if state["expected_active"]
            else "IGN"
        )

        print(
            f"M{state['module_number']} "
            f"Input {state['input_id']} | "
            f"{policy_text} | "
            f"{lock_text:8s} | "
            f"RF={format_value(state['rf_level_dbm'], decimals=1):>7s} dBm | "
            f"SNR={format_value(state['snr_db'], decimals=1):>5s} dB | "
            f"BER={str(state['ber_text'] or 'N/A'):>10s} | "
            f"TS={format_bitrate(state['current_bitrate_bps']):>12s} | "
            f"alarms={state['alarm_count']}"
        )

    print("-" * 104)
    print("CURRENT ALARMS")
    print("-" * 104)

    if not result["alarms"]:
        print(
            "No current alarms detected from enabled rules."
        )
    else:
        for alarm in result[
            "alarms"
        ]:
            pid_text = (
                ""
                if alarm.pid is None
                else f" PID={alarm.pid}"
            )

            delta_text = (
                ""
                if alarm.delta is None
                else f" delta={alarm.delta:g}"
            )

            value_text = (
                ""
                if alarm.value is None
                else f" value={alarm.value}"
            )

            print(
                f"{alarm.severity.upper():8s} | "
                f"M{alarm.module_number} "
                f"Input {alarm.input_id}"
                f"{pid_text} | "
                f"{alarm.category}/"
                f"{alarm.metric} | "
                f"{alarm.message}"
                f"{value_text}"
                f"{delta_text}"
            )

    print("=" * 104)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the latest WISI GT34 database "
            "snapshot and derive current monitoring alarms."
        )
    )

    parser.add_argument(
        "--rf-min",
        type=float,
        default=None,
        help=(
            "Optional minimum RF level in dBm. "
            "Disabled when omitted."
        ),
    )

    parser.add_argument(
        "--snr-min",
        type=float,
        default=None,
        help=(
            "Optional minimum SNR in dB. "
            "Disabled when omitted."
        ),
    )

    parser.add_argument(
        "--ber-max",
        type=float,
        default=None,
        help=(
            "Optional maximum BER. "
            "Disabled when omitted."
        ),
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    thresholds = AlarmThresholds(
        rf_level_min_dbm=args.rf_min,
        snr_min_db=args.snr_min,
        ber_max=args.ber_max,
    )

    result = evaluate_latest_snapshot(
        thresholds=thresholds,
    )

    print_report(
        result
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
