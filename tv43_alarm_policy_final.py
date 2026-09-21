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
RECOVERY_PERSISTENCE_SECONDS = 10
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
    """Resolve the administrative carrier to the current WISI path by RF identity.

    Permanent identity is the validated host/module + RF frequency/polarisation/
    symbol-rate fingerprint. Configured-input and tuner-object IDs are current
    routing attributes and are never derived from the administrative channel.
    """
    key = f"{meta['host']}|M{int(meta['module'])}C{int(meta['channel'])}"
    expected = expected_services[key]
    expected_rows = [{"sid": sid, "name": name} for sid, name in expected]

    module_row = monitor_conn.execute(
        """SELECT m.id AS module_id
           FROM chassis ch JOIN modules m ON m.chassis_id=ch.id
           WHERE ch.host=? AND m.module_number=? LIMIT 1""",
        (meta["host"], meta["module"]),
    ).fetchone()
    if module_row is None:
        return None
    module_id = int(module_row["module_id"])

    latest_cfg = monitor_conn.execute(
        "SELECT MAX(sampled_at) AS sampled_at FROM tuner_config_samples WHERE module_id=?",
        (module_id,),
    ).fetchone()
    sampled_at = None if latest_cfg is None else latest_cfg["sampled_at"]
    if sampled_at is None:
        return None
    sampled_at = str(sampled_at)

    # Exact administrative RF fingerprint with tiny representation tolerance.
    rf_rows = monitor_conn.execute(
        """SELECT * FROM tuner_config_samples
           WHERE module_id=? AND sampled_at=?
             AND ABS(frequency_mhz-?)<=?
             AND UPPER(COALESCE(polarisation,''))=?
             AND ABS(symbol_rate_mbd-?)<=?
           ORDER BY tuner_object_id""",
        (
            module_id, sampled_at, float(meta["frequency_mhz"]),
            RF_FREQUENCY_TOLERANCE_MHZ, str(meta["polarisation"]).upper(),
            float(meta["symbol_rate_mbd"]), RF_SYMBOL_RATE_TOLERANCE_MBD,
        ),
    ).fetchall()

    if not rf_rows:
        # A carrier that was previously positively observable and whose RF
        # configuration subsequently disappears is an alarmable expected-path
        # disappearance. A carrier never positively bound remains fail-closed.
        prior_observable = monitor_conn.execute(
            """SELECT 1
               FROM tuner_config_samples tc
               JOIN input_tuner_mapping_samples ms
                 ON ms.module_id=tc.module_id
                AND ms.sampled_at=tc.sampled_at
                AND ms.tuner_object_id=tc.tuner_object_id
               WHERE tc.module_id=? AND tc.sampled_at<?
                 AND ABS(tc.frequency_mhz-?)<=?
                 AND UPPER(COALESCE(tc.polarisation,''))=?
                 AND ABS(tc.symbol_rate_mbd-?)<=?
                 AND tc.enabled=1
                 AND ms.mapping_status='RESOLVED'
                 AND ms.input_enabled=1
               LIMIT 1""",
            (
                module_id, sampled_at, float(meta["frequency_mhz"]),
                RF_FREQUENCY_TOLERANCE_MHZ, str(meta["polarisation"]).upper(),
                float(meta["symbol_rate_mbd"]), RF_SYMBOL_RATE_TOLERANCE_MBD,
            ),
        ).fetchone()
        if prior_observable is not None:
            return {
                "host": meta["host"], "module": int(meta["module"]),
                "channel": int(meta["channel"]), "observed_at": sampled_at,
                "execution_error": None, "expected_path_absent": True,
                "channels": {}, "expected_services": expected_rows,
                "missing_services": [], "es_missing_services": [],
                "source": "central_sqlite_dynamic_identity",
                "configured_input_id": None, "resolved_tuner_object_id": None,
                "configured_name": None, "configured_uuid": None,
            }
        return _monitoring_path_unavailable(meta, expected_rows, sampled_at)

    candidate_tuners = {int(r["tuner_object_id"]) for r in rf_rows}
    placeholders = ",".join("?" for _ in candidate_tuners)
    mapping_rows = monitor_conn.execute(
        f"""SELECT * FROM input_tuner_mapping_samples
            WHERE module_id=? AND sampled_at=?
              AND tuner_object_id IN ({placeholders})
              AND mapping_status='RESOLVED'
            ORDER BY configured_input_id""",
        (module_id, sampled_at, *sorted(candidate_tuners)),
    ).fetchall()

    # Rank only current relationships. Administrative/WISI name agreement is
    # strongest; an enabled relationship is the secondary discriminator. A tie
    # is deliberately unresolved rather than guessed.
    admin_name = _identity_name(meta.get("identity_name"))
    ranked: list[tuple[tuple[int, int], sqlite3.Row]] = []
    for row in mapping_rows:
        name_match = int(bool(admin_name) and _identity_name(row["configured_name"]) == admin_name)
        enabled = int(row["input_enabled"] == 1)
        ranked.append(((name_match, enabled), row))

    if not ranked:
        # RF identity exists but there is no current configured-input route.
        return _monitoring_path_unavailable(meta, expected_rows, sampled_at)

    best_score = max(score for score, _ in ranked)
    winners = [row for score, row in ranked if score == best_score]
    if len(winners) != 1:
        return _monitoring_path_unavailable(meta, expected_rows, sampled_at)

    mapping = winners[0]
    configured_input_id = int(mapping["configured_input_id"])
    tuner_object_id = int(mapping["tuner_object_id"])

    # A configured RF identity on a disabled tuner/input is known but not
    # currently observable. Do not manufacture a transmission DOWN state.
    rf = next(r for r in rf_rows if int(r["tuner_object_id"]) == tuner_object_id)
    if rf["enabled"] != 1 or mapping["input_enabled"] != 1:
        return _monitoring_path_unavailable(
            meta, expected_rows, sampled_at,
            configured_input_id=configured_input_id,
            tuner_object_id=tuner_object_id,
            configured_name=mapping["configured_name"],
            configured_uuid=mapping["configured_uuid"],
        )

    # Demodulator health follows the resolved WISI tuner object.
    tuner = monitor_conn.execute(
        """SELECT ts.lock_state,ts.enabled,ts.state,ts.disabled
           FROM tuners t JOIN tuner_samples ts ON ts.tuner_id=t.id
           WHERE t.module_id=? AND t.input_id=? AND ts.sampled_at=?
           ORDER BY ts.id DESC LIMIT 1""",
        (module_id, tuner_object_id, sampled_at),
    ).fetchone()
    if tuner is None:
        return _monitoring_path_unavailable(
            meta, expected_rows, sampled_at,
            configured_input_id=configured_input_id,
            tuner_object_id=tuner_object_id,
            configured_name=mapping["configured_name"],
            configured_uuid=mapping["configured_uuid"],
        )

    # TS/service acquisition is stored by configured logical input, which may
    # differ from the resolved demodulator object (e.g. Virtual TV).
    configured = monitor_conn.execute(
        "SELECT id AS tuner_id FROM tuners WHERE module_id=? AND input_id=? LIMIT 1",
        (module_id, configured_input_id),
    ).fetchone()
    if configured is None:
        return _monitoring_path_unavailable(
            meta, expected_rows, sampled_at,
            configured_input_id=configured_input_id,
            tuner_object_id=tuner_object_id,
            configured_name=mapping["configured_name"],
            configured_uuid=mapping["configured_uuid"],
        )
    configured_db_id = int(configured["tuner_id"])

    ts_row = monitor_conn.execute(
        """SELECT current_bitrate_bps FROM ts_samples
           WHERE tuner_id=? AND sampled_at=? ORDER BY id DESC LIMIT 1""",
        (configured_db_id, sampled_at),
    ).fetchone()
    service_rows = monitor_conn.execute(
        """SELECT service_id,service_name FROM service_samples
           WHERE tuner_id=? AND sampled_at=? ORDER BY service_id""",
        (configured_db_id, sampled_at),
    ).fetchall()

    bitrate = None if ts_row is None else ts_row["current_bitrate_bps"]
    current_services = {
        int(r["service_id"]): str(r["service_name"] or f"SID {r['service_id']}")
        for r in service_rows
    }
    missing = [
        {"sid": sid, "name": name}
        for sid, name in expected if sid not in current_services
    ]
    return {
        "host": meta["host"], "module": int(meta["module"]),
        "channel": int(meta["channel"]), "observed_at": sampled_at,
        "execution_error": None,
        "channels": {
            "Demod Lock": int(tuner["lock_state"] or 0),
            "Transport Stream Present": 1 if bitrate is not None and float(bitrate) > 0 else 0,
            "Input Enabled": mapping["input_enabled"],
            "Input State": tuner["state"],
            "Input Disabled": tuner["disabled"],
        },
        "expected_services": expected_rows, "missing_services": missing,
        "es_missing_services": [], "source": "central_sqlite_dynamic_identity",
        "configured_input_id": configured_input_id,
        "resolved_tuner_object_id": tuner_object_id,
        "configured_name": mapping["configured_name"],
        "configured_uuid": mapping["configured_uuid"],
    }


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

    locked = int(ch.get("Demod Lock", 0) or 0) == 1
    ts_up = int(ch.get("Transport Stream Present", 0) or 0) == 1

    # CONDITION 1: carrier lost -> all channels on carrier DOWN.
    if not locked:
        return [
            Condition(
                kind="carrier_unlocked",
                event_key=f"{prefix}|carrier_down",
                affected=expected,
                status_line="❌ Carrier UNLOCKED",
            )
        ]

    # CONDITION 2: carrier locked but TS unavailable -> all channels DOWN.
    if not ts_up:
        return [
            Condition(
                kind="transport_stream_down",
                event_key=f"{prefix}|carrier_down",
                affected=expected,
                status_line="❌ Carrier LOCKED + Transport Stream DOWN",
            )
        ]

    # CONDITION 3: carrier/TS work; only the failed individual services DOWN.
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
    qualified = (observed_at - first_seen).total_seconds() >= threshold_seconds
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
    # state and not an execution failure. Preserve every existing episode and
    # candidate exactly as-is; infer neither Channel DOWN nor Channel UP.
    if snapshot.get("execution_error") == "MONITORING_PATH_UNAVAILABLE":
        return

    current = {c.event_key: c for c in derive_conditions(snapshot)}

    active_rows = conn.execute(
        """
        SELECT * FROM alert_episode
        WHERE host=? AND module=? AND channel=? AND active=1
        """,
        (meta["host"], meta["module"], meta["channel"]),
    ).fetchall()
    active = {str(row["event_key"]): row for row in active_rows}

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
        qualified, first_seen = _qualify_candidate(
            conn,
            condition=condition,
            direction="ALARM",
            observed_at=observed_at,
            threshold_seconds=ALARM_PERSISTENCE_SECONDS,
        )
        if qualified:
            _clear_candidate(conn, event_key, "ALARM")
            new_items.append((event_key, condition, first_seen))
            log.info(
                "ALARM PERSISTENCE QUALIFIED | %s | first_seen=%s | threshold=%ss",
                event_key, first_seen.isoformat(), ALARM_PERSISTENCE_SECONDS,
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
        if condition.kind != "execution_failure":
            # The qualifying observation occurs after persistence has elapsed.
            # Recover the start of the sustained condition from recent immutable
            # monitor samples where possible via native correlation for carrier
            # unlock; other conditions retain qualification time.
            pass
        if condition.kind == "carrier_unlocked":
            native_start, _ = native_carrier_times(
                meta, observed_at=observed_at, log=log
            )
            if native_start is not None:
                started_at = native_start
                log.info(
                    "Using WISI native DOWN time | %s | %s",
                    event_key,
                    local_time(started_at),
                )

        msg = make_email(
            meta=meta,
            condition=condition,
            event_type="ALARM",
            started_at=started_at,
            occurred_at=started_at,
        )
        send_message(msg)

        conn.execute(
            """
            INSERT INTO alert_episode(
                event_key,host,module,channel,condition_kind,active,
                started_at,last_seen_at,cleared_at,affected_json,status_line,
                alarm_email_sent_at,recovery_email_sent_at
            )
            VALUES(?,?,?,?,?,1,?,?,NULL,?,?,?,NULL)
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
                utcnow().isoformat(),
            ),
        )
        insert_history(
            conn,
            condition=condition,
            event_type="ALARM",
            occurred_at=started_at,
            started_at=started_at,
        )
        log.error(
            "ALARM EMAIL SENT IMMEDIATELY | %s | %s",
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
        send_message(msg)

        sent_at = utcnow().isoformat()
        for event_key, condition, _first_seen in individual_new:
            conn.execute(
                """
                INSERT INTO alert_episode(
                    event_key,host,module,channel,condition_kind,active,
                    started_at,last_seen_at,cleared_at,affected_json,status_line,
                    alarm_email_sent_at,recovery_email_sent_at
                )
                VALUES(?,?,?,?,?,1,?,?,NULL,?,?,?,NULL)
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
                    sent_at,
                ),
            )
            insert_history(
                conn,
                condition=condition,
                event_type="ALARM",
                occurred_at=started_at,
                started_at=started_at,
            )

        log.error(
            "MULTIPLEX ALARM EMAIL SENT IMMEDIATELY | %s | services=%d | sids=%s",
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

        if condition.kind == "carrier_unlocked":
            native_start, native_end = native_carrier_times(
                meta,
                observed_at=recovery_first_seen,
                started_at=started_at,
                recovery=True,
                log=log,
            )
            if native_start is not None and native_end is not None:
                started_at = native_start
                cleared_at = native_end
                log.info(
                    "Using WISI native UP time | %s | %s",
                    event_key,
                    local_time(cleared_at),
                )

        msg = make_email(
            meta=meta,
            condition=condition,
            event_type="RECOVERY",
            started_at=started_at,
            occurred_at=cleared_at,
        )
        send_message(msg)

        conn.execute(
            """
            UPDATE alert_episode
            SET active=0,cleared_at=?,recovery_email_sent_at=?,last_seen_at=?
            WHERE event_key=?
            """,
            (
                cleared_at.isoformat(),
                utcnow().isoformat(),
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
        log.info("RECOVERY EMAIL SENT IMMEDIATELY | %s", event_key)

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
        send_message(msg)

        sent_at = utcnow().isoformat()
        for event_key, row, condition, started_at, _recovery_first_seen in individual_recoveries:
            conn.execute(
                """
                UPDATE alert_episode
                SET active=0,cleared_at=?,recovery_email_sent_at=?,last_seen_at=?
                WHERE event_key=?
                """,
                (
                    cleared_at.isoformat(),
                    sent_at,
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
            "MULTIPLEX RECOVERY EMAIL SENT IMMEDIATELY | %s | services=%d | sids=%s",
            key,
            len(affected),
            ",".join(str(sid) for sid, _ in affected),
        )


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
                process_snapshot(conn, log, key, meta, snapshot)
                conn.commit()
            except Exception:
                failures += 1
                conn.rollback()
                log.exception("Policy processing failed | %s", key)
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
