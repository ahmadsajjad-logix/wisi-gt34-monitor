from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from alarm_policy import EXPECTED_ACTIVE_INPUTS
from config import DATABASE_PATH, WISI_MODULES

REQUIRED_TABLES = {
    "schema_metadata", "chassis", "modules", "tuners", "tuner_samples",
    "ts_samples", "pids", "pid_samples", "pcr_input_samples",
    "pcr_pid_samples", "transport_streams", "services", "service_streams",
    "counter_state", "monitoring_events", "poll_runs", "alarm_state",
    "channel_inventory", "channel_state", "channel_audio_inventory",
    "channel_audio_state", "channel_transition_events",
}


def age_seconds(value: str) -> float:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds())


def check(database_path: Path | str, max_age_seconds: float | None) -> int:
    failures: list[str] = []
    warnings: list[str] = []
    db = Path(database_path)

    print("WISI GT34 PRODUCTION SELF-TEST")
    print(f"Database: {db}")
    if not db.exists():
        print("FAIL: database does not exist")
        return 1

    conn = sqlite3.connect(str(db), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"SQLite integrity_check: {integrity}")
        if integrity != "ok":
            failures.append(f"SQLite integrity_check={integrity}")

        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        print(f"Foreign-key violations: {len(fk)}")
        if fk:
            failures.append(f"{len(fk)} foreign-key violations")

        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = sorted(REQUIRED_TABLES - tables)
        print(f"Required tables present: {len(REQUIRED_TABLES) - len(missing)}/{len(REQUIRED_TABLES)}")
        if missing:
            failures.append("Missing tables: " + ", ".join(missing))

        configured_modules = set(WISI_MODULES)
        db_modules = {int(r[0]) for r in conn.execute("SELECT module_number FROM modules")}
        missing_modules = sorted(configured_modules - db_modules)
        print(f"Configured modules present: {len(configured_modules) - len(missing_modules)}/{len(configured_modules)}")
        if missing_modules:
            failures.append("Missing configured modules: " + ", ".join(map(str, missing_modules)))

        found_inputs: set[tuple[int, int]] = set()
        for row in conn.execute(
            """
            SELECT m.module_number, t.input_id
            FROM tuners t JOIN modules m ON m.id=t.module_id
            """
        ):
            found_inputs.add((int(row[0]), int(row[1])))
        missing_expected = sorted(EXPECTED_ACTIVE_INPUTS - found_inputs)
        print(f"Expected-active inputs present: {len(EXPECTED_ACTIVE_INPUTS) - len(missing_expected)}/{len(EXPECTED_ACTIVE_INPUTS)}")
        if missing_expected:
            failures.append(f"Missing expected-active inputs: {missing_expected}")

        incomplete = conn.execute(
            "SELECT COUNT(*) FROM poll_runs WHERE completed_at IS NULL"
        ).fetchone()[0]
        if incomplete:
            warnings.append(f"{incomplete} incomplete poll_run record(s) exist; this can occur after interruption/crash")

        poll = conn.execute(
            "SELECT id, started_at, completed_at, success FROM poll_runs WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if poll is None:
            failures.append("No completed poll_runs record exists")
        else:
            print(f"Latest completed poll: id={poll['id']} success={poll['success']} completed={poll['completed_at']}")
            if int(poll["success"] or 0) != 1:
                failures.append("Latest completed poll was not successful")
            if max_age_seconds is not None:
                age = age_seconds(str(poll["completed_at"]))
                print(f"Latest completed poll age: {age:.1f}s")
                if age > max_age_seconds:
                    warnings.append(f"Latest completed poll is stale ({age:.1f}s > {max_age_seconds:.1f}s)")

        pending = 0
        if "channel_transition_events" in tables:
            pending = conn.execute("SELECT COUNT(*) FROM channel_transition_events WHERE emailed=0").fetchone()[0]
        print(f"Pending channel emails: {pending}")
        if pending:
            warnings.append(f"{pending} channel transition event(s) are pending email delivery")

        active_states = 0
        if "alarm_state" in tables:
            active_states = conn.execute("SELECT COUNT(*) FROM alarm_state WHERE active=1").fetchone()[0]
        print(f"Active durable alarms: {active_states}")

    finally:
        conn.close()

    for item in warnings:
        print(f"WARN: {item}")
    for item in failures:
        print(f"FAIL: {item}")

    if failures:
        print("SELF-TEST RESULT: FAIL")
        return 1
    print("SELF-TEST RESULT: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline/read-only production validation for WISI GT34 monitor.")
    parser.add_argument("--database", default=str(DATABASE_PATH))
    parser.add_argument("--max-age", type=float, default=None, help="Warn if latest completed poll is older than this many seconds")
    args = parser.parse_args()
    return check(args.database, args.max_age)


if __name__ == "__main__":
    raise SystemExit(main())
