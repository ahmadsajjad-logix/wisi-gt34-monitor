from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import DATABASE_PATH
from retention import RETENTION_DAYS, cleanup_retention


HISTORICAL_TABLES = (
    ("tuner_samples", "sampled_at"),
    ("ts_samples", "sampled_at"),
    ("pid_samples", "sampled_at"),
    ("pcr_input_samples", "sampled_at"),
    ("pcr_pid_samples", "sampled_at"),
    ("monitoring_events", "occurred_at"),
    ("poll_runs", "started_at"),
)

PROTECTED_TABLES = (
    "chassis",
    "modules",
    "tuners",
    "pids",
    "transport_streams",
    "services",
    "service_streams",
    "counter_state",
)


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def count_rows(
    conn: sqlite3.Connection,
    tables: tuple[str, ...],
) -> dict[str, int]:
    return {
        table: int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        )
        for table in tables
    }


def oldest_test_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        - timedelta(days=RETENTION_DAYS + 5)
    ).isoformat(timespec="seconds")


def age_one_row_per_historical_table(
    conn: sqlite3.Connection,
    *,
    old_timestamp: str,
) -> dict[str, int]:
    """
    On the TEMPORARY database only, mark one existing row in each historical
    table as older than retention.

    No rows are inserted and no production database is modified.
    """
    modified: dict[str, int] = {}

    for table, timestamp_column in HISTORICAL_TABLES:
        # Explicit alias avoids SQLite/Python row-factory ambiguity where
        # "rowid" can be exposed under the INTEGER PRIMARY KEY column name.
        row = conn.execute(
            f"""
            SELECT rowid AS test_rowid
            FROM {table}
            ORDER BY rowid
            LIMIT 1
            """
        ).fetchone()

        if row is None:
            modified[table] = 0
            continue

        cursor = conn.execute(
            f"""
            UPDATE {table}
            SET {timestamp_column} = ?
            WHERE rowid = ?
            """,
            (
                old_timestamp,
                int(row["test_rowid"]),
            ),
        )

        modified[table] = max(
            int(cursor.rowcount),
            0,
        )

    conn.commit()
    return modified


def create_test_database(
    production_db: Path,
    test_db: Path,
) -> None:
    """
    Create a transactionally consistent SQLite backup and explicitly close
    both database handles before returning.
    """
    with closing(open_db(production_db)) as source:
        with closing(open_db(test_db)) as destination:
            source.backup(destination)


def main() -> None:
    production_db = Path(DATABASE_PATH)

    print("=" * 96)
    print("WISI GT34 RETENTION APPLY-PATH TEST")
    print("=" * 96)
    print(f"Production DB : {production_db}")

    if not production_db.exists():
        raise FileNotFoundError(
            f"Production database does not exist: {production_db}"
        )

    with tempfile.TemporaryDirectory(
        prefix="wisi-retention-test-"
    ) as temp_dir:
        test_db = Path(temp_dir) / "wisi_monitor_test.db"

        create_test_database(
            production_db,
            test_db,
        )

        print(f"Temporary DB  : {test_db}")
        print()
        print(
            "IMPORTANT: all modifications below are made only to the "
            "temporary database."
        )

        with closing(open_db(test_db)) as conn:
            protected_before = count_rows(
                conn,
                PROTECTED_TABLES,
            )

            historical_before = count_rows(
                conn,
                tuple(
                    table
                    for table, _ in HISTORICAL_TABLES
                ),
            )

            old_timestamp = oldest_test_timestamp()

            modified = age_one_row_per_historical_table(
                conn,
                old_timestamp=old_timestamp,
            )

        print()
        print("-" * 96)
        print("1. TEST DATA PREPARATION")
        print("-" * 96)
        print(
            f"Synthetic old timestamp : {old_timestamp}"
        )

        for table, count in modified.items():
            print(
                f"{table:25s} "
                f"rows aged for test={count}"
            )

        expected_expired = sum(
            modified.values()
        )

        print()
        print(
            f"Expected expired rows    : "
            f"{expected_expired}"
        )

        print()
        print("-" * 96)
        print(
            "2. RETENTION DRY RUN AGAINST TEMPORARY DATABASE"
        )
        print("-" * 96)

        dry_run = cleanup_retention(
            retention_days=RETENTION_DAYS,
            apply=False,
            database_path=test_db,
        )

        for table, count in dry_run[
            "expired_rows"
        ].items():
            print(
                f"{table:25s} "
                f"expired={count}"
            )

        print(
            f"Dry-run total expired    : "
            f"{dry_run['total_expired']}"
        )

        if (
            dry_run["total_expired"]
            != expected_expired
        ):
            raise AssertionError(
                "Dry-run expired-row count does not match "
                "the rows prepared for the test."
            )

        print()
        print("-" * 96)
        print(
            "3. APPLY RETENTION AGAINST TEMPORARY DATABASE"
        )
        print("-" * 96)

        applied = cleanup_retention(
            retention_days=RETENTION_DAYS,
            apply=True,
            database_path=test_db,
        )

        for table, count in applied[
            "deleted_rows"
        ].items():
            print(
                f"{table:25s} "
                f"deleted={count}"
            )

        print(
            f"Apply total deleted      : "
            f"{applied['total_deleted']}"
        )

        if (
            applied["total_deleted"]
            != expected_expired
        ):
            raise AssertionError(
                "Applied delete count does not match "
                "expected test rows."
            )

        with closing(open_db(test_db)) as conn:
            historical_after = count_rows(
                conn,
                tuple(
                    table
                    for table, _ in HISTORICAL_TABLES
                ),
            )

            protected_after = count_rows(
                conn,
                PROTECTED_TABLES,
            )

            integrity = conn.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]

            foreign_keys = conn.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()

        print()
        print("-" * 96)
        print("4. POST-CLEANUP VERIFICATION")
        print("-" * 96)

        for table, before_count in (
            historical_before.items()
        ):
            after_count = historical_after[
                table
            ]

            expected_after = (
                before_count
                - modified[table]
            )

            status = (
                "PASS"
                if after_count
                == expected_after
                else "FAIL"
            )

            print(
                f"{table:25s} "
                f"before={before_count:6d} "
                f"after={after_count:6d} "
                f"expected={expected_after:6d} "
                f"{status}"
            )

            if (
                after_count
                != expected_after
            ):
                raise AssertionError(
                    "Historical row-count mismatch "
                    f"for {table}"
                )

        print()
        print("Protected tables:")

        for table, before_count in (
            protected_before.items()
        ):
            after_count = protected_after[
                table
            ]

            status = (
                "PASS"
                if before_count
                == after_count
                else "FAIL"
            )

            print(
                f"{table:25s} "
                f"before={before_count:6d} "
                f"after={after_count:6d} "
                f"{status}"
            )

            if (
                before_count
                != after_count
            ):
                raise AssertionError(
                    "Protected table was modified: "
                    f"{table}"
                )

        print()
        print(
            f"SQLite integrity_check   : "
            f"{integrity}"
        )
        print(
            f"Foreign-key violations   : "
            f"{len(foreign_keys)}"
        )

        if integrity != "ok":
            raise AssertionError(
                "SQLite integrity_check failed: "
                f"{integrity}"
            )

        if foreign_keys:
            raise AssertionError(
                "Foreign-key violations detected "
                "after retention cleanup."
            )

        print()
        print("=" * 96)
        print(
            "RETENTION APPLY-PATH TEST: PASS"
        )
        print(
            "Production database was NOT modified."
        )
        print("=" * 96)


if __name__ == "__main__":
    main()
