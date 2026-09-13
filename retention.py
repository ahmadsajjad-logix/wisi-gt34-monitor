from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from config import DATABASE_PATH


RETENTION_DAYS = 15


@dataclass(frozen=True)
class RetentionTarget:
    table: str
    timestamp_column: str


# Historical/append-only data only.
#
# Deliberately NOT included:
#   chassis
#   modules
#   tuners
#   pids
#   transport_streams
#   services
#   service_streams
#   counter_state
#
# Those tables contain equipment identity, live inventory, or counter baselines
# required by subsequent collection cycles.
RETENTION_TARGETS: tuple[RetentionTarget, ...] = (
    RetentionTarget("tuner_samples", "sampled_at"),
    RetentionTarget("ts_samples", "sampled_at"),
    RetentionTarget("pid_samples", "sampled_at"),
    RetentionTarget("pcr_input_samples", "sampled_at"),
    RetentionTarget("pcr_pid_samples", "sampled_at"),
    RetentionTarget("monitoring_events", "occurred_at"),
    RetentionTarget("poll_runs", "started_at"),
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def cutoff_iso(
    *,
    retention_days: int = RETENTION_DAYS,
    now: datetime | None = None,
) -> str:
    if retention_days <= 0:
        raise ValueError("retention_days must be greater than zero")

    reference = now if now is not None else utc_now()

    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    else:
        reference = reference.astimezone(timezone.utc)

    cutoff = reference - timedelta(days=retention_days)
    return cutoff.isoformat(timespec="seconds")


def open_db(path: Path | str = DATABASE_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table,),
    ).fetchone()

    return row is not None


def column_exists(
    conn: sqlite3.Connection,
    table: str,
    column: str,
) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def validate_targets(
    conn: sqlite3.Connection,
    targets: Iterable[RetentionTarget] = RETENTION_TARGETS,
) -> None:
    errors: list[str] = []

    for target in targets:
        if not table_exists(conn, target.table):
            errors.append(f"missing table: {target.table}")
            continue

        if not column_exists(
            conn,
            target.table,
            target.timestamp_column,
        ):
            errors.append(
                f"missing column: "
                f"{target.table}.{target.timestamp_column}"
            )

    if errors:
        raise RuntimeError(
            "Retention schema validation failed: " + "; ".join(errors)
        )


def count_expired_rows(
    conn: sqlite3.Connection,
    *,
    target: RetentionTarget,
    cutoff: str,
) -> int:
    sql = (
        f"SELECT COUNT(*) "
        f"FROM {target.table} "
        f"WHERE {target.timestamp_column} < ?"
    )

    return int(conn.execute(sql, (cutoff,)).fetchone()[0])


def build_retention_report(
    conn: sqlite3.Connection,
    *,
    cutoff: str,
) -> dict[str, int]:
    report: dict[str, int] = {}

    for target in RETENTION_TARGETS:
        report[target.table] = count_expired_rows(
            conn,
            target=target,
            cutoff=cutoff,
        )

    return report


def cleanup_retention(
    *,
    retention_days: int = RETENTION_DAYS,
    apply: bool = False,
    database_path: Path | str = DATABASE_PATH,
) -> dict[str, object]:
    """
    Delete historical rows older than the configured retention window.

    Safe behavior:
      - apply=False: report only; no data is changed.
      - apply=True: all deletes occur inside one transaction.
      - any failure rolls back the entire cleanup.
      - counter_state and inventory/identity tables are never age-pruned here.
      - database connection is explicitly closed before returning.
    """
    cutoff = cutoff_iso(retention_days=retention_days)

    # IMPORTANT:
    # sqlite3.Connection.__enter__/__exit__ manages transactions but does not
    # guarantee that the connection object is closed. On Windows that can keep
    # the SQLite file locked. contextlib.closing guarantees deterministic close.
    with closing(open_db(database_path)) as conn:
        validate_targets(conn)

        before = build_retention_report(
            conn,
            cutoff=cutoff,
        )

        deleted = {
            table: 0
            for table in before
        }

        if apply:
            try:
                conn.execute("BEGIN IMMEDIATE")

                for target in RETENTION_TARGETS:
                    sql = (
                        f"DELETE FROM {target.table} "
                        f"WHERE {target.timestamp_column} < ?"
                    )

                    cursor = conn.execute(
                        sql,
                        (cutoff,),
                    )

                    deleted[target.table] = max(
                        int(cursor.rowcount),
                        0,
                    )

                conn.commit()

            except Exception:
                conn.rollback()
                raise

        return {
            "database_path": str(database_path),
            "retention_days": retention_days,
            "cutoff": cutoff,
            "apply": apply,
            "expired_rows": before,
            "deleted_rows": deleted,
            "total_expired": sum(before.values()),
            "total_deleted": sum(deleted.values()),
        }


def print_report(result: dict[str, object]) -> None:
    print("=" * 88)
    print("WISI GT34 DATABASE RETENTION")
    print("=" * 88)
    print(f"Database       : {result['database_path']}")
    print(f"Retention      : {result['retention_days']} days")
    print(f"Cutoff (UTC)   : {result['cutoff']}")
    print(
        f"Mode           : "
        f"{'APPLY' if result['apply'] else 'DRY RUN'}"
    )
    print("-" * 88)

    expired_rows = result["expired_rows"]
    deleted_rows = result["deleted_rows"]

    for table in expired_rows:
        print(
            f"{table:25s} "
            f"expired={expired_rows[table]:8d} "
            f"deleted={deleted_rows[table]:8d}"
        )

    print("-" * 88)
    print(f"Total expired  : {result['total_expired']}")
    print(f"Total deleted  : {result['total_deleted']}")

    if not result["apply"]:
        print()
        print(
            "DRY RUN ONLY: no rows were deleted. "
            "Use --apply to perform cleanup."
        )

    print("=" * 88)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Apply or preview the WISI GT34 SQLite "
            "historical-data retention policy."
        )
    )

    parser.add_argument(
        "--days",
        type=int,
        default=RETENTION_DAYS,
        help=f"Retention period in days (default: {RETENTION_DAYS})",
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete expired rows. Default is dry-run.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    result = cleanup_retention(
        retention_days=args.days,
        apply=args.apply,
    )

    print_report(result)


if __name__ == "__main__":
    main()
