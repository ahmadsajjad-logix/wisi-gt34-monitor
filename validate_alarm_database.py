from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from config import DATABASE_PATH


def open_db(path: Path | str = DATABASE_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def main() -> int:
    db_path = Path(DATABASE_PATH)

    print("=" * 96)
    print("WISI GT34 INTEGRATED ALARM DATABASE VALIDATION")
    print("=" * 96)
    print(f"Database : {db_path}")

    if not db_path.exists():
        raise FileNotFoundError(
            f"Database does not exist: {db_path}"
        )

    with closing(open_db(db_path)) as conn:
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if "alarm_state" not in tables:
            raise RuntimeError(
                "alarm_state table does not exist."
            )

        print()
        print("1. TABLE COUNTS")
        print("-" * 96)

        for table in (
            "monitoring_events",
            "alarm_state",
            "poll_runs",
            "tuner_samples",
            "ts_samples",
            "pid_samples",
            "pcr_pid_samples",
        ):
            count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
            )
            print(f"{table:24s} {count:10d}")

        print()
        print("2. ALARM STATE")
        print("-" * 96)

        active_states = conn.execute(
            """
            SELECT
                s.alarm_key,
                s.category,
                s.metric,
                s.severity,
                s.active,
                s.first_active_at,
                s.last_observed_at,
                m.module_number,
                t.input_id
            FROM alarm_state s
            JOIN tuners t
              ON t.id = s.tuner_id
            JOIN modules m
              ON m.id = s.module_id
            ORDER BY
                s.active DESC,
                m.module_number,
                t.input_id,
                s.metric
            """
        ).fetchall()

        if not active_states:
            print("No stateful alarms currently stored.")
        else:
            for row in active_states:
                print(
                    f"M{row['module_number']} "
                    f"Input {row['input_id']} | "
                    f"active={row['active']} | "
                    f"{row['severity']} | "
                    f"{row['category']}/{row['metric']} | "
                    f"first={row['first_active_at']} | "
                    f"last={row['last_observed_at']}"
                )

        print()
        print("3. LATEST 25 MONITORING EVENTS")
        print("-" * 96)

        latest_events = conn.execute(
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
            FROM monitoring_events e
            JOIN modules m
              ON m.id = e.module_id
            LEFT JOIN tuners t
              ON t.id = e.tuner_id
            LEFT JOIN pids p
              ON p.id = e.pid_id
            ORDER BY e.id DESC
            LIMIT 25
            """
        ).fetchall()

        for row in latest_events:
            pid_text = (
                ""
                if row["pid"] is None
                else f" PID={row['pid']}"
            )
            print(
                f"id={row['id']} | "
                f"{row['occurred_at']} | "
                f"M{row['module_number']} "
                f"Input {row['input_id']}"
                f"{pid_text} | "
                f"{row['severity']} | "
                f"{row['category']}/{row['metric']} | "
                f"delta={row['delta']} | "
                f"{row['message']}"
            )

        print()
        print("4. EXACT-DUPLICATE EVENT CHECK")
        print("-" * 96)

        duplicates = conn.execute(
            """
            SELECT
                occurred_at,
                module_id,
                tuner_id,
                pid_id,
                category,
                severity,
                metric,
                COUNT(*) AS copies
            FROM monitoring_events
            GROUP BY
                occurred_at,
                module_id,
                tuner_id,
                pid_id,
                category,
                severity,
                metric
            HAVING COUNT(*) > 1
            ORDER BY copies DESC
            """
        ).fetchall()

        print(
            f"Duplicate event groups : {len(duplicates)}"
        )

        if duplicates:
            for row in duplicates[:20]:
                print(
                    f"{row['occurred_at']} | "
                    f"module_id={row['module_id']} | "
                    f"tuner_id={row['tuner_id']} | "
                    f"pid_id={row['pid_id']} | "
                    f"{row['category']}/{row['metric']} | "
                    f"copies={row['copies']}"
                )

        print()
        print("5. DATABASE INTEGRITY")
        print("-" * 96)

        integrity = conn.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

        fk_violations = conn.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

        print(f"integrity_check         : {integrity}")
        print(f"foreign-key violations : {len(fk_violations)}")

        if integrity != "ok":
            raise RuntimeError(
                "SQLite integrity_check failed."
            )

        if fk_violations:
            raise RuntimeError(
                "Foreign-key violations detected."
            )

        if duplicates:
            raise RuntimeError(
                "Exact duplicate monitoring-event groups detected."
            )

    print()
    print("=" * 96)
    print("INTEGRATED ALARM DATABASE VALIDATION: PASS")
    print("=" * 96)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
