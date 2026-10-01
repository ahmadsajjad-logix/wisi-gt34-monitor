from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alarm_engine import Alarm, AlarmThresholds, DEFAULT_THRESHOLDS, evaluate_latest_snapshot
from config import DATABASE_PATH


STATEFUL_METRICS = {
    "demod_lock",
    "ts_bitrate",
    "rf_level_dbm",
    "snr_db",
    "ber",
}


@dataclass(frozen=True)
class PersistenceResult:
    evaluated_alarms: int
    event_alarms_inserted: int
    state_alarms_opened: int
    state_alarms_refreshed: int
    state_alarms_recovered: int
    duplicate_events_suppressed: int


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


def ensure_alarm_state_schema(
    conn: sqlite3.Connection,
) -> None:
    """
    Add only the current-state table required for stateful alarm suppression.

    Historical alarm/event records continue to use the existing
    monitoring_events table and therefore inherit its existing 30-day
    retention policy.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alarm_state (
            alarm_key TEXT PRIMARY KEY,
            module_id INTEGER NOT NULL,
            tuner_id INTEGER NOT NULL,
            pid_id INTEGER,
            category TEXT NOT NULL,
            metric TEXT NOT NULL,
            severity TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0
                CHECK(active IN (0, 1)),
            first_active_at TEXT,
            last_observed_at TEXT NOT NULL,
            last_changed_at TEXT NOT NULL,
            value REAL,
            delta REAL,
            message TEXT NOT NULL,
            FOREIGN KEY(module_id) REFERENCES modules(id),
            FOREIGN KEY(tuner_id) REFERENCES tuners(id),
            FOREIGN KEY(pid_id) REFERENCES pids(id)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_alarm_state_active
        ON alarm_state(active, module_id, tuner_id)
        """
    )


def resolve_database_ids(
    conn: sqlite3.Connection,
    alarm: Alarm,
) -> tuple[int, int | None]:
    module_row = conn.execute(
        """
        SELECT module_id
        FROM tuners
        WHERE id = ?
        """,
        (alarm.tuner_id,),
    ).fetchone()

    if module_row is None:
        raise RuntimeError(
            f"Unknown tuner_id={alarm.tuner_id}"
        )

    module_id = int(
        module_row["module_id"]
    )

    pid_id: int | None = None

    if alarm.pid is not None:
        pid_row = conn.execute(
            """
            SELECT id
            FROM pids
            WHERE tuner_id = ?
              AND pid = ?
            """,
            (
                alarm.tuner_id,
                alarm.pid,
            ),
        ).fetchone()

        if pid_row is None:
            raise RuntimeError(
                "Could not resolve PID database identity for "
                f"tuner_id={alarm.tuner_id}, pid={alarm.pid}"
            )

        pid_id = int(
            pid_row["id"]
        )

    return module_id, pid_id


def alarm_key(
    alarm: Alarm,
) -> str:
    pid_part = (
        "-"
        if alarm.pid is None
        else str(alarm.pid)
    )

    return (
        f"tuner:{alarm.tuner_id}"
        f"|pid:{pid_part}"
        f"|category:{alarm.category}"
        f"|metric:{alarm.metric}"
    )


def is_stateful_alarm(
    alarm: Alarm,
) -> bool:
    return alarm.metric in STATEFUL_METRICS


def monitoring_event_exists(
    conn: sqlite3.Connection,
    *,
    alarm: Alarm,
    module_id: int,
    pid_id: int | None,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM monitoring_events
        WHERE occurred_at = ?
          AND module_id = ?
          AND tuner_id = ?
          AND pid_id IS ?
          AND category = ?
          AND severity = ?
          AND metric = ?
        LIMIT 1
        """,
        (
            alarm.sampled_at,
            module_id,
            alarm.tuner_id,
            pid_id,
            alarm.category,
            alarm.severity,
            alarm.metric,
        ),
    ).fetchone()

    return row is not None


def insert_monitoring_event(
    conn: sqlite3.Connection,
    *,
    occurred_at: str,
    module_id: int,
    tuner_id: int,
    pid_id: int | None,
    category: str,
    severity: str,
    metric: str,
    value: Any,
    delta: Any,
    message: str,
) -> None:
    conn.execute(
        """
        INSERT INTO monitoring_events(
            occurred_at,
            module_id,
            tuner_id,
            pid_id,
            category,
            severity,
            metric,
            value,
            delta,
            message,
            acknowledged
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            occurred_at,
            module_id,
            tuner_id,
            pid_id,
            category,
            severity,
            metric,
            value,
            delta,
            message,
        ),
    )


def persist_event_alarm(
    conn: sqlite3.Connection,
    alarm: Alarm,
) -> bool:
    module_id, pid_id = resolve_database_ids(
        conn,
        alarm,
    )

    if monitoring_event_exists(
        conn,
        alarm=alarm,
        module_id=module_id,
        pid_id=pid_id,
    ):
        return False

    insert_monitoring_event(
        conn,
        occurred_at=alarm.sampled_at,
        module_id=module_id,
        tuner_id=alarm.tuner_id,
        pid_id=pid_id,
        category=alarm.category,
        severity=alarm.severity,
        metric=alarm.metric,
        value=alarm.value,
        delta=alarm.delta,
        message=alarm.message,
    )

    return True


def persist_stateful_alarm(
    conn: sqlite3.Connection,
    alarm: Alarm,
) -> str:
    """
    Return:
        "opened"     first transition into active state
        "refreshed"  already active; state refreshed, no event inserted
    """
    module_id, pid_id = resolve_database_ids(
        conn,
        alarm,
    )

    key = alarm_key(
        alarm
    )

    existing = conn.execute(
        """
        SELECT *
        FROM alarm_state
        WHERE alarm_key = ?
        """,
        (key,),
    ).fetchone()

    if existing is None or int(existing["active"]) == 0:
        first_active_at = alarm.sampled_at

        if existing is not None:
            first_active_at = alarm.sampled_at

        conn.execute(
            """
            INSERT INTO alarm_state(
                alarm_key,
                module_id,
                tuner_id,
                pid_id,
                category,
                metric,
                severity,
                active,
                first_active_at,
                last_observed_at,
                last_changed_at,
                value,
                delta,
                message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(alarm_key) DO UPDATE SET
                module_id = excluded.module_id,
                tuner_id = excluded.tuner_id,
                pid_id = excluded.pid_id,
                category = excluded.category,
                metric = excluded.metric,
                severity = excluded.severity,
                active = 1,
                first_active_at = excluded.first_active_at,
                last_observed_at = excluded.last_observed_at,
                last_changed_at = excluded.last_changed_at,
                value = excluded.value,
                delta = excluded.delta,
                message = excluded.message
            """,
            (
                key,
                module_id,
                alarm.tuner_id,
                pid_id,
                alarm.category,
                alarm.metric,
                alarm.severity,
                first_active_at,
                alarm.sampled_at,
                alarm.sampled_at,
                alarm.value,
                alarm.delta,
                alarm.message,
            ),
        )

        insert_monitoring_event(
            conn,
            occurred_at=alarm.sampled_at,
            module_id=module_id,
            tuner_id=alarm.tuner_id,
            pid_id=pid_id,
            category=alarm.category,
            severity=alarm.severity,
            metric=alarm.metric,
            value=alarm.value,
            delta=alarm.delta,
            message=(
                "ALARM OPEN: "
                + alarm.message
            ),
        )

        return "opened"

    conn.execute(
        """
        UPDATE alarm_state
        SET
            severity = ?,
            last_observed_at = ?,
            value = ?,
            delta = ?,
            message = ?
        WHERE alarm_key = ?
        """,
        (
            alarm.severity,
            alarm.sampled_at,
            alarm.value,
            alarm.delta,
            alarm.message,
            key,
        ),
    )

    return "refreshed"


def recover_missing_stateful_alarms(
    conn: sqlite3.Connection,
    *,
    current_state_alarm_keys: set[str],
    current_tuner_sample_times: dict[int, str],
) -> int:
    """
    Close active state alarms only when the corresponding tuner has a current
    evaluated snapshot. This prevents a missing/stale tuner from being
    interpreted as a recovery.
    """
    active_rows = conn.execute(
        """
        SELECT *
        FROM alarm_state
        WHERE active = 1
        ORDER BY alarm_key
        """
    ).fetchall()

    recovered = 0

    for row in active_rows:
        key = str(
            row["alarm_key"]
        )

        if key in current_state_alarm_keys:
            continue

        tuner_id = int(
            row["tuner_id"]
        )

        sampled_at = current_tuner_sample_times.get(
            tuner_id
        )

        if sampled_at is None:
            continue

        conn.execute(
            """
            UPDATE alarm_state
            SET
                active = 0,
                last_observed_at = ?,
                last_changed_at = ?
            WHERE alarm_key = ?
            """,
            (
                sampled_at,
                sampled_at,
                key,
            ),
        )

        insert_monitoring_event(
            conn,
            occurred_at=sampled_at,
            module_id=int(
                row["module_id"]
            ),
            tuner_id=tuner_id,
            pid_id=(
                None
                if row["pid_id"] is None
                else int(row["pid_id"])
            ),
            category="recovery",
            severity="info",
            metric=str(
                row["metric"]
            ),
            value=None,
            delta=None,
            message=(
                "ALARM RECOVERY: "
                f"{row['category']}/{row['metric']} "
                "returned to normal."
            ),
        )

        recovered += 1

    return recovered


def persist_evaluation(
    evaluation: dict[str, Any],
    *,
    database_path: Path | str = DATABASE_PATH,
) -> PersistenceResult:
    alarms: list[Alarm] = list(
        evaluation["alarms"]
    )

    current_tuner_sample_times = {
        int(state["tuner_id"]): str(
            state["sampled_at"]
        )
        for state in evaluation["states"]
    }

    event_inserted = 0
    opened = 0
    refreshed = 0
    duplicate_suppressed = 0

    current_state_alarm_keys: set[str] = set()

    with closing(
        open_db(database_path)
    ) as conn:
        ensure_alarm_state_schema(
            conn
        )

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        try:
            for alarm in alarms:
                if is_stateful_alarm(
                    alarm
                ):
                    key = alarm_key(
                        alarm
                    )
                    current_state_alarm_keys.add(
                        key
                    )

                    action = persist_stateful_alarm(
                        conn,
                        alarm,
                    )

                    if action == "opened":
                        opened += 1
                    else:
                        refreshed += 1

                else:
                    inserted = persist_event_alarm(
                        conn,
                        alarm,
                    )

                    if inserted:
                        event_inserted += 1
                    else:
                        duplicate_suppressed += 1

            recovered = recover_missing_stateful_alarms(
                conn,
                current_state_alarm_keys=current_state_alarm_keys,
                current_tuner_sample_times=current_tuner_sample_times,
            )

            conn.commit()

        except Exception:
            conn.rollback()
            raise

    return PersistenceResult(
        evaluated_alarms=len(
            alarms
        ),
        event_alarms_inserted=event_inserted,
        state_alarms_opened=opened,
        state_alarms_refreshed=refreshed,
        state_alarms_recovered=recovered,
        duplicate_events_suppressed=duplicate_suppressed,
    )


def evaluate_and_persist(
    *,
    database_path: Path | str = DATABASE_PATH,
    thresholds: AlarmThresholds = DEFAULT_THRESHOLDS,
) -> tuple[
    dict[str, Any],
    PersistenceResult,
]:
    evaluation = evaluate_latest_snapshot(
        database_path=database_path,
        thresholds=thresholds,
    )

    persistence = persist_evaluation(
        evaluation,
        database_path=database_path,
    )

    return evaluation, persistence


def print_persistence_result(
    result: PersistenceResult,
) -> None:
    print("=" * 88)
    print("WISI GT34 ALARM PERSISTENCE")
    print("=" * 88)
    print(
        f"Evaluated alarms              : "
        f"{result.evaluated_alarms}"
    )
    print(
        f"New event alarms inserted     : "
        f"{result.event_alarms_inserted}"
    )
    print(
        f"State alarms opened           : "
        f"{result.state_alarms_opened}"
    )
    print(
        f"State alarms refreshed        : "
        f"{result.state_alarms_refreshed}"
    )
    print(
        f"State alarms recovered        : "
        f"{result.state_alarms_recovered}"
    )
    print(
        f"Duplicate events suppressed   : "
        f"{result.duplicate_events_suppressed}"
    )
    print("=" * 88)


def main() -> int:
    _, persistence = evaluate_and_persist()

    print_persistence_result(
        persistence
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
