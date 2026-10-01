from __future__ import annotations

import argparse
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from config import DATABASE_PATH


RETENTION_DAYS = 30
RETENTION_BATCH_SIZE = 50_000
RETENTION_BATCH_PAUSE_SECONDS = 0.05


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
    RetentionTarget("service_samples", "sampled_at"),
    RetentionTarget("input_tuner_mapping_samples", "sampled_at"),
    RetentionTarget("tuner_config_samples", "sampled_at"),
    RetentionTarget("tv43_carrier_history", "sampled_at"),
    RetentionTarget("tv43_service_history", "sampled_at"),
    RetentionTarget("wellav_input_samples", "sampled_at"),
    RetentionTarget("wellav_service_samples", "sampled_at"),
    RetentionTarget("wellav_poll_runs", "started_at"),
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



def primary_key_column(
    conn: sqlite3.Connection,
    table: str,
) -> str:
    rows = conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    primary_keys = sorted(
        (int(row["pk"]), str(row["name"]))
        for row in rows
        if int(row["pk"] or 0) > 0
    )

    if len(primary_keys) != 1:
        raise RuntimeError(
            f"Retention target {table} must have exactly one "
            f"primary-key column; found {len(primary_keys)}"
        )

    return primary_keys[0][1]


def delete_expired_batches(
    conn: sqlite3.Connection,
    *,
    target: RetentionTarget,
    cutoff: str,
    batch_size: int = RETENTION_BATCH_SIZE,
    batch_pause_seconds: float = RETENTION_BATCH_PAUSE_SECONDS,
) -> int:
    """Delete expired append-only history in short committed batches.

    The monitored history tables are append-only and use increasing integer
    primary keys. Oldest primary-key rows are processed first to avoid one
    enormous DELETE transaction and WAL burst on the production database.
    """
    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")

    pk = primary_key_column(conn, target.table)
    total_deleted = 0

    while True:
        oldest = conn.execute(
            f"SELECT {pk}, {target.timestamp_column} "
            f"FROM {target.table} "
            f"ORDER BY {pk} ASC LIMIT 1"
        ).fetchone()

        if oldest is None:
            break

        oldest_timestamp = str(oldest[target.timestamp_column])

        if oldest_timestamp >= cutoff:
            break

        try:
            conn.execute("BEGIN IMMEDIATE")
            changes_before = conn.total_changes

            conn.execute(
                f"DELETE FROM {target.table} "
                f"WHERE {pk} IN ("
                f"SELECT {pk} FROM {target.table} "
                f"WHERE {target.timestamp_column} < ? "
                f"ORDER BY {pk} ASC "
                f"LIMIT ?"
                f")",
                (cutoff, batch_size),
            )

            batch_deleted = conn.total_changes - changes_before
            conn.commit()

        except Exception:
            conn.rollback()
            raise

        total_deleted += int(batch_deleted)

        if batch_deleted == 0:
            break

        if batch_deleted < batch_size:
            break

        if batch_pause_seconds > 0:
            time.sleep(batch_pause_seconds)

    return total_deleted


def cleanup_retention(
    *,
    retention_days: int = RETENTION_DAYS,
    apply: bool = False,
    database_path: Path | str = DATABASE_PATH,
    batch_size: int = RETENTION_BATCH_SIZE,
    batch_pause_seconds: float = RETENTION_BATCH_PAUSE_SECONDS,
) -> dict[str, object]:
    """Apply the rolling history-retention policy.

    Dry-run mode preserves the existing exact-count report.
    Apply mode uses short per-batch transactions to limit writer-lock duration
    and WAL growth on large production databases.
    """
    cutoff = cutoff_iso(retention_days=retention_days)

    with closing(open_db(database_path)) as conn:
        validate_targets(conn)

        if not apply:
            before = build_retention_report(
                conn,
                cutoff=cutoff,
            )

            deleted = {
                table: 0
                for table in before
            }

            return {
                "database_path": str(database_path),
                "retention_days": retention_days,
                "cutoff": cutoff,
                "apply": False,
                "expired_rows": before,
                "deleted_rows": deleted,
                "total_expired": sum(before.values()),
                "total_deleted": 0,
            }

        deleted: dict[str, int] = {}

        for target in RETENTION_TARGETS:
            deleted[target.table] = delete_expired_batches(
                conn,
                target=target,
                cutoff=cutoff,
                batch_size=batch_size,
                batch_pause_seconds=batch_pause_seconds,
            )

        expired = dict(deleted)

        return {
            "database_path": str(database_path),
            "retention_days": retention_days,
            "cutoff": cutoff,
            "apply": True,
            "expired_rows": expired,
            "deleted_rows": deleted,
            "total_expired": sum(expired.values()),
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
