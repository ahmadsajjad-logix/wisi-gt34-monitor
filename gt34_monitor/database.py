from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ConfiguredInput, ReceiverIndex, ReceiverSample


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS receivers (
    module INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    first_seen_utc TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    input_name TEXT,
    source_family TEXT,
    source_oid TEXT,
    PRIMARY KEY (module, channel)
);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    module INTEGER NOT NULL,
    channel INTEGER NOT NULL,
    lock INTEGER,
    rf_level_dbm REAL,
    snr_db REAL,
    ber REAL,
    frequency_khz INTEGER,
    polarisation INTEGER,
    symbol_rate_bd INTEGER,
    modulation INTEGER,
    code_rate INTEGER,
    pls_mode INTEGER,
    pls_id INTEGER,
    mis INTEGER,
    lnb_type INTEGER,
    lo_frequency_khz INTEGER,
    lnb_voltage INTEGER,
    tone_22khz INTEGER,
    ts_present INTEGER,
    ts_index INTEGER,
    ts_bitrate_bps INTEGER,
    ts_payload_bitrate_bps INTEGER,
    tsid INTEGER,
    nid INTEGER,
    onid INTEGER,
    network_name TEXT,
    service_count INTEGER,
    expected_service_name TEXT,
    expected_service_present INTEGER,
    expected_service_sid INTEGER,
    expected_service_status INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_receiver_time
ON samples(module, channel, timestamp_utc);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.executescript(SCHEMA)
        self._migrate_receivers_table()
        self._migrate_samples_table()
        self.connection.commit()

    def _migrate_receivers_table(self) -> None:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(receivers)")}
        for name in ("input_name", "source_family", "source_oid"):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE receivers ADD COLUMN {name} TEXT")

    def _migrate_samples_table(self) -> None:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(samples)")}
        additions = {
            "ts_present": "INTEGER",
            "ts_index": "INTEGER",
            "ts_bitrate_bps": "INTEGER",
            "ts_payload_bitrate_bps": "INTEGER",
            "tsid": "INTEGER",
            "nid": "INTEGER",
            "onid": "INTEGER",
            "network_name": "TEXT",
            "service_count": "INTEGER",
            "expected_service_name": "TEXT",
            "expected_service_present": "INTEGER",
            "expected_service_sid": "INTEGER",
            "expected_service_status": "INTEGER",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE samples ADD COLUMN {name} {sql_type}")

    def close(self) -> None:
        self.connection.close()

    def upsert_receiver(self, receiver: ReceiverIndex | ConfiguredInput, seen_utc: str) -> None:
        input_name = receiver.name if isinstance(receiver, ConfiguredInput) else None
        source_family = receiver.family if isinstance(receiver, ConfiguredInput) else None
        source_oid = receiver.source_oid if isinstance(receiver, ConfiguredInput) else None
        self.connection.execute(
            """
            INSERT INTO receivers(
                module, channel, first_seen_utc, last_seen_utc,
                input_name, source_family, source_oid
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(module, channel) DO UPDATE SET
                last_seen_utc=excluded.last_seen_utc,
                input_name=COALESCE(excluded.input_name, receivers.input_name),
                source_family=COALESCE(excluded.source_family, receivers.source_family),
                source_oid=COALESCE(excluded.source_oid, receivers.source_oid)
            """,
            (
                receiver.module, receiver.channel, seen_utc, seen_utc,
                input_name, source_family, source_oid,
            ),
        )

    def insert_sample(self, sample: ReceiverSample) -> None:
        columns = [
            "timestamp_utc", "module", "channel", "lock", "rf_level_dbm", "snr_db", "ber",
            "frequency_khz", "polarisation", "symbol_rate_bd", "modulation", "code_rate",
            "pls_mode", "pls_id", "mis", "lnb_type", "lo_frequency_khz", "lnb_voltage", "tone_22khz",
            "ts_present", "ts_index", "ts_bitrate_bps", "ts_payload_bitrate_bps",
            "tsid", "nid", "onid", "network_name", "service_count",
            "expected_service_name", "expected_service_present",
            "expected_service_sid", "expected_service_status",
        ]
        values = [getattr(sample, c) for c in columns]
        self.connection.execute(
            f"INSERT INTO samples ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            values,
        )
        self.connection.commit()

    def purge_older_than(self, days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.connection.execute("DELETE FROM samples WHERE timestamp_utc < ?", (cutoff,))
        self.connection.commit()
        return cur.rowcount
