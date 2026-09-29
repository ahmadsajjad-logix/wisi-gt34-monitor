from __future__ import annotations

import argparse
import csv
import json
import logging
from html import escape
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from email_notifier import EMAIL_FROM, EMAIL_TO, send_message
from wisi_native_events import fetch_slot_log, tuner_unlock_events

POLICY_DB = ROOT / "database" / "tv43_alarm_policy.sqlite3"
MONITOR_DB = ROOT / "database" / "wisi_monitor.db"
EXPECTED_SERVICES_FILE = ROOT / "tv43_expected_services.json"
MANIFEST = ROOT / "prtg_tv43_deployment" / "tv43_created_sensors.csv"
METADATA_FILE = ROOT / "tv43_carrier_metadata.csv"
LOG_PATH = ROOT / "logs" / "tv43_alarm_policy.log"

CHECK_INTERVAL_SECONDS = 5
SNAPSHOT_STALE_SECONDS = 120
ALARM_PERSISTENCE_SECONDS = 15
CARRIER_UNLOCK_PERSISTENCE_SECONDS = 20
NULL_PAYLOAD_PERSISTENCE_SECONDS = 20
RECOVERY_PERSISTENCE_SECONDS = 10

# Collector normally produces observations about every 5 seconds.
# A gap greater than this breaks continuous DOWN/UP persistence.
PERSISTENCE_MAX_SAMPLE_GAP_SECONDS = 8
CURSOR_BOOTSTRAP_HISTORY_SECONDS = 30

SUBJECT = "Transmission Alert"

# Carrier identity is resolved dynamically from the validated administrative RF
# fingerprint against the collector's current WISI tuner configuration.  A
# monitoring-path-unavailable result is coverage state, never transmission state.
RF_FREQUENCY_TOLERANCE_MHZ = 0.001
RF_SYMBOL_RATE_TOLERANCE_MBD = 0.001


@dataclass(frozen=True)
class Condition:
    kind: str
    event_key: str
    affected: tuple[tuple[int, str], ...]
    status_line: str


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_time(dt: datetime) -> str:
    return dt.astimezone(ZoneInfo("Asia/Karachi")).strftime(
        "%Y-%m-%d %H:%M:%S PKT"
    )


def _native_to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    # Tangram TS-monitor timestamps are chassis local time. The deployed chassis
    # is configured for Karachi (UTC+05:00) with NTP synchronization.
    return value.replace(tzinfo=ZoneInfo("Asia/Karachi")).astimezone(timezone.utc)


def native_carrier_times(meta: dict[str, Any], *, observed_at: datetime,
                         started_at: datetime | None = None, recovery: bool = False,
                         log: logging.Logger | None = None) -> tuple[datetime | None, datetime | None]:
    """Resolve the best-correlated native Tuner-unlock interval; fail closed."""
    try:
        events = fetch_slot_log(str(meta["host"]), int(meta["module"]), length=100)
        matches = tuner_unlock_events(events, int(meta["channel"]))

        if recovery:
            if started_at is None:
                return None, None

            candidates: list[tuple[float, datetime, datetime]] = []
            rejected = 0
            for event in matches:
                ns = _native_to_utc(event.started_at_local)
                ne = _native_to_utc(event.ended_at_local)
                if event.active or ns is None or ne is None:
                    continue

                # A completed native interval must be internally valid and must
                # not end before the DOWN time already stored for this episode.
                if ne < ns or ne < started_at:
                    rejected += 1
                    continue

                # Keep correlation local to this episode.  The start window
                # preserves the existing 180-second tolerance for cases where
                # the episode opened on the SQLite observation time rather than
                # a native DOWN.  The native UP may be at most 30 seconds ahead
                # of the recovery observation to tolerate small chassis/poll skew.
                if abs((ns - started_at).total_seconds()) > 180:
                    rejected += 1
                    continue
                if ne > observed_at + timedelta(seconds=30):
                    rejected += 1
                    continue
                if abs((ne - observed_at).total_seconds()) > 180:
                    rejected += 1
                    continue

                # Prefer the interval whose DOWN most closely matches the active
                # episode; use recovery proximity only as the tie-breaker.
                score = (
                    abs((ns - started_at).total_seconds()) * 1000.0
                    + abs((ne - observed_at).total_seconds())
                )
                candidates.append((score, ns, ne))

            if candidates:
                _, ns, ne = min(candidates, key=lambda item: item[0])
                return ns, ne

            if log is not None:
                log.info(
                    "No trustworthy WISI native recovery match for %s M%sC%s; using SQLite observation time | candidates_rejected=%d",
                    meta.get("host"), meta.get("module"), meta.get("channel"), rejected,
                )
            return None, None

        candidates: list[tuple[float, datetime]] = []
        for event in matches:
            ns = _native_to_utc(event.started_at_local)
            if not event.active or ns is None:
                continue
            delta = (observed_at - ns).total_seconds()
            if -30 <= delta <= 180:
                candidates.append((abs(delta), ns))

        if candidates:
            _, ns = min(candidates, key=lambda item: item[0])
            return ns, None

    except Exception as exc:
        if log is not None:
            log.warning(
                "Native WISI event lookup unavailable for %s M%sC%s: %s",
                meta.get("host"), meta.get("module"), meta.get("channel"), exc,
            )
    return None, None


def duration_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{mins:02d}:{secs:02d}"
    return f"{hours:02d}:{mins:02d}:{secs:02d}"


def configure_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("tv43_alarm_policy")
    log.setLevel(logging.INFO)
    log.propagate = False
    if log.handlers:
        return log

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    fh = RotatingFileHandler(
        LOG_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    return log


def open_db() -> sqlite3.Connection:
    POLICY_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(POLICY_DB), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS alert_episode (
            event_key TEXT PRIMARY KEY,
            host TEXT NOT NULL,
            module INTEGER NOT NULL,
            channel INTEGER NOT NULL,
            condition_kind TEXT NOT NULL,
            active INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            cleared_at TEXT,
            affected_json TEXT NOT NULL,
            status_line TEXT NOT NULL,
            alarm_email_sent_at TEXT,
            recovery_email_sent_at TEXT
        );

        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            cleared_at TEXT,
            duration_seconds INTEGER,
            condition_kind TEXT NOT NULL,
            affected_json TEXT NOT NULL,
            status_line TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transition_candidate (
            event_key TEXT PRIMARY KEY,
            direction TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            condition_kind TEXT NOT NULL,
            affected_json TEXT NOT NULL,
            status_line TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS carrier_sample_cursor (
            carrier_key TEXT PRIMARY KEY,
            last_processed_sampled_at TEXT NOT NULL,
            configured_tuner_db_id INTEGER,
            resolved_tuner_db_id INTEGER,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS email_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_key TEXT NOT NULL UNIQUE,
            carrier_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            episode_targets_json TEXT NOT NULL,
            message_bytes BLOB NOT NULL,
            occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            next_attempt_at TEXT,
            last_error TEXT,
            sent_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_email_outbox_pending
        ON email_outbox(status, id);
        """
    )
    conn.commit()


def _load_carrier_metadata() -> dict[str, dict[str, Any]]:
    if not METADATA_FILE.exists():
        raise FileNotFoundError(f"TV43 carrier metadata missing: {METADATA_FILE}")
    metadata: dict[str, dict[str, Any]] = {}
    with METADATA_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            host = str(row["host"]).strip()
            module = int(row["module"])
            channel = int(row["channel"])
            key = f"{host}|M{module}C{channel}"
            status = str(row.get("validation_status") or "").strip().lower()
            satellite = str(row.get("satellite_name") or "").strip()
            metadata[key] = {
                "frequency_mhz": float(row["frequency_mhz"]),
                "polarisation": str(row["polarisation"]).strip().upper(),
                "symbol_rate_mbd": float(row["symbol_rate_mbd"]),
                "identity_name": str(row.get("carrier_name") or "").strip(),
                "satellite_name": satellite if status == "validated" and satellite else "Not validated",
                "frequency_band": str(row.get("frequency_band") or "Not configured").strip(),
                "metadata_validation_status": status or "unknown",
            }
    if len(metadata) != 43:
        raise RuntimeError(f"Expected 43 TV43 metadata rows, found {len(metadata)}")
    return metadata


def load_manifest() -> dict[str, dict[str, Any]]:
    if not MANIFEST.exists():
        raise FileNotFoundError(f"TV43 deployment state missing: {MANIFEST}")
    carrier_metadata = _load_carrier_metadata()
    rows: dict[str, dict[str, Any]] = {}
    with MANIFEST.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            host = str(row["host"])
            module = int(row["module"])
            channel = int(row["channel"])
            key = f"{host}|M{module}C{channel}"
            if key not in carrier_metadata:
                raise RuntimeError(f"Missing validated metadata row for {key}")
            rows[key] = {
                "host": host,
                "module": module,
                "channel": channel,
                "carrier_name": str(row["sensor_name"]),
                **carrier_metadata[key],
            }
    if len(rows) != 43:
        raise RuntimeError(f"Expected 43 TV43 carriers, found {len(rows)}")
    return rows


def load_expected_services() -> dict[str, tuple[tuple[int, str], ...]]:
    """Load the administrative expected-service baseline independent of PRTG/WISI live state."""
    if not EXPECTED_SERVICES_FILE.exists():
        raise FileNotFoundError(f"Expected-service configuration missing: {EXPECTED_SERVICES_FILE}")
    raw = json.loads(EXPECTED_SERVICES_FILE.read_text(encoding="utf-8"))
    carriers = raw.get("carriers")
    if not isinstance(carriers, dict):
        raise RuntimeError("Invalid expected-service configuration: carriers must be an object")
    result: dict[str, tuple[tuple[int, str], ...]] = {}
    for key, entry in carriers.items():
        rows = entry.get("services", []) if isinstance(entry, dict) else []
        services: list[tuple[int, str]] = []
        seen: set[int] = set()
        for row in rows:
            sid = int(row["sid"])
            name = str(row.get("name") or f"SID {sid}").strip() or f"SID {sid}"
            if sid in seen:
                raise RuntimeError(f"Duplicate expected SID {sid} for {key}")
            seen.add(sid)
            services.append((sid, name))
        if not services:
            raise RuntimeError(f"No expected services configured for {key}")
        result[str(key)] = tuple(services)
    return result


def validate_expected_services(manifest: dict[str, dict[str, Any]], expected: dict[str, tuple[tuple[int, str], ...]]) -> None:
    manifest_keys = set(manifest)
    expected_keys = set(expected)
    missing = sorted(manifest_keys - expected_keys)
    extra = sorted(expected_keys - manifest_keys)
    if missing or extra:
        raise RuntimeError(
            f"Expected-service configuration mismatch: missing={missing or 'none'} extra={extra or 'none'}"
        )


def open_monitor_db() -> sqlite3.Connection:
    if not MONITOR_DB.exists():
        raise FileNotFoundError(f"Central monitor database missing: {MONITOR_DB}")
    conn = sqlite3.connect(
        f"file:{MONITOR_DB.as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _identity_name(value: Any) -> str:
    """Normalize a WISI/admin label only for deterministic identity tie-breaking."""
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _monitoring_path_unavailable(
    meta: dict[str, Any], expected_rows: list[dict[str, Any]], observed_at: str,
    *, configured_input_id: int | None = None, tuner_object_id: int | None = None,
    configured_name: str | None = None, configured_uuid: str | None = None,
) -> dict[str, Any]:
    return {
        "host": meta["host"], "module": int(meta["module"]),
        "channel": int(meta["channel"]), "observed_at": observed_at,
        "execution_error": "MONITORING_PATH_UNAVAILABLE", "channels": {},
        "expected_services": expected_rows, "missing_services": [],
        "es_missing_services": [], "source": "central_sqlite_dynamic_identity",
        "configured_input_id": configured_input_id,
        "resolved_tuner_object_id": tuner_object_id,
        "configured_name": configured_name, "configured_uuid": configured_uuid,
    }


def read_monitor_snapshot(
    monitor_conn: sqlite3.Connection,
    meta: dict[str, Any],
    expected_services: dict[str, tuple[tuple[int, str], ...]],
) -> dict[str, Any] | None:
    """Resolve the administrative carrier to its current live WISI path.

    The administrative host/key, RF fingerprint and expected-service definition
    remain stable. Module number, configured-input ID, tuner-object ID and UUID
    are live routing attributes and may change after GT34 hardware/configuration
    changes.

    Search every current module on the administrative chassis for the exact RF
    fingerprint. Rank current RESOLVED WISI relationships by exact normalized
    identity-name agreement, enabled state, then clean WISI relationship status.
    The winner must be unique; ambiguity remains fail-closed.
    """
    key = f"{meta['host']}|M{int(meta['module'])}C{int(meta['channel'])}"
    expected = expected_services[key]
    expected_rows = [{"sid": sid, "name": name} for sid, name in expected]

    module_rows = monitor_conn.execute(
        """SELECT m.id AS module_id, m.module_number
           FROM chassis ch JOIN modules m ON m.chassis_id=ch.id
           WHERE ch.host=?
           ORDER BY m.module_number""",
        (meta["host"],),
    ).fetchall()
    if not module_rows:
        return None

    admin_name = _identity_name(meta.get("identity_name"))
    ranked: list[
        tuple[tuple[int, int, int], sqlite3.Row, sqlite3.Row, int, int, str]
    ] = []
    latest_seen: str | None = None

    for module_row in module_rows:
        live_module_id = int(module_row["module_id"])
        live_module_number = int(module_row["module_number"])

        latest_cfg = monitor_conn.execute(
            """SELECT MAX(sampled_at) AS sampled_at
               FROM tuner_config_samples
               WHERE module_id=?""",
            (live_module_id,),
        ).fetchone()

        sampled_at = None if latest_cfg is None else latest_cfg["sampled_at"]
        if sampled_at is None:
            continue
        sampled_at = str(sampled_at)

        if latest_seen is None or sampled_at > latest_seen:
            latest_seen = sampled_at

        rf_rows = monitor_conn.execute(
            """SELECT * FROM tuner_config_samples
               WHERE module_id=? AND sampled_at=?
                 AND ABS(frequency_mhz-?)<=?
                 AND UPPER(COALESCE(polarisation,''))=?
                 AND ABS(symbol_rate_mbd-?)<=?
               ORDER BY tuner_object_id""",
            (
                live_module_id,
                sampled_at,
                float(meta["frequency_mhz"]),
                RF_FREQUENCY_TOLERANCE_MHZ,
                str(meta["polarisation"]).upper(),
                float(meta["symbol_rate_mbd"]),
                RF_SYMBOL_RATE_TOLERANCE_MBD,
            ),
        ).fetchall()

        if not rf_rows:
            continue

        rf_by_tuner = {
            int(row["tuner_object_id"]): row
            for row in rf_rows
        }

        candidate_tuners = sorted(rf_by_tuner)
        placeholders = ",".join("?" for _ in candidate_tuners)

        mapping_rows = monitor_conn.execute(
            f"""SELECT * FROM input_tuner_mapping_samples
                WHERE module_id=? AND sampled_at=?
                  AND tuner_object_id IN ({placeholders})
                  AND mapping_status='RESOLVED'
                ORDER BY configured_input_id""",
            (
                live_module_id,
                sampled_at,
                *candidate_tuners,
            ),
        ).fetchall()

        for mapping in mapping_rows:
            tuner_object_id = int(mapping["tuner_object_id"])
            rf = rf_by_tuner.get(tuner_object_id)
            if rf is None:
                continue

            name_match = int(
                bool(admin_name)
                and _identity_name(mapping["configured_name"]) == admin_name
            )
            enabled = int(mapping["input_enabled"] == 1)
            clean_status = int(
                str(mapping["error_status"] or "").strip().lower() == "no error"
            )

            ranked.append(
                (
                    (name_match, enabled, clean_status),
                    mapping,
                    rf,
                    live_module_id,
                    live_module_number,
                    sampled_at,
                )
            )

    observed_at = latest_seen or datetime.now(timezone.utc).isoformat()

    if not ranked:
        # No current RF/mapping candidate exists.
        # Do not scan the complete historical collector database here.
        # Persistent policy cursor state later distinguishes a previously
        # established path from a never-established monitoring path.
        return _monitoring_path_unavailable(
            meta, expected_rows, observed_at
        )

    best_score = max(score for score, *_ in ranked)
    winners = [
        (
            mapping,
            rf,
            live_module_id,
            live_module_number,
            sampled_at,
        )
        for (
            score,
            mapping,
            rf,
            live_module_id,
            live_module_number,
            sampled_at,
        ) in ranked
        if score == best_score
    ]

    if len(winners) != 1:
        return _monitoring_path_unavailable(
            meta, expected_rows, observed_at
        )

    (
        mapping,
        rf,
        live_module_id,
        live_module_number,
        sampled_at,
    ) = winners[0]

    configured_input_id = int(mapping["configured_input_id"])
    tuner_object_id = int(mapping["tuner_object_id"])

    # Demodulator health follows the dynamically resolved live module/tuner.
    tuner = monitor_conn.execute(
        """SELECT
               t.id AS resolved_tuner_db_id,
               ts.lock_state,
               ts.enabled,
               ts.state,
               ts.disabled
           FROM tuners t JOIN tuner_samples ts ON ts.tuner_id=t.id
           WHERE t.module_id=? AND t.input_id=? AND ts.sampled_at=?
           ORDER BY ts.id DESC LIMIT 1""",
        (live_module_id, tuner_object_id, sampled_at),
    ).fetchone()

    if tuner is None:
        return _monitoring_path_unavailable(
            meta,
            expected_rows,
            sampled_at,
            configured_input_id=configured_input_id,
            tuner_object_id=tuner_object_id,
            configured_name=mapping["configured_name"],
            configured_uuid=mapping["configured_uuid"],
        )

    # TS/service acquisition follows the resolved logical input on the live
    # module. Configured input and tuner object may legitimately differ.
    configured = monitor_conn.execute(
        """SELECT id AS tuner_id
           FROM tuners
           WHERE module_id=? AND input_id=?
           LIMIT 1""",
        (live_module_id, configured_input_id),
    ).fetchone()

    if configured is None:
        return _monitoring_path_unavailable(
            meta,
            expected_rows,
            sampled_at,
            configured_input_id=configured_input_id,
            tuner_object_id=tuner_object_id,
            configured_name=mapping["configured_name"],
            configured_uuid=mapping["configured_uuid"],
        )

    configured_db_id = int(configured["tuner_id"])

    ts_row = monitor_conn.execute(
        """SELECT current_bitrate_bps
           FROM ts_samples
           WHERE tuner_id=? AND sampled_at=?
           ORDER BY id DESC LIMIT 1""",
        (configured_db_id, sampled_at),
    ).fetchone()

    service_rows = monitor_conn.execute(
        """SELECT service_id,service_name,elementary_stream_count
           FROM service_samples
           WHERE tuner_id=? AND sampled_at=?
           ORDER BY service_id""",
        (configured_db_id, sampled_at),
    ).fetchall()

    bitrate = None if ts_row is None else ts_row["current_bitrate_bps"]

    service_observation_available = bool(service_rows)

    current_services = {
        int(row["service_id"]):
            str(row["service_name"] or f"SID {row['service_id']}")
        for row in service_rows
    }

    current_es_counts = {
        int(row["service_id"]): row["elementary_stream_count"]
        for row in service_rows
    }

    # An entirely empty service enumeration while the RF/TS path is otherwise
    # healthy is not affirmative evidence that every expected service is DOWN.
    # Treat it as unavailable service-level observation for this sample.
    missing = (
        [
            {"sid": sid, "name": name}
            for sid, name in expected
            if sid not in current_services
        ]
        if service_observation_available
        else []
    )


    # A service can remain advertised while its elementary streams vanish.
    # This has been verified for Geo ME, VSH and Star News Asia.
    # ES=0 is therefore affirmative individual-service DOWN evidence.
    es_missing = (
        [
            {"sid": sid, "name": name}
            for sid, name in expected
            if (
                sid in current_services
                and current_es_counts.get(sid) is not None
                and int(current_es_counts[sid]) == 0
            )
        ]
        if service_observation_available
        else []
    )

    return {
        # Keep administrative module/channel stable for alarm/event identity.
        "host": meta["host"],
        "module": int(meta["module"]),
        "channel": int(meta["channel"]),
        "observed_at": sampled_at,
        "execution_error": None,
        "channels": {
            "Demod Lock": int(tuner["lock_state"] or 0),
            "Transport Stream Present":
                1 if bitrate is not None and float(bitrate) > 0 else 0,
            "Input Enabled": mapping["input_enabled"],
            "Input State": tuner["state"],
            "Input Disabled": tuner["disabled"],
        },
        "expected_services": expected_rows,
        "missing_services": missing,
        "es_missing_services": es_missing,
        "service_observation_available": service_observation_available,
        "source": "central_sqlite_dynamic_identity",
        "configured_input_id": configured_input_id,
        "configured_tuner_db_id": configured_db_id,
        "resolved_tuner_object_id": tuner_object_id,
        "resolved_tuner_db_id": int(tuner["resolved_tuner_db_id"]),
        "configured_name": mapping["configured_name"],
        "configured_uuid": mapping["configured_uuid"],
        "live_module": live_module_number,
        "live_module_db_id": live_module_id,
    }



def read_resolved_sample_history(
    monitor_conn: sqlite3.Connection,
    *,
    base_snapshot: dict[str, Any],
    after_sampled_at: datetime,
    until_sampled_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Reconstruct authoritative collector observations for one already-resolved
    carrier path.

    Lock/demodulator evidence follows resolved_tuner_db_id.
    TS/service evidence follows configured_tuner_db_id.

    The function is read-only and returns observations in chronological order.
    A collector timestamp without a TS sample is omitted; that creates a time
    gap and therefore cannot contribute to persistence.
    """

    resolved_tuner_db_id = base_snapshot.get(
        "resolved_tuner_db_id"
    )
    configured_tuner_db_id = base_snapshot.get(
        "configured_tuner_db_id"
    )

    if (
        resolved_tuner_db_id is None
        or configured_tuner_db_id is None
    ):
        return []

    if until_sampled_at is None:
        until_sampled_at = parse_dt(
            str(base_snapshot["observed_at"])
        )

    expected_rows = list(
        base_snapshot.get("expected_services") or []
    )

    expected = [
        (int(row["sid"]), str(row["name"]))
        for row in expected_rows
    ]

    tuner_rows = monitor_conn.execute(
        """
        SELECT
            id,
            sampled_at,
            lock_state,
            enabled,
            state,
            disabled
        FROM tuner_samples
        WHERE tuner_id=?
          AND sampled_at>?
          AND sampled_at<=?
        ORDER BY sampled_at,id
        """,
        (
            int(resolved_tuner_db_id),
            after_sampled_at.isoformat(),
            until_sampled_at.isoformat(),
        ),
    ).fetchall()

    # If duplicates exist for one timestamp, retain the newest database row.
    tuner_by_time: dict[str, sqlite3.Row] = {}

    for row in tuner_rows:
        tuner_by_time[str(row["sampled_at"])] = row

    result: list[dict[str, Any]] = []

    for sampled_at in sorted(tuner_by_time):
        tuner = tuner_by_time[sampled_at]

        ts_row = monitor_conn.execute(
            """
            SELECT current_bitrate_bps
            FROM ts_samples
            WHERE tuner_id=?
              AND sampled_at=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                int(configured_tuner_db_id),
                sampled_at,
            ),
        ).fetchone()

        # No TS row means the collector did not provide a complete
        # transmission observation for this cycle. Do not convert missing
        # telemetry into a false DOWN or UP state.
        if ts_row is None:
            continue

        service_rows = monitor_conn.execute(
            """
            SELECT
                service_id,
                service_name,
                elementary_stream_count
            FROM service_samples
            WHERE tuner_id=?
              AND sampled_at=?
            ORDER BY service_id
            """,
            (
                int(configured_tuner_db_id),
                sampled_at,
            ),
        ).fetchall()

        bitrate = ts_row["current_bitrate_bps"]

        service_observation_available = bool(
            service_rows
        )

        pid_rows = monitor_conn.execute(
            """
            SELECT p.pid
            FROM pids AS p
            JOIN pid_samples AS ps
              ON ps.pid_id = p.id
            WHERE p.tuner_id=?
              AND ps.sampled_at=?
            ORDER BY p.pid
            """,
            (
                int(configured_tuner_db_id),
                sampled_at,
            ),
        ).fetchall()

        observed_pids = {
            int(row["pid"])
            for row in pid_rows
        }

        pid_observation_available = bool(pid_rows)

        # PID 8191 / 0x1FFF is the MPEG-TS NULL-packet PID.
        # Missing PID telemetry is UNKNOWN, not payload failure.
        null_payload_only = (
            pid_observation_available
            and observed_pids == {8191}
        )

        current_services = {
            int(row["service_id"]):
                str(
                    row["service_name"]
                    or f"SID {row['service_id']}"
                )
            for row in service_rows
        }

        current_es_counts = {
            int(row["service_id"]):
                row["elementary_stream_count"]
            for row in service_rows
        }

        missing = (
            [
                {
                    "sid": sid,
                    "name": name,
                }
                for sid, name in expected
                if sid not in current_services
            ]
            if service_observation_available
            else []
        )

        es_missing = (
            [
                {
                    "sid": sid,
                    "name": name,
                }
                for sid, name in expected
                if (
                    sid in current_services
                    and current_es_counts.get(sid)
                        is not None
                    and int(
                        current_es_counts[sid]
                    ) == 0
                )
            ]
            if service_observation_available
            else []
        )

        historical = dict(base_snapshot)

        historical["observed_at"] = sampled_at
        historical["execution_error"] = None
        historical["expected_path_absent"] = False

        historical["channels"] = {
            "Demod Lock":
                int(tuner["lock_state"] or 0),

            "Transport Stream Present":
                (
                    1
                    if (
                        bitrate is not None
                        and float(bitrate) > 0
                    )
                    else 0
                ),

            "Input Enabled": tuner["enabled"],
            "Input State": tuner["state"],
            "Input Disabled": tuner["disabled"],
        }

        historical["missing_services"] = missing
        historical["es_missing_services"] = (
            es_missing
        )
        historical[
            "service_observation_available"
        ] = service_observation_available

        historical["pid_observation_available"] = (
            pid_observation_available
        )
        historical["observed_pids"] = sorted(
            observed_pids
        )
        historical["null_payload_only"] = (
            null_payload_only
        )
        historical["source"] = (
            "central_sqlite_resolved_history"
        )

        result.append(historical)

    return result

def services(snapshot: dict[str, Any], field: str) -> tuple[tuple[int, str], ...]:
    result = []
    for row in snapshot.get(field, []) or []:
        result.append((int(row["sid"]), str(row["name"])))
    return tuple(result)


def derive_conditions(snapshot: dict[str, Any]) -> list[Condition]:
    ch = dict(snapshot.get("channels") or {})
    host = str(snapshot["host"])
    module = int(snapshot["module"])
    channel = int(snapshot["channel"])
    prefix = f"{host}|M{module}C{channel}"

    expected = services(snapshot, "expected_services")
    missing = services(snapshot, "missing_services")
    es_missing = services(snapshot, "es_missing_services")

    if snapshot.get("execution_error"):
        return [
            Condition(
                kind="execution_failure",
                event_key=f"{prefix}|execution_failure",
                affected=expected,
                status_line="❌ Monitoring execution failure",
            )
        ]

    # A previously established expected configured input that disappears from a
    # successful current mapping generation is a real expected-path outage.
    if snapshot.get("expected_path_absent"):
        return [
            Condition(
                kind="expected_path_down",
                event_key=f"{prefix}|carrier_down",
                affected=expected,
                status_line="❌ Expected input path DOWN",
            )
        ]

    input_enabled = ch.get("Input Enabled")
    input_disabled = ch.get("Input Disabled")
    if input_enabled is not None and int(input_enabled or 0) == 0 or (input_disabled is not None and int(input_disabled or 0) == 1):
        return [
            Condition(
                kind="input_disabled",
                event_key=f"{prefix}|carrier_down",
                affected=expected,
                status_line="❌ Expected input OFF / disabled",
            )
        ]

    locked = int(
        ch.get("Demod Lock", 0) or 0
    ) == 1

    ts_up = int(
        ch.get("Transport Stream Present", 0) or 0
    ) == 1

    # CONDITION 1:
    # A continuously unlocked demodulator is authoritative carrier-down
    # evidence. Transport Stream Present may remain stale or oscillate
    # during RF loss and therefore must not override Demod Lock=0.
    #
    # The normal 15-second persistence layer prevents short WISI lock
    # flaps from becoming alarms.
    if not locked:
        return [
            Condition(
                kind="carrier_unlocked",
                event_key=f"{prefix}|carrier_down",
                affected=expected,
                status_line="? Carrier UNLOCKED",
            )
        ]

    # CONDITION 2:
    # Demodulator is locked, but the transport stream itself is absent.
    if not ts_up:
        return [
            Condition(
                kind="transport_stream_down",
                event_key=f"{prefix}|carrier_down",
                affected=expected,
                status_line="? Carrier LOCKED + Transport Stream DOWN",
            )
        ]

    # CONDITION 3:
    # Carrier and TS framing are present, but the observed transport
    # contains only PID 8191 (NULL packets). This is affirmative
    # evidence that useful program payload is absent.
    if bool(snapshot.get("null_payload_only")):
        return [
            Condition(
                kind="transport_payload_down",
                event_key=f"{prefix}|carrier_down",
                affected=expected,
                status_line="? Carrier LOCKED + Transport Payload DOWN (NULL packets only)",
            )
        ]

    # CONDITION 4:
    # Carrier/TS/program payload work; only failed individual services DOWN.
    down_by_sid: dict[int, str] = {}
    for sid, name in missing:
        down_by_sid[sid] = name
    for sid, name in es_missing:
        down_by_sid[sid] = name

    result: list[Condition] = []
    for sid, name in sorted(down_by_sid.items()):
        result.append(
            Condition(
                kind="individual_service_down",
                event_key=f"{prefix}|service_down|SID{sid}",
                affected=((sid, name),),
                status_line=f"❌ {name} — DOWN",
            )
        )
    return result


def make_email(
    *,
    meta: dict[str, Any],
    condition: Condition,
    event_type: str,
    started_at: datetime,
    occurred_at: datetime,
) -> EmailMessage:
    # Import lazily to avoid a circular import during module startup.
    from tv43_whatsapp_nologo_email import make_whatsapp_email

    return make_whatsapp_email(
        meta=meta,
        condition=condition,
        event_type=event_type,
        started_at=started_at,
        occurred_at=occurred_at,
    )

def queue_email(
    conn: sqlite3.Connection,
    *,
    notification_key: str,
    carrier_key: str,
    event_type: str,
    episode_targets: list[dict[str, str | None]],
    message: EmailMessage,
    occurred_at: datetime,
) -> None:
    """Persist the exact rendered email in the same transaction as alarm state."""
    conn.execute(
        """
        INSERT OR IGNORE INTO email_outbox(
            notification_key,
            carrier_key,
            event_type,
            episode_targets_json,
            message_bytes,
            occurred_at,
            created_at,
            status,
            attempt_count,
            last_attempt_at,
            next_attempt_at,
            last_error,
            sent_at
        )
        VALUES(?,?,?,?,?,?,?,'PENDING',0,NULL,NULL,NULL,NULL)
        """,
        (
            notification_key,
            carrier_key,
            event_type,
            json.dumps(episode_targets, ensure_ascii=False),
            sqlite3.Binary(message.as_bytes()),
            occurred_at.isoformat(),
            utcnow().isoformat(),
        ),
    )


def deliver_pending_emails(
    conn: sqlite3.Connection,
    log: logging.Logger,
) -> int:
    """
    Deliver queued email strictly in outbox ID order.

    Stop after the first SMTP failure so a later recovery notification can never
    overtake an earlier alarm notification.
    """
    from email import policy
    from email.parser import BytesParser

    delivered = 0

    while True:
        row = conn.execute(
            """
            SELECT *
            FROM email_outbox
            WHERE status='PENDING'
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()

        if row is None:
            break

        outbox_id = int(row["id"])
        now = utcnow()

        next_attempt_raw = row["next_attempt_at"]
        if next_attempt_raw:
            next_attempt = parse_dt(str(next_attempt_raw))
            if now < next_attempt:
                break

        attempt_at = now.isoformat()

        try:
            msg = BytesParser(policy=policy.default).parsebytes(
                bytes(row["message_bytes"])
            )

            send_message(msg)

        except Exception as exc:
            # Retry after 60 seconds. Keep the row PENDING permanently until
            # successful delivery; never delete a failed notification.
            next_attempt = now + timedelta(seconds=60)

            conn.execute(
                """
                UPDATE email_outbox
                SET attempt_count=attempt_count+1,
                    last_attempt_at=?,
                    next_attempt_at=?,
                    last_error=?
                WHERE id=? AND status='PENDING'
                """,
                (
                    attempt_at,
                    next_attempt.isoformat(),
                    f"{type(exc).__name__}: {exc}"[:2000],
                    outbox_id,
                ),
            )
            conn.commit()

            log.error(
                "EMAIL OUTBOX DELIVERY FAILED | id=%d | key=%s | "
                "attempt=%d | retry_at=%s | error=%s",
                outbox_id,
                str(row["notification_key"]),
                int(row["attempt_count"]) + 1,
                next_attempt.isoformat(),
                exc,
            )

            # Strict ordering: do not send any later notification while this
            # earlier one remains undelivered.
            break

        sent_at = utcnow().isoformat()
        episode_targets = json.loads(str(row["episode_targets_json"]))
        event_type = str(row["event_type"])

        # Update delivery metadata only if alert_episode still represents the
        # exact episode that produced this queued notification. event_key is
        # intentionally reusable across later DOWN/UP cycles.
        for target in episode_targets:
            event_key = str(target["event_key"])
            started_at = str(target["started_at"])

            if event_type == "ALARM":
                conn.execute(
                    """
                    UPDATE alert_episode
                    SET alarm_email_sent_at=?
                    WHERE event_key=? AND started_at=?
                    """,
                    (sent_at, event_key, started_at),
                )
            elif event_type == "RECOVERY":
                cleared_at = str(target["cleared_at"])
                conn.execute(
                    """
                    UPDATE alert_episode
                    SET recovery_email_sent_at=?
                    WHERE event_key=? AND started_at=? AND cleared_at=?
                    """,
                    (sent_at, event_key, started_at, cleared_at),
                )
            else:
                raise RuntimeError(
                    f"Unsupported email_outbox event_type: {event_type}"
                )

        conn.execute(
            """
            UPDATE email_outbox
            SET status='SENT',
                attempt_count=attempt_count+1,
                last_attempt_at=?,
                next_attempt_at=NULL,
                last_error=NULL,
                sent_at=?
            WHERE id=? AND status='PENDING'
            """,
            (sent_at, sent_at, outbox_id),
        )
        conn.commit()

        delivered += 1

        log.info(
            "EMAIL OUTBOX DELIVERED | id=%d | key=%s | event_type=%s | "
            "episodes=%d",
            outbox_id,
            str(row["notification_key"]),
            str(row["event_type"]),
            len(episode_targets),
        )

    return delivered


def insert_history(
    conn: sqlite3.Connection,
    *,
    condition: Condition,
    event_type: str,
    occurred_at: datetime,
    started_at: datetime,
    cleared_at: datetime | None = None,
) -> None:
    duration = (
        None
        if cleared_at is None
        else max(0, int((cleared_at - started_at).total_seconds()))
    )
    conn.execute(
        """
        INSERT INTO alert_history(
            event_key,event_type,occurred_at,started_at,cleared_at,
            duration_seconds,condition_kind,affected_json,status_line
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            condition.event_key,
            event_type,
            occurred_at.isoformat(),
            started_at.isoformat(),
            None if cleared_at is None else cleared_at.isoformat(),
            duration,
            condition.kind,
            json.dumps(condition.affected, ensure_ascii=False),
            condition.status_line,
        ),
    )



def get_carrier_sample_cursor(
    conn: sqlite3.Connection,
    carrier_key: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            carrier_key,
            last_processed_sampled_at,
            configured_tuner_db_id,
            resolved_tuner_db_id,
            updated_at
        FROM carrier_sample_cursor
        WHERE carrier_key=?
        """,
        (carrier_key,),
    ).fetchone()


def set_carrier_sample_cursor(
    conn: sqlite3.Connection,
    *,
    carrier_key: str,
    sampled_at: datetime,
    configured_tuner_db_id: int | None,
    resolved_tuner_db_id: int | None,
) -> None:
    now = utcnow().isoformat()

    conn.execute(
        """
        INSERT INTO carrier_sample_cursor(
            carrier_key,
            last_processed_sampled_at,
            configured_tuner_db_id,
            resolved_tuner_db_id,
            updated_at
        )
        VALUES(?,?,?,?,?)
        ON CONFLICT(carrier_key) DO UPDATE SET
            last_processed_sampled_at=
                excluded.last_processed_sampled_at,
            configured_tuner_db_id=
                excluded.configured_tuner_db_id,
            resolved_tuner_db_id=
                excluded.resolved_tuner_db_id,
            updated_at=
                excluded.updated_at
        """,
        (
            carrier_key,
            sampled_at.isoformat(),
            configured_tuner_db_id,
            resolved_tuner_db_id,
            now,
        ),
    )

def _candidate_row(conn: sqlite3.Connection, event_key: str, direction: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM transition_candidate WHERE event_key=? AND direction=?",
        (event_key, direction),
    ).fetchone()


def _clear_candidate(conn: sqlite3.Connection, event_key: str, direction: str | None = None) -> None:
    if direction is None:
        conn.execute("DELETE FROM transition_candidate WHERE event_key=?", (event_key,))
    else:
        conn.execute(
            "DELETE FROM transition_candidate WHERE event_key=? AND direction=?",
            (event_key, direction),
        )


def _qualify_candidate(
    conn: sqlite3.Connection,
    *,
    condition: Condition,
    direction: str,
    observed_at: datetime,
    threshold_seconds: int,
) -> tuple[bool, datetime]:
    """Persist transition evidence across policy loops; return (qualified, first_seen)."""
    row = _candidate_row(conn, condition.event_key, direction)
    if row is None:
        # A direction reversal invalidates any pending opposite transition.
        _clear_candidate(conn, condition.event_key)
        conn.execute(
            """
            INSERT INTO transition_candidate(
                event_key,direction,first_seen_at,last_seen_at,
                condition_kind,affected_json,status_line
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                condition.event_key,
                direction,
                observed_at.isoformat(),
                observed_at.isoformat(),
                condition.kind,
                json.dumps(condition.affected, ensure_ascii=False),
                condition.status_line,
            ),
        )
        return False, observed_at

    first_seen = parse_dt(str(row["first_seen_at"]))
    last_seen = parse_dt(str(row["last_seen_at"]))

    # Persistence must be supported by consecutive observations of the
    # SAME underlying condition. A cause change (for example carrier_unlocked
    # -> transport_stream_down), a missing sample, or a late sample starts a
    # fresh persistence interval.
    gap_seconds = (observed_at - last_seen).total_seconds()
    condition_kind_changed = (
        str(row["condition_kind"]) != condition.kind
    )

    if (
        condition_kind_changed
        or gap_seconds < 0
        or gap_seconds > PERSISTENCE_MAX_SAMPLE_GAP_SECONDS
    ):
        conn.execute(
            """
            UPDATE transition_candidate
            SET first_seen_at=?,
                last_seen_at=?,
                condition_kind=?,
                affected_json=?,
                status_line=?
            WHERE event_key=?
            """,
            (
                observed_at.isoformat(),
                observed_at.isoformat(),
                condition.kind,
                json.dumps(condition.affected, ensure_ascii=False),
                condition.status_line,
                condition.event_key,
            ),
        )
        return False, observed_at

    conn.execute(
        """
        UPDATE transition_candidate
        SET last_seen_at=?,condition_kind=?,affected_json=?,status_line=?
        WHERE event_key=?
        """,
        (
            observed_at.isoformat(),
            condition.kind,
            json.dumps(condition.affected, ensure_ascii=False),
            condition.status_line,
            condition.event_key,
        ),
    )

    qualified = (
        observed_at - first_seen
    ).total_seconds() >= threshold_seconds

    return qualified, first_seen

def process_snapshot(
    conn: sqlite3.Connection,
    log: logging.Logger,
    key: str,
    meta: dict[str, Any],
    snapshot: dict[str, Any],
) -> None:
    observed_at = parse_dt(str(snapshot["observed_at"]))
    age = int((utcnow() - observed_at).total_seconds())
    if age > SNAPSHOT_STALE_SECONDS:
        log.warning("Stale snapshot %s age=%ss; no state transition inferred", key, age)
        return

    # A known unavailable monitoring path is coverage state, not transmission
    # state and not affirmative evidence of either Channel DOWN or Channel UP.
    #
    # If legacy active episodes exist for this administrative carrier, retire
    # them silently so they do not remain indefinitely active after monitoring
    # authority has been lost. This is administrative state reconciliation,
    # NOT a transmission recovery: no recovery email and no RECOVERY history
    # event are generated.
    if snapshot.get("execution_error") == "MONITORING_PATH_UNAVAILABLE":
        stale_rows = conn.execute(
            """
            SELECT event_key,condition_kind
            FROM alert_episode
            WHERE host=? AND module=? AND channel=? AND active=1
            """,
            (meta["host"], meta["module"], meta["channel"]),
        ).fetchall()

        for stale_row in stale_rows:
            stale_event_key = str(stale_row["event_key"])

            conn.execute(
                """
                UPDATE alert_episode
                SET active=0,
                    cleared_at=?,
                    last_seen_at=?
                WHERE event_key=? AND active=1
                """,
                (
                    observed_at.isoformat(),
                    observed_at.isoformat(),
                    stale_event_key,
                ),
            )

            _clear_candidate(conn, stale_event_key)

            log.warning(
                "ACTIVE EPISODE SILENTLY RETIRED | %s | kind=%s | "
                "reason=MONITORING_PATH_UNAVAILABLE | recovery_email=NOT_SENT",
                stale_event_key,
                str(stale_row["condition_kind"]),
            )

        # Also remove orphaned pending candidates belonging to this carrier.
        conn.execute(
            "DELETE FROM transition_candidate WHERE event_key LIKE ?",
            (f"{key}|%",),
        )

        return

    current = {c.event_key: c for c in derive_conditions(snapshot)}

    # Service enumeration can occasionally be absent even though the carrier
    # remains locked and the transport stream remains present. That is
    # service-observation uncertainty, not proof of service DOWN or service UP.
    #
    # Preserve any already-active individual-service alarms, and discard their
    # pending ALARM/RECOVERY persistence candidates so a later authoritative
    # service observation must establish a fresh continuous interval.
    service_observation_available = snapshot.get(
        "service_observation_available", True
    )

    if not service_observation_available:
        pending_service_rows = conn.execute(
            """
            SELECT event_key
            FROM transition_candidate
            WHERE event_key LIKE ?
            """,
            (f"{key}|service_down|SID%",),
        ).fetchall()

        for pending_service in pending_service_rows:
            _clear_candidate(conn, str(pending_service["event_key"]))

        # Do not let unavailable enumeration create service-down conditions.
        current = {
            event_key: condition
            for event_key, condition in current.items()
            if condition.kind != "individual_service_down"
        }

    active_rows = conn.execute(
        """
        SELECT * FROM alert_episode
        WHERE host=? AND module=? AND channel=? AND active=1
        """,
        (meta["host"], meta["module"], meta["channel"]),
    ).fetchall()
    active = {str(row["event_key"]): row for row in active_rows}

    # Individual-service state is authoritative only while the parent
    # carrier AND transport stream are available and service enumeration is
    # authoritative.  If the parent carrier/TS is DOWN, disappearance of an
    # individual_service_down condition does NOT prove that service recovered.
    #
    # Preserve established service episodes underneath the parent outage.
    # Once carrier+TS+service observation becomes authoritative again, the
    # normal recovery path may close an episode only if its SID is actually
    # present.  This prevents contradictory multiplex DOWN + service UP emails.
    ch = dict(snapshot.get("channels") or {})
    parent_transmission_available = (
        int(ch.get("Demod Lock", 0) or 0) == 1
        and int(ch.get("Transport Stream Present", 0) or 0) == 1
        and not bool(snapshot.get("null_payload_only"))
    )
    service_state_authoritative = (
        service_observation_available and parent_transmission_available
    )

    if not service_state_authoritative:
        # A pending service transition cannot accumulate persistence while
        # service state is unobservable beneath a parent carrier/TS outage.
        pending_service_rows = conn.execute(
            """
            SELECT event_key
            FROM transition_candidate
            WHERE event_key LIKE ?
            """,
            (f"{key}|service_down|SID%",),
        ).fetchall()

        for pending_service in pending_service_rows:
            _clear_candidate(conn, str(pending_service["event_key"]))

        # Keep established individual-service alarms logically active.
        # This suppresses false service recovery while the parent transmission
        # path cannot provide authoritative per-service evidence.
        for event_key, row in active.items():
            if str(row["condition_kind"]) == "individual_service_down":
                current[event_key] = Condition(
                    kind="individual_service_down",
                    event_key=event_key,
                    affected=tuple(
                        (int(item[0]), str(item[1]))
                        for item in json.loads(str(row["affected_json"]))
                    ),
                    status_line=str(row["status_line"]),
                )

    # Identify raw transitions. Transmission alarms/recoveries are qualified
    # below with configured persistence; execution_failure remains immediate
    # because it represents monitoring-path certainty rather than RF flapping.
    raw_new_items: list[tuple[str, Condition]] = [
        (event_key, condition)
        for event_key, condition in current.items()
        if event_key not in active
    ]
    continuing_items: list[tuple[str, Condition]] = [
        (event_key, condition)
        for event_key, condition in current.items()
        if event_key in active
    ]
    raw_recovery_items: list[tuple[str, sqlite3.Row]] = [
        (event_key, row)
        for event_key, row in active.items()
        if event_key not in current
    ]

    # Any currently active condition cancels a pending recovery candidate.
    for event_key, _condition in continuing_items:
        _clear_candidate(conn, event_key, "RECOVERY")

    # Any currently healthy/inactive event cancels a pending alarm candidate
    # when the condition is no longer present.
    active_keys = set(active)
    current_keys = set(current)
    # Only inspect pending ALARM candidates belonging to THIS carrier.
    # The previous global query allowed processing of the next healthy carrier
    # to delete another carrier's pending persistence candidate before it could
    # reach the configured threshold.
    pending_alarm_rows = conn.execute(
        "SELECT event_key FROM transition_candidate "
        "WHERE direction='ALARM' AND event_key LIKE ?",
        (f"{key}|%",),
    ).fetchall()
    for pending in pending_alarm_rows:
        pending_key = str(pending["event_key"])
        if pending_key not in current_keys and pending_key not in active_keys:
            _clear_candidate(conn, pending_key, "ALARM")

    new_items: list[tuple[str, Condition, datetime]] = []
    for event_key, condition in raw_new_items:
        if condition.kind == "execution_failure":
            _clear_candidate(conn, event_key)
            new_items.append((event_key, condition, observed_at))
            continue
        if condition.kind == "carrier_unlocked":
            alarm_threshold_seconds = CARRIER_UNLOCK_PERSISTENCE_SECONDS
        elif condition.kind == "transport_payload_down":
            alarm_threshold_seconds = NULL_PAYLOAD_PERSISTENCE_SECONDS
        else:
            alarm_threshold_seconds = ALARM_PERSISTENCE_SECONDS

        qualified, first_seen = _qualify_candidate(
            conn,
            condition=condition,
            direction="ALARM",
            observed_at=observed_at,
            threshold_seconds=alarm_threshold_seconds,
        )
        if qualified:
            _clear_candidate(conn, event_key, "ALARM")
            new_items.append((event_key, condition, first_seen))
            log.info(
                "ALARM PERSISTENCE QUALIFIED | %s | first_seen=%s | threshold=%ss",
                event_key, first_seen.isoformat(), alarm_threshold_seconds,
            )

    recovery_items: list[tuple[str, sqlite3.Row, datetime]] = []
    for event_key, row in raw_recovery_items:
        kind = str(row["condition_kind"])
        if kind == "execution_failure":
            _clear_candidate(conn, event_key)
            recovery_items.append((event_key, row, observed_at))
            continue

        # Monitoring-path uncertainty is not affirmative evidence of a
        # transmission recovery. Do not start or advance a recovery timer
        # while this snapshot has an execution/mapping error. Any previously
        # pending recovery is cleared so a later authoritative healthy sample
        # must establish a fresh continuous recovery interval.
        if snapshot.get("execution_error"):
            _clear_candidate(conn, event_key, "RECOVERY")
            continue

        affected = tuple(
            (int(item[0]), str(item[1]))
            for item in json.loads(str(row["affected_json"]))
        )
        recovery_condition = Condition(
            kind=kind,
            event_key=event_key,
            affected=affected,
            status_line=str(row["status_line"]),
        )
        qualified, first_seen = _qualify_candidate(
            conn,
            condition=recovery_condition,
            direction="RECOVERY",
            observed_at=observed_at,
            threshold_seconds=RECOVERY_PERSISTENCE_SECONDS,
        )
        if qualified:
            _clear_candidate(conn, event_key, "RECOVERY")
            recovery_items.append((event_key, row, first_seen))
            log.info(
                "RECOVERY PERSISTENCE QUALIFIED | %s | first_seen=%s | threshold=%ss",
                event_key, first_seen.isoformat(), RECOVERY_PERSISTENCE_SECONDS,
            )

    # Fail closed on monitoring-path uncertainty. Transmission recovery
    # candidates were already blocked above; keep this guard as defense in
    # depth in case recovery_items is populated by future policy changes.
    if snapshot.get("execution_error"):
        preserved_count = sum(
            1 for _event_key, row in raw_recovery_items
            if str(row["condition_kind"]) != "execution_failure"
        )
        if preserved_count:
            log.warning(
                "Recovery suppressed: monitoring path unavailable | %s | "
                "execution_error=%s | preserved_active=%d",
                key, snapshot.get("execution_error"), preserved_count,
            )
        recovery_items = [
            (event_key, row, recovery_first_seen)
            for event_key, row, recovery_first_seen in recovery_items
            if str(row["condition_kind"]) == "execution_failure"
        ]

    # --- ALARMS ---
    individual_new = [
        (event_key, condition, first_seen)
        for event_key, condition, first_seen in new_items
        if condition.kind == "individual_service_down"
    ]
    nonindividual_new = [
        (event_key, condition, first_seen)
        for event_key, condition, first_seen in new_items
        if condition.kind != "individual_service_down"
    ]

    # Carrier/TS/execution conditions already contain all affected services and
    # remain one email per condition.
    for event_key, condition, first_seen in nonindividual_new:
        # Persistence decides whether to notify; the reported transition time is
        # the first authoritative observation of the sustained condition.
        started_at = first_seen
        # The qualifying observation occurs after persistence has elapsed,
        # but the reported DOWN boundary remains the first authoritative
        # observation that began the persistence-qualified condition.
        # The persistence candidate's first authoritative monitor observation
        # is the canonical DOWN timestamp. WISI native event-log timestamps are
        # deliberately not substituted here: a nearby historical native event
        # may belong to a different RF interruption and can otherwise produce an
        # impossible sub-persistence reported outage.

        msg = make_email(
            meta=meta,
            condition=condition,
            event_type="ALARM",
            started_at=started_at,
            occurred_at=started_at,
        )

        conn.execute(
            """
            INSERT INTO alert_episode(
                event_key,host,module,channel,condition_kind,active,
                started_at,last_seen_at,cleared_at,affected_json,status_line,
                alarm_email_sent_at,recovery_email_sent_at
            )
            VALUES(?,?,?,?,?,1,?,?,NULL,?,?,NULL,NULL)
            ON CONFLICT(event_key) DO UPDATE SET
                active=1,
                condition_kind=excluded.condition_kind,
                started_at=excluded.started_at,
                last_seen_at=excluded.last_seen_at,
                cleared_at=NULL,
                affected_json=excluded.affected_json,
                status_line=excluded.status_line,
                alarm_email_sent_at=excluded.alarm_email_sent_at,
                recovery_email_sent_at=NULL
            """,
            (
                event_key,
                meta["host"],
                meta["module"],
                meta["channel"],
                condition.kind,
                started_at.isoformat(),
                observed_at.isoformat(),
                json.dumps(condition.affected, ensure_ascii=False),
                condition.status_line,
            ),
        )
        insert_history(
            conn,
            condition=condition,
            event_type="ALARM",
            occurred_at=started_at,
            started_at=started_at,
        )
        queue_email(
            conn,
            notification_key=f"ALARM|{event_key}|{started_at.isoformat()}",
            carrier_key=key,
            event_type="ALARM",
            episode_targets=[
                {
                    "event_key": event_key,
                    "started_at": started_at.isoformat(),
                    "cleared_at": None,
                }
            ],
            message=msg,
            occurred_at=started_at,
        )
        log.error(
            "ALARM RECORDED + EMAIL QUEUED | %s | %s",
            event_key,
            condition.status_line,
        )

    # All newly missing services on this carrier are one multiplex email.
    if individual_new:
        affected: list[tuple[int, str]] = []
        alarm_starts: list[datetime] = []
        for _, condition, first_seen in individual_new:
            affected.extend(condition.affected)
            alarm_starts.append(first_seen)

        # Stable SID order and de-duplication.
        affected = sorted(dict(affected).items())
        batch_condition = Condition(
            kind="individual_service_down",
            event_key=f"{key}|service_down|MULTIPLEX_BATCH",
            affected=tuple(affected),
            status_line="❌ Multiplex service(s) DOWN",
        )
        started_at = min(alarm_starts)

        msg = make_email(
            meta=meta,
            condition=batch_condition,
            event_type="ALARM",
            started_at=started_at,
            occurred_at=started_at,
        )

        for event_key, condition, _first_seen in individual_new:
            conn.execute(
                """
                INSERT INTO alert_episode(
                    event_key,host,module,channel,condition_kind,active,
                    started_at,last_seen_at,cleared_at,affected_json,status_line,
                    alarm_email_sent_at,recovery_email_sent_at
                )
                VALUES(?,?,?,?,?,1,?,?,NULL,?,?,NULL,NULL)
                ON CONFLICT(event_key) DO UPDATE SET
                    active=1,
                    condition_kind=excluded.condition_kind,
                    started_at=excluded.started_at,
                    last_seen_at=excluded.last_seen_at,
                    cleared_at=NULL,
                    affected_json=excluded.affected_json,
                    status_line=excluded.status_line,
                    alarm_email_sent_at=excluded.alarm_email_sent_at,
                    recovery_email_sent_at=NULL
                """,
                (
                    event_key,
                    meta["host"],
                    meta["module"],
                    meta["channel"],
                    condition.kind,
                    started_at.isoformat(),
                    observed_at.isoformat(),
                    json.dumps(condition.affected, ensure_ascii=False),
                    condition.status_line,
                ),
            )
            insert_history(
                conn,
                condition=condition,
                event_type="ALARM",
                occurred_at=started_at,
                started_at=started_at,
            )

        queue_email(
            conn,
            notification_key=(
                f"ALARM|{batch_condition.event_key}|{started_at.isoformat()}|"
                + ",".join(event_key for event_key, _, _ in individual_new)
            ),
            carrier_key=key,
            event_type="ALARM",
            episode_targets=[
                {
                    "event_key": event_key,
                    "started_at": first_seen.isoformat(),
                    "cleared_at": None,
                }
                for event_key, _, first_seen in individual_new
            ],
            message=msg,
            occurred_at=started_at,
        )

        log.error(
            "MULTIPLEX ALARM RECORDED + EMAIL QUEUED | %s | "
            "services=%d | sids=%s",
            key,
            len(affected),
            ",".join(str(sid) for sid, _ in affected),
        )

    # Same alarm still active: refresh only; never repeat email.
    for event_key, condition in continuing_items:
        conn.execute(
            """
            UPDATE alert_episode
            SET last_seen_at=?,condition_kind=?,affected_json=?,status_line=?
            WHERE event_key=?
            """,
            (
                observed_at.isoformat(),
                condition.kind,
                json.dumps(condition.affected, ensure_ascii=False),
                condition.status_line,
                event_key,
            ),
        )

    # --- RECOVERIES ---
    individual_recoveries: list[tuple[str, sqlite3.Row, Condition, datetime, datetime]] = []
    nonindividual_recoveries: list[tuple[str, sqlite3.Row, Condition, datetime, datetime]] = []

    for event_key, row, recovery_first_seen in recovery_items:
        started_at = parse_dt(str(row["started_at"]))
        affected = tuple(
            (int(item[0]), str(item[1]))
            for item in json.loads(str(row["affected_json"]))
        )
        condition = Condition(
            kind=str(row["condition_kind"]),
            event_key=event_key,
            affected=affected,
            status_line=str(row["status_line"]),
        )
        item = (event_key, row, condition, started_at, recovery_first_seen)
        if condition.kind == "individual_service_down":
            individual_recoveries.append(item)
        else:
            nonindividual_recoveries.append(item)

    for event_key, row, condition, started_at, recovery_first_seen in nonindividual_recoveries:
        # Persistence decides whether to notify; the reported recovery time is
        # the first authoritative healthy observation.
        cleared_at = recovery_first_seen

        # An execution_failure represents loss of monitoring-path certainty,
        # not a proven transmission outage. When authoritative monitoring
        # resumes, close the episode silently. Do not render/send a channel-UP
        # recovery email, because that would falsely claim transmission recovery.
        if condition.kind == "execution_failure":
            conn.execute(
                """
                UPDATE alert_episode
                SET active=0,cleared_at=?,recovery_email_sent_at=NULL,last_seen_at=?
                WHERE event_key=?
                """,
                (
                    cleared_at.isoformat(),
                    cleared_at.isoformat(),
                    event_key,
                ),
            )
            insert_history(
                conn,
                condition=condition,
                event_type="RECOVERY",
                occurred_at=cleared_at,
                started_at=started_at,
                cleared_at=cleared_at,
            )
            log.info(
                "MONITORING PATH RESTORED | %s | transmission recovery email suppressed",
                event_key,
            )
            continue

        # The first authoritative healthy monitor observation is the
        # canonical UP timestamp. Do not replace either episode boundary with
        # WISI native event-log timestamps; persistence-qualified monitor
        # observations remain authoritative for notification and downtime.

        msg = make_email(
            meta=meta,
            condition=condition,
            event_type="RECOVERY",
            started_at=started_at,
            occurred_at=cleared_at,
        )

        conn.execute(
            """
            UPDATE alert_episode
            SET active=0,cleared_at=?,recovery_email_sent_at=NULL,last_seen_at=?
            WHERE event_key=?
            """,
            (
                cleared_at.isoformat(),
                cleared_at.isoformat(),
                event_key,
            ),
        )
        insert_history(
            conn,
            condition=condition,
            event_type="RECOVERY",
            occurred_at=cleared_at,
            started_at=started_at,
            cleared_at=cleared_at,
        )
        queue_email(
            conn,
            notification_key=f"RECOVERY|{event_key}|{cleared_at.isoformat()}",
            carrier_key=key,
            event_type="RECOVERY",
            episode_targets=[
                {
                    "event_key": event_key,
                    "started_at": started_at.isoformat(),
                    "cleared_at": cleared_at.isoformat(),
                }
            ],
            message=msg,
            occurred_at=cleared_at,
        )
        log.info("RECOVERY RECORDED + EMAIL QUEUED | %s", event_key)

    # All service recoveries detected together on this carrier are one email.
    if individual_recoveries:
        affected: list[tuple[int, str]] = []
        starts: list[datetime] = []
        recovery_starts: list[datetime] = []
        for _, _, condition, started_at, recovery_first_seen in individual_recoveries:
            affected.extend(condition.affected)
            starts.append(started_at)
            recovery_starts.append(recovery_first_seen)

        affected = sorted(dict(affected).items())

        # Batched service alarms created by this version share one observed_at.
        # If legacy rows differ, use the earliest start so the single multiplex
        # notification does not understate downtime.
        batch_started_at = min(starts)
        cleared_at = min(recovery_starts)
        batch_condition = Condition(
            kind="individual_service_down",
            event_key=f"{key}|service_down|MULTIPLEX_BATCH",
            affected=tuple(affected),
            status_line="✅ Multiplex service(s) UP",
        )

        msg = make_email(
            meta=meta,
            condition=batch_condition,
            event_type="RECOVERY",
            started_at=batch_started_at,
            occurred_at=cleared_at,
        )

        for event_key, row, condition, started_at, _recovery_first_seen in individual_recoveries:
            conn.execute(
                """
                UPDATE alert_episode
                SET active=0,cleared_at=?,recovery_email_sent_at=NULL,last_seen_at=?
                WHERE event_key=?
                """,
                (
                    cleared_at.isoformat(),
                    cleared_at.isoformat(),
                    event_key,
                ),
            )
            insert_history(
                conn,
                condition=condition,
                event_type="RECOVERY",
                occurred_at=cleared_at,
                started_at=started_at,
                cleared_at=cleared_at,
            )

        queue_email(
            conn,
            notification_key=(
                f"RECOVERY|{batch_condition.event_key}|{cleared_at.isoformat()}|"
                + ",".join(
                    event_key
                    for event_key, _, _, _, _ in individual_recoveries
                )
            ),
            carrier_key=key,
            event_type="RECOVERY",
            episode_targets=[
                {
                    "event_key": event_key,
                    "started_at": episode_started_at.isoformat(),
                    "cleared_at": cleared_at.isoformat(),
                }
                for (
                    event_key,
                    _,
                    _,
                    episode_started_at,
                    _,
                ) in individual_recoveries
            ],
            message=msg,
            occurred_at=cleared_at,
        )

        log.info(
            "MULTIPLEX RECOVERY RECORDED + EMAIL QUEUED | %s | "
            "services=%d | sids=%s",
            key,
            len(affected),
            ",".join(str(sid) for sid, _ in affected),
        )


def process_resolved_history(
    conn: sqlite3.Connection,
    monitor_conn: sqlite3.Connection,
    log: logging.Logger,
    key: str,
    meta: dict[str, Any],
    latest_snapshot: dict[str, Any],
) -> int:
    """
    Process every unseen authoritative collector observation for one resolved
    carrier in chronological order.

    The policy database transaction remains controlled by run_once(). The
    cursor is therefore committed atomically with the corresponding alarm
    state/history/outbox changes.

    Returns the number of collector observations processed.
    """

    # A persistent cursor proves that this administrative carrier previously
    # had a successfully resolved WISI path.
    cursor = get_carrier_sample_cursor(
        conn,
        key,
    )

    # If that established path is absent from the CURRENT mapping generation,
    # interpret it as expected-path disappearance. A carrier that has never
    # been successfully resolved has no cursor and remains fail-closed as
    # MONITORING_PATH_UNAVAILABLE.
    if (
        latest_snapshot.get("execution_error")
        == "MONITORING_PATH_UNAVAILABLE"
        and cursor is not None
    ):
        established_absence = dict(
            latest_snapshot
        )

        established_absence[
            "execution_error"
        ] = None

        established_absence[
            "expected_path_absent"
        ] = True

        process_snapshot(
            conn,
            log,
            key,
            meta,
            established_absence,
        )

        return 1

    # A never-established path with no cursor is monitoring uncertainty.
    # It is neither affirmative DOWN evidence nor affirmative recovery
    # evidence. Preserve any established active episode until authoritative
    # observations become available again.
    if (
        latest_snapshot.get("execution_error")
        == "MONITORING_PATH_UNAVAILABLE"
        and cursor is None
    ):
        conn.execute(
            """
            DELETE FROM transition_candidate
            WHERE event_key LIKE ?
            """,
            (f"{key}|%",),
        )

        log.warning(
            "MONITORING PATH UNKNOWN - ACTIVE STATE PRESERVED | %s | "
            "no_cursor=1 | alarm=NOT_INFERRED | recovery=NOT_INFERRED",
            key,
        )

        return 0

    # Other execution/mapping failures retain the existing processing path.
    if (
        latest_snapshot.get("execution_error")
        or latest_snapshot.get("expected_path_absent")
    ):
        process_snapshot(
            conn,
            log,
            key,
            meta,
            latest_snapshot,
        )
        return 1

    configured_tuner_db_id = latest_snapshot.get(
        "configured_tuner_db_id"
    )

    resolved_tuner_db_id = latest_snapshot.get(
        "resolved_tuner_db_id"
    )

    if (
        configured_tuner_db_id is None
        or resolved_tuner_db_id is None
    ):
        process_snapshot(
            conn,
            log,
            key,
            meta,
            latest_snapshot,
        )
        return 1

    configured_tuner_db_id = int(
        configured_tuner_db_id
    )

    resolved_tuner_db_id = int(
        resolved_tuner_db_id
    )

    latest_at = parse_dt(
        str(latest_snapshot["observed_at"])
    )

    mapping_changed = False

    if cursor is None:
        # First commissioning pass:
        # inspect only a short recent window so a fault that is already
        # sustained now can qualify without replaying old incidents.
        after_at = latest_at - timedelta(
            seconds=CURSOR_BOOTSTRAP_HISTORY_SECONDS
        )

    else:
        previous_tuner_db_id = cursor[
            "configured_tuner_db_id"
        ]

        previous_resolved_tuner_db_id = cursor[
            "resolved_tuner_db_id"
        ]

        configured_path_changed = (
            previous_tuner_db_id is not None
            and int(previous_tuner_db_id)
                != configured_tuner_db_id
        )

        resolved_path_changed = (
            previous_resolved_tuner_db_id is not None
            and int(previous_resolved_tuner_db_id)
                != resolved_tuner_db_id
        )

        if (
            configured_path_changed
            or resolved_path_changed
        ):
            mapping_changed = True

            # Never accumulate pending persistence across a tuner-path change.
            # Established episodes remain preserved; only incomplete
            # ALARM/RECOVERY persistence evidence is discarded.
            conn.execute(
                """
                DELETE FROM transition_candidate
                WHERE event_key LIKE ?
                """,
                (f"{key}|%",),
            )

            after_at = latest_at - timedelta(
                seconds=CURSOR_BOOTSTRAP_HISTORY_SECONDS
            )

            log.warning(
                "CARRIER PATH ID CHANGED | %s | "
                "configured:%s->%s | "
                "resolved:%s->%s | "
                "pending persistence reset",
                key,
                previous_tuner_db_id,
                configured_tuner_db_id,
                previous_resolved_tuner_db_id,
                resolved_tuner_db_id,
            )
        else:
            after_at = parse_dt(
                str(
                    cursor[
                        "last_processed_sampled_at"
                    ]
                )
            )

    # We intentionally do not replay arbitrarily old evidence after a long
    # policy shutdown. The normal sweep is ~90 seconds, while the established
    # freshness guard is 120 seconds, so this retains enough history for the
    # real operational gap while avoiding stale alarm generation.
    freshness_floor = latest_at - timedelta(
        seconds=SNAPSHOT_STALE_SECONDS
    )

    if after_at < freshness_floor:
        after_at = freshness_floor

        # Any pending transition that depended on evidence older than the
        # retained authoritative window is no longer continuous.
        conn.execute(
            """
            DELETE FROM transition_candidate
            WHERE event_key LIKE ?
            """,
            (f"{key}|%",),
        )

        log.warning(
            "CARRIER CURSOR BEHIND FRESHNESS WINDOW | %s | "
            "persistence candidates reset",
            key,
        )

    samples = read_resolved_sample_history(
        monitor_conn,
        base_snapshot=latest_snapshot,
        after_sampled_at=after_at,
        until_sampled_at=latest_at,
    )

    if not samples:
        # This can occur if the cursor already equals the latest collector
        # timestamp. Do not manufacture a duplicate observation.
        if cursor is None or mapping_changed:
            set_carrier_sample_cursor(
                conn,
                carrier_key=key,
                sampled_at=latest_at,
                configured_tuner_db_id=
                    configured_tuner_db_id,
                    resolved_tuner_db_id=
                        resolved_tuner_db_id,
            )

        return 0

    processed = 0

    for sample in samples:
        process_snapshot(
            conn,
            log,
            key,
            meta,
            sample,
        )

        sample_at = parse_dt(
            str(sample["observed_at"])
        )

        set_carrier_sample_cursor(
            conn,
            carrier_key=key,
            sampled_at=sample_at,
            configured_tuner_db_id=
                configured_tuner_db_id,
                resolved_tuner_db_id=
                    resolved_tuner_db_id,
        )

        processed += 1

    log.debug(
        "SEQUENTIAL COLLECTOR HISTORY PROCESSED | %s | "
        "samples=%d | first=%s | last=%s",
        key,
        processed,
        samples[0]["observed_at"],
        samples[-1]["observed_at"],
    )

    return processed

def run_once(log: logging.Logger, *, strict: bool = False) -> int:
    manifest = load_manifest()
    expected_services = load_expected_services()
    validate_expected_services(manifest, expected_services)
    failures = 0
    with closing(open_db()) as conn, closing(open_monitor_db()) as monitor_conn:
        ensure_schema(conn)
        for key, meta in manifest.items():
            try:
                snapshot = read_monitor_snapshot(monitor_conn, meta, expected_services)
                if snapshot is None:
                    log.warning("No central DB sample available | %s", key)
                    continue
                process_resolved_history(
                    conn,
                    monitor_conn,
                    log,
                    key,
                    meta,
                    snapshot,
                )
                conn.commit()
            except Exception:
                failures += 1
                conn.rollback()
                log.exception("Policy processing failed | %s", key)
        # All carrier alarm-state transactions above have already been
        # committed independently. SMTP delivery is deliberately outside those
        # transactions so an SMTP outage can never roll back alarm state.
        try:
            deliver_pending_emails(conn, log)
        except Exception:
            failures += 1
            conn.rollback()
            log.exception("Email outbox processing failed")

    if strict and failures:
        raise RuntimeError(f"{failures} alarm-policy processing failure(s)")
    return failures



def rearm_current_active_episodes(log: logging.Logger) -> int:
    """
    One-time commissioning action.

    Marks only CURRENTLY ACTIVE episodes inactive so the next strict pass sends
    one fresh alarm email for each currently detected fault. This is used once
    during final installation to prove end-to-end email delivery. Normal
    runtime behavior remains unchanged: no repeated email while an episode
    stays active.
    """
    with closing(open_db()) as conn:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT event_key FROM alert_episode WHERE active=1"
        ).fetchall()
        count = len(rows)
        if count:
            conn.execute(
                """
                UPDATE alert_episode
                SET active=0,
                    cleared_at=COALESCE(cleared_at, ?),
                    recovery_email_sent_at=COALESCE(recovery_email_sent_at, ?)
                WHERE active=1
                """,
                (utcnow().isoformat(), utcnow().isoformat()),
            )
            conn.commit()
        log.warning(
            "COMMISSIONING REARM | prior active episodes reset=%d | "
            "next pass will send one fresh alarm for each current fault",
            count,
        )
        return count

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--rearm-current", action="store_true")
    ap.add_argument("--interval", type=float, default=CHECK_INTERVAL_SECONDS)
    args = ap.parse_args()

    log = configure_logging()
    log.info("=" * 100)
    log.info("WISI GT34 TV43 ALERT POLICY V8.1 STARTED | source=CENTRAL_SQLITE_5S")
    log.info(f"alarm_persistence={ALARM_PERSISTENCE_SECONDS}s | recovery_persistence={RECOVERY_PERSISTENCE_SECONDS}s | duplicate_email=SUPPRESSED | execution_failure=IMMEDIATE")
    log.info("logic=15s DOWN / 10s UP persistence ; dynamic RF identity resolver FAIL-CLOSED ; service-state monitoring")
    log.info("=" * 100)

    if args.rearm_current:
        rearm_current_active_episodes(log)

    while True:
        run_once(log, strict=args.strict)
        if args.once:
            return 0
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
