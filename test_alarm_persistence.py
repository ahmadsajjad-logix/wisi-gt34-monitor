from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from alarm_engine import evaluate_latest_snapshot
from alarm_persistence import (
    ensure_alarm_state_schema,
    open_db,
    persist_evaluation,
)
from config import DATABASE_PATH


def create_test_database(
    production_db: Path,
    test_db: Path,
) -> None:
    with closing(
        open_db(production_db)
    ) as source:
        with closing(
            open_db(test_db)
        ) as destination:
            source.backup(
                destination
            )


def event_count(
    conn: sqlite3.Connection,
) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM monitoring_events
            """
        ).fetchone()[0]
    )


def state_count(
    conn: sqlite3.Connection,
) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM alarm_state
            """
        ).fetchone()[0]
    )


def latest_tuner_sample_id(
    conn: sqlite3.Connection,
    *,
    module_number: int,
    input_id: int,
) -> int:
    row = conn.execute(
        """
        SELECT ts.id
        FROM tuner_samples ts
        JOIN tuners t
          ON t.id = ts.tuner_id
        JOIN modules m
          ON m.id = t.module_id
        WHERE m.module_number = ?
          AND t.input_id = ?
        ORDER BY ts.id DESC
        LIMIT 1
        """,
        (
            module_number,
            input_id,
        ),
    ).fetchone()

    if row is None:
        raise RuntimeError(
            "Could not locate requested tuner sample."
        )

    return int(
        row["id"]
    )


def main() -> int:
    production_db = Path(
        DATABASE_PATH
    )

    if not production_db.exists():
        raise FileNotFoundError(
            f"Production database does not exist: {production_db}"
        )

    print("=" * 96)
    print(
        "WISI GT34 ALARM PERSISTENCE "
        "TEMPORARY-DATABASE TEST"
    )
    print("=" * 96)
    print(
        f"Production DB : {production_db}"
    )

    with tempfile.TemporaryDirectory(
        prefix="wisi-alarm-test-"
    ) as temp_dir:
        test_db = (
            Path(temp_dir)
            / "wisi_monitor_alarm_test.db"
        )

        create_test_database(
            production_db,
            test_db,
        )

        print(
            f"Temporary DB  : {test_db}"
        )
        print()
        print(
            "IMPORTANT: all changes below are made "
            "only to the temporary database."
        )

        with closing(
            open_db(test_db)
        ) as conn:
            ensure_alarm_state_schema(
                conn
            )
            conn.commit()

            before_events = event_count(
                conn
            )

        print()
        print(
            "1. EVENT ALARM INSERTION / "
            "DUPLICATE SUPPRESSION"
        )

        evaluation = evaluate_latest_snapshot(
            database_path=test_db
        )

        first = persist_evaluation(
            evaluation,
            database_path=test_db,
        )

        second = persist_evaluation(
            evaluation,
            database_path=test_db,
        )

        print(
            f"Latest evaluated alarms       : "
            f"{len(evaluation['alarms'])}"
        )
        print(
            f"First pass event inserts      : "
            f"{first.event_alarms_inserted}"
        )
        print(
            f"Second pass event inserts     : "
            f"{second.event_alarms_inserted}"
        )
        print(
            f"Second pass duplicates stopped: "
            f"{second.duplicate_events_suppressed}"
        )

        if second.event_alarms_inserted != 0:
            raise AssertionError(
                "Duplicate event alarms were inserted."
            )

        print()
        print(
            "2. STATE ALARM OPEN / REFRESH / RECOVERY"
        )

        with closing(
            open_db(test_db)
        ) as conn:
            sample_id = latest_tuner_sample_id(
                conn,
                module_number=1,
                input_id=2,
            )

            original = conn.execute(
                """
                SELECT
                    lock_state,
                    rf_level_dbm,
                    snr_db,
                    ber_text,
                    ber_value
                FROM tuner_samples
                WHERE id = ?
                """,
                (sample_id,),
            ).fetchone()

            conn.execute(
                """
                UPDATE tuner_samples
                SET lock_state = 0
                WHERE id = ?
                """,
                (sample_id,),
            )
            conn.commit()

        fault_eval = evaluate_latest_snapshot(
            database_path=test_db
        )

        fault_first = persist_evaluation(
            fault_eval,
            database_path=test_db,
        )

        fault_second = persist_evaluation(
            fault_eval,
            database_path=test_db,
        )

        print(
            f"State opens after lock loss   : "
            f"{fault_first.state_alarms_opened}"
        )
        print(
            f"State refreshes on repeat     : "
            f"{fault_second.state_alarms_refreshed}"
        )

        if fault_first.state_alarms_opened < 1:
            raise AssertionError(
                "Synthetic lock loss did not open a state alarm."
            )

        if fault_second.state_alarms_opened != 0:
            raise AssertionError(
                "Repeated state evaluation reopened an active alarm."
            )

        with closing(
            open_db(test_db)
        ) as conn:
            conn.execute(
                """
                UPDATE tuner_samples
                SET
                    lock_state = ?,
                    rf_level_dbm = ?,
                    snr_db = ?,
                    ber_text = ?,
                    ber_value = ?
                WHERE id = ?
                """,
                (
                    original["lock_state"],
                    original["rf_level_dbm"],
                    original["snr_db"],
                    original["ber_text"],
                    original["ber_value"],
                    sample_id,
                ),
            )
            conn.commit()

        recovery_eval = evaluate_latest_snapshot(
            database_path=test_db
        )

        recovery = persist_evaluation(
            recovery_eval,
            database_path=test_db,
        )

        print(
            f"State recoveries             : "
            f"{recovery.state_alarms_recovered}"
        )

        if recovery.state_alarms_recovered < 1:
            raise AssertionError(
                "Synthetic lock recovery was not recorded."
            )

        print()
        print(
            "3. POST-TEST DATABASE CHECKS"
        )

        with closing(
            open_db(test_db)
        ) as conn:
            after_events = event_count(
                conn
            )
            states = state_count(
                conn
            )
            integrity = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]
            fk_violations = conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()

        print(
            f"monitoring_events before      : "
            f"{before_events}"
        )
        print(
            f"monitoring_events after       : "
            f"{after_events}"
        )
        print(
            f"alarm_state rows              : "
            f"{states}"
        )
        print(
            f"SQLite integrity_check        : "
            f"{integrity}"
        )
        print(
            f"Foreign-key violations        : "
            f"{len(fk_violations)}"
        )

        if integrity != "ok":
            raise AssertionError(
                "SQLite integrity_check failed."
            )

        if fk_violations:
            raise AssertionError(
                "Foreign-key violations detected."
            )

    print()
    print("=" * 96)
    print(
        "ALARM PERSISTENCE TEST: PASS"
    )
    print(
        "Production database was NOT modified."
    )
    print("=" * 96)

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
